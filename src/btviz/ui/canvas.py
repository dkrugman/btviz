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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRectF, Qt
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
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QToolBar,
    QVBoxLayout,
)

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
_BOX_H_EXPANDED = _HEADER_H + 154   # body holds detailed info block
_BOX_RADIUS = 10
_ICON_SIZE = 32                      # pt; QFont sets this in points
_GRID_COLS = 6
_GRID_DX = _BOX_W + 24
_GRID_DY = _BOX_H_COLLAPSED + 22

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
}
_FALLBACK_ICON = "\U0001F50C"        # 🔌  generic BLE-ish stand-in


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
    full Device dataclass just for the string)."""
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


def apply_grid_layout(devices: list[CanvasDevice]) -> None:
    """Assign default grid positions to any device missing a layout (both
    pos_x and pos_y equal to 0 and no layout row in the DB). Keeps existing
    positions intact."""
    unplaced = [d for d in devices if d.pos_x == 0.0 and d.pos_y == 0.0]
    for i, d in enumerate(unplaced):
        col = i % _GRID_COLS
        row = i // _GRID_COLS
        d.pos_x = 20 + col * _GRID_DX
        d.pos_y = 20 + row * _GRID_DY


# ──────────────────────────────────────────────────────────────────────────
# Device box (QGraphicsItem)
# ──────────────────────────────────────────────────────────────────────────

class DeviceItem(QGraphicsItem):
    """Draggable, collapsible box representing one device.

    Double-click toggles expanded/collapsed. Drag moves and, on release,
    persists position (and collapsed state) via the owning scene's callback.
    """

    def __init__(self, device: CanvasDevice, persist_cb) -> None:
        super().__init__()
        self.device = device
        self._persist = persist_cb
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setPos(device.pos_x, device.pos_y)
        self.setZValue(1)

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

        # Icon (left side). Prefer a bundled SVG if one exists for this
        # device_class; otherwise fall back to the emoji table.
        icon_rect = QRectF(6, 0, 44, _HEADER_H)
        renderer = _icon_renderer(self.device.device_class)
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
        label_font = QFont()
        label_font.setBold(True)
        label_font.setPointSize(11)
        painter.setFont(label_font)
        painter.setPen(QColor(30, 30, 30))
        text_rect = QRectF(52, 4, _BOX_W - 58, _HEADER_H - 8)
        painter.drawText(
            text_rect,
            Qt.AlignVCenter | Qt.AlignLeft | Qt.TextWordWrap,
            self._truncate(self.device.label, 56),
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
        line(f"pkts: {d.packet_count:,} (adv {d.adv_count:,}, data {d.data_count:,})")
        line(f"rssi: {rssi}")
        vendor = d.vendor or d.oui_vendor or "—"
        line(f"vendor: {self._truncate(vendor, 26)}")
        if d.appearance is not None:
            line(f"appearance: 0x{d.appearance:04X}")

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

    # --- interaction --------------------------------------------------

    def mouseDoubleClickEvent(self, event) -> None:
        self.prepareGeometryChange()
        self.device.collapsed = not self.device.collapsed
        self.update()
        self._persist(self.device, save_pos=False)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        p = self.pos()
        # Only persist if the position actually changed (avoids a write for
        # plain clicks/selection).
        if abs(p.x() - self.device.pos_x) > 0.5 or abs(p.y() - self.device.pos_y) > 0.5:
            self.device.pos_x = float(p.x())
            self.device.pos_y = float(p.y())
            self._persist(self.device, save_pos=True)


# ──────────────────────────────────────────────────────────────────────────
# Project picker
# ──────────────────────────────────────────────────────────────────────────

class ProjectPicker(QDialog):
    """Dialog shown at launch to pick (or create) the active project."""

    def __init__(self, store: Store, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("btviz — Select Project")
        self.resize(360, 150)
        self.store = store
        self.repos = Repos(store)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Project:"))
        self.combo = QComboBox()
        layout.addWidget(self.combo)

        new_btn = QPushButton("New project…")
        new_btn.clicked.connect(self._new_project)
        layout.addWidget(new_btn)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self._reload(select_last=True)

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
        name, ok = QInputDialog.getText(self, "New project", "Name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if self.repos.projects.get_by_name(name):
            QMessageBox.warning(self, "btviz", f"Project {name!r} already exists.")
            return
        proj = self.repos.projects.create(name)
        self._reload(select_id=proj.id)

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
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHints(
            QPainter.Antialiasing | QPainter.TextAntialiasing
        )
        self.view.setDragMode(QGraphicsView.RubberBandDrag)
        self.setCentralWidget(self.view)

        tb = QToolBar("main")
        self.addToolBar(tb)
        tb.addAction("Reload", self.reload)
        tb.addAction("Reset layout", self.reset_layout)
        tb.addSeparator()
        self.status = QLabel("")
        tb.addWidget(self.status)

        self.reload()
        self.repos.meta.set(self.repos.meta.LAST_PROJECT, str(project_id))

    # --- data ---------------------------------------------------------

    def reload(self) -> None:
        self.scene.clear()
        devs = load_canvas_devices(self.store, self.project_id)
        apply_grid_layout(devs)
        for d in devs:
            if d.hidden:
                continue
            item = DeviceItem(d, self._persist_device)
            self.scene.addItem(item)
        total_pkts = sum(d.packet_count for d in devs)
        self.status.setText(
            f"  {len(devs)} devices · {total_pkts:,} pkts · "
            f"project id {self.project_id}"
        )
        # Size the scene to contain all items with margin.
        if devs:
            max_x = max(d.pos_x for d in devs) + _BOX_W + 40
            max_y = max(d.pos_y for d in devs) + _BOX_H_EXPANDED + 40
            self.scene.setSceneRect(0, 0, max_x, max_y)

    def reset_layout(self) -> None:
        with self.store.tx():
            self.store.conn.execute(
                "DELETE FROM device_layouts WHERE project_id = ?",
                (self.project_id,),
            )
        self.reload()

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
            QMessageBox.critical(None, "btviz",
                                 f"Project {project_name!r} not found. Ingest "
                                 f"something first or create it from the picker.")
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
