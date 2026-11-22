"""Combat fixes for the t646385-t646436 battle (2026-11-22).

Run from the repo root:
    python -m pytest tests -q

Covers:
  * Vanguard sweeps before stepping (a legal sweep must not be spent on a MOVE)
  * _vanguard_approach_for_sweep  -- step beside a diagonal enemy, never onto one
  * _ranger_low_hp_kite           -- 1 HP ranger moves, and only from real threats
  * _attack_retreat_decision      -- squad-wide HP verdict; t646406 holds,
                                     t646411 / t646419-646421 still disengage
  * home_defense_leash            -- schema field + load_config round-trip
  * _plan_home_combat             -- defense_targets chase is leashed
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import tactic
from arena_hero import Direction, UnitType
from tactic_config import CONFIG_FIELDS, _FIELDS_BY_KEY, default_config, load_config


class FakeUnit:
    """Minimal Unit stand-in: records the queued move."""

    def __init__(self, uid, pos, hp=4, unit_type=UnitType.VANGUARD):
        self.id = uid
        self.position = pos
        self.hp = hp
        self.unit_type = unit_type
        self.cargo = 0
        self.moved: list[Direction] = []
        self.waited = 0
        self.swept: list[Direction] = []

    def move(self, direction):
        self.moved.append(direction)

    def sweep(self, direction):
        self.swept.append(direction)

    def wait(self):
        self.waited += 1

    @property
    def arg(self):
        return self.swept[-1] if self.swept else (self.moved[-1] if self.moved else None)


def FakeEnemy(pos, unit_type=UnitType.RANGER, hp=2, eid="e"):
    return SimpleNamespace(
        id=eid, position=pos, unit_type=unit_type, hp=hp,
    )


def landing(unit, origin):
    """Cell the unit would stand on after its queued move."""
    assert len(unit.moved) == 1, f"expected exactly one move, got {unit.moved}"
    d = unit.moved[0]
    return (origin[0] + d.delta[0], origin[1] + d.delta[1])


def squad_centroid(units):
    """Same centroid rounding as _choose_actions (attack_squad_pos)."""
    return (
        round(sum(u.position[0] for u in units) / len(units)),
        round(sum(u.position[1] for u in units) / len(units)),
    )


def _reset_combat_state() -> None:
    tactic._attack_regroup_ids.clear()
    tactic._sweep_fuse_until.clear()
    tactic._sweep_miss_streak.clear()
    tactic._enemy_motion_tracks.clear()
    tactic.turn_context.attack_retreat = False
    tactic.turn_context.attack_retreat_from = None
    tactic.turn_context.attack_forbidden_targets = frozenset()
    tactic.turn_context.attack_squad_pos = None
    tactic.turn_context.attack_squad_size = 0
    tactic.turn_context.core_pos = None
    tactic.turn_context.defense_targets = ()
    tactic.turn_context.tick = 0


@pytest.fixture(autouse=True)
def _isolate_combat_state():
    """Clear cross-Tick combat state this module's paths read, both ways."""
    _reset_combat_state()
    yield
    _reset_combat_state()


# --------------------------------------------------------------------------
# Vanguard: a legal sweep outranks the approach step at every call site
# --------------------------------------------------------------------------

# t646406: V12 (-659,-108), E247 cardinal above, E249 diagonal up-left. The
# old order spent the Tick on the approach MOVE and lost SWEEP:DOWN. Note the
# sweep still whiffed in the real log (the enemy moved first); this test only
# pins the action priority, not a hit.
T646406_POS = (-659, -108)
T646406_ENEMIES = (
    FakeEnemy((-659, -107), UnitType.RANGER, hp=2, eid="e247"),
    FakeEnemy((-660, -107), UnitType.RANGER, hp=2, eid="e249"),
)


