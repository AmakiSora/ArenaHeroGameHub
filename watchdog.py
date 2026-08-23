"""Watchdog: ensure direct_wrapper.py stays running and producing ticks.

The second half of this module is the per-unit stall alert used by the
tactic loop. tactic.py feeds one position snapshot per Tick through
``update_stall_tracking``; the watchdog raises a ``stall_alert`` line (the
tactic process' stdout is redirected into tactic_play.log by
docker-entrypoint.py, so the alert lands in the same raw play log as every
other ``[...]`` marker) once a unit's net displacement stays <= 1 cell for
``STALL_ALERT_THRESHOLD`` consecutive ticks.

Liveness rule: every state except DEAD counts as alive (COMBAT / RALLY /
DROPPING / UNLOADING / CAPTURING / HOLD / ... all keep accumulating), and a
unit that disappears from a snapshot is treated as gone and pruned. The
dedup flag is cleared once the unit resumes moving (net displacement > 1),
so a later second stall episode raises a second alert — while a unit that
keeps sitting still is only alerted once per episode (no per-Tick spam).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable

WRAPPER_SCRIPT = os.path.join(os.path.dirname(__file__), "direct_wrapper.py")
CHECK_INTERVAL = 20  # seconds
STUCK_THRESHOLD = 60  # seconds without new tick = stuck

# ── unit stall alert (fed by tactic.py once per Tick) ────────────────────────

# Consecutive ticks of net displacement <= 1 before a stall alert fires.
STALL_ALERT_THRESHOLD = 50
# Net displacement (Manhattan, in cells) above which the unit counts as
# "moving again": clears the stall window AND the one-shot alert flag.
STALL_RESUME_DISPLACEMENT = 1
# The only state excluded from liveness. Every other status (including
# transient ones like COMBAT / RALLY / DROPPING / UNLOADING / CAPTURING and
# any future state) keeps accumulating, so a real stall in ANY live state is
# still detected. Units missing from a snapshot count as gone, not alive.
_DEAD_STATES = frozenset({"DEAD"})

# uid -> (anchor_tick, anchor_pos): the reference point of the current stall
# window. While the unit never strays more than STALL_RESUME_DISPLACEMENT
# from the anchor, the window spans from anchor_tick to now; drifting beyond
# it re-anchors on the current cell (a genuinely moving unit therefore never
# accumulates, even at 1 cell per tick).
_stall_anchor: dict[str, tuple[int, tuple[int, int]]] = {}
# uid -> True once the alert for the current stall episode has been emitted.
# Cleared on resume so a repeat offender alerts again.
_emitted_stall_alerts: set[str] = set()


def reset_stall_watchdog() -> None:
    """Drop all stall tracking state (process restarts and tests)."""
    _stall_anchor.clear()
    _emitted_stall_alerts.clear()


def get_stall_ticks(uid: str, tick: int) -> int:
    """Consecutive ticks the unit has sat within one cell of its anchor.

    Counts the anchor tick itself: a unit observed at one spot on ticks
    t..t+n-1 has stalled n ticks at tick t+n-1. Returns 0 when untracked.
    """
    anchor = _stall_anchor.get(str(uid))
    if anchor is None:
        return 0
    return max(0, tick - anchor[0] + 1)


def is_stall_alert_emitted(uid: str) -> bool:
    """Whether the one-shot alert flag is currently set for this unit."""
    return str(uid) in _emitted_stall_alerts


def _is_unit_alive(status: object) -> bool:
    """Everything except DEAD (or absence from the snapshot) is alive.

    Deliberately NOT a whitelist of WAIT/MOVING/HOLD/CAPTURING: a unit stuck
    in COMBAT / RALLY / DROPPING / UNLOADING (or any future state) must keep
    accumulating stall ticks instead of resetting the counter or being
    skipped entirely.
    """
    if status is None:
        return True
    return str(status).strip().upper() not in _DEAD_STATES


def _emit_stall_alert(
    uid: str,
    tick: int,
    pos: tuple[int, int],
    stalled_ticks: int,
    *,
    writer: Callable[[str], None] | None = None,
) -> str:
    """Format and emit one stall alert line; returns the line.

    Default writer is print(): the tactic process' stdout is captured into
    tactic_play.log by docker-entrypoint.py, matching every other
    ``[tactic]`` / ``[log]`` / ``[team]`` marker already there.
    """
    line = (
        f"[watchdog] stall_alert tick={tick} unit={uid} "
        f"pos=({pos[0]}, {pos[1]}) stalled_ticks={stalled_ticks}"
    )
    emit = writer if writer is not None else print
    try:
        emit(line)
    except Exception:
        # An alert must never break the game loop.
        pass
    return line


def _normalize_snapshot(entry: object) -> tuple[str, tuple[int, int], object] | None:
    """Accept unit-like objects or (uid, pos) / (uid, pos, status) tuples."""
    if isinstance(entry, tuple):
        if len(entry) == 2:
            uid, pos = entry
            status = None
        elif len(entry) >= 3:
            uid, pos, status = entry[0], entry[1], entry[2]
        else:
            return None
    else:
        uid = getattr(entry, "id", None)
        pos = getattr(entry, "position", None)
        status = getattr(entry, "status", None)
    if uid is None or pos is None:
        return None
    try:
        x, y = int(pos[0]), int(pos[1])
    except (TypeError, ValueError, IndexError):
        return None
    return str(uid), (x, y), status


def update_stall_tracking(
    snapshots: Iterable[object],
    tick: int,
    *,
    threshold: int = STALL_ALERT_THRESHOLD,
    writer: Callable[[str], None] | None = None,
) -> list[dict[str, object]]:
    """Feed this Tick's unit snapshot; return any new stall alerts.

    Best-effort by contract: any unexpected error is swallowed and reported
    as an empty alert list — the watchdog must never break the tactic loop.
    """
    try:
        return _update_stall_tracking_impl(
            snapshots, int(tick), threshold=int(threshold), writer=writer,
        )
    except Exception:
        return []


def _update_stall_tracking_impl(
    snapshots: Iterable[object],
    tick: int,
    *,
    threshold: int,
    writer: Callable[[str], None] | None,
) -> list[dict[str, object]]:
    alerts: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in snapshots:
        normalized = _normalize_snapshot(entry)
        if normalized is None:
            continue
        uid, pos, status = normalized
        if not _is_unit_alive(status):
            # Dead units stop being tracked (and can no longer hold a flag).
            _stall_anchor.pop(uid, None)
            _emitted_stall_alerts.discard(uid)
            continue
        seen.add(uid)
        anchor = _stall_anchor.get(uid)
        if anchor is None or tick < anchor[0]:
            anchor = (tick, pos)
        anchor_tick, anchor_pos = anchor
        displacement = abs(pos[0] - anchor_pos[0]) + abs(pos[1] - anchor_pos[1])
        if displacement > STALL_RESUME_DISPLACEMENT:
            # Resumed moving: restart the window and allow future re-alerts.
            _stall_anchor[uid] = (tick, pos)
            _emitted_stall_alerts.discard(uid)
            continue
        _stall_anchor[uid] = anchor
        # +1: the anchor tick itself counts as the first stalled tick, so a
        # unit first seen at t alerts at t+threshold-1 (exactly threshold
        # consecutive ticks of no displacement).
        stalled_ticks = tick - anchor_tick + 1
        if stalled_ticks >= threshold and uid not in _emitted_stall_alerts:
            _emitted_stall_alerts.add(uid)
            _emit_stall_alert(uid, tick, pos, stalled_ticks, writer=writer)
            alerts.append({
                "uid": uid,
                "tick": tick,
                "pos": pos,
                "stalled_ticks": stalled_ticks,
            })
    # Vanished units (not present in this Tick's snapshot) are gone: drop
    # their tracking so a respawned id never inherits a stale window.
    for uid in set(_stall_anchor) - seen:
        _stall_anchor.pop(uid, None)
        _emitted_stall_alerts.discard(uid)
    return alerts


# ── process watchdog (direct_wrapper.py supervision) ─────────────────────────

def get_wrapper_pids() -> list[str]:
    """Find PIDs of processes running direct_wrapper.py."""
    try:
        result = subprocess.run(
            ["wmic", "process", "where", "CommandLine like '%direct_wrapper%'", "get", "ProcessId"],
            capture_output=True, text=True, timeout=5,
        )
        pids = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.isdigit():
                pids.append(line)
        return pids
    except Exception:
        return []


def get_last_tick_from_log() -> int | None:
    """Parse the last tick number from direct_play.log."""
    try:
        with open("direct_play.log", "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line in reversed(lines):
            if "[wrapper] tick=" in line:
                parts = line.strip().split("tick=")
                if len(parts) >= 2:
                    tick_str = parts[1].split()[0].rstrip("]")
                    return int(tick_str)
    except Exception:
        pass
    return None


def get_log_mtime() -> float | None:
    """Get modification time of direct_play.log."""
    try:
        return os.path.getmtime("direct_play.log")
    except Exception:
        return None


def start_wrapper() -> subprocess.Popen | None:
    """Start the wrapper as a fully detached process."""
    try:
        proc = subprocess.Popen(
            [sys.executable, WRAPPER_SCRIPT],
            stdout=open("direct_play.log", "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            cwd=os.getcwd(),
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        print(f"[watchdog] started wrapper pid={proc.pid}", flush=True)
        return proc
    except Exception as exc:
        print(f"[watchdog] failed to start: {exc}", flush=True)
        return None


def main() -> int:
    print("[watchdog] starting watchdog", flush=True)
    
    # Start wrapper initially
    proc = start_wrapper()
    if proc is None:
        print("[watchdog] cannot start wrapper, exiting", flush=True)
        return 1
    
    last_tick = get_last_tick_from_log()
    last_mtime = get_log_mtime()
    print(f"[watchdog] monitoring pid={proc.pid}  initial_tick={last_tick}", flush=True)
    
    while True:
        time.sleep(CHECK_INTERVAL)
        
        # Check 1: Is wrapper process still alive?
        pids = get_wrapper_pids()
        if not pids:
            print("[watchdog] wrapper process dead, restarting...", flush=True)
            proc = start_wrapper()
            if proc is None:
                continue
            last_tick = None
            last_mtime = None
            continue
        
        # Check 2: Is log file still being updated?
        current_mtime = get_log_mtime()
        if last_mtime is not None and current_mtime is not None:
            if current_mtime == last_mtime:
                # Log hasn't changed in STUCK_THRESHOLD seconds
                age = time.time() - current_mtime
                if age > STUCK_THRESHOLD:
                    print(f"[watchdog] log stuck for {age:.0f}s, restarting...", flush=True)
                    for pid in pids:
                        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                    proc = start_wrapper()
                    if proc is None:
                        continue
                    last_tick = None
                    last_mtime = None
                    continue
        last_mtime = current_mtime
        
        # Check 3: Is tick advancing?
        current_tick = get_last_tick_from_log()
        if last_tick is not None and current_tick is not None:
            if current_tick <= last_tick:
                age = time.time() - (current_mtime or time.time())
                if age > STUCK_THRESHOLD:
                    print(f"[watchdog] tick stuck at {current_tick} for {age:.0f}s, restarting...", flush=True)
                    for pid in pids:
                        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                    proc = start_wrapper()
                    if proc is None:
                        continue
                    last_tick = None
                    last_mtime = None
                    continue
        last_tick = current_tick
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
