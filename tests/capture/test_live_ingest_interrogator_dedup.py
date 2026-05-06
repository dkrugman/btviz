"""``LiveIngest`` self-dedup against the active interrogator.

When the interrogator radiates a SCAN_REQ, our own passive sniffers
will pick it up and decode it as advertising traffic from our host's
broadcaster address. Without dedup, that packet would be counted as
a third-party device and fed into the cluster signals, polluting the
analysis. The fix is the
``LiveIngest.set_own_interrogator_addresses`` setter + a hot-path
predicate; these tests cover the predicate's contract without
standing up the full bus-decoder-queue stack.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.capture.live_ingest import LiveIngest  # noqa: E402


@dataclass
class _DecodedFake:
    """Stand-in for the decoded ``Packet`` shape ``_on_packet`` uses.

    Only ``adv_addr`` matters for the dedup predicate; the other
    fields exist on the real Packet but aren't part of this contract.
    """
    adv_addr: str | None = None


def _make_ingest() -> LiveIngest:
    """Build a LiveIngest with all collaborators stubbed.

    The dedup predicate is pure (touches only the addresses set + the
    decoded packet) so we never start the bus, decoder, or DB. ``None``
    on bus/repos is fine because nothing exercises those paths in
    these tests.
    """
    return LiveIngest(
        bus=None,           # type: ignore[arg-type]
        repos=None,         # type: ignore[arg-type]
        project_id=1,
    )


class InterrogatorDedupTests(unittest.TestCase):

    def test_empty_set_means_no_dedup(self):
        ing = _make_ingest()
        # Default state: no interrogator registered → predicate is
        # always False. Hot-path behavior: don't even look at the
        # decoded packet.
        self.assertFalse(
            ing._is_own_interrogator_packet(_DecodedFake(adv_addr="aa:bb:cc:dd:ee:ff"))
        )

    def test_addr_in_set_returns_true(self):
        ing = _make_ingest()
        ing.set_own_interrogator_addresses(("aa:bb:cc:dd:ee:ff",))
        self.assertTrue(
            ing._is_own_interrogator_packet(_DecodedFake(adv_addr="aa:bb:cc:dd:ee:ff"))
        )

    def test_addr_not_in_set_returns_false(self):
        ing = _make_ingest()
        ing.set_own_interrogator_addresses(("aa:bb:cc:dd:ee:ff",))
        self.assertFalse(
            ing._is_own_interrogator_packet(_DecodedFake(adv_addr="11:22:33:44:55:66"))
        )

    def test_uppercase_addr_in_set_normalized_to_lowercase(self):
        # The decoder writes adv_addr lowercase; the public setter
        # normalizes too so callers can hand in either case without
        # silent dedup misses.
        ing = _make_ingest()
        ing.set_own_interrogator_addresses(("AA:BB:CC:DD:EE:FF",))
        self.assertTrue(
            ing._is_own_interrogator_packet(_DecodedFake(adv_addr="aa:bb:cc:dd:ee:ff"))
        )

    def test_none_adv_addr_returns_false(self):
        # SCAN_RSPs and data-channel frames may decode without an
        # adv_addr. Those can't be self-radiated SCAN_REQs (which
        # carry the host's broadcaster) so they pass through.
        ing = _make_ingest()
        ing.set_own_interrogator_addresses(("aa:bb:cc:dd:ee:ff",))
        self.assertFalse(
            ing._is_own_interrogator_packet(_DecodedFake(adv_addr=None))
        )

    def test_clear_with_none_drops_dedup(self):
        ing = _make_ingest()
        ing.set_own_interrogator_addresses(("aa:bb:cc:dd:ee:ff",))
        ing.set_own_interrogator_addresses(None)
        self.assertFalse(
            ing._is_own_interrogator_packet(_DecodedFake(adv_addr="aa:bb:cc:dd:ee:ff"))
        )

    def test_clear_with_empty_set_drops_dedup(self):
        ing = _make_ingest()
        ing.set_own_interrogator_addresses(("aa:bb:cc:dd:ee:ff",))
        ing.set_own_interrogator_addresses(frozenset())
        self.assertFalse(
            ing._is_own_interrogator_packet(_DecodedFake(adv_addr="aa:bb:cc:dd:ee:ff"))
        )

    def test_multiple_addresses_supported(self):
        # An interrogator may rotate its own broadcaster (e.g., for
        # privacy mode). The setter accepts multiple addresses; all
        # match the dedup predicate.
        ing = _make_ingest()
        ing.set_own_interrogator_addresses((
            "aa:bb:cc:dd:ee:ff",
            "11:22:33:44:55:66",
        ))
        self.assertTrue(
            ing._is_own_interrogator_packet(_DecodedFake(adv_addr="11:22:33:44:55:66"))
        )
        self.assertTrue(
            ing._is_own_interrogator_packet(_DecodedFake(adv_addr="aa:bb:cc:dd:ee:ff"))
        )
        self.assertFalse(
            ing._is_own_interrogator_packet(_DecodedFake(adv_addr="99:99:99:99:99:99"))
        )


if __name__ == "__main__":
    unittest.main()
