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
from .cluster_log import (
    apply_cluster_log_prefs,
    configure_cluster_log,
    get_cluster_logger,
)
from .db_loader import load_devices
from .profile_loader import load_profiles
from .runner import ClusterRunner, RunResult
from .signals import load_signals

__all__ = [
    "ClassProfile",
    "ClusterContext",
    "ClusterRunner",
    "Decision",
    "Device",
    "RunResult",
    "Signal",
    "apply_cluster_log_prefs",
    "cluster_pair",
    "configure_cluster_log",
    "get_cluster_logger",
    "load_devices",
    "load_profiles",
    "load_signals",
]
