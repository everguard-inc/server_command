"""Shared git helpers for edge app and central proxy version lookups."""

import os
import subprocess

DEFAULT_GIT_VERSION_TIMEOUT = 12

# Prefer these when current checkout has no usable upstream on origin.
_DEFAULT_REMOTE_BRANCH_FALLBACKS = ("main", "master")


def is_git_repo_dir(path):
  return bool(path and os.path.isdir(os.path.join(path, ".git")))


def git_run(repo_path, *args, timeout=None, env=None):
  if not repo_path:
    return subprocess.CompletedProcess(args, 1, "", "missing repo path")
  cmd = ["git", "-C", repo_path, *args]
  kwargs = {
    "stdout": subprocess.PIPE,
    "stderr": subprocess.PIPE,
    "text": True,
    "env": {**(env or os.environ), "GIT_TERMINAL_PROMPT": "0"},
  }
  if timeout is not None:
    kwargs["timeout"] = timeout
  return subprocess.run(cmd, **kwargs)


def _git_runner(git_cmd=None, timeout=None, env=None):
  if git_cmd is not None:
    return git_cmd
  return lambda repo, *a, **kw: git_run(
    repo, *a, timeout=kw.get("timeout", timeout), env=env,
  )


def git_short_rev(repo_path, ref="HEAD", *, git_cmd=None, timeout=None):
  if not is_git_repo_dir(repo_path):
    return None
  run = _git_runner(git_cmd, timeout=timeout)
  result = run(repo_path, "rev-parse", "--short", ref, timeout=timeout)
  if result.returncode != 0:
    return None
  return (result.stdout or "").strip() or None


def git_upstream_branch(repo_path, *, git_cmd=None, timeout=None):
  """Return remote branch name for @{upstream}, e.g. 'forklift_proximity'."""
  if not is_git_repo_dir(repo_path):
    return None
  run = _git_runner(git_cmd, timeout=timeout)
  result = run(
    repo_path,
    "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}",
    timeout=timeout,
  )
  if result.returncode != 0:
    return None
  upstream = (result.stdout or "").strip()
  if upstream.startswith("origin/"):
    return upstream[len("origin/"):]
  if "/" in upstream:
    return upstream.split("/", 1)[1]
  return upstream or None


def git_current_branch(repo_path, *, git_cmd=None, timeout=None):
  if not is_git_repo_dir(repo_path):
    return None
  run = _git_runner(git_cmd, timeout=timeout)
  result = run(repo_path, "rev-parse", "--abbrev-ref", "HEAD", timeout=timeout)
  if result.returncode != 0:
    return None
  branch = (result.stdout or "").strip()
  if not branch or branch == "HEAD":
    return None
  return branch


def preferred_remote_branches(repo_path, *, git_cmd=None, timeout=None):
  """Ordered branch names to treat as 'latest' for this checkout."""
  ordered = []
  seen = set()

  def add(name):
    if name and name not in seen:
      seen.add(name)
      ordered.append(name)

  add(git_upstream_branch(repo_path, git_cmd=git_cmd, timeout=timeout))
  add(git_current_branch(repo_path, git_cmd=git_cmd, timeout=timeout))

  repo_name = os.path.basename(os.path.realpath(repo_path or ""))
  if repo_name == "forklift_proximity":
    add("forklift_proximity")

  for name in _DEFAULT_REMOTE_BRANCH_FALLBACKS:
    add(name)
  return ordered


def git_ls_remote_origin(repo_path, *, git_cmd=None, timeout=DEFAULT_GIT_VERSION_TIMEOUT, env=None):
  if not is_git_repo_dir(repo_path):
    return None, None
  run = _git_runner(git_cmd, timeout=timeout, env=env)
  result = run(repo_path, "ls-remote", "--heads", "origin", timeout=timeout)
  if result.returncode != 0:
    return None, None
  preferred = preferred_remote_branches(repo_path, git_cmd=run, timeout=timeout)
  return parse_ls_remote_heads(result.stdout, preferred_branches=preferred)


def git_ls_remote_url(remote_url, *, timeout=DEFAULT_GIT_VERSION_TIMEOUT, env=None, preferred_branches=None):
  result = subprocess.run(
    ["git", "ls-remote", "--heads", remote_url],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    timeout=timeout,
    env={**(env or os.environ), "GIT_TERMINAL_PROMPT": "0"},
  )
  if result.returncode != 0:
    return None, None
  return parse_ls_remote_heads(result.stdout, preferred_branches=preferred_branches)


def git_commit_date(repo_path, ref, *, timeout=None, env=None, git_cmd=None):
  if not repo_path or not ref:
    return None
  run = _git_runner(git_cmd, timeout=timeout, env=env)
  result = run(repo_path, "show", "-s", "--format=%ci", ref, timeout=timeout)
  if result.returncode != 0:
    return None
  text = (result.stdout or "").strip()
  return text[:16] if text else None


