#!/usr/bin/env python3
"""Deploy ArenaGame to remote server via SFTP + Docker Compose.

Credentials are read from deploy/.env.deploy (not tracked in git).
Copy deploy/.env.deploy.example to deploy/.env.deploy and fill in the values.

Dashboard is published on TCP port 4399.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import paramiko

ENV_FILE = Path(__file__).resolve().parent / ".env.deploy"


def load_env() -> dict[str, str]:
    """Load deploy credentials from .env.deploy file."""
    if not ENV_FILE.exists():
        print(f"ERROR: {ENV_FILE} not found.")
        print(
            "Copy deploy/.env.deploy.example to deploy/.env.deploy and fill in the values."
        )
        sys.exit(1)

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


# --- Load credentials from .env.deploy ---
env = load_env()

HOST = env.get("DEPLOY_HOST") or sys.exit("ERROR: DEPLOY_HOST not set in .env.deploy")
PORT = int(env.get("DEPLOY_PORT", "22"))
USERNAME = env.get("DEPLOY_USER", "root")
PASSWORD = env.get("DEPLOY_PASSWORD", "")
REMOTE_BASE = env.get("DEPLOY_REMOTE_BASE", "/srv/arena-game")
ARENA_HERO_API_KEY = env.get("ARENA_HERO_API_KEY", "")
DASHBOARD_TOKEN = env.get("DASHBOARD_TOKEN", "")
LOG_LEVEL = env.get("LOG_LEVEL", "info")
APP_PORT = int(env.get("APP_PORT", "4399"))
LOCAL_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXCLUDE_PATTERNS = [
    r"\.git$",
    r"\.git/.*",
    r"node_modules/",
    r"node_modules$",
    r"\.env$",
    r"\.env\..*",
    r"dist/",
    r"dist$",
    r"coverage/",
    r"coverage$",
    r"\.idea/",
    r"\.idea$",
    r"\.claude/",
    r"\.claude$",
    r"\.omp/",
    r"\.omp$",
    r"\.workbuddy/",
    r"\.workbuddy$",
    r"\.github/",
    r"\.github$",
    r"\.pi/",
    r"\.pi$",
    r"__pycache__/",
    r"__pycache__$",
    r"runtime/",
    r"runtime$",
    r"backups/",
    r"backups$",
    r"tests/",
    r"tests$",
    r"deploy/",
    r"deploy$",
    r"\.pyc$",
    r"direct_debug\.json$",
    r"direct_play\.log$",
    r"dashboard\.log$",
    r"dash.*\.log$",
    # Runtime state is uploaded separately into the Docker volume.
    r"^map_memory\.json$",
    r"^tactic_config\.json$",
    r"^tactic_log\.jsonl$",
    r"^tactic_play\.log$",
    r"^battle_log\.jsonl$",
    r"^battle_log\.jsonl\.lock$",
    r"^_.*\.py$",
    r"^fix_.*\.py$",
    # watchdog.py ships intentionally: tactic.choose_actions feeds it the
    # per-tick unit snapshot for the stall_alert lines.
    r"^diagnose\.py$",
    r"^direct_wrapper\.py$",
    r"^nul$",
]

# Local runtime state seeded into a FRESH remote Docker volume only. On an
# existing server these files are live data and are never overwritten.
RUNTIME_SEED_FILES = (
    "map_memory.json",
    "tactic_config.json",
    "game_stats.json",
    "tactic_log.jsonl",
    "tactic_play.log",
)


def should_exclude(rel_path: str) -> bool:
    for pattern in EXCLUDE_PATTERNS:
        if re.search(pattern, rel_path):
            return True
    return False


def ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    try:
        sftp.stat(remote_dir)
        return
    except FileNotFoundError:
        pass

    parts = remote_dir.replace(REMOTE_BASE, "").strip("/").split("/")
    path = REMOTE_BASE
    for part in parts:
        if not part:
            continue
        path = f"{path}/{part}"
        try:
            sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path)


def transfer_files(sftp: paramiko.SFTPClient) -> tuple[int, int]:
    """Transfer project files to remote server."""
    file_count = 0
    total_size = 0

    for root, dirs, files in os.walk(LOCAL_BASE):
        filtered = []
        for d in dirs:
            local_dir = os.path.join(root, d)
            try:
                rel = os.path.relpath(local_dir, LOCAL_BASE).replace("\\", "/")
            except ValueError:
                continue
            if should_exclude(rel):
                continue
            filtered.append(d)
        dirs[:] = filtered

        for f in files:
            local_path = os.path.join(root, f)
            try:
                rel_path = os.path.relpath(local_path, LOCAL_BASE).replace("\\", "/")
            except ValueError:
                continue

            if should_exclude(rel_path):
                continue

            remote_path = f"{REMOTE_BASE}/{rel_path}"
            remote_dir = os.path.dirname(remote_path)
            ensure_remote_dir(sftp, remote_dir)

            sftp.put(local_path, remote_path)
            file_count += 1
            total_size += os.path.getsize(local_path)

            if file_count % 50 == 0:
                print(f"  Transferred {file_count} files...")

    return file_count, total_size


def write_remote_env(client: paramiko.SSHClient) -> None:
    if not ARENA_HERO_API_KEY:
        print("ERROR: ARENA_HERO_API_KEY is not set in .env.deploy")
        sys.exit(1)
    if not DASHBOARD_TOKEN:
        print("ERROR: DASHBOARD_TOKEN is not set in .env.deploy")
        sys.exit(1)

    env_content = (
        f"ARENA_HERO_API_KEY={ARENA_HERO_API_KEY}\n"
        f"DASHBOARD_TOKEN={DASHBOARD_TOKEN}\n"
        f"LOG_LEVEL={LOG_LEVEL}\n"
    )
    stdin, stdout, stderr = client.exec_command(
        f"cat > {REMOTE_BASE}/.env << 'ENVEOF'\n{env_content}ENVEOF\n"
        f"chmod 600 {REMOTE_BASE}/.env"
    )
    stdout.channel.recv_exit_status()
    err = stderr.read().decode().strip()
    if err:
        print(f"  warn writing .env: {err}")


def seed_runtime_data(client: paramiko.SSHClient, sftp: paramiko.SFTPClient) -> None:
    """Bootstrap local runtime files into the Docker volume on first launch.

    Files already present in the volume (dashboard-set config, discovered map
    memory, stats, logs) are kept as-is — seeding only fills an empty volume so
    a brand-new server starts from the local state instead of defaults.
    """
    remote_seed = f"{REMOTE_BASE}/.runtime-seed"
    print("Seeding runtime data from local machine...")
    stdin, stdout, stderr = client.exec_command(f"mkdir -p {remote_seed}")
    stdout.channel.recv_exit_status()

    uploaded = []
    for name in RUNTIME_SEED_FILES:
        local_path = os.path.join(LOCAL_BASE, name)
        if not os.path.isfile(local_path):
            print(f"  skip missing {name}")
            continue
        size = os.path.getsize(local_path)
        print(f"  upload {name} ({size / 1024 / 1024:.1f} MB)" if size > 1024 * 1024 else f"  upload {name} ({size / 1024:.1f} KB)")
        sftp.put(local_path, f"{remote_seed}/{name}")
        uploaded.append(name)

    if not uploaded:
        print("  no local runtime files found to seed")
        return

    # Ensure volume exists, stop app so sqlite/log writers release files, then copy.
    # Bootstrap-only: a file already present in the volume is LIVE data (dashboard
    # config, map memory, stats, logs) and must NOT be overwritten by local state.
    seed_cmd = f"""
