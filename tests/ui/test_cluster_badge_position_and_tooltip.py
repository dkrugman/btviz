"""Cluster badge: lower-left placement + per-region tooltip.

Two coupled behaviour changes pinned here:

  * ``_cluster_badge_rect`` returns a rect anchored to the
    bottom-left corner of the device box (no longer the top-left),
    so the device icon and title text in the header sit at their
    natural left position even when the device is a cluster primary.
    The expanded-mode bounding rect is taller, so the badge tracks
    that height too.

  * ``_build_tooltip`` omits the cluster section + address list when
    the device is a cluster primary, and ``_build_cluster_tooltip``
    carries that content instead. ``hoverMoveEvent`` swaps between
    them based on whether the cursor is over the badge rect.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPointF  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsSceneHoverEvent  # noqa: E402

from btviz.ui.canvas import (  # noqa: E402
    CanvasDevice,
    DeviceItem,
    _BOX_H_COLLAPSED,
    _CLUSTER_BADGE_H,
    _CLUSTER_BADGE_MARGIN,
    _build_cluster_tooltip,
    _build_tooltip,
)


_app: QApplication | None = None


def _ensure_app() -> QApplication:
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication([])
    return _app


def _cluster_primary(member_count: int = 5,
                     addresses: list[tuple[str, str]] | None = None) -> CanvasDevice:
    addresses = addresses or [("aa:bb:cc:dd:ee:ff", "rpa")]
    return CanvasDevice(
        device_id=1, stable_key="rpa:a", kind="unresolved_rpa",
        label="primary",
        device_class="apple_device",
        cluster_id=42,
        cluster_member_ids=list(range(2, 2 + member_count - 1)),
        cluster_min_score=0.95,
        addresses=addresses,
    )


def _solo_device(addresses: list[tuple[str, str]] | None = None) -> CanvasDevice:
    addresses = addresses or [("aa:bb:cc:dd:ee:ff", "rpa")]
    return CanvasDevice(
        device_id=10, stable_key="rpa:b", kind="unresolved_rpa",
        label="solo",
        device_class="hearing_aid",
        addresses=addresses,
    )


class BadgePositionTests(unittest.TestCase):

    def setUp(self) -> None:
        _ensure_app()

    def test_badge_anchored_to_lower_left_of_collapsed_box(self):
        item = DeviceItem(_cluster_primary(), persist_cb=lambda *_a, **_k: None)
        rect = item._cluster_badge_rect()
        # Left margin from the box's left edge.
        self.assertAlmostEqual(rect.x(), _CLUSTER_BADGE_MARGIN)
        # Bottom margin from the box's bottom edge.
        expected_y = _BOX_H_COLLAPSED - _CLUSTER_BADGE_H - _CLUSTER_BADGE_MARGIN
        self.assertAlmostEqual(rect.y(), expected_y)
        self.assertAlmostEqual(rect.height(), _CLUSTER_BADGE_H)

    def test_badge_tracks_box_height_when_expanded(self):
        d = _cluster_primary()
        d.collapsed = False
        item = DeviceItem(d, persist_cb=lambda *_a, **_k: None)
        rect = item._cluster_badge_rect()
        # Expanded boxes are taller; the badge slides down with them.
        # Just assert it sits flush with the (now larger) bounding rect.
        expected_y = (
            item.boundingRect().height()
            - _CLUSTER_BADGE_H
            - _CLUSTER_BADGE_MARGIN
        )
        self.assertAlmostEqual(rect.y(), expected_y)

    def test_badge_rect_empty_when_not_a_cluster_primary(self):
        item = DeviceItem(_solo_device(), persist_cb=lambda *_a, **_k: None)
        self.assertTrue(item._cluster_badge_rect().isEmpty())

    def test_badge_width_grows_with_member_count_digits(self):
        small = DeviceItem(
            _cluster_primary(member_count=2), persist_cb=lambda *_a, **_k: None,
        )
        big = DeviceItem(
            _cluster_primary(member_count=213), persist_cb=lambda *_a, **_k: None,
        )
        self.assertGreater(
            big._cluster_badge_rect().width(),
            small._cluster_badge_rect().width(),
        )


class TooltipSplitTests(unittest.TestCase):

    def test_main_tooltip_for_cluster_primary_omits_cluster_and_addresses(self):
        d = _cluster_primary(
            member_count=4,
            addresses=[(f"aa:bb:cc:00:00:{i:02x}", "rpa") for i in range(4)],
        )
        tip = _build_tooltip(d)
        # Identity remains.
        self.assertIn("Device ID:     1", tip)
        # Cluster identity has moved to the badge tooltip.
        self.assertNotIn("Cluster:", tip)
        self.assertNotIn("absorbed:", tip)
        # The address list has moved too.
        self.assertNotIn("Addresses (", tip)

    def test_cluster_tooltip_carries_cluster_and_addresses(self):
        d = _cluster_primary(
            member_count=3,
            addresses=[
                ("aa:bb:cc:00:00:01", "rpa"),
                ("aa:bb:cc:00:00:02", "rpa"),
                ("aa:bb:cc:00:00:03", "rpa"),
            ],
        )
        tip = _build_cluster_tooltip(d)
        self.assertIn("Cluster:       42 · 3 members", tip)
        self.assertIn("min score 0.95", tip)
        self.assertIn("Absorbed IDs:", tip)
        self.assertIn("Addresses (3):", tip)
        self.assertIn("aa:bb:cc:00:00:02", tip)

    def test_cluster_tooltip_empty_for_non_cluster(self):
        d = _solo_device()
        self.assertEqual(_build_cluster_tooltip(d), "")

    def test_solo_device_main_tooltip_keeps_addresses(self):
        addrs = [("aa:bb:cc:00:00:01", "rpa")]
        d = _solo_device(addresses=addrs)
        tip = _build_tooltip(d)
        # No badge for solo devices, so addresses must still appear in
        # the main tooltip — there's nowhere else to put them.
        self.assertIn("Addresses (1):", tip)
        self.assertIn("aa:bb:cc:00:00:01", tip)


class HoverSwapTests(unittest.TestCase):

    def setUp(self) -> None:
        _ensure_app()

    def _make_hover_event(self, item: DeviceItem, x: float, y: float):
        ev = QGraphicsSceneHoverEvent(QGraphicsSceneHoverEvent.GraphicsSceneHoverMove)
        ev.setPos(QPointF(x, y))
        return ev

    def test_hover_over_badge_swaps_to_cluster_tooltip(self):
        item = DeviceItem(_cluster_primary(), persist_cb=lambda *_a, **_k: None)
        rect = item._cluster_badge_rect()
        # Move into the centre of the badge rect.
        center = rect.center()
        item.hoverMoveEvent(self._make_hover_event(item, center.x(), center.y()))
        self.assertEqual(item.toolTip(), item._cluster_tooltip)

    def test_hover_off_badge_swaps_back_to_main_tooltip(self):
        item = DeviceItem(_cluster_primary(), persist_cb=lambda *_a, **_k: None)
        # First force the cluster tooltip in.
        rect = item._cluster_badge_rect()
        item.hoverMoveEvent(
            self._make_hover_event(item, rect.center().x(), rect.center().y()),
        )
        # Now move into the header (top of the box, definitely outside
        # the badge rect).
        item.hoverMoveEvent(self._make_hover_event(item, 80.0, 10.0))
        self.assertEqual(item.toolTip(), item._main_tooltip)

    def test_solo_device_never_switches_to_cluster_tooltip(self):
        item = DeviceItem(_solo_device(), persist_cb=lambda *_a, **_k: None)
        # The badge rect is empty for a solo device, so even hovering
        # the lower-left corner should leave the main tooltip in place.
        item.hoverMoveEvent(self._make_hover_event(item, 4.0, 100.0))
        self.assertEqual(item.toolTip(), item._main_tooltip)
        # And the cached cluster tooltip is empty.
        self.assertEqual(item._cluster_tooltip, "")


if __name__ == "__main__":
    unittest.main()
