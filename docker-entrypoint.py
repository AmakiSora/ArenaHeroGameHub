#!/usr/bin/env python3
"""Container entrypoint: start tactic + dashboard with shared runtime data dir.

Supervision policy (see OPTIMIZATION_REVIEW.md #5 — the old entrypoint kept the
container "healthy" with no bot running):

* tactic crashes get exponential backoff (2s → 4 → 8 → 16 → 30s cap) and are
  counted; more than ``TACTIC_MAX_CRASHES`` non-zero exits within 10 minutes
  means the container exits (``return 1``) so Docker's ``restart: unless-stopped``
  restarts the whole container instead of the entrypoint looping forever.
* a stall watchdog kills a tactic process that has not written
  ``tactic_log.jsonl`` for ``TACTIC_STALL_SECONDS`` (rotated JSONL: the ``.1``
  backup counts as progress) and hands it to the same restart path.
* ``tactic_play.log`` is rotated at ``PLAY_LOG_MAX_MB`` (2 backups) before it is
  opened; container stdout is additionally capped by compose's
  ``logging: json-file max-size``.

Logs are plain stdout / ``tactic_play.log`` — there is no ``logging`` module
configuration and no ``LOG_LEVEL`` knob.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import TextIO

from state_io import append_jsonl

APP_DIR = Path("/app")
RUNTIME_DIR = Path(os.environ.get("ARENA_DATA_DIR", "/app/runtime")).resolve()

PLAY_LOG_NAME = "tactic_play.log"
PLAY_LOG_BACKUPS = 2
TACTIC_LOG_NAME = "tactic_log.jsonl"

# tactic.py exits with this code when it detects a permanently desynced game
# session (a run of 409 TICK_MISMATCH rejects). It is the signal to restart the
# whole container rather than restarting tactic in place, which does not resync.
STALE_SESSION_EXIT = 3
# Clean gap (seconds) with no game connection before the container restarts, so
# the server has time to reset the player's command baseline.
STALE_SESSION_COOLDOWN = 60

# Crash-loop breaker: non-zero exits inside this window are counted.
CRASH_WINDOW_SECONDS = 600
# Restart backoff in seconds; the last value is the cap.
_RESTART_BACKOFF = (2, 4, 8, 16, 30)
# A run of at least this long counts as "it worked", so the backoff restarts
# from the bottom instead of staying at the cap.
_WARMUP_SECONDS = 120


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """Read a positive int from the environment, falling back with a warning."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(
            f"[entrypoint] ignoring non-integer {name}={raw!r}; using {default}",
            flush=True,
        )
        return default
    return max(minimum, value)


TACTIC_MAX_CRASHES = _env_int("TACTIC_MAX_CRASHES", 10)
TACTIC_STALL_SECONDS = _env_int("TACTIC_STALL_SECONDS", 180)
PLAY_LOG_MAX_MB = _env_int("PLAY_LOG_MAX_MB", 20)

tactic_proc: subprocess.Popen[bytes] | None = None
tactic_log: TextIO | None = None
tactic_started_at: float = 0.0


def _say(message: str) -> None:
    print(f"[entrypoint] {message}", flush=True)


def _has_api_key() -> bool:
    return bool(os.environ.get("ARENA_HERO_API_KEY", "").strip())


