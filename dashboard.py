"""Arena Hero dashboard - pan/zoom SVG map + Chinese dark UI.
Run: python dashboard.py  -> http://localhost:4399
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


# ---------- data loading --------------------------------------------------

def read_latest():
    if not os.path.exists(LOG_FILE):
        return None, time.time()
    mtime = os.path.getmtime(LOG_FILE)
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        for line in reversed([l.strip() for l in f if l.strip()]):
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
    out = []
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        for line in reversed([l.strip() for l in f if l.strip()]):
            try:
                rec = json.loads(line)
                if rec.get("tick") and "plan_unit_actions" in rec:
                    out.append(rec)
                    if len(out) >= ticks:
                        break
            except Exception:
                continue
    return out


def load_map_memory():
    if not os.path.exists(MAP_FILE):
        return {"obstacles": [], "resources": [], "obstacle_count": 0, "resource_count": 0}
    try:
        with open(MAP_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return {
            "obstacles": [tuple(p) for p in d.get("obstacles", []) if len(p) == 2],
            "resources": [tuple(p) for p in d.get("resources", []) if len(p) == 2],
            "obstacle_count": d.get("obstacle_count", len(d.get("obstacles", []))),
            "resource_count": d.get("resource_count", len(d.get("resources", []))),
            "updated_tick": d.get("updated_tick"),
        }
    except Exception:
        return {"obstacles": [], "resources": [], "obstacle_count": 0, "resource_count": 0}


# ---------- helpers -------------------------------------------------------

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
    for g in ("workers", "vanguards", "rangers"):
        for u in rec.get(g, []) or []:
            p = u.get("pos") or []
            if len(p) == 2: pts.append((int(p[0]), int(p[1])))
    for p in rec.get("resource_cells", []) or []:
        if len(p) == 2: pts.append((int(p[0]), int(p[1])))
    for p in mm.get("obstacles", []): pts.append((int(p[0]), int(p[1])))
    for p in mm.get("resources", []): pts.append((int(p[0]), int(p[1])))
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
        return pad + (x - xmin) * cell, pad + (ymax - y) * cell

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
      f'data-width="{W}" data-height="{H}" '
      f'data-focus-x="{(core_cx if core_cx is not None else W/2):.1f}" '
      f'data-focus-y="{(core_cy if core_cy is not None else H/2):.1f}" '
      f'role="img" aria-label="known-map" style="width:{W}px;height:{H}px">')
    a('<defs>'
      '<pattern id="gridPat" width="{c}" height="{c}" patternUnits="userSpaceOnUse">'
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
      '</defs>'.format(c=cell))
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

    def unit(pos, color, label, glow=False, ring=None):
        if not pos or len(pos) != 2: return
        px, py = int(pos[0]), int(pos[1])
        if not (xmin <= px <= xmax and ymin <= py <= ymax): return
        x, y = to_xy(px, py)
        ux, uy = x + cell / 2, y + cell / 2
        if glow: a(f'<circle cx="{ux}" cy="{uy}" r="11" fill="{color}" opacity="0.18"/>')
        if ring: a(f'<circle cx="{ux}" cy="{uy}" r="8.5" fill="none" stroke="{ring}" stroke-width="2"/>')
        a(f'<circle cx="{ux}" cy="{uy}" r="6" fill="{color}" filter="url(#glow)" '
          f'stroke="rgba(255,255,255,0.65)" stroke-width="1.2"/>')
        a(f'<text x="{ux}" y="{uy+3}" text-anchor="middle" font-size="8" '
          f'font-family="Segoe UI, Microsoft YaHei, sans-serif" font-weight="700" fill="#0b1020">{label}</text>')

    for w in rec.get("workers", []):
        c = bool(w.get("cargo"))
        unit(w.get("pos"), "#57d6a3" if c else "#8aa4ff", "B" if c else "W", glow=c, ring="#9ef0c8" if c else None)
    for v in rec.get("vanguards", []):
        unit(v.get("pos"), "#ff6b9d", "V", glow=True)
    for r in rec.get("rangers", []):
        unit(r.get("pos"), "#b38cff", "R", glow=True)

    if core_cx is not None:
        a(f'<circle cx="{core_cx}" cy="{core_cy}" r="15" fill="url(#coreGlow)"/>')
        a(f'<rect x="{core_cx-6.5}" y="{core_cy-6.5}" width="13" height="13" rx="3" '
          f'transform="rotate(45 {core_cx} {core_cy})" fill="#6ea8ff" stroke="#d7e8ff" '
          f'stroke-width="1.4" filter="url(#glow)"/>')
        a(f'<text x="{core_cx}" y="{core_cy+3}" text-anchor="middle" font-size="8" '
          f'font-family="Segoe UI, Microsoft YaHei, sans-serif" font-weight="700" fill="#081018">C</text>')

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
 --accent:#6ea8ff;--pink:#ff6b9d;--green:#57d6a3;--amber:#ffc857;
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
.wrap{max-width:1280px;margin:0 auto;padding:24px 20px 40px}
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
.unit-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
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
.map-stage{position:relative;height:min(68vh,720px);overflow:hidden;border-radius:16px;
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
.map-legend .dot.vg{background:#ff6b9d;color:#ff6b9d}
.map-legend .dot.rg{background:#b38cff;color:#b38cff}
.map-legend .dot.wall{background:#3a455f;color:#7f8eab;border-radius:2px;box-shadow:none;border:1px solid #7f8eab;width:9px;height:9px}
.map-legend .dot.ore{background:#ffc857;color:#ffc857}
.map-legend .dot.ore-mem{background:#c9a227;color:#c9a227;opacity:.8}
@media (max-width:980px){.hero,.metrics,.layout,.unit-grid{grid-template-columns:1fr}.topbar{flex-direction:column}}
"""

