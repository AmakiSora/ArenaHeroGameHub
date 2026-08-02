#!/usr/bin/env python3
"""Container entrypoint: start tactic + dashboard with shared runtime data dir."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path("/app")
RUNTIME_DIR = Path(os.environ.get("ARENA_DATA_DIR", "/app/runtime")).resolve()

tactic_proc: subprocess.Popen[bytes] | None = None
tactic_log = None


def prepare_runtime() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["ARENA_DATA_DIR"] = str(RUNTIME_DIR)
    os.chdir(APP_DIR)


def start_tactic() -> subprocess.Popen[bytes] | None:
    global tactic_log

    api_key = os.environ.get("ARENA_HERO_API_KEY", "").strip()
    if not api_key:
        print(
            "[entrypoint] ARENA_HERO_API_KEY not set; skipping tactic process",
            flush=True,
        )
        return None

    if tactic_log is None:
        tactic_log = open(RUNTIME_DIR / "tactic_play.log", "a", encoding="utf-8")
    print(f"[entrypoint] starting tactic.py data_dir={RUNTIME_DIR}", flush=True)
    env = os.environ.copy()
    env["ARENA_DATA_DIR"] = str(RUNTIME_DIR)
    return subprocess.Popen(
        [sys.executable, str(APP_DIR / "tactic.py")],
        cwd=str(APP_DIR),
        stdout=tactic_log,
        stderr=subprocess.STDOUT,
        env=env,
    )


def stop_tactic() -> None:
    global tactic_proc
    if tactic_proc and tactic_proc.poll() is None:
        tactic_proc.terminate()
        try:
            tactic_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            tactic_proc.kill()
            tactic_proc.wait(timeout=5)
    tactic_proc = None


def shutdown(signum: int, _frame) -> None:
    print(f"[entrypoint] received signal {signum}, shutting down", flush=True)
    stop_tactic()
    if tactic_log is not None:
        tactic_log.close()
    sys.exit(0)


def main() -> int:
    global tactic_proc

    prepare_runtime()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    tactic_proc = start_tactic()

    host = os.environ.get("HOST", "0.0.0.0")
    port = os.environ.get("PORT", "4399")
    print(f"[entrypoint] starting dashboard on {host}:{port}", flush=True)

    env = os.environ.copy()
    env["ARENA_DATA_DIR"] = str(RUNTIME_DIR)
    dashboard = subprocess.Popen(
        [sys.executable, str(APP_DIR / "dashboard.py")],
        cwd=str(APP_DIR),
        env=env,
    )

    while True:
        if dashboard.poll() is not None:
            code = dashboard.returncode or 1
            print(f"[entrypoint] dashboard exited code={code}", flush=True)
            stop_tactic()
            return code
        if tactic_proc is not None and tactic_proc.poll() is not None:
            print(
                f"[entrypoint] tactic exited code={tactic_proc.returncode}; restarting",
                flush=True,
            )
            time.sleep(2)
            tactic_proc = start_tactic()
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
