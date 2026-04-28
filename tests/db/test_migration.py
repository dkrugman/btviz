"""Tests for Store._migrate(): fresh-DB and incremental upgrade paths."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from btviz.db.store import Store, _V1_TO_V2_SQL, _V2_TO_V3_SQL

# SQL needed to build a v1 fixture DB.
_SCHEMA_V1 = """
CREATE TABLE devices (
    id INTEGER PRIMARY KEY,
    stable_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    user_name TEXT, local_name TEXT, gatt_device_name TEXT,
    vendor TEXT, vendor_id INTEGER, oui_vendor TEXT, model TEXT,
    device_class TEXT, appearance INTEGER,
    identifiers_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE addresses (
    id INTEGER PRIMARY KEY,
    address TEXT NOT NULL,
    address_type TEXT NOT NULL,
    device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    resolved_via_irk_id INTEGER,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    UNIQUE(address, address_type)
);
CREATE INDEX idx_addresses_device ON addresses(device_id);
CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    description TEXT, created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    updated_at REAL NOT NULL DEFAULT (strftime('%s','now')));
CREATE TABLE sessions (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
    name TEXT, source_type TEXT NOT NULL, source_path TEXT,
    started_at REAL NOT NULL, ended_at REAL, notes TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE);
CREATE INDEX idx_sessions_project ON sessions(project_id);
CREATE TABLE observations (
    session_id INTEGER NOT NULL, device_id INTEGER NOT NULL,
    packet_count INTEGER NOT NULL DEFAULT 0, adv_count INTEGER NOT NULL DEFAULT 0,
    data_count INTEGER NOT NULL DEFAULT 0,
    rssi_min INTEGER, rssi_max INTEGER,
    rssi_sum INTEGER NOT NULL DEFAULT 0, rssi_samples INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    pdu_types_json TEXT NOT NULL DEFAULT '{}',
    channels_json TEXT NOT NULL DEFAULT '{}',
    phy_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_id, device_id));
CREATE TABLE groups (
    id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
    parent_group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
    name TEXT NOT NULL, color TEXT, collapsed INTEGER NOT NULL DEFAULT 0,
    pos_x REAL NOT NULL DEFAULT 0, pos_y REAL NOT NULL DEFAULT 0,
    width REAL, height REAL, z_order INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE);
CREATE INDEX idx_groups_project ON groups(project_id);
CREATE INDEX idx_groups_parent  ON groups(parent_group_id);
CREATE TABLE group_devices (
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, device_id));
CREATE TABLE device_layouts (
    project_id INTEGER NOT NULL, device_id INTEGER NOT NULL,
    pos_x REAL NOT NULL DEFAULT 0, pos_y REAL NOT NULL DEFAULT 0,
    collapsed INTEGER NOT NULL DEFAULT 1, hidden INTEGER NOT NULL DEFAULT 0,
    z_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, device_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE);
