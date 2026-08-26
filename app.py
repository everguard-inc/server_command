"""Edge agent: pipeline control and status probes.

Runs on each edge host via server_command_client.service (setup_client.py).
Imports shared helpers from servers_cfg.py (deployed with the repo); does not
import proxy_server or read servers.json. Pipeline paths and service names arrive
in POST /command JSON from the proxy.

Runs as the edge service user; systemctl uses passwordless sudo (setup_client.py).
Git/docker commands run under the service user's home ($HOME).
HTTPS GitHub: ~/.git-credentials, ~/.eg/github_token, or GITHUB_TOKEN.
Optional: eg_basics.utils for config include merge and password decrypt.
"""

import base64
import getpass
import json
import os
import random
import re
import shlex
import shutil
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from urllib.parse import urlparse

import httpx
from flask import Flask, jsonify, request

from servers_cfg import (
    ALT_STREAM_FEED_PATH,
    DEFAULT_STREAM_FEED_PATH,
    SKIP_STREAM_HOSTS,
    SYS_MONITOR_SERVICE,
    apply_sys_monitor_peer_status,
    empty_plc_tag_metrics,
    fetch_plc_tag_metrics,
    empty_camera_drift_metrics,
    fetch_camera_drift_metrics,
    fetch_plc_cv_checker_metrics,
    finalize_pipeline_status,
    is_sys_monitored_peer,
    normalize_edge_probe,
    plc_status_url_for,
    plc_cv_checkers_url_for,
    drift_service_url_for,
    repo_path_for_pipeline,
    stream_feed_paths_for,
    tcp_reachable,
)
from git_versions import DEFAULT_GIT_VERSION_TIMEOUT, read_local_repo_versions

app = Flask(__name__)

# Paths & services (edge-local; proxy normally sends sys_monitor_* in POST /command).
EDGE_HOME = os.path.expanduser("~")
EDGE_USER = getpass.getuser()
GITHUB_TOKEN_FILE = os.path.join(EDGE_HOME, ".eg", "github_token")
RUNTIME_GIT_CREDENTIALS = os.path.join(EDGE_HOME, ".eg", ".git-credentials.runtime")
DOCKER_DATA_MOUNT = "/app/data"

# HTTP probe timeouts
PROBE_TIMEOUT = 5
SPEAKER_PROBE_TIMEOUT = httpx.Timeout(connect=1.5, read=6.0, write=1.0, pool=1.0)
STREAM_HEALTH_TIMEOUT = 3
STREAM_SSE_TIMEOUT = 8
CAMERA_PROBE_TIMEOUT = 2.0
CAMERA_PROBE_CACHE_SEC = 15
SPEAKER_STATUS_PATH = "media.cgi?msubmenu=speakerstatus&action=view"
PROBE_SSE_MAX_LINES = 10
QLIGHT_TYPE_MARKERS = ("qlight", "patlite")
SPEAKER_TYPE_MARKERS = ("simpleurlalarm", "simplerestalarm")

# Docker & service control
DOCKER_CMD_TIMEOUT = 10
SERVICE_CONTROL_TIMEOUT = 180
SERVICE_CONTROL_POLL = 2.0
RESTART_CONTAINER_STOP_TIMEOUT = 15
_SYS_MONITOR_BUILD_IMAGES = ("eg/basics:latest", "eg/sys:latest")

_GIT_PULL_RECOVERABLE = (
    "refusing to merge unrelated histories",
    "have diverged",
    "cannot lock ref",
    "not possible to fast-forward",
    "non-fast-forward",
)

_GIT_PULL_NO_UPSTREAM = (
    "no tracking information",
    "please specify which branch you want to merge with",
)

_GIT_PULL_BRANCH_FALLBACKS = ("forklift_proximity", "master", "main")

# Status collection
EDGE_PROBE_WORKERS = 10
RTLS_DEVICE_PROBE_WORKERS = 8
HOST_STATUS_DEADLINE = 45
VERSION_FETCH_WORKERS = 4
GIT_VERSION_TIMEOUT = DEFAULT_GIT_VERSION_TIMEOUT

MEMORY_UNIT_BYTES = {
    "B": 1,
    "KIB": 1024,
    "MIB": 1024 ** 2,
    "GIB": 1024 ** 3,
    "TIB": 1024 ** 4,
}

# Credential decrypt
AES_BLOCK_SIZE = 16
ZERO_IV_HEX = "00" * AES_BLOCK_SIZE

_probe_cache_lock = threading.Lock()
_stream_probe_cache = {}
_camera_probe_cache = {}
_docker_stats_cache = {"at": 0.0, "map": {}}

_update_lock = threading.Lock()
_update_pruning = False
_update_state = {
    "running": False,
    "step": "idle",
    "step_label": "",
    "started_at": None,
    "updated_at": None,
    "error": None,
    "service_names": [],
    "service_steps": {},
}


def _docker_cmd(*args, timeout=DOCKER_CMD_TIMEOUT, text=False, **kwargs):
    return subprocess.run(
        ["docker", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        text=text,
        **kwargs,
    )


def _cmd_detail(result):
    return (result.stderr or result.stdout or "").strip()


# --- container config ---

def _container_dir_candidates():
    dirs = []
    env_dir = os.environ.get("CONTAINER_DIR")
    if env_dir:
        dirs.append(env_dir)
    dirs.append(os.path.join(EDGE_HOME, "containers"))
    dirs.append("/containers")
    yield from _dedupe_preserve_order(dirs, skip_falsy=True)


def _norm_alarm_url(url):
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.netloc:
        return url.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def _load_config_json(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        return cfg
    try:
        from eg_basics.utils import read_include_json
        needs_include = cfg.get("include") and not cfg.get("camera") and not cfg.get("environment_alert")
        if needs_include:
            cfg = read_include_json(cfg, root_path=os.path.dirname(path) or "")
    except ImportError:
        pass
    return cfg


def _config_passkey(cfg, *, config_dir=None):
    passkey = cfg.get("passkey")
    if passkey:
        return passkey
    passkey_file = cfg.get("passkey_file")
    if not isinstance(passkey_file, str) or not passkey_file.strip():
        return None
    value = passkey_file.strip()
    paths = [value]
    if config_dir and not os.path.isabs(value):
        paths.insert(0, os.path.join(config_dir, value))
    for path in paths:
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read().strip()
                if text:
                    return text
            except OSError:
                break
    return value


def _collect_rtls_devices(cfg):
    env_alert = cfg.get("environment_alert")
    if not env_alert:
        return None, None, [], []

    if isinstance(env_alert, list):
        zone_alarm_map = {"_": env_alert}
    elif isinstance(env_alert, dict):
        zone_alarm_map = env_alert.get("zone_alarm_map") or {}
    else:
        zone_alarm_map = {}

    qlights = {}
    speakers = {}
    for alarms in zone_alarm_map.values():
        if not isinstance(alarms, list):
            continue
        for alarm in alarms:
            alarm_type = (alarm.get("type") or "").lower()
            alarm_url = _norm_alarm_url(alarm.get("url"))
            if not alarm_url:
                continue
            if any(marker in alarm_type for marker in QLIGHT_TYPE_MARKERS):
                qlights[alarm_url] = alarm_url
            elif any(marker in alarm_type for marker in SPEAKER_TYPE_MARKERS):
                speakers[alarm_url] = {
                    "url": alarm_url,
                    "user": alarm.get("user") or "",
                    "password": alarm.get("password") or "",
                    "auth": (alarm.get("auth") or "digest").lower(),
                }

    qlight_probes = sorted(qlights.values())
    speaker_probes = [speakers[key] for key in sorted(speakers)]
    qlight_set = len(qlight_probes) or None
    speaker_set = len(speaker_probes) or None
    return qlight_set, speaker_set, qlight_probes, speaker_probes


def _url_hostname(url):
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"http://{url}")
    return parsed.hostname or url


def _url_display_label(url):
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = parsed.hostname or url
    if parsed.port and parsed.port not in (80, 443):
        return f"{host}:{parsed.port}"
    return host


def _device_ip_link(url):
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"http://{url}")
    if not parsed.hostname:
        return ""
    netloc = parsed.hostname
    if parsed.port and parsed.port not in (80, 443):
        netloc = f"{parsed.hostname}:{parsed.port}"
    scheme = parsed.scheme or "http"
    return f"{scheme}://{netloc}"


def _url_host_port(url):
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = parsed.hostname or ""
    if not host:
        return "", None
    if parsed.port:
        port = parsed.port
    elif parsed.scheme == "https":
        port = 443
    else:
        port = 80
    return host, port


def _camera_links_from_cfg(cfg):
    cameras = cfg.get("camera")
    if not isinstance(cameras, list):
        return []
    links = []
    for cam in cameras:
        if not isinstance(cam, dict):
            continue
        url = cam.get("url", "")
        if not url:
            continue
        host = _url_hostname(url)
        if not host:
            continue
        links.append({
            "url": _device_ip_link(url),
            "label": host,
            "title": cam.get("name") or host,
        })
    return links


def _extract_camera_meta(cfg):
    cameras = cfg.get("camera")
    return {
        "cameras_set": len(cameras) if isinstance(cameras, list) else None,
        "streaming_port": cfg.get("streaming_port"),
        "streaming_ip": cfg.get("streaming_ip"),
        "camera_links": _camera_links_from_cfg(cfg),
    }


def _resolve_container_name(server_id, running_names, is_rtls):
    if is_rtls:
        return _matching_container_name(server_id, "rtls", running_names, loose_match=True)
    return server_id if server_id in running_names else None


def _docker_mount_source(container_name):
    if not container_name:
        return None
    result = _docker_cmd(
        "inspect", "-f",
        f"{{{{range .Mounts}}}}{{{{if eq .Destination \"{DOCKER_DATA_MOUNT}\"}}}}{{{{.Source}}}}{{{{end}}}}{{{{end}}}}",
        container_name,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _find_json_in_dir(dirpath, server_id, is_rtls):
    if not dirpath or not os.path.isdir(dirpath):
        return None

    preferred = [f"{server_id}_rtls.json", f"{server_id}.json"] if is_rtls else [f"{server_id}.json"]
    for filename in preferred:
        path = os.path.join(dirpath, filename)
        if os.path.isfile(path):
            return path

    for filename in sorted(os.listdir(dirpath)):
        if not filename.endswith(".json"):
            continue
        if server_id in filename or (is_rtls and filename.endswith("_rtls.json")):
            return os.path.join(dirpath, filename)
    return None


def _match_config_by_server_id(mount_source, server_id):
    if not mount_source or not os.path.isdir(mount_source):
        return None
    for filename in sorted(os.listdir(mount_source)):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(mount_source, filename)
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict) and cfg.get("server_id") == server_id:
                return path
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
    return None


