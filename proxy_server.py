"""Central proxy UI and status collector for Docker Image Manager."""

import asyncio
import base64
import json
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from os.path import dirname, join, realpath
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import httpx
from flask import Flask, Response, jsonify, make_response, render_template, request, stream_with_context

from git_versions import (
  DEFAULT_GIT_VERSION_TIMEOUT,
  git_run,
  is_git_repo_dir,
  read_repo_latest,
)
from pipeline_ip import resolve_server_ip, resolve_sys_monitor_ip
from servers_cfg import (
  SERVERS_PATH,
  apply_sys_monitor_host_status,
  edge_command_url,
  edge_host_command_payload,
  edge_host_power_payload,
  edge_port,
  edge_probe_config,
  edge_service_payload,
  empty_camera_drift_metrics,
  empty_plc_tag_metrics,
  fetch_plc_tag_metrics,
  fetch_camera_drift_metrics,
  rewrite_plc_tag_link_urls,
  finalize_pipeline_status,
  group_by_server_ip,
  drift_service_root_url,
  drift_service_url_for,
  is_camera_drift_pipeline,
  is_kafka_pipeline,
  is_sys_monitor_entry,
  is_usable_edge_host,
  merge_edge_status_fields,
  pipeline_kind_flags,
  pipeline_git_url,
  pipeline_service_name,
  plc_status_url_for,
  read_servers_file,
  repo_path_for_pipeline,
  server_cfg_for_ip,
  split_servers_raw,
  status_display_config,
  ensure_servers_meta,
  stream_feed_paths_for,
  tcp_reachable,
  write_servers_file,
)

app = Flask(__name__)
app.json.sort_keys = False
app.config["TEMPLATES_AUTO_RELOAD"] = True
app._static_folder = join(realpath(dirname(__file__)), "templates/static")

meta = {}
cfg = {}
img_n_status = {}
_status_updated_at = 0.0
REFRESH_INTERVAL = 10
VIEWER_IDLE_SEC = 45
HOST_FETCH_TIMEOUT = 20
HOST_FETCH_RETRY_TIMEOUT = 12
_refreshing = False
_collect_reset = False
_pending_reset = False
_rerun_after = False
_collect_generation = 0
_last_viewer_at = 0.0
_collector_thread = None
_collect_wake = threading.Event()
_collect_lock = threading.Lock()
_update_watch = {}
_update_watch_lock = threading.Lock()
_TERMINAL_UPDATE_STEPS = frozenset({"done", "failed", "idle"})
_VERSION_KEYS = (
  "version_current", "version_current_date",
  "version_latest", "version_latest_date",
)
_central_latest_cache = {}
CENTRAL_VERSION_TTL_SEC = 300
HOST_STATUS_READ_TIMEOUT = 15
HOST_STATUS_RETRY_READ_TIMEOUT = 10
HOST_STATUS_CONNECT_TIMEOUT = 5
RTSP_SNAPSHOT_TIMEOUT = 5
RTSP_POLL_INTERVAL_SEC = 0.35
SSE_WARMUP_MAX_LINES = 30
SSE_WARMUP_MAX_LINES_PARTIAL = 8


def _ok_count(status_map):
  return sum(1 for entry in status_map.values() if entry.get("status") in ("OK", "WARN"))


def pipeline_url(image_cfg, entry=None):
  entry = entry or {}
  if is_kafka_pipeline(cfg=image_cfg) or entry.get("is_kafka"):
    return (
      entry.get("plc_status_url")
      or plc_status_url_for(image_cfg, host_ip=image_cfg.get("server_ip"))
      or ""
    )
  if is_camera_drift_pipeline(cfg=image_cfg) or entry.get("is_camera_drift"):
    return (
      entry.get("drift_service_url")
      or drift_service_url_for(image_cfg, host_ip=image_cfg.get("server_ip"))
      or ""
    )
  host = entry.get("streaming_ip")
  if not is_usable_edge_host(host):
    host = image_cfg.get("server_ip")
  port = image_cfg.get("streaming_port")
  if entry.get("streaming_port") is not None:
    port = entry.get("streaming_port")
  if host and port is not None:
    return f"http://{host}:{port}"
  return ""


def _resolved_drift_service_url(image_cfg, host_ip=None):
  return drift_service_url_for(
    image_cfg,
    host_ip=host_ip if host_ip is not None else (image_cfg or {}).get("server_ip"),
  ) or ""


def stream_url_for_pipeline(pipeline_name):
  entry = (img_n_status or {}).get(pipeline_name)
  if entry and entry.get("url"):
    return entry["url"]
  image_cfg = cfg.get(pipeline_name)
  if not image_cfg:
    return ""
  return pipeline_url(image_cfg, entry)


def _config_cameras_from_payload(payload):
  cameras = (payload.get("config") or {}).get("camera")
  return cameras if isinstance(cameras, list) else []


def _camera_count_hint(payload, pipeline_name=None):
  """Best-effort camera count when SSE has no config.camera (e.g. PLC /stream)."""
  cameras = _config_cameras_from_payload(payload)
  if cameras:
    return len(cameras)
  by_cam = _plc_jpeg_by_cam_map(payload)
  if by_cam:
    return len(by_cam)
  unique = _plc_unique_cam_numbers(payload)
  if unique:
    return len(unique)
  jpeg = payload.get("jpeg")
  if isinstance(jpeg, list):
    return len(jpeg)
  if pipeline_name:
    entry = img_n_status.get(pipeline_name) or {}
    for key in ("cameras_now", "cameras_set"):
      n = entry.get(key)
      if isinstance(n, int) and n > 0:
        return n
    links = entry.get("camera_links") or []
    if links:
      return len(links)
  return None


def _camera_stream_slot(payload, cam_idx, *, pipeline_name=None):
  """Map config camera index (C1=0) to active jpeg slot in SSE payload."""
  if cam_idx < 0:
    return None

  handled, slot = _plc_special_slot(
    payload, cam_idx, pipeline_name=pipeline_name,
  )
  if handled:
    return slot

  cameras = _config_cameras_from_payload(payload)
  jpeg = payload.get("jpeg")

  if not cameras or cam_idx >= len(cameras):
    # No config cameras: map by jpeg list index when possible.
    if isinstance(jpeg, list) and cam_idx < len(jpeg):
      return cam_idx
    return None

  cam = cameras[cam_idx] if isinstance(cameras[cam_idx], dict) else {}
  uid = cam.get("uid")

  camera_ids = payload.get("camera_ids")
  if isinstance(camera_ids, list) and camera_ids and uid:
    try:
      return camera_ids.index(uid)
    except ValueError:
      return None

  index_map = payload.get("index_map")
  if isinstance(index_map, list):
    try:
      return index_map.index(cam_idx)
    except ValueError:
      return None

  if isinstance(jpeg, list):
    if len(jpeg) == len(cameras):
      return cam_idx
    # Partial stream (e.g. cobble): slots 0..N-1 map to config indices 0..N-1.
    if (
      not camera_ids
      and not index_map
      and cam_idx < len(jpeg) < len(cameras)
    ):
      return cam_idx
    if pipeline_name and not camera_ids and not index_map:
      slot = _active_camera_stream_slot(pipeline_name, payload, cam_idx)
      if slot is not None:
        return slot
  return None


def _active_camera_stream_slot(pipeline_name, payload, cam_idx):
  """Map by active camera order when stream has fewer slots than config cameras."""
  cameras = _config_cameras_from_payload(payload)
  jpeg = payload.get("jpeg")
  if not isinstance(jpeg, list) or len(jpeg) >= len(cameras):
    return None
  entry = img_n_status.get(pipeline_name) or {}
  statuses = entry.get("camera_status") or []
  if len(statuses) != len(cameras):
    return None
  active = [i for i, status in enumerate(statuses) if status == "ok"]
  if cam_idx not in active or len(active) != len(jpeg):
    return None
  return active.index(cam_idx)


def _sse_can_map_camera(payload, cam_idx, *, pipeline_name=None):
  """Return False when SSE cannot ever serve this camera index."""
  if cam_idx < 0:
    return False

  handled, slot = _plc_special_slot(
    payload, cam_idx, pipeline_name=pipeline_name,
  )
  if handled:
    return slot is not None

  cameras = _config_cameras_from_payload(payload)
  jpeg = payload.get("jpeg")

  if not cameras:
    return isinstance(jpeg, list) and cam_idx < len(jpeg)

  if cam_idx >= len(cameras):
    return False
  if payload.get("camera_ids") or payload.get("index_map"):
    return _camera_stream_slot(payload, cam_idx, pipeline_name=pipeline_name) is not None
  if not isinstance(jpeg, list):
    return True
  if len(jpeg) >= len(cameras):
    return True
  if cam_idx < len(jpeg):
    return True
  if pipeline_name:
    return _active_camera_stream_slot(pipeline_name, payload, cam_idx) is not None
  return False


def _sse_warmup_limit(payload, cam_idx, *, pipeline_name=None):
  if _sse_can_map_camera(payload, cam_idx, pipeline_name=pipeline_name):
    return SSE_WARMUP_MAX_LINES_PARTIAL
  return 0


def _camera_rtsp_url_from_payload(payload, cam_idx):
  cameras = _config_cameras_from_payload(payload)
  if cam_idx < 0 or cam_idx >= len(cameras):
    return None
  cam = cameras[cam_idx]
  if not isinstance(cam, dict):
    return None
  url = cam.get("url")
  return url if url and str(url).lower().startswith("rtsp") else None


def _camera_rtsp_from_status(pipeline_name, cam_idx):
  entry = img_n_status.get(pipeline_name) or {}
  links = entry.get("camera_links") or []
  if cam_idx < 0 or cam_idx >= len(links):
    return None
  link = links[cam_idx]
  url = (link or {}).get("url") or ""
  if url.lower().startswith("rtsp"):
    return url
  host = (link or {}).get("label")
  if host:
    return f"rtsp://{host}:554/"
  return None


def _rtsp_parts_from_url(rtsp_url):
  if not rtsp_url:
    return None
  parsed = urlparse(rtsp_url if "://" in rtsp_url else f"rtsp://{rtsp_url}")
  if not parsed.hostname:
    return None
  host = parsed.hostname
  port = parsed.port or 554
  path = parsed.path or "/"
  safe_url = urlunparse(("rtsp", f"{host}:{port}", path, "", "", ""))
  return {
    "safe_url": safe_url,
    "user": parsed.username or "",
    "password": parsed.password or "",
  }


def _rtsp_input_url(parts):
  safe_url = parts["safe_url"]
  user, password = parts["user"], parts["password"]
  if not user:
    return safe_url
  parsed = urlparse(safe_url)
  netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{parsed.hostname}"
  if parsed.port:
    netloc += f":{parsed.port}"
  return urlunparse(("rtsp", netloc, parsed.path or "/", "", "", ""))