JS = r"""
<script>
(function(){
  const KEY='arenaMapView.v1';
  const stage=document.getElementById('mapStage');
  const svg=document.getElementById('gameMap');
  if(!stage||!svg){return;}

  // Prevent native image/svg drag ghosts
  svg.addEventListener('dragstart', function(e){ e.preventDefault(); });
  stage.addEventListener('dragstart', function(e){ e.preventDefault(); });

  const baseW=Number(svg.dataset.width||svg.getAttribute('width')||800);
  const baseH=Number(svg.dataset.height||svg.getAttribute('height')||600);
  const focusX=Number(svg.dataset.focusX||(baseW/2));
  const focusY=Number(svg.dataset.focusY||(baseH/2));

  let view=null;
  try{ view=JSON.parse(localStorage.getItem(KEY)||'null'); }catch(e){ view=null; }

  function clamp(v,lo,hi){ return Math.max(lo, Math.min(hi, v)); }

  function defaultView(){
    const r=stage.getBoundingClientRect();
    // fit whole map with a little padding, but not too tiny
    const fit = Math.min(r.width/baseW, r.height/baseH) * 0.92;
    const s = clamp(fit, 0.15, 2.5);
    return {
      scale: s,
      x: r.width/2 - focusX * s,
      y: r.height/2 - focusY * s
    };
  }

  // If map size changed a lot (new walls discovered), reset if old view is nonsense
  const mapSig = baseW + 'x' + baseH;
  if(!view || typeof view.scale !== 'number' || view.mapSig !== mapSig){
    view = defaultView();
    view.mapSig = mapSig;
  }

  const zlbl=document.getElementById('zoomLabel');
  function apply(){
    svg.style.transformOrigin = '0 0';
    svg.style.transform = 'translate(' + view.x + 'px, ' + view.y + 'px) scale(' + view.scale + ')';
    if(zlbl) zlbl.textContent = Math.round(view.scale * 100) + '%';
    view.mapSig = mapSig;
    try{ localStorage.setItem(KEY, JSON.stringify(view)); }catch(e){}
  }

  function zoomAt(clientX, clientY, nextScale){
    const r=stage.getBoundingClientRect();
    const px = clientX - r.left;
    const py = clientY - r.top;
    const worldX = (px - view.x) / view.scale;
    const worldY = (py - view.y) / view.scale;
    view.scale = clamp(nextScale, 0.1, 6);
    view.x = px - worldX * view.scale;
    view.y = py - worldY * view.scale;
    apply();
  }

  let drag=false, moved=false, lx=0, ly=0;
  stage.addEventListener('pointerdown', function(e){
    if(e.button !== 0) return;
    drag=true; moved=false; lx=e.clientX; ly=e.clientY;
    stage.setPointerCapture(e.pointerId);
    svg.classList.add('dragging');
  });
  stage.addEventListener('pointermove', function(e){
    if(!drag) return;
    const dx=e.clientX-lx, dy=e.clientY-ly;
    if(Math.abs(dx)+Math.abs(dy) > 2) moved=true;
    view.x += dx; view.y += dy;
    lx=e.clientX; ly=e.clientY;
    apply();
  });
  function endDrag(e){
    if(!drag) return;
    drag=false;
    svg.classList.remove('dragging');
    try{ stage.releasePointerCapture(e.pointerId); }catch(err){}
  }
  stage.addEventListener('pointerup', endDrag);
  stage.addEventListener('pointercancel', endDrag);
  stage.addEventListener('pointerleave', function(e){ if(drag) endDrag(e); });

  stage.addEventListener('wheel', function(e){
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.12 : (1/1.12);
    zoomAt(e.clientX, e.clientY, view.scale * factor);
  }, {passive:false});

  // Touch pinch zoom (simple 2-finger)
  let pinch=null;
  stage.addEventListener('touchstart', function(e){
    if(e.touches.length===2){
      const a=e.touches[0], b=e.touches[1];
      const dist=Math.hypot(a.clientX-b.clientX, a.clientY-b.clientY);
      pinch={dist:dist, scale:view.scale, cx:(a.clientX+b.clientX)/2, cy:(a.clientY+b.clientY)/2};
    }
  }, {passive:true});
  stage.addEventListener('touchmove', function(e){
    if(e.touches.length===2 && pinch){
      e.preventDefault();
      const a=e.touches[0], b=e.touches[1];
      const dist=Math.hypot(a.clientX-b.clientX, a.clientY-b.clientY);
      const cx=(a.clientX+b.clientX)/2, cy=(a.clientY+b.clientY)/2;
      zoomAt(cx, cy, pinch.scale * (dist / pinch.dist));
    }
  }, {passive:false});
  stage.addEventListener('touchend', function(){ pinch=null; }, {passive:true});

  const zi=document.getElementById('zoomInBtn');
  const zo=document.getElementById('zoomOutBtn');
  const rst=document.getElementById('resetViewBtn');
  const fc=document.getElementById('focusCoreBtn');
  if(zi) zi.onclick=function(){ const r=stage.getBoundingClientRect(); zoomAt(r.left+r.width/2, r.top+r.height/2, view.scale*1.2); };
  if(zo) zo.onclick=function(){ const r=stage.getBoundingClientRect(); zoomAt(r.left+r.width/2, r.top+r.height/2, view.scale/1.2); };
  if(rst) rst.onclick=function(){ view=defaultView(); view.mapSig=mapSig; apply(); };
  if(fc) fc.onclick=function(){
    const r=stage.getBoundingClientRect();
    view.x = r.width/2 - focusX*view.scale;
    view.y = r.height/2 - focusY*view.scale;
    apply();
  };

  window.addEventListener('resize', function(){ apply(); });
  apply();

  // Auto refresh keeps camera. Skip while dragging.
  setInterval(function(){
    if(document.hidden || drag) return;
    try{ localStorage.setItem(KEY, JSON.stringify(view)); }catch(e){}
    location.reload();
  }, 2000);
})();
</script>
"""


