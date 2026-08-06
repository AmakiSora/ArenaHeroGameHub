"""Accumulated battle-report statistics for the Arena Hero bot.

Tactic aggregates every Tick's resolution events into a single cumulative
dict and persists it to ``game_stats.json`` (matching the map_memory.json
pattern). The dashboard reads that file to render economy / combat /
production stats and per-unit details. Stats survive process restarts.

Data sources (all from the previous Tick's ``turn.events``):
- HARVEST_SUCCEEDED / DEPOSIT_SUCCEEDED: worker id + amount → per-worker mining
- SHOT_HIT / SHOT_MISSED: shooter id → per-unit shots / hits
- UNIT_SELF_DESTRUCTED: self-culled units
- DESTRUCTION_PARTICIPATION: kills are global only — the server never sets an
  actor id on these events, so per-unit kills are impossible.
- Friendly-unit deaths: no event exists, so they are detected by diffing the
  unit snapshots between Ticks (present → gone = died).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _data_dir() -> Path:
    raw = os.environ.get("ARENA_DATA_DIR", "").strip()
    return Path(raw).resolve() if raw else Path(__file__).resolve().parent


STATS_PATH = _data_dir() / "game_stats.json"

_SAVE_INTERVAL_TICKS = 20
_SAMPLE_INTERVAL_TICKS = 200
_SAMPLE_MAX = 60

_UNIT_TYPES = ("WORKER", "VANGUARD", "RANGER")
_COMBAT_TYPES = ("VANGUARD", "RANGER")
_MOTION_STATES = (
    "stationary", "moving_unstable", "moving_stable", "uncertain", "insufficient",
    "legacy",
)


def _zeros(keys: tuple[str, ...]) -> dict[str, int]:
    return {key: 0 for key in keys}


def _new_unit_type_set() -> dict[str, int]:
    return _zeros(_UNIT_TYPES)


def _prediction_counter() -> dict[str, int]:
    return {
        "candidates": 0,
        "legal_candidates": 0,
        "eligible_candidates": 0,
        "resolved": 0,
        "predicted_correct": 0,
        "predicted_wrong": 0,
        "unknown": 0,
        "baseline_hits": 0,
        "baseline_misses": 0,
        "improvements": 0,
        "harms": 0,
    }


def new_stats() -> dict[str, Any]:
    """Return a fresh cumulative statistics document."""
    return {
        "version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "start_tick": 0,
        "current_tick": 0,
        "economy": {
            "harvested_total": 0,
            "harvest_count": 0,
            "deposited_total": 0,
            "deposit_count": 0,
            "harvest_failed": 0,
            "beacon_harvest_bonus": 0,
            "moves_succeeded": 0,
            "moves_failed": 0,
        },
        "production": {
            "spawned": _new_unit_type_set(),
            "spawn_failed": 0,
            "self_destructed": _new_unit_type_set(),
        },
        "combat": {
            "vanguard_shots": 0,
            "vanguard_hits": 0,
            "ranger_shots": 0,
            "ranger_hits": 0,
            "kill_participations": 0,
            "damage_taken": 0,
            "sweeps_resolved": 0,
        },
        "deaths": _new_unit_type_set(),
        "shot_prediction": {
            **_prediction_counter(),
            "by_streak": {
                "0": _prediction_counter(),
                "1": _prediction_counter(),
                "2": _prediction_counter(),
                "3_plus": _prediction_counter(),
            },
            "by_target_type": {
                unit_type: _prediction_counter()
                for unit_type in (*_UNIT_TYPES, "CORE", "ENEMY")
            },
            "by_motion_state": {
                state: _prediction_counter() for state in _MOTION_STATES
            },
        },
        "types": {},  # short id -> UNIT_TYPE; kept after death to tag old events
        "per_worker": {},  # short id -> worker record
        "per_combat": {},  # short id -> combat record
        "samples": [],  # [{tick, harvested_total, deposited_total, workers}]
    }


def load(path: Path | str = STATS_PATH) -> dict[str, Any]:
    """Load persisted stats, or return a fresh document when absent/invalid."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("version") == 1:
            # Merge over a fresh skeleton so newer fields default correctly.
            merged = new_stats()
            merged["started_at"] = raw.get("started_at", merged["started_at"])
            merged["start_tick"] = int(raw.get("start_tick") or 0)
            merged["current_tick"] = int(raw.get("current_tick") or 0)
            for section in ("economy", "production", "combat", "deaths"):
                merged[section].update(raw.get(section, {}) or {})
            prediction = raw.get("shot_prediction", {}) or {}
            for key in _prediction_counter():
                merged["shot_prediction"][key] = int(prediction.get(key, 0) or 0)
            for group in ("by_streak", "by_target_type", "by_motion_state"):
                for name, values in (prediction.get(group, {}) or {}).items():
                    bucket = merged["shot_prediction"][group].setdefault(
                        name, _prediction_counter(),
                    )
                    bucket.update({
                        key: int(value or 0)
                        for key, value in (values or {}).items()
                        if key in bucket
                    })
            if not prediction.get("by_motion_state"):
                legacy = merged["shot_prediction"]["by_motion_state"]["legacy"]
                legacy.update({
                    key: int(prediction.get(key, 0) or 0)
                    for key in _prediction_counter()
                })
            merged["types"] = dict(raw.get("types", {}) or {})
            merged["per_worker"] = dict(raw.get("per_worker", {}) or {})
            merged["per_combat"] = dict(raw.get("per_combat", {}) or {})
            merged["samples"] = list(raw.get("samples", []) or [])[-_SAMPLE_MAX:]
            # Version-1 files written before combat deaths were counted still
            # contain died_tick records. Backfill their aggregate totals while
            # preserving any larger historical count already on disk.
            for unit_type in _COMBAT_TYPES:
                detected = sum(
                    1
                    for rec in merged["per_combat"].values()
                    if rec.get("type") == unit_type and rec.get("died_tick") is not None
                )
                merged["deaths"][unit_type] = max(
                    int(merged["deaths"].get(unit_type, 0) or 0),
                    detected,
                )
            return merged
    except (OSError, json.JSONDecodeError):
        pass
    return new_stats()