def _rtsp_snapshot_b64(rtsp_url, *, timeout=RTSP_SNAPSHOT_TIMEOUT):
  parts = _rtsp_parts_from_url(rtsp_url)
  if not parts:
    return None
  input_url = _rtsp_input_url(parts)
  try:
    result = subprocess.run(
      [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-stimeout", "3000000",
        "-probesize", "32768",
        "-analyzeduration", "100000",
        "-i", input_url,
        "-frames:v", "1",
        "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
      ],
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      timeout=timeout,
    )
  except (subprocess.TimeoutExpired, OSError):
    return None
  if result.returncode != 0 or not result.stdout:
    return None
  return base64.b64encode(result.stdout).decode("ascii")


def _resolve_rtsp_targets(pipeline_name, cam_idx, payload=None):
  targets = []
  cred_url = _camera_rtsp_url_from_payload(payload, cam_idx) if payload else None
  status_url = _camera_rtsp_from_status(pipeline_name, cam_idx)
  cred_parts = _rtsp_parts_from_url(cred_url) if cred_url else None
  if cred_parts and cred_parts["user"]:
    targets.append(cred_url)
  if status_url and status_url not in targets:
    targets.append(status_url)
  if cred_url and cred_url not in targets:
    targets.append(cred_url)
  return targets


def _stream_feed_paths_for_pipeline(pipeline_name):
  pipeline_cfg = cfg.get(pipeline_name) or {}
  primary, secondary = stream_feed_paths_for(name=pipeline_name, cfg=pipeline_cfg)
  return list(dict.fromkeys(path for path in (primary, secondary) if path))


def _camera_frame_from_payload(payload, cam_idx, *, pipeline_name=None):
  # Prefer PLC-CV per-camera / per-sign ROI maps when present.
  if _plc_has_per_cam_frames(payload):
    frame = _plc_roi_frame_from_payload(
      payload, cam_idx, pipeline_name=pipeline_name,
    )
    return frame or ""

  jpeg = payload.get("jpeg")
  slot = _camera_stream_slot(payload, cam_idx, pipeline_name=pipeline_name)
  if slot is None:
    return None
  if isinstance(jpeg, list):
    if 0 <= slot < len(jpeg):
      frame = jpeg[slot]
      return frame if isinstance(frame, str) else ""
    return ""
  if isinstance(jpeg, str) and slot == 0:
    return jpeg
  return ""


_LAMP_COLOR_RE = re.compile(
  r"\b(RED|GREEN|YELLOW|ORANGE|BLUE|WHITE|AMBER)\b", re.I,
)
_LAMP_CAM_RE = re.compile(r"Cam\s*(\d+)", re.I)
_ROI_COLOR_PRIORITY = {
  "RED": 0,
  "YELLOW": 1,
  "ORANGE": 1,
  "AMBER": 1,
  "GREEN": 2,
  "BLUE": 3,
  "WHITE": 4,
}


def _lamp_color_from_text(*parts):
  for part in parts:
    match = _LAMP_COLOR_RE.search(str(part or ""))
    if match:
      return match.group(1).upper()
  return ""


def _lamp_is_on(state):
  text = str(state or "").strip()
  if not text or re.fullmatch(r"OFF", text, re.I):
    return False
  if re.search(r"\bOFF\b", text, re.I) and not re.search(r"\bON\b", text, re.I):
    return False
  return bool(re.search(r"\bON\b", text, re.I))


def _cam_number_from_text(text):
  match = _LAMP_CAM_RE.search(str(text or ""))
  return int(match.group(1)) if match else None


def _camera_link_cam_number(pipeline_name, cam_idx):
  """Prefer CamN embedded in camera_links title/label for this C-chip index."""
  if not pipeline_name or not isinstance(cam_idx, int) or cam_idx < 0:
    return None
  entry = img_n_status.get(pipeline_name) or {}
  links = entry.get("camera_links") or []
  if cam_idx >= len(links):
    return None
  link = links[cam_idx] or {}
  for key in ("title", "label", "name"):
    num = _cam_number_from_text(link.get(key))
    if num is not None:
      return num
  return None


def _plc_sign_ids(payload):
  states = payload.get("states")
  if not isinstance(states, dict) or not states:
    return []
  order = payload.get("signOrder")
  ids = [sid for sid in order if sid in states] if isinstance(order, list) else []
  ids.extend(sid for sid in states if sid not in ids)
  return ids


def _plc_sign_entries(payload):
  """Return [(sign_id, name, cam_num, color, state), ...] in signOrder."""
  states = payload.get("states")
  if not isinstance(states, dict):
    return []
  stats_map = payload.get("allRatioStats")
  if not isinstance(stats_map, dict):
    stats_map = {}
  entries = []
  for sid in _plc_sign_ids(payload):
    entry = states.get(sid)
    if not isinstance(entry, (list, tuple)) or not entry:
      continue
    name = str(entry[0] or "").strip()
    state = str(entry[1] if len(entry) > 1 else "").strip()
    stats = stats_map.get(sid) if isinstance(stats_map.get(sid), dict) else {}
    color = str(stats.get("color") or "").strip().upper() or _lamp_color_from_text(
      state, name,
    )
    entries.append((sid, name, _cam_number_from_text(name), color, state))
  return entries


def _plc_unique_cam_numbers(payload):
  seen = []
  for _sid, _name, cam_num, _color, _state in _plc_sign_entries(payload):
    if cam_num is not None and cam_num not in seen:
      seen.append(cam_num)
  return seen


def _plc_resolve_cam_number(payload, cam_idx, *, pipeline_name=None):
  """Map C-chip index to sign CamN. Plant signs use Cam8/Cam3, not Cam1=C1."""
  if not isinstance(cam_idx, int) or cam_idx < 0:
    return None
  link_num = _camera_link_cam_number(pipeline_name, cam_idx)
  if link_num is not None:
    return link_num
  unique = _plc_unique_cam_numbers(payload)
  if unique and cam_idx < len(unique):
    return unique[cam_idx]
  return None


def _plc_jpeg_by_sign_map(payload):
  raw = payload.get("jpeg_by_sign")
  return raw if isinstance(raw, dict) else {}


def _plc_jpeg_by_cam_map(payload):
  for key in ("jpeg_by_cam", "jpeg_by_camera", "jpegByCam"):
    raw = payload.get(key)
    if isinstance(raw, dict) and raw:
      return raw
  return {}


def _plc_has_per_cam_frames(payload):
  by_sign = _plc_jpeg_by_sign_map(payload)
  if any(isinstance(v, str) and v for v in by_sign.values()):
    return True
  by_cam = _plc_jpeg_by_cam_map(payload)
  return any(isinstance(v, str) and v for v in by_cam.values())


def _plc_special_slot(payload, cam_idx, *, pipeline_name=None):
  """PLC jpeg layouts (ROI maps / single blob / Cam-aligned list).

  Returns (handled, slot). handled=False → use normal EG camera mapping.
  """
  if _plc_has_per_cam_frames(payload):
    if _plc_roi_frame_from_payload(
      payload, cam_idx, pipeline_name=pipeline_name,
    ):
      return True, 0
    # Maps present but this cam not ready yet — keep reading SSE.
    count = _camera_count_hint(payload, pipeline_name)
    if count is None or cam_idx < count:
      return True, 0
    return True, None

  cameras = _config_cameras_from_payload(payload)
  jpeg = payload.get("jpeg")

  # Legacy: one selected-sign ROI jpeg shared by every chip.
  if isinstance(jpeg, str) and jpeg:
    count = _camera_count_hint(payload, pipeline_name)
    if count is None or cam_idx < count:
      return True, 0
    return True, None

  # jpeg[] aligned with unique CamN order from states.
  if isinstance(jpeg, list) and not cameras:
    unique = _plc_unique_cam_numbers(payload)
    if unique:
      if cam_idx < len(unique) and cam_idx < len(jpeg):
        return True, cam_idx
      return True, None
    if cam_idx < len(jpeg):
      return True, cam_idx
    return True, None

  return False, None


def _lookup_cam_keyed_frame(by_cam, cam_num, cam_idx):
  if not by_cam:
    return None
  keys = []
  if cam_num is not None:
    keys.extend([
      cam_num,
      str(cam_num),
      f"Cam{cam_num}",
      f"cam{cam_num}",
      f"CAM{cam_num}",
    ])
  if isinstance(cam_idx, int) and cam_idx >= 0:
    keys.extend([cam_idx, str(cam_idx), f"C{cam_idx + 1}"])
  for key in keys:
    frame = by_cam.get(key)
    if isinstance(frame, str) and frame:
      return frame
  # Case-insensitive CamN key scan.
  if cam_num is not None:
    needle = f"cam{cam_num}"
    for key, frame in by_cam.items():
      if not isinstance(frame, str) or not frame:
        continue
      if str(key).strip().lower().replace(" ", "") == needle:
        return frame
  return None


def _plc_roi_frame_from_jpeg_by_sign(payload, cam_num):
  by_sign = _plc_jpeg_by_sign_map(payload)
  if not by_sign:
    return None
  candidates = []
  for sid, name, sign_cam, color, state in _plc_sign_entries(payload):
    if cam_num is not None and sign_cam != cam_num:
      continue
    frame = by_sign.get(sid)
    if not isinstance(frame, str) or not frame:
      continue
    priority = _ROI_COLOR_PRIORITY.get(color or "", 50)
    # Prefer an ON lamp crop when several colors share a camera.
    on_rank = 0 if _lamp_is_on(state) else 1
    candidates.append((on_rank, priority, name, frame))
  if candidates:
    candidates.sort()
    return candidates[0][3]
  # Lab / no Cam labels: use selected signId frame.
  if cam_num is None:
    sid = payload.get("signId")
    frame = by_sign.get(sid) if sid else None
    if isinstance(frame, str) and frame:
      return frame
    for frame in by_sign.values():
      if isinstance(frame, str) and frame:
        return frame
  return None


def _plc_roi_frame_from_payload(payload, cam_idx, *, pipeline_name=None):
  """Pick ROI jpeg for a C-chip from jpeg_by_cam / jpeg_by_sign."""
  cam_num = _plc_resolve_cam_number(
    payload, cam_idx, pipeline_name=pipeline_name,
  )
  by_cam = _plc_jpeg_by_cam_map(payload)
  if by_cam:
    frame = _lookup_cam_keyed_frame(by_cam, cam_num, cam_idx)
    if frame:
      return frame
  if _plc_jpeg_by_sign_map(payload):
    return _plc_roi_frame_from_jpeg_by_sign(payload, cam_num)
  return None


