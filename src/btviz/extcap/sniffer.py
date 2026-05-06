"""Per-dongle sniffer process.

Spawns the Nordic extcap binary in ``--capture`` mode and wires up three
FIFOs per sniffer:

    * capture FIFO       -- extcap writes pcap packets, we read
    * control-in FIFO    -- we write control messages, extcap reads
    * control-out FIFO   -- extcap writes status/events, we read

Channel pinning, follow, and key injection are all driven through the
control pipe (NOT command-line args -- this extcap has no ``--channel``).

Nordic's extcap initialization protocol:
    1. extcap starts and opens its capture FIFO for write
    2. extcap reads control-in messages, applying each SET as it goes
    3. on the first message with type==CTRL_CMD_INIT (0), extcap stops
       consuming init values and begins capture
    4. after init, SET messages can be sent at any time for live retuning

Control pipe frame format (big-endian):
    T          (sync, 0x54)
    0          (reserved)
    uint16     payload_len + 2
    uint8      control arg  (0=Device, 1=KeyType, 2=KeyValue, 3=AdvHop, 7=Clear)
    uint8      command      (0=INIT, 1=SET, 2=ADD, 3=REMOVE, ...)
    payload    UTF-8 bytes, length = payload_len
"""
from __future__ import annotations

import os
import struct
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .discovery import Dongle, find_extcap_binary

# --- Nordic control-pipe constants (from nrf_sniffer_ble.py) ---------------

# Command type (second byte of the message arg/cmd pair)
CTRL_CMD_INIT     = 0
CTRL_CMD_SET      = 1
CTRL_CMD_ADD      = 2
CTRL_CMD_REMOVE   = 3
CTRL_CMD_ENABLE   = 4
CTRL_CMD_DISABLE  = 5
CTRL_CMD_STATUSBAR = 6
CTRL_CMD_INFO_MSG = 7
CTRL_CMD_WARN_MSG = 8
CTRL_CMD_ERROR_MSG = 9

# Control arg number (which control we're setting)
CTRL_ARG_DEVICE       = 0     # selector: device dropdown
CTRL_ARG_KEY_TYPE     = 1     # selector: key type (passkey, LTK, IRK, follow-addr, ...)
CTRL_ARG_KEY_VAL      = 2     # string:   the key bytes or LE address
CTRL_ARG_ADVHOP       = 3     # string:   comma-separated channel list
CTRL_ARG_HELP         = 4
CTRL_ARG_RESTORE      = 5
CTRL_ARG_LOG          = 6
CTRL_ARG_DEVICE_CLEAR = 7     # button
CTRL_ARG_NONE         = 255

# Key-type selector values (for CTRL_ARG_KEY_TYPE)
KEY_TYPE_PASSKEY       = 0
KEY_TYPE_OOB           = 1
KEY_TYPE_LEGACY_LTK    = 2
KEY_TYPE_SC_LTK        = 3
KEY_TYPE_DH_PRIVATE    = 4
KEY_TYPE_IRK           = 5
KEY_TYPE_ADD_ADDR      = 6
KEY_TYPE_FOLLOW_ADDR   = 7


@dataclass
class SnifferState:
    dongle: Dongle
    role: str = "idle"                    # idle | scan | follow
    adv_hop: list[int] = field(default_factory=lambda: [37, 38, 39])
    follow_target: str | None = None      # "aa:bb:... (public|random)"
    running: bool = False
    last_error: str | None = None
    # Wallclock time (epoch seconds) of the most recent successful
    # packet read from this sniffer's capture FIFO. ``None`` until
    # the first packet arrives or after a fresh subprocess spawn —
    # the watchdog (see ``btviz.capture.watchdog``) treats ``None``
    # as a grace period so it doesn't fire the moment a sniffer
    # starts. Updated by ``_capture_loop`` on every record.
    last_packet_ts: float | None = None
    # Wallclock time the subprocess began. Combined with
    # ``last_packet_ts`` so the watchdog can grace-period a
    # newly-spawned sniffer.
    started_at: float | None = None


@dataclass
class RawPacket:
    ts: float             # seconds from pcap record header
    data: bytes           # raw link-layer incl. Nordic BLE PHDR pseudo-header
    source: str           # dongle short id
    meta: dict[str, Any] = field(default_factory=dict)


