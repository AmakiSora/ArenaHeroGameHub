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

    def test_save_and_hot_reload_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tactic_config.json"
            config = default_config()
            config["bfs_max_steps"] = 1250
            config["core_movement_enabled"] = False

            saved = save_config(config, path)
            loaded = load_config(path)

        self.assertEqual(saved, loaded)
        self.assertEqual(loaded["bfs_max_steps"], 1250)
        self.assertFalse(loaded["core_movement_enabled"])

    def test_invalid_and_incomplete_values_are_rejected(self) -> None:
        with self.assertRaises(ConfigValidationError):
            validate_config({"ranger_attack_range": 4})

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ConfigValidationError):
                save_config({"bfs_max_steps": 800}, Path(temp_dir) / "config.json")

    def test_panel_contains_every_config_field_below_map(self) -> None:
        panel = dashboard.render_config_panel()
        page = dashboard.generate_html()

        for field in CONFIG_FIELDS:
            self.assertIn(f'name="{field.key}"', panel)
        self.assertGreater(page.index("策略配置"), page.index('id="mapStage"'))


if __name__ == "__main__":
    unittest.main()
