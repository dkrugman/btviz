"""Live capture → DB writer.

Bridges the bus's ``TOPIC_PACKET`` stream to the same per-packet helper
file ingest uses (``record_packet``), so live and file paths produce
identical device / address / broadcast / observation rows.

Threading model:

  * Reader threads inside each ``SnifferProcess`` call
    ``CaptureCoordinator._handle_raw`` → ``bus.publish(TOPIC_PACKET, …)``.
    The bus is synchronous, so our ``_on_packet`` runs **on the reader
    thread**. We MUST NOT touch sqlite there — the connection is owned
    by the Qt main thread.
  * ``_on_packet`` decodes the raw bytes (in-process, fast) and pushes
    the resulting ``Packet`` onto a deque protected by a Lock.
  * ``flush()`` runs on the thread that owns the DB connection (the Qt
    main thread, normally driven by a ``QTimer``). It drains the deque
    in a single transaction.

The deque is bounded — under a runaway adv flood we drop oldest first
and bump ``stats.packets_dropped``. Better than crashing the process or
locking up the UI thread on an unbounded write batch.
"""
from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from ..bus import EventBus, TOPIC_PACKET
from ..db.repos import Repos
from ..ingest.pipeline import IngestContext, record_packet
from .live_decode import decode_live_packet
from .packet import Packet

# Default cap. ~3k adv pkt/s × 2s of headroom is plenty for the Qt timer
# cadence we drive flush() at (typically 250ms).
_DEFAULT_QUEUE_CAP = 8192


@dataclass
class LiveIngestStats:
    packets_received: int = 0     # bus deliveries we saw
    packets_decoded: int = 0      # decode_live_packet returned a Packet
    packets_recorded: int = 0     # attributed to a device (observation row)
    packets_dropped: int = 0      # queue overflow → oldest evicted
    flushes: int = 0
    last_flush_size: int = 0
    devices_touched: int = 0      # distinct device ids over the session
    broadcasts_seen: int = 0
    ext_adv_seen: int = 0         # ADV_EXT_IND / AUX_ADV_IND packets observed
    ext_adv_with_baa: int = 0     # subset that carried BAA service data


@dataclass
class _SourceState:
    """Per-dongle counters surfaced for UI activity indicators."""
    last_packet_ts: float = 0.0
    packet_count: int = 0


