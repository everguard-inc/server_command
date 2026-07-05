"""Shared servers.json loading and edge URL helpers.

Used by proxy/sync/CLI and by edge app.py for shared constants and status helpers.
Edge app.py does not load servers.json; pipeline config arrives in POST /command JSON.
"""

import json
import os
import socket
from os.path import dirname, expanduser, join, realpath

_EDGE_HOME = expanduser("~")
DEFAULT_EDGE_PORT = 5502
SYS_MONITOR_SERVICE = "eg_monitor"
RTLS_SERVICE = "eg_rtls"
SYS_MONITOR_PATH = join(_EDGE_HOME, "system_monitor")
RTLS_PIPELINE_PATH = join(_EDGE_HOME, "rtls_server")
EG_PIPELINE_PATH = join(_EDGE_HOME, "eg_pipeline")
COBBLE_PIPELINE_PATH = join(_EDGE_HOME, "cobble-pipeline")
DETSEG_PIPELINE_PATH = join(_EDGE_HOME, "detseg_pipeline")
FORKLIFT_PIPELINE_PATH = join(_EDGE_HOME, "forklift_proximity")
DEFAULT_STREAM_FEED_PATH = "/data_feed"
ALT_STREAM_FEED_PATH = "/video_feed"
SERVERS_PATH = join(realpath(dirname(__file__)), "servers.json")
SKIP_STREAM_HOSTS = frozenset({"0.0.0.0", "127.0.0.1", "localhost", ""})

EDGE_STATUS_MERGE_KEYS = (
  "cameras_set", "cameras_now", "streaming_port", "streaming_ip",
  "qlight_set", "speaker_set", "qlight_now", "speaker_now",
  "qlight_links", "speaker_links", "camera_links",
  "qlight_status", "speaker_status", "camera_status",
  "stream_health", "status", "mem_usage", "mem_usage_percent",
  "version_current", "version_latest",
  "version_current_date", "version_latest_date",
  "is_rtls", "is_sys_monitor", "rtls_config_missing",
)

DEFAULT_EDGE_PROBE = {
  "stream_probe_interval_sec": 20,
  "stream_probe_workers": 4,
  "docker_stats_interval_sec": 30,
}

DEFAULT_STATUS_DISPLAY = {
  "mem_warn_percent": 80,
}

_PIPELINE_KINDS = (
  ("rtls", RTLS_PIPELINE_PATH, "RTLS", DEFAULT_STREAM_FEED_PATH),
  ("cobble", COBBLE_PIPELINE_PATH, "COBBLE", ALT_STREAM_FEED_PATH),
  ("detseg", DETSEG_PIPELINE_PATH, "DETSEG", DEFAULT_STREAM_FEED_PATH),
  ("forklift", FORKLIFT_PIPELINE_PATH, "FORKLIFT", DEFAULT_STREAM_FEED_PATH),
)
_PATH_BY_KIND = {keyword: path for keyword, path, _label, _feed in _PIPELINE_KINDS}
_FEED_PATH_BY_KIND = {keyword: feed for keyword, _path, _label, feed in _PIPELINE_KINDS}


def _normalize_nonneg_int_meta(raw, defaults):
  cfg = dict(defaults)
  if not isinstance(raw, dict):
    return cfg
  for key in defaults:
    if key not in raw or raw[key] is None:
      continue
    try:
      cfg[key] = max(0, int(raw[key]))
    except (TypeError, ValueError):
      pass
  return cfg


def _meta_subdict(meta, key):
  return (meta or {}).get(key) if isinstance(meta, dict) else None


def edge_probe_config(meta=None):
  return _normalize_nonneg_int_meta(_meta_subdict(meta, "edge_probe"), DEFAULT_EDGE_PROBE)


def normalize_edge_probe(config=None):
  """Normalize a bare edge_probe dict (e.g. from POST /command JSON)."""
  return _normalize_nonneg_int_meta(config, DEFAULT_EDGE_PROBE)


