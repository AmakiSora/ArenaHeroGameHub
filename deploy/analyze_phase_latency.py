#!/usr/bin/env python3
"""Diagnose planning phase breakdown from the remote tactic_log.jsonl.

Assumes the agent-instrumented tactic.py that writes per-tick:
  - phase_ms: {phase_name: wall-clock ms} for choose_actions phases
  - pathfind_calls, pathfind_expansions, pathfind_ms: A* telemetry
  - latency_ms (already present before instrumentation)

Aggregates across the latest N ticks to answer: which choose_actions phase
dominates slow ticks — prediction, map_memory, battle_log, core_setup,
resource_assign, core_action, unit_setup, unit:worker, unit:vanguard,
unit:ranger — and whether slow ticks are "many A* calls" vs "one huge search".

Pair with diag_remote6.py (which showed the gaps and the_TICK_MISMATCH flood).
"""
from __future__ import annotations

import collections
import json
import re
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


def parse_phase(j: dict) -> tuple[dict[str, float], int, int, float, float]:
    """Return (phase_ms, pf_calls, pf_exp, pf_ms, latency_ms)."""
    return (
        j.get("phase_ms") or {},
        int(j.get("pathfind_calls") or 0),
        int(j.get("pathfind_expansions") or 0),
        float(j.get("pathfind_ms") or 0.0),
        float(j.get("latency_ms") or 0.0),
    )


def parse_dead_end(j: dict) -> tuple[int, float]:
    """Return (runs, ms) of _dead_end_cells for the tick, if present."""
    return (int(j.get("dead_end_runs") or 0), float(j.get("dead_end_ms") or 0.0))


