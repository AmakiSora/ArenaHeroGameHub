"""Arena Hero dashboard - JSON control API + arena SPA host on port 4399.

Serves the built React console under /arena, proxies the game API (injecting
the server-side key), and exposes the tactic bot's state as JSON.
Run: python dashboard.py  -> http://localhost:4399
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import wraps
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit

import map_codec
from tactic_config import (
    CONFIG_PATH,
    ConfigValidationError,
    config_schema,
    default_config,
    load_config,
    update_config,
)
from state_io import append_jsonl, atomic_write_text, data_path, file_lock


def _data_path(name: str) -> str:
    # Single source of truth (state_io.data_dir): ARENA_DATA_DIR, else the
    # module directory — never the process cwd, which used to split runtime
    # state from the tactic process's own files.
    return str(data_path(name))


LOG_FILE = _data_path("tactic_log.jsonl")
MAP_FILE = _data_path("map_memory.json")
WAYPOINTS_FILE = _data_path("waypoints.json")
SELF_DESTRUCT_FILE = _data_path("self_destruct.json")
HOLDS_FILE = _data_path("holds.json")
BATTLE_LOG_FILE = _data_path("battle_log.jsonl")
def _env_host() -> str:
    return os.environ.get("HOST", "").strip() or "0.0.0.0"


def _env_port() -> int:
    raw = os.environ.get("PORT", "").strip()
    if not raw:
        return 4399
    try:
        return int(raw)
    except ValueError:
        print(f"[dashboard] ignoring non-integer PORT={raw!r}; using 4399", flush=True)
        return 4399


# Bind address. Both were hard-coded before, so the HOST/PORT documented in
# compose.yml, the Dockerfile and the README were silently ignored.
HOST = _env_host()
PORT = _env_port()
# Auth gate: requests from outside must present this token (cookie / Bearer /
# ?token=). Empty => auth disabled (local dev without env). From loopback only
# the two liveness probes (GET /api/health, GET /api/state) skip the token, so
# the container healthcheck and deploy smoke-tests keep working.
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


# Vite emits content-hashed bundles (assets/<name>-<hash>.js/css/woff2); only
# those may be cached forever. Unhashed files (index.html, assets/game art)
# must revalidate so a deploy or sprite swap takes effect on the next load.
_ARENA_HASHED_ASSET = re.compile(r"^assets/(?!game/).+-[A-Za-z0-9_-]{8}\.(?:js|css|woff2?)$")
_GZIP_TYPES = {"text/html", "text/css", "text/javascript", "application/javascript", "application/json", "image/svg+xml", "text/plain"}


# Upstream endpoints the browser may reach through the dashboard proxy.
_ARENA_GET_PATHS = {"/api/v1/leaderboard", "/api/v1/me", "/api/v1/me/stats"}
_ARENA_POST_PATHS = {"/api/v1/game/commands", "/api/v1/auth/login", "/api/v1/auth/logout"}
# Extra cookie the proxy uses to carry the upstream CSRF token alongside the
# imported session cookie; never forwarded as-is, never readable by JS.
_ARENA_CSRF_COOKIE = "arena_csrf"
_ARENA_WS_PATH = "/api/v1/game/ws"
_WS_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# Remembered session-import draft (raw upstream cookies + CSRF). Kept behind
# DASHBOARD_TOKEN instead of browser localStorage, where any XSS could read it.
# Persisted to the runtime dir (0600) so a deploy/restart does not lose a
# still-valid upstream session; the operator still re-imports once the upstream
# session itself expires (the official API has no refresh endpoint).
SESSION_DRAFT_FILE = _data_path("session_draft.json")
# Imported cookies stay in the browser for 30 days. The upstream still decides
# whether the session is valid; this only stops a browser restart from dropping
# a cookie that would otherwise still work.
_SESSION_COOKIE_MAX_AGE = 30 * 24 * 60 * 60


def _load_session_draft() -> dict[str, str] | None:
    try:
        data = json.loads(Path(SESSION_DRAFT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    cookies = str(data.get("cookies", "")).strip()
    csrf = str(data.get("csrf", "")).strip()
    if not cookies or not csrf or len(cookies) > 8192 or len(csrf) > 512:
        return None
    return {"cookies": cookies, "csrf": csrf}


def _save_session_draft(draft: dict[str, str] | None) -> None:
    global _SESSION_DRAFT
    _SESSION_DRAFT = draft
    path = Path(SESSION_DRAFT_FILE)
    if draft is None:
        try:
            path.unlink()
        except OSError:
            pass
        return
    atomic_write_text(path, json.dumps(draft, ensure_ascii=False))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


_SESSION_DRAFT: dict[str, str] | None = _load_session_draft()


# ---------- security / freshness knobs ------------------------------------
# Content-Security-Policy for every response this server produces. The Vite
# SPA loads only same-origin bundles (web/dist/index.html carries no inline
# script, only module/link tags), so script-src stays strict. Styles need
# 'unsafe-inline' because the build inlines critical CSS, art/fonts are data:
# or same-origin, and connect-src must allow the ws proxy on this origin.
_CSP = (
    "default-src 'self'; img-src 'self' data: blob:; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "connect-src 'self' ws: wss:; font-src 'self' data:; frame-ancestors 'none'"
)
# LOGIN_HTML is one static document with a single inline submit handler, so it
# gets its own policy: the SPA keeps the strict script-src above.
_CSP_LOGIN = _CSP.replace("script-src 'self';", "script-src 'self' 'unsafe-inline';")
_SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
)
# 415 body for a POST whose body is not declared application/json.
_JSON_ONLY_ERROR = {"ok": False, "error": "content-type must be application/json"}
# Upper bound for a request body: the largest legitimate payload is a config
# save or a session import, so anything bigger is never buffered.
_MAX_BODY_BYTES = 1_048_576


def _split_origin(value: str) -> tuple[str, str]:
    """Split an Origin URL or a Host header into (scheme, "host:port").

    Ports are defaulted from the scheme (https/wss 443, everything else 80) so
    ``https://ops.example.com`` and ``https://ops.example.com:443`` compare
    equal. Returns ("", "") for values that cannot name an origin, including
    the literal ``null`` origin a sandboxed document sends.
    """
    raw = (value or "").strip().lower()
    if not raw or raw == "null":
        return "", ""
    if "//" not in raw:
        raw = "//" + raw
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return "", ""
    host = parts.hostname or ""
    if not host:
        return "", ""
    if port is None:
        port = 443 if parts.scheme in ("https", "wss") else 80
    return parts.scheme, f"{host}:{port}"


def _origin_key(value: str) -> str:
    """Normalize an origin to ``scheme://host:port`` ("" when unparseable)."""
    scheme, host = _split_origin(value)
    return f"{scheme or 'http'}://{host}" if host else ""


