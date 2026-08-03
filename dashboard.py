"""Arena Hero dashboard - pan/zoom SVG map + Chinese dark UI.
Run: python dashboard.py  -> http://localhost:4399
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import game_stats
from tactic_config import (
    CONFIG_PATH,
    ConfigValidationError,
    config_schema,
    default_config,
    load_config,
    save_config,
)

def _data_path(name: str) -> str:
    raw = os.environ.get("ARENA_DATA_DIR", "").strip()
    return str(Path(raw).resolve() / name) if raw else name


LOG_FILE = _data_path("tactic_log.jsonl")
MAP_FILE = _data_path("map_memory.json")
HOST = "0.0.0.0"
PORT = 4399


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


def read_history(ticks: int = 40):
    if ticks <= 0 or not os.path.exists(LOG_FILE):
        return []
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
    return out



def load_map_memory():
    empty = {
        "obstacles": [],
        "resources": [],
        "manual_resources": [],
        "enemy_sightings": [],
        "obstacle_count": 0,
        "resource_count": 0,
        "manual_count": 0,
    }
    if not os.path.exists(MAP_FILE):
        return empty
    try:
        with open(MAP_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        resources = [tuple(p) for p in d.get("resources", []) if len(p) == 2]
        manual = [tuple(p) for p in d.get("manual_resources", []) if len(p) == 2]
        all_res = sorted(set(resources) | set(manual))
        return {
            "obstacles": [tuple(p) for p in d.get("obstacles", []) if len(p) == 2],
            "resources": all_res,
            "manual_resources": manual,
            "enemy_sightings": [tuple(p) for p in d.get("enemy_sightings", []) if len(p) == 2],
            "obstacle_count": d.get("obstacle_count", len(d.get("obstacles", []))),
            "resource_count": len(all_res),
            "manual_count": len(manual),
            "updated_tick": d.get("updated_tick"),
        }
    except Exception:
        return empty


def save_manual_resource(x: int, y: int) -> dict:
    """Add a manually entered resource into map_memory.json."""
    data: dict = {
        "obstacles": [],
        "resources": [],
        "manual_resources": [],
        "obstacle_count": 0,
        "resource_count": 0,
        "manual_count": 0,
    }
    if os.path.exists(MAP_FILE):
        try:
            with open(MAP_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    data.update(loaded)
        except Exception:
            pass

    pos = (int(x), int(y))
    resources = {tuple(p) for p in data.get("resources", []) if len(p) == 2}
    manual = {tuple(p) for p in data.get("manual_resources", []) if len(p) == 2}
    resources.add(pos)
    manual.add(pos)

    payload = {
        "updated_tick": data.get("updated_tick"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "obstacles": data.get("obstacles", []),
        "resources": [list(p) for p in sorted(resources)],
        "manual_resources": [list(p) for p in sorted(manual)],
        "obstacle_count": data.get("obstacle_count", len(data.get("obstacles", []))),
        "resource_count": len(resources),
        "manual_count": len(manual),
        "source": "dashboard-manual",
    }
    tmp = MAP_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, MAP_FILE)
    return {
        "ok": True,
        "pos": [pos[0], pos[1]],
        "resource_count": len(resources),
        "manual_count": len(manual),
    }


def remove_manual_resource(x: int, y: int) -> dict:
    pos = (int(x), int(y))
    if not os.path.exists(MAP_FILE):
        return {"ok": False, "error": "no map file"}
    with open(MAP_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    resources = {tuple(p) for p in data.get("resources", []) if len(p) == 2}
    manual = {tuple(p) for p in data.get("manual_resources", []) if len(p) == 2}
    resources.discard(pos)
    manual.discard(pos)
    payload = {
        "updated_tick": data.get("updated_tick"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "obstacles": data.get("obstacles", []),
        "resources": [list(p) for p in sorted(resources)],
        "manual_resources": [list(p) for p in sorted(manual)],
        "obstacle_count": data.get("obstacle_count", len(data.get("obstacles", []))),
        "resource_count": len(resources),
        "manual_count": len(manual),
        "source": "dashboard-manual",
    }
    tmp = MAP_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, MAP_FILE)
    return {
        "ok": True,
        "pos": [pos[0], pos[1]],
        "resource_count": len(resources),
        "manual_count": len(manual),
    }


TEAM_BOARD_KEYS = ("unassigned", "home", "attack", "guerrilla")
TEAM_BOARD_META = {
    "unassigned": {"label": "待命池", "hint": "未编队", "tone": "idle"},
    "home": {"label": "守家队", "hint": "核心半径巡逻", "tone": "home"},
    "attack": {"label": "进攻队", "hint": "集体推进接战", "tone": "attack"},
    "guerrilla": {"label": "游击队", "hint": "八向分散袭扰", "tone": "guerrilla"},
}
TEAM_ROSTER_FIELDS = ("home_team", "attack_team", "guerrilla_team")
TEAM_SETTING_FIELDS = (
    "home_patrol_radius",
    "attack_target_x",
    "attack_target_y",
    "attack_mode",
    "ranger_attack_range",
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
    if upper in _parse_roster_names(config.get("guerrilla_team", "")):
        return "guerrilla"
    return "unassigned"


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


def render_config_panel(workers: int = 0, vanguards: int = 0, rangers: int = 0) -> str:
    config = load_config(CONFIG_PATH)
    schema = config_schema()
    fields_by_group: dict[str, list[dict]] = defaultdict(list)
    for field in schema["fields"]:
        # Combat rosters + team settings live on the dedicated teams card;
        # production targets live in the dedicated section below.
        if field["group"] in {"combat", "production"}:
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
            state_cls, state_text = "culling", f"超编 {-diff} · 将自裁"
        else:
            state_cls, state_text = "ok", "已达标"
        suffix = key.replace("target_", "").capitalize()  # Workers / Vanguards / Rangers
        return (
            f'<div class="production-target">'
            f'<label for="cfg-{key}">{label}</label>'
            f'<input id="cfg-{key}" name="{key}" type="number" data-kind="integer" '
            f'min="0" max="19" step="1" value="{target}" required>'
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
        '<section class="production-section" aria-labelledby="productionTargetsTitle">'
        '<div class="production-title"><div><b id="productionTargetsTitle">生产需求目标</b>'
        '<span class="count">低于目标自动补兵 · 超出自动自裁 · 阵亡自动补充 · 改后点保存生效</span></div></div>'
        f'<div class="production-targets" id="productionTargets">{target_rows}</div></section>'
        '<form id="tacticConfigForm">'
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

    attack_mode = str(config.get("attack_mode", "coords"))
    mode_checked = {
        "coords": " checked" if attack_mode == "coords" else "",
        "auto": " checked" if attack_mode == "auto" else "",
        "beacon": " checked" if attack_mode == "beacon" else "",
    }
    # Attack coordinates only matter in coords mode; beacon / auto ignore them.
    coords_locked = "" if attack_mode == "coords" else " disabled"

    settings = (
        '<div class="team-settings">'
        '<label>守家半径'
        f'<input id="teamHomeRadius" name="home_patrol_radius" type="number" min="1" max="30" '
        f'step="1" value="{config["home_patrol_radius"]}"></label>'
        '<label>进攻 X'
        f'<input id="teamAttackX" name="attack_target_x" type="number" min="-500" max="500" '
        f'step="1" value="{config["attack_target_x"]}"{coords_locked}></label>'
        '<label>进攻 Y'
        f'<input id="teamAttackY" name="attack_target_y" type="number" min="-500" max="500" '
        f'step="1" value="{config["attack_target_y"]}"{coords_locked}></label>'
        '<label>游侠射程'
        f'<input id="teamRangerRange" name="ranger_attack_range" type="number" min="1" max="3" '
        f'step="1" value="{config["ranger_attack_range"]}"></label>'
        '<div class="team-mode">'
        '<span class="team-mode-title">进攻方式</span>'
        '<div class="team-mode-opts">'
        '<label class="team-radio"><input type="radio" name="attack_mode" value="coords"'
        f'{mode_checked["coords"]}>进攻坐标</label>'
        '<label class="team-radio"><input type="radio" name="attack_mode" value="auto"'
        f'{mode_checked["auto"]}>自动进攻</label>'
        '<label class="team-radio"><input type="radio" name="attack_mode" value="beacon"'
        f'{mode_checked["beacon"]}>进攻冠军信标</label>'
        '</div></div>'
        '</div>'
    )

    return (
        '<section class="panel teams-panel" id="teamsPanel">'
        '<div class="panel-title"><span>战斗分队</span>'
        '<span class="count" id="teamsState">拖拽编队</span></div>'
        '<div class="teams-hero">'
        '<div><b>把先锋 / 游侠拖进队伍</b>'
        '<p>新单位默认进守家队；拖到待命池可暂时不参战编成。</p></div>'
        '<div class="teams-actions">'
        '<button type="button" class="secondary" id="teamsResetBtn">重载</button>'
        '<button type="button" id="teamsSaveBtn">保存分队</button>'
        '</div></div>'
        f'<div class="team-board">{"".join(columns)}</div>'
        f'{settings}'
        '<div class="teams-message" id="teamsMessage" aria-live="polite">'
        '拖拽单位后自动保存，下个 Tick 生效</div>'
        '</section>'
    )


def short_id(uid: str) -> str:
    return (uid or "?")[:8]


def fmt_pos(pos) -> str:
    if not pos:
        return "-"
    return f"({int(pos[0])}, {int(pos[1])})"


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
    for p in mm.get("enemy_sightings", []): pts.append((int(p[0]), int(p[1])))
    bp = rec.get("beacon_pos")
    if bp and len(bp) == 2: pts.append((int(bp[0]), int(bp[1])))
    return pts


def render_svg(rec, mm, cell: int = 16, pad: int = 24, margin: int = 4):
    pts = _collect_points(rec, mm)
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

    # obstacles
    for ox, oy in obs:
        if not (xmin <= ox <= xmax and ymin <= oy <= ymax): continue
        x, y = to_xy(ox, oy)
        a(f'<rect x="{x+1.2}" y="{y+1.2}" width="{cell-2.4}" height="{cell-2.4}" rx="3.5" '
          f'fill="#3a455f" stroke="#7f8eab" stroke-width="1"/>')

    # remembered resources
    for rx, ry in mem_r - vis_r:
        if not (xmin <= rx <= xmax and ymin <= ry <= ymax): continue
        x, y = to_xy(rx, ry)
        cxr, cyr = x + cell / 2, y + cell / 2
        a(f'<circle cx="{cxr}" cy="{cyr}" r="4.5" fill="#c9a227" opacity="0.55"/>')
        a(f'<circle cx="{cxr}" cy="{cyr}" r="2.2" fill="#ffe08a" opacity="0.85"/>')
    for rx, ry in vis_r:
        if not (xmin <= rx <= xmax and ymin <= ry <= ymax): continue
        x, y = to_xy(rx, ry)
        cxr, cyr = x + cell / 2, y + cell / 2
        a(f'<circle cx="{cxr}" cy="{cyr}" r="6.5" fill="#ffc857" filter="url(#glow)"/>')
        a(f'<circle cx="{cxr}" cy="{cyr}" r="2.8" fill="#fff3c4"/>')

    # enemy sightings
    ex_sightings = {(int(a), int(b)) for a, b in mm.get("enemy_sightings", [])}
    for ex, ey in ex_sightings:
        if not (xmin <= ex <= xmax and ymin <= ey <= ymax): continue
        x, y = to_xy(ex, ey)
        cxr, cyr = x + cell / 2, y + cell / 2
        a(f'<circle cx="{cxr}" cy="{cyr}" r="5" fill="#ff6464" opacity="0.40"/>')
        a(f'<circle cx="{cxr}" cy="{cyr}" r="2" fill="#ff9b9b" opacity="0.75"/>')

    # Worker routes and targets are generated by the same planner that moves them.
    route_colors = (
        "#63d8ff", "#57d6a3", "#ffc857", "#ff7aa9",
        "#b38cff", "#ff8a65", "#8fd14f", "#78a9ff",
    )

    def _draw_route(unit_data, name, color, css_class=""):
        path = [p for p in (unit_data.get("path") or []) if len(p) == 2]
        target = unit_data.get("target") or []
        if len(path) > 1:
            route_points = []
            for px, py in path:
                x, y = to_xy(int(px), int(py))
                route_points.append(f"{x + cell / 2:.1f},{y + cell / 2:.1f}")
            points_attr = " ".join(route_points)
            dash = "" if unit_data.get("path_complete") else ' stroke-dasharray="5 4"'
            a(f'<polyline class="{css_class}" data-unit="{name}" '
              f'points="{points_attr}" fill="none" stroke="{color}" '
              f'stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round" '
              f'opacity="0.82"{dash}/>')
        if len(target) == 2:
            tx, ty = int(target[0]), int(target[1])
            if xmin <= tx <= xmax and ymin <= ty <= ymax:
                x, y = to_xy(tx, ty)
                cx, cy = x + cell / 2, y + cell / 2
                a(f'<circle class="{css_class}-target" data-unit="{name}" cx="{cx}" cy="{cy}" '
                  f'r="8" fill="none" stroke="{color}" stroke-width="1.8" opacity="0.9"/>')
                a(f'<circle cx="{cx}" cy="{cy}" r="2" fill="{color}"/>')

    for index, worker in enumerate(rec.get("workers", []) or []):
        _draw_route(worker, worker.get("name") or f"W{index + 1}",
                     route_colors[index % len(route_colors)], "worker-route")

    # Vanguard routes (orange)
    for index, v in enumerate(rec.get("vanguards", []) or []):
        _draw_route(v, v.get("name") or f"V{index + 1}", "#ff8c42", "vanguard-route")

    # Ranger routes (teal)
    for index, r in enumerate(rec.get("rangers", []) or []):
        _draw_route(r, r.get("name") or f"R{index + 1}", "#6ea8ff", "ranger-route")

    def unit(pos, color, label, glow=False, ring=None):
        if not pos or len(pos) != 2: return
        px, py = int(pos[0]), int(pos[1])
        if not (xmin <= px <= xmax and ymin <= py <= ymax): return
        x, y = to_xy(px, py)
        ux, uy = x + cell / 2, y + cell / 2
        if glow: a(f'<circle cx="{ux}" cy="{uy}" r="11" fill="{color}" opacity="0.18"/>')
        if ring: a(f'<circle cx="{ux}" cy="{uy}" r="8.5" fill="none" stroke="{ring}" stroke-width="2"/>')
        unit_radius = 7.5 if len(str(label)) > 2 else 7
        font_size = 6 if len(str(label)) > 2 else 7
        a(f'<circle cx="{ux}" cy="{uy}" r="{unit_radius}" fill="{color}" filter="url(#glow)" '
          f'stroke="rgba(255,255,255,0.65)" stroke-width="1.2"/>')
        a(f'<text x="{ux}" y="{uy+2.5}" text-anchor="middle" font-size="{font_size}" '
          f'font-family="Segoe UI, Microsoft YaHei, sans-serif" font-weight="700" fill="#0b1020">{label}</text>')

    for index, w in enumerate(rec.get("workers", [])):
        c = bool(w.get("cargo"))
        name = w.get("name") or f"W{index + 1}"
        unit(w.get("pos"), "#57d6a3" if c else "#8aa4ff", name, glow=c, ring="#9ef0c8" if c else None)
    for index, v in enumerate(rec.get("vanguards", [])):
        unit(v.get("pos"), "#ff8c42", v.get("name") or f"V{index + 1}", glow=True)
    for index, r in enumerate(rec.get("rangers", [])):
        unit(r.get("pos"), "#b38cff", r.get("name") or f"R{index + 1}", glow=True)
    for index, enemy in enumerate(rec.get("enemies", [])):
        unit(enemy.get("pos"), "#ff6464", enemy.get("name") or f"E{index + 1}", glow=True, ring="#ff9b9b")

    if core_cx is not None:
        a(f'<circle cx="{core_cx}" cy="{core_cy}" r="15" fill="url(#coreGlow)"/>')
        a(f'<rect x="{core_cx-6.5}" y="{core_cy-6.5}" width="13" height="13" rx="3" '
          f'transform="rotate(45 {core_cx} {core_cy})" fill="#6ea8ff" stroke="#d7e8ff" '
          f'stroke-width="1.4" filter="url(#glow)"/>')
        a(f'<text x="{core_cx}" y="{core_cy+3}" text-anchor="middle" font-size="8" '
          f'font-family="Segoe UI, Microsoft YaHei, sans-serif" font-weight="700" fill="#081018">'
          f'{rec.get("core_name") or "C1"}</text>')

    # Beacon
    bp = rec.get("beacon_pos")
    if bp and len(bp) == 2:
        bx, by = to_xy(int(bp[0]), int(bp[1]))
        bcx, bcy = bx + cell / 2, by + cell / 2
        # Glow
        a(f'<circle cx="{bcx}" cy="{bcy}" r="14" fill="#ffc857" opacity="0.18"/>')
        # Diamond shape
        r = 7.5
        a(f'<polygon points="{bcx},{bcy-r} {bcx+r},{bcy} {bcx},{bcy+r} {bcx-r},{bcy}" '
          f'fill="#ffc857" stroke="#ffe08a" stroke-width="1.5" filter="url(#glow)"/>')
        # Inner star
        a(f'<text x="{bcx}" y="{bcy+3.5}" text-anchor="middle" font-size="9" '
          f'font-family="Segoe UI, Microsoft YaHei, sans-serif" font-weight="700" fill="#5c4300">★</text>')

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
.unit-grid{display:grid;grid-template-columns:1fr;gap:8px}
.unit{border-radius:16px;padding:12px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);transition:transform .15s,border-color .15s}
.unit:hover{transform:translateY(-1px);border-color:rgba(255,255,255,.14)}
.unit.cargo{background:rgba(87,214,163,.08)}
.unit.harvest{background:rgba(255,200,87,.08)}
.unit.deposit{background:rgba(110,168,255,.10)}
.unit.wait{background:rgba(255,107,107,.10)}
.unit.explore{background:rgba(179,140,255,.08)}
.unit.combat{background:rgba(255,107,157,.10)}
.unit-top{display:flex;justify-content:space-between;gap:8px;align-items:center;margin-bottom:8px}
.unit-id{font-family:Consolas,monospace;font-size:13px;display:flex;gap:6px;align-items:center}
.badge{font-size:11px;padding:3px 8px;border-radius:999px;border:1px solid transparent}
.badge.cargo,.badge.deposit{background:rgba(87,214,163,.15);color:#8ef0c4}
.badge.harvest{background:rgba(255,200,87,.15);color:#ffd98a}
.badge.wait{background:rgba(255,107,107,.15);color:#ff9b9b}
.badge.explore{background:rgba(179,140,255,.15);color:#d0b8ff}
.badge.combat{background:rgba(255,107,157,.15);color:#ff9ec0}
.badge.move,.badge.other{background:rgba(110,168,255,.12);color:#a9c8ff}
.unit-meta{display:flex;justify-content:space-between;gap:8px;color:var(--muted);font-size:12px;margin-bottom:8px}
.unit-coords{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px}
.unit-coords>div{display:grid;gap:2px;min-width:0;padding:6px 7px;background:rgba(0,0,0,.16);border:1px solid rgba(255,255,255,.05);border-radius:6px}
.unit-coords span{color:var(--muted);font-size:10px}
.unit-coords b{color:#e5edff;font:11px Consolas,monospace;white-space:nowrap}
.pill{padding:2px 8px;border-radius:999px;background:rgba(255,255,255,.05)}
.unit-action{font-size:12px;color:#d7e1f7;line-height:1.4;word-break:break-word}
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
.events{width:100%;border-collapse:collapse;font-size:12px}
.events th,.events td{text-align:left;padding:8px 6px;border-bottom:1px solid rgba(255,255,255,.06)}
.events th{color:var(--muted);font-weight:600}
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
.map-legend{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.map-legend span{font-size:11px;color:var(--muted);padding:4px 8px;border-radius:999px;
 background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.06);display:inline-flex;align-items:center;gap:6px}
.map-legend .dot{width:10px;height:10px;border-radius:50%;box-shadow:0 0 8px currentColor}
.map-legend .dot.core{background:#6ea8ff;color:#6ea8ff;border-radius:2px}
.map-legend .dot.cargo{background:#57d6a3;color:#57d6a3}
.map-legend .dot.worker{background:#8aa4ff;color:#8aa4ff}
.map-legend .dot.vg{background:#ff8c42;color:#ff8c42}
.map-legend .dot.rg{background:#b38cff;color:#b38cff}
.map-legend .dot.enemy{background:#ff6464;color:#ff6464}
.map-legend .dot.wall{background:#3a455f;color:#7f8eab;border-radius:2px;box-shadow:none;border:1px solid #7f8eab;width:9px;height:9px}
.map-legend .dot.ore{background:#ffc857;color:#ffc857}
.map-legend .dot.ore-mem{background:#c9a227;color:#c9a227;opacity:.8}
.map-legend .route-line{width:18px;height:0;border-top:2px solid #63d8ff;box-shadow:none;border-radius:0}
.map-legend .target-ring{width:10px;height:10px;border:2px solid #63d8ff;background:transparent;box-shadow:none}

.main-grid{display:grid;grid-template-columns:280px minmax(0,1fr) 320px;gap:14px;align-items:start}
.side-col{display:grid;gap:12px;min-width:0}
.side-col .panel{padding:14px}
.side-col .panel-title{font-size:14px;margin-bottom:10px}
.enemy-clear-btn{appearance:none;border:1px solid rgba(255,100,100,.25);border-radius:6px;background:rgba(255,100,100,.08);color:#ffb6b6;font-size:10px;padding:2px 8px;cursor:pointer;transition:.12s}
.enemy-clear-btn:hover{background:rgba(255,100,100,.22);border-color:rgba(255,100,100,.5);color:#fff}
.center-col{display:grid;gap:12px;min-width:0}
.map-panel{margin:0}
.map-panel .map-toolbar{margin-bottom:8px}
.map-legend{margin-top:8px}
.compact-list{display:grid;gap:8px;max-height:42vh;overflow:auto;padding-right:2px}
.compact-list::-webkit-scrollbar{width:6px}
.compact-list::-webkit-scrollbar-thumb{background:rgba(255,255,255,.12);border-radius:99px}
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
.teams-panel{margin-top:0;overflow:hidden}
.teams-hero{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:14px;padding:14px 16px;border-radius:16px;border:1px solid rgba(255,255,255,.08);background:
  radial-gradient(circle at 12% 20%, rgba(87,214,163,.18), transparent 42%),
  radial-gradient(circle at 88% 0%, rgba(255,107,157,.16), transparent 36%),
  linear-gradient(135deg, rgba(17,28,48,.95), rgba(12,18,32,.92));}
.teams-hero b{display:block;font-size:15px;color:#eef5ff;margin-bottom:4px}
.teams-hero p{margin:0;color:var(--muted);font-size:12px;line-height:1.5}
.teams-actions{display:flex;gap:8px;flex-wrap:wrap}
.teams-actions button,.team-chip{appearance:none;border:1px solid rgba(110,168,255,.35);border-radius:999px;padding:8px 13px;background:#285b8f;color:#fff;font-size:12px;font-weight:700;cursor:pointer}
.teams-actions button.secondary{background:transparent;border-color:rgba(255,255,255,.16);color:#c7d1e5}
.teams-actions button:disabled{opacity:.55;cursor:wait}
.team-board{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.team-column{min-width:0;border-radius:18px;border:1px solid rgba(255,255,255,.08);background:rgba(255,255,255,.025);overflow:hidden;display:flex;flex-direction:column;min-height:220px}
.team-column.tone-idle{background:linear-gradient(180deg,rgba(148,163,184,.08),rgba(255,255,255,.02))}
.team-column.tone-home{background:linear-gradient(180deg,rgba(87,214,163,.12),rgba(255,255,255,.02));border-color:rgba(87,214,163,.22)}
.team-column.tone-attack{background:linear-gradient(180deg,rgba(255,107,157,.12),rgba(255,255,255,.02));border-color:rgba(255,107,157,.22)}
.team-column.tone-guerrilla{background:linear-gradient(180deg,rgba(179,140,255,.12),rgba(255,255,255,.02));border-color:rgba(179,140,255,.22)}
.team-column.drag-over{box-shadow:0 0 0 2px rgba(110,168,255,.35) inset;transform:translateY(-1px)}
.team-column-head{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;padding:12px 12px 8px}
.team-column-head b{display:block;font-size:13px;color:#eef3ff}
.team-column-head span{display:block;margin-top:3px;color:var(--muted);font-size:11px}
.team-count{min-width:24px;height:24px;border-radius:999px;display:grid;place-items:center;background:rgba(0,0,0,.22);color:#d7e8ff;font:700 11px Consolas,monospace;font-style:normal}
.team-drop{flex:1;display:flex;flex-direction:column;gap:8px;padding:0 10px 12px;min-height:150px}
.team-empty{margin:auto 0;padding:18px 10px;border:1px dashed rgba(255,255,255,.12);border-radius:14px;color:#7f8eab;font-size:12px;text-align:center}
.team-chip{display:flex;align-items:center;gap:8px;width:100%;border-radius:14px;padding:9px 10px;background:rgba(8,14,26,.72);border:1px solid rgba(255,255,255,.10);color:#eef3ff;cursor:grab;text-align:left;box-shadow:0 8px 18px rgba(0,0,0,.18)}
.team-chip:active{cursor:grabbing}
.team-chip.dragging{opacity:.45}
.team-chip .glyph{width:28px;height:28px;border-radius:10px;display:grid;place-items:center;font:800 12px Consolas,monospace;color:#081018}
.team-chip.kind-VANGUARD .glyph{background:#ff8c42}
.team-chip.kind-RANGER .glyph{background:#b38cff}
.team-chip.kind-COMBAT .glyph{background:#6ea8ff}
.team-chip .meta{min-width:0;flex:1}
.team-chip .meta b{display:block;font-size:12px;line-height:1.2}
.team-chip .meta span{display:block;color:#93a0bf;font-size:10px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.team-chip.ghost{opacity:.55;border-style:dashed}
.team-chip .pulse{width:8px;height:8px;border-radius:50%;background:#57d6a3;box-shadow:0 0 0 4px rgba(87,214,163,.12)}
.team-chip.ghost .pulse{background:#7f8eab;box-shadow:none}
.team-settings{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:14px}
.team-settings label{display:grid;gap:6px;padding:10px 12px;border-radius:14px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);color:var(--muted);font-size:11px}
.team-settings input{width:100%;padding:8px 10px;border-radius:10px;border:1px solid rgba(255,255,255,.12);background:#0b1222;color:var(--text);font:13px Consolas,monospace;outline:none}
.team-settings label.team-switch{display:flex;align-items:center;gap:8px;justify-content:space-between}
.team-settings label.team-switch input{width:16px;height:16px;accent-color:#57d6a3;cursor:pointer}
.team-settings input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(110,168,255,.14)}
.team-settings input:disabled{opacity:.4;cursor:not-allowed}
.team-mode{grid-column:1/-1;display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:10px 12px;border-radius:14px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06)}
.team-mode-title{color:var(--muted);font-size:11px}
.team-mode-opts{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.team-radio{display:flex;align-items:center;gap:6px;color:#d7e8ff;font-size:12px;cursor:pointer}
.team-radio input{width:14px;height:14px;accent-color:#57d6a3;margin:0;cursor:pointer}
.teams-message{min-height:18px;margin-top:10px;color:var(--muted);font-size:12px}
.teams-message.ok{color:#8ef0c4}.teams-message.err{color:#ff9b9b}
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
.prod-state.culling{background:rgba(255,107,107,.15);color:#ff9b9b}
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
  .team-board{grid-template-columns:repeat(2,minmax(0,1fr))}
  .team-settings{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media (max-width:680px){
  .config-groups{grid-template-columns:1fr}
  .config-message{width:100%;margin-left:0}
  .production-targets{grid-template-columns:1fr}
  .team-board{grid-template-columns:1fr}
  .team-settings{grid-template-columns:1fr}
  .teams-hero{flex-direction:column}
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
  let refreshing = false;
  let configDirty = false;
  let teamsDirty = false;
  let teamsBusy = false;
  let teamsUnits = [];
  let teamsConfig = null;
  let dragUnitName = null;

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
  function saveView(){
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

  function zoomAt(clientX, clientY, nextScale){
    const before = pixelToWorldUnder(clientX, clientY);
    view.scale = clamp(nextScale, 0.1, 6);
    apply();
    const after = pixelToWorldUnder(clientX, clientY);
    view.worldX += before[0] - after[0];
    view.worldY += before[1] - after[1];
    apply();
  }

  function bindStage(){
    stage = document.getElementById('mapStage');
    svg = document.getElementById('gameMap');
    if(!stage || !svg) return;
    svg.addEventListener('dragstart', function(e){ e.preventDefault(); });

    stage.onpointerdown = function(e){
      if(e.button !== 0) return;
      drag = true; lx = e.clientX; ly = e.clientY;
      try { stage.setPointerCapture(e.pointerId); } catch(err){}
      svg.classList.add('dragging');
    };
    stage.onpointermove = function(e){
      if(!drag) return;
      const dx = e.clientX - lx, dy = e.clientY - ly;
      lx = e.clientX; ly = e.clientY;
      const meta = readSvgMeta(svg);
      view.worldX -= dx / (view.scale * meta.cell);
      view.worldY -= dy / (view.scale * meta.cell);
      apply();
    };
    function endDrag(e){
      if(!drag) return;
      drag = false;
      if(svg) svg.classList.remove('dragging');
      try { stage.releasePointerCapture(e.pointerId); } catch(err){}
    }
    stage.onpointerup = endDrag;
    stage.onpointercancel = endDrag;

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

  async function softRefresh(){
    if(document.hidden || drag || refreshing) return;
    refreshing = true;
    try{
      const res = await fetch('/api/state?ts=' + Date.now(), {cache:'no-store'});
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
      if(data.enemyHtml){ setHtml('#enemySection', data.enemyHtml); }
      if(data.eventsHtml) setHtml('#eventsSection', data.eventsHtml);
      if(data.mapTitle) setHtml('#mapTitleCount', data.mapTitle);
      if(data.footerHtml) setHtml('#footerSection', data.footerHtml);
      if(data.workersCount !== undefined) setText('#workersCount', data.workersCount + ' 个');
      if(data.vgCount !== undefined) setText('#vgCount', String(data.vgCount));
      if(data.rgCount !== undefined) setText('#rgCount', String(data.rgCount));
      if(data.workersCount !== undefined || data.vgCount !== undefined || data.rgCount !== undefined){
        updateProductionCounts(data.workersCount, data.vgCount, data.rgCount);
      }
      if(data.resCount !== undefined) setText('#resCount', data.resCount + ' 可见');
      if(data.enemyCount !== undefined) setText('#enemyCount', data.enemyCount);
      if(data.eventsCount !== undefined) setText('#eventsCount', String(data.eventsCount));
      if(data.combatUnits && !teamsDirty && !teamsBusy){
        teamsUnits = data.combatUnits;
        renderTeamBoard();
      }

      if(data.mapSvg){
        const stageEl = document.getElementById('mapStage');
        if(stageEl){
          stageEl.innerHTML = data.mapSvg;
          svg = document.getElementById('gameMap');
          if(svg){
            svg.addEventListener('dragstart', function(e){ e.preventDefault(); });
            apply();
          }
        }
      }
    } catch(e) {
    } finally {
      refreshing = false;
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
          if(data.ok) refresh();
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
      else if(diff < 0){ text = '超编 ' + (-diff) + ' · 将自裁'; cls = 'culling'; }
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
      state.textContent = kind === 'err' ? '保存失败' : (kind === 'ok' ? '已同步' : (teamsDirty ? '待保存' : '拖拽编队'));
    }
  }

  function currentTeamSettings(){
    const modeEl = document.querySelector('input[name="attack_mode"]:checked');
    return {
      home_patrol_radius: Number((document.getElementById('teamHomeRadius') || {}).value || 5),
      attack_target_x: Number((document.getElementById('teamAttackX') || {}).value || 0),
      attack_target_y: Number((document.getElementById('teamAttackY') || {}).value || 0),
      attack_mode: (modeEl && modeEl.value) || 'coords',
      ranger_attack_range: Number((document.getElementById('teamRangerRange') || {}).value || 3)
    };
  }

  function syncTeamModeDisabled(){
    const modeEl = document.querySelector('input[name="attack_mode"]:checked');
    const mode = (modeEl && modeEl.value) || 'coords';
    ['teamAttackX','teamAttackY'].forEach(function(id){
      const el = document.getElementById(id);
      if(el) el.disabled = (mode !== 'coords');
    });
  }

  function applyTeamSettings(config){
    if(!config) return;
    const map = {
      home_patrol_radius: 'teamHomeRadius',
      attack_target_x: 'teamAttackX',
      attack_target_y: 'teamAttackY',
      ranger_attack_range: 'teamRangerRange'
    };
    Object.keys(map).forEach(function(key){
      const el = document.getElementById(map[key]);
      if(el && key in config) {
        if (el.type === 'checkbox') el.checked = !!config[key];
        else el.value = String(config[key]);
      }
    });
    const mode = config.attack_mode || 'coords';
    document.querySelectorAll('input[name="attack_mode"]').forEach(function(el){
      el.checked = (el.value === mode);
    });
    syncTeamModeDisabled();
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
      guerrilla_team: rosterFromUnits('guerrilla'),
      home_patrol_radius: settings.home_patrol_radius,
      attack_target_x: settings.attack_target_x,
      attack_target_y: settings.attack_target_y,
      attack_mode: settings.attack_mode,
      ranger_attack_range: settings.ranger_attack_range
    };
  }

  function renderTeamBoard(){
    const counts = {unassigned:0, home:0, attack:0, guerrilla:0};
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
      chip.title = '拖到其他队伍';
      const glyph = document.createElement('span');
      glyph.className = 'glyph';
      glyph.textContent = String(unit.name || '?').slice(0,2);
      const meta = document.createElement('div');
      meta.className = 'meta';
      const title = document.createElement('b');
      title.textContent = unit.name;
      const sub = document.createElement('span');
      sub.textContent = teamMetaLine(unit);
      meta.append(title, sub);
      const pulse = document.createElement('span');
      pulse.className = 'pulse';
      chip.append(glyph, meta, pulse);
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
      teamsDirty = true;
      renderTeamBoard();
      setTeamsMessage('已调整编队，保存中…', '');
      saveTeams(true);
    }
    return changed;
  }

  async function saveTeams(auto){
    if(teamsBusy) return;
    const saveBtn = document.getElementById('teamsSaveBtn');
    teamsBusy = true;
    if(saveBtn) saveBtn.disabled = true;
    setTeamsMessage(auto ? '自动保存中…' : '保存中…', '');
    try{
      const res = await fetch('/api/teams', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(buildTeamsPayload())
      });
      const data = await res.json();
      if(!res.ok || !data.ok) throw new Error(data.error || '保存失败');
      teamsConfig = data.config;
      teamsUnits = data.combat_units || teamsUnits;
      applyTeamSettings(data.config);
      teamsDirty = false;
      renderTeamBoard();
      setTeamsMessage(auto ? '已自动保存，下个 Tick 生效' : '分队已保存，下个 Tick 生效', 'ok');
    }catch(err){
      setTeamsMessage(err.message || '网络错误', 'err');
    }finally{
      teamsBusy = false;
      if(saveBtn) saveBtn.disabled = false;
    }
  }

  async function loadTeams(force){
    if(!force && (teamsDirty || teamsBusy)) return;
    try{
      const res = await fetch('/api/teams?ts=' + Date.now(), {cache:'no-store'});
      const data = await res.json();
      if(!res.ok || !data.ok) return;
      teamsConfig = data.config;
      teamsUnits = data.combat_units || [];
      applyTeamSettings(data.config);
      teamsDirty = false;
      renderTeamBoard();
      setTeamsMessage('拖拽单位即可调整分队', '');
    }catch(e){}
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
    ['teamHomeRadius','teamAttackX','teamAttackY','teamRangerRange'].forEach(function(id){
      const el = document.getElementById(id);
      if(!el) return;
      el.addEventListener('change', function(){
        teamsDirty = true;
        setTeamsMessage('参数已修改，保存中…', '');
        saveTeams(true);
      });
    });
    document.querySelectorAll('input[name="attack_mode"]').forEach(function(el){
      el.addEventListener('change', function(){
        syncTeamModeDisabled();
        teamsDirty = true;
        setTeamsMessage('参数已修改，保存中…', '');
        saveTeams(true);
      });
    });
    const saveBtn = document.getElementById('teamsSaveBtn');
    const resetBtn = document.getElementById('teamsResetBtn');
    if(saveBtn) saveBtn.onclick = function(){ saveTeams(false); };
    if(resetBtn) resetBtn.onclick = function(){ loadTeams(true); };
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
      // Keep combat rosters/settings owned by the teams board.
      const teamPayload = buildTeamsPayload();
      Object.keys(teamPayload).forEach(function(key){ config[key] = teamPayload[key]; });
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
        teamsConfig = data.config;
        applyTeamSettings(data.config);
        loadTeams(true);
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
        teamsConfig = data.config;
        applyTeamSettings(data.config);
        loadTeams(true);
      }catch(err){
        setConfigMessage(err.message || '网络错误', 'err');
      }finally{
        resetBtn.disabled = false;
      }
    };
  }

  bindOreForm();
  bindConfigForm();
  bindTeamsBoard();
  loadTeams(true);
  ensureView();
  bindStage();
  apply();
  window.addEventListener('resize', function(){ apply(); });
  if(stage){
    stage.addEventListener('wheel', function(e){
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.12 : (1/1.12);
      zoomAt(e.clientX, e.clientY, view.scale * factor);
    }, {passive:false});
  }
  setInterval(softRefresh, 2000);
  setInterval(function(){ loadConfig(false); }, 10000);
  setInterval(function(){ loadTeams(false); }, 5000);
})();
</script>
"""


