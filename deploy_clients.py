#!/usr/bin/env python3
"""Deploy server_command to edge hosts via SSH.

Brand-new edge host: run setup_client.py on the edge once (README step 3).
Then deploy from the proxy:

  git pull
    first (code + setup + restart):  python3 deploy_clients.py --deps
    daily (code + restart):            python3 deploy_clients.py

  rsync (--local)
    first (code + setup + restart):  python3 deploy_clients.py --local --deps
    daily (code + restart):            python3 deploy_clients.py --local

  python3 deploy_clients.py --hosts 10.0.0.10
  python3 deploy_clients.py --dry-run

--local rsyncs the working tree and .git so edge Current (git HEAD) matches this host.
--deps runs setup_client.py remotely (git credentials, pip, systemd, sudoers, restart).
Set ~/.git-credentials (or GITHUB_TOKEN) on the deploy host when edges lack git credentials.
Daily mode syncs code then restarts server_command_client.service (passwordless sudo).
Requires SUDOPASS only with --deps (SSHPASS is enough when SSH and sudo passwords match).

Prompts once for SSH password (sshpass). Set SSHPASS / SUDOPASS to skip prompts.
"""

import argparse
import base64
import getpass
import os
import re
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from edge_sudoers import REMOTE_SHELL, SERVICE_UNIT, systemctl_paths
from servers_cfg import is_usable_edge_host, load_servers_cfg

# --- config ---

REPO = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SSH_USER = getpass.getuser()
DEFAULT_REPO_PATH = os.path.join(os.path.expanduser("~"), "server_command")
GITHUB_TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".eg", "github_token")
GIT_CREDENTIALS_FILE = os.path.join(os.path.expanduser("~"), ".git-credentials")
# Keep .git so --local updates edge HEAD / UI Current to match this host.
LOCAL_SYNC_EXCLUDES = ("__pycache__", "*.pyc", "nohup.out", "servers.json")
_ENV_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


# --- hosts ---

def collect_edge_hosts(pipelines, only=None):
  known = {
    cfg["server_ip"]
    for cfg in pipelines.values()
    if cfg.get("server_ip") and is_usable_edge_host(cfg["server_ip"])
  }
  if not only:
    return sorted(known)
  unknown = set(only) - known
  if unknown:
    print("warning: hosts not in servers.json:", ", ".join(sorted(unknown)), file=sys.stderr)
  return sorted(only)


# --- credentials & tools ---

def _require_tool(name, hint):
  path = shutil.which(name)
  if not path:
    print(f"{name} is required ({hint})", file=sys.stderr)
    sys.exit(1)
  return path


def _read_password(*, env_var, prompt, fallback=None, dry_run=False):
  if dry_run:
    return None
  value = os.environ.get(env_var)
  if value:
    return value
  if fallback is not None:
    return fallback
  return getpass.getpass(prompt)


def read_credentials(*, dry_run, need_sudo=False):
  ssh = _read_password(
    env_var="SSHPASS",
    prompt=f"SSH password for {DEFAULT_SSH_USER}@<hosts>: ",
    dry_run=dry_run,
  )
  if not dry_run and not ssh:
    print("empty SSH password", file=sys.stderr)
    return None, None
  if dry_run:
    return None, None
  _require_tool("sshpass", "install: apt install sshpass")
  sudo = None
  if need_sudo:
    sudo = _read_password(
      env_var="SUDOPASS",
      prompt=f"sudo password for {DEFAULT_SSH_USER}@<hosts>: ",
      fallback=ssh,
      dry_run=dry_run,
    )
  return ssh, sudo


def _parse_github_token_from_credentials(path):
  if not os.path.isfile(path):
    return ""
  try:
    with open(path, encoding="utf-8") as handle:
      for line in handle:
        line = line.strip()
        if not line or "github.com" not in line:
          continue
        match = re.match(r"https://(?:[^:]+):([^@]+)@github\.com/?", line)
        if match:
          return match.group(1).strip()
  except OSError:
    pass
  return ""


def read_github_token():
  token = os.environ.get("GITHUB_TOKEN", "").strip()
  if token:
    return token
  if os.path.isfile(GITHUB_TOKEN_FILE):
    with open(GITHUB_TOKEN_FILE, encoding="utf-8") as handle:
      token = handle.read().strip()
      if token:
        return token
  return _parse_github_token_from_credentials(GIT_CREDENTIALS_FILE)


