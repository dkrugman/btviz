# The Aggregator

The aggregator is the orchestrator that takes a candidate device pair,
runs the relevant signals, combines their scores, and writes a
decision to `device_clusters`.

## Three layers

```
┌──────────────────────────────────────────────────────────────────┐
│  ClusterRunner                                                    │
│  - schedules itself (idle / cron / explicit run)                 │
│  - generates candidate pairs (cheap pre-filter)                  │
│  - calls cluster_pair() for each candidate                       │
│  - applies transitive closure                                    │
│  - persists to device_clusters                                   │
└────────────────────────────┬──────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  cluster_pair(ctx, dev_a, dev_b) -> Decision | None              │
│  - profile selection                                              │
│  - irk short-circuit                                              │
│  - weighted-sum of applicable signals                             │
│  - threshold comparison                                           │
└────────────────────────────┬──────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  signals/*.py — independent, stateless, side-effect-free         │
└──────────────────────────────────────────────────────────────────┘
```

## Layer 1 — `cluster_pair`

```python
@dataclass(frozen=True)
class Decision:
    merge: bool
    score: float                                # final weighted-mean
    signals: dict[str, tuple[float, float]]     # name -> (score, weight)
    profile: str                                # which profile decided
    abort_reason: str | None = None             # set when merge is False
                                                # because of a constraint,
                                                # not a low score


def cluster_pair(ctx, dev_a, dev_b):
    profile = pick_profile(dev_a, dev_b)
    if profile is None:
        return None  # cross-class or no applicable profile

    # IRK short-circuit. Cryptographic certainty trumps all behavioral
    # evidence. If the IRK signal returns 1.0, both devices resolve to
    # the same identity → unconditional merge. If it returns 0.0,
    # they resolve to *different* known identities → hard rejection
    # (do NOT fall through to behavioral signals; we are crypto-certain
    # they are different).
    if "irk_resolution" in ctx.signals:
        irk = ctx.signals["irk_resolution"]
        if irk.applies_to(ctx, dev_a, dev_b):
            irk_score = irk.score(ctx, dev_a, dev_b)
            if irk_score == 1.0:
                return Decision(merge=True, score=1.0,
                                signals={"irk_resolution": (1.0, 1.0)},
                                profile=profile.name)
            if irk_score == 0.0:
                return Decision(merge=False, score=0.0,
                                signals={"irk_resolution": (0.0, 1.0)},
                                profile=profile.name,
                                abort_reason="irk_mismatch")
            # irk_score is None → fall through to behavioral signals

    weighted_sum = 0.0
    total_weight = 0.0
    contributions = {}
    missing_required = []

    for sig_name, weight in profile.weights.items():
        sig = ctx.signals.get(sig_name)
        if sig is None:
            # Signal module not loaded (probably a future signal).
            # Treat as not applicable; if it was required, abort.
            if sig_name in profile.required:
                missing_required.append(sig_name)
            continue
        if not sig.applies_to(ctx, dev_a, dev_b):
            if sig_name in profile.required:
                missing_required.append(sig_name)
            continue
        s = sig.score(ctx, dev_a, dev_b,
                      params=profile.params.get(sig_name, {}))
        if s is None:
            if sig_name in profile.required:
                missing_required.append(sig_name)
            continue
        weighted_sum += s * weight
        total_weight += weight
        contributions[sig_name] = (s, weight)

    if missing_required:
        return Decision(merge=False, score=0.0, signals=contributions,
                        profile=profile.name,
                        abort_reason=f"missing_required:{','.join(missing_required)}")

    if total_weight == 0.0:
        return None  # no opinion from any signal

    final = weighted_sum / total_weight
    return Decision(
        merge=(final >= profile.threshold),
        score=final,
        signals=contributions,
        profile=profile.name,
    )
```

