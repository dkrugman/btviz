"""Canvas UI: per-project device board.

A QGraphicsScene populated from the DB. Each device is a draggable
``DeviceItem`` that can toggle between a compact summary and a detailed
view. Layout persists to the ``device_layouts`` table on drag-end.

Entry point: ``run_canvas(db_path=None, project_name=None)``. Called by
``btviz canvas`` in __main__.py. Lives alongside the live-capture table
window in app.py (which is unchanged).
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPen,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..bus import EventBus, TOPIC_SNIFFER_STATE
from ..capture.coordinator import CaptureCoordinator, FollowRequest
from ..capture.live_ingest import LiveIngest
from ..db.models import DeviceLayout
from ..db.repos import Repos
from ..db.store import Store, open_store

# Bundled SVG icons (optional). Drop ``<device_class>.svg`` here and the
# canvas renders it instead of the emoji fallback. See data/icons/README.md
# for the file-naming convention.
_ICONS_DIR = Path(__file__).resolve().parent.parent / "data" / "icons"
_renderer_cache: dict[str, QSvgRenderer | None] = {}


def _icon_renderer(device_class: str | None) -> QSvgRenderer | None:
    """Return a cached ``QSvgRenderer`` for ``device_class``, or None.

    Returns None when no SVG exists (or the file fails to parse), so the
    caller can fall back to the emoji table. Renderers are cached per-class
    for the life of the process — they're cheap once loaded.
    """
    if not device_class:
        return None
    if device_class in _renderer_cache:
        return _renderer_cache[device_class]
    path = _ICONS_DIR / f"{device_class}.svg"
    if not path.exists():
        _renderer_cache[device_class] = None
        return None
    r = QSvgRenderer(str(path))
    _renderer_cache[device_class] = r if r.isValid() else None
    return _renderer_cache[device_class]

# Visual constants. Tuned for a ~1400×900 starting window.
_BOX_W = 220
_HEADER_H = 50
_BOX_H_COLLAPSED = _HEADER_H + 30   # body holds one summary line
_BOX_H_EXPANDED = _HEADER_H + 220   # body holds detailed info block
_BOX_RADIUS = 10
_ICON_SIZE = 32                      # pt; QFont sets this in points
_GRID_DX = _BOX_W + 24               # column pitch (box + gutter)
_GRID_DY = _BOX_H_COLLAPSED + 22     # row pitch (collapsed-box + gutter)
_GRID_MARGIN_X = 20                  # left margin before the first column
# Viewport-responsive column counts are clamped between these so a
# pathologically narrow window still places one column per row, and a
# wide one doesn't spread devices so far apart that they're tedious to
# scan.
_GRID_COLS_MIN = 1
_GRID_COLS_MAX = 12
# Fallback when no viewport width is available (headless / pre-show
# initialization). Matches the previous fixed default.
_GRID_COLS_DEFAULT = 6

# Z-stacking. Expanded boxes sit above collapsed ones so their detail
# region isn't occluded by neighbors. The actively-dragged item rises
# above everything during the drag, then settles back to its
# state-appropriate level on release.
_Z_NORMAL = 1
_Z_EXPANDED = 10
_Z_DRAGGING = 100

# Colors by address kind. Muted so text stays readable.
_KIND_FILL = {
    "public_mac": QColor(210, 235, 210),
    "random_static_mac": QColor(220, 225, 245),
    "unresolved_rpa": QColor(245, 230, 215),
    "nrpa": QColor(230, 230, 230),
    "irk_identity": QColor(210, 235, 230),
    "unknown": QColor(235, 235, 235),
}

# device_class -> emoji icon. Apple-Continuity-derived classes are first;
# GAP-appearance-derived classes second. Edit freely; everything else
# falls back to ``_FALLBACK_ICON``. macOS renders these via Apple Color
# Emoji; modern Linux distros via Noto Color Emoji.
_DEVICE_CLASS_ICONS: dict[str, str] = {
    # Apple Continuity
    "airpods":        "\U0001F3A7",  # 🎧
    "airtag":         "\U0001F4CD",  # 📍
    "apple_watch":    "⌚",      # ⌚
    "apple_device":   "\U0001F4F1",  # 📱  (most are iPhones)
    "apple_airplay":  "\U0001F4FA",  # 📺
    "homekit":        "\U0001F3E0",  # 🏠
    "ibeacon":        "\U0001F4E1",  # 📡
    # GAP appearance fallback
    "phone":          "\U0001F4F1",  # 📱
    "computer":       "\U0001F4BB",  # 💻
    "watch":          "⌚",      # ⌚
    "clock":          "\U0001F550",  # 🕐
    "display":        "\U0001F5A5",  # 🖥️
    "remote_control": "\U0001F39B",  # 🎛
    "eyewear":        "\U0001F453",  # 👓
    "tag":            "\U0001F3F7",  # 🏷
    "keyring":        "\U0001F511",  # 🔑
    "media_player":   "\U0001F3B5",  # 🎵
    "barcode_scanner": "\U0001F4E6",  # 📦
    "thermometer":    "\U0001F321",  # 🌡
    "heart_rate_sensor": "❤",   # ❤
    "blood_pressure_monitor": "\U0001FA7A",  # 🩺
    "hid":            "⌨",      # ⌨
    "glucose_meter":  "\U0001FA78",  # 🩸
    "running_walking_sensor": "\U0001F3C3",  # 🏃
    "cycling_sensor": "\U0001F6B4",  # 🚴
    "pulse_oximeter": "\U0001FAC1",  # 🫁
    "weight_scale":   "⚖",      # ⚖
    "fitness_tracker": "\U0001F3CB",  # 🏋
    "hearing_aid":    "\U0001F9BB",  # 🦻
    "personal_mobility_device": "\U0001F9BD",  # 🦽
    # New classes that came in with the iconscout drop. Emoji are emoji-
    # only fallbacks; SVGs in data/icons/ supersede them automatically.
    "camera":         "\U0001F4F7",  # 📷
    "headphones":     "\U0001F3A7",  # 🎧 (same as airpods — generic non-Apple)
    "windows_computer": "\U0001F5A5",  # 🖥
    "hid_keyboard":   "⌨",       # ⌨
    "hid_mouse":      "\U0001F5B1",  # 🖱
    "hid_joystick":   "\U0001F579",  # 🕹
    "hid_gamepad":    "\U0001F3AE",  # 🎮
    # Apple-class refinements emitted by Continuity Nearby action_code
    # heuristics (see decode/apple_continuity.classify). iPhone and iPad
    # aren't reliably distinguishable from passive sniffing today, but
    # the entries are registered so user-set labels and future heuristics
    # can use them. apple_device.svg covers them as a fallback via the
    # SVG cascade until we add iphone.svg / ipad.svg / mac.svg.
    "iphone":         "\U0001F4F1",  # 📱 (same as apple_device for now)
    "ipad":           "\U0001F4F1",  # 📱 (no distinct tablet emoji that's BLE-specific)
    "mac":            "\U0001F4BB",  # 💻
}
_FALLBACK_ICON = "\U0001F50C"        # 🔌  generic BLE-ish stand-in
_FALLBACK_SVG_NAME = "fallback_icon" # picked up from data/icons/<name>.svg


# ──────────────────────────────────────────────────────────────────────────
# Data loading
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class CanvasDevice:
    """Everything the canvas needs for one device in a project.

    Aggregates observations across all sessions in the project so a device
    seen in multiple captures shows its total activity.
    """
    device_id: int
    stable_key: str
    kind: str
    label: str
    addresses: list[tuple[str, str]] = field(default_factory=list)  # (addr, type)
    vendor: str | None = None
    oui_vendor: str | None = None
    vendor_id: int | None = None
    appearance: int | None = None
    device_class: str | None = None
    local_name: str | None = None
    gatt_device_name: str | None = None
    user_name: str | None = None
    model: str | None = None
    # When this device is the source of an Auracast broadcast somewhere in
    # the project, the most recent broadcast_name for that broadcast.
    broadcast_name: str | None = None
    packet_count: int = 0
    adv_count: int = 0
    data_count: int = 0
    rssi_min: int | None = None
    rssi_max: int | None = None
    rssi_avg: float | None = None
    last_seen: float = 0.0
    channels: dict[int, int] = field(default_factory=dict)
    pdu_types: dict[str, int] = field(default_factory=dict)
    # Layout
    pos_x: float = 0.0
    pos_y: float = 0.0
    collapsed: bool = True
    hidden: bool = False


def load_canvas_devices(store: Store, project_id: int) -> list[CanvasDevice]:
    """Load all devices observed in the project plus their saved layout."""
    conn = store.conn
    rows = conn.execute(
        """
        SELECT
            d.id, d.stable_key, d.kind,
            d.user_name, d.gatt_device_name, d.local_name,
            d.vendor, d.vendor_id, d.oui_vendor, d.model, d.device_class,
            d.appearance, d.last_seen,
            SUM(o.packet_count) AS packet_count,
            SUM(o.adv_count)    AS adv_count,
            SUM(o.data_count)   AS data_count,
            MIN(o.rssi_min)     AS rssi_min,
            MAX(o.rssi_max)     AS rssi_max,
            SUM(o.rssi_sum)     AS rssi_sum_total,
            SUM(o.rssi_samples) AS rssi_n_total,
            MAX(o.last_seen)    AS last_obs
        FROM observations o
        JOIN sessions s ON s.id = o.session_id
        JOIN devices  d ON d.id = o.device_id
        WHERE s.project_id = ?
        GROUP BY d.id
        """,
        (project_id,),
    ).fetchall()

    devices: dict[int, CanvasDevice] = {}
    for r in rows:
        label = _row_best_label(r)
        rssi_avg = (
            r["rssi_sum_total"] / r["rssi_n_total"]
            if r["rssi_n_total"] else None
        )
        cd = CanvasDevice(
            device_id=r["id"],
            stable_key=r["stable_key"],
            kind=r["kind"],
            label=label,
            vendor=r["vendor"],
            vendor_id=r["vendor_id"],
            oui_vendor=r["oui_vendor"],
            appearance=r["appearance"],
            device_class=r["device_class"],
            local_name=r["local_name"],
            gatt_device_name=r["gatt_device_name"],
            user_name=r["user_name"],
            model=r["model"],
            packet_count=r["packet_count"] or 0,
            adv_count=r["adv_count"] or 0,
            data_count=r["data_count"] or 0,
            rssi_min=r["rssi_min"],
            rssi_max=r["rssi_max"],
            rssi_avg=rssi_avg,
            last_seen=r["last_obs"] or r["last_seen"],
        )
        devices[cd.device_id] = cd

    if not devices:
        return []

    # Aggregated pdu_type / channel histograms per device.
    placeholders = ",".join("?" * len(devices))
    hist_rows = conn.execute(
        f"""
        SELECT device_id, pdu_types_json, channels_json
          FROM observations o
          JOIN sessions s ON s.id = o.session_id
         WHERE s.project_id = ? AND o.device_id IN ({placeholders})
        """,
        (project_id, *devices.keys()),
    ).fetchall()
    for r in hist_rows:
        cd = devices[r["device_id"]]
        for k, v in json.loads(r["pdu_types_json"]).items():
            cd.pdu_types[k] = cd.pdu_types.get(k, 0) + v
        for k, v in json.loads(r["channels_json"]).items():
            ch = int(k)
            cd.channels[ch] = cd.channels.get(ch, 0) + v

    # All addresses per device.
    addr_rows = conn.execute(
        f"SELECT device_id, address, address_type FROM addresses "
        f"WHERE device_id IN ({placeholders}) ORDER BY last_seen DESC",
        tuple(devices.keys()),
    ).fetchall()
    for r in addr_rows:
        devices[r["device_id"]].addresses.append((r["address"], r["address_type"]))

    # Most recent broadcast_name per broadcaster (across all sessions in
    # this project). Walk in last_seen DESC order and keep first per
    # device_id so the freshest name wins. Devices that never broadcast
    # stay with broadcast_name=None.
    bcast_rows = conn.execute(
        f"""
        SELECT b.broadcaster_device_id AS did, b.broadcast_name
          FROM broadcasts b
          JOIN sessions s ON s.id = b.session_id
         WHERE s.project_id = ?
           AND b.broadcaster_device_id IN ({placeholders})
           AND b.broadcast_name IS NOT NULL
         ORDER BY b.last_seen DESC
        """,
        (project_id, *devices.keys()),
    ).fetchall()
    for r in bcast_rows:
        cd = devices[r["did"]]
        if cd.broadcast_name is None:
            cd.broadcast_name = r["broadcast_name"]

    # Saved layout per project.
    layout_rows = conn.execute(
        f"SELECT * FROM device_layouts WHERE project_id = ? AND device_id IN ({placeholders})",
        (project_id, *devices.keys()),
    ).fetchall()
    for r in layout_rows:
        cd = devices[r["device_id"]]
        cd.pos_x = r["pos_x"]
        cd.pos_y = r["pos_y"]
        cd.collapsed = bool(r["collapsed"])
        cd.hidden = bool(r["hidden"])

    return list(devices.values())


def _row_best_label(r: Any) -> str:
    """Compute a device best-label from a sqlite Row (avoids constructing a
    full Device dataclass just for the string).

    Note: broadcast_name isn't on the devices row so it can't influence
    this label directly. ``DeviceItem`` adjusts its display to prefer
    broadcast_name when this device is a broadcaster (see
    ``_pick_display_label``).
    """
    if r["user_name"]:
        return r["user_name"]
    if r["gatt_device_name"]:
        return r["gatt_device_name"]
    if r["local_name"]:
        return r["local_name"]
    vendor = r["vendor"] or r["oui_vendor"]
    if vendor and r["model"]:
        return f"{vendor} {r['model']}"
    if vendor and r["device_class"]:
        return f"{vendor} {r['device_class']}"
    if vendor:
        return vendor
    sk = r["stable_key"]
    for p in ("pub:", "rs:", "rpa:", "nrpa:", "irk:", "anon:"):
        if sk.startswith(p):
            return sk[len(p):]
    return sk


def _pick_display_label(d: CanvasDevice) -> str:
    """Choose the strongest identity string for the box header.

    Same precedence as ``_row_best_label`` but includes broadcast_name
    near the top — for a device whose primary identity in this project
    is being an Auracast broadcaster, the broadcast name (e.g. "Avantree
    Oasis Aura_65ac") is the most informative thing we can show.
    """
    if d.user_name:
        return d.user_name
    if d.gatt_device_name:
        return d.gatt_device_name
    if d.local_name:
        return d.local_name
    if d.broadcast_name:
        return d.broadcast_name
    return d.label  # already-computed fallback (vendor + model / class / key)


def _build_tooltip(d: CanvasDevice) -> str:
    """Comprehensive plain-text tooltip showing every full value the box
    might be truncating in the visual.

    Hover-target is the whole DeviceItem — we don't do per-line tooltips
    yet, so this single tooltip lists everything notable. Plain text only
    (Qt's tooltip handles ``\\n``); avoids HTML for cross-platform render
    consistency.
    """
    lines: list[str] = []
    lines.append(_pick_display_label(d))
    lines.append("─" * 36)
    lines.append(f"Stable key:    {d.stable_key}")
    lines.append(f"Kind:          {d.kind}")
    if d.device_class:
        lines.append(f"Class:         {d.device_class}")
    if d.user_name:
        lines.append(f"User name:     {d.user_name}")
    if d.gatt_device_name:
        lines.append(f"GATT name:     {d.gatt_device_name}")
    if d.local_name:
        lines.append(f"Local name:    {d.local_name}")
    if d.broadcast_name:
        lines.append(f"Broadcast:     {d.broadcast_name}")
    if d.model:
        lines.append(f"Model:         {d.model}")
    vendor_full = d.vendor or "(none)"
    lines.append(f"Vendor:        {vendor_full}")
    if d.vendor_id is not None:
        lines.append(f"Vendor ID:     0x{d.vendor_id:04X}")
    if d.oui_vendor:
        lines.append(f"OUI vendor:    {d.oui_vendor}")
    if d.appearance is not None:
        lines.append(f"Appearance:    0x{d.appearance:04X}")
    lines.append("")
    lines.append(
        f"Packets:       {d.packet_count:,} "
        f"(adv {d.adv_count:,}, data {d.data_count:,})"
    )
    if d.rssi_avg is not None:
        lines.append(
            f"RSSI:          avg {d.rssi_avg:.0f} dBm "
            f"(min {d.rssi_min}, max {d.rssi_max})"
        )
    if d.pdu_types:
        lines.append("")
        lines.append("PDU types:")
        for pdu, n in sorted(d.pdu_types.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {pdu}: {n}")
    if d.channels:
        lines.append("")
        lines.append("Channels:")
        for ch, n in sorted(d.channels.items()):
            lines.append(f"  {ch}: {n}")
    if d.addresses:
        lines.append("")
        lines.append(f"Addresses ({len(d.addresses)}):")
        for addr, atype in d.addresses:
            lines.append(f"  {addr}  ({atype})")
    return "\n".join(lines)


# Sort keys for the canvas toolbar's two-level sort dropdowns. Each value
# is a function that maps a CanvasDevice to a sort-comparable key. RSSI
# and Packets are negated so the natural ascending sort surfaces the
# loudest / most-active device first; missing values get a sentinel that
# pushes them to the end. Same dict is the source of truth for both the
# primary and secondary dropdowns; "_SORT_KEY_LABELS" preserves a stable
# UI order distinct from the dict's iteration order.
_SORT_KEY_LABELS: tuple[str, ...] = (
    "Type",
    "Name",
    "Address kind",
    "RSSI (avg)",
    "Vendor",
    "Packets",
    "Last seen",
)
_SORT_KEYS: dict[str, "Callable[[CanvasDevice], Any]"] = {
    "Type":         lambda d: (d.device_class or "~"),
    "Name":         lambda d: _pick_display_label(d).lower(),
    "Address kind": lambda d: (d.kind or "~"),
    "RSSI (avg)":   lambda d: -(d.rssi_avg if d.rssi_avg is not None else -200),
    "Vendor":       lambda d: ((d.vendor or d.oui_vendor) or "~").lower(),
    "Packets":      lambda d: -d.packet_count,
    # Most-recent first. Devices with no observation timestamp get
    # pushed to the end via a sentinel (negated max float).
    "Last seen":    lambda d: -(d.last_seen or 0.0),
}


# ──────────────────────────────────────────────────────────────────────────
# Recency → box opacity
# ──────────────────────────────────────────────────────────────────────────

# Time thresholds for the dormancy fade. Devices observed in the last
# minute paint at full opacity; anything older than 24 hours bottoms out
# at the floor. Between, opacity decays linearly in *log* time so a
# 5-minute-old device looks distinctly fresher than a 1-hour-old one
# (linear-in-seconds would have the difference be invisible).
_RECENCY_FRESH_S = 60.0           # < 1 min ago → fully opaque
_RECENCY_DORMANT_S = 86400.0      # > 24 hr ago → at floor
_RECENCY_MIN_OPACITY = 0.10       # never disappear entirely


def opacity_for_recency(last_seen_ts: float, now_ts: float | None = None) -> float:
    """Linear-in-log-time opacity for a CanvasDevice based on its
    last_seen wall-clock timestamp.

    100% for the first minute, decays to 10% by 24 hours, capped both
    ends. Devices that never produced an observation (last_seen=0)
    return the floor opacity — they're listed because of identity
    info but produced no traffic in this project.
    """
    if last_seen_ts is None or last_seen_ts <= 0:
        return _RECENCY_MIN_OPACITY
    if now_ts is None:
        now_ts = time.time()
    age = max(0.0, now_ts - last_seen_ts)
    if age < _RECENCY_FRESH_S:
        return 1.0
    if age >= _RECENCY_DORMANT_S:
        return _RECENCY_MIN_OPACITY
    import math
    log_age = math.log10(age)
    log_min = math.log10(_RECENCY_FRESH_S)
    log_max = math.log10(_RECENCY_DORMANT_S)
    frac = (log_age - log_min) / (log_max - log_min)
    return 1.0 - (1.0 - _RECENCY_MIN_OPACITY) * frac


def cols_for_viewport(viewport_width: int | None) -> int:
    """Pick a column count that fits the current viewport.

    Returns ``_GRID_COLS_DEFAULT`` when ``viewport_width`` is None
    (headless / pre-show). Otherwise divides the available width by the
    column pitch (box + gutter) and clamps to ``[_GRID_COLS_MIN,
    _GRID_COLS_MAX]`` so a pathologically narrow window still gives 1
    column per row, and a very wide one doesn't spread boxes so far
    apart that they're tedious to scan.
    """
    if viewport_width is None or viewport_width <= 0:
        return _GRID_COLS_DEFAULT
    usable = max(0, viewport_width - 2 * _GRID_MARGIN_X)
    cols = max(1, usable // _GRID_DX)
    return max(_GRID_COLS_MIN, min(_GRID_COLS_MAX, int(cols)))


def apply_grid_layout(
    devices: list[CanvasDevice],
    *,
    cols: int = _GRID_COLS_DEFAULT,
) -> None:
    """Assign default grid positions to any device missing a layout (both
    pos_x and pos_y equal to 0 and no layout row in the DB). Keeps existing
    positions intact.

    ``cols`` lets the caller respect the current viewport width so newly-
    placed devices flow into the available area instead of being stuck
    at the historical 6-column grid.
    """
    unplaced = [d for d in devices if d.pos_x == 0.0 and d.pos_y == 0.0]
    for i, d in enumerate(unplaced):
        col = i % cols
        row = i // cols
        d.pos_x = _GRID_MARGIN_X + col * _GRID_DX
        d.pos_y = _GRID_MARGIN_X + row * _GRID_DY


# ──────────────────────────────────────────────────────────────────────────
# Device box (QGraphicsItem)
# ──────────────────────────────────────────────────────────────────────────

class DeviceItem(QGraphicsItem):
    """Draggable, collapsible box representing one device.

    Double-click toggles expanded/collapsed. Drag moves and, on release,
    persists position (and collapsed state) via the owning scene's callback.
    """

    def __init__(self, device: CanvasDevice, persist_cb,
                 context_cb=None) -> None:
        super().__init__()
        self.device = device
        self._persist = persist_cb
        # Optional callback the scene installs to populate the
        # right-click menu. Signature: (device) -> list[QAction]. When
        # None, no context menu is shown.
        self._context_cb = context_cb
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setPos(device.pos_x, device.pos_y)
        self.setZValue(_Z_EXPANDED if not device.collapsed else _Z_NORMAL)
        # Comprehensive tooltip — gives the full text of every value that
        # might get truncated in the rendered box (vendor, model, name,
        # broadcast name, addresses, etc.) so the user can mouse-over to
        # see what doesn't fit on screen.
        self.setToolTip(_build_tooltip(device))

    # --- geometry -----------------------------------------------------

    def boundingRect(self) -> QRectF:
        h = _BOX_H_EXPANDED if not self.device.collapsed else _BOX_H_COLLAPSED
        return QRectF(0, 0, _BOX_W, h)

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        r = self.boundingRect()
        fill = _KIND_FILL.get(self.device.kind, _KIND_FILL["unknown"])
        pen = QPen(QColor(90, 90, 90), 1)
        if self.isSelected():
            pen = QPen(QColor(20, 90, 200), 2)
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        painter.drawRoundedRect(r, _BOX_RADIUS, _BOX_RADIUS)

        # Header band — taller, with an icon and a two-line text region.
        header_rect = QRectF(0, 0, _BOX_W, _HEADER_H)
        painter.setBrush(QBrush(fill.darker(108)))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(header_rect, _BOX_RADIUS, _BOX_RADIUS)
        # Square off the bottom of the header so it joins the body cleanly.
        painter.drawRect(QRectF(0, _BOX_RADIUS, _BOX_W, _HEADER_H - _BOX_RADIUS))

        # Icon (left side). Cascade:
        #   1. SVG matching device_class (data/icons/<class>.svg)
        #   2. SVG fallback (data/icons/fallback_icon.svg) — covers any
        #      class without a specific SVG, including unknown classes.
        #   3. Class emoji from _DEVICE_CLASS_ICONS
        #   4. _FALLBACK_ICON emoji
        # Steps 1+2 are pure-SVG; 3+4 are pure-emoji. We commit to one path
        # so the icon area's font/style is set up only once.
        icon_rect = QRectF(6, 0, 44, _HEADER_H)
        renderer = (
            _icon_renderer(self.device.device_class)
            or _icon_renderer(_FALLBACK_SVG_NAME)
        )
        if renderer is not None:
            # Center a square SVG inside the icon area, with a few pixels
            # of padding so it doesn't crowd the rounded corner.
            svg_size = 36
            cx = icon_rect.x() + icon_rect.width() / 2
            cy = icon_rect.y() + icon_rect.height() / 2
            renderer.render(
                painter,
                QRectF(cx - svg_size / 2, cy - svg_size / 2, svg_size, svg_size),
            )
        else:
            icon = _DEVICE_CLASS_ICONS.get(
                self.device.device_class or "", _FALLBACK_ICON
            )
            icon_font = QFont()
            icon_font.setPointSize(_ICON_SIZE)
            # Force a font that renders color emoji on macOS; on Linux Qt's
            # cascade picks Noto Color Emoji or similar.
            icon_font.setFamily("Apple Color Emoji")
            painter.setFont(icon_font)
            painter.setPen(QColor(30, 30, 30))
            painter.drawText(icon_rect, Qt.AlignVCenter | Qt.AlignHCenter, icon)

        # Title text (right of icon, two-line region with word wrap).
        # Prefer broadcast_name / GATT name / local name over the
        # vendor-derived fallback when one of those is set.
        label_font = QFont()
        label_font.setBold(True)
        label_font.setPointSize(11)
        painter.setFont(label_font)
        painter.setPen(QColor(30, 30, 30))
        text_rect = QRectF(52, 4, _BOX_W - 58, _HEADER_H - 8)
        painter.drawText(
            text_rect,
            Qt.AlignVCenter | Qt.AlignLeft | Qt.TextWordWrap,
            self._truncate(_pick_display_label(self.device), 56),
        )

        # Body
        body_font = QFont()
        body_font.setPointSize(8)
        painter.setFont(body_font)
        painter.setPen(QColor(50, 50, 50))

        if self.device.collapsed:
            self._paint_collapsed_body(painter)
        else:
            self._paint_expanded_body(painter)

    def _paint_collapsed_body(self, painter: QPainter) -> None:
        d = self.device
        rssi = f"{d.rssi_avg:.0f}" if d.rssi_avg is not None else "—"
        top_chs = sorted(d.channels.items(), key=lambda kv: -kv[1])[:3]
        ch_str = "/".join(str(c) for c, _ in top_chs) if top_chs else "—"
        line = f"{d.packet_count:,} pkts · {rssi} dBm · ch {ch_str}"
        painter.drawText(
            QRectF(8, _HEADER_H + 4, _BOX_W - 16, 16),
            Qt.AlignVCenter | Qt.AlignLeft, line,
        )

    def _paint_expanded_body(self, painter: QPainter) -> None:
        d = self.device
        y = _HEADER_H + 4
        lh = 13  # line height

        def line(txt: str) -> None:
            nonlocal y
            painter.drawText(QRectF(8, y, _BOX_W - 16, lh),
                             Qt.AlignVCenter | Qt.AlignLeft, txt)
            y += lh

        rssi = (
            f"{d.rssi_avg:.0f} dBm (min {d.rssi_min}, max {d.rssi_max})"
            if d.rssi_avg is not None else "—"
        )
        line(f"kind: {d.kind}")
        # Class is what determines the icon and most of the label fallback —
        # users want to know where it came from. Show the class string and
        # the appearance value (if any) that produced it.
        if d.device_class:
            line(f"class: {d.device_class}")
        if d.appearance is not None:
            line(f"appearance: 0x{d.appearance:04X}")
        line(f"pkts: {d.packet_count:,} (adv {d.adv_count:,}, data {d.data_count:,})")
        line(f"rssi: {rssi}")

        # Identity strings — show every name source so it's obvious which
        # one drove the label. Empty values stay unprinted to save vertical
        # space; the tooltip lists them all regardless.
        vendor = d.vendor or d.oui_vendor or "—"
        line(f"vendor: {self._truncate(vendor, 26)}")
        if d.model:
            line(f"model: {self._truncate(d.model, 28)}")
        if d.user_name:
            line(f"user_name: {self._truncate(d.user_name, 24)}")
        if d.gatt_device_name:
            line(f"gatt_name: {self._truncate(d.gatt_device_name, 24)}")
        if d.local_name:
            line(f"local_name: {self._truncate(d.local_name, 22)}")
        if d.broadcast_name:
            line(f"broadcast: {self._truncate(d.broadcast_name, 23)}")

        # Top PDU types
        if d.pdu_types:
            top = sorted(d.pdu_types.items(), key=lambda kv: -kv[1])[:3]
            line("pdu: " + ", ".join(f"{k}={v}" for k, v in top))
        if d.channels:
            top_chs = sorted(d.channels.items(), key=lambda kv: -kv[1])[:5]
            line("ch:  " + ", ".join(f"{c}:{v}" for c, v in top_chs))
        line(f"addresses ({len(d.addresses)}):")
        for addr, _atype in d.addresses[:4]:
            line(f"  {addr}")
        if len(d.addresses) > 4:
            line(f"  +{len(d.addresses) - 4} more")

    @staticmethod
    def _truncate(s: str, n: int) -> str:
        return s if len(s) <= n else s[: n - 1] + "…"

    def _state_z(self) -> int:
        """Z value this item should have when not actively being dragged."""
        return _Z_EXPANDED if not self.device.collapsed else _Z_NORMAL

    # --- interaction --------------------------------------------------

    def mousePressEvent(self, event) -> None:
        # Float to the top while the user is interacting with this box —
        # ensures any drag movement (and the rubber-band selection halo)
        # paints above neighbors, including expanded ones.
        self.setZValue(_Z_DRAGGING)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self.prepareGeometryChange()
        self.device.collapsed = not self.device.collapsed
        self.setZValue(self._state_z())
        self.update()
        self._persist(self.device, save_pos=False)
        event.accept()

    def _persist_moved_selection(self) -> None:
        """Persist position for *every* selected item that actually moved.

        Qt translates a multi-selection drag by repositioning all selected
        items together, but only the grabbed item's release event fires —
        so we walk the selection here and write each one whose position
        drifted past a small dead-band. Also covers the (rare) case where
        a drag happens on an unselected item.
        """
        scene = self.scene()
        if scene is None:
            return
        moved: list[DeviceItem] = []
        for item in scene.selectedItems():
            if not isinstance(item, DeviceItem):
                continue
            p = item.pos()
            if (abs(p.x() - item.device.pos_x) > 0.5
                    or abs(p.y() - item.device.pos_y) > 0.5):
                item.device.pos_x = float(p.x())
                item.device.pos_y = float(p.y())
                moved.append(item)
        if not self.isSelected():
            p = self.pos()
            if (abs(p.x() - self.device.pos_x) > 0.5
                    or abs(p.y() - self.device.pos_y) > 0.5):
                self.device.pos_x = float(p.x())
                self.device.pos_y = float(p.y())
                moved.append(self)
        for item in moved:
            item._persist(item.device, save_pos=True)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        # Settle the dragged item back into its state-appropriate stack.
        self.setZValue(self._state_z())
        self._persist_moved_selection()

    def contextMenuEvent(self, event) -> None:
        """Right-click → menu of device actions, built by the scene.

        The callback returns a fully-assembled ``QMenu`` (rather than a
        list of actions) so it can include submenus — the Copy submenu
        is built that way.
        """
        if self._context_cb is None:
            return
        menu = self._context_cb(self.device)
        if menu is None:
            return
        menu.exec(event.screenPos())
        event.accept()


# ──────────────────────────────────────────────────────────────────────────
# Canvas view with directional rubber-band selection
# ──────────────────────────────────────────────────────────────────────────

class _CanvasView(QGraphicsView):
    """Adobe / CAD-style directional rubber-band selection.

    Drag from upper-left toward lower-right → only items *fully enclosed*
    by the rubber band get selected (``ContainsItemShape``).
    Drag in any other direction → items *intersecting* the rubber band
    get selected (``IntersectsItemShape``).

    Implementation: we don't replace Qt's rubber-band drag — we just
    flip its selection mode mid-drag based on where the mouse is now
    relative to where it pressed down. ``setRubberBandSelectionMode``
    takes effect on the live rubber-band selection, so the user sees the
    selection change as they reverse direction.
    """

    def __init__(self, scene: QGraphicsScene) -> None:
        super().__init__(scene)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setRubberBandSelectionMode(Qt.IntersectsItemShape)
        self._press_pos = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._press_pos is not None and event.buttons() & Qt.LeftButton:
            cur = event.position().toPoint()
            dx = cur.x() - self._press_pos.x()
            dy = cur.y() - self._press_pos.y()
            mode = (Qt.ContainsItemShape
                    if dx > 0 and dy > 0
                    else Qt.IntersectsItemShape)
            if self.rubberBandSelectionMode() != mode:
                self.setRubberBandSelectionMode(mode)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._press_pos = None
        super().mouseReleaseEvent(event)


# ──────────────────────────────────────────────────────────────────────────
# Project picker
# ──────────────────────────────────────────────────────────────────────────

class _ConfirmDialog(QDialog):
    """Tiny modal Yes/No prompt — used in place of QMessageBox.question,
    which segfaults on macOS Tahoe + PySide6 6.11 in the Qt metaobject
    builder. Defaults focus to "No" so Enter doesn't confirm a destructive
    action by accident.
    """

    def __init__(self, parent, title: str, message: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        msg = QLabel(message)
        msg.setWordWrap(True)
        layout.addWidget(msg)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Yes
            | QDialogButtonBox.StandardButton.No
        )
        bb.button(QDialogButtonBox.StandardButton.No).setDefault(True)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)


class ProjectPicker(QDialog):
    """Dialog shown at launch to pick / create / delete the active project."""

    def __init__(self, store: Store, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("btviz — Select Project")
        self.resize(380, 180)
        self.store = store
        self.repos = Repos(store)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Project:"))
        self.combo = QComboBox()
        layout.addWidget(self.combo)

        # Side-by-side row: New / Delete. Delete is disabled until a
        # project is selected and turned red so the destructive action
        # reads as such.
        from PySide6.QtWidgets import QHBoxLayout
        action_row = QHBoxLayout()
        new_btn = QPushButton("New project…")
        new_btn.clicked.connect(self._new_project)
        action_row.addWidget(new_btn)
        self._delete_btn = QPushButton("Delete project…")
        self._delete_btn.setStyleSheet("color: #b00;")
        self._delete_btn.clicked.connect(self._delete_project)
        action_row.addWidget(self._delete_btn)
        layout.addLayout(action_row)

        # Inline status label for transient messages (e.g. "name already
        # exists"). Used instead of QMessageBox.warning, which crashes on
        # macOS Tahoe + PySide6 6.11 in the Qt metaobject builder. Empty
        # by default; rendered in red when populated.
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #b00; font-size: 11px;")
        layout.addWidget(self._status_label)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self.combo.currentIndexChanged.connect(self._update_delete_enabled)
        self._reload(select_last=True)
        self._update_delete_enabled()

    def _update_delete_enabled(self) -> None:
        self._delete_btn.setEnabled(self.combo.count() > 0)

    def _reload(self, select_id: int | None = None, *, select_last: bool = False) -> None:
        self.combo.clear()
        projects = self.repos.projects.list()
        for p in projects:
            self.combo.addItem(p.name, p.id)
        if select_id is not None:
            idx = next(
                (i for i in range(self.combo.count())
                 if self.combo.itemData(i) == select_id),
                -1,
            )
            if idx >= 0:
                self.combo.setCurrentIndex(idx)
                return
        if select_last:
            last = self.repos.meta.get(self.repos.meta.LAST_PROJECT)
            if last:
                try:
                    self._reload(select_id=int(last))
                except (TypeError, ValueError):
                    pass

    def _new_project(self) -> None:
        # Clear any prior inline error before this attempt.
        self._status_label.setText("")
        name, ok = QInputDialog.getText(self, "New project", "Name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if self.repos.projects.get_by_name(name):
            # Inline message instead of QMessageBox.warning (see __init__
            # for why). Selecting the existing entry in the combo is the
            # natural follow-through.
            self._status_label.setText(
                f"Project {name!r} already exists — selected."
            )
            existing = self.repos.projects.get_by_name(name)
            self._reload(select_id=existing.id if existing else None)
            return
        proj = self.repos.projects.create(name)
        self._reload(select_id=proj.id)

    def _delete_project(self) -> None:
        """Delete the currently-selected project after confirmation.

        FK cascades take care of sessions, observations, broadcasts,
        device_layouts, groups, and per-project device meta. Devices
        and addresses are global and stay (other projects may have
        observed them too).
        """
        self._status_label.setText("")
        i = self.combo.currentIndex()
        if i < 0:
            return
        proj_id = self.combo.itemData(i)
        proj_name = self.combo.itemText(i)
        if proj_id is None:
            return
        confirm = _ConfirmDialog(
            self,
            "Delete project?",
            f"Permanently delete project '{proj_name}' and all its "
            "sessions, observations, broadcasts, and canvas layout?\n\n"
            "Devices and addresses are global and will remain.",
        )
        if confirm.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.repos.projects.delete(int(proj_id))
        except Exception as e:  # noqa: BLE001
            self._status_label.setText(f"Delete failed: {e}")
            return
        # Clear LAST_PROJECT if it pointed at this one so the next
        # picker open doesn't try to re-select a deleted id.
        last = self.repos.meta.get(self.repos.meta.LAST_PROJECT)
        if last and str(last) == str(proj_id):
            self.repos.meta.set(self.repos.meta.LAST_PROJECT, "")
        self._reload()
        self._update_delete_enabled()

    def selected_project_id(self) -> int | None:
        i = self.combo.currentIndex()
        return None if i < 0 else self.combo.itemData(i)


# ──────────────────────────────────────────────────────────────────────────
# Main window
# ──────────────────────────────────────────────────────────────────────────

class CanvasWindow(QMainWindow):
    def __init__(self, store: Store, project_id: int) -> None:
        super().__init__()
        self.store = store
        self.repos = Repos(store)
        self.project_id = project_id
        self.project = self.repos.projects.get(project_id)
        self.setWindowTitle(f"btviz canvas — {self.project.name}")
        self.resize(1400, 900)

        self.scene = QGraphicsScene()
        self.view = _CanvasView(self.scene)
        self.view.setRenderHints(
            QPainter.Antialiasing | QPainter.TextAntialiasing
        )

        # Sniffer panel + canvas view live side by side in a HBoxLayout
        # so expanding the panel pushes canvas content right rather than
        # covering it. The panel reports its current width via sizeHint,
        # the view takes the remaining space.
        from .sniffer_panel import SnifferPanel
        self.sniffer_panel = SnifferPanel(store=store)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.sniffer_panel)
        layout.addWidget(self.view, 1)  # stretch factor 1 → view fills the rest
        self.setCentralWidget(central)

        # Live-capture state. Created on first Start; recycled across
        # Start/Stop toggles within the same CanvasWindow instance.
        self._bus: EventBus | None = None
        self._coord: CaptureCoordinator | None = None
        self._live: LiveIngest | None = None
        self._live_timer: QTimer | None = None
        self._reload_tick = 0       # increments per timer fire; reload() runs every Nth
        # short_id (pkt.source) → serial_number, so the bus subscriber's
        # per-source notifications can drive the panel's serial-keyed
        # activity dot.
        self._source_to_serial: dict[str, str] = {}
        # Bus unsubscribe callback for TOPIC_SNIFFER_STATE — set in
        # _start_live and cleared in _stop_live. Surfaces per-sniffer
        # errors (notably capture-loop FIFO failures) on the toolbar.
        self._sniffer_state_unsub: "Callable[[], None] | None" = None

        # Active sort keys (None = honor saved per-device layouts). A
        # transient view-mode toggle: changing either dropdown triggers
        # an immediate re-flow without persisting to device_layouts, so
        # sorts don't clobber positions the user has dragged. Reset
        # layout / Clear all data go through their own paths.
        self._current_sort_primary: str | None = None
        self._current_sort_secondary: str | None = None

        tb = QToolBar("main")
        self.addToolBar(tb)
        tb.addAction("Reload", self.reload)
        tb.addAction("Reset layout", self.reset_layout)
        tb.addAction("Clear all data…", self.clear_all_data)
        tb.addAction("Refresh sniffers", self._refresh_sniffers)
        tb.addSeparator()

        tb.addWidget(QLabel("  Sort by: "))
        self._sort_combo_primary = QComboBox()
        self._sort_combo_primary.addItem("(saved positions)")
        for label in _SORT_KEY_LABELS:
            self._sort_combo_primary.addItem(label)
        self._sort_combo_primary.currentTextChanged.connect(self._on_sort_changed)
        tb.addWidget(self._sort_combo_primary)

        tb.addWidget(QLabel("  then by: "))
        self._sort_combo_secondary = QComboBox()
        self._sort_combo_secondary.addItem("(none)")
        for label in _SORT_KEY_LABELS:
            self._sort_combo_secondary.addItem(label)
        self._sort_combo_secondary.setEnabled(False)  # disabled until primary picked
        self._sort_combo_secondary.currentTextChanged.connect(self._on_sort_changed)
        tb.addWidget(self._sort_combo_secondary)
        tb.addSeparator()

        self._live_action = tb.addAction("Start live", self._toggle_live)
        tb.addSeparator()
        self.status = QLabel("")
        tb.addWidget(self.status)

        # Defer the initial reload until after the window is shown. At
        # this point in __init__ the QGraphicsView's viewport hasn't
        # been laid out yet — viewport().width() returns Qt's default
        # (~300 px) which gives us a 2-column grid regardless of the
        # actual window size. QTimer.singleShot(0) posts the reload to
        # the event loop; by the time it fires, the show event has
        # finished and the viewport has its final width.
        QTimer.singleShot(0, self.reload)
        # The sniffer panel reads from the DB at startup so the window
        # appears immediately. Discovery happens on demand via the
        # "Refresh sniffers" toolbar action — calling the Nordic extcap
        # synchronously here would block UI launch by tens of seconds
        # while the extcap probes every serial-class USB device looking
        # for the sniffer protocol (slow when USB-to-UART bridges like
        # the Adafruit Bluefruit LE Sniffer are connected).
        self.sniffer_panel.refresh()
        self.repos.meta.set(self.repos.meta.LAST_PROJECT, str(project_id))

    # --- data ---------------------------------------------------------

    def reload(self) -> None:
        self.scene.clear()
        devs = load_canvas_devices(self.store, self.project_id)
        # Sort mode (toolbar dropdowns) overrides saved positions: zero
        # out positions, sort the list, and re-grid. Saved layouts in
        # the DB are untouched, so toggling back to "(saved positions)"
        # restores them unmodified. Two-level: primary key first, then
        # secondary as a tiebreaker.
        if self._current_sort_primary:
            p_fn = _SORT_KEYS.get(self._current_sort_primary)
            if p_fn is not None:
                s_fn = (
                    _SORT_KEYS.get(self._current_sort_secondary)
                    if self._current_sort_secondary else None
                )
                for d in devs:
                    d.pos_x = 0.0
                    d.pos_y = 0.0
                if s_fn is not None:
                    devs.sort(key=lambda d: (p_fn(d), s_fn(d)))
                else:
                    devs.sort(key=p_fn)
        # Lay out unplaced devices using a column count derived from the
        # current viewport so a wider window flows them across more
        # columns and a narrower one wraps sooner. Already-placed
        # devices keep their saved positions regardless.
        cols = cols_for_viewport(self.view.viewport().width())
        apply_grid_layout(devs, cols=cols)
        # Single ``now`` reference so all opacities computed in this
        # reload pass see consistent ages — avoids tearing if reload
        # is triggered mid-tick.
        now_ts = time.time()
        for d in devs:
            if d.hidden:
                continue
            item = DeviceItem(d, self._persist_device,
                              context_cb=self._device_context_menu)
            # Dormancy fade: 100% if seen in last minute, decaying log-
            # linearly to 10% at 24 hours and beyond. Live capture's
            # periodic reload (~2s) keeps this current.
            item.setOpacity(opacity_for_recency(d.last_seen, now_ts))
            self.scene.addItem(item)
        # Status reflects what's actually on the canvas — `len(devs)` would
        # include hidden devices (rows with hidden=1 in device_layouts) that
        # we skipped above, leaving an off-by-one between the count and the
        # visible grid (one column ends a row earlier than the other).
        visible_devs = [d for d in devs if not d.hidden]
        hidden_count = len(devs) - len(visible_devs)
        total_pkts = sum(d.packet_count for d in visible_devs)
        hidden_note = f" ({hidden_count} hidden)" if hidden_count else ""
        self.status.setText(
            f"  {len(visible_devs)} devices{hidden_note} · "
            f"{total_pkts:,} pkts · project id {self.project_id}"
        )
        # Size the scene to contain all items with margin.
        if visible_devs:
            max_x = max(d.pos_x for d in visible_devs) + _BOX_W + 40
            max_y = max(d.pos_y for d in visible_devs) + _BOX_H_EXPANDED + 40
            self.scene.setSceneRect(0, 0, max_x, max_y)

    def reset_layout(self) -> None:
        with self.store.tx():
            self.store.conn.execute(
                "DELETE FROM device_layouts WHERE project_id = ?",
                (self.project_id,),
            )
        # Going back to a fresh grid means the user no longer wants the
        # transient sort view either — clear both dropdowns.
        self._current_sort_primary = None
        self._current_sort_secondary = None
        self._sort_combo_primary.setCurrentIndex(0)
        self._sort_combo_secondary.setCurrentIndex(0)
        self._sort_combo_secondary.setEnabled(False)
        self.reload()

    def _on_sort_changed(self, _label: str) -> None:
        """Toolbar sort-dropdown change → re-flow scene by chosen keys.

        Reads both combos directly so we don't have to track which one
        emitted the signal. Translates the combo's first sentinel item
        ("(saved positions)" / "(none)") to None.
        """
        p = self._sort_combo_primary.currentText()
        self._current_sort_primary = p if p in _SORT_KEYS else None
        # Secondary is meaningless when there's no primary.
        self._sort_combo_secondary.setEnabled(self._current_sort_primary is not None)
        s = self._sort_combo_secondary.currentText()
        self._current_sort_secondary = (
            s if (self._current_sort_primary and s in _SORT_KEYS) else None
        )
        self.reload()

    def clear_all_data(self) -> None:
        """Wipe all observations, sessions, broadcasts, and layout for
        this project. Devices and addresses are global and stay
        (other projects may have observed them too).

        Confirmed via _ConfirmDialog because this is destructive and
        irreversible without a backup.
        """
        # Refuse while live capture is running so we don't yank the DB
        # out from under an active write loop.
        if self._live is not None and self._live.running:
            self.status.setText(
                "  cannot clear: stop live capture first"
            )
            return
        confirm = _ConfirmDialog(
            self,
            "Clear all project data?",
            f"Permanently delete all sessions, observations, broadcasts, "
            f"and canvas layout for project '{self.project.name}'?\n\n"
            f"Devices and addresses are global and will remain — they may "
            f"reappear if other projects have observed them, or as soon as "
            f"a new live capture starts.",
        )
        if confirm.exec() != QDialog.DialogCode.Accepted:
            return
        # Sessions cascade to observations + broadcasts via FK ON DELETE
        # CASCADE (see schema.sql). device_layouts cascades from project,
        # but we don't want to nuke the project itself, so delete the
        # layout rows explicitly.
        with self.store.tx():
            self.store.conn.execute(
                "DELETE FROM sessions WHERE project_id = ?",
                (self.project_id,),
            )
            self.store.conn.execute(
                "DELETE FROM device_layouts WHERE project_id = ?",
                (self.project_id,),
            )
        self.reload()
        self.status.setText("  cleared all data for this project")

    def _persist_device(self, d: CanvasDevice, *, save_pos: bool) -> None:
        """Called from DeviceItem on drag-end or expand-toggle."""
        layout = DeviceLayout(
            project_id=self.project_id,
            device_id=d.device_id,
            pos_x=d.pos_x,
            pos_y=d.pos_y,
            collapsed=d.collapsed,
            hidden=d.hidden,
        )
        self.repos.layouts.upsert_device(layout)
        # Touch the project so most-recent-used ordering stays meaningful.
        self.repos.projects.touch(self.project_id)

    # --- live capture -------------------------------------------------

    def _toggle_live(self) -> None:
        """Toolbar action handler: start live capture if stopped, stop if running."""
        if self._live is not None and self._live.running:
            self._stop_live()
        else:
            self._start_live()

    def _start_live(self) -> None:
        """Begin a live capture session for this project.

        Wires up: EventBus → CaptureCoordinator (which spawns SnifferProcess
        per dongle) → bus.publish(TOPIC_PACKET) → LiveIngest (decodes and
        queues) → QTimer flush + periodic reload.
        """
        if self._live is not None and self._live.running:
            return
        self._bus = EventBus()
        self._coord = CaptureCoordinator(self._bus)

        # Discovery: list_dongles() runs the slow extcap probe — acceptable
        # at capture-start (the user pressed Start; they expect to wait).
        try:
            self._coord.refresh_dongles()
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"  live: discovery failed: {e}")
            self._bus = None
            self._coord = None
            return
        if not self._coord.dongles:
            self.status.setText("  live: no dongles discovered")
            self._bus = None
            self._coord = None
            return

        # Persist the extcap-discovered dongles into the sniffers table so
        # the panel renders them as active. The fast USB path (_refresh_sniffers)
        # misses hub-connected dongles on some systems; the slow extcap path
        # used here is authoritative — if it found them, they're active.
        try:
            from ..extcap.discovery import discovered_to_db_records
            records = discovered_to_db_records(self._coord.dongles)
            self.repos.sniffers.record_discovered(records)
            self.sniffer_panel.refresh()
        except Exception:  # noqa: BLE001
            pass

        # Build short_id → serial_number map so the per-source notifier
        # can drive the panel's serial-keyed activity dot.
        self._source_to_serial = {
            d.short_id: (d.serial_number or d.short_id)
            for d in self._coord.dongles
        }

        # Compare the slow extcap probe's discovered set against the DB
        # rows the panel already shows (those came from the fast/USB
        # probe). Anything in the DB that the extcap probe missed gets
        # flagged "USB-detected but not extcap-reachable" — typically a
        # Nordic firmware in a hung state that a replug clears. Tooltip
        # in the panel explains the recovery.
        extcap_serials = {
            (d.serial_number or d.serial_path)
            for d in self._coord.dongles
        }
        try:
            db_sniffers = self.repos.sniffers.list_all(
                active_only=False, include_removed=False,
            )
            unreachable = {
                s.serial_number for s in db_sniffers
                if s.serial_number and s.serial_number not in extcap_serials
            }
        except Exception:  # noqa: BLE001 - never let a UX hint break live start
            unreachable = set()
        self.sniffer_panel.set_extcap_unreachable(unreachable)

        # Surface any per-sniffer state changes (notably last_error from
        # capture-loop failures) on the toolbar status. Without this
        # subscription, a SnifferProcess that fails its FIFO open
        # exits silently and the user sees "capturing on N" with fewer
        # blinking dots than they expect.
        self._sniffer_state_unsub = self._bus.subscribe(
            TOPIC_SNIFFER_STATE, self._on_sniffer_state,
        )

        self._live = LiveIngest(
            self._bus, self.repos, self.project_id,
            session_name=f"live-{int(time.time())}",
        )
        self._live.set_packet_callback(self._on_live_packet)
        self._live.start()

        # Spawn sniffers and apply default roles. Subprocess startup can
        # take a beat — the action becomes "Stop" so a second click stops.
        self._coord.start_discover()

        self._live_timer = QTimer(self)
        self._live_timer.timeout.connect(self._live_tick)
        # 250ms flush cadence: low enough latency that the activity dot
        # feels responsive, high enough that DB writes batch usefully
        # under heavy adv traffic.
        self._live_timer.start(250)

        # Count what actually started — with N dongles and 3 primary
        # advertising channels, default_roles pins 3 and parks the rest
        # as ScanUnmonitored, which only spin up if a primary frees
        # (e.g. when one is re-tasked to Follow). Reserved sniffers are
        # idle subprocesses that haven't been started yet.
        running = sum(
            1 for sp in self._coord.sniffers.values() if sp.state.running
        )
        total = len(self._coord.dongles)
        reserved = total - running
        msg = f"  live: capturing on {running} of {total} devices"
        if reserved > 0:
            msg += f" ({reserved} reserved for follow)"
        msg += "…"
        self._live_action.setText("Stop live")
        self.status.setText(msg)

    def _stop_live(self) -> None:
        if self._live_timer is not None:
            self._live_timer.stop()
            self._live_timer = None
        if self._sniffer_state_unsub is not None:
            try:
                self._sniffer_state_unsub()
            except Exception:  # noqa: BLE001
                pass
            self._sniffer_state_unsub = None
        if self._coord is not None:
            try:
                self._coord.stop_all()
            except Exception:  # noqa: BLE001
                pass
        if self._live is not None:
            self._live.stop()
        self._live_action.setText("Start live")
        self._live = None
        self._coord = None
        self._bus = None
        self._source_to_serial = {}
        # Clear the panel's "extcap-unreachable" hint — the next live
        # start will recompute it from a fresh discovery sweep.
        self.sniffer_panel.set_extcap_unreachable(set())
        # Mark all sniffers inactive in the DB so the panel goes grey.
        try:
            self.repos.sniffers.record_discovered([])
            self.sniffer_panel.refresh()
        except Exception:  # noqa: BLE001
            pass
        # One last reload so the user sees the final state of the session.
        self.reload()

    def _on_sniffer_state(self, state) -> None:
        """Bus subscriber for ``TOPIC_SNIFFER_STATE``.

        Surfaces per-sniffer status changes — most importantly the
        ``last_error`` field, which previously vanished into the void
        when a SnifferProcess's capture-loop exited silently (e.g.
        FIFO open returned no bytes). Without this, the toolbar still
        said "capturing on N" while only N-1 dongles were actually
        running.

        Runs on the bus reader thread; the toolbar status label is a
        Qt widget but ``setText`` is thread-safe enough on macOS for
        this lightweight use. If we see threading issues here, route
        through a Qt signal.
        """
        err = getattr(state, "last_error", None)
        if not err:
            return
        sid = getattr(getattr(state, "dongle", None), "short_id", "?")
        self.status.setText(f"  sniffer error [{sid}]: {err}")

    def _live_tick(self) -> None:
        """QTimer callback (main thread). Drains the queue; reloads every Nth."""
        if self._live is None:
            return
        self._live.flush()
        self._reload_tick += 1
        # Reload the scene every ~2s (8 ticks * 250ms). Full rebuild is
        # heavy (re-runs the project-aggregate query and rebuilds every
        # DeviceItem) — incremental updates can replace this later.
        if self._reload_tick % 8 == 0:
            self.reload()
            stats = self._live.stats
            self.status.setText(
                f"  live: rx={stats.packets_received:,} "
                f"dec={stats.packets_decoded:,} "
                f"rec={stats.packets_recorded:,} "
                f"drop={stats.packets_dropped} "
                f"dev={stats.devices_touched} "
                f"ext={stats.ext_adv_seen}"
                f"({stats.ext_adv_with_baa} baa) "
                f"bcast={stats.broadcasts_seen}"
            )

    def _on_live_packet(self, source: str) -> None:
        """LiveIngest per-source notifier. Drives the panel's activity dot."""
        serial = self._source_to_serial.get(source)
        if serial is None:
            return
        self.sniffer_panel.notify_packet(serial)

    # --- device context menu / follow --------------------------------

    def _device_context_menu(self, device: CanvasDevice) -> QMenu:
        """Build the right-click menu for one DeviceItem.

        Returns a fully-assembled ``QMenu`` so the canvas can include
        submenus (rather than just a flat list of actions). Entries:

          * ``Rename device…`` — sets a per-device ``user_name`` that
            wins over every automatic naming source (local_name,
            gatt_device_name, broadcast_name, vendor+model fallback).
          * ``Follow this device`` — re-tasks one sniffer to track the
            device's most-recent address. Useful for capturing
            post-CONNECT_IND data-channel traffic and (with an IRK
            loaded) resolving its rotating RPAs back to a stable
            identity. Disabled when prerequisites aren't met.
          * ``Copy ▶`` submenu — Address / Name / Stable key /
            All info (the multi-line tooltip we already build).
        """
        menu = QMenu()

        rename_action = menu.addAction("Rename device…")
        rename_action.setToolTip(
            "Set a custom name for this device. Wins over every "
            "automatic naming source (local name, GATT name, vendor)."
        )
        rename_action.triggered.connect(
            lambda checked=False, d=device: self._rename_device(d)
        )

        follow_action = menu.addAction("Follow this device")
        if self._coord is None or self._live is None or not self._live.running:
            follow_action.setEnabled(False)
            follow_action.setToolTip(
                "Start live capture first (toolbar → Start live)."
            )
        elif not device.addresses:
            follow_action.setEnabled(False)
            follow_action.setToolTip(
                "This device has no recorded addresses to follow."
            )
        else:
            addr, addr_type = device.addresses[0]
            is_random = addr_type != "public"
            follow_action.setToolTip(
                f"Re-task one sniffer to follow {addr} "
                f"({addr_type or 'unknown'})."
            )
            follow_action.triggered.connect(
                lambda checked=False, a=addr, r=is_random:
                    self._follow_device(a, r)
            )

        menu.addSeparator()
        copy_menu = menu.addMenu("Copy")

        # Most-recent address (addresses[0]; load_canvas_devices sorts
        # them by last_seen DESC). Disabled when the device has none.
        addr_str = device.addresses[0][0] if device.addresses else ""
        addr_action = copy_menu.addAction("Address")
        if addr_str:
            addr_action.triggered.connect(
                lambda checked=False, s=addr_str: self._copy_to_clipboard(s)
            )
        else:
            addr_action.setEnabled(False)

        name_str = _pick_display_label(device)
        name_action = copy_menu.addAction("Name")
        name_action.triggered.connect(
            lambda checked=False, s=name_str: self._copy_to_clipboard(s)
        )

        key_action = copy_menu.addAction("Stable key")
        key_action.triggered.connect(
            lambda checked=False, s=device.stable_key: self._copy_to_clipboard(s)
        )

        all_action = copy_menu.addAction("All info (tooltip)")
        all_action.triggered.connect(
            lambda checked=False, d=device: self._copy_to_clipboard(_build_tooltip(d))
        )

        return menu

    def _copy_to_clipboard(self, text: str) -> None:
        """Put ``text`` on the system clipboard and surface what was
        copied via the toolbar status line.

        Long values (the All-info tooltip) get truncated in the
        confirmation message so the status bar doesn't blow out width.
        """
        if not text:
            return
        QApplication.clipboard().setText(text)
        first_line = text.splitlines()[0] if "\n" in text else text
        snippet = first_line if len(first_line) <= 60 else first_line[:57] + "…"
        self.status.setText(f"  copied: {snippet}")

    def _rename_device(self, device: CanvasDevice) -> None:
        """Prompt for a new ``user_name`` and persist via the devices repo.

        Empty input clears the override. Reload after the change so the
        new label propagates to the box title and tooltip.
        """
        current = device.user_name or ""
        new_name, ok = QInputDialog.getText(
            self,
            "Rename device",
            f"Name for {_pick_display_label(device)}:",
            text=current,
        )
        if not ok:
            return
        new_name = new_name.strip()
        # Empty string clears the override; pass None to set_user_name.
        self.repos.devices.set_user_name(
            device.device_id, new_name if new_name else None,
        )
        self.reload()

    def _follow_device(self, address: str, is_random: bool) -> None:
        """Ask the coordinator to dedicate a sniffer to this address.

        Called from the device context-menu handler. The coordinator
        prefers an idle sniffer, falls back to a scan-unmonitored one,
        and re-tasks any other dongle if neither is available.
        """
        if self._coord is None:
            return
        chosen = self._coord.follow(
            FollowRequest(target_addr=address, is_random=is_random)
        )
        if chosen is None:
            self.status.setText(
                f"  follow: no sniffer available to retask for {address}"
            )
        else:
            self.status.setText(
                f"  follow: sniffer {chosen} now tracking {address}"
            )

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        # Make sure live capture / subprocesses are torn down so we don't
        # leak FIFOs or extcap processes when the window closes.
        if self._live is not None and self._live.running:
            self._stop_live()
        super().closeEvent(event)

    def _refresh_sniffers(self) -> None:
        """Re-run discovery, persist into the sniffers table, refresh panel.

        Uses the fast ``list_dongles_fast()`` path which enumerates USB
        descriptors via ioreg — instant, no subprocess hang. The slow
        ``list_dongles()`` path that calls the Nordic extcap binary's
        --extcap-interfaces probe is reserved for capture-time use, where
        the user has explicitly asked to start a capture and expects to
        wait. Discovery for the panel display doesn't need that probe.

        Discovery failure (e.g. ioreg unavailable on a non-macOS host)
        shouldn't crash the canvas — log it via the status bar and keep
        what's already in the DB on screen.
        """
        try:
            from ..extcap.discovery import (
                discovered_to_db_records, list_dongles_fast,
            )
            dongles = list_dongles_fast()
            # If live capture is running, the coordinator holds the authoritative
            # list of active dongles (found via the slow extcap probe). Merge
            # them in so hub-connected dongles aren't accidentally deactivated
            # by ioreg misses.
            if self._coord is not None:
                coord_serials = {
                    (d.serial_number or d.serial_path)
                    for d in self._coord.dongles
                }
                fast_serials = {
                    (d.serial_number or d.serial_path) for d in dongles
                }
                if coord_serials - fast_serials:
                    extra = [
                        d for d in self._coord.dongles
                        if (d.serial_number or d.serial_path) not in fast_serials
                    ]
                    dongles = dongles + extra
            records = discovered_to_db_records(dongles)
            self.repos.sniffers.record_discovered(records)
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"  sniffer discovery failed: {e}")
        self.sniffer_panel.refresh()


