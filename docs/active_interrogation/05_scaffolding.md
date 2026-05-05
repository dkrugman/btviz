# Scaffolding committed in this PR

This PR ships the design + skeleton, not the working feature. What's
in the tree and what state it's in:

## Code

| Path | State | Purpose |
|---|---|---|
| `src/btviz/probe/__init__.py` | real | Module surface; re-exports types. |
| `src/btviz/probe/types.py` | real | `ProbeRequest`, `ProbeResult`, `GattCharObservation`, `GattService`, `ProbeOutcome`. Cross-thread queue payloads. |
| `src/btviz/probe/gatt_dictionary.py` | real | Tier-1 SIG UUIDs (GAP + Device Information) + human-name table + tier-1 read list. |
| `src/btviz/probe/storage.py` | partial — helpers real, `apply_result` stub | `value_hash` / `value_text` / `serialize_observation` are exercised by tests. `apply_result` raises `NotImplementedError`. |
| `src/btviz/probe/hci.py` | stub | `HciDriver` interface only. Real impl lands with the next PR via `pc-ble-driver-py`. |
| `src/btviz/probe/coordinator.py` | stub | Public interface (`submit`, `cancel`, `shutdown`) plus comments documenting the borrow/release dance with the capture coordinator. |
| `src/btviz/capture/roles.py` | edited | Added `Probe` role variant (TX-capable, used by the probe coordinator). `short_name` updated. |

## Schema

| Path | State | Purpose |
|---|---|---|
| `docs/active_interrogation/v5_to_v6.sql` | draft | Three tables (`gatt_values`, `device_gatt_chars`, `probe_runs`) + index. Will be lifted into `src/btviz/db/store.py` as `_V5_TO_V6_SQL` and `SCHEMA_VERSION` bumped to 6 when the first real consumer (probe coordinator) lands. Keeping it out of `store.py` for now means existing DBs don't migrate against unused tables. |

## Tests

| Path | State | Purpose |
|---|---|---|
| `tests/probe/__init__.py` | empty | package marker |
| `tests/probe/test_storage_helpers.py` | real | Covers `value_hash` (deterministic, distinct values → distinct hashes, empty bytes have a hash), `value_text` (printable UTF-8 round-trips, empty string vs None, binary returns None), `serialize_observation` (value path + error path + neither-set rejection). |

## Docs

| Path | Audience | Purpose |
|---|---|---|
| `00_overview.md` | new reader | Why we're doing this; reading order. |
| `01_initial_plan.md` | reviewer | First-pass design answering the original questions. |
| `02_critique.md` | reviewer | Self-review listing holes / questions. |
| `03_revised_plan.md` | reviewer | What I'd actually build, after critique. v1 manual-only, Tier-1 reads, schema and architecture decisions. |
| `04_classic_bt_integration.md` | reviewer | Phase-4 plan for Ubertooth / HackRF / `ice9-bluetooth-sniffer`. Deliberately less detailed than the BLE work. |
| `05_scaffolding.md` | this file | What's stub vs. real, how to land the next PR. |
| `v5_to_v6.sql` | next-PR author | Schema migration draft to lift into `store.py`. |

## Next PR (Phase 1)

In approximate order:

1. Add `pc-ble-driver-py` dependency to `pyproject.toml`. Pin to a
   specific connectivity-firmware build version. Document the
   flash procedure for the DK in a top-level README section.
2. Implement `HciDriver` (open / close / probe). Connect-by-address,
   service discovery, characteristic discovery, read by UUID for
   the Tier-1 char list, disconnect.
3. Implement `ProbeCoordinator.submit` / `run_request` /
   `cancel` / `shutdown`. Persistent QThread per the PR #83 pattern.
4. Add `borrow_tx_dongle` / `release_dongle` to
   `src/btviz/capture/coordinator.py`.
5. Lift the schema migration from `docs/.../v5_to_v6.sql` into
   `src/btviz/db/store.py`. Bump `SCHEMA_VERSION` to 6.
6. Implement `apply_result` against the new tables.
7. Wire the canvas: right-click on a stable-kind device card →
   "Probe device." Show `Probing…` status; render results in the
   expanded card.
8. Tests for `HciDriver` (mocked Nordic transport), coordinator
   (mocked driver), apply_result (in-memory sqlite).
9. Manual smoke: probe each of your hearing aids, an AirTag, a
   Find My beacon. Compare results to Nordic Scanner's GATT view.

## Open questions for review

These were raised in `02_critique.md` §G and are repeated here for
visibility:

1. **Pairing posture** — confirm "no, never pair." Locks out a few
   chars on stubborn devices but keeps btviz a passive-by-default
   tool with a narrow active surface.
2. **Auto-probe vs manual-only** — the revised plan picks
   manual-only for v1. Reasonable starting point but you may want
   "probe every newly-discovered cluster primary once" as an
   opt-in tier-zero auto-policy.
3. **Host-radio fallback (`bleak`)** — the revised plan rejects
   it. Reconsider if you ever want btviz to be useful on a
   machine with no DK plugged in.
4. **Classic BT timing** — phase 4 in this doc. Ok to push that
   far out, or do you want it sooner?
5. **DK flashing** — connectivity firmware needs a specific build
   matched to `pc-ble-driver-py`'s version. Do you want the next
   PR to ship a flashing helper script (`btviz flash-dk`), or is
   manual `nrfutil` instructions in the README enough?
