"""Tests for the enriched Apple Continuity protocol parser."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.cluster.signals._continuity_protocol import (
    APPLE_CID_BE,
    CONTINUITY_TYPES,
    parse_continuity,
)


class ParserBasicsTests(unittest.TestCase):
    """Bookkeeping: framing, naming, stable-prefix bookkeeping."""

    def test_returns_empty_for_non_apple_cid(self):
        self.assertEqual(parse_continuity(bytes.fromhex("0600AABB")), [])

    def test_returns_empty_for_short_blob(self):
        self.assertEqual(parse_continuity(b""), [])
        self.assertEqual(parse_continuity(APPLE_CID_BE), [])

    def test_names_known_types(self):
        blob = APPLE_CID_BE + bytes.fromhex("0c0e08e55a11b4b17dd224409c38608b")
        tlvs = parse_continuity(blob)
        self.assertEqual(len(tlvs), 1)
        self.assertEqual(tlvs[0].type, 0x0C)
        self.assertEqual(tlvs[0].type_name, "Handoff")

    def test_falls_back_to_hex_name_for_unknown_types(self):
        # Type 0x42 is reserved/unknown
        blob = APPLE_CID_BE + bytes.fromhex("4202aabb")
        tlvs = parse_continuity(blob)
        self.assertEqual(tlvs[0].type, 0x42)
        self.assertEqual(tlvs[0].type_name, "type_0x42")

    def test_stable_prefix_set_per_known_type(self):
        # Type 0x10 has 2-byte stable prefix per spec
        blob = APPLE_CID_BE + bytes.fromhex("1006141d738ffc80")
        tlvs = parse_continuity(blob)
        self.assertEqual(tlvs[0].stable_prefix.hex(), "141d")

    def test_stable_prefix_clamped_to_payload_length(self):
        # Type 0x07 has 4-byte stable prefix, but if payload is only 2 bytes
        # we shouldn't return more than what's there.
        blob = APPLE_CID_BE + bytes.fromhex("0702aabb")
        tlvs = parse_continuity(blob)
        self.assertEqual(tlvs[0].stable_prefix.hex(), "aabb")

    def test_handles_truncated_tlv_gracefully(self):
        # Length claims 8 bytes but only 3 follow
        blob = APPLE_CID_BE + bytes.fromhex("0c08aabbcc")
        tlvs = parse_continuity(blob)
        self.assertEqual(tlvs, [])


class ProximityPairingDecoderTests(unittest.TestCase):
    """Type 0x07: AirPods model lookup + battery decode."""

    def _airpods_payload(self, model_bytes: bytes, status: int = 0x55,
                        bl: int = 4, br: int = 5, bc: int = 6) -> bytes:
        """Build a minimal valid type-0x07 payload."""
        battery_lr = (bl << 4) | br
        battery_case_state = (bc << 4) | 0x0
        return (
            bytes([0x01])              # state byte
            + model_bytes              # bytes 1-2: model
            + bytes([status])          # byte 3: status flags
            + bytes([battery_lr])      # byte 4: left+right battery nibbles
            + bytes([battery_case_state])  # byte 5: case battery + state nibbles
            + bytes([0x80])            # byte 6: lid+battery state
            + bytes(8)                 # bytes 7-14: padding
        )

    def test_airpods_pro_2nd_gen_usb_c_decodes(self):
        blob = APPLE_CID_BE + bytes.fromhex("070f") + self._airpods_payload(
            bytes.fromhex("2420")
        )
        tlvs = parse_continuity(blob)
        self.assertEqual(tlvs[0].type_name, "ProximityPairing")
        self.assertEqual(tlvs[0].decoded["model_bytes"], "2420")
        self.assertEqual(
            tlvs[0].decoded["model_name"],
            "AirPods Pro 2nd gen (USB-C)",
        )

    def test_unknown_model_keeps_raw_bytes(self):
        blob = APPLE_CID_BE + bytes.fromhex("070f") + self._airpods_payload(
            bytes.fromhex("ffff")
        )
        tlvs = parse_continuity(blob)
        self.assertEqual(tlvs[0].decoded["model_bytes"], "ffff")
        self.assertEqual(tlvs[0].decoded["model_name"], "unknown")

    def test_battery_nibbles_decode_to_percent(self):
        blob = APPLE_CID_BE + bytes.fromhex("070f") + self._airpods_payload(
            bytes.fromhex("1420"), bl=8, br=9, bc=2
        )
        d = parse_continuity(blob)[0].decoded
        self.assertEqual(d["battery_left_pct"], 80)
        self.assertEqual(d["battery_right_pct"], 90)
        self.assertEqual(d["battery_case_pct"], 20)

    def test_battery_unknown_nibble_is_none(self):
        # 0xF nibble means "unknown"
        battery_lr = (0xF << 4) | 0x3
        payload = (
            bytes([0x01])
            + bytes.fromhex("1420")    # model
            + bytes([0x55])            # status
            + bytes([battery_lr])      # left=unknown, right=30%
            + bytes(10)
        )
        blob = APPLE_CID_BE + bytes.fromhex("070f") + payload
        d = parse_continuity(blob)[0].decoded
        self.assertIsNone(d["battery_left_pct"])
        self.assertEqual(d["battery_right_pct"], 30)


class NearbyInfoDecoderTests(unittest.TestCase):

    def test_action_code_extracts_top_nibble(self):
        # Top nibble = action 0x4 (TVTransferAuthority), bottom = flags
        blob = APPLE_CID_BE + bytes.fromhex("1005") + bytes.fromhex("4a1cef2ecd")
        d = parse_continuity(blob)[0].decoded
        self.assertEqual(d["action_code"], 0x4)
        self.assertEqual(d["action_name"], "TVTransferAuthority")
        self.assertEqual(d["action_flags"], 0xA)

    def test_status_byte_flag_bits(self):
        # Status byte 0xC0 = WiFi on + AirPods connected
        blob = APPLE_CID_BE + bytes.fromhex("1005") + bytes.fromhex("00c0aabbcc")
        d = parse_continuity(blob)[0].decoded
        self.assertEqual(d["status_byte"], 0xC0)
        self.assertTrue(d["wifi_on"])
        self.assertTrue(d["airpods_connected"])
        self.assertFalse(d["authenticated"])

    def test_short_payload_decodes_action_only(self):
        # Just 1 byte — get action code, no status fields
        blob = APPLE_CID_BE + bytes.fromhex("1001") + bytes.fromhex("60")
        d = parse_continuity(blob)[0].decoded
        self.assertEqual(d["action_code"], 0x6)  # AutoUnlock
        self.assertNotIn("wifi_on", d)


class PairingDecoderTests(unittest.TestCase):

    def test_state_code_variant_2_bytes(self):
        blob = APPLE_CID_BE + bytes.fromhex("12020003")
        d = parse_continuity(blob)[0].decoded
        self.assertEqual(d["variant"], "state_code")
        self.assertEqual(d["state_code"], 0x0003)

    def test_find_my_anchor_variant_25_bytes(self):
        # 25-byte payload → Find-My anchor format
        blob = APPLE_CID_BE + bytes.fromhex("1219") + bytes(25)
        d = parse_continuity(blob)[0].decoded
        self.assertEqual(d["variant"], "find_my_anchor")
        self.assertNotIn("state_code", d)


class MultiTLVTests(unittest.TestCase):
    """Real-world: multiple TLVs packed into one mfg_data blob."""

    def test_handoff_plus_nearby_info(self):
        blob = bytes.fromhex(
            "4c00"
            "0c0e08e55a11b4b17dd224409c38608b"   # Handoff (14 bytes)
            "1005471c69ee65"                     # NearbyInfo (5 bytes)
        )
        tlvs = parse_continuity(blob)
        self.assertEqual([t.type for t in tlvs], [0x0C, 0x10])
        self.assertEqual(tlvs[0].type_name, "Handoff")
        self.assertEqual(tlvs[1].type_name, "NearbyInfo")
        # NearbyInfo decoded
        self.assertEqual(tlvs[1].decoded["action_code"], 0x4)


if __name__ == "__main__":
    unittest.main()
