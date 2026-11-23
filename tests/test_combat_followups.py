"""Follow-ups from the t647050-t647296 mine fight.

Covers: range-aware 1-HP Ranger kite, worker threat memory + mine mark,
detached attack-member local retreat/logging, and empty-cell kite fallback.
"""
from __future__ import annotations

import copy
import unittest

import tactic
from arena_hero import Direction, UnitType
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
        self._retreat = dict(tactic._worker_retreat)
        self._retreat_paths = copy.deepcopy(tactic._worker_retreat_path_cache)
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
        tactic._worker_retreat.clear()
        tactic._worker_retreat.update(self._retreat)
        tactic._worker_retreat_path_cache.clear()
        tactic._worker_retreat_path_cache.update(self._retreat_paths)

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

    def test_injured_worker_keeps_evasion_memory_for_sixteen_ticks(self) -> None:
        enemy = FakeEnemy("e7", (-549, -95), hp=2)
        tactic.turn_context.tick = 100
        first = tactic._worker_evasion_target(
            "w1", (-548, -95), (enemy,), frozenset(), 3, 3, hp=1,
        )
        self.assertEqual(first, (-549, -95))

        tactic.turn_context.tick = 116
        self.assertEqual(
            tactic._worker_evasion_target(
                "w1", (-548, -95), (), frozenset(), 3, 3, hp=1,
            ),
            (-549, -95),
        )
        tactic.turn_context.tick = 117
        self.assertIsNone(
            tactic._worker_evasion_target(
                "w1", (-548, -95), (), frozenset(), 3, 3, hp=1,
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
        self.assertEqual(tactic._worker_retreat["w1"], 100)
        self.assertEqual(
            tactic._recalled_worker_threat("w1"), (-548, -95),
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


class RetreatWorker:
    def __init__(self, uid, pos, hp=1, cargo=0):
        self.id = uid
        self.position = tuple(pos)
        self.hp = hp
        self.cargo = cargo
        self.unit_type = UnitType.WORKER
        self.moved = []
        self.waited = False
        self.harvests = 0
        self.deposits = 0

    def move(self, direction):
        self.moved.append(direction)
        dx, dy = direction.delta
        self.position = (self.position[0] + dx, self.position[1] + dy)

    def wait(self):
        self.waited = True

    def harvest(self):
        self.harvests += 1

    def deposit(self):
        self.deposits += 1
        self.cargo = 0


class InjuredWorkerRetreatTests(CombatFollowupBase):
    DICTS = (
        "_worker_last_pos", "_worker_recent", "_worker_path_cache",
        "_resource_assignments", "_resource_memory", "_waypoint_stuck",
        "_worker_stuck_pos", "_worker_stuck_ticks",
    )

    def setUp(self) -> None:
        super().setUp()
        self._saved_dicts = {
            name: copy.deepcopy(getattr(tactic, name)) for name in self.DICTS
        }
        self._saved_context = {
            "tick": tactic.turn_context.tick,
            "resource_space": tactic.turn_context.resource_space,
            "worker_routes": tactic.turn_context.worker_routes,
            "unit_routes": tactic.turn_context.unit_routes,
        }
        self._saved_scalars = {
            name: getattr(tactic, name)
            for name in (
                "_chute_in_demand", "_delivery_next_pos",
                "_chute_vacating_this_tick", "_dead_end_cache_key",
                "_dead_end_cache", "_path_blockers_union_key",
                "_path_blockers_union",
            )
        }
        for name in self.DICTS:
            getattr(tactic, name).clear()
        tactic._worker_retreat.clear()
        tactic._worker_retreat_path_cache.clear()
        tactic._worker_threat_memory.clear()
        tactic.turn_context.tick = 1
        tactic.turn_context.resource_space = 10
        tactic.turn_context.worker_routes = {}
        tactic.turn_context.unit_routes = {}
        tactic._chute_in_demand = False
        tactic._delivery_next_pos = None
        tactic._chute_vacating_this_tick = False
        tactic._dead_end_cache_key = None
        tactic._dead_end_cache = frozenset()
        tactic._path_blockers_union_key = None
        tactic._path_blockers_union = frozenset()

    def tearDown(self) -> None:
        for name, value in self._saved_dicts.items():
            table = getattr(tactic, name)
            table.clear()
            table.update(value)
        for name, value in self._saved_context.items():
            setattr(tactic.turn_context, name, value)
        for name, value in self._saved_scalars.items():
            setattr(tactic, name, value)
        super().tearDown()

    @staticmethod
    def _config():
        config = default_config()
        config.update({
            "worker_bfs_enabled": True,
            "worker_explore_when_full": False,
            "enemy_threat_radius": 3,
            "ranger_attack_range": 3,
        })
        return config

    def _plan(self, worker, core, *, enemies=(), resources=frozenset(),
              obstacles=frozenset()):
        return tactic._plan_worker(
            worker, core,
            resource_cells=frozenset(resources),
            obstacle_cells=frozenset(obstacles),
            depleted=set(), config=self._config(), enemies=tuple(enemies),
        )

    def test_injured_worker_retreats_instead_of_mining_without_visible_enemy(self) -> None:
        worker = RetreatWorker("w1", (3, 0), hp=1)
        core = type("Core", (), {"position": (0, 0)})()

        action, detail = self._plan(worker, core, resources={(3, 0)})

        self.assertEqual(action, "MOVE")
        self.assertIn("retreat-core safe", detail)
        self.assertEqual(worker.moved, [Direction.LEFT])
        self.assertEqual(worker.harvests, 0)
        self.assertIn("w1", tactic._worker_retreat)

    def test_retreat_path_steps_off_a_visible_ranger_fire_line(self) -> None:
        worker = RetreatWorker("w1", (0, 0), hp=1)
        core = type("Core", (), {"position": (0, 10)})()
        enemy = FakeEnemy("e1", (0, 3), unit_type=UnitType.RANGER)

        action, detail = self._plan(worker, core, enemies=(enemy,))

        self.assertEqual(action, "MOVE")
        self.assertIn("retreat-core safe", detail)
        self.assertNotEqual(worker.position, (0, 1))
        self.assertNotIn(worker.position, tactic._enemy_danger_cells(
            (enemy,), frozenset(), 3,
        ))

    def test_w2_like_corridor_uses_fallback_route_instead_of_oscillating(self) -> None:
        worker = RetreatWorker("w2", (-294, 1225), hp=1)
        core = type("Core", (), {"position": (-244, 1229)})()
        enemy = FakeEnemy("e3", (-296, 1226), unit_type=UnitType.RANGER)
        obstacles = {
            (-294, 1223), (-294, 1227), (-293, 1224), (-293, 1226),
            (-292, 1225),
        }

        details = []
        positions = [worker.position]
        for _ in range(4):
            action, detail = self._plan(
                worker, core, enemies=(enemy,), obstacles=obstacles,
            )
            self.assertEqual(action, "MOVE")
            details.append(detail)
            positions.append(worker.position)

        self.assertIn("retreat-core fallback", details[0])
        self.assertEqual(len(set(positions)), len(positions))
        self.assertNotIn(
            worker.position, {(-294, 1224), (-294, 1225), (-294, 1226)},
        )

    def test_healed_worker_clears_retreat_and_resumes_mining(self) -> None:
        worker = RetreatWorker("w1", (1, 0), hp=2)
        core = type("Core", (), {"position": (0, 0)})()
        tactic._worker_retreat["w1"] = 1
        tactic._worker_retreat_path_cache["w1"] = {"goal": (0, 0)}
        tactic._worker_threat_memory["w1"] = (1, (2, 0), 16)

        action, detail = self._plan(worker, core, resources={(1, 0)})

        self.assertEqual(action, "HARVEST")
        self.assertNotIn("retreat", detail)
        self.assertEqual(worker.harvests, 1)
        self.assertNotIn("w1", tactic._worker_retreat)
        self.assertNotIn("w1", tactic._worker_retreat_path_cache)
        self.assertNotIn("w1", tactic._worker_threat_memory)

    def test_dead_worker_retreat_bookkeeping_is_pruned(self) -> None:
        tactic._worker_retreat["dead"] = 1
        tactic._worker_retreat_path_cache["dead"] = {"goal": (0, 0)}

        tactic._prune_dead_unit_bookkeeping(set())

        self.assertNotIn("dead", tactic._worker_retreat)
        self.assertNotIn("dead", tactic._worker_retreat_path_cache)


if __name__ == "__main__":
    unittest.main()
