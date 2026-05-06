"""``device_interrogation_log`` schema + ``Interrogations`` repo.

Covers the v7→v8 migration path AND the three-step write protocol
(``open_attempt`` → ``record_response`` | ``record_failure``) that
the active-interrogation driver uses to keep an honest audit trail
even when the radio call crashes mid-request.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.db.repos import Repos  # noqa: E402
from btviz.db.store import Store  # noqa: E402


def _seed_fixture(store: Store) -> dict[str, int]:
    """Insert the minimum rows the FK chain requires.

    Mirrors the same shape used elsewhere in the test suite — one
    project, one session, one address, one device.
    """
    repos = Repos(store)
    project = repos.projects.create("test")
    session = repos.sessions.start(
        project_id=project.id,
        source_type="live",
    )
    device = repos.devices.upsert(
        stable_key="aa:bb:cc:dd:ee:ff/random",
        kind="random",
        now=1_000.0,
    )
    address = repos.addresses.upsert(
        address="aa:bb:cc:dd:ee:ff",
        address_type="random",
        device_id=device.id,
        now=1_000.0,
    )
    return {
        "project_id": project.id,
        "session_id": session.id,
        "addr_id": address.id,
        "device_id": device.id,
    }


class InterrogationLogSchemaTests(unittest.TestCase):

    def test_fresh_db_has_table_and_indexes(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "fresh.db")
            try:
                tables = {
                    r[0] for r in store.conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertIn("device_interrogation_log", tables)
                indexes = {
                    r[0] for r in store.conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='index' "
                        "AND tbl_name='device_interrogation_log'"
                    ).fetchall()
                }
                self.assertIn("idx_dil_session_ts", indexes)
                self.assertIn("idx_dil_target_dev", indexes)
            finally:
                store.close()

    def test_fresh_db_columns_match_schema(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "fresh.db")
            try:
                cols = {
                    r[1] for r in store.conn.execute(
                        "PRAGMA table_info(device_interrogation_log)"
                    ).fetchall()
                }
            finally:
                store.close()
        expected = {
            "id", "session_id", "target_address_id", "target_device_id",
            "interrogator_sniffer_id", "requested_at", "responded_at",
            "primitive", "status", "error", "payload",
        }
        self.assertEqual(cols, expected)

    def test_v7_upgrades_to_v8_keeps_existing_rows(self):
        """An existing v7 DB with project/device data should keep it
        through the v7→v8 migration AND get the new table."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "db.sqlite"
            # Build at v8, then knock the version back to v7 so the
            # migrator has to apply v7→v8. Drop the new table first
            # so the rebuild path matches a real v7 DB shape.
            Store(p).close()
            conn = sqlite3.connect(str(p))
            conn.execute("DROP TABLE device_interrogation_log")
            conn.execute("PRAGMA user_version = 7")
            # Seed a project so we can verify it survives.
            conn.execute(
                "INSERT INTO projects (name, created_at) VALUES (?, ?)",
                ("survives", 1.0),
            )
            conn.commit()
            conn.close()

            # Re-open via Store → triggers _migrate from v7.
            store = Store(p)
            try:
                version = store.conn.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(version, 8)
                tables = {
                    r[0] for r in store.conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertIn("device_interrogation_log", tables)
                # Pre-existing project still there.
                rows = store.conn.execute(
                    "SELECT name FROM projects"
                ).fetchall()
                self.assertEqual([r[0] for r in rows], ["survives"])
            finally:
                store.close()


class InterrogationsRepoTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(Path(self._tmp.name) / "test.db")
        self.addCleanup(self.store.close)
        self.repos = Repos(self.store)
        self.fix = _seed_fixture(self.store)

    def test_open_then_record_response_writes_full_row(self):
        att_id = self.repos.interrogations.open_attempt(
            session_id=self.fix["session_id"],
            target_address_id=self.fix["addr_id"],
            target_device_id=self.fix["device_id"],
            interrogator_sniffer_id=None,
            primitive="scan_req",
            requested_at=1_010.0,
        )
        self.repos.interrogations.record_response(
            attempt_id=att_id,
            responded_at=1_010.123,
            payload=b"\x09name",
        )
        row = self.store.conn.execute(
            "SELECT status, requested_at, responded_at, payload, primitive "
            "FROM device_interrogation_log WHERE id = ?", (att_id,),
        ).fetchone()
        self.assertEqual(row["status"], "response")
        self.assertEqual(row["requested_at"], 1_010.0)
        self.assertAlmostEqual(row["responded_at"], 1_010.123, places=4)
        self.assertEqual(bytes(row["payload"]), b"\x09name")
        self.assertEqual(row["primitive"], "scan_req")

    def test_open_then_record_failure_timeout(self):
        att_id = self.repos.interrogations.open_attempt(
            session_id=self.fix["session_id"],
            target_address_id=self.fix["addr_id"],
            primitive="scan_req",
            requested_at=1_020.0,
        )
        self.repos.interrogations.record_failure(
            attempt_id=att_id,
            when=1_021.0,
            error="no SCAN_RSP within 1s",
            timed_out=True,
        )
        row = self.store.conn.execute(
            "SELECT status, error FROM device_interrogation_log WHERE id = ?",
            (att_id,),
        ).fetchone()
        self.assertEqual(row["status"], "timeout")
        self.assertEqual(row["error"], "no SCAN_RSP within 1s")

    def test_open_then_record_failure_error(self):
        att_id = self.repos.interrogations.open_attempt(
            session_id=self.fix["session_id"],
            target_address_id=self.fix["addr_id"],
            primitive="scan_req",
            requested_at=1_030.0,
        )
        self.repos.interrogations.record_failure(
            attempt_id=att_id,
            when=1_030.5,
            error="adapter not initialized",
            timed_out=False,
        )
        row = self.store.conn.execute(
            "SELECT status FROM device_interrogation_log WHERE id = ?",
            (att_id,),
        ).fetchone()
        self.assertEqual(row["status"], "error")

    def test_pending_left_behind_when_caller_crashes(self):
        # Simulates an interrogator process that opens an attempt then
        # crashes before recording response/failure. The audit row
        # should still be in 'pending' so a downstream sweeper can
        # see it. This is the entire point of the open/record split.
        self.repos.interrogations.open_attempt(
            session_id=self.fix["session_id"],
            target_address_id=self.fix["addr_id"],
            primitive="scan_req",
            requested_at=1_040.0,
        )
        row = self.store.conn.execute(
            "SELECT status FROM device_interrogation_log "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["status"], "pending")

    def test_recent_for_device_orders_newest_first(self):
        # Three attempts at increasing timestamps; recent_for_device
        # must yield them DESC and respect ``limit``.
        for ts in (1_100.0, 1_200.0, 1_300.0):
            self.repos.interrogations.open_attempt(
                session_id=self.fix["session_id"],
                target_address_id=self.fix["addr_id"],
                target_device_id=self.fix["device_id"],
                primitive="scan_req",
                requested_at=ts,
            )
        rows = self.repos.interrogations.recent_for_device(
            device_id=self.fix["device_id"], limit=2,
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [r["requested_at"] for r in rows],
            [1_300.0, 1_200.0],
        )

    def test_recent_for_device_excludes_other_devices(self):
        # Open one attempt for the seeded device, one for a NEW device.
        # recent_for_device must filter to only the queried device.
        other_device = self.repos.devices.upsert(
            stable_key="11:22:33:44:55:66/random",
            kind="random",
            now=1_400.0,
        )
        other_address = self.repos.addresses.upsert(
            address="11:22:33:44:55:66",
            address_type="random",
            device_id=other_device.id,
            now=1_400.0,
        )
        self.repos.interrogations.open_attempt(
            session_id=self.fix["session_id"],
            target_address_id=self.fix["addr_id"],
            target_device_id=self.fix["device_id"],
            primitive="scan_req",
            requested_at=1_500.0,
        )
        self.repos.interrogations.open_attempt(
            session_id=self.fix["session_id"],
            target_address_id=other_address.id,
            target_device_id=other_device.id,
            primitive="scan_req",
            requested_at=1_600.0,
        )
        rows = self.repos.interrogations.recent_for_device(
            device_id=self.fix["device_id"],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target_device_id"], self.fix["device_id"])


if __name__ == "__main__":
    unittest.main()
