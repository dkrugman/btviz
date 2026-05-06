"""Dedicated log handler for the cluster framework.

Run-narration (human-readable INFO lines) and per-pair decisions
(JSON-per-line) share one file at ``~/.btviz/cluster.log``. This
keeps causality intact when reading top-to-bottom, and remains
machine-parseable via:

    grep -E '  decision ' ~/.btviz/cluster.log \\
        | sed 's/^.* decision //' | jq

Configured once at app start by calling ``configure_cluster_log()``.
Subsequent calls are no-ops (idempotent, so import-time setup in
tests is safe).
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOG_NAME = "btviz.cluster"
DEFAULT_LOG_DIR = Path.home() / ".btviz"
DEFAULT_LOG_FILE = DEFAULT_LOG_DIR / "cluster.log"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5

_HANDLER_TAG = "_btviz_cluster_handler"


def configure_cluster_log(
    *,
    log_file: Path | str | None = None,
    level: int = logging.INFO,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    propagate: bool = False,
) -> logging.Logger:
    """Attach a rotating file handler to the ``btviz.cluster`` logger.

    Idempotent: safe to call multiple times. Returns the configured logger.
    """
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
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger


def get_cluster_logger() -> logging.Logger:
    """Return the cluster logger without forcing configuration.

    If ``configure_cluster_log`` hasn't been called, this returns a
    logger with no handlers — messages go nowhere. The runner calls
    this rather than configuring on its own so the application gets
    to choose where the log lands.
    """
    return logging.getLogger(LOG_NAME)


def apply_cluster_log_prefs(level: str | int | None = None) -> None:
    """Set the cluster logger's level from the dropdown pref value.

    Mirror of :py:func:`btviz.capture_log.apply_capture_log_prefs`.
    Reuses the same level-name → numeric-level table so the two
    dropdowns are consistent. ``verbose`` on the cluster dropdown
    is currently equivalent to ``info`` (the cluster runner has no
    VERBOSE-tier emissions yet) but the option exists for UI
    parity and so future VERBOSE-tier cluster narration (e.g.,
    per-class headers, per-signal timing) can be added without a
    schema migration.

    On change, logs a confirmation line *at the new level* so the
    message survives the new filter — e.g., setting ERROR still
    produces an ERROR-tier confirmation. The user sees the change
    take effect by glancing at cluster.log after Save.
    """
    # Imported here to avoid a circular at module load (capture_log
    # is independent of cluster, so cluster_log → capture_log is
    # fine, but the import chain stays clean if we keep it lazy).
    from ..capture_log import resolve_level
    target = resolve_level(level)
    logger = logging.getLogger(LOG_NAME)
    old_level = logger.level
    logger.setLevel(target)
    for h in logger.handlers:
        h.setLevel(target)
    if old_level != target:
        logger.log(
            target,
            "cluster log level: %s (was %s)",
            logging.getLevelName(target).lower(),
            logging.getLevelName(old_level).lower(),
        )
