"""Arena Hero dashboard - pan/zoom SVG map + Chinese dark UI.
Run: python dashboard.py  -> http://localhost:4399
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from functools import wraps
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit

import game_stats
from tactic_config import (
    CONFIG_PATH,
    ConfigValidationError,
    config_schema,
    default_config,
    load_config,
    update_config,
)
from state_io import append_jsonl, atomic_write_text, file_lock

def _data_path(name: str) -> str:
    raw = os.environ.get("ARENA_DATA_DIR", "").strip()
    return str(Path(raw).resolve() / name) if raw else name


LOG_FILE = _data_path("tactic_log.jsonl")
MAP_FILE = _data_path("map_memory.json")
WAYPOINTS_FILE = _data_path("waypoints.json")
SELF_DESTRUCT_FILE = _data_path("self_destruct.json")
HOLDS_FILE = _data_path("holds.json")
BATTLE_LOG_FILE = _data_path("battle_log.jsonl")
HOST = "0.0.0.0"
PORT = 4399
# Auth gate: requests from outside must present this token (cookie / Bearer /
# ?token=). Empty => auth disabled (local dev without env). Loopback always
# bypasses so the Docker healthcheck and deploy smoke-tests keep working.
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "").strip()

# ---------- arena web console (React SPA under /arena) ---------------------
# The dashboard serves the built frontend from web/dist and reverse-proxies
# /api/v1/* to the game API, injecting the server-side API key. Browsers
# cannot attach Authorization headers to a WebSocket upgrade, so the WS
# handshake terminates here and only the upstream socket carries the key.
ARENA_API_HOST = os.environ.get("ARENA_API_HOST", "").strip() or "api.arenahero.io"
ARENA_API_KEY = os.environ.get("ARENA_HERO_API_KEY", "").strip()
WEB_DIST_DIR = Path(__file__).resolve().parent / "web" / "dist"

_ARENA_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}

# Upstream endpoints the browser may reach through the dashboard proxy.
_ARENA_GET_PATHS = {"/api/v1/leaderboard", "/api/v1/me", "/api/v1/me/stats"}
_ARENA_POST_PATHS = {"/api/v1/game/commands", "/api/v1/auth/login", "/api/v1/auth/logout"}
# Extra cookie the proxy uses to carry the upstream CSRF token alongside the
# imported session cookie; never forwarded as-is, never readable by JS.
_ARENA_CSRF_COOKIE = "arena_csrf"
_ARENA_WS_PATH = "/api/v1/game/ws"
_WS_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ---------- data loading --------------------------------------------------

def _iter_log_lines_reverse(path: str, chunk_size: int = 64 * 1024):
    """Yield non-empty log lines from newest to oldest without loading the file."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        position = f.tell()
        remainder = b""

        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            f.seek(position)
            parts = (f.read(read_size) + remainder).split(b"\n")
            remainder = parts[0]
            for raw_line in reversed(parts[1:]):
                raw_line = raw_line.rstrip(b"\r")
                if raw_line.strip():
                    yield raw_line.decode("utf-8", errors="replace")

        remainder = remainder.rstrip(b"\r")
        if remainder.strip():
            yield remainder.decode("utf-8", errors="replace")


def read_latest():
    if not os.path.exists(LOG_FILE):
        return None, time.time()
    history = read_history(1)
    return (history[0] if history else None), os.path.getmtime(LOG_FILE)


def _log_signature() -> tuple[int, int] | None:
    """(mtime_ns, size) signature for the tactic log, or None when unreadable.

    Same mtime+size heuristic as the tactic's file signatures: every appended
    tick bumps both, so an unchanged file is detected with a single stat().
    """
    try:
        stat = os.stat(LOG_FILE)
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _stream_signature() -> tuple:
    """Snapshot of every data file whose change should push a live refresh.

    The tactic appends one tick record per server tick, map memory is re-saved
    every ~10 ticks, and the battle log grows per game event — any of them
    moving is worth one SSE push. Same mtime+size heuristic as the other file
    signatures, so an idle dashboard costs a handful of stat() calls per scan.
    """
    def _sig(path: str):
        try:
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    return tuple(_sig(p) for p in (LOG_FILE, MAP_FILE, BATTLE_LOG_FILE))


# build_parts() runs on every /api/state poll (every 2s per client) and always
# asks for the newest 40 tick records. Between polls with no new tick the log
# file is unchanged, so re-reading + re-parsing those 40 (large) records is pure
# overhead. Cache the result by (log signature, ticks); a single appended tick
# bumps the signature and invalidates the entry.
_history_cache: dict[tuple[int, int, int], list[dict]] = {}


def read_history(ticks: int = 40):
    if ticks <= 0 or not os.path.exists(LOG_FILE):
        return []
    sig = _log_signature()
    key = (sig[0], sig[1], ticks) if sig is not None else None
    if key is not None:
        cached = _history_cache.get(key)
        if cached is not None:
            return [dict(rec) for rec in cached]
    out = []
    for line in _iter_log_lines_reverse(LOG_FILE):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and "tick" in rec and "plan_unit_actions" in rec:
            out.append(rec)
            if len(out) >= ticks:
                break
    if key is not None:
        # Keep only the newest entry so an old window or rotated log frees memory.
        _history_cache.clear()
        _history_cache[key] = [dict(rec) for rec in out]
    return out


# ---------- guerrilla route tracks ---------------------------------------
# The tactic bot appends one tiny row per guerrilla unit per Tick to
# runtime/guerrilla_routes-YYYYMMDD.jsonl (see GUERRILLA_ROUTE_PREFIX in
# tactic.py). tactic_log.jsonl holds ~890 Ticks before rotating (~4h at the
# live Tick cycle), so a whole day of exploration lines only exists in these
# files. One row: {"t": tick, "e": epoch, "u": "V1", "i": id8, "p": [x, y]}.

GUERRILLA_ROUTE_PREFIX = "guerrilla_routes-"
GUERRILLA_TRACK_MAX_POINTS = 800


def _guerrilla_route_files() -> list[tuple[str, str]]:
    """(date, path) for every route file, newest date first."""
    directory = Path(_data_path(".")).resolve()
    out: list[tuple[str, str]] = []
    try:
        names = sorted(directory.glob(f"{GUERRILLA_ROUTE_PREFIX}*.jsonl"), reverse=True)
    except OSError:
        return []
    for path in names:
        stamp = path.name[len(GUERRILLA_ROUTE_PREFIX):-len(".jsonl")]
        if len(stamp) == 8 and stamp.isdigit():
            out.append((stamp, str(path)))
    return out