def main() -> None:
    env = load_env()
    host = env["DEPLOY_HOST"]
    port = int(env.get("DEPLOY_PORT", "22"))
    user = env.get("DEPLOY_USER", "root")
    password = env.get("DEPLOY_PASSWORD", "")
    base = env.get("DEPLOY_REMOTE_BASE", "/srv/arena-game")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"connecting to {user}@{host}:{port} …")
    client.connect(host, port, user, password, timeout=15)

    TAIL = 8000
    print(f"== fetching last {TAIL} lines of tactic_log.jsonl ==")
    _, stdout, _ = client.exec_command(
        f"docker exec arena-game-app-1 tail -n {TAIL} "
        f"/app/runtime/tactic_log.jsonl 2>&1",
        timeout=120,
    )
    text = stdout.read().decode(errors="replace")

    recs = []
    instr = 0  # ticks carrying phase_ms (instrumentation present)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(j, dict) or "tick" not in j:
            continue
        recs.append(j)
        if j.get("phase_ms"):
            instr += 1

    print(f"parsed ticks: {len(recs)}  with instrumentation: {instr}")
    if not recs:
        client.close()
        return

    # If instrumentation isn't live yet, tell the user to redeploy.
    if instr == 0:
        print("\n!! No ticks carry phase_ms / pathfind_* fields yet.\n"
              "   The deployed tactic.py predates the instrumentation.\n"
              "   Redeploy (python deploy/deploy.py) and re-run this script.")
        client.close()
        return

    instr_recs = [r for r in recs if r.get("phase_ms")]

    # 1. Per-phase aggregate over instrumented ticks
    phase_total = collections.Counter()
    phase_max = collections.Counter()
    phase_ticks = collections.Counter()
    for j in instr_recs:
        pm, *_ = parse_phase(j)
        for k, v in pm.items():
            phase_total[k] += v
            phase_max[k] = max(phase_max[k], v)
            phase_ticks[k] += 1

    print("\n== per-phase wall-clock across instrumented ticks (ms) ==")
    print(f"{'phase':<16} {'total':>9} {'avg/tick':>9} {'max':>8} {'ticks':>7}")
    for k in sorted(phase_total, key=phase_total.get, reverse=True):
        n = phase_ticks[k] or 1
        print(f"{k:<16} {phase_total[k]:9.0f} {phase_total[k]/n:9.1f} "
              f"{phase_max[k]:8.0f} {phase_ticks[k]:7d}")

    # 2. Pathfinding aggregate
    tot_calls = tot_exp = tot_pf_ms = 0.0
    for j in instr_recs:
        _, c, e, m, _ = parse_phase(j)
        tot_calls += c
        tot_exp += e
        tot_pf_ms += m
    # 2b. Dead-end recomputation aggregate (the cache-miss cost on wall growth)
    tot_de_runs = 0
    tot_de_ms = 0.0
    de_instr = 0
    for j in instr_recs:
        r, m = parse_dead_end(j)
        if j.get("dead_end_runs") is not None:
            de_instr += 1
        tot_de_runs += r
        tot_de_ms += m
    print("\n== pathfinding (A*) across instrumented ticks ==")
    print(f"  calls={int(tot_calls)}  expansions={int(tot_exp)}  "
          f"total={tot_pf_ms:.0f}ms  "
          f"avg/call={(tot_pf_ms/tot_calls if tot_calls else 0):.2f}ms  "
          f"avg expansions/call={(tot_exp/tot_calls if tot_calls else 0):.1f}")
    if de_instr:
        print("\n== dead-end recompute across instrumented ticks ==")
        print(f"  runs={int(tot_de_runs)}  total={tot_de_ms:.0f}ms  "
              f"avg/run={(tot_de_ms/tot_de_runs if tot_de_runs else 0):.1f}ms  "
              f"ticks_with_runs={(sum(1 for j in instr_recs if parse_dead_end(j)[0]) if instr_recs else 0)}")
    print("  pf_share_of_planning = pathfind_ms / sum(phase_ms)")
    pf_share_ok = []
    for j in instr_recs:
        pm, c, e, m, lat = parse_phase(j)
        s = sum(pm.values()) or lat or 1
        pf_share_ok.append((m / s, c, e, m, j["tick"]))
    # pathfind share distribution
    shares = sorted(s for s, *_ in pf_share_ok)
    if shares:
        n = len(shares)
        print(f"  pf_share p50={shares[n//2]:.2f} "
              f"p90={shares[int(n*0.90)]:.2f} max={shares[-1]:.2f}")

    # 3. Slowest ticks with their dominant phase
    print("\n== slowest instrumented ticks (by latency_ms) with top phase ==")
    def top_phase(pm):
        if not pm:
            return "-"
        return max(pm.items(), key=lambda kv: kv[1])
    for j in sorted(instr_recs, key=lambda r: r.get("latency_ms", 0),
                    reverse=True)[:15]:
        pm, c, e, m, lat = parse_phase(j)
        tk, tv = top_phase(pm)
        unit_total = (pm.get("unit:worker", 0) + pm.get("unit:vanguard", 0)
                      + pm.get("unit:ranger", 0))
        de_r, de_m = parse_dead_end(j)
        de_str = f" de(r={de_r} {de_m:.0f}ms)" if de_r else ""
        print(f"  tick={j['tick']} lat={lat:.0f}ms  pf(c={c} exp={e} {m:.0f}ms){de_str}  "
              f"unit={unit_total:.0f}ms  top={tk}({tv:.0f}ms)  "
              f"pop={j.get('population')} walls={j.get('obstacle_memory_count')}")

    # 4. When pathfinding dominates a tick
    print("\n== ticks where pathfind_ms > 20% of latency (A*-bound ticks) ==")
    pf_bound = []
    for j in instr_recs:
        pm, c, e, m, lat = parse_phase(j)
        if lat > 0 and m / lat > 0.20 and m > 100:
            pf_bound.append((j["tick"], lat, m, c, e,
                             j.get("obstacle_memory_count"),
                             j.get("population")))
    print(f"  count: {len(pf_bound)}")
    for tick, lat, m, c, e, walls, pop in sorted(pf_bound, key=lambda r: -r[2])[:20]:
        print(f"  tick={tick} lat={lat:.0f}ms pf={m:.0f}ms "
              f"calls={c} exp={e} walls={walls} pop={pop}")

    # 5. Per-unit-type aggregate — which planner eats the tick
    print("\n== per-unit-type planner aggregate (ms) ==")
    for ut in ("worker", "vanguard", "ranger"):
        tot = maxv = 0.0
        nt = 0
        for j in instr_recs:
            v = j.get("phase_ms", {}).get("unit:" + ut, 0)
            if v:
                tot += v
                maxv = max(maxv, v)
                nt += 1
        avg = tot / nt if nt else 0
        print(f"  unit:{ut:<8} total={tot:8.0f}ms  ticks_seen={nt:4d}  "
              f"avg={avg:6.1f}  max={maxv:6.0f}")

    # 6. Recent slow-plan stdout lines from tactic_play.log
    print("\n== recent [plan] slow-tick lines (stdout) ==")
    _, stdout2, _ = client.exec_command(
        "docker exec arena-game-app-1 grep '\\[plan\\]' "
        "/app/runtime/tactic_play.log 2>&1 | tail -25",
        timeout=60,
    )
    print(stdout2.read().decode(errors="replace").rstrip())

    client.close()
    print("\n[done] remote connection closed.")


if __name__ == "__main__":
    main()