**Why weighted *mean* not weighted *sum*?** The mean is robust to
absent signals: if a signal contributes `None`, the remaining signals
still produce a valid score in `[0.0, 1.0]` rather than getting
dragged down by a missing component. This is critical because the
data sparsity is uneven — some devices have rich `device_ad_history`
and shallow `packets`, others the opposite.

**Why a hard threshold rather than a probabilistic merge?** Because
the result is consumed by the UI as a discrete operation ("collapse
this row into that one") and by the user as a yes/no judgment. We
*also* persist the score, so a future UI could show "78% confident"
or sort merges by certainty.

## Layer 2 — `ClusterRunner`

```python
class ClusterRunner:
    def __init__(self, ctx: ClusterContext):
        self.ctx = ctx

    def run_once(self):
        candidates = self._generate_candidates()
        decisions = []
        for a, b in candidates:
            d = cluster_pair(self.ctx, a, b)
            if d is None:
                continue
            decisions.append((a, b, d))
        merged = self._transitive_closure(decisions)
        self._persist(merged)
```

### Candidate generation

The naive "all pairs" approach is O(n²) in the device count — at 5000
devices that's 12.5M pairs per run. We need a cheap pre-filter that
reduces this to O(n log n) or better while not missing real merges.

**Filters, in order:**

1. **Same device class.** Already enforced in `pick_profile` but
   filtering at candidate-generation time avoids wasted scoring.
2. **Recent overlap.** Both devices observed within the last
   `recent_window` (per-class; default 1 hour). Devices not seen
   recently almost never need re-clustering — and if they're stale,
   the existing cluster decisions are fine.
3. **Same sniffer fingerprint.** At least one shared sniffer in the
   recent observation set. Devices that have never been heard by the
   same sniffer cannot be the same physical device (modulo sniffer
   re-positioning, which is rare and out-of-scope).
4. **RPA prefix match.** Two random_resolvable addresses with the
   same first 2 bits of `prand` (the high bits encoding the address
   type) are eligible. This is a tiny filter but free.
5. **Coarse fingerprint bucket.** Hash `(device_class, tx_power,
   sorted(top_3_service_uuids))` into a bucket; only pair within
   bucket. This is the biggest win — typically reduces candidate
   count by 10-50× without missing real merges.

The candidate generator is itself pluggable so cheap filters can be
swapped or extended.

### Transitive closure

If `(A, B)` and `(B, C)` are both merge decisions, then `A`, `B`, `C`
form one cluster. Implementation: union-find over the merge edges.

Edge cases:

- **Conflicting decisions.** If `(A, B)` says merge and `(A, C)`
  says don't-merge but `(B, C)` says merge — what about `(A, C)`?
  The framework's answer: union-find gives them all the same
  cluster, the negative `(A, C)` decision is overridden. Reasoning:
  the strongest evidence wins; `(A, B)` and `(B, C)` agreeing is
  more evidence than `(A, C)` disagreeing.
- **High-confidence conflicts.** If `(A, B)` is an IRK-certain
  merge (score 1.0) but `(A, C)` is also IRK-certain non-merge
  (score 0.0, irk_mismatch), the cluster is `{A, B}` and `C` is
  separate. IRK decisions are absolute — no override possible by
  weaker evidence.
- **Manual override.** A "Merge" or "Unmerge" written by the user
  sets a row in `device_clusters` with `source='manual'`. Manual
  decisions outrank automatic ones; the runner skips pairs whose
  user-set decision exists.

## Layer 3 — Signals (covered in `01_signals.md`)

A signal module is a single file under `src/btviz/cluster/signals/`
exporting one `Signal`-protocol-conforming object. The module is
discovered at import time (`signals/__init__.py` enumerates the
package) and registered into `ctx.signals` keyed by `signal.name`.

## ClusterContext

```python
@dataclass
class ClusterContext:
    db: sqlite3.Connection                  # WAL-mode handle
    signals: dict[str, Signal]              # name -> instance
    profiles: dict[str, ClassProfile]       # class -> profile
    irks: list[IdentityKey]                 # imported IRKs (may be [])
    now: datetime                           # frozen "now" for deterministic runs
    cache: dict                             # per-device caches
                                            # (per-sniffer rssi sigs, etc.)
```

`now` is frozen at the start of a run so every signal sees the same
clock — important for `rotation_cohort`'s `gap` calculation, where a
slipping clock would change the answer mid-run.

`cache` is per-device, scoped to one `run_once()` call. Signals
write through this cache transparently; nothing persists.

## When the runner runs

Three trigger modes:

1. **Idle-trigger.** When the live-capture thread reports no new
   packets for N seconds and there are pending recent observations,
   schedule a `run_once`. Cheapest because clustering doesn't compete
   with packet ingest for DB write contention.
2. **Cron.** Every 10 minutes regardless of activity. Catches the
   case where capture is steady but clusters need to be updated as
   evidence accumulates over time.
3. **Explicit.** UI button "Re-cluster now" — useful after the user
   imports an IRK, changes a profile threshold, or manually merges.

## Persistence

`device_clusters` schema (covered in `04_schema.md`):

```sql
CREATE TABLE device_clusters (
    cluster_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT,                       -- user-set or auto-derived
    created_at  REAL NOT NULL,              -- when the cluster came into existence
    last_decided_at REAL NOT NULL,          -- last time the runner touched it
    source      TEXT NOT NULL DEFAULT 'auto'-- 'auto' | 'manual' | 'irk'
);

CREATE TABLE device_cluster_members (
    cluster_id   INTEGER NOT NULL,
    device_id    INTEGER NOT NULL,
    score        REAL,                       -- final score that placed this member
    contributions TEXT,                      -- JSON: {sig_name: [score, weight]}
    profile      TEXT,                       -- which profile decided
    decided_at   REAL NOT NULL,
    decided_by   TEXT NOT NULL DEFAULT 'auto', -- 'auto' | 'manual' | 'irk'
    PRIMARY KEY (cluster_id, device_id),
    FOREIGN KEY (cluster_id) REFERENCES device_clusters(cluster_id),
    FOREIGN KEY (device_id) REFERENCES devices(device_id)
);

CREATE INDEX idx_dcm_device ON device_cluster_members(device_id);
```

Each member row stores its own score + contributions JSON, so the UI
can show "this device was added to the cluster with 84% confidence,
based on rotation_cohort=0.91, rssi_signature=0.78, …"

## What the canvas displays

- One scene item per cluster, not per device. The cluster's label is
  the most-confident member's label (or a user-set override).
- The icon is the most-confident member's icon. (For mixed clusters
  this rarely matters because cross-class merges are forbidden.)
- A small badge in the top-right: "3" meaning 3 RPAs collapsed.
- Tooltip lists the members and per-member scores.
- Right-click → "Show members" expands the cluster into individual
  device boxes (transient; collapsing back via right-click).
- Right-click → "Merge selected" performs a manual merge across the
  selected items.
- Right-click → "Unmerge" removes a member from a cluster (writing
  a `source='manual'` exclusion).

## Failure isolation

Each signal runs in a try/except inside `cluster_pair`. A signal that
raises is treated as if it returned `None`; the exception is logged
but does not abort the run. This is critical because signals will
frequently encounter unexpected data — a malformed Continuity payload,
a packet with no RSSI, a DB row with missing columns from an old
migration — and we'd rather the rest of the framework keep working.

```python
try:
    s = sig.score(ctx, dev_a, dev_b, params=...)
except Exception as exc:
    log.warning("signal %s raised on (%s, %s): %s",
                sig_name, dev_a.id, dev_b.id, exc)
    continue  # treat as None, do not include in weighted sum
```

Per-signal exception counters (`ctx.cache['_exc_counts']`) surface in
the logs every run; if a signal is consistently failing, the operator
sees a clear signal to investigate without a crashed framework.
