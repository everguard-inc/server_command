#!/usr/bin/env python3
"""Sync servers.json pipelines from S3 servers db (eg-configs/servers.json)."""

import argparse
import os
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from eg_basics.utils import (
  DefaultCfgBucket,
  DefaultCfgDbFile,
  S3_HEADER,
  get_server_db,
)

from pipeline_ip import (
  edge_status_ip_from_cfg,
  load_pipeline_cfg,
  streaming_ip_from_cfg,
  system_monitor_host_from_cfg,
)
from servers_cfg import (
  SYS_MONITOR_PATH,
  SYS_MONITOR_SERVICE,
  default_service_name,
  eg_service_name,
  is_kafka_pipeline,
  is_rtls_pipeline,
  is_sys_monitor_entry,
  is_usable_edge_host,
  pipeline_path_for,
  read_servers_file,
  split_servers_raw,
  write_servers_file,
)

DEFAULT_WORKERS = 12
PIPELINE_FIELD_ORDER = (
  "server_ip",
  "monitor_host_ip",
  "server_id",
  "service_name",
  "eg_pipeline_path",
  "sys_monitor_path",
)
# Optional plc_status_* / drift_service_* keys are preserved via the extra-keys
# pass when present locally; runtime builds defaults from server_ip.
SYS_MONITOR_FIELD_ORDER = (
  "server_ip",
  "server_id",
  "service_name",
  "sys_monitor_path",
)
SKIP_NAME_MARKERS = ("base", "crane")


def find_site(xsite_id, server_db):
  for label, site in server_db.items():
    if site.get("xSiteId") == xsite_id:
      return label, site.get("servers") or {}
  return None, None


def pipeline_group(name):
  return name.split("-", 1)[0] if "-" in name else "Other"


def pipeline_tier(name):
  if is_sys_monitor_entry(name=name):
    return 0
  if is_rtls_pipeline(name=name):
    return 1
  return 2


def sort_pipeline_names(names):
  group_first_index = {}
  original_index = {}
  for index, name in enumerate(names):
    original_index[name] = index
    group = pipeline_group(name)
    group_first_index.setdefault(group, index)
  return sorted(
    names,
    key=lambda name: (
      group_first_index[pipeline_group(name)],
      pipeline_tier(name),
      original_index[name],
    ),
  )


def pipeline_names_in_order(site_servers):
  skipped = []
  names = []
  for name in site_servers:
    if should_skip_pipeline(name):
      skipped.append(name)
    else:
      names.append(name)
  return sort_pipeline_names(names), skipped


def s3_servers_path(bucket=DefaultCfgBucket, db_file=DefaultCfgDbFile):
  return S3_HEADER + os.path.join(bucket, db_file)


def load_s3_server_db(bucket=DefaultCfgBucket, db_file=DefaultCfgDbFile):
  server_db = get_server_db(bucket=bucket, server_db_file=db_file)
  if not server_db:
    raise SystemExit(f"Failed to load S3 servers db: {s3_servers_path(bucket, db_file)}")
  return server_db


def resolve_xsite_id(override, meta, server_db, s3_path):
  candidates = [override, meta.get("xSiteId"), os.environ.get("xSiteId")]
  for xsite_id in candidates:
    if not xsite_id:
      continue
    _, site_servers = find_site(xsite_id, server_db)
    if site_servers is not None:
      return xsite_id
  tried = [c for c in candidates if c]
  hint = " Pass --xsite-id, set xSiteId in servers.json, or export xSiteId."
  raise SystemExit(
    f"xSiteId not found in {s3_path}. Tried: {tried or ['(none)']}.{hint}"
  )


def should_skip_pipeline(name):
  lower = name.lower()
  return any(marker in lower for marker in SKIP_NAME_MARKERS)


def needs_ip_fetch(local_entry, fetch_all=False):
  if fetch_all:
    return True
  return not is_usable_edge_host((local_entry or {}).get("server_ip"))


def needs_monitor_host_fetch(name, local_entry, fetch_all=False):
  if is_sys_monitor_entry(name=name, cfg=local_entry):
    return False
  if is_rtls_pipeline(name=name):
    return False
  if fetch_all:
    return True
  return not is_usable_edge_host((local_entry or {}).get("monitor_host_ip"))


def needs_cfg_fetch(name, local_entry, fetch_all=False):
  return needs_ip_fetch(local_entry, fetch_all) or needs_monitor_host_fetch(
    name, local_entry, fetch_all,
  )


def pipelines_needing_cfg_fetch(pipeline_names, local_servers, fetch_all=False):
  if fetch_all:
    return list(pipeline_names)
  return [
    name for name in pipeline_names
    if needs_cfg_fetch(name, local_servers.get(name, {}), fetch_all)
  ]


def _pipeline_name_for_server_id(site_servers, server_id):
  for pipeline_name, entry in site_servers.items():
    if entry.get("server_id") == server_id:
      return pipeline_name
  return ""


