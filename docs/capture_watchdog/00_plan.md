# Capture stall watchdog

## Problem

A live capture session running 2 h 4 min stopped delivering packets at
01:39:05. btviz didn't notice. The Pinned dongles' LEDs kept flashing
(firmware alive, radios listening); the OS still saw the dongles in
`ioreg`; the extcap subprocesses were alive at 0% CPU; but the
`/dev/cu.usbmodem*` CDC endpoints had stopped delivering bytes to the
host. FIFO write offsets sat unchanged for 9 h 45 min until the user
noticed.

The bug class: **silent host-side USB-CDC wedge under sustained
multi-device load.** The OS is happy, the firmware is happy, only
the bytes-to-host pipe is dead. Reload didn't recover (confirmed —
the wedge is below btviz's reach).

We need:

1. **Detection** — a watchdog that notices a sniffer has stopped
   producing packets and surfaces it within a minute, not nine hours.
2. **Recovery** — automatic subprocess restart on detection.
3. **Visibility** — a persistent indicator the user sees on next
   reload, even if they weren't watching at the moment of the stall.
4. **Audit trail** — a log file the user can grep to answer "is this
   chronic or a one-off?"

## Detection rule

Per-sniffer threshold. A sniffer is *stalled* when:

- `state.running = True`, AND
- `state.role` is not `Idle` (idle dongles aren't supposed to capture,
  silence is correct), AND
- Time since `state.last_packet_ts` exceeds `STALL_THRESHOLD_S`
  (default 60 s; configurable).

The threshold default of 60 s is conservative. The user's case had
all three Pinned dongles silent for 9+ h, so even a 5-min threshold
would have caught it. 60 s strikes a balance: short enough to catch
real wedges quickly, long enough to avoid false positives in the
rare RF-quiet environment where no nearby device adverts for a
minute. Apple Continuity ensures any unlocked iPhone within range
broadcasts at minimum every few seconds, so 60 s of silence on a
primary advertising channel is genuinely abnormal.

A `last_packet_ts` of `None` (sniffer just started, no packets yet)
is grace-period: don't fire the watchdog for the first
`STALL_THRESHOLD_S` of subprocess uptime.

## Recovery action

On stall detection:

1. **Log a STALL event** — see "Logging" below.
2. **Increment `Sniffer.stall_count`** in the DB. Persistent across
   sessions and btviz restarts.
3. **Update `Sniffer.last_stall_at`** to now.
4. **Stop the wedged subprocess** via existing
   `SnifferProcess.stop()`.
5. **Spawn a fresh subprocess** via the existing coordinator path
   (`Coordinator.start_one`).

Restart attempts are limited to **3 per sniffer per session**, with
a minimum **30 s gap** between attempts. After the third failed
restart, the sniffer is marked **STUCK** in state. The watchdog
stops trying; the panel surfaces the terminal state with a more
prominent indicator. Replugging the dongle resets the attempt
counter (discovery sees a "fresh" device).

Caveat: if the kernel-level CDC endpoint is wedged (the user's
case), restarting the subprocess won't help — pyserial may fail
to open the port, or open it but receive zero bytes. The watchdog
catches this: a restarted subprocess that immediately re-stalls
counts toward the 3-attempt cap. After give-up, the only fix is
physical replug or reboot.

## Logging

New file: `~/.btviz/capture.log` with rotation, mirroring the
existing `cluster.log` infrastructure in
`src/btviz/cluster/cluster_log.py`.

Token `STALL` appears literally in every line related to this
feature, so `grep STALL ~/.btviz/capture.log` shows the full story:

```
2026-05-05 11:50:23.456  STALL detected sniffer=856175 channel=37 silent_for=68.3s attempt=1
2026-05-05 11:50:23.812  STALL restarted sniffer=856175 spawned new subprocess
2026-05-05 11:51:42.103  STALL detected sniffer=856175 channel=37 silent_for=72.1s attempt=2
2026-05-05 11:51:42.422  STALL restarted sniffer=856175 spawned new subprocess
2026-05-05 11:52:54.221  STALL detected sniffer=856175 channel=37 silent_for=70.0s attempt=3
2026-05-05 11:52:54.500  STALL restarted sniffer=856175 spawned new subprocess
2026-05-05 11:54:08.300  STALL gave_up sniffer=856175 channel=37 attempts=3 — replug required
```

Other capture events worth logging in the same file (future scope):
subprocess spawn / exit, USB enumeration changes, role transitions.
For v1 the file is dedicated to STALL.

## Persistence (DB schema)

Two columns on `sniffers`, additive:

```sql
ALTER TABLE sniffers ADD COLUMN stall_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sniffers ADD COLUMN last_stall_at REAL;
```

