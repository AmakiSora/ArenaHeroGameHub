"""Tick-log v4 fields: structured combat events, decisions, config revision."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import replay
import tactic
from arena_hero import CoreState, Direction, UnitType


def _event(event_type, *, actor=None, target=None, values=None, pos=None,
           tick=None):
    return SimpleNamespace(
        event_type=event_type,
        reason_code=None,
        actor_id=actor,
        target_id=target,
        position=pos,
        values=values,
        tick=tick,
        resource_amount=None,
    )


def _fake_turn(tick=100):
    core_view = SimpleNamespace(
        state=CoreState.MOVING,
        move_direction=Direction.RIGHT,
        move_progress=2,
        move_required_ticks=3,
        destination=(5, 0),
    )
    core = SimpleNamespace(
        id="core-1", position=(0, 0), hp=10, shield=5, view=core_view,
    )
    worker = SimpleNamespace(
        id="w1", position=(1, 1), hp=2, cargo=1, unit_type=UnitType.WORKER,
    )
    ranger = SimpleNamespace(
        id="r1", position=(2, 2), hp=2, unit_type=UnitType.RANGER,
    )
    enemy_core = SimpleNamespace(
        kind="CORE", id="e-core", position=(-5, 0), hp=5, shield=3,
        state=CoreState.MOVING, move_direction=Direction.DOWN,
        move_progress=1, move_required_ticks=4, destination=(-5, 4),
    )
    enemy_worker = SimpleNamespace(
        kind="WORKER", id="e-worker", position=(-6, 0), hp=2, cargo=2,
    )
    enemy_ranger = SimpleNamespace(
        kind="RANGER", id="e1", position=(-7, 0), hp=1,
    )
    events = (
        _event(
            "SHOT_HIT", actor="r1", target="e1",
            values={"damage": 1, "hp": 1}, pos=(-7, 0), tick=tick - 1,
        ),
        _event(
            "SWEEP_RESOLVED", actor="r1",
            values={"targets_hit": 0}, pos=(-6, 0), tick=tick - 1,
        ),
    )
    state = SimpleNamespace(population=5)
    beacon = SimpleNamespace(
        position=(5, 5), status=SimpleNamespace(name="GROUND"),
    )
    return SimpleNamespace(
        tick=tick,
        state=state,
        core=core,
        resources=10,
        resource_capacity=20,
        visible_enemies=(enemy_core, enemy_worker, enemy_ranger),
        resource_cells=frozenset(),
        obstacle_cells=frozenset(),
        beacon=beacon,
        units=(worker, ranger),
        workers=(worker,),
        vanguards=(),
        rangers=(ranger,),
        events=events,
    )


class TickLogV4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._names = dict(tactic._object_names)
        self._counters = dict(tactic._object_name_counters)
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        self._ctx = {
            key: getattr(tactic.turn_context, key)
            for key in (
                "tick", "config_rev", "config_changed", "config_delta", "attack_decision",
                "home_defense_decisions", "home_defense_leash",
                "attack_target_candidates", "kite_target_candidates",
                "kite_decisions", "guerrilla_decisions",
                "shot_predictions", "shot_prediction_results",
            )
        }
        tactic.turn_context.kite_decisions = []
        tactic.turn_context.guerrilla_decisions = []
        tactic.turn_context.shot_predictions = []
        tactic.turn_context.shot_prediction_results = []

    def tearDown(self) -> None:
        for key, value in self._ctx.items():
            setattr(tactic.turn_context, key, value)
        tactic._object_names.clear()
        tactic._object_names.update(self._names)
        tactic._object_name_counters.clear()
        tactic._object_name_counters.update(self._counters)

    def _write(self, tick=100) -> dict:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tactic_log.jsonl"
            logger = tactic.TacticLogger(str(path))
            logger.open()
            try:
                logger.record_tick(
                    _fake_turn(tick),
                    core_action="WAIT",
                    unit_actions={"r1": "SHOOT -> (-7, 0)"},
                    accepted=True,
                )
            finally:
                logger.close()
            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[0])["_version"], 4)
        return json.loads(lines[-2])

    def test_events_keep_target_values_and_resolved_tick(self) -> None:
        tactic.turn_context.tick = 100
        rec = self._write()

        shot = next(event for event in rec["events"] if event["type"] == "SHOT_HIT")
        self.assertEqual(shot["target"], "e1")
        self.assertEqual(shot["target_name"], "E3")
        self.assertEqual(shot["values"], {"damage": 1, "hp": 1})
        self.assertEqual(shot["resolved_tick"], 99)
        self.assertEqual(rec["events_tick"], 99)

        sweep = next(
            event for event in rec["events"] if event["type"] == "SWEEP_RESOLVED"
        )
        self.assertEqual(sweep["values"], {"targets_hit": 0})
        self.assertIsNone(sweep.get("target"))

    def test_core_movement_and_enemy_core_fields(self) -> None:
        tactic.turn_context.tick = 100
        rec = self._write()

        self.assertEqual(rec["core_move"], {
            "direction": "RIGHT", "progress": 2,
            "required_ticks": 3, "destination": [5, 0],
        })
        cores = [enemy for enemy in rec["enemies"] if enemy["type"] == "CORE"]
        self.assertEqual(len(cores), 1)
        self.assertEqual(cores[0]["shield"], 3)
        self.assertEqual(cores[0]["state"], "MOVING")
        self.assertEqual(cores[0]["move"]["direction"], "DOWN")
        workers = [enemy for enemy in rec["enemies"] if enemy["type"] == "WORKER"]
        self.assertEqual(workers[0]["cargo"], 2)

    def test_decisions_and_config_revision(self) -> None:
        tactic.turn_context.tick = 100
        tactic.turn_context.config_rev = "abc123"
        tactic.turn_context.config_changed = ["attack_retreat_radius"]
        tactic.turn_context.config_delta = {"attack_retreat_radius": 17}
        tactic.turn_context.attack_decision = {
            "tick": 100, "retreat": True, "enemy_count": 3,
            "enemy_hp": 8, "friend_hp": 6, "reach": 10,
        }
        tactic.turn_context.home_defense_decisions = {
            "v1": {"tick": 100, "unit": "V1", "leash": 12, "kept": 1,
                   "dropped": 2, "target": [-660, -107]},
        }
        tactic.turn_context.attack_target_candidates = [
            {"tick": 100, "cell": [-660, -107], "score": 5},
        ]
        tactic.turn_context.guerrilla_decisions = [
            {"tick": 100, "unit_name": "R1", "action": "MOVE:LEFT"},
        ]
        rec = self._write()

        self.assertEqual(rec["config_rev"], "abc123")
        self.assertEqual(rec["config_changed"], ["attack_retreat_radius"])
        self.assertEqual(rec["config_delta"], {"attack_retreat_radius": 17})
        self.assertTrue(rec["decisions"]["attack"]["retreat"])
        self.assertEqual(rec["decisions"]["home_defense"][0]["unit"], "V1")
        self.assertEqual(rec["decisions"]["home_defense_leash"], 12)
        self.assertEqual(rec["auto_target_candidates"][0]["cell"], [-660, -107])
        self.assertEqual(rec["guerrilla_decisions"][0]["unit_name"], "R1")

    def test_stale_decision_rows_from_an_earlier_tick_are_dropped(self) -> None:
        tactic.turn_context.tick = 100
        tactic.turn_context.attack_decision = {"tick": 99, "retreat": True}
        tactic.turn_context.attack_target_candidates = [
            {"tick": 99, "cell": [1, 1]},
        ]
        rec = self._write()
        self.assertNotIn("attack", rec["decisions"])
        self.assertEqual(rec["auto_target_candidates"], [])


class RetreatDetailTests(unittest.TestCase):
    def test_detail_out_reports_both_hp_pools(self) -> None:
        squad = (
            SimpleNamespace(hp=2), SimpleNamespace(hp=2),
            SimpleNamespace(hp=2), SimpleNamespace(hp=2),
        )
        enemies = (
            SimpleNamespace(
                position=(-1, 0), hp=2, unit_type=UnitType.RANGER,
            ),
            SimpleNamespace(
                position=(-2, 0), hp=4, unit_type=UnitType.VANGUARD,
            ),
            SimpleNamespace(
                position=(-3, 0), hp=2, unit_type=UnitType.WORKER,
            ),
        )
        detail: dict = {}
        retreat, count, _centroid, _forbidden = tactic._attack_retreat_decision(
            enemies, (0, 0), 4, 4, set(),
            squad_units=squad, threat_radius=10, detail_out=detail,
        )
        self.assertFalse(retreat)
        self.assertEqual(count, 2)
        self.assertEqual(detail["friend_hp"], 8)
        self.assertEqual(detail["enemy_hp"], 6)
        self.assertFalse(detail["outnumbered"])
        self.assertFalse(detail["out_hp"])
        self.assertEqual(detail["reach"], 10)


class AutoTargetRowsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._ticks = dict(tactic._enemy_memory_ticks)
        self._types = dict(tactic._enemy_memory_types)
        self._tick = tactic.turn_context.tick

    def tearDown(self) -> None:
        tactic._enemy_memory_ticks.clear()
        tactic._enemy_memory_ticks.update(self._ticks)
        tactic._enemy_memory_types.clear()
        tactic._enemy_memory_types.update(self._types)
        tactic.turn_context.tick = self._tick

    def test_top_three_sorted_with_score_components(self) -> None:
        tactic.turn_context.tick = 100
        tactic._enemy_memory_ticks.clear()
        tactic._enemy_memory_types.clear()
        for cell, seen in (((1, 0), 100), ((5, 0), 100), ((9, 0), 50)):
            tactic._enemy_memory_ticks[cell] = seen
            tactic._enemy_memory_types[cell] = "RANGER"

        rows = tactic._auto_target_rows(
            [(1, 0), (5, 0), (9, 0)], (0, 0), (0, 0), limit=3,
        )
        self.assertEqual([row["cell"] for row in rows], [[1, 0], [5, 0], [9, 0]])
        self.assertEqual(rows[0]["score"], 2)
        self.assertEqual(rows[2]["age"], 50)
        self.assertEqual(rows[2]["type"], "RANGER")
        self.assertTrue(all(row["tick"] == 100 for row in rows))


class BattleLogStructuredFieldsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._names = dict(tactic._object_names)
        self._counters = dict(tactic._object_name_counters)
        self._tick = tactic.turn_context.tick
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        tactic.turn_context.tick = 42

    def tearDown(self) -> None:
        tactic._object_names.clear()
        tactic._object_names.update(self._names)
        tactic._object_name_counters.clear()
        tactic._object_name_counters.update(self._counters)
        tactic.turn_context.tick = self._tick

    def test_combat_row_carries_structured_columns(self) -> None:
        turn = SimpleNamespace(
            units=(SimpleNamespace(id="u1", unit_type=UnitType.RANGER,
                                   position=(0, 0)),),
            visible_enemies=(SimpleNamespace(id="e1", unit_type=UnitType.RANGER,
                                             position=(2, 0)),),
            core=None,
            events=(_event(
                "SHOT_HIT", actor="u1", target="e1",
                values={"damage": 1, "hp": 0}, pos=(2, 0), tick=41,
            ),),
        )
        entries = tactic._battle_log_entries(
            turn, new_resources=set(), new_enemy_sightings=set(),
        )
        row = next(entry for entry in entries if entry["cat"] == "combat")
        self.assertEqual(row["event"], "SHOT_HIT")
        self.assertEqual(row["target"], "e1")
        self.assertEqual(row["values"], {"damage": 1, "hp": 0})
        self.assertEqual(row["resolved_tick"], 41)
        self.assertEqual(row["pos"], [2, 0])


class ReplayTests(unittest.TestCase):
    def _record(self, tick, *, with_v4=True):
        rec = {
            "tick": tick,
            "timestamp": "2026-11-22T00:00:00+00:00",
            "core_name": "C1",
            "core_pos": [0, 0],
            "core_hp": 5,
            "core_shield": 5,
            "core_state": "NORMAL",
            "resources": 10,
            "resource_capacity": 20,
            "population": 3,
            "accepted": True,
            "workers": [],
            "vanguards": [{
                "id": "v1", "name": "V1", "pos": [1, 1], "hp": 4,
                "target": None, "path": None, "path_len": 0,
            }],
            "rangers": [],
            "enemies": [{
                "id": "e1", "name": "E1", "pos": [3, 1], "hp": 2, "type": "RANGER",
            }],
            "plan_unit_actions": {"v1": "MOVE:RIGHT attack-engage (3, 1)"},
            "events": [{
                "type": "UNIT_DAMAGED", "reason": "ATTACK", "actor": None,
                "target": "v1", "target_name": "V1", "pos": [1, 1],
                "values": {"damage": 1, "hp": 3}, "resolved_tick": tick - 1,
            }],
            "shot_predictions": [],
            "shot_prediction_results": [],
            "kite_decisions": [],
            "guerrilla_decisions": [],
            "decisions": {
                "attack": {
                    "retreat": False, "enemy_count": 1, "enemy_hp": 2,
                    "friend_hp": 4, "reach": 10, "squad_size": 1,
                    "squad_pos": [1, 1],
                },
            },
            "config_rev": "abc123",
            "config_changed": [],
        }
        if not with_v4:
            for key in (
                "decisions", "config_rev", "config_changed",
                "guerrilla_decisions", "auto_target_candidates",
                "kite_target_candidates", "events_tick", "core_move",
            ):
                rec.pop(key, None)
            for event in rec["events"]:
                for key in ("target", "target_name", "values", "resolved_tick"):
                    event.pop(key, None)
        return rec

    def test_load_records_dedupes_and_sorts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old = Path(temp_dir) / "tactic_log.jsonl.1"
            new = Path(temp_dir) / "tactic_log.jsonl"
            old.write_text(json.dumps(self._record(100)) + "\n", encoding="utf-8")
            new.write_text(
                json.dumps(self._record(100)) + "\n"
                + json.dumps(self._record(101)) + "\n",
                encoding="utf-8",
            )
            records = replay.load_records([old, new])
        self.assertEqual([rec["tick"] for rec in records], [100, 101])
        self.assertEqual(records[1]["tick"], 101)

    def test_format_record_renders_v4_fields_and_board(self) -> None:
        lines = replay.format_record(
            self._record(100), board=True, all_units=True, all_events=True,
        )
        text = "\n".join(lines)
        self.assertIn("tick 100", text)
        self.assertIn("V1", text)
        self.assertIn("decision attack", text)
        self.assertIn("damage=1", text)
        self.assertIn("resolved=99", text)
        self.assertTrue(any("C" in line for line in lines))

    def test_v3_record_still_renders(self) -> None:
        lines = replay.format_record(self._record(100, with_v4=False))
        text = "\n".join(lines)
        self.assertIn("tick 100", text)
        self.assertIn("UNIT_DAMAGED", text)


class EventValuesAndConfigRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._config_values = tactic.turn_context.config_values

    def tearDown(self) -> None:
        tactic.turn_context.config_values = self._config_values

    def test_oversized_event_values_fall_back_to_keys(self) -> None:
        event = _event(
            "SHOT_HIT", values={"damage": "x" * 2000, "hp": 1},
        )
        self.assertEqual(
            tactic._event_values(event),
            {"_truncated": True, "_keys": ["damage", "hp"]},
        )

    def test_unknown_small_fields_are_kept(self) -> None:
        event = _event("CUSTOM", values={"new_field": 7})
        self.assertEqual(tactic._event_values(event), {"new_field": 7})

    def test_config_revision_reports_changed_keys(self) -> None:
        tactic.turn_context.config_values = {"a": 1, "b": 2}
        revision, changed = tactic._config_revision({"a": 2, "b": 2, "c": 3})
        self.assertEqual(len(revision), 12)
        self.assertEqual(changed, ["a", "c"])
        _same_rev, same_changed = tactic._config_revision({"a": 2, "b": 2, "c": 3})
        self.assertEqual(same_changed, [])


if __name__ == "__main__":
    unittest.main()
