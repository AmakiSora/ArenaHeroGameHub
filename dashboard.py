"""Real-time tactic dashboard - modern Chinese UI.
Run: python dashboard.py
Open: http://localhost:4399
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

LOG_FILE = "tactic_log.jsonl"
MAP_FILE = "map_memory.json"
HOST = "0.0.0.0"
PORT = 4399


def read_latest():
    if not os.path.exists(LOG_FILE):
        return None, time.time()
    mtime = os.path.getmtime(LOG_FILE)
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        lines = [line.strip() for line in f if line.strip()]
    for line in reversed(lines):
        try:
            rec = json.loads(line)
            if rec.get("tick") and "plan_unit_actions" in rec:
                return rec, mtime
        except Exception:
            continue
    return None, mtime


def read_history(ticks: int = 40):
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        lines = [line.strip() for line in f if line.strip()]
    history = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
            if rec.get("tick") and "plan_unit_actions" in rec:
                history.append(rec)
                if len(history) >= ticks:
                    break
        except Exception:
            continue
    return history


def format_pos(pos) -> str:
    if not pos:
        return "—"
    return f"({pos[0]}, {pos[1]})"


def load_map_memory():
    if not os.path.exists(MAP_FILE):
        return {"obstacles": [], "resources": [], "obstacle_count": 0, "resource_count": 0}
    try:
        with open(MAP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "obstacles": [tuple(p) for p in data.get("obstacles", []) if len(p) == 2],
            "resources": [tuple(p) for p in data.get("resources", []) if len(p) == 2],
            "obstacle_count": data.get("obstacle_count", len(data.get("obstacles", []))),
            "resource_count": data.get("resource_count", len(data.get("resources", []))),
            "updated_tick": data.get("updated_tick"),
        }
    except Exception:
        return {"obstacles": [], "resources": [], "obstacle_count": 0, "resource_count": 0}


def render_svg_map(rec, map_mem, radius: int = 16, cell: int = 18) -> str:
    """Render a polished local SVG map around the core."""
    core = rec.get("core_pos")
    if not core:
        return '<div class="muted">暂无核心位置</div>'

    cx, cy = int(core[0]), int(core[1])
    xmin, xmax = cx - radius, cx + radius
    ymin, ymax = cy - radius, cy + radius
    cols = xmax - xmin + 1
    rows = ymax - ymin + 1
    pad = 28
    width = cols * cell + pad * 2
    height = rows * cell + pad * 2

    def in_view(x, y):
        return xmin <= x <= xmax and ymin <= y <= ymax

    def to_xy(x, y):
        # SVG y grows downward; game y grows upward
        return pad + (x - xmin) * cell, pad + (ymax - y) * cell

    obstacles = {(int(x), int(y)) for x, y in map_mem.get("obstacles", []) if in_view(int(x), int(y))}
    mem_res = {(int(x), int(y)) for x, y in map_mem.get("resources", []) if in_view(int(x), int(y))}
    vis_res = set()
    for p in rec.get("resource_cells", []) or []:
        if len(p) == 2 and in_view(int(p[0]), int(p[1])):
            vis_res.add((int(p[0]), int(p[1])))

    parts = [
        f'<svg class="game-map" viewBox="0 0 {width} {height}" role="img" aria-label="known-map">',
        '<defs>',
        '<linearGradient id="floorGrad" x1="0" y1="0" x2="1" y2="1">',
        '<stop offset="0%" stop-color="#121a2f"/>',
        '<stop offset="100%" stop-color="#0b1222"/>',
        '</linearGradient>',
        '<radialGradient id="coreGlow" cx="50%" cy="50%" r="50%">',
        '<stop offset="0%" stop-color="#6ea8ff" stop-opacity="0.55"/>',
        '<stop offset="100%" stop-color="#6ea8ff" stop-opacity="0"/>',
        '</radialGradient>',
        '<filter id="softGlow" x="-50%" y="-50%" width="200%" height="200%">',
        '<feGaussianBlur stdDeviation="2.2" result="coloredBlur"/>',
        '<feMerge><feMergeNode in="coloredBlur"/><feMergeNode in="SourceGraphic"/></feMerge>',
        '</filter>',
        '</defs>',
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="18" fill="url(#floorGrad)"/>',
        f'<rect x="{pad-8}" y="{pad-8}" width="{cols*cell+16}" height="{rows*cell+16}" rx="14" fill="rgba(255,255,255,0.02)" stroke="rgba(255,255,255,0.06)"/>',
    ]

    # floor grid
    for gy in range(rows):
        for gx in range(cols):
            x = pad + gx * cell
            y = pad + gy * cell
            tone = "#152038" if (gx + gy) % 2 == 0 else "#10182c"
            parts.append(
                f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" fill="{tone}" stroke="rgba(255,255,255,0.025)"/>'
            )

    # obstacles
    for ox, oy in obstacles:
        x, y = to_xy(ox, oy)
        parts.append(
            f'<rect x="{x+1.5}" y="{y+1.5}" width="{cell-3}" height="{cell-3}" rx="4" '
            f'fill="#3a455f" stroke="#7f8eab" stroke-width="1"/>'
        )
        parts.append(
            f'<path d="M{x+5} {y+5} L{x+cell-5} {y+cell-5} M{x+cell-5} {y+5} L{x+5} {y+cell-5}" '
            f'stroke="rgba(180,195,220,0.35)" stroke-width="1"/>'
        )

    # remembered resources
    for rx, ry in mem_res - vis_res:
        x, y = to_xy(rx, ry)
        cxr, cyr = x + cell / 2, y + cell / 2
        parts.append(f'<circle cx="{cxr}" cy="{cyr}" r="5" fill="#c9a227" opacity="0.55"/>')
        parts.append(f'<circle cx="{cxr}" cy="{cyr}" r="2.5" fill="#ffe08a" opacity="0.8"/>')

    # visible resources
    for rx, ry in vis_res:
        x, y = to_xy(rx, ry)
        cxr, cyr = x + cell / 2, y + cell / 2
        parts.append(f'<circle cx="{cxr}" cy="{cyr}" r="7" fill="#ffc857" filter="url(#softGlow)"/>')
        parts.append(f'<circle cx="{cxr}" cy="{cyr}" r="3" fill="#fff3c4"/>')

    def unit_marker(pos, color, label, glow=False, ring=None):
        if not pos or len(pos) != 2 or not in_view(int(pos[0]), int(pos[1])):
            return
        x, y = to_xy(int(pos[0]), int(pos[1]))
        ux, uy = x + cell / 2, y + cell / 2
        if glow:
            parts.append(f'<circle cx="{ux}" cy="{uy}" r="12" fill="{color}" opacity="0.18"/>')
        if ring:
            parts.append(f'<circle cx="{ux}" cy="{uy}" r="9" fill="none" stroke="{ring}" stroke-width="2"/>')
        parts.append(
            f'<circle cx="{ux}" cy="{uy}" r="6.5" fill="{color}" filter="url(#softGlow)" '
            f'stroke="rgba(255,255,255,0.65)" stroke-width="1.2"/>'
        )
        parts.append(
            f'<text x="{ux}" y="{uy+3.2}" text-anchor="middle" font-size="8" font-family="Segoe UI, Microsoft YaHei, sans-serif" '
            f'font-weight="700" fill="#0b1020">{label}</text>'
        )

    for w in rec.get("workers", []):
        cargo = bool(w.get("cargo"))
        unit_marker(
            w.get("pos"),
            "#57d6a3" if cargo else "#8aa4ff",
            "B" if cargo else "W",
            glow=cargo,
            ring="#9ef0c8" if cargo else None,
        )
    for v in rec.get("vanguards", []):
        unit_marker(v.get("pos"), "#ff6b9d", "V", glow=True)
    for r in rec.get("rangers", []):
        unit_marker(r.get("pos"), "#b38cff", "R", glow=True)

    # core last so it sits on top
    x, y = to_xy(cx, cy)
    ux, uy = x + cell / 2, y + cell / 2
    parts.append(f'<circle cx="{ux}" cy="{uy}" r="16" fill="url(#coreGlow)"/>')
    parts.append(
        f'<rect x="{ux-7}" y="{uy-7}" width="14" height="14" rx="3" transform="rotate(45 {ux} {uy})" '
        f'fill="#6ea8ff" stroke="#d7e8ff" stroke-width="1.4" filter="url(#softGlow)"/>'
    )
    parts.append(
        f'<text x="{ux}" y="{uy+3.2}" text-anchor="middle" font-size="8" font-family="Segoe UI, Microsoft YaHei, sans-serif" '
        f'font-weight="700" fill="#081018">C</text>'
    )

    # axis labels every 5 cells
    for x in range(xmin, xmax + 1):
        if x % 5 == 0:
            px, _ = to_xy(x, ymin)
            parts.append(
                f'<text x="{px + cell/2}" y="{height - 8}" text-anchor="middle" fill="#7f8eab" font-size="9" '
                f'font-family="Consolas, monospace">{x}</text>'
            )
    for yv in range(ymin, ymax + 1):
        if yv % 5 == 0:
            _, py = to_xy(xmin, yv)
            parts.append(
                f'<text x="{12}" y="{py + cell/2 + 3}" text-anchor="middle" fill="#7f8eab" font-size="9" '
                f'font-family="Consolas, monospace">{yv}</text>'
            )

    parts.append(
        f'<text x="{pad}" y="{18}" fill="#93a0bf" font-size="11" font-family="Segoe UI, Microsoft YaHei, sans-serif">'
        f'center {cx},{cy} | range +/-{radius}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def short_id(uid: str) -> str:
    return (uid or "?")[:8]


def action_kind(action: str, cargo: int = 0) -> str:
    text = action or ""
    if "DEPOSIT" in text:
        return "deposit"
    if "HARVEST" in text:
        return "harvest"
    if "WAIT" in text:
        return "wait"
    if cargo:
        return "cargo"
    if "scout" in text or "explore" in text:
        return "explore"
    if "enemy" in text or "SWEEP" in text or "SHOOT" in text:
        return "combat"
    if "MOVE" in text:
        return "move"
    return "other"


def action_label(action: str, cargo: int = 0) -> str:
    kind = action_kind(action, cargo)
    labels = {
        "deposit": "交矿",
        "harvest": "挖矿",
        "wait": "等待",
        "cargo": "回矿",
        "explore": "探索",
        "combat": "作战",
        "move": "移动",
        "other": "其他",
    }
    return labels.get(kind, "其他")


def check_stuck(history):
    if len(history) < 5:
        return []
    workers_pos = defaultdict(list)
    for rec in history:
        for w in rec.get("workers", []):
            workers_pos[w.get("id", "")].append(tuple(w.get("pos", [])))
    issues = []
    for wid, positions in workers_pos.items():
        if len(positions) < 5:
            continue
        recent = positions[:8]
        if len(set(recent)) == 1:
            issues.append(
                {
                    "level": "danger",
                    "title": "卡住",
                    "detail": f"{short_id(wid)} 连续 {len(positions)} 帧停在 {format_pos(recent[0])}",
                }
            )
        elif len(set(recent)) <= 2 and len(set(recent)) < len(recent):
            unique = list(set(recent))
            issues.append(
                {
                    "level": "warn",
                    "title": "来回走",
                    "detail": f"{short_id(wid)} 在 {format_pos(unique[0])} / {format_pos(unique[1])} 之间摆动",
                }
            )
    return issues


def summarize_workers(workers, actions):
    stats = {
        "cargo": 0,
        "explore": 0,
        "harvest": 0,
        "deposit": 0,
        "wait": 0,
        "move": 0,
        "combat": 0,
        "other": 0,
    }
    for w in workers:
        kind = action_kind(actions.get(w.get("id", ""), ""), w.get("cargo", 0))
        stats[kind] = stats.get(kind, 0) + 1
    return stats


def generate_html() -> str:
    rec, mtime = read_latest()
    history = read_history(40)
    issues = check_stuck(history)
    age = time.time() - mtime if mtime else 0

    if not rec:
        return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>战术仪表盘</title>
<style>body{margin:0;background:#0b1020;color:#e8eefc;font-family:'Segoe UI','Microsoft YaHei',sans-serif;display:grid;place-items:center;min-height:100vh}
.card{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);padding:28px 36px;border-radius:18px}</style>
</head><body><div class="card"><h1>暂无战术数据</h1><p>等待 tactic_log.jsonl 写入…</p></div></body></html>"""

    workers = rec.get("workers", [])
    vgs = rec.get("vanguards", [])
    rgs = rec.get("rangers", [])
    actions = rec.get("plan_unit_actions", {})
    resources = rec.get("resources", 0)
    capacity = rec.get("resource_capacity", 50)
    res_pct = 0 if not capacity else min(100, int(resources * 100 / capacity))
    enemies = rec.get("visible_enemies", 0)
    resource_cells = rec.get("resource_cells", [])
    map_mem = load_map_memory()
    svg_map = render_svg_map(rec, map_mem, radius=16)
    events = rec.get("events", [])[:8]
    running = age < 30
    status_text = "运行中" if running else "已停止"
    status_class = "ok" if running else "down"
    stats = summarize_workers(workers, actions)

    # resource trend sparkline from history
    trend = list(reversed([(h.get("tick"), h.get("resources", 0)) for h in history[:20]]))
    if trend:
        vals = [v for _, v in trend]
        mn, mx = min(vals), max(vals)
        span = max(1, mx - mn)
        points = []
        for i, v in enumerate(vals):
            x = 10 + i * (180 / max(1, len(vals) - 1))
            y = 48 - ((v - mn) / span) * 36
            points.append(f"{x:.1f},{y:.1f}")
        spark = " ".join(points)
        spark_svg = f'<polyline points="{spark}" fill="none" stroke="#57d6a3" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>'
    else:
        spark_svg = ""

    def unit_card(kind, unit, icon):
        uid = unit.get("id", "")
        action = actions.get(uid, "—")
        cargo = unit.get("cargo", 0)
        hp = unit.get("hp", "—")
        a_kind = action_kind(action, cargo)
        label = action_label(action, cargo)
        extra = f'<span class="pill cargo">矿 {cargo}</span>' if cargo else f'<span class="pill hp">HP {hp}</span>'
        return f"""
        <div class="unit {a_kind}">
          <div class="unit-top">
            <div class="unit-id"><span class="icon">{icon}</span>{short_id(uid)}</div>
            <span class="badge {a_kind}">{label}</span>
          </div>
          <div class="unit-meta">
            <span>{format_pos(unit.get('pos'))}</span>
            {extra}
          </div>
          <div class="unit-action">{action}</div>
        </div>"""

    worker_cards = "".join(unit_card("worker", w, "⛏") for w in workers) or '<div class="empty">暂无工人</div>'
    vg_cards = "".join(unit_card("vg", v, "⚔") for v in vgs) or '<div class="empty">暂无先锋</div>'
    rg_cards = "".join(unit_card("rg", r, "🏹") for r in rgs) or '<div class="empty">暂无游侠</div>'

    issues_html = ""
    if issues:
        items = "".join(
            f'<div class="issue {i["level"]}"><strong>{i["title"]}</strong><span>{i["detail"]}</span></div>'
            for i in issues
        )
        issues_html = f'<section class="panel issues-panel"><div class="panel-title">异常告警</div><div class="issues">{items}</div></section>'

    resource_html = ""
    if resource_cells:
        chips = "".join(f'<span class="chip">{format_pos(p)}</span>' for p in resource_cells[:8])
        resource_html = f'<div class="chip-row">{chips}</div>'
    else:
        resource_html = '<div class="muted">当前没有可见矿点</div>'
    remembered_res = map_mem.get("resources", [])
    if remembered_res:
        mem_chips = "".join(f'<span class="chip mem">{format_pos(p)}</span>' for p in remembered_res[:12])
        resource_html += f'<div class="muted" style="margin-top:8px">记忆矿点 {len(remembered_res)}</div><div class="chip-row">{mem_chips}</div>'

    event_html = ""
    if events:
        rows = ""
        for e in events:
            et = e.get("type", "?")
            reason = e.get("reason") or "—"
            actor = short_id(e.get("actor") or "")
            pos = format_pos(e.get("pos"))
            rows += f"<tr><td>{et}</td><td>{reason}</td><td>{actor}</td><td>{pos}</td></tr>"
        event_html = f"""
        <table class="events">
          <thead><tr><th>事件</th><th>原因</th><th>单位</th><th>位置</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>"""
    else:
        event_html = '<div class="muted">本帧没有特殊事件</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="2">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Arena Hero 战术仪表盘</title>
