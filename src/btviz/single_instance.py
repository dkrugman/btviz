"""Single-instance enforcement for ``btviz canvas``.

Two btviz GUIs running against the same SQLite DB cause real
problems — both spawn ``nrf_sniffer_ble.py`` extcap subprocesses
for the same ``/dev/cu.usbmodem*`` ports (only one can hold the
USB-CDC fd, the other fails silently or races); both append to
``~/.btviz/capture.log`` via ``RotatingFileHandler`` (which is
not multi-process safe and corrupts on rotation race); both run
cluster passes on the same rows; SQLite WAL mode tolerates the
DB writes but throws lock-contention errors under heavy ingest.

Solution: kernel-managed advisory file lock keyed on the DB
path. Acquire on canvas launch with ``flock(LOCK_EX | LOCK_NB)``;
exit cleanly with a clear message if the lock is held. Auto-
released on process death — no stale-file cleanup needed.

Scope: only ``btviz canvas``. ``btviz ingest`` is short-lived
and tolerable to race with a running canvas (SQLite WAL handles
the DB side; ingest doesn't write to capture.log lifecycle
events). ``btviz drain`` is read-only on the DB.

Platform: POSIX (macOS, Linux). Windows would need
``msvcrt.locking`` and is deferred until Windows support lands;
on a non-POSIX platform, this module's lock is a no-op so the
canvas still launches but without single-instance protection.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import IO


@dataclass
class LockResult:
    """Outcome of an acquire attempt.

    ``acquired`` is True on success — the caller must keep the
    returned ``file_handle`` alive for the lifetime of the
    process so the lock isn't released early. ``existing_pid`` is
    populated when ``acquired=False`` and the lock holder wrote
    its PID into the lock file (best-effort; may be None on a
    legacy lock file from a prior version).
    """
    acquired: bool
    file_handle: IO | None
    existing_pid: int | None
    lock_path: Path


def acquire_db_lock(db_path: Path) -> LockResult:
    """Try to claim the canvas single-instance lock for this DB.

    Lock file lives at ``<db_path>.lock``. On success, writes our
    PID into the file (debug aid; not load-bearing) and returns
    a ``LockResult`` whose ``file_handle`` MUST be retained by
    the caller — closing it releases the lock.

    On a non-POSIX platform (Windows), returns ``acquired=True``
    with no actual lock — better to launch unprotected than to
    refuse on a platform where we don't yet enforce.
    """
    lock_path = Path(str(db_path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        # Non-POSIX (Windows). Skip enforcement; return success
        # so the canvas still launches. A future Windows port
        # would gate on ``msvcrt.locking`` here.
        return LockResult(
            acquired=True, file_handle=None, existing_pid=None,
            lock_path=lock_path,
        )

    # Open in r+ if it exists, else w+. Read-write is required
    # so the kernel honours flock; pure write-only descriptors
    # work too but r+ lets us read the existing PID for the
    # error message.
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Lock held by another process. Read the PID it wrote
        # so we can show it in the dialog.
        existing_pid: int | None = None
        try:
            fh.seek(0)
            text = fh.read().strip()
            if text.isdigit():
                existing_pid = int(text)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass
        return LockResult(
            acquired=False, file_handle=None,
            existing_pid=existing_pid, lock_path=lock_path,
        )

    # We hold the lock. Stamp our PID for the next conflicting
    # launch's dialog message. Truncate first so a prior PID
    # doesn't linger appended.
    try:
        import os
        fh.seek(0)
        fh.truncate(0)
        fh.write(f"{os.getpid()}\n")
        fh.flush()
    except OSError:
        # Best-effort; lock-holding still works without the PID.
        pass
    return LockResult(
        acquired=True, file_handle=fh, existing_pid=None,
        lock_path=lock_path,
    )


def conflict_message(result: LockResult) -> str:
    """Format the conflict-time message for a held lock.

    Used by the canvas's ``run_canvas`` to populate the dialog.
    Pulled out as a helper so tests can pin the message format
    without driving the dialog widget itself.
    """
    pid_part = (
        f" (PID {result.existing_pid})"
        if result.existing_pid is not None else ""
    )
    return (
        f"btviz is already running on this database{pid_part}.\n\n"
        f"Close the other btviz window before launching another one. "
        f"Two instances on the same DB cause sniffer port collisions "
        f"(only one can read each USB-CDC port), corrupted log "
        f"rotation, and cluster-run interleaving.\n\n"
        f"Use --db <path> to point btviz at a different database "
        f"file if you really want a second instance."
    )
