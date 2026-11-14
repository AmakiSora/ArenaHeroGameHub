#!/usr/bin/env python3
"""Deploy ArenaGame to remote server via SFTP + Docker Compose.

Credentials are read from deploy/.env.deploy (not tracked in git).
Copy deploy/.env.deploy.example to deploy/.env.deploy and fill in the values.

Only the files in the upload manifest below are pushed; the rest of the working
tree is never sent.  The old whole-tree walk uploaded every scratch artifact
lying around (~112.8 MB of ``_remote_*`` dumps per deploy, see
OPTIMIZATION_REVIEW.md #3); the planned upload is now printed *before* the SSH
connection is opened.

Host identity is pinned: server keys live in ``deploy/.ssh_known_hosts`` and an
unknown server key is refused unless ``DEPLOY_TRUST_NEW_HOST=1`` is set for the
first run (TOFU, prints the SHA256 fingerprint).  See OPTIMIZATION_REVIEW.md #4.

Dashboard is published on TCP port 4399.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

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
USERNAME = env.get("DEPLOY_USER", "").strip()
if not USERNAME:
    # Keep the historical fallback, but never silently: running the deploy as
    # root is exactly what OPTIMIZATION_REVIEW.md #4 asks us to move away from.
    USERNAME = "root"
    print(
        "WARNING: DEPLOY_USER is not set in .env.deploy - falling back to 'root'.\n"
        "         Use a dedicated non-root account with docker group access instead."
    )
PASSWORD = env.get("DEPLOY_PASSWORD", "")
KEY_PATH = env.get("DEPLOY_KEY_PATH", "").strip()
KEY_PASSPHRASE = env.get("DEPLOY_KEY_PASSPHRASE", "")
REMOTE_BASE = env.get("DEPLOY_REMOTE_BASE", "/srv/arena-game")
ARENA_HERO_API_KEY = env.get("ARENA_HERO_API_KEY", "")
DASHBOARD_TOKEN = env.get("DASHBOARD_TOKEN", "")
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
    r"\.pytest_cache/",
    r"\.ruff_cache/",
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
    # Scratch artifacts: ONE generic rule.  The old `^_.*\.py$` only stopped
    # `_foo.py` and happily pushed `_remote_state_latest.json` (21.9 MB) and the
    # other `_*.json[l]` dumps.  `^_.*` matches a root-level name (the walk
    # normalises to a repo-relative path), `/_ .*` the same rule at any depth.
    r"^_.*",
    r"/_.*",
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


class UploadItem(NamedTuple):
    """One file of the planned SFTP upload."""

    rel_path: str  # repo-relative, forward slashes
    local_path: Path
    size: int


def should_exclude(rel_path: str) -> bool:
    for pattern in EXCLUDE_PATTERNS:
        if re.search(pattern, rel_path):
            return True
    return False


# ---------------------------------------------------------------------------
# Upload manifest
# ---------------------------------------------------------------------------
# Explicit allow-list instead of a whole-tree walk: reviewable, size-bounded,
# and it cannot pick up a fresh 20 MB production dump someone left in the root.
MANIFEST_FILES = (
    ".dockerignore",
    "Dockerfile",
    "compose.yml",
    "requirements.txt",
    "map_codec.py",
    "state_io.py",
    "tactic_config.py",
    "game_stats.py",
    "watchdog.py",
    "status.py",
    "tactic.py",
    "dashboard.py",
    # The container entrypoint.  deploy_source_only.py forgot it, so a
    # source-only deploy silently kept an old entrypoint in the image.
    "docker-entrypoint.py",
    "BEHAVIOR.md",
)

# Directories uploaded recursively (source only).  `web/src`, `web/public`, the
# vite/ts configs and package-lock.json are what the Dockerfile's web-builder
# stage needs; node_modules and dist stay local (EXCLUDE_PATTERNS).
MANIFEST_DIRS = ("web",)

# Safety net for tree-discovered files: anything bigger than this that is not an
# explicit manifest entry is almost certainly a stray artifact.  Runtime seed
# files are exempt because they are uploaded separately into the volume.
MAX_UNLISTED_FILE_BYTES = 2 * 1024 * 1024


def _rel(path: Path) -> str:
    return os.path.relpath(path, LOCAL_BASE).replace("\\", "/")


def collect_upload_plan() -> tuple[list[UploadItem], list[UploadItem], list[str]]:
    """Resolve the manifest into concrete upload items.

    Returns ``(plan, skipped_large, missing)``.  ``skipped_large`` holds files
    found inside manifest directories that exceed MAX_UNLISTED_FILE_BYTES;
    ``missing`` holds manifest entries absent from the working tree.
    """
    plan: list[UploadItem] = []
    skipped_large: list[UploadItem] = []
    missing: list[str] = []

    for rel_path in MANIFEST_FILES:
        local = Path(LOCAL_BASE) / rel_path
        if not local.is_file():
            missing.append(rel_path)
            continue
        plan.append(UploadItem(rel_path, local, local.stat().st_size))

    for dirname in MANIFEST_DIRS:
        base = Path(LOCAL_BASE) / dirname
        if not base.is_dir():
            missing.append(f"{dirname}/")
            continue
        for root, dirs, files in os.walk(base):
            root_path = Path(root)
            dirs[:] = sorted(d for d in dirs if not should_exclude(_rel(root_path / d)))
            for name in sorted(files):
                local = root_path / name
                rel_path = _rel(local)
                if should_exclude(rel_path):
                    continue
                try:
                    size = local.stat().st_size
                except OSError:
                    continue
                item = UploadItem(rel_path, local, size)
                if size > MAX_UNLISTED_FILE_BYTES:
                    skipped_large.append(item)
                    continue
                plan.append(item)
    return plan, skipped_large, missing


def print_upload_plan(
    plan: list[UploadItem], skipped_large: list[UploadItem], missing: list[str]
) -> None:
    """Print what is about to be uploaded, before any SSH connection."""
    total = sum(item.size for item in plan)
    print(f"Upload plan: {len(plan)} files, {total / 1024 / 1024:.2f} MB "
          f"-> {REMOTE_BASE}")
    largest = sorted(plan, key=lambda item: item.size, reverse=True)[:3]
    for item in largest:
        print(f"  {item.size / 1024:8.1f} KB  {item.rel_path}")
    for item in skipped_large:
        print(
            f"  SKIP {item.rel_path} ({item.size / 1024 / 1024:.1f} MB): larger than "
            f"{MAX_UNLISTED_FILE_BYTES // (1024 * 1024)} MB and not in MANIFEST_FILES"
        )
    if missing:
        print("ERROR: the upload manifest references paths that do not exist:")
        for rel_path in missing:
            print(f"  - {rel_path}")
        sys.exit(1)


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


def transfer_files(sftp: paramiko.SFTPClient, plan: list[UploadItem]) -> tuple[int, int]:
    """Upload exactly the planned files (manifest-scoped, no tree walk)."""
    file_count = 0
    total_size = 0

    for item in plan:
        remote_path = f"{REMOTE_BASE}/{item.rel_path}"
        ensure_remote_dir(sftp, os.path.dirname(remote_path))
        sftp.put(str(item.local_path), remote_path)
        file_count += 1
        total_size += item.size

        if file_count % 50 == 0:
            print(f"  Transferred {file_count}/{len(plan)} files...")

    return file_count, total_size


def write_remote_env(client: paramiko.SSHClient) -> None:
    env_content = (
        f"ARENA_HERO_API_KEY={ARENA_HERO_API_KEY}\n"
        f"DASHBOARD_TOKEN={DASHBOARD_TOKEN}\n"
    )
    stdin, stdout, stderr = client.exec_command(
        f"cat > {REMOTE_BASE}/.env << 'ENVEOF'\n{env_content}ENVEOF\n"
        f"chmod 600 {REMOTE_BASE}/.env"
    )
    stdout.channel.recv_exit_status()
    err = stderr.read().decode().strip()
    if err:
        print(f"  warn writing .env: {err}")
    else:
        print(
            f"  remote .env written: ARENA_HERO_API_KEY={_mask(ARENA_HERO_API_KEY)} "
            f"DASHBOARD_TOKEN={_mask(DASHBOARD_TOKEN)}"
        )


def seed_runtime_data(client: paramiko.SSHClient, sftp: paramiko.SFTPClient) -> None:
    """Bootstrap local runtime files into the Docker volume on first launch.

    Files already present in the volume (dashboard-set config, discovered map
    memory, stats, logs) are kept as-is — seeding only fills an empty volume so
    a brand-new server starts from the local state instead of defaults.

    These files are intentionally NOT subject to the 2 MB manifest guard: they
    go into the data volume, never into the image build context.
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
        shown = f"{size / 1024 / 1024:.1f} MB" if size > 1024 * 1024 else f"{size / 1024:.1f} KB"
        print(f"  upload {name} ({shown})")
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


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
def _http_probe(client: paramiko.SSHClient, path: str, timeout: int = 5) -> tuple[int, str]:
    """GET a path on the VPS host; return (status code, body).

    0 means "no HTTP status at all" (connection refused / curl failure).
    """
    url = f"http://127.0.0.1:{APP_PORT}{path}"
    # Host-side curls reach the container through docker-proxy, so the source is
    # the bridge gateway, not loopback: pass the token as a Bearer header.
    cmd = (
        f"curl -s -m {timeout} -H 'Authorization: Bearer {DASHBOARD_TOKEN}' "
        f"-w '\\n%{{http_code}}' {url}"
    )
    stdin, stdout, stderr = client.exec_command(f"cd {REMOTE_BASE} && {cmd}")
    raw = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace").strip()
    stdout.channel.recv_exit_status()
    body, _, code = raw.rpartition("\n")
    if not code.strip().isdigit():
        return 0, (raw or err)
    return int(code.strip()), body


