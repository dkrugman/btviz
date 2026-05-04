"""Load ClassProfile instances from TOML files in profiles/."""

from __future__ import annotations

import sys
from pathlib import Path

from .base import ClassProfile

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]


PROFILES_DIR = Path(__file__).parent / "profiles"


def load_profiles(directory: Path | None = None) -> dict[str, ClassProfile]:
    """Load every *.toml in profiles/ into a {class_name: ClassProfile} dict."""
    directory = directory or PROFILES_DIR
    out: dict[str, ClassProfile] = {}
    for path in sorted(directory.glob("*.toml")):
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        for class_name, body in data.items():
            out[class_name] = _build_profile(class_name, body)
    return out


def _build_profile(name: str, body: dict) -> ClassProfile:
    weights = dict(body.get("weights", {}))
    threshold = float(body.get("threshold", 0.75))
    min_total_weight = float(body.get("min_total_weight", 0.50))
    req_eventually = frozenset(
        body.get("required_eventually", []) or body.get("required", [])
    )
    req_for_merge = frozenset(body.get("required_for_merge", []))
    params = dict(body.get("params", {}))
    decisive_signals = frozenset(body.get("decisive_signals", []))
    decisive_threshold = float(body.get("decisive_threshold", 0.95))
    negative_block_threshold = float(
        body.get("negative_block_threshold", -0.3),
    )
    return ClassProfile(
        name=name,
        weights=weights,
        threshold=threshold,
        min_total_weight=min_total_weight,
        required_eventually=req_eventually,
        required_for_merge=req_for_merge,
        params=params,
        decisive_signals=decisive_signals,
        decisive_threshold=decisive_threshold,
        negative_block_threshold=negative_block_threshold,
    )
