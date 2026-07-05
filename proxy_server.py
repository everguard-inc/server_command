"""Central proxy UI and status collector for Docker Image Manager."""

import asyncio
import json
import os
import re
import subprocess
import threading
import time
import uuid
from os.path import dirname, join, realpath
from urllib.parse import urlparse

import httpx
from flask import Flask, Response, jsonify, make_response, render_template, request, stream_with_context

from git_versions import GIT_VERSION_TIMEOUT, git_run, git_commit_date, git_latest_remote, git_latest_remote_url, is_git_repo_dir
from pipeline_ip import resolve_server_ip, resolve_sys_monitor_ip
from servers_cfg import (
  EG_PIPELINE_PATH,
  EDGE_STATUS_MERGE_KEYS,
  SERVERS_PATH,
  SYS_MONITOR_PATH,
  apply_sys_monitor_host_status,
  edge_command_url,
  edge_host_command_payload,
  edge_port,
  edge_probe_config,
  edge_service_payload,
  finalize_pipeline_status,
  group_by_server_ip,
  is_rtls_pipeline,
  is_sys_monitor_entry,
  is_usable_edge_host,
  is_usable_stream_host,
  merge_edge_status_data,
  pipeline_kind_flags,
  pipeline_git_url,
  pipeline_path_for,
  pipeline_service_name,
  read_servers_file,
  split_servers_raw,
  status_display_config,
  stream_feed_path_for,
  stream_feed_paths_for,
  tcp_reachable_optional,
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
HOST_FETCH_TIMEOUT = 50
HOST_FETCH_RETRY_TIMEOUT = 25
EDGE_STATUS_KEYS = EDGE_STATUS_MERGE_KEYS
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
_fetch_versions_next = False


def _ok_count(status_map):
  return sum(1 for entry in status_map.values() if entry.get("status") in ("OK", "WARN"))


def pipeline_url(image_cfg, entry=None):
  host = (entry or {}).get("streaming_ip")
  if not is_usable_stream_host(host):
    host = image_cfg.get("server_ip")
  port = image_cfg.get("streaming_port")
  if entry and entry.get("streaming_port") is not None:
    port = entry.get("streaming_port")
  if host and port is not None:
    return f"http://{host}:{port}"
  return ""


def stream_url_for_pipeline(pipeline_name):
  entry = (img_n_status or {}).get(pipeline_name)
  if entry and entry.get("url"):
    return entry["url"]
  image_cfg = cfg.get(pipeline_name)
  if not image_cfg:
    return ""
  return pipeline_url(image_cfg, entry)


def _camera_frame_from_payload(payload, cam_idx):
  jpeg = payload.get("jpeg")
  if isinstance(jpeg, list):
    if 0 <= cam_idx < len(jpeg):
      return jpeg[cam_idx] or ""
    return ""
  if isinstance(jpeg, str) and cam_idx == 0:
    return jpeg
  return ""


def load_cfg():
  global meta, cfg
  meta, pipelines = split_servers_raw(read_servers_file(SERVERS_PATH))
  cfg.clear()
  cfg.update({
    name: image_cfg for name, image_cfg in pipelines.items()
    if image_cfg.get("server_id")
  })


def _base_status_entry(name, image_cfg):
  is_sys = is_sys_monitor_entry(name=name, cfg=image_cfg)
  return {
    "running": False,
    "cameras_set": image_cfg.get("cameras_set"),
    "qlight_set": image_cfg.get("qlight_set"),
    "speaker_set": image_cfg.get("speaker_set"),
    "qlight_now": None,
    "speaker_now": None,
    "is_rtls": False if is_sys else is_rtls_pipeline(image_cfg),
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

  merge_edge_status_data(entry, data, keys=EDGE_STATUS_KEYS)
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
    img_n_status = preview_from_cfg()
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


def request_force_refresh(*, fetch_versions=False):
  global _pending_reset, _rerun_after, img_n_status, _collect_reset
  global _collect_generation, _fetch_versions_next
  with _collect_lock:
    _collect_generation += 1
    _pending_reset = True
    _rerun_after = True
    _collect_reset = True
    img_n_status = preview_from_cfg()
    if fetch_versions:
      _fetch_versions_next = True
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
    result[name] = {
      "url": pipeline_url(image_cfg),
      "server_id": image_cfg.get("server_id"),
      "service_name": pipeline_service_name(image_cfg, name=name),
      "server_ip": image_cfg.get("server_ip") or "",
      "monitor_host_ip": image_cfg.get("monitor_host_ip") or "",
      "cameras_set": image_cfg.get("cameras_set"),
      "qlight_set": image_cfg.get("qlight_set"),
      "speaker_set": image_cfg.get("speaker_set"),
      **pipeline_kind_flags(name=name, cfg=image_cfg),
    }
  return result


def render_context():
  return {
    "pipeline_static": pipeline_static(),
    "pipeline_order": list(cfg.keys()),
    "pipeline_groups": pipeline_groups(),
    "status_display": status_display_config(meta),
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


@app.route("/manage_docker", methods=["GET", "POST"])
async def manage_docker():
  req_json = request.get_json()
  docker_command = req_json["command"]
  docker_images = req_json["lst_images"]
  print("Client res", docker_command, docker_images)

  if docker_command not in MANAGE_COMMANDS:
    return Response("Command not allowed", 400)

  if docker_command == "update":
    by_ip = group_by_server_ip(docker_images, cfg)
    results = await run_n_update(docker_command, docker_images, by_ip=by_ip)
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
  else:
    primary, secondary = stream_feed_paths_for(name=name, cfg=image_cfg)
    item.update({
      "streaming_port": image_cfg.get("streaming_port"),
      "cameras_set": image_cfg.get("cameras_set"),
      "eg_pipeline_path": image_cfg.get("eg_pipeline_path"),
      "is_rtls": is_rtls_pipeline(image_cfg),
      "monitor_host_ip": image_cfg.get("monitor_host_ip"),
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


def _repo_path_for_pipeline(name, image_cfg):
  if is_sys_monitor_entry(name=name, cfg=image_cfg):
    return image_cfg.get("sys_monitor_path") or SYS_MONITOR_PATH
  return image_cfg.get("eg_pipeline_path") or pipeline_path_for(name)


def _read_central_repo_latest(repo_path, *, name=None, image_cfg=None):
  if is_git_repo_dir(repo_path):
    full_sha, latest = git_latest_remote(repo_path, timeout=GIT_VERSION_TIMEOUT)
    if not full_sha:
      return None, None
    latest_date = git_commit_date(repo_path, full_sha)
    if not latest_date:
      git_run(
        repo_path, "fetch", "origin", full_sha, "--depth=1", "--quiet",
        timeout=GIT_VERSION_TIMEOUT,
      )
      latest_date = git_commit_date(repo_path, full_sha)
    return latest, latest_date

  remote_url = pipeline_git_url(name=name, cfg=image_cfg, repo_path=repo_path)
  full_sha, latest = git_latest_remote_url(remote_url, timeout=GIT_VERSION_TIMEOUT)
  if not latest:
    return None, None
  return latest, None


def _fill_central_latest_versions():
  """Backfill version_latest when edge hosts cannot reach GitHub."""
  missing_by_path = {}
  for name, entry in img_n_status.items():
    if entry.get("version_latest"):
      continue
    image_cfg = cfg.get(name)
    if not image_cfg:
      continue
    path = _repo_path_for_pipeline(name, image_cfg)
    missing_by_path.setdefault(path, []).append(name)

  for path, names in missing_by_path.items():
    sample = names[0]
    image_cfg = cfg.get(sample)
    latest, latest_date = _read_central_repo_latest(
      path, name=sample, image_cfg=image_cfg,
    )
    if not latest:
      continue
    print(f"central version fill: {path} -> {latest} ({len(names)} pipeline(s))")
    for name in names:
      entry = img_n_status.get(name)
      if not entry or entry.get("version_latest"):
        continue
      entry["version_latest"] = latest
      if latest_date:
        entry["version_latest_date"] = latest_date
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
    entry["url"] = pipeline_url(image_cfg, entry)
    entry["pipeline"] = name
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


async def _fetch_host_status(ip, host_info, read_timeout, *, fetch_versions=False):
  url = edge_command_url({"port": host_info["port"]}, host_ip=ip)
  payload = {
    "status": host_info["items"],
    "edge_probe": edge_probe_config(meta),
  }
  if fetch_versions:
    payload["fetch_versions"] = True
  text = await post_edge_command_async(url, data=json.dumps(payload), read_timeout=read_timeout)
  return _parse_host_status_response(text, ip)


async def _collect_host_ips(
  ips, by_ip, collect_gen, read_timeout, fetch_timeout, *, fetch_versions=False,
):
  failed_ips = []

  async def fetch_host(ip):
    if _collect_was_cancelled(collect_gen):
      return ip, None
    host_info = by_ip[ip]
    try:
      host_result = await asyncio.wait_for(
        _fetch_host_status(ip, host_info, read_timeout, fetch_versions=fetch_versions),
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
  global img_n_status, _fetch_versions_next
  if not img_n_status:
    img_n_status = preview_from_cfg()

  with _collect_lock:
    fetch_versions = _fetch_versions_next
    _fetch_versions_next = False

  _mark_pipelines_without_ip()

  by_ip = _pipelines_by_ip()
  ips = list(by_ip.keys())

  failed_ips = await _collect_host_ips(
    ips, by_ip, collect_gen,
    read_timeout=45, fetch_timeout=HOST_FETCH_TIMEOUT,
    fetch_versions=fetch_versions,
  )

  if failed_ips and not _collect_was_cancelled(collect_gen):
    await asyncio.sleep(0.5)
    await _collect_host_ips(
      failed_ips, by_ip, collect_gen,
      read_timeout=20, fetch_timeout=HOST_FETCH_RETRY_TIMEOUT,
      fetch_versions=fetch_versions,
    )

  if _collect_was_cancelled(collect_gen):
    return True

  if fetch_versions:
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
    return jsonify({"ok": False, "error": "pipeline not found"}), 404

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


def _tcp_reachable(host, port, *, timeout=PING_TIMEOUT_SEC):
  return tcp_reachable_optional(host, port, timeout=timeout)


def ping_host(host):
  host_input = (host or "").strip()
  parsed_host, port, parse_err = _parse_ping_target(host_input)
  if parse_err:
    return {"ok": False, "host": host_input, "error": parse_err, "rtt_ms": None, "output": ""}

  display = f"{parsed_host}:{port}" if port else parsed_host
  tcp_ok = _tcp_reachable(parsed_host, port) if port else None

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


@app.route("/camera_feed")
def camera_feed():
  note_viewer_activity()
  pipeline_name = request.args.get("pipeline", "")
  try:
    cam_idx = int(request.args.get("cam", 0))
  except (TypeError, ValueError):
    return "invalid cam", 400
  if pipeline_name not in cfg:
    return "pipeline not found", 404
  if cam_idx < 0:
    return "invalid cam", 400

  stream_url = stream_url_for_pipeline(pipeline_name)
  if not stream_url:
    return "stream url unavailable", 404
  feed_path = stream_feed_path_for(name=pipeline_name, cfg=cfg.get(pipeline_name))

  @stream_with_context
  def generate():
    timeout = httpx.Timeout(connect=5, read=None, write=5, pool=5)
    try:
      with httpx.Client(timeout=timeout) as client:
        with client.stream(
          "GET",
          f"{stream_url}{feed_path}",
          headers={"Accept": "text/event-stream"},
        ) as resp:
          if resp.status_code != 200:
            yield f"data: {json.dumps({'error': 'stream unavailable'})}\n\n"
            return
          for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
              continue
            try:
              payload = json.loads(line[5:].strip())
            except json.JSONDecodeError:
              continue
            frame = _camera_frame_from_payload(payload, cam_idx)
            yield f"data: {json.dumps({'jpeg': frame, 'fps': payload.get('fps')})}\n\n"
    except httpx.HTTPError as exc:
      yield f"data: {json.dumps({'error': str(exc)})}\n\n"

  return Response(generate(), mimetype="text/event-stream")


@app.route("/get_status", methods=["GET"])
def get_status():
  note_viewer_activity()
  if request.args.get("force") == "1":
    request_force_refresh(fetch_versions=True)
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
_service_jobs = {}
_service_jobs_lock = threading.Lock()


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


async def run_n_update(docker_command, docker_images, by_ip=None):
  by_ip = by_ip or group_by_server_ip(docker_images, cfg)
  tasks = {}

  for server_ip, names in by_ip.items():
    img_cfg = cfg[names[0]]
    url = edge_command_url(img_cfg, host_ip=server_ip)
    data = edge_host_command_payload(docker_command, names, cfg)
    print("Update request:", server_ip, names)
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


async def post_edge_command_async(url, data, read_timeout=20):
  if read_timeout is None:
    timeout = None
  else:
    timeout = httpx.Timeout(connect=10, read=read_timeout, write=10, pool=10)
  async with httpx.AsyncClient(timeout=timeout) as client:
    response = await client.post(url, data=data)
    return response.content.decode("utf-8")


if __name__ == "__main__":
  load_cfg()
  start_collector()
  app.run(host="0.0.0.0", port=5503)
