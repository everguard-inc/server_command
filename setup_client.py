#!/usr/bin/env python3
"""One-time edge client setup.

  python3 setup_client.py

Installs git credentials, pip dependencies, systemd unit, passwordless systemctl,
and restarts server_command_client.service.

If ~/.git-credentials is invalid (e.g. left from another user), it is removed and
setup prompts for fresh credentials.

Run as the edge user (not sudo directly). xSiteId is read from --site-id,
~/.bashrc, the existing unit file, or ~/.eg/site_id.
"""

import argparse
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

from edge_sudoers import SUDOERS_DEST, sudoers_body_bytes, systemctl_paths

REPO = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.basename(__file__)
REQUIREMENTS = os.path.join(REPO, "requirements.txt")
SERVICE_UNIT = "server_command_client.service"
SERVICE_PATH = f"/etc/systemd/system/{SERVICE_UNIT}"
SITE_ID_FILE = os.path.join(".eg", "site_id")
DEFAULT_REPO_URL = "https://github.com/everguard-inc/server_command.git"


def is_root():
    return os.geteuid() == 0


def user():
    if is_root():
        return os.environ.get("SUDO_USER")
    return pwd.getpwuid(os.geteuid()).pw_name


def home():
    name = user()
    return pwd.getpwnam(name).pw_dir if name else None


def creds_path():
    return os.path.join(home(), ".git-credentials")


def repo_dir():
    return os.path.join(home(), "server_command")


def install_repo_path():
    rd = repo_dir()
    if os.path.isfile(os.path.join(rd, "app.py")):
        return rd
    return REPO


def token_path():
    return os.path.join(home(), ".eg", "github_token")


def site_id_path():
    return os.path.join(home(), SITE_ID_FILE)


