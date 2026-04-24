"""SQLite connection, default path, and migrations."""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH_ENV = "BTVIZ_DB_PATH"
SCHEMA_VERSION = 1
_SCHEMA_FILE = Path(__file__).with_name("schema.sql")


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
        if version == 0:
            # executescript manages its own transaction, so don't wrap it.
            self.conn.executescript(_SCHEMA_FILE.read_text())
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
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
