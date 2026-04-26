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
_PANEL_W = 260                 # expanded-panel width (px) — used in Phase 3
_DOT_SIZE = 12
_ROW_H = 28                    # vertical pitch between sniffers in the strip
_TOP_PAD = 12
_CHEVRON_W = 14
_CHEVRON_H = 28

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
        for i, s in enumerate(self._sniffers):
            cy = self._dot_center_y(i)
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
        # Hit-test the chevron tab. Anywhere outside is currently a no-op
        # in the collapsed state; expanded clicks on rows will be wired
        # in Phase 3.
        if self._chevron_hit(event.position().toPoint()):
            self.toggle()
            event.accept()
            return
        super().mousePressEvent(event)

    def _chevron_hit(self, pos) -> bool:
        cy = self.height() // 2
        x = pos.x()
        y = pos.y()
        return (
            self.width() - _CHEVRON_W - 4 <= x <= self.width() + 6
            and cy - _CHEVRON_H // 2 <= y <= cy + _CHEVRON_H // 2
        )


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