def _find_container_config_path(server_id, is_rtls, running_names):
    container_name = _resolve_container_name(server_id, running_names, is_rtls)
    if container_name:
        mount_source = _docker_mount_source(container_name)
        if mount_source:
            path = _find_json_in_dir(mount_source, server_id, is_rtls)
            if path:
                return path

    for name in sorted(running_names):
        if is_rtls and not name.endswith("_rtls"):
            continue
        if not is_rtls and name.endswith("_rtls"):
            continue
        mount_source = _docker_mount_source(name)
        if not mount_source:
            continue
        path = _match_config_by_server_id(mount_source, server_id)
        if path:
            return path

    for base_dir in _container_dir_candidates():
        path = _find_json_in_dir(os.path.join(base_dir, server_id), server_id, is_rtls)
        if path:
            return path
    return None


def load_runtime_meta(item, running_names):
    if item.get("is_kafka"):
        return dict(item)
    server_id = item.get("server_id")
    is_rtls = item.get("is_rtls", False)
    if not server_id:
        return dict(item)

    config_path = _find_container_config_path(server_id, is_rtls, running_names)
    if not config_path:
        merged = dict(item)
        if is_rtls:
            merged["rtls_config_missing"] = True
        return merged

    cfg = _load_config_json(config_path)
    merged = dict(item)
    merged["config_path"] = config_path
    config_dir = os.path.dirname(config_path)

    if is_rtls:
        ql_set, sp_set, ql_probes, sp_probes = _collect_rtls_devices(cfg)
        merged.update({
            "qlight_set": ql_set,
            "speaker_set": sp_set,
            "qlight_probes": ql_probes,
            "speaker_probes": sp_probes,
            "passkey": _config_passkey(cfg, config_dir=config_dir) or item.get("passkey"),
        })
        if ql_set is None and sp_set is None:
            merged["rtls_devices_missing"] = True
    else:
        merged.update(_extract_camera_meta(cfg))

    return merged


# --- credential decrypt ---

def _read_secret_key(passkey):
    if not passkey:
        return None
    if isinstance(passkey, bytes):
        passkey = passkey.decode("utf-8")
    try:
        return base64.b85decode(passkey).decode("utf-8")
    except Exception:
        return None


def _shuffle_back_bytes(data, seed):
    buf = bytearray(data)
    order = list(range(len(data)))
    random.Random(seed).shuffle(order)
    for new_i, old_i in enumerate(order):
        buf[old_i] = data[new_i]
    return bytes(buf)


def _decrypt_with_openssl(encrypted, key):
    payload = encrypted.encode("utf-8") if isinstance(encrypted, str) else encrypted
    proc = subprocess.run(
        [
            "openssl", "enc", "-d", "-aes-128-ofb", "-nopad",
            "-K", key.encode("utf-8").hex(),
            "-iv", ZERO_IV_HEX,
        ],
        input=base64.b64decode(payload),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace"))

    buf = bytearray(proc.stdout)
    length = struct.unpack("1i", buf[0:4])[0]
    if length > 0:
        return buf[4:length + 4].decode("utf-8")

    length = struct.unpack("1h", buf[4:6])[0]
    seed = struct.unpack("1h", buf[6:8])[0]
    return _shuffle_back_bytes(buf[8:length + 8], seed).decode("utf-8")


def _decrypt_password(encrypted, passkey):
    if not encrypted or not passkey:
        return encrypted
    try:
        from eg_basics.utils import decrypt, read_secret_key
        key = read_secret_key(passkey)
        if key:
            return decrypt(encrypted, key)
    except Exception:
        pass
    try:
        key = _read_secret_key(passkey)
        if key:
            return _decrypt_with_openssl(encrypted, key)
    except Exception:
        pass
    return encrypted


def _resolve_passkey(passkey=None):
    return passkey or os.environ.get("PASSKEY") or os.environ.get("EG_PASSKEY")


# --- HTTP probes ---

def _join_url(base_url, path):
    return str(httpx.URL(base_url.rstrip("/") + "/").join(path.lstrip("/")))


def _auth_for(user, password, auth):
    if not user or not password:
        return None
    if auth == "digest":
        return httpx.DigestAuth(user, password)
    return httpx.BasicAuth(user, password)


def _camera_count_from_sse(line):
    if not line.startswith("data:"):
        return None
    payload = json.loads(line[5:].strip())
    camera_ids = payload.get("camera_ids")
    if isinstance(camera_ids, list) and camera_ids:
        return len(camera_ids)
    jpeg = payload.get("jpeg")
    if isinstance(jpeg, list):
        return len(jpeg)
    # PLC /stream often sends one jpeg blob plus CamN labels in states.
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
            return len(cams)
    if isinstance(jpeg, str) and jpeg:
        return 1
    return None


def _dedupe_preserve_order(items, skip_falsy=False):
    seen = set()
    for item in items:
        if skip_falsy and not item:
            continue
        if item not in seen:
            seen.add(item)
            yield item


def _stream_probe_hosts(item):
    hosts = []
    for key in ("streaming_ip", "server_ip"):
        ip = (item or {}).get(key)
        if ip and ip not in SKIP_STREAM_HOSTS:
            hosts.append(ip)
    hosts.extend(["127.0.0.1", "localhost"])
    return list(_dedupe_preserve_order(hosts))


def _probe_stream_at_host(host, port, feed_paths=None):
    """Probe SSE feed paths. /health is optional (PLC has feed without /health)."""
    base = f"http://{host}:{port}"
    paths = feed_paths or (DEFAULT_STREAM_FEED_PATH, ALT_STREAM_FEED_PATH)
    for path in paths:
        try:
            with httpx.Client(timeout=STREAM_SSE_TIMEOUT) as client:
                with client.stream(
                    "GET",
                    f"{base}{path}",
                    headers={"Accept": "text/event-stream"},
                ) as resp:
                    if resp.status_code != 200:
                        continue
                    for idx, line in enumerate(resp.iter_lines()):
                        if idx >= PROBE_SSE_MAX_LINES:
                            break
                        if not line:
                            continue
                        count = _camera_count_from_sse(line.strip())
                        if count is not None:
                            return True, count
            return True, None
        except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError):
            continue

    try:
        resp = httpx.get(f"{base}/health", timeout=STREAM_HEALTH_TIMEOUT)
        if resp.status_code == 200:
            return True, None
    except httpx.HTTPError:
        pass
    return False, None


def _probe_local_stream(port, hosts=None, feed_paths=None):
    if not port:
        return False, None
    for host in hosts or ["127.0.0.1"]:
        health_ok, cameras_now = _probe_stream_at_host(host, port, feed_paths)
        if health_ok:
            return True, cameras_now
    return False, None


def probe_qlight(url):
    """Check QLight reachability via TCP and Q-Light homepage."""
    host, port = _url_host_port(url)
    if not host:
        return False

    if not tcp_reachable(host, port, timeout=PROBE_TIMEOUT):
        return False

    try:
        resp = httpx.get(url, timeout=PROBE_TIMEOUT)
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    body = (resp.text or "").lower()
    return "q-light" in body or "tower lamp" in body


