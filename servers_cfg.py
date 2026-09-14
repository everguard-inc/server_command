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
DEFAULT_PLC_CV_CHECKERS_PATH = "/checkers"
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
  "plc_cv_checkers",
  "drift_status", "drift_cameras_set", "drift_cameras_now",
  "drift_cameras_online",
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


def plc_cv_checkers_url_for(
  cfg=None,
  *,
  host_ip=None,
  streaming_port=None,
  prefer_localhost=False,
):
  """URL for PLC-CV sign_monitor checker chips (GET /checkers)."""
  cfg = cfg or {}
  explicit = str(cfg.get("plc_cv_checkers_url") or "").strip()
  if explicit:
    return explicit.rstrip("/")
  port = streaming_port if streaming_port is not None else cfg.get("streaming_port")
  try:
    port = int(port)
  except (TypeError, ValueError):
    return None
  if prefer_localhost:
    ip = "127.0.0.1"
  else:
    ip = host_ip or cfg.get("streaming_ip") or cfg.get("server_ip") or ""
  if not ip:
    return None
  path = str(cfg.get("plc_cv_checkers_path") or DEFAULT_PLC_CV_CHECKERS_PATH).strip()
  path = path or DEFAULT_PLC_CV_CHECKERS_PATH
  if not path.startswith("/"):
    path = f"/{path}"
  return f"http://{ip}:{port}{path}".rstrip("/")


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


def _plc_cv_first_token(text):
  parts = str(text or "").strip().split()
  return parts[0] if parts else ""


def _plc_cv_checker_chip_health(raw):
  """Map /checkers tag value to list chip tone (error string → err)."""
  text = str(raw or "").strip().lower()
  if text == "error":
    return "err"
  if text == "ok":
    return "ok"
  if text in ("warn", "warning"):
    return "warn"
  return "unknown"


def _plc_cv_chip_label(name, signs=None):
  """Chip label = first token of checker name, else first sign name.

  \"Cam111 Green2\" / \"Cam111\" → Cam111. Generic checker-N uses first sign.
  """
  text = str(name or "").strip()
  if text and not re.fullmatch(r"checker-\d+", text, re.I):
    return _plc_cv_first_token(text) or "Sign"
  for sign in signs or []:
    if not isinstance(sign, dict):
      continue
    token = _plc_cv_first_token(sign.get("name"))
    if token:
      return token
  return _plc_cv_first_token(text) or "Sign"


def fetch_plc_cv_checker_metrics(checkers_url, *, timeout=5.0):
  """Probe sign_monitor GET /checkers → Signs chips (camera_* fields)."""
  empty = {"plc_cv_checkers": False}
  if not checkers_url:
    return empty
  try:
    payload = _http_get_json(str(checkers_url).rstrip("/"), timeout)
  except _HTTP_JSON_ERRORS:
    return empty
  if not isinstance(payload, dict):
    return empty

  tags = payload.get("tags") if isinstance(payload.get("tags"), dict) else {}
  checkers = [c for c in (payload.get("checkers") or []) if isinstance(c, dict)]
  if not checkers and tags:
    checkers = [
      {"name": key, "status": value, "signs": []}
      for key, value in tags.items()
    ]

  statuses = []
  links = []
  ok_n = 0
  for checker in checkers:
    name = str(checker.get("name") or checker.get("id") or "").strip()
    if not name:
      continue
    checker_id = str(checker.get("id") or "")
    raw_status = tags.get(name, tags.get(checker_id, checker.get("status")))
    health = _plc_cv_checker_chip_health(raw_status)
    if health == "ok":
      ok_n += 1
    statuses.append(health)

    signs = [s for s in (checker.get("signs") or []) if isinstance(s, dict)]
    sign_ids = [str(s["id"]) for s in signs if s.get("id")]
    chip_label = _plc_cv_chip_label(name, signs)
    display = str(raw_status or "").strip().lower() or health
    links.append({
      "label": chip_label,
      "title": f"{name}: {display}",
      "value": chip_label,
      "sign_ids": sign_ids,
    })

  return {
    "plc_cv_checkers": True,
    "cameras_set": len(links),
    "cameras_now": ok_n,
    "camera_status": statuses,
    "camera_links": links,
  }


