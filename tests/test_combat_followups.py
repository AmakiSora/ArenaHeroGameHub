"""Follow-ups from the t647050-t647296 mine fight.

Covers: range-aware 1-HP Ranger kite, worker threat memory + mine mark,
detached attack-member local retreat/logging, and empty-cell kite fallback.
"""
from __future__ import annotations

import unittest

import tactic
from arena_hero import UnitType
from tactic_config import default_config


class FakeUnit:
    def __init__(self, uid, pos, hp, unit_type=UnitType.RANGER):
        self.id = uid
        self.position = pos
        self.hp = hp
        self.unit_type = unit_type
        self.moved = []

    def move(self, direction):
        self.moved.append(direction)

    def wait(self):
        self.moved.append("wait")

    def shoot(self, target, **kwargs):
        self.moved.append(("shoot", target))

    def shoot_cell(self, cell):
        self.moved.append(("shoot_cell", cell))

    def sweep(self, direction):
        self.moved.append(("sweep", direction))


class FakeEnemy:
    def __init__(self, uid, pos, hp=2, unit_type=UnitType.RANGER):
        self.id = uid
        self.position = pos
        self.hp = hp
        self.unit_type = unit_type


class CombatFollowupBase(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = {
            key: getattr(tactic.turn_context, key)
            for key in (
                "tick", "attack_squad_pos", "attack_squad_size",
                "attack_retreat", "attack_retreat_from",
                "attack_forbidden_targets", "attack_local_decisions",
                "attack_queue_target", "beacon_pos", "core_pos",
            )
        }
        self._threat_memory = dict(tactic._worker_threat_memory)
        self._mine_ban = dict(tactic._mine_danger_ban)
        tactic.turn_context.attack_retreat = False
        tactic.turn_context.attack_retreat_from = None
        tactic.turn_context.attack_forbidden_targets = frozenset()
        tactic.turn_context.attack_local_decisions = {}
        tactic.turn_context.attack_queue_target = None
        tactic.turn_context.beacon_pos = None

    def tearDown(self) -> None:
        for key, value in self._ctx.items():
            setattr(tactic.turn_context, key, value)
        tactic._worker_threat_memory.clear()
        tactic._worker_threat_memory.update(self._threat_memory)
        tactic._mine_danger_ban.clear()
        tactic._mine_danger_ban.update(self._mine_ban)

    @staticmethod
    def _config() -> dict:
        config = default_config()
        config.update({
            "attack_mode": "auto",
            "attack_target_x": -665,
            "attack_target_y": -108,
            "attack_retreat_radius": 4,
            "attack_march_engage_radius": 10,
            "ranger_attack_range": 3,
        })
        return config


class RangerKiteRangeTests(CombatFollowupBase):
    def test_one_hp_ranger_kites_from_a_ranger_at_range_three(self) -> None:
        ranger = FakeUnit("r1", (-547, -86), hp=1)
        enemy = FakeEnemy("e8", (-547, -89), hp=2)
        action = tactic._ranger_low_hp_kite(
            ranger, (-547, -86), (enemy,), frozenset(), {},
            ranger_range=3,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action[0], "MOVE")
        self.assertIn("low-hp-kite", action[1])
        self.assertTrue(ranger.moved)

    def test_kites_from_a_ranger_in_range_without_a_current_legal_line(self) -> None:
        # t647294: E7 at (-545,-96) is Chebyshev 3 from R2 (-547,-99) but not
        # on a legal line yet; it stepped into line and killed R2 next Tick.
        ranger = FakeUnit("r2", (-547, -99), hp=1)
        enemy = FakeEnemy("e7", (-545, -96), hp=2)
        action = tactic._ranger_low_hp_kite(
            ranger, (-547, -99), (enemy,), frozenset(), {},
            ranger_range=3,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action[0], "MOVE")
        self.assertIn("low-hp-kite", action[1])

    def test_enemy_worker_at_range_three_does_not_trigger(self) -> None:
        ranger = FakeUnit("r1", (-547, -86), hp=1)
        worker = FakeEnemy("e1", (-547, -89), hp=2, unit_type=UnitType.WORKER)
        self.assertIsNone(
            tactic._ranger_low_hp_kite(
                ranger, (-547, -86), (worker,), frozenset(), {},
                ranger_range=3,
            )
        )

    def test_kite_prefers_an_empty_cell_over_a_full_one(self) -> None:
        ranger = FakeUnit("r2", (-547, -99), hp=1)
        enemy = FakeEnemy("e7", (-547, -96), hp=2)
        # UP is the natural escape; mark it full so the kite must take another.
        action = tactic._ranger_low_hp_kite(
            ranger, (-547, -99), (enemy,), frozenset(), {(-547, -100): 2},
            ranger_range=3,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action[0], "MOVE")
        self.assertNotEqual(ranger.moved[0].name, "DOWN")  # not back into the shot
        self.assertTrue(ranger.moved)


class WorkerThreatMemoryTests(CombatFollowupBase):
    def test_memory_keeps_evasion_for_three_ticks(self) -> None:
        enemy = FakeEnemy("e7", (-549, -95), hp=2)
        tactic.turn_context.tick = 100
        first = tactic._worker_evasion_target(
            "w1", (-548, -95), (enemy,), frozenset(), 3, 3,
        )
        self.assertEqual(first, (-549, -95))

        tactic.turn_context.tick = 102
        remembered = tactic._worker_evasion_target(
            "w1", (-548, -95), (), frozenset(), 3, 3,
        )
        self.assertEqual(remembered, (-549, -95))

        tactic.turn_context.tick = 104
        self.assertIsNone(
            tactic._worker_evasion_target(
                "w1", (-548, -95), (), frozenset(), 3, 3,
            )
        )

    def test_worker_damage_marks_the_cell(self) -> None:
        worker = FakeUnit("w1", (-548, -95), hp=2, unit_type=UnitType.WORKER)
        event = type("Event", (), {
            "event_type": "UNIT_DAMAGED",
            "target_id": "w1",
            "position": (-548, -95),
        })()
        turn = type("Turn", (), {
            "workers": (worker,),
            "events": (event,),
        })()
        tactic.turn_context.tick = 100
        tactic._mine_danger_ban.clear()
        tactic._mark_worker_damage_sites(turn)
        self.assertGreater(
            tactic._mine_danger_ban.get((-548, -95), 0), 100,
        )


class AttackLocalOutlierTests(CombatFollowupBase):
    def test_detached_vanguard_retreats_locally(self) -> None:
        config = self._config()
        tactic.turn_context.attack_squad_pos = (-548, -30)
        tactic.turn_context.attack_squad_size = 4
        tactic.turn_context.tick = 647294
        vanguard = FakeUnit("v1", (-547, -95), hp=1, unit_type=UnitType.VANGUARD)
        enemies = (
            FakeEnemy("a", (-547, -93), hp=2),
            FakeEnemy("b", (-545, -95), hp=2),
        )
        action, detail = tactic._plan_attack_combat(
            vanguard, unit_kind="vanguard", enemies=enemies,
            obstacle_cells=frozenset(), config=config, cell_counts={},
        )
        self.assertEqual(action, "MOVE")
        self.assertIn("attack-local-retreat", detail)
        self.assertTrue(vanguard.moved)
        row = tactic.turn_context.attack_local_decisions["v1"]
        self.assertEqual(row["local_enemies"], 2)
        self.assertEqual(row["local_hp"], 1)

    def test_r1_snapshot_reports_local_threats_even_when_kiting(self) -> None:
        config = self._config()
        tactic.turn_context.attack_squad_pos = (-548, -30)
        tactic.turn_context.attack_squad_size = 4
        tactic.turn_context.tick = 647229
        ranger = FakeUnit("r1", (-547, -86), hp=1)
        enemies = (
            FakeEnemy("e8", (-547, -89), hp=2),
            FakeEnemy("e12", (-547, -91), hp=2),
        )
        action, detail = tactic._plan_attack_combat(
            ranger, unit_kind="ranger", enemies=enemies,
            obstacle_cells=frozenset(), config=config, cell_counts={},
        )
        self.assertEqual(action, "MOVE")
        self.assertIn("low-hp-kite", detail)
        row = tactic.turn_context.attack_local_decisions["r1"]
        self.assertEqual(row["local_enemies"], 2)
        self.assertEqual(row["local_hp"], 1)
        self.assertGreater(row["detached"], 10)


if __name__ == "__main__":
    unittest.main()
