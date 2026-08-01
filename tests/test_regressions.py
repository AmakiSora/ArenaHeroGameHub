from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
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


class ConfiguredPlannerTests(unittest.TestCase):
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
            with patch.object(tactic, "_bfs_direction", return_value=tactic.Direction.RIGHT) as bfs:
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
