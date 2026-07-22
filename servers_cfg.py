"""Shared servers.json loading and edge URL helpers.

Used by proxy/sync/CLI. Edge app.py imports shared constants and status helpers only;
it does not load servers.json (pipeline config arrives in POST /command JSON).
"""

import json
import re
import socket
import urllib.error
import urllib.request
from os.path import dirname, expanduser, join, realpath
from urllib.parse import quote

_EDGE_HOME = expanduser("~")
DEFAULT_EDGE_PORT = 5502
SYS_MONITOR_SERVICE = "eg_monitor"
RTLS_SERVICE = "eg_rtls"
SYS_MONITOR_PATH = join(_EDGE_HOME, "system_monitor")
RTLS_PIPELINE_PATH = join(_EDGE_HOME, "rtls_server")
EG_PIPELINE_PATH = join(_EDGE_HOME, "eg_pipeline")
PLC_PIPELINE_PATH = join(_EDGE_HOME, "sign_monitor")
KAFKA_PIPELINE_PATH = join(_EDGE_HOME, "plc-engine-kafka")
CAMERA_DRIFT_PIPELINE_PATH = join(_EDGE_HOME, "camera_drift")
COBBLE_PIPELINE_PATH = join(_EDGE_HOME, "cobble-pipeline")
DETSEG_PIPELINE_PATH = join(_EDGE_HOME, "detseg_pipeline")
FORKLIFT_PIPELINE_PATH = join(_EDGE_HOME, "forklift_proximity")
DEFAULT_STREAM_FEED_PATH = "/data_feed"
ALT_STREAM_FEED_PATH = "/video_feed"
PLC_STREAM_FEED_PATH = "/stream"
DEFAULT_PLC_STATUS_PORT = 22000
DEFAULT_PLC_STATUS_PATH = "/plc"
DEFAULT_DRIFT_SERVICE_PORT = 8083
DEFAULT_DRIFT_SERVICE_PATH = "/get_drift"
SERVERS_PATH = join(realpath(dirname(__file__)), "servers.json")
SKIP_STREAM_HOSTS = frozenset({"0.0.0.0", "127.0.0.1", "localhost", ""})

DEFAULT_EDGE_PROBE = {
  "stream_probe_interval_sec": 10,
  "stream_probe_workers": 4,
  "docker_stats_interval_sec": 10,
}

DEFAULT_STATUS_DISPLAY = {
  "mem_warn_percent": 80,
}

EDGE_STATUS_KEYS = (
  "cameras_set", "cameras_now", "streaming_port", "streaming_ip",
  "qlight_set", "speaker_set", "qlight_now", "speaker_now",
  "qlight_links", "speaker_links", "camera_links",
  "qlight_status", "speaker_status", "camera_status",
  "plc_tags_now", "plc_tags_set", "plc_tag_status", "plc_tag_links",
  "plc_tag_groups", "plc_tags_ok",
  "drift_status", "drift_cameras_set", "drift_cameras_now",
  "drift_camera_status", "drift_camera_links", "drift_camera_groups",
  "drift_api_ok",
  "stream_health", "status", "mem_usage", "mem_usage_percent",
  "version_current", "version_latest",
  "version_current_date", "version_latest_date",
  "rtls_config_missing", "rtls_devices_missing",
)

