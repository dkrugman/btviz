"""Pair-level aggregator.

Takes a candidate device pair, runs the relevant signals, and
returns a Decision. The runner that loops over all candidates +
applies transitive closure lives in ``runner.py`` (separate module
because it carries DB-access concerns).
"""

from __future__ import annotations

import logging

from .base import ClusterContext, Decision, Device, pick_profile

log = logging.getLogger(__name__)


def cluster_pair(
    ctx: ClusterContext, a: Device, b: Device
) -> Decision | None:
    """Score a pair and decide whether to merge.

    Returns:
        Decision with merge=True, merge=False, or None.
        None means "no opinion" — neither evidence for nor against.
        merge=False with abort_reason set means "actively rejected"
        (cryptographic mismatch or missing-required signal).

    Thin wrapper over ``cluster_pair_with_reason`` for callers (tests,
    external use) that don't need the abstain-reason side channel.
    """
    decision, _reason, _contribs = cluster_pair_with_reason(ctx, a, b)
    return decision


def cluster_pair_with_reason(
    ctx: ClusterContext, a: Device, b: Device
) -> tuple[
    Decision | None,
    str | None,
    dict[str, tuple[float, float]] | None,
]:
    """Score a pair; return (Decision, abstain_reason, contributions).

    Mirrors ``cluster_pair`` but surfaces the *reason* an abstain
    happened plus any partial signal contributions accumulated before
    the abstain. The runner uses this to roll abstains up by
    class+reason in the summary AND to emit a per-pair debug line
    showing exactly what scored.

    Return shape:
      - merge decided: (Decision, None, None) — contributions already
        live inside the Decision
      - abort (rejected): (Decision(merge=False, abort_reason=...),
        None, None) — same as above
      - abstain: (None, reason, contribs_or_None) — contribs is the
        partial dict of {sig_name: (score, weight)} accumulated
        before the abstain, or None if no signal contributed at all
    """
    profile = pick_profile(ctx, a, b)
    if profile is None:
        return None, "cross_class", None

    irk_short = _try_irk_short_circuit(ctx, a, b, profile.name)
    if irk_short is not None:
        return irk_short, None, None

    weighted_sum = 0.0
    total_weight = 0.0
    contributions: dict[str, tuple[float, float]] = {}
    missing_eventually: list[str] = []
    missing_for_merge: list[str] = []

    for sig_name, weight in profile.weights.items():
        sig = ctx.signals.get(sig_name)
        if sig is None:
            if sig_name in profile.required_for_merge:
                missing_for_merge.append(sig_name)
            elif sig_name in profile.required_eventually:
                missing_eventually.append(sig_name)
            continue

        try:
            applies = sig.applies_to(ctx, a, b)
        except Exception as exc:
            log.warning(
                "signal %s.applies_to raised on (%s, %s): %s",
                sig_name, a.id, b.id, exc,
            )
            applies = False

        if not applies:
            if sig_name in profile.required_for_merge:
                missing_for_merge.append(sig_name)
            elif sig_name in profile.required_eventually:
                missing_eventually.append(sig_name)
            continue

        try:
            s = sig.score(ctx, a, b, params=profile.params.get(sig_name, {}))
        except Exception as exc:
            log.warning(
                "signal %s.score raised on (%s, %s): %s",
                sig_name, a.id, b.id, exc,
            )
            s = None

        if s is None:
            if sig_name in profile.required_for_merge:
                missing_for_merge.append(sig_name)
            elif sig_name in profile.required_eventually:
                missing_eventually.append(sig_name)
            continue

        s = max(-1.0, min(1.0, float(s)))
        weighted_sum += s * weight
        total_weight += weight
        contributions[sig_name] = (s, weight)

    if missing_for_merge:
        return Decision(
            merge=False,
            score=0.0,
            signals=contributions,
            profile=profile.name,
            abort_reason=f"missing_required_for_merge:{','.join(missing_for_merge)}",
        ), None, None

    # Decisive-signal short circuit. If any signal listed in
    # ``profile.decisive_signals`` scored at or above
    # ``decisive_threshold`` AND no signal returned a strong
    # negative (below ``negative_block_threshold``), merge regardless
    # of total weight or weighted-sum threshold. Implements the
    # user-named heuristic "one strong signal + no conflicts → merge"
    # so a near-perfect handoff or co-emission can decide on its own
    # even when it's the only signal that fired in a given pair.
    #
    # Ordering matters: this runs before missing_eventually so a
    # decisive signal can rescue a pair that would otherwise abstain
    # for lack of (e.g.) rotation_cohort data — when the decisive
    # signal already proves same-device, the missing-eventually
    # signal would have nothing to add.
    if profile.decisive_signals and contributions:
        worst_score = min(s for s, _ in contributions.values())
        if worst_score >= profile.negative_block_threshold:
            decisive_scores = [
                s for sig, (s, _) in contributions.items()
                if sig in profile.decisive_signals
                and s >= profile.decisive_threshold
            ]
            if decisive_scores:
                return Decision(
                    merge=True,
                    score=max(decisive_scores),
                    signals=contributions,
                    profile=profile.name,
                ), None, None

    if missing_eventually:
        return (
            None,
            f"missing_eventually:{','.join(missing_eventually)}",
            contributions or None,
        )

    if total_weight < profile.min_total_weight:
        return (
            None,
            f"below_min_total_weight:{total_weight:.2f}/{profile.min_total_weight:.2f}",
            contributions or None,
        )

    final = weighted_sum / total_weight if total_weight else 0.0
    return Decision(
        merge=(final >= profile.threshold),
        score=final,
        signals=contributions,
        profile=profile.name,
    ), None, None


def _try_irk_short_circuit(
    ctx: ClusterContext, a: Device, b: Device, profile_name: str
) -> Decision | None:
    """Run the IRK signal first if available; return a Decision if it speaks."""
    irk = ctx.signals.get("irk_resolution")
    if irk is None:
        return None
    try:
        if not irk.applies_to(ctx, a, b):
            return None
        s = irk.score(ctx, a, b, params={})
    except Exception as exc:
        log.warning("irk_resolution raised on (%s, %s): %s", a.id, b.id, exc)
        return None

    if s is None:
        return None
    if s >= 0.999:
        return Decision(
            merge=True,
            score=1.0,
            signals={"irk_resolution": (1.0, 1.0)},
            profile=profile_name,
        )
    if s <= -0.999:
        return Decision(
            merge=False,
            score=-1.0,
            signals={"irk_resolution": (-1.0, 1.0)},
            profile=profile_name,
            abort_reason="irk_mismatch",
        )
    return None
