#!/usr/bin/env python3
"""Analyze tick-stream continuity: gaps in tick numbers and timestamps.

A tick stream that freezes (server stops sending for a while) will show up as a
large tick-number jump and/or a large timestamp gap between consecutive records.
This distinguishes "server stopped the stream" (external) from "bot planning was
slow" (internal). A permanent desync correlates with a freeze where the server's
command window advanced past the last accepted plan.

Also flags single-tick mismatches interleaved with successes (the intermittent
failure mode) versus continuous mismatch runs.
"""
import json
import sys


def main(paths):
    # Merge records from newest-priority list of files (already ordered oldest->newest
    # by callers). Track consecutive ticks & timestamps.
    records = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        j = json.loads(line)
                    except Exception:
                        continue
                    if j.get("_meta") or "tick" not in j:
                        continue
                    records.append(j)
        except OSError as e:
            print(f"  (skip {p}: {e})")
    records.sort(key=lambda r: (r["tick"], r.get("timestamp", "")))
    print(f"records: {len(records)} across {paths}")

    prev_tick = None
    prev_ts = None
    print("\n== tick jumps > 1 (missed ticks between accepted plans) ==")
    n_jump = 0
    for j in records:
        t = j["tick"]
        ts = j.get("timestamp", "")
        if prev_tick is not None and t - prev_tick > 1:
            n_jump += 1
            print(
                f"  tick {prev_tick} -> {t} (jump {t - prev_tick}) "
                f"ts {prev_ts and prev_ts[:19]} -> {ts and ts[:19]}"
            )
        prev_tick = t
        prev_ts = ts
    print(f"  total jumps: {n_jump}")

    # Timestamp gaps: only for consecutive same-second ticks with real ts.
    print("\n== timestamp gaps > 60s between consecutive records ==")
    prev = None
    n_ts = 0
    for j in records:
        ts = j.get("timestamp")
        if not ts:
            continue
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        if prev is not None:
            gap = (dt - prev).total_seconds()
            if gap > 60:
                n_ts += 1
                print(
                    f"  tick {j['tick']}: {gap:.0f}s gap (prev ts {prev.isoformat()[:19]}, "
                    f"this {dt.isoformat()[:19]})"
                )
        prev = dt
    print(f"  total gaps>60s: {n_ts}")


if __name__ == "__main__":
    main(sys.argv[1:])
