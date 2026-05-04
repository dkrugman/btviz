"""Row dataclasses for the btviz database.

These mirror the tables in schema.sql. `id` is None on unsaved rows.
Fields with SQL defaults may be 0.0 / None until the row is read back.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# --- identity --------------------------------------------------------------

@dataclass
class Device:
    id: int | None
    stable_key: str                      # "pub:<mac>" | "rs:<mac>" | "irk:<hex>"
    kind: str                            # public_mac | random_static_mac | irk_identity

    # User override wins over everything automatic.
    user_name: str | None = None

    # Names seen on the wire.
    local_name: str | None = None        # adv Complete/Shortened Local Name
    gatt_device_name: str | None = None  # GATT Device Name characteristic

    # Vendor / class / model clues.
    vendor: str | None = None
    vendor_id: int | None = None         # Bluetooth SIG company id
    oui_vendor: str | None = None        # from public MAC OUI
    model: str | None = None
    device_class: str | None = None

    appearance: int | None = None        # GAP Appearance uint16

    # Open-ended identifiers: serial_number, firmware_rev, hardware_rev,
    # manufacturer_name_string, apple_continuity_type, etc.
    identifiers: dict[str, str] = field(default_factory=dict)

    notes: str | None = None
    first_seen: float = 0.0
    last_seen: float = 0.0
    created_at: float = 0.0

    def best_label(self) -> str:
        """Best human-readable name given the evidence currently collected."""
        if self.user_name:
            return self.user_name
        if self.gatt_device_name:
            return self.gatt_device_name
        if self.local_name:
            return self.local_name
        vendor = self.vendor or self.oui_vendor
        if vendor and self.model:
            return f"{vendor} {self.model}"
        if vendor and self.device_class:
            return f"{vendor} {self.device_class}"
        if vendor:
            return vendor
        # Fall back to the stable key, trimmed of its prefix.
        if self.stable_key.startswith("pub:"):
            return self.stable_key[4:]
        if self.stable_key.startswith("rs:"):
            return self.stable_key[3:]
        if self.stable_key.startswith("irk:"):
            return f"irk-{self.stable_key[4:12]}"
        return self.stable_key


@dataclass
class Address:
    id: int | None
    address: str
    address_type: str                    # public | random_static | rpa | nrpa
    device_id: int | None = None
    resolved_via_irk_id: int | None = None
    first_seen: float = 0.0
    last_seen: float = 0.0


# --- projects & sessions ---------------------------------------------------

@dataclass
class Project:
    id: int | None
    name: str
    description: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class Session:
    id: int | None
    project_id: int
    source_type: str                     # live | file
    started_at: float
    name: str | None = None
    source_path: str | None = None
    ended_at: float | None = None
    notes: str | None = None


@dataclass
class Observation:
    session_id: int
    device_id: int
    packet_count: int = 0
    adv_count: int = 0
    data_count: int = 0
    rssi_min: int | None = None
    rssi_max: int | None = None
    rssi_sum: int = 0
    rssi_samples: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    pdu_types: dict[str, int] = field(default_factory=dict)
    channels: dict[int, int] = field(default_factory=dict)
    phys: dict[str, int] = field(default_factory=dict)


# --- canvas ----------------------------------------------------------------

@dataclass
class Group:
    id: int | None
    project_id: int
    name: str
    parent_group_id: int | None = None
    color: str | None = None
    collapsed: bool = False
    pos_x: float = 0.0
    pos_y: float = 0.0
    width: float | None = None
    height: float | None = None
    z_order: int = 0


@dataclass
class DeviceLayout:
    project_id: int
    device_id: int
    pos_x: float = 0.0
    pos_y: float = 0.0
    collapsed: bool = True
    hidden: bool = False
    z_order: int = 0


@dataclass
class DeviceProjectMeta:
    project_id: int
    device_id: int
    label: str | None = None
    color: str | None = None
    tags: list[str] = field(default_factory=list)
    notes: str | None = None


@dataclass
class CanvasState:
    project_id: int
    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0
    last_opened_at: float | None = None


# --- keys ------------------------------------------------------------------

@dataclass
class IRK:
    id: int | None
    project_id: int
    key_hex: str                         # 32 hex chars
    label: str | None = None
    device_id: int | None = None
    notes: str | None = None
    created_at: float = 0.0


@dataclass
class LTK:
    id: int | None
    key_hex: str
    ediv: int | None = None
    rand_hex: str | None = None
    label: str | None = None
    device_a_id: int | None = None
    device_b_id: int | None = None
    notes: str | None = None
    created_at: float = 0.0


# --- observed topology -----------------------------------------------------

@dataclass
class Connection:
    id: int | None
    session_id: int
    access_address: int
    started_at: float
    central_device_id: int | None = None
    peripheral_device_id: int | None = None
    ended_at: float | None = None
    interval_us: int | None = None
    latency: int | None = None
    timeout_ms: int | None = None


@dataclass
class Broadcast:
    id: int | None
    session_id: int
    first_seen: float
    last_seen: float
    broadcaster_device_id: int | None = None
    broadcast_id: int | None = None
    broadcast_name: str | None = None
    big_handle: int | None = None
    bis_count: int | None = None
    phy: str | None = None
    encrypted: bool = False


@dataclass
class BroadcastReceiver:
    broadcast_id: int
    device_id: int
    first_seen: float
    last_seen: float
    packets_received: int = 0
    packets_lost: int = 0
    rssi_avg: float | None = None


# --- RPA collapse / cluster framework -------------------------------------

@dataclass
class DeviceAdHistory:
    device_id:  int
    ad_type:    int      # BLE AD type byte
    ad_value:   bytes    # raw payload
    first_seen: float
    last_seen:  float
    count:      int = 1


@dataclass
class Packet:
    id:         int | None
    session_id: int
    device_id:  int
    address_id: int
    ts:         float
    rssi:       int
    channel:    int
    pdu_type:   int
    sniffer_id: int | None = None
    raw:        bytes | None = None


@dataclass
class DeviceCluster:
    id:              int | None
    created_at:      float
    last_decided_at: float
    source:          str = "auto"   # 'auto' | 'manual' | 'irk'
    label:           str | None = None


@dataclass
class DeviceClusterMember:
    cluster_id:    int
    device_id:     int
    decided_at:    float
    score:         float | None = None
    contributions: dict | None = None  # {signal: [score, weight]}
    profile:       str | None = None
    decided_by:    str = "auto"


# --- physical sniffers -----------------------------------------------------

@dataclass
class Sniffer:
    """A capture dongle / DK known to btviz.

    Persisted across launches so the canvas always shows the same fleet
    even when hardware isn't currently plugged in. ``is_active`` tracks
    whether discovery saw it most recently; ``removed`` is the user's
    "hide this" flag.
    """
    id: int | None
    serial_number: str
    kind: str = "unknown"               # dongle | dk | unknown
    name: str | None = None
    usb_port_id: str | None = None      # /dev/cu.usbmodem... etc.
    location_id_hex: str | None = None  # USB physical-port id (sort key)
    interface_id: str | None = None     # extcap interface value
    display: str | None = None          # extcap display string
    usb_product: str | None = None      # USB Product Name descriptor
    is_active: bool = False
    removed: bool = False
    first_seen: float = 0.0
    last_seen: float = 0.0
    notes: str | None = None

    @property
    def is_tx_capable(self) -> bool:
        """True if the firmware on this device can transmit (TX/RX),
        False if it's an RX-only sniffer firmware.

        Derived from the firmware's identifying strings rather than
        the hardware kind — the same nRF52840 chip is RX-only when
        running Nordic's nRF Sniffer for BLE firmware and TX-capable
        when running connectivity / SoftDevice / custom firmware.
        Heuristic: the substring "sniffer" appearing in either the
        extcap display string or the USB product descriptor implies
        the sniffer firmware is loaded; anything else is assumed
        TX-capable.

        See ``btviz.capture.capability.is_firmware_tx_capable``.
        """
        from ..capture.capability import is_firmware_tx_capable
        return is_firmware_tx_capable(self.usb_product, self.display)
