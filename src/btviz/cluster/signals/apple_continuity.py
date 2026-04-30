"""apple_continuity signal.

Parses Apple Continuity Protocol TLVs out of each device's
manufacturer-specific advertising data (AD type 0xFF, company id
0x004C) and uses **shared TLV payloads** as a clustering fingerprint.

The Continuity Protocol uses a TLV format embedded inside Apple's
manufacturer data:

    [0x4C, 0x00,        # Apple company id (little-endian 0x004C)
     <type><length><payload> ...repeating...]

Common types observed in this dataset:

  0x07  ProximityPairing  AirPods + Beats (model + battery + lid)
  0x09  AirPlaySource     speakers / Apple TV
  0x0A  AirPlayTarget
  0x0C  Handoff           encrypted, rotates with the user's session
  0x0D  TetheringTarget
  0x0F  NearbyAction      "Tap to set up"
  0x10  NearbyInfo        action-type + status flags + auth tag
  0x11  FindMyDevice      AirTags
  0x12  Pairing           short state code OR longer pairing data

Scoring rationale:
  * Long payloads (>= 8 bytes) embed encrypted session keys, sequence
    numbers, or device-specific bytes that an unrelated device is
    extremely unlikely to emit by coincidence. So a *byte-for-byte*
    payload match between two devices is near-certainty same physical
    advertiser captured under different addresses (RPA collapse).
  * Short payloads (e.g. 0x12 with a 2-byte state code) are state
    enums shared by many devices of the same model — too generic to
    fingerprint. Ignored.
  * Two devices with disjoint Continuity type sets are *probably*
    different categories of Apple device (e.g. iPhone vs AirPods).
    We score this case mildly negative; the aggregator caller can
    decide whether to weight that further.

Output domain:
  * None    one or both devices have no Continuity history → abstain
  *  1.0    at least one long-payload exact match → strong same-device
  *  0.4    common types but no exact long-payload match → weak same-class
  *  0.0    only short-state-code overlap → no useful signal
  * -0.3    completely disjoint type sets → mild negative
"""

from __future__ import annotations

from typing import Any, Mapping

from ..base import ClusterContext, Device

AD_TYPE_MFG = 0xFF
APPLE_CID_BE = b"\x4c\x00"   # little-endian 0x004C as on-the-wire bytes

# Minimum payload length (in bytes) for a TLV to count as a "fingerprint"
# match. Payloads shorter than this are state enums shared across many
# devices of the same model; matching them yields false positives.
DEFAULT_MIN_FINGERPRINT_BYTES = 8


def _parse_continuity_tlvs(blob: bytes) -> list[tuple[int, bytes]]:
    """Pull (type, payload) tuples out of one mfg_data blob.

    Returns an empty list when the CID isn't Apple, the blob is too
    short, or a length byte runs past end-of-blob (truncated capture).
    """
    if len(blob) < 4 or blob[:2] != APPLE_CID_BE:
        return []
    out: list[tuple[int, bytes]] = []
    i = 2
    n = len(blob)
    while i + 1 < n:
        t = blob[i]
        length = blob[i + 1]
        payload_start = i + 2
        payload_end = payload_start + length
        if payload_end > n:
            # Truncated TLV — accept what's there only if length>0; some
            # captures cut off mid-payload. Keep parsing what we can.
            break
        out.append((t, bytes(blob[payload_start:payload_end])))
        i = payload_end
    return out


def _device_fingerprints(
    db, device_id: int, min_fingerprint_bytes: int,
) -> tuple[set[tuple[int, bytes]], set[int]] | None:
    """Build two fingerprints for one device:

      * ``payloads`` — the set of (type, payload) tuples whose payload
        is at least ``min_fingerprint_bytes`` long. Used for exact
        same-device matching.
      * ``types``    — every type observed at all, regardless of
        payload length. Used for the weaker "same Apple class"
        score.

    Returns ``None`` when the device has no Apple Continuity entries
    in ``device_ad_history`` — caller treats as abstain.
    """
    rows = db.execute(
        "SELECT ad_value FROM device_ad_history"
        " WHERE device_id = ? AND ad_type = ?",
        (device_id, AD_TYPE_MFG),
    ).fetchall()
    if not rows:
        return None
    payloads: set[tuple[int, bytes]] = set()
    types: set[int] = set()
    saw_continuity = False
    for r in rows:
        blob = r[0] if isinstance(r, (tuple, list)) else r["ad_value"]
        if not blob or len(blob) < 4 or blob[:2] != APPLE_CID_BE:
            continue
        saw_continuity = True
        for t, pl in _parse_continuity_tlvs(bytes(blob)):
            types.add(t)
            if len(pl) >= min_fingerprint_bytes:
                payloads.add((t, pl))
    if not saw_continuity:
        return None
    return payloads, types


class AppleContinuity:
    """Cluster signal: Apple Continuity TLV-payload fingerprint match."""

    name = "apple_continuity"

    def applies_to(self, ctx: ClusterContext, a: Device, b: Device) -> bool:
        return ctx.db is not None

    def score(
        self,
        ctx: ClusterContext,
        a: Device,
        b: Device,
        params: Mapping[str, Any] | None = None,
    ) -> float | None:
        if ctx.db is None:
            return None
        params = params or {}
        min_bytes: int = int(params.get(
            "min_fingerprint_bytes", DEFAULT_MIN_FINGERPRINT_BYTES,
        ))

        a_fp = _device_fingerprints(ctx.db.conn, a.id, min_bytes)
        if a_fp is None:
            return None
        b_fp = _device_fingerprints(ctx.db.conn, b.id, min_bytes)
        if b_fp is None:
            return None

        a_payloads, a_types = a_fp
        b_payloads, b_types = b_fp

        # Exact-payload match on a long TLV → near-certainty same device.
        if a_payloads & b_payloads:
            return 1.0

        # Type sets share something but no long-payload match.
        common_types = a_types & b_types
        if common_types:
            # Soft positive: same Apple class. Scaled by Jaccard so a
            # "we both broadcast NearbyInfo" pair scores lower than a
            # "we both broadcast NearbyInfo + Handoff + AirPlay" pair.
            union = a_types | b_types
            jaccard = len(common_types) / len(union)
            return round(0.4 * jaccard, 4)

        # Disjoint Continuity vocabularies = different roles entirely
        # (e.g. one is broadcasting AirPods Pairing, the other only
        # NearbyInfo). Mild negative so the aggregator's weighted sum
        # sees opposing evidence rather than zero.
        return -0.3
