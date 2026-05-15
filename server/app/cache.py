"""In-memory TTL cache.

Design §6.2 specifies Redis with specific TTLs (corpus_cache: 5min,
audit_result: 30d, etc). For the skeleton, a process-local dict suffices —
we just need the same eviction semantics so adding Redis later is a swap.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class _Entry:
    value: Any
    expires_at: float


class TTLCache:
    def __init__(self) -> None:
        self._store: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            e = self._store.get(key)
            if e is None:
                return None
            if e.expires_at < time.monotonic():
                del self._store[key]
                return None
            return e.value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        with self._lock:
            self._store[key] = _Entry(value=value, expires_at=time.monotonic() + ttl_seconds)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def size(self) -> int:
        with self._lock:
            return len(self._store)
