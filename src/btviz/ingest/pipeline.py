"""Ingest pipeline: dissected packets → DB.

The per-packet ``record_packet()`` function is reused by:
  * file ingest (``ingest_file``) — drives packets via ``dissect_file()``
    + ``normalize()`` and writes the whole capture in a single tx.
  * live capture (``btviz.capture.live_ingest.LiveIngest``) — receives
    packets from the bus and flushes batches periodically.

Both paths share the same identity-enrichment, Auracast-extraction, and
observation-aggregation behavior because they share the same helper.

Design notes:
  * Every advertising address becomes a device row. Public / random-static
    MACs key on themselves; unresolved RPAs key on their current address
    (kind ``unresolved_rpa``); NRPAs key on their current address (kind
    ``nrpa``). IRK resolution later merges RPA-derived device rows into
    their true-identity rows.
  * The file path runs the whole capture in one tx. The live path
    batches per-flush. WAL mode makes either pattern fine for the
    expected packet rates.
  * Enrichment (local_name, vendor_id, appearance, OUI vendor) only fires
    when the current packet carries the relevant AD entries, so most
    hot-path packets skip the identity-merge UPDATE entirely.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..capture.packet import Packet

from ..db.repos import Repos
from ..db.store import Store
from ..decode.appearance import appearance_to_class
from ..decode.apple_continuity import classify as classify_apple, parse_continuity
from ..decode.auracast import parse_auracast
from ..vendors import company_vendor, oui_vendor
from .normalize import _as_int, _first, normalize
from .tshark import dissect_file

APPLE_COMPANY_ID = 0x004C

# device_class precedence — used to decide whether an incoming class clue
# from a single packet should override what we've already learned about a
# device. Higher = more specific. Without this, a device whose first packet
# revealed it's a Mac (Nearby action 0x0F) could later be re-labeled as
# generic "apple_device" by the next Nearby packet that lacked the
# distinguishing action code, ping-ponging the device_class column.
_CLASS_PRECEDENCE: dict[str | None, int] = {
    None: 0,
    "unknown": 0,
    "apple_device": 1,
    "apple_airplay": 2,
    "homekit": 2,
    "ibeacon": 2,
    "phone": 3,
    "computer": 3,
    "watch": 3,
    # Specific identities (Apple Continuity-derived or appearance-confirmed)
    "airpods": 5,
    "airtag": 5,
    "apple_watch": 5,
    "iphone": 5,
    "ipad": 5,
    "mac": 5,
    "hearing_aid": 5,
}


def _class_precedence(cls: str | None) -> int:
    """Specificity score for a device_class. Higher = more specific."""
    return _CLASS_PRECEDENCE.get(cls, 3)


# ──────────────────────────────────────────────────────────────────────────
# Reusable per-packet helper — shared by file and live ingest paths
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class IngestContext:
    """Per-session state that lives across packets within one ingest run.

    Both file ingest (single-tx, drains a generator) and live ingest
    (long-running, periodic flushes) instantiate one of these for the
    session they're recording into. ``record_packet`` reads/writes the
    device-state cache and the seen-id sets.
    """
    session_id: int
    seen_device_ids: set[int] = field(default_factory=set)
    seen_address_ids: set[int] = field(default_factory=set)
    seen_broadcast_ids: set[int] = field(default_factory=set)
    # Cache {device_id -> current identity fields} so we only UPDATE when
    # a new clue actually changes something.
    device_state: dict[int, dict[str, Any]] = field(default_factory=dict)
    # Diagnostics for the Auracast detection path. ext_adv_count is
    # every ADV_EXT_IND we saw (primary or AUX_ADV_IND — they share PDU
    # type 0x7); ext_adv_with_baa counts those where parse_auracast
    # found a Broadcast Audio Announcement Service entry. The gap
    # between the two distinguishes "didn't capture the AUX" (PHY /
    # firmware config issue) from "captured but parser didn't match"
    # (genuine bug to investigate).
    ext_adv_count: int = 0
    ext_adv_with_baa: int = 0
    # Set True to write a row to `packets` for every attributed packet.
    # Off by default: at 200 pkts/s that's ~17M rows/day.
    keep_packets: bool = False


def record_packet(repos: "Repos", ctx: IngestContext, pkt: Packet) -> "int | None":
    """Apply one packet's contribution to the DB.

    Returns the attributed ``device.id`` when the packet was recorded,
    or ``None`` when skipped (no advertising address, CRC failed,
    etc.). Truthy/falsy semantics are preserved — callers using
    ``if record_packet(...)`` work unchanged. Does NOT manage
    transactions — callers are responsible for tx scoping.

    CRC-failed packets are explicitly skipped here. The address bytes
    in a CRC-failed packet are corrupted — attributing them would
    spawn ghost device rows whose addresses sit 1-4 bits away from a
    real canonical advertiser. Live capture preserves the packet
    upstream so the sniffer panel can render a dropout flash, but it
    must NOT enter the DB-attribution path.

    Side effects on ``ctx``:
      * adds the device id to ``seen_device_ids``
      * adds the address id to ``seen_address_ids``
      * caches the device's current identity fields
      * adds Auracast broadcast ids when an ADV_EXT_IND with BAA service
        data is seen
    """
    if not pkt.crc_ok:
        return None
    if not pkt.adv_addr:
        return None

    stable_key, kind = _ingest_key(pkt.adv_addr, pkt.adv_addr_type)
    device = repos.devices.upsert(stable_key, kind, now=pkt.ts)
    ctx.seen_device_ids.add(device.id)

    addr = repos.addresses.upsert(
        pkt.adv_addr, pkt.adv_addr_type or "unknown",
        device.id, now=pkt.ts,
    )
    ctx.seen_address_ids.add(addr.id)

    # Identity enrichment: merge AD-derived clues, plus OUI vendor for
    # public MACs. Skip if nothing changes.
    clues = _extract_ad_clues(pkt.extras.get("layers", {}))
    state = ctx.device_state.get(device.id)
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
        ctx.device_state[device.id] = state

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

    # Precedence guard: don't downgrade a more-specific device_class.
    new_class = clues.get("device_class")
    if new_class is not None:
        if _class_precedence(new_class) <= _class_precedence(state.get("device_class")):
            clues.pop("device_class", None)

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

    # Auracast: if this packet carries a Broadcast Audio Announcement,
    # upsert a row in `broadcasts`. Only ADV_EXT_IND / AUX_ADV_IND packets
    # carry BAA, so cheap reject for the non-extended-adv hot path.
    if pkt.pdu_type == "ADV_EXT_IND":
        ctx.ext_adv_count += 1
        ai = parse_auracast(pkt.extras.get("layers", {}))
        if ai is not None:
            ctx.ext_adv_with_baa += 1
            repos.broadcasts.upsert(
                ctx.session_id, ai.broadcast_id,
                broadcaster_device_id=device.id,
                broadcast_name=ai.broadcast_name,
                bis_count=ai.bis_count,
                phy=ai.phy,
                encrypted=ai.encrypted,
                ts=pkt.ts,
            )
            ctx.seen_broadcast_ids.add(ai.broadcast_id)

    # Observation: per (session, device) aggregate.
    is_adv = (pkt.pdu_type in _ADV_PDU_TYPES) if pkt.pdu_type else True
    repos.observations.record_packet(
        ctx.session_id, device.id,
        ts=pkt.ts, is_adv=is_adv,
        rssi=pkt.rssi, channel=pkt.channel, phy=pkt.phy,
        pdu_type=pkt.pdu_type,
    )

    # AD vocabulary: upsert any new (device, ad_type, ad_value) tuples.
    ad_entries = _extract_ad_entries(pkt.extras.get("layers", {}))
    if ad_entries:
        repos.ad_history.upsert_many(device.id, ad_entries, pkt.ts)

    # Per-packet event row (opt-in — off by default due to volume).
    if ctx.keep_packets:
        pdu_int = _PDU_TYPE_INT.get(pkt.pdu_type, 0xFF)
        repos.packets.insert(
            session_id=ctx.session_id,
            device_id=device.id,
            address_id=addr.id,
            ts=pkt.ts,
            rssi=pkt.rssi or 0,
            channel=pkt.channel or 0,
            pdu_type=pdu_int,
        )

    return device.id


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


_PDU_TYPE_INT: dict[str | None, int] = {
    "ADV_IND": 0, "ADV_DIRECT_IND": 1, "ADV_NONCONN_IND": 2,
    "SCAN_REQ": 3, "SCAN_RSP": 4, "CONNECT_IND": 5,
    "ADV_SCAN_IND": 6, "ADV_EXT_IND": 7,
}


def _extract_ad_entries(layers: dict) -> list[tuple[int, bytes]]:
    """Extract (ad_type, ad_value_bytes) pairs from tshark EK layers.

    Returns canonical byte representations for the AD types that the
    cluster signals consume. Only entries that parse cleanly are returned;
    failures are silently dropped so a malformed field never kills an ingest.

    Supported types:
      0x09 (Complete Local Name)   → UTF-8 bytes
      0x02/0x03 (16-bit UUIDs)     → 2 bytes LE per UUID, one entry each
      0xFF (Manufacturer Specific) → company_id LE16 + data bytes
      0x0A (TX Power Level)        → 1-byte signed
      0x19 (Appearance)            → 2 bytes LE
    """
    import struct

    results: list[tuple[int, bytes]] = []

    def _lookup(key: str) -> Any:
        for layer in ("btle", "btcommon"):
            v = layers.get(layer, {}).get(key)
            if v is not None:
                return v
        return None

    # 0x09 Complete Local Name
    name_raw = _first(_lookup("btcommon_btcommon_eir_ad_entry_device_name"))
    if isinstance(name_raw, str) and name_raw:
        try:
            results.append((0x09, name_raw.encode("utf-8")))
        except Exception:
            pass

    # 0x02/0x03 16-bit Service UUIDs (tshark merges both types into one field)
    uuid16_raw = _lookup("btcommon_btcommon_eir_ad_entry_uuid_16")
    if uuid16_raw is not None:
        uuids = uuid16_raw if isinstance(uuid16_raw, list) else [uuid16_raw]
        for u in uuids:
            v = _as_int(u)
            if v is not None:
                try:
                    results.append((0x03, struct.pack("<H", v & 0xFFFF)))
                except Exception:
                    pass

    # 0xFF Manufacturer Specific: reconstruct company_id LE16 + data
    cid = _as_int(_lookup("btcommon_btcommon_eir_ad_entry_company_id"))
    data_raw = _first(_lookup("btcommon_btcommon_eir_ad_entry_data"))
    if cid is not None:
        data_bytes = _hexstr_to_bytes(data_raw) if isinstance(data_raw, str) else b""
        try:
            results.append((0xFF, struct.pack("<H", cid & 0xFFFF) + (data_bytes or b"")))
        except Exception:
            pass

    # 0x0A TX Power Level
    tx_raw = _as_int(_lookup("btcommon_btcommon_eir_ad_entry_power_level"))
    if tx_raw is not None:
        try:
            results.append((0x0A, struct.pack("b", max(-128, min(127, tx_raw)))))
        except Exception:
            pass

    # 0x19 Appearance
    appearance = _as_int(_lookup("btcommon_btcommon_eir_ad_entry_appearance"))
    if appearance is not None:
        try:
            results.append((0x19, struct.pack("<H", appearance & 0xFFFF)))
        except Exception:
            pass

    return results


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
    keep_packets: bool = False,
) -> IngestReport:
    """Ingest a pcap/pcapng file into the store under the named project."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    repos = Repos(store)
    t0 = time.monotonic()

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

        ctx = IngestContext(session_id=sess.id, keep_packets=keep_packets)

        for rec in dissect_file(path, keep_bad_crc=keep_bad_crc):
            packets_seen += 1
            pkt = normalize(rec, source=str(path))
            if pkt is None:
                continue
            if not pkt.adv_addr:
                packets_no_addr += 1
                continue
            if record_packet(repos, ctx, pkt):
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
        devices_touched=len(ctx.seen_device_ids),
        addresses_new=addresses_after - addresses_before,
        broadcasts_seen=len(ctx.seen_broadcast_ids),
        duration_s=time.monotonic() - t0,
    )
