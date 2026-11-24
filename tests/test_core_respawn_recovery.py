"""Regressions from the 2026-11-23 C1 death / C2 respawn freeze.

Live evidence (``_review/tactic_log.jsonl``, ticks 649645-650054):

* C1 died at (-374,-10) to an 18-unit siege; C2 respawned at (-197,1501) with
  1 worker and 5 resources.
* ``tactic_config.json`` still carried ``core_target=(-249,-8)`` (the dead
  base). The Core immediately began marching at 1 cell / 4 Ticks — 100 MOVE
  starts and ~300 MOVING Ticks in 409 Ticks. A MOVING Core rejects every
  worker DEPOSIT, so the two starting workers could never unload reliably.
* Worker mine assignment used ``worker_mine_max_distance=200`` against a
  26k-cell resource memory. Both workers walked 80-100 cells to remembered
  nodes while the Core kept sliding away from them.
* Result: 825 worker moves, 4 harvests, 3 deposits, population stuck at 2.

The fix has three parts, all covered here:

1. A ``CORE_RESPAWNED`` event freezes Core movement and re-anchors the drift
   leash at the respawn cell until ``_CORE_RECOVERY_HEADCOUNT`` workers and a
   worker's spawn cost exist.
2. The configured manual target is treated as the dead base's leftover after a
   respawn unless it lies inside ``core_max_drift`` of the new anchor; a later
   dashboard edit of the target keys restores it.
3. During recovery, mine assignment and the worker rally fallback use a short
   radius (``_CORE_RECOVERY_MINE_RADIUS``) so miners probe the new area
   instead of chasing far memory.
"""
from __future__ import annotations

import contextlib
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arena_hero import CoreState, Direction, UnitType

import tactic
from tactic_config import default_config


class FakeEvent:
    def __init__(self, event_type, **kwargs):
        self.event_type = event_type
        self.reason_code = kwargs.get("reason_code")
        self.actor_id = kwargs.get("actor_id")
        self.target_id = kwargs.get("target_id")
        self.position = kwargs.get("position")
        self.values = kwargs.get("values")
        self.tick = kwargs.get("tick", 1)


class FakeWorker:
    """Minimal Worker double whose methods apply one Tick's action at once."""

    def __init__(self, world, uid: str, pos: tuple[int, int]) -> None:
        self.world = world
        self.id = uid
        self.position = tuple(pos)
        self.cargo = 0
        self.hp = 2
        self.unit_type = UnitType.WORKER
        self.last = None

    def move(self, direction) -> None:
        dx, dy = direction.delta
        self.position = (self.position[0] + dx, self.position[1] + dy)
        self.last = ("move", direction.name)

    def wait(self) -> None:
        self.last = ("wait",)

    def harvest(self) -> None:
        self.cargo += 1
        self.world.harvests += 1
        self.last = ("harvest",)

    def deposit(self) -> None:
        self.world.resources += self.cargo
        self.cargo = 0
        self.world.deposits += 1
        self.last = ("deposit",)

    def heal(self) -> None:
        self.last = ("heal",)


class FakeCore:
    def __init__(self, world, uid: str, pos: tuple[int, int]) -> None:
        self.world = world
        self.id = uid
        self.position = tuple(pos)
        self.hp = 5
        self.shield = 5
        self.view = SimpleNamespace(state=CoreState.NORMAL)
        self.moves: list[str] = []
        self.spawns = 0

    def start_move(self, direction) -> None:
        self.moves.append(direction.name)

    def wait(self) -> None:
        pass

    def spawn(self, unit_type) -> None:
        if self.world.resources < tactic.UNIT_SPAWN_COSTS["WORKER"]:
            raise AssertionError("planner spawned without banked resources")
        self.world.resources -= tactic.UNIT_SPAWN_COSTS["WORKER"]
        self.spawns += 1
        self.world.add_worker(tuple(self.position))

    def repair_shield(self) -> None:
        pass

    def heal(self) -> None:
        pass

    def pickup_beacon(self) -> None:
        pass