def _sync_fetch_note(fetch_all, fetch_elapsed, cfg_fetch_names, cfg_count):
  if fetch_all:
    return f"cfg fetch all {fetch_elapsed:.1f}s"
  if cfg_count:
    return f"cfg fetch missing {fetch_elapsed:.1f}s ({len(cfg_fetch_names)} pipelines)"
  return "cfg fetch skipped (all IPs present)"


def prefetch_cfg_cache(xsite_id, server_ids, site_servers, workers):
  items = list(dict.fromkeys(server_id for server_id in server_ids if server_id))

  def fetch_one(server_id):
    name = _pipeline_name_for_server_id(site_servers, server_id)
    return server_id, load_pipeline_cfg(xsite_id, name, server_id)

  cfg_cache = {}
  with ThreadPoolExecutor(max_workers=workers) as pool:
    for server_id, cfg in pool.map(fetch_one, items):
      cfg_cache[server_id] = cfg
  return cfg_cache, len(items)


def _resolved_server_ip(cfg, local_entry, extract_ip, *, require_usable=False):
  server_ip = extract_ip(cfg) if cfg is not None else None
  if not server_ip:
    server_ip = (local_entry or {}).get("server_ip")
  if require_usable:
    return server_ip if is_usable_edge_host(server_ip) else "0.0.0.0"
  return server_ip or "0.0.0.0"


def build_sys_monitor_entry(site_entry, cfg=None, local_entry=None):
  server_id = site_entry.get("server_id")
  if not server_id:
    return None

  local_entry = local_entry or {}
  return {
    "server_id": server_id,
    "service_name": local_entry.get("service_name") or SYS_MONITOR_SERVICE,
    "sys_monitor_path": SYS_MONITOR_PATH,
    "server_ip": _resolved_server_ip(
      cfg, local_entry, edge_status_ip_from_cfg, require_usable=True,
    ),
  }


def build_pipeline_entry(pipeline_name, site_entry, cfg=None, local_entry=None):
  server_id = site_entry.get("server_id")
  if not server_id:
    return None

  local_entry = local_entry or {}
  eg_path = (
    str(local_entry.get("eg_pipeline_path") or "").strip()
    or pipeline_path_for(pipeline_name)
  )
  entry = {
    "server_id": server_id,
    "service_name": local_entry.get("service_name") or default_service_name(pipeline_name, server_id),
    "eg_pipeline_path": eg_path,
    "server_ip": _resolved_server_ip(cfg, local_entry, streaming_ip_from_cfg),
  }
  # Keep local absence of sys_monitor_path (e.g. PLC-Kafka). New non-kafka AWS
  # pipelines still get the default path so Update keeps building system_monitor.
  if "sys_monitor_path" in local_entry:
    sys_path = str(local_entry.get("sys_monitor_path") or "").strip()
    if sys_path:
      entry["sys_monitor_path"] = sys_path
  elif not is_kafka_pipeline(name=pipeline_name, cfg={"eg_pipeline_path": eg_path}):
    entry["sys_monitor_path"] = SYS_MONITOR_PATH
  monitor_host = system_monitor_host_from_cfg(cfg) or local_entry.get("monitor_host_ip")
  if is_usable_edge_host(monitor_host):
    entry["monitor_host_ip"] = monitor_host
  return entry


def load_cfg_for_entry(xsite_id, name, server_id, local_entry, cfg_cache, fetch_all):
  cfg = cfg_cache.get(server_id)
  if cfg is None and needs_cfg_fetch(name, local_entry, fetch_all):
    cfg = load_pipeline_cfg(xsite_id, name, server_id)
  return cfg


def build_sync_entry(name, site_entry, local_entry, cfg):
  if is_sys_monitor_entry(name=name):
    return build_sys_monitor_entry(site_entry, cfg, local_entry)
  return build_pipeline_entry(name, site_entry, cfg, local_entry)


def reorder_pipeline_entry(entry, pipeline_name=None):
  field_order = (
    SYS_MONITOR_FIELD_ORDER
    if is_sys_monitor_entry(name=pipeline_name, cfg=entry)
    else PIPELINE_FIELD_ORDER
  )
  ordered = {key: entry[key] for key in field_order if key in entry}
  if "service_name" not in ordered:
    ordered["service_name"] = (
      SYS_MONITOR_SERVICE
      if is_sys_monitor_entry(name=pipeline_name, cfg=entry)
      else eg_service_name(entry.get("server_id", ""))
    )
  # Keep any extra local-only keys (future fields) after the known order.
  for key, value in entry.items():
    if key not in ordered:
      ordered[key] = value
  return ordered


