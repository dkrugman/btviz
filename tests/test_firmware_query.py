"""Firmware-version query + Coded-PHY compatibility detection.

Tests the protocol parsing (SLIP framing, RESP_VERSION extraction) and
the three-state ``CodedPhyStatus`` decision logic without touching real
serial hardware. The on-the-wire path is exercised via byte fixtures
so it stays deterministic on machines without dongles attached.

Why pin this rigorously: the schema entry for ``capture.coded_phy`` is
silent-failure-prone (Nordic firmware 4.1.1 makes capture stop dead
without surfacing any error), so the prefs UI relies on this query
returning a stable, parseable answer. A regression that silently
returns ``None`` for valid firmware would re-open the same trap.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.extcap.firmware_query import (  # noqa: E402
    CODED_PHY_HELP_URL,
    CODED_PHY_KNOWN_BROKEN,
    CodedPhyStatus,
    HEADER_LENGTH,
    PROTOVER_V1,
    REQ_VERSION,
    RESP_VERSION,
    SLIP_END,
    SLIP_ESC,
    SLIP_ESC_END,
    SLIP_ESC_ESC,
    SLIP_ESC_START,
    SLIP_START,
    _build_request,
    _slip_decode,
    _slip_encode,
    coded_phy_status_for_versions,
    parse_version,
    parse_version_response,
)


class SlipFramingTests(unittest.TestCase):
    """SLIP encode/decode roundtrip + escape correctness."""

    def test_roundtrip_plain_payload(self):
        # Bytes with no escape needs round-trip cleanly.
        payload = bytes([0x06, 0x00, 0x01, 0x05, 0x00, 0x1B])
        encoded = _slip_encode(payload)
        # Must be wrapped in SLIP_START / SLIP_END.
        self.assertEqual(encoded[0], SLIP_START)
        self.assertEqual(encoded[-1], SLIP_END)
        self.assertEqual(_slip_decode(encoded), payload)

    def test_roundtrip_payload_containing_start_byte(self):
        # SLIP_START (0xAB) inside the body must be escaped to
        # SLIP_ESC + SLIP_ESC_START.
        payload = bytes([0xAB])
        encoded = _slip_encode(payload)
        self.assertIn(SLIP_ESC, encoded)
        self.assertIn(SLIP_ESC_START, encoded)
        self.assertEqual(_slip_decode(encoded), payload)

    def test_roundtrip_payload_containing_end_byte(self):
        payload = bytes([SLIP_END])
        encoded = _slip_encode(payload)
        self.assertEqual(_slip_decode(encoded), payload)

    def test_roundtrip_payload_containing_esc_byte(self):
        payload = bytes([SLIP_ESC])
        encoded = _slip_encode(payload)
        self.assertEqual(_slip_decode(encoded), payload)

    def test_roundtrip_all_three_specials_in_one_payload(self):
        payload = bytes([SLIP_START, 0x42, SLIP_END, 0xFF, SLIP_ESC, 0x00])
        self.assertEqual(_slip_decode(_slip_encode(payload)), payload)


class BuildRequestTests(unittest.TestCase):
    """Verify the host→firmware request packet matches Nordic's layout."""

    def test_req_version_header_layout(self):
        encoded = _build_request(REQ_VERSION, counter=42)
        decoded = _slip_decode(encoded)
        # Layout: [HEADER_LEN][payload_len][PROTOVER_V1][ctr_lo][ctr_hi][id]
        self.assertEqual(decoded[0], HEADER_LENGTH)
        self.assertEqual(decoded[1], 0)
        self.assertEqual(decoded[2], PROTOVER_V1)
        self.assertEqual(decoded[3], 42)        # counter low byte
        self.assertEqual(decoded[4], 0)         # counter high byte
        self.assertEqual(decoded[5], REQ_VERSION)

    def test_counter_high_byte_set_when_counter_exceeds_byte(self):
        encoded = _build_request(REQ_VERSION, counter=0x0102)
        decoded = _slip_decode(encoded)
        self.assertEqual(decoded[3], 0x02)
        self.assertEqual(decoded[4], 0x01)


class ParseVersionResponseTests(unittest.TestCase):
    """RESP_VERSION packet → version string."""

    @staticmethod
    def _resp_packet(version_str: str) -> bytes:
        """Build a synthetic decoded RESP_VERSION packet."""
        version_bytes = version_str.encode("ascii")
        return bytes([
            HEADER_LENGTH, len(version_bytes), PROTOVER_V1,
            0x00, 0x00, RESP_VERSION,
        ]) + version_bytes

    def test_parses_simple_version(self):
        pkt = self._resp_packet("4.1.1")
        self.assertEqual(parse_version_response(pkt), "4.1.1")

    def test_strips_null_padding(self):
        # Firmware sometimes appends a trailing NUL.
        pkt = self._resp_packet("4.1.1") + b"\x00\x00"
        self.assertEqual(parse_version_response(pkt), "4.1.1")

    def test_strips_trailing_whitespace(self):
        pkt = self._resp_packet("4.1.1\n")
        self.assertEqual(parse_version_response(pkt), "4.1.1")

    def test_returns_none_for_wrong_packet_id(self):
        # PING_RESP (0x0E) instead of RESP_VERSION.
        pkt = bytes([HEADER_LENGTH, 0, PROTOVER_V1, 0, 0, 0x0E])
        self.assertIsNone(parse_version_response(pkt))

    def test_returns_none_for_truncated_packet(self):
        self.assertIsNone(parse_version_response(b""))
        self.assertIsNone(parse_version_response(b"\x06\x00\x01\x00\x00"))


