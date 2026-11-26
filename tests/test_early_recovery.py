"""Early-respawn economy fixes (t663811 stall).

Live evidence (_review/tactic_log.jsonl, ticks 663387-663811): the respawned
Core sat in a mine-poor site (only 4 remembered mines within 32 cells, nearest
26), the fixed 32-cell recovery ring stalled the rebuild (300-Tick timeout at 3
workers, 9 harvests in 424 Ticks), and the worker-only spawn order left the base
with 0 combat units while an enemy Core (E124) sat 16 cells away.

Two fixes covered here:
1. _spawn_order early guard — one cheap Vanguard before filling workers when the
   base has a couple of workers but no combat unit.
2. _effective_mine_radius adaptive ring — widen the recovery radius when the base
   ring holds too few mines.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from arena_hero import UnitType

import tactic
from tactic_config import default_config


def _turn(workers=0, vanguards=0, rangers=0, population=8):
    units = []
    for i in range(workers):
        units.append(SimpleNamespace(id=f"w{i}", unit_type=UnitType.WORKER, position=(i + 1, 0)))
    for i in range(vanguards):
        units.append(SimpleNamespace(id=f"v{i}", unit_type=UnitType.VANGUARD, position=(-i - 1, 0)))
    for i in range(rangers):
        units.append(SimpleNamespace(id=f"r{i}", unit_type=UnitType.RANGER, position=(-i - 1, 1)))
    return SimpleNamespace(
        units=tuple(units),
        workers=tuple(u for u in units if u.unit_type == UnitType.WORKER),
        vanguards=tuple(u for u in units if u.unit_type == UnitType.VANGUARD),
        rangers=tuple(u for u in units if u.unit_type == UnitType.RANGER),
        state=SimpleNamespace(population=population),
        resources=0,
    )


class EarlyGuardOrder(unittest.TestCase):
    def setUp(self):
        self.config = default_config()

    def test_two_workers_no_combat_spawns_guard_first(self):
        order = tactic._spawn_order(_turn(workers=2), self.config)
        self.assertEqual(order[0], UnitType.VANGUARD)

    def test_below_worker_minimum_stays_worker_first(self):
        order = tactic._spawn_order(_turn(workers=1), self.config)
        self.assertEqual(order[0], UnitType.WORKER)

    def test_existing_combat_resumes_worker_first(self):
        order = tactic._spawn_order(_turn(workers=2, vanguards=1), self.config)
        self.assertEqual(order[0], UnitType.WORKER)

    def test_disabled_stays_worker_first(self):
        self.config["early_guard_enabled"] = False
        order = tactic._spawn_order(_turn(workers=2), self.config)
        self.assertEqual(order[0], UnitType.WORKER)

    def test_workers_full_alternates_combat(self):
        self.config["target_workers"] = 2
        order = tactic._spawn_order(_turn(workers=2, vanguards=0, rangers=0), self.config)
        self.assertEqual(order[0], UnitType.WORKER)
        self.assertEqual(order[1], UnitType.VANGUARD)


class EarlyGuardSpawn(unittest.TestCase):
    class Core:
        def __init__(self):
            self.position = (0, 0)
            self.spawned = []

        def spawn(self, unit_type):
            self.spawned.append(unit_type)

    def setUp(self):
        self.config = default_config()

    def test_affordable_guard_spawns_vanguard(self):
        core = self.Core()
        turn = _turn(workers=2)
        result = tactic._plan_demand_spawn(turn, core, resources=10, config=self.config)
        self.assertEqual(result, "SPAWN_VANGUARD")
        self.assertEqual(core.spawned, [UnitType.VANGUARD])

    def test_unaffordable_guard_falls_back_to_worker(self):
        core = self.Core()
        turn = _turn(workers=2)
        result = tactic._plan_demand_spawn(turn, core, resources=5, config=self.config)
        self.assertEqual(result, "SPAWN_WORKER")


class AdaptiveRecoveryRadius(unittest.TestCase):
    def setUp(self):
        self.config = default_config()
        self._saved_recovery = tactic._core_recovery
        self._saved_memory = tactic._resource_memory
        self._saved_core_pos = tactic.turn_context.core_pos
        tactic._core_recovery = True
        tactic.turn_context.core_pos = (0, 0)
        tactic._resource_memory = set()

    def tearDown(self):
        tactic._core_recovery = self._saved_recovery
        tactic._resource_memory = self._saved_memory
        tactic.turn_context.core_pos = self._saved_core_pos

    def test_mine_poor_ring_widens(self):
        # Only 4 mines within 32 -> widen to 80.
        tactic._resource_memory = {(5, 0), (10, 0), (20, 0), (30, 0), (70, 0)}
        self.assertEqual(tactic._effective_mine_radius(self.config), 80)

    def test_mine_rich_ring_stays_base(self):
        tactic._resource_memory = {(i, 0) for i in range(1, 10)}
        self.assertEqual(tactic._effective_mine_radius(self.config), 32)

    def test_configured_cap_bounds_widening(self):
        self.config["worker_mine_max_distance"] = 50
        tactic._resource_memory = {(5, 0)}
        self.assertEqual(tactic._effective_mine_radius(self.config), 50)

    def test_disabled_stays_base(self):
        self.config["recovery_adaptive_radius"] = False
        tactic._resource_memory = {(5, 0)}
        self.assertEqual(tactic._effective_mine_radius(self.config), 32)

    def test_outside_recovery_uses_configured(self):
        tactic._core_recovery = False
        self.config["worker_mine_max_distance"] = 200
        self.assertEqual(tactic._effective_mine_radius(self.config), 200)


if __name__ == "__main__":
    unittest.main()