def _lamps_public(lamps):
  """Drop internal cam index before sending to the UI."""
  for lamp in lamps:
    lamp.pop("cam", None)
  return lamps


def _lamp_judgments_from_payload(payload, cam_idx=None, *, pipeline_name=None):
  """PLC-CV /stream lamp judgments from states (+ optional cam filter)."""
  states = payload.get("states")
  if not isinstance(states, dict) or not states:
    return None

  lamps = []
  for _sid, name, cam_num, color, state in _plc_sign_entries(payload):
    if not name and not state:
      continue
    on = _lamp_is_on(state)
    lamps.append({
      "name": name,
      "state": state or ("ON" if on else "OFF"),
      "color": color,
      "on": on,
      "cam": cam_num,
    })

  if not lamps:
    return []

  # No CamN labels (lab / RND desk): show every sign.
  if all(lamp.get("cam") is None for lamp in lamps):
    return _lamps_public(lamps)

  resolved = _plc_resolve_cam_number(
    payload, cam_idx, pipeline_name=pipeline_name,
  )
  if resolved is None:
    return _lamps_public(lamps)

  matched = [lamp for lamp in lamps if lamp.get("cam") == resolved]
  if matched:
    return _lamps_public(matched)
  return _lamps_public(lamps)


def _camera_feed_event(payload, cam_idx, *, pipeline_name=None):
  frame = _camera_frame_from_payload(
    payload, cam_idx, pipeline_name=pipeline_name,
  )
  if not frame:
    return None
  event = {"jpeg": frame, "fps": payload.get("fps")}
  lamps = _lamp_judgments_from_payload(
    payload, cam_idx, pipeline_name=pipeline_name,
  )
  if lamps is not None:
    event["lamps"] = lamps
  return event


def load_cfg():
  global meta, cfg
  meta, pipelines = split_servers_raw(read_servers_file(SERVERS_PATH))
  meta = ensure_servers_meta(meta)
  cfg.clear()
  cfg.update({
    name: image_cfg for name, image_cfg in pipelines.items()
    if image_cfg.get("server_id")
  })


def _base_status_entry(name, image_cfg):
  is_sys = is_sys_monitor_entry(name=name, cfg=image_cfg)
  flags = pipeline_kind_flags(name=name, cfg=image_cfg)
  return {
    "running": False,
    "cameras_set": image_cfg.get("cameras_set"),
    "qlight_set": image_cfg.get("qlight_set"),
    "speaker_set": image_cfg.get("speaker_set"),
    "qlight_now": None,
    "speaker_now": None,
    "is_rtls": False if is_sys else flags["is_rtls"],
    "is_kafka": False if is_sys else flags["is_kafka"],
    "is_plc_cv": False if is_sys else flags["is_plc_cv"],
    "is_camera_drift": False if is_sys else flags["is_camera_drift"],
    "is_sys_monitor": is_sys,
    "cameras_now": None,
    "streaming_port": image_cfg.get("streaming_port"),
    "stream_health": False,
    "status": "PENDING",
    "mem_usage": None,
    "mem_usage_percent": None,
    "url": pipeline_url(image_cfg),
    "pipeline": name,
  }


def preview_from_cfg():
  return {name: _base_status_entry(name, image_cfg) for name, image_cfg in cfg.items()}


def build_status_entry(raw, image_cfg, pipeline_name):
  entry = _base_status_entry(pipeline_name, image_cfg)
  if not raw:
    return entry

  try:
    data = json.loads(raw) if isinstance(raw, str) else raw
  except (json.JSONDecodeError, TypeError):
    return entry

  if not isinstance(data, dict):
    return entry

  merge_edge_status_fields(entry, data)
  entry["url"] = pipeline_url(image_cfg, entry)
  return entry


def _parse_json_dict(text):
  try:
    data = json.loads(text)
  except (json.JSONDecodeError, TypeError, ValueError):
    return None
  return data if isinstance(data, dict) else None


def run_collect(reset=False):
  global img_n_status, _status_updated_at, _refreshing, _collect_reset
  with _collect_lock:
    collect_gen = _collect_generation
  _collect_reset = reset
  _refreshing = True
  if reset:
    _reset_status_keeping_cache()
  cancelled = False
  try:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    cancelled = loop.run_until_complete(collect_status_via_edge(collect_gen))
    loop.close()
    if not cancelled:
      _status_updated_at = time.time()
  except Exception as exc:
    print(f"check_status failed: {exc}")
  finally:
    _refreshing = False
  return cancelled


def note_viewer_activity():
  global _last_viewer_at
  _last_viewer_at = time.time()
  _collect_wake.set()


def _viewers_active():
  if _last_viewer_at <= 0:
    return False
  return (time.time() - _last_viewer_at) < VIEWER_IDLE_SEC


def _collect_was_cancelled(collect_gen):
  with _collect_lock:
    return collect_gen != _collect_generation


def request_force_refresh():
  global _pending_reset, _rerun_after, img_n_status, _collect_reset
  global _collect_generation
  with _collect_lock:
    _collect_generation += 1
    _pending_reset = True
    _rerun_after = True
    _collect_reset = True
    _reset_status_keeping_cache()
  _collect_wake.set()


def start_collector():
  global _collector_thread

  def collector_loop():
    global _pending_reset, _rerun_after
    while True:
      while not _viewers_active():
        _collect_wake.wait(1.0)
        _collect_wake.clear()

      reset = False
      with _collect_lock:
        if _pending_reset:
          reset = True
          _pending_reset = False
        elif not _is_collect_complete(img_n_status):
          reset = True

      if not _viewers_active():
        continue

      run_collect(reset=reset)

      with _collect_lock:
        if _rerun_after:
          _rerun_after = False
          continue

      if not _viewers_active():
        continue

      deadline = time.time() + REFRESH_INTERVAL
      while time.time() < deadline:
        if not _viewers_active():
          break
        with _collect_lock:
          if _rerun_after:
            break
        remaining = deadline - time.time()
        if remaining <= 0:
          break
        _collect_wake.wait(min(remaining, 1.0))
        _collect_wake.clear()

  with _collect_lock:
    if _collector_thread and _collector_thread.is_alive():
      return
    _collector_thread = threading.Thread(target=collector_loop, daemon=True)
    _collector_thread.start()


def pipeline_static():
  result = {}
  for name, image_cfg in cfg.items():
    flags = pipeline_kind_flags(name=name, cfg=image_cfg)
    result[name] = {
      "url": pipeline_url(image_cfg),
      "server_id": image_cfg.get("server_id"),
      "service_name": pipeline_service_name(image_cfg, name=name),
      "server_ip": image_cfg.get("server_ip") or "",
      "monitor_host_ip": image_cfg.get("monitor_host_ip") or "",
      "eg_pipeline_path": image_cfg.get("eg_pipeline_path") or "",
      "sys_monitor_path": image_cfg.get("sys_monitor_path") or "",
      "cameras_set": image_cfg.get("cameras_set"),
      "qlight_set": image_cfg.get("qlight_set"),
      "speaker_set": image_cfg.get("speaker_set"),
      **flags,
    }
    if flags["is_kafka"]:
      result[name]["plc_status_url"] = plc_status_url_for(image_cfg) or ""
    if flags["is_camera_drift"]:
      result[name]["drift_service_url"] = _resolved_drift_service_url(image_cfg)
  return result


def _local_ipv4_addrs():
  """IPv4 addresses on this proxy host (used to power self last)."""
  ips = {"127.0.0.1"}
  try:
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
      ip = info[4][0]
      if ip:
        ips.add(ip)
  except OSError:
    pass
  try:
    out = subprocess.check_output(
      ["hostname", "-I"], text=True, timeout=2,
    )
    for token in out.split():
      if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", token):
        ips.add(token)
  except (OSError, subprocess.SubprocessError):
    pass
  try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect(("8.8.8.8", 80))
    ips.add(sock.getsockname()[0])
    sock.close()
  except OSError:
    pass
  return ips


def _partition_servers_self_last(server_ips):
  """Return (others, self_ips) so this proxy host is powered last."""
  local = _local_ipv4_addrs()
  others, selves = [], []
  for ip in server_ips:
    (selves if ip in local else others).append(ip)
  return others, selves


def render_context():
  return {
    "pipeline_static": pipeline_static(),
    "pipeline_order": list(cfg.keys()),
    "pipeline_groups": pipeline_groups(),
    "status_display": status_display_config(meta),
    "proxy_host_ips": sorted(
      ip for ip in _local_ipv4_addrs() if ip and not ip.startswith("127.")
    ),
  }


def pipeline_groups():
  groups = {}
  order = []
  for name in cfg.keys():
    group = name.split("-", 1)[0] if "-" in name else "Other"
    if group not in groups:
      groups[group] = []
      order.append(group)
    groups[group].append(name)
  return [(group, groups[group]) for group in order]


def _render_index():
  note_viewer_activity()
  resp = make_response(render_template("index.html", **render_context()))
  resp.headers["Cache-Control"] = "no-store"
  return resp


@app.route("/")
@app.route("/index", methods=["GET"])
@app.route("/update", methods=["GET"])
def index():
  return _render_index()


SERVICE_COMMANDS = {"service:start", "service:stop", "service:restart", "service:status"}
MANAGE_COMMANDS = SERVICE_COMMANDS | {"update"}
SERVER_POWER_COMMANDS = {
  "server:reboot": "reboot",
  "server:shutdown": "shutdown",
}


@app.route("/manage_docker", methods=["POST"])
async def manage_docker():
  req_json = request.get_json(silent=True) or {}
  docker_command = req_json.get("command")
  docker_images = req_json.get("lst_images")
  if not docker_command:
    return jsonify({"ok": False, "error": "command required"}), 400
  if not isinstance(docker_images, list):
    return jsonify({"ok": False, "error": "lst_images required"}), 400

  if docker_command not in MANAGE_COMMANDS:
    return Response("Command not allowed", 400)

  if docker_command == "update":
    git_refs = req_json.get("git_refs") if isinstance(req_json.get("git_refs"), dict) else {}
    by_ip = group_by_server_ip(docker_images, cfg)
    results = await run_n_update(docker_command, docker_images, by_ip=by_ip, git_refs=git_refs)
    started, busy, errors = _summarize_update_results(results)
    if started or busy:
      _register_update_watch([*started, *busy], by_ip)
    request_force_refresh()
    ok = bool(started) and not busy and not errors
    status = 200 if ok else 409
    return jsonify({
      "ok": ok,
      "started": started,
      "busy": busy,
      "errors": errors,
    }), status

  job_id = _start_service_job(docker_command, docker_images)
  _spawn_service_job(job_id, docker_command, docker_images)
  return jsonify({
    "ok": True,
    "job_id": job_id,
    "command": docker_command,
    "pipelines": docker_images,
  }), 202


