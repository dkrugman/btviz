"""Tests for the channel-color palette.

Validates that the 40-channel scheme produces distinct colors per
channel, the advertising-channel pinning works, and the helper
functions are robust against out-of-range / None inputs (since they
get called from per-packet hot paths and must never raise).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

# This test file requires Qt for QColor. The headless test runner has
# QtCore available but make sure to use the offscreen platform plugin.
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QColor   # noqa: E402

from btviz.ui.channel_colors import (   # noqa: E402
    channel_label,
    color_for_channel,
    text_color_for_channel,
)


class PaletteTests(unittest.TestCase):

    def test_returns_valid_color_for_every_channel_index(self):
        for ch in range(40):
            color = color_for_channel(ch)
            self.assertIsInstance(color, QColor)
            self.assertTrue(color.isValid())

    def test_all_40_channels_produce_distinct_colors(self):
        colors = {color_for_channel(c).name() for c in range(40)}
        # 40 distinct hex strings — golden-angle distribution + 3 fixed
        # adv channels means no collisions.
        self.assertEqual(len(colors), 40)

    def test_adv_channels_pinned_to_red_green_blue(self):
        # Sanity check the documented red/green/blue convention:
        # ch 37 should be reddish, 38 greenish, 39 blueish.
        c37 = color_for_channel(37)
        c38 = color_for_channel(38)
        c39 = color_for_channel(39)
        self.assertGreater(c37.red(), c37.green())
        self.assertGreater(c37.red(), c37.blue())
        self.assertGreater(c38.green(), c38.red())
        self.assertGreater(c38.green(), c38.blue())
        self.assertGreater(c39.blue(), c39.red())
        self.assertGreater(c39.blue(), c39.green())

    def test_out_of_range_returns_grey(self):
        # Hot path — must never raise even on garbage input.
        for bad in (-1, 40, 100, None, 9999):
            color = color_for_channel(bad)
            self.assertIsInstance(color, QColor)
            r, g, b, _ = color.getRgb()
            self.assertAlmostEqual(r, g, delta=10)
            self.assertAlmostEqual(g, b, delta=10)

    def test_text_color_is_readable_against_background(self):
        # For each channel, foreground vs background luminance should
        # differ by enough that text is readable.
        for ch in range(40):
            bg = color_for_channel(ch)
            fg = text_color_for_channel(ch)
            br, bg_g, bb, _ = bg.getRgbF()
            fr, fg_g, fb, _ = fg.getRgbF()
            bg_lum = 0.2126 * br + 0.7152 * bg_g + 0.0722 * bb
            fg_lum = 0.2126 * fr + 0.7152 * fg_g + 0.0722 * fb
            self.assertGreater(abs(fg_lum - bg_lum), 0.4)

    def test_label_format(self):
        self.assertEqual(channel_label(37), "ch 37")
        self.assertEqual(channel_label(0), "ch 0")
        self.assertEqual(channel_label(None), "ch ?")
        self.assertEqual(channel_label(99), "ch ?")


if __name__ == "__main__":
    unittest.main()