def status_display_config(meta=None):
  cfg = _normalize_nonneg_int_meta(
    _meta_subdict(meta, "status_display"),
    DEFAULT_STATUS_DISPLAY,
  )
  for key in DEFAULT_STATUS_DISPLAY:
    cfg[key] = min(100, max(0, cfg[key]))
  return cfg


def ensure_servers_meta(meta):
  merged = dict(meta or {})
  merged["edge_probe"] = edge_probe_config(merged)
  merged["status_display"] = status_display_config(merged)
  return merged


def pipeline_kind_for(name=None, cfg=None):
  if is_sys_monitor_entry(name=name, cfg=cfg):
    return "sys", "SYSTEM"
  lower = (name or "").lower()
  eg_path = (cfg or {}).get("eg_pipeline_path", "") if cfg else ""
  for keyword, path, label, _feed_path in _PIPELINE_KINDS:
    if keyword in lower or (eg_path and path in eg_path):
      return keyword, label
  return "eg", "EG"


def stream_feed_paths_for(name=None, cfg=None):
  cfg = cfg or {}
  raw = cfg.get("stream_feed_paths")
  if isinstance(raw, (list, tuple)) and len(raw) >= 2:
    primary, secondary = raw[0], raw[1]
    if primary and secondary:
      return primary, secondary
  kind, _ = pipeline_kind_for(name=name, cfg=cfg)
  primary = _FEED_PATH_BY_KIND.get(kind, DEFAULT_STREAM_FEED_PATH)
  secondary = (
    ALT_STREAM_FEED_PATH
    if primary == DEFAULT_STREAM_FEED_PATH
    else DEFAULT_STREAM_FEED_PATH
  )
  return primary, secondary


def stream_feed_path_for(name=None, cfg=None):
  return stream_feed_paths_for(name=name, cfg=cfg)[0]


def pipeline_path_for(name):
  kind, _ = pipeline_kind_for(name=name)
  return _PATH_BY_KIND.get(kind, EG_PIPELINE_PATH)


GITHUB_ORG = "everguard-inc"


def pipeline_git_url(name=None, cfg=None, repo_path=None):
  """Map a local pipeline path to its GitHub HTTPS remote."""
  path = repo_path
  if not path:
    if cfg and cfg.get("eg_pipeline_path"):
      path = cfg["eg_pipeline_path"]
    elif name:
      path = pipeline_path_for(name)
    else:
      path = EG_PIPELINE_PATH
  repo_name = path.rstrip("/").split("/")[-1]
  return f"https://github.com/{GITHUB_ORG}/{repo_name}.git"


def is_usable_stream_host(host):
  return bool(host) and host not in SKIP_STREAM_HOSTS


is_usable_edge_host = is_usable_stream_host


def pipeline_kind_flags(name=None, cfg=None):
  kind, label = pipeline_kind_for(name=name, cfg=cfg)
  return {
    "pipeline_kind": kind,
    "pipeline_kind_label": label,
    "is_sys_monitor": kind == "sys",
    "is_rtls": kind == "rtls",
  }


def _pipeline_edge_item(pipeline_cfg, *, name=None, extra=None):
  item = dict(extra or {})
  server_id = pipeline_cfg.get("server_id")
  if server_id:
    item["server_id"] = server_id
  if is_sys_monitor_entry(name=name, cfg=pipeline_cfg):
    item["is_sys_monitor"] = True
  elif is_rtls_pipeline(cfg=pipeline_cfg, name=name):
    item["is_rtls"] = True
  return item


def is_pipeline_entry(value):
  return isinstance(value, dict) and "server_id" in value


def is_sys_monitor_entry(name=None, cfg=None):
  if cfg and cfg.get("sys_monitor_only"):
    return True
  if name and "system" in name.lower():
    return True
  if cfg and cfg.get("sys_monitor_path") and not cfg.get("eg_pipeline_path"):
    return True
  return False


def is_rtls_pipeline(cfg=None, name=None):
  return pipeline_kind_for(name=name, cfg=cfg)[0] == "rtls"