def read_local_repo_versions(
  repo_path,
  *,
  git_cmd=None,
  timeout=DEFAULT_GIT_VERSION_TIMEOUT,
  include_remote=True,
):
  """Return (current, current_date, latest, latest_date) for a local git checkout.

  include_remote=False skips git ls-remote/fetch (edge status path): only HEAD.
  Latest is then filled by the central proxy from its own checkouts/cache.
  """
  run = _git_runner(git_cmd, timeout=timeout)
  current = git_short_rev(repo_path, git_cmd=run, timeout=timeout)
  current_date = git_commit_date(repo_path, "HEAD", timeout=timeout, git_cmd=run) if current else None
  if not include_remote:
    return current, current_date, None, None

  full_sha, latest = git_ls_remote_origin(repo_path, git_cmd=run, timeout=timeout)
  latest_date = None
  if full_sha:
    latest_date = git_commit_date(repo_path, full_sha, timeout=timeout, git_cmd=run)
    if not latest_date:
      run(repo_path, "fetch", "origin", full_sha, "--depth=1", "--quiet", timeout=timeout)
      latest_date = git_commit_date(repo_path, full_sha, timeout=timeout, git_cmd=run)
  if current and latest and current == latest and current_date:
    latest_date = current_date
  return current, current_date, latest, latest_date


def read_tracking_latest(repo_path, *, git_cmd=None, timeout=DEFAULT_GIT_VERSION_TIMEOUT):
  """Latest from local origin/* refs only (no network)."""
  if not is_git_repo_dir(repo_path):
    return None, None
  run = _git_runner(git_cmd, timeout=timeout)
  for branch in preferred_remote_branches(repo_path, git_cmd=run, timeout=timeout):
    ref = f"origin/{branch}"
    latest = git_short_rev(repo_path, ref, git_cmd=run, timeout=timeout)
    if not latest:
      continue
    latest_date = git_commit_date(repo_path, ref, timeout=timeout, git_cmd=run)
    return latest, latest_date
  return None, None


def read_repo_latest(repo_path, *, git_cmd=None, remote_url=None, timeout=DEFAULT_GIT_VERSION_TIMEOUT):
  """Latest remote version for a local repo or HTTPS remote URL.

  Prefers already-fetched origin/* tips (no network), then ls-remote.
  """
  if is_git_repo_dir(repo_path):
    latest, latest_date = read_tracking_latest(
      repo_path, git_cmd=git_cmd, timeout=timeout,
    )
    if latest:
      return latest, latest_date
    full_sha, latest = git_ls_remote_origin(repo_path, git_cmd=git_cmd, timeout=timeout)
    if not full_sha:
      return None, None
    latest_date = git_commit_date(repo_path, full_sha, timeout=timeout, git_cmd=git_cmd)
    if not latest_date:
      run = _git_runner(git_cmd, timeout=timeout)
      run(repo_path, "fetch", "origin", full_sha, "--depth=1", "--quiet", timeout=timeout)
      latest_date = git_commit_date(repo_path, full_sha, timeout=timeout, git_cmd=git_cmd)
    return latest, latest_date

  if not remote_url:
    return None, None
  preferred = None
  # Python 3.8 has no str.removesuffix
  repo_basename = str(remote_url).rstrip("/")
  if repo_basename.endswith(".git"):
    repo_basename = repo_basename[:-4]
  repo_name = os.path.basename(repo_basename)
  if repo_name == "forklift_proximity":
    preferred = ("forklift_proximity", "main", "master")
  _full_sha, latest = git_ls_remote_url(
    remote_url, timeout=timeout, preferred_branches=preferred,
  )
  if not latest:
    return None, None
  return latest, None


def parse_ls_remote_heads(text, preferred_branches=None):
  """Pick the tip SHA for the preferred remote branch.

  Prefer checkout-specific branches (upstream/current/forklift) before main/master,
  so forklift checkouts are not compared against eg_pipeline master.
  """
  heads = {}
  for line in (text or "").splitlines():
    parts = line.strip().split()
    if len(parts) != 2:
      continue
    sha, ref = parts
    heads[ref] = sha

  if not heads:
    return None, None

  for branch in preferred_branches or ():
    ref = branch if branch.startswith("refs/") else f"refs/heads/{branch}"
    if ref in heads:
      sha = heads[ref]
      return sha, sha[:7]

  for ref in ("HEAD", "refs/heads/main", "refs/heads/master"):
    if ref in heads:
      sha = heads[ref]
      return sha, sha[:7]

  # Stable fallback: first listed head (ls-remote order).
  sha = next(iter(heads.values()))
  return sha, sha[:7]