@app.route("/manage_servers", methods=["POST"])
async def manage_servers():
  req_json = request.get_json(silent=True) or {}
  command = req_json.get("command")
  servers = req_json.get("servers")
  action = SERVER_POWER_COMMANDS.get(command)
  if not action:
    return jsonify({"ok": False, "error": "command required"}), 400
  if not isinstance(servers, list) or not servers:
    return jsonify({"ok": False, "error": "servers required"}), 400

  unique_servers = list(dict.fromkeys(
    str(ip).strip() for ip in servers if str(ip or "").strip()
  ))
  # Schedule every other host first; power this proxy host last so it can
  # finish dispatching before its own reboot/shutdown.
  others, selves = _partition_servers_self_last(unique_servers)
  results = []
  if others:
    results.extend(await asyncio.gather(*[
      _run_host_power(server_ip, action) for server_ip in others
    ]))
  for server_ip in selves:
    results.append(await _run_host_power(server_ip, action))
  ok = all(entry.get("ok") for entry in results)
  return jsonify({"ok": ok, "results": results}), 200 if ok else 409


async def _run_host_power(server_ip, action):
  image_cfg = server_cfg_for_ip(server_ip, cfg)
  if not image_cfg or not is_usable_edge_host(server_ip):
    return {
      "server_ip": server_ip,
      "ok": False,
      "response": "server not found",
    }
  url = edge_command_url(image_cfg, host_ip=server_ip)
  data = edge_host_power_payload(action)
  try:
    text = await post_edge_command_async(url, data, read_timeout=15)
    label = (text or "").strip()
    return {
      "server_ip": server_ip,
      "ok": label == "Scheduled",
      "response": label or "empty response",
    }
  except Exception as exc:
    return {
      "server_ip": server_ip,
      "ok": False,
      "response": str(exc),
    }


def _summarize_update_results(results):
  started, busy, errors = [], [], {}
  for server_ip, text in results.items():
    label = text.strip() if isinstance(text, str) else ""
    if label == "Started":
      started.append(server_ip)
    elif label == "Busy":
      busy.append(server_ip)
    else:
      errors[server_ip] = label or "unknown response"
  return started, busy, errors


def _register_update_watch(started_ips, by_ip):
  with _update_watch_lock:
    for server_ip in started_ips:
      if server_ip in by_ip:
        _update_watch[server_ip] = {
          "since": time.time(),
          "pipelines": list(by_ip[server_ip]),
        }


def _update_step_active(status):
  if status.get("running"):
    return True
  return status.get("step") not in _TERMINAL_UPDATE_STEPS


def _prune_update_watch(server_status):
  """Drop finished hosts only after every watched host has reached a terminal step."""
  now = time.time()
  with _update_watch_lock:
    if not _update_watch:
      return
    for server_ip in _update_watch:
      if _update_step_active(server_status.get(server_ip, {})):
        return
    for server_ip in list(_update_watch):
      status = server_status.get(server_ip, {})
      finished_at = status.get("updated_at") or now
      if now - finished_at >= 45:
        del _update_watch[server_ip]


def _update_status_active(servers):
  return any(_update_step_active(status) for status in servers.values())


def _update_status_entry(server_ip, watch_info, **overrides):
  entry = {
    "running": False,
    "step": "failed",
    "elapsed_sec": 0,
    "pipelines": watch_info.get("pipelines", []),
    "server_ip": server_ip,
  }
  entry.update(overrides)
  return entry


async def _fetch_edge_update_status(server_ip, port):
  url = edge_command_url({"port": port}, host_ip=server_ip)
  data = json.dumps({"update_status": True})
  text = await post_edge_command_async(url, data, read_timeout=10)
  payload = _parse_json_dict(text)
  if payload is not None:
    return payload
  return _update_status_entry(
    server_ip,
    {},
    step_label="Status unavailable",
    error=text or "invalid response",
  )


async def _collect_update_status():
  with _update_watch_lock:
    watch = dict(_update_watch)
  if not watch:
    return {}

  by_ip = _pipelines_by_ip()
  tasks = {}
  for server_ip, info in watch.items():
    host_info = by_ip.get(server_ip)
    if not host_info:
      continue
    tasks[server_ip] = asyncio.create_task(
      _fetch_edge_update_status(server_ip, host_info["port"]),
    )

  results = {}
  for server_ip, task in tasks.items():
    try:
      status = await task
      status["pipelines"] = watch[server_ip].get("pipelines", [])
      status["server_ip"] = server_ip
      results[server_ip] = status
    except Exception as exc:
      results[server_ip] = _update_status_entry(
        server_ip,
        watch[server_ip],
        step_label="Status fetch failed",
        error=str(exc),
      )

  _prune_update_watch(results)
  return results


def _status_request_item(name, image_cfg):
  is_sys = is_sys_monitor_entry(name=name, cfg=image_cfg)
  item = {
    "name": name,
    "server_id": image_cfg["server_id"],
    "service_name": pipeline_service_name(image_cfg, name=name),
    "is_sys_monitor": is_sys,
  }
  if is_sys:
    item["sys_monitor_path"] = image_cfg.get("sys_monitor_path")
    item["server_ip"] = image_cfg.get("server_ip")
    return item

  flags = pipeline_kind_flags(name=name, cfg=image_cfg)
  item.update({
    "eg_pipeline_path": image_cfg.get("eg_pipeline_path"),
    "is_rtls": flags["is_rtls"],
    "is_kafka": flags["is_kafka"],
    "is_plc_cv": flags["is_plc_cv"],
    "is_camera_drift": flags["is_camera_drift"],
    "monitor_host_ip": image_cfg.get("monitor_host_ip"),
    "server_ip": image_cfg.get("server_ip"),
  })
  if flags["is_kafka"]:
    # Edge probes via localhost; proxy enrich uses host_ip / explicit URL.
    item["plc_status_url"] = image_cfg.get("plc_status_url") or ""
    item["plc_status_port"] = image_cfg.get("plc_status_port")
    item["plc_status_path"] = image_cfg.get("plc_status_path")
    return item
  if flags["is_camera_drift"]:
    item["drift_service_url"] = image_cfg.get("drift_service_url") or ""
    item["drift_service_port"] = image_cfg.get("drift_service_port")
    item["drift_service_path"] = image_cfg.get("drift_service_path")
    # Optional override when auto-match (name contains camera_drift) is wrong.
    if image_cfg.get("container_name"):
      item["container_name"] = image_cfg["container_name"]
    return item

  primary, secondary = stream_feed_paths_for(name=name, cfg=image_cfg)
  item.update({
    "streaming_port": image_cfg.get("streaming_port"),
    "cameras_set": image_cfg.get("cameras_set"),
    "stream_feed_paths": [primary, secondary],
  })
  return item


def _pipelines_by_ip():
  by_ip = {}
  for name, image_cfg in cfg.items():
    server_ip = image_cfg.get("server_ip")
    if not is_usable_edge_host(server_ip):
      continue
    by_ip.setdefault(server_ip, {"port": edge_port(image_cfg), "items": []})
    by_ip[server_ip]["items"].append(_status_request_item(name, image_cfg))
  return by_ip


def _mark_pipelines_without_ip():
  for name, image_cfg in cfg.items():
    if is_usable_edge_host(image_cfg.get("server_ip")):
      continue
    _mark_pipeline_err(name)


def _xsite_id():
  return meta.get("xSiteId") or os.environ.get("xSiteId")


def _save_pipeline_server_ip(pipeline_name, server_ip):
  raw = read_servers_file(SERVERS_PATH)
  file_meta, pipelines = split_servers_raw(raw)
  if pipeline_name not in pipelines:
    return False
  if not (file_meta.get("xSiteId") or _xsite_id()):
    return False
  pipelines[pipeline_name]["server_ip"] = server_ip
  write_servers_file(file_meta, pipelines, path=SERVERS_PATH)
  cfg[pipeline_name]["server_ip"] = server_ip
  return True


def _preserve_version_fields(entry, name):
  old = img_n_status.get(name) or {}
  for key in _VERSION_KEYS:
    if not entry.get(key) and old.get(key):
      entry[key] = old[key]


def _snapshot_version_fields(status_map=None):
  """Keep Current/Latest across force-refresh resets (status rows are wiped)."""
  status_map = status_map if status_map is not None else img_n_status
  out = {}
  for name, entry in (status_map or {}).items():
    if not isinstance(entry, dict):
      continue
    snap = {key: entry.get(key) for key in _VERSION_KEYS if entry.get(key)}
    if snap:
      out[name] = snap
  return out


def _restore_version_fields(status_map, version_snap):
  if not status_map or not version_snap:
    return status_map
  for name, snap in version_snap.items():
    entry = status_map.get(name)
    if not isinstance(entry, dict) or not isinstance(snap, dict):
      continue
    for key, value in snap.items():
      if value and not entry.get(key):
        entry[key] = value
  return status_map


_DRIFT_METRIC_KEYS = tuple(empty_camera_drift_metrics())
_PLC_METRIC_KEYS = tuple(empty_plc_tag_metrics())
# Keep row healthy across wipe while systemd / probes re-run.
_PROBE_RUNTIME_KEYS = (
  "running", "status", "mem_usage", "mem_usage_percent",
  "drift_service_url", "plc_status_url", "url",
)


def _copy_entry_keys(dst, src, keys):
  for key in keys:
    if key in src:
      dst[key] = src[key]


def _probe_metric_keys_for(entry):
  if entry.get("is_camera_drift") and not _drift_metrics_incomplete(entry):
    return _DRIFT_METRIC_KEYS
  if entry.get("is_kafka") and not _kafka_metrics_incomplete(entry):
    return _PLC_METRIC_KEYS
  return ()


def _snapshot_probe_metrics(status_map=None):
  """Keep last good drift/PLC chips across force-refresh row wipes."""
  status_map = status_map if status_map is not None else img_n_status
  out = {}
  for name, entry in (status_map or {}).items():
    if not isinstance(entry, dict):
      continue
    metric_keys = _probe_metric_keys_for(entry)
    if not metric_keys:
      continue
    snap = {}
    _copy_entry_keys(snap, entry, metric_keys)
    _copy_entry_keys(snap, entry, _PROBE_RUNTIME_KEYS)
    out[name] = snap
  return out


def _restore_probe_metrics(status_map, probe_snap):
  if not status_map or not probe_snap:
    return status_map
  for name, snap in probe_snap.items():
    entry = status_map.get(name)
    if not isinstance(entry, dict) or not isinstance(snap, dict):
      continue
    entry.update(snap)
    finalize_pipeline_status(entry)
  return status_map


