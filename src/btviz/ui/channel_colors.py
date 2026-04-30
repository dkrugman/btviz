"""Per-BLE-channel color scheme for spectrum-activity visualization.

BLE has 40 RF channels: indices 0..36 are data channels (used by
connections, periodic advertising, BIS), and 37/38/39 are the three
primary advertising channels. We want each channel to have a
distinct color so spectrum usage shows up at a glance — e.g. an
Auracast broadcaster + its receivers should appear to flash the
same color sequence in lockstep as they hop.

Design constraints:

  * 40 colors must be visually distinct from each other when
    flashed in close succession.
  * Adjacent channel indices (e.g. 12 vs 13) shouldn't look
    similar — the user reads the channel number from the digit on
    the badge anyway, so shuffling the hue order improves
    readability.
  * The three advertising channels (37/38/39) should be especially
    easy to identify because they're the most-active and the user
    is most often looking for them. Pinned to red / green / blue.
  * Output must be readable on both light and dark canvas
    backgrounds (the box recency-fade goes from full saturation
    down to ~10% opacity), so colors are saturated enough to
    survive heavy alpha compositing.

Implementation:

  * Adv channels 37/38/39 → fixed primaries (red / green / blue).
  * Data channels 0-36 → golden-angle hue distribution. Each
    channel's hue is `i × 137.5077°` mod 360. Golden-angle spacing
    is the standard trick for categorical palettes — every next
    color is maximally distant from the recent few in hue space.
  * Saturation 0.65, value 0.92 — saturated enough to be punchy,
    not so saturated that adjacent hues blur.
"""

from __future__ import annotations

from PySide6.QtGui import QColor

# Golden angle in degrees. The irrational fraction of 360° that
# minimizes visual collisions between consecutive samples.
_GOLDEN_ANGLE_DEG = 137.5077640500378

# Fixed colors for the three primary advertising channels. Chosen for
# memorability + alignment with the convention "red = ch 37" used in
# many BLE tools (the lowest-frequency adv channel at 2402 MHz).
_ADV_CHANNEL_COLORS: dict[int, QColor] = {
    37: QColor(0xE6, 0x39, 0x46),   # red — 2402 MHz
    38: QColor(0x52, 0xB7, 0x88),   # green — 2426 MHz
    39: QColor(0x3F, 0x88, 0xC5),   # blue — 2480 MHz
}


def color_for_channel(channel: int) -> QColor:
    """Return the canonical color for a BLE channel index (0-39).

    Out-of-range or None inputs return mid-grey so caller can paint
    "unknown channel" without special-casing.
    """
    if channel is None or not 0 <= channel <= 39:
        return QColor(160, 160, 165)
    if channel in _ADV_CHANNEL_COLORS:
        return _ADV_CHANNEL_COLORS[channel]
    hue = (channel * _GOLDEN_ANGLE_DEG) % 360.0
    return QColor.fromHsvF(hue / 360.0, 0.65, 0.92)


def text_color_for_channel(channel: int) -> QColor:
    """Return a foreground color (black or white) that's readable
    against ``color_for_channel(channel)``.

    Computed from the channel color's perceptual luminance so badges
    don't render unreadable text on saturated backgrounds.
    """
    bg = color_for_channel(channel)
    # Rec.709 luminance — same formula sRGB browsers use to compute
    # contrast. Values < 0.5 are dark backgrounds → white text;
    # values >= 0.5 are light → black text.
    r, g, b, _ = bg.getRgbF()
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return QColor(0, 0, 0) if luminance >= 0.55 else QColor(255, 255, 255)


def channel_label(channel: int) -> str:
    """Compact label for a channel: "ch 37", "ch 0", or "ch ?" for unknown."""
    if channel is None or not 0 <= channel <= 39:
        return "ch ?"
    return f"ch {channel}"
