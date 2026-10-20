from __future__ import annotations

import contextlib
import copy
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

from arena_hero import CoreState, Direction, UnitType

import dashboard
import game_stats
import map_codec
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


class MapSaveCacheTests(unittest.TestCase):
    """The two save-path caches may change how long a save takes, never what
    lands on disk.

    They replace work that ran on every save: re-sorting the whole obstacle set
    (~2.0s at 509k walls) and json.loads-ing the entire file to recover the
    three dashboard-owned fields (~0.4s). Both are now incremental.
    """

    MEMORY_GLOBALS = (
        "_obstacle_memory", "_resource_memory", "_resource_tombstones",
        "_known_obstacles", "_enemy_memory", "_enemy_memory_types",
        "_enemy_memory_ticks", "_enemy_clear_seq", "_core_anchor",
        "_last_dashboard_map_sig", "_map_dirty", "_last_map_save_tick",
    )
    CACHE_GLOBALS = (
        "_obstacle_sorted_cache", "_obstacle_pending_adds", "_map_disk_manual",
        "_map_disk_forgotten", "_map_disk_clear_seq", "_map_file_sig_cache",
    )

    def setUp(self) -> None:
        self._snap = {
            name: copy.deepcopy(getattr(tactic, name))
            for name in self.MEMORY_GLOBALS
        }
        self._reset_caches()

    def tearDown(self) -> None:
        for name, val in self._snap.items():
            setattr(tactic, name, val)
        self._reset_caches()

    def _reset_caches(self) -> None:
        tactic._obstacle_sorted_cache = None
        tactic._obstacle_pending_adds = set()
        tactic._map_disk_manual = set()
        tactic._map_disk_forgotten = set()
        tactic._map_disk_clear_seq = 0
        tactic._map_file_sig_cache = {}

    @staticmethod
    def _seed(path: Path, obstacles: list, manual: list | None = None) -> None:
        path.write_text(json.dumps({
            "obstacles": obstacles,
            "resources": manual or [],
            "manual_resources": manual or [],
            "forgotten_resources": [],
            "enemy_sightings": [],
            "enemy_clear_seq": 0,
            "core_anchor": None,
        }), encoding="utf-8")

    def test_incremental_wall_adds_write_the_same_file_as_a_cold_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "map_memory.json"
            self._seed(path, [[5, 5], [1, 1]])
            with patch.object(tactic, "MAP_MEMORY_PATH", path):
                tactic._load_map_memory()
                tactic._update_obstacle_memory(
                    SimpleNamespace(obstacle_cells=frozenset({(3, 9), (0, -2)}))
                )
                tactic._map_dirty = True
                tactic._save_map_memory(force=True)
                first = json.loads(path.read_text(encoding="utf-8"))["obstacles"]
                # A later wall must be merged into the sorted order, not appended.
                tactic._update_obstacle_memory(
                    SimpleNamespace(obstacle_cells=frozenset({(-7, 4)}))
                )
                tactic._map_dirty = True
                tactic._save_map_memory(force=True)
                second = json.loads(path.read_text(encoding="utf-8"))["obstacles"]

        self.assertEqual(first, sorted([[5, 5], [1, 1], [3, 9], [0, -2]]))
        self.assertEqual(second, sorted(first + [[-7, 4]]))

    def test_clean_save_neither_reparses_the_file_nor_resorts_the_walls(self) -> None:
        # The two costs this change removes, asserted as absences: a save whose
        # file signature is unchanged parses nothing, and a save with no new
        # walls reuses the sorted payload object instead of rebuilding it.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "map_memory.json"
            self._seed(path, [[5, 5], [1, 1], [3, 3]])
            real_loads = tactic.json.loads
            parsed: list[int] = []
            with patch.object(
                tactic.json,
                "loads",
                lambda text, *a, **k: (parsed.append(len(text)), real_loads(text))[1],
            ), patch.object(tactic, "MAP_MEMORY_PATH", path):
                tactic._load_map_memory()
                tactic._map_dirty = True
                tactic._save_map_memory(force=True)  # one parse: the edit-sync
                parsed.clear()
                cached = tactic._obstacle_sorted_cache
                tactic._map_dirty = True
                tactic._save_map_memory(force=True)

            self.assertEqual(parsed, [], "a clean save re-read no file")
            self.assertIsNotNone(cached, "the save must publish a sorted cache")
            self.assertIs(tactic._obstacle_sorted_cache, cached)

    def test_obstacle_added_without_the_pending_bit_still_persists(self) -> None:
        # A future mutation site that forgets to register its add must cost a
        # rebuild, never silently drop a wall from the file.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "map_memory.json"
            self._seed(path, [[1, 1]])
            with patch.object(tactic, "MAP_MEMORY_PATH", path):
                tactic._load_map_memory()
                tactic._map_dirty = True
                tactic._save_map_memory(force=True)  # fills the cache
                tactic._obstacle_memory.add((42, 42))  # deliberately unregistered
                tactic._map_dirty = True
                tactic._save_map_memory(force=True)
                saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertIn([42, 42], saved["obstacles"])
        self.assertEqual(saved["obstacle_count"], len(saved["obstacles"]))

    def test_reload_of_a_smaller_map_wins_over_the_stale_cache(self) -> None:
        # A wholesale reload replaces the obstacle set, so the cached payload may
        # not survive it — otherwise walls the reloaded file never had would be
        # written straight back.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "map_memory.json"
            self._seed(path, [[1, 1], [2, 2], [3, 3], [4, 4]])
            with patch.object(tactic, "MAP_MEMORY_PATH", path):
                tactic._load_map_memory()
                tactic._map_dirty = True
                tactic._save_map_memory(force=True)
                self._seed(path, [[2, 2]])
                tactic._load_map_memory()
                tactic._map_dirty = True
                tactic._save_map_memory(force=True)
                saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(saved["obstacles"], [[2, 2]])

    def test_manual_resource_survives_a_save_that_re_parses_nothing(self) -> None:
        # The second save early-returns from the edit-sync (unchanged file
        # signature), so the dashboard-owned field it used to re-read must come
        # from the cache instead.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "map_memory.json"
            self._seed(path, [[1, 1]], manual=[[3, 3]])
            with patch.object(tactic, "MAP_MEMORY_PATH", path):
                tactic._load_map_memory()
                tactic._map_dirty = True
                tactic._save_map_memory(force=True)
                tactic._map_dirty = True
                tactic._save_map_memory(force=True)
                saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(saved["manual_resources"], [[3, 3]])
        self.assertIn([3, 3], saved["resources"])

    def test_manual_resource_written_on_disk_between_saves_is_preserved(self) -> None:
        # Cross-process case the old full re-parse covered by construction: the
        # dashboard adds a manual resource after tactic's previous save, and the
        # next tactic save must absorb it rather than clobber it.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "map_memory.json"
            self._seed(path, [[1, 1]])
            with patch.object(tactic, "MAP_MEMORY_PATH", path):
                tactic._load_map_memory()
                tactic._map_dirty = True
                tactic._save_map_memory(force=True)
                self._seed(path, [[1, 1]], manual=[[6, 6]])
                tactic._map_dirty = True
                tactic._save_map_memory(force=True)
                saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(saved["manual_resources"], [[6, 6]])
        self.assertIn((6, 6), tactic._resource_memory)

    def test_large_map_saves_packed_and_survives_dashboard_edits(self) -> None:
        obstacles = [[x, 0] for x in range(12)] + [[3, 1], [4, 1], [-2, -3]]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "map_memory.json"
            self._seed(path, obstacles)
            with patch.object(tactic, "MAP_MEMORY_PATH", path), \
                    patch.object(map_codec, "PLAIN_OBSTACLE_LIMIT", 5):
                tactic._load_map_memory()
                tactic._map_dirty = True
                tactic._save_map_memory(force=True)
                raw = json.loads(path.read_text(encoding="utf-8"))
                self.assertNotIn("obstacles", raw)
                self.assertIn(map_codec.OBSTACLE_RLE_KEY, raw)
                decoded = map_codec.decode_obstacles(raw)
                self.assertEqual(decoded, {tuple(p) for p in obstacles})
                with patch.object(dashboard, "MAP_FILE", str(path)):
                    dashboard._map_memory_cache.clear()
                    loaded = dashboard.load_map_memory()
                    self.assertEqual(loaded["obstacles"], sorted(decoded))
                    # A dashboard edit must carry the packed blob along instead
                    # of dropping it (which would erase every wall).
                    dashboard.save_manual_resource(6, 6)
                    raw2 = json.loads(path.read_text(encoding="utf-8"))
                    self.assertNotIn("obstacles", raw2)
                    self.assertIn(map_codec.OBSTACLE_RLE_KEY, raw2)
                    self.assertEqual(map_codec.decode_obstacles(raw2), decoded)


