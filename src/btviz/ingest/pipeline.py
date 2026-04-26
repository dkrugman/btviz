"""File ingest pipeline: pcap/pcapng → tshark → normalize → DB.

Invoked by the ``btviz ingest`` CLI. Writes devices, addresses, session
observations, and AD-structure-derived identity clues (name, vendor,
appearance) into the configured store.

Design notes:
  * Every advertising address becomes a device row. Public / random-static
    MACs key on themselves; unresolved RPAs key on their current address
    (kind ``unresolved_rpa``); NRPAs key on their current address (kind
    ``nrpa``). IRK resolution later merges RPA-derived device rows into
    their true-identity rows.
  * The whole ingest runs in a single transaction. WAL mode makes this
    fine for 10k–100k packets; only revisit if we start ingesting hours
    of live traffic in one pass.
  * Enrichment (local_name, vendor_id, appearance, OUI vendor) only fires
    when the current packet carries the relevant AD entries, so most
    hot-path packets skip the identity-merge UPDATE entirely.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..db.repos import Repos
from ..db.store import Store
from ..decode.appearance import appearance_to_class
from ..decode.apple_continuity import classify as classify_apple, parse_continuity
from ..decode.auracast import parse_auracast
from ..vendors import company_vendor, oui_vendor
from .normalize import normalize
from .tshark import dissect_file

APPLE_COMPANY_ID = 0x004C


@dataclass
class IngestReport:
    project_id: int
    project_name: str
    session_id: int
    path: str
    packets_seen: int           # records yielded by tshark
    packets_recorded: int       # attributed to a device (observation written)
    packets_no_addr: int        # no adv_addr, not attributable (data-channel etc.)
    devices_new: int
    devices_touched: int        # distinct devices contributed to this session
    addresses_new: int
    broadcasts_seen: int        # distinct Auracast broadcasts in this session
    duration_s: float

    def format(self) -> str:
        return (
            f"ingested {self.path}\n"
            f"  project:        {self.project_name} (id {self.project_id})\n"
            f"  session:        id {self.session_id}\n"
            f"  packets seen:   {self.packets_seen}\n"
            f"  packets kept:   {self.packets_recorded} "
            f"({self.packets_no_addr} had no adv_addr)\n"
            f"  devices:        {self.devices_touched} touched "
            f"({self.devices_new} new)\n"
            f"  addresses new:  {self.addresses_new}\n"
            f"  broadcasts:     {self.broadcasts_seen} Auracast\n"
            f"  duration:       {self.duration_s:.1f}s"
        )


def _ingest_key(address: str, address_type: str | None) -> tuple[str, str]:
    """Return (stable_key, kind) for any observed address.

    Public and random-static MACs use the stable schemes from
    ``Devices.stable_key_for``. RPAs and NRPAs get provisional keys — each
    unique address becomes its own device row until IRK resolution merges
    RPAs into true identities.
    """
    addr = address.lower()
    if address_type == "public":
        return (f"pub:{addr}", "public_mac")
    if address_type == "random_static":
        return (f"rs:{addr}", "random_static_mac")
    if address_type == "rpa":
        return (f"rpa:{addr}", "unresolved_rpa")
    if address_type == "nrpa":
        return (f"nrpa:{addr}", "nrpa")
    return (f"anon:{addr}", "unknown")


# PDU types that are ADV_* link-layer advertising (as opposed to data-channel).
_ADV_PDU_TYPES = frozenset({
    "ADV_IND", "ADV_DIRECT_IND", "ADV_NONCONN_IND",
    "ADV_SCAN_IND", "ADV_EXT_IND",
    "SCAN_REQ", "SCAN_RSP", "CONNECT_IND",
})


def _first(v: Any) -> Any:
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _as_int(v: Any) -> int | None:
    v = _first(v)
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    try:
        s = str(v).strip()
        return int(s, 0) if s.startswith(("0x", "0X")) else int(s)
    except (ValueError, AttributeError):
        return None


def _hexstr_to_bytes(s: str) -> bytes | None:
    """Parse tshark's colon-or-space-separated hex (e.g. ``'10:06:43'``).

    Returns None on any parse failure. Defensive — never raises.
    """
    if not isinstance(s, str) or not s:
        return None
    try:
        return bytes.fromhex(s.replace(":", "").replace(" ", ""))
    except ValueError:
        return None


def _extract_ad_clues(layers: dict) -> dict[str, Any]:
    """Pull identity clues from AD entries.

    tshark's EK output nests the btcommon AD fields inside the ``btle`` layer
    (keyed ``btcommon_btcommon_eir_ad_entry_*``) rather than as a sibling
    layer. We check both the ``btle`` layer and any separate ``btcommon``
    layer for resilience across tshark versions.

    Returns a dict with any of: local_name, vendor_id, appearance,
    device_class, model. Apple-vendor packets get an extra Continuity
    decode pass that may set device_class (airpods/airtag/apple_watch/…)
    and model (e.g. "AirPods Pro (2nd gen)"). Continuity parsing failures
    don't propagate — at worst we just don't add those clues.
    """
    if not isinstance(layers, dict):
        return {}

    # Fields we want, indexed by their EK key (with the btcommon prefix tshark
    # uses even when they appear inside other layers).
    NAME_KEY = "btcommon_btcommon_eir_ad_entry_device_name"
    CID_KEY = "btcommon_btcommon_eir_ad_entry_company_id"
    APPEAR_KEY = "btcommon_btcommon_eir_ad_entry_appearance"
    MFG_DATA_KEY = "btcommon_btcommon_eir_ad_entry_data"

    def _lookup(key: str) -> Any:
        for layer in ("btle", "btcommon"):
            v = layers.get(layer, {}).get(key)
            if v is not None:
                return v
        return None

    clues: dict[str, Any] = {}

    name = _first(_lookup(NAME_KEY))
    if isinstance(name, str) and name:
        clues["local_name"] = name

    cid = _as_int(_lookup(CID_KEY))
    if cid is not None:
        clues["vendor_id"] = cid

    appearance = _as_int(_lookup(APPEAR_KEY))
    if appearance is not None:
        clues["appearance"] = appearance

    # Apple Continuity: derive device_class / model from sub-type bytes.
    if cid == APPLE_COMPANY_ID:
        data_bytes = _hexstr_to_bytes(_first(_lookup(MFG_DATA_KEY)))
        if data_bytes:
            entries = parse_continuity(data_bytes)
            device_class, model = classify_apple(entries)
            if device_class:
                clues["device_class"] = device_class
            if model:
                clues["model"] = model

    return clues


def ingest_file(
    path: str | Path,
    store: Store,
    *,
    project: str,
    session_name: str | None = None,
    keep_bad_crc: bool = False,
) -> IngestReport:
    """Ingest a pcap/pcapng file into the store under the named project."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    repos = Repos(store)
    t0 = time.monotonic()

    # State that tracks uniqueness / caches identity across packets.
    seen_device_ids: set[int] = set()
    seen_address_ids: set[int] = set()
    seen_broadcast_ids: set[int] = set()
    # Cache {device_id -> current identity fields} so we only UPDATE when
    # a new clue actually changes something.
    device_state: dict[int, dict[str, Any]] = {}

    packets_seen = 0
    packets_recorded = 0
    packets_no_addr = 0
    devices_new_before = 0

    with store.tx():
        proj = repos.projects.get_by_name(project) or repos.projects.create(project)
        sess = repos.sessions.start(
            proj.id, "file", source_path=str(path), name=session_name
        )

        # Snapshot the pre-existing device count so we can report how many are new.
        devices_new_before = store.conn.execute(
            "SELECT COUNT(*) AS n FROM devices"
        ).fetchone()["n"]
        addresses_before = store.conn.execute(
            "SELECT COUNT(*) AS n FROM addresses"
        ).fetchone()["n"]

        for rec in dissect_file(path, keep_bad_crc=keep_bad_crc):
            packets_seen += 1
            pkt = normalize(rec, source=str(path))
            if pkt is None:
                continue
            if not pkt.adv_addr:
                packets_no_addr += 1
                continue

            stable_key, kind = _ingest_key(pkt.adv_addr, pkt.adv_addr_type)
            device = repos.devices.upsert(stable_key, kind, now=pkt.ts)
            seen_device_ids.add(device.id)

            addr = repos.addresses.upsert(
                pkt.adv_addr, pkt.adv_addr_type or "unknown",
                device.id, now=pkt.ts,
            )
            seen_address_ids.add(addr.id)

            # Identity enrichment: merge AD-derived clues, plus OUI vendor for
            # public MACs. Skip if nothing changes.
            clues = _extract_ad_clues(pkt.extras.get("layers", {}))
            state = device_state.get(device.id)
            if state is None:
                state = {
                    "local_name": device.local_name,
                    "vendor_id": device.vendor_id,
                    "vendor": device.vendor,
                    "oui_vendor": device.oui_vendor,
                    "appearance": device.appearance,
                    "device_class": device.device_class,
                    "model": device.model,
                }
                # One-shot OUI lookup per device (public MACs only).
                if kind == "public_mac" and state["oui_vendor"] is None:
                    ouiv = oui_vendor(pkt.adv_addr)
                    if ouiv:
                        clues.setdefault("oui_vendor", ouiv)
                device_state[device.id] = state

            # Appearance → device_class fallback. Fires only when the more
            # specific Continuity classifier didn't already set a class on
            # this packet AND the device has no class in the DB yet, so a
            # narrow class (e.g. airpods) is never downgraded by a generic
            # category (e.g. media_player) seen later.
            if (
                "device_class" not in clues
                and clues.get("appearance") is not None
                and not state.get("device_class")
            ):
                cls = appearance_to_class(clues["appearance"])
                if cls:
                    clues["device_class"] = cls

            updates: dict[str, Any] = {}
            for k, v in clues.items():
                if v is not None and v != state.get(k):
                    updates[k] = v
            # Derive vendor name from vendor_id if we just learned one.
            if "vendor_id" in updates and state.get("vendor") is None:
                vname = company_vendor(updates["vendor_id"])
                if vname:
                    updates["vendor"] = vname

            if updates:
                repos.devices.merge_identity(device.id, **updates)
                state.update(updates)

            # Auracast: if this packet carries a Broadcast Audio Announcement
            # (BAA service data), upsert a row in `broadcasts`. Only ADV_EXT_
            # IND / AUX_ADV_IND packets carry BAA, so cheap reject for the
            # non-extended-adv hot path.
            if pkt.pdu_type == "ADV_EXT_IND":
                ai = parse_auracast(pkt.extras.get("layers", {}))
                if ai is not None:
                    repos.broadcasts.upsert(
                        sess.id, ai.broadcast_id,
                        broadcaster_device_id=device.id,
                        broadcast_name=ai.broadcast_name,
                        bis_count=ai.bis_count,
                        phy=ai.phy,
                        encrypted=ai.encrypted,
                        ts=pkt.ts,
                    )
                    seen_broadcast_ids.add(ai.broadcast_id)

            # Observation: per (session, device) aggregate.
            is_adv = (pkt.pdu_type in _ADV_PDU_TYPES) if pkt.pdu_type else True
            repos.observations.record_packet(
                sess.id, device.id,
                ts=pkt.ts, is_adv=is_adv,
                rssi=pkt.rssi, channel=pkt.channel, phy=pkt.phy,
                pdu_type=pkt.pdu_type,
            )
            packets_recorded += 1

        repos.sessions.end(sess.id)
        repos.projects.touch(proj.id)

        devices_after = store.conn.execute(
            "SELECT COUNT(*) AS n FROM devices"
        ).fetchone()["n"]
        addresses_after = store.conn.execute(
            "SELECT COUNT(*) AS n FROM addresses"
        ).fetchone()["n"]

    return IngestReport(
        project_id=proj.id,
        project_name=proj.name,
        session_id=sess.id,
        path=str(path),
        packets_seen=packets_seen,
        packets_recorded=packets_recorded,
        packets_no_addr=packets_no_addr,
        devices_new=devices_after - devices_new_before,
        devices_touched=len(seen_device_ids),
        addresses_new=addresses_after - addresses_before,
        broadcasts_seen=len(seen_broadcast_ids),
        duration_s=time.monotonic() - t0,
    )
