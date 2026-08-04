from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arena_hero import Direction, UnitType

import dashboard
import game_stats
import status
import tactic
import tactic_config
from tactic_config import default_config


class ResourceMergeTests(unittest.TestCase):
    def test_visible_and_remembered_resources_are_deduplicated(self) -> None:
        resources = tactic._merge_resource_cells(
            visible=[(2, 3), (4, 5)],
            remembered={(2, 3), (6, 7)},
            depleted={(4, 5)},
        )

        self.assertEqual(resources, [(2, 3), (6, 7)])

    def test_forgotten_manual_resource_is_not_restored_on_save(self) -> None:
        stale_resource = (1, 1)
        active_resource = (2, 2)
        original_memory = set(tactic._resource_memory)
        original_tombstones = set(tactic._resource_tombstones)
        original_dirty = tactic._map_dirty
        original_sig = tactic._last_dashboard_map_sig
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                memory_path = Path(temp_dir) / "map_memory.json"
                memory_path.write_text(json.dumps({
                    "obstacles": [],
                    "resources": [list(stale_resource), list(active_resource)],
                    "manual_resources": [list(stale_resource)],
                }), encoding="utf-8")
                tactic._resource_memory.clear()
                tactic._resource_memory.update({stale_resource, active_resource})
                tactic._resource_tombstones.clear()
                tactic._last_dashboard_map_sig = None

                with patch.object(tactic, "MAP_MEMORY_PATH", memory_path):
                    tactic._forget_resource(stale_resource)
                    tactic._save_map_memory(force=True)

                saved = json.loads(memory_path.read_text(encoding="utf-8"))
        finally:
            tactic._resource_memory.clear()
            tactic._resource_memory.update(original_memory)
            tactic._resource_tombstones.clear()
            tactic._resource_tombstones.update(original_tombstones)
            tactic._map_dirty = original_dirty
            tactic._last_dashboard_map_sig = original_sig

        self.assertEqual(saved["resources"], [list(active_resource)])
        self.assertEqual(saved["manual_resources"], [])
        self.assertEqual(saved["forgotten_resources"], [list(stale_resource)])


