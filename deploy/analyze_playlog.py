#!/usr/bin/env python3
"""Reconstruct the tactic_play.log timeline: sessions, gaps, mismatches.

Answers:
  - How many keepalive timeouts / reconnects, and did each recover or desync?
  - How many ticks elapse between last success and each failure?
  - Pattern around the 3 sustained desyncs (09:51 / 12:50 / 13:38).
"""
import re
import sys

SUCCESS_RE = re.compile(r"^tick=(\d+) core=")
MISMATCH_RE = re.compile(r"^tick=(\d+) submit_error=409 (TICK_MISMATCH|COMMAND_WINDOW_CLOSED)")
STREAK_RE = re.compile(r"^tick=(\d+) submit_error=409 TICK_MISMATCH \(streak=")
CONNECT_RE = re.compile(r"\[tactic\] connecting session=(\d+)")
RECONNECT_RE = re.compile(r"\[tactic\] reconnecting")
KEEPALIVE_RE = re.compile(r"keepalive ping timeout|ConnectionClosedError")
STREAM_ERR_RE = re.compile(r"\[tactic\] (stream error|unexpected error) session=")
MARKER_RE = re.compile(r"\[entrypoint\] (.*)")


def main(path: str) -> None:
    events = []  # (lineno, kind, tick_or_none, detail)
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if MARKER_RE.match(line):
                events.append((i, "entrypoint", None, line.strip()))
                continue
            m = CONNECT_RE.search(line)
            if m:
                events.append((i, "connect", None, f"session={m.group(1)}"))
                continue
            m = KEEPALIVE_RE.search(line)
            if m:
                events.append((i, "keepalive_timeout", None, "1011 keepalive ping timeout"))
                continue
            m = STREAM_ERR_RE.search(line)
            if m:
                events.append((i, "stream_error", None, line.strip()[:120]))
                continue
            m = SUCCESS_RE.match(line)
            if m:
                events.append((i, "success", int(m.group(1)), None))
                continue
            m = MISMATCH_RE.match(line)
            if m:
                events.append((i, "mismatch", int(m.group(1)), m.group(2)))
                continue

    # Summarize each gap: a failure episode is a run of mismatch/mismatch events
    # with no success between. For each episode record last success tick, first
    # and last failure tick, count, and whether a success followed after.
    print(f"events parsed: {len(events)}")
    success_tick = None
    episodes = []
    cur = None
    for lineno, kind, tick, detail in events:
        if kind == "success":
            success_tick = tick
            cur = None
        elif kind in ("mismatch", "keepalive_timeout", "stream_error"):
            if cur is None:
                cur = {
                    "line": lineno,
                    "last_success": success_tick,
                    "errors": [],
                    "err_types": set(),
                }
            cur["errors"].append(tick if tick is not None else lineno)
            cur["err_types"].add(detail if detail else kind)
        elif kind == "connect":
            if cur is not None:
                episodes.append(cur)
                cur = None
        elif kind == "entrypoint":
            if cur is not None:
                episodes.append(cur)
                cur = None

    print(f"failure episodes: {len(episodes)}\n")
    for ep in episodes:
        ticks = [t for t in ep["errors"] if t is not None]
        types = ", ".join(sorted(ep["err_types"]))
        span = f"{min(ticks)}..{max(ticks)}" if ticks else f"(no tick, lines {ep['errors'][0]})"
        print(
            f"ep@line{ep['line']}: last_success={ep['last_success']} "
            f"tick_span={span} n_errors={len(ep['errors'])} [{types}]"
        )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/app/runtime/tactic_play.log")