<style>
:root {{
  --bg0: #070b16;
  --bg1: #10182b;
  --card: rgba(255,255,255,0.045);
  --card-strong: rgba(255,255,255,0.07);
  --line: rgba(255,255,255,0.08);
  --text: #eef3ff;
  --muted: #93a0bf;
  --accent: #6ea8ff;
  --pink: #ff6b9d;
  --green: #57d6a3;
  --amber: #ffc857;
  --red: #ff6b6b;
  --purple: #b38cff;
  --shadow: 0 18px 50px rgba(0,0,0,.35);
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  min-height: 100vh;
  color: var(--text);
  font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
  background:
    radial-gradient(1200px 600px at 10% -10%, rgba(110,168,255,.18), transparent 55%),
    radial-gradient(900px 500px at 100% 0%, rgba(255,107,157,.12), transparent 45%),
    radial-gradient(700px 400px at 70% 100%, rgba(87,214,163,.10), transparent 40%),
    linear-gradient(180deg, #0a1020 0%, #070b16 100%);
}}
.wrap {{
  max-width: 1280px;
  margin: 0 auto;
  padding: 24px 20px 40px;
}}
.topbar {{
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
  margin-bottom: 18px;
}}
.brand h1 {{
  margin: 0;
  font-size: 28px;
  letter-spacing: .3px;
}}
.brand p {{
  margin: 6px 0 0;
  color: var(--muted);
  font-size: 14px;
}}
.status-pill {{
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-radius: 999px;
  background: rgba(255,255,255,.05);
  border: 1px solid var(--line);
  backdrop-filter: blur(10px);
  white-space: nowrap;
}}
.dot {{
  width: 10px;
  height: 10px;
  border-radius: 50%;
  box-shadow: 0 0 12px currentColor;
}}
.status-pill.ok .dot {{ background: var(--green); color: var(--green); }}
.status-pill.down .dot {{ background: var(--red); color: var(--red); }}
.hero {{
  display: grid;
  grid-template-columns: 1.4fr .9fr .9fr;
  gap: 14px;
  margin-bottom: 14px;
}}
.card {{
  background: linear-gradient(180deg, rgba(255,255,255,.06), rgba(255,255,255,.03));
  border: 1px solid var(--line);
  border-radius: 20px;
  box-shadow: var(--shadow);
  padding: 18px;
  position: relative;
  overflow: hidden;
}}
.card::before {{
  content: "";
  position: absolute;
  inset: 0 auto auto 0;
  width: 100%;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,.25), transparent);
}}
.kicker {{
  color: var(--muted);
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 1.2px;
  margin-bottom: 8px;
}}
.big {{
  font-size: 34px;
  font-weight: 700;
  line-height: 1.1;
}}
.sub {{
  margin-top: 8px;
  color: var(--muted);
  font-size: 13px;
}}
.metrics {{
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 14px;
}}
.metric {{
  padding: 16px;
  border-radius: 18px;
  background: var(--card);
  border: 1px solid var(--line);
}}
.metric .label {{ color: var(--muted); font-size: 12px; margin-bottom: 8px; }}
.metric .value {{ font-size: 24px; font-weight: 700; }}
.bar {{
  height: 8px;
  border-radius: 999px;
  background: rgba(255,255,255,.08);
  overflow: hidden;
  margin-top: 10px;
}}
.bar > span {{
  display: block;
  height: 100%;
  width: {res_pct}%;
  background: linear-gradient(90deg, #57d6a3, #6ea8ff);
}}
.layout {{
  display: grid;
  grid-template-columns: 1.4fr .9fr;
  gap: 14px;
}}
.panel {{
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 20px;
  padding: 16px;
  box-shadow: var(--shadow);
}}
.panel-title {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  font-size: 16px;
  font-weight: 700;
}}
.count {{
  color: var(--muted);
  font-size: 13px;
  font-weight: 500;
}}
.unit-grid {{
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}}
.unit {{
  border-radius: 16px;
  padding: 12px;
  background: rgba(255,255,255,.03);
  border: 1px solid rgba(255,255,255,.06);
  transition: transform .15s ease, border-color .15s ease;
}}
.unit:hover {{
  transform: translateY(-1px);
  border-color: rgba(255,255,255,.14);
}}
.unit.cargo {{ background: rgba(87,214,163,.08); }}
.unit.harvest {{ background: rgba(255,200,87,.08); }}
.unit.deposit {{ background: rgba(110,168,255,.10); }}
.unit.wait {{ background: rgba(255,107,107,.10); }}
.unit.explore {{ background: rgba(179,140,255,.08); }}
.unit.combat {{ background: rgba(255,107,157,.10); }}
.unit-top {{
  display: flex;
  justify-content: space-between;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}}
