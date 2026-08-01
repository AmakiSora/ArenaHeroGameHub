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
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from typing import Any

from arena_hero import (
    ArenaHeroClient,
    Direction,
    UnitType,
)
from tactic_config import load_config

MAP_MEMORY_PATH = Path("map_memory.json")


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


# ── BFS pathfinding (multi-step lookahead) ────────────────────────────────────

def _bfs_path(
    start: tuple[int, int],
    goal: tuple[int, int],
    obstacles: frozenset[tuple[int, int]],
    max_steps: int = 800,
) -> list[tuple[int, int]] | None:
    """Return the shortest path including start and goal, or None."""
    if start == goal:
        return [start]
    queue = deque([start])
    parents: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
    steps = 0
    while queue and steps < max_steps:
        steps += 1
        x, y = queue.popleft()
        for d in (Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT):
            nx, ny = x + d.delta[0], y + d.delta[1]
            next_pos = (nx, ny)
            if next_pos in parents or next_pos in obstacles:
                continue
            parents[next_pos] = (x, y)
            if next_pos == goal:
                path = [goal]
                cursor = parents[goal]
                while cursor is not None:
                    path.append(cursor)
                    cursor = parents[cursor]
                return list(reversed(path))
            queue.append(next_pos)
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


def _bfs_direction(
    start: tuple[int, int],
    goal: tuple[int, int],
    obstacles: frozenset[tuple[int, int]],
    max_steps: int = 800,
) -> Direction | None:
    """BFS shortest path, return first step direction or None if no path."""
    path = _bfs_path(start, goal, obstacles, max_steps)
    return _direction_for_step(start, path[1]) if path and len(path) > 1 else None


_object_names: dict[tuple[str, str], str] = {}
_object_name_counters: defaultdict[str, int] = defaultdict(int)


def _object_name(object_id: Any, prefix: str) -> str:
    key = prefix, str(object_id)
    if key not in _object_names:
        _object_name_counters[prefix] += 1
        _object_names[key] = f"{prefix}{_object_name_counters[prefix]}"
    return _object_names[key]


