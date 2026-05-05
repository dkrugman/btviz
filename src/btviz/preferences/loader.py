"""TOML load + save with validation against ``schema.SCHEMA``.

Resolution order for any field's value: ``env > TOML > default``.

* ``env`` checks ``os.environ[field.env]`` if ``field.env`` is set.
  Used for the legacy ``BTVIZ_DB_PATH`` / ``BTVIZ_NRF_EXTCAP``
  contracts. New fields shouldn't grow env-var support unless there's
  a CI / dev-loop need.
* ``TOML`` is the file at ``<prefs_dir>/<file>.toml`` for that field's
  ``file`` attribute. Out-of-range or wrong-type values fall back to
  the default and emit a one-line warning to ``capture.log`` so the
  user can see when their file was ignored.
* ``default`` is the schema-defined fallback.
"""
from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Mapping
from logging import getLogger
from pathlib import Path
from typing import Any

from .schema import SCHEMA, Field, fields_for_file, files

log = getLogger("btviz.preferences")


# ──────────────────────────────────────────────────────────────────────
# Path resolution
# ──────────────────────────────────────────────────────────────────────

def default_prefs_dir() -> Path:
    """Where the per-file TOMLs live. ``~/.btviz/preferences``."""
    return Path.home() / ".btviz" / "preferences"


def platform_default_db_path() -> Path:
    """Mirror of ``btviz.db.store.default_db_path`` without the import.

    Kept here so the loader can resolve the ``"<platform default>"``
    sentinel without pulling in ``store.py`` (which itself reads
    preferences during ``Store.__init__`` — the cycle would crash).
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "btviz"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home())) / "btviz"
    else:
        base = (
            Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
            / "btviz"
        )
    return base / "btviz.db"


def _resolve_path_default(field: Field) -> Any:
    """Translate sentinel default values to real paths."""
    if field.default == "<platform default>":
        return str(platform_default_db_path())
    if isinstance(field.default, str) and field.default.startswith("~"):
        return str(Path(field.default).expanduser())
    return field.default


# ──────────────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────────────

def _validate(field: Field, raw: Any) -> tuple[Any, str | None]:
    """Coerce ``raw`` to ``field.type`` and bound-check.

    Returns ``(value, error)`` where ``error`` is ``None`` on success
    or a short reason string on failure. The caller substitutes the
    default and logs the reason.
    """
    if raw is None:
        return field.default, None

    # Type coercion. TOML's int/float/bool/str map directly to Python's
    # so we mostly just need to allow int↔float crossovers (a TOML
    # ``stall_threshold_s = 60`` reads as int but the field is float).
    expected = field.type
    try:
        if expected is bool and not isinstance(raw, bool):
            return field.default, f"expected bool, got {type(raw).__name__}"
        if expected is int and isinstance(raw, bool):
            return field.default, "expected int, got bool"
        if expected is int:
            value = int(raw)
        elif expected is float:
            value = float(raw)
        elif expected is str:
            value = str(raw)
        elif expected is bool:
            value = bool(raw)
        else:
            value = raw
    except (TypeError, ValueError):
        return field.default, f"could not coerce {raw!r} to {expected.__name__}"

    if field.enum is not None and value not in field.enum:
        return field.default, f"{value!r} not in allowed values {field.enum}"
    if field.min is not None and value < field.min:
        return field.default, f"{value} below min {field.min}"
    if field.max is not None and value > field.max:
        return field.default, f"{value} above max {field.max}"

    return value, None


# ──────────────────────────────────────────────────────────────────────
# Load
# ──────────────────────────────────────────────────────────────────────

def _read_toml(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("failed to read %s: %s — using defaults", path, e)
        return {}


def load_all(prefs_dir: Path | None = None) -> dict[str, Any]:
    """Resolve every schema field. Returns a flat dict keyed by ``Field.key``.

    Order: env var (if set) > TOML file (if present) > schema default.
    """
    prefs_dir = prefs_dir or default_prefs_dir()
    by_file: dict[str, Mapping[str, Any]] = {}
    for fname in files():
        by_file[fname] = _read_toml(prefs_dir / f"{fname}.toml")

    out: dict[str, Any] = {}
    for f in SCHEMA:
        # 1. env var override
        if f.env:
            env_val = os.environ.get(f.env)
            if env_val not in (None, ""):
                value, err = _validate(f, env_val)
                if err is None:
                    out[f.key] = value
                    continue
                log.warning("env %s=%r ignored: %s", f.env, env_val, err)

        # 2. TOML file
        section = by_file.get(f.file, {}).get(f.section, {})
        if isinstance(section, Mapping) and f.name in section:
            value, err = _validate(f, section[f.name])
            if err is None:
                out[f.key] = value
                continue
            log.warning(
                "%s.toml [%s].%s = %r ignored: %s — using default",
                f.file, f.section, f.name, section[f.name], err,
            )

        # 3. default (with path-sentinel resolution)
        out[f.key] = _resolve_path_default(f)

    return out


# ──────────────────────────────────────────────────────────────────────
# Save
# ──────────────────────────────────────────────────────────────────────

def save_all(values: Mapping[str, Any], prefs_dir: Path | None = None) -> None:
    """Write the schema-known values back out across the per-file TOMLs.

    Writes one file per ``Field.file`` group, with ``[section]`` headers
    derived from the schema. Values not present in ``values`` use the
    schema default. Existing comments / hand-edits in the TOMLs are
    NOT preserved — the dialog rewrites the whole file.
    """
    prefs_dir = prefs_dir or default_prefs_dir()
    prefs_dir.mkdir(parents=True, exist_ok=True)

    for fname in files():
        path = prefs_dir / f"{fname}.toml"
        lines: list[str] = [
            f"# btviz preferences — {fname}",
            "# Auto-generated. Hand-edits are preserved on read but lost",
            "# the next time the Preferences dialog saves this file.",
            "",
        ]
        # Group by section in declaration order.
        seen_sections: list[str] = []
        sections: dict[str, list[Field]] = {}
        for f in fields_for_file(fname):
            if f.section not in seen_sections:
                seen_sections.append(f.section)
                sections[f.section] = []
            sections[f.section].append(f)

        for section in seen_sections:
            lines.append(f"[{section}]")
            for f in sections[section]:
                value = values.get(f.key, _resolve_path_default(f))
                lines.append(f"# {f.label} — {f.description}")
                lines.append(_emit_kv(f.name, f.type, value))
                lines.append("")
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")


def _emit_kv(name: str, type_: type, value: Any) -> str:
    """Render one TOML ``name = value`` line."""
    if type_ is bool:
        return f"{name} = {'true' if value else 'false'}"
    if type_ is int:
        return f"{name} = {int(value)}"
    if type_ is float:
        return f"{name} = {float(value)}"
    # str — TOML basic-string quoting. Backslashes / quotes are rare
    # in our values (paths, names) but escape defensively.
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'{name} = "{s}"'