class RespawnWorld:
    """Tiny economy model: workers mine known cells, the Core spawns on gold.

    One Tick calls ``choose_actions`` exactly like the live loop; the fake
    units apply the chosen action immediately. Resources are infinite; this is
    a cadence model, not a game server.
    """

    def __init__(self, *, core_pos, mines, resources=5, workers=1):
        self.tick = 0

        self.core_pos = tuple(core_pos)
        self.mines = frozenset(tuple(m) for m in mines)
        self.resources = resources
        self.harvests = 0
        self.deposits = 0
        self.peak_resources = resources
        self.world_workers: list[FakeWorker] = []
        self.core = FakeCore(self, "c2", self.core_pos)
        for index in range(workers):
            self.add_worker(self.core_pos, uid=f"w{index + 1:07d}")

    def add_worker(self, pos, uid: str | None = None) -> None:
        if uid is None:
            uid = f"w{len(self.world_workers) + 1:07d}"
        self.world_workers.append(FakeWorker(self, uid, pos))

    def visible_resources(self) -> frozenset[tuple[int, int]]:
        return frozenset(
            mine
            for mine in self.mines
            if any(
                tactic._manhattan(mine, worker.position) <= 2
                for worker in self.world_workers
            )
        )

    def turn(self, events=()) -> SimpleNamespace:
        population = len(self.world_workers)
        capacity = 10 + 2 * population
        return SimpleNamespace(
            tick=self.tick,
            units=tuple(self.world_workers),
            workers=tuple(self.world_workers),
            vanguards=(),
            rangers=(),
            visible_enemies=(),
            core=self.core,
            resources=self.resources,
            resource_cells=self.visible_resources(),
            resource_space=max(0, capacity - self.resources),
            beacon=SimpleNamespace(position=None, status="GROUND", carrier_id=None),
            state=SimpleNamespace(population=population),
            events=tuple(events),
            obstacle_cells=frozenset(),
        )

    def step(self, events=()) -> str:
        self.tick += 1
        action, _ = tactic.choose_actions(self.turn(events))
        self.peak_resources = max(self.peak_resources, self.resources)
        return action


class RespawnRecoveryBase(unittest.TestCase):
    """Run whole Ticks without leaking module state into the other suites.

    ``choose_actions`` writes dozens of module globals (``_chute_vacating_
    this_tick``, the delivery lease, ``_worker_threat_memory`` ...) besides the
    recovery flags this test targets. Restoring a hand-picked list left two
    leaks that only showed up in a full-suite run: a leftover nonzero
    ``turn_context.tick`` armed ``_recalled_worker_threat`` (WorkerEnemyEvasion
    saw a stale flee) and a leftover ``_chute_vacating_this_tick`` disabled the
    chute queue (WorkerCongestion fell through to plain BFS). Snapshot every
    plain data global instead, and restore container *contents* so the shared
    objects keep their identity for other modules.
    """

    SNAPSHOT_TYPES = (bool, int, float, str, tuple, frozenset, type(None),
                      bytes)

    def setUp(self) -> None:
        self._saved_scalars = {}
        self._saved_containers = {}
        for name, value in vars(tactic).items():
            if name.startswith("__") or name == "turn_context":
                continue
            if isinstance(value, (dict, set, list)):
                # (the original object, a copy of its contents) so both a
                # rebind and in-place mutation inside a Tick are undone.
                self._saved_containers[name] = (value, copy.copy(value))
            elif isinstance(value, self.SNAPSHOT_TYPES):
                self._saved_scalars[name] = value
        # Snapshot the whole per-Tick context. Choosing 200 Ticks leaves
        # `tick`, `resource_space`, `mine_danger_cells`, ... as instance
        # attributes; a leftover nonzero `tick` would arm `_recalled_worker_
        # threat` (see the class docstring), so restore the exact dict.
        self._saved_context = dict(vars(tactic.turn_context))
        tactic._core_recovery = False
        tactic._core_recovery_tick = 0
        tactic._core_target_stale_after_respawn = False
        tactic._core_anchor = None
        tactic._core_pending_move = None
        tactic._core_move_hold_until_tick = 0

    def tearDown(self) -> None:
        for name, value in self._saved_scalars.items():
            setattr(tactic, name, value)
        for name, (original, contents) in self._saved_containers.items():
            setattr(tactic, name, original)
            if isinstance(original, dict):
                original.clear()
                original.update(contents)
            elif isinstance(original, set):
                original.clear()
                original.update(contents)
            else:
                original[:] = contents
        tactic.turn_context.__dict__.clear()
        tactic.turn_context.__dict__.update(self._saved_context)

    def config(self, **overrides):
        config = default_config()
        config.update({
            "core_movement_enabled": True,
            "core_target_enabled": True,
            "core_target_x": -249,
            "core_target_y": -8,
            "core_max_drift": 200,
            "worker_mine_max_distance": 200,
            "target_workers": 26,
            "target_vanguards": 0,
            "target_rangers": 0,
            "worker_bfs_enabled": True,
            "enemy_threat_radius": 3,
            "worker_explore_when_full": False,
            "map_save_interval_ticks": 200,
        })
        config.update(overrides)
        return config

    def patch_paths(self, temp: Path):
        return (
            patch.object(tactic, "load_config", return_value=self._config),
            patch.object(tactic, "MAP_MEMORY_PATH", temp / "map_memory.json"),
            patch.object(tactic, "WAYPOINTS_PATH", temp / "waypoints.json"),
            patch.object(tactic, "SELF_DESTRUCT_PATH", temp / "self_destruct.json"),
            patch.object(tactic, "BATTLE_LOG_PATH", temp / "battle_log.jsonl"),
            patch.object(tactic, "CONFIG_PATH", temp / "tactic_config.json"),
        )

    @contextlib.contextmanager
    def isolated_tick_env(self, temp_dir: Path):
        with contextlib.ExitStack() as stack:
            for patcher in self.patch_paths(temp_dir):
                stack.enter_context(patcher)
            yield

    def run_ticks(self, world: RespawnWorld, *, ticks: int, respawn: bool = False,
                  config=None):
        self._config = config or self.config()
        with tempfile.TemporaryDirectory() as temp_dir, \
                self.isolated_tick_env(Path(temp_dir)):
            actions = []
            for _ in range(ticks):
                events = ()
                if respawn and world.tick == 0:
                    events = (
                        FakeEvent(
                            "CORE_RESPAWNED",
                            target_id=world.core.id,
                            position=world.core_pos,
                            values={"resources": world.resources, "workers": 1},
                            tick=0,
                        ),
                    )
                actions.append(world.step(events))
        return actions


