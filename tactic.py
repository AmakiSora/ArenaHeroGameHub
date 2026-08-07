"""
Balanced Arena Hero tactic with play-by-play logging
=====================================================
Records every Tick's decisions into a JSONL log file for later analysis.
"""

from __future__ import annotations

import json
import math
import os
import time
import traceback
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from functools import wraps
from getpass import getpass
from heapq import heappop, heappush
from pathlib import Path
from typing import Any

from arena_hero import (
    ArenaHeroClient,
    CoreState,
    Direction,
    UnitType,
)
from arena_hero.errors import ProtocolError, TransportError
import game_stats
from state_io import append_jsonl, atomic_write_text, file_lock
from tactic_config import CONFIG_PATH, load_config, mutate_config

def _data_dir() -> Path:
    raw = os.environ.get("ARENA_DATA_DIR", "").strip()
    return Path(raw).resolve() if raw else Path.cwd()


MAP_MEMORY_PATH = _data_dir() / "map_memory.json"
DEFAULT_LOG_PATH = str(_data_dir() / "tactic_log.jsonl")
# Categorized battle log (discoveries / kills / defeats / config changes) that
# both the tactic process and the dashboard process append to.
BATTLE_LOG_PATH = _data_dir() / "battle_log.jsonl"
# Manual per-unit target coordinates set from the dashboard (display-name keyed).
WAYPOINTS_PATH = _data_dir() / "waypoints.json"

# Display-name prefix per unit type (W / V / R), shared with the dashboard.
_UNIT_NAME_PREFIX = {
    UnitType.WORKER: "W",
    UnitType.VANGUARD: "V",
    UnitType.RANGER: "R",
}
_DIRECTION_BY_NAME = {d.name: d for d in Direction}

# Rotate tactic_log.jsonl when it exceeds this size and keep at most N backups.
LOG_MAX_BYTES = int(os.environ.get("ARENA_LOG_MAX_MB", "20")) * 1024 * 1024
LOG_BACKUP_COUNT = 3
# Shutdown summary only reads this many of the newest tick records.
_SUMMARY_TAIL_RECORDS = 10000

# Enemy motion is sampled in shadow mode only. Three consecutive steps are
# required before a lead shot becomes eligible; two consecutive zero-steps
# establish that a target is stationary.
_ENEMY_MOTION_HISTORY = 4
_STABLE_MOVE_STREAK = 3
_STATIONARY_STREAK = 2

# Resource cost of each spawnable unit type (demand-based production).
UNIT_SPAWN_COSTS = {
    "WORKER": 5,
    "VANGUARD": 10,
    "RANGER": 12,
}
# Spawn priority when several types are below their target (economy first).
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


def _neighbor_cells(pos: tuple[int, int]) -> tuple[tuple[int, int], ...]:
    x, y = pos
    return tuple((x + dx, y + dy) for dx, dy in _CARDINAL_DELTAS)


def _open_degree(
    pos: tuple[int, int],
    blocked: frozenset[tuple[int, int]] | set[tuple[int, int]],
) -> int:
    """Count free cardinal neighbors of pos (cells not in blocked)."""
    return sum(1 for n in _neighbor_cells(pos) if n not in blocked)


def _dead_end_cells(
    obstacles: frozenset[tuple[int, int]] | set[tuple[int, int]],
) -> frozenset[tuple[int, int]]:
    """Return free cells that are obvious dead ends given known walls.

    Seed candidates are free cells adjacent to any known obstacle. A candidate
    is a dead end when it has at most one free neighbor. Marked dead ends are
    then treated as blocked so one-wide corridors collapse inward.
    """
    if not obstacles:
        return frozenset()
    obs = frozenset(obstacles)
    candidates: set[tuple[int, int]] = set()
    for cell in obs:
        for n in _neighbor_cells(cell):
            if n not in obs:
                candidates.add(n)

    dead: set[tuple[int, int]] = set()
    changed = True
    while changed:
        changed = False
        blocked = obs | dead
        for cell in candidates:
            if cell in dead:
                continue
            if _open_degree(cell, blocked) <= 1:
                dead.add(cell)
                changed = True
    return frozenset(dead)


def _get_dead_ends(
    obstacles: frozenset[tuple[int, int]] | set[tuple[int, int]],
) -> frozenset[tuple[int, int]]:
    """Cached dead-end set for the current known obstacle map."""
    global _dead_end_cache_key, _dead_end_cache
    # Callers always pass a frozenset (and share one object across a tick), so
    # use it directly as the cache key instead of rebuilding frozenset(obstacles)
    # — an O(n) copy on every _is_dead_end_step call. frozenset == short-circuits
    # on length, so a growing obstacle set is an O(1) miss during exploration.
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


def _path_blockers(
    obstacles: frozenset[tuple[int, int]],
    *,
    start: tuple[int, int] | None = None,
    goal: tuple[int, int] | None = None,
    avoid_dead_ends: bool = True,
) -> frozenset[tuple[int, int]]:
    """Obstacles plus dead ends, keeping corridors that contain start/goal."""
    if not avoid_dead_ends:
        return obstacles
    dead = _get_dead_ends(obstacles)
    if not dead:
        return obstacles
    allowed: set[tuple[int, int]] = set()
    if start is not None and start in dead:
        allowed |= _dead_component(start, dead)
    if goal is not None and goal in dead:
        allowed |= _dead_component(goal, dead)
    return obstacles | (dead - allowed)


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
) -> list[tuple[int, int]] | None:
    """Return a short grid path including start and goal, or None.

    Uses A* (Manhattan heuristic) so long routes across open maps need far
    fewer expansions than plain BFS. max_steps still caps node expansions.

    When avoid_dead_ends is True, 凸-shaped cul-de-sacs and one-wide corridors
    that only lead into them are treated as blocked, unless start or goal lies
    inside such a pocket (so units can still exit or reach a resource there).
    """
    if start == goal:
        return [start]
    blocked = _path_blockers(
        obstacles,
        start=start,
        goal=goal,
        avoid_dead_ends=avoid_dead_ends,
    )
    # Goal itself must remain enterable even if classified as a dead end.
    if goal in blocked and goal not in obstacles:
        blocked = blocked - {goal}

    # A*: f = g + h. tie-break on h so we bias toward the goal.
    # Heap entries: (f, h, counter, g, cell). counter breaks remaining ties.
    open_heap: list[tuple[int, int, int, int, tuple[int, int]]] = []
    g_score: dict[tuple[int, int], int] = {start: 0}
    parents: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
    heappush(open_heap, (_manhattan(start, goal), _manhattan(start, goal), 0, 0, start))
    expansions = 0
    counter = 0
    while open_heap and expansions < max_steps:
        _f, _h, _c, g_current, current = heappop(open_heap)
        if g_current > g_score.get(current, 1 << 30):
            continue  # stale heap entry
        if current == goal:
            path = [goal]
            cursor = parents[goal]
            while cursor is not None:
                path.append(cursor)
                cursor = parents[cursor]
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


_object_names: dict[tuple[str, str], str] = {}
_object_name_counters: defaultdict[str, int] = defaultdict(int)


def _object_name(object_id: Any, prefix: str) -> str:
    key = prefix, str(object_id)
    if key not in _object_names:
        _object_name_counters[prefix] += 1
        _object_names[key] = f"{prefix}{_object_name_counters[prefix]}"
    return _object_names[key]


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
    population_tier: int = 0
    upkeep_next_tick: int = 0
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
    shot_predictions: list[dict] = field(default_factory=list)
    shot_prediction_results: list[dict] = field(default_factory=list)
    plan_unit_actions: dict[str, str] = field(default_factory=dict)
    plan_core_action: str | None = None
    accepted: bool = False
    latency_ms: float = 0.0


