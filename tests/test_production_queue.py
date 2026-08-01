from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arena_hero import UnitType

import production_queue
import tactic


class ProductionQueueTests(unittest.TestCase):
    def test_queue_preserves_order_and_enforces_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue.db"
            requested_types = ["WORKER", "VANGUARD", "RANGER"]
            for index in range(production_queue.MAX_QUEUE_SIZE):
                production_queue.enqueue(
                    requested_types[index % len(requested_types)],
                    path,
                )

            items = production_queue.list_requests(path)
            with self.assertRaises(production_queue.QueueFullError):
                production_queue.enqueue("WORKER", path)

        self.assertEqual(len(items), 20)
        self.assertEqual(
            [item["unit_type"] for item in items[:3]],
            requested_types,
        )

    def test_claim_only_removes_request_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue.db"
            request = production_queue.enqueue("VANGUARD", path)

            self.assertTrue(production_queue.claim_request(request["id"], 100, path))
            self.assertEqual(production_queue.head_request(path)["status"], "inflight")
            self.assertTrue(production_queue.finish_inflight(
                request["id"], succeeded=False, path=path
            ))
            self.assertEqual(production_queue.head_request(path)["status"], "pending")
            self.assertTrue(production_queue.claim_request(request["id"], 101, path))
            self.assertTrue(production_queue.finish_inflight(
                request["id"], succeeded=True, path=path
            ))

            self.assertIsNone(production_queue.head_request(path))


class QueuedSpawnPlannerTests(unittest.TestCase):
    class Core:
        position = (0, 0)

        def spawn(self, unit_type: UnitType) -> None:
            self.spawned = unit_type

    def test_affordable_head_is_spawned_and_success_removes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue.db"
            with patch.object(production_queue, "QUEUE_PATH", path):
                production_queue.enqueue("RANGER")
                turn = SimpleNamespace(tick=10, units=(), events=())
                core = self.Core()

                self.assertIsNone(tactic._plan_queued_spawn(turn, core, resources=11))
                self.assertEqual(production_queue.head_request()["status"], "pending")
                self.assertEqual(
                    tactic._plan_queued_spawn(turn, core, resources=12),
                    "SPAWN_RANGER",
                )
                self.assertEqual(core.spawned, UnitType.RANGER)
                self.assertEqual(production_queue.head_request()["status"], "inflight")

                success_turn = SimpleNamespace(
                    tick=11,
                    units=(),
                    events=(SimpleNamespace(event_type="CORE_SPAWN_SUCCEEDED"),),
                )
                tactic._sync_production_queue(success_turn)

                self.assertIsNone(production_queue.head_request())

    def test_occupied_core_cell_keeps_request_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue.db"
            with patch.object(production_queue, "QUEUE_PATH", path):
                production_queue.enqueue("WORKER")
                occupying_unit = SimpleNamespace(position=(0, 0))
                turn = SimpleNamespace(tick=20, units=(occupying_unit,), events=())

                action = tactic._plan_queued_spawn(turn, self.Core(), resources=20)

                self.assertIsNone(action)
                self.assertEqual(production_queue.head_request()["status"], "pending")


if __name__ == "__main__":
    unittest.main()
