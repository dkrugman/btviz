"""Preferences dialog — Qt UI auto-generated from the schema.

One tab per ``Field.file`` group, one form row per ``Field``. Widget
type is inferred from ``Field.type`` (with a Browse button for
``ui_kind="path"`` strings). Save button writes through the
in-memory ``Preferences`` instance and persists to disk; cancel
discards.

Adding a knob *never* requires touching this module — the schema
drives everything visible here. Adding a new widget type would.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import Preferences, fields_for_file, files
from .schema import Field

# Subtitle / file-level descriptions shown above each tab's form.
_FILE_BLURB: dict[str, str] = {
    "general": (
        "Application-level paths. Changes here generally require "
        "restarting btviz."
    ),
    "capture": (
        "Sniffer subprocess flags and the capture stall watchdog. "
        "Changes apply on the next Start Capture."
    ),
    "cluster": (
        "Cluster runner thresholds. Apply to the next cluster pass."
    ),
    "canvas": (
        "Display defaults for the canvas: aging curves, default "
        "stale-window selection."
    ),
}


class PreferencesDialog(QDialog):
    """Modal preferences dialog."""

    def __init__(self, prefs: Preferences, parent=None) -> None:
        super().__init__(parent)
        self._prefs = prefs
        # Per-field widgets, keyed by Field.key, so Save knows where
        # to read each value back from.
        self._widgets: dict[str, QWidget] = {}

        self.setWindowTitle("btviz Preferences")
        self.resize(640, 520)

        root = QVBoxLayout(self)

        self._tabs = QTabWidget(self)
        for fname in files():
            self._tabs.addTab(self._build_tab(fname), fname.title())
        root.addWidget(self._tabs)

        # Footer: open-toml + reset-section + the standard buttons.
        footer = QHBoxLayout()
        self._open_btn = QPushButton("Open TOML…", self)
        self._open_btn.setToolTip(
            "Open the TOML file for the active tab in your default "
            "editor. Useful for hand-edits the dialog doesn't expose."
        )
        self._open_btn.clicked.connect(self._on_open_toml)
        footer.addWidget(self._open_btn)

        self._reset_btn = QPushButton("Reset section to defaults", self)
        self._reset_btn.clicked.connect(self._on_reset_section)
        footer.addWidget(self._reset_btn)

        footer.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        root.addLayout(footer)

    # ------------------------------------------------------------------
    # tab construction
    # ------------------------------------------------------------------

    def _build_tab(self, fname: str) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(8, 8, 8, 8)

        blurb = _FILE_BLURB.get(fname)
        if blurb:
            lbl = QLabel(blurb)
            lbl.setWordWrap(True)
            f = lbl.font(); f.setItalic(True); lbl.setFont(f)
            outer.addWidget(lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        form = QFormLayout(inner)
        form.setContentsMargins(0, 8, 0, 8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        for field in fields_for_file(fname):
            widget, controls = self._make_widget(field)
            self._widgets[field.key] = widget
            label = self._make_label(field)
            form.addRow(label, controls if controls is not None else widget)

        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        return page

    def _make_label(self, field: Field) -> QLabel:
        text = field.label
        if field.requires_restart:
            text += " ⟲"
        lbl = QLabel(text)
        lbl.setToolTip(field.description + (
            "\n\n(requires restart)" if field.requires_restart else ""
        ))
        return lbl

    def _make_widget(self, field: Field) -> tuple[QWidget, QWidget | None]:
        """Return (value_widget, container) — container holds extras
        (e.g. Browse button for path fields). When no container, the
        form uses ``value_widget`` directly.
        """
        current = self._prefs.get(field.key)

        if field.type is bool:
            cb = QCheckBox()
            cb.setChecked(bool(current))
            cb.setToolTip(field.description)
            return cb, None

        if field.enum is not None:
            combo = QComboBox()
            for v in field.enum:
                combo.addItem(str(v), v)
            idx = combo.findData(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            combo.setToolTip(field.description)
            return combo, None

        if field.type is int:
            sb = QSpinBox()
            sb.setMinimum(int(field.min) if field.min is not None else -2_147_483_648)
            sb.setMaximum(int(field.max) if field.max is not None else 2_147_483_647)
            sb.setValue(int(current))
            sb.setToolTip(field.description)
            return sb, None

        if field.type is float:
            sb = QDoubleSpinBox()
            sb.setDecimals(2)
            sb.setSingleStep(0.1)
            sb.setMinimum(float(field.min) if field.min is not None else -1e9)
            sb.setMaximum(float(field.max) if field.max is not None else 1e9)
            sb.setValue(float(current))
            sb.setToolTip(field.description)
            return sb, None

        # str
        line = QLineEdit()
        line.setText(str(current))
        line.setToolTip(field.description)
        if field.ui_kind == "path":
            container = QWidget()
            row = QHBoxLayout(container)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(line, 1)
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda _=False, le=line, fld=field:
                                   self._on_browse(le, fld))
            row.addWidget(browse)
            return line, container
        return line, None

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def _on_browse(self, line: QLineEdit, field: Field) -> None:
        # If the field name suggests a directory ("dir"), pick a dir;
        # otherwise pick a file. Cheap heuristic — the only path
        # fields today are db_path, log_dir, nrf_extcap_path.
        is_dir = "dir" in field.name
        current = line.text() or str(Path.home())
        if is_dir:
            chosen = QFileDialog.getExistingDirectory(
                self, f"Choose {field.label}", current,
            )
        else:
            chosen, _ = QFileDialog.getOpenFileName(
                self, f"Choose {field.label}", current,
            )
        if chosen:
            line.setText(chosen)

    def _on_save(self) -> None:
        """Read every widget back into the Preferences object and persist."""
        from .schema import SCHEMA
        for field in SCHEMA:
            w = self._widgets.get(field.key)
            if w is None:
                continue
            value = self._read_widget(field, w)
            self._prefs.set(field.key, value)
        try:
            self._prefs.save()
        except OSError as e:
            QMessageBox.critical(
                self, "btviz Preferences",
                f"Could not save preferences:\n{e}\n\n"
                f"Files in {self._prefs.prefs_dir} were not updated.",
            )
            return
        self.accept()

    def _read_widget(self, field: Field, w: QWidget) -> Any:
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, QComboBox):
            return w.currentData()
        if isinstance(w, QSpinBox):
            return w.value()
        if isinstance(w, QDoubleSpinBox):
            return w.value()
        if isinstance(w, QLineEdit):
            return w.text()
        # Should not happen.
        return field.default

    def _on_reset_section(self) -> None:
        """Reset only the fields visible on the active tab."""
        idx = self._tabs.currentIndex()
        if idx < 0:
            return
        fname = files()[idx]
        from .loader import _resolve_path_default
        for field in fields_for_file(fname):
            w = self._widgets.get(field.key)
            if w is None:
                continue
            self._set_widget_value(w, field, _resolve_path_default(field))

    def _set_widget_value(self, w: QWidget, field: Field, value: Any) -> None:
        if isinstance(w, QCheckBox):
            w.setChecked(bool(value))
        elif isinstance(w, QComboBox):
            i = w.findData(value)
            if i >= 0:
                w.setCurrentIndex(i)
        elif isinstance(w, QSpinBox):
            w.setValue(int(value))
        elif isinstance(w, QDoubleSpinBox):
            w.setValue(float(value))
        elif isinstance(w, QLineEdit):
            w.setText(str(value))

    def _on_open_toml(self) -> None:
        """Reveal the active tab's TOML file in the default app."""
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        idx = self._tabs.currentIndex()
        if idx < 0:
            return
        fname = files()[idx]
        path = self._prefs.prefs_dir / f"{fname}.toml"
        # Ensure file exists so the OS has something to open. Save
        # if missing — the user wanted to inspect.
        if not path.exists():
            try:
                self._prefs.save()
            except OSError:
                return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