def stream_camera_snapshot(payload):
  """Extract lightweight camera presence from an eg_pipeline SSE payload.

  Avoids retaining jpeg blobs. Returns None when payload is not a dict.
  """
  if not isinstance(payload, dict):
    return None

  camera_ids = payload.get("camera_ids")
  if not isinstance(camera_ids, list):
    camera_ids = None

  index_map = payload.get("index_map")
  if not isinstance(index_map, list):
    index_map = None

  jpeg = payload.get("jpeg")
  jpeg_present = None
  cameras_now = None
  if camera_ids is not None:
    cameras_now = len(camera_ids)
  elif isinstance(jpeg, list):
    jpeg_present = [bool(frame) for frame in jpeg]
    cameras_now = sum(1 for present in jpeg_present if present)
  elif isinstance(jpeg, str) and jpeg:
    jpeg_present = [True]
    cameras_now = 1
  else:
    states = payload.get("states")
    if isinstance(states, dict) and states:
      cams = set()
      for value in states.values():
        label = ""
        if isinstance(value, (list, tuple)) and value:
          label = str(value[0] or "")
        elif isinstance(value, str):
          label = value
        match = re.match(r"Cam\s*(\d+)", label, re.I)
        if match:
          cams.add(match.group(1))
      if cams:
        cameras_now = len(cams)

  config_cameras = (payload.get("config") or {}).get("camera")
  config_uids = None
  if isinstance(config_cameras, list):
    config_uids = [
      (cam.get("uid") if isinstance(cam, dict) else None)
      for cam in config_cameras
    ]

  return {
    "cameras_now": cameras_now,
    "camera_ids": camera_ids,
    "index_map": index_map,
    "jpeg_present": jpeg_present,
    "config_uids": config_uids,
  }


def camera_statuses_from_stream(snapshot, camera_links):
  """Map stream snapshot to per-config-camera ok/err list, or None if unmappable."""
  if not snapshot or not camera_links:
    return None

  n = len(camera_links)
  camera_ids = snapshot.get("camera_ids")
  config_uids = snapshot.get("config_uids")

  uids = []
  for idx, link in enumerate(camera_links):
    uid = None
    if isinstance(link, dict):
      uid = link.get("uid")
    if not uid and isinstance(config_uids, list) and idx < len(config_uids):
      uid = config_uids[idx]
    uids.append(uid)

  if isinstance(camera_ids, list) and uids and all(uids):
    id_set = set(camera_ids)
    return ["ok" if uid in id_set else "err" for uid in uids]

  index_map = snapshot.get("index_map")
  if isinstance(index_map, list):
    active = {idx for idx in index_map if isinstance(idx, int)}
    return ["ok" if idx in active else "err" for idx in range(n)]

  jpeg_present = snapshot.get("jpeg_present")
  if isinstance(jpeg_present, list) and len(jpeg_present) == n:
    return ["ok" if present else "err" for present in jpeg_present]

  return None


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
    "drift_cameras_online": None,
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
  """DRIFT first, then HOLD (보류), otherwise numeric IP ascending."""
  value = str((tag or {}).get("value") or "").strip().lower()
  if value == "drift":
    rank = 0
  elif value in ("hold", "deferred", "pending", "보류"):
    rank = 1
  else:
    rank = 2
  name = str((tag or {}).get("name") or "")
  found, ip_parts, label = _ipv4_sort_key(name)
  return (rank, found, ip_parts, label.lower())


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


def _is_camera_online(cam):
  return str((cam or {}).get("status") or "").strip().lower() == "online"


def _drift_tag_fields(is_drift, is_online, eval_status=None):
  """Return (value, health, title_state, area_chip_state).

  DRIFT → red (err). HOLD/deferred/pending (보류) → yellow (warn).
  OFF never warns chips.
  """
  if is_drift:
    return "DRIFT", "err", "Drifted", "err"
  status = str(eval_status or "").strip().lower()
  if status in ("deferred", "pending", "hold"):
    return "HOLD", "warn", "Hold", "warn"
  if not is_online:
    return "OFF", "idle", "Offline", "ok"
  return "OK", "ok", "OK", "ok"


def _drift_area_chip_health(drifted, held, total):
  """Any drift → red; else any hold (보류) → yellow; else ok."""
  if drifted > 0:
    return "err"
  if held > 0:
    return "warn"
  return "ok"


def _drift_eval_is_hold(eval_status):
  return str(eval_status or "").strip().lower() in (
    "deferred", "pending", "hold",
  )


