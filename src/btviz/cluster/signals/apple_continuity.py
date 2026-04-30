"""apple_continuity signal.

Uses Apple Continuity Protocol TLVs (parsed by
``_continuity_protocol``) as a clustering fingerprint. Two devices
that share a long payload are near-certainly the same physical
advertiser captured under different addresses (RPA collapse); two
devices that share only TLV types are likely the same Apple class
(both iPhones, both AirPods, etc.) but possibly different units.

Scoring rationale:
  * Long payloads (>= 8 bytes) embed encrypted session keys,
    sequence numbers, or device-specific stable bytes that an
    unrelated device is extremely unlikely to emit by coincidence.
  * Short payloads (e.g. 0x12 with a 2-byte state code) are state
    enums shared by many devices of the same model — too generic to
    fingerprint as same-device.
  * Disjoint Continuity vocabularies = different roles entirely
    (e.g. iPhone vs AirPods); we score that mildly negative.

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
from ._continuity_protocol import APPLE_CID_BE, parse_continuity

AD_TYPE_MFG = 0xFF

# Re-export for backward compatibility with the v1 test suite — older
# tests imported _parse_continuity_tlvs directly.
def _parse_continuity_tlvs(blob: bytes) -> list[tuple[int, bytes]]:
    """Backward-compat shim returning (type, payload) tuples.

    Internal callers should use ``parse_continuity`` from
    ``_continuity_protocol`` directly to get richly-decoded TLVs;
    this wrapper is kept so external tooling that imported the v1
    private helper keeps working.
    """
    return [(tlv.type, tlv.payload) for tlv in parse_continuity(blob)]


# Minimum payload length (in bytes) for a TLV to count as a "fingerprint"
# match. Payloads shorter than this are state enums shared across many
# devices of the same model; matching them yields false positives.
DEFAULT_MIN_FINGERPRINT_BYTES = 8


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
        for tlv in parse_continuity(bytes(blob)):
            types.add(tlv.type)
            if len(tlv.payload) >= min_fingerprint_bytes:
                payloads.add((tlv.type, tlv.payload))
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
