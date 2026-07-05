"""Shared sudoers/systemctl helpers for deploy_clients and setup_client."""

import os
import shlex
import shutil

SUDOERS_DEST = "/etc/sudoers.d/server_command"
REMOTE_SHELL = ("env", "-u", "BASH_ENV", "bash", "--norc", "--noprofile")


def systemctl_paths():
  paths = []
  for candidate in (shutil.which("systemctl"), "/usr/bin/systemctl", "/bin/systemctl"):
    if candidate and os.path.isfile(candidate):
      real = os.path.realpath(candidate)
      if real not in paths:
        paths.append(real)
  return paths or ["/usr/bin/systemctl"]


def sudoers_body(username):
  paths = ", ".join(systemctl_paths())
  return (
    f"# Passwordless systemctl for server_command client (User={username}).\n"
    f"Cmnd_Alias EG_SERVER_COMMAND_SYSTEMCTL = {paths}\n\n"
    f"{username} ALL=(root) NOPASSWD: EG_SERVER_COMMAND_SYSTEMCTL\n"
  )


def sudoers_body_bytes(username):
  return sudoers_body(username).encode()
