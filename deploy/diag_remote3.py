#!/usr/bin/env python3
"""Inspect remote tactic_play.log: first mismatch, count, and the lines around the transition."""

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
        ("count TICK_MISMATCH", "docker exec arena-game-app-1 grep -c '409 TICK_MISMATCH' /app/runtime/tactic_play.log 2>&1"),
        ("first TICK_MISMATCH line number", "docker exec arena-game-app-1 grep -n '409 TICK_MISMATCH' /app/runtime/tactic_play.log | head -1 2>&1"),
        ("total lines in play log", "docker exec arena-game-app-1 wc -l /app/runtime/tactic_play.log 2>&1"),
        ("40 lines BEFORE first mismatch", "docker exec arena-game-app-1 sh -c 'N=$(grep -n \"409 TICK_MISMATCH\" /app/runtime/tactic_play.log | head -1 | cut -d: -f1); sed -n \"$((N-40)),$((N+8))p\" /app/runtime/tactic_play.log' 2>&1"),
        ("last 60 lines", "docker exec arena-game-app-1 tail -60 /app/runtime/tactic_play.log 2>&1"),
        ("any session/reconnect lines", "docker exec arena-game-app-1 grep -nE 'session=|reconnecting|stream error|unexpected error|stop|close|quit|game over|ended' /app/runtime/tactic_play.log | tail -30 2>&1"),
    ]

    for title, cmd in checks:
        print(title)
        print("-" * 60)
        print(run(cmd))
        print()

    client.close()


if __name__ == "__main__":
    main()
