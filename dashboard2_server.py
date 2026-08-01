"""Arena Hero 战术指挥台 — dashboard2 配套服务（零侵入原 dashboard.py）。

复用 dashboard 模块的全部数据加载与 API 逻辑，仅替换根页面为 dashboard2.html，
监听 4400 端口，与原 dashboard.py（4399）互不影响。

运行：python dashboard2_server.py  ->  http://localhost:4400
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# 确保能 import 同目录下的 dashboard / tactic_config / production_queue
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import dashboard  # 复用其全部数据/API 函数
import production_queue
from tactic_config import CONFIG_PATH, ConfigValidationError, default_config, load_config, save_config

HOST = "0.0.0.0"
PORT = 4400
HTML_FILE = _HERE / "dashboard2.html"


class Handler(BaseHTTPRequestHandler):
    server_version = "ArenaDashboard2/1.0"

    # ---------- 底层工具 ----------
    def _send(self, code: int, body: bytes, content_type: str):
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def log_message(self, fmt, *args):  # 静默逐请求日志
        return

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/dashboard2.html":
            try:
                body = HTML_FILE.read_bytes()
            except OSError:
                self._send(500, b"dashboard2.html not found", "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return

        if path == "/api/state":
            parts = dashboard.build_parts()
            if not parts:
                self._send_json(200, {"tick": None, "error": "no data"})
            else:
                self._send_json(200, parts)
            return

        if path == "/api/config":
            self._send_json(200, {"ok": True, "config": load_config(CONFIG_PATH)})
            return

        if path == "/api/teams":
            config = load_config(CONFIG_PATH)
            history = dashboard.read_history(1)
            rec = history[0] if history else {}
            self._send_json(200, {
                "ok": True,
                "config": config,
                "combat_units": dashboard.collect_combat_units(rec, config),
            })
            return

        if path == "/api/production-queue":
            try:
                self._send_json(200, production_queue.queue_payload())
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": f"读取队列失败: {exc}"})
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        data = self._read_json()

        # 配置保存 / 恢复默认
        if path in {"/api/config", "/api/config/reset"}:
            try:
                values = default_config() if path.endswith("/reset") else data
                if path == "/api/config":
                    current = load_config(CONFIG_PATH)
                    merged = dict(current)
                    for key, value in values.items():
                        merged[key] = value
                    values = merged
                config = save_config(values, CONFIG_PATH)
            except ConfigValidationError as exc:
                self._send_json(400, {"ok": False, "error": "配置值无效", "fields": exc.errors})
                return
            except OSError as exc:
                self._send_json(500, {"ok": False, "error": f"保存失败: {exc}"})
                return
            self._send_json(200, {"ok": True, "config": config})
            return

        # 分队保存
        if path == "/api/teams":
            try:
                current = load_config(CONFIG_PATH)
                merged = dict(current)
                for key in list(dashboard.TEAM_ROSTER_FIELDS) + list(dashboard.TEAM_SETTING_FIELDS):
                    if key in data:
                        merged[key] = data[key]
                for key in dashboard.TEAM_ROSTER_FIELDS:
                    merged[key] = dashboard._format_roster_names(dashboard._parse_roster_names(merged.get(key, "")))
                config = save_config(merged, CONFIG_PATH)
            except ConfigValidationError as exc:
                self._send_json(400, {"ok": False, "error": "分队配置无效", "fields": exc.errors})
                return
            except OSError as exc:
                self._send_json(500, {"ok": False, "error": f"保存失败: {exc}"})
                return
            history = dashboard.read_history(1)
            rec = history[0] if history else {}
            self._send_json(200, {
                "ok": True,
                "config": config,
                "combat_units": dashboard.collect_combat_units(rec, config),
            })
            return

        # 生产队列
        if path == "/api/production-queue/add":
            try:
                production_queue.enqueue(str(data.get("unit_type", "")))
                self._send_json(200, production_queue.queue_payload())
            except production_queue.InvalidUnitTypeError:
                self._send_json(400, {"ok": False, "error": "未知单位类型"})
            except production_queue.QueueFullError:
                self._send_json(409, {"ok": False, "error": "需求队列已达到 20 个"})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": f"加入队列失败: {exc}"})
            return

        if path == "/api/production-queue/remove":
            try:
                request_id = int(data.get("id"))
            except (TypeError, ValueError):
                self._send_json(400, {"ok": False, "error": "无效的队列编号"})
                return
            production_queue.remove_request(request_id)
            self._send_json(200, production_queue.queue_payload())
            return

        if path == "/api/production-queue/clear":
            production_queue.clear_requests()
            self._send_json(200, production_queue.queue_payload())
            return

        # 手动矿点
        try:
            x = int(data.get("x"))
            y = int(data.get("y"))
        except Exception:
            self._send_json(400, {"ok": False, "error": "x/y 必须是整数"})
            return

        if path == "/api/resource/add":
            self._send_json(200, dashboard.save_manual_resource(x, y))
            return
        if path == "/api/resource/remove":
            result = dashboard.remove_manual_resource(x, y)
            self._send_json(200 if result.get("ok") else 400, result)
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"[dashboard2] 全新战术指挥台  http://localhost:{PORT}")
    print(f"[dashboard2] 原 dashboard.py (4399) 不受影响")
    print("[dashboard2] 软刷新 /api/state · Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard2] 已停止")
        srv.server_close()


if __name__ == "__main__":
    main()
