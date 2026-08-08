#!/usr/bin/env python3
"""Deep-dive into the running arena-game container: processes, API health,
runtime log tails, and last activity timestamps."""

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
    print(f"Connected to {host}\n")

    def run(cmd: str) -> str:
        _, stdout, stderr = client.exec_command(cmd, timeout=60)
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
        return out + (f"\n[stderr] {err}" if err else "")

    checks = [
        ("== processes inside container ==", "docker exec arena-game-app-1 ps aux 2>&1"),
        ("== dashboard health from inside ==",
         "docker exec arena-game-app-1 sh -c \"curl -s -o /dev/null -w 'root HTTP %{http_code}\\n' http://127.0.0.1:4399/; "
         "curl -s -m 5 http://127.0.0.1:4399/api/state | head -c 2000; echo\" 2>&1"),
        ("== tactic_play.log (tail 30) ==",
         "docker exec arena-game-app-1 sh -c 'tail -30 /app/runtime/tactic_play.log 2>&1 || echo no-file'"),
        ("== tactic_log.jsonl last line + count ==",
         "docker exec arena-game-app-1 sh -c 'wc -l /app/runtime/tactic_log.jsonl 2>&1; tail -1 /app/runtime/tactic_log.jsonl 2>&1 | head -c 1000; echo'"),
        ("== dashboard.log ==", "docker exec arena-game-app-1 sh -c 'tail -30 /app/runtime/dashboard.log 2>&1 || ls -la /app/runtime'"),
        ("== runtime dir listing ==", "docker exec arena-game-app-1 ls -la /app/runtime"),
        ("== mtime of runtime files (now on host) ==",
         "date -u; docker exec arena-game-app-1 sh -c 'for f in /app/runtime/*.json /app/runtime/*.log /app/runtime/*.jsonl; do [ -f \"$f\" ] && echo \"$(stat -c %y \"$f\") $f\"; done'"),
        ("== is tactic connected to Arena Hero? (netstat) ==",
         "docker exec arena-game-app-1 sh -c 'cat /proc/net/tcp | wc -l; ss -tnp 2>/dev/null | head -30 || netstat -tnp 2>/dev/null | head -30'"),
        ("== recent docker events for arena-game-app-1 ==",
         "docker events --since 12h --until 0s --filter container=arena-game-app-1 --format '{{.Time}} {{.Action}}' 2>&1 | tail -20"),
    ]

    for title, cmd in checks:
        print(title)
        print("-" * 60)
        print(run(cmd))
        print()

    client.close()


if __name__ == "__main__":
    main()
