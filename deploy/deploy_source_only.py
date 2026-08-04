#!/usr/bin/env python3
"""Source-only deploy: upload changed .py/.md files, rebuild, preserve volume data."""
from __future__ import annotations

import sys
from pathlib import Path

import paramiko

ENV_FILE = Path(__file__).resolve().parent / ".env.deploy"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    with open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip()
    return env


env = load_env()
HOST = env.get("DEPLOY_HOST") or sys.exit("ERROR: DEPLOY_HOST not set")
PORT = int(env.get("DEPLOY_PORT", "22"))
USERNAME = env.get("DEPLOY_USER", "root")
PASSWORD = env.get("DEPLOY_PASSWORD", "")
REMOTE_BASE = env.get("DEPLOY_REMOTE_BASE", "/srv/arena-game")
APP_PORT = int(env.get("APP_PORT", "4399"))
LOCAL_BASE = Path(__file__).resolve().parents[1]

FILES = ("tactic.py", "tactic_config.py", "BEHAVIOR.md")

print(f"Connecting to {HOST}:{PORT} as {USERNAME}...")
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, PORT, USERNAME, PASSWORD, timeout=15)

sftp = client.open_sftp()
for name in FILES:
    local = LOCAL_BASE / name
    remote = f"{REMOTE_BASE}/{name}"
    print(f"  upload {name} -> {remote}")
    sftp.put(str(local), remote)
sftp.close()

print("Rebuilding and restarting container (volume untouched)...")
stdin, stdout, stderr = client.exec_command(
    f"cd {REMOTE_BASE} && docker compose up --build --detach 2>&1"
)
for line in iter(stdout.readline, ""):
    print(f"  {line}", end="")
exit_code = stdout.channel.recv_exit_status()
if exit_code != 0:
    err = stderr.read().decode()
    print(f"Build/start failed (exit={exit_code}): {err}")
    client.close()
    sys.exit(1)

print("Verifying...")
stdin, stdout, stderr = client.exec_command("sleep 3")
stdout.channel.recv_exit_status()
cmds = [
    "docker compose ps",
    f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{APP_PORT}/",
    f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{APP_PORT}/api/state",
    f"curl -s http://127.0.0.1:{APP_PORT}/api/config | head -c 400",
    "docker compose logs --tail=5 app",
]
for cmd in cmds:
    stdin, stdout, stderr = client.exec_command(f"cd {REMOTE_BASE} && {cmd}")
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    print(f"  $ {cmd}")
    print(f"  -> {out or err or '(empty)'}")

client.close()
print(f"\nDone! Dashboard: http://{HOST}:{APP_PORT}")
