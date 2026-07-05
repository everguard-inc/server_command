"""Resolve server IPs from S3 cfg ({server_id}.json) or local sys files."""

import json
import os
from urllib.parse import urlparse

from eg_basics.utils import read_include_json, s3download_cfg, s3path_frim_id

from servers_cfg import is_sys_monitor_entry, is_usable_stream_host

EDGE_HOME = os.path.expanduser("~")


def _needs_include_merge(cfg):
  if not cfg.get("include"):
    return False
  if cfg.get("streaming_port") is None and not cfg.get("edge_status_ip"):
    return True
  monitor = cfg.get("monitor")
  if not isinstance(monitor, dict):
    return True
  return not monitor.get("system_monitor_url")


def _download_cfg(url):
  cfg = s3download_cfg(url, return_ordered=False, verbose=False)
  if not cfg:
    return None
  if _needs_include_merge(cfg):
    cfg = read_include_json(cfg, root_path="")
  return cfg


def load_server_cfg(xsite_id, server_id):
  """Load cfg from S3 s3://.../{xSiteId}/{server_id}.json."""
  url = s3path_frim_id(xsite_id, server_id, is_sys=False, return_url=True)
  return _download_cfg(url)


def load_pipeline_cfg(xsite_id, name, server_id):
  """S3 cfg for pipelines; SYS falls back to local *_sys.json."""
  if is_sys_monitor_entry(name=name):
    return load_server_cfg(xsite_id, server_id) or load_local_sys_cfg(server_id)
  return load_server_cfg(xsite_id, server_id)


def _unique_paths(paths):
  seen = set()
  for path in paths:
    if path and path not in seen:
      seen.add(path)
      yield path


def _container_dir_candidates():
  dirs = []
  env_dir = os.environ.get("CONTAINER_DIR")
  if env_dir:
    dirs.append(env_dir)
  dirs.extend([os.path.join(EDGE_HOME, "containers"), "/containers"])
  return list(_unique_paths(dirs))


def _read_json_file(path):
  try:
    with open(path, encoding="utf-8") as handle:
      data = json.load(handle)
    return data if isinstance(data, dict) else None
  except (OSError, json.JSONDecodeError, TypeError, ValueError):
    return None


def _local_sys_cfg_paths(server_id):
  paths = []
  for base in _container_dir_candidates():
    paths.append(os.path.join(base, server_id, f"{server_id}_sys.json"))
    if not os.path.isdir(base):
      continue
    try:
      for entry in os.listdir(base):
        if entry.endswith("_sys.json"):
          paths.append(os.path.join(base, entry))
        else:
          paths.append(os.path.join(base, entry, f"{entry}_sys.json"))
    except OSError:
      continue

  sys_monitor_dir = os.path.join(EDGE_HOME, "system_monitor")
  if os.path.isdir(sys_monitor_dir):
    for name in os.listdir(sys_monitor_dir):
      if name.endswith("_sys.json"):
        paths.append(os.path.join(sys_monitor_dir, name))

  return list(_unique_paths(paths))


def load_local_sys_cfg(server_id):
  """Fallback when S3 cfg is missing (edge host local *_sys.json)."""
  if not server_id:
    return None

  for path in _local_sys_cfg_paths(server_id):
    if not os.path.isfile(path):
      continue
    cfg = _read_json_file(path)
    if not cfg:
      continue
    if cfg.get("server_id") == server_id or path.endswith(f"{server_id}_sys.json"):
      return cfg
  return None


def _ip_from_cfg(cfg, key):
  if not cfg:
    return None
  ip = cfg.get(key)
  return ip if is_usable_stream_host(ip) else None


def streaming_ip_from_cfg(cfg):
  return _ip_from_cfg(cfg, "streaming_ip")


def edge_status_ip_from_cfg(cfg):
  return _ip_from_cfg(cfg, "edge_status_ip")


def _host_from_url(url):
  if not url:
    return None
  parsed = urlparse(url if "://" in url else f"http://{url}")
  host = parsed.hostname
  return host if is_usable_stream_host(host) else None


def system_monitor_host_from_cfg(cfg):
  """Host/IP from monitor.system_monitor_url in a pipeline cfg."""
  if not cfg:
    return None
  monitor = cfg.get("monitor")
  if not isinstance(monitor, dict):
    return None
  return _host_from_url(monitor.get("system_monitor_url"))


def _load_resolve_cfg(xsite_id, server_id, *, local_sys_fallback=False):
  cfg = load_server_cfg(xsite_id, server_id)
  if local_sys_fallback and not cfg:
    cfg = load_local_sys_cfg(server_id)
  return cfg


# --- resolve ---

def resolve_server_ip(xsite_id, server_id):
  return streaming_ip_from_cfg(_load_resolve_cfg(xsite_id, server_id))


def resolve_sys_monitor_ip(xsite_id, server_id):
  return edge_status_ip_from_cfg(
    _load_resolve_cfg(xsite_id, server_id, local_sys_fallback=True),
  )
