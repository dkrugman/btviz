"""Convert a tshark EK record into a normalized ``Packet``.

EK field naming: tshark emits `protocol.sub.field` as
``layers[<proto>][<proto>_<proto>_<sub>_<field>]`` — the protocol name is
prefixed twice. Values are strings (occasionally lists for repeated fields
or booleans for flags). This module handles the quirks in one place so the
rest of btviz sees plain Python types.

For BLE dissected by tshark, the relevant layers are:
  - ``nordic_ble``  Nordic pseudo-header (channel, rssi, phy, flags)
  - ``btle_rf``     Generic BLE RF header (channel, signal_dbm) — may or
                    may not appear depending on linktype
  - ``btle``        Link-layer: pdu type, addresses, extended adv header,
                    link layer data (CONNECT_IND params)
  - ``btcommon``    Advertising data entries (device name, company id, …)

We pull a small, commonly-used set of fields into named attributes on
``Packet``. Everything else stays in ``Packet.extras["layers"]`` for later
passes (Auracast BASE parsing, decryption, etc.).
"""
from __future__ import annotations

from typing import Any

from ..capture.packet import Packet
from ..decode.adv import classify_address

# btle.advertising_header.pdu_type numeric values
_PDU_TYPE_NAMES = {
    0: "ADV_IND",
    1: "ADV_DIRECT_IND",
    2: "ADV_NONCONN_IND",
    3: "SCAN_REQ",
    4: "SCAN_RSP",
    5: "CONNECT_IND",
    6: "ADV_SCAN_IND",
    7: "ADV_EXT_IND",
}

# nordic_ble.phy / btle_rf.phy enumeration
_PHY_NAMES = {
    0: "1M",
    1: "2M",
    2: "Coded",
    3: "Coded",
}


def _first(v: Any) -> Any:
    """tshark EK wraps repeated fields as lists; unwrap single values."""
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
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(s, 0) if s.startswith(("0x", "0X")) else int(s)
    except ValueError:
        return None


def _as_str(v: Any) -> str | None:
    v = _first(v)
    if v is None:
        return None
    return str(v)


def _field(layers: dict, proto: str, field: str) -> Any:
    """Look up ``<proto>.<field>`` in the EK record. Returns None if missing.

    ``field`` uses underscores where the original tshark name has dots
    (e.g. ``advertising_header_pdu_type`` for ``btle.advertising_header.pdu_type``).
    """
    layer = layers.get(proto)
    if not isinstance(layer, dict):
        return None
    return layer.get(f"{proto}_{proto}_{field}")


def _pdu_name(raw: Any) -> str | None:
    """Map a tshark pdu_type value to a readable name. Accepts numeric
    strings and already-named strings."""
    if raw is None:
        return None
    s = _as_str(raw)
    if s is None:
        return None
    n = _as_int(s)
    if n is not None and n in _PDU_TYPE_NAMES:
        return _PDU_TYPE_NAMES[n]
    return s


def normalize(rec: dict, *, source: str = "file") -> Packet | None:
    """Convert one tshark EK record into a ``Packet``.

    Returns None if the record has no ``btle`` layer (i.e. not a BLE frame).
    Non-BLE frames should be filtered upstream via `-Y btle`, but we
    double-check here for defensive reasons.
    """
    layers = rec.get("layers")
    if not isinstance(layers, dict) or "btle" not in layers:
        return None

    # Timestamp: top-level "timestamp" in EK is epoch milliseconds as string.
    ts_ms = _as_int(rec.get("timestamp"))
    ts = (ts_ms / 1000.0) if ts_ms is not None else 0.0

    # Channel / RSSI / PHY: prefer nordic_ble, fall back to btle_rf.
    channel = _as_int(_field(layers, "nordic_ble", "channel"))
    if channel is None:
        channel = _as_int(_field(layers, "btle_rf", "channel"))

    rssi = _as_int(_field(layers, "nordic_ble", "rssi"))
    if rssi is None:
        rssi = _as_int(_field(layers, "btle_rf", "signal_dbm"))

    phy_raw = _as_int(_field(layers, "nordic_ble", "phy"))
    if phy_raw is None:
        phy_raw = _as_int(_field(layers, "btle_rf", "phy"))
    phy = _PHY_NAMES.get(phy_raw) if phy_raw is not None else None

    # PDU type and addresses from the BLE link layer.
    pdu_type = _pdu_name(_field(layers, "btle", "advertising_header_pdu_type"))
    tx_random = _as_int(_field(layers, "btle", "advertising_header_randomized_tx"))

    adv_addr = _as_str(_field(layers, "btle", "advertising_address"))
    init_addr = _as_str(_field(layers, "btle", "initiator_address"))
    scanning_addr = _as_str(_field(layers, "btle", "scanning_address"))
    target_addr = _as_str(_field(layers, "btle", "target_address"))

    # For SCAN_REQ et al. the peer is the scanner; for directed adv it's target.
    peer_addr = target_addr or scanning_addr

    addr_type = None
    if adv_addr and tx_random is not None:
        addr_type = classify_address(adv_addr.lower(), random=bool(tx_random))

    return Packet(
        ts=ts,
        source=source,
        channel=channel,
        rssi=rssi,
        phy=phy,
        pdu_type=pdu_type,
        adv_addr=adv_addr.lower() if adv_addr else None,
        adv_addr_type=addr_type,
        init_addr=init_addr.lower() if init_addr else None,
        target_addr=peer_addr.lower() if peer_addr else None,
        adv_data=None,
        raw=b"",
        extras={"layers": layers},
    )
