"""Shared test bootstrap: keep mutable runtime state out of the repo root.

The root modules resolve their data paths at **import** time
(``tactic._data_dir()`` → ``tactic_log.jsonl`` / ``battle_log.jsonl`` / …,
``game_stats.STATS_PATH``, ``tactic_config.CONFIG_PATH``, …), so an isolation
that only runs at fixture time is too late: importing ``tactic`` would already
have pinned ``tactic_log.jsonl`` / ``game_stats.json`` / ``holds.json.lock`` to
the working directory, which is how those files ended up in the repo root.

``pytest_configure`` therefore creates a throwaway directory and exports
``ARENA_DATA_DIR`` before any test module is imported.  The autouse session
fixture below is the documented entry point: it yields that directory, and if
the variable was not set (e.g. a plugin reset it, or pytest was invoked with a
conftest-less path) it creates one via ``tmp_path_factory`` and exports it.

An explicitly configured ``ARENA_DATA_DIR`` — the ``patch.dict(os.environ, ...)``
blocks inside the tests — is never overridden: those set the variable for the
duration of a test and unittest.mock restores the previous value afterwards.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

ENV_KEY = "ARENA_DATA_DIR"

#: Directory created in pytest_configure (None when the variable was preset).
_CONFIGURED_DIR: Path | None = None


def pytest_configure(config: pytest.Config) -> None:
    """Point ARENA_DATA_DIR at a temp dir before test modules are imported."""
    global _CONFIGURED_DIR
    if os.environ.get(ENV_KEY, "").strip():
        return
    _CONFIGURED_DIR = Path(tempfile.mkdtemp(prefix="arenagame-pytest-"))
    os.environ[ENV_KEY] = str(_CONFIGURED_DIR)


@pytest.fixture(scope="session", autouse=True)
def arena_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-scoped isolated data dir shared by every test module."""
    configured = os.environ.get(ENV_KEY, "").strip()
    if configured:
        path = Path(configured)
        path.mkdir(parents=True, exist_ok=True)
        yield path
        return

    path = tmp_path_factory.mktemp("arena-data")
    os.environ[ENV_KEY] = str(path)
    try:
        yield path
    finally:
        os.environ.pop(ENV_KEY, None)