class DashboardMapMemoryTests(unittest.TestCase):
    def test_remove_resource_writes_sticky_forgotten_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "map_memory.json"
            memory_path.write_text(json.dumps({
                "obstacles": [],
                "resources": [[10, 10], [20, 20]],
                "manual_resources": [[10, 10]],
                "enemy_sightings": [],
            }), encoding="utf-8")
            with patch.object(dashboard, "MAP_FILE", str(memory_path)):
                result = dashboard.remove_manual_resource(10, 10)
                loaded = dashboard.load_map_memory()
                raw = json.loads(memory_path.read_text(encoding="utf-8"))

        self.assertTrue(result["ok"])
        self.assertEqual(loaded["resources"], [(20, 20)])
        self.assertEqual(raw["forgotten_resources"], [[10, 10]])
        self.assertEqual(raw["manual_resources"], [])

    def test_clear_remembered_resources_tombstones_everything(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "map_memory.json"
            memory_path.write_text(json.dumps({
                "obstacles": [[1, 1]],
                "resources": [[3, 3], [4, 4]],
                "manual_resources": [[4, 4]],
                "enemy_sightings": [[9, 9]],
            }), encoding="utf-8")
            with patch.object(dashboard, "MAP_FILE", str(memory_path)):
                result = dashboard.clear_remembered_resources()
                loaded = dashboard.load_map_memory()
                raw = json.loads(memory_path.read_text(encoding="utf-8"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["resource_count"], 0)
        self.assertEqual(loaded["resources"], [])
        self.assertEqual(sorted(raw["forgotten_resources"]), [[3, 3], [4, 4]])
        self.assertEqual(raw["enemy_sightings"], [[9, 9]])
        self.assertEqual(raw["obstacles"], [[1, 1]])

    def test_dashboard_clear_is_not_restored_by_tactic_save(self) -> None:
        stale_resource = (7, 7)
        keep_resource = (8, 8)
        original_memory = set(tactic._resource_memory)
        original_tombstones = set(tactic._resource_tombstones)
        original_dirty = tactic._map_dirty
        original_sig = tactic._last_dashboard_map_sig
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                memory_path = Path(temp_dir) / "map_memory.json"
                memory_path.write_text(json.dumps({
                    "obstacles": [],
                    "resources": [list(stale_resource), list(keep_resource)],
                    "manual_resources": [],
                    "enemy_sightings": [],
                }), encoding="utf-8")

                tactic._resource_memory.clear()
                tactic._resource_memory.update({stale_resource, keep_resource})
                tactic._resource_tombstones.clear()
                tactic._last_dashboard_map_sig = None
                tactic._map_dirty = True

                with patch.object(dashboard, "MAP_FILE", str(memory_path)), \
                     patch.object(tactic, "MAP_MEMORY_PATH", memory_path):
                    # Simulate the user clicking clear on the dashboard while
                    # the tactic process still holds the old RAM memory.
                    dashboard.clear_remembered_resources()
                    tactic._save_map_memory(force=True)
                    saved = json.loads(memory_path.read_text(encoding="utf-8"))
                    in_memory = set(tactic._resource_memory)
        finally:
            tactic._resource_memory.clear()
            tactic._resource_memory.update(original_memory)
            tactic._resource_tombstones.clear()
            tactic._resource_tombstones.update(original_tombstones)
            tactic._map_dirty = original_dirty
            tactic._last_dashboard_map_sig = original_sig

        self.assertEqual(saved["resources"], [])
        self.assertEqual(in_memory, set())
        self.assertIn(list(stale_resource), saved["forgotten_resources"])
        self.assertIn(list(keep_resource), saved["forgotten_resources"])

    def test_live_rediscovery_clears_forgotten_entry(self) -> None:
        pos = (5, 5)
        original_memory = set(tactic._resource_memory)
        original_tombstones = set(tactic._resource_tombstones)
        original_dirty = tactic._map_dirty
        original_sig = tactic._last_dashboard_map_sig
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                memory_path = Path(temp_dir) / "map_memory.json"
                memory_path.write_text(json.dumps({
                    "obstacles": [],
                    "resources": [],
                    "manual_resources": [],
                    "forgotten_resources": [list(pos)],
                    "enemy_sightings": [],
                }), encoding="utf-8")
                tactic._resource_memory.clear()
                tactic._resource_tombstones.clear()
                tactic._resource_tombstones.add(pos)
                tactic._last_dashboard_map_sig = None

                turn = SimpleNamespace(
                    resource_cells=frozenset({pos}),
                    events=(),
                )
                with patch.object(tactic, "MAP_MEMORY_PATH", memory_path):
                    tactic._apply_dashboard_map_edits()
                    # After absorbing the clear, a live sighting relearns it.
                    tactic._update_resource_memory(turn)
                    tactic._map_dirty = True
                    tactic._save_map_memory(force=True)
                    saved = json.loads(memory_path.read_text(encoding="utf-8"))
        finally:
            tactic._resource_memory.clear()
            tactic._resource_memory.update(original_memory)
            tactic._resource_tombstones.clear()
            tactic._resource_tombstones.update(original_tombstones)
            tactic._map_dirty = original_dirty
            tactic._last_dashboard_map_sig = original_sig

        self.assertEqual(saved["resources"], [list(pos)])
        self.assertEqual(saved["forgotten_resources"], [])


class ConfiguredPlannerTests(unittest.TestCase):
    def test_worker_forgets_empty_remembered_target_and_explores(self) -> None:
        class Worker:
            id = "worker-1"
            position = (0, 0)
            cargo = 0

            def move(self, direction) -> None:
                self.direction = direction

            def wait(self) -> None:
                self.waited = True

        class Core:
            position = (0, 0)

        worker = Worker()
        stale_resource = worker.position
        config = default_config()
        original_memory = set(tactic._resource_memory)
        original_tombstones = set(tactic._resource_tombstones)
        original_dirty = tactic._map_dirty
        tactic._resource_memory.clear()
        tactic._resource_memory.add(stale_resource)
        tactic._resource_tombstones.clear()
        tactic._resource_assignments[str(worker.id)] = stale_resource
        try:
            action, detail = tactic._plan_worker(
                worker,
                Core(),
                resource_cells=frozenset(),
                obstacle_cells=frozenset(),
                depleted=set(),
                config=config,
            )
            forgotten = stale_resource not in tactic._resource_memory
            tombstoned = stale_resource in tactic._resource_tombstones
        finally:
            tactic._resource_memory.clear()
            tactic._resource_memory.update(original_memory)
            tactic._resource_tombstones.clear()
            tactic._resource_tombstones.update(original_tombstones)
            tactic._resource_assignments.clear()
            tactic._map_dirty = original_dirty

        self.assertEqual(action, "MOVE")
        self.assertIn("explore", detail)
        self.assertTrue(forgotten)
        self.assertTrue(tombstoned)

    def test_worker_uses_configured_bfs_limit(self) -> None:
        class Worker:
            id = "worker-1"
            position = (0, 0)
            cargo = 0

            def move(self, direction) -> None:
                self.direction = direction

        class Core:
            position = (0, 0)

        worker = Worker()
        config = default_config()
        config["bfs_max_steps"] = 1250
        tactic._resource_assignments[str(worker.id)] = (2, 0)
        try:
            with patch.object(tactic, "_bfs_path", return_value=[(0, 0), (1, 0), (2, 0)]) as bfs:
                action, _ = tactic._plan_worker(
                    worker,
                    Core(),
                    resource_cells=frozenset({(2, 0)}),
                    obstacle_cells=frozenset(),
                    depleted=set(),
                    config=config,
                )
        finally:
            tactic._resource_assignments.clear()

        self.assertEqual(action, "MOVE")
        self.assertEqual(bfs.call_args.kwargs["max_steps"], 1250)
        self.assertEqual(tactic.turn_context.worker_routes["worker-1"]["target"], (2, 0))
        self.assertEqual(len(tactic.turn_context.worker_routes["worker-1"]["path"]), 3)

    def test_partial_cargo_worker_on_resource_returns_home_not_harvest(self) -> None:
        # Harvest fills the worker in one action (2 while the beacon is carried),
        # so a worker at cargo=1 can never top up — the server answers with
        # CARGO_FULL and the worker would wedge on the mine forever. It must
        # return home to deposit instead.
        class Worker:
            id = "worker-1"
            position = (5, 0)
            cargo = 1

            def move(self, direction) -> None:
                self.direction = direction

            def harvest(self) -> None:
                self.harvested = True

            def deposit(self) -> None:
                self.deposited = True

        class Core:
            position = (0, 0)

        worker = Worker()
        config = default_config()
        config["worker_bfs_enabled"] = False  # exercise the greedy fallback
        try:
            action, _ = tactic._plan_worker(
                worker,
                Core(),
                resource_cells=frozenset({(5, 0)}),
                obstacle_cells=frozenset(),
                depleted=set(),
                config=config,
            )
        finally:
            tactic.turn_context.worker_routes = {}

        self.assertNotEqual(action, "HARVEST")
        self.assertIsNone(getattr(worker, "harvested", None))
        self.assertEqual(action, "MOVE")  # heading home with the partial load
        self.assertEqual(worker.direction, Direction.LEFT)

    def test_partial_cargo_worker_at_core_deposits(self) -> None:
        class Worker:
            id = "worker-1"
            position = (0, 0)
            cargo = 1

            def deposit(self) -> None:
                self.deposited = True

        class Core:
            position = (0, 0)

        worker = Worker()
        tactic.turn_context.resource_space = 5
        try:
            action, _ = tactic._plan_worker(
                worker,
                Core(),
                resource_cells=frozenset(),
                obstacle_cells=frozenset(),
                depleted=set(),
                config=default_config(),
            )
        finally:
            tactic.turn_context.worker_routes = {}

        self.assertEqual(action, "DEPOSIT")
        self.assertTrue(worker.deposited)

    def test_empty_worker_on_resource_harvests(self) -> None:
        class Worker:
            id = "worker-1"
            position = (5, 0)
            cargo = 0

            def harvest(self) -> None:
                self.harvested = True

        class Core:
            position = (0, 0)

        worker = Worker()
        try:
            action, _ = tactic._plan_worker(
                worker,
                Core(),
                resource_cells=frozenset({(5, 0)}),
                obstacle_cells=frozenset(),
                depleted=set(),
                config=default_config(),
            )
        finally:
            tactic.turn_context.worker_routes = {}

        self.assertEqual(action, "HARVEST")
        self.assertTrue(worker.harvested)

    def test_bfs_path_routes_around_obstacles(self) -> None:
        path = tactic._bfs_path(
            (0, 0),
            (2, 0),
            frozenset({(1, 0)}),
            max_steps=100,
        )

        self.assertIsNotNone(path)
        self.assertEqual(path[0], (0, 0))
        self.assertEqual(path[-1], (2, 0))
        self.assertNotIn((1, 0), path)

    def test_dead_end_cells_detect_cul_de_sac(self) -> None:
        # Convex pocket: free cell (0, 0) has walls on UP/LEFT/RIGHT, open DOWN.
        obstacles = frozenset({
            (0, -1),  # UP
            (-1, 0),  # LEFT
            (1, 0),   # RIGHT
        })
        dead = tactic._dead_end_cells(obstacles)
        self.assertIn((0, 0), dead)

    def test_dead_end_cells_collapse_corridor_into_cul_de_sac(self) -> None:
        # Corridor (0,0)->(0,1) ends in a three-sided pocket at (0,0).
        # Only one free exit from (0,1) after (0,0) is marked dead.
        obstacles = frozenset({
            (-1, 0), (1, 0), (0, -1),  # pocket walls around (0,0)
            (-1, 1), (1, 1),           # corridor side walls around (0,1)
        })
        dead = tactic._dead_end_cells(obstacles)
        self.assertIn((0, 0), dead)
        self.assertIn((0, 1), dead)

    def test_bfs_avoids_dead_end_unless_goal_requires_it(self) -> None:
        # Open path around a cul-de-sac entrance.
        # Layout: start (0,2) -> goal (2,2); pocket entrance (0,1) leads to (0,0).
        obstacles = frozenset({
            (-1, 0), (1, 0), (0, -1),  # pocket walls for (0,0)
            (-1, 1), (1, 1),           # corridor sides for (0,1)
        })
        path = tactic._bfs_path((0, 2), (2, 2), obstacles, max_steps=200)
        self.assertIsNotNone(path)
        assert path is not None
        self.assertNotIn((0, 0), path)
        self.assertNotIn((0, 1), path)

        # Goal inside the pocket must still be reachable.
        into_pocket = tactic._bfs_path((0, 2), (0, 0), obstacles, max_steps=200)
        self.assertIsNotNone(into_pocket)
        assert into_pocket is not None
        self.assertEqual(into_pocket[-1], (0, 0))
        self.assertIn((0, 1), into_pocket)

    def test_worker_explore_skips_dead_end(self) -> None:
        class Worker:
            id = "worker-deadend"
            position = (0, 1)
            cargo = 0
            direction = None

            def move(self, direction) -> None:
                self.direction = direction

            def wait(self) -> None:
                self.waited = True

        class Core:
            position = (5, 5)

        # At (0,1): UP is the cul-de-sac (0,0); RIGHT/LEFT blocked; DOWN is open.
        obstacles = frozenset({
            (-1, 0), (1, 0), (0, -1),  # walls around (0,0)
            (-1, 1), (1, 1),           # side walls at current row
        })
        worker = Worker()
        config = default_config()
        tactic._worker_last_pos.clear()
        tactic._worker_recent.clear()
        tactic._resource_assignments.clear()
        action, detail = tactic._plan_worker(
            worker,
            Core(),
            resource_cells=frozenset(),
            obstacle_cells=obstacles,
            depleted=set(),
            config=config,
        )
        self.assertEqual(action, "MOVE")
        self.assertIn("explore", detail)
        self.assertIsNotNone(worker.direction)
        # Must not step UP into the dead end when DOWN is free.
        self.assertNotEqual(worker.direction, tactic.Direction.UP)
        self.assertEqual(worker.direction, tactic.Direction.DOWN)

    def test_object_names_are_stable_and_sequential(self) -> None:
        tactic._object_names.clear()
        tactic._object_name_counters.clear()

        self.assertEqual(tactic._object_name("a", "W"), "W1")
        self.assertEqual(tactic._object_name("b", "W"), "W2")
        self.assertEqual(tactic._object_name("a", "W"), "W1")
        self.assertEqual(tactic._object_name("enemy", "E"), "E1")

    def test_astar_reaches_distant_goal_within_budget(self) -> None:
        # Plain BFS needed ~900 expansions on open maps; A* should be far lower.
        path = tactic._bfs_path(
            (0, 0),
            (40, 0),
            frozenset({(20, y) for y in range(-5, 6)} - {(20, 3)}),
            max_steps=2500,
        )
        self.assertIsNotNone(path)
        assert path is not None
        self.assertEqual(path[0], (0, 0))
        self.assertEqual(path[-1], (40, 0))
        self.assertNotIn((20, 0), path)

    def test_worker_backtrack_memory_survives_full_id_keys(self) -> None:
        class Worker:
            id = "abcdef12-ffff-ffff-ffff-ffffffffffff"
            position = (1, 0)
            cargo = 0
            direction = None

            def move(self, direction) -> None:
                self.direction = direction

            def wait(self) -> None:
                self.waited = True

        class Core:
            position = (0, 0)

        worker = Worker()
        config = default_config()
        # Simulate prior tick memory written with the full id (as planners do).
        tactic._worker_last_pos[str(worker.id)] = (0, 0)
        tactic._worker_recent[str(worker.id)] = [(0, 0)]
        tactic._resource_assignments.clear()
        try:
            action, detail = tactic._plan_worker(
                worker,
                Core(),
                resource_cells=frozenset(),
                obstacle_cells=frozenset(),
                depleted=set(),
                config=config,
            )
        finally:
            tactic._worker_last_pos.pop(str(worker.id), None)
            tactic._worker_recent.pop(str(worker.id), None)

        self.assertEqual(action, "MOVE")
        self.assertIn("explore", detail)
        # Must not immediately reverse into the previous cell (0,0) = LEFT.
        self.assertNotEqual(worker.direction, tactic.Direction.LEFT)


class WorkerEnemyEvasionTests(unittest.TestCase):
    """Workers must keep MOVING when an enemy is in range — a stationary unit
    takes damage in this game, a moving unit never does."""

    class Worker:
        def __init__(self, position: tuple[int, int], cargo: int = 0) -> None:
            self.id = "worker-1"
            self.position = position
            self.cargo = cargo
            self.direction = None
            self.waited = False
            self.deposited = False
            self.harvested = False

        def move(self, direction) -> None:
            self.direction = direction

        def wait(self) -> None:
            self.waited = True

        def deposit(self) -> None:
            self.deposited = True

        def harvest(self) -> None:
            self.harvested = True

    class Enemy:
        def __init__(self, position: tuple[int, int], unit_type: str = "VANGUARD") -> None:
            self.position = position
            self.unit_type = unit_type

    class Core:
        position = (0, 0)

    def _clear_worker_state(self) -> None:
        tactic._worker_last_pos.clear()
        tactic._worker_recent.clear()
        tactic.turn_context.worker_routes = {}

    def test_worker_on_mine_flees_when_enemy_adjacent(self) -> None:
        worker = self.Worker((5, 0))
        config = default_config()
        try:
            action, detail = tactic._plan_worker(
                worker,
                self.Core(),
                resource_cells=frozenset({(5, 0)}),
                obstacle_cells=frozenset(),
                depleted=set(),
                config=config,
                enemies=(self.Enemy((5, 1)),),
            )
        finally:
            self._clear_worker_state()

        # Evasion outranks harvesting: the worker leaves the mine instead of
        # standing still to harvest next to an enemy.
        self.assertEqual(action, "MOVE")
        self.assertIn("flee", detail)
        self.assertIsNotNone(worker.direction)
        self.assertFalse(worker.harvested)
        self.assertFalse(worker.waited)
        # The chosen cell must be free and not the enemy's own cell.
        dx, dy = worker.direction.delta
        npos = (5 + dx, 0 + dy)
        self.assertNotEqual(npos, (5, 1))
        self.assertGreaterEqual(tactic._manhattan(npos, (5, 1)), tactic._manhattan((5, 0), (5, 1)))

    def test_carrying_worker_flees_instead_of_depositing(self) -> None:
        worker = self.Worker((0, 0), cargo=1)
        tactic.turn_context.resource_space = 5
        config = default_config()
        try:
            action, detail = tactic._plan_worker(
                worker,
                self.Core(),
                resource_cells=frozenset(),
                obstacle_cells=frozenset(),
                depleted=set(),
                config=config,
                enemies=(self.Enemy((1, 0)),),
            )
        finally:
            self._clear_worker_state()

        # A full worker standing on the core would normally DEPOSIT, but with
        # an enemy adjacent it must keep moving instead of taking damage.
        self.assertEqual(action, "MOVE")
        self.assertIn("flee", detail)
        self.assertFalse(worker.deposited)
        self.assertFalse(worker.waited)

    def test_worker_outside_threat_radius_harvests_normally(self) -> None:
        worker = self.Worker((5, 0))
        config = default_config()
        try:
            action, _ = tactic._plan_worker(
                worker,
                self.Core(),
                resource_cells=frozenset({(5, 0)}),
                obstacle_cells=frozenset(),
                depleted=set(),
                config=config,
                enemies=(self.Enemy((9, 0)),),
            )
        finally:
            self._clear_worker_state()

        # Enemy 4 cells away is beyond the default radius 3 → normal harvest.
        self.assertEqual(action, "HARVEST")
        self.assertTrue(worker.harvested)

    def test_evasion_disabled_by_radius_zero(self) -> None:
        worker = self.Worker((5, 0))
        config = default_config()
        config["enemy_threat_radius"] = 0
        try:
            action, _ = tactic._plan_worker(
                worker,
                self.Core(),
                resource_cells=frozenset({(5, 0)}),
                obstacle_cells=frozenset(),
                depleted=set(),
                config=config,
                enemies=(self.Enemy((5, 1)),),
            )
        finally:
            self._clear_worker_state()

        self.assertEqual(action, "HARVEST")
        self.assertTrue(worker.harvested)

    def test_flee_never_enters_enemy_or_obstacle_cell(self) -> None:
        worker = self.Worker((0, 0))
        config = default_config()
        obstacle = (0, 1)
        enemy = (1, 0)
        try:
            action, detail = tactic._plan_worker(
                worker,
                self.Core(),
                resource_cells=frozenset(),
                obstacle_cells=frozenset({obstacle}),
                depleted=set(),
                config=config,
                enemies=(self.Enemy(enemy),),
            )
        finally:
            self._clear_worker_state()

        self.assertEqual(action, "MOVE")
        self.assertIn("flee", detail)
        dx, dy = worker.direction.delta
        npos = (dx, dy)
        self.assertNotIn(npos, {obstacle, enemy})

    def test_hostile_worker_does_not_force_flee(self) -> None:
        """Enemy Workers cannot attack — cargo should still walk home."""
        worker = self.Worker((2, 0), cargo=1)
        config = default_config()
        try:
            action, detail = tactic._plan_worker(
                worker,
                self.Core(),
                resource_cells=frozenset(),
                obstacle_cells=frozenset(),
                depleted=set(),
                config=config,
                enemies=(self.Enemy((3, 0), unit_type="WORKER"),),
            )
        finally:
            self._clear_worker_state()

        self.assertEqual(action, "MOVE")
        self.assertNotIn("flee", detail)
        self.assertIn("-> (0, 0)", detail)

    def test_carrying_flee_prefers_homeward_breakout(self) -> None:
        """A cargo courier next to a Vanguard should not reverse into oscillation."""
        worker = self.Worker((-221, 336), cargo=1)
        config = default_config()
        # Seed A<->B oscillation history: ... left cell, current right-ish cell.
        uid = str(worker.id)
        tactic._worker_last_pos[uid] = (-222, 336)
        tactic._worker_recent[uid] = [(-222, 336), (-221, 336)]
        core = self.Core()
        core.position = (-250, 363)
        try:
            action, detail = tactic._plan_worker(
                worker,
                core,
                resource_cells=frozenset(),
                obstacle_cells=frozenset(),
                depleted=set(),
                config=config,
                enemies=(self.Enemy((-220, 336), unit_type="VANGUARD"),),
            )
        finally:
            self._clear_worker_state()

        self.assertEqual(action, "MOVE")
        self.assertIn("flee", detail)
        dx, dy = worker.direction.delta
        npos = (-221 + dx, 336 + dy)
        # Must not reverse back onto the previous cell.
        self.assertNotEqual(npos, (-222, 336))
        # Should not get closer to the attacker at (-220,336).
        self.assertGreaterEqual(
            tactic._manhattan(npos, (-220, 336)),
            tactic._manhattan((-221, 336), (-220, 336)),
        )


class CombatTeamPlannerTests(unittest.TestCase):
    class CombatUnit:
        def __init__(self, unit_id: str, position: tuple[int, int]) -> None:
            self.id = unit_id
            self.position = position
            self.action = None
            self.arg = None

        def move(self, direction) -> None:
            self.action = "MOVE"
            self.arg = direction

        def wait(self) -> None:
            self.action = "WAIT"

        def sweep(self, direction) -> None:
            self.action = "SWEEP"
            self.arg = direction

        def shoot(self, target) -> None:
            self.action = "SHOOT"
            self.arg = target

    class Enemy:
        def __init__(self, position: tuple[int, int]) -> None:
            self.position = position

    def setUp(self) -> None:
        self.config = default_config()
        self._prev_last_pos = dict(tactic._worker_last_pos)
        tactic._worker_last_pos.clear()
        tactic.turn_context.tick = 0
        tactic.turn_context.beacon_pos = None

    def tearDown(self) -> None:
        tactic._worker_last_pos.clear()
        tactic._worker_last_pos.update(self._prev_last_pos)
        tactic.turn_context.beacon_pos = None

    def test_team_name_parsing_and_priority(self) -> None:
        config = default_config()
        config["home_team"] = "V1, r1"
        config["attack_team"] = "V1,V2"
        config["guerrilla_team"] = "R2"

        self.assertEqual(tactic._combat_team_for("V1", config), "home")
        self.assertEqual(tactic._combat_team_for("v2", config), "attack")
        self.assertEqual(tactic._combat_team_for("R2", config), "guerrilla")
        self.assertEqual(tactic._combat_team_for("V9", config), "unassigned")

    def test_new_combat_units_auto_join_home_team(self) -> None:
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tactic_config.json"
            config = default_config()
            config["home_team"] = "V1"
            config["attack_team"] = "V2"
            from tactic_config import save_config, load_config

            save_config(config, path)
            turn = SimpleNamespace(
                vanguards=(
                    SimpleNamespace(id="vang-1"),
                    SimpleNamespace(id="vang-2"),
                    SimpleNamespace(id="vang-3"),
                ),
                rangers=(SimpleNamespace(id="rang-1"),),
            )
            with patch.object(tactic, "save_config", side_effect=lambda values: save_config(values, path)), \
                 patch.object(tactic, "CONFIG_PATH", path):
                updated = tactic._auto_enlist_new_combat_units(turn, load_config(path))

            loaded = load_config(path)

        self.assertEqual(tactic._object_name("vang-1", "V"), "V1")
        self.assertEqual(tactic._object_name("vang-3", "V"), "V3")
        self.assertEqual(tactic._object_name("rang-1", "R"), "R1")
        self.assertEqual(updated["home_team"], "R1, V1, V3")
        self.assertEqual(updated["attack_team"], "V2")
        self.assertEqual(loaded["home_team"], "R1, V1, V3")
        self.assertEqual(tactic._combat_team_for("V2", updated), "attack")
        self.assertEqual(tactic._combat_team_for("V3", updated), "home")
        self.assertEqual(tactic._combat_team_for("R1", updated), "home")

    def test_home_team_returns_inside_patrol_radius(self) -> None:
        unit = self.CombatUnit("v-home", (20, 0))
        self.config["home_patrol_radius"] = 3

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="home",
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("home-return", detail)
        self.assertEqual(unit.action, "MOVE")

    def test_home_team_hysteresis_avoids_radius_edge_flip(self) -> None:
        # Exactly at radius+1 should patrol/hold, not flip into home-return.
        unit = self.CombatUnit("v-edge", (4, 0))
        self.config["home_patrol_radius"] = 3

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="home",
        )

        self.assertNotIn("home-return", detail)
        self.assertIn(action, {"MOVE", "WAIT"})

    def test_attack_team_marches_to_configured_target(self) -> None:
        unit = self.CombatUnit("v-attack", (0, 0))
        self.config["attack_target_x"] = 5
        self.config["attack_target_y"] = 0

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="attack",
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("attack-march", detail)
        self.assertEqual(unit.arg.name, "RIGHT")

    def test_attack_team_engages_enemies_en_route(self) -> None:
        unit = self.CombatUnit("r-attack", (0, 0))
        enemy = self.Enemy((0, 2))
        self.config["attack_target_x"] = 10
        self.config["attack_target_y"] = 0

        action, detail = tactic._plan_ranger(
            unit,
            enemies=(enemy,),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="attack",
        )

        self.assertEqual(action, "SHOOT")
        self.assertIn("enemy at", detail)
        self.assertIs(unit.arg, enemy)

    def test_attack_team_beacon_mode_marches_to_beacon_ignoring_coords(self) -> None:
        tactic.turn_context.beacon_pos = (8, 0)
        unit = self.CombatUnit("v-attack", (0, 0))
        self.config["attack_mode"] = "beacon"
        self.config["attack_target_x"] = 5
        self.config["attack_target_y"] = 7

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="attack",
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("attack-march-beacon (8, 0)", detail)
        self.assertEqual(unit.arg.name, "RIGHT")  # toward beacon, not (5, 7)

    def test_attack_team_beacon_mode_engages_enemies_en_route(self) -> None:
        tactic.turn_context.beacon_pos = (8, 0)
        unit = self.CombatUnit("r-attack", (0, 0))
        enemy = self.Enemy((0, 2))
        self.config["attack_mode"] = "beacon"
        self.config["attack_target_x"] = 50
        self.config["attack_target_y"] = 50

        action, detail = tactic._plan_ranger(
            unit,
            enemies=(enemy,),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="attack",
        )

        self.assertEqual(action, "SHOOT")
        self.assertIn("enemy at", detail)
        self.assertIs(unit.arg, enemy)

    def test_attack_team_beacon_mode_without_beacon_falls_back_to_coords(self) -> None:
        # turn_context.beacon_pos is None; static target is a defensive fallback.
        unit = self.CombatUnit("v-attack", (0, 0))
        self.config["attack_mode"] = "beacon"
        self.config["attack_target_x"] = 5
        self.config["attack_target_y"] = 0

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="attack",
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("attack-march-beacon (5, 0)", detail)
        self.assertEqual(unit.arg.name, "RIGHT")

    def test_attack_team_auto_mode_ignores_static_coords(self) -> None:
        tactic._enemy_memory.update({(6, 6)})
        try:
            unit = self.CombatUnit("v-attack", (0, 0))
            self.config["attack_mode"] = "auto"
            self.config["attack_target_x"] = 90
            self.config["attack_target_y"] = 90

            action, detail = tactic._plan_vanguard(
                unit,
                enemies=(),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=(0, 0),
                team="attack",
            )

            self.assertEqual(action, "MOVE")
            self.assertIn("attack-march-auto (6, 6)", detail)
        finally:
            tactic._enemy_memory.discard((6, 6))

    def test_guerrilla_retreats_from_three_enemies(self) -> None:
        unit = self.CombatUnit("v-g", (5, 5))
        enemies = (
            self.Enemy((6, 5)),
            self.Enemy((5, 6)),
            self.Enemy((6, 6)),
        )

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=enemies,
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="guerrilla",
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("guerrilla-retreat", detail)
        self.assertEqual(unit.action, "MOVE")

    def test_guerrilla_engages_single_enemy(self) -> None:
        unit = self.CombatUnit("v-g2", (0, 0))
        enemy = self.Enemy((1, 0))

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(enemy,),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="guerrilla",
        )

        self.assertEqual(action, "SWEEP")
        self.assertIn("enemy at", detail)


