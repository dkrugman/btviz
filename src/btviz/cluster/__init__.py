"""RPA collapse / device clustering framework.

See docs/rpa_collapse/ for the architecture. The framework is
deliberately data-source-agnostic at the Signal layer so that the
same signals can run against the live SQLite DB once the schema
migration lands or against synthetic in-memory data for tests.
"""

from .aggregator import cluster_pair
from .base import (
    ClassProfile,
    ClusterContext,
    Decision,
    Device,
    Signal,
)
from .cluster_log import configure_cluster_log, get_cluster_logger
from .runner import ClusterRunner, RunResult

__all__ = [
    "ClassProfile",
    "ClusterContext",
    "ClusterRunner",
    "Decision",
    "Device",
    "RunResult",
    "Signal",
    "cluster_pair",
    "configure_cluster_log",
    "get_cluster_logger",
]
