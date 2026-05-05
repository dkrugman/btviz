"""User preferences — single source of truth via :mod:`schema`.

Public API:

* ``get_prefs()`` returns the singleton ``Preferences`` instance,
  loaded lazily on first call. Use this from consumption sites:

      from btviz.preferences import get_prefs
      threshold = get_prefs().get("watchdog.stall_threshold_s")

* ``Preferences.set(key, value)`` updates the in-memory value.
  Persistence is on ``save()``.

* ``Preferences.save()`` writes every value back to the per-file
  TOMLs.

* ``Preferences.reset(key)`` reverts one field to its schema default.
  ``reset_all()`` reverts everything.

The dialog (see :mod:`btviz.preferences.ui`) is the recommended way
for end users to edit preferences. Direct TOML hand-editing also
works — values are validated against the schema on load.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .loader import default_prefs_dir, load_all, save_all
from .schema import SCHEMA, Field, by_key, fields_for_file, files


class Preferences:
    """Loaded preferences. Validates against the schema."""

    def __init__(self, values: Mapping[str, Any], prefs_dir: Path) -> None:
        self._values = dict(values)
        self._prefs_dir = prefs_dir

    @classmethod
    def load(cls, prefs_dir: Path | None = None) -> "Preferences":
        prefs_dir = prefs_dir or default_prefs_dir()
        return cls(load_all(prefs_dir), prefs_dir)

    def get(self, key: str) -> Any:
        if key not in self._values:
            raise KeyError(f"unknown preference key: {key}")
        return self._values[key]

    def set(self, key: str, value: Any) -> None:
        if by_key(key) is None:
            raise KeyError(f"unknown preference key: {key}")
        self._values[key] = value

    def reset(self, key: str) -> None:
        f = by_key(key)
        if f is None:
            raise KeyError(f"unknown preference key: {key}")
        from .loader import _resolve_path_default
        self._values[key] = _resolve_path_default(f)

    def reset_all(self) -> None:
        for f in SCHEMA:
            self.reset(f.key)

    def save(self) -> None:
        save_all(self._values, self._prefs_dir)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._values)

    @property
    def prefs_dir(self) -> Path:
        return self._prefs_dir


# ──────────────────────────────────────────────────────────────────────
# Module-level singleton
# ──────────────────────────────────────────────────────────────────────

_singleton: Preferences | None = None


def get_prefs() -> Preferences:
    """Return the loaded ``Preferences``, constructing it on first use."""
    global _singleton
    if _singleton is None:
        _singleton = Preferences.load()
    return _singleton


def reset_singleton_for_tests(prefs: Preferences | None = None) -> None:
    """Replace the cached singleton — for tests only.

    Avoids cross-test contamination when each test loads from a
    different ``prefs_dir`` or constructs its own ``Preferences``.
    """
    global _singleton
    _singleton = prefs


__all__ = [
    "Field",
    "Preferences",
    "SCHEMA",
    "by_key",
    "fields_for_file",
    "files",
    "get_prefs",
    "reset_singleton_for_tests",
]
