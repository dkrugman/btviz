"""Sniffer side-panel for the canvas: activity-dot strip + slide-out detail.

Anchored to the left edge of the canvas viewport (NOT a scene item — the
panel stays put as the user pans/zooms the canvas underneath). Reads from
the ``sniffers`` table; doesn't manage hardware.

Two visual states:
  * **Collapsed (default)** — narrow strip showing one activity dot per
    registered sniffer, sorted by USB Location ID. Always visible.
  * **Expanded** — slides out to a wider panel showing per-sniffer
    name / kind / serial / port. Phase 2 ships the collapsed strip + the
    chevron toggle; Phase 3 fills in the expanded body content.

Activity dot colors:
  * gray     = not currently detected (``is_active=0``)
  * green    = detected, idle (no recent packets)
  * brighter = recent packet from this sniffer (decays over ~600 ms)
  * faint    = ``removed=1`` (user-hidden) — listed but de-emphasized

Removed sniffers are NOT shown by the panel during normal operation;
they reappear automatically when their serial is re-discovered (the
DB layer clears the ``removed`` flag on rediscovery).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QPushButton, QSizePolicy, QWidget

from ..db.models import Sniffer
from ..db.repos import Repos
from ..db.store import Store
from .channel_colors import (
    color_for_channel as _channel_color,
    text_color_for_channel as _channel_text_color,
)

# ──────────────────────────────────────────────────────────────────────────
# Visual constants
# ──────────────────────────────────────────────────────────────────────────

_STRIP_W = 100                 # collapsed-panel width (px); fits one
                               # activity dot + a horizontal row of up
                               # to three channel tags at 10pt bold.
_PANEL_W = 320                 # expanded-panel width (px); widened from
                               # 280 to keep the channel column intact
                               # when the silhouette + text columns
                               # slide into view.
_DOT_SIZE = 12
_ROW_H = 52                    # row pitch (same in both states so dots
                               # don't jump vertically when expanding)
# Refresh button at the top of the panel — moved here from the canvas
# toolbar in 2026-04 because the action's results land in this panel
# (sniffer rows reload), so co-locating control + result removes the
# trip to the toolbar's overflow menu.
_REFRESH_BTN_TOP = 6
_REFRESH_BTN_H = 22
_REFRESH_BTN_BOTTOM_PAD = 8
_TOP_PAD = (
    _REFRESH_BTN_TOP + _REFRESH_BTN_H + _REFRESH_BTN_BOTTOM_PAD
)
_CHEVRON_W = 14
_CHEVRON_H = 28
_DOT_X = 11                    # dot center stays at the original narrow
                               # position so tooltips / hit-tests don't
                               # shift when the strip widened.

# Channel-tag column. Sits right of the dot in both collapsed and
# expanded modes. Renders the sniffer's listening set (1-3 advertising
# channels, or one data channel for idle test mode) horizontally —
# tags are laid out left-to-right, centered in the column. The current
# channel is highlighted in a filled blue pill.
_CH_COL_X = 22
_CH_COL_W = _STRIP_W - _CH_COL_X - 4   # = 74
_CH_TAG_W = 22
_CH_TAG_H = 18
_CH_TAG_GAP = 2
_CH_TAG_BG_IDLE = QColor(225, 225, 232)
_CH_TAG_FG_IDLE = QColor(70, 70, 80)
# Dropout-flash colors when the most recent hit on a tag was CRC-fail.
# Near-black background with red text reads as "received but
# corrupted" — distinct from both the channel-color active state and
# the grey idle state.
_CH_TAG_BG_CRC_FAIL = QColor(20, 20, 28)
_CH_TAG_FG_CRC_FAIL = QColor(230, 70, 70)
_CH_TAG_FONT_PT = 10

# Shape geometry (expanded mode). Dongles are 1:3, DKs ~1:2.15 — actual
# aspect ratios of the hardware so the silhouettes read at a glance.
# _SHAPE_X starts past the channel column (which extends to
# _CH_COL_X + _CH_COL_W = 96) so silhouette + channels don't overlap
# when the panel is expanded.
_SHAPE_X = 110                 # left edge of the silhouette column
_SHAPE_W = 54                  # silhouette width
_DONGLE_H = int(_SHAPE_W / 3.0)        # = 18
_DK_H = int(_SHAPE_W / 2.15)           # = 25

# X-delete button for inactive rows. Reserved on the right edge whether
# the row is active or not, so text width is consistent across rows.
_X_BTN_SIZE = 16
_X_BTN_MARGIN = 6

# Text column starts after the silhouette + small gap, and ends before
# the reserved X-button column on the right.
_TEXT_X = _SHAPE_X + _SHAPE_W + 10
_TEXT_W = _PANEL_W - _TEXT_X - _X_BTN_SIZE - _X_BTN_MARGIN - 6

_X_BTN_BG = QColor(200, 200, 210)
_X_BTN_BG_HOVER = QColor(220, 80, 80)
_X_BTN_FG = QColor(60, 60, 70)
_X_BTN_FG_HOVER = QColor(255, 255, 255)

# Hardware-silhouette palette
_DONGLE_BODY = QColor(245, 245, 240)
_DONGLE_OUTLINE = QColor(60, 60, 70)
_DONGLE_USB = QColor(180, 180, 190)     # USB connector tab
_DONGLE_LED = QColor(220, 60, 60)       # status LED (red when capturing)

_DK_BODY = QColor(40, 40, 60)           # darker PCB-ish body
_DK_OUTLINE = QColor(20, 20, 30)
_DK_HEADER = QColor(180, 160, 60)       # gold-ish pin headers
_DK_SOC = QColor(80, 80, 100)           # SoC chip square

_PANEL_BG = QColor(248, 248, 250)
_STRIP_BG = QColor(232, 232, 238)
_BORDER = QColor(200, 200, 208)

# Activity dot palette — keyed by visual state.
_DOT_DETECTED = QColor(60, 190, 80)          # steady green
_DOT_FLASH = QColor(160, 255, 160)           # brief flash on good packet
_DOT_FLASH_CRC_FAIL = QColor(220, 40, 40)    # dropout flash — red
_DOT_INACTIVE = QColor(155, 155, 160)        # gray
_DOT_REMOVED = QColor(155, 155, 160, 70)     # very faint
_DOT_OUTLINE = QColor(80, 80, 80)

# Activity flash decay: how long the *dot* stays "flashed" after a
# packet. Shorter than the tag fade so each packet's contribution is
# discrete / readable rather than blurring into a long-running bright
# state. At ~150 pkts/sec this still looks "near-solid bright" because
# the inter-packet interval (~6 ms) is smaller than this duration; at
# slower rates individual flashes are visible.
_FLASH_DURATION_S = 0.1

# Channel-tag fade decay. Longer than the dot flash so the colored
# pill behind the channel number reads as "near-solid" and doesn't
# distract from / obscure the digit. The user perceives steady-but-
# subtly-animated activity rather than rapid flicker. When packets
# stop entirely the tag still visibly fades to idle within this
# window so silence is detectable.
_TAG_FADE_DURATION_S = 1

# Probability that a CRC-fail flash also draws a single random
# "noise" pixel inside the dot. Cheap visual cue that the packet
# was corrupted rather than just rendered dark — at 1 in 2 a
# steady stream of dropouts produces a flickering speckle.
_CRC_FAIL_NOISE_PROB = 0.5


# ──────────────────────────────────────────────────────────────────────────
# Panel widget
# ──────────────────────────────────────────────────────────────────────────

class SnifferPanel(QWidget):
    """Left-side panel showing registered sniffers as a vertical strip of
    activity dots, with a chevron to expand into a detail view.

    Designed to live in a QHBoxLayout next to the canvas view — its
    width comes from ``sizeHint()`` (``_STRIP_W`` collapsed, ``_PANEL_W``
    expanded). Toggling expansion calls ``updateGeometry()`` so the
    parent layout re-asks for the new width and the canvas widget
    flexes to fit the remaining space.
    """

    # Emitted when the user toggles expansion. The CanvasWindow can use
    # this to e.g. re-size the scene viewport, save state to DB, etc.
    expansionChanged = Signal(bool)
    # Emitted when the user clicks the panel-top "Refresh" button.
    # Wired to the canvas's ``_refresh_sniffers`` so a click re-runs
    # USB discovery; co-locating the control with the panel that
    # displays the result removes a trip to the toolbar.
    refreshRequested = Signal()

    def __init__(self, parent: QWidget | None = None,
                 store: Store | None = None) -> None:
        super().__init__(parent)
        if store is None:
            raise ValueError("SnifferPanel requires a Store")
        self.store = store
        self.repos = Repos(store)

        self._sniffers: list[Sniffer] = []
        self._expanded = False

        # serial_number -> monotonic time of last packet seen.
        # Used to compute the flash decay; populated by notify_packet().
        self._last_packet_at: dict[str, float] = {}

        # serial_number -> tuple of channel ints the sniffer is currently
        # hopping over. Populated by ``set_sniffer_channels`` from the
        # capture coordinator (reflects the role: Pinned/ScanUnmonitored/
        # Follow). Empty / missing means "we don't know what it's
        # listening to" — channel tags are skipped for that row.
        self._listening_channels: dict[str, tuple[int, ...]] = {}

        # serial_number -> the most recently observed channel for that
        # sniffer (from pkt.channel of the last decoded packet).
        # Used for tooltips / debug; the *visual* highlight uses
        # ``_channel_hit_at`` for a per-channel fade animation.
        self._current_channel: dict[str, int | None] = {}

        # serial_number -> {channel -> monotonic time of last hit}.
        # Each packet on a channel relights that specific tag to bright
        # blue and starts a fade back to idle over
        # ``_TAG_FADE_DURATION_S`` (longer than the dot fade so the
        # number stays readable). Multiple tags in one row can be
        # in-flight simultaneously — a ScanUnmonitored sniffer hopping
        # between channels keeps each visited tag warm if the hop
        # interval is shorter than the fade window.
        self._channel_hit_at: dict[str, dict[int, float]] = {}

        # Parallel structure to _channel_hit_at: True iff the most-
        # recent hit on this (serial, channel) tuple was a CRC-fail.
        # Drives the per-tag dropout rendering — black flash with
        # red text for the duration of that hit's fade window.
        self._channel_hit_was_crc_fail: dict[str, dict[int, bool]] = {}

        # serial_number -> True iff the most-recent flash was a CRC-fail
        # packet. Drives the dropout-style dot rendering for the
        # remainder of the flash decay window.
        self._last_was_crc_fail: dict[str, bool] = {}

        # serial_number -> (good_count, bad_count) since the panel was
        # opened. Surfaced in the row tooltip + the expanded body so
        # the user can see per-sniffer signal quality at a glance.
        self._good_packets: dict[str, int] = {}
        self._bad_packets: dict[str, int] = {}

        # Hovered X-button index (sniffer row), if any. Used to render
        # the destructive button in red on hover.
        self._x_btn_hover_idx: int | None = None

        # Set of serial_numbers (DB column) that fast/USB discovery
        # turned up but the slow extcap probe couldn't reach. Populated
        # by ``set_extcap_unreachable`` after a live-capture start
        # discovery sweep. Surfaced as a warning line in the row
        # tooltip — visually nothing changes today, the dot stays gray
        # because no packets flow.
        self._extcap_unreachable: set[str] = set()

        # Repaint timer — keeps the flash decay smooth without us having
        # to push frames from the bus thread. Runs only while the panel
        # has any active flash; stopped otherwise to keep CPU idle.
        self._anim = QTimer(self)
        self._anim.setInterval(50)  # 20 Hz — plenty for fading dots
        self._anim.timeout.connect(self._tick)

        # Width is owned by sizeHint(); height stretches with the
        # parent layout. Fixed-horizontal so a HBoxLayout doesn't try
        # to negotiate with us when the canvas window resizes.
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        # Refresh button at the top — fires ``refreshRequested`` so
        # the canvas can re-run USB discovery. Geometry is set in
        # ``resizeEvent`` so it tracks the panel's collapsed/expanded
        # width.
        self._refresh_btn = QPushButton("Refresh", self)
        self._refresh_btn.setToolTip(
            "Re-run USB sniffer discovery and refresh the rows below."
        )
        self._refresh_btn.clicked.connect(self.refreshRequested.emit)

        self.refresh()

    # --- public API ---------------------------------------------------

    def refresh(self) -> None:
        """Reload sniffer rows from the DB. Call after a discovery sweep."""
        self._sniffers = self.repos.sniffers.list_all(
            active_only=False, include_removed=False
        )
        self.update()

    def set_sniffer_channels(
        self, serial_number: str, channels: "list[int] | tuple[int, ...]",
    ) -> None:
        """Tell the panel which channels this sniffer is hopping over.

        Driven by the capture coordinator: when a sniffer is started or
        its role changes, push the new listening set here. 1-3 entries
        for adv mode, 1 entry for follow / idle-stub. Pass an empty
        sequence to clear (no channel tags painted for this row).
        """
        cur = self._listening_channels.get(serial_number)
        new = tuple(channels)
        if cur == new:
            return
        self._listening_channels[serial_number] = new
        # Drop the current-channel highlight if it's no longer in the
        # new set; otherwise the wrong tag would stay highlighted until
        # the next packet arrives.
        active = self._current_channel.get(serial_number)
        if active is not None and active not in new:
            self._current_channel[serial_number] = None
        self.update()

    def notify_packet(
        self,
        serial_number: str,
        channel: "int | None" = None,
        crc_ok: bool = True,
    ) -> None:
        """Tick the activity-flash timer for a sniffer.

        Wire this to your live-capture bus — each packet from a known
        sniffer's interface should call this with its serial_number.

        Animations driven by this call:
          * **Activity dot**: flashes brighter for ``_FLASH_DURATION_S``
            then decays back to steady (uses ``_last_packet_at``). When
            ``crc_ok=False`` the flash colors land on the near-black
            dropout palette and the dot renders speckle pixels for
            the remainder of the decay window.
          * **Channel tag** (when ``channel`` is provided): the matching
            tag relights to bright blue and fades back to idle over
            ``_TAG_FADE_DURATION_S``. Multiple tags can be in-flight at
            the same time — a hopping sniffer keeps each visited tag
            warm until its individual fade expires.
          * **Quality counters** (good/bad): incremented per packet so
            the row tooltip can surface a per-sniffer CRC-fail rate.
            CRC-failed packets are not eligible for device
            attribution upstream — ``record_packet`` skips them — but
            they DO contribute to this counter so the user can see
            that the radio is receiving even when packets aren't
            decodable.

        Sub-second update frequency is achieved by piggybacking on the
        existing flash-decay timer's repaint.
        """
        now = time.monotonic()
        self._last_packet_at[serial_number] = now
        self._last_was_crc_fail[serial_number] = not crc_ok
        if channel is not None:
            self._current_channel[serial_number] = channel
            self._channel_hit_at.setdefault(serial_number, {})[channel] = now
            self._channel_hit_was_crc_fail.setdefault(
                serial_number, {},
            )[channel] = not crc_ok
        if crc_ok:
            self._good_packets[serial_number] = (
                self._good_packets.get(serial_number, 0) + 1
            )
        else:
            self._bad_packets[serial_number] = (
                self._bad_packets.get(serial_number, 0) + 1
            )
        if not self._anim.isActive():
            self._anim.start()
        # No update() here — the timer paints; calling update on every
        # packet would saturate the event loop on heavy traffic.

    def set_extcap_unreachable(self, serials: set[str]) -> None:
        """Mark sniffers that USB discovery saw but extcap couldn't probe.

        These rows render the same as inactive ones today (grey dot,
        dim silhouette), but their tooltip carries an extra line
        explaining the mismatch and suggesting recovery. Pass an empty
        set to clear the marking (e.g. when live capture stops).
        """
        if self._extcap_unreachable == serials:
            return
        self._extcap_unreachable = set(serials)
        self.update()

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        if self._expanded == expanded:
            return
        self._expanded = expanded
        # updateGeometry() invalidates the cached sizeHint so the parent
        # layout re-queries us at our new width. The canvas view (the
        # other layout child) then flexes to fill the remaining space —
        # which is the "push canvas content over" behavior, as opposed
        # to the old overlay style that covered it.
        self.updateGeometry()
        self.update()
        self.expansionChanged.emit(expanded)

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    # --- size negotiation --------------------------------------------

    def sizeHint(self) -> QSize:
        return QSize(self._current_width(), 0)

    def minimumSizeHint(self) -> QSize:
        return QSize(self._current_width(), 0)

    # --- internals ----------------------------------------------------

    def _current_width(self) -> int:
        return _PANEL_W if self._expanded else _STRIP_W

    def _tick(self) -> None:
        """Animation tick. Stops itself when no flashes are still decaying."""
        now = time.monotonic()
        # Drop expired dot-flash entries — keeps the dict bounded.
        for sn, t in list(self._last_packet_at.items()):
            if now - t > _FLASH_DURATION_S:
                del self._last_packet_at[sn]
        # Same for per-channel-tag fade entries. Done in two passes so
        # we can also drop a sniffer's whole sub-dict when it goes
        # empty (otherwise a temporarily-active sniffer leaves a
        # permanent {serial: {}} entry).
        for sn, hits in list(self._channel_hit_at.items()):
            for ch, t in list(hits.items()):
                if now - t > _TAG_FADE_DURATION_S:
                    del hits[ch]
                    # Drop the parallel CRC-state entry too.
                    crc_map = self._channel_hit_was_crc_fail.get(sn)
                    if crc_map is not None and ch in crc_map:
                        del crc_map[ch]
            if not hits:
                del self._channel_hit_at[sn]
                self._channel_hit_was_crc_fail.pop(sn, None)
        if not self._last_packet_at and not self._channel_hit_at:
            self._anim.stop()
        self.update()

    def _dot_center_y(self, idx: int) -> int:
        """Vertical center of the i-th activity dot, measured from top."""
        return _TOP_PAD + idx * _ROW_H + _ROW_H // 2

    def _dot_color_for(self, s: Sniffer) -> QColor:
        if s.removed:
            return _DOT_REMOVED
        if not s.is_active:
            return _DOT_INACTIVE
        # Active. Check for in-flight flash.
        flash_t = self._last_packet_at.get(s.serial_number)
        if flash_t is not None:
            age = time.monotonic() - flash_t
            if age < _FLASH_DURATION_S:
                t = age / _FLASH_DURATION_S
                # CRC-failed flash: dropout look — near-black flash that
                # decays back to detected green. The painter also
                # speckle-noise inside the dot for a clearly-different
                # visual signature from a clean flash.
                if self._last_was_crc_fail.get(s.serial_number):
                    return _interp(_DOT_FLASH_CRC_FAIL, _DOT_DETECTED, t)
                return _interp(_DOT_FLASH, _DOT_DETECTED, t)
        return _DOT_DETECTED

    # --- painting -----------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()

        # Background: differentiate strip vs expanded.
        bg = _PANEL_BG if self._expanded else _STRIP_BG
        p.fillRect(rect, bg)

        # Right-edge border separates panel from canvas content.
        p.setPen(QPen(_BORDER, 1))
        p.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())

        # Activity dots — always rendered, in both states.
        self._paint_dots(p)

        # Chevron toggle, vertically centered on the visible area.
        self._paint_chevron(p)

        p.end()

    def _paint_dots(self, p: QPainter) -> None:
        """Paint one row per sniffer. The activity dot column is identical
        between collapsed and expanded modes — no vertical jump on toggle.
        Expanded adds silhouette + text columns to the right.
        """
        import random as _random
        for i, s in enumerate(self._sniffers):
            cy = self._dot_center_y(i)
            # Activity dot pinned to the left so the channel-tag column
            # has a known starting x in both expanded and collapsed states.
            cx = _DOT_X
            color = self._dot_color_for(s)
            p.setPen(QPen(_DOT_OUTLINE, 1))
            p.setBrush(QBrush(color))
            p.drawEllipse(
                cx - _DOT_SIZE // 2,
                cy - _DOT_SIZE // 2,
                _DOT_SIZE,
                _DOT_SIZE,
            )
            # Speckle: when this sniffer's most-recent flash was a CRC
            # failure and the flash is still active, scatter 1-2 light
            # noise pixels inside the dot for a clearly-different
            # "dropout" signature. Stops naturally as the flash decays
            # (this code only runs while _last_was_crc_fail is True
            # AND _last_packet_at is fresh).
            if (
                s.is_active
                and not s.removed
                and self._last_was_crc_fail.get(s.serial_number)
            ):
                t = self._last_packet_at.get(s.serial_number, 0.0)
                age = time.monotonic() - t
                if age < _FLASH_DURATION_S:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QBrush(QColor(220, 220, 220)))
                    for _ in range(2):
                        if _random.random() > _CRC_FAIL_NOISE_PROB:
                            continue
                        ox = _random.randint(-3, 3)
                        oy = _random.randint(-3, 3)
                        p.drawEllipse(cx + ox - 1, cy + oy - 1, 2, 2)
            self._paint_row_channels(p, s, cy)
            if self._expanded:
                self._paint_row_silhouette(p, s, cy)
                self._paint_row_text(p, s, cy)
                # X-delete button only on inactive rows. Active sniffers
                # don't need to be hideable — they're really there.
                if not s.is_active:
                    self._paint_x_button(p, i, cy)

    def _paint_row_channels(self, p: QPainter, s: Sniffer, cy: int) -> None:
        """Paint the channel-tag column for one row.

        Reads ``self._listening_channels[serial]`` (the configured set of
        channels the sniffer is hopping over, 1..3 entries for adv-mode
        or 1 for follow / idle-stub).

        Each tag carries an independent fade animation:
          * Clean packet on channel C → tag lights up in C's canonical
            color (from the channel-colors palette so 37/38/39 are
            red/green/blue and data channels span the spectrum) and
            fades back to idle grey over ``_TAG_FADE_DURATION_S``.
          * CRC-failed packet on channel C → tag flashes near-black
            with red text instead, visually communicating "dropout"
            distinctly from both clean and idle states.

        Multiple tags can be in-flight at once — a hopping sniffer
        keeps each visited tag warm if its hop interval is shorter
        than the fade window. Tags are laid out horizontally and
        centered in the channel column.
        """
        if not s.is_active or s.removed:
            return
        sn = s.serial_number or ""
        channels = self._listening_channels.get(sn) or ()
        if not channels:
            return
        hits = self._channel_hit_at.get(sn, {})
        crc_fails = self._channel_hit_was_crc_fail.get(sn, {})
        now = time.monotonic()

        n = len(channels)
        total_w = n * _CH_TAG_W + (n - 1) * _CH_TAG_GAP
        left = _CH_COL_X + max(0, (_CH_COL_W - total_w) // 2)
        top = cy - _CH_TAG_H // 2

        font = QFont()
        font.setPointSize(_CH_TAG_FONT_PT)
        font.setBold(True)
        p.setFont(font)
        for i, ch in enumerate(channels):
            hit_t = hits.get(ch)
            if hit_t is not None:
                age = now - hit_t
                if age < _TAG_FADE_DURATION_S:
                    t = age / _TAG_FADE_DURATION_S  # 0 = just hit, 1 = fully faded
                    if crc_fails.get(ch):
                        # Dropout flash — black bg + red fg, both fading
                        # back to the idle grey/text colors.
                        bg = _interp(_CH_TAG_BG_CRC_FAIL, _CH_TAG_BG_IDLE, t)
                        fg = _interp(_CH_TAG_FG_CRC_FAIL, _CH_TAG_FG_IDLE, t)
                    else:
                        # Clean flash — channel-color bg, contrasting
                        # text. Color comes from the spectrum palette
                        # so 37/38/39 are red/green/blue and data
                        # channels span hue space.
                        ch_bg = _channel_color(ch)
                        ch_fg = _channel_text_color(ch)
                        bg = _interp(ch_bg, _CH_TAG_BG_IDLE, t)
                        fg = _interp(ch_fg, _CH_TAG_FG_IDLE, t)
                else:
                    bg = _CH_TAG_BG_IDLE
                    fg = _CH_TAG_FG_IDLE
            else:
                bg = _CH_TAG_BG_IDLE
                fg = _CH_TAG_FG_IDLE
            rect = QRectF(
                left + i * (_CH_TAG_W + _CH_TAG_GAP), top,
                _CH_TAG_W, _CH_TAG_H,
            )
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(bg))
            p.drawRoundedRect(rect, 4, 4)
            p.setPen(QPen(fg))
            p.drawText(
                rect,
                Qt.AlignmentFlag.AlignCenter,
                str(ch),
            )

    def _paint_row_silhouette(self, p: QPainter, s: Sniffer, cy: int) -> None:
        """Render a small icon of the actual hardware (1:3 dongle or
        1:2.15 DK aspect). When the sniffer is inactive the silhouette
        is dimmed via a lower alpha."""
        is_dk = (s.kind == "dk")
        sw = _SHAPE_W
        sh = _DK_H if is_dk else _DONGLE_H
        sx = _SHAPE_X
        sy = cy - sh // 2

        # Inactive sniffers paint dim so the column reads at a glance.
        alpha = 255 if s.is_active and not s.removed else 110

        if is_dk:
            self._paint_dk(p, sx, sy, sw, sh, alpha)
        else:
            self._paint_dongle(p, sx, sy, sw, sh, alpha)

    def _paint_dongle(self, p: QPainter, x: int, y: int, w: int, h: int,
                      alpha: int) -> None:
        """nRF52840 dongle silhouette — a thin pill with a USB tab on one
        end and an LED on the other. Stylized, not pixel-perfect."""
        body = QColor(_DONGLE_BODY); body.setAlpha(alpha)
        outline = QColor(_DONGLE_OUTLINE); outline.setAlpha(alpha)
        usb = QColor(_DONGLE_USB); usb.setAlpha(alpha)
        led = QColor(_DONGLE_LED); led.setAlpha(alpha)

        # Body — rounded rectangle
        p.setPen(QPen(outline, 1))
        p.setBrush(QBrush(body))
        p.drawRoundedRect(QRectF(x, y, w, h), 3, 3)
        # USB connector tab on the LEFT end (shorter, wider)
        usb_w = h
        p.setBrush(QBrush(usb))
        p.drawRect(QRectF(x - usb_w // 2, y + 3, usb_w // 2 + 1, h - 6))
        # LED dot on the right end
        p.setBrush(QBrush(led))
        led_d = max(3, h // 4)
        p.drawEllipse(
            int(x + w - led_d - 4), int(y + h // 2 - led_d // 2),
            led_d, led_d,
        )

    def _paint_dk(self, p: QPainter, x: int, y: int, w: int, h: int,
                  alpha: int) -> None:
        """nRF5340 Audio DK silhouette — squarer, dark PCB body with a
        gold pin-header strip across the top and an SoC chip square."""
        body = QColor(_DK_BODY); body.setAlpha(alpha)
        outline = QColor(_DK_OUTLINE); outline.setAlpha(alpha)
        header = QColor(_DK_HEADER); header.setAlpha(alpha)
        soc = QColor(_DK_SOC); soc.setAlpha(alpha)

        # PCB body
        p.setPen(QPen(outline, 1))
        p.setBrush(QBrush(body))
        p.drawRoundedRect(QRectF(x, y, w, h), 2, 2)
        # Gold header strip across the top
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(header))
        p.drawRect(QRectF(x + 4, y + 3, w - 8, 3))
        # SoC chip square in the middle
        p.setBrush(QBrush(soc))
        ch = h // 3
        p.drawRect(QRectF(x + w // 2 - ch, y + h // 2 - ch // 2, ch * 2, ch))

    def _x_button_rect(self, idx: int) -> QRectF | None:
        """Bounding rect of the X-delete button for row idx, or None when
        the panel is collapsed (X button only renders in expanded mode)."""
        if not self._expanded:
            return None
        cy = self._dot_center_y(idx)
        x = self.width() - _X_BTN_SIZE - _X_BTN_MARGIN
        y = cy - _X_BTN_SIZE // 2
        return QRectF(x, y, _X_BTN_SIZE, _X_BTN_SIZE)

    def _paint_x_button(self, p: QPainter, idx: int, cy: int) -> None:
        rect = self._x_button_rect(idx)
        if rect is None:
            return
        hovered = (self._x_btn_hover_idx == idx)
        bg = _X_BTN_BG_HOVER if hovered else _X_BTN_BG
        fg = _X_BTN_FG_HOVER if hovered else _X_BTN_FG
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(bg))
        p.drawEllipse(rect)
        # Draw the X — two short diagonal strokes
        p.setPen(QPen(fg, 1.5))
        m = 4  # margin from circle edge to X stroke
        x0 = rect.left() + m
        y0 = rect.top() + m
        x1 = rect.right() - m
        y1 = rect.bottom() - m
        p.drawLine(int(x0), int(y0), int(x1), int(y1))
        p.drawLine(int(x0), int(y1), int(x1), int(y0))

    def _paint_row_text(self, p: QPainter, s: Sniffer, cy: int) -> None:
        """Three lines of identity text to the right of the silhouette.

        Line 1: bold display name (user_name OR autogen).
        Line 2: kind · last 8 chars of serial.
        Line 3: USB port (truncated). Tooltip shows full path.
        """
        x = _TEXT_X
        # Vertical typography: 3 lines, total ~36px, centered on row.
        line_h = 12
        total_h = line_h * 3
        top = cy - total_h // 2

        # Inactive items render lighter so they read as "not currently here".
        text_color = QColor(40, 40, 50) if s.is_active else QColor(110, 110, 120)
        accent_color = QColor(110, 110, 120) if s.is_active else QColor(140, 140, 150)
        p.setPen(QPen(text_color))

        name = s.name or _autogen_name(s)
        bold = QFont(); bold.setBold(True); bold.setPointSize(9)
        regular = QFont(); regular.setPointSize(8)

        p.setFont(bold)
        p.drawText(QRectF(x, top, _TEXT_W, line_h),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   _truncate(name, 28))

        p.setPen(QPen(accent_color))
        p.setFont(regular)
        kind_str = s.kind or "unknown"
        sn_short = (s.serial_number or "")[-10:]
        p.drawText(QRectF(x, top + line_h, _TEXT_W, line_h),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   _truncate(f"{kind_str} · …{sn_short}", 32))

        port_str = s.usb_port_id or "(no port)"
        # Strip the platform prefix to save room ("/dev/cu.usbmodem" → "")
        for prefix in ("/dev/cu.usbmodem", "/dev/cu.", "/dev/"):
            if port_str.startswith(prefix):
                port_str = port_str[len(prefix):]
                break
        p.drawText(QRectF(x, top + 2 * line_h, _TEXT_W, line_h),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   _truncate(port_str, 32))

    def _paint_chevron(self, p: QPainter) -> None:
        """Acrobat-style chevron tab on the inner edge of the panel.

        Vertically centered on the visible viewport. Points right when
        collapsed (click → expands), points left when expanded (click →
        collapses). The chevron sits ON the right border so it visually
        protrudes from the panel into the canvas area.
        """
        h = self.height()
        cx = self.width() - _CHEVRON_W // 2 - 1
        cy = h // 2
        # Tab background — a small rounded rect that overlaps the border
        # so the chevron looks attached to the panel edge.
        tab = QRectF(
            self.width() - _CHEVRON_W - 1,
            cy - _CHEVRON_H // 2,
            _CHEVRON_W + 6,
            _CHEVRON_H,
        )
        p.setPen(QPen(_BORDER, 1))
        p.setBrush(QBrush(_PANEL_BG if self._expanded else _STRIP_BG))
        p.drawRoundedRect(tab, 4, 4)
        # The triangle. ▶ when collapsed (click expands), ◀ when expanded.
        p.setPen(QPen(QColor(80, 80, 90), 1.5))
        p.setBrush(QBrush(QColor(80, 80, 90)))
        if self._expanded:
            # Pointing left: click collapses
            pts = [
                (cx + 3, cy - 5),
                (cx + 3, cy + 5),
                (cx - 3, cy),
            ]
        else:
            # Pointing right: click expands
            pts = [
                (cx - 3, cy - 5),
                (cx - 3, cy + 5),
                (cx + 3, cy),
            ]
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPolygonF
        poly = QPolygonF([QPointF(x, y) for x, y in pts])
        p.drawPolygon(poly)

    # --- mouse handling ------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        """Keep the Refresh button spanning the panel width.

        The panel width changes between collapsed (``_STRIP_W``) and
        expanded (``_PANEL_W``); we resize the button on every event
        rather than tracking expansion separately so it stays in sync
        even on the initial show.
        """
        super().resizeEvent(event)
        self._refresh_btn.setGeometry(
            4,
            _REFRESH_BTN_TOP,
            self.width() - 8,
            _REFRESH_BTN_H,
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        pos = event.position().toPoint()
        # X-delete button (inactive rows only) takes precedence over
        # chevron / row hit-testing.
        idx = self._x_button_hit(pos)
        if idx is not None:
            self._on_x_button_clicked(idx)
            event.accept()
            return
        if self._chevron_hit(pos):
            self.toggle()
            event.accept()
            return
        super().mousePressEvent(event)

    def _x_button_hit(self, pos) -> int | None:
        """Return the row index whose X button is under ``pos``, or None.

        Active rows have no X button so they're skipped even if pos lies
        inside the reserved column.
        """
        if not self._expanded:
            return None
        for i, s in enumerate(self._sniffers):
            if s.is_active:
                continue
            rect = self._x_button_rect(i)
            if rect is not None and rect.contains(pos):
                return i
        return None

    def _on_x_button_clicked(self, idx: int) -> None:
        s = self._sniffers[idx]
        if s.id is None:
            return
        self.repos.sniffers.soft_delete(s.id)
        # list_all() filters out removed=1 by default, so the row drops
        # off the panel immediately. If the same serial is rediscovered
        # later, record_discovered() un-removes it automatically.
        self.refresh()

    def _chevron_hit(self, pos) -> bool:
        cy = self.height() // 2
        x = pos.x()
        y = pos.y()
        return (
            self.width() - _CHEVRON_W - 4 <= x <= self.width() + 6
            and cy - _CHEVRON_H // 2 <= y <= cy + _CHEVRON_H // 2
        )

    def _row_at(self, pos) -> Sniffer | None:
        """Map a mouse position to a Sniffer row, or None if not on a row.

        Each row occupies the y band ``[_TOP_PAD + idx*_ROW_H,
        _TOP_PAD + (idx+1)*_ROW_H)`` — the same range its dot, silhouette,
        and text are painted in (see ``_dot_center_y``). A simple
        floor-divide gives the row index for any y in that band.

        Why: a previous version added ``_ROW_H // 2`` to the dividend,
        which shifted boundaries by half a row — hovering on the bottom
        half of any row reported the *next* row, so tiny vertical mouse
        jitter flipped the tooltip between adjacent rows (visible most
        clearly when an active sniffer sat just above an inactive one).
        """
        y = pos.y()
        if y < _TOP_PAD:
            return None
        idx = (y - _TOP_PAD) // _ROW_H
        if 0 <= idx < len(self._sniffers):
            return self._sniffers[int(idx)]
        return None

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        """Update tooltip per-row, and X-button hover state, on cursor move."""
        pos = event.position().toPoint()
        # Update X-button hover state — triggers repaint when it changes.
        new_x_hover = self._x_button_hit(pos)
        if new_x_hover != self._x_btn_hover_idx:
            self._x_btn_hover_idx = new_x_hover
            self.update()
            self.setCursor(
                Qt.CursorShape.PointingHandCursor if new_x_hover is not None
                else Qt.CursorShape.ArrowCursor
            )

        # Tooltip swaps as cursor enters / leaves rows.
        s = self._row_at(pos)
        if s is not None:
            unreachable = bool(
                s.serial_number and s.serial_number in self._extcap_unreachable
            )
            good = self._good_packets.get(s.serial_number or "", 0)
            bad = self._bad_packets.get(s.serial_number or "", 0)
            new_tt = _row_tooltip(
                s, extcap_unreachable=unreachable,
                good_packets=good, bad_packets=bad,
            )
            if self.toolTip() != new_tt:
                self.setToolTip(new_tt)
        else:
            if self.toolTip():
                self.setToolTip("")
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        """Clear hover state when the cursor leaves the panel."""
        if self._x_btn_hover_idx is not None:
            self._x_btn_hover_idx = None
            self.update()
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().leaveEvent(event)


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _interp(a: QColor, b: QColor, t: float) -> QColor:
    """Linear-interpolate two colors at fraction t∈[0,1]."""
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red()   + (b.red()   - a.red())   * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue()  + (b.blue()  - a.blue())  * t),
        int(a.alpha() + (b.alpha() - a.alpha()) * t),
    )


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _autogen_name(s: Sniffer) -> str:
    """Default label for a sniffer when the user hasn't named it.

    Format: ``<kind>-<short id>`` so the row is recognizable without
    exposing the full 16-char chip serial.
    """
    kind = (s.kind or "unknown").replace("_", " ")
    return f"{kind}-{_short_id(s.serial_number or '')}"


def _short_id(sn: str) -> str:
    """Compact identifier suffix for a sniffer label.

    For real USB iSerials (Nordic / SEGGER) we just take the last 6
    chars. For path-shaped fallback IDs (Silicon-Labs-bridged sniffers
    that have no iSerial — Adafruit Bluefruit LE etc.) we strip the
    macOS-side ``/dev/cu.…`` framing first so users see a meaningful tail
    like the location-prefix instead of the literal ``-None`` suffix that
    macOS appends.
    """
    if not sn:
        return "?"
    # Strip common macOS device-node prefixes when serial_path is being
    # used as the fallback identifier.
    for prefix in (
        "/dev/cu.usbmodem",
        "/dev/cu.usbserial-",
        "/dev/cu.SLAB_USBtoUART",
        "/dev/cu.",
        "/dev/",
    ):
        if sn.startswith(prefix):
            sn = sn[len(prefix):]
            break
    # Strip trailing macOS interface-index decorations.
    for suffix in ("-None", "-1", "-2", "-3", "-4"):
        if sn.endswith(suffix):
            sn = sn[: -len(suffix)]
            break
    return sn[-6:] if len(sn) >= 6 else sn


def _row_tooltip(
    s: Sniffer,
    *,
    extcap_unreachable: bool = False,
    good_packets: int = 0,
    bad_packets: int = 0,
) -> str:
    """Multi-line tooltip with the full identity for a sniffer row.

    Shown on hover so any truncated value in the rendered row is
    available in full. ``extcap_unreachable`` adds a warning when fast
    USB discovery saw the dongle but the slow extcap probe couldn't
    reach it. ``good_packets`` / ``bad_packets`` add a "Quality:" line
    so the user can see per-sniffer signal quality at a glance.
    """
    lines: list[str] = []
    lines.append(s.name or _autogen_name(s))
    lines.append("─" * 32)
    lines.append(f"Kind:        {s.kind or 'unknown'}")
    lines.append(f"Serial:      {s.serial_number or '(none)'}")
    lines.append(f"USB port:    {s.usb_port_id or '(none)'}")
    lines.append(f"Location ID: {s.location_id_hex or '(none)'}")
    if s.interface_id and s.interface_id != s.usb_port_id:
        lines.append(f"Interface:   {s.interface_id}")
    if s.display:
        lines.append(f"Display:     {s.display}")
    if s.usb_product:
        lines.append(f"USB product: {s.usb_product}")
    total = good_packets + bad_packets
    if total > 0:
        bad_pct = 100.0 * bad_packets / total
        lines.append(
            f"Quality:     {good_packets:,} good · "
            f"{bad_packets:,} CRC-fail ({bad_pct:.1f}%)"
        )
    state = []
    if s.is_active:
        state.append("active")
    else:
        state.append("inactive")
    if s.removed:
        state.append("removed (hidden)")
    lines.append(f"State:       {', '.join(state)}")
    if extcap_unreachable:
        lines.append("─" * 32)
        lines.append(
            "⚠ USB-detected but not responding to extcap probe — "
            "try replug to recover."
        )
    return "\n".join(lines)
