"""Shared git helpers for version lookup (proxy backfill and edge agent)."""

import os
import subprocess

GIT_VERSION_TIMEOUT = 12


def is_git_repo_dir(path):
  return bool(path and os.path.isdir(os.path.join(path, ".git")))


def parse_ls_remote_heads(text):
  chosen = None
  for line in (text or "").splitlines():
    parts = line.strip().split()
    if len(parts) != 2:
      continue
    sha, ref = parts
    if ref in ("HEAD", "refs/heads/main", "refs/heads/master"):
      chosen = sha
      break
    if chosen is None:
      chosen = sha
  if not chosen:
    return None, None
  return chosen, chosen[:7]


def git_run(repo_path, *args, timeout=None, extra_args=()):
  cmd = ["git", *extra_args, "-C", repo_path, *args]
  kwargs = {
    "stdout": subprocess.PIPE,
    "stderr": subprocess.PIPE,
    "text": True,
    "env": {**os.environ, "GIT_TERMINAL_PROMPT": "0"},
  }
  if timeout is not None:
    kwargs["timeout"] = timeout
  return subprocess.run(cmd, **kwargs)


def git_commit_date(repo_path, ref, *, git_runner=None):
  if not repo_path or not ref:
    return None
  run = git_runner or git_run
  result = run(repo_path, "show", "-s", "--format=%ci", ref)
  if result.returncode != 0:
    return None
  text = (result.stdout or "").strip()
  return text[:16] if text else None


def git_latest_remote(repo_path, *, timeout=GIT_VERSION_TIMEOUT, git_runner=None):
  if not is_git_repo_dir(repo_path):
    return None, None
  run = git_runner or git_run
  result = run(repo_path, "ls-remote", "--heads", "origin", timeout=timeout)
  if result.returncode != 0:
    return None, None
  return parse_ls_remote_heads(result.stdout)


def git_latest_remote_url(remote_url, *, timeout=GIT_VERSION_TIMEOUT):
  result = subprocess.run(
    ["git", "ls-remote", "--heads", remote_url],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    timeout=timeout,
    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
  )
  if result.returncode != 0:
    return None, None
  return parse_ls_remote_heads(result.stdout)
