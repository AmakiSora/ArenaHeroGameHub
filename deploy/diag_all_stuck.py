#!/usr/bin/env python3
"""Diagnose ALL unit types (workers/vanguards/rangers) for stuck behavior.

Pulls the tail of tactic_log.jsonl from the remote VPS and flags units whose
position barely changed over the trailing window, then prints their recent
trajectory + action details so we can see why they stopped progressing.
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

import paramiko

ENV_FILE = Path(__file__).resolve().parent / ".env.deploy"
TAIL = 1500          # lines of tactic_log.jsonl to fetch
WINDOW = 200         # ticks analyzed
STUCK_NET = 4        # net displacement below this -> flagged


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


def run_cmd(client: paramiko.SSHClient, cmd: str, timeout: int = 180) -> str:
    _, stdout, _ = client.exec_command(cmd, timeout=timeout)
    return stdout.read().decode(errors="replace")


def main() -> None:
    env = load_env()
    host = env.get("DEPLOY_HOST") or sys.exit("DEPLOY_HOST not set")
    port = int(env.get("DEPLOY_PORT", "22"))
    user = env.get("DEPLOY_USER", "root")
    password = env.get("DEPLOY_PASSWORD", "")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"connecting to {user}@{host}:{port} …")
    client.connect(host, port, user, password, timeout=15)

    text = run_cmd(
        client,
        f"docker exec arena-game-app-1 tail -n {TAIL} /app/runtime/tactic_log.jsonl",
    )
    # Also dump the live config + waypoints/holds so we can rule out manual holds.
    cfg = run_cmd(client, "docker exec arena-game-app-1 cat /app/runtime/tactic_config.json")
    holds = run_cmd(client, "docker exec arena-game-app-1 cat /app/runtime/holds.json 2>/dev/null")
    wpts = run_cmd(client, "docker exec arena-game-app-1 cat /app/runtime/waypoints.json 2>/dev/null")
    client.close()

    print("\n== remote config (relevant keys) ==")
    try:
        cj = json.loads(cfg)
        for k in sorted(cj):
            if any(s in k for s in ("explore", "bfs", "congest", "queue", "chute",
                                    "backtrack", "retreat", "hold")):
                print(f"  {k} = {cj[k]}")
    except json.JSONDecodeError as exc:
        print("  config parse failed:", exc)

    print("\n== holds.json ==", holds.strip() or "(empty)")
    print("\n== waypoints.json ==", (wpts.strip() or "(empty)"))

    recs = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if j.get("_meta") or j.get("_summary"):
            continue
        if "tick" in j:
            recs.append(j)
    recs = recs[-WINDOW:]
    if not recs:
        print("no tick records parsed — aborting")
        return
    print(f"\nanalyzing ticks {recs[0]['tick']} .. {recs[-1]['tick']} ({len(recs)} ticks)")
    core_pos = tuple(recs[-1].get("core_pos") or ())
    print(f"core @ {core_pos}, storage={recs[-1].get('resources')}/{recs[-1].get('resource_capacity')}")

    groups = ("workers", "vanguards", "rangers")
    traj: dict[str, list] = collections.defaultdict(list)   # uid -> [(tick, pos, action)]
    for rec in recs:
        acts = rec.get("plan_unit_actions", {})
        for grp in groups:
            for u in rec.get(grp, []):
                traj[u["id"]].append((rec["tick"], tuple(u["pos"]),
                                      u.get("cargo", 0), acts.get(u["id"], "")))

    flagged = []
    print("\n== all units: net displacement over window ==")
    for uid, seq in sorted(traj.items()):
        if len(seq) < WINDOW // 2:
            continue
        head, tail = seq[0][1], seq[-1][1]
        net = abs(head[0] - tail[0]) + abs(head[1] - tail[1])
        uniq = len({s[1] for s in seq})
        cargo = seq[-1][2]
        status = "STUCK?" if net <= STUCK_NET else ""
        print(f"  {uid}  n={len(seq):3d} net={net:3d} uniq={uniq:2d} "
              f"last={tail} cargo={cargo} act={seq[-1][3][:40]!r} {status}")
        if status:
            flagged.append((uid, seq))

    for uid, seq in flagged:
        print(f"\n-- {uid} last 25 ticks --")
        for tick, pos, cargo, act in seq[-25:]:
            print(f"  t={tick} pos={pos} cargo={cargo} act={act}")
        act_hist = collections.Counter(s[3] for s in seq)
        print("  action histogram:")
        for k, v in act_hist.most_common(8):
            print(f"    {v:4d}  {k}")

    # chute demand context for the stuck cargo workers
    print("\n== WAIT:core-congested occurrences (last 50) ==")
    shown = 0
    for rec in reversed(recs):
        acts = rec.get("plan_unit_actions", {})
        for uid, act in acts.items():
            if "core-congested" in act:
                dist = None
                for grp in groups:
                    for u in rec.get(grp, []):
                        if u["id"] == uid:
                            dist = abs(u["pos"][0] - core_pos[0]) + abs(u["pos"][1] - core_pos[1])
                print(f"  t={rec['tick']} {uid} act={act} dist_to_core={dist}")
                shown += 1
        if shown >= 50:
            break


if __name__ == "__main__":
    main()