# PLC has two kinds: CV (sign_monitor /stream) and Kafka (plc-engine-kafka).
# feed_path None = no camera/SSE stream (Kafka uses /plc tags instead).
_PIPELINE_KINDS = (
  ("rtls", RTLS_PIPELINE_PATH, "RTLS", DEFAULT_STREAM_FEED_PATH),
  ("camera_drift", CAMERA_DRIFT_PIPELINE_PATH, "DRIFT", None),
  ("plc_kafka", KAFKA_PIPELINE_PATH, "PLC", None),
  ("plc_cv", PLC_PIPELINE_PATH, "PLC", PLC_STREAM_FEED_PATH),
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


def _is_camera_drift_name(name):
  lower = (name or "").lower()
  return "camera-drift" in lower or "camera_drift" in lower


def _name_matches_kind(keyword, name, eg_path):
  """True when pipeline name/path should use this kind keyword."""
  lower = (name or "").lower()
  path = (eg_path or "").replace("\\", "/").lower()
  if keyword == "camera_drift":
    return _is_camera_drift_name(name)
  if keyword == "plc_kafka":
    # Camera-Drift / PLC-CV are separate; never treat as PLC-Kafka.
    if _is_camera_drift_name(name):
      return False
    if "cv-plc" in lower or "cv_plc" in lower or "plc-cv" in lower or "plc_cv" in lower:
      return False
    return (
      "kafka" in lower
      or lower.endswith("-plc")
      or lower.endswith("_plc")
      or "plc-engine-kafka" in path
    )
  if keyword == "plc_cv":
    if _is_camera_drift_name(name):
      return False
    if "kafka" in lower or "plc-engine-kafka" in path:
      return False
    if "sign_monitor" in path:
      return True
    return (
      "cv-plc" in lower
      or "cv_plc" in lower
      or "plc-cv" in lower
      or "plc_cv" in lower
    )
  return keyword in lower


def pipeline_kind_for(name=None, cfg=None):
  if is_sys_monitor_entry(name=name, cfg=cfg):
    return "sys", "SYSTEM"
  eg_path = (cfg or {}).get("eg_pipeline_path", "") if cfg else ""
  for keyword, path, label, _feed_path in _PIPELINE_KINDS:
    if _name_matches_kind(keyword, name, eg_path):
      return keyword, label
  for keyword, path, label, _feed_path in _PIPELINE_KINDS:
    if eg_path and path and path in eg_path:
      # Mis-set kafka/sign_monitor paths must not override Camera-Drift.
      if keyword in ("plc_kafka", "plc_cv") and _is_camera_drift_name(name):
        continue
      return keyword, label
  return "eg", "EG"


def stream_feed_paths_for(name=None, cfg=None):
  cfg = cfg or {}
  raw = cfg.get("stream_feed_paths")
  if isinstance(raw, (list, tuple)):
    paths = [path for path in raw if path]
    if len(paths) >= 2:
      return paths[0], paths[1]
    if len(paths) == 1:
      primary = paths[0]
      secondary = (
        ALT_STREAM_FEED_PATH
        if primary == DEFAULT_STREAM_FEED_PATH
        else DEFAULT_STREAM_FEED_PATH
      )
      return primary, secondary
  kind, _ = pipeline_kind_for(name=name, cfg=cfg)
  primary = _FEED_PATH_BY_KIND.get(kind, DEFAULT_STREAM_FEED_PATH)
  if not primary:
    return None, None
  secondary = (
    ALT_STREAM_FEED_PATH
    if primary == DEFAULT_STREAM_FEED_PATH
    else DEFAULT_STREAM_FEED_PATH
  )
  return primary, secondary


def pipeline_path_for(name):
  kind, _ = pipeline_kind_for(name=name)
  return _PATH_BY_KIND.get(kind, EG_PIPELINE_PATH)


def repo_path_for_pipeline(name=None, cfg=None, *, item=None):
  """Resolve git checkout path for a pipeline, status item, or image cfg."""
  if item is not None:
    if item.get("is_sys_monitor"):
      return item.get("sys_monitor_path") or SYS_MONITOR_PATH
    return item.get("eg_pipeline_path")
  if is_sys_monitor_entry(name=name, cfg=cfg):
    return (cfg or {}).get("sys_monitor_path") or SYS_MONITOR_PATH
  if cfg and cfg.get("eg_pipeline_path"):
    return cfg["eg_pipeline_path"]
  if name:
    return pipeline_path_for(name)
  return None


GITHUB_ORG = "everguard-inc"


def repo_name_from_path(repo_path):
  if not repo_path:
    return ""
  return str(repo_path).rstrip("/").split("/")[-1]


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
  return f"https://github.com/{GITHUB_ORG}/{repo_name_from_path(path)}.git"


def _http_service_url(
  cfg,
  *,
  url_key,
  port_key,
  path_key,
  default_port,
  default_path,
  host_ip=None,
  prefer_localhost=False,
  missing="",
):
  """Build http://host:port/path from explicit URL or server_ip defaults."""
  cfg = cfg or {}
  explicit = str(cfg.get(url_key) or "").strip()
  if explicit:
    return explicit.rstrip("/")
  if prefer_localhost:
    ip = "127.0.0.1"
  else:
    ip = host_ip or cfg.get("server_ip") or ""
  if not ip:
    return missing
  try:
    port = int(cfg.get(port_key) or default_port)
  except (TypeError, ValueError):
    port = default_port
  path = str(cfg.get(path_key) or default_path).strip() or default_path
  if not path.startswith("/"):
    path = f"/{path}"
  return f"http://{ip}:{port}{path}".rstrip("/")


def drift_service_url_for(cfg=None, *, host_ip=None, prefer_localhost=False):
  """URL for Camera-Drift API (GET /get_drift)."""
  return _http_service_url(
    cfg,
    url_key="drift_service_url",
    port_key="drift_service_port",
    path_key="drift_service_path",
    default_port=DEFAULT_DRIFT_SERVICE_PORT,
    default_path=DEFAULT_DRIFT_SERVICE_PATH,
    host_ip=host_ip,
    prefer_localhost=prefer_localhost,
    missing="",
  )


def plc_status_url_for(cfg=None, *, host_ip=None, prefer_localhost=False):
  """Base URL for Kafka PLC tag API (GET /plc, /plc/<loc>/<sub>)."""
  return _http_service_url(
    cfg,
    url_key="plc_status_url",
    port_key="plc_status_port",
    path_key="plc_status_path",
    default_port=DEFAULT_PLC_STATUS_PORT,
    default_path=DEFAULT_PLC_STATUS_PATH,
    host_ip=host_ip,
    prefer_localhost=prefer_localhost,
    missing=None,
  )


def _http_get_json(url, timeout):
  req = urllib.request.Request(url, headers={"Accept": "application/json"})
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
      return json.loads(resp.read().decode("utf-8", errors="replace"))
  except socket.timeout as exc:
    # Python 3.8: urllib raises socket.timeout, not TimeoutError.
    raise TimeoutError(str(exc) or "timed out") from exc


_HTTP_JSON_ERRORS = (
  urllib.error.URLError,
  urllib.error.HTTPError,
  TimeoutError,
  socket.timeout,
  ValueError,
  TypeError,
  json.JSONDecodeError,
)


_PLC_TAG_META_KEYS = frozenset({
  "timestamp",
  "everguard_srvtime",
  "stale",
  "stale_after_ms",
  "updated_at_ms",
})


def _plc_tag_entries(payload):
  """Parse /plc leaf JSON into (name, display, health). Only literal error is err."""
  out = []
  if not isinstance(payload, dict):
    return out
  for name, val in payload.items():
    key = str(name)
    key_l = key.lower()
    if key_l in _PLC_TAG_META_KEYS or key_l.endswith(
      ("_nifitime", "_srctime", "_srvtime")
    ):
      continue

    if isinstance(val, bool):
      display = "true" if val else "false"
      health = "ok"
    elif isinstance(val, (int, float)) and not isinstance(val, bool):
      display = str(val)
      health = "ok"
    elif isinstance(val, str):
      text = val.strip()
      low = text.lower()
      if low == "error":
        display = "error"
        health = "err"
      elif low in ("true", "false"):
        display = low
        health = "ok"
      else:
        # Numeric or other reported tag state.
        display = text
        health = "ok"
    else:
      continue
    out.append((key, display, health))
  return out


def empty_camera_drift_metrics():
  return {
    "drift_status": None,
    "drift_cameras_set": None,
    "drift_cameras_now": None,
    "drift_camera_status": [],
    "drift_camera_links": [],
    "drift_camera_groups": [],
    "drift_api_ok": None,
  }


def drift_service_root_url(base_url):
  """Strip a trailing /get_drift path; otherwise return the service root URL."""
  base = str(base_url or "").rstrip("/")
  if base.endswith("/get_drift"):
    return base[: -len("/get_drift")]
  return base


def _drift_camera_short_label(cam):
  name = str(cam.get("display_name") or cam.get("name") or "").strip()
  if name:
    # Prefer trailing IP / short token for compact chips.
    if " - " in name:
      tail = name.rsplit(" - ", 1)[-1].strip()
      if tail:
        # Drop long description after ":" when present.
        return tail.split(":", 1)[0].strip() or tail
    return name[:24]
  cid = str(cam.get("id") or "").strip()
  return cid.split("-", 1)[0] if "-" in cid else (cid[:8] or "?")


def _ipv4_sort_key(text):
  """Numeric IPv4 key from free text; missing IP sorts last."""
  match = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", str(text or ""))
  if not match:
    return (1, (999, 999, 999, 999), str(text or ""))
  try:
    parts = tuple(int(part) for part in match.group(1).split("."))
  except ValueError:
    return (1, (999, 999, 999, 999), str(text or ""))
  if len(parts) != 4 or any(part < 0 or part > 255 for part in parts):
    return (1, (999, 999, 999, 999), str(text or ""))
  return (0, parts, str(text or ""))


def _drift_tag_sort_key(tag):
  """DRIFT first, then numeric IP ascending from the camera label."""
  value = str((tag or {}).get("value") or "").strip().lower()
  drifted = 0 if value == "drift" else 1
  name = str((tag or {}).get("name") or "")
  _found, ip_parts, label = _ipv4_sort_key(name)
  return (drifted, _found, ip_parts, label.lower())


def _drift_area_key(cam):
  """Top area label from API group_path / group_label (RND, LBC, PC2, …)."""
  path = cam.get("group_path")
  if isinstance(path, list):
    parts = [str(p).strip() for p in path if str(p).strip()]
  else:
    parts = []
  if not parts:
    label = str(cam.get("group_label") or "").strip()
    if label:
      parts = [p.strip() for p in label.split("/") if p.strip()]
  return parts[0] if parts else "Other"


def fetch_camera_drift_metrics(base_url, *, timeout=10.0):
  """Probe /get_drift + /api/cameras; group like PLC (area → sub chips)."""
  empty = empty_camera_drift_metrics()
  if not base_url:
    return empty
  root = drift_service_root_url(base_url)
  try:
    drift_payload = _http_get_json(f"{root}/get_drift", timeout)
  except _HTTP_JSON_ERRORS:
    empty["drift_api_ok"] = False
    return empty
  if not isinstance(drift_payload, dict):
    empty["drift_api_ok"] = False
    return empty

  cameras = []
  cameras_ok = False
  try:
    cameras_payload = _http_get_json(f"{root}/api/cameras", timeout)
    if isinstance(cameras_payload, dict):
      raw = cameras_payload.get("cameras") or []
      if isinstance(raw, list):
        cameras = [c for c in raw if isinstance(c, dict)]
        cameras_ok = True
  except _HTTP_JSON_ERRORS:
    cameras = []

  # /api/cameras timed out or failed: do not publish a fake 0-camera OK.
  if not cameras_ok:
    empty["drift_status"] = str(drift_payload.get("camera_status") or "").strip().upper() or None
    empty["drift_api_ok"] = None
    return empty

  abnormal = drift_payload.get("abnormal_cameras") or []
  if not isinstance(abnormal, list):
    abnormal = []
  drifted_ids = {
    str(cid).strip() for cid in abnormal if str(cid).strip()
  }
  # Prefer live isDrift flags when /api/cameras is available.
  for cam in cameras:
    cid = str(cam.get("id") or "").strip()
    if cid and cam.get("isDrift"):
      drifted_ids.add(cid)

  drift_status = str(drift_payload.get("camera_status") or "").strip().upper()
  # One Drift block; chips are top areas (RND, LBC, PC2, …).
  preferred_chip_order = ("RND", "LBC", "PC2")
  areas = {}
  flat_links = []
  flat_statuses = []

  for cam in cameras:
    cid = str(cam.get("id") or "").strip()
    if not cid:
      continue
    top = _drift_area_key(cam)
    is_drift = cid in drifted_ids or bool(cam.get("isDrift"))
    name = str(cam.get("display_name") or cam.get("name") or cid).strip()
    short = _drift_camera_short_label(cam)
    tag = {
      "name": name,
      "value": "DRIFT" if is_drift else "OK",
      "health": "err" if is_drift else "ok",
      "id": cid,
    }
    area = areas.setdefault(top, {"now": 0, "set": 0, "tags": []})
    area["set"] += 1
    if is_drift:
      area["now"] += 1
    area["tags"].append(tag)
    flat_links.append({
      "label": short,
      "title": f"{'Drifted' if is_drift else 'OK'}: {name}",
      "value": cid,
      "id": cid,
      "url": root,
    })
    flat_statuses.append("err" if is_drift else "ok")

  chip_status = []
  chip_links = []
  ordered_areas = [name for name in preferred_chip_order if name in areas]
  ordered_areas.extend(sorted(name for name in areas if name not in preferred_chip_order))
  for area_name in ordered_areas:
    area = areas[area_name]
    area["tags"].sort(key=_drift_tag_sort_key)
    area_now = area["now"]
    area_set = area["set"]
    if area_now <= 0:
      health = "ok"
    elif area_now >= area_set:
      health = "err"
    else:
      health = "warn"
    chip_status.append(health)
    chip_links.append({
      "label": area_name,
      "title": f"{area_name}: {area_now} drifted / {area_set} cameras",
      "url": f"{root}/get_drift",
      "value": area_name,
      "location": "Drift",
      "tags": area["tags"],
    })

  groups = []
  if chip_links:
    groups.append({
      "label": "Drift",
      "now": sum(area["now"] for area in areas.values()),
      "set": sum(area["set"] for area in areas.values()),
      "chip_status": chip_status,
      "links": chip_links,
    })

  # Fallback when /api/cameras is empty: flat drifted IDs only.
  if not cameras:
    for cid in sorted(drifted_ids):
      short_id = cid.split("-", 1)[0] if "-" in cid else cid[:8]
      flat_links.append({
        "label": short_id,
        "title": f"Drifted camera: {cid}",
        "value": cid,
        "id": cid,
        "url": root,
      })
      flat_statuses.append("err")

  drift_now = sum(1 for status in flat_statuses if status == "err")
  if cameras:
    drift_now = sum(
      1 for cam in cameras if str(cam.get("id") or "").strip() in drifted_ids
    )
  drift_set = len(cameras) if cameras else len(flat_links)
  if not drift_status:
    drift_status = "OK" if drift_now == 0 else "WARN"
  return {
    "drift_status": drift_status,
    "drift_cameras_set": drift_set,
    "drift_cameras_now": drift_now,
    "drift_camera_status": flat_statuses,
    "drift_camera_links": flat_links,
    "drift_camera_groups": groups,
    "drift_api_ok": True,
  }


def empty_plc_tag_metrics():
  return {
    "plc_tags_now": None,
    "plc_tags_set": None,
    "plc_tag_status": [],
    "plc_tag_links": [],
    "plc_tag_groups": [],
    "plc_tags_ok": None,
  }


def fetch_plc_tag_metrics(base_url, *, timeout=5.0):
  """Probe /plc and group chips by location/sub (ok / warn / err)."""
  empty = empty_plc_tag_metrics()
  if not base_url:
    return empty
  base = str(base_url).rstrip("/")
  try:
    root = _http_get_json(base, timeout)
  except _HTTP_JSON_ERRORS:
    return empty
  if not isinstance(root, dict):
    return empty

  healthy = 0
  total = 0
  groups = []
  flat_statuses = []
  flat_links = []

  for loc, subs in root.items():
    if not isinstance(subs, dict):
      continue
    loc_name = str(loc)
    loc_healthy = 0
    loc_total = 0
    chip_status = []
    chip_links = []
    for sub in subs:
      sub_name = str(sub)
      leaf_url = f"{base}/{quote(loc_name, safe='')}/{quote(sub_name, safe='')}"
      try:
        leaf = _http_get_json(leaf_url, timeout)
      except _HTTP_JSON_ERRORS:
        continue
      entries = _plc_tag_entries(leaf)
      if not entries:
        continue
      sub_total = len(entries)
      sub_errors = sum(1 for _n, _d, health in entries if health == "err")
      sub_healthy = sub_total - sub_errors
      true_n = sum(1 for _n, display, _h in entries if display == "true")
      false_n = sum(1 for _n, display, _h in entries if display == "false")
      other_n = sub_total - true_n - false_n - sub_errors

      loc_total += sub_total
      loc_healthy += sub_healthy
      total += sub_total
      healthy += sub_healthy

      if sub_errors <= 0:
        health = "ok"
      elif sub_errors >= sub_total:
        health = "err"
      else:
        health = "warn"
      detail_parts = [f"{true_n} true", f"{false_n} false"]
      if other_n:
        detail_parts.append(f"{other_n} other")
      if sub_errors:
        detail_parts.append(f"{sub_errors} error")
      tag_preview = ", ".join(
        f"{name}={display}" for name, display, _h in entries[:8]
      )
      if sub_total > 8:
        tag_preview += ", …"
      title = f"{loc_name}/{sub_name}: {', '.join(detail_parts)}"
      if tag_preview:
        title = f"{title} — {tag_preview}"

      chip_status.append(health)
      meta = {}
      if isinstance(leaf, dict):
        if "stale" in leaf:
          # API field is boolean `stale`; expose as status for the UI.
          meta["status"] = "WARN" if bool(leaf.get("stale")) else "OK"
        if leaf.get("updated_at_ms") is not None:
          try:
            meta["updated_at_ms"] = int(leaf["updated_at_ms"])
          except (TypeError, ValueError):
            pass
      chip_links.append({
        "label": sub_name,
        "title": title,
        "url": leaf_url,
        "value": sub_name,
        "location": loc_name,
        "meta": meta,
        "tags": [
          {"name": name, "value": display, "health": tag_health}
          for name, display, tag_health in entries
        ],
      })
      flat_statuses.append(health)
      flat_links.append(chip_links[-1])

    if loc_total <= 0:
      continue
    groups.append({
      "label": loc_name,
      "now": loc_healthy,
      "set": loc_total,
      "chip_status": chip_status,
      "links": chip_links,
      "ok": loc_healthy == loc_total,
    })

  if total <= 0:
    return empty
  return {
    "plc_tags_now": healthy,
    "plc_tags_set": total,
    "plc_tag_status": flat_statuses,
    "plc_tag_links": flat_links,
    "plc_tag_groups": groups,
    "plc_tags_ok": healthy == total,
  }


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
    "is_plc_cv": kind == "plc_cv",
    "is_kafka": kind == "plc_kafka",
    "is_camera_drift": kind == "camera_drift",
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
  elif is_kafka_pipeline(cfg=pipeline_cfg, name=name):
    item["is_kafka"] = True
  elif is_camera_drift_pipeline(cfg=pipeline_cfg, name=name):
    item["is_camera_drift"] = True
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


def is_kafka_pipeline(cfg=None, name=None):
  return pipeline_kind_for(name=name, cfg=cfg)[0] == "plc_kafka"


def is_camera_drift_pipeline(cfg=None, name=None):
  return pipeline_kind_for(name=name, cfg=cfg)[0] == "camera_drift"


def is_sys_monitored_peer(name=None, cfg=None, *, item=None):
  """True if this pipeline is counted under a SYS monitor host (excludes SYS)."""
  if item is not None:
    return not item.get("is_sys_monitor")
  if is_sys_monitor_entry(name=name, cfg=cfg):
    return False
  return True


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
  # SYS and co-located RTLS (often no monitor_host_ip) key off server_ip.
  if is_sys_monitor_entry(name=name, cfg=pipeline_cfg) or is_rtls_pipeline(
    name=name, cfg=pipeline_cfg,
  ):
    server_ip = pipeline_cfg.get("server_ip")
    return server_ip if is_usable_edge_host(server_ip) else None
  return None


def monitored_pipeline_names(pipelines_cfg, monitor_host_ip):
  """Pipelines counted under this SYS host (excludes SYS; includes RTLS/Kafka)."""
  if not monitor_host_ip:
    return []
  names = []
  for name, pipeline_cfg in pipelines_cfg.items():
    if not is_sys_monitored_peer(name=name, cfg=pipeline_cfg):
      continue
    if pipeline_monitor_host(name, pipeline_cfg) == monitor_host_ip:
      names.append(name)
  return names


def apply_sys_monitor_peer_status(status_map, sys_name, monitored_names):
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
    apply_sys_monitor_peer_status(status_map, name, monitored)
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

  # Camera-Drift: systemd running + /get_drift health.
  if entry.get("is_camera_drift"):
    if not entry.get("running"):
      entry["status"] = "ERR"
      return entry
    if entry.get("drift_api_ok") is False or entry.get("drift_cameras_set") is None:
      entry["status"] = "WARN"
      return entry
    drift_count = entry.get("drift_cameras_now")
    if drift_count is None:
      drift_count = 0
    drift_status = str(entry.get("drift_status") or "").upper()
    if drift_count > 0 or drift_status not in ("", "OK"):
      entry["status"] = "WARN"
    else:
      entry["status"] = "OK"
    return entry

  # Kafka consumer: systemd running + PLC tag API health.
  if entry.get("is_kafka"):
    if not entry.get("running"):
      entry["status"] = "ERR"
      return entry
    if entry.get("plc_tags_set") is None:
      entry["status"] = "WARN"
      return entry
    entry["status"] = (
      "OK" if device_counts_ok(entry.get("plc_tags_set"), entry.get("plc_tags_now"))
      else "WARN"
    )
    return entry

  if entry.get("running") and entry.get("stream_health"):
    cameras_ok = device_counts_ok(entry.get("cameras_set"), entry.get("cameras_now"))
    entry["status"] = "OK" if cameras_ok else "WARN"
  elif entry.get("running"):
    # Stream-less services (no cameras_set): running alone is OK.
    if entry.get("cameras_set") is None:
      entry["status"] = "OK"
    else:
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


def load_pipelines(path=None):
  _, pipelines = split_servers_raw(read_servers_file(path))
  return {name: cfg for name, cfg in pipelines.items() if is_pipeline_entry(cfg)}


def load_servers_cfg(path=None):
  return load_pipelines(path)


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


def edge_host_command_payload(command, pipeline_names, pipelines, *, git_refs=None):
  if command in ("stream", "watchdog"):
    return edge_run_payload(command, pipeline_names, pipelines)
  if command == "update":
    return edge_update_payload(pipeline_names, pipelines, git_refs=git_refs)
  raise ValueError(f"unsupported host command: {command!r}")


def edge_host_power_payload(action):
  if action not in ("reboot", "shutdown"):
    raise ValueError(f"unsupported host power action: {action!r}")
  return json.dumps({"host_power": action})


def server_cfg_for_ip(server_ip, pipelines):
  """Return any pipeline cfg that uses this edge server_ip."""
  for pipeline_cfg in pipelines.values():
    if isinstance(pipeline_cfg, dict) and pipeline_cfg.get("server_ip") == server_ip:
      return pipeline_cfg
  return None


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


def normalize_git_refs(git_refs):
  """Normalize {repo_name: ref} map; empty refs are dropped."""
  if not isinstance(git_refs, dict):
    return {}
  cleaned = {}
  for key, value in git_refs.items():
    name = str(key or "").strip()
    ref = str(value or "").strip()
    if name and ref:
      cleaned[name] = ref
  return cleaned


def _resolve_update_sys_monitor(pipeline_names, pipelines):
  """Return (path, service) for system_monitor update, or (None, None) to skip."""
  sys_cfg = _sys_monitor_cfg_for_update(pipeline_names, pipelines)
  if sys_cfg:
    path = str(sys_cfg.get("sys_monitor_path") or SYS_MONITOR_PATH).strip()
    service = pipeline_service_name(sys_cfg) or SYS_MONITOR_SERVICE
    return path or None, service if path else None

  for name in pipeline_names:
    cfg = pipelines.get(name) or {}
    if is_sys_monitor_entry(name=name, cfg=cfg):
      continue
    path = str(cfg.get("sys_monitor_path") or "").strip()
    if path:
      return path, SYS_MONITOR_SERVICE
  return None, None


def edge_update_payload(pipeline_names, pipelines, *, git_refs=None):
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
  sys_monitor_path, sys_monitor_service = _resolve_update_sys_monitor(
    pipeline_names, pipelines,
  )
  update_payload = {
    "service_names": sorted(seen_services),
    "pipelines": pipeline_jobs,
  }
  if sys_monitor_path:
    update_payload["sys_monitor_path"] = sys_monitor_path
    update_payload["sys_monitor_service_name"] = sys_monitor_service or SYS_MONITOR_SERVICE
  if pipeline_jobs:
    update_payload["eg_pipeline_path"] = pipeline_jobs[0]["eg_pipeline_path"]
  cleaned_refs = normalize_git_refs(git_refs)
  if cleaned_refs:
    update_payload["git_refs"] = cleaned_refs
  return json.dumps({"update": update_payload})


def post_edge_command(url, data, *, timeout=30):
  import requests

  headers = {"Content-Type": "application/json"}
  response = requests.post(url, data=data, headers=headers, timeout=timeout)
  text = response.content.decode("utf-8", errors="replace")
  try:
    response.raise_for_status()
  except requests.HTTPError as exc:
    raise requests.HTTPError(
      f"{exc.response.status_code} {url}: {text.strip() or exc.response.reason}",
      response=exc.response,
    ) from exc
  return response, text


def tcp_reachable(host, port, *, timeout=3.0):
  """Return True/False for TCP probe; False when host or port is missing."""
  if not host or not port:
    return False
  try:
    with socket.create_connection((host, port), timeout=timeout):
      return True
  except OSError:
    return False


def merge_edge_status_fields(entry, data):
  if not isinstance(data, dict):
    return entry
  for key in EDGE_STATUS_KEYS:
    if key in data and data[key] is not None:
      entry[key] = data[key]
  if "running" in data:
    entry["running"] = bool(data["running"])
  return entry


def edge_check_payload(server, *, name=None):
  pipeline_name = name or server.get("pipeline") or ""
  return json.dumps({
    "check": {
      "server_id": server["server_id"],
      "service_name": pipeline_service_name(server, name=pipeline_name),
      **pipeline_kind_flags(name=pipeline_name, cfg=server),
    },
  })