def _origin_host(value: str) -> str:
    """``host:port`` of an origin/Host header ("" when unparseable)."""
    return _split_origin(value)[1]


# Extra origins allowed to reach state-changing endpoints (comma separated,
# compared case-insensitively on scheme+host+port). Same-origin requests are
# always accepted, and requests carrying no Origin header at all (curl, the
# Docker healthcheck, server-to-server calls, deploy smoke tests) are not a
# browser CSRF vector and stay allowed.
DASHBOARD_ALLOWED_ORIGINS = frozenset(
    key for key in (_origin_key(item) for item in
                    os.environ.get("DASHBOARD_ALLOWED_ORIGINS", "").split(","))
    if key
)


def _env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# /api/health answers 503 once the newest tick is older than this: a container
# whose tactic process died (or never started) must not report healthy.
DASHBOARD_STALE_TICK_SECONDS = _env_seconds("DASHBOARD_STALE_TICK_SECONDS", 300.0)


def _accepts_gzip(value: str) -> bool:
    """True when Accept-Encoding actually allows gzip (q > 0).

    A plain ``"gzip" in header`` test treats ``gzip;q=0`` (an explicit refusal)
    as consent; parse the weights instead, letting an explicit gzip entry win
    over a ``*`` wildcard.
    """
    wildcard = None
    for chunk in (value or "").split(","):
        name, _, params = chunk.strip().partition(";")
        name = name.strip().lower()
        if not name:
            continue
        quality = 1.0
        for param in params.split(";"):
            key, _, raw = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(raw.strip())
                except (TypeError, ValueError):
                    quality = 0.0
        if name == "gzip":
            return quality > 0
        if name == "*":
            wildcard = quality
    return wildcard is not None and wildcard > 0


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


# The tick history is re-read on every poll (the SPA asks for the newest record
# several times per tick) and always asks for the newest N tick records. Between
# polls with no new tick the log file is unchanged, so re-reading + re-parsing
# those (large) records is pure overhead. Cache the result by (log signature,
# ticks); a single appended tick bumps the signature and invalidates the entry.
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


# Keyed by (path, max_points, hours) -> (signature, built_at, payload). The
# route file gains rows every Tick, so a signature-only cache would re-parse
# several MB on every poll; the tracks layer is a review tool, so one refresh
# per minute is plenty and keeps this process off the tactic bot's CPU.
# The requested window stays in the key because the payload is filtered by it,
# but the derived wall-clock ``since_epoch`` does NOT: keying on
# int(time.time()) made every request a fresh miss, so the throttle never hit
# and every poll re-read the whole day file.
_guerrilla_track_cache: dict[tuple, tuple[tuple[int, int] | None, float, dict]] = {}
GUERRILLA_TRACK_REFRESH_SECONDS = 60.0
# Hard bound on rows scanned from one day file (newest→oldest). A full day is
# ~100k rows at the live Tick cycle; the cap only guards against a pathological
# file while keeping the response bounded.
GUERRILLA_TRACK_MAX_ROWS = 400_000


