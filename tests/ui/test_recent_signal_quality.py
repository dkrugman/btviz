"""Per-device Signal/Quality meters use a rolling window.

The meters average packet observations over the last few seconds so the
bars reflect *current* link health, not session-cumulative aggregates.
This test pins:

  * ``notify_channel_hit`` appends ``(ts, rssi, crc_ok)`` to the device's
    rolling deque
  * ``_recent_stats`` returns the in-window RSSI average and good/bad
    counts and returns ``(None, 0, 0)`` when the device has gone silent
  * Samples older than ``_RECENT_WINDOW_S`` are pruned on read so the
    meters drain to neutral after capture stops or the device leaves
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402

from btviz.ui.canvas import (  # noqa: E402
    CanvasDevice,
    DeviceItem,
    _RECENT_WINDOW_S,
)


_app: QApplication | None = None


def _ensure_app() -> QApplication:
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication([])
    return _app


def _make_item() -> DeviceItem:
    _ensure_app()
    dev = CanvasDevice(
        device_id=1, stable_key="rpa:aa:bb:cc:dd:ee:ff",
        kind="unresolved_rpa", label="test",
    )
    return DeviceItem(dev, persist_cb=lambda *_a, **_k: None)


class RecentStatsTests(unittest.TestCase):

    def test_empty_window_returns_none(self):
        item = _make_item()
        rssi_avg, good, bad = item._recent_stats()
        self.assertIsNone(rssi_avg)
        self.assertEqual((good, bad), (0, 0))

    def test_in_window_averages_rssi_and_counts_crc(self):
        item = _make_item()
        # Three clean packets at -60, -70, -80 → avg -70.
        item.notify_channel_hit(channel=37, crc_ok=True, rssi=-60)
        item.notify_channel_hit(channel=37, crc_ok=True, rssi=-70)
        item.notify_channel_hit(channel=37, crc_ok=True, rssi=-80)
        # One CRC-fail counted as bad; rssi mixed in too.
        item.notify_channel_hit(channel=37, crc_ok=False, rssi=-90)

        rssi_avg, good, bad = item._recent_stats()
        self.assertIsNotNone(rssi_avg)
        self.assertAlmostEqual(rssi_avg, (-60 - 70 - 80 - 90) / 4)
        self.assertEqual(good, 3)
        self.assertEqual(bad, 1)

    def test_packets_with_no_rssi_still_count_for_quality(self):
        # Quality should still be computable even if some packets had
        # no RSSI (e.g., decoder couldn't pin it down).
        item = _make_item()
        item.notify_channel_hit(channel=38, crc_ok=True, rssi=None)
        item.notify_channel_hit(channel=38, crc_ok=True, rssi=None)
        rssi_avg, good, bad = item._recent_stats()
        self.assertIsNone(rssi_avg)  # no RSSI samples in window
        self.assertEqual((good, bad), (2, 0))

    def test_old_samples_pruned_by_window(self):
        # Hand-crafted deque entries with an old timestamp should be
        # pruned out by ``_recent_stats``.
        item = _make_item()
        now = 1_000_000.0
        item._recent.append((now - _RECENT_WINDOW_S - 1.0, -50, True))  # stale
        item._recent.append((now - _RECENT_WINDOW_S - 0.1, -55, True))  # stale
        item._recent.append((now - 1.0, -65, True))                     # in
        item._recent.append((now - 0.5, -75, False))                    # in

        rssi_avg, good, bad = item._recent_stats(now=now)
        # Only the two in-window samples survive.
        self.assertEqual(len(item._recent), 2)
        self.assertAlmostEqual(rssi_avg, (-65 - 75) / 2)
        self.assertEqual((good, bad), (1, 1))

    def test_drains_to_neutral_after_window_expires(self):
        # After all samples age out, the meters return the empty/silent
        # tuple so the painters fall back to neutral grey.
        item = _make_item()
        now = 1_000_000.0
        item._recent.append((now - _RECENT_WINDOW_S - 1.0, -50, True))
        item._recent.append((now - _RECENT_WINDOW_S - 0.5, -55, False))
        rssi_avg, good, bad = item._recent_stats(now=now)
        self.assertIsNone(rssi_avg)
        self.assertEqual((good, bad), (0, 0))
        self.assertEqual(len(item._recent), 0)


if __name__ == "__main__":
    unittest.main()