def compose_synced_servers(aws_servers, local_servers, site_servers, failed_names):
  """Keep AWS sync results, preserve local-only pipelines not present in S3.

  Local file order is kept for entries that already existed locally; brand-new
  AWS pipelines are appended. Extra local keys (e.g. plc_status_url) survive
  on entries that exist in both.
  """
  final = OrderedDict()
  preserved = []
  failed = set(failed_names or [])
  aws_ok = set(aws_servers)

  for name, local_entry in local_servers.items():
    local = local_entry or {}
    if name in aws_ok:
      entry = dict(aws_servers[name])
      if local.get("eg_pipeline_path"):
        entry["eg_pipeline_path"] = local["eg_pipeline_path"]
      if local.get("service_name"):
        entry["service_name"] = local["service_name"]
      if "sys_monitor_path" in local:
        sys_path = str(local.get("sys_monitor_path") or "").strip()
        if sys_path:
          entry["sys_monitor_path"] = sys_path
        else:
          entry.pop("sys_monitor_path", None)
      for key, value in local.items():
        if key not in entry:
          entry[key] = value
      final[name] = reorder_pipeline_entry(entry, pipeline_name=name)
      continue
    if name not in site_servers or name in failed:
      # Not in S3 (manual local add), or S3 sync failed — keep local entry.
      final[name] = reorder_pipeline_entry(dict(local), pipeline_name=name)
      preserved.append(name)

  for name, entry in aws_servers.items():
    if name not in final:
      final[name] = entry

  return final, preserved


def main():
  parser = argparse.ArgumentParser(
    description="Sync all pipelines from S3 servers.json into local servers.json."
  )
  parser.add_argument("--servers", default="servers.json")
  parser.add_argument(
    "--xsite-id",
    default=None,
    help="xSiteId override (default: local servers.json meta, then xSiteId env)",
  )
  parser.add_argument(
    "--cfg-bucket",
    default=DefaultCfgBucket,
    help=f"S3 config bucket (default: {DefaultCfgBucket})",
  )
  parser.add_argument(
    "--cfg-db-file",
    default=DefaultCfgDbFile,
    help=f"S3 servers db file (default: {DefaultCfgDbFile})",
  )
  parser.add_argument(
    "--workers",
    type=int,
    default=DEFAULT_WORKERS,
    help=f"parallel S3 cfg downloads (default: {DEFAULT_WORKERS})",
  )
  parser.add_argument(
    "--fetch-ip",
    action="store_true",
    help="re-fetch server_ip from S3 for all pipelines (default: only missing/0.0.0.0)",
  )
  args = parser.parse_args()

  s3_path = s3_servers_path(args.cfg_bucket, args.cfg_db_file)
  server_db = load_s3_server_db(args.cfg_bucket, args.cfg_db_file)

  meta, local_servers = split_servers_raw(read_servers_file(args.servers))

  xsite_id = resolve_xsite_id(args.xsite_id, meta, server_db, s3_path)
  site_label, site_servers = find_site(xsite_id, server_db)

  print(f"Using xSiteId {xsite_id}" + (f" ({site_label})" if site_label else ""))
  print(f"Source: {s3_path}")

  pipeline_names, skipped = pipeline_names_in_order(site_servers)

  t0 = time.time()
  cfg_fetch_names = pipelines_needing_cfg_fetch(
    pipeline_names, local_servers, fetch_all=args.fetch_ip,
  )
  cfg_cache = {}
  cfg_count = 0
  if cfg_fetch_names:
    server_ids = [
      site_servers[name].get("server_id")
      for name in cfg_fetch_names
      if site_servers[name].get("server_id")
    ]
    cfg_cache, cfg_count = prefetch_cfg_cache(
      xsite_id, server_ids, site_servers, args.workers,
    )
  fetch_elapsed = time.time() - t0

  synced, failed = 0, []
  aws_servers = OrderedDict()

  for name in pipeline_names:
    local_entry = local_servers.get(name, {})
    site_entry = site_servers[name]
    try:
      server_id = site_entry.get("server_id")
      cfg = load_cfg_for_entry(
        xsite_id, name, server_id, local_entry, cfg_cache, args.fetch_ip,
      )
      entry = build_sync_entry(name, site_entry, local_entry, cfg)
    except Exception:
      entry = None
    if not entry:
      failed.append(name)
      continue
    aws_servers[name] = reorder_pipeline_entry(entry, pipeline_name=name)
    synced += 1

  servers, preserved = compose_synced_servers(
    aws_servers, local_servers, site_servers, failed,
  )
  write_servers_file(meta, servers, path=args.servers, xsite_id=xsite_id)

  elapsed = time.time() - t0
  fetch_note = _sync_fetch_note(args.fetch_ip, fetch_elapsed, cfg_fetch_names, cfg_count)
  print(
    f"Done in {elapsed:.1f}s ({fetch_note}): "
    f"synced {synced}/{len(pipeline_names)}, skipped {len(skipped)}, failed {len(failed)}, "
    f"preserved local-only {len(preserved)}, cfg fetched {cfg_count}"
  )
  for label, names in (
    ("Skipped", skipped),
    ("Failed", failed),
    ("Preserved local-only", preserved),
  ):
    if not names:
      continue
    shown = ", ".join(names[:10])
    print(f"{label}:", shown, "..." if len(names) > 10 else "")


if __name__ == "__main__":
  main()