.unit-id {{
  font-family: Consolas, "Cascadia Mono", monospace;
  font-size: 13px;
  display: flex;
  gap: 6px;
  align-items: center;
}}
.icon {{ opacity: .9; }}
.badge {{
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 999px;
  border: 1px solid transparent;
}}
.badge.cargo, .badge.deposit {{ background: rgba(87,214,163,.15); color: #8ef0c4; }}
.badge.harvest {{ background: rgba(255,200,87,.15); color: #ffd98a; }}
.badge.wait {{ background: rgba(255,107,107,.15); color: #ff9b9b; }}
.badge.explore {{ background: rgba(179,140,255,.15); color: #d0b8ff; }}
.badge.combat {{ background: rgba(255,107,157,.15); color: #ff9ec0; }}
.badge.move, .badge.other {{ background: rgba(110,168,255,.12); color: #a9c8ff; }}
.unit-meta {{
  display: flex;
  justify-content: space-between;
  gap: 8px;
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 8px;
}}
.pill {{
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(255,255,255,.05);
}}
.unit-action {{
  font-size: 12px;
  color: #d7e1f7;
  line-height: 1.4;
  word-break: break-word;
}}
.side-stack {{
  display: grid;
  gap: 14px;
}}
.chip-row {{
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}}
.chip {{
  padding: 6px 10px;
  border-radius: 999px;
  background: rgba(110,168,255,.12);
  border: 1px solid rgba(110,168,255,.18);
  color: #c7dbff;
  font-size: 12px;
  font-family: Consolas, monospace;
}}
.chip.mem {{
  background: rgba(255,200,87,.10);
  border-color: rgba(255,200,87,.18);
  color: #ffe0a0;
}}
.map-panel .game-map {{
  width: 100%;
  height: auto;
  display: block;
  border-radius: 16px;
  background: rgba(0,0,0,.18);
  border: 1px solid rgba(255,255,255,.06);
}}
.map-stage {{
  overflow: auto;
  border-radius: 16px;
}}
.map-legend {{
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}}
.map-legend span {{
  font-size: 11px;
  color: var(--muted);
  padding: 4px 8px;
  border-radius: 999px;
  background: rgba(255,255,255,.04);
  border: 1px solid rgba(255,255,255,.06);
}}
.map-legend .dot {{
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  margin-right: 6px;
  vertical-align: -1px;
  box-shadow: 0 0 8px currentColor;
}}
.map-legend .dot.core {{ background:#6ea8ff; color:#6ea8ff; border-radius:2px; transform:rotate(45deg); }}
.map-legend .dot.cargo {{ background:#57d6a3; color:#57d6a3; }}
.map-legend .dot.worker {{ background:#8aa4ff; color:#8aa4ff; }}
.map-legend .dot.vg {{ background:#ff6b9d; color:#ff6b9d; }}
.map-legend .dot.rg {{ background:#b38cff; color:#b38cff; }}
.map-legend .dot.wall {{ background:#3a455f; color:#7f8eab; border-radius:2px; box-shadow:none; border:1px solid #7f8eab; }}
.map-legend .dot.ore {{ background:#ffc857; color:#ffc857; }}
.map-legend .dot.ore-mem {{ background:#c9a227; color:#c9a227; opacity:.8; }}

.muted {{ color: var(--muted); font-size: 13px; }}
.empty {{
  grid-column: 1 / -1;
  padding: 18px;
  text-align: center;
  color: var(--muted);
  border: 1px dashed rgba(255,255,255,.1);
  border-radius: 14px;
}}
.issues {{
  display: grid;
  gap: 8px;
}}
.issue {{
  display: grid;
  gap: 4px;
  padding: 12px;
  border-radius: 14px;
  border: 1px solid transparent;
}}
.issue.danger {{
  background: rgba(255,107,107,.10);
  border-color: rgba(255,107,107,.22);
}}
.issue.warn {{
  background: rgba(255,200,87,.10);
  border-color: rgba(255,200,87,.22);
}}
.issue strong {{ font-size: 13px; }}
.issue span {{ color: var(--muted); font-size: 12px; }}
.events {{
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}}
.events th, .events td {{
  text-align: left;
  padding: 8px 6px;
  border-bottom: 1px solid rgba(255,255,255,.06);
}}
.events th {{ color: var(--muted); font-weight: 600; }}
.footer {{
  margin-top: 16px;
  color: var(--muted);
  font-size: 12px;
  display: flex;
  justify-content: space-between;
  gap: 12px;
}}
.spark {{
  margin-top: 10px;
  width: 100%;
  height: 56px;
}}
.stat-row {{
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}}
.stat {{
  padding: 6px 10px;
  border-radius: 999px;
  background: rgba(255,255,255,.04);
  border: 1px solid rgba(255,255,255,.06);
  font-size: 12px;
  color: var(--muted);
}}
.stat b {{ color: var(--text); margin-left: 4px; }}
@media (max-width: 980px) {{
  .hero, .metrics, .layout, .unit-grid {{ grid-template-columns: 1fr; }}
  .topbar {{ flex-direction: column; }}
}}
</style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="brand">
        <h1>Arena Hero 战术仪表盘</h1>
        <p>实时决策视图 · Tick {rec.get('tick')} · 延迟 {rec.get('latency_ms', 0):.0f} ms</p>
      </div>
      <div class="status-pill {status_class}">
        <span class="dot"></span>
        <span>{status_text}</span>
        <span style="color:var(--muted)">日志 {age:.0f}s 前</span>
      </div>
    </div>

    <div class="hero">
      <div class="card">
        <div class="kicker">核心状态</div>
        <div class="big">{format_pos(rec.get('core_pos'))}</div>
        <div class="sub">动作 {rec.get('core_action') or '—'} · 状态 {rec.get('core_state') or '—'} · HP {rec.get('core_hp','?')} / Shield {rec.get('core_shield','?')}</div>
        <div class="stat-row">
          <div class="stat">人口 <b>{rec.get('population', 0)}</b></div>
          <div class="stat">人口层 <b>{rec.get('population_tier', 0)}</b></div>
          <div class="stat">保养 <b>{rec.get('upkeep_next_tick', 0)}</b></div>
          <div class="stat">信标 <b>{format_pos(rec.get('beacon_pos'))}</b></div>
        </div>
      </div>
      <div class="card">
        <div class="kicker">资源</div>
        <div class="big">{resources}<span style="font-size:18px;color:var(--muted)"> / {capacity}</span></div>
        <div class="bar"><span></span></div>
        <div class="sub">进度 {res_pct}% · 可见矿点 {rec.get('resource_cells_visible', 0)}</div>
        <svg class="spark" viewBox="0 0 200 56" preserveAspectRatio="none">{spark_svg}</svg>
      </div>
      <div class="card">
        <div class="kicker">战场</div>
        <div class="big">{enemies}</div>
        <div class="sub">可见敌人 · 工人 {len(workers)} · 先锋 {len(vgs)} · 游侠 {len(rgs)}</div>
        <div class="stat-row">
          <div class="stat">回矿 <b>{stats['cargo'] + stats['deposit']}</b></div>
          <div class="stat">探索 <b>{stats['explore']}</b></div>
          <div class="stat">等待 <b>{stats['wait']}</b></div>
          <div class="stat">挖矿 <b>{stats['harvest']}</b></div>
        </div>
      </div>
    </div>

    <div class="metrics">
      <div class="metric"><div class="label">回矿工人</div><div class="value">{stats['cargo'] + stats['deposit']}</div></div>
      <div class="metric"><div class="label">探索工人</div><div class="value">{stats['explore']}</div></div>
      <div class="metric"><div class="label">卡住 / 等待</div><div class="value">{stats['wait']}</div></div>
      <div class="metric"><div class="label">异常告警</div><div class="value">{len(issues)}</div></div>
    </div>

    {issues_html}

    <section class="panel map-panel" style="margin-bottom:14px">
      <div class="panel-title">
        <span>已知地图</span>
        <span class="count">墙 {map_mem.get('obstacle_count', 0)} · 记忆矿 {map_mem.get('resource_count', 0)} · 可见矿 {len(resource_cells)}</span>
      </div>
      <div class="map-stage">{svg_map}</div>
      <div class="map-legend">
        <span><i class="dot core"></i>核心</span>
        <span><i class="dot cargo"></i>带矿工人</span>
        <span><i class="dot worker"></i>空手工人</span>
        <span><i class="dot vg"></i>先锋</span>
        <span><i class="dot rg"></i>游侠</span>
        <span><i class="dot wall"></i>永久障碍</span>
        <span><i class="dot ore"></i>可见矿</span>
        <span><i class="dot ore-mem"></i>记忆矿</span>
      </div>
    </section>

    <div class="layout">
      <section class="panel">
        <div class="panel-title">
          <span>工人</span>
          <span class="count">{len(workers)} 个单位</span>
        </div>
        <div class="unit-grid">{worker_cards}</div>
      </section>

      <div class="side-stack">
        <section class="panel">
          <div class="panel-title"><span>先锋</span><span class="count">{len(vgs)}</span></div>
          <div class="unit-grid" style="grid-template-columns:1fr">{vg_cards}</div>
        </section>
        <section class="panel">
          <div class="panel-title"><span>游侠</span><span class="count">{len(rgs)}</span></div>
          <div class="unit-grid" style="grid-template-columns:1fr">{rg_cards}</div>
        </section>
        <section class="panel">
          <div class="panel-title"><span>矿点</span><span class="count">{len(resource_cells)} 可见</span></div>
          {resource_html}
        </section>
        <section class="panel">
          <div class="panel-title"><span>本帧事件</span><span class="count">{len(events)}</span></div>
          {event_html}
        </section>
      </div>
    </div>

    <div class="footer">
      <div>自动刷新 · 每 2 秒</div>
      <div>更新于 {time.strftime('%H:%M:%S')}</div>
    </div>
  </div>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(generate_html().encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    server = HTTPServer((HOST, PORT), Handler)
    print(f"[仪表盘] http://localhost:{PORT}")
    print("[仪表盘] Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[仪表盘] 已停止")
        server.server_close()


if __name__ == "__main__":
    main()
