"""Vendor lookups for MAC OUIs and Bluetooth SIG company identifiers.

Two independent mappings:
  * ``oui_vendor(mac)``      -- uses Wireshark's ``tshark -G manuf`` table
                                when tshark is on PATH; otherwise returns None.
  * ``company_vendor(cid)``  -- uses the bundled Bluetooth SIG company_identifiers
                                JSON (from bluetooth.com public registry).

Both lookups are lazy (loaded on first call) and cached for the process.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

# Extension length -> number of hex nibbles compared (including colons),
# for indexing into the padded MAC string.
_MANUF_PREFIX_BITS = (24, 28, 36)


# --- Bluetooth SIG company identifiers -------------------------------------

_COMPANY_JSON = Path(__file__).with_name("data") / "company_identifiers.json"


@lru_cache(maxsize=1)
def _load_company_map() -> dict[int, str]:
    if not _COMPANY_JSON.exists():
        return {}
    raw = json.loads(_COMPANY_JSON.read_text())
    return {int(k): v for k, v in raw.items()}


def company_vendor(company_id: int) -> str | None:
    """Return vendor name for a Bluetooth SIG 16-bit Company Identifier."""
    return _load_company_map().get(company_id)


# --- MAC OUI from Wireshark's tshark ---------------------------------------

# Known install locations for tshark on each platform.
_TSHARK_CANDIDATES = (
    "/Applications/Wireshark.app/Contents/MacOS/tshark",
    "/usr/bin/tshark",
    "/usr/local/bin/tshark",
    "/opt/homebrew/bin/tshark",
)


def _find_tshark() -> str | None:
    which = shutil.which("tshark")
    if which:
        return which
    for p in _TSHARK_CANDIDATES:
        if Path(p).exists():
            return p
    return None


# Parsed representation: a dict keyed by (prefix_bits, normalized_prefix_hex).
# normalized_prefix_hex is the upper `prefix_bits/4` hex nibbles of the MAC,
# uppercase, no colons.
@lru_cache(maxsize=1)
def _load_oui_map() -> dict[tuple[int, str], str]:
    tshark = _find_tshark()
    if tshark is None:
        return {}
    try:
        out = subprocess.check_output(
            [tshark, "-G", "manuf"],
            stderr=subprocess.DEVNULL,
            timeout=15,
            text=True,
        )
    except (subprocess.SubprocessError, OSError):
        return {}

    table: dict[tuple[int, str], str] = {}
    for line in out.splitlines():
        # Each line: "<prefix>[/<bits>]  <short>  <long>" separated by tabs.
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        prefix_field = parts[0].strip()
        # Prefer the long name (3rd column), fall back to short name.
        vendor = (parts[2].strip() if len(parts) >= 3 and parts[2].strip()
                  else parts[1].strip())
        if not prefix_field or not vendor:
            continue

        if "/" in prefix_field:
            mac_part, bits_part = prefix_field.split("/", 1)
            try:
                bits = int(bits_part)
            except ValueError:
                continue
        else:
            mac_part, bits = prefix_field, 24

        hex_only = mac_part.replace(":", "").upper()
        nibbles = bits // 4
        if len(hex_only) < nibbles:
            continue
        key = (bits, hex_only[:nibbles])
        # First occurrence wins (registry is ordered and most-specific entries
        # are unique by prefix).
        table.setdefault(key, vendor)
    return table


_MAC_RE = re.compile(r"[0-9A-Fa-f]{2}")


def _normalize_mac(mac: str) -> str | None:
    """Strip separators and uppercase; return 12 hex chars or None."""
    hex_only = "".join(_MAC_RE.findall(mac))
    if len(hex_only) != 12:
        return None
    return hex_only.upper()


def oui_vendor(mac: str) -> str | None:
    """Return vendor for a MAC address via Wireshark's manuf table.

    Looks up /36, /28, /24 prefixes in that order (most-specific wins).
    """
    norm = _normalize_mac(mac)
    if norm is None:
        return None
    table = _load_oui_map()
    if not table:
        return None
    # /36 first (most specific), then /28, then /24.
    for bits in (36, 28, 24):
        nibbles = bits // 4
        key = (bits, norm[:nibbles])
        vendor = table.get(key)
        if vendor:
            return vendor
    return None


# --- utility ---------------------------------------------------------------

def have_tshark() -> bool:
    """True if tshark is findable on this system (OUI lookups will work)."""
    return _find_tshark() is not None