def test_attack_combat_sweeps_before_stepping():
    config = default_config()
    config["attack_mode"] = "coords"
    config["attack_target_x"] = -675
    config["attack_target_y"] = -112
    unit = FakeUnit("v12", T646406_POS, hp=2)

    action, detail = tactic._plan_attack_combat(
        unit,
        unit_kind="vanguard",
        enemies=T646406_ENEMIES,
        obstacle_cells=frozenset(),
        config=config,
        core_pos=(-626, -73),
    )

    assert action == "SWEEP", detail
    assert detail.startswith("DOWN"), detail
    assert unit.moved == [], "spent the Tick on a MOVE while a sweep was legal"


def test_home_combat_sweeps_before_stepping():
    config = default_config()
    config["home_team"] = "V12"
    unit = FakeUnit("v12", T646406_POS, hp=2)

    action, detail = tactic._plan_home_combat(
        unit,
        unit_kind="vanguard",
        enemies=T646406_ENEMIES,
        obstacle_cells=frozenset(),
        core_pos=(-626, -73),
        config=config,
    )

    assert action == "SWEEP", detail
    assert detail.startswith("DOWN"), detail
    assert unit.moved == []


def test_waypoint_attack_mode_sweeps_before_stepping():
    config = default_config()
    unit = FakeUnit("v12", T646406_POS, hp=2)

    action, detail = tactic._plan_waypoint(
        unit,
        "V12",
        {"queue": [(-675, -112)], "mode": "attack"},
        config=config,
        obstacle_cells=frozenset(),
        occupied=frozenset(),
        enemies=T646406_ENEMIES,
        core_pos=(-626, -73),
    )

    assert action == "SWEEP", detail
    assert detail.startswith("DOWN"), detail
    assert unit.moved == []


# --------------------------------------------------------------------------
# Approach: step beside a diagonal enemy, never onto one
# --------------------------------------------------------------------------

def test_vanguard_steps_onto_a_cardinal_cell_of_a_diagonal_enemy():
    origin = (0, 0)
    unit = FakeUnit("v12", origin, hp=4)
    enemy = FakeEnemy((1, 1), UnitType.RANGER, hp=2)

    action = tactic._vanguard_approach_for_sweep(
        unit, origin, (enemy,), frozenset(),
    )

    assert action is not None
    assert action[0] == "MOVE"
    cell = landing(unit, origin)
    # The step must leave us cardinal-adjacent (Manhattan 1) to the enemy,
    # which is exactly what the sweep checks.
    assert abs(cell[0] - 1) + abs(cell[1] - 1) == 1
    assert "sweep-approach" in action[1]


def test_vanguard_does_not_step_when_already_cardinal_adjacent():
    origin = (0, 0)
    unit = FakeUnit("v12", origin, hp=4)
    enemy = FakeEnemy((1, 0), UnitType.RANGER, hp=2)

    assert tactic._vanguard_approach_for_sweep(unit, origin, (enemy,), frozenset()) is None
    assert unit.moved == []


def test_vanguard_approach_skips_a_full_cell():
    origin = (0, 0)
    unit = FakeUnit("v12", origin, hp=4)
    enemy = FakeEnemy((1, 1), UnitType.RANGER, hp=2)

    action = tactic._vanguard_approach_for_sweep(
        unit, origin, (enemy,), frozenset(), {(1, 0): 2},
    )

    assert action is not None
    cell = landing(unit, origin)
    assert cell != (1, 0), "stepped onto a cell already at the unit limit"
    assert abs(cell[0] - 1) + abs(cell[1] - 1) == 1


def test_vanguard_approach_never_steps_onto_an_enemy_cell():
    """Enemies are not in obstacle_cells; they must be blocked explicitly."""
    origin = (0, 0)
    unit = FakeUnit("v12", origin, hp=4)
    target = FakeEnemy((1, 1), UnitType.RANGER, hp=2, eid="target")
    # UP of the target is another enemy; the approach must take the free
    # cardinal neighbour (LEFT) instead of the enemy cell the scan hits first.
    blocker = FakeEnemy((1, 0), UnitType.RANGER, hp=2, eid="b1")
    enemies = (target, blocker)

    action = tactic._vanguard_approach_for_sweep(
        unit, origin, enemies, frozenset(),
    )

    assert action is not None
    cell = landing(unit, origin)
    enemy_cells = {tuple(e.position) for e in enemies}
    assert cell not in enemy_cells, f"stepped onto enemy cell {cell}"
    assert cell == (0, 1)


