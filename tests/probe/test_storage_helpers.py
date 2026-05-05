"""Tests for the storage-layer helpers that aren't stubs.

``apply_result`` itself is still NotImplementedError until the
real probe coordinator lands; the helpers it depends on
(``value_hash``, ``value_text``, ``serialize_observation``) are
fully implemented and have invariants worth locking in.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.probe.storage import (  # noqa: E402
    serialize_observation,
    value_hash,
    value_text,
)
from btviz.probe.types import GattCharObservation  # noqa: E402


class ValueHashTests(unittest.TestCase):

    def test_hash_is_deterministic(self):
        self.assertEqual(value_hash(b"Apple Inc."), value_hash(b"Apple Inc."))

    def test_distinct_values_distinct_hashes(self):
        self.assertNotEqual(value_hash(b"Apple Inc."), value_hash(b"Apple"))

    def test_empty_bytes_have_a_hash(self):
        # Empty value is signal — distinct from "char absent" — so it
        # must round-trip through value_hash without becoming None or
        # crashing.
        h = value_hash(b"")
        self.assertEqual(h, "da39a3ee5e6b4b0d3255bfef95601890afd80709")


class ValueTextTests(unittest.TestCase):

    def test_printable_utf8_decodes(self):
        self.assertEqual(value_text(b"Apple Inc."), "Apple Inc.")

    def test_empty_bytes_decode_to_empty_string(self):
        # Distinct from None ("not human-readable").
        self.assertEqual(value_text(b""), "")

    def test_binary_blob_returns_none(self):
        self.assertIsNone(value_text(b"\x01\x02\x03\x04\x05"))

    def test_invalid_utf8_returns_none(self):
        self.assertIsNone(value_text(b"\xff\xfe\xfd"))


class SerializeObservationTests(unittest.TestCase):

    def test_value_observation_serializes_with_hash(self):
        obs = GattCharObservation(
            service_uuid="0000180a-0000-1000-8000-00805f9b34fb",
            char_uuid="00002a29-0000-1000-8000-00805f9b34fb",
            value=b"Apple Inc.",
        )
        row = serialize_observation(obs)
        self.assertIsNotNone(row["value_hash"])
        self.assertIsNone(row["att_error"])

    def test_error_observation_serializes_with_error_code(self):
        obs = GattCharObservation(
            service_uuid="0000180a-0000-1000-8000-00805f9b34fb",
            char_uuid="00002a25-0000-1000-8000-00805f9b34fb",
            att_error=0x02,   # read not permitted
        )
        row = serialize_observation(obs)
        self.assertIsNone(row["value_hash"])
        self.assertEqual(row["att_error"], 0x02)

    def test_neither_value_nor_error_is_an_error(self):
        # Storage CHECK constraint forbids this state on the row; the
        # serializer should reject before we ever reach SQLite.
        obs = GattCharObservation(
            service_uuid="x", char_uuid="y",
        )
        with self.assertRaises(ValueError):
            serialize_observation(obs)


if __name__ == "__main__":
    unittest.main()
