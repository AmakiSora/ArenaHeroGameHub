#!/usr/bin/env python3
"""Diagnose why some ticks have no action: latency spikes vs stuck/idle.

Pulls the structured tactic_log.jsonl from the remote VPS, runs the same
latency analysis that analyze_latency.py does, and additionally computes
inter-tick gaps (wall-clock seconds between consecutive recorded ticks) to
distinguish three failure modes:

1. HIGH LATENCY  — choose_actions+submit is slow (latency_ms large). If it
   approaches the keepalive ping_timeout (60s), the sync websockets client
   can't read pongs in time → ConnectionClosed 1011 keepalive-ping-timeout.
2. STUCK / GAPS   — large wall-clock gap between consecutive tick records
   with no error in between → the planning loop stalled (or the process
   was blocked on something).
3. RECONNECTS     — a stream error printed a reconnect line; ticks then
   resume. Gaps here are expected (the SDK retry/backoff).

Also scans tactic_play.log (unstructured stdout) for the tell-tale signposts:
  - "[tactic] stream error" / "ConnectionClosedError"  → transport drop
  - "submit_error=...TICK_MISMATCH"                      → desynced session
  - "[tactic] connecting session="                        → reconnect event
  - "plan_error="                                        → planning crash
"""
from __future__ import annotations

import collections
import io
import json
import re
import sys
import tarfile
from pathlib import Path

import paramiko

ENV_FILE = Path(__file__).resolve().parent / ".env.deploy"

# A tick is "no action" only if we have a record but it submitted nothing
# useful — but the user's question is "some ticks have NO record at all
# between two recorded ticks". We detect that via inter-tick wall-clock gap.


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


def parse_ts(ts: str) -> float | None:
    """Parse an ISO-ish timestamp prefix into seconds-since-epoch-ish monotonic.

    We don't need real epoch; we only compare deltas, so we parse the
    HH:MM:SS.f part and let day-wrap be ignored (a session is contiguous).
    """
    if not ts:
        return None
    m = re.match(r"(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?", ts)
    if not m:
        # maybe full ISO with date
        m = re.search(r"(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?", ts)
        if not m:
            return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    frac = 0.0
    if m.group(4):
        frac = float("0." + m.group(4))
    return h * 3600 + mi * 60 + s + frac


def fetch_log_text(client: paramiko.SSHClient, remote_path: str,
                   tail_lines: int = 0) -> str:
    """Fetch a remote log file (optionally tail) as text via docker exec."""
    if tail_lines:
        cmd = (f"docker exec arena-game-app-1 tail -n {tail_lines} "
               f"{remote_path} 2>&1")
    else:
        cmd = f"docker exec arena-game-app-1 cat {remote_path} 2>&1"
    _, stdout, _ = client.exec_command(cmd, timeout=120)
    return stdout.read().decode(errors="replace")


def run_cmd(client: paramiko.SSHClient, cmd: str, timeout: int = 120) -> str:
    _, stdout, _ = client.exec_command(cmd, timeout=timeout)
    return stdout.read().decode(errors="replace").strip()