def _guerrilla_route_signature(path: str) -> tuple[int, int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


# Keyed by (path, max_points, since_epoch) -> (signature, built_at, payload).
# The route file gains rows every Tick, so a signature-only cache would
# re-parse several MB on every poll; the tracks layer is a review tool, so one
# refresh per minute is plenty and keeps this process off the tactic bot's CPU.
_guerrilla_track_cache: dict[tuple, tuple[tuple[int, int] | None, float, dict]] = {}
GUERRILLA_TRACK_REFRESH_SECONDS = 60.0


def read_guerrilla_tracks(
    date: str | None = None,
    max_points: int = GUERRILLA_TRACK_MAX_POINTS,
    hours: int | None = None,
) -> dict:
    """Aggregate one day's route rows into a polyline per guerrilla unit.

    Consecutive duplicate cells are folded first (a unit that WAITs or is
    refused its MOVE still writes a row every Tick), then the remaining walk is
    strided down to ``max_points`` with the newest cell always kept.
    """
    files = _guerrilla_route_files()
    dates = [stamp for stamp, _ in files]
    if not files:
        return {"ok": True, "date": None, "dates": [], "units": []}
    if date is None or date not in dates:
        date, path = files[0]
    else:
        path = next(p for stamp, p in files if stamp == date)
    since_epoch = None
    if hours and hours > 0:
        since_epoch = int(time.time()) - hours * 3600
    signature = _guerrilla_route_signature(path)
    key = (path, int(max_points), since_epoch)
    cached = _guerrilla_track_cache.get(key)
    if cached is not None:
        cached_sig, built_at, payload = cached
        throttled = time.monotonic() - built_at < GUERRILLA_TRACK_REFRESH_SECONDS
        if throttled or cached_sig == signature:
            return {**payload, "dates": dates}

    tracks: dict[str, dict] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        pos = row.get("p")
        uid = str(row.get("i") or row.get("u") or "")
        if not uid or not isinstance(pos, list) or len(pos) != 2:
            continue
        if since_epoch is not None and int(row.get("e") or 0) < since_epoch:
            continue
        track = tracks.setdefault(uid, {
            "id": uid,
            "name": str(row.get("u") or uid),
            "type": "RANGER" if str(row.get("u") or "").upper().startswith("R") else "VANGUARD",
            "cells": [],
            "distinct": set(),
            "ticks": 0,
            "first_tick": row.get("t"),
            "last_tick": row.get("t"),
            "first_epoch": row.get("e"),
            "last_epoch": row.get("e"),
        })
        cell = (int(pos[0]), int(pos[1]))
        track["ticks"] += 1
        track["last_tick"] = row.get("t")
        track["last_epoch"] = row.get("e")
        track["distinct"].add(cell)
        if not track["cells"] or track["cells"][-1] != cell:
            track["cells"].append(cell)

    units = []
    for track in tracks.values():
        cells = track["cells"]
        stride = max(1, -(-len(cells) // max(1, int(max_points))))
        kept = cells[::stride]
        if not kept or kept[-1] != cells[-1]:
            kept.append(cells[-1])
        # Distinct cells / net drift are measured over EVERY row, not over the
        # decimated polyline: "400 Ticks inside 6 cells" is the whole point of
        # the recorder, and striding would undercount it.
        distinct = track["distinct"]
        xs = [cell[0] for cell in distinct]
        ys = [cell[1] for cell in distinct]
        start, end = cells[0], cells[-1]
        units.append({
            "id": track["id"],
            "name": track["name"],
            "type": track["type"],
            "points": [[x, y] for x, y in kept],
            "ticks": track["ticks"],
            "distinct": len(distinct),
            "net": max(abs(end[0] - start[0]), abs(end[1] - start[1])),
            "bounds": [min(xs), min(ys), max(xs), max(ys)],
            "first_tick": track["first_tick"],
            "last_tick": track["last_tick"],
            "first_epoch": track["first_epoch"],
            "last_epoch": track["last_epoch"],
        })
    units.sort(key=lambda item: (-item["ticks"], item["name"]))
    payload = {
        "ok": True,
        "date": date,
        "max_points": int(max_points),
        "hours": hours,
        "units": units,
    }
    _guerrilla_track_cache.clear()
    _guerrilla_track_cache[key] = (signature, time.monotonic(), payload)
    return {**payload, "dates": dates}


def _parse_iso_ts(value) -> float | None:
    """Parse a tactic-log ``timestamp`` (ISO string) to wall-clock epoch seconds.

    Returns None when absent or unparseable so a record can't be placed on the
    time axis.  Handles both the tactic's ``+00:00`` suffix and a trailing ``Z``;
    a bare datetime is treated as UTC.
    """
    if not isinstance(value, str) or not value:
        return None
    s = value
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _read_tick_records_since(cutoff: float | None, max_records: int = 30000) -> list[dict]:
    """Return newest-first tick records with parsed wall-clock ``_ts``.

    The tactic log is time-ordered, so reverse iteration can stop at the first
    record older than ``cutoff`` (records after it are older still) — a time
    window reads only as many lines as it needs.  ``max_records`` bounds the
    read for very sparse logs.  Records without a parseable timestamp are
    skipped without stopping the scan.
    """
    out = []
    for line in _iter_log_lines_reverse(LOG_FILE):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not (isinstance(rec, dict) and "tick" in rec and "plan_unit_actions" in rec):
            continue
        ts = _parse_iso_ts(rec.get("timestamp"))
        rec["_ts"] = ts
        if ts is None:
            continue
        if cutoff is not None and ts < cutoff:
            break
        out.append(rec)
        if len(out) >= max_records:
            break
    return out


def _trend_points(records: list[dict], max_points: int = 240) -> list[dict]:
    """Flatten newest-first tick records into a chronological compact series.

    Each point carries the metrics the trend charts plot.  ``t`` is the wall-clock
    epoch of the record so the charts lay out by real time; records without a
    parseable timestamp are dropped.  Missing metric fields are treated as 0 so
    both old (v2) and current (v3) log lines chart correctly.  Long windows are
    downsampled by a stride so the JSON payload stays small.
    """
    records = [rec for rec in records if rec.get("_ts") is not None]
    if not records:
        return []
    if len(records) > max_points:
        stride = (len(records) + max_points - 1) // max_points
        records = records[::stride]
    points = []
    for rec in reversed(records):
        points.append({
            "t": rec["_ts"],
            "r": int(rec.get("resources") or 0),
            "c": int(rec.get("resource_capacity") or 0),
            "w": len(rec.get("workers") or []),
            "v": len(rec.get("vanguards") or []),
            "g": len(rec.get("rangers") or []),
            "e": int(rec.get("visible_enemies") or 0),
        })
    return points


# /api/trends is polled every 2 s; re-parsing full tick records per poll is pure
# overhead while the log file has not grown. Cache the compact series by
# (log mtime+size, window-seconds) — same heuristic as the tactic's file
# signatures; every appended tick bumps mtime and invalidates the entry.
_trends_cache: dict[tuple[int, int, int], list[dict]] = {}


def _trends_points(window_seconds: int) -> list[dict]:
    try:
        stat = os.stat(LOG_FILE)
    except OSError:
        return []
    key = (stat.st_mtime_ns, stat.st_size, window_seconds)
    cached = _trends_cache.get(key)
    if cached is not None:
        return cached
    cutoff = time.time() - window_seconds
    points = _trend_points(_read_tick_records_since(cutoff))
    # Keep only the newest entry so an old window or rotated log frees memory.
    _trends_cache.clear()
    _trends_cache[key] = points
    return points


# ── Categorized battle log (「战斗日志」panel below the config) ──────────────
# The tactic process appends discovery/combat/economy/failure rows each tick;
# the dashboard process appends config-change rows on save.  Filter chips in
# the panel map to the entry "cat" field.

# Log-category chips shown in the panel header (label + default visibility).
LOG_CATEGORIES = (
    ("discover", "发现", True),
    ("kill", "击杀", True),
    ("defeat", "被击败", True),
    ("combat", "战斗", False),
    ("economy", "经济", False),
    ("config", "配置", True),
    ("warn", "异常", True),
)

_CONFIG_FIELD_LABELS = {f["key"]: f["label"] for f in config_schema()["fields"]}


def read_battle_log(n: int = 200) -> list[dict]:
    """Return up to ``n`` newest battle-log entries (newest first)."""
    if n <= 0 or not os.path.exists(BATTLE_LOG_FILE):
        return []
    out = []
    for line in _iter_log_lines_reverse(BATTLE_LOG_FILE):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("msg"):
            out.append(rec)
            if len(out) >= n:
                break
    return out


def battle_log_json_entries(limit: int = 200) -> list[dict]:
    """Normalized tick/ts/cat/msg rows for the arena SPA's battle-log panel.

    The server-rendered dashboard gets HTML rows from /api/log; the arena
    page under /arena renders its own panel and needs plain JSON. Only the
    four fields the panel uses are sent, so raw rows never leak extra keys.
    """
    return [
        {
            "tick": entry.get("tick"),
            "ts": entry.get("ts"),
            "cat": str(entry.get("cat", "")),
            "msg": str(entry.get("msg", "")),
        }
        for entry in read_battle_log(limit)
    ]


def _config_log_message(updates: dict) -> str:
    """Human-readable summary of which config keys changed, newest-named."""
    if not updates:
        return "配置保存（无变化）"
    parts = []
    for key in sorted(updates):
        label = _CONFIG_FIELD_LABELS.get(key, key)
        parts.append(f"{label}={updates[key]}")
    return "配置调整： " + "，".join(parts)


def append_config_log(updates: dict, *, action: str = "") -> None:
    """Record a dashboard-initiated config/team change into the battle log."""
    msg = _config_log_message(updates)
    if action:
        msg = f"{action}：{msg}"
    try:
        append_jsonl(BATTLE_LOG_FILE, [{"tick": None, "ts": time.time(), "cat": "config", "msg": msg}])
    except OSError:
        pass


def _battle_log_html(limit: int = 200) -> tuple[str, int]:
    """Render the newest battle-log rows for the panel.

    ``limit`` bounds how many rows the server sends; the client asks for more
    when a larger time window (or 'all') is selected so the "全部" filter can
    actually cover the full retained log instead of a fixed newest-200.
    """
    entries = read_battle_log(limit)
    if not entries:
        return '<div class="muted">暂无日志</div>', 0
    rows = []
    for e in entries:
        tick = e.get("tick")
        ts = e.get("ts")
        label_parts = []
        if ts:
            label_parts.append(time.strftime("%H:%M:%S", time.localtime(float(ts))))
        if tick is not None:
            label_parts.append(f"tick {tick}")
        tick_label = " · ".join(label_parts)
        ts_attr = f' data-ts="{ts}"' if ts is not None else ""
        msg = html.escape(str(e.get("msg", "")))
        rows.append(
            f'<div class="log-row" data-cat="{html.escape(str(e.get("cat", "")))}"{ts_attr}>'
            f'<span class="log-tick">{html.escape(tick_label)}</span>'
            f'<span class="log-msg">{_log_coord_spans(msg)}</span></div>'
        )
    return "".join(rows), len(entries)


# 消息里的 (x,y) 坐标：tactic 的发现日志与战斗事件消息都以 "(x,y)" 或
# "(x1,y1)→(x2,y2)" 结尾（见 tactic._fmt_cell），这里把每个坐标包成可点击
# 跳转的 span。正则跑在 html.escape 之后的字符串上，数字/括号/逗号不受转义
# 影响，所以直接匹配即可。
_LOG_COORD_RE = re.compile(r"\((-?\d+)\s*,\s*(-?\d+)\)")


def _log_coord_spans(msg: str) -> str:
    """把已转义消息里的 (x,y) 坐标包成带 data-focus 属性的可点击 span。

    前端对带 data-focus-wx/wy 的元素做委托点击，调用 focusWorld 把地图视角
    重置到该坐标（与右侧单位卡片跳转同一机制），这里只需输出属性无需额外 JS。
    无坐标的消息原样返回。
    """
    return _LOG_COORD_RE.sub(
        lambda m: (
            f'<span class="log-coord" data-focus-wx="{m.group(1)}" '
            f'data-focus-wy="{m.group(2)}" title="点击定位到地图">'
            f"({m.group(1)},{m.group(2)})</span>"
        ),
        msg,
    )


# ── 战斗热点（哪里在打仗） ──────────────────────────────────────────────
# 把"哪里发生战斗"聚合成可点击热点：地图上方告警条 + 地图脉冲光圈。
# 三级危险度：engaged（可见敌人贴着我方单位）、recent（窗口内战斗日志
# 行，坐标取结算目标格）、sighted（可见但未接战）。
_HOTSPOT_ENGAGE_RADIUS = 2      # 敌我 Chebyshev 距离 ≤ 该值视为交战中
_HOTSPOT_RECENT_WINDOW = 180.0  # 近期交火窗口（秒）
_HOTSPOT_LEVEL_ORDER = {"engaged": 0, "recent": 1, "sighted": 2}


def _recent_combat_cells(window: float = _HOTSPOT_RECENT_WINDOW) -> dict:
    """Collect {cell: {ts, events}} from combat/kill/defeat rows in the window.

    Scans the battle log newest-first and stops at the first row older than
    the window (the file is time-ordered), so an idle stretch costs only the
    newest few lines. Coordinates come from the trailing "(x,y)" /
    "(ax,ay)→(tx,ty)" suffix the tactic appends; an arrow resolves to the
    target cell (the last coordinate), i.e. where the hit landed.
    """
    if not os.path.exists(BATTLE_LOG_FILE):
        return {}
    cutoff = time.time() - window
    out: dict = {}
    for line in _iter_log_lines_reverse(BATTLE_LOG_FILE):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        try:
            ts = float(row.get("ts"))
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            break
        if row.get("cat") not in ("combat", "kill", "defeat"):
            continue
        coords = _LOG_COORD_RE.findall(str(row.get("msg", "")))
        if not coords:
            continue
        cell = (int(coords[-1][0]), int(coords[-1][1]))
        info = out.get(cell)
        if info is None:
            out[cell] = {"ts": ts, "events": 1}
        else:
            info["events"] += 1
            info["ts"] = max(info["ts"], ts)
    return out


def _combat_hotspots(rec: dict) -> list:
    """Aggregate combat locations into sorted hotspot dicts.

    Order: engaged > recent > sighted, then newest / most enemies first.
    Each entry: {x, y, level, enemies, friendlies, events, last_ts, age_s}.
    """
    friendlies: list = []
    core = rec.get("core_pos")
    if core and len(core) == 2:
        friendlies.append((int(core[0]), int(core[1])))
    for group in ("workers", "vanguards", "rangers"):
        for u in rec.get(group, []) or []:
            p = u.get("pos") or []
            if len(p) == 2:
                friendlies.append((int(p[0]), int(p[1])))

    hotspots: dict = {}

    def spot(x: int, y: int, level: str) -> dict:
        s = hotspots.get((x, y))
        if s is None:
            s = {"x": x, "y": y, "level": level, "enemies": 0,
                 "friendlies": 0, "events": 0, "last_ts": None}
            hotspots[(x, y)] = s
        elif _HOTSPOT_LEVEL_ORDER[level] < _HOTSPOT_LEVEL_ORDER[s["level"]]:
            s["level"] = level  # a live fight outranks a stale event cell
        return s

    for enemy in rec.get("enemies", []) or []:
        p = enemy.get("pos") or []
        if len(p) != 2:
            continue
        x, y = int(p[0]), int(p[1])
        near = sum(
            1 for fx, fy in friendlies
            if max(abs(fx - x), abs(fy - y)) <= _HOTSPOT_ENGAGE_RADIUS
        )
        s = spot(x, y, "engaged" if near else "sighted")
        s["enemies"] += 1
        s["friendlies"] += near

    for (x, y), info in _recent_combat_cells().items():
        s = spot(x, y, "recent")
        s["events"] += info["events"]
        s["last_ts"] = info["ts"] if s["last_ts"] is None else max(s["last_ts"], info["ts"])

    out = list(hotspots.values())
    now = time.time()
    for s in out:
        s["age_s"] = (now - s["last_ts"]) if s["last_ts"] is not None else None
    out.sort(key=lambda s: (
        _HOTSPOT_LEVEL_ORDER[s["level"]],
        -(s["last_ts"] or 0.0),
        -s["enemies"],
    ))
    return out


def _hotspots_html(hotspots: list, limit: int = 12) -> str:
    """Render the hotspot strip above the map; chips jump the map on click.

    Chips carry data-focus-wx/wy, so the existing document-level delegation
    (focusWorld) handles the jump — no extra JS needed.
    """
    if not hotspots:
        return '<span class="muted">当前无战斗 · 视野内无敌人</span>'
    chips = []
    for s in hotspots[:limit]:
        level = s["level"]
        coord = f"({s['x']},{s['y']})"
        if level == "engaged":
            label = f"⚔ 交战 {coord} · 敌{s['enemies']}"
            if s["friendlies"]:
                label += f" 我{s['friendlies']}"
        elif level == "recent":
            age = int(s.get("age_s") or 0)
            ago = f"{age}秒前" if age < 60 else f"{age // 60}分钟前"
            label = f"✦ {ago}交火 {coord}"
        else:
            label = f"👁 敌人 {coord}"
            if s["enemies"] > 1:
                label += f" ×{s['enemies']}"
        chips.append(
            f'<button type="button" class="hotspot {level}" data-focus-wx="{s["x"]}" '
            f'data-focus-wy="{s["y"]}" title="点击定位到地图">{html.escape(label)}</button>'
        )
    extra = len(hotspots) - len(chips)
    tail = f'<span class="hotspot-more">+{extra} 处</span>' if extra > 0 else ""
    return "".join(chips) + tail


def _clamp_log_limit(request) -> int:
    """Parse the ``?log=`` / ``?limit=`` query into a bounded row count.

    The client scales how many battle-log rows it wants by the selected time
    window; the cap keeps a single poll from serializing the whole file (which
    can hold tens of thousands of lines before the 2 MB trim kicks in).
    """
    try:
        qs = parse_qs(urlsplit(request.path).query)
        n = int((qs.get("log") or qs.get("limit") or ["200"])[0])
    except (TypeError, ValueError):
        return 200
    return max(200, min(n, 8000))


def _read_map_file() -> dict:
    """Load raw map_memory.json, or an empty structure when missing/invalid."""
    empty = {
        "obstacles": [],
        "resources": [],
        "manual_resources": [],
        "forgotten_resources": [],
        "enemy_sightings": [],
        "obstacle_count": 0,
        "resource_count": 0,
        "manual_count": 0,
        "enemy_sighting_count": 0,
        "enemy_clear_seq": 0,
    }
    if not os.path.exists(MAP_FILE):
        return empty
    try:
        with open(MAP_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            empty.update(loaded)
        return empty
    except Exception:
        return empty


def _map_mutation(func):
    @wraps(func)
    def locked(*args, **kwargs):
        with file_lock(MAP_FILE):
            return func(*args, **kwargs)

    return locked


def _write_map_file(payload: dict) -> None:
    """Atomically replace map_memory.json."""
    atomic_write_text(MAP_FILE, json.dumps(payload, ensure_ascii=False))


def _coord_set(raw) -> set[tuple[int, int]]:
    out: set[tuple[int, int]] = set()
    for item in raw or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out.add((int(item[0]), int(item[1])))
    return out


# ── remembered enemy sightings (last-known positions + unit type) ───────────

# Color + short label per enemy unit type, matching the live-enemy map colors.
_ENEMY_TYPE_STYLE = {
    "WORKER": ("#8aa4ff", "工"),
    "VANGUARD": ("#ff8c42", "先"),
    "RANGER": ("#b38cff", "游"),
    # A star stays legible at the map's tiny label size and cannot be confused
    # with the similarly dense "敌" glyph used by legacy/unknown sightings.
    "CORE": ("#ff4964", "★"),
}


def _enemy_type_char(etype: str) -> str:
    """Short map label for an enemy type (★/工/先/游, default 敌)."""
    return _ENEMY_TYPE_STYLE.get(str(etype or "").upper(), ("#ff6464", "敌"))[1]


def _enemy_type_color(etype: str) -> str:
    return _ENEMY_TYPE_STYLE.get(str(etype or "").upper(), ("#ff6464", "敌"))[0]


def _parse_enemy_sighting(item) -> tuple[tuple[int, int], str, int] | None:
    """Normalize one enemy-sighting entry to ((x, y), type, last-seen tick).

    Accepts the raw on-disk forms ``[x, y]``, ``[x, y, "CORE"]`` and
    ``[x, y, "CORE", tick]`` as well as the parsed dicts returned by
    load_map_memory. Unknown/legacy entries default to type "ENEMY" and
    missing ticks to 0 (oldest).
    """
    if isinstance(item, dict):
        pos = item.get("pos")
        if not pos or len(pos) != 2:
            return None
        try:
            tick = int(item.get("tick") or 0)
        except (TypeError, ValueError):
            tick = 0
        return (int(pos[0]), int(pos[1])), str(item.get("type") or "ENEMY").upper(), tick
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        etype = str(item[2]).upper() if len(item) >= 3 and item[2] else "ENEMY"
        try:
            tick = int(item[3]) if len(item) >= 4 else 0
        except (TypeError, ValueError):
            tick = 0
        return (int(item[0]), int(item[1])), etype, tick
    return None


# The arena's top-left strip caps its memory chips at 21 per enabled
# filter combination, so the API must hand each type enough candidates:
# cap per type (most recent first) instead of one global cutoff, which
# let newer unit sightings push every older CORE entry out of the list.
ENEMY_MEMORY_PER_TYPE_CAP = 21


def _rank_enemy_sightings(parsed) -> list:
    """Rank parsed sightings by last-seen tick (newest first), capped per type.

    Every type keeps its ENEMY_MEMORY_PER_TYPE_CAP most recent entries, so a
    filter combination on the arena page can always fill its own 21 slots
    even when another type has far fresher sightings.
    """
    ranked = sorted(parsed, key=lambda s: s[2], reverse=True)
    counts: dict[str, int] = {}
    selected = []
    for pos, etype, tick in ranked:
        if counts.get(etype, 0) >= ENEMY_MEMORY_PER_TYPE_CAP:
            continue
        counts[etype] = counts.get(etype, 0) + 1
        selected.append((pos, etype, tick))
    return selected


# load_map_memory() parses the full map_memory.json on every poll. The file only
# changes when the tactic saves new discoveries or the dashboard edits it, so an
# unchanged file (single stat()) must not cost a full re-parse. Cache by file
# signature; mutations below bump the mtime and invalidate the entry.
_map_memory_cache: dict[tuple[int, int], dict] = {}


def _map_file_signature() -> tuple[int, int] | None:
    try:
        stat = os.stat(MAP_FILE)
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def load_map_memory():
    sig = _map_file_signature()
    if sig is not None:
        cached = _map_memory_cache.get(sig)
        if cached is not None:
            return cached
    d = _read_map_file()
    forgotten = _coord_set(d.get("forgotten_resources"))
    resources = _coord_set(d.get("resources")) - forgotten
    manual = _coord_set(d.get("manual_resources")) - forgotten
    all_res = sorted(resources | manual)
    enemy_sightings = [
        s for s in (_parse_enemy_sighting(item) for item in d.get("enemy_sightings", []) or [])
        if s is not None
    ]
    enemy_sightings.sort(key=lambda s: s[0])
    result = {
        "obstacles": sorted(_coord_set(d.get("obstacles"))),
        "resources": all_res,
        "manual_resources": sorted(manual),
        "forgotten_resources": sorted(forgotten),
        "enemy_sightings": [
            {"pos": pos, "type": etype, "tick": tick}
            for pos, etype, tick in enemy_sightings
        ],
        "obstacle_count": d.get("obstacle_count", len(d.get("obstacles", []) or [])),
        "resource_count": len(all_res),
        "manual_count": len(manual),
        "enemy_clear_seq": int(d.get("enemy_clear_seq", 0) or 0),
        "updated_tick": d.get("updated_tick"),
    }
    if sig is not None:
        # Keep only the newest entry so a rewritten map frees the stale parse.
        _map_memory_cache.clear()
        _map_memory_cache[sig] = result
    return result


@_map_mutation
def save_manual_resource(x: int, y: int) -> dict:
    """Add a manually entered resource into map_memory.json."""
    data = _read_map_file()
    pos = (int(x), int(y))
    resources = _coord_set(data.get("resources"))
    manual = _coord_set(data.get("manual_resources"))
    forgotten = _coord_set(data.get("forgotten_resources"))
    resources.add(pos)
    manual.add(pos)
    # Re-adding from the dashboard revives a previously cleared coordinate.
    forgotten.discard(pos)

    payload = {
        "updated_tick": data.get("updated_tick"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "obstacles": data.get("obstacles", []),
        "resources": [list(p) for p in sorted(resources)],
        "manual_resources": [list(p) for p in sorted(manual)],
        "forgotten_resources": [list(p) for p in sorted(forgotten)],
        "enemy_sightings": data.get("enemy_sightings", []),
        "obstacle_count": data.get("obstacle_count", len(data.get("obstacles", []) or [])),
        "resource_count": len(resources),
        "manual_count": len(manual),
        "enemy_sighting_count": data.get(
            "enemy_sighting_count", len(data.get("enemy_sightings", []) or [])
        ),
        "enemy_clear_seq": int(data.get("enemy_clear_seq", 0) or 0),
        "source": "dashboard-manual",
    }
    _write_map_file(payload)
    return {
        "ok": True,
        "pos": [pos[0], pos[1]],
        "resource_count": len(resources),
        "manual_count": len(manual),
    }


@_map_mutation
def remove_manual_resource(x: int, y: int) -> dict:
    """Forget one remembered resource and sticky-tombstone it for the tactic process."""
    data = _read_map_file()
    pos = (int(x), int(y))
    resources = _coord_set(data.get("resources"))
    manual = _coord_set(data.get("manual_resources"))
    forgotten = _coord_set(data.get("forgotten_resources"))
    resources.discard(pos)
    manual.discard(pos)
    forgotten.add(pos)
    payload = {
        "updated_tick": data.get("updated_tick"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "obstacles": data.get("obstacles", []),
        "resources": [list(p) for p in sorted(resources)],
        "manual_resources": [list(p) for p in sorted(manual)],
        "forgotten_resources": [list(p) for p in sorted(forgotten)],
        "enemy_sightings": data.get("enemy_sightings", []),
        "obstacle_count": data.get("obstacle_count", len(data.get("obstacles", []) or [])),
        "resource_count": len(resources),
        "manual_count": len(manual),
        "enemy_sighting_count": data.get(
            "enemy_sighting_count", len(data.get("enemy_sightings", []) or [])
        ),
        "enemy_clear_seq": int(data.get("enemy_clear_seq", 0) or 0),
        "source": "dashboard-manual",
    }
    _write_map_file(payload)
    return {
        "ok": True,
        "pos": [pos[0], pos[1]],
        "resource_count": len(resources),
        "manual_count": len(manual),
        "forgotten_count": len(forgotten),
    }


@_map_mutation
def clear_remembered_resources() -> dict:
    """Clear all auto + manual resource memory and tombstone every former point."""
    data = _read_map_file()
    resources = _coord_set(data.get("resources"))
    manual = _coord_set(data.get("manual_resources"))
    forgotten = _coord_set(data.get("forgotten_resources"))
    forgotten |= resources | manual
    payload = {
        "updated_tick": data.get("updated_tick"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "obstacles": data.get("obstacles", []),
        "resources": [],
        "manual_resources": [],
        "forgotten_resources": [list(p) for p in sorted(forgotten)],
        "enemy_sightings": data.get("enemy_sightings", []),
        "obstacle_count": data.get("obstacle_count", len(data.get("obstacles", []) or [])),
        "resource_count": 0,
        "manual_count": 0,
        "enemy_sighting_count": data.get(
            "enemy_sighting_count", len(data.get("enemy_sightings", []) or [])
        ),
        "enemy_clear_seq": int(data.get("enemy_clear_seq", 0) or 0),
        "source": "dashboard-clear-resources",
    }
    _write_map_file(payload)
    return {
        "ok": True,
        "cleared": True,
        "resource_count": 0,
        "forgotten_count": len(forgotten),
    }


@_map_mutation
def clear_enemy_sightings() -> dict:
    """Clear enemy sightings and bump a seq so the tactic process drops its RAM copy."""
    data = _read_map_file()
    seq = int(data.get("enemy_clear_seq", 0) or 0) + 1
    payload = {
        "updated_tick": data.get("updated_tick"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "obstacles": data.get("obstacles", []),
        "resources": data.get("resources", []),
        "manual_resources": data.get("manual_resources", []),
        "forgotten_resources": data.get("forgotten_resources", []),
        "enemy_sightings": [],
        "obstacle_count": data.get("obstacle_count", len(data.get("obstacles", []) or [])),
        "resource_count": data.get("resource_count", len(data.get("resources", []) or [])),
        "manual_count": data.get("manual_count", len(data.get("manual_resources", []) or [])),
        "enemy_sighting_count": 0,
        "enemy_clear_seq": seq,
        "source": "dashboard-clear-enemies",
    }
    _write_map_file(payload)
    return {"ok": True, "cleared": True, "enemy_clear_seq": seq}


# ── manual per-unit waypoints (⌖ map pick) ──────────────────────────────────

# SVG render cache for build_parts: keyed on everything that can change the
# map — the newest tick record, the map file, the waypoints file, and the
# config markers (attack/core target) drawn onto the map. A hit skips the
# full obstacle/route re-render; any change bumps a signature and re-renders.
_svg_cache: dict[tuple, str] = {}


def _file_sig(path: str) -> tuple[int, int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _render_svg_cached(rec: dict, mm: dict, waypoints: dict,
                       hotspots: list | None = None) -> str:
    config = load_config(CONFIG_PATH)
    marker_sig = (
        str(config.get("attack_mode", "coords")),
        int(config.get("attack_target_x", 0)),
        int(config.get("attack_target_y", 0)),
        str(config.get("kite_mode", "coords")),
        int(config.get("kite_target_x", 0)),
        int(config.get("kite_target_y", 0)),
        bool(config.get("core_target_enabled", False)),
        int(config.get("core_target_x", 0)),
        int(config.get("core_target_y", 0)),
    )
    # The combat rings change with the battle log even between ticks, so the
    # hotspot set joins the cache signature.
    hotspot_sig = tuple(
        (s["x"], s["y"], s["level"]) for s in (hotspots or [])
    )
    key = (
        rec.get("tick"),
        _file_sig(MAP_FILE),
        _file_sig(WAYPOINTS_FILE),
        marker_sig,
        hotspot_sig,
    )
    cached = _svg_cache.get(key)
    if cached is not None:
        return cached
    svg = render_svg(rec, mm, config=config, waypoints=waypoints, hotspots=hotspots)
    # Keep only the newest entry so a moved map frees the stale render.
    _svg_cache.clear()
    _svg_cache[key] = svg
    return svg


def _read_waypoints_file() -> dict:
    try:
        with open(WAYPOINTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _waypoint_mutation(func):
    @wraps(func)
    def locked(*args, **kwargs):
        with file_lock(WAYPOINTS_FILE):
            return func(*args, **kwargs)

    return locked


def _write_waypoints_file(payload: dict) -> None:
    atomic_write_text(WAYPOINTS_FILE, json.dumps(payload, ensure_ascii=False))


_WAYPOINT_NAME_RE = re.compile(r"^[WVR][1-9][0-9]*$")


def _waypoint_name(raw: object) -> str:
    name = str(raw or "").strip().upper()
    if not _WAYPOINT_NAME_RE.fullmatch(name):
        raise ValueError("name 必须是 W/V/R 加正整数，例如 W1")
    return name


def _normalize_wp_entry(raw: object) -> dict | None:
    """Normalize a waypoints.json value to {"queue": [[x,y],...], "mode": str}.

    Accepts both the new queue format and the legacy single-target [x, y] — a
    legacy target becomes a one-stop walk-only (rush) queue, matching the
    pre-queue "just march" behavior.
    """
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            return {"queue": [[int(raw[0]), int(raw[1])]], "mode": "rush"}
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    queue_raw = raw.get("queue")
    queue: list[list[int]] = []
    if isinstance(queue_raw, (list, tuple)):
        for pos in queue_raw:
            if not isinstance(pos, (list, tuple)) or len(pos) < 2:
                continue
            try:
                queue.append([int(pos[0]), int(pos[1])])
            except (TypeError, ValueError):
                continue
    mode = str(raw.get("mode") or "attack")
    if mode not in ("attack", "rush"):
        mode = "attack"
    if not queue:
        return None
    return {"queue": queue, "mode": mode}


def _wp_mode(raw: object) -> str:
    mode = str(raw or "attack").strip().lower()
    return mode if mode in ("attack", "rush") else "attack"


def load_waypoints() -> dict[str, dict]:
    """Load manual per-unit target queues as {name: {"queue": [...], "mode": str}}."""
    data = _read_waypoints_file()
    targets = data.get("targets") if isinstance(data, dict) else None
    out: dict[str, dict] = {}
    if isinstance(targets, dict):
        for name, raw in targets.items():
            entry = _normalize_wp_entry(raw)
            if entry is None:
                continue
            try:
                out[_waypoint_name(name)] = entry
            except (TypeError, ValueError):
                continue
    return out


def _wp_count(waypoints: dict) -> int:
    """Total queued targets across every unit."""
    total = 0
    for raw in waypoints.values():
        entry = _normalize_wp_entry(raw)
        if entry:
            total += len(entry["queue"])
    return total


@_waypoint_mutation
def set_waypoint(name: str, x: int, y: int, mode: str = "attack") -> dict:
    """Append a target to one unit's queue and set its march mode."""
    name = _waypoint_name(name)
    mode = _wp_mode(mode)
    targets = load_waypoints()
    entry = targets.get(name)
    queue = (entry["queue"] if entry else []) + [[int(x), int(y)]]
    targets[name] = {"queue": queue, "mode": mode}
    _write_waypoints_file({
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "targets": targets,
    })
    return {
        "ok": True,
        "name": name,
        "pos": [int(x), int(y)],
        "queue": [list(p) for p in queue],
        "mode": mode,
        "waypoint_count": len(targets),
    }


@_waypoint_mutation
def set_waypoint_mode(name: str, mode: str) -> dict:
    """Switch one unit's queue march mode (attack / rush)."""
    name = _waypoint_name(name)
    mode = _wp_mode(mode)
    targets = load_waypoints()
    entry = targets.get(name)
    if entry is None:
        return {"ok": False, "error": "目标不存在"}
    entry = dict(entry)
    entry["mode"] = mode
    targets[name] = entry
    _write_waypoints_file({
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "targets": targets,
    })
    return {"ok": True, "name": name, "mode": mode, "waypoint_count": len(targets)}


@_waypoint_mutation
def remove_waypoint(name: str, index: int | None = None) -> dict:
    """Remove one unit's whole queue (index=None) or a single queued target."""
    name = _waypoint_name(name)
    targets = load_waypoints()
    entry = targets.get(name)
    if entry is None:
        return {"ok": False, "error": "目标不存在"}
    if index is None:
        del targets[name]
    else:
        queue = list(entry["queue"])
        if not 0 <= index < len(queue):
            return {"ok": False, "error": "目标不存在"}
        queue.pop(index)
        if queue:
            targets[name] = dict(entry, queue=queue)
        else:
            del targets[name]
    _write_waypoints_file({
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "targets": targets,
    })
    return {"ok": True, "name": name, "index": index, "waypoint_count": len(targets)}


@_waypoint_mutation
def clear_waypoints() -> dict:
    _write_waypoints_file({
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "targets": {},
    })
    return {"ok": True, "cleared": True, "waypoint_count": 0}


# ── squad attack-target queues (进攻队/风筝队 目标队列) ──────────────────
# Ordered coordinate queues per squad in squad_targets.json, consumed by the
# tactic in coords mode: the head pops once the squad arrived and the area is
# cleared. The dashboard only appends/removes; both sides lock the file, so
# operator edits and the tactic's pops never clobber each other.
SQUAD_TARGETS_FILE = _data_path("squad_targets.json")
_SQUAD_KEYS = ("attack", "kite")
_SQUAD_TARGET_LIMIT = 20
_SQUAD_TARGET_BOUND = 10000
_SQUAD_LABELS = {"attack": "进攻队", "kite": "风筝队"}


def _squad_key(raw: object) -> str:
    squad = str(raw or "").strip().lower()
    if squad not in _SQUAD_KEYS:
        raise ValueError("分队不存在")
    return squad


def _normalize_squad_queue(raw: object) -> list[list[int]]:
    queue: list[list[int]] = []
    if isinstance(raw, (list, tuple)):
        for point in raw:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            try:
                queue.append([int(point[0]), int(point[1])])
            except (TypeError, ValueError):
                continue
    return queue


def load_squad_targets() -> dict[str, list[list[int]]]:
    """Load squad target queues as {squad: [[x, y], ...]}; empty queues drop."""
    try:
        data = json.loads(Path(SQUAD_TARGETS_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, list[list[int]]] = {}
    if isinstance(data, dict):
        for squad in _SQUAD_KEYS:
            queue = _normalize_squad_queue(data.get(squad))
            if queue:
                out[squad] = queue
    return out


def _squad_target_mutation(func):
    @wraps(func)
    def locked(*args, **kwargs):
        with file_lock(SQUAD_TARGETS_FILE):
            return func(*args, **kwargs)

    return locked


def _write_squad_targets_file(targets: dict[str, list[list[int]]]) -> None:
    atomic_write_text(SQUAD_TARGETS_FILE, json.dumps(targets, ensure_ascii=False))


@_squad_target_mutation
def add_squad_target(squad: str, x: int, y: int) -> dict:
    """Append one coordinate to a squad's attack-target queue."""
    squad = _squad_key(squad)
    targets = load_squad_targets()
    queue = targets.get(squad, [])
    if len(queue) >= _SQUAD_TARGET_LIMIT:
        return {"ok": False, "error": f"目标队列已达上限 {_SQUAD_TARGET_LIMIT} 个"}
    queue = queue + [[int(x), int(y)]]
    targets[squad] = queue
    _write_squad_targets_file(targets)
    append_jsonl(BATTLE_LOG_FILE, [{
        "tick": None,
        "ts": time.time(),
        "cat": "config",
        "msg": f"{_SQUAD_LABELS[squad]}新增目标 ({int(x)},{int(y)})，队列第 {len(queue)} 个",
    }])
    return {"ok": True, "squad": squad, "targets": targets}


@_squad_target_mutation
def remove_squad_target(squad: str, index: int | None = None) -> dict:
    """Remove one queued target by index, or the squad's whole queue."""
    squad = _squad_key(squad)
    targets = load_squad_targets()
    queue = targets.get(squad)
    if not queue:
        return {"ok": False, "error": "目标不存在"}
    if index is None:
        del targets[squad]
    else:
        if not 0 <= index < len(queue):
            return {"ok": False, "error": "目标不存在"}
        queue.pop(index)
        if queue:
            targets[squad] = queue
        else:
            del targets[squad]
    _write_squad_targets_file(targets)
    return {"ok": True, "squad": squad, "targets": targets}


@_squad_target_mutation
def clear_squad_targets(squad: str) -> dict:
    squad = _squad_key(squad)
    targets = load_squad_targets()
    targets.pop(squad, None)
    _write_squad_targets_file(targets)
    return {"ok": True, "squad": squad, "targets": targets}


# ── manual per-unit self-destruct (「自裁」 command, shared with the tactic) ──
# The dashboard appends display names here; the tactic process reads the file
# each Tick, issues SELF_DESTRUCT for units that are still alive, then prunes
# the names. Same cross-process lock discipline as waypoints.json.

def _read_self_destruct_file() -> set[str]:
    try:
        with open(SELF_DESTRUCT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return set()
    units = data.get("units") if isinstance(data, dict) else None
    if not isinstance(units, list):
        return set()
    out: set[str] = set()
    for name in units:
        if isinstance(name, str) and _WAYPOINT_NAME_RE.fullmatch(name.upper()):
            out.add(name.upper())
    return out


def _write_self_destruct_file(units: set[str]) -> None:
    atomic_write_text(SELF_DESTRUCT_FILE, json.dumps({
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "units": sorted(units),
    }, ensure_ascii=False))


def request_self_destruct(name: str) -> dict:
    """Queue a per-unit self-destruct command (display-name keyed)."""
    name = _waypoint_name(name)
    with file_lock(SELF_DESTRUCT_FILE):
        units = _read_self_destruct_file()
        units.add(name)
        _write_self_destruct_file(units)
    append_jsonl(BATTLE_LOG_FILE, [{
        "tick": None,
        "ts": time.time(),
        "cat": "config",
        "msg": f"自裁指令：{name}",
    }])
    return {"ok": True, "name": name, "pending": len(units)}


# ── manual per-unit hold position (「驻守」toggle, shared with the tactic) ──
# The dashboard toggles display names in holds.json; the tactic process reads
# the file each Tick, pins those units in place (auto-attack in range, no
# movement), and prunes entries whose unit is gone. Same cross-process lock
# discipline as waypoints.json / self_destruct.json.


def _read_holds_file() -> set[str]:
    try:
        with open(HOLDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return set()
    units = data.get("units") if isinstance(data, dict) else None
    if not isinstance(units, list):
        return set()
    out: set[str] = set()
    for name in units:
        if isinstance(name, str) and _WAYPOINT_NAME_RE.fullmatch(name.upper()):
            out.add(name.upper())
    return out


def _write_holds_file(units: set[str]) -> None:
    atomic_write_text(HOLDS_FILE, json.dumps({
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "units": sorted(units),
    }, ensure_ascii=False))


def load_holds() -> set[str]:
    """Read the current held-unit display names (for /api/holds + cards)."""
    return _read_holds_file()


def set_hold(name: str) -> dict:
    """Enter 驻守模式 for one unit (display-name keyed)."""
    name = _waypoint_name(name)
    with file_lock(HOLDS_FILE):
        units = _read_holds_file()
        units.add(name)
        _write_holds_file(units)
    append_jsonl(BATTLE_LOG_FILE, [{
        "tick": None,
        "ts": time.time(),
        "cat": "config",
        "msg": f"驻守指令：{name}",
    }])
    return {"ok": True, "name": name, "held": len(units)}


def clear_hold(name: str) -> dict:
    """Cancel 驻守模式 for one unit (display-name keyed)."""
    name = _waypoint_name(name)
    with file_lock(HOLDS_FILE):
        units = _read_holds_file()
        before = len(units)
        units.discard(name)
        _write_holds_file(units)
    if before > len(units):
        append_jsonl(BATTLE_LOG_FILE, [{
            "tick": None,
            "ts": time.time(),
            "cat": "config",
            "msg": f"解除驻守：{name}",
        }])
    return {"ok": True, "name": name, "held": len(units)}


TEAM_BOARD_KEYS = ("unassigned", "home", "attack", "kite", "guerrilla")
TEAM_BOARD_META = {
    "unassigned": {"label": "待命池", "hint": "未编队", "tone": "idle"},
    "home": {"label": "守家队", "hint": "巡逻+迎击驱赶", "tone": "home"},
    "attack": {"label": "进攻队", "hint": "集体推进接战", "tone": "attack"},
    "kite": {"label": "风筝队", "hint": "避开射程 · 预判打格", "tone": "kite"},
    "guerrilla": {"label": "游击队", "hint": "八向分散袭扰", "tone": "guerrilla"},
}
TEAM_ROSTER_FIELDS = ("home_team", "attack_team", "kite_team", "guerrilla_team")
TEAM_SETTING_FIELDS = tuple(
    field["key"]
    for field in config_schema()["fields"]
    if field["group"] == "combat" and field["key"] not in TEAM_ROSTER_FIELDS
)
STRATEGY_CONFIG_FIELDS = tuple(
    key
    for key in default_config()
    if key not in set(TEAM_ROSTER_FIELDS) | set(TEAM_SETTING_FIELDS)
)


def reset_strategy_config(path: Path = CONFIG_PATH) -> dict[str, int | bool | str]:
    defaults = default_config()
    return update_config(
        {key: defaults[key] for key in STRATEGY_CONFIG_FIELDS},
        path,
    )


def _parse_roster_names(raw: object) -> list[str]:
    if not isinstance(raw, str) or not raw.strip():
        return []
    names: list[str] = []
    seen: set[str] = set()
    for chunk in raw.replace(";", ",").replace(" ", ",").split(","):
        name = chunk.strip().upper()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _format_roster_names(names: list[str] | set[str]) -> str:
    def sort_key(name: str) -> tuple[str, int, str]:
        prefix = name[:1]
        suffix = name[1:]
        number = int(suffix) if suffix.isdigit() else 0
        return prefix, number, name

    return ", ".join(sorted({str(name).upper() for name in names if str(name).strip()}, key=sort_key))


def _team_of_name(name: str, config: dict) -> str:
    upper = name.upper()
    if upper in _parse_roster_names(config.get("home_team", "")):
        return "home"
    if upper in _parse_roster_names(config.get("attack_team", "")):
        return "attack"
    if upper in _parse_roster_names(config.get("kite_team", "")):
        return "kite"
    if upper in _parse_roster_names(config.get("guerrilla_team", "")):
        return "guerrilla"
    return "unassigned"


# Max HP per combat kind (game rule, mirrored from tactic._UNIT_MAX_HP).
# The segmented health bars on the team board get one segment per point.
_COMBAT_MAX_HP = {"VANGUARD": 4, "RANGER": 2, "COMBAT": 2}


def collect_combat_units(rec: dict | None = None, config: dict | None = None) -> list[dict]:
    """Merge live combat units with configured roster names for the drag board."""
    rec = rec or {}
    config = config if config is not None else load_config(CONFIG_PATH)
    actions = rec.get("plan_unit_actions", {}) or {}
    by_name: dict[str, dict] = {}

    def upsert(name: str, *, kind: str, unit: dict | None = None) -> None:
        key = name.upper()
        if not key:
            return
        item = by_name.get(key) or {
            "name": key,
            "kind": kind,
            "id": "",
            "pos": None,
            "hp": None,
            "action": "",
            "alive": False,
            "team": _team_of_name(key, config),
        }
        item["kind"] = kind or item.get("kind") or ("VANGUARD" if key.startswith("V") else "RANGER")
        item["max_hp"] = _COMBAT_MAX_HP.get(item["kind"], 2)
        item["team"] = _team_of_name(key, config)
        if unit:
            uid = str(unit.get("id") or "")
            sid = short_id(uid)
            item["id"] = sid
            item["pos"] = unit.get("pos")
            item["hp"] = unit.get("hp")
            item["action"] = actions.get(sid) or actions.get(uid, "") or item.get("action", "")
            item["alive"] = True
        by_name[key] = item

    for vanguard in rec.get("vanguards", []) or []:
        name = str(vanguard.get("name") or "").upper()
        if name:
            upsert(name, kind="VANGUARD", unit=vanguard)
    for ranger in rec.get("rangers", []) or []:
        name = str(ranger.get("name") or "").upper()
        if name:
            upsert(name, kind="RANGER", unit=ranger)

    for field_key in TEAM_ROSTER_FIELDS:
        for name in _parse_roster_names(config.get(field_key, "")):
            if name not in by_name:
                kind = (
                    "VANGUARD" if name.startswith("V")
                    else "RANGER" if name.startswith("R")
                    else "COMBAT"
                )
                upsert(name, kind=kind)

    def sort_key(item: dict) -> tuple[str, int, str]:
        name = str(item.get("name") or "")
        suffix = name[1:]
        number = int(suffix) if suffix.isdigit() else 0
        return name[:1], number, name

    return sorted(by_name.values(), key=sort_key)


def unit_display_names(rec: dict | None = None) -> dict[str, str]:
    """Map short unit id -> tactic display name (W1/V2/R3) for the arena SPA.

    The tactic dashboard keys every unit by these names; the arena page looks
    them up by str(object_id)[:8] (the record id format) so both surfaces
    label the same unit identically. Only live units from the latest record
    are exposed — a just-spawned unit appears once the tactic names it.
    """
    rec = rec or {}
    names: dict[str, str] = {}
    for group in ("workers", "vanguards", "rangers"):
        for unit in rec.get(group) or []:
            if not isinstance(unit, dict):
                continue
            uid = unit.get("id")
            name = unit.get("name")
            if isinstance(uid, str) and uid and isinstance(name, str) and name:
                names[uid] = name
    return names


def render_waypoints_panel(
    waypoints: dict[str, dict],
    workers: list[str],
    vanguards: list[str],
    rangers: list[str],
) -> str:
    """Manual per-unit target queue panel: list queues, append one for a unit."""
    def _opts(label: str, names: list[str]) -> str:
        return "".join(
            f'<option value="{html.escape(n, quote=True)}">'
            f'{html.escape(n)}（{label}）</option>'
            for n in names
        )

    options = _opts("工人", workers) + _opts("先锋", vanguards) + _opts("游侠", rangers)
    if not options:
        options = '<option value="" disabled>暂无存活单位</option>'

    if waypoints:
        entries = []
        for name, raw in sorted(waypoints.items()):
            entry = _normalize_wp_entry(raw)
            if entry is None:
                continue
            mode = entry["mode"]
            mode_label = "攻击" if mode == "attack" else "赶路"
            safe_name = html.escape(str(name), quote=True)
            chips = "".join(
                f'<span class="chip removable wp-target" title="第{idx + 1}个">'
                f'{html.escape(f"({x}, {y})")}'
                f'<button type="button" class="chip-x" '
                f'data-wp-remove="{safe_name}" data-wp-index="{idx}" '
                f'aria-label="删除 {safe_name} 第{idx + 1}个目标" title="删除">×</button></span>'
                for idx, (x, y) in enumerate(entry["queue"])
            )
            entries.append(
                '<div class="wp-entry" data-wp-unit="' + safe_name + '">'
                '<div class="wp-entry-head">'
                f'<span class="wp-unit-name">{html.escape(str(name))}</span>'
                f'<button type="button" class="wp-mode-btn" data-wp-mode-toggle="{safe_name}" '
                f'data-mode="{mode}" title="切换模式（攻击=沿途接敌 / 赶路=只行军）">{mode_label}</button>'
                f'<button type="button" class="chip-x" data-wp-clear-unit="{safe_name}" '
                f'title="清空 {html.escape(str(name))} 全部目标">×</button>'
                '</div><div class="wp-targets">' + chips + '</div></div>'
            )
        list_html = f'<div class="wp-list">{"".join(entries)}</div>'
    else:
        list_html = '<div class="muted">暂无手动目标 · 到达后自动清除</div>'

    return (
        '<section class="panel waypoint-panel" id="waypointPanel">'
        '<div class="panel-title"><span>手动目标</span>'
        f'<span class="count" id="waypointCount">{_wp_count(waypoints)} 个</span></div>'
        f'{list_html}'
        '<div class="wp-add">'
        f'<select id="wpName" title="选择单位"><option value="">选择单位…</option>{options}</select>'
        '<input id="wpX" type="number" step="1" min="-1000" max="1000" placeholder="X" required>'
        '<input id="wpY" type="number" step="1" min="-1000" max="1000" placeholder="Y" required>'
        '<button type="button" class="pick-btn" id="pickWpBtn" '
        'title="点击地图选择坐标（X 与 Y 一起填入）">⌖</button>'
        '<select id="wpMode" title="行军模式">'
        '<option value="attack" selected>攻击</option>'
        '<option value="rush">赶路</option></select>'
        '<button type="button" id="wpSetBtn">加入队列</button>'
        '<button type="button" class="secondary" id="wpClearBtn">清空全部</button>'
        '</div>'
        '<div class="wp-msg" id="wpMsg">到达目标后自动恢复程序行动</div>'
        '</section>'
    )


def short_id(uid: str) -> str:
    return (uid or "?")[:8]


def fmt_pos(pos) -> str:
    if not pos:
        return "-"
    return f"({int(pos[0])}, {int(pos[1])})"


def _seg_cells(cur, total: int) -> str:
    """One <i> per point of a segmented vitality bar; filled cells are 'on'.

    Values clamp to [0, total] so a stale log (e.g. shield already above the
    display cap) never overflows the bar. None means unknown → all cells empty.
    """
    total = int(total)
    if total <= 0:
        return ""
    cur = 0 if cur is None else max(0, min(int(cur), total))
    return '<i class="on"></i>' * cur + '<i></i>' * (total - cur)


def _core_rail_html(rec: dict) -> str:
    """Left-rail core card: position + segmented HP/shield bars + quick rows."""
    cp = rec.get("core_pos")
    hp = rec.get("core_hp")
    sh = rec.get("core_shield")
    bp = rec.get("beacon_pos")
    hp_total = 5
    # The Core's shield caps at 10 only while it carries the champion beacon;
    # otherwise the game (and the repair logic) keeps it at 5.
    sh_total = 10 if (cp and bp and int(cp[0]) == int(bp[0]) and int(cp[1]) == int(bp[1])) else 5
    return (
        f'<div class="rail-focus"><div class="rail-focus-main"><span class="rail-eyebrow">当前位置</span>'
        f'<strong class="rail-value">{fmt_pos(cp)}</strong></div></div>'
        f'<div class="rail-vitals">'
        f'<div class="vital"><span class="vital-label hp">HP</span>'
        f'<span class="vital-cells hp">{_seg_cells(hp, hp_total)}</span>'
        f'<span class="vital-num">{hp if hp is not None else "?"}/{hp_total}</span></div>'
        f'<div class="vital"><span class="vital-label sh">盾</span>'
        f'<span class="vital-cells sh">{_seg_cells(sh, sh_total)}</span>'
        f'<span class="vital-num">{sh if sh is not None else "?"}/{sh_total}</span></div></div>'
        f'<div class="rail-rows"><div class="rail-row"><span>当前动作</span><b>{rec.get("core_action") or "—"}</b></div>'
        f'<div class="rail-row"><span>人口</span><b>{rec.get("population",0)}</b></div>'
        f'<div class="rail-row"><span>信标坐标</span><b>{fmt_pos(bp)}</b></div></div>'
    )


def action_kind(action: str, cargo: int = 0) -> str:
    a = action or ""
    if "DEPOSIT" in a: return "deposit"
    if "HARVEST" in a: return "harvest"
    if "WAIT" in a: return "wait"
    if cargo: return "cargo"
    if "scout" in a or "explore" in a: return "explore"
    if "enemy" in a or "SWEEP" in a or "SHOOT" in a: return "combat"
    if "MOVE" in a: return "move"
    return "other"


def action_label(action: str, cargo: int = 0) -> str:
    return {
        "deposit": "交矿", "harvest": "挖矿", "wait": "等待",
        "cargo": "回矿", "explore": "探索", "combat": "作战",
        "move": "移动", "other": "其他",
    }.get(action_kind(action, cargo), "其他")


def check_stuck(history):
    issues = []
    if len(history) < 5:
        return issues
    pos_log = defaultdict(list)
    for rec in history:
        for w in rec.get("workers", []):
            pos_log[w.get("id", "")].append(tuple(w.get("pos", [])))
    for wid, positions in pos_log.items():
        if len(positions) < 5:
            continue
        recent = positions[:8]
        if len(set(recent)) == 1:
            issues.append({"level": "danger", "title": "卡住",
                           "detail": f"{short_id(wid)} 连续 {len(positions)} 帧停在 {fmt_pos(recent[0])}"})
        elif len(set(recent)) <= 2 and len(set(recent)) < len(recent):
            u = list(set(recent))
            issues.append({"level": "warn", "title": "来回走",
                           "detail": f"{short_id(wid)} 在 {fmt_pos(u[0])} / {fmt_pos(u[1])} 摆动"})
    return issues


# ---------- SVG map -------------------------------------------------------

def _remaining_path(unit_data: dict) -> list:
    """Return the planned route minus the cells the unit has already walked.

    The planner re-emits the full A* polyline every tick while a unit advances
    along a cached path, so the log record's ``path`` is stale — it still
    contains the origin the unit started from. Trim everything up to and
    including the unit's current cell so the map and step counts only show the
    route that is actually still ahead.
    """
    path = []
    for p in (unit_data.get("path") or []):
        if len(p) == 2:
            path.append([int(p[0]), int(p[1])])
    cur = unit_data.get("pos") or []
    if len(cur) == 2 and len(path) > 1:
        try:
            start = path.index([int(cur[0]), int(cur[1])])
        except ValueError:
            return path
        return path[start:]
    return path


def _collect_points(rec, mm):
    pts = []
    c = rec.get("core_pos")
    if c: pts.append((int(c[0]), int(c[1])))
    for g in ("workers", "vanguards", "rangers", "enemies"):
        for u in rec.get(g, []) or []:
            p = u.get("pos") or []
            if len(p) == 2: pts.append((int(p[0]), int(p[1])))
            target = u.get("target") or []
            if len(target) == 2: pts.append((int(target[0]), int(target[1])))
            for route_pos in u.get("path", []) or []:
                if len(route_pos) == 2:
                    pts.append((int(route_pos[0]), int(route_pos[1])))
    for p in rec.get("resource_cells", []) or []:
        if len(p) == 2: pts.append((int(p[0]), int(p[1])))
    for p in mm.get("obstacles", []): pts.append((int(p[0]), int(p[1])))
    for p in mm.get("resources", []): pts.append((int(p[0]), int(p[1])))
    for s in mm.get("enemy_sightings", []):
        parsed = _parse_enemy_sighting(s)
        if parsed is not None:
            pts.append(parsed[0])
    bp = rec.get("beacon_pos")
    if bp and len(bp) == 2: pts.append((int(bp[0]), int(bp[1])))
    return pts


def render_svg(rec, mm, cell: int = 16, pad: int = 24, margin: int = 4,
               config: dict | None = None, waypoints: dict | None = None,
               hotspots: list | None = None):
    if config is None:
        config = load_config(CONFIG_PATH)
    if waypoints is None:
        waypoints = load_waypoints()
    pts = _collect_points(rec, mm)
    # Hotspot cells from the battle log may sit outside the live-unit bounds
    # (e.g. a fight where every participant died) — keep them on the canvas.
    for s in (hotspots or []):
        pts.append((s["x"], s["y"]))
    core = rec.get("core_pos")
    if not pts:
        return '<div class="muted">暂无地图数据</div>'
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    xmin, xmax = min(xs) - margin, max(xs) + margin
    ymin, ymax = min(ys) - margin, max(ys) + margin
    if xmax - xmin < 24:
        m = (xmin + xmax) // 2; xmin, xmax = m - 12, m + 12
    if ymax - ymin < 24:
        m = (ymin + ymax) // 2; ymin, ymax = m - 12, m + 12

    cols, rows = xmax - xmin + 1, ymax - ymin + 1
    W, H = cols * cell + pad * 2, rows * cell + pad * 2

    def to_xy(x, y):
        # Game UP decreases Y, so smaller world-Y must sit higher on screen.
        return pad + (x - xmin) * cell, pad + (y - ymin) * cell

    obs = {(int(a), int(b)) for a, b in mm.get("obstacles", [])}
    mem_r = {(int(a), int(b)) for a, b in mm.get("resources", [])}
    vis_r = {(int(a), int(b)) for a, b in mm.get("resources", [])}  # placeholder
    vis_r = set()
    for p in rec.get("resource_cells", []) or []:
        if len(p) == 2: vis_r.add((int(p[0]), int(p[1])))

    core_cx = core_cy = None
    if core and len(core) == 2:
        cx_, cy_ = to_xy(int(core[0]), int(core[1]))
        core_cx, core_cy = cx_ + cell / 2, cy_ + cell / 2

    out = []
    a = out.append
    a(f'<svg class="game-map" id="gameMap" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
      f'data-width="{W}" data-height="{H}" data-xmin="{xmin}" data-xmax="{xmax}" '
      f'data-ymin="{ymin}" data-ymax="{ymax}" data-cell="{cell}" data-pad="{pad}" '
      f'data-focus-x="{(core_cx if core_cx is not None else W/2):.1f}" '
      f'data-focus-y="{(core_cy if core_cy is not None else H/2):.1f}" '
      f'role="img" aria-label="known-map" style="width:{W}px;height:{H}px">')
    a('<defs>'
      '<pattern id="gridPat" x="{p}" y="{p}" width="{c}" height="{c}" patternUnits="userSpaceOnUse">'
      '<rect width="{c}" height="{c}" fill="#10182c"/>'
      '<rect width="{c}" height="{c}" fill="#152038" opacity="0.35"/>'
      '<path d="M {c} 0 L 0 0 0 {c}" fill="none" stroke="rgba(255,255,255,0.04)" stroke-width="1"/>'
      '</pattern>'
      '<radialGradient id="coreGlow" cx="50%" cy="50%" r="50%">'
      '<stop offset="0%" stop-color="#6ea8ff" stop-opacity="0.55"/>'
      '<stop offset="100%" stop-color="#6ea8ff" stop-opacity="0"/>'
      '</radialGradient>'
      '<filter id="glow" x="-50%" y="-50%" width="200%" height="200%">'
      '<feGaussianBlur stdDeviation="2.2" result="b"/>'
      '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>'
      '</filter>'
      '</defs>'.format(c=cell, p=pad))
    a(f'<rect x="0" y="0" width="{W}" height="{H}" fill="#0b1222"/>')
    a(f'<rect x="{pad}" y="{pad}" width="{cols*cell}" height="{rows*cell}" fill="url(#gridPat)" '
      f'stroke="rgba(255,255,255,0.06)" rx="8"/>')
    # major grid
    step = 5 if max(cols, rows) <= 80 else 10
    for x in range(xmin, xmax + 1):
        if x % step == 0:
            px, _ = to_xy(x, ymin)
            a(f'<line x1="{px}" y1="{pad}" x2="{px}" y2="{pad+rows*cell}" stroke="rgba(110,168,255,0.08)"/>')
    for y in range(ymin, ymax + 1):
        if y % step == 0:
            _, py = to_xy(xmin, y)
            a(f'<line x1="{pad}" y1="{py}" x2="{pad+cols*cell}" y2="{py}" stroke="rgba(110,168,255,0.08)"/>')

    # obstacles — one <path> carrying every wall cell as a subpath. A full map
    # can hold thousands of wall cells; a <rect> per cell turned into thousands
    # of DOM nodes that the browser had to keep and re-layout on every pan.
    wall_d = []
    for ox, oy in obs:
        if not (xmin <= ox <= xmax and ymin <= oy <= ymax): continue
        x, y = to_xy(ox, oy)
        # Fits one cell with the same inset the old rects used (1.2px each side).
        wall_d.append(f"M{x + 1.2},{y + 1.2}h{cell - 2.4}v{cell - 2.4}h-{cell - 2.4}z")
    if wall_d:
        a(f'<path data-cat="wall" d="{" ".join(wall_d)}" '
          f'fill="#3a455f" stroke="#7f8eab" stroke-width="1"/>')

    # remembered resources
    for rx, ry in mem_r - vis_r:
        if not (xmin <= rx <= xmax and ymin <= ry <= ymax): continue
        x, y = to_xy(rx, ry)
        cxr, cyr = x + cell / 2, y + cell / 2
        a(f'<circle data-cat="ore-mem" cx="{cxr}" cy="{cyr}" r="4.5" fill="#c9a227" opacity="0.55"/>')
        a(f'<circle data-cat="ore-mem" cx="{cxr}" cy="{cyr}" r="2.2" fill="#ffe08a" opacity="0.85"/>')
    for rx, ry in vis_r:
        if not (xmin <= rx <= xmax and ymin <= ry <= ymax): continue
        x, y = to_xy(rx, ry)
        cxr, cyr = x + cell / 2, y + cell / 2
        a(f'<circle data-cat="ore" cx="{cxr}" cy="{cyr}" r="6.5" fill="#ffc857" filter="url(#glow)"/>')
        a(f'<circle data-cat="ore" cx="{cxr}" cy="{cyr}" r="2.8" fill="#fff3c4"/>')

    # Enemy sightings (remembered, last-known positions).  Each marker carries
    # the unit type last seen there so an out-of-vision CORE (总部) can be told
    # from a worker scout without re-scouting.  The dashed ring + dim fill mark
    # it as last-known (possibly moved).  Live visible enemies are skipped so
    # the brighter unit marker on top is not duplicated underneath.
    visible_enemy_positions = {
        tuple(int(c) for c in (e.get("pos") or []))
        for e in rec.get("enemies", [])
        if len(e.get("pos") or []) == 2
    }
    for sighting in mm.get("enemy_sightings", []):
        parsed = _parse_enemy_sighting(sighting)
        if parsed is None:
            continue
        ex, ey = parsed[0]
        if (ex, ey) in visible_enemy_positions:
            continue
        if not (xmin <= ex <= xmax and ymin <= ey <= ymax):
            continue
        etype = parsed[1]
        color = _enemy_type_color(etype)
        label = _enemy_type_char(etype)
        x, y = to_xy(ex, ey)
        cxr, cyr = x + cell / 2, y + cell / 2
        if etype == "CORE":
            # Headquarters use a unique diamond + star silhouette. At the
            # default 16px cell size this reads much faster than another red
            # circle containing a dense one-character Chinese label.
            radius = 8.5
            points = (f"{cxr},{cyr-radius} {cxr+radius},{cyr} "
                      f"{cxr},{cyr+radius} {cxr-radius},{cyr}")
            a(f'<circle data-cat="enemy-trace" cx="{cxr}" cy="{cyr}" r="12" '
              f'fill="{color}" opacity="0.18"/>')
            a(f'<polygon data-cat="enemy-trace" data-marker="enemy-core-memory" '
              f'points="{points}" fill="{color}" fill-opacity="0.68" '
              f'stroke="#ffd5dc" stroke-width="1.6" stroke-dasharray="3 2" '
              f'filter="url(#glow)"/>')
            a(f'<text data-cat="enemy-trace" x="{cxr}" y="{cyr + 3.1}" '
              f'text-anchor="middle" font-size="9.5" '
              f'font-family="Segoe UI Symbol, Segoe UI, Microsoft YaHei, sans-serif" '
              f'font-weight="700" fill="#fff4f6">{label}</text>')
        else:
            a(f'<circle data-cat="enemy-trace" cx="{cxr}" cy="{cyr}" r="9" fill="{color}" opacity="0.14"/>')
            a(f'<circle data-cat="enemy-trace" cx="{cxr}" cy="{cyr}" r="6.5" fill="{color}" opacity="0.5"/>')
            a(f'<circle data-cat="enemy-trace" cx="{cxr}" cy="{cyr}" r="6.5" fill="none" '
              f'stroke="{color}" stroke-width="1.2" stroke-dasharray="3 3" opacity="0.95"/>')
            a(f'<text data-cat="enemy-trace" x="{cxr}" y="{cyr + 2.2}" text-anchor="middle" '
              f'font-size="7.5" font-family="Segoe UI, Microsoft YaHei, sans-serif" '
              f'font-weight="700" fill="#0b1020" opacity="0.9">{label}</text>')

    # Combat hotspots: pulsing double ring where a fight is on (engaged),
    # dashed orange ring where one just happened (recent). Drawn below
    # routes/units so the markers stay readable on top. Sighted-only enemies
    # already have their unit markers, no extra ring.
    for s in (hotspots or []):
        if s.get("level") == "sighted":
            continue
        hx, hy = s["x"], s["y"]
        if not (xmin <= hx <= xmax and ymin <= hy <= ymax):
            continue
        x, y = to_xy(hx, hy)
        hcx, hcy = x + cell / 2, y + cell / 2
        if s["level"] == "engaged":
            a(f'<circle data-cat="combat" cx="{hcx}" cy="{hcy}" r="17" fill="none" '
              f'stroke="#ff4964" stroke-width="1.3" opacity="0.65"/>')
            a(f'<circle data-cat="combat" class="hotspot-ring engaged" cx="{hcx}" cy="{hcy}" r="12" '
              f'fill="rgba(255,73,100,0.15)" stroke="#ff4964" stroke-width="2"/>')
        else:
            a(f'<circle data-cat="combat" cx="{hcx}" cy="{hcy}" r="12" fill="none" '
              f'stroke="#ff8c42" stroke-width="1.6" stroke-dasharray="4 3" opacity="0.85"/>')

    # Worker routes and targets are generated by the same planner that moves them.
    route_colors = (
        "#63d8ff", "#57d6a3", "#ffc857", "#ff7aa9",
        "#b38cff", "#ff8a65", "#8fd14f", "#78a9ff",
    )

    def _draw_route(unit_data, name, color, css_class=""):
        # Unit names ultimately come from the game server (enemy names are
        # opponent-controlled), so escape before interpolating into attributes.
        safe_name = html.escape(str(name), quote=True)
        # Trim the already-walked segment: the planner logs the full A* polyline
        # every tick while a unit advances along it, so without this the map
        # keeps showing ground the unit has already crossed.
        path = _remaining_path(unit_data)
        target = unit_data.get("target") or []
        if len(path) > 1:
            route_points = []
            for px, py in path:
                x, y = to_xy(int(px), int(py))
                route_points.append(f"{x + cell / 2:.1f},{y + cell / 2:.1f}")
            points_attr = " ".join(route_points)
            dash = "" if unit_data.get("path_complete") else ' stroke-dasharray="5 4"'
            a(f'<polyline class="{css_class}" data-cat="route" data-unit="{safe_name}" '
              f'points="{points_attr}" fill="none" stroke="{color}" '
              f'stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round" '
              f'opacity="0.82"{dash}/>')
        if len(target) == 2:
            tx, ty = int(target[0]), int(target[1])
            if xmin <= tx <= xmax and ymin <= ty <= ymax:
                x, y = to_xy(tx, ty)
                cx, cy = x + cell / 2, y + cell / 2
                a(f'<circle class="{css_class}-target" data-cat="target" data-unit="{safe_name}" cx="{cx}" cy="{cy}" '
                  f'r="8" fill="none" stroke="{color}" stroke-width="1.8" opacity="0.9"/>')
                a(f'<circle data-cat="target" cx="{cx}" cy="{cy}" r="2" fill="{color}"/>')

    for index, worker in enumerate(rec.get("workers", []) or []):
        _draw_route(worker, worker.get("name") or f"W{index + 1}",
                     route_colors[index % len(route_colors)], "worker-route")

    # Vanguard routes (orange)
    for index, v in enumerate(rec.get("vanguards", []) or []):
        _draw_route(v, v.get("name") or f"V{index + 1}", "#ff8c42", "vanguard-route")

    # Ranger routes (teal)
    for index, r in enumerate(rec.get("rangers", []) or []):
        _draw_route(r, r.get("name") or f"R{index + 1}", "#6ea8ff", "ranger-route")

    def unit(pos, color, label, glow=False, ring=None, cat="unit", unit_name=None,
             anim_key=None):
        if not pos or len(pos) != 2: return
        px, py = int(pos[0]), int(pos[1])
        if not (xmin <= px <= xmax and ymin <= py <= ymax): return
        x, y = to_xy(px, py)
        ux, uy = x + cell / 2, y + cell / 2
        # data-unit lets the waypoint panel pick a unit by clicking its marker.
        du = f' data-unit="{html.escape(str(unit_name), quote=True)}"' if unit_name else ""
        # data-anim + world coords give the dashboard a stable identity per
        # marker so it can FLIP-animate units between tick positions instead
        # of teleporting them on every SVG swap. Names are server/opponent
        # controlled — escape before embedding.
        if anim_key:
            a(f'<g data-anim="{html.escape(str(anim_key), quote=True)}" '
              f'data-wx="{px}" data-wy="{py}">')
        if glow: a(f'<circle data-cat="{cat}"{du} cx="{ux}" cy="{uy}" r="11" fill="{color}" opacity="0.18"/>')
        if ring: a(f'<circle data-cat="{cat}"{du} cx="{ux}" cy="{uy}" r="8.5" fill="none" stroke="{ring}" stroke-width="2"/>')
        unit_radius = 7.5 if len(str(label)) > 2 else 7
        font_size = 6 if len(str(label)) > 2 else 7
        a(f'<circle data-cat="{cat}"{du} cx="{ux}" cy="{uy}" r="{unit_radius}" fill="{color}" filter="url(#glow)" '
          f'stroke="rgba(255,255,255,0.65)" stroke-width="1.2"/>')
        # Enemy labels are opponent-controlled: escape before embedding as text.
        a(f'<text data-cat="{cat}"{du} x="{ux}" y="{uy+2.5}" text-anchor="middle" font-size="{font_size}" '
          f'font-family="Segoe UI, Microsoft YaHei, sans-serif" font-weight="700" fill="#0b1020">'
          f'{html.escape(str(label))}</text>')
        if anim_key:
            a('</g>')

    def enemy_core(pos, label):
        """Draw a live enemy HQ with a silhouette unlike any mobile unit."""
        if not pos or len(pos) != 2:
            return
        px, py = int(pos[0]), int(pos[1])
        if not (xmin <= px <= xmax and ymin <= py <= ymax):
            return
        x, y = to_xy(px, py)
        ux, uy = x + cell / 2, y + cell / 2
        # Same FLIP identity as mobile units (see unit() above).
        a(f'<g data-anim="E:{html.escape(str(label), quote=True)}" '
          f'data-wx="{px}" data-wy="{py}">')
        radius = 9.5
        points = (f"{ux},{uy-radius} {ux+radius},{uy} "
                  f"{ux},{uy+radius} {ux-radius},{uy}")
        a(f'<circle data-cat="enemy-core" cx="{ux}" cy="{uy}" r="14" '
          f'fill="#ff4964" opacity="0.22"/>')
        a(f'<polygon data-cat="enemy-core" data-marker="enemy-core-live" '
          f'points="{points}" fill="#ff4964" stroke="#ffe0e5" '
          f'stroke-width="1.8" filter="url(#glow)"/>')
        a(f'<text data-cat="enemy-core" x="{ux}" y="{uy+2.5}" text-anchor="middle" '
          f'font-size="7" font-family="Segoe UI, Microsoft YaHei, sans-serif" '
          f'font-weight="800" fill="#2b0710">{html.escape(str(label))}</text>')
        a('</g>')

    for index, w in enumerate(rec.get("workers", [])):
        c = bool(w.get("cargo"))
        name = w.get("name") or f"W{index + 1}"
        unit(w.get("pos"), "#57d6a3" if c else "#8aa4ff", name, glow=c, ring="#9ef0c8" if c else None, cat="worker", unit_name=name, anim_key=name)
    for index, v in enumerate(rec.get("vanguards", [])):
        name = v.get("name") or f"V{index + 1}"
        unit(v.get("pos"), "#ff8c42", name, glow=True, cat="vanguard", unit_name=name, anim_key=name)
    for index, r in enumerate(rec.get("rangers", [])):
        name = r.get("name") or f"R{index + 1}"
        unit(r.get("pos"), "#b38cff", name, glow=True, cat="ranger", unit_name=name, anim_key=name)
    # Visible enemies are filtered per type (WORKER / VANGUARD / RANGER / CORE)
    # so the legend can show/hide each class independently.  Enemies without a
    # typed unit_type land in the generic ENEMY category.  Several enemies can
    # share one cell — the enemy CORE has its spawned workers standing on the
    # HQ square — so per cell only the highest-priority unit is drawn; otherwise
    # a blue worker marker would cover the red enemy-HQ marker at the same spot.
    _ENEMY_PRIORITY = {"ENEMY": 0, "WORKER": 1, "VANGUARD": 2, "RANGER": 2, "CORE": 3}

    def _enemy_rank(e):
        return _ENEMY_PRIORITY.get(str(e.get("type") or "ENEMY").upper(), 0)

    drawn_enemy_cells: set[tuple[int, int]] = set()
    fallback = 1
    for enemy in sorted(rec.get("enemies", []) or [], key=_enemy_rank, reverse=True):
        epos = enemy.get("pos") or []
        if len(epos) != 2:
            continue
        ekey = (int(epos[0]), int(epos[1]))
        if ekey in drawn_enemy_cells:
            continue
        drawn_enemy_cells.add(ekey)
        etype = str(enemy.get("type") or "ENEMY").upper()
        name = enemy.get("name") or f"E{fallback}"
        fallback += 1
        if etype == "CORE":
            enemy_core(epos, name)
            continue
        if etype == "WORKER":
            cat, color, ring = "enemy-worker", "#8aa4ff", None
        elif etype == "VANGUARD":
            cat, color, ring = "enemy-vanguard", "#ff8c42", None
        elif etype == "RANGER":
            cat, color, ring = "enemy-ranger", "#b38cff", None
        else:
            cat, color, ring = "enemy", "#ff6464", "#ff9b9b"
        # E: prefix keeps enemy FLIP identities disjoint from own-unit names.
        unit(epos, color, name, glow=True, ring=ring, cat=cat, anim_key="E:" + name)

    if core_cx is not None:
        # Wrapped like the unit markers so core migration glides too.
        a(f'<g data-anim="__core__" data-wx="{int(core[0])}" data-wy="{int(core[1])}">')
        a(f'<circle data-cat="core" cx="{core_cx}" cy="{core_cy}" r="15" fill="url(#coreGlow)"/>')
        a(f'<rect data-cat="core" x="{core_cx-6.5}" y="{core_cy-6.5}" width="13" height="13" rx="3" '
          f'transform="rotate(45 {core_cx} {core_cy})" fill="#6ea8ff" stroke="#d7e8ff" '
          f'stroke-width="1.4" filter="url(#glow)"/>')
        a(f'<text data-cat="core" x="{core_cx}" y="{core_cy+3}" text-anchor="middle" font-size="8" '
          f'font-family="Segoe UI, Microsoft YaHei, sans-serif" font-weight="700" fill="#081018">'
          f'{html.escape(str(rec.get("core_name") or "C1"))}</text>')
        a('</g>')

    # Beacon
    bp = rec.get("beacon_pos")
    if bp and len(bp) == 2:
        bx, by = to_xy(int(bp[0]), int(bp[1]))
        bcx, bcy = bx + cell / 2, by + cell / 2
        # Glow
        a(f'<circle data-cat="beacon" cx="{bcx}" cy="{bcy}" r="14" fill="#ffc857" opacity="0.18"/>')
        # Diamond shape
        r = 7.5
        a(f'<polygon data-cat="beacon" points="{bcx},{bcy-r} {bcx+r},{bcy} {bcx},{bcy+r} {bcx-r},{bcy}" '
          f'fill="#ffc857" stroke="#ffe08a" stroke-width="1.5" filter="url(#glow)"/>')
        # Inner star
        a(f'<text data-cat="beacon" x="{bcx}" y="{bcy+3.5}" text-anchor="middle" font-size="9" '
          f'font-family="Segoe UI, Microsoft YaHei, sans-serif" font-weight="700" fill="#5c4300">★</text>')

    # Configured strategy points — the same coordinates the map-pick buttons
    # fill. Drawing them makes the map double as a coordinate reference.
    def _config_marker(x, y, color, prefix, cat):
        if not (xmin <= x <= xmax and ymin <= y <= ymax):
            return
        px, py = to_xy(x, y)
        cx, cy = px + cell / 2, py + cell / 2
        label = f"{prefix}({x},{y})"
        w = 6.5 * len(label) + 12
        a(f'<circle data-cat="{cat}" cx="{cx}" cy="{cy}" r="9" fill="none" stroke="{color}" '
          f'stroke-width="1.6" stroke-dasharray="4 3" opacity="0.9"/>')
        a(f'<circle data-cat="{cat}" cx="{cx}" cy="{cy}" r="2.4" fill="{color}"/>')
        a(f'<rect data-cat="{cat}" x="{cx + 8}" y="{cy - 12}" width="{w:.0f}" height="16" rx="8" '
          f'fill="#0b1222" stroke="{color}" stroke-opacity="0.55" stroke-width="1"/>')
        a(f'<text data-cat="{cat}" x="{cx + 8 + w / 2:.0f}" y="{cy + 0.5}" text-anchor="middle" '
          f'font-size="10" fill="{color}" font-family="Consolas, monospace">{label}</text>')

    if str(config.get("attack_mode", "coords")) == "coords":
        _config_marker(int(config.get("attack_target_x", 0)),
                       int(config.get("attack_target_y", 0)), "#ff8c42", "攻", "attack-target")
    if str(config.get("kite_mode", "coords")) == "coords":
        _config_marker(int(config.get("kite_target_x", 0)),
                       int(config.get("kite_target_y", 0)), "#3dd6c9", "筝", "kite-target")
    if bool(config.get("core_target_enabled", False)):
        _config_marker(int(config.get("core_target_x", 0)),
                       int(config.get("core_target_y", 0)), "#6ea8ff", "核", "core-target")

    # Manual per-unit target queues (dashboard ⌖). One marker per queued point.
    for name, raw in sorted(waypoints.items()):
        entry = _normalize_wp_entry(raw)
        if entry is None:
            continue
        for x, y in entry["queue"]:
            if not (xmin <= x <= xmax and ymin <= y <= ymax):
                continue
            px, py = to_xy(x, y)
            cx, cy = px + cell / 2, py + cell / 2
            label = html.escape(f"{name}→({x},{y})")
            w = 6.5 * len(label) + 12
            a(f'<circle data-cat="wp" cx="{cx}" cy="{cy}" r="9" fill="none" stroke="#3dd6c9" '
              f'stroke-width="1.6" stroke-dasharray="4 3" opacity="0.9"/>')
            a(f'<circle data-cat="wp" cx="{cx}" cy="{cy}" r="2.4" fill="#3dd6c9"/>')
            a(f'<rect data-cat="wp" x="{cx + 8}" y="{cy - 12}" width="{w:.0f}" height="16" rx="8" '
              f'fill="#0b1222" stroke="#3dd6c9" stroke-opacity="0.55" stroke-width="1"/>')
            a(f'<text data-cat="wp" x="{cx + 8 + w / 2:.0f}" y="{cy + 0.5}" text-anchor="middle" '
              f'font-size="10" fill="#3dd6c9" font-family="Consolas, monospace">{label}</text>')

    for x in range(xmin, xmax + 1):
        if x % step == 0:
            px, _ = to_xy(x, ymin)
            a(f'<text x="{px+cell/2}" y="{H-6}" text-anchor="middle" fill="#7f8eab" font-size="9" '
              f'font-family="Consolas, monospace">{x}</text>')
    for y in range(ymin, ymax + 1):
        if y % step == 0:
            _, py = to_xy(xmin, y)
            a(f'<text x="10" y="{py+cell/2+3}" text-anchor="middle" fill="#7f8eab" font-size="9" '
              f'font-family="Consolas, monospace">{y}</text>')
    a(f'<text x="{pad}" y="16" fill="#93a0bf" font-size="11" '
      f'font-family="Segoe UI, Microsoft YaHei, sans-serif">'
      f'map {xmin},{ymin} ~ {xmax},{ymax} · {cols}x{rows}</text>')
    a('</svg>')
    return "".join(out)


def build_parts(log_limit: int = 200):
    """Build all dashboard fragments + map for page and /api/state."""
    history = read_history(40)
    rec = history[0] if history else None
    try:
        mtime = os.path.getmtime(LOG_FILE)
    except OSError:
        mtime = time.time()
    issues = check_stuck(history)
    age = time.time() - mtime if mtime else 0
    if not rec:
        return None

    workers = rec.get("workers", [])
    vgs = rec.get("vanguards", [])
    rgs = rec.get("rangers", [])
    actions = rec.get("plan_unit_actions", {})
    resources = rec.get("resources", 0)
    cap = rec.get("resource_capacity", 50)
    pct = min(100, int(resources * 100 / cap)) if cap else 0
    enemies = rec.get("visible_enemies", 0)
    rcells = rec.get("resource_cells", [])
    mm = load_map_memory()
    waypoints = load_waypoints()
    # Held (驻守) unit names — the per-unit cards render a 驻守 toggle for them.
    holds = load_holds()
    # Combat hotspots drive both the alert strip above the map and the pulse
    # rings on it — compute once, reuse for both.
    hotspots = _combat_hotspots(rec)
    hotspots_html = _hotspots_html(hotspots)
    # render_svg redraws every known obstacle/resource/route on each poll. The
    # output only changes with the newest tick record, the map file, the
    # waypoints file, or the few config markers drawn on the map — so cache it
    # by those signatures and skip the 4ms re-render when nothing moved.
    svg = _render_svg_cached(rec, mm, waypoints, hotspots)
    running = age < 30
    status_cls = "ok" if running else "down"
    status_text = "运行中" if running else "已停止"

    stats = defaultdict(int)
    for w in workers:
        wid = short_id(w.get("id", ""))
        act = actions.get(wid) or actions.get(w.get("id", ""), "")
        stats[action_kind(act, w.get("cargo", 0))] += 1

    # Battle-report statistics (economy / combat / production + per-unit).
    game_stat = game_stats.load()
    derived = game_stats.derive(game_stat, alive_workers=len(workers))
    per_worker = game_stat.get("per_worker", {}) or {}
    per_combat = game_stat.get("per_combat", {}) or {}

    def wcard(w):
        wid = w.get("id", "")
        sid = short_id(wid)
        name = w.get("name") or f"W{workers.index(w) + 1}"
        act = actions.get(sid) or actions.get(wid, "")
        cargo = w.get("cargo", 0)
        kind = action_kind(act, cargo)
        badge = action_label(act, cargo)
        path = _remaining_path(w)
        target = w.get("target")
        steps = max(0, len(path) - 1)
        route_text = f"{steps}步" if w.get("path_complete") else f"规划{steps}步"
        vitals = f"矿 {cargo}" if cargo else f'HP {w.get("hp","?")}'
        pw = per_worker.get(sid)
        history_text = ""
        if pw is not None and (pw.get("harvested", 0) or pw.get("deposited", 0)):
            history_text = (
                f'<span class="unit-fact" title="累计采矿 / 卸货">'
                f'采{pw.get("harvested", 0)}·卸{pw.get("deposited", 0)}</span>'
            )
        safe_name = html.escape(str(name))
        safe_sid = html.escape(str(sid))
        safe_act = html.escape(str(act or "暂无指令"))
        # The card carries the unit's live world position so a click can jump
        # the map view to it (see focusWorld in the JS).
        wpos = w.get("pos") or []
        focus_attr = (
            f' data-focus-wx="{int(wpos[0])}" data-focus-wy="{int(wpos[1])}"'
            if len(wpos) == 2 else ""
        )
        held = str(name).upper() in holds
        hold_label = "解除驻守" if held else "驻守"
        return (
            f'<div class="unit {kind}{" hold" if held else ""}" title="{safe_act}"{focus_attr}><div class="unit-top">'
            f'<div class="unit-id">{safe_name}<span class="count">{safe_sid}</span></div>'
            f'<span class="unit-actions"><span class="badge {kind}">{html.escape(str(badge))}</span>'
            f'<button type="button" class="hold-btn{" on" if held else ""}" data-hold-unit="{safe_name}" '
            f'aria-label="{hold_label} {safe_name}" title="{hold_label}（原地驻守，进入射程自动攻击）">{hold_label}</button>'
            f'<button type="button" class="sd-btn" data-sd-unit="{safe_name}" '
            f'aria-label="自裁 {safe_name}" title="自裁">自裁</button></span></div>'
            f'<div class="unit-facts"><span class="unit-locator">{fmt_pos(w.get("pos"))}'
            f'<span class="arrow">→</span>{fmt_pos(target)}</span>'
            f'<span class="unit-fact">{vitals}</span><span class="unit-fact">{route_text}</span>{history_text}</div></div>'
        )

    def team_label(act: str) -> str:
        if "[home]" in act:
            return "守家"
        if "[attack]" in act:
            return "进攻"
        if "[guerrilla]" in act:
            return "游击"
        if "[unassigned]" in act:
            return "未编队"
        return "作战"

    def combat_stat_line(sid: str) -> str:
        """Compact per-combat-unit shots / hits, or death summary."""
        rec = per_combat.get(sid)
        if not rec:
            return ""
        shots = int(rec.get("shots", 0) or 0)
        hits = int(rec.get("hits", 0) or 0)
        rate = (hits * 100 / shots) if shots else 0.0
        alive = rec.get("died_tick") is None
        if alive:
            return f'<span class="unit-fact" title="攻击 / 命中 / 命中率">攻{shots}·中{hits}·{rate:.0f}%</span>'
        return f'<span class="unit-fact" title="生前攻击 / 命中">阵亡·{shots}/{hits}</span>'

    def ucard(u, color_cls):
        uid = u.get("id", "")
        sid = short_id(uid)
        name = u.get("name") or sid
        act = actions.get(sid) or actions.get(uid, "")
        label = team_label(act)
        stat_line = combat_stat_line(sid)
        safe_name = html.escape(str(name))
        safe_sid = html.escape(str(sid))
        safe_act = html.escape(str(act or "暂无指令"))
        # Same focus attribute as worker cards: click → map jumps to the unit.
        upos = u.get("pos") or []
        focus_attr = (
            f' data-focus-wx="{int(upos[0])}" data-focus-wy="{int(upos[1])}"'
            if len(upos) == 2 else ""
        )
        held = str(name).upper() in holds
        hold_label = "解除驻守" if held else "驻守"
        return (
            f'<div class="unit {color_cls}{" hold" if held else ""}" title="{safe_act}"{focus_attr}><div class="unit-top">'
            f'<div class="unit-id">{safe_name}<span class="count">{safe_sid}</span></div>'
            f'<span class="unit-actions"><span class="badge {color_cls}">{label}</span>'
            f'<button type="button" class="hold-btn{" on" if held else ""}" data-hold-unit="{safe_name}" '
            f'aria-label="{hold_label} {safe_name}" title="{hold_label}（原地驻守，进入射程自动攻击）">{hold_label}</button>'
            f'<button type="button" class="sd-btn" data-sd-unit="{safe_name}" '
            f'aria-label="自裁 {safe_name}" title="自裁">自裁</button></span></div>'
            f'<div class="unit-facts"><span class="unit-locator">{fmt_pos(u.get("pos"))}</span>'
            f'<span class="unit-fact">HP {u.get("hp","?")}</span>{stat_line}</div></div>'
        )

    w_html = "".join(wcard(w) for w in workers) or '<div class="empty">暂无工人</div>'
    vg_html = "".join(ucard(v, "combat") for v in vgs) or '<div class="empty">暂无先锋</div>'
    rg_html = "".join(ucard(r, "combat") for r in rgs) or '<div class="empty">暂无游侠</div>'
    combat_units = collect_combat_units(rec, load_config(CONFIG_PATH))
    wp_workers = [w.get("name") for w in workers if w.get("name")]
    wp_vgs = [v.get("name") for v in vgs if v.get("name")]
    wp_rgs = [r.get("name") for r in rgs if r.get("name")]
    waypoint_html = render_waypoints_panel(waypoints, wp_workers, wp_vgs, wp_rgs)

    if issues:
        items = "".join(
            f'<div class="issue {i["level"]}"><strong>{i["title"]}</strong>'
            f'<span>{i["detail"]}</span></div>'
            for i in issues
        )
        issues_html = (
            '<section class="panel" style="margin-bottom:14px">'
            f'<div class="panel-title">异常告警</div><div class="issues">{items}</div></section>'
        )
    else:
        issues_html = ""

    def _res_chip(p, removable: bool, manual: bool = False):
        x, y = int(p[0]), int(p[1])
        x_btn = (
            f'<button type="button" class="chip-x" data-remove-x="{x}" data-remove-y="{y}" '
            f'aria-label="删除 ({x},{y})" title="删除">×</button>' if removable else ""
        )
        base_cls = "chip removable"
        if manual:
            cls = base_cls + " mem manual"
        elif removable:
            cls = base_cls + " mem"
        else:
            cls = "chip"
        return f'<span class="{cls}">{fmt_pos(p)}{x_btn}</span>'

    vis_chips_html = "".join(_res_chip(p, False) for p in rcells)
    if not vis_chips_html:
        vis_chips_html = '<div class="muted">当前无可见矿点</div>'
    mem_resources = list(mm.get("resources", []) or [])
    manual_set = {tuple(p) for p in (mm.get("manual_resources") or [])}
    mem_auto = [p for p in mem_resources if tuple(p) not in manual_set]
    mem_manual = [p for p in mem_resources if tuple(p) in manual_set]
    auto_chips = "".join(_res_chip(p, True, manual=False) for p in mem_auto)
    manual_chips = "".join(_res_chip(p, True, manual=True) for p in mem_manual)

    res_html = '<div class="res-section">'
    res_html += '<h4>可见矿点</h4><div class="chip-row">' + vis_chips_html + '</div>'
    if mem_auto:
        res_html += f'<h4>记忆矿点 {len(mem_auto)}</h4><div class="chip-row">{auto_chips}</div>'
    if mem_manual:
        res_html += f'<h4>手动录入 <span class="manual-tag">{len(mem_manual)}</span></h4><div class="chip-row">{manual_chips}</div>'
    if not mem_resources:
        res_html += '<h4>记忆矿点</h4><div class="muted">暂无记忆矿点</div>'
    if mem_resources:
        res_html += (
            '<div class="actions" style="margin-top:4px">'
            '<button type="button" id="clearResourceBtn" class="enemy-clear-btn">清除记忆矿点</button>'
            '</div>'
        )
    res_html += (
        '<div class="res-add-form" id="resAddForm">'
        '<div class="row">'
        '<label>X<input id="oreX" name="x" type="number" step="1" placeholder="-30" required></label>'
        '<label>Y<input id="oreY" name="y" type="number" step="1" placeholder="65" required></label>'
        '<button type="button" class="pick-btn ore-pick-btn" id="pickOreBtn" '
        'title="点击地图选择矿点坐标（X 与 Y 一起填入）">⌖ 地图点选</button>'
        '</div>'
        '<div class="actions">'
        '<button type="button" id="oreAddBtn">加入记忆</button>'
        '<button type="button" class="secondary" id="oreCancelBtn">取消</button>'
        '</div>'
        '<div class="msg" id="oreMsg">输入坐标后点加入</div>'
        '</div>'
        '</div>'
    )

    # Enemy sightings — separate card below ore panel
    ex_sightings = mm.get("enemy_sightings", [])
    if ex_sightings:
        ex_chips = "".join(
            f'<span class="chip enemy-chip" title="{html.escape(str(s.get("type") or "ENEMY"))}">'
            f'{_enemy_type_char(s.get("type"))}{fmt_pos(s.get("pos"))}</span>'
            for s in ex_sightings[:30]
        )
        enemy_html = f'<div class="res-section"><h4>敌人踪迹</h4><div class="chip-row">{ex_chips}</div></div>'
    else:
        enemy_html = '<div class="muted">暂无敌人踪迹</div>'

    brand = (
        f"Tick {rec.get('tick')} · 延迟 {rec.get('latency_ms',0):.0f} ms · "
        f"核心 {fmt_pos(rec.get('core_pos'))}"
    )
    status_html = (
        f'<span class="dot"></span><span>{status_text}</span>'
        f'<span style="color:var(--muted)">日志 {age:.0f}s 前</span>'
    )
    hero_html = (
        f'<div class="card"><div class="kicker">核心状态</div>'
        f'<div class="big">{fmt_pos(rec.get("core_pos"))}</div>'
        f'<div class="sub">动作 {rec.get("core_action") or "—"} · 状态 {rec.get("core_state") or "—"} · '
        f'HP {rec.get("core_hp","?")} / Shield {rec.get("core_shield","?")}</div>'
        f'<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px">'
        f'<span class="pill">人口 {rec.get("population",0)}</span>'
        f'<span class="pill">信标 {fmt_pos(rec.get("beacon_pos"))}</span></div></div>'
        f'<div class="card"><div class="kicker">资源</div>'
        f'<div class="big">{resources}<span style="font-size:18px;color:var(--muted)"> / {cap}</span></div>'
        f'<div class="bar"><span style="width:{pct}%"></span></div>'
        f'<div class="sub">进度 {pct}% · 可见矿点 {len(rcells)}</div></div>'
        f'<div class="card"><div class="kicker">战场</div>'
        f'<div class="big">{enemies}</div>'
        f'<div class="sub">可见敌人 · 工人 {len(workers)} · 先锋 {len(vgs)} · 游侠 {len(rgs)}</div>'
        f'<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px">'
        f'<span class="pill">回矿 {stats["cargo"]+stats["deposit"]}</span>'
        f'<span class="pill">探索 {stats["explore"]}</span>'
        f'<span class="pill">等待 {stats["wait"]}</span>'
        f'<span class="pill">挖矿 {stats["harvest"]}</span></div></div>'
    )
    metrics_html = (
        f'<div class="metric"><div class="label">墙记忆</div>'
        f'<div class="value">{mm.get("obstacle_count",0)}</div></div>'
        f'<div class="metric"><div class="label">可见障碍</div>'
        f'<div class="value">{rec.get("obstacle_cells_visible",0)}</div></div>'
        f'<div class="metric"><div class="label">记忆矿点</div>'
        f'<div class="value">{mm.get("resource_count",0)}</div></div>'
        f'<div class="metric"><div class="label">异常告警</div>'
        f'<div class="value">{len(issues)}</div></div>'
    )
    map_title = (
        f"墙 {mm.get('obstacle_count',0)} · 记忆矿 {mm.get('resource_count',0)} · "
        f"可见矿 {len(rcells)}"
    )
    footer_html = (
        f'<div>软刷新 · 不重置视角 · 不抢焦点</div>'
        f'<div>更新于 {time.strftime("%H:%M:%S")} · Tick {rec.get("tick")}</div>'
    )

    log_html, log_count = _battle_log_html(log_limit)


    left_core = _core_rail_html(rec)
    left_res = (
        f'<div class="rail-focus"><div class="rail-focus-main" style="flex:1"><span class="rail-eyebrow">资源库存</span>'
        f'<strong class="rail-value">{resources}<small>/ {cap}</small></strong>'
        f'<div class="rail-progress"><span style="width:{pct}%"></span></div></div>'
        f'<span class="rail-focus-meta">{pct}%</span></div>'
        f'<div class="rail-metric-grid"><div class="rail-metric"><span>可见矿点</span><b>{len(rcells)}</b></div>'
        f'<div class="rail-metric"><span>记忆矿点</span><b>{mm.get("resource_count",0)}</b></div>'
        f'<div class="rail-metric"><span>墙体记忆</span><b>{mm.get("obstacle_count",0)}</b></div>'
        f'<div class="rail-metric"><span>可见墙体</span><b>{rec.get("obstacle_cells_visible",0)}</b></div></div>'
    )
    left_fight = (
        f'<div class="rail-focus"><div class="rail-focus-main"><span class="rail-eyebrow">可见敌人</span>'
        f'<strong class="rail-value">{enemies}</strong></div>'
        f'<span class="rail-focus-meta">我方 {len(workers)+len(vgs)+len(rgs)}</span></div>'
        f'<div class="rail-metric-grid"><div class="rail-metric"><span>工人</span><b>{len(workers)}</b></div>'
        f'<div class="rail-metric"><span>先锋 / 游侠</span><b>{len(vgs)} / {len(rgs)}</b></div></div>'
        f'<div class="rail-activity"><span>回矿 {stats["cargo"]+stats["deposit"]}</span>'
        f'<span>探索 {stats["explore"]}</span><span>等待 {stats["wait"]}</span>'
        f'<span>挖矿 {stats["harvest"]}</span></div>'
    )
    if issues:
        left_issues = "".join(
            f'<div class="issue {i["level"]}"><strong>{i["title"]}</strong><span>{i["detail"]}</span></div>'
            for i in issues[:8]
        )
    else:
        left_issues = '<div class="muted">暂无异常</div>'

    # ── Battle-report panel ─────────────────────────────────────────────
    def _bar(rate: float) -> str:
        pct = min(100, int(rate))
        return (
            f'<div class="mini-bar"><span style="width:{pct}%"></span></div>'
            f'<div class="mini-label">{rate:.0f}%</div>'
        )

    eco = game_stat.get("economy", {}) or {}
    prod = game_stat.get("production", {}) or {}
    comb = game_stat.get("combat", {}) or {}
    prediction = game_stat.get("shot_prediction", {}) or {}
    death = game_stat.get("deaths", {}) or {}
    spawned = prod.get("spawned", {}) or {}
    self_destructed = prod.get("self_destructed", {}) or {}
    label = {"WORKER": "工人", "VANGUARD": "先锋", "RANGER": "游侠"}

    def unit_counts_map(counter: dict) -> str:
        return " · ".join(
            f"{label[t]} {int(counter.get(t, 0) or 0)}" for t in ("WORKER", "VANGUARD", "RANGER")
        )

    def kv_row(k: str, v: str) -> str:
        return f'<div class="kv"><span>{k}</span><b>{v}</b></div>'

    def prediction_group_count(group: str, name: str) -> int:
        groups = prediction.get(group, {}) or {}
        bucket = groups.get(name, {}) or {}
        return int(bucket.get("candidates", 0) or 0)

    vg_rate = derived["vanguard_hit_rate"]
    rg_rate = derived["ranger_hit_rate"]
    report_html = (
        '<section class="panel left-rail-panel report-panel"><div class="panel-title">'
        '<span class="rail-title"><i class="rail-mark">▥</i>战报统计</span>'
        f'<span class="count">累计 {derived["ticks"]} tick</span></div>'
        '<div class="kv"><span>总采集</span>'
        f'<b>{eco.get("harvested_total", 0)} <small>({eco.get("harvest_count", 0)} 次)</small></b></div>'
        '<div class="kv"><span>总卸货</span>'
        f'<b>{eco.get("deposited_total", 0)} <small>({eco.get("deposit_count", 0)} 次)</small></b></div>'
        f'<div class="kv"><span>采集效率</span><b>{derived["harvest_per_tick"]}/tick · 每工人 {derived["harvest_per_worker"]}</b></div>'
        f'<div class="kv"><span>近窗效率</span><b>{derived["window_harvest_per_tick"]}/tick</b></div>'
        f'<div class="kv"><span>采集失败</span><b>{eco.get("harvest_failed", 0)}</b></div>'
        '<div class="stat-chips" style="margin-top:8px">'
        f'<span class="pill">生产 {unit_counts_map(spawned)}</span>'
        f'<span class="pill">自裁 {unit_counts_map(self_destructed)}</span>'
        f'<span class="pill">阵亡 {unit_counts_map(death)}</span>'
        f'<span class="pill">生产失败 {int(prod.get("spawn_failed", 0) or 0)}</span></div>'
        '<div style="margin-top:8px"><div class="kv"><span>先锋</span>'
        f'<b>{comb.get("vanguard_shots", 0)} 攻 / {comb.get("vanguard_hits", 0)} 中</b></div>{_bar(vg_rate)}</div>'
        '<div style="margin-top:8px"><div class="kv"><span>游侠</span>'
        f'<b>{comb.get("ranger_shots", 0)} 攻 / {comb.get("ranger_hits", 0)} 中</b></div>{_bar(rg_rate)}</div>'
        '<div class="stat-chips" style="margin-top:8px">'
        f'<span class="pill">参与击杀 {int(comb.get("kill_participations", 0) or 0)}</span>'
        f'<span class="pill">承伤 {int(comb.get("damage_taken", 0) or 0)}</span>'
        f'<span class="pill">扫描 {int(comb.get("sweeps_resolved", 0) or 0)}</span></div>'
        '<div class="kv" style="margin-top:8px"><span>预判候选</span>'
        f'<b>候选 {int(prediction.get("eligible_candidates", 0) or 0)} / '
        f'{int(prediction.get("candidates", 0) or 0)}</b></div>'
        '<div class="stat-chips">'
        f'<span class="pill">正确 {int(prediction.get("predicted_correct", 0) or 0)}</span>'
        f'<span class="pill">错误 {int(prediction.get("predicted_wrong", 0) or 0)}</span>'
        f'<span class="pill">未知 {int(prediction.get("unknown", 0) or 0)}</span>'
        f'<span class="pill">理论 +{int(prediction.get("improvements", 0) or 0)} '
        f'/ -{int(prediction.get("harms", 0) or 0)}</span></div>'
        '<div class="kv" style="margin-top:8px"><span>真实预判</span>'
        f'<b>{int(prediction.get("lead_fire_attempts", 0) or 0)} 攻 / '
        f'{int(prediction.get("lead_fire_hits", 0) or 0)} 中</b></div>'
        '<div class="stat-chips">'
        f'<span class="pill">未中 {int(prediction.get("lead_fire_misses", 0) or 0)}</span>'
        f'<span class="pill">未知 {int(prediction.get("lead_fire_unknown", 0) or 0)}</span>'
        f'<span class="pill">实际 +{int(prediction.get("lead_fire_improvements", 0) or 0)} '
        f'/ -{int(prediction.get("lead_fire_harms", 0) or 0)}</span></div>'
        '<div class="stat-chips" style="margin-top:8px">'
        f'<span class="pill">静止 {prediction_group_count("by_motion_state", "stationary")}</span>'
        f'<span class="pill">移动观察 '
        f'{prediction_group_count("by_motion_state", "moving_unstable")}</span>'
        f'<span class="pill">稳定移动 '
        f'{prediction_group_count("by_motion_state", "moving_stable")}</span>'
        f'<span class="pill">状态不足 '
        f'{prediction_group_count("by_motion_state", "uncertain") + prediction_group_count("by_motion_state", "insufficient")}</span>'
        f'<span class="pill">旧样本 '
        f'{prediction_group_count("by_motion_state", "legacy")}</span></div>'
        '<div class="kv" style="margin-top:8px"><span>移动</span>'
        f'<b>成功 {eco.get("moves_succeeded", 0)} · 失败 {eco.get("moves_failed", 0)}</b></div>'
        '</section>'
    )

    left_html = (
        f'<section class="panel left-rail-panel core-summary"><div class="panel-title">'
        f'<span class="rail-title"><i class="rail-mark">◆</i>核心 · {html.escape(str(rec.get("core_name") or "C1"))}</span>'
        f'<span class="count">{rec.get("core_state") or "—"}</span></div>{left_core}</section>'
        f'<section class="panel left-rail-panel resource-summary"><div class="panel-title">'
        f'<span class="rail-title"><i class="rail-mark">⬡</i>资源</span><span class="count">采集态势</span></div>{left_res}</section>'
        f'<section class="panel left-rail-panel battle-summary"><div class="panel-title">'
        f'<span class="rail-title"><i class="rail-mark">◎</i>战场</span><span class="count">实时兵力</span></div>{left_fight}</section>'
        f'<section class="panel left-rail-panel issue-summary"><div class="panel-title">'
        f'<span class="rail-title"><i class="rail-mark">!</i>异常</span><span class="count">{len(issues)}</span></div>'
        f'<div class="compact-list">{left_issues}</div></section>'
        + report_html
        + f'<section class="panel left-rail-panel enemy-panel" id="leftEnemyPanel"><div class="panel-title">'
        f'<span class="rail-title"><i class="rail-mark">⌁</i>敌人踪迹</span>'
        f'<span class="count" id="enemyCount">{len(ex_sightings)} 处</span>'
        f'<button type="button" id="clearEnemyBtn" class="enemy-clear-btn">清除</button></div>'
        f'<div id="enemySection">{enemy_html}</div></section>'
    )

    return {
        "tick": rec.get("tick"),
        "leftHtml": left_html,
        "brand": brand,
        "statusHtml": status_html,
        "statusClass": status_cls,
        "heroHtml": hero_html,
        "metricsHtml": metrics_html,
        "issuesHtml": issues_html,
        "workersHtml": w_html,
        "vgHtml": vg_html,
        "rgHtml": rg_html,
        "resHtml": res_html,
        "enemyHtml": enemy_html,
        "waypointHtml": waypoint_html,
        "mapSvg": svg,
        "mapTitle": map_title,
        "hotspotsHtml": hotspots_html,
        "footerHtml": footer_html,
        "workersCount": len(workers),
        "vgCount": len(vgs),
        "rgCount": len(rgs),
        "resCount": len(rcells),
        "enemyCount": f"{len(ex_sightings)} 处",
        "waypointCount": len(waypoints),
        "combatUnits": combat_units,
        "logHtml": log_html,
        "logCount": log_count,
    }


LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><link rel="icon" href="data:,"><title>Arena Hero 战术仪表盘 · 登录</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:#11141a; color:#e8eef7; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",
         "Microsoft YaHei",sans-serif; }
  .login-card { width:min(360px, 92vw); padding:28px 24px; border-radius:12px;
                background:#181d26; border:1px solid #262e3a; box-shadow:0 10px 30px rgba(0,0,0,.35); }
  h1 { margin:0 0 6px; font-size:18px; letter-spacing:.5px; }
  p { margin:0 0 18px; font-size:12.5px; color:#8a94a6; }
  input { width:100%; padding:10px 12px; border-radius:8px; border:1px solid #303a48;
          background:#0f1218; color:#e8eef7; font-size:14px; outline:none; }
  input:focus { border-color:#4a86ff; }
  button { width:100%; margin-top:12px; padding:10px; border:0; border-radius:8px;
           background:#4a86ff; color:#fff; font-size:14px; cursor:pointer; }
  button:hover { background:#3a72e0; }
  .err { margin-top:12px; font-size:12.5px; color:#ff6b6b; min-height:1em; }
</style>
</head>
<body>
  <form class="login-card" id="loginForm" autocomplete="off">
    <h1>🔐 Arena Hero 战术仪表盘</h1>
    <p>请输入访问令牌以继续</p>
    <input id="tokenInput" type="password" placeholder="DASHBOARD_TOKEN" autocomplete="current-password" required>
    <button type="submit">进入</button>
    <div class="err" id="errMsg"></div>
  </form>
<script>
(function(){
  var form = document.getElementById('loginForm');
  var input = document.getElementById('tokenInput');
  var err = document.getElementById('errMsg');
  input.focus();
  form.addEventListener('submit', async function(e){
    e.preventDefault();
    err.textContent = '';
    var btn = form.querySelector('button');
    btn.disabled = true;
    btn.textContent = '验证中…';
    try{
      var res = await fetch('/api/login', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({token: input.value})
      });
      if(res.ok){ location.href = '/'; return; }
      var data = await res.json().catch(function(){ return {}; });
      err.textContent = (data && data.error) || '令牌无效';
      input.value = '';
      input.focus();
    }catch(e){
      err.textContent = '网络错误';
    }finally{
      btn.disabled = false;
      btn.textContent = '进入';
    }
  });
})();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    # ---- token auth -------------------------------------------------------

    def _is_loopback(self) -> bool:
        host = self.client_address[0] if self.client_address else ""
        return host in ("127.0.0.1", "::1", "localhost")

    def _token_ok(self, token: str) -> bool:
        if not DASHBOARD_TOKEN:
            return True  # token not configured -> auth disabled (local dev)
        return bool(token) and hmac.compare_digest(token, DASHBOARD_TOKEN)

    def _client_token(self) -> str:
        # 1) HttpOnly cookie set by /api/login
        cookie = self.headers.get("Cookie")
        if cookie:
            try:
                parsed = SimpleCookie()
                parsed.load(cookie)
                if parsed.get("arena_token"):
                    return parsed["arena_token"].value
            except Exception:
                pass
        # 2) Authorization: Bearer <token>
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        # 3) ?token= query param
        query = urlsplit(self.path).query
        qs = parse_qs(query)
        values = qs.get("token")
        return values[0] if values else ""

    def _authed(self) -> bool:
        return self._is_loopback() or self._token_ok(self._client_token())

    def _send(self, code: int, body: bytes, content_type: str, extra_headers=None):
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            for name, value in extra_headers or ():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # Client disconnected mid-response; nothing to do.
            pass

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        # Silence per-request noise; errors still print via handle_one_request.
        return

    def _read_raw_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or 0)
        return self.rfile.read(min(length, 1_048_576)) if length > 0 else b""

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- arena SPA static hosting (/arena) ---------------------------------

    def _serve_arena_spa(self, path: str):
        if path == "/arena":
            self._redirect("/arena/")
            return
        if not WEB_DIST_DIR.is_dir():
            self._send(404, "arena 页面未构建：请先在 web/ 下执行 npm run build".encode("utf-8"), "text/plain; charset=utf-8")
            return
        dist_root = WEB_DIST_DIR.resolve()
        rel = path[len("/arena/"):].split("?", 1)[0].lstrip("/")
        target = (dist_root / rel).resolve() if rel else dist_root / "index.html"
        # Unknown subpaths fall back to index.html (React Router owns them);
        # the parent check also blocks ../ traversal attempts.
        if target != dist_root / "index.html" and not (target.is_file() and dist_root in target.parents):
            target = dist_root / "index.html"
        try:
            body = target.read_bytes()
        except OSError:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        mime = _ARENA_MIME.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, body, mime)

    # ---- reverse proxy to the game API (injects the server-side key) -------

    def _session_cookies(self) -> str:
        """The browser's game-session cookies (dashboard-owned ones excluded).

        Upstream login sets an HttpOnly session cookie that this proxy
        rewrites to the dashboard origin; its presence means the operator
        logged in, so upstream requests must use the session credential
        (commands land in the MANUAL plan slot) instead of the injected
        API key (AGENT slot, shared with the tactic bot). The CSRF carrier
        cookie is kept out: it is attached separately as X-CSRF-Token.
        """
        raw = self.headers.get("Cookie") if getattr(self, "headers", None) else None
        if not raw:
            return ""
        pairs = []
        for part in raw.split(";"):
            part = part.strip()
            name = part.split("=", 1)[0].strip()
            if not part or name in ("arena_token", _ARENA_CSRF_COOKIE):
                continue
            pairs.append(part)
        return "; ".join(pairs)

    def _csrf_token(self) -> str:
        """CSRF token bound to the imported session (carrier cookie or header).

        Storing it beside the session cookie keeps both in lockstep: a new
        import replaces both, so a stale browser-side token can never poison
        a fresh session.
        """
        raw = self.headers.get("Cookie") if getattr(self, "headers", None) else None
        if raw:
            for part in raw.split(";"):
                name, sep, value = part.strip().partition("=")
                if sep and name.strip() == _ARENA_CSRF_COOKIE:
                    return value.strip()
        return ""

    def _arena_upstream(self, method: str, path: str, body: bytes | None = None,
                        extra_headers: dict | None = None):
        """Forward one request to the game API; return (status, headers, body).

        headers is a list of (name, value) tuples so repeated Set-Cookie
        headers survive. Credential choice: a browser session cookie wins;
        otherwise the server-side API key is injected.
        """
        cookies = self._session_cookies()
        if not cookies and not ARENA_API_KEY:
            return 503, [], json.dumps({"error": "ARENA_HERO_API_KEY 未配置"}, ensure_ascii=False).encode("utf-8")
        url = f"https://{ARENA_API_HOST}{path}"
        headers = {"User-Agent": "arenagame-dashboard/1.0"}
        if cookies:
            headers["Cookie"] = cookies
        else:
            headers["Authorization"] = f"Bearer {ARENA_API_KEY}"
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                return resp.status, list(resp.headers.items()), resp.read()
        except urllib.error.HTTPError as exc:
            try:
                payload = exc.read()
            except Exception:
                payload = b""
            return exc.code, list((exc.headers or {}).items()), payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return 502, [], json.dumps({"error": f"上游不可达: {exc}"}, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _rewrite_set_cookie(value: str) -> str:
        """Rebind an upstream session cookie to this origin.

        The upstream cookie targets api.arenahero.io over HTTPS; the browser
        talks to the dashboard origin (possibly plain HTTP), so Domain and
        Secure are dropped and the attributes are normalized.
        """
        name_value = value.split(";", 1)[0].strip()
        return f"{name_value}; Path=/; HttpOnly; SameSite=Lax"

    _COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
    _COOKIE_VALUE_RE = re.compile(r"^[\x21-\x7e]+$")

    def _import_session(self, raw: bytes):
        """Validate pasted upstream cookies, then bind them to this origin."""
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            data = {}
        cookies_raw = str(data.get("cookies", "")).strip()
        pairs = []
        for part in cookies_raw.split(";"):
            part = part.strip()
            if not part:
                continue
            name, sep, value = part.partition("=")
            name = name.strip()
            if not sep or name == "arena_token" \
                    or not self._COOKIE_NAME_RE.match(name) \
                    or not self._COOKIE_VALUE_RE.match(value):
                self._send_json(400, {"ok": False, "error": "SESSION_IMPORT_INVALID"})
                return
            pairs.append((name, value))
        if not pairs:
            self._send_json(400, {"ok": False, "error": "SESSION_IMPORT_INVALID"})
            return
        cookie_header = "; ".join(f"{n}={v}" for n, v in pairs)
        # The CSRF token is mandatory for session-credential (MANUAL) command
        # POSTs; the upstream only issues it at authentication time, so the
        # operator copies it from the official site's localStorage and we
        # echo it back for the frontend to store.
        csrf = str(data.get("csrf", "")).strip()
        if csrf and not self._COOKIE_VALUE_RE.match(csrf):
            self._send_json(400, {"ok": False, "error": "SESSION_IMPORT_INVALID"})
            return
        # Validate against the game API before storing anything.
        request = urllib.request.Request(
            f"https://{ARENA_API_HOST}/api/v1/me",
            headers={"Cookie": cookie_header, "User-Agent": "arenagame-dashboard/1.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                user_body = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                exc.read()
            except Exception:
                pass
            self._send_json(401, {"ok": False, "error": "SESSION_IMPORT_EXPIRED"})
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._send_json(502, {"ok": False, "error": f"上游不可达: {exc}"})
            return
        set_cookies = [("Set-Cookie", f"{n}={v}; Path=/; HttpOnly; SameSite=Lax") for n, v in pairs]
        if csrf:
            # Bind the CSRF token to the session cookie itself: the proxy
            # attaches it as X-CSRF-Token on session POSTs, so a stale
            # browser-side token can never outlive the session it came from.
            set_cookies.append(("Set-Cookie", f"{_ARENA_CSRF_COOKIE}={csrf}; Path=/; HttpOnly; SameSite=Lax"))
        try:
            user = json.loads(user_body.decode("utf-8"))
        except Exception:
            user = {}
        print(f"[arena-proxy] session import ok for {user.get('username', '?')} "
              f"({len(pairs)} cookie(s), csrf={'yes' if csrf else 'no'})", flush=True)
        self._send(200, json.dumps({"ok": True, "user": user, "csrf_token": csrf}, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra_headers=set_cookies)

    def _forward_upstream_response(self, status, headers, body):
        ctype = "application/json; charset=utf-8"
        set_cookies = []
        for name, value in headers:
            lowered = name.lower()
            if lowered == "content-type":
                ctype = value
            elif lowered == "set-cookie":
                set_cookies.append(("Set-Cookie", self._rewrite_set_cookie(value)))
        self._send(status, body, ctype, extra_headers=set_cookies)

    def _log_proxy(self, method: str, path: str, status: int, body: bytes) -> None:
        # Diagnostic trail for manual-control issues: the upstream error code
        # (CSRF_INVALID, TICK_MISMATCH, ...) tells exactly why a plan failed.
        try:
            snippet = body[:240].decode("utf-8", "replace")
        except Exception:
            snippet = ""
        print(f"[arena-proxy] {method} {path} -> {status} {snippet}", flush=True)

    def _proxy_arena_get(self, path: str):
        status, headers, body = self._arena_upstream("GET", path)
        self._log_proxy("GET", path, status, body)
        self._forward_upstream_response(status, headers, body)

    def _proxy_arena_post(self, path: str, raw: bytes):
        extra = {"Content-Type": self.headers.get("Content-Type", "application/json")}
        idem = self.headers.get("Idempotency-Key")
        if idem:
            extra["Idempotency-Key"] = idem
        # Session credential (imported cookies or email login): upstream
        # validates CSRF on browser Manual requests. The token travels in a
        # proxy-owned cookie so it always matches the current session; an
        # explicit header (fresh email login) wins over it.
        cookies = self._session_cookies()
        if cookies:
            csrf = self.headers.get("X-CSRF-Token") or self._csrf_token()
            if csrf:
                extra["X-CSRF-Token"] = csrf
        else:
            csrf = self.headers.get("X-CSRF-Token")
            if csrf:
                extra["X-CSRF-Token"] = csrf
        print(f"[arena-proxy] POST {path} arrived (session={'yes' if cookies else 'no'}, "
              f"csrf={'yes' if 'X-CSRF-Token' in extra else 'no'}, {len(raw)}B)", flush=True)
        status, headers, body = self._arena_upstream("POST", path, body=raw, extra_headers=extra)
        self._log_proxy("POST", path, status, body)
        self._forward_upstream_response(status, headers, body)

    # ---- WebSocket transparent proxy (/api/v1/game/ws) ----------------------

    def _proxy_game_ws(self):
        """Pipe browser <-> game API. The upstream socket carries the API key;
        frames move byte-for-byte in both directions without being parsed."""
        key = self.headers.get("Sec-WebSocket-Key", "")
        protocol = self.headers.get("Sec-WebSocket-Protocol")
        if not key:
            self._send(400, b"missing Sec-WebSocket-Key", "text/plain; charset=utf-8")
            return
        if not ARENA_API_KEY:
            self._send(503, json.dumps({"error": "ARENA_HERO_API_KEY 未配置"}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        try:
            upstream, leftover, upstream_err = self._ws_upstream_handshake(protocol)
        except OSError as exc:
            self._send(502, json.dumps({"error": f"上游不可达: {exc}"}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        if upstream is None:
            # Non-101 upstream reply (401/409/429 ...): surface its body.
            status, body, ctype = upstream_err
            print(f"[arena-proxy] WS handshake rejected upstream -> {status} "
                  f"{body[:160].decode('utf-8', 'replace')}", flush=True)
            self._send(status, body, ctype)
            return
        print("[arena-proxy] WS open", flush=True)
        # Finish the browser-side handshake with raw bytes, then take over the
        # socket: send_response() would add server log noise on the wire.
        accept = base64.b64encode(hashlib.sha1((key + _WS_ACCEPT_GUID).encode()).digest()).decode()
        try:
            lines = [
                "HTTP/1.1 101 Switching Protocols",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Accept: {accept}",
            ]
            if protocol:
                lines.append(f"Sec-WebSocket-Protocol: {protocol}")
            self.wfile.write(("\r\n".join(lines) + "\r\n\r\n").encode())
            # Frames received while the handshake was in flight (the tick
            # often arrives with the 101) must not be dropped.
            if leftover:
                self.wfile.write(leftover)
            self.wfile.flush()
        except OSError:
            upstream.close()
            return
        self.close_connection = True
        self._pipe_pair(self.connection, upstream)
        print("[arena-proxy] WS closed", flush=True)

    def _ws_upstream_handshake(self, protocol: str | None):
        """Open the upstream WebSocket with the API key. Returns
        (socket, leftover_bytes, None) on 101 — leftover carries frames that
        already arrived and must be flushed to the browser before piping — or
        (None, b"", (status, body, content_type)) for error replies."""
        sock = socket.create_connection((ARENA_API_HOST, 443), timeout=30)
        wrapped = ssl.create_default_context().wrap_socket(sock, server_hostname=ARENA_API_HOST)
        headers = [
            f"GET {_ARENA_WS_PATH} HTTP/1.1",
            f"Host: {ARENA_API_HOST}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}",
            "Sec-WebSocket-Version: 13",
            f"Authorization: Bearer {ARENA_API_KEY}",
        ]
        if protocol:
            headers.append(f"Sec-WebSocket-Protocol: {protocol}")
        wrapped.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
        # Read the response head byte-by-byte: it is small and keeps the
        # stream aligned for the frame pipe that follows a 101.
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 8192:
            chunk = wrapped.recv(1)
            if not chunk:
                break
            buf += chunk
        head, _, leftover = buf.partition(b"\r\n\r\n")
        parts = head.split(b"\r\n", 1)[0].split(None, 2)
        status = int(parts[1]) if len(parts) >= 2 else 502
        if status != 101:
            ctype = "application/json; charset=utf-8"
            for line in head.split(b"\r\n")[1:]:
                if line.lower().startswith(b"content-type:"):
                    ctype = line.split(b":", 1)[1].strip().decode("latin-1")
            body = leftover
            try:
                while len(body) < 65536:
                    chunk = wrapped.recv(4096)
                    if not chunk:
                        break
                    body += chunk
            except OSError:
                pass
            wrapped.close()
            return None, b"", (status, body, ctype)
        return wrapped, leftover, None

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket):
        try:
            while True:
                chunk = src.recv(16384)
                if not chunk:
                    break
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            for sock in (src, dst):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

    def _pipe_pair(self, browser_sock: socket.socket, upstream_sock: socket.socket):
        # Upstream->browser on a helper thread; browser->upstream blocks here
        # until either side closes, then we wait briefly for the mirror pipe.
        helper = threading.Thread(target=self._pipe, args=(upstream_sock, browser_sock), daemon=True)
        helper.start()
        self._pipe(browser_sock, upstream_sock)
        helper.join(timeout=5)

    def _stream_events(self):
        """Server-Sent Events: push the moment the data files change.

        Replaces the old 2 s client poll — a new tick (or map-memory / battle
        log update) shows up on the dashboard as soon as the tactic writes it.
        The client keeps a slow fallback poll in case the stream is blocked.
        """
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-store")
            # Some reverse proxies buffer SSE bodies by default.
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        self.close_connection = True
        try:
            self.wfile.write(b"retry: 2000\n: connected\n\n")
            self.wfile.flush()
            last = _stream_signature()
            idle = 0.0
            while True:
                time.sleep(0.25)
                sig = _stream_signature()
                if sig != last:
                    last = sig
                    self.wfile.write(b"event: tick\ndata: 1\n\n")
                    idle = 0.0
                else:
                    idle += 0.25
                    if idle >= 15.0:
                        # Keep-alive comment: stops proxies from dropping the
                        # idle connection and surfaces dead peers promptly.
                        self.wfile.write(b": ping\n\n")
                        idle = 0.0
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # Client disconnected; nothing to clean up (daemon thread).
            return

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._authed():
            if path == "/" or path == "/arena" or path.startswith("/arena/"):
                self._send(401, LOGIN_HTML.encode("utf-8"), "text/html; charset=utf-8")
            else:
                self._send_json(401, {"ok": False, "error": "未授权：缺少或错误的 DASHBOARD_TOKEN"})
            return
        if path == "/":
            self._redirect("/arena")
            return
        if path == _ARENA_WS_PATH and "websocket" in (self.headers.get("Upgrade") or "").lower():
            self._proxy_game_ws()
            return
        if path == "/arena" or path.startswith("/arena/"):
            self._serve_arena_spa(path)
            return
        if path in _ARENA_GET_PATHS:
            self._proxy_arena_get(path)
            return
        if path == "/api/stream":
            self._stream_events()
            return
        if path == "/api/state":
            parts = build_parts(log_limit=_clamp_log_limit(self))
            if not parts:
                body = json.dumps({"tick": None, "error": "no data"}, ensure_ascii=False).encode("utf-8")
            else:
                body = json.dumps(parts, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/log":
            # Standalone battle-log rows for the current time window, so a
            # window change can fetch more rows immediately without waiting for
            # the next tick (softRefresh skips re-renders while tick is idle).
            log_html, log_count = _battle_log_html(_clamp_log_limit(self))
            self._send_json(200, {"ok": True, "html": log_html, "count": log_count})
            return
        if path == "/api/battle-log":
            # JSON rows for the arena SPA's battle-log panel (the HTML
            # /api/log endpoint stays with the server-rendered dashboard).
            self._send_json(200, {"ok": True, "entries": battle_log_json_entries(_clamp_log_limit(self))})
            return
        if path == "/api/config":
            self._send_json(200, {"ok": True, "config": load_config(CONFIG_PATH)})
            return
        if path == "/api/teams":
            config = load_config(CONFIG_PATH)
            history = read_history(1)
            rec = history[0] if history else {}
            self._send_json(200, {
                "ok": True,
                "config": config,
                "combat_units": collect_combat_units(rec, config),
            })
            return
        if path == "/api/trends":
            query = parse_qs(urlsplit(self.path).query)
            raw = query.get("seconds", query.get("ticks", ["600"]))[0]
            try:
                window = int(raw)
            except (TypeError, ValueError):
                window = 600
            window = max(60, min(21600, window))
            points = _trends_points(window)
            self._send_json(200, {
                "ok": True,
                "window": window,
                "lastTs": points[-1]["t"] if points else None,
                "points": points,
            })
            return
        if path == "/api/waypoints":
            self._send_json(200, {"ok": True, "waypoints": load_waypoints()})
            return
        if path == "/api/squad-targets":
            self._send_json(200, {"ok": True, "targets": load_squad_targets()})
            return
        if path == "/api/holds":
            # Held (驻守) unit display names, for the arena SPA's unit dialog.
            self._send_json(200, {"ok": True, "holds": sorted(load_holds())})
            return
        if path == "/api/enemy-memory":
            # Remembered enemies (last-known positions) from the shared
            # map_memory.json — the same data this dashboard draws as its
            # enemy-trace layer. Enemies currently visible in the latest
            # tick are skipped so the arena page never duplicates markers.
            # Ranked by last-seen tick and capped per type so each filter
            # combination on the arena page can fill its own 21 slots.
            memory = load_map_memory()
            history = read_history(1)
            rec = history[0] if history else {}
            visible = set()
            for enemy in rec.get("enemies", []) or []:
                pos = enemy.get("pos") or []
                if len(pos) == 2:
                    visible.add((int(pos[0]), int(pos[1])))
            parsed = [
                s for s in (
                    _parse_enemy_sighting(item)
                    for item in memory.get("enemy_sightings", []) or []
                )
                if s is not None and s[0] not in visible
            ]
            sightings = [
                {"pos": list(pos), "type": etype, "tick": tick}
                for pos, etype, tick in _rank_enemy_sightings(parsed)
            ]
            self._send_json(200, {"ok": True, "sightings": sightings})
            return
        if path == "/api/unit-names":
            # Tactic display names (W1/V2/R3) keyed by short unit id, so the
            # arena SPA labels units exactly like this dashboard does.
            history = read_history(1)
            rec = history[0] if history else {}
            self._send_json(200, {
                "ok": True,
                "tick": rec.get("tick"),
                "names": unit_display_names(rec),
            })
            return
        if path == "/api/unit-routes":
            # Per-unit destinations + planned routes for the arena SPA's
            # 路径 filter layer — the very data this dashboard's route/target
            # layer draws: the remaining A* path (already trimmed of walked
            # cells) plus the current target cell from the newest tick.
            history = read_history(1)
            rec = history[0] if history else {}
            units = []
            for group, etype in (("workers", "WORKER"),
                                 ("vanguards", "VANGUARD"),
                                 ("rangers", "RANGER")):
                for index, raw in enumerate(rec.get(group) or []):
                    if not isinstance(raw, dict):
                        continue
                    pos = raw.get("pos") or []
                    target = raw.get("target") or []
                    path = _remaining_path(raw)
                    if len(target) != 2 and not path:
                        continue
                    units.append({
                        "name": str(raw.get("name")
                                    or f"{etype[0]}{index + 1}"),
                        "type": etype,
                        "pos": [int(pos[0]), int(pos[1])]
                        if len(pos) == 2 else None,
                        "target": [int(target[0]), int(target[1])]
                        if len(target) == 2 else None,
                        "path": path,
                        "complete": bool(raw.get("path_complete")),
                    })
            self._send_json(200, {
                "ok": True,
                "tick": rec.get("tick"),
                "units": units,
            })
            return
        if path == "/api/guerrilla-tracks":
            # A whole day of guerrilla exploration lines for the arena SPA's
            # 游击轨迹 layer: ?date=YYYYMMDD (default newest), ?hours=N to cut to
            # the newest N hours, ?max_points= to decimate each polyline.
            query = parse_qs(urlsplit(self.path).query)
            wanted_date = (query.get("date") or [""])[0].strip() or None
            try:
                max_points = int((query.get("max_points") or [GUERRILLA_TRACK_MAX_POINTS])[0])
            except (TypeError, ValueError):
                max_points = GUERRILLA_TRACK_MAX_POINTS
            max_points = max(50, min(2000, max_points))
            try:
                hours = int((query.get("hours") or [0])[0]) or None
            except (TypeError, ValueError):
                hours = None
            self._send_json(200, read_guerrilla_tracks(wanted_date, max_points, hours))
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        raw = self._read_raw_body()
        try:
            parsed = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            parsed = {}
        data = parsed if isinstance(parsed, dict) else {}
        if path == "/api/login":
            # Login is the one endpoint that must work without auth.
            token = str(data.get("token", "")).strip()
            if self._token_ok(token):
                self.send_response(200)
                self.send_header("Set-Cookie", f"arena_token={token}; Path=/; HttpOnly; SameSite=Lax")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(b'{"ok":true}')))
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            else:
                self._send_json(401, {"ok": False, "error": "token 错误"})
            return
        if not self._authed():
            if path.startswith("/api/v1/"):
                # A browser POST to the game API that never reaches the
                # proxy shows up as the generic frontend error; log it so the
                # missing-dashboard-token case is visible server-side.
                print(f"[arena-proxy] POST {path} blocked: dashboard auth failed", flush=True)
            self._send_json(401, {"ok": False, "error": "未授权：缺少或错误的 DASHBOARD_TOKEN"})
            return
        if path == "/api/v1/session/import":
            # OAuth-only accounts (LINUX DO / GitHub) cannot log in here
            # directly: the upstream OAuth redirect_uri is fixed to the
            # official site. The operator copies their session cookie there
            # and imports it; we validate it upstream, then rebind it to
            # this origin so later requests flow through the MANUAL slot.
            self._import_session(raw)
            return
        if path in _ARENA_POST_PATHS:
            # Game command plan: forward upstream with the server-side key.
            self._proxy_arena_post(path, raw)
            return
        if path in {"/api/config", "/api/config/reset"}:
            try:
                is_reset = path.endswith("/reset")
                config = (
                    reset_strategy_config(CONFIG_PATH)
                    if is_reset
                    else update_config(data, CONFIG_PATH)
                )
            except ConfigValidationError as exc:
                self._send_json(400, {
                    "ok": False,
                    "error": "配置值无效",
                    "fields": exc.errors,
                })
                return
            except OSError as exc:
                self._send_json(500, {"ok": False, "error": f"保存失败: {exc}"})
                return
            if is_reset:
                append_config_log({}, action="恢复默认配置")
            elif data:
                changed = {key: value for key, value in data.items() if key in config}
                if changed:
                    append_config_log(changed)
            self._send_json(200, {"ok": True, "config": config})
            return

        if path == "/api/teams":
            try:
                updates = {}
                for key in list(TEAM_ROSTER_FIELDS) + list(TEAM_SETTING_FIELDS):
                    if key in data:
                        updates[key] = data[key]
                # Normalize roster text for stable dashboard/tactic display.
                for key in TEAM_ROSTER_FIELDS:
                    if key in updates:
                        updates[key] = _format_roster_names(_parse_roster_names(updates[key]))
                config = update_config(updates, CONFIG_PATH)
            except ConfigValidationError as exc:
                self._send_json(400, {
                    "ok": False,
                    "error": "分队配置无效",
                    "fields": exc.errors,
                })
                return
            except OSError as exc:
                self._send_json(500, {"ok": False, "error": f"保存失败: {exc}"})
                return
            if updates:
                append_config_log(updates, action="分队调整")
            history = read_history(1)
            rec = history[0] if history else {}
            self._send_json(200, {
                "ok": True,
                "config": config,
                "combat_units": collect_combat_units(rec, config),
            })
            return

        if path == "/api/enemy/clear":
            try:
                result = clear_enemy_sightings()
                self._send_json(200, result)
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        if path == "/api/resource/clear":
            try:
                result = clear_remembered_resources()
                self._send_json(200, result)
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        if path == "/api/waypoint/set":
            try:
                name = _waypoint_name(data.get("name"))
                x = int(data.get("x"))
                y = int(data.get("y"))
            except (TypeError, ValueError) as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            x = max(-10000, min(10000, x))
            y = max(-10000, min(10000, y))
            result = set_waypoint(name, x, y, mode=data.get("mode"))
            self._send_json(200 if result.get("ok") else 400, result)
            return

        if path == "/api/waypoint/mode":
            try:
                name = _waypoint_name(data.get("name"))
                mode = _wp_mode(data.get("mode"))
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            result = set_waypoint_mode(name, mode)
            self._send_json(200 if result.get("ok") else 400, result)
            return

        if path == "/api/waypoint/remove":
            try:
                name = _waypoint_name(data.get("name"))
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            index = data.get("index")
            if index is not None and not isinstance(index, bool):
                try:
                    index = int(index)
                except (TypeError, ValueError):
                    index = None
            result = remove_waypoint(name, index=index)
            self._send_json(200 if result.get("ok") else 400, result)
            return

        if path == "/api/waypoint/clear":
            self._send_json(200, clear_waypoints())
            return

        if path == "/api/squad-target/add":
            try:
                squad = _squad_key(data.get("squad"))
                x = int(data.get("x"))
                y = int(data.get("y"))
            except (TypeError, ValueError) as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            x = max(-_SQUAD_TARGET_BOUND, min(_SQUAD_TARGET_BOUND, x))
            y = max(-_SQUAD_TARGET_BOUND, min(_SQUAD_TARGET_BOUND, y))
            result = add_squad_target(squad, x, y)
            self._send_json(200 if result.get("ok") else 400, result)
            return

        if path == "/api/squad-target/remove":
            try:
                squad = _squad_key(data.get("squad"))
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            index = data.get("index")
            if index is not None and not isinstance(index, bool):
                try:
                    index = int(index)
                except (TypeError, ValueError):
                    index = None
            result = remove_squad_target(squad, index=index)
            self._send_json(200 if result.get("ok") else 400, result)
            return

        if path == "/api/squad-target/clear":
            try:
                squad = _squad_key(data.get("squad"))
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            result = clear_squad_targets(squad)
            self._send_json(200, result)
            return

        if path == "/api/unit/self_destruct":
            try:
                name = _waypoint_name(data.get("name"))
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            result = request_self_destruct(name)
            self._send_json(200 if result.get("ok") else 400, result)
            return

        if path == "/api/hold/set":
            try:
                name = _waypoint_name(data.get("name"))
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            result = set_hold(name)
            self._send_json(200 if result.get("ok") else 400, result)
            return

        if path == "/api/hold/clear":
            try:
                name = _waypoint_name(data.get("name"))
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            result = clear_hold(name)
            self._send_json(200 if result.get("ok") else 400, result)
            return

        try:
            x = int(data.get("x"))
            y = int(data.get("y"))
        except Exception:
            body = json.dumps({"ok": False, "error": "x/y 必须是整数"}, ensure_ascii=False).encode("utf-8")
            self._send(400, body, "application/json; charset=utf-8")
            return

        if path == "/api/resource/add":
            result = save_manual_resource(x, y)
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        if path == "/api/resource/remove":
            result = remove_manual_resource(x, y)
            code = 200 if result.get("ok") else 400
            self._send(code, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        pass




def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"[dashboard] http://localhost:{PORT}")
    print("[dashboard] soft refresh /api/state · Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] 已停止")
        srv.server_close()


if __name__ == "__main__":
    main()