set -e
cd {REMOTE_BASE}
docker compose stop app >/dev/null 2>&1 || true
docker volume create arena-game-runtime >/dev/null
docker run --rm \
  -v arena-game-runtime:/data \
  -v {remote_seed}:/seed:ro \
  alpine:3.21 sh -c '
    set -e
    mkdir -p /data
    for f in map_memory.json tactic_config.json game_stats.json tactic_log.jsonl tactic_play.log; do
      if [ -f "/seed/$f" ] && [ ! -e "/data/$f" ]; then
        cp -f "/seed/$f" "/data/$f"
        echo "seeded $f (bootstrap)"
      else
        echo "kept existing $f"
      fi
    done
    # Container runs as uid 10001 (arena).
    chown -R 10001:10001 /data
    chmod -R u+rwX,go+rX /data
    ls -la /data
  '
rm -rf {remote_seed}
"""
    stdin, stdout, stderr = client.exec_command(seed_cmd)
    for line in iter(stdout.readline, ""):
        print(f"  {line}", end="")
    exit_code = stdout.channel.recv_exit_status()
    if exit_code != 0:
        err = stderr.read().decode()
        print(f"Runtime seed failed (exit={exit_code}): {err}")
        client.close()
        sys.exit(1)


def main() -> None:
    if not PASSWORD:
        print("ERROR: DEPLOY_PASSWORD is not set in .env.deploy")
        sys.exit(1)

    # Remote build output contains unicode (vite checkmarks); the Windows
    # GBK console would crash print() mid-stream and kill the deploy.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    print(f"Connecting to {HOST}:{PORT} as {USERNAME}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, PORT, USERNAME, PASSWORD, timeout=15)

    print(f"Creating {REMOTE_BASE}...")
    stdin, stdout, stderr = client.exec_command(f"mkdir -p {REMOTE_BASE}/runtime")
    stdout.channel.recv_exit_status()

    print("Transferring files...")
    sftp = client.open_sftp()
    ensure_remote_dir(sftp, REMOTE_BASE)
    file_count, total_size = transfer_files(sftp)
    print(f"Transferred {file_count} files ({total_size / 1024:.1f} KB)")

    print("Writing remote .env...")
    write_remote_env(client)

    seed_runtime_data(client, sftp)
    sftp.close()

    print("Building and starting Docker Compose...")
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

    print("\nDeployment complete!")
    print("\nVerifying deployment...")
    # Give the container a moment to bind the port.
    stdin, stdout, stderr = client.exec_command("sleep 2")
    stdout.channel.recv_exit_status()
    cmds = [
        "docker compose ps",
        f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{APP_PORT}/",
        # Host-side curls hit the container via docker-proxy (source = bridge
        # gateway, not loopback), so pass the token through Bearer.
        f"curl -s -o /dev/null -w '%{{http_code}}' -H 'Authorization: Bearer {DASHBOARD_TOKEN}' http://127.0.0.1:{APP_PORT}/api/state",
        "docker exec arena-game-app-1 ls -la /app/runtime",
    ]
    for cmd in cmds:
        stdin, stdout, stderr = client.exec_command(f"cd {REMOTE_BASE} && {cmd}")
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        print(f"  $ {cmd}")
        print(f"  -> {out or err or '(empty)'}")

    client.close()
    print(f"\nAll done! Dashboard is running at http://{HOST}:{APP_PORT}")


if __name__ == "__main__":
    main()