# ──────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────

def run_canvas(db_path: Path | None = None, project_name: str | None = None) -> int:
    """Launch the canvas window. If project_name is None, show the picker."""
    app = QApplication.instance() or QApplication([])
    store = open_store(db_path)

    repos = Repos(store)
    project_id: int | None = None
    if project_name:
        proj = repos.projects.get_by_name(project_name)
        if proj is None:
            # NOTE: previously this used QMessageBox.critical(None, ...) but
            # it segfaults on macOS Tahoe + PySide6 6.11 in the Qt
            # metaobject builder (PyUnicode_InternFromString → bad ptr deref).
            # Bug isn't ours; until Qt/PySide ship a fix, surface the error
            # via stderr — it's better CLI UX anyway.
            import sys
            print(
                f"error: project {project_name!r} not found. "
                f"Ingest something first, or omit --project to use the "
                f"picker dialog.",
                file=sys.stderr,
            )
            return 2
        project_id = proj.id
    else:
        # Create a default project if the DB is empty so the picker isn't blank.
        if not repos.projects.list():
            repos.projects.create("default")
        picker = ProjectPicker(store)
        if picker.exec() != QDialog.Accepted:
            return 0
        project_id = picker.selected_project_id()
        if project_id is None:
            return 0

    win = CanvasWindow(store, project_id)
    win.show()
    return app.exec()
