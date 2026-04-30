"""Passive intelligence-gathering across captured advertising data.

Mines the existing ``device_ad_history`` table to extract human-
readable device information that wasn't visible to the live UI:
specific Apple Pods/Beats models, iPhone activity-state histograms,
AirPlay sources, Find-My emitters, Auracast broadcasters, vendor
distributions across non-Apple manufacturer-data, and likely
RPA-collapse candidates that the cluster runner hasn't merged yet.

This module is **read-only** and does NO active probing — no GATT
reads, no L2CAP connection requests, no scan-response solicitation.
Every value here comes from passive advertising captures already in
the DB.

Public API:

    inventory(store)              -> structured findings
    print_report(store, out=...)  -> render findings as text

The findings dataclass is designed for both display and downstream
analysis (e.g. as input to a clustering signal that wants to know
"is this NearbyInfo state byte rare or common across the
population?").
"""

from __future__ import annotations

import struct
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .signals._continuity_protocol import (
    AIRPODS_MODEL_BY_BYTES,
    APPLE_CID_BE,
    NEARBY_INFO_ACTIONS,
    parse_continuity,
)

AD_TYPE_FLAGS = 0x01
AD_TYPE_UUID16_INCOMPLETE = 0x02
AD_TYPE_UUID16_COMPLETE = 0x03
AD_TYPE_LOCAL_NAME_SHORT = 0x08
AD_TYPE_LOCAL_NAME_COMPLETE = 0x09
AD_TYPE_TX_POWER = 0x0A
AD_TYPE_SERVICE_DATA_16 = 0x16
AD_TYPE_APPEARANCE = 0x19
AD_TYPE_MFG = 0xFF

# Bluetooth SIG company-id lookup for the heaviest-hitting non-Apple
# vendors. Big-endian on-the-wire (little-endian as decoded). Kept
# minimal — exhaustive list lives at the SIG. Add as needed when a
# new vendor shows up in the dataset and we want it labeled.
COMPANY_ID_NAMES: dict[int, str] = {
    0x004C: "Apple, Inc.",
    0x0006: "Microsoft",
    0x0075: "Samsung Electronics",
    0x00E0: "Google",
    0x00D2: "LAIRD Technologies",
    0x000F: "Broadcom",
    0x008A: "Bose",
    0x0087: "Garmin International",
    0x00B5: "TZ-Mobile / Sonos",
    0x027D: "Avantree",
    0x019A: "Tile",
    0x07A8: "Intel Corporate",
}

# 16-bit service UUIDs that identify specific BLE 5.x audio profiles —
# the ones a clustering signal would want to recognize as Auracast/LE
# Audio markers.
AUDIO_UUIDS_16: dict[int, str] = {
    0x1843: "Audio Input Control Service (AICS)",
    0x1844: "Volume Offset Control (VOCS)",
    0x1845: "Audio Output Control (AOCS)",
    0x1846: "Volume Control (VCS)",
    0x1847: "Audio Input Service",
    0x1848: "Microphone Control",
    0x1849: "Generic Telephony Bearer",
    0x184A: "Telephony and Media Audio Profile",
    0x184B: "Common Audio Service (CAS)",
    0x184C: "Hearing Access (HAS)",
    0x184D: "Tone-Map Service (TMAS)",
    0x184E: "Audio Stream Control Service (ASCS)",
    0x184F: "Broadcast Audio Scan Service (BASS)",
    0x1850: "Published Audio Capabilities (PACS)",
    0x1852: "Broadcast Audio Scan",
    0x1853: "Broadcast Audio Announcement Service (BAAS)",
    0x1854: "Common Audio Service",
    0x1855: "Hearing Aid Service",
    0x1856: "Broadcast Audio Source",
    # Below: not LE Audio but commonly seen near it
    0xFE2C: "Google Cast Service",
    0xFE0F: "Philips Hue",
    0xFD6F: "Apple Find My",
    0xFEED: "Tile",
}


