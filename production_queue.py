"""Persistent FIFO production queue shared by the dashboard and tactic."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _data_dir() -> Path:
    raw = os.environ.get("ARENA_DATA_DIR", "").strip()
    return Path(raw).resolve() if raw else Path(__file__).resolve().parent


QUEUE_PATH = _data_dir() / "production_queue.db"
MAX_QUEUE_SIZE = 20
UNIT_COSTS = {
    "WORKER": 5,
    "VANGUARD": 10,
    "RANGER": 12,
}
UNIT_LABELS = {
    "WORKER": "工人",
    "VANGUARD": "先锋",
    "RANGER": "游侠",
}


class ProductionQueueError(RuntimeError):
    pass


class InvalidUnitTypeError(ProductionQueueError):
    pass


class QueueFullError(ProductionQueueError):
    pass


def _resolve_path(path: Path | None) -> Path:
    return QUEUE_PATH if path is None else Path(path)


def _connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = _resolve_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS production_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            issued_tick INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def _as_request(row: sqlite3.Row) -> dict[str, Any]:
    unit_type = str(row["unit_type"])
    return {
        "id": int(row["id"]),
        "unit_type": unit_type,
        # Fall back gracefully for rows with an unknown unit type (old schema or
        # a manual DB edit) so one bad row can't break the whole dashboard queue.
        "label": UNIT_LABELS.get(unit_type, unit_type),
        "cost": UNIT_COSTS.get(unit_type, 0),
        "status": str(row["status"]),
        "issued_tick": row["issued_tick"],
        "created_at": str(row["created_at"]),
    }


def list_requests(path: Path | None = None) -> list[dict[str, Any]]:
    with closing(_connect(path)) as connection:
        rows = connection.execute(
            "SELECT * FROM production_queue ORDER BY id"
        ).fetchall()
    return [_as_request(row) for row in rows]


def enqueue(unit_type: str, path: Path | None = None) -> dict[str, Any]:
    normalized = str(unit_type).upper()
    if normalized not in UNIT_COSTS:
        raise InvalidUnitTypeError(f"unsupported unit type: {unit_type}")

    with closing(_connect(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        count = int(connection.execute(
            "SELECT COUNT(*) FROM production_queue"
        ).fetchone()[0])
        if count >= MAX_QUEUE_SIZE:
            connection.rollback()
            raise QueueFullError(f"production queue is limited to {MAX_QUEUE_SIZE} requests")
        cursor = connection.execute(
            "INSERT INTO production_queue (unit_type, status, created_at) VALUES (?, 'pending', ?)",
            (normalized, datetime.now(timezone.utc).isoformat()),
        )
        request_id = int(cursor.lastrowid)
        connection.commit()

    return next(item for item in list_requests(path) if item["id"] == request_id)


def remove_request(request_id: int, path: Path | None = None) -> bool:
    with closing(_connect(path)) as connection:
        cursor = connection.execute(
            "DELETE FROM production_queue WHERE id = ?",
            (int(request_id),),
        )
        connection.commit()
    return cursor.rowcount > 0


def clear_requests(path: Path | None = None) -> int:
    with closing(_connect(path)) as connection:
        cursor = connection.execute("DELETE FROM production_queue")
        connection.commit()
    return cursor.rowcount


def head_request(path: Path | None = None) -> dict[str, Any] | None:
    with closing(_connect(path)) as connection:
        row = connection.execute(
            "SELECT * FROM production_queue ORDER BY id LIMIT 1"
        ).fetchone()
    return _as_request(row) if row is not None else None


def inflight_request(path: Path | None = None) -> dict[str, Any] | None:
    with closing(_connect(path)) as connection:
        row = connection.execute(
            "SELECT * FROM production_queue WHERE status = 'inflight' ORDER BY id LIMIT 1"
        ).fetchone()
    return _as_request(row) if row is not None else None


def claim_request(
    request_id: int,
    tick: int,
    path: Path | None = None,
) -> bool:
    """Atomically mark the pending head as the single in-flight request."""
    with closing(_connect(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id, status FROM production_queue ORDER BY id LIMIT 1"
        ).fetchone()
        has_inflight = connection.execute(
            "SELECT 1 FROM production_queue WHERE status = 'inflight' LIMIT 1"
        ).fetchone()
        if (
            row is None
            or int(row["id"]) != int(request_id)
            or row["status"] != "pending"
            or has_inflight is not None
        ):
            connection.rollback()
            return False
        connection.execute(
            "UPDATE production_queue SET status = 'inflight', issued_tick = ? WHERE id = ?",
            (int(tick), int(request_id)),
        )
        connection.commit()
    return True


def finish_inflight(
    request_id: int,
    *,
    succeeded: bool,
    path: Path | None = None,
) -> bool:
    with closing(_connect(path)) as connection:
        if succeeded:
            cursor = connection.execute(
                "DELETE FROM production_queue WHERE id = ? AND status = 'inflight'",
                (int(request_id),),
            )
        else:
            cursor = connection.execute(
                "UPDATE production_queue SET status = 'pending', issued_tick = NULL "
                "WHERE id = ? AND status = 'inflight'",
                (int(request_id),),
            )
        connection.commit()
    return cursor.rowcount > 0


def reset_stale_inflight(current_tick: int, path: Path | None = None) -> int:
    """Retry a request when its issued Tick passed without a spawn event."""
    with closing(_connect(path)) as connection:
        cursor = connection.execute(
            "UPDATE production_queue SET status = 'pending', issued_tick = NULL "
            "WHERE status = 'inflight' AND issued_tick < ?",
            (int(current_tick),),
        )
        connection.commit()
    return cursor.rowcount


def queue_payload(path: Path | None = None) -> dict[str, Any]:
    items = list_requests(path)
    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "limit": MAX_QUEUE_SIZE,
        "costs": dict(UNIT_COSTS),
        "labels": dict(UNIT_LABELS),
    }
