#!/usr/bin/env python3
"""Source-only deploy: upload changed .py/.md files, rebuild, preserve volume data.

Host keys are pinned in ``deploy/.ssh_known_hosts`` (see ssh_common.py); an
unknown key is refused unless ``DEPLOY_TRUST_NEW_HOST=1`` is set for the first
run.  Key auth via ``DEPLOY_KEY_PATH`` is preferred; ``DEPLOY_PASSWORD`` still
works.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import paramiko

_DEPLOY_DIR = Path(__file__).resolve().parent
if str(_DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(_DEPLOY_DIR))

from ssh_common import _mask, connect_ssh  # noqa: E402

ENV_FILE = _DEPLOY_DIR / ".env.deploy"

# A deploy script that dies while streaming the build leaves the old container
# half-updated: docker's build output carries glyphs a legacy Windows console
# cannot encode (a check mark in the vite log killed one mid-build), so never let
# printing be the thing that breaks a deployment.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


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
USERNAME = env.get("DEPLOY_USER", "").strip()
if not USERNAME:
    # Keep the historical fallback, but never silently.
    USERNAME = "root"
    print(
        "WARNING: DEPLOY_USER is not set in .env.deploy - falling back to 'root'.\n"
        "         Use a dedicated non-root account with docker group access instead."
    )
PASSWORD = env.get("DEPLOY_PASSWORD", "")
KEY_PATH = env.get("DEPLOY_KEY_PATH", "").strip()
KEY_PASSPHRASE = env.get("DEPLOY_KEY_PASSPHRASE", "")
REMOTE_BASE = env.get("DEPLOY_REMOTE_BASE", "/srv/arena-game")
APP_PORT = int(env.get("APP_PORT", "4399"))
DASHBOARD_TOKEN = env.get("DASHBOARD_TOKEN", "")
LOCAL_BASE = Path(__file__).resolve().parents[1]

# Every file the Dockerfile copies into the image, plus the build inputs.
# `docker-entrypoint.py` and `status.py` were missing here, so a source-only
# deploy silently kept the previous container entrypoint / status viewer.
FILES = (
    ".dockerignore",
    "compose.yml",
    "Dockerfile",
    "requirements.txt",
    "tactic.py",
    "tactic_config.py",
    "dashboard.py",
    "map_codec.py",
    "game_stats.py",
    "state_io.py",
    "watchdog.py",
    "status.py",
    "replay.py",
    "docker-entrypoint.py",
    "BEHAVIOR.md",
)

if not KEY_PATH and not PASSWORD:
    print("ERROR: no SSH credential. Set DEPLOY_KEY_PATH or DEPLOY_PASSWORD in .env.deploy")
    sys.exit(1)

print(f"Upload plan: {len(FILES)} source files -> {REMOTE_BASE}")
print(f"Connecting to {HOST}:{PORT} as {USERNAME}...")
client = connect_ssh(
    HOST,
    PORT,
    USERNAME,
    password=PASSWORD,
    key_path=KEY_PATH,
    key_passphrase=KEY_PASSPHRASE,
)

sftp = client.open_sftp()
for name in FILES:
    local = LOCAL_BASE / name
    remote = f"{REMOTE_BASE}/{name}"
    print(f"  upload {name} -> {remote}")
    sftp.put(str(local), remote)
sftp.close()

# Keep remote .env's DASHBOARD_TOKEN in sync (preserving all other keys).
if DASHBOARD_TOKEN:
    remote_env = f"{REMOTE_BASE}/.env"
    stdin, stdout, stderr = client.exec_command(
        f"test -f {remote_env} && cat {remote_env} || echo '__MISSING__'"
    )
    raw = stdout.read().decode()
    stdout.channel.recv_exit_status()
    if raw.strip() == "__MISSING__":
        print(f"  {remote_env} not found; skipping .env sync (run deploy.py first)")
    else:
        lines = raw.splitlines()
        out_lines = []
        found = False
        for line in lines:
            if line.strip().startswith("DASHBOARD_TOKEN="):
                out_lines.append(f"DASHBOARD_TOKEN={DASHBOARD_TOKEN}")
                found = True
            else:
                out_lines.append(line)
        if not found:
            out_lines.append(f"DASHBOARD_TOKEN={DASHBOARD_TOKEN}")
        content = "\n".join(out_lines) + "\n"
        stdin, stdout, stderr = client.exec_command(
            f"cat > {remote_env} << 'ENVEOF'\n{content}ENVEOF\n"
            f"chmod 600 {remote_env}"
        )
        stdout.channel.recv_exit_status()
        err = stderr.read().decode().strip()
        if err:
            print(f"  warn updating .env: {err}")
        else:
            print(f"  .env DASHBOARD_TOKEN synced ({_mask(DASHBOARD_TOKEN)})")
else:
    print("  DASHBOARD_TOKEN not set in .env.deploy; leaving remote .env as-is")

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


def _probe(path: str, timeout: int = 5) -> tuple[int, str]:
    """GET a dashboard path on the VPS host; (0, output) when there is no reply."""
    url = f"http://127.0.0.1:{APP_PORT}{path}"
    auth = f"-H 'Authorization: Bearer {DASHBOARD_TOKEN}'" if DASHBOARD_TOKEN else ""
    cmd = f"curl -s -m {timeout} {auth} -w '\\n%{{http_code}}' {url}"
    stdin, stdout, stderr = client.exec_command(f"cd {REMOTE_BASE} && {cmd}")
    raw = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace").strip()
    stdout.channel.recv_exit_status()
    body, _, code = raw.rpartition("\n")
    if not code.strip().isdigit():
        return 0, (raw or err)
    return int(code.strip()), body


def _json_ok(body: str) -> bool:
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and data.get("ok") is True


problems: list[str] = []

# Suppress the token everywhere in the console output.
print(f"  $ curl {_mask(DASHBOARD_TOKEN)} / /api/health /api/state")

status, _body = _probe("/")
print(f"  GET / -> {status or 'no HTTP status'}")
if status not in (200, 301, 302, 303, 307, 308):
    problems.append(f"GET / returned {status or 'no HTTP status'}")

status, body = _probe("/api/health")
print(f"  GET /api/health -> {status} {body[:200]}")
if status == 200:
    if not _json_ok(body):
        problems.append(f'/api/health body lacks "ok": true: {body[:120]!r}')
elif status == 503:
    print(
        "  WARNING: /api/health 503 - container up but the bot is not ticking yet; "
        "not failing the deploy."
    )
else:
    problems.append(f"/api/health returned {status or 'no HTTP status'}")

status, body = _probe("/api/state")
print(f"  GET /api/state -> {status} {body[:200]}")
if status != 200:
    problems.append(f"/api/state returned {status or 'no HTTP status'}")
elif not _json_ok(body):
    problems.append(f'/api/state body lacks "ok": true: {body[:120]!r}')

for cmd in ("docker compose ps", "docker compose logs --tail=5 app"):
    stdin, stdout, stderr = client.exec_command(f"cd {REMOTE_BASE} && {cmd}")
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    print(f"  $ {cmd}")
    print(f"  -> {out or err or '(empty)'}")

client.close()

if problems:
    print("\nSmoke test FAILED:")
    for problem in problems:
        print(f"  ERROR: {problem}")
    sys.exit(1)

print("Smoke test passed.")
print(f"\nDone! Dashboard: http://{HOST}:{APP_PORT}")