def test_vanguard_approach_returns_none_when_every_slot_is_an_enemy():
    origin = (0, 0)
    unit = FakeUnit("v12", origin, hp=4)
    target = FakeEnemy((1, 1), UnitType.RANGER, hp=2, eid="target")
    # Both one-step cardinal neighbours of the diagonal target hold enemies.
    enemies = (
        target,
        FakeEnemy((1, 0), UnitType.RANGER, hp=2, eid="b1"),
        FakeEnemy((0, 1), UnitType.RANGER, hp=2, eid="b2"),
    )

    action = tactic._vanguard_approach_for_sweep(
        unit, origin, enemies, frozenset(),
    )

    assert action is None
    assert unit.moved == []


def test_vanguard_approach_then_sweep_lands_a_hit():
    """End-to-end: approach, then the sweep function finds a target."""
    origin = (0, 0)
    unit = FakeUnit("v12", origin, hp=4)
    enemy = FakeEnemy((1, 1), UnitType.RANGER, hp=2)

    tactic._vanguard_approach_for_sweep(unit, origin, (enemy,), frozenset())
    cell = landing(unit, origin)
    sweep = tactic._vanguard_adjacent_sweep(unit, cell, (enemy,))
    assert sweep is not None
    assert sweep[0] == "SWEEP"


# --------------------------------------------------------------------------
# Ranger kite: 1 HP moves instead of trading shots, and only from threats
# --------------------------------------------------------------------------

def test_one_hp_ranger_moves_instead_of_shooting():
    origin = (0, 0)
    ranger = FakeUnit("r18", origin, hp=1, unit_type=UnitType.RANGER)
    enemy = FakeEnemy((1, 0), UnitType.RANGER, hp=2)

    action = tactic._ranger_low_hp_kite(
        ranger, origin, (enemy,), frozenset(),
    )

    assert action is not None
    assert action[0] == "MOVE"
    assert "low-hp-kite" in action[1]
    assert landing(ranger, origin) != origin


def test_two_hp_ranger_keeps_firing():
    origin = (0, 0)
    ranger = FakeUnit("r18", origin, hp=2, unit_type=UnitType.RANGER)
    enemy = FakeEnemy((1, 0), UnitType.RANGER, hp=2)

    assert tactic._ranger_low_hp_kite(ranger, origin, (enemy,), frozenset()) is None
    assert ranger.moved == []


def test_one_hp_ranger_ignores_distant_enemies():
    origin = (0, 0)
    ranger = FakeUnit("r18", origin, hp=1, unit_type=UnitType.RANGER)
    enemy = FakeEnemy((5, 0), UnitType.RANGER, hp=2)

    assert tactic._ranger_low_hp_kite(ranger, origin, (enemy,), frozenset()) is None


def test_one_hp_ranger_does_not_flee_a_worker():
    """t646421: R18 at 1 HP fled from E250 WORKER two cells away."""
    origin = (0, 0)
    ranger = FakeUnit("r18", origin, hp=1, unit_type=UnitType.RANGER)
    worker = FakeEnemy((2, 0), UnitType.WORKER, hp=2, eid="e250")

    action = tactic._ranger_low_hp_kite(ranger, origin, (worker,), frozenset())

    assert action is None, "a worker cannot hurt the ranger; it must keep firing"
    assert ranger.moved == []


def test_low_hp_kite_never_picks_an_enemy_cell():
    origin = (0, 0)
    ranger = FakeUnit("r18", origin, hp=1, unit_type=UnitType.RANGER)
    # Enemies surround three of four exits; the last one is a wall.
    enemies = (
        FakeEnemy((1, 0)), FakeEnemy((0, 1)), FakeEnemy((-1, 0)),
    )
    walls = frozenset({(0, -1)})

    action = tactic._ranger_low_hp_kite(ranger, origin, enemies, walls)

    assert action is None, "boxed ranger must not walk into an enemy cell"
    assert ranger.moved == []


