#!/usr/bin/env python3
"""One-off diag: count enemy-sighting entries in the remote map_memory.json
by on-disk shape and type label, to find type-less ("ENEMY") memories."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import paramiko

ENV_FILE = Path(__file__).resolve().parent / ".env.deploy"
PURGE_SCRIPT = Path(__file__).resolve().parent / "purge_typeless_enemy_memory.py"


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
    do_purge = "--purge" in sys.argv
    env = load_env()
    host = env.get("DEPLOY_HOST") or sys.exit("DEPLOY_HOST not set")
    port = int(env.get("DEPLOY_PORT", "22"))
    user = env.get("DEPLOY_USER", "root")
    password = env.get("DEPLOY_PASSWORD", "")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port, user, password, timeout=15)

    cmd = "docker run --rm -v arena-game-runtime:/data:ro alpine:3.21 cat /data/map_memory.json"
    _, stdout, stderr = client.exec_command(cmd, timeout=60)
    raw = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace").strip()
    client.close()
    if err:
        print(f"[stderr] {err}")

    data = json.loads(raw)
    sightings = data.get("enemy_sightings", []) or []
    print(f"updated_tick={data.get('updated_tick')} total={len(sightings)}")

    shapes = Counter()
    types = Counter()
    for item in sightings:
        if not isinstance(item, list):
            shapes["non-list"] += 1
            continue
        shapes[f"len={len(item)}"] += 1
        etype = item[2] if len(item) >= 3 and item[2] else "<missing>"
        types[str(etype)] += 1

    print("shapes:", dict(shapes))
    print("types:", dict(types))

    by_tick: dict[str, list[int]] = {}
    for item in sightings:
        if isinstance(item, list) and len(item) >= 4:
            by_tick.setdefault(str(item[2]), []).append(int(item[3] or 0))
    for etype, ticks in sorted(by_tick.items()):
        ticks.sort()
        print(
            f"{etype}: n={len(ticks)} min_tick={ticks[0]} max_tick={ticks[-1]}"
        )

    client2 = paramiko.SSHClient()
    client2.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client2.connect(host, port, user, password, timeout=15)

    def run2(cmd: str) -> str:
        _, out, err = client2.exec_command(cmd, timeout=60)
        text = out.read().decode(errors="replace").strip()
        errtext = err.read().decode(errors="replace").strip()
        return text + (f"\n[stderr] {errtext}" if errtext else "")

    cid = run2("docker ps -q | head -1")
    print("container:", cid)
    print("remote sdk:", run2(
        f"docker exec {cid} python -c 'import arena_hero; print(arena_hero.version())'"
    ))
    for probe in (
        "_enemy_memory_ticks",
        "_enemy_unit_type_name",
        "seen_tick = int(getattr(turn",
        "_enemy_sightings_from_payload",
    ):
        n = run2(f"docker exec {cid} grep -c {probe!r} /app/tactic.py")
        print(f"remote tactic.py contains {probe!r}: {n}")

    if do_purge:
        sftp = client2.open_sftp()
        sftp.put(str(PURGE_SCRIPT), "/tmp/purge_typeless_enemy_memory.py")
        sftp.close()
        print("purge:", run2(
            f"docker cp /tmp/purge_typeless_enemy_memory.py {cid}:/tmp/ && "
            f"docker exec -w /app -e PYTHONPATH=/app {cid} python /tmp/purge_typeless_enemy_memory.py"
        ))
    client2.close()


if __name__ == "__main__":
    main()
