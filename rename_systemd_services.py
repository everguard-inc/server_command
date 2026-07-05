#!/usr/bin/env python3
"""
Rename systemd unit files to server IDs listed in system_monitor/__services__.

Example __services__ line (with or without .service suffix):
    eg_pipeline_1    a1accf68-0424-456e-9b16-52acb30d82b2
    eg_pipeline_2.service    0cd2f071-678c-48cb-af66-03d928ee6eac

Stops running services in parallel, disables old names, renames unit files (or
recovers from a prior partial rename), daemon-reload, enables new names, and
starts previously active services in parallel.
eg_checker is restarted at the end if it was running.
"""

import argparse
import concurrent.futures
import os
import pwd
import shutil
import subprocess
import sys
from typing import NamedTuple

SYSTEMD_DIR = "/etc/systemd/system"
CHECKER_SERVICE = "eg_checker.service"
SERVICE_PREFIX = "eg_"


def login_home():
    """Home directory of the invoking user (not root when run via sudo)."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        return pwd.getpwnam(sudo_user).pw_dir
    return os.path.expanduser("~")


def default_services_file():
    return os.path.join(login_home(), "system_monitor", "__services__")


class PendingRename(NamedTuple):
    old_name: str
    new_name: str
    was_running: bool
    old_path: str
    new_path: str
    old_unit: str
    new_unit: str
    already_renamed: bool
    recover_only: bool


def normalize_service_name(name):
    """Strip optional .service suffix from a unit name token."""
    name = (name or "").strip()
    if name.endswith(".service"):
        return name[: -len(".service")]
    return name


def service_name_for(server_id):
    if server_id.startswith(SERVICE_PREFIX):
        return server_id
    return f"{SERVICE_PREFIX}{server_id}"


def run_cmd(cmd, check=False):
    print("+", " ".join(cmd))
    return subprocess.run(cmd, check=check)


def systemctl(*args):
    return run_cmd(["systemctl", *args]).returncode == 0


def is_service_active(name):
    return subprocess.run(
        ["systemctl", "is-active", "--quiet", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def parse_services_file(path):
    entries = []
    with open(path, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) < 2:
                print(f"Warning: skip malformed line {lineno}: {line!r}", file=sys.stderr)
                continue
            entries.append((normalize_service_name(parts[0]), parts[1], parts[2:]))
    return entries


def parallel_systemctl(action, names, dry_run):
    if not names:
        return
    if dry_run:
        for name in names:
            print(f"Would {action} {name}")
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as executor:
        futures = [executor.submit(systemctl, action, name) for name in names]
        concurrent.futures.wait(futures)


def collect_pending_renames(entries, systemd_dir, *, recover=False):
    pending = []
    ok = True
    for old_name, server_id, _extra in entries:
        new_name = service_name_for(server_id)
        new_unit = f"{new_name}.service"
        new_path = os.path.join(systemd_dir, new_unit)

        if old_name == new_name:
            if not recover:
                print(f"Skip {old_name}: service name already up to date")
                continue
            if not os.path.isfile(new_path):
                print(f"Warning: {new_path} not found, skipping", file=sys.stderr)
                ok = False
                continue
            print(f"Recover {new_unit} (verify and restart if needed)")
            pending.append(
                PendingRename(
                    old_name=old_name,
                    new_name=new_name,
                    was_running=True,
                    old_path=new_path,
                    new_path=new_path,
                    old_unit=new_unit,
                    new_unit=new_unit,
                    already_renamed=True,
                    recover_only=True,
                )
            )
            continue

        old_unit = f"{old_name}.service"
        old_path = os.path.join(systemd_dir, old_unit)
        was_running = is_service_active(old_name) or is_service_active(new_name)
        already_renamed = False

        if not os.path.isfile(old_path):
            if os.path.isfile(new_path):
                already_renamed = True
                print(f"Recover {old_unit} -> {new_unit} (unit file already renamed)")
            else:
                print(f"Warning: {old_path} not found, skipping", file=sys.stderr)
                ok = False
                continue
        elif os.path.exists(new_path):
            print(f"Error: {new_path} already exists", file=sys.stderr)
            ok = False
            continue

        pending.append(
            PendingRename(
                old_name=old_name,
                new_name=new_name,
                was_running=was_running,
                old_path=old_path,
                new_path=new_path,
                old_unit=old_unit,
                new_unit=new_unit,
                already_renamed=already_renamed,
                recover_only=False,
            )
        )

    return ok, pending


def container_name_for(service_name):
    if service_name.startswith(SERVICE_PREFIX):
        return service_name[len(SERVICE_PREFIX) :]
    return service_name


def is_container_running(name):
    result = subprocess.run(
        ["docker", "ps", "-q", "-f", f"name=^{name}$"],
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def docker_stop(name):
    return run_cmd(["docker", "stop", name]).returncode == 0


def parallel_docker_stop(names, dry_run):
    if not names:
        return
    if dry_run:
        for name in names:
            print(f"Would docker stop {name}")
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as executor:
        futures = [executor.submit(docker_stop, name) for name in names]
        concurrent.futures.wait(futures)


def cgroup_unit_for_pid(pid):
    cgroup_path = f"/proc/{pid}/cgroup"
    try:
        with open(cgroup_path, encoding="utf-8") as handle:
            for line in handle:
                if "system.slice" not in line:
                    continue
                unit = line.strip().rsplit("/", 1)[-1]
                if unit.endswith(".service"):
                    return normalize_service_name(unit)
    except OSError:
        pass
    return None


def find_orphan_units_from_processes(server_id, new_name):
    result = subprocess.run(
        ["pgrep", "-af", f"docker-run.sh watchdog {server_id}"],
        capture_output=True,
        text=True,
    )
    orphans = set()
    for line in result.stdout.splitlines():
        pid = line.split(None, 1)[0]
        unit = cgroup_unit_for_pid(pid)
        if unit and unit != new_name:
            orphans.add(unit)
    return orphans


def candidate_legacy_names(pending):
    """Old service names from __services__ that may still have stale systemd state."""
    return {p.old_name for p in pending if p.old_name != p.new_name}


def find_stale_enable_symlinks(systemd_dir, candidate_names):
    wants_dir = os.path.join(systemd_dir, "multi-user.target.wants")
    stale = set()
    if not os.path.isdir(wants_dir):
        return stale

    for name in candidate_names:
        link = os.path.join(wants_dir, f"{name}.service")
        if os.path.lexists(link):
            stale.add(name)
    return stale


def find_ghost_units(candidate_names):
    ghosts = set()
    for name in candidate_names:
        show = subprocess.run(
            ["systemctl", "show", "-p", "LoadState,ActiveState", "--value", name],
            capture_output=True,
            text=True,
        )
        values = show.stdout.strip().split("\n")
        load_state = values[0] if values else ""
        active_state = values[1] if len(values) > 1 else ""
        if load_state == "not-found" and active_state in ("active", "failed", "activating"):
            ghosts.add(name)
    return ghosts


def discover_legacy_units(pending, systemd_dir):
    valid_names = {p.new_name for p in pending}
    legacy = set()
    candidates = candidate_legacy_names(pending)

    for p in pending:
        server_id = container_name_for(p.new_name)
        legacy.update(find_orphan_units_from_processes(server_id, p.new_name))
        if p.already_renamed and p.old_name != p.new_name:
            legacy.add(p.old_name)

    legacy.update(find_stale_enable_symlinks(systemd_dir, candidates))
    legacy.update(find_ghost_units(candidates))
    return sorted(legacy - valid_names)


def remove_enable_symlink(unit_name, systemd_dir, dry_run):
    link = os.path.join(systemd_dir, "multi-user.target.wants", f"{unit_name}.service")
    if not os.path.lexists(link):
        return
    if dry_run:
        print(f"Would remove enable symlink {link}")
        return
    os.remove(link)
    print(f"Removed enable symlink {link}")


def cleanup_legacy_units(names, systemd_dir, dry_run):
    if not names:
        return

    # Ghost units (no unit file) are cleared by docker stop + reset-failed.
    # systemctl stop on them waits out TimeoutStopSec for orphan watchdogs.
    to_stop = []
    for name in names:
        unit_path = os.path.join(systemd_dir, f"{name}.service")
        if os.path.isfile(unit_path) and is_service_active(name):
            to_stop.append(name)
    parallel_systemctl("stop", to_stop, dry_run)

    for name in names:
        unit_path = os.path.join(systemd_dir, f"{name}.service")
        if os.path.isfile(unit_path):
            if dry_run:
                print(f"Would disable {name}")
            else:
                systemctl("disable", name)
        else:
            remove_enable_symlink(name, systemd_dir, dry_run)

    parallel_systemctl("reset-failed", names, dry_run)


def shutdown_old_services(pending, systemd_dir, dry_run, *, recover=False):
    to_stop = [
        p.old_name
        for p in pending
        if p.old_name != p.new_name and not p.recover_only and is_service_active(p.old_name)
    ]
    to_disable = [
        p.old_name
        for p in pending
        if p.old_name != p.new_name and not p.already_renamed and not p.recover_only
    ]

    # Discover before docker stop; orphan watchdog processes disappear after stop.
    legacy = []
    if recover or any(p.already_renamed or p.recover_only for p in pending):
        legacy = discover_legacy_units(pending, systemd_dir)

    parallel_systemctl("stop", to_stop, dry_run)
    parallel_systemctl("disable", to_disable, dry_run)

    containers = []
    for p in pending:
        if is_service_active(p.new_name):
            continue
        container = container_name_for(p.new_name)
        if is_container_running(container):
            containers.append(container)

    parallel_docker_stop(containers, dry_run)

    if legacy:
        print("Legacy units:", ", ".join(legacy))
        cleanup_legacy_units(legacy, systemd_dir, dry_run)

    old_names = [
        p.old_name
        for p in pending
        if not p.recover_only and p.old_name != p.new_name
    ]
    parallel_systemctl("reset-failed", old_names, dry_run)


def rename_unit_files(pending, dry_run):
    for p in pending:
        if p.already_renamed:
            continue
        if dry_run:
            print(f"Would rename {p.old_path} -> {p.new_path}")
            continue
        shutil.move(p.old_path, p.new_path)
        print(f"Renamed {p.old_unit} -> {p.new_unit}")


def activate_renamed_services(pending, dry_run, *, no_start=False):
    for p in pending:
        if dry_run:
            print(f"Would enable {p.new_name}")
            continue
        systemctl("enable", p.new_name)

    if no_start:
        if dry_run:
            for p in pending:
                if p.was_running or p.recover_only:
                    print(f"Would start {p.new_name} (skipped: --no-start)")
        return

    to_start = []
    for p in pending:
        should_start = p.recover_only or p.was_running
        if should_start and not is_service_active(p.new_name):
            if dry_run:
                print(f"Would start {p.new_name}")
            else:
                systemctl("reset-failed", p.new_name)
                to_start.append(p.new_name)

    parallel_systemctl("start", to_start, dry_run)


def update_services_file(path, pending, dry_run):
    rename_map = {p.old_name: p.new_name for p in pending}
    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue

        parts = stripped.split()
        service_name = normalize_service_name(parts[0]) if parts else ""
        if len(parts) >= 2 and service_name in rename_map:
            parts[0] = rename_map[service_name]
            sep = "\t" if "\t" in line else " "
            new_lines.append(sep.join(parts) + "\n")
        else:
            new_lines.append(line)

    if dry_run:
        print(f"Would update {path}:")
        for line in new_lines:
            print(f"  {line.rstrip()}")
        return

    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(new_lines)
    print(f"Updated {path}")


def restart_checker_if_running(was_running, dry_run):
    if dry_run:
        if was_running:
            print(f"Would enable/restart {CHECKER_SERVICE} (was running)")
        else:
            print(f"Skip {CHECKER_SERVICE} (was not running)")
        return
    if not was_running:
        return
    systemctl("enable", CHECKER_SERVICE)
    systemctl("restart", CHECKER_SERVICE)


def main():
    parser = argparse.ArgumentParser(
        description="Rename systemd services to server IDs from __services__"
    )
    services_default = default_services_file()
    parser.add_argument(
        "-f",
        "--file",
        default=services_default,
        help=f"path to __services__ file (default: {services_default})",
    )
    parser.add_argument(
        "--systemd-dir",
        default=SYSTEMD_DIR,
        help=f"systemd unit directory (default: {SYSTEMD_DIR})",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="show planned actions without changing files or systemd state",
    )
    parser.add_argument(
        "--recover",
        action="store_true",
        help="verify and fix services even when names already match __services__",
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="rename and enable only; do not restart renamed pipeline services",
    )
    parser.add_argument(
        "--no-update-file",
        action="store_true",
        help="do not rewrite __services__",
    )
    args = parser.parse_args()

    if os.geteuid() != 0 and not args.dry_run:
        print("Run as root: sudo python3 rename_systemd_services.py", file=sys.stderr)
        return 1

    if not os.path.isfile(args.file):
        print(f"Services file not found: {args.file}", file=sys.stderr)
        return 1

    entries = parse_services_file(args.file)
    if not entries:
        print("No service entries found.", file=sys.stderr)
        return 1

    ok, pending = collect_pending_renames(entries, args.systemd_dir, recover=args.recover)
    if not pending:
        print("Nothing to do.")
        return 0 if ok else 1

    checker_was_running = is_service_active(normalize_service_name(CHECKER_SERVICE))

    shutdown_old_services(pending, args.systemd_dir, args.dry_run, recover=args.recover)
    rename_unit_files(pending, args.dry_run)

    if not args.dry_run:
        systemctl("daemon-reload")
    else:
        print("Would run: systemctl daemon-reload")

    activate_renamed_services(pending, args.dry_run, no_start=args.no_start)

    restart_checker_if_running(checker_was_running, args.dry_run)

    if not args.no_update_file:
        update_services_file(args.file, pending, args.dry_run)

    if not args.dry_run:
        print("Done.")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
