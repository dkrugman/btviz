"""App-wide configuration and defaults."""
from __future__ import annotations

from pathlib import Path

# Primary BLE advertising channels.
PRIMARY_ADV_CHANNELS: tuple[int, ...] = (37, 38, 39)

# Where to look for the Nordic nRF Sniffer extcap binary on each platform.
# Order matters: first existing path wins. Override with $BTVIZ_NRF_EXTCAP.
NRF_EXTCAP_CANDIDATE_PATHS: tuple[str, ...] = (
    # macOS (Wireshark.app bundle)
    "/Applications/Wireshark.app/Contents/MacOS/extcap/nrf_sniffer_ble.sh",
    "/Applications/Wireshark.app/Contents/MacOS/extcap/nrf_sniffer_ble.py",
    # Linux
    "/usr/lib/x86_64-linux-gnu/wireshark/extcap/nrf_sniffer_ble.sh",
    "/usr/local/lib/wireshark/extcap/nrf_sniffer_ble.sh",
    # Per-user (Wireshark "Personal Extcap path")
    str(Path.home() / ".local/lib/wireshark/extcap/nrf_sniffer_ble.sh"),
    str(Path.home() / ".config/wireshark/extcap/nrf_sniffer_ble.sh"),
)

# How long a device can be silent before we mark it idle (seconds).
DEVICE_IDLE_AFTER_S: float = 30.0
# How long before we drop it from the live view (still in DB).
DEVICE_STALE_AFTER_S: float = 300.0
