"""Bottom-of-canvas channel-spectrum panel.

Shares the visual language of the SnifferPanel (chevron toggle, idle vs
active vs CRC-fail palette) but slices activity by *channel* rather
than *sniffer*: 40 boxes — one per BLE channel 0-39 — that double as

  * **Reference key.** Each box is permanently labelled with its
    channel number against the channel's canonical color, so the user
    can always look at the strip to remember which channel is which
    color elsewhere in the UI. Idle boxes render at 50 % opacity so
    they recede into the background until activity arrives.

  * **Live activity indicator (collapsed).** Each packet seen on a
    channel pulses that box to full opacity for ``_FLASH_DURATION_S``
    then decays. CRC-fail packets paint the box black with a red
    glyph for the same window (matches sniffer-panel + canvas-box
    dropout treatment).

  * **Spectrum histogram (expanded).** Same boxes grow into a
    vintage-graphic-EQ style bar display where bar height = packet
    count over the trailing ``_HIST_WINDOW_S`` seconds. Each bar
    carries a peak-hold dot that lingers above the falling bar for
    ``_PEAK_HOLD_S`` and then drops at ``_PEAK_FALL_PER_S``.

The strip scales horizontally with the canvas window: 40 boxes fit
between ``_MIN_TOTAL_W`` and ``_MAX_TOTAL_W`` so a small window stays
legible and a wide one doesn't stretch into uselessly-wide bars.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QMenu, QSizePolicy, QToolTip, QWidget

from .channel_colors import (
    color_for_channel as _channel_color,
    text_color_for_channel as _channel_text_color,
)

# BLE physical-channel index → center frequency (MHz). Adv channels
# 37/38/39 are interleaved with the data channels in the 2.4 GHz
# band: 37 sits below ch 0, 38 between ch 10 and 11, 39 above ch 36.
# Used by the "sort by frequency" view and the per-box tooltip.
_CHANNEL_MHZ: dict[int, int] = {
    37: 2402,
    **{c: 2404 + 2 * c for c in range(0, 11)},   # ch 0..10  → 2404..2424
    38: 2426,
    **{c: 2428 + 2 * (c - 11) for c in range(11, 37)},  # ch 11..36 → 2428..2478
    39: 2480,
}
# Channels listed in ascending physical-frequency order — the alternative
# display ordering exposed via the right-click sort menu.
_CHANNELS_BY_FREQ: list[int] = sorted(
    _CHANNEL_MHZ, key=lambda c: _CHANNEL_MHZ[c],
)

# ──────────────────────────────────────────────────────────────────────────
# Visual constants
# ──────────────────────────────────────────────────────────────────────────

_NUM_CHANNELS = 40

# Width budget. The strip wants at least enough horizontal space for
# 40 boxes at a comfortable text size; if the parent gives us more we
# stretch up to ``_MAX_TOTAL_W``. The boxes don't disappear at small
# sizes because they double as the channel-color reference — we
# always paint all 40.
_MIN_BOX_W = 22                   # tightest comfortable for "37" at 9 pt
_MAX_BOX_W = 90                   # past this the strip stops growing
_MIN_TOTAL_W = _MIN_BOX_W * _NUM_CHANNELS    # = 880
_MAX_TOTAL_W = _MAX_BOX_W * _NUM_CHANNELS    # = 3600
_BOX_GAP = 1
_OUTER_PAD = 4

# Vertical layout. Collapsed mode shows just the row of channel boxes;
# expanded mode adds a histogram canvas above the row, separated by a
# thin divider.
_BOX_H_COLLAPSED = 22
_HIST_H = 120
_DIVIDER_H = 1
_PANEL_H_COLLAPSED = _BOX_H_COLLAPSED + 2 * _OUTER_PAD
_PANEL_H_EXPANDED = (
    _HIST_H + _DIVIDER_H + _BOX_H_COLLAPSED + 2 * _OUTER_PAD
)

# Idle vs active alpha. 50 % was the user's request; keep boxes always
# visible so the strip doubles as a colour key.
_IDLE_ALPHA = 0.50
_ACTIVE_ALPHA = 1.0

# Flash decay window — matches the sniffer-panel dot.
_FLASH_DURATION_S = 0.18

# Dropout (CRC-fail) palette — same as sniffer panel + canvas device boxes.
_FLASH_DROPOUT_BG = QColor(20, 20, 28)
_FLASH_DROPOUT_FG = QColor(230, 70, 70)

# Histogram. Bar height is the rolling packet count in the most recent
# ``_HIST_WINDOW_S`` seconds. The widget repaints at 20 Hz so the bars
# read as continuous animation. A circular buffer of per-bin counts
# keeps the math O(1) per packet — full per-packet timestamp lists
# would balloon under heavy traffic.
_HIST_WINDOW_S = 1.5
_HIST_BIN_S = 0.05
_HIST_BINS = int(_HIST_WINDOW_S / _HIST_BIN_S)   # = 30

# Peak-hold "falling dot" — the line that sits at the prior peak for
# a moment before falling, evoking a vintage graphic-EQ display. Hold
# for ``_PEAK_HOLD_S`` after a new max, then fall at the configured
# rate (units = packets/second of bar-height).
_PEAK_HOLD_S = 0.5
_PEAK_FALL_PER_S = 25.0

# Background palette — track sniffer panel's `_PANEL_BG` / `_BORDER`
# values so the two strips read as one family.
_PANEL_BG = QColor(248, 248, 250)
_BORDER = QColor(200, 200, 208)
_HIST_BG = QColor(245, 245, 248)
_HIST_GRID = QColor(220, 220, 226)
_PEAK_DOT = QColor(40, 40, 50)

# Chevron toggle — small triangle in the top-right corner so a click
# anywhere on the strip can flip expanded state without colliding with
# the box hit-area.
_CHEVRON_W = 12
_CHEVRON_H = 12
_CHEVRON_MARGIN = 4


class ChannelStrip(QWidget):
    """40-box channel spectrum strip; lives below the canvas view.

    Collapsed: a single row of 40 channel boxes. Expanded: a histogram
    panel rendered above the same row.
    """

    expansionChanged = Signal(bool)

    SORT_BY_CHANNEL = "channel"
    SORT_BY_FREQUENCY = "frequency"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._expanded = False
        # Display order. ``"channel"`` paints boxes 0..39 left-to-right
        # (index order — easy to find a known channel); ``"frequency"``
        # paints them in physical-spectrum order, so 37 sits at the
        # left edge between the band's lower guard and ch 0, 38 in the
        # middle, 39 at the right edge. Toggled via the right-click
        # menu on the strip.
        self._sort_mode: str = self.SORT_BY_CHANNEL
        # Per-channel "last hit" timestamp + CRC-fail flag for the box
        # flash. Mirror of the per-(serial, channel) dict in the
        # sniffer panel — keyed by channel index here since this strip
        # aggregates across all sniffers.
        self._hit_at: dict[int, float] = {}
        self._hit_was_crc_fail: dict[int, bool] = {}
        # Circular bin buffer for the histogram. Each entry counts the
        # number of packets that landed in a 50 ms window. ``_bin_index``
        # is the index of the *currently filling* bin; when wall clock
        # crosses a bin boundary we advance the index and zero the new
        # bin. Bar height for channel c = sum of all bins for c.
        self._bins: list[list[int]] = [
            [0] * _HIST_BINS for _ in range(_NUM_CHANNELS)
        ]
        self._bin_index = 0
        self._bin_started_at = time.monotonic()
        # Peak-hold state: per-channel current peak height + the wall
        # time it was set. Decays toward the live bar height once
        # ``_PEAK_HOLD_S`` has elapsed since the last new peak.
        self._peak_height: list[float] = [0.0] * _NUM_CHANNELS
        self._peak_at: list[float] = [0.0] * _NUM_CHANNELS

        self._anim = QTimer(self)
        self._anim.setInterval(50)  # 20 Hz, same cadence as sniffer panel
        self._anim.timeout.connect(self._tick)
        # Always running while there's anything to fade; we start it
        # lazily on the first packet so an idle window stays at 0 Hz.

        self.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed,
        )
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # ---------------------------------------------------------------- API

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        if self._expanded == expanded:
            return
        self._expanded = expanded
        self.updateGeometry()
        self.update()
        self.expansionChanged.emit(expanded)

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def notify_packet(self, channel: int | None, crc_ok: bool = True) -> None:
        """Record one packet on ``channel`` (0-39).

        Called by the canvas's per-source LiveIngest hook for every
        decoded packet. ``channel=None`` (decoder couldn't determine)
        is ignored — there's no box to attribute it to.
        """
        if channel is None or not 0 <= channel < _NUM_CHANNELS:
            return
        now = time.monotonic()
        self._hit_at[channel] = now
        self._hit_was_crc_fail[channel] = not crc_ok
        # Advance bin counter if we crossed a boundary, then increment.
        self._roll_bins(now)
        self._bins[channel][self._bin_index] += 1
        if not self._anim.isActive():
            self._anim.start()

    # ------------------------------------------------------------- sizing

    def sizeHint(self) -> QSize:
        # Width is "preferred" — the layout will stretch us inside
        # [_MIN_TOTAL_W, _MAX_TOTAL_W]. We report a comfortable mid
        # value so initial layout doesn't pin us at the minimum.
        return QSize(
            min(_MAX_TOTAL_W, max(_MIN_TOTAL_W, 1400)),
            self._panel_height(),
        )

    def minimumSizeHint(self) -> QSize:
        return QSize(_MIN_TOTAL_W, self._panel_height())

    def _panel_height(self) -> int:
        return (
            _PANEL_H_EXPANDED if self._expanded else _PANEL_H_COLLAPSED
        )

    def _channel_order(self) -> list[int]:
        """Channels listed in current display (left-to-right) order."""
        if self._sort_mode == self.SORT_BY_FREQUENCY:
            return _CHANNELS_BY_FREQ
        return list(range(_NUM_CHANNELS))

    def _channel_at_pos(self, pos) -> int | None:
        """Return the channel under widget-coords ``pos``, or None.

        Used by the hover tooltip + right-click menu to identify which
        box the cursor is over. Walks the same metrics the paint loop
        uses so it stays in sync if the strip resizes.
        """
        box_w, total_w = self._box_metrics()
        strip_left = (self.width() - total_w) // 2
        x = pos.x() - strip_left
        if x < 0 or x >= total_w:
            return None
        pitch = box_w + _BOX_GAP
        idx = int(x // pitch)
        if not 0 <= idx < _NUM_CHANNELS:
            return None
        return self._channel_order()[idx]

    # --------------------------------------------------------- internals

    def _roll_bins(self, now: float) -> None:
        """Advance ``_bin_index`` if wall-clock has crossed a bin edge.

        Called from both ``notify_packet`` (writer) and ``_tick`` (reader)
        so the bin window stays correct even when the strip is
        completely idle. Each step zeroes the new bin so old data
        ages out automatically.
        """
        elapsed = now - self._bin_started_at
        steps = int(elapsed / _HIST_BIN_S)
        if steps <= 0:
            return
        steps = min(steps, _HIST_BINS)  # cap so a long idle period
                                        # zeroes the whole window once
        for _ in range(steps):
            self._bin_index = (self._bin_index + 1) % _HIST_BINS
            for c in range(_NUM_CHANNELS):
                self._bins[c][self._bin_index] = 0
        self._bin_started_at += steps * _HIST_BIN_S

    def _tick(self) -> None:
        """20 Hz repaint pump. Stops when nothing's left to animate."""
        now = time.monotonic()
        self._roll_bins(now)

        # Drop expired flash-state entries so the dicts don't grow.
        for ch in list(self._hit_at):
            if now - self._hit_at[ch] > _FLASH_DURATION_S:
                del self._hit_at[ch]
                self._hit_was_crc_fail.pop(ch, None)

        # Decay peak-hold dots. Once hold expires the dot falls toward
        # the live bar value; when they meet we leave the peak pinned
        # to the current bar so a small uptick still triggers a fresh
        # "rise then hold" cycle.
        for c in range(_NUM_CHANNELS):
            live = float(sum(self._bins[c]))
            if live > self._peak_height[c]:
                self._peak_height[c] = live
                self._peak_at[c] = now
            elif now - self._peak_at[c] > _PEAK_HOLD_S:
                drop = _PEAK_FALL_PER_S * (self._anim.interval() / 1000.0)
                self._peak_height[c] = max(live, self._peak_height[c] - drop)

        any_flash = bool(self._hit_at)
        any_bars = self._expanded and any(
            self._peak_height[c] > 0 or sum(self._bins[c]) > 0
            for c in range(_NUM_CHANNELS)
        )
        if any_flash or any_bars:
            self.update()
        else:
            self._anim.stop()

    # ----------------------------------------------------------- painting

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            r = self.rect()
            p.fillRect(r, _PANEL_BG)
            p.setPen(QPen(_BORDER, 1))
            p.drawLine(r.left(), r.top(), r.right(), r.top())

            box_w, box_total_w = self._box_metrics()
            strip_left = (r.width() - box_total_w) // 2

            if self._expanded:
                hist_top = _OUTER_PAD
                hist_rect = QRectF(
                    strip_left, hist_top, box_total_w, _HIST_H,
                )
                self._paint_histogram(p, hist_rect, box_w)
                row_top = (
                    hist_top + _HIST_H + _DIVIDER_H
                )
                # Divider line under the histogram so the strip below
                # reads as a separate row.
                p.setPen(QPen(_BORDER, 1))
                p.drawLine(
                    strip_left,
                    int(hist_top + _HIST_H),
                    strip_left + box_total_w,
                    int(hist_top + _HIST_H),
                )
            else:
                row_top = _OUTER_PAD

            self._paint_channel_row(p, strip_left, row_top, box_w)
            self._paint_chevron(p)
        finally:
            p.end()

    def _box_metrics(self) -> tuple[int, int]:
        """Return ``(box_w, total_w)`` for the current widget width."""
        avail = max(0, self.width() - 2 * _OUTER_PAD)
        # Aim for as wide as fits, clamped to [_MIN_BOX_W, _MAX_BOX_W].
        per_box_with_gap = max(
            _MIN_BOX_W + _BOX_GAP,
            min(
                _MAX_BOX_W + _BOX_GAP,
                avail // _NUM_CHANNELS,
            ),
        )
        box_w = max(_MIN_BOX_W, per_box_with_gap - _BOX_GAP)
        total = _NUM_CHANNELS * box_w + (_NUM_CHANNELS - 1) * _BOX_GAP
        return box_w, total

    def _paint_channel_row(
        self, p: QPainter, left: int, top: int, box_w: int,
    ) -> None:
        """Render the row of 40 channel boxes — idle key / live flash."""
        now = time.monotonic()
        font = QFont()
        font.setBold(True)
        font.setPointSize(8 if box_w < 28 else 9)
        p.setFont(font)
        for slot, c in enumerate(self._channel_order()):
            x = left + slot * (box_w + _BOX_GAP)
            rect = QRectF(x, top, box_w, _BOX_H_COLLAPSED)
            entry_t = self._hit_at.get(c)
            crc_fail = (
                self._hit_was_crc_fail.get(c, False) if entry_t else False
            )
            if entry_t is not None:
                age = max(0.0, now - entry_t)
                t = max(0.0, 1.0 - age / _FLASH_DURATION_S)
                alpha = _IDLE_ALPHA + (_ACTIVE_ALPHA - _IDLE_ALPHA) * t
            else:
                alpha = _IDLE_ALPHA
            if crc_fail:
                bg = QColor(_FLASH_DROPOUT_BG)
                fg = QColor(_FLASH_DROPOUT_FG)
            else:
                bg = QColor(_channel_color(c))
                fg = QColor(_channel_text_color(c))
            bg.setAlphaF(alpha)
            fg.setAlphaF(min(1.0, alpha + 0.2))  # text stays a bit
                                                  # crisper than the
                                                  # fill so the digit
                                                  # remains readable
                                                  # when the box is faded.
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(bg))
            p.drawRoundedRect(rect, 3, 3)
            p.setPen(QPen(fg))
            p.drawText(
                rect, Qt.AlignmentFlag.AlignCenter, str(c),
            )

    def _paint_histogram(
        self, p: QPainter, area: QRectF, box_w: int,
    ) -> None:
        """Render per-channel bars + peak-hold dots inside ``area``."""
        p.fillRect(area, _HIST_BG)
        # Soft baseline + half-line grid for visual reference.
        p.setPen(QPen(_HIST_GRID, 1))
        p.drawLine(
            area.left(), area.bottom(),
            area.right(), area.bottom(),
        )
        p.drawLine(
            area.left(), area.center().y(),
            area.right(), area.center().y(),
        )

        # Auto-scale: tallest current-or-peak bar maps to full height.
        # Floor at 4 so a single hit doesn't fill the panel; ceiling
        # by clamping when the live count exceeds the floor.
        scale_max = 4.0
        for c in range(_NUM_CHANNELS):
            live = float(sum(self._bins[c]))
            scale_max = max(scale_max, live, self._peak_height[c])

        h = area.height()
        for slot, c in enumerate(self._channel_order()):
            x = area.left() + slot * (box_w + _BOX_GAP)
            live = float(sum(self._bins[c]))
            bar_h = (live / scale_max) * h if scale_max > 0 else 0.0
            bar_rect = QRectF(
                x, area.bottom() - bar_h, box_w, bar_h,
            )
            color = QColor(_channel_color(c))
            color.setAlphaF(0.85)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            p.drawRect(bar_rect)

            # Peak dot: a thin horizontal line at the held-peak height.
            peak = self._peak_height[c]
            if peak > 0.5:
                peak_h = (peak / scale_max) * h if scale_max > 0 else 0.0
                py = area.bottom() - peak_h
                p.setPen(QPen(_PEAK_DOT, 2))
                p.drawLine(x, py, x + box_w, py)

    def _paint_chevron(self, p: QPainter) -> None:
        """Tiny triangle hint in the top-right corner. Click anywhere
        on the strip toggles expansion; the chevron is purely visual.
        """
        r = self.rect()
        cx = r.right() - _CHEVRON_MARGIN - _CHEVRON_W // 2
        cy = r.top() + _CHEVRON_MARGIN + _CHEVRON_H // 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(_BORDER))
        if self._expanded:
            # Down chevron — clicking collapses.
            pts = [
                (cx - _CHEVRON_W // 2, cy - _CHEVRON_H // 4),
                (cx + _CHEVRON_W // 2, cy - _CHEVRON_H // 4),
                (cx, cy + _CHEVRON_H // 4),
            ]
        else:
            # Up chevron — clicking expands.
            pts = [
                (cx - _CHEVRON_W // 2, cy + _CHEVRON_H // 4),
                (cx + _CHEVRON_W // 2, cy + _CHEVRON_H // 4),
                (cx, cy - _CHEVRON_H // 4),
            ]
        poly = QPolygonF([QPointF(x, y) for x, y in pts])
        p.drawPolygon(poly)

    # -------------------------------------------------------- interaction

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # Left-click toggles expansion. Right-click is left for
        # ``contextMenuEvent`` to handle so it doesn't also flip the
        # panel out from under the menu.
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle()
            event.accept()
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        """Right-click → sort-mode menu.

        Tiny menu (two items) so the user can flip between
        index-ordered (default) and frequency-ordered display. The
        right-click is the only space on the strip — there's no room
        for a toolbar control given the 40 boxes already eat the
        full width budget.
        """
        menu = QMenu(self)
        a_chan = QAction("Sort by channel #", self)
        a_chan.setCheckable(True)
        a_chan.setChecked(self._sort_mode == self.SORT_BY_CHANNEL)
        a_chan.triggered.connect(
            lambda: self._set_sort_mode(self.SORT_BY_CHANNEL),
        )
        a_freq = QAction("Sort by frequency", self)
        a_freq.setCheckable(True)
        a_freq.setChecked(self._sort_mode == self.SORT_BY_FREQUENCY)
        a_freq.triggered.connect(
            lambda: self._set_sort_mode(self.SORT_BY_FREQUENCY),
        )
        menu.addAction(a_chan)
        menu.addAction(a_freq)
        menu.exec(event.globalPos())
        event.accept()

    def _set_sort_mode(self, mode: str) -> None:
        if self._sort_mode == mode:
            return
        self._sort_mode = mode
        self.update()

    def event(self, ev) -> bool:  # noqa: N802 (Qt naming)
        """Per-box tooltips fire on hover via QEvent.ToolTip.

        Custom-painted widgets don't get setToolTip-per-region for
        free — we override the dispatch and translate cursor pos to
        a channel box. Outside any box the tooltip stays hidden.
        """
        if ev.type() == QEvent.Type.ToolTip:
            ch = self._channel_at_pos(ev.pos())
            if ch is None:
                QToolTip.hideText()
                ev.ignore()
                return True
            mhz = _CHANNEL_MHZ.get(ch)
            kind = "adv" if ch >= 37 else "data"
            QToolTip.showText(
                ev.globalPos(),
                f"Channel {ch} ({kind}) — {mhz} MHz",
                self,
            )
            return True
        return super().event(ev)