class SummaryTests(unittest.TestCase):
    def test_summary_reads_jsonl_tick_records(self) -> None:
        records = [
            {"_meta": "test"},
            {
                "tick": 10,
                "events": [{"type": "HARVEST_SUCCEEDED"}],
                "plan_unit_actions": {"a": "HARVEST:on_resource"},
            },
            {
                "tick": 11,
                "events": [{"type": "DEPOSIT_SUCCEEDED"}],
                "plan_unit_actions": {"a": "MOVE:RIGHT"},
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "tactic.jsonl"
            with log_path.open("w", encoding="utf-8") as f:
                for record in records:
                    f.write(json.dumps(record) + "\n")
                f.write("incomplete json\n")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                tactic._print_summary(str(log_path))

        summary = output.getvalue()
        self.assertIn("Ticks played:     2 (10 -> 11)", summary)
        self.assertIn("Harvest success:  1", summary)
        self.assertIn("Deposit success:  1", summary)


class DashboardLogTests(unittest.TestCase):
    def test_svg_contains_worker_route_target_and_object_names(self) -> None:
        rec = {
            "core_pos": [0, 0],
            "core_name": "C1",
            "workers": [{
                "id": "worker-1",
                "name": "W1",
                "pos": [0, 0],
                "target": [2, 0],
                "path": [[0, 0], [0, 1], [1, 1], [2, 1], [2, 0]],
                "path_complete": True,
                "cargo": 0,
            }],
            "vanguards": [{"name": "V1", "pos": [1, 2]}],
            "rangers": [{"name": "R1", "pos": [2, 2]}],
            "enemies": [{"name": "E1", "pos": [3, 2]}],
            "resource_cells": [],
        }
        memory = {"obstacles": [], "resources": []}

        svg = dashboard.render_svg(rec, memory)

        self.assertIn('<pattern id="gridPat" x="24" y="24"', svg)
        self.assertIn('class="worker-route"', svg)
        self.assertIn('class="worker-route-target"', svg)
        for name in ("C1", "W1", "V1", "R1", "E1"):
            self.assertIn(f">{name}</text>", svg)

    def test_svg_y_axis_matches_game_up_direction(self) -> None:
        """Smaller world-Y must render higher because Direction.UP is (0, -1)."""
        rec = {
            "core_pos": [0, 0],
            "core_name": "C1",
            "workers": [
                {"name": "W-north", "pos": [0, -2], "cargo": 0},
                {"name": "W-south", "pos": [0, 2], "cargo": 0},
            ],
            "vanguards": [],
            "rangers": [],
            "enemies": [],
            "resource_cells": [],
        }

        svg = dashboard.render_svg(rec, {"obstacles": [], "resources": []}, margin=4)

        def unit_cy(label: str) -> float:
            marker = f">{label}</text>"
            index = svg.index(marker)
            text_tag_start = svg.rfind("<text", 0, index)
            y_token = 'y="'
            y_start = svg.index(y_token, text_tag_start) + len(y_token)
            y_end = svg.index('"', y_start)
            return float(svg[y_start:y_end])

        self.assertLess(unit_cy("W-north"), unit_cy("W-south"))

    def test_reverse_reader_handles_small_chunk_boundaries(self) -> None:
        lines = ["first", "第二行", "third line is longer"]
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "chunked.log"
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            actual = list(dashboard._iter_log_lines_reverse(str(log_path), chunk_size=7))

        self.assertEqual(actual, list(reversed(lines)))

    def test_history_reads_latest_valid_records_from_file_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "tactic.jsonl"
            with log_path.open("w", encoding="utf-8") as f:
                for tick in range(1, 80):
                    f.write(json.dumps({
                        "tick": tick,
                        "plan_unit_actions": {},
                        "padding": "x" * 64,
                    }) + "\n")
                f.write('{"tick": 80')
                # A tick-0 record is valid and must not be skipped by the reader.
                f.write('\n{"tick": 0, "plan_unit_actions": {}}')

            with patch.object(dashboard, "LOG_FILE", str(log_path)):
                history = dashboard.read_history(3)

        self.assertEqual([record["tick"] for record in history], [0, 79, 78])


class LoggerRotationTests(unittest.TestCase):
    @staticmethod
    def _fake_turn(tick: int):
        state = SimpleNamespace(population=5, population_tier=1, upkeep_next_tick=0)
        core = SimpleNamespace(
            id=f"core-{tick}",
            position=(0, 0),
            hp=10,
            shield=5,
            view=SimpleNamespace(state=SimpleNamespace(value="ALIVE")),
        )
        beacon = SimpleNamespace(
            position=(5, 5),
            status=SimpleNamespace(name="GROUND"),
        )
        return SimpleNamespace(
            tick=tick,
            state=state,
            core=core,
            resources=10,
            resource_capacity=20,
            visible_enemies=[],
            resource_cells=frozenset(),
            obstacle_cells=frozenset(),
            beacon=beacon,
            workers=[],
            vanguards=[],
            rangers=[],
            events=[],
        )

    def setUp(self) -> None:
        self._names = dict(tactic._object_names)
        self._counters = dict(tactic._object_name_counters)
        tactic._object_names.clear()
        tactic._object_name_counters.clear()

    def tearDown(self) -> None:
        tactic._object_names.clear()
        tactic._object_names.update(self._names)
        tactic._object_name_counters.clear()
        tactic._object_name_counters.update(self._counters)

    def test_rotation_creates_bounded_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "tactic_log.jsonl"
            logger = tactic.TacticLogger(str(log_path))
            logger.open()
            try:
                with patch.object(tactic, "LOG_MAX_BYTES", 300):
                    for tick in range(1, 12):
                        logger.record_tick(self._fake_turn(tick))
            finally:
                logger.close()

            current_size = log_path.stat().st_size
            backup_exists = Path(str(log_path) + ".1").exists()

        self.assertTrue(backup_exists, "expected a rotated .1 backup")
        # The current file may exceed LOG_MAX_BYTES by at most one record (rotation
        # is checked before each write) — but it must stay bounded, not grow free.
        self.assertLess(current_size, 2000)

    def test_rotation_failure_keeps_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "tactic_log.jsonl"
            logger = tactic.TacticLogger(str(log_path))
            logger.open()
            try:
                with patch.object(tactic, "LOG_MAX_BYTES", 300):
                    # Every rotation rename fails; record_tick must keep writing
                    # to the current file and never propagate the exception.
                    with patch.object(Path, "rename", side_effect=OSError("boom")):
                        for tick in range(1, 6):
                            logger.record_tick(self._fake_turn(tick))
            finally:
                logger.close()

            lines = log_path.read_text(encoding="utf-8").splitlines()

        self.assertGreaterEqual(len(lines), 6)  # header + summary + records


class StatusLogTests(unittest.TestCase):
    def test_read_history_newest_first_and_read_latest_include_tick_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "tactic_log.jsonl"
            with log_path.open("w", encoding="utf-8") as f:
                for tick in (0, 1, 2, 3):
                    f.write(json.dumps({"tick": tick, "plan_unit_actions": {}}) + "\n")
            with patch.object(status, "LOG_FILE", str(log_path)):
                history = status.read_history(3)
                latest = status.read_latest()

        self.assertEqual([record["tick"] for record in history], [3, 2, 1])
        self.assertEqual(latest["tick"], 3)


class DeadEndCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._key = tactic._dead_end_cache_key
        self._cache = tactic._dead_end_cache
        tactic._dead_end_cache_key = None
        tactic._dead_end_cache = frozenset()

    def tearDown(self) -> None:
        tactic._dead_end_cache_key = self._key
        tactic._dead_end_cache = self._cache

    def test_cache_reuses_equal_frozensets_and_recomputes_on_growth(self) -> None:
        obstacles = frozenset({(1, 0), (2, 0)})
        calls: list = []
        real = tactic._dead_end_cells

        def counting_dead_end_cells(obs):
            calls.append(obs)
            return real(obs)

        with patch.object(tactic, "_dead_end_cells", side_effect=counting_dead_end_cells):
            tactic._get_dead_ends(obstacles)                                   # compute
            tactic._get_dead_ends(obstacles)                                   # identity hit
            tactic._get_dead_ends(frozenset({(2, 0), (1, 0)}))                 # equal-but-distinct
            tactic._get_dead_ends(frozenset({(1, 0), (2, 0), (3, 0)}))         # grown -> recompute

        self.assertEqual(len(calls), 2)


class WorkerPathCacheTests(unittest.TestCase):
    class Worker:
        def __init__(self, worker_id: str, position: tuple[int, int]) -> None:
            self.id = worker_id
            self.position = position
            self.cargo = 0
            self.direction = None

        def move(self, direction) -> None:
            self.direction = direction
            self.position = (
                self.position[0] + direction.delta[0],
                self.position[1] + direction.delta[1],
            )

        def wait(self) -> None:
            self.direction = None

    class Core:
        position = (50, 50)

    def setUp(self) -> None:
        self._names = dict(tactic._object_names)
        self._counters = dict(tactic._object_name_counters)
        self._last_pos = dict(tactic._worker_last_pos)
        self._recent = {k: list(v) for k, v in tactic._worker_recent.items()}
        self._assignments = dict(tactic._resource_assignments)
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        tactic._worker_last_pos.clear()
        tactic._worker_recent.clear()
        tactic._resource_assignments.clear()
        tactic._worker_path_cache.clear()

    def tearDown(self) -> None:
        tactic._worker_path_cache.clear()
        tactic._worker_last_pos.clear()
        tactic._worker_last_pos.update(self._last_pos)
        tactic._worker_recent.clear()
        tactic._worker_recent.update(self._recent)
        tactic._resource_assignments.clear()
        tactic._resource_assignments.update(self._assignments)
        tactic._object_names.clear()
        tactic._object_names.update(self._names)
        tactic._object_name_counters.clear()
        tactic._object_name_counters.update(self._counters)

    def _plan(self, worker, config, resource_cells, obstacle_cells):
        return tactic._plan_worker(
            worker,
            self.Core(),
            resource_cells=resource_cells,
            obstacle_cells=obstacle_cells,
            depleted=set(),
            config=config,
        )

    def test_follows_cached_path_without_recomputing(self) -> None:
        config = default_config()
        worker = self.Worker("worker-1", (0, 0))
        tactic._resource_assignments["worker-1"] = (3, 0)
        path = [(0, 0), (1, 0), (2, 0), (3, 0)]
        try:
            with patch.object(tactic, "_bfs_path", return_value=path) as bfs:
                action1, _ = self._plan(
                    worker, config,
                    resource_cells=frozenset({(3, 0)}),
                    obstacle_cells=frozenset(),
                )
                self.assertEqual(action1, "MOVE")
                self.assertEqual(worker.position, (1, 0))
                self.assertEqual(bfs.call_count, 1)

                # Second tick: the worker is on the cached path; no recompute.
                action2, _ = self._plan(
                    worker, config,
                    resource_cells=frozenset({(3, 0)}),
                    obstacle_cells=frozenset(),
                )
                self.assertEqual(action2, "MOVE")
                self.assertEqual(worker.position, (2, 0))
                self.assertEqual(bfs.call_count, 1)
        finally:
            tactic._resource_assignments.clear()

    def test_replans_when_next_step_blocked(self) -> None:
        config = default_config()
        worker = self.Worker("worker-1", (0, 0))
        tactic._resource_assignments["worker-1"] = (3, 0)
        tactic._worker_path_cache["worker-1"] = {
            "goal": (3, 0),
            "path": [(0, 0), (1, 0), (2, 0), (3, 0)],
        }
        try:
            with patch.object(
                tactic, "_bfs_path",
                return_value=[(0, 0), (0, 1), (0, 2), (3, 0)],
            ) as bfs:
                action, _ = self._plan(
                    worker, config,
                    resource_cells=frozenset({(3, 0)}),
                    obstacle_cells=frozenset({(1, 0)}),  # blocks the cached next step
                )
        finally:
            tactic._resource_assignments.clear()

        self.assertEqual(action, "MOVE")
        self.assertEqual(bfs.call_count, 1)  # cached step invalid -> replanned

    def test_replans_when_goal_changes(self) -> None:
        config = default_config()
        worker = self.Worker("worker-1", (0, 0))
        tactic._resource_assignments["worker-1"] = (5, 0)
        tactic._worker_path_cache["worker-1"] = {
            "goal": (3, 0),
            "path": [(0, 0), (1, 0), (2, 0), (3, 0)],
        }
        try:
            with patch.object(
                tactic, "_bfs_path",
                return_value=[(0, 0), (1, 0), (2, 0), (5, 0)],
            ) as bfs:
                action, _ = self._plan(
                    worker, config,
                    resource_cells=frozenset({(5, 0)}),
                    obstacle_cells=frozenset(),
                )
        finally:
            tactic._resource_assignments.clear()

        self.assertEqual(action, "MOVE")
        self.assertEqual(bfs.call_count, 1)  # different goal -> replanned

    def test_clears_cache_when_goal_reached(self) -> None:
        tactic._worker_path_cache["worker-1"] = {
            "goal": (3, 0),
            "path": [(2, 0), (3, 0)],
        }
        worker = self.Worker("worker-1", (3, 0))

        result = tactic._worker_cached_path_step(
            worker, "worker-1", (3, 0), (3, 0), frozenset(),
        )

        self.assertIsNone(result)
        self.assertNotIn("worker-1", tactic._worker_path_cache)

    def test_prune_helper_removes_cache_and_names(self) -> None:
        tactic._worker_path_cache["dead-1"] = {"goal": (0, 0), "path": [(0, 0)]}
        tactic._worker_path_cache["alive-1"] = {"goal": (0, 0), "path": [(0, 0)]}
        tactic._worker_last_pos["dead-1"] = (1, 1)
        tactic._worker_recent["dead-1"] = [(1, 1)]
        tactic._resource_assignments["dead-1"] = (1, 1)
        tactic._object_names[("W", "dead-1")] = "W1"
        tactic._object_names[("W", "alive-1")] = "W2"
        tactic._object_names[("E", "enemy-1")] = "E1"

        tactic._prune_dead_unit_bookkeeping({"alive-1"})

        self.assertNotIn("dead-1", tactic._worker_path_cache)
        self.assertIn("alive-1", tactic._worker_path_cache)
        self.assertNotIn("dead-1", tactic._worker_last_pos)
        self.assertNotIn("dead-1", tactic._worker_recent)
        self.assertNotIn("dead-1", tactic._resource_assignments)
        self.assertNotIn(("W", "dead-1"), tactic._object_names)
        self.assertIn(("W", "alive-1"), tactic._object_names)
        self.assertIn(("E", "enemy-1"), tactic._object_names)


class WorkerCongestionTests(unittest.TestCase):
    """Core-cell congestion coordination + un-stick recovery."""

    class Worker:
        def __init__(self, worker_id: str, position: tuple[int, int]) -> None:
            self.id = worker_id
            self.position = position
            self.cargo = 0
            self.direction = None

        def move(self, direction) -> None:
            self.direction = direction
            self.position = (
                self.position[0] + direction.delta[0],
                self.position[1] + direction.delta[1],
            )

        def wait(self) -> None:
            self.direction = None

    class Core:
        def __init__(self, position: tuple[int, int] = (28, -20)) -> None:
            self.position = position

    def setUp(self) -> None:
        self._last_pos = dict(tactic._worker_last_pos)
        self._recent = {k: list(v) for k, v in tactic._worker_recent.items()}
        self._assignments = dict(tactic._resource_assignments)
        self._memory = set(tactic._resource_memory)
        self._stuck = dict(tactic._worker_stuck_ticks)
        self._stuck_pos = dict(tactic._worker_stuck_pos)
        tactic._worker_last_pos.clear()
        tactic._worker_recent.clear()
        tactic._resource_assignments.clear()
        tactic._worker_path_cache.clear()
        tactic._worker_stuck_ticks.clear()
        tactic._worker_stuck_pos.clear()
        tactic._resource_memory.clear()

    def tearDown(self) -> None:
        tactic._worker_path_cache.clear()
        tactic._worker_stuck_ticks.clear()
        tactic._worker_stuck_pos.clear()
        tactic._worker_last_pos.clear()
        tactic._worker_last_pos.update(self._last_pos)
        tactic._worker_recent.clear()
        tactic._worker_recent.update(self._recent)
        tactic._resource_assignments.clear()
        tactic._resource_assignments.update(self._assignments)
        tactic._resource_memory.clear()
        tactic._resource_memory.update(self._memory)
        tactic._worker_stuck_ticks.update(self._stuck)
        tactic._worker_stuck_pos.update(self._stuck_pos)

    def _plan(self, worker, core=None, *, obstacle_cells=frozenset(),
              resource_cells=frozenset(), occupied=frozenset(), config=None):
        return tactic._plan_worker(
            worker,
            core or self.Core(),
            resource_cells=resource_cells,
            obstacle_cells=obstacle_cells,
            depleted=set(),
            config=config or default_config(),
            occupied=occupied,
        )

    def test_full_worker_retreats_when_core_occupied(self) -> None:
        worker = self.Worker("w-full", (27, -20))
        worker.cargo = 2
        # Core cell is taken by another worker; (27,-19) also occupied.
        occupied = frozenset({(28, -20), (27, -19)})

        action, detail = self._plan(worker, occupied=occupied)

        self.assertEqual(action, "MOVE")
        self.assertIn("core-retreat", detail)
        # Must move away from the core, never into the occupied core cell.
        self.assertGreater(
            tactic._manhattan(worker.position, (28, -20)),
            tactic._manhattan((27, -20), (28, -20)),
        )

    def test_full_worker_waits_when_core_occupied_and_no_retreat(self) -> None:
        worker = self.Worker("w-full2", (27, -20))
        worker.cargo = 2
        # All four neighbours of (27,-20) are occupied -> nowhere to retreat.
        occupied = frozenset({(28, -20), (27, -19), (27, -21), (26, -20)})

        action, detail = self._plan(worker, occupied=occupied)

        self.assertEqual(action, "WAIT")
        self.assertIn("core-congested", detail)
        self.assertEqual(worker.position, (27, -20))

    def test_empty_worker_vacates_core_when_goal_blocked(self) -> None:
        worker = self.Worker("w-core", (28, -20))
        worker.cargo = 0
        # A remembered resource to the west is walled off, and three of the core's
        # neighbours are occupied — BFS fails, so the worker must leave the chute.
        tactic._resource_assignments["w-core"] = (25, -23)
        tactic._resource_memory.add((25, -23))
        obstacles = frozenset({
            (25, -24), (26, -24), (26, -23), (26, -22), (25, -22), (24, -22), (24, -23),
        })
        occupied = frozenset({(27, -20), (29, -20), (28, -19)})

        action, detail = self._plan(
            worker, obstacle_cells=obstacles, occupied=occupied,
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("vacate-core", detail)
        self.assertNotEqual(worker.position, (28, -20))

    def test_worker_avoids_occupied_cell_in_bfs(self) -> None:
        worker = self.Worker("w-bfs", (0, 0))
        worker.cargo = 0
        tactic._resource_assignments["w-bfs"] = (0, 2)
        tactic._resource_memory.add((0, 2))
        # The direct UP neighbour (0,1) is taken by another unit.
        occupied = frozenset({(0, 1)})

        action, detail = self._plan(
            worker, resource_cells=frozenset({(0, 2)}), occupied=occupied,
        )

        self.assertEqual(action, "MOVE")
        self.assertIsNotNone(worker.direction)
        self.assertNotEqual(worker.direction, tactic.Direction.UP)

    def test_unstick_forgets_stale_goal_and_explores(self) -> None:
        worker = self.Worker("w-stuck", (44, 6))
        worker.cargo = 0
        tactic._resource_assignments["w-stuck"] = (44, 5)
        tactic._resource_memory.add((44, 5))
        tactic._worker_stuck_pos["w-stuck"] = (44, 6)
        tactic._worker_stuck_ticks["w-stuck"] = tactic._STUCK_THRESHOLD - 1
        # The remembered resource cell is occupied (e.g. by an enemy).
        occupied = frozenset({(44, 5)})

        action, detail = self._plan(worker, occupied=occupied)

        self.assertEqual(action, "MOVE")
        self.assertIn("explore", detail)
        self.assertNotIn("w-stuck", tactic._resource_assignments)
        self.assertNotIn((44, 5), tactic._resource_memory)

    def test_unstick_counts_frozen_worker_without_move_history(self) -> None:
        # A worker that has never moved (no _worker_last_pos entry) must still
        # accumulate stuck ticks — the old detection only counted moves, so a
        # worker frozen from the start was never unstuck.
        worker = self.Worker("w-frozen", (44, 6))
        worker.cargo = 0
        tactic._resource_assignments["w-frozen"] = (44, 5)
        tactic._resource_memory.add((44, 5))
        occupied = frozenset({(44, 5)})

        action1, _ = self._plan(worker, occupied=occupied)
        self.assertEqual(action1, "WAIT")  # blocked, no movement
        # Second tick: still stuck at the same position, count must advance.
        tactic._worker_stuck_pos["w-frozen"] = (44, 6)
        tactic._worker_stuck_ticks["w-frozen"] = tactic._STUCK_THRESHOLD - 1
        action2, detail2 = self._plan(worker, occupied=occupied)

        self.assertEqual(action2, "MOVE")
        self.assertIn("explore", detail2)
        self.assertNotIn("w-frozen", tactic._resource_assignments)


class DemandSpawnTests(unittest.TestCase):
    """Demand-based production: fill each type up to its config target."""

    class Core:
        position = (0, 0)

        def __init__(self) -> None:
            self.spawned: list[UnitType] = []

        def spawn(self, unit_type: UnitType) -> None:
            self.spawned.append(unit_type)

    def setUp(self) -> None:
        self.config = default_config()

    @staticmethod
    def _turn(*, workers=0, vanguards=0, rangers=0, population=8, extra_units=()):
        units: list = []
        for index in range(workers):
            units.append(SimpleNamespace(
                id=f"w{index}", unit_type=UnitType.WORKER, position=(index + 1, 0)))
        for index in range(vanguards):
            units.append(SimpleNamespace(
                id=f"v{index}", unit_type=UnitType.VANGUARD, position=(-index - 1, 0)))
        for index in range(rangers):
            units.append(SimpleNamespace(
                id=f"r{index}", unit_type=UnitType.RANGER, position=(-index - 1, 0)))
        units.extend(extra_units)
        return SimpleNamespace(
            units=units,
            workers=tuple(u for u in units if u.unit_type == UnitType.WORKER),
            vanguards=tuple(u for u in units if u.unit_type == UnitType.VANGUARD),
            rangers=tuple(u for u in units if u.unit_type == UnitType.RANGER),
            state=SimpleNamespace(population=population),
        )

    @staticmethod
    def _unit(name: str, unit_type: UnitType, pos: tuple[int, int]):
        return SimpleNamespace(id=name, unit_type=unit_type, position=pos)

    def test_below_target_spawns_worker_first(self) -> None:
        core = self.Core()
        turn = self._turn(workers=8, vanguards=2, rangers=2)

        self.assertEqual(
            tactic._plan_demand_spawn(turn, core, resources=100, config=self.config),
            "SPAWN_WORKER",
        )
        self.assertEqual(core.spawned, [UnitType.WORKER])

    def test_at_target_stops_producing(self) -> None:
        turn = self._turn(workers=10, vanguards=2, rangers=2)

        self.assertIsNone(tactic._plan_demand_spawn(turn, self.Core(), 100, self.config))

    def test_above_target_does_not_spawn(self) -> None:
        # Over-target is the self-destruct's job, never extra spawning.
        turn = self._turn(workers=11, vanguards=2, rangers=2)

        self.assertIsNone(tactic._plan_demand_spawn(turn, self.Core(), 100, self.config))

    def test_vanguard_filled_only_after_workers(self) -> None:
        turn = self._turn(workers=10, vanguards=1, rangers=2)

        self.assertEqual(
            tactic._plan_demand_spawn(turn, self.Core(), 100, self.config),
            "SPAWN_VANGUARD",
        )

    def test_ranger_filled_last(self) -> None:
        turn = self._turn(workers=10, vanguards=2, rangers=1)

        self.assertEqual(
            tactic._plan_demand_spawn(turn, self.Core(), 100, self.config),
            "SPAWN_RANGER",
        )

    def test_population_cap_blocks_spawn(self) -> None:
        turn = self._turn(workers=8, population=20)

        self.assertIsNone(tactic._plan_demand_spawn(turn, self.Core(), 100, self.config))

    def test_occupied_core_cell_blocks_spawn(self) -> None:
        blocking = self._unit("blocker", UnitType.WORKER, (0, 0))  # stands on the Core
        turn = self._turn(workers=8, extra_units=[blocking])

        self.assertIsNone(tactic._plan_demand_spawn(turn, self.Core(), 100, self.config))

    def test_resource_reserve_is_respected(self) -> None:
        turn = self._turn(workers=8)
        core = self.Core()
        config = dict(self.config)
        config["resource_reserve"] = 20  # worker costs 5, need 5 + 20 = 25

        self.assertIsNone(tactic._plan_demand_spawn(turn, core, resources=24, config=config))
        self.assertEqual(
            tactic._plan_demand_spawn(turn, core, resources=25, config=config),
            "SPAWN_WORKER",
        )


class SelfDestructTests(unittest.TestCase):
    """Over-target units shed one per type per Tick until back at target."""

    class Core:
        position = (0, 0)

    def setUp(self) -> None:
        self.config = default_config()

    @staticmethod
    def _unit(name: str, unit_type: UnitType, pos: tuple[int, int], cargo: int = 0):
        unit = SimpleNamespace(id=name, unit_type=unit_type, position=pos, cargo=cargo)
        unit.self_destruct_calls = []
        unit.self_destruct = lambda: unit.self_destruct_calls.append(1)
        return unit

    def _turn(self, *, workers=(), vanguards=(), rangers=(), carrier: str | None = None):
        units = list(workers) + list(vanguards) + list(rangers)
        beacon = SimpleNamespace(carrier_id=carrier) if carrier is not None else None
        return SimpleNamespace(
            units=units,
            workers=tuple(workers),
            vanguards=tuple(vanguards),
            rangers=tuple(rangers),
            beacon=beacon,
        )

    def test_over_target_worker_trims_exactly_one(self) -> None:
        workers = [self._unit(f"w{i}", UnitType.WORKER, (i + 1, 0)) for i in range(11)]
        turn = self._turn(workers=workers)

        result = tactic._plan_over_target_self_destruct(turn, self.Core(), self.config)

        self.assertEqual(len(result), 1)
        unit, detail = result[0]
        self.assertEqual(unit.id, "w10")
        self.assertEqual(detail, "11>10")
        unit.self_destruct()
        self.assertEqual(unit.self_destruct_calls, [1])

    def test_two_types_over_target_each_trimmed(self) -> None:
        workers = [self._unit(f"w{i}", UnitType.WORKER, (i + 1, 0)) for i in range(11)]
        vanguards = [self._unit(f"v{i}", UnitType.VANGUARD, (-i - 1, 0)) for i in range(3)]
        turn = self._turn(workers=workers, vanguards=vanguards)

        result = tactic._plan_over_target_self_destruct(turn, self.Core(), self.config)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0][1], "11>10")
        self.assertEqual(result[1][1], "3>2")

    def test_at_or_below_target_nothing_trimmed(self) -> None:
        workers = [self._unit(f"w{i}", UnitType.WORKER, (i + 1, 0)) for i in range(10)]
        vanguards = [self._unit(f"v{i}", UnitType.VANGUARD, (-i - 1, 0)) for i in range(2)]
        rangers = [self._unit(f"r{i}", UnitType.RANGER, (-i - 1, 0)) for i in range(2)]
        turn = self._turn(workers=workers, vanguards=vanguards, rangers=rangers)

        self.assertEqual(tactic._plan_over_target_self_destruct(turn, self.Core(), self.config), [])

    def test_beacon_carrier_is_never_selected(self) -> None:
        workers = [self._unit(f"w{i}", UnitType.WORKER, (i + 1, 0)) for i in range(10)]
        carrier = self._unit("w-beacon", UnitType.WORKER, (30, 30))
        workers.append(carrier)
        turn = self._turn(workers=workers, carrier="w-beacon")

        result = tactic._plan_over_target_self_destruct(turn, self.Core(), self.config)

        self.assertEqual(len(result), 1)
        self.assertNotEqual(result[0][0].id, "w-beacon")

    def test_prefers_empty_worker_at_equal_distance(self) -> None:
        full = self._unit("w-full", UnitType.WORKER, (0, 10), cargo=2)
        empty = self._unit("w-empty", UnitType.WORKER, (0, -10), cargo=0)
        # Nine closer workers fill the roster (11 total → one over target).
        others = [self._unit(f"w{i}", UnitType.WORKER, (i + 1, 0)) for i in range(9)]
        turn = self._turn(workers=[full, empty, *others])

        result = tactic._plan_over_target_self_destruct(turn, self.Core(), self.config)

        self.assertEqual(result[0][0].id, "w-empty")

    def test_sheds_furthest_before_near_empty(self) -> None:
        near_empty = self._unit("w-near", UnitType.WORKER, (1, 0), cargo=0)
        far_full = self._unit("w-far", UnitType.WORKER, (20, 0), cargo=2)
        others = [self._unit(f"w{i}", UnitType.WORKER, (i + 3, 0)) for i in range(9)]
        turn = self._turn(workers=[near_empty, far_full, *others])

        result = tactic._plan_over_target_self_destruct(turn, self.Core(), self.config)

        self.assertEqual(result[0][0].id, "w-far")


class ConfigClobberProtectionTests(unittest.TestCase):
    """Auto-enlist must never overwrite newer dashboard edits (e.g. targets)."""

    def test_freshest_config_overrides_cached_with_newer_disk(self) -> None:
        full = tactic_config.default_config()
        full["target_vanguards"] = 4
        full["target_rangers"] = 5
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tactic_config.json"
            path.write_text(json.dumps(full, ensure_ascii=False), encoding="utf-8")
            with patch.object(tactic, "CONFIG_PATH", path):
                fresh = tactic._freshest_config()

        self.assertEqual(fresh["target_vanguards"], 4)
        self.assertEqual(fresh["target_rangers"], 5)
        # All fields survive, not just the edited ones.
        for key in ("target_workers", "population_cap", "home_team"):
            self.assertIn(key, fresh)

    def test_auto_enlist_preserves_newer_disk_targets(self) -> None:
        # The on-disk config has targets the tactic's in-memory copy doesn't.
        full = tactic_config.default_config()
        full["target_vanguards"] = 4
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tactic_config.json"
            path.write_text(json.dumps(full, ensure_ascii=False), encoding="utf-8")
            stale = tactic_config.default_config()  # still target 2
            with patch.object(tactic, "CONFIG_PATH", path), \
                 patch.object(tactic, "save_config", wraps=tactic_config.save_config) as save_mock:
                result = tactic._ensure_home_team_membership(stale, ["V9"])

        self.assertEqual(result["target_vanguards"], 4)  # preserved from disk
        self.assertIn("V9", result["home_team"])
        self.assertEqual(save_mock.call_count, 1)
        saved = save_mock.call_args[0][0]
        self.assertEqual(saved["target_vanguards"], 4)


class GameStatsTests(unittest.TestCase):
    """Cumulative battle-report aggregation (economy / combat / production)."""

    @staticmethod
    def _ev(event_type, actor=None, values=None):
        return SimpleNamespace(
            event_type=event_type,
            reason_code=None,
            actor_id=actor,
            values=values,
            position=None,
        )

    @staticmethod
    def _unit(name, unit_type):
        return SimpleNamespace(id=name, unit_type=unit_type)

    @staticmethod
    def _turn(units=(), events=()):
        return SimpleNamespace(units=units, events=events)

    def test_per_worker_harvest_and_deposit_amounts(self) -> None:
        stats = game_stats.new_stats()
        worker = self._unit("worker-11111111", UnitType.WORKER)
        game_stats.sync_units(stats, self._turn(units=(worker,)), tick=10)
        game_stats.record_events(stats, self._turn(events=(
            self._ev("HARVEST_SUCCEEDED", "worker-11111111", {"amount": 2}),
            self._ev("HARVEST_SUCCEEDED", "worker-11111111", {"amount": 2}),
            self._ev("DEPOSIT_SUCCEEDED", "worker-11111111", {"amount": 4}),
        )), tick=11)

        self.assertEqual(stats["economy"]["harvested_total"], 4)
        self.assertEqual(stats["economy"]["harvest_count"], 2)
        self.assertEqual(stats["economy"]["deposited_total"], 4)
        rec = stats["per_worker"]["worker-1"]
        self.assertEqual(rec["harvested"], 4)
        self.assertEqual(rec["harvest_count"], 2)
        self.assertEqual(rec["deposited"], 4)

    def test_combat_shots_classified_by_type(self) -> None:
        stats = game_stats.new_stats()
        vanguard = self._unit("vg-22222222", UnitType.VANGUARD)
        ranger = self._unit("rg-33333333", UnitType.RANGER)
        game_stats.sync_units(stats, self._turn(units=(vanguard, ranger)), tick=10)
        game_stats.record_events(stats, self._turn(events=(
            self._ev("SHOT_HIT", "vg-22222222"),
            self._ev("SHOT_MISSED", "vg-22222222"),
            self._ev("SHOT_HIT", "rg-33333333"),
            self._ev("SHOT_MISSED", "rg-33333333"),
            self._ev("SHOT_MISSED", "rg-33333333"),
        )), tick=11)

        comb = stats["combat"]
        self.assertEqual(comb["vanguard_shots"], 2)
        self.assertEqual(comb["vanguard_hits"], 1)
        self.assertEqual(comb["ranger_shots"], 3)
        self.assertEqual(comb["ranger_hits"], 1)
        self.assertEqual(stats["per_combat"]["vg-22222"]["hits"], 1)
        self.assertEqual(stats["per_combat"]["rg-33333"]["shots"], 3)

    def test_birth_baseline_and_spawned_by_type(self) -> None:
        stats = game_stats.new_stats()
        worker = self._unit("worker-44444444", UnitType.WORKER)
        # First tick is a baseline: existing units are NOT counted as spawned.
        game_stats.sync_units(stats, self._turn(units=(worker,)), tick=100)
        self.assertEqual(stats["production"]["spawned"]["WORKER"], 0)

        vanguard = self._unit("vg-55555555", UnitType.VANGUARD)
        game_stats.sync_units(stats, self._turn(units=(worker, vanguard)), tick=101)
        self.assertEqual(stats["production"]["spawned"]["WORKER"], 0)
        self.assertEqual(stats["production"]["spawned"]["VANGUARD"], 1)
        self.assertEqual(stats["deaths"]["WORKER"], 0)

    def test_death_detected_on_snapshot_gone(self) -> None:
        stats = game_stats.new_stats()
        worker = self._unit("worker-66666666", UnitType.WORKER)
        game_stats.sync_units(stats, self._turn(units=(worker,)), tick=10)
        # Worker disappears on the next tick.
        game_stats.sync_units(stats, self._turn(units=()), tick=11)

        self.assertEqual(stats["deaths"]["WORKER"], 1)
        self.assertEqual(stats["per_worker"]["worker-6"]["died_tick"], 11)

    def test_self_destruct_and_global_combat_events(self) -> None:
        stats = game_stats.new_stats()
        ranger = self._unit("rg-77777777", UnitType.RANGER)
        game_stats.sync_units(stats, self._turn(units=(ranger,)), tick=10)
        game_stats.record_events(stats, self._turn(events=(
            self._ev("UNIT_SELF_DESTRUCTED", "rg-77777777"),
            self._ev("DESTRUCTION_PARTICIPATION"),
            self._ev("UNIT_DAMAGED"),
            self._ev("UNIT_MOVE_SUCCEEDED"),
            self._ev("UNIT_MOVE_FAILED"),
            self._ev("CORE_SPAWN_FAILED"),
        )), tick=11)

        self.assertEqual(stats["production"]["self_destructed"]["RANGER"], 1)
        self.assertEqual(stats["combat"]["kill_participations"], 1)
        self.assertEqual(stats["combat"]["damage_taken"], 1)
        self.assertEqual(stats["economy"]["moves_succeeded"], 1)
        self.assertEqual(stats["economy"]["moves_failed"], 1)
        self.assertEqual(stats["production"]["spawn_failed"], 1)

    def test_derive_metrics(self) -> None:
        stats = game_stats.new_stats()
        stats["start_tick"] = 100
        stats["current_tick"] = 300
        stats["economy"]["harvested_total"] = 400
        stats["economy"]["deposited_total"] = 200
        stats["combat"]["vanguard_shots"] = 10
        stats["combat"]["vanguard_hits"] = 3
        stats["samples"] = [
            {"tick": 100, "harvested_total": 0},
            {"tick": 300, "harvested_total": 400},
        ]

        derived = game_stats.derive(stats, alive_workers=8)

        self.assertEqual(derived["ticks"], 200)
        self.assertEqual(derived["harvest_per_tick"], 2.0)
        self.assertEqual(derived["harvest_per_worker"], 50.0)
        self.assertEqual(derived["vanguard_hit_rate"], 30.0)
        self.assertEqual(derived["window_harvest_per_tick"], 2.0)

    def test_save_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "stats.json"
            stats = game_stats.new_stats()
            stats["current_tick"] = 42
            stats["economy"]["harvested_total"] = 123
            game_stats.save(stats, path)

            loaded = game_stats.load(path)

        self.assertEqual(loaded["current_tick"], 42)
        self.assertEqual(loaded["economy"]["harvested_total"], 123)

    def test_maybe_save_respects_interval(self) -> None:
        stats = game_stats.new_stats()
        stats["start_tick"] = 10
        with patch.object(game_stats, "save", return_value=None) as save_mock:
            self.assertFalse(game_stats.maybe_save(stats, tick=20, interval=20))
            self.assertTrue(game_stats.maybe_save(stats, tick=30, interval=20))
            self.assertFalse(game_stats.maybe_save(stats, tick=35, interval=20))
            self.assertTrue(game_stats.maybe_save(stats, tick=50, interval=20))
        self.assertEqual(save_mock.call_count, 2)


class RangerShootingTests(unittest.TestCase):
    """8-way (diagonal) Ranger fire — rules v0.8/v0.13."""

    class Ranger:
        def __init__(self, position: tuple[int, int]) -> None:
            self.position = position
            self.action = None
            self.arg = None

        def shoot(self, target) -> None:
            self.action = "SHOOT"
            self.arg = target

    class Enemy:
        def __init__(self, position: tuple[int, int]) -> None:
            self.position = position

    def _shoot(
        self,
        enemy_positions,
        obstacles=(),
        attack_range: int = 3,
        ranger_pos: tuple[int, int] = (0, 0),
    ):
        ranger = self.Ranger(ranger_pos)
        enemies = tuple(self.Enemy(p) for p in enemy_positions)
        result = tactic._ranger_best_shot(
            ranger,
            tuple(ranger_pos),
            enemies,
            frozenset(obstacles),
            attack_range,
        )
        return result, ranger, enemies

    def test_diagonal_shot_within_range(self) -> None:
        result, ranger, enemies = self._shoot([(3, 3)])
        self.assertEqual(ranger.action, "SHOOT")
        self.assertIs(ranger.arg, enemies[0])
        self.assertEqual(result[0], "SHOOT")

    def test_diagonal_shot_out_of_range(self) -> None:
        result, ranger, _ = self._shoot([(4, 4)])
        self.assertIsNone(result)
        self.assertIsNone(ranger.action)

    def test_diagonal_blocked_by_intermediate_cell(self) -> None:
        result, ranger, _ = self._shoot([(3, 3)], obstacles=[(1, 1)])
        self.assertIsNone(result)
        self.assertIsNone(ranger.action)

    def test_side_obstacle_does_not_block_diagonal(self) -> None:
        # Obstacles beside the diagonal line never block (rules v0.8).
        result, ranger, _ = self._shoot([(2, 2)], obstacles=[(0, 2), (2, 0)])
        self.assertEqual(ranger.action, "SHOOT")
        self.assertEqual(result[0], "SHOOT")

    def test_non_aligned_cell_never_shots(self) -> None:
        result, ranger, _ = self._shoot([(2, 1)])
        self.assertIsNone(result)
        self.assertIsNone(ranger.action)

    def test_cardinal_shot_still_works(self) -> None:
        result, ranger, enemies = self._shoot([(0, 3)])
        self.assertEqual(ranger.action, "SHOOT")
        self.assertIs(ranger.arg, enemies[0])

    def test_picks_closest_enemy(self) -> None:
        result, ranger, enemies = self._shoot([(2, 2), (1, 0)])
        self.assertEqual(ranger.action, "SHOOT")
        self.assertIs(ranger.arg, enemies[1])  # (1,0) is Chebyshev 1

    def test_line_blocked_supports_diagonal(self) -> None:
        self.assertTrue(tactic._line_blocked((0, 0), (3, 3), frozenset({(1, 1)})))
        self.assertFalse(tactic._line_blocked((0, 0), (3, 3), frozenset()))
        # Non-aligned line is not a legal shot line.
        self.assertTrue(tactic._line_blocked((0, 0), (2, 1), frozenset()))
        # Cardinal still works.
        self.assertTrue(tactic._line_blocked((0, 0), (0, 3), frozenset({(0, 2)})))


class HealingPlannerTests(unittest.TestCase):
    """Post-combat healing decisions — rules v0.10."""

    @staticmethod
    def _unit(*, pos=(0, 0), hp, unit_type=UnitType.VANGUARD, cargo=0):
        return SimpleNamespace(position=pos, hp=hp, unit_type=unit_type, cargo=cargo)

    @staticmethod
    def _core(hp=5):
        return SimpleNamespace(hp=hp)

    def _needs_heal(self, unit, **overrides) -> bool:
        kwargs = dict(
            core_pos=(0, 0), core_moving=False, heal_budget=3, heal_enabled=True
        )
        kwargs.update(overrides)
        return tactic._unit_needs_heal(unit, **kwargs)

    def test_damaged_unit_at_core_heals(self) -> None:
        self.assertTrue(self._needs_heal(self._unit(hp=2)))

    def test_full_hp_does_not_heal(self) -> None:
        self.assertFalse(self._needs_heal(self._unit(hp=4)))

    def test_off_core_does_not_heal(self) -> None:
        self.assertFalse(self._needs_heal(self._unit(pos=(1, 1), hp=2)))

    def test_moving_core_does_not_heal(self) -> None:
        self.assertFalse(self._needs_heal(self._unit(hp=2), core_moving=True))

    def test_low_budget_does_not_heal(self) -> None:
        self.assertFalse(self._needs_heal(self._unit(hp=2), heal_budget=0))

    def test_disabled_does_not_heal(self) -> None:
        self.assertFalse(self._needs_heal(self._unit(hp=2), heal_enabled=False))

    def test_loaded_worker_deposits_not_heal(self) -> None:
        self.assertFalse(
            self._needs_heal(
                self._unit(hp=1, unit_type=UnitType.WORKER, cargo=1)
            )
        )

    def test_worker_heals_when_empty(self) -> None:
        self.assertTrue(
            self._needs_heal(
                self._unit(hp=1, unit_type=UnitType.WORKER, cargo=0)
            )
        )

    def test_core_heals_when_damaged(self) -> None:
        self.assertTrue(tactic._core_should_heal(self._core(hp=3), 2, default_config()))

    def test_core_full_hp_no_heal(self) -> None:
        self.assertFalse(tactic._core_should_heal(self._core(hp=5), 2, default_config()))

    def test_core_heal_respects_config(self) -> None:
        config = default_config()
        config["heal_enabled"] = False
        self.assertFalse(tactic._core_should_heal(self._core(hp=3), 2, config))


class ManualWaypointTests(unittest.TestCase):
    """Per-unit manual target coordinates (dashboard ⌖) — march, then resume."""

    class Unit:
        def __init__(self, name, pos, unit_type, cargo=0):
            self.id = name
            self.position = pos
            self.unit_type = unit_type
            self.cargo = cargo
            self.action = None
            self.arg = None

        def move(self, direction):
            self.action = "MOVE"
            self.arg = direction

        def wait(self):
            self.action = "WAIT"

    class Enemy:
        def __init__(self, position, unit_type="VANGUARD"):
            self.position = position
            self.unit_type = unit_type

    def setUp(self) -> None:
        self._wp_path = tactic.WAYPOINTS_PATH
        self._names = dict(tactic._object_names)
        self._counters = dict(tactic._object_name_counters)
        self._last_pos = dict(tactic._worker_last_pos)
        self._recent = {k: list(v) for k, v in tactic._worker_recent.items()}
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        tactic._worker_last_pos.clear()
        tactic._worker_recent.clear()
        tactic.turn_context.worker_routes = {}
        tactic.turn_context.unit_routes = {}

    def tearDown(self) -> None:
        tactic.WAYPOINTS_PATH = self._wp_path
        tactic._object_names.clear()
        tactic._object_names.update(self._names)
        tactic._object_name_counters.clear()
        tactic._object_name_counters.update(self._counters)
        tactic._worker_last_pos.clear()
        tactic._worker_last_pos.update(self._last_pos)
        tactic._worker_recent.clear()
        tactic._worker_recent.update(self._recent)
        tactic._waypoint_stuck.clear()
        tactic.turn_context.worker_routes = {}
        tactic.turn_context.unit_routes = {}

    def _plan(self, unit, name, wp, **overrides):
        kwargs = dict(
            config=default_config(),
            obstacle_cells=frozenset(),
            occupied=frozenset(),
            enemies=(),
            core_pos=(0, 0),
        )
        kwargs.update(overrides)
        return tactic._plan_waypoint(unit, name, wp, **kwargs)

    def test_march_toward_target(self) -> None:
        unit = self.Unit("w1", (0, 0), UnitType.WORKER)
        action, detail = self._plan(unit, "W1", (5, 0))
        self.assertEqual(action, "MOVE")
        self.assertEqual(unit.arg.name, "RIGHT")
        self.assertIn("waypoint", detail)

    def test_reach_clears_waypoint_only_for_that_unit(self) -> None:
        unit = self.Unit("w1", (5, 0), UnitType.WORKER)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "waypoints.json"
            with patch.object(tactic, "WAYPOINTS_PATH", path):
                tactic._write_waypoints({"W1": (5, 0), "V2": (1, 1)})
                action, detail = self._plan(unit, "W1", (5, 0))
                remaining = tactic._load_waypoints()
        self.assertEqual(action, "WAIT")
        self.assertIn("waypoint-reached", detail)
        self.assertNotIn("W1", remaining)
        self.assertIn("V2", remaining)

    def test_worker_evades_enemy_while_marching(self) -> None:
        unit = self.Unit("w1", (0, 0), UnitType.WORKER)
        enemy = self.Enemy((1, 0))
        action, detail = self._plan(unit, "W1", (9, 0), enemies=(enemy,))
        self.assertEqual(action, "MOVE")
        self.assertIn("flee", detail)

    def test_vanguard_marches_without_firing(self) -> None:
        unit = self.Unit("v1", (0, 0), UnitType.VANGUARD)
        enemy = self.Enemy((1, 1))
        action, detail = self._plan(unit, "V1", (4, 4), enemies=(enemy,))
        self.assertEqual(action, "MOVE")
        self.assertIn("waypoint", detail)

    def test_blocked_waits_and_keeps_target(self) -> None:
        unit = self.Unit("r1", (0, 0), UnitType.RANGER)
        obstacles = frozenset({(1, 0), (-1, 0), (0, 1), (0, -1)})
        action, detail = self._plan(
            unit, "R1", (5, 0), obstacle_cells=obstacles,
        )
        self.assertEqual(action, "WAIT")
        self.assertIn("waypoint-blocked", detail)

    def test_prune_removes_dead_unit_targets(self) -> None:
        targets = {"W1": (1, 1), "V2": (2, 2), "R3": (3, 3)}
        changed = tactic._prune_waypoint_targets(targets, {"W1", "V2"})
        self.assertTrue(changed)
        self.assertEqual(targets, {"W1": (1, 1), "V2": (2, 2)})
        self.assertFalse(tactic._prune_waypoint_targets(targets, {"W1", "V2"}))

    def test_load_write_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "waypoints.json"
            with patch.object(tactic, "WAYPOINTS_PATH", path):
                tactic._write_waypoints({"W1": (10, -20), "R3": (-5, 8)})
                loaded = tactic._load_waypoints()
        self.assertEqual(loaded, {"W1": (10, -20), "R3": (-5, 8)})

    def test_obstacle_target_adjacent_counts_as_arrived(self) -> None:
        # The target cell is a wall — it can never be entered. Standing next to
        # it is the closest reachable success: clear the waypoint and resume.
        unit = self.Unit("w1", (1, 0), UnitType.WORKER)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "waypoints.json"
            with patch.object(tactic, "WAYPOINTS_PATH", path):
                tactic._write_waypoints({"W1": (2, 0)})
                action, detail = self._plan(
                    unit, "W1", (2, 0),
                    obstacle_cells=frozenset({(2, 0)}),
                )
                remaining = tactic._load_waypoints()
        self.assertEqual(action, "WAIT")
        self.assertIn("waypoint-reached-adjacent", detail)
        self.assertNotIn("W1", remaining)

    def test_unreachable_target_auto_clears_after_stuck_threshold(self) -> None:
        # Unit boxed in on all four sides: the waypoint can never be reached.
        # After the stuck threshold the waypoint clears and the unit resumes
        # its normal program instead of standing there forever.
        unit = self.Unit("w1", (0, 0), UnitType.WORKER)
        obstacles = frozenset({(1, 0), (-1, 0), (0, 1), (0, -1)})
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "waypoints.json"
            with patch.object(tactic, "WAYPOINTS_PATH", path):
                tactic._write_waypoints({"W1": (9, 0)})
                last = None
                for _ in range(tactic._WAYPOINT_STUCK_THRESHOLD):
                    last = self._plan(unit, "W1", (9, 0), obstacle_cells=obstacles)
                remaining = tactic._load_waypoints()
        self.assertEqual(last[0], "WAIT")
        self.assertIn("waypoint-unreachable", last[1])
        self.assertNotIn("W1", remaining)

    def test_progress_resets_stuck_counter(self) -> None:
        unit = self.Unit("w1", (0, 0), UnitType.WORKER)
        tactic._waypoint_stuck["w1"] = (tactic._WAYPOINT_STUCK_THRESHOLD - 1, (3, 0))
        action, _ = self._plan(unit, "W1", (3, 0))
        self.assertEqual(action, "MOVE")
        self.assertNotIn("w1", tactic._waypoint_stuck)  # real progress → reset

    def test_worker_flee_resets_stuck_counter(self) -> None:
        unit = self.Unit("w1", (0, 0), UnitType.WORKER)
        enemy = self.Enemy((1, 0))
        tactic._waypoint_stuck["w1"] = (tactic._WAYPOINT_STUCK_THRESHOLD - 1, (9, 0))
        action, detail = self._plan(unit, "W1", (9, 0), enemies=(enemy,))
        self.assertEqual(action, "MOVE")
        self.assertIn("flee", detail)
        self.assertNotIn("w1", tactic._waypoint_stuck)  # fleeing is not stagnation


class DashboardWaypointTests(unittest.TestCase):
    def test_set_remove_clear_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wp_file = Path(temp_dir) / "waypoints.json"
            with patch.object(dashboard, "WAYPOINTS_FILE", str(wp_file)):
                self.assertTrue(dashboard.set_waypoint("W3", 10, 20)["ok"])
                self.assertEqual(dashboard.load_waypoints(), {"W3": [10, 20]})
                self.assertTrue(dashboard.remove_waypoint("W3")["ok"])
                self.assertEqual(dashboard.load_waypoints(), {})
                self.assertFalse(dashboard.remove_waypoint("W3")["ok"])
                self.assertTrue(dashboard.set_waypoint("V2", -5, 8)["ok"])
                self.assertTrue(dashboard.clear_waypoints()["ok"])
                self.assertEqual(dashboard.load_waypoints(), {})

    def test_render_waypoints_panel_controls(self) -> None:
        html = dashboard.render_waypoints_panel(
            {"W3": [10, 20]}, workers=["W3"], vanguards=["V2"], rangers=[],
        )
        self.assertIn('id="waypointPanel"', html)
        self.assertIn('id="wpName"', html)
        self.assertIn('<option value="W3">W3（工人）</option>', html)
        self.assertIn('<option value="V2">V2（先锋）</option>', html)
        self.assertIn('id="wpX"', html)
        self.assertIn('id="wpY"', html)
        self.assertIn('id="pickWpBtn"', html)
        self.assertIn('id="wpSetBtn"', html)
        self.assertIn('id="wpClearBtn"', html)
        self.assertIn("W3 → (10, 20)", html)
        self.assertIn('data-wp-remove="W3"', html)

    def test_svg_draws_waypoint_marker(self) -> None:
        rec = {
            "core_pos": [0, 0],
            "workers": [], "vanguards": [], "rangers": [], "enemies": [],
            "resource_cells": [],
        }
        memory = {"obstacles": [], "resources": []}
        svg = dashboard.render_svg(rec, memory, waypoints={"W3": [0, 0]})
        self.assertIn("W3→(0,0)", svg)


if __name__ == "__main__":
    unittest.main()