def _marker(message: str) -> None:
    """Leave a visible lifecycle trace (boot / restart / shutdown) in the logs.

    Writes the same line to the raw play log (tactic_play.log) and to the
    dashboard's categorized battle log (battle_log.jsonl, warn category), so a
    container restart is visible both in the raw log and in the 「战斗日志」panel.
    Best-effort: a log write must never break the entrypoint loop.
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        global tactic_log
        if tactic_log is None:
            _open_play_log()
        if tactic_log is not None:
            tactic_log.write(f"[entrypoint] {stamp} {message}\n")
            tactic_log.flush()
    except OSError:
        pass
    try:
        append_jsonl(
            RUNTIME_DIR / "battle_log.jsonl",
            [{"tick": None, "ts": time.time(), "cat": "warn", "msg": message}],
        )
    except Exception:
        pass


def _rotate_play_log() -> None:
    """Keep tactic_play.log bounded: shift .1/.2 backups once it passes the cap."""
    base = RUNTIME_DIR / PLAY_LOG_NAME
    try:
        size = base.stat().st_size
    except OSError:
        return
    if size < PLAY_LOG_MAX_MB * 1024 * 1024:
        return

    oldest = RUNTIME_DIR / f"{PLAY_LOG_NAME}.{PLAY_LOG_BACKUPS}"
    try:
        oldest.unlink()
    except OSError:
        pass
    for index in range(PLAY_LOG_BACKUPS - 1, 0, -1):
        source = RUNTIME_DIR / f"{PLAY_LOG_NAME}.{index}"
        target = RUNTIME_DIR / f"{PLAY_LOG_NAME}.{index + 1}"
        if source.exists():
            try:
                os.replace(source, target)
            except OSError:
                pass
    try:
        os.replace(base, RUNTIME_DIR / f"{PLAY_LOG_NAME}.1")
    except OSError:
        return
    _say(
        f"rotated {PLAY_LOG_NAME} ({size / 1024 / 1024:.1f} MB > {PLAY_LOG_MAX_MB} MB), "
        f"keeping {PLAY_LOG_BACKUPS} backups"
    )


def _open_play_log() -> None:
    """(Re)open tactic_play.log, rotating it first when it grew past the cap."""
    global tactic_log
    if tactic_log is not None:
        try:
            tactic_log.close()
        except OSError:
            pass
        tactic_log = None
    _rotate_play_log()
    tactic_log = open(RUNTIME_DIR / PLAY_LOG_NAME, "a", encoding="utf-8")


def prepare_runtime() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["ARENA_DATA_DIR"] = str(RUNTIME_DIR)
    os.chdir(APP_DIR)


def start_tactic() -> subprocess.Popen[bytes] | None:
    global tactic_started_at

    if not _has_api_key():
        _say("ARENA_HERO_API_KEY not set; skipping tactic process (dashboard-only mode)")
        return None

    _open_play_log()
    _say(f"starting tactic.py data_dir={RUNTIME_DIR}")
    env = os.environ.copy()
    env["ARENA_DATA_DIR"] = str(RUNTIME_DIR)
    proc = subprocess.Popen(
        [sys.executable, str(APP_DIR / "tactic.py")],
        cwd=str(APP_DIR),
        stdout=tactic_log,
        stderr=subprocess.STDOUT,
        env=env,
    )
    tactic_started_at = time.time()
    return proc


def _terminate(proc: subprocess.Popen[bytes] | None, *, what: str) -> None:
    """SIGTERM, then SIGKILL after 10s."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _say(f"{what} ignored SIGTERM; killing")
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _say(f"{what} did not exit after SIGKILL")


def stop_tactic() -> None:
    global tactic_proc
    _terminate(tactic_proc, what="tactic")
    tactic_proc = None


def stop_dashboard(dashboard: subprocess.Popen[bytes]) -> None:
    _terminate(dashboard, what="dashboard")


def _next_backoff(consecutive: int) -> int:
    index = min(max(consecutive, 1) - 1, len(_RESTART_BACKOFF) - 1)
    return _RESTART_BACKOFF[index]


def _tactic_log_mtime() -> float:
    """Newest mtime of tactic_log.jsonl and its rotated ``.1`` backup (0 if none)."""
    newest = 0.0
    for name in (TACTIC_LOG_NAME, f"{TACTIC_LOG_NAME}.1"):
        try:
            newest = max(newest, (RUNTIME_DIR / name).stat().st_mtime)
        except OSError:
            continue
    return newest


def _stall_seconds(now: float) -> float:
    """Seconds since the tactic last showed progress (log write, else start)."""
    return now - max(_tactic_log_mtime(), tactic_started_at)


def _kill_stalled_tactic(stalled_seconds: float) -> None:
    global tactic_proc
    message = (
        f"战术进程 {int(stalled_seconds)} 秒未写入 {TACTIC_LOG_NAME}"
        f"（阈值 {TACTIC_STALL_SECONDS}s），强制重启"
    )
    _say(
        f"STALL: no {TACTIC_LOG_NAME} progress for {int(stalled_seconds)}s "
        f"(> TACTIC_STALL_SECONDS={TACTIC_STALL_SECONDS}); restarting tactic"
    )
    _marker(message)
    _terminate(tactic_proc, what="stalled tactic")


def _sleep_with_dashboard_check(seconds: int, dashboard: subprocess.Popen[bytes]) -> bool:
    """Sleep in 1s slices so a dead dashboard during backoff is noticed."""
    deadline = time.monotonic() + seconds
    while True:
        if dashboard.poll() is not None:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(1.0, remaining))


