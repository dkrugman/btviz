"""Storage adapter for probe results.

Translates a :class:`ProbeResult` into rows in ``probe_runs``,
``device_gatt_chars``, and ``gatt_values``.

**Stub.** Schema is defined in
``src/btviz/db/migrations/v5_to_v6.sql``; this module's
``apply_result`` will execute the inserts/updates against the
main-thread sqlite connection. Worker emits a ``ProbeResult``
back to the main thread, the main thread calls ``apply_result``.
"""
from __future__ import annotations

import hashlib

from .types import GattCharObservation, ProbeResult


def value_hash(value: bytes) -> str:
    """Content-addressed hash for ``gatt_values.value_hash``.

    SHA-1 chosen for size — gatt values are small (<512 bytes for
    standard chars) and we don't need cryptographic resistance, just
    deduplication. Hex-encoded to make the column easy to read in
    ad-hoc sqlite queries.
    """
    return hashlib.sha1(value).hexdigest()


def value_text(value: bytes) -> str | None:
    """Try to decode as UTF-8 for ``gatt_values.value_text``.

    Returns the decoded string when it round-trips cleanly,
    otherwise None. Storage keeps the raw blob in ``value_blob``
    regardless, so a None text column means "not human-readable"
    not "no value."
    """
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.isprintable() and text != "":
        return None
    return text


def serialize_observation(obs: GattCharObservation) -> dict:
    """Convert one observation into a kwargs dict for the DB row.

    Exactly one of ``value_hash`` / ``att_error`` is non-None,
    matching the table CHECK constraint.
    """
    if obs.value is not None:
        return {
            "service_uuid": obs.service_uuid,
            "char_uuid": obs.char_uuid,
            "value_hash": value_hash(obs.value),
            "att_error": None,
        }
    if obs.att_error is not None:
        return {
            "service_uuid": obs.service_uuid,
            "char_uuid": obs.char_uuid,
            "value_hash": None,
            "att_error": obs.att_error,
        }
    raise ValueError(
        "GattCharObservation must have either value or att_error set"
    )


def apply_result(store, result: ProbeResult) -> int:
    """Persist a probe result and return the new ``probe_runs.id``.

    **Stub.** Real implementation lands with the migration's first
    real consumer. Signature is fixed: ``store`` is the main-thread
    ``Store`` instance, and we return the row id so callers can
    correlate.
    """
    raise NotImplementedError(
        "probe.storage.apply_result: stub. See "
        "docs/active_interrogation/05_scaffolding.md for status."
    )