def _preserve_probe_metrics(entry, name):
  """Keep last good drift/PLC chips when a refresh probe fails mid-cycle."""
  old = img_n_status.get(name) or {}
  if not old:
    return
  if entry.get("is_camera_drift") and _drift_metrics_incomplete(entry):
    if not _drift_metrics_incomplete(old):
      _copy_entry_keys(entry, old, _DRIFT_METRIC_KEYS)
  if entry.get("is_kafka") and _kafka_metrics_incomplete(entry):
    if not _kafka_metrics_incomplete(old):
      _copy_entry_keys(entry, old, _PLC_METRIC_KEYS)


def _reset_status_keeping_cache():
  """Wipe rows to PENDING but keep versions + last-good probe chips."""
  global img_n_status
  version_snap = _snapshot_version_fields(img_n_status)
  probe_snap = _snapshot_probe_metrics(img_n_status)
  img_n_status = preview_from_cfg()
  _restore_version_fields(img_n_status, version_snap)
  _restore_probe_metrics(img_n_status, probe_snap)


def _central_git_cmd(repo_path, *args, timeout=None):
  return git_run(
    repo_path, *args,
    timeout=timeout or DEFAULT_GIT_VERSION_TIMEOUT,
    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
  )


def _read_central_repo_latest(repo_path, *, name=None, image_cfg=None):
  remote_url = None
  if not is_git_repo_dir(repo_path):
    remote_url = pipeline_git_url(name=name, cfg=image_cfg, repo_path=repo_path)
  return read_repo_latest(
    repo_path,
    git_cmd=_central_git_cmd,
    remote_url=remote_url,
    timeout=DEFAULT_GIT_VERSION_TIMEOUT,
  )


def _cached_central_repo_latest(repo_path, *, name=None, image_cfg=None):
  """Return (latest, latest_date) with a short TTL to avoid repeat ls-remote."""
  path_key = repo_path or f"url:{pipeline_git_url(name=name, cfg=image_cfg, repo_path=repo_path)}"
  now = time.time()
  cached = _central_latest_cache.get(path_key)
  if cached and (now - cached[0]) < CENTRAL_VERSION_TTL_SEC:
    return cached[1], cached[2]
  latest, latest_date = _read_central_repo_latest(
    repo_path, name=name, image_cfg=image_cfg,
  )
  if latest:
    _central_latest_cache[path_key] = (now, latest, latest_date)
  return latest, latest_date


def _fill_central_latest_versions():
  """Fill version_latest from central checkouts (edges only report HEAD)."""
  by_path = {}
  for name, entry in img_n_status.items():
    image_cfg = cfg.get(name)
    if not image_cfg:
      continue
    path = repo_path_for_pipeline(name=name, cfg=image_cfg)
    by_path.setdefault(path, []).append(name)

  if not by_path:
    return

  def _lookup(path, names):
    sample = names[0]
    image_cfg = cfg.get(sample)
    latest, latest_date = _cached_central_repo_latest(
      path, name=sample, image_cfg=image_cfg,
    )
    return path, names, latest, latest_date

  workers = min(8, len(by_path))
  with ThreadPoolExecutor(max_workers=workers) as pool:
    futures = [
      pool.submit(_lookup, path, names) for path, names in by_path.items()
    ]
    for future in as_completed(futures):
      try:
        _path, names, latest, latest_date = future.result()
      except Exception:
        continue
      if not latest:
        continue
      for name in names:
        entry = img_n_status.get(name)
        if not entry:
          continue
        entry["version_latest"] = latest
        if latest_date:
          entry["version_latest_date"] = latest_date
        else:
          entry["version_latest_date"] = None
        current = entry.get("version_current")
        if current and current == latest and entry.get("version_current_date"):
          entry["version_latest_date"] = entry["version_current_date"]


def _apply_host_status(ip, host_result):
  if not isinstance(host_result, dict):
    return
  for name, entry in host_result.items():
    if name not in cfg:
      continue
    image_cfg = cfg[name]
    entry["pipeline"] = name
    flags = pipeline_kind_flags(name=name, cfg=image_cfg)
    entry["is_rtls"] = flags["is_rtls"]
    entry["is_kafka"] = flags["is_kafka"]
    entry["is_plc_cv"] = flags["is_plc_cv"]
    entry["is_camera_drift"] = flags["is_camera_drift"]
    entry["is_sys_monitor"] = flags["is_sys_monitor"]
    if flags["is_kafka"] and not entry.get("plc_status_url"):
      entry["plc_status_url"] = plc_status_url_for(image_cfg, host_ip=ip) or ""
    if flags["is_camera_drift"] and not entry.get("drift_service_url"):
      entry["drift_service_url"] = _resolved_drift_service_url(image_cfg, host_ip=ip)
    entry["url"] = pipeline_url(image_cfg, entry)
    _preserve_probe_metrics(entry, name)
    finalize_pipeline_status(entry)
    _preserve_version_fields(entry, name)
    img_n_status[name] = entry


def _mark_pipeline_err(name):
  if name not in cfg:
    return
  entry = build_status_entry(None, cfg[name], name)
  finalize_pipeline_status(entry)
  img_n_status[name] = entry


def _mark_host_err(items):
  for item in items:
    _mark_pipeline_err(item["name"])


def _mark_missing_pipelines_err(items, host_result):
  for item in items:
    name = item["name"]
    if name not in host_result:
      _mark_pipeline_err(name)


def _apply_host_fetch_result(ip, host_result, items):
  failed = False
  if not host_result:
    _mark_host_err(items)
    failed = True
  else:
    try:
      _apply_host_status(ip, host_result)
      _mark_missing_pipelines_err(items, host_result)
    except Exception as exc:
      print(f"status apply from {ip} failed: {exc}")
      _mark_host_err(items)
      failed = True
  apply_sys_monitor_host_status(img_n_status, cfg, server_ip=ip)
  return failed


def _parse_host_status_response(text, ip=None):
  data = _parse_json_dict(text)
  if data is not None:
    return data
  if ip:
    print(f"status from {ip}: invalid JSON: {text[:120]!r}")
  return None


async def _probe_edge_service_active(url, service, probe_timeout):
  """Return True when edge reports systemd unit active."""
  if not service:
    return False
  try:
    text = await post_edge_command_async(
      url,
      data=json.dumps({"service:status": service}),
      read_timeout=probe_timeout,
    )
    return (text or "").strip().lower() == "active"
  except Exception as exc:
    print(f"service status probe {url} {service}: {exc}")
    return False


async def _enrich_systemd_running(ip, host_info, host_result, read_timeout):
  """Backfill running=True for systemd-only units older edges miss."""
  if not isinstance(host_result, dict):
    return
  url = edge_command_url({"port": host_info["port"]}, host_ip=ip)
  probe_timeout = min(max(read_timeout, 1), 10)
  for item in host_info.get("items") or []:
    name = item.get("name")
    entry = host_result.get(name) if name else None
    if not entry or entry.get("running"):
      continue
    service = (item.get("service_name") or "").strip()
    if not service:
      continue
    if await _probe_edge_service_active(url, service, probe_timeout):
      entry["running"] = True
      finalize_pipeline_status(entry)


def _kafka_metrics_incomplete(entry):
  """True when the edge did not return usable Kafka /plc metrics."""
  return entry.get("plc_tags_set") is None


def _drift_metrics_incomplete(entry):
  """True when Camera-Drift metrics/chips are missing or unusable."""
  if entry.get("drift_api_ok") is not True or entry.get("drift_cameras_set") is None:
    return True
  try:
    set_n = int(entry.get("drift_cameras_set"))
  except (TypeError, ValueError):
    return True
  if set_n <= 0:
    return False
  groups = entry.get("drift_camera_groups") or []
  first = groups[0] if groups and isinstance(groups[0], dict) else {}
  if first.get("links") or first.get("chip_status"):
    return False
  # Flat fallback when area groups are absent.
  return not (entry.get("drift_camera_links") or entry.get("drift_camera_status"))


async def _enrich_kafka_status(ip, host_info, host_result):
  """Fill /plc URL; probe tags only if the edge left metrics empty."""
  if not isinstance(host_result, dict):
    return
  loop = asyncio.get_event_loop()
  for item in host_info.get("items") or []:
    if not item.get("is_kafka"):
      continue
    name = item.get("name")
    entry = host_result.get(name) if name else None
    if not entry:
      continue

    image_cfg = cfg.get(name) or {}
    plc_url = (
      image_cfg.get("plc_status_url")
      or plc_status_url_for(image_cfg, host_ip=ip)
    )
    if plc_url:
      entry["plc_status_url"] = plc_url
      entry["url"] = plc_url
      # Edge often probes via 127.0.0.1; rewrite Open API links for the browser.
      rewrite_plc_tag_link_urls(entry, plc_url)

    if plc_url and _kafka_metrics_incomplete(entry):
      try:
        metrics = await loop.run_in_executor(
          None, lambda u=plc_url: fetch_plc_tag_metrics(u, timeout=5.0),
        )
        entry.update(metrics)
      except Exception as exc:
        print(f"kafka plc tags probe {ip}: {exc}")

    finalize_pipeline_status(entry)


async def _enrich_camera_drift_status(ip, host_info, host_result):
  """Fill drift URL; probe /get_drift only if the edge left metrics empty."""
  if not isinstance(host_result, dict):
    return
  loop = asyncio.get_event_loop()
  for item in host_info.get("items") or []:
    if not item.get("is_camera_drift"):
      continue
    name = item.get("name")
    entry = host_result.get(name) if name else None
    if not entry:
      continue

    image_cfg = cfg.get(name) or {}
    drift_url = _resolved_drift_service_url(image_cfg, host_ip=ip)
    if drift_url:
      entry["drift_service_url"] = drift_url
      entry["url"] = drift_url
      if _drift_metrics_incomplete(entry):
        try:
          metrics = await loop.run_in_executor(
            None, lambda u=drift_url: fetch_camera_drift_metrics(u, timeout=10.0),
          )
          entry.update(metrics)
        except Exception as exc:
          print(f"camera drift probe {ip}: {exc}")

    finalize_pipeline_status(entry)


async def _fetch_host_status(ip, host_info, read_timeout):
  url = edge_command_url({"port": host_info["port"]}, host_ip=ip)
  payload = {
    "status": host_info["items"],
    "edge_probe": edge_probe_config(meta),
  }
  text = await post_edge_command_async(
    url,
    data=json.dumps(payload),
    read_timeout=read_timeout,
    connect_timeout=HOST_STATUS_CONNECT_TIMEOUT,
  )
  host_result = _parse_host_status_response(text, ip)
  if host_result:
    await _enrich_systemd_running(ip, host_info, host_result, read_timeout)
    await _enrich_camera_drift_status(ip, host_info, host_result)
    await _enrich_kafka_status(ip, host_info, host_result)
  return host_result


