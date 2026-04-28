"""mfg_data_prefix signal.

Compares the manufacturer-specific data (AD type 0xFF) each device has
advertised. The on-wire format stored in device_ad_history is:

    [company_id_lo, company_id_hi, payload...]

as written by ingest/pipeline._extract_ad_entries().

Scoring:
  - Abstains when either device has no 0xFF entries.
  - Abstains when the two devices have different company IDs (different
    manufacturer — not a mismatch, just inapplicable).
  - For matching company IDs: compares the first `prefix_len` bytes of
    payload (default 4). An exact prefix match scores 1.0; a mismatch
    scores 0.0 (absence, not negative — mfg payload formats vary widely
    and a non-match is not diagnostic).
  - When a device has multiple 0xFF entries (rare but possible), any
    pair that matches scores 1.0.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..base import ClusterContext, Device

AD_TYPE_MFG = 0xFF


def _mfg_entries(db, device_id: int) -> list[bytes]:
    """Return all raw manufacturer data blobs for device_id."""
    rows = db.execute(
        "SELECT ad_value FROM device_ad_history"
        " WHERE device_id = ? AND ad_type = ?",
        (device_id, AD_TYPE_MFG),
    ).fetchall()
    result = []
    for r in rows:
        blob = r[0] if isinstance(r, (tuple, list)) else r["ad_value"]
        if len(blob) >= 2:
            result.append(bytes(blob))
    return result


class MfgDataPrefix:
    name = "mfg_data_prefix"

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
        prefix_len: int = int(params.get("prefix_len", 4))

        entries_a = _mfg_entries(ctx.db.conn, a.id)
        entries_b = _mfg_entries(ctx.db.conn, b.id)

        if not entries_a or not entries_b:
            return None

        # Try every (a_entry, b_entry) pair — any match is sufficient.
        for ea in entries_a:
            cid_a = ea[:2]
            for eb in entries_b:
                cid_b = eb[:2]
                if cid_a != cid_b:
                    continue  # different company — inapplicable, not a mismatch
                # Same company. Compare payload prefix.
                pay_a = ea[2:2 + prefix_len]
                pay_b = eb[2:2 + prefix_len]
                if pay_a and pay_b and pay_a == pay_b:
                    return 1.0

        # All same-company pairs had mismatched prefixes, or no same-company
        # pairs existed. Return 0.0 (no signal) rather than None so the
        # aggregator counts this as a contributing signal with zero score.
        same_company = any(
            ea[:2] == eb[:2] for ea in entries_a for eb in entries_b
        )
        return 0.0 if same_company else None