# --------------------------------------------------------------------------
# Retreat verdict: real t646385-t646436 scenes
# --------------------------------------------------------------------------

def _t646406_squad():
    """t646406: V12 hp2 + four rangers hp2 each (10 HP total)."""
    return (
        FakeUnit("v12", (-659, -108), hp=2),
        FakeUnit("r5", (-651, -105), hp=2, unit_type=UnitType.RANGER),
        FakeUnit("r18", (-654, -106), hp=2, unit_type=UnitType.RANGER),
        FakeUnit("r6", (-640, -104), hp=2, unit_type=UnitType.RANGER),
        FakeUnit("r12", (-655, -106), hp=2, unit_type=UnitType.RANGER),
    )


def _t646406_enemies():
    """E251 sits exactly on the 10-cell boundary; E248 is out at 11."""
    return (
        FakeEnemy((-660, -107), UnitType.RANGER, hp=2, eid="e249"),
        FakeEnemy((-659, -107), UnitType.RANGER, hp=2, eid="e247"),
        FakeEnemy((-661, -107), UnitType.WORKER, hp=2, eid="e255"),
        FakeEnemy((-662, -108), UnitType.VANGUARD, hp=4, eid="e251"),
        FakeEnemy((-663, -108), UnitType.RANGER, hp=1, eid="e248"),
    )


def test_t646406_holds_the_fight():
    """10 squad HP (all five members) vs 8 enemy HP: no retreat."""
    squad = _t646406_squad()
    squad_pos = squad_centroid(squad)
    assert squad_pos == (-652, -106)

    retreat, count, centroid, forbidden = tactic._attack_retreat_decision(
        enemies=_t646406_enemies(),
        squad_pos=squad_pos,
        squad_size=len(squad),
        radius=4,
        enemy_memory={(-660, -107)},
        squad_units=squad,
        threat_radius=10,
    )

    assert retreat is False, "5 units at 10 HP must not disengage from 8 HP"
    assert count == 3  # e255 WORKER is not a threat; e248 is outside reach
    assert centroid is None
    assert forbidden == frozenset()


def test_t646411_retreats():
    """3 units (6 HP) vs 3 combat enemies inside the leash."""
    squad = (
        FakeUnit("r5", (-655, -106), hp=2, unit_type=UnitType.RANGER),
        FakeUnit("r18", (-657, -107), hp=2, unit_type=UnitType.RANGER),
        FakeUnit("r6", (-644, -105), hp=2, unit_type=UnitType.RANGER),
    )
    enemies = (
        FakeEnemy((-661, -107), UnitType.RANGER, hp=2, eid="e249"),
        FakeEnemy((-661, -107), UnitType.RANGER, hp=2, eid="e247"),
        FakeEnemy((-659, -106), UnitType.RANGER, hp=1, eid="e256"),
    )

    retreat, count, _centroid, forbidden = tactic._attack_retreat_decision(
        enemies=enemies,
        squad_pos=squad_centroid(squad),
        squad_size=len(squad),
        radius=4,
        enemy_memory={(-661, -107)},
        squad_units=squad,
        threat_radius=10,
    )

    assert retreat is True
    assert count == 3
    assert (-661, -107) in forbidden


