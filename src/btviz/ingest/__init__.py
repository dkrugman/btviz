"""pcap/pcapng ingest via tshark dissection.

Two layers:
    tshark.py    -- subprocess wrapper that yields raw tshark EK records
    normalize.py -- converts a tshark record into a normalized `Packet`

This split lets normalize.py be tested against stored JSON fixtures with no
tshark installed.
"""
from .normalize import normalize
from .pipeline import IngestReport, ingest_file
from .tshark import (
    TsharkError,
    TsharkNotFound,
    dissect_file,
    find_tshark,
)

__all__ = [
    "IngestReport",
    "TsharkError",
    "TsharkNotFound",
    "dissect_file",
    "find_tshark",
    "ingest_file",
    "normalize",
]