class CoreRespawnHoldTests(RespawnRecoveryBase):
    def test_old_behaviour_would_march_back_to_the_dead_base(self) -> None:
        # Control for the live freeze: without the respawn guard the manual
        # target 1500+ cells away is honoured and the Core starts walking.
        world = RespawnWorld(
            core_pos=(-197, 1501), mines={(-193, 1499), (-205, 1496)},
        )
        config = self.config(target_workers=1)
        actions = self.run_ticks(world, ticks=1, respawn=False, config=config)
        self.assertEqual(world.core.moves, ["UP"])
        self.assertEqual(actions, ["MOVE_UP"])

    def test_respawn_freezes_core_against_the_stale_target(self) -> None:
        world = RespawnWorld(
            core_pos=(-197, 1501), mines={(-193, 1499), (-205, 1496)},
        )
        config = self.config(target_workers=1)
        actions = self.run_ticks(world, ticks=1, respawn=True, config=config)
        self.assertEqual(world.core.moves, [])
        self.assertEqual(actions, ["WAIT"])
        self.assertTrue(tactic._core_recovery)
        self.assertEqual(tactic._core_anchor, (-197, 1501))
        self.assertTrue(tactic._core_target_stale_after_respawn)

    def test_recovery_ends_once_headcount_and_vault_are_back(self) -> None:
        world = RespawnWorld(
            core_pos=(-197, 1501), mines={(-193, 1499), (-205, 1496)},
        )
        config = self.config()
        # Tick 1: the respawn event with one worker enters recovery.
        self.run_ticks(world, ticks=1, respawn=True, config=config)
        self.assertTrue(tactic._core_recovery)
        for _ in range(3):
            world.add_worker((-198, 1500))
        world.resources = tactic.UNIT_SPAWN_COSTS["WORKER"]
        world.step(())
        self.assertFalse(tactic._core_recovery)
        # The stale target is still dropped: the Core stays at its new site.
        self.assertEqual(world.core.moves, [])

    def test_recovery_gives_up_after_the_window_without_resources(self) -> None:
        # A respawn site with no reachable mine cannot rebuild the fleet;
        # rather than freezing the Core forever, the window expires and the
        # normal policy resumes. The stale manual target stays suppressed, so
        # the Core drifts with its worker instead of marching home.
        world = RespawnWorld(
            core_pos=(-197, 1501), mines=set(), resources=1,
        )
        self.run_ticks(world, ticks=302, respawn=True, config=self.config())
        self.assertFalse(tactic._core_recovery)
        self.assertLessEqual(
            tactic._manhattan(tuple(world.core.position), (-197, 1501)), 10,
            "the expired window must not turn back into a march on the old base",
        )

    def test_fresh_target_after_respawn_is_honoured(self) -> None:
        world = RespawnWorld(
            core_pos=(-197, 1501), mines={(-193, 1499), (-205, 1496)},
        )
        first = self.config()
        self.run_ticks(world, ticks=1, respawn=True, config=first)
        self.assertTrue(tactic._core_recovery)

        # Operator re-aims the target near the new site; four workers and a
        # spawn's worth of gold also end recovery.
        for _ in range(3):
            world.add_worker((-198, 1500))
        world.resources = tactic.UNIT_SPAWN_COSTS["WORKER"]
        second = self.config(
            core_target_x=-180, core_target_y=1500, target_workers=4,
        )
        self._config = second
        with tempfile.TemporaryDirectory() as temp_dir, \
                self.isolated_tick_env(Path(temp_dir)):
            action, _ = tactic.choose_actions(world.turn(()))
        self.assertFalse(tactic._core_recovery)
        self.assertFalse(tactic._core_target_stale_after_respawn)
        self.assertEqual(world.core.moves, [Direction.RIGHT.name])
        self.assertEqual(action, "MOVE_RIGHT")


