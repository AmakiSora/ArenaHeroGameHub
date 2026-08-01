"""
Direct-play wrapper for Arena Hero
====================================
Subprocess bridge: spawns direct_session.py, reads NDJSON, computes plans,
writes control lines back.  Auto-restarts on bridge crash.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from queue import Queue
from typing import Any

# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _step_towards(
    src: tuple[int, int], dst: tuple[int, int],
) -> dict[str, Any] | None:
    dx = dst[0] - src[0]
    dy = dst[1] - src[1]
    if dx == 0 and dy == 0:
        return None
    if abs(dx) >= abs(dy):
        direction = "RIGHT" if dx > 0 else "LEFT"
    else:
        direction = "DOWN" if dy > 0 else "UP"
    return {"type": "MOVE", "direction": direction}


def _dirs_toward(
    src: tuple[int, int], dst: tuple[int, int],
) -> list[str]:
    """Return direction names ordered by preference toward dst (left-biased)."""
    dx = dst[0] - src[0]
    dy = dst[1] - src[1]
    dirs: list[str] = []
    if dx < 0:
        dirs.append("LEFT")
    elif dx > 0:
        dirs.append("RIGHT")
    if dy < 0:
        dirs.append("UP")
    elif dy > 0:
        dirs.append("DOWN")
    for d in ["LEFT", "RIGHT", "UP", "DOWN"]:
        if d not in dirs:
            dirs.append(d)
    return dirs


def _line_blocked(
    a: tuple[int, int], b: tuple[int, int],
    obstacles: set[tuple[int, int]],
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


# ---------------------------------------------------------------------------
# BFS pathfinding
# ---------------------------------------------------------------------------

def _enemy_positions(enemies: list) -> set[tuple[int, int]]:
    return {tuple(e["position"]) for e in enemies if e.get("position")}


def _is_closest_worker(
    worker_pos: tuple[int, int],
    resources: set[tuple[int, int]],
    all_controlled: list[dict],
) -> bool:
    worker_positions = [
        tuple(u["position"])
        for u in all_controlled
        if u.get("unit_type") == "WORKER" and u.get("position")
    ]
    for res in resources:
        dist_to_this = _manhattan(worker_pos, res)
        for wpos in worker_positions:
            if wpos == worker_pos:
                continue
            if _manhattan(wpos, res) < dist_to_this:
                break
        else:
            return True
    return False


def _bfs_next_step(
    start: tuple[int, int],
    goal: tuple[int, int],
    obstacles: set[tuple[int, int]],
    max_steps: int = 200,
) -> dict[str, Any] | None:
    """Return the first MOVE action along a shortest obstacle-avoiding path."""
    from collections import deque

    if start == goal:
        return None

    start_x, _ = start
    goal_x, _ = goal
    # Prefer left-biased direction order when goal is to the left
    if goal_x < start_x:
        dirs = ["LEFT", "UP", "DOWN", "RIGHT"]
    elif goal_x > start_x:
        dirs = ["RIGHT", "UP", "DOWN", "LEFT"]
    else:
        dirs = ["UP", "DOWN", "LEFT", "RIGHT"]

    queue: deque[tuple[tuple[int, int], dict[str, Any] | None]] = deque()
    queue.append((start, None))
    visited = {start}
    steps = 0

    while queue and steps < max_steps:
        steps += 1
        (x, y), first_action = queue.popleft()
        for d in dirs:
            dx, dy = DirectionDelta[d]
            nx, ny = x + dx, y + dy
            if (nx, ny) in visited or (nx, ny) in obstacles:
                continue
            visited.add((nx, ny))
            action = first_action if first_action is not None else {"type": "MOVE", "direction": d}
            if (nx, ny) == goal:
                return action
            queue.append(((nx, ny), action))
    return None


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def decide_plan(tick: int, state: dict[str, Any]) -> dict[str, Any]:
    """Return a complete CommandPlan dict for the current state."""

    objects: list[dict[str, Any]] = state.get("objects", [])

    # Core
    core_obj = next((o for o in objects if o.get("kind") == "CORE" and o.get("controlled")), None)
    if core_obj is None:
        return {"tick": tick, "unit_actions": {}, "core_action": None}

    core_pos: tuple[int, int] = tuple(core_obj["position"])
    core_shield: int = core_obj["shield"]
    resources: int = state["resources"]
    core_state_str: str = core_obj.get("state", "NORMAL")

    beacon = state["champion_beacon"]
    beacon_pos: tuple[int, int] = tuple(beacon["position"])
    beacon_status: str | None = beacon.get("status")
    beacon_carrier_id = beacon.get("carrier_id")

    beacon_on_ground_here = beacon_status == "GROUND" and beacon_pos == core_pos
    beacon_carried_by_core = beacon_status == "CARRIED" and str(beacon_carrier_id) == str(core_obj["id"])

    # Units
    controlled_units = [o for o in objects if o.get("kind") == "UNIT" and o.get("controlled")]
    workers = [u for u in controlled_units if u["unit_type"] == "WORKER"]
    vanguards = [u for u in controlled_units if u["unit_type"] == "VANGUARD"]
    rangers = [u for u in controlled_units if u["unit_type"] == "RANGER"]
    enemies = [o for o in objects if o.get("kind") in ("UNIT", "CORE") and not o.get("controlled")]

    # Terrain (mixed in objects with kind OBSTACLE / RESOURCE)
    obstacle_set: set[tuple[int, int]] = set()
    resource_set: set[tuple[int, int]] = set()
    for obj in state.get("objects", []):
        kind = obj.get("kind")
        if kind not in ("OBSTACLE", "RESOURCE"):
            continue
        for pos in obj.get("positions", []):
            p = tuple(pos)
            if kind == "OBSTACLE":
                obstacle_set.add(p)
            else:
                resource_set.add(p)

    # Depleted from previous events
    depleted: set[tuple[int, int]] = set()
    for event in state.get("events", []):
        if (
            event.get("event_type") == "HARVEST_FAILED"
            and event.get("reason_code") == "RESOURCE_DEPLETED"
            and event.get("position")
        ):
            depleted.add(tuple(event["position"]))

    available_resources = resource_set - depleted

    # -- Core action --
    core_action: dict[str, Any] | None = None

    cell_occupants = sum(
        1
        for o in objects
        if o.get("position") is not None
        and tuple(o["position"]) == core_pos
        and o.get("controlled", False)
    )
    cell_free = 2 - cell_occupants
    n_workers = len(workers)
    n_vanguards = len(vanguards)
    n_rangers = len(rangers)

    if beacon_on_ground_here:
        core_action = {"type": "PICKUP_BEACON"}
    elif core_shield < 5 and resources >= 1:
        core_action = {"type": "REPAIR_SHIELD"}
    elif cell_free >= 1:
        if n_workers < 4 and resources >= 5:
            core_action = {"type": "SPAWN", "unit_type": "WORKER"}
        elif enemies and n_vanguards < 2 and resources >= 10:
            core_action = {"type": "SPAWN", "unit_type": "VANGUARD"}
        elif (
            enemies
            and n_workers >= 4
            and n_vanguards >= 1
            and n_rangers < 2
            and resources >= 12
        ):
            core_action = {"type": "SPAWN", "unit_type": "RANGER"}

    # Stop core if any worker has cargo (let them deposit first)
    if core_action is None:
        any_cargo = any(
            u.get("cargo", 0) > 0 for u in controlled_units
            if u.get("unit_type") == "WORKER"
        )
        if not any_cargo and not enemies and core_state_str != "MOVING":
            for direction in ["RIGHT", "UP", "DOWN"]:
                dx, dy = DirectionDelta[direction]
                dest_x = core_pos[0] + dx
                dest_y = core_pos[1] + dy
                dest_occupied = any(
                    tuple(o.get("position", [])) == (dest_x, dest_y)
                    for o in objects
                    if o.get("position") is not None
                )
                if not dest_occupied and (dest_x, dest_y) not in obstacle_set:
                    core_action = {"type": "START_MOVE", "direction": direction}
                    break
        # If all blocked, just wait for next tick

    if core_action is None:
        core_action = {"type": "WAIT"}

    # -- Unit actions --
    unit_actions: dict[str, dict[str, Any]] = {}

    for unit in controlled_units:
        uid = str(unit["id"])
        ux, uy = unit["position"]
        u_type = unit["unit_type"]

        if u_type == "WORKER":
            cargo = unit.get("cargo") or 0
            action: dict[str, Any] | None = None

            # 1) Cargo -> deposit at core
            if cargo and (ux, uy) != core_pos:
                blocked = obstacle_set | _enemy_positions(enemies)
                step = _bfs_next_step((ux, uy), core_pos, blocked, max_steps=500)
                if step is not None:
                    action = step
                else:
                    for d in _dirs_toward((ux, uy), core_pos):
                        nx = ux + DirectionDelta[d][0]
                        ny = uy + DirectionDelta[d][1]
                        if (nx, ny) not in obstacle_set:
                            action = {"type": "MOVE", "direction": d}
                            break

            # 2) At core with cargo -> deposit (only if Core not migrating)
            if action is None and cargo and (ux, uy) == core_pos:
                if core_state_str == "MOVING":
                    action = {"type": "WAIT"}
                else:
                    capacity = max(10, state.get("population", 0) * 5)
                    if resources < capacity:
                        action = {"type": "DEPOSIT"}
                    else:
                        action = {"type": "WAIT"}

            # 3) Standing on resource -> harvest
            if action is None and (ux, uy) in available_resources and not cargo:
                action = {"type": "HARVEST"}

            # 4) No cargo, resource visible -> closest worker goes, others explore
            if action is None and not cargo and available_resources:
                if _is_closest_worker((ux, uy), available_resources, controlled_units):
                    goal = min(available_resources, key=lambda p: _manhattan((ux, uy), p))
                    blocked = obstacle_set | _enemy_positions(enemies)
                    step = _bfs_next_step((ux, uy), goal, blocked, max_steps=500)
                    if step is not None:
                        action = step
                    else:
                        for d in _dirs_toward((ux, uy), goal):
                            nx = ux + DirectionDelta[d][0]
                            ny = uy + DirectionDelta[d][1]
                            if (nx, ny) not in blocked:
                                action = {"type": "MOVE", "direction": d}
                                break

            # 5) No cargo, no resource visible -> explore right-up (avoid enemies)
            if action is None and not cargo:
                goal = (ux + 8, uy - 8)
                blocked = obstacle_set | _enemy_positions(enemies)
                step = _bfs_next_step((ux, uy), goal, blocked, max_steps=200)
                if step is not None:
                    action = step
                else:
                    for d in ("RIGHT", "UP", "DOWN", "LEFT"):
                        nx = ux + DirectionDelta[d][0]
                        ny = uy + DirectionDelta[d][1]
                        if (nx, ny) not in obstacle_set:
                            action = {"type": "MOVE", "direction": d}
                            break

            if action is None:
                action = {"type": "WAIT"}
            unit_actions[uid] = action

        elif u_type == "VANGUARD":
            action = None
            for d, (dx, dy) in DirectionDelta.items():
                tx, ty = ux + dx, uy + dy
                for enemy in enemies:
                    if tuple(enemy["position"]) == (tx, ty):
                        action = {"type": "SWEEP", "direction": d}
                        break
                if action is not None:
                    break

            if action is None and enemies:
                nearest = min(enemies, key=lambda e: _manhattan((ux, uy), tuple(e["position"])))
                step = _step_towards((ux, uy), tuple(nearest["position"]))
                if step is not None:
                    nx = ux + DirectionDelta[step["direction"]][0]
                    ny = uy + DirectionDelta[step["direction"]][1]
                    if (nx, ny) not in obstacle_set:
                        action = step

            # No enemies: explore right-up
            if action is None:
                goal = (ux + 8, uy - 8)
                step = _bfs_next_step((ux, uy), goal, obstacle_set, max_steps=200)
                if step is not None:
                    action = step
                else:
                    for d in ("RIGHT", "UP", "DOWN", "LEFT"):
                        nx = ux + DirectionDelta[d][0]
                        ny = uy + DirectionDelta[d][1]
                        if (nx, ny) not in obstacle_set:
                            action = {"type": "MOVE", "direction": d}
                            break

            if action is None:
                action = {"type": "WAIT"}
            unit_actions[uid] = action

        elif u_type == "RANGER":
            action = None
            best_dist = 10_000
            best_enemy = None
            for enemy in enemies:
                dist = _manhattan((ux, uy), tuple(enemy["position"]))
                if not (1 <= dist <= 3):
                    continue
                if _line_blocked((ux, uy), tuple(enemy["position"]), obstacle_set):
                    continue
                if dist < best_dist:
                    best_dist = dist
                    best_enemy = enemy

            if best_enemy is not None:
                action = {
                    "type": "SHOOT",
                    "target_id": str(best_enemy["id"]),
                    "expected_cell": list(best_enemy["position"]),
                }
            else:
                action = {"type": "WAIT"}
            unit_actions[uid] = action

    return {"tick": tick, "unit_actions": unit_actions, "core_action": core_action}


# ---------------------------------------------------------------------------
# Bridge I/O
# ---------------------------------------------------------------------------

DirectionDelta: dict[str, tuple[int, int]] = {
    "UP": (0, -1),
    "DOWN": (0, 1),
    "LEFT": (-1, 0),
    "RIGHT": (1, 0),
}


def read_json_lines(stream, out: Queue[dict[str, Any] | None]) -> None:
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            out.put(json.loads(line))
        except json.JSONDecodeError:
            out.put(None)
    out.put(None)


def write_control(stream, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, separators=(",", ":")) + "\n")
    stream.flush()


def _dump_debug(tick, state, plan=None):
    try:
        workers = []
        for o in state.get("objects", []):
            if o.get("kind") == "UNIT" and o.get("unit_type") == "WORKER" and o.get("controlled"):
                workers.append({"pos": o.get("position"), "cargo": o.get("cargo", 0)})
        resources = []
        for t in state.get("terrain", []):
            if t.get("kind") == "RESOURCE":
                resources.extend([p for p in t.get("positions", [])])
        out = {
            "tick": tick,
            "terrain": state.get("terrain", []),
            "resources": resources[:30],
            "workers": workers,
        }
        if plan:
            out["plan"] = {
                "unit_actions": {k: v for k, v in plan.get("unit_actions", {}).items()},
                "core_action": plan.get("core_action"),
            }
        with open("direct_debug.json", "w", encoding="utf-8") as f:
            json.dump(out, f)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_direct_wrapper(
    *,
    api_key: str,
    decision_timeout: float = 8.0,
) -> int:
    env = os.environ.copy()
    env["ARENA_HERO_API_KEY"] = api_key

    proc = subprocess.Popen(
        [sys.executable, r"C:\cosmos\github\ArenaGame\.pi\skills\arena-hero-skill\scripts\direct_session.py"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        bufsize=1,
        text=True,
    )

    events: Queue[dict[str, Any] | None] = Queue()
    reader = threading.Thread(target=read_json_lines, args=(proc.stdout, events), daemon=True)
    reader.start()

    stderr_lines: list[str] = []
    def _drain_stderr() -> None:
        try:
            for line in proc.stderr:
                line = line.rstrip()
                stderr_lines.append(line)
                print(f"[bridge stderr] {line}", flush=True)
        except Exception:
            pass
    threading.Thread(target=_drain_stderr, daemon=True).start()

    print(f"[wrapper] bridge PID={proc.pid}  waiting for events ...", flush=True)

    current_tick: int | None = None
    current_state: dict[str, Any] | None = None
    exit_code = 0

    while True:
        raw = events.get()
        if raw is None:
            rc = proc.returncode
            if rc is not None:
                print(f"[wrapper] bridge exited early code={rc}", flush=True)
                for line in stderr_lines[-20:]:
                    print(f"[bridge stderr] {line}", flush=True)
            break

        etype = raw.get("type")

        if etype == "ready":
            print(f"[wrapper] ready  viewer={raw['viewer_url']}", flush=True)

        elif etype == "tick":
            current_tick = raw["tick"]
            print(f"[wrapper] tick={current_tick}", flush=True)

        elif etype == "turn":
            current_tick = raw["tick"]
            current_state = raw["state"]
            print(f"[wrapper] turn={current_tick}  deciding ...", flush=True)

            try:
                plan = decide_plan(raw["tick"], current_state)
                control = {"type": "submit", "plan": plan}
                _dump_debug(current_tick, current_state, plan)
                write_control(proc.stdin, control)
                print(f"[wrapper] submitted plan for tick={current_tick}", flush=True)
            except Exception as exc:
                print(f"[wrapper] decision error: {exc}", flush=True)
                write_control(proc.stdin, {"type": "skip", "tick": current_tick})
                print(f"[wrapper] skipped tick={current_tick}", flush=True)

        elif etype == "accepted":
            print(
                f"[wrapper] accepted tick={raw['acknowledgement']['tick']} "
                f"source={raw['acknowledgement']['source']}",
                flush=True,
            )

        elif etype == "received":
            print(
                f"[wrapper] received tick={raw['receipt']['tick']} "
                f"source={raw['receipt']['source']}",
                flush=True,
            )

        elif etype == "missed":
            print(f"[wrapper] MISSED tick={raw['tick']} reason={raw['reason']}", flush=True)

        elif etype == "skipped":
            print(f"[wrapper] skipped tick={raw['tick']}", flush=True)

        elif etype == "submit_error":
            print(
                f"[wrapper] submit_error tick={raw['tick']} "
                f"{raw['error']}: {raw['message']}",
                flush=True,
            )
            if raw.get("status_code") in (401, 403):
                exit_code = 1
                break

        elif etype == "input_error":
            print(f"[wrapper] input_error tick={raw['tick']}: {raw['message']}", flush=True)

        elif etype == "error":
            print(f"[wrapper] error {raw['error']}: {raw['message']}", flush=True)

        elif etype == "stopped":
            print(f"[wrapper] stopped reason={raw['reason']}", flush=True)
            break

        else:
            print(f"[wrapper] unknown event: {raw}", flush=True)

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    print(f"[wrapper] bridge exited code={proc.returncode}", flush=True)
    return exit_code


def _api_key_from_env_file(path) -> str | None:
    from pathlib import Path
    path = Path(path)
    if not path.is_file():
        return None
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        name, separator, value = line.partition("=")
        if separator and name.strip() == "ARENA_HERO_API_KEY":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            return value or None
    return None


def load_api_key() -> str:
    key = os.environ.get("ARENA_HERO_API_KEY", "").strip()
    if key:
        return key
    key = _api_key_from_env_file(".env")
    if key:
        return key
    sys.exit("Set ARENA_HERO_API_KEY or write it to .env before running this wrapper.")


def _auto_restart(*, api_key: str, max_retries: int = 999) -> int:
    for attempt in range(1, max_retries + 1):
        print(f"[main] starting wrapper attempt={attempt}", flush=True)
        try:
            rc = run_direct_wrapper(api_key=api_key)
            if rc != 0:
                print(f"[main] wrapper exited code={rc}  not retrying", flush=True)
                return rc
        except Exception as exc:
            print(f"[main] wrapper crashed: {exc}", flush=True)
        print(f"[main] restarting in 2s...", flush=True)
        time.sleep(2)
    print("[main] max retries reached", flush=True)
    return 1


if __name__ == "__main__":
    key = load_api_key()
    sys.exit(_auto_restart(api_key=key))