class LiveIngest:
    """Subscribe to TOPIC_PACKET, decode, queue, flush on demand."""

    def __init__(
        self,
        bus: EventBus,
        repos: Repos,
        project_id: int,
        *,
        session_name: str | None = None,
        queue_cap: int = _DEFAULT_QUEUE_CAP,
    ) -> None:
        self._bus = bus
        self._repos = repos
        self._project_id = project_id
        self._session_name = session_name
        self._queue: deque[Packet] = deque(maxlen=queue_cap)
        self._lock = threading.Lock()
        self._unsub: Callable[[], None] | None = None
        self._ctx: IngestContext | None = None
        self._session_id: int | None = None
        self._on_source_packet: Callable[[str, int | None], None] | None = None
        # Optional per-device callback fired on each successfully-recorded
        # packet. Receives (device_id, channel) so the canvas can flash a
        # per-device channel indicator. Distinct from the source callback
        # because attribution to a device requires a successful
        # ``record_packet`` (i.e. the packet had a parseable adv_addr) —
        # the source callback fires for every decoded packet regardless.
        self._on_device_packet: Callable[[int, int | None], None] | None = None
        self._sources: dict[str, _SourceState] = {}
        # Per-source decode diagnostics. ``received`` increments for
        # every bus packet from each source (before decode); ``rejected``
        # increments when ``decode_live_packet`` returns None. Their
        # difference is the per-source decoded count. Used by the
        # toolbar status to spot "this dongle is delivering bytes but
        # nothing decodes" vs "this dongle is silent." Mutated under
        # ``_lock`` because ``_on_packet`` runs on a reader thread.
        self._source_received: dict[str, int] = {}
        self._source_rejected: dict[str, int] = {}
        # One-shot diagnostic: log the first reject per source so we can
        # see the byte layout when decode_live_packet returns None for
        # everything (DLT mismatch, unexpected PHDR variant, etc.).
        self._dumped_sources: set[str] = set()
        self.stats = LiveIngestStats()

    # ------------------------------------------------------------------ API

    @property
    def session_id(self) -> int | None:
        return self._session_id

    @property
    def running(self) -> bool:
        return self._unsub is not None

    def set_packet_callback(
        self, fn: Callable[[str, int | None], None] | None,
    ) -> None:
        """Set a per-source notifier called on flush (main thread).

        Receives ``(source, channel)`` for each decoded packet — source
        is the dongle short id; channel is the BLE channel index (0-39)
        from the decoded pseudo-header, or ``None`` when the decoder
        couldn't determine it. Used to drive sniffer-panel activity
        dots and the per-row channel-tag highlight.
        """
        self._on_source_packet = fn

    def set_device_packet_callback(
        self, fn: "Callable[[int, int | None], None] | None",
    ) -> None:
        """Set a per-device notifier called on flush (main thread).

        Receives ``(device_id, channel)`` for each packet that
        successfully recorded against a device row (i.e. ``pkt.adv_addr``
        was parseable). Used to drive the per-device-box channel-flash
        indicator on the canvas.

        Skipped packets (no adv_addr) don't trigger this — they wouldn't
        have a device_id anyway.
        """
        self._on_device_packet = fn

    def source_stats(self) -> dict[str, _SourceState]:
        """Snapshot of per-source packet counters. Read on main thread."""
        return dict(self._sources)

    def source_health(self) -> dict[str, tuple[int, int]]:
        """Per-source ``(received, rejected)`` snapshot, main thread.

        Reading is racy but cheap — we never decrement, so worst case
        a sample shows a value that's a few packets stale. Used by the
        toolbar status string to surface which sniffer is producing
        decodable bytes vs which is silent or all-rejecting.
        """
        with self._lock:
            return {
                src: (self._source_received.get(src, 0),
                      self._source_rejected.get(src, 0))
                for src in self._source_received
            }

    def start(self) -> int:
        """Open a live session and begin queuing packets. Returns session id."""
        if self.running:
            return self._session_id  # type: ignore[return-value]
        sess = self._repos.sessions.start(
            self._project_id,
            source_type="live",
            name=self._session_name,
        )
        self._session_id = sess.id
        self._ctx = IngestContext(session_id=sess.id)
        self._unsub = self._bus.subscribe(TOPIC_PACKET, self._on_packet)
        return sess.id

    def stop(self) -> None:
        """Unsubscribe, flush remaining queue, end the session."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        # Final drain — caller is on main thread (where stop() is invoked)
        # so it's safe to write.
        self.flush()
        if self._session_id is not None:
            self._repos.sessions.end(self._session_id)
        self._session_id = None
        self._ctx = None

    # ---------------------------------------------------------------- internal

    def _on_packet(self, pkt: Packet) -> None:
        """Bus subscriber — runs on a reader thread. NO DB access."""
        self.stats.packets_received += 1
        src = pkt.source or "?"
        with self._lock:
            self._source_received[src] = self._source_received.get(src, 0) + 1
        # The coordinator stamps pkt.extras["dlt"] from the pcap global
        # header so the decoder can pick the right PHDR layout
        # (256 = LE_LL_WITH_PHDR, 272 = NORDIC_BLE).
        dlt = (pkt.extras or {}).get("dlt")
        decoded = decode_live_packet(
            pkt.raw, source=pkt.source, ts=pkt.ts, dlt=dlt,
        )
        if decoded is None:
            with self._lock:
                self._source_rejected[src] = (
                    self._source_rejected.get(src, 0) + 1
                )
            # One-shot per-source hexdump so we can see what the decoder
            # rejected — distinguishes a DLT / PHDR mismatch (bytes don't
            # start with the Nordic phdr we expect) from a runtime error.
            if src not in self._dumped_sources:
                self._dumped_sources.add(src)
                head = (pkt.raw or b"")[:32]
                hex_str = " ".join(f"{b:02x}" for b in head)
                import sys
                print(
                    f"[live-decode] reject src={src} "
                    f"len={len(pkt.raw or b'')} first32={hex_str}",
                    file=sys.stderr,
                    flush=True,
                )
            return
        self.stats.packets_decoded += 1
        with self._lock:
            if len(self._queue) == self._queue.maxlen:
                # deque(maxlen=…) silently drops oldest on append; track it.
                self.stats.packets_dropped += 1
            self._queue.append(decoded)

    def flush(self) -> int:
        """Drain queue → DB. Call on the thread owning the connection.

        Returns number of packets written.
        """
        if self._ctx is None:
            return 0
        with self._lock:
            if not self._queue:
                return 0
            batch = list(self._queue)
            self._queue.clear()

        recorded = 0
        with self._repos.store.tx():
            for pkt in batch:
                src = pkt.source or ""
                # Fire for every decoded packet so the activity dot flashes
                # even when the packet has no adv_addr (e.g. data-channel
                # frames, SCAN_RSP, hub-connected sniffers on non-primary chs).
                if self._on_source_packet is not None:
                    try:
                        self._on_source_packet(src, pkt.channel)
                    except Exception:  # noqa: BLE001
                        import traceback
                        traceback.print_exc()
                device_id = record_packet(self._repos, self._ctx, pkt)
                if device_id is not None:
                    recorded += 1
                    state = self._sources.get(src)
                    if state is None:
                        state = _SourceState()
                        self._sources[src] = state
                    state.last_packet_ts = pkt.ts
                    state.packet_count += 1
                    # Per-device flash: fire after successful attribution
                    # so the canvas can light up the right DeviceItem
                    # with the channel color.
                    if self._on_device_packet is not None:
                        try:
                            self._on_device_packet(device_id, pkt.channel)
                        except Exception:  # noqa: BLE001
                            import traceback
                            traceback.print_exc()
        self.stats.packets_recorded += recorded
        self.stats.flushes += 1
        self.stats.last_flush_size = len(batch)
        self.stats.devices_touched = len(self._ctx.seen_device_ids)
        self.stats.broadcasts_seen = len(self._ctx.seen_broadcast_ids)
        self.stats.ext_adv_seen = self._ctx.ext_adv_count
        self.stats.ext_adv_with_baa = self._ctx.ext_adv_with_baa
        return recorded
