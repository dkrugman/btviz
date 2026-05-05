# User preferences

Single-source-of-truth schema. TOML on disk for portability; auto-generated
Qt dialog so adding a knob doesn't mean writing UI code.

## Files

```
~/.btviz/preferences/
├── general.toml      Paths (DB, logs), app-level
├── capture.toml      Sniffer subprocess flags + watchdog
├── cluster.toml      Cluster runner thresholds, caps, cadence
├── canvas.toml       Display: aging, flash durations, default views
└── probe.toml        (placeholder; populated by active-interrogation work)
```

Each TOML is generated on first run from defaults, then editable by hand
or via the dialog. Dialog rewrites the file on save; comments/order are
not preserved (we re-emit from schema), but every knob has its
description + range expressed in the dialog so the file is mostly a
machine artefact.

## Schema as code

`src/btviz/preferences/schema.py` defines every knob:

```python
@dataclass(frozen=True)
class Field:
    key: str              # dotted path, e.g. "watchdog.stall_threshold_s"
    file: str             # which TOML, e.g. "capture"
    section: str          # TOML [section]
    name: str             # in-file key, e.g. "stall_threshold_s"
    type: type            # int / float / bool / str
    default: Any
    label: str            # human-readable for dialog
    description: str      # tooltip
    min: float | None = None
    max: float | None = None
    enum: tuple[Any, ...] | None = None
    requires_restart: bool = False
```

`SCHEMA: tuple[Field, ...]` is the canonical list. Code that reads a
preference does:

```python
from btviz.preferences import get_prefs
threshold = get_prefs().get("watchdog.stall_threshold_s")
```

`get_prefs()` returns a singleton `Preferences` object that owns the
loaded values (TOML overlaid on defaults). It validates on load, falls
back to defaults on out-of-range values, and emits a warning to the
capture log when it does.

## Precedence

`env var > TOML file > code default`

Env vars are deliberate per-process overrides (CI, ad-hoc test runs).
TOML is the user's "set once and forget it." Defaults are the floor.
Today only `BTVIZ_DB_PATH` and `BTVIZ_NRF_EXTCAP` are env-overridable;
the schema marks those fields with the env name so the loader can
respect them.

## Dialog

`PreferencesDialog` is a `QDialog` with a left-side category list and
a right-side scroll area. The form rows are auto-generated from the
schema:

- `bool` → checkbox
- `int` / `float` with `min`/`max` → spinbox / double-spinbox
- `enum` → combobox
- `str` (path) → line edit + Browse button
- `str` (other) → line edit

Each row has the field's `label` and `description` (as tooltip + small
italic line under the control). `requires_restart=True` shows a small
"⟲ requires restart" suffix.

Buttons: **Save & Close**, **Reset section**, **Open TOML…**, **Cancel**.

## What's wired in v1 vs. left hardcoded

Wired (visible + editable):

- General: DB path, log dir
- Capture: 5 sniffer subprocess flags
- Watchdog: threshold, period, max_attempts, min_gap
- Cluster: max_per_class, collapse_threshold
- Canvas: stale_window_default, recency_fresh_s, recency_dormant_s,
  recency_min_opacity, stable_class_min_packets

Stayed hardcoded (out of scope until/unless asked):

- All UI pixel/layout values (`_BOX_W`, `_HEADER_H`, etc. — geometry,
  not preferences)
- Internal Z-order constants
- Animation timing / fade durations (visual feel; tweaking via TOML
  is more friction than value)
- Hard-coded SIG GATT UUID dictionary (in code on purpose; see
  `docs/active_interrogation/01_initial_plan.md` §gatt_dictionary)

Adding a knob later = one schema entry + one consumer call. No UI code,
no migration.
