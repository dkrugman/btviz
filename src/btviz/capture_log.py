"""Dedicated log handler for the capture pipeline.

Mirror of :mod:`btviz.cluster.cluster_log`. Lives at
``~/.btviz/capture.log``. The watchdog and the capture lifecycle
narration write here. Token ``STALL`` is used in every line related
to subprocess wedge detection / restart so the user can:

    grep STALL ~/.btviz/capture.log

The user-facing UI indicator literally says ``STALL`` so the grep
pattern is self-documenting.

Three-tier logging:

  * **Default (INFO=20)**: high-level lifecycle only. Capture
    started / stopped, dongle-count summary, every STALL event.
    Quiet enough to leave on permanently.
  * **Verbose (VERBOSE=15)**: per-dongle discovery rows, role
    assignments, watchdog start, periodic summaries. Gated by the
    ``capture.verbose_log`` preference. Use when something's
    misbehaving and you want narrative context.
  * **Debug (DEBUG=10)**: per-tick watchdog eligibility, per-source
    throughput, anything chatty enough to flood the file under
    normal traffic. Gated by ``capture.debug_log``. Off by default.

The custom ``VERBOSE`` level slots between INFO and DEBUG so a
single integer threshold separates "narrative on" from "fire-hose
on". Calling ``configure_capture_log()`` also registers a
``log.verbose(...)`` shorthand on the Logger class so callers
don't have to write ``log.log(VERBOSE, ...)``.

Configured once at app start by calling ``configure_capture_log()``.
Subsequent calls are no-ops (idempotent).
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOG_NAME = "btviz.capture"
DEFAULT_LOG_DIR = Path.home() / ".btviz"
DEFAULT_LOG_FILE = DEFAULT_LOG_DIR / "capture.log"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5

_HANDLER_TAG = "_btviz_capture_handler"

# Wall-clock at the moment :py:func:`configure_capture_log` first
# runs. Used by the exit-event logger to compute btviz process
# uptime. ``None`` means the function hasn't been called yet —
# callers should fall back to "uptime unknown" rather than crash.
_PROGRAM_STARTED_AT: float | None = None


def get_program_started_at() -> float | None:
    """Wall-clock at the moment configure_capture_log first ran."""
    return _PROGRAM_STARTED_AT

#: Custom log level between INFO (20) and DEBUG (10). Reserved for
#: the verbose-but-not-chatty narration tier — per-dongle discovery
#: rows, watchdog start, role assignments, periodic summary lines.
#: Use ``log.verbose()`` at call sites; the shorthand is registered
#: by :py:func:`_register_verbose_level`.
VERBOSE = 15


def _register_verbose_level() -> None:
    """Idempotently register the VERBOSE log level + ``log.verbose()``.

    Safe to call multiple times — ``addLevelName`` is idempotent
    when the (level, name) pair matches, and the Logger monkeypatch
    only installs once. Pulled out of ``configure_capture_log`` so
    tests can register the level without standing up a file handler.
    """
    logging.addLevelName(VERBOSE, "VERBOSE")

    def verbose(self, msg, *args, **kwargs):
        if self.isEnabledFor(VERBOSE):
            self._log(VERBOSE, msg, args, **kwargs)

    if not hasattr(logging.Logger, "verbose"):
        logging.Logger.verbose = verbose  # type: ignore[attr-defined]


_register_verbose_level()


def configure_capture_log(
    *,
    log_file: Path | str | None = None,
    level: int = logging.INFO,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    propagate: bool = False,
) -> logging.Logger:
    """Attach a rotating file handler to the ``btviz.capture`` logger.

    Idempotent: safe to call multiple times. Returns the configured
    logger. The default ``level`` is INFO; ``__main__.py`` bumps it
    to ``VERBOSE`` or ``DEBUG`` based on the ``capture.verbose_log``
    and ``capture.debug_log`` preferences after the handler is in
    place.
    """
    _register_verbose_level()
    global _PROGRAM_STARTED_AT
    if _PROGRAM_STARTED_AT is None:
        import time as _time
        _PROGRAM_STARTED_AT = _time.time()
    logger = logging.getLogger(LOG_NAME)
    logger.setLevel(level)
    logger.propagate = propagate

    for h in logger.handlers:
        if getattr(h, _HANDLER_TAG, False):
            return logger

    path = Path(log_file) if log_file else DEFAULT_LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    setattr(handler, _HANDLER_TAG, True)
    # Level field appears in every line so grep '\bINFO\b' /
    # '\bVERBOSE\b' / '\bDEBUG\b' partitions the file by tier.
    # Useful when the user has verbose on and wants just the
    # lifecycle entries, or vice versa.
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s.%(msecs)03d  %(levelname)-7s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger


#: Mapping from the user-facing level name (the dropdown values in
#: ``capture.log_level``) to Python's numeric log level. Order in
#: the dropdown should mirror this dict — quietest first, loudest
#: last — so the UI reads naturally.
LEVEL_NAMES: dict[str, int] = {
    "error":   logging.ERROR,    # 40
    "warning": logging.WARNING,  # 30
    "info":    logging.INFO,     # 20
    "verbose": VERBOSE,          # 15
    "debug":   logging.DEBUG,    # 10
}


def resolve_level(name: str | int | None) -> int:
    """Convert a level name (or numeric level) to int. INFO on miss.

    Accepts the dropdown's string values, plain Python level ints,
    or ``None`` (which means "stick with INFO"). Case-insensitive
    on the string path so a stale ``capture.toml`` with ``"INFO"``
    instead of ``"info"`` still works.
    """
    if name is None:
        return logging.INFO
    if isinstance(name, int):
        return name
    return LEVEL_NAMES.get(str(name).lower(), logging.INFO)


def apply_capture_log_prefs(level: str | int | None = None) -> None:
    """Set the capture logger's level from the dropdown pref value.

    Called at startup from ``__main__.py`` and again from the prefs
    dialog's ``_on_save`` so changes take effect without a restart.
    Idempotent: re-applying the same level is a no-op (the
    confirmation message only lands when the level actually
    changed). Re-applies to all attached handlers so the change
    flows through immediately.

    On change, logs a confirmation line *at the new level* so the
    message is visible at the threshold the user just set — i.e.,
    setting ERROR still produces an ERROR-level confirmation that
    survives the new filter. Useful at the prefs-save callsite so
    the user sees the change land in capture.log.
    """
    target = resolve_level(level)
    logger = logging.getLogger(LOG_NAME)
    old_level = logger.level
    logger.setLevel(target)
    for h in logger.handlers:
        h.setLevel(target)
    if old_level != target:
        logger.log(
            target,
            "capture log level: %s (was %s)",
            logging.getLevelName(target).lower(),
            logging.getLevelName(old_level).lower(),
        )


def get_capture_logger() -> logging.Logger:
    """Return the capture logger without forcing configuration.

    If ``configure_capture_log`` hasn't been called, the returned
    logger has no handlers and messages go nowhere — the application
    chooses where the log lands.
    """
    return logging.getLogger(LOG_NAME)