def fetch_camera_drift_metrics(base_url, *, timeout=10.0):
  """Probe /get_drift + /api/cameras; one TOTAL block with area chips."""
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

  # Incomplete cameras list: avoid publishing a fake 0-camera OK.
  if not cameras_ok:
    empty["drift_status"] = (
      str(drift_payload.get("camera_status") or "").strip().upper() or None
    )
    empty["drift_api_ok"] = None
    return empty

  abnormal = drift_payload.get("abnormal_cameras") or []
  if not isinstance(abnormal, list):
    abnormal = []
  drifted_ids = {str(cid).strip() for cid in abnormal if str(cid).strip()}
  for cam in cameras:
    cid = str(cam.get("id") or "").strip()
    if cid and cam.get("isDrift"):
      drifted_ids.add(cid)

  preferred_chip_order = ("RND", "LBC", "PC2")
  areas = {}
  flat_links = []
  flat_statuses = []

  for cam in cameras:
    cid = str(cam.get("id") or "").strip()
    if not cid:
      continue
    is_drift = cid in drifted_ids or bool(cam.get("isDrift"))
    is_online = _is_camera_online(cam)
    eval_status = cam.get("eval_status")
    is_hold = (not is_drift) and _drift_eval_is_hold(eval_status)
    name = str(cam.get("display_name") or cam.get("name") or cid).strip()
    tag_value, tag_health, title_state, chip_state = _drift_tag_fields(
      is_drift, is_online, eval_status,
    )
    tag = {
      "name": name,
      "value": tag_value,
      "health": tag_health,
      "id": cid,
    }
    top = _drift_area_key(cam)
    area = areas.setdefault(
      top, {"now": 0, "hold": 0, "set": 0, "tags": []},
    )
    area["set"] += 1
    if is_drift:
      area["now"] += 1
    elif is_hold:
      area["hold"] += 1
    area["tags"].append(tag)
    flat_links.append({
      "label": _drift_camera_short_label(cam),
      "title": f"{title_state}: {name}",
      "value": cid,
      "id": cid,
      "url": root,
    })
    flat_statuses.append(chip_state)

  # Empty /api/cameras: surface drifted IDs from /get_drift only.
  if not cameras:
    for cid in sorted(drifted_ids):
      flat_links.append({
        "label": _drift_camera_short_label({"id": cid}),
        "title": f"Drifted camera: {cid}",
        "value": cid,
        "id": cid,
        "url": root,
      })
      flat_statuses.append("err")

  chip_status = []
  chip_links = []
  ordered_areas = [name for name in preferred_chip_order if name in areas]
  ordered_areas.extend(
    sorted(name for name in areas if name not in preferred_chip_order)
  )
  for area_name in ordered_areas:
    area = areas[area_name]
    area["tags"].sort(key=_drift_tag_sort_key)
    area_now = area["now"]
    area_hold = area["hold"]
    area_set = area["set"]
    chip_status.append(
      _drift_area_chip_health(area_now, area_hold, area_set),
    )
    chip_links.append({
      "label": area_name,
      "title": (
        f"{area_name}: {area_now} drifted"
        f" / {area_hold} hold / {area_set} cameras"
      ),
      "url": f"{root}/get_drift",
      "value": area_name,
      "location": "Drift",
      "tags": area["tags"],
    })

  if cameras:
    drift_set = len(cameras)
    drift_now = sum(
      1 for cam in cameras if str(cam.get("id") or "").strip() in drifted_ids
    )
    drift_online = sum(1 for cam in cameras if _is_camera_online(cam))
  else:
    drift_set = len(flat_links)
    drift_now = sum(1 for status in flat_statuses if status == "err")
    drift_online = 0

  groups = []
  if chip_links:
    groups.append({
      "label": "Drift",
      "now": drift_now,
      "set": drift_set,
      "online": drift_online,
      "chip_status": chip_status,
      "links": chip_links,
    })

  drift_status = str(drift_payload.get("camera_status") or "").strip().upper()
  if not drift_status:
    drift_status = "OK" if drift_now == 0 else "WARN"
  return {
    "drift_status": drift_status,
    "drift_cameras_set": drift_set,
    "drift_cameras_now": drift_now,
    "drift_cameras_online": drift_online,
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


def _sync_plc_chip_status_from_meta(entry):
  """Align list chip_status with each link's meta.status (stale → WARN)."""
  if not isinstance(entry, dict):
    return entry
  groups = entry.get("plc_tag_groups")
  if not isinstance(groups, list):
    return entry
  flat = []
  for group in groups:
    if not isinstance(group, dict):
      continue
    links = group.get("links") or []
    statuses = list(group.get("chip_status") or [])
    while len(statuses) < len(links):
      statuses.append("ok")
    for idx, link in enumerate(links):
      if not isinstance(link, dict):
        continue
      meta = link.get("meta") or {}
      meta_status = str(meta.get("status") or "").strip().upper()
      if meta_status in ("ERROR", "ERR"):
        statuses[idx] = "err"
      elif meta_status == "WARN" and statuses[idx] == "ok":
        statuses[idx] = "warn"
      flat.append(statuses[idx])
    group["chip_status"] = statuses[:len(links)] if links else statuses
  if flat:
    entry["plc_tag_status"] = flat
  return entry


def _plc_leaf_url(base_url, loc_name, sub_name):
  base = str(base_url or "").rstrip("/")
  if not base:
    return ""
  return f"{base}/{quote(str(loc_name), safe='')}/{quote(str(sub_name), safe='')}"


def rewrite_plc_tag_link_urls(entry, public_base_url):
  """Point Open API / chip links at a browser-reachable host (not 127.0.0.1)."""
  if not isinstance(entry, dict):
    return entry
  base = str(public_base_url or "").rstrip("/")
  if not base:
    return entry

  def _rewrite_link(link):
    if not isinstance(link, dict):
      return
    loc = link.get("location")
    sub = link.get("value") if link.get("value") is not None else link.get("label")
    if loc is None or sub is None:
      return
    link["url"] = _plc_leaf_url(base, loc, sub)

  for link in entry.get("plc_tag_links") or []:
    _rewrite_link(link)
  for group in entry.get("plc_tag_groups") or []:
    if not isinstance(group, dict):
      continue
    for link in group.get("links") or []:
      _rewrite_link(link)
  return entry


def fetch_plc_tag_metrics(base_url, *, timeout=5.0, link_base_url=None):
  """Probe /plc and group chips by location/sub (ok / warn / err).

  base_url: used for HTTP probes (may be http://127.0.0.1:…).
  link_base_url: optional browser-facing base for chip / Open API URLs.
  """
  empty = empty_plc_tag_metrics()
  if not base_url:
    return empty
  base = str(base_url).rstrip("/")
  link_base = str(link_base_url or base).rstrip("/") or base
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
      probe_url = _plc_leaf_url(base, loc_name, sub_name)
      try:
        leaf = _http_get_json(probe_url, timeout)
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

      meta = {}
      if isinstance(leaf, dict):
        if "stale" in leaf:
          # API field is boolean `stale`; expose as status for the UI.
          meta["status"] = "WARN" if bool(leaf.get("stale")) else "OK"
        raw_status = str(leaf.get("status") or "").strip().upper()
        if raw_status in ("OK", "WARN", "ERROR", "ERR"):
          meta["status"] = "ERROR" if raw_status == "ERR" else raw_status
        if leaf.get("updated_at_ms") is not None:
          try:
            meta["updated_at_ms"] = int(leaf["updated_at_ms"])
          except (TypeError, ValueError):
            pass
      # List chip (EAF4, …) must match modal Status — not tag true/false alone.
      meta_status = str(meta.get("status") or "").upper()
      if meta_status in ("ERROR", "ERR"):
        health = "err"
      elif meta_status == "WARN" and health == "ok":
        health = "warn"
      chip_status.append(health)
      chip_links.append({
        "label": sub_name,
        "title": title,
        "url": _plc_leaf_url(link_base, loc_name, sub_name),
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
    if entry.get("drift_api_ok") is False:
      entry["status"] = "WARN"
      return entry
    # Incomplete metrics: keep PENDING (avoid chip-less false WARN).
    if entry.get("drift_cameras_set") is None:
      if entry.get("status") not in ("OK", "WARN", "ERR"):
        entry["status"] = "PENDING"
      return entry
    drift_count = entry.get("drift_cameras_now") or 0
    drift_status = str(entry.get("drift_status") or "").upper()
    hold_count = sum(
      1 for status in (entry.get("drift_camera_status") or [])
      if status == "warn"
    )
    entry["status"] = (
      "WARN"
      if drift_count > 0 or hold_count > 0 or drift_status not in ("", "OK")
      else "OK"
    )
    return entry

  # Kafka consumer: systemd running + PLC tag API health.
  if entry.get("is_kafka"):
    if not entry.get("running"):
      entry["status"] = "ERR"
      return entry
    if entry.get("plc_tags_set") is None:
      entry["status"] = "WARN"
      return entry
    _sync_plc_chip_status_from_meta(entry)
    chip_bad = any(
      status in ("warn", "err")
      for group in entry.get("plc_tag_groups") or []
      for status in (group.get("chip_status") or [])
    )
    entry["status"] = (
      "OK" if (
        device_counts_ok(entry.get("plc_tags_set"), entry.get("plc_tags_now"))
        and not chip_bad
      ) else "WARN"
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
