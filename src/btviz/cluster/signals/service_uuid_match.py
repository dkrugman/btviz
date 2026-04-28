"""service_uuid_match signal.

Compares the set of 16-bit Bluetooth service UUIDs each device has
ever advertised (from device_ad_history, ad_type 0x03). A shared UUID
is weak evidence of same device (many devices share common UUIDs like
0x180A Device Information); a *mismatched* set is stronger evidence of
different devices.

Scoring:
  - Abstains when either device has no UUID history (data sparse).
  - Returns 0.0 when both devices share no UUIDs in common (no signal,
    not a mismatch — absence of evidence is not evidence of absence for
    UUID sets, since a device may have rotated before all its services
    were captured).
  - Returns positive score proportional to Jaccard similarity of UUID sets.
  - Returns -0.5 when one device has a distinctive UUID (rare in the
    population) that the other explicitly lacks from a rich history.
    "Distinctive" = present in fewer than `rare_threshold` devices total
    (default 3). This requires a population query and is skipped when
    `ctx.db` is None.
"""

from __future__ import annotations

import struct
from typing import Any, Mapping

from ..base import ClusterContext, Device

AD_TYPE_UUID16 = 0x03


def _uuid16_set(db, device_id: int) -> set[int] | None:
    """Return the set of 16-bit UUIDs seen for device_id, or None if empty."""
    rows = db.execute(
        "SELECT ad_value FROM device_ad_history"
        " WHERE device_id = ? AND ad_type = ?",
        (device_id, AD_TYPE_UUID16),
    ).fetchall()
    if not rows:
        return None
    uuids: set[int] = set()
    for r in rows:
        blob = r[0] if isinstance(r, (tuple, list)) else r["ad_value"]
        if len(blob) >= 2:
            uuids.add(struct.unpack_from("<H", blob)[0])
    return uuids or None


class ServiceUuidMatch:
    name = "service_uuid_match"

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
        rare_threshold: int = int(params.get("rare_threshold", 3))

        uuids_a = _uuid16_set(ctx.db.conn, a.id)
        uuids_b = _uuid16_set(ctx.db.conn, b.id)

        if uuids_a is None or uuids_b is None:
            return None

        intersection = uuids_a & uuids_b
        union = uuids_a | uuids_b

        if not union:
            return None

        jaccard = len(intersection) / len(union)

        # Distinctive-UUID mismatch: one device has a rare UUID the other
        # doesn't. Requires a population count query — skip if DB unavailable.
        only_a = uuids_a - uuids_b
        only_b = uuids_b - uuids_a
        for uuid_val in (only_a | only_b):
            # Count how many distinct devices have advertised this UUID.
            blob = struct.pack("<H", uuid_val)
            row = ctx.db.conn.execute(
                "SELECT COUNT(DISTINCT device_id) FROM device_ad_history"
                " WHERE ad_type = ? AND ad_value = ?",
                (AD_TYPE_UUID16, blob),
            ).fetchone()
            population = row[0] if row else 0
            if population <= rare_threshold:
                return -0.5

        return round(jaccard, 4)