class PlanPathfindingDeadlineTests(unittest.TestCase):
    def tearDown(self) -> None:
        tactic._plan_pathfind_deadline = None

    def test_bfs_path_aborts_once_the_deadline_passed(self) -> None:
        obstacles = frozenset()
        tactic._plan_pathfind_deadline = time.monotonic() - 1
        self.assertIsNone(
            tactic._bfs_path((0, 0), (200, 0), obstacles, max_steps=120_000)
        )

    def test_normal_plan_still_finds_short_paths(self) -> None:
        self.assertIsNone(tactic._plan_pathfind_deadline)
        path = tactic._bfs_path((0, 0), (5, 0), frozenset(), max_steps=120_000)
        self.assertEqual(path, [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0), (5, 0)])

    def test_choose_actions_sets_and_clears_the_deadline(self) -> None:
        seen = {}

        def fake(turn):
            seen["deadline"] = tactic._plan_pathfind_deadline
            return ("WAIT", {})

        with patch.object(tactic, "_choose_actions", fake):
            tactic.choose_actions(SimpleNamespace())
        self.assertIsNotNone(seen["deadline"])
        self.assertIsNone(tactic._plan_pathfind_deadline)


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
        # Ratio off ("0" = all-zero): the legacy behavior keeps every new
        # combat unit in the home roster.
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tactic_config.json"
            config = default_config()
            config["home_team"] = "V1"
            config["attack_team"] = "V2"
            config["combat_team_ratio"] = "0"
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

    def test_new_combat_units_follow_team_ratio(self) -> None:
        # Ratio 1:1:1:1 with empty rosters: four new units spread one per
        # squad (home first, then attack / kite / guerrilla).
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tactic_config.json"
            config = default_config()
            config["combat_team_ratio"] = "1:1:1:1"
            from tactic_config import save_config, load_config

            save_config(config, path)
            turn = SimpleNamespace(
                vanguards=tuple(
                    SimpleNamespace(id=f"vang-{n}") for n in range(1, 5)
                ),
                rangers=(),
            )
            with patch.object(
                tactic,
                "mutate_config",
                side_effect=lambda mutator, _path: tactic_config.mutate_config(mutator, path),
            ), \
                 patch.object(tactic, "CONFIG_PATH", path):
                updated = tactic._auto_enlist_new_combat_units(turn, load_config(path))

        self.assertEqual(updated["home_team"], "V1")
        self.assertEqual(updated["attack_team"], "V2")
        self.assertEqual(updated["kite_team"], "V3")
        self.assertEqual(updated["guerrilla_team"], "V4")

    def test_team_ratio_fills_the_most_understaffed_squad(self) -> None:
        # Roster already 1/1/0/0 with ratio 2:1:1:1: the next unit goes where
        # the deficit against the configured share is largest (kite vs
        # guerrilla tie resolves in squad order home→attack→kite→guerrilla).
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tactic_config.json"
            config = default_config()
            config["home_team"] = "V1"
            config["attack_team"] = "V2"
            config["combat_team_ratio"] = "2:1:1:1"
            from tactic_config import save_config, load_config

            save_config(config, path)
            turn = SimpleNamespace(
                vanguards=(
                    SimpleNamespace(id="vang-1"),
                    SimpleNamespace(id="vang-2"),
                    SimpleNamespace(id="vang-3"),
                ),
                rangers=(),
            )
            with patch.object(
                tactic,
                "mutate_config",
                side_effect=lambda mutator, _path: tactic_config.mutate_config(mutator, path),
            ), \
                 patch.object(tactic, "CONFIG_PATH", path):
                updated = tactic._auto_enlist_new_combat_units(turn, load_config(path))

        # Counts 1/1/0/0: first deficit pick is kite (ties beat guerrilla on
        # order), then the second pick is guerrilla.
        self.assertEqual(updated["kite_team"], "V3")
        self.assertEqual(updated["guerrilla_team"], "")
        self.assertEqual(updated["home_team"], "V1")
        self.assertEqual(updated["attack_team"], "V2")
        self.assertEqual(tactic._combat_team_for("V3", updated), "kite")

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

    def test_attack_auto_prefers_unguarded_core_memory(self) -> None:
        # (2,0) 更近，但 (6,0) 是新鲜的落单核心记忆点 -> 优先清核。
        tactic._enemy_memory.update({(2, 0), (6, 0)})
        tactic._enemy_memory_types[(6, 0)] = "CORE"
        tactic.turn_context.core_pos = (0, 0)
        try:
            unit = self.CombatUnit("v-attack-core", (0, 0))
            self.config["attack_mode"] = "auto"
            self.config["attack_auto_radius"] = 20
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
            self.assertIn("attack-march-auto (6, 0)", detail)
        finally:
            tactic._enemy_memory.discard((2, 0))
            tactic._enemy_memory.discard((6, 0))
            tactic._enemy_memory_types.pop((6, 0), None)
            tactic.turn_context.core_pos = None

    def test_auto_core_targets_drop_guarded_and_stale_memories(self) -> None:
        tactic._enemy_memory_types.update(
            {(4, 0): "CORE", (8, 0): "CORE", (12, 0): "CORE"}
        )
        tactic._enemy_memory_ticks.update({(12, 0): 1000})
        tactic.turn_context.tick = 5000
        try:
            targets, drop = tactic._auto_core_targets(
                {(4, 0), (8, 0), (12, 0)},
                (SimpleNamespace(position=(9, 0), unit_type="VANGUARD"),),
            )

            self.assertEqual(targets, {(4, 0)})
            self.assertEqual(drop, {(8, 0)})
        finally:
            for pos in ((4, 0), (8, 0), (12, 0)):
                tactic._enemy_memory_types.pop(pos, None)
                tactic._enemy_memory_ticks.pop(pos, None)
            tactic.turn_context.tick = 0

    def test_kite_auto_prefers_unguarded_core_memory(self) -> None:
        tactic._enemy_memory.update({(2, 0), (6, 0)})
        tactic._enemy_memory_types[(6, 0)] = "CORE"
        tactic.turn_context.core_pos = (0, 0)
        tactic.turn_context.kite_squad_pos = (0, 0)
        try:
            self.config["kite_mode"] = "auto"
            self.config["kite_auto_radius"] = 20

            mode, target = tactic._kite_mode_target((0, 0), self.config, ())

            self.assertEqual((mode, target), ("auto", (6, 0)))
        finally:
            tactic._enemy_memory.discard((2, 0))
            tactic._enemy_memory.discard((6, 0))
            tactic._enemy_memory_types.pop((6, 0), None)
            tactic.turn_context.core_pos = None
            tactic.turn_context.kite_squad_pos = None

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

    def test_guerrilla_skips_core_with_guard_outside_local_sight(self) -> None:
        """A visible Core is not a target when another sighting reveals an
        adjacent enemy combat unit guarding it."""
        unit = self.CombatUnit("v-g-guarded-core", (0, 0))
        enemies = (
            SimpleNamespace(position=(4, 0), unit_type="CORE"),
            # Outside this Vanguard's local sight (4), but adjacent to Core.
            SimpleNamespace(position=(5, 0), unit_type="VANGUARD"),
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

    def test_guerrilla_prefers_isolated_core_over_distant_worker(self) -> None:
        """Once no combat threat is present, an exposed Core is the solo
        guerrilla objective instead of an unrelated, distant worker."""
        unit = self.CombatUnit("v-g-core-priority", (0, 0))
        enemies = (
            SimpleNamespace(position=(3, 0), unit_type="CORE"),
            SimpleNamespace(position=(4, 0), unit_type="WORKER"),
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
        self.assertIn("kite-route", detail)
        self.assertNotIn("worker", detail.lower())

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

    def test_guerrilla_core_lock_survives_vision_flicker(self) -> None:
        # 核心在墙角会短暂离开视野；目标锁必须让 A* 继续绕行，而不是回漫游。
        unit = self.CombatUnit("r-g-core-lock", (0, 0))
        core = SimpleNamespace(position=(4, 0), unit_type="CORE", id="core-lock")
        tactic.turn_context.tick = 100
        try:
            action, detail = tactic._plan_ranger(
                unit,
                enemies=(core,),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=(0, 0),
                team="guerrilla",
            )
            self.assertEqual(action, "MOVE")
            self.assertIn("kite-route (4, 0)", detail)
            self.assertIn("r-g-core-lock", tactic._guerrilla_core_target)

            tactic.turn_context.tick = 101
            unit.position = (1, 0)
            action, detail = tactic._plan_ranger(
                unit,
                enemies=(),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=(0, 0),
                team="guerrilla",
            )
            self.assertEqual(action, "MOVE")
            self.assertIn("kite-route (4, 0)", detail)
            self.assertNotIn("guerrilla-roam", detail)

            tactic.turn_context.tick = 107
            unit.position = (2, 0)
            action, detail = tactic._plan_ranger(
                unit,
                enemies=(),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=(0, 0),
                team="guerrilla",
            )
            self.assertIn("guerrilla-roam", detail)
        finally:
            tactic._guerrilla_core_target.pop("r-g-core-lock", None)
            tactic.turn_context.tick = 0

    def test_guerrilla_core_lock_drops_when_guard_appears(self) -> None:
        unit = self.CombatUnit("r-g-core-lock-guard", (0, 0))
        core = SimpleNamespace(position=(4, 0), unit_type="CORE", id="core-lock-guard")
        guard = SimpleNamespace(position=(5, 0), unit_type="VANGUARD", id="guard-1")
        tactic.turn_context.tick = 200
        try:
            tactic._plan_ranger(
                unit,
                enemies=(core,),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=(0, 0),
                team="guerrilla",
            )
            self.assertIn("r-g-core-lock-guard", tactic._guerrilla_core_target)

            tactic.turn_context.tick = 201
            tactic._plan_ranger(
                unit,
                enemies=(core, guard),
                obstacle_cells=frozenset(),
                config=self.config,
                core_pos=(0, 0),
                team="guerrilla",
            )
            self.assertNotIn("r-g-core-lock-guard", tactic._guerrilla_core_target)
        finally:
            tactic._guerrilla_core_target.pop("r-g-core-lock-guard", None)
            tactic.turn_context.tick = 0

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

    def test_battle_log_json_entries_normalize_rows(self) -> None:
        """The arena SPA's /api/battle-log gets plain tick/ts/cat/msg rows
        (newest first); raw battle-log records never leak extra keys."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "battle_log.jsonl"
            path.write_text(
                json.dumps({"tick": 1, "ts": 100.0, "cat": "economy",
                            "msg": "挖矿 (1,2)", "extra": "secret"})
                + "\n" + json.dumps({"tick": None, "ts": 200.0, "cat": "config", "msg": "配置调整"})
                + "\nnot-json\n",
                encoding="utf-8",
            )
            with patch.object(dashboard, "BATTLE_LOG_FILE", str(path)):
                entries = dashboard.battle_log_json_entries(limit=10)
        self.assertEqual(entries, [
            {"tick": None, "ts": 200.0, "cat": "config", "msg": "配置调整"},
            {"tick": 1, "ts": 100.0, "cat": "economy", "msg": "挖矿 (1,2)"},
        ])
        for entry in entries:
            self.assertEqual(set(entry), {"tick", "ts", "cat", "msg"})

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




class ApiHealthProbeTests(unittest.TestCase):
    """``/api/health`` is what the container healthcheck and the deploy smoke
    test call.

    Production measured ``/api/state`` at a 22.9 MB body whose ``mapSvg`` field
    alone is 20.8 MB (90.7%) — every remembered wall cell drawn into one SVG —
    answering in 1.7-26.8 s against a healthcheck that gives up at 3 s. Nothing
    consumes that body (the arena SPA never requests ``/api/state``), so the
    30-second probe was the only caller paying for the render, and it was the
    thing the render kept making it fail.
    """

    def _get(self, path: str, log_file: str):
        recorded = {"build_parts": 0}

        class _Wire:
            def __init__(self) -> None:
                self.body = b""

            def write(self, data: bytes) -> None:
                self.body += data

        def boom(*_args, **_kwargs):
            recorded["build_parts"] += 1
            return None

        with (
            patch.object(dashboard, "build_parts", boom),
            patch.object(dashboard, "LOG_FILE", log_file),
            patch.object(dashboard.Handler, "_is_loopback", lambda self: True),
        ):
            handler = dashboard.Handler.__new__(dashboard.Handler)
            handler.path = path
            handler.headers = {}
            wire = _Wire()
            statuses: list[int] = []
            handler.send_response = statuses.append
            handler.send_header = lambda *_pair: None
            handler.end_headers = lambda: None
            handler.wfile = wire
            handler.do_GET()
        return statuses, wire.body, recorded

    def test_probe_answers_small_without_building_the_state_payload(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "tactic_log.jsonl"
            log.write_text(
                json.dumps({"tick": 435397, "plan_unit_actions": {}}) + "\n",
                encoding="utf-8",
            )
            statuses, body, recorded = self._get("/api/health", str(log))
        self.assertEqual(statuses, [200])
        self.assertLess(len(body), 256)
        # The whole point: the probe must not pay for the map render.
        self.assertEqual(recorded["build_parts"], 0)
        self.assertEqual(json.loads(body)["tick"], 435397)

    def test_probe_reports_no_tick_without_a_log_but_still_answers(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            statuses, body, _recorded = self._get(
                "/api/health", str(Path(tmp) / "absent.jsonl"),
            )
        self.assertEqual(statuses, [200])
        self.assertIsNone(json.loads(body)["tick"])

    def test_state_endpoint_still_builds_the_full_payload(self) -> None:
        # The two endpoints differ on purpose: /api/state keeps rendering the
        # panel payload it always did (the deploy smoke test still proves it
        # once), /api/health never does.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            statuses, _body, recorded = self._get("/api/state", Path(tmp) / "x.jsonl")
        self.assertEqual(statuses, [200])
        self.assertEqual(recorded["build_parts"], 1)


class CoreRailBarTests(unittest.TestCase):
    """Core vitals bars come from the live helpers (_seg_cells /
    _core_rail_html) that /api/state still emits for the arena console."""

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
        self.save_calls = []
        with contextlib.redirect_stdout(io.StringIO()):
            with (
                patch.object(tactic, "ArenaHeroClient", return_value=client),
                patch.object(tactic, "TacticLogger", self._FakeLogger),
                patch.object(tactic, "_load_map_memory", lambda: None),
                patch.object(
                    tactic, "_save_map_memory",
                    lambda *a, **k: self.save_calls.append(k.get("force", False)),
                ),
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

    def test_stale_session_exit_skips_the_forced_map_save(self) -> None:
        # The forced save serializes the whole wall map; on the small VPS that
        # blocked the exiting process in swap and the container restart waited
        # minutes. The map is already saved every few ticks, so skip it here.
        code = self._run_play([self._mismatch() for _ in range(6)])
        self.assertEqual(code, 3)
        self.assertEqual(self.save_calls, [])

    def test_normal_stream_end_still_saves_the_map(self) -> None:
        code = self._run_play([None, None])
        self.assertEqual(code, 9)
        self.assertTrue(self.save_calls, "shutdown save must still run")
        self.assertTrue(all(self.save_calls))

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

    The React build is served from web/dist with the game API proxied behind the
    dashboard token (the proxy injects the server-side API key). The legacy
    server-rendered page was retired: "/" now lands on /arena and /dashboard is
    gone.
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

    def test_root_redirects_to_arena(self) -> None:
        status, headers, _ = self._request("GET", "/")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/arena")

    def test_legacy_dashboard_path_is_gone(self) -> None:
        status, _headers, body = self._request("GET", "/dashboard")
        self.assertEqual(status, 404, body[:120])

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
        # ±1 micro-jitter stays inside the anchor drift bound and keeps
        # accumulating instead of restarting the unlock window.
        tactic._record_kite_stall("u", (2, 1))
        self.assertEqual(tactic._kite_stall_ticks["u"], 3)
        # A genuine move (net displacement > _KITE_STALL_DRIFT from the
        # anchor) finally resets the counter and re-anchors.
        tactic._record_kite_stall("u", (3, 1))
        self.assertEqual(tactic._kite_stall_ticks["u"], 0)
        self.assertEqual(tactic._kite_stall_pos["u"], (3, 1))

    def test_contested_micro_jitter_keeps_accumulating_to_the_unlock(self):
        # Field blind spot (V15 vanguard e17ed270): the goal cell was
        # MOVE_CONTESTED every tick while an occasional ±1 DOWN step
        # succeeded; the old reset-on-any-move counter never reached the
        # 12-tick threshold. With the anchor-based counter the stall window
        # must keep accumulating through the jitter and actually cross the
        # unlock gate.
        anchor = (-145, -588)
        micro = (-145, -587)
        tactic._record_kite_stall("v15", anchor)
        self.assertEqual(tactic._kite_stall_ticks["v15"], 0)
        ticks_seen = []
        for i in range(tactic._KITE_STALL_UNLOCK_TICKS + 4):
            # Every 3rd observation the unit slips one cell (the observed
            # once-every-3-4-ticks micro-move); every other tick stays put.
            pos = micro if i % 3 == 2 else anchor
            tactic._record_kite_stall("v15", pos)
            ticks_seen.append(tactic._kite_stall_ticks["v15"])
        self.assertEqual(
            ticks_seen,
            list(range(1, len(ticks_seen) + 1)),
            "micro-jitter must never reset the stall window",
        )
        self.assertGreaterEqual(
            ticks_seen[-1], tactic._KITE_STALL_UNLOCK_TICKS,
        )
        # The anchor never moved, so the unlock gate the scorer reads is the
        # accumulated counter itself.
        self.assertEqual(tactic._kite_stall_pos["v15"], anchor)
        unit = self.unit("v15-unlock", anchor)
        walls = frozenset({(-146, -588), (-145, -589)})
        direction, _, _ = tactic._kite_choose_move(
            unit, anchor, (-144, -588), (), walls, 3, {},
            must_move=False,
            stall_ticks=tactic._kite_stall_ticks["v15"],
        )
        self.assertIsNotNone(
            direction,
            "the accumulated stall must finally unlock a detour step",
        )

    def test_real_move_of_two_or_more_cells_resets_counter_and_anchor(self):
        # False-trigger guard: once the unit genuinely breaks out (net
        # displacement >= 2 from the anchor), the counter resets and the new
        # position becomes the anchor for the next stall window.
        tactic._record_kite_stall("u", (0, 0))
        for _ in range(5):
            tactic._record_kite_stall("u", (0, 0))
        self.assertEqual(tactic._kite_stall_ticks["u"], 5)
        tactic._record_kite_stall("u", (2, 0))
        self.assertEqual(tactic._kite_stall_ticks["u"], 0)
        self.assertEqual(tactic._kite_stall_pos["u"], (2, 0))
        # A diagonal breakout beyond the drift bound (Chebyshev >= 2 from
        # the anchor) resets just the same.
        for _ in range(3):
            tactic._record_kite_stall("u", (2, 0))
        tactic._record_kite_stall("u", (4, 1))
        self.assertEqual(tactic._kite_stall_ticks["u"], 0)
        self.assertEqual(tactic._kite_stall_pos["u"], (4, 1))
        # And a steady one-cell-per-tick advance never lets the window
        # accumulate toward the threshold (each second step re-anchors).
        tactic._record_kite_stall("w", (0, 0))
        for i in range(1, tactic._KITE_STALL_UNLOCK_TICKS * 2):
            tactic._record_kite_stall("w", (i, 0))
            self.assertLessEqual(
                tactic._kite_stall_ticks["w"], 1,
                f"steady advance must never build a stall window (i={i})",
            )

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


class KiteThreatShuttleBreakerTests(KiteTeamPlannerTests):
    """A threat-visible A-B-A shuttle needs its own breaker.

    Field (after the 5th deploy): R7 (ranger, hp=1) alternated (934,-45) and
    (934,-44) 36 Ticks each way, net displacement 0, MOVE every Tick; V1 did the
    same between (327,-174) and (327,-173). The existing stall unlock cannot
    help them — it is deliberately suppressed while any enemy is visible, and
    these units see one every Tick. And `-backtrack` cannot win either: the
    U-turn cell ties the alternative on threat distance and beats it on goal
    progress, so the two cells already differ higher up in the score tuple.
    """

    ENEMY_AT = (0, 0)
    POS = (2, -1)
    CAME_FROM = (2, 0)
    WALLS = frozenset({(3, -1)})
    RANGE = 2

    def setUp(self):
        super().setUp()
        self._saved_prev = dict(tactic._kite_prev_pos)
        self._saved_prev_prev = dict(tactic._kite_prev_prev_pos)
        self._saved_shuttle = dict(tactic._kite_shuttle_ticks)
        self._saved_stall_ticks = dict(tactic._kite_stall_ticks)
        self._saved_stall_pos = dict(tactic._kite_stall_pos)
        tactic._kite_prev_pos.clear()
        tactic._kite_prev_prev_pos.clear()
        tactic._kite_shuttle_ticks.clear()

    def tearDown(self):
        for name, saved in (
            ("_kite_prev_pos", self._saved_prev),
            ("_kite_prev_prev_pos", self._saved_prev_prev),
            ("_kite_shuttle_ticks", self._saved_shuttle),
            ("_kite_stall_ticks", self._saved_stall_ticks),
            ("_kite_stall_pos", self._saved_stall_pos),
        ):
            getattr(tactic, name).clear()
            getattr(tactic, name).update(saved)
        super().tearDown()

    def _choose(self, uid, reversals):
        unit = self.unit(uid, self.POS)
        ranger = self.enemy("shooter", self.ENEMY_AT, UnitType.RANGER)
        tactic._kite_prev_pos[uid] = self.CAME_FROM
        tactic._kite_shuttle_ticks[uid] = reversals
        direction, assessment, candidates = tactic._kite_choose_move(
            unit, self.POS, self.ENEMY_AT, (ranger,), self.WALLS, self.RANGE, {},
            must_move=True, stall_ticks=0, global_enemies=(ranger,),
        )
        return unit, direction, assessment, candidates

    def test_below_the_gate_the_undo_my_last_step_still_wins(self) -> None:
        # Proves the geometry really does rank the U-turn first: DOWN (2,0) is
        # the cell it came from, ties UP (2,-2) on threat distance (Chebyshev 2)
        # and beats it on progress toward the goal.
        _unit, direction, _a, candidates = self._choose(
            "kite-shuttle-gate", reversals=0,
        )
        self.assertEqual(direction, Direction.DOWN, candidates)

    def test_the_gate_marks_itself_in_the_assessment(self) -> None:
        """A breaker nobody can see fire stays invisible for days.

        The contested-backoff id[:8] bug sat undetected in 44MB of production
        log; this gate therefore announces itself in the action detail (the
        caller renders ``assessment["shuttle_break"]`` as " shuttle-break").
        """
        _unit, _direction, assessment, _c = self._choose(
            "kite-shuttle-marked",
            reversals=tactic._KITE_SHUTTLE_BREAK_REVERSALS,
        )
        self.assertTrue(assessment.get("shuttle_break"), assessment)
        _unit, _direction, quiet, _c = self._choose(
            "kite-shuttle-unmarked", reversals=0,
        )
        self.assertFalse(quiet.get("shuttle_break"), quiet)

    def test_shuttle_gate_drops_the_u_turn_and_steps_deeper_away(self) -> None:
        _unit, direction, _a, candidates = self._choose(
            "kite-shuttle-break",
            reversals=tactic._KITE_SHUTTLE_BREAK_REVERSALS,
        )
        self.assertEqual(
            direction, Direction.UP,
            "past the gate the unit must keep backing off instead of re-entering "
            "the ring it just left",
        )
        self.assertNotIn(
            self.CAME_FROM, [tuple(c["cell"]) for c in candidates],
            "the pruned option must not survive into the ranked list",
        )

    def test_a_deliberate_hold_never_counts_as_a_shuttle(self) -> None:
        """Field correction after the 7th deploy.

        The first version gated on the stall counter ("ticks without progress"),
        which fired 8 times on V4 — a unit sitting on ``kite-hold-safe`` by
        design — and poked it out of its chosen safe cell every ~16 Ticks. A
        hold matches pos == prev == prev_prev trivially; only a unit that keeps
        *undoing an actual move* is shuttling.
        """
        uid = "v4-holder"
        for _ in range(tactic._KITE_SHUTTLE_BREAK_REVERSALS + 5):
            tactic._record_kite_stall(uid, (503, -216))
        self.assertEqual(tactic._kite_shuttle_ticks[uid], 0)
        # And the same counter does climb for a genuine A-B-A.
        other = "r7-shuttle"
        for pos in ((934, -45), (934, -44)) * 4:
            tactic._record_kite_stall(other, pos)
        self.assertGreaterEqual(
            tactic._kite_shuttle_ticks[other],
            tactic._KITE_SHUTTLE_BREAK_REVERSALS,
        )

    def test_the_marker_reaches_the_action_detail(self) -> None:
        """End-to-end on R7's own path (ranger at hp=1, same geometry).

        The chooser-level flag is worthless if the planner does not surface it:
        the grep for "shuttle-break" in the live log is what tells the next
        inspection whether this gate ever actually fires in the field.
        """
        config = dict(self.config)
        config["bfs_max_steps"] = 1
        shooter = self.enemy("mark-shooter-e2e", self.ENEMY_AT, UnitType.RANGER)
        for reversals, expect in (
            (0, Direction.DOWN),
            (tactic._KITE_SHUTTLE_BREAK_REVERSALS, Direction.UP),
        ):
            unit = self.unit(
                f"mark-e2e-{reversals}", self.POS,
                unit_type=UnitType.RANGER, hp=1,
            )
            tactic._kite_shuttle_ticks[unit.id] = reversals
            tactic._kite_prev_pos[unit.id] = self.CAME_FROM
            action, detail = tactic._plan_kite_combat(
                unit, unit_kind="ranger", enemies=(shooter,),
                obstacle_cells=self.WALLS, config=config, cell_counts={},
            )
            self.assertEqual(action, "MOVE", detail)
            self.assertEqual(unit.arg, expect, detail)
            if expect is Direction.UP:
                self.assertIn("shuttle-break", detail)
            else:
                self.assertNotIn("shuttle-break", detail)

    def test_u_turn_still_allowed_when_it_is_the_only_exit(self) -> None:
        # A unit in a one-cell nub must be allowed to retreat the way it came.
        # Two shapes of that, because the chooser ranks safety first:
        self.WALLS = frozenset({(3, -1), (2, -2), (1, -1)})
        try:
            # (a) a ranger threat makes WAIT illegal, the U-turn is the only step.
            _unit, direction, _a, candidates = self._choose(
                "kite-only-exit-move",
                reversals=tactic._KITE_SHUTTLE_BREAK_REVERSALS + 5,
            )
            self.assertEqual(direction, Direction.DOWN, candidates)

            # (b) no threat at all: WAIT is a legal candidate and is
            # equal-safe with the retreat step. If WAIT counted as "another
            # option", the prune would strip the U-turn and leave the unit
            # sitting in the nub forever.
            unit = self.unit("kite-only-exit-wait", self.POS)
            tactic._kite_prev_pos[unit.id] = self.CAME_FROM
            tactic._kite_shuttle_ticks[unit.id] = (
                tactic._KITE_SHUTTLE_BREAK_REVERSALS + 5
            )
            direction, _assessment, candidates = tactic._kite_choose_move(
                unit, self.POS, self.ENEMY_AT, (), self.WALLS, self.RANGE, {},
                must_move=False, stall_ticks=tactic._KITE_STALL_UNLOCK_TICKS,
            )
            self.assertEqual(
                direction, Direction.DOWN,
                f"retreat must beat WAIT in a nub: {candidates}",
            )
        finally:
            self.WALLS = frozenset({(3, -1)})



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
        self._saved_banned = {
            uid: dict(cells) for uid, cells in tactic._roam_banned.items()
        }
        self._saved_dead_obstacles = tactic._dead_obstacles
        tactic._roam_pos_history.clear()
        tactic._roam_escape_left.clear()
        tactic._roam_stall_cells.clear()
        if hasattr(tactic, "_roam_stall_origin"):
            tactic._roam_stall_origin.clear()
        tactic._roam_bearing_offset.clear()
        tactic._roam_flip_pending.clear()
        tactic._roam_flips_used.clear()
        tactic._roam_banned.clear()
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
        tactic._roam_banned.clear()
        tactic._roam_banned.update(self._saved_banned)
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

    def test_three_cell_loop_oscillation_triggers_escape(self):
        # Field blind spot: unit 2a4d82bf cycled forever through three
        # cells — (270,-744)/(270,-743)/(269,-744) — which the old
        # <=_ROAM_STALL_CELLS (2 distinct) test never caught. A full window
        # cycling through 3 cells with a tiny net displacement must trigger.
        loop = [(270, -744), (270, -743), (269, -744)]
        self._record_history(
            "probe3",
            [loop[i % 3] for i in range(tactic._ROAM_STALL_WINDOW)],
        )
        self.assertTrue(tactic._roam_update_stall("probe3"))
        for cell in loop:
            self.assertIn(cell, tactic._roam_stall_cells["probe3"])
        self.assertEqual(tactic._roam_pos_history["probe3"], [])

        # End to end through _guerrilla_roam: a fresh history filling one
        # window and one more recorded step flips the unit into the escape
        # phase (labelled "escape") with a full escape budget.
        unit = self.unit("roam3", (269, -744))
        self._record_history(
            "roam3",
            [loop[i % 3] for i in range(tactic._ROAM_STALL_WINDOW)],
        )
        action, detail = tactic._guerrilla_roam(
            unit, (269, -744), frozenset(), self.config,
        )
        self.assertEqual(action, "MOVE")
        self.assertIn("escape", detail)
        self.assertEqual(
            tactic._roam_escape_left.get("roam3"),
            tactic._ROAM_ESCAPE_TICKS,
        )

    def test_genuine_outward_roaming_with_growing_displacement_never_triggers(self):
        # False-positive guard: a roaming unit that covers more than 3
        # distinct cells and keeps drifting outward (net displacement of
        # every window grows) must never be mistaken for a 3-cell loop.
        self._record_history(
            "roamer",
            [(i, i) for i in range(tactic._ROAM_STALL_WINDOW + 8)],
        )
        self.assertFalse(tactic._roam_update_stall("roamer"))
        self.assertNotIn("roamer", tactic._roam_stall_cells)
        # A staircase walk (two cells per new column) also keeps growing
        # its net displacement and must stay untouched.
        self._record_history(
            "stair",
            [(i // 2, i % 2) for i in range(tactic._ROAM_STALL_WINDOW + 8)],
        )
        self.assertFalse(tactic._roam_update_stall("stair"))
        self.assertNotIn("stair", tactic._roam_stall_cells)

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

    def test_flip_deferred_until_exit_confirmed_then_escalates(self):
        # Review fix: the bearing flip used to fire on planning intent, so a
        # server-cancelled first escape step re-flipped on the next tick and
        # the offset jittered 0<->4 until the escape budget ran out with a
        # net-zero flip (silent no-op). The flip now waits for the confirmed
        # exit and fires once per escape event, escalating with each repeat.
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

        # A fresh oscillation triggers a second escape event. The lifetime cap
        # used to leave the offset frozen here — every later escape then replayed
        # the same heading straight back into the pocket (observed: R2 re-trapping
        # every 8 ticks). Instead the heading turns a quarter further.
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
        self.assertEqual(tactic._roam_bearing_offset.get("roam-flip", 0), 6)
        self.assertEqual(tactic._roam_flips_used.get("roam-flip", 0), 2)

    def test_escaped_pocket_stays_banned_after_the_escape_budget_dies(self):
        """A confirmed escape must not walk straight back into the same bounce.

        The active escape forgets its region when the _ROAM_ESCAPE_TICKS budget
        expires, so the ID-derived bearing re-picks the pocket heading on the
        very next tick — that amnesia is what kept R2/V7 oscillating for hours.
        The banned-region tail is what makes one escape change the route.
        """
        unit = self.unit("roam-ban", (0, 1))
        bounce = [(0, 0), (0, 1)] * ((tactic._ROAM_STALL_WINDOW + 1) // 2)
        self._record_history("roam-ban", bounce)

        action, detail = tactic._guerrilla_roam(
            unit, (0, 1), frozenset({(0, 0)}), self.config,
        )
        self.assertEqual(action, "MOVE")
        self.assertIn("escape", detail)
        self.assertTrue(tactic._roam_banned.get("roam-ban"))
        self.assertIn((0, 1), tactic._roam_banned["roam-ban"])

        # Drain the escape budget: the active region is gone, the ban is not.
        pos = (0, 1)
        for _ in range(tactic._ROAM_ESCAPE_TICKS + 2):
            tactic._guerrilla_roam(unit, pos, frozenset({(0, 0)}), self.config)
            if unit.arg is not None:
                pos = (pos[0] + unit.arg.delta[0], pos[1] + unit.arg.delta[1])
        self.assertEqual(tactic._roam_escape_left.get("roam-ban", 0), 0)
        self.assertNotIn("roam-ban", tactic._roam_stall_cells)
        self.assertTrue(tactic._roam_banned.get("roam-ban"))

        # Once the ban expires the unit gets that terrain back.
        tactic._roam_banned["roam-ban"] = {
            cell: tactic.turn_context.tick - 1
            for cell in tactic._roam_banned["roam-ban"]
        }
        tactic._guerrilla_roam(unit, pos, frozenset({(0, 0)}), self.config)
        self.assertNotIn("roam-ban", tactic._roam_banned)

    def test_scout_least_bad_exit_is_not_blocked_by_the_escape_blacklist(self):
        """A nub whose only exit is blacklisted must move, not freeze.

        Field: V7 spent 5 straight ticks on WAIT:no_way at (1890,926). Its one
        free neighbour (1890,925) was the bounce region the roam escape told it
        to avoid, and the least-bad-exit pass applied that blacklist too, so the
        escape sealed the unit inside the pocket it was trying to leave. A
        stationary unit takes damage and advances nothing, which is strictly
        worse than re-entering the region — the stall memory is a preference,
        not a wall.
        """
        uid = "scout-nub"
        unit = self.unit(uid, (0, 0))
        only_exit = next(iter(tactic._neighbor_cells((0, 0))))
        walls = frozenset(tactic._neighbor_cells((0, 0))) - {only_exit}
        action, detail = tactic._scout_cardinal(
            unit, (0, 0), walls, self.config,
            label="guerrilla-roam escape",
            avoid_cells=(only_exit,),
        )
        self.assertEqual(action, "MOVE", detail)
        self.assertEqual(
            (only_exit[0], only_exit[1]),
            (unit.arg.delta[0], unit.arg.delta[1]),
        )


class SquadTargetQueueTests(unittest.TestCase):
    """进攻队/风筝队目标队列：多点进攻按序推进，到达且肃清后自动下一个。"""

    def setUp(self) -> None:
        self._prev_enemy_memory = set(tactic._enemy_memory)
        self._prev_attack_queue = tactic.turn_context.attack_queue_target
        self._prev_kite_queue = tactic.turn_context.kite_queue_target

    def tearDown(self) -> None:
        tactic._enemy_memory.clear()
        tactic._enemy_memory.update(self._prev_enemy_memory)
        tactic.turn_context.attack_queue_target = self._prev_attack_queue
        tactic.turn_context.kite_queue_target = self._prev_kite_queue

    def test_parse_squad_targets_payload_normalizes(self) -> None:
        parsed = tactic._parse_squad_targets_payload({
            "attack": [[1, 2], ["x", 3], (4, 5)],
            "kite": [],
            "bogus": [[1, 1]],
        })
        self.assertEqual(parsed, {"attack": [(1, 2), (4, 5)]})

    def test_advance_pops_arrived_and_cleared_head(self) -> None:
        logs: list = []
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "squad_targets.json"
            path.write_text(json.dumps({"attack": [[5, 0], [10, 0]]}), encoding="utf-8")
            with patch.object(tactic, "SQUAD_TARGETS_PATH", path):
                current, remaining = tactic._advance_squad_target_queue(
                    "attack", [(5, 0), (10, 0)], (5, 0), (), logs,
                )
                saved = tactic._load_squad_targets_unlocked()
        self.assertEqual(current, (10, 0))
        self.assertEqual(remaining, [(10, 0)])
        self.assertEqual(saved, {"attack": [(10, 0)]})
        self.assertEqual(len(logs), 1)
        self.assertIn("进攻队目标肃清 (5,0)", logs[0]["msg"])
        self.assertIn("(10,0)", logs[0]["msg"])

    def test_advance_keeps_head_while_visible_enemy_near(self) -> None:
        enemy = SimpleNamespace(position=(6, 0))
        logs: list = []
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "squad_targets.json"
            path.write_text(json.dumps({"attack": [[5, 0], [10, 0]]}), encoding="utf-8")
            with patch.object(tactic, "SQUAD_TARGETS_PATH", path):
                current, remaining = tactic._advance_squad_target_queue(
                    "attack", [(5, 0), (10, 0)], (5, 0), (enemy,), logs,
                )
                saved = tactic._load_squad_targets_unlocked()
        self.assertEqual(current, (5, 0))
        self.assertEqual(remaining, [(5, 0), (10, 0)])
        self.assertEqual(saved["attack"], [(5, 0), (10, 0)])
        self.assertEqual(logs, [])

    def test_advance_keeps_head_while_memory_enemy_near(self) -> None:
        tactic._enemy_memory.add((5, 2))
        logs: list = []
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "squad_targets.json"
            path.write_text(json.dumps({"kite": [[5, 0]]}), encoding="utf-8")
            with patch.object(tactic, "SQUAD_TARGETS_PATH", path):
                current, _ = tactic._advance_squad_target_queue(
                    "kite", [(5, 0)], (5, 0), (), logs,
                )
        self.assertEqual(current, (5, 0))
        self.assertEqual(logs, [])

    def test_advance_keeps_head_until_arrived(self) -> None:
        logs: list = []
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "squad_targets.json"
            path.write_text(json.dumps({"attack": [[5, 0]]}), encoding="utf-8")
            with patch.object(tactic, "SQUAD_TARGETS_PATH", path):
                current, _ = tactic._advance_squad_target_queue(
                    "attack", [(5, 0)], (0, 0), (), logs,
                )
        self.assertEqual(current, (5, 0))
        self.assertEqual(logs, [])

    def test_advance_never_moves_for_wiped_squad(self) -> None:
        logs: list = []
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "squad_targets.json"
            path.write_text(json.dumps({"attack": [[5, 0]]}), encoding="utf-8")
            with patch.object(tactic, "SQUAD_TARGETS_PATH", path):
                current, _ = tactic._advance_squad_target_queue(
                    "attack", [(5, 0)], None, (), logs,
                )
                saved = tactic._load_squad_targets_unlocked()
        self.assertEqual(current, (5, 0))
        self.assertEqual(saved["attack"], [(5, 0)])

    def test_advance_pops_consecutive_cleared_heads(self) -> None:
        logs: list = []
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "squad_targets.json"
            path.write_text(json.dumps({"attack": [[5, 0], [5, 1]]}), encoding="utf-8")
            with patch.object(tactic, "SQUAD_TARGETS_PATH", path):
                current, remaining = tactic._advance_squad_target_queue(
                    "attack", [(5, 0), (5, 1)], (5, 0), (), logs,
                )
                saved = tactic._load_squad_targets_unlocked()
        self.assertIsNone(current)
        self.assertEqual(remaining, [])
        self.assertEqual(saved, {})
        self.assertEqual(len(logs), 2)
        self.assertIn("目标队列已清空", logs[-1]["msg"])

    def test_queue_head_overrides_static_attack_coords(self) -> None:
        config = default_config()
        config["attack_target_x"] = 99
        config["attack_target_y"] = 99
        unit = CombatTeamPlannerTests.CombatUnit("v-queue", (0, 0))
        tactic.turn_context.attack_queue_target = (8, 0)
        action, detail = tactic._plan_vanguard(
            unit,
            enemies=(),
            obstacle_cells=frozenset(),
            config=config,
            core_pos=(0, 0),
            team="attack",
        )
        self.assertEqual(action, "MOVE")
        self.assertIn("attack-march-coords (8, 0)", detail)

    def test_kite_queue_head_overrides_static_coords(self) -> None:
        config = default_config()
        config["kite_mode"] = "coords"
        config["kite_target_x"] = 99
        config["kite_target_y"] = 99
        tactic.turn_context.kite_queue_target = (7, 2)
        mode, target = tactic._kite_mode_target((0, 0), config)
        self.assertEqual((mode, target), ("coords", (7, 2)))
        # 非 coords 模式不消费队列。
        config["kite_mode"] = "beacon"
        tactic.turn_context.beacon_pos = (1, 1)
        try:
            mode, target = tactic._kite_mode_target((0, 0), config)
            self.assertEqual((mode, target), ("beacon", (1, 1)))
        finally:
            tactic.turn_context.beacon_pos = None


class DashboardSquadTargetTests(unittest.TestCase):
    def test_add_remove_clear_roundtrip_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            st_file = Path(temp_dir) / "squad_targets.json"
            log_file = Path(temp_dir) / "battle_log.jsonl"
            with patch.object(dashboard, "SQUAD_TARGETS_FILE", str(st_file)), \
                 patch.object(dashboard, "BATTLE_LOG_FILE", str(log_file)):
                self.assertTrue(dashboard.add_squad_target("attack", -2000, -2000)["ok"])
                self.assertEqual(
                    dashboard.load_squad_targets(), {"attack": [[-2000, -2000]]},
                )
                self.assertTrue(dashboard.add_squad_target("attack", 7, 2)["ok"])
                self.assertEqual(dashboard.load_squad_targets(), {
                    "attack": [[-2000, -2000], [7, 2]],
                })
                self.assertTrue(dashboard.add_squad_target("kite", 1, 1)["ok"])
                self.assertTrue(dashboard.remove_squad_target("attack", index=0)["ok"])
                self.assertEqual(dashboard.load_squad_targets(), {
                    "attack": [[7, 2]], "kite": [[1, 1]],
                })
                # Removing the last target drops the squad's entry entirely.
                self.assertTrue(dashboard.remove_squad_target("attack", index=0)["ok"])
                self.assertEqual(dashboard.load_squad_targets(), {"kite": [[1, 1]]})
                self.assertFalse(dashboard.remove_squad_target("attack")["ok"])
                self.assertFalse(dashboard.remove_squad_target("attack", index=3)["ok"])
                with self.assertRaises(ValueError):
                    dashboard.add_squad_target("bogus", 0, 0)
                self.assertTrue(dashboard.clear_squad_targets("kite")["ok"])
                self.assertEqual(dashboard.load_squad_targets(), {})
                # Queue length cap.
                for i in range(dashboard._SQUAD_TARGET_LIMIT):
                    self.assertTrue(dashboard.add_squad_target("attack", i, 0)["ok"])
                self.assertFalse(dashboard.add_squad_target("attack", 999, 0)["ok"])


class CoreDriftLeashTests(unittest.TestCase):
    """Core-movement regressions from the remote battle log: the base chased
    far-flung explorers 2800+ cells with no restoring force, and one
    CORE_ALREADY_MOVING warn row landed every Tick because START_MOVE was
    re-issued while a multi-Tick migration was still in progress."""

    def _run_choose_actions(
        self, *, core_pos, worker_pos, core_state, config, anchor=None,
    ):
        """One Tick with a single worker pulling the Core; returns
        (core_action, issued_start_moves, anchor_after)."""
        moves: list = []
        core = SimpleNamespace(
            id="core", position=core_pos, hp=5, shield=10,
            view=SimpleNamespace(state=core_state),
            spawn=lambda unit_type: None,
            heal=lambda: None,
            repair_shield=lambda: None,
            pickup_beacon=lambda: None,
            start_move=lambda direction: moves.append(direction),
            wait=lambda: None,
        )
        worker = SimpleNamespace(
            id="worker-1", unit_type=UnitType.WORKER, position=worker_pos,
            cargo=0, direction=None,
        )
        worker.move = lambda direction: None
        worker.wait = lambda: None
        beacon = SimpleNamespace(
            position=None, status=SimpleNamespace(name="GROUND"),
        )
        turn = SimpleNamespace(
            tick=1,
            units=(worker,),
            workers=(worker,),
            vanguards=(),
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
        # Targets exactly met so production never grabs the Core action.
        config["target_workers"] = 1
        config["target_vanguards"] = 0
        config["target_rangers"] = 0
        prev_anchor = tactic._core_anchor
        prev_names = dict(tactic._object_names)
        prev_counters = dict(tactic._object_name_counters)
        tactic._core_anchor = anchor
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            tactic._object_names.clear()
            tactic._object_name_counters.clear()
            with patch.object(tactic, "load_config", return_value=config), \
                 patch.object(tactic, "MAP_MEMORY_PATH", temp / "map_memory.json"), \
                 patch.object(tactic, "WAYPOINTS_PATH", temp / "waypoints.json"), \
                 patch.object(tactic, "SELF_DESTRUCT_PATH", temp / "self_destruct.json"), \
                 patch.object(tactic, "BATTLE_LOG_PATH", temp / "battle_log.jsonl"), \
                 patch.object(tactic, "CONFIG_PATH", temp / "tactic_config.json"):
                tactic._map_dirty = False
                try:
                    core_action, _ = tactic.choose_actions(turn)
                    anchor_after = tactic._core_anchor
                finally:
                    tactic._core_anchor = prev_anchor
                    tactic._object_names.clear()
                    tactic._object_names.update(prev_names)
                    tactic._object_name_counters.clear()
                    tactic._object_name_counters.update(prev_counters)
        return core_action, moves, anchor_after

    def test_start_move_not_reissued_while_core_is_moving(self) -> None:
        config = default_config()
        config["core_max_drift"] = 0  # isolate from the leash
        action, moves, _ = self._run_choose_actions(
            core_pos=(0, 0), worker_pos=(500, 0),
            core_state=CoreState.MOVING, config=config,
        )
        self.assertEqual(moves, [])
        self.assertEqual(action, "WAIT")

    def test_start_move_still_issued_when_core_is_normal(self) -> None:
        action, moves, _ = self._run_choose_actions(
            core_pos=(0, 0), worker_pos=(500, 0),
            core_state=CoreState.NORMAL, config=default_config(),
        )
        self.assertEqual(moves, [Direction.RIGHT])
        self.assertEqual(action, "MOVE_RIGHT")

    def test_leash_blocks_step_beyond_anchor(self) -> None:
        config = default_config()
        config["core_max_drift"] = 2
        action, moves, anchor_after = self._run_choose_actions(
            core_pos=(2, 0), worker_pos=(500, 0),
            core_state=CoreState.NORMAL, config=config, anchor=(0, 0),
        )
        self.assertEqual(moves, [])
        self.assertEqual(action, "WAIT")
        self.assertEqual(anchor_after, (0, 0))

    def test_leash_allows_step_toward_anchor(self) -> None:
        config = default_config()
        config["core_max_drift"] = 2
        action, moves, _ = self._run_choose_actions(
            core_pos=(2, 0), worker_pos=(-500, 0),
            core_state=CoreState.NORMAL, config=config, anchor=(0, 0),
        )
        self.assertEqual(moves, [Direction.LEFT])
        self.assertEqual(action, "MOVE_LEFT")

    def test_anchor_initialized_on_first_moving_tick(self) -> None:
        config = default_config()
        config["core_max_drift"] = 2
        _, _, anchor_after = self._run_choose_actions(
            core_pos=(7, -3), worker_pos=(500, 0),
            core_state=CoreState.NORMAL, config=config, anchor=None,
        )
        self.assertEqual(anchor_after, (7, -3))

    def test_core_far_outside_leash_reanchors_instead_of_walking_home(self) -> None:
        # A respawn/teleport (or a legacy long migration) may land the Core far
        # beyond the leash; re-anchor there instead of marching thousands of
        # cells back toward the stale anchor.
        config = default_config()
        config["core_max_drift"] = 2
        action, moves, anchor_after = self._run_choose_actions(
            core_pos=(100, 0), worker_pos=(600, 0),
            core_state=CoreState.NORMAL, config=config, anchor=(0, 0),
        )
        self.assertEqual(anchor_after, (100, 0))
        self.assertEqual(moves, [Direction.RIGHT])

    def test_manual_core_target_bypasses_the_leash(self) -> None:
        config = default_config()
        config["core_max_drift"] = 2
        config["core_target_enabled"] = True
        config["core_target_x"] = 500
        config["core_target_y"] = 0
        action, moves, _ = self._run_choose_actions(
            core_pos=(2, 0), worker_pos=(-500, 0),
            core_state=CoreState.NORMAL, config=config, anchor=(0, 0),
        )
        self.assertEqual(moves, [Direction.RIGHT])
        self.assertEqual(action, "MOVE_RIGHT")

    def test_core_anchor_round_trips_through_map_memory(self) -> None:
        prev = (
            tactic._core_anchor, tactic._map_dirty,
            set(tactic._resource_memory), set(tactic._obstacle_memory),
            set(tactic._enemy_memory), tactic._enemy_clear_seq,
            tactic._known_obstacles,
        )
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "map_memory.json"
                path.write_text(json.dumps({
                    "obstacles": [], "resources": [], "manual_resources": [],
                    "forgotten_resources": [], "enemy_sightings": [],
                    "enemy_clear_seq": 0, "core_anchor": [7, -9],
                }), encoding="utf-8")
                with patch.object(tactic, "MAP_MEMORY_PATH", path):
                    tactic._load_map_memory()
                    self.assertEqual(tactic._core_anchor, (7, -9))
                    tactic._core_anchor = (1, 2)
                    tactic._map_dirty = True
                    tactic._save_map_memory(force=True)
                saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["core_anchor"], [1, 2])
        finally:
            (tactic._core_anchor, tactic._map_dirty) = prev[:2]
            tactic._resource_memory.clear()
            tactic._resource_memory.update(prev[2])
            tactic._obstacle_memory.clear()
            tactic._obstacle_memory.update(prev[3])
            tactic._enemy_memory.clear()
            tactic._enemy_memory.update(prev[4])
            tactic._enemy_clear_seq = prev[5]
            tactic._known_obstacles = prev[6]


class CoreAxisDegenerateTargetTests(unittest.TestCase):
    """Regression: a core target sharing the core's row/column yields a single
    heading; when that cell is walled the candidate list empties and the Core
    waits forever (silent stall — no warn rows). The perpendicular axis must be
    offered as sidestep candidates so the Core can route around the wall."""

    GLOBALS = (
        "_dead_obstacles", "_dead_open_count", "_dead_set", "_dead_view",
        "_dead_structure_built", "_known_obstacles", "_obstacle_memory",
        "_dead_end_cache_key", "_dead_end_cache", "_path_blockers_union_key",
        "_path_blockers_union", "_core_anchor", "_core_pending_move",
        "_core_move_hold_until_tick", "_map_dirty", "_object_names",
        "_object_name_counters",
    )

    def setUp(self) -> None:
        # Copy at snapshot time: these globals are mutated in place by the
        # code under test, so a reference snapshot would capture the pollution.
        self._snap = {name: copy.copy(getattr(tactic, name))
                      for name in self.GLOBALS}

    def tearDown(self) -> None:
        for name, val in self._snap.items():
            setattr(tactic, name, copy.copy(val))

    def _run(self, *, core_pos, target, walls=(), tick=1):
        moves: list = []
        core = SimpleNamespace(
            id="core", position=core_pos, hp=5, shield=10,
            view=SimpleNamespace(state=CoreState.NORMAL),
            spawn=lambda unit_type: None,
            heal=lambda: None,
            repair_shield=lambda: None,
            pickup_beacon=lambda: None,
            start_move=lambda direction: moves.append(direction),
            wait=lambda: None,
        )
        worker = SimpleNamespace(
            id="worker-1", unit_type=UnitType.WORKER, position=(-500, -500),
            cargo=0, direction=None,
        )
        worker.move = lambda direction: None
        worker.wait = lambda: None
        beacon = SimpleNamespace(
            position=None, status=SimpleNamespace(name="GROUND"),
        )
        turn = SimpleNamespace(
            tick=tick,
            units=(worker,),
            workers=(worker,),
            vanguards=(),
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
        config = default_config()
        config["target_workers"] = 1
        config["target_vanguards"] = 0
        config["target_rangers"] = 0
        config["core_max_drift"] = 0
        config["core_target_enabled"] = True
        config["core_target_x"] = target[0]
        config["core_target_y"] = target[1]
        # Reset the persistent dead-end structure to cover exactly `walls`.
        tactic._obstacle_memory = set(walls)
        tactic._known_obstacles = frozenset()
        tactic._reset_dead_structure(frozenset())
        tactic._dead_end_cache_key = None
        tactic._dead_end_cache = frozenset()
        tactic._path_blockers_union_key = None
        tactic._path_blockers_union = frozenset()
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        tactic._core_anchor = None
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            with patch.object(tactic, "load_config", return_value=config),                  patch.object(tactic, "MAP_MEMORY_PATH", temp / "map_memory.json"),                  patch.object(tactic, "WAYPOINTS_PATH", temp / "waypoints.json"),                  patch.object(tactic, "SELF_DESTRUCT_PATH", temp / "self_destruct.json"),                  patch.object(tactic, "BATTLE_LOG_PATH", temp / "battle_log.jsonl"),                  patch.object(tactic, "CONFIG_PATH", temp / "tactic_config.json"):
                tactic._map_dirty = False
                core_action, _ = tactic.choose_actions(turn)
        return core_action, moves

    def test_row_aligned_target_with_walled_east_cell_routes_via_ast(self) -> None:
        # Target due east, east neighbour walled: the Core takes the first step
        # of the real A* detour (cached) instead of jittering perpendicular.
        action, moves = self._run(
            core_pos=(0, 0), target=(500, 0), walls={(1, 0)},
        )
        self.assertEqual(len(moves), 1)
        self.assertNotEqual(moves[0], Direction.RIGHT)
        self.assertEqual(action, f"MOVE_{moves[0].name}")
        cpath = tactic._core_target_path_cache.get("path") or []
        # Loose bound: near-optimal route past the wall (dead-end closure may
        # stretch the optimum by a few cells; exact length is tie-break).
        self.assertLessEqual(len(cpath), 510)
        self.assertNotIn((1, 0), cpath)  # route avoids the wall
        self.assertEqual(moves[0].delta, (cpath[1][0], cpath[1][1]))

    def test_column_aligned_target_with_walled_south_cell_sidesteps(self) -> None:
        action, moves = self._run(
            core_pos=(0, 0), target=(0, 500), walls={(0, 1)},
        )
        self.assertEqual(moves, [Direction.RIGHT])
        self.assertEqual(action, "MOVE_RIGHT")

    def test_free_straight_heading_is_unchanged(self) -> None:
        # No wall on the direct heading: behaviour identical to before.
        action, moves = self._run(core_pos=(0, 0), target=(500, 0))
        self.assertEqual(moves, [Direction.RIGHT])
        self.assertEqual(action, "MOVE_RIGHT")

    def test_perpendicular_candidates_walled_routes_around_via_ast(self) -> None:
        # East walled AND both perpendicular cells walled: the local box no
        # longer traps the Core — A* routes around the wall cluster entirely
        # (the old greedy behaviour waited forever in this spot).
        action, moves = self._run(
            core_pos=(0, 0), target=(500, 0),
            walls={(1, 0), (0, 1), (0, -1)},
        )
        self.assertEqual(len(moves), 1)
        self.assertNotIn(moves[0], (Direction.RIGHT, Direction.DOWN, Direction.UP))
        cpath = tactic._core_target_path_cache.get("path") or []
        self.assertTrue(cpath)
        self.assertFalse(set(cpath) & {(1, 0), (0, 1), (0, -1)})
        self.assertEqual(moves[0].delta, (cpath[1][0], cpath[1][1]))


class CoreTerrainBlockedLearningTests(unittest.TestCase):
    """Regression from the remote battle log: a core step rejected with
    CORE_DESTINATION_TERRAIN_BLOCKED was re-issued every Tick (4900+ identical
    warn rows) because the blocked destination never reached obstacle memory.
    The rejected cell must be learned once and the heading must move on."""

    GLOBALS = (
        "_dead_obstacles", "_dead_open_count", "_dead_set", "_dead_view",
        "_dead_structure_built", "_known_obstacles", "_obstacle_memory",
        "_dead_end_cache_key", "_dead_end_cache", "_path_blockers_union_key",
        "_path_blockers_union", "_core_anchor", "_core_pending_move",
        "_core_move_hold_until_tick", "_map_dirty", "_object_names",
        "_object_name_counters",
    )

    def setUp(self) -> None:
        self._snap = {name: copy.copy(getattr(tactic, name))
                      for name in self.GLOBALS}

    def tearDown(self) -> None:
        for name, val in self._snap.items():
            # Shallow-copy mutable snapshots: several globals (obstacle memory,
            # name registries, path caches) are mutated in place by the code
            # under test, so restoring the original reference would be a no-op.
            setattr(tactic, name, copy.copy(val))

    def _run(self, *, core_pos, worker_pos, tick, events=(), config=None,
             anchor=None, extra_workers=()):
        """One Tick; returns (core_action, issued_start_moves, details)."""
        moves: list = []
        core = SimpleNamespace(
            id="core", position=core_pos, hp=5, shield=10,
            view=SimpleNamespace(state=CoreState.NORMAL),
            spawn=lambda unit_type: None,
            heal=lambda: None,
            repair_shield=lambda: None,
            pickup_beacon=lambda: None,
            start_move=lambda direction: moves.append(direction),
            wait=lambda: None,
        )
        worker = SimpleNamespace(
            id="worker-1", unit_type=UnitType.WORKER, position=worker_pos,
            cargo=0, direction=None,
        )
        worker.move = lambda direction: None
        worker.wait = lambda: None
        workers = [worker]
        for index, position in enumerate(extra_workers, start=2):
            extra = SimpleNamespace(
                id=f"worker-{index}", unit_type=UnitType.WORKER,
                position=position, cargo=0, direction=None,
            )
            extra.move = lambda direction: None
            extra.wait = lambda: None
            workers.append(extra)
        beacon = SimpleNamespace(
            position=None, status=SimpleNamespace(name="GROUND"),
        )
        turn = SimpleNamespace(
            tick=tick,
            units=tuple(workers),
            workers=tuple(workers),
            vanguards=(),
            rangers=(),
            visible_enemies=(),
            core=core,
            resources=50,
            resource_cells=frozenset(),
            resource_space=0,
            beacon=beacon,
            state=SimpleNamespace(population=8),
            events=tuple(events),
            obstacle_cells=frozenset(),
        )
        config = config or default_config()
        config["target_workers"] = 1
        config["target_vanguards"] = 0
        config["target_rangers"] = 0
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        tactic._core_anchor = anchor
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            with patch.object(tactic, "load_config", return_value=config), \
                 patch.object(tactic, "MAP_MEMORY_PATH", temp / "map_memory.json"), \
                 patch.object(tactic, "WAYPOINTS_PATH", temp / "waypoints.json"), \
                 patch.object(tactic, "SELF_DESTRUCT_PATH", temp / "self_destruct.json"), \
                 patch.object(tactic, "BATTLE_LOG_PATH", temp / "battle_log.jsonl"), \
                 patch.object(tactic, "CONFIG_PATH", temp / "tactic_config.json"):
                tactic._map_dirty = False
                core_action, details = tactic.choose_actions(turn)
        return core_action, moves, details

    def test_blocked_destination_is_learned_and_skipped(self) -> None:
        config = default_config()
        config["core_max_drift"] = 0
        self._run(
            core_pos=(0, 0), worker_pos=(500, 0), tick=1, config=config,
        )
        self.assertEqual(tactic._core_pending_move, ((0, 0), Direction.RIGHT, 1))

        event = SimpleNamespace(
            event_type="CORE_MOVE_FAILED",
            reason_code="CORE_DESTINATION_TERRAIN_BLOCKED",
            actor_id="core", target_id=None, position=(1, 0), values={},
        )
        action, moves, _ = self._run(
            core_pos=(0, 0), worker_pos=(500, 0), tick=2, events=[event],
            config=config,
        )
        self.assertIn((1, 0), tactic._obstacle_memory)
        # Learned wall removes the straight heading; the axis-degenerate
        # fallback must sidestep (perpendicular axis) instead of stalling.
        self.assertEqual(moves, [Direction.DOWN])
        self.assertEqual(action, "MOVE_DOWN")

    def test_blocked_destination_derived_from_pending_move(self) -> None:
        # Server reports the failure on the core's own cell instead of the
        # destination: the remembered heading (pos + delta) must be learned.
        config = default_config()
        config["core_max_drift"] = 0
        self._run(core_pos=(0, 0), worker_pos=(500, 0), tick=1, config=config)

        event = SimpleNamespace(
            event_type="CORE_MOVE_START_FAILED",
            reason_code="CORE_DESTINATION_TERRAIN_BLOCKED",
            actor_id="core", target_id=None, position=(0, 0), values={},
        )
        action, moves, _ = self._run(
            core_pos=(0, 0), worker_pos=(500, 0), tick=2, events=[event],
            config=config,
        )
        self.assertIn((1, 0), tactic._obstacle_memory)
        self.assertEqual(moves, [Direction.DOWN])
        self.assertEqual(action, "MOVE_DOWN")

    def test_unknown_destination_without_fresh_pending_is_not_learned(self) -> None:
        # No prior step this Tick-1 and the event cell is not adjacent to the
        # core: nothing may be guessed into obstacle memory.
        config = default_config()
        config["core_max_drift"] = 0
        event = SimpleNamespace(
            event_type="CORE_MOVE_FAILED",
            reason_code="CORE_DESTINATION_TERRAIN_BLOCKED",
            actor_id="core", target_id=None, position=(0, 0), values={},
        )
        action, moves, _ = self._run(
            core_pos=(0, 0), worker_pos=(500, 0), tick=7, events=[event],
            config=config,
        )
        self.assertNotIn((1, 0), tactic._obstacle_memory)
        self.assertEqual(moves, [Direction.RIGHT])

    def test_already_moving_event_engages_short_cooldown(self) -> None:
        config = default_config()
        config["core_max_drift"] = 0
        event = SimpleNamespace(
            event_type="CORE_MOVE_FAILED",
            reason_code="CORE_ALREADY_MOVING",
            actor_id="core", target_id=None, position=(0, 0), values={},
        )
        action, moves, _ = self._run(
            core_pos=(0, 0), worker_pos=(500, 0), tick=1, events=[event],
            config=config,
        )
        self.assertEqual(tactic._core_move_hold_until_tick, 3)
        self.assertEqual(moves, [])
        self.assertEqual(action, "WAIT")

        action, moves, _ = self._run(
            core_pos=(0, 0), worker_pos=(500, 0), tick=2, config=config,
        )
        self.assertEqual(moves, [])

        action, moves, _ = self._run(
            core_pos=(0, 0), worker_pos=(500, 0), tick=3, config=config,
        )
        self.assertEqual(moves, [Direction.RIGHT])
        self.assertEqual(action, "MOVE_RIGHT")

    def test_blocked_resource_cell_is_never_learned(self) -> None:
        # A protocol quirk must not wall off a remembered mine permanently.
        config = default_config()
        config["core_max_drift"] = 0
        original_resources = set(tactic._resource_memory)
        tactic._resource_memory.add((1, 0))
        try:
            event = SimpleNamespace(
                event_type="CORE_MOVE_FAILED",
                reason_code="CORE_DESTINATION_TERRAIN_BLOCKED",
                actor_id="core", target_id=None, position=(1, 0), values={},
            )
            action, moves, _ = self._run(
                core_pos=(0, 0), worker_pos=(500, 0), tick=2, events=[event],
                config=config,
            )
            self.assertNotIn((1, 0), tactic._obstacle_memory)
            self.assertEqual(moves, [Direction.RIGHT])
        finally:
            tactic._resource_memory.clear()
            tactic._resource_memory.update(original_resources)


class WorkerContestedBackoffTests(unittest.TestCase):
    """Two workers whose deterministic plans submit the same cell every Tick
    cancel both moves every Tick (MOVE_CONTESTED). After two identical
    (pos, destination) rejections a worker must sit one Tick out."""

    GLOBALS = (
        "_dead_obstacles", "_dead_open_count", "_dead_set", "_dead_view",
        "_dead_structure_built", "_known_obstacles", "_obstacle_memory",
        "_dead_end_cache_key", "_dead_end_cache", "_path_blockers_union_key",
        "_path_blockers_union", "_core_anchor", "_worker_contest_streak",
        "_worker_contest_repeat", "_worker_contest_blocked",
        "_worker_planned_step",
        "_cell_limit_streak", "_object_names", "_object_name_counters",
        "_worker_path_cache", "_combat_path_cache", "_worker_last_pos",
        "_worker_recent", "_resource_assignments",
        "_worker_explore_history", "_worker_rally_home",
    )

    def setUp(self) -> None:
        self._snap = {name: copy.copy(getattr(tactic, name))
                      for name in self.GLOBALS}

    def tearDown(self) -> None:
        for name, val in self._snap.items():
            # Shallow-copy mutable snapshots: several globals (obstacle memory,
            # name registries, path caches) are mutated in place by the code
            # under test, so restoring the original reference would be a no-op.
            setattr(tactic, name, copy.copy(val))

    def _run(self, *, tick, events=(), worker_id="worker-1", position=(5, 0)):
        """One Tick with one cargo worker heading home; returns details."""
        core = SimpleNamespace(
            id="core", position=(0, 0), hp=5, shield=10,
            view=SimpleNamespace(state=CoreState.NORMAL),
            spawn=lambda unit_type: None,
            heal=lambda: None,
            repair_shield=lambda: None,
            pickup_beacon=lambda: None,
            start_move=lambda direction: None,
            wait=lambda: None,
        )
        worker = SimpleNamespace(
            id=worker_id, unit_type=UnitType.WORKER, position=position,
            cargo=1, direction=None,
        )
        worker.move = lambda direction: None
        worker.wait = lambda: None
        beacon = SimpleNamespace(
            position=None, status=SimpleNamespace(name="GROUND"),
        )
        turn = SimpleNamespace(
            tick=tick,
            units=(worker,),
            workers=(worker,),
            vanguards=(),
            rangers=(),
            visible_enemies=(),
            core=core,
            resources=50,
            resource_cells=frozenset(),
            resource_space=3,
            beacon=beacon,
            state=SimpleNamespace(population=8),
            events=tuple(events),
            obstacle_cells=frozenset(),
        )
        config = default_config()
        config["target_workers"] = 1
        config["target_vanguards"] = 0
        config["target_rangers"] = 0
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            with patch.object(tactic, "load_config", return_value=config), \
                 patch.object(tactic, "MAP_MEMORY_PATH", temp / "map_memory.json"), \
                 patch.object(tactic, "WAYPOINTS_PATH", temp / "waypoints.json"), \
                 patch.object(tactic, "SELF_DESTRUCT_PATH", temp / "self_destruct.json"), \
                 patch.object(tactic, "BATTLE_LOG_PATH", temp / "battle_log.jsonl"), \
                 patch.object(tactic, "CONFIG_PATH", temp / "tactic_config.json"):
                tactic._map_dirty = False
                _, details = tactic.choose_actions(turn)
        return details

    @staticmethod
    def _contested_event(actor_id="worker-1", position=(4, 0)):
        return SimpleNamespace(
            event_type="UNIT_MOVE_FAILED",
            reason_code="MOVE_CONTESTED",
            actor_id=actor_id, target_id=None, position=position, values={},
        )

    def test_repeat_contested_rejection_backs_off_one_tick(self) -> None:
        # Tick 1: first contested rejection -> streak 1 -> normal planning.
        details = self._run(tick=1, events=[self._contested_event()])
        self.assertEqual(tactic._worker_contest_streak["worker-1"][2], 1)
        self.assertTrue(
            any(v.startswith("MOVE") for v in details.values()), details,
        )

        # Tick 2: same cell, same destination -> streak 2 -> backoff wait.
        details = self._run(tick=2, events=[self._contested_event()])
        self.assertEqual(tactic._worker_contest_streak["worker-1"][2], 2)
        self.assertIn("worker-1", details)
        self.assertTrue(
            details["worker-1"].startswith("WAIT:contested-backoff"), details,
        )

        # Tick 3: no fresh rejection -> streak resets -> normal planning.
        details = self._run(tick=3)
        self.assertEqual(tactic._worker_contest_streak, {})
        self.assertTrue(
            any(v.startswith("MOVE") for v in details.values()), details,
        )

    def test_non_worker_actors_never_enter_the_worker_tracker(self) -> None:
        event = SimpleNamespace(
            event_type="UNIT_MOVE_FAILED",
            reason_code="MOVE_CONTESTED",
            actor_id="vanguard-9", target_id=None, position=(4, 0), values={},
        )
        self._run(tick=1, events=[event])
        self.assertEqual(tactic._worker_contest_streak, {})

    def test_backoff_survives_uuid_sized_ids(self) -> None:
        """The tracker is keyed by the FULL unit id the events carry.

        Field: the plan loop looks units up by ``uid = str(unit.id)[:8]`` while
        the rebuild wrote the streak under ``str(event.actor_id)`` (a full UUID),
        so the lookup missed on every real unit and contested-backoff never fired
        once in 44MB of production log. The test double's id ("worker-1") is
        short enough that ``[:8]`` was a no-op, which is exactly why the suite
        stayed green while the breaker was dead.
        """
        uid = "49797bf4-8ab1-4a51-9c0d-000000000005"
        short = uid[:8]

        details = self._run(
            tick=1, events=[self._contested_event(uid)], worker_id=uid,
        )
        self.assertEqual(tactic._worker_contest_streak[uid][2], 1)
        self.assertTrue(details[short].startswith("MOVE"), details)

        details = self._run(
            tick=2, events=[self._contested_event(uid)], worker_id=uid,
        )
        self.assertEqual(tactic._worker_contest_streak[uid][2], 2)
        self.assertTrue(
            details[short].startswith("WAIT:contested-backoff"), details,
        )

    def test_persistent_refusal_parks_the_rejected_destination(self) -> None:
        """A worker that keeps losing the same cell has to route around it.

        Observed: W5 (49797bf4) issued the same MOVE:RIGHT out of (279,-424) for
        118 straight Ticks, 57 of the last 60 warn rows, because the claimant of
        that neighbour is invisible to us and one-Tick backoffs cannot resolve a
        fight with something that never moves. The waiting Tick emits no
        rejection at all, so the tally is kept per origin instead of rebuilt.
        """
        seen: list = []
        for tick in range(1, tactic._WORKER_CONTEST_BLOCK_AFTER + 3):
            details = self._run(tick=tick, events=[self._contested_event()])
            seen.append((
                tick,
                details["worker-1"],
                tactic._worker_contest_blocked.get("worker-1", (None, None))[0],
                tactic._worker_planned_step.get("worker-1", (None, None))[1],
            ))

        blocked_cell, until = tactic._worker_contest_blocked["worker-1"]
        self.assertEqual(blocked_cell, (4, 0))
        self.assertGreater(until, tactic.turn_context.tick)
        # (4,0) is the straight line to the Core at (0,0): with it parked the
        # plan must step somewhere else rather than re-issuing the refused move.
        self.assertTrue(details["worker-1"].startswith("MOVE"), details)
        self.assertNotEqual(
            tactic._worker_planned_step["worker-1"][1], (4, 0),
        )
        # The park must be sticky. Re-pointing it at each Tick's fresh plan
        # destination un-parks the cell the escape just walked into, so the
        # carrier flips between the two refused steps forever instead of
        # routing around the claimant.
        parked = [(tick, plan, blocked, dest) for tick, plan, blocked, dest in seen
                  if blocked is not None]
        self.assertTrue(parked, seen)
        self.assertEqual({blocked for _t, _p, blocked, _d in parked}, {(4, 0)}, seen)
        for _tick, plan, _blocked, dest in parked:
            self.assertNotEqual(dest, (4, 0), seen)
            self.assertTrue(plan.startswith("MOVE"), seen)

    def test_the_chute_cell_is_never_parked(self) -> None:
        """Parking the Core cell would starve the vault, not dodge a peer.

        A carrier refused on the Core cell is refused on the deposit path
        itself — the only way in is in. Parking it for
        _WORKER_CONTEST_BLOCK_TICKS would freeze that carrier's deliveries, the
        very economy stall the whole breaker was written to prevent.
        """
        for tick in range(1, tactic._WORKER_CONTEST_BLOCK_AFTER + 3):
            self._run(
                tick=tick,
                events=[self._contested_event(position=(1, 0))],
                position=(1, 0),
            )
        self.assertNotIn("worker-1", tactic._worker_contest_blocked)
        self.assertGreaterEqual(
            tactic._worker_contest_repeat["worker-1"][1],
            tactic._WORKER_CONTEST_BLOCK_AFTER,
            "the tally has to cross the threshold for this to prove anything",
        )

    def test_block_is_dropped_when_the_worker_leaves_that_origin(self) -> None:
        for tick in range(1, tactic._WORKER_CONTEST_BLOCK_AFTER + 2):
            self._run(tick=tick, events=[self._contested_event()])
        self.assertIn("worker-1", tactic._worker_contest_repeat)
        # No further refusals and the worker standing elsewhere: tally is gone.
        self._run(tick=tactic._WORKER_CONTEST_BLOCK_AFTER + 3, worker_id="worker-1",
                  position=(9, 9))
        self.assertEqual(tactic._worker_contest_repeat, {})


class CapacityAwareCombatMoveTests(unittest.TestCase):
    """Home/heal/attack planners must not steer into a cell the server will
    reject as CELL_UNIT_LIMIT: _move_towards callers now pass cell_counts."""

    def setUp(self) -> None:
        self._snap = {name: copy.copy(getattr(tactic, name)) for name in (
            "_combat_path_cache", "_worker_last_pos",
        )}

    def tearDown(self) -> None:
        for name, val in self._snap.items():
            # Shallow-copy mutable snapshots: several globals (obstacle memory,
            # name registries, path caches) are mutated in place by the code
            # under test, so restoring the original reference would be a no-op.
            setattr(tactic, name, copy.copy(val))

    def _unit(self, moves: list):
        unit = SimpleNamespace(id="u1", position=(0, 0))
        unit.move = lambda direction: moves.append(direction)
        return unit

    def test_packed_next_cell_blocks_a_straight_corridor(self) -> None:
        # Goal due east through a packed cell with no side street: no move at
        # all beats re-issuing the doomed step every Tick.
        moves: list = []
        result = tactic._move_towards(
            self._unit(moves),
            (0, 0),
            (2, 0),
            frozenset(),
            detail_prefix="test",
            cell_counts={(1, 0): tactic._CELL_UNIT_LIMIT},
        )
        self.assertIsNone(result)
        self.assertEqual(moves, [])

    def test_packed_next_cell_reroutes_to_a_free_axis(self) -> None:
        # Goal (2,2) with (1,0) packed: the planner must step DOWN (the free
        # axis) instead of pushing into the packed cell.
        moves: list = []
        result = tactic._move_towards(
            self._unit(moves),
            (0, 0),
            (2, 2),
            frozenset(),
            detail_prefix="test",
            cell_counts={(1, 0): tactic._CELL_UNIT_LIMIT},
        )
        self.assertIsNotNone(result)
        self.assertEqual(moves, [Direction.DOWN])

    def test_unpacked_corridor_still_moves(self) -> None:
        moves: list = []
        result = tactic._move_towards(
            self._unit(moves),
            (0, 0),
            (2, 0),
            frozenset(),
            detail_prefix="test",
            cell_counts={(1, 0): 1},
        )
        self.assertIsNotNone(result)
        self.assertEqual(moves, [Direction.RIGHT])


class AttackRegroupTests(unittest.TestCase):
    """Regression: attack-team units scattered beyond the A* expansion budget
    (observed 11k cells) sat at WAIT:attack-blocked forever with no warn row
    and never healed. On a failed march the unit must switch to home-squad
    recovery: a distance-independent greedy march home, healing on arrival."""

    GLOBALS = (
        "_dead_obstacles", "_dead_open_count", "_dead_set", "_dead_view",
        "_dead_structure_built", "_known_obstacles", "_obstacle_memory",
        "_dead_end_cache_key", "_dead_end_cache", "_path_blockers_union_key",
        "_path_blockers_union", "_combat_path_cache", "_worker_last_pos",
        "_object_names", "_object_name_counters", "_attack_regroup_ids",
        "_chute_in_demand",
    )

    def setUp(self) -> None:
        self._snap = {name: copy.copy(getattr(tactic, name))
                      for name in self.GLOBALS}
        # Reset the persistent dead-end structure to an empty map.
        tactic._obstacle_memory = set()
        tactic._known_obstacles = frozenset()
        tactic._reset_dead_structure(frozenset())
        tactic._dead_end_cache_key = None
        tactic._dead_end_cache = frozenset()
        tactic._path_blockers_union_key = None
        tactic._path_blockers_union = frozenset()

    def tearDown(self) -> None:
        for name, val in self._snap.items():
            setattr(tactic, name, copy.copy(val))

    def _unit(self, pos, moves, waits):
        unit = SimpleNamespace(id="u1", position=pos)
        unit.move = lambda direction: moves.append(direction)
        unit.wait = lambda: waits.append(True)
        return unit

    def _config(self, target):
        config = default_config()
        config["attack_mode"] = "coords"
        config["attack_target_x"] = target[0]
        config["attack_target_y"] = target[1]
        return config

    def test_blocked_march_far_from_core_regroups_home(self) -> None:
        moves: list = []
        waits: list = []
        unit = self._unit((5000, -6000), moves, waits)
        with patch.object(tactic, "_move_towards", return_value=None):
            action, detail = tactic._plan_attack_combat(
                unit,
                unit_kind="ranger",
                enemies=(),
                obstacle_cells=frozenset(),
                config=self._config((-337, -800)),
                core_pos=(-245, -239),
            )
        self.assertEqual(moves, [Direction.DOWN])
        self.assertEqual(waits, [])
        self.assertIn("attack-regroup", detail)

    def test_blocked_march_at_core_keeps_waiting(self) -> None:
        moves: list = []
        waits: list = []
        unit = self._unit((-245, -239), moves, waits)
        with patch.object(tactic, "_move_towards", return_value=None):
            action, detail = tactic._plan_attack_combat(
                unit,
                unit_kind="ranger",
                enemies=(),
                obstacle_cells=frozenset(),
                config=self._config((-337, -800)),
                core_pos=(-245, -239),
            )
        self.assertEqual(moves, [])
        self.assertEqual(waits, [True])
        self.assertIn("attack-blocked", detail)

    def test_long_march_step_picks_best_of_four_directions(self) -> None:
        # Straight-ahead cell walled, diagonal goal: the other distance-
        # reducing axis must be picked even though it is not the primary.
        moves: list = []
        unit = self._unit((0, 0), moves, [])
        result = tactic._long_march_step(
            unit, (0, 0), (5, 10), frozenset({(0, 1)}),
            detail_prefix="test",
        )
        self.assertIsNotNone(result)
        self.assertEqual(moves, [Direction.RIGHT])

    def test_long_march_step_respects_capacity(self) -> None:
        # Both distance-reducing cells unavailable (wall + packed): the best
        # remaining step is taken over waiting (sidestep out of the pocket).
        moves: list = []
        unit = self._unit((0, 0), moves, [])
        result = tactic._long_march_step(
            unit, (0, 0), (5, 10), frozenset({(0, 1)}),
            detail_prefix="test",
            cell_counts={(1, 0): tactic._CELL_UNIT_LIMIT},
        )
        self.assertIsNotNone(result)
        self.assertEqual(moves, [Direction.UP])

    def test_long_march_step_returns_none_when_boxed(self) -> None:
        moves: list = []
        unit = self._unit((0, 0), moves, [])
        result = tactic._long_march_step(
            unit, (0, 0), (0, 10),
            frozenset({(0, 1), (1, 0), (0, -1), (-1, 0)}),
            detail_prefix="test",
        )
        self.assertIsNone(result)
        self.assertEqual(moves, [])

    def test_long_march_step_avoids_immediate_backtrack(self) -> None:
        # The unit came from the north (its only strictly-closer cell): the
        # anti-backtrack pass must steer sideways instead of bouncing.
        moves: list = []
        unit = self._unit((0, 0), moves, [])
        tactic._worker_last_pos["u1"] = (0, -1)
        result = tactic._long_march_step(
            unit, (0, 0), (0, -5), frozenset(),
            detail_prefix="test",
        )
        self.assertIsNotNone(result)
        self.assertEqual(moves, [Direction.RIGHT])

    def test_long_march_step_escapes_through_dead_end_mouth(self) -> None:
        # Unit sits inside a dead-end pocket with every non-mouth cell walled.
        # The mouth cell is flagged dead-end, but it is the only way out — the
        # escape pass must take it instead of cycling inside the pocket
        # (observed: R4 boxed in a 2x2 pocket, cycling 4 cells forever).
        moves: list = []
        unit = self._unit((0, 0), moves, [])
        walls = {(0, 1), (0, -1), (1, 1), (1, -1), (2, 1), (2, -1), (2, 0), (-1, 0)}
        tactic._obstacle_memory = set(walls)
        tactic._known_obstacles = frozenset()
        tactic._reset_dead_structure(frozenset())
        tactic._dead_end_cache_key = None
        tactic._dead_end_cache = frozenset()
        tactic._path_blockers_union_key = None
        tactic._path_blockers_union = frozenset()
        tactic._update_obstacle_memory(SimpleNamespace(obstacle_cells=walls))
        result = tactic._long_march_step(
            unit, (0, 0), (10, 0), frozenset(walls),
            detail_prefix="test",
        )
        self.assertIsNotNone(result)
        self.assertEqual(moves, [Direction.RIGHT])

    def test_long_march_step_backtracks_only_when_boxed(self) -> None:
        # Every forward/side cell walled: going back is allowed as last resort.
        moves: list = []
        unit = self._unit((0, 0), moves, [])
        tactic._worker_last_pos["u1"] = (0, -1)
        result = tactic._long_march_step(
            unit, (0, 0), (0, -5), frozenset({(1, 0), (0, 1), (-1, 0)}),
            detail_prefix="test",
        )
        self.assertIsNotNone(result)
        self.assertEqual(moves, [Direction.UP])

    def test_regroup_is_sticky_until_core_arrival(self) -> None:
        # Once the march failed and regrouping started, the TARGET march must
        # NOT be re-attempted each Tick (that A-B-A'd at wall corners); the
        # recovery march homes on the Core with a distance-scaled A* budget.
        moves: list = []
        waits: list = []
        unit = self._unit((5000, -6000), moves, waits)
        calls: list = []

        def fake_move_towards(unit_arg, pos, goal, *args, **kwargs):
            calls.append((goal, kwargs.get("max_steps")))
            return None

        config = self._config((-337, -800))
        with patch.object(tactic, "_move_towards", side_effect=fake_move_towards):
            tactic._plan_attack_combat(
                unit, unit_kind="ranger", enemies=(),
                obstacle_cells=frozenset(), config=config,
                core_pos=(-245, -239),
            )
            tactic._plan_attack_combat(
                unit, unit_kind="ranger", enemies=(),
                obstacle_cells=frozenset(), config=config,
                core_pos=(-245, -239),
            )
        self.assertEqual(calls[0][0], (-337, -800))  # first: target march
        self.assertEqual(calls[1][0], (-245, -239))  # then: regroup home
        self.assertGreater(calls[1][1], 2500)        # scaled budget, not default
        self.assertEqual(moves, [Direction.DOWN, Direction.DOWN])
        self.assertIn("u1", tactic._attack_regroup_ids)
        self.assertEqual(waits, [])

    def test_regroup_arrival_clears_flag(self) -> None:
        moves: list = []
        waits: list = []
        unit = self._unit((-245, -239), moves, waits)
        tactic._attack_regroup_ids.add("u1")
        with patch.object(tactic, "_move_towards", return_value=None):
            action, detail = tactic._plan_attack_combat(
                unit, unit_kind="ranger", enemies=(),
                obstacle_cells=frozenset(), config=self._config((-337, -800)),
                core_pos=(-245, -239),
            )
        self.assertNotIn("u1", tactic._attack_regroup_ids)
        self.assertEqual(moves, [])
        self.assertEqual(waits, [True])
        self.assertIn("attack-blocked", detail)

    def test_regroup_ring_holds_when_chute_in_demand(self) -> None:
        # Observed 1588+ Tick chute deadlock: two full-HP attack units kept
        # re-boarding the core cell from the ring (regroup target = the cell
        # itself), winning the server's cell race against the queued carrier
        # every Tick — zero deposits, spawn squatter-blocked, economy frozen.
        # With carriers hauling, a full-HP regroup unit adjacent to the Core
        # must hold at the ring instead of stepping onto the chute.
        moves: list = []
        waits: list = []
        unit = self._unit((-244, -239), moves, waits)
        unit.hp = 2  # full HP for the default max — no heal reason to board
        tactic._attack_regroup_ids.add("u1")
        tactic._chute_in_demand = True
        with patch.object(tactic, "_move_towards", return_value=None):
            action, detail = tactic._plan_attack_combat(
                unit, unit_kind="ranger", enemies=(),
                obstacle_cells=frozenset(), config=self._config((-337, -800)),
                core_pos=(-245, -239),
            )
        self.assertEqual(moves, [])
        self.assertEqual(waits, [True])
        self.assertIn("attack-regroup-ring", detail)
        # The failed march re-arms the sticky flag; next Tick re-holds cheaply.
        self.assertIn("u1", tactic._attack_regroup_ids)

    def test_wounded_regroup_still_claims_core_cell(self) -> None:
        # A wounded regroup unit adjacent to the Core keeps boarding the chute:
        # on-cell passive healing outranks the deposit queue (healer-hold).
        moves: list = []
        waits: list = []
        unit = self._unit((-244, -239), moves, waits)
        unit.hp = 1
        tactic._attack_regroup_ids.add("u1")
        tactic._chute_in_demand = True
        with patch.object(tactic, "_move_towards", return_value=None):
            action, detail = tactic._plan_attack_combat(
                unit, unit_kind="ranger", enemies=(),
                obstacle_cells=frozenset(), config=self._config((-337, -800)),
                core_pos=(-245, -239),
            )
        self.assertEqual(moves, [Direction.LEFT])
        self.assertEqual(waits, [])
        self.assertIn("attack-regroup", detail)

    def test_regroup_ring_requires_carrier_demand(self) -> None:
        # Without hauling carriers there is no chute contention: the ring hold
        # must not fire and the unit walks onto the Core cell as before.
        moves: list = []
        waits: list = []
        unit = self._unit((-244, -239), moves, waits)
        unit.hp = 2
        tactic._attack_regroup_ids.add("u1")
        tactic._chute_in_demand = False
        with patch.object(tactic, "_move_towards", return_value=None):
            action, detail = tactic._plan_attack_combat(
                unit, unit_kind="ranger", enemies=(),
                obstacle_cells=frozenset(), config=self._config((-337, -800)),
                core_pos=(-245, -239),
            )
        self.assertEqual(moves, [Direction.LEFT])
        self.assertEqual(waits, [])
        self.assertIn("attack-regroup", detail)


class CoreManualTargetAstDetourTests(unittest.TestCase):
    """Regression: with a manual core target sharing the core's row, a wall
    band spanning the perpendicular fallback made the Core jitter vertically
    forever (observed: bouncing (-225,-238)/(-225,-239), zero eastward
    progress for a whole window). The Core must route with a real A* toward
    the target when its primary heading is walled."""

    GLOBALS = (
        "_dead_obstacles", "_dead_open_count", "_dead_set", "_dead_view",
        "_dead_structure_built", "_known_obstacles", "_obstacle_memory",
        "_dead_end_cache_key", "_dead_end_cache", "_path_blockers_union_key",
        "_path_blockers_union", "_core_anchor", "_core_pending_move",
        "_core_move_hold_until_tick", "_map_dirty", "_object_names",
        "_object_name_counters", "_core_target_path_cache",
    )

    def setUp(self) -> None:
        self._snap = {name: copy.copy(getattr(tactic, name))
                      for name in self.GLOBALS}
        tactic._obstacle_memory = set()
        tactic._known_obstacles = frozenset()
        tactic._reset_dead_structure(frozenset())
        tactic._dead_end_cache_key = None
        tactic._dead_end_cache = frozenset()
        tactic._path_blockers_union_key = None
        tactic._path_blockers_union = frozenset()

    def tearDown(self) -> None:
        for name, val in self._snap.items():
            setattr(tactic, name, copy.copy(val))

    def _run(self, *, core_pos, target, walls=(), tick=1):
        moves: list = []
        core = SimpleNamespace(
            id="core", position=core_pos, hp=5, shield=10,
            view=SimpleNamespace(state=CoreState.NORMAL),
            spawn=lambda unit_type: None,
            heal=lambda: None,
            repair_shield=lambda: None,
            pickup_beacon=lambda: None,
            start_move=lambda direction: moves.append(direction),
            wait=lambda: None,
        )
        worker = SimpleNamespace(
            id="worker-1", unit_type=UnitType.WORKER, position=(-500, -500),
            cargo=0, direction=None,
        )
        worker.move = lambda direction: None
        worker.wait = lambda: None
        beacon = SimpleNamespace(
            position=None, status=SimpleNamespace(name="GROUND"),
        )
        turn = SimpleNamespace(
            tick=tick,
            units=(worker,),
            workers=(worker,),
            vanguards=(),
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
        config = default_config()
        config["target_workers"] = 1
        config["target_vanguards"] = 0
        config["target_rangers"] = 0
        config["core_max_drift"] = 0
        config["core_target_enabled"] = True
        config["core_target_x"] = target[0]
        config["core_target_y"] = target[1]
        tactic._obstacle_memory = set(walls)
        tactic._known_obstacles = frozenset(walls)
        tactic._reset_dead_structure(frozenset(walls))
        tactic._dead_end_cache_key = None
        tactic._dead_end_cache = frozenset()
        tactic._path_blockers_union_key = None
        tactic._path_blockers_union = frozenset()
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        tactic._core_anchor = None
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            with patch.object(tactic, "load_config", return_value=config),                  patch.object(tactic, "MAP_MEMORY_PATH", temp / "map_memory.json"),                  patch.object(tactic, "WAYPOINTS_PATH", temp / "waypoints.json"),                  patch.object(tactic, "SELF_DESTRUCT_PATH", temp / "self_destruct.json"),                  patch.object(tactic, "BATTLE_LOG_PATH", temp / "battle_log.jsonl"),                  patch.object(tactic, "CONFIG_PATH", temp / "tactic_config.json"):
                tactic._map_dirty = False
                core_action, _ = tactic.choose_actions(turn)
        return core_action, moves

    def test_walled_east_band_routes_via_ast_detour(self) -> None:
        # East heading walled by a vertical band: the Core must take the A*
        # detour step (not the perpendicular jitter, which would bounce
        # forever between the two cells beside the wall).
        walls = {(1, 0), (1, 1), (1, -1)}
        action, moves = self._run(core_pos=(0, 0), target=(10, 0), walls=walls)
        self.assertEqual(len(moves), 1)
        expected_path = tactic._bfs_path(
            (0, 0), (10, 0), frozenset(walls), max_steps=50_000,
        )
        self.assertIsNotNone(expected_path)
        first_step = expected_path[1]
        self.assertEqual(moves[0].delta, (first_step[0], first_step[1]))
        self.assertTrue(tactic._core_target_path_cache.get("path"))
        self.assertEqual(action, f"MOVE_{moves[0].name}")

    def test_open_east_heading_skips_ast(self) -> None:
        moves: list = []
        calls: list = []

        def fake_bfs(*args, **kwargs):
            calls.append(1)
            return None

        with patch.object(tactic, "_bfs_path", side_effect=fake_bfs):
            action, moves = self._run(core_pos=(0, 0), target=(10, 0))
        self.assertEqual(moves, [Direction.RIGHT])
        self.assertEqual(calls, [])  # straight heading needs no pathfinding


class CargoHomeAstBudgetTests(unittest.TestCase):
    """A loaded worker must get an A* budget that scales with its distance home.

    The flat ``bfs_max_steps`` (2500) was exhausted long before the Core on real
    return trips: 7 carriers 270..1083 cells out each burned exactly the cap
    every Tick, fell to the greedy step, and lapped an 8-cell box for an hour
    (6 deposits in 343 Ticks, storage 44 -> 2). Replaying those 7 positions
    against the live obstacle memory, the scaled budget found every route with a
    single search per worker and walked them all home.
    """

    GLOBALS = (
        "_dead_obstacles", "_dead_open_count", "_dead_set", "_dead_view",
        "_dead_structure_built", "_known_obstacles", "_obstacle_memory",
        "_dead_end_cache_key", "_dead_end_cache", "_path_blockers_union_key",
        "_path_blockers_union", "_worker_path_cache", "_cargo_route_miss_dist",
        "_worker_last_pos", "_worker_recent", "_resource_assignments",
        "_chute_in_demand", "_chute_vacating_this_tick",
    )

    def setUp(self) -> None:
        self._snap = {name: copy.copy(getattr(tactic, name)) for name in self.GLOBALS}
        tactic._worker_path_cache.clear()
        tactic._cargo_route_miss_dist.clear()
        tactic._worker_last_pos.clear()
        tactic._worker_recent.clear()
        tactic._resource_assignments.clear()
        tactic._chute_in_demand = False
        tactic._chute_vacating_this_tick = False

    def tearDown(self) -> None:
        for name, val in self._snap.items():
            setattr(tactic, name, copy.copy(val))

    class _Worker:
        def __init__(self, uid: str, pos, cargo: int = 1) -> None:
            self.id = uid
            self.position = pos
            self.cargo = cargo
            self.direction = None
            self.waited = False

        def move(self, direction) -> None:
            self.direction = direction

        def wait(self) -> None:
            self.waited = True

    def _plan(self, worker, *, obstacle_cells=frozenset(), resource_cells=frozenset(),
              config=None, bfs_result="real"):
        """Plan one Tick for ``worker``; return (action, detail, budgets).

        ``budgets`` records the ``max_steps`` of every map search the planner ran,
        which is what these regression tests assert on.
        """
        budgets: list[int] = []
        real_bfs = tactic._bfs_path

        def spy(start, goal, obstacles, max_steps=2500, **kwargs):
            budgets.append(max_steps)
            if bfs_result == "real":
                return real_bfs(start, goal, obstacles, max_steps=max_steps, **kwargs)
            return None

        with patch.object(tactic, "_bfs_path", side_effect=spy):
            action, detail = tactic._plan_worker(
                worker,
                SimpleNamespace(id="core", position=(0, 0)),
                resource_cells=resource_cells,
                obstacle_cells=obstacle_cells,
                depleted=set(),
                config=config or default_config(),
            )
        return action, detail, budgets

    def test_far_carrier_searches_with_scaled_budget(self) -> None:
        worker = self._Worker("carrier-far", (400, 0))
        action, detail, budgets = self._plan(worker)
        self.assertEqual(budgets, [400 * tactic._CARGO_HOME_BUDGET_FACTOR])
        self.assertEqual(action, "MOVE")
        self.assertIn("-> (0, 0)", detail)
        self.assertEqual(worker.direction, Direction.LEFT)
        # A full cached route, not the two-cell greedy step.
        cached = tactic._worker_path_cache["carrier-far"]
        self.assertEqual(len(cached["path"]), 401)

    def test_carrier_budget_stays_capped(self) -> None:
        worker = self._Worker("carrier-huge", (6000, 0))
        _, _, budgets = self._plan(worker)
        self.assertEqual(budgets, [tactic._CARGO_HOME_BUDGET_MAX])

    def test_empty_worker_keeps_flat_budget(self) -> None:
        # The scaling is for the home march only: 25 carriers is bounded work,
        # every worker re-planning to a far mine every Tick is not.
        worker = self._Worker("miner-far", (200, 0), cargo=0)
        tactic._resource_assignments["miner-far"] = (600, 0)
        config = default_config()
        _, _, budgets = self._plan(
            worker,
            resource_cells=frozenset({(600, 0)}),
            config=config,
        )
        self.assertEqual(budgets, [config["bfs_max_steps"]])

    def test_far_miss_is_recorded_at_the_missed_distance(self) -> None:
        worker = self._Worker("carrier-blocked", (400, 0))
        action, detail, budgets = self._plan(worker, bfs_result="none")
        self.assertEqual(budgets, [400 * tactic._CARGO_HOME_BUDGET_FACTOR])
        self.assertEqual(tactic._cargo_route_miss_dist["carrier-blocked"], 400)
        # The greedy march still takes over rather than parking the carrier.
        self.assertEqual(action, "MOVE")
        self.assertIn("-> (0, 0)", detail)

    def test_far_retry_is_suppressed_until_the_carrier_closes(self) -> None:
        tactic._cargo_route_miss_dist["carrier-blocked"] = 400
        worker = self._Worker("carrier-blocked", (395, 0))
        _, _, budgets = self._plan(worker)
        self.assertEqual(budgets, [])
        self.assertEqual(tactic._cargo_route_miss_dist["carrier-blocked"], 400)

    def test_far_retry_resumes_after_real_progress(self) -> None:
        tactic._cargo_route_miss_dist["carrier-blocked"] = 400
        worker = self._Worker("carrier-blocked", (400 - tactic._CARGO_HOME_RETRY_GAP, 0))
        _, _, budgets = self._plan(worker)
        self.assertEqual(len(budgets), 1)
        self.assertNotIn("carrier-blocked", tactic._cargo_route_miss_dist)

    def test_near_home_miss_never_suppresses_the_search(self) -> None:
        # Close to the Core a missed route is usually transient congestion at
        # the unloading chute, not a wall: freezing the route would strand cargo.
        tactic._cargo_route_miss_dist["carrier-near"] = 20
        worker = self._Worker("carrier-near", (tactic._CARGO_HOME_MISS_FAR - 18, 0))
        _, _, budgets = self._plan(worker, bfs_result="none")
        self.assertEqual(len(budgets), 1)

    def test_near_home_success_clears_a_stale_miss(self) -> None:
        tactic._cargo_route_miss_dist["carrier-near"] = 20
        worker = self._Worker("carrier-near", (12, 0))
        self._plan(worker)
        self.assertNotIn("carrier-near", tactic._cargo_route_miss_dist)


class HomeChuteHysteresisTests(unittest.TestCase):
    """A defender pushed off the unloading ring must not walk straight back.

    chute-clear (yield the chute to queued carriers) and home-patrol (return to
    the assigned slot) used to take turns: ``RIGHT chute-clear`` one Tick,
    ``LEFT home-patrol`` the next, on 100% of Ticks for V8/V3 — and V3's
    re-entry alone logged 967 CELL_UNIT_LIMIT warns per 3000 Ticks.
    """

    def setUp(self) -> None:
        self._snap = {name: copy.copy(getattr(tactic, name)) for name in (
            "_object_names", "_object_name_counters", "_home_patrol_slot_cache",
            "_worker_last_pos", "_combat_path_cache", "_chute_in_demand",
            "_march_route_miss", "_march_budget_spent",
        )}
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        tactic._home_patrol_slot_cache.clear()
        tactic._worker_last_pos.clear()
        tactic._combat_path_cache.clear()
        # A refused march parks the goal in a cooldown, and that suppressed
        # search hides the ring lock in later cases within the same class.
        tactic._march_route_miss.clear()
        tactic._march_budget_spent = 0
        tactic._chute_in_demand = False

    def tearDown(self) -> None:
        for name, val in self._snap.items():
            setattr(tactic, name, copy.copy(val))

    def _plan(self, *, pos, chute_demand: bool):
        tactic._chute_in_demand = chute_demand
        unit = SimpleNamespace(
            id="vang-1", unit_type=UnitType.VANGUARD, position=pos, hp=5,
        )
        unit.move = lambda direction: None
        unit.wait = lambda: None
        config = default_config()
        config["home_team"] = "V1"
        config["home_patrol_radius"] = 1
        return tactic._plan_home_combat(
            unit,
            unit_kind="vanguard",
            enemies=(),
            obstacle_cells=frozenset(),
            core_pos=(0, 0),
            config=config,
        )

    def test_slot_on_the_chute_ring_is_held_from_outside(self) -> None:
        # The single home defender gets slot (0,-1) — one cell from the Core.
        action, detail = self._plan(pos=(2, 0), chute_demand=True)
        self.assertEqual(action, "WAIT")
        self.assertIn("home-chute-hold", detail)
        self.assertIn("(0, -1)", detail)

    def test_patrol_resumes_without_delivery_demand(self) -> None:
        action, detail = self._plan(pos=(2, 0), chute_demand=False)
        self.assertEqual(action, "MOVE")
        self.assertIn("home-patrol", detail)

    def test_demand_does_not_park_a_defender_whose_slot_is_off_the_ring(self) -> None:
        # "vanguard-6" hashes onto slot (1,-1) — distance 2 from the Core, so
        # yielding the chute is none of its business and it keeps patrolling.
        tactic._chute_in_demand = True
        unit = SimpleNamespace(
            id="vanguard-6", unit_type=UnitType.VANGUARD, position=(2, 0), hp=5,
        )
        unit.move = lambda direction: None
        unit.wait = lambda: None
        config = default_config()
        config["home_patrol_radius"] = 1
        action, detail = tactic._plan_home_combat(
            unit, unit_kind="vanguard", enemies=(), obstacle_cells=frozenset(),
            core_pos=(0, 0), config=config,
        )
        self.assertEqual(action, "MOVE", detail)
        self.assertIn("home-patrol", detail)


    def test_march_routes_around_the_ring_not_through_it(self) -> None:
        # V4's dither: standing one step outside the ring, its only open cell
        # is a ring cell, and the chute evicts it the moment it enters — so the
        # march must refuse the ring entirely instead of feeding chute-clear.
        tactic._chute_in_demand = True
        unit = SimpleNamespace(
            id="vanguard-2", unit_type=UnitType.VANGUARD, position=(0, -2), hp=5,
        )
        moved: list = []
        unit.move = lambda direction: moved.append(direction)
        unit.wait = lambda: None
        config = default_config()
        config["home_patrol_radius"] = 2
        # "vanguard-2" hashes onto slot (0,2) — straight across the Core.
        # All exits but the ring cell (0,-1) are walled.
        walls = frozenset({(1, -2), (-1, -2), (0, -3)})
        action, detail = tactic._plan_home_combat(
            unit, unit_kind="vanguard", enemies=(), obstacle_cells=walls,
            core_pos=(0, 0), config=config,
        )
        self.assertEqual(moved, [], f"defender entered the unloading ring: {action} {detail}")

    def test_march_enters_the_ring_when_no_carriers_queue(self) -> None:
        tactic._chute_in_demand = False
        unit = SimpleNamespace(
            id="vanguard-2", unit_type=UnitType.VANGUARD, position=(0, -2), hp=5,
        )
        moved: list = []
        unit.move = lambda direction: moved.append(direction)
        unit.wait = lambda: None
        config = default_config()
        config["home_patrol_radius"] = 2
        walls = frozenset({(1, -2), (-1, -2), (0, -3)})
        tactic._plan_home_combat(
            unit, unit_kind="vanguard", enemies=(), obstacle_cells=walls,
            core_pos=(0, 0), config=config,
        )
        self.assertEqual(moved, [Direction.DOWN])

    def test_scout_keeps_the_wall_identity_and_avoids_the_ring(self) -> None:
        # Performance regression guard: handing the scout a fresh
        # ``obstacle_cells | ring`` set defeats the identity-cached dead-end
        # structure and re-classifies the whole wall memory per call (measured
        # on the live memory: 631ms vs 85ms), which is what pushed the vanguard
        # planning phase from 166ms to 1012ms per Tick after the ring lock.
        tactic._chute_in_demand = True
        walls = frozenset()
        unit = SimpleNamespace(
            id="vanguard-2", unit_type=UnitType.VANGUARD, position=(0, -2), hp=5,
        )
        unit.move = lambda direction: None
        unit.wait = lambda: None
        config = default_config()
        config["home_patrol_radius"] = 2
        # A route to the slot exists, so the march is only stopped by packed
        # neighbours -- that keeps the "slot unreachable" hold out of the way
        # and lands the plan on the scout, which is the call being guarded.
        counts = {cell: tactic._CELL_UNIT_LIMIT for cell in (
            (-1, -2), (1, -2), (0, -3), (0, -1),
        )}
        seen: dict = {}

        def fake_scout(_unit, _pos, obstacles, _config, *, label, avoid_cells=()):
            seen["obstacles"] = obstacles
            seen["avoid"] = frozenset(avoid_cells)
            seen["label"] = label
            return ("WAIT", "no_way")

        with patch.object(tactic, "_scout_cardinal", fake_scout):
            action, detail = tactic._plan_home_combat(
                unit, unit_kind="vanguard", enemies=(), obstacle_cells=walls,
                core_pos=(0, 0), config=config, cell_counts=counts,
            )
        self.assertEqual((action, detail), ("WAIT", "no_way"))
        self.assertIs(seen["obstacles"], walls, "home planner rebuilt the wall set")
        self.assertEqual(
            seen["avoid"],
            {(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)},
            seen["avoid"],
        )

    def _walk(self, start, *, demand: bool, ticks: int = 6, radius: int = 2):
        """Re-plan one defender for ``ticks`` Ticks, returning its cells."""
        tactic._chute_in_demand = demand
        config = default_config()
        config["home_patrol_radius"] = radius
        pos = start
        visit: list = []
        for _ in range(ticks):
            unit = SimpleNamespace(
                id="vanguard-2", unit_type=UnitType.VANGUARD, position=pos, hp=5,
            )
            moved: list = []
            unit.move = lambda direction: moved.append(direction)
            unit.wait = lambda: None
            tactic._plan_home_combat(
                unit, unit_kind="vanguard", enemies=(), obstacle_cells=frozenset(),
                core_pos=(0, 0), config=config,
            )
            if moved:
                pos = (pos[0] + moved[0].delta[0], pos[1] + moved[0].delta[1])
            visit.append(pos)
        return visit

    def test_defender_standing_on_the_core_is_not_walled_in(self) -> None:
        # The lock covers the Core's four exits, so applying it to a defender
        # that is already on the Core would strand it there for the whole
        # delivery window instead of letting it join its patrol slot.
        tactic._chute_in_demand = True
        unit = SimpleNamespace(
            id="vanguard-2", unit_type=UnitType.VANGUARD, position=(0, 0), hp=5,
        )
        moved: list = []
        unit.move = lambda direction: moved.append(direction)
        unit.wait = lambda: None
        config = default_config()
        config["home_patrol_radius"] = 2
        action, detail = tactic._plan_home_combat(
            unit, unit_kind="vanguard", enemies=(), obstacle_cells=frozenset(),
            core_pos=(0, 0), config=config,
        )
        self.assertEqual(action, "MOVE", detail)
        self.assertEqual(len(moved), 1)

    def test_core_escape_settles_instead_of_trading_turns_with_the_chute(self) -> None:
        # Letting the Core-sitter out must not reopen the dither: chute-clear
        # evicts it from the ring, and the lock then keeps it out.
        ring = {(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)}
        visit = self._walk((0, 0), demand=True)
        self.assertNotIn(visit[-1], ring, f"defender settled back on the chute: {visit}")
        self.assertEqual(
            len(set(visit[-3:])), 1, f"defender never came to rest: {visit}",
        )


class HomeReturnSlotGoalTests(unittest.TestCase):
    """home-return and home-patrol must share one destination.

    home-return used to aim at the Core cell while home-patrol aimed at the
    defender's patrol slot, so a slot approach that the terrain routed around
    the Core gained distance-to-Core and was dragged straight back by the next
    home-return Tick: a permanent two-cell bounce (observed on the live map at
    (684,-298) <-> (684,-299), with and without chute demand).
    """

    # Slot for "vanguard-1" is (2,0); these four walls force the approach to
    # loop below it, which is where the two-goal bounce used to start.
    WALLS = frozenset({(-2, 0), (-1, 1), (1, -1), (1, 0)})

    def setUp(self) -> None:
        self._snap = {name: copy.copy(getattr(tactic, name)) for name in (
            "_object_names", "_object_name_counters", "_home_patrol_slot_cache",
            "_worker_last_pos", "_combat_path_cache", "_chute_in_demand",
            "_march_route_miss", "_march_budget_spent",
        )}
        for name, val in self._snap.items():
            if hasattr(val, "clear"):
                val.clear()
        tactic._chute_in_demand = False
        tactic._march_budget_spent = 0

    def tearDown(self) -> None:
        for name, val in self._snap.items():
            setattr(tactic, name, copy.copy(val))

    def _plan(self, pos, *, demand=False):
        tactic._chute_in_demand = demand
        unit = SimpleNamespace(
            id="vanguard-1", unit_type=UnitType.VANGUARD, position=pos, hp=5,
        )
        moved: list = []
        unit.move = lambda direction: moved.append(direction)
        unit.wait = lambda: None
        config = default_config()
        config["home_patrol_radius"] = 2
        action, detail = tactic._plan_home_combat(
            unit, unit_kind="vanguard", enemies=(), obstacle_cells=self.WALLS,
            core_pos=(0, 0), config=config,
        )
        return action, detail, moved, unit

    def test_far_defender_returns_to_its_slot_not_the_core_cell(self) -> None:
        action, detail, moved, _unit = self._plan((-3, -2))
        self.assertEqual(action, "MOVE")
        self.assertIn("home-return", detail)
        self.assertIn("(2, 0)", detail, detail)
        self.assertNotIn("(0, 0)", detail, detail)

    def test_slot_approach_beyond_the_band_is_not_reversed(self) -> None:
        pos, visit = (-3, -2), []
        for _ in range(16):
            _action, _detail, moved, _unit = self._plan(pos)
            if moved:
                pos = (pos[0] + moved[0].delta[0], pos[1] + moved[0].delta[1])
            visit.append(pos)
        reversals = sum(
            1 for i in range(2, len(visit))
            if visit[i] == visit[i - 2] and visit[i] != visit[i - 1]
        )
        self.assertEqual(reversals, 0, f"defender bounced between two cells: {visit}")
        self.assertLessEqual(tactic._manhattan(visit[-1], (2, 0)), 1, visit)


class HomeSlotUnreachableHoldTests(unittest.TestCase):
    """A walled-off patrol slot must not leave the defender shuffling.

    V3's radius-3 slot (685,-300) has no path at all from the Core
    neighbourhood, so each Tick it alternated a failed A* with the greedy step
    one cell east/west: ``LEFT home-patrol`` / ``RIGHT home-patrol
    (685, -300)`` on 100% of Ticks (live ticks 385531-385554, after the ring
    lock stopped it re-entering the chute).
    """

    # "vanguard-1" parks on slot (2,0); these three walls plus the locked chute
    # ring cell (0,-1) leave the defender at (0,-2) with no walkable step.
    WALLS = frozenset({(1, -2), (-1, -2), (0, -3)})

    def setUp(self) -> None:
        self._snap = {name: copy.copy(getattr(tactic, name)) for name in (
            "_object_names", "_object_name_counters", "_home_patrol_slot_cache",
            "_worker_last_pos", "_combat_path_cache", "_chute_in_demand",
            "_march_route_miss", "_march_budget_spent",
        )}
        for name, val in self._snap.items():
            if hasattr(val, "clear"):
                val.clear()
        tactic._chute_in_demand = False
        tactic._march_budget_spent = 0

    def tearDown(self) -> None:
        for name, val in self._snap.items():
            setattr(tactic, name, copy.copy(val))

    def _plan(self, pos, *, demand: bool):
        tactic._chute_in_demand = demand
        unit = SimpleNamespace(
            id="vanguard-1", unit_type=UnitType.VANGUARD, position=pos, hp=5,
        )
        moved: list = []
        unit.move = lambda direction: moved.append(direction)
        unit.wait = lambda: None
        config = default_config()
        config["home_patrol_radius"] = 2
        action, detail = tactic._plan_home_combat(
            unit, unit_kind="vanguard", enemies=(), obstacle_cells=self.WALLS,
            core_pos=(0, 0), config=config,
        )
        return action, detail, moved

    def test_defender_holds_when_its_slot_cannot_be_reached(self) -> None:
        action, detail, moved = self._plan((0, -2), demand=True)
        self.assertEqual(moved, [], f"{action} {detail}")
        self.assertEqual(action, "WAIT")
        self.assertIn("home-slot-unreachable", detail)
        self.assertIn("(2, 0)", detail)

    def test_holding_repeats_instead_of_resuming_the_shuffle(self) -> None:
        pos, visit = (0, -2), []
        for _ in range(6):
            _action, _detail, moved = self._plan(pos, demand=True)
            if moved:
                pos = (pos[0] + moved[0].delta[0], pos[1] + moved[0].delta[1])
            visit.append(pos)
        self.assertEqual(set(visit), {(0, -2)}, visit)

    def test_patrol_continues_while_the_slot_is_still_reachable(self) -> None:
        # Control: without chute demand the ring cell (0,-1) is open, the route
        # to (2,0) exists and no hold may swallow the defender there.
        action, detail, moved = self._plan((0, -2), demand=False)
        self.assertEqual(action, "MOVE", detail)
        self.assertEqual(len(moved), 1)


class CellLimitCooldownBreakerTests(unittest.TestCase):
    """The CELL_UNIT_LIMIT breaker must stop re-issuing the rejected move.

    One forced sidestep was not a break: normal planning resumed the next Tick
    and walked straight back onto the same packed cell, so a single defender
    (V3 onto (685,-302)) logged one warn every three Ticks for 27,000 Ticks.
    """

    GLOBALS = (
        "_cell_limit_streak", "_cell_limit_blocked", "_worker_contest_streak",
        "_object_names", "_object_name_counters", "_combat_path_cache",
        "_worker_last_pos", "_known_obstacles", "_obstacle_memory",
        "_dead_end_cache_key", "_dead_end_cache", "_path_blockers_union_key",
        "_path_blockers_union",
    )

    def setUp(self) -> None:
        self._snap = {name: copy.copy(getattr(tactic, name)) for name in self.GLOBALS}
        tactic._cell_limit_streak = {}
        tactic._cell_limit_blocked = {}
        tactic._worker_contest_streak = {}
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        tactic._combat_path_cache.clear()
        tactic._worker_last_pos.clear()
        tactic._obstacle_memory = set()
        tactic._known_obstacles = frozenset()

    def tearDown(self) -> None:
        for name, val in self._snap.items():
            setattr(tactic, name, copy.copy(val))

    @staticmethod
    def _rejected_event() -> SimpleNamespace:
        return SimpleNamespace(
            event_type="UNIT_MOVE_FAILED",
            reason_code="CELL_UNIT_LIMIT",
            actor_id="vang-1", target_id=None, position=(4, 0), values={},
        )

    def _run(self, *, tick, events=()):
        core = SimpleNamespace(
            id="core", position=(0, 0), hp=5, shield=10,
            view=SimpleNamespace(state=CoreState.NORMAL),
            spawn=lambda unit_type: None, heal=lambda: None,
            repair_shield=lambda: None, pickup_beacon=lambda: None,
            start_move=lambda direction: None, wait=lambda: None,
        )
        unit = SimpleNamespace(
            id="vang-1", unit_type=UnitType.VANGUARD, position=(3, 0),
            direction=None,
        )
        unit.move = lambda direction: None
        unit.wait = lambda: None
        turn = SimpleNamespace(
            tick=tick, units=(unit,), workers=(), vanguards=(unit,), rangers=(),
            visible_enemies=(), core=core, resources=50,
            resource_cells=frozenset(), resource_space=3,
            beacon=SimpleNamespace(position=None, status=SimpleNamespace(name="GROUND")),
            state=SimpleNamespace(population=3), events=tuple(events),
            obstacle_cells=frozenset(),
        )
        config = default_config()
        config["target_workers"] = 0
        config["target_vanguards"] = 1
        config["target_rangers"] = 0
        seen: dict[str, object] = {}
        real_plan = tactic._plan_vanguard

        def spy(vanguard, enemies, obstacle_cells, *args, **kwargs):
            seen["obstacles"] = frozenset(obstacle_cells)
            return real_plan(vanguard, enemies, obstacle_cells, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            with patch.object(tactic, "load_config", return_value=config), \
                 patch.object(tactic, "MAP_MEMORY_PATH", temp / "map_memory.json"), \
                 patch.object(tactic, "WAYPOINTS_PATH", temp / "waypoints.json"), \
                 patch.object(tactic, "SELF_DESTRUCT_PATH", temp / "self_destruct.json"), \
                 patch.object(tactic, "BATTLE_LOG_PATH", temp / "battle_log.jsonl"), \
                 patch.object(tactic, "CONFIG_PATH", temp / "tactic_config.json"), \
                 patch.object(tactic, "_plan_vanguard", side_effect=spy):
                tactic._map_dirty = False
                _, details = tactic.choose_actions(turn)
        return details, seen


    def _reject(self, ticks):
        """Consecutive Ticks in which this vanguard is rejected from (4, 0)."""
        details = {}
        for tick in ticks:
            details, _ = self._run(tick=tick, events=[self._rejected_event()])
        return details

    def test_rejects_below_the_threshold_do_not_block_the_cell(self) -> None:
        self._run(tick=1, events=[self._rejected_event()])
        self._run(tick=2, events=[self._rejected_event()])
        self.assertEqual(tactic._cell_limit_streak["vang-1"], 2)
        self.assertEqual(tactic._cell_limit_blocked, {})

    def test_third_reject_trips_the_breaker_and_blocks_the_cell(self) -> None:
        self._run(tick=1, events=[self._rejected_event()])
        self._run(tick=2, events=[self._rejected_event()])
        details, _ = self._run(tick=3, events=[self._rejected_event()])
        self.assertEqual(tactic._cell_limit_blocked["vang-1"], (
            (4, 0), 1, 3 + tactic._CELL_LIMIT_BLOCK_TICKS,
        ))
        self.assertIn("cell-limit-detour", details["vang-1"])

    def test_blocked_cell_is_a_wall_while_the_window_is_open(self) -> None:
        self._reject([1, 2, 3])
        _, seen = self._run(tick=4)
        self.assertIn((4, 0), seen["obstacles"])
        _, seen = self._run(tick=2 + tactic._CELL_LIMIT_BLOCK_TICKS)
        self.assertIn((4, 0), seen["obstacles"])
        # Window closed: the cell is walkable again, it was never a wall.
        _, seen = self._run(tick=3 + tactic._CELL_LIMIT_BLOCK_TICKS)
        self.assertNotIn((4, 0), seen["obstacles"])

    def test_repeat_trips_double_the_window(self) -> None:
        self._reject([1, 2, 3])
        self.assertEqual(tactic._cell_limit_blocked["vang-1"][1], 1)
        # The trip works: the unit stops re-issuing the move, so its streak
        # clears, and only a later rejection on the same cell earns a longer
        # window.
        self._run(tick=4)
        self._reject([5, 6, 7])
        self.assertEqual(tactic._cell_limit_blocked["vang-1"], (
            (4, 0), 2, 7 + 2 * tactic._CELL_LIMIT_BLOCK_TICKS,
        ))
        self._run(tick=8)
        self._reject([9, 10, 11])
        self.assertEqual(tactic._cell_limit_blocked["vang-1"][1], 3)
        self.assertEqual(
            tactic._cell_limit_blocked["vang-1"][2],
            11 + 4 * tactic._CELL_LIMIT_BLOCK_TICKS,
        )


class KiteBacktrackTieBreakTests(unittest.TestCase):
    """The kite single-step chooser must not undo its own last step.

    V11/V6 shuttled between two cells on 98% of Ticks: the A* route layer
    (``kite-route``) stepped north, then from the next cell the route search
    missed and ``kite-position`` scored every safe cell as a tie and took the
    first in scan order — the cell it had just come from.
    """

    def setUp(self) -> None:
        self._snap = {name: copy.copy(getattr(tactic, name)) for name in (
            "_kite_prev_pos", "_kite_stall_pos", "_kite_stall_ticks",
            "_dead_end_cache_key", "_dead_end_cache",
        )}
        tactic._kite_prev_pos.clear()
        tactic._kite_stall_pos.clear()
        tactic._kite_stall_ticks.clear()

    def tearDown(self) -> None:
        for name, val in self._snap.items():
            setattr(tactic, name, copy.copy(val))

    def _choose(self, *, came_from, stall_ticks=0, goal=(50, 0),
                walls=((1, 0),), unit_id="kite-1", pos=(0, 0)):
        unit = SimpleNamespace(id=unit_id, position=pos)
        if came_from is not None:
            tactic._kite_prev_pos[unit_id] = came_from
        direction, _, _ = tactic._kite_choose_move(
            unit, pos, goal, (), frozenset(walls), 3, None,
            must_move=True, stall_ticks=stall_ticks,
        )
        return direction

    def test_equal_progress_step_beats_stepping_back(self) -> None:
        # UP (0,-1), DOWN (0,1) and LEFT (-1,0) all cost one cell of progress.
        # UP is the cell the unit came from and would otherwise win on scan
        # order; DOWN is next in the scan, so that is the step it must take.
        self.assertEqual(self._choose(came_from=(0, -1)), Direction.DOWN)

    def test_without_the_memory_the_scan_order_shuttles(self) -> None:
        # Documents the old failure: with no came-from memory the tie resolves
        # to the first scanned cell, which is the one it just left.
        self.assertEqual(self._choose(came_from=None), Direction.UP)

    def test_stall_unlocked_still_refuses_the_backtrack(self) -> None:
        self.assertEqual(
            self._choose(
                came_from=(0, -1), stall_ticks=tactic._KITE_STALL_UNLOCK_TICKS
            ),
            Direction.DOWN,
        )

    def test_backtrack_is_only_a_tie_break(self) -> None:
        # When stepping back is also the only step that advances, take it.
        direction = self._choose(
            came_from=(0, -1), goal=(0, -50),
            walls=((1, 0), (-1, 0), (0, 1)), unit_id="kite-2",
        )
        self.assertEqual(direction, Direction.UP)


class KiteRouteNoUTurnRegressionTests(KiteTeamPlannerTests):
    """A stalled kite unit must not have its U-turn served by the route layer.

    Disciplining only ``kite-position`` moved the shuttle from two cells to
    three: the chooser stepped east to escape, then the cached ``kite-route``
    stepped straight back west. Both layers now honour the same no-U-turn rule
    (tactic.py:5421), while a genuinely boxed unit still retreats.
    """

    def setUp(self):
        super().setUp()
        self._prev = dict(tactic._kite_prev_pos)
        self._stalls = dict(tactic._kite_stall_ticks)
        self._anchors = dict(tactic._kite_stall_pos)
        self._paths = dict(tactic._combat_path_cache)
        tactic._kite_prev_pos.clear()
        tactic._kite_stall_ticks.clear()
        tactic._kite_stall_pos.clear()
        tactic._combat_path_cache.clear()

    def tearDown(self):
        tactic._kite_prev_pos.clear()
        tactic._kite_prev_pos.update(self._prev)
        tactic._kite_stall_ticks.clear()
        tactic._kite_stall_ticks.update(self._stalls)
        tactic._kite_stall_pos.clear()
        tactic._kite_stall_pos.update(self._anchors)
        tactic._combat_path_cache.clear()
        tactic._combat_path_cache.update(self._paths)
        super().tearDown()

    def _plan(self, uid, pos, came_from, stall_ticks, obstacles):
        tactic._kite_prev_pos[uid] = came_from
        tactic._kite_stall_pos[uid] = came_from
        tactic._kite_stall_ticks[uid] = stall_ticks
        unit = self.unit(uid, pos)
        # Goal due west, so the westward cell is also the one it came from.
        config = dict(self.config)
        config.update({"kite_target_x": -10, "kite_target_y": 0})
        action, detail = tactic._plan_vanguard(
            unit, (), frozenset(obstacles), config,
            core_pos=(0, 0), team="kite",
        )
        return action, detail, unit

    def test_route_step_back_is_refused_while_stalled(self) -> None:
        # Goal is due west, so (0, 0) — the cell the unit came from — is the
        # natural route step. South (1, 1) is open and leads around.
        walls = {(-1, 0), (0, -1), (1, -1)}
        action, detail, unit = self._plan(
            "kite-uturn", (1, 0), (0, 0), tactic._KITE_STALL_UNLOCK_TICKS, walls,
        )
        self.assertEqual(action, "MOVE", detail)
        self.assertNotEqual(unit.arg, Direction.LEFT, detail)

    def test_route_still_doubles_back_when_not_stalled(self) -> None:
        walls = {(-1, 0), (0, -1), (1, -1)}
        action, detail, unit = self._plan(
            "kite-march", (1, 0), (0, 0), 0, walls,
        )
        self.assertEqual(action, "MOVE", detail)
        self.assertEqual(unit.arg, Direction.LEFT, detail)

    def test_boxed_stalled_unit_retreats_instead_of_sitting(self) -> None:
        # Only the cell it came from is open: retreating must beat WAIT.
        walls = {(2, 0), (1, -1), (1, 1)}
        action, detail, unit = self._plan(
            "kite-boxed", (1, 0), (0, 0), tactic._KITE_STALL_UNLOCK_TICKS, walls,
        )
        self.assertEqual(action, "MOVE", detail)
        self.assertEqual(unit.arg, Direction.LEFT, detail)


class SquadMarchBudgetTests(unittest.TestCase):
    """Squad marches need a distance-scaled A* budget, bounded per Tick.

    Measured on the live obstacle memory: all 9 sampled kite/attack units had a
    reachable objective (route length ~= straight line), yet the flat
    bfs_max_steps=2500 missed every one, so each re-decided a single step per
    Tick and shuttled instead of marching.
    """

    def setUp(self) -> None:
        self._snap = {
            "miss": dict(tactic._march_route_miss),
            "spent": tactic._march_budget_spent,
            "cache": dict(tactic._combat_path_cache),
            "calls": tactic._pathfind_calls,
            "exp": tactic._pathfind_expansions,
            "ms": tactic._pathfind_ms,
            "de_ms": tactic._dead_end_ms,
            "de_runs": tactic._dead_end_runs,
        }
        tactic._march_route_miss.clear()
        tactic._march_budget_spent = 0
        tactic._combat_path_cache.clear()

    def tearDown(self) -> None:
        tactic._march_route_miss.clear()
        tactic._march_route_miss.update(self._snap["miss"])
        tactic._march_budget_spent = self._snap["spent"]
        tactic._combat_path_cache.clear()
        tactic._combat_path_cache.update(self._snap["cache"])
        tactic._pathfind_calls = self._snap["calls"]
        tactic._pathfind_expansions = self._snap["exp"]
        tactic._pathfind_ms = self._snap["ms"]
        tactic._dead_end_ms = self._snap["de_ms"]
        tactic._dead_end_runs = self._snap["de_runs"]

    def _budget(self, uid, pos, goal, min_budget=2500):
        return tactic._march_budget(uid, pos, goal, min_budget)

    def test_close_goal_keeps_the_callers_budget(self) -> None:
        self.assertEqual(self._budget("u1", (0, 0), (30, 0)), 2500)

    def test_far_goal_scales_with_distance(self) -> None:
        self.assertEqual(self._budget("u2", (0, 0), (200, 0)),
                         200 * tactic._MARCH_BUDGET_FACTOR)
        self.assertEqual(self._budget("u3", (0, 0), (3000, 0)),
                         tactic._MARCH_BUDGET_MAX)

    def test_miss_cooldown_skips_the_same_goal_only(self) -> None:
        goal = (500, 0)
        budget = self._budget("u4", (0, 0), goal)
        tactic._march_note_result("u4", goal, budget, found=False)
        self.assertIsNone(self._budget("u4", (1, 0), goal))
        # A new objective is worth a fresh search.
        self.assertIsNotNone(self._budget("u4", (1, 0), (0, 600)))

    def test_miss_cooldown_expires(self) -> None:
        goal = (500, 0)
        tactic._march_note_result("u5", goal, self._budget("u5", (0, 0), goal), found=False)
        for _ in range(tactic._MARCH_MISS_COOLDOWN - 1):
            self.assertIsNone(self._budget("u5", (0, 0), goal))
        self.assertIsNotNone(self._budget("u5", (0, 0), goal))

    def test_success_clears_the_miss(self) -> None:
        goal = (500, 0)
        tactic._march_note_result("u6", goal, self._budget("u6", (0, 0), goal), found=False)
        tactic._march_note_result("u6", goal, 0, found=True)
        self.assertEqual(tactic._march_route_miss, {})
        self.assertIsNotNone(self._budget("u6", (0, 0), goal))

    def test_per_tick_cap_stops_a_squad_stampede(self) -> None:
        tactic._march_budget_spent = tactic._MARCH_BUDGET_PER_TICK - 10
        self.assertIsNone(self._budget("u7", (0, 0), (500, 0)))
        tactic._reset_pathfind_counters()
        self.assertEqual(tactic._march_budget_spent, 0)
        self.assertIsNotNone(self._budget("u7", (0, 0), (500, 0)))

    def test_move_towards_searches_far_goals_with_the_scaled_budget(self) -> None:
        seen: list[int] = []
        real = tactic._bfs_path

        def spy(start, goal, obstacles, max_steps=2500, **kwargs):
            seen.append(max_steps)
            return real(start, goal, obstacles, max_steps=max_steps, **kwargs)

        unit = SimpleNamespace(id="march-1", position=(0, 0))
        unit.move = lambda direction: None
        unit.wait = lambda: None
        with patch.object(tactic, "_bfs_path", side_effect=spy):
            result = tactic._move_towards(
                unit, (0, 0), (0, 300), frozenset(), detail_prefix="test",
            )
        self.assertEqual(seen, [300 * tactic._MARCH_BUDGET_FACTOR])
        self.assertIsNotNone(result)
        self.assertEqual(tactic._combat_path_cache["march-1"]["goal"], (0, 300))
        self.assertEqual(tactic._march_route_miss, {})

    def test_move_towards_records_a_far_miss_and_stops_retrying(self) -> None:
        searches: list[int] = []
        unit = SimpleNamespace(id="march-2", position=(0, 0))
        unit.move = lambda direction: None
        unit.wait = lambda: None

        def miss(start, goal, obstacles, max_steps=2500, **kwargs):
            searches.append(max_steps)
            return None

        with patch.object(tactic, "_bfs_path", side_effect=miss):
            first = tactic._move_towards(
                unit, (0, 0), (0, 300), frozenset(), detail_prefix="test",
            )
            second = tactic._move_towards(
                unit, (0, 0), (0, 300), frozenset(), detail_prefix="test",
            )
        # Both Ticks still step (the budget-free greedy march remains), but the
        # expensive search only runs once.
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(searches, [300 * tactic._MARCH_BUDGET_FACTOR])
        self.assertEqual(tactic._march_route_miss["march-2"][0], (0, 300))


class CorePackedDestinationTests(CoreTerrainBlockedLearningTests):
    """Regression from the remote log: the Core picks headings from the
    obstacle/dead-end view only, so it re-issued a step into a cell packed with
    its own workers. Every rejected attempt re-entered MOVING, and a MOVING Core
    rejects every worker DEPOSIT — the vault sat at 26/185 for 11 hours while
    "C1 移动/动作失败：CELL_UNIT_LIMIT (666,-317)" alternated with
    "W16 卸货失败：核心迁移中"."""

    def test_packed_heading_is_skipped_for_a_free_one(self) -> None:
        config = default_config()
        config["core_max_drift"] = 0
        # Two workers fill (1,0) — the server caps a cell at _CELL_UNIT_LIMIT.
        action, moves, _ = self._run(
            core_pos=(0, 0), worker_pos=(1, 0), extra_workers=((1, 0),),
            tick=1, config=config,
        )
        self.assertNotEqual(moves, [Direction.RIGHT])
        self.assertEqual(moves, [Direction.DOWN])
        self.assertEqual(action, "MOVE_DOWN")

    def test_cell_limit_rejection_suppresses_the_following_steps(self) -> None:
        config = default_config()
        config["core_max_drift"] = 0
        event = SimpleNamespace(
            event_type="CORE_MOVE_FAILED",
            reason_code="CELL_UNIT_LIMIT",
            actor_id="core", target_id=None, position=(1, 0), values={},
        )
        self._run(
            core_pos=(0, 0), worker_pos=(500, 0), tick=5, events=[event],
            config=config,
        )
        self.assertEqual(
            tactic._core_move_hold_until_tick,
            5 + tactic._CORE_MOVE_HOLD_TICKS,
        )
        # A packed cell is our own bodies, not terrain: nothing may be learned
        # into the permanent obstacle map from this rejection.
        self.assertNotIn((1, 0), tactic._obstacle_memory)

        # Still holding: the Core waits instead of hammering the chute away.
        action, moves, _ = self._run(
            core_pos=(0, 0), worker_pos=(500, 0), tick=6, config=config,
        )
        self.assertEqual(moves, [])
        self.assertEqual(action, "WAIT")

        # Past the hold the march resumes on its own.
        action, moves, _ = self._run(
            core_pos=(0, 0), worker_pos=(500, 0),
            tick=6 + tactic._CORE_MOVE_HOLD_TICKS, config=config,
        )
        self.assertEqual(moves, [Direction.RIGHT])


class WorkerExploreLoopBreakTests(unittest.TestCase):
    """The explore fan-out ranked candidates with a 4-position memory, shorter
    than the rings it was meant to break, so an unassigned worker circled the
    same pocket until it died (observed W7/W31/W13/W20 each standing on the same
    8 cells 78 times in 625 Ticks while the store stayed at 26/185)."""

    def setUp(self) -> None:
        self._saved = {
            "last": dict(tactic._worker_last_pos),
            "recent": {k: list(v) for k, v in tactic._worker_recent.items()},
            "history": {k: list(v) for k, v in tactic._worker_explore_history.items()},
            "rally": dict(tactic._worker_rally_home),
            "assign": dict(tactic._resource_assignments),
            "space": tactic.turn_context.resource_space,
        }
        tactic._worker_last_pos.clear()
        tactic._worker_recent.clear()
        tactic._worker_explore_history.clear()
        tactic._worker_rally_home.clear()
        tactic._resource_assignments.clear()

    def tearDown(self) -> None:
        tactic._worker_last_pos.clear()
        tactic._worker_last_pos.update(self._saved["last"])
        tactic._worker_recent.clear()
        tactic._worker_recent.update(self._saved["recent"])
        tactic._worker_explore_history.clear()
        tactic._worker_explore_history.update(self._saved["history"])
        tactic._worker_rally_home.clear()
        tactic._worker_rally_home.update(self._saved["rally"])
        tactic._resource_assignments.clear()
        tactic._resource_assignments.update(self._saved["assign"])
        tactic.turn_context.resource_space = self._saved["space"]

    def test_never_walked_exit_outranks_a_ring_cell_lapped_seven_steps_ago(self):
        class Worker:
            id = "w-ring"
            position = (2, 1)
            cargo = 0
            direction = None

            def move(self, direction) -> None:
                self.direction = direction

            def wait(self) -> None:
                self.waited = True

        lap = [(2, 0), (1, 0), (0, 0), (0, 1), (0, 2), (1, 2), (2, 2), (2, 1)]
        tactic._worker_explore_history["w-ring"] = list(lap)
        tactic._worker_recent["w-ring"] = list(lap[-4:])
        tactic._worker_last_pos["w-ring"] = (2, 2)
        # A 3x3 shell around a centre wall; (3,1) is the pocket's only exit and
        # the ring successor (2,0) was last stood on 7 steps ago.
        obstacles = frozenset({(1, 1), (-1, 0), (-1, 1), (-1, 2), (0, -1),
                               (1, -1), (2, -1), (0, 3), (1, 3), (2, 3)})
        worker = Worker()
        config = default_config()
        tactic.turn_context.resource_space = 100

        tactic._plan_worker(
            worker,
            SimpleNamespace(position=(900, 900)),
            resource_cells=frozenset(),
            obstacle_cells=obstacles,
            depleted=set(),
            config=config,
        )

        self.assertIsNotNone(worker.direction)
        self.assertEqual(
            (worker.position[0] + worker.direction.delta[0],
             worker.position[1] + worker.direction.delta[1]),
            (3, 1),
        )

    def test_rally_engages_far_out_and_releases_inside_the_mining_ring(self):
        config = default_config()
        config["worker_mine_max_distance"] = 100
        core = (0, 0)
        # Past 2x the mine radius an unassignable worker walks home instead.
        self.assertEqual(tactic._worker_rally_goal("u", (0, 201), core, config), core)
        self.assertTrue(tactic._worker_rally_home.get("u"))
        # Hysteresis: still inside the ring it keeps marching in, and only
        # releases well inside the assignable zone — a single threshold would
        # flip rally/explore every Tick at the boundary.
        self.assertEqual(tactic._worker_rally_goal("u", (0, 120), core, config), core)
        self.assertIsNone(tactic._worker_rally_goal("u", (0, 50), core, config))
        self.assertNotIn("u", tactic._worker_rally_home)
        # A fresh worker inside the enter threshold keeps exploring.
        self.assertIsNone(tactic._worker_rally_goal("v", (0, 150), core, config))

    def test_rally_is_inert_without_a_mine_radius(self):
        config = default_config()
        config["worker_mine_max_distance"] = 0
        self.assertIsNone(
            tactic._worker_rally_goal("u", (0, 900), (0, 0), config)
        )

    def test_far_unassigned_worker_marches_home_instead_of_exploring(self):
        class Worker:
            id = "w-rally"
            position = (0, 400)
            cargo = 0
            direction = None

            def move(self, direction) -> None:
                self.direction = direction

            def wait(self) -> None:
                self.waited = True

        config = default_config()
        config["worker_mine_max_distance"] = 100
        tactic.turn_context.resource_space = 100
        worker = Worker()

        action, detail = tactic._plan_worker(
            worker,
            SimpleNamespace(position=(0, 0)),
            resource_cells=frozenset(),
            obstacle_cells=frozenset(),
            depleted=set(),
            config=config,
        )

        self.assertEqual(action, "MOVE")
        self.assertIn("rally-home", detail)
        self.assertNotIn("explore", detail)
        self.assertEqual(
            (worker.position[0] + worker.direction.delta[0],
             worker.position[1] + worker.direction.delta[1]),
            (0, 399),
        )


class HomeReturnFarStrandedTests(unittest.TestCase):
    """A home defender stranded far outside the patrol ring must march, not shuffle.

    Post-2026-10-10 deploy: V8 sat 1420 cells from the Core alternating
    ``UP/RIGHT home-patrol`` with ``DOWN/LEFT home-return`` across 3 cells
    (98% revisits over 130 Ticks). The A* march has a bounded expansion budget,
    so at that distance it returns None every Tick and the planner fell through
    to the patrol scout step, whose one-cell shuffle the next Tick's march
    undid. The attack march already solved exactly this with a budget-free
    greedy step; the home march has to do the same instead of drifting down into
    the scout.
    """

    def setUp(self) -> None:
        self._saved = {
            "last": dict(tactic._worker_last_pos),
            "miss": dict(tactic._march_route_miss),
            "names": dict(tactic._object_names),
            "counters": dict(tactic._object_name_counters),
        }
        tactic._worker_last_pos.clear()
        tactic._march_route_miss.clear()

    def tearDown(self) -> None:
        tactic._worker_last_pos.clear()
        tactic._worker_last_pos.update(self._saved["last"])
        tactic._march_route_miss.clear()
        tactic._march_route_miss.update(self._saved["miss"])
        tactic._object_names.clear()
        tactic._object_names.update(self._saved["names"])
        tactic._object_name_counters.clear()
        tactic._object_name_counters.update(self._saved["counters"])

    def test_far_defender_falls_back_to_the_budget_free_march(self) -> None:
        core_pos = (668, -234)
        pos = (839, -1656)
        unit = SimpleNamespace(
            id="v-far", unit_type=UnitType.VANGUARD, position=pos, hp=5,
        )
        steps: list = []
        unit.move = lambda direction: steps.append(direction)
        unit.wait = lambda: None
        config = default_config()
        config["home_patrol_radius"] = 3

        with patch.object(tactic, "_move_towards", return_value=None):
            action, detail = tactic._plan_home_combat(
                unit, unit_kind="vanguard", enemies=(),
                obstacle_cells=frozenset(), core_pos=core_pos, config=config,
            )

        self.assertEqual(action, "MOVE")
        self.assertIn("home-return", detail)
        self.assertNotIn("home-patrol", detail)
        self.assertEqual(len(steps), 1)
        # The step has to close the distance to the slot; a shuffle does not.
        nxt = (pos[0] + steps[0].delta[0], pos[1] + steps[0].delta[1])
        self.assertEqual(
            tactic._manhattan(nxt, core_pos),
            tactic._manhattan(pos, core_pos) - 1,
        )


class CoreCommittedRouteStickinessTests(CoreTerrainBlockedLearningTests):
    """A committed Core detour must drive the following Ticks, not just its own.

    Post-deploy field: C1 bounced (668,-236)/(668,-237) with net 0 over 81 Ticks.
    (668,-235) is walled, so every Tick standing on -236 the A* detour said "step
    back to -237 and go around", while every Tick standing on -237 the greedy
    heading saw -236 free and stepped straight back onto it. The detour was
    only consulted when the greedy primary cell was blocked, so the two rules
    erased each other forever.
    """

    def setUp(self) -> None:
        # Hermetic: an earlier test's obstacle memory makes the cached route's
        # next cell look walled, and the block then silently re-runs A* instead
        # of following the committed path. The base class snapshots these
        # globals, so clearing here is restored automatically on teardown.
        super().setUp()
        tactic._obstacle_memory.clear()
        tactic._known_obstacles = frozenset()
        tactic._dead_obstacles = None
        tactic._dead_structure_built = False
        tactic._dead_set = set()
        tactic._dead_view = frozenset()
        tactic._dead_open_count = {}
        tactic._path_blockers_union_key = None
        tactic._path_blockers_union = frozenset()
        tactic._core_target_path_cache.clear()

    def test_cached_route_wins_even_when_the_greedy_heading_is_free(self) -> None:
        config = default_config()
        config["core_target_enabled"] = True
        config["core_target_x"] = 0
        config["core_target_y"] = 50
        config["core_max_drift"] = 0
        # Down is open and is exactly what the greedy heading wants, but the
        # cached route says the way around starts by stepping up.
        tactic._core_target_path_cache.clear()
        tactic._core_target_path_cache.update({
            "goal": (0, 50),
            "path": [(0, 0), (0, -1), (1, -1), (1, 0), (1, 1), (1, 2)],
        })

        action, moves, _ = self._run(
            core_pos=(0, 0), worker_pos=(500, 0), tick=1, config=config,
        )

        self.assertEqual(moves, [Direction.UP])
        self.assertEqual(action, "MOVE_UP")

    def test_greedy_heading_still_used_with_no_committed_route(self) -> None:
        config = default_config()
        config["core_target_enabled"] = True
        config["core_target_x"] = 0
        config["core_target_y"] = 50
        config["core_max_drift"] = 0
        tactic._core_target_path_cache.clear()

        _action, moves, _ = self._run(
            core_pos=(0, 0), worker_pos=(500, 0), tick=1, config=config,
        )

        self.assertEqual(moves, [Direction.DOWN])


class LongMarchBoxEscapeTests(unittest.TestCase):
    """`_long_march_step` must not re-walk a cell two steps back.

    One step of back-track memory is shorter than the 2x2 box it traps units
    in: the march ranks by distance to the goal, so the best-distance neighbour
    is usually the cell stepped through two Ticks ago. Observed right after the
    rally-home deploy — W21 at (732,-681) and W14 at (1425,-237) each spent 81
    Ticks on 4 cells with net displacement 0 while supposedly marching hundreds
    of cells home.
    """

    def setUp(self) -> None:
        self._saved = {
            "last": dict(tactic._worker_last_pos),
            "history": {k: list(v) for k, v in tactic._worker_explore_history.items()},
        }
        tactic._worker_last_pos.clear()
        tactic._worker_explore_history.clear()

    def tearDown(self) -> None:
        tactic._worker_last_pos.clear()
        tactic._worker_last_pos.update(self._saved["last"])
        tactic._worker_explore_history.clear()
        tactic._worker_explore_history.update(self._saved["history"])

    def _march(self):
        unit = SimpleNamespace(id="m-box", position=(0, 0))
        steps: list = []
        unit.move = lambda direction: steps.append(direction)
        # The unit circled A(0,1) -> B(1,1) -> C(1,0) and now stands on D(0,0);
        # only C is inside the one-step memory, so A is still a legal "best".
        tactic._worker_explore_history["m-box"] = [(0, 1), (1, 1), (1, 0), (0, 0)]
        tactic._worker_last_pos["m-box"] = (1, 0)
        action, detail = tactic._long_march_step(
            unit, (0, 0), (0, 100), frozenset(), detail_prefix="rally-home",
        )
        return action, detail, steps

    def test_visited_corner_loses_the_distance_tie(self) -> None:
        action, _detail, steps = self._march()
        self.assertEqual(action, "MOVE")
        self.assertEqual(len(steps), 1)
        # A(0,1) is 99 from the goal but was walked 3 steps ago; the nudge must
        # hand the step to fresh ground instead of closing the box cycle.
        self.assertNotEqual(steps[0].delta, (0, 1))
        self.assertEqual(
            tactic._worker_explore_history["m-box"][-1], (0, 0),
            "the march must record its step into the same visited tail",
        )

    def test_without_history_the_closest_cell_still_wins(self) -> None:
        tactic._worker_explore_history.clear()
        tactic._worker_last_pos.clear()
        unit = SimpleNamespace(id="m-fresh", position=(0, 0))
        steps: list = []
        unit.move = lambda direction: steps.append(direction)
        tactic._long_march_step(
            unit, (0, 0), (0, 100), frozenset(), detail_prefix="rally-home",
        )
        self.assertEqual(steps[0].delta, (0, 1))


class LongMarchDeadEndMouthEscalationTests(unittest.TestCase):
    """Recycled interior ground must push the march to the pocket mouth.

    A dead-end pocket's INTERIOR cells are ordinary cells, so the first greedy
    pass always finds one and never falls through to the pass that is allowed to
    step onto the flagged mouth. Observed: V8 at (839,-1656), 1568 cells from
    home, circling 3 cells with net displacement 0 while the home-return march
    ranked its own cul-de-sac.
    """

    # (1,0) is the mouth: walled on three sides, only (0,0) open behind it.
    # (0,-1) is the way out into open map, but 2 cells further from (10,0).
    WALLS = frozenset({(-1, 0), (0, 1), (2, 0), (1, 1), (1, -1)})

    def setUp(self) -> None:
        self._saved = {
            "last": dict(tactic._worker_last_pos),
            "history": {k: list(v) for k, v in tactic._worker_explore_history.items()},
        }
        tactic._worker_last_pos.clear()
        tactic._worker_explore_history.clear()

    def tearDown(self) -> None:
        tactic._worker_last_pos.clear()
        tactic._worker_last_pos.update(self._saved["last"])
        tactic._worker_explore_history.clear()
        tactic._worker_explore_history.update(self._saved["history"])

    def _step(self, uid: str, tail: list[tuple[int, int]]):
        unit = SimpleNamespace(id=uid, position=(0, 0))
        steps: list = []
        unit.move = lambda direction: steps.append(direction)
        if tail:
            tactic._worker_explore_history[uid] = tail
        result = tactic._long_march_step(
            unit, (0, 0), (10, 0), self.WALLS, detail_prefix="home-return",
        )
        self.assertIsNotNone(result)
        return steps[0].delta

    def test_fresh_unit_prefers_the_open_exit_over_the_mouth(self) -> None:
        self.assertEqual(self._step("m-mouth-fresh", []), (0, -1))

    def test_recycled_interior_reaches_for_the_mouth(self) -> None:
        # Standing on the mouth itself is legal for the escape pass: leaving a
        # pocket is only possible through it.
        self.assertEqual(
            self._step("m-mouth-stale", [(0, -1), (0, 0)]), (1, 0),
            "best ordinary step is already-walked ground -> take the dead-end mouth",
        )


class RoamBoxCycleDetectionTests(unittest.TestCase):
    """A 2x2 roam box proves circling even though it visits 4 distinct cells."""

    def setUp(self) -> None:
        self._saved = {
            k: list(v) for k, v in tactic._roam_pos_history.items()
        }
        tactic._roam_pos_history.clear()

    def tearDown(self) -> None:
        tactic._roam_pos_history.clear()
        tactic._roam_pos_history.update(self._saved)

    def test_two_by_two_box_triggers(self) -> None:
        box = [(0, 0), (1, 0), (1, 1), (0, 1)]
        tactic._roam_pos_history["u"] = list(box * 2)
        self.assertTrue(tactic._roam_update_stall("u"))

    def test_wide_sweep_never_triggers(self) -> None:
        tactic._roam_pos_history["u"] = [(i, i % 2) for i in range(8)]
        self.assertFalse(tactic._roam_update_stall("u"))


class GuerrillaRouteRecorderTests(unittest.TestCase):
    """The day-long route recorder (runtime/guerrilla_routes-YYYYMMDD.jsonl).

    tactic_log.jsonl rotates after ~890 Ticks, so this recorder is the only
    thing that can answer "where did the guerrillas walk yesterday".
    """

    def setUp(self) -> None:
        self._names = dict(tactic._object_names)
        self._counters = dict(tactic._object_name_counters)
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        tactic._guerrilla_route_state["date"] = ""
        tactic._guerrilla_route_state["paused"] = False

    def tearDown(self) -> None:
        tactic._object_names.clear()
        tactic._object_names.update(self._names)
        tactic._object_name_counters.clear()
        tactic._object_name_counters.update(self._counters)

    @staticmethod
    def _turn():
        return SimpleNamespace(
            tick=420190,
            vanguards=[SimpleNamespace(id="vanguard-aaaa", position=(821, -421))],
            rangers=[
                SimpleNamespace(id="ranger-bbbb", position=(976, -1175)),
                SimpleNamespace(id="ranger-cccc", position=(10, 10)),
            ],
        )

    @staticmethod
    def _rows(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_records_only_guerrilla_tagged_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ARENA_DATA_DIR": tmp}), \
                    patch.object(tactic, "load_config", return_value={"guerrilla_route_log": True}), \
                    patch.object(tactic.time, "strftime", return_value="20261011"):
                tactic._record_guerrilla_routes(self._turn(), {
                    "vanguard": "MOVE:LEFT guerrilla-roam N[guerrilla]",
                    "ranger-b": "MOVE:UP guerrilla-roam S[guerrilla]",
                    "ranger-c": "WAIT:kite-hold-safe[kite]",
                })
            rows = self._rows(Path(tmp) / "guerrilla_routes-20261011.jsonl")
        self.assertEqual([row["u"] for row in rows], ["V1", "R1"])
        self.assertEqual(rows[0]["i"], "vanguard")
        self.assertEqual(rows[0]["p"], [821, -421])
        self.assertEqual(rows[0]["t"], 420190)
        self.assertIsInstance(rows[0]["e"], int)

    def test_switch_off_writes_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ARENA_DATA_DIR": tmp}), \
                    patch.object(tactic, "load_config", return_value={"guerrilla_route_log": False}), \
                    patch.object(tactic.time, "strftime", return_value="20261011"):
                tactic._record_guerrilla_routes(self._turn(), {
                    "vanguard": "MOVE:LEFT guerrilla-roam N[guerrilla]",
                })
            self.assertEqual(list(Path(tmp).glob("guerrilla_routes-*")), [])

    def test_new_day_rolls_over_and_prunes_beyond_the_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for stamp in ("20261008", "20261009", "20261010", "20261011", "junkfile"):
                (root / f"guerrilla_routes-{stamp}.jsonl").write_text("{}\n", encoding="utf-8")
            with patch.dict(os.environ, {"ARENA_DATA_DIR": tmp}), \
                    patch.object(tactic, "load_config", return_value={"guerrilla_route_log": True}), \
                    patch.object(tactic.time, "strftime", return_value="20261011"):
                tactic._record_guerrilla_routes(self._turn(), {
                    "vanguard": "MOVE:LEFT guerrilla-roam N[guerrilla]",
                })
            kept = sorted(path.name for path in root.glob("guerrilla_routes-*"))
            # Keep-days window counts from today: 11, 10, 09 stay; 08 is dropped.
            self.assertEqual(kept, [
                "guerrilla_routes-20261009.jsonl",
                "guerrilla_routes-20261010.jsonl",
                "guerrilla_routes-20261011.jsonl",
                "guerrilla_routes-junkfile.jsonl",
            ])
            self.assertEqual(len(self._rows(root / "guerrilla_routes-20261011.jsonl")), 2)

    def test_recorder_never_costs_a_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ARENA_DATA_DIR": tmp}), \
                    patch.object(tactic, "load_config", side_effect=OSError("config unreadable")):
                tactic._record_guerrilla_routes(self._turn(), {
                    "vanguard": "MOVE:LEFT guerrilla-roam N[guerrilla]",
                })
            self.assertEqual(list(Path(tmp).glob("guerrilla_routes-*")), [])

    def test_day_size_cap_pauses_recording(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ARENA_DATA_DIR": tmp}), \
                    patch.object(tactic, "load_config", return_value={"guerrilla_route_log": True}), \
                    patch.object(tactic.time, "strftime", return_value="20261011"), \
                    patch.object(tactic, "GUERRILLA_ROUTE_MAX_DAY_BYTES", 1):
                tactic._record_guerrilla_routes(self._turn(), {
                    "vanguard": "MOVE:LEFT guerrilla-roam N[guerrilla]",
                })
                before = (Path(tmp) / "guerrilla_routes-20261011.jsonl").stat().st_size
                tactic._record_guerrilla_routes(self._turn(), {
                    "vanguard": "MOVE:LEFT guerrilla-roam N[guerrilla]",
                })
                after = (Path(tmp) / "guerrilla_routes-20261011.jsonl").stat().st_size
        self.assertGreater(before, 0)
        self.assertEqual(before, after, "paused recorder must stop appending")


class GuerrillaTrackEndpointTests(unittest.TestCase):
    """Dashboard aggregation of the route files for /api/guerrilla-tracks."""

    def _write_day(self, root: Path, stamp: str, rows: list[dict]) -> None:
        (root / f"guerrilla_routes-{stamp}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def _rows(self) -> list[dict]:
        # V1: a deterministic 3-cell loop (many Ticks, almost no ground covered).
        # R2: a real march, including a repeated cell to fold.
        loop = [(821, -421), (820, -421), (819, -421)]
        rows = [{"t": 100 + index, "e": 1000 + index, "u": "V1", "i": "vanguard-a",
                 "p": list(loop[index % 3])} for index in range(30)]
        rows.append({"t": 100, "e": 1000, "u": "V1", "i": "vanguard-a", "p": [821, -421]})
        for index in range(10):
            rows.append({"t": 200 + index, "e": 2000 + index, "u": "R2", "i": "ranger-b",
                         "p": [0, index]})
            rows.append({"t": 200 + index, "e": 2000 + index, "u": "R2", "i": "ranger-b",
                         "p": [0, index]})
        return rows

    def test_aggregates_loops_marches_and_ignores_junk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_day(root, time.strftime("%Y%m%d"), self._rows())
            self._write_day(root, "20260101", [{"t": 1, "e": 1, "u": "V1", "i": "x", "p": [5, 5]}])
            (root / "guerrilla_routes-notes.jsonl").write_text("{}\n", encoding="utf-8")
            with patch.dict(os.environ, {"ARENA_DATA_DIR": tmp}):
                data = dashboard.read_guerrilla_tracks()
            self.assertEqual(data["date"], time.strftime("%Y%m%d"))
            self.assertEqual(data["dates"], [time.strftime("%Y%m%d"), "20260101"])
            by_id = {unit["id"]: unit for unit in data["units"]}
            self.assertEqual(len(by_id), 2, "non-dated files must not be read")
            loop = by_id["vanguard-a"]
            march = by_id["ranger-b"]
            self.assertEqual(loop["type"], "VANGUARD")
            self.assertEqual(march["type"], "RANGER")
            # 31 rows, 3 distinct cells, back where it started.
            self.assertEqual((loop["ticks"], loop["distinct"], loop["net"]), (31, 3, 0))
            self.assertEqual(loop["bounds"], [819, -421, 821, -421])
            # Consecutive duplicate cells fold: 20 rows but 10 walked cells.
            self.assertEqual((march["ticks"], march["distinct"], march["net"]), (20, 10, 9))
            self.assertEqual(march["points"], [[0, index] for index in range(10)])

    def test_decimation_keeps_the_newest_cell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [{"t": index, "e": 1000 + index, "u": "R2", "i": "ranger-b",
                     "p": [index, 0]} for index in range(100)]
            self._write_day(root, time.strftime("%Y%m%d"), rows)
            with patch.dict(os.environ, {"ARENA_DATA_DIR": tmp}):
                data = dashboard.read_guerrilla_tracks(max_points=7)
        points = data["units"][0]["points"]
        # stride = ceil(100 / 7) = 15 → cells 0,15,…,90 plus the always-kept last.
        self.assertEqual(len(points), 8)
        self.assertEqual(points[0], [0, 0])
        self.assertEqual(points[-1], [99, 0])

    def test_cache_throttles_reparsing_a_growing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stamp = time.strftime("%Y%m%d")
            path = root / f"guerrilla_routes-{stamp}.jsonl"
            self._write_day(root, stamp, [{"t": 1, "e": 1000, "u": "V1", "i": "a", "p": [0, 0]}])
            with patch.dict(os.environ, {"ARENA_DATA_DIR": tmp}):
                first = dashboard.read_guerrilla_tracks()
                self._write_day(root, stamp, [
                    {"t": 1, "e": 1000, "u": "V1", "i": "a", "p": [0, 0]},
                    {"t": 2, "e": 1001, "u": "V1", "i": "a", "p": [1, 0]},
                ])
                throttled = dashboard.read_guerrilla_tracks()
                with patch.object(dashboard, "GUERRILLA_TRACK_REFRESH_SECONDS", 0.0):
                    refreshed = dashboard.read_guerrilla_tracks()
        self.assertEqual(throttled["units"][0]["ticks"], first["units"][0]["ticks"])
        self.assertEqual(refreshed["units"][0]["ticks"], 2)

    def test_http_endpoint_serves_the_tracks(self) -> None:
        from http.server import ThreadingHTTPServer

        with tempfile.TemporaryDirectory() as tmp:
            self._write_day(Path(tmp), time.strftime("%Y%m%d"), self._rows())
            server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                import http.client

                with patch.dict(os.environ, {"ARENA_DATA_DIR": tmp}):
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                    conn.request("GET", "/api/guerrilla-tracks?max_points=200")
                    response = conn.getresponse()
                    status_code = response.status
                    body = json.loads(response.read())
                    conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
        self.assertEqual(status_code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["units"]), 2)


class SweepMissFuseTests(KiteTeamPlannerTests):
    """横扫空枪熔断 (sweep miss fuse).

    Production proof from the route recorder plus battle_log: V15 stood at
    (889,-920) for ~2000 Ticks and issued 407 sweeps that hit 0 targets — every
    ~5 Ticks, forever, while it was shot down to HP 1. A sweep resolves AFTER
    movement, so aiming at the cell the enemy occupies this Tick misses every
    time that enemy steps aside, and the planner re-aimed at the same cell with
    no memory of having wasted 406 swings already.
    """

    def setUp(self):
        super().setUp()
        self._saved_streak = dict(tactic._sweep_miss_streak)
        self._saved_until = dict(tactic._sweep_fuse_until)
        tactic._sweep_miss_streak.clear()
        tactic._sweep_fuse_until.clear()
        tactic.turn_context.tick = 0

    def tearDown(self):
        tactic._sweep_miss_streak.clear()
        tactic._sweep_miss_streak.update(self._saved_streak)
        tactic._sweep_fuse_until.clear()
        tactic._sweep_fuse_until.update(self._saved_until)
        super().tearDown()

    def _turn(self, unit, outcomes):
        """A turn carrying one SWEEP_RESOLVED event per outcome value."""
        group = "rangers" if unit.unit_type == UnitType.RANGER else "vanguards"
        kwargs = {"workers": (), "vanguards": (), "rangers": ()}
        kwargs[group] = (unit,)
        return SimpleNamespace(
            **kwargs,
            events=tuple(
                SimpleNamespace(
                    event_type="SWEEP_RESOLVED",
                    actor_id=unit.id,
                    values={"targets_hit": hits},
                )
                for hits in outcomes
            ),
        )

    def test_three_zero_hit_sweeps_refuse_the_current_cell_sweep(self):
        unit = self.unit("fuse-vanguard", (889, -920))
        enemy = self.enemy("fuse-enemy", (888, -920))

        # Baseline: an enemy standing on the adjacent cell gets swept.
        tactic.turn_context.tick = 0
        action, detail = tactic._vanguard_adjacent_sweep(unit, (889, -920), (enemy,))
        self.assertEqual(action, "SWEEP")
        self.assertEqual(unit.action, "SWEEP")

        # Two misses is not yet proof — real fights miss sometimes.
        tactic._record_sweep_resolutions(self._turn(unit, [0, 0]))
        self.assertFalse(tactic._sweep_fused(unit))
        self.assertIsNotNone(
            tactic._vanguard_adjacent_sweep(unit, (889, -920), (enemy,))[0]
        )

        # The third consecutive miss arms the fuse: the same aim is refused, and
        # no sweep is issued at all (the caller falls through to prediction or
        # to closing the distance).
        tactic._record_sweep_resolutions(self._turn(unit, [0]))
        self.assertTrue(tactic._sweep_fused(unit))
        unit.action = None
        self.assertIsNone(
            tactic._vanguard_adjacent_sweep(unit, (889, -920), (enemy,))
        )
        self.assertIsNone(unit.action)

    def test_one_landing_sweep_clears_the_debt(self):
        unit = self.unit("fuse-reset", (0, 0))
        tactic._record_sweep_resolutions(self._turn(unit, [0, 0, 1]))
        self.assertFalse(tactic._sweep_fused(unit))
        self.assertNotIn(str(unit.id), tactic._sweep_miss_streak)
        self.assertNotIn(str(unit.id), tactic._sweep_fuse_until)

    def test_fuse_is_a_cooldown_not_a_disarmament(self):
        unit = self.unit("fuse-expiry", (0, 0))
        enemy = self.enemy("fuse-expiry-e", (1, 0))
        tactic._record_sweep_resolutions(self._turn(unit, [0, 0, 0]))
        self.assertTrue(tactic._sweep_fused(unit))
        tactic.turn_context.tick = tactic._SWEEP_FUSE_TICKS
        self.assertFalse(tactic._sweep_fused(unit))
        self.assertEqual(
            tactic._vanguard_adjacent_sweep(unit, (0, 0), (enemy,))[0], "SWEEP"
        )

    def test_enemy_misses_never_enter_the_ledger(self):
        # The ledger is keyed by unit id and pruned with the unit; recording an
        # enemy's wasted swings would grow it without bound on dead enemy ids.
        unit = self.unit("fuse-ally", (0, 0))
        turn = SimpleNamespace(
            workers=(), vanguards=(unit,), rangers=(),
            events=(SimpleNamespace(
                event_type="SWEEP_RESOLVED", actor_id="some-enemy-id",
                values={"targets_hit": 0},
            ),),
        )
        tactic._record_sweep_resolutions(turn)
        self.assertEqual(tactic._sweep_miss_streak, {})

    def test_marker_is_greppable_while_the_fuse_is_armed(self):
        # A silent safety mechanism is a mechanism that does not exist: the
        # detail must say so, because that string is what an inspection greps.
        unit = self.unit("fuse-marker", (0, 0))
        self.assertEqual(tactic._sweep_fuse_note(unit), "")
        tactic._record_sweep_resolutions(self._turn(unit, [0, 0, 0]))
        self.assertEqual(tactic._sweep_fuse_note(unit), " sweep-fuse")

    def test_full_vanguard_trade_is_gated_by_the_same_fuse(self):
        # The other current-cell sweep a guerrilla vanguard issues: a full
        # health vanguard adjacent to a ranger trades blows at the ranger's
        # present cell, which is exactly the aim that missed 407 times.
        unit = self.unit("fuse-trade", (0, 0))
        ranger = self.enemy("fuse-trade-r", (1, 0), UnitType.RANGER)
        config = dict(self.config)
        config["guerrilla_engage_radius"] = 10
        kwargs = {
            "unit_kind": "vanguard",
            "enemies": (ranger,),
            "obstacle_cells": frozenset(),
            "config": config,
            "solo": True,
        }
        # A/B: the same geometry without the fuse trades blows as before.
        self.assertEqual(
            tactic._plan_kite_combat(unit, **kwargs)[0], "SWEEP",
        )
        tactic._record_sweep_resolutions(self._turn(unit, [0, 0, 0]))
        action, detail = tactic._plan_kite_combat(unit, **kwargs)
        self.assertNotEqual(action, "SWEEP")
        self.assertIn("full-vanguard-close", detail)

    def test_switch_off_loses_the_ledger_and_the_armed_fuse(self):
        unit = self.unit("fuse-switch", (0, 0))
        tactic._record_sweep_resolutions(self._turn(unit, [0, 0, 0]))
        self.assertTrue(tactic._sweep_fused(unit))
        # The dashboard switch has to be able to un-arm an existing fuse, not
        # merely stop new ones: otherwise turning it off changes nothing for
        # 40 Ticks.
        tactic._record_sweep_resolutions(self._turn(unit, []), enabled=False)
        self.assertFalse(tactic._sweep_fused(unit))
        self.assertEqual(tactic._sweep_miss_streak, {})
        self.assertEqual(tactic._sweep_fuse_until, {})

    def test_dead_unit_fuse_state_is_pruned(self):
        unit = self.unit("fuse-prune", (0, 0))
        tactic._record_sweep_resolutions(self._turn(unit, [0, 0, 0]))
        self.assertIn(str(unit.id), tactic._sweep_fuse_until)
        tactic._prune_dead_unit_bookkeeping(set())
        self.assertNotIn(str(unit.id), tactic._sweep_fuse_until)
        self.assertNotIn(str(unit.id), tactic._sweep_miss_streak)


class RoamOrbitBreakTests(KiteTeamPlannerTests):
    """宏观绕圈拆解 (orbit break) — the failure the 8-Tick stall window cannot see.

    Three days of runtime/guerrilla_routes-*.jsonl (145,972 guerrilla steps):
    V2 spent all of 10-13 re-walking 426 cells inside a 150x48 pocket with a
    ~468-Tick period, V13 471 cells in 102x102, V15 3889 steps over 25 cells in
    an 18x5 box. Every short window of those trails looks like fresh ground, so
    ``_roam_update_stall`` never fired once. The separating statistic is a stall
    in NEW ground: those units went 2,120 / 4,559 / 4,703 recorded steps between
    their last two new cells, while every unit that kept exploring stayed under
    361.
    """

    def setUp(self):
        super().setUp()
        self._saved_discovered = {
            uid: dict(cells) for uid, cells in tactic._roam_discovered.items()
        }
        self._saved_stall = dict(tactic._roam_new_stall)
        self._saved_recent = {
            uid: list(cells) for uid, cells in tactic._roam_recent.items()
        }
        self._saved_break = dict(tactic._roam_break_tick)
        self._saved_history = {
            uid: list(cells) for uid, cells in tactic._roam_pos_history.items()
        }
        self._saved_cells = dict(tactic._roam_stall_cells)
        self._saved_origin = dict(tactic._roam_stall_origin)
        self._saved_escape = dict(tactic._roam_escape_left)
        self._saved_banned = {
            uid: dict(cells) for uid, cells in tactic._roam_banned.items()
        }
        self._saved_offset = dict(tactic._roam_bearing_offset)
        self._saved_flip = dict(tactic._roam_flip_pending)
        self._saved_flips_used = dict(tactic._roam_flips_used)
        self._saved_dead = tactic._dead_obstacles
        for store in (
            tactic._roam_discovered, tactic._roam_new_stall, tactic._roam_recent,
            tactic._roam_break_tick, tactic._roam_pos_history,
            tactic._roam_stall_cells, tactic._roam_stall_origin,
            tactic._roam_escape_left, tactic._roam_banned,
            tactic._roam_bearing_offset, tactic._roam_flip_pending,
            tactic._roam_flips_used,
        ):
            store.clear()
        tactic._dead_obstacles = None
        tactic.turn_context.tick = 0

    def tearDown(self):
        tactic._roam_discovered.clear()
        tactic._roam_discovered.update(self._saved_discovered)
        tactic._roam_new_stall.clear()
        tactic._roam_new_stall.update(self._saved_stall)
        tactic._roam_recent.clear()
        tactic._roam_recent.update(self._saved_recent)
        tactic._roam_break_tick.clear()
        tactic._roam_break_tick.update(self._saved_break)
        tactic._roam_pos_history.clear()
        tactic._roam_pos_history.update(self._saved_history)
        tactic._roam_stall_cells.clear()
        tactic._roam_stall_cells.update(self._saved_cells)
        tactic._roam_stall_origin.clear()
        tactic._roam_stall_origin.update(self._saved_origin)
        tactic._roam_escape_left.clear()
        tactic._roam_escape_left.update(self._saved_escape)
        tactic._roam_banned.clear()
        tactic._roam_banned.update(self._saved_banned)
        tactic._roam_bearing_offset.clear()
        tactic._roam_bearing_offset.update(self._saved_offset)
        tactic._roam_flip_pending.clear()
        tactic._roam_flip_pending.update(self._saved_flip)
        tactic._roam_flips_used.clear()
        tactic._roam_flips_used.update(self._saved_flips_used)
        tactic._dead_obstacles = self._saved_dead
        super().tearDown()

    @staticmethod
    def _pocket_circuit(width=150, height=48):
        """V2's shape: the perimeter circuit of a 150x48 pocket, 392 cells."""
        return (
            [(x, 0) for x in range(width)]
            + [(width - 1, y) for y in range(1, height)]
            + [(x, height - 1) for x in range(width - 2, -1, -1)]
            + [(0, y) for y in range(height - 2, 0, -1)]
        )

    def test_threshold_sits_inside_the_measured_gap(self):
        # The constant is data, not taste: the fleet that kept exploring never
        # went longer than 361 recorded steps without a new cell, and the three
        # units that ended a day orbiting never shorter than 2,120.
        self.assertGreater(tactic._ROAM_ORBIT_STALL_STEPS, 361)
        self.assertLess(tactic._ROAM_ORBIT_STALL_STEPS, 2120)

    def test_production_scale_loop_is_proven(self):
        circuit = self._pocket_circuit()
        self.assertEqual(len(circuit), 392)
        proven = [
            tactic._roam_record_discovery("v2", circuit[index % len(circuit)])
            for index in range(5170)
        ]
        self.assertIn(True, proven)
        # Earliest possible proof: the first lap is all new ground, then a full
        # stall of revisits.
        self.assertEqual(
            proven.index(True), 392 + tactic._ROAM_ORBIT_STALL_STEPS - 1
        )

    def test_genuine_exploration_is_never_proven(self):
        # False-positive guard, learned the hard way from the V4 驻守 misfire.
        stall = tactic._ROAM_ORBIT_STALL_STEPS
        for index in range(stall * 20):
            self.assertFalse(
                tactic._roam_record_discovery("explorer", (index, index // 2))
            )

    def test_extending_patrol_is_not_an_orbit(self):
        # Repetition alone is not the trigger — a stall in NEW ground is. A unit
        # shuttling over a patrol line whose far end grows one cell a lap walks
        # onto fresh ground before the stall bound can be reached.
        proven = []
        for lap in range(60):
            far = 100 + lap
            path = (
                [(index, 0) for index in range(far)]
                + [(index, 0) for index in range(far - 1, -1, -1)]
            )
            for cell in path:
                proven.append(tactic._roam_record_discovery("patrol", cell))
        self.assertNotIn(True, proven)

    def test_stationary_hold_records_nothing(self):
        # Consecutive duplicates are folded, so a deliberate hold can never fill
        # the ledger — the exact misfire the stall-tick gate produced on V4.
        for _ in range(tactic._ROAM_ORBIT_STALL_STEPS * 3):
            self.assertFalse(tactic._roam_record_discovery("holder", (889, -920)))
        self.assertEqual(len(tactic._roam_recent["holder"]), 1)
        self.assertEqual(tactic._roam_new_stall.get("holder", 0), 0)

    def test_ledger_sees_the_loop_that_the_short_window_is_blind_to(self):
        # The A/B proving this is not a re-test of the old gate: replaying V2's
        # circuit leaves _roam_update_stall silent while the ledger confirms an
        # orbit. The walk length is a fixed three laps, NOT derived from the
        # bound — otherwise disabling the bound would silently shorten the walk
        # and this test would stay green with the detector switched off.
        circuit = self._pocket_circuit()
        history = tactic._roam_pos_history.setdefault("v2", [])
        stall_seen = False
        proven = False
        for index in range(3 * len(circuit)):
            pos = circuit[index % len(circuit)]
            if not history or history[-1] != pos:
                history.append(pos)
                del history[:-tactic._ROAM_STALL_REGION]
            stall_seen = stall_seen or tactic._roam_update_stall("v2")
            if tactic._roam_record_discovery("v2", pos):
                proven = True
                break
        self.assertTrue(proven, "the ledger never confirmed the production orbit")
        self.assertFalse(stall_seen)

    def test_confirmed_orbit_arms_the_escape_and_rebaselines(self):
        with (
            patch.object(tactic, "_ROAM_ORBIT_STALL_STEPS", 24),
            patch.object(tactic, "_ROAM_ORBIT_REGION", 12),
            patch.object(tactic, "_ROAM_ORBIT_ESCAPE_TICKS", 33),
        ):
            unit = self.unit("orbit-e2e", (0, 0))
            circuit = [(index % 4, (index // 4) % 4) for index in range(16)]
            detail = ""
            for step in range(45):
                action, detail = tactic._guerrilla_roam(
                    unit, circuit[step % len(circuit)], frozenset(), self.config,
                )
                if "orbit-break" in detail:
                    break
            self.assertIn("orbit-break", detail)
            self.assertEqual(tactic._roam_escape_left["orbit-e2e"], 33)
            self.assertGreaterEqual(len(tactic._roam_stall_cells["orbit-e2e"]), 4)
            # Banning the whole loop while the unit still stands inside it would
            # leave no legal step and trade an orbit for a freeze, so only the
            # approach tail is forbidden.
            self.assertLessEqual(
                len(tactic._roam_banned.get("orbit-e2e", {})),
                tactic._ROAM_STALL_REGION,
            )
            # Re-baselined: another full stall is needed before it fires again.
            self.assertEqual(tactic._roam_new_stall["orbit-e2e"], 0)

    def test_orbit_switch_off_disables_the_ledger_too(self):
        with patch.object(tactic, "_ROAM_ORBIT_STALL_STEPS", 8):
            config = dict(self.config)
            config["roam_orbit_break"] = False
            unit = self.unit("orbit-off", (0, 0))
            circuit = [(index % 4, (index // 4) % 4) for index in range(16)]
            details = []
            for step in range(30):
                _, detail = tactic._guerrilla_roam(
                    unit, circuit[step % len(circuit)], frozenset(), config,
                )
                details.append(detail)
            self.assertNotIn(True, ["orbit-break" in d for d in details])
            self.assertNotIn("orbit-off", tactic._roam_discovered)

    def test_the_a_b_a_escape_switch_does_not_own_the_orbit_detector(self):
        # Two independent levers: the dashboard must be able to disable the
        # long-horizon detector without switching off the two-cell bounce escape
        # it shares, and the other way round.
        with patch.object(tactic, "_ROAM_ORBIT_STALL_STEPS", 8):
            config = dict(self.config)
            config["roam_oscillation_escape"] = False
            config["roam_orbit_break"] = True
            unit = self.unit("orbit-only", (0, 0))
            circuit = [(index % 4, (index // 4) % 4) for index in range(16)]
            for step in range(30):
                _, detail = tactic._guerrilla_roam(
                    unit, circuit[step % len(circuit)], frozenset(), config,
                )
                if "orbit-break" in detail:
                    break
            self.assertIn("orbit-break", detail)

    def test_dead_unit_orbit_state_is_pruned(self):
        tactic._roam_record_discovery("orbit-die", (0, 0))
        tactic._roam_break_tick["orbit-die"] = 1
        tactic._prune_dead_unit_bookkeeping(set())
        self.assertNotIn("orbit-die", tactic._roam_discovered)
        self.assertNotIn("orbit-die", tactic._roam_recent)
        self.assertNotIn("orbit-die", tactic._roam_new_stall)
        self.assertNotIn("orbit-die", tactic._roam_break_tick)



class GuerrillaFrontierRoamTests(unittest.TestCase):
    """游击前沿探索 (guerrilla_frontier_roam) — 不再抱着一个 ID 派生方位不放。

    Production 15h window after the orbit-break gate shipped: V1 re-walked 25.7%
    of its steps, V2 21.9%, R14 20.1%, V5 20.0% — ground the unit had already
    stood on 3+ times. The gate catches a pocket only after 500 recorded steps
    with no new cell; the fixed bearing is what keeps walking them back in.
    Replaying the five real break events on today's 509,299-cell wall memory:
    the old arm rewalked 22.1% on average and re-triggered the orbit gate 2 of 5
    times, the new arm 0.6% and zero, with no WAIT freeze in either.
    """

    class SimUnit:
        def __init__(self, uid, position):
            self.id = uid
            self.position = position
            self.arg = None

        def move(self, direction):
            self.arg = direction

        def wait(self):
            self.arg = None

    _STORES = (
        "_roam_pos_history", "_roam_escape_left", "_roam_stall_cells",
        "_roam_stall_origin", "_roam_bearing_offset", "_roam_flip_pending",
        "_roam_flips_used", "_roam_banned", "_roam_discovered",
        "_roam_new_stall", "_roam_recent", "_roam_break_tick", "_roam_seen",
    )

    def setUp(self):
        self.config = default_config()
        self._saved = {
            name: {uid: copy.deepcopy(value)
                   for uid, value in getattr(tactic, name).items()}
            for name in self._STORES
        }
        for name in self._STORES:
            getattr(tactic, name).clear()
        tactic.turn_context.tick = 0

    def tearDown(self):
        for name, saved in self._saved.items():
            store = getattr(tactic, name)
            store.clear()
            store.update(saved)

    def _seed_line(self, uid, origin, length=12):
        """Mark the ground along this unit's fixed bearing (both ways) as seen."""
        base = tactic._stable_slot_index(uid, len(tactic._EIGHT_WAY_DELTAS))
        dx, dy = tactic._EIGHT_WAY_DELTAS[base]
        cells = {}
        for step in range(-length, length + 1):
            cells[(origin[0] + dx * step, origin[1] + dy * step)] = None
        tactic._roam_seen[uid] = cells
        return base, cells

    def test_virgin_terrain_is_byte_for_byte_the_old_policy(self) -> None:
        # Nothing visited yet => every bearing ties => the rotation starts at the
        # base bearing => the fleet keeps the spread of headings it has today.
        unit = self.SimUnit("frontier-virgin", (500, 500))
        config_off = dict(self.config)
        config_off["guerrilla_frontier_roam"] = False
        tactic.turn_context.tick = 1
        _a1, d1 = tactic._guerrilla_roam(unit, (500, 500), frozenset(), config_off)
        tactic._roam_seen.clear(); tactic._roam_pos_history.clear()
        _a2, d2 = tactic._guerrilla_roam(
            self.SimUnit("frontier-virgin", (500, 500)), (500, 500), frozenset(), self.config,
        )
        self.assertEqual(d1, d2)

    def test_aimed_at_fresh_ground_instead_of_its_own_trail(self) -> None:
        uid = "frontier-turn"
        origin = (40, 40)
        base, seen = self._seed_line(uid, origin)
        unit = self.SimUnit(uid, origin)
        tactic.turn_context.tick = 3
        _a, detail_off = tactic._guerrilla_roam(
            unit, origin, frozenset(), {**self.config, "guerrilla_frontier_roam": False},
        )
        cell_off = (origin[0] + tactic._EIGHT_WAY_DELTAS[base][0],
                    origin[1] + tactic._EIGHT_WAY_DELTAS[base][1])
        self.assertIn(cell_off, seen)
        self.assertIn(tactic._EIGHT_WAY_LABELS[base], detail_off)

        tactic._roam_pos_history.clear()
        unit = self.SimUnit(uid, origin)
        tactic.turn_context.tick = 3
        action, detail_on = tactic._guerrilla_roam(
            unit, origin, frozenset(), self.config,
        )
        self.assertEqual(action, "MOVE")
        dx, dy = unit.arg.delta
        # The old heading lands on ground it has stood on; the frontier one does
        # not, and it is a different heading — that is the whole change.
        self.assertNotEqual(detail_off.split()[0], detail_on.split()[0])
        self.assertNotIn((origin[0] + dx, origin[1] + dy), seen)

    def test_a_dead_end_still_backs_out_instead_of_freezing(self) -> None:
        # Everything legal is behind the unit. Skipping blocked headings must not
        # turn "no fresh ground ahead" into a WAIT: a frozen unit explores less
        # than one that orbits, and the gate cannot help a unit that never moves.
        uid = "frontier-deadend"
        origin = (0, 0)
        walls = frozenset(
            {(x, y) for x in range(-2, 3) for y in range(-2, 3)}
            - {(0, 0), (0, 1)}
        )
        tactic._roam_seen[uid] = {(0, 0): None}
        unit = self.SimUnit(uid, origin)
        tactic.turn_context.tick = 1
        action, detail = tactic._guerrilla_roam(unit, origin, walls, self.config)
        self.assertEqual(action, "MOVE")
        self.assertIn(unit.arg.delta, [(0, 1), (1, 1), (1, 0)])

    def test_the_escape_phase_is_not_resteered(self) -> None:
        # Blast radius: the two-phase escape is measured-good in production (all
        # three orbit breaks in the 15h window worked). Frontier scoring must not
        # touch a unit that is escaping.
        uid = "frontier-escape"
        pos = (5, 5)
        tactic._roam_escape_left[uid] = 4
        tactic._roam_stall_cells[uid] = frozenset(
            [(x, y) for x in range(3, 8) for y in range(3, 8)]
        )
        tactic._roam_stall_origin[uid] = (4, 5)
        tactic._roam_seen[uid] = {pos: None}
        unit = self.SimUnit(uid, pos)
        with patch.object(
            tactic, "_roam_frontier_bearing",
            side_effect=AssertionError("frontier steering ran during an escape"),
        ):
            _action, detail = tactic._guerrilla_roam(
                unit, pos, frozenset(), self.config,
            )
        self.assertIn("escape", detail)

    def test_frontier_memory_outlives_the_orbit_ledger(self) -> None:
        # The detector wants a fresh baseline after a break; the heading chooser
        # must keep remembering where it has already been, or the first hundred
        # steps after every break would look like virgin ground again.
        uid = "frontier-memory"
        tactic._roam_record_seen(uid, (1, 1))
        tactic._roam_discovered[uid] = {(1, 1): None}
        tactic._roam_reset_discovery(uid)
        self.assertNotIn(uid, tactic._roam_discovered)
        self.assertIn((1, 1), tactic._roam_seen[uid])

    def test_frontier_memory_is_bounded(self) -> None:
        uid = "frontier-bound"
        with patch.object(tactic, "_ROAM_FRONTIER_MEMORY", 50):
            for index in range(400):
                tactic._roam_record_seen(uid, (index, 0))
            self.assertEqual(len(tactic._roam_seen[uid]), 50)
            self.assertIn((399, 0), tactic._roam_seen[uid])
            self.assertNotIn((0, 0), tactic._roam_seen[uid])

    def test_dead_unit_frontier_memory_is_pruned(self) -> None:
        tactic._roam_record_seen("frontier-die", (0, 0))
        tactic._prune_dead_unit_bookkeeping(set())
        self.assertNotIn("frontier-die", tactic._roam_seen)


class GuerrillaFrontierRoamConfigTests(unittest.TestCase):
    """The new policy ships behind its own switch so production can turn the
    roaming behaviour back without a redeploy — the same lever that made the two
    gates in the last deployment safe to try."""

    def test_field_registered_and_default_on(self) -> None:
        keys = {field.key for field in tactic_config.CONFIG_FIELDS}
        self.assertIn("guerrilla_frontier_roam", keys)
        self.assertTrue(default_config()["guerrilla_frontier_roam"])

    def test_validation_rejects_non_boolean(self) -> None:
        self.assertFalse(
            tactic_config.validate_config({"guerrilla_frontier_roam": False})[
                "guerrilla_frontier_roam"
            ]
        )
        with self.assertRaises(tactic_config.ConfigValidationError):
            tactic_config.validate_config({"guerrilla_frontier_roam": "yes"})


class OrbitBreakAndSweepFuseConfigTests(unittest.TestCase):
    """Both new gates ship as registered booleans so production can turn either
    one off without a redeploy (the same reason roam_oscillation_escape exists)."""

    def test_fields_registered_and_default_on(self) -> None:
        keys = {field.key for field in tactic_config.CONFIG_FIELDS}
        self.assertIn("roam_orbit_break", keys)
        self.assertIn("sweep_miss_fuse", keys)
        config = default_config()
        self.assertTrue(config["roam_orbit_break"])
        self.assertTrue(config["sweep_miss_fuse"])

    def test_validation_rejects_non_boolean(self) -> None:
        for key in ("roam_orbit_break", "sweep_miss_fuse"):
            self.assertFalse(tactic_config.validate_config({key: False})[key])
            with self.assertRaises(tactic_config.ConfigValidationError):
                tactic_config.validate_config({key: "yes"})


if __name__ == "__main__":
    unittest.main()