@pytest.mark.parametrize(
    "tick, squad, enemies",
    [
        (
            "646419",
            (
                FakeUnit("r18", (-659, -107), hp=1, unit_type=UnitType.RANGER),
                FakeUnit("r6", (-651, -106), hp=2, unit_type=UnitType.RANGER),
            ),
            (
                FakeEnemy((-661, -109), UnitType.RANGER, hp=1, eid="e247"),
                FakeEnemy((-663, -107), UnitType.VANGUARD, hp=4, eid="e251"),
            ),
        ),
        (
            "646420",
            (
                FakeUnit("r18", (-659, -107), hp=1, unit_type=UnitType.RANGER),
                FakeUnit("r6", (-652, -106), hp=2, unit_type=UnitType.RANGER),
            ),
            (
                FakeEnemy((-662, -109), UnitType.RANGER, hp=1, eid="e247"),
                FakeEnemy((-663, -107), UnitType.VANGUARD, hp=4, eid="e251"),
            ),
        ),
        (
            "646421",
            (
                FakeUnit("r18", (-660, -107), hp=1, unit_type=UnitType.RANGER),
                FakeUnit("r6", (-653, -106), hp=2, unit_type=UnitType.RANGER),
            ),
            (
                FakeEnemy((-663, -109), UnitType.RANGER, hp=1, eid="e247"),
                FakeEnemy((-663, -109), UnitType.RANGER, hp=1, eid="e256"),
                FakeEnemy((-661, -105), UnitType.WORKER, hp=2, eid="e250"),
                FakeEnemy((-663, -108), UnitType.VANGUARD, hp=4, eid="e251"),
            ),
        ),
    ],
)
def test_late_squad_retreats(tick, squad, enemies):
    retreat, count, _centroid, _forbidden = tactic._attack_retreat_decision(
        enemies=enemies,
        squad_pos=squad_centroid(squad),
        squad_size=len(squad),
        radius=4,
        enemy_memory=set(),
        squad_units=squad,
        threat_radius=10,
    )

    assert retreat is True, f"t{tick} must disengage (saw {count})"


def test_old_radius_alone_still_sees_nothing():
    """Guard the diagnosis: radius 4 from the centroid cannot see x<=-660."""
    squad = _t646406_squad()

    retreat, count, _centroid, _forbidden = tactic._attack_retreat_decision(
        enemies=_t646406_enemies(),
        squad_pos=squad_centroid(squad),
        squad_size=len(squad),
        radius=4,
        enemy_memory=set(),
        squad_units=(),
    )

    assert count == 0
    assert retreat is False


def test_healthy_squad_does_not_retreat_from_one_straggler():
    # 1 enemy, 5 healthy squad members, well inside the threat radius.
    squad_pos = (0, 0)
    squad = tuple(FakeUnit(f"u{i}", (0, 0), hp=4) for i in range(5))
    enemies = (FakeEnemy((3, 0), UnitType.RANGER, hp=2),)

    retreat, _count, _centroid, _forbidden = tactic._attack_retreat_decision(
        enemies=enemies,
        squad_pos=squad_pos,
        squad_size=len(squad),
        radius=4,
        enemy_memory=set(),
        squad_units=squad,
        threat_radius=10,
    )

    assert retreat is False


def test_wounded_squad_retreats_from_a_pair():
    squad_pos = (0, 0)
    squad = (
        FakeUnit("a", (0, 0), hp=1),
        FakeUnit("b", (0, 1), hp=1),
    )
    enemies = (
        FakeEnemy((3, 0), UnitType.RANGER, hp=2, eid="e1"),
        FakeEnemy((3, 1), UnitType.RANGER, hp=2, eid="e2"),
    )

    retreat, _count, _centroid, _forbidden = tactic._attack_retreat_decision(
        enemies=enemies,
        squad_pos=squad_pos,
        squad_size=len(squad),
        radius=4,
        enemy_memory=set(),
        squad_units=squad,
        threat_radius=10,
    )

    assert retreat is True, "2 HP total vs 4 enemy HP must disengage"


# --------------------------------------------------------------------------
# home_defense_leash: schema field and config round-trip
# --------------------------------------------------------------------------

def test_home_defense_leash_is_a_config_schema_field():
    field = _FIELDS_BY_KEY["home_defense_leash"]

    assert field.group == "combat"
    assert field.kind == "integer"
    assert field.default == 12
    assert (field.minimum, field.maximum, field.step) == (0, 50, 1)
    assert any(f.key == "home_defense_leash" for f in CONFIG_FIELDS)
    assert default_config()["home_defense_leash"] == 12