def read_guerrilla_tracks(
    date: str | None = None,
    max_points: int = GUERRILLA_TRACK_MAX_POINTS,
    hours: int | None = None,
) -> dict:
    """Aggregate one day's route rows into a polyline per guerrilla unit.

    Consecutive duplicate cells are folded first (a unit that WAITs or is
    refused its MOVE still writes a row every Tick), then the remaining walk is
    strided down to ``max_points`` with the newest cell always kept.

    Rows are streamed newest-first with the shared reverse tail reader and the
    scan stops at the ``?hours=N`` cutoff, so the whole file is never loaded
    into memory as one string.
    """
    files = _guerrilla_route_files()
    dates = [stamp for stamp, _ in files]
    if not files:
        return {"ok": True, "date": None, "dates": [], "units": []}
    if date is None or date not in dates:
        date, path = files[0]
    else:
        path = next(p for stamp, p in files if stamp == date)
    window_hours = int(hours or 0)
    signature = _guerrilla_route_signature(path)
    key = (path, int(max_points), window_hours)
    cached = _guerrilla_track_cache.get(key)
    if cached is not None:
        cached_sig, built_at, payload = cached
        throttled = time.monotonic() - built_at < GUERRILLA_TRACK_REFRESH_SECONDS
        if throttled or cached_sig == signature:
            return {**payload, "dates": dates}

    since_epoch = int(time.time()) - window_hours * 3600 if window_hours > 0 else None
    tracks: dict[str, dict] = {}
    rows = 0
    try:
        for line in _iter_log_lines_reverse(path):
            rows += 1
            if rows > GUERRILLA_TRACK_MAX_ROWS:
                break
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
                # Rows are time-ordered: every remaining row is older still.
                break
            track = tracks.get(uid)
            if track is None:
                track = tracks[uid] = {
                    "id": uid,
                    "name": str(row.get("u") or uid),
                    "type": "RANGER" if str(row.get("u") or "").upper().startswith("R") else "VANGUARD",
                    "cells": [],
                    "distinct": set(),
                    "ticks": 0,
                    # Reading newest-first: last_* comes from the first row seen,
                    # first_* keeps being overwritten down to the oldest row.
                    "first_tick": row.get("t"),
                    "last_tick": row.get("t"),
                    "first_epoch": row.get("e"),
                    "last_epoch": row.get("e"),
                }
            cell = (int(pos[0]), int(pos[1]))
            track["ticks"] += 1
            track["first_tick"] = row.get("t")
            track["first_epoch"] = row.get("e")
            track["distinct"].add(cell)
            if not track["cells"] or track["cells"][-1] != cell:
                track["cells"].append(cell)
    except OSError:
        pass

    units = []
    for track in tracks.values():
        # The cells were appended newest-first; the rendered walk is oldest-first.
        cells = track["cells"][::-1]
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


def _tick_health() -> dict:
    """Liveness + tick-freshness verdict for /api/health.

    The Docker healthcheck, the deploy smoke test and the watchdog all use this
    endpoint, so a container whose tactic process died (or never started) must
    stop looking healthy. Freshness is reported in the body and in the status
    code: 503 + ``ok: false`` once the newest tick is older than
    DASHBOARD_STALE_TICK_SECONDS (default 300 s).

    Age comes from the newest tick record's ISO ``timestamp`` (the wall-clock
    time the bot produced it); when the record has no parseable timestamp the
    log file's mtime is used instead, so at least a written log counts.
    """
    history = read_history(1)
    rec = history[0] if history else None
    stamp = _parse_iso_ts(rec.get("timestamp")) if rec else None
    if stamp is None:
        try:
            stamp = os.path.getmtime(LOG_FILE)
        except OSError:
            stamp = None
    age = None if stamp is None else max(0.0, time.time() - stamp)
    alive = age is not None and age <= DASHBOARD_STALE_TICK_SECONDS
    return {
        "ok": alive,
        "tick": rec.get("tick") if rec else None,
        "tick_age_s": None if age is None else round(age, 1),
        "tactic_alive": alive,
        "stale_after_s": DASHBOARD_STALE_TICK_SECONDS,
    }


# ── Categorized battle log (「战斗日志」panel below the config) ──────────────
# The tactic process appends discovery/combat/economy/failure rows each tick;
# the dashboard process appends config-change rows on save.  Filter chips in
# the panel map to the entry "cat" field.


# Log-category chips shown in the panel header (label + default visibility).
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

    The arena page under /arena renders its own panel and needs plain JSON
    (the retired server-rendered HTML panel is gone). Only the four fields the
    panel uses are sent, so raw rows never leak extra keys.
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


def _clamp_log_limit(request) -> int:
    """Parse the ``?log=`` / ``?limit=`` query into a bounded row count.

    The arena page scales how many battle-log rows it wants; the cap keeps a
    single poll from serializing the whole file (which can hold tens of
    thousands of lines before the 2 MB trim kicks in). The floor is 1, not 200:
    ``?limit=1`` must actually return one row (a "newest line only" probe).
    """
    try:
        qs = parse_qs(urlsplit(request.path).query)
        n = int((qs.get("log") or qs.get("limit") or ["200"])[0])
    except (TypeError, ValueError):
        return 200
    return max(1, min(n, 8000))