def build_parts():
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
    svg = render_svg(rec, mm)
    events = (rec.get("events", []) or [])[:8]
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
        path = w.get("path") or []
        target = w.get("target")
        steps = max(0, len(path) - 1)
        route_text = f"路径 {steps} 步" if w.get("path_complete") else f"当前规划 {steps} 步"
        extra = (
            f'<span class="pill">矿 {cargo}</span>'
            if cargo else f'<span class="pill">HP {w.get("hp","?")}</span>'
        )
        pw = per_worker.get(sid)
        if pw is not None:
            extra += (
                f'<span class="pill" title="累计采矿 / 卸货">采 {pw.get("harvested", 0)}'
                f' · 卸 {pw.get("deposited", 0)}</span>'
            )
        return (
            f'<div class="unit {kind}"><div class="unit-top">'
            f'<div class="unit-id">{name}<span class="count">{sid}</span></div>'
            f'<span class="badge {kind}">{badge}</span></div>'
            f'<div class="unit-coords"><div><span>当前坐标</span><b>{fmt_pos(w.get("pos"))}</b></div>'
            f'<div><span>目标坐标</span><b>{fmt_pos(target)}</b></div></div>'
            f'<div class="unit-meta">{extra}<span>{route_text}</span></div>'
            f'<div class="unit-action">{act}</div></div>'
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
        """Per-combat-unit stats: shots / hits / hit-rate, or '已阵亡'."""
        rec = per_combat.get(sid)
        if not rec:
            return ""
        shots = int(rec.get("shots", 0) or 0)
        hits = int(rec.get("hits", 0) or 0)
        rate = (hits * 100 / shots) if shots else 0.0
        alive = rec.get("died_tick") is None
        if alive:
            return f'<span class="pill" title="攻击次数 / 命中次数">攻 {shots} · 中 {hits} ({rate:.0f}%)</span>'
        return f'<span class="pill" title="生前攻击 / 命中">已阵亡 · 生前攻 {shots} · 中 {hits}</span>'

    def ucard(u, color_cls):
        uid = u.get("id", "")
        sid = short_id(uid)
        name = u.get("name") or sid
        act = actions.get(sid) or actions.get(uid, "")
        label = team_label(act)
        stat_line = combat_stat_line(sid)
        return (
            f'<div class="unit {color_cls}"><div class="unit-top">'
            f'<div class="unit-id">{name}<span class="count">{sid}</span></div>'
            f'<span class="badge {color_cls}">{label}</span></div>'
            f'<div class="unit-meta"><span>{fmt_pos(u.get("pos"))}</span>'
            f'<span class="pill">HP {u.get("hp","?")}</span>{stat_line}</div>'
            f'<div class="unit-action">{act}</div></div>'
        )

    w_html = "".join(wcard(w) for w in workers) or '<div class="empty">暂无工人</div>'
    vg_html = "".join(ucard(v, "combat") for v in vgs) or '<div class="empty">暂无先锋</div>'
    rg_html = "".join(ucard(r, "combat") for r in rgs) or '<div class="empty">暂无游侠</div>'
    combat_units = collect_combat_units(rec, load_config(CONFIG_PATH))

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
    res_html += (
        '<div class="res-add-form" id="resAddForm">'
        '<div class="row">'
        '<label>X<input id="oreX" name="x" type="number" step="1" placeholder="-30" required></label>'
        '<label>Y<input id="oreY" name="y" type="number" step="1" placeholder="65" required></label>'
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
        ex_chips = "".join(f'<span class="chip enemy-chip">{fmt_pos(p)}</span>' for p in ex_sightings[:30])
        enemy_html = f'<div class="res-section"><h4>敌人踪迹</h4><div class="chip-row">{ex_chips}</div></div>'
    else:
        enemy_html = '<div class="muted">暂无敌人踪迹</div>'

    if events:
        rows = ""
        for e in events:
            rows += (
                f"<tr><td>{e.get('type','')}</td><td>{e.get('reason') or '—'}</td>"
                f"<td>{short_id(e.get('actor')) if e.get('actor') else '—'}</td>"
                f"<td>{fmt_pos(e.get('pos'))}</td></tr>"
            )
        events_html = (
            '<table class="events"><thead><tr><th>事件</th><th>原因</th>'
            '<th>单位</th><th>位置</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
        )
    else:
        events_html = '<div class="muted">本帧无特殊事件</div>'

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
        f'<span class="pill">人口层 {rec.get("population_tier",0)}</span>'
        f'<span class="pill">保养 {rec.get("upkeep_next_tick",0)}</span>'
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


    left_core = (
        f'<div class="kv"><span>名称</span><b>{rec.get("core_name") or "C1"}</b></div>'
        f'<div class="kv"><span>位置</span><b>{fmt_pos(rec.get("core_pos"))}</b></div>'
        f'<div class="kv"><span>动作</span><b>{rec.get("core_action") or "—"}</b></div>'
        f'<div class="kv"><span>状态</span><b>{rec.get("core_state") or "—"}</b></div>'
        f'<div class="kv"><span>HP / 盾</span><b>{rec.get("core_hp","?")} / {rec.get("core_shield","?")}</b></div>'
        f'<div class="kv"><span>人口</span><b>{rec.get("population",0)} · 层{rec.get("population_tier",0)}</b></div>'
        f'<div class="kv"><span>信标</span><b>{fmt_pos(rec.get("beacon_pos"))}</b></div>'
    )
    left_res = (
        f'<div class="kv"><span>库存</span><b>{resources} / {cap}</b></div>'
        f'<div class="mini-bar"><span style="width:{pct}%"></span></div>'
        f'<div class="kv" style="margin-top:8px"><span>可见矿</span><b>{len(rcells)}</b></div>'
        f'<div class="kv"><span>记忆矿</span><b>{mm.get("resource_count",0)}</b></div>'
        f'<div class="kv"><span>墙记忆</span><b>{mm.get("obstacle_count",0)}</b></div>'
        f'<div class="kv"><span>可见墙</span><b>{rec.get("obstacle_cells_visible",0)}</b></div>'
    )
    left_fight = (
        f'<div class="kv"><span>敌人</span><b>{enemies}</b></div>'
        f'<div class="kv"><span>工人</span><b>{len(workers)}</b></div>'
        f'<div class="kv"><span>先锋 / 游侠</span><b>{len(vgs)} / {len(rgs)}</b></div>'
        f'<div class="stat-chips" style="margin-top:8px">'
        f'<span class="pill">回矿 {stats["cargo"]+stats["deposit"]}</span>'
        f'<span class="pill">探索 {stats["explore"]}</span>'
        f'<span class="pill">等待 {stats["wait"]}</span>'
        f'<span class="pill">挖矿 {stats["harvest"]}</span></div>'
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

    vg_rate = derived["vanguard_hit_rate"]
    rg_rate = derived["ranger_hit_rate"]
    report_html = (
        '<section class="panel"><div class="panel-title"><span>战报统计</span>'
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
        '<div class="kv" style="margin-top:8px"><span>移动</span>'
        f'<b>成功 {eco.get("moves_succeeded", 0)} · 失败 {eco.get("moves_failed", 0)}</b></div>'
        '</section>'
    )

    left_html = (
        f'<section class="panel"><div class="panel-title"><span>核心</span><span class="count">状态</span></div>{left_core}</section>'
        f'<section class="panel"><div class="panel-title"><span>资源</span><span class="count">{pct}%</span></div>{left_res}</section>'
        f'<section class="panel"><div class="panel-title"><span>战场</span><span class="count">摘要</span></div>{left_fight}</section>'
        f'<section class="panel"><div class="panel-title"><span>异常</span><span class="count">{len(issues)}</span></div><div class="compact-list">{left_issues}</div></section>'
        + report_html
        + f'<section class="panel enemy-panel" id="leftEnemyPanel"><div class="panel-title"><span>敌人踪迹</span><span class="count" id="enemyCount">{len(ex_sightings)} 处</span><button type="button" id="clearEnemyBtn" class="enemy-clear-btn">清除</button></div><div id="enemySection">{enemy_html}</div></section>'
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
        "eventsHtml": events_html,
        "mapSvg": svg,
        "mapTitle": map_title,
        "footerHtml": footer_html,
        "workersCount": len(workers),
        "vgCount": len(vgs),
        "rgCount": len(rgs),
        "resCount": len(rcells),
        "enemyCount": f"{len(ex_sightings)} 处",
        "eventsCount": len(events),
        "combatUnits": combat_units,
    }


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
       <div class="map-toolbar">
        <button type="button" id="zoomOutBtn">-</button>
        <button type="button" id="zoomInBtn">+</button>
        <button type="button" id="resetViewBtn">重置视角</button>
        <button type="button" id="focusCoreBtn">定位核心</button>
        <span id="zoomLabel">100%</span>
        <span class="hint">拖动 · 滚轮 · 软刷新</span>
       </div>
       <div class="map-stage" id="mapStage">{parts['mapSvg']}</div>
       <div class="map-legend">
        <span><i class="dot core"></i>核心</span>
        <span><i class="dot cargo"></i>带矿</span>
        <span><i class="dot worker"></i>空手</span>
        <span><i class="dot vg"></i>先锋</span>
        <span><i class="dot rg"></i>游侠</span>
        <span><i class="dot enemy"></i>敌人</span>
        <span><i class="dot wall"></i>墙</span>
        <span><i class="dot ore"></i>可见矿</span>
        <span><i class="dot ore-mem"></i>记忆矿</span>
        <span><i class="dot route-line"></i>工人路径</span>
        <span><i class="dot target-ring"></i>目标</span>
       </div>
      </section>
      {render_teams_panel()}
      {render_config_panel(parts['workersCount'], parts['vgCount'], parts['rgCount'])}
      <div id="issuesSection" style="display:none">{parts['issuesHtml']}</div>
    </section>

    <aside class="side-col">
      <section class="panel">
        <div class="panel-title"><span>工人</span><span class="count" id="workersCount">{parts['workersCount']} 个</span></div>
        <div class="unit-grid compact-list" id="workersGrid">{parts['workersHtml']}</div>
      </section>
      <section class="panel">
        <div class="panel-title"><span>先锋</span><span class="count" id="vgCount">{parts['vgCount']}</span></div>
        <div class="unit-grid" id="vgGrid">{parts['vgHtml']}</div>
      </section>
      <section class="panel">
        <div class="panel-title"><span>游侠</span><span class="count" id="rgCount">{parts['rgCount']}</span></div>
        <div class="unit-grid" id="rgGrid">{parts['rgHtml']}</div>
      </section>
      <section class="panel res-panel" id="resPanel">
        <div class="res-head">
          <div class="panel-title" style="margin-bottom:0"><span>矿点</span><span class="count" id="resCount">{parts['resCount']} 可见</span></div>
          <button type="button" class="add-ore-btn" id="resAddToggle" title="录入矿点">+</button>
        </div>
        <div id="resSection">{parts['resHtml']}</div>
      </section>
      <section class="panel">
        <div class="panel-title"><span>事件</span><span class="count" id="eventsCount">{parts['eventsCount']}</span></div>
        <div id="eventsSection">{parts['eventsHtml']}</div>
      </section>
    </aside>
  </div>

  <div class="footer" id="footerSection">{parts['footerHtml']}</div>
</div>
{JS}
</body></html>"""



class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str):
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # Client disconnected mid-response; nothing to do.
            pass

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        # Silence per-request noise; errors still print via handle_one_request.
        return

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, generate_html().encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            parts = build_parts()
            if not parts:
                body = json.dumps({"tick": None, "error": "no data"}, ensure_ascii=False).encode("utf-8")
            else:
                body = json.dumps(parts, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
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
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._read_json()
        if path in {"/api/config", "/api/config/reset"}:
            try:
                values = default_config() if path.endswith("/reset") else data
                if path == "/api/config":
                    # Strategy form no longer owns combat rosters/settings.
                    current = load_config(CONFIG_PATH)
                    merged = dict(current)
                    for key, value in values.items():
                        merged[key] = value
                    values = merged
                config = save_config(values, CONFIG_PATH)
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
            self._send_json(200, {"ok": True, "config": config})
            return

        if path == "/api/teams":
            try:
                current = load_config(CONFIG_PATH)
                merged = dict(current)
                for key in list(TEAM_ROSTER_FIELDS) + list(TEAM_SETTING_FIELDS):
                    if key in data:
                        merged[key] = data[key]
                # Normalize roster text for stable dashboard/tactic display.
                for key in TEAM_ROSTER_FIELDS:
                    merged[key] = _format_roster_names(_parse_roster_names(merged.get(key, "")))
                config = save_config(merged, CONFIG_PATH)
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
                with open(MAP_FILE, "r+", encoding="utf-8") as f:
                    d = json.load(f)
                    d["enemy_sightings"] = []
                    d["enemy_sighting_count"] = 0
                    f.seek(0)
                    f.truncate()
                    json.dump(d, f, ensure_ascii=False, indent=2)
                self._send_json(200, {"ok": True, "cleared": True})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
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
