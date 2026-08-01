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
from collections.abc import Iterator
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


# ── geometry helpers ─────────────────────────────────────────────────────────

def _manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


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


# ── decision logger ──────────────────────────────────────────────────────────

@dataclass
class TickRecord:
    tick: int
    timestamp: str = ""
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
    visible_enemies: int = 0
    resource_cells_visible: int = 0
    resource_cells: list[list[int]] = field(default_factory=list)
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
            "_version": 1,
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
        rec.beacon_pos = list(turn.beacon.position) if turn.beacon.position else None
        rec.beacon_status = turn.beacon.status.name if turn.beacon.status else None
        rec.core_action = core_action
        rec.plan_unit_actions = unit_actions or {}
        rec.accepted = accepted
        rec.latency_ms = round(latency_ms, 1)

        if core:
            rec.core_pos = list(core.position)
            rec.core_hp = core.hp
            rec.core_shield = core.shield
            rec.core_state = core.view.state.value if hasattr(core.view.state, "value") else str(core.view.state)

        for w in turn.workers:
            rec.workers.append({
                "id": str(w.id)[:8],
                "pos": list(w.position),
                "cargo": w.cargo,
                "hp": w.hp,
            })

        for v in turn.vanguards:
            rec.vanguards.append({
                "id": str(v.id)[:8],
                "pos": list(v.position),
                "hp": v.hp,
            })

        for r in turn.rangers:
            rec.rangers.append({
                "id": str(r.id)[:8],
                "pos": list(r.position),
                "hp": r.hp,
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
) -> tuple[str, str]:
    """Return (action_name, detail)."""
    pos = worker.position

    if worker.cargo and pos == core.position and turn_context.resource_space > 0:
        worker.deposit()
        return ("DEPOSIT", f"at_core cargo={worker.cargo}")

    if pos in resource_cells and pos not in depleted and not worker.cargo:
        worker.harvest()
        return ("HARVEST", f"on_resource {pos}")

    goal: tuple[int, int] | None = None
    if worker.cargo:
        goal = core.position
    elif resource_cells:
        candidates = [c for c in resource_cells if c not in depleted]
        if candidates:
            goal = min(candidates, key=lambda p: _manhattan(pos, p))
    # Fallback: use remembered resource coordinates
    if goal is None and not worker.cargo:
        remembered = [p for p in _resource_memory if p not in depleted]
        if remembered:
            goal = min(remembered, key=lambda p: _manhattan(pos, p))

    # Move toward goal (try all 4 dirs, sorted by proximity to goal)
    if goal is not None and goal != pos:
        direction = _step_towards(pos, goal)
        dirs_try = [direction] if direction else []
        # Add remaining directions sorted by how much they reduce distance to goal
        remaining = [d for d in [Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT]
                     if d not in dirs_try]
        remaining.sort(key=lambda d: _manhattan((pos[0]+d.delta[0], pos[1]+d.delta[1]), goal))
        for d in dirs_try + remaining:
            if d is None:
                continue
            nx, ny = pos[0] + d.delta[0], pos[1] + d.delta[1]
            if (nx, ny) not in obstacle_cells:
                worker.move(d)
                return ("MOVE", f"{d.name} -> {goal}")
        # All blocked, fall through to explore
        goal = None

    # No goal at all: fan out in unique direction based on worker ID
    if goal is None and not worker.cargo:
        # Use UUID hash to pick a unique exploration direction
        idx = hash(str(worker.id)) % 4
        dirs = [Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT]
        preferred = dirs[idx]
        nx, ny = pos[0] + preferred.delta[0], pos[1] + preferred.delta[1]
        if (nx, ny) not in obstacle_cells:
            worker.move(preferred)
            return ("MOVE", f"{preferred.name} explore")
        # Fallback: try other directions
        for d in dirs:
            nx, ny = pos[0] + d.delta[0], pos[1] + d.delta[1]
            if (nx, ny) not in obstacle_cells:
                worker.move(d)
                return ("MOVE", f"{d.name} explore")

    if goal is not None and goal != pos:
        direction = _step_towards(pos, goal)
        if direction is not None:
            nx = pos[0] + direction.delta[0]
            ny = pos[1] + direction.delta[1]
            if (nx, ny) not in obstacle_cells:
                worker.move(direction)
                return ("MOVE", f"{direction.name} -> {goal}")

    worker.wait()
    return ("WAIT", "no_action")


def _plan_vanguard(
    vanguard,
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
) -> tuple[str, str]:
    """Return (action_name, detail)."""
    pos = vanguard.position

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

    # No enemies: scout unexplored area (up-priority, different from workers' right-up)
    for d in (Direction.UP, Direction.RIGHT, Direction.LEFT, Direction.DOWN):
        nx, ny = pos[0] + d.delta[0], pos[1] + d.delta[1]
        if (nx, ny) not in obstacle_cells:
            vanguard.move(d)
            return ("MOVE", f"{d.name} scout")

    vanguard.wait()
    return ("WAIT", "no_way")


def _plan_ranger(
    ranger,
    enemies: tuple,
    obstacle_cells: frozenset[tuple[int, int]],
) -> tuple[str, str]:
    """Return (action_name, detail)."""
    pos = ranger.position

    # 1. Shoot nearest in-range enemy with clear LOS
    best_dist = 10_000
    best_target = None

    for enemy in enemies:
        dist = _manhattan(pos, enemy.position)
        if not (1 <= dist <= 3):
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
    if enemies:
        nearest = min(enemies, key=lambda e: _manhattan(pos, e.position))
        direction = _step_towards(pos, nearest.position)
        if direction is not None:
            nx, ny = pos[0] + direction.delta[0], pos[1] + direction.delta[1]
            if (nx, ny) not in obstacle_cells:
                ranger.move(direction)
                return ("MOVE", f"{direction.name} -> enemy at {nearest.position}")

    # 3. No enemies: scout in unique direction (based on UUID hash)
    idx = hash(str(ranger.id)) % 4
    dirs = [Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT]
    preferred = dirs[idx]
    nx, ny = pos[0] + preferred.delta[0], pos[1] + preferred.delta[1]
    if (nx, ny) not in obstacle_cells:
        ranger.move(preferred)
        return ("MOVE", f"{preferred.name} scout")
    for d in dirs:
        nx, ny = pos[0] + d.delta[0], pos[1] + d.delta[1]
        if (nx, ny) not in obstacle_cells:
            ranger.move(d)
            return ("MOVE", f"{d.name} scout")

    ranger.wait()
    return ("WAIT", "no_way")


# ── top-level tactic ─────────────────────────────────────────────────────────

# ── shared resource memory (persists across ticks) ────────────────────────
_resource_memory: set[tuple[int, int]] = set()


def _update_resource_memory(turn) -> None:
    """Remember visible resources, remove harvested ones."""
    global _resource_memory
    # Add newly visible resources
    for p in turn.resource_cells:
        _resource_memory.add(p)
    # Remove resources that were successfully harvested
    for event in turn.events:
        if event.event_type == "HARVEST_SUCCEEDED" and event.position:
            _resource_memory.discard(event.position)
        if event.event_type == "HARVEST_FAILED" and event.reason_code == "RESOURCE_DEPLETED" and event.position:
            _resource_memory.discard(event.position)


# Context holder for the current turn's resource_space (set by choose_actions)
turn_context = type("_Ctx", (), {"resource_space": 0})()


def choose_actions(turn) -> tuple[str, dict[str, str]]:
    """Queue actions, return (core_action_name, {unit_id: action_detail})."""
    unit_actions_detail: dict[str, str] = {}
    core_action_name = "WAIT"

    # ── Update resource memory ─────────────────────────────────────────
    _update_resource_memory(turn)

    # ── Lifecycle guard ─────────────────────────────────────────────────
    if turn.core is None:
        return ("RESPAWN", {})

    core = turn.core
    core_pos = core.position
    resources = turn.resources
    turn_context.resource_space = turn.resource_space

    resource_cells: frozenset[tuple[int, int]] = turn.resource_cells
    obstacle_cells: frozenset[tuple[int, int]] = turn.obstacle_cells
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

    # ── Core action ─────────────────────────────────────────────────────
    core_done = False

    if beacon_on_ground_here:
        core.pickup_beacon()
        core_action_name = "PICKUP_BEACON"
        core_done = True

    if not core_done and resources >= 1:
        effective_cap = 10 if beacon_carried_by_core else 5
        want_repair = core.shield < min(effective_cap, 3 if enemies else effective_cap)
        if want_repair:
            core.repair_shield()
            core_action_name = "REPAIR_SHIELD"
            core_done = True

    # ── No auto-spawn ── manual control only ─────────────────────────────
    if not core_done:
        # Stop if a cargo worker is close (5 cells), otherwise move toward them
        close_cargo = any(
            w.cargo > 0 and _manhattan(w.position, core_pos) <= 5
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
            all_res = list(turn.resource_cells) + [p for p in _resource_memory if p not in depleted]
            if all_res:
                res_target = min(all_res, key=lambda p: _manhattan(core_pos, p))
            # Choose target: nearest resource or worker center, whichever is closer
            if res_target and _manhattan(core_pos, res_target) < _manhattan(core_pos, (avg_x, avg_y)):
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
            )
            unit_actions_detail[uid] = f"{action}:{detail}"
        elif unit.unit_type == UnitType.VANGUARD:
            action, detail = _plan_vanguard(unit, enemies, obstacle_cells)
            unit_actions_detail[uid] = f"{action}:{detail}"
        elif unit.unit_type == UnitType.RANGER:
            action, detail = _plan_ranger(unit, enemies, obstacle_cells)
            unit_actions_detail[uid] = f"{action}:{detail}"

    return core_action_name, unit_actions_detail


# ── live loop ────────────────────────────────────────────────────────────────

def play(api_key: str, log_path: str = "tactic_log.jsonl") -> None:
    logger = TacticLogger(log_path)
    logger.open()
    print(f"[tactic] logging to {log_path}", flush=True)

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
                        f"latency={latency:.0f}ms",
                        flush=True,
                    )
                except Exception as e:
                    print(f"tick={turn.tick} submit_error={e}", flush=True)
    except KeyboardInterrupt:
        print("\n[tactic] stopped by user", flush=True)
    finally:
        logger.close()
        _print_summary(log_path)


def _print_summary(log_path: str) -> None:
    """Quick summary from the log file."""
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip() and not l.startswith("{")]

        ticks = [l for l in lines if "tick" in l and "_meta" not in l and "_summary" not in l]
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