class RespawnMineRadiusTests(RespawnRecoveryBase):
    def test_recovery_assigns_only_mines_near_the_new_core(self) -> None:
        world = RespawnWorld(
            core_pos=(-197, 1501), mines={(-193, 1499), (-205, 1496)},
        )
        world.add_worker((-199, 1501))
        near = (-193, 1499)
        far = (-77, 1499)  # 120 cells east: a stale memory from the old base
        tactic._resource_memory.update({near, far})
        self.run_ticks(world, ticks=1, respawn=True, config=self.config())
        assigned = set(tactic._resource_assignments.values())
        self.assertIn(near, assigned)
        self.assertNotIn(far, assigned)
        for mine in assigned:
            self.assertLessEqual(tactic._manhattan(mine, (-197, 1501)), 32)

    def test_normal_play_still_uses_the_full_mine_radius(self) -> None:
        world = RespawnWorld(
            core_pos=(-197, 1501), mines={(-193, 1499), (-205, 1496)},
        )
        world.add_worker((-199, 1501))
        near = (-193, 1499)
        far = (-77, 1499)
        tactic._resource_memory.update({near, far})
        self.run_ticks(world, ticks=1, respawn=False, config=self.config())
        assigned = set(tactic._resource_assignments.values())
        # Two idle workers claim the two nearest known mines; nothing about
        # normal play is narrowed by the respawn-only radius.
        self.assertEqual(assigned, {near, far})


class RespawnEconomyRecoveryTests(RespawnRecoveryBase):
    def test_economy_recovers_within_200_ticks_of_respawn(self) -> None:
        """Acceptance: after the Core swap, 200 Ticks must buy workers back.

        Live run before the fix: 0 spawns, 4 harvests, 3 deposits, population
        2 after 409 Ticks. With the fix the Core holds still until the fleet is
        back, so deposits land every trip; afterwards it may drift with its
        workers but never back to the dead base.
        """
        world = RespawnWorld(
            core_pos=(-197, 1501),
            mines={(-193, 1499), (-205, 1496), (-190, 1505)},
            resources=5,
            workers=1,
        )
        tactic._resource_memory.update(world.mines)
        self.run_ticks(
            world, ticks=200, respawn=True,
            config=self.config(target_workers=26),
        )

        self.assertLessEqual(
            tactic._manhattan(tuple(world.core.position), (-197, 1501)), 32,
            "once recovery exits the Core may follow its workers, but it must "
            "stay at the new site",
        )
        self.assertGreaterEqual(
            len(world.world_workers), 4,
            f"only {len(world.world_workers)} workers after 200 ticks",
        )
        self.assertGreaterEqual(world.deposits, 8)
        self.assertGreaterEqual(world.harvests, 8)
        self.assertGreaterEqual(world.peak_resources, tactic.UNIT_SPAWN_COSTS["WORKER"])
        self.assertFalse(tactic._core_recovery)