CREATE TABLE device_project_meta (
    project_id INTEGER NOT NULL, device_id INTEGER NOT NULL,
    label TEXT, color TEXT, tags_json TEXT NOT NULL DEFAULT '[]', notes TEXT,
    PRIMARY KEY (project_id, device_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE);
CREATE TABLE canvas_state (
    project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    zoom REAL NOT NULL DEFAULT 1.0, pan_x REAL NOT NULL DEFAULT 0,
    pan_y REAL NOT NULL DEFAULT 0, last_opened_at REAL);
CREATE TABLE irks (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key_hex TEXT NOT NULL, label TEXT,
    device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    notes TEXT, created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(project_id, key_hex));
CREATE TABLE ltks (
    id INTEGER PRIMARY KEY, key_hex TEXT NOT NULL, ediv INTEGER,
    rand_hex TEXT, label TEXT,
    device_a_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    device_b_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    notes TEXT, created_at REAL NOT NULL DEFAULT (strftime('%s','now')));
CREATE TABLE connections (
    id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL,
    access_address INTEGER NOT NULL,
    central_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    peripheral_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    started_at REAL NOT NULL, ended_at REAL, interval_us INTEGER,
    latency INTEGER, timeout_ms INTEGER,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE);
CREATE INDEX idx_connections_session ON connections(session_id);
CREATE TABLE broadcasts (
    id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL,
    broadcaster_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    broadcast_id INTEGER, broadcast_name TEXT, big_handle INTEGER,
    bis_count INTEGER, phy TEXT, encrypted INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE);
CREATE INDEX idx_broadcasts_session ON broadcasts(session_id);
CREATE TABLE broadcast_receivers (
    broadcast_id INTEGER NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    packets_received INTEGER NOT NULL DEFAULT 0,
    packets_lost INTEGER NOT NULL DEFAULT 0, rssi_avg REAL,
    PRIMARY KEY (broadcast_id, device_id));
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

_V3_TABLES = {
    "device_ad_history",
    "packets",
    "device_clusters",
    "device_cluster_members",
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def _make_v1_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA_V1)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()


def _make_v2_db(path: Path) -> None:
    _make_v1_db(path)
    conn = sqlite3.connect(str(path))
    conn.executescript(_V1_TO_V2_SQL)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()


class FreshDbTests(unittest.TestCase):

    def test_fresh_db_has_v3_tables(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "fresh.db")
            tables = _tables(store.conn)
            store.close()
            for t in _V3_TABLES:
                self.assertIn(t, tables, f"missing table: {t}")

    def test_fresh_db_version_is_3(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "fresh.db")
            version = store.conn.execute("PRAGMA user_version").fetchone()[0]
            store.close()
            self.assertEqual(version, 3)


class V1UpgradeTests(unittest.TestCase):

    def test_v1_upgrades_to_v3(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "v1.db"
            _make_v1_db(p)
            store = Store(p)
            tables = _tables(store.conn)
            version = store.conn.execute("PRAGMA user_version").fetchone()[0]
            store.close()
            self.assertEqual(version, 3)
            self.assertIn("sniffers", tables)
            for t in _V3_TABLES:
                self.assertIn(t, tables, f"missing after v1→v3: {t}")

    def test_v1_data_survives_upgrade(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "v1.db"
            _make_v1_db(p)
            # Insert a device row before upgrading.
            conn = sqlite3.connect(str(p))
            conn.execute(
                "INSERT INTO devices (stable_key, kind, first_seen, last_seen)"
                " VALUES ('pub:aa:bb:cc:dd:ee:ff', 'public_mac', 0, 0)"
            )
            conn.commit()
            conn.close()

            store = Store(p)
            count = store.conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
            store.close()
            self.assertEqual(count, 1)


class V2UpgradeTests(unittest.TestCase):

    def test_v2_upgrades_to_v3(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "v2.db"
            _make_v2_db(p)
            store = Store(p)
            tables = _tables(store.conn)
            version = store.conn.execute("PRAGMA user_version").fetchone()[0]
            store.close()
            self.assertEqual(version, 3)
            for t in _V3_TABLES:
                self.assertIn(t, tables, f"missing after v2→v3: {t}")

    def test_v3_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "v3.db"
            Store(p).close()          # creates at v3
            Store(p).close()          # should not re-migrate or crash


class SchemaIntegrityTests(unittest.TestCase):

    def test_device_ad_history_fk(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "fk.db")
            store.conn.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(Exception):
                store.conn.execute(
                    "INSERT INTO device_ad_history"
                    " (device_id, ad_type, ad_value, first_seen, last_seen)"
                    " VALUES (9999, 1, X'01', 0, 0)"
                )
            store.close()

    def test_device_cluster_members_cascade(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "cascade.db")
            store.conn.execute("PRAGMA foreign_keys = ON")
            store.conn.execute(
                "INSERT INTO device_clusters (created_at, last_decided_at)"
                " VALUES (0, 0)"
            )
            cluster_id = store.conn.execute(
                "SELECT id FROM device_clusters"
            ).fetchone()[0]
            store.conn.execute(
                "INSERT INTO devices (stable_key, kind, first_seen, last_seen)"
                " VALUES ('rs:11:22:33:44:55:66', 'random_static_mac', 0, 0)"
            )
            device_id = store.conn.execute(
                "SELECT id FROM devices"
            ).fetchone()[0]
            store.conn.execute(
                "INSERT INTO device_cluster_members"
                " (cluster_id, device_id, decided_at) VALUES (?, ?, 0)",
                (cluster_id, device_id),
            )
            store.conn.execute(
                "DELETE FROM device_clusters WHERE id = ?", (cluster_id,)
            )
            count = store.conn.execute(
                "SELECT COUNT(*) FROM device_cluster_members"
            ).fetchone()[0]
            store.close()
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