def save(stats: dict[str, Any], path: Path | str = STATS_PATH) -> None:
    """Persist stats atomically (tmp + replace, matching map_memory.py)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ── per-Tick aggregation ─────────────────────────────────────────────────────

def _short_id(uid: Any) -> str:
    return str(uid)[:8]


def _type_of(unit: Any) -> str:
    t = getattr(unit, "unit_type", None)
    if hasattr(t, "name"):
        return t.name
    return str(t)


def sync_units(stats: dict[str, Any], turn: Any, tick: int) -> None:
    """Detect births (spawned) and deaths by diffing the unit snapshot.

    The first Tick establishes a baseline: existing units are remembered but
    NOT counted as spawned (we only report *new* production from then on).
    """
    is_first = not stats.get("start_tick")
    if is_first:
        stats["start_tick"] = tick
    stats["current_tick"] = tick

    alive: dict[str, str] = {}  # short id -> type
    for unit in getattr(turn, "units", ()) or ():
        sid = _short_id(unit.id)
        unit_type = _type_of(unit)
        alive[sid] = unit_type
        stats["types"][sid] = unit_type  # refresh, handles short-id reuse

    worker_sids = {sid for sid, t in alive.items() if t == "WORKER"}
    combat_sids = {sid for sid, t in alive.items() if t in _COMBAT_TYPES}

    # Births — new ids not yet tracked. Skip counting on the baseline Tick.
    for sid in worker_sids:
        rec = stats["per_worker"].get(sid)
        if rec is None:
            stats["per_worker"][sid] = {
                "harvested": 0, "harvest_count": 0, "deposited": 0,
                "born_tick": tick, "died_tick": None,
            }
            if not is_first:
                stats["production"]["spawned"]["WORKER"] += 1
        elif rec.get("died_tick") is not None:
            rec["died_tick"] = None  # seen alive again

    for sid in combat_sids:
        rec = stats["per_combat"].get(sid)
        if rec is None:
            stats["per_combat"][sid] = {
                "type": alive[sid],
                "shots": 0, "hits": 0,
                "born_tick": tick, "died_tick": None,
            }
            if not is_first:
                stats["production"]["spawned"][alive[sid]] += 1
        elif rec.get("died_tick") is not None:
            rec["died_tick"] = None

    # Deaths — a tracked unit that was alive and is now gone.
    for sid, rec in list(stats["per_worker"].items()):
        if rec.get("died_tick") is None and sid not in worker_sids:
            rec["died_tick"] = tick
            unit_type = stats["types"].get(sid)
            if unit_type:
                stats["deaths"][unit_type] += 1
    for sid, rec in list(stats["per_combat"].items()):
        if rec.get("died_tick") is None and sid not in combat_sids:
            rec["died_tick"] = tick
            unit_type = stats["types"].get(sid)
            if unit_type in _COMBAT_TYPES:
                stats["deaths"][unit_type] += 1


def record_events(stats: dict[str, Any], turn: Any, tick: int) -> None:
    """Fold the previous Tick's resolution events into the cumulative stats."""
    economy = stats["economy"]
    production = stats["production"]
    combat = stats["combat"]
    tick = tick or int(stats.get("current_tick") or 0)

    def worker_rec(actor: str) -> dict[str, Any]:
        rec = stats["per_worker"].get(actor)
        if rec is None:
            rec = {
                "harvested": 0, "harvest_count": 0, "deposited": 0,
                "born_tick": tick, "died_tick": None,
            }
            stats["per_worker"][actor] = rec
        return rec

    def combat_rec(actor: str, unit_type: str) -> dict[str, Any]:
        rec = stats["per_combat"].get(actor)
        if rec is None:
            rec = {
                "type": unit_type, "shots": 0, "hits": 0,
                "born_tick": tick, "died_tick": None,
            }
            stats["per_combat"][actor] = rec
        return rec

    for event in getattr(turn, "events", ()) or ():
        event_type = str(getattr(event, "event_type", ""))
        actor = _short_id(getattr(event, "actor_id", None) or "")
        values = getattr(event, "values", None) or {}
        amount = values.get("amount") if isinstance(values, dict) else None
        amt = int(amount) if isinstance(amount, int) else 0

        if event_type == "HARVEST_SUCCEEDED":
            economy["harvested_total"] += amt
            economy["harvest_count"] += 1
            if actor:
                rec = worker_rec(actor)
                rec["harvested"] += amt
                rec["harvest_count"] += 1
        elif event_type == "HARVEST_FAILED":
            economy["harvest_failed"] += 1
        elif event_type == "DEPOSIT_SUCCEEDED":
            economy["deposited_total"] += amt
            economy["deposit_count"] += 1
            if actor:
                rec = worker_rec(actor)
                rec["deposited"] += amt
        elif event_type == "BEACON_HARVEST_BONUS":
            economy["beacon_harvest_bonus"] += 1
        elif event_type == "UNIT_MOVE_SUCCEEDED":
            economy["moves_succeeded"] += 1
        elif event_type == "UNIT_MOVE_FAILED":
            economy["moves_failed"] += 1
        elif event_type == "CORE_SPAWN_SUCCEEDED":
            pass  # type attribution happens via sync_units' birth detection
        elif event_type == "CORE_SPAWN_FAILED":
            production["spawn_failed"] += 1
        elif event_type == "UNIT_SELF_DESTRUCTED":
            unit_type = stats["types"].get(actor, "WORKER")
            if unit_type in production["self_destructed"]:
                production["self_destructed"][unit_type] += 1
        elif event_type in ("SHOT_HIT", "SHOT_MISSED"):
            unit_type = stats["types"].get(actor)
            if unit_type not in _COMBAT_TYPES:
                continue  # unknown shooter — ignore
            is_hit = event_type == "SHOT_HIT"
            prefix = "vanguard" if unit_type == "VANGUARD" else "ranger"
            combat[f"{prefix}_shots"] += 1
            if is_hit:
                combat[f"{prefix}_hits"] += 1
            rec = combat_rec(actor, unit_type)
            rec["shots"] += 1
            if is_hit:
                rec["hits"] += 1
        elif event_type == "DESTRUCTION_PARTICIPATION":
            combat["kill_participations"] += 1
        elif event_type == "UNIT_DAMAGED":
            combat["damage_taken"] += 1
        elif event_type == "SWEEP_RESOLVED":
            combat["sweeps_resolved"] += 1