def unit_card(kind: str, uid: str, pos, extra: str, badge_text: str, badge_cls: str):
    return (f'<div class="unit {kind}">'
            f'<div class="unit-top"><div class="unit-id">{short_id(uid)}</div>'
            f'<span class="badge {badge_cls}">{badge_text}</span></div>'
            f'<div class="unit-meta"><span>{fmt_pos(pos)}</span><span>{extra}</span></div></div>')


def generate_html() -> str:
    rec, mtime = read_latest()
    history = read_history(40)
    issues = check_stuck(history)
    age = time.time() - mtime if mtime else 0
    if not rec:
        return ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>战术仪表盘</title>'
                '<style>' + CSS + '</style></head><body><div class="wrap"><div class="card"><h1>暂无数据</h1>'
                '<p class="muted">等待 tactic_log.jsonl 写入…</p></div></div></body></html>')

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
        stats[action_kind(actions.get(short_id(w.get("id", "")), ""), w.get("cargo", 0))] += 1

    def wcard(w):
        wid = w.get("id", "")
        act = actions.get(short_id(wid), "")
        cargo = w.get("cargo", 0)
        kind = action_kind(act, cargo)
        badge = action_label(act, cargo)
        extra = f'<span class="pill">矿 {cargo}</span>' if cargo else f'<span class="pill">HP {w.get("hp","?")}</span>'
        meta = f'<div class="unit-meta"><span>{fmt_pos(w.get("pos"))}</span>{extra}</div>'
        act_div = f'<div class="unit-action">{act}</div>'
        return (f'<div class="unit {kind}"><div class="unit-top">'
                f'<div class="unit-id">{short_id(wid)}</div>'
                f'<span class="badge {kind}">{badge}</span></div>{meta}{act_div}</div>')

    w_html = "".join(wcard(w) for w in workers) or '<div class="empty">暂无工人</div>'

    def ucard(u, kind, color_cls, icon, label):
        uid = u.get("id", "")
        act = actions.get(short_id(uid), "")
        return (f'<div class="unit {color_cls}"><div class="unit-top">'
                f'<div class="unit-id">{short_id(uid)}</div>'
                f'<span class="badge {color_cls}">{label}</span></div>'
                f'<div class="unit-meta"><span>{fmt_pos(u.get("pos"))}</span>'
                f'<span class="pill">HP {u.get("hp","?")}</span></div>'
                f'<div class="unit-action">{act}</div></div>')

    vg_html = "".join(ucard(v, "vg", "combat", "V", "作战") for v in vgs) or '<div class="empty">暂无先锋</div>'
    rg_html = "".join(ucard(r, "rg", "combat", "R", "作战") for r in rgs) or '<div class="empty">暂无游侠</div>'

    issues_html = ""
    if issues:
        items = "".join(f'<div class="issue {i["level"]}"><strong>{i["title"]}</strong><span>{i["detail"]}</span></div>' for i in issues)
        issues_html = f'<section class="panel" style="margin-bottom:14px"><div class="panel-title">异常告警</div><div class="issues">{items}</div></section>'

    chips = "".join(f'<span class="chip">{fmt_pos(p)}</span>' for p in rcells[:12]) if rcells else '<div class="muted">当前无可见矿点</div>'
    mem_chips = "".join(f'<span class="chip mem">{fmt_pos(p)}</span>' for p in mm.get("resources", [])[:12])
    res_html = chips + (f'<div class="muted" style="margin-top:8px">记忆矿点 {mm.get("resource_count",0)}</div><div class="chip-row">{mem_chips}</div>' if mm.get("resources") else '')

    ev_rows = ""
    if events:
        for e in events:
            ev_rows += f"<tr><td>{e.get('type','')}</td><td>{e.get('reason') or '—'}</td><td>{short_id(e.get('actor')) if e.get('actor') else '—'}</td><td>{fmt_pos(e.get('pos'))}</td></tr>"
        ev_html = f'<table class="events"><thead><tr><th>事件</th><th>原因</th><th>单位</th><th>位置</th></tr></thead><tbody>{ev_rows}</tbody></table>'
    else:
        ev_html = '<div class="muted">本帧无特殊事件</div>'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><title>Arena Hero 战术仪表盘</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">
      <h1>Arena Hero 战术仪表盘</h1>
      <p>Tick {rec.get('tick')} · 延迟 {rec.get('latency_ms',0):.0f} ms · 核心 {fmt_pos(rec.get('core_pos'))}</p>
    </div>
    <div class="status-pill {status_cls}"><span class="dot"></span><span>{status_text}</span><span style="color:var(--muted)">日志 {age:.0f}s 前</span></div>
  </div>

  <div class="hero">
    <div class="card"><div class="kicker">核心状态</div>
     <div class="big">{fmt_pos(rec.get('core_pos'))}</div>
     <div class="sub">动作 {rec.get('core_action') or '—'} · 状态 {rec.get('core_state') or '—'} · HP {rec.get('core_hp','?')} / Shield {rec.get('core_shield','?')}</div>
     <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px">
      <span class="pill">人口 {rec.get('population',0)}</span>
      <span class="pill">人口层 {rec.get('population_tier',0)}</span>
      <span class="pill">保养 {rec.get('upkeep_next_tick',0)}</span>
      <span class="pill">信标 {fmt_pos(rec.get('beacon_pos'))}</span>
     </div>
    </div>
    <div class="card"><div class="kicker">资源</div>
     <div class="big">{resources}<span style="font-size:18px;color:var(--muted)"> / {cap}</span></div>
     <div class="bar"><span style="width:{pct}%"></span></div>
     <div class="sub">进度 {pct}% · 可见矿点 {len(rcells)}</div>
    </div>
    <div class="card"><div class="kicker">战场</div>
     <div class="big">{enemies}</div>
     <div class="sub">可见敌人 · 工人 {len(workers)} · 先锋 {len(vgs)} · 游侠 {len(rgs)}</div>
     <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px">
      <span class="pill">回矿 {stats['cargo']+stats['deposit']}</span>
      <span class="pill">探索 {stats['explore']}</span>
      <span class="pill">等待 {stats['wait']}</span>
      <span class="pill">挖矿 {stats['harvest']}</span>
     </div>
    </div>
  </div>

  <div class="metrics">
   <div class="metric"><div class="label">墙记忆</div><div class="value">{mm.get('obstacle_count',0)}</div></div>
   <div class="metric"><div class="label">可见障碍</div><div class="value">{rec.get('obstacle_cells_visible',0)}</div></div>
   <div class="metric"><div class="label">记忆矿点</div><div class="value">{mm.get('resource_count',0)}</div></div>
   <div class="metric"><div class="label">异常告警</div><div class="value">{len(issues)}</div></div>
  </div>

  {issues_html}

  <section class="panel map-panel" style="margin-bottom:14px">
   <div class="panel-title"><span>已知地图</span><span class="count">墙 {mm.get('obstacle_count',0)} · 记忆矿 {mm.get('resource_count',0)} · 可见矿 {len(rcells)}</span></div>
   <div class="map-toolbar">
    <button type="button" id="zoomOutBtn">-</button>
    <button type="button" id="zoomInBtn">+</button>
    <button type="button" id="resetViewBtn">重置视角</button>
    <button type="button" id="focusCoreBtn">定位核心</button>
    <span id="zoomLabel">100%</span>
    <span class="hint">拖动平移 · 滚轮缩放 · 视角自动记忆</span>
   </div>
   <div class="map-stage" id="mapStage">{svg}</div>
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
    <section class="panel"><div class="panel-title"><span>工人</span><span class="count">{len(workers)} 个</span></div>
     <div class="unit-grid">{w_html}</div>
    </section>
    <div class="side-stack">
     <section class="panel"><div class="panel-title"><span>先锋</span><span class="count">{len(vgs)}</span></div><div class="unit-grid" style="grid-template-columns:1fr">{vg_html}</div></section>
     <section class="panel"><div class="panel-title"><span>游侠</span><span class="count">{len(rgs)}</span></div><div class="unit-grid" style="grid-template-columns:1fr">{rg_html}</div></section>
     <section class="panel"><div class="panel-title"><span>矿点</span><span class="count">{len(rcells)} 可见</span></div>{res_html}</section>
     <section class="panel"><div class="panel-title"><span>本帧事件</span><span class="count">{len(events)}</span></div>{ev_html}</section>
    </div>
  </div>

  <div class="footer"><div>每 2 秒刷新 · 拖动地图可平移</div><div>更新于 {time.strftime('%H:%M:%S')}</div></div>
</div>
{JS}
</body></html>"""
    return html


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

    def log_message(self, fmt, *args):
        pass


def main():
    srv = HTTPServer((HOST, PORT), Handler)
    print(f"[dashboard] http://localhost:{PORT}")
    print("[dashboard] Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] 已停止")
        srv.server_close()


if __name__ == "__main__":
    main()
