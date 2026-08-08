#!/usr/bin/env python3
"""Pull remote game_stats.json + last structured-log ticks to see the state at the transition."""

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
        ("== game_stats.json (remote) ==", "docker exec arena-game-app-1 cat /app/runtime/game_stats.json 2>&1"),
        ("== last 5 structured log ticks ==", "docker exec arena-game-app-1 tail -5 /app/runtime/tactic_log.jsonl 2>&1"),
        ("== structured log tail (compact) ==", "docker exec arena-game-app-1 sh -c 'tail -3 /app/runtime/tactic_log.jsonl | python3 -c \"import sys,json; [print(\\\"tick=\\\",j[\\\"tick\\\"],\\\"time=\\\",j[\\\"timestamp\\\"],\\\"core_state=\\\",j.get(\\\"core_state\\\"),\\\"core_hp=\\\",j.get(\\\"core_hp\\\"),\\\"core_shield=\\\",j.get(\\\"core_shield\\\"),\\\"pop=\\\",j.get(\\\"population\\\"),\\\"res=\\\",j.get(\\\"resources\\\"),sep=\\\" \\\") for j in map(json.loads, sys.stdin)]\"' 2>&1"),
        ("== tactic_log.jsonl total successful ticks ==", "docker exec arena-game-app-1 wc -l /app/runtime/tactic_log.jsonl 2>&1"),
        ("== play log: which sessions / how many connects ==", "docker exec arena-game-app-1 grep -c 'connecting session=' /app/runtime/tactic_play.log 2>&1"),
        ("== play log: distinct session numbers ==", "docker exec arena-game-app-1 grep -oE 'connecting session=[0-9]+' /app/runtime/tactic_play.log | sort | uniq -c 2>&1"),
        ("== play log: stream errors / unexpected / reconnecting ==", "docker exec arena-game-app-1 grep -nE 'stream error|unexpected error|reconnecting|submit_error' /app/runtime/tactic_play.log | tail -20 2>&1"),
    ]

    for title, cmd in checks:
        print(title)
        print("-" * 60)
        print(run(cmd))
        print()

    client.close()


if __name__ == "__main__":
    main()
