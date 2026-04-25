"""tshark subprocess wrapper.

We invoke tshark with `-T ek` (elastic-search-style JSON: one record per line,
streamable). This is the same dissection pipeline used by the live path later,
so file ingest and live ingest share a normalizer.

The wrapper does not interpret fields; it just yields parsed JSON dicts. See
`normalize.py` for field extraction.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

# Candidate tshark binaries, first-wins. Override with $BTVIZ_TSHARK.
TSHARK_CANDIDATES: tuple[str, ...] = (
    "/Applications/Wireshark.app/Contents/MacOS/tshark",
    "/usr/bin/tshark",
    "/usr/local/bin/tshark",
    "/opt/homebrew/bin/tshark",
)


class TsharkNotFound(RuntimeError):
    """tshark binary could not be located."""


class TsharkError(RuntimeError):
    """tshark ran but exited non-zero."""


def find_tshark() -> str:
    override = os.environ.get("BTVIZ_TSHARK")
    if override and Path(override).exists():
        return override
    for cand in TSHARK_CANDIDATES:
        if Path(cand).exists():
            return cand
    which = shutil.which("tshark")
    if which:
        return which
    raise TsharkNotFound(
        "tshark not found. Install Wireshark, or set $BTVIZ_TSHARK to the "
        "tshark binary path."
    )


# Default display filter: only actual BLE link-layer frames.
_BLE_FILTER = "btle"

# CRC-failed packets never reach a real device's host stack; drop them by
# default. Written as "not (field == 0)" so packets lacking the field (e.g.
# from a sniffer that doesn't emit it) still pass.
_CRC_OK_FILTER = (
    "not nordic_ble.crcok == 0 and not btle_rf.flags.crc_valid == 0"
)


def dissect_file(
    path: str | Path,
    *,
    display_filter: str | None = None,
    keep_bad_crc: bool = False,
    extra_args: tuple[str, ...] = (),
    tshark_bin: str | None = None,
) -> Iterator[dict]:
    """Yield one tshark EK record per matching packet in a pcap/pcapng file.

    display_filter
        Passed to `-Y`. If None (default), use ``btle`` AND the CRC-OK filter
        (unless `keep_bad_crc=True`). Pass an explicit string to override
        entirely; pass "" to disable filtering completely.
    keep_bad_crc
        When True, retain packets where the LL CRC check failed. Useful for
        measuring per-channel error rates / interference, but produces many
        phantom "devices" because the address field can be bit-flipped. Only
        honored when ``display_filter`` is None (i.e. default filter path).

    Each yielded record has the shape::

        {"timestamp": "...", "layers": {"btle": {...}, "frame": {...}, ...}}

    `-T ek` emits paired "index" / "data" lines; we only yield the data lines
    (those with a ``layers`` key).
    """
    if display_filter is None:
        display_filter = _BLE_FILTER
        if not keep_bad_crc:
            display_filter = f"({display_filter}) and {_CRC_OK_FILTER}"
    elif display_filter == "":
        display_filter = None
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    bin_ = tshark_bin or find_tshark()
    cmd = [
        bin_,
        "-r", str(path),
        "-T", "ek",
        "-l",    # line-buffered stdout
        "-n",    # no name resolution (faster, avoids DNS lookups)
    ]
    if display_filter:
        cmd += ["-Y", display_filter]
    cmd += list(extra_args)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        text=True,
    )
    assert proc.stdout is not None and proc.stderr is not None

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # EK emits an "index" meta-line before each packet; also
                # some tshark builds prepend non-JSON banner text. Skip
                # anything that doesn't parse.
                continue
            if "layers" in obj:
                yield obj
    finally:
        # Drain stderr and surface it on failure.
        stderr_text = proc.stderr.read() if not proc.stderr.closed else ""
        rc = proc.wait()
        if rc != 0:
            raise TsharkError(
                f"tshark exited {rc} for {path}: {stderr_text.strip()}"
            )