class ParseVersionTupleTests(unittest.TestCase):
    """Version string → tuple-of-ints for ordering comparisons."""

    def test_three_part(self):
        self.assertEqual(parse_version("4.1.1"), (4, 1, 1))

    def test_two_part(self):
        self.assertEqual(parse_version("4.1"), (4, 1))

    def test_returns_none_for_garbage(self):
        self.assertIsNone(parse_version("not a version"))
        self.assertIsNone(parse_version(""))
        self.assertIsNone(parse_version(None))

    def test_ordering_is_correct_for_4_1_1_vs_4_2_0(self):
        # Sanity: must compare numerically, not lexically.
        self.assertLess(parse_version("4.1.1"), parse_version("4.2.0"))
        self.assertLess(parse_version("4.1.1"), parse_version("4.1.10"))


class CodedPhyStatusForVersionsTests(unittest.TestCase):
    """Three-state decision logic.

    Mirrors what the prefs dialog renders:
      * blocked → 4.1.1 detected somewhere → checkbox disabled
      * warning → only newer-than-4.1.1 firmware → soft-warning link
      * None    → all unknown / older / no detection → render plain
    """

    def test_no_versions_means_no_warning(self):
        self.assertIsNone(coded_phy_status_for_versions([]).severity)

    def test_only_unknown_versions_means_no_warning(self):
        # ``None`` entries from query failures or unparseable strings.
        st = coded_phy_status_for_versions([None, None])
        self.assertIsNone(st.severity)

    def test_known_broken_version_blocks(self):
        st = coded_phy_status_for_versions(["4.1.1"])
        self.assertEqual(st.severity, "blocked")
        self.assertIn("4.1.1", st.suffix or "")
        self.assertIn("incompatible", (st.suffix or "").lower())
        self.assertEqual(st.url, CODED_PHY_HELP_URL)

    def test_blocked_message_matches_user_specified_format(self):
        # The user pinned the exact text — keep it verbatim so a rename
        # doesn't silently regress the UX they asked for.
        st = coded_phy_status_for_versions(["4.1.1"])
        self.assertEqual(st.suffix, "FW v. 4.1.1 detected, incompatible")

    def test_blocked_tooltip_matches_user_specified_format(self):
        st = coded_phy_status_for_versions(["4.1.1"])
        self.assertIn(
            "One or more sniffer devices is using Nordic Firmware "
            "v. 4.1.1 which is incompatible with capturing coded PHY",
            st.tooltip or "",
        )

    def test_blocked_wins_over_warning_when_mixed(self):
        # Even one 4.1.1 is enough to disable, regardless of other dongles.
        st = coded_phy_status_for_versions(["4.1.1", "4.2.0"])
        self.assertEqual(st.severity, "blocked")

    def test_newer_firmware_yields_warning(self):
        st = coded_phy_status_for_versions(["4.2.0"])
        self.assertEqual(st.severity, "warning")
        self.assertEqual(st.suffix, "compatibility warning")
        self.assertEqual(st.url, CODED_PHY_HELP_URL)

    def test_older_firmware_yields_no_warning(self):
        # Nordic added Coded PHY in 4.0.0, broken in 4.1.1. We don't
        # warn on pre-broken-version firmware — those users have other
        # bugs but not this one.
        st = coded_phy_status_for_versions(["4.0.0"])
        self.assertIsNone(st.severity)

    def test_known_broken_constant_is_4_1_1(self):
        # Pin the constant so a future "let's also block 4.1.0" change
        # surfaces as a deliberate test edit.
        self.assertEqual(CODED_PHY_KNOWN_BROKEN, (4, 1, 1))


class CodedPhyStatusContractTests(unittest.TestCase):
    """The dataclass-like ``CodedPhyStatus`` is the interface the UI
    depends on. Pin its shape so a refactor to ``@dataclass`` later
    doesn't silently drop fields the dialog reads.
    """

    def test_severity_none_status_is_no_op(self):
        s = CodedPhyStatus(severity=None)
        self.assertIsNone(s.severity)
        self.assertIsNone(s.suffix)
        self.assertIsNone(s.tooltip)
        self.assertIsNone(s.url)
        self.assertEqual(s.versions, ())

    def test_url_is_devzone_thread(self):
        # Guard the URL — if it changes, tests should force the dev to
        # re-confirm the new target documents the same Nordic-engineer
        # acknowledgment.
        self.assertIn("devzone.nordicsemi.com", CODED_PHY_HELP_URL)
        self.assertIn("117393", CODED_PHY_HELP_URL)


if __name__ == "__main__":
    unittest.main()
