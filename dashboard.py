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


def _parse_enemy_sighting(item) -> tuple[tuple[int, int], str] | None:
    """Normalize one enemy-sighting entry to ((x, y), type).

    Accepts the raw on-disk forms ``[x, y]`` and ``[x, y, "CORE"]`` as well as
    the parsed ``{"pos": ..., "type": ...}`` dicts returned by load_map_memory.
    Unknown/legacy entries default to type "ENEMY".
    """
    if isinstance(item, dict):
        pos = item.get("pos")
        if not pos or len(pos) != 2:
            return None
        return (int(pos[0]), int(pos[1])), str(item.get("type") or "ENEMY").upper()
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        etype = str(item[2]).upper() if len(item) >= 3 and item[2] else "ENEMY"
        return (int(item[0]), int(item[1])), etype
    return None


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
            {"pos": pos, "type": etype} for pos, etype in enemy_sightings
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


# ── manual per-unit self-destruct (「自裁」command, shared with the tactic) ──
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


# Coordinate fields the user can fill by clicking the map. Maps the X field to
# its paired Y field and the DOM id of the map-pick button (rendered on the X row).
COORD_PICKER_ROWS = {
    "core_target_x": ("core_target_y", "pickCoreBtn"),
}


def render_config_panel(workers: int = 0, vanguards: int = 0, rangers: int = 0) -> str:
    config = load_config(CONFIG_PATH)
    schema = config_schema()
    fields_by_group: dict[str, list[dict]] = defaultdict(list)
    for field in schema["fields"]:
        # Every combat field lives on the dedicated teams card. Filtering by
        # schema group prevents newly added combat settings leaking back into
        # this generic form when a hand-maintained allow-list is missed.
        if field["group"] == "combat":
            continue
        if field["group"] == "production":
            continue
        fields_by_group[field["group"]].append(field)

    groups = []
    for group in schema["groups"]:
        rows = []
        for field in fields_by_group.get(group["key"], []):
            key = field["key"]
            value = config[key]
            if field["kind"] == "boolean":
                checked = " checked" if value else ""
                control = (
                    f'<label class="config-switch" for="cfg-{key}">'
                    f'<input id="cfg-{key}" name="{key}" type="checkbox" '
                    f'data-kind="boolean"{checked}><span></span></label>'
                )
            else:
                control = (
                    f'<input id="cfg-{key}" name="{key}" type="number" '
                    f'data-kind="integer" value="{value}" min="{field["minimum"]}" '
                    f'max="{field["maximum"]}" step="{field["step"]}" required>'
                )
                if key in COORD_PICKER_ROWS:
                    control += (
                        f'<button type="button" class="pick-btn" id="{COORD_PICKER_ROWS[key][1]}" '
                        f'title="点击地图选择坐标（X 与 Y 一起填入）">⌖</button>'
                    )
            rows.append(
                '<div class="config-row">'
                f'<label for="cfg-{key}">{field["label"]}</label>{control}</div>'
            )
        if not rows:
            continue
        groups.append(
            '<fieldset class="config-group">'
            f'<legend>{group["label"]}</legend>{"".join(rows)}</fieldset>'
        )

    def target_row(key: str, label: str, target: int, current: int) -> str:
        diff = target - current
        if diff > 0:
            state_cls, state_text = "producing", f"缺 {diff} · 生产中"
        elif diff < 0:
            state_cls, state_text = "ok", f"超编 {-diff} · 停止生产"
        else:
            state_cls, state_text = "ok", "已达标"
        suffix = key.replace("target_", "").capitalize()  # Workers / Vanguards / Rangers
        return (
            f'<div class="production-target">'
            f'<label for="cfg-{key}">{label}</label>'
            f'<input id="cfg-{key}" name="{key}" type="number" data-kind="integer" '
            f'min="0" max="100" step="1" value="{target}" required>'
            f'<span class="production-current">'
            f'当前 <b id="prodCurrent{suffix}">{current}</b>'
            f' / 需求 <b id="prodTarget{suffix}">{target}</b>'
            f'<em id="prodState{suffix}" class="prod-state {state_cls}">{state_text}</em>'
            f'</span></div>'
        )

    target_rows = "".join([
        target_row("target_workers", "工人目标", int(config["target_workers"]), workers),
        target_row("target_vanguards", "先锋目标", int(config["target_vanguards"]), vanguards),
        target_row("target_rangers", "游侠目标", int(config["target_rangers"]), rangers),
    ])

    return (
        '<section class="panel config-panel">'
        '<div class="panel-title"><span>策略配置</span>'
        '<span class="count" id="configState">当前值</span></div>'
        '<form id="tacticConfigForm">'
        # Production targets must live INSIDE the form so "保存配置" submits
        # them via form.querySelectorAll('[name]') and applyConfigValues()
        # re-syncs them on load/save/reset. Being a sibling of the form made
        # every save silently drop the edited worker/vanguard/ranger targets.
        '<section class="production-section" aria-labelledby="productionTargetsTitle">'
        '<div class="production-title"><div><b id="productionTargetsTitle">生产需求目标</b>'
        '<span class="count">低于目标自动补兵 · 达到或超出停止生产 · 阵亡自动补充 · 改后点保存生效</span></div></div>'
        f'<div class="production-targets" id="productionTargets">{target_rows}</div></section>'
        f'<div class="config-groups">{"".join(groups)}</div>'
        '<div class="config-actions">'
        '<button type="submit" id="configSaveBtn">保存配置</button>'
        '<button type="button" class="secondary" id="configResetBtn">恢复默认</button>'
        '<span class="config-message" id="configMessage" aria-live="polite"></span>'
        '</div></form></section>'
    )


def render_teams_panel() -> str:
    config = load_config(CONFIG_PATH)
    columns = []
    for key in TEAM_BOARD_KEYS:
        meta = TEAM_BOARD_META[key]
        columns.append(
            f'<div class="team-column tone-{meta["tone"]}" data-team="{key}">'
            f'<div class="team-column-head"><div><b>{meta["label"]}</b>'
            f'<span>{meta["hint"]}</span></div>'
            f'<em class="team-count" data-team-count="{key}">0</em></div>'
            f'<div class="team-drop" data-team-drop="{key}" tabindex="0">'
            f'<div class="team-empty">拖到这里</div></div></div>'
        )

    def number_field(
        field_id: str,
        key: str,
        label: str,
        minimum: int,
        maximum: int,
        hint: str,
    ) -> str:
        return (
            f'<label class="team-field" data-field-wrap="{key}" for="{field_id}">'
            f'<span>{label}<small>{hint}</small></span>'
            f'<input id="{field_id}" name="{key}" type="number" min="{minimum}" '
            f'max="{maximum}" step="1" value="{config[key]}" required>'
            f'<em class="team-field-error" data-field-error="{key}"></em></label>'
        )

    attack_mode = str(config.get("attack_mode", "coords"))
    mode_checked = {
        "coords": " checked" if attack_mode == "coords" else "",
        "auto": " checked" if attack_mode == "auto" else "",
        "beacon": " checked" if attack_mode == "beacon" else "",
    }
    coords_hidden = "" if attack_mode == "coords" else " hidden"
    auto_hidden = "" if attack_mode == "auto" else " hidden"
    beacon_hidden = "" if attack_mode == "beacon" else " hidden"
    kite_mode = str(config.get("kite_mode", "coords"))
    kite_mode_checked = {
        "coords": " checked" if kite_mode == "coords" else "",
        "auto": " checked" if kite_mode == "auto" else "",
        "beacon": " checked" if kite_mode == "beacon" else "",
    }
    kite_coords_hidden = "" if kite_mode == "coords" else " hidden"
    kite_auto_hidden = "" if kite_mode == "auto" else " hidden"
    kite_beacon_hidden = "" if kite_mode == "beacon" else " hidden"
    lead_checked = " checked" if bool(config["ranger_lead_fire_enabled"]) else ""

    home_settings = "".join([
        number_field("teamHomeRadius", "home_patrol_radius", "巡逻半径", 1, 30, "距核心的日常巡逻范围"),
        number_field("teamHomeEngageRadius", "home_engage_radius", "迎击半径", 0, 30, "0 表示关闭主动迎击"),
        number_field("teamHomeMemory", "home_engage_memory_ticks", "目标记忆", 0, 20, "失去视野后保留的 Tick，0 关闭"),
        number_field("teamHealThreshold", "combat_heal_hp_threshold", "回血阈值", 0, 4, "生命值不高于此值时回城，0 关闭"),
        number_field("teamHealReturnLimit", "combat_heal_return_limit", "回撤名额", 0, 19, "同时回城单位上限，0 不限"),
    ])
    guerrilla_settings = number_field(
        "teamGuerrillaSight",
        "guerrilla_engage_radius",
        "感知半径",
        0,
        30,
        "0 按单位自身视野，各自响应威胁",
    )
    attack_coords = (
        f'<div class="team-attack-fields" data-attack-fields="coords"{coords_hidden}>'
        '<label class="team-field" data-field-wrap="attack_target_x" for="teamAttackX">'
        '<span>目标坐标<small>点击地图按钮可同时填写 X / Y</small></span>'
        '<span class="coord-input">'
        f'<input id="teamAttackX" name="attack_target_x" type="number" min="-1000" max="1000" '
        f'step="1" value="{config["attack_target_x"]}" required aria-label="进攻目标 X">'
        '<span class="coord-divider">,</span>'
        f'<input id="teamAttackY" name="attack_target_y" type="number" min="-1000" max="1000" '
        f'step="1" value="{config["attack_target_y"]}" required aria-label="进攻目标 Y">'
        '<button type="button" class="pick-btn" id="pickAttackBtn" '
        'title="点击地图选择进攻坐标（X 与 Y 一起填入）">⌖</button></span>'
        '<em class="team-field-error" data-field-error="attack_target_x"></em>'
        '<em class="team-field-error" data-field-error="attack_target_y"></em></label></div>'
    )
    attack_auto = (
        f'<div class="team-attack-fields team-field-grid" data-attack-fields="auto"{auto_hidden}>'
        + number_field("teamAutoRadius", "attack_auto_radius", "目标范围", 0, 1000, "距核心的搜索范围，0 不限")
        + number_field("teamRetreatRadius", "attack_retreat_radius", "撤退判定半径", 0, 30, "敌方战力不低于本队时撤离，0 关闭")
        + '</div>'
    )
    attack_beacon = (
        f'<p class="team-mode-note" data-attack-fields="beacon"{beacon_hidden}>'
        '进攻队将以冠军信标为目标，无需额外参数。</p>'
    )
    # 坐标/信标共用：沿途接敌束带。坐标与信标模式都只追"顺路"敌人；
    # N>0 时额外限定只追距本单位 N 格内的顺路敌人，0 = 仅顺路过滤。
    march_leash_field = (
        f'<div class="team-attack-fields team-field-grid" data-attack-fields="coords beacon">'
        + number_field("teamMarchRadius", "attack_march_engage_radius", "沿途接敌半径", 0, 500,
                       "坐标/信标只接战顺路敌人；N>0 时再限定距本单位 N 格内")
        + '</div>'
    )
    kite_coords = (
        f'<div class="team-kite-fields" data-kite-fields="coords"{kite_coords_hidden}>'
        '<label class="team-field" data-field-wrap="kite_target_x" for="teamKiteX">'
        '<span>目标坐标<small>点击地图按钮可同时填写 X / Y</small></span>'
        '<span class="coord-input">'
        f'<input id="teamKiteX" name="kite_target_x" type="number" min="-1000" max="1000" '
        f'step="1" value="{config["kite_target_x"]}" required aria-label="风筝队目标 X">'
        '<span class="coord-divider">,</span>'
        f'<input id="teamKiteY" name="kite_target_y" type="number" min="-1000" max="1000" '
        f'step="1" value="{config["kite_target_y"]}" required aria-label="风筝队目标 Y">'
        '<button type="button" class="pick-btn" id="pickKiteBtn" '
        'title="点击地图选择风筝队坐标（X 与 Y 一起填入）">⌖</button></span>'
        '<em class="team-field-error" data-field-error="kite_target_x"></em>'
        '<em class="team-field-error" data-field-error="kite_target_y"></em></label></div>'
    )
    kite_auto = (
        f'<div class="team-kite-fields team-field-grid" data-kite-fields="auto"{kite_auto_hidden}>'
        + number_field("teamKiteAutoRadius", "kite_auto_radius", "目标范围", 0, 1000,
                       "距核心的搜索范围，0 不限；风筝队没有人数劣势撤退")
        + '</div>'
    )
    kite_beacon = (
        f'<p class="team-mode-note" data-kite-fields="beacon"{kite_beacon_hidden}>'
        '风筝队将以冠军信标为目标，接敌后仍执行避险与预判。</p>'
    )
    kite_march_leash = (
        '<div class="team-kite-fields team-field-grid" data-kite-fields="coords beacon">'
        + number_field("teamKiteMarchRadius", "kite_march_engage_radius", "沿途接敌半径", 0, 500,
                       "坐标/信标只主动处理顺路敌人；危险范围内敌人始终参与避险")
        + '</div>'
    )

    return (
        '<section class="panel teams-panel" id="teamsPanel">'
        '<div class="panel-title"><span>战斗分队</span>'
        '<span class="count" id="teamsState">已同步</span></div>'
        '<form id="teamsForm">'
        '<p class="teams-hint">拖动单位调整编队；虚线单位表示仅存在于配置。新单位默认进入守家队。</p>'
        f'<div class="team-board">{"".join(columns)}</div>'
        '<div class="team-parameter-head"><div><b>战斗参数</b>'
        '<span>按职责归类；无关的进攻参数会自动隐藏</span></div></div>'
        '<div class="team-parameter-top">'
        '<section class="team-setting-group tone-home" aria-labelledby="teamHomeTitle">'
        '<div class="team-setting-title"><b id="teamHomeTitle">守家策略</b><span>巡逻 · 迎击 · 回城</span></div>'
        f'<div class="team-field-grid">{home_settings}</div></section>'
        '<div class="team-parameter-stack">'
        '<section class="team-setting-group tone-guerrilla" aria-labelledby="teamGuerrillaTitle">'
        '<div class="team-setting-title"><b id="teamGuerrillaTitle">游击策略</b><span>独立感知</span></div>'
        f'<div class="team-field-grid">{guerrilla_settings}</div></section>'
        '<section class="team-setting-group tone-common" aria-labelledby="teamCommonTitle">'
        '<div class="team-setting-title"><b id="teamCommonTitle">通用战斗</b><span>全队生效</span></div>'
        '<div class="team-field-grid">'
        '<label class="team-field" data-field-wrap="ranger_attack_range" for="teamRangerRange">'
        '<span>游侠射程<small>游戏规则允许 1–3 格</small></span>'
        f'<select id="teamRangerRange" name="ranger_attack_range">'
        + "".join(
            f'<option value="{n}"{" selected" if int(config["ranger_attack_range"]) == n else ""}>{n} 格</option>'
            for n in (1, 2, 3)
        )
        + '</select><em class="team-field-error" data-field-error="ranger_attack_range"></em></label>'
        '<label class="team-switch-field" data-field-wrap="ranger_lead_fire_enabled" for="teamLeadFire">'
        '<span>移动预判实射<small>对稳定移动目标预测下一格</small></span>'
        f'<input id="teamLeadFire" name="ranger_lead_fire_enabled" type="checkbox"{lead_checked}>'
        '<span class="team-switch-ui" aria-hidden="true"></span>'
        '<em class="team-field-error" data-field-error="ranger_lead_fire_enabled"></em></label>'
        '</div></section></div></div>'
        '<section class="team-setting-group tone-attack team-attack-group" aria-labelledby="teamAttackTitle">'
        '<div class="team-setting-title"><b id="teamAttackTitle">进攻策略</b><span>选择方式后只显示相关参数</span></div>'
        '<fieldset class="team-mode"><legend>进攻方式</legend><div class="team-mode-opts">'
        '<label class="team-radio"><input type="radio" name="attack_mode" value="coords"'
        f'{mode_checked["coords"]}><span>坐标进攻</span></label>'
        '<label class="team-radio"><input type="radio" name="attack_mode" value="auto"'
        f'{mode_checked["auto"]}><span>自动进攻</span></label>'
        '<label class="team-radio"><input type="radio" name="attack_mode" value="beacon"'
        f'{mode_checked["beacon"]}><span>冠军信标</span></label>'
        '</div><em class="team-field-error" data-field-error="attack_mode"></em></fieldset>'
        f'{attack_coords}{attack_auto}{attack_beacon}{march_leash_field}</section>'
        '<section class="team-setting-group tone-kite team-kite-group" aria-labelledby="teamKiteTitle">'
        '<div class="team-setting-title"><b id="teamKiteTitle">风筝策略</b>'
        '<span>无人数撤退 · 躲射程 · 预判攻击格</span></div>'
        '<fieldset class="team-mode"><legend>进攻方式</legend><div class="team-mode-opts">'
        '<label class="team-radio"><input type="radio" name="kite_mode" value="coords"'
        f'{kite_mode_checked["coords"]}><span>坐标进攻</span></label>'
        '<label class="team-radio"><input type="radio" name="kite_mode" value="auto"'
        f'{kite_mode_checked["auto"]}><span>自动进攻</span></label>'
        '<label class="team-radio"><input type="radio" name="kite_mode" value="beacon"'
        f'{kite_mode_checked["beacon"]}><span>冠军信标</span></label>'
        '</div><em class="team-field-error" data-field-error="kite_mode"></em></fieldset>'
        f'{kite_coords}{kite_auto}{kite_beacon}{kite_march_leash}</section>'
        '<div class="teams-footer">'
        '<span class="teams-message" id="teamsMessage" aria-live="polite">修改后统一保存，下个 Tick 生效</span>'
        '<div class="teams-actions">'
        '<button type="button" class="secondary" id="teamsResetBtn" disabled>放弃修改</button>'
        '<button type="submit" id="teamsSaveBtn" disabled>保存修改</button>'
        '</div></div></form>'
        '</section>'
    )


def render_trends_panel() -> str:
    """Static shell for the 历史趋势 panel; charts are filled client-side.

    The SVG figures are rendered from /api/trends by the JS IIFE, so this shell
    is emitted once in generate_html() and never part of the 2s fragment swap.
    """
    def figure(chart: str, title: str) -> str:
        return (
            f'<figure class="trend-figure" data-trend-chart="{chart}">'
            f'<figcaption>{title}</figcaption>'
            '<div class="trend-legend" data-trend-legend></div>'
            '<svg class="trend-svg" data-trend-svg viewBox="0 0 400 160" '
            'role="img" aria-label="' + title + '"></svg>'
            '<div class="trend-tooltip" data-trend-tooltip hidden></div>'
            '</figure>'
        )

    return (
        '<section class="panel trends-panel" id="trendsPanel">'
        '<div class="panel-title"><span>历史趋势</span>'
        '<span class="count" data-trend-range>—</span></div>'
        '<div class="trend-toolbar">'
        '<span class="trend-window-label">窗口</span>'
        + "".join(
            f'<button type="button" class="trend-window-btn{" active" if sec == 600 else ""}" '
            f'data-trend-window="{sec}">{label}</button>'
            for sec, label in ((600, "10分钟"), (1800, "30分钟"), (3600, "1小时"))
        )
        + '<span class="trend-window-hint">最近</span>'
        '</div>'
        '<div class="trend-charts">'
        + figure("res", "资源") + figure("pop", "人口") + figure("enemy", "敌人")
        + '</div>'
        '<details class="trend-details"><summary>数据明细</summary>'
        '<div class="trend-table" data-trend-table></div></details>'
        '</section>'
    )


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


# ---------- page HTML -----------------------------------------------------