async def _collect_host_ips(ips, by_ip, collect_gen, read_timeout, fetch_timeout):
  failed_ips = []

  async def fetch_host(ip):
    if _collect_was_cancelled(collect_gen):
      return ip, None
    host_info = by_ip[ip]
    try:
      host_result = await asyncio.wait_for(
        _fetch_host_status(ip, host_info, read_timeout),
        timeout=fetch_timeout,
      )
      return ip, host_result
    except asyncio.TimeoutError:
      print(f"status from {ip} timed out")
      return ip, None
    except Exception as exc:
      print(f"status from {ip} failed: {exc}")
      return ip, None

  tasks = [asyncio.create_task(fetch_host(ip)) for ip in ips]
  try:
    for task in asyncio.as_completed(tasks):
      if _collect_was_cancelled(collect_gen):
        break
      ip, host_result = await task
      if _apply_host_fetch_result(ip, host_result, by_ip[ip]["items"]):
        failed_ips.append(ip)
  finally:
    for task in tasks:
      if not task.done():
        task.cancel()
    if tasks:
      await asyncio.gather(*tasks, return_exceptions=True)

  return failed_ips


async def collect_status_via_edge(collect_gen):
  global img_n_status, _rerun_after
  if not img_n_status:
    img_n_status = preview_from_cfg()

  _mark_pipelines_without_ip()

  by_ip = _pipelines_by_ip()
  ips = list(by_ip.keys())

  failed_ips = await _collect_host_ips(
    ips, by_ip, collect_gen,
    read_timeout=HOST_STATUS_READ_TIMEOUT,
    fetch_timeout=HOST_FETCH_TIMEOUT,
  )

  if failed_ips and not _collect_was_cancelled(collect_gen):
    await asyncio.sleep(0.5)
    await _collect_host_ips(
      failed_ips, by_ip, collect_gen,
      read_timeout=HOST_STATUS_RETRY_READ_TIMEOUT,
      fetch_timeout=HOST_FETCH_RETRY_TIMEOUT,
    )

  if _collect_was_cancelled(collect_gen):
    return True

  # Edges report Current (local HEAD); Latest comes from central (TTL cache).
  _fill_central_latest_versions()

  apply_sys_monitor_host_status(img_n_status, cfg)

  ok = _ok_count(img_n_status)
  print(f"check_status: {len(img_n_status)} pipelines, {ok} OK (via edge)")
  return False


def _done_count(status_map):
  return sum(1 for entry in status_map.values() if entry.get("status") != "PENDING")


def _is_collect_complete(status_map):
  return bool(status_map) and len(status_map) >= len(cfg) and _done_count(status_map) >= len(cfg)


def status_payload():
  pipelines = img_n_status if img_n_status else preview_from_cfg()
  complete = _is_collect_complete(img_n_status)
  ready = complete and not _pending_reset
  return {
    "pipelines": pipelines,
    "refreshing": _refreshing or _pending_reset,
    "background_refreshing": _refreshing and not _collect_reset and not _pending_reset,
    "ready": ready,
    "done_count": _done_count(pipelines),
    "updated_at": _status_updated_at,
    "ok_count": _ok_count(pipelines),
    "total": len(cfg),
  }


def _no_store_json(data, status=200):
  resp = make_response(jsonify(data), status)
  resp.headers["Cache-Control"] = "no-store"
  return resp


def _resolve_pipeline_server_ip(pipeline_name, image_cfg, xsite_id):
  server_id = image_cfg.get("server_id")
  if is_sys_monitor_entry(name=pipeline_name, cfg=image_cfg):
    return resolve_sys_monitor_ip(xsite_id, server_id), "edge_status_ip not found in sys cfg"
  return resolve_server_ip(xsite_id, server_id), "streaming_ip not found in S3 cfg"


@app.route("/update_server_ip", methods=["POST"])
def update_server_ip():
  note_viewer_activity()
  body = request.get_json(silent=True) or {}
  pipeline_name = body.get("pipeline")
  if not pipeline_name or pipeline_name not in cfg:
    return jsonify({"ok": False, "error": "service not found"}), 404

  image_cfg = cfg[pipeline_name]
  server_id = image_cfg.get("server_id")
  if not server_id:
    return jsonify({"ok": False, "error": "server_id missing"}), 400

  xsite_id = _xsite_id()
  if not xsite_id:
    return jsonify({"ok": False, "error": "xSiteId not configured"}), 400

  server_ip, ip_error = _resolve_pipeline_server_ip(pipeline_name, image_cfg, xsite_id)
  if not server_ip:
    return jsonify({"ok": False, "error": ip_error}), 404

  if not _save_pipeline_server_ip(pipeline_name, server_ip):
    return jsonify({"ok": False, "error": "failed to save servers.json"}), 500

  return jsonify({"ok": True, "pipeline": pipeline_name, "server_ip": server_ip})


PING_COUNT = 3
PING_TIMEOUT_SEC = 2
_HOST_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def _parse_ping_target(raw):
  text = (raw or "").strip()
  if not text:
    return None, None, "host required"
  if "://" in text:
    parsed = urlparse(text)
    host = parsed.hostname or ""
    port = parsed.port
  elif text.count(":") == 1 and text.rsplit(":", 1)[1].isdigit():
    host, port_text = text.rsplit(":", 1)
    port = int(port_text)
  else:
    host, port = text, None
  host = host.strip()
  if not host or not _HOST_RE.match(host):
    return None, None, "invalid host"
  return host, port, None


def ping_host(host):
  host_input = (host or "").strip()
  parsed_host, port, parse_err = _parse_ping_target(host_input)
  if parse_err:
    return {"ok": False, "host": host_input, "error": parse_err, "rtt_ms": None, "output": ""}

  display = f"{parsed_host}:{port}" if port else parsed_host
  tcp_ok = tcp_reachable(parsed_host, port, timeout=PING_TIMEOUT_SEC) if port else None

  try:
    result = subprocess.run(
      ["ping", "-c", str(PING_COUNT), "-W", str(PING_TIMEOUT_SEC), parsed_host],
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      timeout=PING_TIMEOUT_SEC + 3,
      text=True,
    )
    output = (result.stdout or "").strip()
    ping_ok = result.returncode == 0
    rtt_ms = None
    if ping_ok:
      match = re.search(r"time[=<]([\d.]+)\s*ms", output)
      if match:
        rtt_ms = float(match.group(1))
  except subprocess.TimeoutExpired:
    return {"ok": False, "host": display, "error": "ping timed out", "rtt_ms": None, "output": ""}
  except (OSError, ValueError) as exc:
    return {"ok": False, "host": display, "error": str(exc), "rtt_ms": None, "output": ""}

  if port:
    ok = tcp_ok is True
    if ok:
      detail = f"TCP port {port} is open"
      if ping_ok and rtt_ms is not None:
        detail += f"; ICMP {rtt_ms} ms"
      elif ping_ok:
        detail += "; ICMP reachable"
      else:
        detail += "; ICMP blocked or unreachable"
    else:
      detail = f"TCP port {port} unreachable"
    return {
      "ok": ok,
      "host": display,
      "rtt_ms": rtt_ms if ping_ok else None,
      "output": output,
      "error": None if ok else detail,
      "detail": detail,
    }

  return {
    "ok": ping_ok,
    "host": display,
    "rtt_ms": rtt_ms,
    "output": output,
    "error": None if ping_ok else "unreachable",
  }


@app.route("/ping_device", methods=["POST"])
def ping_device():
  note_viewer_activity()
  body = request.get_json(silent=True) or {}
  host = (body.get("host") or "").strip()
  if not host:
    return jsonify({"ok": False, "error": "host required"}), 400
  return jsonify(ping_host(host))


@app.route("/camera_drift_reset", methods=["POST"])
async def camera_drift_reset():
  """Proxy POST /camera/{cam_uid}/reset to the Camera-Drift service."""
  note_viewer_activity()
  body = request.get_json(silent=True) or {}
  cam_uid = str(body.get("cam_uid") or "").strip()
  pipeline_name = str(body.get("pipeline") or "").strip()
  if not cam_uid:
    return jsonify({"ok": False, "error": "cam_uid required"}), 400

  pipeline_name, _image_cfg, root, err, code = _resolve_camera_drift_target(pipeline_name)
  if err:
    return jsonify({"ok": False, "error": err}), code
  reset_url = f"{root}/camera/{quote(cam_uid, safe='')}/reset"

  try:
    timeout = httpx.Timeout(connect=5, read=20, write=10, pool=5)
    async with httpx.AsyncClient(timeout=timeout) as client:
      resp = await client.post(reset_url)
    text = (resp.text or "").strip()
    if resp.status_code >= 400:
      return jsonify({
        "ok": False,
        "error": text or f"HTTP {resp.status_code}",
        "status_code": resp.status_code,
        "url": reset_url,
      }), 502
    return jsonify({
      "ok": True,
      "pipeline": pipeline_name,
      "cam_uid": cam_uid,
      "url": reset_url,
      "response": text[:500],
    })
  except Exception as exc:
    return jsonify({
      "ok": False,
      "error": str(exc),
      "url": reset_url,
    }), 502


def _resolve_camera_drift_target(pipeline_name=""):
  """Return (pipeline_name, image_cfg, root_url, error, http_status)."""
  name = str(pipeline_name or "").strip()
  image_cfg = cfg.get(name) if name else None
  if not image_cfg:
    for candidate_name, candidate in cfg.items():
      if is_camera_drift_pipeline(cfg=candidate, name=candidate_name):
        image_cfg = candidate
        name = candidate_name
        break
  if not image_cfg or not is_camera_drift_pipeline(cfg=image_cfg, name=name):
    return name, None, None, "camera drift service not found", 404

  drift_url = _resolved_drift_service_url(image_cfg)
  if not drift_url:
    return name, image_cfg, None, "drift service url missing", 400
  root = drift_service_root_url(drift_url)
  if not root:
    return name, image_cfg, None, "drift service url missing", 400
  return name, image_cfg, root, None, 200


