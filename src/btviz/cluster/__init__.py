"""RPA collapse / device clustering framework.

See docs/rpa_collapse/ for the architecture. The framework is
deliberately data-source-agnostic at the Signal layer so that the
same signals can run against the live SQLite DB once the schema
migration lands or against synthetic in-memory data for tests.
"""

from .base import (
    ClassProfile,
    ClusterContext,
    Decision,
    Device,
    Signal,
)
from .aggregator import cluster_pair

__all__ = [
    "ClassProfile",
    "ClusterContext",
    "Decision",
    "Device",
    "Signal",
    "cluster_pair",
]