CSS = r"""
:root{
 --bg0:#070b16;--bg1:#10182b;--card:rgba(255,255,255,.045);
 --line:rgba(255,255,255,.08);--text:#eef3ff;--muted:#93a0bf;
 --accent:#6ea8ff;--pink:#ff6b9d;--orange:#ff8c42;--green:#57d6a3;--amber:#ffc857;
 --red:#ff6b6b;--purple:#b38cff;--shadow:0 18px 50px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;color:var(--text);
 font-family:"Segoe UI","Microsoft YaHei",sans-serif;
 background:
  radial-gradient(1200px 600px at 10% -10%,rgba(110,168,255,.18),transparent 55%),
  radial-gradient(900px 500px at 100% 0%,rgba(255,107,157,.12),transparent 45%),
  radial-gradient(700px 400px at 70% 100%,rgba(87,214,163,.10),transparent 40%),
  linear-gradient(180deg,#0a1020 0%,#070b16 100%);}
.wrap{max-width:1480px;margin:0 auto;padding:18px 16px 28px}
.topbar{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:18px}
.brand h1{margin:0;font-size:28px;letter-spacing:.3px}
.brand p{margin:6px 0 0;color:var(--muted);font-size:14px}
.status-pill{display:inline-flex;align-items:center;gap:8px;padding:10px 14px;border-radius:999px;
 background:rgba(255,255,255,.05);border:1px solid var(--line);white-space:nowrap;backdrop-filter:blur(10px)}
.dot{width:10px;height:10px;border-radius:50%;box-shadow:0 0 12px currentColor}
.status-pill.ok .dot{background:var(--green);color:var(--green)}
.status-pill.down .dot{background:var(--red);color:var(--red)}
.hero{display:grid;grid-template-columns:1.4fr .9fr .9fr;gap:14px;margin-bottom:14px}
.card,.panel{background:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,.03));
 border:1px solid var(--line);border-radius:20px;box-shadow:var(--shadow);padding:18px;position:relative;overflow:hidden}
.card::before,.panel::before{content:"";position:absolute;inset:0 auto auto 0;width:100%;height:1px;
 background:linear-gradient(90deg,transparent,rgba(255,255,255,.25),transparent)}
.kicker{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:1.2px;margin-bottom:8px}
.big{font-size:34px;font-weight:700;line-height:1.1}
.sub{margin-top:8px;color:var(--muted);font-size:13px}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px}
.metric{padding:16px;border-radius:18px;background:var(--card);border:1px solid var(--line)}
.metric .label{color:var(--muted);font-size:12px;margin-bottom:8px}
.metric .value{font-size:24px;font-weight:700}
.bar{height:8px;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden;margin-top:10px}
.bar>span{display:block;height:100%;background:linear-gradient(90deg,#57d6a3,#6ea8ff)}
.layout{display:grid;grid-template-columns:1.4fr .9fr;gap:14px}
.panel-title{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;font-size:16px;font-weight:700}
.count{color:var(--muted);font-size:13px;font-weight:500}
.unit-grid{display:grid;grid-template-columns:1fr;gap:6px}
.unit{--unit-tone:110,168,255;position:relative;border-radius:12px;padding:9px 10px 8px 13px;
 background:linear-gradient(100deg,rgba(var(--unit-tone),.10),rgba(255,255,255,.025) 58%);
 border:1px solid rgba(255,255,255,.065);overflow:hidden;transition:border-color .15s,background .15s}
.unit::after{content:"";position:absolute;inset:8px auto 8px 0;width:3px;border-radius:0 3px 3px 0;background:rgb(var(--unit-tone));opacity:.8}
.unit:hover{border-color:rgba(var(--unit-tone),.30);background:linear-gradient(100deg,rgba(var(--unit-tone),.14),rgba(255,255,255,.04) 62%)}
.unit.cargo{--unit-tone:87,214,163}
.unit.harvest{--unit-tone:255,200,87}
.unit.deposit{--unit-tone:110,168,255}
.unit.wait{--unit-tone:255,107,107}
.unit.explore{--unit-tone:179,140,255}
.unit.combat{--unit-tone:255,107,157}
.unit-top{display:flex;justify-content:space-between;gap:8px;align-items:center;min-width:0}
.unit-id{min-width:0;font-family:Consolas,monospace;font-size:13px;font-weight:700;display:flex;gap:6px;align-items:baseline}
.unit-id .count{max-width:92px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px;font-weight:400;opacity:.72}
.badge{flex:0 0 auto;font-size:10px;padding:2px 7px;border-radius:999px;border:1px solid transparent;line-height:1.35}
.badge.cargo,.badge.deposit{background:rgba(87,214,163,.15);color:#8ef0c4}
.badge.harvest{background:rgba(255,200,87,.15);color:#ffd98a}
.badge.wait{background:rgba(255,107,107,.15);color:#ff9b9b}
.badge.explore{background:rgba(179,140,255,.15);color:#d0b8ff}
.badge.combat{background:rgba(255,107,157,.15);color:#ff9ec0}
.badge.move,.badge.other{background:rgba(110,168,255,.12);color:#a9c8ff}
.unit-actions{display:flex;align-items:center;gap:6px;flex:0 0 auto}
.sd-btn{appearance:none;border:1px solid rgba(255,107,107,.35);background:rgba(255,80,80,.12);color:#ff9b9b;
 font-size:10px;line-height:1.4;padding:1px 7px;border-radius:999px;cursor:pointer;white-space:nowrap;
 transition:background .12s,border-color .12s}
.sd-btn:hover{background:rgba(255,80,80,.42);color:#fff;border-color:rgba(255,120,120,.65)}
.unit-facts{display:flex;align-items:center;gap:7px;min-width:0;margin-top:5px;color:var(--muted);font-size:10px;line-height:1.35}
.unit-locator{min-width:0;flex:1;color:#c8d4eb;font:10.5px Consolas,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.unit-locator .arrow{padding:0 3px;color:rgb(var(--unit-tone))}
.unit-fact{flex:0 0 auto;white-space:nowrap;color:#afbdd5}
.unit-fact+.unit-fact{padding-left:7px;border-left:1px solid rgba(255,255,255,.09)}
.pill{padding:2px 8px;border-radius:999px;background:rgba(255,255,255,.05)}
.side-stack{display:grid;gap:14px}
.chip-row{display:flex;flex-wrap:wrap;gap:8px}
.chip{padding:6px 10px;border-radius:999px;background:rgba(110,168,255,.12);border:1px solid rgba(110,168,255,.18);
 color:#c7dbff;font-size:12px;font-family:Consolas,monospace}
.chip.mem{background:rgba(255,200,87,.10);border-color:rgba(255,200,87,.18);color:#ffe0a0}
.muted{color:var(--muted);font-size:13px}
.empty{grid-column:1/-1;padding:18px;text-align:center;color:var(--muted);
 border:1px dashed rgba(255,255,255,.1);border-radius:14px}
.issues{display:grid;gap:8px}
.issue{display:grid;gap:4px;padding:12px;border-radius:14px;border:1px solid transparent}
.issue.danger{background:rgba(255,107,107,.10);border-color:rgba(255,107,107,.22)}
.issue.warn{background:rgba(255,200,87,.10);border-color:rgba(255,200,87,.22)}
.issue strong{font-size:13px}
.issue span{color:var(--muted);font-size:12px}

.footer{margin-top:16px;color:var(--muted);font-size:12px;display:flex;justify-content:space-between;gap:12px}
.map-panel .game-map{display:block;background:transparent;cursor:grab;
 touch-action:none;user-select:none;max-width:none;transform-origin:0 0;
 will-change:transform}
.map-panel .game-map.dragging{cursor:grabbing}
.map-stage{position:relative;height:min(52vh,520px);overflow:hidden;border-radius:16px;
 border:1px solid rgba(255,255,255,.06);
 background:radial-gradient(800px 400px at 20% 0%,rgba(110,168,255,.08),transparent 55%),
  radial-gradient(700px 360px at 90% 100%,rgba(255,107,157,.06),transparent 50%),#0b1222}
.map-toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 10px}
.map-toolbar button{appearance:none;border:1px solid rgba(255,255,255,.10);background:rgba(255,255,255,.05);
 color:#eef3ff;border-radius:999px;padding:6px 12px;font-size:12px;cursor:pointer}
.map-toolbar button:hover{background:rgba(110,168,255,.16);border-color:rgba(110,168,255,.28)}
.map-toolbar .hint{color:var(--muted);font-size:12px}
.map-toolbar #zoomLabel{min-width:52px;color:#c7dbff;font-family:Consolas,monospace;font-size:12px}
.map-toolbar .coord-readout{min-width:92px;color:#8ef0c4;font:12px Consolas,monospace;padding:4px 10px;border-radius:999px;background:rgba(87,214,163,.10);border:1px solid rgba(87,214,163,.22)}
.map-stage.picking{cursor:crosshair}
.map-stage.picking .game-map{cursor:crosshair}
.map-legend{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.map-legend button.map-filter{font:inherit;font-size:11px;color:var(--muted);padding:4px 8px;border-radius:999px;
 background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.06);display:inline-flex;align-items:center;gap:6px;
 cursor:pointer;transition:.12s}
.map-legend button.map-filter:hover{border-color:rgba(255,255,255,.2);color:#eef3ff}
.map-legend button.map-filter.off{opacity:.32;text-decoration:line-through;border-color:rgba(255,255,255,.05)}
.map-legend button.map-filter.off .dot{box-shadow:none;filter:grayscale(.95)}
.map-legend button.map-filter-reset{font:inherit;font-size:11px;color:var(--muted);padding:4px 10px;border-radius:999px;
 background:transparent;border:1px dashed rgba(255,255,255,.16);cursor:pointer;transition:.12s}
.map-legend button.map-filter-reset:hover{color:#eef3ff;border-color:rgba(255,255,255,.35)}
/* Category filters: the SVG carries a hide-<cat> class; CSS hides that whole
   category in one pass instead of the client walking every [data-cat] node. */
.game-map.hide-core [data-cat="core"],
.game-map.hide-worker [data-cat="worker"],
.game-map.hide-vanguard [data-cat="vanguard"],
.game-map.hide-ranger [data-cat="ranger"],
.game-map.hide-enemy-worker [data-cat="enemy-worker"],
.game-map.hide-enemy-vanguard [data-cat="enemy-vanguard"],
.game-map.hide-enemy-ranger [data-cat="enemy-ranger"],
.game-map.hide-enemy-core [data-cat="enemy-core"],
.game-map.hide-enemy [data-cat="enemy"],
.game-map.hide-enemy-trace [data-cat="enemy-trace"],
.game-map.hide-wall [data-cat="wall"],
.game-map.hide-ore [data-cat="ore"],
.game-map.hide-ore-mem [data-cat="ore-mem"],
.game-map.hide-route [data-cat="route"],
.game-map.hide-target [data-cat="target"],
.game-map.hide-beacon [data-cat="beacon"],
.game-map.hide-attack-target [data-cat="attack-target"],
.game-map.hide-kite-target [data-cat="kite-target"],
.game-map.hide-core-target [data-cat="core-target"],
.game-map.hide-combat [data-cat="combat"],
.game-map.hide-wp [data-cat="wp"]{display:none}
.map-legend .dot{width:10px;height:10px;border-radius:50%;box-shadow:0 0 8px currentColor}
.map-legend .dot.core{background:#6ea8ff;color:#6ea8ff;border-radius:2px}
.map-legend .dot.worker{background:#8aa4ff;color:#8aa4ff}
.map-legend .dot.vg{background:#ff8c42;color:#ff8c42}
.map-legend .dot.rg{background:#b38cff;color:#b38cff}
.map-legend .dot.enemy{background:#ff6464;color:#ff6464}
.map-legend .dot.enemy-worker{background:#8aa4ff;color:#8aa4ff;box-shadow:0 0 8px rgba(138,164,255,.7)}
.map-legend .dot.enemy-vanguard{background:#ff8c42;color:#ff8c42;box-shadow:0 0 8px rgba(255,140,66,.7)}
.map-legend .dot.enemy-ranger{background:#b38cff;color:#b38cff;box-shadow:0 0 8px rgba(179,140,255,.7)}
.map-legend .dot.enemy-core{background:#ff4964;color:#ff4964;border:1px solid #ffd5dc;box-shadow:0 0 10px rgba(255,73,100,.8)}
.map-legend .dot.enemy-trace{background:#ff6464;color:#ff6464;opacity:.45}
.map-legend .dot.wall{background:#3a455f;color:#7f8eab;border-radius:2px;box-shadow:none;border:1px solid #7f8eab;width:9px;height:9px}
.map-legend .dot.ore{background:#ffc857;color:#ffc857}
.map-legend .dot.ore-mem{background:#c9a227;color:#c9a227;opacity:.8}
.map-legend .dot.beacon{background:#ffc857;color:#ffc857;border-radius:2px;transform:rotate(45deg);width:9px;height:9px}
.map-legend .route-line{width:18px;height:0;border-top:2px solid #63d8ff;box-shadow:none;border-radius:0}
.map-legend .target-ring{width:10px;height:10px;border:2px solid #63d8ff;background:transparent;box-shadow:none}
.map-legend .dot.attack-target{width:10px;height:10px;border:2px dashed #ff8c42;background:transparent;box-shadow:none;border-radius:0}
.map-legend .dot.kite-target{width:10px;height:10px;border:2px dashed #3dd6c9;background:transparent;box-shadow:none;border-radius:0}
.map-legend .dot.core-target{width:10px;height:10px;border:2px dashed #6ea8ff;background:transparent;box-shadow:none;border-radius:0}
.map-legend .dot.wp{width:10px;height:10px;border:2px dashed #3dd6c9;background:transparent;box-shadow:none;border-radius:0}
.map-legend .dot.combat{background:#ff4964;color:#ff4964}

/* Combat hotspot strip: the loudest element on the page while a fight is on.
   Chips are clickable (data-focus-wx/wy → focusWorld); engaged ones pulse. */
.hotspot-bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 10px;
 margin-bottom:10px;border-radius:10px;background:rgba(255,255,255,.028);border:1px solid rgba(255,255,255,.06)}
.hotspot{font:inherit;font-size:12px;line-height:1;padding:6px 11px;border-radius:999px;cursor:pointer;transition:.12s}
.hotspot:hover{filter:brightness(1.25)}
.hotspot.engaged{background:rgba(255,73,100,.16);border:1px solid rgba(255,73,100,.6);color:#ff9db0;
 animation:hotspot-pulse 1.6s ease-in-out infinite}
.hotspot.recent{background:rgba(255,140,66,.13);border:1px solid rgba(255,140,66,.5);color:#ffc39a}
.hotspot.sighted{background:rgba(138,164,255,.09);border:1px solid rgba(138,164,255,.32);color:#b7c4e8}
.hotspot-more{font-size:11px;color:var(--muted)}
@keyframes hotspot-pulse{0%,100%{box-shadow:0 0 0 0 rgba(255,73,100,.45)}50%{box-shadow:0 0 0 7px rgba(255,73,100,0)}}
.hotspot-ring.engaged{animation:ring-pulse 1.5s ease-in-out infinite}
@keyframes ring-pulse{0%,100%{opacity:1}50%{opacity:.3}}
@media (prefers-reduced-motion: reduce){
 .hotspot.engaged,.hotspot-ring.engaged{animation:none}
}

.main-grid{display:grid;grid-template-columns:280px minmax(0,1fr) 320px;gap:14px;align-items:start}
.side-col{display:grid;gap:12px;min-width:0}
.side-col .panel{padding:14px}
.side-col .panel-title{font-size:14px;margin-bottom:10px}
.left-rail-panel{--rail-tone:110,168,255;background:
 radial-gradient(220px 120px at 0 0,rgba(var(--rail-tone),.11),transparent 72%),
 linear-gradient(180deg,rgba(255,255,255,.055),rgba(255,255,255,.025));
 border-color:rgba(var(--rail-tone),.13);border-radius:18px}
.left-rail-panel::after{content:"";position:absolute;inset:14px auto 14px 0;width:3px;border-radius:0 3px 3px 0;
 background:rgb(var(--rail-tone));box-shadow:0 0 14px rgba(var(--rail-tone),.45);opacity:.78}
.left-rail-panel.resource-summary{--rail-tone:87,214,163}
.left-rail-panel.battle-summary{--rail-tone:255,140,66}
.left-rail-panel.issue-summary{--rail-tone:255,107,107}
.left-rail-panel.report-panel{--rail-tone:179,140,255}
.left-rail-panel.enemy-panel{--rail-tone:255,100,100}
.rail-title{display:flex;align-items:center;gap:8px;min-width:0}
.rail-mark{display:grid;place-items:center;width:24px;height:24px;border-radius:8px;
 color:rgb(var(--rail-tone));background:rgba(var(--rail-tone),.12);border:1px solid rgba(var(--rail-tone),.2);
 font:12px Consolas,monospace;line-height:1;box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}
.rail-focus{display:flex;align-items:flex-end;justify-content:space-between;gap:10px;padding:10px 11px;margin-bottom:7px;
 border-radius:12px;background:rgba(0,0,0,.16);border:1px solid rgba(var(--rail-tone),.12)}
.rail-focus-main{display:grid;gap:3px;min-width:0}
.rail-eyebrow{color:var(--muted);font-size:10px;letter-spacing:.35px}
.rail-value{color:#f4f7ff;font:700 19px Consolas,monospace;white-space:nowrap}
.rail-value small{margin-left:4px;color:var(--muted);font-size:11px;font-weight:500}
.rail-focus-meta{flex:0 0 auto;color:rgb(var(--rail-tone));font:11px Consolas,monospace;white-space:nowrap}
.rail-progress{height:5px;border-radius:99px;background:rgba(255,255,255,.07);overflow:hidden;margin-top:4px}
.rail-progress>span{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,rgba(var(--rail-tone),.7),rgb(var(--rail-tone)));
 box-shadow:0 0 10px rgba(var(--rail-tone),.35)}
.rail-rows{display:grid}
.rail-row{display:flex;align-items:center;justify-content:space-between;gap:10px;min-height:29px;padding:5px 2px;
 border-bottom:1px solid rgba(255,255,255,.055);font-size:11px}
.rail-row:last-child{border-bottom:0}
.rail-row span{color:var(--muted)}
.rail-row b{max-width:68%;overflow:hidden;text-overflow:ellipsis;color:#e8eef9;font:600 11px Consolas,monospace;white-space:nowrap}
.rail-metric-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}
.rail-metric{display:grid;gap:3px;padding:8px 9px;border-radius:10px;background:rgba(255,255,255,.025);
 border:1px solid rgba(255,255,255,.055)}
.rail-metric span{color:var(--muted);font-size:10px}
.rail-metric b{color:#eef3ff;font:700 14px Consolas,monospace}
.rail-activity{display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-top:7px}
.rail-activity span{padding:3px 7px;border-radius:7px;background:rgba(var(--rail-tone),.075);color:#b9c5da;font-size:10px}
.rail-vitals{display:grid;gap:7px;margin-bottom:7px;padding:9px 11px;border-radius:12px;background:rgba(0,0,0,.16);border:1px solid rgba(255,255,255,.06)}
.vital{display:flex;align-items:center;gap:8px}
.vital-label{flex:0 0 26px;text-align:center;font:700 10px Consolas,monospace;letter-spacing:.5px;padding:2px 0;border-radius:6px}
.vital-label.hp{color:#57d6a3;background:rgba(87,214,163,.12);box-shadow:inset 0 0 0 1px rgba(87,214,163,.25)}
.vital-label.sh{color:#63d8ff;background:rgba(99,216,255,.12);box-shadow:inset 0 0 0 1px rgba(99,216,255,.25)}
.vital-cells{flex:1;display:flex;gap:3px;min-width:0}
.vital-cells i{flex:1;min-width:0;height:11px;border-radius:3px;background:rgba(255,255,255,.055);box-shadow:inset 0 1px 2px rgba(0,0,0,.35),inset 0 0 0 1px rgba(255,255,255,.07)}
.vital-cells.hp i.on{background:linear-gradient(180deg,#8ef0c4,#4cc48f);box-shadow:inset 0 1px 0 rgba(255,255,255,.4),inset 0 -1px 2px rgba(0,40,20,.35),0 0 8px rgba(87,214,163,.45)}
.vital-cells.sh i.on{background:linear-gradient(180deg,#9fe4ff,#4db8e8);box-shadow:inset 0 1px 0 rgba(255,255,255,.4),inset 0 -1px 2px rgba(0,20,45,.35),0 0 8px rgba(99,216,255,.45)}
.vital-num{flex:0 0 auto;min-width:36px;text-align:right;color:var(--muted);font:600 10px Consolas,monospace}
.report-panel .kv{padding:7px 2px;border:0;border-bottom:1px solid rgba(255,255,255,.055);border-radius:0;background:transparent;font-size:11px}
.report-panel .kv b{font-size:11px;text-align:right}
.report-panel .stat-chips .pill{padding:3px 7px;border-radius:7px;background:rgba(179,140,255,.075);font-size:10px;color:#c5bdd8}
.issue-summary .issue{padding:9px 10px;border-radius:10px}
.enemy-panel .chip{padding:4px 8px;font-size:10px}
.enemy-clear-btn{appearance:none;border:1px solid rgba(255,100,100,.25);border-radius:6px;background:rgba(255,100,100,.08);color:#ffb6b6;font-size:10px;padding:2px 8px;cursor:pointer;transition:.12s}
.enemy-clear-btn:hover{background:rgba(255,100,100,.22);border-color:rgba(255,100,100,.5);color:#fff}
.center-col{display:grid;gap:12px;min-width:0}
.map-panel{margin:0}
.map-panel .map-toolbar{margin-bottom:8px}
.map-legend{margin-top:8px}
.compact-list{display:grid;gap:6px;max-height:52vh;overflow:auto;padding-right:2px}
.compact-list::-webkit-scrollbar{width:6px}
.compact-list::-webkit-scrollbar-thumb{background:rgba(255,255,255,.12);border-radius:99px}
.units-tabs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.unit-tab{appearance:none;font:inherit;font-size:12px;color:var(--muted);padding:6px 12px;border-radius:999px;
 background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);cursor:pointer;
 display:inline-flex;align-items:center;gap:6px;transition:.12s}
.unit-tab:hover{color:#eef3ff;border-color:rgba(255,255,255,.22)}
.unit-tab.active{background:rgba(110,168,255,.16);border-color:rgba(110,168,255,.4);color:#cfe6ff}
.unit-tab .count{font-size:11px}
.unit-tab-pane{display:none}
.unit-tab-pane.active{display:block}
.kv{display:flex;justify-content:space-between;gap:8px;padding:8px 10px;border-radius:12px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);font-size:12px}
.kv b{color:#eef3ff;font-weight:700}
.kv span{color:var(--muted)}
.stat-chips{display:flex;flex-wrap:wrap;gap:6px}
.mini-bar{height:6px;border-radius:99px;background:rgba(255,255,255,.08);overflow:hidden;margin-top:8px}
.mini-bar>span{display:block;height:100%;background:linear-gradient(90deg,#57d6a3,#6ea8ff)}
.mini-label{font-size:10px;color:#8ef0c4;font-family:Consolas,monospace;margin-top:3px;text-align:right}.res-add-form{display:none;gap:8px;margin-top:10px;padding:10px;border-radius:12px;background:rgba(0,0,0,.2);border:1px solid rgba(110,168,255,.22)}.res-add-form.open{display:grid}.res-add-form .row{display:grid;grid-template-columns:1fr 1fr;gap:8px}.res-add-form label{display:grid;gap:4px;font-size:11px;color:var(--muted)}.res-add-form input{width:100%;padding:7px 9px;border-radius:8px;border:1px solid rgba(255,255,255,.10);background:#0b1222;color:#eef3ff;font-family:Consolas,monospace;font-size:12px;outline:none}.res-add-form input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(110,168,255,.14)}.res-add-form .actions{display:flex;gap:6px;flex-wrap:wrap}.res-add-form button{appearance:none;border:1px solid rgba(110,168,255,.35);background:#285b8f;color:#fff;border-radius:999px;padding:6px 11px;font-size:11px;font-weight:700;cursor:pointer}.res-add-form button.secondary{background:transparent;border-color:rgba(255,255,255,.16);color:#c7d1e5}.res-add-form button:hover{border-color:rgba(110,168,255,.55)}.res-add-form .msg{min-height:14px;font-size:11px;color:var(--muted)}.res-add-form .msg.ok{color:#8ef0c4}.res-add-form .msg.err{color:#ff9b9b}.chip.removable{position:relative;display:inline-flex;align-items:center;gap:6px;padding-right:4px}
.chip.mem.manual{background:rgba(255,200,87,.18);border-color:rgba(255,200,87,.35);color:#ffe4a8}
.chip.mem{background:rgba(255,200,87,.10);border-color:rgba(255,200,87,.18);color:#ffe0a0}.chip.enemy-chip{background:rgba(255,100,100,.10);border-color:rgba(255,100,100,.18);color:#ffa8a8}.chip .chip-x{opacity:0;display:inline-grid;place-items:center;width:16px;height:16px;border-radius:50%;background:rgba(255,100,100,.2);color:#ffb6b6;margin-left:4px;cursor:pointer;font-size:10px;line-height:1;border:none;padding:0;transition:opacity .12s,background .12s}.chip .chip-x:hover{background:rgba(255,100,100,.45);color:#fff}.chip.removable:hover .chip-x{opacity:1}.res-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:10px}.res-head .title{display:flex;align-items:baseline;gap:8px}.res-head .add-ore-btn{appearance:none;border:1px solid rgba(110,168,255,.3);width:22px;height:22px;border-radius:50%;background:rgba(110,168,255,.12);color:#c7dbff;cursor:pointer;font-size:14px;line-height:1;padding:0;display:grid;place-items:center;transition:.12s}.res-head .add-ore-btn:hover{background:rgba(110,168,255,.28);border-color:rgba(110,168,255,.6);color:#fff}.res-section{display:grid;gap:8px}.res-section h4{margin:0;font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.8px}.manual-tag{color:#ffd98a;font-size:10px;margin-left:6px}
.config-panel{margin-top:0}
.config-groups{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px 26px}
.config-group{margin:0;padding:0;border:0;min-width:0}
.config-group legend{width:100%;padding:0 0 8px;color:#c7dbff;font-size:13px;font-weight:700;border-bottom:1px solid var(--line)}
.config-row{display:grid;grid-template-columns:minmax(0,1fr) 112px;align-items:center;gap:12px;min-height:42px;border-bottom:1px solid rgba(255,255,255,.05)}
.config-row>label{color:var(--muted);font-size:12px;line-height:1.35}
.config-row>input[type=number]{width:112px;padding:7px 9px;border:1px solid rgba(255,255,255,.12);border-radius:6px;background:#0b1222;color:var(--text);font:13px Consolas,monospace;outline:none}
.config-row>input[type=number]:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(110,168,255,.14)}
.pick-btn{appearance:none;border:1px solid rgba(110,168,255,.35);border-radius:8px;background:rgba(110,168,255,.10);color:#a9c8ff;width:26px;height:26px;font-size:13px;line-height:1;cursor:pointer;display:grid;place-items:center;padding:0;transition:.12s;flex:0 0 auto}
.pick-btn:hover{background:rgba(110,168,255,.28);border-color:rgba(110,168,255,.6);color:#fff}
.pick-btn.active{background:#285b8f;border-color:#6ea8ff;color:#fff;box-shadow:0 0 0 2px rgba(110,168,255,.25)}
.pick-btn:disabled{opacity:.4;cursor:not-allowed}
.team-field .coord-input{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr) 28px;gap:6px;align-items:center;min-width:0}
.team-field .coord-input input{min-width:0;width:100%}
.res-add-form button.ore-pick-btn{grid-column:1/-1;justify-self:start;width:auto;height:28px;padding:0 12px;font-size:11px;font-family:inherit;display:inline-flex;align-items:center;gap:4px;background:rgba(110,168,255,.10);border-color:rgba(110,168,255,.35);color:#a9c8ff}
.res-add-form button.ore-pick-btn:hover{background:rgba(110,168,255,.28);border-color:rgba(110,168,255,.6);color:#fff}
.wp-list{display:flex;flex-direction:column;gap:8px;margin-bottom:10px}
.wp-chip{background:rgba(61,214,201,.10);border-color:rgba(61,214,201,.22);color:#7fe8dd}
.wp-add{display:grid;grid-template-columns:minmax(0,1fr) 52px 52px 28px auto auto;gap:8px;align-items:center;
 padding:9px;border:1px solid rgba(61,214,201,.12);border-radius:var(--radius-block);background:rgba(7,14,29,.38)}
.wp-add select,.wp-add input{width:100%;padding:7px 9px;border:1px solid rgba(255,255,255,.12);border-radius:8px;background:#0b1222;color:var(--text);font:12px Consolas,monospace;outline:none;min-width:0}
.wp-add select:focus,.wp-add input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(110,168,255,.14)}
.wp-add button:not(.pick-btn){border:1px solid rgba(61,214,201,.35);border-radius:999px;padding:7px 11px;background:rgba(61,214,201,.12);color:#bff5ec;font-size:11px;font-weight:700;cursor:pointer;white-space:nowrap}
.wp-add button.secondary{background:transparent;border-color:rgba(255,255,255,.16);color:#c7d1e5}
.wp-add button:not(.pick-btn):hover{border-color:rgba(61,214,201,.6);color:#fff}
.wp-add #wpMode{grid-column:1/3}
.wp-add #wpSetBtn{grid-column:3/5;background:linear-gradient(180deg,#326da8,#285b8f);
 border-color:rgba(110,168,255,.42);color:#fff}
.wp-add #wpClearBtn{grid-column:5/7}
.wp-msg{min-height:15px;margin-top:8px;color:var(--muted);font-size:11px}
.wp-msg.ok{color:#8ef0c4}
.teams-panel{margin-top:0;overflow:hidden;container-type:inline-size}
.teams-hint{margin:-3px 0 12px;color:var(--muted);font-size:11px;line-height:1.5}
.teams-state.dirty{color:#ffe08a;border-color:rgba(255,200,87,.25);background:rgba(255,200,87,.08)}
.teams-state.busy{color:#a9c8ff;border-color:rgba(110,168,255,.25);background:rgba(110,168,255,.08)}
.teams-state.err{color:#ff9b9b;border-color:rgba(255,100,100,.25);background:rgba(255,100,100,.08)}
.team-board{display:grid;grid-template-columns:1fr;gap:10px}
.team-column{min-width:0;border-radius:16px;border:1px solid rgba(255,255,255,.08);background:rgba(255,255,255,.025);overflow:hidden;display:flex;flex-direction:row;align-items:stretch}
.team-column.tone-idle{background:linear-gradient(90deg,rgba(148,163,184,.08),rgba(255,255,255,.02))}
.team-column.tone-home{background:linear-gradient(90deg,rgba(87,214,163,.12),rgba(255,255,255,.02));border-color:rgba(87,214,163,.22)}
.team-column.tone-attack{background:linear-gradient(90deg,rgba(255,107,157,.12),rgba(255,255,255,.02));border-color:rgba(255,107,157,.22)}
.team-column.tone-kite{background:linear-gradient(90deg,rgba(61,214,201,.12),rgba(255,255,255,.02));border-color:rgba(61,214,201,.24)}
.team-column.tone-guerrilla{background:linear-gradient(90deg,rgba(179,140,255,.12),rgba(255,255,255,.02));border-color:rgba(179,140,255,.22)}
.team-column.drag-over{box-shadow:0 0 0 2px rgba(110,168,255,.35) inset}
.team-column-head{display:flex;flex-direction:column;justify-content:center;gap:2px;flex:0 0 108px;padding:10px 14px;border-right:1px solid rgba(255,255,255,.06)}
.team-column-head b{display:block;font-size:13px;color:#eef3ff}
.team-column-head span{display:block;color:var(--muted);font-size:10px;line-height:1.3}
.team-count{width:20px;height:20px;border-radius:999px;display:grid;place-items:center;background:rgba(0,0,0,.22);color:#d7e8ff;font:700 11px Consolas,monospace;font-style:normal}
.team-drop{flex:1;display:flex;flex-direction:row;flex-wrap:wrap;align-items:center;align-content:center;gap:8px;padding:10px 12px;min-height:58px}
.team-empty{padding:8px 14px;border:1px dashed rgba(255,255,255,.12);border-radius:12px;color:#7f8eab;font-size:11px}
.team-chip{position:relative;display:grid;place-items:center;width:34px;height:34px;padding:0;border-radius:12px;background:rgba(8,14,26,.72);border:1px solid rgba(255,255,255,.12);cursor:grab;color:#081018;font:800 12px Consolas,monospace;box-shadow:0 4px 10px rgba(0,0,0,.2)}
.team-chip:active{cursor:grabbing}
.team-chip.dragging{opacity:.45}
.team-chip .glyph{width:100%;height:100%;display:grid;place-items:center;font:800 12px Consolas,monospace;color:#081018;border-radius:10px}
.team-chip .glyph.sm{font-size:9.5px}
.team-chip .glyph.xs{font-size:8px}
.team-chip.kind-VANGUARD .glyph{background:#ff8c42}
.team-chip.kind-RANGER .glyph{background:#b38cff}
.team-chip.kind-COMBAT .glyph{background:#6ea8ff}
.team-chip.ghost{opacity:.5;border-style:dashed}
.team-chip .hpbar{position:absolute;left:50%;bottom:3px;transform:translateX(-50%);display:flex;gap:1px;width:22px;height:3px;pointer-events:none}
.team-chip .hpbar .seg{flex:1;min-width:0;border-radius:1px;background:rgba(0,0,0,.35);box-shadow:inset 0 0 0 1px rgba(255,255,255,.22)}
.team-chip .hpbar .seg.on{background:#57d6a3;box-shadow:inset 0 0 0 1px rgba(0,20,10,.35)}
.team-chip .pulse{position:absolute;right:-2px;top:-2px;width:8px;height:8px;border-radius:50%;background:#57d6a3;box-shadow:0 0 0 3px rgba(87,214,163,.12)}
.team-chip.ghost .pulse{background:#7f8eab;box-shadow:none}
.team-parameter-head{display:flex;align-items:end;justify-content:space-between;gap:12px;margin:18px 0 9px;padding-top:16px;border-top:1px solid var(--line)}
.team-parameter-head>div{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap}
.team-parameter-head b{color:#eef3ff;font-size:14px}
.team-parameter-head span{color:var(--muted);font-size:11px}
.team-parameter-top{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
.team-parameter-stack{display:grid;gap:9px;align-content:start}
.team-setting-group{min-width:0;padding:12px;border:1px solid rgba(255,255,255,.07);border-radius:12px;background:rgba(7,14,29,.34)}
.team-setting-group.tone-home{border-color:rgba(87,214,163,.18);background:linear-gradient(135deg,rgba(87,214,163,.07),rgba(7,14,29,.34) 46%)}
.team-setting-group.tone-attack{border-color:rgba(255,107,157,.18);background:linear-gradient(135deg,rgba(255,107,157,.065),rgba(7,14,29,.34) 42%)}
.team-setting-group.tone-kite{border-color:rgba(61,214,201,.2);background:linear-gradient(135deg,rgba(61,214,201,.07),rgba(7,14,29,.34) 46%)}
.team-setting-group.tone-guerrilla{border-color:rgba(179,140,255,.18);background:linear-gradient(135deg,rgba(179,140,255,.07),rgba(7,14,29,.34) 48%)}
.team-setting-group.tone-common{border-color:rgba(110,168,255,.16)}
.team-setting-title{display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid rgba(255,255,255,.06)}
.team-setting-title b{color:#eef3ff;font-size:12px}
.team-setting-title span{color:var(--muted);font-size:10px;text-align:right}
.team-field-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:8px}
.team-field{display:grid;align-content:start;gap:6px;min-width:0;color:#cbd6e8;font-size:11px}
.team-field>span:first-child{display:grid;gap:2px}
.team-field small,.team-switch-field small{display:block;color:var(--muted);font-size:9.5px;line-height:1.35;font-weight:400}
.team-field input,.team-field select{width:100%;min-width:0;padding:8px 9px;border-radius:8px;border:1px solid rgba(255,255,255,.11);background:#0b1222;color:var(--text);font:12px Consolas,monospace;outline:none}
.team-field select{cursor:pointer}
.team-field select option{background:#0b1222;color:var(--text)}
.team-field input:focus,.team-field select:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(110,168,255,.14)}
.team-field.has-error input,.team-field.has-error select,.team-switch-field.has-error .team-switch-ui,.team-field .field-error-control{border-color:rgba(255,100,100,.7)}
.team-field-error{display:none;color:#ff9b9b;font-size:10px;font-style:normal;line-height:1.35}
.team-field-error:not(:empty){display:block}
.team-switch-field{position:relative;display:grid;grid-template-columns:minmax(0,1fr) 38px;gap:8px;align-items:center;min-width:0;color:#cbd6e8;font-size:11px;cursor:pointer}
.team-switch-field input{position:absolute;opacity:0;pointer-events:none}
.team-switch-ui{display:block;width:38px;height:22px;border-radius:11px;background:#263149;border:1px solid rgba(255,255,255,.12);transition:.16s}
.team-switch-ui::after{content:"";display:block;width:16px;height:16px;margin:2px;border-radius:50%;background:#a9b5cc;transition:.16s}
.team-switch-field input:checked+.team-switch-ui{background:#326c5d;border-color:#57d6a3}
.team-switch-field input:checked+.team-switch-ui::after{transform:translateX(16px);background:#b9f5dc}
.team-switch-field input:focus-visible+.team-switch-ui{box-shadow:0 0 0 2px rgba(110,168,255,.35)}
.team-switch-field .team-field-error{grid-column:1/-1}
.team-attack-group{margin-top:9px}
.team-mode{margin:0 0 10px;padding:0;border:0;min-width:0}
.team-mode legend{margin-bottom:6px;padding:0;color:var(--muted);font-size:10px}
.team-mode-opts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:5px;padding:4px;border-radius:10px;background:rgba(0,0,0,.18);border:1px solid rgba(255,255,255,.055)}
.team-radio{display:block;min-width:0;cursor:pointer}
.team-radio input{position:absolute;opacity:0;pointer-events:none}
.team-radio span{display:block;padding:7px 8px;border-radius:7px;color:var(--muted);font-size:11px;text-align:center;white-space:nowrap;transition:.14s}
.team-radio:hover span{color:#eef3ff;background:rgba(255,255,255,.04)}
.team-radio input:checked+span{color:#fff;background:#285b8f;box-shadow:inset 0 0 0 1px rgba(110,168,255,.38)}
.team-radio input:focus-visible+span{box-shadow:0 0 0 2px rgba(110,168,255,.5)}
.team-attack-fields[hidden],.team-mode-note[hidden]{display:none}
.team-attack-fields{max-width:560px}
.team-mode-note{margin:0;padding:9px 11px;border-radius:8px;background:rgba(110,168,255,.07);color:#b9c8dc;font-size:11px}
.coord-divider{color:var(--muted);font:12px Consolas,monospace}
.teams-footer{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}
.teams-actions{display:flex;gap:7px;flex-wrap:wrap;margin-left:auto}
.teams-actions button{appearance:none;border:1px solid rgba(110,168,255,.35);border-radius:8px;padding:8px 13px;background:#285b8f;color:#fff;font-size:12px;font-weight:700;cursor:pointer}
.teams-actions button.secondary{background:transparent;border-color:rgba(255,255,255,.16);color:#c7d1e5}
.teams-actions button:disabled{opacity:.42;cursor:not-allowed}
.teams-message{min-height:18px;color:var(--muted);font-size:11px}
.teams-message.ok{color:#8ef0c4}.teams-message.err{color:#ff9b9b}
@container (max-width:520px){
  .team-parameter-top{grid-template-columns:1fr}
  .team-field-grid{grid-template-columns:1fr 1fr}
}
@container (max-width:360px){
  .team-column{display:block}
  .team-column-head{border-right:0;border-bottom:1px solid rgba(255,255,255,.06)}
  .team-field-grid{grid-template-columns:1fr}
  .team-mode-opts{grid-template-columns:1fr}
  .team-radio span{text-align:left}
  .teams-actions{width:100%}
  .teams-actions button{flex:1}
}
.config-switch{justify-self:end;position:relative;width:38px;height:22px}
.config-switch input{position:absolute;opacity:0;pointer-events:none}
.config-switch span{display:block;width:38px;height:22px;border-radius:11px;background:#263149;border:1px solid rgba(255,255,255,.12);cursor:pointer;transition:.16s}
.config-switch span::after{content:"";display:block;width:16px;height:16px;margin:2px;border-radius:50%;background:#a9b5cc;transition:.16s}
.config-switch input:checked+span{background:#326c5d;border-color:#57d6a3}
.config-switch input:checked+span::after{transform:translateX(16px);background:#b9f5dc}
.config-switch input:focus-visible+span{box-shadow:0 0 0 2px rgba(110,168,255,.35)}
.config-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:16px;padding-top:14px;border-top:1px solid var(--line)}
.config-actions button{border:1px solid rgba(110,168,255,.35);border-radius:6px;padding:8px 13px;background:#285b8f;color:#fff;font-size:12px;font-weight:700;cursor:pointer}
.config-actions button.secondary{background:transparent;border-color:rgba(255,255,255,.16);color:#c7d1e5}
.config-actions button:disabled{opacity:.55;cursor:wait}
.config-message{min-height:18px;color:var(--muted);font-size:12px;margin-left:auto}
.config-message.ok{color:#8ef0c4}.config-message.err{color:#ff9b9b}
.log-panel{margin-top:14px}
.log-filters{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
.log-filter{appearance:none;font:inherit;font-size:11px;color:var(--muted);padding:3px 11px;border-radius:999px;
 background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);cursor:pointer;transition:.12s}
.log-filter:hover{color:#eef3ff;border-color:rgba(255,255,255,.22)}
.log-filter.on{background:rgba(110,168,255,.16);border-color:rgba(110,168,255,.4);color:#cfe6ff}
.log-filter.off{opacity:.35;text-decoration:line-through}
.log-time-filters{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:8px}
.log-time-btn{appearance:none;font:inherit;font-size:11px;color:var(--muted);padding:3px 11px;border-radius:999px;
 background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);cursor:pointer;transition:.12s}
.log-time-btn:hover{color:#eef3ff;border-color:rgba(255,255,255,.22)}
.log-time-btn.on{background:rgba(110,168,255,.16);border-color:rgba(110,168,255,.4);color:#cfe6ff}
.log-time-custom{display:inline-flex;align-items:center;gap:5px}
.log-time-custom input{width:88px;padding:3px 8px;border:1px solid rgba(255,255,255,.12);border-radius:999px;
 background:#0b1222;color:var(--text);font:11px Consolas,monospace;outline:none}
.log-time-custom input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(110,168,255,.14)}
.log-list{display:grid;gap:2px;max-height:46vh;overflow:auto;padding-right:4px}
.log-row{display:flex;align-items:baseline;gap:8px;padding:4px 8px;border-radius:8px;font-size:11.5px;line-height:1.45}
.log-row:nth-child(odd){background:rgba(255,255,255,.025)}
.log-tick{flex:0 0 132px;color:#7f8eab;font-family:Consolas,monospace;font-size:10.5px;white-space:nowrap}
.log-msg{color:#c7d1e5;word-break:break-word}
.log-row[data-cat="discover"] .log-msg{color:#ffe08a}
.log-row[data-cat="kill"] .log-msg{color:#ff9b9b}
.log-row[data-cat="defeat"] .log-msg{color:#ff7aa9}
.log-row[data-cat="combat"] .log-msg{color:#c9a2ff}
.log-row[data-cat="economy"] .log-msg{color:#8ef0c4}
.log-row[data-cat="config"] .log-msg{color:#6ea8ff}
.log-row[data-cat="warn"] .log-msg{color:#ffc857}
.log-msg .log-coord{color:inherit;border-bottom:1px dashed rgba(255,255,255,.45);cursor:pointer;white-space:nowrap;border-radius:3px;padding:0 1px;transition:background .12s,border-color .12s}
.log-msg .log-coord:hover{background:rgba(110,168,255,.22);border-bottom-color:#6ea8ff}
.production-section{margin-bottom:18px;padding-bottom:18px;border-bottom:1px solid var(--line)}
.production-title{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}
.production-title>div{display:flex;flex-direction:column;gap:2px}
.production-title b{font-size:13px;color:#c7dbff}
.production-title .count{font-size:11px;color:var(--muted)}
.production-targets{display:grid;gap:8px}
.production-target{display:grid;grid-template-columns:64px 96px minmax(0,1fr);gap:10px;align-items:center;min-height:36px}
.production-target>label{color:var(--muted);font-size:12px}
.production-target>input[type=number]{width:96px;padding:7px 9px;border:1px solid rgba(255,255,255,.12);border-radius:6px;background:#0b1222;color:var(--text);font:13px Consolas,monospace;outline:none}
.production-target>input[type=number]:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(110,168,255,.14)}
.production-current{display:flex;align-items:center;gap:6px;flex-wrap:wrap;color:var(--muted);font-size:11px;font-family:Consolas,monospace}
.production-current b{color:#8ef0c4;font-size:13px}
.production-current #prodTargetWorkers,.production-current #prodTargetVanguards,.production-current #prodTargetRangers{color:#ffe08a}
.prod-state{padding:2px 8px;border-radius:999px;font-size:10px;font-style:normal;font-family:"Segoe UI","Microsoft YaHei",sans-serif}
.prod-state.producing{background:rgba(87,214,163,.15);color:#8ef0c4}
.prod-state.ok{background:rgba(110,168,255,.12);color:#a9c8ff}
.hero{display:none}
.metrics{display:none}
.layout{display:none}
@media (max-width:1100px){
  .main-grid{grid-template-columns:1fr}
  .compact-list{max-height:none}
  .map-stage{height:min(48vh,460px)}
  .hero{display:grid}
  .metrics{display:grid}
}
@media (max-width:980px){
  .topbar{flex-direction:column}
}
@media (max-width:1100px){
  .team-column-head{flex:0 0 88px;padding:8px 10px}
}
@media (max-width:680px){
  .config-groups{grid-template-columns:1fr}
  .config-message{width:100%;margin-left:0}
  .production-targets{grid-template-columns:1fr}
}

/* ── 历史趋势 panel ──────────────────────────────────────────────── */
.trends-panel{position:relative}
.trend-toolbar{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.trend-window-label,.trend-window-hint{color:var(--muted);font-size:12px}
.trend-window-hint{margin-left:2px}
.trend-window-btn{appearance:none;font:inherit;font-size:12px;color:var(--muted);padding:5px 11px;
 border-radius:999px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
 cursor:pointer;transition:.12s;font-family:Consolas,monospace}
.trend-window-btn:hover{color:#eef3ff;border-color:rgba(255,255,255,.22)}
.trend-window-btn.active{background:rgba(110,168,255,.16);border-color:rgba(110,168,255,.4);color:#cfe6ff}
.trend-charts{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.trend-figure{position:relative;margin:0;padding:12px;border-radius:16px;
 background:rgba(0,0,0,.18);border:1px solid rgba(255,255,255,.06)}
.trend-figure figcaption{font-size:13px;font-weight:700;margin-bottom:8px}
.trend-svg{display:block;width:100%;height:auto;background:#0b1222;
 border:1px solid rgba(255,255,255,.05);border-radius:10px;touch-action:manipulation}
.trend-svg .t-grid{stroke:rgba(255,255,255,.06);stroke-width:1}
.trend-svg .t-line{fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.trend-svg .t-line.dash{stroke-dasharray:4 3;stroke-width:1.5}
.trend-svg .t-crosshair{stroke:rgba(255,255,255,.45);stroke-width:1;pointer-events:none}
.trend-legend{display:flex;flex-wrap:wrap;gap:6px 10px;margin-bottom:8px;min-height:16px}
.trend-legend .t-key{display:inline-flex;align-items:center;gap:5px;color:var(--muted);font-size:11px}
.trend-legend .t-key i{width:12px;height:3px;border-radius:2px;display:inline-block}
.trend-tooltip{position:absolute;z-index:5;min-width:132px;max-width:210px;padding:8px 10px;
 border-radius:10px;background:rgba(7,11,22,.92);border:1px solid rgba(255,255,255,.14);
 box-shadow:0 8px 24px rgba(0,0,0,.5);font-size:11px;color:var(--text);pointer-events:none;
 transform:translate(-50%,-110%);white-space:nowrap}
.trend-tooltip .tt-tick{color:var(--muted);font-family:Consolas,monospace;margin-bottom:4px}
.trend-tooltip .tt-row{display:flex;align-items:center;gap:6px;justify-content:space-between}
.trend-tooltip .tt-row i{width:10px;height:3px;border-radius:2px;display:inline-block;flex:none}
.trend-tooltip .tt-row b{font-weight:700}
.trend-tooltip .tt-row span{color:var(--muted)}
.trend-details{margin-top:12px}
.trend-details summary{cursor:pointer;color:var(--muted);font-size:12px;user-select:none}
.trend-details summary:hover{color:#eef3ff}
.trend-table{max-height:220px;overflow:auto;margin-top:8px;border:1px solid rgba(255,255,255,.06);
 border-radius:10px;background:rgba(0,0,0,.18)}
.trend-table table{width:100%;border-collapse:collapse;font-size:12px}
.trend-table th,.trend-table td{padding:5px 10px;text-align:right;border-bottom:1px solid rgba(255,255,255,.05)}
.trend-table th:first-child,.trend-table td:first-child{text-align:left}
.trend-table th{position:sticky;top:0;background:#10182b;color:var(--muted);font-weight:600}
.trend-table td{color:#d7e1f7;font-family:Consolas,monospace}
.trend-table tr:hover td{background:rgba(110,168,255,.08)}
.trend-empty{padding:22px;text-align:center;color:var(--muted);font-size:13px}
@media (max-width:1100px){
  .trend-charts{grid-template-columns:1fr}
}
@media (max-width:680px){
  .trend-charts{grid-template-columns:1fr}
}

/* ── Unified dashboard visual system ─────────────────────────────── */
:root{
 --surface-soft:rgba(255,255,255,.028);--surface-raised:rgba(255,255,255,.052);
 --control-bg:rgba(7,14,29,.72);--control-line:rgba(255,255,255,.105);
 --radius-panel:18px;--radius-block:12px;--radius-control:9px;
}
body{background:
 radial-gradient(1000px 520px at 8% -8%,rgba(83,139,235,.16),transparent 58%),
 radial-gradient(760px 460px at 96% 0%,rgba(179,140,255,.105),transparent 52%),
 radial-gradient(720px 420px at 72% 100%,rgba(61,214,201,.06),transparent 48%),
 linear-gradient(180deg,#09101e 0%,#070b15 62%,#060a13 100%)}
.wrap{padding-top:20px}
.topbar{align-items:center;margin-bottom:20px;padding:0 2px}
.brand h1{font-size:27px;letter-spacing:.15px;text-shadow:0 4px 24px rgba(110,168,255,.12)}
.brand h1::after{content:"";display:block;width:38px;height:3px;margin-top:8px;border-radius:99px;
 background:linear-gradient(90deg,var(--accent),var(--green));box-shadow:0 0 14px rgba(110,168,255,.35)}
.brand p{margin-top:7px;font-size:12px;letter-spacing:.15px}
.status-pill{min-height:38px;padding:8px 13px;border-radius:12px;background:rgba(12,20,37,.76);
 border-color:rgba(255,255,255,.095);box-shadow:0 10px 30px rgba(0,0,0,.18)}
.card,.panel{border-radius:var(--radius-panel);border-color:rgba(255,255,255,.085);
 background:linear-gradient(180deg,rgba(255,255,255,.052),rgba(255,255,255,.026));
 box-shadow:0 14px 38px rgba(0,0,0,.25)}
.card::before,.panel::before{background:linear-gradient(90deg,transparent,rgba(255,255,255,.18),transparent)}
.panel-title{min-height:26px;margin-bottom:14px;font-size:15px;letter-spacing:.1px}
.panel-title>.count{padding:3px 7px;border-radius:7px;background:rgba(255,255,255,.035);
 border:1px solid rgba(255,255,255,.055);font-size:11px}
.left-rail-panel .panel-title>.count{padding:0;background:transparent;border:0}
.map-panel,.teams-panel,.config-panel,.trends-panel,.log-panel,
.units-panel,.waypoint-panel,.res-panel{--panel-tone:110,168,255;background:
 radial-gradient(300px 150px at 0 0,rgba(var(--panel-tone),.065),transparent 72%),
 linear-gradient(180deg,rgba(255,255,255,.052),rgba(255,255,255,.026));
 border-color:rgba(var(--panel-tone),.14)}
.teams-panel{--panel-tone:179,140,255}
.config-panel{--panel-tone:87,214,163}
.trends-panel{--panel-tone:255,200,87}
.log-panel{--panel-tone:255,107,157}
.units-panel{--panel-tone:255,107,157}
.waypoint-panel{--panel-tone:61,214,201}
.res-panel{--panel-tone:255,200,87}
.map-panel>.panel-title>span:first-child,.teams-panel>.panel-title>span:first-child,
.config-panel>.panel-title>span:first-child,.trends-panel>.panel-title>span:first-child,
.log-panel>.panel-title>span:first-child,.waypoint-panel>.panel-title>span:first-child,
.res-panel .panel-title>span:first-child{display:flex;align-items:center;gap:8px}
.map-panel>.panel-title>span:first-child::before,.teams-panel>.panel-title>span:first-child::before,
.config-panel>.panel-title>span:first-child::before,.trends-panel>.panel-title>span:first-child::before,
.log-panel>.panel-title>span:first-child::before,.waypoint-panel>.panel-title>span:first-child::before,
.res-panel .panel-title>span:first-child::before{content:"";width:3px;height:15px;border-radius:3px;
 background:rgb(var(--panel-tone));box-shadow:0 0 10px rgba(var(--panel-tone),.42)}
.map-stage{border-radius:var(--radius-block);border-color:rgba(110,168,255,.11);background:
 radial-gradient(800px 400px at 20% 0%,rgba(110,168,255,.065),transparent 55%),
 radial-gradient(700px 360px at 90% 100%,rgba(179,140,255,.05),transparent 50%),#091225}
.map-toolbar{gap:6px}
.map-toolbar button,.teams-actions button,.config-actions button,
.wp-add button:not(.pick-btn),.res-add-form button,.trend-window-btn{
 min-height:30px;border-radius:var(--radius-control);border-color:var(--control-line);
 background:var(--surface-raised);color:#cbd7eb;box-shadow:inset 0 1px 0 rgba(255,255,255,.035)}
.map-toolbar button:hover,.teams-actions button:hover,.config-actions button:hover,
.wp-add button:not(.pick-btn):hover,.res-add-form button:hover,.trend-window-btn:hover{
 color:#fff;border-color:rgba(110,168,255,.38);background:rgba(110,168,255,.12)}
.teams-actions button:not(.secondary),.config-actions button:not(.secondary),
.wp-add button:not(.pick-btn):first-of-type,.res-add-form button:not(.secondary){
 background:linear-gradient(180deg,#326da8,#285b8f);border-color:rgba(110,168,255,.42);color:#fff}
.map-toolbar #zoomLabel{padding:0 5px}
.map-toolbar .coord-readout{border-radius:8px;background:rgba(61,214,201,.075)}
.map-legend{gap:6px}
.map-legend button.map-filter,.log-filter,.unit-tab,.trend-window-btn{box-shadow:inset 0 1px 0 rgba(255,255,255,.025)}
.map-legend button.map-filter{padding:4px 8px;background:rgba(255,255,255,.025);border-color:rgba(255,255,255,.055)}
.team-board{gap:8px}
.team-column{border-radius:var(--radius-block);background:rgba(7,14,29,.34);border-color:rgba(255,255,255,.065)}
.team-column-head{background:rgba(255,255,255,.018)}
.team-field input,.team-field select,.wp-add input,.wp-add select,.res-add-form input,
.config-row>input[type=number],.production-target>input[type=number]{
 border-radius:var(--radius-control);background:var(--control-bg);border-color:var(--control-line);
 box-shadow:inset 0 1px 4px rgba(0,0,0,.22)}
.team-field input:focus,.team-field select:focus,.wp-add input:focus,.wp-add select:focus,
.res-add-form input:focus,.config-row>input[type=number]:focus,.production-target>input[type=number]:focus{
 border-color:rgba(110,168,255,.5);box-shadow:0 0 0 2px rgba(110,168,255,.11)}
.production-section{margin-bottom:16px;padding-bottom:16px}
.production-title b,.config-group legend{color:#dbe6f8}
.config-group legend{display:flex;align-items:center;gap:7px;padding-bottom:9px}
.config-group legend::before{content:"";width:6px;height:6px;border-radius:2px;background:rgba(87,214,163,.75);
 box-shadow:0 0 8px rgba(87,214,163,.3)}
.config-row{min-height:41px;transition:background .12s}
.config-row:hover{background:rgba(255,255,255,.015)}
.config-actions{margin-top:14px}
.trend-toolbar{margin-bottom:10px}
.trend-figure{padding:10px;border-radius:var(--radius-block);background:rgba(7,14,29,.32);
 border-color:rgba(255,200,87,.075)}
.trend-svg{border-radius:9px;background:rgba(7,14,29,.78);border-color:rgba(255,255,255,.045)}
.trend-details{padding-top:1px}
.log-panel{margin-top:0}
.log-filters{gap:5px;margin-bottom:10px}
.log-filter{padding:4px 10px;background:rgba(255,255,255,.025)}
.log-filter.on{background:rgba(255,107,157,.105);border-color:rgba(255,107,157,.24);color:#ffd2e2}
.log-row{border-radius:7px}
.units-tabs{gap:5px;margin-bottom:10px;padding-bottom:9px;border-bottom:1px solid rgba(255,255,255,.055)}
.unit-tab{padding:5px 10px}
.unit-tab.active{background:rgba(255,107,157,.11);border-color:rgba(255,107,157,.28);color:#ffd0df}
.waypoint-panel .muted{line-height:1.5}
.wp-entry{border:1px solid rgba(61,214,201,.14);border-radius:10px;background:rgba(7,14,29,.30);
 padding:6px 8px}
.wp-entry-head{display:flex;align-items:center;gap:6px;margin-bottom:4px}
.wp-unit-name{font-weight:700;color:#bff5ec;font-size:11px}
.wp-mode-btn{appearance:none;border:1px solid rgba(61,214,201,.35);border-radius:999px;
 background:rgba(61,214,201,.12);color:#bff5ec;font-size:10px;padding:2px 8px;cursor:pointer;line-height:1.4}
.wp-mode-btn:hover{border-color:rgba(61,214,201,.6);color:#fff}
.wp-entry-head .chip-x{margin-left:auto;opacity:1;width:16px;height:16px;font-size:11px}
.wp-targets{display:flex;flex-wrap:wrap;gap:4px}
.wp-target.chip{padding:3px 8px;font-size:10px;color:#7fe8dd;background:rgba(61,214,201,.10);
 border-color:rgba(61,214,201,.22)}
.wp-target.chip .chip-x{width:14px;height:14px;font-size:11px;line-height:1}
.res-head{margin-bottom:12px}
.res-head .add-ore-btn{width:26px;height:26px;border-radius:8px;background:rgba(255,200,87,.08);
 border-color:rgba(255,200,87,.22);color:#ffe1a1}
.res-section h4{display:flex;align-items:center;gap:6px;letter-spacing:.45px;text-transform:none;color:#aab7cc}
.res-panel #resSection{max-height:430px;overflow:auto;padding-right:3px}
.res-panel #resSection::-webkit-scrollbar,.trend-table::-webkit-scrollbar,.log-list::-webkit-scrollbar{width:6px}
.res-panel #resSection::-webkit-scrollbar-thumb,.trend-table::-webkit-scrollbar-thumb,.log-list::-webkit-scrollbar-thumb{
 background:rgba(255,255,255,.11);border-radius:99px}
.res-panel .chip{padding:5px 9px;border-radius:8px;background:rgba(255,200,87,.07);border-color:rgba(255,200,87,.13)}
.res-panel .chip:not(.mem){background:rgba(110,168,255,.075);border-color:rgba(110,168,255,.13);color:#c9dafe}
.footer{margin-top:18px;padding-top:12px;border-top:1px solid rgba(255,255,255,.055)}
button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid rgba(110,168,255,.65);outline-offset:2px}
@media (max-width:1100px){
  .res-panel #resSection{max-height:none}
  .left-rail-panel{max-width:none}
}
@media (max-width:680px){
  .wrap{padding:14px 10px 22px}
  .brand h1{font-size:23px}
  .card,.panel{border-radius:15px}
  .panel{padding:14px}
  .status-pill{width:100%;justify-content:center}
}

"""