def ssh_opts(connect_timeout):
  return [
    "-o", f"ConnectTimeout={connect_timeout}",
    "-o", "StrictHostKeyChecking=accept-new",
  ]


# --- shell helpers ---

def shell_script(*steps):
  return "set -eu -o pipefail; " + " && ".join(steps)


def clean_ssh_output(text):
  """Drop shell startup noise (set/xtrace dumps) from captured SSH stdout."""
  lines = []
  for line in (text or "").splitlines():
    stripped = line.strip()
    if not stripped:
      continue
    if stripped.startswith("+ "):
      continue
    if _ENV_LINE.match(stripped):
      continue
    if stripped in ("{", "}", "};") or stripped.endswith("()"):
      continue
    lines.append(line)
  return lines


def restart_service_step():
  systemctl = shlex.quote(systemctl_paths()[0])
  unit = shlex.quote(SERVICE_UNIT)
  return f"sudo -n {systemctl} restart {unit}"


def restart_service_script():
  return shell_script(restart_service_step())


def setup_client_env_steps(sudo_password, username, *, github_token=""):
  pw = sudo_password if sudo_password is not None else ""
  b64 = shlex.quote(base64.b64encode(pw.encode()).decode())
  user = shlex.quote(username)
  steps = [
    f"export SUDOPASS=$(printf '%s' {b64} | base64 -d)",
    f"export SUDO_USER={user}",
  ]
  if github_token:
    token_b64 = shlex.quote(base64.b64encode(github_token.encode()).decode())
    steps.append(f"export GITHUB_TOKEN=$(printf '%s' {token_b64} | base64 -d)")
  steps.append("python3 setup_client.py")
  return steps


def setup_client_remote_script(repo_path, sudo_password, username, *, github_token=""):
  """Run setup_client.py on a remote host with SUDOPASS/SUDO_USER set reliably."""
  repo = shlex.quote(repo_path)
  return shell_script(
    f"cd {repo}",
    *setup_client_env_steps(sudo_password, username, github_token=github_token),
  )


def git_pull_script(repo_path, *, with_setup, sudo_password, username, github_token=""):
  repo = shlex.quote(repo_path)
  steps = [f"cd {repo}", "git pull"]
  if with_setup:
    steps.extend(setup_client_env_steps(
      sudo_password, username, github_token=github_token,
    ))
  else:
    steps.append(restart_service_step())
  return shell_script(*steps)


# --- SSH runner ---

class SshRunner:
  def __init__(self, *, user, password, connect_timeout, command_timeout, dry_run):
    self.user = user
    self.password = password
    self.connect_timeout = connect_timeout
    self.command_timeout = command_timeout
    self.dry_run = dry_run
    self._sshpass = _require_tool("sshpass", "install: apt install sshpass") if password else None

  def _wrap(self, cmd):
    if not self.password:
      return cmd, None
    return [self._sshpass, "-e", *cmd], {"SSHPASS": self.password}

  def _ssh_cmd(self, host, script):
    return ["ssh", *ssh_opts(self.connect_timeout), f"{self.user}@{host}", *REMOTE_SHELL, "-c", script]

  def _rsync_cmd(self, host, local_dir, remote_dir):
    transport = "ssh " + " ".join(ssh_opts(self.connect_timeout))
    excludes = [item for pattern in LOCAL_SYNC_EXCLUDES for item in ("--exclude", pattern)]
    src = local_dir.rstrip("/") + "/"
    dest = f"{self.user}@{host}:{remote_dir.rstrip('/')}/"
    return ["rsync", "-az", "--delete", "-e", transport, *excludes, src, dest]

  def run(self, host, cmd, *, label=None):
    cmd, env = self._wrap(cmd)
    if self.dry_run:
      print(f"[dry-run] {host}: {' '.join(shlex.quote(p) for p in cmd)}")
      return 0
    if label:
      print(label)
    try:
      result = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=self.command_timeout, env={**os.environ, **(env or {})},
      )
    except subprocess.TimeoutExpired:
      print(f"FAIL {host}: timed out", file=sys.stderr)
      return 124

    if result.returncode == 0:
      print(f"ok {host}")
      for line in clean_ssh_output(result.stdout):
        print(f"   {line}")
      return 0

    detail = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
    print(f"FAIL {host}: {detail}", file=sys.stderr)
    if "sudo:" in detail and ("password" in detail.lower() or "terminal" in detail.lower()):
      print(
        f"       hint: run with --deps --hosts {host} once to install sudoers",
        file=sys.stderr,
      )
    return result.returncode

  def ssh(self, host, script, *, label):
    return self.run(host, self._ssh_cmd(host, script), label=label)

  def rsync(self, host, local_dir, remote_dir, *, label):
    return self.run(host, self._rsync_cmd(host, local_dir, remote_dir), label=label)


