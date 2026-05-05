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
    _DeviceLiveState,
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


class LiveStatePreservationTests(unittest.TestCase):
    """Per-device live state must survive scene.clear() during reload.

    The canvas rebuilds DeviceItems every ~2 s (full scene rebuild
    after reload()). Without a canvas-owned ``_DeviceLiveState``, every
    reload would empty the rolling-window deque, drop the channel-flash
    tails, and reset the live packet delta — flashing all bars to
    neutral and freezing the per-device packet counter for 2 s at a
    time.
    """

    def test_recent_samples_survive_deviceitem_replacement(self):
        # Simulate canvas reload: same live_state, new DeviceItem.
        _ensure_app()
        live = _DeviceLiveState()
        dev = CanvasDevice(
            device_id=42, stable_key="rpa:11:22:33:44:55:66",
            kind="unresolved_rpa", label="t",
        )
        item1 = DeviceItem(dev, persist_cb=lambda *_a, **_k: None,
                           live_state=live)
        item1.notify_channel_hit(channel=37, crc_ok=True, rssi=-65)
        item1.notify_channel_hit(channel=38, crc_ok=True, rssi=-70)

        # New DeviceItem (post-reload) sees the same samples.
        item2 = DeviceItem(dev, persist_cb=lambda *_a, **_k: None,
                           live_state=live)
        rssi_avg, good, bad = item2._recent_stats()
        self.assertAlmostEqual(rssi_avg, -67.5)
        self.assertEqual((good, bad), (2, 0))

    def test_channel_flash_state_survives_replacement(self):
        # The flash tails are aliased into the live_state too — a new
        # DeviceItem inherits any in-flight fades from its predecessor.
        _ensure_app()
        live = _DeviceLiveState()
        dev = CanvasDevice(
            device_id=43, stable_key="rpa:11:22:33:44:55:67",
            kind="unresolved_rpa", label="t",
        )
        item1 = DeviceItem(dev, persist_cb=lambda *_a, **_k: None,
                           live_state=live)
        item1.notify_channel_hit(channel=37, crc_ok=True, rssi=-65)
        item1.notify_channel_hit(channel=12, crc_ok=True, rssi=-70)

        item2 = DeviceItem(dev, persist_cb=lambda *_a, **_k: None,
                           live_state=live)
        # Adv flash (ch 37) preserved, plus the data flash tail entry.
        self.assertIn(37, item2._adv_flash)
        self.assertEqual(len(item2._data_flash_recent), 1)
        self.assertEqual(item2._data_flash_recent[0][1], 12)

    def test_live_packet_delta_ticks_per_attribution(self):
        # The card displays ``device.packet_count + live_packet_delta``
        # so the counter ticks live between reloads instead of being
        # frozen on the last DB total for ~2 s.
        _ensure_app()
        live = _DeviceLiveState()
        dev = CanvasDevice(
            device_id=44, stable_key="rpa:11:22:33:44:55:68",
            kind="unresolved_rpa", label="t", packet_count=1000,
        )
        item = DeviceItem(dev, persist_cb=lambda *_a, **_k: None,
                          live_state=live)
        for _ in range(5):
            item.notify_channel_hit(channel=37, crc_ok=True, rssi=-60)
        self.assertEqual(live.live_packet_delta, 5)
        # Display total = DB count + delta.
        self.assertEqual(dev.packet_count + live.live_packet_delta, 1005)

    def test_in_place_pruning_keeps_alias_live(self):
        # notify_channel_hit slice-assigns the data_flash_recent list
        # rather than rebinding it. If that ever regressed, the alias
        # on _live.data_flash_recent would diverge from the
        # DeviceItem's view and updates would be lost across reload.
        _ensure_app()
        live = _DeviceLiveState()
        dev = CanvasDevice(
            device_id=45, stable_key="rpa:11:22:33:44:55:69",
            kind="unresolved_rpa", label="t",
        )
        item = DeviceItem(dev, persist_cb=lambda *_a, **_k: None,
                          live_state=live)
        # Push more entries than the cap so pruning fires.
        for ch in range(10, 20):
            item.notify_channel_hit(channel=ch, crc_ok=True, rssi=-60)
        # The DeviceItem's alias and the live_state's list are still
        # the same object (no rebind happened).
        self.assertIs(item._data_flash_recent, live.data_flash_recent)
        # Cap is 6 most-recent entries.
        self.assertEqual(len(item._data_flash_recent), 6)


if __name__ == "__main__":
    unittest.main()