def ensure_cli_command(command, allowed):
  if command is None:
    print("No Command Found")
    raise SystemExit(0)
  if command not in allowed:
    print("Command Name Error, Must be", ", ".join(allowed))
    raise SystemExit(0)


def resolve_cli_pipelines(pipelines, names=None):
  """Validate CLI pipeline names and return the target name list."""
  if not names:
    return list(pipelines.keys())
  unknown = [name for name in names if name not in pipelines]
  if unknown:
    print("Unknown pipeline(s):", ", ".join(unknown))
    raise SystemExit(1)
  return list(names)


def eg_service_name(server_id):
  if not server_id:
    return ""
  if server_id.startswith("eg_"):
    return server_id
  return f"eg_{server_id}"


def default_service_name(pipeline_name, server_id):
  if is_sys_monitor_entry(name=pipeline_name):
    return SYS_MONITOR_SERVICE
  if is_rtls_pipeline(name=pipeline_name):
    return RTLS_SERVICE
  return eg_service_name(server_id)


def pipeline_service_name(pipeline_cfg, *, name=None):
  existing = (pipeline_cfg or {}).get("service_name")
  if existing:
    return existing
  return default_service_name(name, (pipeline_cfg or {}).get("server_id"))


def device_counts_ok(expected, actual):
  if expected is None or expected <= 0:
    return True
  if actual is None:
    return False
  return expected == actual


def tcp_reachable(host, port, *, timeout=3.0):
  """TCP probe; empty host/port returns False."""
  if not host or not port:
    return False
  try:
    with socket.create_connection((host, int(port)), timeout=timeout):
      return True
  except OSError:
    return False


def tcp_reachable_optional(host, port, *, timeout=3.0):
  """TCP probe; empty host/port returns None (not applicable)."""
  if not host or not port:
    return None
  return tcp_reachable(host, port, timeout=timeout)


def merge_edge_status_data(entry, data, *, keys=EDGE_STATUS_MERGE_KEYS):
  if not isinstance(data, dict):
    return entry
  for key in keys:
    if key in data and data[key] is not None:
      entry[key] = data[key]
  entry["running"] = bool(data.get("running", entry.get("running")))
  return entry


def edge_check_payload(server, *, name):
  return json.dumps({
    "check": {
      "server_id": server["server_id"],
      "service_name": pipeline_service_name(server, name=name),
      **pipeline_kind_flags(name=name, cfg=server),
    },
  })


def edge_stop_payload(server_id):
  return json.dumps({"stop": server_id})


def sys_monitor_status_from_counts(running_count, total_count, monitor_running=False):
  if not monitor_running:
    return "ERR"
  if not total_count:
    return "OK"
  if running_count == total_count:
    return "OK"
  return "WARN"


def pipeline_monitor_host(name, pipeline_cfg):
  """SYS edge_status IP, or monitor_host_ip / system_monitor_url target for EG pipelines."""
  host = pipeline_cfg.get("monitor_host_ip")
  if is_usable_edge_host(host):
    return host
  if is_sys_monitor_entry(name=name, cfg=pipeline_cfg):
    server_ip = pipeline_cfg.get("server_ip")
    return server_ip if is_usable_edge_host(server_ip) else None
  return None


def monitored_pipeline_names(pipelines_cfg, monitor_host_ip):
  """EG pipelines whose system_monitor_url points at this SYS host (excludes SYS/RTLS)."""
  if not monitor_host_ip:
    return []
  names = []
  for name, pipeline_cfg in pipelines_cfg.items():
    if is_sys_monitor_entry(name=name, cfg=pipeline_cfg):
      continue
    if is_rtls_pipeline(name=name):
      continue
    if pipeline_monitor_host(name, pipeline_cfg) == monitor_host_ip:
      names.append(name)
  return names


