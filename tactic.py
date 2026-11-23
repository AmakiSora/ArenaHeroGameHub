"""
Balanced Arena Hero tactic with play-by-play logging
=====================================================
Records every Tick's decisions into a JSONL log file for later analysis.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import time
import traceback
import gzip
import shutil

import watchdog

from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Set as AbstractSet
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from functools import wraps
from getpass import getpass
from heapq import heappop, heappush, merge
from pathlib import Path
from typing import Any

from arena_hero import (
    ArenaHeroClient,
    CoreState,
    Direction,
    UnitType,
    unit_cost,
)
from arena_hero.errors import APIError, ProtocolError, TransportError
import game_stats
import map_codec
from state_io import append_jsonl, atomic_write_text, data_dir, data_path, file_lock
from tactic_config import CONFIG_PATH, load_config, mutate_config, parse_team_ratio

MAP_MEMORY_PATH = data_path("map_memory.json")
DEFAULT_LOG_PATH = str(data_path("tactic_log.jsonl"))
MAP_COLD_PRUNE_MARKER = MAP_MEMORY_PATH.parent / "map_cold_prune.done"
# Categorized battle log (discoveries / kills / defeats / config changes) that
# both the tactic process and the dashboard process append to.
BATTLE_LOG_PATH = data_path("battle_log.jsonl")
# Manual per-unit target coordinates set from the dashboard (display-name keyed).
WAYPOINTS_PATH = data_path("waypoints.json")
# Manual per-unit self-destruct commands set from the dashboard (display-name keyed).
SELF_DESTRUCT_PATH = data_path("self_destruct.json")
# Manual per-unit hold-position (驻守) commands set from the dashboard
# (display-name keyed): a held unit stands in place and auto-attacks enemies
# entering its range instead of following its normal program.
HOLDS_PATH = data_path("holds.json")
# Ordered attack-target queues per combat squad ("attack" / "kite"), set from
# the dashboard / web console: {"attack": [[x, y], ...], "kite": [...]}. Only
# the coords mode consumes a queue; the head pops once the squad arrived and
# the area is cleared (see _advance_squad_target_queue).
SQUAD_TARGETS_PATH = data_path("squad_targets.json")

# Display-name prefix per unit type (W / V / R), shared with the dashboard.
_UNIT_NAME_PREFIX = {
    UnitType.WORKER: "W",
    UnitType.VANGUARD: "V",
    UnitType.RANGER: "R",
}

# Rotate tactic_log.jsonl when it exceeds this size and keep at most N backups.
LOG_MAX_BYTES = int(os.environ.get("ARENA_LOG_MAX_MB", "20")) * 1024 * 1024
LOG_BACKUP_COUNT = 3
# Guerrilla route recorder: one date-stamped append-only file per local day
# (guerrilla_routes-YYYYMMDD.jsonl), one tiny row per guerrilla unit per Tick.
# tactic_log.jsonl is ~22KB/Tick and rotates every ~890 Ticks (~4h at the live
# ~16s Tick cycle), so it can never hold a whole day of routes; these rows are
# ~60B and the 3-day window costs about 10MB on the runtime volume.
GUERRILLA_ROUTE_PREFIX = "guerrilla_routes-"
GUERRILLA_ROUTE_KEEP_DAYS = 3
# A day file this big means something is writing far more than the expected
# ~8 units x 5400 Ticks, so recording pauses rather than filling the volume.
GUERRILLA_ROUTE_MAX_DAY_BYTES = 64 * 1024 * 1024
# Shutdown summary only reads this many of the newest tick records.
_SUMMARY_TAIL_RECORDS = 10000
# Print a per-Tick phase breakdown here when planning+submit crossed this many
# ms, so multi-second spikes (the source of ticks that look "stuck" on the live
# server) get attributed to a phase without re-running offline. Override with
# the ARENA_SLOW_PLAN_THRESHOLD_MS env var; 0 always prints.
_SLOW_PLAN_THRESHOLD_MS = float(
    os.environ.get("ARENA_SLOW_PLAN_THRESHOLD_MS", "3000")
)

# Enemy motion is sampled in shadow mode only. Three consecutive steps are
# required before a lead shot becomes eligible; two consecutive zero-steps
# establish that a target is stationary.
_ENEMY_MOTION_HISTORY = 4
_STABLE_MOVE_STREAK = 3
_STATIONARY_STREAK = 2

# Base price of each spawnable unit type (demand-based production). The actual
# charge grows with population — see _spawn_cost below.
UNIT_SPAWN_COSTS = {
    "WORKER": 5,
    "VANGUARD": 10,
    "RANGER": 12,
}
# Spawn priority when several types are below their target (economy first).
# Once the worker target is met the combat types alternate instead of filling
# Vanguards before Rangers — see _spawn_order.
_SPAWN_PRIORITY = (UnitType.WORKER, UnitType.VANGUARD, UnitType.RANGER)
# Config key for each unit type's target, and its fallback when unset.
_TARGET_KEYS = {
    UnitType.WORKER: "target_workers",
    UnitType.VANGUARD: "target_vanguards",
    UnitType.RANGER: "target_rangers",
}
_TARGET_DEFAULTS = {
    UnitType.WORKER: 10,
    UnitType.VANGUARD: 2,
    UnitType.RANGER: 2,
}


# ── geometry helpers ─────────────────────────────────────────────────────────

def _manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _chebyshev(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Chebyshev (max-norm) distance — the 8-connected step metric used for
    Ranger range checks and attack-squad engagement radii."""
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _merge_resource_cells(
    visible: Iterable[tuple[int, int]],
    remembered: set[tuple[int, int]],
    depleted: set[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Return unique, available resource cells in deterministic order."""
    cells = {tuple(position) for position in visible}
    cells.update(tuple(position) for position in remembered)
    return sorted(cells - depleted)


def _step_towards(
    src: tuple[int, int],
    dst: tuple[int, int],
) -> Direction | None:
    dx = dst[0] - src[0]
    dy = dst[1] - src[1]
    if dx == 0 and dy == 0:
        return None
    if abs(dx) >= abs(dy):
        return Direction.RIGHT if dx > 0 else Direction.LEFT
    return Direction.DOWN if dy > 0 else Direction.UP


def _line_blocked(
    a: tuple[int, int],
    b: tuple[int, int],
    obstacles: frozenset[tuple[int, int]],
) -> bool:
    """True when an intermediate cell on the straight a→b line is blocked.

    Supports horizontal, vertical, and exact 45° diagonals (rules v0.8). Only
    the cells actually crossed are checked; obstacles beside the line never
    block. A non-aligned line (not a legal shot) returns True.
    """
    x1, y1 = a
    x2, y2 = b
    dx = x2 - x1
    dy = y2 - y1
    if not (dx == 0 or dy == 0 or abs(dx) == abs(dy)):
        return True
    sx = (dx > 0) - (dx < 0)
    sy = (dy > 0) - (dy < 0)
    x, y = x1 + sx, y1 + sy
    while (x, y) != (x2, y2):
        if (x, y) in obstacles:
            return True
        x += sx
        y += sy
    return False


def _vision_obstructed(
    a: tuple[int, int],
    b: tuple[int, int],
    obstacles: frozenset[tuple[int, int]] | set[tuple[int, int]],
) -> bool:
    """True when an obstacle cell lies on the integer supercover line a→b.

    Vision follows the game rule: obstacles block sight along the line, the
    obstacle cell itself is visible but cells behind it are not, and when the
    line passes exactly through a shared corner an obstacle in either adjacent
    cell blocks it. The viewer `a` and the looked-at cell `b` never block.
    """
    x0, y0 = a
    x1, y1 = b
    if (x0, y0) == (x1, y1):
        return False
    dx = x1 - x0
    dy = y1 - y0
    adx = abs(dx)
    ady = abs(dy)
    sx = 1 if dx > 0 else -1
    sy = 1 if dy > 0 else -1

    if adx == 0:  # purely vertical
        for yy in range(y0 + sy, y1, sy):
            if (x0, yy) in obstacles:
                return True
        return False
    if ady == 0:  # purely horizontal
        for xx in range(x0 + sx, x1, sx):
            if (xx, y0) in obstacles:
                return True
        return False

    # Integer DDA over the grid the segment crosses. `px`/`py` are the progress
    # (in units of adx*ady) at which the segment next hits a vertical/horizontal
    # gridline; equal values mean it passes exactly through a shared corner, so
    # an obstacle on either side blocks the line (rules supercover rule).
    px = ady
    py = adx
    cx, cy = x0, y0
    while (cx, cy) != (x1, y1):
        if px < py:
            cx += sx
            px += ady
        elif py < px:
            cy += sy
            py += adx
        else:
            if (cx + sx, cy) in obstacles or (cx, cy + sy) in obstacles:
                return True
            cx += sx
            cy += sy
            px += ady
            py += adx
        if (cx, cy) == (x1, y1):
            break
        if (cx, cy) in obstacles:
            return True
    return False


# ── Dead-end map recognition ──────────────────────────────────────────────────
# A free cell with only one open cardinal neighbor is a 凸-shaped cul-de-sac
# (three sides walled). One-wide corridors that only lead into such pockets are
# expanded iteratively so explorers do not walk into obvious dead ends.

_CARDINAL_DELTAS: tuple[tuple[int, int], ...] = ((0, -1), (1, 0), (0, 1), (-1, 0))
_dead_end_cache_key: frozenset[tuple[int, int]] | None = None
_dead_end_cache: frozenset[tuple[int, int]] = frozenset()
# Largest wall set a from-scratch dead-end batch may be computed for (see the
# report #8 safety net in _dead_end_cells_batch). The persistent structure
# seeds the production map (~488k walls) through the seeding_structure flag.
_DEAD_END_BATCH_MAX_WALLS = 100_000

# Persistent incremental dead-end structure for the permanent obstacle memory.
# _dead_obstacles is the exact obstacle set the structure covers — the same
# object as _known_obstacles in the normal flow, so the wall fast-path is an
# O(1) identity hit. Maintained add-only by _dead_add_walls(); extras
# (transient occupied cells) are derived on a scratch copy, never here.
_dead_obstacles: frozenset[tuple[int, int]] = frozenset()
_dead_open_count: dict[tuple[int, int], int] = {}   # candidate free cell -> open degree
_dead_set: set[tuple[int, int]] = set()             # current wall dead ends
_dead_view: frozenset[tuple[int, int]] = frozenset()  # cached frozenset(_dead_set)
_dead_structure_built: bool = False
# Stable per-tick view of _obstacle_memory: the same frozenset object across
# ticks unless walls actually grew (see _update_obstacle_memory).
_known_obstacles: frozenset[tuple[int, int]] = frozenset()
# Monotone version of _obstacle_memory plus the version _known_obstacles (and the
# persistent structure above) currently mirrors. The per-Tick "did the wall map
# change?" test used to be `_known_obstacles != _obstacle_memory` — a ~488k-cell
# set comparison on every no-growth Tick (21-32 ms measured, ×2-3 on the VPS, see
# OPTIMIZATION_REVIEW.md #6). Two integers decide it instead. Every mutation site
# must bump the version (direct in-place mutation: call _bump_obstacle_generation);
# _reset_dead_structure records the version it rebased the structure to.
_obstacle_mem_generation: int = 0
_known_obstacles_generation: int = 0
# Cells sampled per side by _obstacle_view_is_current. Small maps (all tests) are
# probed exhaustively; the production map costs O(1) per Tick.
_OBSTACLE_VIEW_PROBE_CELLS = 8


def _bump_obstacle_generation() -> None:
    """Mark _obstacle_memory as changed outside _update_obstacle_memory.

    Call this after mutating the wall set directly (a wholesale reload, the cold
    prune, or the Core's learned blocked cell) so the next _update_obstacle_memory
    rebases the stable view and the incremental dead-end structure.
    """
    global _obstacle_mem_generation
    _obstacle_mem_generation += 1


def _obstacle_view_is_current() -> bool:
    """True when _known_obstacles still mirrors _obstacle_memory.

    Cheap replacement for the full set comparison: the version counter catches
    every mutation this module performs, and a constant-size sample probe is the
    safety net for code that clears/restores the set directly (tests, tools).
    The probe is exhaustive for maps of at most _OBSTACLE_VIEW_PROBE_CELLS walls.
    """
    if _known_obstacles_generation != _obstacle_mem_generation:
        return False
    if len(_known_obstacles) != len(_obstacle_memory):
        return False
    known = _known_obstacles
    probe = 0
    for cell in _obstacle_memory:
        if cell not in known:
            return False
        probe += 1
        if probe >= _OBSTACLE_VIEW_PROBE_CELLS:
            break
    if probe < len(_obstacle_memory):
        # Memory is larger than the probe: sample the stable view as well, so a
        # same-cardinality replacement is still noticed.
        probe = 0
        for cell in known:
            if cell not in _obstacle_memory:
                return False
            probe += 1
            if probe >= _OBSTACLE_VIEW_PROBE_CELLS:
                break
    return True


def _neighbor_cells(pos: tuple[int, int]) -> tuple[tuple[int, int], ...]:
    x, y = pos
    return tuple((x + dx, y + dy) for dx, dy in _CARDINAL_DELTAS)


def _open_degree(
    pos: tuple[int, int],
    blocked: frozenset[tuple[int, int]] | set[tuple[int, int]],
) -> int:
    """Count free cardinal neighbors of pos (cells not in blocked)."""
    return sum(1 for n in _neighbor_cells(pos) if n not in blocked)


class _PhaseTimer:
    """Per-Tick wall-clock profiler for choose_actions, broken into named phases.

    Zero-overhead when idle: phases only call time.monotonic() once at start
    and once at stop, so the hot unit loop stays cheap. Accumulated phase
    times flow into the structured log (phase_ms) and a stdout summary line
    is printed only when planning is slow, so healthy ticks emit nothing
    extra. Used to localize the multi-second planning spikes seen on the
    live server (see analyze_latency.py / diag_remote6.py).
    """

    def __init__(self) -> None:
        self.phases: dict[str, float] = {}
        self._start: float | None = None
        self._phase: str | None = None

    def start(self, name: str) -> None:
        # Stop the previous phase implicitly so wraps don't lose time.
        if self._phase is not None:
            self.stop()
        self._phase = name
        self._start = time.monotonic()

    def stop(self) -> None:
        if self._start is None or self._phase is None:
            return
        self.phases[self._phase] = self.phases.get(self._phase, 0.0) + (
            time.monotonic() - self._start
        )
        self._phase = None
        self._start = None

    def total_ms(self) -> float:
        return round(1000.0 * sum(self.phases.values()), 1)

    def as_ms(self) -> dict[str, float]:
        return {k: round(1000.0 * v, 1) for k, v in self.phases.items()}


# ── Pathfinding instrumentation (reset every Tick by choose_actions) ────────
# Counts A* invocations and node expansions across one Tick so the summary
# line can attribute slow ticks to "many calls" vs "one huge search". A
# per-call overhead floor and the bfs_call counter live here rather than inside
# _bfs_path's hot loop to keep the search itself allocation-light.
_pathfind_calls: int = 0
_pathfind_expansions: int = 0
_pathfind_ms: float = 0.0
# Dead-end recomputation telemetry (the O(candidates×dead) cost that runs on
# every cache miss — once per tick while walls grow). Reset per tick alongside
# the pathfind counters.
_dead_end_ms: float = 0.0
_dead_end_runs: int = 0

# Squad-march A* budget. Combat objectives sit hundreds to thousands of cells
# away and the flat bfs_max_steps (2500) is exhausted long before them, so the
# unit re-decides a single step every Tick and shuttles instead of marching
# (observed: V11/V6 against a 1294-cell kite objective, V5/V10/R3 walking ~15
# cells toward (-337,-800) and being pulled back by regroup). The budget scales
# with distance like the carriers' home march, with three bounds on the blast
# radius because a whole squad re-targets in one Tick:
#   * goals closer than _MARCH_SCALE_FROM keep the caller's budget as-is;
#   * a unit that missed with a scaled budget waits _MARCH_MISS_COOLDOWN plan
#     calls before searching for that same goal again, so a genuinely sealed
#     far goal cannot burn its full budget every Tick;
#   * _MARCH_BUDGET_PER_TICK caps the scaled work all combat units may spend in
#     one Tick, so a mass re-target cannot stall the loop.
_MARCH_SCALE_FROM = 60
_MARCH_BUDGET_FACTOR = 60
_MARCH_BUDGET_MAX = 120_000
_MARCH_MISS_COOLDOWN = 120
_MARCH_BUDGET_PER_TICK = 300_000
# A march may keep following its cached route while the caller's objective stays
# within this many cells of the route's own goal. Objectives are re-picked every
# Tick (a kited enemy cluster jitters one cell, a migrating Core slides its ring
# slot), and an exact-equality cache check turns that jitter into a fresh
# full-budget A* (measured: 254/360 production Ticks over 2s, with V10 alone
# re-searching 150 times at the 120k cap for a goal that had moved <=8 cells).
# The last approach still re-anchors, so a unit always finishes on the cell the
# caller actually asked for.
_MARCH_GOAL_DRIFT_CELLS = 8
_march_route_miss: dict[str, tuple[tuple[int, int], int]] = {}
_march_budget_spent: int = 0

# Wall-clock cap for all A* work in one plan. A far regroup search can burn its
# whole 120k-expansion budget (6-13 s on the VPS) while the server's command
# window moves on, producing 409 TICK_MISMATCH and eventually a desync restart.
# Expiry is treated exactly like the expansion cap: no path, greedy fallback.
PLAN_PATHFIND_BUDGET_S = 2.0
_plan_pathfind_deadline: float | None = None


def _reset_pathfind_counters() -> None:
    global _pathfind_calls, _pathfind_expansions, _pathfind_ms
    global _dead_end_ms, _dead_end_runs, _march_budget_spent
    _pathfind_calls = 0
    _pathfind_expansions = 0
    _pathfind_ms = 0.0
    _dead_end_ms = 0.0
    _dead_end_runs = 0
    _march_budget_spent = 0


def _bfs_path_snapshot() -> tuple[int, int, float]:
    """Return (calls, expansions, ms) since the last reset."""
    return _pathfind_calls, _pathfind_expansions, round(_pathfind_ms, 1)


def _dead_end_snapshot() -> tuple[int, float]:
    """Return (runs, ms) of _dead_end_cells since the last reset."""
    return _dead_end_runs, round(_dead_end_ms, 1)


def _march_budget(
    uid: str,
    pos: tuple[int, int],
    goal: tuple[int, int],
    min_budget: int,
) -> int | None:
    """Expansion budget for a march toward ``goal``, or None to skip the search.

    See the ``_MARCH_*`` block for why the flat cap fails far objectives. None
    means the caller keeps its own budget-free fallback step, which is what the
    squad did before this scaling existed — never a worse outcome than a Tick
    without planning.
    """
    miss = _march_route_miss.get(uid)
    if miss is not None:
        missed_goal, remaining = miss
        # Count the cooldown down on every plan call, including the calls this
        # miss suppresses: a far goal can miss because a friendly body sits on a
        # chokepoint, and freezing it forever would strand the march.
        if remaining <= 1:
            _march_route_miss.pop(uid, None)
        else:
            _march_route_miss[uid] = (missed_goal, remaining - 1)
            if missed_goal == goal:
                return None
    dist = _manhattan(pos, goal)
    if dist < _MARCH_SCALE_FROM:
        return min_budget
    budget = min(_MARCH_BUDGET_MAX, max(min_budget, dist * _MARCH_BUDGET_FACTOR))
    global _march_budget_spent
    if _march_budget_spent + budget > _MARCH_BUDGET_PER_TICK:
        return None
    _march_budget_spent += budget
    return budget


def _march_note_result(
    uid: str,
    goal: tuple[int, int],
    budget: int | None,
    found: bool,
) -> None:
    """Remember a scaled-budget miss so the next 120 plan calls skip the search."""
    if budget is None:
        return
    if found:
        _march_route_miss.pop(uid, None)
    else:
        _march_route_miss[uid] = (goal, _MARCH_MISS_COOLDOWN)


def _reset_plan_profile_context() -> None:
    """Clear profiler output before planning a new Tick."""
    turn_context.plan_phase_ms = {}
    turn_context.plan_pathfind_calls = 0
    turn_context.plan_pathfind_expansions = 0
    turn_context.plan_pathfind_ms = 0.0
    turn_context.plan_dead_end_ms = 0.0
    turn_context.plan_dead_end_runs = 0


def _publish_plan_profile(phase: _PhaseTimer) -> None:
    """Publish this Tick's completed planning telemetry to ``turn_context``."""
    phase.stop()
    turn_context.plan_phase_ms = phase.as_ms()
    pf_calls, pf_expansions, pf_ms = _bfs_path_snapshot()
    turn_context.plan_pathfind_calls = pf_calls
    turn_context.plan_pathfind_expansions = pf_expansions
    turn_context.plan_pathfind_ms = pf_ms
    de_runs, de_ms = _dead_end_snapshot()
    turn_context.plan_dead_end_ms = de_ms
    turn_context.plan_dead_end_runs = de_runs


def _dead_end_cells_batch(
    obstacles: frozenset[tuple[int, int]] | set[tuple[int, int]],
    *,
    seeding_structure: bool = False,
) -> tuple[set[tuple[int, int]], dict[tuple[int, int], int]]:
    """Dead-end closure + final open-count, computed from scratch (batch).

    Returns (dead, open_count) where open_count[c] = number of neighbors of c
    that are free (not obstacle, not currently marked dead). The persistent
    incremental structure consumes both artifacts: the batch result seeds it,
    and incremental wall-adds reuse/mutate open_count.

    Cascade parity invariant: the cascade only propagates through *candidate*
    cells (free cells adjacent to at least one obstacle). A free cell adjacent
    to no obstacle is never a candidate and is never touched.

    Safety net (report #8): a from-scratch batch over the production map
    (~488k walls) costs 4.2 s and allocates two 2.1M-entry containers. Only the
    persistent structure's own seeding may do that; any other caller asking for
    a batch this large reuses the persistent result and warns instead. The
    primary fix keeps transient obstacle sets small — this is the backstop.
    """
    if not seeding_structure and len(obstacles) > _DEAD_END_BATCH_MAX_WALLS:
        print(
            f"[map] dead-end batch refused: {len(obstacles)} walls > "
            f"{_DEAD_END_BATCH_MAX_WALLS}; reusing persistent structure "
            f"(report #8 safety net)",
            flush=True,
        )
        _ensure_dead_structure()
        return set(_dead_view), {}
    obs = frozenset(obstacles)
    candidates: set[tuple[int, int]] = set()
    for cell in obs:
        for n in _neighbor_cells(cell):
            if n not in obs:
                candidates.add(n)
    open_count: dict[tuple[int, int], int] = {
        c: sum(1 for n in _neighbor_cells(c) if n not in obs)
        for c in candidates
    }
    dead: set[tuple[int, int]] = set()
    work = deque(c for c in candidates if open_count[c] <= 1)
    while work:
        cell = work.popleft()
        if cell in dead or open_count[cell] > 1:
            continue
        dead.add(cell)
        for n in _neighbor_cells(cell):
            if n in open_count and n not in dead:
                open_count[n] -= 1
                if open_count[n] <= 1:
                    work.append(n)
    return dead, open_count


def _dead_end_cells(
    obstacles: frozenset[tuple[int, int]] | set[tuple[int, int]],
) -> frozenset[tuple[int, int]]:
    """Return free cells that are obvious dead ends given known walls.

    Seed candidates are free cells adjacent to any known obstacle. A candidate
    is a dead end when it has at most one free neighbor. Marked dead ends are
    then treated as blocked so one-wide corridors collapse inward.

    Algorithm: a single work-list pass. A cell's open-degree only changes when a
    NEIGHBOR is newly marked dead, so we only re-check the neighbors of a cell
    just marked — not all candidates every iteration (the old re-scan-all loop
    was O(dead × candidates), the proven cause of multi-second ticks once the
    wall memory passed ~18k cells; see analyze_phase_latency.py).
    """
    global _dead_end_ms, _dead_end_runs
    if not obstacles:
        return frozenset()
    _dt0 = time.monotonic()
    _dead_end_runs += 1
    dead, _ = _dead_end_cells_batch(obstacles)
    _dead_end_ms += (time.monotonic() - _dt0) * 1000.0
    return frozenset(dead)


def _ensure_dead_structure() -> None:
    """Build the incremental structure for _dead_obstacles if absent."""
    global _dead_open_count, _dead_set, _dead_view, _dead_structure_built
    if _dead_structure_built:
        return
    _dt0 = time.monotonic()
    # Only the structure's own seeding may batch the production wall set; see
    # the size guard in _dead_end_cells_batch.
    _dead_set, _dead_open_count = _dead_end_cells_batch(
        _dead_obstacles, seeding_structure=True,
    )
    _dead_view = frozenset(_dead_set)
    _dead_structure_built = True
    # Count the batch build as a dead-end run for telemetry so the first call of
    # a fresh map is not silently invisible in the phase breakdown.
    global _dead_end_ms, _dead_end_runs
    _dead_end_runs += 1
    _dead_end_ms += (time.monotonic() - _dt0) * 1000.0


def _reset_dead_structure(obstacles: frozenset) -> None:
    """Rebase after a wholesale obstacle reload (_load_map_memory / out-of-band
    test mutation). The batch build is deferred to the next _ensure_dead_structure.

    Also rebases the stable _known_obstacles view and records the wall-map
    generation it now reflects, so the next _update_obstacle_memory does not
    rebuild the structure a second time. Every caller passes exactly the set it
    just published as _known_obstacles.
    """
    global _dead_obstacles, _dead_open_count, _dead_set, _dead_view, \
        _dead_structure_built, _known_obstacles, _known_obstacles_generation
    _dead_obstacles = obstacles
    _known_obstacles = obstacles
    _known_obstacles_generation = _obstacle_mem_generation
    _dead_open_count = {}
    _dead_set = set()
    _dead_view = frozenset()
    _dead_structure_built = False


def _dead_add_walls(new_walls) -> None:
    """Incrementally fold newly-discovered walls into the persistent structure.

    Walls only ever grow (a new wall can reduce a neighbor's open-degree to <=1,
    making it dead; it can never raise an open-degree), so this is add-only —
    a cell that is a dead end stays a dead end. Two subtle cases:

    - A newly-discovered wall `w` might itself have been a *marked dead-end free
      cell* (a unit sat in an alcove the server later reports as rock). `w` must
      leave the dead-set (walls are not dead ends), but its neighbors must NOT be
      decremented again — they were already decremented when `w` died. Guarded by
      the `was_dead` set.
    - Two new walls adjacent to one fresh candidate: the fresh count is computed
      against `new_obs` (all new walls folded in), and `touched` prevents the
      second wall from double-subtracting.
    """
    global _dead_obstacles, _dead_open_count, _dead_set, _dead_view, \
        _dead_structure_built
    if not new_walls:
        return
    _ensure_dead_structure()
    new_obs = _dead_obstacles | frozenset(new_walls)
    work: deque = deque()
    touched: set[tuple[int, int]] = set()
    was_dead: set[tuple[int, int]] = set()
    for w in new_walls:
        if w in _dead_set:
            was_dead.add(w)
        _dead_open_count.pop(w, None)
        _dead_set.discard(w)
    for w in new_walls:
        for n in _neighbor_cells(w):
            if n in new_obs or n in _dead_set or n in touched:
                continue
            if n in _dead_open_count:
                if w not in was_dead:
                    cnt = _dead_open_count[n] - 1
                    _dead_open_count[n] = cnt
                    if cnt <= 1:
                        work.append(n)
            else:
                cnt = sum(
                    1 for m in _neighbor_cells(n)
                    if m not in new_obs and m not in _dead_set
                )
                _dead_open_count[n] = cnt
                touched.add(n)
                if cnt <= 1:
                    work.append(n)
            # else: n already excludes w (w was a marked dead end) — no change.
    while work:
        cell = work.popleft()
        if cell in new_obs or cell in _dead_set or _dead_open_count.get(cell, 0) > 1:
            continue
        _dead_set.add(cell)
        for n in _neighbor_cells(cell):
            if n in new_obs or n in _dead_set:
                continue
            if n not in _dead_open_count:
                continue  # batch parity: cascade only propagates through candidates
            _dead_open_count[n] -= 1
            if _dead_open_count[n] <= 1:
                work.append(n)
    _dead_obstacles = new_obs
    _dead_view = frozenset(_dead_set)


def _dead_ends_with_extras(obstacles, extras) -> frozenset[tuple[int, int]]:
    """dead(O ∪ X) derived from the persistent wall structure on a scratch copy.

    `extras` are transient occupied cells (fellow units + visible enemies) that
    the caller unions into its search's obstacle set. Treating them as walls can
    collapse corridors only *this* tick, so the result must be recomputed per
    tick — but from the cached wall base, never from scratch.

    The cascade touches only the neighbourhood of `extras`, so it runs over a
    copy-on-write overlay instead of duplicating the whole open-count map. That
    map reached 2.1M candidate cells on production; copying it per A* call was
    a multi-hundred-millisecond term in slow ticks.

    If `obstacles` is not the persistent wall set (a caller built a different
    obstacle set, e.g. the rare core-cell-as-obstacle case), fall back to the
    batch `_dead_end_cells(obstacles | extras)` — exactly today's behavior.
    """
    global _dead_end_ms, _dead_end_runs
    if obstacles is not _dead_obstacles:
        return _dead_end_cells(obstacles | extras)
    if not extras:
        _ensure_dead_structure()
        return _dead_view
    _ensure_dead_structure()
    _dt0 = time.monotonic()
    _dead_end_runs += 1

    walls = _dead_obstacles
    base_dead = _dead_set
    base_oc = _dead_open_count
    extra_set = frozenset(extras)
    removed = {x for x in extra_set if x in base_dead}
    new_dead: set[tuple[int, int]] = set()
    overrides: dict[tuple[int, int], int] = {}
    fresh: set[tuple[int, int]] = set()
    work: deque = deque()

    def effective_dead(cell: tuple[int, int]) -> bool:
        return cell in new_dead or (cell in base_dead and cell not in removed)

    for x in extra_set:
        if x in walls:
            continue  # already a permanent wall — blocked in every count already
        was_dead = x in base_dead
        for n in _neighbor_cells(x):
            if n in walls or n in extra_set or effective_dead(n) or n in fresh:
                continue
            cnt = overrides[n] if n in overrides else base_oc.get(n)
            if cnt is None:
                cnt = sum(
                    1 for m in _neighbor_cells(n)
                    if m not in walls and m not in extra_set and not effective_dead(m)
                )
                overrides[n] = cnt
                fresh.add(n)
            elif not was_dead:
                cnt -= 1
                overrides[n] = cnt
            if n in overrides and overrides[n] <= 1:
                work.append(n)

    while work:
        cell = work.popleft()
        if cell in walls or cell in extra_set or effective_dead(cell):
            continue
        if overrides.get(cell, base_oc.get(cell, 0)) > 1:
            continue
        new_dead.add(cell)
        for n in _neighbor_cells(cell):
            if n in walls or n in extra_set or effective_dead(n):
                continue
            if n in overrides:
                overrides[n] -= 1
                cnt = overrides[n]
            elif n in base_oc:
                cnt = base_oc[n] - 1
                overrides[n] = cnt
            else:
                continue  # batch parity: cascade only propagates through candidates
            if cnt <= 1:
                work.append(n)

    _dead_end_ms += (time.monotonic() - _dt0) * 1000.0
    if not new_dead and not removed:
        return _dead_view
    return frozenset((base_dead - removed) | new_dead)


class _ObstacleOverlay(AbstractSet):
    """``base | extra`` obstacle test without copying the base wall set.

    The combat planners receive one obstacle collection and consult it in a
    hundred places (`in` checks, dead-end lookups, A* calls). Handing them
    ``obstacle_cells | {one or two transient cells}`` copied the ~488k-cell wall
    set per unit and — worse — broke the `_dead_obstacles` identity cache, so
    `_get_dead_ends` fell into the from-scratch batch (measured 4,176 ms and
    ~200 MB transient on the live map, see OPTIMIZATION_REVIEW.md #8).

    This read-only view answers membership exactly like that union. Every
    consumer of the obstacle argument either does `in` tests or goes through
    `_get_dead_ends` / `_path_blockers`, which unwrap the overlay back into the
    stable wall set + the existing transient-`extras` channel, so the dead-end
    result stays bit-identical to the folded union.
    """

    __slots__ = ("base", "extra")
    _from_iterable = frozenset  # set operators (`|`, `-`) return real frozensets

    def __init__(
        self,
        base: frozenset[tuple[int, int]],
        extra: frozenset[tuple[int, int]],
    ) -> None:
        self.base = base
        self.extra = extra

    def __contains__(self, cell: object) -> bool:
        return cell in self.base or cell in self.extra

    def __iter__(self):
        yield from self.base
        for cell in self.extra:
            if cell not in self.base:
                yield cell

    def __len__(self) -> int:
        return len(self.base) + sum(
            1 for cell in self.extra if cell not in self.base
        )

    def unwrap(
        self,
        extras: frozenset[tuple[int, int]],
    ) -> tuple[frozenset[tuple[int, int]], frozenset[tuple[int, int]]]:
        """Split into (stable obstacle set, extras ∪ this overlay)."""
        return self.base, extras | self.extra


def _get_dead_ends(
    obstacles: frozenset[tuple[int, int]] | set[tuple[int, int]] | _ObstacleOverlay,
    *,
    extras: frozenset[tuple[int, int]] = frozenset(),
) -> frozenset[tuple[int, int]]:
    """Cached dead-end set for the current known obstacle map.

    Wall fast path: the persistent structure covers the stable `_known_obstacles`
    object, so a call with that exact object is an O(1) identity hit. `extras`
    (transient occupied cells) derive dead(O ∪ X) on a scratch copy. Any other
    obstacle set (tests, one-off callers) falls through to the legacy
    identity/equality cache.
    """
    global _dead_end_cache_key, _dead_end_cache
    if isinstance(obstacles, _ObstacleOverlay):
        obstacles, extras = obstacles.unwrap(extras)
    if extras:
        return _dead_ends_with_extras(obstacles, extras)
    if obstacles is _dead_obstacles:
        _ensure_dead_structure()
        return _dead_view
    # Legacy identity/equality cache for arbitrary obstacle sets (tests, one-off
    # callers). Callers always pass a frozenset (and share one object across a
    # tick), so use it directly as the cache key instead of rebuilding
    # frozenset(obstacles) — an O(n) copy on every _is_dead_end_step call.
    # frozenset == short-circuits on length, so a growing obstacle set is an
    # O(1) miss during exploration.
    if obstacles is _dead_end_cache_key:
        return _dead_end_cache
    if isinstance(obstacles, frozenset) and obstacles == _dead_end_cache_key:
        return _dead_end_cache
    if not isinstance(obstacles, frozenset):
        obstacles = frozenset(obstacles)
    _dead_end_cache = _dead_end_cells(obstacles)
    _dead_end_cache_key = obstacles
    return _dead_end_cache


def _dead_component(
    start: tuple[int, int],
    dead: frozenset[tuple[int, int]] | set[tuple[int, int]],
) -> frozenset[tuple[int, int]]:
    """4-connected component of start inside the dead-end set."""
    if start not in dead:
        return frozenset()
    seen: set[tuple[int, int]] = {start}
    queue: deque[tuple[int, int]] = deque([start])
    while queue:
        cell = queue.popleft()
        for n in _neighbor_cells(cell):
            if n in dead and n not in seen:
                seen.add(n)
                queue.append(n)
    return frozenset(seen)


# Cached (obstacles | dead-end) union per obstacle map: rebuilding this
# thousands-of-cells frozenset on every _path_blockers call was the main
# replan cost. Same identity/equality discipline as _get_dead_ends.
_path_blockers_union_key: frozenset[tuple[int, int]] | None = None
_path_blockers_union: frozenset[tuple[int, int]] = frozenset()


class _BlockedView:
    """Lazy ``obstacles | dead | extras`` membership test for one A* call.

    Materialising that union costs ~72 ms per call on the production wall set
    (810k cells) and can never be cached while ``extras`` is non-empty — every
    worker/combat search carries transient occupied cells (see
    OPTIMIZATION_REVIEW.md #7). A* only ever asks "is this cell blocked?", so
    three set lookups replace the copy.

    ``allow`` carries cells that must stay enterable even though they are in the
    union: the dead-end pocket containing start/goal (formerly ``union - allowed``)
    and the goal itself (formerly ``blocked = blocked - {goal}``). Both were full
    union copies per call.
    """

    __slots__ = ("obstacles", "dead", "extras", "allow")

    def __init__(
        self,
        obstacles: frozenset[tuple[int, int]],
        dead: frozenset[tuple[int, int]] | set[tuple[int, int]],
        extras: frozenset[tuple[int, int]],
        allow: frozenset[tuple[int, int]] = frozenset(),
    ) -> None:
        self.obstacles = obstacles
        self.dead = dead
        self.extras = extras
        self.allow = allow

    def __contains__(self, cell: tuple[int, int]) -> bool:
        if cell in self.allow:
            return False
        return (
            cell in self.obstacles
            or cell in self.dead
            or cell in self.extras
        )

    def with_allowed(self, cell: tuple[int, int]) -> "_BlockedView":
        """Return a copy of this view that also lets ``cell`` be entered."""
        return _BlockedView(
            self.obstacles, self.dead, self.extras, self.allow | {cell},
        )


def _path_blockers(
    obstacles: frozenset[tuple[int, int]],
    *,
    start: tuple[int, int] | None = None,
    goal: tuple[int, int] | None = None,
    avoid_dead_ends: bool = True,
    extras: frozenset[tuple[int, int]] = frozenset(),
) -> frozenset[tuple[int, int]] | _BlockedView:
    """Obstacles plus dead ends, keeping corridors that contain start/goal.

    `extras` are transient occupied cells the caller treats as blocked this tick
    but which must not defeat the wall dead-end cache: dead-ends are computed
    from `obstacles` (cached) and `extras` are unioned in as plain blockers.

    Returns the cached frozenset union when nothing has to be subtracted (the
    common no-`extras` shape), otherwise a `_BlockedView` that answers the same
    membership questions without copying the wall set.
    """
    if isinstance(obstacles, _ObstacleOverlay):
        obstacles, extras = obstacles.unwrap(extras)
    empty = frozenset()
    if not avoid_dead_ends:
        return _BlockedView(obstacles, empty, extras)
    dead = _get_dead_ends(obstacles, extras=extras)
    if not dead:
        return _BlockedView(obstacles, empty, extras)
    allowed: set[tuple[int, int]] = set()
    if start is not None and start in dead:
        allowed |= _dead_component(start, dead)
    if goal is not None and goal in dead:
        allowed |= _dead_component(goal, dead)
    if extras or allowed:
        # Never materialise the union for a transient search (report #7): every
        # planner call carries occupied cells, so this is the production shape.
        return _BlockedView(obstacles, dead, extras, frozenset(allowed))
    # Purely wall-based search: return the cached identity-stable union so
    # repeated calls share one frozenset (the pre-existing cache).
    global _path_blockers_union_key, _path_blockers_union
    if obstacles is _path_blockers_union_key \
            or obstacles == _path_blockers_union_key:
        return _path_blockers_union
    union = obstacles | dead
    _path_blockers_union_key = obstacles
    _path_blockers_union = union
    return union


def _is_dead_end_step(
    pos: tuple[int, int],
    obstacles: frozenset[tuple[int, int]],
    *,
    allow: Iterable[tuple[int, int]] = (),
) -> bool:
    """True if stepping onto pos would enter a recognized dead end."""
    if pos in allow:
        return False
    return pos in _get_dead_ends(obstacles)


# ── Pathfinding (A* multi-step lookahead; kept as _bfs_path for callers) ──────

def _bfs_path(
    start: tuple[int, int],
    goal: tuple[int, int],
    obstacles: frozenset[tuple[int, int]],
    max_steps: int = 2500,
    *,
    avoid_dead_ends: bool = True,
    extras: frozenset[tuple[int, int]] = frozenset(),
) -> list[tuple[int, int]] | None:
    """Return a short grid path including start and goal, or None.

    Uses A* (Manhattan heuristic) so long routes across open maps need far
    fewer expansions than plain BFS. max_steps still caps node expansions.

    When avoid_dead_ends is True, 凸-shaped cul-de-sacs and one-wide corridors
    that only lead into them are treated as blocked, unless start or goal lies
    inside such a pocket (so units can still exit or reach a resource there).

    `extras` are transient occupied cells (other units / enemies) treated as
    blocked this tick but excluded from the wall dead-end cache key; see
    _path_blockers.
    """
    if start == goal:
        return [start]
    global _pathfind_calls, _pathfind_expansions, _pathfind_ms
    _pathfind_calls += 1
    _pf_t0 = time.monotonic()
    blocked = _path_blockers(
        obstacles,
        start=start,
        goal=goal,
        avoid_dead_ends=avoid_dead_ends,
        extras=extras,
    )
    # Goal itself must remain enterable even if classified as a dead end, but
    # NOT if it is an obstacle or occupied-by-other this tick (extras). This used
    # to be `blocked = blocked - {goal}` — another full union copy per call; the
    # view carries the exception as a membership test instead.
    if goal in blocked and goal not in obstacles and goal not in extras:
        if isinstance(blocked, _BlockedView):
            blocked = blocked.with_allowed(goal)
        else:
            blocked = blocked - {goal}

    # A*: f = g + h. tie-break on h so we bias toward the goal.
    # Heap entries: (f, h, counter, g, cell). counter breaks remaining ties.
    open_heap: list[tuple[int, int, int, int, tuple[int, int]]] = []
    g_score: dict[tuple[int, int], int] = {start: 0}
    parents: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
    heappush(open_heap, (_manhattan(start, goal), _manhattan(start, goal), 0, 0, start))
    expansions = 0
    counter = 0
    deadline = _plan_pathfind_deadline
    while open_heap and expansions < max_steps:
        if deadline is not None and (expansions & 2047) == 0 \
                and time.monotonic() > deadline:
            break
        _f, _h, _c, g_current, current = heappop(open_heap)
        if g_current > g_score.get(current, 1 << 30):
            continue  # stale heap entry
        if current == goal:
            path = [goal]
            cursor = parents[goal]
            while cursor is not None:
                path.append(cursor)
                cursor = parents[cursor]
            _pathfind_expansions += expansions
            # monotonic() is in seconds; convert to ms (see the cap-hit return).
            _pathfind_ms += (time.monotonic() - _pf_t0) * 1000.0
            return list(reversed(path))
        expansions += 1
        x, y = current
        for d in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT):
            next_pos = (x + d.delta[0], y + d.delta[1])
            if next_pos in blocked:
                continue
            tentative = g_current + 1
            if tentative >= g_score.get(next_pos, 1 << 30):
                continue
            parents[next_pos] = current
            g_score[next_pos] = tentative
            h = _manhattan(next_pos, goal)
            counter += 1
            heappush(open_heap, (tentative + h, h, counter, tentative, next_pos))
    # Hit the max_steps cap or exhausted the open set: still account the work.
    _pathfind_expansions += expansions
    # time.monotonic() returns SECONDS; convert to ms so pathfind_ms is in the
    # same unit as latency_ms (a missing *1000 here made A* look ~1000x cheaper
    # than it really was — see analyze_latency.py / diag_remote6.py).
    _pathfind_ms += (time.monotonic() - _pf_t0) * 1000.0
    return None


def _direction_for_step(
    start: tuple[int, int],
    next_pos: tuple[int, int],
) -> Direction | None:
    delta = next_pos[0] - start[0], next_pos[1] - start[1]
    for direction in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT):
        if direction.delta == delta:
            return direction
    return None


def _path_index(
    path: list[tuple[int, int]],
    pos: tuple[int, int],
    hint: int = -1,
) -> int:
    """Index of pos on path, trusting the cached cursor when it still matches.

    Units advance one cell per Tick, so the cursor almost always hits and the
    O(len(path)) list scan is skipped; a mismatch falls back to the scan.
    """
    if 0 <= hint < len(path) and path[hint] == pos:
        return hint
    try:
        return path.index(pos)
    except ValueError:
        return -1


_object_names: dict[tuple[str, str], str] = {}
_object_name_counters: defaultdict[str, int] = defaultdict(int)
# Recency order of the enemy ("E") half of _object_names, least recently seen
# first. Enemy ids churn (every spawn is a fresh UUID), so this table used to
# grow for the whole process lifetime — days on the live server — and the
# per-Tick prune materialised list(_object_names.items()) over it (report #15).
# C/W/V/R names are never evicted: Core and unit names must stay stable, and
# live-unit entries are pruned by life cycle in _prune_dead_unit_bookkeeping.
_OBJECT_NAME_ENEMY_LIMIT = 4096
_object_name_enemy_order: OrderedDict[tuple[str, str], None] = OrderedDict()


def _remember_enemy_name(key: tuple[str, str]) -> None:
    """Mark an enemy name most-recently-seen; evict the oldest past the cap."""
    order = _object_name_enemy_order
    order[key] = None
    order.move_to_end(key)
    while len(order) > _OBJECT_NAME_ENEMY_LIMIT:
        oldest, _ = order.popitem(last=False)
        _object_names.pop(oldest, None)


def _object_name(object_id: Any, prefix: str) -> str:
    key = prefix, str(object_id)
    name = _object_names.get(key)
    if name is not None:
        if prefix == "E":
            # Touch: an enemy that is still visible (named every Tick) must never
            # be the least-recently-seen entry, so its name cannot drift.
            _object_name_enemy_order[key] = None
            _object_name_enemy_order.move_to_end(key)
        return name
    _object_name_counters[prefix] += 1
    name = f"{prefix}{_object_name_counters[prefix]}"
    _object_names[key] = name
    if prefix == "E":
        _remember_enemy_name(key)
    return name


def _set_unit_route(
    unit: Any,
    target: tuple[int, int],
    path: list[tuple[int, int]],
    *,
    complete: bool,
) -> None:
    """Record a planned route for any unit (worker, vanguard, ranger, core)."""
    turn_context.unit_routes[str(unit.id)[:8]] = {
        "target": target,
        "path": path,
        "complete": complete,
    }


def _set_worker_route(
    worker: Any,
    target: tuple[int, int],
    path: list[tuple[int, int]],
    *,
    complete: bool,
) -> None:
    _set_unit_route(worker, target, path, complete=complete)
    # Backward compat: keep worker_routes populated
    turn_context.worker_routes[str(worker.id)[:8]] = {
        "target": target,
        "path": path,
        "complete": complete,
    }


# ── decision logger ──────────────────────────────────────────────────────────

@dataclass
class TickRecord:
    tick: int
    timestamp: str = ""
    core_name: str = "C1"
    core_pos: list[int] | None = None
    core_hp: int = 0
    core_shield: int = 0
    core_state: str = ""
    core_action: str = ""
    resources: int = 0
    resource_capacity: int = 0
    population: int = 0
    workers: list[dict] = field(default_factory=list)
    vanguards: list[dict] = field(default_factory=list)
    rangers: list[dict] = field(default_factory=list)
    enemies: list[dict] = field(default_factory=list)
    visible_enemies: int = 0
    resource_cells_visible: int = 0
    resource_cells: list[list[int]] = field(default_factory=list)
    obstacle_cells_visible: int = 0
    obstacle_memory_count: int = 0
    resource_memory_count: int = 0
    beacon_pos: list[int] | None = None
    beacon_status: str | None = None
    events: list[dict] = field(default_factory=list)
    # Resolution events are produced while resolving the *previous* Tick. The
    # record-level value is set when every event agrees; each event also carries
    # its own `resolved_tick` (v4) for mixed/older producers.
    events_tick: int | None = None
    core_move: dict | None = None
    shot_predictions: list[dict] = field(default_factory=list)
    shot_prediction_results: list[dict] = field(default_factory=list)
    kite_decisions: list[dict] = field(default_factory=list)
    guerrilla_decisions: list[dict] = field(default_factory=list)
    # Auto-mode target candidate ranking (top 3) for attack/kite squads.
    auto_target_candidates: list[dict] = field(default_factory=list)
    kite_target_candidates: list[dict] = field(default_factory=list)
    # Squad/defence verdict inputs and fuse counters that drove this Tick.
    decisions: dict = field(default_factory=dict)
    config_rev: str = ""
    config_changed: list[str] = field(default_factory=list)
    config_delta: dict = field(default_factory=dict)
    plan_unit_actions: dict[str, str] = field(default_factory=dict)
    plan_core_action: str | None = None
    accepted: bool = False
    latency_ms: float = 0.0
    # Per-Tick planning profiler: name → wall-clock ms for each choose_actions
    # phase. Absent on old log segments (added for the latency investigation).
    phase_ms: dict[str, float] = field(default_factory=dict)
    pathfind_calls: int = 0
    pathfind_expansions: int = 0
    pathfind_ms: float = 0.0
    dead_end_runs: int = 0
    dead_end_ms: float = 0.0


_EVENT_VALUE_KEYS = frozenset({
    "damage", "hp", "targets_hit", "amount", "unit_type", "cost",
    "required", "source", "direction", "progress", "required_ticks",
    "destination", "shield", "resource_amount", "target_id",
})
_EVENT_VALUES_MAX_BYTES = 512


def _event_values(event: Any) -> dict | None:
    """Keep a compact, size-capped copy of a ResolutionEvent's values.

    The wire carries damage/hp/targets_hit and a few economy fields inside
    ``values``; the old Tick log dropped the whole dict, which made combat
    attribution impossible without the Chinese battle-log text. Unknown keys
    are preserved while they fit the cap so a new protocol field is not lost
    silently; oversized payloads fall back to the known combat/economy keys.
    """
    values = getattr(event, "values", None)
    if not isinstance(values, Mapping) or not values:
        return None
    candidate = {str(key): value for key, value in values.items()}
    encoded = json.dumps(candidate, ensure_ascii=False, default=str)
    if len(encoded) <= _EVENT_VALUES_MAX_BYTES:
        return json.loads(encoded)
    trimmed = {key: value for key, value in candidate.items() if key in _EVENT_VALUE_KEYS}
    encoded = json.dumps(trimmed, ensure_ascii=False, default=str)
    if len(encoded) <= _EVENT_VALUES_MAX_BYTES:
        return json.loads(encoded)
    return {"_truncated": True, "_keys": sorted(candidate)[:12]}


def _direction_name(direction: Any) -> str | None:
    """Direction enum/string → the four-letter name used elsewhere in the log."""
    if direction is None:
        return None
    return str(getattr(direction, "name", direction))


def _core_move_fields(view: Any) -> dict:
    """Movement fields available on CoreView (own or enemy), Nones omitted."""
    fields = {
        "direction": _direction_name(getattr(view, "move_direction", None)),
        "progress": getattr(view, "move_progress", None),
        "required_ticks": getattr(view, "move_required_ticks", None),
        "destination": (
            list(view.destination)
            if getattr(view, "destination", None) is not None
            else None
        ),
    }
    return {key: value for key, value in fields.items() if value is not None}


def _sweep_fuse_snapshot() -> dict:
    """Active sweep-fuse debts, keyed by the 8-char id used across the log."""
    tick = int(getattr(turn_context, "tick", 0) or 0)
    out: dict[str, dict] = {}
    for uid, until in _sweep_fuse_until.items():
        if until > tick:
            out[str(uid)[:8]] = {"remaining": until - tick}
    for uid, streak in _sweep_miss_streak.items():
        if streak <= 0:
            continue
        key = str(uid)[:8]
        out.setdefault(key, {})["miss_streak"] = streak
    return out


def _waypoint_stuck_snapshot() -> dict:
    """Units currently burning their waypoint retry budget."""
    out: dict[str, dict] = {}
    for uid, value in _waypoint_stuck.items():
        try:
            ticks, target = value
        except (TypeError, ValueError):
            continue
        if ticks > 0:
            out[str(uid)[:8]] = {"ticks": ticks, "target": list(target)}
    return out


def _config_revision(config: Mapping) -> tuple[str, list[str]]:
    """Stable revision + changed keys since the previous Tick.

    Replays need to know which config a row was planned under. The revision is
    a short hash of the whole config; the changed-key list makes a hot change
    visible without diffing two full snapshots.
    """
    payload = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    revision = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    previous = getattr(turn_context, "config_values", None)
    changed: list[str] = []
    if isinstance(previous, Mapping):
        changed = [
            key for key in sorted(set(previous) | set(config))
            if previous.get(key) != config.get(key)
        ]
    turn_context.config_values = dict(config)
    return revision, changed


def _rows_for_tick(rows: Iterable[Any], tick: int) -> list:
    """Drop planner rows left over from an earlier Tick.

    Decision stores live on the process-global ``turn_context``; a test or a
    crashed planning pass can leave a previous Tick's rows behind. Logging them
    under the current Tick would silently mis-attribute a decision, so rows that
    carry a different ``tick`` are discarded. Rows without a tick key are kept
    for backward compatibility with older producers.
    """
    out = []
    for row in rows or ():
        if isinstance(row, Mapping) and row.get("tick") not in (None, tick):
            continue
        out.append(row)
    return out


class TacticLogger:
    """Logs every Tick decision to a JSONL file for later review."""

    def __init__(self, path: str = "tactic_log.jsonl") -> None:
        self.path = path
        self._file: Any = None
        self._tick_count = 0
        self._start_time = time.monotonic()
        self._last_tick_time = 0.0
        # uid -> hash of the route polyline written for that unit last Tick.
        # Cached A* routes are re-emitted unchanged for every Tick a unit walks
        # them, and the worker polylines were 87% of a 53 KB record; an
        # unchanged route is logged as ``path: null`` + ``path_len`` and the
        # dashboard recovers the coordinates from an earlier record.
        self._last_path_sig: dict[str, int] = {}

    def _route_fields(self, uid: str, route: Mapping, *, seen: set[str]) -> dict:
        """Path fields for one unit, omitting a route unchanged since last Tick."""
        path = route.get("path") or []
        sig = hash(tuple(path))
        seen.add(uid)
        if self._last_path_sig.get(uid) == sig:
            return {"path": None, "path_len": len(path)}
        self._last_path_sig[uid] = sig
        return {"path": [list(p) for p in path], "path_len": len(path)}

    def open(self) -> None:
        self._file = open(self.path, "a", encoding="utf-8")
        self._write_header()

    def _write_header(self) -> None:
        header = {
            "_meta": "arena-hero-tactic-log",
            "_started_at": datetime.now(timezone.utc).isoformat(),
            "_version": 4,
        }
        self._file.write(json.dumps(header, ensure_ascii=False) + "\n")
        self._file.flush()

    def _maybe_rotate(self) -> None:
        """Rotate the log by size so it never grows without bound.

        Readers (dashboard/status/summary) open the current path fresh on every
        call, so after a rotate they transparently read the newest segment.
        Rotation must never crash a Tick: any failure is swallowed and a writable
        handle to the current file is restored.
        """
        try:
            if not self._file or self._file.closed:
                return
            if os.fstat(self._file.fileno()).st_size < LOG_MAX_BYTES:
                return
            # Close before renaming — an open handle blocks rename on Windows.
            self._file.flush()
            self._file.close()
            log_path = Path(self.path)
            # Shift backups newest -> oldest; unlink the destination first
            # (Path.rename fails if the destination already exists on Windows).
            for i in range(LOG_BACKUP_COUNT - 1, 0, -1):
                src = log_path.with_name(f"{log_path.name}.{i}")
                dst = log_path.with_name(f"{log_path.name}.{i + 1}")
                if dst.exists():
                    dst.unlink()
                if src.exists():
                    src.rename(dst)
            backup = log_path.with_name(f"{log_path.name}.1")
            if log_path.exists():
                if backup.exists():
                    backup.unlink()
                log_path.rename(backup)
            self._file = open(self.path, "a", encoding="utf-8")
            # A reader can no longer look across the rotation boundary for the
            # route coordinates we omitted, so re-emit every full path once.
            self._last_path_sig.clear()
            self._write_header()
            print(f"[log] rotated {self.path} -> {backup.name}", flush=True)
        except Exception as exc:
            if not self._file or self._file.closed:
                try:
                    self._file = open(self.path, "a", encoding="utf-8")
                except Exception:
                    self._file = None
            print(f"[log] rotation failed: {exc}", flush=True)

    def close(self) -> None:
        if self._file and not self._file.closed:
            elapsed = time.monotonic() - self._start_time
            summary = {
                "_summary": True,
                "_ticks": self._tick_count,
                "_elapsed_seconds": round(elapsed, 1),
                "_ticks_per_minute": round(self._tick_count / (elapsed / 60), 1) if elapsed > 0 else 0,
            }
            self._file.write(json.dumps(summary, ensure_ascii=False) + "\n")
            self._file.close()

    def record_tick(
        self,
        turn: Any,
        *,
        core_action: str = "",
        unit_actions: dict[str, str] | None = None,
        accepted: bool = False,
        latency_ms: float = 0.0,
        phase_ms: dict[str, float] | None = None,
        pathfind_calls: int = 0,
        pathfind_expansions: int = 0,
        pathfind_ms: float = 0.0,
        dead_end_runs: int = 0,
        dead_end_ms: float = 0.0,
    ) -> TickRecord:
        """Record a single Tick's state and decisions."""
        now = time.monotonic()
        state = turn.state
        core = turn.core

        rec = TickRecord(tick=turn.tick)
        rec.timestamp = datetime.now(timezone.utc).isoformat()
        rec.resources = turn.resources
        rec.resource_capacity = turn.resource_capacity
        rec.population = state.population
        rec.visible_enemies = len(turn.visible_enemies)
        rec.resource_cells_visible = len(turn.resource_cells)
        rec.resource_cells = [list(p) for p in turn.resource_cells]
        rec.obstacle_cells_visible = len(getattr(turn, "obstacle_cells", ()) or ())
        rec.obstacle_memory_count = len(_obstacle_memory)
        rec.resource_memory_count = len(_resource_memory)
        rec.beacon_pos = list(turn.beacon.position) if turn.beacon.position else None
        rec.beacon_status = turn.beacon.status.name if turn.beacon.status else None
        rec.core_action = core_action
        rec.plan_unit_actions = unit_actions or {}
        rec.shot_predictions = [
            {key: value for key, value in item.items() if not key.startswith("_")}
            for item in turn_context.shot_predictions
        ]
        rec.shot_prediction_results = list(turn_context.shot_prediction_results)
        rec.kite_decisions = _rows_for_tick(
            getattr(turn_context, "kite_decisions", []) or [], turn.tick,
        )
        rec.guerrilla_decisions = _rows_for_tick(
            getattr(turn_context, "guerrilla_decisions", []) or [], turn.tick,
        )
        rec.auto_target_candidates = _rows_for_tick(
            getattr(turn_context, "attack_target_candidates", []) or [], turn.tick,
        )
        rec.kite_target_candidates = _rows_for_tick(
            getattr(turn_context, "kite_target_candidates", []) or [], turn.tick,
        )
        decisions: dict[str, Any] = {}
        attack_decision = getattr(turn_context, "attack_decision", None)
        if isinstance(attack_decision, Mapping):
            if attack_decision.get("tick") in (None, turn.tick):
                decisions["attack"] = dict(attack_decision)
        home_defense = getattr(turn_context, "home_defense_decisions", None)
        if isinstance(home_defense, Mapping):
            home_rows = [
                row for row in home_defense.values()
                if isinstance(row, Mapping) and row.get("tick") in (None, turn.tick)
            ]
            if home_rows:
                decisions["home_defense"] = home_rows
        home_leash = getattr(turn_context, "home_defense_leash", None)
        if home_leash is not None:
            decisions["home_defense_leash"] = int(home_leash)
        sweep_fuse = _sweep_fuse_snapshot()
        if sweep_fuse:
            decisions["sweep_fuse"] = sweep_fuse
        waypoint_stuck = _waypoint_stuck_snapshot()
        if waypoint_stuck:
            decisions["waypoint_stuck"] = waypoint_stuck
        rec.decisions = decisions
        rec.config_rev = str(getattr(turn_context, "config_rev", "") or "")
        rec.config_changed = list(
            getattr(turn_context, "config_changed", []) or []
        )
        rec.config_delta = dict(getattr(turn_context, "config_delta", {}) or {})
        rec.accepted = accepted
        rec.latency_ms = round(latency_ms, 1)
        if phase_ms:
            rec.phase_ms = phase_ms
        rec.pathfind_calls = pathfind_calls
        rec.pathfind_expansions = pathfind_expansions
        rec.pathfind_ms = round(pathfind_ms, 1)
        rec.dead_end_runs = dead_end_runs
        rec.dead_end_ms = round(dead_end_ms, 1)

        if core:
            rec.core_name = _object_name(core.id, "C")
            rec.core_pos = list(core.position)
            rec.core_hp = core.hp
            rec.core_shield = core.shield
            rec.core_state = core.view.state.value if hasattr(core.view.state, "value") else str(core.view.state)
            move_fields = _core_move_fields(core.view)
            if move_fields:
                rec.core_move = move_fields

        logged_uids: set[str] = set()
        for w in turn.workers:
            wid = str(w.id)[:8]
            route = turn_context.worker_routes.get(wid, {})
            rec.workers.append({
                "id": wid,
                "name": _object_name(w.id, "W"),
                "pos": list(w.position),
                "target": list(route["target"]) if route.get("target") else None,
                **self._route_fields(wid, route, seen=logged_uids),
                "path_complete": bool(route.get("complete", False)),
                "cargo": w.cargo,
                "hp": w.hp,
            })

        for v in turn.vanguards:
            vid = str(v.id)[:8]
            v_route = turn_context.unit_routes.get(vid, {})
            rec.vanguards.append({
                "id": vid,
                "name": _object_name(v.id, "V"),
                "pos": list(v.position),
                "target": list(v_route["target"]) if v_route.get("target") else None,
                **self._route_fields(vid, v_route, seen=logged_uids),
                "path_complete": bool(v_route.get("complete", False)),
                "hp": v.hp,
            })

        for r in turn.rangers:
            rid = str(r.id)[:8]
            r_route = turn_context.unit_routes.get(rid, {})
            rec.rangers.append({
                "id": rid,
                "name": _object_name(r.id, "R"),
                "pos": list(r.position),
                "target": list(r_route["target"]) if r_route.get("target") else None,
                **self._route_fields(rid, r_route, seen=logged_uids),
                "path_complete": bool(r_route.get("complete", False)),
                "hp": r.hp,
            })
        # Drop signatures of units that are gone so the table cannot grow with
        # the respawn churn of a multi-day run.
        if len(self._last_path_sig) > 256:
            self._last_path_sig = {
                uid: sig for uid, sig in self._last_path_sig.items() if uid in logged_uids
            }

        for enemy in turn.visible_enemies:
            enemy_type = _enemy_unit_type_name(enemy) or "ENEMY"
            entry = {
                "id": str(enemy.id)[:8],
                "name": _object_name(enemy.id, "E"),
                "pos": list(enemy.position),
                "hp": getattr(enemy, "hp", None),
                "type": enemy_type,
            }
            if enemy_type == "CORE":
                # Enemy CoreView carries the same movement/defence fields as
                # our own core; the old log kept only position/HP.
                entry["shield"] = getattr(enemy, "shield", None)
                state = getattr(enemy, "state", None)
                entry["state"] = (
                    state.value if hasattr(state, "value") else str(state)
                ) if state is not None else None
                core_move = _core_move_fields(enemy)
                if core_move:
                    entry["move"] = core_move
            elif enemy_type == "WORKER":
                cargo = getattr(enemy, "cargo", None)
                if cargo is not None:
                    entry["cargo"] = cargo
            rec.enemies.append(entry)

        event_ticks: set[int] = set()
        for event in turn.events:
            actor_id = getattr(event, "actor_id", None)
            target_id = getattr(event, "target_id", None)
            target_name = _battle_actor_name(turn, target_id) if target_id else None
            resolved_tick = getattr(event, "tick", None)
            try:
                event_tick = int(resolved_tick) if resolved_tick is not None else 0
            except (TypeError, ValueError):
                event_tick = 0
            if event_tick > 0:
                event_ticks.add(event_tick)
            actor_name = _battle_actor_name(turn, actor_id) if actor_id else None
            event_row = {
                "type": event.event_type,
                "reason": event.reason_code,
                "actor": str(actor_id)[:8] if actor_id else None,
                "pos": list(event.position) if event.position else None,
                "amount": getattr(event, "resource_amount", None),
            }
            # Omit empty/null columns: movement events dominate the log (~45 per
            # Tick) and doubling their width would cost more than the new detail.
            if actor_name and actor_name != "—":
                event_row["actor_name"] = actor_name
            if target_id:
                event_row["target"] = str(target_id)[:8]
            if target_name and target_name != "—":
                event_row["target_name"] = target_name
            values = _event_values(event)
            if values:
                event_row["values"] = values
            if event_tick:
                event_row["resolved_tick"] = event_tick
            rec.events.append(event_row)
        rec.events_tick = next(iter(event_ticks)) if len(event_ticks) == 1 else None

        self._maybe_rotate()
        if self._file and not self._file.closed:
            self._file.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
            self._file.flush()

        self._tick_count += 1
        self._last_tick_time = now
        return rec


# ── guerrilla route recorder ─────────────────────────────────────────────────

_guerrilla_route_state = {"date": "", "paused": False}


def _guerrilla_route_path(date: str) -> Path:
    return data_path(f"{GUERRILLA_ROUTE_PREFIX}{date}.jsonl")


def _prune_guerrilla_route_logs(today: str) -> None:
    """Delete route files older than GUERRILLA_ROUTE_KEEP_DAYS local days."""
    try:
        today_date = datetime.strptime(today, "%Y%m%d").date()
    except ValueError:
        return
    for path in data_dir().glob(f"{GUERRILLA_ROUTE_PREFIX}*.jsonl"):
        stamp = path.name[len(GUERRILLA_ROUTE_PREFIX):-len(".jsonl")]
        try:
            logged_date = datetime.strptime(stamp, "%Y%m%d").date()
        except ValueError:
            continue
        if (today_date - logged_date).days >= GUERRILLA_ROUTE_KEEP_DAYS:
            try:
                path.unlink()
                print(f"[route-log] pruned {path.name}", flush=True)
            except OSError:
                pass


def _guerrilla_route_rows(
    turn: Any,
    unit_actions: dict[str, str],
) -> list[dict[str, Any]]:
    """One row per unit the planner tagged [guerrilla] this Tick.

    The tag comes from ``unit_actions_detail`` (the same string the Tick log
    keeps), so the roster itself is never re-resolved here. The short id rides
    along with the display name because names get recycled when a unit dies —
    without it two successive units share one polyline.
    """
    by_uid: dict[str, tuple[Any, str]] = {}
    for prefix, group in (("V", turn.vanguards), ("R", turn.rangers)):
        for unit in group:
            by_uid[str(unit.id)[:8]] = (unit, prefix)
    now_epoch = int(time.time())
    rows: list[dict[str, Any]] = []
    for uid, detail in unit_actions.items():
        if "[guerrilla]" not in str(detail):
            continue
        found = by_uid.get(uid)
        if found is None:
            continue
        unit, prefix = found
        rows.append({
            "t": turn.tick,
            "e": now_epoch,
            "u": _object_name(unit.id, prefix),
            "i": uid,
            "p": [int(unit.position[0]), int(unit.position[1])],
        })
    return rows


def _record_guerrilla_routes(turn: Any, unit_actions: dict[str, str]) -> None:
    """Append this Tick's guerrilla positions to the current day's route file.

    Never allowed to cost a Tick: every failure is swallowed the way the Tick
    logger's rotation is. The file is append-only and never trimmed — the whole
    point is that a day of routes survives intact for later review, unlike the
    size-rotated tactic_log.jsonl.
    """
    try:
        if not load_config().get("guerrilla_route_log", True):
            return
        today = time.strftime("%Y%m%d")
        if today != _guerrilla_route_state["date"]:
            _guerrilla_route_state["date"] = today
            _guerrilla_route_state["paused"] = False
            _prune_guerrilla_route_logs(today)
        if _guerrilla_route_state["paused"]:
            return
        rows = _guerrilla_route_rows(turn, unit_actions)
        if not rows:
            return
        path = _guerrilla_route_path(today)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="") as f:
            f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        if path.stat().st_size > GUERRILLA_ROUTE_MAX_DAY_BYTES:
            _guerrilla_route_state["paused"] = True
            print(
                f"[route-log] {path.name} over "
                f"{GUERRILLA_ROUTE_MAX_DAY_BYTES} bytes, recording paused",
                flush=True,
            )
    except Exception as exc:
        print(f"[route-log] write failed: {exc}", flush=True)


# ── unit planners ────────────────────────────────────────────────────────────

def _retreat_from(
    pos: tuple[int, int],
    core_pos: tuple[int, int],
    obstacle_cells: frozenset[tuple[int, int]],
    others: frozenset[tuple[int, int]],
    cell_counts: Mapping | None = None,
    *,
    force: bool = False,
) -> Direction | None:
    """Pick a direction that moves away from core_pos, if any is free.

    Used to back a full worker out of the core's immediate ring so the core cell
    can free up for unloading. Prefers the direction that increases distance to
    the core the most; skips obstacles, blocked cells and dead ends.

    Capacity awareness: the server allows _CELL_UNIT_LIMIT (2) stacked units on
    one cell. When ``cell_counts`` (this tick's friendly unit count per cell)
    is given, a neighbour is enterable while its count stays below the limit —
    ``others`` then lists only hard-blocked cells (enemies). Without counts the
    legacy "any occupied cell is fully blocked" view applies. Regression: the
    fully-blocked view let a healed healer sit forever on the core cell of a
    narrow-mouth base because its only squeeze exit was "occupied" by a single
    carrier, sealing the unloading chute (observed 95+ ticks).

    ``force`` skips dead-end avoidance: the last-resort evacuation of a unit
    squatting on the chute must not fail on style grounds.
    """
    best: Direction | None = None
    best_dist = -1
    for d in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT):
        npos = (pos[0] + d.delta[0], pos[1] + d.delta[1])
        if npos in obstacle_cells:
            continue
        if cell_counts is not None:
            if npos in others:
                continue
            if cell_counts.get(npos, 0) >= _CELL_UNIT_LIMIT:
                continue
        elif npos in others:
            continue
        if not force and _is_dead_end_step(npos, obstacle_cells):
            continue
        dist = _manhattan(npos, core_pos)
        if dist > best_dist:
            best_dist = dist
            best = d
    return best


def _capacity_blocked_cells(
    occupied: frozenset[tuple[int, int]],
    enemy_cells: frozenset[tuple[int, int]],
    cell_counts: Mapping | None,
    core_pos: tuple[int, int] | None = None,
) -> frozenset[tuple[int, int]]:
    """Occupied cells that hard-block a move this tick, capacity aware.

    The server stacks up to _CELL_UNIT_LIMIT units on one cell, so a cell with
    a single friendly occupant is enterable ("squeeze") — two workers meeting
    head-on in a narrow lane pass each other instead of WAITing forever. Enemy
    cells, cells already at the stacking limit and the core cell (capacity is
    Core + one unit: the single unloading chute) stay hard-blocked. Without
    ``cell_counts`` the legacy fully-blocked view is returned.
    """
    if cell_counts is None:
        return occupied
    return frozenset(
        cell
        for cell in occupied
        if cell in enemy_cells
        or (core_pos is not None and cell == core_pos)
        or cell_counts.get(cell, 0) >= _CELL_UNIT_LIMIT
    )


def _worker_cached_path_step(
    worker,
    uid: str,
    pos: tuple[int, int],
    goal: tuple[int, int],
    obstacle_cells: frozenset[tuple[int, int]],
    others: frozenset[tuple[int, int]] = frozenset(),
) -> tuple[str, str] | None:
    """Advance one step along a cached A* path, or None to force a replan.

    Obstacles only ever grow (never shrink), so a cached path can never become
    shorter or suboptimal — it can only become blocked ahead. Local next-cell
    validation (traversable + not a dead end + not occupied) is therefore enough
    to keep reusing it across ticks; comparing whole obstacle sets would change
    every tick during exploration and defeat the cache.
    """
    cached = _worker_path_cache.get(uid)
    if cached is None or cached["goal"] != goal:
        return None
    path = cached["path"]
    if pos == goal:
        _worker_path_cache.pop(uid, None)
        return None
    k = _path_index(path, pos, cached.get("index", -1))
    if k < 0:
        # Unit is not on the cached path (blocked or diverted last tick).
        return None
    if k + 1 >= len(path):
        _worker_path_cache.pop(uid, None)
        return None
    next_cell = path[k + 1]
    # The goal cell may be stepped on only while it is actually free — except
    # during a chute hand-off: when the occupant vacates this tick the entry
    # chains behind its departure ("one out, one in"). Any other occupied
    # cell invalidates the cached step (server rejects the move).
    if next_cell in others and not (
        next_cell == goal and _chute_vacating_this_tick
    ):
        return None
    valid = next_cell == goal or (
        next_cell not in obstacle_cells
        and not _is_dead_end_step(next_cell, obstacle_cells, allow=(goal,))
    )
    if not valid:
        return None
    bfs_dir = _direction_for_step(pos, next_cell)
    if bfs_dir is None:
        return None
    worker.move(bfs_dir)
    _worker_last_pos[uid] = pos
    recent = _worker_recent.get(uid, [])
    recent.append(pos)
    if len(recent) > 6:
        recent.pop(0)
    _worker_recent[uid] = recent
    cached["index"] = k + 1
    _set_worker_route(worker, tuple(goal), path, complete=True)
    return ("MOVE", f"{bfs_dir.name} -> {goal}")


def _cargo_home_budget(
    uid: str,
    pos: tuple[int, int],
    core_pos: tuple[int, int],
    min_budget: int,
) -> int | None:
    """A* expansion budget for a loaded worker's route home, or None to skip the search.

    The flat ``bfs_max_steps`` (2500) is exhausted long before the Core on any
    real return trip, and the greedy fallback that then takes over laps an
    8-cell box beside a wall band instead of closing distance (observed: 7
    carriers 270..1083 cells out, each burning exactly the 2500-expansion cap
    every Tick for an hour; 6 deposits in 343 Ticks, storage 44 -> 2). A* fills
    the equal-f plateau around concave wall clusters, so the budget scales with
    the distance the way the attack-regroup march does — 29..32x was what the
    real map memory actually needed.

    A far miss is remembered at the distance it happened: wall memory only ever
    grows, so re-searching is only worth its cost once the carrier has closed
    ``_CARGO_HOME_RETRY_GAP`` cells since. Near home the suppression is off,
    because there a miss is usually transient congestion (the chute ring is
    packed) rather than a wall, and freezing the route would strand the cargo.
    """
    dist = _manhattan(pos, core_pos)
    failed_at = _cargo_route_miss_dist.get(uid)
    if (
        dist > _CARGO_HOME_MISS_FAR
        and failed_at is not None
        and failed_at - dist < _CARGO_HOME_RETRY_GAP
    ):
        return None
    return min(_CARGO_HOME_BUDGET_MAX, max(min_budget, dist * _CARGO_HOME_BUDGET_FACTOR))


def _enemy_unit_type_name(enemy: Any) -> str | None:
    """Return WORKER/VANGUARD/RANGER/CORE/None for a visible enemy object."""
    kind = getattr(enemy, "kind", None)
    if kind is not None:
        value = kind.value if hasattr(kind, "value") else str(kind)
        upper = value.upper()
        if upper == "CORE":
            return "CORE"
        if upper in {"WORKER", "VANGUARD", "RANGER"}:
            return upper
    unit_type = getattr(enemy, "unit_type", None)
    if unit_type is None:
        return None
    if hasattr(unit_type, "value"):
        return str(unit_type.value).upper()
    return str(unit_type).upper()


def _enemy_type_priority(etype: str | None) -> int:
    """Rank for resolving which unit type a sighting cell should keep when
    several enemies share one cell.  The enemy CORE (总部) is permanent and its
    spawned workers routinely stand on its own cell, so a WORKER seen there must
    never downgrade the label to a worker scout; CORE is the strongest hint,
    unknown ENEMY the weakest."""
    return {
        "ENEMY": 0,
        "WORKER": 1,
        "VANGUARD": 2,
        "RANGER": 2,
        "CORE": 3,
    }.get(str(etype or "").upper(), 0)


def _is_combat_threat(enemy: Any) -> bool:
    """True when the visible enemy can deal combat damage.

    Workers and enemy Cores have no attack. Only Vanguard melee and Ranger
    shots can hurt a friendly unit. Unknown stubs (tests / bare objects) stay
    treated as threats so missing type data fails safe.
    """
    name = _enemy_unit_type_name(enemy)
    if name in {"WORKER", "CORE"}:
        return False
    if name in {"VANGUARD", "RANGER"}:
        return True
    return True


def _combat_threats(enemies: tuple | list) -> tuple:
    """Filter visible enemies down to units that can actually attack."""
    return tuple(e for e in enemies if _is_combat_threat(e))


def _enemy_danger_cells(
    enemies: tuple | list,
    obstacle_cells: frozenset[tuple[int, int]],
    ranger_range: int = 3,
) -> set[tuple[int, int]]:
    """Cells a visible enemy combat unit could attack this Tick.

    Uses the real attack geometry — Ranger 8-way Chebyshev shots up to
    ``ranger_range`` with line-of-sight, Vanguard cardinal melee — instead of a
    Manhattan radius, so a diagonal shooter at (2,2) covers the cells it can
    actually hit. Mines falling in this set are unsafe to work even when the
    enemy is several Manhattan cells away.
    """
    danger: set[tuple[int, int]] = set()
    for enemy in _combat_threats(enemies):
        origin = tuple(enemy.position)
        if _enemy_unit_type_name(enemy) == "RANGER":
            for dx in range(-ranger_range, ranger_range + 1):
                for dy in range(-ranger_range, ranger_range + 1):
                    if dx == 0 and dy == 0:
                        continue
                    if max(abs(dx), abs(dy)) > ranger_range:
                        continue
                    if not (dx == 0 or dy == 0 or abs(dx) == abs(dy)):
                        continue
                    cell = (origin[0] + dx, origin[1] + dy)
                    if not _line_blocked(origin, cell, obstacle_cells):
                        danger.add(cell)
        else:
            danger.update(_neighbor_cells(origin))
    return danger


def _cell_fire_cover(
    cell: tuple[int, int],
    enemies: tuple | list,
    obstacle_cells: frozenset[tuple[int, int]],
    ranger_range: int = 3,
) -> int:
    """How many visible enemy combat units could hit ``cell`` without moving.

    Production review 2026-11-23 (5h, 5,700 unit-ticks near enemies): a unit
    whose END cell was on a visible Ranger's fire line was hit 22% of the time
    even while moving; an end cell outside every fire line was hit 0.1%. The
    old "keep moving and you are safe" rule is wrong against predictive
    shooters; the end cell's exposure is what matters. Used to pick escape and
    approach steps that stay off every visible fire line.
    """
    cover = 0
    for enemy in _combat_threats(enemies):
        if _kite_enemy_can_hit(cell, enemy, obstacle_cells, ranger_range):
            cover += 1
    return cover


def _nearest_worker_threat(
    pos: tuple[int, int],
    combat_enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    threat_radius: int,
    ranger_range: int = 3,
) -> Any | None:
    """Nearest enemy that actually threatens a worker at ``pos``, or None.

    A worker must flee when an enemy can shoot it, not merely when one is close
    by Manhattan distance. The old radius-only test missed diagonal Rangers: an
    enemy at (2,2) is Chebyshev 2 (a legal shot) but Manhattan 4, outside the
    radius-3 flee trigger, so workers harvested calmly and were shot dead at the
    same camped mine. Threaten on the radius OR the real attack geometry.
    """
    nearest = None
    best = 1 << 30
    for enemy in combat_enemies:
        epos = tuple(enemy.position)
        dist = _manhattan(pos, epos)
        if dist <= threat_radius or _kite_enemy_can_hit(
            pos, enemy, obstacle_cells, ranger_range,
        ):
            if dist < best:
                best = dist
                nearest = enemy
    return nearest


def _attack_retreat_decision(
    enemies: tuple,
    squad_pos: tuple[int, int] | None,
    squad_size: int,
    radius: int,
    enemy_memory: set[tuple[int, int]],
    *,
    squad_units: Iterable[Any] = (),
    threat_radius: int = 0,
    detail_out: dict | None = None,
) -> tuple[bool, int, tuple[int, int] | None, frozenset[tuple[int, int]]]:
    """Decide whether the attack squad is outmatched and should disengage.

    Auto-attack engagement policy: the squad disengages when the enemy
    combat units near it outmatch it. Two signals, either one is enough:

      * numbers -- enemies within ``threat_radius`` (Chebyshev, measured from
        the squad centroid) are at least as numerous as the squad;
      * hit points -- the same enemy group out-HPs the whole squad, because
        ``squad_units`` is every living squad member regardless of where it
        stands. Measuring only the members inside ``threat_radius`` made the
        comparison asymmetric: at t646406 R6 stood 12 cells out, so the squad
        counted 8 HP against the enemies' 8 and retreated while actually
        holding 10 HP. The far straggler is still part of the fight it will
        reinforce, so its HP counts.

    ``threat_radius`` defaults to ``radius``. It exists because `radius` is
    also the old numbers-only gate: with ``attack_retreat_radius = 4`` the
    squad never even *saw* the enemies it was trading with at 7-10 cells
    (observed t646406: centroid (-652,-106), enemies at (-660,-107)..(-662,-108),
    so `nearby` was empty and the 5-unit squad fought on to 7 losses). Pass the
    real engagement leash here.

    A single enemy never triggers a squad-wide retreat on its own; ties on HP
    do, because a tie means the squad trades 1:1 or worse against a group it
    could out-manoeuvre instead.

    ``radius <= 0`` disables the whole check. Returns a
    ``(retreat, enemy_count, cluster_centroid, forbidden_targets)`` tuple;
    ``forbidden_targets`` covers enemy-memory sightings inside the cluster's
    footprint so the auto target scorer skips the cluster and marches on the
    next-best sighting instead.
    """
    forbidden: frozenset[tuple[int, int]] = frozenset()
    reach = 0 if radius <= 0 else max(int(threat_radius or 0), int(radius), 0)
    # Sum the squad before the early return: an idle Tick (no visible enemies)
    # must still report the squad's real HP, otherwise the decision log says
    # friend_hp=0 and a replay reads it as a wiped squad.
    friend_hp = sum(int(getattr(unit, "hp", 0) or 0) for unit in squad_units)
    detail = {
        "retreat": False,
        "reach": reach,
        "squad_size": int(squad_size),
        "friend_hp": friend_hp,
        "enemy_hp": 0,
        "enemy_count": 0,
        "outnumbered": False,
        "hp_outmatched": False,
        "out_hp": False,
    }
    if radius <= 0 or squad_pos is None or squad_size <= 0 or not enemies:
        if detail_out is not None:
            detail_out.update(detail)
        return False, 0, None, forbidden

    threats = [e for e in enemies if _is_combat_threat(e)]
    nearby = [
        e for e in threats if _chebyshev(tuple(e.position), squad_pos) <= reach
    ]
    enemy_hp = sum(int(getattr(enemy, "hp", 0) or 0) for enemy in nearby)
    outnumbered = len(nearby) >= squad_size
    hp_outmatched = friend_hp > 0 and enemy_hp >= friend_hp
    out_hp = hp_outmatched and len(nearby) >= 2
    detail.update({
        "friend_hp": friend_hp,
        "enemy_hp": enemy_hp,
        "enemy_count": len(nearby),
        "outnumbered": outnumbered,
        "hp_outmatched": hp_outmatched,
        "out_hp": out_hp,
        "retreat": bool(outnumbered or out_hp),
    })
    if detail_out is not None:
        detail_out.update(detail)
    if not nearby:
        return False, 0, None, forbidden
    if not outnumbered and not out_hp:
        return False, len(nearby), None, forbidden

    # outmatched: disengage and re-target.
    cluster_cells = [tuple(e.position) for e in nearby]
    cx = round(sum(x for x, _ in cluster_cells) / len(cluster_cells))
    cy = round(sum(y for _, y in cluster_cells) / len(cluster_cells))
    forbidden = frozenset(
        p
        for p in enemy_memory
        if any(_chebyshev(p, c) <= reach for c in cluster_cells)
    )
    if detail_out is not None:
        detail_out["retreat"] = True
        detail_out["forbidden"] = len(forbidden)
    return True, len(nearby), (cx, cy), forbidden


def _worker_flee(
    worker,
    uid: str,
    pos: tuple[int, int],
    enemy_pos: tuple[int, int],
    core_pos: tuple[int, int],
    obstacle_cells: frozenset[tuple[int, int]],
    others: frozenset[tuple[int, int]],
    carrying: bool,
    *,
    enemies: tuple | list = (),
    ranger_range: int = 3,
) -> tuple[str, str]:
    """Move one step away from the enemy.

    A threatened worker spends every Tick MOVING (never WAIT / HARVEST /
    DEPOSIT). Candidate cells are ranked first by how many visible enemies
    could hit the END cell (``_cell_fire_cover`` over ``enemies``), then by
    distance from the nearest enemy. Production 2026-11-23: 7 of 14 worker
    deaths fled away from the nearest Ranger straight onto another Ranger's
    fire line while a cover-0 neighbour existed (W23 took 8 damage stepping
    LEFT into six shooters). Fall back to any free cell; only WAIT when every
    neighbor is blocked. `others` includes the enemy cells themselves, so the
    worker can never step onto an enemy.

    Cargo workers bias hard toward the Core so they do not ping-pong on the
    same two cells next to a stationary attacker while carrying resources home.
    """
    prev = _worker_last_pos.get(uid)
    recent = _worker_recent.get(uid, [])
    recent_set = set(recent)
    # Detect 2-cell oscillation: A->B->A. Break it by banning the reverse
    # step when any other free neighbor exists.
    oscillating = (
        len(recent) >= 2
        and recent[-1] == pos
        and prev == recent[-2]
        and prev is not None
    )
    candidates: list[tuple[float, int, Direction, tuple[int, int]]] = []
    for d in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT):
        nx, ny = pos[0] + d.delta[0], pos[1] + d.delta[1]
        npos = (nx, ny)
        if npos in obstacle_cells or npos in others:
            continue
        dist_enemy = _manhattan(npos, enemy_pos)
        score = dist_enemy * 10.0
        if enemies:
            # Fire-line exposure dominates every other term: one covering
            # shooter outweighs the whole distance/backtrack/cargo bias.
            score -= 100.0 * _cell_fire_cover(
                npos, enemies, obstacle_cells, ranger_range,
            )
        if carrying:
            closer_home = _manhattan(pos, core_pos) - _manhattan(npos, core_pos)
            score += closer_home * 8.0
            if closer_home > 0:
                score += 4.0
        if _is_dead_end_step(npos, obstacle_cells):
            score -= 5.0
        else:
            score += 5.0
        if prev and npos == prev:
            # Strong anti-backtrack. Oscillating cargo couriers must not keep
            # flipping LEFT/RIGHT next to the same enemy.
            score -= 12.0 if carrying else 6.0
            if oscillating:
                score -= 20.0
        if npos in recent_set:
            score -= 4.0 if carrying else 2.0
        candidates.append((score, d.delta[0] * 4 + d.delta[1], d, npos))
    candidates.sort(reverse=True)

    def _try_pick(allow_reverse: bool, allow_dead_end: bool) -> tuple[str, str] | None:
        for _score, _tie, d, npos in candidates:
            if not allow_dead_end and _is_dead_end_step(npos, obstacle_cells):
                continue
            if not allow_reverse and prev and npos == prev and oscillating:
                continue
            worker.move(d)
            _worker_last_pos[uid] = pos
            hist = _worker_recent.get(uid, [])
            hist.append(pos)
            if len(hist) > 6:
                hist.pop(0)
            _worker_recent[uid] = hist
            _set_worker_route(worker, tuple(npos), [tuple(pos), npos], complete=False)
            return ("MOVE", f"{d.name} flee-enemy@{enemy_pos}")
        return None

    for allow_dead_end in (False, True):
        picked = _try_pick(allow_reverse=False, allow_dead_end=allow_dead_end)
        if picked is not None:
            return picked
    for allow_dead_end in (False, True):
        picked = _try_pick(allow_reverse=True, allow_dead_end=allow_dead_end)
        if picked is not None:
            return picked
    worker.wait()
    _set_worker_route(worker, tuple(pos), [tuple(pos)], complete=True)
    return ("WAIT", "flee-boxed")


def _plan_waypoint(
    unit,
    name: str,
    waypoint: tuple[int, int],
    *,
    config: dict[str, Any],
    obstacle_cells: frozenset[tuple[int, int]],
    occupied: frozenset[tuple[int, int]],
    enemies: tuple,
    core_pos: tuple[int, int],
    cell_counts: Mapping | None = None,
) -> tuple[str, str]:
    """March one unit along its manual target queue; resume planning on arrival.

    Dashboard per-unit targets are display-name keyed (W3/V2/R1). Each unit has
    an ordered queue of coordinates plus a march mode:
      - rush   -> pure march to the current target, ignoring everything — no
                  engagement and no enemy evasion, even for workers.
      - attack -> engage every visible enemy on the way (vanguard sweeps /
                  ranger fires / both close on the nearest enemy), then resume
                  the march. Workers cannot fight, so they keep the evasion
                  rule so a manual trip cannot get them killed.

    Reaching the current target pops it and advances to the next one; an empty
    queue removes the unit's entry and the unit falls back to its normal
    program behavior. A target that is an obstacle counts as reached from the
    cell adjacent to it. A target that cannot be reached at all (sealed off /
    permanently occupied) is SKIPPED after _WAYPOINT_STUCK_THRESHOLD ticks of
    no progress, so a stuck waypoint does not stall the rest of the queue.
    """
    pos = tuple(unit.position)
    if (
        isinstance(waypoint, (list, tuple))
        and len(waypoint) == 2
        and not isinstance(waypoint[0], (list, tuple, dict))
    ):
        # Legacy single-target entry {name: [x, y]}: a walk-only rush queue.
        waypoint = {
            "queue": [(int(waypoint[0]), int(waypoint[1]))],
            "mode": "rush",
        }
    queue = [(int(x), int(y)) for x, y in waypoint["queue"]]
    mode = str(waypoint.get("mode") or "attack")
    if mode not in ("attack", "rush"):
        mode = "attack"
    if not queue:
        return ("WAIT", "waypoint-empty")
    target = queue[0]
    is_worker = getattr(unit, "unit_type", None) == UnitType.WORKER
    uid = str(unit.id)

    def _record(path: list[tuple[int, int]], complete: bool) -> None:
        if is_worker:
            _set_worker_route(unit, target, path, complete=complete)
        else:
            _set_unit_route(unit, target, path, complete=complete)

    def _finish(detail_prefix: str) -> tuple[str, str]:
        _waypoint_stuck.pop(uid, None)
        _pop_waypoint_target(name, expected_target=target)
        _record([tuple(pos), target], complete=True)
        return ("WAIT", f"{detail_prefix} {target}")

    if pos == target:
        return _finish("waypoint-reached")

    # Obstacle target: the closest reachable success is the adjacent cell.
    if target in obstacle_cells and _manhattan(pos, target) <= 1:
        return _finish("waypoint-reached-adjacent")

    # Capacity-aware occupancy: the server stacks up to _CELL_UNIT_LIMIT units
    # per cell, so a single friendly occupant shares its cell and marchers
    # squeeze past instead of wedging head-on. Enemy and packed cells stay
    # hard-blocked; without counts the legacy fully-blocked view applies.
    blocked = frozenset(obstacle_cells) | _capacity_blocked_cells(
        frozenset(occupied),
        frozenset(tuple(e.position) for e in enemies),
        cell_counts,
        tuple(core_pos),
    )

    def _count_stuck() -> bool:
        """Return True when the unit should skip the current target."""
        ticks, last_target = _waypoint_stuck.get(uid, (0, target))
        new_ticks = ticks + 1 if last_target == target else 1
        _waypoint_stuck[uid] = (new_ticks, target)
        return new_ticks >= _WAYPOINT_STUCK_THRESHOLD

    if mode == "attack":
        if is_worker:
            # Workers keep the survival rule while marching: never stop next to
            # an attacking enemy. Fleeing is survival, not stagnation — reset
            # the stuck counter. (rush mode deliberately disables this.)
            threat_radius = int(config.get("enemy_threat_radius", 3))
            combat_enemies = _combat_threats(enemies)
            if combat_enemies and threat_radius > 0:
                nearest = _nearest_worker_threat(
                    pos, combat_enemies, obstacle_cells, threat_radius,
                    int(config.get("ranger_attack_range", 3)),
                )
                if nearest is not None:
                    _waypoint_stuck.pop(uid, None)
                    flee_blocked = blocked | {tuple(e.position) for e in enemies}
                    return _worker_flee(
                        unit,
                        uid,
                        pos,
                        tuple(nearest.position),
                        tuple(core_pos),
                        obstacle_cells,
                        flee_blocked,
                        carrying=(getattr(unit, "cargo", 0) > 0),
                        enemies=combat_enemies,
                        ranger_range=int(config.get("ranger_attack_range", 3)),
                    )
        else:
            # 攻击模式：前进道路上遇到的敌人一律接战，打完继续行军。
            if unit.unit_type == UnitType.VANGUARD:
                # Sweep first: an adjacent enemy is free damage this Tick.
                # Approach only when no cardinal target exists, because the
                # approach MOVE would otherwise steal the Tick from a legal
                # sweep (t646406: E247 cardinal above V12, E249 diagonal).
                sweep = _vanguard_adjacent_sweep(unit, pos, enemies)
                if sweep is not None:
                    _waypoint_stuck.pop(uid, None)
                    return sweep
                approach = _vanguard_approach_for_sweep(
                    unit, pos, enemies, obstacle_cells, cell_counts,
                )
                if approach is not None:
                    _waypoint_stuck.pop(uid, None)
                    return approach
            else:
                shot = _ranger_best_shot(
                    unit,
                    pos,
                    enemies,
                    obstacle_cells,
                    int(config.get("ranger_attack_range", 3)),
                    lead_fire_enabled=bool(config.get("ranger_lead_fire_enabled", True)),
                )
                if shot is not None:
                    _waypoint_stuck.pop(uid, None)
                    return shot
            if enemies:
                nearest = min(
                    enemies, key=lambda e: _manhattan(pos, tuple(e.position))
                )
                moved = _move_towards(
                    unit,
                    pos,
                    tuple(nearest.position),
                    obstacle_cells,
                    detail_prefix="waypoint-engage",
                    cell_counts=cell_counts,
                )
                if moved is not None:
                    _waypoint_stuck.pop(uid, None)
                    return moved

    # Adjacent but the target cell itself is occupied this tick. Hold instead of
    # wandering beside it; a transient occupant may move away next tick.
    if _manhattan(pos, target) == 1 and target in blocked:
        if _count_stuck():
            return _finish("waypoint-unreachable")
        unit.wait()
        _record([tuple(pos), target], complete=True)
        return ("WAIT", f"waypoint-blocked {target}")

    goals = [target]
    if target in obstacle_cells:
        goals = [cell for cell in _neighbor_cells(target) if cell not in blocked]

    paths = [
        path
        for goal in goals
        if (
            path := _bfs_path(
                pos,
                goal,
                blocked,
                max_steps=int(config.get("bfs_max_steps", 2500)),
                avoid_dead_ends=False,
            )
        )
    ]
    path = min(paths, key=len) if paths else None
    if path is not None and len(path) > 1:
        direction = _direction_for_step(pos, path[1])
        if direction is not None:
            unit.move(direction)
            _worker_last_pos[uid] = pos
            _waypoint_stuck.pop(uid, None)
            _record(path, complete=True)
            return ("MOVE", f"{direction.name} waypoint {target}")

    if _count_stuck():
        return _finish("waypoint-unreachable")
    _record([tuple(pos), target], complete=True)
    return ("WAIT", f"waypoint-blocked {target}")


def _plan_hold(
    unit: Any,
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    config: dict[str, Any],
) -> tuple[str, str]:
    """驻守模式: stand in place and auto-attack enemies entering range.

    The unit never issues a MOVE: it holds its cell and only fires at hostile
    units that are already inside its attack range (or that a motion-track
    prediction puts inside it next Tick). Combat mirrors the kite policy's
    predictive targeting — shoot/sweep the cell a moving enemy WILL occupy
    after movement resolves, so a closing Vanguard/Ranger is hit on its
    post-move position instead of the stale cell it just left.
    """
    pos = tuple(unit.position)
    if getattr(unit, "unit_type", None) == UnitType.WORKER:
        # Workers cannot fight; hold mode simply pins them in place.
        unit.wait()
        return ("WAIT", "hold-stationary")
    attack_range = int(config.get("ranger_attack_range", 3))
    threats = _combat_threats(enemies)
    if unit.unit_type == UnitType.VANGUARD:
        sweep = _vanguard_adjacent_sweep(unit, pos, enemies)
        if sweep is not None:
            return sweep
        # Predictive sweep: an advancing enemy is about to step into an
        # adjacent cardinal cell — sweep that cell so the hit lands on its
        # post-move position (movement resolves before combat).
        for enemy in sorted(
            threats, key=lambda e: _chebyshev(pos, tuple(e.position)),
        ):
            predicted = _kite_motion_info(enemy, pos).get("predicted")
            if predicted is None or predicted in obstacle_cells:
                continue
            dx, dy = predicted[0] - pos[0], predicted[1] - pos[1]
            if abs(dx) + abs(dy) != 1:
                continue
            direction = _cardinal_toward_delta(dx, dy)
            if direction is None:
                continue
            unit.sweep(direction)
            return (
                "SWEEP",
                f"{direction.name} hold-predict {predicted} "
                f"enemy at {tuple(enemy.position)}",
            )
        unit.wait()
        return ("WAIT", "hold-stationary")
    # Ranger: first shoot where a moving threat will be next Tick (kite-style
    # trajectory prediction), then fall back to the best current-position shot
    # (catches stationary enemies / workers without a stable velocity).
    for enemy in sorted(
        threats, key=lambda e: _chebyshev(pos, tuple(e.position)),
    ):
        if _enemy_unit_type_name(enemy) == "RANGER":
            continue
        predicted = _kite_motion_info(enemy, pos).get("predicted")
        if predicted is None:
            continue
        fired = _kite_ranger_fire(
            unit, pos, enemy, predicted, obstacle_cells, attack_range,
        )
        if fired is not None:
            return fired
    shot = _ranger_best_shot(
        unit,
        pos,
        enemies,
        obstacle_cells,
        attack_range,
        lead_fire_enabled=bool(config.get("ranger_lead_fire_enabled", True)),
    )
    if shot is not None:
        return shot
    unit.wait()
    return ("WAIT", "hold-stationary")


def _assign_delivery_lease(workers: Any, core_pos: tuple[int, int]) -> str | None:
    """Grant the core-cell delivery lease to exactly one carrier.

    The core cell is the single unloading chute (capacity: Core + one unit).
    When every cargo worker races for it each tick, only one squeeze-in
    succeeds and the rest get CELL_UNIT_LIMIT rejections while the ring
    oscillates approach/backoff forever (observed: 6 deposits per 87 ticks,
    145 rejected moves). Serializing the chute fixes it: all carriers except
    the lease holder queue at Manhattan distance 2, so entry is uncontested
    and the deposit rhythm settles at the enter/deposit/vacate cycle.

    The lease is STICKY: the current holder keeps it while carrying and either
    in queue range or still closing in, so equidistant carriers never flip the
    lease every tick (the flip let several carriers sit on the ring at once
    and race the same cell). It rotates away only when the holder drops its
    cargo, is stuck _DELIVERY_STUCK_LIMIT ticks (rejected moves = no progress),
    or stalls _DELIVERY_STALL_LIMIT ticks without getting closer while still
    beyond queue range (fleeing/wedged courier must not starve the queue).
    """
    global _delivery_lease_uid, _delivery_next_uid
    carriers = [w for w in workers if int(getattr(w, "cargo", 0) or 0) > 0]
    carrier_by_uid = {str(w.id): w for w in carriers}

    if not carriers:
        _delivery_lease_uid = None
        _delivery_next_uid = None
        _delivery_next_pos = None
        _delivery_stall.clear()
        return None

    def _dist(w: Any) -> int:
        return _manhattan(tuple(w.position), core_pos)

    def _pick_next(lease: str) -> None:
        # Next-in-line: the closest other carrier pre-positions adjacent to
        # the chute and chains in the tick the occupant leaves. Sticky on
        # ties so the waiting slot does not flip every tick.
        global _delivery_next_uid, _delivery_next_pos
        rest = [w for w in carriers if str(w.id) != lease]
        if not rest:
            _delivery_next_uid = None
            _delivery_next_pos = None
            return

        def _nkey(w: Any) -> tuple[int, int]:
            return (_dist(w), 0 if str(w.id) == _delivery_next_uid else 1)

        chosen = min(rest, key=_nkey)
        _delivery_next_uid = str(chosen.id)
        _delivery_next_pos = tuple(chosen.position)

    # Stall watchdog: ticks without closing in on the chute.
    current = _delivery_lease_uid
    if current in carrier_by_uid:
        d = _dist(carrier_by_uid[current])
        prev_d, n = _delivery_stall.get(current, (d, 0))
        _delivery_stall[current] = (d, 0 if d < prev_d else n + 1)
    else:
        current = None
    for wuid in [k for k in _delivery_stall if k not in carrier_by_uid]:
        _delivery_stall.pop(wuid, None)

    if current is not None:
        holder = carrier_by_uid[current]
        d = _dist(holder)
        _d_prev, stall_n = _delivery_stall.get(current, (d, 0))
        holder_stuck = (
            _worker_stuck_ticks.get(current, 0) >= _stuck_limit_at(d)
        )
        keeps_lease = not holder_stuck and (d <= 2 or stall_n < _DELIVERY_STALL_LIMIT)
        if keeps_lease:
            # Livelock watch: a holder parked in queue range that stops moving
            # at all is boxed in by the queue itself — flag yield so the other
            # queued carriers sidestep instead of holding their slots.
            hpos = tuple(holder.position)
            last_pos, n = _delivery_holder_queue_stall.get(current, (hpos, 0))
            frozen = n + 1 if hpos == last_pos else 0
            _delivery_holder_queue_stall[current] = (hpos, frozen)
            global _delivery_lease_yield
            _delivery_lease_yield = d <= 2 and frozen >= _DELIVERY_HOLDER_YIELD_AFTER
            _pick_next(current)
            return current
    _delivery_lease_yield = False

    def _key(w: Any) -> tuple[bool, int, int]:
        wuid = str(w.id)
        stuck = _worker_stuck_ticks.get(wuid, 0) >= _stuck_limit_at(_dist(w))
        return (stuck, _dist(w), 0 if wuid == current else 1)

    _delivery_lease_uid = str(min(carriers, key=_key).id)
    _delivery_stall.pop(_delivery_lease_uid, None)
    _pick_next(_delivery_lease_uid)
    return _delivery_lease_uid


def _stuck_limit_at(dist: int) -> int:
    """Stuck tolerance before the lease rotates, by distance to the chute.

    In queue range (<= 2) a frozen holder is usually just waiting out the
    enter/deposit/vacate cycle or the yield dance, so it gets double the
    grace before the lease churns away from it; rotating too eagerly makes
    several blocked carriers pass the lease around without anyone entering.
    """
    return _DELIVERY_STUCK_LIMIT if dist > 2 else _DELIVERY_STUCK_LIMIT * 2


def _queue_sidestep(
    worker,
    uid: str,
    pos: tuple[int, int],
    core_pos: tuple[int, int],
    obstacle_cells: frozenset[tuple[int, int]],
    others: frozenset[tuple[int, int]],
) -> tuple[str, str]:
    """Step off the queue lane so the boxed-in lease holder can pass.

    Prefers a free, non-dead-end cell that does not get closer to the core
    (sideways first, outward second); falls back to any free cell, and only
    waits when nothing is free this tick.
    """
    prev = _worker_last_pos.get(uid)
    recent_set = set(_worker_recent.get(uid, []))
    here = _manhattan(pos, core_pos)
    candidates: list[tuple[int, int, Direction, tuple[int, int]]] = []
    for d in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT):
        npos = (pos[0] + d.delta[0], pos[1] + d.delta[1])
        if npos in obstacle_cells or npos in others:
            continue
        if _is_dead_end_step(npos, obstacle_cells):
            continue
        nd = _manhattan(npos, core_pos)
        closer = 1 if nd < here else 0
        backtrack = 1 if prev and npos == prev else 0
        revisit = 1 if npos in recent_set else 0
        candidates.append((closer, backtrack, revisit, d, npos))
    candidates.sort()
    if candidates:
        _closer, _backtrack, _revisit, d, npos = candidates[0]
        worker.move(d)
        _worker_last_pos[uid] = pos
        recent = _worker_recent.get(uid, [])
        recent.append(pos)
        if len(recent) > 6:
            recent.pop(0)
        _worker_recent[uid] = recent
        _set_worker_route(worker, core_pos, [tuple(pos), npos], complete=False)
        return ("MOVE", f"{d.name} core-queue-yield")
    worker.wait()
    _set_worker_route(worker, core_pos, [tuple(pos)], complete=False)
    return ("WAIT", "core-queue-yield-boxed")


def _worker_remember_explore(uid: str, pos: tuple[int, int]) -> None:
    """Append one observation to the explore tail (repeats collapse)."""
    history = _worker_explore_history.setdefault(uid, [])
    if history and history[-1] == pos:
        return
    history.append(pos)
    if len(history) > _WORKER_EXPLORE_MEMORY:
        del history[:-_WORKER_EXPLORE_MEMORY]


def _worker_explore_ages(uid: str) -> dict[tuple[int, int], int]:
    """``cell -> steps since it was last stood on`` over the explore tail."""
    history = _worker_explore_history.get(uid) or []
    newest = len(history) - 1
    ages: dict[tuple[int, int], int] = {}
    for index, cell in enumerate(history):
        ages[cell] = newest - index
    return ages


def _is_tight_loop(
    uid: str,
    *,
    window: int = 12,
    span: int = 3,
    min_steps: int = 8,
) -> bool:
    """True when the unit's recent trail is trapped in a small area."""
    history = _worker_explore_history.get(uid) or []
    recent = history[-window:]
    if len(recent) < min_steps:
        return False
    xs = [cell[0] for cell in recent]
    ys = [cell[1] for cell in recent]
    return max(max(xs) - min(xs), max(ys) - min(ys)) <= span


def _worker_rally_goal(
    uid: str,
    pos: tuple[int, int],
    core_pos: tuple[int, int],
    config: dict[str, Any],
) -> tuple[int, int] | None:
    """Rally target for a worker that no mine assignment can ever reach.

    Mines are only assignable inside ``worker_mine_max_distance`` of the Core,
    so an unassigned worker outside that ring mines nothing however far it
    wanders — the fan-out just burns the unit. Two thresholds give hysteresis:
    walking home starts only past ``2 x radius`` and stops only inside
    ``radius / 2``, because one threshold flips rally/explore every tick at the
    boundary and drops the worker back into the milling it just escaped. A
    radius of 0 disables the filter itself, so there is no excluded zone.
    """
    mine_radius = int(config.get("worker_mine_max_distance", 0) or 0)
    if mine_radius <= 0:
        return None
    dist = _manhattan(pos, core_pos)
    if _worker_rally_home.get(uid):
        if dist <= max(4, mine_radius // 2):
            _worker_rally_home.pop(uid, None)
            return None
        return core_pos
    if dist > mine_radius * 2:
        _worker_rally_home[uid] = True
        return core_pos
    return None


def _rally_route_goal(
    goal: tuple[int, int],
    pos: tuple[int, int],
    obstacle_cells: frozenset[tuple[int, int]],
    blocked: frozenset[tuple[int, int]],
) -> tuple[int, int]:
    """Pick the cell a home march should be routed *to*, beside the Core.

    While any carrier is delivering, the chute reservation puts the Core cell in
    every empty worker's transient blockers, so an A* whose goal is that cell has
    no legal final step: on the live map the search burned its whole 11,000
    expansion budget, failed, armed the 120-Tick miss cooldown and left the
    worker walking the same 8 cells 185 from home (W9, ticks 541306-541343).
    Routing to an open neighbour of the same goal finds the identical route for
    1.1k expansions, and the worker still hands back to normal mining inside
    ``radius // 2`` — it never needs the Core cell itself.
    """
    neighbours = [
        (goal[0] + dx, goal[1] + dy)
        for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0))
    ]
    open_cells = [cell for cell in neighbours if cell not in obstacle_cells]
    if not open_cells:
        return goal
    enterable = [cell for cell in open_cells if cell not in blocked]
    pool = enterable or open_cells
    return min(
        pool,
        key=lambda cell: (abs(cell[0] - pos[0]) + abs(cell[1] - pos[1]), cell),
    )


def _plan_worker(
    worker,
    core,
    *,
    resource_cells: frozenset[tuple[int, int]],
    obstacle_cells: frozenset[tuple[int, int]],
    depleted: set[tuple[int, int]],
    config: dict[str, int | bool],
    occupied: frozenset[tuple[int, int]] = frozenset(),
    enemies: tuple = (),
    lease_uid: str | None = None,
    next_uid: str | None = None,
    cell_counts: Mapping | None = None,
    contest_blocked: frozenset[tuple[int, int]] = frozenset(),
) -> tuple[str, str]:
    """Return (action_name, detail).

    ``lease_uid`` names the single carrier allowed to contest the core cell
    this tick (see _assign_delivery_lease); ``next_uid`` the carrier that
    waits adjacent and chains in the tick the chute empties. None disables
    the queue — used by unit tests and any call outside choose_actions.
    ``contest_blocked`` are cells this worker has been refused entry to
    repeatedly (MOVE_CONTESTED); they ride the transient-blocker channel, not
    the wall set, so the dead-end cache keeps its identity.

    ``cell_counts`` (friendly unit count per cell) enables capacity-aware
    retreats: a neighbour with one occupant is squeezable (server limit 2).
    Without it, retreats keep the legacy fully-blocked view.
    """
    pos = worker.position
    uid = str(worker.id)
    # Cells taken by other units this tick. Workers cannot move into them, so
    # pathfinding treats them as temporary obstacles. The worker's own cell is
    # excluded; the core cell is not in `occupied` at all (the core may be
    # stepped on to unload), so it only blocks when another unit stands there.
    core_pos = tuple(core.position)
    others = occupied - {pos}

    # A harvest fills the worker in one action (2 units while the Core carries
    # the beacon), so a partially-loaded worker can never top up — the server
    # answers any further harvest with HARVEST_FAILED / CARGO_FULL, permanently
    # wedging the worker on the mine at partial cargo. Such a worker returns
    # home to deposit what it carries; only empty workers mine.
    carrying = worker.cargo > 0
    # While carriers are delivering, the chute is reserved: empty workers must
    # not route through the core cell (they would step back onto it the tick
    # after vacating and block the one-out-one-in hand-off).
    if not carrying and _chute_in_demand and pos != core_pos:
        others = others | {core_pos}

    # Movement view: the server stacks up to _CELL_UNIT_LIMIT units on one
    # cell, so a cell with a single friendly occupant is enterable — workers
    # meeting head-on in a narrow lane pass each other instead of both WAITing
    # forever (observed W13/W22 wedging each other). Enemy cells, packed cells
    # and the core cell stay hard-blocked. Queue coordination below keeps the
    # strict ``others`` view; only actual stepping uses the squeezed set.
    move_blocked = _capacity_blocked_cells(
        others,
        frozenset(tuple(e.position) for e in enemies),
        cell_counts,
        core_pos,
    )
    if contest_blocked:
        # Cells this worker has been refused again and again join the transient
        # blockers (the same channel fellow units use), never the wall set: a
        # fresh obstacle object would defeat the identity-cached dead-end
        # structure for every later caller.
        move_blocked = move_blocked | frozenset(contest_blocked)

    # Retreat view: with per-cell counts the friendly units are judged by
    # capacity (< _CELL_UNIT_LIMIT is squeezable) and only enemy cells stay
    # hard-blocked; the core-cell mirroring above carries over so an adjacent
    # retreat never steps back onto the chute mid-delivery. Without counts the
    # legacy `others` set blocks every occupied cell outright.
    if cell_counts is not None:
        _retreat_blocked = frozenset(tuple(e.position) for e in enemies) | (
            {core_pos} if not carrying and _chute_in_demand and pos != core_pos
            else frozenset()
        )
        _retreat_counts: Mapping | None = cell_counts
    else:
        _retreat_blocked = others
        _retreat_counts = None

    # 金币满仓 + 配置开启 → 工人进入探索模式（仅工人）。仓库放不下更多金币时，
    # 工人不再挖矿/围在核心转圈，而是散开去探索；空载与载货都探索。仓库一旦有
    # 空位（生产/修盾花掉金币），下个 tick 自动恢复正常挖矿/交矿。
    explore_mode = (
        bool(config.get("worker_explore_when_full", False))
        and turn_context.resource_space == 0
    )

    # ── Enemy evasion ─────────────────────────────────────────────────
    # Game rule (player-confirmed): a moving unit is never hit — only a
    # stationary target takes damage. When a visible enemy is inside the
    # threat radius, this worker must spend the Tick MOVING away from it,
    # never HARVEST / DEPOSIT / WAIT. Evasion outranks everything: a dead
    # worker mines nothing. Radius 0 (dashboard knob) disables the safety.
    threat_radius = int(config.get("enemy_threat_radius", 3))
    # Only combat-capable enemies force evasion. Hostile Workers cannot attack,
    # so dancing away from them only wastes cargo trips and creates LEFT/RIGHT
    # oscillation next to a harmless unit.
    combat_enemies = _combat_threats(enemies)
    if combat_enemies and threat_radius > 0:
        nearest_enemy = _nearest_worker_threat(
            pos, combat_enemies, obstacle_cells, threat_radius,
            int(config.get("ranger_attack_range", 3)),
        )
        if nearest_enemy is not None:
            # `others` already carries the enemy cells in production (occupied
            # includes them); union them again so a fleeing worker can never
            # step onto an enemy even if `occupied` was not passed. Block all
            # visible enemy cells (including non-combat Workers) as geometry.
            flee_blocked = move_blocked | {tuple(e.position) for e in enemies}
            return _worker_flee(
                worker,
                uid,
                pos,
                tuple(nearest_enemy.position),
                core_pos,
                obstacle_cells,
                flee_blocked,
                carrying,
                enemies=combat_enemies,
                ranger_range=int(config.get("ranger_attack_range", 3)),
            )

    if pos == core.position and turn_context.resource_space > 0 and carrying:
        worker.deposit()
        _set_worker_route(worker, tuple(core.position), [tuple(pos)], complete=True)
        return ("DEPOSIT", f"at_core cargo={worker.cargo}")

    # ── Vacate the core cell promptly ────────────────────────────────────
    # The core cell holds at most two entities (the Core + one Unit), so it is
    # the single unloading chute for the whole fleet. An empty worker lingering
    # on it — freshly spawned, or just finished depositing — blocks every
    # cargo worker trying to unload, and the surrounding ring then deadlocks
    # (the on-core worker cannot leave because the ring is full, the ring
    # cannot advance because the chute is occupied). Step off to any free
    # neighbour immediately instead of waiting for the normal goal/BFS flow to
    # maybe move it. A carrying worker was already handled by DEPOSIT above, so
    # only empty workers reach this. If every neighbour is blocked this tick we
    # fall through and let the normal logic retry next tick.
    if pos == core_pos and not carrying:
        vacate = _retreat_from(pos, core_pos, obstacle_cells, _retreat_blocked, _retreat_counts)
        if vacate is None and (_delivery_next_pos is not None or _chute_in_demand):
            # Ring fully packed: swap slots with an adjacent carrier — it
            # chains into the chute while this worker takes its cell. Prefer
            # the designated next-in-line, else any unit on the ring
            # (local view of the occupied ring beats the global pick).
            swap_target = None
            if (
                _delivery_next_pos is not None
                and abs(_delivery_next_pos[0] - pos[0])
                + abs(_delivery_next_pos[1] - pos[1])
                == 1
            ):
                swap_target = _delivery_next_pos
            else:
                for d in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT):
                    npos = (pos[0] + d.delta[0], pos[1] + d.delta[1])
                    if npos in others and npos not in obstacle_cells:
                        swap_target = npos
                        break
            if swap_target is not None:
                vacate = _direction_for_step(pos, swap_target)
        if vacate is not None:
            npos = (pos[0] + vacate.delta[0], pos[1] + vacate.delta[1])
            worker.move(vacate)
            _worker_last_pos[uid] = pos
            recent = _worker_recent.get(uid, [])
            recent.append(pos)
            if len(recent) > 6:
                recent.pop(0)
            _worker_recent[uid] = recent
            _set_worker_route(worker, npos, [tuple(pos), npos], complete=False)
            return ("MOVE", f"{vacate.name} vacate-core")

    if pos in resource_cells and pos not in depleted and not carrying and not explore_mode:
        worker.harvest()
        _set_worker_route(worker, tuple(pos), [tuple(pos)], complete=True)
        return ("HARVEST", f"on_resource {pos}")

    # ── Delivery lease queue ─────────────────────────────────────────────
    # The lease (see _assign_delivery_lease) lets exactly one carrier contest
    # the chute; every other carrier queues at Manhattan distance 2 and waits
    # its turn instead of racing in and bouncing on CELL_UNIT_LIMIT. Queued
    # workers beyond distance 2 keep marching toward the queue area.
    chute_reserved = (
        carrying
        and not explore_mode
        and lease_uid is not None
        and uid != lease_uid
        and uid != next_uid
        and pos != core_pos
    )
    if chute_reserved:
        dist_core = _manhattan(pos, core_pos)
        if _delivery_lease_yield and dist_core <= 2:
            # The lease holder is boxed in by the queue itself: sidestep.
            return _queue_sidestep(worker, uid, pos, core_pos, obstacle_cells, others)
        if dist_core <= 1:
            # On the ring: back out to distance 2 so the insider can vacate.
            retreat = _retreat_from(pos, core_pos, obstacle_cells, _retreat_blocked, _retreat_counts)
            if retreat is not None:
                npos = (pos[0] + retreat.delta[0], pos[1] + retreat.delta[1])
                worker.move(retreat)
                _worker_last_pos[uid] = pos
                recent = _worker_recent.get(uid, [])
                recent.append(pos)
                if len(recent) > 6:
                    recent.pop(0)
                _worker_recent[uid] = recent
                _set_worker_route(worker, core_pos, [tuple(pos), npos], complete=False)
                return ("MOVE", f"{retreat.name} core-queue-backoff")
        elif dist_core == 2:
            # Queue slot: hold position until the lease rotates to this worker.
            worker.wait()
            _set_worker_route(worker, core_pos, [tuple(pos)], complete=False)
            return ("WAIT", "core-queue-hold")
        # dist >= 3: fall through and keep marching toward the queue area.

    # ── Next-in-line delivery pipeline ───────────────────────────────────
    # Unit moves resolve as one global dependency graph, so the next carrier
    # may step onto the core cell the same tick the occupant steps off
    # ("one out, one in"). It therefore camps adjacent (distance 1) instead
    # of two cells out, cutting the enter delay from two ticks to zero.
    if (
        carrying
        and not explore_mode
        and uid == next_uid
        and pos != core_pos
    ):
        dist_core = _manhattan(pos, core_pos)
        if (
            dist_core == 1
            and not _chute_vacating_this_tick
            and (core_pos in others or _lease_holder_adjacent)
        ):
            # Insider still unloading inside (or the holder entering this
            # tick): hold the adjacent slot.
            worker.wait()
            _set_worker_route(worker, core_pos, [tuple(pos)], complete=False)
            return ("WAIT", "core-next-hold")
        # Chute free or vacating this tick, or still marching in from afar:
        # fall through so BFS targets the core cell; the goal is exempted
        # from the transient blockers below, and the server chains the entry
        # behind the occupant's departure when it is still occupied.

    # ── Unloading-chute coordination ─────────────────────────────────────
    # The core cell is the single unloading chute (capacity 2: Core + one
    # Unit). While another unit occupies it, the lease holder (or any carrier
    # when no queue is active) must NOT crowd the immediate ring: the on-core
    # worker can only vacate into a free neighbour, so a ring packed with
    # waiting cargo workers deadlocks the chute. Hold back at Manhattan
    # distance >= 2 until the chute frees up; only step in once it is actually
    # empty. Non-lease carriers are governed by the lease queue above, so this
    # branch never parks them far from home while the chute cycles.
    if (
        carrying
        and pos != core_pos
        and core_pos in others
        and (lease_uid is None or uid == lease_uid)
        and not _chute_vacating_this_tick
    ):
        dist_core = _manhattan(pos, core_pos)
        if dist_core <= 1:
            # On the ring: back out to distance 2 so the insider can vacate.
            retreat = _retreat_from(pos, core_pos, obstacle_cells, _retreat_blocked, _retreat_counts)
            if retreat is not None:
                npos = (pos[0] + retreat.delta[0], pos[1] + retreat.delta[1])
                worker.move(retreat)
                _worker_last_pos[uid] = pos
                recent = _worker_recent.get(uid, [])
                recent.append(pos)
                if len(recent) > 6:
                    recent.pop(0)
                _worker_recent[uid] = recent
                _set_worker_route(worker, core_pos, [tuple(pos), npos], complete=False)
                return ("MOVE", f"{retreat.name} core-queue-backoff")
        else:
            # Already at distance >= 2: hold position and wait for the chute.
            worker.wait()
            _set_worker_route(worker, core_pos, [tuple(pos)], complete=False)
            return ("WAIT", "core-queue-hold")

    goal: tuple[int, int] | None = None
    if not explore_mode:
        if carrying:
            # A partial load is still worth depositing; the worker can never fill
            # the rest en route, so it heads home with whatever it has.
            goal = core.position
        elif resource_cells:
            # Only go to assigned resource, not the nearest one
            assigned = _resource_assignments.get(uid)
            if assigned and assigned in resource_cells:
                goal = assigned
            # If no assignment, skip visible resources (they're assigned to other workers)
        # Fallback: use remembered resource coordinates (only if assigned)
        if goal is None and not carrying:
            assigned = _resource_assignments.get(uid)
            if assigned and assigned in _resource_memory:
                goal = assigned

    # Reaching a remembered target without seeing a resource confirms that the
    # memory is stale. Forget it and explore immediately instead of waiting on
    # the empty cell forever. (Carrying workers are heading home to deposit —
    # the Core cell is never a stale resource target.)
    if goal == pos and not carrying and pos not in resource_cells:
        _forget_resource(pos)
        _resource_assignments.pop(uid, None)
        goal = None

    # ── Un-stick recovery ────────────────────────────────────────────────
    # A worker frozen in place for _STUCK_THRESHOLD ticks is almost always
    # chasing a remembered resource that has been depleted or is occupied by an
    # enemy (server keeps rejecting the move). Drop the stale goal and force a
    # re-plan/explore instead of hammering the same blocked cell forever.
    if _worker_stuck_pos.get(uid) == pos:
        stuck = _worker_stuck_ticks.get(uid, 0) + 1
    else:
        stuck = 0
    _worker_stuck_pos[uid] = pos
    _worker_stuck_ticks[uid] = stuck
    if stuck >= _STUCK_THRESHOLD:
        _worker_path_cache.pop(uid, None)
        _cargo_route_miss_dist.pop(uid, None)
        if not carrying or explore_mode:
            if goal is not None and tuple(goal) != core_pos:
                _forget_resource(goal)
            _resource_assignments.pop(uid, None)
            _worker_stuck_ticks[uid] = 0
            goal = None

    # ── Core-cell congestion coordination ────────────────────────────────
    # With a fixed core, every full worker funnels to the same cell, which holds
    # one unit at a time. Blindly moving into an occupied core cell is rejected
    # by the server, so the ring of full workers wedges in place. Rules:
    #   - a worker standing on the core cell leaves first (frees the chute);
    #   - a full worker only approaches the core when the cell is actually free,
    #     otherwise it backs out of the immediate ring or waits.
    # (core_pos is already defined above for `others`.)

    # Move toward goal (BFS multi-step pathfinding, avoids dead ends)
    if config["worker_bfs_enabled"] and goal is not None and goal != pos:
        cached_move = _worker_cached_path_step(worker, uid, pos, goal, obstacle_cells, move_blocked)
        if cached_move is not None:
            return cached_move

        # Workers can walk onto the core cell; the game may report it as an
        # obstacle so temporarily exclude it from the pathfinding obstacle set.
        # Other units are real blockers and stay in the obstacle set. Split into
        # wall `obstacles` (cached dead-end base) + transient `extras` (occupied
        # cells), so the wall dead-end cache is not defeated per worker.
        obstacles_here = obstacle_cells
        extras_here = move_blocked
        if goal == core_pos:
            if goal in obstacle_cells:
                # Rare: the Core cell itself is in the wall memory. Must exclude
                # it from the wall set (new frozenset -> batch dead-end fallback,
                # unavoidable but rare). Set-algebra keeps the blocked set
                # identical to the old (obstacle_cells | others) - {goal}.
                obstacles_here = obstacle_cells - {goal}
                extras_here = move_blocked - {goal}
            elif goal in move_blocked:
                # Goal (Core) occupied by a unit this tick: keep the stable wall
                # object so dead-ends come from the cached base, and just drop
                # the goal from the transient blockers so it stays enterable.
                # dead((O | X) - {g}) == dead(O | (X - {g})) when g not in O,
                # so the blocked set is byte-identical — but no new frozenset.
                extras_here = move_blocked - {goal}
        cargo_home = carrying and goal == core_pos
        search_budget = int(config["bfs_max_steps"])
        run_search = True
        if cargo_home:
            scaled_budget = _cargo_home_budget(
                uid, pos, core_pos, int(config["bfs_max_steps"])
            )
            if scaled_budget is None:
                run_search = False
            else:
                search_budget = scaled_budget
        path = (
            _bfs_path(
                pos,
                goal,
                obstacles_here,
                max_steps=search_budget,
                extras=extras_here,
            )
            if run_search
            else None
        )
        if cargo_home and run_search:
            if path and len(path) > 1:
                # Clear on any success: a stale miss record from a nearer cell
                # would otherwise suppress every far retry for this carrier.
                _cargo_route_miss_dist.pop(uid, None)
            elif _manhattan(pos, core_pos) > _CARGO_HOME_MISS_FAR:
                _cargo_route_miss_dist[uid] = _manhattan(pos, core_pos)
        if path and len(path) > 1:
            _worker_path_cache[uid] = {
                "goal": tuple(goal),
                "path": path,
                # Cursor of the unit on the path — path[0] is the current cell.
                "index": 0,
            }
        else:
            _worker_path_cache.pop(uid, None)
        bfs_dir = _direction_for_step(pos, path[1]) if path and len(path) > 1 else None
        if bfs_dir is not None:
            nx, ny = pos[0] + bfs_dir.delta[0], pos[1] + bfs_dir.delta[1]
            npos = (nx, ny)
            # Step only into a free cell (the goal cell itself counts only
            # when free — or when its occupant vacates this tick, so the
            # entry chains behind the departure: "one out, one in").
            if (npos not in obstacle_cells or npos == goal) and (
                npos not in move_blocked
                or (npos == goal and _chute_vacating_this_tick)
            ):
                worker.move(bfs_dir)
                _worker_last_pos[uid] = pos
                recent = _worker_recent.get(uid, [])
                recent.append(pos)
                if len(recent) > 6:
                    recent.pop(0)
                _worker_recent[uid] = recent
                _set_worker_route(worker, tuple(goal), path, complete=True)
                return ("MOVE", f"{bfs_dir.name} -> {goal}")
        # BFS failed (goal blocked/unreachable this tick) — coordinate congestion.
        # A worker standing on the core frees the unloading chute; a full worker
        # wedged into the core ring backs out or waits instead of hammering the
        # occupied core cell every tick.
        if pos == core_pos and not carrying:
            for d in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT):
                npos = (pos[0] + d.delta[0], pos[1] + d.delta[1])
                if npos in obstacle_cells or npos in move_blocked:
                    continue
                if _is_dead_end_step(npos, obstacle_cells):
                    continue
                worker.move(d)
                _worker_last_pos[uid] = pos
                recent = _worker_recent.get(uid, [])
                recent.append(pos)
                if len(recent) > 6:
                    recent.pop(0)
                _worker_recent[uid] = recent
                _set_worker_route(worker, npos, [tuple(pos), npos], complete=False)
                return ("MOVE", f"{d.name} vacate-core")
        if carrying and goal == core_pos and core_pos in others:
            if pos != core_pos and _manhattan(pos, core_pos) <= 1:
                retreat = _retreat_from(pos, core_pos, obstacle_cells, _retreat_blocked, _retreat_counts)
                if retreat is not None:
                    worker.move(retreat)
                    _worker_last_pos[uid] = pos
                    recent = _worker_recent.get(uid, [])
                    recent.append(pos)
                    if len(recent) > 6:
                        recent.pop(0)
                    _worker_recent[uid] = recent
                    _set_worker_route(worker, core_pos, [tuple(pos)], complete=False)
                    return ("MOVE", f"{retreat.name} core-retreat")
            worker.wait()
            _set_worker_route(worker, core_pos, [tuple(pos)], complete=True)
            return ("WAIT", "core-congested")
        # Path search failed this tick. Keep the goal so cargo/resource greedy
        # fallbacks still march the same way instead of flipping into explore.

    # Cargo worker greedy fallback toward core (also used when BFS misses)
    if worker.cargo and goal is not None:
        # Re-bind goal to core for the cargo march detail string.
        goal = tuple(core.position)
    if worker.cargo and not explore_mode:
        prev = _worker_last_pos.get(uid)
        recent = _worker_recent.get(uid, [])
        recent_set = set(recent)
        cargo_candidates: list[tuple[int, Direction, tuple[int, int]]] = []
        for d in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT):
            nx, ny = pos[0] + d.delta[0], pos[1] + d.delta[1]
            npos = (nx, ny)
            if npos in obstacle_cells or npos in move_blocked:
                continue
            dist = _manhattan(npos, core.position)
            if prev and npos == prev:
                dist += int(config["backtrack_penalty"])
            if npos in recent_set:
                dist += 3
            if _is_dead_end_step(npos, obstacle_cells):
                dist += 50
            cargo_candidates.append((dist, d, npos))
        cargo_candidates.sort(key=lambda item: item[0])
        for _dist, d, npos in cargo_candidates:
            worker.move(d)
            _worker_last_pos[uid] = pos
            recent = _worker_recent.get(uid, [])
            recent.append(pos)
            if len(recent) > 6:
                recent.pop(0)
            _worker_recent[uid] = recent
            _set_worker_route(
                worker,
                tuple(core.position),
                [tuple(pos), npos],
                complete=False,
            )
            return ("MOVE", f"{d.name} -> {core.position}")

    # No goal at all: fan out with backtracking + dead-end avoidance.
    # In explore_mode a carrying worker may also fan out (it cannot deposit while
    # storage is full, and a moving unit cannot be hit, so scouting is safer
    # than milling around the core).
    if goal is None and (not carrying or explore_mode):
        uid = str(worker.id)
        # Far-from-home, unassignable, and milling: worth more walking back to
        # the mining zone than scouting the pocket it is standing in. Mines are
        # only assigned inside worker_mine_max_distance of the Core, so a mine
        # found 800 cells out stays unassignable until the Core arrives.
        # Full-vault explore_mode is the one case where fanning out IS the plan,
        # so the rally stays out of it.
        rally = (
            None
            if explore_mode
            else _worker_rally_goal(uid, pos, tuple(core.position), config)
        )
        if rally is not None:
            # A greedy step alone cannot break a wall island. On the live map
            # memory two workers (W2/W9) circled the same 6 cells for 171 Ticks
            # — net displacement 2, cargo 0 — 175 cells from the Core, while a
            # real route home existed. March the rally with the same cached A*
            # the combat units use for home-return, and keep the budget-free
            # greedy step for the Tick the search misses or the per-Tick march
            # cap is already spent. The search is aimed at a cell beside the
            # Core, never at the Core cell itself (see _rally_route_goal).
            route_goal = _rally_route_goal(rally, pos, obstacle_cells, move_blocked)
            marched = _move_towards(
                worker,
                pos,
                route_goal,
                obstacle_cells,
                detail_prefix="rally-home",
                cell_counts=cell_counts,
                extra_obstacles=move_blocked,
                max_steps=int(config.get("bfs_max_steps", 2500)),
            ) or _long_march_step(
                worker,
                pos,
                route_goal,
                obstacle_cells,
                detail_prefix="rally-home",
                cell_counts=cell_counts,
                extra_obstacles=move_blocked,
            )
            if marched is not None:
                _worker_remember_explore(uid, pos)
                try:
                    march_dir = Direction[marched[1].split()[0]]
                except (KeyError, IndexError):
                    march_dir = None
                march_next = (
                    (pos[0] + march_dir.delta[0], pos[1] + march_dir.delta[1])
                    if march_dir is not None
                    else pos
                )
                _set_worker_route(worker, rally, [tuple(pos), march_next], complete=False)
                return marched
        prev = _worker_last_pos.get(uid)
        recent = _worker_recent.get(uid, [])
        idx = hash(uid) % 4
        base = [Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT]
        rotated = base[idx:] + base[:idx]
        # Sort: deprioritize dead ends, backtracking and recently visited cells
        recent_set = set(recent)
        visited_age = _worker_explore_ages(uid)

        def _sort_key(d):
            nx, ny = pos[0] + d.delta[0], pos[1] + d.delta[1]
            score = 0
            if _is_dead_end_step((nx, ny), obstacle_cells):
                score += 3  # obvious dead end = avoid first
            # A 4-step memory cannot break an 8-cell ring: by the time the unit
            # comes back around, every cell is "unrecent" again and the fixed
            # ID direction rotation re-picks the same circuit forever (observed
            # W7/W31/W13/W20 each visiting the same 8 cells 78 times in 625
            # ticks). Age over the whole tail instead, so the never-walked exit
            # of a pocket always outranks the ring successor.
            age = visited_age.get((nx, ny))
            if age is None:
                score += 0  # fresh ground wins
            elif age < 6:
                score += 4
            elif age < 12:
                score += 3
            elif age < 18:
                score += 2
            else:
                score += 1
            if (nx, ny) in recent_set:
                score += 2  # recently visited
            if config["avoid_backtracking"] and prev and (nx, ny) == prev:
                score += 1  # backtracking
            return score
        rotated.sort(key=_sort_key)
        for d in rotated:
            nx, ny = pos[0] + d.delta[0], pos[1] + d.delta[1]
            if (nx, ny) in obstacle_cells or (nx, ny) in move_blocked:
                continue
            # Never explore into a recognized dead end when any open alternative exists.
            if _is_dead_end_step((nx, ny), obstacle_cells):
                # Only take it if every remaining free neighbor is also a dead end
                # or blocked (unit is already trapped / must exit).
                free = [
                    (pos[0] + od.delta[0], pos[1] + od.delta[1])
                    for od in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT)
                    if (pos[0] + od.delta[0], pos[1] + od.delta[1]) not in obstacle_cells
                ]
                open_free = [
                    cell for cell in free
                    if not _is_dead_end_step(cell, obstacle_cells)
                ]
                if open_free:
                    continue
            worker.move(d)
            _worker_last_pos[uid] = pos
            recent.append(pos)
            if len(recent) > 4:
                recent.pop(0)
            _worker_recent[uid] = recent
            _worker_remember_explore(uid, pos)
            _set_worker_route(worker, (nx, ny), [tuple(pos), (nx, ny)], complete=True)
            return ("MOVE", f"{d.name} explore")

    if goal is not None and goal != pos:
        direction = _step_towards(pos, goal)
        if direction is not None:
            nx = pos[0] + direction.delta[0]
            ny = pos[1] + direction.delta[1]
            if (nx, ny) not in obstacle_cells and (nx, ny) not in move_blocked and not (
                _is_dead_end_step((nx, ny), obstacle_cells, allow=(goal,))
            ):
                worker.move(direction)
                _set_worker_route(
                    worker,
                    tuple(goal),
                    [tuple(pos), (nx, ny)],
                    complete=False,
                )
                return ("MOVE", f"{direction.name} -> {goal}")

    if pos == core_pos and not carrying:
        # Last-resort chute release: every exit was blocked above, but the
        # core cell must never hold an empty worker while carriers queue.
        # Swap with any adjacent occupant (friendly swaps are legal), and if
        # even that is unavailable wait with a flag so diagnostics catch it.
        for d in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT):
            npos = (pos[0] + d.delta[0], pos[1] + d.delta[1])
            if npos in obstacle_cells:
                continue
            if npos in move_blocked:
                worker.move(d)
                _worker_last_pos[uid] = pos
                _set_worker_route(worker, npos, [tuple(pos), npos], complete=False)
                return ("MOVE", f"{d.name} vacate-core-swap")
            if not _is_dead_end_step(npos, obstacle_cells):
                worker.move(d)
                _worker_last_pos[uid] = pos
                _set_worker_route(worker, npos, [tuple(pos), npos], complete=False)
                return ("MOVE", f"{d.name} vacate-core")

    worker.wait()
    _set_worker_route(worker, tuple(pos), [tuple(pos)], complete=True)
    return ("WAIT", "no_action")


# Cardinal + diagonal offsets used by guerrilla units to fan out.
_EIGHT_WAY_DELTAS: tuple[tuple[int, int], ...] = (
    (0, -1),   # N
    (1, -1),   # NE
    (1, 0),    # E
    (1, 1),    # SE
    (0, 1),    # S
    (-1, 1),   # SW
    (-1, 0),   # W
    (-1, -1),  # NW
)
_EIGHT_WAY_LABELS: tuple[str, ...] = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
# Vision radius per guerrilla unit type (game rules; same values the
# stale-sighting removal uses): Vanguard 4 / Ranger 5.
_GUERRILLA_SIGHT: dict[str, int] = {"vanguard": 4, "ranger": 5}
# A core is considered "isolated" when none of the eight cells immediately
# around it contains an enemy unit that can deal combat damage.  Keep this
# deliberately local: a Vanguard/Ranger elsewhere on the map is not a guard
# for this target, while a combat unit in one of the surrounding cells is.
_GUERRILLA_CORE_GUARD_RADIUS = 1
# Remember a solo guerrilla's last-seen exposed core for a few ticks: at a
# wall corner the core can flicker out of sight for a tick or two, and
# immediately reverting to the roam bearing makes the unit A-B-A. The lock
# lets the normal A* route commit around the corner instead.
_GUERRILLA_CORE_MEMORY_TICKS = 6
# A flickering non-core prey needs the same lock, but a much shorter one. The
# oscillation feeds itself: the cell the unit just left is the one that had
# sight of the prey, so the blind Tick reverts to the roam bearing, steps
# straight back, and sees the prey again. Holding the last-seen cell for one
# Tick breaks that cycle, while a longer leash would send a guerrilla chasing a
# fleeing worker across the map.
# (observed: V6 shuttled (-978,-2765)/(-978,-2766)/(-977,-2766) for 92 Ticks
# against a worker parked two cells north behind a corner.)
_GUERRILLA_TARGET_MEMORY_TICKS = 2
# Auto-attack core hunting only trusts recent sightings: cores move, and an
# old marker would send the squad to a cell that has been empty for hours.
_CORE_HUNT_MEMORY_MAX_AGE_TICKS = 2000
# Auto 目标打分必须区分"这拍看见的"和"几千拍前看见的"：`_enemy_memory` 只在友方
# 亲眼看到该格已空时才遗忘，被墙挡住的目击点永远留着（实测堆到 3005 格）。只按距离
# 打分时，一个 4153 拍前的幽灵格 (-976,-2788) 会和当期目击 (-887,-2816) 逐拍互换
# argmin 胜者：V15 与风筝 V9/V11/V13 在 450 拍里来回翻转 311-363 次那个 117 格长的
# 目标，净位移只剩 92-131 格，而且每次翻转都重跑一次 A*（寻路展开中位数 78 → 4786）。
# 惩罚不能封顶：封成 200 拍后，所有更旧的格子拿到同一个 400 分，彼此又退化成"只比
# 距离"，于是换目标只是从"幽灵 vs 活的"变成"幽灵 vs 幽灵"（observed 549776-550223：
# 进攻 V19/V21 与风筝 V20 有 75% 的拍次目标是"当期看不见、且离最近可见敌人 ≥60 格"
# 的记忆格，448 拍内相邻目标跳变 >40 格 204 次，净位移只剩 45-91 格）。不封顶后，
# 越新的目击严格压制越旧的，整队只在一个格子的记忆真的消失时才换目标。
_AUTO_TARGET_STALE_PENALTY_PER_TICK = 2


def _core_position_unguarded(
    core_pos: tuple[int, int],
    enemies: Iterable[Any],
    *,
    guard_radius: int = _GUERRILLA_CORE_GUARD_RADIUS,
    ignore: Any = None,
) -> bool:
    """Return whether *core_pos* has no adjacent enemy combat-unit escort.

    Enemy workers and cores do not count as guards.  Unknown enemy types are
    treated as threats by :func:`_is_combat_threat`, so incomplete sightings
    fail safely instead of making a defended core look exposed.
    """
    radius = max(0, int(guard_radius))
    for enemy in enemies:
        if enemy is ignore:
            continue
        raw_enemy_pos = getattr(enemy, "position", None)
        if raw_enemy_pos is None:
            continue
        enemy_pos = tuple(raw_enemy_pos)
        if len(enemy_pos) != 2 or not _is_combat_threat(enemy):
            continue
        if _chebyshev(core_pos, enemy_pos) <= radius:
            return False
    return True


def _guerrilla_core_isolated(
    core: Any,
    enemies: Iterable[Any],
    *,
    guard_radius: int = _GUERRILLA_CORE_GUARD_RADIUS,
) -> bool:
    """Return whether *core* has no nearby enemy combat-unit escort."""
    raw_core_pos = getattr(core, "position", None)
    if raw_core_pos is None:
        return False
    core_pos = tuple(raw_core_pos)
    if len(core_pos) != 2:
        return False
    return _core_position_unguarded(
        core_pos, enemies, guard_radius=guard_radius, ignore=core,
    )


def _auto_core_targets(
    candidates: set[tuple[int, int]],
    enemies: Iterable[Any],
) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    """Split remembered CORE sightings into fresh unguarded targets and drops.

    Fresh, unguarded cores are returned as priority targets.  Guarded core
    memories are returned separately so the caller can remove them from the
    normal candidate pool instead of marching into an escorted target.
    """
    tick = int(getattr(turn_context, "tick", 0) or 0)
    targets: set[tuple[int, int]] = set()
    drop: set[tuple[int, int]] = set()
    for pos in candidates:
        if _enemy_memory_types.get(pos) != "CORE":
            continue
        if not _core_position_unguarded(pos, enemies):
            drop.add(pos)
            continue
        seen = int(_enemy_memory_ticks.get(pos, 0) or 0)
        if tick > 0 and seen > 0 and tick - seen > _CORE_HUNT_MEMORY_MAX_AGE_TICKS:
            continue
        targets.add(pos)
    return targets, drop


def _auto_target_rank(
    cell: tuple[int, int],
    core_pos: tuple[int, int] | None,
    squad_pos: tuple[int, int],
) -> int:
    """Score an auto-mode target candidate; lower wins.

    Distance to the Core (threat to the base) plus distance to the squad
    centroid (reachable now), then a stale-sighting penalty so the whole team
    commits to prey somebody actually saw instead of penduluming between two
    remembered cells whose relative score swaps every Tick.
    """
    score = _manhattan(cell, squad_pos)
    if core_pos is not None:
        score += _manhattan(cell, core_pos)
    tick = int(getattr(turn_context, "tick", 0) or 0)
    seen = int(_enemy_memory_ticks.get(cell, 0) or 0)
    age = tick - seen
    if seen > 0 and age > 0:
        score += age * _AUTO_TARGET_STALE_PENALTY_PER_TICK
    return score


def _auto_target_rows(
    candidates: Iterable[tuple[int, int]],
    core_pos: tuple[int, int] | None,
    squad_pos: tuple[int, int],
    *,
    limit: int = 3,
    priority: str | None = None,
) -> list[dict]:
    """Top-N auto target candidates with the score components that ranked them.

    The chosen cell and the reason string in ``plan_unit_actions`` show *what*
    the squad marched on; this list shows what it rejected. Candidates are the
    same set every squad member sees (memory + the shared centroid), so the
    list is stable across the Tick.
    """
    tick = int(getattr(turn_context, "tick", 0) or 0)
    rows = []
    for cell in candidates:
        seen = int(_enemy_memory_ticks.get(cell, 0) or 0)
        age = tick - seen if seen > 0 and tick > 0 else None
        row = {
            "cell": list(cell),
            "score": _auto_target_rank(cell, core_pos, squad_pos),
            "distance_squad": _manhattan(cell, squad_pos),
            "distance_core": (
                _manhattan(cell, core_pos) if core_pos is not None else None
            ),
            "age": age,
            "type": _enemy_memory_types.get(cell),
        }
        if priority is not None:
            row["priority"] = priority
        row["tick"] = tick
        rows.append(row)
    rows.sort(key=lambda row: row["score"])
    return rows[: max(int(limit), 1)]


def _guerrilla_roam_goal(unit: Any, pos: tuple[int, int]) -> tuple[int, int]:
    """Return a far, per-unit bearing goal for solo guerrilla movement.

    The goal is intentionally derived only from this unit's stable ID.  A far
    goal lets the kite scorer choose safe cells while preserving the unit's
    bearing; diagonal bearings alternate their two component axes each Tick.
    No squad centroid, kite target, or shared route is involved.
    """
    bearing = _stable_slot_index(str(unit.id), len(_EIGHT_WAY_DELTAS))
    dx, dy = _EIGHT_WAY_DELTAS[bearing]
    if dx and dy:
        tick_bit = int(getattr(turn_context, "tick", 0) or 0) & 1
        major, minor = (1000, 999) if tick_bit == 0 else (999, 1000)
        return (pos[0] + dx * major, pos[1] + dy * minor)
    return (pos[0] + dx * 1000, pos[1] + dy * 1000)


def _roam_bearing_candidates(
    bearing: int,
    *,
    tick_bit: int,
    prefer: Direction | None = None,
) -> tuple[Direction | None, ...]:
    """Cardinal step order for one eight-way bearing.

    Diagonal bearings alternate their two component axes each Tick (the same
    rule _guerrilla_roam used inline before); cardinal bearings map directly.
    When `prefer` is one of the two axes, that axis steps first regardless of
    the tick parity (the escape uses this to bias toward leaving the
    oscillation cells instead of zig-zagging back into them).
    """
    dx, dy = _EIGHT_WAY_DELTAS[bearing]
    if dx and dy:
        horizontal = Direction.RIGHT if dx > 0 else Direction.LEFT
        vertical = Direction.DOWN if dy > 0 else Direction.UP
        if prefer is not None and prefer in (horizontal, vertical):
            other = vertical if prefer is horizontal else horizontal
            return (prefer, other)
        return (horizontal, vertical) if tick_bit == 0 else (vertical, horizontal)
    return (_cardinal_toward_delta(dx, dy),)


def _roam_update_stall(uid: str) -> bool:
    """Detect a wall ping-pong from the recorded position history.

    The caller records pos into _roam_pos_history each tick (consecutive
    same-cell observations are folded, so the window counts position changes,
    not ticks — a stationary unit never fills it and never triggers); this
    returns True once the last _ROAM_STALL_WINDOW recorded positions only
    span _ROAM_STALL_CELLS or fewer distinct cells (deterministic A-B-A wall
    bouncing). Windows spanning _ROAM_STALL_CELLS + 1 cells also count, but
    only when the net window displacement (Chebyshev distance between the
    window's first and last position) is <= _ROAM_STALL_DRIFT — that catches
    3-cell loop oscillations (A-B-C-A) while genuine roaming, which keeps
    drifting outward, never trips the drift bound. Fully deterministic — no
    randomness anywhere in the escape.
    On trigger, _roam_stall_cells remembers the whole recent region (the
    bounce cells plus the approach cells), so the escape also avoids walking
    straight back in, and _roam_stall_origin remembers the cell the unit just
    came from — stepping back onto it would only replay the oscillation in
    reverse, so escape candidates deprioritize it.
    """
    history = _roam_pos_history.setdefault(uid, [])
    window = history[-_ROAM_STALL_WINDOW:]
    if len(window) >= _ROAM_STALL_WINDOW:
        distinct = set(window)
        drift = max(
            abs(window[-1][0] - window[0][0]),
            abs(window[-1][1] - window[0][1]),
        )
        # Bounding extent, not first-to-last drift: a unit circling a 2x2 box
        # (V2 at (-336,-806), V5 at (237,-619) — UP/DOWN/LEFT/RIGHT over 4 cells,
        # net 2 in 81 Ticks) returns to where it started so drift reads ~0 for a
        # genuine advance too, while the box's extent pins it inside 3x3. Real
        # roaming covers new ground and blows the box out well past it.
        extent_x = max(c[0] for c in window) - min(c[0] for c in window)
        extent_y = max(c[1] for c in window) - min(c[1] for c in window)
        stalled = (
            len(distinct) <= _ROAM_STALL_CELLS
            or (
                len(distinct) <= _ROAM_STALL_CELLS + 1
                and drift <= _ROAM_STALL_DRIFT
            )
            or (
                len(distinct) <= _ROAM_STALL_BOX_CELLS
                and extent_x <= _ROAM_STALL_BOX_EXTENT
                and extent_y <= _ROAM_STALL_BOX_EXTENT
            )
        )
        if stalled:
            region = frozenset(history)
            _roam_stall_cells[uid] = region
            _roam_stall_origin[uid] = window[-2] if len(window) >= 2 else window[-1]
            expiry = int(getattr(turn_context, "tick", 0) or 0) + _ROAM_BAN_TICKS
            banned = _roam_banned.setdefault(uid, {})
            for cell in region:
                if banned.get(cell, 0) < expiry:
                    banned[cell] = expiry
            history.clear()
            return True
    return False


def _roam_record_discovery(uid: str, pos: tuple[int, int]) -> bool:
    """Fold this step into the unit's discovery ledger; True once it orbits.

    The proof is a stall in *new ground*, not a repeated cell: production V2
    circled a 150x48 pocket with a ~468-Tick period, so every short window of
    its trail looked like fresh exploration while the whole day re-walked the
    same 426 cells (V13 471 cells, V15 25 cells). Nothing in the 8-position
    A-B-A window can see that.

    Over three days of recorded routes (145,972 guerrilla steps) the split is
    unambiguous: the units that ended a day orbiting went 2,120 / 4,559 / 4,703
    recorded steps between their last two new cells, while every unit that kept
    exploring stayed under 361. Consecutive duplicates are folded, so a unit
    deliberately holding station records nothing — the misfire that the
    stall-tick gate produced on V4's intentional 驻守 cannot repeat here.

    The ledger is not reset here: the caller arms the escape and resets it, so
    an orbit proven while another escape is still running is not thrown away.
    """
    discovered = _roam_discovered.setdefault(uid, {})
    recent = _roam_recent.setdefault(uid, deque(maxlen=_ROAM_ORBIT_REGION))
    if recent and recent[-1] == pos:
        return False
    recent.append(pos)
    if pos not in discovered:
        discovered[pos] = None
        _roam_new_stall[uid] = 0
        # Bound the ledger so a weeks-long process cannot grow without limit.
        # Forgetting the oldest discoveries only makes the detector more
        # forgiving, never more trigger-happy.
        while len(discovered) > _ROAM_ORBIT_MEMORY:
            discovered.pop(next(iter(discovered)))
        return False
    _roam_new_stall[uid] = _roam_new_stall.get(uid, 0) + 1
    return _roam_new_stall[uid] >= _ROAM_ORBIT_STALL_STEPS


def _roam_reset_discovery(uid: str) -> None:
    """Start this unit's exploration ledger over after an orbit was broken."""
    _roam_discovered.pop(uid, None)
    _roam_new_stall[uid] = 0


def _arm_roam_orbit_escape(uid: str) -> None:
    """Hand a confirmed orbit to the existing two-phase escape.

    The remembered region is the unit's recent loop rather than a two-cell
    bounce, and the budget is long enough to walk a pocket's width out of it
    (the 6-Tick bounce budget cannot cross a 150x48 region). Only the tail of
    the loop gets banned: banning every cell the unit still stands inside would
    leave it no legal step and trade an orbit for a freeze.
    """
    recent = _roam_recent.get(uid)
    if not recent:
        return
    cells = list(recent)
    region = frozenset(cells)
    _roam_stall_cells[uid] = region
    _roam_stall_origin[uid] = cells[-2] if len(cells) >= 2 else cells[-1]
    _roam_escape_left[uid] = _ROAM_ORBIT_ESCAPE_TICKS
    expiry = int(getattr(turn_context, "tick", 0) or 0) + _ROAM_BAN_TICKS
    banned = _roam_banned.setdefault(uid, {})
    for cell in cells[-_ROAM_STALL_REGION:]:
        if banned.get(cell, 0) < expiry:
            banned[cell] = expiry


def _roam_record_seen(uid: str, pos: tuple[int, int]) -> None:
    """Remember every cell this unit has stood on, oldest-first bounded.

    Bounding it to a few days of ground only makes the frontier policy more
    forgiving about ancient history, never less.
    """
    seen = _roam_seen.setdefault(uid, {})
    seen[pos] = None
    while len(seen) > _ROAM_FRONTIER_MEMORY:
        seen.pop(next(iter(seen)))


def _roam_frontier_bearing(
    uid: str,
    pos: tuple[int, int],
    base_bearing: int,
    obstacle_cells: frozenset[tuple[int, int]],
) -> int:
    """Pick the heading with the most never-visited ground straight ahead.

    The lookahead is a straight line per bearing — 8 x 3 dict lookups, and it is
    the question the ID-derived fixed bearing never asked. A heading blocked by
    a wall is skipped while any legal one exists, so a dead end still falls back
    to the base bearing and the existing cardinal scout. Ties go to whichever
    comes first in the rotation starting at the base bearing, so in virgin
    terrain every unit keeps exactly the heading it keeps today and the fleet
    keeps its spread of directions.
    """
    seen = _roam_seen.get(uid)
    if not seen:
        return base_bearing
    count = len(_EIGHT_WAY_DELTAS)
    best_bearing = base_bearing
    best_score = -1
    for offset in range(count):
        bearing = (base_bearing + offset) % count
        dx, dy = _EIGHT_WAY_DELTAS[bearing]
        if (pos[0] + dx, pos[1] + dy) in obstacle_cells:
            continue
        fresh = 0
        for step in range(1, _ROAM_FRONTIER_LOOKAHEAD + 1):
            cell = (pos[0] + dx * step, pos[1] + dy * step)
            if cell in obstacle_cells:
                break
            if cell not in seen:
                fresh += 1
        if fresh > best_score:
            best_score = fresh
            best_bearing = bearing
    return best_bearing


def _roam_break_note(uid: str) -> str:
    """Self-report the orbit break in the action detail so it is greppable.

    A safety mechanism nobody can see is a mechanism that does not exist: the
    last two of these were only found because a marker made them countable.
    """
    if _roam_break_tick.get(uid) == int(getattr(turn_context, "tick", 0) or 0):
        return " orbit-break"
    return ""


def _roam_banned_cells(uid: str) -> frozenset[tuple[int, int]]:
    """Cells this unit proved unable to roam out of and must still avoid.

    Prunes the expired entries on the way out, so a unit that escaped long ago
    gets the terrain back.
    """
    banned = _roam_banned.get(uid)
    if not banned:
        return frozenset()
    now = int(getattr(turn_context, "tick", 0) or 0)
    live = {cell: expiry for cell, expiry in banned.items() if expiry > now}
    if live:
        if len(live) != len(banned):
            _roam_banned[uid] = live
    else:
        _roam_banned.pop(uid, None)
    return frozenset(live)


def _roam_bearing_choice(
    pos: tuple[int, int],
    bearing: int,
    *,
    tick_bit: int,
    deflecting: bool,
    stall_cells: frozenset[tuple[int, int]],
    stall_origin: tuple[int, int] | None,
    avoided: frozenset[tuple[int, int]],
    obstacle_cells: frozenset[tuple[int, int]],
    unit: Any,
) -> tuple[Direction, tuple[int, int]] | None:
    """One deterministic roam step: bearing moves, or deflections while
    escaping an oscillation. Returns (direction, next_cell) or None."""
    if deflecting:
        # Deterministic deflection order: along the wall in both directions
        # (+2 / -2 eighth-turns), then straight backwards (+4).
        deflected = (
            (bearing + 2) % len(_EIGHT_WAY_DELTAS),
            (bearing + 6) % len(_EIGHT_WAY_DELTAS),
            (bearing + 4) % len(_EIGHT_WAY_DELTAS),
        )
        candidates: list[tuple[int, Direction | None]] = []
        for roam_bearing in deflected:
            for direction in _roam_bearing_candidates(roam_bearing, tick_bit=tick_bit):
                if direction is None:
                    continue
                next_cell = (
                    pos[0] + direction.delta[0],
                    pos[1] + direction.delta[1],
                )
                if next_cell not in stall_cells:
                    # Leaving the region is the goal, so try every such
                    # deflection first. Leaving cells can never be the origin
                    # (the origin sits inside the remembered region), so no
                    # further ordering is needed here.
                    candidates.append((0, direction))
        for roam_bearing in deflected:
            prefer_dir: Direction | None = None
            for direction in _roam_bearing_candidates(roam_bearing, tick_bit=tick_bit):
                if direction is None:
                    continue
                next_cell = (
                    pos[0] + direction.delta[0],
                    pos[1] + direction.delta[1],
                )
                if next_cell in stall_cells:
                    if next_cell != stall_origin:
                        candidates.append((2, direction))
                elif prefer_dir is None:
                    # Bias the diagonal axis toward the leaving cell so the
                    # escape does not zig-zag back into the oscillation.
                    prefer_dir = direction
            if prefer_dir is not None:
                candidates.append((3, prefer_dir))
        # Original bearing last: the unit is never worse off than before.
        for direction in _roam_bearing_candidates(bearing, tick_bit=tick_bit):
            candidates.append((4, direction))
    else:
        candidates = [
            (0, direction)
            for direction in _roam_bearing_candidates(bearing, tick_bit=tick_bit)
        ]
    for avoid_dead_ends in (True, False):
        for _rank, direction in sorted(candidates, key=lambda item: item[0]):
            if direction is None:
                continue
            next_cell = (
                pos[0] + direction.delta[0],
                pos[1] + direction.delta[1],
            )
            if next_cell in avoided:
                continue
            if _try_move(
                unit,
                direction,
                pos,
                obstacle_cells,
                avoid_dead_ends=avoid_dead_ends,
            ):
                return direction, next_cell
    return None


def _guerrilla_roam(
    unit: Any,
    pos: tuple[int, int],
    obstacle_cells: frozenset[tuple[int, int]],
    config: dict[str, Any],
    avoid_cells: Iterable[tuple[int, int]] = (),
) -> tuple[str, str]:
    """Take one safe step on this unit's independent eight-way bearing.

    Wall ping-pong escape: a fixed ID-derived bearing makes some units bounce
    A-B-A along a wall forever (every single move accepted by the server).
    Once the position history proves the oscillation, the escape runs in two
    deterministic phases: while still inside the recent region the unit
    deflects to the bearings perpendicular to its own (reversed bearing as
    last resort); once outside, it keeps avoiding the region until the
    _ROAM_ESCAPE_TICKS budget expires, then resumes normal roaming. The
    escaped pocket stays banned for _ROAM_BAN_TICKS and each confirmed escape
    turns the heading further, so resuming roaming means a different route.
    """
    avoided = frozenset(avoid_cells)
    uid = str(unit.id)
    tick_bit = int(getattr(turn_context, "tick", 0) or 0) & 1
    # The ID-derived base bearing plus any escape flips: after a successful
    # escape the bearing is reversed, so the unit is no longer pulled straight
    # back into the pocket it just left.
    base_bearing = _stable_slot_index(uid, len(_EIGHT_WAY_DELTAS))
    bearing = (base_bearing + _roam_bearing_offset.get(uid, 0)) % len(_EIGHT_WAY_DELTAS)
    # Per-tick position record (independent of this call's outcome) so the
    # oscillation window stays calibrated in ticks even when some ticks WAIT.
    history = _roam_pos_history.setdefault(uid, [])
    if not history or history[-1] != pos:
        history.append(pos)
        del history[:-_ROAM_STALL_REGION]
    if bool(config.get("guerrilla_frontier_roam", True)):
        _roam_record_seen(uid, pos)
    # The discovery ledger records every step regardless of escape state: an
    # orbit proven while another escape is still running must not be thrown
    # away, it arms on the first Tick that is no longer escaping.
    orbit_proven = (
        bool(config.get("roam_orbit_break", True))
        and _roam_record_discovery(uid, pos)
    )
    escaping = _roam_escape_left.get(uid, 0) > 0
    # roam_oscillation_escape gates the whole detection/trigger entry point:
    # with the switch off the unit keeps its original bearing-based roaming
    # and no escape state is ever created (an escape already in progress is
    # always allowed to finish its cooldown).
    if (
        not escaping
        and bool(config.get("roam_oscillation_escape", True))
        and _roam_update_stall(uid)
    ):
        escaping = True
        _roam_escape_left[uid] = _ROAM_ESCAPE_TICKS
    elif escaping:
        _roam_escape_left[uid] -= 1
        if _roam_escape_left[uid] <= 0:
            # Budget spent: drop the escape state so normal roaming resumes
            # and a fresh stall window can re-trigger if still oscillating.
            # An escape that never got a confirmed exit keeps its bearing.
            escaping = False
            _roam_stall_cells.pop(uid, None)
            _roam_stall_origin.pop(uid, None)
            _roam_flip_pending.pop(uid, None)
    # The 8-position window above can only prove a two-cell bounce. A unit
    # walking a long loop inside one pocket looks like fresh ground in every
    # short window (production: 468-Tick period over 426 cells, for a whole
    # day), so the discovery ledger judges the long trail instead — and it never
    # interrupts an escape that is already running.
    if orbit_proven and not escaping:
        _arm_roam_orbit_escape(uid)
        _roam_reset_discovery(uid)
        escaping = True
        _roam_break_tick[uid] = int(getattr(turn_context, "tick", 0) or 0)
        print(
            f"[orbit-break] uid={uid} 连续 {_ROAM_ORBIT_STALL_STEPS} 步没踏上任何新格，"
            f"判定为绕圈而非探索，转入 {_ROAM_ORBIT_ESCAPE_TICKS} 拍脱困",
            flush=True,
        )
    stall_cells = _roam_stall_cells.get(uid, frozenset())
    stall_origin = _roam_stall_origin.get(uid)
    deflecting = escaping and pos in stall_cells
    if (
        escaping
        and _roam_flip_pending.get(uid)
        and pos not in stall_cells
    ):
        # A deflection step pointed out of the region last tick; this tick's
        # pos proves the server actually accepted the move (a cancelled step
        # would still be standing inside the region). Only now is the
        # bearing flip confirmed — at most once per escape event.
        _roam_flip_pending.pop(uid, None)
        # The first escape reverses the heading; later ones turn it a quarter.
        # Capping this at one per lifetime left repeat-trapped units with every
        # future escape replaying the same bearing that walks them back in.
        _roam_bearing_offset[uid] = (
            _roam_bearing_offset.get(uid, 0)
            + (
                len(_EIGHT_WAY_DELTAS) // 2
                if _roam_flips_used.get(uid, 0) < 1
                else 2
            )
        ) % len(_EIGHT_WAY_DELTAS)
        _roam_flips_used[uid] = _roam_flips_used.get(uid, 0) + 1
    # guerrilla_frontier_roam: hold the fixed bearing only while escaping. The
    # bearing itself is what drags a unit back along its own trail every Tick —
    # production measured a fifth of every guerrilla step landing on ground that
    # unit had already stood on three times or more. Out of an escape, aim at the
    # direction that still has unvisited ground in front of it.
    if not escaping and bool(config.get("guerrilla_frontier_roam", True)):
        bearing = _roam_frontier_bearing(uid, pos, bearing, obstacle_cells)
    # Pockets escaped earlier stay off the route for _ROAM_BAN_TICKS: the active
    # escape forgets its region as soon as the budget expires, and without this
    # the base bearing pulls the unit straight back into the same bounce.
    avoided = avoided | _roam_banned_cells(uid)
    if deflecting:
        label = "escape"
    else:
        if escaping:
            # Cooldown phase: already outside the region — keep away from it
            # until the budget expires so the bearing does not pull the unit
            # straight back into the bounce.
            avoided = avoided | stall_cells
        label = _EIGHT_WAY_LABELS[bearing]
    choice = _roam_bearing_choice(
        pos,
        bearing,
        tick_bit=tick_bit,
        deflecting=deflecting,
        stall_cells=stall_cells,
        stall_origin=stall_origin,
        avoided=avoided,
        obstacle_cells=obstacle_cells,
        unit=unit,
    )
    if choice is not None:
        direction, next_cell = choice
        if deflecting and next_cell not in stall_cells:
            # The escape step points out of the region, but the server may
            # still cancel the move. The 180-degree bearing flip is deferred
            # until the next tick's pos confirms the unit really left (see
            # the _roam_flip_pending confirmation above).
            _roam_flip_pending[uid] = True
        _kite_record_move_attempt(unit, pos, next_cell)
        return ("MOVE", f"{direction.name} guerrilla-roam {label}{_roam_break_note(uid)}")
    action, detail = _scout_cardinal(
        unit,
        pos,
        obstacle_cells,
        config,
        label=f"guerrilla-roam {label}{_roam_break_note(uid)}",
        avoid_cells=avoided,
    )
    if action == "MOVE":
        try:
            direction = Direction[detail.split()[0]]
        except (KeyError, IndexError):
            direction = None
        if direction is not None:
            _kite_record_move_attempt(
                unit,
                pos,
                (pos[0] + direction.delta[0], pos[1] + direction.delta[1]),
            )
    else:
        _kite_clear_move_attempt(unit)
    return action, detail


def _parse_team_names(raw: Any) -> set[str]:
    """Parse a comma/space/semicolon separated combat roster into unit names."""
    if not isinstance(raw, str) or not raw.strip():
        return set()
    names: set[str] = set()
    for chunk in raw.replace(";", ",").replace(" ", ",").split(","):
        name = chunk.strip().upper()
        if name:
            names.add(name)
    return names


def _combat_team_for(unit_name: str, config: dict[str, Any]) -> str:
    """Return the priority-resolved squad for a named combat unit."""
    name = unit_name.upper()
    if name in _parse_team_names(config.get("home_team", "")):
        return "home"
    if name in _parse_team_names(config.get("attack_team", "")):
        return "attack"
    if name in _parse_team_names(config.get("kite_team", "")):
        return "kite"
    if name in _parse_team_names(config.get("guerrilla_team", "")):
        return "guerrilla"
    return "unassigned"


def _format_team_roster(names: set[str]) -> str:
    """Stable, human-friendly roster text for the dashboard config fields."""

    def sort_key(name: str) -> tuple[str, int, str]:
        prefix = name[:1]
        suffix = name[1:]
        number = int(suffix) if suffix.isdigit() else 0
        return prefix, number, name

    return ", ".join(sorted((name.upper() for name in names), key=sort_key))


def _freshest_config() -> dict[str, Any]:
    """Return a full-field config with the newest on-disk values.

    Bypasses tactic_config's signature cache so team auto-enlist never
    overwrites a concurrent dashboard save (e.g. production targets) with a
    stale in-memory copy — the dashboard and tactic are separate processes
    sharing tactic_config.json, and last-writer-wins must be the newest file.
    """
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    base = load_config(CONFIG_PATH)  # full field set (defaults + cached file)
    for key, value in raw.items():
        if key in base:
            base[key] = value
    return base


def _ensure_home_team_membership(
    config: dict[str, Any],
    unit_names: Iterable[str],
) -> dict[str, Any]:
    """Auto-enlist unassigned Vanguards/Rangers, balancing the four combat
    rosters toward the configured 新兵分队比例 (combat_team_ratio, 守:攻:筝:游).

    Each new unit joins the team with the largest deficit against its
    configured share, counting living members only (integer math keeps ties
    exact; ties resolve home → attack → kite → guerrilla). An unset, invalid
    or all-zero ratio keeps the legacy behavior: everything joins home.
    """
    normalized_names = {
        str(raw_name).strip().upper()
        for raw_name in unit_names
        if str(raw_name).strip()
    }
    # Fast path: every living unit already sits in some roster, so the
    # cross-process config lock is never touched — the hot case every Tick
    # once the army is complete.
    assigned = (
        _parse_team_names(config.get("home_team", ""))
        | _parse_team_names(config.get("attack_team", ""))
        | _parse_team_names(config.get("kite_team", ""))
        | _parse_team_names(config.get("guerrilla_team", ""))
    )
    if not (normalized_names - assigned):
        return config
    added: list[str] = []

    def apply(latest: dict[str, int | bool | str]) -> dict[str, Any] | None:
        home = _parse_team_names(latest.get("home_team", ""))
        attack = _parse_team_names(latest.get("attack_team", ""))
        kite = _parse_team_names(latest.get("kite_team", ""))
        guerrilla = _parse_team_names(latest.get("guerrilla_team", ""))
        assigned_now = home | attack | kite | guerrilla
        pending = sorted(normalized_names - assigned_now)
        if not pending:
            return None
        added.extend(pending)
        rosters = (home, attack, kite, guerrilla)
        # Off (empty/invalid) or all-zero ratio falls back to (1, 0, 0, 0):
        # the legacy home-only enlistment.
        weights = parse_team_ratio(latest.get("combat_team_ratio"))
        if not weights or not sum(weights):
            weights = (1, 0, 0, 0)
        total_weight = sum(weights)
        counts = [len(roster & normalized_names) for roster in rosters]
        for name in pending:
            total_after = sum(counts) + 1
            best = max(
                range(4),
                key=lambda t: total_after * weights[t] - counts[t] * total_weight,
            )
            rosters[best].add(name)
            counts[best] += 1
        latest["home_team"] = _format_team_roster(home)
        latest["attack_team"] = _format_team_roster(attack)
        latest["kite_team"] = _format_team_roster(kite)
        latest["guerrilla_team"] = _format_team_roster(guerrilla)
        return latest

    try:
        saved = mutate_config(apply, CONFIG_PATH)
        if added:
            print(
                f"[team] auto-enlisted {', '.join(added)} by ratio "
                f"{saved.get('combat_team_ratio', '')!r} -> "
                f"home={saved['home_team']} | attack={saved['attack_team']} | "
                f"kite={saved['kite_team']} | guerrilla={saved['guerrilla_team']}",
                flush=True,
            )
        return saved
    except Exception as exc:
        print(f"[team] auto-enlist save failed: {exc}", flush=True)
        return config


def _auto_enlist_new_combat_units(turn: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Give every living Vanguard/Ranger a name and default them to home.
    Also remove dead units from all team rosters."""
    names: list[str] = []
    for vanguard in getattr(turn, "vanguards", ()) or ():
        names.append(_object_name(vanguard.id, "V"))
    for ranger in getattr(turn, "rangers", ()) or ():
        names.append(_object_name(ranger.id, "R"))

    alive = {n.upper() for n in names}
    config = _ensure_home_team_membership(config, names)

    # Fast path: no dead name in any roster — skip the per-Tick lock pass
    # entirely (rosters only change when a unit dies or the dashboard edits).
    rosters = (
        _parse_team_names(config.get("home_team", "")),
        _parse_team_names(config.get("attack_team", "")),
        _parse_team_names(config.get("kite_team", "")),
        _parse_team_names(config.get("guerrilla_team", "")),
    )
    if not any(roster - alive for roster in rosters):
        return config

    # Prune dead units from the latest config under the same cross-process lock
    # used by dashboard saves.
    changed = False

    def prune(latest: dict[str, int | bool | str]) -> dict[str, Any] | None:
        nonlocal changed
        for team_key in ("home_team", "attack_team", "kite_team", "guerrilla_team"):
            old = _parse_team_names(latest.get(team_key, ""))
            pruned = old & alive
            if pruned != old:
                latest[team_key] = _format_team_roster(pruned)
                changed = True
        return latest if changed else None

    try:
        saved = mutate_config(prune, CONFIG_PATH)
        if changed:
            print(f"[team] pruned dead from rosters", flush=True)
        return saved
    except Exception as exc:
        print(f"[team] roster prune failed: {exc}", flush=True)
        return config


def _cardinal_toward_delta(dx: int, dy: int) -> Direction | None:
    """Convert any free-form offset into the nearest legal cardinal move."""
    if dx == 0 and dy == 0:
        return None
    if abs(dx) >= abs(dy):
        return Direction.RIGHT if dx > 0 else Direction.LEFT
    return Direction.DOWN if dy > 0 else Direction.UP


def _try_move(
    unit: Any,
    direction: Direction,
    pos: tuple[int, int],
    obstacle_cells: frozenset[tuple[int, int]],
    *,
    avoid_dead_ends: bool = False,
    allow: Iterable[tuple[int, int]] = (),
) -> bool:
    nx, ny = pos[0] + direction.delta[0], pos[1] + direction.delta[1]
    if (nx, ny) in obstacle_cells:
        return False
    if avoid_dead_ends and _is_dead_end_step((nx, ny), obstacle_cells, allow=allow):
        return False
    unit.move(direction)
    uid = str(unit.id)
    _worker_last_pos[uid] = pos
    # The distance-free march's 2x2 orbit break compares visit ages, so every
    # step this unit takes must enter the same tail — recording only the march's
    # own steps let a mixed march/_move_towards loop stay invisible (R16 cycled
    # A-B-C-D around a wall corner for 100+ ticks).
    _worker_remember_explore(uid, pos)
    return True


def _move_towards(
    unit: Any,
    pos: tuple[int, int],
    goal: tuple[int, int],
    obstacle_cells: frozenset[tuple[int, int]],
    *,
    detail_prefix: str,
    cell_counts: Mapping | None = None,
    extra_obstacles: Iterable[tuple[int, int]] = (),
    step_filter: Callable[[tuple[int, int]], bool] | None = None,
    max_steps: int = 2500,
) -> tuple[str, str] | None:
    if pos == goal:
        _combat_path_cache.pop(str(unit.id), None)
        return None

    uid = str(unit.id)
    counts = cell_counts or {}
    transient_obstacles = frozenset(extra_obstacles)
    last_pos = _worker_last_pos.get(uid)

    def _reverses_last_step(cell: tuple[int, int]) -> bool:
        # The greedy fallback below ranks neighbours by distance alone, so when
        # the distance-best cell is the one this unit just left it walks straight
        # back. Callers only get their distance-free march (with the
        # anti-backtrack ladder) when this returns None; without the guard a
        # wall pocket becomes an endless A-B-A (V8/V11/R11/R14/R16 home-return
        # and attack-regroup, 100+ ticks on 2-3 cells).
        return last_pos is not None and cell == last_pos and cell != goal

    def _step_allowed(cell: tuple[int, int]) -> bool:
        if cell in obstacle_cells or cell in transient_obstacles:
            return False
        if counts.get(cell, 0) >= _CELL_UNIT_LIMIT:
            return False
        return step_filter is None or bool(step_filter(cell))

    cached = _combat_path_cache.get(uid)
    if cached is not None:
        cached_goal = cached.get("goal")
        if (
            isinstance(cached_goal, tuple)
            and cached_goal != goal
            and _manhattan(cached_goal, goal) <= _MARCH_GOAL_DRIFT_CELLS
            and _manhattan(pos, goal) > _MARCH_GOAL_DRIFT_CELLS
        ):
            goal = cached_goal
    path: list[tuple[int, int]] | None = None
    if cached is not None and cached.get("goal") == goal:
        cached_path = cached.get("path") or []
        index = _path_index(cached_path, pos, cached.get("index", -1))
        if index >= 0 and index + 1 < len(cached_path):
            next_cell = cached_path[index + 1]
            if _step_allowed(next_cell) and not _is_dead_end_step(
                next_cell, obstacle_cells, allow=(goal,),
            ):
                path = cached_path
                cached["index"] = index
    if path is None:
        _combat_path_cache.pop(uid, None)
        budget = _march_budget(uid, pos, goal, max_steps)
        path = (
            _bfs_path(
                pos,
                goal,
                obstacle_cells,
                max_steps=budget,
                extras=transient_obstacles,
            )
            if budget is not None
            else None
        )
        _march_note_result(uid, goal, budget, bool(path and len(path) > 1))
        if path and len(path) > 1:
            _combat_path_cache[uid] = {"goal": goal, "path": path, "index": 0}

    if path and len(path) > 1:
        entry = _combat_path_cache.get(uid)
        index = _path_index(path, pos, entry.get("index", -1) if entry else -1)
        next_cell = path[index + 1] if 0 <= index + 1 < len(path) else None
        direction = _direction_for_step(pos, next_cell) if next_cell else None
        if (
            direction is not None
            and _step_allowed(next_cell)
            and _try_move(
                unit,
                direction,
                pos,
                obstacle_cells,
                avoid_dead_ends=True,
                allow=(goal,),
            )
        ):
            if entry is not None:
                entry["index"] = index + 1
            _set_unit_route(unit, goal, path, complete=True)
            return ("MOVE", f"{direction.name} {detail_prefix} {goal}")

    # Preserve the cheap greedy fallback when A* exhausts its search budget.
    direction = _step_towards(pos, goal)
    if direction is None:
        return None
    # Skip 凸-shaped cul-de-sacs unless the goal itself is inside one.
    allow = (goal,)
    next_cell = (pos[0] + direction.delta[0], pos[1] + direction.delta[1])
    if (
        _step_allowed(next_cell)
        and not _reverses_last_step(next_cell)
        and _try_move(
            unit,
            direction,
            pos,
            obstacle_cells,
            avoid_dead_ends=True,
            allow=allow,
        )
    ):
        _set_unit_route(unit, goal, [pos, goal], complete=False)
        return ("MOVE", f"{direction.name} {detail_prefix} {goal}")
    # Prefer alternate axis when the primary step is blocked.
    dx = goal[0] - pos[0]
    dy = goal[1] - pos[1]
    alternates: list[Direction] = []
    if abs(dx) >= abs(dy):
        if dy > 0:
            alternates.append(Direction.DOWN)
        elif dy < 0:
            alternates.append(Direction.UP)
        if dx > 0:
            alternates.append(Direction.RIGHT)
        elif dx < 0:
            alternates.append(Direction.LEFT)
    else:
        if dx > 0:
            alternates.append(Direction.RIGHT)
        elif dx < 0:
            alternates.append(Direction.LEFT)
        if dy > 0:
            alternates.append(Direction.DOWN)
        elif dy < 0:
            alternates.append(Direction.UP)
    for alt in alternates:
        if alt == direction:
            continue
        alt_cell = (pos[0] + alt.delta[0], pos[1] + alt.delta[1])
        if (
            _step_allowed(alt_cell)
            and not _reverses_last_step(alt_cell)
            and _try_move(
                unit,
                alt,
                pos,
                obstacle_cells,
                avoid_dead_ends=True,
                allow=allow,
            )
        ):
            return ("MOVE", f"{alt.name} {detail_prefix} {goal}")
    # Last resort: ignore dead-end filter if goal requires it.
    next_cell = (pos[0] + direction.delta[0], pos[1] + direction.delta[1])
    if (
        _step_allowed(next_cell)
        and not _reverses_last_step(next_cell)
        and _try_move(
            unit, direction, pos, obstacle_cells,
        )
    ):
        _set_unit_route(unit, goal, [pos, goal], complete=False)
        return ("MOVE", f"{direction.name} {detail_prefix} {goal}")
    for alt in alternates:
        if alt == direction:
            continue
        alt_cell = (pos[0] + alt.delta[0], pos[1] + alt.delta[1])
        if (
            _step_allowed(alt_cell)
            and not _reverses_last_step(alt_cell)
            and _try_move(unit, alt, pos, obstacle_cells)
        ):
            return ("MOVE", f"{alt.name} {detail_prefix} {goal}")
    return None


def _long_march_step(
    unit: Any,
    pos: tuple[int, int],
    goal: tuple[int, int],
    obstacle_cells: frozenset[tuple[int, int]],
    *,
    detail_prefix: str,
    cell_counts: Mapping | None = None,
    extra_obstacles: Iterable[tuple[int, int]] = (),
) -> tuple[str, str] | None:
    """Distance-independent local march: one step toward ``goal``, any axis.

    A* pathing (``_move_towards``) has a bounded expansion budget; a goal
    thousands of cells away exceeds it every Tick and the unit stalls forever
    (observed: attack-team units scattered up to 11k cells from the target
    sat at ``attack-blocked`` for hours with no warn row at all). This helper
    replaces pathing with a plain greedy step: rank the four neighbours by
    Manhattan distance to the goal and take the best walkable one, so a unit
    trapped far from home keeps making progress and the budget-free home
    march eventually brings it back. Walls, enemy/packed cells and dead-end
    pockets are still filtered; returns None only when fully boxed in.
    """
    best: Direction | None = None
    best_dist: int | None = None
    blocked = frozenset(extra_obstacles)
    uid = str(unit.id)
    last = _worker_last_pos.get(uid)
    # Same cycle-blindness the explore fan-out had: one step of back-track
    # memory cannot break a 2x2 box. The march just walks the perimeter and the
    # next Tick the "best distance" neighbour is the cell it came through two
    # steps ago (observed after the rally-home deploy: W21 at (732,-681) and
    # W14 at (1425,-237) each spent 81 Ticks on 4 cells with net displacement 0
    # while supposedly marching 486 / 757 cells home). Nudge by visit age so a
    # fresh cell wins the tie, and record the step into the same tail.
    visited_age = _worker_explore_ages(uid)
    # Three escalating passes: (1) the normal step — no backtrack, no dead-end
    # cells; (2) escape pass — allow dead-end cells, because a unit that is
    # already INSIDE a pocket can only leave through its flagged mouth (this
    # trapped the regroup march in a 2x2 box, cycling forever); (3) last
    # resort — also allow stepping back the way it came.
    # Pass 1 also escalates when its best step is ground this unit already
    # walked: a pocket's INTERIOR cells are ordinary cells, so pass 1 always
    # finds something and the mouth in pass 2 was only ever reached when pass 1
    # came back empty (observed: V8 at (839,-1656), 1568 cells from home, circling
    # 3 cells with net 0 while marching toward its own patrol slot).
    fallback: tuple[Direction, tuple[int, int]] | None = None
    for allow_dead_end, allow_backtrack in ((False, False), (True, False), (True, True)):
        best = None
        best_dist = None
        best_cell: tuple[int, int] | None = None
        for direction in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT):
            npos = (pos[0] + direction.delta[0], pos[1] + direction.delta[1])
            if npos in obstacle_cells or npos in blocked:
                continue
            if not allow_backtrack and last is not None and npos == last:
                continue
            if cell_counts is not None:
                if cell_counts.get(npos, 0) >= _CELL_UNIT_LIMIT:
                    continue
            if not allow_dead_end and _is_dead_end_step(npos, obstacle_cells):
                continue
            dist = _manhattan(npos, goal)
            age = visited_age.get(npos)
            if age is not None:
                dist += 3 if age < 6 else 1
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best = direction
                best_cell = npos
        if best is not None and fallback is None:
            fallback = (best, best_cell)
        if best is None:
            continue
        if not allow_dead_end and visited_age.get(best_cell) is not None:
            # Best ordinary step is ground already walked: this unit is circling
            # inside a pocket, so reach for the dead-end mouth instead of
            # breaking out of the pass ladder here.
            continue
        break
    else:
        # Every escalation pass came back empty — keep the normal step rather
        # than freezing a unit that did have somewhere to go.
        if fallback is not None:
            best, best_cell = fallback
    if best is None:
        return None
    unit.move(best)
    _worker_last_pos[uid] = pos
    _worker_remember_explore(uid, pos)
    return ("MOVE", f"{best.name} {detail_prefix} {goal}")


def _regroup_needs_core_cell(unit: Any, config: Mapping) -> bool:
    """True when a regrouping unit still has a reason to stand on the core
    cell itself (healing); False when stopping at the ring loses nothing."""
    return bool(config.get("heal_enabled", True)) and int(
        getattr(unit, "hp", 0) or 0
    ) < _unit_max_hp(unit)


def _attack_regroup_step(
    unit: Any,
    pos: tuple[int, int],
    core_pos: tuple[int, int],
    obstacle_cells: frozenset[tuple[int, int]],
    *,
    cell_counts: Mapping | None = None,
    config: Mapping | None = None,
    enemies: tuple = (),
    unit_kind: str = "ranger",
) -> tuple[str, str] | None:
    """One step of the attack-team home-recovery march toward the Core.

    Pure greedy orbits around wall clusters forever (observed: R4 circled a
    2x2 box beside a diagonal wall line — every westward cell walled, so the
    distance-ranked greedy lapped the box with no escape). Instead run a real
    A* with a budget scaled to the distance; the found path is cached per
    unit+goal so the one-off cost amortises across Ticks. The budget must be
    generous: A* fills the equal-f plateau around concave wall clusters near
    the start, needing ~40-60x the Manhattan distance in expansions even for
    a near-straight path (observed: a 11539-cell beeline required ~500k
    expansions; 4x distance failed). The all-direction greedy step remains
    the fallback when even the scaled budget finds no path (or the unit is
    boxed), and its anti-backtrack passes still beat waiting in place.
    """
    if (
        config is not None
        and _chute_in_demand
        and _manhattan(pos, core_pos) <= 1
        and not _regroup_needs_core_cell(unit, config)
    ):
        # Deposit-chute priority: with carriers hauling, the core cell belongs
        # to the delivery pipeline. A full-HP regroup unit stepping onto it
        # wins the server's cell race against the queued carrier every Tick
        # (observed: two full-HP attack units swap-occupied the chute 1588+
        # Ticks — zero deposits, spawn squatter-blocked, the whole economy
        # frozen). Stop at the ring; a wounded unit still claims the cell.
        unit.wait()
        _set_unit_route(unit, core_pos, [tuple(pos)], complete=True)
        return ("WAIT", f"attack-regroup-ring {core_pos}")
    # A visible enemy next to the unit is not in obstacle_cells, so A* happily
    # routes through its cell and the server rejects every step with
    # MOVE_DESTINATION_OCCUPIED. Production R11 sat at (-1056,980) for 130
    # Ticks next to a stationary enemy worker, re-issuing the same westward
    # move 122 times. Treat adjacent enemies as temporary obstacles; if the
    # unit is boxed with no way around, shoot/sweep the blocker instead of
    # walking into it forever.
    adjacent_enemies = tuple(
        enemy for enemy in enemies
        if _manhattan(pos, tuple(enemy.position)) == 1
    )
    blocked_cells = frozenset(
        tuple(enemy.position) for enemy in adjacent_enemies
    )
    budget = min(1_500_000, max(50_000, _manhattan(pos, core_pos) * 60))
    moved = _move_towards(
        unit,
        pos,
        core_pos,
        obstacle_cells,
        detail_prefix="attack-regroup",
        cell_counts=cell_counts,
        extra_obstacles=blocked_cells,
        max_steps=budget,
    )
    if moved is not None:
        return moved
    marched = _long_march_step(
        unit,
        pos,
        core_pos,
        obstacle_cells,
        detail_prefix="attack-regroup",
        cell_counts=cell_counts,
        extra_obstacles=blocked_cells,
    )
    if marched is not None:
        return marched
    if adjacent_enemies:
        if unit_kind == "vanguard":
            sweep = _vanguard_adjacent_sweep(unit, pos, adjacent_enemies)
            if sweep is not None:
                return ("SWEEP", f"attack-regroup-blocker {sweep[1]}")
        else:
            shot = _ranger_best_shot(
                unit,
                pos,
                adjacent_enemies,
                obstacle_cells,
                int((config or {}).get("ranger_attack_range", 3)),
                lead_fire_enabled=bool(
                    (config or {}).get("ranger_lead_fire_enabled", True)
                ),
            )
            if shot is not None:
                return ("SHOOT", f"attack-regroup-blocker {shot[1]}")
    return None


# ── sweep miss fuse ──────────────────────────────────────────────────────────
# A sweep resolves AFTER movement, so aiming at the cell an enemy occupies
# this Tick misses whenever that enemy steps aside. Production proved the cost:
# V15 stood at (889,-920) for ~2000 Ticks and issued 407 sweeps that hit 0
# targets — every Tick the planner re-aimed at the enemy's current cell and
# every resolution came back empty, while the enemy traded blows with it.
# The fuse counts one unit's consecutive zero-hit sweeps; once they prove the
# aim is wasted, current-cell sweeps are refused for _SWEEP_FUSE_TICKS so the
# predictive and movement branches take over instead. Any sweep that lands a
# target wipes the debt: this is about wasted aim, not about fighting.
_SWEEP_MISS_FUSE_LIMIT = 3
_SWEEP_FUSE_TICKS = 40
_sweep_miss_streak: dict[str, int] = {}
_sweep_fuse_until: dict[str, int] = {}


def _record_sweep_resolutions(turn: Any, enabled: bool = True) -> None:
    """Feed last Tick's SWEEP_RESOLVED outcomes into the miss fuse.

    Keys are full unit ids, never ``id[:8]``: display names get recycled when a
    unit dies and truncated ids have already killed one safety mechanism here.
    ``enabled`` comes from the dashboard switch; off wipes the ledger, because a
    fuse already armed would otherwise keep refusing swings the operator has
    decided to allow.
    """
    if not enabled:
        _sweep_miss_streak.clear()
        _sweep_fuse_until.clear()
        return
    tick = int(getattr(turn_context, "tick", 0) or 0)
    allies = {
        str(unit.id)
        for group in (
            getattr(turn, "workers", ()) or (),
            getattr(turn, "vanguards", ()) or (),
            getattr(turn, "rangers", ()) or (),
        )
        for unit in group
    }
    for event in getattr(turn, "events", ()) or ():
        if getattr(event, "event_type", "") != "SWEEP_RESOLVED":
            continue
        actor_id = getattr(event, "actor_id", None)
        if actor_id is None:
            continue
        uid = str(actor_id)
        if uid not in allies:
            # An enemy's wasted swings are none of our business — and recording
            # them would grow the ledger without bound on dead enemy ids.
            continue
        values = getattr(event, "values", None) or {}
        try:
            hit = int(values.get("targets_hit", 0) or 0)
        except (TypeError, ValueError):
            hit = 0
        if hit > 0:
            _sweep_miss_streak.pop(uid, None)
            _sweep_fuse_until.pop(uid, None)
            continue
        streak = _sweep_miss_streak.get(uid, 0) + 1
        if streak >= _SWEEP_MISS_FUSE_LIMIT:
            _sweep_miss_streak[uid] = 0
            _sweep_fuse_until[uid] = tick + _SWEEP_FUSE_TICKS
            print(
                f"[sweep-fuse] uid={uid} 连续 {streak} 次横扫空枪，"
                f"{_SWEEP_FUSE_TICKS} 拍内不再朝当前格横扫",
                flush=True,
            )
        else:
            _sweep_miss_streak[uid] = streak


def _sweep_fused(unit: Any) -> bool:
    """True while this unit's aim at an occupied cell is proven wasted."""
    expiry = _sweep_fuse_until.get(str(unit.id))
    if expiry is None:
        return False
    if int(getattr(turn_context, "tick", 0) or 0) >= expiry:
        _sweep_fuse_until.pop(str(unit.id), None)
        return False
    return True


def _sweep_fuse_note(unit: Any) -> str:
    """Self-report marker so the fuse is greppable in the Tick log."""
    return " sweep-fuse" if _sweep_fused(unit) else ""


def _vanguard_approach_for_sweep(
    unit: Any,
    pos: tuple[int, int],
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    cell_counts: Mapping | None = None,
) -> tuple[str, str] | None:
    """Step beside a diagonally-adjacent enemy so the sweep can land.

    `_vanguard_adjacent_sweep` only sweeps the four cardinal neighbours. A
    Vanguard that ends its move on a diagonal cell therefore sweeps empty air
    every Tick while the enemy sits safely on the corner. Observed
    t646403-t646407, V12 swept 0 targets three Ticks running: the target left
    the swept cell first each time (battle log), and a diagonal enemy cannot be
    swept at all until this step puts the Vanguard on a cardinal neighbour.

    Returns a one-step MOVE onto a free cardinal neighbour of the closest
    diagonal enemy, or None when no such enemy or no free slot exists. Enemy
    cells are hard-blocked: stepping onto one is rejected as MOVE_CONTESTED,
    so the approach must never aim at a cell another enemy occupies.
    """
    diagonal = [
        enemy
        for enemy in enemies
        if _chebyshev(pos, tuple(enemy.position)) == 1
        and _manhattan(pos, tuple(enemy.position)) != 1
    ]
    if not diagonal:
        return None
    target = min(diagonal, key=lambda e: _chebyshev(pos, tuple(e.position)))
    tx, ty = target.position
    # Same membership rule as _choose_actions (`enemy_cells`): enemies are not
    # in obstacle_cells, so they must be blocked explicitly.
    blocked = frozenset(obstacle_cells) | frozenset(
        tuple(enemy.position) for enemy in enemies
    )
    steps = (Direction.UP, Direction.DOWN, Direction.LEFT, Direction.RIGHT)
    for around in steps:
        cell = (tx + around.delta[0], ty + around.delta[1])
        if cell == pos or cell in blocked:
            continue
        if cell_counts is not None and cell_counts.get(cell, 0) >= _CELL_UNIT_LIMIT:
            continue
        delta = (cell[0] - pos[0], cell[1] - pos[1])
        for step in steps:
            if step.delta == delta:
                unit.move(step)
                return (
                    "MOVE",
                    f"{step.name} sweep-approach -> {target.position}",
                )
    return None


def _vanguard_adjacent_sweep(
    vanguard: Any,
    pos: tuple[int, int],
    enemies: tuple,
) -> tuple[str, str] | None:
    if _sweep_fused(vanguard):
        # Refuse the current-cell aim; the caller falls through to predictive
        # aiming or closes the distance, which is the point of the fuse.
        return None
    for direction in (Direction.UP, Direction.DOWN, Direction.LEFT, Direction.RIGHT):
        tx = pos[0] + direction.delta[0]
        ty = pos[1] + direction.delta[1]
        for enemy in enemies:
            if tuple(enemy.position) == (tx, ty):
                vanguard.sweep(direction)
                return ("SWEEP", f"{direction.name} -> enemy at {enemy.position}")
    return None


def _update_enemy_motion_tracks(enemies: Iterable[Any], tick: int) -> None:
    """Keep at most four consecutive observations for each visible enemy."""
    for enemy in enemies:
        enemy_id = str(enemy.id)
        position = tuple(enemy.position)
        history = _enemy_motion_tracks.get(enemy_id, [])
        if history and history[-1][0] == tick:
            history[-1] = (tick, position)
        elif history and history[-1][0] == tick - 1:
            history.append((tick, position))
        else:
            history = [(tick, position)]
        _enemy_motion_tracks[enemy_id] = history[-_ENEMY_MOTION_HISTORY:]

    for enemy_id, history in list(_enemy_motion_tracks.items()):
        if not history or tick - history[-1][0] > 4:
            _enemy_motion_tracks.pop(enemy_id, None)


def _motion_streak(
    history: list[tuple[int, tuple[int, int]]],
) -> tuple[tuple[int, int] | None, int]:
    """Return the latest velocity and its consecutive repetition count."""
    if len(history) < 2:
        return None, 0
    latest = history[-1][1]
    previous = history[-2][1]
    velocity = latest[0] - previous[0], latest[1] - previous[1]
    if abs(velocity[0]) + abs(velocity[1]) != 1:
        return velocity, 0

    streak = 1
    for index in range(len(history) - 2, 0, -1):
        current = history[index][1]
        earlier = history[index - 1][1]
        if (current[0] - earlier[0], current[1] - earlier[1]) != velocity:
            break
        streak += 1
    return velocity, streak


def _stationary_streak(
    history: list[tuple[int, tuple[int, int]]],
) -> int:
    """Return the number of latest consecutive zero-velocity steps."""
    streak = 0
    for index in range(len(history) - 1, 0, -1):
        if history[index][1] != history[index - 1][1]:
            break
        streak += 1
    return streak


def _shadow_shot_prediction(
    ranger: Any,
    ranger_pos: tuple[int, int],
    target: Any,
    obstacle_cells: frozenset[tuple[int, int]],
    attack_range: int,
) -> dict[str, Any]:
    """Describe a lead-shot candidate before the caller queues the shot."""
    target_id = str(target.id)
    current_cell = tuple(target.position)
    history = _enemy_motion_tracks.get(target_id, [])
    velocity, move_streak = _motion_streak(history)
    stationary_streak = _stationary_streak(history)
    predicted_cell: tuple[int, int] | None = None
    legal = False
    reason = "insufficient_history"
    motion_state = "insufficient"

    if velocity is not None:
        if velocity == (0, 0):
            if stationary_streak >= _STATIONARY_STREAK:
                motion_state = "stationary"
                reason = "stationary"
            else:
                motion_state = "uncertain"
                reason = "stationary_unconfirmed"
        elif abs(velocity[0]) + abs(velocity[1]) != 1:
            motion_state = "uncertain"
            reason = "invalid_velocity"
        else:
            motion_state = (
                "moving_stable"
                if move_streak >= _STABLE_MOVE_STREAK
                else "moving_unstable"
            )
            predicted_cell = (
                current_cell[0] + velocity[0],
                current_cell[1] + velocity[1],
            )
            dx = predicted_cell[0] - ranger_pos[0]
            dy = predicted_cell[1] - ranger_pos[1]
            distance = max(abs(dx), abs(dy))
            if not 1 <= distance <= attack_range:
                reason = "out_of_range"
            elif not (dx == 0 or dy == 0 or abs(dx) == abs(dy)):
                reason = "invalid_line"
            elif _line_blocked(ranger_pos, predicted_cell, obstacle_cells):
                reason = "blocked"
            else:
                legal = True
                reason = (
                    "eligible"
                    if move_streak >= _STABLE_MOVE_STREAK
                    else "unstable_velocity"
                )

    return {
        "tick": turn_context.tick,
        "ranger_id": str(ranger.id)[:8],
        "ranger_name": _object_name(ranger.id, "R"),
        "target_id": target_id[:8],
        "target_name": _object_name(target.id, "E"),
        "target_type": _enemy_unit_type_name(target) or "ENEMY",
        "current_cell": list(current_cell),
        "predicted_cell": list(predicted_cell) if predicted_cell else None,
        "velocity": list(velocity) if velocity is not None else None,
        "move_streak": move_streak,
        "stationary_streak": stationary_streak,
        "motion_state": motion_state,
        "prediction_legal": legal,
        "eligible": legal and move_streak >= _STABLE_MOVE_STREAK,
        "reason": reason,
        "_ranger_key": str(ranger.id),
        "_target_key": target_id,
    }


def _resolve_shadow_predictions(turn: Any, tick: int) -> list[dict[str, Any]]:
    """Resolve committed shadow candidates against the following Tick."""
    global _pending_shot_predictions
    if not _pending_shot_predictions:
        return []

    event_by_actor = {
        str(event.actor_id): str(event.event_type)
        for event in getattr(turn, "events", ()) or ()
        if getattr(event, "actor_id", None)
        and str(getattr(event, "event_type", "")) in ("SHOT_HIT", "SHOT_MISSED")
    }
    enemy_positions = {
        str(enemy.id): tuple(enemy.position)
        for enemy in getattr(turn, "visible_enemies", ()) or ()
    }
    resolved: list[dict[str, Any]] = []
    for pending in _pending_shot_predictions:
        public = {
            key: value for key, value in pending.items() if not key.startswith("_")
        }
        tick_gap = tick - int(pending.get("tick", tick))
        actual = (
            enemy_positions.get(pending["_target_key"])
            if tick_gap == 1
            else None
        )
        predicted = pending.get("predicted_cell")
        current = pending.get("current_cell")
        fired = pending.get("fired_cell") or current
        public.update({
            "resolved_tick": tick,
            "tick_gap": tick_gap,
            "actual_cell": list(actual) if actual is not None else None,
            "shot_result": (
                event_by_actor.get(pending["_ranger_key"], "UNRESOLVED")
                if tick_gap == 1
                else "UNRESOLVED"
            ),
            "predicted_match": (
                actual == tuple(predicted) if actual is not None and predicted else None
            ),
            "current_match": (
                actual == tuple(current) if actual is not None and current else None
            ),
            "fired_match": (
                actual == tuple(fired) if actual is not None and fired else None
            ),
        })
        resolved.append(public)
    _pending_shot_predictions = []
    return resolved


def _commit_shadow_predictions(accepted: bool) -> None:
    """Commit only predictions from an accepted plan for next-Tick resolution."""
    global _pending_shot_predictions
    if not accepted:
        return
    committed = [dict(item) for item in turn_context.shot_predictions]
    _pending_shot_predictions.extend(committed)
    game_stats.record_prediction_candidates(_game_stats, committed)


def _enemy_motion_tier(enemy: Any) -> int:
    """Hit-prospect tier for a shot at the enemy's CURRENT cell.

    Live fire control showed stationary targets hit ~96% while moving ones hit
    ~3% (a moving unit is barely hittable at its current cell). Rank still
    targets first (0), unknown history next (1), clearly moving last (2), so a
    Ranger spends its shot on something it can actually hit instead of the
    nearest mover.
    """
    history = _enemy_motion_tracks.get(str(getattr(enemy, "id", id(enemy))), [])
    velocity, _ = _motion_streak(history)
    if velocity is None:
        return 1
    if velocity == (0, 0):
        return 0
    if abs(velocity[0]) + abs(velocity[1]) == 1:
        return 2
    return 1


def _ranger_best_shot(
    ranger: Any,
    pos: tuple[int, int],
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    attack_range: int,
    *,
    lead_fire_enabled: bool = False,
) -> tuple[str, str] | None:
    """Pick the best enemy in legal 8-way range (rules v0.8/v0.13).

    Distance is Chebyshev — horizontal, vertical, or exact-diagonal offsets
    where max(|dx|, |dy|) is within 1..attack_range. Diagonal shots are legal
    and only intermediate crossed cells can block the line. Among legal targets
    a still one outranks a mover (current-cell hits on movers are ~3%), with
    distance breaking ties inside the same motion tier.
    """
    best_key = (1 << 30, 1 << 30)
    best_dist = 10_000
    best_target = None
    for enemy in enemies:
        dx = int(enemy.position[0]) - pos[0]
        dy = int(enemy.position[1]) - pos[1]
        dist = max(abs(dx), abs(dy))
        if not (1 <= dist <= attack_range):
            continue
        if _line_blocked(pos, tuple(enemy.position), obstacle_cells):
            continue
        key = (_enemy_motion_tier(enemy), dist)
        if key < best_key:
            best_key = key
            best_dist = dist
            best_target = enemy
    if best_target is None:
        return None
    prediction = _shadow_shot_prediction(
        ranger,
        pos,
        best_target,
        obstacle_cells,
        attack_range,
    )
    predicted_cell = prediction.get("predicted_cell")
    target_is_worker = prediction["target_type"] == "WORKER"
    lead_already_claimed = any(
        item.get("lead_fire_used")
        and item.get("_target_key") == prediction.get("_target_key")
        for item in turn_context.shot_predictions
    )
    lead_fire_used = bool(
        lead_fire_enabled
        and target_is_worker
        and prediction.get("eligible")
        and predicted_cell
        and not lead_already_claimed
    )
    fired_cell = predicted_cell if lead_fire_used else prediction["current_cell"]
    prediction.update({
        "lead_fire_used": lead_fire_used,
        "lead_fire_rejection": (
            "target_type"
            if not target_is_worker
            else "target_claimed"
            if lead_already_claimed
            else None
        ),
        "fire_mode": "lead" if lead_fire_used else "current",
        "fired_cell": list(fired_cell),
    })
    turn_context.shot_predictions.append(prediction)

    if lead_fire_used:
        ranger.shoot(best_target, expected_cell=tuple(predicted_cell))
        return (
            "SHOOT",
            f"lead {tuple(predicted_cell)} enemy at {best_target.position} "
            f"dist={best_dist}",
        )

    ranger.shoot(best_target)
    return ("SHOOT", f"current enemy at {best_target.position} dist={best_dist}")


def _ranger_low_hp_kite(
    ranger: Any,
    pos: tuple[int, int],
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    cell_counts: Mapping | None = None,
) -> tuple[str, str] | None:
    """Make a 1-HP Ranger inside knife range move instead of trading shots.

    Game rule (player-confirmed, see `_worker_flee`): a moving unit is never
    hit; only a stationary target takes damage. A Ranger at 1 HP that fires
    from inside Chebyshev 2 is therefore a free kill -- it stands still for a
    full Tick next to an enemy that kills it. Observed t646411-t646423: R18
    held (-660,-107) at hp1 and fired seven Ticks in a row, then died.

    Returns a MOVE away from the nearest close threat, or None when the Ranger
    is not in danger (no combat enemy within Chebyshev 2) or every escape cell
    is blocked. Workers and enemy Cores cannot hurt it, so they never trigger
    the kite: at t646421 R18 at 1 HP fled from E250, a WORKER two cells away.
    """
    if getattr(ranger, "unit_type", None) != UnitType.RANGER:
        return None
    hp = getattr(ranger, "hp", None)
    if hp is None or int(hp) > 1:
        return None
    threats = [
        enemy for enemy in enemies
        if _is_combat_threat(enemy)
        and _chebyshev(pos, tuple(enemy.position)) <= 2
    ]
    if not threats:
        return None
    nearest = min(threats, key=lambda e: _chebyshev(pos, tuple(e.position)))
    blocked = frozenset(tuple(enemy.position) for enemy in enemies)
    step = _retreat_from(
        pos, tuple(nearest.position), obstacle_cells, blocked, cell_counts,
    )
    if step is None:
        return None
    ranger.move(step)
    return ("MOVE", f"{step.name} low-hp-kite hp={hp}")


def _scout_cardinal(
    unit: Any,
    pos: tuple[int, int],
    obstacle_cells: frozenset[tuple[int, int]],
    config: dict[str, Any],
    *,
    label: str,
    avoid_cells: Iterable[tuple[int, int]] = (),
) -> tuple[str, str]:
    prev = _worker_last_pos.get(str(unit.id))
    avoided = frozenset(avoid_cells)
    idx = _stable_slot_index(str(unit.id), 4)
    base = [Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT]
    rotated = base[idx:] + base[:idx]

    def _scout_key(d: Direction) -> int:
        npos = (pos[0] + d.delta[0], pos[1] + d.delta[1])
        score = 0
        if _is_dead_end_step(npos, obstacle_cells):
            score += 2
        if (
            config.get("avoid_backtracking")
            and prev
            and npos == prev
        ):
            score += 1
        return score

    rotated.sort(key=_scout_key)
    for direction in rotated:
        npos = (pos[0] + direction.delta[0], pos[1] + direction.delta[1])
        if npos in avoided:
            continue
        if _try_move(
            unit,
            direction,
            pos,
            obstacle_cells,
            avoid_dead_ends=True,
        ):
            _set_unit_route(unit, npos, [pos, npos], complete=False)
            return ("MOVE", f"{direction.name} {label}")
    # If every open neighbor is a dead end, take the least-bad exit rather than wait.
    # This pass ignores `avoided` on purpose: during a roam-oscillation escape the
    # blacklist is the whole bounce region, so in a one-cell nub the only exit back
    # through it IS the exit (observed: V7 wedged 5 straight WAIT:no_way at
    # (1890,926) with (1890,925) blacklisted and nowhere else to go). A standing
    # unit takes damage and advances nothing, which is strictly worse than
    # re-entering the region — the escape state escalates the heading instead.
    for direction in rotated:
        npos = (pos[0] + direction.delta[0], pos[1] + direction.delta[1])
        if _try_move(unit, direction, pos, obstacle_cells):
            _set_unit_route(unit, npos, [pos, npos], complete=False)
            return ("MOVE", f"{direction.name} {label}")
    unit.wait()
    return ("WAIT", "no_way")


def _stable_slot_index(text: str, modulo: int) -> int:
    """Restart-safe slot pick: builtin hash() is salted per process
    (PYTHONHASHSEED), which reshuffled patrol slots on every restart."""
    digest = hashlib.md5(str(text).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % modulo


def _home_patrol_slots(radius: int) -> tuple[tuple[int, int], ...]:
    """Return the perimeter slot offsets for a given patrol radius."""
    radius = max(1, int(radius))
    if radius >= 2:
        # Eight slots around the ring keep multiple defenders spread out.
        # Axial slots sit exactly `radius` out; diagonal slots are pulled in
        # to Manhattan distance `radius` (ceil/floor split) so defenders can
        # actually reach and hold them — the old (±r, ±r) corners sat at 2r,
        # past the return_radius=r+1 line, ping-ponging home-return/patrol.
        half = radius - radius // 2  # ceil(radius/2)
        other = radius // 2          # floor(radius/2)
        return (
            (0, -radius),
            (half, -other),
            (radius, 0),
            (half, other),
            (0, radius),
            (-half, other),
            (-radius, 0),
            (-half, -other),
        )
    # Radius 1 has only 4 cells within distance 1; keep the full 8-slot
    # ring (corners at distance 2) for spread.
    return (
        (0, -radius),
        (radius, -radius),
        (radius, 0),
        (radius, radius),
        (0, radius),
        (-radius, radius),
        (-radius, 0),
        (-radius, -radius),
    )


def _home_patrol_slot_assignments(
    home_names: Iterable[str],
    radius: int,
) -> dict[str, int]:
    """Distribute home team names across patrol slots as evenly as possible.

    Each server cell can stack up to ``_CELL_UNIT_LIMIT`` friendly units, so
    we allow that many defenders per slot before spilling to the next slot.
    This avoids the hash-collision case where several units map to the same
    of the 8 slots and perpetually CELL_UNIT_LIMIT each other.
    """
    cache_key = (int(radius), tuple(sorted(home_names, key=lambda n: n.upper())))
    cached = _home_patrol_slot_cache.get(cache_key)
    if cached is not None:
        return cached

    slots = _home_patrol_slots(radius)
    sorted_names = sorted(home_names, key=lambda n: n.upper())
    loads = [0] * len(slots)
    assignments: dict[str, int] = {}
    for name in sorted_names:
        # Pick the least-loaded slot; cap each slot at the per-cell unit limit
        # so stacked pairings are intentional rather than accidental collisions.
        best = min(
            range(len(slots)),
            key=lambda i: (loads[i] if loads[i] < _CELL_UNIT_LIMIT else 10_000, i),
        )
        assignments[name] = best
        loads[best] += 1
    _home_patrol_slot_cache[cache_key] = assignments
    return assignments


def _home_patrol_goal(
    unit_id: str,
    core_pos: tuple[int, int],
    radius: int,
    *,
    preferred_slot: int | None = None,
) -> tuple[int, int]:
    """Pick a stable perimeter offset around the Core for this unit."""
    slots = _home_patrol_slots(radius)
    if preferred_slot is None:
        slot = slots[_stable_slot_index(unit_id, len(slots))]
    else:
        slot = slots[preferred_slot % len(slots)]
    return (core_pos[0] + slot[0], core_pos[1] + slot[1])


def _home_safe_approach(
    unit: Any,
    pos: tuple[int, int],
    goal: tuple[int, int],
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    ranger_range: int,
    cell_counts: Mapping | None,
    prefix: str,
) -> tuple[str, str] | None:
    """Fire-aware approach step for a home defender that cannot attack yet.

    Production 2026-11-23: 11 of 32 friendly hits were home/attack units
    stepping straight down a Ranger's fire line (``home-intercept``) while a
    neighbour off every fire line existed. Only engages when the direct
    approach is actually exposed: then pick the free neighbour with the least
    fire cover, breaking ties by distance to ``goal``. Returns None when the
    current situation is not exposed (caller keeps its normal march).
    """
    threats = _combat_threats(enemies)
    if not threats:
        return None
    if min(_chebyshev(pos, tuple(e.position)) for e in threats) > ranger_range + 2:
        return None
    enemy_cells = {tuple(e.position) for e in enemies}
    options: list[tuple[int, int, int, Direction, tuple[int, int]]] = []
    for order, d in enumerate(
        (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT)
    ):
        npos = (pos[0] + d.delta[0], pos[1] + d.delta[1])
        if npos in obstacle_cells or npos in enemy_cells:
            continue
        if cell_counts is not None and cell_counts.get(npos, 0) >= _CELL_UNIT_LIMIT:
            continue
        cover = _cell_fire_cover(npos, threats, obstacle_cells, ranger_range)
        options.append((cover, _manhattan(npos, goal), order, d, npos))
    if not options:
        return None
    options.sort()
    best_cover, _dist, _order, best_dir, best_cell = options[0]
    # Greedy direct step (what _move_towards would usually take) — only
    # override it when it is exposed and a strictly safer step exists.
    direct = min(options, key=lambda o: (o[1], o[2]))
    if direct[0] == 0 or best_cover >= direct[0]:
        return None
    unit.move(best_dir)
    _set_unit_route(unit, goal, [pos, best_cell], complete=False)
    return ("MOVE", f"{best_dir.name} {prefix}-safe cover={best_cover}/{direct[0]}")


def _plan_home_combat(
    unit: Any,
    *,
    unit_kind: str,
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    core_pos: tuple[int, int],
    config: dict[str, Any],
    cell_counts: Mapping | None = None,
) -> tuple[str, str]:
    """Defend near the Core: engage nearby threats, otherwise patrol the ring."""
    pos = tuple(unit.position)
    radius = int(config["home_patrol_radius"])
    # Hysteresis: only force a return when clearly outside the ring. A one-cell
    # band stops home-return / home-patrol A-B-A flipping on the perimeter.
    return_radius = radius + 1
    # Engage radius lets defenders sally out past the patrol ring to drive off
    # threats (0 = disabled, falls back to the legacy return_radius chase).
    # Initialized before every branch so no path hits an unbound name.
    engage_radius = max(int(config.get("home_engage_radius", 10) or 0), return_radius)

    # Prolonged delivery-ring freeze escape: a defender stuck on the same ring
    # cell for _HOME_CHUTE_WAIT_ESCAPE Ticks stops yielding to carriers and
    # pathfinds out to its patrol slot (see _home_chute_wait). The counter resets
    # as soon as the defender stands somewhere else.
    _uid = str(unit.id)
    _cw = _home_chute_wait.get(_uid)
    if _cw is not None and _cw[0] != pos:
        _home_chute_wait.pop(_uid, None)
        _cw = None
    _chute_escape = _cw is not None and _cw[1] >= _HOME_CHUTE_WAIT_ESCAPE

    if unit_kind == "vanguard":
        # Sweep first: an adjacent enemy is free damage this Tick. Approach
        # only when no cardinal target exists (see _plan_waypoint).
        sweep = _vanguard_adjacent_sweep(unit, pos, enemies)
        if sweep is not None:
            return sweep
        approach = _vanguard_approach_for_sweep(
            unit, pos, enemies, obstacle_cells, cell_counts,
        )
        if approach is not None:
            return approach
    else:
        kite = _ranger_low_hp_kite(unit, pos, enemies, obstacle_cells, cell_counts)
        if kite is not None:
            return kite
        shot = _ranger_best_shot(
            unit,
            pos,
            enemies,
            obstacle_cells,
            int(config["ranger_attack_range"]),
            lead_fire_enabled=bool(config.get("ranger_lead_fire_enabled", True)),
        )
        if shot is not None:
            return shot

    # Chase enemies inside the engage radius. Hysteresis (same one-cell-band
    # pattern as return_radius above): a pursued target only drops once it
    # clears engage_radius + 1, so defenders don't A-B-A flip on the ring
    # perimeter. Inner threats take priority over the borderline straggler.
    local_enemies = [
        enemy for enemy in enemies
        if _manhattan(core_pos, enemy.position) <= engage_radius
    ]
    if not local_enemies and engage_radius > return_radius:
        local_enemies = [
            enemy for enemy in enemies
            if _manhattan(core_pos, enemy.position) <= engage_radius + 1
        ]
    # Economic defence: campers threatening a mine or a recent death cell get
    # intercepted even far outside the patrol ring. The home squad used to idle
    # on the delivery ring while workers were killed at a mine 60+ cells away
    # (engage_radius only measured from the Core). resource_assign caps these
    # targets to the mining radius so defenders do not chase across the map.
    defense = getattr(turn_context, "defense_targets", ())
    if defense:
        # Leash the far-away economic-defence chase. `defense_targets` exists
        # so campers threatening a mine are answered at the mine, but it used
        # to ignore distance entirely: defenders walked 45+ cells to a death
        # hotspot and the fight was decided before they arrived (observed
        # t646408: V1 at (-636,-86) ordered to (-660,-107), 45 Manhattan,
        # while the last death landed 5 Ticks later at t646433). 0 disables.
        leash = max(int(config.get("home_defense_leash", 12) or 0), 0)
        _seen = {id(enemy) for enemy in local_enemies}
        kept_defense = []
        dropped_defense = []
        for enemy in defense:
            if id(enemy) in _seen:
                continue
            if leash <= 0 or _manhattan(pos, enemy.position) <= leash:
                kept_defense.append(enemy)
            else:
                dropped_defense.append(enemy)
        local_enemies.extend(kept_defense)
        store = getattr(turn_context, "home_defense_decisions", None)
        if store is not None and (kept_defense or dropped_defense):
            store[str(unit.id)] = {
                "tick": int(getattr(turn_context, "tick", 0) or 0),
                "unit": _object_name(
                    unit.id, "V" if unit_kind == "vanguard" else "R",
                ),
                "leash": leash,
                "kept": len(kept_defense),
                "dropped": len(dropped_defense),
                "target": (
                    list(kept_defense[0].position) if kept_defense else None
                ),
            }
    if local_enemies:
        # A fresh visible threat supersedes any remembered chase target.
        _home_engage_target.pop(str(unit.id), None)
        nearest = min(local_enemies, key=lambda e: _manhattan(pos, e.position))
        # Target lock: remember this foe so a brief vision loss (a tick or
        # two) does not flip the defender straight back into patrol.
        engage_memory_ticks = max(
            int(config.get("home_engage_memory_ticks", 4) or 0), 0,
        )
        if engage_memory_ticks > 0:
            _home_engage_target[str(unit.id)] = (
                str(nearest.id), turn_context.tick,
            )
        prefix = (
            "home-engage"
            if _manhattan(core_pos, nearest.position) <= return_radius
            else "home-intercept"
        )
        safe = _home_safe_approach(
            unit, pos, tuple(nearest.position), enemies, obstacle_cells,
            int(config.get("ranger_attack_range", 3)), cell_counts, prefix,
        )
        if safe is not None:
            return safe
        moved = _move_towards(
            unit,
            pos,
            tuple(nearest.position),
            obstacle_cells,
            detail_prefix=prefix,
            cell_counts=cell_counts,
        )
        if moved is not None:
            return moved
    else:
        # Target lock fallback: the chased enemy slipped out of vision — keep
        # marching on its last-known position (from the motion tracks) for
        # home_engage_memory_ticks instead of A-B-A flipping into patrol.
        remembered = _home_engage_target.get(str(unit.id))
        if remembered is not None:
            enemy_id, last_seen = remembered
            engage_memory_ticks = max(
                int(config.get("home_engage_memory_ticks", 4) or 0), 0,
            )
            track = _enemy_motion_tracks.get(enemy_id)
            last_pos = track[-1][1] if track else None
            stale = (
                engage_memory_ticks <= 0
                or turn_context.tick - last_seen > engage_memory_ticks
                or last_pos is None
                or _manhattan(core_pos, last_pos) > engage_radius + 1
            )
            if stale:
                _home_engage_target.pop(str(unit.id), None)
            elif pos == last_pos:
                # Caught up with the remembered foe: treat the chase as done
                # so no single-cell A-B-A remains against home-return.
                _home_engage_target.pop(str(unit.id), None)
            else:
                # Delivery pipeline first: on the core ring while carriers
                # queue, yield to chute-clear — the lock survives the yield
                # and the chase resumes next tick while memory holds.
                if not (_chute_in_demand and _manhattan(pos, core_pos) == 1):
                    moved = _move_towards(
                        unit,
                        pos,
                        last_pos,
                        obstacle_cells,
                        detail_prefix="home-intercept",
                        cell_counts=cell_counts,
                    )
                    if moved is not None:
                        return moved

    dist_home = _manhattan(pos, core_pos)
    # The unloading ring belongs to the delivery pipeline while carriers are
    # queued: chute-clear below evicts anyone standing on it, so the march must
    # also route around it. Without the lock a defender whose slot lies on the
    # far side re-enters the ring every Tick and the two rules trade turns
    # forever (observed V4, 100% of Ticks: ``UP chute-clear`` / ``DOWN
    # home-patrol`` between (682,-301) and (682,-302)).
    # A defender standing on the Core is the one case the lock must not apply:
    # its four exits are exactly the locked cells, so it would freeze there for
    # the whole delivery window. Letting it out is self-limiting -- once it is
    # on a cardinal, chute-clear evicts it outward and the lock holds it there.
    _chute_ring = (
        frozenset({
            core_pos,
            (core_pos[0] + 1, core_pos[1]), (core_pos[0] - 1, core_pos[1]),
            (core_pos[0], core_pos[1] + 1), (core_pos[0], core_pos[1] - 1),
        })
        if _chute_in_demand and dist_home > 0
        else frozenset()
    )

    unit_name = _object_name(
        unit.id, _UNIT_NAME_PREFIX.get(getattr(unit, "unit_type", None), "U")
    )
    home_names = _parse_team_names(config.get("home_team", ""))
    slot_assignments = _home_patrol_slot_assignments(home_names, radius)
    goal = _home_patrol_goal(
        str(unit.id),
        core_pos,
        radius,
        preferred_slot=slot_assignments.get(unit_name),
    )
    # Home-return aims at the defender's own slot, not at the Core cell, so the
    # two home rules share one destination. Two goals meant a patrol detour
    # that closed in on the slot while gaining a cell of distance-to-Core
    # crossed the return_radius band and was dragged straight back (observed on
    # the live map: (684,-298) <-> (684,-299) on 100% of Ticks, with and without
    # chute demand). The Core cell is also the chute the carriers need.
    if dist_home > return_radius:
        # _move_towards' greedy fallback and _long_march_step can undo each
        # other in a 2x2 pocket: the former steps toward the goal, the latter
        # steps out, and the pair loops forever. Production R19 spent 300+
        # Ticks in a 2x2 pocket 398 cells from home. Once the recent trail
        # proves a tight loop, let the loop-aware march pick first; it already
        # leans on visit age to leave the pocket.
        if _is_tight_loop(str(unit.id)):
            marched = _long_march_step(
                unit,
                pos,
                goal,
                obstacle_cells,
                detail_prefix="home-return",
                cell_counts=cell_counts,
                extra_obstacles=_chute_ring,
            )
            if marched is not None:
                return marched
        moved = _move_towards(
            unit,
            pos,
            goal,
            obstacle_cells,
            detail_prefix="home-return",
            cell_counts=cell_counts,
            extra_obstacles=_chute_ring,
        )
        if moved is not None:
            return moved
        # Pathing has a bounded expansion budget, so a defender stranded far
        # outside the ring misses every Tick and used to fall through to the
        # home-patrol scout step below. That scout step shuffled one cell out on
        # the next Tick's march right back (observed after the 2026-10-10 deploy:
        # V8 at (839,-1656), 1420 cells from home, 3 distinct cells and 98%
        # revisits over 130 Ticks, alternating UP/RIGHT home-patrol with
        # DOWN/LEFT home-return). Same answer the attack march uses: a
        # budget-free greedy step toward the slot.
        marched = _long_march_step(
            unit,
            pos,
            goal,
            obstacle_cells,
            detail_prefix="home-return",
            cell_counts=cell_counts,
            extra_obstacles=_chute_ring,
        )
        if marched is not None:
            return marched

    # Delivery pipeline: the cells adjacent to the chute are the next-in-line
    # pre-position slots. While carriers queue, a defender camped there blocks
    # the one-out-one-in hand-off, so take one step outward. Pre-check the
    # target cell's occupancy first: pushing into an already-packed cell
    # (server limit _CELL_UNIT_LIMIT) is rejected every tick and the defender
    # keeps hammering the same dead move (observed in the core-ring deadlock).
    if _chute_in_demand and dist_home == 1:
        _clear_counts = cell_counts or {}
        for d in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT):
            npos = (pos[0] + d.delta[0], pos[1] + d.delta[1])
            if npos in obstacle_cells:
                continue
            if _manhattan(npos, core_pos) <= dist_home:
                continue
            if _clear_counts.get(npos, 0) >= _CELL_UNIT_LIMIT:
                continue
            unit.move(d)
            _set_unit_route(unit, npos, [pos, npos], complete=False)
            return ("MOVE", f"{d.name} chute-clear")
        # Every outward cell is at capacity: waiting one tick is better than
        # re-issuing the same blocked move and logging another CELL_UNIT_LIMIT.
        # After a prolonged freeze, fall through to patrol instead of yielding
        # forever (the ring stays packed with a large worker fleet).
        _home_chute_wait[_uid] = (pos, (_cw[1] + 1) if _cw is not None else 1)
        if not _chute_escape:
            unit.wait()
            return ("WAIT", "packed-outward-wait")

    # Already on/near the assigned slot: hold instead of micro-stepping.
    if _manhattan(pos, goal) <= 1 and dist_home <= return_radius and not _chute_escape:
        unit.wait()
        _set_unit_route(unit, goal, [pos], complete=True)
        return ("WAIT", f"home-hold {goal}")
    if (
        _chute_in_demand and dist_home > 1 and _manhattan(goal, core_pos) <= 1
        and not _chute_escape
    ):
        # Chute hysteresis: chute-clear above pushed this defender outward to
        # keep the unloading ring free, so it must not walk straight back onto
        # it while carriers are still queued. Without this the two rules take
        # turns every Tick (observed V8/V3: ``LEFT home-patrol`` / ``RIGHT
        # chute-clear`` on 100% of Ticks, and the re-entry was itself rejected
        # often enough to log 967 CELL_UNIT_LIMIT warns per 3000 Ticks).
        _home_chute_wait[_uid] = (pos, (_cw[1] + 1) if _cw is not None else 1)
        unit.wait()
        _set_unit_route(unit, goal, [pos], complete=False)
        return ("WAIT", f"home-chute-hold {goal}")
    if pos != goal:
        moved = _move_towards(
            unit,
            pos,
            goal,
            obstacle_cells,
            detail_prefix="home-patrol",
            cell_counts=cell_counts,
            extra_obstacles=_chute_ring,
        )
        if moved is not None:
            return moved
        if dist_home <= return_radius + 1:
            # ``_move_towards`` searched this goal already and the miss it
            # recorded is what keeps the next 120 plan calls from re-searching,
            # so a None here means the slot is walled off from this side rather
            # than merely crowded (observed V3: its radius-3 slot (685,-300) has
            # no path at all from the Core neighbourhood, so it alternated a
            # failed search with the greedy step one cell east/west forever --
            # ``LEFT home-patrol`` / ``RIGHT home-patrol (685, -300)``). Holding
            # the guard cell it is on beats shuffling beside it.
            miss = _march_route_miss.get(str(unit.id))
            if miss is not None and miss[0] == goal:
                unit.wait()
                _set_unit_route(unit, goal, [pos], complete=False)
                return ("WAIT", f"home-slot-unreachable {goal}")

    return _scout_cardinal(
        unit, pos, obstacle_cells, config, label="home-patrol",
        # Not `obstacle_cells | _chute_ring`: a fresh set object defeats the
        # identity-cached dead-end structure and re-runs the batch classification
        # over the whole wall memory (measured live: unit:vanguard 166ms ->
        # 1012ms per Tick). The scout only ever picks one adjacent cell, so
        # excluding the ring as avoid_cells is the same rule for free.
        avoid_cells=_chute_ring,
    )


def _plan_attack_combat(
    unit: Any,
    *,
    unit_kind: str,
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    config: dict[str, Any],
    cell_counts: Mapping | None = None,
    core_pos: tuple[int, int] | None = None,
) -> tuple[str, str]:
    """March as a group toward the configured destination; engage en route.

    attack_mode (三选一) picks the destination:
      - coords -> static attack_target_x / attack_target_y
      - auto   -> enemy sighting from memory weighted by distance to the CORE
                  (base defense) and to the attack squad's current centroid
      - beacon -> the champion beacon's always-public position; the static
                  coordinate and auto-attack settings are ignored in this mode

    attack_auto_radius (auto 模式专用，曼哈顿距离，0=不限制)：
      约束 auto 目标候选与沿途接敌，均只保留距核心 N 格内的点/敌人；
      不约束撤退分支（attack-retreat）。radius=0 或非 auto 模式时完全不生效。

    coords / beacon 模式的沿途接敌（优先级 2）只追"顺路"敌人：
      到目标点的距离不超过本单位当前到目标点的距离（敌人在目标侧/前进
      方向），反方向或偏离行军方向的可见敌人忽略。attack_march_engage_radius
      （N>0）可再限定只追距本单位 N 格内的顺路敌人；0 = 仅顺路过滤。
    """
    pos = tuple(unit.position)

    mode = str(config.get("attack_mode", "coords"))
    attack_radius = max(int(config.get("attack_auto_radius", 0) or 0), 0)
    # Sticky home-recovery: once this unit's march has failed it keeps walking
    # to the Core and must NOT re-attempt the target march each Tick — at a
    # wall corner the two planners otherwise flip the unit back and forth
    # forever (observed: UP regroup / DOWN march alternating 60+ Ticks).
    # Recovery clears on arrival at the Core cell, where the passive HEAL
    # repairs it and the target is back within pathing budget.
    if str(unit.id) in _attack_regroup_ids:
        if (
            core_pos is None
            or pos == tuple(core_pos)
            or (
                _chute_in_demand
                and _manhattan(pos, core_pos) <= 1
                and not _regroup_needs_core_cell(unit, config)
            )
        ):
            # Arrival clears recovery. With carriers hauling the chute, the
            # ring already counts as home: recovery must release the unit so
            # the march (or engagement) is re-planned instead of pressing the
            # core cell every Tick — the deposit queue never wins that cell
            # race (observed 1588+ Tick chute deadlock).
            _attack_regroup_ids.discard(str(unit.id))
        else:
            step = _attack_regroup_step(
                unit,
                pos,
                core_pos,
                obstacle_cells,
                cell_counts=cell_counts,
                config=config,
                enemies=enemies,
                unit_kind=unit_kind,
            )
            if step is not None:
                return step
            unit.wait()
            return ("WAIT", f"attack-regroup-boxed {core_pos}")
    # Squad-wide outnumbered-retreat verdict (set once per Tick in
    # choose_actions from the full enemy view + squad centroid). In auto mode a
    # True verdict short-circuits all engagement: the squad disengages away from
    # the enemy cluster and the auto scorer re-targets past the forbidden cells.
    retreat = bool(getattr(turn_context, "attack_retreat", False))
    forbidden = getattr(turn_context, "attack_forbidden_targets", frozenset())
    cluster_centroid = getattr(turn_context, "attack_retreat_from", None)

    if mode == "beacon":
        beacon_pos = getattr(turn_context, "beacon_pos", None)
        target = beacon_pos or (int(config["attack_target_x"]), int(config["attack_target_y"]))
    elif mode == "auto" and _enemy_memory:
        # Prefer enemies close to the CORE (threats to the base) and close to
        # the attack squad's centroid (reachable now). Both references are
        # shared by every squad member, so the whole team converges on the same
        # target instead of splitting toward per-unit nearest points. When the
        # squad is empty, fall back to the unit's own position.
        core_pos = getattr(turn_context, "core_pos", None)
        squad_pos = getattr(turn_context, "attack_squad_pos", None) or pos
        candidates = set(_enemy_memory) - set(forbidden)
        if attack_radius > 0 and core_pos is not None:
            # 半径约束：只保留距核心 attack_radius 格（曼哈顿）内的目击点；
            # 过滤后为空则落入下方静态进攻坐标回退。
            candidates = {
                p for p in candidates if _manhattan(p, core_pos) <= attack_radius
            }
        core_targets, guarded_cores = _auto_core_targets(candidates, enemies)
        candidates -= guarded_cores
        if core_targets:
            # 落单核心优先：半径内有新鲜的无护卫核心目击点时，整队先清核。
            candidates = core_targets
        if not candidates:
            # Everything in memory is part of the outnumbering cluster (or
            # memory only holds the cluster): regroup toward the home coords
            # instead of charging back into the losing fight.
            target = (int(config["attack_target_x"]), int(config["attack_target_y"]))
        else:
            turn_context.attack_target_candidates = _auto_target_rows(
                candidates, core_pos, squad_pos,
                priority="core" if core_targets else None,
            )
            target = min(
                candidates, key=lambda p: _auto_target_rank(p, core_pos, squad_pos)
            )
    else:
        # A queued squad target (队首) outranks the static attack coordinates;
        # an empty queue falls back to them.
        target = getattr(turn_context, "attack_queue_target", None) or (
            int(config["attack_target_x"]), int(config["attack_target_y"])
        )

    # Outnumbered-retreat policy (auto mode only): disengage away from the
    # enemy cluster centroid and re-target. This deliberately overrides the
    # adjacent-sweep / best-shot / engage steps so a losing fight is never
    # traded into — the squad sheds contact first, then re-picks a target.
    if retreat and enemies and cluster_centroid is not None:
        squad_pos = getattr(turn_context, "attack_squad_pos", None) or pos
        flee_dx = squad_pos[0] - cluster_centroid[0]
        flee_dy = squad_pos[1] - cluster_centroid[1]
        if flee_dx == 0 and flee_dy == 0:
            direction = (
                Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT
            )[hash(str(unit.id)) % 4]
        else:
            direction = _cardinal_toward_delta(
                1 if flee_dx > 0 else (-1 if flee_dx < 0 else 0),
                1 if flee_dy > 0 else (-1 if flee_dy < 0 else 0),
            )
        if direction is not None and _try_move(unit, direction, pos, obstacle_cells):
            return ("MOVE", f"{direction.name} attack-retreat n={len(enemies)}")
        for fallback in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT):
            if direction is not None and fallback == direction:
                continue
            if _try_move(unit, fallback, pos, obstacle_cells):
                return ("MOVE", f"{fallback.name} attack-retreat n={len(enemies)}")
        unit.wait()
        return ("WAIT", f"attack-retreat-blocked n={len(enemies)}")

    if unit_kind == "vanguard":
        # Sweep first: an adjacent enemy is free damage this Tick. Approach
        # only when no cardinal target exists (see _plan_waypoint).
        sweep = _vanguard_adjacent_sweep(unit, pos, enemies)
        if sweep is not None:
            return sweep
        approach = _vanguard_approach_for_sweep(
            unit, pos, enemies, obstacle_cells, cell_counts,
        )
        if approach is not None:
            return approach
    else:
        kite = _ranger_low_hp_kite(unit, pos, enemies, obstacle_cells, cell_counts)
        if kite is not None:
            return kite
        shot = _ranger_best_shot(
            unit,
            pos,
            enemies,
            obstacle_cells,
            int(config["ranger_attack_range"]),
            lead_fire_enabled=bool(config.get("ranger_lead_fire_enabled", True)),
        )
        if shot is not None:
            return shot

    if enemies:
        engage_pool = enemies
        if (
            mode == "auto"
            and attack_radius > 0
            and getattr(turn_context, "core_pos", None) is not None
        ):
            # 沿途接敌同样受半径约束：只追击距核心 N 格内的可见敌人；
            # 子集为空则跳过接敌落入行军。radius=0 或非 auto 模式保持原逻辑。
            # `enemies` 是全军视野的并集，所以半径之外还必须按本单位视野收口：
            # 一个斥候在 300 格外看见敌弓手，就会把正在向西清矿的先锋整个调头
            # 向东，两拍一换方向、行军永远走不完（observed: V19 456 拍净位移 19
            # 格、V20 368 拍净位移 9 格；同样的调头把 V17/V18/R8 送进了敌火力）。
            # attack_march_engage_radius=0 时维持旧行为（只受半径约束）。
            engage_core = getattr(turn_context, "core_pos", None)
            engage_leash = max(int(config.get("attack_march_engage_radius", 0) or 0), 0)
            engage_pool = tuple(
                e
                for e in enemies
                if _manhattan(tuple(e.position), engage_core) <= attack_radius
                and (
                    engage_leash <= 0
                    or _manhattan(pos, tuple(e.position)) <= engage_leash
                )
            )
        elif mode in ("coords", "beacon"):
            # 坐标/信标模式只接战"顺路"敌人：该敌人到目标点的距离不超过
            # 本单位当前到目标点的距离（敌人在目标侧/前进方向）。反方向或
            # 偏离行军方向的可见敌人一律忽略，落入行军朝目标推进。
            # attack_march_engage_radius（N>0）额外要求敌人在本单位 N 格内，
            # 防止为远处敌人大幅绕路；0 = 仅顺路过滤。两模式共用该配置。
            march_leash = max(int(config.get("attack_march_engage_radius", 0) or 0), 0)
            target_dist = _manhattan(pos, target)
            engage_pool = tuple(
                e
                for e in enemies
                if _manhattan(tuple(e.position), target) <= target_dist
                and (march_leash <= 0 or _manhattan(pos, tuple(e.position)) <= march_leash)
            )
        if engage_pool:
            nearest = min(engage_pool, key=lambda e: _manhattan(pos, e.position))
            moved = _move_towards(
                unit,
                pos,
                tuple(nearest.position),
                obstacle_cells,
                detail_prefix="attack-engage",
                cell_counts=cell_counts,
            )
            if moved is not None:
                return moved

    if pos == target:
        unit.wait()
        return ("WAIT", f"attack-hold-{mode} {target}")

    moved = _move_towards(
        unit,
        pos,
        target,
        obstacle_cells,
        detail_prefix=f"attack-march-{mode}",
        cell_counts=cell_counts,
    )
    if moved is not None:
        return moved

    # March failed (goal beyond the A* expansion budget, or the goal-ward
    # neighbours are walled): switch to home-squad recovery instead of waiting
    # forever. Walk toward the Core with the distance-independent greedy step,
    # heal on the Core cell once arrived, and re-march the target when it is
    # back within pathing budget. The regroup flag is sticky (see top of this
    # function) so the two planners cannot flip-flop at a wall corner.
    if core_pos is not None and _manhattan(pos, core_pos) > 0:
        _attack_regroup_ids.add(str(unit.id))
        regroup = _attack_regroup_step(
            unit,
            pos,
            core_pos,
            obstacle_cells,
            cell_counts=cell_counts,
            config=config,
            enemies=enemies,
            unit_kind=unit_kind,
        )
        if regroup is not None:
            return regroup

    unit.wait()
    return ("WAIT", f"attack-blocked-{mode} {target}")


def _kite_motion_info(
    enemy: Any,
    friendly_pos: tuple[int, int],
) -> dict[str, Any]:
    """Classify the enemy's last step relative to one kite unit.

    The velocity dot the vector from the enemy's previous cell to us is
    positive for 进攻 and negative for 撤退. This detects a horizontal closing
    step even when Chebyshev distance stays flat because the units are also
    vertically separated. Lateral/stationary motion remains explicit.
    """
    history = _enemy_motion_tracks.get(
        str(getattr(enemy, "id", id(enemy))),
        [],
    )
    velocity, streak = _motion_streak(history)
    state = "unknown"
    previous = history[-2][1] if len(history) >= 2 else None
    current = tuple(enemy.position)
    positions = [entry[1] for entry in history]
    oscillating = (
        len(positions) >= 3
        and positions[-1] == current
        and positions[-2] != current
        and positions[-3] == current
    )
    if previous is not None:
        step = current[0] - previous[0], current[1] - previous[1]
        toward = friendly_pos[0] - previous[0], friendly_pos[1] - previous[1]
        alignment = step[0] * toward[0] + step[1] * toward[1]
        if current == previous:
            state = "stationary"
        elif alignment > 0:
            state = "advance"
        elif alignment < 0:
            state = "retreat"
        else:
            state = "lateral"
    predicted = None
    if oscillating:
        # A two-cell bounce reverses every Tick, so continuing the latest
        # velocity aims one cell past the target every time. Production R15/R12
        # fired 140 shots at an enemy alternating (-1352,1301)/(-1352,1302) and
        # hit twice; the next cell is the one it just left.
        predicted = positions[-2]
    elif velocity is not None and abs(velocity[0]) + abs(velocity[1]) == 1:
        predicted = (current[0] + velocity[0], current[1] + velocity[1])
    return {
        "state": state,
        "previous": previous,
        "current": current,
        "velocity": velocity,
        "streak": streak,
        "predicted": predicted,
        "oscillating": oscillating,
    }


def _kite_enemy_can_hit(
    cell: tuple[int, int],
    enemy: Any,
    obstacle_cells: frozenset[tuple[int, int]],
    ranger_range: int,
    *,
    enemy_pos: tuple[int, int] | None = None,
) -> bool:
    """Whether an enemy could attack ``cell`` if it occupied ``enemy_pos``."""
    origin = tuple(enemy_pos or enemy.position)
    etype = _enemy_unit_type_name(enemy)
    dx, dy = cell[0] - origin[0], cell[1] - origin[1]
    if etype == "WORKER":
        return False
    if etype == "RANGER":
        dist = max(abs(dx), abs(dy))
        return (
            1 <= dist <= ranger_range
            and (dx == 0 or dy == 0 or abs(dx) == abs(dy))
            and not _line_blocked(origin, cell, obstacle_cells)
        )
    # Vanguard and Core are conservatively treated as cardinal melee threats.
    return abs(dx) + abs(dy) == 1


def _kite_cell_assessment(
    cell: tuple[int, int],
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    ranger_range: int,
) -> dict[str, Any]:
    current_hits = 0
    predicted_hits = 0
    nearest = 10_000
    for enemy in _combat_threats(enemies):
        epos = tuple(enemy.position)
        nearest = min(nearest, _chebyshev(cell, epos))
        if _kite_enemy_can_hit(cell, enemy, obstacle_cells, ranger_range):
            current_hits += 1
        motion = _kite_motion_info(enemy, cell)
        predicted = motion.get("predicted")
        if predicted is not None and _kite_enemy_can_hit(
            cell,
            enemy,
            obstacle_cells,
            ranger_range,
            enemy_pos=predicted,
        ):
            predicted_hits += 1
    return {
        "current_hits": current_hits,
        "predicted_hits": predicted_hits,
        "nearest": nearest if nearest < 10_000 else 999,
    }


def _kite_record_move_attempt(
    unit: Any,
    pos: tuple[int, int],
    target_cell: tuple[int, int],
) -> None:
    """Track a same-tick move collision to enable predictive cell attacks.

    Movement resolves before combat. If two units (or a unit and an enemy)
    both submit a move into the same cell on the same tick, the server
    cancels both and neither advances. When this unit calls move() toward
    target_cell, record (pos, target_cell). On the next tick _plan_kite_combat
    inspects the entry: if pos is unchanged the previous move collided, and
    the counter increments. At count >= 2 the enemy's next move into that cell
    is predictable, so the caller attacks the cell directly.
    """
    uid = str(unit.id)
    prev = _kite_collision_streak.get(uid)
    if prev is not None and prev[0] == pos and prev[1] == target_cell:
        _kite_collision_streak[uid] = (pos, target_cell, prev[2] + 1)
    else:
        _kite_collision_streak[uid] = (pos, target_cell, 1)


def _kite_clear_move_attempt(unit: Any) -> None:
    """Drop the collision tracker once the unit advanced or attacked."""
    _kite_collision_streak.pop(str(unit.id), None)


def _finalize_kite_collision_state(accepted: bool) -> None:
    """Rollback speculative collision updates when a plan was not accepted."""
    snapshot = getattr(turn_context, "kite_collision_snapshot", None)
    if snapshot is None:
        return
    if not accepted:
        _kite_collision_streak.clear()
        _kite_collision_streak.update(snapshot)
    turn_context.kite_collision_snapshot = None


def _discard_noncollision_move_failures(turn: Any) -> None:
    """Do not interpret explicit capacity rejections as move collisions."""
    for event in getattr(turn, "events", ()) or ():
        if (
            getattr(event, "event_type", "") == "UNIT_MOVE_FAILED"
            and getattr(event, "reason_code", None) == "CELL_UNIT_LIMIT"
        ):
            actor_id = getattr(event, "actor_id", None)
            if actor_id is not None:
                _kite_collision_streak.pop(str(actor_id), None)


def _kite_bfs_unstick(
    unit: Any,
    pos: tuple[int, int],
    goal: tuple[int, int],
    obstacle_cells: frozenset[tuple[int, int]],
    cell_counts: Mapping | None,
    *,
    extra_obstacles: Iterable[tuple[int, int]] = (),
    step_filter: Callable[[tuple[int, int]], bool] | None = None,
    max_steps: int = 2500,
) -> tuple[str, str] | None:
    """Multi-step escape hatch for a kite unit pinned by walls.

    ``_kite_choose_move`` is a single-step greedy picker. When a unit is
    cornered against a wall, every adjacent cell that leads out of the pocket
    is either an obstacle or a dead end, so progress turns negative and WAIT
    wins forever — the unit never leaves. This runs a real A* search toward
    ``goal`` and takes the first step, bypassing the dead-end penalty the way
    ``_move_towards`` does for the home/attack squads. Safety (danger-zone
    evasion) is already settled before this is reached; this only fires when
    the single-step policy would otherwise WAIT on the spot indefinitely.
    """
    if pos == goal:
        return None
    transient_obstacles = frozenset(extra_obstacles)
    path = _bfs_path(
        pos,
        goal,
        obstacle_cells,
        max_steps=max_steps,
        extras=transient_obstacles,
    )
    if not path or len(path) < 2:
        return None
    counts = cell_counts or {}
    for idx in range(1, len(path)):
        next_cell = path[idx]
        if next_cell in obstacle_cells or next_cell in transient_obstacles:
            continue
        if counts.get(next_cell, 0) >= _CELL_UNIT_LIMIT:
            continue
        if step_filter is not None and not step_filter(next_cell):
            continue
        # allow dead-end cells here: the route itself proves an exit exists
        direction = _direction_for_step(pos, next_cell)
        if direction is None:
            continue
        if _try_move(
            unit, direction, pos, obstacle_cells, avoid_dead_ends=False,
        ):
            _kite_record_move_attempt(unit, pos, next_cell)
            _set_unit_route(unit, goal, path, complete=False)
            return ("MOVE", f"{direction.name} kite-unstick goal={goal}")
    return None


def _kite_choose_move(
    unit: Any,
    pos: tuple[int, int],
    goal: tuple[int, int],
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    ranger_range: int,
    cell_counts: Mapping | None,
    *,
    must_move: bool,
    avoid_cells: Iterable[tuple[int, int]] = (),
    stall_ticks: int = 0,
    global_enemies: tuple = (),
) -> tuple[Direction | None, dict[str, Any], list[dict[str, Any]]]:
    """Choose the safest cardinal cell, using goal progress only as a tie-break.

    ``stall_ticks`` is how many consecutive ticks this unit has failed to move.
    Once that reaches _KITE_STALL_UNLOCK_TICKS with no visible threat at all,
    progress stops outranking a safe detour step (progress=-1 versus WAIT's 0
    froze units forever), so the risk-free tie-break ignores progress and any
    safe non-dead-end step beats WAIT. Hit-count safety scoring is untouched:
    the unlock only applies when this unit sees no enemy threat whatsoever.

    Two unlock tiers, because one gate is not enough. The cover contract is
    deliberately kept for threats this unit cannot see: ``global_enemies`` is
    the full visible-enemy tuple of the whole army, so an enemy outside this
    unit's vision still keeps it in cover — but only up to
    _KITE_STALL_HARD_UNLOCK_TICKS. In combat the army sees *something* on ~70%
    of Ticks, so the short 12-Tick tier alone was unreachable and a hold-free
    unit at a memory-dead-ended pocket mouth froze forever (observed V10 at
    (-807,-2244): three walled neighbours, the single free one classified a
    dead end, ``enemies=[]`` on 170/170 Ticks, immobile 1,670+ Ticks).
    The unlock is also suppressed once the unit already stands on its goal —
    arriving at a coordinate/beacon target is a deliberate hold, and letting
    the unlock fire there would jitter the unit one cell every threshold
    ticks.

    Past ``_KITE_SHUTTLE_BREAK_REVERSALS`` strict reversals the step back onto
    ``came_from`` is dropped from the candidate list, whenever at least one
    *other step* remains (holding still is never an alternative to a
    cul-de-sac retreat). That gate carries no threat condition on purpose: a
    unit ping-ponging across an enemy's threat ring sees a threat every Tick, so
    the unlock above can never fire for it, and `-backtrack` alone cannot win
    because the two cells already differ higher up in the score tuple.
    """
    counts = cell_counts or {}
    enemy_cells = {tuple(enemy.position) for enemy in enemies}
    avoided = frozenset(avoid_cells)
    # The cell this unit stood on when it last planned. Stepping back onto it is
    # the A-B-A shuttle: with the stall unlock active every safe cell ties on
    # progress, and the scan order (UP first) then sends the unit straight back
    # where it came from (observed V11/V6: ``kite-route`` one Tick, ``kite-position``
    # the reverse the next, 98% of Ticks, net displacement zero).
    came_from = _kite_prev_pos.get(str(unit.id))
    # Decided once per plan, not per candidate: the unlock also has to lift the
    # `-dead_end` penalty below, or a unit whose only exit memory calls a dead
    # end keeps losing to WAIT and stays frozen whatever the tier.
    stall_unlocked = (
        pos != goal
        and not enemies
        and stall_ticks >= (
            _KITE_STALL_UNLOCK_TICKS
            if not global_enemies
            else _KITE_STALL_HARD_UNLOCK_TICKS
        )
    )
    choices: list[tuple[tuple, Direction | None, tuple[int, int], dict[str, Any]]] = []
    directions: tuple[Direction | None, ...] = (
        Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT, None,
    )
    for direction in directions:
        if direction is None:
            if must_move:
                continue
            cell = pos
        else:
            cell = (pos[0] + direction.delta[0], pos[1] + direction.delta[1])
            if cell in obstacle_cells or cell in enemy_cells or cell in avoided:
                continue
            if counts.get(cell, 0) >= _CELL_UNIT_LIMIT:
                continue
        assessment = _kite_cell_assessment(
            cell, enemies, obstacle_cells, ranger_range,
        )
        progress = _manhattan(pos, goal) - _manhattan(cell, goal)
        dead_end = int(
            direction is not None
            and not stall_unlocked
            and _is_dead_end_step(cell, obstacle_cells, allow=(goal,))
        )
        backtrack = int(
            direction is not None and came_from is not None and cell == came_from
        )
        # Never trade safety for progress. Once two cells are equally safe,
        # however, keep advancing instead of backing away forever from a
        # stationary/retreating melee target. Distance remains the stronger
        # tie-break while every available cell is still dangerous.
        risk_free = not (
            assessment["current_hits"] or assessment["predicted_hits"]
        )
        tactical_ties = (
            ((0, assessment["nearest"]) if stall_unlocked else (progress, assessment["nearest"]))
            if risk_free
            else (assessment["nearest"], progress)
        )
        score = (
            -assessment["current_hits"],
            -assessment["predicted_hits"],
            *tactical_ties,
            -dead_end,
            # Any step outranks sitting still: a unit in a genuine cul-de-sac
            # must be allowed to retreat the way it came. Among steps, the one
            # that undoes the last move ranks lowest.
            int(direction is not None),
            -backtrack,
        )
        choices.append((score, direction, cell, {
            **assessment,
            "progress": progress,
            "backtrack": backtrack,
        }))
    if not choices:
        return None, _kite_cell_assessment(pos, enemies, obstacle_cells, ranger_range), []
    pruned = False
    if came_from is not None and (
        _kite_shuttle_ticks.get(str(unit.id), 0) >= _KITE_SHUTTLE_BREAK_REVERSALS
    ):
        # Past the gate the `-backtrack` tie-break is not enough: the two cells
        # differ higher up in the tuple (one of them is inside the enemy's ring,
        # the other advances the goal), so the U-turn still wins on safety and
        # the unit alternates forever. Drop the option instead.
        # The trigger asks for another STEP, not just another candidate: WAIT is
        # always available, so counting it would strip the retreat out of a
        # one-cell nub and freeze the unit there (protected by
        # KiteRouteNoUTurnRegressionTests.test_boxed_stalled_unit_retreats_instead
        # _of_sitting). WAIT itself stays in the list — only the U-turn goes.
        steps = [item for item in choices if item[1] is not None]
        if [item for item in steps if not item[3]["backtrack"]]:
            filtered = [
                item for item in choices
                if item[1] is None or not item[3]["backtrack"]
            ]
            if len(filtered) != len(choices):
                pruned = True
            choices = filtered
    choices.sort(key=lambda item: item[0], reverse=True)
    _, direction, _, assessment = choices[0]
    if pruned:
        # Surfaced in the action detail by the caller: a breaker nobody can see
        # fire is how the contested-backoff id[:8] bug stayed invisible for days.
        assessment = {**assessment, "shuttle_break": True}
    public = [
        {
            "direction": item[1].name if item[1] is not None else "WAIT",
            "cell": list(item[2]),
            **item[3],
        }
        for item in choices
    ]
    return direction, assessment, public


def _kite_mode_target(
    pos: tuple[int, int],
    config: dict[str, Any],
    enemies: tuple = (),
) -> tuple[str, tuple[int, int]]:
    mode = str(config.get("kite_mode", "coords"))
    fallback = (
        int(config.get("kite_target_x", 0)),
        int(config.get("kite_target_y", 0)),
    )
    if mode == "coords":
        # A queued squad target (队首) outranks the static kite coordinates.
        queue_target = getattr(turn_context, "kite_queue_target", None)
        if queue_target is not None:
            return mode, queue_target
    if mode == "beacon":
        return mode, getattr(turn_context, "beacon_pos", None) or fallback
    if mode == "auto" and _enemy_memory:
        radius = max(int(config.get("kite_auto_radius", 0) or 0), 0)
        core_pos = getattr(turn_context, "core_pos", None)
        squad_pos = getattr(turn_context, "kite_squad_pos", None) or pos
        candidates = set(_enemy_memory)
        if radius > 0 and core_pos is not None:
            candidates = {p for p in candidates if _manhattan(p, core_pos) <= radius}
        core_targets, guarded_cores = _auto_core_targets(candidates, enemies)
        candidates -= guarded_cores
        if core_targets:
            candidates = core_targets
        if candidates:
            turn_context.kite_target_candidates = _auto_target_rows(
                candidates, core_pos, squad_pos,
                priority="core" if core_targets else None,
            )
            return mode, min(
                candidates, key=lambda p: _auto_target_rank(p, core_pos, squad_pos)
            )
    return mode, fallback


def _kite_engage_pool(
    pos: tuple[int, int],
    target: tuple[int, int],
    mode: str,
    enemies: tuple,
    config: dict[str, Any],
) -> tuple:
    if mode == "auto":
        radius = max(int(config.get("kite_auto_radius", 0) or 0), 0)
        core_pos = getattr(turn_context, "core_pos", None)
        if radius > 0 and core_pos is not None:
            return tuple(
                enemy for enemy in enemies
                if _manhattan(tuple(enemy.position), core_pos) <= radius
            )
        return tuple(enemies)
    leash = max(int(config.get("kite_march_engage_radius", 0) or 0), 0)
    target_dist = _manhattan(pos, target)
    return tuple(
        enemy for enemy in enemies
        if _manhattan(tuple(enemy.position), target) <= target_dist
        and (leash <= 0 or _manhattan(pos, tuple(enemy.position)) <= leash)
    )


def _kite_diagonal_sweep_directions(
    unit_pos: tuple[int, int],
    enemy: Any,
) -> tuple[Direction, Direction]:
    """Return preferred/alternate bridge cells for a diagonal Vanguard duel."""
    epos = tuple(enemy.position)
    x_direction = Direction.RIGHT if epos[0] > unit_pos[0] else Direction.LEFT
    y_direction = Direction.DOWN if epos[1] > unit_pos[1] else Direction.UP
    history = _enemy_motion_tracks.get(str(enemy.id), [])
    axis_score = {"x": 0, "y": 0}
    for weight, index in enumerate(range(max(1, len(history) - 3), len(history)), start=1):
        if index <= 0:
            continue
        dx = history[index][1][0] - history[index - 1][1][0]
        dy = history[index][1][1] - history[index - 1][1][1]
        if dx:
            axis_score["x"] += weight
        if dy:
            axis_score["y"] += weight
    # Horizontal enemy motion enters the vertical bridge (our y direction),
    # vertical motion enters the horizontal bridge (our x direction).
    if axis_score["x"] > axis_score["y"]:
        return y_direction, x_direction
    if axis_score["y"] > axis_score["x"]:
        return x_direction, y_direction
    if _stable_slot_index(str(enemy.id), 2) == 0:
        return x_direction, y_direction
    return y_direction, x_direction


def _kite_log_decision(
    unit: Any,
    unit_kind: str,
    mode: str,
    objective: tuple[int, int],
    enemies: tuple,
    action: str,
    reason: str,
    *,
    target: Any | None = None,
    candidates: list[dict[str, Any]] | None = None,
) -> None:
    pos = tuple(unit.position)
    enemy_rows = []
    ranger_range = int(getattr(turn_context, "kite_ranger_range", 3) or 3)
    obstacles = getattr(turn_context, "kite_obstacles", frozenset())
    for enemy in enemies:
        motion = _kite_motion_info(enemy, pos)
        predicted = motion["predicted"]
        enemy_rows.append({
            "id": str(getattr(enemy, "id", id(enemy)))[:8],
            "type": _enemy_unit_type_name(enemy) or "ENEMY",
            "position": list(enemy.position),
            "motion": motion["state"],
            "velocity": list(motion["velocity"]) if motion["velocity"] is not None else None,
            "streak": motion["streak"],
            "predicted": list(predicted) if predicted else None,
            "can_hit_now": _kite_enemy_can_hit(pos, enemy, obstacles, ranger_range),
            "can_hit_predicted": bool(
                predicted is not None
                and _kite_enemy_can_hit(
                    pos, enemy, obstacles, ranger_range, enemy_pos=predicted,
                )
            ),
        })
    decision_store = (
        "guerrilla_decisions" if mode == "guerrilla" else "kite_decisions"
    )
    decisions = getattr(turn_context, decision_store, None)
    if decisions is None:
        decisions = []
        setattr(turn_context, decision_store, decisions)
    decisions.append({
        "tick": turn_context.tick,
        "unit_id": str(unit.id)[:8],
        "unit_name": _object_name(unit.id, "V" if unit_kind == "vanguard" else "R"),
        "unit_type": unit_kind.upper(),
        "position": list(pos),
        "hp": int(getattr(unit, "hp", 0) or 0),
        "mode": mode,
        "objective": list(objective),
        "target_id": (
            str(getattr(target, "id", id(target)))[:8]
            if target is not None else None
        ),
        "enemies": enemy_rows,
        "action": action,
        "reason": reason,
        "candidates": candidates or [],
    })


def _kite_battle_log_entries(
    decisions: Iterable[dict[str, Any]],
    tick: int,
) -> list[dict[str, Any]]:
    """Build at most one compact battle-log row for an accepted kite Tick.

    The structured tactic log retains every candidate and enemy assessment.
    The shared dashboard battle log has a 500-line retention budget, so it
    records only Ticks with actual contact and summarizes the whole squad.
    """
    all_decisions = list(decisions)
    contact = [
        decision for decision in all_decisions
        if decision.get("enemies")
        or str(decision.get("action", "")).upper() in {"SHOOT", "SWEEP"}
    ]
    if not contact:
        return []

    action_counts: dict[str, int] = defaultdict(int)
    enemy_ids: set[str] = set()
    summaries: list[str] = []
    for decision in contact:
        action = str(decision.get("action") or "WAIT").upper()
        action_counts[action] += 1
        enemy_ids.update(
            str(row.get("id"))
            for row in decision.get("enemies", [])
            if row.get("id")
        )
        if len(summaries) < 8:
            summaries.append(
                f"{decision.get('unit_name', '?')}:{action} "
                f"{decision.get('reason', '')}".strip()
            )

    actions = ", ".join(
        f"{action}={action_counts[action]}" for action in sorted(action_counts)
    )
    omitted = len(contact) - len(summaries)
    detail = "; ".join(summaries)
    if omitted > 0:
        detail += f"; 另 {omitted} 个单位"
    return [{
        "tick": tick,
        "ts": time.time(),
        "cat": "combat",
        "msg": (
            f"风筝队接敌：单位 {len(contact)}/{len(all_decisions)}，"
            f"敌人 {len(enemy_ids)}，{actions}｜{detail}"
        ),
    }]


def _append_accepted_kite_battle_log(accepted: bool) -> None:
    """Persist kite battle summaries only after the server accepts the plan."""
    if not accepted:
        return
    entries = _kite_battle_log_entries(
        getattr(turn_context, "kite_decisions", []) or [],
        int(getattr(turn_context, "tick", 0) or 0),
    )
    if not entries:
        return
    try:
        append_jsonl(BATTLE_LOG_PATH, entries)
    except OSError:
        pass


def _kite_ranger_fire(
    ranger: Any,
    pos: tuple[int, int],
    target: Any,
    expected: tuple[int, int] | None,
    obstacle_cells: frozenset[tuple[int, int]],
    attack_range: int,
) -> tuple[str, str] | None:
    fired = expected or tuple(target.position)
    dx, dy = fired[0] - pos[0], fired[1] - pos[1]
    dist = max(abs(dx), abs(dy))
    if not (
        1 <= dist <= attack_range
        and (dx == 0 or dy == 0 or abs(dx) == abs(dy))
        and not _line_blocked(pos, fired, obstacle_cells)
    ):
        return None
    if expected is not None:
        ranger.shoot(target, expected_cell=expected)
        motion = _kite_motion_info(target, pos)
        prediction = _shadow_shot_prediction(
            ranger, pos, target, obstacle_cells, attack_range,
        )
        prediction.update({
            "predicted_cell": list(expected),
            "velocity": list(motion["velocity"]) if motion["velocity"] else None,
            "move_streak": motion["streak"],
            "motion_state": f"kite_{motion['state']}",
            "oscillating": bool(motion.get("oscillating")),
            "prediction_legal": True,
            "eligible": True,
            "reason": "kite_motion_prediction",
            "lead_fire_used": True,
            "lead_fire_rejection": None,
            "fire_mode": "kite_lead",
            "fired_cell": list(expected),
        })
        turn_context.shot_predictions.append(prediction)
        suffix = " bounce" if motion.get("oscillating") else ""
        return "SHOOT", f"kite-lead {expected} motion={motion['state']}{suffix}"
    ranger.shoot(target)
    return "SHOOT", f"kite-current {tuple(target.position)}"


def _prepare_kite_coordination(
    units: Iterable[Any],
    enemies: tuple,
    config: dict[str, Any],
    obstacle_cells: frozenset[tuple[int, int]],
) -> dict[str, dict[str, Any]]:
    """Pre-assign Nv1 collision traps and two-Vanguard diagonal coverage."""
    kite_names = _parse_team_names(config.get("kite_team", ""))
    kite_units = [
        unit for unit in units
        if unit.unit_type in (UnitType.VANGUARD, UnitType.RANGER)
        and _object_name(unit.id, _UNIT_NAME_PREFIX[unit.unit_type]).upper() in kite_names
    ]
    directives: dict[str, dict[str, Any]] = {}
    threats = _combat_threats(enemies)
    enemy_vanguards = [e for e in threats if _enemy_unit_type_name(e) == "VANGUARD"]
    friendly_cells: dict[tuple[int, int], int] = defaultdict(int)
    for unit in units:
        if getattr(unit, "unit_type", None) in (
            UnitType.WORKER, UnitType.VANGUARD, UnitType.RANGER,
        ):
            friendly_cells[tuple(unit.position)] += 1

    # Game rule 2: two friendly Vanguards stacked on a diagonal cover both
    # possible bridge cells, so the opponent cannot choose an unguarded side.
    by_cell: dict[tuple[int, int], list[Any]] = defaultdict(list)
    for unit in kite_units:
        if unit.unit_type == UnitType.VANGUARD:
            by_cell[tuple(unit.position)].append(unit)
    for cell, stacked in by_cell.items():
        if len(threats) != 1 or len(stacked) < 2:
            continue
        diagonal = next((
            enemy for enemy in enemy_vanguards
            if abs(enemy.position[0] - cell[0]) == 1
            and abs(enemy.position[1] - cell[1]) == 1
            and _kite_motion_info(enemy, cell)["state"] == "advance"
        ), None)
        if diagonal is None:
            continue
        first, second = _kite_diagonal_sweep_directions(cell, diagonal)
        open_directions = tuple(
            direction for direction in (first, second)
            if (cell[0] + direction.delta[0], cell[1] + direction.delta[1])
            not in obstacle_cells
        )
        for unit, direction in zip(
            sorted(stacked, key=lambda u: str(u.id)), open_directions,
        ):
            directives[str(unit.id)] = {
                "kind": "sweep", "direction": direction, "target": diagonal,
                "reason": "nv1-stacked-diagonal-cover",
            }

    # Game rule 1: in Nv1, collide one Vanguard with an advancing enemy at the
    # middle cell while one Ranger fires at the enemy's unchanged current cell.
    if len(kite_units) >= 2 and len(threats) == 1 and enemy_vanguards:
        enemy = enemy_vanguards[0]
        vanguards = sorted(
            (u for u in kite_units if u.unit_type == UnitType.VANGUARD),
            key=lambda u: _manhattan(tuple(u.position), tuple(enemy.position)),
        )
        rangers = sorted(
            (u for u in kite_units if u.unit_type == UnitType.RANGER),
            key=lambda u: _manhattan(tuple(u.position), tuple(enemy.position)),
        )
        for vanguard in vanguards:
            vpos = tuple(vanguard.position)
            dx = enemy.position[0] - vpos[0]
            dy = enemy.position[1] - vpos[1]
            if not ((abs(dx) == 2 and dy == 0) or (abs(dy) == 2 and dx == 0)):
                continue
            if _kite_motion_info(enemy, vpos)["state"] != "advance":
                continue
            direction = _cardinal_toward_delta(dx, dy)
            middle = (vpos[0] + direction.delta[0], vpos[1] + direction.delta[1])
            if (
                middle in obstacle_cells
                or friendly_cells.get(middle, 0) > 0
                or str(vanguard.id) in directives
            ):
                continue
            shooter = next((
                ranger for ranger in rangers
                if _kite_ranger_fire_legal(
                    tuple(ranger.position), tuple(enemy.position), obstacle_cells,
                    int(config.get("ranger_attack_range", 3)),
                )
                and not _kite_enemy_can_hit(
                    tuple(ranger.position), enemy, obstacle_cells,
                    int(config.get("ranger_attack_range", 3)),
                )
            ), None)
            if shooter is None:
                continue
            directives[str(vanguard.id)] = {
                "kind": "move", "direction": direction, "target": enemy,
                "reason": "nv1-contest-middle",
            }
            directives[str(shooter.id)] = {
                "kind": "shoot_current", "target": enemy,
                "reason": "nv1-collision-pin-shot",
            }
            break
    return directives


def _kite_ranger_fire_legal(
    ranger_pos: tuple[int, int],
    cell: tuple[int, int],
    obstacle_cells: frozenset[tuple[int, int]],
    attack_range: int,
) -> bool:
    dx, dy = cell[0] - ranger_pos[0], cell[1] - ranger_pos[1]
    return (
        1 <= max(abs(dx), abs(dy)) <= attack_range
        and (dx == 0 or dy == 0 or abs(dx) == abs(dy))
        and not _line_blocked(ranger_pos, cell, obstacle_cells)
    )


# The full-HP Vanguard vs Ranger dance is a face-to-face manoeuvre: step out of
# the cell the Ranger is aiming at, then close the last cell and trade blows.
# It had no range gate, so a Ranger that merely happened to be the squad's
# long-distance objective hijacked it and reversed the march every other Tick —
# the direct step ran at the Ranger while the spacing chooser stepped back
# toward the real objective. (observed: V13 52 Ticks and V15 38 Ticks of
# one-cell ping-pong against a Ranger 357-391 cells away.) The slack still lets
# the dance finish closing on a Ranger one step outside its reach; anything
# farther is the cached A* march's job.
_FULL_V_DANCE_REACH_SLACK = 1


def _plan_kite_combat(
    unit: Any,
    *,
    unit_kind: str,
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    config: dict[str, Any],
    cell_counts: Mapping | None = None,
    solo: bool = False,
) -> tuple[str, str]:
    """Fight to the end by staying outside attack zones and striking future cells.

    ``solo=True`` reuses the kite combat policy for a single guerrilla unit.
    It deliberately skips kite-team directives and shared objectives: each
    guerrilla unit selects only its own local threats and falls back to its own
    stable roaming bearing when no threat is present.
    """
    pos = tuple(unit.position)
    uid = str(unit.id)
    tick = int(getattr(turn_context, "tick", 0) or 0)
    # Expire / relocate friendly-split memos kept from earlier ticks. The memo
    # is sticky: it survives collision streak resets and only lapses when the
    # unit has actually left the stacked cell (or the timer runs out).
    friendly_memo = _kite_friendly_split.get(uid)
    if friendly_memo is not None:
        contested_m, origin_m, expire_m = friendly_memo
        if expire_m <= tick or pos != origin_m:
            _kite_friendly_split.pop(uid, None)
            friendly_memo = None
    friendly_memo_cells = (friendly_memo[0],) if friendly_memo is not None else ()
    if solo:
        mode = "guerrilla"
        objective = _guerrilla_roam_goal(unit, pos)
    else:
        mode, objective = _kite_mode_target(pos, config, enemies)
    attack_range = int(config.get("ranger_attack_range", 3))
    # turn.visible_enemies is the union of the whole army's sight. "Nearby"
    # must remain per-kite-unit or one scout would make every kite unit dodge a
    # threat on the other side of the map.
    if solo:
        sight = int(config.get("guerrilla_engage_radius", 0) or 0)
        if sight <= 0:
            sight = _GUERRILLA_SIGHT.get(unit_kind, attack_range + 1)
    else:
        sight = _GUERRILLA_SIGHT.get(unit_kind, attack_range + 1)
    local_enemies = tuple(
        enemy for enemy in enemies
        if _manhattan(pos, tuple(enemy.position)) <= sight
    )
    threats = _combat_threats(local_enemies)
    directive = (
        None
        if solo
        else getattr(turn_context, "kite_directives", {}).get(str(unit.id))
    )
    if directive is not None:
        target = directive.get("target")
        if directive["kind"] == "sweep":
            _kite_clear_move_attempt(unit)
            unit.sweep(directive["direction"])
            action, detail = "SWEEP", f"{directive['direction'].name} {directive['reason']}"
        elif directive["kind"] == "move":
            _kite_record_move_attempt(
                unit,
                pos,
                (
                    pos[0] + directive["direction"].delta[0],
                    pos[1] + directive["direction"].delta[1],
                ),
            )
            unit.move(directive["direction"])
            action, detail = "MOVE", f"{directive['direction'].name} {directive['reason']}"
        else:
            _kite_clear_move_attempt(unit)
            unit.shoot(target)
            action, detail = "SHOOT", f"{directive['reason']} {tuple(target.position)}"
        _kite_log_decision(
            unit, unit_kind, mode, objective, threats, action, detail, target=target,
        )
        _record_kite_stall(uid, pos)
        return action, detail

    # A solo guerrilla has no route leash or squad objective.  Its own local
    # sightings are the complete engagement pool; otherwise the kite planner's
    # shared mode/target would make independent guerrillas converge. As in the
    # kite policy, workers/Cores may be attacked but never create danger zones.
    engage_pool = (
        local_enemies
        if solo
        else _kite_engage_pool(pos, objective, mode, enemies, config)
    )
    if solo:
        # Guerrillas hunt exposed enemy Cores, but do not walk into a Core
        # that has a combat escort in its immediate neighbourhood.  Evaluate
        # the escort against the complete visible set (rather than only this
        # unit's local slice): another friendly scout may have revealed the
        # guard even when this guerrilla cannot see it itself.
        engage_pool = tuple(
            enemy
            for enemy in engage_pool
            if _enemy_unit_type_name(enemy) != "CORE"
            or _guerrilla_core_isolated(enemy, enemies)
        )
    # A kite squad may not ignore an off-route enemy that is about to enter
    # its own range: route leashes only select long-distance objectives. Keep
    # every nearby combat threat in the tactical pool for pre-fire/spacing.
    local_pool = tuple(
        enemy for enemy in threats
        if _chebyshev(pos, tuple(enemy.position)) <= attack_range + 1
    )
    pool_by_id = {
        str(getattr(enemy, "id", id(enemy))): enemy
        for enemy in (*engage_pool, *local_pool)
    }
    combat_pool = _combat_threats(tuple(pool_by_id.values()))
    target_pool = combat_pool or engage_pool
    if solo and not combat_pool:
        isolated_cores = tuple(
            enemy for enemy in engage_pool
            if _enemy_unit_type_name(enemy) == "CORE"
            and _guerrilla_core_isolated(enemy, enemies)
        )
        if isolated_cores:
            # Keep an immediately actionable worker/core in preference to a
            # farther target (the old safe-sweep behaviour), but otherwise
            # let an exposed Core define the guerrilla's objective instead of
            # wandering toward an unrelated worker.
            direct_non_core = tuple(
                enemy for enemy in engage_pool
                if _enemy_unit_type_name(enemy) != "CORE"
                and _chebyshev(pos, tuple(enemy.position)) <= attack_range
            )
            if not direct_non_core:
                target_pool = isolated_cores
    target = min(
        target_pool,
        key=lambda enemy: _manhattan(pos, tuple(enemy.position)),
        default=None,
    )
    if solo:
        locked_core = _guerrilla_core_target.get(uid)
        if locked_core is not None and not _core_position_unguarded(
            locked_core[0], enemies,
        ):
            # A combat escort showed up next to the remembered core: forget it.
            _guerrilla_core_target.pop(uid, None)
    if solo and target is not None and _enemy_unit_type_name(target) == "CORE":
        # Refresh the lock every tick the exposed core is actually visible.
        _guerrilla_core_target[uid] = (tuple(target.position), tick)
    elif solo and target is not None:
        # Non-core prey gets the shorter lock: the flicker cycle is driven by
        # this unit's own step, so the cell has to survive one blind tick.
        _guerrilla_unit_target[uid] = (tuple(target.position), tick)
    current_assessment = _kite_cell_assessment(
        pos, threats, obstacle_cells, attack_range,
    )

    # Full-health Vanguard vs Ranger is the deliberate exception: keep moving
    # so the non-predictive Ranger shoots the old cell, close, then trade 4 HP
    # into 2 HP face-to-face. It never invokes a retreat-to-base branch.
    full_v_vs_r = (
        unit_kind == "vanguard"
        and int(getattr(unit, "hp", 0) or 0) >= 4
        and target is not None
        and _enemy_unit_type_name(target) == "RANGER"
        and _chebyshev(pos, tuple(target.position))
        <= attack_range + _FULL_V_DANCE_REACH_SLACK
    )
    # Predictive attack on a contested cell. If this unit already had two
    # same-tick move attempts toward the same cell cancelled by collision
    # (same pos, same target cell, the server dropped both moves), a Vanguard
    # can sweep that cell. For a Ranger, only pre-fire it when an enemy's own
    # motion track predicts that cell; the collision may have been with an ally.
    collision = _kite_collision_streak.get(str(unit.id))
    collision_avoid: tuple[int, int] | None = None
    if (
        collision is not None
        and collision[0] == pos
        and collision[2] >= 2
        and target is not None
    ):
        contested = collision[1]
        cdx, cdy = contested[0] - pos[0], contested[1] - pos[1]
        if unit_kind == "vanguard" and abs(cdx) + abs(cdy) == 1:
            direction = _direction_for_step(pos, contested)
            if direction is not None:
                unit.sweep(direction)
                _kite_collision_streak.pop(str(unit.id), None)
                detail = (
                    f"{direction.name} kite-collision-prefire "
                    f"cell={contested} enemy={tuple(target.position) if target else contested}"
                )
                _kite_log_decision(
                    unit, unit_kind, mode, objective, threats, "SWEEP", detail,
                    target=target,
                )
                _record_kite_stall(uid, pos)
                return "SWEEP", detail
        if unit_kind == "ranger":
            # A repeated failed move can be an ally collision; it does not prove
            # that the currently selected enemy caused it. Only pre-fire when a
            # visible enemy's own motion track predicts this exact contested cell.
            contested_enemy = next((
                enemy for enemy in threats
                if _enemy_unit_type_name(enemy) != "RANGER"
                and _kite_motion_info(enemy, pos).get("predicted") == contested
            ), None)
            if contested_enemy is not None:
                shot = _kite_ranger_fire(
                    unit, pos, contested_enemy, contested,
                    obstacle_cells, attack_range,
                )
                if shot is not None:
                    _kite_collision_streak.pop(str(unit.id), None)
                    action, detail = shot
                    _kite_log_decision(
                        unit, unit_kind, mode, objective, threats, action, detail,
                        target=contested_enemy,
                    )
                    _record_kite_stall(uid, pos)
                    return action, detail
    elif (
        collision is not None
        and collision[0] == pos
        and collision[2] >= 2
        and target is None
    ):
        # A contested move without a visible target is a friendly collision:
        # two stacked allies picked the same exit and the server cancelled the
        # pair. Avoiding the cell for one tick only re-creates the identical
        # choice next tick (the old deadlock), so assign the exits by a
        # deterministic uid ordering that is symmetric from both sides: the
        # lowest-ranked unit yields the contested exit and steps to the
        # parity-rotated side cell, while the higher-ranked unit keeps
        # planning normally. The memo makes the decision sticky until the
        # pair actually separates. A lone unit with a residual collision
        # count (no stacked ally) keeps the old behavior: avoid the contested
        # cell for one tick and continue the full safety-assessed planning.
        contested = collision[1]
        collision_avoid = contested
        my_rank = (_stable_slot_index(uid, 2), uid)
        ally_ranks = [
            (_stable_slot_index(str(ally.id), 2), str(ally.id))
            for ally in getattr(turn_context, "units_view", ()) or ()
            if str(ally.id) != uid and tuple(ally.position) == pos
        ]
        # Deterministic pair ordering: the rank key is computed from each
        # unit's own id alone, so both sides see the same total order and
        # exactly one of the stacked units ranks lowest — one yielder and one
        # keeper, without any communication and without touching display
        # names. A single unit (no stacked ally) never yields sideways.
        yield_split = bool(ally_ranks) and my_rank <= min(ally_ranks)
        if (
            ally_ranks
            and friendly_memo is not None
            and friendly_memo[0] == contested
        ):
            # Sticky re-application: keep the already-decided split role.
            yield_split = True
        if yield_split:
            _kite_friendly_split[uid] = (
                contested,
                pos,
                tick + _KITE_FRIENDLY_SPLIT_TICKS,
            )
            split_dir: Direction | None = None
            step_dir = _direction_for_step(pos, contested)
            if step_dir is not None:
                cardinal_order = (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT)
                slot = cardinal_order.index(step_dir)
                side = _stable_slot_index(uid, 2)
                # Parity 0 rotates the contested exit clockwise, parity 1
                # counter-clockwise (both sides compute the same rule from
                # their own uid). The mapping constant is chosen so the
                # common east-bound detour pair separates instead of both
                # picking the route tie-break side.
                side_dir = cardinal_order[(slot - 1) % 4] if side else cardinal_order[(slot + 1) % 4]
                side_cell = (pos[0] + side_dir.delta[0], pos[1] + side_dir.delta[1])
                side_assessment = _kite_cell_assessment(
                    side_cell, threats, obstacle_cells, attack_range,
                )
                if (
                    side_cell not in obstacle_cells
                    and side_cell not in {tuple(enemy.position) for enemy in enemies}
                    and (cell_counts or {}).get(side_cell, 0) < _CELL_UNIT_LIMIT
                    and not (
                        side_assessment["current_hits"]
                        or side_assessment["predicted_hits"]
                    )
                ):
                    split_dir = side_dir
            # A threatened side cell aborts the split: fall through to the
            # normal safety-assessed planning instead of dodging sideways
            # into enemy fire (the memo stays armed and keeps the contested
            # exit blocked for the route/single-step layers below).
            if split_dir is not None:
                split_cell = (
                    pos[0] + split_dir.delta[0], pos[1] + split_dir.delta[1],
                )
                _kite_record_move_attempt(unit, pos, split_cell)
                unit.move(split_dir)
                detail = (
                    f"{split_dir.name} kite-friendly-split contested={contested}"
                )
                _kite_log_decision(
                    unit, unit_kind, mode, objective, threats, "MOVE", detail,
                    target=target,
                )
                _record_kite_stall(uid, pos)
                return "MOVE", detail
        _kite_clear_move_attempt(unit)
    if full_v_vs_r:
        dx = target.position[0] - pos[0]
        dy = target.position[1] - pos[1]
        if abs(dx) + abs(dy) == 1 and not _sweep_fused(unit):
            direction = _cardinal_toward_delta(dx, dy)
            _kite_clear_move_attempt(unit)
            unit.sweep(direction)
            detail = f"{direction.name} full-vanguard-trade ranger={tuple(target.position)}"
            _kite_log_decision(
                unit, unit_kind, mode, objective, threats, "SWEEP", detail, target=target,
            )
            _record_kite_stall(uid, pos)
            return "SWEEP", detail
        direct = _cardinal_toward_delta(dx, dy)
        if direct is not None:
            next_cell = (pos[0] + direct.delta[0], pos[1] + direct.delta[1])
            counts = cell_counts or {}
            if (
                next_cell not in obstacle_cells
                and next_cell not in {tuple(enemy.position) for enemy in enemies}
                and counts.get(next_cell, 0) < _CELL_UNIT_LIMIT
            ):
                _kite_record_move_attempt(unit, pos, next_cell)
                unit.move(direct)
                detail = (
                    f"{direct.name} full-vanguard-moving-dodge "
                    f"ranger={tuple(target.position)}"
                )
                _kite_log_decision(
                    unit, unit_kind, mode, objective, threats, "MOVE", detail,
                    target=target,
                )
                _record_kite_stall(uid, pos)
                return "MOVE", detail
        direction, assessment, candidates = _kite_choose_move(
            unit, pos, tuple(target.position), threats, obstacle_cells,
            attack_range, cell_counts, must_move=True,
        )
        if direction is not None:
            _kite_record_move_attempt(
                unit,
                pos,
                (pos[0] + direction.delta[0], pos[1] + direction.delta[1]),
            )
            unit.move(direction)
            detail = (
                f"{direction.name} full-vanguard-close ranger={tuple(target.position)} "
                f"risk={assessment['current_hits']}/{assessment['predicted_hits']}"
            )
            _kite_log_decision(
                unit, unit_kind, mode, objective, threats, "MOVE", detail,
                target=target, candidates=candidates,
            )
            _record_kite_stall(uid, pos)
            return "MOVE", detail

    # Standing in any current enemy range always yields to movement. This is
    # the central invariant: prefer a missed attack over accepting damage.
    if current_assessment["current_hits"] > 0:
        direction, assessment, candidates = _kite_choose_move(
            unit, pos, objective, threats, obstacle_cells, attack_range,
            cell_counts, must_move=True,
        )
        # Ranger exception: against a melee Vanguard that has already closed
        # to face-to-face, kiting is futile — the Vanguard chases one cell per
        # tick, the Ranger can only retreat one cell, so the gap stays 1 and
        # the Ranger dies having never shot. When every escape cell still
        # predicts a hit (running does not break contact), stand and lead-fire
        # the enemy's next cell instead of fleeing into the same damage.
        if (
            unit_kind == "ranger"
            and target is not None
            and _enemy_unit_type_name(target) == "VANGUARD"
            and assessment["predicted_hits"] > 0
        ):
            motion = _kite_motion_info(target, pos)
            expected = motion.get("predicted")
            shot = _kite_ranger_fire(
                unit, pos, target, expected, obstacle_cells, attack_range,
            )
            # The enemy's predicted cell may be the Ranger's own cell
            # (distance 0, face-to-face charge): lead-fire is illegal there,
            # so fall back to shooting the enemy's current cell instead of
            # fleeing into the same damage.
            if shot is None:
                shot = _kite_ranger_fire(
                    unit, pos, target, None, obstacle_cells, attack_range,
                )
            if shot is not None:
                action, detail = shot
                _kite_clear_move_attempt(unit)
                _kite_log_decision(
                    unit, unit_kind, mode, objective, threats, action, detail,
                    target=target, candidates=candidates,
                )
                _record_kite_stall(uid, pos)
                return action, detail
        if direction is not None:
            _kite_record_move_attempt(
                unit, pos,
                (pos[0] + direction.delta[0], pos[1] + direction.delta[1]),
            )
            unit.move(direction)
            detail = (
                f"{direction.name} kite-evade current={current_assessment['current_hits']} "
                f"next={assessment['current_hits']}/{assessment['predicted_hits']}"
            )
            _kite_log_decision(
                unit, unit_kind, mode, objective, threats, "MOVE", detail,
                target=target, candidates=candidates,
            )
            _record_kite_stall(uid, pos)
            return "MOVE", detail
        _kite_clear_move_attempt(unit)
        unit.wait()
        detail = (
            f"kite-evade-blocked current={current_assessment['current_hits']} "
            "skip-attack"
        )
        _kite_log_decision(
            unit, unit_kind, mode, objective, threats, "WAIT", detail,
            target=target, candidates=candidates,
        )
        _record_kite_stall(uid, pos)
        return "WAIT", detail

    if target is not None and unit_kind == "vanguard":
        etype = _enemy_unit_type_name(target)
        dx = int(target.position[0]) - pos[0]
        dy = int(target.position[1]) - pos[1]
        motion = _kite_motion_info(target, pos)
        # V vs V straight line with one gap: sweep the entry cell now.
        if (
            etype == "VANGUARD"
            and motion["state"] == "advance"
            and ((abs(dx) == 2 and dy == 0) or (abs(dy) == 2 and dx == 0))
        ):
            direction = _cardinal_toward_delta(dx, dy)
            gap = (pos[0] + direction.delta[0], pos[1] + direction.delta[1])
            if gap not in obstacle_cells:
                _kite_clear_move_attempt(unit)
                unit.sweep(direction)
                detail = f"{direction.name} kite-prefire-gap enemy={tuple(target.position)}"
                _kite_log_decision(
                    unit, unit_kind, mode, objective, threats, "SWEEP", detail, target=target,
                )
                _record_kite_stall(uid, pos)
                return "SWEEP", detail
        # Diagonal V vs V: cover the historically preferred bridge cell.
        if (
            etype == "VANGUARD"
            and motion["state"] == "advance"
            and abs(dx) == 1 and abs(dy) == 1
        ):
            preferred, alternate = _kite_diagonal_sweep_directions(pos, target)
            direction = next((
                candidate for candidate in (preferred, alternate)
                if (pos[0] + candidate.delta[0], pos[1] + candidate.delta[1])
                not in obstacle_cells
            ), None)
            if direction is not None:
                _kite_clear_move_attempt(unit)
                unit.sweep(direction)
                detail = f"{direction.name} kite-prefire-diagonal enemy={tuple(target.position)}"
                _kite_log_decision(
                    unit, unit_kind, mode, objective, threats, "SWEEP", detail, target=target,
                )
                _record_kite_stall(uid, pos)
                return "SWEEP", detail
        if abs(dx) + abs(dy) == 1:
            # Workers cannot retaliate; combat targets would have triggered
            # current-range evasion above (except the full-health Ranger case).
            if etype in {"WORKER", "CORE"} and not _sweep_fused(unit):
                direction = _cardinal_toward_delta(dx, dy)
                _kite_clear_move_attempt(unit)
                unit.sweep(direction)
                detail = f"{direction.name} kite-safe-sweep {etype.lower()}={tuple(target.position)}"
                _kite_log_decision(
                    unit, unit_kind, mode, objective, threats, "SWEEP", detail, target=target,
                )
                _record_kite_stall(uid, pos)
                return "SWEEP", detail

    if target is not None and unit_kind == "ranger":
        motion = _kite_motion_info(target, pos)
        # Production outcomes favor current-cell shots against enemy Rangers
        # (151 counterfactual harms vs 33 improvements); keep lead fire for
        # targets whose motion prediction has shown a net benefit.
        expected = (
            motion.get("predicted")
            if _enemy_unit_type_name(target) != "RANGER"
            and (motion["state"] in {"advance", "retreat"} or motion.get("oscillating"))
            else None
        )
        shot = _kite_ranger_fire(
            unit, pos, target, expected, obstacle_cells, attack_range,
        )
        if shot is None and (
            _enemy_unit_type_name(target) == "RANGER"
            or motion["state"] in {"stationary", "unknown"}
            or motion.get("oscillating")
        ):
            shot = _kite_ranger_fire(
                unit, pos, target, None, obstacle_cells, attack_range,
            )
        if shot is not None:
            action, detail = shot
            _kite_clear_move_attempt(unit)
            _kite_log_decision(
                unit, unit_kind, mode, objective, threats, action, detail, target=target,
            )
            _record_kite_stall(uid, pos)
            return action, detail

    # No safe/valuable attack: kite units keep advancing their squad objective;
    # a solo guerrilla resumes its own bearing instead of converging on a
    # shared coordinate. Tactical evasion is per-unit in both modes.
    if solo and target is None:
        locked_core = _guerrilla_core_target.get(uid)
        if locked_core is not None:
            core_cell, last_seen = locked_core
            if (
                not threats
                and tick - last_seen <= _GUERRILLA_CORE_MEMORY_TICKS
                and pos != core_cell
                and _core_position_unguarded(core_cell, enemies)
            ):
                # The core is only briefly out of sight (typically a wall
                # corner). Keep the normal A* route pointed at its last-seen
                # cell instead of flipping back to the roam bearing.
                objective = core_cell
            else:
                _guerrilla_core_target.pop(uid, None)
        if uid not in _guerrilla_core_target:
            locked_unit = _guerrilla_unit_target.get(uid)
            if locked_unit is not None:
                prey_cell, prey_seen = locked_unit
                if (
                    not threats
                    and tick - prey_seen <= _GUERRILLA_TARGET_MEMORY_TICKS
                    and pos != prey_cell
                ):
                    objective = prey_cell
                else:
                    _guerrilla_unit_target.pop(uid, None)
        if (
            uid not in _guerrilla_core_target
            and uid not in _guerrilla_unit_target
        ):
            roam_action, roam_detail = _guerrilla_roam(
                unit,
                pos,
                obstacle_cells,
                config,
                avoid_cells=(collision_avoid,) if collision_avoid is not None else (),
            )
            _record_kite_stall(uid, pos)
            return roam_action, roam_detail
    must_move = any(_enemy_unit_type_name(enemy) == "RANGER" for enemy in threats)
    move_goal = tuple(target.position) if target is not None else objective
    # Follow a real A* route toward the objective first. The single-step
    # greedy picker below scores cells by Manhattan progress, which oscillates
    # against walls: at cell A it picks the y-axis step that looks marginally
    # closer, at cell B every step scores <=0 so BFS picks the first cell of
    # the real detour (back to A), and the unit shuttles forever. A cached
    # multi-step route keeps the unit committed to one detour direction.
    route_enemy_cells = {tuple(enemy.position) for enemy in local_enemies}

    def _kite_route_step_allowed(cell: tuple[int, int]) -> bool:
        if collision_avoid is not None and cell == collision_avoid:
            return False
        if cell in friendly_memo_cells:
            return False
        if cell in route_enemy_cells:
            return False
        assessment = _kite_cell_assessment(
            cell, threats, obstacle_cells, attack_range,
        )
        return not (
            assessment["current_hits"] or assessment["predicted_hits"]
        )

    route_counts = cell_counts or {}
    route_blocked_steps: set[tuple[int, int]] = set()
    for candidate in (
        Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT,
    ):
        cell = (
            pos[0] + candidate.delta[0],
            pos[1] + candidate.delta[1],
        )
        if (
            route_counts.get(cell, 0) >= _CELL_UNIT_LIMIT
            or not _kite_route_step_allowed(cell)
        ):
            route_blocked_steps.add(cell)
    route_blocked_steps.update(friendly_memo_cells)
    # The route layer has to obey the same no-U-turn rule as the single-step
    # chooser below. With only the chooser disciplined, the two disagreed and
    # the unit cycled a 3-cell pocket instead of a 2-cell one (observed after
    # that first fix: ``kite-position RIGHT`` one Tick, cached ``kite-route
    # LEFT`` straight back the next). Applies only while the unit is stalled —
    # a healthy march may legitimately double back.
    _came_from = _kite_prev_pos.get(uid)
    if (
        _came_from is not None
        and _came_from != pos
        and _kite_stall_ticks.get(uid, 0) >= _KITE_STALL_UNLOCK_TICKS
    ):
        route_blocked_steps.add(_came_from)
    bfs_step = _move_towards(
        unit, pos, move_goal, obstacle_cells,
        detail_prefix="kite-route",
        cell_counts=cell_counts,
        extra_obstacles=route_blocked_steps,
        step_filter=_kite_route_step_allowed,
        max_steps=int(config.get("bfs_max_steps", 2500)),
    )
    if bfs_step is not None:
        action, detail = bfs_step
        # Record the attempted target cell so a same-tick collision (the
        # unit stays on pos next tick) is detected next tick.
        try:
            _route_dir = Direction[detail.split()[0]]
        except (KeyError, IndexError):
            _route_dir = None
        if _route_dir is not None:
            _kite_record_move_attempt(
                unit, pos,
                (pos[0] + _route_dir.delta[0], pos[1] + _route_dir.delta[1]),
            )
        _kite_log_decision(
            unit, unit_kind, mode, objective, threats, action, detail,
            target=target,
        )
        _record_kite_stall(uid, pos)
        return action, detail
    # No multi-step route (open field or A* exhausted): fall back to the
    # single-step safety/progress picker, then to a forced unstick step.
    direction, assessment, candidates = _kite_choose_move(
        unit, pos, move_goal, threats, obstacle_cells, attack_range,
        cell_counts,
        must_move=must_move,
        avoid_cells=(
            *((collision_avoid,) if collision_avoid is not None else ()),
            *friendly_memo_cells,
        ),
        stall_ticks=_kite_stall_ticks.get(uid, 0),
        global_enemies=enemies,
    )
    if direction is not None:
        _next_cell = (pos[0] + direction.delta[0], pos[1] + direction.delta[1])
        _kite_record_move_attempt(unit, pos, _next_cell)
        unit.move(direction)
        detail = (
            f"{direction.name} kite-position goal={move_goal} "
            f"risk={assessment['current_hits']}/{assessment['predicted_hits']}"
            + (" shuttle-break" if assessment.get("shuttle_break") else "")
        )
        _kite_log_decision(
            unit, unit_kind, mode, objective, threats, "MOVE", detail,
            target=target, candidates=candidates,
        )
        _record_kite_stall(uid, pos)
        return "MOVE", detail
    # Single-step selection would WAIT here. When the unit is cornered by
    # walls (every adjacent cell is a dead end relative to the objective),
    # WAIT wins forever and the unit never leaves the pocket. Fall back to a
    # real A* search so the squad can still navigate out of cul-de-sacs.
    unstick = _kite_bfs_unstick(
        unit, pos, move_goal, obstacle_cells, cell_counts,
        extra_obstacles=route_blocked_steps,
        step_filter=_kite_route_step_allowed,
        max_steps=int(config.get("bfs_max_steps", 2500)),
    )
    if unstick is not None:
        action, detail = unstick
        _kite_log_decision(
            unit, unit_kind, mode, objective, threats, action, detail,
            target=target, candidates=candidates,
        )
        _record_kite_stall(uid, pos)
        return action, detail
    _kite_clear_move_attempt(unit)
    unit.wait()
    detail = f"kite-hold-safe objective={objective}"
    _kite_log_decision(
        unit, unit_kind, mode, objective, threats, "WAIT", detail,
        target=target, candidates=candidates,
    )
    _record_kite_stall(uid, pos)
    return "WAIT", detail


def _plan_guerrilla_combat(
    unit: Any,
    *,
    unit_kind: str,
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    config: dict[str, Any],
    cell_counts: Mapping | None = None,
) -> tuple[str, str]:
    """Run the kite combat policy independently for this one unit.

    Unlike the kite squad, this planner never reads squad directives or a
    shared kite objective.  The common combat policy still handles danger-zone
    avoidance, motion prediction, pre-fire, and target selection; exposed
    (unguarded) enemy Cores are the only Core targets admitted to its local
    pool.  The fallback bearing is derived separately for each unit.
    """
    return _plan_kite_combat(
        unit,
        unit_kind=unit_kind,
        enemies=enemies,
        obstacle_cells=obstacle_cells,
        config=config,
        cell_counts=cell_counts,
        solo=True,
    )


def _plan_vanguard(
    vanguard,
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    config: dict[str, Any],
    *,
    core_pos: tuple[int, int],
    team: str,
    cell_counts: Mapping | None = None,
) -> tuple[str, str]:
    """Dispatch a Vanguard according to its combat team assignment.

    `obstacle_cells` may be an `_ObstacleOverlay` (wall set + the unit's 1-2
    CELL_UNIT_LIMIT / MOVE_CONTESTED refusal cells). Every membership test and
    every `_get_dead_ends` / `_bfs_path` call below sees the folded union, while
    the dead-end structure keeps its wall-set identity (see report #8).
    """
    if team == "home":
        return _plan_home_combat(
            vanguard,
            unit_kind="vanguard",
            enemies=enemies,
            obstacle_cells=obstacle_cells,
            core_pos=core_pos,
            config=config,
            cell_counts=cell_counts,
        )
    if team == "attack":
        return _plan_attack_combat(
            vanguard,
            unit_kind="vanguard",
            enemies=enemies,
            obstacle_cells=obstacle_cells,
            config=config,
            cell_counts=cell_counts,
            core_pos=core_pos,
        )
    if team == "kite":
        return _plan_kite_combat(
            vanguard,
            unit_kind="vanguard",
            enemies=enemies,
            obstacle_cells=obstacle_cells,
            config=config,
            cell_counts=cell_counts,
        )
    if team == "guerrilla":
        return _plan_guerrilla_combat(
            vanguard,
            unit_kind="vanguard",
            enemies=enemies,
            obstacle_cells=obstacle_cells,
            config=config,
            cell_counts=cell_counts,
        )

    # Unassigned units keep a conservative default: local scout only.
    pos = tuple(vanguard.position)
    sweep = _vanguard_adjacent_sweep(vanguard, pos, enemies)
    if sweep is not None:
        return sweep
    return _scout_cardinal(
        vanguard, pos, obstacle_cells, config, label="unassigned-scout",
    )


def _plan_ranger(
    ranger,
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    config: dict[str, Any],
    *,
    core_pos: tuple[int, int],
    team: str,
    cell_counts: Mapping | None = None,
) -> tuple[str, str]:
    """Dispatch a Ranger according to its combat team assignment.

    See _plan_vanguard: `obstacle_cells` may carry an `_ObstacleOverlay` with the
    unit's transient refusal cells.
    """
    if team == "home":
        return _plan_home_combat(
            ranger,
            unit_kind="ranger",
            enemies=enemies,
            obstacle_cells=obstacle_cells,
            core_pos=core_pos,
            config=config,
            cell_counts=cell_counts,
        )
    if team == "attack":
        return _plan_attack_combat(
            ranger,
            unit_kind="ranger",
            enemies=enemies,
            obstacle_cells=obstacle_cells,
            config=config,
            cell_counts=cell_counts,
            core_pos=core_pos,
        )
    if team == "kite":
        return _plan_kite_combat(
            ranger,
            unit_kind="ranger",
            enemies=enemies,
            obstacle_cells=obstacle_cells,
            config=config,
            cell_counts=cell_counts,
        )
    if team == "guerrilla":
        return _plan_guerrilla_combat(
            ranger,
            unit_kind="ranger",
            enemies=enemies,
            obstacle_cells=obstacle_cells,
            config=config,
            cell_counts=cell_counts,
        )

    pos = tuple(ranger.position)
    shot = _ranger_best_shot(
        ranger,
        pos,
        enemies,
        obstacle_cells,
        int(config["ranger_attack_range"]),
        lead_fire_enabled=bool(config.get("ranger_lead_fire_enabled", True)),
    )
    if shot is not None:
        return shot
    return _scout_cardinal(
        ranger, pos, obstacle_cells, config, label="unassigned-scout",
    )


# ── top-level tactic ─────────────────────────────────────────────────────────

# ── shared map memory (persists across ticks + process restarts) ───────────
_resource_memory: set[tuple[int, int]] = set()
# Resources confirmed absent but not yet flushed to map_memory.json. Tombstones
# prevent sticky manual entries on disk from being merged back during a save.
_resource_tombstones: set[tuple[int, int]] = set()
# Permanent obstacles: once seen, always blocked
_obstacle_memory: set[tuple[int, int]] = set()
# Enemy sightings: remember every position where enemies were seen
_enemy_memory: set[tuple[int, int]] = set()
# Last-known unit type per sighted position (WORKER/VANGUARD/RANGER/CORE/ENEMY).
# Kept in lockstep with _enemy_memory so an out-of-vision CORE can be told apart
# from a worker scout on the dashboard.
_enemy_memory_types: dict[tuple[int, int], str] = {}
# Last-seen tick per sighted position, kept in lockstep with _enemy_memory so
# the arena page can show only the most recently observed sightings.
_enemy_memory_ticks: dict[tuple[int, int], int] = {}
# Consecutive visible positions keyed by full enemy UUID. This is intentionally
# in-memory only; reconnects must rebuild confidence from fresh observations.
_enemy_motion_tracks: dict[str, list[tuple[int, tuple[int, int]]]] = {}
# Home-team intercept target lock: unit id -> (enemy id, last-visible tick).
# Keeps a defender chasing a foe that slipped out of vision for a few ticks
# instead of flipping back into patrol the instant it disappears.
_home_engage_target: dict[str, tuple[str, int]] = {}
# Solo guerrilla exposed-core lock: unit id -> (last-seen core cell, tick).
# Keeps the A* route committed when a wall corner makes the core flicker out
# of sight instead of flipping back to the roam bearing.
_guerrilla_core_target: dict[str, tuple[tuple[int, int], int]] = {}
# Solo guerrilla flicker lock for non-core prey: unit id -> (last-seen enemy
# cell, tick). The Core lock above only covers Cores, so a worker or combat
# unit behind a corner left every blind Tick to the roam bearing, which stepped
# straight back into sight range and restarted the cycle (see
# _GUERRILLA_TARGET_MEMORY_TICKS).
_guerrilla_unit_target: dict[str, tuple[tuple[int, int], int]] = {}
_pending_shot_predictions: list[dict[str, Any]] = []
# Last applied dashboard enemy-clear sequence from map_memory.json.
_enemy_clear_seq: int = 0
# Drift-leash anchor for the Core: the spot the automatic movement heuristic
# may not drift beyond (see core_max_drift). Persisted to map_memory.json so a
# restart never re-anchors an already-migrated base.
_core_anchor: tuple[int, int] | None = None
# Last core step we issued: (pos, direction, tick). A CORE_DESTINATION_
# TERRAIN_BLOCKED rejection on the next Tick is attributed to pos+delta so the
# blocked cell lands in obstacle memory instead of being re-issued forever
# (observed: 4900+ identical rejections while the base sat next to a wall).
_core_pending_move: tuple[tuple[int, int], "Direction", int] | None = None
# Tick until which automatic core movement is suppressed after a
# CORE_ALREADY_MOVING rejection (usually a manual/auto command race): yield to
# the other driver instead of re-issuing the same rejected start_move.
_core_move_hold_until_tick: int = 0
# How long a CELL_UNIT_LIMIT rejection suppresses core movement. The rejected
# step is blocked by our own units, which move on their own, so a short hold
# beats hammering: each attempt re-enters MOVING and a MOVING Core rejects every
# worker DEPOSIT, which is how an 11-hour freeze looked in the field.
_CORE_MOVE_HOLD_TICKS = 4
# Core danger freeze: a migrating Core cannot repair or heal (CORE_MOVING), and
# production C1 kept stepping east toward its manual target for 30 Ticks after
# 4+ Rangers appeared 8 cells away, then was shot from 10 HP+shield to dead in
# 6 Ticks (t649639-t649645). While a combat enemy is within this Chebyshev
# radius the Core stops starting new steps; the freeze lingers a while after
# the last sighting because campers slip in and out of vision.
_CORE_DANGER_RADIUS = 12
_CORE_DANGER_LINGER_TICKS = 40
_core_danger_until: int = 0
# After a respawn the manual core target still points at the old base (C2
# respawned 1,500 cells away and spent 74% of 545 Ticks MOVING toward it,
# depositing 4 resources in total). Suppress the manual target until the
# operator saves a new one or this many Ticks pass.
_CORE_RESPAWN_TARGET_SUPPRESS_TICKS = 3000
_core_respawn_suppress: dict[str, Any] = {}
# Worker MOVE_CONTESTED repeat tracker: {uid: (pos, contested_cell, count)}.
# Two workers whose deterministic plans submit the same cell every Tick cancel
# both moves every Tick (observed 30+ identical rejections); after two
# identical (pos, cell) rejections the worker backs off for one Tick so the
# peer's move can finally land.
_worker_contest_streak: dict[str, tuple[tuple[int, int], tuple[int, int], int]] = {}
# Where a worker's last planned step was headed: ``full unit id -> (origin, dest)``.
# The server reports MOVE_CONTESTED at the actor's OWN cell, so the rejection
# event alone cannot say which neighbour was refused — the plan that produced it
# can. Kept so a repeat offender can blacklist the destination instead of
# re-issuing the same refused step forever.
_worker_planned_step: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {}
# Destinations refused this many consecutive Ticks get parked for the carrier,
# and for how long. Observed: W5 (49797bf4) fired the same MOVE:RIGHT out of
# (279,-424) for 118 straight Ticks, 57 of the last 60 warn rows in
# battle_log — an invisible claimant holds that cell and the carrier has no way
# to learn that from a rejection that names only its own cell. The park is a SET
# of refused cells: a unit boxed in on several sides (V27 logged 3456
# MOVE_CONTESTED re-issuing the one non-parked direction) must learn every
# refused neighbour, not just the first.
_WORKER_CONTEST_BLOCK_AFTER = 4
_WORKER_CONTEST_BLOCK_TICKS = 60
_worker_contest_blocked: dict[str, tuple[frozenset[tuple[int, int]], int]] = {}
# Rejections counted per origin cell: ``full unit id -> (origin, count)``. Kept
# across the waiting Tick (which emits no rejection) so a worker that keeps
# losing the same fight actually reaches the block threshold.
_worker_contest_repeat: dict[str, tuple[tuple[int, int], int]] = {}
# Attack-team units currently in sticky home-recovery (regroup) mode: once a
# march has failed, keep walking home until the Core cell is reached instead of
# re-attempting the target march every Tick (that A-B-A'd against the regroup
# step at wall corners — observed 60+ identical UP/DOWN flips). Entries linger
# after unit death (bounded by total units ever regrouped; new units carry
# fresh UUIDs so stale ids can never mis-arm a respawn).
_attack_regroup_ids: set[str] = set()
# Cached A* path for the manual core target (goal + full cell path). Reused
# while the Core stays on it; recomputed when the target changes, the Core
# leaves the path, or a newly learned wall blocks the next step.
_core_target_path_cache: dict[str, Any] = {}
# A manual-target A* that exhausts its budget without a path used to re-run the
# identical failing search every Tick (~1s/Tick at an 84,900-expansion budget,
# 82% of live Ticks — the source of COMMAND_WINDOW_CLOSED rejections). Walls only
# ever grow, so a failed search stays failed; cache the negative result and retry
# only after this backoff (a target change clears it immediately).
_CORE_PATH_FAIL_BACKOFF = 60
# Recent ally-death heat: cell -> Tick a friendly unit died there. A mine near
# a death stays banned after the campers slip out of vision, so workers are not
# sent straight back into the same ambush (observed: ~20 workers killed at one
# camped mine cluster while the distance-only assignment kept refilling it).
_death_heat: dict[tuple[int, int], int] = {}
_DEATH_HEAT_TTL = 150
# Hysteresis ban on dangerous mines: cell -> Tick the ban lifts. Refreshed every
# Tick a mine is covered by enemy fire or sits by a recent death; once the
# threat leaves, the ban persists a while so assignments stop flickering as
# campers mill in and out of the old "enemy within 1 cell" filter.
_mine_danger_ban: dict[tuple[int, int], int] = {}
_MINE_DANGER_BAN_TICKS = 60
# Throttle for the economic-defence situation row in battle_log so the review
# can track the ban/heat/sortie state over time without flooding the 2MB budget.
_last_defense_log_tick = -(1 << 30)
_DEFENSE_LOG_EVERY = 100
# Previous Tick's unit snapshot (full uid -> (stable name, position)) used to
# detect deaths for the heat map and to keep a dead unit's name for the battle
# log (the name registry is pruned before events are classified).
_prev_unit_snapshot: dict[str, tuple[str, tuple[int, int]]] = {}
# Signature of the last dashboard map edits we absorbed (avoid re-applying every tick).
_last_dashboard_map_sig: tuple | None = None
# Track each worker's previous position to avoid backtracking
_worker_last_pos: dict[str, tuple[int, int]] = {}
_worker_recent: dict[str, list[tuple[int, int]]] = {}  # last 4 positions, anti-oscillation
# Long-tail memory of the explore fan-out only: the 4-position `_worker_recent`
# window is shorter than the rings it is meant to break, so a worker re-picks
# the same circuit forever. Age over this tail makes the never-walked exit of a
# pocket outrank the ring successor.
_worker_explore_history: dict[str, list[tuple[int, int]]] = {}
_WORKER_EXPLORE_MEMORY = 24
# Sticky "walk back to the mining zone" flag, set by _worker_rally_goal.
_worker_rally_home: dict[str, bool] = {}
# Resource assignment: each resource assigned to closest worker only
_resource_assignments: dict[str, tuple[int, int]] = {}
# Worker A* path cache: reuse a computed path across ticks instead of recomputing
# from scratch every tick. Keyed by full str(worker.id); entry {goal, path}.
_worker_path_cache: dict[str, dict] = {}
# Loaded workers whose far-from-home A* route to the Core missed, at the
# distance where it missed. Wall memory only grows, so re-running a search that
# costs tens of thousands of expansions is only worth it after real progress.
_cargo_route_miss_dist: dict[str, int] = {}
_CARGO_HOME_BUDGET_FACTOR = 60
# Kept below the attack-regroup march's 1.5M cap: up to target_workers carriers
# can each search in one Tick, so one route home must stay ~1s of planning.
_CARGO_HOME_BUDGET_MAX = 200_000
_CARGO_HOME_RETRY_GAP = 15
# Within this range of the Core a missed route is usually transient congestion
# around the unloading chute, not a wall, so the miss is not recorded at all.
_CARGO_HOME_MISS_FAR = 30
# Combat units use the same map search but keep a separate cache because their
# goals change independently as enemies move or team assignments change.
_combat_path_cache: dict[str, dict] = {}
# Same-tick move collision tracker for kite/guerrilla units. When two units
# (or a unit and an enemy) both submit a move into the same cell on the same
# tick, the server cancels both moves and neither advances. If the unit keeps
# targeting the same contested cell from the same position, that is a deadlock.
# After two such cancelled attempts, the enemy's next move into that cell is
# predictable, so the unit attacks the cell directly (Vanguard sweep / Ranger
# shoot_cell) instead of retrying the doomed move.
# {uid: (pos, contested_cell, count)}
_kite_collision_streak: dict[str, tuple[tuple[int, int], tuple[int, int], int]] = {}
# Consecutive ticks a kite/guerrilla unit did not really advance, tracked by
# the single entry point _record_kite_stall (called on EVERY exit path of
# _plan_kite_combat, including early returns). _kite_stall_pos is the stall
# anchor: the counter keeps accumulating while the unit stays within
# _KITE_STALL_DRIFT Chebyshev of the anchor (MOVE_CONTESTED ±1 micro-jitter
# must not restart the unlock window), and only a genuine move of at least
# _KITE_STALL_DRIFT + 1 cells resets the counter and re-anchors. Read-only
# stall input: once a threat-free unit has been frozen for
# _KITE_STALL_UNLOCK_TICKS ticks, _kite_choose_move stops letting WAIT's
# progress=0 beat a safe detour step's progress=-1 (the remote scoring
# deadlock).
_kite_stall_pos: dict[str, tuple[int, int]] = {}
_kite_stall_ticks: dict[str, int] = {}
_KITE_STALL_UNLOCK_TICKS = 12
# Ceiling for the cover contract. While any ally sees an enemy the short window
# above never opens, and the army reports a sighting on ~70% of combat Ticks —
# so a threat-free unit that only has memory-dead-ended exits left froze for
# hours (V10: 1,670+ Ticks, enemies=[] every Tick). Past this many frozen Ticks
# the detour unlocks anyway; a hold that lasts this long is not cover, it is a
# freeze. 180 Ticks ≈ 48 min of real time at the observed cadence.
_KITE_STALL_HARD_UNLOCK_TICKS = 180
_KITE_STALL_DRIFT = 1
# Where each kite unit stood when it last planned, so the single-step chooser
# can rank a step back onto that cell below every other equally safe step.
_kite_prev_pos: dict[str, tuple[int, int]] = {}
# Confirmed A-B-A shuttle breaker. The stall unlock above is deliberately
# threat-free only, so exactly the worst case slips through: a unit ping-ponging
# across an enemy's threat ring sees a threat, never trips the unlock and shuttles
# forever (observed after the 5th deploy: R7 between (934,-45) and (934,-44) and
# V1 between (327,-174) and (327,-173), 36 Ticks each way, net displacement 0,
# MOVE every single Tick). Past this many *strict reversals* in a row — pos equal
# to where the unit stood two Ticks ago while having actually moved last Tick —
# the single-step chooser drops the "undo my last step" candidate. Safety
# ranking is untouched; what goes is the option, not the contract.
# Counting reversals and not "ticks without progress" is the field correction
# after the 7th deploy: the stall-tick version fired 8 times on V4, a unit
# deliberately holding on `kite-hold-safe`, poking it out of its chosen safe cell
# every ~16 Ticks. A hold is not a shuttle, and only a hold-free unit that keeps
# undoing itself deserves the break.
_KITE_SHUTTLE_BREAK_REVERSALS = 3
_kite_shuttle_ticks: dict[str, int] = {}
# The planned position from two Ticks ago, kept only to detect the reversal.
_kite_prev_prev_pos: dict[str, tuple[int, int]] = {}


def _record_kite_stall(uid: str, pos: tuple[int, int]) -> None:
    """Single entry point for the kite anchor-based stall counter.

    Called on every exit path of _plan_kite_combat (including early returns)
    so a planned action that still left the unit near its anchor counts as a
    stall tick and the WAIT-unlock threshold cannot be bypassed by any
    branch. Positions within _KITE_STALL_DRIFT Chebyshev of the anchor are
    micro-jitter (e.g. the occasional ±1 MOVE_CONTESTED escape step) and keep
    accumulating; only a net displacement of at least _KITE_STALL_DRIFT + 1
    counts as a real move, resetting the counter and re-anchoring.

    Also the single place that sees the per-unit planned-position trail, so it
    keeps the strict-reversal counter the shuttle breaker reads: ``pos`` equal to
    the position from two Ticks ago *while last Tick's position differed*. A
    unit holding still matches that test trivially (pos == prev == prev_prev),
    and a deliberate hold is not a shuttle — that distinction is what the
    stall-tick version of the gate got wrong for V4.
    """
    anchor = _kite_stall_pos.get(uid)
    if (
        anchor is not None
        and max(abs(pos[0] - anchor[0]), abs(pos[1] - anchor[1]))
        <= _KITE_STALL_DRIFT
    ):
        _kite_stall_ticks[uid] = _kite_stall_ticks.get(uid, 0) + 1
    else:
        _kite_stall_pos[uid] = pos
        _kite_stall_ticks[uid] = 0
    prev = _kite_prev_pos.get(uid)
    prev_prev = _kite_prev_prev_pos.get(uid)
    if prev is not None and prev_prev is not None and prev != pos and prev_prev == pos:
        _kite_shuttle_ticks[uid] = _kite_shuttle_ticks.get(uid, 0) + 1
    else:
        _kite_shuttle_ticks[uid] = 0
    _kite_prev_prev_pos[uid] = prev
    _kite_prev_pos[uid] = pos


# Friendly same-cell split memos: {uid: (contested_cell, origin, expire_tick)}.
# Two stacked allies picking the same exit cancel each other server-side every
# tick; the loser of a deterministic display-name comparison keeps the
# contested cell avoided (route/route-steps/roam) until expiry, and while it
# still stands on ``origin`` the split decision is re-applied every tick —
# collision events never reset it before the pair actually separates.
_kite_friendly_split: dict[str, tuple[tuple[int, int], tuple[int, int], int]] = {}
_KITE_FRIENDLY_SPLIT_TICKS = 6
# Guerrilla roam wall ping-pong escape: recent positions per unit and a
# countdown of deflected-bearing ticks once the oscillation is detected.
_roam_pos_history: dict[str, list[tuple[int, int]]] = {}
_roam_escape_left: dict[str, int] = {}
_roam_stall_cells: dict[str, frozenset[tuple[int, int]]] = {}
_roam_stall_origin: dict[str, tuple[int, int]] = {}
_roam_bearing_offset: dict[str, int] = {}
# Escape bearing-flip bookkeeping: a deflection step out of the oscillation
# region only arms a pending flip; the flip itself executes once the NEXT
# tick's pos confirms the server accepted the move (a cancelled first step
# leaves the unit inside the region and the pending flag stays inert).
# The first confirmed escape reverses the bearing; every later one turns it a
# quarter instead, so a unit that keeps re-trapping keeps changing its route
# instead of replaying the heading that walked it back into the pocket.
_roam_flip_pending: dict[str, bool] = {}
_roam_flips_used: dict[str, int] = {}
# Durable memory of the pockets that proved to be oscillation traps, as
# ``cell -> expiry tick``. The active escape drops its region the moment the
# _ROAM_ESCAPE_TICKS budget runs out, and the ID-derived base bearing then
# pulls the unit straight back into the same bounce (observed: R2 ping-ponging
# (975,-1175)/(975,-1174) with the escape re-arming every 8 ticks — 9 distinct
# cells in 625 ticks). Banned cells stay out of the roam choices for
# _ROAM_BAN_TICKS, which is what turns one escape into an actual route change.
_roam_banned: dict[str, dict[tuple[int, int], int]] = {}
_ROAM_BAN_TICKS = 120
# An 8-position-change window spanning <=2 cells proves an A-B-A bounce (the
# window counts recorded position changes, not ticks; 8 also catches a
# stationary unit, whose escape candidates just degrade into normal roaming,
# so harmless). Windows spanning 3 cells also count, but only when the net
# window displacement (Chebyshev) stays <= _ROAM_STALL_DRIFT: that catches
# 3-cell loop oscillations (A-B-C-A, e.g. the (270,-744)/(270,-743)/(269,-744)
# cycle) while genuine outward roaming keeps drifting beyond the bound and
# never trips. The region remembered on trigger keeps this many most-recent
# cells so the escape also avoids the approach path back into the bounce.
_ROAM_STALL_WINDOW = 8
_ROAM_STALL_CELLS = 2
_ROAM_STALL_DRIFT = 2
# A window that never leaves a 3x3 box proves circling even when it visits more
# cells than _ROAM_STALL_CELLS (the 2x2 perimeter the <=2-cell and drift tests
# both miss). Extent rather than drift, because a box cycle keeps returning to
# its start while a genuine advance also has small first-to-last drift.
_ROAM_STALL_BOX_CELLS = 4
_ROAM_STALL_BOX_EXTENT = 2
# Keep a longer tail than the detection window so the remembered region also
# covers the approach path into the bounce (the escape avoids re-entering it).
_ROAM_STALL_REGION = 16
_ROAM_ESCAPE_TICKS = 6
# Long-horizon orbit detector. ``_ROAM_STALL_WINDOW`` proves a two-cell bounce;
# it cannot see a unit circling a whole pocket, which is what the route recorder
# caught in production: V2 re-walked 426 cells for 5170 Ticks (150x48, ~468-Tick
# period), V13 471 cells in 102x102, V15 25 cells in an 18x5 box. The proof is a
# stall in NEW ground: those three went 2,120 / 4,559 / 4,703 recorded steps
# between their last two new cells, while every unit that kept exploring stayed
# under 361. ``_ROAM_ORBIT_STALL_STEPS`` sits inside that gap.
_ROAM_ORBIT_STALL_STEPS = 500
# The loop handed to the escape, and the bound on the discovery ledger (a unit
# covers ~4,700 new cells a day, so this is a few days of ground per unit).
_ROAM_ORBIT_REGION = 200
_ROAM_ORBIT_MEMORY = 8000
# Walking out of a 150x48 pocket takes more steps than the 6-Tick bounce budget
# allows, so an orbit escape gets its own longer budget.
_ROAM_ORBIT_ESCAPE_TICKS = 120
_roam_discovered: dict[str, dict[tuple[int, int], None]] = {}
_roam_new_stall: dict[str, int] = {}
_roam_recent: dict[str, deque] = {}
_roam_break_tick: dict[str, int] = {}
# Frontier roaming memory: every cell this unit has stood on since the process
# started, bounded FIFO. Unlike _roam_discovered it is NOT reset when an orbit is
# broken — the detector wants a fresh baseline after an escape, but a unit that
# is choosing where to walk next must keep remembering the ground it came from.
_ROAM_FRONTIER_LOOKAHEAD = 3
_ROAM_FRONTIER_MEMORY = 8000
_roam_seen: dict[str, dict[tuple[int, int], None]] = {}
# Consecutive ticks a worker has not moved — triggers un-stick recovery.
# _worker_stuck_ticks counts consecutive same-position ticks; _worker_stuck_pos
# remembers the position those ticks were counted at (independent of last-pos,
# which only tracks moves and would miss a worker frozen from the start).
_worker_stuck_ticks: dict[str, int] = {}
_worker_stuck_pos: dict[str, tuple[int, int]] = {}
_STUCK_THRESHOLD = 8
# Delivery lease: uid of the single carrier allowed to contest the core cell
# (unloading chute) this tick; recomputed every Tick by _assign_delivery_lease.
# None when no carrier exists (or planning runs outside choose_actions).
_delivery_lease_uid: str | None = None
# Second role of the delivery pipeline: the carrier that pre-positions one
# cell from the chute and chains in the same tick the current occupant steps
# out. Recomputed by _assign_delivery_lease.
_delivery_next_uid: str | None = None
# True when the unit standing on the core cell this tick is going to leave it
# (empty worker -> vacate-core, healer/non-carrier -> yield-chute). The server
# resolves moves as one global dependency graph, so a carrier may enter the
# occupied chute in that same tick — its move chains behind the departure.
_chute_vacating_this_tick = False
# True when the lease holder stands adjacent (distance <= 1) to the chute, so
# the next-in-line yields the entry slot instead of racing for the same cell.
_lease_holder_adjacent = False
# True while any carrier is hauling cargo: empty workers must then route
# AROUND the core cell instead of through it, or a just-vacated worker steps
# right back onto the chute next tick and ping-pongs there forever
# (observed: 15-tick squat loop that shut the whole delivery line).
_chute_in_demand = False
# Position of the next-in-line carrier (recomputed by _assign_delivery_lease).
# A worker vacating the chute prefers stepping toward it so the one-out-one-in
# hand-off never deadlocks on a ring with no free cell.
_delivery_next_pos: tuple[int, int] | None = None
# True when any carrier stands adjacent to the chute: healers squatting on
# the core cell must yield it (yield-chute) so the carrier can chain in.
_carrier_at_chute = False
# Carriers already in the chute's immediate queue (Manhattan distance to the
# Core <= _HEAL_GATE_QUEUE_RADIUS). Only these physically compete for the
# core cell this tick; carriers still mining far away must not gate healing.
_HEAL_GATE_QUEUE_RADIUS = 3
# Names of units commanded HEAL last Tick: while still damaged they keep the
# core cell for one more Tick so their heal finishes before yield-chute.
_healing_units_prev: set[str] = set()
# Names of units commanded a heal-return march last Tick: while still below
# the heal threshold they keep re-claiming their stagger slot in flight.
_heal_return_inflight: set[str] = set()
# Server-side stacking rule: at most 2 units share one cell (CELL_UNIT_LIMIT).
# Retreat/yield decisions squeeze into a cell with 1 occupant instead of
# treating it as fully blocked.
_CELL_UNIT_LIMIT = 2
# Consecutive-Tick CELL_UNIT_LIMIT move rejections per unit id. A unit that
# keeps bouncing on the same packed cell re-tries deterministically forever;
# after _CELL_LIMIT_DETOUR_AFTER such ticks it is forced one sidestep to
# break the loop. Rebuilt from scratch each Tick (only units that failed
# again survive), so it can neither grow unbounded nor go stale.
_cell_limit_streak: dict[str, int] = {}
_CELL_LIMIT_DETOUR_AFTER = 3
# uid -> (cell, trips, valid_until_tick). One sidestep is not a break: the
# patrol plan re-issues the rejected move the next Tick, so the breaker kept
# producing one CELL_UNIT_LIMIT every _CELL_LIMIT_DETOUR_AFTER Ticks for 27,000
# Ticks (V3 onto (685,-302)). While the window is open the cell is a wall for
# this unit only; each repeat trip on the same cell doubles the window (up to
# 8x), so a permanently packed cell is retried ever more rarely. Entries are
# dropped with the unit itself (see _prune_dead_unit_state).
_cell_limit_blocked: dict[str, tuple[tuple[int, int], int, int]] = {}
_CELL_LIMIT_BLOCK_TICKS = 40


def _cell_limit_block_cell(uid: str, tick: int) -> tuple[int, int] | None:
    """The cell ``uid`` must treat as a wall this Tick, or None."""
    block = _cell_limit_blocked.get(uid)
    if block is None or block[2] <= tick:
        return None
    return block[0]


def _attack_contest_cells(uid: str, tick: int) -> frozenset[tuple[int, int]]:
    """The neighbours ``uid`` keeps being refused entry to, or an empty set.

    The invisible-claimant case: a visible blocker already reaches the planners
    through ``adjacent_enemies``, but a claimant outside vision only shows up as
    a MOVE_CONTESTED rejection naming the unit's own cell. Parking the refused
    destinations (the worker backoff's mechanism) makes the march route around
    them instead of re-issuing the doomed step forever. Only the teams in
    ``_CONTEST_PARK_TEAMS`` consume it; kite/guerrilla aim at the claimant's cell
    through ``_kite_collision_streak`` instead.
    """
    block = _worker_contest_blocked.get(uid)
    if block is None or block[1] <= tick:
        return frozenset()
    return block[0]


# Long-march teams with no collision breaker of their own. Production V7
# (2d735308) spent 346 straight Ticks on one cell re-issuing ``MOVE:DOWN
# home-return (-218,-2620)`` out of (-1599,-1870), every attempt rejected with
# MOVE_CONTESTED and no friendly unit within 4 cells — the breaker existed for
# the attack march only, so a home defender refused by an unseen claimant had no
# way to learn the destination was lost.
_CONTEST_PARK_TEAMS = ("attack", "home")


def _record_combat_planned_step(unit: Any, detail: str) -> None:
    """Remember which neighbour this combat step aimed at, for the breaker above."""
    try:
        direction = Direction[detail.split(" ", 1)[0]]
    except (KeyError, IndexError, ValueError):
        return
    origin = tuple(unit.position)
    _worker_planned_step[str(unit.id)] = (
        origin,
        (origin[0] + direction.delta[0], origin[1] + direction.delta[1]),
    )


def _attack_contest_note(cells: frozenset[tuple[int, int]]) -> str:
    """Suffix marking that this step was planned around parked refusals."""
    if not cells:
        return ""
    return " contest-avoid=" + ",".join(
        f"({x}, {y})" for x, y in sorted(cells)
    )


# Cache of per-name home-team patrol slot indices. Keyed by (radius, sorted
# roster tuple) so changing the roster or radius recomputes the assignment.
# Prevents multiple home defenders from hashing onto the same patrol slot and
# hammering each other with CELL_UNIT_LIMIT moves (observed with R5/R6).
_home_patrol_slot_cache: dict[tuple[int, tuple[str, ...]], dict[str, int]] = {}
# Consecutive Ticks a home defender has spent frozen on the delivery ring
# (packed-outward-wait / home-chute-hold): uid -> (cell, count). With a large
# worker fleet the ring is permanently packed, so the courtesy wait used to
# freeze defenders for 99% of Ticks while the economy was raided. After
# _HOME_CHUTE_WAIT_ESCAPE Ticks stuck on the same cell the defender stops
# yielding and pathfinds out to its patrol slot instead.
_home_chute_wait: dict[str, tuple[tuple[int, int], int]] = {}
_HOME_CHUTE_WAIT_ESCAPE = 6
# Lease holder frozen this many ticks (rejected moves count as no progress)
# before the lease rotates to the next carrier.
_DELIVERY_STUCK_LIMIT = 4
# Stall watchdog for the lease holder: (distance_to_core, ticks_without_closer).
# A holder far from the chute who stops making progress yields the lease; a
# holder already in queue range (<= 2) is legitimately waiting its turn.
_delivery_stall: dict[str, tuple[int, int]] = {}
_DELIVERY_STALL_LIMIT = 6
# Queue-range livelock breaker: when the lease holder sits in queue range
# (<= 2) but cannot move for _DELIVERY_HOLDER_YIELD_AFTER ticks — typically
# because queued workers camp the very cells its only path uses — everyone
# else in the queue must sidestep instead of holding until the way clears.
_delivery_holder_queue_stall: dict[str, tuple[tuple[int, int], int]] = {}
_DELIVERY_HOLDER_YIELD_AFTER = 2
_delivery_lease_yield = False
# Consecutive ticks a unit fails to get closer to its manual waypoint before the
# waypoint auto-clears and the unit resumes its normal program. (An obstacle
# target never resolves to entry — being adjacent counts as arrival instead.)
_waypoint_stuck: dict[str, tuple[int, tuple[int, int]]] = {}
_WAYPOINT_STUCK_THRESHOLD = 15
_map_dirty: bool = False
_last_map_save_tick: int = -1
# Deferred disk write: planning only marks the save, the live loop flushes it
# after turn.submit() so a 10s wall serialization cannot eat the command window.
_pending_map_save_tick: int | None = None
# Disk-owned map fields, refreshed only when map_memory.json's signature changes
# (see _apply_dashboard_map_edits). They used to be recovered by json.loads-ing
# the whole file on every save — ~0.4s at 560k walls, for three small fields.
_map_disk_manual: set[tuple[int, int]] = set()
_map_disk_forgotten: set[tuple[int, int]] = set()
_map_disk_clear_seq: int = 0
# Sorted obstacle payload reused across saves, and the adds not merged into it
# yet. See _obstacles_sorted_payload().
_obstacle_sorted_cache: list[tuple[int, int]] | None = None
_obstacle_pending_adds: set[tuple[int, int]] = set()

# Cumulative battle-report statistics (economy / combat / production + per-unit
# details), persisted to game_stats.json so they survive process restarts.
_game_stats: dict[str, Any] = game_stats.load()


def _coords_from_payload(raw) -> set[tuple[int, int]]:
    """Parse [[x,y], ...] payloads into coordinate tuples."""
    out: set[tuple[int, int]] = set()
    for item in raw or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out.add((int(item[0]), int(item[1])))
    return out


def _enemy_sightings_from_payload(raw) -> tuple[set[tuple[int, int]], dict[tuple[int, int], str], dict[tuple[int, int], int]]:
    """Parse enemy-sighting payloads into (positions, types, last-seen ticks).

    Supports the legacy ``[x, y]`` form, the typed ``[x, y, "CORE"]`` form and
    the timestamped ``[x, y, "CORE", tick]`` form; unknown types default to
    "ENEMY" and missing ticks to 0 (oldest).
    """
    positions: set[tuple[int, int]] = set()
    types: dict[tuple[int, int], str] = {}
    ticks: dict[tuple[int, int], int] = {}
    for item in raw or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        pos = (int(item[0]), int(item[1]))
        positions.add(pos)
        if len(item) >= 3 and item[2]:
            types[pos] = str(item[2]).upper()
        if len(item) >= 4:
            try:
                ticks[pos] = int(item[3])
            except (TypeError, ValueError):
                pass
    return positions, types, ticks


def _load_map_memory() -> None:
    """Load permanent obstacle/resource/enemy memory from disk."""
    global _resource_memory, _obstacle_memory, _enemy_memory, _enemy_memory_types, \
        _enemy_memory_ticks, _enemy_clear_seq, _last_dashboard_map_sig, _known_obstacles, \
        _core_anchor, _map_disk_manual, _map_disk_forgotten, _map_disk_clear_seq, \
        _obstacle_sorted_cache
    if not MAP_MEMORY_PATH.exists():
        return
    try:
        data = json.loads(MAP_MEMORY_PATH.read_text(encoding="utf-8"))
        forgotten = _coords_from_payload(data.get("forgotten_resources"))
        _obstacle_memory = map_codec.decode_obstacles(data)
        # Wholesale replacement: tell the dead-end rebase check the wall map moved.
        _bump_obstacle_generation()
        resources = _coords_from_payload(data.get("resources"))
        manual = _coords_from_payload(data.get("manual_resources"))
        _resource_memory = (resources | manual) - forgotten
        _resource_tombstones.clear()
        _resource_tombstones.update(forgotten)
        _enemy_memory, _enemy_memory_types, _enemy_memory_ticks = _enemy_sightings_from_payload(
            data.get("enemy_sightings")
        )
        _enemy_clear_seq = int(data.get("enemy_clear_seq", 0) or 0)
        raw_anchor = data.get("core_anchor")
        _core_anchor = (
            (int(raw_anchor[0]), int(raw_anchor[1]))
            if isinstance(raw_anchor, (list, tuple)) and len(raw_anchor) == 2
            else None
        )
        _last_dashboard_map_sig = _dashboard_map_sig(data)
        _map_disk_manual = manual
        _map_disk_forgotten = forgotten
        _map_disk_clear_seq = _enemy_clear_seq
        # Wholesale reload: the file this process just read is no longer what any
        # earlier save saw, so drop the incremental obstacle payload cache.
        _obstacle_sorted_cache = None
        _obstacle_pending_adds.clear()
        # Wholesale reload: rebase the stable obstacle view and defer the batch
        # dead-end build to the first _ensure_dead_structure() of the next tick.
        _known_obstacles = frozenset(_obstacle_memory)
        _reset_dead_structure(_known_obstacles)
        print(
            f"[map] loaded obstacles={len(_obstacle_memory)} resources={len(_resource_memory)} "
            f"manual={len(manual - forgotten)} forgotten={len(forgotten)} "
            f"enemies={len(_enemy_memory)} from {MAP_MEMORY_PATH}",
            flush=True,
        )
    except Exception as e:
        print(f"[map] load failed: {e}", flush=True)


def _latest_tactic_record_for_prune() -> dict | None:
    """Return the newest complete tick record for cold-wall retention."""
    path = Path(DEFAULT_LOG_PATH)
    try:
        size = path.stat().st_size
        if size <= 0:
            return None
        with open(path, "rb") as handle:
            handle.seek(max(0, size - 2_000_000))
            chunk = handle.read()
    except OSError:
        return None
    for raw in reversed(chunk.splitlines()):
        try:
            record = json.loads(raw.decode("utf-8", "replace"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(record, dict)
            and isinstance(record.get("tick"), int)
            and record.get("core_pos")
        ):
            return record
    return None


def _prune_cold_map_memory() -> None:
    """One-shot startup prune of walls far from all current activity.

    Safety rails: config-gated, marker-gated, full pre-prune backup, a gzipped
    archive of every removed wall, and an in-memory rollback if the pruned map
    cannot be saved. The running unit/path set comes from the newest complete
    tick record, so a restart prunes against where the army actually is.
    """
    global _obstacle_memory, _known_obstacles, _map_dirty
    config = load_config()
    if not bool(config.get("map_cold_prune_enabled", False)):
        return
    if (
        bool(config.get("map_cold_prune_once", True))
        and MAP_COLD_PRUNE_MARKER.exists()
    ):
        print("[map] cold prune skipped: marker present", flush=True)
        return
    if not MAP_MEMORY_PATH.exists() or not _obstacle_memory:
        return
    record = _latest_tactic_record_for_prune()
    if record is None:
        print("[map] cold prune skipped: no recent tick record", flush=True)
        return
    try:
        core_raw = record.get("core_pos") or ()
        core = (int(core_raw[0]), int(core_raw[1]))
    except (TypeError, ValueError, IndexError):
        print("[map] cold prune skipped: invalid core position", flush=True)
        return
    units: list[dict[str, Any]] = []
    for group in ("workers", "vanguards", "rangers"):
        for unit in record.get(group) or []:
            if not isinstance(unit, dict):
                continue
            pos = unit.get("pos")
            if not isinstance(pos, (list, tuple)) or len(pos) < 2:
                continue
            target = unit.get("target")
            units.append({
                "pos": (int(pos[0]), int(pos[1])),
                "target": (
                    (int(target[0]), int(target[1]))
                    if isinstance(target, (list, tuple)) and len(target) >= 2
                    else None
                ),
                "path": unit.get("path") or [],
            })
    if not units:
        print("[map] cold prune skipped: no unit snapshot", flush=True)
        return

    core_radius = max(0, int(config.get("map_cold_prune_core_radius", 1000) or 0))
    unit_radius = max(0, int(config.get("map_cold_prune_unit_radius", 1000) or 0))
    path_radius = max(0, int(config.get("map_cold_prune_path_radius", 5) or 0))

    corridor: set[tuple[int, int]] = set()
    for unit in units:
        for cell in unit["path"]:
            if not isinstance(cell, (list, tuple)) or len(cell) < 2:
                continue
            x, y = int(cell[0]), int(cell[1])
            for dx in range(-path_radius, path_radius + 1):
                for dy in range(-path_radius, path_radius + 1):
                    corridor.add((x + dx, y + dy))
                    if len(corridor) > 500_000:
                        print(
                            "[map] cold prune skipped: path corridor too large",
                            flush=True,
                        )
                        return

    points: set[tuple[int, int]] = set()
    for unit in units:
        points.add(unit["pos"])
        if unit["target"] is not None:
            points.add(unit["target"])
    cell_size = max(unit_radius, 1)
    grid: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for point in points:
        grid[(point[0] // cell_size, point[1] // cell_size)].append(point)

    cold: set[tuple[int, int]] = set()
    for wall in _obstacle_memory:
        x, y = int(wall[0]), int(wall[1])
        if abs(x - core[0]) + abs(y - core[1]) <= core_radius:
            continue
        near_unit = False
        if unit_radius > 0:
            gx, gy = x // cell_size, y // cell_size
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for point in grid.get((gx + dx, gy + dy), ()):
                        if abs(x - point[0]) + abs(y - point[1]) <= unit_radius:
                            near_unit = True
                            break
                    if near_unit:
                        break
                if near_unit:
                    break
        if near_unit:
            continue
        if wall in corridor:
            continue
        cold.add((x, y))

    if not cold:
        print("[map] cold prune skipped: no cold walls", flush=True)
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = MAP_MEMORY_PATH.with_name(f"map_memory.pre-prune-{stamp}.json")
    archive_path = MAP_MEMORY_PATH.with_name(f"map_memory.cold-prune-{stamp}.json.gz")
    try:
        shutil.copy2(MAP_MEMORY_PATH, backup_path)
        tmp_archive = archive_path.with_name(archive_path.name + ".tmp")
        with gzip.open(tmp_archive, "wt", encoding="utf-8") as handle:
            json.dump({
                "version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "tick": record.get("tick"),
                "core": list(core),
                "policy": {
                    "core_radius": core_radius,
                    "unit_radius": unit_radius,
                    "path_radius": path_radius,
                },
                "cold_count": len(cold),
                "walls": [list(wall) for wall in cold],
            }, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_archive, archive_path)
    except Exception as exc:
        print(f"[map] cold prune skipped: archive failed: {exc}", flush=True)
        return

    _obstacle_memory.difference_update(cold)
    _bump_obstacle_generation()
    _known_obstacles = frozenset(_obstacle_memory)
    _reset_dead_structure(_known_obstacles)
    _map_dirty = True
    try:
        _save_map_memory(force=True)
    except Exception as exc:
        _obstacle_memory.update(cold)
        _bump_obstacle_generation()
        _known_obstacles = frozenset(_obstacle_memory)
        _reset_dead_structure(_known_obstacles)
        _map_dirty = True
        print(f"[map] cold prune save failed, restored walls: {exc}", flush=True)
        return
    try:
        atomic_write_text(
            MAP_COLD_PRUNE_MARKER,
            json.dumps({
                "created_at": datetime.now(timezone.utc).isoformat(),
                "tick": record.get("tick"),
                "removed": len(cold),
                "remaining": len(_obstacle_memory),
                "policy": {
                    "core_radius": core_radius,
                    "unit_radius": unit_radius,
                    "path_radius": path_radius,
                },
                "backup": backup_path.name,
                "archive": archive_path.name,
            }, ensure_ascii=False),
        )
    except Exception as exc:
        print(f"[map] cold prune marker write failed: {exc}", flush=True)
    print(
        f"[map] cold prune removed={len(cold)} remaining={len(_obstacle_memory)} "
        f"core_r={core_radius} unit_r={unit_radius} path_r={path_radius} "
        f"backup={backup_path.name} archive={archive_path.name}",
        flush=True,
    )



def _dashboard_map_sig(data: dict) -> tuple:
    """Stable signature of dashboard-owned map fields."""
    forgotten = tuple(sorted(_coords_from_payload(data.get("forgotten_resources"))))
    manual = tuple(sorted(_coords_from_payload(data.get("manual_resources"))))
    resources = tuple(sorted(_coords_from_payload(data.get("resources"))))
    enemies = tuple(sorted(_enemy_sightings_from_payload(data.get("enemy_sightings"))[0]))
    clear_seq = int(data.get("enemy_clear_seq", 0) or 0)
    return (forgotten, manual, resources, enemies, clear_seq)


def _file_signature(path: Path) -> tuple[int, int] | None:
    """(mtime_ns, size) signature for cheap unchanged-file detection.

    Same mtime+size heuristic as tactic_config._path_signature: a rewrite with
    an identical size landing in the same st_mtime_ns tick would be missed —
    acceptable for these small dashboard-owned JSON files, where a content
    change almost always changes the size too.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


# Last seen file signature of map_memory.json, keyed by path. The dashboard
# rarely edits the map, yet the edit-sync used to json.loads the whole
# (ever-growing) file twice per Tick; an unchanged file is now skipped with a
# single stat().
_map_file_sig_cache: dict[str, tuple[int, int]] = {}


def _apply_dashboard_map_edits() -> None:
    """Pull dashboard deletions/additions into process memory before planning/saving.

    The dashboard and tactic are separate processes. Without this sync, a
    dashboard clear only edits map_memory.json, and the next tactic save would
    rewrite the old in-memory resources or enemy sightings back onto disk.

    Edits are applied only when the dashboard-owned signature changes, so a
    live re-discovery after a clear is not immediately re-forgotten from a
    stale on-disk forget list.
    """
    global _resource_memory, _enemy_memory, _enemy_memory_types, _enemy_memory_ticks, \
        _enemy_clear_seq, _map_dirty, _last_dashboard_map_sig, _map_disk_manual, \
        _map_disk_forgotten, _map_disk_clear_seq
    if not MAP_MEMORY_PATH.exists():
        return
    file_sig = _file_signature(MAP_MEMORY_PATH)
    cache_key = str(MAP_MEMORY_PATH)
    if file_sig is not None and file_sig == _map_file_sig_cache.get(cache_key):
        return  # unchanged since the last sync — skip the full re-parse
    try:
        data = json.loads(MAP_MEMORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    if file_sig is not None:
        _map_file_sig_cache[cache_key] = file_sig

    # Stash the disk-owned fields: _save_map_memory needs them and must not
    # re-parse the file to get them. While this signature still matches the
    # file, they are exactly what is on disk.
    _map_disk_forgotten = _coords_from_payload(data.get("forgotten_resources"))
    _map_disk_manual = _coords_from_payload(data.get("manual_resources"))
    _map_disk_clear_seq = int(data.get("enemy_clear_seq", 0) or 0)

    sig = _dashboard_map_sig(data)
    if sig == _last_dashboard_map_sig:
        return
    _last_dashboard_map_sig = sig

    forgotten = _map_disk_forgotten
    manual = _map_disk_manual

    if forgotten:
        before = set(_resource_memory)
        for pos in forgotten:
            _resource_memory.discard(pos)
            _resource_tombstones.add(pos)
        if _resource_memory != before:
            _map_dirty = True
        for wid, goal in list(_resource_assignments.items()):
            if tuple(goal) in forgotten:
                _resource_assignments.pop(wid, None)

    # Absorb dashboard manual adds only. Auto resources are owned by this
    # process; rehydrating them here would undo a local _forget_resource()
    # that has not been flushed to disk yet.
    #
    # Local tombstones also win over a still-stale on-disk manual entry until
    # the next save flushes the forget list.
    for pos in manual - forgotten - set(_resource_tombstones):
        if pos not in _resource_memory:
            _resource_memory.add(pos)
            _map_dirty = True
        _resource_tombstones.discard(pos)
    # On-disk auto resources are intentionally unused for rehydration: they are
    # owned by this process, and a stale copy would resurrect a local forget.

    clear_seq = _map_disk_clear_seq
    if clear_seq > _enemy_clear_seq:
        _enemy_clear_seq = clear_seq
        disk_positions, disk_types, disk_ticks = _enemy_sightings_from_payload(
            data.get("enemy_sightings")
        )
        if _enemy_memory != disk_positions:
            _enemy_memory.clear()
            _enemy_memory.update(disk_positions)
            _enemy_memory_types.clear()
            _enemy_memory_types.update(disk_types)
            _enemy_memory_ticks.clear()
            _enemy_memory_ticks.update(disk_ticks)
            _map_dirty = True


def _map_transaction(func):
    @wraps(func)
    def locked(*args, **kwargs):
        with file_lock(MAP_MEMORY_PATH):
            return func(*args, **kwargs)

    return locked


def _obstacles_sorted_payload() -> list[tuple[int, int]]:
    """Obstacle coordinates as a sorted list, reused across saves.

    Walls only ever grow (every mutation is a `.add()`), so a save that added a
    handful of cells merges them into the cached sorted list instead of
    re-sorting ~560k coordinates — which measured 1.9s per save and was the
    single biggest term in the `map_memory` phase of a slow tick.

    Any disagreement between (cache + recorded adds) and the live set rebuilds
    the cache from scratch, so a mutation site that forgets to register its add
    costs time but can never write a wrong file.
    """
    global _obstacle_sorted_cache
    cache = _obstacle_sorted_cache
    if cache is not None:
        pending = _obstacle_pending_adds
        # Same-cardinality swaps would slip past the length check, but obstacles
        # never shrink and a wholesale reload resets the cache in _load_map_memory.
        if len(_obstacle_memory) == len(cache) + len(pending) and pending <= _obstacle_memory:
            if pending:
                cache = list(merge(cache, sorted(pending)))
                _obstacle_sorted_cache = cache
            _obstacle_pending_adds.clear()
            return cache
    rebuilt = sorted(_obstacle_memory)
    _obstacle_sorted_cache = rebuilt
    _obstacle_pending_adds.clear()
    return rebuilt


def _save_map_memory(
    tick: int | None = None,
    force: bool = False,
    save_interval_ticks: int = 10,
    defer: bool = False,
) -> None:
    """Persist permanent map memory. Obstacles never shrink.

    Manual resources entered from the dashboard are preserved across saves,
    except coordinates listed in forgotten_resources / local tombstones.

    ``defer`` performs the dashboard-memory reconciliation and due checks but
    leaves the expensive encode/write to the live loop after submit.

    The cheap no-op gates run BEFORE the cross-process map lock: _choose_actions
    calls this every Tick with ``defer=True``, and taking map_memory.lock on a
    Tick that only sets a pending marker (or returns immediately) serialized the
    live loop against the dashboard for nothing. _apply_dashboard_map_edits() is
    not newly-unlocked code — _choose_actions already calls it outside this lock
    every Tick — and the actual write below still holds the lock.
    """
    global _pending_map_save_tick
    # Always honor dashboard deletions, even on a non-dirty no-op path.
    _apply_dashboard_map_edits()
    if not force and not _map_dirty:
        return
    if (
        not force
        and tick is not None
        and _last_map_save_tick >= 0
        and tick - _last_map_save_tick < save_interval_ticks
    ):
        return
    if defer and not force and tick is not None:
        if _pending_map_save_tick is None:
            _pending_map_save_tick = tick
        return
    _save_map_memory_locked(
        tick=tick,
        force=force,
        save_interval_ticks=save_interval_ticks,
        defer=defer,
    )


@_map_transaction
def _save_map_memory_locked(
    tick: int | None = None,
    force: bool = False,
    save_interval_ticks: int = 10,
    defer: bool = False,
) -> None:
    """Write body of :func:`_save_map_memory`; always called under the map lock."""
    global _map_dirty, _last_map_save_tick, _enemy_clear_seq, _last_dashboard_map_sig
    global _pending_map_save_tick
    # Always honor dashboard deletions, even on a non-dirty no-op path.
    _apply_dashboard_map_edits()
    if not force and not _map_dirty:
        return
    if (
        not force
        and tick is not None
        and _last_map_save_tick >= 0
        and tick - _last_map_save_tick < save_interval_ticks
    ):
        return
    if defer and not force and tick is not None:
        if _pending_map_save_tick is None:
            _pending_map_save_tick = tick
        return

    # Taken from the edit-sync's parse of this same file (under this lock), not a
    # second full re-parse. Copied because the sets below are mutated.
    manual: set[tuple[int, int]] = set(_map_disk_manual)
    disk_forgotten: set[tuple[int, int]] = set(_map_disk_forgotten)
    disk_enemy_clear_seq = _map_disk_clear_seq
    if not MAP_MEMORY_PATH.exists():
        manual.clear()
        disk_forgotten.clear()
        disk_enemy_clear_seq = _enemy_clear_seq

    # Sticky forget list: dashboard clears + runtime depletion confirmations.
    # Anything currently known again (re-seen live, or revived in RAM) leaves
    # the forget list so workers can relearn after a manual clear.
    forgotten = (set(_resource_tombstones) | disk_forgotten) - set(_resource_memory)
    manual -= forgotten
    resources = (set(_resource_memory) | manual) - forgotten
    _resource_memory.clear()
    _resource_memory.update(resources)
    _resource_tombstones.clear()
    _resource_tombstones.update(forgotten)
    _enemy_clear_seq = max(_enemy_clear_seq, disk_enemy_clear_seq)

    obstacle_fields = (
        # Large maps are encoded straight from the live set: the sorted list
        # only exists to make the legacy plain payload cheap, and keeping it
        # would hold another 760k coordinate tuples in RAM on a 2 GB VPS.
        map_codec.encode_obstacle_fields(_obstacle_memory)
        if len(_obstacle_memory) > map_codec.PLAIN_OBSTACLE_LIMIT
        else map_codec.encode_obstacle_fields(_obstacles_sorted_payload())
    )

    payload = {
        "updated_tick": tick,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **obstacle_fields,
        "resources": sorted([list(p) for p in resources]),
        "manual_resources": sorted([list(p) for p in manual]),
        "forgotten_resources": sorted([list(p) for p in forgotten]),
        "enemy_sightings": sorted(
            [
                list(pos)
                + [_enemy_memory_types.get(pos) or "ENEMY"]
                + [_enemy_memory_ticks.get(pos, 0)]
                for pos in _enemy_memory
            ]
        ),
        "obstacle_count": len(_obstacle_memory),
        "resource_count": len(resources),
        "manual_count": len(manual),
        "enemy_sighting_count": len(_enemy_memory),
        "enemy_clear_seq": _enemy_clear_seq,
        "core_anchor": list(_core_anchor) if _core_anchor is not None else None,
    }
    atomic_write_text(MAP_MEMORY_PATH, json.dumps(payload, ensure_ascii=False))
    _map_dirty = False
    _pending_map_save_tick = None
    _last_dashboard_map_sig = _dashboard_map_sig(payload)
    # The file we just wrote is in sync — seed its signature so the next
    # Tick's edit-sync skips the full re-read entirely.
    written_sig = _file_signature(MAP_MEMORY_PATH)
    if written_sig is not None:
        _map_file_sig_cache[str(MAP_MEMORY_PATH)] = written_sig
    if tick is not None:
        _last_map_save_tick = tick


# ── background map-memory writer ─────────────────────────────────────────────
# Only one child may be outstanding: two writers would serialize on the map
# flock and the second one would still be holding the loop hostage. The child
# never prints, because fork() carries only the forking thread and an inherited
# stdio lock could wedge it forever; it reports through its exit code instead,
# and the timeout below is the backstop for exactly that case (kill releases
# the flock, and the save retries on the next due Tick).
_MAP_SAVE_CHILD_TIMEOUT_SECONDS = 240.0
_MAP_SAVE_CHILD_KILL_SIGNAL = getattr(signal, "SIGKILL", getattr(signal, "SIGTERM", 15))
_map_save_child_pid: int | None = None
_map_save_child_started = 0.0


def _reap_map_save_child(*, block: bool = False) -> None:
    """Collect the background writer.

    ``block`` waits for it instead of polling — used before the exit save, so a
    snapshot forked minutes earlier cannot land on top of the newer file the
    loop is about to write.
    """
    global _map_save_child_pid, _map_save_child_started, _map_dirty
    if _map_save_child_pid is None:
        return
    waitpid = getattr(os, "waitpid", None)
    wnohang = getattr(os, "WNOHANG", None)
    if waitpid is None or (not block and wnohang is None):
        # A child can only exist where fork exists; a checkout without the
        # POSIX wait primitives has nothing to collect.
        _map_save_child_pid = None
        return
    flags = 0 if block else wnohang
    if not block and (
        time.monotonic() - _map_save_child_started > _MAP_SAVE_CHILD_TIMEOUT_SECONDS
    ):
        print(
            f"[map] background save pid={_map_save_child_pid} exceeded "
            f"{_MAP_SAVE_CHILD_TIMEOUT_SECONDS:.0f}s, killed",
            flush=True,
        )
        try:
            os.kill(_map_save_child_pid, _MAP_SAVE_CHILD_KILL_SIGNAL)
        except OSError:
            pass
        _map_save_child_pid = None
        return
    try:
        pid, status = waitpid(_map_save_child_pid, flags)
    except (ChildProcessError, OSError):
        _map_save_child_pid = None
        return
    if not pid:
        return
    _map_save_child_pid = None
    code = os.waitstatus_to_exitcode(status)
    if code == 2:
        # The child absorbed a dashboard edit that landed after this process'
        # last sync (see _fork_map_save). Its write is good, but leaving the
        # stale signature in place is deliberate: the next Tick re-parses the
        # file so the edit is not dropped from live memory.
        return
    if code != 0:
        _map_dirty = True
        print(f"[map] background save failed exit={code}", flush=True)
        return
    # The child wrote the file in its own address space and exited, so the parent
    # never saw the new signature. Without this refresh the next Tick's
    # _apply_dashboard_map_edits() concludes "changed", re-parses the whole
    # multi-MB map_memory.json and only then finds the dashboard sig unchanged
    # (production: one 2,124 ms map_memory phase per successful fork; report #9).
    saved_sig = _file_signature(MAP_MEMORY_PATH)
    if saved_sig is not None:
        _map_file_sig_cache[str(MAP_MEMORY_PATH)] = saved_sig


def _fork_map_save(tick: int) -> bool:
    """Write this save in a child process. False means the caller saves inline."""
    global _map_save_child_pid, _map_save_child_started, _last_map_save_tick
    if _map_save_child_pid is not None:
        return False
    fork = getattr(os, "fork", None)
    if fork is None:  # Windows dev boxes have no fork: keep the inline write
        return False
    try:
        pid = fork()
    except OSError as exc:
        print(f"[map] background save fork failed: {exc}", flush=True)
        return False
    if pid == 0:
        code = 0
        # Signatures inherited from the parent. If the file moved after that sync
        # the child re-reads it inside _save_map_memory; the parent's copy of the
        # dashboard-owned fields would then be stale, so flag it (exit 2) and let
        # the parent re-parse on its next Tick instead of trusting the new
        # signature (report #9's refresh must not skip a real dashboard edit).
        inherited_sig = _map_file_sig_cache.get(str(MAP_MEMORY_PATH))
        try:
            current_sig = _file_signature(MAP_MEMORY_PATH)
            if inherited_sig is None or current_sig != inherited_sig:
                code = 2
            _save_map_memory(tick=tick, force=True)
        except BaseException:
            code = 1
        os._exit(code)
    _map_save_child_pid = pid
    _map_save_child_started = time.monotonic()
    # The parent owns the schedule: this snapshot is already on its way to disk.
    _last_map_save_tick = tick
    return True


def _flush_pending_map_memory(*, background: bool = False) -> None:
    """Write a save deferred out of the planning phase, after submit.

    ``background`` hands the encode to a forked child (POSIX only). A deferred
    save serializes every remembered wall cell; at 800k+ cells on the 2-core VPS
    that measured 33s, and since it sits between the submit and the next command
    window it cost 2 Ticks — a TICK_MISMATCH — on every single slow save. The
    child needs no loop state, so the parent keeps planning on copy-on-write
    pages and collects the result on a later Tick.
    """
    global _pending_map_save_tick, _map_dirty
    if background:
        # Collected on every Tick, not only when a save comes due: the parent
        # must notice the child finished long before the next interval.
        _reap_map_save_child()
    tick = _pending_map_save_tick
    if tick is None:
        return
    _pending_map_save_tick = None
    if background:
        if _fork_map_save(tick):
            print(f"[map] deferred save forked tick={tick}", flush=True)
            return
        if _map_save_child_pid is not None:
            # The running child already covers this Tick's cells; blocking here
            # would put the stall back, so the save just waits for the next due
            # interval (_map_dirty stays set, which is what gates it).
            print(
                f"[map] deferred save held tick={tick}: background save running",
                flush=True,
            )
            return
    started = time.monotonic()
    try:
        _save_map_memory(tick=tick, force=True)
    except Exception as exc:
        _map_dirty = True
        print(f"[map] deferred save failed tick={tick}: {exc}", flush=True)
        return
    elapsed_ms = (time.monotonic() - started) * 1000
    if elapsed_ms >= 1000:
        print(f"[map] deferred save tick={tick} {elapsed_ms:.0f}ms", flush=True)


# ── manual per-unit waypoints (dashboard ⌖) ─────────────────────────────────
# The dashboard and tactic are separate processes sharing waypoints.json. All
# writes are read-modify-write on the latest file so one side's change never
# clobbers the other's (same discipline as map_memory.json).

def _normalize_waypoint_entry(raw: object) -> dict | None:
    """Return a waypoint entry {"queue": [(x,y),...], "mode": str} or None.

    Accepts both the new queue format and the legacy single-target
    {name: [x, y]} value — a legacy target becomes a one-stop walk-only
    (rush) queue, matching the pre-queue "just march" behavior.
    """
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            return {"queue": [(int(raw[0]), int(raw[1]))], "mode": "rush"}
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    queue_raw = raw.get("queue")
    queue: list[tuple[int, int]] = []
    if isinstance(queue_raw, (list, tuple)):
        for pos in queue_raw:
            if not isinstance(pos, (list, tuple)) or len(pos) < 2:
                continue
            try:
                queue.append((int(pos[0]), int(pos[1])))
            except (TypeError, ValueError):
                continue
    mode = str(raw.get("mode") or "attack")
    if mode not in ("attack", "rush"):
        mode = "attack"
    if not queue:
        return None
    return {"queue": queue, "mode": mode}


def _load_waypoints() -> dict[str, dict]:
    """Read manual per-unit target queues as {name: {"queue": [...], "mode": str}}."""
    if not WAYPOINTS_PATH.exists():
        return {}
    try:
        data = json.loads(WAYPOINTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    targets = data.get("targets") if isinstance(data, dict) else None
    out: dict[str, dict] = {}
    if isinstance(targets, dict):
        for name, raw in targets.items():
            entry = _normalize_waypoint_entry(raw)
            if isinstance(name, str) and entry is not None:
                out[name] = entry
    return out


def _write_waypoints_unlocked(targets: dict[str, dict]) -> None:
    payload: dict[str, object] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "targets": {},
    }
    for name, raw in targets.items():
        entry = _normalize_waypoint_entry(raw)
        if entry is None:
            continue
        payload["targets"][name] = {
            "queue": [[int(x), int(y)] for x, y in entry["queue"]],
            "mode": entry.get("mode", "attack"),
        }
    atomic_write_text(WAYPOINTS_PATH, json.dumps(payload, ensure_ascii=False))


def _write_waypoints(targets: dict[str, dict]) -> None:
    with file_lock(WAYPOINTS_PATH):
        _write_waypoints_unlocked(targets)


def _remove_waypoint(
    name: str,
    *,
    expected_target: tuple[int, int] | None = None,
) -> None:
    """Delete one unit's whole manual queue, preserving concurrent writes."""
    try:
        with file_lock(WAYPOINTS_PATH):
            targets = _load_waypoints()
            entry = targets.get(name)
            if entry is not None and (
                expected_target is None
                or (
                    entry["queue"]
                    and tuple(entry["queue"][0]) == tuple(expected_target)
                )
            ):
                del targets[name]
                _write_waypoints_unlocked(targets)
            # No per-Tick cache refresh here: the write bumps mtime, so the
            # next Tick's signature check misses and reloads from disk anyway.
    except Exception:
        pass


def _pop_waypoint_target(name: str, expected_target: tuple[int, int]) -> None:
    """Advance one unit's queue: drop the head if it still is expected_target.

    Reaching a target pops it and moves on to the next; an empty queue removes
    the unit's entry entirely. The expected_target guard preserves any
    concurrent dashboard rewrite of the head.
    """
    try:
        with file_lock(WAYPOINTS_PATH):
            targets = _load_waypoints()
            entry = targets.get(name)
            if (
                not entry
                or not entry["queue"]
                or tuple(entry["queue"][0]) != tuple(expected_target)
            ):
                return
            entry["queue"].pop(0)
            if not entry["queue"]:
                del targets[name]
            _write_waypoints_unlocked(targets)
    except Exception:
        pass


def _prune_waypoint_targets(
    targets: dict[str, dict],
    alive_names: set[str],
) -> bool:
    """Remove waypoints whose unit is gone; return True when anything changed."""
    stale = [name for name in targets if name not in alive_names]
    for name in stale:
        targets.pop(name, None)
    return bool(stale)


# Signature cache for waypoints.json, keyed by path: the file only changes
# when the dashboard sets a target or a unit reaches one, so an unchanged file
# must not cost a cross-process lock + full read every Tick. Pruning against
# the changing alive set runs in memory on the cached copy, and is persisted
# the moment it actually drops something (a unit with a target died).
_waypoints_sig_cache: dict[str, tuple[int, int] | None] = {}
_waypoints_cached: dict[str, dict[str, dict]] = {}


def _load_and_prune_waypoints(alive_names: set[str]) -> dict[str, dict]:
    """Load and prune targets as one transaction with dashboard writers."""
    key = str(WAYPOINTS_PATH)
    sig = _file_signature(WAYPOINTS_PATH)
    if key in _waypoints_sig_cache and sig == _waypoints_sig_cache[key]:
        cached = _waypoints_cached.get(key, {})
        if all(name in alive_names for name in cached):
            return dict(cached)  # steady state: unchanged file, nothing stale
        # A unit with a manual target died: fall through and persist the prune
        # so the dashboard — and a restart's name re-use — never see it again.
    with file_lock(WAYPOINTS_PATH):
        targets = _load_waypoints()
        if _prune_waypoint_targets(targets, alive_names):
            _write_waypoints_unlocked(targets)
            sig = _file_signature(WAYPOINTS_PATH)
        _waypoints_sig_cache[key] = sig
        _waypoints_cached[key] = dict(targets)
        return targets


# ── squad attack-target queues (进攻队/风筝队 目标队列) ──────────────────
# Ordered coordinate queues per squad in squad_targets.json. Only the coords
# mode consumes them: each Tick the head is "cleared" (popped) once the squad
# centroid has arrived and no visible or remembered enemy lingers around it,
# so the squad marches on to the next point. The tactic pops while the
# dashboard appends — both sides read-modify-write under the file lock so a
# concurrent operator edit is never clobbered.
_SQUAD_ARRIVE_RADIUS = 2
_SQUAD_CLEAR_RADIUS = 3
_SQUAD_QUEUE_LABELS = {"attack": "进攻队", "kite": "风筝队"}

_squad_targets_sig_cache: dict[str, tuple[int, int] | None] = {}
_squad_targets_cached: dict[str, dict[str, list[tuple[int, int]]]] = {}


def _parse_squad_targets_payload(data: Any) -> dict[str, list[tuple[int, int]]]:
    """Normalize a squad_targets.json payload; empty queues stay out."""
    out: dict[str, list[tuple[int, int]]] = {}
    if not isinstance(data, dict):
        return out
    for squad in ("attack", "kite"):
        raw = data.get(squad)
        if not isinstance(raw, (list, tuple)):
            continue
        queue: list[tuple[int, int]] = []
        for point in raw:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                try:
                    queue.append((int(point[0]), int(point[1])))
                except (TypeError, ValueError):
                    continue
        if queue:
            out[squad] = queue
    return out


def _load_squad_targets_unlocked() -> dict[str, list[tuple[int, int]]]:
    if not SQUAD_TARGETS_PATH.exists():
        return {}
    try:
        data = json.loads(SQUAD_TARGETS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return _parse_squad_targets_payload(data)


def _load_squad_targets() -> dict[str, list[tuple[int, int]]]:
    """Read the squad target queues; an unchanged file costs no re-read."""
    key = str(SQUAD_TARGETS_PATH)
    sig = _file_signature(SQUAD_TARGETS_PATH)
    if key in _squad_targets_sig_cache and sig == _squad_targets_sig_cache[key]:
        cached = _squad_targets_cached.get(key, {})
        return {squad: list(queue) for squad, queue in cached.items()}
    targets = _load_squad_targets_unlocked()
    _squad_targets_sig_cache[key] = sig
    _squad_targets_cached[key] = {
        squad: list(queue) for squad, queue in targets.items()
    }
    return targets


def _pop_squad_target(squad: str, head: tuple[int, int]) -> list[tuple[int, int]]:
    """Persist the removal of a completed queue head; returns the rest."""
    with file_lock(SQUAD_TARGETS_PATH):
        targets = _load_squad_targets_unlocked()
        queue = targets.get(squad, [])
        # Only pop while the head still matches: a concurrent dashboard edit
        # (the operator re-ordered the queue) takes precedence.
        if queue and queue[0] == head:
            queue = queue[1:]
        if queue:
            targets[squad] = queue
        else:
            targets.pop(squad, None)
        atomic_write_text(SQUAD_TARGETS_PATH, json.dumps(targets, ensure_ascii=False))
        remaining = list(queue)
    # The write bumps mtime, so the next Tick's signature check misses and
    # reloads — no manual cache invalidation needed.
    return remaining


def _advance_squad_target_queue(
    squad: str,
    queue: list[tuple[int, int]],
    squad_pos: tuple[int, int] | None,
    enemies: tuple,
    log_entries: list[dict],
) -> tuple[tuple[int, int] | None, list[tuple[int, int]]]:
    """Pop arrived-and-cleared queue heads; returns (current head, queue now).

    A head completes only when the squad centroid sits within
    _SQUAD_ARRIVE_RADIUS AND no visible or remembered enemy remains within
    _SQUAD_CLEAR_RADIUS of it. A wiped squad (squad_pos is None) never
    advances: respawned or re-enlisted units must still converge on the head.
    """
    while queue:
        head = queue[0]
        if squad_pos is None or _manhattan(squad_pos, head) > _SQUAD_ARRIVE_RADIUS:
            break
        contested = any(
            _manhattan(tuple(e.position), head) <= _SQUAD_CLEAR_RADIUS
            for e in enemies
        ) or any(
            _manhattan(p, head) <= _SQUAD_CLEAR_RADIUS for p in _enemy_memory
        )
        if contested:
            break
        queue = _pop_squad_target(squad, head)
        label = _SQUAD_QUEUE_LABELS.get(squad, squad)
        if queue:
            nxt = queue[0]
            msg = f"{label}目标肃清 ({head[0]},{head[1]})，下一个 ({nxt[0]},{nxt[1]})"
        else:
            msg = f"{label}目标肃清 ({head[0]},{head[1]})，目标队列已清空"
        log_entries.append({
            "tick": getattr(turn_context, "tick", 0),
            "ts": time.time(),
            "cat": "config",
            "msg": msg,
        })
    return (queue[0] if queue else None), queue


# ── manual per-unit self-destruct (dashboard 自裁 command) ───────────────────
# Same cross-process file discipline as waypoints.json: the dashboard appends
# display names here, and each Tick the tactic issues SELF_DESTRUCT for units
# that are still alive, then removes only the names it actually commanded so a
# concurrent dashboard write is never clobbered.

def _load_self_destructs_unlocked() -> set[str]:
    """Read pending self-destruct display names from the shared file."""
    if not SELF_DESTRUCT_PATH.exists():
        return set()
    try:
        data = json.loads(SELF_DESTRUCT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return set()
    units = data.get("units") if isinstance(data, dict) else None
    if not isinstance(units, list):
        return set()
    return {name for name in units if isinstance(name, str)}


def _write_self_destructs_unlocked(names: set[str]) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "units": sorted(names),
    }
    atomic_write_text(SELF_DESTRUCT_PATH, json.dumps(payload, ensure_ascii=False))


# Same signature cache for self_destruct.json — dashboard commands are rare,
# so the per-Tick lock + read is pure overhead in the steady state.
_self_destruct_sig_cache: dict[str, tuple[int, int] | None] = {}
_self_destruct_cached: dict[str, set[str]] = {}


def _load_and_prune_self_destructs(alive_names: set[str]) -> set[str]:
    """Load pending self-destruct names, dropping units already gone.

    Returns the names still alive that the planner should command this Tick.
    """
    key = str(SELF_DESTRUCT_PATH)
    sig = _file_signature(SELF_DESTRUCT_PATH)
    if key in _self_destruct_sig_cache and sig == _self_destruct_sig_cache[key]:
        cached = _self_destruct_cached.get(key, set())
        if not cached or cached <= alive_names:
            return set(cached)  # steady state: unchanged file, all pending alive
        # A pending command targets a dead unit: fall through and persist the
        # prune so a restart's name re-use can never inherit a stale command.
    with file_lock(SELF_DESTRUCT_PATH):
        pending = _load_self_destructs_unlocked()
        remaining = {n for n in pending if n in alive_names}
        if remaining != pending:
            _write_self_destructs_unlocked(remaining)
            sig = _file_signature(SELF_DESTRUCT_PATH)
        _self_destruct_sig_cache[key] = sig
        _self_destruct_cached[key] = set(remaining)
        return remaining


def _remove_self_destructs(names: set[str]) -> None:
    """Delete specific pending names, preserving any concurrent dashboard writes."""
    if not names:
        return
    try:
        with file_lock(SELF_DESTRUCT_PATH):
            pending = _load_self_destructs_unlocked()
            before = len(pending)
            pending.difference_update(names)
            if len(pending) != before:
                _write_self_destructs_unlocked(pending)
                # No per-Tick cache refresh here: the write bumps mtime, so the
                # next Tick's signature check misses and reloads from disk.
    except Exception:
        pass


# ── manual per-unit hold position (dashboard 驻守 command) ───────────────────
# Same cross-process file discipline as self_destruct.json: the dashboard
# toggles display names in holds.json, and each Tick the tactic loads the held
# set (pruning names whose unit no longer exists) and pins those units in
# place. Hold is a persistent state, so the tactic never deletes entries — the
# dashboard clears a name when the operator clicks the button again.


def _load_holds_unlocked() -> set[str]:
    """Read the held-unit display names from the shared file."""
    if not HOLDS_PATH.exists():
        return set()
    try:
        data = json.loads(HOLDS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return set()
    units = data.get("units") if isinstance(data, dict) else None
    if not isinstance(units, list):
        return set()
    return {name for name in units if isinstance(name, str)}


def _write_holds_unlocked(names: set[str]) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "units": sorted(names),
    }
    atomic_write_text(HOLDS_PATH, json.dumps(payload, ensure_ascii=False))


_holds_sig_cache: dict[str, tuple[int, int] | None] = {}
_holds_cached: dict[str, set[str]] = {}


def _load_and_prune_holds(alive_names: set[str]) -> set[str]:
    """Load held-unit names, dropping entries whose unit is already gone.

    Returns the names still alive that the planner should hold this Tick.
    """
    key = str(HOLDS_PATH)
    sig = _file_signature(HOLDS_PATH)
    if key in _holds_sig_cache and sig == _holds_sig_cache[key]:
        cached = _holds_cached.get(key, set())
        if not cached or cached <= alive_names:
            return set(cached)  # steady state: unchanged file, all held alive
        # A held unit died: fall through and persist the prune so a restart's
        # name re-use can never inherit a stale hold.
    with file_lock(HOLDS_PATH):
        pending = _load_holds_unlocked()
        remaining = {n for n in pending if n in alive_names}
        if remaining != pending:
            _write_holds_unlocked(remaining)
            sig = _file_signature(HOLDS_PATH)
        _holds_sig_cache[key] = sig
        _holds_cached[key] = set(remaining)
        return remaining




def _update_obstacle_memory(turn) -> frozenset[tuple[int, int]]:
    """Accumulate permanent obstacles. Returns the stable known-obstacle set.

    The returned frozenset is the SAME object across ticks unless walls actually
    grew (only `_obstacle_memory.add` exists — walls never shrink). New walls
    are folded into the persistent dead-end structure incrementally, so the
    wall-only dead-end fast-path in _get_dead_ends is an O(1) identity hit on
    no-growth ticks and a tiny incremental update on growth ticks.
    """
    global _obstacle_memory, _map_dirty, _known_obstacles, _obstacle_sorted_cache, \
        _known_obstacles_generation
    if not _obstacle_view_is_current():
        # _obstacle_memory changed outside this function — either a version bump
        # (wholesale reload / cold prune / learned Core cell) or code that
        # clears/restores the set directly. Rebase the incremental structure
        # before accumulating; the sampled probe (see _obstacle_view_is_current)
        # also catches a same-cardinality replacement, which a length check alone
        # cannot distinguish from an unchanged map.
        _known_obstacles = frozenset(_obstacle_memory)
        _reset_dead_structure(_known_obstacles)
        _ensure_dead_structure()
        # Same reason: a replacement this function did not perform cannot be
        # merged into the sorted payload cache, so drop it and rebuild on save.
        _obstacle_sorted_cache = None
        _obstacle_pending_adds.clear()
    added: list[tuple[int, int]] = []
    for p in turn.obstacle_cells:
        q = tuple(p) if not isinstance(p, tuple) else p
        if q not in _obstacle_memory:
            _obstacle_memory.add(q)
            added.append(q)
    if added:
        _map_dirty = True
        _obstacle_pending_adds.update(added)
        _dead_add_walls(added)
        _known_obstacles = _dead_obstacles
        # The incremental fold above brought the stable view along with the
        # mutation, so record the new version as already mirrored (a bump without
        # this would force a full rebase on the next Tick).
        _bump_obstacle_generation()
        _known_obstacles_generation = _obstacle_mem_generation
    return _known_obstacles


def _forget_resource(position: tuple[int, int]) -> None:
    """Mark a resource as absent in memory and the next persisted map."""
    global _map_dirty
    pos = tuple(position)
    _resource_memory.discard(pos)
    _resource_tombstones.add(pos)
    _map_dirty = True


def _update_resource_memory(turn) -> set[tuple[int, int]]:
    """Remember visible resources permanently until depletion is confirmed.

    A node is forgotten only when the server actually reports it gone:
    HARVEST_FAILED / RESOURCE_DEPLETED, or a worker arriving at a remembered
    cell that no longer shows a resource. HARVEST_FAILED / CARGO_FULL is NOT a
    depletion signal — it is the server refusing a harvest because the worker
    already carries cargo (harvest fills the full capacity in one action), so
    the node stays remembered for the next empty worker.

    Returns the cells newly remembered this Tick so the battle log can diff
    discoveries without copying the whole (20k+) resource set every Tick.
    """
    global _resource_memory, _map_dirty
    added: set[tuple[int, int]] = set()

    for p in turn.resource_cells:
        pos = tuple(p) if not isinstance(p, tuple) else p
        if pos not in _resource_memory:
            _resource_memory.add(pos)
            added.add(pos)
            _map_dirty = True
        _resource_tombstones.discard(pos)

    for event in turn.events:
        if (
            event.event_type == "HARVEST_FAILED"
            and event.reason_code == "RESOURCE_DEPLETED"
            and event.position
        ):
            pos = tuple(event.position)
            _forget_resource(pos)

    return added


def _update_enemy_sightings(
    turn, obstacles: frozenset[tuple[int, int]] | set[tuple[int, int]] | None = None
) -> set[tuple[int, int]]:
    """Record every position where enemies were seen.  Remove stale sightings
    only when a friendly unit can genuinely see the cell (within its own vision
    radius, with unobstructed line of sight) and no enemy is there.  A sighting
    no friendly can actually see is kept as last-known enemy info.

    ``obstacles`` lets the live loop hand in the cached known-wall frozenset so
    this does not rebuild a ~500k-cell copy every Tick. Returns the newly added
    sighting cells for the battle-log discovery diff."""
    global _enemy_memory, _enemy_memory_types, _enemy_memory_ticks, _map_dirty
    before = len(_enemy_memory)

    # Vision radius differs by object type (rules): Core 5 / Worker 3 /
    # Vanguard 4 / Ranger 5.  A sighting is confirmed empty only when the cell
    # falls inside a unit's *own* radius — a far worker with radius 3 passing
    # within 5 cells of a spot must not erase it.
    friendly_views: list[tuple[tuple[int, int], int]] = []
    if turn.core:
        friendly_views.append((tuple(turn.core.position), 5))
    for w in turn.workers:
        friendly_views.append((tuple(w.position), 3))
    for v in turn.vanguards:
        friendly_views.append((tuple(v.position), 4))
    for r in turn.rangers:
        friendly_views.append((tuple(r.position), 5))

    # Visible enemies this tick
    visible_enemies = tuple(getattr(turn, "visible_enemies", ()) or ())
    visible_enemy_positions: set[tuple[int, int]] = {
        tuple(enemy.position) for enemy in visible_enemies
    }

    # Add new sightings and refresh each position's last-known unit type so the
    # dashboard can still tell a CORE from a worker scout after line of sight is
    # lost.  Type-less stubs (tests / bare objects) land as "ENEMY".  When
    # several enemies share a cell (an enemy CORE has workers standing on its
    # own square), the higher-priority type wins so a worker never overwrites
    # the HQ label.
    added: set[tuple[int, int]] = set()
    for enemy in visible_enemies:
        pos = tuple(enemy.position)
        if pos not in _enemy_memory:
            _enemy_memory.add(pos)
            added.add(pos)
        new_type = _enemy_unit_type_name(enemy) or "ENEMY"
        if _enemy_type_priority(new_type) >= _enemy_type_priority(
            _enemy_memory_types.get(pos)
        ):
            _enemy_memory_types[pos] = new_type
        # Refresh the last-seen tick so consumers can rank sightings by
        # recency (the arena page only shows the most recent ones).
        seen_tick = int(getattr(turn, "tick", 0) or 0)
        if _enemy_memory_ticks.get(pos) != seen_tick:
            _enemy_memory_ticks[pos] = seen_tick
            _map_dirty = True

    # Remove stale sightings: some friendly unit can actually see the cell
    # (within its own vision radius, line of sight unobstructed) but no enemy
    # is there.  Obstacles block sight, so a spot behind a wall stays a hint.
    # Only a sighting inside *some* friendly's vision diamond can ever be
    # confirmed empty, so intersect the memory with the union of those diamonds
    # first instead of scanning every sighting against every friendly. The
    # expensive wall set is only materialised when a candidate survives.
    if _enemy_memory:
        candidates: set[tuple[int, int]] = set()
        for fpos, radius in friendly_views:
            fx, fy = fpos
            for dx in range(-radius, radius + 1):
                rem = radius - abs(dx)
                for dy in range(-rem, rem + 1):
                    candidates.add((fx + dx, fy + dy))
        stale_candidates = (_enemy_memory & candidates) - visible_enemy_positions
        if stale_candidates:
            if obstacles is None:
                obstacles = frozenset(_obstacle_memory) | turn.obstacle_cells
            stale: set[tuple[int, int]] = set()
            for sighting in stale_candidates:
                for fpos, radius in friendly_views:
                    if (
                        _manhattan(fpos, sighting) <= radius
                        and not _vision_obstructed(fpos, sighting, obstacles)
                    ):
                        stale.add(sighting)
                        break
            if stale:
                _enemy_memory -= stale
                for pos in stale:
                    _enemy_memory_types.pop(pos, None)
                    _enemy_memory_ticks.pop(pos, None)

    if len(_enemy_memory) != before:
        _map_dirty = True

    return added


# ── Categorized battle log (dashboard「战斗日志」panel) ────────────────────────
# The tactic process appends one row per discovery / combat / economy / failure
# each tick; the dashboard process appends config-change rows. Categories are
# the filter chips in the dashboard log panel.

_LOG_CAT_REASONS = {
    "HARVEST_FAILED": {
        "CARGO_FULL": "货舱满",
        "RESOURCE_DEPLETED": "矿点被抢先采空",
        "NOT_RESOURCE_CELL": "当前格非矿点",
    },
    "DEPOSIT_FAILED": {
        "CORE_RESOURCE_FULL": "仓库满",
        "CORE_NOT_PRESENT": "核心不在本格",
        "CORE_MOVING": "核心迁移中",
        "WORKER_EMPTY": "身上无货",
    },
    "CORE_SPAWN_FAILED": {
        "CELL_UNIT_LIMIT": "格位已满",
        "INSUFFICIENT_RESOURCES": "资源不足",
        "DETERMINISTIC_ID_COLLISION": "生成 ID 冲突",
    },
}
_UNIT_TYPE_LABELS = {"WORKER": "工人", "VANGUARD": "先锋", "RANGER": "游侠"}


def _battle_actor_name(turn: Any, object_id: Any) -> str:
    """Resolve a resolution-event object id to its stable display name."""
    if object_id is None:
        return "—"
    key = str(object_id)
    for unit in getattr(turn, "units", ()) or ():
        if str(unit.id) == key:
            prefix = _UNIT_NAME_PREFIX.get(getattr(unit, "unit_type", None), "U")
            return _object_name(object_id, prefix)
    for enemy in getattr(turn, "visible_enemies", ()) or ():
        if str(enemy.id) == key:
            return _object_name(object_id, "E")
    core = getattr(turn, "core", None)
    if core is not None and str(core.id) == key:
        return _object_name(object_id, "C")
    # A unit killed this Tick has already left the snapshot and its name was
    # pruned from the registry; recover the stable name captured in bookkeeping
    # so kill/defeat rows keep the unit's identity instead of minting a fresh
    # U### that reads as a different object.
    dead_name = getattr(turn_context, "dead_unit_names", {}).get(key)
    if dead_name is not None:
        return dead_name
    for prefix in ("W", "V", "R", "C", "E"):
        name = _object_names.get((prefix, key))
        if name is not None:
            return name
    return _object_name(object_id, "U")


def _battle_position_map(turn: Any) -> dict[str, tuple[int, int]]:
    """按对象 id 收集本回合各单位/敌人/核心的格子，供战斗日志附加坐标。

    事件的 actor_id / target_id 可能指向我方单位、可见敌人或核心；直接取
    当前回合状态里的 position，保证坐标与事件行同一时刻一致。
    """
    out: dict[str, tuple[int, int]] = {}
    for obj in list(getattr(turn, "units", ()) or ()) + list(getattr(turn, "visible_enemies", ()) or ()):
        pos = getattr(obj, "position", None)
        if getattr(obj, "id", None) is not None and pos is not None:
            out[str(obj.id)] = (int(pos[0]), int(pos[1]))
    core = getattr(turn, "core", None)
    if core is not None:
        pos = getattr(core, "position", None)
        if getattr(core, "id", None) is not None and pos is not None:
            out[str(core.id)] = (int(pos[0]), int(pos[1]))
    return out


def _fmt_cell(pos: Any) -> str:
    """格子坐标格式化为 "(x,y)"，与发现日志的写法保持一致。"""
    return f"({int(pos[0])},{int(pos[1])})"


def _classify_battle_event(
    turn: Any,
    event: Any,
    positions: dict[str, tuple[int, int]] | None = None,
) -> tuple[str | None, str | None]:
    """Map one resolution event to a (category, human message) log row.

    消息末尾附带坐标（无坐标可附时保持原样），优先级：
    1. actor 与 target 双方当前格子不同 → "(ax,ay)→(tx,ty)"（战斗事件最直观）；
    2. 事件自带的结算格 event.position；
    3. 回退到 actor / target 当前所在格。
    """
    et = getattr(event, "event_type", "")
    reason = getattr(event, "reason_code", None) or ""
    actor = _battle_actor_name(turn, getattr(event, "actor_id", None))
    target = _battle_actor_name(turn, getattr(event, "target_id", None))
    values = getattr(event, "values", None) or {}
    # SDK ResolutionEvent exposes `resource_amount` as a property; tests / stub
    # objects carry the raw value in `values["amount"]` instead.
    amount = getattr(event, "resource_amount", None)
    if amount is None:
        amount = values.get("amount")

    # 坐标后缀：双方箭头 > 事件结算格 > actor 格 > target 格
    if positions is None:
        positions = _battle_position_map(turn)
    actor_id = getattr(event, "actor_id", None)
    target_id = getattr(event, "target_id", None)
    actor_cell = positions.get(str(actor_id)) if actor_id is not None else None
    target_cell = positions.get(str(target_id)) if target_id is not None else None
    if actor_cell is not None and target_cell is not None and actor_cell != target_cell:
        cell_suffix = f" {_fmt_cell(actor_cell)}→{_fmt_cell(target_cell)}"
    elif getattr(event, "position", None) is not None:
        cell_suffix = f" {_fmt_cell(event.position)}"
    elif actor_cell is not None:
        cell_suffix = f" {_fmt_cell(actor_cell)}"
    elif target_cell is not None:
        cell_suffix = f" {_fmt_cell(target_cell)}"
    else:
        cell_suffix = ""

    def row(cat: str, msg: str) -> tuple[str, str]:
        """返回 (分类, 消息+坐标后缀)。"""
        return cat, f"{msg}{cell_suffix}"

    if et == "SHOT_HIT":
        dmg = values.get("damage")
        return row("combat", f"{actor} 击中 {target}" + (f" 造成 {dmg} 伤害" if dmg else ""))
    if et == "SWEEP_RESOLVED":
        n = values.get("targets_hit", 0)
        return row("combat", f"{actor} 横扫命中 {n} 个目标")
    if et == "DESTRUCTION_PARTICIPATION":
        return row("kill", f"{actor} 参与摧毁 {target}")
    if et == "CORE_RESOURCES_CAPTURED":
        return row("kill", "摧毁敌方核心" + (f"，缴获 {amount} 资源" if amount else ""))
    if et == "UNIT_DAMAGED":
        hp = values.get("hp")
        dmg = values.get("damage")
        if hp == 0:
            # An enemy reduced to 0 HP is our kill, not a defeat; only friendly
            # (W/V/R/C) names belong in the defeat category.
            if target.startswith("E"):
                return row("kill", f"{actor} 击败 {target}")
            return row("defeat", f"{target} 被击败")
        return row("combat", f"{target} 受 {dmg} 伤害（HP {hp}）")
    if et == "UNIT_SELF_DESTRUCTED":
        return row("defeat", f"{actor} 超编自裁")
    if et == "CORE_DAMAGED":
        return row("defeat", "核心受到攻击")
    if et == "CORE_DESTROYED":
        return row("defeat", "核心被摧毁")
    if et == "HARVEST_SUCCEEDED":
        return row("economy", f"{actor} 挖矿 +{amount or '?'}")
    if et == "DEPOSIT_SUCCEEDED":
        return row("economy", f"{actor} 卸货 +{amount or '?'}")
    if et == "BEACON_HARVEST_BONUS":
        return row("economy", f"{actor} 信标加成 +{amount or '?'}")
    if et == "CORE_REPAIR_SUCCEEDED":
        return row("economy", "核心修盾 +1")
    if et in ("CORE_HEAL_SUCCEEDED", "UNIT_HEAL_SUCCEEDED"):
        who = "核心" if et.startswith("CORE") else actor
        return row("economy", f"{who} 回血 +{amount or '?'}")
    if et == "CORE_SPAWN_SUCCEEDED":
        tname = _UNIT_TYPE_LABELS.get(str(values.get("unit_type", "")), "单位")
        cost = values.get("cost")
        suffix = f"（{cost} 资源）" if cost is not None else ""
        return row("economy", f"生产 {tname}{suffix}")
    if et == "CORE_RESOURCE_OVERFLOW_DESTROYED":
        return row("economy", f"人口下降，{amount or '?'} 资源被销毁")

    if et == "HARVEST_FAILED":
        return row("warn", f"{actor} 挖矿失败：{_LOG_CAT_REASONS['HARVEST_FAILED'].get(reason, reason)}")
    if et == "DEPOSIT_FAILED":
        return row("warn", f"{actor} 卸货失败：{_LOG_CAT_REASONS['DEPOSIT_FAILED'].get(reason, reason)}")
    if et == "SHOT_MISSED":
        return row("warn", f"{actor} 射击未命中")
    if et in ("CORE_HEAL_FAILED", "UNIT_HEAL_FAILED", "CORE_REPAIR_FAILED"):
        return row("warn", f"修复/回血失败：{reason or '—'}")
    if et == "CORE_SPAWN_FAILED":
        why = _LOG_CAT_REASONS["CORE_SPAWN_FAILED"].get(reason, reason or "—")
        return row("warn", f"生产失败：{why}")
    if et in (
        "UNIT_MOVE_FAILED",
        "CORE_MOVE_FAILED",
        "CORE_MOVE_START_FAILED",
        "CORE_ACTION_FAILED",
    ):
        return row("warn", f"{actor} 移动/动作失败：{reason or '—'}")
    return None, None


def _battle_event_fields(turn: Any, event: Any) -> dict[str, Any]:
    """Structured columns that back a battle-log row.

    The panel still renders ``msg``; these fields let a replay or an offline
    query answer "who hit whom, for how much" without parsing Chinese text.
    The SPA endpoint deliberately still exposes only tick/ts/cat/msg.
    """
    actor_id = getattr(event, "actor_id", None)
    target_id = getattr(event, "target_id", None)
    pos = getattr(event, "position", None)
    return {
        "event": getattr(event, "event_type", None),
        "resolved_tick": getattr(event, "tick", None),
        "actor": str(actor_id)[:8] if actor_id else None,
        "target": str(target_id)[:8] if target_id else None,
        "pos": list(pos) if pos is not None else None,
        "values": _event_values(event),
    }


def _battle_log_entries(
    turn: Any,
    *,
    new_resources: set[tuple[int, int]],
    new_enemy_sightings: set[tuple[int, int]],
) -> list[dict]:
    """Build the categorized log rows for one Tick (discoveries + events)."""
    entries: list[dict] = []
    tick = turn_context.tick
    ts = time.time()  # wall-clock stamp so the panel shows when each batch happened
    for pos in sorted(new_resources):
        entries.append({
            "tick": tick, "ts": ts, "cat": "discover",
            "msg": f"发现新矿点 ({pos[0]},{pos[1]})",
        })
    for pos in sorted(new_enemy_sightings):
        entries.append({
            "tick": tick, "ts": ts, "cat": "discover",
            "msg": f"发现敌人踪迹 ({pos[0]},{pos[1]})",
        })
    # 预建一次对象位置索引，避免对每个事件重复遍历单位列表
    positions = _battle_position_map(turn)
    for event in getattr(turn, "events", ()) or ():
        cat, msg = _classify_battle_event(turn, event, positions)
        if cat and msg:
            entry = {"tick": tick, "ts": ts, "cat": cat, "msg": msg}
            entry.update(_battle_event_fields(turn, event))
            entries.append(entry)
    return entries


# Context holder for the current turn's resource_space (set by choose_actions)
turn_context = type(
    "_Ctx",
    (),
    {
        "resource_space": 0,
        "worker_routes": {},
        "unit_routes": {},
        "tick": 0,
        "beacon_pos": None,
        "core_pos": None,
        "attack_squad_pos": None,
        "attack_squad_size": 0,
        "attack_retreat": False,
        "attack_retreat_from": None,
        "attack_forbidden_targets": frozenset(),
        "attack_decision": None,
        "attack_target_candidates": [],
        "attack_queue_target": None,
        "kite_squad_pos": None,
        "kite_directives": {},
        "kite_decisions": [],
        "kite_target_candidates": [],
        "kite_queue_target": None,
        "guerrilla_decisions": [],
        "kite_collision_snapshot": None,
        "kite_ranger_range": 3,
        "kite_obstacles": frozenset(),
        "dead_unit_names": {},
        "recent_death_cells": frozenset(),
        "mine_danger_cells": frozenset(),
        "defense_targets": (),
        "home_defense_leash": 12,
        "home_defense_decisions": {},
        "config_values": None,
        "config_rev": "",
        "config_changed": [],
        "config_delta": {},
        "shot_predictions": [],
        "shot_prediction_results": [],
        # Per-Tick planning profiler output (filled by choose_actions, read by
        # play() and forwarded to record_tick). Held on turn_context so
        # choose_actions keeps its single-arg signature.
        "plan_phase_ms": {},
        "plan_pathfind_calls": 0,
        "plan_pathfind_expansions": 0,
        "plan_pathfind_ms": 0.0,
        "plan_dead_end_ms": 0.0,
        "plan_dead_end_runs": 0,
    },
)()


def _spawn_cost(unit_type: UnitType, population: int) -> int:
    """Estimate the dynamic production price for a unit type (game rules v0.14).

    Units 1-20 cost the base price; the 21st Unit is the first +30% and the exact
    1.3 multiplier rises again every five more Units. The server settles the price
    after same-Tick Unit self-destructs and combat deaths, so this is a preview —
    `CORE_SPAWN_SUCCEEDED.values.cost` / `CORE_SPAWN_FAILED.values.required` are
    authoritative.
    """
    try:
        return unit_cost(unit_type, population)
    except Exception:
        return UNIT_SPAWN_COSTS[unit_type.name]


def _spawn_order(turn: Any, config: dict[str, Any]) -> tuple[UnitType, ...]:
    """Production order for one Tick: workers first, then alternating combat.

    While the worker count is below `target_workers`, the fixed worker-first
    priority applies. Once workers are full, Vanguards and Rangers alternate —
    the type with fewer live units goes next, ties go to the Vanguard — so the
    two types grow evenly (V, R, V, R...) instead of Vanguards filling up first.
    """
    worker_target = int(config.get(_TARGET_KEYS[UnitType.WORKER], _TARGET_DEFAULTS[UnitType.WORKER]))
    if _unit_type_count(turn, UnitType.WORKER) < worker_target:
        return _SPAWN_PRIORITY
    combat = sorted(
        (UnitType.VANGUARD, UnitType.RANGER),
        key=lambda unit_type: _unit_type_count(turn, unit_type),
    )
    return (UnitType.WORKER, *combat)


def _plan_demand_spawn(turn: Any, core: Any, resources: int, config: dict[str, Any]) -> str | None:
    """Spawn the highest-priority type whose current count is below its target.

    Demand-based production: counts are recomputed every Tick, so a successful
    spawn naturally stops further production (count +1) and a failed one is
    retried on the next Tick — no inflight bookkeeping is needed because the
    Core takes exactly one action per Tick.

    Spawn only when:
    - the Core cell is free (a unit standing on it blocks the spawn),
    - resources cover `cost + resource_reserve`.
    """
    if any(tuple(unit.position) == tuple(core.position) for unit in turn.units):
        return None

    population = getattr(getattr(turn, "state", None), "population", 0) or 0
    reserve = int(config.get("resource_reserve", 0))
    for unit_type in _spawn_order(turn, config):
        count = _unit_type_count(turn, unit_type)
        target = int(config.get(_TARGET_KEYS[unit_type], _TARGET_DEFAULTS[unit_type]))
        if count >= target:
            continue
        cost = _spawn_cost(unit_type, population)
        if resources < cost + reserve:
            continue
        try:
            core.spawn(unit_type)
        except Exception:
            return None
        return f"SPAWN_{unit_type.name}"
    return None


def _unit_type_count(turn: Any, unit_type: UnitType) -> int:
    attr = {
        UnitType.WORKER: "workers",
        UnitType.VANGUARD: "vanguards",
        UnitType.RANGER: "rangers",
    }[unit_type]
    return len(getattr(turn, attr, ()) or ())


def _prune_dead_unit_bookkeeping(alive_ids: set[str]) -> None:
    """Drop per-unit bookkeeping for units that no longer exist.

    Keys in _worker_last_pos / _worker_recent / _resource_assignments use the
    full str(unit.id). Previously cleanup used id[:8], which wiped every entry
    each tick and disabled backtrack avoidance → A-B-A oscillation. Also prunes
    the A* path cache and _object_names so a long-lived process does not grow
    without bound. Core (C) and enemy (E) names are kept — enemy visibility is
    intermittent, so pruning those would churn their names.
    """
    # Every table below is a {unit_id: ...} dict; drop entries for units that
    # no longer exist. The keys() view's set difference avoids materialising a
    # full copy of each table's keys every Tick (~40 tables on the live server).
    for table in (
        _worker_last_pos, _worker_recent, _worker_explore_history,
        _worker_rally_home, _worker_planned_step, _worker_contest_blocked,
        _worker_contest_repeat, _resource_assignments, _worker_path_cache,
        _cargo_route_miss_dist, _march_route_miss, _cell_limit_blocked,
        _combat_path_cache, _home_engage_target, _guerrilla_core_target,
        _guerrilla_unit_target, _worker_stuck_ticks, _worker_stuck_pos,
        _waypoint_stuck, _kite_collision_streak, _kite_stall_pos,
        _home_chute_wait,
        _kite_stall_ticks, _kite_prev_pos, _kite_shuttle_ticks,
        _kite_prev_prev_pos, _kite_friendly_split, _roam_pos_history,
        _roam_escape_left, _roam_stall_cells, _roam_stall_origin,
        _roam_banned, _roam_bearing_offset, _roam_flip_pending,
        _roam_discovered, _roam_seen, _roam_new_stall, _roam_recent,
        _roam_break_tick, _sweep_miss_streak, _sweep_fuse_until,
        _roam_flips_used,
    ):
        for dead_id in table.keys() - alive_ids:
            table.pop(dead_id, None)
    for (prefix, obj_id), name in list(_object_names.items()):
        if prefix in ("W", "V", "R") and str(obj_id) not in alive_ids:
            _object_names.pop((prefix, obj_id), None)


# ── post-combat healing (rules v0.10) ─────────────────────────────────────────
# A Unit may spend its whole action to recover HP only while sharing a cell with
# its own stationary Core; the Core may heal as its action. 1 resource = 1 HP,
# resolved after combat (Unit heals before the Core action). A heal that is
# still impossible at resolution fails privately and spends nothing.

_UNIT_MAX_HP = {UnitType.WORKER: 2, UnitType.VANGUARD: 4, UnitType.RANGER: 2}
_CORE_MAX_HP = 5


def _unit_max_hp(unit: Any) -> int:
    return _UNIT_MAX_HP.get(getattr(unit, "unit_type", None), 2)


def _unit_needs_heal(
    unit: Any,
    *,
    core_pos: tuple[int, int],
    core_moving: bool,
    heal_budget: int,
    heal_enabled: bool,
) -> bool:
    """True when a Unit should spend its whole action on HEAL this Tick."""
    if not heal_enabled or core_moving:
        return False
    if heal_budget < 1:
        return False
    if tuple(unit.position) != tuple(core_pos):
        return False
    if getattr(unit, "hp", 0) >= _unit_max_hp(unit):
        return False
    # A loaded Worker at the Core must deposit first — unloading funds the
    # whole economy; it can heal next Tick.
    if (
        getattr(unit, "unit_type", None) == UnitType.WORKER
        and getattr(unit, "cargo", 0) > 0
    ):
        return False
    return True


def _core_should_heal(core: Any, resources: int, config: dict) -> bool:
    """True when the Core should spend its action recovering HP (HP first)."""
    return (
        bool(config.get("heal_enabled", True))
        and resources >= 1
        and int(getattr(core, "hp", 0)) < _CORE_MAX_HP
    )


def _unit_should_return_to_heal(
    unit: Any,
    config: dict[str, Any],
    *,
    core_pos: tuple[int, int],
    core_moving: bool,
    team: str,
) -> bool:
    """True when a 守家队 combat unit should march back to the Core to heal.

    Only the home squad returns to heal — attack / guerrilla / unassigned units
    keep fighting damaged, since the passive HEAL branch in choose_actions only
    fires when a unit is already standing on the Core cell. The march is gated
    on the Core being stationary and healing enabled so units don't chase a
    moving Core; the actual HEAL still resolves against the real resource pool
    (1 HP = 1 resource) and fails privately if it can't.
    """
    if team != "home":
        return False
    if unit.unit_type not in (UnitType.VANGUARD, UnitType.RANGER):
        return False
    if not bool(config.get("heal_enabled", True)):
        return False
    if core_moving:
        return False
    threshold = int(config.get("combat_heal_hp_threshold", 2))
    if threshold < 1:
        return False
    hp = int(getattr(unit, "hp", 0) or 0)
    if hp <= 0:
        return False
    if hp >= _unit_max_hp(unit):
        return False
    if hp >= threshold:
        return False
    if tuple(unit.position) == tuple(core_pos):
        return False  # already on the Core cell -> the HEAL branch handles it
    return True


# ── Stall alerting lives in watchdog.update_stall_tracking: choose_actions
# feeds every Tick's full unit snapshot (uid, position, status) into it, and
# the watchdog prints one [watchdog] stall_alert line per stall episode
# (stdout -> tactic_play.log via docker-entrypoint.py). tactic.py keeps no
# stall-alert state of its own.


def choose_actions(turn) -> tuple[str, dict[str, str]]:
    """Plan one Tick, holding every A* search to a shared wall-clock budget."""
    global _plan_pathfind_deadline
    _plan_pathfind_deadline = time.monotonic() + PLAN_PATHFIND_BUDGET_S
    try:
        return _choose_actions(turn)
    finally:
        _plan_pathfind_deadline = None


def _choose_actions(turn) -> tuple[str, dict[str, str]]:
    """Queue actions, return (core_action_name, {unit_id: action_detail})."""
    _phase = _PhaseTimer()
    _reset_pathfind_counters()
    _reset_plan_profile_context()
    unit_actions_detail: dict[str, str] = {}
    core_action_name = "WAIT"
    config = load_config()
    # Per-Tick decision telemetry: reset before planning so a stale verdict or
    # candidate list from the previous Tick can never be logged as this one's.
    turn_context.attack_decision = None
    turn_context.attack_target_candidates = []
    turn_context.kite_target_candidates = []
    turn_context.home_defense_decisions = {}
    turn_context.home_defense_leash = max(
        int(config.get("home_defense_leash", 12) or 0), 0,
    )
    turn_context.worker_routes = {}
    turn_context.unit_routes = {}
    turn_context.tick = int(getattr(turn, "tick", 0) or 0)
    _discard_noncollision_move_failures(turn)
    _record_sweep_resolutions(turn, bool(config.get("sweep_miss_fuse", True)))
    turn_context.shot_predictions = []
    turn_context.kite_decisions = []
    turn_context.guerrilla_decisions = []
    _phase.start("prediction")
    turn_context.shot_prediction_results = _resolve_shadow_predictions(
        turn, turn_context.tick,
    )
    game_stats.record_prediction_results(
        _game_stats, turn_context.shot_prediction_results,
    )
    _update_enemy_motion_tracks(turn.visible_enemies, turn_context.tick)
    _phase.stop()

    # Manual per-unit targets set from the dashboard (display-name keyed).
    # Prune targets whose unit no longer exists — names are computed BEFORE the
    # dead-unit cleanup below so a just-died unit's name is never re-issued.
    _phase.start("bookkeeping")
    alive_names = {
        _object_name(u.id, _UNIT_NAME_PREFIX.get(u.unit_type, "U"))
        for u in turn.units
    }
    waypoints = _load_and_prune_waypoints(alive_names)

    # Manual per-unit hold-position (驻守) commands set from the dashboard.
    # Held units stand in place and auto-attack instead of following their
    # normal program; the dashboard toggles the entry off to release them.
    holds = _load_and_prune_holds(alive_names)

    # Manual per-unit self-destruct commands set from the dashboard. Commands
    # for units still alive are issued in the unit loop below, then removed.
    self_destructs = _load_and_prune_self_destructs(alive_names)

    # ── Cleanup dead-unit bookkeeping ──────────────────────────────────
    alive_ids: set[str] = set()
    for w in turn.workers:
        alive_ids.add(str(w.id))
    for v in getattr(turn, "vanguards", ()) or ():
        alive_ids.add(str(v.id))
    for r in getattr(turn, "rangers", ()) or ():
        alive_ids.add(str(r.id))
    # Detect this Tick's deaths before the name registry is pruned: keep the
    # dead unit's stable name for the battle log and mark its cell in the death
    # heat map so mine assignment and home defence react to the ambush.
    global _prev_unit_snapshot, _death_heat
    _tick_now = turn_context.tick
    _snapshot: dict[str, tuple[str, tuple[int, int]]] = {}
    for u in turn.units:
        _snapshot[str(u.id)] = (
            _object_name(u.id, _UNIT_NAME_PREFIX.get(u.unit_type, "U")),
            tuple(u.position),
        )
    dead_names: dict[str, str] = {}
    for uid, (name, pos) in _prev_unit_snapshot.items():
        if uid not in _snapshot:
            dead_names[uid] = name
            _death_heat[pos] = _tick_now
    _prev_unit_snapshot = _snapshot
    _death_heat = {
        cell: tick for cell, tick in _death_heat.items()
        if _tick_now - tick <= _DEATH_HEAT_TTL
    }
    turn_context.dead_unit_names = dead_names
    turn_context.recent_death_cells = frozenset(_death_heat)
    _prune_dead_unit_bookkeeping(alive_ids)
    # Ally position view for the same-cell friendly-split ranking (read-only).
    turn_context.units_view = tuple(turn.units)
    turn_context.kite_collision_snapshot = dict(_kite_collision_streak)

    # Per-tick stall-alert feed: the watchdog tracks every unit's net
    # displacement and prints one [watchdog] stall_alert line (stdout ->
    # tactic_play.log via docker-entrypoint.py) once a live unit stays within
    # one cell of its anchor for watchdog.STALL_ALERT_THRESHOLD ticks. This
    # is the only producer of stall alerts — best-effort by contract, the
    # watchdog swallows its own errors and never breaks the game loop.
    watchdog.update_stall_tracking(turn.units, turn_context.tick)

    # ── Aggregate battle-report statistics ─────────────────────────────
    game_stats.sync_units(_game_stats, turn, turn_context.tick)
    game_stats.record_events(_game_stats, turn, turn_context.tick)
    game_stats.sampled(_game_stats, turn_context.tick)
    game_stats.maybe_save(_game_stats, turn_context.tick)
    _phase.stop()

    # ── Update permanent map memory ────────────────────────────────────
    # Honor dashboard clears before we re-accumulate or plan against memory.
    _phase.start("map_memory")
    _apply_dashboard_map_edits()
    known_obstacles = _update_obstacle_memory(turn)
    new_resources = _update_resource_memory(turn)
    new_enemy_sightings = _update_enemy_sightings(turn, known_obstacles)
    _save_map_memory(
        tick=getattr(turn, "tick", None),
        save_interval_ticks=int(config["map_save_interval_ticks"]),
        defer=True,
    )
    _phase.stop()

    # ── Categorized battle log (discoveries + resolution events) ────────
    # Best-effort: a log write must never break the game loop, so failures
    # are swallowed.  New discoveries are diffed from the memory updates above;
    # combat/economy/warn rows come from this Tick's resolution events.
    _phase.start("battle_log")
    battle_entries = _battle_log_entries(
        turn,
        new_resources=new_resources,
        new_enemy_sightings=new_enemy_sightings,
    )
    if battle_entries:
        try:
            append_jsonl(BATTLE_LOG_PATH, battle_entries)
        except OSError:
            pass
    _phase.stop()

    # ── Lifecycle guard ─────────────────────────────────────────────────
    if turn.core is None:
        _publish_plan_profile(_phase)
        turn_context.config_rev, turn_context.config_changed = _config_revision(config)
        turn_context.config_delta = {
            key: config.get(key) for key in turn_context.config_changed
        }
        return ("RESPAWN", {})

    _phase.start("core_setup")
    # Newly produced (or previously unassigned) combat units join 守家队.
    config = _auto_enlist_new_combat_units(turn, config)
    turn_context.config_rev, turn_context.config_changed = _config_revision(config)
    turn_context.config_delta = {
        key: config.get(key) for key in turn_context.config_changed
    }

    core = turn.core
    core_pos = core.position
    resources = turn.resources
    turn_context.resource_space = turn.resource_space

    resource_cells: frozenset[tuple[int, int]] = turn.resource_cells
    # Use permanent obstacle memory for pathfinding, not just current vision
    obstacle_cells: frozenset[tuple[int, int]] = known_obstacles
    enemies = turn.visible_enemies
    beacon = turn.beacon
    # Beacon position is always public; keep it for the attack teams.
    turn_context.beacon_pos = (
        tuple(beacon.position) if getattr(beacon, "position", None) else None
    )

    # Shared auto-attack references: the CORE's cell and the attack squad's
    # current centroid. Both are the same for every squad member, so auto mode
    # picks one target the whole team marches on (see _plan_attack_combat).
    turn_context.core_pos = tuple(core_pos)
    attack_squad_names = _parse_team_names(config.get("attack_team", ""))
    squad_cells = [
        tuple(u.position)
        for u in turn.units
        if _object_name(u.id, _UNIT_NAME_PREFIX.get(u.unit_type, "U")).upper()
        in attack_squad_names
    ]
    if squad_cells:
        turn_context.attack_squad_pos = (
            round(sum(x for x, _ in squad_cells) / len(squad_cells)),
            round(sum(y for _, y in squad_cells) / len(squad_cells)),
        )
    else:
        turn_context.attack_squad_pos = None

    kite_squad_names = _parse_team_names(config.get("kite_team", ""))
    kite_cells = [
        tuple(u.position)
        for u in turn.units
        if u.unit_type in (UnitType.VANGUARD, UnitType.RANGER)
        and _object_name(u.id, _UNIT_NAME_PREFIX.get(u.unit_type, "U")).upper()
        in kite_squad_names
    ]
    turn_context.kite_squad_pos = (
        (
            round(sum(x for x, _ in kite_cells) / len(kite_cells)),
            round(sum(y for _, y in kite_cells) / len(kite_cells)),
        )
        if kite_cells else None
    )
    turn_context.kite_ranger_range = int(config.get("ranger_attack_range", 3))
    turn_context.kite_obstacles = obstacle_cells
    turn_context.kite_directives = _prepare_kite_coordination(
        turn.units, enemies, config, obstacle_cells,
    )

    # ── Squad target queues (到达且肃清后推进下一个) ──────────────
    # Only the coords mode consumes a queue; auto/beacon keep their own
    # targeting. Advancement pops completed heads from squad_targets.json and
    # logs the progression into the battle log (best-effort, like all rows).
    squad_targets = _load_squad_targets()
    turn_context.attack_queue_target = None
    turn_context.kite_queue_target = None
    queue_log_entries: list[dict] = []
    for squad, mode_key, squad_pos, attr in (
        ("attack", "attack_mode", turn_context.attack_squad_pos, "attack_queue_target"),
        ("kite", "kite_mode", turn_context.kite_squad_pos, "kite_queue_target"),
    ):
        if str(config.get(mode_key, "coords")) != "coords":
            continue
        queue = squad_targets.get(squad) or []
        if not queue:
            continue
        current, _ = _advance_squad_target_queue(
            squad, queue, squad_pos, enemies, queue_log_entries,
        )
        setattr(turn_context, attr, current)
    if queue_log_entries:
        try:
            append_jsonl(BATTLE_LOG_PATH, queue_log_entries)
        except OSError:
            pass

    # Squad-wide auto-attack retreat decision (computed once per Tick so every
    # attack-team member acts on the same verdict). The squad disengages when
    # it is outnumbered or out-HP'd by the enemies inside its engagement leash;
    # the view is widened to attack_march_engage_radius because the squad trades
    # across distances attack_retreat_radius alone never looked at. radius == 0
    # disables the policy (see _attack_retreat_decision).
    retreat_radius = int(config.get("attack_retreat_radius", 5))
    squad_size = len(squad_cells)
    turn_context.attack_squad_size = squad_size
    # The squad fights at the march engagement leash, so the retreat verdict
    # must look at least that far instead of stopping at attack_retreat_radius
    # (4 by default), which is shorter than the gap it actually trades across.
    retreat_view = max(
        retreat_radius,
        int(config.get("attack_march_engage_radius", 0) or 0),
    )
    squad_units = tuple(
        unit
        for unit in turn.units
        if _object_name(unit.id, _UNIT_NAME_PREFIX.get(unit.unit_type, "U"))
        .upper() in attack_squad_names
    )
    attack_detail: dict[str, Any] = {}
    retreat, enemy_count, cluster_centroid, forbidden = _attack_retreat_decision(
        enemies,
        turn_context.attack_squad_pos,
        squad_size,
        retreat_radius,
        _enemy_memory,
        squad_units=squad_units,
        threat_radius=retreat_view,
        detail_out=attack_detail,
    )
    attack_detail.update({
        "tick": int(getattr(turn_context, "tick", 0) or 0),
        "squad_pos": (
            list(turn_context.attack_squad_pos)
            if turn_context.attack_squad_pos is not None else None
        ),
        "squad_size": squad_size,
        "centroid": list(cluster_centroid) if cluster_centroid else None,
        "forbidden": len(forbidden),
        "mode": str(config.get("attack_mode", "coords")),
        "radius": retreat_radius,
        "view": retreat_view,
        "retreat": bool(retreat),
    })
    turn_context.attack_decision = attack_detail
    # Only the auto mode honors the outnumbered-retreat policy; beacon/coords
    # mode march on a fixed destination and don't re-target from memory.
    if retreat and str(config.get("attack_mode", "coords")) == "auto":
        turn_context.attack_retreat = True
        turn_context.attack_retreat_from = cluster_centroid
        turn_context.attack_forbidden_targets = forbidden
    else:
        turn_context.attack_retreat = False
        turn_context.attack_retreat_from = None
        turn_context.attack_forbidden_targets = frozenset()

    beacon_on_ground_here = beacon.status == "GROUND" and beacon.position == core_pos
    beacon_carried_by_core = beacon.status == "CARRIED" and beacon.carrier_id == core.id

    # Build depleted set from previous Tick events
    depleted: set[tuple[int, int]] = set()
    for event in turn.events:
        if (
            event.event_type == "HARVEST_FAILED"
            and event.reason_code == "RESOURCE_DEPLETED"
            and event.position
        ):
            depleted.add(event.position)
    _phase.stop()  # core_setup

    # 金币满仓 + 开启"满仓探索"时，不给工人派矿点：空载工人失去目标后自然进入
    # 探索分支，去各处侦察。仓库一旦有空位，下一 tick 恢复派矿。
    _phase.start("resource_assign")
    gold_full_explore = (
        bool(config.get("worker_explore_when_full", False))
        and turn_context.resource_space == 0
    )
    # Defined in both branches below: the core-movement heuristic reads it even
    # in full-explore mode, so it must never be left undefined.
    all_resources: list = []
    turn_context.mine_danger_cells = frozenset()
    turn_context.defense_targets = ()
    if gold_full_explore:
        _resource_assignments.clear()
    else:
        # Assign each resource to one worker (avoid stampede).
        # Keep sticky assignments across ticks so two nearby workers do not swap the
        # same mine every tick (that produced goal/explore A-B-A flipping).
        # Only empty workers mine: a worker carrying cargo (full or partial) heads
        # home to deposit, so it is never assigned a fresh mine.
        idle_workers = [(str(w.id), tuple(w.position)) for w in turn.workers if w.cargo == 0]
        idle_ids = {wid for wid, _ in idle_workers}
        all_resources = _merge_resource_cells(
            turn.resource_cells,
            _resource_memory,
            depleted,
        )

        # Workers ignore mines farther than this Manhattan distance from the core —
        # a far deposit round-trip wastes more ticks than it earns, and a worker
        # stranded out deep is easy prey. 0 (default) disables the cap.
        mine_max_distance = int(config.get("worker_mine_max_distance", 0))
        if mine_max_distance > 0:
            all_resources = [
                r for r in all_resources
                if _manhattan(tuple(r), core_pos) <= mine_max_distance
            ]

        # Dangerous mines: covered by enemy fire (real attack geometry, so a
        # diagonal Ranger at (2,2) bans the mine it can actually shoot), adjacent
        # to any visible enemy (a contested cell both sides race for), or near a
        # recent ally death. Ban them with hysteresis: the old instantaneous
        # "enemy within 1 cell" filter flickered as campers milled about, flipping
        # sticky assignments every few Ticks, and it never saw the diagonal shot
        # that kept killing workers at the same mine. The ban persists after the
        # threat leaves so routes stabilise and workers are not fed back into the
        # ambush the moment a camper steps out of vision.
        global _mine_danger_ban
        danger_cells = _enemy_danger_cells(
            enemies, obstacle_cells, int(config.get("ranger_attack_range", 3)),
        )
        turn_context.mine_danger_cells = frozenset(danger_cells)
        enemy_cells = {tuple(e.position) for e in enemies}
        death_cells = turn_context.recent_death_cells
        for res in all_resources:
            rt = tuple(res)
            if (
                rt in danger_cells
                or any(_manhattan(rt, ec) <= 1 for ec in enemy_cells)
                # 2026-11-23: W24/W14/W20 died 2-4 cells from the mine they
                # were walking to within 60 Ticks; a 1-cell ban never fired.
                or any(_chebyshev(rt, dc) <= 4 for dc in death_cells)
            ):
                _mine_danger_ban[rt] = _tick_now + _MINE_DANGER_BAN_TICKS
        _mine_danger_ban = {
            cell: until for cell, until in _mine_danger_ban.items()
            if until > _tick_now
        }
        all_resources = [
            r for r in all_resources
            if _mine_danger_ban.get(tuple(r), 0) <= _tick_now
        ]
        # Home-defence sortie targets: visible combat units lurking by a recent
        # death or a banned mine, within the mining radius. Home defenders chase
        # these even far outside the core patrol ring (see _plan_home_combat),
        # so the squad clears the camp instead of idling on the delivery ring.
        turn_context.defense_targets = tuple(
            e for e in _combat_threats(enemies)
            if (mine_max_distance <= 0
                or _manhattan(tuple(e.position), core_pos) <= mine_max_distance)
            and (
                any(_chebyshev(tuple(e.position), dc) <= 4 for dc in death_cells)
                or any(
                    _chebyshev(tuple(e.position), cell) <= 4
                    for cell in _mine_danger_ban
                )
            )
        )
        # Periodic situation row so a later log review can quantify whether the
        # danger ban / death heat / home sortie are engaging the camp.
        global _last_defense_log_tick
        if _tick_now - _last_defense_log_tick >= _DEFENSE_LOG_EVERY:
            _last_defense_log_tick = _tick_now
            try:
                append_jsonl(BATTLE_LOG_PATH, [{
                    "tick": _tick_now,
                    "ts": time.time(),
                    "cat": "combat",
                    "msg": (
                        f"经济防卫：禁采矿 {len(_mine_danger_ban)} 个，"
                        f"阵亡热点 {len(death_cells)} 处，"
                        f"出击目标 {len(turn_context.defense_targets)} 个"
                    ),
                }])
            except OSError:
                pass

        # A mine under the Core is not mineable: an empty worker must vacate the
        # chute before HARVEST, so assigning it produces a vacate/return A-B-A
        # loop and starves Core production with CELL_UNIT_LIMIT.
        assignable_resources = [
            r for r in all_resources if tuple(r) != tuple(core_pos)
        ]

        # Sticky validation must use the filtered set, otherwise a worker keeps
        # its old assignment to a mine that just became contested/out of range.
        resource_set = set(assignable_resources)

        sticky: dict[str, tuple[int, int]] = {}
        claimed_resources: set[tuple[int, int]] = set()
        for wid, res in list(_resource_assignments.items()):
            res_t = tuple(res)
            if wid in idle_ids and res_t in resource_set and res_t not in claimed_resources:
                sticky[wid] = res_t
                claimed_resources.add(res_t)
        _resource_assignments.clear()
        _resource_assignments.update(sticky)

        available = [w for w in idle_workers if w[0] not in sticky]
        open_resources = [r for r in assignable_resources if r not in claimed_resources]
        for res in sorted(
            open_resources,
            key=lambda p: min(_manhattan(p, w[1]) for w in available) if available else 0,
        ):
            if not available:
                break
            closest = min(available, key=lambda w: _manhattan(res, w[1]))
            wid = closest[0]
            _resource_assignments[wid] = res
            available.remove(closest)
    _phase.stop()  # resource_assign

    # ── Core action ─────────────────────────────────────────────────────
    _phase.start("core_action")
    core_done = False

    # Core danger freeze (see _CORE_DANGER_RADIUS): any visible combat enemy
    # near the Core refreshes a linger window during which the Core neither
    # starts a migration step nor gives up shield repair.
    global _core_danger_until
    if any(
        _chebyshev(tuple(e.position), tuple(core_pos)) <= _CORE_DANGER_RADIUS
        for e in _combat_threats(enemies)
    ):
        _core_danger_until = turn_context.tick + _CORE_DANGER_LINGER_TICKS
    _core_in_danger = turn_context.tick < _core_danger_until

    # Respawn: suppress a stale manual core target (see
    # _CORE_RESPAWN_TARGET_SUPPRESS_TICKS). Lifted as soon as the operator
    # saves a different target, or after the timeout.
    for _ev in getattr(turn, "events", ()) or ():
        if getattr(_ev, "event_type", "") == "CORE_RESPAWNED":
            _core_respawn_suppress.update({
                "target": (
                    int(config.get("core_target_x", 0)),
                    int(config.get("core_target_y", 0)),
                ),
                "until": turn_context.tick + _CORE_RESPAWN_TARGET_SUPPRESS_TICKS,
            })
            _core_danger_until = 0
            break
    if _core_respawn_suppress:
        _cur_target = (
            int(config.get("core_target_x", 0)),
            int(config.get("core_target_y", 0)),
        )
        if (
            _cur_target != _core_respawn_suppress.get("target")
            or turn_context.tick >= int(_core_respawn_suppress.get("until", 0))
        ):
            _core_respawn_suppress.clear()
        elif config.get("core_target_enabled", False):
            config = dict(config)
            config["core_target_enabled"] = False

    if beacon_on_ground_here:
        core.pickup_beacon()
        core_action_name = "PICKUP_BEACON"
        core_done = True

    if not core_done:
        demand_spawn = _plan_demand_spawn(turn, core, resources, config)
        if demand_spawn is not None:
            core_action_name = demand_spawn
            core_done = True

    # Recover Core HP before repairing shield — HP is permanent damage.
    if not core_done and _core_should_heal(core, resources, config):
        core.heal()
        core_action_name = "HEAL_CORE"
        core_done = True

    if not core_done and config["repair_enabled"] and resources >= 1:
        effective_cap = 10 if beacon_carried_by_core else 5
        shield_target = (
            int(config["combat_shield_target"])
            if enemies
            else int(config["peace_shield_target"])
        )
        # Under threat near the Core, top the shield up to its cap instead of
        # the low combat target: C1 died with 2 spare resources while its
        # shield sat at the combat target of 3 (t649639-t649645).
        if _core_in_danger:
            shield_target = max(shield_target, effective_cap)
        want_repair = core.shield < shield_target and core.shield < effective_cap
        if want_repair:
            core.repair_shield()
            core_action_name = "REPAIR_SHIELD"
            core_done = True

    # ── Core movement ────────────────────────────────────────────────────
    if not core_done and config["core_movement_enabled"]:
        # A Core migration spans several Ticks (state stays MOVING until the
        # step lands). Re-issuing START_MOVE mid-step is rejected as
        # CORE_ALREADY_MOVING and used to flood the battle log with one warn
        # row per Tick — let the step finish, then re-pick the heading.
        core_mid_move = (
            getattr(getattr(core, "view", None), "state", None) == CoreState.MOVING
        )
        global _core_pending_move, _core_move_hold_until_tick
        global _core_anchor, _map_dirty
        # Learn from last Tick's core move rejections. A terrain-blocked
        # destination is a permanent wall the vision snapshot never reported,
        # so record it in obstacle memory; otherwise the heading heuristic
        # deterministically re-picks the same blocked step every Tick
        # (observed 4900+ identical CORE_DESTINATION_TERRAIN_BLOCKED rows).
        core_blocked_now: set[tuple[int, int]] = set()
        for _ev in getattr(turn, "events", ()) or ():
            if getattr(_ev, "actor_id", None) != core.id:
                continue
            if getattr(_ev, "event_type", "") not in (
                "CORE_MOVE_FAILED",
                "CORE_MOVE_START_FAILED",
                "CORE_ACTION_FAILED",
            ):
                continue
            _reason = getattr(_ev, "reason_code", None) or ""
            if _reason != "CORE_DESTINATION_TERRAIN_BLOCKED":
                if _reason == "CORE_ALREADY_MOVING":
                    # Usually a manual/auto command race: yield the movement
                    # slot briefly instead of re-issuing the rejected move.
                    _core_move_hold_until_tick = turn_context.tick + 2
                elif _reason == "CELL_UNIT_LIMIT":
                    # The destination is packed with our OWN units, not walled,
                    # so there is nothing to learn into the map — but hammering
                    # it is far from free: every attempt puts the Core back into
                    # MOVING, and a MOVING Core rejects every worker DEPOSIT.
                    # Observed 11 hours of a frozen vault (26/185) while C1
                    # re-issued one step into (666,-317) every few Ticks and the
                    # economy logged "W16 卸货失败：核心迁移中" over and over.
                    # Hold off and let the ring clear instead.
                    _core_move_hold_until_tick = (
                        turn_context.tick + _CORE_MOVE_HOLD_TICKS
                    )
                continue
            _dest: tuple[int, int] | None = None
            _pos_attr = getattr(_ev, "position", None)
            if _pos_attr is not None:
                _dest = (
                    tuple(_pos_attr) if not isinstance(_pos_attr, tuple) else _pos_attr
                )
            _pending = _core_pending_move
            if _dest is None or _manhattan(_dest, core_pos) != 1:
                # The event cell is not the attempted destination (it may be
                # the core's own cell); fall back to the remembered heading.
                if _pending is not None and _pending[2] == turn_context.tick - 1:
                    _dest = (
                        _pending[0][0] + _pending[1].delta[0],
                        _pending[0][1] + _pending[1].delta[1],
                    )
                else:
                    _dest = None
            if (
                _dest is not None
                and _manhattan(_dest, core_pos) == 1
                and _dest not in _obstacle_memory
                and _dest not in _resource_memory
            ):
                _obstacle_memory.add(_dest)
                _bump_obstacle_generation()
                _obstacle_pending_adds.add(_dest)
                _map_dirty = True
                core_blocked_now.add(_dest)
        # Stop if a cargo worker is close (any carrying worker heads home to
        # deposit), otherwise move toward them
        close_cargo = any(
            w.cargo > 0
            and _manhattan(w.position, core_pos) <= int(config["cargo_wait_distance"])
            for w in turn.workers
        )
        # Friendly units per cell, for the destination pre-check below. The
        # server stacks at most _CELL_UNIT_LIMIT units on a cell, and the Core
        # used to pick headings from the obstacle/dead-end view only — so a
        # destination packed with our own workers was re-issued every Tick,
        # rejected as CELL_UNIT_LIMIT, and the resulting MOVING churn starved the
        # unloading chute.
        core_cell_counts: dict[tuple[int, int], int] = {}
        for _core_count_list in (
            turn.workers,
            getattr(turn, "vanguards", ()) or (),
            getattr(turn, "rangers", ()) or (),
        ):
            for _u in _core_count_list:
                _p = tuple(_u.position)
                core_cell_counts[_p] = core_cell_counts.get(_p, 0) + 1
        if (
            not close_cargo
            and not core_mid_move
            and not _core_in_danger
            and turn_context.tick >= _core_move_hold_until_tick
        ):
            # Drift leash: the automatic heuristic chases the worker/resource
            # mass center, and far-flung explorers keep pulling it outward
            # while the mining radius (worker_mine_max_distance) follows the
            # Core — the whole base migrates forever (observed 2800+ cells).
            # Clamp every automatic step to a persisted anchor instead. 0
            # disables the leash; a manual core target bypasses it.
            max_drift = max(0, int(config.get("core_max_drift", 200)))
            if _core_anchor is None:
                _core_anchor = tuple(core_pos)
                _map_dirty = True
            elif max_drift > 0 and _manhattan(tuple(core_pos), _core_anchor) > max_drift:
                # Only a respawn/teleport can land outside the leash, since
                # normal steps are clamped below — re-anchor there.
                _core_anchor = tuple(core_pos)
                _map_dirty = True
            # Determine movement target
            core_target_enabled = config.get("core_target_enabled", False)
            if core_target_enabled:
                # Fixed coordinate target (overrides resource/worker heuristics)
                target = (
                    int(config.get("core_target_x", 0)),
                    int(config.get("core_target_y", 0)),
                )
            else:
                # Move toward workers + resources center of mass
                # Compute average worker position
                wx = [w.position[0] for w in turn.workers]
                wy = [w.position[1] for w in turn.workers]
                if wx and wy:
                    avg_x = sum(wx) // len(wx)
                    avg_y = sum(wy) // len(wy)
                else:
                    avg_x, avg_y = core_pos
                # Also consider nearest resource (visible or remembered).
                # Never target the Core's own cell or an adjacent mine: the
                # Core would sit on it and leave it unminable (empty workers
                # vacate the chute before HARVEST). Prefer the next resource.
                res_target = None
                if all_resources:
                    res_candidates = [
                        r for r in all_resources
                        if _manhattan(core_pos, r) > 1
                    ]
                    if res_candidates:
                        res_target = min(
                            res_candidates, key=lambda p: _manhattan(core_pos, p)
                        )
                # Choose target: nearest resource or worker center, whichever is closer
                if (
                    config["prefer_resources_for_core"]
                    and res_target
                    and _manhattan(core_pos, res_target) < _manhattan(core_pos, (avg_x, avg_y))
                ):
                    target = res_target
                else:
                    target = (avg_x, avg_y)
            # Determine best direction
            dx = target[0] - core_pos[0]
            dy = target[1] - core_pos[1]
            dirs = []
            if abs(dx) >= abs(dy):
                if dx > 0: dirs.append(Direction.RIGHT)
                elif dx < 0: dirs.append(Direction.LEFT)
                if dy > 0: dirs.append(Direction.DOWN)
                elif dy < 0: dirs.append(Direction.UP)
            else:
                if dy > 0: dirs.append(Direction.DOWN)
                elif dy < 0: dirs.append(Direction.UP)
                if dx > 0: dirs.append(Direction.RIGHT)
                elif dx < 0: dirs.append(Direction.LEFT)
            # Axis-degenerate fallback: when the target shares the Core's row or
            # column, the builder above yields a single heading. If that cell is
            # walled the candidate list empties and the Core waits forever
            # (observed: target (362,-239) vs core (-292,-239) with the east
            # cell in obstacle memory — silent stall, no warn rows at all).
            # Offer the perpendicular axis as sidestep candidates; the obstacle/
            # leash/dead-end checks below still filter each one.
            if dx != 0 and dy == 0:
                dirs.extend((Direction.DOWN, Direction.UP))
            elif dy != 0 and dx == 0:
                dirs.extend((Direction.RIGHT, Direction.LEFT))
            # Manual-target mode with the primary heading walled: the
            # perpendicular fallback candidates only jitter the Core vertically
            # when the wall spans them too (observed: core bouncing between
            # (-225,-238)/(-225,-239) with zero eastward progress against a
            # two-cell wall band). Route with a real A* toward the target —
            # budget scaled to distance, path cached per goal and recomputed
            # when the Core leaves the cached path or a new wall blocks it.
            # Auto mode keeps the greedy heading (it is leash-constrained).
            #
            # Once a detour is committed it also drives every following Tick, not
            # just the Tick its wall was found: the greedy heading is free to
            # step straight back onto the cell the detour just left, and the two
            # rules then erase each other (observed: C1 bouncing (668,-236)/
            # (668,-237) with net 0 over 81 Ticks — (668,-235) is walled, so
            # every Tick from -236 the A* said "back to -237 and around" while
            # every Tick from -237 the greedy heading said "down to -236").
            _core_on_cached_route = (
                core_target_enabled
                and _core_target_path_cache.get("goal") == tuple(target)
                and tuple(core_pos) in (_core_target_path_cache.get("path") or ())
            )
            _core_astep = None
            _core_hold = False
            if (
                core_target_enabled
                and dirs
                and (
                    _core_on_cached_route
                    or (core_pos[0] + dirs[0].delta[0], core_pos[1] + dirs[0].delta[1])
                    in obstacle_cells
                    or _is_dead_end_step(
                        (core_pos[0] + dirs[0].delta[0], core_pos[1] + dirs[0].delta[1]),
                        obstacle_cells,
                        allow=(target,),
                    )
                )
            ):
                cpos = tuple(core_pos)
                nxt = None
                _cache = _core_target_path_cache
                _goal = tuple(target)
                if _cache.get("goal") == _goal:
                    cpath = _cache.get("path") or []
                    if cpos in cpath:
                        cidx = cpath.index(cpos)
                        if (
                            0 <= cidx < len(cpath) - 1
                            and cpath[cidx + 1] not in obstacle_cells
                        ):
                            ncell = cpath[cidx + 1]
                            if core_cell_counts.get(ncell, 0) < _CELL_UNIT_LIMIT:
                                nxt = ncell
                            else:
                                # A friendly unit is squatting on the route's next
                                # cell. The cached path is still valid (occupancy is
                                # transient, not a wall), so hold without re-searching
                                # — a fresh A* over the same walls returns the same
                                # blocked step and used to burn the budget every Tick.
                                _core_hold = True
                if nxt is None and not _core_hold:
                    _fail_active = (
                        _cache.get("fail_goal") == _goal
                        and turn_context.tick < _cache.get("fail_until", 0)
                    )
                    if not _fail_active:
                        budget = min(
                            1_500_000,
                            max(50_000, _manhattan(cpos, _goal) * 60),
                        )
                        cpath = _bfs_path(
                            cpos, _goal, obstacle_cells, max_steps=budget
                        )
                        if cpath and len(cpath) > 1:
                            _cache.update({
                                "goal": _goal, "path": cpath,
                                "fail_goal": None, "fail_until": 0,
                                "fail_warned": False,
                            })
                            ncell = cpath[1]
                            if core_cell_counts.get(ncell, 0) < _CELL_UNIT_LIMIT:
                                nxt = ncell
                            else:
                                _core_hold = True
                        else:
                            # Unreachable within budget: remember the failure so
                            # the next Ticks skip the search instead of repeating
                            # it. Warn once per episode so an unreachable manual
                            # target is visible rather than silently burning CPU.
                            _cache.update({
                                "goal": _goal, "path": [],
                                "fail_goal": _goal,
                                "fail_until": turn_context.tick + _CORE_PATH_FAIL_BACKOFF,
                            })
                            if not _cache.get("fail_warned"):
                                _cache["fail_warned"] = True
                                try:
                                    append_jsonl(BATTLE_LOG_PATH, [{
                                        "tick": turn_context.tick,
                                        "ts": time.time(),
                                        "cat": "warn",
                                        "msg": (
                                            f"核心目标 {_fmt_cell(_goal)} 在寻路预算内不可达，"
                                            f"暂停重试 {_CORE_PATH_FAIL_BACKOFF} tick"
                                        ),
                                    }])
                                except OSError:
                                    pass
                if nxt is not None:
                    _core_astep = (nxt[0] - core_pos[0], nxt[1] - core_pos[1])
            if _core_astep is not None:
                for d in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT):
                    if d.delta == _core_astep:
                        core.start_move(d)
                        _core_pending_move = (tuple(core_pos), d, turn_context.tick)
                        core_action_name = f"MOVE_{d.name}"
                        core_done = True
                        break
            # Try each direction (obstacle + dead-end aware). Skipped when the
            # A* detour already issued this Tick's core move, or when the core is
            # holding for a friendly squatter on the cached route's next cell.
            for d in (dirs if _core_astep is None and not _core_hold else []):
                nx, ny = core_pos[0] + d.delta[0], core_pos[1] + d.delta[1]
                if (nx, ny) in obstacle_cells or (nx, ny) in core_blocked_now:
                    continue
                if core_cell_counts.get((nx, ny), 0) >= _CELL_UNIT_LIMIT:
                    continue
                if (
                    max_drift > 0
                    and not core_target_enabled
                    and _manhattan((nx, ny), _core_anchor) > max_drift
                ):
                    continue
                if _is_dead_end_step((nx, ny), obstacle_cells, allow=(target,)):
                    continue
                core.start_move(d)
                _core_pending_move = (tuple(core_pos), d, turn_context.tick)
                core_action_name = f"MOVE_{d.name}"
                core_done = True
                break

    if not core_done:
        core.wait()
        core_action_name = "WAIT"
    _phase.stop()  # core_action

    # Occupied cells this tick (every friendly unit + visible enemies). Workers
    # use this to avoid hammering blocked cells — the main cause of the core-ring
    # deadlock where full workers wedge in place and the economy stalls. The core
    # cell itself is deliberately NOT included: a worker may step onto it to
    # unload, so it only counts as occupied when another unit is standing there.
    _phase.start("unit_setup")
    occupied: frozenset[tuple[int, int]] = frozenset(
        {tuple(w.position) for w in turn.workers}
        | {tuple(v.position) for v in getattr(turn, "vanguards", ()) or ()}
        | {tuple(r.position) for r in getattr(turn, "rangers", ()) or ()}
        | {tuple(e.position) for e in enemies}
    )
    # Per-cell friendly unit multiplicity this tick: the server stacks up to
    # _CELL_UNIT_LIMIT units on one cell, so yield/retreat/chute-clear
    # decisions judge a cell by count, not mere presence. Enemy cells are kept
    # separately — they stay hard-blocked (never squeeze onto an enemy).
    friendly_cell_counts: dict[tuple[int, int], int] = {}
    for _u_list in (
        turn.workers,
        getattr(turn, "vanguards", ()) or (),
        getattr(turn, "rangers", ()) or (),
    ):
        for _u in _u_list:
            _p = tuple(_u.position)
            friendly_cell_counts[_p] = friendly_cell_counts.get(_p, 0) + 1
    enemy_cells: frozenset[tuple[int, int]] = frozenset(
        tuple(e.position) for e in enemies
    )

    # CELL_UNIT_LIMIT retry breaker bookkeeping: count consecutive Ticks each
    # unit was rejected for moving onto a packed cell. The dict is rebuilt
    # from scratch every Tick (only repeat offenders survive), so it stays
    # bounded by the unit count and never carries stale entries.
    global _cell_limit_streak, _healing_units_prev
    global _heal_return_inflight, _worker_contest_streak
    global _worker_contest_blocked, _worker_contest_repeat
    _failed_again: set[str] = set()
    _failed_cell: dict[str, tuple[int, int]] = {}
    for _ev in getattr(turn, "events", ()) or ():
        if (
            getattr(_ev, "event_type", "") == "UNIT_MOVE_FAILED"
            and getattr(_ev, "reason_code", None) == "CELL_UNIT_LIMIT"
            and getattr(_ev, "actor_id", None) is not None
        ):
            _failed_again.add(str(_ev.actor_id))
            if getattr(_ev, "position", None) is not None:
                _failed_cell[str(_ev.actor_id)] = tuple(_ev.position)
    _cell_limit_streak = {
        _key: _cell_limit_streak.get(_key, 0) + 1 for _key in _failed_again
    }

    # MOVE_CONTESTED repeat bookkeeping (workers and the attack team): two
    # workers whose deterministic plans submit the same cell every Tick cancel
    # both moves every Tick. A worker rejected twice in a row from the same cell
    # toward the same destination backs off for one Tick (worker branch below)
    # so the peer's move can land. Same rebuild-from-scratch semantics as the
    # streak above. Combat units are tracked here too because an attack march can
    # be refused by a claimant it cannot see: production R5 re-issued the same
    # MOVE:RIGHT out of (-1182,-2391) for 152 straight Ticks, net displacement 0,
    # and the rejection only ever names the unit's own cell. Only the teams in
    # _CONTEST_PARK_TEAMS read the parked cell back, so kite/guerrilla keep their
    # own breakers untouched.
    _worker_pos_by_id = {
        str(u.id): tuple(u.position)
        for u in (*turn.workers, *turn.vanguards, *turn.rangers)
    }
    _contested_now: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {}
    for _ev in getattr(turn, "events", ()) or ():
        if (
            getattr(_ev, "event_type", "") == "UNIT_MOVE_FAILED"
            and getattr(_ev, "reason_code", None) == "MOVE_CONTESTED"
            and getattr(_ev, "position", None) is not None
        ):
            _key = str(getattr(_ev, "actor_id", None))
            _wpos = _worker_pos_by_id.get(_key)
            if _wpos is None:
                continue  # unknown actor (enemy or already dead)
            _contested_now[_key] = (_wpos, tuple(_ev.position))
    _worker_contest_streak = {
        _key: (
            _cell,
            _dest,
            (_worker_contest_streak[_key][2] + 1)
            if _worker_contest_streak.get(_key, ("", ""))[:2] == (_cell, _dest)
            else 1,
        )
        for _key, (_cell, _dest) in _contested_now.items()
    }
    # Backing off one Tick only helps when the claimant is a peer that also
    # moves. Count rejections PER ORIGIN and keep the tally across the Tick the
    # worker spends waiting (a waiting worker emits no rejection at all, so a
    # from-scratch rebuild would bounce between 1 and 2 forever and never reach
    # the block threshold). Drop it as soon as the worker stands somewhere else:
    # the head-on is behind it.
    _repeat_now: dict[str, tuple[tuple[int, int], int]] = {}
    for _key, (_cell, _dest, _count) in _worker_contest_streak.items():
        _prev = _worker_contest_repeat.get(_key)
        _repeat_now[_key] = (
            _cell,
            _prev[1] + 1 if _prev is not None and _prev[0] == _cell else 1,
        )
    _worker_contest_repeat = {
        **{
            _k: _v for _k, _v in _worker_contest_repeat.items()
            if _k not in _repeat_now
            and _worker_pos_by_id.get(_k) == _v[0]
        },
        **_repeat_now,
    }
    # Once the same origin keeps losing the same fight, park the destinations it
    # keeps aiming at so the march routes around the claimants instead of
    # re-issuing the refused step forever. The rejection event names only the
    # worker's own cell, so the destination comes from what we planned last Tick.
    _blocked_now: dict[str, tuple[frozenset[tuple[int, int]], int]] = {}
    _core_cell = tuple(core_pos)
    for _key, (_cell, _repeat) in _worker_contest_repeat.items():
        if _repeat < _WORKER_CONTEST_BLOCK_AFTER:
            continue
        _planned = _worker_planned_step.get(_key)
        # The newest refused destination (None if the unit moved since that plan,
        # or if it aimed at the chute — parking the Core cell starves the vault).
        _new_dest = (
            _planned[1]
            if _planned is not None
            and _planned[0] == _cell
            and _planned[1] != _core_cell
            else None
        )
        _live = _worker_contest_blocked.get(_key)
        if _live is not None and _live[1] > turn_context.tick:
            # Sticky while the carrier stands its ground: keep every parked cell
            # and ADD the newly refused one — but only when a MOVE_CONTESTED
            # actually fired this Tick. The per-origin tally persists across
            # refusal-free Ticks, so adding the latest plan unconditionally would
            # park cells the unit successfully stepped onto. A set grows only
            # with genuinely refused neighbours, so a unit boxed in on several
            # sides learns them all instead of re-issuing the one non-parked
            # direction forever (V27: 3456 MOVE_CONTESTED on the alternative).
            _cells = _live[0]
            if _new_dest is not None and _key in _contested_now:
                _cells = _cells | {_new_dest}
            _blocked_now[_key] = (
                _cells, turn_context.tick + _WORKER_CONTEST_BLOCK_TICKS,
            )
            continue
        if _new_dest is None:
            continue
        _blocked_now[_key] = (
            frozenset({_new_dest}),
            turn_context.tick + _WORKER_CONTEST_BLOCK_TICKS,
        )
    _worker_contest_blocked = {
        **{
            _k: _v for _k, _v in _worker_contest_blocked.items()
            if _v[1] > turn_context.tick and _k not in _blocked_now
        },
        **_blocked_now,
    }

    # Serialize core deliveries: exactly one carrier may contest the chute
    # this tick; the rest queue at distance 2 (see _assign_delivery_lease).
    delivery_lease = _assign_delivery_lease(turn.workers, core_pos)

    # Is the lease holder already adjacent to the chute? Then the next-in-line
    # must yield the entry slot instead of racing for the same cell.
    global _lease_holder_adjacent
    _lease_holder_adjacent = any(
        str(w.id) == delivery_lease and _manhattan(tuple(w.position), core_pos) <= 1
        for w in turn.workers
    ) if delivery_lease else False

    # ── Unit actions ────────────────────────────────────────────────────
    # Healing budget: leftover resources after reserve + this Tick's spawn cost,
    # so healing never starves production. Unit heals resolve before the Core
    # action (spawn), so the spawn's cost is reserved first.
    core_moving = (
        getattr(getattr(core, "view", None), "state", None) == CoreState.MOVING
    )
    heal_enabled = bool(config.get("heal_enabled", True))
    heal_budget = resources - int(config.get("resource_reserve", 0))
    if core_action_name.startswith("SPAWN_"):
        spawn_type = core_action_name.split("_", 1)[1]
        population = getattr(getattr(turn, "state", None), "population", 0) or 0
        try:
            spawn_cost = _spawn_cost(UnitType(spawn_type), population)
        except ValueError:
            spawn_cost = UNIT_SPAWN_COSTS.get(spawn_type, 0)
        heal_budget -= spawn_cost

    # 错峰回撤: cap how many home-squad units may start marching back to the
    # Core this Tick, so the defense line peels off one by one instead of
    # emptying at once. Limited slots go to the most-damaged units first; ties
    # go to whoever is already closer to the Core, so an in-flight return keeps
    # re-claiming its slot and never stalls behind a fresh casualty. When the
    # limit is 0, every eligible unit returns (no stagger).
    heal_return_limit = int(config.get("combat_heal_return_limit", 1))
    heal_return_allowed: set[str] | None = None  # None = unlimited
    # Delivery pressure: the core cell doubles as the healing spot, and a
    # healer squatting on it shuts the unloading chute. Only carriers already
    # queued at the chute (Manhattan distance <= _HEAL_GATE_QUEUE_RADIUS)
    # physically compete for the core cell this tick — counting every carrier
    # on the map kept the gate shut whenever the economy was healthy and
    # starved healing entirely. When a real queue has formed, stop NEW
    # heal-return marches; healers already on the core cell step aside below
    # before the unit loop.
    _carrying_workers = sum(
        1 for w in turn.workers if int(getattr(w, "cargo", 0) or 0) > 0
    )
    _queue_carriers = sum(
        1 for w in turn.workers
        if int(getattr(w, "cargo", 0) or 0) > 0
        and _manhattan(tuple(w.position), core_pos) <= _HEAL_GATE_QUEUE_RADIUS
    )
    heal_return_gate = _queue_carriers >= 2

    # Delivery line active: empty workers route around the chute (see
    # _chute_in_demand). Any hauling carrier anywhere keeps detours on, even
    # when it is still far from the chute.
    global _chute_in_demand
    _chute_in_demand = _carrying_workers > 0

    # A carrier already adjacent to the chute: healers on the core cell must
    # yield it even when the queue behind is still thin.
    global _carrier_at_chute
    _carrier_at_chute = any(
        int(getattr(w, "cargo", 0) or 0) > 0
        and _manhattan(tuple(w.position), core_pos) == 1
        for w in turn.workers
    )

    # A healer commanded HEAL last Tick that is still damaged keeps the core
    # cell while its heal can proceed this Tick (HEAL outranks yield-chute;
    # the carrier deposits the tick after the heal finishes).
    def _healer_holds_core(u: Any, u_name: str) -> bool:
        return (
            u_name in _healing_units_prev
            and heal_enabled
            and not core_moving
            and heal_budget >= 1
            and int(getattr(u, "hp", 0) or 0) < _unit_max_hp(u)
        )

    # Chute pipeline flag: will the unit standing on the core cell leave it
    # this tick? A cargo worker deposits and stays; an empty worker vacates
    # (vacate-core) and a healer yields when a carrier queue has formed
    # (yield-chute). When True, the next carrier may chain INTO the still
    # occupied cell in this very tick ("one out, one in").
    global _chute_vacating_this_tick
    _chute_vacating_this_tick = False
    for _occ in getattr(turn, "units", ()) or ():
        if tuple(_occ.position) != core_pos:
            continue
        _occ_carrying = (
            _occ.unit_type == UnitType.WORKER
            and int(getattr(_occ, "cargo", 0) or 0) > 0
        )
        if _occ_carrying:
            break  # deposits this tick, stays on the chute
        if _occ.unit_type == UnitType.WORKER:
            _chute_vacating_this_tick = True  # just unloaded -> vacate-core
        else:
            # yield-chute fires for a formed queue or an adjacent carrier,
            # unless the healer is mid-heal and holds the cell one more tick
            _occ_name = _object_name(
                _occ.id, _UNIT_NAME_PREFIX.get(_occ.unit_type, "U")
            )
            _chute_vacating_this_tick = (
                (heal_return_gate or _carrier_at_chute)
                and not _healer_holds_core(_occ, _occ_name)
            )
        break
    # Heal-spot capacity control: while a healer still sits on the core cell —
    # mid-heal (commanded HEAL last Tick and still damaged) or a heal-return
    # marcher that already arrived but has not healed and left yet — admit no
    # new casualty. Narrow-mouth bases have only one or two ring exits; a
    # second returnee stacking up behind a blocked heal spot deadlocked the
    # chute (observed 95+ ticks). The spot frees the tick the healer finishes
    # (full HP → yield-chute/forced evacuation below moves it along).
    heal_spot_busy = False
    for _occ in getattr(turn, "units", ()) or ():
        if tuple(_occ.position) != core_pos:
            continue
        if _occ.unit_type == UnitType.WORKER:
            continue
        _occ_name = _object_name(
            _occ.id, _UNIT_NAME_PREFIX.get(_occ.unit_type, "U")
        )
        if _occ_name in _heal_return_inflight:
            heal_spot_busy = True
            break
        if (
            _occ_name in _healing_units_prev
            and int(getattr(_occ, "hp", 0) or 0) < _unit_max_hp(_occ)
        ):
            heal_spot_busy = True
            break
    if heal_return_gate or heal_spot_busy:
        heal_return_allowed = set()
    elif heal_return_limit > 0:
        eligible: list[tuple[int, int, str]] = []
        for unit in turn.units:
            if unit.unit_type not in (UnitType.VANGUARD, UnitType.RANGER):
                continue
            unit_name = _object_name(
                unit.id, _UNIT_NAME_PREFIX.get(unit.unit_type, "U")
            )
            if not _unit_should_return_to_heal(
                unit,
                config,
                core_pos=core_pos,
                core_moving=core_moving,
                team=_combat_team_for(unit_name, config),
            ):
                continue
            eligible.append((
                int(getattr(unit, "hp", 0) or 0),
                _manhattan(tuple(unit.position), core_pos),
                unit_name,
            ))
        eligible.sort()
        # In-flight returns keep their slot: a unit already marching home
        # (commanded last Tick) re-claims priority first, so the stagger limit
        # never revokes its march tick by tick in favour of a fresh casualty.
        heal_return_allowed = {
            name for _, _, name in eligible if name in _heal_return_inflight
        }
        for _entry in eligible:
            if len(heal_return_allowed) >= heal_return_limit:
                break
            heal_return_allowed.add(_entry[2])
    # heal_return_limit == 0 (no stagger) keeps heal_return_allowed = None.
    _phase.stop()  # unit_setup

    # Per-unit planner wall-clock, broken down by unit type so the summary can
    # attribute a slow tick to "workers" vs "vanguards" vs "rangers". Each is the
    # sum over every unit of that type this Tick.
    unit_phase_ms: dict[str, float] = {"worker": 0.0, "vanguard": 0.0, "ranger": 0.0,
                                       "other": 0.0}
    commanded_self_destructs: set[str] = set()
    healed_names: set[str] = set()
    heal_return_names: set[str] = set()
    for unit in turn.units:
        uid = str(unit.id)[:8]
        name = _object_name(unit.id, _UNIT_NAME_PREFIX.get(unit.unit_type, "U"))
        # Dashboard 自裁 command: remove the unit before any other action this
        # Tick (Worker cargo drops on its final cell).
        if name in self_destructs:
            unit.self_destruct()
            unit_actions_detail[uid] = "SELF_DESTRUCT:manual"
            commanded_self_destructs.add(name)
            continue
        # Dashboard 驻守 command: pin the unit in place. It never moves and
        # auto-attacks enemies entering its range (with kite-style trajectory
        # prediction). Hold outranks every program branch below except 自裁,
        # so the operator can park a unit exactly where it stands.
        if name in holds:
            action, detail = _plan_hold(
                unit,
                enemies,
                obstacle_cells,
                config,
            )
            unit_actions_detail[uid] = f"{action}:{detail}[hold]"
            continue
        # Delivery priority: a healer (or any idle unit) squatting on the core
        # cell blocks the unloading chute. While cargo workers are queued up,
        # step off for one tick so the lease holder can deposit; only a loaded
        # worker stays (it deposits this very tick). Resume healing next tick
        # if the queue has drained.
        if (
            tuple(unit.position) == core_pos
            and (heal_return_gate or _carrier_at_chute)
            and not (
                unit.unit_type == UnitType.WORKER
                and int(getattr(unit, "cargo", 0) or 0) > 0
            )
            and not _healer_holds_core(unit, name)
        ):
            step_aside = _retreat_from(
                tuple(unit.position), core_pos, obstacle_cells, enemy_cells,
                friendly_cell_counts,
            )
            if step_aside is not None:
                unit.move(step_aside)
                unit_actions_detail[uid] = f"MOVE:{step_aside.name} yield-chute"
                continue
            # Forced evacuation fallback: the capacity-aware retreat found no
            # exit only when every non-wall neighbour is at the cell limit or
            # dead-end filtered — a squatter must still leave the chute, so
            # take ANY under-limit non-wall neighbour (dead ends included).
            # Without this the unit falls into home-patrol, re-targets the
            # same packed cell every tick and seals the chute permanently
            # (the narrow-mouth core deadlock).
            forced_aside = _retreat_from(
                tuple(unit.position), core_pos, obstacle_cells, enemy_cells,
                friendly_cell_counts, force=True,
            )
            if forced_aside is not None:
                unit.move(forced_aside)
                unit_actions_detail[uid] = (
                    f"MOVE:{forced_aside.name} yield-chute-forced"
                )
                continue
        # Post-combat healing: a damaged Unit on the Core cell with a stationary
        # Core spends its whole action recovering HP (1 resource / 1 HP).
        if _unit_needs_heal(
            unit,
            core_pos=core_pos,
            core_moving=core_moving,
            heal_budget=heal_budget,
            heal_enabled=heal_enabled,
        ):
            unit.heal()
            unit_actions_detail[uid] = "HEAL"
            healed_names.add(name)
            continue
        # 守家队主动回撤回血: a home-squad Vanguard/Ranger below the
        # combat_heal_hp_threshold marches back to the Core; the HEAL branch
        # above picks it up once it arrives. Attack / guerrilla squads never
        # give up field presence to heal.
        if (
            unit.unit_type in (UnitType.VANGUARD, UnitType.RANGER)
            and _unit_should_return_to_heal(
                unit,
                config,
                core_pos=core_pos,
                core_moving=core_moving,
                team=_combat_team_for(name, config),
            )
            and (heal_return_allowed is None or name in heal_return_allowed)
        ):
            moved = _move_towards(
                unit,
                tuple(unit.position),
                core_pos,
                obstacle_cells,
                detail_prefix="home-heal-return",
                cell_counts=friendly_cell_counts,
            )
            if moved is not None:
                unit_actions_detail[uid] = f"MOVE:{moved[1]}[heal-return]"
                heal_return_names.add(name)
                continue
        # CELL_UNIT_LIMIT retry breaker: rejected 3+ consecutive Ticks means
        # the planner deterministically re-issues the same blocked move. Force
        # one sidestep into any under-limit non-wall neighbour to break the
        # loop, and block that cell for this unit for a while so the planner
        # stops re-issuing the move the moment the sidestep is over.
        # Workers are exempt: the delivery lease already rotates stuck
        # carriers and a forced detour would derail the chute pipeline.
        if (
            unit.unit_type != UnitType.WORKER
            and _cell_limit_streak.get(str(unit.id), 0)
            >= _CELL_LIMIT_DETOUR_AFTER
        ):
            _dpos = tuple(unit.position)
            _rejected_cell = _failed_cell.get(str(unit.id))
            if _rejected_cell is not None:
                _prior = _cell_limit_blocked.get(str(unit.id))
                _trips = (
                    _prior[1] if _prior and _prior[0] == _rejected_cell else 0
                )
                _cell_limit_blocked[str(unit.id)] = (
                    _rejected_cell,
                    _trips + 1,
                    turn.tick + _CELL_LIMIT_BLOCK_TICKS * min(2 ** _trips, 8),
                )
            _detour_dir: Direction | None = None
            for _d in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT):
                _npos = (_dpos[0] + _d.delta[0], _dpos[1] + _d.delta[1])
                if _npos in obstacle_cells or _npos in enemy_cells:
                    continue
                if friendly_cell_counts.get(_npos, 0) >= _CELL_UNIT_LIMIT:
                    continue
                if _npos == _rejected_cell:
                    continue
                _detour_dir = _d
                break
            if _detour_dir is not None:
                unit.move(_detour_dir)
                unit_actions_detail[uid] = (
                    f"MOVE:{_detour_dir.name} cell-limit-detour"
                )
                continue
        # Manual per-unit waypoint queue: march through it in order (attack =
        # engage enemies on the way, rush = ignore everything), then resume the
        # normal planner once the queue is exhausted.
        wp = waypoints.get(name)
        if wp is not None:
            action, detail = _plan_waypoint(
                unit,
                name,
                wp,
                config=config,
                obstacle_cells=obstacle_cells,
                occupied=occupied,
                enemies=enemies,
                core_pos=core_pos,
                cell_counts=friendly_cell_counts,
            )
            unit_actions_detail[uid] = f"{action}:{detail}[waypoint]"
            continue
        if unit.unit_type == UnitType.WORKER:
            # MOVE_CONTESTED backoff: rejected twice in a row from the same
            # cell toward the same destination means a deterministic head-on
            # with a peer worker. Sit this Tick out so the peer's move lands
            # instead of cancelling both again; normal planning resumes next
            # Tick (the streak resets once no contested rejection recurs).
            # Keyed by the FULL unit id: the rejection events carry it, while
            # this loop's ``uid`` is the 8-character display id. Looking the
            # streak up by ``uid`` missed every single entry, which is why this
            # breaker had never once fired in production (grep: zero
            # contested-backoff lines in 44MB of tactic_play.log, while the test
            # double's id "worker-1" was short enough to survive [:8]).
            _full_uid = str(unit.id)
            _contest = _worker_contest_streak.get(_full_uid)
            _blocked = _worker_contest_blocked.get(_full_uid)
            _blocking = _blocked is not None and _blocked[1] > turn_context.tick
            # Once the destination is parked, plan around it instead of sitting
            # out another Tick: waiting cannot resolve a head-on with something
            # this worker cannot see or move.
            if not _blocking and _contest is not None and _contest[2] >= 2:
                unit.wait()
                unit_actions_detail[uid] = (
                    f"WAIT:contested-backoff {_contest[1]}"
                )
                continue
            _ut0 = time.monotonic()
            action, detail = _plan_worker(
                unit, core,
                resource_cells=resource_cells,
                obstacle_cells=obstacle_cells,
                depleted=depleted,
                config=config,
                occupied=occupied,
                enemies=enemies,
                lease_uid=delivery_lease,
                next_uid=_delivery_next_uid,
                cell_counts=friendly_cell_counts,
                contest_blocked=(_blocked[0] if _blocking else frozenset()),
            )
            unit_phase_ms["worker"] += time.monotonic() - _ut0
            unit_actions_detail[uid] = f"{action}:{detail}"
            if action == "MOVE":
                # Remember where the step was going: the server reports
                # MOVE_CONTESTED on the worker's own cell, so this is the only
                # record of which neighbour was refused.
                try:
                    _step_dir = Direction[detail.split(" ", 1)[0]]
                except (KeyError, IndexError, ValueError):
                    _step_dir = None
                if _step_dir is not None:
                    _origin = tuple(unit.position)
                    _worker_planned_step[_full_uid] = (
                        _origin,
                        (
                            _origin[0] + _step_dir.delta[0],
                            _origin[1] + _step_dir.delta[1],
                        ),
                    )
        elif unit.unit_type == UnitType.VANGUARD:
            team = _combat_team_for(name, config)
            _blocked_cell = _cell_limit_block_cell(str(unit.id), turn.tick)
            _contest_cells = (
                _attack_contest_cells(str(unit.id), turn.tick)
                if team in _CONTEST_PARK_TEAMS
                else frozenset()
            )
            # Thread the refusal cells through the transient channel instead
            # of copying the whole wall set: `obstacle_cells | _extra_block`
            # broke the persistent dead-end structure's identity cache, so every
            # unit in the congestion episode paid a from-scratch 4.2 s batch
            # (report #8). The overlay keeps the planner's obstacle argument a
            # complete blocked set (identical membership) without the copy.
            _extra_block = set(_contest_cells)
            if _blocked_cell is not None:
                _extra_block.add(_blocked_cell)
            _unit_obstacles = (
                _ObstacleOverlay(obstacle_cells, frozenset(_extra_block))
                if _extra_block
                else obstacle_cells
            )
            _ut0 = time.monotonic()
            action, detail = _plan_vanguard(
                unit,
                enemies,
                _unit_obstacles,
                config,
                core_pos=tuple(core_pos),
                team=team,
                cell_counts=friendly_cell_counts,
            )
            unit_phase_ms["vanguard"] += time.monotonic() - _ut0
            unit_actions_detail[uid] = (
                f"{action}:{detail}[{team}]{_sweep_fuse_note(unit)}"
                + _attack_contest_note(_contest_cells)
            )
            if action == "MOVE" and team in _CONTEST_PARK_TEAMS:
                _record_combat_planned_step(unit, detail)
        elif unit.unit_type == UnitType.RANGER:
            team = _combat_team_for(name, config)
            _blocked_cell = _cell_limit_block_cell(str(unit.id), turn.tick)
            _contest_cells = (
                _attack_contest_cells(str(unit.id), turn.tick)
                if team in _CONTEST_PARK_TEAMS
                else frozenset()
            )
            # Same transient-channel treatment as the Vanguard branch above.
            _extra_block = set(_contest_cells)
            if _blocked_cell is not None:
                _extra_block.add(_blocked_cell)
            _unit_obstacles = (
                _ObstacleOverlay(obstacle_cells, frozenset(_extra_block))
                if _extra_block
                else obstacle_cells
            )
            _ut0 = time.monotonic()
            action, detail = _plan_ranger(
                unit,
                enemies,
                _unit_obstacles,
                config,
                core_pos=tuple(core_pos),
                team=team,
                cell_counts=friendly_cell_counts,
            )
            unit_phase_ms["ranger"] += time.monotonic() - _ut0
            unit_actions_detail[uid] = (
                f"{action}:{detail}[{team}]{_sweep_fuse_note(unit)}"
                + _attack_contest_note(_contest_cells)
            )
            if action == "MOVE" and team in _CONTEST_PARK_TEAMS:
                _record_combat_planned_step(unit, detail)

    # Ack the dashboard's 自裁 commands we just issued; concurrent new commands
    # added while planning stay pending for the next Tick.
    if commanded_self_destructs:
        _remove_self_destructs(commanded_self_destructs)

    # Fold the per-unit-type wall-clock into the named phases so the structured
    # log shows unit_loop broken down by planner. Surfaced to record_tick via
    # turn_context (play() is the caller) so choose_actions keeps its signature.
    _phase.phases["unit:worker"] = unit_phase_ms["worker"]
    _phase.phases["unit:vanguard"] = unit_phase_ms["vanguard"]
    _phase.phases["unit:ranger"] = unit_phase_ms["ranger"]
    _publish_plan_profile(_phase)

    # Remember this Tick's heal decisions so next Tick can keep an in-flight
    # heal-return slot and let a mid-heal unit hold the core cell.
    _healing_units_prev = healed_names
    _heal_return_inflight = heal_return_names

    return core_action_name, unit_actions_detail


# ── live loop ────────────────────────────────────────────────────────────────

# Set on the desynced-session exit path: the forced full map save there can
# stall the process on a memory-tight VPS, delaying the container restart that
# is the only recovery.
_skip_exit_map_save = False


def play(api_key: str, log_path: str = DEFAULT_LOG_PATH) -> None:
    """Run forever with automatic reconnect on protocol/transport failures."""
    global _skip_exit_map_save, _pending_map_save_tick
    _skip_exit_map_save = False
    _pending_map_save_tick = None
    _load_map_memory()
    try:
        _prune_cold_map_memory()
    except Exception as exc:
        print(f"[map] cold prune failed: {exc}", flush=True)
    # Build the dead-end structure before connecting. Lazy-building it on the
    # first turn costs ~10s at 760k walls; the command window for that turn is
    # already gone by the time the plan finishes, which shows up as a
    # TICK_MISMATCH right after every container restart.
    _warm_t0 = time.monotonic()
    try:
        _ensure_dead_structure()
        print(
            f"[map] dead-end structure ready in "
            f"{(time.monotonic() - _warm_t0) * 1000:.0f}ms",
            flush=True,
        )
    except Exception as e:
        print(f"[map] dead-end warmup failed: {e}", flush=True)
    logger = TacticLogger(log_path)
    logger.open()
    print(f"[tactic] logging to {log_path}", flush=True)
    print(
        f"[map] obstacles={len(_obstacle_memory)} resources={len(_resource_memory)}"
        f" enemies={len(_enemy_memory)}",
        flush=True,
    )

    reconnect_delay = 1.0
    max_reconnect_delay = 30.0
    session = 0
    # Consecutive 409 TICK_MISMATCH rejections = desynced game session. A fresh
    # connection self-heals within a tick or two; a run well past that never
    # recovers in-process, so we exit for the entrypoint's container restart.
    stale_streak = 0
    max_stale_streak = 5

    try:
        while True:
            session += 1
            stale_streak = 0  # a new connection gets a fresh streak budget
            try:
                print(f"[tactic] connecting session={session}", flush=True)
                with ArenaHeroClient(api_key=api_key) as game:
                    for turn in game.turns():
                        reconnect_delay = 1.0  # healthy stream → reset backoff
                        tick_start = time.monotonic()
                        try:
                            core_action, unit_actions = choose_actions(turn)
                        except Exception as e:
                            _finalize_kite_collision_state(False)
                            print(
                                f"tick={getattr(turn, 'tick', '?')} plan_error={e}\n"
                                f"{traceback.format_exc()}",
                                flush=True,
                            )
                            continue
                        try:
                            _submit_t0 = time.monotonic()
                            accepted = turn.submit()
                            _submit_ms = (time.monotonic() - _submit_t0) * 1000
                        except APIError as e:
                            _finalize_kite_collision_state(False)
                            # 409 TICK_MISMATCH: the server rejects every tick of
                            # a desynced session. A fresh connection normally
                            # self-heals within a tick or two; an unbroken run well
                            # past that means the session is permanently desynced
                            # (the SDK's in-place reconnect never recovers — stuck
                            # for hours on the live server). Exit so the entrypoint
                            # restarts the container, the only proven recovery.
                            if e.status_code == 409 and e.error == "TICK_MISMATCH":
                                stale_streak += 1
                                print(
                                    f"tick={turn.tick} submit_error={e} "
                                    f"(streak={stale_streak}/{max_stale_streak})",
                                    flush=True,
                                )
                                if stale_streak >= max_stale_streak:
                                    print(
                                        f"[tactic] {stale_streak} consecutive TICK_MISMATCH; "
                                        "session desynced, exiting for container restart",
                                        flush=True,
                                    )
                                    # The map is already saved every few ticks; a
                                    # forced full save here serializes 760k walls
                                    # and on a small VPS blocks the process in
                                    # swap, so the entrypoint's restart waits
                                    # minutes. Losing the last few ticks of walls
                                    # is cheaper than a stuck container.
                                    _skip_exit_map_save = True
                                    raise SystemExit(3) from e
                                continue
                            stale_streak = 0
                            print(
                                f"tick={turn.tick} submit_error={e}\n"
                                f"{traceback.format_exc()}",
                                flush=True,
                            )
                            continue
                        except Exception as e:
                            _finalize_kite_collision_state(False)
                            stale_streak = 0
                            print(
                                f"tick={turn.tick} submit_error={e}\n"
                                f"{traceback.format_exc()}",
                                flush=True,
                            )
                            continue
                        finally:
                            # Measure the command path before the deferred disk
                            # write, then persist outside the submit window.
                            latency = (time.monotonic() - tick_start) * 1000
                            if not _skip_exit_map_save:
                                _flush_pending_map_memory(background=True)
                        stale_streak = 0
                        _finalize_kite_collision_state(accepted.accepted)
                        _commit_shadow_predictions(accepted.accepted)
                        _append_accepted_kite_battle_log(accepted.accepted)
                        plan_actions: dict[str, str] = {}
                        for uid, detail in unit_actions.items():
                            plan_actions[uid] = detail
                        phase_ms = dict(getattr(turn_context, "plan_phase_ms", {}) or {})
                        phase_ms["submit"] = round(_submit_ms, 1)
                        pf_calls = int(getattr(turn_context, "plan_pathfind_calls", 0))
                        pf_exp = int(getattr(turn_context, "plan_pathfind_expansions", 0))
                        pf_ms = float(getattr(turn_context, "plan_pathfind_ms", 0.0))
                        de_runs = int(getattr(turn_context, "plan_dead_end_runs", 0))
                        de_ms = float(getattr(turn_context, "plan_dead_end_ms", 0.0))
                        logger.record_tick(
                            turn,
                            core_action=core_action,
                            unit_actions=plan_actions,
                            accepted=accepted.accepted,
                            latency_ms=latency,
                            phase_ms=phase_ms,
                            pathfind_calls=pf_calls,
                            pathfind_expansions=pf_exp,
                            pathfind_ms=pf_ms,
                            dead_end_runs=de_runs,
                            dead_end_ms=de_ms,
                        )
                        _record_guerrilla_routes(turn, plan_actions)
                        print(
                            f"tick={accepted.tick} "
                            f"core={core_action} "
                            f"res={turn.resources}/{turn.resource_capacity} "
                            f"pop={turn.state.population} "
                            f"workers={len(turn.workers)} "
                            f"enemies={len(turn.visible_enemies)} "
                            f"resources_visible={len(turn.resource_cells)} "
                            f"memory={len(_resource_memory)} "
                            f"walls={len(_obstacle_memory)}",
                            flush=True,
                        )
                        # Slow-tick phase breakdown: only when planning crossed
                        # the threshold so healthy ticks stay one-liners. The
                        # dominant phase is surfaced first so a glance tells you
                        # whether the tick died on pathfinding, the unit loop, or
                        # map memory. Phases under 5% of total are folded to keep
                        # the line readable.
                        if latency >= _SLOW_PLAN_THRESHOLD_MS and phase_ms:
                            total = sum(phase_ms.values()) or latency
                            items = sorted(
                                phase_ms.items(), key=lambda kv: kv[1], reverse=True,
                            )
                            shown = [
                                f"{k}={v:.0f}ms" for k, v in items if v >= 0.05 * total
                            ]
                            print(
                                f"  [plan] tick={accepted.tick} latency={latency:.0f}ms "
                                f"pf(calls={pf_calls} exp={pf_exp} {pf_ms:.0f}ms) "
                                f"deadend(runs={de_runs} {de_ms:.0f}ms) "
                                + " ".join(shown),
                                flush=True,
                            )
            except KeyboardInterrupt:
                raise
            except (ProtocolError, TransportError, OSError, ConnectionError, TimeoutError) as e:
                print(
                    f"[tactic] stream error session={session}: {type(e).__name__}: {e}",
                    flush=True,
                )
            except Exception as e:
                # Catch-all so one unexpected crash doesn't kill the process.
                print(
                    f"[tactic] unexpected error session={session}: {type(e).__name__}: {e}",
                    flush=True,
                )
            _reap_map_save_child(block=True)
            _save_map_memory(force=True)
            print(
                f"[tactic] reconnecting in {reconnect_delay:.1f}s…",
                flush=True,
            )
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
    except KeyboardInterrupt:
        print("\n[tactic] stopped by user", flush=True)
    finally:
        if _skip_exit_map_save:
            print(
                "[tactic] stale session exit: skipping full map save so the "
                "container can restart",
                flush=True,
            )
        else:
            # The exit file must be the newest word: an outstanding child writes
            # the snapshot it forked, which is older than what is about to land.
            _reap_map_save_child(block=True)
            _save_map_memory(force=True)
        logger.close()
        _print_summary(log_path)


def _print_summary(log_path: str) -> None:
    """Quick summary from the tail of the log file.

    Reads only the newest _SUMMARY_TAIL_RECORDS ticks (the current segment after
    log rotation) instead of slurping the whole — possibly tens-of-MB — file.
    """
    try:
        # Import lazily so the running bot never loads dashboard code until
        # shutdown.
        from dashboard import _iter_log_lines_reverse

        ticks = []
        for line in _iter_log_lines_reverse(log_path):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and "tick" in record:
                ticks.append(record)
                if len(ticks) >= _SUMMARY_TAIL_RECORDS:
                    break
        if not ticks:
            return
        ticks.reverse()  # reader yields newest-first; summary wants oldest-first

        total_ticks = len(ticks)
        first_tick = ticks[0]["tick"]
        last_tick = ticks[-1]["tick"]
        total_harvests = sum(
            1 for t in ticks
            for e in t.get("events", [])
            if e.get("type") == "HARVEST_SUCCEEDED"
        )
        total_deposits = sum(
            1 for t in ticks
            for e in t.get("events", [])
            if e.get("type") == "DEPOSIT_SUCCEEDED"
        )
        total_move = sum(
            1 for t in ticks
            for uid, action in t.get("plan_unit_actions", {}).items()
            if action.startswith("MOVE")
        )
        total_harvest_actions = sum(
            1 for t in ticks
            for uid, action in t.get("plan_unit_actions", {}).items()
            if action.startswith("HARVEST")
        )

        print("\n" + "=" * 60)
        print("TACTIC SUMMARY")
        print("=" * 60)
        print(f"  Ticks played:     {total_ticks} ({first_tick} -> {last_tick})")
        print(f"  Harvest actions:  {total_harvest_actions}")
        print(f"  Harvest success:  {total_harvests}")
        print(f"  Deposit success:  {total_deposits}")
        print(f"  Move actions:     {total_move}")
        print(f"  Log file:         {log_path}")
        print("=" * 60)
        print()
    except Exception as exc:
        print(f"[summary] could not read {log_path}: {exc}", flush=True)


if __name__ == "__main__":
    api_key = os.environ.get("ARENA_HERO_API_KEY") or getpass("Arena Hero API key: ")
    play(api_key)
