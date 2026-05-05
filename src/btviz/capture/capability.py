"""Sniffer-firmware capability detection.

The same nRF52840 hardware is RX-only when running Nordic's "nRF Sniffer
for Bluetooth LE" firmware and TX-capable when running connectivity /
SoftDevice / custom Zephyr firmware. Capability is therefore a property
of the *firmware*, not the chip — and we have to infer it from whatever
the firmware identifies itself as during discovery.

The most reliable signal we currently get is the extcap display string
and the USB product descriptor. Nordic's sniffer firmware reports
itself as "nRF Sniffer for Bluetooth LE COM<n>" (or similar) on every
platform we care about. A case-insensitive substring match for
``"sniffer"`` covers every shipped variant.

Anything that isn't sniffer firmware is assumed TX-capable. This is a
deliberate over-approximation: a brand new firmware we've never seen
will be treated as TX-capable. The downside (a TX command sent to a
firmware that can't handle it) is a no-op the user notices; the
opposite default (assume RX-only) would silently exclude TX-capable
hardware from active probing roles, which is harder to debug.
"""
from __future__ import annotations


_SNIFFER_FIRMWARE_HINT = "sniffer"


def is_firmware_tx_capable(
    usb_product: str | None,
    display: str | None,
) -> bool:
    """Return True iff the firmware likely exposes a TX path.

    ``usb_product`` is the USB Product Name descriptor; ``display`` is
    the extcap display string. Either may be ``None``; both are checked
    case-insensitively for the sniffer-firmware substring.
    """
    blob = f"{usb_product or ''} {display or ''}".lower()
    return _SNIFFER_FIRMWARE_HINT not in blob