def _apply_sys_status_for_sys_name(status_map, sys_name, monitored_names):
  running_count = 0
  pending = False
  for name in monitored_names:
    peer = status_map.get(name)
    if not peer or peer.get("status") == "PENDING":
      pending = True
      continue
    if peer.get("running"):
      running_count += 1
  entry = status_map.get(sys_name)
  if not entry or not entry.get("is_sys_monitor"):
    return
  if pending and monitored_names:
    entry["status"] = "WARN"
    return
  entry["status"] = sys_monitor_status_from_counts(
    running_count, len(monitored_names), entry.get("running"),
  )


apply_sys_monitor_peer_status = _apply_sys_status_for_sys_name


def apply_sys_monitor_host_status(status_map, pipelines_cfg, server_ip=None):
  """Set SYS OK/WARN/ERR from monitored EG pipelines (system_monitor_url → SYS IP)."""
  for name, pipeline_cfg in pipelines_cfg.items():
    if not is_sys_monitor_entry(name=name, cfg=pipeline_cfg):
      continue
    if server_ip and pipeline_cfg.get("server_ip") != server_ip:
      continue
    monitor_host = pipeline_monitor_host(name, pipeline_cfg)
    if not monitor_host:
      continue
    monitored = monitored_pipeline_names(pipelines_cfg, monitor_host)
    _apply_sys_status_for_sys_name(status_map, name, monitored)
  return status_map


def finalize_pipeline_status(entry):
  """Compute OK/WARN/ERR for a pipeline status entry (proxy and edge)."""
  if entry.get("is_sys_monitor"):
    entry["status"] = "OK" if entry.get("running") else "ERR"
    return entry

  if entry.get("is_rtls"):
    if not entry.get("running"):
      entry["status"] = "ERR"
      return entry
    if entry.get("rtls_config_missing"):
      entry["status"] = "WARN"
      return entry
    devices_ok = (
      device_counts_ok(entry.get("qlight_set"), entry.get("qlight_now"))
      and device_counts_ok(entry.get("speaker_set"), entry.get("speaker_now"))
    )
    entry["status"] = "OK" if devices_ok else "WARN"
    return entry

  if entry.get("running") and entry.get("stream_health"):
    cameras_ok = device_counts_ok(entry.get("cameras_set"), entry.get("cameras_now"))
    entry["status"] = "OK" if cameras_ok else "WARN"
  elif entry.get("running"):
    entry["status"] = "WARN"
  elif entry.get("status") not in ("OK", "WARN"):
    entry["status"] = "ERR"
  return entry


def read_servers_file(path=None):
  with open(path or SERVERS_PATH, encoding="utf-8") as f:
    return json.load(f)


def write_servers_file(meta, pipelines, *, path=None, xsite_id=None):
  output = ensure_servers_meta(dict(meta))
  if xsite_id:
    output["xSiteId"] = xsite_id
  output["servers"] = pipelines
  target = path or SERVERS_PATH
  with open(target, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=4)
    f.write("\n")


def split_servers_raw(raw):
  if isinstance(raw.get("servers"), dict):
    meta = {key: value for key, value in raw.items() if key != "servers"}
    return meta, dict(raw["servers"])

  meta = {}
  pipelines = {}
  for key, value in raw.items():
    if is_pipeline_entry(value):
      pipelines[key] = value
    else:
      meta[key] = value
  return meta, pipelines


def load_pipelines(path=None, require_server_ip=False):
  _, pipelines = split_servers_raw(read_servers_file(path))
  if require_server_ip:
    return {
      name: cfg for name, cfg in pipelines.items()
      if is_pipeline_entry(cfg) and cfg.get("server_ip")
    }
  return {name: cfg for name, cfg in pipelines.items() if is_pipeline_entry(cfg)}


def load_servers_cfg(path=None):
  return load_pipelines(path, require_server_ip=False)


def edge_port(server_cfg):
  return str(server_cfg.get("port", DEFAULT_EDGE_PORT))


def edge_command_url(server_cfg, host_ip=None):
  ip = host_ip or server_cfg.get("server_ip")
  return f"http://{ip}:{edge_port(server_cfg)}/command"


