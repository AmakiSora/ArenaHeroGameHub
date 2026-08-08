#!/usr/bin/env python3
"""Restart the arena-game app container on the VPS and verify the bot resumes.

Runtime data lives on the docker volume and survives the restart.
"""

from __future__ import annotations

import sys
import time
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
    dashboard_token = env.get("DASHBOARD_TOKEN", "")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port, user, password, timeout=15)

    def run(cmd: str, timeout: int = 120) -> tuple[int, str, str]:
        _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        code = stdout.channel.recv_exit_status()
        return code, out.strip(), err.strip()

    # 1. Restart
    print("Restarting app container...")
    code, out, err = run(f"cd {base} && docker compose restart app")
    print(f"  -> {out or err or '(empty)'}")

    # 2. Give it time to boot + tactic to reconnect
    print("Waiting 20s for boot + reconnect...")
    time.sleep(20)

    # 3. Container status
    code, out, err = run("docker compose ps")
    print(f"\n== docker compose ps ==\n{out or err}")

    # 4. API health
    code, out, err = run(
        f"curl -s -o /dev/null -w '%{{http_code}}' -H 'Authorization: Bearer {dashboard_token}' "
        f"http://127.0.0.1:4399/api/state"
    )
    print(f"\n== /api/state HTTP == {out}")

    # 5. New play-log lines after restart (tactic resume proof)
    code, out, err = run(
        "docker exec arena-game-app-1 sh -c "
        "'wc -l /app/runtime/tactic_play.log; tail -12 /app/runtime/tactic_play.log'"
    )
    print(f"\n== tactic_play.log tail ==\n{out or err}")

    client.close()


if __name__ == "__main__":
    main()
