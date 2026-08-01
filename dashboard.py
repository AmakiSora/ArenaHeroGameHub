"""Real-time tactic dashboard - lightweight HTTP server.
Run: python dashboard.py
Then open http://localhost:8080 in your browser.
"""
import json
import os
import time
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

LOG_FILE = "tactic_log.jsonl"
HOST = "0.0.0.0"
PORT = 4399


def read_latest():
    if not os.path.exists(LOG_FILE):
        return None, time.time()
    mtime = os.path.getmtime(LOG_FILE)
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        lines = [l.strip() for l in f if l.strip()]
    for line in reversed(lines):
        try:
            rec = json.loads(line)
            if rec.get("tick") and "plan_unit_actions" in rec:
                return rec, mtime
        except:
            continue
    return None, mtime


def read_history(ticks=30):
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        lines = [l.strip() for l in f if l.strip()]
    history = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
            if rec.get("tick") and "plan_unit_actions" in rec:
                history.append(rec)
                if len(history) >= ticks:
                    break
        except:
            continue
    return history


def check_stuck(history):
    if len(history) < 5:
        return []
    workers_pos = defaultdict(list)
    for rec in history:
        for w in rec.get("workers", []):
            wid = w.get("id", "")
            pos = tuple(w.get("pos", []))
            workers_pos[wid].append(pos)
    issues = []
    for wid, positions in workers_pos.items():
        if len(positions) < 5:
            continue
        recent = positions[:8]
        if len(set(recent)) == 1:
            issues.append(f"STUCK {wid[:8]} at {recent[0]} ({len(positions)} ticks)")
        elif len(set(recent)) <= 2 and len(set(recent)) < len(recent):
            unique = list(set(recent))
            issues.append(f"OSCILLATE {wid[:8]} between {unique}")
    return issues


def resource_trend(history):
    """Track resource count over time."""
    ticks = []
    for rec in reversed(history):
        ticks.append((rec["tick"], rec.get("res", 0)))
    return ticks


def format_pos(pos):
    if not pos:
        return "?"
    return f"({pos[0]},{pos[1]})"


def generate_html():
    rec, mtime = read_latest()
    history = read_history(30)
    issues = check_stuck(history)
    age = time.time() - mtime if mtime else 0

    if not rec:
        return "<html><body><h1>No data yet</h1></body></html>"

    workers = rec.get("workers", [])
    vgs = rec.get("vanguards", [])
    rgs = rec.get("rangers", [])
    actions = rec.get("plan_unit_actions", {})

    alive = "RUNNING" if age < 30 else "DOWN"
    alive_color = "green" if age < 30 else "red"

    w_rows = ""
    for w in workers:
        wid = w.get("id", "")
        action = actions.get(wid, "?")
        cargo = w.get("cargo", 0)
        cls = "cargo" if cargo else "explore" if "explore" in action else "wait" if "WAIT" in action else "harvest" if "HARVEST" in action else ""
        marker = "&#x1F4E6;" if cargo else "&#x1F50D;" if "explore" in action else "&#x23F3;" if "WAIT" in action else "&#x26CF;"
        w_rows += f"<tr class='{cls}'><td>{marker}</td><td>{wid[:8]}</td><td>{format_pos(w.get('pos',[]))}</td><td>{cargo}</td><td>{action}</td></tr>"

    vg_rows = ""
    for v in vgs:
        vid = v.get("id", "")
        action = actions.get(vid, "?")
        vg_rows += f"<tr><td>&#x2694;</td><td>{vid[:8]}</td><td>{format_pos(v.get('pos',[]))}</td><td>{v.get('hp','?')}</td><td>{action}</td></tr>"

    rg_rows = ""
    for r in rgs:
        rid = r.get("id", "")
        action = actions.get(rid, "?")
        rg_rows += f"<tr><td>&#x1F3F9;</td><td>{rid[:8]}</td><td>{format_pos(r.get('pos',[]))}</td><td>{r.get('hp','?')}</td><td>{action}</td></tr>"

    issues_html = ""
    if issues:
        issues_html = "<div class='issues'><h3>Issues</h3><ul>" + "".join(f"<li>{i}</li>" for i in issues) + "</ul></div>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="3">
<title>Arena Hero Tactic Dashboard</title>
<style>
body {{ font-family: 'Segoe UI', sans-serif; margin: 20px; background: #1a1a2e; color: #e0e0e0; }}
h1, h2, h3 {{ color: #e94560; }}
.status {{ display: inline-block; padding: 4px 12px; border-radius: 4px; font-weight: bold; }}
table {{ border-collapse: collapse; width: 100%; margin: 8px 0; }}
th, td {{ text-align: left; padding: 6px 12px; border-bottom: 1px solid #333; }}
th {{ background: #16213e; color: #e94560; }}
tr:hover {{ background: #0f3460; }}
.cargo {{ background: #1a3a2e; }}
.explore {{ background: #2a1a3e; }}
.wait {{ background: #3e1a1a; }}
.harvest {{ background: #3e3e1a; }}
.issues {{ background: #3e1a1a; border: 1px solid #e94560; border-radius: 8px; padding: 10px; margin: 12px 0; }}
.issues li {{ color: #ff6b6b; }}
.meta {{ font-size: 0.9em; color: #888; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
@media (max-width: 800px) {{ .grid {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<h1>Arena Hero Tactic</h1>
<div class="meta">
  Tick: <strong>{rec['tick']}</strong> |
  Core: {format_pos(rec.get('core_pos'))} -> {rec.get('core_action','?')} |
  Resources: {rec.get('res',0)}/{rec.get('max_res',50)} |
  Enemies: {rec.get('enemies',0)} |
  <span class="status" style="background:{alive_color};">{alive}</span> (log age: {age:.0f}s)
</div>
{issues_html}
<div class="grid">
  <div>
    <h2>Workers ({len(workers)})</h2>
    <table>
      <tr><th></th><th>ID</th><th>Pos</th><th>Cargo</th><th>Action</th></tr>
      {w_rows}
    </table>
  </div>
  <div>
    <h2>Vanguards ({len(vgs)})</h2>
    <table>
      <tr><th></th><th>ID</th><th>Pos</th><th>HP</th><th>Action</th></tr>
      {vg_rows}
    </table>
    <h2>Rangers ({len(rgs)})</h2>
    <table>
      <tr><th></th><th>ID</th><th>Pos</th><th>HP</th><th>Action</th></tr>
      {rg_rows}
    </table>
  </div>
</div>
<div class="meta">
  Last updated: {time.strftime('%H:%M:%S')} | Auto-refresh every 3s
</div>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(generate_html().encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress logs


def main():
    server = HTTPServer((HOST, PORT), Handler)
    print(f"[Dashboard] http://localhost:{PORT}")
    print(f"[Dashboard] Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Dashboard] stopped")
        server.server_close()


if __name__ == "__main__":
    main()