def group_by_server_ip(pipeline_names, pipelines):
  by_ip = {}
  for name in pipeline_names:
    ip = pipelines[name].get("server_ip")
    if ip and is_usable_edge_host(ip):
      by_ip.setdefault(ip, []).append(name)
  return by_ip


def edge_run_payload(command, pipeline_names, pipelines):
  pipeline_names = [
    name for name in pipeline_names
    if not is_sys_monitor_entry(name=name, cfg=pipelines[name])
  ]
  if not pipeline_names:
    raise ValueError("no runnable pipelines selected")
  first = pipelines[pipeline_names[0]]
  return json.dumps({
    "run": {
      "type": command,
      "json": pipeline_names,
      "path": first["eg_pipeline_path"],
    },
  })


def edge_service_payload(command, pipeline_cfg, *, name=None):
  return json.dumps(_pipeline_edge_item(
    pipeline_cfg,
    name=name,
    extra={command: pipeline_service_name(pipeline_cfg, name=name)},
  ))


def edge_host_command_payload(command, pipeline_names, pipelines):
  if command in ("stream", "watchdog"):
    return edge_run_payload(command, pipeline_names, pipelines)
  if command == "update":
    return edge_update_payload(pipeline_names, pipelines)
  raise ValueError(f"unsupported host command: {command!r}")


def _sys_monitor_cfg_for_update(pipeline_names, pipelines):
  """SYS entry for this update batch (same host), if present in pipelines."""
  for name in pipeline_names:
    cfg = pipelines.get(name)
    if cfg and is_sys_monitor_entry(name=name, cfg=cfg):
      return cfg
  host_ips = {
    pipelines[name].get("server_ip")
    for name in pipeline_names
    if name in pipelines and pipelines[name].get("server_ip")
  }
  if len(host_ips) != 1:
    return None
  server_ip = next(iter(host_ips))
  for name, cfg in pipelines.items():
    if not isinstance(cfg, dict):
      continue
    if is_sys_monitor_entry(name=name, cfg=cfg) and cfg.get("server_ip") == server_ip:
      return cfg
  return None


def edge_update_payload(pipeline_names, pipelines):
  first = pipelines[pipeline_names[0]]
  pipeline_jobs = []
  seen_services = set()
  for name in pipeline_names:
    pipeline_cfg = pipelines[name]
    if is_sys_monitor_entry(name=name, cfg=pipeline_cfg):
      continue
    service_name = pipeline_service_name(pipeline_cfg, name=name)
    if service_name in seen_services:
      continue
    seen_services.add(service_name)
    job = _pipeline_edge_item(
      pipeline_cfg,
      name=name,
      extra={
        "service_name": service_name,
        "eg_pipeline_path": pipeline_cfg["eg_pipeline_path"],
      },
    )
    pipeline_jobs.append(job)
  sys_cfg = _sys_monitor_cfg_for_update(pipeline_names, pipelines)
  if sys_cfg:
    sys_monitor_service = pipeline_service_name(sys_cfg) or SYS_MONITOR_SERVICE
    sys_monitor_path = sys_cfg.get("sys_monitor_path", SYS_MONITOR_PATH)
  else:
    sys_monitor_service = SYS_MONITOR_SERVICE
    sys_monitor_path = first.get("sys_monitor_path", SYS_MONITOR_PATH)
  update_payload = {
    "sys_monitor_path": sys_monitor_path,
    "service_names": sorted(seen_services),
    "sys_monitor_service_name": sys_monitor_service,
    "pipelines": pipeline_jobs,
  }
  if pipeline_jobs:
    update_payload["eg_pipeline_path"] = pipeline_jobs[0]["eg_pipeline_path"]
  return json.dumps({"update": update_payload})


def post_edge_command(url, data, *, timeout=30):
  import requests

  response = requests.post(url, data=data, timeout=timeout)
  text = response.content.decode("utf-8", errors="replace")
  try:
    response.raise_for_status()
  except requests.HTTPError as exc:
    raise requests.HTTPError(
      f"{exc.response.status_code} {url}: {text.strip() or exc.response.reason}",
      response=exc.response,
    ) from exc
  return response, text
