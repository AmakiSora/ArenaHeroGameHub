#!/usr/bin/env python3
"""Read tactic_log.jsonl as a per-Tick combat timeline.

This is the offline companion to the live dashboard: it prints state, planned
actions, resolution events, squad verdict inputs and (optionally) an ASCII board
for one Tick range. It accepts the current log and its rotated backups, and it
stays readable on v3 segments that predate the structured event fields.

Examples:
    python replay.py --from 646385 --to 646436 --file _review/tactic_log.jsonl
    python replay.py --tick 646406 --board --file _review/tactic_log.jsonl
    python replay.py --from 646400 --to 646420 --unit V12 --events
    python replay.py --from 646400 --to 646410 --json > ticks.jsonl
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from state_io import data_path


def default_paths() -> list[Path]:
    """Current log + rotated backups, oldest first so newer files win.

    Prefers the runtime log; falls back to the checked-out review copy so
    ``python replay.py --from ...`` works in a fresh clone without a flag.
    """
    base = Path(data_path("tactic_log.jsonl"))
    if not base.exists():
        review = Path("_review/tactic_log.jsonl")
        if review.exists():
            base = review
    paths = [base.with_name(f"{base.name}.{i}") for i in (3, 2, 1)]
    paths.append(base)
    return paths


def load_records(paths: Iterable[Path]) -> list[dict]:
    """Load tick records from one or more JSONL segments, deduped by Tick.

    Files are read in the order given; a later file wins on a duplicate tick
    (current log over a backup). Header/summary rows and malformed lines are
    skipped rather than aborting the review.
    """
    records: dict[int, dict] = {}
    for path in paths:
        path = Path(path)
        if not path.exists():
            continue
        try:
            if path.name.endswith(".gz"):
                handle = gzip.open(path, "rt", encoding="utf-8")
            else:
                handle = path.open("r", encoding="utf-8")
        except OSError as exc:
            print(f"[replay] cannot read {path}: {exc}", file=sys.stderr)
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("_meta") or rec.get("_summary"):
                    continue
                tick = rec.get("tick")
                if isinstance(tick, int):
                    records[tick] = rec
    return [records[tick] for tick in sorted(records)]


def _cell(pos) -> str:
    if not pos or len(pos) != 2:
        return "?"
    return f"({pos[0]},{pos[1]})"


def _name_map(rec: Mapping) -> dict[str, str]:
    names: dict[str, str] = {}
    for group in ("workers", "vanguards", "rangers"):
        for unit in rec.get(group) or ():
            if isinstance(unit, Mapping) and unit.get("id"):
                names[str(unit["id"])] = str(unit.get("name") or unit["id"])
    for enemy in rec.get("enemies") or ():
        if isinstance(enemy, Mapping) and enemy.get("id"):
            names[str(enemy["id"])] = str(enemy.get("name") or enemy["id"])
    return names


def _unit_text(unit: Mapping) -> str:
    extra = ""
    if unit.get("cargo") is not None:
        extra += f" cargo={unit['cargo']}"
    if unit.get("state"):
        extra += f" {unit['state']}"
    if unit.get("shield") is not None:
        extra += f" shield={unit['shield']}"
    if unit.get("move"):
        move = unit["move"]
        extra += (
            f" move={move.get('direction')} "
            f"{move.get('progress')}/{move.get('required_ticks')}"
        )
        if move.get("destination"):
            extra += f"->{_cell(move['destination'])}"
    label = str(unit.get("name") or unit.get("id"))
    if unit.get("type"):
        label += f"[{unit['type']}]"
    return f"{label}{_cell(unit.get('pos'))} hp={unit.get('hp')}{extra}"


def _format_values(values: Mapping | None) -> str:
    if not values:
        return ""
    return " " + " ".join(f"{key}={values[key]}" for key in sorted(values))


def _format_event(event: Mapping, names: Mapping[str, str]) -> str:
    actor = event.get("actor_name") or names.get(str(event.get("actor"))) or event.get("actor")
    target = event.get("target_name") or names.get(str(event.get("target"))) or event.get("target")
    who = str(actor or "-")
    if target:
        who += f"->{target}"
    line = f"{who} {event.get('type') or '?'}"
    if event.get("pos"):
        line += f" {_cell(event['pos'])}"
    values = event.get("values")
    if not values and event.get("amount") is not None:
        values = {"amount": event["amount"]}
    line += _format_values(values)
    if event.get("resolved_tick") is not None:
        line += f" resolved={event['resolved_tick']}"
    return line


def _format_decision(decisions: Mapping) -> list[str]:
    lines: list[str] = []
    attack = decisions.get("attack")
    if isinstance(attack, Mapping):
        parts = [
            f"retreat={attack.get('retreat')}",
            f"enemy_count={attack.get('enemy_count')}",
            f"enemy_hp={attack.get('enemy_hp')}",
            f"friend_hp={attack.get('friend_hp')}",
            f"reach={attack.get('reach')}",
        ]
        if attack.get("squad_pos") is not None:
            parts.append(f"squad={_cell(attack['squad_pos'])}/{attack.get('squad_size')}")
        if attack.get("centroid"):
            parts.append(f"centroid={_cell(attack['centroid'])}")
        if attack.get("forbidden"):
            parts.append(f"forbidden={attack['forbidden']}")
        lines.append("attack " + " ".join(parts))
    home = decisions.get("home_defense")
    if isinstance(home, list):
        for row in home:
            if not isinstance(row, Mapping):
                continue
            lines.append(
                "home "
                f"{row.get('unit')} leash={row.get('leash')} "
                f"kept={row.get('kept')} dropped={row.get('dropped')} "
                f"target={_cell(row.get('target'))}"
            )
    fuse = decisions.get("sweep_fuse")
    if isinstance(fuse, Mapping):
        for uid, row in sorted(fuse.items()):
            if not isinstance(row, Mapping):
                continue
            parts = [f"{uid}"]
            if row.get("remaining") is not None:
                parts.append(f"remaining={row['remaining']}")
            if row.get("miss_streak") is not None:
                parts.append(f"miss_streak={row['miss_streak']}")
            lines.append("sweep-fuse " + " ".join(parts))
    stuck = decisions.get("waypoint_stuck")
    if isinstance(stuck, Mapping):
        for uid, row in sorted(stuck.items()):
            if isinstance(row, Mapping):
                lines.append(
                    f"waypoint-stuck {uid} ticks={row.get('ticks')} "
                    f"target={_cell(row.get('target'))}"
                )
    return lines


def _board(rec: Mapping, *, all_units: bool = False) -> list[str]:
    """Small ASCII board around the nearest fight (or the Core when idle)."""
    marks: dict[tuple[int, int], str] = {}
    core = rec.get("core_pos")
    if core and len(core) == 2:
        marks[(int(core[0]), int(core[1]))] = "C"
    combat_positions: list[tuple[int, int]] = []
    for group, letter in (("workers", "W"), ("vanguards", "V"), ("rangers", "R")):
        if group == "workers" and not all_units:
            continue
        for unit in rec.get(group) or ():
            pos = unit.get("pos")
            if pos and len(pos) == 2:
                cell = (int(pos[0]), int(pos[1]))
                marks[cell] = letter
                if letter in ("V", "R"):
                    combat_positions.append(cell)
    enemy_positions: list[tuple[int, int]] = []
    for enemy in rec.get("enemies") or ():
        pos = enemy.get("pos")
        if not pos or len(pos) != 2:
            continue
        etype = str(enemy.get("type") or "?").upper()
        letter = {"WORKER": "w", "VANGUARD": "v", "RANGER": "r", "CORE": "e"}.get(etype, "e")
        cell = (int(pos[0]), int(pos[1]))
        marks.setdefault(cell, letter)
        enemy_positions.append(cell)
    resources = {
        (int(pos[0]), int(pos[1]))
        for pos in rec.get("resource_cells") or ()
        if isinstance(pos, (list, tuple)) and len(pos) == 2
    }
    if not marks:
        return ["board: (no objects)"]
    # Focus on the closest friendly/enemy pair so a 60x24 crop shows the fight,
    # not the idle Core 30 cells away.
    focus = None
    if combat_positions and enemy_positions:
        a, b = min(
            ((a, b) for a in combat_positions for b in enemy_positions),
            key=lambda pair: max(
                abs(pair[0][0] - pair[1][0]), abs(pair[0][1] - pair[1][1]),
            ),
        )
        focus = ((a[0] + b[0]) // 2, (a[1] + b[1]) // 2)
    if focus is None:
        focus = (
            (int(core[0]), int(core[1]))
            if core and len(core) == 2
            else (sum(x for x, _ in marks) // len(marks), sum(y for _, y in marks) // len(marks))
        )
    max_w, max_h = 60, 24
    min_x, max_x = focus[0] - max_w // 2, focus[0] + max_w // 2
    min_y, max_y = focus[1] - max_h // 2, focus[1] + max_h // 2
    lines = []
    for y in range(min_y, max_y + 1):
        row = []
        for x in range(min_x, max_x + 1):
            if (x, y) in marks:
                row.append(marks[(x, y)])
            elif (x, y) in resources:
                row.append("*")
            else:
                row.append(".")
        lines.append("".join(row))
    return lines


def format_record(
    rec: Mapping,
    *,
    events_only: bool = False,
    board: bool = False,
    all_units: bool = False,
    all_events: bool = False,
) -> list[str]:
    """Render one Tick record as text lines."""
    tick = rec.get("tick")
    names = _name_map(rec)
    if events_only:
        lines = []
        for event in rec.get("events") or ():
            if not isinstance(event, Mapping):
                continue
            if not all_events and event.get("type") == "UNIT_MOVE_SUCCEEDED":
                continue
            lines.append(f"tick {tick} event {_format_event(event, names)}")
        return lines

    core = (
        f"{rec.get('core_name') or 'C'} {_cell(rec.get('core_pos'))} "
        f"hp={rec.get('core_hp')} shield={rec.get('core_shield')} "
        f"{rec.get('core_state') or '?'}"
    )
    meta = [
        f"tick {tick}",
        str(rec.get("timestamp") or ""),
        f"core {core}",
        f"res={rec.get('resources')}/{rec.get('resource_capacity')}",
        f"pop={rec.get('population')}",
        f"action={rec.get('core_action')}" if rec.get("core_action") else "",
        "accepted" if rec.get("accepted") else "rejected",
    ]
    lines = ["  ".join(part for part in meta if part)]
    if rec.get("config_rev"):
        changed = rec.get("config_changed") or []
        lines.append(
            f"  config rev={rec['config_rev']}"
            + (f" changed={','.join(str(key) for key in changed)}" if changed else "")
        )
        delta = rec.get("config_delta")
        if isinstance(delta, Mapping) and delta:
            lines.append(
                "  config-delta "
                + " ".join(f"{key}={delta[key]}" for key in sorted(delta))
            )
    core_move = rec.get("core_move")
    if isinstance(core_move, Mapping):
        lines.append(
            "  core-move "
            f"{core_move.get('direction')} "
            f"{core_move.get('progress')}/{core_move.get('required_ticks')} "
            f"-> {_cell(core_move.get('destination'))}"
        )
    units = []
    groups = ("workers", "vanguards", "rangers") if all_units else ("vanguards", "rangers")
    for group in groups:
        for unit in rec.get(group) or ():
            if isinstance(unit, Mapping):
                units.append(_unit_text(unit))
    if not all_units and rec.get("workers"):
        units.append(f"workers={len(rec['workers'])} (use --all-units)")
    if units:
        lines.append("  units    " + " | ".join(units))
    enemies = []
    for enemy in rec.get("enemies") or ():
        if isinstance(enemy, Mapping):
            enemies.append(_unit_text(enemy))
    if enemies:
        lines.append("  enemies  " + " | ".join(enemies))
    actions = rec.get("plan_unit_actions") or {}
    if actions:
        visible_ids = set()
        for group in groups:
            for unit in rec.get(group) or ():
                if isinstance(unit, Mapping) and unit.get("id"):
                    visible_ids.add(str(unit["id"]))
        rendered = []
        for uid, action in actions.items():
            if not all_units and str(uid) not in visible_ids:
                continue
            rendered.append(f"{names.get(str(uid), uid)}: {action}")
        if rendered:
            lines.append("  actions  " + " | ".join(rendered))
    for event in rec.get("events") or ():
        if not isinstance(event, Mapping):
            continue
        if not all_events and event.get("type") == "UNIT_MOVE_SUCCEEDED":
            continue
        lines.append("  event    " + _format_event(event, names))
    for line in _format_decision(rec.get("decisions") or {}):
        lines.append("  decision " + line)
    for row in rec.get("shot_prediction_results") or ():
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "  shot "
            f"{row.get('ranger_name') or row.get('ranger_id')}->"
            f"{row.get('target_name') or row.get('target_id')} "
            f"{row.get('shot_result')} actual={_cell(row.get('actual_cell'))} "
            f"predicted={_cell(row.get('predicted_cell'))} "
            f"mode={row.get('fire_mode')}"
        )
    for key, label in (
        ("kite_decisions", "kite"),
        ("guerrilla_decisions", "guerrilla"),
    ):
        for row in rec.get(key) or ():
            if not isinstance(row, Mapping):
                continue
            lines.append(
                f"  {label} {row.get('unit_name') or row.get('unit_id')} "
                f"{row.get('action')} "
                f"objective={_cell(row.get('objective'))} "
                f"reason={row.get('reason')}"
            )
    for key, label in (
        ("auto_target_candidates", "attack-candidates"),
        ("kite_target_candidates", "kite-candidates"),
    ):
        for row in rec.get(key) or ():
            if not isinstance(row, Mapping):
                continue
            lines.append(
                f"  {label} {_cell(row.get('cell'))} score={row.get('score')} "
                f"age={row.get('age')} squad={row.get('distance_squad')} "
                f"core={row.get('distance_core')} "
                f"type={row.get('type')}"
                + (f" priority={row['priority']}" if row.get("priority") else "")
            )
    if board:
        lines.extend("  " + line for line in _board(rec, all_units=all_units))
    return lines


def _matches_unit(text: str, needle: str) -> bool:
    return needle.lower() in text.lower()


def _filter_units(rec: Mapping, needle: str) -> dict:
    """Keep only matching own units and their actions; enemies stay as context."""
    names = _name_map(rec)
    filtered = dict(rec)
    for group in ("workers", "vanguards", "rangers"):
        filtered[group] = [
            unit for unit in rec.get(group) or ()
            if _matches_unit(str(unit.get("name") or ""), needle)
            or _matches_unit(str(unit.get("id") or ""), needle)
        ]
    filtered["plan_unit_actions"] = {
        uid: action for uid, action in (rec.get("plan_unit_actions") or {}).items()
        if _matches_unit(str(names.get(str(uid), uid)), needle)
        or _matches_unit(str(uid), needle)
    }
    filtered["events"] = [
        event for event in rec.get("events") or ()
        if _matches_unit(
            " ".join(str(event.get(key) or "") for key in (
                "actor", "actor_name", "target", "target_name",
            )),
            needle,
        )
    ]
    filtered["kite_decisions"] = [
        row for row in rec.get("kite_decisions") or ()
        if _matches_unit(str(row.get("unit_name") or "") + str(row.get("unit_id") or ""), needle)
    ]
    filtered["guerrilla_decisions"] = [
        row for row in rec.get("guerrilla_decisions") or ()
        if _matches_unit(str(row.get("unit_name") or "") + str(row.get("unit_id") or ""), needle)
    ]
    return filtered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--file", action="append", default=[], help="JSONL log path (repeatable)")
    parser.add_argument("--from", dest="from_tick", type=int, help="first tick (inclusive)")
    parser.add_argument("--to", dest="to_tick", type=int, help="last tick (inclusive)")
    parser.add_argument("--tick", type=int, help="single tick")
    parser.add_argument("--unit", help="only this unit name or id prefix")
    parser.add_argument("--events", action="store_true", help="print events only")
    parser.add_argument("--all-events", action="store_true", help="include movement-success rows")
    parser.add_argument("--all-units", action="store_true", help="list workers too")
    parser.add_argument("--board", action="store_true", help="draw an ASCII board")
    parser.add_argument("--json", action="store_true", help="emit filtered records as JSONL")
    parser.add_argument("--limit", type=int, default=0, help="print at most N ticks (0 = all)")
    args = parser.parse_args(argv)

    paths = [Path(path) for path in args.file] if args.file else default_paths()
    records = load_records(paths)
    if args.tick is not None:
        records = [rec for rec in records if rec.get("tick") == args.tick]
    else:
        if args.from_tick is not None:
            records = [rec for rec in records if rec.get("tick", 0) >= args.from_tick]
        if args.to_tick is not None:
            records = [rec for rec in records if rec.get("tick", 0) <= args.to_tick]
    if args.unit:
        records = [_filter_units(rec, args.unit) for rec in records]
    if args.limit and args.limit > 0:
        records = records[: args.limit]
    if not records:
        print("[replay] no tick records matched", file=sys.stderr)
        return 1

    if args.json:
        for rec in records:
            print(json.dumps(rec, ensure_ascii=False))
        return 0

    print(f"[replay] {len(records)} tick(s), {records[0].get('tick')}..{records[-1].get('tick')}")
    for rec in records:
        for line in format_record(
            rec,
            events_only=args.events,
            board=args.board,
            all_units=bool(args.all_units or args.unit),
            all_events=args.all_events,
        ):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
