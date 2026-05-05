"""Round-trip tests for the preferences loader + saver.

Schema-driven: tests validate behavior on actual ``SCHEMA`` entries
rather than mocking the schema, so adding/removing fields can't
silently bypass the load/save invariants.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.preferences import (  # noqa: E402
    Preferences,
    SCHEMA,
    by_key,
    files,
    reset_singleton_for_tests,
)
from btviz.preferences.loader import (  # noqa: E402
    _resolve_path_default,
    load_all,
    save_all,
)


class LoadDefaultsTests(unittest.TestCase):
    """With no TOML files on disk, every field resolves to its default."""

    def test_load_with_no_files(self):
        with tempfile.TemporaryDirectory() as d:
            values = load_all(Path(d))
            self.assertEqual(set(values.keys()), {f.key for f in SCHEMA})
            for f in SCHEMA:
                # Path-style sentinels resolve to real paths.
                self.assertEqual(values[f.key], _resolve_path_default(f))


class RoundTripTests(unittest.TestCase):
    """Save → reload reproduces the in-memory values."""

    def test_round_trip_preserves_values(self):
        with tempfile.TemporaryDirectory() as d:
            prefs_dir = Path(d)
            prefs = Preferences.load(prefs_dir)
            # Mutate one field of each type.
            prefs.set("watchdog.stall_threshold_s", 90.0)
            prefs.set("cluster.max_per_class", 800)
            prefs.set("capture.coded_phy", True)
            prefs.save()

            # Each TOML file got written.
            for fname in files():
                self.assertTrue(
                    (prefs_dir / f"{fname}.toml").exists(),
                    f"missing TOML for {fname}",
                )

            # Reload via the loader (not through the cached instance).
            reload_values = load_all(prefs_dir)
            self.assertEqual(reload_values["watchdog.stall_threshold_s"], 90.0)
            self.assertEqual(reload_values["cluster.max_per_class"], 800)
            self.assertEqual(reload_values["capture.coded_phy"], True)


class ValidationTests(unittest.TestCase):

    def test_out_of_range_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as d:
            prefs_dir = Path(d)
            (prefs_dir / "capture.toml").write_text(
                "[watchdog]\n"
                "stall_threshold_s = 999999.0\n"   # past max=600
            )
            values = load_all(prefs_dir)
            field = by_key("watchdog.stall_threshold_s")
            self.assertEqual(values["watchdog.stall_threshold_s"], field.default)

    def test_wrong_type_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as d:
            prefs_dir = Path(d)
            (prefs_dir / "capture.toml").write_text(
                "[sniffer_flags]\n"
                "coded_phy = \"true\"\n"            # not a bool
            )
            values = load_all(prefs_dir)
            self.assertEqual(values["capture.coded_phy"], False)


class EnvOverrideTests(unittest.TestCase):
    """Env vars beat TOML for fields that declare an ``env``."""

    def test_env_overrides_toml(self):
        with tempfile.TemporaryDirectory() as d:
            prefs_dir = Path(d)
            (prefs_dir / "general.toml").write_text(
                "[paths]\n"
                'db_path = "/from/toml.db"\n'
            )
            old = os.environ.get("BTVIZ_DB_PATH")
            os.environ["BTVIZ_DB_PATH"] = "/from/env.db"
            try:
                values = load_all(prefs_dir)
                self.assertEqual(values["general.db_path"], "/from/env.db")
            finally:
                if old is None:
                    del os.environ["BTVIZ_DB_PATH"]
                else:
                    os.environ["BTVIZ_DB_PATH"] = old


class SingletonResetTests(unittest.TestCase):
    """Defensive — ensure tests can isolate the singleton."""

    def test_reset_singleton_returns_none(self):
        reset_singleton_for_tests(None)
        # A subsequent get_prefs() would re-read from disk; we don't
        # call it here to keep this test hermetic.

    def tearDown(self):
        reset_singleton_for_tests(None)


if __name__ == "__main__":
    unittest.main()