def _parse_speaker_status(body):
    text = (body or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            status = data.get("Status", data.get("status"))
            if status is not None:
                return status
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    match = re.search(r"<Status[^>]*>([^<]+)</Status>", text, re.I)
    return match.group(1).strip() if match else None


def probe_speaker(probe, passkey=None):
    if isinstance(probe, str):
        probe = {"url": probe}
    url = _join_url(probe.get("url", ""), SPEAKER_STATUS_PATH)
    if not url:
        return False

    user = probe.get("user") or ""
    password = probe.get("password") or ""
    auth = (probe.get("auth") or "digest").lower()
    if password:
        password = _decrypt_password(password, _resolve_passkey(passkey))

    try:
        resp = httpx.get(
            url,
            auth=_auth_for(user, password, auth),
            timeout=SPEAKER_PROBE_TIMEOUT,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return False
        return _parse_speaker_status(resp.text) is not None
    except httpx.HTTPError:
        return False


def _rtls_device_links(qlight_probes, speaker_probes):
    qlight_links = [
        {"url": _device_ip_link(url), "label": _url_display_label(url), "title": _url_display_label(url)}
        for url in qlight_probes
        if _url_hostname(url)
    ]
    speaker_links = [
        {
            "url": _device_ip_link(probe.get("url", "")),
            "label": _url_display_label(probe.get("url", "")),
            "title": _url_display_label(probe.get("url", "")),
        }
        for probe in speaker_probes
        if _url_hostname(probe.get("url", ""))
    ]
    return qlight_links, speaker_links


def _camera_link_port(url, default=554):
    if not url:
        return default
    parsed = urlparse(url if "://" in url else f"rtsp://{url}")
    return parsed.port or default


def _probe_one_camera_link(link, *, timeout=CAMERA_PROBE_TIMEOUT):
    host = link.get("label") or _url_hostname(link.get("url", ""))
    if not host:
        return False
    port = _camera_link_port(link.get("url", ""))
    key = (host, port)
    now = time.time()
    with _probe_cache_lock:
        cached = _camera_probe_cache.get(key)
        if cached and now - cached[0] < CAMERA_PROBE_CACHE_SEC:
            return cached[1]
    ok = tcp_reachable(host, port, timeout=timeout)
    with _probe_cache_lock:
        _camera_probe_cache[key] = (now, ok)
    return ok


def _probe_camera_links(camera_links, *, timeout=CAMERA_PROBE_TIMEOUT):
    if not camera_links:
        return [], 0
    statuses = ["err"] * len(camera_links)
    workers = min(len(camera_links), 4)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_probe_one_camera_link, link, timeout=timeout): idx
            for idx, link in enumerate(camera_links)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                statuses[idx] = "ok" if future.result() else "err"
            except Exception:
                statuses[idx] = "err"
    cameras_now = sum(1 for status in statuses if status == "ok")
    return statuses, cameras_now


def probe_rtls_devices(qlight_probes, speaker_probes, passkey=None):
    qlight_probes = list(qlight_probes or [])
    speaker_probes = list(speaker_probes or [])
    qlight_status = [False] * len(qlight_probes)
    speaker_status = [False] * len(speaker_probes)
    if not qlight_probes and not speaker_probes:
        return None, None, [], []

    workers = min(RTLS_DEVICE_PROBE_WORKERS, len(qlight_probes) + len(speaker_probes))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for idx, url in enumerate(qlight_probes):
            futures[pool.submit(probe_qlight, url)] = ("qlight", idx)
        for idx, probe in enumerate(speaker_probes):
            futures[pool.submit(probe_speaker, probe, passkey)] = ("speaker", idx)
        for future in as_completed(futures):
            kind, idx = futures[future]
            try:
                ok = bool(future.result())
            except Exception:
                ok = False
            if kind == "qlight":
                qlight_status[idx] = ok
            else:
                speaker_status[idx] = ok

    qlight_now = sum(qlight_status) if qlight_status else None
    speaker_now = sum(speaker_status) if speaker_status else None
    return qlight_now, speaker_now, qlight_status, speaker_status


# --- docker runtime ---

def _running_container_names():
    result = _docker_cmd("ps", text=True)
    names = set()
    for line in (result.stdout or "").splitlines()[1:]:
        line = line.strip()
        if line:
            names.add(line.split()[-1])
    return names


# --- edge status probes ---

def _container_memory_map():
    result = _docker_cmd("stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}", text=True)
    if result.returncode != 0:
        return {}
    memory = {}
    for line in (result.stdout or "").splitlines():
        if "\t" not in line:
            continue
        name, usage = line.split("\t", 1)
        name = name.strip()
        usage = usage.strip()
        if name and usage:
            memory[name] = usage
    return memory


def _container_memory_map_cached(interval_sec):
    if interval_sec <= 0:
        return _container_memory_map()
    now = time.time()
    with _probe_cache_lock:
        cached_at = _docker_stats_cache["at"]
        cached_map = _docker_stats_cache["map"]
        if cached_map and (now - cached_at) < interval_sec:
            return dict(cached_map)
    mem_map = _container_memory_map()
    with _probe_cache_lock:
        _docker_stats_cache["at"] = now
        _docker_stats_cache["map"] = dict(mem_map)
    return mem_map


def _invalidate_probe_caches():
    with _probe_cache_lock:
        _stream_probe_cache.clear()
        _docker_stats_cache["at"] = 0.0
        _docker_stats_cache["map"] = {}


def _stream_probe_cache_key(item):
    return (
        item.get("name"),
        item.get("streaming_port"),
        tuple(_stream_probe_hosts(item)),
        tuple(stream_feed_paths_for(name=item.get("name"), cfg=item)),
    )


def _cached_probe_local_stream(item, interval_sec):
    port = item.get("streaming_port") or 5000
    hosts = _stream_probe_hosts(item)
    feed_paths = stream_feed_paths_for(name=item.get("name"), cfg=item)
    if interval_sec <= 0:
        return _probe_local_stream(port, hosts, feed_paths)

    key = _stream_probe_cache_key(item)
    now = time.time()
    with _probe_cache_lock:
        cached = _stream_probe_cache.get(key)
        if cached and (now - cached[0]) < interval_sec:
            return cached[1], cached[2]

    health_ok, cameras_now = _probe_local_stream(port, hosts, feed_paths)
    with _probe_cache_lock:
        _stream_probe_cache[key] = (now, health_ok, cameras_now)
    return health_ok, cameras_now


def _matching_camera_drift_container(running_names, mem_map=None):
    """Resolve Camera-Drift docker name (e.g. camera_drift_service_optimized).

    server_id is a UUID and does not match the container; systemd eg_camera_drift
    only tracks the wrapper, so memory must come from the drift container itself.
    """
    names = [
        name for name in running_names
        if name and "camera_drift" in name.lower()
    ]
    if not names:
        return None
    if len(names) == 1:
        return names[0]
    if mem_map:
        def used_bytes(name):
            usage = mem_map.get(name) or ""
            used_s = usage.split("/", 1)[0].strip()
            return _parse_memory_size(used_s) or 0.0
        return max(names, key=used_bytes)
    preferred = [name for name in names if "service" in name.lower()]
    return max(preferred or names, key=len)


def _container_name_for_item(item, running_names, mem_map=None):
    if item.get("is_kafka"):
        return None
    explicit = (item.get("container_name") or "").strip()
    if explicit and explicit in running_names:
        return explicit
    if item.get("is_camera_drift"):
        return _matching_camera_drift_container(running_names, mem_map)
    server_id = item.get("server_id")
    if item.get("is_sys_monitor"):
        return _matching_container_name(server_id, "sys", running_names)
    if item.get("is_rtls"):
        return _matching_container_name(
            server_id, "rtls", running_names, loose_match=True,
        )
    if not server_id:
        return None
    return server_id if server_id in running_names else None


def _parse_memory_size(text):
    match = re.match(
        r"^([\d.]+)\s*(B|KiB|MiB|GiB|TiB)?$",
        (text or "").strip(),
        re.I,
    )
    if not match:
        return None
    try:
        amount = float(match.group(1))
    except (TypeError, ValueError):
        return None
    unit = (match.group(2) or "B").upper()
    factor = MEMORY_UNIT_BYTES.get(unit)
    if factor is None:
        return None
    return amount * factor


def _mem_usage_percent(usage):
    if not usage or "/" not in usage:
        return None
    used_s, total_s = [part.strip() for part in usage.split("/", 1)]
    used = _parse_memory_size(used_s)
    total = _parse_memory_size(total_s)
    if not used or not total:
        return None
    return min(100.0, (used / total) * 100.0)


def _format_bytes_iec(num_bytes):
    try:
        value = float(num_bytes)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    for unit, factor in (
        ("TiB", 1024 ** 4),
        ("GiB", 1024 ** 3),
        ("MiB", 1024 ** 2),
        ("KiB", 1024),
        ("B", 1),
    ):
        if value >= factor or unit == "B":
            if unit == "B":
                return f"{int(value)}B"
            scaled = value / factor
            if scaled >= 100:
                return f"{scaled:.1f}{unit}"
            return f"{scaled:.2f}{unit}"
    return f"{int(value)}B"


def _host_mem_total_bytes():
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    return int(parts[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _proc_rss_bytes(pid):
    try:
        with open(f"/proc/{int(pid)}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _systemd_show_props(service_name, *props):
    if not service_name or not props:
        return {}
    # Use "-p Name" (not "-p=Name"): some systemd builds return empty stdout for equals form.
    args = ["show", service_name]
    for prop in props:
        args.extend(["-p", prop])
    result = _run_systemctl(*args)
    out = {}
    for line in (result.stdout or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    # Read-only show often works without sudo when passwordless sudo is limited.
    if not out:
        result = subprocess.run(
            ["systemctl", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for line in (result.stdout or "").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _systemd_memory_current_bytes(service_name):
    """Best-effort RSS/cgroup for systemd services (e.g. kafka-consumer)."""
    if not service_name:
        return None
    props = _systemd_show_props(service_name, "MemoryCurrent", "MainPID")
    raw = props.get("MemoryCurrent") or ""
    if raw.isdigit():
        current = int(raw)
        # unset / unlimited is often reported as max uint64
        if 0 < current < (2 ** 63):
            return current

    unit = service_name if service_name.endswith(".service") else f"{service_name}.service"
    for path in (
        f"/sys/fs/cgroup/system.slice/{unit}/memory.current",
        f"/sys/fs/cgroup/system.slice/{unit}/memory.usage_in_bytes",
    ):
        try:
            with open(path, encoding="utf-8") as f:
                current = int(f.read().strip())
            if current > 0:
                return current
        except (OSError, ValueError):
            continue

    pid = props.get("MainPID") or ""
    if pid.isdigit() and int(pid) > 0:
        return _proc_rss_bytes(pid)
    return None


def _systemd_memory_usage(service_name):
    current = _systemd_memory_current_bytes(service_name)
    if not current:
        return None
    used = _format_bytes_iec(current)
    if not used:
        return None
    total_bytes = _host_mem_total_bytes()
    if not total_bytes:
        return used
    total = _format_bytes_iec(total_bytes)
    if not total:
        return used
    return f"{used} / {total}"


def _apply_container_memory(entry, item, mem_map):
    if item.get("is_kafka"):
        # Host MemTotal makes % misleading for WARN; show usage string only.
        usage = _systemd_memory_usage(_pipeline_service_name(item))
        entry["mem_usage"] = usage
        entry["mem_usage_percent"] = None
        return
    container = _container_name_for_item(item, set(mem_map), mem_map)
    usage = mem_map.get(container) if container else None
    if usage:
        entry["mem_usage"] = usage
        entry["mem_usage_percent"] = _mem_usage_percent(usage)
        return
    # Fallback when no matching docker container (wrapper unit only).
    service = _pipeline_service_name(item)
    if service and _is_service_active(service):
        entry["mem_usage"] = _systemd_memory_usage(service)
        entry["mem_usage_percent"] = None
        return
    entry["mem_usage"] = None
    entry["mem_usage_percent"] = None


def _is_service_active(service_name):
    if not service_name:
        return False
    return _run_systemctl("is-active", "--quiet", service_name).returncode == 0


def _docker_image_exists(image_ref):
    result = _docker_cmd("images", "-q", image_ref, text=True)
    return bool((result.stdout or "").strip())


def _require_docker_images(*image_refs):
    missing = [ref for ref in image_refs if not _docker_image_exists(ref)]
    if missing:
        raise RuntimeError(f"docker image(s) not found after build: {', '.join(missing)}")


def _matching_container_name(server_id, suffix, running_names, *, loose_match=False):
    if not server_id:
        return None
    primary = f"{server_id}_{suffix}"
    if primary in running_names:
        return primary
    if not loose_match:
        return None
    token = f"_{suffix}"
    for name in running_names:
        if name.endswith(token) and server_id in name:
            return name
    return None


def _is_container_running(server_id, suffix, running_names, *, loose_match=False):
    return _matching_container_name(
        server_id, suffix, running_names, loose_match=loose_match
    ) is not None


def _container_running_for_kind(server_id, running_names, *, is_rtls=False, is_sys_monitor=False):
    if is_sys_monitor:
        return _is_container_running(server_id, "sys", running_names)
    if is_rtls:
        return _is_container_running(server_id, "rtls", running_names, loose_match=True)
    return bool(server_id and server_id in running_names)


def _pipeline_service_name(item):
    return item.get("service_name") or ""


def _kafka_unit_running(service_name, server_id=None, running_names=None):
    """Kafka runs as a systemd unit (optional docker name fallback)."""
    if service_name and _is_service_active(service_name):
        return True
    names = running_names if running_names is not None else set()
    if service_name and service_name in names:
        return True
    return bool(server_id and server_id in names)


def _is_pipeline_running(item, running_names):
    server_id = item.get("server_id")
    service_name = _pipeline_service_name(item)
    if item.get("is_sys_monitor") or item.get("is_rtls"):
        return _is_service_active(service_name) or _container_running_for_kind(
            server_id,
            running_names,
            is_rtls=bool(item.get("is_rtls")),
            is_sys_monitor=bool(item.get("is_sys_monitor")),
        )
    if item.get("is_kafka"):
        return _kafka_unit_running(service_name, server_id, running_names)
    if server_id and server_id in running_names:
        return True
    # Non-Docker pipelines (Camera-Drift etc.): honor systemd unit.
    return bool(service_name and _is_service_active(service_name))


# --- pipeline control ---

def run_pipeline(cfg):
    if not all(k in cfg for k in ("type", "json", "path")):
        return False
    for json_name in cfg["json"]:
        result = subprocess.run(
            ["./docker-run.sh", cfg["type"], json_name, "--opt", "background"],
            cwd=cfg["path"],
        )
        if result.returncode != 0:
            return False
        time.sleep(5)
    return True


def stop_pipeline(server_id):
    result = _docker_cmd("stop", server_id)
    return result.returncode == 0


# --- service control ---

def _systemctl_cmd(*args):
    sudo = shutil.which("sudo")
    if sudo:
        return [sudo, "-n", "systemctl", *args]
    return ["systemctl", *args]


def _is_pipeline_container_running(server_id, is_rtls=False, is_sys_monitor=False):
    if not server_id:
        return False
    return _container_running_for_kind(
        server_id, _running_container_names(), is_rtls=is_rtls, is_sys_monitor=is_sys_monitor,
    )


def _pipeline_container_name(server_id, *, is_rtls=False, is_sys_monitor=False):
    if not server_id:
        return None
    running_names = _running_container_names()
    if is_sys_monitor:
        return _matching_container_name(server_id, "sys", running_names)
    if is_rtls:
        return _matching_container_name(server_id, "rtls", running_names, loose_match=True)
    if server_id in running_names:
        return server_id
    return None


def _force_remove_pipeline_container(server_id, *, is_rtls=False, is_sys_monitor=False):
    name = _pipeline_container_name(server_id, is_rtls=is_rtls, is_sys_monitor=is_sys_monitor)
    if not name:
        return
    result = _docker_cmd("rm", "-f", name, text=True)
    if result.returncode != 0:
        detail = _cmd_detail(result)
        if detail:
            print(f"docker rm -f {name}: {detail}")


def _is_pipeline_under_control(service_name, server_id=None, is_rtls=False, is_sys_monitor=False):
    if server_id and _is_pipeline_container_running(server_id, is_rtls, is_sys_monitor):
        return True
    return _is_service_active(service_name)


def _wait_until(predicate, timeout, poll_interval=SERVICE_CONTROL_POLL):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_interval)
    return predicate()


def _wait_for_service_active(service_name, timeout, poll_interval=SERVICE_CONTROL_POLL):
    return _wait_until(lambda: _is_service_active(service_name), timeout, poll_interval)


def _wait_for_pipeline_container(
    server_id, is_rtls, want_running, timeout, poll_interval=SERVICE_CONTROL_POLL, *,
    is_sys_monitor=False,
):
    if not server_id:
        return False
    return _wait_until(
        lambda: _is_pipeline_container_running(server_id, is_rtls, is_sys_monitor) == want_running,
        timeout,
        poll_interval,
    )


def _wait_for_pipeline_ready(
    service_name, server_id=None, is_rtls=False, is_sys_monitor=False, timeout=SERVICE_CONTROL_TIMEOUT,
):
    if is_sys_monitor:
        return _wait_for_service_active(service_name, timeout)
    if server_id:
        return _wait_for_pipeline_container(server_id, is_rtls, True, timeout)
    return _wait_for_service_active(service_name, timeout)


def _run_systemctl(*args):
    return subprocess.run(
        _systemctl_cmd(*args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _systemctl_fail_message(result):
    detail = _cmd_detail(result)
    return f"Failed: {detail}" if detail else "Failed"


def service_pipeline(
    command, service_name, *, server_id=None, is_rtls=False, is_sys_monitor=False, is_kafka=False,
):
    if is_kafka:
        server_id = None
    running = _is_pipeline_under_control(service_name, server_id, is_rtls, is_sys_monitor)
    if is_kafka and not running:
        running = _is_service_active(service_name)

    if command == "start":
        if running:
            return "Already running"
        result = _run_systemctl("start", service_name)
        if result.returncode != 0:
            return _systemctl_fail_message(result)
        if not _wait_for_pipeline_ready(service_name, server_id, is_rtls, is_sys_monitor):
            return "Start timed out"
        return "Succeed"
    if command == "stop":
        if not running:
            return "Already stopped"
        result = _run_systemctl("stop", service_name)
        if result.returncode != 0:
            return _systemctl_fail_message(result)
        if server_id:
            _wait_for_pipeline_container(
                server_id, is_rtls, False, SERVICE_CONTROL_TIMEOUT, is_sys_monitor=is_sys_monitor,
            )
        return "Succeed"
    if command == "restart":
        if server_id:
            if running or _is_pipeline_container_running(server_id, is_rtls, is_sys_monitor):
                stop_result = _run_systemctl("stop", service_name)
                if stop_result.returncode != 0:
                    detail = _cmd_detail(stop_result)
                    print(f"{service_name}: stop before restart failed: {detail or stop_result.returncode}")
            _force_remove_pipeline_container(
                server_id, is_rtls=is_rtls, is_sys_monitor=is_sys_monitor,
            )
            _wait_for_pipeline_container(
                server_id, is_rtls, False, RESTART_CONTAINER_STOP_TIMEOUT,
                is_sys_monitor=is_sys_monitor,
            )
            result = _run_systemctl("start", service_name)
            if result.returncode != 0:
                return _systemctl_fail_message(result)
            if not _wait_for_pipeline_ready(service_name, server_id, is_rtls, is_sys_monitor):
                return "Start timed out"
            return "Succeed"
        result = _run_systemctl("restart", service_name)
        if result.returncode != 0:
            return _systemctl_fail_message(result)
        return "Succeed"
    if command == "status":
        result = _run_systemctl("is-active", service_name)
        state = (result.stdout or "").strip()
        if state:
            return state
        return _systemctl_fail_message(result)
    result = _run_systemctl(command, service_name)
    if result.returncode != 0:
        return _systemctl_fail_message(result)
    return "Succeed"


def _restart_service_or_raise(
    service_name, *, server_id=None, is_rtls=False, is_sys_monitor=False, is_kafka=False,
    wait_active=False,
):
    print("Restarting service:", service_name)
    result = service_pipeline(
        "restart",
        service_name,
        server_id=server_id,
        is_rtls=is_rtls,
        is_sys_monitor=is_sys_monitor,
        is_kafka=is_kafka,
    )
    if result not in ("Succeed", "Already running"):
        raise RuntimeError(f"{service_name}: {result}")
    if wait_active and not _wait_for_service_active(service_name, SERVICE_CONTROL_TIMEOUT):
        raise RuntimeError(f"{service_name} did not become active")


def _service_names_from_command(command):
    names = command.get("service_names") or []
    if not names and command.get("service_name"):
        names = [command["service_name"]]
    return names


def _sys_monitor_service_name(command):
    """Proxy update payload should include sys_monitor_service_name."""
    return (
        command.get("sys_monitor_service_name")
        or SYS_MONITOR_SERVICE
    )


def _was_pipeline_job_active(job):
    service_name = job.get("service_name")
    if not service_name:
        return False
    if _is_service_active(service_name):
        return True
    server_id = job.get("server_id")
    if not server_id:
        return False
    return _is_pipeline_container_running(server_id, is_rtls=bool(job.get("is_rtls")))


def _active_pipeline_service_names(pipeline_jobs):
    return {
        job["service_name"]
        for job in pipeline_jobs
        if _was_pipeline_job_active(job)
    }


# --- git ---

def _git_env():
    env = os.environ.copy()
    env["HOME"] = EDGE_HOME
    env["USER"] = EDGE_USER
    env["LOGNAME"] = EDGE_USER
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _docker_build_env():
    """docker-build.sh already supports IGNORE_CA=1 to skip sudo zip (non-interactive Update)."""
    env = _git_env()
    env["IGNORE_CA"] = "1"
    return env


def _try_remove_docker_images(*image_refs):
    for ref in image_refs:
        if not ref:
            continue
        result = _docker_cmd("image", "rm", "-f", ref, text=True)
        if result.returncode != 0:
            detail = _cmd_detail(result)
            if detail:
                print(f"docker image rm -f {ref}: {detail}")


def _run_docker_build(repo_path, label, *, pre_remove=()):
    for ref in pre_remove:
        _try_remove_docker_images(ref)
    _run_step(
        ["./docker-build.sh"],
        repo_path,
        f"{label}: docker build",
        env=_docker_build_env(),
    )


def _append_update_error(errors, service_name, exc, *, failed_label, summary=None):
    msg = summary or f"{service_name}: {exc}"
    errors.append(msg)
    _mark_service_step_failed(service_name, label=failed_label)
    print("update step failed:", msg)


def _docker_error_summary(result, max_lines=8):
    lines = [line.strip() for line in _cmd_detail(result).splitlines() if line.strip()]
    err_lines = [
        line for line in lines
        if re.search(r"\b(ERROR|error|failed|denied)\b", line, re.I)
    ]
    picked = err_lines if err_lines else lines
    return "\n".join(picked[-max_lines:])


def _git_cmd(repo, *args, timeout=None, use_credentials=False):
    cmd = ["git"]
    if use_credentials:
        cmd.extend(_git_config_args(repo))
    cmd.extend(["-C", repo, *args])
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "env": _git_env(),
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    return subprocess.run(cmd, **kwargs)


def _git_error_summary(result, max_lines=6):
    """Short git failure text for UI (skip branch-list noise from pull/fetch)."""
    lines = []
    for chunk in (result.stderr or "", result.stdout or ""):
        for raw in chunk.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith((" * [", " + ", " - ", "remote:", "From https://", "From git@")):
                continue
            lines.append(line)

    important = [
        line for line in lines
        if re.search(r"\b(fatal|error|hint):\b", line, re.I)
    ]
    picked = important if important else lines
    seen = set()
    uniq = []
    for line in picked:
        if line in seen:
            continue
        seen.add(line)
        uniq.append(line)
    return "\n".join(uniq[:max_lines])


_keyed_locks_guard = threading.Lock()
_git_sync_locks = {}
_repo_build_locks = {}


def _get_keyed_lock(registry, key):
    with _keyed_locks_guard:
        lock = registry.get(key)
        if lock is None:
            lock = threading.Lock()
            registry[key] = lock
        return lock


def _git_sync_lock(repo):
    return _get_keyed_lock(_git_sync_locks, repo)


def _repo_build_lock(repo_path):
    return _get_keyed_lock(_repo_build_locks, os.path.realpath(repo_path or ""))


def _raise_git_failure(label, result):
    if result.returncode == 0:
        return
    detail = _git_error_summary(result)
    raise RuntimeError(f"{label} failed (exit {result.returncode}): {detail}")


def _git_current_branch(repo):
    result = _git_cmd(repo, "rev-parse", "--abbrev-ref", "HEAD")
    branch = (result.stdout or "").strip()
    if result.returncode == 0 and branch and branch != "HEAD":
        return branch
    return "master"


def _git_working_tree_clean(repo):
    result = _git_cmd(repo, "status", "--porcelain")
    return result.returncode == 0 and not (result.stdout or "").strip()


def _git_output_text(result):
    return f"{result.stderr or ''}\n{result.stdout or ''}"


def _git_ref_exists(repo, ref):
    result = _git_run(repo, ["rev-parse", "--verify", ref])
    return result.returncode == 0


def _git_resolve_pull_branch(repo, label):
    branch = _git_current_branch(repo)
    fetch = _git_run(repo, ["fetch", "origin", "--prune"])
    if fetch.returncode != 0:
        _raise_git_failure(f"{label}: git fetch origin", fetch)

    if _git_ref_exists(repo, f"origin/{branch}"):
        return branch

    for fallback in _GIT_PULL_BRANCH_FALLBACKS:
        if fallback != branch and _git_ref_exists(repo, f"origin/{fallback}"):
            print(f"{label}: no upstream for {branch}; syncing to origin/{fallback}")
            return fallback

    raise RuntimeError(
        f"{label}: no upstream branch for {branch} and no fallback found on origin",
    )


def _git_sync_to_origin_with_stash(repo, label, branch, *, log_prefix=None, reset_notice=None):
    prefix = log_prefix or label
    stashed = False
    if not _git_working_tree_clean(repo):
        print(f"{prefix}: stashing local changes before sync")
        stashed = _git_stash_local_changes(repo, label)
    if reset_notice:
        print(reset_notice)
    _git_reset_to_origin(repo, label, branch)
    if stashed:
        print(f"{prefix}: restoring stashed local changes")
        _git_stash_pop(repo, label)


def _git_pull_no_upstream(repo, label, detail):
    lower = (detail or "").lower()
    if not any(marker in lower for marker in _GIT_PULL_NO_UPSTREAM):
        return False

    branch = _git_resolve_pull_branch(repo, label)
    _git_sync_to_origin_with_stash(
        repo, label, branch, log_prefix=f"{label}: git pull",
    )
    return True


def _git_pull_recoverable_result(result):
    lower = _git_output_text(result).lower()
    return any(marker in lower for marker in _GIT_PULL_RECOVERABLE)


def _edge_version_git_cmd(repo_path, *args, timeout=None, **_ignored):
    return _git_cmd(
        repo_path, *args,
        timeout=timeout or GIT_VERSION_TIMEOUT,
        use_credentials=True,
    )


def _read_repo_versions(repo_path):
    return read_local_repo_versions(
        repo_path,
        git_cmd=_edge_version_git_cmd,
        timeout=GIT_VERSION_TIMEOUT,
        include_remote=False,
    )


def _unique_repo_paths(items):
    return list(_dedupe_preserve_order(
        (repo_path_for_pipeline(item=item) for item in items),
        skip_falsy=True,
    ))


def _prefetch_repo_versions(items):
    paths = _unique_repo_paths(items)
    if not paths:
        return {}

    cache = {}
    workers = min(VERSION_FETCH_WORKERS, len(paths))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_read_repo_versions, path): path for path in paths}
        for future in as_completed(futures):
            path = futures[future]
            try:
                cache[path] = future.result()
            except Exception:
                cache[path] = (None, None, None, None)
    return cache


def _apply_repo_versions(entry, item, version_cache):
    repo_path = repo_path_for_pipeline(item=item)
    if not repo_path:
        return
    current, current_date, _latest, _latest_date = version_cache.get(
        repo_path, (None, None, None, None),
    )
    entry["version_current"] = current
    entry["version_current_date"] = current_date
    # Remote latest is filled by the central proxy.


def _step_failure_detail(label, result):
    if "docker build" in label.lower():
        return _docker_error_summary(result)
    if "git" in label.lower():
        return _git_error_summary(result)
    return _cmd_detail(result)


def _run_step(cmd, cwd, label, *, env=None):
    result = subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env or _git_env(),
    )
    if result.returncode != 0:
        detail = _step_failure_detail(label, result)
        if detail:
            raise RuntimeError(f"{label} failed (exit {result.returncode}): {detail}")
        raise RuntimeError(f"{label} failed (exit {result.returncode})")
    return result


def _assert_git_repo(path, label):
    repo = os.path.realpath(path)
    if not os.path.isdir(repo):
        raise RuntimeError(f"{label}: directory not found: {path}")
    if not os.path.isdir(os.path.join(repo, ".git")):
        raise RuntimeError(f"{label}: not a git repository: {path}")
    if not os.access(repo, os.R_OK | os.W_OK | os.X_OK):
        raise RuntimeError(f"{label}: cannot access {path}")
    return repo


def _git_credentials_file():
    creds = os.path.join(EDGE_HOME, ".git-credentials")
    return creds if os.path.isfile(creds) else ""


def _github_token():
    if os.path.isfile(GITHUB_TOKEN_FILE):
        with open(GITHUB_TOKEN_FILE, encoding="utf-8") as f:
            token = f.read().strip()
            if token:
                return token
    for name in ("GITHUB_TOKEN", "GIT_HTTP_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _runtime_git_credentials(token):
    eg_dir = os.path.dirname(RUNTIME_GIT_CREDENTIALS)
    os.makedirs(eg_dir, mode=0o700, exist_ok=True)
    with open(RUNTIME_GIT_CREDENTIALS, "w", encoding="utf-8") as f:
        f.write(f"https://x-access-token:{token}@github.com\n")
    os.chmod(RUNTIME_GIT_CREDENTIALS, 0o600)
    return RUNTIME_GIT_CREDENTIALS


def _git_credential_store():
    creds = _git_credentials_file()
    if creds:
        return creds
    token = _github_token()
    if token:
        return _runtime_git_credentials(token)
    return ""


def _ensure_git_auth(repo, label):
    origin = _git_origin_url(repo)
    if not origin.startswith("https://github.com/"):
        return
    if _git_credential_store() or _ssh_key_file():
        return
    raise RuntimeError(
        f"{label}: HTTPS github remote needs ~/.git-credentials, "
        f"{GITHUB_TOKEN_FILE}, or ~/.ssh/id_rsa|id_ed25519"
    )


def _ssh_key_file():
    ssh_dir = os.path.join(EDGE_HOME, ".ssh")
    for name in ("id_ed25519", "id_rsa"):
        path = os.path.join(ssh_dir, name)
        if os.path.isfile(path) and os.access(path, os.R_OK):
            return path
    return ""


def _git_origin_url(repo):
    result = _git_cmd(repo, "remote", "get-url", "origin")
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _github_https_to_ssh(url):
    match = re.match(r"https://github\.com/([^/]+)/(.+?)(?:\.git)?/?$", url or "")
    if not match:
        return ""
    return f"git@github.com:{match.group(1)}/{match.group(2)}.git"


def _git_config_args(repo):
    args = []
    creds = _git_credential_store()
    if creds:
        args.extend(["-c", f"credential.helper=store --file={creds}"])

    origin = _git_origin_url(repo)
    key = _ssh_key_file()
    if key:
        ssh_cmd = f"ssh -i {shlex.quote(key)} -o IdentitiesOnly=yes -o BatchMode=yes"
        args.extend(["-c", f"core.sshCommand={ssh_cmd}"])
        if origin.startswith("https://github.com/") and not _git_credentials_file() and not _github_token():
            ssh_url = _github_https_to_ssh(origin)
            if ssh_url:
                args.extend(["-c", f"remote.origin.url={ssh_url}"])

    return args


def _git_shell_command(repo, git_args):
    config_args = _git_config_args(repo)
    if isinstance(git_args, str):
        git_args = shlex.split(git_args)
    parts = ["git", *config_args, *git_args]
    command = " ".join(shlex.quote(part) for part in parts)
    return f"cd {shlex.quote(repo)} && {command}"


def _git_run(repo, git_args):
    return subprocess.run(
        ["bash", "-lc", _git_shell_command(repo, git_args)],
        cwd=EDGE_HOME,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_git_env(),
    )


def _git_reset_to_origin(repo, label, branch):
    branch = branch or "master"
    remote_ref = f"origin/{branch}"
    fetch_label = f"{label}: git fetch origin"

    fetch = _git_run(repo, ["fetch", "origin", "--prune"])
    if fetch.returncode != 0:
        refspec = _git_run(repo, ["fetch", "origin", f"+{branch}:refs/remotes/{remote_ref}"])
        if refspec.returncode != 0:
            shallow = _git_run(repo, ["fetch", "origin", branch, "--depth=1"])
            _raise_git_failure(fetch_label, shallow)

    reset = _git_run(repo, ["reset", "--hard", remote_ref])
    if reset.returncode != 0:
        _raise_git_failure(
            f"{label}: git reset --hard {remote_ref}",
            _git_run(repo, ["reset", "--hard", "FETCH_HEAD"]),
        )
    print(f"{label}: synced to {remote_ref}")


def _git_stash_local_changes(repo, label):
    stash = _git_run(repo, ["stash", "push", "-u", "-m", "server_command update"])
    if stash.returncode != 0:
        raise RuntimeError(
            f"{label}: git stash failed: {_git_error_summary(stash) or stash.returncode}",
        )
    if "No local changes to save" in (stash.stdout or "") + (stash.stderr or ""):
        return False
    return True


def _git_has_merge_conflicts(repo):
    result = _git_run(repo, ["ls-files", "-u"])
    return bool((result.stdout or "").strip())


def _git_raise_stash_merge_conflict(label):
    raise RuntimeError(
        f"{label}: git stash pop left merge conflicts "
        f"(local changes conflict with upstream). "
        f"Stash preserved; resolve manually, then retry.",
    )


def _git_stash_pop(repo, label):
    pop = _git_run(repo, ["stash", "pop"])
    if pop.returncode == 0 and not _git_has_merge_conflicts(repo):
        return
    if _git_has_merge_conflicts(repo):
        _git_run(repo, ["reset", "--hard"])
        _git_raise_stash_merge_conflict(label)
    detail = _git_error_summary(pop) or pop.returncode
    raise RuntimeError(
        f"{label}: git stash pop failed (local changes preserved in stash): {detail}",
    )


def _git_resolve_fetch_ref(repo, git_ref):
    """Resolve a fetched ref to a commit-ish usable with reset --hard."""
    candidates = [
        git_ref,
        f"refs/tags/{git_ref}",
        f"origin/{git_ref}",
        "FETCH_HEAD",
    ]
    for candidate in candidates:
        if _git_ref_exists(repo, candidate):
            return candidate
    return None


def _git_checkout_ref(repo, label, git_ref):
    """Fetch and hard-reset to a branch, tag, or commit SHA."""
    ref = (git_ref or "").strip()
    if not ref:
        raise RuntimeError(f"{label}: empty git ref")

    fetch_label = f"{label}: git fetch origin {ref}"
    fetch = _git_run(repo, ["fetch", "origin", "--prune", "--tags"])
    if fetch.returncode != 0:
        _raise_git_failure(f"{label}: git fetch origin --tags", fetch)

    targeted = _git_run(repo, ["fetch", "origin", ref, "--depth=1"])
    if targeted.returncode != 0:
        # Tags/commits may already be present after --tags fetch; keep going.
        print(f"{fetch_label}: targeted fetch skipped ({_git_error_summary(targeted) or 'exit '+str(targeted.returncode)})")

    resolved = _git_resolve_fetch_ref(repo, ref)
    if not resolved:
        raise RuntimeError(f"{label}: git ref not found after fetch: {ref}")

    stashed = False
    if not _git_working_tree_clean(repo):
        print(f"{label}: stashing local changes before checkout {ref}")
        stashed = _git_stash_local_changes(repo, label)

    reset = _git_run(repo, ["reset", "--hard", resolved])
    if reset.returncode != 0:
        _raise_git_failure(f"{label}: git reset --hard {resolved}", reset)
    print(f"{label}: synced to {ref} ({resolved})")

    if stashed:
        print(f"{label}: restoring stashed local changes")
        _git_stash_pop(repo, label)


def _git_pull_inner(repo, label):
    pull_label = f"{label}: git pull"
    pull = _git_run(repo, ["pull"])
    if pull.returncode == 0:
        return

    detail = _git_error_summary(pull)
    if _git_pull_no_upstream(repo, label, detail):
        return
    if _git_pull_recoverable_result(pull):
        branch = _git_current_branch(repo)
        try:
            _git_sync_to_origin_with_stash(
                repo,
                label,
                branch,
                log_prefix=pull_label,
                reset_notice=(
                    f"{pull_label} failed; resetting to origin/{branch} "
                    "(recoverable sync error)"
                ),
            )
            return
        except RuntimeError as exc:
            raise RuntimeError(
                f"{pull_label} failed (exit {pull.returncode}): {detail}; "
                f"sync fallback failed: {exc}",
            ) from exc

    if not _git_working_tree_clean(repo):
        detail = f"{detail}\nhint: commit or stash local changes, then retry update"
    raise RuntimeError(f"{pull_label} failed (exit {pull.returncode}): {detail}")


def _git_submodule_run_shell(repo, inner_cmd):
    script = (
        f"cd {shlex.quote(repo)} && "
        f"git submodule foreach --recursive {shlex.quote(inner_cmd)}"
    )
    return subprocess.run(
        ["bash", "-lc", script],
        cwd=EDGE_HOME,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_git_env(),
    )


def _git_submodule_foreach(repo, git_args, *, ignore_errors=False):
    inner_cmd = "git " + " ".join(shlex.quote(arg) for arg in git_args)
    if ignore_errors:
        inner_cmd += " || true"
    return _git_submodule_run_shell(repo, inner_cmd)


def _git_submodule_stash_all(repo, label):
    result = _git_submodule_foreach(
        repo,
        ["stash", "push", "-u", "-m", "server_command update"],
        ignore_errors=True,
    )
    if result.returncode != 0:
        detail = _cmd_detail(result) or (result.stderr or "").strip()
        if detail and "No local changes to save" not in detail:
            print(f"{label}: submodule stash warning: {detail}")


def _git_submodule_has_merge_conflicts(repo):
    inner_cmd = 'if [ -n "$(git ls-files -u)" ]; then exit 1; fi'
    return _git_submodule_run_shell(repo, inner_cmd).returncode != 0


def _git_submodule_reset_hard_all(repo):
    _git_submodule_foreach(repo, ["reset", "--hard"], ignore_errors=True)


def _git_submodule_raise_stash_conflict(repo, label):
    _git_submodule_reset_hard_all(repo)
    raise RuntimeError(
        f"{label}: submodule stash pop left merge conflicts "
        f"(local compat changes conflict with upstream). "
        f"Stash preserved; resolve eg_common manually, then retry.",
    )


def _git_submodule_pop_all(repo, label):
    result = _git_submodule_run_shell(
        repo,
        "if git stash list | grep -q .; then git stash pop; fi",
    )
    if result.returncode != 0:
        detail = _cmd_detail(result) or (result.stderr or "").strip()
        if _git_submodule_has_merge_conflicts(repo):
            _git_submodule_raise_stash_conflict(repo, label)
        raise RuntimeError(
            f"{label}: submodule stash pop failed (exit {result.returncode}): {detail}"
        )
    if _git_submodule_has_merge_conflicts(repo):
        _git_submodule_raise_stash_conflict(repo, label)


def _git_submodule_update_inner(repo, label):
    _git_submodule_stash_all(repo, label)
    _run_login_shell(
        _git_shell_command(repo, ["submodule", "update", "--recursive"]),
        f"{label}: submodule update",
    )
    _git_submodule_pop_all(repo, label)


def _run_login_shell(script, label):
    _run_step(["bash", "-lc", script], EDGE_HOME, label)


def _prepare_git_repo(repo_path, label):
    repo = _assert_git_repo(repo_path, label)
    _ensure_git_auth(repo, label)
    return repo


def _git_ref_for_repo(git_refs, repo_path, label=None):
    if not git_refs:
        return None
    keys = []
    if label:
        keys.append(label)
    base = os.path.basename((repo_path or "").rstrip(os.sep))
    if base:
        keys.append(base)
    if repo_path:
        keys.append(repo_path)
    for key in keys:
        ref = (git_refs.get(key) or "").strip()
        if ref:
            return ref
    return None


def _git_sync_repo(repo_path, label, git_ref=None):
    """git pull (or checkout git_ref) and submodule update under one repo lock."""
    repo = _prepare_git_repo(repo_path, label)
    with _git_sync_lock(repo):
        if git_ref:
            _git_checkout_ref(repo, label, git_ref)
        else:
            _git_pull_inner(repo, label)
        _git_submodule_update_inner(repo, label)


def _docker_images_from_build_script(repo_path, label="repo"):
    repo = os.path.realpath(repo_path or "")
    script = os.path.join(repo, "docker-build.sh")
    if not os.path.isfile(script):
        raise RuntimeError(f"{label}: docker-build.sh not found in {repo_path}")
    with open(script, encoding="utf-8") as handle:
        text = handle.read()
    images = []
    seen = set()
    for match in re.finditer(r"-t\s+(\S+)", text):
        image = match.group(1)
        if image not in seen:
            seen.add(image)
            images.append(image)
    if not images:
        raise RuntimeError(f"{label}: no docker image tag (-t) found in {script}")
    return images


def _require_built_docker_images(repo_path, label):
    images = _docker_images_from_build_script(repo_path, label)
    _require_docker_images(*images)
    return images


def _pipeline_job_from_item(item):
    service_name = item.get("service_name")
    eg_path = item.get("eg_pipeline_path")
    if not service_name or not eg_path:
        return None
    job = {
        "service_name": service_name,
        "eg_pipeline_path": eg_path,
    }
    server_id = item.get("server_id")
    if server_id:
        job["server_id"] = server_id
    if item.get("is_rtls"):
        job["is_rtls"] = True
    if item.get("is_kafka"):
        job["is_kafka"] = True
    return job


def _pipeline_jobs_from_command(command):
    pipelines = command.get("pipelines")
    if pipelines:
        jobs = [job for item in pipelines if (job := _pipeline_job_from_item(item))]
        if jobs:
            return jobs
    eg_path = command.get("eg_pipeline_path")
    server_ids = command.get("server_ids") or {}
    legacy_server_id = command.get("server_id")
    jobs = []
    for name in _service_names_from_command(command):
        if not name or not eg_path:
            continue
        job = {"service_name": name, "eg_pipeline_path": eg_path}
        server_id = server_ids.get(name) or legacy_server_id
        if server_id:
            job["server_id"] = server_id
        jobs.append(job)
    return jobs


# --- update ---

def _set_update_step(step, step_label, *, service_name=None):
    with _update_lock:
        _update_state["step"] = step
        _update_state["step_label"] = step_label
        _update_state["updated_at"] = time.time()
        if service_name:
            service_steps = dict(_update_state.get("service_steps") or {})
            service_steps[service_name] = {"step": step, "label": step_label}
            _update_state["service_steps"] = service_steps


def _mark_service_step(service_name, step, *, label=None):
    with _update_lock:
        service_steps = dict(_update_state.get("service_steps") or {})
        service_steps[service_name] = {
            "step": step,
            "label": label or f"{service_name}: {step}",
        }
        _update_state["service_steps"] = service_steps


def _mark_service_step_done(service_name, *, label=None):
    _mark_service_step(service_name, "done", label=label)


def _mark_service_step_failed(service_name, *, label=None):
    _mark_service_step(service_name, "failed", label=label)


def _begin_update_state(service_names, *, include_sys_monitor=True, pipeline_jobs=None):
    if include_sys_monitor:
        step, step_label = "sys_monitor_git", "system_monitor: git pull"
    elif pipeline_jobs:
        first = pipeline_jobs[0]["service_name"]
        step, step_label = "pipelines_git", f"{first}: git pull"
    else:
        step, step_label = "starting", "Preparing update…"
    with _update_lock:
        now = time.time()
        _update_state.update({
            "running": True,
            "step": step,
            "step_label": step_label,
            "started_at": now,
            "updated_at": now,
            "error": None,
            "service_names": list(service_names),
            "service_steps": {},
        })


def _finish_update_state(success, error=None):
    with _update_lock:
        _update_state["running"] = False
        _update_state["step"] = "done" if success else "failed"
        if success:
            _update_state["step_label"] = "Update complete"
        elif error:
            _update_state["step_label"] = "Update finished with errors"
        _update_state["updated_at"] = time.time()
        if error and _update_state.get("step_label"):
            step_label = _update_state["step_label"]
            if ";" not in error and step_label not in error:
                error = f"{step_label}: {error}"
        _update_state["error"] = error


def get_update_status():
    with _update_lock:
        state = dict(_update_state)
        state["service_names"] = list(_update_state["service_names"])
        state["service_steps"] = dict(_update_state.get("service_steps") or {})
    started_at = state.get("started_at")
    state["elapsed_sec"] = round(time.time() - started_at) if started_at else 0
    return state


def _prune_unused_docker_images():
    result = _docker_cmd("image", "prune", "-f", text=True)
    if result.returncode != 0:
        raise RuntimeError(f"docker image prune failed: {_cmd_detail(result) or result.returncode}")
    removed = (result.stdout or "").strip()
    if removed:
        print("docker image prune:", removed)


def _update_in_progress():
    return _update_state["running"] or _update_pruning


def _build_sys_monitor(sys_monitor_path, sys_monitor_service, git_ref=None):
    if git_ref:
        step_label = f"system_monitor: git checkout {git_ref}"
    else:
        step_label = "system_monitor: git pull"
    _set_update_step("sys_monitor_git", step_label, service_name=sys_monitor_service)
    _git_sync_repo(sys_monitor_path, "system_monitor", git_ref=git_ref)
    _set_update_step(
        "sys_monitor_build", "system_monitor: docker build", service_name=sys_monitor_service,
    )
    _run_docker_build(sys_monitor_path, "system_monitor", pre_remove=_SYS_MONITOR_BUILD_IMAGES)
    _set_update_step(
        "sys_monitor_verify", "system_monitor: verify images", service_name=sys_monitor_service,
    )
    _require_built_docker_images(sys_monitor_path, "system_monitor")
    _mark_service_step_done(sys_monitor_service, label="system_monitor: build complete")


def _make_step_setter(step_key, step_lock, service_name=None):
    def set_step(label):
        with step_lock:
            _set_update_step(step_key, label, service_name=service_name)
    return set_step


def _set_service_group_step(step, suffix, service_names, step_lock):
    with step_lock:
        for service_name in service_names:
            _set_update_step(step, f"{service_name}: {suffix}", service_name=service_name)


def _group_pipeline_jobs_by_repo(jobs):
    groups = {}
    order = []
    for job in jobs:
        repo_path = os.path.realpath(job["eg_pipeline_path"])
        if repo_path not in groups:
            groups[repo_path] = []
            order.append(repo_path)
        groups[repo_path].append(job)
    return [(repo_path, groups[repo_path]) for repo_path in order]


def _build_repo_for_jobs(repo_path, jobs, step_lock, git_ref=None):
    label = os.path.basename(repo_path.rstrip(os.sep)) or "repo"
    service_names = [job["service_name"] for job in jobs]

    git_suffix = f"git checkout {git_ref}" if git_ref else "git pull"
    _set_service_group_step("pipeline_build", git_suffix, service_names, step_lock)
    _git_sync_repo(repo_path, label, git_ref=git_ref)
    _set_service_group_step("pipeline_build", "docker build", service_names, step_lock)
    with _repo_build_lock(repo_path):
        _run_docker_build(repo_path, label)
        _set_service_group_step("pipeline_build", "verify images", service_names, step_lock)
        _require_built_docker_images(repo_path, label)
    for service_name in service_names:
        _mark_service_step_done(service_name, label=f"{service_name}: build complete")


def _try_build_repo_for_jobs(repo_path, jobs, step_lock, git_ref=None):
    label = os.path.basename(repo_path.rstrip(os.sep)) or "repo"
    try:
        _build_repo_for_jobs(repo_path, jobs, step_lock, git_ref=git_ref)
        return None
    except Exception as exc:
        for job in jobs:
            _mark_service_step_failed(
                job["service_name"],
                label=f"{job['service_name']}: build failed",
            )
        print(f"pipeline build failed ({label}): {exc}")
        return f"{label}: {exc}"


def _build_pipeline_jobs(pipeline_jobs, step_lock, git_refs=None):
    """Build all pipeline repos first. Returns (errors, jobs that built successfully)."""
    groups = _group_pipeline_jobs_by_repo(pipeline_jobs)
    errors = []
    built_jobs = []
    git_refs = git_refs or {}

    def build_one(repo_path, jobs):
        label = os.path.basename(repo_path.rstrip(os.sep)) or "repo"
        err = _try_build_repo_for_jobs(
            repo_path, jobs, step_lock,
            git_ref=_git_ref_for_repo(git_refs, repo_path, label),
        )
        return err, ([] if err else list(jobs))

    if len(groups) == 1:
        build_err, jobs = build_one(*groups[0])
        if build_err:
            errors.append(build_err)
        built_jobs.extend(jobs)
        return errors, built_jobs

    _set_update_step(
        "pipelines_parallel",
        f"Building {len(pipeline_jobs)} pipeline(s) across {len(groups)} repo(s)",
    )
    with ThreadPoolExecutor(max_workers=len(groups)) as executor:
        futures = {
            executor.submit(build_one, repo_path, jobs): repo_path
            for repo_path, jobs in groups
        }
        for future in as_completed(futures):
            build_err, jobs = future.result()
            if build_err:
                errors.append(build_err)
            built_jobs.extend(jobs)
    return errors, built_jobs


def _build_and_restart_pipeline_jobs(pipeline_jobs, active_before, step_lock, git_refs=None):
    """Build every repo, then restart all previously-active pipelines, before sys_monitor."""
    errors, built_jobs = _build_pipeline_jobs(pipeline_jobs, step_lock, git_refs=git_refs)
    if built_jobs:
        errors.extend(_restart_active_pipelines(built_jobs, active_before, step_lock))
    return errors


def _restart_pipeline_job(job, step_lock):
    service_name = job["service_name"]
    set_step = _make_step_setter("pipeline_restart", step_lock, service_name=service_name)
    set_step(f"{service_name}: restarting")
    _restart_service_or_raise(
        service_name,
        server_id=job.get("server_id"),
        is_rtls=bool(job.get("is_rtls")),
        is_kafka=bool(job.get("is_kafka")),
    )
    _mark_service_step_done(service_name, label=f"{service_name}: restarted")


def _restart_sys_monitor_if_active(sys_monitor_service, was_active):
    if not was_active:
        return
    _set_update_step(
        "sys_monitor_restart", "system_monitor: restarting", service_name=sys_monitor_service,
    )
    _restart_service_or_raise(sys_monitor_service, wait_active=True)
    _mark_service_step_done(sys_monitor_service, label="system_monitor: restarted")


def _try_restart_pipeline_job(job, step_lock):
    service_name = job["service_name"]
    try:
        _restart_pipeline_job(job, step_lock)
        return None
    except Exception as exc:
        _mark_service_step_failed(
            service_name,
            label=f"{service_name}: restart failed",
        )
        print(f"pipeline restart failed ({service_name}): {exc}")
        return f"{service_name}: {exc}"


def _restart_active_pipelines(pipeline_jobs, active_before, step_lock):
    active_jobs = [job for job in pipeline_jobs if job["service_name"] in active_before]
    if not active_jobs:
        return []
    _set_update_step("pipelines_restart", f"Restarting {len(active_jobs)} pipeline(s)")
    errors = []
    if len(active_jobs) == 1:
        err = _try_restart_pipeline_job(active_jobs[0], step_lock)
        if err:
            errors.append(err)
        return errors

    _set_update_step(
        "pipelines_parallel",
        f"Restarting {len(active_jobs)} pipeline(s) in parallel",
    )
    with ThreadPoolExecutor(max_workers=len(active_jobs)) as executor:
        futures = {
            executor.submit(_try_restart_pipeline_job, job, step_lock): job
            for job in active_jobs
        }
        for future in as_completed(futures):
            err = future.result()
            if err:
                errors.append(err)
    return errors


def update_pipeline(command):
    sys_monitor_path = str(command.get("sys_monitor_path") or "").strip() or None
    include_sys_monitor = bool(sys_monitor_path)
    sys_monitor_service = _sys_monitor_service_name(command) if include_sys_monitor else None
    pipeline_jobs = _pipeline_jobs_from_command(command)
    if not pipeline_jobs and not sys_monitor_path:
        raise RuntimeError("no pipeline jobs in update command")

    git_refs = command.get("git_refs") if isinstance(command.get("git_refs"), dict) else {}
    sys_git_ref = (
        _git_ref_for_repo(git_refs, sys_monitor_path, "system_monitor")
        if include_sys_monitor else None
    )

    pipeline_names = [job["service_name"] for job in pipeline_jobs]
    active_before = _active_pipeline_service_names(pipeline_jobs)
    sys_monitor_was_active = (
        _is_service_active(sys_monitor_service) if include_sys_monitor else False
    )
    tracked_services = list(pipeline_names)
    if include_sys_monitor and sys_monitor_service:
        tracked_services = list(dict.fromkeys(pipeline_names + [sys_monitor_service]))
    _begin_update_state(
        tracked_services,
        include_sys_monitor=include_sys_monitor,
        pipeline_jobs=pipeline_jobs,
    )
    errors = []
    step_lock = threading.Lock()
    sys_build_ok = not include_sys_monitor

    try:
        if include_sys_monitor:
            try:
                _build_sys_monitor(sys_monitor_path, sys_monitor_service, git_ref=sys_git_ref)
                sys_build_ok = True
            except Exception as exc:
                _append_update_error(
                    errors, sys_monitor_service, exc,
                    failed_label="system_monitor: build failed",
                    summary=f"system_monitor: {exc}",
                )

        if pipeline_jobs and sys_build_ok:
            # Build all repos, restart all active pipelines, then sys_monitor last.
            errors.extend(_build_and_restart_pipeline_jobs(
                pipeline_jobs, active_before, step_lock, git_refs=git_refs,
            ))
        elif pipeline_jobs and not sys_build_ok:
            skip_msg = "pipeline update skipped: system_monitor build failed"
            errors.append(skip_msg)
            print("update step failed:", skip_msg)
            for job in pipeline_jobs:
                _mark_service_step_failed(
                    job["service_name"],
                    label=f"{job['service_name']}: skipped (system_monitor build failed)",
                )

        if include_sys_monitor and sys_build_ok:
            try:
                _restart_sys_monitor_if_active(sys_monitor_service, sys_monitor_was_active)
            except Exception as exc:
                _append_update_error(
                    errors, sys_monitor_service, exc,
                    failed_label="system_monitor: restart failed",
                    summary=f"system_monitor restart: {exc}",
                )

    except Exception as exc:
        errors.append(str(exc))
        print("update failed:", exc)
    finally:
        error = "; ".join(errors) if errors else None
        _finish_update_state(not errors, error)

    def _prune_after_update():
        global _update_pruning
        try:
            with _update_lock:
                _update_pruning = True
            _prune_unused_docker_images()
        except Exception as exc:
            print("update prune failed:", exc)
        finally:
            with _update_lock:
                _update_pruning = False

    threading.Thread(target=_prune_after_update, daemon=True).start()


def _mark_update_preparing():
    now = time.time()
    _update_state.update({
        "running": True,
        "step": "starting",
        "step_label": "Preparing update…",
        "started_at": now,
        "updated_at": now,
        "error": None,
        "service_names": [],
        "service_steps": {},
    })


def start_update_pipeline(command):
    def _run():
        try:
            update_pipeline(command)
        except Exception as exc:
            with _update_lock:
                if _update_state["running"]:
                    _finish_update_state(False, str(exc))

    with _update_lock:
        if _update_in_progress():
            return False
        _mark_update_preparing()
    _invalidate_probe_caches()
    threading.Thread(target=_run, daemon=True).start()
    return True


def check_pipeline(target):
    if isinstance(target, dict):
        server_id = target.get("server_id")
        is_rtls = bool(target.get("is_rtls"))
        is_sys_monitor = bool(target.get("is_sys_monitor"))
        is_kafka = bool(target.get("is_kafka"))
        service_name = target.get("service_name") or ""
    else:
        server_id = target
        is_rtls = is_sys_monitor = is_kafka = False
        service_name = ""

    running_names = _running_container_names()
    if is_rtls or is_sys_monitor:
        if service_name and _is_service_active(service_name):
            return True
        return _container_running_for_kind(
            server_id,
            running_names,
            is_rtls=is_rtls,
            is_sys_monitor=is_sys_monitor,
        )
    if is_kafka:
        return _kafka_unit_running(service_name, server_id, running_names)

    if server_id and server_id in running_names:
        return True
    # Same server_id may back an RTLS container when the check payload omits is_rtls.
    if _container_running_for_kind(server_id, running_names, is_rtls=True):
        return True
    if _is_container_running(server_id, "sys", running_names):
        return True
    if service_name and _is_service_active(service_name):
        return True
    return False


# --- status ---

def _monitored_peer_names(items, monitor_host):
    return [
        peer["name"] for peer in items
        if is_sys_monitored_peer(item=peer)
        and peer.get("monitor_host_ip") == monitor_host
    ]


def _apply_sys_monitor_status(results, items):
    for item in items:
        if not item.get("is_sys_monitor"):
            continue
        monitor_host = item.get("server_ip")
        if not monitor_host:
            continue
        monitored = _monitored_peer_names(items, monitor_host)
        apply_sys_monitor_peer_status(results, item["name"], monitored)


def _set_camera_status(entry, item):
    camera_links = item.get("camera_links") or []
    if not camera_links:
        return
    if not entry.get("running"):
        entry["camera_status"] = ["idle"] * len(camera_links)
        return
    statuses, cameras_now = _probe_camera_links(camera_links)
    entry["camera_status"] = statuses
    entry["cameras_now"] = cameras_now


def _status_entry(name, item, running_names):
    return {
        "running": _is_pipeline_running(item, running_names),
        "cameras_set": item.get("cameras_set"),
        "qlight_set": item.get("qlight_set"),
        "speaker_set": item.get("speaker_set"),
        "qlight_now": None,
        "speaker_now": None,
        "is_rtls": item.get("is_rtls", False),
        "is_kafka": item.get("is_kafka", False),
        "is_plc_cv": item.get("is_plc_cv", False),
        "is_camera_drift": item.get("is_camera_drift", False),
        "is_sys_monitor": item.get("is_sys_monitor", False),
        "plc_cv_checkers": False,
        "rtls_config_missing": bool(item.get("rtls_config_missing")),
        "rtls_devices_missing": bool(item.get("rtls_devices_missing")),
        "cameras_now": None,
        "camera_links": item.get("camera_links") or [],
        "qlight_status": [],
        "speaker_status": [],
        "camera_status": [],
        "streaming_port": item.get("streaming_port"),
        "streaming_ip": item.get("streaming_ip"),
        "stream_health": False,
        "status": "PENDING",
        "pipeline": name,
        "version_current": None,
        "version_current_date": None,
        "version_latest": None,
        "version_latest_date": None,
        "mem_usage": None,
        "mem_usage_percent": None,
        **empty_plc_tag_metrics(),
        **empty_camera_drift_metrics(),
    }


def _populate_rtls_metrics(entry, item):
    ql_probes = item.get("qlight_probes") or []
    sp_probes = item.get("speaker_probes") or []
    entry["qlight_now"], entry["speaker_now"], entry["qlight_status"], entry["speaker_status"] = probe_rtls_devices(
        ql_probes, sp_probes, item.get("passkey"),
    )
    entry["qlight_links"], entry["speaker_links"] = _rtls_device_links(ql_probes, sp_probes)


def _populate_kafka_metrics(entry, item):
    # Probe locally on the edge; expose server_ip URLs for Open API in the browser.
    probe_base = plc_status_url_for(item, prefer_localhost=True)
    public_base = plc_status_url_for(item, host_ip=item.get("server_ip"))
    if not probe_base:
      probe_base = public_base
    link_base = public_base or probe_base
    entry.update(
      fetch_plc_tag_metrics(
        probe_base, timeout=PROBE_TIMEOUT, link_base_url=link_base,
      )
    )
    if link_base:
        entry["plc_status_url"] = link_base
        entry["url"] = link_base


def _populate_camera_drift_metrics(entry, item):
    base = drift_service_url_for(item, prefer_localhost=True)
    if not base:
      base = drift_service_url_for(item, host_ip=item.get("server_ip"))
    entry.update(fetch_camera_drift_metrics(base, timeout=max(PROBE_TIMEOUT, 10)))
    if base:
      entry["drift_service_url"] = base
      entry["url"] = base


def _populate_stream_metrics(entry, item, probe_cfg):
    health_ok, cameras_now = _cached_probe_local_stream(
        item, probe_cfg["stream_probe_interval_sec"],
    )
    if health_ok:
        entry["stream_health"] = True
        entry["running"] = True
    if cameras_now is not None and not (item.get("camera_links") or []):
        entry["cameras_now"] = cameras_now
    _set_camera_status(entry, item)


def _populate_plc_cv_metrics(entry, item, probe_cfg):
    """PLC-CV: keep /stream health; Signs chips come from GET /checkers."""
    if item.get("streaming_port"):
        health_ok, _ = _cached_probe_local_stream(
            item, probe_cfg["stream_probe_interval_sec"],
        )
        if health_ok:
            entry["stream_health"] = True
            entry["running"] = True

    port = item.get("streaming_port")
    checkers_url = (
        plc_cv_checkers_url_for(item, prefer_localhost=True, streaming_port=port)
        or plc_cv_checkers_url_for(
            item, host_ip=item.get("server_ip"), streaming_port=port,
        )
    )
    metrics = fetch_plc_cv_checker_metrics(checkers_url, timeout=PROBE_TIMEOUT)
    if metrics.get("plc_cv_checkers"):
        entry.update(metrics)
    else:
        _set_camera_status(entry, item)


def status_one(item, running_names, version_cache=None, mem_map=None, probe_cfg=None):
    probe_cfg = probe_cfg or normalize_edge_probe(None)
    name = item["name"]
    item = load_runtime_meta(item, running_names)
    entry = _status_entry(name, item, running_names)
    _apply_repo_versions(entry, item, version_cache or {})
    _apply_container_memory(entry, item, mem_map or {})

    if entry["is_sys_monitor"]:
        return name, entry

    if entry["is_rtls"]:
        _populate_rtls_metrics(entry, item)
    elif entry.get("is_kafka"):
        _populate_kafka_metrics(entry, item)
    elif entry.get("is_camera_drift"):
        _populate_camera_drift_metrics(entry, item)
    elif entry.get("is_plc_cv"):
        _populate_plc_cv_metrics(entry, item, probe_cfg)
    elif not item.get("streaming_port"):
        _set_camera_status(entry, item)
    else:
        _populate_stream_metrics(entry, item, probe_cfg)

    finalize_pipeline_status(entry)
    return name, entry


def _status_failure(name, item, running_names, mem_map):
    entry = _status_entry(name, item, running_names)
    entry["status"] = "ERR"
    _apply_container_memory(entry, item, mem_map)
    return entry


def _status_worker_count(probe_cfg, item_count):
    return min(
        probe_cfg["stream_probe_workers"] or 1,
        EDGE_PROBE_WORKERS,
        max(item_count, 1),
    )


def _collect_status_results(future_map, running_names, mem_map):
    results = {}
    completed = set()
    try:
        for future in as_completed(future_map, timeout=HOST_STATUS_DEADLINE):
            completed.add(future)
            item = future_map[future]
            name = item["name"]
            try:
                name, entry = future.result()
                results[name] = entry
            except Exception as exc:
                print(f"status {name} failed: {exc}")
                results[name] = _status_failure(name, item, running_names, mem_map)
    except FuturesTimeoutError:
        print(f"status batch timed out after {HOST_STATUS_DEADLINE}s")

    for future, item in future_map.items():
        if future in completed:
            continue
        name = item["name"]
        future.cancel()
        print(f"status {name} skipped (timeout)")
        results[name] = _status_failure(name, item, running_names, mem_map)
    return results


def status_pipelines(items, probe_config=None):
    """Build status for all pipelines on this edge.

    Always includes Current (local HEAD, path-deduped). Latest is filled by the
    central proxy.
    """
    probe_cfg = normalize_edge_probe(probe_config)
    running_names = _running_container_names()
    mem_map = _container_memory_map_cached(probe_cfg["docker_stats_interval_sec"])
    version_cache = _prefetch_repo_versions(items)
    workers = _status_worker_count(probe_cfg, len(items))
    future_map = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item in items:
            future_map[pool.submit(
                status_one,
                item,
                running_names,
                version_cache,
                mem_map,
                probe_cfg,
            )] = item
        results = _collect_status_results(future_map, running_names, mem_map)

    _apply_sys_monitor_status(results, items)
    return results


# --- flask ---

_SERVICE_ACTIONS = ("service:start", "service:stop", "service:restart", "service:status")


def _service_control_kwargs(source):
    return {
        "server_id": source.get("server_id"),
        "is_rtls": bool(source.get("is_rtls")),
        "is_sys_monitor": bool(source.get("is_sys_monitor")),
        "is_kafka": bool(source.get("is_kafka")),
    }


def _service_command(command, action_key):
    action = action_key.split(":", 1)[1]
    return service_pipeline(action, command[action_key], **_service_control_kwargs(command))


def _service_batch_results(command):
    items = command["service_batch"]
    if not items:
        return []
    if len(items) == 1:
        item = items[0]
        return [service_pipeline(item["action"], item["service"], **_service_control_kwargs(item))]

    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=len(items)) as executor:
        futures = {
            executor.submit(
                service_pipeline,
                item["action"],
                item["service"],
                **_service_control_kwargs(item),
            ): index
            for index, item in enumerate(items)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                results[index] = str(exc)
    return results


_HOST_POWER_ACTIONS = {
    "reboot": "reboot",
    "shutdown": "poweroff",
}


def host_power(action):
    """Schedule host reboot/poweroff after the HTTP response can flush."""
    systemctl_action = _HOST_POWER_ACTIONS.get(action)
    if not systemctl_action:
        return f"unsupported host power action: {action}"

    def _run():
        time.sleep(1.0)
        result = subprocess.run(
            _systemctl_cmd(systemctl_action),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = _cmd_detail(result) or result.returncode
            print(f"host_power {action} failed: {detail}", flush=True)

    threading.Thread(target=_run, daemon=True, name=f"host-power-{action}").start()
    return "Scheduled"


def handle_command(command):
    """Dispatch POST /command JSON from the central proxy."""
    if "run" in command:
        return "Error" if not run_pipeline(command["run"]) else "Succeed"
    if "stop" in command:
        return "200" if stop_pipeline(command["stop"]) else "Error"
    if "update" in command:
        return "Started" if start_update_pipeline(command["update"]) else "Busy"
    if command.get("update_status"):
        return jsonify(get_update_status())
    if "host_power" in command:
        return host_power(command["host_power"])
    if "service_batch" in command:
        return jsonify({"results": _service_batch_results(command)})
    for action in _SERVICE_ACTIONS:
        if action in command:
            return _service_command(command, action)
    if "check" in command:
        return str(check_pipeline(command["check"]))
    if "status" in command:
        return jsonify(status_pipelines(
            command["status"],
            command.get("edge_probe"),
        ))
    return "200"


def _log_command(command):
    """Log the incoming command compactly (large payloads are summarized)."""
    parts = []
    for key in sorted(command):
        value = command[key]
        if isinstance(value, (list, dict)):
            parts.append(f"{key}[{len(value)}]")
        else:
            parts.append(f"{key}={value}")
    print("command:", ", ".join(parts) or "(empty)")


def _parse_command_body():
    command = request.get_json(silent=True)
    if isinstance(command, dict):
        return command
    raw = request.get_data(as_text=True)
    if not raw or not raw.strip():
        return None
    try:
        command = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return command if isinstance(command, dict) else None


@app.route("/command", methods=["POST"])
def command_recv():
    command = _parse_command_body()
    if command is None:
        return jsonify({"error": "invalid JSON body"}), 400
    _log_command(command)
    return handle_command(command)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5502)
