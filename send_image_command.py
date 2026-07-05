#!/usr/bin/env python3
"""CLI to send host-grouped or per-pipeline commands to edge agents."""

import argparse
import sys

from send_command import build_payload, join_processes, send_cmd, start_process
from servers_cfg import (
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


def launch_host_command(server_ip, names, args, cfg):
  first = cfg[names[0]]
  url = edge_command_url(first, host_ip=server_ip)
  data = edge_host_command_payload(args.command, names, cfg)
  return start_process(send_cmd, (names[0], args, url, data))


def launch_pipeline_command(pipeline_name, args, cfg):
  server = cfg[pipeline_name]
  data = build_payload(args.command, pipeline_name, server, cfg)
  if data is None:
    return None

  url = edge_command_url(server)
  print("Request send to:", server.get("server_ip"))
  print("Target Image:", pipeline_name)
  print("Command:", args.command)
  return start_process(send_cmd, (pipeline_name, args, url, data))


if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument("-s", "--servers", type=str, nargs="+", default=None)
  parser.add_argument("-d", "--docker", type=str, nargs="+", default=None,
                      help=argparse.SUPPRESS)
  parser.add_argument("-c", "--command", type=str, default="stream")
  args = parser.parse_args()

  cfg = load_servers_cfg()
  ensure_cli_command(args.command, ALLOWED_COMMANDS)
  print("Command Accepted")

  only = args.servers if args.servers is not None else args.docker
  targets = resolve_cli_pipelines(cfg, only)
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