class SnifferProcess:
    """Owns one nRF Sniffer extcap subprocess, plus its capture and control FIFOs."""

    def __init__(
        self,
        dongle: Dongle,
        on_packet: Callable[[Dongle, RawPacket], None],
        on_state: Callable[[SnifferState], None] | None = None,
    ) -> None:
        self._dongle = dongle
        self._on_packet = on_packet
        self._on_state = on_state or (lambda s: None)
        self._proc: subprocess.Popen[bytes] | None = None
        self._fifo_dir: Path | None = None
        self._capture_fifo: Path | None = None
        self._ctrl_in_fifo: Path | None = None   # we write to this
        self._ctrl_out_fifo: Path | None = None  # we read from this
        self._ctrl_in_fd: int | None = None      # file descriptor we write to
        self._ctrl_in_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._ctrl_reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stop = threading.Event()
        self.state = SnifferState(dongle=dongle)

    # --- lifecycle -------------------------------------------------------

    def start(
        self,
        *,
        adv_hop: list[int] | None = None,
        follow_address: tuple[str, bool] | None = None,  # (mac, is_random)
        only_advertising: bool = True,
        only_legacy_advertising: bool = False,
        scan_follow_rsp: bool = True,
        scan_follow_aux: bool = True,
        coded_phy: bool = False,
    ) -> None:
        if self.state.running:
            return

        adv_hop = adv_hop or [37, 38, 39]
        _validate_adv_hop(adv_hop)

        extcap = find_extcap_binary()

        tmpdir = Path(tempfile.mkdtemp(prefix="btviz-sniffer-"))
        cap_fifo = tmpdir / "cap.pcap"
        ctrl_in = tmpdir / "ctrl_in"
        ctrl_out = tmpdir / "ctrl_out"
        for p in (cap_fifo, ctrl_in, ctrl_out):
            os.mkfifo(p)
        self._fifo_dir = tmpdir
        self._capture_fifo = cap_fifo
        self._ctrl_in_fifo = ctrl_in
        self._ctrl_out_fifo = ctrl_out

        args: list[str] = [
            str(extcap),
            "--capture",
            "--extcap-interface", self._dongle.interface_id,
            "--fifo", str(cap_fifo),
            "--extcap-control-in", str(ctrl_in),
            "--extcap-control-out", str(ctrl_out),
        ]
        if only_advertising:
            args.append("--only-advertising")
        if only_legacy_advertising:
            args.append("--only-legacy-advertising")
        if scan_follow_rsp:
            args.append("--scan-follow-rsp")
        if scan_follow_aux:
            args.append("--scan-follow-aux")
        if coded_phy:
            args.append("--coded")
        if follow_address is not None:
            mac, is_random = follow_address
            args += ["--device", _format_addr(mac, is_random)]

        # Open capture FIFO for read non-blocking, before spawning writer.
        cap_fd = os.open(cap_fifo, os.O_RDONLY | os.O_NONBLOCK)

        # Capture stderr so Nordic script tracebacks (e.g. SnifferAPI
        # import failures, serial-open errors) surface as last_error
        # rather than vanishing into an unread pipe buffer. Without
        # the drain, a full pipe blocks the subprocess and we'd see
        # only the pcap header before silence.
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        # Open control pipes. These block until the extcap has opened
        # its end. We open ctrl_out first (extcap writes; we read), then
        # ctrl_in (we write; extcap reads).
        try:
            ctrl_out_fd = os.open(ctrl_out, os.O_RDONLY | os.O_NONBLOCK)
            ctrl_in_fd = os.open(ctrl_in, os.O_WRONLY)
        except OSError as e:
            self.state.last_error = f"ctrl fifo open: {e}"
            self._on_state(self.state)
            self.stop()
            return
        self._ctrl_in_fd = ctrl_in_fd

        self._stop.clear()
        self._reader = threading.Thread(
            target=self._capture_loop, args=(cap_fd,),
            name=f"sniffer-{self._dongle.short_id}", daemon=True,
        )
        self._ctrl_reader = threading.Thread(
            target=self._ctrl_out_loop, args=(ctrl_out_fd,),
            name=f"sniffer-ctrl-{self._dongle.short_id}", daemon=True,
        )
        self._stderr_reader = threading.Thread(
            target=self._stderr_loop,
            name=f"sniffer-err-{self._dongle.short_id}", daemon=True,
        )
        self._reader.start()
        self._ctrl_reader.start()
        self._stderr_reader.start()

        # Send initial control values, then the INIT terminator so the extcap
        # finishes consuming init values and starts capturing.
        self._write_ctrl(CTRL_ARG_ADVHOP, CTRL_CMD_SET, _adv_hop_str(adv_hop))
        self._write_ctrl(CTRL_ARG_NONE, CTRL_CMD_INIT, "")

        self.state.running = True
        self.state.started_at = time.time()
        self.state.last_packet_ts = None
        self.state.adv_hop = list(adv_hop)
        self.state.role = "follow" if follow_address else "scan"
        self.state.follow_target = (
            _format_addr(*follow_address) if follow_address else None
        )
        self._on_state(self.state)

    def stop(self) -> None:
        if not self.state.running and self._proc is None:
            return
        self._stop.set()
        if self._ctrl_in_fd is not None:
            try:
                os.close(self._ctrl_in_fd)
            except OSError:
                pass
            self._ctrl_in_fd = None
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        for t in (self._reader, self._ctrl_reader, self._stderr_reader):
            if t is not None:
                t.join(timeout=1)
        self._reader = None
        self._ctrl_reader = None
        self._stderr_reader = None
        if self._fifo_dir and self._fifo_dir.exists():
            for p in (self._capture_fifo, self._ctrl_in_fifo, self._ctrl_out_fifo):
                if p and p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass
            try:
                self._fifo_dir.rmdir()
            except OSError:
                pass
        self._fifo_dir = None
        self.state.running = False
        self.state.role = "idle"
        self.state.follow_target = None
        self._on_state(self.state)

    # --- live control ---------------------------------------------------

    def set_adv_hop(self, channels: list[int]) -> None:
        """Live: change the advertising channel hop sequence."""
        _validate_adv_hop(channels)
        if not self.state.running:
            self.state.adv_hop = list(channels)
            return
        self._write_ctrl(CTRL_ARG_ADVHOP, CTRL_CMD_SET, _adv_hop_str(channels))
        self.state.adv_hop = list(channels)
        self._on_state(self.state)

    def follow_address(self, mac: str, is_random: bool) -> None:
        """Live: follow a specific advertiser address (by setting key-type + value)."""
        addr_str = _format_addr(mac, is_random)
        # Select the "Follow LE address" key type, then write the address value.
        self._write_ctrl(CTRL_ARG_KEY_TYPE, CTRL_CMD_SET, str(KEY_TYPE_FOLLOW_ADDR))
        self._write_ctrl(CTRL_ARG_KEY_VAL, CTRL_CMD_SET, addr_str)
        self.state.role = "follow"
        self.state.follow_target = addr_str
        self._on_state(self.state)

    def add_irk(self, irk_hex: str) -> None:
        """Feed an IRK to the sniffer for RPA resolution."""
        _validate_hex(irk_hex, 16)
        self._write_ctrl(CTRL_ARG_KEY_TYPE, CTRL_CMD_SET, str(KEY_TYPE_IRK))
        self._write_ctrl(CTRL_ARG_KEY_VAL, CTRL_CMD_SET, "0x" + irk_hex)

    def add_ltk(self, ltk_hex: str, legacy: bool = True) -> None:
        """Feed an LTK for link-layer decryption (legacy or Secure Connections)."""
        _validate_hex(ltk_hex, 16)
        kt = KEY_TYPE_LEGACY_LTK if legacy else KEY_TYPE_SC_LTK
        self._write_ctrl(CTRL_ARG_KEY_TYPE, CTRL_CMD_SET, str(kt))
        self._write_ctrl(CTRL_ARG_KEY_VAL, CTRL_CMD_SET, "0x" + ltk_hex)

    def clear_devices(self) -> None:
        """Ask the sniffer to clear its internal device list."""
        self._write_ctrl(CTRL_ARG_DEVICE_CLEAR, CTRL_CMD_SET, "")

    # --- pipe helpers ---------------------------------------------------

    def _write_ctrl(self, arg: int, cmd: int, payload: str) -> None:
        """Serialize and write one control-pipe message. Thread-safe."""
        if self._ctrl_in_fd is None:
            return
        data = payload.encode("utf-8")
        header = struct.pack(">BBHBB", ord("T"), 0, len(data) + 2, arg, cmd)
        frame = header + data
        with self._ctrl_in_lock:
            try:
                os.write(self._ctrl_in_fd, frame)
            except OSError as e:
                self.state.last_error = f"ctrl write: {e}"
                self._on_state(self.state)

    # --- capture reader -------------------------------------------------

    def _capture_loop(self, fd: int) -> None:
        """Read pcap stream from the capture FIFO and emit RawPacket objects."""
        import fcntl
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

        with os.fdopen(fd, "rb", buffering=0) as f:
            try:
                header = _read_exact(f, 24)
                if not header or len(header) < 24:
                    return
                magic = struct.unpack("<I", header[:4])[0]
                little = magic in (0xa1b2c3d4, 0xa1b23c4d)
                dlt_fmt = "<I" if little else ">I"
                dlt = struct.unpack(dlt_fmt, header[20:24])[0]
                while not self._stop.is_set():
                    rec_hdr = _read_exact(f, 16)
                    if not rec_hdr or len(rec_hdr) < 16:
                        return
                    fmt = "<IIII" if little else ">IIII"
                    ts_sec, ts_usec, incl_len, _orig_len = struct.unpack(fmt, rec_hdr)
                    if incl_len == 0 or incl_len > 65535:
                        return
                    payload = _read_exact(f, incl_len)
                    if payload is None or len(payload) < incl_len:
                        return
                    pkt = RawPacket(
                        ts=ts_sec + ts_usec / 1_000_000.0,
                        data=payload,
                        source=self._dongle.short_id,
                        meta={"dlt": dlt},
                    )
                    # Wallclock — distinct from pkt.ts which comes
                    # from the pcap record header. The watchdog
                    # compares this to ``time.time()``; using the
                    # pcap ts would not work if the firmware clock
                    # drifts.
                    self.state.last_packet_ts = time.time()
                    self._on_packet(self._dongle, pkt)
            except Exception as e:  # noqa: BLE001
                self.state.last_error = repr(e)
                self._on_state(self.state)

    # --- control-out reader ---------------------------------------------

    def _ctrl_out_loop(self, fd: int) -> None:
        """Drain the control-out pipe so the extcap never blocks writing.

        We don't currently surface extcap device-list updates to the UI
        (we track devices ourselves from the packet stream), but the pipe
        must be drained. Exposing these events is a future enhancement.
        """
        import fcntl
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

        with os.fdopen(fd, "rb", buffering=0) as f:
            while not self._stop.is_set():
                hdr = _read_exact(f, 6)
                if hdr is None or len(hdr) < 6:
                    return
                try:
                    _, _, length, arg, cmd = struct.unpack(">sBHBB", hdr)
                except struct.error:
                    return
                payload_len = max(0, length - 2)
                payload = _read_exact(f, payload_len) if payload_len else b""
                self._handle_ctrl_out(arg, cmd, payload or b"")

    def _handle_ctrl_out(self, arg: int, cmd: int, payload: bytes) -> None:
        """Hook for surfacing extcap-emitted events. Currently logs errors only."""
        if cmd in (CTRL_CMD_WARN_MSG, CTRL_CMD_ERROR_MSG):
            self.state.last_error = payload.decode("utf-8", errors="replace")
            self._on_state(self.state)

    # --- stderr drainer -------------------------------------------------

    def _stderr_loop(self) -> None:
        """Drain the subprocess's stderr.

        Without this, a subprocess that prints (Python tracebacks,
        SnifferAPI errors, the Nordic launcher's benign shell warnings)
        eventually fills the OS pipe buffer (~64 KB on macOS) and
        blocks on its next stderr write — typically right after the
        pcap header is emitted, which leaves us with "header but no
        packets" and no clue why.

        The most recent non-blank line is stashed on ``state.last_error``
        so the panel / status bar can surface it. We read line-by-line
        rather than chunked so partial buffers don't hide a one-line
        traceback.
        """
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in iter(proc.stderr.readline, b""):
                if self._stop.is_set():
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text or _is_benign_extcap_stderr(text):
                    continue
                self.state.last_error = text
                self._on_state(self.state)
        except (OSError, ValueError):
            # ValueError fires when the file is closed mid-read by stop().
            return