JS = r"""
<script>
(function(){
  const KEY = 'arenaMapView.v3';
  let stage = document.getElementById('mapStage');
  let svg = document.getElementById('gameMap');
  let view = null;
  let drag = false, lx = 0, ly = 0;
  let lastTick = null;
  let lastMapSvg = null;
  let refreshing = false;
  let configDirty = false;
  let teamsDirty = false;
  let teamsBusy = false;
  let teamsUnits = [];
  let teamsConfig = null;
  let dragUnitName = null;
  let pickMode = null;
  let downX = 0, downY = 0, moved = false;
  const MAP_FILTER_KEY = 'arenaMapFilters.v1';
  let mapFilters = null;
  const MAP_CATS = ['core','worker','vanguard','ranger',
                    'enemy-worker','enemy-vanguard','enemy-ranger','enemy-core','enemy',
                    'enemy-trace','wall','ore','ore-mem','route','target','beacon',
                    'attack-target','core-target','combat','wp'];
  const LOG_FILTER_KEY = 'arenaLogFilters.v1';
  let logFilters = null;
  const LOG_CATS = ['discover','kill','defeat','combat','economy','config','warn'];
  // Noisy categories default off so the panel starts readable.
  const LOG_CATS_DEFAULT_OFF = ['combat','economy'];
  // Battle-log time window: seconds into the past, or 'all' for every row.
  const LOG_WINDOW_KEY = 'arenaLogWindow.v1';
  let logWindow = 'all';
  const LOG_WINDOWS = {600: '10分钟', 1800: '30分钟', 3600: '1小时', 21600: '6小时', all: '全部'};

  // 历史趋势 panel: time-window (seconds) persisted, deduped on the newest
  // point's wall-clock ts like softRefresh.
  const TREND_WINDOW_KEY = 'arenaTrendWindow.v2';
  let trendWindow = 600;
  let lastTrendTs = null;
  let trendPoints = [];
  const TREND_WINDOWS = [600, 1800, 3600];
  const TREND_CHART_STATE = {};
  const TREND_CHARTS = {
    res:   { keys: ['r', 'c'], label: '资源' },
    pop:   { keys: ['w', 'v', 'g'], label: '人口' },
    enemy: { keys: ['e'], label: '敌人' },
  };
  // Entity colors match the map (same entity reads as the same color):
  // resources gold, muted capacity, worker blue, vanguard orange, ranger
  // purple, enemy red. See render_svg().
  const TREND_SERIES = {
    r:     { key: 'r', label: '资源',  color: '#ffc857' },
    c:     { key: 'c', label: '容量',  color: '#93a0bf', dash: true },
    w:     { key: 'w', label: '工人',  color: '#8aa4ff' },
    v:     { key: 'v', label: '先锋',  color: '#ff8c42' },
    g:     { key: 'g', label: '游侠',  color: '#b38cff' },
    e:     { key: 'e', label: '敌人',  color: '#ff6464' },
  };

  const PICK_TARGETS = {
    attack: {xId: 'teamAttackX', yId: 'teamAttackY'},
    kite:   {xId: 'teamKiteX', yId: 'teamKiteY'},
    core:   {xId: 'cfg-core_target_x', yId: 'cfg-core_target_y'},
    ore:    {xId: 'oreX', yId: 'oreY'},
    wp:     {xId: 'wpX', yId: 'wpY'},
  };
  const PICK_BUTTONS = {
    pickAttackBtn: 'attack',
    pickKiteBtn:   'kite',
    pickCoreBtn:   'core',
    pickOreBtn:    'ore',
    pickWpBtn:     'wp',
  };
  const PICK_LABELS = {
    attack: '进攻目标', kite: '风筝队目标', core: '核心目标', ore: '矿点', wp: '手动目标',
  };

  function clamp(v, lo, hi){ return Math.max(lo, Math.min(hi, v)); }

  function readSvgMeta(el){
    if(!el) return null;
    return {
      width: Number(el.dataset.width || el.getAttribute('width') || 800),
      height: Number(el.dataset.height || el.getAttribute('height') || 600),
      xmin: Number(el.dataset.xmin || 0),
      xmax: Number(el.dataset.xmax || 0),
      ymin: Number(el.dataset.ymin || 0),
      ymax: Number(el.dataset.ymax || 0),
      cell: Number(el.dataset.cell || 16),
      pad: Number(el.dataset.pad || 24),
      focusX: Number(el.dataset.focusX || 0),
      focusY: Number(el.dataset.focusY || 0)
    };
  }

  function worldToSvg(meta, wx, wy){
    const px = meta.pad + (wx - meta.xmin) * meta.cell + meta.cell / 2;
    // Match server/SVG: smaller world-Y is toward the top of the map.
    const py = meta.pad + (wy - meta.ymin) * meta.cell + meta.cell / 2;
    return [px, py];
  }

  function svgToWorld(meta, px, py){
    const wx = meta.xmin + (px - meta.pad) / meta.cell - 0.5;
    const wy = meta.ymin + (py - meta.pad) / meta.cell - 0.5;
    return [wx, wy];
  }

  function loadView(){
    try { return JSON.parse(localStorage.getItem(KEY) || 'null'); } catch(e){ return null; }
  }
  let viewSaveTimer = null;
  function saveView(force){
    // Pan fires apply() on every pointermove; writing localStorage per frame
    // is synchronous I/O that stalls the render loop. Throttle to at most one
    // write per 500ms, and force a final write when the gesture ends.
    if(force && viewSaveTimer){ clearTimeout(viewSaveTimer); viewSaveTimer = null; }
    if(viewSaveTimer) return;
    if(!force){
      viewSaveTimer = setTimeout(function(){ viewSaveTimer = null; }, 500);
    }
    try {
      localStorage.setItem(KEY, JSON.stringify({
        scale: view.scale, worldX: view.worldX, worldY: view.worldY
      }));
    } catch(e){}
  }

  function defaultViewFromSvg(el){
    const meta = readSvgMeta(el);
    const r = stage.getBoundingClientRect();
    const fit = Math.min(r.width / meta.width, r.height / meta.height) * 0.92;
    const scale = clamp(fit, 0.15, 2.5);
    let wx, wy;
    if (meta.focusX || meta.focusY) {
      const w = svgToWorld(meta, meta.focusX, meta.focusY);
      wx = w[0]; wy = w[1];
    } else {
      wx = (meta.xmin + meta.xmax) / 2;
      wy = (meta.ymin + meta.ymax) / 2;
    }
    return { scale: scale, worldX: wx, worldY: wy };
  }

  function ensureView(){
    view = loadView();
    if(!view || typeof view.scale !== 'number' || typeof view.worldX !== 'number'){
      view = defaultViewFromSvg(svg);
      saveView();
    }
  }

  function apply(){
    if(!svg || !stage || !view) return;
    const meta = readSvgMeta(svg);
    const r = stage.getBoundingClientRect();
    const sp = worldToSvg(meta, view.worldX, view.worldY);
    const x = r.width / 2 - sp[0] * view.scale;
    const y = r.height / 2 - sp[1] * view.scale;
    svg.style.transformOrigin = '0 0';
    svg.style.transform = 'translate(' + x + 'px, ' + y + 'px) scale(' + view.scale + ')';
    const zlbl = document.getElementById('zoomLabel');
    if(zlbl) zlbl.textContent = Math.round(view.scale * 100) + '%';
    view._x = x; view._y = y;
    saveView();
  }

  function pixelToWorldUnder(clientX, clientY){
    const meta = readSvgMeta(svg);
    const r = stage.getBoundingClientRect();
    const px = (clientX - r.left - view._x) / view.scale;
    const py = (clientY - r.top - view._y) / view.scale;
    return svgToWorld(meta, px, py);
  }

  function pixelToSvgLocal(clientX, clientY){
    const r = stage.getBoundingClientRect();
    return [
      (clientX - r.left - (view._x || 0)) / view.scale,
      (clientY - r.top - (view._y || 0)) / view.scale,
    ];
  }

  function setCoordLabel(text){
    const label = document.getElementById('mapCoordLabel');
    if(label) label.textContent = text;
  }

  function updateCoordReadout(clientX, clientY){
    if(!svg || !view || !stage) return;
    const meta = readSvgMeta(svg);
    const p = pixelToSvgLocal(clientX, clientY);
    if(p[0] < 0 || p[1] < 0 || p[0] > meta.width || p[1] > meta.height){
      setCoordLabel(pickMode ? '拾取：移入地图点选' : '坐标 —');
      return;
    }
    const w = svgToWorld(meta, p[0], p[1]);
    const x = Math.round(w[0]), y = Math.round(w[1]);
    setCoordLabel((pickMode ? '拾取 (' : '坐标 (') + x + ', ' + y + ')');
  }

  function resetCoordReadout(){
    if(pickMode) setCoordLabel('拾取：点击地图选择「' + PICK_LABELS[pickMode] + '」，Esc 取消');
    else setCoordLabel('坐标 —');
  }

  function setPickMode(mode){
    pickMode = mode;
    const stageEl = document.getElementById('mapStage');
    if(stageEl) stageEl.classList.toggle('picking', !!mode);
    Object.keys(PICK_BUTTONS).forEach(function(id){
      const btn = document.getElementById(id);
      if(btn) btn.classList.toggle('active', !!mode && PICK_BUTTONS[id] === mode);
    });
    if(mode) setCoordLabel('拾取：点击地图选择「' + PICK_LABELS[mode] + '」，Esc 取消');
    else resetCoordReadout();
  }

  function applyPick(world){
    if(!pickMode) return;
    const t = PICK_TARGETS[pickMode];
    const xEl = document.getElementById(t.xId);
    const yEl = document.getElementById(t.yId);
    if(!xEl || !yEl) return;
    const fit = function(el, v){
      const lo = el.min === '' ? -Infinity : Number(el.min);
      const hi = el.max === '' ? Infinity : Number(el.max);
      return clamp(Math.round(v), lo, hi);
    };
    xEl.value = String(fit(xEl, world[0]));
    yEl.value = String(fit(yEl, world[1]));
    const picked = xEl.value + ', ' + yEl.value;
    setPickMode(null);
    setCoordLabel('已拾取 (' + picked + ')');
    // One bubbled event pair marks the owning form as an unsaved draft.
    xEl.dispatchEvent(new Event('input', {bubbles:true}));
    xEl.dispatchEvent(new Event('change', {bubbles:true}));
    setTimeout(function(){
      if(!pickMode) setCoordLabel('坐标 —');
    }, 1800);
  }

  function pickOwnUnitAt(clientX, clientY){
    // Default map click: if the click lands on an own unit (W/V/R) marker,
    // select it in the manual-target dropdown — no extra button needed.
    // Only own units carry data-unit; enemies/core never do.
    const el = document.elementFromPoint(clientX, clientY);
    const hit = el && el.closest ? el.closest('[data-unit]') : null;
    if(!hit) return false;
    const name = hit.getAttribute('data-unit') || '';
    if(!name) return false;
    const sel = document.getElementById('wpName');
    if(!sel) return false;
    // The dropdown lists units alive at page render; a just-spawned unit may
    // be missing from it, so fail loudly instead of submitting a phantom name.
    let matched = false;
    for(let i = 0; i < sel.options.length; i++){
      if(sel.options[i].value === name){
        sel.selectedIndex = i;
        matched = true;
        break;
      }
    }
    if(!matched){
      setCoordLabel('「' + name + '」不在可选列表，请刷新页面后再试');
      return true;
    }
    // Enter coordinate pick so the next map click sets the destination.
    setPickMode('wp');
    setCoordLabel('已选 ' + name + '，点击地图选目标坐标，Esc 取消');
    const wpMsg = document.getElementById('wpMsg');
    if(wpMsg){ wpMsg.textContent = '已选 ' + name + '，点击地图选择目标坐标'; wpMsg.className = 'wp-msg ok'; }
    return true;
  }

  function handleStageClick(clientX, clientY){
    if(!svg) return;
    // Default (no pick mode): clicking an own unit selects it for a manual
    // target; clicking empty map / enemies / core does nothing.
    if(!pickMode){
      pickOwnUnitAt(clientX, clientY);
      return;
    }
    const meta = readSvgMeta(svg);
    const p = pixelToSvgLocal(clientX, clientY);
    if(p[0] < 0 || p[1] < 0 || p[0] > meta.width || p[1] > meta.height){
      setCoordLabel('拾取：请在地图范围内点击');
      return;
    }
    applyPick(svgToWorld(meta, p[0], p[1]));
  }

  function bindPickButton(id){
    const btn = document.getElementById(id);
    if(!btn || btn._bound) return;
    btn._bound = true;
    btn.onclick = function(e){
      e.preventDefault();
      e.stopPropagation();
      const mode = PICK_BUTTONS[id];
      setPickMode(pickMode === mode ? null : mode);
    };
  }

  function bindPickButtons(){
    Object.keys(PICK_BUTTONS).forEach(bindPickButton);
  }

  function syncPickButtonsDisabled(){
    const modeEl = document.querySelector('input[name="attack_mode"]:checked');
    const btn = document.getElementById('pickAttackBtn');
    if(btn) btn.disabled = !!(modeEl && modeEl.value !== 'coords');
    const kiteModeEl = document.querySelector('input[name="kite_mode"]:checked');
    const kiteBtn = document.getElementById('pickKiteBtn');
    if(kiteBtn) kiteBtn.disabled = !!(kiteModeEl && kiteModeEl.value !== 'coords');
  }

  function zoomAt(clientX, clientY, nextScale){
    const before = pixelToWorldUnder(clientX, clientY);
    view.scale = clamp(nextScale, 0.1, 6);
    apply();
    const after = pixelToWorldUnder(clientX, clientY);
    view.worldX += before[0] - after[0];
    view.worldY += before[1] - after[1];
    apply();
    saveView(); // 节流版：滚轮连续缩放不会每帧写
  }

  // Jump the map view so the given world cell sits at the stage centre.
  // Zoom is preserved — the user's zoom level is theirs to keep.
  function focusWorld(wx, wy){
    if(!svg || !view) return;
    view.worldX = wx;
    view.worldY = wy;
    apply();
    setCoordLabel('已定位 (' + Math.round(wx) + ', ' + Math.round(wy) + ')');
  }

  function bindStage(){
    stage = document.getElementById('mapStage');
    svg = document.getElementById('gameMap');
    if(!stage || !svg) return;
    svg.addEventListener('dragstart', function(e){ e.preventDefault(); });

    stage.onpointerdown = function(e){
      if(e.button !== 0) return;
      drag = true; moved = false;
      downX = e.clientX; downY = e.clientY;
      lx = e.clientX; ly = e.clientY;
      try { stage.setPointerCapture(e.pointerId); } catch(err){}
      svg.classList.add('dragging');
    };
    stage.onpointermove = function(e){
      updateCoordReadout(e.clientX, e.clientY);
      if(!drag) return;
      if(Math.hypot(e.clientX - downX, e.clientY - downY) > 5) moved = true;
      const dx = e.clientX - lx, dy = e.clientY - ly;
      lx = e.clientX; ly = e.clientY;
      const meta = readSvgMeta(svg);
      view.worldX -= dx / (view.scale * meta.cell);
      view.worldY -= dy / (view.scale * meta.cell);
      apply();
    };
    stage.onpointerup = function(e){
      if(!drag) return;
      drag = false;
      if(svg) svg.classList.remove('dragging');
      try { stage.releasePointerCapture(e.pointerId); } catch(err){}
      saveView(true); // 手势结束，落盘一次视图
      if(!moved) handleStageClick(e.clientX, e.clientY);
    };
    stage.onpointercancel = function(e){
      if(!drag) return;
      drag = false;
      if(svg) svg.classList.remove('dragging');
      try { stage.releasePointerCapture(e.pointerId); } catch(err){}
      saveView(true);
    };
    stage.onpointerleave = function(){
      if(!drag) resetCoordReadout();
    };

    const zi = document.getElementById('zoomInBtn');
    const zo = document.getElementById('zoomOutBtn');
    const rst = document.getElementById('resetViewBtn');
    const fc = document.getElementById('focusCoreBtn');
    if(zi) zi.onclick = function(){
      const r=stage.getBoundingClientRect();
      zoomAt(r.left+r.width/2, r.top+r.height/2, view.scale*1.2);
    };
    if(zo) zo.onclick = function(){
      const r=stage.getBoundingClientRect();
      zoomAt(r.left+r.width/2, r.top+r.height/2, view.scale/1.2);
    };
    if(rst) rst.onclick = function(){ view = defaultViewFromSvg(svg); apply(); };
    if(fc) fc.onclick = function(){
      const meta = readSvgMeta(svg);
      const w = svgToWorld(meta, meta.focusX, meta.focusY);
      view.worldX = w[0]; view.worldY = w[1];
      apply();
    };
    const rm = document.getElementById('refreshMapBtn');
    if(rm) rm.onclick = function(){ refreshMapNow(); };
  }

  function setHtml(sel, html){
    const el = document.querySelector(sel);
    if(el) el.innerHTML = html;
  }
  function setText(sel, text){
    const el = document.querySelector(sel);
    if(el) el.textContent = text;
  }
  function setClass(sel, cls){
    const el = document.querySelector(sel);
    if(el) el.className = cls;
  }

  // ── Map category filters (legend toggles) ────────────────────────────────
  // Each drawable map element carries a data-cat; toggling a legend button
  // hides/shows that whole category. State persists in localStorage so the
  // view survives soft refreshes and page reloads.
  function loadMapFilters(){
    const out = {};
    MAP_CATS.forEach(function(c){ out[c] = true; });
    try{
      const raw = JSON.parse(localStorage.getItem(MAP_FILTER_KEY) || 'null');
      if(raw && typeof raw === 'object'){
        MAP_CATS.forEach(function(c){ out[c] = raw[c] !== false; });
      }
    }catch(e){}
    return out;
  }
  function applyMapFilters(){
    if(!svg) return;
    mapFilters = mapFilters || loadMapFilters();
    // Toggle one class per hidden category; CSS rules do the actual hiding so
    // this never walks the SVG node list (thousands of wall cells etc).
    MAP_CATS.forEach(function(cat){
      svg.classList.toggle('hide-' + cat, mapFilters[cat] === false);
    });
    const btns = document.querySelectorAll('.map-filter[data-cat]');
    for(let i = 0; i < btns.length; i++){
      const cat = btns[i].getAttribute('data-cat');
      btns[i].classList.toggle('off', mapFilters[cat] === false);
    }
  }
  function bindMapFilters(){
    const btns = document.querySelectorAll('.map-filter[data-cat]');
    for(let i = 0; i < btns.length; i++){
      const b = btns[i];
      b.addEventListener('click', function(){
        mapFilters = mapFilters || loadMapFilters();
        const cat = b.getAttribute('data-cat');
        mapFilters[cat] = !mapFilters[cat];
        try{ localStorage.setItem(MAP_FILTER_KEY, JSON.stringify(mapFilters)); }catch(e){}
        applyMapFilters();
      });
    }
    const rst = document.getElementById('mapFilterReset');
    if(rst) rst.addEventListener('click', function(){
      mapFilters = loadMapFilters();
      MAP_CATS.forEach(function(c){ mapFilters[c] = true; });
      try{ localStorage.setItem(MAP_FILTER_KEY, JSON.stringify(mapFilters)); }catch(e){}
      applyMapFilters();
    });
  }

  // ── Battle-log category filters ─────────────────────────────────────────
  function loadLogFilters(){
    const out = {};
    LOG_CATS.forEach(function(c){ out[c] = LOG_CATS_DEFAULT_OFF.indexOf(c) < 0; });
    try{
      const raw = JSON.parse(localStorage.getItem(LOG_FILTER_KEY) || 'null');
      if(raw && typeof raw === 'object'){
        LOG_CATS.forEach(function(c){ out[c] = raw[c] !== false; });
      }
    }catch(e){}
    return out;
  }
  function applyLogFilters(){
    const list = document.getElementById('logSection');
    if(!list) return;
    logFilters = logFilters || loadLogFilters();
    if(logWindow == null) logWindow = loadLogWindow();
    // Rows older than the selected time window are hidden. 'all' disables it.
    const cutoff = logWindow === 'all' ? null : (Date.now() / 1000 - logWindow);
    const rows = list.querySelectorAll('.log-row');
    for(let i = 0; i < rows.length; i++){
      const cat = rows[i].getAttribute('data-cat') || '';
      let visible = logFilters[cat] !== false;
      if(visible && cutoff !== null){
        const ts = parseFloat(rows[i].getAttribute('data-ts'));
        visible = !isNaN(ts) && ts >= cutoff;
      }
      rows[i].style.display = visible ? '' : 'none';
    }
    const btns = document.querySelectorAll('.log-filter[data-log-cat]');
    for(let i = 0; i < btns.length; i++){
      const cat = btns[i].getAttribute('data-log-cat');
      btns[i].classList.toggle('on', logFilters[cat] !== false);
      btns[i].classList.toggle('off', logFilters[cat] === false);
    }
    updateLogTimeButtons();
  }
  function bindLogFilters(){
    const btns = document.querySelectorAll('.log-filter[data-log-cat]');
    for(let i = 0; i < btns.length; i++){
      btns[i].addEventListener('click', function(){
        logFilters = logFilters || loadLogFilters();
        const cat = btns[i].getAttribute('data-log-cat');
        logFilters[cat] = !logFilters[cat];
        try{ localStorage.setItem(LOG_FILTER_KEY, JSON.stringify(logFilters)); }catch(e){}
        applyLogFilters();
      });
    }
  }

  // ── Battle-log time-window filter ───────────────────────────────────────
  function loadLogWindow(){
    let w = 'all';
    try{
      const raw = localStorage.getItem(LOG_WINDOW_KEY);
      if(raw === 'all') w = 'all';
      else if(raw !== null){
        const n = Number(raw);
        if(!isNaN(n) && n >= 1) w = n;
      }
    }catch(e){}
    return w;
  }
  // How many rows the server should send for a given window. The window only
  // filters rows the client already has, so bigger windows need more rows
  // fetched, otherwise "全部" would still cap at the fixed newest-200.
  function logLimitFor(w){
    // Bigger windows need more rows fetched; 'all' pulls as much as the server
    // will send (bounded by the clamp + the log file's own retention).
    if(w === 'all') return 3000;
    if(w >= 21600) return 2000;   // 6h
    if(w >= 3600) return 1000;    // 1h
    if(w >= 1800) return 600;     // 30min
    return 300;
  }
  function refreshLogRows(){
    // Re-fetch the log rows for the currently selected window immediately,
    // without waiting for the next tick (softRefresh skips same-tick renders).
    fetch('/api/log?limit=' + logLimitFor(logWindow) + '&ts=' + Date.now(), {cache:'no-store'})
      .then(function(res){ return res.json(); })
      .then(function(data){
        if(data && data.ok && data.html){
          const list = document.getElementById('logSection');
          if(list){ list.innerHTML = data.html; applyLogFilters(); }
        }
      })
      .catch(function(){});
  }
  function persistLogWindow(){
    try{ localStorage.setItem(LOG_WINDOW_KEY, String(logWindow)); }catch(e){}
  }
  function updateLogTimeButtons(){
    const btns = document.querySelectorAll('.log-time-btn[data-log-window]');
    for(let i = 0; i < btns.length; i++){
      const w = btns[i].getAttribute('data-log-window');
      const active = w === 'all' ? logWindow === 'all'
        : (logWindow !== 'all' && Number(w) === logWindow);
      btns[i].classList.toggle('on', active);
    }
    // A custom (non-preset) window is echoed back into the minutes input.
    const input = document.getElementById('logWindowMinutes');
    if(input && logWindow !== 'all' && !(String(logWindow) in LOG_WINDOWS)){
      input.value = String(Math.round(logWindow / 60));
    }
  }
  function bindLogTimeFilters(){
    const panel = document.getElementById('logPanel');
    if(!panel || panel._timeBound) return;
    panel._timeBound = true;
    logWindow = loadLogWindow();
    const btns = document.querySelectorAll('.log-time-btn[data-log-window]');
    for(let i = 0; i < btns.length; i++){
      btns[i].addEventListener('click', function(){
        const w = btns[i].getAttribute('data-log-window');
        logWindow = w === 'all' ? 'all' : Number(w);
        persistLogWindow();
        updateLogTimeButtons();
        applyLogFilters();
        refreshLogRows();
      });
    }
    const input = document.getElementById('logWindowMinutes');
    const applyBtn = document.getElementById('logWindowCustomApply');
    function applyCustom(){
      const v = parseInt(input.value, 10);
      if(!isNaN(v) && v >= 1){
        logWindow = v * 60;
        persistLogWindow();
        updateLogTimeButtons();
        applyLogFilters();
        refreshLogRows();
      }
    }
    if(applyBtn) applyBtn.addEventListener('click', applyCustom);
    if(input){
      input.addEventListener('keydown', function(e){ if(e.key === 'Enter') applyCustom(); });
    }
    updateLogTimeButtons();
  }

  // ── Right-sidebar unit tabs (工人 / 先锋 / 游侠) ──────────────────────
  // One panel, three tabs. Active tab persists so a soft refresh / reload
  // keeps showing the type the user was reading.
  const UNIT_TAB_KEY = 'arenaUnitTab.v1';
  function applyUnitTab(tab){
    const tabs = document.querySelectorAll('.unit-tab[data-unit-tab]');
    const panes = document.querySelectorAll('.unit-tab-pane[data-unit-pane]');
    for(let i = 0; i < tabs.length; i++){
      const on = tabs[i].getAttribute('data-unit-tab') === tab;
      tabs[i].classList.toggle('active', on);
      tabs[i].setAttribute('aria-selected', on ? 'true' : 'false');
    }
    for(let i = 0; i < panes.length; i++){
      panes[i].classList.toggle('active', panes[i].getAttribute('data-unit-pane') === tab);
    }
    try{ localStorage.setItem(UNIT_TAB_KEY, tab); }catch(e){}
  }
  function bindUnitTabs(){
    const tabs = document.querySelectorAll('.unit-tab[data-unit-tab]');
    for(let i = 0; i < tabs.length; i++){
      tabs[i].addEventListener('click', function(){
        applyUnitTab(tabs[i].getAttribute('data-unit-tab'));
      });
    }
    let saved = null;
    try{ saved = localStorage.getItem(UNIT_TAB_KEY); }catch(e){}
    if(saved && ['workers','vanguards','rangers'].indexOf(saved) >= 0){
      applyUnitTab(saved);
    }
  }

  // ── Marker FLIP animation ────────────────────────────────────────────
  // render_svg() wraps every moving marker in <g data-anim data-wx data-wy>.
  // Before swapping in the new SVG we snapshot each marker's world position,
  // then translate the fresh marker from the old spot to the new one so units
  // glide between ticks instead of teleporting. Deltas are computed in world
  // cells × cell-size, so a growing map viewport cannot skew the motion.
  function captureAnimAnchors(){
    const anchors = {};
    const stageEl = document.getElementById('mapStage');
    if(!stageEl) return anchors;
    stageEl.querySelectorAll('g[data-anim]').forEach(function(g){
      anchors[g.getAttribute('data-anim')] = [Number(g.dataset.wx), Number(g.dataset.wy)];
    });
    return anchors;
  }

  function animateMarkers(prevAnchors){
    if(!svg || drag) return;
    if(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const cellSize = Number(svg.dataset.cell || 16);
    svg.querySelectorAll('g[data-anim]').forEach(function(g){
      const prev = prevAnchors[g.getAttribute('data-anim')];
      if(!prev) return;
      const dx = (Number(g.dataset.wx) - prev[0]) * cellSize;
      const dy = (Number(g.dataset.wy) - prev[1]) * cellSize;
      if(dx === 0 && dy === 0) return;
      // A jump of more than a handful of cells is a respawn / relocation, not
      // movement — animating it would look like a unit streaking across the
      // map, so it snaps instead.
      if(Math.abs(dx) > cellSize * 6 || Math.abs(dy) > cellSize * 6) return;
      g.style.transition = 'none';
      g.style.transform = 'translate(' + (-dx) + 'px,' + (-dy) + 'px)';
      void g.getBoundingClientRect();  // force layout so the offset sticks
      g.style.transition = 'transform 1.1s cubic-bezier(0.25, 0.6, 0.3, 1)';
      g.style.transform = 'translate(0px,0px)';
    });
  }

  async function softRefresh(){
    if(document.hidden || drag || refreshing) return;
    refreshing = true;
    try{
      const res = await fetch('/api/state?ts=' + Date.now() + '&log=' + logLimitFor(logWindow), {cache:'no-store'});
      if(res.status === 401){ location.href = '/'; return; }
      if(!res.ok) return;
      const data = await res.json();
      if(!data || data.tick == null) return;
      if(lastTick !== null && data.tick === lastTick) return;
      lastTick = data.tick;

      if(data.brand) setHtml('#brandLine', data.brand);
      if(data.leftHtml){ setHtml('#leftColumn', data.leftHtml); bindOreForm(); }
      if(data.statusHtml) setHtml('#statusPill', data.statusHtml);
      if(data.statusClass) setClass('#statusPill', 'status-pill ' + data.statusClass);
      if(data.heroHtml) setHtml('#heroSection', data.heroHtml);
      if(data.metricsHtml) setHtml('#metricsSection', data.metricsHtml);
      if(data.issuesHtml !== undefined) setHtml('#issuesSection', data.issuesHtml);
      if(data.workersHtml) setHtml('#workersGrid', data.workersHtml);
      if(data.vgHtml) setHtml('#vgGrid', data.vgHtml);
      if(data.rgHtml) setHtml('#rgGrid', data.rgHtml);
      if(data.resHtml){ setHtml('#resSection', data.resHtml); bindOreForm(); }
      if(data.waypointHtml){ setHtml('#waypointSection', data.waypointHtml); bindWaypointPanel(); }
      if(data.enemyHtml){ setHtml('#enemySection', data.enemyHtml); }
      if(data.mapTitle) setHtml('#mapTitleCount', data.mapTitle);
      if(data.hotspotsHtml !== undefined) setHtml('#hotspotBar', data.hotspotsHtml);
      if(data.footerHtml) setHtml('#footerSection', data.footerHtml);
      if(data.workersCount !== undefined) setText('#workersCount', data.workersCount + ' 个');
      if(data.vgCount !== undefined) setText('#vgCount', String(data.vgCount));
      if(data.rgCount !== undefined) setText('#rgCount', String(data.rgCount));
      if(data.workersCount !== undefined || data.vgCount !== undefined || data.rgCount !== undefined){
        updateProductionCounts(data.workersCount, data.vgCount, data.rgCount);
      }
      if(data.resCount !== undefined) setText('#resCount', data.resCount + ' 可见');
      if(data.enemyCount !== undefined) setText('#enemyCount', data.enemyCount);
      if(data.combatUnits && !teamsDirty && !teamsBusy){
        teamsUnits = data.combatUnits;
        renderTeamBoard();
      }

      if(data.mapSvg && data.mapSvg !== lastMapSvg){
        const stageEl = document.getElementById('mapStage');
        if(stageEl){
          const prevAnchors = captureAnimAnchors();
          stageEl.innerHTML = data.mapSvg;
          lastMapSvg = data.mapSvg;
          svg = document.getElementById('gameMap');
          if(svg){
            svg.addEventListener('dragstart', function(e){ e.preventDefault(); });
            applyMapFilters();
            apply();
            animateMarkers(prevAnchors);
          }
        }
      }
      if(data.logHtml){
        const logList = document.getElementById('logSection');
        if(logList){
          logList.innerHTML = data.logHtml;
          applyLogFilters();
        }
      }
      if(data.logCount !== undefined) setText('#logCount', String(data.logCount));
    } catch(e) {
    } finally {
      refreshing = false;
    }
  }

  
  // Force a full re-render bypassing the idle/drag/tick guards and the
  // mapSvg-unchanged dedup so a manually requested refresh always repaints.
  async function refreshMapNow(){
    const wasDragging = drag;
    // release the refreshing guard if it's stuck (it shouldn't normally be).
    lastTick = null;
    lastMapSvg = '';
    refreshing = false;
    drag = false;
    try{
      await softRefresh();
    } finally {
      drag = wasDragging;
    }
  }

  function bindOreForm(){
    // Bind the right-column ore add form + chip delete buttons; idempotent.
    // Also bind enemy clear button.
    const clearEnemyBtn = document.getElementById('clearEnemyBtn');
    if(clearEnemyBtn && !clearEnemyBtn._bound){
      clearEnemyBtn._bound = true;
      clearEnemyBtn.addEventListener('click', async function(){
        if(!confirm('确定清除所有敌人踪迹？')) return;
        try{
          const res = await fetch('/api/enemy/clear', {method:'POST'});
          const data = await res.json();
          if(data.ok){
            lastTick = null;
            softRefresh();
          }
        }catch(e){}
      });
    }
    const clearResourceBtn = document.getElementById('clearResourceBtn');
    if(clearResourceBtn && !clearResourceBtn._bound){
      clearResourceBtn._bound = true;
      clearResourceBtn.addEventListener('click', async function(){
        if(!confirm('确定清除全部记忆矿点？旧坐标会被屏蔽，直到重新看见或手动录入。')) return;
        try{
          const res = await fetch('/api/resource/clear', {method:'POST'});
          const data = await res.json();
          if(data.ok){
            lastTick = null;
            softRefresh();
          }
        }catch(e){}
      });
    }
    const toggle = document.getElementById('resAddToggle');
    const form = document.getElementById('resAddForm');
    const msg = document.getElementById('oreMsg');
    const xEl = document.getElementById('oreX');
    const yEl = document.getElementById('oreY');
    const addBtn = document.getElementById('oreAddBtn');
    const cancelBtn = document.getElementById('oreCancelBtn');
    const section = document.getElementById('resSection');

    function setMsg(text, kind){
      if(!msg) return;
      msg.textContent = text || '';
      msg.className = 'msg' + (kind ? ' ' + kind : '');
    }
    function openForm(){
      if(form) form.classList.add('open');
      if(toggle) toggle.textContent = '–';
      setTimeout(function(){ if(xEl) xEl.focus(); }, 30);
    }
    function closeForm(){
      if(form) form.classList.remove('open');
      if(toggle) toggle.textContent = '+';
      if(xEl) xEl.value = '';
      if(yEl) yEl.value = '';
      setMsg('输入坐标后点加入', '');
    }
    if(toggle) toggle.onclick = function(){
      if(form && form.classList.contains('open')) closeForm(); else openForm();
    };
    if(cancelBtn) cancelBtn.onclick = closeForm;

    async function postOre(path, x, y){
      if(!Number.isFinite(x) || !Number.isFinite(y)){
        setMsg('请输入有效整数坐标', 'err');
        return;
      }
      setMsg('提交中…', '');
      try{
        const res = await fetch(path, {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({x:Math.trunc(x), y:Math.trunc(y)})
        });
        const data = await res.json();
        if(!res.ok || !data.ok){
          setMsg((data && data.error) || '失败', 'err');
          return;
        }
        setMsg(
          (path.indexOf('remove')>=0 ? '已删除 ' : '已加入 ') +
          '(' + data.pos[0] + ', ' + data.pos[1] + ') · 记忆 ' + data.resource_count, 'ok'
        );
        if(xEl) xEl.value = '';
        if(yEl) yEl.value = '';
        if(path.indexOf('remove') < 0) setTimeout(closeForm, 900);
        lastTick = null;
        softRefresh();
      }catch(e){
        setMsg('网络错误', 'err');
      }
    }
    if(addBtn) addBtn.onclick = function(){
      postOre('/api/resource/add', Number(xEl && xEl.value), Number(yEl && yEl.value));
    };
    if(form){
      form.onsubmit = function(e){ e.preventDefault(); };
      if(xEl && yEl){
        xEl.addEventListener('keydown', function(e){
          if(e.key === 'Escape') closeForm();
          if(e.key === 'Enter'){ e.preventDefault(); postOre('/api/resource/add', Number(xEl.value), Number(yEl.value)); }
        });
        yEl.addEventListener('keydown', function(e){
          if(e.key === 'Escape') closeForm();
          if(e.key === 'Enter'){ e.preventDefault(); postOre('/api/resource/add', Number(xEl.value), Number(yEl.value)); }
        });
      }
    }
    if(section){
      section.querySelectorAll('button.chip-x[data-remove-x]').forEach(function(btn){
        if(btn.dataset.bound === '1') return;
        btn.dataset.bound = '1';
        btn.onclick = function(){
          const x = Number(btn.dataset.removeX);
          const y = Number(btn.dataset.removeY);
          postOre('/api/resource/remove', x, y);
        };
      });
    }
    const orePickBtn = document.getElementById('pickOreBtn');
    if(orePickBtn) bindPickButton('pickOreBtn');
  }

  function bindWaypointPanel(){
    // Manual per-unit target queues: append / remove / clear / mode toggle +
    // ⌖ map pick. Idempotent; re-called after each soft refresh re-renders.
    const panel = document.getElementById('waypointPanel');
    if(!panel) return;
    bindPickButton('pickWpBtn');

    function msg(text, kind){
      const m = document.getElementById('wpMsg');
      if(m){ m.textContent = text || ''; m.className = 'wp-msg' + (kind ? ' ' + kind : ''); }
    }

    function wpPost(path, payload, okMsg){
      return fetch(path, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(payload)
      })
      .then(function(res){ return res.json(); })
      .then(function(data){
        if(data && data.ok){
          if(okMsg) msg(okMsg, 'ok');
          lastTick = null;
          softRefresh();
        } else {
          msg((data && data.error) || '操作失败', 'err');
        }
      })
      .catch(function(){ msg('网络错误', 'err'); });
    }

    const setBtn = document.getElementById('wpSetBtn');
    if(setBtn && !setBtn._bound){
      setBtn._bound = true;
      setBtn.onclick = async function(){
        const nameEl = document.getElementById('wpName');
        const xEl = document.getElementById('wpX');
        const yEl = document.getElementById('wpY');
        const modeEl = document.getElementById('wpMode');
        const name = ((nameEl && nameEl.value) || '').trim();
        const x = Number(xEl && xEl.value);
        const y = Number(yEl && yEl.value);
        const mode = (modeEl && modeEl.value) || 'attack';
        if(!name){ msg('请先选择单位', 'err'); return; }
        if(!Number.isFinite(x) || !Number.isFinite(y)){
          msg('请输入有效整数坐标', 'err');
          return;
        }
        try{
          const res = await fetch('/api/waypoint/set', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({name:name, x:Math.trunc(x), y:Math.trunc(y), mode:mode})
          });
          const data = await res.json();
          if(!res.ok || !data.ok){
            msg((data && data.error) || '加入失败', 'err');
            return;
          }
          const n = (data.queue && data.queue.length) || 1;
          msg(name + ' + (' + data.pos[0] + ', ' + data.pos[1] + ') 已加入队列（共 ' + n + ' 个）', 'ok');
          if(xEl) xEl.value = '';
          if(yEl) yEl.value = '';
          lastTick = null;
          softRefresh();
        }catch(e){ msg('网络错误', 'err'); }
      };
    }

    const clearBtn = document.getElementById('wpClearBtn');
    if(clearBtn && !clearBtn._bound){
      clearBtn._bound = true;
      clearBtn.onclick = async function(){
        if(!confirm('确定清空全部手动目标？')) return;
        try{
          const res = await fetch('/api/waypoint/clear', {method:'POST'});
          const data = await res.json();
          if(data.ok){
            msg('已清空全部手动目标', 'ok');
            lastTick = null;
            softRefresh();
          }
        }catch(e){}
      };
    }

    // Per-unit mode toggle (攻击 <-> 赶路).
    panel.querySelectorAll('button[data-wp-mode-toggle]').forEach(function(btn){
      if(btn.dataset.bound === '1') return;
      btn.dataset.bound = '1';
      btn.onclick = function(){
        const name = btn.dataset.wpModeToggle;
        const next = (btn.dataset.mode === 'attack') ? 'rush' : 'attack';
        wpPost('/api/waypoint/mode',
          {name:name, mode:next},
          name + ' → ' + (next === 'attack' ? '攻击' : '赶路'));
      };
    });

    // Remove one queued target by index.
    panel.querySelectorAll('button[data-wp-remove]').forEach(function(btn){
      if(btn.dataset.bound === '1') return;
      btn.dataset.bound = '1';
      btn.onclick = function(){
        const name = btn.dataset.wpRemove;
        const index = Number(btn.dataset.wpIndex);
        wpPost('/api/waypoint/remove', {name:name, index:index}, name + ' 目标已删除');
      };
    });

    // Clear one unit's whole queue.
    panel.querySelectorAll('button[data-wp-clear-unit]').forEach(function(btn){
      if(btn.dataset.bound === '1') return;
      btn.dataset.bound = '1';
      btn.onclick = function(){
        const name = btn.dataset.wpClearUnit;
        if(!confirm('清空 ' + name + ' 的全部手动目标？')) return;
        wpPost('/api/waypoint/remove', {name:name}, name + ' 目标已清空');
      };
    });
  }

  let productionCounts = {workers: null, vanguards: null, rangers: null};

  function productionTargetValues(){
    const out = {};
    document.querySelectorAll('#productionTargets input[name^="target_"]').forEach(function(inp){
      out[inp.name] = Number(inp.value);
    });
    return out;
  }

  function updateProductionStates(){
    const targets = productionTargetValues();
    const set = function(id, value){
      const el = document.getElementById(id);
      if(el) el.textContent = String(value == null ? '-' : value);
    };
    set('prodTargetWorkers', targets.target_workers);
    set('prodTargetVanguards', targets.target_vanguards);
    set('prodTargetRangers', targets.target_rangers);
    const known = productionCounts.workers != null
      || productionCounts.vanguards != null
      || productionCounts.rangers != null;
    if(!known) return;
    const rows = [
      ['prodStateWorkers', 'target_workers', productionCounts.workers],
      ['prodStateVanguards', 'target_vanguards', productionCounts.vanguards],
      ['prodStateRangers', 'target_rangers', productionCounts.rangers],
    ];
    rows.forEach(function(row){
      const el = document.getElementById(row[0]);
      if(!el) return;
      const target = Number(targets[row[1]]);
      const current = Number(row[2] == null ? 0 : row[2]);
      const diff = target - current;
      let text, cls;
      if(diff > 0){ text = '缺 ' + diff + ' · 生产中'; cls = 'producing'; }
      else if(diff < 0){ text = '超编 ' + (-diff) + ' · 停止生产'; cls = 'ok'; }
      else { text = '已达标'; cls = 'ok'; }
      el.textContent = text;
      el.className = 'prod-state ' + cls;
    });
  }

  function updateProductionCounts(workers, vanguards, rangers){
    productionCounts = {workers: workers, vanguards: vanguards, rangers: rangers};
    const set = function(id, value){
      const el = document.getElementById(id);
      if(el) el.textContent = String(value == null ? '-' : value);
    };
    set('prodCurrentWorkers', workers);
    set('prodCurrentVanguards', vanguards);
    set('prodCurrentRangers', rangers);
    updateProductionStates();
  }

  function applyConfigValues(config){
    const form = document.getElementById('tacticConfigForm');
    if(!form || !config) return;
    form.querySelectorAll('[name]').forEach(function(input){
      if(!(input.name in config)) return;
      // Never overwrite the field being typed into — a stale poll response
      // must not wipe a half-typed draft.
      if(input === document.activeElement) return;
      if(input.dataset.kind === 'boolean') input.checked = Boolean(config[input.name]);
      else input.value = String(config[input.name]);
    });
    updateProductionStates();
  }

  function teamKindLabel(kind){
    if(kind === 'VANGUARD') return '先锋';
    if(kind === 'RANGER') return '游侠';
    return '作战';
  }

  function teamMetaLine(unit){
    const bits = [];
    bits.push(teamKindLabel(unit.kind));
    if(unit.alive){
      if(unit.pos) bits.push('(' + unit.pos[0] + ', ' + unit.pos[1] + ')');
      if(unit.hp != null) bits.push('HP ' + unit.hp);
    }else{
      bits.push('配置中');
    }
    return bits.join(' · ');
  }

  function setTeamsMessage(text, kind){
    const msg = document.getElementById('teamsMessage');
    if(msg){
      msg.textContent = text || '';
      msg.className = 'teams-message' + (kind ? ' ' + kind : '');
    }
    const state = document.getElementById('teamsState');
    if(state){
      const stateKind = kind === 'err' ? 'err' : (teamsBusy ? 'busy' : (teamsDirty ? 'dirty' : 'ok'));
      state.textContent = stateKind === 'err' ? '保存失败'
        : (stateKind === 'busy' ? '保存中…' : (stateKind === 'dirty' ? '有未保存修改' : '已同步'));
      state.className = 'count teams-state ' + stateKind;
    }
    updateTeamsActions();
  }

  function updateTeamsActions(){
    const saveBtn = document.getElementById('teamsSaveBtn');
    const resetBtn = document.getElementById('teamsResetBtn');
    if(saveBtn) saveBtn.disabled = teamsBusy || !teamsDirty;
    if(resetBtn) resetBtn.disabled = teamsBusy || !teamsDirty;
  }

  function clearTeamFieldErrors(){
    document.querySelectorAll('#teamsForm [data-field-error]').forEach(function(el){ el.textContent = ''; });
    document.querySelectorAll('#teamsForm [data-field-wrap]').forEach(function(el){ el.classList.remove('has-error'); });
    document.querySelectorAll('#teamsForm .field-error-control').forEach(function(el){
      el.classList.remove('field-error-control');
      el.removeAttribute('aria-invalid');
    });
  }

  function showTeamFieldErrors(fields){
    Object.keys(fields || {}).forEach(function(key){
      const error = document.querySelector('#teamsForm [data-field-error="' + key + '"]');
      const wrap = document.querySelector('#teamsForm [data-field-wrap="' + key + '"]');
      const control = document.querySelector('#teamsForm [name="' + key + '"]');
      if(error) error.textContent = fields[key];
      if(wrap) wrap.classList.add('has-error');
      if(control){ control.classList.add('field-error-control'); control.setAttribute('aria-invalid', 'true'); }
    });
  }

  function markTeamsDirty(message){
    teamsDirty = true;
    clearTeamFieldErrors();
    setTeamsMessage(message || '有未保存修改', '');
  }

  function currentTeamSettings(){
    const modeEl = document.querySelector('input[name="attack_mode"]:checked');
    const kiteModeEl = document.querySelector('input[name="kite_mode"]:checked');
    return {
      home_patrol_radius: Number((document.getElementById('teamHomeRadius') || {}).value || 5),
      home_engage_radius: Number((document.getElementById('teamHomeEngageRadius') || {}).value || 0),
      home_engage_memory_ticks: Number((document.getElementById('teamHomeMemory') || {}).value || 0),
      combat_heal_hp_threshold: Number((document.getElementById('teamHealThreshold') || {}).value || 0),
      combat_heal_return_limit: Number((document.getElementById('teamHealReturnLimit') || {}).value || 0),
      attack_target_x: Number((document.getElementById('teamAttackX') || {}).value || 0),
      attack_target_y: Number((document.getElementById('teamAttackY') || {}).value || 0),
      attack_mode: (modeEl && modeEl.value) || 'coords',
      kite_target_x: Number((document.getElementById('teamKiteX') || {}).value || 0),
      kite_target_y: Number((document.getElementById('teamKiteY') || {}).value || 0),
      kite_mode: (kiteModeEl && kiteModeEl.value) || 'coords',
      kite_auto_radius: Number((document.getElementById('teamKiteAutoRadius') || {}).value || 0),
      kite_march_engage_radius: Number((document.getElementById('teamKiteMarchRadius') || {}).value || 0),
      ranger_attack_range: Number((document.getElementById('teamRangerRange') || {}).value || 3),
      ranger_lead_fire_enabled: Boolean((document.getElementById('teamLeadFire') || {}).checked),
      attack_retreat_radius: Number((document.getElementById('teamRetreatRadius') || {}).value || 5),
      attack_auto_radius: Number((document.getElementById('teamAutoRadius') || {}).value || 0),
      attack_march_engage_radius: Number((document.getElementById('teamMarchRadius') || {}).value || 0),
      guerrilla_engage_radius: Number((document.getElementById('teamGuerrillaSight') || {}).value || 0)
    };
  }

  function syncTeamModeVisibility(){
    const modeEl = document.querySelector('input[name="attack_mode"]:checked');
    const mode = (modeEl && modeEl.value) || 'coords';
    document.querySelectorAll('[data-attack-fields]').forEach(function(group){
      const modes = (group.getAttribute('data-attack-fields') || '').split(' ');
      const active = modes.indexOf(mode) !== -1;
      group.hidden = !active;
      group.querySelectorAll('input,select,button').forEach(function(el){ el.disabled = !active; });
    });
    syncPickButtonsDisabled();
    if(mode !== 'coords' && pickMode === 'attack') setPickMode(null);
    const kiteModeEl = document.querySelector('input[name="kite_mode"]:checked');
    const kiteMode = (kiteModeEl && kiteModeEl.value) || 'coords';
    document.querySelectorAll('[data-kite-fields]').forEach(function(group){
      const modes = (group.getAttribute('data-kite-fields') || '').split(' ');
      const active = modes.indexOf(kiteMode) !== -1;
      group.hidden = !active;
      group.querySelectorAll('input,select,button').forEach(function(el){ el.disabled = !active; });
    });
    if(kiteMode !== 'coords' && pickMode === 'kite') setPickMode(null);
  }

  function applyTeamSettings(config){
    if(!config) return;
    const map = {
      home_patrol_radius: 'teamHomeRadius',
      home_engage_radius: 'teamHomeEngageRadius',
      home_engage_memory_ticks: 'teamHomeMemory',
      combat_heal_hp_threshold: 'teamHealThreshold',
      combat_heal_return_limit: 'teamHealReturnLimit',
      attack_target_x: 'teamAttackX',
      attack_target_y: 'teamAttackY',
      ranger_attack_range: 'teamRangerRange',
      ranger_lead_fire_enabled: 'teamLeadFire',
      attack_retreat_radius: 'teamRetreatRadius',
      attack_auto_radius: 'teamAutoRadius',
      attack_march_engage_radius: 'teamMarchRadius',
      kite_target_x: 'teamKiteX',
      kite_target_y: 'teamKiteY',
      kite_auto_radius: 'teamKiteAutoRadius',
      kite_march_engage_radius: 'teamKiteMarchRadius',
      guerrilla_engage_radius: 'teamGuerrillaSight'
    };
    Object.keys(map).forEach(function(key){
      const el = document.getElementById(map[key]);
      if(el && key in config) {
        // Skip the field being typed into — a stale poll response must not
        // wipe a half-typed draft.
        if(el === document.activeElement) return;
        if (el.type === 'checkbox') el.checked = !!config[key];
        else el.value = String(config[key]);
      }
    });
    const mode = config.attack_mode || 'coords';
    document.querySelectorAll('input[name="attack_mode"]').forEach(function(el){
      el.checked = (el.value === mode);
    });
    const kiteMode = config.kite_mode || 'coords';
    document.querySelectorAll('input[name="kite_mode"]').forEach(function(el){
      el.checked = (el.value === kiteMode);
    });
    syncTeamModeVisibility();
  }

  function rosterFromUnits(team){
    return teamsUnits
      .filter(function(unit){ return unit.team === team; })
      .map(function(unit){ return unit.name; })
      .sort(function(a, b){
        const na = parseInt(a.slice(1), 10) || 0;
        const nb = parseInt(b.slice(1), 10) || 0;
        if(a[0] !== b[0]) return a[0] < b[0] ? -1 : 1;
        return na - nb;
      })
      .join(', ');
  }

  function buildTeamsPayload(){
    const settings = currentTeamSettings();
    return {
      home_team: rosterFromUnits('home'),
      attack_team: rosterFromUnits('attack'),
      kite_team: rosterFromUnits('kite'),
      guerrilla_team: rosterFromUnits('guerrilla'),
      home_patrol_radius: settings.home_patrol_radius,
      home_engage_radius: settings.home_engage_radius,
      home_engage_memory_ticks: settings.home_engage_memory_ticks,
      combat_heal_hp_threshold: settings.combat_heal_hp_threshold,
      combat_heal_return_limit: settings.combat_heal_return_limit,
      attack_target_x: settings.attack_target_x,
      attack_target_y: settings.attack_target_y,
      attack_mode: settings.attack_mode,
      kite_target_x: settings.kite_target_x,
      kite_target_y: settings.kite_target_y,
      kite_mode: settings.kite_mode,
      kite_auto_radius: settings.kite_auto_radius,
      kite_march_engage_radius: settings.kite_march_engage_radius,
      ranger_attack_range: settings.ranger_attack_range,
      ranger_lead_fire_enabled: settings.ranger_lead_fire_enabled,
      attack_retreat_radius: settings.attack_retreat_radius,
      attack_auto_radius: settings.attack_auto_radius,
      attack_march_engage_radius: settings.attack_march_engage_radius,
      guerrilla_engage_radius: settings.guerrilla_engage_radius
    };
  }

  function renderTeamBoard(){
    const counts = {unassigned:0, home:0, attack:0, kite:0, guerrilla:0};
    document.querySelectorAll('[data-team-drop]').forEach(function(drop){
      drop.innerHTML = '';
      drop.classList.remove('drag-over');
    });
    (teamsUnits || []).forEach(function(unit){
      const team = unit.team || 'unassigned';
      const drop = document.querySelector('[data-team-drop="' + team + '"]');
      if(!drop) return;
      counts[team] = (counts[team] || 0) + 1;
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'team-chip kind-' + (unit.kind || 'COMBAT') + (unit.alive ? '' : ' ghost');
      chip.draggable = true;
      chip.dataset.unitName = unit.name;
      chip.dataset.team = team;
      chip.title = unit.name + ' · ' + teamMetaLine(unit) + '（拖到其他队伍）';
      const glyph = document.createElement('span');
      glyph.className = 'glyph';
      // Show the full name so W10 is not collapsed into an ambiguous W1;
      // longer names get a smaller glyph font to keep the 34px chip tidy.
      const glyphLabel = String(unit.name || '?');
      glyph.textContent = glyphLabel;
      if (glyphLabel.length > 3) glyph.classList.add('xs');
      else if (glyphLabel.length === 3) glyph.classList.add('sm');
      const pulse = document.createElement('span');
      pulse.className = 'pulse';
      // Segmented health bar under the icon: one green segment per max HP point.
      const hpbar = document.createElement('span');
      hpbar.className = 'hpbar';
      hpbar.setAttribute('aria-hidden', 'true');
      const maxHp = unit.max_hp || 2;
      const curHp = unit.alive && unit.hp != null ? Math.max(0, unit.hp) : 0;
      for(let i = 0; i < maxHp; i++){
        const seg = document.createElement('i');
        seg.className = 'seg' + (i < curHp ? ' on' : '');
        hpbar.appendChild(seg);
      }
      chip.append(glyph, hpbar, pulse);
      chip.addEventListener('dragstart', function(e){
        dragUnitName = unit.name;
        chip.classList.add('dragging');
        try{
          e.dataTransfer.setData('text/plain', unit.name);
          e.dataTransfer.effectAllowed = 'move';
        }catch(err){}
      });
      chip.addEventListener('dragend', function(){
        dragUnitName = null;
        chip.classList.remove('dragging');
        document.querySelectorAll('.team-column').forEach(function(col){ col.classList.remove('drag-over'); });
      });
      drop.appendChild(chip);
    });
    Object.keys(counts).forEach(function(team){
      const el = document.querySelector('[data-team-count="' + team + '"]');
      if(el) el.textContent = String(counts[team] || 0);
      const drop = document.querySelector('[data-team-drop="' + team + '"]');
      if(drop && !(counts[team] > 0)){
        const empty = document.createElement('div');
        empty.className = 'team-empty';
        empty.textContent = '拖到这里';
        drop.appendChild(empty);
      }
    });
  }

  function moveUnitToTeam(name, team){
    if(!name || !team) return false;
    let changed = false;
    teamsUnits = (teamsUnits || []).map(function(unit){
      if(unit.name !== name || unit.team === team) return unit;
      changed = true;
      return Object.assign({}, unit, {team: team});
    });
    if(changed){
      renderTeamBoard();
      markTeamsDirty('编队已调整，点击保存后生效');
    }
    return changed;
  }

  async function saveTeams(){
    if(teamsBusy) return;
    teamsBusy = true;
    clearTeamFieldErrors();
    setTeamsMessage('正在保存战斗分队…', '');
    try{
      const res = await fetch('/api/teams', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(buildTeamsPayload())
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        showTeamFieldErrors(data.fields || {});
        throw new Error(data.error || '保存失败');
      }
      teamsConfig = data.config;
      teamsUnits = data.combat_units || teamsUnits;
      applyTeamSettings(data.config);
      teamsDirty = false;
      renderTeamBoard();
      teamsBusy = false;
      setTeamsMessage('已保存，下个 Tick 生效', 'ok');
    }catch(err){
      setTeamsMessage(err.message || '网络错误', 'err');
    }finally{
      teamsBusy = false;
      updateTeamsActions();
    }
  }

  async function loadTeams(force){
    const form = document.getElementById('teamsForm');
    const editing = form && form.contains(document.activeElement);
    if(!force && (teamsDirty || teamsBusy || editing)) return;
    if(teamsBusy) return;
    const discardedDraft = force && teamsDirty;
    if(discardedDraft){
      teamsBusy = true;
      setTeamsMessage('正在恢复服务器配置…', '');
    }
    try{
      const res = await fetch('/api/teams?ts=' + Date.now(), {cache:'no-store'});
      const data = await res.json();
      if(!res.ok || !data.ok) return;
      // Re-check after the await: a draft started mid-flight must survive.
      if(!force && (teamsDirty || (form && form.contains(document.activeElement)))) return;
      teamsConfig = data.config;
      teamsUnits = data.combat_units || [];
      applyTeamSettings(data.config);
      teamsDirty = false;
      clearTeamFieldErrors();
      renderTeamBoard();
      teamsBusy = false;
      setTeamsMessage(discardedDraft ? '已放弃修改，恢复服务器配置' : '修改后统一保存，下个 Tick 生效', '');
    }catch(e){
      if(discardedDraft) setTeamsMessage('恢复失败，请稍后重试', 'err');
    }finally{
      teamsBusy = false;
      updateTeamsActions();
    }
  }

  function bindTeamsBoard(){
    document.querySelectorAll('.team-column').forEach(function(column){
      const team = column.dataset.team;
      column.addEventListener('dragover', function(e){
        e.preventDefault();
        column.classList.add('drag-over');
        try{ e.dataTransfer.dropEffect = 'move'; }catch(err){}
      });
      column.addEventListener('dragleave', function(e){
        if(!column.contains(e.relatedTarget)) column.classList.remove('drag-over');
      });
      column.addEventListener('drop', function(e){
        e.preventDefault();
        column.classList.remove('drag-over');
        let name = dragUnitName;
        try{ name = e.dataTransfer.getData('text/plain') || name; }catch(err){}
        moveUnitToTeam(name, team);
      });
    });
    const form = document.getElementById('teamsForm');
    if(form){
      form.addEventListener('input', function(e){
        if(e.target && (e.target.name === 'attack_mode' || e.target.name === 'kite_mode')) syncTeamModeVisibility();
        markTeamsDirty('参数已修改，点击保存后生效');
      });
      form.addEventListener('change', function(e){
        if(e.target && (e.target.name === 'attack_mode' || e.target.name === 'kite_mode')) syncTeamModeVisibility();
      });
      form.onsubmit = function(e){
        e.preventDefault();
        if(!form.reportValidity()) return;
        saveTeams();
      };
    }
    const resetBtn = document.getElementById('teamsResetBtn');
    if(resetBtn) resetBtn.onclick = function(){ loadTeams(true); };
    updateTeamsActions();
  }

  function setConfigMessage(text, kind){
    const msg = document.getElementById('configMessage');
    if(msg){
      msg.textContent = text || '';
      msg.className = 'config-message' + (kind ? ' ' + kind : '');
    }
    const state = document.getElementById('configState');
    if(state) state.textContent = kind === 'err' ? '保存失败' : (kind === 'ok' ? '已同步' : '待保存');
  }

  async function loadConfig(force){
    const form = document.getElementById('tacticConfigForm');
    if(!form || (!force && (configDirty || form.contains(document.activeElement)))) return;
    try{
      const res = await fetch('/api/config?ts=' + Date.now(), {cache:'no-store'});
      const data = await res.json();
      // The fetch can resolve after the user started typing; re-check so a
      // slow response never clobbers a draft that appeared mid-flight.
      if(!force && (configDirty || form.contains(document.activeElement))) return;
      if(res.ok && data.ok){ applyConfigValues(data.config); configDirty = false; }
    }catch(e){}
  }

  function bindConfigForm(){
    const form = document.getElementById('tacticConfigForm');
    if(!form) return;
    const saveBtn = document.getElementById('configSaveBtn');
    const resetBtn = document.getElementById('configResetBtn');
    form.addEventListener('input', function(){
      configDirty = true;
      setConfigMessage('有未保存修改', '');
      updateProductionStates();
    });
    form.onsubmit = async function(e){
      e.preventDefault();
      if(!form.reportValidity()) return;
      const config = {};
      form.querySelectorAll('[name]').forEach(function(input){
        if(input.dataset.kind === 'boolean') config[input.name] = input.checked;
        else config[input.name] = Number(input.value);
      });
      if(saveBtn) saveBtn.disabled = true;
      setConfigMessage('保存中…', '');
      try{
        const res = await fetch('/api/config', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify(config)
        });
        const data = await res.json();
        if(!res.ok || !data.ok) throw new Error(data.error || '保存失败');
        applyConfigValues(data.config);
        configDirty = false;
        setConfigMessage('已保存，下个 Tick 生效', 'ok');
      }catch(err){
        setConfigMessage(err.message || '网络错误', 'err');
      }finally{
        if(saveBtn) saveBtn.disabled = false;
      }
    };
    if(resetBtn) resetBtn.onclick = async function(){
      resetBtn.disabled = true;
      setConfigMessage('恢复中…', '');
      try{
        const res = await fetch('/api/config/reset', {
          method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'
        });
        const data = await res.json();
        if(!res.ok || !data.ok) throw new Error(data.error || '恢复失败');
        applyConfigValues(data.config);
        configDirty = false;
        setConfigMessage('已恢复默认值', 'ok');
      }catch(err){
        setConfigMessage(err.message || '网络错误', 'err');
      }finally{
        resetBtn.disabled = false;
      }
    };
  }

  // ── 历史趋势 panel ──────────────────────────────────────────────────
  function trendNum(v){ return (v == null) ? 0 : Number(v); }
  function trendTimeLabel(t){
    const d = new Date(t * 1000);
    const p = function(n){ return String(n).padStart(2, '0'); };
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }
  function trendNiceStep(maxVal){
    if(!(maxVal > 0)) return 4;
    const mag = Math.pow(10, Math.floor(Math.log10(maxVal)));
    const norm = maxVal / mag;
    let step;
    if(norm <= 1) step = 1;
    else if(norm <= 2) step = 2;
    else if(norm <= 5) step = 5;
    else step = 10;
    return step * mag;
  }

  function renderTrendChart(name){
    const fig = document.querySelector('.trend-figure[data-trend-chart="' + name + '"]');
    if(!fig) return;
    const series = TREND_CHARTS[name].keys.map(function(k){ return TREND_SERIES[k]; });
    const points = trendPoints;
    const svg = fig.querySelector('[data-trend-svg]');
    const legend = fig.querySelector('[data-trend-legend]');
    if(!points.length){
      if(svg) svg.innerHTML = '';
      if(legend) legend.innerHTML = '<span class="t-key">暂无数据</span>';
      TREND_CHART_STATE[name] = {points: [], series: series, svg: svg};
      return;
    }

    let maxVal = 0;
    points.forEach(function(p){
      series.forEach(function(s){ const v = trendNum(p[s.key]); if(v > maxVal) maxVal = v; });
    });
    const yMax = trendNiceStep(maxVal);
    const W = 400, H = 160, PL = 30, PR = 10, PT = 8, PB = 18;
    const PW = W - PL - PR, PH = H - PT - PB;
    // Lay out by real wall-clock time so uneven tick cadence is visible.
    const t0 = points[0].t, tN = points[points.length - 1].t;
    const x = function(t){ return (tN === t0) ? (PL + PW / 2) : (PL + ((t - t0) / (tN - t0)) * PW); };
    const y = function(v){ return PT + (1 - v / yMax) * PH; };

    let grid = '';
    const ticks = 4;
    for(let g = 0; g <= ticks; g++){
      const val = yMax * g / ticks;
      const gy = y(val);
      grid += '<line class="t-grid" x1="' + PL + '" y1="' + gy.toFixed(2) +
              '" x2="' + (W - PR) + '" y2="' + gy.toFixed(2) + '"/>' +
              '<text x="' + (PL - 4) + '" y="' + (gy + 3).toFixed(2) + '" text-anchor="end" ' +
              'font-size="8" fill="#7f8eab">' + Math.round(val) + '</text>';
    }
    let xlabels = '';
    const tMid = t0 + (tN - t0) / 2;
    const labelTs = [];
    [t0, tMid, tN].forEach(function(tv){ if(labelTs.indexOf(tv) === -1) labelTs.push(tv); });
    labelTs.forEach(function(tv){
      xlabels += '<text x="' + x(tv).toFixed(2) + '" y="' + (H - 4) + '" text-anchor="middle" ' +
                 'font-size="8" fill="#7f8eab">' + trendTimeLabel(tv) + '</text>';
    });

    let lines = '';
    series.forEach(function(s){
      const pts = points.map(function(p){
        return x(p.t).toFixed(2) + ',' + y(trendNum(p[s.key])).toFixed(2);
      }).join(' ');
      lines += '<polyline class="t-line' + (s.dash ? ' dash' : '') + '" points="' + pts + '" stroke="' + s.color + '"/>';
      const last = points[points.length - 1];
      lines += '<circle cx="' + x(last.t).toFixed(2) + '" cy="' + y(trendNum(last[s.key])).toFixed(2) +
               '" r="3" fill="' + s.color + '" stroke="#0b1222" stroke-width="1.5"/>';
    });

    const cross = '<line class="t-crosshair" x1="0" y1="' + PT + '" x2="0" y2="' + (H - PB) + '" visibility="hidden"/>';
    svg.innerHTML = grid + xlabels + lines + cross;
    if(legend){
      legend.innerHTML = series.map(function(s){
        return '<span class="t-key"><i style="background:' + s.color + (s.dash ? ';height:2px' : '') + '"></i>' +
               s.label + '</span>';
      }).join('');
    }
    TREND_CHART_STATE[name] = {points: points, series: series, svg: svg, x: x};
  }

  function onTrendMove(name, e){
    const st = TREND_CHART_STATE[name];
    if(!st || !st.points.length) return;
    const fig = document.querySelector('.trend-figure[data-trend-chart="' + name + '"]');
    const rect = st.svg.getBoundingClientRect();
    const frac = (e.clientX - rect.left) / rect.width;
    // Points are spaced by wall-clock time, so invert the pixel x-fraction
    // back to a time and pick the nearest point.
    const pts = st.points;
    const t0 = pts[0].t, tN = pts[pts.length - 1].t;
    const time = t0 + frac * (tN - t0);
    let idx = 0, best = Infinity;
    pts.forEach(function(p, i){
      const d = Math.abs(p.t - time);
      if(d < best){ best = d; idx = i; }
    });
    const line = st.svg.querySelector('.t-crosshair');
    if(line){
      line.setAttribute('x1', st.x(pts[idx].t).toFixed(2));
      line.setAttribute('x2', st.x(pts[idx].t).toFixed(2));
      line.setAttribute('visibility', 'visible');
    }
    const tip = fig.querySelector('[data-trend-tooltip]');
    if(!tip) return;
    tip.innerHTML = '';
    const tick = document.createElement('div');
    tick.className = 'tt-tick';
    tick.textContent = trendTimeLabel(pts[idx].t);
    tip.appendChild(tick);
    st.series.forEach(function(s){
      const row = document.createElement('div');
      row.className = 'tt-row';
      const sw = document.createElement('i');
      sw.style.background = s.color;
      if(s.dash) sw.style.height = '2px';
      const label = document.createElement('span');
      label.textContent = s.label;
      const val = document.createElement('b');
      val.textContent = String(trendNum(st.points[idx][s.key]));
      row.appendChild(sw); row.appendChild(label); row.appendChild(val);
      tip.appendChild(row);
    });
    tip.hidden = false;
    const figRect = fig.getBoundingClientRect();
    tip.style.left = (e.clientX - figRect.left) + 'px';
    tip.style.top = (rect.top - figRect.top) + 'px';
  }

  function onTrendLeave(name){
    const st = TREND_CHART_STATE[name];
    if(!st) return;
    const line = st.svg && st.svg.querySelector('.t-crosshair');
    if(line) line.setAttribute('visibility', 'hidden');
    const tip = document.querySelector('.trend-figure[data-trend-chart="' + name + '"] [data-trend-tooltip]');
    if(tip) tip.hidden = true;
  }

  function buildTrendTable(points){
    const el = document.querySelector('[data-trend-table]');
    if(!el) return;
    const rows = points.slice(-30).reverse();
    const table = document.createElement('table');
    const hr = document.createElement('tr');
    ['时间', '资源', '容量', '工人', '先锋', '游侠', '敌人'].forEach(function(h){
      const th = document.createElement('th');
      th.textContent = h;
      hr.appendChild(th);
    });
    const thead = document.createElement('thead');
    thead.appendChild(hr);
    table.appendChild(thead);
    const tb = document.createElement('tbody');
    rows.forEach(function(p){
      const tr = document.createElement('tr');
      const cells = [trendTimeLabel(p.t), p.r, p.c, p.w, p.v, p.g, p.e];
      cells.forEach(function(v){
        const td = document.createElement('td');
        td.textContent = String(v);
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    table.appendChild(tb);
    el.replaceChildren(table);
  }

  function updateTrendWindowButtons(){
    const panel = document.getElementById('trendsPanel');
    if(!panel) return;
    panel.querySelectorAll('[data-trend-window]').forEach(function(btn){
      btn.classList.toggle('active', Number(btn.getAttribute('data-trend-window')) === trendWindow);
    });
  }

  async function loadTrends(){
    if(document.hidden) return;
    const panel = document.getElementById('trendsPanel');
    if(!panel) return;
    try{
      const res = await fetch('/api/trends?seconds=' + trendWindow + '&ts=' + Date.now(), {cache: 'no-store'});
      if(!res.ok) return;
      const data = await res.json();
      if(!data || !data.points) return;
      if(lastTrendTs !== null && data.lastTs === lastTrendTs) return;
      lastTrendTs = data.lastTs;
      trendPoints = data.points;
      renderTrendCharts();
      buildTrendTable(data.points);
      const range = panel.querySelector('[data-trend-range]');
      if(range){
        range.textContent = data.points.length
          ? (data.points.length + ' 点 · ' + trendTimeLabel(data.points[0].t) +
             ' → ' + trendTimeLabel(data.points[data.points.length - 1].t))
          : '—';
      }
    }catch(e){}
  }

  function renderTrendCharts(){
    Object.keys(TREND_CHARTS).forEach(renderTrendChart);
  }

  function bindTrends(){
    const panel = document.getElementById('trendsPanel');
    if(!panel) return;
    try{
      const saved = localStorage.getItem(TREND_WINDOW_KEY);
      if(saved && TREND_WINDOWS.indexOf(Number(saved)) !== -1) trendWindow = Number(saved);
    }catch(e){}
    updateTrendWindowButtons();
    if(!panel._bound){
      panel._bound = true;
      panel.querySelectorAll('[data-trend-window]').forEach(function(btn){
        btn.addEventListener('click', function(){
          trendWindow = Number(btn.getAttribute('data-trend-window'));
          updateTrendWindowButtons();
          try{ localStorage.setItem(TREND_WINDOW_KEY, String(trendWindow)); }catch(e){}
          lastTrendTs = null;
          loadTrends();
        });
      });
      panel.querySelectorAll('.trend-figure').forEach(function(fig){
        const name = fig.getAttribute('data-trend-chart');
        const svg = fig.querySelector('[data-trend-svg]');
        if(!svg || svg._bound) return;
        svg._bound = true;
        svg.addEventListener('pointermove', function(e){ onTrendMove(name, e); });
        svg.addEventListener('pointerleave', function(){ onTrendLeave(name); });
      });
    }
    loadTrends();
  }

  bindOreForm();
  bindConfigForm();
  bindTeamsBoard();
  bindWaypointPanel();
  bindMapFilters();
  bindLogFilters();
  bindLogTimeFilters();
  bindUnitTabs();
  // ── per-unit 自裁 (self-destruct) buttons ─────────────────────────────
  // Cards are re-rendered on every soft refresh, so bind once on the document
  // and delegate by class.
  document.addEventListener('click', function(e){
    const el = e.target;
    if(!(el instanceof Element)) return;
    const btn = el.closest ? el.closest('.sd-btn') : null;
    if(!btn) return;
    const name = btn.getAttribute('data-sd-unit');
    if(!name) return;
    if(!confirm('确认自裁 ' + name + '？该单位将被立即移除，无法撤销。')) return;
    fetch('/api/unit/self_destruct', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name:name})
    })
    .then(function(res){ return res.json(); })
    .then(function(data){
      if(data && data.ok){ lastTick = null; softRefresh(); }
    })
    .catch(function(){});
  });
  // ── click a unit card in the right rail → jump the map to the unit ─────
  // Cards re-render on every soft refresh, so bind once on the document and
  // delegate by the focus attributes each card carries.
  document.addEventListener('click', function(e){
    const el = e.target;
    if(!(el instanceof Element)) return;
    // The 自裁 button lives inside the card; that click must not move the map.
    if(el.closest('.sd-btn')) return;
    const card = el.closest('[data-focus-wx][data-focus-wy]');
    if(!card) return;
    const wx = Number(card.getAttribute('data-focus-wx'));
    const wy = Number(card.getAttribute('data-focus-wy'));
    if(Number.isFinite(wx) && Number.isFinite(wy)) focusWorld(wx, wy);
  });
  bindTrends();
  loadTeams(true);
  ensureView();
  bindStage();
  bindPickButtons();
  syncPickButtonsDisabled();
  applyMapFilters();
  applyLogFilters();
  apply();
  window.addEventListener('resize', function(){ apply(); });
  window.addEventListener('beforeunload', function(e){
    if(!configDirty && !teamsDirty) return;
    e.preventDefault();
    e.returnValue = '';
  });
  document.addEventListener('keydown', function(e){
    if(e.key === 'Escape' && pickMode) setPickMode(null);
  });
  if(stage){
    stage.addEventListener('wheel', function(e){
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.12 : (1/1.12);
      zoomAt(e.clientX, e.clientY, view.scale * factor);
    }, {passive:false});
  }
  // SSE push replaces the old 2 s poll: the server signals the instant the
  // tactic writes a new tick (or map memory / battle log changes), so the
  // map, panels and log update live. EventSource reconnects on its own; the
  // slow poll stays as a fallback for when the stream is blocked.
  function onStreamTick(){
    softRefresh();
    refreshLogRows();
    loadTrends();
  }
  if(window.EventSource){
    const stream = new EventSource('/api/stream');
    stream.addEventListener('tick', onStreamTick);
  }
  setInterval(softRefresh, 10000);
  setInterval(loadTrends, 10000);
  setInterval(function(){ loadConfig(false); }, 10000);
  setInterval(function(){ loadTeams(false); }, 5000);
})();
</script>
"""


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
        return (
            f'<div class="unit {kind}" title="{safe_act}"{focus_attr}><div class="unit-top">'
            f'<div class="unit-id">{safe_name}<span class="count">{safe_sid}</span></div>'
            f'<span class="unit-actions"><span class="badge {kind}">{html.escape(str(badge))}</span>'
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
        return (
            f'<div class="unit {color_cls}" title="{safe_act}"{focus_attr}><div class="unit-top">'
            f'<div class="unit-id">{safe_name}<span class="count">{safe_sid}</span></div>'
            f'<span class="unit-actions"><span class="badge {color_cls}">{label}</span>'
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


def generate_html() -> str:
    parts = build_parts()
    if not parts:
        return (
            '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
            '<title>战术仪表盘</title><style>' + CSS + '</style></head><body>'
            '<div class="wrap"><div class="card"><h1>暂无数据</h1>'
            '<p class="muted">等待 tactic_log.jsonl 写入…</p></div></div></body></html>'
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><link rel="icon" href="data:,"><title>Arena Hero 战术仪表盘</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">
      <h1>Arena Hero 战术仪表盘</h1>
      <p id="brandLine">{parts['brand']}</p>
    </div>
    <div class="status-pill {parts['statusClass']}" id="statusPill">{parts['statusHtml']}</div>
  </div>

  <div class="main-grid">
    <aside class="side-col" id="leftColumn">{parts['leftHtml']}</aside>

    <section class="center-col">
      <section class="panel map-panel">
       <div class="panel-title"><span>已知地图</span><span class="count" id="mapTitleCount">{parts['mapTitle']}</span></div>
       <div class="hotspot-bar" id="hotspotBar">{parts['hotspotsHtml']}</div>
       <div class="map-toolbar">
        <button type="button" id="zoomOutBtn">-</button>
        <button type="button" id="zoomInBtn">+</button>
        <button type="button" id="resetViewBtn">重置视角</button>
        <button type="button" id="focusCoreBtn">定位核心</button>
        <button type="button" id="refreshMapBtn" title="强制重新拉取并刷新地图">↻ 刷新地图</button>
        <span id="zoomLabel">100%</span>
        <span class="coord-readout" id="mapCoordLabel">坐标 —</span>
       </div>
       <div class="map-stage" id="mapStage">{parts['mapSvg']}</div>
       <div class="map-legend" id="mapLegend">
        <button type="button" class="map-filter" data-cat="core"><i class="dot core"></i>核心</button>
        <button type="button" class="map-filter" data-cat="worker"><i class="dot worker"></i>工人</button>
        <button type="button" class="map-filter" data-cat="vanguard"><i class="dot vg"></i>先锋</button>
        <button type="button" class="map-filter" data-cat="ranger"><i class="dot rg"></i>游侠</button>
        <button type="button" class="map-filter" data-cat="enemy-worker"><i class="dot enemy-worker"></i>敌·工人</button>
        <button type="button" class="map-filter" data-cat="enemy-vanguard"><i class="dot enemy-vanguard"></i>敌·先锋</button>
        <button type="button" class="map-filter" data-cat="enemy-ranger"><i class="dot enemy-ranger"></i>敌·游侠</button>
        <button type="button" class="map-filter" data-cat="enemy-core"><i class="dot enemy-core"></i>敌·核心</button>
        <button type="button" class="map-filter" data-cat="enemy"><i class="dot enemy"></i>敌人(未知)</button>
        <button type="button" class="map-filter" data-cat="combat"><i class="dot combat"></i>战斗</button>
        <button type="button" class="map-filter" data-cat="enemy-trace"><i class="dot enemy-trace"></i>敌人踪迹</button>
        <button type="button" class="map-filter" data-cat="wall"><i class="dot wall"></i>墙</button>
        <button type="button" class="map-filter" data-cat="ore"><i class="dot ore"></i>可见矿</button>
        <button type="button" class="map-filter" data-cat="ore-mem"><i class="dot ore-mem"></i>记忆矿</button>
        <button type="button" class="map-filter" data-cat="route"><i class="dot route-line"></i>路径</button>
        <button type="button" class="map-filter" data-cat="target"><i class="dot target-ring"></i>目标</button>
        <button type="button" class="map-filter" data-cat="beacon"><i class="dot beacon"></i>信标</button>
        <button type="button" class="map-filter" data-cat="attack-target"><i class="dot attack-target"></i>进攻目标</button>
        <button type="button" class="map-filter" data-cat="kite-target"><i class="dot kite-target"></i>风筝目标</button>
        <button type="button" class="map-filter" data-cat="core-target"><i class="dot core-target"></i>核心目标</button>
        <button type="button" class="map-filter" data-cat="wp"><i class="dot wp"></i>手动目标</button>
        <button type="button" class="map-filter-reset" id="mapFilterReset">全部显示</button>
       </div>
      </section>
      {render_teams_panel()}
      {render_config_panel(parts['workersCount'], parts['vgCount'], parts['rgCount'])}
      {render_trends_panel()}
      <section class="panel log-panel" id="logPanel">
        <div class="panel-title"><span>战斗日志</span><span class="count" id="logCount">{parts['logCount']}</span></div>
        <div class="log-filters" id="logFilters">
          <button type="button" class="log-filter" data-log-cat="discover">发现</button>
          <button type="button" class="log-filter" data-log-cat="kill">击杀</button>
          <button type="button" class="log-filter" data-log-cat="defeat">被击败</button>
          <button type="button" class="log-filter" data-log-cat="combat">战斗</button>
          <button type="button" class="log-filter" data-log-cat="economy">经济</button>
          <button type="button" class="log-filter" data-log-cat="config">配置</button>
          <button type="button" class="log-filter" data-log-cat="warn">异常</button>
        </div>
        <div class="log-time-filters" id="logTimeFilters">
          <button type="button" class="log-time-btn" data-log-window="600">10分钟</button>
          <button type="button" class="log-time-btn" data-log-window="1800">30分钟</button>
          <button type="button" class="log-time-btn" data-log-window="3600">1小时</button>
          <button type="button" class="log-time-btn" data-log-window="21600">6小时</button>
          <button type="button" class="log-time-btn" data-log-window="all">全部</button>
          <span class="log-time-custom">
            <input type="number" id="logWindowMinutes" min="1" step="1" placeholder="自定义分钟"
                   aria-label="自定义时间窗口分钟数">
            <button type="button" class="log-time-btn" id="logWindowCustomApply">应用</button>
          </span>
        </div>
        <div class="log-list" id="logSection">{parts['logHtml']}</div>
      </section>
      <div id="issuesSection" style="display:none">{parts['issuesHtml']}</div>
    </section>

    <aside class="side-col">
      <section class="panel units-panel">
        <div class="units-tabs" role="tablist">
          <button type="button" class="unit-tab active" data-unit-tab="workers" role="tab" aria-selected="true">工人<span class="count" id="workersCount">{parts['workersCount']} 个</span></button>
          <button type="button" class="unit-tab" data-unit-tab="vanguards" role="tab" aria-selected="false">先锋<span class="count" id="vgCount">{parts['vgCount']}</span></button>
          <button type="button" class="unit-tab" data-unit-tab="rangers" role="tab" aria-selected="false">游侠<span class="count" id="rgCount">{parts['rgCount']}</span></button>
        </div>
        <div class="unit-tab-pane active" data-unit-pane="workers" role="tabpanel">
          <div class="unit-grid compact-list" id="workersGrid">{parts['workersHtml']}</div>
        </div>
        <div class="unit-tab-pane" data-unit-pane="vanguards" role="tabpanel">
          <div class="unit-grid compact-list" id="vgGrid">{parts['vgHtml']}</div>
        </div>
        <div class="unit-tab-pane" data-unit-pane="rangers" role="tabpanel">
          <div class="unit-grid compact-list" id="rgGrid">{parts['rgHtml']}</div>
        </div>
      </section>
      <div id="waypointSection">{parts['waypointHtml']}</div>
      <section class="panel res-panel" id="resPanel">
        <div class="res-head">
          <div class="panel-title" style="margin-bottom:0"><span>矿点</span><span class="count" id="resCount">{parts['resCount']} 可见</span></div>
          <button type="button" class="add-ore-btn" id="resAddToggle" title="录入矿点">+</button>
        </div>
        <div id="resSection">{parts['resHtml']}</div>
      </section>
    </aside>
  </div>

  <div class="footer" id="footerSection">{parts['footerHtml']}</div>
</div>
{JS}
</body></html>"""



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
            if path == "/" or path == "/dashboard" or path == "/arena" or path.startswith("/arena/"):
                self._send(401, LOGIN_HTML.encode("utf-8"), "text/html; charset=utf-8")
            else:
                self._send_json(401, {"ok": False, "error": "未授权：缺少或错误的 DASHBOARD_TOKEN"})
            return
        if path == "/":
            self._redirect("/dashboard")
            return
        if path == "/dashboard":
            self._send(200, generate_html().encode("utf-8"), "text/html; charset=utf-8")
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
        if path == "/api/enemy-memory":
            # Remembered enemies (last-known positions) from the shared
            # map_memory.json — the same data this dashboard draws as its
            # enemy-trace layer. Enemies currently visible in the latest
            # tick are skipped so the arena page never duplicates markers.
            memory = load_map_memory()
            history = read_history(1)
            rec = history[0] if history else {}
            visible = set()
            for enemy in rec.get("enemies", []) or []:
                pos = enemy.get("pos") or []
                if len(pos) == 2:
                    visible.add((int(pos[0]), int(pos[1])))
            sightings = []
            for item in memory.get("enemy_sightings", []) or []:
                parsed = _parse_enemy_sighting(item)
                if parsed is None or parsed[0] in visible:
                    continue
                sightings.append({"pos": list(parsed[0]), "type": parsed[1]})
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
            x = max(-1000, min(1000, x))
            y = max(-1000, min(1000, y))
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

        if path == "/api/unit/self_destruct":
            try:
                name = _waypoint_name(data.get("name"))
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            result = request_self_destruct(name)
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
