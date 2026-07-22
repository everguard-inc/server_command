#!/usr/bin/env python3
"""CLI to send commands to edge agents."""

import argparse
import json
import multiprocessing
import sys

from servers_cfg import (
  edge_check_payload,
  edge_command_url,
  edge_service_payload,
  edge_update_payload,
  ensure_cli_command,
  load_servers_cfg,
  normalize_git_refs,
  post_edge_command,
  resolve_cli_pipelines,
)

RUN_COMMANDS = ("stream", "watchdog")
SERVICE_COMMANDS = ("service:start", "service:stop", "service:restart", "service:status")
ALLOWED_COMMANDS = ("check", "update", "stop", *RUN_COMMANDS, *SERVICE_COMMANDS)
PRINT_DETAIL_COMMANDS = (*RUN_COMMANDS, *SERVICE_COMMANDS, "update", "check")


def build_payload(command, pipeline_name, server, pipelines=None, git_refs=None):
  if command in RUN_COMMANDS:
    return json.dumps({
      "run": {
        "type": command,
        "json": server.get("default_cfg") or [pipeline_name],
        "path": server["eg_pipeline_path"],
      },
    })
  if command == "check":
    return edge_check_payload(server, name=pipeline_name)
  if command == "update":
    return edge_update_payload(
      [pipeline_name], pipelines or {pipeline_name: server}, git_refs=git_refs,
    )
  if command == "stop":
    return json.dumps({"stop": server["server_id"]})
  if command in SERVICE_COMMANDS:
    return edge_service_payload(command, server, name=pipeline_name)
  return None


def start_process(target, args):
  proc = multiprocessing.Process(target=target, args=args)
  proc.start()
  return proc


def join_processes(jobs):
  for proc in jobs:
    if proc:
      proc.join()


def send_cmd(pipeline_name, args, url, data):
  try:
    _, text = post_edge_command(url, data)
  except Exception as exc:
    print(f"FAIL {pipeline_name}: {exc}", file=sys.stderr)
    return
  print(url, data)
  if args.command not in PRINT_DETAIL_COMMANDS:
    return
  print("In Server:", pipeline_name)
  if args.command == "check":
    print("Current Running:", text)
  else:
    print("Status:", text)


def launch_for_server(pipeline_name, server, args, pipelines):
  git_refs = getattr(args, "git_refs", None)
  data = build_payload(args.command, pipeline_name, server, pipelines, git_refs=git_refs)
  if data is None:
    return None
  url = edge_command_url(server)
  print("Request send to:", server.get("server_ip"))
  print("Command:", args.command)
  return start_process(send_cmd, (pipeline_name, args, url, data))


def parse_git_ref_args(values):
  """Parse --git-ref REPO=REF entries into a dict."""
  refs = {}
  for raw in values or []:
    if "=" not in raw:
      print(f"invalid --git-ref {raw!r}; expected REPO=REF", file=sys.stderr)
      raise SystemExit(1)
    repo, ref = raw.split("=", 1)
    refs[repo.strip()] = ref.strip()
  return normalize_git_refs(refs)


if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument("-s", "--servers", type=str, nargs="+")
  parser.add_argument("-c", "--command", type=str, default="check")
  parser.add_argument(
    "--git-ref", action="append", default=[], metavar="REPO=REF",
    help="with update: checkout REPO at REF (repeatable; e.g. system_monitor=abc123)",
  )
  args = parser.parse_args()
  args.git_refs = parse_git_ref_args(args.git_ref)

  cfg = load_servers_cfg()
  ensure_cli_command(args.command, ALLOWED_COMMANDS)
  print("Command Accepted")

  targets = cfg if args.servers is None else {
    name: cfg[name] for name in resolve_cli_pipelines(cfg, args.servers)
  }
  jobs = []
  for name, server in targets.items():
    proc = launch_for_server(name, server, args, cfg)
    if proc:
      jobs.append(proc)
  join_processes(jobs)
