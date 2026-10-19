"""Lossless on-disk encoding for the permanent obstacle map.

The arena map is large (760k+ wall cells). Storing one [x, y] pair per wall
made map_memory.json ~11 MB and every save re-serialized the whole thing.
Walls are stored here as horizontal runs per row, zlib-compressed and
base64-encoded into a single JSON string. Small maps keep the legacy plain
list so files stay readable (and old tests/fixtures keep working).

Readers accept both formats: packed wins when present, otherwise the legacy
``obstacles`` list is used.
"""

from __future__ import annotations

import base64
import json
import zlib
from collections.abc import Iterable

OBSTACLE_RLE_KEY = "obstacles_rle"
OBSTACLE_ENCODING_KEY = "obstacles_encoding"
OBSTACLE_ENCODING = "rows-rle+zlib+base64"
# Below this count the plain list is cheaper to write than a compressed blob
# and stays greppable. Production crossed this boundary long ago.
PLAIN_OBSTACLE_LIMIT = 5000
_COMPRESS_LEVEL = 3


def encode_obstacle_fields(obstacles: Iterable[tuple[int, int]]) -> dict:
    """Return the obstacle fields to put into map_memory.json."""
    if hasattr(obstacles, "__len__"):
        count = len(obstacles)  # type: ignore[arg-type]
    else:
        obstacles = list(obstacles)
        count = len(obstacles)
    if count <= PLAIN_OBSTACLE_LIMIT:
        return {"obstacles": [list(p) for p in sorted(obstacles)]}

    rows: dict[int, list[int]] = {}
    for x, y in obstacles:
        rows.setdefault(int(y), []).append(int(x))
    packed_rows: list[list[int]] = []
    for y in sorted(rows):
        xs = sorted(rows[y])
        row = [y]
        start = prev = xs[0]
        for x in xs[1:]:
            if x == prev + 1:
                prev = x
            else:
                row.extend((start, prev))
                start = prev = x
        row.extend((start, prev))
        packed_rows.append(row)
    raw = json.dumps(packed_rows, separators=(",", ":")).encode("utf-8")
    blob = base64.b64encode(
        zlib.compress(raw, _COMPRESS_LEVEL)
    ).decode("ascii")
    return {
        OBSTACLE_RLE_KEY: blob,
        OBSTACLE_ENCODING_KEY: OBSTACLE_ENCODING,
    }


def decode_obstacles(data: dict) -> set[tuple[int, int]]:
    """Decode obstacles from either the packed or the legacy plain format."""
    blob = data.get(OBSTACLE_RLE_KEY)
    if isinstance(blob, str) and blob:
        rows = json.loads(zlib.decompress(base64.b64decode(blob)))
        out: set[tuple[int, int]] = set()
        for row in rows:
            y = int(row[0])
            for i in range(1, len(row), 2):
                start = int(row[i])
                end = int(row[i + 1])
                out.update((x, y) for x in range(start, end + 1))
        return out

    out = set()
    for item in data.get("obstacles") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out.add((int(item[0]), int(item[1])))
    return out