def _rewrite_drift_image_side(side_info, *, pipeline_name, cam_uid, side, lang=""):
  if not isinstance(side_info, dict):
    return side_info
  out = dict(side_info)
  if not out.get("available"):
    out["url"] = None
    return out
  params = {
    "pipeline": pipeline_name or "",
    "cam_uid": cam_uid,
  }
  if lang:
    params["lang"] = lang
  # Keep edge query from meta.url (preview, batch, …) — do not drop batch.
  raw_url = str(out.get("url") or "").strip()
  if raw_url:
    for key, value in parse_qsl(urlparse(raw_url).query, keep_blank_values=True):
      if key in ("pipeline", "cam_uid"):
        continue
      params[key] = value
  if side in ("before", "after") and "preview" not in params:
    params["preview"] = "1"
  out["url"] = f"/camera_drift_images/{side}?{urlencode(params)}"
  return out


def _drift_edge_images_url(root, cam_uid, side_name=None, lang="", query=None):
  """Build edge drift_images URL, forwarding query params (preview/batch/…)."""
  edge_url = f"{root}/camera/{quote(cam_uid, safe='')}/drift_images"
  if side_name:
    edge_url = f"{edge_url}/{side_name}"
  params = {}
  if isinstance(query, dict):
    for key, value in query.items():
      if value is None:
        continue
      key_s = str(key)
      if key_s in ("pipeline", "cam_uid"):
        continue
      params[key_s] = value if isinstance(value, str) else str(value)
  if lang and "lang" not in params:
    params["lang"] = lang
  if params:
    edge_url = f"{edge_url}?{urlencode(params)}"
  return edge_url


@app.route("/camera_drift_images", methods=["GET"])
async def camera_drift_images():
  """Proxy Camera-Drift image metadata.

  Meta: GET /camera_drift_images?pipeline=&cam_uid=&phase=ref|compare|full
  """
  note_viewer_activity()
  cam_uid = str(request.args.get("cam_uid") or "").strip()
  pipeline_name = str(request.args.get("pipeline") or "").strip()
  if not cam_uid:
    return jsonify({"ok": False, "error": "cam_uid required"}), 400

  pipeline_name, _image_cfg, root, err, code = _resolve_camera_drift_target(pipeline_name)
  if err:
    return jsonify({"ok": False, "error": err}), code

  lang = str(request.args.get("lang") or request.headers.get("Accept-Language") or "").strip()
  # Forward phase/lang/… to edge; pipeline/cam_uid stay central-only.
  forward = {
    key: request.args.get(key)
    for key in request.args
    if key not in ("pipeline", "cam_uid")
  }
  if lang and not str(forward.get("lang") or "").strip():
    forward["lang"] = lang
  edge_url = _drift_edge_images_url(root, cam_uid, query=forward)

  try:
    # compare/full may run illumination match; ref should be faster.
    timeout = httpx.Timeout(connect=5, read=55, write=10, pool=5)
    async with httpx.AsyncClient(timeout=timeout) as client:
      resp = await client.get(edge_url)
  except Exception as exc:
    return jsonify({"ok": False, "error": str(exc), "url": edge_url}), 502

  try:
    payload = resp.json()
  except ValueError:
    return jsonify({
      "ok": False,
      "error": (resp.text or "").strip()[:500] or f"HTTP {resp.status_code}",
      "status_code": resp.status_code,
      "url": edge_url,
    }), 502

  if not isinstance(payload, dict):
    return jsonify({"ok": False, "error": "invalid drift images payload"}), 502

  out = dict(payload)
  out["pipeline"] = pipeline_name
  out["cam_uid"] = cam_uid
  rewrite_kw = {
    "pipeline_name": pipeline_name,
    "cam_uid": cam_uid,
    "lang": lang,
  }
  out["before"] = _rewrite_drift_image_side(out.get("before"), side="before", **rewrite_kw)
  out["after"] = _rewrite_drift_image_side(out.get("after"), side="after", **rewrite_kw)
  out["overlay"] = _rewrite_drift_image_side(out.get("overlay"), side="overlay", **rewrite_kw)
  status = 200 if out.get("ok") is not False and resp.status_code < 400 else (
    resp.status_code if resp.status_code >= 400 else 404
  )
  return jsonify(out), status


@app.route("/camera_drift_images/<side>", methods=["GET"])
def camera_drift_images_side(side):
  """Proxy image bytes via a sync view (safe streaming under Flask async).

  Image: GET /camera_drift_images/{before|after|overlay}?pipeline=&cam_uid=
  """
  note_viewer_activity()
  cam_uid = str(request.args.get("cam_uid") or "").strip()
  pipeline_name = str(request.args.get("pipeline") or "").strip()
  if not cam_uid:
    return jsonify({"ok": False, "error": "cam_uid required"}), 400

  side_name = str(side or "").strip().lower()
  if side_name not in ("before", "after", "overlay"):
    return jsonify({"ok": False, "error": "side must be before, after, or overlay"}), 400

  pipeline_name, _image_cfg, root, err, code = _resolve_camera_drift_target(pipeline_name)
  if err:
    return jsonify({"ok": False, "error": err}), code

  # Forward edge-relevant query (preview, batch, lang, …).
  # "_" is browser cache-bust only — never send it to edge (breaks batch cache).
  forward = {
    key: request.args.get(key)
    for key in request.args
    if key not in ("pipeline", "cam_uid", "_")
  }
  if side_name in ("before", "after") and not str(forward.get("preview") or "").strip():
    forward["preview"] = "1"
  edge_url = _drift_edge_images_url(
    root, cam_uid, side_name=side_name, query=forward,
  )
  return _proxy_drift_image_stream(edge_url)


@app.route("/camera_drift_persistent_points", methods=["GET", "POST"])
async def camera_drift_persistent_points():
  """Proxy pinpoint list/add/undo/clear to Camera-Drift.

  GET  ?pipeline=&cam_uid=
  POST {pipeline, cam_uid, points:[{x,y}]}
       {pipeline, cam_uid, remove_last_manual:true}
       {pipeline, cam_uid, remove_points:[...]}
       {pipeline, cam_uid, clear_manual:true}
  """
  note_viewer_activity()
  if request.method == "GET":
    cam_uid = str(request.args.get("cam_uid") or "").strip()
    pipeline_name = str(request.args.get("pipeline") or "").strip()
    body = {}
  else:
    body = request.get_json(silent=True) or {}
    cam_uid = str(body.get("cam_uid") or request.args.get("cam_uid") or "").strip()
    pipeline_name = str(body.get("pipeline") or request.args.get("pipeline") or "").strip()

  if not cam_uid:
    return jsonify({"ok": False, "error": "cam_uid required"}), 400

  pipeline_name, _image_cfg, root, err, code = _resolve_camera_drift_target(pipeline_name)
  if err:
    return jsonify({"ok": False, "error": err}), code

  edge_url = f"{root}/camera/{quote(cam_uid, safe='')}/persistent_points"
  try:
    timeout = httpx.Timeout(connect=5, read=20, write=10, pool=5)
    async with httpx.AsyncClient(timeout=timeout) as client:
      if request.method == "GET":
        resp = await client.get(edge_url)
      else:
        payload = {}
        if body.get("clear_manual"):
          payload["clear_manual"] = True
        if body.get("remove_last_manual"):
          payload["remove_last_manual"] = True
        if isinstance(body.get("remove_points"), list):
          payload["remove_points"] = body.get("remove_points")
        if isinstance(body.get("points"), list):
          payload["points"] = body.get("points")
        resp = await client.post(edge_url, json=payload)
  except Exception as exc:
    return jsonify({"ok": False, "error": str(exc), "url": edge_url}), 502

  try:
    data = resp.json()
  except ValueError:
    return jsonify({
      "ok": False,
      "error": (resp.text or "").strip()[:500] or f"HTTP {resp.status_code}",
      "status_code": resp.status_code,
      "url": edge_url,
    }), 502

  if not isinstance(data, dict):
    return jsonify({"ok": False, "error": "invalid persistent points payload"}), 502

  out = dict(data)
  out["ok"] = bool(data.get("success", resp.status_code < 400))
  out["pipeline"] = pipeline_name
  out["cam_uid"] = cam_uid
  status = 200 if out["ok"] and resp.status_code < 400 else (
    resp.status_code if resp.status_code >= 400 else 502
  )
  return jsonify(out), status


def _proxy_drift_image_stream(edge_url):
  """Stream edge image bytes without buffering the full body (sync views only)."""
  timeout = httpx.Timeout(connect=5, read=60, write=10, pool=5)
  client = httpx.Client(timeout=timeout)
  try:
    req = client.build_request("GET", edge_url)
    resp = client.send(req, stream=True)
  except Exception as exc:
    client.close()
    return jsonify({"ok": False, "error": str(exc), "url": edge_url}), 502

  if resp.status_code >= 400:
    try:
      err_text = (resp.read() or b"").decode("utf-8", errors="replace")[:500]
    except Exception:
      err_text = ""
    resp.close()
    client.close()
    return jsonify({
      "ok": False,
      "error": err_text or f"HTTP {resp.status_code}",
      "status_code": resp.status_code,
      "url": edge_url,
    }), 502

  content_type = resp.headers.get("content-type") or (
    "image/jpeg" if "/overlay" in edge_url else "image/png"
  )

  def generate():
    try:
      for chunk in resp.iter_bytes(chunk_size=64 * 1024):
        if chunk:
          yield chunk
    finally:
      resp.close()
      client.close()

  return Response(
    generate(),
    mimetype=content_type,
    headers={
      # Browser may reuse bytes across modal reopens; UI cache-busts after Reset.
      "Cache-Control": "private, max-age=300",
      "X-Accel-Buffering": "no",
    },
  )