def main() -> None:
    env = load_env()
    host = env.get("DEPLOY_HOST") or sys.exit("DEPLOY_HOST not set")
    port = int(env.get("DEPLOY_PORT", "22"))
    user = env.get("DEPLOY_USER", "root")
    password = env.get("DEPLOY_PASSWORD", "")
    base = env.get("DEPLOY_REMOTE_BASE", "/srv/arena-game")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"connecting to {user}@{host}:{port} …")
    client.connect(host, port, user, password, timeout=15)

    # 0. Container + log file sanity
    print("\n== container status ==")
    print(run_cmd(client, "cd %s && docker compose ps 2>&1" % base))

    print("\n== log file sizes / mtimes ==")
    print(run_cmd(client,
        "docker exec arena-game-app-1 sh -c "
        "'ls -la /app/runtime/tactic_log.jsonl /app/runtime/tactic_play.log 2>&1'"))

    print("\n== container uptime (started_at) ==")
    print(run_cmd(client,
        "docker inspect -f '{{.State.StartedAt}}  pid={{.State.Pid}}' "
        "arena-game-app-1 2>&1"))

    # 1. Pull the structured JSONL log (tail a generous window for analysis)
    TAIL = 4000
    print(f"\n== fetching last {TAIL} lines of tactic_log.jsonl ==")
    jtext = fetch_log_text(client, "/app/runtime/tactic_log.jsonl", tail_lines=TAIL)

    recs = []
    bad = 0
    for line in jtext.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            bad += 1
            continue
        if j.get("_meta"):
            continue
        if "tick" in j:
            recs.append(j)
    print(f"parsed ticks: {len(recs)}  (unparseable lines: {bad})")

    if recs:
        # timestamp field name?
        ts_key = "timestamp" if "timestamp" in recs[-1] else "ts"
        # latency distribution
        lat = [j.get("latency_ms") for j in recs]
        lat = [x for x in lat if x is not None]
        if lat:
            s = sorted(lat)
            n = len(s)
            print(
                "\n== latency_ms (choose_actions + submit) =="
                "\nticks=%d  min=%.0f  p50=%.0f  p90=%.0f  p99=%.0f  max=%.0f"
                % (n, s[0], s[n // 2], s[int(n * 0.90)], s[int(n * 0.99)], s[-1])
            )
            for thr in (1_000, 3_000, 10_000, 20_000, 30_000, 60_000):
                cnt = sum(1 for x in lat if x > thr)
                if cnt:
                    print(f"  latency > {thr/1000:g}s : {cnt} ticks")

        # inter-tick wall-clock gaps (the real "no action between ticks" signal)
        gaps = []  # (from_tick, to_tick, sec, mid_ts)
        for a, b in zip(recs, recs[1:]):
            ta = parse_ts(a.get(ts_key, ""))
            tb = parse_ts(b.get(ts_key, ""))
            if ta is None or tb is None:
                continue
            d = tb - ta
            if d < 0:
                d += 86400  # day wrap
            gaps.append((a["tick"], b["tick"], d, b.get(ts_key, "")))

        if gaps:
            secs = [g[2] for g in gaps]
            gs = sorted(secs)
            gn = len(gs)
            print(
                "\n== inter-tick gap (wall-clock seconds between recorded ticks) =="
                "\nn=%d  min=%.1f  p50=%.1f  p90=%.1f  p99=%.1f  max=%.1f"
                % (gn, gs[0], gs[gn // 2], gs[int(gn * 0.90)],
                   gs[int(gn * 0.99)], gs[-1])
            )
            for thr in (5, 10, 20, 30, 60, 120):
                cnt = sum(1 for x in secs if x > thr)
                if cnt:
                    print(f"  gap > {thr}s : {cnt}")

            print("\n== 20 largest inter-tick gaps ==")
            for from_t, to_t, sec, mid in sorted(gaps, key=lambda g: -g[2])[:20]:
                # show how many ticks were skipped
                skipped = to_t - from_t - 1
                print(f"  tick {from_t}->{to_t}  gap={sec:7.1f}s  "
                      f"skipped={skipped:3d}  near={mid[:19]}")

        # latency band trend (does it slow down as map memory grows?)
        print("\n== latency by tick band (ms) ==")
        band: dict[int, list] = collections.OrderedDict()
        for j in recs:
            t = j["tick"]
            b = (t // 1000) * 1000
            band.setdefault(b, []).append(j.get("latency_ms", 0))
        for b, vs in band.items():
            vs = [x for x in vs if x is not None]
            if not vs:
                continue
            ss = sorted(vs)
            n = len(ss)
            print("  tick ~%-6s n=%-4d p50=%-7.0f p99=%-7.0f max=%-7.0f"
                  % (b, n, ss[n // 2], ss[int(n * 0.99)], ss[-1]))

    # 2. Pull the unstructured stdout log tail and grep for signposts
    print("\n== tactic_play.log signpost counts (full file) ==")
    signposts = [
        ("stream error / ConnectionClosed", "grep -c -E 'stream error|ConnectionClosed'"),
        ("ConnectionClosedError", "grep -c 'ConnectionClosedError'"),
        ("TICK_MISMATCH / submit_error", "grep -c -E 'TICK_MISMATCH|submit_error'"),
        ("plan_error", "grep -c 'plan_error'"),
        ("connecting session=", "grep -c 'connecting session='"),
        ("reconnecting in", "grep -c 'reconnecting in'"),
        ("desynced", "grep -c 'desynced'"),
    ]
    for label, grep in signposts:
        cnt = run_cmd(client,
            f"docker exec arena-game-app-1 {grep} /app/runtime/tactic_play.log 2>&1"
        )
        print(f"  {label:40s} {cnt}")

    # 3. Context around the biggest gaps — pull stdout lines near those
    # timestamps to see what was logged in the gap.
    print("\n== last 60 lines of tactic_play.log ==")
    print(fetch_log_text(client, "/app/runtime/tactic_play.log", tail_lines=60))

    # 4. Sample any reconnect / stream-error blocks
    print("\n== recent 'connecting session=' / 'stream error' context ==")
    print(run_cmd(client,
        "docker exec arena-game-app-1 sh -c "
        "'grep -n -E \"connecting session=|stream error|ConnectionClosed"
        "|reconnecting in|TICK_MISMATCH|plan_error\" "
        "/app/runtime/tactic_play.log 2>&1 | tail -40'"))

    client.close()
    print("\n[done] remote connection closed.")


if __name__ == "__main__":
    main()
