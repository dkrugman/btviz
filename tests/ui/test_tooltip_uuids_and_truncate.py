"""Tooltip Service UUIDs + heavy-cluster address truncation.

Two coupled changes pinned here:

  * ``load_canvas_devices`` joins ``device_ad_history`` for AD types
    0x02 / 0x03 (incomplete / complete 16-bit service UUID lists) and
    surfaces them on ``CanvasDevice.service_uuids``. Cluster primaries
    inherit the union via ``_absorb_cluster_member``.
  * ``_build_tooltip`` renders a ``Service UUIDs`` section (with
    friendly names from ``_KNOWN_UUID16`` where available) and
    truncates the address list at ``_TOOLTIP_ADDR_MAX`` so a
    heavy-merge cluster (200+ absorbed RPAs) doesn't overflow Qt's
    tooltip viewport.
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from btviz.db.repos import Repos  # noqa: E402
from btviz.db.store import Store  # noqa: E402
from btviz.ui.canvas import (  # noqa: E402
    CanvasDevice,
    _absorb_cluster_member,
    _build_tooltip,
    _KNOWN_UUID16,
    _TOOLTIP_ADDR_MAX,
    load_canvas_devices,
)


def _u16le(v: int) -> bytes:
    return struct.pack("<H", v)


class ServiceUuidLoadingTests(unittest.TestCase):

    def setUp(self) -> None:
        d = tempfile.mkdtemp()
        self.store = Store(Path(d) / "uuid.db")
        self.repos = Repos(self.store)
        self.project = self.repos.projects.create("p")
        self.session = self.repos.sessions.start(
            self.project.id, source_type="live", name="s",
        )

    def tearDown(self) -> None:
        self.store.close()

    def _create_dev_with_uuids(
        self, suffix: str, uuids: list[int],
    ) -> int:
        dev = self.repos.devices.upsert(
            f"rpa:00:11:22:33:44:{suffix}", "rpa",
        )
        self.repos.observations.record_packet(
            self.session.id, dev.id,
            ts=1.0, is_adv=True, rssi=-60, channel=37,
            phy="1M", pdu_type="ADV_IND",
        )
        # AD type 3 (complete UUID16 list); each row is one UUID.
        entries = [(3, _u16le(u)) for u in uuids]
        self.repos.ad_history.upsert_many(dev.id, entries, ts=1.0)
        return dev.id

    def test_load_pulls_uuids_from_ad_history(self):
        # HAS service plus Apple Continuity — covers a known and a
        # known-vendor UUID, and verifies the LE-decode path.
        dev_id = self._create_dev_with_uuids("01", [0x1854, 0xFD6F])

        loaded = {d.device_id: d for d in load_canvas_devices(
            self.store, self.project.id,
        )}
        self.assertEqual(
            sorted(loaded[dev_id].service_uuids),
            [0x1854, 0xFD6F],
        )

    def test_load_dedups_uuids(self):
        # Same UUID inserted via two AD-history rows (e.g., one with
        # ad_type=2 incomplete, one with ad_type=3 complete) must
        # appear once on the loaded device.
        dev = self.repos.devices.upsert("rpa:00:11:22:33:44:0d", "rpa")
        self.repos.observations.record_packet(
            self.session.id, dev.id,
            ts=1.0, is_adv=True, rssi=-60, channel=37,
            phy="1M", pdu_type="ADV_IND",
        )
        # Incomplete + complete forms of the same UUID.
        self.repos.ad_history.upsert_many(
            dev.id, [(2, _u16le(0x180F))], ts=1.0,
        )
        self.repos.ad_history.upsert_many(
            dev.id, [(3, _u16le(0x180F))], ts=1.0,
        )

        loaded = {d.device_id: d for d in load_canvas_devices(
            self.store, self.project.id,
        )}
        self.assertEqual(loaded[dev.id].service_uuids, [0x180F])

    def test_cluster_primary_gets_union_of_member_uuids(self):
        # Two devices in the same cluster with overlapping but
        # distinct UUID sets — primary should inherit the union.
        primary = CanvasDevice(
            device_id=1, stable_key="rpa:a", kind="unresolved_rpa",
            label="primary", service_uuids=[0x1854, 0x184E],
        )
        member = CanvasDevice(
            device_id=2, stable_key="rpa:b", kind="unresolved_rpa",
            label="member", service_uuids=[0x184E, 0x1850, 0xFE2C],
        )
        _absorb_cluster_member(primary, member)
        self.assertEqual(
            sorted(primary.service_uuids),
            [0x184E, 0x1850, 0x1854, 0xFE2C],
        )


class TooltipRenderingTests(unittest.TestCase):

    def test_service_uuid_section_renders_with_friendly_names(self):
        d = CanvasDevice(
            device_id=1, stable_key="rpa:a", kind="unresolved_rpa",
            label="t",
            service_uuids=[0x1854, 0xFE2C, 0x9999],  # known, known, unknown
        )
        tip = _build_tooltip(d)
        self.assertIn("Service UUIDs (3):", tip)
        self.assertIn("0x1854  HAS (Hearing Access)", tip)
        self.assertIn("0xFE2C  Google Fast Pair", tip)
        # Unknown UUID renders as bare hex with no friendly suffix.
        self.assertIn("0x9999", tip)
        self.assertNotIn("0x9999  ", tip)  # double-space marker means a name follows

    def test_no_uuid_section_when_empty(self):
        d = CanvasDevice(
            device_id=1, stable_key="rpa:a", kind="unresolved_rpa",
            label="t",
        )
        tip = _build_tooltip(d)
        self.assertNotIn("Service UUIDs", tip)

    def test_address_list_truncated_for_heavy_cluster(self):
        # Simulate a 200-RPA cluster's primary. Tooltip must show the
        # cap + a "+N more" line, not 200 individual addresses.
        addrs = [
            (f"aa:bb:cc:00:{i // 256:02x}:{i % 256:02x}", "rpa")
            for i in range(200)
        ]
        d = CanvasDevice(
            device_id=1, stable_key="rpa:a", kind="unresolved_rpa",
            label="t", addresses=addrs,
        )
        tip = _build_tooltip(d)

        # Header still reports the true count.
        self.assertIn("Addresses (200):", tip)
        # First N addresses appear in full; the (N+1)th is suppressed.
        self.assertIn(addrs[_TOOLTIP_ADDR_MAX - 1][0], tip)
        self.assertNotIn(addrs[_TOOLTIP_ADDR_MAX][0], tip)
        # Truncation marker shows the residual count.
        self.assertIn(f"+{200 - _TOOLTIP_ADDR_MAX} more", tip)

    def test_address_list_not_truncated_at_or_below_cap(self):
        # Devices with ≤ cap addresses shouldn't show a "+N more"
        # line — the marker is reserved for actual overflow.
        addrs = [
            (f"aa:bb:cc:00:00:{i:02x}", "rpa")
            for i in range(_TOOLTIP_ADDR_MAX)
        ]
        d = CanvasDevice(
            device_id=1, stable_key="rpa:a", kind="unresolved_rpa",
            label="t", addresses=addrs,
        )
        tip = _build_tooltip(d)
        self.assertNotIn("more", tip)

    def test_known_uuid_table_covers_le_audio_and_apple(self):
        # Spot-check the curated table — these are the IDs the user's
        # own data exercises (HA stack, Apple Continuity, Google
        # Nearby + Fast Pair).
        for uuid in (0x1854, 0x184E, 0x184F, 0x1850, 0x1853,
                     0xFD6F, 0xFE2C, 0xFDF0):
            self.assertIn(uuid, _KNOWN_UUID16, f"missing 0x{uuid:04X}")


if __name__ == "__main__":
    unittest.main()
