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

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..db.models import Sniffer
from ..db.repos import Repos
from ..db.store import Store

# ──────────────────────────────────────────────────────────────────────────
# Visual constants
# ──────────────────────────────────────────────────────────────────────────

_STRIP_W = 22                  # collapsed-panel width (px)
_PANEL_W = 280                 # expanded-panel width (px)
_DOT_SIZE = 12
_ROW_H = 52                    # row pitch (same in both states so dots
                               # don't jump vertically when expanding)
_TOP_PAD = 14
_CHEVRON_W = 14
_CHEVRON_H = 28

# Shape geometry (expanded mode). Dongles are 1:3, DKs ~1:2.15 — actual
# aspect ratios of the hardware so the silhouettes read at a glance.
_SHAPE_X = 26                  # left edge of the silhouette column
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
_DOT_FLASH = QColor(160, 255, 160)           # brief flash on packet
_DOT_INACTIVE = QColor(155, 155, 160)        # gray
_DOT_REMOVED = QColor(155, 155, 160, 70)     # very faint
_DOT_OUTLINE = QColor(80, 80, 80)

# Activity flash decay: how long a dot stays "flashed" after a packet.
_FLASH_DURATION_S = 0.6


# ──────────────────────────────────────────────────────────────────────────
# Panel widget
# ──────────────────────────────────────────────────────────────────────────

class SnifferPanel(QWidget):
    """Left-edge overlay on the canvas. Shows registered sniffers as a
    vertical strip of activity dots, with a chevron to expand into a
    detail view (Phase 3).

    Owners must call ``reposition(viewport_rect)`` on canvas resize so
    the panel hugs the left edge at the right height.
    """

    # Emitted when the user toggles expansion. The CanvasWindow can use
    # this to e.g. re-size the scene viewport, save state to DB, etc.
    expansionChanged = Signal(bool)

    def __init__(self, parent: QWidget, store: Store) -> None:
        super().__init__(parent)
        self.store = store
        self.repos = Repos(store)

        self._sniffers: list[Sniffer] = []
        self._expanded = False

        # serial_number -> monotonic time of last packet seen.
        # Used to compute the flash decay; populated by notify_packet().
        self._last_packet_at: dict[str, float] = {}

        # Hovered X-button index (sniffer row), if any. Used to render
        # the destructive button in red on hover.
        self._x_btn_hover_idx: int | None = None

        # Repaint timer — keeps the flash decay smooth without us having
        # to push frames from the bus thread. Runs only while the panel
        # has any active flash; stopped otherwise to keep CPU idle.
        self._anim = QTimer(self)
        self._anim.setInterval(50)  # 20 Hz — plenty for fading dots
        self._anim.timeout.connect(self._tick)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.refresh()

    # --- public API ---------------------------------------------------

    def refresh(self) -> None:
        """Reload sniffer rows from the DB. Call after a discovery sweep."""
        self._sniffers = self.repos.sniffers.list_all(
            active_only=False, include_removed=False
        )
        self._resize_to_state()
        self.update()

    def notify_packet(self, serial_number: str) -> None:
        """Tick the activity-flash timer for a sniffer.

        Wire this to your live-capture bus — each packet from a known
        sniffer's interface should call this with its serial_number. The
        dot will flash brighter for ~600 ms then decay back to steady.
        """
        self._last_packet_at[serial_number] = time.monotonic()
        if not self._anim.isActive():
            self._anim.start()
        # No update() here — the timer paints; calling update on every
        # packet would saturate the event loop on heavy traffic.

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        if self._expanded == expanded:
            return
        self._expanded = expanded
        self._resize_to_state()
        self.update()
        self.expansionChanged.emit(expanded)

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def reposition(self, viewport_rect: QRectF) -> None:
        """Hug the left edge at the parent viewport's full height."""
        h = int(viewport_rect.height())
        w = self._current_width()
        self.setGeometry(0, 0, w, h)

    # --- internals ----------------------------------------------------

    def _current_width(self) -> int:
        return _PANEL_W if self._expanded else _STRIP_W

    def _resize_to_state(self) -> None:
        if self.parentWidget() is None:
            return
        h = self.parentWidget().height()
        self.setGeometry(0, 0, self._current_width(), h)

    def _tick(self) -> None:
        """Animation tick. Stops itself when no flashes are still decaying."""
        now = time.monotonic()
        # Drop expired entries — keeps the dict from growing unboundedly.
        for sn, t in list(self._last_packet_at.items()):
            if now - t > _FLASH_DURATION_S:
                del self._last_packet_at[sn]
        if not self._last_packet_at:
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
                # Linear interpolate flash color → detected color across
                # the decay window.
                t = age / _FLASH_DURATION_S
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
        for i, s in enumerate(self._sniffers):
            cy = self._dot_center_y(i)
            # Activity dot (same x in both states — matches collapsed strip)
            cx = _STRIP_W // 2
            color = self._dot_color_for(s)
            p.setPen(QPen(_DOT_OUTLINE, 1))
            p.setBrush(QBrush(color))
            p.drawEllipse(
                cx - _DOT_SIZE // 2,
                cy - _DOT_SIZE // 2,
                _DOT_SIZE,
                _DOT_SIZE,
            )
            if self._expanded:
                self._paint_row_silhouette(p, s, cy)
                self._paint_row_text(p, s, cy)
                # X-delete button only on inactive rows. Active sniffers
                # don't need to be hideable — they're really there.
                if not s.is_active:
                    self._paint_x_button(p, i, cy)

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
            new_tt = _row_tooltip(s)
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

    Format: ``<kind>-<short serial>`` so the row is recognizable without
    exposing the full 16-char chip serial. User can override via
    Sniffers.set_name().
    """
    sn = (s.serial_number or "?")
    short = sn[-6:] if len(sn) >= 6 else sn
    kind = (s.kind or "unknown").replace("_", " ")
    return f"{kind}-{short}"


def _row_tooltip(s: Sniffer) -> str:
    """Multi-line tooltip with the full identity for a sniffer row.

    Shown on hover so any truncated value in the rendered row is
    available in full.
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
    state = []
    if s.is_active:
        state.append("active")
    else:
        state.append("inactive")
    if s.removed:
        state.append("removed (hidden)")
    lines.append(f"State:       {', '.join(state)}")
    return "\n".join(lines)