def shutdown(signum: int, _frame) -> None:
    _say(f"received signal {signum}, shutting down")
    _marker(f"收到信号 {signum}，容器关闭")
    stop_tactic()
    if tactic_log is not None:
        tactic_log.close()
    sys.exit(0)


def main() -> int:
    global tactic_proc

    prepare_runtime()
    _marker(f"容器启动 (pid={os.getpid()})")
    _say(
        f"guards: TACTIC_MAX_CRASHES={TACTIC_MAX_CRASHES}/{CRASH_WINDOW_SECONDS}s, "
        f"TACTIC_STALL_SECONDS={TACTIC_STALL_SECONDS}, PLAY_LOG_MAX_MB={PLAY_LOG_MAX_MB}"
    )

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    tactic_proc = start_tactic()

    host = os.environ.get("HOST", "0.0.0.0")
    port = os.environ.get("PORT", "4399")
    _say(f"starting dashboard on {host}:{port}")

    env = os.environ.copy()
    env["ARENA_DATA_DIR"] = str(RUNTIME_DIR)
    dashboard = subprocess.Popen(
        [sys.executable, str(APP_DIR / "dashboard.py")],
        cwd=str(APP_DIR),
        env=env,
    )

    crash_times: deque[float] = deque()
    consecutive = 0

    while True:
        if dashboard.poll() is not None:
            code = dashboard.returncode or 1
            _say(f"dashboard exited code={code}")
            _marker(f"dashboard 退出 (code={code})，容器将退出")
            stop_tactic()
            return code

        if tactic_proc is not None and tactic_proc.poll() is not None:
            code = tactic_proc.returncode
            ran_for = time.time() - tactic_started_at
            if code == STALE_SESSION_EXIT:
                # tactic.py detected a permanently desynced game session (sustained
                # 409 TICK_MISMATCH). Restarting tactic in place does NOT recover —
                # only a fresh container (all connections closed) resyncs the
                # server baseline. Stop the dashboard, wait out a clean gap so the
                # server resets, then exit so docker's restart policy relaunches
                # the whole container.
                _say(
                    f"tactic exited code={code} (desynced session); "
                    f"waiting {STALE_SESSION_COOLDOWN}s then container restart"
                )
                _marker(
                    f"战术会话失同步（连续 TICK_MISMATCH），"
                    f"容器将在 {STALE_SESSION_COOLDOWN} 秒后自动重启"
                )
                stop_tactic()
                stop_dashboard(dashboard)
                time.sleep(STALE_SESSION_COOLDOWN)
                return code

            if ran_for >= _WARMUP_SECONDS:
                consecutive = 0  # it ran long enough to count as a fresh failure
            if code != 0:
                consecutive += 1
                now = time.time()
                crash_times.append(now)
                while crash_times and now - crash_times[0] > CRASH_WINDOW_SECONDS:
                    crash_times.popleft()
                if len(crash_times) > TACTIC_MAX_CRASHES:
                    _say(
                        f"tactic exited non-zero {len(crash_times)} times in "
                        f"{CRASH_WINDOW_SECONDS // 60} min (limit TACTIC_MAX_CRASHES="
                        f"{TACTIC_MAX_CRASHES}); exiting so Docker restarts the container"
                    )
                    _marker(
                        f"战术进程 {CRASH_WINDOW_SECONDS // 60} 分钟内非零退出 "
                        f"{len(crash_times)} 次（上限 {TACTIC_MAX_CRASHES}），"
                        "容器退出并交由 Docker 重启"
                    )
                    stop_tactic()
                    stop_dashboard(dashboard)
                    return 1

            delay = _next_backoff(consecutive)
            _say(f"tactic exited code={code} after {ran_for:.0f}s; restarting in {delay}s")
            _marker(f"战术进程退出 (code={code})，{delay} 秒后重启")
            if not _sleep_with_dashboard_check(delay, dashboard):
                code = dashboard.returncode or 1
                _say(f"dashboard exited code={code} during tactic backoff")
                _marker(f"dashboard 退出 (code={code})，容器将退出")
                return code
            tactic_proc = start_tactic()
            continue

        if (
            tactic_proc is not None
            and tactic_proc.poll() is None
            and _has_api_key()
            and time.time() - tactic_started_at >= _WARMUP_SECONDS
        ):
            stalled_seconds = _stall_seconds(time.time())
            if stalled_seconds >= TACTIC_STALL_SECONDS:
                _kill_stalled_tactic(stalled_seconds)
                continue  # next iteration sees the exit and restarts with backoff

        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
