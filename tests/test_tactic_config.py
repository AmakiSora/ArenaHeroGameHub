from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import dashboard
from tactic_config import (
    CONFIG_FIELDS,
    ConfigValidationError,
    default_config,
    load_config,
    save_config,
    validate_config,
)


class TacticConfigTests(unittest.TestCase):
    def test_defaults_preserve_existing_strategy(self) -> None:
        config = default_config()

        self.assertEqual(config["bfs_max_steps"], 800)
        self.assertEqual(config["cargo_wait_distance"], 5)
        self.assertEqual(config["combat_shield_target"], 3)
        self.assertTrue(config["core_movement_enabled"])
        self.assertEqual(config["home_team"], "")
        self.assertEqual(config["attack_team"], "")
        self.assertEqual(config["guerrilla_team"], "")
        self.assertEqual(config["home_patrol_radius"], 5)
        self.assertEqual(config["attack_target_x"], 0)
        self.assertEqual(config["attack_target_y"], 0)

    def test_save_and_hot_reload_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tactic_config.json"
            config = default_config()
            config["bfs_max_steps"] = 1250
            config["core_movement_enabled"] = False
            config["home_team"] = "V1, R1"
            config["attack_team"] = "V2"
            config["guerrilla_team"] = "R2,R3"
            config["home_patrol_radius"] = 7
            config["attack_target_x"] = 12
            config["attack_target_y"] = -4

            saved = save_config(config, path)
            loaded = load_config(path)

        self.assertEqual(saved, loaded)
        self.assertEqual(loaded["bfs_max_steps"], 1250)
        self.assertFalse(loaded["core_movement_enabled"])
        self.assertEqual(loaded["home_team"], "V1, R1")
        self.assertEqual(loaded["attack_target_x"], 12)
        self.assertEqual(loaded["home_patrol_radius"], 7)

    def test_invalid_and_incomplete_values_are_rejected(self) -> None:
        with self.assertRaises(ConfigValidationError):
            validate_config({"ranger_attack_range": 4})

        with self.assertRaises(ConfigValidationError):
            validate_config({"home_team": 123})

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ConfigValidationError):
                save_config({"bfs_max_steps": 800}, Path(temp_dir) / "config.json")

    def test_legacy_engage_flags_are_ignored(self) -> None:
        config = validate_config({
            **default_config(),
            "vanguard_engage_enabled": False,
            "ranger_engage_enabled": True,
            "home_team": "V1",
        })

        self.assertEqual(config["home_team"], "V1")
        self.assertNotIn("vanguard_engage_enabled", config)

    def test_panel_contains_every_config_field_below_map(self) -> None:
        panel = dashboard.render_config_panel()
        teams = dashboard.render_teams_panel()
        page = dashboard.generate_html()

        for field in CONFIG_FIELDS:
            if field.group == "combat":
                continue
            self.assertIn(f'name="{field.key}"', panel)
        for unit_type in ("WORKER", "VANGUARD", "RANGER"):
            self.assertIn(f'data-queue-unit="{unit_type}"', panel)
        self.assertIn('id="productionQueueList"', panel)
        self.assertNotIn('name="home_team"', panel)
        self.assertNotIn('name="attack_team"', panel)
        self.assertNotIn('name="guerrilla_team"', panel)
        self.assertNotIn("录入矿点", panel)
        self.assertIn('id="teamsPanel"', teams)
        self.assertIn("team-board", teams)
        self.assertIn("守家队", teams)
        self.assertIn("进攻队", teams)
        self.assertIn("游击队", teams)
        self.assertIn("待命池", teams)
        self.assertIn("resAddToggle", page)
        self.assertIn("resAddForm", page)
        self.assertIn("chip-x", page)
        self.assertGreater(page.index("策略配置"), page.index('id="mapStage"'))
        self.assertGreater(page.index('id="teamsPanel"'), page.index('id="mapStage"'))
        self.assertLess(page.index('id="teamsPanel"'), page.index("策略配置"))

    def test_collect_combat_units_merges_live_and_roster(self) -> None:
        config = default_config()
        config["home_team"] = "V1"
        config["attack_team"] = "V2"
        config["guerrilla_team"] = "R9"
        rec = {
            "vanguards": [{"name": "V1", "id": "aaaa1111", "pos": [1, 2], "hp": 4}],
            "rangers": [{"name": "R1", "id": "bbbb2222", "pos": [3, 4], "hp": 3}],
            "plan_unit_actions": {
                "aaaa1111": "MOVE:home[home]",
                "bbbb2222": "MOVE:scout[unassigned]",
            },
        }

        units = dashboard.collect_combat_units(rec, config)
        by_name = {unit["name"]: unit for unit in units}

        self.assertEqual(by_name["V1"]["team"], "home")
        self.assertTrue(by_name["V1"]["alive"])
        self.assertEqual(by_name["V2"]["team"], "attack")
        self.assertFalse(by_name["V2"]["alive"])
        self.assertEqual(by_name["R1"]["team"], "unassigned")
        self.assertTrue(by_name["R1"]["alive"])
        self.assertEqual(by_name["R9"]["team"], "guerrilla")


if __name__ == "__main__":
    unittest.main()