class TacticLogger:
    """Logs every Tick decision to a JSONL file for later review."""

    def __init__(self, path: str = "tactic_log.jsonl") -> None:
        self.path = path
        self._file: Any = None
        self._tick_count = 0
        self._start_time = time.monotonic()
        self._last_tick_time = 0.0

    def open(self) -> None:
        self._file = open(self.path, "a", encoding="utf-8")
        self._write_header()

    def _write_header(self) -> None:
        header = {
            "_meta": "arena-hero-tactic-log",
            "_started_at": datetime.now(timezone.utc).isoformat(),
            "_version": 3,
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
        # SDK 0.2.9+ dropped these two server fields; keep the dashboard/log shape.
        rec.population_tier = getattr(state, "population_tier", 0)
        rec.upkeep_next_tick = getattr(state, "upkeep_next_tick", 0)
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
        rec.accepted = accepted
        rec.latency_ms = round(latency_ms, 1)

        if core:
            rec.core_name = _object_name(core.id, "C")
            rec.core_pos = list(core.position)
            rec.core_hp = core.hp
            rec.core_shield = core.shield
            rec.core_state = core.view.state.value if hasattr(core.view.state, "value") else str(core.view.state)

        for w in turn.workers:
            wid = str(w.id)[:8]
            route = turn_context.worker_routes.get(wid, {})
            rec.workers.append({
                "id": wid,
                "name": _object_name(w.id, "W"),
                "pos": list(w.position),
                "target": list(route["target"]) if route.get("target") else None,
                "path": [list(position) for position in route.get("path", [])],
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
                "path": [list(p) for p in v_route.get("path", [])],
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
                "path": [list(p) for p in r_route.get("path", [])],
                "path_complete": bool(r_route.get("complete", False)),
                "hp": r.hp,
            })

        for enemy in turn.visible_enemies:
            rec.enemies.append({
                "id": str(enemy.id)[:8],
                "name": _object_name(enemy.id, "E"),
                "pos": list(enemy.position),
                "hp": getattr(enemy, "hp", None),
                "type": _enemy_unit_type_name(enemy) or "ENEMY",
            })

        for event in turn.events:
            rec.events.append({
                "type": event.event_type,
                "reason": event.reason_code,
                "actor": str(event.actor_id)[:8] if event.actor_id else None,
                "pos": list(event.position) if event.position else None,
                "amount": event.resource_amount,
            })

        self._maybe_rotate()
        if self._file and not self._file.closed:
            self._file.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
            self._file.flush()

        self._tick_count += 1
        self._last_tick_time = now
        return rec


# ── unit planners ────────────────────────────────────────────────────────────

def _retreat_from(
    pos: tuple[int, int],
    core_pos: tuple[int, int],
    obstacle_cells: frozenset[tuple[int, int]],
    others: frozenset[tuple[int, int]],
) -> Direction | None:
    """Pick a direction that moves away from core_pos, if any is free.

    Used to back a full worker out of the core's immediate ring so the core cell
    can free up for unloading. Prefers the direction that increases distance to
    the core the most; skips obstacles, occupied cells and dead ends.
    """
    best: Direction | None = None
    best_dist = -1
    for d in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT):
        npos = (pos[0] + d.delta[0], pos[1] + d.delta[1])
        if npos in obstacle_cells or npos in others:
            continue
        if _is_dead_end_step(npos, obstacle_cells):
            continue
        dist = _manhattan(npos, core_pos)
        if dist > best_dist:
            best_dist = dist
            best = d
    return best


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
    try:
        k = path.index(pos)
    except ValueError:
        # Unit is not on the cached path (blocked or diverted last tick).
        return None
    if k + 1 >= len(path):
        _worker_path_cache.pop(uid, None)
        return None
    next_cell = path[k + 1]
    # The goal cell may be stepped on only while it is actually free; any other
    # occupied cell invalidates the cached step (server rejects the move).
    if next_cell in others:
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
    _set_worker_route(worker, tuple(goal), path, complete=True)
    return ("MOVE", f"{bfs_dir.name} -> {goal}")


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


def _is_combat_threat(enemy: Any) -> bool:
    """True when the visible enemy can deal combat damage.

    Workers have no attack. Only Vanguard melee, Ranger shots, and enemy Cores
    can hurt a stationary friendly unit. Unknown stubs (tests / bare objects)
    stay treated as threats so missing type data fails safe.
    """
    name = _enemy_unit_type_name(enemy)
    if name == "WORKER":
        return False
    if name in {"VANGUARD", "RANGER", "CORE"}:
        return True
    return True


def _combat_threats(enemies: tuple | list) -> tuple:
    """Filter visible enemies down to units/cores that can actually attack."""
    return tuple(e for e in enemies if _is_combat_threat(e))


