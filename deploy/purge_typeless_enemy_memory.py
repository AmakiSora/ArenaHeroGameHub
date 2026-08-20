#!/usr/bin/env python3
"""One-off cleanup: drop type-less ("ENEMY") enemy memories from map_memory.json.

Runs INSIDE the app container (imports state_io from /app) so it shares the
same flock + atomic write discipline as tactic.py / dashboard.py. Bumps
enemy_clear_seq so the tactic process reloads the filtered list from disk
instead of writing its stale RAM copy back.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from state_io import atomic_write_text, file_lock

data_dir = os.environ.get("ARENA_DATA_DIR", "").strip() or "."
path = Path(data_dir) / "map_memory.json"

with file_lock(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    sightings = data.get("enemy_sightings", []) or []

    kept, dropped = [], []
    for item in sightings:
        etype = str(item[2]).upper() if isinstance(item, list) and len(item) >= 3 and item[2] else "ENEMY"
        (dropped if etype == "ENEMY" else kept).append(item)

    seq = int(data.get("enemy_clear_seq", 0) or 0) + 1
    data["enemy_sightings"] = kept
    data["enemy_sighting_count"] = len(kept)
    data["enemy_clear_seq"] = seq
    atomic_write_text(path, json.dumps(data, ensure_ascii=False))

print(f"dropped={len(dropped)} kept={len(kept)} new_clear_seq={seq}")
