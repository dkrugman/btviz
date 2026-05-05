"""Rename propagation in ``load_canvas_devices``.

When two physical devices broadcast the same ``local_name`` and the
user has labelled them differently (e.g. left/right hearing aids,
stereo earbuds), the canvas must NOT auto-fill an unnamed third
device with one of those labels — it has no signal to pick the right
one.

Companion fix lives in ``src/btviz/ui/canvas.py``: the propagation
pass now bails when more than one distinct ``user_name`` exists for
a given ``local_name`` or cluster.
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

from btviz.db.repos import Repos  # noqa: E402
from btviz.db.store import Store  # noqa: E402
from btviz.ui.canvas import load_canvas_devices  # noqa: E402


class RenamePropagationTests(unittest.TestCase):

    def setUp(self) -> None:
        d = tempfile.mkdtemp()
        self.store = Store(Path(d) / "rename.db")
        self.repos = Repos(self.store)
        self.project = self.repos.projects.create("p")
        self.session = self.repos.sessions.start(
            self.project.id, source_type="live", name="s",
        )

    def tearDown(self) -> None:
        self.store.close()

    def _create_ha_device(self, suffix: str, *, user_name: str | None) -> int:
        """Create a fake HA RPA device with a single observation row."""
        dev = self.repos.devices.upsert(f"rpa:00:11:22:33:44:{suffix}", "rpa")
        self.repos.devices.merge_identity(
            dev.id, local_name="Douglas Hearing Aids",
        )
        if user_name is not None:
            self.repos.devices.set_user_name(dev.id, user_name)
        self.repos.observations.record_packet(
            self.session.id, dev.id,
            ts=1.0, is_adv=True, rssi=-60, channel=37,
            phy="1M", pdu_type="ADV_IND",
        )
        return dev.id

    def test_ambiguous_local_name_does_not_propagate(self):
        # Real-world bug: user renamed the left HA "Doug HA (L)" and
        # the right HA "Doug HA (R)". A third RPA from either HA shows
        # up unnamed. Previous behavior: the canvas picked the
        # most-recently-renamed name and stamped it on the unnamed
        # device — so the right HA could appear as "Doug HA (L)".
        l_id = self._create_ha_device("01", user_name="Doug HA (L)")
        r_id = self._create_ha_device("02", user_name="Doug HA (R)")
        fresh_id = self._create_ha_device("03", user_name=None)

        loaded = {d.device_id: d for d in load_canvas_devices(
            self.store, self.project.id, stale_cutoff=None,
        )}

        # Renamed devices keep their explicit names.
        self.assertEqual(loaded[l_id].user_name, "Doug HA (L)")
        self.assertEqual(loaded[r_id].user_name, "Doug HA (R)")
        # Unnamed device must remain unnamed — propagation is
        # ambiguous and bailed.
        self.assertIsNone(loaded[fresh_id].user_name)

    def test_unique_local_name_still_propagates(self):
        # Sanity: if only ONE rename exists for a local_name, the
        # propagation behavior is preserved — a fresh RPA picks up
        # the rename. This is the original feature the canvas
        # implements and we don't want to regress it.
        named_id = self._create_ha_device("0a", user_name="My Speaker")
        fresh_id = self._create_ha_device("0b", user_name=None)

        loaded = {d.device_id: d for d in load_canvas_devices(
            self.store, self.project.id, stale_cutoff=None,
        )}

        self.assertEqual(loaded[named_id].user_name, "My Speaker")
        self.assertEqual(loaded[fresh_id].user_name, "My Speaker")


if __name__ == "__main__":
    unittest.main()
