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
import socket
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
    SYS_MONITOR_PATH,
    SYS_MONITOR_SERVICE,
    apply_sys_monitor_peer_status,
    finalize_pipeline_status,
    normalize_edge_probe,
    stream_feed_paths_for,
)

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
SPEAKER_STATUS_PATH = "media.cgi?msubmenu=speakerstatus&action=view"
PROBE_SSE_MAX_LINES = 40
QLIGHT_TYPE_MARKERS = ("qlight", "patlite")
SPEAKER_TYPE_MARKERS = ("simpleurlalarm", "simplerestalarm")

# Docker & service control
DOCKER_CMD_TIMEOUT = 10
SERVICE_CONTROL_TIMEOUT = 180
SERVICE_CONTROL_POLL = 2.0

# Status collection
EDGE_PROBE_WORKERS = 10
RTLS_DEVICE_PROBE_WORKERS = 8
HOST_STATUS_DEADLINE = 45
VERSION_FETCH_WORKERS = 4
GIT_VERSION_TIMEOUT = 12

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
_docker_stats_cache = {"at": 0.0, "map": {}}

_update_lock = threading.Lock()
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


def _tcp_reachable(host, port, *, timeout=None):
    from servers_cfg import tcp_reachable
    return tcp_reachable(host, port, timeout=timeout if timeout is not None else PROBE_TIMEOUT)


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
        if not cfg.get("environment_alert") or (ql_set is None and sp_set is None):
            merged["rtls_config_missing"] = True
        merged.update({
            "qlight_set": ql_set,
            "speaker_set": sp_set,
            "qlight_probes": ql_probes,
            "speaker_probes": sp_probes,
            "passkey": _config_passkey(cfg, config_dir=config_dir) or item.get("passkey"),
        })
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
    base = f"http://{host}:{port}"
    try:
        resp = httpx.get(f"{base}/health", timeout=STREAM_HEALTH_TIMEOUT)
        if resp.status_code != 200:
            return False, None
    except httpx.HTTPError:
        return False, None

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

    if not _tcp_reachable(host, port):
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


def _camera_device_status(camera_links, cameras_now, cameras_set, running):
    count = len(camera_links)
    if not count:
        return []
    if not running:
        return ["idle"] * count
    if cameras_now is None:
        return ["unknown"] * count
    target = cameras_set if cameras_set is not None else count
    if cameras_now >= target:
        return ["ok"] * count
    return ["ok" if idx < cameras_now else "err" for idx in range(count)]


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


def _container_name_for_item(item, running_names):
    server_id = item.get("server_id")
    if not server_id:
        return None
    if item.get("is_sys_monitor"):
        return _matching_container_name(server_id, "sys", running_names)
    if item.get("is_rtls"):
        return _matching_container_name(
            server_id, "rtls", running_names, loose_match=True,
        )
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


def _apply_container_memory(entry, item, mem_map):
    container = _container_name_for_item(item, set(mem_map))
    usage = mem_map.get(container) if container else None
    entry["mem_usage"] = usage
    entry["mem_usage_percent"] = _mem_usage_percent(usage)


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


def _is_pipeline_running(item, running_names):
    server_id = item.get("server_id")
    if item.get("is_sys_monitor") or item.get("is_rtls"):
        return _is_service_active(_pipeline_service_name(item)) or _container_running_for_kind(
            server_id,
            running_names,
            is_rtls=bool(item.get("is_rtls")),
            is_sys_monitor=bool(item.get("is_sys_monitor")),
        )
    return server_id in running_names


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


def service_pipeline(command, service_name, *, server_id=None, is_rtls=False, is_sys_monitor=False):
    running = _is_pipeline_under_control(service_name, server_id, is_rtls, is_sys_monitor)

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
        result = _run_systemctl("restart", service_name)
        if result.returncode != 0:
            return _systemctl_fail_message(result)
        # systemctl restart already blocks until the unit finishes restarting.
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


