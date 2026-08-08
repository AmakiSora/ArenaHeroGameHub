#!/usr/bin/env python3
"""Pull specific regions of the remote tactic_play.log to reconstruct the sequence."""

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
    host = env.get("DEPLOY_HOST")
    port = int(env.get("DEPLOY_PORT", "22"))
    user = env.get("DEPLOY_USER", "root")
    password = env.get("DEPLOY_PASSWORD", "")
    base = env.get("DEPLOY_REMOTE_BASE", "/srv/arena-game")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port, user, password, timeout=15)

    def run(cmd: str) -> str:
        _, stdout, stderr = client.exec_command(cmd, timeout=60)
        return stdout.read().decode(errors="replace").strip()

    checks = [
        ("== first 25 lines of play log ==", "docker exec arena-game-app-1 head -25 /app/runtime/tactic_play.log 2>&1"),
        ("== region around 17970 (ConnectionClosedError) ==", "docker exec arena-game-app-1 sed -n '17970,18000p' /app/runtime/tactic_play.log 2>&1"),
        ("== region around 13735 (connecting session=1) ==", "docker exec arena-game-app-1 sed -n '13735,13795p' /app/runtime/tactic_play.log 2>&1"),
        ("== region around 16185 (connecting session=1) ==", "docker exec arena-game-app-1 sed -n '16185,16240p' /app/runtime/tactic_play.log 2>&1"),
        ("== count of ConnectionClosedError tracebacks ==", "docker exec arena-game-app-1 grep -c 'ConnectionClosedError' /app/runtime/tactic_play.log 2>&1"),
        ("== container stdout (docker logs full) ==", "cd /srv/arena-game && docker compose logs 2>&1 | head -80"),
    ]

    for title, cmd in checks:
        print(title)
        print("-" * 60)
        print(run(cmd))
        print()

    client.close()


if __name__ == "__main__":
    main()
