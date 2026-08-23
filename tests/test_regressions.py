from __future__ import annotations

import contextlib
import io
import json
import os
import re
import tempfile
import threading
import time
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
import watchdog
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

        # Keep the worker off the Core cell so the new vacate-the-chute branch
        # (an empty worker standing on the Core steps off first) does not
        # short-circuit the stale-target / explore path under test.
        class CoreOff:
            position = (9, 9)

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
                CoreOff(),
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

        # Worker off the Core cell so the vacate-the-chute branch does not
        # pre-empt the BFS path under test.
        class CoreOff:
            position = (9, 9)

        tactic._resource_assignments[str(worker.id)] = (2, 0)
        try:
            with patch.object(tactic, "_bfs_path", return_value=[(0, 0), (1, 0), (2, 0)]) as bfs:
                action, _ = tactic._plan_worker(
                    worker,
                    CoreOff(),
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

    def test_empty_worker_explores_when_gold_full_and_enabled(self) -> None:
        class Worker:
            id = "worker-1"
            position = (5, 0)
            cargo = 0

            def harvest(self) -> None:
                self.harvested = True

            def move(self, direction) -> None:
                self.direction = direction

            def wait(self) -> None:
                self.waited = True

        class Core:
            position = (0, 0)

        worker = Worker()
        config = default_config()
        config["worker_explore_when_full"] = True
        orig_space = tactic.turn_context.resource_space
        tactic.turn_context.resource_space = 0
        try:
            action, detail = tactic._plan_worker(
                worker,
                Core(),
                resource_cells=frozenset({(5, 0)}),
                obstacle_cells=frozenset(),
                depleted=set(),
                config=config,
            )
        finally:
            tactic.turn_context.resource_space = orig_space
            tactic.turn_context.worker_routes = {}

        self.assertEqual(action, "MOVE")
        self.assertIn("explore", detail)
        self.assertFalse(getattr(worker, "harvested", False))
        self.assertFalse(getattr(worker, "waited", False))

    def test_carrying_worker_explores_when_gold_full_and_enabled(self) -> None:
        class Worker:
            id = "worker-1"
            position = (5, 0)
            cargo = 2

            def deposit(self) -> None:
                self.deposited = True

            def move(self, direction) -> None:
                self.direction = direction

            def wait(self) -> None:
                self.waited = True

        class Core:
            position = (0, 0)

        worker = Worker()
        config = default_config()
        config["worker_explore_when_full"] = True
        orig_space = tactic.turn_context.resource_space
        tactic.turn_context.resource_space = 0
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
            tactic.turn_context.resource_space = orig_space
            tactic.turn_context.worker_routes = {}

        self.assertEqual(action, "MOVE")
        self.assertIn("explore", detail)
        self.assertFalse(getattr(worker, "deposited", False))
        self.assertFalse(getattr(worker, "waited", False))

    def test_worker_harvests_when_gold_full_but_config_off(self) -> None:
        # Feature is opt-in: with the toggle off, full storage keeps today's
        # behavior (empty workers still harvest even though deposits are blocked).
        class Worker:
            id = "worker-1"
            position = (5, 0)
            cargo = 0

            def harvest(self) -> None:
                self.harvested = True

        class Core:
            position = (0, 0)

        worker = Worker()
        orig_space = tactic.turn_context.resource_space
        tactic.turn_context.resource_space = 0
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
            tactic.turn_context.resource_space = orig_space
            tactic.turn_context.worker_routes = {}

        self.assertEqual(action, "HARVEST")
        self.assertTrue(worker.harvested)

    def test_worker_harvests_when_config_on_but_space_available(self) -> None:
        # Explore mode only triggers when storage is actually full; with free
        # space the toggle changes nothing.
        class Worker:
            id = "worker-1"
            position = (5, 0)
            cargo = 0

            def harvest(self) -> None:
                self.harvested = True

        class Core:
            position = (0, 0)

        worker = Worker()
        config = default_config()
        config["worker_explore_when_full"] = True
        orig_space = tactic.turn_context.resource_space
        tactic.turn_context.resource_space = 5
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
            tactic.turn_context.resource_space = orig_space
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

    def test_worker_squeezes_past_single_friendly_in_narrow_lane(self) -> None:
        # Two workers meeting head-on in a one-wide lane used to WAIT forever:
        # each treated the other's cell as fully blocked. The server stacks up
        # to _CELL_UNIT_LIMIT units per cell, so the planner must squeeze past
        # a cell holding a single friendly (observed W13/W22 wedging each other).
        class Worker:
            id = "w-squeeze"
            position = (0, 1)
            cargo = 0
            direction = None

            def move(self, direction) -> None:
                self.direction = direction

            def wait(self) -> None:
                self.waited = True

        class CoreOff:
            position = (9, 9)

        worker = Worker()
        config = default_config()
        # Sealed one-wide corridor: the only route to (0,3) is through (0,2).
        obstacles = frozenset({
            (0, -1),
            (-1, 0), (-1, 1), (-1, 2), (-1, 3),
            (1, 0), (1, 1), (1, 2), (1, 3),
        })
        tactic._resource_assignments[str(worker.id)] = (0, 3)
        try:
            action, detail = tactic._plan_worker(
                worker,
                CoreOff(),
                resource_cells=frozenset({(0, 3)}),
                obstacle_cells=obstacles,
                depleted=set(),
                config=config,
                occupied=frozenset({(0, 1), (0, 2)}),
                enemies=(),
                cell_counts={(0, 1): 1, (0, 2): 1},
            )
        finally:
            tactic._resource_assignments.clear()
            tactic._worker_path_cache.pop(str(worker.id), None)
            tactic._worker_stuck_pos.pop(str(worker.id), None)
            tactic._worker_stuck_ticks.pop(str(worker.id), None)
            tactic._worker_last_pos.pop(str(worker.id), None)
            tactic._worker_recent.pop(str(worker.id), None)

        self.assertEqual(action, "MOVE")
        expected = tactic._direction_for_step((0, 1), (0, 2))
        self.assertEqual(worker.direction, expected)
        self.assertIn("(0, 3)", detail)

    def test_worker_legacy_view_still_waits_behind_friendly(self) -> None:
        # Without cell counts the planner keeps the legacy fully-blocked view
        # (unit tests / callers that do not pass counts): the lane is sealed.
        class Worker:
            id = "w-squeeze-legacy"
            position = (0, 1)
            cargo = 0
            direction = None

            def move(self, direction) -> None:
                self.direction = direction

            def wait(self) -> None:
                self.waited = True

        class CoreOff:
            position = (9, 9)

        worker = Worker()
        config = default_config()
        # Sealed one-wide corridor: the only route to (0,3) is through (0,2).
        obstacles = frozenset({
            (0, -1),
            (-1, 0), (-1, 1), (-1, 2), (-1, 3),
            (1, 0), (1, 1), (1, 2), (1, 3),
        })
        tactic._resource_assignments[str(worker.id)] = (0, 3)
        try:
            action, _ = tactic._plan_worker(
                worker,
                CoreOff(),
                resource_cells=frozenset({(0, 3)}),
                obstacle_cells=obstacles,
                depleted=set(),
                config=config,
                occupied=frozenset({(0, 1), (0, 2)}),
                enemies=(),
            )
        finally:
            tactic._resource_assignments.clear()
            tactic._worker_path_cache.pop(str(worker.id), None)
            tactic._worker_stuck_pos.pop(str(worker.id), None)
            tactic._worker_stuck_ticks.pop(str(worker.id), None)
            tactic._worker_last_pos.pop(str(worker.id), None)
            tactic._worker_recent.pop(str(worker.id), None)

        self.assertEqual(action, "WAIT")

    def test_cargo_worker_greedy_squeezes_past_friendly(self) -> None:
        # Greedy home-march fallback (BFS disabled): LEFT toward the core is
        # occupied by a single friendly. Capacity-aware stepping squeezes
        # through instead of detouring away from the core.
        class Worker:
            id = "w-cargo-squeeze"
            position = (5, 0)
            cargo = 2
            direction = None

            def move(self, direction) -> None:
                self.direction = direction

            def wait(self) -> None:
                self.waited = True

            def deposit(self) -> None:
                self.deposited = True

        class Core:
            position = (0, 0)

        worker = Worker()
        config = default_config()
        config["worker_bfs_enabled"] = False
        action, _ = tactic._plan_worker(
            worker,
            Core(),
            resource_cells=frozenset(),
            obstacle_cells=frozenset(),
            depleted=set(),
            config=config,
            occupied=frozenset({(5, 0), (4, 0)}),
            enemies=(),
            cell_counts={(5, 0): 1, (4, 0): 1},
        )

        self.assertEqual(action, "MOVE")
        self.assertEqual(worker.direction, Direction.LEFT)

    def test_worker_never_squeezes_onto_enemy_or_packed_cell(self) -> None:
        # Squeezing only applies to cells with one friendly occupant: enemy
        # cells and cells already at the stacking limit stay hard-blocked.
        self.assertEqual(
            tactic._capacity_blocked_cells(
                frozenset({(1, 0), (2, 0), (3, 0)}),
                frozenset({(1, 0)}),
                {(1, 0): 0, (2, 0): 1, (3, 0): tactic._CELL_UNIT_LIMIT},
                core_pos=(9, 9),
            ),
            frozenset({(1, 0), (3, 0)}),
        )
        # Core cell keeps its stricter Core+one-unit capacity.
        self.assertEqual(
            tactic._capacity_blocked_cells(
                frozenset({(0, 0)}),
                frozenset(),
                {(0, 0): 1},
                core_pos=(0, 0),
            ),
            frozenset({(0, 0)}),
        )
        # No counts: legacy fully-blocked view.
        self.assertEqual(
            tactic._capacity_blocked_cells(
                frozenset({(2, 0)}), frozenset(), None, core_pos=(9, 9),
            ),
            frozenset({(2, 0)}),
        )

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
            self.expected_cell = None

        def move(self, direction) -> None:
            self.action = "MOVE"
            self.arg = direction

        def wait(self) -> None:
            self.action = "WAIT"

        def sweep(self, direction) -> None:
            self.action = "SWEEP"
            self.arg = direction

        def shoot(self, target, *, expected_cell=None) -> None:
            self.action = "SHOOT"
            self.arg = target
            self.expected_cell = expected_cell

    class Enemy:
        def __init__(self, position: tuple[int, int]) -> None:
            self.id = f"enemy-{position[0]}-{position[1]}"
            self.position = position
            self.unit_type = UnitType.VANGUARD

    def setUp(self) -> None:
        self.config = default_config()
        self._prev_last_pos = dict(tactic._worker_last_pos)
        self._prev_combat_paths = dict(tactic._combat_path_cache)
        self._prev_engage_target = dict(tactic._home_engage_target)
        self._prev_motion_tracks = {
            key: list(value) for key, value in tactic._enemy_motion_tracks.items()
        }
        tactic._worker_last_pos.clear()
        tactic._combat_path_cache.clear()
        tactic._home_engage_target.clear()
        tactic._enemy_motion_tracks.clear()
        tactic.turn_context.tick = 0
        tactic.turn_context.beacon_pos = None
        tactic.turn_context.core_pos = None
        tactic.turn_context.attack_squad_pos = None

    def tearDown(self) -> None:
        tactic._worker_last_pos.clear()
        tactic._worker_last_pos.update(self._prev_last_pos)
        tactic._combat_path_cache.clear()
        tactic._combat_path_cache.update(self._prev_combat_paths)
        tactic._home_engage_target.clear()
        tactic._home_engage_target.update(self._prev_engage_target)
        tactic._enemy_motion_tracks.clear()
        tactic._enemy_motion_tracks.update(self._prev_motion_tracks)
        tactic.turn_context.beacon_pos = None
        tactic.turn_context.core_pos = None
        tactic.turn_context.attack_squad_pos = None

    def test_team_name_parsing_and_priority(self) -> None:
        config = default_config()
        config["home_team"] = "V1, r1"
        config["attack_team"] = "V1,V2"
        config["kite_team"] = "V2,R2"
        config["guerrilla_team"] = "R2,R3"

        self.assertEqual(tactic._combat_team_for("V1", config), "home")
        self.assertEqual(tactic._combat_team_for("v2", config), "attack")
        self.assertEqual(tactic._combat_team_for("R2", config), "kite")
        self.assertEqual(tactic._combat_team_for("R3", config), "guerrilla")
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
            with patch.object(
                tactic,
                "mutate_config",
                side_effect=lambda mutator, _path: tactic_config.mutate_config(mutator, path),
            ), \
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

    def test_home_team_intercepts_enemy_outside_patrol_ring(self) -> None:
        # Enemy 6 cells out: beyond return_radius (3+1) but inside the
        # engage radius -> defender sallies out with the home-intercept label.
        unit = self.CombatUnit("v-sally", (0, 0))
        self.config["home_patrol_radius"] = 3
        self.config["home_engage_radius"] = 10

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(self.Enemy((6, 0)),),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="home",
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("home-intercept", detail)
        self.assertEqual(unit.arg.name, "RIGHT")

    def test_home_team_ignores_enemy_beyond_engage_radius(self) -> None:
        # Enemy beyond engage_radius + 1 -> no sally; fall back to patrol.
        unit = self.CombatUnit("v-far", (0, 0))
        self.config["home_patrol_radius"] = 3
        self.config["home_engage_radius"] = 10

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(self.Enemy((12, 0)),),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="home",
        )

        self.assertNotIn("home-intercept", detail)
        self.assertNotIn("home-engage", detail)
        self.assertIn(action, {"MOVE", "WAIT"})

    def test_home_engage_radius_zero_keeps_legacy_behavior(self) -> None:
        # 0 disables the intercept: an out-of-ring enemy is ignored exactly
        # like the old return_radius-only chase.
        unit = self.CombatUnit("v-off", (0, 0))
        self.config["home_patrol_radius"] = 3
        self.config["home_engage_radius"] = 0

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(self.Enemy((6, 0)),),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="home",
        )

        self.assertNotIn("home-intercept", detail)
        self.assertNotIn("home-engage", detail)
        self.assertIn(action, {"MOVE", "WAIT"})

    def test_home_intercept_hysteresis_no_boundary_flip(self) -> None:
        # Enemies exactly on engage_radius and one cell past it (the abandon
        # threshold) both keep the chase; only beyond engage_radius + 1 drops
        # it, so no A-B-A flip on the ring boundary.
        self.config["home_patrol_radius"] = 3
        self.config["home_engage_radius"] = 10

        for offset in (10, 11):
            unit = self.CombatUnit(f"v-band-{offset}", (0, 0))
            tactic._combat_path_cache.clear()
            action, detail = tactic._plan_vanguard(
                unit,
                enemies=(self.Enemy((offset, 0)),),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=(0, 0),
                team="home",
            )
            self.assertEqual(action, "MOVE")
            self.assertIn("home-intercept", detail)

        tactic._combat_path_cache.clear()
        unit = self.CombatUnit("v-past", (0, 0))
        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(self.Enemy((12, 0)),),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="home",
        )
        self.assertNotIn("home-intercept", detail)

    def test_home_patrol_slots_stay_inside_radius(self) -> None:
        # All 8 slots must be distinct and reachable: Manhattan distance
        # <= radius (the return line sits at radius+1, so anything farther
        # ping-pongs home-return/home-patrol and is never reached).
        core = (0, 0)
        for radius in range(2, 9):
            goals = {
                tactic._home_patrol_goal(f"probe-{i}", core, radius)
                for i in range(400)
            }
            self.assertEqual(len(goals), 8, f"radius={radius}")
            for goal in goals:
                self.assertLessEqual(
                    tactic._manhattan(goal, core), radius, f"radius={radius}",
                )
        # Radius 1 cannot host 8 distinct cells within distance 1; the ring
        # keeps 8 distinct slots (corners at distance 2) there.
        goals = {
            tactic._home_patrol_goal(f"probe-{i}", core, 1) for i in range(400)
        }
        self.assertEqual(len(goals), 8)

    def test_home_patrol_diagonal_slot_reachable_and_holds(self) -> None:
        # Regression: diagonal slots used to sit at Manhattan distance 2r,
        # past return_radius r+1 — defenders ping-ponged home-return/patrol
        # forever. A diagonal-slot defender must now walk in and settle into
        # home-hold without ever triggering home-return.
        radius = self.config["home_patrol_radius"]
        core = (0, 0)
        uid = None
        for i in range(400):
            cand = f"probe-{i}"
            goal = tactic._home_patrol_goal(cand, core, radius)
            if goal[0] != core[0] and goal[1] != core[1]:
                uid = cand
                break
        self.assertIsNotNone(uid, "no diagonal-slot unit id found")

        unit = self.CombatUnit(uid, core)
        for _ in range(4 * radius + 4):
            tactic._combat_path_cache.clear()
            action, detail = tactic._plan_vanguard(
                unit,
                enemies=(),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=core,
                team="home",
            )
            self.assertNotIn("home-return", detail)
            if action == "WAIT" and "home-hold" in detail:
                break
            self.assertEqual(action, "MOVE", detail)
            dx, dy = unit.arg.delta
            unit.position = (unit.position[0] + dx, unit.position[1] + dy)
        else:
            self.fail("diagonal-slot defender never reached home-hold")

    def test_home_intercept_memory_holds_target_after_vision_loss(self) -> None:
        # A chased enemy slipping out of vision for a couple of ticks must
        # not flip the defender back to patrol; the target lock survives
        # home_engage_memory_ticks and only then expires.
        self.config["home_patrol_radius"] = 3
        self.config["home_engage_radius"] = 10
        self.config["home_engage_memory_ticks"] = 4
        core = (0, 0)

        tactic.turn_context.tick = 0
        unit = self.CombatUnit("v-lock", (0, 0))
        enemy = self.Enemy((6, 0))
        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(enemy,),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=core,
            team="home",
        )
        self.assertEqual(action, "MOVE")
        self.assertIn("home-intercept", detail)
        # Simulate the sighting feeding the motion tracks, then vision loss.
        tactic._update_enemy_motion_tracks((enemy,), 0)

        for tick in (1, 2, 4):
            tactic.turn_context.tick = tick
            tactic._combat_path_cache.clear()
            action, detail = tactic._plan_vanguard(
                unit,
                enemies=(),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=core,
                team="home",
            )
            self.assertEqual(action, "MOVE", f"tick={tick}: {detail}")
            self.assertIn("home-intercept", detail, f"tick={tick}")

        # Memory window expired -> the chase drops and patrol resumes.
        tactic.turn_context.tick = 5
        tactic._combat_path_cache.clear()
        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=core,
            team="home",
        )
        self.assertNotIn("home-intercept", detail)
        self.assertNotIn("home-engage", detail)
        self.assertIn(action, {"MOVE", "WAIT"})

    def test_home_intercept_memory_yields_to_chute_clear(self) -> None:
        # Delivery pipeline priority: on the core ring while carriers queue,
        # the remembered-chase fallback must yield to chute-clear; the lock
        # survives the yield so the chase resumes once the lane is clear.
        self.config["home_patrol_radius"] = 3
        self.config["home_engage_radius"] = 10
        self.config["home_engage_memory_ticks"] = 4
        core = (0, 0)

        tactic.turn_context.tick = 0
        unit = self.CombatUnit("v-yield", (1, 0))
        enemy = self.Enemy((6, 0))
        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(enemy,),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=core,
            team="home",
        )
        self.assertIn("home-intercept", detail)
        tactic._update_enemy_motion_tracks((enemy,), 0)

        tactic.turn_context.tick = 1
        tactic._combat_path_cache.clear()
        prev_flag = tactic._chute_in_demand
        tactic._chute_in_demand = True
        try:
            action, detail = tactic._plan_home_combat(
                unit,
                unit_kind="vanguard",
                enemies=(),
                obstacle_cells=frozenset(),
                core_pos=core,
                config=self.config,
                cell_counts={},
            )
        finally:
            tactic._chute_in_demand = prev_flag
        self.assertEqual(action, "MOVE")
        self.assertIn("chute-clear", detail)
        # Memory survives the yield: chase resumes while the window holds.
        self.assertIn("v-yield", tactic._home_engage_target)

    def test_home_intercept_memory_drops_on_catchup(self) -> None:
        # Reaching the remembered last-known cell ends the lock (no residual
        # single-cell A-B-A vs. home-return beyond the return line).
        self.config["home_patrol_radius"] = 3
        self.config["home_engage_radius"] = 10
        self.config["home_engage_memory_ticks"] = 4
        core = (0, 0)
        tactic._home_engage_target["v-catch"] = ("enemy-5-0", 0)
        tactic._enemy_motion_tracks["enemy-5-0"] = [(0, (5, 0))]

        tactic.turn_context.tick = 1
        unit = self.CombatUnit("v-catch", (5, 0))
        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=core,
            team="home",
        )
        self.assertNotIn("home-intercept", detail)
        self.assertNotIn("home-engage", detail)
        self.assertNotIn("v-catch", tactic._home_engage_target)
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

    def test_attack_coords_ignores_enemy_behind_target(self) -> None:
        # 坐标进攻只接战"顺路"敌人：反方向（比本单位更远离目标）的可见敌人
        # 不再被追，队伍继续朝目标行军。
        unit = self.CombatUnit("v-attack", (0, 0))
        enemy = self.Enemy((-3, 0))
        self.config["attack_target_x"] = 5
        self.config["attack_target_y"] = 0

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(enemy,),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="attack",
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("attack-march-coords (5, 0)", detail)
        self.assertNotIn("attack-engage", detail)
        self.assertEqual(unit.arg.name, "RIGHT")

    def test_attack_coords_engages_on_way_enemy(self) -> None:
        # 目标侧的敌人（到目标距离 ≤ 本单位到目标距离）算"沿途"，正常追击。
        unit = self.CombatUnit("v-attack", (0, 0))
        enemy = self.Enemy((3, 0))
        self.config["attack_target_x"] = 5
        self.config["attack_target_y"] = 0

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(enemy,),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="attack",
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("attack-engage", detail)
        self.assertEqual(unit.arg.name, "RIGHT")

    def test_attack_coords_holds_at_target_ignores_distant_enemy(self) -> None:
        # 已到达目标点后只打贴身/射程内敌人；远处可见敌人不再带离坐标。
        unit = self.CombatUnit("v-attack", (5, 0))
        enemy = self.Enemy((5, 3))
        self.config["attack_target_x"] = 5
        self.config["attack_target_y"] = 0

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(enemy,),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="attack",
        )

        self.assertEqual(action, "WAIT")
        self.assertIn("attack-hold-coords (5, 0)", detail)
        self.assertNotIn("attack-engage", detail)

    def test_attack_beacon_ignores_enemy_behind_beacon(self) -> None:
        # 信标模式同样只追顺路敌人：反方向敌人忽略，继续朝信标行军。
        tactic.turn_context.beacon_pos = (5, 0)
        unit = self.CombatUnit("v-attack", (0, 0))
        enemy = self.Enemy((-3, 0))
        self.config["attack_mode"] = "beacon"

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(enemy,),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="attack",
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("attack-march-beacon (5, 0)", detail)
        self.assertNotIn("attack-engage", detail)
        self.assertEqual(unit.arg.name, "RIGHT")

    def test_attack_march_leash_radius_limits_chase(self) -> None:
        # attack_march_engage_radius=N 时，顺路但距本单位超过 N 格的敌人
        # 也被排除，避免为远处敌人大幅绕路。
        unit = self.CombatUnit("v-attack", (0, 0))
        enemy = self.Enemy((4, 0))  # 顺路（距目标 1），但距本单位 4 > N=2
        self.config["attack_target_x"] = 5
        self.config["attack_target_y"] = 0
        self.config["attack_march_engage_radius"] = 2

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(enemy,),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="attack",
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("attack-march-coords (5, 0)", detail)
        self.assertNotIn("attack-engage", detail)
        self.assertEqual(unit.arg.name, "RIGHT")

    def test_attack_team_detours_when_direct_steps_are_blocked(self) -> None:
        unit = self.CombatUnit("v-detour", (0, 0))
        self.config["attack_target_x"] = 2
        self.config["attack_target_y"] = 2

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(),
            obstacle_cells=frozenset({(1, 0), (0, 1)}),
            config=self.config,
            core_pos=(0, 0),
            team="attack",
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("attack-march", detail)
        self.assertEqual(unit.arg.name, "UP")
        self.assertEqual(
            tactic._combat_path_cache["v-detour"]["path"][:2],
            [(0, 0), (0, -1)],
        )

    def test_combat_path_cache_is_reused_across_ticks(self) -> None:
        unit = self.CombatUnit("v-cached", (0, 0))
        self.config["attack_target_x"] = 2
        self.config["attack_target_y"] = 0
        path = [(0, 0), (0, -1), (1, -1), (2, -1), (2, 0)]

        with patch.object(tactic, "_bfs_path", return_value=path) as bfs:
            action1, _ = tactic._plan_vanguard(
                unit,
                enemies=(),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=(0, 0),
                team="attack",
            )
            unit.position = (0, -1)
            action2, _ = tactic._plan_vanguard(
                unit,
                enemies=(),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=(0, 0),
                team="attack",
            )

        self.assertEqual(action1, "MOVE")
        self.assertEqual(action2, "MOVE")
        self.assertEqual(unit.arg.name, "RIGHT")
        self.assertEqual(bfs.call_count, 1)

    def test_combat_path_replans_when_next_step_becomes_blocked(self) -> None:
        unit = self.CombatUnit("v-replan", (0, 0))
        self.config["attack_target_x"] = 2
        self.config["attack_target_y"] = 0
        tactic._combat_path_cache["v-replan"] = {
            "goal": (2, 0),
            "path": [(0, 0), (1, 0), (2, 0)],
        }
        replacement = [(0, 0), (0, -1), (1, -1), (2, -1), (2, 0)]

        with patch.object(tactic, "_bfs_path", return_value=replacement) as bfs:
            action, _ = tactic._plan_vanguard(
                unit,
                enemies=(),
                obstacle_cells=frozenset({(1, 0)}),
                config=self.config,
                core_pos=(0, 0),
                team="attack",
            )

        self.assertEqual(action, "MOVE")
        self.assertEqual(unit.arg.name, "UP")
        self.assertEqual(bfs.call_count, 1)

    def test_combat_path_replans_when_goal_changes(self) -> None:
        unit = self.CombatUnit("v-new-goal", (0, 0))
        self.config["attack_target_x"] = 0
        self.config["attack_target_y"] = 2
        tactic._combat_path_cache["v-new-goal"] = {
            "goal": (2, 0),
            "path": [(0, 0), (1, 0), (2, 0)],
        }
        replacement = [(0, 0), (0, 1), (0, 2)]

        with patch.object(tactic, "_bfs_path", return_value=replacement) as bfs:
            action, _ = tactic._plan_vanguard(
                unit,
                enemies=(),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=(0, 0),
                team="attack",
            )

        self.assertEqual(action, "MOVE")
        self.assertEqual(unit.arg.name, "DOWN")
        self.assertEqual(bfs.call_count, 1)
        self.assertEqual(tactic._combat_path_cache["v-new-goal"]["goal"], (0, 2))

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

    def test_lead_fire_switch_reaches_ranger_shot_planner(self) -> None:
        unit = self.CombatUnit("r-config", (0, 0))
        self.config["ranger_lead_fire_enabled"] = False
        with patch.object(
            tactic,
            "_ranger_best_shot",
            return_value=("SHOOT", "mock"),
        ) as shot:
            action, _ = tactic._plan_ranger(
                unit,
                enemies=(),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=(0, 0),
                team="unassigned",
            )

        self.assertEqual(action, "SHOOT")
        self.assertFalse(shot.call_args.kwargs["lead_fire_enabled"])

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

    def test_attack_team_auto_mode_prefers_enemies_near_core_and_squad(self) -> None:
        # Target score = dist(enemy, core) + dist(enemy, squad centroid).
        # (9,0) sits beside the shared core/squad at (10,0); (0,5) is the
        # nearest point to the unit itself. The weighting must override the
        # per-unit nearest pick, proving the whole squad converges on the
        # core-protecting target.
        tactic._enemy_memory.update({(0, 5), (9, 0)})
        tactic.turn_context.core_pos = (10, 0)
        tactic.turn_context.attack_squad_pos = (10, 0)
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
                core_pos=(10, 0),
                team="attack",
            )

            self.assertEqual(action, "MOVE")
            self.assertIn("attack-march-auto (9, 0)", detail)
            self.assertEqual(unit.arg.name, "RIGHT")
        finally:
            tactic._enemy_memory.discard((0, 5))
            tactic._enemy_memory.discard((9, 0))
            tactic.turn_context.core_pos = None
            tactic.turn_context.attack_squad_pos = None

    def test_attack_team_auto_mode_squad_proximity_counts_independently(self) -> None:
        # Same unit, same core: the squad centroid at (10,0) tips the pick to
        # (9,0) even though (0,5) is closer to both the unit and the core.
        tactic._enemy_memory.update({(0, 5), (9, 0)})
        tactic.turn_context.core_pos = (0, 0)
        tactic.turn_context.attack_squad_pos = (10, 0)
        try:
            unit = self.CombatUnit("r-attack", (0, 0))
            self.config["attack_mode"] = "auto"
            self.config["attack_target_x"] = 90
            self.config["attack_target_y"] = 90

            action, detail = tactic._plan_ranger(
                unit,
                enemies=(),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=(0, 0),
                team="attack",
            )

            self.assertEqual(action, "MOVE")
            self.assertIn("attack-march-auto (9, 0)", detail)
            self.assertEqual(unit.arg.name, "RIGHT")
        finally:
            tactic._enemy_memory.discard((0, 5))
            tactic._enemy_memory.discard((9, 0))
            tactic.turn_context.core_pos = None
            tactic.turn_context.attack_squad_pos = None

    def test_attack_auto_radius_filters_target_candidates(self) -> None:
        # radius=3、core=(0,0)：唯一目击点 (10,0) 距核心 10 格 > 3，
        # 候选被滤空后回退静态进攻坐标。
        tactic._enemy_memory.update({(10, 0)})
        tactic.turn_context.core_pos = (0, 0)
        try:
            unit = self.CombatUnit("v-attack", (0, 0))
            self.config["attack_mode"] = "auto"
            self.config["attack_auto_radius"] = 3
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
            self.assertIn("attack-march-auto (90, 90)", detail)
            self.assertNotIn("attack-march-auto (10, 0)", detail)
        finally:
            tactic._enemy_memory.discard((10, 0))
            tactic.turn_context.core_pos = None

    def test_attack_auto_radius_keeps_in_range_candidate(self) -> None:
        # radius=3：(10,0) 被滤掉，(2,0) 在半径内被选中。
        tactic._enemy_memory.update({(10, 0), (2, 0)})
        tactic.turn_context.core_pos = (0, 0)
        try:
            unit = self.CombatUnit("v-attack", (0, 0))
            self.config["attack_mode"] = "auto"
            self.config["attack_auto_radius"] = 3
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
            self.assertIn("attack-march-auto (2, 0)", detail)
            self.assertEqual(unit.arg.name, "RIGHT")
        finally:
            tactic._enemy_memory.discard((10, 0))
            tactic._enemy_memory.discard((2, 0))
            tactic.turn_context.core_pos = None

    def test_attack_auto_radius_blocks_engage_outside_radius(self) -> None:
        # radius=3、core=(0,0)：可见敌人在 (5,0)（半径外）不触发追击，
        # 跳过接敌落入行军。
        tactic.turn_context.core_pos = (0, 0)
        try:
            unit = self.CombatUnit("v-attack", (0, 0))
            self.config["attack_mode"] = "auto"
            self.config["attack_auto_radius"] = 3
            self.config["attack_target_x"] = 0
            self.config["attack_target_y"] = 5

            action, detail = tactic._plan_vanguard(
                unit,
                enemies=(self.Enemy((5, 0)),),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=(0, 0),
                team="attack",
            )

            self.assertEqual(action, "MOVE")
            self.assertNotIn("attack-engage", detail)
            self.assertIn("attack-march-auto", detail)
        finally:
            tactic.turn_context.core_pos = None

    def test_attack_auto_radius_zero_keeps_existing_behavior(self) -> None:
        # radius=0（默认）：目标候选与沿途接敌均不受半径约束。
        tactic._enemy_memory.update({(10, 0)})
        tactic.turn_context.core_pos = (0, 0)
        try:
            self.config["attack_mode"] = "auto"
            self.config["attack_auto_radius"] = 0
            self.config["attack_target_x"] = 90
            self.config["attack_target_y"] = 90

            march_unit = self.CombatUnit("v-attack", (0, 0))
            action, detail = tactic._plan_vanguard(
                march_unit,
                enemies=(),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=(0, 0),
                team="attack",
            )
            self.assertEqual(action, "MOVE")
            self.assertIn("attack-march-auto (10, 0)", detail)

            engage_unit = self.CombatUnit("v-engage", (0, 0))
            action, detail = tactic._plan_vanguard(
                engage_unit,
                enemies=(self.Enemy((5, 0)),),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=(0, 0),
                team="attack",
            )
            self.assertEqual(action, "MOVE")
            self.assertIn("attack-engage", detail)
        finally:
            tactic._enemy_memory.discard((10, 0))
            tactic.turn_context.core_pos = None

    def test_guerrilla_uses_kite_evasion_for_three_enemies(self) -> None:
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
        self.assertIn("kite-evade", detail)
        self.assertEqual(unit.action, "MOVE")

    def test_guerrilla_safely_attacks_adjacent_worker(self) -> None:
        """Workers are targets but not danger zones, matching kite policy."""
        unit = self.CombatUnit("v-g5", (5, 5))
        enemies = (
            SimpleNamespace(position=(7, 5), unit_type="CORE"),
            SimpleNamespace(position=(6, 5), unit_type="WORKER"),
        )

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=enemies,
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="guerrilla",
        )

        self.assertEqual(action, "SWEEP")
        self.assertIn("safe-sweep worker", detail)

    def test_guerrilla_attacks_lone_worker(self) -> None:
        """A lone Worker is attacked without being assessed as a threat."""
        unit = self.CombatUnit("v-g6", (5, 5))
        enemies = (SimpleNamespace(position=(6, 5), unit_type="WORKER"),)

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=enemies,
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="guerrilla",
        )

        self.assertEqual(action, "SWEEP")
        self.assertIn("safe-sweep worker", detail)

    def _setup_attack_retreat(self, squad_pos, squad_size, forbidden=frozenset(),
                              cluster_centroid=None):
        tactic.turn_context.attack_squad_pos = squad_pos
        tactic.turn_context.attack_squad_size = squad_size
        tactic.turn_context.attack_retreat = True
        tactic.turn_context.attack_retreat_from = cluster_centroid
        tactic.turn_context.attack_forbidden_targets = forbidden

    def tearDown_attack_retreat(self):
        tactic.turn_context.attack_squad_pos = None
        tactic.turn_context.attack_squad_size = 0
        tactic.turn_context.attack_retreat = False
        tactic.turn_context.attack_retreat_from = None
        tactic.turn_context.attack_forbidden_targets = frozenset()

    def test_attack_retreat_when_outnumbered(self) -> None:
        # 1 squad member vs 2 enemy combat units within radius: enemy >= squad
        # => retreat away from the cluster centroid, never engage.
        tactic._enemy_memory.update({(11, 10)})
        self._setup_attack_retreat(
            squad_pos=(10, 10), squad_size=1,
            cluster_centroid=(12, 10), forbidden=frozenset({(11, 10)}),
        )
        # retreat decision helper: 2 threats within radius vs 1 squad member.
        decision = tactic._attack_retreat_decision(
            enemies=(self.Enemy((12, 10)), self.Enemy((13, 10))),
            squad_pos=(10, 10), squad_size=1, radius=5,
            enemy_memory={(11, 10)},
        )
        self.assertTrue(decision[0])  # retreat=True
        self.assertEqual(decision[1], 2)  # 2 nearby threats
        try:
            unit = self.CombatUnit("v-attack", (10, 10))
            self.config["attack_mode"] = "auto"
            action, detail = tactic._plan_vanguard(
                unit,
                enemies=(self.Enemy((12, 10)), self.Enemy((13, 10))),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=(10, 10),
                team="attack",
            )
            self.assertEqual(action, "MOVE")
            self.assertIn("attack-retreat", detail)
            # Must move away from the cluster at x=12 (i.e. LEFT/negative x).
            self.assertEqual(unit.arg.name, "LEFT")
        finally:
            tactic._enemy_memory.discard((11, 10))
            self.tearDown_attack_retreat()

    def test_attack_retreat_forbidden_targets_skipped_in_autotarget(self) -> None:
        # The retreating squad's auto scorer must skip the forbidden cluster cell
        # and march on the next-best sighting instead.
        tactic._enemy_memory.update({(9, 0), (50, 50)})
        self._setup_attack_retreat(
            squad_pos=(10, 0), squad_size=1,
            cluster_centroid=(9, 0), forbidden=frozenset({(9, 0)}),
        )
        try:
            unit = self.CombatUnit("r-attack", (10, 0))
            self.config["attack_mode"] = "auto"
            action, detail = tactic._plan_ranger(
                unit,
                enemies=(),  # hit only the target-selection path here
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=(0, 0),
                team="attack",
            )
            self.assertEqual(action, "MOVE")
            self.assertIn("attack-march-auto (50, 50)", detail)
        finally:
            tactic._enemy_memory.discard((9, 0))
            tactic._enemy_memory.discard((50, 50))
            self.tearDown_attack_retreat()

    def test_attack_no_retreat_when_enemy_count_below_squad(self) -> None:
        # 2 squad members, only 1 nearby enemy threat => not outnumbered.
        decision = tactic._attack_retreat_decision(
            enemies=(self.Enemy((12, 10)),),
            squad_pos=(10, 10), squad_size=2, radius=5,
            enemy_memory={(12, 10)},
        )
        self.assertFalse(decision[0])
        self.assertEqual(decision[1], 1)

    def test_attack_retreat_ignores_enemy_workers(self) -> None:
        # 1 squad member, 1 enemy Vanguard + 3 enemy Workers within radius.
        # Workers can't attack, so only 1 combat threat vs 1 squad => not >,
        # and equal counts *do* retreat, but with only threats counted the
        # nearby combat count is 1 == squad 1 => retreat fires on the 1 threat.
        enemies = (
            self.Enemy((12, 10)),  # VANGUARD threat
            self.Enemy((11, 10)), self.Enemy((13, 10)), self.Enemy((10, 11)),
        )
        for e in enemies[1:]:
            e.unit_type = UnitType.WORKER
        decision = tactic._attack_retreat_decision(
            enemies=enemies,
            squad_pos=(10, 10), squad_size=1, radius=5,
            enemy_memory={(12, 10)},
        )
        self.assertTrue(decision[0])
        self.assertEqual(decision[1], 1)  # only the VANGUARD counts
        # Forbidden set is built from the threat cell only.
        self.assertIn((12, 10), decision[3])
        for w in ((11, 10), (13, 10), (10, 11)):
            self.assertNotIn(w, decision[3])

    def test_attack_retreat_radius_zero_disables(self) -> None:
        decision = tactic._attack_retreat_decision(
            enemies=(self.Enemy((10, 10)), self.Enemy((10, 11))),
            squad_pos=(10, 10), squad_size=1, radius=0,
            enemy_memory={(10, 10)},
        )
        self.assertFalse(decision[0])
        self.assertEqual(decision[1], 0)

    def test_guerrilla_uses_kite_evasion_when_face_to_face(self) -> None:
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

        self.assertEqual(action, "MOVE")
        self.assertIn("kite-evade", detail)

    def test_guerrilla_ranger_uses_kite_motion_prediction(self) -> None:
        ranger = self.CombatUnit("r-g-kite", (0, 0))
        enemy = self.Enemy((0, 3))
        tactic._enemy_motion_tracks[enemy.id] = [(3, (0, 4)), (4, (0, 3))]

        action, detail = tactic._plan_ranger(
            ranger,
            enemies=(enemy,),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="guerrilla",
        )

        self.assertEqual(action, "SHOOT")
        self.assertEqual(ranger.expected_cell, (0, 2))
        self.assertIn("kite-lead", detail)
        tactic.turn_context.shot_predictions.clear()

    def test_guerrilla_ignores_kite_team_directive(self) -> None:
        unit = self.CombatUnit("v-g-solo", (0, 0))
        tactic.turn_context.kite_directives[str(unit.id)] = {
            "kind": "sweep",
            "direction": Direction.RIGHT,
            "reason": "should-not-apply",
        }

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(),
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="guerrilla",
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("guerrilla-roam", detail)
        self.assertNotIn("should-not-apply", detail)
        tactic.turn_context.kite_directives.pop(str(unit.id), None)

    def test_guerrilla_attacks_core_without_treating_pack_as_danger(self) -> None:
        """A CORE and workers are safe targets, not attack threats."""
        unit = self.CombatUnit("v-g3", (5, 5))
        enemies = (
            SimpleNamespace(position=(4, 5), unit_type="CORE"),
            SimpleNamespace(position=(6, 5), unit_type="WORKER"),
            SimpleNamespace(position=(5, 4), unit_type="WORKER"),
        )

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=enemies,
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="guerrilla",
        )

        self.assertEqual(action, "SWEEP")
        self.assertIn("safe-sweep core", detail)

    def test_guerrilla_kites_three_threats_plus_worker(self) -> None:
        """Workers remain irrelevant while the combat threats are handled
        by the per-unit kite policy rather than a pack retreat."""
        unit = self.CombatUnit("v-g4", (5, 5))
        enemies = (
            SimpleNamespace(position=(6, 5), unit_type="VANGUARD"),
            SimpleNamespace(position=(5, 6), unit_type="VANGUARD"),
            SimpleNamespace(position=(6, 6), unit_type="VANGUARD"),
            SimpleNamespace(position=(4, 5), unit_type="WORKER"),
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
        self.assertIn("kite-evade", detail)

    def test_guerrilla_ignores_core_spotted_by_other_teammate(self) -> None:
        """A CORE inside a teammate's vision but far outside this unit's own
        sight must not drag this unit off its bearing — it keeps roaming."""
        unit = self.CombatUnit("v-g-far", (0, 0))
        enemies = (SimpleNamespace(position=(10, 0), unit_type="CORE"),)

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=enemies,
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="guerrilla",
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("guerrilla-roam", detail)

    def test_guerrilla_far_pack_does_not_trigger_retreat(self) -> None:
        """3 threats all beyond this unit's own sight do not count as a pack
        for this unit — the retreat threshold is local, not team-wide."""
        unit = self.CombatUnit("v-g-far2", (0, 0))
        enemies = (
            SimpleNamespace(position=(10, 0), unit_type="VANGUARD"),
            SimpleNamespace(position=(10, 1), unit_type="VANGUARD"),
            SimpleNamespace(position=(10, -1), unit_type="VANGUARD"),
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
        self.assertIn("guerrilla-roam", detail)

    def test_guerrilla_engage_radius_extends_sight(self) -> None:
        """guerrilla_engage_radius extends the independent target pool."""
        self.config["guerrilla_engage_radius"] = 10
        unit = self.CombatUnit("v-g-rad", (0, 0))
        enemies = (SimpleNamespace(position=(6, 0), unit_type="CORE"),)

        action, detail = tactic._plan_vanguard(
            unit,
            enemies=enemies,
            obstacle_cells=frozenset(),
            config=self.config,
            core_pos=(0, 0),
            team="guerrilla",
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("kite-route", detail)
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


class KiteTeamPlannerTests(unittest.TestCase):
    class Unit:
        def __init__(self, uid, position, unit_type, hp):
            self.id = uid
            self.position = position
            self.unit_type = unit_type
            self.hp = hp
            self.action = None
            self.arg = None
            self.expected_cell = None

        def move(self, direction):
            self.action = "MOVE"
            self.arg = direction

        def sweep(self, direction):
            self.action = "SWEEP"
            self.arg = direction

        def shoot(self, target, *, expected_cell=None):
            self.action = "SHOOT"
            self.arg = target
            self.expected_cell = expected_cell

        def wait(self):
            self.action = "WAIT"

    class Enemy:
        def __init__(self, uid, position, unit_type):
            self.id = uid
            self.position = position
            self.unit_type = unit_type

    def setUp(self):
        self.config = default_config()
        self.config.update({
            "kite_target_x": 10,
            "kite_target_y": 0,
            "kite_mode": "coords",
        })
        self.old_tracks = {
            key: list(value) for key, value in tactic._enemy_motion_tracks.items()
        }
        self.old_decisions = list(getattr(tactic.turn_context, "kite_decisions", []))
        self.old_directives = dict(getattr(tactic.turn_context, "kite_directives", {}))
        self.old_predictions = list(getattr(tactic.turn_context, "shot_predictions", []))
        self.old_beacon = getattr(tactic.turn_context, "beacon_pos", None)
        self.old_core = getattr(tactic.turn_context, "core_pos", None)
        self.old_kite_squad = getattr(tactic.turn_context, "kite_squad_pos", None)
        self.old_enemy_memory = set(tactic._enemy_memory)
        self.old_collision = dict(tactic._kite_collision_streak)
        tactic._enemy_motion_tracks.clear()
        tactic._kite_collision_streak.clear()
        tactic.turn_context.tick = 5
        tactic.turn_context.kite_decisions = []
        tactic.turn_context.kite_directives = {}
        tactic.turn_context.kite_obstacles = frozenset()
        tactic.turn_context.kite_ranger_range = 3
        tactic.turn_context.shot_predictions = []
        tactic.turn_context.beacon_pos = None
        tactic.turn_context.core_pos = (0, 0)
        tactic.turn_context.kite_squad_pos = (0, 0)

    def tearDown(self):
        tactic._enemy_motion_tracks.clear()
        tactic._enemy_motion_tracks.update(self.old_tracks)
        tactic._kite_collision_streak.clear()
        tactic._kite_collision_streak.update(self.old_collision)
        tactic.turn_context.kite_decisions = self.old_decisions
        tactic.turn_context.kite_directives = self.old_directives
        tactic.turn_context.shot_predictions = self.old_predictions
        tactic.turn_context.beacon_pos = self.old_beacon
        tactic.turn_context.core_pos = self.old_core
        tactic.turn_context.kite_squad_pos = self.old_kite_squad
        tactic._enemy_memory.clear()
        tactic._enemy_memory.update(self.old_enemy_memory)

    def enemy(self, uid, position, unit_type=UnitType.VANGUARD):
        return self.Enemy(uid, position, unit_type)

    def unit(self, uid, position, unit_type=UnitType.VANGUARD, hp=None):
        if hp is None:
            hp = 4 if unit_type == UnitType.VANGUARD else 2
        return self.Unit(uid, position, unit_type, hp)

    def test_motion_classifies_advance_and_retreat_from_previous_tick(self):
        enemy = self.enemy("enemy-motion", (2, 0))
        tactic._enemy_motion_tracks[enemy.id] = [(3, (3, 0)), (4, (2, 0))]
        self.assertEqual(tactic._kite_motion_info(enemy, (0, 0))["state"], "advance")

        enemy.position = (3, 0)
        tactic._enemy_motion_tracks[enemy.id] = [(3, (2, 0)), (4, (3, 0))]
        self.assertEqual(tactic._kite_motion_info(enemy, (0, 0))["state"], "retreat")

    def test_vanguard_prefires_straight_gap_against_advancing_vanguard(self):
        unit = self.unit("kite-v", (0, 0))
        enemy = self.enemy("enemy-v", (2, 0))
        tactic._enemy_motion_tracks[enemy.id] = [(3, (3, 0)), (4, (2, 0))]

        action, detail = tactic._plan_vanguard(
            unit, (enemy,), frozenset(), self.config,
            core_pos=(0, 0), team="kite",
        )

        self.assertEqual(action, "SWEEP")
        self.assertEqual(unit.arg, Direction.RIGHT)
        self.assertIn("prefire-gap", detail)

    def test_vanguard_uses_enemy_axis_habit_for_diagonal_prefire(self):
        unit = self.unit("kite-v", (0, 0))
        enemy = self.enemy("enemy-diag", (1, 1))
        # Repeated horizontal approach means the enemy habitually enters the
        # vertical bridge (0,1), so sweep DOWN rather than RIGHT.
        tactic._enemy_motion_tracks[enemy.id] = [
            (2, (3, 1)), (3, (2, 1)), (4, (1, 1)),
        ]

        action, detail = tactic._plan_vanguard(
            unit, (enemy,), frozenset(), self.config,
            core_pos=(0, 0), team="kite",
        )

        self.assertEqual(action, "SWEEP")
        self.assertEqual(unit.arg, Direction.DOWN)
        self.assertIn("prefire-diagonal", detail)

    def test_face_to_face_vanguard_moves_instead_of_trading(self):
        unit = self.unit("kite-v", (0, 0))
        enemy = self.enemy("enemy-face", (1, 0))
        tactic._enemy_motion_tracks[enemy.id] = [(3, (2, 0)), (4, (1, 0))]

        action, detail = tactic._plan_vanguard(
            unit, (enemy,), frozenset(), self.config,
            core_pos=(0, 0), team="kite",
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("kite-evade", detail)
        self.assertNotEqual(unit.arg, Direction.RIGHT)

    def test_vanguard_advances_on_stationary_enemy_when_safe(self):
        unit = self.unit("kite-v", (0, 0))
        enemy = self.enemy("enemy-stationary", (3, 0))
        tactic._enemy_motion_tracks[enemy.id] = [(3, (3, 0)), (4, (3, 0))]

        action, detail = tactic._plan_vanguard(
            unit, (enemy,), frozenset(), self.config,
            core_pos=(0, 0), team="kite",
        )

        self.assertEqual(action, "MOVE")
        self.assertEqual(unit.arg, Direction.RIGHT)
        self.assertIn("kite-route", detail)

    def test_ranger_leads_both_advancing_and_retreating_vanguards(self):
        ranger = self.unit("kite-r1", (0, 0), UnitType.RANGER)
        advancing = self.enemy("advance-v", (0, 3))
        tactic._enemy_motion_tracks[advancing.id] = [(3, (0, 4)), (4, (0, 3))]
        action, _ = tactic._plan_ranger(
            ranger, (advancing,), frozenset(), self.config,
            core_pos=(0, 0), team="kite",
        )
        self.assertEqual(action, "SHOOT")
        self.assertEqual(ranger.expected_cell, (0, 2))

        ranger = self.unit("kite-r2", (0, 0), UnitType.RANGER)
        retreating = self.enemy("retreat-v", (0, 2))
        tactic._enemy_motion_tracks[retreating.id] = [(3, (0, 1)), (4, (0, 2))]
        action, _ = tactic._plan_ranger(
            ranger, (retreating,), frozenset(), self.config,
            core_pos=(0, 0), team="kite",
        )
        self.assertEqual(action, "SHOOT")
        self.assertEqual(ranger.expected_cell, (0, 3))

    def test_vanguard_sweeps_contested_cell_after_two_move_collisions(self):
        # Two units moving into the same cell on the same tick both fail to
        # advance (server cancels both). After two such cancelled attempts at
        # the same cell, the enemy's next move into that cell is predictable:
        # attack the cell directly instead of retrying the doomed move.
        #
        # Vanguard at (0,0), enemy Ranger at (1,1) (diagonal). The Vanguard
        # wants to enter (1,0) toward the Ranger. Simulate two collisions by
        # pre-seeding the streak: same pos (0,0), same contested cell (1,0),
        # count=2. The planner must SWEEP (1,0), not MOVE.
        vanguard = self.unit("kite-v-collide", (0, 0), hp=4)
        enemy = self.enemy("enemy-r-diag", (1, 1), UnitType.RANGER)
        tactic._enemy_motion_tracks[enemy.id] = [(3, (2, 1)), (4, (1, 1))]
        tactic._kite_collision_streak.clear()
        tactic._kite_collision_streak[str(vanguard.id)] = ((0, 0), (1, 0), 2)

        action, detail = tactic._plan_vanguard(
            vanguard, (enemy,), frozenset(), self.config,
            core_pos=(0, 0), team="kite",
        )

        self.assertEqual(action, "SWEEP")
        self.assertEqual(vanguard.arg, Direction.RIGHT)
        self.assertIn("kite-collision-prefire", detail)
        # The streak is cleared once the predictive attack fires.
        self.assertNotIn(str(vanguard.id), tactic._kite_collision_streak)

    def test_ranger_shoots_contested_cell_after_two_move_collisions(self):
        # Same collision rule as the Vanguard test, but for a Ranger: after
        # two cancelled move attempts into the same cell, shoot_cell that cell
        # instead of retrying the doomed move (movement resolves before combat,
        # so the shot lands on the enemy's post-move cell).
        ranger = self.unit("kite-r-collide", (0, 0), UnitType.RANGER)
        enemy = self.enemy("enemy-v", (0, 1), UnitType.VANGUARD)
        tactic._enemy_motion_tracks[enemy.id] = [(3, (0, 2)), (4, (0, 1))]
        tactic._kite_collision_streak.clear()
        tactic._kite_collision_streak[str(ranger.id)] = ((0, 0), (0, -1), 2)

        action, detail = tactic._plan_ranger(
            ranger, (enemy,), frozenset(), self.config,
            core_pos=(0, 0), team="kite",
        )

        self.assertEqual(action, "SHOOT")
        self.assertIn("kite-lead", detail)
        self.assertEqual(ranger.expected_cell, (0, -1))
        self.assertNotIn(str(ranger.id), tactic._kite_collision_streak)

    def test_ranger_without_target_avoids_contested_cell_instead_of_shooting(self):
        ranger = self.unit("kite-r-no-target", (0, 0), UnitType.RANGER)
        tactic._kite_collision_streak[str(ranger.id)] = (
            (0, 0), (1, 0), 2,
        )

        action, detail = tactic._plan_ranger(
            ranger, (), frozenset(), self.config,
            core_pos=(0, 0), team="kite",
        )

        self.assertEqual(action, "MOVE")
        self.assertNotEqual(ranger.arg, Direction.RIGHT)
        # Review fix (restored contract): a contested move without a visible
        # target AND without a stacked ally is a single-unit case: avoid the
        # contested cell for one tick and keep the full safety-assessed route
        # planning — never an unevaluated sideways split step.
        self.assertIn("kite-route", detail)
        self.assertNotIn(str(ranger.id), tactic._kite_friendly_split)

    def test_guerrilla_roam_avoids_repeatedly_contested_cell(self):
        ranger = self.unit("guerrilla-r-contested", (0, 0), UnitType.RANGER)
        roam_goal = tactic._guerrilla_roam_goal(ranger, (0, 0))
        preferred = tactic._step_towards((0, 0), roam_goal)
        contested = preferred.delta
        tactic._kite_collision_streak[str(ranger.id)] = (
            (0, 0), contested, 2,
        )

        action, detail = tactic._plan_ranger(
            ranger, (), frozenset(), self.config,
            core_pos=(0, 0), team="guerrilla",
        )

        self.assertEqual(action, "MOVE")
        next_cell = ranger.arg.delta
        self.assertNotEqual(next_cell, contested)
        # Review fix (restored contract): with no stacked ally this stays a
        # single-unit case — the contested exit is avoided for one tick and
        # the normal safety-assessed bearing roam picks a different cell.
        self.assertIn("guerrilla-roam", detail)
        self.assertNotIn(str(ranger.id), tactic._kite_friendly_split)

    def test_kite_route_avoids_enemy_range_and_full_friendly_cell(self):
        ranger = self.unit("kite-r-safe-route", (0, 0), UnitType.RANGER)
        enemy = self.enemy("enemy-r-route", (0, 4), UnitType.RANGER)

        action, detail = tactic._plan_ranger(
            ranger, (enemy,), frozenset(), self.config,
            core_pos=(0, 0), team="kite",
            cell_counts={(0, 1): tactic._CELL_UNIT_LIMIT},
        )

        self.assertEqual(action, "MOVE")
        self.assertNotEqual(ranger.arg, Direction.DOWN)
        next_cell = (
            ranger.position[0] + ranger.arg.delta[0],
            ranger.position[1] + ranger.arg.delta[1],
        )
        assessment = tactic._kite_cell_assessment(
            next_cell, (enemy,), frozenset(), 3,
        )
        self.assertEqual(assessment["current_hits"], 0)
        self.assertIn("kite-route", detail)

    def test_kite_attack_clears_stale_move_attempt(self):
        ranger = self.unit("kite-r-clear", (0, 0), UnitType.RANGER)
        enemy = self.enemy("enemy-worker", (2, 0), UnitType.WORKER)
        tactic._kite_collision_streak[str(ranger.id)] = (
            (0, 0), (1, 0), 1,
        )

        action, _ = tactic._plan_ranger(
            ranger, (enemy,), frozenset(), self.config,
            core_pos=(0, 0), team="kite",
        )

        self.assertEqual(action, "SHOOT")
        self.assertNotIn(str(ranger.id), tactic._kite_collision_streak)

    def test_rejected_plan_rolls_back_collision_state(self):
        unit = self.unit("kite-rejected", (0, 0))
        tactic._kite_collision_streak[str(unit.id)] = (
            (0, 0), (1, 0), 1,
        )
        tactic.turn_context.kite_collision_snapshot = dict(
            tactic._kite_collision_streak
        )
        tactic._kite_record_move_attempt(unit, (0, 0), (1, 0))
        self.assertEqual(tactic._kite_collision_streak[str(unit.id)][2], 2)

        tactic._finalize_kite_collision_state(False)

        self.assertEqual(tactic._kite_collision_streak[str(unit.id)][2], 1)

    def test_cell_limit_failure_is_not_counted_as_collision(self):
        unit = self.unit("kite-cell-limit", (0, 0))
        tactic._kite_collision_streak[str(unit.id)] = (
            (0, 0), (1, 0), 1,
        )
        turn = SimpleNamespace(events=(SimpleNamespace(
            event_type="UNIT_MOVE_FAILED",
            reason_code="CELL_UNIT_LIMIT",
            actor_id=unit.id,
        ),))

        tactic._discard_noncollision_move_failures(turn)

        self.assertNotIn(str(unit.id), tactic._kite_collision_streak)

    def test_ranger_lead_fires_when_cornered_by_vanguard_cannot_escape(self):
        # Regression: a Ranger cornered by a melee Vanguard used to flee
        # every tick. The Vanguard chases one cell per tick, the Ranger can
        # only retreat one cell, so the gap stays 1 — the Ranger dies having
        # never shot. When every escape cell still predicts a hit, the Ranger
        # must stand and lead-fire the enemy's next cell instead.
        #
        # Ranger at (0,0); enemy Vanguard advanced (0,2)->(0,1) toward it,
        # so velocity=(0,-1), predicted next cell=(0,0). Down is walled so
        # the only retreat is UP to (0,-1), but the Vanguard's predicted cell
        # (0,0) still hits (0,-1) -> predicted_hits=1, i.e. fleeing fails.
        ranger = self.unit("kite-r-cornered", (0, 0), UnitType.RANGER)
        enemy = self.enemy("chasing-v", (0, 1), UnitType.VANGUARD)
        tactic._enemy_motion_tracks[enemy.id] = [(3, (0, 2)), (4, (0, 1))]
        walls = frozenset({(1, 0), (-1, 0)})  # block sideways, force vertical

        action, detail = tactic._plan_ranger(
            ranger, (enemy,), walls, self.config,
            core_pos=(0, 0), team="kite",
        )

        self.assertEqual(action, "SHOOT")
        # The enemy's predicted cell is the Ranger's own cell (distance 0),
        # so lead-fire is illegal and it falls back to the current cell.
        self.assertIn("kite-current", detail)

    def test_ranger_in_enemy_ranger_range_moves_before_shooting(self):
        ranger = self.unit("kite-r", (0, 0), UnitType.RANGER)
        enemy = self.enemy("enemy-r", (0, 3), UnitType.RANGER)
        tactic._enemy_motion_tracks[enemy.id] = [(3, (0, 4)), (4, (0, 3))]

        action, detail = tactic._plan_ranger(
            ranger, (enemy,), frozenset(), self.config,
            core_pos=(0, 0), team="kite",
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("kite-evade", detail)
        self.assertNotEqual(ranger.arg, Direction.DOWN)

    def test_full_health_vanguard_keeps_moving_toward_ranger(self):
        vanguard = self.unit("kite-v", (0, 0), hp=4)
        enemy = self.enemy("enemy-r", (0, 3), UnitType.RANGER)

        action, detail = tactic._plan_vanguard(
            vanguard, (enemy,), frozenset(), self.config,
            core_pos=(0, 0), team="kite",
        )

        self.assertEqual(action, "MOVE")
        self.assertEqual(vanguard.arg, Direction.DOWN)
        self.assertIn("moving-dodge", detail)

    def test_nv1_prepares_collision_pin_and_current_cell_shot(self):
        vanguard = self.unit("vanguard-pin", (0, 0))
        ranger = self.unit("ranger-pin", (2, 3), UnitType.RANGER)
        enemy = self.enemy("enemy-pin", (2, 0))
        tactic._enemy_motion_tracks[enemy.id] = [(3, (3, 0)), (4, (2, 0))]
        self.config["kite_team"] = "V1, R1"
        with patch.object(
            tactic,
            "_object_name",
            side_effect=lambda uid, prefix: "V1" if str(uid).startswith("vanguard") else "R1",
        ):
            directives = tactic._prepare_kite_coordination(
                (vanguard, ranger), (enemy,), self.config, frozenset(),
            )

        self.assertEqual(directives[vanguard.id]["kind"], "move")
        self.assertEqual(directives[vanguard.id]["direction"], Direction.RIGHT)
        self.assertEqual(directives[ranger.id]["kind"], "shoot_current")

    def test_two_stacked_vanguards_cover_both_diagonal_cells(self):
        first = self.unit("vanguard-a", (0, 0))
        second = self.unit("vanguard-b", (0, 0))
        enemy = self.enemy("enemy-diag", (1, 1))
        tactic._enemy_motion_tracks[enemy.id] = [(3, (2, 1)), (4, (1, 1))]
        self.config["kite_team"] = "V1, V2"
        with patch.object(
            tactic,
            "_object_name",
            side_effect=lambda uid, prefix: "V1" if str(uid).endswith("a") else "V2",
        ):
            directives = tactic._prepare_kite_coordination(
                (first, second), (enemy,), self.config, frozenset(),
            )

        directions = {directives[first.id]["direction"], directives[second.id]["direction"]}
        self.assertEqual(directions, {Direction.RIGHT, Direction.DOWN})

    def test_kite_planner_ignores_attack_squad_retreat_verdict(self):
        unit = self.unit("kite-v", (0, 0))
        tactic.turn_context.attack_retreat = True
        tactic.turn_context.attack_retreat_from = (1, 0)
        try:
            action, detail = tactic._plan_vanguard(
                unit, (), frozenset(), self.config,
                core_pos=(0, 0), team="kite",
            )
        finally:
            tactic.turn_context.attack_retreat = False
            tactic.turn_context.attack_retreat_from = None

        self.assertEqual(action, "MOVE")
        self.assertNotIn("attack-retreat", detail)

    def test_kite_beacon_and_auto_modes_choose_their_own_targets(self):
        unit = self.unit("kite-v", (0, 0))
        self.config["kite_mode"] = "beacon"
        tactic.turn_context.beacon_pos = (0, 5)
        action, detail = tactic._plan_vanguard(
            unit, (), frozenset(), self.config,
            core_pos=(0, 0), team="kite",
        )
        self.assertEqual(action, "MOVE")
        self.assertEqual(unit.arg, Direction.DOWN)
        self.assertIn("kite-route (0, 5)", detail)

        unit = self.unit("kite-v-auto", (0, 0))
        self.config["kite_mode"] = "auto"
        tactic._enemy_memory.clear()
        tactic._enemy_memory.add((0, 4))
        action, detail = tactic._plan_vanguard(
            unit, (), frozenset(), self.config,
            core_pos=(0, 0), team="kite",
        )
        self.assertEqual(action, "MOVE")
        self.assertEqual(unit.arg, Direction.DOWN)
        self.assertIn("kite-route (0, 4)", detail)

    def test_kite_core_is_a_target_but_not_a_danger_zone(self):
        unit = self.unit("kite-v-core", (0, 0))
        core = self.enemy("enemy-core", (1, 0), "CORE")
        action, detail = tactic._plan_vanguard(
            unit, (core,), frozenset(), self.config,
            core_pos=(0, 0), team="kite",
        )
        self.assertEqual(action, "SWEEP")
        self.assertEqual(unit.arg, Direction.RIGHT)
        self.assertIn("core", detail)

    def test_battle_log_is_contact_only_and_one_row_per_tick(self):
        no_contact = [{
            "unit_name": "V1", "action": "MOVE", "reason": "kite-position",
            "enemies": [],
        }]
        self.assertEqual(tactic._kite_battle_log_entries(no_contact, 5), [])

        contact = [
            {
                "unit_name": "V1", "action": "MOVE", "reason": "kite-evade",
                "enemies": [{"id": "enemy-1"}],
            },
            {
                "unit_name": "R1", "action": "SHOOT", "reason": "kite-current",
                "enemies": [{"id": "enemy-1"}],
            },
        ]
        entries = tactic._kite_battle_log_entries(contact, 5)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tick"], 5)
        self.assertIn("单位 2/2", entries[0]["msg"])
        self.assertIn("MOVE=1", entries[0]["msg"])
        self.assertIn("SHOOT=1", entries[0]["msg"])

    def test_battle_log_writes_only_after_accepted_submission(self):
        tactic.turn_context.kite_decisions = [{
            "unit_name": "R1", "action": "SHOOT", "reason": "kite-current",
            "enemies": [{"id": "enemy-1"}],
        }]
        with patch.object(tactic, "append_jsonl") as append_mock:
            tactic._append_accepted_kite_battle_log(False)
            append_mock.assert_not_called()

            tactic._append_accepted_kite_battle_log(True)
            append_mock.assert_called_once()
            self.assertEqual(len(append_mock.call_args.args[1]), 1)

    def test_kite_unit_escapes_dead_end_pocket_via_bfs_unstick(self):
        # Regression: a kite unit whose only exit steps *away* from the
        # objective used to WAIT forever. _kite_choose_move is single-step
        # and scores on goal progress, so any detour step (progress=-1) loses
        # to WAIT (progress=0) even when the detour is the start of a valid
        # A* route. The BFS unstick fallback must take the first A* step
        # toward the objective instead of stalling.
        #
        # Map: unit at (0,0), objective at (10,0) (far right). RIGHT and UP
        # are walled; only DOWN (a progress=-1 detour) and LEFT are open.
        # Without the fallback the unit WAITed forever; with it the unit steps
        # DOWN, the first cell of the A* route that loops back to the goal.
        unit = self.unit("kite-stuck", (0, 0))
        walls = frozenset({(1, 0), (0, -1)})
        # Force the dead-end cache to recompute for this obstacle set.
        saved_obstacles = tactic._dead_obstacles
        tactic._dead_obstacles = None
        try:
            action, detail = tactic._plan_kite_combat(
                unit,
                unit_kind="vanguard",
                enemies=(),
                obstacle_cells=walls,
                config=self.config,
                cell_counts={},
            )
        finally:
            tactic._dead_obstacles = saved_obstacles

        self.assertEqual(action, "MOVE")
        self.assertIn("kite-route", detail)
        self.assertEqual(unit.action, "MOVE")
        self.assertEqual(unit.arg, Direction.DOWN)


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

    def test_svg_unit_markers_carry_data_unit_only_for_own_units(self) -> None:
        """Manual-target map-pick needs the clicked marker to identify its unit.
        Own units (W/V/R) carry data-unit; enemies and the core must not, so a
        stray click on them cannot pick a phantom manual-target name."""
        rec = {
            "core_pos": [0, 0],
            "core_name": "C1",
            "workers": [{"name": "W1", "pos": [0, 1]}],
            "vanguards": [{"name": "V2", "pos": [1, 2]}],
            "rangers": [{"name": "R3", "pos": [2, 2]}],
            "enemies": [{"name": "E9", "pos": [3, 3]}],
            "resource_cells": [],
        }
        memory = {"obstacles": [], "resources": []}

        svg = dashboard.render_svg(rec, memory)

        for name in ("W1", "V2", "R3"):
            self.assertIn(f'data-unit="{name}"', svg)
        self.assertNotIn('data-unit="E9"', svg)
        self.assertNotIn('data-unit="C1"', svg)

    def test_unit_display_names_maps_short_ids_to_dashboard_names(self) -> None:
        """The arena SPA looks up tactic display names by short unit id so it
        labels units exactly like the dashboard (W1/V2/R3)."""
        rec = {
            "workers": [
                {"id": "aaaaaaaa-1111", "name": "W1"},
                {"id": "", "name": "W2"},
                {"id": "cccccccc-3333", "name": ""},
                "not-a-dict",
            ],
            "vanguards": [{"id": "bbbbbbbb-2222", "name": "V1"}],
            "rangers": [{"id": "dddddddd-4444", "name": "R1"}],
            "enemies": [{"id": "eeeeeeee-5555", "name": "E1"}],
        }

        names = dashboard.unit_display_names(rec)

        self.assertEqual(names, {
            "aaaaaaaa-1111": "W1",
            "bbbbbbbb-2222": "V1",
            "dddddddd-4444": "R1",
        })
        self.assertEqual(dashboard.unit_display_names(None), {})
        self.assertEqual(dashboard.unit_display_names({}), {})

    def test_svg_route_is_trimmed_to_unwalked_remainder(self) -> None:
        """Routes only draw the segment ahead of the unit, not the ground it
        has already crossed. A unit mid-path must not re-render the cells it
        already walked through."""
        rec = {
            "core_pos": [0, 0],
            "core_name": "C1",
            "workers": [{
                "name": "W1", "pos": [1, 0],
                "target": [3, 0],
                "path": [[0, 0], [1, 0], [2, 0], [3, 0]],
                "path_complete": True,
            }],
            "vanguards": [], "rangers": [], "enemies": [],
            "resource_cells": [],
        }
        memory = {"obstacles": [], "resources": []}
        svg = dashboard.render_svg(rec, memory)

        m = re.search(
            r'<polyline class="worker-route"[^>]*points="([^"]+)"', svg
        )
        self.assertIsNotNone(m, "worker route polyline should be drawn")
        points = m.group(1).split()

        # Decode the polyline back to grid cells via the SVG's own viewport
        # attributes, so this does not depend on cell/pad/min-map-size internals.
        xmin = int(re.search(r'data-xmin="(-?\d+)"', svg).group(1))
        ymin = int(re.search(r'data-ymin="(-?\d+)"', svg).group(1))
        cell = int(re.search(r'data-cell="(\d+)"', svg).group(1))
        pad = int(re.search(r'data-pad="(\d+)"', svg).group(1))

        def center(gx: int, gy: int) -> str:
            return (f"{pad + (gx - xmin) * cell + cell / 2:.1f},"
                    f"{pad + (gy - ymin) * cell + cell / 2:.1f}")

        # Trimmed polyline starts at the unit's current cell (1,0) and never
        # re-draws the already-walked origin (0,0).
        self.assertEqual(points[0], center(1, 0))
        self.assertNotIn(center(0, 0), points)

    def test_svg_elements_are_tagged_for_map_filters(self) -> None:
        """Every drawable category carries a data-cat so legend toggles can
        show/hide it client-side."""
        rec = {
            "core_pos": [0, 0],
            "core_name": "C1",
            "beacon_pos": [1, 1],
            "workers": [{
                "name": "W1", "pos": [1, 0], "cargo": 0,
                "path": [[0, 0], [1, 0], [2, 0]], "path_complete": True,
                "target": [2, 0],
            }],
            "vanguards": [{"name": "V1", "pos": [1, 2]}],
            "rangers": [{"name": "R1", "pos": [2, 2]}],
            "enemies": [{"name": "E1", "pos": [3, 2]}],
            "resource_cells": [[4, 4]],
        }
        memory = {
            "obstacles": [[5, 5]],
            "resources": [[6, 6]],
            "enemy_sightings": [[7, 7]],
        }
        svg = dashboard.render_svg(rec, memory, waypoints={"W1": [2, 0]})

        for cat, minimum in (
            ("core", 1), ("worker", 1), ("vanguard", 1), ("ranger", 1),
            ("enemy", 1), ("enemy-trace", 1), ("wall", 1), ("ore", 1),
            ("ore-mem", 1), ("route", 1), ("target", 1), ("beacon", 1),
            ("wp", 1),
        ):
            self.assertGreaterEqual(
                svg.count(f'data-cat="{cat}"'), minimum,
                f"category {cat} should be tagged in the map SVG",
            )

    def test_svg_enemy_type_is_filtered_per_type(self) -> None:
        """Visible enemies carry their unit type and are tagged with a per-type
        category (enemy-worker / enemy-vanguard / enemy-ranger / enemy-core /
        enemy) so the legend can hide each enemy class independently."""
        rec = {
            "core_pos": [0, 0],
            "core_name": "C1",
            "workers": [], "vanguards": [], "rangers": [],
            "resource_cells": [],
            "enemies": [
                {"name": "E1", "pos": [1, 0], "type": "WORKER"},
                {"name": "E2", "pos": [2, 0], "type": "VANGUARD"},
                {"name": "E3", "pos": [3, 0], "type": "RANGER"},
                {"name": "E4", "pos": [4, 0], "type": "CORE"},
                {"name": "E5", "pos": [5, 0], "type": None},
            ],
        }
        svg = dashboard.render_svg(rec, {"obstacles": [], "resources": []})

        for cat in ("enemy-worker", "enemy-vanguard", "enemy-ranger",
                    "enemy-core", "enemy"):
            self.assertGreaterEqual(
                svg.count(f'data-cat="{cat}"'), 2,
                f"enemy category {cat} should be tagged",
            )

        # A live enemy HQ has its own diamond silhouette, rather than looking
        # like another circular enemy unit with only a different label.
        self.assertIn('data-marker="enemy-core-live"', svg)
        self.assertRegex(
            svg,
            r'<polygon data-cat="enemy-core" data-marker="enemy-core-live"',
        )

    def test_svg_remembered_enemy_core_uses_star_diamond_marker(self) -> None:
        """Unknown enemy and HQ memories must remain obvious at tiny scale."""
        rec = {
            "core_pos": [0, 0], "core_name": "C1",
            "workers": [], "vanguards": [], "rangers": [], "enemies": [],
            "resource_cells": [],
        }
        memory = {
            "obstacles": [], "resources": [],
            "enemy_sightings": [[2, 0], [4, 0, "CORE"]],
        }

        svg = dashboard.render_svg(rec, memory)

        self.assertIn('data-marker="enemy-core-memory"', svg)
        self.assertIn(">★</text>", svg)
        self.assertIn(">敌</text>", svg)
        self.assertEqual(dashboard._enemy_type_char("CORE"), "★")

    def test_svg_same_cell_enemy_keeps_highest_priority_type(self) -> None:
        """When a worker stands on the enemy CORE's own cell, only the CORE
        marker is drawn there — the blue worker must not cover the enemy HQ."""
        rec = {
            "core_pos": [0, 0],
            "core_name": "C1",
            "workers": [], "vanguards": [], "rangers": [],
            "resource_cells": [],
            "enemies": [
                {"name": "E1", "pos": [3, 0], "type": "CORE"},
                {"name": "E2", "pos": [3, 0], "type": "WORKER"},
                {"name": "E3", "pos": [5, 0], "type": "WORKER"},
            ],
        }
        svg = dashboard.render_svg(rec, {"obstacles": [], "resources": []})

        # The shared cell renders exactly one enemy marker, and it is the core.
        self.assertGreaterEqual(svg.count('data-cat="enemy-core"'), 1)
        self.assertEqual(svg.count(">E1</text>"), 1)
        # The worker duplicated on the HQ cell is suppressed; the other worker
        # (its own cell) is still drawn.
        self.assertGreaterEqual(svg.count('data-cat="enemy-worker"'), 1)
        self.assertEqual(svg.count(">E2</text>"), 0)
        self.assertEqual(svg.count(">E3</text>"), 1)

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
        state = SimpleNamespace(population=5)
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


class IncrementalDeadEndTests(unittest.TestCase):
    """Equivalence of the persistent incremental dead-end structure vs the batch.

    Mirrors the session's 300-random-map equivalence check: the incremental
    wall-add path (_dead_add_walls) and the extras derivation must produce the
    same dead-end set as a full from-scratch _dead_end_cells. Saves/restores the
    persistent-structure globals so tests never leak state across methods.
    """

    GLOBALS = (
        "_dead_obstacles", "_dead_open_count", "_dead_set", "_dead_view",
        "_dead_structure_built", "_known_obstacles", "_obstacle_memory",
        "_dead_end_cache_key", "_dead_end_cache", "_path_blockers_union_key",
        "_path_blockers_union",
    )

    def _snapshot(self) -> dict:
        return {name: getattr(tactic, name) for name in self.GLOBALS}

    def _restore(self, snap: dict) -> None:
        for name, val in snap.items():
            setattr(tactic, name, val)

    def setUp(self) -> None:
        self._snap = self._snapshot()

    def tearDown(self) -> None:
        self._restore(self._snap)

    def _fresh_map(self, walls: set) -> None:
        """Reset the persistent structure to cover exactly `walls`."""
        tactic._obstacle_memory = set(walls)
        tactic._known_obstacles = frozenset()
        tactic._reset_dead_structure(frozenset())
        tactic._dead_end_cache_key = None
        tactic._dead_end_cache = frozenset()
        tactic._path_blockers_union_key = None
        tactic._path_blockers_union = frozenset()

    def test_incremental_wall_add_matches_batch(self) -> None:
        import random
        random.seed(42)
        walls: set = set()
        self._fresh_map(walls)
        for step in range(200):
            for _ in range(random.randint(1, 3)):
                walls.add((random.randint(-15, 15), random.randint(-15, 15)))
            known = tactic._update_obstacle_memory(
                SimpleNamespace(obstacle_cells=walls)
            )
            inc = tactic._get_dead_ends(known)
            batch = tactic._dead_end_cells(frozenset(walls))
            self.assertEqual(inc, batch,
                             f"step {step}: incremental != batch "
                             f"(only_inc={len(inc - batch)} only_batch={len(batch - inc)})")

    def test_incremental_wall_add_lands_on_dead_end_matches_batch(self) -> None:
        """Walls landing on existing free dead-end cells exercise the was_dead path."""
        import random
        random.seed(99)
        walls: set = set()
        self._fresh_map(walls)
        for step in range(120):
            for _ in range(8):
                walls.add((random.randint(-8, 8), random.randint(-8, 8)))
            dead_now = tactic._get_dead_ends(
                tactic._known_obstacles
            ) if tactic._known_obstacles else frozenset()
            for c in list(dead_now)[:2]:
                walls.add(c)  # a wall lands where a dead-end free cell was
            known = tactic._update_obstacle_memory(
                SimpleNamespace(obstacle_cells=walls)
            )
            inc = tactic._get_dead_ends(known)
            batch = tactic._dead_end_cells(frozenset(walls))
            self.assertEqual(inc, batch, f"step {step}: was_dead path diverged")

    def test_extras_derivation_matches_batch(self) -> None:
        import random
        random.seed(7)
        walls: set = set()
        for _ in range(300):
            walls.add((random.randint(-15, 15), random.randint(-15, 15)))
        self._fresh_map(walls)
        tactic._update_obstacle_memory(SimpleNamespace(obstacle_cells=walls))
        for _ in range(150):
            extras = {
                (random.randint(-15, 15), random.randint(-15, 15))
                for _ in range(random.randint(1, 40))
            }
            inc = tactic._get_dead_ends(
                tactic._known_obstacles, extras=frozenset(extras)
            )
            batch = tactic._dead_end_cells(frozenset(walls) | frozenset(extras))
            self.assertEqual(inc, batch,
                             f"extras derivation != batch "
                             f"(only_inc={len(inc - batch)} only_batch={len(batch - inc)})")

    def test_extras_does_not_mutate_persistent_structure(self) -> None:
        walls = {(1, 0), (2, 0), (0, 1), (3, 0)}
        self._fresh_map(walls)
        tactic._update_obstacle_memory(SimpleNamespace(obstacle_cells=walls))
        tactic._get_dead_ends(tactic._known_obstacles)  # build structure
        view_before = tactic._dead_view
        oc_before = dict(tactic._dead_open_count)
        tactic._get_dead_ends(
            tactic._known_obstacles, extras=frozenset({(0, 0), (1, 1)})
        )
        self.assertIs(tactic._dead_view, view_before,
                      "extras call mutated the cached _dead_view")
        self.assertEqual(tactic._dead_open_count, oc_before,
                         "extras call mutated _dead_open_count")

    def test_bfs_path_extras_equals_folded_union(self) -> None:
        import random
        random.seed(11)
        walls: set = set()
        for _ in range(200):
            walls.add((random.randint(-15, 15), random.randint(-15, 15)))
        self._fresh_map(walls)
        tactic._update_obstacle_memory(SimpleNamespace(obstacle_cells=walls))
        known = tactic._known_obstacles
        mism = 0
        for _ in range(120):
            extras = {
                (random.randint(-15, 15), random.randint(-15, 15))
                for _ in range(random.randint(0, 12))
            }
            start = (random.randint(-12, 12), random.randint(-12, 12))
            goal = (random.randint(-12, 12), random.randint(-12, 12))
            if goal in extras:
                continue
            p1 = tactic._bfs_path(start, goal, known, extras=frozenset(extras))
            p2 = tactic._bfs_path(start, goal, known | frozenset(extras))
            self.assertEqual(p1 or [], p2 or [], f"extras != folded for {start}->{goal}")
            mism += 1
        self.assertGreater(mism, 0)

    def test_goal_in_extras_stays_blocked(self) -> None:
        walls = {(1, 0), (2, 0), (0, 1)}
        self._fresh_map(walls)
        tactic._update_obstacle_memory(SimpleNamespace(obstacle_cells=walls))
        known = tactic._known_obstacles
        # goal cell is occupied by an "other" unit this tick: must not be entered
        for goal in ((0, 0), (0, -1), (1, 1)):
            p1 = tactic._bfs_path((2, 2), goal, known, extras=frozenset({goal}))
            p2 = tactic._bfs_path((2, 2), goal, known | frozenset({goal}))
            self.assertEqual(p1 or [], p2 or [],
                             f"goal {goal} in extras: extras path != folded union")

    def test_known_obstacles_stable_and_self_healing(self) -> None:
        walls = {(-1, 0), (0, 1), (1, 0)}
        self._fresh_map(walls)
        t = SimpleNamespace(obstacle_cells=walls)
        known1 = tactic._update_obstacle_memory(t)
        known2 = tactic._update_obstacle_memory(t)
        self.assertIs(known1, known2, "no-growth tick must return the same object")
        # Same-cardinality out-of-band replacement must self-heal on next call.
        # The old length-only check missed this and kept the cul-de-sac at (0, 0).
        replacement = {(10, 10), (20, 20), (30, 30)}
        tactic._obstacle_memory.clear()
        tactic._obstacle_memory.update(replacement)
        known3 = tactic._update_obstacle_memory(
            SimpleNamespace(obstacle_cells=replacement)
        )
        self.assertEqual(known3, frozenset(replacement))
        self.assertEqual(tactic._get_dead_ends(known3),
                         tactic._dead_end_cells(frozenset(replacement)))
        self.assertNotIn((0, 0), tactic._get_dead_ends(known3))

    def test_load_map_memory_resets_structure(self) -> None:
        walls = {(0, 1), (1, 0), (1, 1), (2, 0)}
        self._fresh_map(walls)
        tactic._update_obstacle_memory(SimpleNamespace(obstacle_cells=walls))
        tactic._get_dead_ends(tactic._known_obstacles)
        self.assertEqual(tactic._dead_obstacles, tactic._known_obstacles)
        # simulate a load of a different wall set from a real map_memory.json
        new_walls = {(5, 5), (5, 6), (6, 5)}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "map_memory.json"
            path.write_text(json.dumps({
                "obstacles": [list(p) for p in sorted(new_walls)],
                "resources": [],
                "manual_resources": [],
                "forgotten_resources": [],
                "enemy_sightings": [],
                "enemy_clear_seq": 0,
            }), encoding="utf-8")
            with patch.object(tactic, "MAP_MEMORY_PATH", path):
                tactic._load_map_memory()
        self.assertEqual(tactic._known_obstacles, frozenset(new_walls))
        self.assertEqual(tactic._get_dead_ends(tactic._known_obstacles),
                         tactic._dead_end_cells(frozenset(new_walls)))
        self.assertEqual(tactic._dead_obstacles, tactic._known_obstacles)


class PlanProfileTests(unittest.TestCase):
    PROFILE_FIELDS = (
        "plan_phase_ms", "plan_pathfind_calls", "plan_pathfind_expansions",
        "plan_pathfind_ms", "plan_dead_end_ms", "plan_dead_end_runs",
    )

    def test_respawn_tick_replaces_stale_profile(self) -> None:
        previous = {
            name: getattr(tactic.turn_context, name)
            for name in self.PROFILE_FIELDS
        }
        tactic.turn_context.plan_phase_ms = {"stale": 9999.0}
        tactic.turn_context.plan_pathfind_calls = 77
        tactic.turn_context.plan_pathfind_expansions = 888
        tactic.turn_context.plan_pathfind_ms = 999.0
        tactic.turn_context.plan_dead_end_ms = 555.0
        tactic.turn_context.plan_dead_end_runs = 44
        turn = SimpleNamespace(
            tick=123,
            core=None,
            units=(),
            workers=(),
            vanguards=(),
            rangers=(),
            visible_enemies=(),
            obstacle_cells=frozenset(),
        )

        patches = (
            patch.object(tactic, "load_config", return_value=default_config()),
            patch.object(tactic, "_resolve_shadow_predictions", return_value=[]),
            patch.object(tactic, "_update_enemy_motion_tracks"),
            patch.object(tactic, "_load_and_prune_waypoints", return_value={}),
            patch.object(tactic, "_load_and_prune_self_destructs", return_value=set()),
            patch.object(tactic, "_prune_dead_unit_bookkeeping"),
            patch.object(tactic, "_apply_dashboard_map_edits"),
            patch.object(tactic, "_update_obstacle_memory", return_value=frozenset()),
            patch.object(tactic, "_update_resource_memory"),
            patch.object(tactic, "_update_enemy_sightings"),
            patch.object(tactic, "_save_map_memory"),
            patch.object(tactic, "_battle_log_entries", return_value=[]),
            patch.object(tactic.game_stats, "record_prediction_results"),
            patch.object(tactic.game_stats, "sync_units"),
            patch.object(tactic.game_stats, "record_events"),
            patch.object(tactic.game_stats, "sampled"),
            patch.object(tactic.game_stats, "maybe_save"),
        )
        try:
            with contextlib.ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                action, unit_actions = tactic.choose_actions(turn)

            self.assertEqual((action, unit_actions), ("RESPAWN", {}))
            self.assertNotIn("stale", tactic.turn_context.plan_phase_ms)
            self.assertIn("prediction", tactic.turn_context.plan_phase_ms)
            self.assertEqual(tactic.turn_context.plan_pathfind_calls, 0)
            self.assertEqual(tactic.turn_context.plan_pathfind_expansions, 0)
            self.assertEqual(tactic.turn_context.plan_pathfind_ms, 0.0)
            self.assertEqual(tactic.turn_context.plan_dead_end_runs, 0)
            self.assertEqual(tactic.turn_context.plan_dead_end_ms, 0.0)
        finally:
            for name, value in previous.items():
                setattr(tactic.turn_context, name, value)


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
        self._combat_paths = dict(tactic._combat_path_cache)
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        tactic._worker_last_pos.clear()
        tactic._worker_recent.clear()
        tactic._resource_assignments.clear()
        tactic._worker_path_cache.clear()
        tactic._combat_path_cache.clear()

    def tearDown(self) -> None:
        tactic._worker_path_cache.clear()
        tactic._combat_path_cache.clear()
        tactic._combat_path_cache.update(self._combat_paths)
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
        tactic._combat_path_cache["dead-1"] = {"goal": (0, 0), "path": [(0, 0)]}
        tactic._combat_path_cache["alive-1"] = {"goal": (0, 0), "path": [(0, 0)]}
        tactic._worker_last_pos["dead-1"] = (1, 1)
        tactic._worker_recent["dead-1"] = [(1, 1)]
        tactic._resource_assignments["dead-1"] = (1, 1)
        tactic._object_names[("W", "dead-1")] = "W1"
        tactic._object_names[("W", "alive-1")] = "W2"
        tactic._object_names[("E", "enemy-1")] = "E1"

        tactic._prune_dead_unit_bookkeeping({"alive-1"})

        self.assertNotIn("dead-1", tactic._worker_path_cache)
        self.assertIn("alive-1", tactic._worker_path_cache)
        self.assertNotIn("dead-1", tactic._combat_path_cache)
        self.assertIn("alive-1", tactic._combat_path_cache)
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
        self.assertIn("core-queue-backoff", detail)
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

    def test_full_worker_holds_at_distance_two_when_core_occupied(self) -> None:
        # Chute coordination: a cargo worker already at Manhattan distance >= 2
        # must hold position (not creep back onto the ring) while the core cell
        # is occupied, so the on-core worker keeps a free neighbour to vacate.
        worker = self.Worker("w-hold", (30, -20))  # distance 2 from core (28,-20)
        worker.cargo = 2
        occupied = frozenset({(28, -20), (30, -20)})

        action, detail = self._plan(worker, occupied=occupied)

        self.assertEqual(action, "WAIT")
        self.assertIn("core-queue-hold", detail)
        self.assertEqual(worker.position, (30, -20))

    def test_full_worker_enters_core_when_chute_free(self) -> None:
        # With the core cell empty, a cargo worker on the ring steps in to
        # unload — the queue-hold must not block normal deposit flow.
        worker = self.Worker("w-enter", (29, -20))  # adjacent to core (28,-20)
        worker.cargo = 2
        occupied = frozenset({(29, -20)})  # core cell itself is free

        action, detail = self._plan(worker, occupied=occupied)

        self.assertEqual(action, "MOVE")
        self.assertEqual(worker.position, (28, -20))

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


class DeliveryLeaseTests(unittest.TestCase):
    """Delivery lease: serialize cargo traffic through the core-cell chute.

    Without the lease every carrier races for the single chute each tick;
    losers get CELL_UNIT_LIMIT rejections and the ring oscillates forever.
    """

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
        position = (28, -20)

    def setUp(self) -> None:
        self._lease = tactic._delivery_lease_uid
        self._next = tactic._delivery_next_uid
        self._stuck = dict(tactic._worker_stuck_ticks)
        self._stall = dict(tactic._delivery_stall)
        self._qstall = dict(tactic._delivery_holder_queue_stall)
        self._yield = tactic._delivery_lease_yield
        self._vacating = tactic._chute_vacating_this_tick
        self._adjacent = tactic._lease_holder_adjacent
        self._demand = tactic._chute_in_demand
        self._next_pos = tactic._delivery_next_pos
        self._at_chute = tactic._carrier_at_chute
        self._assignments = dict(tactic._resource_assignments)
        self._memory = set(tactic._resource_memory)
        tactic._delivery_lease_uid = None
        tactic._delivery_next_uid = None
        tactic._worker_stuck_ticks.clear()
        tactic._delivery_stall.clear()
        tactic._delivery_holder_queue_stall.clear()
        tactic._delivery_lease_yield = False
        tactic._chute_vacating_this_tick = False
        tactic._lease_holder_adjacent = False
        tactic._chute_in_demand = False
        tactic._delivery_next_pos = None
        tactic._carrier_at_chute = False
        tactic._worker_path_cache.clear()

    def tearDown(self) -> None:
        tactic._delivery_lease_uid = self._lease
        tactic._delivery_next_uid = self._next
        tactic._worker_stuck_ticks.clear()
        tactic._worker_stuck_ticks.update(self._stuck)
        tactic._delivery_stall.clear()
        tactic._delivery_stall.update(self._stall)
        tactic._delivery_holder_queue_stall.clear()
        tactic._delivery_holder_queue_stall.update(self._qstall)
        tactic._delivery_lease_yield = self._yield
        tactic._chute_vacating_this_tick = self._vacating
        tactic._lease_holder_adjacent = self._adjacent
        tactic._chute_in_demand = self._demand
        tactic._delivery_next_pos = self._next_pos
        tactic._carrier_at_chute = self._at_chute
        tactic._resource_assignments.clear()
        tactic._resource_assignments.update(self._assignments)
        tactic._resource_memory.clear()
        tactic._resource_memory.update(self._memory)
        tactic._worker_path_cache.clear()

    def _plan(self, worker, *, lease_uid, occupied=frozenset(), next_uid=None):
        return tactic._plan_worker(
            worker,
            self.Core(),
            resource_cells=frozenset(),
            obstacle_cells=frozenset(),
            depleted=set(),
            config=default_config(),
            occupied=occupied,
            lease_uid=lease_uid,
            next_uid=next_uid,
        )

    def test_lease_goes_to_closest_carrier(self) -> None:
        near = SimpleNamespace(id="w-near", position=(30, -20), cargo=1)
        far = SimpleNamespace(id="w-far", position=(40, -20), cargo=2)
        empty = SimpleNamespace(id="w-empty", position=(29, -20), cargo=0)

        self.assertEqual(
            tactic._assign_delivery_lease((far, near, empty), (28, -20)),
            "w-near",
        )
        self.assertIsNone(tactic._assign_delivery_lease((empty,), (28, -20)))

    def test_lease_tie_prefers_current_holder(self) -> None:
        a = SimpleNamespace(id="w-a", position=(30, -20), cargo=1)
        b = SimpleNamespace(id="w-b", position=(28, -22), cargo=1)  # both dist 2
        tactic._delivery_lease_uid = "w-b"

        self.assertEqual(tactic._assign_delivery_lease((a, b), (28, -20)), "w-b")

    def test_lease_is_sticky_against_closer_rival(self) -> None:
        # The holder keeps the lease while still closing in, even when another
        # carrier is currently closer — equidistant flip-flop was the cause of
        # ring oscillation (several carriers racing the same cell each tick).
        holder = SimpleNamespace(id="w-holder", position=(33, -20), cargo=1)
        closer = SimpleNamespace(id="w-closer", position=(30, -20), cargo=1)
        tactic._delivery_lease_uid = "w-holder"

        self.assertEqual(
            tactic._assign_delivery_lease((holder, closer), (28, -20)),
            "w-holder",
        )

    def test_lease_rotates_when_holder_stalls_far(self) -> None:
        # A holder beyond queue range that stops making progress (fleeing /
        # wedged) must yield the lease after the stall limit, or the queue
        # starves behind it.
        holder = SimpleNamespace(id="w-holder", position=(33, -20), cargo=1)
        closer = SimpleNamespace(id="w-closer", position=(30, -20), cargo=1)
        tactic._delivery_lease_uid = "w-holder"
        stall = tactic._DELIVERY_STALL_LIMIT
        tactic._delivery_stall["w-holder"] = (5, stall)

        self.assertEqual(
            tactic._assign_delivery_lease((holder, closer), (28, -20)),
            "w-closer",
        )

    def test_lease_holder_in_queue_range_never_stalls_out(self) -> None:
        # Waiting at distance <= 2 is legitimate queueing, not stalling: the
        # stall counter must never evict a holder already in queue range.
        holder = SimpleNamespace(id="w-holder", position=(30, -20), cargo=1)
        rival = SimpleNamespace(id="w-rival", position=(31, -20), cargo=1)
        tactic._delivery_lease_uid = "w-holder"
        stall = tactic._DELIVERY_STALL_LIMIT
        tactic._delivery_stall["w-holder"] = (2, stall * 10)

        self.assertEqual(
            tactic._assign_delivery_lease((holder, rival), (28, -20)),
            "w-holder",
        )

    def test_lease_rotates_away_from_stuck_holder(self) -> None:
        # Beyond queue range the lease rotates after _DELIVERY_STUCK_LIMIT
        # frozen ticks (rejected moves count as no progress).
        stuck = SimpleNamespace(id="w-stuck", position=(31, -20), cargo=1)  # dist 3
        other = SimpleNamespace(id="w-other", position=(30, -20), cargo=1)
        tactic._delivery_lease_uid = "w-stuck"
        tactic._worker_stuck_ticks["w-stuck"] = tactic._DELIVERY_STUCK_LIMIT

        self.assertEqual(
            tactic._assign_delivery_lease((stuck, other), (28, -20)),
            "w-other",
        )

    def test_lease_grace_for_queue_range_holder(self) -> None:
        # In queue range the same freeze is usually a legitimate wait for the
        # enter/deposit/vacate cycle, so the holder gets double grace before
        # the lease churns away.
        stuck = SimpleNamespace(id="w-stuck", position=(29, -20), cargo=1)  # dist 1
        other = SimpleNamespace(id="w-other", position=(31, -20), cargo=1)
        tactic._delivery_lease_uid = "w-stuck"
        tactic._worker_stuck_ticks["w-stuck"] = tactic._DELIVERY_STUCK_LIMIT

        self.assertEqual(
            tactic._assign_delivery_lease((stuck, other), (28, -20)),
            "w-stuck",
        )

    def test_queued_carrier_holds_at_distance_two(self) -> None:
        # Chute is FREE but reserved for the lease holder — the queued carrier
        # must hold at distance 2 instead of racing in.
        worker = self.Worker("w-queue", (30, -20))
        worker.cargo = 2

        action, detail = self._plan(
            worker, lease_uid="w-other", occupied=frozenset({(30, -20)}),
        )

        self.assertEqual(action, "WAIT")
        self.assertIn("core-queue-hold", detail)
        self.assertEqual(worker.position, (30, -20))

    def test_queued_carrier_backs_off_the_ring(self) -> None:
        worker = self.Worker("w-ring", (29, -20))
        worker.cargo = 2

        action, detail = self._plan(
            worker, lease_uid="w-other", occupied=frozenset({(29, -20)}),
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("core-queue-backoff", detail)
        self.assertGreater(
            tactic._manhattan(worker.position, (28, -20)),
            tactic._manhattan((29, -20), (28, -20)),
        )

    def test_queued_carrier_far_still_marches_home(self) -> None:
        # Beyond the queue radius the lease must not stop the march home.
        worker = self.Worker("w-march", (32, -20))
        worker.cargo = 2

        action, _detail = self._plan(
            worker, lease_uid="w-other", occupied=frozenset({(32, -20)}),
        )

        self.assertEqual(action, "MOVE")
        self.assertLess(
            tactic._manhattan(worker.position, (28, -20)),
            tactic._manhattan((32, -20), (28, -20)),
        )

    def test_lease_holder_enters_free_chute(self) -> None:
        worker = self.Worker("w-lease", (29, -20))
        worker.cargo = 2

        action, _detail = self._plan(
            worker, lease_uid="w-lease", occupied=frozenset({(29, -20)}),
        )

        self.assertEqual(action, "MOVE")
        self.assertEqual(worker.position, (28, -20))

    def test_boxed_in_holder_makes_queue_yield(self) -> None:
        # A holder frozen in queue range for _DELIVERY_HOLDER_YIELD_AFTER
        # ticks is boxed in by the queue itself: queued carriers must
        # sidestep instead of holding their slots.
        holder = SimpleNamespace(id="w-holder", position=(30, -20), cargo=1)
        queued = self.Worker("w-queued", (28, -22))
        queued.cargo = 1
        for _ in range(tactic._DELIVERY_HOLDER_YIELD_AFTER + 1):
            tactic._assign_delivery_lease((holder, queued), (28, -20))
        self.assertTrue(tactic._delivery_lease_yield)

        action, detail = self._plan(
            queued, lease_uid="w-holder", occupied=frozenset({(28, -22)}),
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("core-queue-yield", detail)
        # Sidestep never steps closer to the chute when another cell exists.
        self.assertGreaterEqual(
            tactic._manhattan(queued.position, (28, -20)), 2,
        )

    def test_moving_holder_keeps_queue_holding(self) -> None:
        # No freeze, no yield: queued carriers keep holding their slots.
        holder = SimpleNamespace(id="w-holder", position=(30, -20), cargo=1)
        queued = self.Worker("w-queued", (28, -22))
        queued.cargo = 1
        tactic._assign_delivery_lease((holder, queued), (28, -20))
        self.assertFalse(tactic._delivery_lease_yield)

        action, detail = self._plan(
            queued, lease_uid="w-holder", occupied=frozenset({(28, -22)}),
        )

        self.assertEqual(action, "WAIT")
        self.assertIn("core-queue-hold", detail)
        self.assertEqual(queued.position, (28, -22))

    def test_next_is_second_closest_carrier_and_sticky(self) -> None:
        # The next-in-line slot goes to the closest carrier after the lease
        # holder, and ties keep the current next (no flip-flop on the ring).
        a = SimpleNamespace(id="w-a", position=(30, -20), cargo=1)   # dist 2
        b = SimpleNamespace(id="w-b", position=(31, -20), cargo=1)   # dist 3
        c = SimpleNamespace(id="w-c", position=(28, -22), cargo=1)   # dist 2

        tactic._assign_delivery_lease((a, b, c), (28, -20))
        self.assertEqual(tactic._delivery_lease_uid, "w-a")
        self.assertEqual(tactic._delivery_next_uid, "w-c")

        # Equidistant rivals: keep the incumbent next.
        b2 = SimpleNamespace(id="w-b", position=(28, -23), cargo=1)  # dist 3
        tactic._delivery_next_uid = "w-b"
        tactic._assign_delivery_lease((a, b2, c), (28, -20))
        self.assertEqual(tactic._delivery_next_uid, "w-c")  # c is closer
        c2 = SimpleNamespace(id="w-c", position=(31, -21), cargo=1)  # dist 4
        tactic._assign_delivery_lease((a, b2, c2), (28, -20))
        self.assertEqual(tactic._delivery_next_uid, "w-b")  # b is closer
        b3 = SimpleNamespace(id="w-b", position=(30, -22), cargo=1)  # dist 4
        c3 = SimpleNamespace(id="w-c", position=(29, -23), cargo=1)  # dist 4
        tactic._delivery_next_uid = "w-c"
        tactic._assign_delivery_lease((a, b3, c3), (28, -20))
        self.assertEqual(tactic._delivery_next_uid, "w-c")  # sticky tie

    def test_next_chains_into_vacating_chute(self) -> None:
        # The occupant leaves this tick: the next carrier adjacent to the
        # chute steps onto the core cell in the SAME tick — the server chains
        # the entry behind the departure (one out, one in).
        worker = self.Worker("w-next", (29, -20))
        worker.cargo = 2
        tactic._chute_vacating_this_tick = True

        action, _detail = self._plan(
            worker, lease_uid="w-insider", next_uid="w-next",
            occupied=frozenset({(28, -20), (29, -20)}),
        )

        self.assertEqual(action, "MOVE")
        self.assertEqual(worker.position, (28, -20))

    def test_next_holds_adjacent_while_insider_deposits(self) -> None:
        # Insider still carrying on the chute (deposits and stays this tick):
        # the next carrier must hold its adjacent slot, not squeeze in.
        worker = self.Worker("w-next", (29, -20))
        worker.cargo = 2

        action, detail = self._plan(
            worker, lease_uid="w-insider", next_uid="w-next",
            occupied=frozenset({(28, -20), (29, -20)}),
        )

        self.assertEqual(action, "WAIT")
        self.assertIn("core-next-hold", detail)
        self.assertEqual(worker.position, (29, -20))

    def test_next_yields_entry_to_adjacent_holder(self) -> None:
        # Chute free but the lease holder is already adjacent and entering
        # this tick: the next carrier must not race for the same cell.
        worker = self.Worker("w-next", (28, -21))
        worker.cargo = 2
        tactic._lease_holder_adjacent = True

        action, detail = self._plan(
            worker, lease_uid="w-insider", next_uid="w-next",
            occupied=frozenset({(28, -21)}),
        )

        self.assertEqual(action, "WAIT")
        self.assertIn("core-next-hold", detail)
        self.assertEqual(worker.position, (28, -21))

    def test_next_enters_free_chute_when_holder_far(self) -> None:
        # Chute free and the lease holder out of reach (fleeing/marching):
        # the adjacent next carrier takes the slot instead of waiting.
        worker = self.Worker("w-next", (29, -20))
        worker.cargo = 2

        action, _detail = self._plan(
            worker, lease_uid="w-insider", next_uid="w-next",
            occupied=frozenset({(29, -20)}),
        )

        self.assertEqual(action, "MOVE")
        self.assertEqual(worker.position, (28, -20))

    def test_empty_worker_routes_around_core_under_delivery_pressure(self) -> None:
        # While carriers are delivering, an empty worker's path must not cut
        # through the chute: it just vacated and would otherwise step right
        # back on, squat-looping and blocking the one-out-one-in hand-off.
        worker = self.Worker("w-empty", (29, -20))  # adjacent, east of core
        tactic._chute_in_demand = True
        tactic._resource_assignments["w-empty"] = (27, -20)  # west of core
        tactic._resource_memory.add((27, -20))

        action, _detail = self._plan(
            worker, lease_uid="w-other", occupied=frozenset({(29, -20)}),
        )

        self.assertEqual(action, "MOVE")
        # Must NOT step onto the core cell even though it is the straight way.
        self.assertNotEqual(worker.position, (28, -20))

    def test_vacate_swaps_with_next_carrier_on_packed_ring(self) -> None:
        # Every free ring cell is taken: the empty worker on the chute must
        # swap slots with the incoming next carrier (server chains the entry
        # behind the departure) instead of WAIT-ing and deadlocking the line.
        worker = self.Worker("w-core", (28, -20))
        tactic._delivery_next_pos = (29, -20)
        occupied = frozenset({
            (28, -20), (29, -20),        # core + next carrier
            (28, -19), (28, -21), (27, -20),  # other ring cells packed
        })

        action, detail = self._plan(worker, lease_uid="w-other", occupied=occupied)

        self.assertEqual(action, "MOVE")
        self.assertIn("vacate-core", detail)
        self.assertEqual(worker.position, (29, -20))

    def test_vacate_swaps_with_ring_occupant_without_next(self) -> None:
        # Single-carrier fleet (no next-in-line picked): the packed-ring swap
        # must still fire while the delivery line is in demand, otherwise the
        # empty worker squats on the chute and the lone carrier waits forever.
        worker = self.Worker("w-core", (28, -20))
        tactic._delivery_next_pos = None
        tactic._chute_in_demand = True
        occupied = frozenset({
            (28, -20), (28, -21), (29, -20), (28, -19), (27, -20),
        })

        action, detail = self._plan(worker, lease_uid="w-other", occupied=occupied)

        self.assertEqual(action, "MOVE")
        self.assertIn("vacate-core", detail)
        self.assertEqual(worker.position, (28, -21))

    def test_tail_fallback_releases_core_cell(self) -> None:
        # Every exit blocked, no delivery line active: the last-resort block
        # before WAIT:no_action must still swap the empty worker off the
        # chute — a parked worker there stalls every future delivery.
        worker = self.Worker("w-core", (28, -20))
        tactic._delivery_next_pos = None
        tactic._chute_in_demand = False
        occupied = frozenset({
            (28, -20), (28, -21), (29, -20), (28, -19), (27, -20),
        })

        action, detail = self._plan(worker, lease_uid=None, occupied=occupied)

        self.assertEqual(action, "MOVE")
        self.assertIn("vacate-core", detail)
        self.assertNotEqual(worker.position, (28, -20))


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
        # Over-target stops production; existing units are kept as-is.
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

    def test_dynamic_price_above_population_20(self) -> None:
        # Game rules v0.14: the 21st Unit is the first +30% (Worker 5 -> 7).
        turn = self._turn(workers=9, population=21)  # worker below target, pop 21
        core = self.Core()
        config = dict(self.config)
        config["resource_reserve"] = 0

        self.assertEqual(tactic._spawn_cost(UnitType.WORKER, 21), 7)
        self.assertIsNone(tactic._plan_demand_spawn(turn, core, resources=6, config=config))
        self.assertEqual(
            tactic._plan_demand_spawn(turn, core, resources=7, config=config),
            "SPAWN_WORKER",
        )

    def test_spawn_success_log_shows_dynamic_cost(self) -> None:
        event = SimpleNamespace(
            event_type="CORE_SPAWN_SUCCEEDED",
            reason_code=None,
            actor_id=None,
            target_id=None,
            values={"unit_type": "VANGUARD", "cost": 13},
            resource_amount=None,
        )
        cat, msg = tactic._classify_battle_event(self._turn(workers=8), event)
        self.assertEqual(cat, "economy")
        self.assertIn("生产", msg)
        self.assertIn("13 资源", msg)


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
        for key in ("target_workers", "home_team"):
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
                 patch.object(tactic, "mutate_config", wraps=tactic_config.mutate_config) as mutate_mock:
                result = tactic._ensure_home_team_membership(stale, ["V9"])

        self.assertEqual(result["target_vanguards"], 4)  # preserved from disk
        self.assertIn("V9", result["home_team"])
        self.assertEqual(mutate_mock.call_count, 1)

    def test_lead_fire_defaults_on_for_existing_config_and_renders_switch(self) -> None:
        legacy = tactic_config.default_config()
        legacy.pop("ranger_lead_fire_enabled")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tactic_config.json"
            path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
            loaded = tactic_config.load_config(path)

        self.assertTrue(loaded["ranger_lead_fire_enabled"])
        html = dashboard.render_teams_panel()
        self.assertIn('name="ranger_lead_fire_enabled"', html)
        self.assertIn("移动预判实射", html)


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

    def test_load_backfills_legacy_combat_death_totals(self) -> None:
        stats = game_stats.new_stats()
        stats["per_combat"] = {
            "vg-old": {
                "type": "VANGUARD", "shots": 0, "hits": 0,
                "born_tick": 1, "died_tick": 5,
            },
            "rg-old": {
                "type": "RANGER", "shots": 0, "hits": 0,
                "born_tick": 1, "died_tick": 6,
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "game_stats.json"
            game_stats.save(stats, path)
            loaded = game_stats.load(path)

        self.assertEqual(loaded["deaths"]["VANGUARD"], 1)
        self.assertEqual(loaded["deaths"]["RANGER"], 1)

    def test_combat_deaths_are_counted_by_type(self) -> None:
        stats = game_stats.new_stats()
        vanguard = self._unit("vg-dead-1111", UnitType.VANGUARD)
        ranger = self._unit("rg-dead-2222", UnitType.RANGER)
        game_stats.sync_units(stats, self._turn(units=(vanguard, ranger)), tick=10)
        game_stats.sync_units(stats, self._turn(units=()), tick=11)

        self.assertEqual(stats["deaths"]["VANGUARD"], 1)
        self.assertEqual(stats["deaths"]["RANGER"], 1)
        self.assertEqual(stats["per_combat"]["vg-dead-"]["died_tick"], 11)
        self.assertEqual(stats["per_combat"]["rg-dead-"]["died_tick"], 11)

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

    def test_shadow_prediction_candidates_and_results_are_aggregated(self) -> None:
        stats = game_stats.new_stats()
        candidate = {
            "target_type": "WORKER",
            "move_streak": 3,
            "motion_state": "moving_stable",
            "prediction_legal": True,
            "eligible": True,
        }
        result = {
            **candidate,
            "predicted_match": True,
            "shot_result": "SHOT_MISSED",
        }

        game_stats.record_prediction_candidates(stats, [candidate])
        game_stats.record_prediction_results(stats, [result])

        prediction = stats["shot_prediction"]
        self.assertEqual(prediction["candidates"], 1)
        self.assertEqual(prediction["legal_candidates"], 1)
        self.assertEqual(prediction["eligible_candidates"], 1)
        self.assertEqual(prediction["predicted_correct"], 1)
        self.assertEqual(prediction["baseline_misses"], 1)
        self.assertEqual(prediction["improvements"], 1)
        self.assertEqual(prediction["harms"], 0)
        self.assertEqual(prediction["by_streak"]["3_plus"]["improvements"], 1)
        self.assertEqual(
            prediction["by_target_type"]["WORKER"]["predicted_correct"], 1,
        )
        self.assertEqual(
            prediction["by_motion_state"]["moving_stable"]["improvements"], 1,
        )

    def test_shadow_prediction_unknown_and_harm_are_counted(self) -> None:
        stats = game_stats.new_stats()
        common = {
            "target_type": "VANGUARD",
            "move_streak": 3,
            "prediction_legal": True,
            "eligible": True,
        }
        game_stats.record_prediction_results(stats, [
            {**common, "predicted_match": False, "shot_result": "SHOT_HIT"},
            {**common, "predicted_match": None, "shot_result": "UNRESOLVED"},
        ])

        prediction = stats["shot_prediction"]
        self.assertEqual(prediction["predicted_wrong"], 1)
        self.assertEqual(prediction["unknown"], 1)
        self.assertEqual(prediction["harms"], 1)
        self.assertEqual(prediction["by_streak"]["3_plus"]["resolved"], 2)

    def test_live_lead_fire_results_are_counted_separately(self) -> None:
        stats = game_stats.new_stats()
        common = {
            "target_type": "VANGUARD",
            "move_streak": 3,
            "motion_state": "moving_stable",
            "prediction_legal": True,
            "eligible": True,
            "lead_fire_used": True,
        }
        candidates = [common, common]
        results = [
            {
                **common,
                "predicted_match": True,
                "current_match": False,
                "shot_result": "SHOT_HIT",
            },
            {
                **common,
                "predicted_match": False,
                "current_match": True,
                "shot_result": "SHOT_MISSED",
            },
        ]

        game_stats.record_prediction_candidates(stats, candidates)
        game_stats.record_prediction_results(stats, results)

        prediction = stats["shot_prediction"]
        self.assertEqual(prediction["lead_fire_attempts"], 2)
        self.assertEqual(prediction["lead_fire_hits"], 1)
        self.assertEqual(prediction["lead_fire_misses"], 1)
        self.assertEqual(prediction["lead_fire_improvements"], 1)
        self.assertEqual(prediction["lead_fire_harms"], 1)
        self.assertEqual(prediction["baseline_hits"], 0)
        self.assertEqual(prediction["baseline_misses"], 0)
        self.assertEqual(prediction["improvements"], 0)
        self.assertEqual(prediction["harms"], 0)
        stable = prediction["by_motion_state"]["moving_stable"]
        self.assertEqual(stable["lead_fire_attempts"], 2)
        self.assertEqual(stable["lead_fire_hits"], 1)

    def test_save_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "stats.json"
            stats = game_stats.new_stats()
            stats["current_tick"] = 42
            stats["economy"]["harvested_total"] = 123
            stats["shot_prediction"]["eligible_candidates"] = 7
            stats["shot_prediction"]["by_streak"]["2"]["predicted_correct"] = 3
            stats["shot_prediction"]["by_motion_state"]["stationary"][
                "baseline_hits"
            ] = 9
            game_stats.save(stats, path)

            loaded = game_stats.load(path)

        self.assertEqual(loaded["current_tick"], 42)
        self.assertEqual(loaded["economy"]["harvested_total"], 123)
        self.assertEqual(loaded["shot_prediction"]["eligible_candidates"], 7)
        self.assertEqual(
            loaded["shot_prediction"]["by_streak"]["2"]["predicted_correct"], 3,
        )
        self.assertEqual(
            loaded["shot_prediction"]["by_motion_state"]["stationary"][
                "baseline_hits"
            ],
            9,
        )

    def test_load_puts_pre_classification_prediction_totals_in_legacy(self) -> None:
        stats = game_stats.new_stats()
        stats["shot_prediction"]["candidates"] = 12
        stats["shot_prediction"]["resolved"] = 10
        stats["shot_prediction"]["baseline_hits"] = 8
        stats["shot_prediction"].pop("by_motion_state")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "stats.json"
            game_stats.save(stats, path)
            loaded = game_stats.load(path)

        legacy = loaded["shot_prediction"]["by_motion_state"]["legacy"]
        self.assertEqual(legacy["candidates"], 12)
        self.assertEqual(legacy["resolved"], 10)
        self.assertEqual(legacy["baseline_hits"], 8)

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
            self.id = "ranger-shadow-1"
            self.position = position
            self.action = None
            self.arg = None
            self.expected_cell = None

        def shoot(self, target, *, expected_cell=None) -> None:
            self.action = "SHOOT"
            self.arg = target
            self.expected_cell = expected_cell

    class Enemy:
        def __init__(
            self,
            position: tuple[int, int],
            index: int = 1,
            unit_type: UnitType = UnitType.VANGUARD,
        ) -> None:
            self.id = f"enemy-shadow-{index}"
            self.position = position
            self.unit_type = unit_type

    def setUp(self) -> None:
        self._tracks = {
            key: list(value) for key, value in tactic._enemy_motion_tracks.items()
        }
        self._pending = [dict(item) for item in tactic._pending_shot_predictions]
        self._tick = tactic.turn_context.tick
        self._predictions = list(tactic.turn_context.shot_predictions)
        self._results = list(tactic.turn_context.shot_prediction_results)
        tactic._enemy_motion_tracks.clear()
        tactic._pending_shot_predictions.clear()
        tactic.turn_context.tick = 0
        tactic.turn_context.shot_predictions = []
        tactic.turn_context.shot_prediction_results = []

    def tearDown(self) -> None:
        tactic._enemy_motion_tracks.clear()
        tactic._enemy_motion_tracks.update(self._tracks)
        tactic._pending_shot_predictions.clear()
        tactic._pending_shot_predictions.extend(self._pending)
        tactic.turn_context.tick = self._tick
        tactic.turn_context.shot_predictions = self._predictions
        tactic.turn_context.shot_prediction_results = self._results

    def _shoot(
        self,
        enemy_positions,
        obstacles=(),
        attack_range: int = 3,
        ranger_pos: tuple[int, int] = (0, 0),
    ):
        ranger = self.Ranger(ranger_pos)
        enemies = tuple(
            self.Enemy(position, index)
            for index, position in enumerate(enemy_positions, 1)
        )
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

    def test_shadow_prediction_records_stable_lead_without_changing_shot(self) -> None:
        enemy = self.Enemy((0, 0))
        for tick, position in (
            (1, (0, 0)),
            (2, (0, 1)),
            (3, (0, 2)),
            (4, (0, 3)),
        ):
            enemy.position = position
            tactic._update_enemy_motion_tracks((enemy,), tick)
        tactic.turn_context.tick = 4
        ranger = self.Ranger((0, 1))

        result = tactic._ranger_best_shot(
            ranger, ranger.position, (enemy,), frozenset(), 3,
        )

        prediction = tactic.turn_context.shot_predictions[0]
        self.assertEqual(result[0], "SHOOT")
        self.assertIs(ranger.arg, enemy)  # real shot still targets the current view
        self.assertEqual(prediction["predicted_cell"], [0, 4])
        self.assertEqual(prediction["move_streak"], 3)
        self.assertEqual(prediction["motion_state"], "moving_stable")
        self.assertTrue(prediction["eligible"])
        self.assertFalse(prediction["lead_fire_used"])
        self.assertEqual(prediction["fired_cell"], [0, 3])
        self.assertIsNone(ranger.expected_cell)

    def test_stable_legal_prediction_uses_expected_cell_when_enabled(self) -> None:
        enemy = self.Enemy((0, 0), unit_type=UnitType.WORKER)
        for tick, position in (
            (1, (0, 0)),
            (2, (0, 1)),
            (3, (0, 2)),
            (4, (0, 3)),
        ):
            enemy.position = position
            tactic._update_enemy_motion_tracks((enemy,), tick)
        tactic.turn_context.tick = 4
        ranger = self.Ranger((0, 1))

        result = tactic._ranger_best_shot(
            ranger,
            ranger.position,
            (enemy,),
            frozenset(),
            3,
            lead_fire_enabled=True,
        )

        prediction = tactic.turn_context.shot_predictions[0]
        self.assertEqual(result[0], "SHOOT")
        self.assertIn("lead", result[1])
        self.assertIs(ranger.arg, enemy)
        self.assertEqual(ranger.expected_cell, (0, 4))
        self.assertTrue(prediction["lead_fire_used"])
        self.assertEqual(prediction["fire_mode"], "lead")
        self.assertEqual(prediction["fired_cell"], [0, 4])
        self.assertIsNone(prediction["lead_fire_rejection"])

    def test_stable_combat_target_remains_shadow_only_when_enabled(self) -> None:
        for unit_type in (UnitType.VANGUARD, UnitType.RANGER):
            with self.subTest(unit_type=unit_type):
                tactic._enemy_motion_tracks.clear()
                tactic.turn_context.shot_predictions = []
                enemy = self.Enemy((0, 0), unit_type=unit_type)
                for tick, position in (
                    (1, (0, 0)),
                    (2, (0, 1)),
                    (3, (0, 2)),
                    (4, (0, 3)),
                ):
                    enemy.position = position
                    tactic._update_enemy_motion_tracks((enemy,), tick)
                tactic.turn_context.tick = 4
                ranger = self.Ranger((0, 1))

                tactic._ranger_best_shot(
                    ranger,
                    ranger.position,
                    (enemy,),
                    frozenset(),
                    3,
                    lead_fire_enabled=True,
                )

                prediction = tactic.turn_context.shot_predictions[0]
                self.assertTrue(prediction["eligible"])
                self.assertFalse(prediction["lead_fire_used"])
                self.assertEqual(prediction["lead_fire_rejection"], "target_type")
                self.assertEqual(prediction["fired_cell"], [0, 3])
                self.assertIsNone(ranger.expected_cell)

    def test_only_one_ranger_leads_the_same_worker_per_tick(self) -> None:
        enemy = self.Enemy((0, 0), unit_type=UnitType.WORKER)
        for tick, position in (
            (1, (0, 0)),
            (2, (0, 1)),
            (3, (0, 2)),
            (4, (0, 3)),
        ):
            enemy.position = position
            tactic._update_enemy_motion_tracks((enemy,), tick)
        tactic.turn_context.tick = 4
        first_ranger = self.Ranger((0, 1))
        second_ranger = self.Ranger((0, 1))

        tactic._ranger_best_shot(
            first_ranger,
            first_ranger.position,
            (enemy,),
            frozenset(),
            3,
            lead_fire_enabled=True,
        )
        tactic._ranger_best_shot(
            second_ranger,
            second_ranger.position,
            (enemy,),
            frozenset(),
            3,
            lead_fire_enabled=True,
        )

        first_prediction, second_prediction = tactic.turn_context.shot_predictions
        self.assertEqual(first_ranger.expected_cell, (0, 4))
        self.assertTrue(first_prediction["lead_fire_used"])
        self.assertIsNone(second_ranger.expected_cell)
        self.assertFalse(second_prediction["lead_fire_used"])
        self.assertEqual(second_prediction["lead_fire_rejection"], "target_claimed")
        self.assertEqual(second_prediction["fired_cell"], [0, 3])

    def test_shadow_prediction_classifies_confirmed_stationary_target(self) -> None:
        enemy = self.Enemy((0, 2))
        for tick in (1, 2, 3):
            tactic._update_enemy_motion_tracks((enemy,), tick)
        tactic.turn_context.tick = 3

        prediction = tactic._shadow_shot_prediction(
            self.Ranger((0, 0)), (0, 0), enemy, frozenset(), 3,
        )

        self.assertEqual(prediction["stationary_streak"], 2)
        self.assertEqual(prediction["motion_state"], "stationary")
        self.assertEqual(prediction["reason"], "stationary")
        self.assertIsNone(prediction["predicted_cell"])
        self.assertFalse(prediction["eligible"])

    def test_shadow_prediction_requires_three_steps_for_stable_motion(self) -> None:
        enemy = self.Enemy((0, 0))
        for tick, position in (
            (1, (0, 0)),
            (2, (0, 1)),
            (3, (0, 2)),
        ):
            enemy.position = position
            tactic._update_enemy_motion_tracks((enemy,), tick)
        tactic.turn_context.tick = 3

        prediction = tactic._shadow_shot_prediction(
            self.Ranger((0, 0)), (0, 0), enemy, frozenset(), 3,
        )

        self.assertEqual(prediction["move_streak"], 2)
        self.assertEqual(prediction["motion_state"], "moving_unstable")
        self.assertTrue(prediction["prediction_legal"])
        self.assertFalse(prediction["eligible"])
        self.assertEqual(prediction["reason"], "unstable_velocity")

        ranger = self.Ranger((0, 0))
        tactic._ranger_best_shot(
            ranger,
            ranger.position,
            (enemy,),
            frozenset(),
            3,
            lead_fire_enabled=True,
        )
        queued = tactic.turn_context.shot_predictions[-1]
        self.assertFalse(queued["lead_fire_used"])
        self.assertIsNone(ranger.expected_cell)

    def test_shadow_prediction_marks_direction_change_unstable(self) -> None:
        enemy = self.Enemy((0, 0))
        for tick, position in ((1, (0, 0)), (2, (0, 1)), (3, (1, 1))):
            enemy.position = position
            tactic._update_enemy_motion_tracks((enemy,), tick)
        tactic.turn_context.tick = 3

        prediction = tactic._shadow_shot_prediction(
            self.Ranger((0, 1)), (0, 1), enemy, frozenset(), 3,
        )

        self.assertEqual(prediction["predicted_cell"], [2, 1])
        self.assertEqual(prediction["move_streak"], 1)
        self.assertEqual(prediction["motion_state"], "moving_unstable")
        self.assertTrue(prediction["prediction_legal"])
        self.assertFalse(prediction["eligible"])
        self.assertEqual(prediction["reason"], "unstable_velocity")

    def test_enemy_track_resets_after_tick_gap(self) -> None:
        enemy = self.Enemy((0, 0))
        tactic._update_enemy_motion_tracks((enemy,), 1)
        enemy.position = (0, 1)
        tactic._update_enemy_motion_tracks((enemy,), 3)

        prediction = tactic._shadow_shot_prediction(
            self.Ranger((0, 0)), (0, 0), enemy, frozenset(), 3,
        )

        self.assertEqual(len(tactic._enemy_motion_tracks[str(enemy.id)]), 1)
        self.assertIsNone(prediction["predicted_cell"])
        self.assertEqual(prediction["reason"], "insufficient_history")

    def test_pending_prediction_resolves_against_next_tick(self) -> None:
        enemy = self.Enemy((0, 3))
        tactic._pending_shot_predictions.append({
            "tick": 3,
            "ranger_id": "ranger-s",
            "target_id": "enemy-s",
            "target_type": "VANGUARD",
            "current_cell": [0, 2],
            "predicted_cell": [0, 3],
            "move_streak": 2,
            "prediction_legal": True,
            "eligible": True,
            "_ranger_key": "ranger-shadow-1",
            "_target_key": "enemy-shadow-1",
        })
        turn = SimpleNamespace(
            visible_enemies=(enemy,),
            events=(SimpleNamespace(
                actor_id="ranger-shadow-1", event_type="SHOT_MISSED",
            ),),
        )

        results = tactic._resolve_shadow_predictions(turn, 4)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["actual_cell"], [0, 3])
        self.assertTrue(results[0]["predicted_match"])
        self.assertFalse(results[0]["current_match"])
        self.assertEqual(results[0]["shot_result"], "SHOT_MISSED")
        self.assertEqual(tactic._pending_shot_predictions, [])

    def test_pending_prediction_is_unknown_after_tick_gap(self) -> None:
        enemy = self.Enemy((0, 3))
        tactic._pending_shot_predictions.append({
            "tick": 3,
            "current_cell": [0, 2],
            "predicted_cell": [0, 3],
            "move_streak": 2,
            "eligible": True,
            "_ranger_key": "ranger-shadow-1",
            "_target_key": "enemy-shadow-1",
        })
        turn = SimpleNamespace(
            visible_enemies=(enemy,),
            events=(SimpleNamespace(
                actor_id="ranger-shadow-1", event_type="SHOT_HIT",
            ),),
        )

        results = tactic._resolve_shadow_predictions(turn, 6)

        self.assertEqual(results[0]["tick_gap"], 3)
        self.assertIsNone(results[0]["actual_cell"])
        self.assertIsNone(results[0]["predicted_match"])
        self.assertEqual(results[0]["shot_result"], "UNRESOLVED")

    def test_shadow_candidates_commit_only_after_accepted_plan(self) -> None:
        prediction = {
            "tick": 3,
            "move_streak": 2,
            "eligible": True,
            "_ranger_key": "ranger-shadow-1",
            "_target_key": "enemy-shadow-1",
        }
        tactic.turn_context.shot_predictions = [prediction]

        with patch.object(game_stats, "record_prediction_candidates") as record:
            tactic._commit_shadow_predictions(False)
            self.assertEqual(tactic._pending_shot_predictions, [])
            record.assert_not_called()

            tactic._commit_shadow_predictions(True)

        self.assertEqual(len(tactic._pending_shot_predictions), 1)
        record.assert_called_once()


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


class HomeHealReturnTests(unittest.TestCase):
    """守家队主动回核心回血: only the home squad marches back to heal."""

    @staticmethod
    def _unit(*, pos=(10, 0), hp=1, unit_type=UnitType.RANGER):
        return SimpleNamespace(position=pos, hp=hp, unit_type=unit_type)

    def _should_return(self, unit, *, team="home", config=None, core_moving=False):
        cfg = config or default_config()
        return tactic._unit_should_return_to_heal(
            unit,
            cfg,
            core_pos=(0, 0),
            core_moving=core_moving,
            team=team,
        )

    def test_home_ranger_at_one_hp_returns(self) -> None:
        self.assertTrue(self._should_return(self._unit(hp=1, unit_type=UnitType.RANGER)))

    def test_home_vanguard_below_threshold_returns(self) -> None:
        # Threshold 2 ("HP 低于阈值就回"): a 3/4 vanguard stays out; only a
        # 1/4 vanguard comes home. Raising the threshold brings back 2/4 too.
        self.assertFalse(self._should_return(self._unit(hp=3, unit_type=UnitType.VANGUARD)))
        self.assertFalse(self._should_return(self._unit(hp=2, unit_type=UnitType.VANGUARD)))
        self.assertTrue(self._should_return(self._unit(hp=1, unit_type=UnitType.VANGUARD)))

    def test_full_hp_does_not_return(self) -> None:
        self.assertFalse(self._should_return(self._unit(hp=2, unit_type=UnitType.RANGER)))

    def test_attack_team_never_returns(self) -> None:
        self.assertFalse(
            self._should_return(self._unit(hp=1), team="attack")
        )
        self.assertFalse(
            self._should_return(self._unit(hp=1), team="guerrilla")
        )
        self.assertFalse(
            self._should_return(self._unit(hp=1), team="unassigned")
        )

    def test_threshold_zero_disables_retreat(self) -> None:
        config = default_config()
        config["combat_heal_hp_threshold"] = 0
        self.assertFalse(
            self._should_return(self._unit(hp=1), config=config)
        )

    def test_threshold_raises_returns_full_vanguard(self) -> None:
        config = default_config()
        config["combat_heal_hp_threshold"] = 4
        self.assertTrue(
            self._should_return(
                self._unit(hp=3, unit_type=UnitType.VANGUARD), config=config,
            )
        )

    def test_on_core_cell_left_to_heal_branch(self) -> None:
        self.assertFalse(self._should_return(self._unit(pos=(0, 0), hp=1)))

    def test_moving_core_does_not_chase(self) -> None:
        self.assertFalse(self._should_return(self._unit(hp=1), core_moving=True))

    def test_heal_disabled_no_retreat(self) -> None:
        config = default_config()
        config["heal_enabled"] = False
        self.assertFalse(self._should_return(self._unit(hp=1), config=config))

    def test_worker_never_returns(self) -> None:
        self.assertFalse(
            self._should_return(self._unit(hp=1, unit_type=UnitType.WORKER))
        )

    @staticmethod
    def _make_ranger(uid, pos, hp):
        ranger = SimpleNamespace(
            id=uid, unit_type=UnitType.RANGER, position=pos, hp=hp,
        )
        ranger.move = lambda direction: None
        ranger.wait = lambda: None
        return ranger

    def _run_choose_actions(
        self, combat_units, *, config, limit=None, home_team=None, workers=(),
        obstacle_cells=frozenset(), events=(),
    ):
        """Run choose_actions against the given combat units; return the
        per-unit actions dict. Names are deterministic (R1, R2, … / V1, V2…)."""
        rangers = tuple(u for u in combat_units if u.unit_type == UnitType.RANGER)
        vanguards = tuple(u for u in combat_units if u.unit_type == UnitType.VANGUARD)
        if home_team is not None:
            config["home_team"] = home_team
        prev_names = dict(tactic._object_names)
        prev_counters = dict(tactic._object_name_counters)
        prev_resource_space = tactic.turn_context.resource_space
        prev_healing_prev = set(tactic._healing_units_prev)
        prev_inflight = set(tactic._heal_return_inflight)
        prev_streak = dict(tactic._cell_limit_streak)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            tactic._object_names.clear()
            tactic._object_name_counters.clear()
            core = SimpleNamespace(
                id="core", position=(0, 0), hp=5, shield=10,
                view=SimpleNamespace(state=SimpleNamespace(value="ALIVE")),
                spawn=lambda unit_type: None,
                heal=lambda: None,
                repair_shield=lambda: None,
                move=lambda *args, **kwargs: None,
                wait=lambda: None,
            )
            beacon = SimpleNamespace(
                position=None, status=SimpleNamespace(name="GROUND"),
            )
            turn = SimpleNamespace(
                tick=1,
                units=rangers + vanguards,
                workers=tuple(workers),
                vanguards=vanguards,
                rangers=rangers,
                visible_enemies=(),
                core=core,
                resources=50,
                resource_cells=frozenset(),
                resource_space=0,
                beacon=beacon,
                state=SimpleNamespace(population=8),
                events=tuple(events),
                obstacle_cells=frozenset(obstacle_cells),
            )
            if limit is not None:
                config["combat_heal_return_limit"] = limit
            with patch.object(tactic, "load_config", return_value=config), \
                 patch.object(tactic, "MAP_MEMORY_PATH", temp / "map_memory.json"), \
                 patch.object(tactic, "WAYPOINTS_PATH", temp / "waypoints.json"), \
                 patch.object(tactic, "SELF_DESTRUCT_PATH", temp / "self_destruct.json"), \
                 patch.object(tactic, "BATTLE_LOG_PATH", temp / "battle_log.jsonl"), \
                 patch.object(tactic, "CONFIG_PATH", temp / "tactic_config.json"):
                tactic._map_dirty = False
                try:
                    _, actions = tactic.choose_actions(turn)
                finally:
                    tactic._object_names.clear()
                    tactic._object_names.update(prev_names)
                    tactic._object_name_counters.clear()
                    tactic._object_name_counters.update(prev_counters)
                    tactic.turn_context.resource_space = prev_resource_space
                    tactic._healing_units_prev = prev_healing_prev
                    tactic._heal_return_inflight = prev_inflight
                    tactic._cell_limit_streak = prev_streak
        return actions

    def test_home_ranger_actually_marches_home_in_choose_actions(self) -> None:
        """The user-visible bug: a 1-HP home ranger off the Core now issues a
        MOVE toward the Core instead of keeping up its patrol/roam."""
        config = default_config()
        ranger = self._make_ranger("ranger-1", (10, 0), 1)
        actions = self._run_choose_actions((ranger,), config=config, home_team="R1")
        self.assertIn("ranger-1", actions)
        self.assertIn("home-heal-return", actions["ranger-1"])

    def test_stagger_limit_one_sends_only_most_damaged_home(self) -> None:
        """Two 1-HP home rangers: the per-Tick cap of 1 peels off only one;
        the closer one wins the single slot and the other keeps defending."""
        config = default_config()
        rangers = (
            self._make_ranger("ranger-a", (10, 0), 1),
            self._make_ranger("ranger-b", (12, 0), 1),
        )
        actions = self._run_choose_actions(rangers, config=config, limit=1, home_team="R1, R2")
        returning = [k for k, v in actions.items() if "home-heal-return" in v]
        self.assertEqual(returning, ["ranger-a"])

    def test_stagger_limit_zero_sends_all_home(self) -> None:
        """Limit 0 (不限) keeps the original behavior: every eligible unit
        returns in the same Tick."""
        config = default_config()
        rangers = (
            self._make_ranger("ranger-a", (10, 0), 1),
            self._make_ranger("ranger-b", (12, 0), 1),
        )
        actions = self._run_choose_actions(rangers, config=config, limit=0, home_team="R1, R2")
        returning = [k for k, v in actions.items() if "home-heal-return" in v]
        self.assertEqual(len(returning), 2)

    def test_stagger_prefers_lowest_hp_over_distance(self) -> None:
        """Scarce slots go to the most-damaged unit first, even when a less
        damaged one is much closer to the Core."""
        config = default_config()
        config["combat_heal_hp_threshold"] = 3  # both 2-HP and 1-HP eligible
        ranger = self._make_ranger("ranger-a", (100, 0), 1)   # 1 HP, far away
        vanguard = SimpleNamespace(
            id="vanguard-a", unit_type=UnitType.VANGUARD,
            position=(5, 0), hp=2,  # 2 HP, near the Core
        )
        vanguard.move = lambda direction: None
        vanguard.wait = lambda: None
        actions = self._run_choose_actions(
            (ranger, vanguard), config=config, limit=1, home_team="R1, V1",
        )
        # The 1-HP ranger wins the single slot over the 2-HP vanguard.
        returning = [k for k, v in actions.items() if "home-heal-return" in v]
        self.assertEqual(returning, ["ranger-a"])


class HealReturnGateTests(HomeHealReturnTests):
    """heal_return_gate only counts carriers queued at the chute (≤ 3 cells).

    Regression: the gate used to count EVERY carrying worker on the map, so a
    healthy economy kept it shut 100% of ticks and healing never fired.
    """

    @staticmethod
    def _carrier(uid, pos, cargo=1):
        return SimpleNamespace(id=uid, position=pos, cargo=cargo)

    def test_far_carriers_do_not_block_heal_return(self) -> None:
        """Two carrying workers exist but both mine far away (> 3 cells from
        the Core): the gate stays open and the 1-HP home ranger marches home
        to heal instead of being sent to SHOOT/patrol."""
        config = default_config()
        ranger = self._make_ranger("ranger-1", (10, 0), 1)
        carriers = (self._carrier("w1", (20, 0)), self._carrier("w2", (25, 0)))
        actions = self._run_choose_actions(
            (ranger,), config=config, home_team="R1", workers=carriers,
        )
        self.assertIn("ranger-1", actions)
        detail = actions["ranger-1"]
        self.assertTrue(
            "[heal-return]" in detail or detail == "HEAL",
            f"expected heal-return/HEAL, got {detail!r}",
        )

    def test_queue_at_core_keeps_gate_closed(self) -> None:
        """Two carrying workers queued right at the Core (≤ 3 cells): the gate
        shuts and the low-HP ranger keeps defending — delivery priority."""
        config = default_config()
        ranger = self._make_ranger("ranger-1", (10, 0), 1)
        carriers = (self._carrier("w1", (1, 0)), self._carrier("w2", (2, 0)))
        actions = self._run_choose_actions(
            (ranger,), config=config, home_team="R1", workers=carriers,
        )
        self.assertIn("ranger-1", actions)
        detail = actions["ranger-1"]
        self.assertNotIn("[heal-return]", detail)
        self.assertNotEqual(detail, "HEAL")

    def test_inflight_return_keeps_slot_over_fresh_casualty(self) -> None:
        """A unit already marching home (commanded last Tick) keeps its
        stagger slot; a fresh casualty does not revoke it."""
        config = default_config()
        rangers = (
            self._make_ranger("ranger-a", (10, 0), 1),  # R1, fresh casualty
            self._make_ranger("ranger-b", (4, 0), 1),    # R2, in flight
        )
        tactic._heal_return_inflight = {"R2"}
        try:
            actions = self._run_choose_actions(
                rangers, config=config, limit=1, home_team="R1, R2",
            )
        finally:
            tactic._heal_return_inflight = set()
        returning = [k for k, v in actions.items() if "home-heal-return" in v]
        self.assertEqual(returning, ["ranger-b"])

    def test_mid_heal_unit_holds_core_cell_for_heal(self) -> None:
        """A healer that HEALed last Tick and is still damaged keeps the core
        cell and heals again even while a carrier waits adjacent (no
        yield-chute eviction before the HEAL resolves)."""
        config = default_config()
        ranger = self._make_ranger("ranger-1", (0, 0), 1)
        ranger.heal = lambda: None
        carrier = self._carrier("w1", (1, 0))
        tactic._healing_units_prev = {"R1"}
        try:
            actions = self._run_choose_actions(
                (ranger,), config=config, home_team="R1", workers=(carrier,),
            )
        finally:
            tactic._healing_units_prev = set()
        self.assertEqual(actions.get("ranger-1"), "HEAL")


class CoreChokeDeadlockTests(HomeHealReturnTests):
    """窄口地形核心霸格死锁回归（线上核心两面是墙、仅 2 个出入口）.

    Regression: a fully-healed healer on the core cell could not leave
    because _retreat_from treated every occupied neighbour as fully blocked
    (server cell limit is 2) and home-patrol re-targeted the same packed
    cell every tick — sealing the unloading chute for 95+ ticks.
    """

    # Core at (0,0); two adjacent walls leave exactly two ring exits.
    _WALLS = frozenset({(-1, 0), (0, -1)})

    @staticmethod
    def _carrier(uid, pos, cargo=1):
        return SimpleNamespace(id=uid, position=pos, cargo=cargo)

    def _recording_ranger(self, uid, pos, hp):
        ranger = self._make_ranger(uid, pos, hp)
        moves: list = []
        ranger.move = lambda direction: moves.append(direction)
        return ranger, moves

    def test_healed_healer_escapes_narrow_core_ring(self) -> None:
        """(a) Deadlock repro: full-HP healer on the core cell, exit 1 holds a
        single carrier (count 1 < limit 2), exit 2 is packed (2 units = the
        server cell limit). The healer must leave the core cell THIS tick
        into the under-limit exit — not stay, not ram the packed cell."""
        config = default_config()
        healed, moves = self._recording_ranger("ranger-1", (0, 0), 2)
        r2 = self._make_ranger("ranger-2", (0, 1), 2)
        r3 = self._make_ranger("ranger-3", (0, 1), 2)  # exit 2 at the limit
        carrier = self._carrier("w1", (1, 0))           # exit 1: squeezable
        actions = self._run_choose_actions(
            (healed, r2, r3), config=config, home_team="R1, R2, R3",
            workers=(carrier,), obstacle_cells=self._WALLS,
        )
        detail = actions["ranger-1"]
        self.assertTrue(detail.startswith("MOVE:"), detail)
        self.assertEqual(len(moves), 1, detail)
        target = (moves[0].delta[0], moves[0].delta[1])
        self.assertNotIn(target, self._WALLS)
        self.assertNotEqual(target, (0, 0))
        self.assertNotEqual(target, (0, 1))  # never into the packed exit
        self.assertEqual(target, (1, 0))     # squeeze beside the lone carrier

    def test_cell_squatter_fallback_keeps_delivery_priority(self) -> None:
        """(b) With carrying workers queued at the core the mid-heal healer
        keeps the core cell (HEAL, no forced evacuation): the squatter
        fallback never overrides the heal-hold, delivery priority intact."""
        config = default_config()
        healer, moves = self._recording_ranger("ranger-1", (0, 0), 1)
        healer.heal = lambda: None
        carriers = (self._carrier("w1", (1, 0)), self._carrier("w2", (2, 0)))
        tactic._healing_units_prev = {"R1"}
        try:
            actions = self._run_choose_actions(
                (healer,), config=config, home_team="R1",
                workers=carriers, obstacle_cells=self._WALLS,
            )
        finally:
            tactic._healing_units_prev = set()
        self.assertEqual(actions.get("ranger-1"), "HEAL")
        self.assertEqual(moves, [])

    def test_busy_heal_spot_blocks_new_heal_return(self) -> None:
        """(c) Heal-return capacity control: a mid-heal (still damaged)
        healer on the core cell keeps the gate shut for fresh casualties —
        the second returnee must not stack up behind a blocked heal spot."""
        config = default_config()
        healer = self._make_ranger("ranger-1", (0, 0), 1)
        healer.heal = lambda: None
        casualty = self._make_ranger("ranger-2", (10, 0), 1)
        tactic._healing_units_prev = {"R1"}
        try:
            actions = self._run_choose_actions(
                (healer, casualty), config=config, home_team="R1, R2",
            )
        finally:
            tactic._healing_units_prev = set()
        self.assertEqual(actions.get("ranger-1"), "HEAL")
        detail = actions["ranger-2"]
        self.assertNotIn("heal-return", detail)
        self.assertNotEqual(detail, "HEAL")

    def test_chute_clear_never_pushes_into_packed_cell(self) -> None:
        """(d) chute-clear occupancy pre-check: the only outward cell is at
        the server limit, so the defender must not issue a chute-clear push
        into it (it would bounce on CELL_UNIT_LIMIT every tick). Without the
        pre-check the same setup deterministically returned a chute-clear
        MOVE into the packed cell."""
        config = default_config()
        config["home_patrol_radius"] = 1
        core_pos = (0, 0)
        packed = (1, -1)  # the only outward neighbour, at the cell limit
        # A unit id whose patrol slot is its own cell, so the fall-through
        # after the blocked chute-clear ends in a hold, not a re-target.
        uid = next(
            f"probe-{i}" for i in range(200)
            if tactic._home_patrol_goal(f"probe-{i}", core_pos, 1) == (1, 0)
        )
        unit = SimpleNamespace(id=uid, position=(1, 0), hp=2)
        moves: list = []
        unit.move = lambda d: moves.append(d)
        unit.wait = lambda: None
        walls = frozenset({(2, 0), (1, 1)})
        cell_counts = {packed: tactic._CELL_UNIT_LIMIT}
        prev_flag = tactic._chute_in_demand
        tactic._chute_in_demand = True  # carriers queuing at the chute
        try:
            action, detail = tactic._plan_home_combat(
                unit, unit_kind="ranger", enemies=(), obstacle_cells=walls,
                core_pos=core_pos, config=config, cell_counts=cell_counts,
            )
        finally:
            tactic._chute_in_demand = prev_flag
        self.assertNotIn("chute-clear", detail)
        for m in moves:
            self.assertNotEqual((1 + m.delta[0], m.delta[1]), packed)

    def test_cell_limit_streak_forces_detour(self) -> None:
        """Retry breaker: 3+ consecutive Ticks of CELL_UNIT_LIMIT rejections
        force one sidestep (cell-limit-detour) instead of the same blocked
        move; the streak is seeded by last Tick's count + this Tick's event."""
        config = default_config()
        ranger, moves = self._recording_ranger("ranger-1", (5, 0), 2)
        tactic._cell_limit_streak = {"ranger-1": 2}
        events = (SimpleNamespace(
            event_type="UNIT_MOVE_FAILED", reason_code="CELL_UNIT_LIMIT",
            actor_id="ranger-1", target_id=None, position=None,
            resource_amount=None,
        ),)
        try:
            actions = self._run_choose_actions(
                (ranger,), config=config, home_team="R1", events=events,
            )
        finally:
            tactic._cell_limit_streak = {}
        self.assertIn("cell-limit-detour", actions["ranger-1"])
        self.assertEqual(len(moves), 1)


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

        def sweep(self, direction):
            self.action = "SWEEP"
            self.arg = direction

        def shoot(self, target, expected_cell=None):
            self.action = "SHOOT"
            self.arg = (target, expected_cell)

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

    def test_march_squeezes_past_single_friendly_cell(self) -> None:
        # A marcher must not stall behind a cell holding one friendly unit:
        # the server stacks up to two units per cell, so it squeezes through
        # (same head-on wedge class as the W13/W22 worker deadlock).
        unit = self.Unit("w1", (0, 0), UnitType.WORKER)
        action, _ = self._plan(
            unit, "W1", (2, 0),
            occupied=frozenset({(0, 0), (1, 0)}),
            cell_counts={(0, 0): 1, (1, 0): 1},
        )
        self.assertEqual(action, "MOVE")
        self.assertEqual(unit.arg, Direction.RIGHT)

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

    def test_worker_evades_enemy_while_marching_attack_mode(self) -> None:
        unit = self.Unit("w1", (0, 0), UnitType.WORKER)
        enemy = self.Enemy((1, 0))
        action, detail = self._plan(
            unit, "W1", {"queue": [(9, 0)], "mode": "attack"}, enemies=(enemy,),
        )
        self.assertEqual(action, "MOVE")
        self.assertIn("flee", detail)

    def test_rush_mode_worker_ignores_enemy(self) -> None:
        # 赶路：不管任何东西 —— 工人贴着敌人也直接行军，不回避。
        unit = self.Unit("w1", (0, 0), UnitType.WORKER)
        enemy = self.Enemy((1, 0))
        action, detail = self._plan(
            unit, "W1", {"queue": [(9, 0)], "mode": "rush"}, enemies=(enemy,),
        )
        self.assertEqual(action, "MOVE")
        self.assertIn("waypoint", detail)
        self.assertNotIn("flee", detail)

    def test_vanguard_marches_without_firing(self) -> None:
        unit = self.Unit("v1", (0, 0), UnitType.VANGUARD)
        enemy = self.Enemy((1, 1))
        action, detail = self._plan(unit, "V1", (4, 4), enemies=(enemy,))
        self.assertEqual(action, "MOVE")
        self.assertIn("waypoint", detail)

    def test_attack_mode_vanguard_engages_enemy(self) -> None:
        # 攻击：前进道路上遇到敌人即接战 —— 相邻敌直接横扫。
        unit = self.Unit("v1", (0, 0), UnitType.VANGUARD)
        enemy = self.Enemy((1, 0))
        action, detail = self._plan(
            unit, "V1", {"queue": [(5, 0)], "mode": "attack"}, enemies=(enemy,),
        )
        self.assertEqual(action, "SWEEP")
        self.assertIn("enemy", detail)

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
                tactic._write_waypoints({
                    "W1": {"queue": [(10, -20), (1, 1)], "mode": "attack"},
                    "R3": {"queue": [(-5, 8)], "mode": "rush"},
                })
                loaded = tactic._load_waypoints()
        self.assertEqual(loaded, {
            "W1": {"queue": [(10, -20), (1, 1)], "mode": "attack"},
            "R3": {"queue": [(-5, 8)], "mode": "rush"},
        })

    def test_reaching_old_target_does_not_delete_concurrent_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "waypoints.json"
            with patch.object(tactic, "WAYPOINTS_PATH", path):
                tactic._write_waypoints({"W1": (5, 0)})
                tactic._write_waypoints({"W1": (9, 0)})
                tactic._remove_waypoint("W1", expected_target=(5, 0))
                remaining = tactic._load_waypoints()

        self.assertEqual(remaining, {"W1": {"queue": [(9, 0)], "mode": "rush"}})

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
        action, detail = self._plan(
            unit, "W1", {"queue": [(9, 0)], "mode": "attack"}, enemies=(enemy,),
        )
        self.assertEqual(action, "MOVE")
        self.assertIn("flee", detail)
        self.assertNotIn("w1", tactic._waypoint_stuck)  # fleeing is not stagnation

    def test_attack_mode_ranger_chases_enemy_not_target(self) -> None:
        # 攻击：可见敌人优先于目标 —— 游侠朝最近敌人逼进而不是朝目标行军。
        unit = self.Unit("r1", (0, 0), UnitType.RANGER)
        enemy = self.Enemy((4, 0))  # Chebyshev 4 > range 3: 不满足开火，走追击
        action, detail = self._plan(
            unit, "R1", {"queue": [(9, 9)], "mode": "attack"}, enemies=(enemy,),
        )
        self.assertEqual(action, "MOVE")
        self.assertIn("waypoint-engage", detail)

    def test_queue_advances_to_next_target_after_reaching_first(self) -> None:
        unit = self.Unit("w1", (5, 0), UnitType.WORKER)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "waypoints.json"
            with patch.object(tactic, "WAYPOINTS_PATH", path):
                tactic._write_waypoints({
                    "W1": {"queue": [(5, 0), (8, 0)], "mode": "rush"},
                })
                action, detail = self._plan(
                    unit, "W1", {"queue": [(5, 0), (8, 0)], "mode": "rush"},
                )
                remaining = tactic._load_waypoints()
        self.assertEqual(action, "WAIT")
        self.assertIn("waypoint-reached", detail)
        self.assertEqual(remaining["W1"]["queue"], [(8, 0)])  # 下一目标保留
        self.assertEqual(remaining["W1"]["mode"], "rush")

    def test_unreachable_target_is_skipped_queue_continues(self) -> None:
        # 卡死当前目标 → 跳过继续下一目标，而不是清空整条队列。
        unit = self.Unit("w1", (0, 0), UnitType.WORKER)
        obstacles = frozenset({(1, 0), (-1, 0), (0, 1), (0, -1)})
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "waypoints.json"
            with patch.object(tactic, "WAYPOINTS_PATH", path):
                tactic._write_waypoints({
                    "W1": {"queue": [(9, 0), (2, 0)], "mode": "rush"},
                })
                last = None
                for _ in range(tactic._WAYPOINT_STUCK_THRESHOLD):
                    last = self._plan(
                        unit, "W1", {"queue": [(9, 0), (2, 0)], "mode": "rush"},
                        obstacle_cells=obstacles,
                    )
                remaining = tactic._load_waypoints()
        self.assertIn("waypoint-unreachable", last[1])
        self.assertEqual(remaining["W1"]["queue"], [(2, 0)])  # 跳到了下一目标

    def test_legacy_waypoint_normalizes_to_rush_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "waypoints.json"
            with patch.object(tactic, "WAYPOINTS_PATH", path):
                tactic._write_waypoints({"W1": (5, 0)})
                loaded = tactic._load_waypoints()
        self.assertEqual(loaded, {"W1": {"queue": [(5, 0)], "mode": "rush"}})

    def test_reachable_waypoint_routes_around_long_wall(self) -> None:
        unit = self.Unit("v1", (0, 0), UnitType.VANGUARD)
        obstacles = frozenset((1, y) for y in range(-16, 17))
        details = []
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "waypoints.json"
            with patch.object(tactic, "WAYPOINTS_PATH", path):
                tactic._write_waypoints({"V1": (2, 0)})
                for _ in range(50):
                    action, detail = self._plan(
                        unit,
                        "V1",
                        (2, 0),
                        obstacle_cells=obstacles,
                    )
                    details.append(detail)
                    if action == "MOVE":
                        dx, dy = unit.arg.delta
                        unit.position = (unit.position[0] + dx, unit.position[1] + dy)
                    if "waypoint-reached" in detail:
                        break
                remaining = tactic._load_waypoints()

        self.assertEqual(unit.position, (2, 0))
        self.assertTrue(any("waypoint-reached" in detail for detail in details))
        self.assertFalse(any("waypoint-unreachable" in detail for detail in details))
        self.assertNotIn("V1", remaining)


class DashboardWaypointTests(unittest.TestCase):
    def test_set_append_remove_mode_clear_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wp_file = Path(temp_dir) / "waypoints.json"
            with patch.object(dashboard, "WAYPOINTS_FILE", str(wp_file)):
                self.assertTrue(dashboard.set_waypoint("W3", 10, 20)["ok"])
                self.assertEqual(
                    dashboard.load_waypoints(),
                    {"W3": {"queue": [[10, 20]], "mode": "attack"}},
                )
                # Appending grows the queue; the given mode rides along.
                self.assertTrue(dashboard.set_waypoint("W3", 11, 21, mode="rush")["ok"])
                self.assertEqual(dashboard.load_waypoints(), {
                    "W3": {"queue": [[10, 20], [11, 21]], "mode": "rush"},
                })
                # Remove one queued target by index.
                self.assertTrue(dashboard.remove_waypoint("W3", index=0)["ok"])
                self.assertEqual(
                    dashboard.load_waypoints(),
                    {"W3": {"queue": [[11, 21]], "mode": "rush"}},
                )
                # Removing the last target clears the unit entirely.
                self.assertTrue(dashboard.remove_waypoint("W3", index=0)["ok"])
                self.assertEqual(dashboard.load_waypoints(), {})
                self.assertFalse(dashboard.remove_waypoint("W3")["ok"])
                # Mode switch on an existing queue.
                self.assertTrue(dashboard.set_waypoint("V2", -5, 8)["ok"])
                self.assertTrue(dashboard.set_waypoint_mode("V2", "rush")["ok"])
                self.assertEqual(
                    dashboard.load_waypoints(),
                    {"V2": {"queue": [[-5, 8]], "mode": "rush"}},
                )
                self.assertTrue(dashboard.clear_waypoints()["ok"])
                self.assertEqual(dashboard.load_waypoints(), {})

    def test_render_waypoints_panel_controls(self) -> None:
        html = dashboard.render_waypoints_panel(
            {
                "W3": {"queue": [[10, 20], [30, 40]], "mode": "attack"},
                "V2": {"queue": [[5, 5]], "mode": "rush"},
            },
            workers=["W3"], vanguards=["V2"], rangers=[],
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
        self.assertIn('id="wpMode"', html)
        self.assertIn('<option value="attack" selected>攻击</option>', html)
        # 每个目标一个可删 chip；每个单位一个模式切换 + 清空按钮。
        self.assertIn('data-wp-remove="W3" data-wp-index="0"', html)
        self.assertIn('data-wp-remove="W3" data-wp-index="1"', html)
        self.assertIn('data-wp-mode-toggle="W3" data-mode="attack"', html)
        self.assertIn('data-wp-mode-toggle="V2" data-mode="rush"', html)
        self.assertIn('data-wp-clear-unit="W3"', html)
        self.assertIn("(10, 20)", html)
        self.assertIn("(30, 40)", html)
        self.assertIn("赶路", html)

    def test_svg_draws_waypoint_marker(self) -> None:
        rec = {
            "core_pos": [0, 0],
            "workers": [], "vanguards": [], "rangers": [], "enemies": [],
            "resource_cells": [],
        }
        memory = {"obstacles": [], "resources": []}
        svg = dashboard.render_svg(rec, memory, waypoints={"W3": [0, 0]})
        self.assertIn("W3→(0,0)", svg)

    def test_svg_draws_each_queued_target_marker(self) -> None:
        rec = {
            "core_pos": [0, 0],
            "workers": [], "vanguards": [], "rangers": [], "enemies": [],
            "resource_cells": [],
        }
        memory = {"obstacles": [], "resources": []}
        svg = dashboard.render_svg(
            rec, memory,
            waypoints={"W3": {"queue": [[0, 0], [0, 1]], "mode": "attack"}},
        )
        self.assertIn("W3→(0,0)", svg)
        self.assertIn("W3→(0,1)", svg)

    def test_waypoint_name_rejects_markup_and_renderer_escapes(self) -> None:
        malicious = '\"><img src=x onerror=alert(1)>'
        with tempfile.TemporaryDirectory() as temp_dir:
            wp_file = Path(temp_dir) / "waypoints.json"
            with patch.object(dashboard, "WAYPOINTS_FILE", str(wp_file)):
                with self.assertRaises(ValueError):
                    dashboard.set_waypoint(malicious, 1, 2)

        html_output = dashboard.render_waypoints_panel(
            {malicious: [1, 2]}, workers=[], vanguards=[], rangers=[],
        )
        self.assertNotIn("<img", html_output)
        self.assertIn("&lt;img", html_output)

    def test_concurrent_waypoint_updates_preserve_every_target(self) -> None:
        failures = []
        with tempfile.TemporaryDirectory() as temp_dir:
            wp_file = Path(temp_dir) / "waypoints.json"
            with patch.object(dashboard, "WAYPOINTS_FILE", str(wp_file)):
                dashboard.clear_waypoints()
                barrier = threading.Barrier(24)

                def update(index: int) -> None:
                    try:
                        barrier.wait()
                        dashboard.set_waypoint(f"W{index + 1}", index, -index)
                    except Exception as exc:
                        failures.append(exc)

                threads = [threading.Thread(target=update, args=(i,)) for i in range(24)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                saved = dashboard.load_waypoints()

        self.assertEqual(failures, [])
        self.assertEqual(len(saved), 24)


class ManualSelfDestructTests(unittest.TestCase):
    """Dashboard「自裁」commands consumed by the tactic each Tick."""

    def test_load_and_prune_drops_dead_units(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "self_destruct.json"
            with patch.object(tactic, "SELF_DESTRUCT_PATH", path):
                tactic._write_self_destructs_unlocked({"W1", "V2", "R3"})
                pending = tactic._load_and_prune_self_destructs({"W1", "R3"})
                persisted = tactic._load_self_destructs_unlocked()

        self.assertEqual(pending, {"W1", "R3"})
        self.assertEqual(persisted, {"W1", "R3"})

    def test_load_and_prune_preserves_all_alive_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "self_destruct.json"
            with patch.object(tactic, "SELF_DESTRUCT_PATH", path):
                tactic._write_self_destructs_unlocked({"W1", "V2"})
                pending = tactic._load_and_prune_self_destructs({"W1", "V2"})

        self.assertEqual(pending, {"W1", "V2"})

    def test_remove_self_destructs_preserves_concurrent_adds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "self_destruct.json"
            with patch.object(tactic, "SELF_DESTRUCT_PATH", path):
                tactic._write_self_destructs_unlocked({"W1", "V2"})
                # A dashboard write lands after the tactic's read.
                tactic._write_self_destructs_unlocked({"W1", "V2", "W9"})
                tactic._remove_self_destructs({"W1", "V2"})
                persisted = tactic._load_self_destructs_unlocked()

        self.assertEqual(persisted, {"W9"})

    def test_dashboard_request_flows_into_tactic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sd_file = Path(temp_dir) / "self_destruct.json"
            with patch.object(dashboard, "SELF_DESTRUCT_FILE", str(sd_file)), \
                 patch.object(dashboard, "BATTLE_LOG_FILE", str(Path(temp_dir) / "battle_log.jsonl")), \
                 patch.object(tactic, "SELF_DESTRUCT_PATH", sd_file):
                self.assertTrue(dashboard.request_self_destruct("W1")["ok"])
                pending = tactic._load_and_prune_self_destructs({"W1"})

        self.assertEqual(pending, {"W1"})


class DashboardSelfDestructTests(unittest.TestCase):
    def test_request_roundtrip_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sd_file = Path(temp_dir) / "self_destruct.json"
            with patch.object(dashboard, "SELF_DESTRUCT_FILE", str(sd_file)), \
                 patch.object(dashboard, "BATTLE_LOG_FILE", str(Path(temp_dir) / "battle_log.jsonl")):
                self.assertTrue(dashboard.request_self_destruct("W3")["ok"])
                self.assertTrue(dashboard.request_self_destruct("W3")["ok"])
                self.assertTrue(dashboard.request_self_destruct("v2")["ok"])
                pending = dashboard._read_self_destruct_file()

        self.assertEqual(pending, {"W3", "V2"})

    def test_invalid_name_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sd_file = Path(temp_dir) / "self_destruct.json"
            with patch.object(dashboard, "SELF_DESTRUCT_FILE", str(sd_file)):
                with self.assertRaises(ValueError):
                    dashboard.request_self_destruct('<img onerror=alert(1)>')
                with self.assertRaises(ValueError):
                    dashboard.request_self_destruct("not-a-unit")

    def test_unit_cards_render_self_destruct_button(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "tactic_log.jsonl"
            rec = {
                "tick": 1,
                "plan_unit_actions": {
                    "aaaaaaaa": "HARVEST",
                    "bbbbbbbb": "MOVE",
                    "cccccccc": "MOVE",
                },
                "workers": [{
                    "id": "aaaaaaaa", "name": "W1", "pos": [0, 0],
                    "target": [2, 0], "path": [[0, 0], [1, 0], [2, 0]],
                    "path_complete": True, "cargo": 0, "hp": 3,
                }],
                "vanguards": [{"id": "bbbbbbbb", "name": "V1", "pos": [1, 1], "hp": 5}],
                "rangers": [{"id": "cccccccc", "name": "R1", "pos": [2, 2], "hp": 5}],
                "resources": 0,
                "resource_capacity": 50,
                "visible_enemies": 0,
                "resource_cells": [],
                "core_pos": [0, 0],
                "core_name": "C1",
            }
            log_path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
            game_stats_path = Path(temp_dir) / "game_stats.json"
            config_path = Path(temp_dir) / "tactic_config.json"
            with patch.object(dashboard, "LOG_FILE", str(log_path)), \
                 patch.object(dashboard, "MAP_FILE", str(Path(temp_dir) / "map_memory.json")), \
                 patch.object(dashboard, "WAYPOINTS_FILE", str(Path(temp_dir) / "waypoints.json")), \
                 patch.object(dashboard, "BATTLE_LOG_FILE", str(Path(temp_dir) / "battle_log.jsonl")), \
                 patch.object(game_stats, "STATS_PATH", game_stats_path), \
                 patch.object(tactic_config, "CONFIG_PATH", config_path):
                parts = dashboard.build_parts()

        self.assertIsNotNone(parts)
        for unit_name in ("W1", "V1", "R1"):
            self.assertIn(
                f'data-sd-unit="{unit_name}"',
                parts["workersHtml"] + parts["vgHtml"] + parts["rgHtml"],
                f"{unit_name} card should carry a 自裁 button",
            )
        self.assertIn('class="sd-btn"', parts["workersHtml"])
        self.assertIn('class="sd-btn"', parts["vgHtml"])
        self.assertIn('class="sd-btn"', parts["rgHtml"])

    def test_unit_cards_carry_map_focus_coords(self) -> None:
        """Each right-rail unit card embeds its live world position so a click
        can jump the map view to the unit (focusWorld)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "tactic_log.jsonl"
            rec = {
                "tick": 1,
                "plan_unit_actions": {},
                "workers": [{
                    "id": "aaaaaaaa", "name": "W1", "pos": [3, -2],
                    "target": [3, -2], "path": [], "path_complete": True,
                    "cargo": 0, "hp": 3,
                }],
                "vanguards": [{"id": "bbbbbbbb", "name": "V1", "pos": [1, 1], "hp": 5}],
                "rangers": [{"id": "cccccccc", "name": "R1", "pos": [-4, 7], "hp": 5}],
                "resources": 0,
                "resource_capacity": 50,
                "visible_enemies": 0,
                "resource_cells": [],
                "core_pos": [0, 0],
                "core_name": "C1",
            }
            log_path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
            game_stats_path = Path(temp_dir) / "game_stats.json"
            config_path = Path(temp_dir) / "tactic_config.json"
            with patch.object(dashboard, "LOG_FILE", str(log_path)), \
                 patch.object(dashboard, "MAP_FILE", str(Path(temp_dir) / "map_memory.json")), \
                 patch.object(dashboard, "WAYPOINTS_FILE", str(Path(temp_dir) / "waypoints.json")), \
                 patch.object(dashboard, "BATTLE_LOG_FILE", str(Path(temp_dir) / "battle_log.jsonl")), \
                 patch.object(game_stats, "STATS_PATH", game_stats_path), \
                 patch.object(tactic_config, "CONFIG_PATH", config_path):
                parts = dashboard.build_parts()

        self.assertIsNotNone(parts)
        self.assertIn('data-focus-wx="3" data-focus-wy="-2"', parts["workersHtml"])
        self.assertIn('data-focus-wx="1" data-focus-wy="1"', parts["vgHtml"])
        self.assertIn('data-focus-wx="-4" data-focus-wy="7"', parts["rgHtml"])


class ManualHoldTests(unittest.TestCase):
    """Dashboard「驻守」commands consumed by the tactic each Tick."""

    def _hold_unit(self, uid, pos, unit_type):
        actions = {"moves": [], "shoots": [], "sweeps": [], "waits": 0}

        class U:
            def __init__(self):
                self.id = uid
                self.position = pos
                self.unit_type = unit_type

            def move(self, direction):
                actions["moves"].append(direction)

            def shoot(self, target, expected_cell=None):
                actions["shoots"].append((
                    tuple(target.position),
                    tuple(expected_cell) if expected_cell else None,
                ))

            def shoot_cell(self, cell):
                actions["shoots"].append(("cell", tuple(cell)))

            def sweep(self, direction):
                actions["sweeps"].append(direction)

            def wait(self):
                actions["waits"] += 1

        return U(), actions

    def _enemy(self, uid, pos, etype="VANGUARD"):
        return SimpleNamespace(id=uid, position=pos, unit_type=etype)

    def test_worker_holds_stationary(self) -> None:
        unit, actions = self._hold_unit("w1", (0, 0), UnitType.WORKER)
        enemy = self._enemy("e1", (0, 1))
        result = tactic._plan_hold(unit, (enemy,), frozenset(), {})
        self.assertEqual(result[0], "WAIT")
        self.assertIn("hold-stationary", result[1])
        self.assertEqual(actions["waits"], 1)
        self.assertEqual(actions["moves"], [])

    def test_vanguard_sweeps_adjacent_enemy(self) -> None:
        unit, actions = self._hold_unit("v1", (0, 0), UnitType.VANGUARD)
        enemy = self._enemy("e1", (0, 1))
        action, _ = tactic._plan_hold(unit, (enemy,), frozenset(), {})
        self.assertEqual(action, "SWEEP")
        self.assertEqual(actions["sweeps"], [Direction.DOWN])

    def test_vanguard_predictive_sweep_of_incoming_enemy(self) -> None:
        # Enemy at (0, 2) moving DOWN toward us -> predicted next cell (0, 1).
        tactic._enemy_motion_tracks.clear()
        tactic._enemy_motion_tracks["e1"] = [
            (100, (0, 4)), (101, (0, 3)), (102, (0, 2)),
        ]
        unit, actions = self._hold_unit("v1", (0, 0), UnitType.VANGUARD)
        enemy = self._enemy("e1", (0, 2))
        try:
            action, detail = tactic._plan_hold(unit, (enemy,), frozenset(), {})
        finally:
            tactic._enemy_motion_tracks.clear()
        self.assertEqual(action, "SWEEP")
        self.assertEqual(actions["sweeps"], [Direction.DOWN])
        self.assertIn("hold-predict", detail)

    def test_vanguard_waits_when_nothing_in_range(self) -> None:
        unit, actions = self._hold_unit("v1", (0, 0), UnitType.VANGUARD)
        enemy = self._enemy("e1", (5, 5))
        result = tactic._plan_hold(unit, (enemy,), frozenset(), {})
        self.assertEqual(result[0], "WAIT")
        self.assertEqual(actions["waits"], 1)

    def test_ranger_predictive_fire_at_moving_enemy(self) -> None:
        # Enemy in range (Chebyshev 2) moving toward us: shoot the predicted
        # next cell (1, 0) instead of the stale current cell (2, 0).
        tactic._enemy_motion_tracks.clear()
        tactic._enemy_motion_tracks["e1"] = [
            (100, (4, 0)), (101, (3, 0)), (102, (2, 0)),
        ]
        unit, actions = self._hold_unit("r1", (0, 0), UnitType.RANGER)
        enemy = self._enemy("e1", (2, 0))
        try:
            action, detail = tactic._plan_hold(unit, (enemy,), frozenset(), {})
        finally:
            tactic._enemy_motion_tracks.clear()
        self.assertEqual(action, "SHOOT")
        self.assertEqual(actions["shoots"][0], ((2, 0), (1, 0)))
        self.assertIn("kite-lead", detail)

    def test_ranger_waits_when_enemy_out_of_range(self) -> None:
        unit, actions = self._hold_unit("r1", (0, 0), UnitType.RANGER)
        enemy = self._enemy("e1", (9, 9))
        result = tactic._plan_hold(unit, (enemy,), frozenset(), {})
        self.assertEqual(result[0], "WAIT")
        self.assertEqual(actions["waits"], 1)

    def test_load_and_prune_drops_dead_units(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "holds.json"
            with patch.object(tactic, "HOLDS_PATH", path):
                tactic._holds_sig_cache.clear()
                tactic._holds_cached.clear()
                tactic._write_holds_unlocked({"W1", "V2", "R3"})
                pending = tactic._load_and_prune_holds({"W1", "R3"})
                persisted = tactic._load_holds_unlocked()

        self.assertEqual(pending, {"W1", "R3"})
        self.assertEqual(persisted, {"W1", "R3"})

    def test_dashboard_request_flows_into_tactic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            holds_file = Path(temp_dir) / "holds.json"
            with patch.object(dashboard, "HOLDS_FILE", str(holds_file)), \
                 patch.object(dashboard, "BATTLE_LOG_FILE", str(Path(temp_dir) / "battle_log.jsonl")), \
                 patch.object(tactic, "HOLDS_PATH", holds_file):
                self.assertTrue(dashboard.set_hold("W1")["ok"])
                held = tactic._load_and_prune_holds({"W1"})
                self.assertEqual(held, {"W1"})
                self.assertTrue(dashboard.clear_hold("W1")["ok"])
                held = tactic._load_and_prune_holds({"W1"})
                self.assertEqual(held, set())


class DashboardHoldTests(unittest.TestCase):
    def test_set_and_clear_roundtrip_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            holds_file = Path(temp_dir) / "holds.json"
            with patch.object(dashboard, "HOLDS_FILE", str(holds_file)), \
                 patch.object(dashboard, "BATTLE_LOG_FILE", str(Path(temp_dir) / "battle_log.jsonl")):
                self.assertTrue(dashboard.set_hold("W3")["ok"])
                self.assertTrue(dashboard.set_hold("W3")["ok"])  # idempotent
                self.assertTrue(dashboard.set_hold("v2")["ok"])
                self.assertEqual(dashboard.load_holds(), {"W3", "V2"})
                self.assertTrue(dashboard.clear_hold("W3")["ok"])
                self.assertEqual(dashboard.load_holds(), {"V2"})

    def test_invalid_name_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            holds_file = Path(temp_dir) / "holds.json"
            with patch.object(dashboard, "HOLDS_FILE", str(holds_file)):
                with self.assertRaises(ValueError):
                    dashboard.set_hold('<img onerror=alert(1)>')
                with self.assertRaises(ValueError):
                    dashboard.clear_hold("not-a-unit")

    def test_unit_cards_render_hold_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "tactic_log.jsonl"
            rec = {
                "tick": 1,
                "plan_unit_actions": {},
                "workers": [{
                    "id": "aaaaaaaa", "name": "W1", "pos": [0, 0],
                    "target": [2, 0], "path": [[0, 0], [1, 0], [2, 0]],
                    "path_complete": True, "cargo": 0, "hp": 3,
                }],
                "vanguards": [{"id": "bbbbbbbb", "name": "V1", "pos": [1, 1], "hp": 5}],
                "rangers": [{"id": "cccccccc", "name": "R1", "pos": [2, 2], "hp": 5}],
                "resources": 0,
                "resource_capacity": 50,
                "visible_enemies": 0,
                "resource_cells": [],
                "core_pos": [0, 0],
                "core_name": "C1",
            }
            log_path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
            game_stats_path = Path(temp_dir) / "game_stats.json"
            config_path = Path(temp_dir) / "tactic_config.json"
            holds_file = Path(temp_dir) / "holds.json"
            holds_file.write_text(json.dumps({
                "updated_at": "2025-01-01T00:00:00",
                "units": ["V1"],
            }), encoding="utf-8")
            with patch.object(dashboard, "LOG_FILE", str(log_path)), \
                 patch.object(dashboard, "MAP_FILE", str(Path(temp_dir) / "map_memory.json")), \
                 patch.object(dashboard, "WAYPOINTS_FILE", str(Path(temp_dir) / "waypoints.json")), \
                 patch.object(dashboard, "BATTLE_LOG_FILE", str(Path(temp_dir) / "battle_log.jsonl")), \
                 patch.object(dashboard, "HOLDS_FILE", str(holds_file)), \
                 patch.object(game_stats, "STATS_PATH", game_stats_path), \
                 patch.object(tactic_config, "CONFIG_PATH", config_path):
                parts = dashboard.build_parts()

        self.assertIsNotNone(parts)
        for unit_name in ("W1", "V1", "R1"):
            self.assertIn(
                f'data-hold-unit="{unit_name}"',
                parts["workersHtml"] + parts["vgHtml"] + parts["rgHtml"],
                f"{unit_name} card should carry a 驻守 toggle",
            )
        # V1 is held: its card is highlighted and shows 解除驻守, the others 驻守.
        self.assertIn('class="unit combat hold"', parts["vgHtml"])
        self.assertIn("解除驻守", parts["vgHtml"])
        self.assertNotIn("解除驻守", parts["workersHtml"])
        self.assertIn("驻守", parts["workersHtml"])


class EnemySightingsMemoryTests(unittest.TestCase):
    """Stale enemy sightings are removed only when a friendly unit can
    actually see the cell (its own vision radius + unobstructed line of
    sight), never just because it is within the old flat range-5 check."""

    @staticmethod
    def _unit(pos: tuple[int, int]):
        return SimpleNamespace(position=pos)

    @staticmethod
    def _core(pos: tuple[int, int]):
        return SimpleNamespace(position=pos)

    @staticmethod
    def _turn(*, core=None, workers=(), vanguards=(), rangers=(), visible=(), obstacles=(), tick=0):
        return SimpleNamespace(
            core=core,
            workers=tuple(workers),
            vanguards=tuple(vanguards),
            rangers=tuple(rangers),
            visible_enemies=tuple(visible),
            obstacle_cells=frozenset(obstacles),
            tick=tick,
        )

    def setUp(self) -> None:
        self._enemies_backup = set(tactic._enemy_memory)
        self._enemy_types_backup = dict(tactic._enemy_memory_types)
        self._enemy_ticks_backup = dict(tactic._enemy_memory_ticks)
        self._obstacles_backup = set(tactic._obstacle_memory)
        self._dirty_backup = tactic._map_dirty
        tactic._enemy_memory.clear()
        tactic._enemy_memory_types.clear()
        tactic._enemy_memory_ticks.clear()
        tactic._obstacle_memory.clear()

    def tearDown(self) -> None:
        tactic._enemy_memory.clear()
        tactic._enemy_memory.update(self._enemies_backup)
        tactic._enemy_memory_types.clear()
        tactic._enemy_memory_types.update(self._enemy_types_backup)
        tactic._enemy_memory_ticks.clear()
        tactic._enemy_memory_ticks.update(self._enemy_ticks_backup)
        tactic._obstacle_memory.clear()
        tactic._obstacle_memory.update(self._obstacles_backup)
        tactic._map_dirty = self._dirty_backup

    def test_worker_within_old_range_5_keeps_sighting_it_cannot_see(self) -> None:
        # Worker vision is 3; a worker 4 cells away must NOT erase the marker
        # (the old flat range-5 check would have).
        tactic._enemy_memory.update({(5, 0)})
        turn = self._turn(workers=[self._unit((1, 0))])
        tactic._update_enemy_sightings(turn)
        self.assertIn((5, 0), tactic._enemy_memory)

    def test_visible_enemy_records_last_seen_tick(self) -> None:
        # The arena page ranks memory markers by recency, so every sighting
        # must carry the tick it was last observed at.
        turn = self._turn(visible=[self._unit((2, 3))], tick=77)
        tactic._update_enemy_sightings(turn)
        self.assertEqual(tactic._enemy_memory_ticks.get((2, 3)), 77)
        self.assertTrue(tactic._map_dirty)

        # Re-sighting refreshes the tick; a confirmed-empty sighting drops it.
        tactic._update_enemy_sightings(self._turn(visible=[self._unit((2, 3))], tick=81))
        self.assertEqual(tactic._enemy_memory_ticks.get((2, 3)), 81)
        tactic._update_enemy_sightings(self._turn(rangers=[self._unit((2, 3))], tick=85))
        self.assertNotIn((2, 3), tactic._enemy_memory)
        self.assertNotIn((2, 3), tactic._enemy_memory_ticks)

    def test_sightings_payload_round_trip_keeps_ticks(self) -> None:
        positions, types, ticks = tactic._enemy_sightings_from_payload(
            [[1, 2, "CORE", 9], [3, 4], [5, 6, "RANGER"]]
        )
        self.assertEqual(positions, {(1, 2), (3, 4), (5, 6)})
        self.assertEqual(types, {(1, 2): "CORE", (5, 6): "RANGER"})
        self.assertEqual(ticks, {(1, 2): 9})

    def test_dashboard_parse_enemy_sighting_extracts_tick(self) -> None:
        self.assertEqual(
            dashboard._parse_enemy_sighting([1, 2, "CORE", 55]),
            ((1, 2), "CORE", 55),
        )
        # Legacy entries without a timestamp rank as oldest.
        self.assertEqual(dashboard._parse_enemy_sighting([3, 4]), ((3, 4), "ENEMY", 0))
        self.assertEqual(
            dashboard._parse_enemy_sighting({"pos": [5, 6], "type": "WORKER", "tick": 12}),
            ((5, 6), "WORKER", 12),
        )

    def test_dashboard_ranks_enemy_sightings_with_per_type_cap(self) -> None:
        # The old global top-20 cutoff pushed every older CORE memory out
        # once fresher unit sightings outnumbered them; each type must keep
        # its own newest 21 entries so the arena's per-filter cap can fill.
        parsed = (
            [((i, 0), "CORE", i) for i in range(1, 26)]
            + [((i, 100), "VANGUARD", 100 + i) for i in range(1, 26)]
        )

        ranked = dashboard._rank_enemy_sightings(parsed)

        self.assertEqual(len(ranked), 42)
        core_ticks = [tick for _, etype, tick in ranked if etype == "CORE"]
        self.assertEqual(core_ticks, list(range(25, 4, -1)))
        # The overall order still reads newest first across types.
        self.assertEqual(ranked[0][2], 125)
        self.assertEqual(ranked[21][2], 25)

    def test_ranger_within_own_radius_confirms_empty_and_clears(self) -> None:
        # Ranger vision is 5; standing 5 away with clear sight confirms the
        # cell is empty and the stale marker goes away.
        tactic._enemy_memory.update({(5, 0)})
        turn = self._turn(rangers=[self._unit((0, 0))])
        tactic._update_enemy_sightings(turn)
        self.assertNotIn((5, 0), tactic._enemy_memory)

    def test_worker_adjacent_confirms_empty_and_clears(self) -> None:
        tactic._enemy_memory.update({(1, 0)})
        turn = self._turn(workers=[self._unit((0, 0))])
        tactic._update_enemy_sightings(turn)
        self.assertNotIn((1, 0), tactic._enemy_memory)

    def test_obstacle_between_unit_and_sighting_keeps_memory(self) -> None:
        # A wall on the straight line blocks sight, so the marker survives even
        # though the ranger is within its vision radius.
        tactic._enemy_memory.update({(3, 0)})
        turn = self._turn(
            rangers=[self._unit((0, 0))],
            obstacles=[(1, 0)],
        )
        tactic._update_enemy_sightings(turn)
        self.assertIn((3, 0), tactic._enemy_memory)

    def test_visible_enemy_still_in_memory_is_kept(self) -> None:
        tactic._enemy_memory.update({(5, 0)})
        turn = self._turn(
            rangers=[self._unit((0, 0))],
            visible=[self._unit((5, 0))],
        )
        tactic._update_enemy_sightings(turn)
        self.assertIn((5, 0), tactic._enemy_memory)

    def test_new_visible_enemy_is_recorded(self) -> None:
        turn = self._turn(visible=[self._unit((7, 2))])
        tactic._update_enemy_sightings(turn)
        self.assertIn((7, 2), tactic._enemy_memory)

    def test_visible_enemy_type_is_recorded_for_dashboard(self) -> None:
        # The dashboard needs the last-known unit type so an out-of-vision CORE
        # can be told from a worker scout without re-scouting.
        enemy = SimpleNamespace(position=(7, 2), kind=SimpleNamespace(value="CORE"))
        tactic._update_enemy_sightings(self._turn(visible=[enemy]))
        self.assertEqual(tactic._enemy_memory_types.get((7, 2)), "CORE")
        # Type-less stubs default to ENEMY, never None.
        tactic._update_enemy_sightings(self._turn(visible=[self._unit((9, 9))]))
        self.assertEqual(tactic._enemy_memory_types.get((9, 9)), "ENEMY")

    def test_worker_on_core_cell_does_not_downgrade_sighting_type(self) -> None:
        # An enemy CORE has its spawned workers standing on its own square, so
        # a cell can hold CORE + WORKER in the same tick.  The last-iterated
        # unit must not win: a worker seen there must never relabel the HQ as a
        # worker scout.
        core = SimpleNamespace(position=(7, 2), kind=SimpleNamespace(value="CORE"))
        worker = SimpleNamespace(position=(7, 2), kind=SimpleNamespace(value="WORKER"))
        # Worker processed after the CORE (order matters for the old code).
        tactic._update_enemy_sightings(self._turn(visible=[core, worker]))
        self.assertEqual(tactic._enemy_memory_types.get((7, 2)), "CORE")
        # And a fresh CORE sighting still upgrades a previously-scouted cell.
        tactic._enemy_memory.update({(4, 4)})
        tactic._enemy_memory_types[(4, 4)] = "WORKER"
        tactic._update_enemy_sightings(
            self._turn(visible=[SimpleNamespace(position=(4, 4), kind=SimpleNamespace(value="CORE"))])
        )
        self.assertEqual(tactic._enemy_memory_types.get((4, 4)), "CORE")

    def test_sighting_type_survives_disk_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "map_memory.json"
            with patch.object(tactic, "MAP_MEMORY_PATH", path):
                tactic._enemy_memory.clear()
                tactic._enemy_memory_types.clear()
                tactic._enemy_memory.update({(7, 2), (3, 3)})
                tactic._enemy_memory_types[(7, 2)] = "CORE"
                tactic._enemy_memory_ticks[(7, 2)] = 41
                tactic._map_dirty = True
                tactic._save_map_memory(force=True)
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    sorted(saved["enemy_sightings"]),
                    [[3, 3, "ENEMY", 0], [7, 2, "CORE", 41]],
                )

                tactic._enemy_memory.clear()
                tactic._enemy_memory_types.clear()
                tactic._enemy_memory_ticks.clear()
                tactic._load_map_memory()
                self.assertEqual(tactic._enemy_memory_types.get((7, 2)), "CORE")
                self.assertEqual(tactic._enemy_memory_ticks.get((7, 2)), 41)
                self.assertIn((3, 3), tactic._enemy_memory)

    def test_stale_removal_also_drops_recorded_type(self) -> None:
        tactic._enemy_memory.update({(5, 0)})
        tactic._enemy_memory_types[(5, 0)] = "RANGER"
        turn = self._turn(rangers=[self._unit((0, 0))])
        tactic._update_enemy_sightings(turn)
        self.assertNotIn((5, 0), tactic._enemy_memory)
        self.assertNotIn((5, 0), tactic._enemy_memory_types)

    def test_vision_obstructed_axis_lines(self) -> None:
        self.assertTrue(tactic._vision_obstructed((0, 0), (0, 3), {(0, 2)}))
        self.assertTrue(tactic._vision_obstructed((0, 0), (0, 3), {(0, 1)}))
        self.assertFalse(tactic._vision_obstructed((0, 0), (0, 3), {(0, 3)}))
        self.assertFalse(tactic._vision_obstructed((0, 0), (0, 3), {(0, 0)}))
        self.assertFalse(tactic._vision_obstructed((0, 0), (0, 3), {(1, 1)}))
        self.assertTrue(tactic._vision_obstructed((0, 0), (3, 0), {(1, 0)}))
        self.assertFalse(tactic._vision_obstructed((0, 0), (3, 0), {(0, 1)}))
        self.assertFalse(tactic._vision_obstructed((0, 0), (3, 0), {(3, 0)}))

    def test_vision_obstructed_diagonal_corner_rule(self) -> None:
        # (0,0)→(2,2) crosses the shared corner at (1,1); an obstacle in either
        # adjacent cell, or on the line itself, blocks. Source/target and cells
        # beside the line do not.
        self.assertTrue(tactic._vision_obstructed((0, 0), (2, 2), {(1, 0)}))
        self.assertTrue(tactic._vision_obstructed((0, 0), (2, 2), {(0, 1)}))
        self.assertTrue(tactic._vision_obstructed((0, 0), (2, 2), {(1, 1)}))
        self.assertTrue(tactic._vision_obstructed((0, 0), (2, 2), {(2, 1)}))
        self.assertTrue(tactic._vision_obstructed((0, 0), (2, 2), {(1, 2)}))
        self.assertFalse(tactic._vision_obstructed((0, 0), (2, 2), {(0, 0)}))
        self.assertFalse(tactic._vision_obstructed((0, 0), (2, 2), {(2, 2)}))
        self.assertFalse(tactic._vision_obstructed((0, 0), (2, 2), {(2, 0)}))

    def test_vision_obstructed_side_cell_never_blocks_shallow_line(self) -> None:
        # (0,0)→(3,1) passes through (1,0),(2,0) and the endpoint corner; the
        # cell (1,1) sits beside the line and must not block it.
        self.assertTrue(tactic._vision_obstructed((0, 0), (3, 1), {(1, 0)}))
        self.assertTrue(tactic._vision_obstructed((0, 0), (3, 1), {(2, 0)}))
        self.assertTrue(tactic._vision_obstructed((0, 0), (3, 1), {(2, 1)}))
        self.assertFalse(tactic._vision_obstructed((0, 0), (3, 1), {(1, 1)}))


class BattleLogTests(unittest.TestCase):
    """Categorized battle log: append_jsonl persistence, event classification,
    discovery rows, and the dashboard reader/panel render."""

    @staticmethod
    def _event(event_type, reason=None, actor=None, target=None, values=None, pos=None):
        return SimpleNamespace(
            event_type=event_type, reason_code=reason,
            actor_id=actor, target_id=target, values=values, position=pos,
        )

    @staticmethod
    def _unit(uid, unit_type, pos):
        return SimpleNamespace(id=uid, unit_type=unit_type, position=pos)

    def setUp(self) -> None:
        self._names = dict(tactic._object_names)
        self._counters = dict(tactic._object_name_counters)
        self._tick = tactic.turn_context.tick
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        tactic.turn_context.tick = 42

    def tearDown(self) -> None:
        tactic._object_names.clear()
        tactic._object_names.update(self._names)
        tactic._object_name_counters.clear()
        tactic._object_name_counters.update(self._counters)
        tactic.turn_context.tick = self._tick

    def test_append_jsonl_appends_and_rotates(self) -> None:
        from state_io import append_jsonl
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "battle_log.jsonl"
            append_jsonl(path, [{"cat": "discover", "msg": "a"}, {"cat": "kill", "msg": "b"}])
            append_jsonl(path, [{"cat": "defeat", "msg": "c"}])
            lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
            self.assertEqual([e["cat"] for e in lines], ["discover", "kill", "defeat"])
            # Rotation keeps only the newest lines once the file is oversized.
            append_jsonl(path, [{"msg": f"row{i}"} for i in range(10)], max_bytes=1, keep_lines=3)
            tail = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
            self.assertEqual(len(tail), 3)
            self.assertEqual(tail[-1]["msg"], "row9")

    def test_classify_battle_event_categories(self) -> None:
        turn = SimpleNamespace(
            units=(
                self._unit("u1", UnitType.WORKER, (0, 0)),
                self._unit("u2", UnitType.VANGUARD, (1, 0)),
                self._unit("u3", UnitType.RANGER, (2, 0)),
            ),
            visible_enemies=(self._unit("e1", UnitType.WORKER, (9, 9)),),
            core=None,
        )
        cases = [
            ("SHOT_HIT", None, "u3", "e1", {"damage": 1}, "combat", "R1"),
            ("SWEEP_RESOLVED", None, "u2", None, {"targets_hit": 2}, "combat", "V1"),
            ("DESTRUCTION_PARTICIPATION", "UNIT", "u3", "e1", None, "kill", "E1"),
            ("UNIT_DAMAGED", "ATTACK", None, "u1", {"damage": 2, "hp": 0}, "defeat", "W1"),
            ("UNIT_SELF_DESTRUCTED", None, "u1", None, None, "defeat", "W1"),
            ("CORE_DESTROYED", "ATTACK", None, None, None, "defeat", "核心"),
            ("HARVEST_SUCCEEDED", None, "u1", None, {"amount": 2}, "economy", "挖矿"),
            ("DEPOSIT_SUCCEEDED", None, "u1", None, {"amount": 2}, "economy", "卸货"),
            ("SHOT_MISSED", "SHOT_MISSED", "u3", None, None, "warn", "未命中"),
            ("HARVEST_FAILED", "CARGO_FULL", "u1", None, None, "warn", "货舱满"),
            ("CORE_SPAWN_FAILED", "INSUFFICIENT_RESOURCES", None, None, None, "warn", "资源不足"),
        ]
        for et, reason, actor, target, values, cat, needle in cases:
            event = self._event(et, reason, actor, target, values)
            got_cat, got_msg = tactic._classify_battle_event(turn, event)
            self.assertEqual(got_cat, cat, et)
            self.assertIn(needle, got_msg, et)

    def test_battle_log_entries_include_discoveries_and_events(self) -> None:
        turn = SimpleNamespace(
            units=(self._unit("u1", UnitType.WORKER, (0, 0)),),
            visible_enemies=(),
            core=None,
            events=(self._event("HARVEST_SUCCEEDED", actor="u1", values={"amount": 2}),),
        )
        entries = tactic._battle_log_entries(
            turn,
            new_resources={(3, 4)},
            new_enemy_sightings={(7, 8)},
        )
        cats = [e["cat"] for e in entries]
        self.assertIn("discover", cats)
        self.assertIn("economy", cats)
        self.assertTrue(all(e["tick"] == 42 for e in entries))
        self.assertTrue(all(isinstance(e.get("ts"), (int, float)) for e in entries))
        self.assertTrue(any("(3,4)" in e["msg"] for e in entries))
        self.assertTrue(any("(7,8)" in e["msg"] for e in entries))

    def test_event_messages_carry_coordinates(self) -> None:
        """事件行附带坐标：双方箭头优先，其次事件结算格，最后单方格子。"""
        turn = SimpleNamespace(
            units=(self._unit("u1", UnitType.WORKER, (3, 4)),),
            visible_enemies=(self._unit("e1", UnitType.RANGER, (7, 8)),),
            core=None,
        )
        # 双方位置都已知且不同 → (ax,ay)→(tx,ty)
        _cat, msg = tactic._classify_battle_event(
            turn, self._event("SHOT_HIT", actor="u1", target="e1", values={"damage": 1}),
        )
        self.assertIn("(3,4)→(7,8)", msg)
        # 事件自带结算格优先于 actor 当前格
        _cat, msg = tactic._classify_battle_event(
            turn,
            self._event("HARVEST_SUCCEEDED", actor="u1", values={"amount": 2}, pos=(5, 6)),
        )
        self.assertIn("(5,6)", msg)
        self.assertNotIn("(3,4)", msg)
        # 双方同格时退化为单个坐标
        same = SimpleNamespace(
            units=(self._unit("u1", UnitType.WORKER, (9, 9)),),
            visible_enemies=(self._unit("e1", UnitType.RANGER, (9, 9)),),
            core=None,
        )
        _cat, msg = tactic._classify_battle_event(
            same, self._event("SHOT_HIT", actor="u1", target="e1", values={"damage": 1}),
        )
        self.assertIn("(9,9)", msg)
        self.assertNotIn("→", msg)

    def test_read_battle_log_returns_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "battle_log.jsonl"
            path.write_text(
                json.dumps({"msg": "first"}) + "\n" + json.dumps({"msg": "second"}) + "\n",
                encoding="utf-8",
            )
            with patch.object(dashboard, "BATTLE_LOG_FILE", str(path)):
                entries = dashboard.read_battle_log(10)
        self.assertEqual([e["msg"] for e in entries], ["second", "first"])

    def test_config_log_message_uses_field_labels(self) -> None:
        msg = dashboard._config_log_message({"target_workers": 8, "ranger_attack_range": 2})
        self.assertIn("工人目标=8", msg)
        self.assertIn("游侠开火距离=2", msg)

    def test_log_panel_html_renders_rows_with_categories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "battle_log.jsonl"
            path.write_text(
                json.dumps({"tick": 1, "cat": "discover", "msg": "发现矿点"}) + "\n"
                + json.dumps({"tick": 2, "cat": "kill", "msg": "参与摧毁"}) + "\n",
                encoding="utf-8",
            )
            with patch.object(dashboard, "BATTLE_LOG_FILE", str(path)):
                html, count = dashboard._battle_log_html()
        self.assertEqual(count, 2)
        self.assertIn('data-cat="discover"', html)
        self.assertIn('data-cat="kill"', html)
        self.assertIn("发现矿点", html)
        self.assertIn("tick 1", html)

    def test_log_panel_coords_are_clickable_map_jumps(self) -> None:
        """日志消息里的 (x,y) 坐标渲染成带 data-focus 属性的可点击 span，
        前端点击后调用 focusWorld 把地图视角重置到战斗发生的位置。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "battle_log.jsonl"
            path.write_text(
                json.dumps({"tick": 1, "cat": "kill", "msg": "W1 参与摧毁 E3 (12,-34)"}) + "\n"
                + json.dumps({"tick": 2, "cat": "combat", "msg": "W1 击中 E3 (5,6)→(9,10)"}) + "\n"
                + json.dumps({"tick": 3, "cat": "discover", "msg": "发现新矿点 (3,4)"}) + "\n"
                + json.dumps({"tick": 4, "cat": "config", "msg": "配置调整"}) + "\n",
                encoding="utf-8",
            )
            with patch.object(dashboard, "BATTLE_LOG_FILE", str(path)):
                html, count = dashboard._battle_log_html()
        self.assertEqual(count, 4)
        # 每个坐标都被包成可点击 span 且携带跳转属性（负数坐标也支持）
        self.assertIn('class="log-coord" data-focus-wx="12" data-focus-wy="-34"', html)
        # 箭头形式的战斗事件两个坐标都能跳转
        self.assertIn('data-focus-wx="5" data-focus-wy="6"', html)
        self.assertIn('data-focus-wx="9" data-focus-wy="10"', html)
        self.assertIn('data-focus-wx="3" data-focus-wy="4"', html)
        # 无坐标的消息不生成跳转属性
        self.assertNotIn('data-focus-wx="0"', html)

    def test_log_panel_html_shows_time_beside_tick(self) -> None:
        """Rows carrying both tick and ts render the wall-clock time next to
        the tick label (config rows already render ts-only as a time)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "battle_log.jsonl"
            path.write_text(
                json.dumps({"tick": 5, "ts": 1234567890.0, "cat": "economy", "msg": "挖矿"})
                + "\n" + json.dumps({"ts": 1234567890.0, "cat": "config", "msg": "配置调整"}) + "\n",
                encoding="utf-8",
            )
            with patch.object(dashboard, "BATTLE_LOG_FILE", str(path)):
                html, count = dashboard._battle_log_html()
        self.assertEqual(count, 2)
        self.assertIn("tick 5", html)
        # The wall-clock time must be shown next to the tick, not dropped.
        self.assertIn(" · ", html)
        expected_time = dashboard.time.strftime(
            "%H:%M:%S", dashboard.time.localtime(1234567890.0)
        )
        self.assertIn(expected_time, html)

    def test_log_panel_is_present_below_config(self) -> None:
        page = dashboard.generate_html()
        self.assertIn('id="logPanel"', page)
        self.assertIn('id="logSection"', page)
        self.assertIn('id="logCount"', page)
        for cat in ("discover", "kill", "defeat", "combat", "economy", "config", "warn"):
            self.assertIn(f'data-log-cat="{cat}"', page)
        self.assertGreater(page.index('id="logPanel"'), page.index('id="tacticConfigForm"'))

    def test_log_panel_has_time_window_buttons(self) -> None:
        """The battle-log panel offers time-window filters (presets + custom)
        that replace the fixed newest-200 view."""
        page = dashboard.generate_html()
        for seconds in ("600", "1800", "3600", "21600"):
            self.assertIn(f'data-log-window="{seconds}"', page)
        self.assertIn('data-log-window="all"', page)
        self.assertIn('id="logWindowMinutes"', page)
        self.assertIn('id="logWindowCustomApply"', page)

    def test_battle_log_rows_carry_ts_for_time_filtering(self) -> None:
        """Each rendered row carries its wall-clock ts so the client can hide
        rows older than the selected time window."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "battle_log.jsonl"
            path.write_text(
                json.dumps({"tick": 7, "ts": 1234567890.0, "cat": "warn", "msg": "告警"}) + "\n",
                encoding="utf-8",
            )
            with patch.object(dashboard, "BATTLE_LOG_FILE", str(path)):
                html, _count = dashboard._battle_log_html()
        self.assertIn('data-ts="1234567890.0"', html)
        # Time is shown first, tick second.
        self.assertLess(html.find(":"), html.index("tick 7"))

    def test_battle_log_limit_controls_rows_rendered(self) -> None:
        """The server sends only ``limit`` newest rows; larger windows ask for
        more so "全部" can cover more history than a fixed newest-200."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "battle_log.jsonl"
            path.write_text(
                "".join(json.dumps({"tick": i, "cat": "warn", "msg": f"行{i}"}) + "\n"
                        for i in range(10)),
                encoding="utf-8",
            )
            with patch.object(dashboard, "BATTLE_LOG_FILE", str(path)):
                html, count = dashboard._battle_log_html(limit=4)
        self.assertEqual(count, 4)
        # Newest four rows: ticks 9,8,7,6.
        self.assertIn("tick 9", html)
        self.assertIn("tick 6", html)
        self.assertNotIn("tick 5", html)

    def test_clamp_log_limit_bounds_query(self) -> None:
        import types
        req = types.SimpleNamespace(path="/api/state?log=999999")
        self.assertEqual(dashboard._clamp_log_limit(req), 8000)
        req2 = types.SimpleNamespace(path="/api/log?limit=50")
        self.assertEqual(dashboard._clamp_log_limit(req2), 200)
        req3 = types.SimpleNamespace(path="/api/state?log=abc")
        self.assertEqual(dashboard._clamp_log_limit(req3), 200)
        req4 = types.SimpleNamespace(path="/api/state")
        self.assertEqual(dashboard._clamp_log_limit(req4), 200)


class DashboardVisualSystemTests(unittest.TestCase):
    def test_major_panels_share_visual_system_tokens(self) -> None:
        for token in ("--surface-soft", "--control-bg", "--radius-panel", "--radius-control"):
            self.assertIn(token, dashboard.CSS)
        for selector in (
            ".map-panel",
            ".teams-panel",
            ".config-panel",
            ".trends-panel",
            ".log-panel",
            ".units-panel",
            ".waypoint-panel",
            ".res-panel",
        ):
            self.assertIn(selector, dashboard.CSS)

    def test_long_resource_list_has_bounded_desktop_region(self) -> None:
        self.assertIn(".res-panel #resSection{max-height:430px;overflow:auto", dashboard.CSS)


class LeftSidebarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = dashboard.generate_html()

    def test_left_sidebar_uses_layered_summary_panels(self) -> None:
        for panel in (
            "core-summary",
            "resource-summary",
            "battle-summary",
            "issue-summary",
            "report-panel",
            "enemy-panel",
        ):
            self.assertIn(panel, self.page)
        self.assertIn('class="rail-focus"', self.page)
        self.assertIn('class="rail-metric-grid"', self.page)
        self.assertIn('class="rail-activity"', self.page)

    def test_core_card_has_segmented_hp_and_shield_bars(self) -> None:
        """The left core card shows HP and shield as one cell per point."""
        self.assertIn('class="rail-vitals"', self.page)
        self.assertIn('class="vital-label hp">HP', self.page)
        self.assertIn('class="vital-label sh">盾', self.page)

    def test_seg_cells_one_cell_per_point_with_filled_prefix(self) -> None:
        self.assertEqual(
            dashboard._seg_cells(3, 5),
            '<i class="on"></i><i class="on"></i><i class="on"></i><i></i><i></i>',
        )
        # Unknown / out-of-range values never overflow the bar.
        self.assertEqual(dashboard._seg_cells(None, 5), "<i></i>" * 5)
        self.assertEqual(dashboard._seg_cells(99, 5), '<i class="on"></i>' * 5)
        self.assertEqual(dashboard._seg_cells(2, 0), "")

    def test_core_rail_shield_cap_grows_when_beacon_is_on_core(self) -> None:
        """No beacon → shield bar holds 5 cells; beacon on the Core → 10."""
        base = {"core_pos": [3, 4], "core_name": "C1", "core_hp": 4,
                "core_shield": 3, "population": 7, "core_action": "HEAL_CORE"}
        html = dashboard._core_rail_html(base)
        self.assertIn("4/5", html)
        self.assertIn("3/5", html)
        self.assertIn('class="vital-cells hp">' + '<i class="on"></i>' * 4 + "<i></i>", html)

        carried = dict(base, beacon_pos=[3, 4])
        self.assertIn("3/10", dashboard._core_rail_html(carried))

        away = dict(base, beacon_pos=[0, 0])
        self.assertIn("3/5", dashboard._core_rail_html(away))


class TrendPanelTests(unittest.TestCase):
    def test_chart_series_names_match_trend_point_keys(self) -> None:
        for key in ("r", "c", "w", "v", "g", "e"):
            self.assertIn(f"{key}:     {{ key: '{key}'", dashboard.JS)

    def test_window_buttons_use_time_seconds_not_ticks(self) -> None:
        page = dashboard.generate_html()
        for seconds, label in (("600", "10分钟"), ("1800", "30分钟"), ("3600", "1小时")):
            self.assertIn(f'data-trend-window="{seconds}">{label}', page)
        self.assertIn("最近", page)
        self.assertNotIn('data-trend-window="400"', page)


class TrendTimeWindowTests(unittest.TestCase):
    """The trend series is laid out by wall-clock time, not tick index."""

    def test_parse_iso_ts_handles_utc_and_z(self) -> None:
        dt = dashboard.datetime(2026, 8, 12, 3, 14, 30, 123456, tzinfo=dashboard.timezone.utc)
        self.assertAlmostEqual(
            dashboard._parse_iso_ts("2026-08-12T03:14:30.123456+00:00"),
            dt.timestamp(),
            places=2,
        )
        self.assertAlmostEqual(
            dashboard._parse_iso_ts("2026-08-12T03:14:30Z"),
            dashboard.datetime(2026, 8, 12, 3, 14, 30, tzinfo=dashboard.timezone.utc).timestamp(),
            places=2,
        )
        self.assertIsNone(dashboard._parse_iso_ts("not-a-date"))
        self.assertIsNone(dashboard._parse_iso_ts(""))
        self.assertIsNone(dashboard._parse_iso_ts(None))

    def test_trend_points_use_epoch_time_and_drop_untimestamped(self) -> None:
        base = 1760000000.0
        records = [
            {"tick": 3, "plan_unit_actions": {}, "_ts": base + 40,
             "resources": 8, "resource_capacity": 50, "workers": [], "vanguards": [], "rangers": [], "visible_enemies": 0},
            {"tick": 2, "plan_unit_actions": {}, "_ts": None,
             "resources": 9, "resource_capacity": 50, "workers": [{"id": "w"}], "vanguards": [], "rangers": [], "visible_enemies": 2},
            {"tick": 1, "plan_unit_actions": {}, "_ts": base + 10,
             "resources": 5, "resource_capacity": 50, "workers": [{"id": "a"}, {"id": "b"}], "vanguards": [], "rangers": [{"id": "r"}], "visible_enemies": 1},
        ]
        points = dashboard._trend_points(records)
        # Chronological order; the untimestamped middle record is dropped.
        self.assertEqual([p["t"] for p in points], [base + 10, base + 40])
        self.assertEqual(points[0]["w"], 2)
        self.assertEqual(points[1]["e"], 0)

    def test_read_tick_records_since_stops_at_oldest_in_window(self) -> None:
        now = dashboard.time.time()
        # The tactic log appends chronologically (oldest tick first); reverse
        # iteration yields newest-first, so the reader must stop at the first
        # record older than the window cutoff.
        lines = [
            json.dumps({"tick": 1, "plan_unit_actions": {},
                        "timestamp": dashboard.datetime.fromtimestamp(now - 600, dashboard.timezone.utc).isoformat()}),
            json.dumps({"tick": 2, "plan_unit_actions": {},
                        "timestamp": dashboard.datetime.fromtimestamp(now - 40, dashboard.timezone.utc).isoformat()}),
            json.dumps({"tick": 3, "plan_unit_actions": {},
                        "timestamp": dashboard.datetime.fromtimestamp(now - 5, dashboard.timezone.utc).isoformat()}),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tactic_log.jsonl"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with patch.object(dashboard, "LOG_FILE", str(path)):
                records = dashboard._read_tick_records_since(now - 60)
        self.assertEqual([r["tick"] for r in records], [3, 2])
        self.assertTrue(all(r["_ts"] is not None for r in records))


class UnitTabsTests(unittest.TestCase):
    """Right sidebar unit cards are one panel with three tabs."""

    def setUp(self) -> None:
        self.page = dashboard.generate_html()

    def test_three_unit_tabs_in_one_panel(self) -> None:
        for tab in ("workers", "vanguards", "rangers"):
            self.assertIn(f'data-unit-tab="{tab}"', self.page)
        for pane in ("workers", "vanguards", "rangers"):
            self.assertIn(f'data-unit-pane="{pane}"', self.page)
        # One tab container, exactly three tab buttons + three panes.
        self.assertEqual(self.page.count("class=\"units-tabs\""), 1)
        self.assertEqual(self.page.count("data-unit-tab="), 3)
        self.assertEqual(self.page.count("data-unit-pane="), 3)
        # The three grids are the tab panes' contents.
        self.assertEqual(self.page.count("data-unit-pane="), 3)

    def test_unit_grids_and_counts_still_present(self) -> None:
        for grid in ("workersGrid", "vgGrid", "rgGrid"):
            self.assertIn(f'id="{grid}"', self.page)
        for count in ("workersCount", "vgCount", "rgCount"):
            self.assertIn(f'id="{count}"', self.page)

    def test_separate_unit_panels_removed(self) -> None:
        # The old per-type <section class="panel"> cards are gone; the tabs are
        # inside a single units-panel section.
        self.assertNotIn('<section class="panel">\n        <div class="panel-title"><span>工人</span>', self.page)
        self.assertIn('class="panel units-panel"', self.page)

    def test_unit_cards_use_compact_two_row_layout(self) -> None:
        self.assertIn('class="unit-facts"', self.page)
        self.assertIn('class="unit-locator"', self.page)
        self.assertIn('class="unit-fact"', self.page)
        self.assertNotIn('class="unit-coords"', self.page)
        self.assertNotIn('class="unit-meta"', self.page)
        self.assertNotIn('class="unit-action"', self.page)


class TickMismatchSelfHealTests(unittest.TestCase):
    """The bot must self-heal from 409 TICK_MISMATCH instead of stalling forever.

    Regression for production incidents: after a WebSocket keepalive timeout the
    SDK reconnects in place but the server keeps rejecting every submit with 409
    TICK_MISMATCH (1200+ rejections over hours). In-place reconnects and
    tactic-only restarts do NOT recover — only a fresh container (all
    connections closed) resyncs the server baseline. So a sustained mismatch
    run must make the tactic exit with STALE_SESSION_EXIT (3) so the entrypoint
    restarts the container.
    """

    class _FakeLogger:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def open(self) -> None:
            pass

        def close(self) -> None:
            pass

        def record_tick(self, *args, **kwargs) -> None:
            pass

    class _FakeClient:
        def __init__(self, turns: list) -> None:
            self._turns = turns

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def turns(self):
            return iter(self._turns)

    @staticmethod
    def _fake_turn(tick: int, result):
        turn = SimpleNamespace(
            tick=tick,
            resources=0,
            resource_capacity=10,
            state=SimpleNamespace(population=1),
            workers=(),
            visible_enemies=(),
            resource_cells=frozenset(),
        )

        def submit():
            if result is None:
                return SimpleNamespace(tick=tick, accepted={})
            raise result

        turn.submit = submit
        return turn

    @staticmethod
    def _mismatch() -> tactic.APIError:
        return tactic.APIError(status_code=409, error="TICK_MISMATCH")

    def _run_play(self, results) -> int | None:
        """Run play() over mocked turns; return the SystemExit code or None."""
        turns = [
            self._fake_turn(100 + i, r) for i, r in enumerate(results)
        ]
        client = self._FakeClient(turns)
        with contextlib.redirect_stdout(io.StringIO()):
            with (
                patch.object(tactic, "ArenaHeroClient", return_value=client),
                patch.object(tactic, "TacticLogger", self._FakeLogger),
                patch.object(tactic, "_load_map_memory", lambda: None),
                patch.object(tactic, "_save_map_memory", lambda force=False: None),
                patch.object(tactic, "_commit_shadow_predictions", lambda *a, **k: None),
                patch.object(tactic, "_print_summary", lambda *a, **k: None),
                patch.object(tactic, "choose_actions", lambda turn: ("WAIT", {})),
                # Ends the loop when a healthy session winds down normally.
                patch.object(tactic.time, "sleep", side_effect=SystemExit(9)),
            ):
                try:
                    tactic.play("test-key")
                except SystemExit as exc:
                    return exc.code
        return None

    def test_benign_single_mismatch_does_not_exit(self) -> None:
        # One mismatch after a fresh connection is normal and self-heals.
        code = self._run_play([
            self._mismatch(),
            None,
            None,
        ])
        self.assertEqual(code, 9)  # loop ended via the sleep sentinel, not exit 3

    def test_warmup_few_mismatches_then_success_does_not_exit(self) -> None:
        code = self._run_play([
            self._mismatch(),
            self._mismatch(),
            None,
            None,
        ])
        self.assertEqual(code, 9)

    def test_sustained_mismatch_run_exits_for_container_restart(self) -> None:
        # A run well past the fresh-connection warm-up means the session is
        # permanently desynced: exit with code 3 so the entrypoint restarts the
        # container (the only proven recovery).
        code = self._run_play([self._mismatch() for _ in range(6)])
        self.assertEqual(code, 3)

    def test_success_resets_the_streak(self) -> None:
        # 4 mismatches, then a success, then more mismatches: the success resets
        # the streak, so the run restarts counting and eventually exits — proving
        # the counter is not a global wall-clock but a consecutive run.
        results = [self._mismatch() for _ in range(4)]
        results.append(None)
        results.extend(self._mismatch() for _ in range(6))
        code = self._run_play(results)
        self.assertEqual(code, 3)


class EntrypointRestartLogTests(unittest.TestCase):
    """Container restarts must leave a visible trace in the logs.

    The dashboard's 「战斗日志」panel reads battle_log.jsonl (warn category shown
    by default) and tactic_play.log is the raw play log; the entrypoint writes a
    lifecycle marker to both so any boot / self-heal restart / crash-restart is
    visible instead of the log just resuming silently.
    """

    @staticmethod
    def _load_entrypoint(runtime_dir: str):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "docker_entrypoint", str(Path(__file__).parent.parent / "docker-entrypoint.py")
        )
        mod = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"ARENA_DATA_DIR": runtime_dir}):
            spec.loader.exec_module(mod)
        return mod

    def test_marker_writes_to_play_log_and_battle_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            de = self._load_entrypoint(temp_dir)
            try:
                de._marker("容器启动 (pid=12345)")
                de._marker("战术会话失同步（连续 TICK_MISMATCH），容器将在 60 秒后自动重启")

                play = (Path(temp_dir) / "tactic_play.log").read_text(encoding="utf-8")
                self.assertIn("[entrypoint]", play)
                self.assertIn("容器启动 (pid=12345)", play)
                self.assertIn("战术会话失同步", play)

                battle = [
                    json.loads(line)
                    for line in (Path(temp_dir) / "battle_log.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertEqual(len(battle), 2)
                for rec in battle:
                    self.assertEqual(rec["cat"], "warn")
                    self.assertIsNone(rec["tick"])
                    self.assertIsInstance(rec["ts"], (int, float))
                self.assertEqual(battle[0]["msg"], "容器启动 (pid=12345)")
                self.assertIn("战术会话失同步", battle[1]["msg"])
            finally:
                de.tactic_log.close()


class CombatHotspotTests(unittest.TestCase):
    """战斗热点：把"哪里在打仗"聚合成可点击跳转的告警条与地图光圈。"""

    def _write_battle_log(self, path: Path, rows: list[dict]) -> None:
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

    def test_three_levels_sorted_by_severity(self) -> None:
        now = time.time()
        rec = {
            "core_pos": [0, 0],
            "workers": [{"name": "W1", "pos": [10, 10]}],
            "vanguards": [], "rangers": [],
            "enemies": [
                # 贴着 W1 → engaged；远离所有我方 → sighted
                {"name": "E1", "type": "WORKER", "pos": [11, 10]},
                {"name": "E2", "type": "RANGER", "pos": [40, 40]},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            log = Path(temp_dir) / "battle_log.jsonl"
            # 箭头坐标取最后一个（结算目标格）；economy 行不算交火
            self._write_battle_log(log, [
                {"tick": 1, "ts": now - 30, "cat": "economy",
                 "msg": "W1 挖矿 +5 (50,50)"},
                {"tick": 2, "ts": now - 20, "cat": "combat",
                 "msg": "W1 击中 E3 造成 3 伤害 (5,5)→(6,5)"},
            ])
            with patch.object(dashboard, "BATTLE_LOG_FILE", str(log)):
                spots = dashboard._combat_hotspots(rec)

        self.assertEqual(
            [(s["x"], s["y"], s["level"]) for s in spots],
            [(11, 10, "engaged"), (6, 5, "recent"), (40, 40, "sighted")],
        )
        self.assertEqual(spots[0]["enemies"], 1)
        self.assertEqual(spots[0]["friendlies"], 1)
        self.assertEqual(spots[1]["events"], 1)

    def test_window_cutoff_and_same_cell_merge(self) -> None:
        now = time.time()
        rec = {
            "core_pos": [0, 0],
            "workers": [{"name": "W1", "pos": [10, 10]}],
            "vanguards": [], "rangers": [],
            "enemies": [{"name": "E1", "type": "WORKER", "pos": [11, 10]}],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            log = Path(temp_dir) / "battle_log.jsonl"
            self._write_battle_log(log, [
                # 窗口外：不该出现
                {"tick": 1, "ts": now - 3600, "cat": "combat",
                 "msg": "旧战斗 (90,90)"},
                # 与 engaged 同格：并入且保持 engaged 级别
                {"tick": 2, "ts": now - 5, "cat": "combat",
                 "msg": "W1 击中 E1 (10,10)→(11,10)"},
                {"tick": 3, "ts": now - 4, "cat": "kill",
                 "msg": "W1 参与摧毁 E1 (11,10)"},
            ])
            with patch.object(dashboard, "BATTLE_LOG_FILE", str(log)):
                spots = dashboard._combat_hotspots(rec)

        self.assertEqual(len(spots), 1)
        spot = spots[0]
        self.assertEqual((spot["x"], spot["y"], spot["level"]), (11, 10, "engaged"))
        self.assertEqual(spot["events"], 2)

    def test_hotspot_chips_are_clickable_and_empty_state(self) -> None:
        html = dashboard._hotspots_html([
            {"x": 11, "y": 10, "level": "engaged", "enemies": 2, "friendlies": 3,
             "events": 0, "last_ts": None, "age_s": None},
            {"x": 6, "y": 5, "level": "recent", "enemies": 0, "friendlies": 0,
             "events": 1, "last_ts": time.time() - 30, "age_s": 30},
        ])
        self.assertIn('class="hotspot engaged"', html)
        self.assertIn('data-focus-wx="11" data-focus-wy="10"', html)
        self.assertIn("敌2", html)
        self.assertIn('data-focus-wx="6" data-focus-wy="5"', html)
        self.assertIn("30秒前交火", html)
        self.assertIn("当前无战斗", dashboard._hotspots_html([]))

    def test_svg_draws_combat_rings_only_for_engaged_and_recent(self) -> None:
        rec = {"core_pos": [0, 0], "workers": [], "vanguards": [], "rangers": [],
               "enemies": [], "resource_cells": []}
        memory = {"obstacles": [], "resources": []}
        spots = [
            {"x": 3, "y": 3, "level": "engaged"},
            {"x": 5, "y": 5, "level": "recent"},
            {"x": 7, "y": 7, "level": "sighted"},
        ]
        svg = dashboard.render_svg(rec, memory, hotspots=spots)
        # engaged 双环（2 个）+ recent 虚线环（1 个）；sighted 不画
        self.assertEqual(svg.count('data-cat="combat"'), 3)
        self.assertIn('class="hotspot-ring engaged"', svg)



class ArenaConsoleRoutingTests(unittest.TestCase):
    """HTTP-level coverage for the /arena console integration.

    The old dashboard page moved to /dashboard, / redirects there, and the
    React build is served from web/dist with the game API proxied behind the
    dashboard token (the proxy injects the server-side API key).
    """

    @classmethod
    def setUpClass(cls) -> None:
        from http.server import ThreadingHTTPServer

        cls._server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
        cls._server.daemon_threads = True
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()
        cls._port = cls._server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server.shutdown()
        cls._server.server_close()

    def _request(self, method: str, path: str, body: bytes | None = None,
                 headers: dict | None = None):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, dict(resp.getheaders()), data
        finally:
            conn.close()

    def test_root_redirects_to_dashboard(self) -> None:
        status, headers, _ = self._request("GET", "/")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/dashboard")

    def test_dashboard_path_serves_legacy_page(self) -> None:
        status, headers, body = self._request("GET", "/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("<title>Arena Hero 战术仪表盘</title>", body.decode("utf-8"))

    def test_arena_spa_serves_dist_with_route_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp)
            (dist / "index.html").write_text("<html>arena spa</html>", encoding="utf-8")
            assets = dist / "assets"
            assets.mkdir()
            (assets / "app.js").write_text("console.log(1)", encoding="utf-8")
            with patch.object(dashboard, "WEB_DIST_DIR", dist):
                # /arena redirects so relative asset URLs resolve.
                status, headers, _ = self._request("GET", "/arena")
                self.assertEqual(status, 302)
                self.assertEqual(headers.get("Location"), "/arena/")
                # Built entry point.
                status, headers, body = self._request("GET", "/arena/")
                self.assertEqual(status, 200)
                self.assertEqual(body, b"<html>arena spa</html>")
                # Static asset with its MIME type.
                status, headers, body = self._request("GET", "/arena/assets/app.js")
                self.assertEqual(status, 200)
                self.assertIn("javascript", headers.get("Content-Type", ""))
                self.assertEqual(body, b"console.log(1)")
                # Client-side routes fall back to index.html.
                status, _, body = self._request("GET", "/arena/leaderboard")
                self.assertEqual(status, 200)
                self.assertEqual(body, b"<html>arena spa</html>")
                # Traversal attempts never escape the dist root.
                status, _, body = self._request("GET", "/arena/../dashboard.py")
                self.assertEqual(status, 200)
                self.assertEqual(body, b"<html>arena spa</html>")

    def test_arena_without_build_reports_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(dashboard, "WEB_DIST_DIR", Path(tmp) / "missing"):
                status, _, body = self._request("GET", "/arena/")
        self.assertEqual(status, 404)
        self.assertIn("npm run build", body.decode("utf-8"))

    def test_unauthenticated_arena_pages_show_login_html(self) -> None:
        with patch.object(dashboard, "DASHBOARD_TOKEN", "secret-token"), \
                patch.object(dashboard.Handler, "_is_loopback", return_value=False):
            status, headers, body = self._request("GET", "/arena/")
            self.assertEqual(status, 401)
            self.assertIn("text/html", headers.get("Content-Type", ""))
            self.assertIn("登录", body.decode("utf-8"))
            status, headers, body = self._request("GET", "/api/v1/leaderboard")
            self.assertEqual(status, 401)
            self.assertIn("application/json", headers.get("Content-Type", ""))

    def test_get_proxy_forwards_whitelisted_paths_only(self) -> None:
        calls = []

        def fake_upstream(self_handler, method, path, body=None, extra_headers=None):
            calls.append((method, path))
            return 200, [("Content-Type", "application/json")], b'{"ranked": true}'

        with patch.object(dashboard.Handler, "_arena_upstream", fake_upstream):
            status, _, body = self._request("GET", "/api/v1/leaderboard")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"ranked": True})
            self.assertEqual(calls, [("GET", "/api/v1/leaderboard")])
            # Non-whitelisted upstream paths stay local 404s.
            status, _, _ = self._request("GET", "/api/v1/auth/login")
            self.assertEqual(status, 404)

    def test_post_commands_proxy_keeps_body_and_idempotency_key(self) -> None:
        captured = {}

        def fake_upstream(self_handler, method, path, body=None, extra_headers=None):
            captured["method"] = method
            captured["path"] = path
            captured["body"] = body
            captured["extra"] = extra_headers
            return 202, [("Content-Type", "application/json")], b'{"accepted": true}'

        plan = json.dumps({"tick": 7, "unit_actions": {}}).encode("utf-8")
        with patch.object(dashboard.Handler, "_arena_upstream", fake_upstream):
            status, _, body = self._request(
                "POST", "/api/v1/game/commands", body=plan,
                headers={"Content-Type": "application/json", "Idempotency-Key": "manual-7-plan-01"},
            )
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body), {"accepted": True})
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/api/v1/game/commands")
        self.assertEqual(captured["body"], plan)
        self.assertEqual(captured["extra"].get("Idempotency-Key"), "manual-7-plan-01")

    def test_upstream_request_injects_bearer_key(self) -> None:
        import urllib.request

        seen = {}

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            seen["method"] = request.get_method()
            return FakeResponse()

        with patch.object(dashboard, "ARENA_API_KEY", "ah_live_test"), \
                patch.object(dashboard, "ARENA_API_HOST", "api.example.test"), \
                patch.object(urllib.request, "urlopen", fake_urlopen):
            handler = dashboard.Handler.__new__(dashboard.Handler)
            status, _, body = handler._arena_upstream("GET", "/api/v1/me")
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"ok": true}')
        self.assertEqual(seen["url"], "https://api.example.test/api/v1/me")
        self.assertEqual(seen["auth"], "Bearer ah_live_test")

    def test_upstream_error_statuses_pass_through(self) -> None:
        import io as _io
        import urllib.error
        import urllib.request

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 429, "rate limited",
                {"Content-Type": "application/json"},
                _io.BytesIO(b'{"error": "REALTIME_CONNECTION_LIMIT"}'),
            )

        with patch.object(dashboard, "ARENA_API_KEY", "ah_live_test"), \
                patch.object(urllib.request, "urlopen", fake_urlopen):
            handler = dashboard.Handler.__new__(dashboard.Handler)
            status, _, body = handler._arena_upstream("GET", "/api/v1/me")
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["error"], "REALTIME_CONNECTION_LIMIT")

    def test_upstream_without_key_reports_503(self) -> None:
        with patch.object(dashboard, "ARENA_API_KEY", ""):
            handler = dashboard.Handler.__new__(dashboard.Handler)
            status, _, body = handler._arena_upstream("GET", "/api/v1/me")
        self.assertEqual(status, 503)
        self.assertIn("ARENA_HERO_API_KEY", body.decode("utf-8"))

    def test_ws_endpoint_rejects_plain_get_and_missing_key(self) -> None:
        # Without an Upgrade header the WS path is not a normal GET route.
        status, _, _ = self._request("GET", "/api/v1/game/ws")
        self.assertEqual(status, 404)
        # Upgrade without Sec-WebSocket-Key is a bad handshake.
        status, _, _ = self._request("GET", "/api/v1/game/ws",
                                     headers={"Upgrade": "websocket", "Connection": "Upgrade"})
        self.assertEqual(status, 400)
        # Valid handshake shape but no server-side key configured.
        with patch.object(dashboard, "ARENA_API_KEY", ""):
            status, _, body = self._request(
                "GET", "/api/v1/game/ws",
                headers={"Upgrade": "websocket", "Connection": "Upgrade",
                         "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                         "Sec-WebSocket-Version": "13"},
            )
        self.assertEqual(status, 503)
        self.assertIn("ARENA_HERO_API_KEY", body.decode("utf-8"))

    def test_session_cookie_takes_precedence_over_api_key(self) -> None:
        import urllib.request

        seen = {}

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            seen["cookie"] = request.get_header("Cookie")
            seen["auth"] = request.get_header("Authorization")
            return FakeResponse()

        with patch.object(dashboard, "ARENA_API_KEY", "ah_live_test"), \
                patch.object(urllib.request, "urlopen", fake_urlopen):
            status, _, _ = self._request(
                "GET", "/api/v1/leaderboard",
                headers={"Cookie": "ah_session=abc123; arena_token=whatever; arena_csrf=c9"},
            )
        self.assertEqual(status, 200)
        # Session credential wins: cookie forwarded, API key NOT injected, and
        # the dashboard's own token / CSRF carrier never leak upstream.
        self.assertEqual(seen["cookie"], "ah_session=abc123")
        self.assertIsNone(seen["auth"])

    def test_login_set_cookie_is_rewritten_to_dashboard_origin(self) -> None:
        import urllib.request

        class FakeResponse:
            status = 201
            headers = {
                "Content-Type": "application/json",
                "Set-Cookie": "ah_session=abc123; Domain=api.arenahero.io; Path=/; Secure; HttpOnly",
            }

            def read(self):
                return b'{"csrf_token": "csrf-x"}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            return FakeResponse()

        with patch.object(dashboard, "ARENA_API_KEY", "ah_live_test"), \
                patch.object(urllib.request, "urlopen", fake_urlopen):
            status, headers, body = self._request(
                "POST", "/api/v1/auth/login",
                body=b'{"email": "a@b.c", "password": "pw"}',
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body), {"csrf_token": "csrf-x"})
        cookie = headers.get("Set-Cookie", "")
        # Domain/Secure must go (dashboard origin may be plain HTTP); the
        # cookie must stay HttpOnly and bind to this origin's path.
        self.assertEqual(cookie, "ah_session=abc123; Path=/; HttpOnly; SameSite=Lax")

    def test_csrf_token_is_forwarded_upstream(self) -> None:
        captured = {}

        def fake_upstream(self_handler, method, path, body=None, extra_headers=None):
            captured["extra"] = extra_headers
            return 202, [("Content-Type", "application/json")], b'{"accepted": true}'

        with patch.object(dashboard.Handler, "_arena_upstream", fake_upstream):
            self._request(
                "POST", "/api/v1/game/commands", body=b'{"tick": 1, "unit_actions": {}}',
                headers={"Content-Type": "application/json",
                         "Idempotency-Key": "manual-1-plan-01", "X-CSRF-Token": "csrf-9"},
            )
        self.assertEqual(captured["extra"].get("X-CSRF-Token"), "csrf-9")

    def test_csrf_carrier_cookie_is_attached_to_session_posts(self) -> None:
        captured = {}

        def fake_upstream(self_handler, method, path, body=None, extra_headers=None):
            captured["extra"] = extra_headers
            return 202, [("Content-Type", "application/json")], b'{"accepted": true}'

        with patch.object(dashboard.Handler, "_arena_upstream", fake_upstream):
            self._request_with_cookies(
                "POST", "/api/v1/game/commands", body=b'{"tick": 2, "unit_actions": {}}',
                cookies="ah_session=x1; arena_csrf=carrier-csrf",
                headers={"Content-Type": "application/json", "Idempotency-Key": "manual-2-plan-01"},
            )
        # No explicit header was sent: the token bound to the session cookie
        # (carrier cookie) must be attached so MANUAL POSTs pass CSRF checks.
        self.assertEqual(captured["extra"].get("X-CSRF-Token"), "carrier-csrf")

    def _request_with_cookies(self, method: str, path: str, body: bytes | None = None,
                              cookies: str = "", headers: dict | None = None):
        import http.client

        merged = dict(headers or {})
        merged["Cookie"] = cookies
        conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=merged)
            resp = conn.getresponse()
            return resp.status, resp.getheaders(), resp.read()
        finally:
            conn.close()

    def _import_request(self, cookies_json: str):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=10)
        try:
            conn.request("POST", "/api/v1/session/import", body=cookies_json.encode("utf-8"),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, resp.getheaders(), resp.read()
        finally:
            conn.close()

    def test_session_import_validates_then_binds_cookies(self) -> None:
        import urllib.request

        seen = {}

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            def read(self):
                return b'{"username": "operator", "email": "", "auth_source": "MANUAL", "oauth_providers": ["linux_do"]}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["cookie"] = request.get_header("Cookie")
            return FakeResponse()

        with patch.object(urllib.request, "urlopen", fake_urlopen):
            status, headers, body = self._import_request(
                '{"cookies": "arena_session=s3cr3t; csrf_hint=x1", "csrf": "csrf-abc"}')
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["user"]["username"], "operator")
        # The CSRF token is echoed back so the frontend can attach it to the
        # session-credential MANUAL command POSTs.
        self.assertEqual(payload["csrf_token"], "csrf-abc")
        self.assertEqual(seen["url"], "https://api.arenahero.io/api/v1/me")
        self.assertEqual(seen["cookie"], "arena_session=s3cr3t; csrf_hint=x1")
        set_cookies = [v for name, v in headers if name.lower() == "set-cookie"]
        self.assertIn("arena_session=s3cr3t; Path=/; HttpOnly; SameSite=Lax", set_cookies)
        self.assertIn("csrf_hint=x1; Path=/; HttpOnly; SameSite=Lax", set_cookies)
        # The CSRF token rides in its own carrier cookie so the proxy can
        # attach it as X-CSRF-Token on session POSTs.
        self.assertIn("arena_csrf=csrf-abc; Path=/; HttpOnly; SameSite=Lax", set_cookies)

    def test_session_import_rejects_malformed_cookies(self) -> None:
        status, _, body = self._import_request('{"cookies": "no-equals-sign"}')
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "SESSION_IMPORT_INVALID")
        status, _, body = self._import_request('{"cookies": "arena_token=keepmeout"}')
        self.assertEqual(status, 400)
        status, _, body = self._import_request('{"cookies": ""}')
        self.assertEqual(status, 400)

    def test_session_import_expired_session_reports_401(self) -> None:
        import io as _io
        import urllib.error
        import urllib.request

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 401, "unauthorized",
                {"Content-Type": "application/json"},
                _io.BytesIO(b'{"error": "UNAUTHORIZED"}'),
            )

        with patch.object(urllib.request, "urlopen", fake_urlopen):
            status, _, body = self._import_request('{"cookies": "arena_session=stale"}')
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "SESSION_IMPORT_EXPIRED")


class WatchdogStallAlertTests(unittest.TestCase):
    """Per-unit stall alerts: raised once a live unit sits within one cell of
    its anchor for >= 50 consecutive ticks, deduped per stall episode, and
    re-armable once the unit moves again.

    Liveness is blacklist-based: everything except DEAD / disappearance from
    the snapshot counts as alive, so a stall in COMBAT / RALLY / DROPPING /
    UNLOADING / ... still accumulates instead of resetting the counter.
    """

    def setUp(self):
        watchdog.reset_stall_watchdog()
        self.lines: list[str] = []

    def tearDown(self):
        watchdog.reset_stall_watchdog()

    def _feed(
        self,
        positions: dict[str, tuple[int, int]],
        tick: int,
        statuses: dict[str, str] | None = None,
    ) -> list[dict]:
        statuses = statuses or {}
        snapshots = [
            (uid, pos, statuses.get(uid)) for uid, pos in positions.items()
        ]
        return watchdog.update_stall_tracking(
            snapshots, tick, writer=self.lines.append,
        )

    def test_alert_fires_exactly_at_threshold_not_before(self) -> None:
        for tick in range(1, watchdog.STALL_ALERT_THRESHOLD):
            self.assertEqual(self._feed({"u1": (3, 4)}, tick), [])
        alerts = self._feed({"u1": (3, 4)}, watchdog.STALL_ALERT_THRESHOLD)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["uid"], "u1")
        self.assertEqual(alerts[0]["stalled_ticks"], watchdog.STALL_ALERT_THRESHOLD)
        self.assertEqual(len(self.lines), 1)
        self.assertIn("[watchdog] stall_alert", self.lines[0])
        self.assertIn("unit=u1", self.lines[0])
        self.assertIn("pos=(3, 4)", self.lines[0])

    def test_jitter_within_one_cell_still_counts_as_stalled(self) -> None:
        # A-B ping-pong between adjacent cells is a real stall: net
        # displacement never exceeds 1 from the anchor.
        for tick in range(1, watchdog.STALL_ALERT_THRESHOLD + 1):
            pos = (0, 0) if tick % 2 == 0 else (0, 1)
            alerts = self._feed({"u1": pos}, tick)
        self.assertEqual(len(alerts), 1)

    def test_no_repeat_alert_while_still_stalled(self) -> None:
        all_alerts = []
        for tick in range(1, watchdog.STALL_ALERT_THRESHOLD + 20):
            all_alerts.extend(self._feed({"u1": (0, 0)}, tick))
        self.assertEqual(len(all_alerts), 1)
        self.assertEqual(len(self.lines), 1)

    def test_recovery_rearms_the_alert_for_a_second_stall(self) -> None:
        # Review fix: the dedup flag used to stick for the unit's whole life,
        # so a unit that stalled, recovered, and stalled again only alerted
        # once. Moving again (net displacement > 1) must clear the flag.
        for tick in range(1, watchdog.STALL_ALERT_THRESHOLD + 1):
            self._feed({"u1": (0, 0)}, tick)
        self.assertTrue(watchdog.is_stall_alert_emitted("u1"))

        # Recover: jump more than one cell away, then move on one step.
        resume_tick = watchdog.STALL_ALERT_THRESHOLD + 1
        self._feed({"u1": (10, 0)}, resume_tick)
        self.assertFalse(watchdog.is_stall_alert_emitted("u1"))
        self._feed({"u1": (11, 0)}, resume_tick + 1)

        # Second stall episode alerts again at the threshold.
        second_alert_tick = resume_tick + 1 + watchdog.STALL_ALERT_THRESHOLD
        all_alerts = []
        for tick in range(resume_tick + 2, second_alert_tick + 1):
            all_alerts.extend(self._feed({"u1": (11, 0)}, tick))
        self.assertEqual(len(all_alerts), 1)
        self.assertEqual(all_alerts[0]["pos"], (11, 0))
        self.assertEqual(len(self.lines), 2)

    def test_every_live_state_accumulates_only_dead_is_excluded(self) -> None:
        # Review fix: liveness must not be a WAIT/MOVING/HOLD/CAPTURING
        # whitelist — COMBAT/RALLY/DROPPING/UNLOADING etc. count as alive.
        live_states = (
            "WAIT", "MOVING", "HOLD", "CAPTURING",
            "COMBAT", "RALLY", "DROPPING", "UNLOADING",
        )
        for index, state in enumerate(live_states):
            uid = f"unit-{state.lower()}"
            self._feed({uid: (index, 0)}, 1, statuses={uid: state})
            self._feed({uid: (index, 0)}, 2, statuses={uid: state})
            self.assertEqual(
                watchdog.get_stall_ticks(uid, 2), 2,
                f"state {state} must keep accumulating stall ticks",
            )
        self._feed({"dead-unit": (9, 9)}, 1, statuses={"dead-unit": "DEAD"})
        self.assertEqual(watchdog.get_stall_ticks("dead-unit", 1), 0)
        self._feed({"dead-unit": (9, 9)}, 2, statuses={"dead-unit": "dead"})
        self.assertEqual(watchdog.get_stall_ticks("dead-unit", 2), 0)
        # And a DEAD unit never fires the alert even after the threshold.
        for tick in range(1, watchdog.STALL_ALERT_THRESHOLD + 1):
            alerts = self._feed(
                {"dead-unit": (9, 9)}, tick, statuses={"dead-unit": "DEAD"},
            )
        self.assertEqual(alerts, [])

    def test_unit_vanishing_from_the_snapshot_is_pruned(self) -> None:
        self._feed({"u1": (0, 0), "u2": (5, 5)}, 1)
        self.assertEqual(watchdog.get_stall_ticks("u1", 1), 1)
        self.assertEqual(watchdog.get_stall_ticks("u2", 1), 1)
        # u2 disappears (dead/gone): its window must be dropped so a unit
        # reappearing under the same id starts from scratch.
        self._feed({"u1": (0, 0)}, 2)
        self.assertEqual(watchdog.get_stall_ticks("u2", 2), 0)
        self._feed({"u2": (5, 5)}, 3)
        self.assertEqual(watchdog.get_stall_ticks("u2", 3), 1)

    def test_slow_drift_resets_the_anchor_and_never_alerts(self) -> None:
        # A unit moving one cell per tick genuinely progresses: the anchor
        # follows it and no alert fires within the horizon.
        for tick in range(1, watchdog.STALL_ALERT_THRESHOLD + 30):
            alerts = self._feed({"u1": (tick, 0)}, tick)
            self.assertEqual(alerts, [])
        self.assertFalse(watchdog.is_stall_alert_emitted("u1"))

    def test_object_snapshots_and_reset(self) -> None:
        unit = SimpleNamespace(id="u-obj", position=(2, 2), status="MOVING")
        for tick in range(1, watchdog.STALL_ALERT_THRESHOLD + 1):
            alerts = watchdog.update_stall_tracking(
                [unit], tick, writer=self.lines.append,
            )
        self.assertEqual(len(alerts), 1)
        watchdog.reset_stall_watchdog()
        self.assertEqual(watchdog.get_stall_ticks("u-obj",
                         watchdog.STALL_ALERT_THRESHOLD + 1), 0)
        self.assertFalse(watchdog.is_stall_alert_emitted("u-obj"))

    def test_choose_actions_feeds_the_watchdog_every_tick(self) -> None:
        # Review fix: update_stall_tracking used to be dead code — nothing in
        # the production path ever called it. choose_actions now pushes every
        # tick's full unit snapshot, so a unit frozen for the whole
        # threshold window produces a real [watchdog] stall_alert line on
        # stdout (docker-entrypoint.py redirects it into tactic_play.log).
        config = default_config()
        vanguard = SimpleNamespace(
            id="stall-wire", position=(4, 4), unit_type=UnitType.VANGUARD,
            hp=4, status="WAIT",
        )
        vanguard.move = lambda direction: None
        vanguard.wait = lambda: None
        vanguard.sweep = lambda direction: None
        vanguard.shoot = lambda target, expected_cell=None: None
        prev_names = dict(tactic._object_names)
        prev_counters = dict(tactic._object_name_counters)
        prev_stall_pos = dict(tactic._kite_stall_pos)
        prev_stall_ticks = dict(tactic._kite_stall_ticks)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            tactic._object_names.clear()
            tactic._object_name_counters.clear()
            core = SimpleNamespace(
                id="core", position=(0, 0), hp=5, shield=10,
                view=SimpleNamespace(state=SimpleNamespace(value="ALIVE")),
                spawn=lambda unit_type: None,
                heal=lambda: None,
                repair_shield=lambda: None,
                move=lambda *args, **kwargs: None,
                wait=lambda: None,
            )
            beacon = SimpleNamespace(
                position=None, status=SimpleNamespace(name="GROUND"),
            )
            stdout = io.StringIO()
            tactic._map_dirty = False
            try:
                with patch.object(tactic, "load_config", return_value=config), \
                     patch.object(tactic, "MAP_MEMORY_PATH", temp / "map_memory.json"), \
                     patch.object(tactic, "WAYPOINTS_PATH", temp / "waypoints.json"), \
                     patch.object(tactic, "SELF_DESTRUCT_PATH", temp / "self_destruct.json"), \
                     patch.object(tactic, "BATTLE_LOG_PATH", temp / "battle_log.jsonl"), \
                     patch.object(tactic, "CONFIG_PATH", temp / "tactic_config.json"), \
                     contextlib.redirect_stdout(stdout):
                    for tick in range(1, watchdog.STALL_ALERT_THRESHOLD + 1):
                        turn = SimpleNamespace(
                            tick=tick,
                            units=(vanguard,),
                            workers=(),
                            vanguards=(vanguard,),
                            rangers=(),
                            visible_enemies=(),
                            core=core,
                            resources=50,
                            resource_cells=frozenset(),
                            resource_space=0,
                            beacon=beacon,
                            state=SimpleNamespace(population=8),
                            events=(),
                            obstacle_cells=frozenset(),
                        )
                        tactic.choose_actions(turn)
            finally:
                tactic._object_names.clear()
                tactic._object_names.update(prev_names)
                tactic._object_name_counters.clear()
                tactic._object_name_counters.update(prev_counters)
                tactic._kite_stall_pos.clear()
                tactic._kite_stall_pos.update(prev_stall_pos)
                tactic._kite_stall_ticks.clear()
                tactic._kite_stall_ticks.update(prev_stall_ticks)
        output = stdout.getvalue()
        self.assertIn("[watchdog] stall_alert", output)
        self.assertIn("unit=stall-wire", output)
        self.assertTrue(watchdog.is_stall_alert_emitted("stall-wire"))


class RoamOscillationConfigTests(unittest.TestCase):
    """roam_oscillation_escape ships as a registered boolean config field."""

    def test_field_registered_and_defaults_on(self) -> None:
        keys = {field.key for field in tactic_config.CONFIG_FIELDS}
        self.assertIn("roam_oscillation_escape", keys)
        field = tactic_config._FIELDS_BY_KEY["roam_oscillation_escape"]
        self.assertEqual(field.kind, "boolean")
        self.assertTrue(default_config()["roam_oscillation_escape"])

    def test_validation_rejects_non_boolean(self) -> None:
        config = tactic_config.validate_config({"roam_oscillation_escape": False})
        self.assertFalse(config["roam_oscillation_escape"])
        with self.assertRaises(tactic_config.ConfigValidationError):
            tactic_config.validate_config({"roam_oscillation_escape": "yes"})
        with self.assertRaises(tactic_config.ConfigValidationError):
            tactic_config.validate_config({"roam_oscillation_escape": 1})

    def test_shipped_config_file_contains_the_field(self) -> None:
        path = Path(__file__).parent.parent / "tactic_config.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(raw.get("roam_oscillation_escape"))


class KiteStallUnlockRegressionTests(KiteTeamPlannerTests):
    """Kite units frozen by the remote WAIT-vs-detour scoring deadlock must
    unlock once they have stalled for _KITE_STALL_UNLOCK_TICKS threat-free
    ticks (review: WAIT's progress=0 beat a safe detour's progress=-1
    forever). The stall counter is fed by the single _record_kite_stall
    entry point on every _plan_kite_combat exit path."""

    def setUp(self):
        super().setUp()
        self._saved_stall_pos = dict(tactic._kite_stall_pos)
        self._saved_stall_ticks = dict(tactic._kite_stall_ticks)
        self._saved_combat_cache = dict(tactic._combat_path_cache)
        self._saved_dead_obstacles = tactic._dead_obstacles
        tactic._kite_stall_pos.clear()
        tactic._kite_stall_ticks.clear()
        tactic._combat_path_cache.clear()
        tactic._dead_obstacles = None  # recompute dead ends for these walls

    def tearDown(self):
        tactic._kite_stall_pos.clear()
        tactic._kite_stall_pos.update(self._saved_stall_pos)
        tactic._kite_stall_ticks.clear()
        tactic._kite_stall_ticks.update(self._saved_stall_ticks)
        tactic._combat_path_cache.clear()
        tactic._combat_path_cache.update(self._saved_combat_cache)
        tactic._dead_obstacles = self._saved_dead_obstacles
        super().tearDown()

    def test_record_kite_stall_counts_same_cell_and_resets_on_move(self):
        tactic._record_kite_stall("u", (1, 1))
        self.assertEqual(tactic._kite_stall_ticks["u"], 0)
        tactic._record_kite_stall("u", (1, 1))
        self.assertEqual(tactic._kite_stall_ticks["u"], 1)
        tactic._record_kite_stall("u", (1, 1))
        self.assertEqual(tactic._kite_stall_ticks["u"], 2)
        tactic._record_kite_stall("u", (2, 1))  # the unit finally moved
        self.assertEqual(tactic._kite_stall_ticks["u"], 0)

    def test_choose_move_waits_until_the_stall_unlock_threshold(self):
        # Walled pocket: only DOWN/LEFT detours (progress=-1) exist; WAIT is
        # progress=0. Below the threshold WAIT keeps winning, at the
        # threshold the risk-free tie-break ignores progress and a safe
        # detour step is chosen instead.
        unit = self.unit("kite-unlock-score", (0, 0))
        walls = frozenset({(1, 0), (0, -1)})
        for stall_ticks in (0, tactic._KITE_STALL_UNLOCK_TICKS - 1):
            direction, _, _ = tactic._kite_choose_move(
                unit, (0, 0), (10, 0), (), walls, 3, {},
                must_move=False, stall_ticks=stall_ticks,
            )
            self.assertIsNone(direction)
        direction, _, _ = tactic._kite_choose_move(
            unit, (0, 0), (10, 0), (), walls, 3, {},
            must_move=False, stall_ticks=tactic._KITE_STALL_UNLOCK_TICKS,
        )
        self.assertIn(direction, (Direction.DOWN, Direction.LEFT))

    def test_stall_unlock_is_suppressed_while_any_threat_is_visible(self):
        unit = self.unit("kite-unlock-threat", (0, 0))
        walls = frozenset({(1, 0), (0, -1)})
        enemy = self.enemy("unlock-blocker", (10, 10))
        direction, _, _ = tactic._kite_choose_move(
            unit, (0, 0), (10, 0), (enemy,), walls, 3, {},
            must_move=False, stall_ticks=tactic._KITE_STALL_UNLOCK_TICKS,
            global_enemies=(enemy,),
        )
        self.assertIsNone(direction)

    def test_stall_unlock_requires_the_global_enemy_pool_to_be_empty(self):
        # Review fix: the unlock gate used to inspect only this unit's local
        # threats, so an enemy visible to the rest of the army but outside
        # this unit's sight still unlocked it from cover. The gate must use
        # the full visible-enemy tuple the planner received.
        unit = self.unit("kite-unlock-global", (0, 0))
        walls = frozenset({(1, 0), (0, -1)})
        far_enemy = self.enemy("far-visible", (30, 30))
        direction, _, _ = tactic._kite_choose_move(
            unit, (0, 0), (10, 0), (), walls, 3, {},
            must_move=False, stall_ticks=tactic._KITE_STALL_UNLOCK_TICKS,
            global_enemies=(far_enemy,),
        )
        self.assertIsNone(direction)

    def test_stall_unlock_is_suppressed_once_the_goal_is_reached(self):
        # Review fix: a kite squad that has arrived at its coordinate/beacon
        # goal deliberately holds position; unlocking there would jitter the
        # unit one cell every threshold ticks.
        unit = self.unit("kite-unlock-goal", (0, 0))
        walls = frozenset({(1, 0), (0, -1)})
        direction, _, _ = tactic._kite_choose_move(
            unit, (0, 0), (0, 0), (), walls, 3, {},
            must_move=False, stall_ticks=tactic._KITE_STALL_UNLOCK_TICKS,
        )
        self.assertIsNone(direction)

    def test_planner_stops_waiting_after_twelve_stall_ticks(self):
        # End-to-end: every call plans but the unit never moves (its stall
        # counter keeps climbing through the planner's own exit paths). With
        # the route search budget exhausted (bfs_max_steps=1) there is no
        # multi-step detour, so the single-step picker decides — and it must
        # stop returning WAIT forever once the stall threshold is reached.
        unit = self.unit("kite-unlock-plan", (0, 0))
        walls = frozenset({(1, 0), (0, -1)})
        config = dict(self.config)
        config["bfs_max_steps"] = 1
        actions = []
        # The counter anchors at 0 on the first observation, so the planner
        # needs _KITE_STALL_UNLOCK_TICKS + 1 more calls before the scorer
        # reads the threshold. Loop long enough to see MOVE twice.
        for _ in range(tactic._KITE_STALL_UNLOCK_TICKS + 3):
            action, detail = tactic._plan_kite_combat(
                unit,
                unit_kind="vanguard",
                enemies=(),
                obstacle_cells=walls,
                config=config,
                cell_counts={},
            )
            actions.append(action)
        self.assertEqual(
            actions[:tactic._KITE_STALL_UNLOCK_TICKS + 1],
            ["WAIT"] * (tactic._KITE_STALL_UNLOCK_TICKS + 1),
        )
        self.assertEqual(
            actions[tactic._KITE_STALL_UNLOCK_TICKS + 1:],
            ["MOVE", "MOVE"],
        )
        self.assertEqual(unit.action, "MOVE")

    def test_stall_ticks_accumulate_on_the_directive_early_exit(self):
        # Review fix: every exit path of _plan_kite_combat must record the
        # stall, including early returns — otherwise a unit frozen on a
        # branch that never reaches the scorer bypassed the unlock.
        unit = self.unit("kite-stall-directive", (0, 0))
        tactic.turn_context.kite_directives[str(unit.id)] = {
            "kind": "move",
            "direction": Direction.RIGHT,
            "reason": "probe",
        }
        tactic._plan_kite_combat(
            unit,
            unit_kind="vanguard",
            enemies=(),
            obstacle_cells=frozenset(),
            config=self.config,
            cell_counts={},
        )
        # First observation anchors the counter; every further same-cell
        # observation increments it — the early exit is no exception.
        self.assertEqual(tactic._kite_stall_ticks[str(unit.id)], 0)
        tactic._plan_kite_combat(
            unit,
            unit_kind="vanguard",
            enemies=(),
            obstacle_cells=frozenset(),
            config=self.config,
            cell_counts={},
        )
        self.assertEqual(tactic._kite_stall_ticks[str(unit.id)], 1)
        tactic._plan_kite_combat(
            unit,
            unit_kind="vanguard",
            enemies=(),
            obstacle_cells=frozenset(),
            config=self.config,
            cell_counts={},
        )
        self.assertEqual(tactic._kite_stall_ticks[str(unit.id)], 2)


class FriendlySameCellSplitRegressionTests(KiteTeamPlannerTests):
    """Two stacked allies whose same-exit moves keep cancelling server-side
    must deterministically pick different exits, and the split decision is
    sticky: collision events never reset it before the pair separates."""

    def setUp(self):
        super().setUp()
        self._saved_split = dict(tactic._kite_friendly_split)
        self._saved_units_view = getattr(tactic.turn_context, "units_view", ())
        tactic._kite_friendly_split.clear()

    def tearDown(self):
        tactic._kite_friendly_split.clear()
        tactic._kite_friendly_split.update(self._saved_split)
        tactic.turn_context.units_view = self._saved_units_view
        super().tearDown()

    def _stacked_pair(self):
        first = self.unit("vanguard-a", (0, 0))
        second = self.unit("vanguard-b", (0, 0))
        tactic.turn_context.units_view = (first, second)
        return first, second

    def _mark_friendly_collision(self, unit, contested=(1, 0)):
        tactic._kite_collision_streak[str(unit.id)] = (
            (0, 0), contested, 2,
        )

    def _plan_pair(self, first, second):
        names = {"vanguard-a": "V1", "vanguard-b": "V2"}
        with patch.object(
            tactic,
            "_object_name",
            side_effect=lambda uid, prefix: names[str(uid)],
        ):
            first_action, first_detail = tactic._plan_kite_combat(
                first,
                unit_kind="vanguard",
                enemies=(),
                obstacle_cells=frozenset(),
                config=self.config,
                cell_counts={},
            )
            second_action, second_detail = tactic._plan_kite_combat(
                second,
                unit_kind="vanguard",
                enemies=(),
                obstacle_cells=frozenset(),
                config=self.config,
                cell_counts={},
            )
        return (first_action, first.arg, first_detail), (
            second_action, second.arg, second_detail,
        )

    def test_two_stacked_allies_get_different_exits(self):
        first, second = self._stacked_pair()
        self._mark_friendly_collision(first)
        self._mark_friendly_collision(second)
        (a1, d1, det1), (a2, d2, det2) = self._plan_pair(first, second)
        self.assertEqual((a1, a2), ("MOVE", "MOVE"))
        # The deterministic uid ordering yields exactly one split move and
        # one normal route move — never the same exit again.
        self.assertIsNotNone(d1)
        self.assertIsNotNone(d2)
        self.assertNotEqual(d1, d2)
        self.assertIn("kite-friendly-split", det1)
        self.assertNotIn("kite-friendly-split", det2)
        self.assertIn(str(first.id), tactic._kite_friendly_split)
        self.assertNotIn(str(second.id), tactic._kite_friendly_split)

    def test_exits_stay_different_across_consecutive_frames(self):
        first, second = self._stacked_pair()
        for tick in (5, 6, 7):
            tactic.turn_context.tick = tick
            self._mark_friendly_collision(first)
            self._mark_friendly_collision(second)
            (a1, d1, _), (a2, d2, _) = self._plan_pair(first, second)
            self.assertEqual((a1, a2), ("MOVE", "MOVE"), f"tick={tick}")
            self.assertEqual(d1, Direction.DOWN, f"tick={tick}")
            self.assertEqual(d2, Direction.UP, f"tick={tick}")

    def test_split_memo_is_sticky_through_collision_resets(self):
        # Review fix: the collision streak used to be the only carrier of the
        # split state, so a collision event reset it and the pair deadlocked
        # again. The memo alone must keep the contested exit avoided while
        # the unit still stands on the stacked cell, even with no collision
        # streak at all.
        first, second = self._stacked_pair()
        self._mark_friendly_collision(first)
        self._mark_friendly_collision(second)
        self._plan_pair(first, second)  # creates the memo for the yielder
        self.assertIn(str(first.id), tactic._kite_friendly_split)

        for tick in (6, 7, 8):
            tactic.turn_context.tick = tick
            tactic._kite_collision_streak.clear()  # collision event reset
            (a1, d1, det1), _ = self._plan_pair(first, second)
            self.assertEqual(a1, "MOVE", f"tick={tick}")
            # The memo keeps the contested exit (RIGHT) forbidden, so the
            # route detours around it instead of retrying the doomed cell.
            self.assertNotEqual(d1, Direction.RIGHT, f"tick={tick}")
            self.assertEqual(d1, Direction.UP, f"tick={tick}")
            self.assertIn(str(first.id), tactic._kite_friendly_split)

    def test_split_memo_lapses_once_the_unit_leaves_the_stacked_cell(self):
        first, second = self._stacked_pair()
        self._mark_friendly_collision(first)
        self._mark_friendly_collision(second)
        self._plan_pair(first, second)
        self.assertIn(str(first.id), tactic._kite_friendly_split)
        # The yielder actually separated; on the next tick the memo expires.
        first.position = (0, 1)
        tactic.turn_context.tick = 6
        tactic._kite_collision_streak.clear()
        tactic._plan_kite_combat(
            first,
            unit_kind="vanguard",
            enemies=(),
            obstacle_cells=frozenset(),
            config=self.config,
            cell_counts={},
        )
        self.assertNotIn(str(first.id), tactic._kite_friendly_split)

    def test_single_unit_with_residual_collision_keeps_safety_planning(self):
        # Review fix: a lone unit with a residual collision count (no ally on
        # its cell) used to be forced into the sideways split branch,
        # bypassing the safety assessment. It must keep the old behavior:
        # avoid the contested cell for one tick and run the normal
        # safety-assessed planning.
        lone = self.unit("vanguard-lone", (0, 0))
        tactic.turn_context.units_view = (lone,)
        self._mark_friendly_collision(lone)
        action, detail = tactic._plan_kite_combat(
            lone,
            unit_kind="vanguard",
            enemies=(),
            obstacle_cells=frozenset(),
            config=self.config,
            cell_counts={},
        )
        self.assertEqual(action, "MOVE")
        self.assertNotEqual(lone.arg, Direction.RIGHT)
        self.assertNotIn("kite-friendly-split", detail)
        self.assertNotIn(str(lone.id), tactic._kite_friendly_split)

    def test_side_step_is_blocked_when_the_side_cell_is_threatened(self):
        # Review fix: the sideways target cell used to bypass
        # _kite_cell_assessment entirely. With a visible enemy covering the
        # side cell the split must abort and fall back to the normal
        # safety-assessed planning instead of dodging sideways into fire.
        first, second = self._stacked_pair()
        # Below full HP so the full-vanguard-trade branch stays out of it.
        first.hp = 3
        self._mark_friendly_collision(first)
        self._mark_friendly_collision(second)
        # Ranger far enough away that (0,0) stays outside its range (4),
        # while the yielder's side cell (0,1) is still covered (distance 3).
        threat = self.enemy("side-threat", (3, 1), UnitType.RANGER)
        action, detail = tactic._plan_kite_combat(
            first,
            unit_kind="vanguard",
            enemies=(threat,),
            obstacle_cells=frozenset(),
            config=self.config,
            cell_counts={},
        )
        self.assertNotIn("kite-friendly-split", detail)
        if action == "MOVE":
            next_cell = (
                first.position[0] + first.arg.delta[0],
                first.position[1] + first.arg.delta[1],
            )
            self.assertNotEqual(next_cell, (0, 1))

    def test_yield_rank_key_is_symmetric_and_never_touches_display_names(self):
        # Review fix: the ranking used to apply the planning unit's own
        # unit-kind name prefix to every stacked ally (asymmetric across
        # kinds: both sides of the pair could rank themselves first, and
        # minting phantom display names advanced the name counter forever).
        # The key is now (_stable_slot_index(uid, 2), uid): both sides
        # compute the same total order and no display name is touched.
        def rank(uid):
            return (tactic._stable_slot_index(str(uid), 2), str(uid))

        first, second = self._stacked_pair()
        rank_a, rank_b = rank(first.id), rank(second.id)
        self.assertNotEqual(rank_a, rank_b)
        yielder_from_first = first.id if rank_a <= rank_b else second.id
        yielder_from_second = second.id if rank_b <= rank_a else first.id
        self.assertEqual(yielder_from_first, yielder_from_second)

        self._mark_friendly_collision(first)
        self._mark_friendly_collision(second)
        names_before = set(tactic._object_names)
        # Unpatched _object_name on purpose: the split branch must not mint
        # any display names while deciding the yielder (decision logging may
        # still mint each unit's OWN name — that is the pre-existing, kind
        # correct naming path).
        tactic._plan_kite_combat(
            first,
            unit_kind="vanguard",
            enemies=(),
            obstacle_cells=frozenset(),
            config=self.config,
            cell_counts={},
        )
        tactic._plan_kite_combat(
            second,
            unit_kind="vanguard",
            enemies=(),
            obstacle_cells=frozenset(),
            config=self.config,
            cell_counts={},
        )
        new_entries = set(tactic._object_names) - names_before
        for prefix, obj_id in new_entries:
            if str(obj_id) in (str(first.id), str(second.id)):
                self.assertEqual(
                    prefix, "V",
                    "split ranking minted a wrong-prefix name",
                )
        self.assertIn(str(yielder_from_first), tactic._kite_friendly_split)

    def test_split_never_mints_cross_kind_phantom_names(self):
        # Review fix: the old ranking applied the PLANNING unit's name
        # prefix to every stacked ally, minting phantom display names for
        # mixed-kind pairs (a Vanguard planning minted "V…" names for its
        # Ranger ally) and permanently advancing the name counters. The
        # uid-keyed ranking must never touch the display-name system.
        vanguard = self.unit("mixed-v", (0, 0))
        ranger = self.unit("mixed-r", (0, 0), UnitType.RANGER)
        tactic.turn_context.units_view = (vanguard, ranger)
        tactic._kite_collision_streak[str(vanguard.id)] = ((0, 0), (1, 0), 2)
        tactic._kite_collision_streak[str(ranger.id)] = ((0, 0), (1, 0), 2)
        for unit, kind in ((vanguard, "vanguard"), (ranger, "ranger")):
            tactic._plan_kite_combat(
                unit,
                unit_kind=kind,
                enemies=(),
                obstacle_cells=frozenset(),
                config=self.config,
                cell_counts={},
            )
        # No cross-kind phantom: the Vanguard id never gets an "R" name and
        # the Ranger id never gets a "V" name.
        self.assertNotIn(("R", str(vanguard.id)), tactic._object_names)
        self.assertNotIn(("V", str(ranger.id)), tactic._object_names)


class RoamOscillationEscapeRegressionTests(KiteTeamPlannerTests):
    """Guerrilla roam wall ping-pong: once the recorded position history
    proves an A-B-A oscillation (window of _ROAM_STALL_WINDOW observations
    spanning <= _ROAM_STALL_CELLS cells), the unit deflects its bearing
    deterministically and its position actually changes; afterwards it keeps
    avoiding the remembered region until the escape budget expires."""

    def setUp(self):
        super().setUp()
        self._saved_history = {
            uid: list(cells)
            for uid, cells in tactic._roam_pos_history.items()
        }
        self._saved_escape = dict(tactic._roam_escape_left)
        self._saved_cells = dict(tactic._roam_stall_cells)
        self._saved_origin = dict(getattr(tactic, "_roam_stall_origin", {}))
        self._saved_offset = dict(tactic._roam_bearing_offset)
        self._saved_flip_pending = dict(tactic._roam_flip_pending)
        self._saved_flips_used = dict(tactic._roam_flips_used)
        self._saved_dead_obstacles = tactic._dead_obstacles
        tactic._roam_pos_history.clear()
        tactic._roam_escape_left.clear()
        tactic._roam_stall_cells.clear()
        if hasattr(tactic, "_roam_stall_origin"):
            tactic._roam_stall_origin.clear()
        tactic._roam_bearing_offset.clear()
        tactic._roam_flip_pending.clear()
        tactic._roam_flips_used.clear()
        tactic._dead_obstacles = None

    def tearDown(self):
        tactic._roam_pos_history.clear()
        tactic._roam_pos_history.update(self._saved_history)
        tactic._roam_escape_left.clear()
        tactic._roam_escape_left.update(self._saved_escape)
        tactic._roam_stall_cells.clear()
        tactic._roam_stall_cells.update(self._saved_cells)
        if hasattr(tactic, "_roam_stall_origin"):
            tactic._roam_stall_origin.clear()
            tactic._roam_stall_origin.update(self._saved_origin)
        tactic._roam_bearing_offset.clear()
        tactic._roam_bearing_offset.update(self._saved_offset)
        tactic._roam_flip_pending.clear()
        tactic._roam_flip_pending.update(self._saved_flip_pending)
        tactic._roam_flips_used.clear()
        tactic._roam_flips_used.update(self._saved_flips_used)
        tactic._dead_obstacles = self._saved_dead_obstacles
        super().tearDown()

    def _record_history(self, uid, positions):
        """Mirror _guerrilla_roam's per-tick recording: one entry per tick,
        consecutive duplicates collapsed, capped at the region tail."""
        history = tactic._roam_pos_history.setdefault(uid, [])
        for pos in positions:
            if not history or history[-1] != pos:
                history.append(pos)
                del history[:-tactic._ROAM_STALL_REGION]
        return history

    def test_update_stall_triggers_exactly_at_the_window_threshold(self):
        oscillating = ((0, 0), (0, 1))
        # One short of a full window: no trigger.
        self._record_history(
            "probe",
            [oscillating[i % 2] for i in range(tactic._ROAM_STALL_WINDOW - 1)],
        )
        self.assertFalse(tactic._roam_update_stall("probe"))
        # A full window spanning only 2 cells proves the bounce.
        self._record_history(
            "probe", [oscillating[(tactic._ROAM_STALL_WINDOW - 1) % 2]],
        )
        self.assertTrue(tactic._roam_update_stall("probe"))
        self.assertIn((0, 0), tactic._roam_stall_cells["probe"])
        self.assertIn((0, 1), tactic._roam_stall_cells["probe"])
        self.assertEqual(tactic._roam_pos_history["probe"], [])

    def test_wandering_positions_never_trigger(self):
        self._record_history(
            "probe",
            [(i, 0) for i in range(tactic._ROAM_STALL_WINDOW + 5)],
        )
        self.assertFalse(
            tactic._roam_update_stall("probe"),
        )

    def test_oscillation_triggers_escape_and_position_changes(self):
        unit = self.unit("roam-esc", (0, 1))
        # Approach path into the bounce, then a full oscillation window.
        bounce = [(0, 0), (0, 1)] * ((tactic._ROAM_STALL_WINDOW + 1) // 2)
        self._record_history("roam-esc", [(-2, 1), (-1, 1)] + bounce)
        simpos = (0, 1)
        positions: list[tuple[int, int]] = [simpos]
        for step in range(tactic._ROAM_ESCAPE_TICKS + 2):
            action, detail = tactic._guerrilla_roam(
                unit, simpos, frozenset({(0, 0)}), self.config,
            )
            self.assertEqual(action, "MOVE", f"step={step}")
            self.assertIsNotNone(unit.arg, f"step={step}")
            simpos = (
                simpos[0] + unit.arg.delta[0],
                simpos[1] + unit.arg.delta[1],
            )
            positions.append(simpos)
            self._record_history("roam-esc", [simpos])
        # The very first escape step leaves the bounce cells, so the unit's
        # position genuinely changes instead of ping-ponging.
        self.assertNotIn(positions[1], {(0, 0), (0, 1)})
        # While the escape budget runs, every step keeps clear of the
        # remembered region (bounce cells plus the approach path).
        for cell in positions[1:-1]:
            self.assertNotIn(
                cell, {(-2, 1), (-1, 1), (0, 0), (0, 1)},
            )
        self.assertEqual(tactic._roam_escape_left.get("roam-esc"), 0)
        self.assertNotIn("roam-esc", tactic._roam_stall_cells)
        self.assertGreater(
            len(set(positions)), tactic._ROAM_STALL_CELLS,
            "the unit must cover more than the two bounce cells",
        )
        # Review fix: the escape's bearing flip is confirmed by the actual
        # displacement — after the loop the unit really left the region, so
        # the offset flipped exactly once (180 degrees = +4 of 8 slots).
        self.assertEqual(tactic._roam_bearing_offset.get("roam-esc", 0), 4)
        self.assertEqual(tactic._roam_flips_used.get("roam-esc"), 1)
        self.assertNotIn("roam-esc", tactic._roam_flip_pending)

    def test_first_escape_step_is_labelled_escape(self):
        unit = self.unit("roam-esc-label", (0, 1))
        bounce = [(0, 0), (0, 1)] * ((tactic._ROAM_STALL_WINDOW + 1) // 2)
        self._record_history("roam-esc-label", bounce)
        action, detail = tactic._guerrilla_roam(
            unit, (0, 1), frozenset({(0, 0)}), self.config,
        )
        self.assertEqual(action, "MOVE")
        self.assertIn("escape", detail)

    def test_roam_escape_honors_the_roam_oscillation_escape_config(self):
        # The shipped roam_oscillation_escape switch gates the escape
        # mechanism: with it off, a proven oscillation must NOT trigger any
        # escape state and the unit keeps its original bearing roaming.
        unit = self.unit("roam-off", (0, 1))
        bounce = [(0, 0), (0, 1)] * ((tactic._ROAM_STALL_WINDOW + 1) // 2)
        self._record_history("roam-off", bounce)
        config = dict(self.config)
        config["roam_oscillation_escape"] = False
        action, detail = tactic._guerrilla_roam(
            unit,
            (0, 1),
            frozenset({(0, 0)}),
            config,
        )
        self.assertNotIn("escape", detail)
        self.assertNotIn(
            "roam-off", tactic._roam_stall_cells,
            "with the switch off, oscillation detection must stay inactive",
        )
        self.assertEqual(tactic._roam_escape_left.get("roam-off", 0), 0)

        # Same history, switch on (the default): the escape triggers.
        unit2 = self.unit("roam-on", (0, 1))
        self._record_history("roam-on", bounce)
        action2, detail2 = tactic._guerrilla_roam(
            unit2,
            (0, 1),
            frozenset({(0, 0)}),
            self.config,
        )
        self.assertEqual(action2, "MOVE")
        self.assertIn("escape", detail2)

    def test_flip_deferred_until_exit_confirmed_and_capped_at_one(self):
        # Review fix: the bearing flip used to fire on planning intent, so a
        # server-cancelled first escape step re-flipped on the next tick and
        # the offset jittered 0<->4 until the escape budget ran out with a
        # net-zero flip (silent no-op). The flip now waits for the confirmed
        # exit, happens at most once per escape event, and at most once per
        # unit lifetime so long games keep the eight-way bearing spread.
        unit = self.unit("roam-flip", (0, 1))
        bounce = [(0, 0), (0, 1)] * ((tactic._ROAM_STALL_WINDOW + 1) // 2)
        self._record_history("roam-flip", bounce)

        # Escape triggers and picks a step out of the region — but no flip
        # yet: the server has not confirmed the move.
        action, detail = tactic._guerrilla_roam(
            unit, (0, 1), frozenset({(0, 0)}), self.config,
        )
        self.assertEqual(action, "MOVE")
        self.assertIn("escape", detail)
        self.assertEqual(tactic._roam_bearing_offset.get("roam-flip", 0), 0)
        self.assertTrue(tactic._roam_flip_pending.get("roam-flip"))

        # The first escape step is cancelled by the server: the unit still
        # stands inside the region next tick, so the offset must NOT flip.
        action, _ = tactic._guerrilla_roam(
            unit, (0, 1), frozenset({(0, 0)}), self.config,
        )
        self.assertEqual(action, "MOVE")
        self.assertEqual(tactic._roam_bearing_offset.get("roam-flip", 0), 0)
        self.assertTrue(tactic._roam_flip_pending.get("roam-flip"))

        # Next tick the unit really is outside the region: exactly one flip.
        outside = (
            0 + unit.arg.delta[0],
            1 + unit.arg.delta[1],
        )
        self.assertNotIn(outside, tactic._roam_stall_cells["roam-flip"])
        tactic._guerrilla_roam(unit, outside, frozenset({(0, 0)}), self.config)
        self.assertEqual(tactic._roam_bearing_offset["roam-flip"], 4)
        self.assertNotIn("roam-flip", tactic._roam_flip_pending)
        self.assertEqual(tactic._roam_flips_used["roam-flip"], 1)

        # Spend the remaining cooldown budget outside the region.
        pos = outside
        while tactic._roam_escape_left.get("roam-flip", 0) > 0:
            action, _ = tactic._guerrilla_roam(
                unit, pos, frozenset({(0, 0)}), self.config,
            )
            if action == "MOVE" and unit.arg is not None:
                pos = (
                    pos[0] + unit.arg.delta[0],
                    pos[1] + unit.arg.delta[1],
                )
        self.assertEqual(tactic._roam_escape_left.get("roam-flip", 0), 0)

        # A fresh oscillation triggers a second escape event; even after its
        # exit is confirmed the lifetime cap keeps the offset where it is.
        self._record_history("roam-flip", bounce)
        action, detail = tactic._guerrilla_roam(
            unit, (0, 1), frozenset({(0, 0)}), self.config,
        )
        self.assertEqual(action, "MOVE")
        self.assertIn("escape", detail)
        outside2 = (
            0 + unit.arg.delta[0],
            1 + unit.arg.delta[1],
        )
        tactic._guerrilla_roam(
            unit, outside2, frozenset({(0, 0)}), self.config,
        )
        self.assertEqual(tactic._roam_bearing_offset.get("roam-flip", 0), 4)
        self.assertEqual(tactic._roam_flips_used.get("roam-flip", 0), 1)


if __name__ == "__main__":
    unittest.main()
