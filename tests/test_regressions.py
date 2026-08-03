from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import dashboard
import status
import tactic
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

        self.assertEqual(saved["resources"], [list(active_resource)])
        self.assertEqual(saved["manual_resources"], [])


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
            with patch.object(tactic, "save_config", side_effect=lambda values: save_config(values, path)):
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


if __name__ == "__main__":
    unittest.main()