@app.route("/camera_feed")
def camera_feed():
  note_viewer_activity()
  pipeline_name = request.args.get("pipeline", "")
  try:
    cam_idx = int(request.args.get("cam", 0))
  except (TypeError, ValueError):
    return "invalid cam", 400
  if pipeline_name not in cfg:
    return "service not found", 404
  if cam_idx < 0:
    return "invalid cam", 400

  if is_kafka_pipeline(cfg=cfg.get(pipeline_name), name=pipeline_name):
    return "kafka services have no camera stream", 404
  stream_url = stream_url_for_pipeline(pipeline_name)
  if not stream_url:
    return "stream url unavailable", 404
  feed_paths = _stream_feed_paths_for_pipeline(pipeline_name)
  if not feed_paths:
    return "stream feed unavailable", 404

  @stream_with_context
  def generate():
    timeout = httpx.Timeout(connect=5, read=None, write=5, pool=5)
    last_payload = None
    unresolved_lines = 0
    got_stream_frame = False
    warmup_limit = SSE_WARMUP_MAX_LINES
    try:
      with httpx.Client(timeout=timeout) as client:
        for feed_path in feed_paths:
          if got_stream_frame:
            break
          unresolved_lines = 0
          try:
            with client.stream(
              "GET",
              f"{stream_url}{feed_path}",
              headers={"Accept": "text/event-stream"},
            ) as resp:
              if resp.status_code != 200:
                continue
              for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                  continue
                try:
                  payload = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                  continue
                last_payload = payload
                warmup_limit = _sse_warmup_limit(
                  payload, cam_idx, pipeline_name=pipeline_name,
                )
                if warmup_limit == 0:
                  break
                slot = _camera_stream_slot(
                  payload, cam_idx, pipeline_name=pipeline_name,
                )
                if slot is None:
                  unresolved_lines += 1
                  if unresolved_lines >= warmup_limit:
                    break
                  continue
                unresolved_lines = 0
                event = _camera_feed_event(
                  payload, cam_idx, pipeline_name=pipeline_name,
                )
                if event:
                  got_stream_frame = True
                  yield f"data: {json.dumps(event)}\n\n"
          except httpx.HTTPError:
            continue
          if warmup_limit == 0 and not got_stream_frame:
            break
      if got_stream_frame:
        return

      rtsp_targets = _resolve_rtsp_targets(pipeline_name, cam_idx, last_payload)
      if not rtsp_targets:
        yield f"data: {json.dumps({'error': 'Camera stream URL unavailable'})}\n\n"
        return

      target_idx = 0
      while True:
        frame = _rtsp_snapshot_b64(rtsp_targets[target_idx])
        if not frame and target_idx + 1 < len(rtsp_targets):
          target_idx += 1
          continue
        if frame:
          yield f"data: {json.dumps({'jpeg': frame, 'fps': 'RTSP'})}\n\n"
        time.sleep(RTSP_POLL_INTERVAL_SEC)
    except httpx.HTTPError as exc:
      yield f"data: {json.dumps({'error': str(exc)})}\n\n"

  return Response(generate(), mimetype="text/event-stream")


@app.route("/get_status", methods=["GET"])
def get_status():
  note_viewer_activity()
  if request.args.get("force") == "1":
    request_force_refresh()
  return _no_store_json(status_payload())


@app.route("/update_status", methods=["GET"])
async def get_update_status():
  note_viewer_activity()
  servers = await _collect_update_status()
  return _no_store_json({
    "active": _update_status_active(servers),
    "servers": servers,
  })


SERVICE_OK_RESPONSES = frozenset({"Succeed", "200", "Started"})
SERVICE_INFO_RESPONSES = frozenset({"Already running", "Already stopped"})
SERVICE_COMMAND_TIMEOUT = 200
_SERVICE_JOB_TTL_SEC = 3600
_SERVICE_JOB_MAX = 200
_service_jobs = {}
_service_jobs_lock = threading.Lock()


def _prune_service_jobs_unlocked(now=None):
  now = now or time.time()
  stale = [
    job_id for job_id, job in _service_jobs.items()
    if not job.get("active") and now - job.get("updated_at", 0) > _SERVICE_JOB_TTL_SEC
  ]
  for job_id in stale:
    del _service_jobs[job_id]
  if len(_service_jobs) <= _SERVICE_JOB_MAX:
    return
  finished = sorted(
    (
      (job_id, job.get("updated_at", 0))
      for job_id, job in _service_jobs.items()
      if not job.get("active")
    ),
    key=lambda item: item[1],
  )
  overflow = len(_service_jobs) - _SERVICE_JOB_MAX
  for job_id, _updated_at in finished[:overflow]:
    _service_jobs.pop(job_id, None)


def _pipeline_service_ref(pipeline_name):
  image_cfg = cfg[pipeline_name]
  return image_cfg, {
    "pipeline": pipeline_name,
    "server_ip": image_cfg.get("server_ip", ""),
    "service": pipeline_service_name(image_cfg, name=pipeline_name),
  }


def _pending_service_result(pipeline_name):
  _, base = _pipeline_service_ref(pipeline_name)
  return {
    **base,
    "status": "pending",
    "phase": "queued",
    "started_at": None,
    "ok": False,
    "info": False,
    "response": "",
  }


def _start_service_job(command, pipeline_names):
  job_id = uuid.uuid4().hex
  job = {
    "job_id": job_id,
    "command": command,
    "active": True,
    "pipelines": list(pipeline_names),
    "results": {name: _pending_service_result(name) for name in pipeline_names},
    "updated_at": time.time(),
  }
  with _service_jobs_lock:
    _service_jobs[job_id] = job
    _prune_service_jobs_unlocked()
  return job_id


def _set_service_job_result(job_id, pipeline_name, entry):
  entry["status"] = "done"
  entry["phase"] = "done"
  with _service_jobs_lock:
    job = _service_jobs.get(job_id)
    if not job:
      return
    job["results"][pipeline_name] = entry
    job["updated_at"] = time.time()


def _mark_pipeline_active(job_id, pipeline_name):
  with _service_jobs_lock:
    job = _service_jobs.get(job_id)
    if not job:
      return
    entry = job["results"][pipeline_name]
    entry["phase"] = "active"
    entry["started_at"] = time.time()
    job["current_pipeline"] = pipeline_name
    job["updated_at"] = time.time()


def _finalize_service_job(job_id, pipeline_names):
  with _service_jobs_lock:
    job = _service_jobs.get(job_id)
    if not job:
      return
    for pipeline_name in pipeline_names:
      entry = job["results"][pipeline_name]
      if entry.get("status") == "done":
        continue
      failed = _service_result_entry(pipeline_name, "Interrupted")
      failed["status"] = "done"
      failed["phase"] = "done"
      job["results"][pipeline_name] = failed
    job["active"] = False
    job["updated_at"] = time.time()
    _prune_service_jobs_unlocked()


def _service_job_snapshot(job_id):
  with _service_jobs_lock:
    job = _service_jobs.get(job_id)
    if not job:
      return None
    results = [job["results"][name] for name in job["pipelines"]]
    done = all(entry.get("status") == "done" for entry in results)
    return {
      "job_id": job_id,
      "command": job["command"],
      "active": job["active"],
      "pipelines": job["pipelines"],
      "results": results,
      "done": done and not job["active"],
      "ok": done and not job["active"] and all(entry.get("ok") for entry in results),
      "updated_at": job["updated_at"],
    }


def _service_status_ok(text):
  state = text.strip().lower()
  return state in ("active", "activating")


def _service_status_info(text):
  state = text.strip().lower()
  return state in ("inactive", "deactivating")


def _service_result_entry(pipeline_name, response_text, command=None):
  _, base = _pipeline_service_ref(pipeline_name)
  text = response_text.strip() if isinstance(response_text, str) else str(response_text)
  if command == "service:status":
    ok = _service_status_ok(text)
    info = not ok and _service_status_info(text)
    return {
      **base,
      "ok": ok,
      "info": info,
      "response": text,
    }
  return {
    **base,
    "ok": text in SERVICE_OK_RESPONSES or text in SERVICE_INFO_RESPONSES,
    "info": text in SERVICE_INFO_RESPONSES,
    "response": text,
  }


async def _run_single_service_command(command, pipeline_name):
  image_cfg = cfg[pipeline_name]
  data = edge_service_payload(command, image_cfg, name=pipeline_name)
  url = edge_command_url(image_cfg)
  return await post_edge_command_async(url, data, read_timeout=SERVICE_COMMAND_TIMEOUT)


async def _run_server_service_commands(job_id, command, names, server_ip):
  async def run_one(pipeline_name):
    _mark_pipeline_active(job_id, pipeline_name)
    try:
      text = await _run_single_service_command(command, pipeline_name)
      entry = _service_result_entry(pipeline_name, text, command)
    except Exception as exc:
      entry = _service_result_entry(pipeline_name, str(exc), command)
    _set_service_job_result(job_id, pipeline_name, entry)

  await asyncio.gather(*[run_one(name) for name in names])


def _fail_service_job(job_id, pipeline_names, message):
  for pipeline_name in pipeline_names:
    with _service_jobs_lock:
      job = _service_jobs.get(job_id)
      if not job:
        return
      if job["results"][pipeline_name].get("status") == "done":
        continue
    _set_service_job_result(job_id, pipeline_name, _service_result_entry(pipeline_name, message))
  _finalize_service_job(job_id, pipeline_names)


def _spawn_service_job(job_id, command, pipeline_names):
  def runner():
    try:
      loop = asyncio.new_event_loop()
      asyncio.set_event_loop(loop)
      loop.run_until_complete(_run_service_job(job_id, command, pipeline_names))
      loop.close()
    except Exception as exc:
      print(f"service job {job_id} failed: {exc}", flush=True)
      _fail_service_job(job_id, pipeline_names, str(exc))

  threading.Thread(
    target=runner,
    daemon=True,
    name=f"service-job-{job_id[:8]}",
  ).start()


async def _run_service_job(job_id, command, pipeline_names):
  try:
    by_ip = group_by_server_ip(pipeline_names, cfg)
    await asyncio.gather(*[
      _run_server_service_commands(job_id, command, names, server_ip)
      for server_ip, names in by_ip.items()
    ])
  finally:
    _finalize_service_job(job_id, pipeline_names)
    request_force_refresh()


@app.route("/service_command_status")
async def service_command_status():
  job_id = request.args.get("job_id", "")
  snapshot = _service_job_snapshot(job_id)
  if not snapshot:
    return _no_store_json({"error": "job not found"}, 404)
  return _no_store_json(snapshot)


async def run_n_update(docker_command, docker_images, by_ip=None, git_refs=None):
  by_ip = by_ip or group_by_server_ip(docker_images, cfg)
  tasks = {}

  for server_ip, names in by_ip.items():
    img_cfg = cfg[names[0]]
    url = edge_command_url(img_cfg, host_ip=server_ip)
    data = edge_host_command_payload(docker_command, names, cfg, git_refs=git_refs)
    print("Update request:", server_ip, names, f"git_refs={git_refs or {}}")
    tasks[server_ip] = asyncio.create_task(post_edge_command_async(url, data, read_timeout=60))

  results = {}
  for server_ip, task in tasks.items():
    try:
      results[server_ip] = await task
      print(f"Update response from {server_ip}: {results[server_ip]!r}")
    except Exception as exc:
      print(f"Update failed for {server_ip}: {exc}")
      results[server_ip] = f"error: {exc}"
  return results


async def post_edge_command_async(url, data, read_timeout=20, connect_timeout=10):
  if read_timeout is None:
    timeout = None
  else:
    timeout = httpx.Timeout(
      connect=connect_timeout, read=read_timeout, write=10, pool=10,
    )
  headers = {"Content-Type": "application/json"}
  async with httpx.AsyncClient(timeout=timeout) as client:
    response = await client.post(url, content=data, headers=headers)
    return response.content.decode("utf-8")


if __name__ == "__main__":
  load_cfg()
  start_collector()
  app.run(host="0.0.0.0", port=5503)