def _prediction_buckets(
    stats: dict[str, Any], item: dict[str, Any],
) -> list[dict[str, int]]:
    prediction = stats["shot_prediction"]
    streak = int(item.get("move_streak", 0) or 0)
    streak_key = str(streak) if streak < 3 else "3_plus"
    target_type = str(item.get("target_type") or "ENEMY").upper()
    motion_state = str(item.get("motion_state") or "legacy").lower()
    return [
        prediction,
        prediction["by_streak"].setdefault(streak_key, _prediction_counter()),
        prediction["by_target_type"].setdefault(target_type, _prediction_counter()),
        prediction["by_motion_state"].setdefault(
            motion_state, _prediction_counter(),
        ),
    ]


def record_prediction_candidates(
    stats: dict[str, Any], candidates: list[dict[str, Any]],
) -> None:
    """Count accepted shadow predictions without changing combat behavior."""
    for item in candidates:
        for bucket in _prediction_buckets(stats, item):
            bucket["candidates"] += 1
            if item.get("prediction_legal"):
                bucket["legal_candidates"] += 1
            if item.get("eligible"):
                bucket["eligible_candidates"] += 1


def record_prediction_results(
    stats: dict[str, Any], results: list[dict[str, Any]],
) -> None:
    """Aggregate next-Tick outcomes for previously accepted shadow shots."""
    for item in results:
        predicted_match = item.get("predicted_match")
        shot_result = str(item.get("shot_result") or "UNRESOLVED")
        for bucket in _prediction_buckets(stats, item):
            bucket["resolved"] += 1
            if predicted_match is True:
                bucket["predicted_correct"] += 1
            elif predicted_match is False:
                bucket["predicted_wrong"] += 1
            else:
                bucket["unknown"] += 1
            if shot_result == "SHOT_HIT":
                bucket["baseline_hits"] += 1
            elif shot_result == "SHOT_MISSED":
                bucket["baseline_misses"] += 1
            if (
                item.get("eligible")
                and predicted_match is True
                and shot_result == "SHOT_MISSED"
            ):
                bucket["improvements"] += 1
            if (
                item.get("eligible")
                and predicted_match is False
                and shot_result == "SHOT_HIT"
            ):
                bucket["harms"] += 1


