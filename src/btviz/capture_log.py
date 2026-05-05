"""Dedicated log handler for the capture pipeline.

Mirror of :mod:`btviz.cluster.cluster_log`. Lives at
``~/.btviz/capture.log``. The watchdog (and any future capture-side
narration) writes here. Token ``STALL`` is used in every line
related to subprocess wedge detection / restart so the user can:

    grep STALL ~/.btviz/capture.log

The user-facing UI indicator literally says ``STALL`` so the grep
pattern is self-documenting.

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


def configure_capture_log(
    *,
    log_file: Path | str | None = None,
    level: int = logging.INFO,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    propagate: bool = False,
) -> logging.Logger:
    """Attach a rotating file handler to the ``btviz.capture`` logger.

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
            fmt="%(asctime)s.%(msecs)03d  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger


def get_capture_logger() -> logging.Logger:
    """Return the capture logger without forcing configuration.

    If ``configure_capture_log`` hasn't been called, the returned
    logger has no handlers and messages go nowhere — the application
    chooses where the log lands.
    """
    return logging.getLogger(LOG_NAME)