class MiningEconomyTests(RespawnRecoveryBase):
    """Dead-memory invalidation, sticky revalidation, deposit chute.

    Live window 654574-655274 (``_review`` logs): 65 of 171 mine runs walked
    to a cell someone had already harvested and 57 arrived to nothing; a
    remembered cell still held a resource only 27 times in 62 stands (43.5%).
    ``_review/_respawn_vision.py`` over 628662-655498 (467 harvests): every
    harvested cell came within vision again and exactly one was ever
    refilled, 339 Ticks later — the nodes are one-shot, so a dead memory entry
    costs the whole round trip.

    A failed deposit is the opposite: over all 232 live DEPOSIT_FAILED, the
    same worker's next DEPOSIT_SUCCEEDED came a median 2 Ticks later (p90 3,
    max 13), so the MOVING guard is kept for a clean log, not for income.
    """

    def test_harvest_succeeded_tombstones_the_cell(self) -> None:
        world = RespawnWorld(core_pos=(-197, 1501), mines=set(), workers=0)
        world.add_worker((-197, 1503))
        harvested = (-197, 1491)
        tactic._resource_memory.add(harvested)
        tactic._resource_tombstones.discard(harvested)

        tactic._update_resource_memory(world.turn((FakeEvent(
            "HARVEST_SUCCEEDED",
            actor_id=world.world_workers[0].id,
            position=harvested,
            values={"amount": 1},
        ),)))

        self.assertNotIn(harvested, tactic._resource_memory)
        self.assertIn(
            harvested, tactic._resource_tombstones,
            "the tombstone must survive the next map save",
        )

    def test_sticky_assignment_dies_when_the_mine_is_harvested_out(self) -> None:
        world = RespawnWorld(core_pos=(-197, 1501), mines=set(), workers=0)
        world.add_worker((-197, 1503))
        wid = world.world_workers[0].id
        mine = (-197, 1491)          # 10 cells north: a 22-step round trip
        tactic._resource_memory.add(mine)
        self.run_ticks(world, ticks=1, config=self.config())
        self.assertEqual(tactic._resource_assignments.get(wid), mine)

        # What HARVEST_SUCCEEDED does when another worker empties the cell.
        tactic._forget_resource(mine)
        self.run_ticks(world, ticks=1, config=self.config())
        self.assertNotEqual(
            tactic._resource_assignments.get(wid), mine,
            "a harvested-out memory cell must not keep its worker walking",
        )

    def test_sticky_assignment_is_revalidated_against_the_round_trip(self) -> None:
        # A mine inside the 200-cell radius when assigned can fall out of it as
        # the Core migrates. Re-check "outbound + return" every Tick: 150 out
        # from the Core plus 210 from the worker is 360 > 200, so the sticky
        # entry dies and the nearer mine wins.
        world = RespawnWorld(core_pos=(0, 0), mines=set(), workers=0)
        world.add_worker((0, -60))
        wid = world.world_workers[0].id
        far_mine = (0, 150)
        near_mine = (0, -70)
        tactic._resource_memory.update({far_mine, near_mine})
        tactic._resource_assignments[wid] = far_mine
        self.run_ticks(world, ticks=1, config=self.config())
        self.assertEqual(tactic._resource_assignments.get(wid), near_mine)

    def test_loaded_worker_waits_instead_of_depositing_into_a_moving_core(
            self) -> None:
        world = RespawnWorld(core_pos=(-197, 1501), mines=set(), workers=0)
        world.add_worker((-197, 1501))
        worker = world.world_workers[0]
        worker.cargo = 1
        world.core.view = SimpleNamespace(state=CoreState.MOVING)
        self._config = self.config(target_workers=1)
        with tempfile.TemporaryDirectory() as temp_dir, \
                self.isolated_tick_env(Path(temp_dir)):
            _core_action, details = tactic.choose_actions(world.turn(()))
        self.assertIn("deposit-defer-core-moving", details[worker.id])
        self.assertEqual(world.deposits, 0)
        self.assertEqual(worker.cargo, 1, "the cargo must stay aboard")
        self.assertEqual(worker.last, ("wait",))

    def test_loaded_worker_deposits_once_the_core_lands(self) -> None:
        world = RespawnWorld(core_pos=(-197, 1501), mines=set(), workers=0)
        world.add_worker((-197, 1501))
        worker = world.world_workers[0]
        worker.cargo = 1
        self._config = self.config(target_workers=1)
        with tempfile.TemporaryDirectory() as temp_dir, \
                self.isolated_tick_env(Path(temp_dir)):
            _core_action, details = tactic.choose_actions(world.turn(()))
        self.assertIn("at_core", details[worker.id])
        self.assertEqual(world.deposits, 1)
        self.assertEqual(worker.cargo, 0)


