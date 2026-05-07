"""Query the firmware version of a Nordic nRF Sniffer dongle.

Used by the preferences dialog to detect Coded-PHY-incompatible firmware
versions and surface the warning before the user flips the toggle. The
protocol is the same one ``nrf_sniffer_ble.py`` uses over USB CDC; we
re-implement the minimal subset (SLIP framing + ``REQ_VERSION`` →
``RESP_VERSION``) here so we can probe without spawning the extcap.

Why direct query instead of scraping the extcap:
  * The extcap binary opens the serial port exclusively, so we can't
    sniff its stderr while it's running. Probing from outside means we
    can do it before any capture starts (and again any time prefs is
    opened).
  * The extcap's startup output format isn't part of any stable
    contract, so parsing it would break across Nordic releases.

Constants are taken verbatim from Nordic's ``SnifferAPI`` (Apache-style
license; see Packet.py + Types.py in their repo). The `--coded` flag
silently breaks capture on firmware 4.1.1 — see
``btviz/preferences/ui.py`` for where this lookup is consumed.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import serial

log = logging.getLogger(__name__)

# --- Nordic SnifferAPI protocol constants ---------------------------------
# From Nordic's SnifferAPI/Types.py and Packet.py.
SLIP_START      = 0xAB
SLIP_END        = 0xBC
SLIP_ESC        = 0xCD
SLIP_ESC_START  = SLIP_START + 1   # 0xAC
SLIP_ESC_END    = SLIP_END   + 1   # 0xBD
SLIP_ESC_ESC    = SLIP_ESC   + 1   # 0xCE

PROTOVER_V1 = 1
HEADER_LENGTH = 6

PING_REQ      = 0x0D
PING_RESP     = 0x0E
REQ_VERSION   = 0x1B
RESP_VERSION  = 0x1C

# Header-byte positions inside the unwrapped (post-SLIP) packet.
ID_POS      = 5   # HEADER_LEN, payload_len, protover, ctr_lo, ctr_hi, id
PAYLOAD_POS = 6   # version string starts immediately after the id byte

# Nordic's official baudrates, in priority order.
SNIFFER_BAUDRATES = (1_000_000, 460_800)

# Per-dongle query budget. The extcap typically gets a response within
# ~10ms; 500ms is generous and lets us tolerate an OS scheduler hiccup
# or a sniffer that's still booting after a recent replug.
DEFAULT_QUERY_TIMEOUT_S = 0.5


def _slip_encode(payload: bytes) -> bytes:
    """SLIP-frame ``payload`` per Nordic's variant of the protocol.

    Three escape pairs are honored: SLIP_START → ESC + ESC_START,
    SLIP_END → ESC + ESC_END, SLIP_ESC → ESC + ESC_ESC. Everything
    else passes through. Frame is wrapped in SLIP_START / SLIP_END.
    """
    out = bytearray([SLIP_START])
    for b in payload:
        if b == SLIP_START:
            out += bytes([SLIP_ESC, SLIP_ESC_START])
        elif b == SLIP_END:
            out += bytes([SLIP_ESC, SLIP_ESC_END])
        elif b == SLIP_ESC:
            out += bytes([SLIP_ESC, SLIP_ESC_ESC])
        else:
            out.append(b)
    out.append(SLIP_END)
    return bytes(out)


def _slip_decode(frame: bytes) -> bytes:
    """Reverse of ``_slip_encode``. ``frame`` may include the start/end
    bytes; they're stripped. Malformed escape sequences pass through as
    a single byte rather than raising — version queries are best-effort.
    """
    body = frame
    if body.startswith(bytes([SLIP_START])):
        body = body[1:]
    if body.endswith(bytes([SLIP_END])):
        body = body[:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        b = body[i]
        if b == SLIP_ESC and i + 1 < len(body):
            n = body[i + 1]
            if n == SLIP_ESC_START:
                out.append(SLIP_START)
            elif n == SLIP_ESC_END:
                out.append(SLIP_END)
            elif n == SLIP_ESC_ESC:
                out.append(SLIP_ESC)
            else:
                out.append(n)
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def _build_request(packet_id: int, counter: int = 0) -> bytes:
    """Build a SLIP-encoded host→firmware request with empty payload.

    Header layout (pre-SLIP):
        [HEADER_LEN][payload_len][PROTOVER_V1][ctr_lo][ctr_hi][id]
    """
    body = bytes([
        HEADER_LENGTH,
        0,                       # payload length (no payload for VERSION/PING)
        PROTOVER_V1,
        counter & 0xFF,
        (counter >> 8) & 0xFF,
        packet_id,
    ])
    return _slip_encode(body)


def parse_version_response(payload: bytes) -> str | None:
    """Extract the version string from a decoded RESP_VERSION packet body.

    Expected layout (post-SLIP-decode):
        [HEADER_LEN][payload_len][protover][ctr_lo][ctr_hi][0x1C][version chars...]

    Version is ASCII, may be NUL-terminated, may include a trailing
    newline. Returns ``None`` if the packet isn't a valid RESP_VERSION.
    Exposed at module scope so tests can pin the parsing without
    spinning up a serial port.
    """
    if len(payload) <= PAYLOAD_POS:
        return None
    if payload[ID_POS] != RESP_VERSION:
        return None
    raw = payload[PAYLOAD_POS:]
    # Trim NULs and whitespace; firmware often appends \0 padding.
    text = raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
    return text or None


def _read_one_frame(ser: serial.Serial, timeout_s: float) -> bytes | None:
    """Read one SLIP frame from ``ser``, honoring ``timeout_s`` total.

    Discards bytes until a SLIP_START is seen, then accumulates until
    the matching SLIP_END (escape-aware). Returns the full frame
    including SLIP_START / SLIP_END, or ``None`` on timeout.
    """
    import time
    deadline = time.monotonic() + timeout_s
    # Hunt for SLIP_START.
    while time.monotonic() < deadline:
        b = ser.read(1)
        if not b:
            continue
        if b[0] == SLIP_START:
            break
    else:
        return None
    frame = bytearray([SLIP_START])
    escaped = False
    while time.monotonic() < deadline:
        b = ser.read(1)
        if not b:
            continue
        frame.append(b[0])
        if escaped:
            escaped = False
            continue
        if b[0] == SLIP_ESC:
            escaped = True
        elif b[0] == SLIP_END:
            return bytes(frame)
    return None


def query_firmware_version(
    serial_path: str,
    *,
    timeout_s: float = DEFAULT_QUERY_TIMEOUT_S,
    baudrates: tuple[int, ...] = SNIFFER_BAUDRATES,
) -> str | None:
    """Open the dongle's serial port, send REQ_VERSION, return the version.

    Returns ``None`` on any failure: port busy (capture in progress),
    not a Nordic Sniffer, no response, malformed response. Callers
    should treat ``None`` as "unknown" — never as "definitely OK" or
    "definitely bad". Caller is responsible for filtering to dongles
    that are plausibly Nordic Sniffer firmware before calling.

    Tries each baudrate in ``baudrates`` in order; first one that
    yields a SLIP frame wins. The Nordic firmware autodetects baud,
    but its responses come at the configured rate, so if we guess
    wrong we'll just see garbage and try the next.
    """
    for baud in baudrates:
        try:
            with serial.Serial(
                port=serial_path,
                baudrate=baud,
                timeout=0.05,            # per-read; outer loop enforces budget
                exclusive=True,
            ) as ser:
                # Drain any in-flight bytes (the firmware may be mid-heartbeat).
                ser.reset_input_buffer()
                ser.write(_build_request(REQ_VERSION))
                ser.flush()
                frame = _read_one_frame(ser, timeout_s)
                if frame is None:
                    continue
                payload = _slip_decode(frame)
                version = parse_version_response(payload)
                if version is not None:
                    return version
                # Got a frame but it wasn't RESP_VERSION — some firmware
                # versions reply to REQ_VERSION with PING_RESP instead.
                # We don't try to extract the legacy 2-byte version here;
                # we just don't know it and report unknown.
        except serial.SerialException as e:
            # EBUSY (port in use by capture) and ENOENT (race with
            # unplug) both land here. Caller treats None as "unknown".
            log.debug("firmware version query failed on %s @ %d: %s",
                      serial_path, baud, e)
            continue
        except OSError as e:
            log.debug("OS error querying %s @ %d: %s", serial_path, baud, e)
            continue
    return None


def query_firmware_versions(
    serial_paths: list[str],
    *,
    timeout_s: float = DEFAULT_QUERY_TIMEOUT_S,
    max_workers: int = 8,
) -> dict[str, str | None]:
    """Query firmware versions for several dongles in parallel.

    Returns a dict mapping ``serial_path`` → version string or None.
    Total wall time is bounded by ``timeout_s`` plus thread-pool
    overhead, regardless of dongle count (up to ``max_workers``).

    Used by the preferences dialog so a 7-dongle setup doesn't add 3+
    seconds of latency to opening the dialog.
    """
    if not serial_paths:
        return {}
    workers = min(max_workers, len(serial_paths))
    results: dict[str, str | None] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(query_firmware_version, p, timeout_s=timeout_s): p
            for p in serial_paths
        }
        for fut in futures:
            path = futures[fut]
            try:
                results[path] = fut.result(timeout=timeout_s + 0.2)
            except Exception:  # noqa: BLE001 — best-effort
                results[path] = None
    return results


# ──────────────────────────────────────────────────────────────────────────
# Coded-PHY compatibility detection
# ──────────────────────────────────────────────────────────────────────────

# Firmware versions where Coded PHY is *known* to be broken on the
# Nordic Sniffer firmware. Nordic engineer Einar Thorsrud acknowledged
# 4.1.x as broken on DevZone with no fix planned. See
# ``CODED_PHY_HELP_URL``. Any firmware *newer* than the latest known-
# broken version is treated as a soft "compatibility warning" — Nordic
# hasn't shipped a fix yet, so a fresher firmware is unverified rather
# than known-good.
CODED_PHY_KNOWN_BROKEN: tuple[int, ...] = (4, 1, 1)

# Best-of-the-bunch DevZone thread for users wanting to verify the bug:
# this is the one where Nordic explicitly acknowledged it.
CODED_PHY_HELP_URL = (
    "https://devzone.nordicsemi.com/f/nordic-q-a/117393/"
    "ble-sniffer-in-nrf52840-coded-phy-don-t-work"
)


def parse_version(s: str | None) -> tuple[int, ...] | None:
    """Parse a Nordic firmware version string like ``"4.1.1"`` into a
    tuple of ints. Returns ``None`` for unparseable strings (custom
    builds, garbled responses) — caller treats those as "unknown" and
    shows no warning.
    """
    if not s:
        return None
    parts = s.strip().split(".")
    try:
        return tuple(int(p) for p in parts)
    except (TypeError, ValueError):
        return None


class CodedPhyStatus:
    """Rendering hint for the prefs-dialog ``capture.coded_phy`` field.

    Three states:
      * ``severity == "blocked"`` — at least one dongle runs the known-
        broken firmware. The dialog must disable the checkbox, force
        it unchecked, and render ``suffix`` (red, with a "more info"
        link) to the right.
      * ``severity == "warning"`` — at least one dongle runs a
        firmware newer than the known-broken one (and no dongle is on
        the broken version). Nordic hasn't shipped a fix, so we
        surface a soft "compatibility warning" link beside the
        enabled checkbox.
      * ``severity is None`` — nothing of interest detected. Render
        the checkbox plainly.
    """
    __slots__ = ("severity", "suffix", "tooltip", "url", "versions")

    def __init__(
        self,
        *,
        severity: str | None,
        suffix: str | None = None,
        tooltip: str | None = None,
        url: str | None = None,
        versions: tuple[str, ...] = (),
    ) -> None:
        self.severity = severity
        self.suffix = suffix
        self.tooltip = tooltip
        self.url = url
        self.versions = versions


def coded_phy_status_for_versions(
    versions: list[str | None],
) -> CodedPhyStatus:
    """Decide warning state based on observed firmware versions.

    Precedence: any ``blocked`` dongle wins over any ``warning``
    dongle (a single 4.1.1 in the rack is enough to disable the
    checkbox, even if other dongles are 4.2.0). Unknown versions
    (``None``) are ignored — we don't punish users on custom firmware.
    """
    parsed: list[tuple[int, ...]] = [
        v for v in (parse_version(s) for s in versions) if v is not None
    ]
    raw_strings: list[str] = sorted({s for s in versions if s})

    blocked = [v for v in parsed if v == CODED_PHY_KNOWN_BROKEN]
    if blocked:
        bad_strings = sorted({
            s for s in raw_strings
            if parse_version(s) == CODED_PHY_KNOWN_BROKEN
        })
        vs = ", ".join(bad_strings)
        return CodedPhyStatus(
            severity="blocked",
            suffix=f"FW v. {vs} detected, incompatible",
            tooltip=(
                f"One or more sniffer devices is using Nordic Firmware "
                f"v. {vs} which is incompatible with capturing coded "
                f"PHY, more info."
            ),
            url=CODED_PHY_HELP_URL,
            versions=tuple(bad_strings),
        )

    newer = [v for v in parsed if v > CODED_PHY_KNOWN_BROKEN]
    if newer:
        newer_strings = sorted({
            s for s in raw_strings
            if (pv := parse_version(s)) is not None
            and pv > CODED_PHY_KNOWN_BROKEN
        })
        vs = ", ".join(newer_strings)
        return CodedPhyStatus(
            severity="warning",
            suffix="compatibility warning",
            tooltip=(
                f"Sniffer firmware v. {vs} is newer than the Nordic "
                f"firmware (4.1.1) where Coded PHY was confirmed "
                f"broken. Nordic has not announced a fix; this version "
                f"may still misbehave. Click for the DevZone thread "
                f"that documents the bug."
            ),
            url=CODED_PHY_HELP_URL,
            versions=tuple(newer_strings),
        )

    return CodedPhyStatus(severity=None)


def detect_coded_phy_incompatibility(
    *,
    timeout_s: float = DEFAULT_QUERY_TIMEOUT_S,
) -> CodedPhyStatus:
    """Top-level entry point for the prefs dialog.

    Lists currently-attached Nordic-VID sniffer dongles, queries each
    in parallel for its firmware version, then returns the rendering
    hint per ``coded_phy_status_for_versions``. On any error returns
    a no-op status (``severity=None``) — never blocks dialog open.
    """
    try:
        from . import discovery

        dongles = discovery.list_dongles_fast()
        # Query every attached sniffer dongle. ``list_dongles_fast``
        # already filters to USB devices whose product string contains
        # "sniffer", so the pool is small. Non-Nordic hardware
        # (Adafruit Bluefruit, CH340 bridges) won't respond to
        # REQ_VERSION — those queries return None and are ignored.
        # We deliberately don't pre-filter by USB product name:
        # descriptor strings vary between firmware builds (e.g.
        # "nRF Sniffer for BLE", "Nordic Semiconductor nRF Sniffer",
        # custom rebuilds) and a too-strict filter would silently drop
        # dongles whose firmware we needed to check — which is exactly
        # the failure mode we're trying to surface to the user.
        paths = [d.serial_path for d in dongles]
        log.debug(
            "coded-phy compat: probing %d attached dongle(s)",
            len(paths),
        )
        if not paths:
            return CodedPhyStatus(severity=None)
        versions = query_firmware_versions(paths, timeout_s=timeout_s)
        log.debug("coded-phy compat: versions=%r", versions)
        return coded_phy_status_for_versions(list(versions.values()))
    except Exception:  # noqa: BLE001 — never block the prefs dialog
        log.debug("coded-phy compat detection failed", exc_info=True)
        return CodedPhyStatus(severity=None)
