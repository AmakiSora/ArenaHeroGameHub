"""Regression tests for the 2026-11-23 survival fixes in tactic.py.

Covers the fire-cover helpers (`_cell_fire_cover`, fire-aware `_worker_flee`,
`_home_safe_approach`), the Core danger freeze, and the post-respawn
suppression of a stale manual core target.
"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arena_hero import CoreState, Direction, UnitType

import tactic
from tactic_config import default_config


def _enemy(position, unit_type=UnitType.VANGUARD, uid="enemy-1"):
    """Visible enemy stub: `unit_type` is what `_enemy_unit_type_name` reads."""
    return SimpleNamespace(id=uid, position=position, unit_type=unit_type)


class CellFireCoverTests(unittest.TestCase):
    """`_cell_fire_cover` counts only enemies that could actually attack."""

    def test_ranger_on_same_row_at_distance_two_covers_cell(self) -> None:
        enemies = (_enemy((2, 0), UnitType.RANGER),)
        self.assertEqual(
            tactic._cell_fire_cover((0, 0), enemies, frozenset()), 1,
        )

    def test_ranger_at_knight_offset_does_not_cover_cell(self) -> None:
        # (1, 2) is neither a row, column, nor (anti-)diagonal shot.
        enemies = (_enemy((1, 2), UnitType.RANGER),)
        self.assertEqual(
            tactic._cell_fire_cover((0, 0), enemies, frozenset()), 0,
        )

    def test_wall_between_ranger_and_cell_blocks_cover(self) -> None:
        enemies = (_enemy((2, 0), UnitType.RANGER),)
        self.assertEqual(
            tactic._cell_fire_cover((0, 0), enemies, frozenset({(1, 0)})), 0,
        )

    def test_adjacent_vanguard_melees_cell(self) -> None:
        enemies = (_enemy((0, 1), UnitType.VANGUARD),)
        self.assertEqual(
            tactic._cell_fire_cover((0, 0), enemies, frozenset()), 1,
        )

    def test_enemy_worker_does_not_cover_cell(self) -> None:
        enemies = (_enemy((1, 0), UnitType.WORKER),)
        self.assertEqual(
            tactic._cell_fire_cover((0, 0), enemies, frozenset()), 0,
        )


class WorkerFleeFireCoverTests(unittest.TestCase):
    """Flee ranking must prefer a cover-0 end cell over plain distance."""

    GLOBALS = (
        "_worker_last_pos", "_worker_recent",
        "_dead_end_cache_key", "_dead_end_cache",
    )

    def setUp(self) -> None:
        self._snap = {name: copy.copy(getattr(tactic, name))
                      for name in self.GLOBALS}
        self._worker_routes = tactic.turn_context.worker_routes
        self._unit_routes = tactic.turn_context.unit_routes
        tactic._worker_last_pos.clear()
        tactic._worker_recent.clear()
        tactic.turn_context.worker_routes = {}
        tactic.turn_context.unit_routes = {}

    def tearDown(self) -> None:
        for name, val in self._snap.items():
            setattr(tactic, name, copy.copy(val))
        tactic.turn_context.worker_routes = self._worker_routes
        tactic.turn_context.unit_routes = self._unit_routes

    def test_flee_avoids_fire_line_instead_of_only_adding_distance(self) -> None:
        moved: list = []
        worker = SimpleNamespace(
            id="worker-1", position=(0, 0),
            move=moved.append, wait=lambda: None,
        )
        enemies = (
            _enemy((3, 0), UnitType.RANGER, "ranger-nearest"),
            _enemy((3, 1), UnitType.RANGER, "ranger-flank"),
        )
        # Distance-only ranking tie-breaks to DOWN (delta sort: (0,1) > (0,-1)
        # > (-1,0)); the flank Ranger covers that cell. UP is equally far and
        # off every fire line.
        self.assertEqual(
            tactic._cell_fire_cover((0, 1), enemies, frozenset()), 1,
        )
        self.assertEqual(
            tactic._cell_fire_cover((0, -1), enemies, frozenset()), 0,
        )

        action, detail = tactic._worker_flee(
            worker, "worker-1", (0, 0), (3, 0), (0, 0),
            frozenset(), frozenset(), False,
            enemies=enemies,
        )

        self.assertEqual(action, "MOVE")
        self.assertEqual(moved, [Direction.UP])
        self.assertIn("flee-enemy@(3, 0)", detail)


class HomeSafeApproachTests(unittest.TestCase):
    """`_home_safe_approach` overrides only an exposed direct approach step."""

    def setUp(self) -> None:
        self._unit_routes = tactic.turn_context.unit_routes
        self._dead_cache_key = tactic._dead_end_cache_key
        self._dead_cache = tactic._dead_end_cache
        tactic.turn_context.unit_routes = {}

    def tearDown(self) -> None:
        tactic.turn_context.unit_routes = self._unit_routes
        tactic._dead_end_cache_key = self._dead_cache_key
        tactic._dead_end_cache = self._dead_cache

    def test_returns_none_without_enemies(self) -> None:
        moved: list = []
        unit = SimpleNamespace(
            id="v-defender-1", position=(0, 0), move=moved.append,
        )
        result = tactic._home_safe_approach(
            unit, (0, 0), (0, 10), (), frozenset(), 3, None,
            "home-intercept",
        )
        self.assertIsNone(result)
        self.assertEqual(moved, [])

    def test_moves_to_cover_zero_cell_when_direct_step_is_covered(self) -> None:
        moved: list = []
        unit = SimpleNamespace(
            id="v-defender-1", position=(0, 0), move=moved.append,
        )
        enemies = (_enemy((0, 3), UnitType.RANGER, "ranger-1"),)
        # Goal (0, 10): the greedy direct step DOWN (0, 1) sits on the
        # Ranger's column; UP (0, -1) is off the fire line.
        self.assertEqual(
            tactic._cell_fire_cover((0, 1), enemies, frozenset()), 1,
        )
        result = tactic._home_safe_approach(
            unit, (0, 0), (0, 10), enemies, frozenset(), 3, None,
            "home-intercept",
        )
        self.assertEqual(moved, [Direction.UP])
        self.assertEqual(result, ("MOVE", "UP home-intercept-safe cover=0/1"))


class CoreSurvivalTests(unittest.TestCase):
    """Core danger freeze + post-respawn manual-target suppression.

    Same one-Tick `choose_actions` harness as `CoreTerrainBlockedLearningTests`,
    extended with visible enemies and the new cross-Tick core globals.
    """

    GLOBALS = (
        "_dead_obstacles", "_dead_open_count", "_dead_set", "_dead_view",
        "_dead_structure_built", "_known_obstacles", "_obstacle_memory",
        "_dead_end_cache_key", "_dead_end_cache", "_path_blockers_union_key",
        "_path_blockers_union", "_core_anchor", "_core_pending_move",
        "_core_move_hold_until_tick", "_core_danger_until",
        "_core_respawn_suppress", "_core_target_path_cache", "_map_dirty",
        "_object_names", "_object_name_counters", "_resource_assignments",
        "_resource_memory", "_resource_tombstones",
        "_worker_last_pos", "_worker_recent",
        "_obstacle_pending_adds", "_obstacle_sorted_cache",
        "_mine_danger_ban", "_death_heat",
    )

    def setUp(self) -> None:
        self._snap = {name: copy.copy(getattr(tactic, name))
                      for name in self.GLOBALS}
        self._worker_routes = tactic.turn_context.worker_routes
        self._unit_routes = tactic.turn_context.unit_routes
        self._tick = tactic.turn_context.tick
        tactic._core_danger_until = 0
        tactic._core_respawn_suppress.clear()
        tactic._core_target_path_cache.clear()
        tactic._core_pending_move = None
        tactic._core_move_hold_until_tick = 0
        tactic._core_anchor = None
        tactic._resource_memory.clear()
        tactic._resource_tombstones.clear()
        tactic._resource_assignments.clear()
        tactic._worker_last_pos.clear()
        tactic._worker_recent.clear()
        tactic._mine_danger_ban.clear()
        tactic._death_heat.clear()

    def tearDown(self) -> None:
        for name, val in self._snap.items():
            setattr(tactic, name, copy.copy(val))
        tactic.turn_context.worker_routes = self._worker_routes
        tactic.turn_context.unit_routes = self._unit_routes
        tactic.turn_context.tick = self._tick

    def _run(
        self,
        *,
        visible_enemies=(),
        events=(),
        target=(500, 0),
        worker_pos=(500, 0),
        resource_cells=frozenset(),
        tick=1,
    ):
        """One Tick with a manual core target; returns (action, start_moves)."""
        moves: list = []
        core = SimpleNamespace(
            id="core", position=(0, 0), hp=5, shield=10,
            view=SimpleNamespace(state=CoreState.NORMAL),
            spawn=lambda unit_type: None,
            heal=lambda: None,
            repair_shield=lambda: None,
            pickup_beacon=lambda: None,
            start_move=lambda direction: moves.append(direction),
            wait=lambda: None,
        )
        worker = SimpleNamespace(
            id="worker-1", unit_type=UnitType.WORKER, position=worker_pos,
            cargo=0, direction=None,
        )
        worker.move = lambda direction: None
        worker.wait = lambda: None
        beacon = SimpleNamespace(
            position=None, status=SimpleNamespace(name="GROUND"),
        )
        turn = SimpleNamespace(
            tick=tick,
            units=(worker,),
            workers=(worker,),
            vanguards=(),
            rangers=(),
            visible_enemies=tuple(visible_enemies),
            core=core,
            resources=50,
            resource_cells=resource_cells,
            resource_space=0,
            beacon=beacon,
            state=SimpleNamespace(population=8),
            events=tuple(events),
            obstacle_cells=frozenset(),
        )
        config = default_config()
        config["target_workers"] = 1
        config["target_vanguards"] = 0
        config["target_rangers"] = 0
        config["core_target_enabled"] = True
        config["core_target_x"], config["core_target_y"] = target
        # Earlier tests may leave learned walls / dead-end caches behind; give
        # this Tick an empty obstacle map so the heading is deterministic.
        tactic._obstacle_memory = set()
        tactic._known_obstacles = frozenset()
        tactic._reset_dead_structure(frozenset())
        tactic._dead_end_cache_key = None
        tactic._dead_end_cache = frozenset()
        tactic._path_blockers_union_key = None
        tactic._path_blockers_union = frozenset()
        tactic._obstacle_pending_adds.clear()
        tactic._obstacle_sorted_cache = None
        tactic._object_names.clear()
        tactic._object_name_counters.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            with (
                patch.object(tactic, "load_config", return_value=config),
                patch.object(
                    tactic, "MAP_MEMORY_PATH", temp / "map_memory.json",
                ),
                patch.object(
                    tactic, "WAYPOINTS_PATH", temp / "waypoints.json",
                ),
                patch.object(
                    tactic, "SELF_DESTRUCT_PATH", temp / "self_destruct.json",
                ),
                patch.object(
                    tactic, "BATTLE_LOG_PATH", temp / "battle_log.jsonl",
                ),
                patch.object(
                    tactic, "CONFIG_PATH", temp / "tactic_config.json",
                ),
            ):
                tactic._map_dirty = False
                core_action, _ = tactic.choose_actions(turn)
        return core_action, moves

    def test_visible_enemy_within_danger_radius_blocks_core_start_move(self) -> None:
        enemy = _enemy((10, 0), UnitType.RANGER, "ranger-camper")
        action, moves = self._run(visible_enemies=(enemy,))
        self.assertEqual(moves, [])
        self.assertEqual(action, "WAIT")
        self.assertEqual(
            tactic._core_danger_until, 1 + tactic._CORE_DANGER_LINGER_TICKS,
        )

    def test_no_enemy_baseline_starts_core_move(self) -> None:
        action, moves = self._run()
        self.assertEqual(moves, [Direction.RIGHT])
        self.assertEqual(action, "MOVE_RIGHT")

    def test_respawn_event_suppresses_stale_manual_target(self) -> None:
        event = SimpleNamespace(event_type="CORE_RESPAWNED")
        action, moves = self._run(
            events=(event,), worker_pos=(0, 500), tick=5,
        )
        self.assertEqual(
            tactic._core_respawn_suppress.get("target"), (500, 0),
        )
        # The manual target is invalidated, so the Core follows the auto
        # heuristic (worker mass at (0, 500)) instead of marching east.
        self.assertEqual(moves, [Direction.DOWN])
        self.assertEqual(action, "MOVE_DOWN")

    def test_manual_target_without_respawn_still_moves_toward_it(self) -> None:
        action, moves = self._run(worker_pos=(0, 500), tick=5)
        self.assertEqual(moves, [Direction.RIGHT])
        self.assertEqual(action, "MOVE_RIGHT")

    def test_mine_chebyshev_four_from_recent_death_is_banned(self) -> None:
        # Old rule banned only mines within Manhattan 1 of the death; (4, 0)
        # is Chebyshev 4 (Manhattan 4) and must now be banned too.
        tactic._death_heat[(0, 0)] = 1
        self._run(
            resource_cells=frozenset({(4, 0)}), worker_pos=(5, 0), tick=1,
        )
        self.assertIn((4, 0), tactic._mine_danger_ban)
        self.assertNotIn((4, 0), tactic._resource_assignments.values())


if __name__ == "__main__":
    unittest.main()