class CoreTargetArrivalLogTests(RespawnRecoveryBase):
    """A manual target must say when the Core actually gets there.

    The operator could previously only infer arrival from the Core stopping;
    one config row per arrival episode makes it explicit without spamming a
    row every Tick the Core parks on the target.
    """

    @staticmethod
    def _rows(log: Path) -> list[dict]:
        if not log.exists():
            return []
        return [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_arrival_is_logged_once_per_target(self) -> None:
        world = RespawnWorld(core_pos=(-197, 1501), mines=set(), workers=1)
        self._config = self.config(core_target_x=-197, core_target_y=1501)
        with tempfile.TemporaryDirectory() as temp_dir:
            log = Path(temp_dir) / "battle_log.jsonl"
            with self.isolated_tick_env(Path(temp_dir)):
                world.step()
                world.step()
            arrivals = [
                row for row in self._rows(log)
                if "到达指定目标" in row.get("msg", "")
            ]
        self.assertEqual(len(arrivals), 1)
        self.assertIn("(-197,1501)", arrivals[0]["msg"])
        self.assertEqual(arrivals[0]["cat"], "config")

    def test_arrival_logs_only_after_the_core_reaches_the_cell(self) -> None:
        world = RespawnWorld(core_pos=(-197, 1502), mines=set(), workers=1)
        self._config = self.config(core_target_x=-197, core_target_y=1501)
        with tempfile.TemporaryDirectory() as temp_dir:
            log = Path(temp_dir) / "battle_log.jsonl"
            with self.isolated_tick_env(Path(temp_dir)):
                world.step()
                before = [
                    row for row in self._rows(log)
                    if "到达指定目标" in row.get("msg", "")
                ]
                world.core.position = (-197, 1501)
                world.step()
            after = [
                row for row in self._rows(log)
                if "到达指定目标" in row.get("msg", "")
            ]
        self.assertEqual(before, [])
        self.assertEqual(len(after), 1)

    def test_arrival_is_logged_even_when_a_loaded_worker_holds_the_core(self) -> None:
        # close_cargo stops the Core from starting a move, but it must not hide
        # the arrival: the Core is already parked on the operator's target.
        world = RespawnWorld(
            core_pos=(-197, 1501), mines=set(), workers=1, resources=0,
        )
        world.world_workers[0].position = (-197, 1502)
        world.world_workers[0].cargo = 1
        self._config = self.config(core_target_x=-197, core_target_y=1501)
        with tempfile.TemporaryDirectory() as temp_dir:
            log = Path(temp_dir) / "battle_log.jsonl"
            with self.isolated_tick_env(Path(temp_dir)):
                world.step()
            arrivals = [
                row for row in self._rows(log)
                if "到达指定目标" in row.get("msg", "")
            ]
        self.assertEqual(len(arrivals), 1)

    def test_stale_respawn_target_is_not_reported_as_arrived(self) -> None:
        # The configured target points at the dead base; after a respawn the
        # Core is clamped to its new anchor. Sitting on the anchor is not the
        # operator's target, so the arrival row must not claim it.
        world = RespawnWorld(core_pos=(-197, 1501), mines=set(), workers=1)
        tactic._core_target_stale_after_respawn = True
        tactic._core_anchor = (-197, 1501)
        self._config = self.config(core_target_x=-249, core_target_y=-8)
        with tempfile.TemporaryDirectory() as temp_dir:
            log = Path(temp_dir) / "battle_log.jsonl"
            with self.isolated_tick_env(Path(temp_dir)):
                world.step()
            arrivals = [
                row for row in self._rows(log)
                if "到达指定目标" in row.get("msg", "")
            ]
        self.assertEqual(arrivals, [])


if __name__ == "__main__":
    unittest.main()
