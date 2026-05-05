"""SQLite connection, default path, and migrations."""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH_ENV = "BTVIZ_DB_PATH"
SCHEMA_VERSION = 6
_SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# Incremental migrations applied to existing DBs to bring them up to
# ``SCHEMA_VERSION``. Fresh DBs get the full schema.sql (which already
# contains everything through SCHEMA_VERSION) and skip these.

# v6 — sniffers.stall_count + sniffers.last_stall_at. The capture
# stall watchdog (src/btviz/capture/watchdog.py) increments these
# whenever a sniffer's data path goes silent for the threshold and
# we restart its subprocess. The counter is lifetime (not session)
# so users can spot chronic per-dongle stalls across capture runs.
# Surfaced in the panel as a "STALL ×N" badge — the literal token
# matches the log output for grep-friendliness.
_V5_TO_V6_SQL = """
ALTER TABLE sniffers ADD COLUMN stall_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sniffers ADD COLUMN last_stall_at REAL;
"""

# v5 — device_clusters.confirmed. When True (the default for new
# auto-runs), the cluster runner treats the cluster as "decided" —
# future runs add members but never tear it down or remove members
# absent explicit negative evidence. Implements the user's "merges
# should be monotonic" intuition: once we say A and B are the same
# device, the cluster doesn't shrink just because the pair-edge
# weakens (e.g., Continuity payload rotated).
_V4_TO_V5_SQL = """
ALTER TABLE device_clusters ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0;
UPDATE device_clusters SET confirmed = 1;
"""

# v4 — observations.bad_packet_count. Per-(session, device) cumulative
# count of CRC-failed packets that the live-ingest cache attributed
# to this device. Lets the canvas show cumulative quality across
# capture sessions and after capture stops, not just live counters.
_V3_TO_V4_SQL = """
ALTER TABLE observations ADD COLUMN bad_packet_count INTEGER NOT NULL DEFAULT 0;
"""

_V2_TO_V3_SQL = """
CREATE TABLE device_ad_history (
    device_id   INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    ad_type     INTEGER NOT NULL,
    ad_value    BLOB    NOT NULL,
    first_seen  REAL    NOT NULL,
    last_seen   REAL    NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (device_id, ad_type, ad_value)
);
CREATE INDEX idx_dah_type ON device_ad_history(ad_type);

CREATE TABLE packets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id)  ON DELETE CASCADE,
    device_id   INTEGER NOT NULL REFERENCES devices(id)   ON DELETE CASCADE,
    address_id  INTEGER NOT NULL REFERENCES addresses(id) ON DELETE CASCADE,
    ts          REAL    NOT NULL,
    rssi        INTEGER NOT NULL,
    channel     INTEGER NOT NULL,
    pdu_type    INTEGER NOT NULL,
    sniffer_id  INTEGER REFERENCES sniffers(id),
    raw         BLOB
);
CREATE INDEX idx_packets_device_ts  ON packets(device_id,  ts);
CREATE INDEX idx_packets_sniffer_ts ON packets(sniffer_id, ts);
CREATE INDEX idx_packets_session_ts ON packets(session_id, ts);

CREATE TABLE device_clusters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT,
    created_at      REAL NOT NULL,
    last_decided_at REAL NOT NULL,
    source          TEXT NOT NULL DEFAULT 'auto'
);

CREATE TABLE device_cluster_members (
    cluster_id    INTEGER NOT NULL REFERENCES device_clusters(id) ON DELETE CASCADE,
    device_id     INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    score         REAL,
    contributions TEXT,
    profile       TEXT,
    decided_at    REAL NOT NULL,
    decided_by    TEXT NOT NULL DEFAULT 'auto',
    PRIMARY KEY (cluster_id, device_id)
);
CREATE INDEX idx_dcm_device ON device_cluster_members(device_id);
"""

_V1_TO_V2_SQL = """
CREATE TABLE sniffers (
    id              INTEGER PRIMARY KEY,
    serial_number   TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL DEFAULT 'unknown',
    name            TEXT,
    usb_port_id     TEXT,
    location_id_hex TEXT,
    interface_id    TEXT,
    display         TEXT,
    usb_product     TEXT,
    is_active       INTEGER NOT NULL DEFAULT 0,
    removed         INTEGER NOT NULL DEFAULT 0,
    first_seen      REAL NOT NULL,
    last_seen       REAL NOT NULL,
    notes           TEXT
);
CREATE INDEX idx_sniffers_active ON sniffers(is_active, removed);
CREATE INDEX idx_sniffers_location ON sniffers(location_id_hex);
"""


def default_db_path() -> Path:
    override = os.environ.get(DB_PATH_ENV)
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "btviz"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home())) / "btviz"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "btviz"
    return base / "btviz.db"


class Store:
    """Wraps a single SQLite connection. Not thread-safe; one per process/thread."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self._migrate()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        # Fresh DB: schema.sql is the source of truth and already includes
        # everything through SCHEMA_VERSION. Skip the per-version steps.
        if version == 0:
            self.conn.executescript(_SCHEMA_FILE.read_text())
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        # Existing DB at a prior version: apply incremental migrations.
        if version == 1:
            self.conn.executescript(_V1_TO_V2_SQL)
            self.conn.execute("PRAGMA user_version = 2")
            version = 2
        if version == 2:
            self.conn.executescript(_V2_TO_V3_SQL)
            self.conn.execute("PRAGMA user_version = 3")
            version = 3
        if version == 3:
            self.conn.executescript(_V3_TO_V4_SQL)
            self.conn.execute("PRAGMA user_version = 4")
            version = 4
        if version == 4:
            self.conn.executescript(_V4_TO_V5_SQL)
            self.conn.execute("PRAGMA user_version = 5")
            version = 5
        if version == 5:
            self.conn.executescript(_V5_TO_V6_SQL)
            self.conn.execute("PRAGMA user_version = 6")
            version = 6
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"Unknown db schema version {version}; app expects {SCHEMA_VERSION}"
            )

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Explicit transaction: BEGIN / COMMIT or ROLLBACK on exception."""
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def open_store(path: Path | None = None) -> Store:
    return Store(path or default_db_path())
