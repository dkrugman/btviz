"""PySide6 main window: dongle/sniffer panel + live device table."""
from __future__ import annotations

import time

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..bus import (
    EventBus,
    TOPIC_DEVICE_UPSERT,
    TOPIC_DONGLES_CHANGED,
    TOPIC_SNIFFER_STATE,
)
from ..capture.coordinator import CaptureCoordinator, FollowRequest
from ..extcap import ExtcapNotFound
from ..extcap.sniffer import SnifferState
from ..tracking import Device, Inventory


class DeviceTableModel(QAbstractTableModel):
    HEADERS = ("Address", "Type", "Name", "RSSI", "Ch", "Pkts", "Last seen", "Cmpny")

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[Device] = []
        self._index: dict[str, int] = {}

    def upsert(self, dev: Device) -> None:
        idx = self._index.get(dev.address)
        if idx is None:
            self.beginInsertRows(QModelIndex(), len(self._rows), len(self._rows))
            self._index[dev.address] = len(self._rows)
            self._rows.append(dev)
            self.endInsertRows()
        else:
            self._rows[idx] = dev
            top = self.index(idx, 0)
            bot = self.index(idx, len(self.HEADERS) - 1)
            self.dataChanged.emit(top, bot)

    def device_at(self, row: int) -> Device | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    # Qt model API -----------------------------------------------------
    def rowCount(self, _parent: QModelIndex = QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, _parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role != Qt.DisplayRole:
            return None
        d = self._rows[index.row()]
        c = index.column()
        if c == 0: return d.address
        if c == 1: return d.address_type
        if c == 2: return d.local_name or ""
        if c == 3: return "" if d.last_rssi is None else f"{d.last_rssi} dBm"
        if c == 4: return "" if d.last_channel is None else d.last_channel
        if c == 5: return d.packet_count
        if c == 6: return f"{time.time() - d.last_seen:.1f}s ago"
        if c == 7: return "" if d.company_id is None else f"0x{d.company_id:04X}"
        return None


class _BusBridge(QWidget):
    """Marshals bus callbacks (any thread) onto the Qt main thread via signals."""
    device_upsert = Signal(object)
    sniffer_state = Signal(object)
    dongles_changed = Signal(object)


class MainWindow(QMainWindow):
    def __init__(self, bus: EventBus, coord: CaptureCoordinator) -> None:
        super().__init__()
        self.setWindowTitle("btviz — Bluetooth Discovery")
        self.resize(1100, 700)
        self.bus = bus
        self.coord = coord

        self.bridge = _BusBridge()
        bus.subscribe(TOPIC_DEVICE_UPSERT, self.bridge.device_upsert.emit)
        bus.subscribe(TOPIC_SNIFFER_STATE, self.bridge.sniffer_state.emit)
        bus.subscribe(TOPIC_DONGLES_CHANGED, self.bridge.dongles_changed.emit)
        self.bridge.device_upsert.connect(self._on_device_upsert)
        self.bridge.sniffer_state.connect(self._on_sniffer_state)
        self.bridge.dongles_changed.connect(self._on_dongles_changed)

        # --- top: sniffer/dongle panel ------------------------------
        self.sniffer_label = QLabel("No dongles discovered yet.")
        self.refresh_btn = QPushButton("Refresh dongles")
        self.start_btn = QPushButton("Start scan (37/38/39)")
        self.stop_btn = QPushButton("Stop")
        self.follow_btn = QPushButton("Follow selected")
        self.follow_btn.setEnabled(False)

        self.refresh_btn.clicked.connect(self._refresh_dongles)
        self.start_btn.clicked.connect(self._start_scan)
        self.stop_btn.clicked.connect(self._stop)
        self.follow_btn.clicked.connect(self._follow_selected)

        top = QWidget()
        top_l = QVBoxLayout(top)
        btns = QHBoxLayout()
        for b in (self.refresh_btn, self.start_btn, self.stop_btn, self.follow_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        top_l.addLayout(btns)
        top_l.addWidget(self.sniffer_label)

        # --- bottom: device table -----------------------------------
        self.model = DeviceTableModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.selectionModel().selectionChanged.connect(self._on_selection)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(self.table)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        # Periodic redraw of "last seen" column
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._refresh_view)
        self._tick.start(1000)

        QTimer.singleShot(0, self._refresh_dongles)

    # --- actions -------------------------------------------------------

    def _refresh_dongles(self) -> None:
        try:
            self.coord.refresh_dongles()
        except ExtcapNotFound as e:
            self.sniffer_label.setText(f"⚠ {e}")

    def _start_scan(self) -> None:
        try:
            self.coord.start_discover()
        except ExtcapNotFound as e:
            self.sniffer_label.setText(f"⚠ {e}")

    def _stop(self) -> None:
        self.coord.stop_all()

    def _follow_selected(self) -> None:
        sel = self.table.selectionModel().selectedRows()
        if not sel:
            return
        dev = self.model.device_at(sel[0].row())
        if dev is None:
            return
        self.coord.follow(FollowRequest(target_addr=dev.address))

    # --- bus -> ui -----------------------------------------------------

    def _on_device_upsert(self, dev: Device) -> None:
        self.model.upsert(dev)

    def _on_sniffer_state(self, _state: SnifferState) -> None:
        self._render_sniffer_summary()

    def _on_dongles_changed(self, _dongles: list) -> None:
        self._render_sniffer_summary()

    def _render_sniffer_summary(self) -> None:
        if not self.coord.dongles:
            self.sniffer_label.setText("No dongles discovered.")
            return
        lines: list[str] = []
        for d in self.coord.dongles:
            sp = self.coord.sniffers.get(d.short_id)
            if sp:
                state = sp.state
                ch = "hop 37/38/39" if state.channel == 0 else f"ch {state.channel}"
                lines.append(
                    f"  {d.short_id}  [{state.role}]  {ch}"
                    + (f"  → {state.follow_target}" if state.follow_target else "")
                )
            else:
                lines.append(f"  {d.short_id}  [stopped]")
        self.sniffer_label.setText("Dongles:\n" + "\n".join(lines))

    def _on_selection(self) -> None:
        self.follow_btn.setEnabled(self.table.selectionModel().hasSelection())

    def _refresh_view(self) -> None:
        if self.model.rowCount() > 0:
            top = self.model.index(0, 6)
            bot = self.model.index(self.model.rowCount() - 1, 6)
            self.model.dataChanged.emit(top, bot)

    def closeEvent(self, event) -> None:
        self.coord.stop_all()
        super().closeEvent(event)


def run_gui() -> int:
    app = QApplication.instance() or QApplication([])
    bus = EventBus()
    coord = CaptureCoordinator(bus)
    Inventory(bus)  # subscribes itself
    win = MainWindow(bus, coord)
    win.show()
    return app.exec()