def test_load_config_accepts_home_defense_leash_without_resetting(tmp_path):
    """A file carrying the key must not silently fall back to all defaults."""
    path = tmp_path / "tactic_config.json"
    path.write_text(
        json.dumps({
            "home_defense_leash": 7,
            "attack_retreat_radius": 17,
            "home_engage_radius": 8,
        }),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config["home_defense_leash"] == 7
    # The old failure mode: unknown key -> ConfigValidationError -> the whole
    # file discarded, these two back at defaults (5 and 10).
    assert config["attack_retreat_radius"] == 17
    assert config["home_engage_radius"] == 8


# --------------------------------------------------------------------------
# P1-D: home defenders are not marched 45 cells to a death hotspot
# --------------------------------------------------------------------------

def _plan_defense(unit_pos, target_pos, leash=None):
    config = default_config()
    config["home_team"] = "V1"
    config["home_patrol_radius"] = 3
    config["home_engage_radius"] = 10
    if leash is not None:
        config["home_defense_leash"] = leash

    unit = FakeUnit("v1", unit_pos, hp=4)
    enemy = FakeEnemy(target_pos, UnitType.RANGER, hp=2, eid="camp")

    saved = tactic.turn_context.defense_targets
    tactic.turn_context.defense_targets = (enemy,)
    try:
        action, detail = tactic._plan_home_combat(
            unit,
            unit_kind="vanguard",
            enemies=(),
            obstacle_cells=frozenset(),
            core_pos=(0, 0),
            config=config,
        )
    finally:
        tactic.turn_context.defense_targets = saved
    return action, detail, unit


def test_far_defense_hotspot_is_not_chased():
    # t646408: V1 at (-636,-86) was ordered to (-660,-107), 45 cells out.
    action, detail, _unit = _plan_defense((0, 0), (45, 0))

    assert "home-intercept" not in detail, f"chased 45 cells: {detail}"
    assert action in ("MOVE", "WAIT")


def test_nearby_camper_is_still_intercepted():
    action, detail, _unit = _plan_defense((0, 0), (8, 0))

    assert action == "MOVE"
    assert "home-intercept" in detail or "home-engage" in detail


def test_leash_zero_restores_unlimited_chase():
    action, detail, _unit = _plan_defense((0, 0), (45, 0), leash=0)

    assert "home-intercept" in detail or "home-engage" in detail


# --------------------------------------------------------------------------
# End-to-end: the retreat verdict short-circuits engagement
# --------------------------------------------------------------------------

def test_plan_attack_combat_follows_the_retreat_verdict():
    config = default_config()
    config["attack_mode"] = "auto"
    config["attack_target_x"] = -664
    config["attack_target_y"] = -108

    unit = FakeUnit("v12", (-659, -108), hp=2)
    enemies = (
        FakeEnemy((-660, -107), UnitType.RANGER, hp=2, eid="e249"),
        FakeEnemy((-662, -108), UnitType.VANGUARD, hp=4, eid="e251"),
    )

    tactic.turn_context.attack_retreat = True
    tactic.turn_context.attack_retreat_from = (-661, -107)
    tactic.turn_context.attack_squad_pos = (-652, -106)
    tactic.turn_context.core_pos = (-626, -73)
    tactic.turn_context.tick = 646411

    action, detail = tactic._plan_attack_combat(
        unit,
        unit_kind="vanguard",
        enemies=enemies,
        obstacle_cells=frozenset(),
        config=config,
        core_pos=(-626, -73),
    )

    assert action == "MOVE", detail
    assert "attack-retreat" in detail, detail
    # The order backs off from the cluster centroid (-661,-107); assert the
    # landing cell is strictly farther from it instead of hard-coding a compass
    # direction (the direction comes from squad-centroid vs cluster geometry).
    assert unit.moved, detail
    cell = landing(unit, (-659, -108))
    before = abs(-659 + 661) + abs(-108 + 107)
    after = abs(cell[0] + 661) + abs(cell[1] + 107)
    assert after > before, f"stepped toward the cluster: {cell}"
