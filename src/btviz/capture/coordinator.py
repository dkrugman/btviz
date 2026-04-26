"""CaptureCoordinator: owns N SnifferProcess instances and their roles.

A role is the user's intent for one sniffer (see `roles.py`):
  Idle, Pinned(channels), ScanUnmonitored, Follow(addr, random)

set_role() translates roles into concrete sniffer actions (start/stop,
set_adv_hop, follow_address) and, after any change, recomputes the hop
set for every ScanUnmonitored sniffer so gaps in primary-channel
coverage are filled automatically.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..bus import (
    EventBus,
    TOPIC_DONGLES_CHANGED,
    TOPIC_PACKET,
    TOPIC_SNIFFER_STATE,
)
from ..extcap import Dongle, list_dongles
from ..extcap.sniffer import RawPacket, SnifferProcess, SnifferState
from .packet import Packet
from .roles import (
    PRIMARY_ADV_CHANNELS,
    Follow,
    Idle,
    Pinned,
    ScanUnmonitored,
    SnifferRole,
    default_roles,
)


@dataclass
class FollowRequest:
    target_addr: str
    is_random: bool = False
    prefer_dongle: str | None = None


class CaptureCoordinator:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.dongles: list[Dongle] = []
        self.sniffers: dict[str, SnifferProcess] = {}     # short_id -> sniffer
        self.roles: dict[str, SnifferRole] = {}           # short_id -> current role

    # --- discovery -------------------------------------------------------

    def refresh_dongles(self) -> list[Dongle]:
        self.dongles = list_dongles()
        self.bus.publish(TOPIC_DONGLES_CHANGED, list(self.dongles))
        return self.dongles

    # --- start / stop ----------------------------------------------------

    def start_discover(self) -> None:
        """Register sniffers for all known dongles and apply default roles."""
        if not self.dongles:
            self.refresh_dongles()
        for dongle in self.dongles:
            if dongle.short_id in self.sniffers:
                continue
            sp = SnifferProcess(
                dongle=dongle,
                on_packet=self._handle_raw,
                on_state=self._handle_state,
            )
            self.sniffers[dongle.short_id] = sp
            self.roles[dongle.short_id] = Idle()  # placeholder before plan

        plan = default_roles([d.short_id for d in self.dongles])
        # Apply pinned/follow roles first, then ScanUnmonitored (so the
        # recompute sees a stable pinned set).
        for did, role in plan.items():
            if not isinstance(role, ScanUnmonitored):
                self.set_role(did, role)
        for did, role in plan.items():
            if isinstance(role, ScanUnmonitored):
                self.set_role(did, role)

    def stop_all(self) -> None:
        for sp in list(self.sniffers.values()):
            sp.stop()
        self.sniffers.clear()
        self.roles.clear()

    # --- roles -----------------------------------------------------------

    def get_role(self, dongle_id: str) -> SnifferRole:
        return self.roles.get(dongle_id, Idle())

    def set_role(self, dongle_id: str, role: SnifferRole) -> None:
        """Change one sniffer's role and retune the rest as needed."""
        if dongle_id not in self.sniffers:
            raise KeyError(f"unknown dongle: {dongle_id}")
        self.roles[dongle_id] = role
        self._apply_role(dongle_id, role)
        self._recompute_scan_unmonitored()

    def _apply_role(self, dongle_id: str, role: SnifferRole) -> None:
        sp = self.sniffers[dongle_id]
        if isinstance(role, Idle):
            if sp.state.running:
                sp.stop()
            return
        if isinstance(role, Pinned):
            channels = list(role.channels)
            if sp.state.running:
                sp.set_adv_hop(channels)
            else:
                sp.start(adv_hop=channels)
            return
        if isinstance(role, Follow):
            if sp.state.running:
                sp.follow_address(role.target_addr, role.is_random)
            else:
                sp.start(follow_address=(role.target_addr, role.is_random))
            # IRK feed for RPA resolution. Sent AFTER the follow_address
            # call so the sniffer's key state is already on "Follow LE
            # address"; add_irk overrides the key-type to IRK and writes
            # the value. Stripping any 0x prefix to match add_irk's
            # internal validator (which expects 32 raw hex chars).
            if role.irk_hex:
                sp.add_irk(role.irk_hex.lower().removeprefix("0x"))
            return
        # ScanUnmonitored: handled in _recompute_scan_unmonitored()
        return

    def _recompute_scan_unmonitored(self) -> None:
        """Update every ScanUnmonitored sniffer to hop the uncovered primaries."""
        pinned_channels: set[int] = set()
        for did, role in self.roles.items():
            if isinstance(role, Pinned):
                pinned_channels.update(role.channels)
        uncovered = [c for c in PRIMARY_ADV_CHANNELS if c not in pinned_channels]

        for did, role in self.roles.items():
            if not isinstance(role, ScanUnmonitored):
                continue
            sp = self.sniffers[did]
            if not uncovered:
                # All primaries pinned elsewhere; idle this sniffer but keep
                # its role (it will automatically resume if gaps appear).
                if sp.state.running:
                    sp.stop()
                continue
            if sp.state.running:
                if list(sp.state.adv_hop) != uncovered:
                    sp.set_adv_hop(uncovered)
            else:
                sp.start(adv_hop=uncovered)

    # --- follow convenience ---------------------------------------------

    def follow(self, req: FollowRequest) -> str | None:
        """Assign a Follow role to a chosen or picked dongle."""
        dongle_id = req.prefer_dongle or self._pick_follow_dongle()
        if dongle_id is None:
            return None
        self.set_role(dongle_id, Follow(req.target_addr, req.is_random))
        return dongle_id

    def _pick_follow_dongle(self) -> str | None:
        """Prefer an Idle dongle; else a ScanUnmonitored one; else any non-follow."""
        for role_type in (Idle, ScanUnmonitored):
            for did, role in self.roles.items():
                if isinstance(role, role_type):
                    return did
        for did, role in self.roles.items():
            if not isinstance(role, Follow):
                return did
        return None

    # --- internal callbacks ---------------------------------------------

    def _handle_raw(self, dongle: Dongle, raw: RawPacket) -> None:
        pkt = Packet(ts=raw.ts, source=raw.source, raw=raw.data)
        self.bus.publish(TOPIC_PACKET, pkt)

    def _handle_state(self, state: SnifferState) -> None:
        self.bus.publish(TOPIC_SNIFFER_STATE, state)