# --- deploy ---

def needs_setup_sudo(args):
  return bool(args.deps)


def deploy_host(host, args, runner):
  if args.local:
    code = runner.rsync(host, REPO, args.repo_path, label=f"rsync {host}")
    if not runner.dry_run and code != 0:
      return code
    if not args.deps:
      return runner.ssh(host, restart_service_script(), label=f"restart {host}")
    script = setup_client_remote_script(
      args.repo_path, args.sudo_password, args.user,
      github_token=args.github_token,
    )
    return runner.ssh(host, script, label=f"deploy {host}")

  script = git_pull_script(
    args.repo_path,
    with_setup=bool(args.deps),
    sudo_password=args.sudo_password,
    username=args.user,
    github_token=args.github_token,
  )
  return runner.ssh(host, script, label=f"deploy {host}")


def describe_mode(args):
  if args.local:
    deploy = (
      "rsync(+.git) + setup_client.py" if args.deps
      else "rsync(+.git) + service restart"
    )
  elif args.deps:
    deploy = "git pull + setup_client.py"
  else:
    deploy = "git pull + service restart"
  return deploy


def validate_args(args):
  if args.local and not args.dry_run:
    _require_tool("rsync", "install: apt install rsync")
  if needs_setup_sudo(args) and not args.dry_run and not args.sudo_password:
    print("empty sudo password (required for remote setup_client.py)", file=sys.stderr)
    return 1
  if args.deps and not args.dry_run and not args.github_token:
    print(
      "no GitHub token on deploy host (required for remote git credentials setup)",
      file=sys.stderr,
    )
    print(
      f"       set GITHUB_TOKEN or create {GIT_CREDENTIALS_FILE} on the deploy host",
      file=sys.stderr,
    )
    return 1
  return None


def build_parser():
  p = argparse.ArgumentParser(description="Deploy server_command agent to edge hosts via SSH.")
  p.add_argument("--local", action="store_true",
                 help="rsync local folder + .git (updates edge Current/HEAD) instead of git pull")
  p.add_argument("--hosts", nargs="+", metavar="IP",
                 help="target IPs (default: all in servers.json)")
  p.add_argument("--user", default=DEFAULT_SSH_USER)
  p.add_argument("--repo-path", default=DEFAULT_REPO_PATH)
  p.add_argument("--deps", action="store_true",
                 help="after sync/pull: run setup_client.py (git, pip, systemd, sudoers, restart)")
  p.add_argument("-j", "--jobs", type=int, default=8, metavar="N")
  p.add_argument("--connect-timeout", type=int, default=10)
  p.add_argument("--command-timeout", type=int, default=300)
  p.add_argument("--dry-run", action="store_true")
  return p


def main():
  args = build_parser().parse_args()
  args.github_token = read_github_token()
  args.ssh_password, args.sudo_password = read_credentials(
    dry_run=args.dry_run,
    need_sudo=needs_setup_sudo(args),
  )
  if args.ssh_password is None and not args.dry_run:
    return 1

  err = validate_args(args)
  if err is not None:
    return err

  hosts = collect_edge_hosts(load_servers_cfg(), only=args.hosts)
  if not hosts:
    print("no edge hosts found", file=sys.stderr)
    return 1

  runner = SshRunner(
    user=args.user,
    password=args.ssh_password,
    connect_timeout=args.connect_timeout,
    command_timeout=args.command_timeout,
    dry_run=args.dry_run,
  )

  print(f"deploying to {len(hosts)} host(s): {', '.join(hosts)}")
  print(f"mode: {describe_mode(args)}")

  failures = []
  workers = max(1, min(args.jobs, len(hosts)))
  with ThreadPoolExecutor(max_workers=workers) as pool:
    futures = {pool.submit(deploy_host, host, args, runner): host for host in hosts}
    for future in as_completed(futures):
      if future.result() != 0:
        failures.append(futures[future])

  print()
  if failures:
    print(f"failed ({len(failures)}): {', '.join(sorted(failures))}", file=sys.stderr)
    return 1
  print(f"all {len(hosts)} host(s) ok")
  return 0


if __name__ == "__main__":
  sys.exit(main())