# ──────────────────────────────────────────────────────────────────────────
# Findings dataclass
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class Inventory:
    total_devices: int = 0
    devices_with_continuity: int = 0
    apple_continuity_type_counts: Counter[str] = field(default_factory=Counter)
    apple_continuity_devs_per_type: dict[str, set[int]] = field(default_factory=dict)
    airpods_models: Counter[str] = field(default_factory=Counter)
    airpods_devs_per_model: dict[str, set[int]] = field(default_factory=dict)
    nearby_actions: Counter[str] = field(default_factory=Counter)
    nearby_devs_per_action: dict[str, set[int]] = field(default_factory=dict)
    findmy_emitters: list[int] = field(default_factory=list)
    airplay_emitters: list[int] = field(default_factory=list)
    auracast_emitters: list[int] = field(default_factory=list)
    vendors: Counter[str] = field(default_factory=Counter)
    vendor_devs: dict[str, set[int]] = field(default_factory=dict)
    audio_service_emitters: dict[str, set[int]] = field(default_factory=dict)
    rpa_pair_candidates: list[tuple[int, int, int]] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────
# Mining
# ──────────────────────────────────────────────────────────────────────────

def inventory(store) -> Inventory:
    """Compute the full inventory from the live DB."""
    inv = Inventory()
    conn = store.conn

    inv.total_devices = conn.execute(
        "SELECT COUNT(*) FROM devices"
    ).fetchone()[0]

    # Per-device Continuity TLV parsing — keep results in memory keyed by
    # device id so we can also feed the rpa_pair_candidate detector.
    device_payloads: dict[int, list[bytes]] = defaultdict(list)
    rows = conn.execute(
        "SELECT device_id, ad_value, count FROM device_ad_history"
        " WHERE ad_type = ?",
        (AD_TYPE_MFG,),
    ).fetchall()
    for r in rows:
        blob = bytes(r["ad_value"])
        device_payloads[r["device_id"]].append(blob)

        # Vendor histogram — full mfg_data CID lookup.
        if len(blob) >= 2:
            cid = struct.unpack_from("<H", blob)[0]
            vendor = COMPANY_ID_NAMES.get(cid, f"CID 0x{cid:04X}")
            inv.vendors[vendor] += r["count"]
            inv.vendor_devs.setdefault(vendor, set()).add(r["device_id"])

    inv.devices_with_continuity = sum(
        1 for blobs in device_payloads.values()
        if any(b[:2] == APPLE_CID_BE for b in blobs)
    )

    # Per-type Continuity histogram + AirPods model decode.
    long_payload_index: dict[bytes, set[int]] = defaultdict(set)
    for did, blobs in device_payloads.items():
        seen_types_this_dev: set[int] = set()
        seen_models_this_dev: set[str] = set()
        seen_actions_this_dev: set[str] = set()
        for blob in blobs:
            for tlv in parse_continuity(blob):
                seen_types_this_dev.add(tlv.type)
                inv.apple_continuity_type_counts[tlv.type_name] += 1
                inv.apple_continuity_devs_per_type.setdefault(
                    tlv.type_name, set()
                ).add(did)

                if tlv.type == 0x07:
                    model_bytes = tlv.decoded.get("model_bytes")
                    model_name = tlv.decoded.get("model_name", "unknown")
                    if model_bytes:
                        key = f"{model_name} ({model_bytes})"
                        seen_models_this_dev.add(key)

                if tlv.type == 0x10:
                    action_name = tlv.decoded.get("action_name")
                    if action_name:
                        seen_actions_this_dev.add(action_name)

                if tlv.type == 0x12:
                    if tlv.decoded.get("variant") == "find_my_anchor":
                        if did not in inv.findmy_emitters:
                            inv.findmy_emitters.append(did)

                if tlv.type == 0x09:
                    if did not in inv.airplay_emitters:
                        inv.airplay_emitters.append(did)

                # Long payloads → RPA-collapse fingerprints.
                if len(tlv.payload) >= 8:
                    long_payload_index[tlv.payload].add(did)

        for m in seen_models_this_dev:
            inv.airpods_models[m] += 1
            inv.airpods_devs_per_model.setdefault(m, set()).add(did)
        for a in seen_actions_this_dev:
            inv.nearby_actions[a] += 1
            inv.nearby_devs_per_action.setdefault(a, set()).add(did)

    # RPA-collapse candidates: pairs of devices that share at least one
    # long Continuity payload. The apple_continuity signal already merges
    # these; surfacing here lets us cross-check coverage and find
    # under-merged clusters.
    pair_share_counts: Counter[tuple[int, int]] = Counter()
    for payload, dev_set in long_payload_index.items():
        if len(dev_set) >= 2:
            devs = sorted(dev_set)
            for i in range(len(devs)):
                for j in range(i + 1, len(devs)):
                    pair_share_counts[(devs[i], devs[j])] += 1
    inv.rpa_pair_candidates = [
        (a, b, n) for (a, b), n in pair_share_counts.most_common(20)
    ]

    # Audio-service UUID emitters (Auracast / LE Audio markers).
    uuid_rows = conn.execute(
        "SELECT device_id, ad_value FROM device_ad_history"
        " WHERE ad_type IN (?, ?)",
        (AD_TYPE_UUID16_INCOMPLETE, AD_TYPE_UUID16_COMPLETE),
    ).fetchall()
    for r in uuid_rows:
        blob = bytes(r["ad_value"])
        if len(blob) >= 2:
            uuid = struct.unpack_from("<H", blob)[0]
            label = AUDIO_UUIDS_16.get(uuid)
            if label is None:
                continue
            inv.audio_service_emitters.setdefault(label, set()).add(
                r["device_id"]
            )
            # Anything with BASS / BAAS / Source / Sink is an Auracast
            # candidate — surface those devices specifically.
            if any(
                k in label for k in ("Broadcast", "Auracast", "BASS", "BAAS")
            ):
                if r["device_id"] not in inv.auracast_emitters:
                    inv.auracast_emitters.append(r["device_id"])

    return inv


