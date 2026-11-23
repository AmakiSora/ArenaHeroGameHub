"""Archive/retention helpers for the hourly ArenaWatch log pull."""
from __future__ import annotations

import gzip
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

try:
    import _fetch_all_logs as fetch
except ImportError:  # local-only helper, absent in a clean CI checkout
    fetch = None


@unittest.skipUnless(fetch is not None, "_fetch_all_logs.py is a local-only helper")
class ArchiveTests(unittest.TestCase):
    def test_writes_gzip_and_skips_identical_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state: dict[str, str] = {}
            stamp = "2026-11-23_0005"

            first = fetch.archive_text(
                "tactic_log.jsonl", '{"tick": 1}\n', stamp, state,
                archive_root=root,
            )
            self.assertIsNotNone(first)
            self.assertEqual(
                gzip.decompress(first.read_bytes()).decode("utf-8"),
                '{"tick": 1}\n',
            )
            self.assertIn("tactic_log.jsonl", state)

            second = fetch.archive_text(
                "tactic_log.jsonl", '{"tick": 1}\n', "2026-11-23_0105", state,
                archive_root=root,
            )
            self.assertIsNone(second)

            changed = fetch.archive_text(
                "tactic_log.jsonl", '{"tick": 2}\n', "2026-11-23_0205", state,
                archive_root=root,
            )
            self.assertIsNotNone(changed)
            self.assertNotEqual(first, changed)
            self.assertEqual(len(list(root.rglob("*.gz"))), 2)

    def test_empty_content_is_not_archived(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state: dict[str, str] = {}
            path = fetch.archive_text(
                "battle_log.jsonl", "  \n", "2026-11-23_0005", state,
                archive_root=Path(temp_dir),
            )
            self.assertIsNone(path)
            self.assertEqual(state, {})

    def test_prune_keeps_the_inclusive_fifteen_day_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            today = date(2026, 11, 23)
            keep = today - timedelta(days=14)
            drop = today - timedelta(days=15)
            for day in (today, keep, drop):
                (root / day.isoformat()).mkdir(parents=True)
            (root / "not-a-day").mkdir()
            removed = fetch.prune_archive(
                today, keep_days=15, archive_root=root,
            )
            self.assertEqual([path.name for path in removed], [drop.isoformat()])
            self.assertTrue((root / today.isoformat()).exists())
            self.assertTrue((root / keep.isoformat()).exists())
            self.assertFalse((root / drop.isoformat()).exists())
            self.assertTrue((root / "not-a-day").exists())

    def test_prune_guerrilla_routes_by_filename_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            today = date(2026, 11, 23)
            keep = today - timedelta(days=14)
            drop = today - timedelta(days=15)
            for day in (today, keep, drop):
                (root / f"guerrilla_routes-{day.strftime('%Y%m%d')}.jsonl").write_text(
                    "x\n", encoding="utf-8",
                )
            removed = fetch.prune_guerrilla_routes(
                today, keep_days=15, review_root=root,
            )
            self.assertEqual(
                [path.name for path in removed],
                [f"guerrilla_routes-{drop.strftime('%Y%m%d')}.jsonl"],
            )
            self.assertTrue(
                (root / f"guerrilla_routes-{today.strftime('%Y%m%d')}.jsonl").exists()
            )
            self.assertFalse(
                (root / f"guerrilla_routes-{drop.strftime('%Y%m%d')}.jsonl").exists()
            )

    def test_retention_floor_is_at_least_fifteen_days(self) -> None:
        self.assertGreaterEqual(fetch.RETENTION_DAYS, 15)


if __name__ == "__main__":
    unittest.main()
