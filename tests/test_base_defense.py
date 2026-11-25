"""Phase 1 base-defence regressions (t663387 wipe).

Live evidence (_review/tactic_log.jsonl, ticks 663329-663387): ~30 enemy Rangers
massed on the Core while only 9 home defenders stood against them and the main
army roamed 300-2600 cells away. The home squad fired 5 shots in 59 Ticks (each
Ranger picked its own nearest foe) and the visible-only sortie saw 0 targets
because the killers flickered out of vision.

Three fixes are covered here:
1. _update_recall — base-alert recall of the dispersed squads (linger + hysteresis).
2. _pick_home_focus_target / _ranger_best_shot(focus_id=...) — shared home fire.
3. _inferred_threat_enemies — off-vision death-heat sortie targets.
Plus the recall march branch in _plan_home_combat.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from arena_hero import UnitType

import tactic
from tactic_config import default_config


def _enemy(uid, pos, hp=2, utype=UnitType.RANGER):
    return SimpleNamespace(id=uid, position=tuple(pos), hp=hp, unit_type=utype)


def _combat_unit(uid, utype, pos, hp=None):
    return SimpleNamespace(
        id=uid, unit_type=utype, position=tuple(pos),
        hp=hp if hp is not None else (4 if utype == UnitType.VANGUARD else 2),
    )


class _FakeUnit:
    """Combat unit double: records the queued action without resolving it."""

    def __init__(self, uid, utype, pos, hp=None):
        self.id = uid
        self.unit_type = utype
        self.position = tuple(pos)
        self.hp = hp if hp is not None else (4 if utype == UnitType.VANGUARD else 2)
        self.moves = []
        self.shots = []
        self.waited = 0

    def move(self, direction):
        self.moves.append(direction.name)

    def wait(self):
        self.waited += 1

    def shoot(self, target, expected_cell=None):
        self.shots.append((str(target.id), expected_cell))

    def sweep(self, *args, **kwargs):
        pass

    def heal(self):
        pass


class RecallTrigger(unittest.TestCase):
    def setUp(self):
        self.config = default_config()
        tactic.turn_context.tick = 1000
        tactic.turn_context.recent_death_cells = frozenset()
        tactic.turn_context.recall_deaths = 0
        tactic._recall_ids.clear()
        tactic._recall_threat_until = 0
        tactic._recall_active_logged = False
        tactic._death_heat.clear()

    def _turn(self, units, events=()):
        return SimpleNamespace(
            units=tuple(units),
            core=SimpleNamespace(hp=5, shield=5),
            events=tuple(events),
        )

    def _enemies_near_core(self, n):
        return tuple(_enemy(f"e{i}", (3 + i, 0)) for i in range(n))

    def test_below_threshold_does_not_recall(self):
        turn = self._turn([_combat_unit("g0", UnitType.RANGER, (40, 0))])
        tactic._update_recall(turn, self._enemies_near_core(5), (0, 0), self.config)
        self.assertFalse(tactic.turn_context.recall_active)
        self.assertEqual(tactic._recall_ids, set())

    def test_threshold_recalls_non_home_unit_within_cap(self):
        far = _combat_unit("g0", UnitType.RANGER, (40, 0))
        turn = self._turn([far])
        tactic._update_recall(turn, self._enemies_near_core(6), (0, 0), self.config)
        self.assertTrue(tactic.turn_context.recall_active)
        self.assertIn("g0", tactic._recall_ids)

    def test_unit_beyond_cap_is_not_recalled(self):
        # t663387: nearest recalled unit was 296 cells out and the Core died
        # 34 Ticks later; the alert may still be true, but the far unit must
        # stay on its front rather than walk for 296 Ticks.
        far = _combat_unit("g0", UnitType.RANGER, (296, 0))
        turn = self._turn([far])
        tactic._update_recall(turn, self._enemies_near_core(6), (0, 0), self.config)
        self.assertTrue(tactic.turn_context.recall_active)
        self.assertNotIn("g0", tactic._recall_ids)

    def test_cap_zero_recalls_far_units(self):
        self.config["recall_max_distance"] = 0
        far = _combat_unit("g0", UnitType.RANGER, (296, 0))
        turn = self._turn([far])
        tactic._update_recall(turn, self._enemies_near_core(6), (0, 0), self.config)
        self.assertIn("g0", tactic._recall_ids)

    def test_home_and_near_units_are_not_recalled(self):
        self.config["home_team"] = "V1"
        home = _combat_unit("h0", UnitType.VANGUARD, (40, 0))
        near = _combat_unit("n0", UnitType.RANGER, (3, 0))
        far = _combat_unit("g0", UnitType.RANGER, (40, 0))
        tactic._object_names[("V", "h0")] = "V1"
        turn = self._turn([home, near, far])
        tactic._update_recall(turn, self._enemies_near_core(6), (0, 0), self.config)
        self.assertIn("g0", tactic._recall_ids)
        self.assertNotIn("h0", tactic._recall_ids)
        self.assertNotIn("n0", tactic._recall_ids)

    def test_linger_then_release(self):
        turn = self._turn([_combat_unit("g0", UnitType.RANGER, (40, 0))])
        tactic._update_recall(turn, self._enemies_near_core(6), (0, 0), self.config)
        self.assertTrue(tactic.turn_context.recall_active)
        # Enemies slip out of vision: the alert lingers release_ticks.
        tactic.turn_context.tick = 1000 + 20
        tactic._update_recall(turn, (), (0, 0), self.config)
        self.assertTrue(tactic.turn_context.recall_active)
        self.assertIn("g0", tactic._recall_ids)
        # Past the linger window the alert releases and the set clears.
        tactic.turn_context.tick = 1000 + 31
        tactic._update_recall(turn, (), (0, 0), self.config)
        self.assertFalse(tactic.turn_context.recall_active)
        self.assertEqual(tactic._recall_ids, set())

    def test_death_heat_triggers_before_mass_is_visible(self):
        # t663345: two ally deaths near the Core, no visible mass yet.
        tactic.turn_context.recent_death_cells = frozenset({(4, 0), (5, 0)})
        tactic._death_heat[(4, 0)] = 999
        tactic._death_heat[(5, 0)] = 999
        turn = self._turn([_combat_unit("g0", UnitType.RANGER, (40, 0))])
        tactic._update_recall(turn, (), (0, 0), self.config)
        self.assertTrue(tactic.turn_context.recall_active)
        self.assertEqual(tactic.turn_context.recall_deaths, 2)
        self.assertIn("g0", tactic._recall_ids)

    def test_old_death_heat_outside_window_does_not_trigger(self):
        tactic.turn_context.recent_death_cells = frozenset({(4, 0), (5, 0)})
        tactic._death_heat[(4, 0)] = 900
        tactic._death_heat[(5, 0)] = 900
        turn = self._turn([_combat_unit("g0", UnitType.RANGER, (40, 0))])
        tactic._update_recall(turn, (), (0, 0), self.config)
        self.assertFalse(tactic.turn_context.recall_active)
        self.assertEqual(tactic._recall_ids, set())

    def test_core_damaged_event_triggers_recall(self):
        turn = self._turn(
            [_combat_unit("g0", UnitType.RANGER, (40, 0))],
            events=[SimpleNamespace(event_type="CORE_DAMAGED")],
        )
        tactic._update_recall(turn, (), (0, 0), self.config)
        self.assertTrue(tactic.turn_context.recall_active)
        self.assertIn("g0", tactic._recall_ids)

    def test_disabled_never_recalls(self):
        self.config["recall_enabled"] = False
        turn = self._turn([_combat_unit("g0", UnitType.RANGER, (40, 0))])
        tactic._update_recall(turn, self._enemies_near_core(20), (0, 0), self.config)
        self.assertFalse(tactic.turn_context.recall_active)
        self.assertEqual(tactic._recall_ids, set())


class HomeFocusFire(unittest.TestCase):
    def setUp(self):
        self.config = default_config()

    def test_picks_lowest_hp_then_nearest_core(self):
        enemies = (
            _enemy("full", (2, 0), hp=2),
            _enemy("hurt", (5, 0), hp=1),
            _enemy("hurt_far", (8, 0), hp=1),
        )
        self.assertEqual(
            tactic._pick_home_focus_target(enemies, (0, 0), self.config), "hurt",
        )

    def test_outside_engage_range_is_none(self):
        enemies = (_enemy("far", (50, 0), hp=1),)
        self.assertIsNone(
            tactic._pick_home_focus_target(enemies, (0, 0), self.config),
        )

    def test_workers_are_not_focus_targets(self):
        enemies = (_enemy("w", (2, 0), hp=1, utype=UnitType.WORKER),)
        self.assertIsNone(
            tactic._pick_home_focus_target(enemies, (0, 0), self.config),
        )

    def test_disabled_is_none(self):
        self.config["home_focus_fire"] = False
        enemies = (_enemy("hurt", (2, 0), hp=1),)
        self.assertIsNone(
            tactic._pick_home_focus_target(enemies, (0, 0), self.config),
        )


class RangerFocusShot(unittest.TestCase):
    def setUp(self):
        self.config = default_config()
        tactic.turn_context.shot_predictions = []

    def test_focus_target_overrides_nearest_pick(self):
        ranger = _FakeUnit("r0", UnitType.RANGER, (0, 0))
        near = _enemy("near", (1, 0))
        focus = _enemy("focus", (2, 0))
        action, detail = tactic._ranger_best_shot(
            ranger, (0, 0), (near, focus), frozenset(), 3, focus_id="focus",
        )
        self.assertEqual(action, "SHOOT")
        self.assertEqual(ranger.shots[-1][0], "focus")

    def test_focus_out_of_range_falls_back_to_legal(self):
        ranger = _FakeUnit("r0", UnitType.RANGER, (0, 0))
        near = _enemy("near", (1, 0))
        focus = _enemy("focus", (10, 0))
        action, _ = tactic._ranger_best_shot(
            ranger, (0, 0), (near, focus), frozenset(), 3, focus_id="focus",
        )
        self.assertEqual(action, "SHOOT")
        self.assertEqual(ranger.shots[-1][0], "near")


class ThreatInference(unittest.TestCase):
    def setUp(self):
        self.config = default_config()
        tactic._death_heat.clear()

    def test_death_heat_near_core_yields_ranger_stub(self):
        tactic._death_heat[(4, 0)] = 1000
        out = tactic._inferred_threat_enemies((0, 0), self.config, ())
        self.assertEqual(len(out), 1)
        self.assertEqual(tuple(out[0].position), (4, 0))
        self.assertEqual(tactic._enemy_unit_type_name(out[0]), "RANGER")
        self.assertTrue(tactic._is_combat_threat(out[0]))

    def test_visible_cell_is_not_inferred(self):
        tactic._death_heat[(4, 0)] = 1000
        visible = (_enemy("e", (4, 0)),)
        self.assertEqual(
            tactic._inferred_threat_enemies((0, 0), self.config, visible), (),
        )

    def test_far_death_is_not_inferred(self):
        tactic._death_heat[(100, 0)] = 1000
        self.assertEqual(
            tactic._inferred_threat_enemies((0, 0), self.config, ()), (),
        )

    def test_disabled_is_empty(self):
        self.config["threat_inference_enabled"] = False
        tactic._death_heat[(4, 0)] = 1000
        self.assertEqual(
            tactic._inferred_threat_enemies((0, 0), self.config, ()), (),
        )


class RecallMarch(unittest.TestCase):
    def setUp(self):
        self.config = default_config()
        tactic._recall_ids.clear()
        tactic._combat_path_cache.clear()
        tactic._worker_last_pos.clear()

    def test_far_recalled_unit_marches_home(self):
        unit = _FakeUnit("g0", UnitType.RANGER, (40, 0))
        tactic._recall_ids.add("g0")
        action, detail = tactic._plan_home_combat(
            unit, unit_kind="ranger", enemies=(), obstacle_cells=frozenset(),
            core_pos=(0, 0), config=self.config,
        )
        self.assertEqual(action, "MOVE")
        self.assertIn("recall-march", detail)
        self.assertIn("g0", tactic._recall_ids)  # still marching, not arrived

    def test_arrived_recalled_unit_is_discarded(self):
        unit = _FakeUnit("g0", UnitType.RANGER, (2, 0))
        tactic._recall_ids.add("g0")
        tactic._plan_home_combat(
            unit, unit_kind="ranger", enemies=(), obstacle_cells=frozenset(),
            core_pos=(0, 0), config=self.config,
        )
        self.assertNotIn("g0", tactic._recall_ids)


if __name__ == "__main__":
    unittest.main()