"""User device-class override.

End-to-end exercise of the override pipeline:

  * ``Devices.set_user_device_class`` writes / clears the column.
  * ``load_canvas_devices`` exposes the override (effective
    ``device_class``) and the original auto-detected value
    (``auto_device_class``) so the tooltip can show both.
  * ``cluster.db_loader.load_devices`` selects on the COALESCE'd
    class so the user override drives cluster profile lookup.
  * ``DeviceClassDialog`` filter behavior + display-label rule
    (underscore → space).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402

from btviz.cluster.db_loader import load_devices  # noqa: E402
from btviz.db.repos import Repos  # noqa: E402
from btviz.db.store import Store  # noqa: E402
from btviz.device_classes import (  # noqa: E402
    DEVICE_CLASSES,
    display_label,
)
from btviz.ui.canvas import (  # noqa: E402
    CanvasDevice,
    DeviceClassDialog,
    _CLASS_RESET_SENTINEL,
    _build_tooltip,
    load_canvas_devices,
)


_app: QApplication | None = None


def _ensure_app() -> QApplication:
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication([])
    return _app


def _make_db():
    d = tempfile.mkdtemp()
    store = Store(Path(d) / "uc.db")
    repos = Repos(store)
    project = repos.projects.create("p")
    sess = repos.sessions.start(
        project.id, source_type="live", name="s",
    )
    return store, repos, project, sess


def _seed(repos: Repos, sess_id: int, suffix: str, *,
          auto_class: str | None = None) -> int:
    dev = repos.devices.upsert(f"rpa:00:11:22:33:44:{suffix}", "rpa")
    if auto_class is not None:
        repos.devices.merge_identity(dev.id, device_class=auto_class)
    repos.observations.record_packet(
        sess_id, dev.id,
        ts=1.0, is_adv=True, rssi=-60, channel=37,
        phy="1M", pdu_type="ADV_IND",
    )
    repos.addresses.upsert(
        f"00:11:22:33:44:{suffix}", "rpa", dev.id, now=1.0,
    )
    return dev.id


class RepoTests(unittest.TestCase):

    def test_set_and_clear_user_device_class(self):
        store, repos, project, sess = _make_db()
        try:
            dev_id = _seed(repos, sess.id, "01", auto_class="apple_device")
            repos.devices.set_user_device_class(dev_id, "iphone")
            row = store.conn.execute(
                "SELECT user_device_class, device_class FROM devices "
                "WHERE id = ?", (dev_id,),
            ).fetchone()
            self.assertEqual(row["user_device_class"], "iphone")
            # The auto column is preserved untouched — override doesn't
            # destroy the wire-inferred history.
            self.assertEqual(row["device_class"], "apple_device")

            repos.devices.set_user_device_class(dev_id, None)
            row = store.conn.execute(
                "SELECT user_device_class FROM devices WHERE id = ?",
                (dev_id,),
            ).fetchone()
            self.assertIsNone(row["user_device_class"])
        finally:
            store.close()


class CanvasLoadTests(unittest.TestCase):

    def test_override_drives_effective_class_in_canvas(self):
        store, repos, project, sess = _make_db()
        try:
            dev_id = _seed(repos, sess.id, "02", auto_class="apple_device")
            repos.devices.set_user_device_class(dev_id, "iphone")

            loaded = {d.device_id: d for d in load_canvas_devices(
                store, project.id,
            )}
            cd = loaded[dev_id]
            # Effective class flows through the override.
            self.assertEqual(cd.device_class, "iphone")
            # Both auxiliary fields are populated so the tooltip and
            # any "revert to auto" UX has the data it needs.
            self.assertEqual(cd.auto_device_class, "apple_device")
            self.assertEqual(cd.user_device_class, "iphone")
        finally:
            store.close()

    def test_no_override_passes_auto_through(self):
        store, repos, project, sess = _make_db()
        try:
            dev_id = _seed(repos, sess.id, "03", auto_class="airtag")
            loaded = {d.device_id: d for d in load_canvas_devices(
                store, project.id,
            )}
            cd = loaded[dev_id]
            self.assertEqual(cd.device_class, "airtag")
            self.assertEqual(cd.auto_device_class, "airtag")
            self.assertIsNone(cd.user_device_class)
        finally:
            store.close()

    def test_tooltip_shows_auto_in_parens_when_overridden(self):
        d = CanvasDevice(
            device_id=1, stable_key="rpa:a", kind="unresolved_rpa",
            label="t",
            device_class="iphone",
            auto_device_class="apple_device",
            user_device_class="iphone",
        )
        tip = _build_tooltip(d)
        self.assertIn("Class:         iphone", tip)
        self.assertIn("(auto: apple_device)", tip)

    def test_tooltip_no_paren_suffix_when_no_override(self):
        d = CanvasDevice(
            device_id=1, stable_key="rpa:a", kind="unresolved_rpa",
            label="t",
            device_class="airpods",
            auto_device_class="airpods",
        )
        tip = _build_tooltip(d)
        self.assertIn("Class:         airpods", tip)
        self.assertNotIn("(auto:", tip)


class ClusterLoaderTests(unittest.TestCase):

    def test_cluster_loader_uses_override_for_profile_class(self):
        # The runner picks profile by device_class; the override must
        # therefore propagate into cluster.db_loader's selected value.
        store, repos, project, sess = _make_db()
        try:
            dev_id = _seed(repos, sess.id, "04", auto_class="apple_device")
            repos.devices.set_user_device_class(dev_id, "iphone")

            loaded = load_devices(store, recent_window_s=None)
            picked = next((d for d in loaded if d.id == dev_id), None)
            self.assertIsNotNone(picked)
            self.assertEqual(picked.device_class, "iphone")
        finally:
            store.close()

    def test_cluster_loader_includes_devices_with_override_only(self):
        # Device with NO auto-detected class but a user override
        # should still participate in clustering.
        store, repos, project, sess = _make_db()
        try:
            dev_id = _seed(repos, sess.id, "05", auto_class=None)
            repos.devices.set_user_device_class(dev_id, "iphone")

            loaded = load_devices(store, recent_window_s=None)
            self.assertTrue(any(d.id == dev_id for d in loaded))
        finally:
            store.close()


class CanonicalListTests(unittest.TestCase):

    def test_canonical_list_includes_known_classes(self):
        # Spot-check: classes that show up in real usage.
        for klass in ("iphone", "ipad", "airpods", "airtag",
                      "apple_device", "hearing_aid", "auracast_source",
                      "phone", "headphones"):
            self.assertIn(klass, DEVICE_CLASSES)

    def test_display_label_replaces_underscores_with_spaces(self):
        self.assertEqual(display_label("auracast_source"), "auracast source")
        self.assertEqual(display_label("apple_device"), "apple device")

    def test_display_label_overrides_take_precedence(self):
        # "hid_keyboard" should NOT render as "hid keyboard" — the
        # explicit override gives a more readable form.
        self.assertEqual(display_label("hid_keyboard"), "HID keyboard")
        self.assertEqual(display_label("hid"), "HID (generic)")


class DialogTests(unittest.TestCase):

    def setUp(self) -> None:
        _ensure_app()

    def test_dialog_lists_every_canonical_class_plus_reset(self):
        dlg = DeviceClassDialog(None, current=None, auto=None)
        # Canonical classes + the reset sentinel row.
        self.assertEqual(dlg._list.count(), len(DEVICE_CLASSES) + 1)

    def test_filter_hides_non_matching_rows_keeps_reset_visible(self):
        dlg = DeviceClassDialog(None, current=None, auto=None)
        dlg._search.setText("iphone")
        # Reset row stays visible even when filter doesn't match it.
        # Find it by its sentinel.
        reset_idx = next(
            i for i in range(dlg._list.count())
            if dlg._list.item(i).data(0x100) == _CLASS_RESET_SENTINEL
        )
        self.assertFalse(dlg._list.item(reset_idx).isHidden())
        # The "iphone" row is visible.
        iphone_idx = next(
            i for i in range(dlg._list.count())
            if dlg._list.item(i).data(0x100) == "iphone"
        )
        self.assertFalse(dlg._list.item(iphone_idx).isHidden())
        # An unrelated class — say "weight_scale" — is hidden.
        weight_idx = next(
            i for i in range(dlg._list.count())
            if dlg._list.item(i).data(0x100) == "weight_scale"
        )
        self.assertTrue(dlg._list.item(weight_idx).isHidden())

    def test_filter_matches_against_label_with_spaces(self):
        # User types "auracast source" (label, with a space) and the
        # underscored class still matches.
        dlg = DeviceClassDialog(None, current=None, auto=None)
        dlg._search.setText("auracast source")
        idx = next(
            i for i in range(dlg._list.count())
            if dlg._list.item(i).data(0x100) == "auracast_source"
        )
        self.assertFalse(dlg._list.item(idx).isHidden())

    def test_dialog_preselects_current_override(self):
        dlg = DeviceClassDialog(None, current="iphone", auto="apple_device")
        item = dlg._list.currentItem()
        self.assertIsNotNone(item)
        self.assertEqual(item.data(0x100), "iphone")


if __name__ == "__main__":
    unittest.main()
