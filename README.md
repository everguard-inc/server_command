# Server Command

Docker Image Manager for Everguard edge sites. A central **proxy** collects status
from and dispatches commands to **edge agents** running on each host, and serves a
web UI for monitoring and controlling pipelines.

Korean version: [README.ko.md](README.ko.md)

## Architecture

```
                         +---------------------------+
   Browser  <----HTTP---->  proxy_server.py (:5503)  |   central host
                         |   - web UI + status API    |
                         |   - status collector       |
                         +-------------+--------------+
                                       |  POST /command (HTTP)
                 +---------------------+---------------------+
                 |                     |                     |
        +--------v-------+   +---------v------+   +----------v-----+
        | app.py (:5502) |   | app.py (:5502) |   | app.py (:5502) |   edge hosts
        | edge agent     |   | edge agent     |   | edge agent     |
        +----------------+   +----------------+   +----------------+
```

- **Proxy** (`proxy_server.py`, port `5503`): serves the UI, periodically polls each
  edge host for pipeline status, and forwards service/update commands.
- **Edge agent** (`app.py`, port `5502`): runs on each host as a systemd
  service (`server_command_client.service`). It controls pipelines (start/stop/restart),
  probes device health (camera / qlight / speaker), reports memory usage, and performs
  git-pull + docker-build updates. It does **not** import the proxy modules.
- **`servers.json`**: per-site configuration (site id, probe tuning, and the pipeline
  list). Generated from the S3 servers db by `sync_servers_json.py`. **Not tracked in
  git** (`.gitignore`).

## Pipeline kinds

Pipelines are classified by name/config into: `SYSTEM` (system monitor), `RTLS`, `EG`
(default camera pipeline), `COBBLE`, `DETSEG`, `FORKLIFT`. Status is computed as
`OK` / `WARN` / `ERR` per kind (device counts, stream health, container/service state).

## Requirements

Install Python dependencies:

```bash
pip install -r requirements.txt
```

- **Edge hosts** need only `flask` + `httpx` (`app.py` imports `eg_basics` optionally).
- **Proxy / CLI host** also needs `requests` (in `requirements.txt`) and the private
  `eg_basics` package (for S3 config loading and password decryption).

## Files

| File | Role |
|------|------|
| `proxy_server.py` | Central proxy: web UI, status collector, command dispatch (port 5503) |
| `app.py` | Edge agent: pipeline control + health probes (port 5502) |
| `servers_cfg.py` | Shared helpers and `servers.json` loading (proxy/sync/CLI; edge `app.py` imports only, no JSON load) |
| `git_versions.py` | Shared git version lookup (proxy backfill, edge agent) |
| `pipeline_ip.py` | Resolve edge/stream IPs from the S3 config |
| `sync_servers_json.py` | Regenerate `servers.json` from the S3 servers db |
| `setup_client.py` | One-time edge setup: deps, systemd unit, git credentials, sudoers. Removes invalid `~/.git-credentials` only |
| `deploy_clients.py` | SSH deploy (git pull/rsync; `--deps`: setup_client; daily: sync + service restart) |
| `edge_sudoers.py` | Shared sudoers/systemctl helpers (`deploy_clients`, `setup_client`) |
| `rename_systemd_services.py` | Rename systemd units to server IDs from `__services__` |
| `send_command.py` | CLI: send per-pipeline commands to edge agents |
| `send_image_command.py` | CLI: send host-grouped / per-pipeline commands |
| `servers.json` | Per-site pipeline configuration (not in git; see `servers.json.template`) |
| `templates/`, `templates/static/` | Web UI (`index.html`, `index.js`, `style.css`) |

Paths and the service user are resolved at runtime (`~` / current user), so the code
runs unchanged whether the edge user is `everguard`, `eg`, or anything else — as long
as the proxy host and edge hosts of a site share the same username.

## Deploying to a new site

1. **Prepare the proxy host**: clone the repo, `pip install -r requirements.txt`, and
   make sure the private `eg_basics` package is installed.
2. **Generate `servers.json`** for the site (not tracked in git; see `servers.json.template`):

   ```bash
   cp servers.json.template servers.json   # optional starting point
   python3 sync_servers_json.py --xsite-id <SITE_UUID>
   ```

3. **Set up each edge host** (run as the edge login user, not `sudo` directly):

   ```bash
   export xSiteId=<SITE_UUID>      # or pass --site-id below
   python3 setup_client.py         # deps + systemd service + git creds + sudoers
   ```

   If a previous user's `~/.git-credentials` is left behind, setup removes that file
   only and re-creates credentials.
   Provide a GitHub token via `~/.eg/github_token`, `GITHUB_TOKEN`, or `--github-token`.

4. **Deploy code** to all edge hosts from the proxy host:

   On a brand-new edge host, run step 3 `setup_client.py` first. Then from the
   proxy:

   **git pull**

   ```bash
   python3 deploy_clients.py --deps              # first (code + setup + restart)
   python3 deploy_clients.py                     # daily (code + restart)
   ```

   **rsync (`--local`)**

   ```bash
   python3 deploy_clients.py --local --deps      # first (code + setup + restart)
   python3 deploy_clients.py --local             # daily (code + restart)
   ```

   `--deps` / `--local --deps` run `setup_client.py --skip-git` remotely (pip deps +
   systemd unit + sudoers + service restart). Requires sudo password (`SSHPASS` works
   if same as SSH). Daily mode syncs code then restarts `server_command_client.service`
   (passwordless sudo).

   `--local` mirrors the remote `~/server_command` to match the local tree (`rsync
   --delete`). `.git` and `servers.json` are excluded. Requires `rsync` and `sshpass`.

   Other options:

   ```bash
   python3 deploy_clients.py --hosts 10.0.0.5     # limit to specific hosts
   python3 deploy_clients.py --dry-run                # print commands only
   python3 deploy_clients.py -j 4                     # parallel SSH workers (default 8)
   ```

   `SSHPASS` is the SSH password; `SUDOPASS` is for sudo when using `--deps`.

5. **Run the proxy**:

   ```bash
   python3 proxy_server.py          # serves the UI on http://<proxy>:5503
   ```

## CLI usage

`send_command.py` — per-pipeline commands:

```bash
python3 send_command.py -c check                       # default; all pipelines
python3 send_command.py -s RND-3090 -c service:restart
python3 send_command.py -s RND-3090 -c update
```

Allowed: `check`, `update`, `stop`, `stream`, `watchdog`,
`service:start`, `service:stop`, `service:restart`, `service:status`.

`send_image_command.py` — host-grouped or per-pipeline commands (`-s` / hidden `-d`):

```bash
python3 send_image_command.py -c stream                # default; all pipelines
python3 send_image_command.py -s RND-3090 -c check
```

Allowed: `stream`, `watchdog`, `update` (host-grouped); `check`, `stop` (per-pipeline).

## Notes

- The edge agent (`app.py`) does not import `proxy_server` or read `servers.json`.
  Pipeline paths and service names arrive in the `POST /command` JSON. Shared status
  helpers and constants live in `servers_cfg.py` (deployed with the repo).
- `git`/`docker` on the edge run under the service user's `$HOME`. HTTPS GitHub auth
  uses `~/.git-credentials`, `~/.eg/github_token`, or the `GITHUB_TOKEN` env var.
- `systemctl` uses passwordless sudo via `/etc/sudoers.d/server_command`, set up in
  step 3 `setup_client.py`.
