"""Single-instance lock for ``btviz canvas``.

Tests acquire/release semantics directly against the lock module,
plus PID-stamping for the conflict-message UX. The integration into
``run_canvas`` is exercised loosely by holding the lock from one
file handle and confirming a second ``acquire_db_lock`` call
returns ``acquired=False``.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.single_instance import (  # noqa: E402
    acquire_db_lock, conflict_message,
)


class AcquireLockTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "test.db"
        # We don't need a real SQLite file — the lock module only
        # touches ``<db_path>.lock``, not the DB itself.
        self.db_path.touch()

    def test_first_acquire_succeeds(self):
        result = acquire_db_lock(self.db_path)
        self.addCleanup(self._close, result.file_handle)
        self.assertTrue(result.acquired)
        self.assertIsNotNone(result.file_handle)
        self.assertEqual(
            result.lock_path, Path(str(self.db_path) + ".lock"),
        )
        # Lock file should exist now.
        self.assertTrue(result.lock_path.exists())

    def test_second_acquire_blocks_while_first_held(self):
        first = acquire_db_lock(self.db_path)
        self.addCleanup(self._close, first.file_handle)
        self.assertTrue(first.acquired)

        second = acquire_db_lock(self.db_path)
        self.assertFalse(second.acquired)
        self.assertIsNone(second.file_handle)

    def test_release_allows_reacquire(self):
        first = acquire_db_lock(self.db_path)
        self.assertTrue(first.acquired)
        # Release by closing the handle.
        assert first.file_handle is not None
        first.file_handle.close()

        second = acquire_db_lock(self.db_path)
        self.addCleanup(self._close, second.file_handle)
        self.assertTrue(second.acquired)

    def test_pid_stamped_into_lock_file(self):
        result = acquire_db_lock(self.db_path)
        self.addCleanup(self._close, result.file_handle)
        # Read the file content directly — should contain our PID.
        body = result.lock_path.read_text(encoding="utf-8").strip()
        self.assertEqual(int(body), os.getpid())

    def test_conflict_reports_holder_pid(self):
        first = acquire_db_lock(self.db_path)
        self.addCleanup(self._close, first.file_handle)
        second = acquire_db_lock(self.db_path)
        self.assertFalse(second.acquired)
        # The second acquire reads the lock file and surfaces the
        # holding process's PID for the dialog message.
        self.assertEqual(second.existing_pid, os.getpid())

    def test_different_dbs_dont_collide(self):
        # Lock domain is the DB path. Two different DBs should
        # both acquire successfully — supports the documented
        # ``--db <other>`` escape hatch for users who genuinely
        # want a second canvas on different data.
        other_db = Path(self._tmp.name) / "other.db"
        other_db.touch()
        a = acquire_db_lock(self.db_path)
        self.addCleanup(self._close, a.file_handle)
        b = acquire_db_lock(other_db)
        self.addCleanup(self._close, b.file_handle)
        self.assertTrue(a.acquired)
        self.assertTrue(b.acquired)

    @staticmethod
    def _close(fh):
        if fh is not None:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass


class ConflictMessageTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "test.db"

    def test_message_includes_pid_when_known(self):
        from btviz.single_instance import LockResult
        result = LockResult(
            acquired=False, file_handle=None,
            existing_pid=12345,
            lock_path=Path(str(self.db_path) + ".lock"),
        )
        msg = conflict_message(result)
        self.assertIn("PID 12345", msg)

    def test_message_omits_pid_when_unknown(self):
        # Legacy lock file from an older btviz that didn't stamp
        # a PID; conflict message should still be coherent.
        from btviz.single_instance import LockResult
        result = LockResult(
            acquired=False, file_handle=None,
            existing_pid=None,
            lock_path=Path(str(self.db_path) + ".lock"),
        )
        msg = conflict_message(result)
        self.assertNotIn("PID", msg)
        self.assertIn("already running", msg)

    def test_message_mentions_db_flag_escape_hatch(self):
        # Users who legitimately want two canvases (different DBs)
        # need to know the flag. Pin its presence so a future
        # message rewrite can't drop it without updating tests.
        from btviz.single_instance import LockResult
        result = LockResult(
            acquired=False, file_handle=None, existing_pid=None,
            lock_path=Path(str(self.db_path) + ".lock"),
        )
        msg = conflict_message(result)
        self.assertIn("--db", msg)


if __name__ == "__main__":
    unittest.main()
