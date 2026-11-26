#!/usr/bin/env python3
"""Update eg_reporter on edge hosts via SSH.

Assumes ~/eg_reporter already exists on each edge (git clone + systemd once).
Daily flow from the proxy:

  python3 update_eg_reporter.py
  python3 update_eg_reporter.py --hosts 10.0.0.10
  python3 update_eg_reporter.py --ref main
  python3 update_eg_reporter.py --skip-build
  python3 update_eg_reporter.py --dry-run

Each host runs: git pull → ./docker-build.sh → systemctl restart eg_reporter.service
(passwordless sudo via existing edge sudoers). Set SSHPASS to skip the SSH prompt.
Docker builds are slow; default --command-timeout is 1800s and -j defaults to 4.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from deploy_clients import (
  DEFAULT_SSH_USER,
  SshRunner,
  collect_edge_hosts,
  read_credentials,
  shell_script,
)
from edge_sudoers import systemctl_paths
from servers_cfg import load_servers_cfg

DEFAULT_REPO_PATH = os.path.join(os.path.expanduser("~"), "eg_reporter")
SERVICE_UNIT = "eg_reporter.service"
BUILD_SCRIPT = "docker-build.sh"


def restart_step():
  systemctl = shlex.quote(systemctl_paths()[0])
  unit = shlex.quote(SERVICE_UNIT)
  return f"sudo -n {systemctl} restart {unit}"


def update_script(repo_path, *, ref="", skip_build=False):
  repo = shlex.quote(repo_path)
  steps = [
    f"cd {repo}",
    "test -d .git",
  ]
  if ref:
    ref_q = shlex.quote(ref)
    steps.extend([
      "git fetch --all --tags --prune",
      f"git checkout {ref_q}",
      f"git pull --ff-only origin {ref_q} || git pull --ff-only",
    ])
  else:
    steps.append("git pull --ff-only")
  if not skip_build:
    steps.append(f"test -x ./{BUILD_SCRIPT}")
    steps.append(f"./{BUILD_SCRIPT}")
  steps.append(restart_step())
  return shell_script(*steps)


def build_parser():
  p = argparse.ArgumentParser(
    description="Update eg_reporter on edge hosts (git pull + docker build + restart).",
  )
  p.add_argument("--hosts", nargs="+", metavar="IP",
                 help="target IPs (default: all in servers.json)")
  p.add_argument("--user", default=DEFAULT_SSH_USER)
  p.add_argument("--repo-path", default=DEFAULT_REPO_PATH,
                 help=f"remote eg_reporter path (default: {DEFAULT_REPO_PATH})")
  p.add_argument("--ref", default="", metavar="REF",
                 help="optional git branch/tag/commit to checkout before pull")
  p.add_argument("--skip-build", action="store_true",
                 help="git pull + restart only (skip ./docker-build.sh)")
  p.add_argument("-j", "--jobs", type=int, default=4, metavar="N",
                 help="parallel SSH workers (default 4; builds are heavy)")
  p.add_argument("--connect-timeout", type=int, default=10)
  p.add_argument("--command-timeout", type=int, default=1800,
                 help="per-host timeout seconds (default 1800 for docker build)")
  p.add_argument("--dry-run", action="store_true")
  return p


def describe_mode(args):
  parts = ["git pull"]
  if args.ref:
    parts.insert(0, f"checkout {args.ref}")
  if args.skip_build:
    parts.append("service restart")
  else:
    parts.append("./docker-build.sh")
    parts.append("service restart")
  return " + ".join(parts)


def update_host(host, args, runner):
  script = update_script(
    args.repo_path,
    ref=args.ref,
    skip_build=bool(args.skip_build),
  )
  return runner.ssh(host, script, label=f"update eg_reporter {host}")


def main():
  args = build_parser().parse_args()
  args.ssh_password, _sudo = read_credentials(dry_run=args.dry_run, need_sudo=False)
  if args.ssh_password is None and not args.dry_run:
    return 1

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

  print(f"updating eg_reporter on {len(hosts)} host(s): {', '.join(hosts)}")
  print(f"mode: {describe_mode(args)}")
  print(f"repo: {args.repo_path}  unit: {SERVICE_UNIT}")

  failures = []
  workers = max(1, min(args.jobs, len(hosts)))
  with ThreadPoolExecutor(max_workers=workers) as pool:
    futures = {pool.submit(update_host, host, args, runner): host for host in hosts}
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