# --- helpers ---------------------------------------------------------------

def _validate_adv_hop(channels: list[int]) -> None:
    if not channels:
        raise ValueError("adv_hop must be non-empty")
    if not all(ch in (37, 38, 39) for ch in channels):
        raise ValueError(f"adv_hop must be a subset of {{37,38,39}}: {channels}")


def _adv_hop_str(channels: list[int]) -> str:
    return ",".join(str(c) for c in channels)


def _format_addr(mac: str, is_random: bool) -> str:
    """Format for CTRL_ARG_KEY_VAL / --device: 'aa:bb:cc:dd:ee:ff public|random'."""
    return f"{mac.lower()} {'random' if is_random else 'public'}"


def _is_benign_extcap_stderr(line: str) -> bool:
    """True for stderr lines we know aren't real failures.

    Nordic's launcher script (nrf_sniffer_ble.sh) ships with a
    shell-syntax bug at the ``$VIRTUAL_ENV`` test (missing space
    before ``]``) that prints ``[: missing ']'`` on every spawn.
    Execution still succeeds, so surfacing it as last_error would
    make every healthy capture look broken.
    """
    return "missing `]'" in line


def _validate_hex(value: str, want_bytes: int) -> None:
    v = value.lower().removeprefix("0x")
    if len(v) != want_bytes * 2 or any(c not in "0123456789abcdef" for c in v):
        raise ValueError(f"expected {want_bytes}-byte hex string, got {value!r}")


def _read_exact(f, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)