# ──────────────────────────────────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────────────────────────────────

def print_report(store, *, out=None) -> None:
    """Render the inventory as human-readable text on ``out``."""
    out = out or sys.stdout
    inv = inventory(store)

    def section(title: str) -> None:
        print(file=out)
        print("─" * 70, file=out)
        print(title, file=out)
        print("─" * 70, file=out)

    section("Coverage")
    print(f"  total devices in DB:        {inv.total_devices:>6,}", file=out)
    print(f"  with Apple Continuity:      {inv.devices_with_continuity:>6,}", file=out)
    print(f"  Find-My emitters:           {len(inv.findmy_emitters):>6,}", file=out)
    print(f"  AirPlay-source emitters:    {len(inv.airplay_emitters):>6,}", file=out)
    print(f"  Auracast emitters:          {len(inv.auracast_emitters):>6,}", file=out)

    section("Apple Continuity types observed")
    width = max((len(t) for t in inv.apple_continuity_type_counts), default=10)
    for type_name, count in inv.apple_continuity_type_counts.most_common():
        n_devs = len(inv.apple_continuity_devs_per_type.get(type_name, ()))
        print(
            f"  {type_name:<{width}}  TLVs={count:>9,}  distinct devs={n_devs:>5,}",
            file=out,
        )

    section("AirPods / Beats model inventory (from type 0x07)")
    if not inv.airpods_models:
        print("  (none observed)", file=out)
    else:
        width = max(len(k) for k in inv.airpods_models)
        for model, dev_count in inv.airpods_models.most_common():
            print(
                f"  {model:<{width}}  distinct devs={dev_count:>4,}",
                file=out,
            )

    section("NearbyInfo action histogram (top 4-bit nibble)")
    if not inv.nearby_actions:
        print("  (none observed)", file=out)
    else:
        width = max(len(a) for a in inv.nearby_actions)
        for action, dev_count in inv.nearby_actions.most_common():
            n_devs = len(inv.nearby_devs_per_action.get(action, ()))
            print(
                f"  {action:<{width}}  distinct devs={n_devs:>4,}",
                file=out,
            )

    section("Vendor distribution (mfg_data CID)")
    width = max((len(v) for v in inv.vendors), default=20)
    for vendor, total in inv.vendors.most_common(15):
        n_devs = len(inv.vendor_devs.get(vendor, ()))
        print(
            f"  {vendor:<{width}}  obs={total:>9,}  distinct devs={n_devs:>5,}",
            file=out,
        )

    section("Audio service emitters (LE Audio + Auracast UUIDs)")
    if not inv.audio_service_emitters:
        print("  (none observed)", file=out)
    else:
        width = max(len(k) for k in inv.audio_service_emitters)
        for label, dev_set in sorted(
            inv.audio_service_emitters.items(),
            key=lambda kv: -len(kv[1]),
        ):
            print(
                f"  {label:<{width}}  distinct devs={len(dev_set):>4,}",
                file=out,
            )

    section("Top RPA-collapse candidates (shared long Continuity payloads)")
    if not inv.rpa_pair_candidates:
        print("  (none — apple_continuity signal has full coverage)", file=out)
    else:
        for a, b, shared in inv.rpa_pair_candidates:
            print(
                f"  device_{a}  ↔  device_{b}    shared long payloads = {shared}",
                file=out,
            )


if __name__ == "__main__":
    from btviz.db.store import open_store
    store = open_store()
    print_report(store)