`stall_count` accumulates across the device's lifetime. The user
explicitly asked for this — visibility into whether this is chronic
across sessions/days, not just "in flight right now."

`last_stall_at` is the most recent stall's epoch timestamp. Lets the
panel render "STALL ×3 (last: 30 min ago)" if we want to.

There's no auto-clear policy. The counter only resets when:
- The user right-clicks → "Clear stall counter" (UI follow-up; out of
  scope for v1).
- The sniffer row is deleted (replug + soft-delete or DB wipe).

## UI indicator

In the sniffer panel row, when `Sniffer.stall_count > 0`, render a
small amber badge **"STALL ×N"** next to the channel column.

- Color: amber (`#E0A040` ish). Distinguishable from CRC-fail red
  and active-channel green.
- Text: literal `STALL ×3` — gives the user the grep token directly.
- Tooltip: *"3 capture stalls recovered this device's lifetime.
  Search 'STALL' in `~/.btviz/capture.log` for details."*

For terminal state (3 attempts failed, give-up), upgrade the badge:

- Text: `STALL ×3 — replug`.
- Color: red.
- Tooltip: *"Capture stalled and 3 restart attempts failed. Replug
  this dongle to recover."*

The panel re-reads stall_count on every reload, so the indicator
persists across canvas reloads, capture stops, and btviz restarts —
exactly as requested.

## Architecture

### `SnifferState`

Add `last_packet_ts: float | None`. Updated by `_capture_loop` on
every successful packet read.

### `src/btviz/capture_log.py` (new)

Mirror of `src/btviz/cluster/cluster_log.py`. Configures a rotating
file handler under `~/.btviz/capture.log`. Module-level
`get_capture_logger()` returns a `logging.Logger` with the standard
btviz formatting.

### `src/btviz/capture/watchdog.py` (new)

```python
class StallWatchdog:
    """Periodically scans active sniffers for stalls.

    Owned by the canvas. Drives a QTimer at WATCHDOG_PERIOD_S
    cadence (default 10 s). On each tick, walks the coordinator's
    sniffer list and applies the detection rule. On hit:
    log → bump DB → request coordinator restart → update state.
    """
    def __init__(self, *, coordinator, repos, threshold_s=60.0,
                 max_attempts=3, min_gap_s=30.0):
        ...
    def tick(self) -> None: ...
    def reset_attempt_count(self, sniffer_id: str) -> None: ...
```

### `Coordinator.restart_one(short_id)`

New method on `src/btviz/capture/coordinator.py` exposed to the
watchdog. Stops the named `SnifferProcess`, instantiates a fresh
one with the same role + dongle, starts it. Returns `True` on
spawn success, `False` if discovery can't find the dongle anymore
(disconnected).

### Schema migration

`store.py` migration ladder gets `_V5_TO_V6_SQL`:

```python
_V5_TO_V6_SQL = """
ALTER TABLE sniffers ADD COLUMN stall_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sniffers ADD COLUMN last_stall_at REAL;
"""
```

`SCHEMA_VERSION` bumps to 6.

### `Repos.sniffers`

Three new methods:

```python
def bump_stall_counter(self, sniffer_id: int, when: float) -> None: ...
def clear_stall_counter(self, sniffer_id: int) -> None: ...   # for the future right-click action
def get_stall_state(self, sniffer_id: int) -> tuple[int, float | None]:
    """Return (stall_count, last_stall_at)."""
```

## What ships in v1

Real implementation:
- All of the architecture above.
- Detection + log + DB bump + restart + 3-attempt cap.
- Panel badge rendering.

Deferred:
- Right-click "Clear stall counter" UI action.
- Per-sniffer threshold tuning UI (the global default is fine for v1).
- USB-level health probe (try-write-byte) before declaring stuck.
- Cross-correlation: "all three pinned stalled at once" → likely
  kernel-level wedge → surface a single canvas-wide warning rather
  than three identical badges.

## Open questions

1. **Threshold default.** 60 s feels right but is empirical. After
   shipping, the log will tell us the distribution of "real" inter-
   packet gaps for a normal capture. If 60 s produces false-positive
   restarts, raise it.
2. **Automatic give-up vs. infinite retry.** I'd rather give up after
   3 attempts than thrash. But the user might prefer infinite retry
   with backoff for unattended overnight runs. Switch to backoff if
   chronic-but-recoverable stalls turn out to be common.
3. **Reset on user "Stop Capture / Start Capture"?** Currently the
   per-session attempt counter lives in `StallWatchdog` memory and
   resets when the watchdog itself is recreated. The DB
   `stall_count` is lifetime. I think this is right — session-scoped
   attempt counter for backoff, lifetime counter for chronic-detection.
