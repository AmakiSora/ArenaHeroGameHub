from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_registry_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve(strict=False))
    with _registry_guard:
        return _thread_locks.setdefault(key, threading.RLock())


@contextmanager
def file_lock(path: Path | str) -> Iterator[None]:
    """Serialize a state-file transaction across threads and processes."""
    target = Path(path)
    lock_path = Path(f"{target}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with _thread_lock(lock_path):
        with lock_path.open("a+b") as lock_file:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                while True:
                    try:
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.01)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def atomic_write_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace a text file using a unique sibling temporary file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        if target.exists():
            os.chmod(temp_name, target.stat().st_mode)
        os.replace(temp_name, target)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def append_jsonl(
    path: Path | str,
    entries: list[dict],
    *,
    max_bytes: int = 2 * 1024 * 1024,
    keep_lines: int = 500,
) -> None:
    """Append JSONL dicts under the cross-process file lock.

    Shared by the tactic process (battle events) and the dashboard process
    (config changes) writing the same log concurrently. When the file grows
    past ``max_bytes`` it is trimmed to the newest ``keep_lines`` lines so the
    log never grows without bound.
    """
    if not entries:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries
    )
    with file_lock(target):
        with target.open("a", encoding="utf-8", newline="") as f:
            f.write(payload)
        try:
            if target.stat().st_size > max_bytes:
                _trim_jsonl_tail(target, keep_lines)
        except OSError:
            pass


def _trim_jsonl_tail(path: Path, keep_lines: int) -> None:
    """Rewrite the JSONL file keeping only the newest ``keep_lines`` lines."""
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= keep_lines:
        return
    try:
        with path.open("w", encoding="utf-8", newline="") as f:
            f.writelines(lines[-keep_lines:])
    except OSError:
        pass