def _restart_service_or_raise(service_name, *, server_id=None, is_rtls=False, is_sys_monitor=False, wait_active=False):
    print("Restarting service:", service_name)
    result = service_pipeline(
        "restart", service_name, server_id=server_id, is_rtls=is_rtls, is_sys_monitor=is_sys_monitor,
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


_SYS_MONITOR_BUILD_IMAGES = ("eg/basics:latest", "eg/sys:latest")


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


_GIT_PULL_RECOVERABLE = (
    "refusing to merge unrelated histories",
    "have diverged",
    "cannot lock ref",
    "not possible to fast-forward",
    "non-fast-forward",
)

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


def _git_pull_recoverable_result(result):
    lower = _git_output_text(result).lower()
    return any(marker in lower for marker in _GIT_PULL_RECOVERABLE)


def _is_git_repo_dir(path):
    from git_versions import is_git_repo_dir
    return is_git_repo_dir(path)


def _git_short_rev(repo_path, ref="HEAD"):
    if not _is_git_repo_dir(repo_path):
        return None
    result = _git_cmd(repo_path, "rev-parse", "--short", ref)
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def _git_commit_date(repo_path, ref):
    if not repo_path or not ref:
        return None
    result = _git_cmd(repo_path, "show", "-s", "--format=%ci", ref)
    if result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    return text[:16] if text else None


def _git_latest_remote_sha(repo_path):
    if not _is_git_repo_dir(repo_path):
        return None, None
    result = _git_cmd(
        repo_path, "ls-remote", "--heads", "origin",
        timeout=GIT_VERSION_TIMEOUT, use_credentials=True,
    )
    if result.returncode != 0:
        return None, None
    from git_versions import parse_ls_remote_heads
    return parse_ls_remote_heads(result.stdout)


def _read_repo_versions(repo_path):
    current = _git_short_rev(repo_path)
    current_date = _git_commit_date(repo_path, "HEAD") if current else None
    full_sha, latest = _git_latest_remote_sha(repo_path)
    latest_date = None
    if full_sha:
        latest_date = _git_commit_date(repo_path, full_sha)
        if not latest_date:
            _git_cmd(
                repo_path, "fetch", "origin", full_sha, "--depth=1", "--quiet",
                timeout=GIT_VERSION_TIMEOUT, use_credentials=True,
            )
            latest_date = _git_commit_date(repo_path, full_sha)
    if current and latest and current == latest and current_date:
        latest_date = current_date
    return current, current_date, latest, latest_date


def _repo_path_for_item(item):
    if item.get("is_sys_monitor"):
        # Proxy status item should include sys_monitor_path.
        return item.get("sys_monitor_path") or SYS_MONITOR_PATH
    return item.get("eg_pipeline_path")


def _unique_repo_paths(items):
    return list(_dedupe_preserve_order(
        (_repo_path_for_item(item) for item in items),
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
    repo_path = _repo_path_for_item(item)
    if not repo_path:
        return
    current, current_date, latest, latest_date = version_cache.get(
        repo_path, (None, None, None, None),
    )
    entry["version_current"] = current
    entry["version_current_date"] = current_date
    entry["version_latest"] = latest
    entry["version_latest_date"] = latest_date


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


def _git_pull_inner(repo, label):
    pull_label = f"{label}: git pull"
    pull = _git_run(repo, ["pull"])
    if pull.returncode == 0:
        return

    detail = _git_error_summary(pull)
    if _git_pull_recoverable_result(pull) and _git_working_tree_clean(repo):
        branch = _git_current_branch(repo)
        print(
            f"{pull_label} failed; resetting to origin/{branch} "
            "(diverged local history, working tree clean)",
        )
        try:
            _git_reset_to_origin(repo, label, branch)
            return
        except RuntimeError as exc:
            raise RuntimeError(
                f"{pull_label} failed (exit {pull.returncode}): {detail}; "
                f"sync fallback failed: {exc}",
            ) from exc

    if not _git_working_tree_clean(repo):
        detail = f"{detail}\nhint: commit or stash local changes, then retry update"
    raise RuntimeError(f"{pull_label} failed (exit {pull.returncode}): {detail}")


def _git_submodule_update_inner(repo, label):
    _run_login_shell(
        _git_shell_command(repo, ["submodule", "update", "--recursive", "--remote"]),
        f"{label}: submodule update",
    )


def _run_login_shell(script, label):
    _run_step(["bash", "-lc", script], EDGE_HOME, label)


def _prepare_git_repo(repo_path, label):
    repo = _assert_git_repo(repo_path, label)
    _ensure_git_auth(repo, label)
    return repo


def _git_sync_repo(repo_path, label):
    """git pull (+ fetch/reset fallback) and submodule update under one repo lock."""
    repo = _prepare_git_repo(repo_path, label)
    with _git_sync_lock(repo):
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
    return job


def _pipeline_jobs_from_command(command):
    pipelines = command.get("pipelines")
    if pipelines:
        jobs = [job for item in pipelines if (job := _pipeline_job_from_item(item))]
        if jobs:
            return jobs
    eg_path = command.get("eg_pipeline_path")
    return [
        {"service_name": name, "eg_pipeline_path": eg_path}
        for name in _service_names_from_command(command)
        if name and eg_path
    ]


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


def _mark_service_step_done(service_name, *, label=None):
    with _update_lock:
        service_steps = dict(_update_state.get("service_steps") or {})
        service_steps[service_name] = {
            "step": "done",
            "label": label or f"{service_name}: done",
        }
        _update_state["service_steps"] = service_steps


def _begin_update_state(service_names):
    with _update_lock:
        now = time.time()
        _update_state.update({
            "running": True,
            "step": "sys_monitor_git",
            "step_label": "system_monitor: git pull",
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
        _update_state["updated_at"] = time.time()
        if error and _update_state.get("step_label"):
            step_label = _update_state["step_label"]
            if step_label not in error:
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
    _set_update_step("prune_images", "Cleaning unused docker images")
    result = _docker_cmd("image", "prune", "-f", text=True)
    if result.returncode != 0:
        raise RuntimeError(f"docker image prune failed: {_cmd_detail(result) or result.returncode}")
    removed = (result.stdout or "").strip()
    if removed:
        print("docker image prune:", removed)


def _build_sys_monitor(sys_monitor_path, sys_monitor_service):
    _set_update_step(
        "sys_monitor_git", "system_monitor: git pull", service_name=sys_monitor_service,
    )
    _git_sync_repo(sys_monitor_path, "system_monitor")
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


def _build_repo_for_jobs(repo_path, jobs, step_lock):
    label = os.path.basename(repo_path.rstrip(os.sep)) or "repo"
    service_names = [job["service_name"] for job in jobs]

    _set_service_group_step("pipeline_build", "git pull", service_names, step_lock)
    _git_sync_repo(repo_path, label)
    _set_service_group_step("pipeline_build", "docker build", service_names, step_lock)
    with _repo_build_lock(repo_path):
        _run_docker_build(repo_path, label)
        _set_service_group_step("pipeline_build", "verify images", service_names, step_lock)
        _require_built_docker_images(repo_path, label)
    for service_name in service_names:
        _mark_service_step_done(service_name, label=f"{service_name}: build complete")


def _run_tasks_collect_failures(tasks):
    """Run ``(name, callable)`` tasks in parallel; raise a combined RuntimeError on failure."""
    failures = []
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {executor.submit(fn): name for name, fn in tasks}
        for future in as_completed(futures):
            name = futures[future]
            try:
                future.result()
            except Exception as exc:
                failures.append((name, exc))
    if failures:
        raise RuntimeError("; ".join(f"{name}: {exc}" for name, exc in failures))


def _build_pipeline_jobs(pipeline_jobs, step_lock):
    groups = _group_pipeline_jobs_by_repo(pipeline_jobs)
    if len(groups) == 1:
        repo_path, jobs = groups[0]
        _build_repo_for_jobs(repo_path, jobs, step_lock)
        return

    _set_update_step(
        "pipelines_parallel",
        f"Building {len(pipeline_jobs)} pipeline(s) across {len(groups)} repo(s)",
    )
    _run_tasks_collect_failures([
        (
            os.path.basename(repo_path),
            (lambda rp=repo_path, js=jobs: _build_repo_for_jobs(rp, js, step_lock)),
        )
        for repo_path, jobs in groups
    ])


def _restart_pipeline_job(job, step_lock):
    service_name = job["service_name"]
    set_step = _make_step_setter("pipeline_restart", step_lock, service_name=service_name)
    set_step(f"{service_name}: restarting")
    _restart_service_or_raise(
        service_name,
        server_id=job.get("server_id"),
        is_rtls=bool(job.get("is_rtls")),
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


def _run_parallel_pipeline_tasks(label, jobs, worker):
    if len(jobs) == 1:
        worker(jobs[0])
        return
    _set_update_step("pipelines_parallel", label)
    _run_tasks_collect_failures([
        ((job.get("service_name") or "pipeline"), (lambda j=job: worker(j)))
        for job in jobs
    ])


def _restart_active_pipelines(pipeline_jobs, active_before, step_lock):
    active_jobs = [job for job in pipeline_jobs if job["service_name"] in active_before]
    if not active_jobs:
        return
    _set_update_step("pipelines_restart", f"Restarting {len(active_jobs)} pipeline(s)")
    _run_parallel_pipeline_tasks(
        f"Restarting {len(active_jobs)} pipeline(s) in parallel",
        active_jobs,
        lambda job: _restart_pipeline_job(job, step_lock),
    )


def update_pipeline(command):
    sys_monitor_path = command["sys_monitor_path"]
    sys_monitor_service = _sys_monitor_service_name(command)
    pipeline_jobs = _pipeline_jobs_from_command(command)
    if not pipeline_jobs and not sys_monitor_path:
        raise RuntimeError("no pipeline jobs in update command")

    pipeline_names = [job["service_name"] for job in pipeline_jobs]
    active_before = _active_pipeline_service_names(pipeline_jobs)
    sys_monitor_was_active = _is_service_active(sys_monitor_service)
    tracked_services = list(dict.fromkeys(pipeline_names + [sys_monitor_service]))
    _begin_update_state(tracked_services)
    error = None
    step_lock = threading.Lock()

    try:
        _build_sys_monitor(sys_monitor_path, sys_monitor_service)
        if pipeline_jobs:
            _build_pipeline_jobs(pipeline_jobs, step_lock)
            _restart_active_pipelines(pipeline_jobs, active_before, step_lock)

        _restart_sys_monitor_if_active(sys_monitor_service, sys_monitor_was_active)
        _prune_unused_docker_images()
    except Exception as exc:
        error = str(exc)
        print("update failed:", exc)
    finally:
        _finish_update_state(error is None, error)


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
        if _update_state["running"]:
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
        service_name = target.get("service_name") or ""
    else:
        server_id = target
        is_rtls = is_sys_monitor = False
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

    if server_id and server_id in running_names:
        return True
    # Same server_id may back both EG and RTLS containers on one host.
    if _container_running_for_kind(server_id, running_names, is_rtls=True):
        return True
    if _is_container_running(server_id, "sys", running_names):
        return True
    return False


# --- status ---

def _monitored_peer_names(items, monitor_host):
    return [
        peer["name"] for peer in items
        if not peer.get("is_sys_monitor")
        and not peer.get("is_rtls")
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
    entry["camera_status"] = _camera_device_status(
        camera_links,
        entry.get("cameras_now"),
        entry.get("cameras_set"),
        entry.get("running"),
    )


def _status_entry(name, item, running_names):
    return {
        "running": _is_pipeline_running(item, running_names),
        "cameras_set": item.get("cameras_set"),
        "qlight_set": item.get("qlight_set"),
        "speaker_set": item.get("speaker_set"),
        "qlight_now": None,
        "speaker_now": None,
        "is_rtls": item.get("is_rtls", False),
        "is_sys_monitor": item.get("is_sys_monitor", False),
        "rtls_config_missing": bool(item.get("rtls_config_missing")),
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
    }


def _populate_rtls_metrics(entry, item):
    ql_probes = item.get("qlight_probes") or []
    sp_probes = item.get("speaker_probes") or []
    entry["qlight_now"], entry["speaker_now"], entry["qlight_status"], entry["speaker_status"] = probe_rtls_devices(
        ql_probes, sp_probes, item.get("passkey"),
    )
    entry["qlight_links"], entry["speaker_links"] = _rtls_device_links(ql_probes, sp_probes)


def _populate_stream_metrics(entry, item, probe_cfg):
    health_ok, cameras_now = _cached_probe_local_stream(
        item, probe_cfg["stream_probe_interval_sec"],
    )
    if health_ok:
        entry["stream_health"] = True
        entry["running"] = True
    if cameras_now is not None:
        entry["cameras_now"] = cameras_now
    _set_camera_status(entry, item)


def status_one(
    item, running_names, version_cache=None, mem_map=None, probe_cfg=None,
    *, fetch_versions=False,
):
    probe_cfg = probe_cfg or normalize_edge_probe(None)
    name = item["name"]
    item = load_runtime_meta(item, running_names)
    entry = _status_entry(name, item, running_names)
    if fetch_versions:
        _apply_repo_versions(entry, item, version_cache or {})
    _apply_container_memory(entry, item, mem_map or {})

    if entry["is_sys_monitor"]:
        return name, entry

    if entry["is_rtls"]:
        _populate_rtls_metrics(entry, item)
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


def status_pipelines(items, probe_config=None, fetch_versions=False):
    probe_cfg = normalize_edge_probe(probe_config)
    running_names = _running_container_names()
    mem_map = _container_memory_map_cached(probe_cfg["docker_stats_interval_sec"])
    version_cache = _prefetch_repo_versions(items) if fetch_versions else {}
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
                fetch_versions=fetch_versions,
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
            fetch_versions=bool(command.get("fetch_versions")),
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


@app.route("/command", methods=["POST"])
def command_recv():
    command = json.loads(request.data)
    _log_command(command)
    return handle_command(command)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5502)