def git_env():
    return {**os.environ, "HOME": home(), "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}


def run(cmd, *, shell=False, input=None, capture=True, env=None, check=False):
    return subprocess.run(
        ["bash", "-lc", cmd] if shell else cmd,
        input=input,
        text=True,
        capture_output=capture,
        env=env or os.environ,
        check=check,
    )


def run_show(cmd, *, shell=False, env=None):
    print("+", cmd if shell else " ".join(shlex.quote(str(part)) for part in cmd))
    result = subprocess.run(
        ["bash", "-lc", cmd] if shell else cmd,
        env=env or os.environ,
    )
    return result.returncode


def pip_executable():
    return shutil.which("pip3") or shutil.which("pip")


def python_executable():
    return shutil.which("python3") or sys.executable


def git_store_cmd(repo, *args, creds=None):
    creds = creds or creds_path()
    helper = shlex.quote(f"store --file={creds}")
    return (
        f"git -c credential.helper= -c credential.helper={helper} "
        f"-C {shlex.quote(repo)} {' '.join(shlex.quote(a) for a in args)}"
    )


def resolve_repo_url(explicit=None):
    if explicit:
        return explicit.strip()
    if os.path.isdir(os.path.join(REPO, ".git")):
        result = run(["git", "-C", REPO, "remote", "get-url", "origin"], env=git_env())
        if result.returncode == 0:
            url = (result.stdout or "").strip()
            if url:
                return url
    return DEFAULT_REPO_URL


def git_ls_remote_url(repo_url, creds=None):
    creds = creds or creds_path()
    if not os.path.isfile(creds):
        return False
    helper = shlex.quote(f"store --file={creds}")
    cmd = (
        f"git -c credential.helper= -c credential.helper={helper} "
        f"ls-remote {shlex.quote(repo_url)} HEAD"
    )
    return run(cmd, shell=True, env=git_env()).returncode == 0


def git_remote_ok(repo, creds=None):
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        return False
    creds = creds or creds_path()
    if not os.path.isfile(creds):
        return False
    return run(git_store_cmd(repo, "ls-remote", "origin", "HEAD", creds=creds), shell=True, env=git_env()).returncode == 0


def credentials_file_ok(repo_url=None):
    path = creds_path()
    if not os.path.isfile(path):
        return False
    repo_url = repo_url or resolve_repo_url()
    for test_repo in (repo_dir(), REPO, install_repo_path()):
        if git_remote_ok(test_repo, creds=path):
            return True
    return git_ls_remote_url(repo_url, creds=path)


def remove_credentials_file():
    path = creds_path()
    if os.path.isfile(path):
        os.remove(path)
        print(f"removed {path}")


def reset_stale_credentials(args, repo_url):
    if args.force_git_credentials:
        return
    path = creds_path()
    if os.path.isfile(path) and not credentials_file_ok(repo_url):
        print(f"stale {path}; removing", file=sys.stderr)
        remove_credentials_file()


def ensure_repo_cloned(repo_url):
    repo_path = repo_dir()
    if os.path.isdir(os.path.join(repo_path, ".git")):
        return 0
    if not credentials_file_ok(repo_url):
        print(f"cannot clone {repo_url}: missing valid credentials", file=sys.stderr)
        return 1
    parent = os.path.dirname(repo_path)
    os.makedirs(parent, exist_ok=True)
    if run_show(["git", "clone", repo_url, repo_path], env=git_env()) != 0:
        return 1
    print(f"cloned {repo_url} -> {repo_path}")
    return 0


def parse_unit_env(path, key):
    if not os.path.isfile(path):
        return None
    pattern = re.compile(rf"^Environment={re.escape(key)}=(.+)$")
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            match = pattern.match(line.strip())
            if match:
                return match.group(1).strip()
    return None


def _expand_bash_value(value, user_home):
    value = value.strip().strip('"').strip("'")
    if user_home:
        value = value.replace("${HOME}", user_home).replace("$HOME", user_home)
    return value


def parse_bashrc_export(key, user_home=None):
    """Read export KEY=... from ~/.bashrc (supports ${HOME})."""
    user_home = user_home or home()
    if not user_home:
        return None
    bashrc = os.path.join(user_home, ".bashrc")
    if not os.path.isfile(bashrc):
        return None
    pattern = re.compile(rf"^\s*export\s+{re.escape(key)}=(.+)$")
    with open(bashrc, encoding="utf-8") as handle:
        for line in handle:
            match = pattern.match(line.strip())
            if match:
                value = _expand_bash_value(match.group(1), user_home)
                if value:
                    return value
    return None


def resolve_site_id(explicit):
    if explicit:
        return explicit.strip()
    from_unit = parse_unit_env(SERVICE_PATH, "xSiteId")
    if from_unit:
        return from_unit
    path = site_id_path()
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
            if value:
                return value
    from_bashrc = parse_bashrc_export("xSiteId")
    if from_bashrc:
        return from_bashrc
    from_env = os.environ.get("xSiteId", "").strip()
    if from_env:
        return from_env
    return None


def resolve_container_dir(explicit):
    if explicit:
        return os.path.abspath(explicit)
    from_unit = parse_unit_env(SERVICE_PATH, "CONTAINER_DIR")
    if from_unit:
        return from_unit
    from_bashrc = parse_bashrc_export("CONTAINER_DIR")
    if from_bashrc:
        return os.path.abspath(from_bashrc)
    from_env = os.environ.get("CONTAINER_DIR", "").strip()
    if from_env:
        return os.path.abspath(from_env)
    return os.path.join(home(), "containers")


def save_site_id(site_id):
    path = site_id_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(site_id.strip() + "\n")
    os.chmod(path, 0o600)
    if is_root():
        pw = pwd.getpwnam(user())
        os.chown(path, pw.pw_uid, pw.pw_gid)


def service_unit_text(*, site_id, container_dir, username, python_bin, repo):
    return f"""[Unit]
Description=Server Command Client
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
Restart=always
RestartSec=10
User={username}
WorkingDirectory={repo}/
Environment=xSiteId={site_id}
Environment=CONTAINER_DIR={container_dir}
ExecStart={python_bin} {repo}/app.py

[Install]
WantedBy=multi-user.target
"""


def _resolve_pip():
    if not os.path.isfile(REQUIREMENTS):
        print(f"missing {REQUIREMENTS}", file=sys.stderr)
        return None
    pip = pip_executable()
    if not pip:
        print("pip not found", file=sys.stderr)
        return None
    return pip


def _run_pip_steps(steps, *, env=None, flask_label=""):
    for step in steps:
        if run_show(step, shell=True, env=env) != 0:
            print(f"failed: {step}", file=sys.stderr)
            return 1
    if run_show("flask --version", shell=True, env=env) != 0:
        print(f"warning: {flask_label}flask --version failed", file=sys.stderr)
    return 0


def install_dependencies_root():
    pip = _resolve_pip()
    if not pip:
        return 1
    req = shlex.quote(REQUIREMENTS)
    return _run_pip_steps(
        [
            f"{shlex.quote(pip)} install --upgrade pip",
            f"{shlex.quote(pip)} install -r {req} --upgrade --ignore-installed --no-cache-dir",
        ],
        flask_label="sudo ",
    )


def install_dependencies_user():
    pip = _resolve_pip()
    if not pip:
        return 1
    req = shlex.quote(REQUIREMENTS)
    return _run_pip_steps(
        [f"{shlex.quote(pip)} install -r {req} --upgrade --force-reinstall --no-cache-dir"],
        env=git_env(),
    )


def install_systemd_service(site_id, container_dir):
    username = user()
    user_home = home()
    if not username or not user_home:
        print("could not determine edge user (run via sudo from a login user)", file=sys.stderr)
        return 1

    resolved_site_id = resolve_site_id(site_id)
    if not resolved_site_id:
        print("xSiteId is required on first install", file=sys.stderr)
        print("  set export xSiteId=... in ~/.bashrc, or", file=sys.stderr)
        print("  python3 setup_client.py --site-id <uuid>", file=sys.stderr)
        return 1

    resolved_container_dir = resolve_container_dir(container_dir)
    python_bin = python_executable()
    body = service_unit_text(
        site_id=resolved_site_id,
        container_dir=resolved_container_dir,
        username=username,
        python_bin=python_bin,
        repo=install_repo_path(),
    )

    existing = None
    if os.path.isfile(SERVICE_PATH):
        with open(SERVICE_PATH, encoding="utf-8") as handle:
            existing = handle.read()
    if existing == body:
        print(f"{SERVICE_PATH} already up to date")
    else:
        with open(SERVICE_PATH, "w", encoding="utf-8") as handle:
            handle.write(body)
        print(f"{'updated' if existing is not None else 'installed'} {SERVICE_PATH}")

    if site_id:
        save_site_id(resolved_site_id)

    systemctl = systemctl_paths()[0]
    for cmd in (
        [systemctl, "daemon-reload"],
        [systemctl, "enable", SERVICE_UNIT],
        [systemctl, "restart", SERVICE_UNIT],
    ):
        if run_show(cmd) != 0:
            print(f"failed: {' '.join(cmd)}", file=sys.stderr)
            return 1

    status = run([systemctl, "status", SERVICE_UNIT, "--no-pager", "-n", "20"], capture=False)
    return 0 if status.returncode in (0, 3) else status.returncode


def install_client_sudoers():
    name = user()
    if not name:
        print("run without sudo:", file=sys.stderr)
        print(f"  python3 {SCRIPT}", file=sys.stderr)
        return 1

    body = sudoers_body_bytes(name)

    if os.path.isfile(SUDOERS_DEST):
        with open(SUDOERS_DEST, "rb") as f:
            if f.read() == body:
                print("sudoers already up to date")
                return 0

    tmp = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(body)
            tmp = f.name
        subprocess.run(["install", "-o", "root", "-g", "root", "-m", "440", tmp, SUDOERS_DEST], check=True)
        subprocess.run(["visudo", "-cf", SUDOERS_DEST], check=True)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)

    print(f"installed {SUDOERS_DEST}")
    test = run(["sudo", "-u", name, "-H", systemctl_paths()[0], "is-active", SERVICE_UNIT], env=git_env())
    if test.returncode in (0, 3):
        print("sudo systemctl test ok")
        return 0
    print("sudo systemctl test failed", file=sys.stderr)
    if test.stderr or test.stdout:
        print((test.stderr or test.stdout).strip(), file=sys.stderr)
    return 1