def sampled(stats: dict[str, Any], tick: int) -> None:
    """Append a lightweight economy snapshot every _SAMPLE_INTERVAL_TICKS."""
    samples = stats.get("samples") or []
    if not samples or tick - samples[-1]["tick"] >= _SAMPLE_INTERVAL_TICKS:
        samples.append({
            "tick": tick,
            "harvested_total": stats["economy"]["harvested_total"],
            "deposited_total": stats["economy"]["deposited_total"],
            "workers": len(stats["per_worker"]),
        })
        if len(samples) > _SAMPLE_MAX:
            del samples[:len(samples) - _SAMPLE_MAX]
        stats["samples"] = samples


def maybe_save(stats: dict[str, Any], tick: int, interval: int = _SAVE_INTERVAL_TICKS) -> bool:
    """Save every `interval` Ticks; returns True when a save happened."""
    if tick - int(stats.get("_saved_tick") or stats.get("start_tick") or 0) < interval:
        return False
    stats["_saved_tick"] = tick
    try:
        save(stats)
    except OSError:
        return False
    return True


# ── derived metrics (dashboard) ─────────────────────────────────────────────

def _rate(hits: int, shots: int) -> float:
    return round(hits * 100 / shots, 1) if shots else 0.0


def derive(stats: dict[str, Any], alive_workers: int = 0) -> dict[str, Any]:
    """Compute human-facing efficiency numbers from cumulative stats."""
    eco = stats["economy"]
    combat = stats["combat"]
    ticks = max(1, int(stats.get("current_tick") or 0) - int(stats.get("start_tick") or 0))
    workers = alive_workers or max(1, len(stats["per_worker"]))

    window_rate = 0.0
    samples = stats.get("samples") or []
    if len(samples) >= 2:
        first, last = samples[0], samples[-1]
        span = last["tick"] - first["tick"]
        if span > 0:
            window_rate = round(
                (last["harvested_total"] - first["harvested_total"]) / span, 2
            )

    return {
        "ticks": ticks,
        "harvest_per_tick": round(eco["harvested_total"] / ticks, 2),
        "harvest_per_worker": round(eco["harvested_total"] / workers, 1),
        "deposit_per_tick": round(eco["deposited_total"] / ticks, 2),
        "vanguard_hit_rate": _rate(combat["vanguard_hits"], combat["vanguard_shots"]),
        "ranger_hit_rate": _rate(combat["ranger_hits"], combat["ranger_shots"]),
        "window_harvest_per_tick": window_rate,
    }
