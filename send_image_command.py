#!/usr/bin/env python3
"""CLI to send host-grouped or per-pipeline commands to edge agents."""

import argparse
import json
import sys

from send_command import (
  join_processes,
  parse_git_ref_args,
  send_cmd as send_pipeline_cmd,
  start_process,
)
from servers_cfg import (
  edge_check_payload,
  edge_command_url,
  edge_host_command_payload,
  ensure_cli_command,
  group_by_server_ip,
  load_servers_cfg,
  post_edge_command,
  resolve_cli_pipelines,
)

HOST_COMMANDS = ("stream", "watchdog", "update")
PIPELINE_COMMANDS = ("check", "stop")
ALLOWED_COMMANDS = (*HOST_COMMANDS, *PIPELINE_COMMANDS)


def build_pipeline_payload(command, image, pipeline_name):
  if command == "check":
    return edge_check_payload(image, name=pipeline_name)
  if command == "stop":
    return json.dumps({"stop": image["server_id"]})
  return None


def send_host_cmd(url, data):
  try:
    _, text = post_edge_command(url, data)
  except Exception as exc:
    print(f"FAIL: {exc}", file=sys.stderr)
    return
  print(url, data)
  print(text)


def launch_host_command(server_ip, names, args, cfg):
  first = cfg[names[0]]
  url = edge_command_url(first, host_ip=server_ip)
  git_refs = getattr(args, "git_refs", None)
  data = edge_host_command_payload(args.command, names, cfg, git_refs=git_refs)
  return start_process(send_host_cmd, (url, data))


def launch_pipeline_command(pipeline_name, args, cfg):
  image = cfg[pipeline_name]
  data = build_pipeline_payload(args.command, image, pipeline_name)
  if data is None:
    return None

  url = edge_command_url(image)
  print("Request send to:", image["server_ip"])
  print("Command:", args.command)
  return start_process(send_pipeline_cmd, (pipeline_name, args, url, data))


if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument("-s", "--servers", type=str, nargs="+", default=None,
                      help="pipeline name(s); same as send_command.py -s")
  parser.add_argument("-d", "--docker", type=str, nargs="+", default=None,
                      help=argparse.SUPPRESS)
  parser.add_argument("-c", "--command", type=str, default="stream")
  parser.add_argument(
    "--git-ref", action="append", default=[], metavar="REPO=REF",
    help="with update: checkout REPO at REF (repeatable; e.g. eg_pipeline=abc123)",
  )
  args = parser.parse_args()
  args.git_refs = parse_git_ref_args(args.git_ref)

  pipeline_filter = args.servers if args.servers is not None else args.docker
  cfg = load_servers_cfg()
  ensure_cli_command(args.command, ALLOWED_COMMANDS)
  print("Command Accepted")

  targets = resolve_cli_pipelines(cfg, pipeline_filter)
  jobs = []

  if args.command in HOST_COMMANDS:
    for server_ip, names in group_by_server_ip(targets, cfg).items():
      jobs.append(launch_host_command(server_ip, names, args, cfg))
  else:
    for pipeline_name in targets:
      proc = launch_pipeline_command(pipeline_name, args, cfg)
      if proc:
        jobs.append(proc)

  join_processes(jobs)
