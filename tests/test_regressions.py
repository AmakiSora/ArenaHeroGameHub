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

    def test_object_names_are_stable_and_sequential(self) -> None:
        tactic._object_names.clear()
        tactic._object_name_counters.clear()

        self.assertEqual(tactic._object_name("a", "W"), "W1")
        self.assertEqual(tactic._object_name("b", "W"), "W2")
        self.assertEqual(tactic._object_name("a", "W"), "W1")
        self.assertEqual(tactic._object_name("enemy", "E"), "E1")


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

    def tearDown(self) -> None:
        tactic._worker_last_pos.clear()
        tactic._worker_last_pos.update(self._prev_last_pos)

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
        self.assertIn('class="worker-target"', svg)
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

            with patch.object(dashboard, "LOG_FILE", str(log_path)):
                history = dashboard.read_history(3)

        self.assertEqual([record["tick"] for record in history], [79, 78, 77])


if __name__ == "__main__":
    unittest.main()