def read_token(path, inline):
    if inline:
        return inline.strip()
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return os.environ.get("GITHUB_TOKEN", "").strip()


def write_credentials(token):
    pw = pwd.getpwnam(user())
    path = creds_path()
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(f"https://x-access-token:{token}@github.com\n".encode())
            tmp = f.name
        os.chmod(tmp, 0o600)
        if is_root():
            os.chown(tmp, pw.pw_uid, pw.pw_gid)
        shutil.move(tmp, path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def test_git(token):
    if not os.path.isdir(REPO):
        return True
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            f.write(f"https://x-access-token:{token}@github.com\n")
            tmp = f.name
        os.chmod(tmp, 0o600)
        return run(git_store_cmd(REPO, "ls-remote", "origin", "HEAD", creds=tmp), shell=True, env=git_env()).returncode == 0
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def validate_credentials(repo_url=None):
    path = creds_path()
    if not os.path.isfile(path):
        print(f"missing {path}", file=sys.stderr)
        return 1
    if credentials_file_ok(repo_url):
        print("git credential test ok")
        return 0
    print("git credential test failed", file=sys.stderr)
    return 1


def bootstrap_keyring():
    pw = pwd.getpwnam(user())
    env = {**os.environ, "HOME": home()}
    runtime = f"/run/user/{pw.pw_uid}"
    if os.path.isdir(runtime):
        env["XDG_RUNTIME_DIR"] = runtime
        bus = os.path.join(runtime, "bus")
        if os.path.exists(bus):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"

    fill = run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n", env=env)
    if fill.returncode != 0 or "username=" not in (fill.stdout or ""):
        return False
    store = run(["git", "credential-store", "store"], input=fill.stdout, env=env)
    return store.returncode == 0 and os.path.isfile(creds_path())


def setup_git(args, token_file, token_inline, repo_url):
    run(["git", "config", "--global", "--replace-all", "credential.helper", "store"]).check_returncode()
    print("git credential.helper=store configured")

    reset_stale_credentials(args, repo_url)

    path = creds_path()
    if os.path.isfile(path) and not args.force_git_credentials:
        if credentials_file_ok(repo_url):
            print(f"keeping {path}")
            return validate_credentials(repo_url)
        print(f"invalid {path}; removing", file=sys.stderr)
        remove_credentials_file()

    token = read_token(token_file, token_inline)
    if token:
        if not test_git(token):
            print("GitHub token validation failed", file=sys.stderr)
            return 1
        write_credentials(token)
        print(f"wrote {path}")
        code = ensure_repo_cloned(repo_url)
        if code != 0:
            return code
        return validate_credentials(repo_url)

    print("copying GitHub credentials from login keyring")
    if bootstrap_keyring():
        print(f"created {path}")
        code = ensure_repo_cloned(repo_url)
        if code != 0:
            return code
        return validate_credentials(repo_url)

    test_repo = install_repo_path()
    if os.path.isdir(test_repo):
        print(f"git -C {test_repo} pull")
        run(["git", "-C", test_repo, "pull"], env={**os.environ, "HOME": home()})
        if os.path.isfile(path):
            print(f"created {path}")
            return validate_credentials(repo_url)

    print(f"could not create {path}", file=sys.stderr)
    print(f"place a token in {token_file} or export GITHUB_TOKEN", file=sys.stderr)
    return 1


def sudo_self(*extra_args):
    cmd = [sys.executable, os.path.abspath(__file__), "--skip-git", *extra_args]
    print("installing via sudo:", " ".join(shlex.quote(part) for part in extra_args))
    sudopass = os.environ.get("SUDOPASS")
    if sudopass:
        env = os.environ.copy()
        env["SUDOPASS"] = sudopass
        if not env.get("SUDO_USER"):
            env["SUDO_USER"] = user() or ""
        return subprocess.run(
            ["sudo", "-S", "-E", "-p", "", *cmd],
            input=sudopass + "\n",
            text=True,
            env=env,
        ).returncode
    return subprocess.run(["sudo", *cmd]).returncode


def main():
    p = argparse.ArgumentParser(description="Edge client setup (deps, service, git, sudoers).")
    p.add_argument("--skip-git", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--skip-sudoers", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--skip-deps", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--skip-service", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--deps-root", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--install-service", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--site-id", default="", help="xSiteId for server_command_client.service")
    p.add_argument("--container-dir", default="", help="CONTAINER_DIR (default: ~/containers)")
    p.add_argument("--github-token-file", default=None)
    p.add_argument("--github-token", default="")
    p.add_argument("--force-git-credentials", action="store_true")
    p.add_argument("--repo-url", default="", help="git clone URL (default: origin of this repo)")
    args = p.parse_args()
    token_file = args.github_token_file or token_path()
    repo_url = resolve_repo_url(args.repo_url)

    if is_root():
        if args.deps_root:
            return install_dependencies_root()
        if args.install_service:
            return install_systemd_service(args.site_id, args.container_dir)
        if not args.skip_git:
            print("run without sudo:", file=sys.stderr)
            print(f"  python3 {SCRIPT}", file=sys.stderr)
            return 1
        return install_client_sudoers()

    if not args.skip_git:
        code = setup_git(args, token_file, args.github_token, repo_url)
        if code != 0:
            return code

    if not args.skip_deps:
        print("installing Python dependencies (user)")
        code = install_dependencies_user()
        if code != 0:
            return code
        code = sudo_self("--deps-root")
        if code != 0:
            return code

    if not args.skip_service:
        service_args = ["--install-service"]
        if args.site_id:
            service_args.extend(["--site-id", args.site_id])
        if args.container_dir:
            service_args.extend(["--container-dir", args.container_dir])
        code = sudo_self(*service_args)
        if code != 0:
            return code

    if args.skip_sudoers:
        return 0

    print("installing sudoers")
    return sudo_self()


if __name__ == "__main__":
    sys.exit(main())
