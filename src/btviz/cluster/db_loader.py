"""Hydrate ``cluster.base.Device`` instances from the SQLite DB.

The cluster framework's Device type is intentionally narrower than the
DB-side ``db.models.Device``: it carries only what the signals consume
(id, device_class, address, first_seen, last_seen, label). This module
reads from the live store and produces those, picking the most recently
observed address per device so RPA-rotating devices show their current
identity rather than an arbitrary stale one.

Devices without ``device_class`` are filtered out — the cluster
runner's ``pick_profile`` would skip them anyway, and dropping them
early keeps the candidate-pair count down. A ``recent_window`` cutoff
is also applied so a long-running DB doesn't drag thousands of stale
rows into every periodic tick.
"""
from __future__ import annotations

import time

from .base import Address, Device

# DB ``addresses.address_type`` → ``cluster.base.Address.kind``. The
# cluster framework names track the BLE spec wording; the DB uses the
# shorter "rpa"/"nrpa" forms historically. Anything not in this map
# falls through as the raw value (best-effort).
_ADDR_KIND_MAP = {
    "public":         "public",
    "random_static":  "random_static",
    "rpa":            "random_resolvable",
    "nrpa":           "random_non_resolvable",
}


def load_devices(
    store,
    *,
    recent_window_s: float | None = 300.0,
    now: float | None = None,
    require_class: bool = True,
) -> list[Device]:
    """Read devices from ``store`` and return them as cluster.Device.

    ``recent_window_s`` filters by ``last_seen`` so stale RPAs don't
    blow up the O(n²) candidate-pair count. Pass ``None`` to disable.

    ``now`` defaults to wall-clock time; tests can pin it.

    ``require_class`` drops devices with ``device_class IS NULL`` —
    they can't be matched against a profile anyway. Pass ``False`` to
    keep them (e.g. to count what's available).
    """
    cutoff = None
    if recent_window_s is not None:
        cutoff = (now if now is not None else time.time()) - recent_window_s

    where: list[str] = []
    params: list[object] = []
    if cutoff is not None:
        where.append("last_seen >= ?")
        params.append(cutoff)
    if require_class:
        where.append("device_class IS NOT NULL AND device_class != ''")
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    rows = store.conn.execute(
        f"SELECT id, device_class, user_name, local_name, vendor, model, "
        f"first_seen, last_seen FROM devices{clause}",
        params,
    ).fetchall()
    if not rows:
        return []

    # Bulk-load latest address per device in one pass — cheaper than
    # one-query-per-device. ``MAX(last_seen)`` plus a self-join would
    # be cleaner but Python-side aggregation is fast enough at the
    # device counts we care about (low thousands).
    addr_rows = store.conn.execute(
        "SELECT device_id, address, address_type, resolved_via_irk_id, "
        "last_seen FROM addresses WHERE device_id IS NOT NULL "
        "ORDER BY last_seen DESC"
    ).fetchall()
    latest_addr: dict[int, dict] = {}
    for ar in addr_rows:
        did = ar["device_id"]
        if did not in latest_addr:
            latest_addr[did] = dict(ar)

    out: list[Device] = []
    for r in rows:
        ar = latest_addr.get(r["id"])
        if ar is None:
            continue  # device has no address rows yet — nothing to compare
        try:
            addr_bytes = bytes.fromhex(ar["address"].replace(":", ""))
        except (AttributeError, ValueError):
            continue
        kind = _ADDR_KIND_MAP.get(ar["address_type"], ar["address_type"])
        out.append(Device(
            id=r["id"],
            device_class=r["device_class"],
            address=Address(
                bytes_=addr_bytes,
                kind=kind,
                resolved_via_irk_id=ar["resolved_via_irk_id"],
            ),
            first_seen=r["first_seen"],
            last_seen=r["last_seen"],
            label=r["user_name"] or r["local_name"]
                  or (f"{r['vendor']} {r['model']}".strip() if r["vendor"] or r["model"] else None),
        ))
    return out