def _json_ok(body: str) -> tuple[bool, str]:
    """True when the body is a JSON object with ``"ok": true``."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return False, f'body is not JSON: {body[:120]!r}'
    if isinstance(data, dict) and data.get("ok") is True:
        return True, ""
    return False, f'body lacks "ok": true: {body[:120]!r}'


def smoke_test(client: paramiko.SSHClient) -> None:
    """Verify the new container serves the dashboard and reports a healthy bot.

    An HTTP 200 alone is not enough (the reviewed dashboard returned 200 with a
    dead tactic), so the JSON bodies must carry ``"ok": true``.  A 503 from
    /api/health is *expected* right after a deploy — it means "container up, bot
    not ticking yet" — and is reported as a warning, not a failure.
    """
    problems: list[str] = []

    status, _body = _http_probe(client, "/")
    print(f"  GET / -> {status or 'no HTTP status'}")
    if status not in (200, 301, 302, 303, 307, 308):
        problems.append(f"GET / returned {status or 'no HTTP status'}")

    status, body = _http_probe(client, "/api/health")
    print(f"  GET /api/health -> {status} {body[:200]}")
    if status == 200:
        ok, why = _json_ok(body)
        if not ok:
            problems.append(f"/api/health {why}")
    elif status == 503:
        print(
            "  WARNING: /api/health returned 503 - the container is up but the bot is "
            "not ticking yet (tactic still starting, or ARENA_HERO_API_KEY unset).\n"
            "           Deploy is treated as successful; re-check in a minute."
        )
    else:
        problems.append(f"/api/health returned {status or 'no HTTP status'}")

    status, body = _http_probe(client, "/api/state")
    print(f"  GET /api/state -> {status} {body[:200]}")
    if status != 200:
        problems.append(f"/api/state returned {status or 'no HTTP status'}")
    else:
        ok, why = _json_ok(body)
        if not ok:
            problems.append(f"/api/state {why}")

    if problems:
        print("\nSmoke test FAILED:")
        for problem in problems:
            print(f"  ERROR: {problem}")
        print(
            "The container may still be starting; check by hand with:\n"
            f"  curl -s -H 'Authorization: Bearer {_mask(DASHBOARD_TOKEN)}' "
            f"http://127.0.0.1:{APP_PORT}/api/health"
        )
        raise SystemExit(1)
    print("Smoke test passed: / served, /api/health ok, /api/state ok")


def main() -> int:
    if not ARENA_HERO_API_KEY:
        print("ERROR: ARENA_HERO_API_KEY is not set in .env.deploy")
        return 1
    if not DASHBOARD_TOKEN:
        print("ERROR: DASHBOARD_TOKEN is not set in .env.deploy")
        return 1
    # The token is interpolated into a remote `curl -H 'Authorization: Bearer …'`
    # command, so whitespace/quotes/backslashes/shell metacharacters would break
    # the probe (or worse) rather than just fail to authenticate.
    if re.search(r"[\s'\"`$\\]", DASHBOARD_TOKEN):
        print(
            "ERROR: DASHBOARD_TOKEN contains whitespace, quotes or shell "
            "metacharacters; regenerate it with secrets.token_urlsafe(32)."
        )
        return 1
    if not KEY_PATH and not PASSWORD:
        print(
            "ERROR: no SSH credential. Set DEPLOY_KEY_PATH (preferred) "
            "or DEPLOY_PASSWORD in .env.deploy."
        )
        return 1

    # Plan and print BEFORE connecting: a deploy should never be a surprise.
    plan, skipped_large, missing = collect_upload_plan()
    print_upload_plan(plan, skipped_large, missing)

    print(f"Connecting to {HOST}:{PORT} as {USERNAME}...")
    client = connect_ssh(
        HOST,
        PORT,
        USERNAME,
        password=PASSWORD,
        key_path=KEY_PATH,
        key_passphrase=KEY_PASSPHRASE,
    )
    try:
        print(f"Creating {REMOTE_BASE}...")
        stdin, stdout, stderr = client.exec_command(f"mkdir -p {REMOTE_BASE}/runtime")
        stdout.channel.recv_exit_status()

        print("Transferring files...")
        sftp = client.open_sftp()
        ensure_remote_dir(sftp, REMOTE_BASE)
        file_count, total_size = transfer_files(sftp, plan)
        print(f"Transferred {file_count} files ({total_size / 1024 / 1024:.2f} MB)")

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
            return 1

        print("\nDeployment complete!")
        print("\nVerifying deployment...")
        # Give the container a moment to bind the port.
        stdin, stdout, stderr = client.exec_command("sleep 2")
        stdout.channel.recv_exit_status()

        informative = [
            "docker compose ps",
            "docker exec arena-game-app-1 ls -la /app/runtime",
        ]
        for cmd in informative:
            stdin, stdout, stderr = client.exec_command(f"cd {REMOTE_BASE} && {cmd}")
            out = stdout.read().decode().strip()
            err = stderr.read().decode().strip()
            print(f"  $ {cmd}")
            print(f"  -> {out or err or '(empty)'}")

        smoke_test(client)
    finally:
        client.close()

    print(f"\nAll done! Dashboard is running at http://{HOST}:{APP_PORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
