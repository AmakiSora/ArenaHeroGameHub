#!/usr/bin/env python3
"""Diagnose stuck/oscillating cargo workers on the remote VPS.

Pulls the tail of tactic_log.jsonl and analyzes per-worker trajectories:

1. OSCILLATION — a worker bouncing between 2-3 cells for many ticks
   (A->B->A->B) instead of progressing toward its target.
2. CARGO LINGER — workers holding cargo > 0 for a long time without a
   DEPOSIT_SUCCEEDED, i.e. they never reach/enter the core cell.
3. ACTION MIX — histogram of plan_unit_actions detail strings so we can
   see which branch (core-queue-hold / core-retreat / flee-enemy /
   greedy fallback) dominates the stuck workers.
4. FAILURE EVENTS — counts of DEPOSIT_FAILED / UNIT_MOVE_FAILED /
   HARVEST_FAILED reasons in the analyzed window.
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

import paramiko

ENV_FILE = Path(__file__).resolve().parent / ".env.deploy"
TAIL = 2500          # lines of tactic_log.jsonl to fetch
WINDOW = 300         # ticks analyzed for trajectories
OSC_WINDOW = 12      # ticks used for oscillation detection
OSC_MIN = 8          # min bouncing ticks to flag a worker


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
    since_tick = None
    watch: set[str] = set()
    for arg in sys.argv[1:]:
        if arg.startswith("--since="):
            since_tick = int(arg.split("=", 1)[1])
        elif arg.startswith("--watch="):
            watch = {s for s in arg.split("=", 1)[1].split(",") if s}
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
    client.close()

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
    if since_tick is not None:
        recs = [r for r in recs if r["tick"] >= since_tick]
    recs = recs[-WINDOW:]
    if not recs:
        print("no tick records parsed — aborting")
        return
    print(f"analyzing ticks {recs[0]['tick']} .. {recs[-1]['tick']} ({len(recs)} ticks)")

    # ── per-worker trajectories ──────────────────────────────────────────
    traj: dict[str, list[tuple[int, tuple, int, tuple | None]]] = collections.defaultdict(list)
    action_hist: collections.Counter = collections.Counter()
    worker_action_hist: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    event_hist: collections.Counter = collections.Counter()
    core_pos_last = None
    res_last = (None, None)
    deposit_success: dict[str, int] = collections.Counter()

    for rec in recs:
        core_pos_last = tuple(rec.get("core_pos") or ())
        res_last = (rec.get("resources"), rec.get("resource_capacity"))
        acts = rec.get("plan_unit_actions", {})
        if watch:
            cpos = rec.get("core_pos") or []
            cpt = tuple(cpos) if cpos else None
            on_core = []
            near_ring = []
            for grp in ("workers", "vanguards", "rangers"):
                for u in rec.get(grp, []):
                    up = tuple(u["pos"])
                    if cpt and up == cpt:
                        on_core.append(f"{grp[:2]}:{u['id']}(c{u.get('cargo','')})")
                    elif cpt and abs(up[0]-cpt[0])+abs(up[1]-cpt[1]) == 1:
                        near_ring.append(f"{grp[:2]}:{u['id'][:6]}@{up}")
            line = (f"t={rec['tick']} core_state={rec.get('core_state','')} "
                    f"on_core={on_core} ring={near_ring} enemies={rec.get('visible_enemies')}")
            for w in rec.get("workers", []):
                if w["id"] in watch:
                    line += f"\n    {w['id']} pos={w['pos']} cargo={w['cargo']} act={acts.get(w['id'],'')}"
            print(line)
        for w in rec.get("workers", []):
            wid = w["id"]
            traj[wid].append((rec["tick"], tuple(w["pos"]), w.get("cargo", 0),
                              tuple(w["target"]) if w.get("target") else None))
            detail = acts.get(wid, "")
            if detail:
                # normalize away coordinates for the histogram
                key = detail.split(" -> ")[0].split("@")[0]
                action_hist[key] += 1
                worker_action_hist[wid][key] += 1
        for ev in rec.get("events", []):
            et = ev.get("type", "")
            reason = ev.get("reason") or ""
            event_hist[f"{et}:{reason}" if reason else et] += 1
            if et == "DEPOSIT_SUCCEEDED" and ev.get("actor"):
                deposit_success[str(ev["actor"])[:8]] += 1

    # ── oscillation + cargo linger detection ─────────────────────────────
    print(f"\n== worker summary (window={len(recs)} ticks, core@{core_pos_last}, "
          f"storage={res_last[0]}/{res_last[1]}) ==")
    flagged = []
    for wid, seq in traj.items():
        if len(seq) < OSC_WINDOW:
            continue
        recent = seq[-OSC_WINDOW:]
        positions = [s[1] for s in recent]
        uniq = set(positions)
        # bouncing: few unique cells, many reversals
        reversals = sum(
            1 for i in range(2, len(positions))
            if positions[i] == positions[i - 2]
        )
        cargo_ticks = sum(1 for s in seq if s[2] > 0)
        total_ticks = len(seq)
        # net displacement in window
        net = abs(positions[-1][0] - positions[0][0]) + abs(positions[-1][1] - positions[0][1])
        osc = len(uniq) <= 3 and reversals >= OSC_MIN // 2
        stuck_cargo = cargo_ticks > total_ticks * 0.6 and deposit_success.get(wid, 0) == 0
        status = ("OSC+STUCK" if osc and stuck_cargo else
                  "OSCILLATE" if osc else
                  "CARGO-LINGER" if stuck_cargo else "")
        last_pos, last_cargo, last_target = positions[-1], recent[-1][2], recent[-1][3]
        print(f"  {wid} ticks={total_ticks:3d} uniq_cells={len(uniq)} rev={reversals:2d} "
              f"net={net:2d} cargo_ticks={cargo_ticks:3d} deposits={deposit_success.get(wid,0)} "
              f"last_pos={last_pos} cargo={last_cargo} target={last_target} {status}")
        if status:
            flagged.append((wid, seq))

    # ── trajectory dumps for flagged workers ─────────────────────────────
    for wid, seq in flagged[:6]:
        tail = seq[-30:]
        print(f"\n-- {wid} last {len(tail)} ticks --")
        for tick, pos, cargo, target in tail:
            act = worker_action_hist[wid].most_common(3)
            print(f"  t={tick} pos={pos} cargo={cargo} target={target}")
        print(f"  top actions: {act}")

    # ── global action & event histograms ─────────────────────────────────
    print("\n== worker action mix (top 15) ==")
    for key, cnt in action_hist.most_common(15):
        print(f"  {cnt:5d}  {key}")

    print("\n== events (top 20) ==")
    for key, cnt in event_hist.most_common(20):
        print(f"  {cnt:5d}  {key}")


if __name__ == "__main__":
    main()
