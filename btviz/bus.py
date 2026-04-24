"""Tiny synchronous pub/sub event bus.

Subscribers are called inline on publish. UI consumers should marshal to
their thread (Qt: emit a signal from the subscriber callback).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from threading import RLock
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        self._lock = RLock()

    def subscribe(self, topic: str, fn: Callable[[Any], None]) -> Callable[[], None]:
        with self._lock:
            self._subs[topic].append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subs[topic]:
                    self._subs[topic].remove(fn)
        return unsubscribe

    def publish(self, topic: str, payload: Any) -> None:
        with self._lock:
            subs = list(self._subs[topic])
        for fn in subs:
            try:
                fn(payload)
            except Exception:  # noqa: BLE001 - never let one subscriber kill others
                import traceback
                traceback.print_exc()


# Topic constants (string consts beat magic strings everywhere).
TOPIC_DONGLES_CHANGED = "dongles.changed"
TOPIC_PACKET = "capture.packet"
TOPIC_DEVICE_UPSERT = "inventory.device_upsert"
TOPIC_SNIFFER_STATE = "sniffer.state"