def _set_worker_route(
    worker: Any,
    target: tuple[int, int],
    path: list[tuple[int, int]],
    *,
    complete: bool,
) -> None:
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
        header = {
            "_meta": "arena-hero-tactic-log",
            "_started_at": datetime.now(timezone.utc).isoformat(),
            "_version": 2,
        }
        self._file.write(json.dumps(header, ensure_ascii=False) + "\n")
        self._file.flush()

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
            rec.vanguards.append({
                "id": str(v.id)[:8],
                "name": _object_name(v.id, "V"),
                "pos": list(v.position),
                "hp": v.hp,
            })

        for r in turn.rangers:
            rec.rangers.append({
                "id": str(r.id)[:8],
                "name": _object_name(r.id, "R"),
                "pos": list(r.position),
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

        if self._file and not self._file.closed:
            self._file.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
            self._file.flush()

        self._tick_count += 1
        self._last_tick_time = now
        return rec


# ── unit planners ────────────────────────────────────────────────────────────

def _plan_worker(
    worker,
    core,
    *,
    resource_cells: frozenset[tuple[int, int]],
    obstacle_cells: frozenset[tuple[int, int]],
    depleted: set[tuple[int, int]],
    config: dict[str, int | bool],
) -> tuple[str, str]:
    """Return (action_name, detail)."""
    pos = worker.position

    if worker.cargo and pos == core.position and turn_context.resource_space > 0:
        worker.deposit()
        _set_worker_route(worker, tuple(core.position), [tuple(pos)], complete=True)
        return ("DEPOSIT", f"at_core cargo={worker.cargo}")

    if pos in resource_cells and pos not in depleted and not worker.cargo:
        worker.harvest()
        _set_worker_route(worker, tuple(pos), [tuple(pos)], complete=True)
        return ("HARVEST", f"on_resource {pos}")

    goal: tuple[int, int] | None = None
    if worker.cargo:
        goal = core.position
    elif resource_cells:
        # Only go to assigned resource, not the nearest one
        assigned = _resource_assignments.get(str(worker.id))
        if assigned and assigned in resource_cells:
            goal = assigned
        # If no assignment, skip visible resources (they're assigned to other workers)
    # Fallback: use remembered resource coordinates (only if assigned)
    if goal is None and not worker.cargo:
        assigned = _resource_assignments.get(str(worker.id))
        if assigned and assigned in _resource_memory:
            goal = assigned

    # Reaching a remembered target without seeing a resource confirms that the
    # memory is stale. Forget it and explore immediately instead of waiting on
    # the empty cell forever.
    if goal == pos and not worker.cargo and pos not in resource_cells:
        _forget_resource(pos)
        _resource_assignments.pop(str(worker.id), None)
        goal = None

    # Move toward goal (BFS multi-step pathfinding, avoids dead ends)
    if config["worker_bfs_enabled"] and goal is not None and goal != pos:
        path = _bfs_path(
            pos,
            goal,
            obstacle_cells,
            max_steps=int(config["bfs_max_steps"]),
        )
        bfs_dir = _direction_for_step(pos, path[1]) if path and len(path) > 1 else None
        if bfs_dir is not None:
            nx, ny = pos[0] + bfs_dir.delta[0], pos[1] + bfs_dir.delta[1]
            if (nx, ny) not in obstacle_cells:
                worker.move(bfs_dir)
                _worker_last_pos[str(worker.id)] = pos
                _set_worker_route(worker, tuple(goal), path, complete=True)
                return ("MOVE", f"{bfs_dir.name} -> {goal}")
        # BFS failed (trapped) - fall through to explore or greedy fallback
        goal = None

    # Cargo worker with BFS failed: greedy move toward core
    if worker.cargo and goal is None:
        prev = _worker_last_pos.get(str(worker.id))
        def _cargo_sort(d):
            nx, ny = pos[0] + d.delta[0], pos[1] + d.delta[1]
            dist = _manhattan((nx, ny), core.position)
            if prev and (nx, ny) == prev:
                dist += int(config["backtrack_penalty"])
            return dist
        all_dirs = [Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT]
        all_dirs.sort(key=_cargo_sort)
        for d in all_dirs:
            nx, ny = pos[0] + d.delta[0], pos[1] + d.delta[1]
            if (nx, ny) not in obstacle_cells:
                worker.move(d)
                _worker_last_pos[str(worker.id)] = pos
                _set_worker_route(
                    worker,
                    tuple(core.position),
                    [tuple(pos), (nx, ny)],
                    complete=False,
                )
                return ("MOVE", f"{d.name} -> {core.position}")

    # No goal at all: fan out with backtracking avoidance
    if goal is None and not worker.cargo:
        uid = str(worker.id)
        prev = _worker_last_pos.get(uid)
        idx = hash(uid) % 4
        base = [Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT]
        rotated = base[idx:] + base[:idx]
        # Sort: deprioritize direction that goes back to previous position
        def _sort_key(d):
            nx, ny = pos[0] + d.delta[0], pos[1] + d.delta[1]
            if config["avoid_backtracking"] and prev and (nx, ny) == prev:
                return 1  # backtracking = bad
            return 0
        rotated.sort(key=_sort_key)
        for d in rotated:
            nx, ny = pos[0] + d.delta[0], pos[1] + d.delta[1]
            if (nx, ny) not in obstacle_cells:
                worker.move(d)
                _worker_last_pos[uid] = pos
                _set_worker_route(worker, (nx, ny), [tuple(pos), (nx, ny)], complete=True)
                return ("MOVE", f"{d.name} explore")

    if goal is not None and goal != pos:
        direction = _step_towards(pos, goal)
        if direction is not None:
            nx = pos[0] + direction.delta[0]
            ny = pos[1] + direction.delta[1]
            if (nx, ny) not in obstacle_cells:
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


def _plan_vanguard(
    vanguard,
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    config: dict[str, int | bool],
) -> tuple[str, str]:
    """Return (action_name, detail)."""
    pos = vanguard.position

    if config["vanguard_engage_enabled"]:
        for direction in (Direction.UP, Direction.DOWN, Direction.LEFT, Direction.RIGHT):
            tx, ty = pos[0] + direction.delta[0], pos[1] + direction.delta[1]
            for enemy in enemies:
                if enemy.position == (tx, ty):
                    vanguard.sweep(direction)
                    return ("SWEEP", f"{direction.name} -> enemy at {enemy.position}")

        if enemies:
            nearest = min(enemies, key=lambda e: _manhattan(pos, e.position))
            direction = _step_towards(pos, nearest.position)
            if direction is not None:
                nx, ny = pos[0] + direction.delta[0], pos[1] + direction.delta[1]
                if (nx, ny) not in obstacle_cells:
                    vanguard.move(direction)
                    return ("MOVE", f"{direction.name} -> enemy at {nearest.position}")

    # No enemies: scout with UUID rotation + backtrack avoidance
    prev = _worker_last_pos.get(str(vanguard.id))
    idx = hash(str(vanguard.id)) % 4
    base = [Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT]
    rotated = base[idx:] + base[:idx]
    rotated.sort(
        key=lambda d: 1
        if config["avoid_backtracking"]
        and prev
        and (pos[0] + d.delta[0], pos[1] + d.delta[1]) == prev
        else 0
    )
    for d in rotated:
        nx, ny = pos[0] + d.delta[0], pos[1] + d.delta[1]
        if (nx, ny) not in obstacle_cells:
            vanguard.move(d)
            _worker_last_pos[str(vanguard.id)] = pos
            return ("MOVE", f"{d.name} scout")

    vanguard.wait()
    return ("WAIT", "no_way")


def _plan_ranger(
    ranger,
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
    config: dict[str, int | bool],
) -> tuple[str, str]:
    """Return (action_name, detail)."""
    pos = ranger.position

    # 1. Shoot nearest in-range enemy with clear LOS
    best_dist = 10_000
    best_target = None

    if config["ranger_engage_enabled"]:
        for enemy in enemies:
            dist = _manhattan(pos, enemy.position)
            if not (1 <= dist <= int(config["ranger_attack_range"])):
                continue
            if _line_blocked(pos, enemy.position, obstacle_cells):
                continue
            if dist < best_dist:
                best_dist = dist
                best_target = enemy

    if best_target is not None:
        ranger.shoot(best_target)
        return ("SHOOT", f"enemy at {best_target.position} dist={best_dist}")

    # 2. Enemies visible but out of range: move toward them
    if config["ranger_engage_enabled"] and enemies:
        nearest = min(enemies, key=lambda e: _manhattan(pos, e.position))
        direction = _step_towards(pos, nearest.position)
        if direction is not None:
            nx, ny = pos[0] + direction.delta[0], pos[1] + direction.delta[1]
            if (nx, ny) not in obstacle_cells:
                ranger.move(direction)
                return ("MOVE", f"{direction.name} -> enemy at {nearest.position}")

    # 3. No enemies: scout in unique direction (based on UUID hash)
    idx = hash(str(ranger.id)) % 4
    base = [Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT]
    rotated = base[idx:] + base[:idx]
    for d in rotated:
        nx, ny = pos[0] + d.delta[0], pos[1] + d.delta[1]
        if (nx, ny) not in obstacle_cells:
            ranger.move(d)
            return ("MOVE", f"{d.name} scout")

    ranger.wait()
    return ("WAIT", "no_way")


# ── top-level tactic ─────────────────────────────────────────────────────────

# ── shared map memory (persists across ticks + process restarts) ───────────
_resource_memory: set[tuple[int, int]] = set()
# Resources confirmed absent but not yet flushed to map_memory.json. Tombstones
# prevent sticky manual entries on disk from being merged back during a save.
_resource_tombstones: set[tuple[int, int]] = set()
# Permanent obstacles: once seen, always blocked
_obstacle_memory: set[tuple[int, int]] = set()
# Track each worker's previous position to avoid backtracking
_worker_last_pos: dict[str, tuple[int, int]] = {}
# Resource assignment: each resource assigned to closest worker only
_resource_assignments: dict[str, tuple[int, int]] = {}
_map_dirty: bool = False
_last_map_save_tick: int = -1


def _load_map_memory() -> None:
    """Load permanent obstacle/resource memory from disk."""
    global _resource_memory, _obstacle_memory
    if not MAP_MEMORY_PATH.exists():
        return
    try:
        data = json.loads(MAP_MEMORY_PATH.read_text(encoding="utf-8"))
        _obstacle_memory = {tuple(p) for p in data.get("obstacles", []) if len(p) == 2}
        resources = {tuple(p) for p in data.get("resources", []) if len(p) == 2}
        # manual resources are sticky and must survive auto-cleanup
        manual = {tuple(p) for p in data.get("manual_resources", []) if len(p) == 2}
        _resource_memory = resources | manual
        print(
            f"[map] loaded obstacles={len(_obstacle_memory)} resources={len(_resource_memory)} "
            f"manual={len(manual)} from {MAP_MEMORY_PATH}",
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
        "obstacle_count": len(_obstacle_memory),
        "resource_count": len(resources),
        "manual_count": len(manual),
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



# Context holder for the current turn's resource_space (set by choose_actions)
turn_context = type(
    "_Ctx",
    (),
    {"resource_space": 0, "config": {}, "worker_routes": {}},
)()


def choose_actions(turn) -> tuple[str, dict[str, str]]:
    """Queue actions, return (core_action_name, {unit_id: action_detail})."""
    unit_actions_detail: dict[str, str] = {}
    core_action_name = "WAIT"
    config = load_config()
    turn_context.config = config
    turn_context.worker_routes = {}

    # ── Update permanent map memory ────────────────────────────────────
    known_obstacles = _update_obstacle_memory(turn)
    _update_resource_memory(turn)
    _save_map_memory(
        tick=getattr(turn, "tick", None),
        save_interval_ticks=int(config["map_save_interval_ticks"]),
    )

    # ── Lifecycle guard ─────────────────────────────────────────────────
    if turn.core is None:
        return ("RESPAWN", {})

    core = turn.core
    core_pos = core.position
    resources = turn.resources
    turn_context.resource_space = turn.resource_space

    resource_cells: frozenset[tuple[int, int]] = turn.resource_cells
    # Use permanent obstacle memory for pathfinding, not just current vision
    obstacle_cells: frozenset[tuple[int, int]] = known_obstacles
    enemies = turn.visible_enemies
    beacon = turn.beacon

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

    # Assign each resource to the closest worker (avoid stampede)
    _resource_assignments.clear()
    # Only assign to workers without cargo (cargo workers are heading home)
    idle_workers = [(str(w.id), tuple(w.position)) for w in turn.workers if not w.cargo]
    all_resources = _merge_resource_cells(
        turn.resource_cells,
        _resource_memory,
        depleted,
    )
    # Sort resources by distance to nearest idle worker, assign each to closest
    available = list(idle_workers)  # copy, will remove assigned workers
    for res in sorted(all_resources, key=lambda p: min(_manhattan(p, w[1]) for w in available) if available else 0):
        if not available:
            break
        closest = min(available, key=lambda w: _manhattan(res, w[1]))
        wid = closest[0]
        _resource_assignments[wid] = res
        available.remove(closest)  # remove assigned worker from pool
    for event in turn.events:
        if (
            event.event_type == "HARVEST_FAILED"
            and event.reason_code == "RESOURCE_DEPLETED"
            and event.position
        ):
            depleted.add(event.position)

    # ── Core action ─────────────────────────────────────────────────────
    core_done = False

    if beacon_on_ground_here:
        core.pickup_beacon()
        core_action_name = "PICKUP_BEACON"
        core_done = True

    if not core_done and config["repair_enabled"] and resources >= 1:
        effective_cap = 10 if beacon_carried_by_core else 5
        shield_target = (
            int(config["combat_shield_target"])
            if enemies
            else int(config["peace_shield_target"])
        )
        want_repair = core.shield < min(effective_cap, shield_target)
        if want_repair:
            core.repair_shield()
            core_action_name = "REPAIR_SHIELD"
            core_done = True

    # ── No auto-spawn ── manual control only ─────────────────────────────
    if not core_done and config["core_movement_enabled"]:
        # Stop if a cargo worker is close, otherwise move toward them
        close_cargo = any(
            w.cargo > 0
            and _manhattan(w.position, core_pos) <= int(config["cargo_wait_distance"])
            for w in turn.workers
        )
        if not close_cargo:
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
            # Try each direction (obstacle-aware)
            for d in dirs:
                nx, ny = core_pos[0] + d.delta[0], core_pos[1] + d.delta[1]
                if (nx, ny) not in obstacle_cells:
                    core.start_move(d)
                    core_action_name = f"MOVE_{d.name}"
                    core_done = True
                    break

    if not core_done:
        core.wait()
        core_action_name = "WAIT"

    # ── Unit actions ────────────────────────────────────────────────────
    for unit in turn.units:
        uid = str(unit.id)[:8]
        if unit.unit_type == UnitType.WORKER:
            action, detail = _plan_worker(
                unit, core,
                resource_cells=resource_cells,
                obstacle_cells=obstacle_cells,
                depleted=depleted,
                config=config,
            )
            unit_actions_detail[uid] = f"{action}:{detail}"
        elif unit.unit_type == UnitType.VANGUARD:
            action, detail = _plan_vanguard(unit, enemies, obstacle_cells, config)
            unit_actions_detail[uid] = f"{action}:{detail}"
        elif unit.unit_type == UnitType.RANGER:
            action, detail = _plan_ranger(unit, enemies, obstacle_cells, config)
            unit_actions_detail[uid] = f"{action}:{detail}"

    return core_action_name, unit_actions_detail


# ── live loop ────────────────────────────────────────────────────────────────

def play(api_key: str, log_path: str = "tactic_log.jsonl") -> None:
    _load_map_memory()
    logger = TacticLogger(log_path)
    logger.open()
    print(f"[tactic] logging to {log_path}", flush=True)
    print(
        f"[map] obstacles={len(_obstacle_memory)} resources={len(_resource_memory)}",
        flush=True,
    )

    try:
        with ArenaHeroClient(api_key=api_key) as game:
            for turn in game.turns():
                tick_start = time.monotonic()
                core_action, unit_actions = choose_actions(turn)
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
                    print(f"tick={turn.tick} submit_error={e}", flush=True)
    except KeyboardInterrupt:
        print("\n[tactic] stopped by user", flush=True)
    finally:
        _save_map_memory(force=True)
        logger.close()
        _print_summary(log_path)


def _print_summary(log_path: str) -> None:
    """Quick summary from the log file."""
    try:
        ticks = []
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and "tick" in record:
                    ticks.append(record)
        if not ticks:
            return

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
    except Exception:
        pass


if __name__ == "__main__":
    api_key = os.environ.get("ARENA_HERO_API_KEY") or getpass("Arena Hero API key: ")
    play(api_key)
