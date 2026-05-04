"""Tests for firmware-capability detection + capability-aware role assignment.

Covers:
  * ``is_firmware_tx_capable`` — the substring heuristic for inferring
    TX capability from extcap display + USB product strings.
  * ``default_roles`` — the role-assignment policy must reserve TX-
    capable devices as Idle when there are ≥ 4 dongles, and treat
    every dongle as interchangeable when there are ≤ 3.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.capture.capability import is_firmware_tx_capable  # noqa: E402
from btviz.capture.roles import (  # noqa: E402
    Idle, Pinned, ScanUnmonitored, default_roles,
)


class CapabilityHeuristicTests(unittest.TestCase):

    def test_sniffer_in_display_is_rx_only(self):
        self.assertFalse(is_firmware_tx_capable(
            usb_product="nRF Sniffer for BLE",
            display="nRF Sniffer for Bluetooth LE COM3",
        ))

    def test_sniffer_in_usb_product_is_rx_only(self):
        self.assertFalse(is_firmware_tx_capable(
            usb_product="nRF Sniffer for Bluetooth LE",
            display=None,
        ))

    def test_match_is_case_insensitive(self):
        self.assertFalse(is_firmware_tx_capable(
            usb_product="NRF SNIFFER",
            display=None,
        ))

    def test_connectivity_firmware_is_tx_capable(self):
        # nRF Connect / SoftDevice / SEGGER J-Link don't carry "sniffer".
        self.assertTrue(is_firmware_tx_capable(
            usb_product="nRF Connectivity",
            display="nRF52840-DK J-Link",
        ))

    def test_blank_strings_default_to_tx_capable(self):
        # Conservative-permissive default: unknown firmware is treated
        # as TX-capable so the user notices a no-op rather than missing
        # an active-capable device entirely.
        self.assertTrue(is_firmware_tx_capable(None, None))
        self.assertTrue(is_firmware_tx_capable("", ""))


class DefaultRolesTests(unittest.TestCase):

    def test_zero_dongles(self):
        self.assertEqual(default_roles([]), {})

    def test_one_dongle_scans_all(self):
        plan = default_roles(["a"])
        self.assertEqual(plan, {"a": ScanUnmonitored()})

    def test_two_dongles_pin_one_scan_one(self):
        plan = default_roles(["a", "b"])
        self.assertEqual(plan["a"], Pinned((37,)))
        self.assertEqual(plan["b"], ScanUnmonitored())

    def test_three_dongles_pin_each(self):
        plan = default_roles(["a", "b", "c"])
        self.assertEqual(plan["a"], Pinned((37,)))
        self.assertEqual(plan["b"], Pinned((38,)))
        self.assertEqual(plan["c"], Pinned((39,)))

    def test_three_dongles_capability_does_not_change_assignment(self):
        # ≤ 3 devices: every radio is needed for primary-channel coverage,
        # so a TX-capable device gets a sniffing role just like an
        # RX-only one. No reservation.
        plan = default_roles(
            ["rx1", "rx2", "tx1"],
            tx_capable_ids={"tx1"},
        )
        self.assertEqual(plan["rx1"], Pinned((37,)))
        self.assertEqual(plan["rx2"], Pinned((38,)))
        self.assertEqual(plan["tx1"], Pinned((39,)))

    def test_four_dongles_no_capability_info_legacy_behavior(self):
        # When tx_capable_ids is None or empty, every dongle is
        # interchangeable: first 3 pin, rest idle (pre-capability).
        plan = default_roles(["a", "b", "c", "d"])
        self.assertEqual(plan["a"], Pinned((37,)))
        self.assertEqual(plan["b"], Pinned((38,)))
        self.assertEqual(plan["c"], Pinned((39,)))
        self.assertEqual(plan["d"], Idle())

    def test_four_dongles_reserves_tx_capable(self):
        # The user's exact case: 6 RX-only sniffers + 1 TX-capable DK.
        # Pin 37/38/39 to three of the RX-only devices; the TX-capable
        # device stays Idle (reserved for interrogation).
        plan = default_roles(
            ["rx1", "rx2", "rx3", "rx4", "rx5", "rx6", "tx1"],
            tx_capable_ids={"tx1"},
        )
        # tx1 must NOT have a sniffing role.
        self.assertEqual(plan["tx1"], Idle())
        # The three pin roles must go to RX-only devices.
        pinned_ids = [
            d for d, r in plan.items() if isinstance(r, Pinned)
        ]
        self.assertEqual(len(pinned_ids), 3)
        for did in pinned_ids:
            self.assertNotIn(did, {"tx1"})

    def test_four_dongles_tx_listed_first_still_reserved(self):
        # Order in dongle_ids must NOT determine assignment; the
        # capability flag is what matters.
        plan = default_roles(
            ["tx1", "rx1", "rx2", "rx3"],
            tx_capable_ids={"tx1"},
        )
        self.assertEqual(plan["tx1"], Idle())
        self.assertIsInstance(plan["rx1"], Pinned)
        self.assertIsInstance(plan["rx2"], Pinned)
        self.assertIsInstance(plan["rx3"], Pinned)

    def test_four_dongles_two_tx_capable_both_reserved(self):
        # Two TX-capable devices in a 4-device fleet: both reserved
        # Idle. But that leaves only 2 RX-only — falls back to using
        # one TX-capable for the third pin role since we can't have
        # fewer than 3 pins. Verify which: when forced, sort puts
        # RX-only first, TX last; so TX devices fill remaining pins
        # only as needed.
        plan = default_roles(
            ["rx1", "rx2", "tx1", "tx2"],
            tx_capable_ids={"tx1", "tx2"},
        )
        # All four must be assigned.
        self.assertEqual(set(plan.keys()), {"rx1", "rx2", "tx1", "tx2"})
        # Two RX-only get pin roles.
        self.assertIsInstance(plan["rx1"], Pinned)
        self.assertIsInstance(plan["rx2"], Pinned)
        # One TX-capable forced into the third pin role; the other
        # stays Idle (reserved).
        tx_pinned = sum(
            1 for d in ("tx1", "tx2") if isinstance(plan[d], Pinned)
        )
        tx_idle = sum(
            1 for d in ("tx1", "tx2") if isinstance(plan[d], Idle)
        )
        self.assertEqual(tx_pinned, 1)
        self.assertEqual(tx_idle, 1)


if __name__ == "__main__":
    unittest.main()
