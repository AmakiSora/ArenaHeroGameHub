#!/usr/bin/env python3
"""Diagnose why the remote ArenaGame container stopped/exited.

Connects to the VPS from deploy/.env.deploy and collects:
  - docker compose ps -a
  - docker inspect exit reason / restart count / timestamps
  - container logs (last N lines)
  - host disk / memory and recent OOM kills
"""

from __future__ import annotations

import sys
from pathlib import Path

import paramiko

ENV_FILE = Path(__file__).resolve().parent / ".env.deploy"


def load_env() -> dict[str, str]:
    env = {}
    with open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip()
    return env


def main() -> None:
    env = load_env()
    host = env.get("DEPLOY_HOST") or sys.exit("DEPLOY_HOST not set")
    port = int(env.get("DEPLOY_PORT", "22"))
    user = env.get("DEPLOY_USER", "root")
    password = env.get("DEPLOY_PASSWORD", "")
    base = env.get("DEPLOY_REMOTE_BASE", "/srv/arena-game")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port, user, password, timeout=15)
    print(f"Connected to {host}\n")

    def run(cmd: str) -> str:
        _, stdout, stderr = client.exec_command(cmd, timeout=60)
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
        return out + (f"\n[stderr] {err}" if err else "")

    checks = [
        ("== docker compose ps -a ==", f"cd {base} && docker compose ps -a"),
        (
            "== container exit info ==",
            "docker inspect -f '{{.Name}} | state={{.State.Status}} | exit={{.State.ExitCode}} "
            "| OOM={{.State.OOMKilled}} | restart={{.RestartCount}} | started={{.State.StartedAt}} "
            "| finished={{.State.FinishedAt}} | restartpolicy={{.HostConfig.RestartPolicy.Name}}' "
            "$(docker ps -aq) 2>&1",
        ),
        ("== docker compose logs (tail 200) ==", f"cd {base} && docker compose logs --tail=200 2>&1"),
        ("== host uptime/load ==", "uptime"),
        ("== disk ==", "df -h / /srv 2>&1"),
        ("== memory ==", "free -m"),
        ("== OOM kills in dmesg (last 20) ==", "dmesg -T 2>/dev/null | grep -iE 'killed process|out of memory|oom' | tail -20 || echo 'dmesg restricted'"),
        ("== docker events (last 30) ==", "docker events --since 24h --until 0s --format '{{.Time}} {{.Type}} {{.Action}} {{.Actor.Attributes.name}}' 2>&1 | tail -30"),
    ]

    for title, cmd in checks:
        print(title)
        print("-" * 60)
        print(run(cmd))
        print()

    client.close()


if __name__ == "__main__":
    main()