def _worker_flee(
    worker,
    uid: str,
    pos: tuple[int, int],
    enemy_pos: tuple[int, int],
    core_pos: tuple[int, int],
    obstacle_cells: frozenset[tuple[int, int]],
    others: frozenset[tuple[int, int]],
    carrying: bool,
) -> tuple[str, str]:
    """Move one step away from the enemy.

    Game rule (player-confirmed): a moving unit is never hit — only a
    stationary one takes damage. A threatened worker must therefore spend every
    Tick MOVING (never WAIT / HARVEST / DEPOSIT). Pick a free, non-dead-end
    cell that maximizes distance from the enemy; fall back to any free cell;
    only WAIT when every neighbor is blocked. `others` includes the enemy cells
    themselves, so the worker can never step onto an enemy.

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
) -> tuple[str, str]:
    """March one unit to a manually set target; resume normal planning on arrival.

    Dashboard per-unit ⌖ targets are display-name keyed (W3/V2/R1). While under
    a manual waypoint the unit only walks — no mining / depositing / firing —
    except workers keep the enemy-evasion rule so a manual trip cannot get them
    killed. Reaching the target deletes the waypoint; the next Tick the unit
    falls back to its normal program behavior.

    A target that is an obstacle can never be entered; standing in the cell
    adjacent to it counts as arrival. A target that cannot be reached at all
    (sealed off / permanently occupied) auto-clears after
    _WAYPOINT_STUCK_THRESHOLD ticks of no progress, so the unit does not stand
    or circle there forever.
    """
    pos = tuple(unit.position)
    target = (int(waypoint[0]), int(waypoint[1]))
    is_worker = getattr(unit, "unit_type", None) == UnitType.WORKER
    uid = str(unit.id)

    def _record(path: list[tuple[int, int]], complete: bool) -> None:
        if is_worker:
            _set_worker_route(unit, target, path, complete=complete)
        else:
            _set_unit_route(unit, target, path, complete=complete)

    def _finish(detail_prefix: str) -> tuple[str, str]:
        _waypoint_stuck.pop(uid, None)
        _remove_waypoint(name, expected_target=target)
        _record([tuple(pos), target], complete=True)
        return ("WAIT", f"{detail_prefix} {target}")

    if pos == target:
        return _finish("waypoint-reached")

    # Obstacle target: the closest reachable success is the adjacent cell.
    if target in obstacle_cells and _manhattan(pos, target) <= 1:
        return _finish("waypoint-reached-adjacent")

    blocked = frozenset(obstacle_cells) | frozenset(occupied)

    def _count_stuck() -> bool:
        """Return True when the unit should give up on this waypoint."""
        ticks, last_target = _waypoint_stuck.get(uid, (0, target))
        new_ticks = ticks + 1 if last_target == target else 1
        _waypoint_stuck[uid] = (new_ticks, target)
        return new_ticks >= _WAYPOINT_STUCK_THRESHOLD

    # Workers keep the survival rule while marching: never stop next to an
    # attacking enemy. Fleeing is survival, not stagnation — reset the counter.
    if is_worker:
        threat_radius = int(config.get("enemy_threat_radius", 3))
        combat_enemies = _combat_threats(enemies)
        if combat_enemies and threat_radius > 0:
            nearest = min(
                combat_enemies, key=lambda e: _manhattan(pos, tuple(e.position))
            )
            if _manhattan(pos, tuple(nearest.position)) <= threat_radius:
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
                )

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
) -> tuple[str, str]:
    """Return (action_name, detail)."""
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
        nearest_enemy = min(
            combat_enemies, key=lambda e: _manhattan(pos, tuple(e.position))
        )
        if _manhattan(pos, tuple(nearest_enemy.position)) <= threat_radius:
            # `others` already carries the enemy cells in production (occupied
            # includes them); union them again so a fleeing worker can never
            # step onto an enemy even if `occupied` was not passed. Block all
            # visible enemy cells (including non-combat Workers) as geometry.
            flee_blocked = others | {tuple(e.position) for e in enemies}
            return _worker_flee(
                worker,
                uid,
                pos,
                tuple(nearest_enemy.position),
                core_pos,
                obstacle_cells,
                flee_blocked,
                carrying,
            )

    if pos == core.position and turn_context.resource_space > 0 and carrying:
        worker.deposit()
        _set_worker_route(worker, tuple(core.position), [tuple(pos)], complete=True)
        return ("DEPOSIT", f"at_core cargo={worker.cargo}")

    if pos in resource_cells and pos not in depleted and not carrying and not explore_mode:
        worker.harvest()
        _set_worker_route(worker, tuple(pos), [tuple(pos)], complete=True)
        return ("HARVEST", f"on_resource {pos}")

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
        cached_move = _worker_cached_path_step(worker, uid, pos, goal, obstacle_cells, others)
        if cached_move is not None:
            return cached_move

        # Workers can walk onto the core cell; the game may report it as an
        # obstacle so temporarily exclude it from the pathfinding obstacle set.
        # Other units are real blockers and stay in the obstacle set.
        bfs_obs = obstacle_cells | others
        if goal == core_pos:
            bfs_obs = bfs_obs - {goal}
        path = _bfs_path(
            pos,
            goal,
            bfs_obs,
            max_steps=int(config["bfs_max_steps"]),
        )
        if path and len(path) > 1:
            _worker_path_cache[uid] = {
                "goal": tuple(goal),
                "path": path,
                # Debug only — never compared for cache validity.
                "obstacles_used": bfs_obs,
            }
        else:
            _worker_path_cache.pop(uid, None)
        bfs_dir = _direction_for_step(pos, path[1]) if path and len(path) > 1 else None
        if bfs_dir is not None:
            nx, ny = pos[0] + bfs_dir.delta[0], pos[1] + bfs_dir.delta[1]
            npos = (nx, ny)
            # Step only into a free cell (the goal cell itself counts only when free).
            if (npos not in obstacle_cells or npos == goal) and npos not in others:
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
                if npos in obstacle_cells or npos in others:
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
                retreat = _retreat_from(pos, core_pos, obstacle_cells, others)
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
            if npos in obstacle_cells or npos in others:
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
        prev = _worker_last_pos.get(uid)
        recent = _worker_recent.get(uid, [])
        idx = hash(uid) % 4
        base = [Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT]
        rotated = base[idx:] + base[:idx]
        # Sort: deprioritize dead ends, backtracking and recently visited cells
        recent_set = set(recent)
        def _sort_key(d):
            nx, ny = pos[0] + d.delta[0], pos[1] + d.delta[1]
            score = 0
            if _is_dead_end_step((nx, ny), obstacle_cells):
                score += 3  # obvious dead end = avoid first
            if (nx, ny) in recent_set:
                score += 2  # recently visited
            if config["avoid_backtracking"] and prev and (nx, ny) == prev:
                score += 1  # backtracking
            return score
        rotated.sort(key=_sort_key)
        for d in rotated:
            nx, ny = pos[0] + d.delta[0], pos[1] + d.delta[1]
            if (nx, ny) in obstacle_cells or (nx, ny) in others:
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
            _set_worker_route(worker, (nx, ny), [tuple(pos), (nx, ny)], complete=True)
            return ("MOVE", f"{d.name} explore")

    if goal is not None and goal != pos:
        direction = _step_towards(pos, goal)
        if direction is not None:
            nx = pos[0] + direction.delta[0]
            ny = pos[1] + direction.delta[1]
            if (nx, ny) not in obstacle_cells and (nx, ny) not in others and not (
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
    """Return home / attack / guerrilla / unassigned for a named combat unit."""
    name = unit_name.upper()
    if name in _parse_team_names(config.get("home_team", "")):
        return "home"
    if name in _parse_team_names(config.get("attack_team", "")):
        return "attack"
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
    """Auto-enlist unassigned Vanguards/Rangers into the home team roster."""
    normalized_names = {
        str(raw_name).strip().upper()
        for raw_name in unit_names
        if str(raw_name).strip()
    }
    added: list[str] = []

    def apply(latest: dict[str, int | bool | str]) -> dict[str, Any] | None:
        home = _parse_team_names(latest.get("home_team", ""))
        attack = _parse_team_names(latest.get("attack_team", ""))
        guerrilla = _parse_team_names(latest.get("guerrilla_team", ""))
        assigned = home | attack | guerrilla
        added.extend(sorted(normalized_names - assigned))
        if not added:
            return None
        home.update(added)
        latest["home_team"] = _format_team_roster(home)
        return latest

    try:
        saved = mutate_config(apply, CONFIG_PATH)
        if added:
            print(
                f"[team] auto-enlisted {', '.join(added)} -> home_team={saved['home_team']}",
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

    # Prune dead units from the latest config under the same cross-process lock
    # used by dashboard saves.
    changed = False

    def prune(latest: dict[str, int | bool | str]) -> dict[str, Any] | None:
        nonlocal changed
        for team_key in ("home_team", "attack_team", "guerrilla_team"):
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
    _worker_last_pos[str(unit.id)] = pos
    return True


def _move_towards(
    unit: Any,
    pos: tuple[int, int],
    goal: tuple[int, int],
    obstacle_cells: frozenset[tuple[int, int]],
    *,
    detail_prefix: str,
) -> tuple[str, str] | None:
    if pos == goal:
        _combat_path_cache.pop(str(unit.id), None)
        return None

    uid = str(unit.id)
    cached = _combat_path_cache.get(uid)
    path: list[tuple[int, int]] | None = None
    if cached is not None and cached.get("goal") == goal:
        cached_path = cached.get("path") or []
        try:
            index = cached_path.index(pos)
        except ValueError:
            index = -1
        if index >= 0 and index + 1 < len(cached_path):
            next_cell = cached_path[index + 1]
            if next_cell not in obstacle_cells and not _is_dead_end_step(
                next_cell, obstacle_cells, allow=(goal,),
            ):
                path = cached_path
    if path is None:
        _combat_path_cache.pop(uid, None)
        path = _bfs_path(pos, goal, obstacle_cells, max_steps=2500)
        if path and len(path) > 1:
            _combat_path_cache[uid] = {"goal": goal, "path": path}

    if path and len(path) > 1:
        try:
            index = path.index(pos)
        except ValueError:
            index = -1
        next_cell = path[index + 1] if 0 <= index + 1 < len(path) else None
        direction = _direction_for_step(pos, next_cell) if next_cell else None
        if direction is not None and _try_move(
            unit,
            direction,
            pos,
            obstacle_cells,
            avoid_dead_ends=True,
            allow=(goal,),
        ):
            _set_unit_route(unit, goal, path, complete=True)
            return ("MOVE", f"{direction.name} {detail_prefix} {goal}")

    # Preserve the cheap greedy fallback when A* exhausts its search budget.
    direction = _step_towards(pos, goal)
    if direction is None:
        return None
    # Skip 凸-shaped cul-de-sacs unless the goal itself is inside one.
    allow = (goal,)
    if _try_move(
        unit,
        direction,
        pos,
        obstacle_cells,
        avoid_dead_ends=True,
        allow=allow,
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
        if _try_move(
            unit,
            alt,
            pos,
            obstacle_cells,
            avoid_dead_ends=True,
            allow=allow,
        ):
            return ("MOVE", f"{alt.name} {detail_prefix} {goal}")
    # Last resort: ignore dead-end filter if goal requires it.
    if _try_move(unit, direction, pos, obstacle_cells):
        _set_unit_route(unit, goal, [pos, goal], complete=False)
        return ("MOVE", f"{direction.name} {detail_prefix} {goal}")
    for alt in alternates:
        if alt == direction:
            continue
        if _try_move(unit, alt, pos, obstacle_cells):
            return ("MOVE", f"{alt.name} {detail_prefix} {goal}")
    return None


def _vanguard_adjacent_sweep(
    vanguard: Any,
    pos: tuple[int, int],
    enemies: tuple,
) -> tuple[str, str] | None:
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
    """Describe a lead-shot candidate without changing the queued shot."""
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


def _ranger_best_shot(
    ranger: Any,
    pos: tuple[int, int],
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    attack_range: int,
) -> tuple[str, str] | None:
    """Pick the closest enemy in legal 8-way range (rules v0.8/v0.13).

    Distance is Chebyshev — horizontal, vertical, or exact-diagonal offsets
    where max(|dx|, |dy|) is within 1..attack_range. Diagonal shots are legal
    and only intermediate crossed cells can block the line.
    """
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
        if dist < best_dist:
            best_dist = dist
            best_target = enemy
    if best_target is None:
        return None
    turn_context.shot_predictions.append(
        _shadow_shot_prediction(
            ranger,
            pos,
            best_target,
            obstacle_cells,
            attack_range,
        )
    )
    # Shadow mode only: keep the authoritative current-cell shot unchanged.
    ranger.shoot(best_target)
    return ("SHOOT", f"enemy at {best_target.position} dist={best_dist}")


def _scout_cardinal(
    unit: Any,
    pos: tuple[int, int],
    obstacle_cells: frozenset[tuple[int, int]],
    config: dict[str, Any],
    *,
    label: str,
) -> tuple[str, str]:
    prev = _worker_last_pos.get(str(unit.id))
    idx = hash(str(unit.id)) % 4
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
        if _try_move(
            unit,
            direction,
            pos,
            obstacle_cells,
            avoid_dead_ends=True,
        ):
            npos = (pos[0] + direction.delta[0], pos[1] + direction.delta[1])
            _set_unit_route(unit, npos, [pos, npos], complete=False)
            return ("MOVE", f"{direction.name} {label}")
    # If every open neighbor is a dead end, take the least-bad exit rather than wait.
    for direction in rotated:
        if _try_move(unit, direction, pos, obstacle_cells):
            npos = (pos[0] + direction.delta[0], pos[1] + direction.delta[1])
            _set_unit_route(unit, npos, [pos, npos], complete=False)
            return ("MOVE", f"{direction.name} {label}")
    unit.wait()
    return ("WAIT", "no_way")


def _home_patrol_goal(
    unit_id: str,
    core_pos: tuple[int, int],
    radius: int,
) -> tuple[int, int]:
    """Pick a stable perimeter offset around the Core for this unit."""
    radius = max(1, int(radius))
    # Eight slots around the ring keep multiple defenders spread out.
    slots = (
        (0, -radius),
        (radius, -radius),
        (radius, 0),
        (radius, radius),
        (0, radius),
        (-radius, radius),
        (-radius, 0),
        (-radius, -radius),
    )
    slot = slots[hash(str(unit_id)) % len(slots)]
    return (core_pos[0] + slot[0], core_pos[1] + slot[1])


def _plan_home_combat(
    unit: Any,
    *,
    unit_kind: str,
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    core_pos: tuple[int, int],
    config: dict[str, Any],
) -> tuple[str, str]:
    """Defend near the Core: engage nearby threats, otherwise patrol the ring."""
    pos = tuple(unit.position)
    radius = int(config["home_patrol_radius"])
    # Hysteresis: only force a return when clearly outside the ring. A one-cell
    # band stops home-return / home-patrol A-B-A flipping on the perimeter.
    return_radius = radius + 1

    if unit_kind == "vanguard":
        sweep = _vanguard_adjacent_sweep(unit, pos, enemies)
        if sweep is not None:
            return sweep
    else:
        shot = _ranger_best_shot(
            unit,
            pos,
            enemies,
            obstacle_cells,
            int(config["ranger_attack_range"]),
        )
        if shot is not None:
            return shot

    # Only chase enemies that are already inside the home perimeter.
    local_enemies = [
        enemy for enemy in enemies
        if _manhattan(core_pos, enemy.position) <= return_radius
    ]
    if local_enemies:
        nearest = min(local_enemies, key=lambda e: _manhattan(pos, e.position))
        moved = _move_towards(
            unit,
            pos,
            tuple(nearest.position),
            obstacle_cells,
            detail_prefix="home-engage",
        )
        if moved is not None:
            return moved

    dist_home = _manhattan(pos, core_pos)
    if dist_home > return_radius:
        moved = _move_towards(
            unit, pos, core_pos, obstacle_cells, detail_prefix="home-return",
        )
        if moved is not None:
            return moved

    goal = _home_patrol_goal(str(unit.id), core_pos, radius)
    # Already on/near the assigned slot: hold instead of micro-stepping.
    if _manhattan(pos, goal) <= 1 and dist_home <= return_radius:
        unit.wait()
        _set_unit_route(unit, goal, [pos], complete=True)
        return ("WAIT", f"home-hold {goal}")
    if pos != goal:
        moved = _move_towards(
            unit, pos, goal, obstacle_cells, detail_prefix="home-patrol",
        )
        if moved is not None:
            return moved

    return _scout_cardinal(
        unit, pos, obstacle_cells, config, label="home-patrol",
    )


def _plan_attack_combat(
    unit: Any,
    *,
    unit_kind: str,
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    config: dict[str, Any],
) -> tuple[str, str]:
    """March as a group toward the configured destination; engage en route.

    attack_mode (三选一) picks the destination:
      - coords -> static attack_target_x / attack_target_y
      - auto   -> nearest enemy sighting from memory
      - beacon -> the champion beacon's always-public position; the static
                  coordinate and auto-attack settings are ignored in this mode
    """
    pos = tuple(unit.position)

    mode = str(config.get("attack_mode", "coords"))
    if mode == "beacon":
        beacon_pos = getattr(turn_context, "beacon_pos", None)
        target = beacon_pos or (int(config["attack_target_x"]), int(config["attack_target_y"]))
    elif mode == "auto" and _enemy_memory:
        target = min(_enemy_memory, key=lambda p: _manhattan(pos, p))
    else:
        target = (int(config["attack_target_x"]), int(config["attack_target_y"]))

    if unit_kind == "vanguard":
        sweep = _vanguard_adjacent_sweep(unit, pos, enemies)
        if sweep is not None:
            return sweep
    else:
        shot = _ranger_best_shot(
            unit,
            pos,
            enemies,
            obstacle_cells,
            int(config["ranger_attack_range"]),
        )
        if shot is not None:
            return shot

    if enemies:
        nearest = min(enemies, key=lambda e: _manhattan(pos, e.position))
        moved = _move_towards(
            unit,
            pos,
            tuple(nearest.position),
            obstacle_cells,
            detail_prefix="attack-engage",
        )
        if moved is not None:
            return moved

    if pos == target:
        unit.wait()
        return ("WAIT", f"attack-hold-{mode} {target}")

    moved = _move_towards(
        unit, pos, target, obstacle_cells, detail_prefix=f"attack-march-{mode}",
    )
    if moved is not None:
        return moved

    unit.wait()
    return ("WAIT", f"attack-blocked-{mode} {target}")


def _plan_guerrilla_combat(
    unit: Any,
    *,
    unit_kind: str,
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    config: dict[str, Any],
) -> tuple[str, str]:
    """Fan out on 8 bearings; pick off singles, retreat from packs."""
    pos = tuple(unit.position)
    enemy_count = len(enemies)

    if enemy_count >= 3:
        # Retreat away from the enemy cluster centroid.
        cx = sum(int(e.position[0]) for e in enemies) / enemy_count
        cy = sum(int(e.position[1]) for e in enemies) / enemy_count
        flee_dx = pos[0] - cx
        flee_dy = pos[1] - cy
        if flee_dx == 0 and flee_dy == 0:
            # Already on the centroid: pick any hashed cardinal escape.
            direction = (
                Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT
            )[hash(str(unit.id)) % 4]
        else:
            direction = _cardinal_toward_delta(
                1 if flee_dx > 0 else (-1 if flee_dx < 0 else 0),
                1 if flee_dy > 0 else (-1 if flee_dy < 0 else 0),
            )
        if direction is not None and _try_move(unit, direction, pos, obstacle_cells):
            return ("MOVE", f"{direction.name} guerrilla-retreat n={enemy_count}")
        # If primary escape is blocked, try remaining cardinals.
        for fallback in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT):
            if direction is not None and fallback == direction:
                continue
            if _try_move(unit, fallback, pos, obstacle_cells):
                return ("MOVE", f"{fallback.name} guerrilla-retreat n={enemy_count}")
        unit.wait()
        return ("WAIT", f"guerrilla-retreat-blocked n={enemy_count}")

    if enemy_count == 1:
        if unit_kind == "vanguard":
            sweep = _vanguard_adjacent_sweep(unit, pos, enemies)
            if sweep is not None:
                return sweep
        else:
            shot = _ranger_best_shot(
                unit,
                pos,
                enemies,
                obstacle_cells,
                int(config["ranger_attack_range"]),
            )
            if shot is not None:
                return shot
        nearest = enemies[0]
        moved = _move_towards(
            unit,
            pos,
            tuple(nearest.position),
            obstacle_cells,
            detail_prefix="guerrilla-engage",
        )
        if moved is not None:
            return moved

    if enemy_count == 2:
        # Two enemies: hold position if already able to strike, else keep roaming.
        if unit_kind == "vanguard":
            sweep = _vanguard_adjacent_sweep(unit, pos, enemies)
            if sweep is not None:
                return sweep
        else:
            shot = _ranger_best_shot(
                unit,
                pos,
                enemies,
                obstacle_cells,
                int(config["ranger_attack_range"]),
            )
            if shot is not None:
                return shot

    # Roam on a fixed 8-way bearing. Diagonals become alternating cardinals.
    bearing = hash(str(unit.id)) % 8
    dx, dy = _EIGHT_WAY_DELTAS[bearing]
    label = _EIGHT_WAY_LABELS[bearing]
    if dx != 0 and dy != 0:
        # Alternate the two component axes so the path approximates the diagonal.
        tick_bit = int(getattr(turn_context, "tick", 0) or 0) & 1
        primary = (
            (Direction.RIGHT if dx > 0 else Direction.LEFT)
            if tick_bit == 0
            else (Direction.DOWN if dy > 0 else Direction.UP)
        )
        secondary = (
            (Direction.DOWN if dy > 0 else Direction.UP)
            if tick_bit == 0
            else (Direction.RIGHT if dx > 0 else Direction.LEFT)
        )
        for direction in (primary, secondary):
            if _try_move(
                unit, direction, pos, obstacle_cells, avoid_dead_ends=True,
            ):
                return ("MOVE", f"{direction.name} guerrilla-roam {label}")
        for direction in (primary, secondary):
            if _try_move(unit, direction, pos, obstacle_cells):
                return ("MOVE", f"{direction.name} guerrilla-roam {label}")
    else:
        direction = _cardinal_toward_delta(dx, dy)
        if direction is not None and _try_move(
            unit, direction, pos, obstacle_cells, avoid_dead_ends=True,
        ):
            return ("MOVE", f"{direction.name} guerrilla-roam {label}")
        if direction is not None and _try_move(unit, direction, pos, obstacle_cells):
            return ("MOVE", f"{direction.name} guerrilla-roam {label}")

    return _scout_cardinal(
        unit, pos, obstacle_cells, config, label=f"guerrilla-roam {label}",
    )


def _plan_vanguard(
    vanguard,
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    config: dict[str, Any],
    *,
    core_pos: tuple[int, int],
    team: str,
) -> tuple[str, str]:
    """Dispatch a Vanguard according to its combat team assignment."""
    if team == "home":
        return _plan_home_combat(
            vanguard,
            unit_kind="vanguard",
            enemies=enemies,
            obstacle_cells=obstacle_cells,
            core_pos=core_pos,
            config=config,
        )
    if team == "attack":
        return _plan_attack_combat(
            vanguard,
            unit_kind="vanguard",
            enemies=enemies,
            obstacle_cells=obstacle_cells,
            config=config,
        )
    if team == "guerrilla":
        return _plan_guerrilla_combat(
            vanguard,
            unit_kind="vanguard",
            enemies=enemies,
            obstacle_cells=obstacle_cells,
            config=config,
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
) -> tuple[str, str]:
    """Dispatch a Ranger according to its combat team assignment."""
    if team == "home":
        return _plan_home_combat(
            ranger,
            unit_kind="ranger",
            enemies=enemies,
            obstacle_cells=obstacle_cells,
            core_pos=core_pos,
            config=config,
        )
    if team == "attack":
        return _plan_attack_combat(
            ranger,
            unit_kind="ranger",
            enemies=enemies,
            obstacle_cells=obstacle_cells,
            config=config,
        )
    if team == "guerrilla":
        return _plan_guerrilla_combat(
            ranger,
            unit_kind="ranger",
            enemies=enemies,
            obstacle_cells=obstacle_cells,
            config=config,
        )

    pos = tuple(ranger.position)
    shot = _ranger_best_shot(
        ranger,
        pos,
        enemies,
        obstacle_cells,
        int(config["ranger_attack_range"]),
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
# Consecutive visible positions keyed by full enemy UUID. This is intentionally
# in-memory only; reconnects must rebuild confidence from fresh observations.
_enemy_motion_tracks: dict[str, list[tuple[int, tuple[int, int]]]] = {}
_pending_shot_predictions: list[dict[str, Any]] = []
# Last applied dashboard enemy-clear sequence from map_memory.json.
_enemy_clear_seq: int = 0
# Signature of the last dashboard map edits we absorbed (avoid re-applying every tick).
_last_dashboard_map_sig: tuple | None = None
# Track each worker's previous position to avoid backtracking
_worker_last_pos: dict[str, tuple[int, int]] = {}
_worker_recent: dict[str, list[tuple[int, int]]] = {}  # last 4 positions, anti-oscillation
# Resource assignment: each resource assigned to closest worker only
_resource_assignments: dict[str, tuple[int, int]] = {}
# Worker A* path cache: reuse a computed path across ticks instead of recomputing
# from scratch every tick. Keyed by full str(worker.id); entry {goal, path}.
_worker_path_cache: dict[str, dict] = {}
# Combat units use the same map search but keep a separate cache because their
# goals change independently as enemies move or team assignments change.
_combat_path_cache: dict[str, dict] = {}
# Consecutive ticks a worker has not moved — triggers un-stick recovery.
# _worker_stuck_ticks counts consecutive same-position ticks; _worker_stuck_pos
# remembers the position those ticks were counted at (independent of last-pos,
# which only tracks moves and would miss a worker frozen from the start).
_worker_stuck_ticks: dict[str, int] = {}
_worker_stuck_pos: dict[str, tuple[int, int]] = {}
_STUCK_THRESHOLD = 8
# Consecutive ticks a unit fails to get closer to its manual waypoint before the
# waypoint auto-clears and the unit resumes its normal program. (An obstacle
# target never resolves to entry — being adjacent counts as arrival instead.)
_waypoint_stuck: dict[str, tuple[int, tuple[int, int]]] = {}
_WAYPOINT_STUCK_THRESHOLD = 15
_map_dirty: bool = False
_last_map_save_tick: int = -1

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


def _load_map_memory() -> None:
    """Load permanent obstacle/resource/enemy memory from disk."""
    global _resource_memory, _obstacle_memory, _enemy_memory, _enemy_clear_seq, _last_dashboard_map_sig
    if not MAP_MEMORY_PATH.exists():
        return
    try:
        data = json.loads(MAP_MEMORY_PATH.read_text(encoding="utf-8"))
        forgotten = _coords_from_payload(data.get("forgotten_resources"))
        _obstacle_memory = _coords_from_payload(data.get("obstacles"))
        resources = _coords_from_payload(data.get("resources"))
        manual = _coords_from_payload(data.get("manual_resources"))
        _resource_memory = (resources | manual) - forgotten
        _resource_tombstones.clear()
        _resource_tombstones.update(forgotten)
        _enemy_memory = _coords_from_payload(data.get("enemy_sightings"))
        _enemy_clear_seq = int(data.get("enemy_clear_seq", 0) or 0)
        _last_dashboard_map_sig = _dashboard_map_sig(data)
        print(
            f"[map] loaded obstacles={len(_obstacle_memory)} resources={len(_resource_memory)} "
            f"manual={len(manual - forgotten)} forgotten={len(forgotten)} "
            f"enemies={len(_enemy_memory)} from {MAP_MEMORY_PATH}",
            flush=True,
        )
    except Exception as e:
        print(f"[map] load failed: {e}", flush=True)



def _dashboard_map_sig(data: dict) -> tuple:
    """Stable signature of dashboard-owned map fields."""
    forgotten = tuple(sorted(_coords_from_payload(data.get("forgotten_resources"))))
    manual = tuple(sorted(_coords_from_payload(data.get("manual_resources"))))
    resources = tuple(sorted(_coords_from_payload(data.get("resources"))))
    enemies = tuple(sorted(_coords_from_payload(data.get("enemy_sightings"))))
    clear_seq = int(data.get("enemy_clear_seq", 0) or 0)
    return (forgotten, manual, resources, enemies, clear_seq)


def _apply_dashboard_map_edits() -> None:
    """Pull dashboard deletions/additions into process memory before planning/saving.

    The dashboard and tactic are separate processes. Without this sync, a
    dashboard clear only edits map_memory.json, and the next tactic save would
    rewrite the old in-memory resources or enemy sightings back onto disk.

    Edits are applied only when the dashboard-owned signature changes, so a
    live re-discovery after a clear is not immediately re-forgotten from a
    stale on-disk forget list.
    """
    global _resource_memory, _enemy_memory, _enemy_clear_seq, _map_dirty, _last_dashboard_map_sig
    if not MAP_MEMORY_PATH.exists():
        return
    try:
        data = json.loads(MAP_MEMORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return

    sig = _dashboard_map_sig(data)
    if sig == _last_dashboard_map_sig:
        return
    _last_dashboard_map_sig = sig

    forgotten = _coords_from_payload(data.get("forgotten_resources"))
    manual = _coords_from_payload(data.get("manual_resources"))
    disk_resources = _coords_from_payload(data.get("resources"))

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
    # disk_resources is intentionally unused for rehydration.
    _ = disk_resources

    clear_seq = int(data.get("enemy_clear_seq", 0) or 0)
    if clear_seq > _enemy_clear_seq:
        _enemy_clear_seq = clear_seq
        disk_enemies = _coords_from_payload(data.get("enemy_sightings"))
        if _enemy_memory != disk_enemies:
            _enemy_memory.clear()
            _enemy_memory.update(disk_enemies)
            _map_dirty = True


def _map_transaction(func):
    @wraps(func)
    def locked(*args, **kwargs):
        with file_lock(MAP_MEMORY_PATH):
            return func(*args, **kwargs)

    return locked


@_map_transaction
def _save_map_memory(
    tick: int | None = None,
    force: bool = False,
    save_interval_ticks: int = 10,
) -> None:
    """Persist permanent map memory. Obstacles never shrink.

    Manual resources entered from the dashboard are preserved across saves,
    except coordinates listed in forgotten_resources / local tombstones.
    """
    global _map_dirty, _last_map_save_tick, _enemy_clear_seq, _last_dashboard_map_sig
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

    manual: set[tuple[int, int]] = set()
    disk_forgotten: set[tuple[int, int]] = set()
    disk_enemy_clear_seq = _enemy_clear_seq
    if MAP_MEMORY_PATH.exists():
        try:
            prev = json.loads(MAP_MEMORY_PATH.read_text(encoding="utf-8"))
            manual = _coords_from_payload(prev.get("manual_resources"))
            disk_forgotten = _coords_from_payload(prev.get("forgotten_resources"))
            disk_enemy_clear_seq = int(prev.get("enemy_clear_seq", _enemy_clear_seq) or 0)
        except Exception:
            manual = set()
            disk_forgotten = set()

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

    payload = {
        "updated_tick": tick,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "obstacles": sorted([list(p) for p in _obstacle_memory]),
        "resources": sorted([list(p) for p in resources]),
        "manual_resources": sorted([list(p) for p in manual]),
        "forgotten_resources": sorted([list(p) for p in forgotten]),
        "enemy_sightings": sorted([list(p) for p in _enemy_memory]),
        "obstacle_count": len(_obstacle_memory),
        "resource_count": len(resources),
        "manual_count": len(manual),
        "enemy_sighting_count": len(_enemy_memory),
        "enemy_clear_seq": _enemy_clear_seq,
    }
    atomic_write_text(MAP_MEMORY_PATH, json.dumps(payload, ensure_ascii=False))
    _map_dirty = False
    _last_dashboard_map_sig = _dashboard_map_sig(payload)
    if tick is not None:
        _last_map_save_tick = tick


# ── manual per-unit waypoints (dashboard ⌖) ─────────────────────────────────
# The dashboard and tactic are separate processes sharing waypoints.json. All
# writes are read-modify-write on the latest file so one side's change never
# clobbers the other's (same discipline as map_memory.json).

def _load_waypoints() -> dict[str, tuple[int, int]]:
    """Read manual per-unit targets as {display_name: (x, y)}."""
    if not WAYPOINTS_PATH.exists():
        return {}
    try:
        data = json.loads(WAYPOINTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    targets = data.get("targets") if isinstance(data, dict) else None
    out: dict[str, tuple[int, int]] = {}
    if isinstance(targets, dict):
        for name, pos in targets.items():
            if (
                isinstance(name, str)
                and isinstance(pos, (list, tuple))
                and len(pos) == 2
            ):
                try:
                    out[name] = (int(pos[0]), int(pos[1]))
                except (TypeError, ValueError):
                    continue
    return out


def _write_waypoints_unlocked(targets: dict[str, tuple[int, int]]) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "targets": {name: [int(x), int(y)] for name, (x, y) in targets.items()},
    }
    atomic_write_text(WAYPOINTS_PATH, json.dumps(payload, ensure_ascii=False))


def _write_waypoints(targets: dict[str, tuple[int, int]]) -> None:
    with file_lock(WAYPOINTS_PATH):
        _write_waypoints_unlocked(targets)


def _remove_waypoint(
    name: str,
    *,
    expected_target: tuple[int, int] | None = None,
) -> None:
    """Delete one manual target, preserving any concurrent dashboard writes."""
    try:
        with file_lock(WAYPOINTS_PATH):
            targets = _load_waypoints()
            if (
                name in targets
                and (
                    expected_target is None
                    or targets[name] == tuple(expected_target)
                )
            ):
                del targets[name]
                _write_waypoints_unlocked(targets)
    except Exception:
        pass


def _prune_waypoint_targets(
    targets: dict[str, tuple[int, int]],
    alive_names: set[str],
) -> bool:
    """Remove waypoints whose unit is gone; return True when anything changed."""
    stale = [name for name in targets if name not in alive_names]
    for name in stale:
        targets.pop(name, None)
    return bool(stale)


def _load_and_prune_waypoints(alive_names: set[str]) -> dict[str, tuple[int, int]]:
    """Load and prune targets as one transaction with dashboard writers."""
    with file_lock(WAYPOINTS_PATH):
        targets = _load_waypoints()
        if _prune_waypoint_targets(targets, alive_names):
            _write_waypoints_unlocked(targets)
        return targets



def _update_obstacle_memory(turn) -> frozenset[tuple[int, int]]:
    """Accumulate permanent obstacles. Returns full known obstacle set."""
    global _obstacle_memory, _map_dirty
    before = len(_obstacle_memory)
    for p in turn.obstacle_cells:
        _obstacle_memory.add(tuple(p) if not isinstance(p, tuple) else p)
    if len(_obstacle_memory) > before:
        _map_dirty = True
    return frozenset(_obstacle_memory)


def _forget_resource(position: tuple[int, int]) -> None:
    """Mark a resource as absent in memory and the next persisted map."""
    global _map_dirty
    pos = tuple(position)
    _resource_memory.discard(pos)
    _resource_tombstones.add(pos)
    _map_dirty = True


def _update_resource_memory(turn) -> None:
    """Remember visible resources permanently until depletion is confirmed.

    A node is forgotten only when the server actually reports it gone:
    HARVEST_FAILED / RESOURCE_DEPLETED, or a worker arriving at a remembered
    cell that no longer shows a resource. HARVEST_FAILED / CARGO_FULL is NOT a
    depletion signal — it is the server refusing a harvest because the worker
    already carries cargo (harvest fills the full capacity in one action), so
    the node stays remembered for the next empty worker.
    """
    global _resource_memory, _map_dirty
    before = set(_resource_memory)

    for p in turn.resource_cells:
        pos = tuple(p) if not isinstance(p, tuple) else p
        _resource_memory.add(pos)
        _resource_tombstones.discard(pos)

    for event in turn.events:
        if (
            event.event_type == "HARVEST_FAILED"
            and event.reason_code == "RESOURCE_DEPLETED"
            and event.position
        ):
            pos = tuple(event.position)
            _forget_resource(pos)

    if _resource_memory != before:
        _map_dirty = True


def _update_enemy_sightings(turn) -> None:
    """Record every position where enemies were seen.  Remove stale sightings
    only when a friendly unit can genuinely see the cell (within its own vision
    radius, with unobstructed line of sight) and no enemy is there.  A sighting
    no friendly can actually see is kept as last-known enemy info."""
    global _enemy_memory, _map_dirty
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
    visible_enemy_positions: set[tuple[int, int]] = {
        tuple(enemy.position) for enemy in turn.visible_enemies
    }

    # Add new sightings
    for pos in visible_enemy_positions:
        if pos not in _enemy_memory:
            _enemy_memory.add(pos)

    # Remove stale sightings: some friendly unit can actually see the cell
    # (within its own vision radius, line of sight unobstructed) but no enemy
    # is there.  Obstacles block sight, so a spot behind a wall stays a hint.
    obstacles = frozenset(_obstacle_memory) | turn.obstacle_cells
    stale: set[tuple[int, int]] = set()
    for sighting in _enemy_memory:
        if sighting in visible_enemy_positions:
            continue  # still there
        for fpos, radius in friendly_views:
            if (
                _manhattan(fpos, sighting) <= radius
                and not _vision_obstructed(fpos, sighting, obstacles)
            ):
                stale.add(sighting)
                break

    if stale:
        _enemy_memory -= stale

    if len(_enemy_memory) != before:
        _map_dirty = True


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
    return _object_name(object_id, "U")


def _classify_battle_event(turn: Any, event: Any) -> tuple[str | None, str | None]:
    """Map one resolution event to a (category, human message) log row."""
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

    if et == "SHOT_HIT":
        dmg = values.get("damage")
        return "combat", f"{actor} 击中 {target}" + (f" 造成 {dmg} 伤害" if dmg else "")
    if et == "SWEEP_RESOLVED":
        n = values.get("targets_hit", 0)
        return "combat", f"{actor} 横扫命中 {n} 个目标"
    if et == "DESTRUCTION_PARTICIPATION":
        return "kill", f"{actor} 参与摧毁 {target}"
    if et == "CORE_RESOURCES_CAPTURED":
        return "kill", "摧毁敌方核心" + (f"，缴获 {amount} 资源" if amount else "")
    if et == "UNIT_DAMAGED":
        hp = values.get("hp")
        dmg = values.get("damage")
        if hp == 0:
            return "defeat", f"{target} 被击败"
        if reason == "UPKEEP_DEFICIT":
            return "defeat", f"{target} 因维护费亏损 {dmg} 点"
        return "combat", f"{target} 受 {dmg} 伤害（HP {hp}）"
    if et == "UNIT_SELF_DESTRUCTED":
        return "defeat", f"{actor} 超编自裁"
    if et == "CORE_DAMAGED":
        return "defeat", "核心受到攻击"
    if et == "CORE_DESTROYED":
        return "defeat", "核心被摧毁"
    if et == "HARVEST_SUCCEEDED":
        return "economy", f"{actor} 挖矿 +{amount or '?'}"
    if et == "DEPOSIT_SUCCEEDED":
        return "economy", f"{actor} 卸货 +{amount or '?'}"
    if et == "BEACON_HARVEST_BONUS":
        return "economy", f"{actor} 信标加成 +{amount or '?'}"
    if et == "UPKEEP_PAID":
        due = values.get("due", 0)
        deficit = values.get("deficit", 0)
        msg = f"维护费 {due}"
        if deficit:
            msg += f"，亏损 {deficit} 自裁单位"
        return "economy", msg
    if et == "CORE_REPAIR_SUCCEEDED":
        return "economy", "核心修盾 +1"
    if et in ("CORE_HEAL_SUCCEEDED", "UNIT_HEAL_SUCCEEDED"):
        who = "核心" if et.startswith("CORE") else actor
        return "economy", f"{who} 回血 +{amount or '?'}"
    if et == "CORE_SPAWN_SUCCEEDED":
        tname = _UNIT_TYPE_LABELS.get(str(values.get("unit_type", "")), "单位")
        return "economy", f"生产 {tname}（{target}）"
    if et == "CORE_RESOURCE_OVERFLOW_DESTROYED":
        return "economy", f"人口下降，{amount or '?'} 资源被销毁"

    if et == "HARVEST_FAILED":
        return "warn", f"{actor} 挖矿失败：{_LOG_CAT_REASONS['HARVEST_FAILED'].get(reason, reason)}"
    if et == "DEPOSIT_FAILED":
        return "warn", f"{actor} 卸货失败：{_LOG_CAT_REASONS['DEPOSIT_FAILED'].get(reason, reason)}"
    if et == "SHOT_MISSED":
        return "warn", f"{actor} 射击未命中"
    if et in ("CORE_HEAL_FAILED", "UNIT_HEAL_FAILED", "CORE_REPAIR_FAILED"):
        return "warn", f"修复/回血失败：{reason or '—'}"
    if et == "CORE_SPAWN_FAILED":
        why = _LOG_CAT_REASONS["CORE_SPAWN_FAILED"].get(reason, reason or "—")
        return "warn", f"生产失败：{why}"
    if et in (
        "UNIT_MOVE_FAILED",
        "CORE_MOVE_FAILED",
        "CORE_MOVE_START_FAILED",
        "CORE_ACTION_FAILED",
    ):
        return "warn", f"{actor} 移动/动作失败：{reason or '—'}"
    return None, None


def _battle_log_entries(
    turn: Any,
    *,
    new_resources: set[tuple[int, int]],
    new_enemy_sightings: set[tuple[int, int]],
) -> list[dict]:
    """Build the categorized log rows for one Tick (discoveries + events)."""
    entries: list[dict] = []
    tick = turn_context.tick
    for pos in sorted(new_resources):
        entries.append({
            "tick": tick, "cat": "discover",
            "msg": f"发现新矿点 ({pos[0]},{pos[1]})",
        })
    for pos in sorted(new_enemy_sightings):
        entries.append({
            "tick": tick, "cat": "discover",
            "msg": f"发现敌人踪迹 ({pos[0]},{pos[1]})",
        })
    for event in getattr(turn, "events", ()) or ():
        cat, msg = _classify_battle_event(turn, event)
        if cat and msg:
            entries.append({"tick": tick, "cat": cat, "msg": msg})
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
        "shot_predictions": [],
        "shot_prediction_results": [],
    },
)()


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

    reserve = int(config.get("resource_reserve", 0))
    for unit_type in _SPAWN_PRIORITY:
        count = _unit_type_count(turn, unit_type)
        target = int(config.get(_TARGET_KEYS[unit_type], _TARGET_DEFAULTS[unit_type]))
        if count >= target:
            continue
        cost = UNIT_SPAWN_COSTS[unit_type.name]
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
    for dead_id in set(_worker_last_pos) - alive_ids:
        _worker_last_pos.pop(dead_id, None)
    for dead_id in set(_worker_recent) - alive_ids:
        _worker_recent.pop(dead_id, None)
    for dead_id in set(_resource_assignments) - alive_ids:
        _resource_assignments.pop(dead_id, None)
    for dead_id in set(_worker_path_cache) - alive_ids:
        _worker_path_cache.pop(dead_id, None)
    for dead_id in set(_combat_path_cache) - alive_ids:
        _combat_path_cache.pop(dead_id, None)
    for dead_id in set(_worker_stuck_ticks) - alive_ids:
        _worker_stuck_ticks.pop(dead_id, None)
    for dead_id in set(_worker_stuck_pos) - alive_ids:
        _worker_stuck_pos.pop(dead_id, None)
    for dead_id in set(_waypoint_stuck) - alive_ids:
        _waypoint_stuck.pop(dead_id, None)
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


def choose_actions(turn) -> tuple[str, dict[str, str]]:
    """Queue actions, return (core_action_name, {unit_id: action_detail})."""
    unit_actions_detail: dict[str, str] = {}
    core_action_name = "WAIT"
    config = load_config()
    turn_context.worker_routes = {}
    turn_context.unit_routes = {}
    turn_context.tick = int(getattr(turn, "tick", 0) or 0)
    turn_context.shot_predictions = []
    turn_context.shot_prediction_results = _resolve_shadow_predictions(
        turn, turn_context.tick,
    )
    game_stats.record_prediction_results(
        _game_stats, turn_context.shot_prediction_results,
    )
    _update_enemy_motion_tracks(turn.visible_enemies, turn_context.tick)

    # Manual per-unit targets set from the dashboard (display-name keyed).
    # Prune targets whose unit no longer exists — names are computed BEFORE the
    # dead-unit cleanup below so a just-died unit's name is never re-issued.
    alive_names = {
        _object_name(u.id, _UNIT_NAME_PREFIX.get(u.unit_type, "U"))
        for u in turn.units
    }
    waypoints = _load_and_prune_waypoints(alive_names)

    # ── Cleanup dead-unit bookkeeping ──────────────────────────────────
    alive_ids: set[str] = set()
    for w in turn.workers:
        alive_ids.add(str(w.id))
    for v in getattr(turn, "vanguards", ()) or ():
        alive_ids.add(str(v.id))
    for r in getattr(turn, "rangers", ()) or ():
        alive_ids.add(str(r.id))
    _prune_dead_unit_bookkeeping(alive_ids)

    # ── Aggregate battle-report statistics ─────────────────────────────
    game_stats.sync_units(_game_stats, turn, turn_context.tick)
    game_stats.record_events(_game_stats, turn, turn_context.tick)
    game_stats.sampled(_game_stats, turn_context.tick)
    game_stats.maybe_save(_game_stats, turn_context.tick)

    # ── Update permanent map memory ────────────────────────────────────
    # Honor dashboard clears before we re-accumulate or plan against memory.
    _apply_dashboard_map_edits()
    known_obstacles = _update_obstacle_memory(turn)
    res_before = set(_resource_memory)
    enemy_before = set(_enemy_memory)
    _update_resource_memory(turn)
    _update_enemy_sightings(turn)
    _save_map_memory(
        tick=getattr(turn, "tick", None),
        save_interval_ticks=int(config["map_save_interval_ticks"]),
    )

    # ── Categorized battle log (discoveries + resolution events) ────────
    # Best-effort: a log write must never break the game loop, so failures
    # are swallowed.  New discoveries are diffed from the memory updates above;
    # combat/economy/warn rows come from this Tick's resolution events.
    battle_entries = _battle_log_entries(
        turn,
        new_resources=set(_resource_memory) - res_before,
        new_enemy_sightings=set(_enemy_memory) - enemy_before,
    )
    if battle_entries:
        try:
            append_jsonl(BATTLE_LOG_PATH, battle_entries)
        except OSError:
            pass

    # ── Lifecycle guard ─────────────────────────────────────────────────
    if turn.core is None:
        return ("RESPAWN", {})

    # Newly produced (or previously unassigned) combat units join 守家队.
    config = _auto_enlist_new_combat_units(turn, config)

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

    # 金币满仓 + 开启"满仓探索"时，不给工人派矿点：空载工人失去目标后自然进入
    # 探索分支，去各处侦察。仓库一旦有空位，下一 tick 恢复派矿。
    gold_full_explore = (
        bool(config.get("worker_explore_when_full", False))
        and turn_context.resource_space == 0
    )
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
        resource_set = set(all_resources)

        # Workers ignore mines farther than this Manhattan distance from the core —
        # a far deposit round-trip wastes more ticks than it earns, and a worker
        # stranded out deep is easy prey. 0 (default) disables the cap.
        mine_max_distance = int(config.get("worker_mine_max_distance", 0))
        if mine_max_distance > 0:
            all_resources = [
                r for r in all_resources
                if _manhattan(tuple(r), core_pos) <= mine_max_distance
            ]

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
        open_resources = [r for r in all_resources if r not in claimed_resources]
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

    # ── Core action ─────────────────────────────────────────────────────
    core_done = False

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
        want_repair = core.shield < shield_target and core.shield < effective_cap
        if want_repair:
            core.repair_shield()
            core_action_name = "REPAIR_SHIELD"
            core_done = True

    # ── Core movement ────────────────────────────────────────────────────
    if not core_done and config["core_movement_enabled"]:
        # Stop if a cargo worker is close (any carrying worker heads home to
        # deposit), otherwise move toward them
        close_cargo = any(
            w.cargo > 0
            and _manhattan(w.position, core_pos) <= int(config["cargo_wait_distance"])
            for w in turn.workers
        )
        if not close_cargo:
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
                # Also consider nearest resource (visible or remembered)
                res_target = None
                if all_resources:
                    res_target = min(all_resources, key=lambda p: _manhattan(core_pos, p))
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
            # Try each direction (obstacle + dead-end aware)
            for d in dirs:
                nx, ny = core_pos[0] + d.delta[0], core_pos[1] + d.delta[1]
                if (nx, ny) in obstacle_cells:
                    continue
                if _is_dead_end_step((nx, ny), obstacle_cells, allow=(target,)):
                    continue
                core.start_move(d)
                core_action_name = f"MOVE_{d.name}"
                core_done = True
                break

    if not core_done:
        core.wait()
        core_action_name = "WAIT"

    # Occupied cells this tick (every friendly unit + visible enemies). Workers
    # use this to avoid hammering blocked cells — the main cause of the core-ring
    # deadlock where full workers wedge in place and the economy stalls. The core
    # cell itself is deliberately NOT included: a worker may step onto it to
    # unload, so it only counts as occupied when another unit is standing there.
    occupied: frozenset[tuple[int, int]] = frozenset(
        {tuple(w.position) for w in turn.workers}
        | {tuple(v.position) for v in getattr(turn, "vanguards", ()) or ()}
        | {tuple(r.position) for r in getattr(turn, "rangers", ()) or ()}
        | {tuple(e.position) for e in enemies}
    )

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
        heal_budget -= UNIT_SPAWN_COSTS.get(core_action_name.split("_", 1)[1], 0)

    for unit in turn.units:
        uid = str(unit.id)[:8]
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
            continue
        # Manual per-unit waypoint: march to the configured coordinate, then
        # resume the normal planner once it is reached.
        name = _object_name(unit.id, _UNIT_NAME_PREFIX.get(unit.unit_type, "U"))
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
            )
            unit_actions_detail[uid] = f"{action}:{detail}[waypoint]"
            continue
        if unit.unit_type == UnitType.WORKER:
            action, detail = _plan_worker(
                unit, core,
                resource_cells=resource_cells,
                obstacle_cells=obstacle_cells,
                depleted=depleted,
                config=config,
                occupied=occupied,
                enemies=enemies,
            )
            unit_actions_detail[uid] = f"{action}:{detail}"
        elif unit.unit_type == UnitType.VANGUARD:
            unit_name = _object_name(unit.id, "V")
            team = _combat_team_for(unit_name, config)
            action, detail = _plan_vanguard(
                unit,
                enemies,
                obstacle_cells,
                config,
                core_pos=tuple(core_pos),
                team=team,
            )
            unit_actions_detail[uid] = f"{action}:{detail}[{team}]"
        elif unit.unit_type == UnitType.RANGER:
            unit_name = _object_name(unit.id, "R")
            team = _combat_team_for(unit_name, config)
            action, detail = _plan_ranger(
                unit,
                enemies,
                obstacle_cells,
                config,
                core_pos=tuple(core_pos),
                team=team,
            )
            unit_actions_detail[uid] = f"{action}:{detail}[{team}]"

    return core_action_name, unit_actions_detail


# ── live loop ────────────────────────────────────────────────────────────────

def play(api_key: str, log_path: str = DEFAULT_LOG_PATH) -> None:
    """Run forever with automatic reconnect on protocol/transport failures."""
    _load_map_memory()
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

    try:
        while True:
            session += 1
            try:
                print(f"[tactic] connecting session={session}", flush=True)
                with ArenaHeroClient(api_key=api_key) as game:
                    for turn in game.turns():
                        reconnect_delay = 1.0  # healthy stream → reset backoff
                        tick_start = time.monotonic()
                        try:
                            core_action, unit_actions = choose_actions(turn)
                        except Exception as e:
                            print(
                                f"tick={getattr(turn, 'tick', '?')} plan_error={e}\n"
                                f"{traceback.format_exc()}",
                                flush=True,
                            )
                            continue
                        try:
                            accepted = turn.submit()
                            _commit_shadow_predictions(accepted.accepted)
                            latency = (time.monotonic() - tick_start) * 1000
                            plan_actions: dict[str, str] = {}
                            for uid, detail in unit_actions.items():
                                plan_actions[uid] = detail
                            logger.record_tick(
                                turn,
                                core_action=core_action,
                                unit_actions=plan_actions,
                                accepted=accepted.accepted,
                                latency_ms=latency,
                            )
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
                        except Exception as e:
                            print(
                                f"tick={turn.tick} submit_error={e}\n"
                                f"{traceback.format_exc()}",
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
