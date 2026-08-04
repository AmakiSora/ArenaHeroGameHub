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
from getpass import getpass
from heapq import heappop, heappush
from pathlib import Path
from typing import Any

from arena_hero import (
    ArenaHeroClient,
    Direction,
    UnitType,
)
from arena_hero.errors import ProtocolError, TransportError
import game_stats
from tactic_config import CONFIG_PATH, load_config, save_config

def _data_dir() -> Path:
    raw = os.environ.get("ARENA_DATA_DIR", "").strip()
    return Path(raw).resolve() if raw else Path.cwd()


MAP_MEMORY_PATH = _data_dir() / "map_memory.json"
DEFAULT_LOG_PATH = str(_data_dir() / "tactic_log.jsonl")

# Rotate tactic_log.jsonl when it exceeds this size and keep at most N backups.
LOG_MAX_BYTES = int(os.environ.get("ARENA_LOG_MAX_MB", "20")) * 1024 * 1024
LOG_BACKUP_COUNT = 3
# Shutdown summary only reads this many of the newest tick records.
_SUMMARY_TAIL_RECORDS = 10000

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
    x1, y1 = a
    x2, y2 = b
    if x1 == x2:
        step = 1 if y2 > y1 else -1
        for y in range(y1 + step, y2, step):
            if (x1, y) in obstacles:
                return True
    elif y1 == y2:
        step = 1 if x2 > x1 else -1
        for x in range(x1 + step, x2, step):
            if (x, y1) in obstacles:
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
            "_version": 2,
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
        rec.population_tier = state.population_tier
        rec.upkeep_next_tick = state.upkeep_next_tick
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
            enemy_type = getattr(enemy, "unit_type", None)
            if hasattr(enemy_type, "value"):
                enemy_type = enemy_type.value
            rec.enemies.append({
                "id": str(enemy.id)[:8],
                "name": _object_name(enemy.id, "E"),
                "pos": list(enemy.position),
                "hp": getattr(enemy, "hp", None),
                "type": str(enemy_type) if enemy_type is not None else "ENEMY",
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


def _plan_worker(
    worker,
    core,
    *,
    resource_cells: frozenset[tuple[int, int]],
    obstacle_cells: frozenset[tuple[int, int]],
    depleted: set[tuple[int, int]],
    config: dict[str, int | bool],
    beacon_carried: bool = False,
    occupied: frozenset[tuple[int, int]] = frozenset(),
) -> tuple[str, str]:
    """Return (action_name, detail)."""
    pos = worker.position
    max_cargo = 2 if beacon_carried else 1
    uid = str(worker.id)
    # Cells taken by other units this tick. Workers cannot move into them, so
    # pathfinding treats them as temporary obstacles. The worker's own cell is
    # excluded; the core cell is not in `occupied` at all (the core may be
    # stepped on to unload), so it only blocks when another unit stands there.
    core_pos = tuple(core.position)
    others = occupied - {pos}

    if worker.cargo >= max_cargo and pos == core.position and turn_context.resource_space > 0:
        worker.deposit()
        _set_worker_route(worker, tuple(core.position), [tuple(pos)], complete=True)
        return ("DEPOSIT", f"at_core cargo={worker.cargo}")

    if pos in resource_cells and pos not in depleted and worker.cargo < max_cargo:
        worker.harvest()
        _set_worker_route(worker, tuple(pos), [tuple(pos)], complete=True)
        return ("HARVEST", f"on_resource {pos}")

    goal: tuple[int, int] | None = None
    if worker.cargo >= max_cargo:
        # Only FULL workers target the core. A partially-loaded worker (e.g. a
        # second haul while the beacon is carried) must keep harvesting — sending
        # it to the core just parks it on the unloading cell where it can't
        # deposit, wedging the full workers behind it.
        goal = core.position
    elif resource_cells:
        # Only go to assigned resource, not the nearest one
        assigned = _resource_assignments.get(uid)
        if assigned and assigned in resource_cells:
            goal = assigned
        # If no assignment, skip visible resources (they're assigned to other workers)
    # Fallback: use remembered resource coordinates (only if assigned)
    if goal is None and worker.cargo < max_cargo:
        assigned = _resource_assignments.get(uid)
        if assigned and assigned in _resource_memory:
            goal = assigned

    # Reaching a remembered target without seeing a resource confirms that the
    # memory is stale. Forget it and explore immediately instead of waiting on
    # the empty cell forever.
    if goal == pos and worker.cargo < max_cargo and pos not in resource_cells:
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
        if worker.cargo < max_cargo:
            if goal is not None:
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
        if pos == core_pos and worker.cargo < max_cargo:
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
        if worker.cargo >= max_cargo and goal == core_pos and core_pos in others:
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
    if worker.cargo:
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

    # No goal at all: fan out with backtracking + dead-end avoidance
    if goal is None and worker.cargo < max_cargo:
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
    home = _parse_team_names(config.get("home_team", ""))
    attack = _parse_team_names(config.get("attack_team", ""))
    guerrilla = _parse_team_names(config.get("guerrilla_team", ""))
    added: list[str] = []
    for raw_name in unit_names:
        name = str(raw_name).strip().upper()
        if not name or name in home or name in attack or name in guerrilla:
            continue
        home.add(name)
        added.append(name)
    if not added:
        return config

    updated = dict(_freshest_config())
    updated["home_team"] = _format_team_roster(home)
    try:
        saved = save_config(updated)
        print(
            f"[team] auto-enlisted {', '.join(added)} -> home_team={saved['home_team']}",
            flush=True,
        )
        return saved
    except Exception as exc:
        print(f"[team] auto-enlist save failed: {exc}", flush=True)
        return updated


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

    # Prune dead units from all team rosters
    updated = dict(_freshest_config())
    changed = False
    for team_key in ("home_team", "attack_team", "guerrilla_team"):
        old = _parse_team_names(config.get(team_key, ""))
        pruned = old & alive
        if pruned != old:
            updated[team_key] = _format_team_roster(pruned)
            changed = True

    if changed:
        try:
            saved = save_config(updated)
            print(f"[team] pruned dead from rosters", flush=True)
            return saved
        except Exception as exc:
            print(f"[team] roster prune failed: {exc}", flush=True)
            return updated

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
        return None
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


def _ranger_best_shot(
    ranger: Any,
    pos: tuple[int, int],
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    attack_range: int,
) -> tuple[str, str] | None:
    best_dist = 10_000
    best_target = None
    for enemy in enemies:
        dist = _manhattan(pos, enemy.position)
        if not (1 <= dist <= attack_range):
            continue
        if _line_blocked(pos, enemy.position, obstacle_cells):
            continue
        if dist < best_dist:
            best_dist = dist
            best_target = enemy
    if best_target is None:
        return None
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
# Track each worker's previous position to avoid backtracking
_worker_last_pos: dict[str, tuple[int, int]] = {}
_worker_recent: dict[str, list[tuple[int, int]]] = {}  # last 4 positions, anti-oscillation
# Resource assignment: each resource assigned to closest worker only
_resource_assignments: dict[str, tuple[int, int]] = {}
# Worker A* path cache: reuse a computed path across ticks instead of recomputing
# from scratch every tick. Keyed by full str(worker.id); entry {goal, path}.
_worker_path_cache: dict[str, dict] = {}
# Consecutive ticks a worker has not moved — triggers un-stick recovery.
# _worker_stuck_ticks counts consecutive same-position ticks; _worker_stuck_pos
# remembers the position those ticks were counted at (independent of last-pos,
# which only tracks moves and would miss a worker frozen from the start).
_worker_stuck_ticks: dict[str, int] = {}
_worker_stuck_pos: dict[str, tuple[int, int]] = {}
_STUCK_THRESHOLD = 8
_map_dirty: bool = False
_last_map_save_tick: int = -1

# Cumulative battle-report statistics (economy / combat / production + per-unit
# details), persisted to game_stats.json so they survive process restarts.
_game_stats: dict[str, Any] = game_stats.load()


def _load_map_memory() -> None:
    """Load permanent obstacle/resource/enemy memory from disk."""
    global _resource_memory, _obstacle_memory, _enemy_memory
    if not MAP_MEMORY_PATH.exists():
        return
    try:
        data = json.loads(MAP_MEMORY_PATH.read_text(encoding="utf-8"))
        _obstacle_memory = {tuple(p) for p in data.get("obstacles", []) if len(p) == 2}
        resources = {tuple(p) for p in data.get("resources", []) if len(p) == 2}
        manual = {tuple(p) for p in data.get("manual_resources", []) if len(p) == 2}
        _resource_memory = resources | manual
        _enemy_memory = {tuple(p) for p in data.get("enemy_sightings", []) if len(p) == 2}
        print(
            f"[map] loaded obstacles={len(_obstacle_memory)} resources={len(_resource_memory)} "
            f"manual={len(manual)} enemies={len(_enemy_memory)} from {MAP_MEMORY_PATH}",
            flush=True,
        )
    except Exception as e:
        print(f"[map] load failed: {e}", flush=True)



def _save_map_memory(
    tick: int | None = None,
    force: bool = False,
    save_interval_ticks: int = 10,
) -> None:
    """Persist permanent map memory. Obstacles never shrink.

    Manual resources entered from the dashboard are preserved across saves.
    """
    global _map_dirty, _last_map_save_tick
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
    if MAP_MEMORY_PATH.exists():
        try:
            prev = json.loads(MAP_MEMORY_PATH.read_text(encoding="utf-8"))
            manual = {tuple(p) for p in prev.get("manual_resources", []) if len(p) == 2}
        except Exception:
            manual = set()

    manual -= _resource_tombstones
    resources = (set(_resource_memory) | manual) - _resource_tombstones
    _resource_memory.clear()
    _resource_memory.update(resources)

    payload = {
        "updated_tick": tick,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "obstacles": sorted([list(p) for p in _obstacle_memory]),
        "resources": sorted([list(p) for p in resources]),
        "manual_resources": sorted([list(p) for p in manual]),
        "enemy_sightings": sorted([list(p) for p in _enemy_memory]),
        "obstacle_count": len(_obstacle_memory),
        "resource_count": len(resources),
        "manual_count": len(manual),
        "enemy_sighting_count": len(_enemy_memory),
    }
    tmp = MAP_MEMORY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(MAP_MEMORY_PATH)
    _resource_tombstones.clear()
    _map_dirty = False
    if tick is not None:
        _last_map_save_tick = tick



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

    Manual resources are never auto-removed here; only RESOURCE_DEPLETED clears
    a remembered point. HARVEST_SUCCEEDED alone does not prove the node is gone.
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
    when a friendly unit is looking at the cell and no enemy is there."""
    global _enemy_memory, _map_dirty
    before = len(_enemy_memory)

    # Collect all friendly positions for sight checking
    friendly_positions: set[tuple[int, int]] = set()
    if turn.core:
        friendly_positions.add(tuple(turn.core.position))
    for w in turn.workers:
        friendly_positions.add(tuple(w.position))
    for v in turn.vanguards:
        friendly_positions.add(tuple(v.position))
    for r in turn.rangers:
        friendly_positions.add(tuple(r.position))

    # Visible enemies this tick
    visible_enemy_positions: set[tuple[int, int]] = {
        tuple(enemy.position) for enemy in turn.visible_enemies
    }

    # Add new sightings
    for pos in visible_enemy_positions:
        if pos not in _enemy_memory:
            _enemy_memory.add(pos)

    # Remove stale sightings: friendly unit within range 5 of a sighting,
    # but no visible enemy there
    VISION_RANGE = 5
    stale: set[tuple[int, int]] = set()
    for sighting in _enemy_memory:
        if sighting in visible_enemy_positions:
            continue  # still there
        # Check if any friendly unit is close enough to see this cell
        for fpos in friendly_positions:
            if _manhattan(fpos, sighting) <= VISION_RANGE:
                stale.add(sighting)
                break

    if stale:
        _enemy_memory -= stale

    if len(_enemy_memory) != before:
        _map_dirty = True


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
    - population is below `population_cap`,
    - resources cover `cost + resource_reserve`.
    """
    if any(tuple(unit.position) == tuple(core.position) for unit in turn.units):
        return None
    pop_cap = int(config.get("population_cap", 20))
    if getattr(turn.state, "population", 0) >= pop_cap:
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


def _plan_over_target_self_destruct(
    turn: Any, core: Any, config: dict[str, Any],
) -> list[tuple[Any, str]]:
    """Pick one unit per over-target type to self-destruct this Tick.

    When a type's current count exceeds its target (e.g. the target was lowered),
    shed the least-useful unit: empty workers first, and among candidates the one
    furthest from the Core. At most one unit per type per Tick so a big overrun is
    trimmed gradually instead of wiping a whole corps at once. The Champion Beacon
    carrier is never selected.
    """
    result: list[tuple[Any, str]] = []
    core_pos = tuple(core.position)
    beacon = getattr(turn, "beacon", None)
    carrier_id = str(getattr(beacon, "carrier_id", None) or "")

    for unit_type in _SPAWN_PRIORITY:
        count = _unit_type_count(turn, unit_type)
        target = int(config.get(_TARGET_KEYS[unit_type], _TARGET_DEFAULTS[unit_type]))
        if count <= target:
            continue
        pool = [
            unit for unit in turn.units
            if unit.unit_type == unit_type and str(unit.id) != carrier_id
        ]
        if not pool:
            continue

        def key(unit: Any) -> tuple[int, int]:
            # Farthest from Core first; for workers prefer an empty one.
            empty = 1 if unit_type == UnitType.WORKER and getattr(unit, "cargo", 0) == 0 else 0
            return (_manhattan(unit.position, core_pos), empty)

        chosen = max(pool, key=key)
        result.append((chosen, f"{count}>{target}"))
    return result


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
    for dead_id in set(_worker_stuck_ticks) - alive_ids:
        _worker_stuck_ticks.pop(dead_id, None)
    for dead_id in set(_worker_stuck_pos) - alive_ids:
        _worker_stuck_pos.pop(dead_id, None)
    for (prefix, obj_id), name in list(_object_names.items()):
        if prefix in ("W", "V", "R") and str(obj_id) not in alive_ids:
            _object_names.pop((prefix, obj_id), None)


def choose_actions(turn) -> tuple[str, dict[str, str]]:
    """Queue actions, return (core_action_name, {unit_id: action_detail})."""
    unit_actions_detail: dict[str, str] = {}
    core_action_name = "WAIT"
    config = load_config()
    turn_context.worker_routes = {}
    turn_context.unit_routes = {}
    turn_context.tick = int(getattr(turn, "tick", 0) or 0)

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
    known_obstacles = _update_obstacle_memory(turn)
    _update_resource_memory(turn)
    _update_enemy_sightings(turn)
    _save_map_memory(
        tick=getattr(turn, "tick", None),
        save_interval_ticks=int(config["map_save_interval_ticks"]),
    )

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

    # Assign each resource to one worker (avoid stampede).
    # Keep sticky assignments across ticks so two nearby workers do not swap the
    # same mine every tick (that produced goal/explore A-B-A flipping).
    max_w_cargo = 2 if beacon_carried_by_core else 1
    idle_workers = [(str(w.id), tuple(w.position)) for w in turn.workers if w.cargo < max_w_cargo]
    idle_ids = {wid for wid, _ in idle_workers}
    all_resources = _merge_resource_cells(
        turn.resource_cells,
        _resource_memory,
        depleted,
    )
    resource_set = set(all_resources)

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
        # Stop if a cargo worker is close, otherwise move toward them
        close_cargo = any(
            w.cargo >= max_w_cargo
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
    # Shed over-target units first so their self-destruct (not a normal action)
    # is issued this Tick, and the per-unit planner skips them below.
    self_destructs = _plan_over_target_self_destruct(turn, core, config)
    sd_ids = {str(unit.id) for unit, _ in self_destructs}

    for unit in turn.units:
        if str(unit.id) in sd_ids:
            continue
        uid = str(unit.id)[:8]
        if unit.unit_type == UnitType.WORKER:
            action, detail = _plan_worker(
                unit, core,
                resource_cells=resource_cells,
                obstacle_cells=obstacle_cells,
                depleted=depleted,
                config=config,
                beacon_carried=beacon_carried_by_core,
                occupied=occupied,
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

    for unit, detail in self_destructs:
        unit.self_destruct()
        unit_actions_detail[str(unit.id)[:8]] = f"SELF_DESTRUCT:{detail}"

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
