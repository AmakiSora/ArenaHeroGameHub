#!/usr/bin/env python3
"""Analyze the structured tactic log for tick-latency spikes (server-side).

Hypothesis under test: the bot's per-tick planning (choose_actions + submit)
sometimes stalls long enough (>= keepalive ping_timeout=60s) that the sync
websockets client cannot read pongs in time, forcing ConnectionClosed 1011
keepalive-ping-timeout, which is the direct cause of the session desyncs.
"""
import collections
import json
import sys


def load(path: str):
    recs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if j.get("_meta"):
                continue
            recs.append(j)
    return recs


def main(path: str) -> None:
    recs = load(path)
    lat = [j.get("latency_ms") for j in recs]
    lat = [x for x in lat if x is not None]
    print(f"ticks recorded: {len(recs)}")
    print(f"latency samples: {len(lat)}")
    if not lat:
        return
    s = sorted(lat)
    n = len(s)
    print(
        "latency ms: min=%.0f p50=%.0f p90=%.0f p99=%.0f max=%.0f"
        % (s[0], s[n // 2], s[int(n * 0.90)], s[int(n * 0.99)], s[-1])
    )
    for thr in (10_000, 20_000, 30_000, 40_000, 60_000):
        print(f"count >{thr/1000:.0f}s: {sum(1 for x in lat if x > thr)}")

    print("\n== 15 slowest ticks ==")
    for j in sorted(recs, key=lambda r: r.get("latency_ms", 0), reverse=True)[:15]:
        print(
            "tick=%s lat=%.0fms pop=%s workers=%s enemies=%s %s"
            % (
                j["tick"],
                j.get("latency_ms", 0),
                j.get("population"),
                len(j.get("workers", [])),
                len(j.get("visible_enemies", [])),
                j.get("timestamp", "")[:19],
            )
        )

    # Per-1000-tick latency trend: does planning slow down as map memory grows?
    print("\n== latency by tick band (ms) ==")
    band = collections.OrderedDict()
    for j in recs:
        t = j["tick"]
        b = (t // 1000) * 1000
        v = j.get("latency_ms", 0)
        band.setdefault(b, []).append(v)
    for b, vs in band.items():
        vs = [x for x in vs if x is not None]
        if not vs:
            continue
        s = sorted(vs)
        n = len(s)
        print(
            "tick ~%-6s n=%-4d p50=%-7.0f p99=%-7.0f max=%-7.0f"
            % (b, n, s[n // 2], s[int(n * 0.99)], s[-1])
        )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/app/runtime/tactic_log.jsonl")