def _empty_map() -> dict:
    return {
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


def _load_map_json() -> tuple[dict, bool]:
    """Load raw map_memory.json as (data, cacheable).

    "missing" and "unreadable" are distinct: a missing file is a stable state
    whose empty result may be cached, while a parse failure is usually a torn
    read racing the tactic's rewrite — caching that empty structure would
    poison the signature cache until the file changes again (and report zero
    resources / walls for a whole polling interval).
    """
    empty = _empty_map()
    try:
        with open(MAP_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except FileNotFoundError:
        return empty, True
    except Exception:
        return empty, False
    if isinstance(loaded, dict):
        empty.update(loaded)
    return empty, True


def _read_map_file() -> dict:
    """Load raw map_memory.json, or an empty structure when missing/invalid.

    Mutating callers (save/remove/clear) want the raw document and rewrite it
    wholesale; they deliberately ignore the cacheable flag because the next
    write replaces the file anyway.
    """
    return _load_map_json()[0]


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


def _obstacle_fields(data: dict) -> dict:
    """Carry the on-disk obstacle representation through a dashboard edit.

    Tactic may have written the packed form; decoding and re-encoding it just
    to add a resource would rewrite a multi-MB blob on every click, so copy
    the fields verbatim. The plain list remains for legacy/small maps.
    """
    blob = data.get(map_codec.OBSTACLE_RLE_KEY)
    if isinstance(blob, str) and blob:
        return {
            map_codec.OBSTACLE_RLE_KEY: blob,
            map_codec.OBSTACLE_ENCODING_KEY: data.get(
                map_codec.OBSTACLE_ENCODING_KEY, map_codec.OBSTACLE_ENCODING
            ),
        }
    return {"obstacles": data.get("obstacles", [])}


# ── remembered enemy sightings (last-known positions + unit type) ───────────


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
    d, cacheable = _load_map_json()
    forgotten = _coord_set(d.get("forgotten_resources"))
    resources = _coord_set(d.get("resources")) - forgotten
    manual = _coord_set(d.get("manual_resources")) - forgotten
    all_res = sorted(resources | manual)
    enemy_sightings = [
        s for s in (_parse_enemy_sighting(item) for item in d.get("enemy_sightings", []) or [])
        if s is not None
    ]
    enemy_sightings.sort(key=lambda s: s[0])
    obstacles = sorted(map_codec.decode_obstacles(d))
    result = {
        "obstacles": obstacles,
        "resources": all_res,
        "manual_resources": sorted(manual),
        "forgotten_resources": sorted(forgotten),
        "enemy_sightings": [
            {"pos": pos, "type": etype, "tick": tick}
            for pos, etype, tick in enemy_sightings
        ],
        "obstacle_count": d.get("obstacle_count", len(obstacles)),
        "resource_count": len(all_res),
        "manual_count": len(manual),
        "enemy_clear_seq": int(d.get("enemy_clear_seq", 0) or 0),
        "updated_tick": d.get("updated_tick"),
    }
    if sig is not None and cacheable:
        # Keep only the newest entry so a rewritten map frees the stale parse.
        _map_memory_cache.clear()
        _map_memory_cache[sig] = result
    return result


# /api/enemy-memory is polled every tick but only needs the sighting list.
# Reaching it through load_map_memory() decodes and sorts every remembered wall
# cell (~450k cells measured at 0.73 s cold / 65 MB peak) — and because the
# tactic rewrites map_memory.json every few ticks, the cache misses exactly when
# the page polls. Read the raw JSON and keep only the small fields this endpoint
# returns, behind its own single-slot signature cache.
_enemy_sightings_cache: dict[tuple[int, int], dict] = {}


def load_enemy_sightings_memory() -> dict:
    """Cheap ``{enemy_sightings, enemy_clear_seq}`` view of map_memory.json."""
    sig = _map_file_signature()
    if sig is not None:
        cached = _enemy_sightings_cache.get(sig)
        if cached is not None:
            return cached
    data, cacheable = _load_map_json()
    parsed = [
        s for s in (
            _parse_enemy_sighting(item) for item in data.get("enemy_sightings", []) or []
        )
        if s is not None
    ]
    parsed.sort(key=lambda s: s[0])
    result = {
        "enemy_sightings": [
            {"pos": pos, "type": etype, "tick": tick}
            for pos, etype, tick in parsed
        ],
        "enemy_clear_seq": int(data.get("enemy_clear_seq", 0) or 0),
    }
    if sig is not None and cacheable:
        _enemy_sightings_cache.clear()
        _enemy_sightings_cache[sig] = result
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
        **_obstacle_fields(data),
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
        **_obstacle_fields(data),
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
        **_obstacle_fields(data),
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
        **_obstacle_fields(data),
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


def short_id(uid: str) -> str:
    return (uid or "?")[:8]


# ---------- planned route helpers ----------------------------------------
# Kept from the retired SVG renderer: /api/unit-routes serves the same trimmed
# polyline to the arena SPA's 路径 layer.


def _remaining_path(unit_data: dict, fallback_path: list | None = None) -> list:
    """Return the planned route minus the cells the unit has already walked.

    The planner re-emits the full A* polyline every tick while a unit advances
    along a cached path, so the log record's ``path`` is stale — it still
    contains the origin the unit started from. Trim everything up to and
    including the unit's current cell so the map and step counts only show the
    route that is actually still ahead.

    ``fallback_path`` is the route recovered from an older record when the
    newest one logged ``path: null`` (unchanged since the previous Tick); see
    :func:`_resolve_unit_paths`.
    """
    source = unit_data.get("path")
    if not source:
        source = fallback_path or []
    path = []
    for p in source:
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


# How many older tick records the route resolver may scan. tactic.py omits a
# route when it is unchanged since the previous Tick, so a unit on a long march
# can point at a record dozens of ticks old; beyond this the route layer simply
# shows the target marker (target is always logged).
_ROUTE_LOOKBACK_RECORDS = 60
_UNIT_ROUTE_GROUPS = (("workers", "WORKER"), ("vanguards", "VANGUARD"), ("rangers", "RANGER"))


def _resolve_unit_paths(rec: dict) -> dict[str, list]:
    """Recover routes omitted from the newest tick record.

    Returns ``{unit_id: full_path}`` for every unit whose newest entry carries
    ``path: null`` with a non-zero ``path_len``. The scan is newest-first and
    bounded; a unit with no recoverable route inside the window is simply left
    out (callers render its target marker instead).
    """
    missing: set[str] = set()
    for group, _etype in _UNIT_ROUTE_GROUPS:
        for raw in rec.get(group) or ():
            if (
                isinstance(raw, dict)
                and raw.get("path") is None
                and raw.get("path_len")
            ):
                missing.add(str(raw.get("id") or ""))
    if not missing:
        return {}
    found: dict[str, list] = {}
    records = 0
    try:
        for line in _iter_log_lines_reverse(LOG_FILE):
            try:
                older = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(older, dict) or "workers" not in older:
                continue
            records += 1
            for group, _etype in _UNIT_ROUTE_GROUPS:
                for raw in older.get(group) or ():
                    if not isinstance(raw, dict):
                        continue
                    uid = str(raw.get("id") or "")
                    if uid in missing and raw.get("path"):
                        found[uid] = raw["path"]
                        missing.discard(uid)
            if not missing or records >= _ROUTE_LOOKBACK_RECORDS:
                break
    except OSError:
        pass
    return found

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
    # Release idle / slow-loris connections instead of letting one blocked
    # readline() pin a worker thread forever. Long-lived upgrades (WebSocket)
    # restore blocking mode explicitly before they take over the socket.
    timeout = 30
    # Keep-alive (HTTP/1.1) is safe here because every response path either
    # sends an exact Content-Length (_send/_redirect) or takes over the socket
    # (WebSocket) / closes the connection explicitly. It saves a TCP handshake
    # per poll for the SPA's 8-per-tick fetch fan-out.
    protocol_version = "HTTP/1.1"
    # Do not advertise the interpreter: "arena" instead of
    # "BaseHTTP/0.6 Python/3.x" (version fingerprinting for scanners).
    server_version = "arena"
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version

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

    def _is_liveness_probe(self) -> bool:
        """True only for the two unauthenticated GET probes on loopback.

        The container healthcheck and the deploy smoke test call the dashboard
        from the host with no token, so loopback keeps exactly ``GET
        /api/health`` and ``GET /api/state``. Any other method or path from
        loopback needs DASHBOARD_TOKEN: a local process (or anything sharing the
        operator's machine / the container network) must not be able to drive
        the bot just because it can reach 127.0.0.1.
        """
        if str(getattr(self, "command", "") or "").upper() != "GET":
            return False
        return urlparse(self.path).path in ("/api/health", "/api/state")

    def _authed(self) -> bool:
        if self._is_loopback() and self._is_liveness_probe():
            return True
        return self._token_ok(self._client_token())

    # ---- CSRF / Origin gate ------------------------------------------------

    def _origin_ok(self) -> bool:
        """Reject cross-site state changes that a browser would happily send.

        No CORS preflight guards a simple POST or a WebSocket upgrade, and the
        server-side game API key is attached to proxied requests, so the only
        trustworthy signal is Origin.

        Rules:
        * no Origin header -> allowed (curl, healthchecks, server-to-server,
          the deploy smoke test); not reachable from a browser page;
        * Origin host:port == Host header host:port -> same-origin, allowed;
        * Origin listed in DASHBOARD_ALLOWED_ORIGINS -> allowed (explicit
          operator opt-in for a separate console origin).
        """
        headers = getattr(self, "headers", None)
        if headers is None:
            return True
        origin = (headers.get("Origin") or "").strip()
        if not origin:
            return True
        request_host = _origin_host(headers.get("Host") or "")
        if request_host and _origin_host(origin) == request_host:
            return True
        return _origin_key(origin) in DASHBOARD_ALLOWED_ORIGINS

    def _origin_reject(self) -> bool:
        """Answer 403 + JSON when the Origin is not acceptable; True if rejected."""
        if self._origin_ok():
            return False
        # The body was not read (the gate runs first), so the socket must not be
        # reused for another request: an unread body would be parsed as one.
        self.close_connection = True
        self._send_json(403, {"ok": False, "error": "origin rejected"})
        return True

    def _send(self, code: int, body: bytes, content_type: str, extra_headers=None,
              cache_control: str = "no-store", csp: str | None = _CSP,
              gzip_body: bytes | None = None):
        try:
            # Text payloads (SPA bundles, JSON) compress ~3x; skip small bodies
            # and already-compressed formats (png/webp/woff2). ``gzip_body`` is
            # a pre-built *.gz sibling of the static asset being served (built
            # by the frontend pipeline); it is only used when the client asks.
            accept = self.headers.get("Accept-Encoding", "") if getattr(self, "headers", None) else ""
            base_type = content_type.split(";", 1)[0].strip()
            gzip_ok = base_type in _GZIP_TYPES and len(body) > 1024 and _accepts_gzip(accept)
            compressed = False
            if gzip_ok and gzip_body is not None:
                body = gzip_body
                compressed = True
            elif gzip_ok:
                body = gzip.compress(body, compresslevel=6)
                compressed = True
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", cache_control)
            if base_type in _GZIP_TYPES:
                self.send_header("Vary", "Accept-Encoding")
            if compressed:
                self.send_header("Content-Encoding", "gzip")
            for name, value in extra_headers or ():
                self.send_header(name, value)
            for name, value in _SECURITY_HEADERS:
                self.send_header(name, value)
            if csp:
                self.send_header("Content-Security-Policy", csp)
            # getattr: BaseHTTPRequestHandler.setup() always sets this, but unit
            # harnesses build the handler with __new__ and never run setup().
            if getattr(self, "close_connection", True):
                # The socket is about to be closed (rejected request, oversized
                # body, WS takeover); say so instead of letting a keep-alive
                # client send another request into a dying connection.
                self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # Client disconnected mid-response; nothing to do.
            pass

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        """Minimal access log: 1xx/4xx/5xx only, one line to stderr.

        BaseHTTPRequestHandler calls this from send_response() with
        (requestline, code, size) for every response. Healthy 2xx/3xx traffic
        stays silent (the SPA polls several endpoints per tick), while auth
        failures, CSRF/Origin rejections, unsupported media types and server
        errors remain visible for ops.
        """
        code = 0
        if len(args) >= 2:
            try:
                code = int(args[1])
            except (TypeError, ValueError):
                code = 0
        if 200 <= code < 400:
            return
        try:
            print(f"[access] {format % args}", file=sys.stderr, flush=True)
        except Exception:
            pass

    def _read_raw_body(self) -> bytes:
        """Read the declared request body (bounded), never desynchronizing.

        With keep-alive on, a body that is only partially consumed would be
        parsed as the next request line, so an oversized / truncated / unknown
        framing marks the connection for closing after the reply.
        """
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except (ValueError, TypeError):
            # Malformed Content-Length (scanners / broken clients): treat as an
            # empty body rather than letting ValueError print a full traceback,
            # but do not reuse a socket whose framing we cannot trust.
            self.close_connection = True
            return b""
        if length <= 0:
            return b""
        if length > _MAX_BODY_BYTES:
            # No endpoint here accepts a body this large; the unread remainder
            # would land in the next request, so read what we can and close.
            self.close_connection = True
            return self.rfile.read(_MAX_BODY_BYTES)
        data = self.rfile.read(length)
        if len(data) < length:
            self.close_connection = True
        return data

    @staticmethod
    def _parse_body(raw: bytes) -> dict:
        try:
            parsed = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _read_json_body(self, raw: bytes | None = None) -> tuple[int, dict]:
        """Read the POST body as JSON; (200, payload) or (415, {}) for a bad type.

        A body that is present must be declared ``application/json``; otherwise
        a cross-site ``text/plain`` POST (a CORS "simple request" the browser
        sends without preflight) could still be parsed as JSON. An empty body
        needs no Content-Type at all (``/api/config/reset`` sends none).

        ``raw`` lets a dispatcher that already consumed the request stream (the
        proxy paths forward the bytes untouched) reuse them: the stream can only
        be read once.
        """
        if raw is None:
            raw = self._read_raw_body()
        if raw:
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                return 415, {}
        return 200, self._parse_body(raw)

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _redirect(self, location: str) -> None:
        try:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            for name, value in _SECURITY_HEADERS:
                self.send_header(name, value)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

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
        # the parent check also blocks ../ traversal attempts. Asset paths
        # never fall back: a stale hash must 404, not cache HTML as immutable JS.
        if target != dist_root / "index.html" and not (target.is_file() and dist_root in target.parents):
            if rel.startswith("assets/"):
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            target = dist_root / "index.html"
        try:
            body = target.read_bytes()
        except OSError:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        mime = _ARENA_MIME.get(target.suffix.lower(), "application/octet-stream")
        served_rel = target.relative_to(dist_root).as_posix()
        cache_control = "public, max-age=31536000, immutable" if _ARENA_HASHED_ASSET.match(served_rel) else "no-cache"
        # A *.gz sibling ships with the build (pre-compressed asset); reusing it
        # avoids re-gzipping the same multi-hundred-KB bundle on every request.
        # Only trust it when it is at least as new as the file it compresses, so
        # a stale sidecar from an older build can never be served.
        gz_body = None
        gz_path = Path(str(target) + ".gz")
        try:
            if gz_path.is_file() and gz_path.stat().st_mtime >= target.stat().st_mtime:
                gz_body = gz_path.read_bytes()
        except OSError:
            gz_body = None
        self._send(200, body, mime, cache_control=cache_control, gzip_body=gz_body)

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
            error_headers = list((exc.headers or {}).items())
            try:
                # HTTPError wraps the live response socket: without an explicit
                # close every upstream 4xx/5xx keeps one fd until the GC runs
                # (CPython 3.14 also warns about the implicit cleanup).
                exc.close()
            except Exception:
                pass
            return exc.code, error_headers, payload
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
        attrs = []
        for part in value.split(";")[1:]:
            attr = part.strip()
            name, sep, _ = attr.partition("=")
            if sep and name.strip().lower() in ("max-age", "expires"):
                attrs.append(attr)
        if not attrs:
            attrs.append(f"Max-Age={_SESSION_COOKIE_MAX_AGE}")
        return "; ".join([name_value, "Path=/", "HttpOnly", "SameSite=Lax", *attrs])

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
            try:
                # Same fd discipline as _arena_upstream: a rejected validation
                # must not park the upstream socket until the GC runs.
                exc.close()
            except Exception:
                pass
            self._send_json(401, {"ok": False, "error": "SESSION_IMPORT_EXPIRED"})
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._send_json(502, {"ok": False, "error": f"上游不可达: {exc}"})
            return
        max_age = f"Max-Age={_SESSION_COOKIE_MAX_AGE}"
        set_cookies = [
            ("Set-Cookie", f"{n}={v}; Path=/; HttpOnly; SameSite=Lax; {max_age}")
            for n, v in pairs
        ]
        if csrf:
            # Bind the CSRF token to the session cookie itself: the proxy
            # attaches it as X-CSRF-Token on session POSTs, so a stale
            # browser-side token can never outlive the session it came from.
            set_cookies.append((
                "Set-Cookie",
                f"{_ARENA_CSRF_COOKIE}={csrf}; Path=/; HttpOnly; SameSite=Lax; {max_age}",
            ))
        try:
            user = json.loads(user_body.decode("utf-8"))
        except Exception:
            user = {}
        print(f"[arena-proxy] session import ok for {user.get('username', '?')} "
              f"({len(pairs)} cookie(s), csrf={'yes' if csrf else 'no'})", flush=True)
        _save_session_draft({"cookies": cookie_header, "csrf": csrf})
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
        # Successful forwards stay silent (one line per failed command only).
        if status < 400:
            return
        try:
            snippet = body[:240].decode("utf-8", "replace")
        except Exception:
            snippet = ""
        print(f"[arena-proxy] {method} {path} -> {status} {snippet}", file=sys.stderr, flush=True)

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
        status, headers, body = self._arena_upstream("POST", path, body=raw, extra_headers=extra)
        self._log_proxy("POST", path, status, body)
        self._forward_upstream_response(status, headers, body)

    # ---- WebSocket transparent proxy (/api/v1/game/ws) ----------------------

    def _proxy_game_ws(self):
        """Pipe browser <-> game API. The upstream socket carries the API key;
        frames move byte-for-byte in both directions without being parsed."""
        # Browsers never preflight a WebSocket upgrade, so an Origin check is
        # the only thing standing between a hostile page and a pipe that
        # carries the server-side game key.
        if self._origin_reject():
            return
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
            print(f"[arena-proxy] WS handshake failed: {exc}", file=sys.stderr, flush=True)
            self._send(502, json.dumps({"error": f"上游不可达: {exc}"}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        if upstream is None:
            # Non-101 upstream reply (401/409/429 ...): surface its body.
            status, body, ctype = upstream_err
            print(f"[arena-proxy] WS handshake rejected upstream -> {status} "
                  f"{body[:160].decode('utf-8', 'replace')}", file=sys.stderr, flush=True)
            self._send(status, body, ctype)
            return
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
        # The request-read timeout must not apply to a long-lived WebSocket:
        # an idle watcher sends no frames for minutes, and a recv() timeout
        # here would tear down the whole pipe. Restore blocking mode.
        try:
            self.connection.settimeout(None)
        except OSError:
            pass
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

    # ---- request dispatchers -----------------------------------------------

    def do_GET(self):
        try:
            self._handle_get()
        except Exception:
            self._internal_error()

    def do_POST(self):
        try:
            self._handle_post()
        except Exception:
            self._internal_error()

    def _internal_error(self) -> None:
        """Turn an unexpected handler exception into a 500 instead of a drop.

        BaseHTTPRequestHandler already closes the connection on an uncaught
        exception, but the client (SPA fetch / Docker healthcheck) then sees a
        transport error rather than a diagnosable response. Log the traceback
        and answer with a small JSON error.
        """
        traceback.print_exc(file=sys.stderr)
        try:
            self._send_json(500, {"ok": False, "error": "internal"})
        except Exception:
            pass

    def _body_framing_rejected(self) -> bool:
        """411 for a chunked request body we cannot delimit.

        With keep-alive on, an unread chunked body would be parsed as the next
        request; refuse it and close instead of desynchronizing the socket.
        """
        if not (self.headers.get("Transfer-Encoding") or "").strip():
            return False
        self.close_connection = True
        self._send_json(411, {"ok": False, "error": "transfer-encoding unsupported"})
        return True

    def _handle_get(self):
        if self._body_framing_rejected():
            return
        # A GET carrying a body is unusual, but leaving its bytes in the socket
        # would desynchronize the next request on a keep-alive connection.
        self._read_raw_body()
        path = urlparse(self.path).path
        if not self._authed():
            if path == "/" or path == "/arena" or path.startswith("/arena/"):
                self._send(401, LOGIN_HTML.encode("utf-8"), "text/html; charset=utf-8",
                           csp=_CSP_LOGIN)
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
        if path == "/api/session/draft":
            self._send_json(200, {"ok": True, "draft": _SESSION_DRAFT})
            return
        if path in _ARENA_GET_PATHS:
            self._proxy_arena_get(path)
            return
        if path == "/api/health":
            # Liveness probe for the container healthcheck, the deploy smoke
            # test and the watchdog: cheap (one stat + one log tail) but it also
            # reports tick freshness, so a container whose tactic died is no
            # longer "healthy". 503 + ok:false once the newest tick is stale.
            health = _tick_health()
            self._send_json(200 if health["ok"] else 503, health)
            return
        if path == "/api/state":
            # Compact probe only: the old ?full=1 server-rendered payload (a
            # single SVG of every remembered wall, ~20 MB / seconds of CPU) is
            # gone. The query param is still accepted and ignored so external
            # scripts and old deploy helpers that pass it keep working.
            history = read_history(1)
            self._send_json(200, {
                "ok": True,
                "tick": history[0].get("tick") if history else None,
                "compact": True,
            })
            return
        if path == "/api/battle-log":
            # JSON rows for the arena SPA's battle-log panel.
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
            # map_memory.json. Enemies currently visible in the latest tick are
            # skipped so the arena page never duplicates markers. Ranked by
            # last-seen tick and capped per type so each filter combination on
            # the arena page can fill its own 21 slots.
            memory = load_enemy_sightings_memory()
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
            # Routes unchanged since the previous tick are logged as
            # ``path: null`` (tactic.py keeps a 53 KB record down to ~8 KB);
            # recover the coordinates from an older record in the log tail.
            recovered = _resolve_unit_paths(rec)
            units = []
            for group, etype in _UNIT_ROUTE_GROUPS:
                for index, raw in enumerate(rec.get(group) or []):
                    if not isinstance(raw, dict):
                        continue
                    pos = raw.get("pos") or []
                    target = raw.get("target") or []
                    path = _remaining_path(
                        raw, recovered.get(str(raw.get("id") or "")),
                    )
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

    def _handle_post(self):
        if self._body_framing_rejected():
            return
        path = urlparse(self.path).path
        # Every POST either mutates shared state or proxies a game command, so
        # the Origin gate runs before anything is read or parsed.
        if self._origin_reject():
            return
        raw = self._read_raw_body()
        if path == "/api/login":
            # Login is the one endpoint that must work without auth.
            status, data = self._read_json_body(raw)
            if status != 200:
                self._send_json(415, _JSON_ONLY_ERROR)
                return
            token = str(data.get("token", "")).strip()
            if self._token_ok(token):
                self._send(200, b'{"ok":true}', "application/json; charset=utf-8",
                           extra_headers=((
                               "Set-Cookie",
                               f"arena_token={token}; Path=/; HttpOnly; SameSite=Lax",
                           ),))
            else:
                self._send_json(401, {"ok": False, "error": "token 错误"})
            return
        if path == "/api/logout":
            # Clearing the cookie needs no auth (it only forgets a credential
            # the caller already holds) and no state: the token itself stays
            # valid, so this cannot lock an operator out.
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8",
                       extra_headers=((
                           "Set-Cookie",
                           "arena_token=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
                       ),))
            return
        if not self._authed():
            if path.startswith("/api/v1/"):
                # A browser POST to the game API that never reaches the
                # proxy shows up as the generic frontend error; log it so the
                # missing-dashboard-token case is visible server-side.
                print(f"[arena-proxy] POST {path} blocked: dashboard auth failed",
                      file=sys.stderr, flush=True)
            self._send_json(401, {"ok": False, "error": "未授权：缺少或错误的 DASHBOARD_TOKEN"})
            return
        if path == "/api/v1/session/import":
            # OAuth-only accounts (LINUX DO / GitHub) cannot log in here
            # directly: the upstream OAuth redirect_uri is fixed to the
            # official site. The operator copies their session cookie there
            # and imports it; we validate it upstream, then rebind it to
            # this origin so later requests flow through the MANUAL slot.
            status, _ = self._read_json_body(raw)
            if status != 200:
                self._send_json(415, _JSON_ONLY_ERROR)
                return
            self._import_session(raw)
            return
        if path == "/api/session/draft":
            # One-time migration of a legacy browser-stored draft, or an
            # explicit save. Format-check only: the upstream validation
            # happens on the actual import.
            status, data = self._read_json_body(raw)
            if status != 200:
                self._send_json(415, _JSON_ONLY_ERROR)
                return
            cookies = str(data.get("cookies", "")).strip()
            csrf = str(data.get("csrf", "")).strip()
            if not cookies or not csrf or len(cookies) > 8192 or len(csrf) > 512:
                self._send_json(400, {"ok": False, "error": "SESSION_IMPORT_INVALID"})
                return
            _save_session_draft({"cookies": cookies, "csrf": csrf})
            self._send_json(200, {"ok": True})
            return
        if path == "/api/session/draft/clear":
            _save_session_draft(None)
            self._send_json(200, {"ok": True})
            return
        if path in _ARENA_POST_PATHS:
            # Game command plan: forward upstream with the server-side key.
            self._proxy_arena_post(path, raw)
            return
        # The proxy paths above forward the raw body untouched; only the
        # config/management endpoints below need it parsed, so the high-volume
        # game commands skip a full JSON parse of every request body. A wrong
        # Content-Type is answered here, before any handler runs, so a 415 can
        # never leave a half-applied change behind.
        status, data = self._read_json_body(raw)
        if status != 200:
            self._send_json(415, _JSON_ONLY_ERROR)
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


class _QuietHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer tuned for a bursty polling SPA + public port.

    The dashboard port is public, so internet scanners connect and reset
    constantly; each one used to print a full ConnectionResetError traceback
    via socketserver.handle_error. Swallow the benign disconnect/timeout errors
    and keep real failures visible.

    request_queue_size 64 (instead of 5) absorbs the SPA's per-tick fetch fan-out
    plus healthcheck curls without TCP refusals, and daemon_threads keeps a
    shutdown from waiting on hijacked WebSocket pipes.
    """

    daemon_threads = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    srv = _QuietHTTPServer((HOST, PORT), Handler)
    print(f"[dashboard] http://localhost:{PORT}", flush=True)
    if DASHBOARD_TOKEN:
        print("[dashboard] DASHBOARD_TOKEN set: loopback also needs the token "
              "(cookie / Bearer / ?token=) except GET /api/health and GET /api/state",
              flush=True)
    else:
        print("[dashboard] DASHBOARD_TOKEN unset: auth disabled (local dev mode)", flush=True)
    print("[dashboard] soft refresh /api/state · Ctrl+C 停止", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] 已停止")
        srv.server_close()


if __name__ == "__main__":
    main()
