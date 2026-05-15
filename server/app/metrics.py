"""Plain-text Prometheus exposition with zero deps.

Design §13.1 lists `/metrics` as part of monitoring. We implement just
enough metric types to be useful — counters and histograms — without
pulling in `prometheus_client`. Histogram buckets are fixed and match the
design's latency targets (§11.3: <30s rules-only, <90s single-dim, <5min
all-dim).
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field


_HIST_BUCKETS_MS = (
    50, 200, 1_000, 5_000, 30_000, 90_000, 300_000, math.inf,
)


@dataclass
class _Counter:
    samples: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)


@dataclass
class _Histogram:
    """Tracks bucket counts + sum + count per label-set."""
    buckets: tuple[float, ...]
    bucket_counts: dict[tuple[tuple[str, str], ...], list[int]] = field(default_factory=dict)
    sum_value: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)
    total_count: dict[tuple[tuple[str, str], ...], int] = field(default_factory=dict)


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, _Counter] = {}
        self._gauges: dict[str, _Counter] = {}
        self._histograms: dict[str, _Histogram] = {}

    def incr(self, name: str, *, labels: dict[str, str] | None = None, amount: float = 1.0) -> None:
        with self._lock:
            ctr = self._counters.setdefault(name, _Counter())
            key = _label_key(labels)
            ctr.samples[key] = ctr.samples.get(key, 0.0) + amount

    def set_gauge(self, name: str, value: float, *, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            g = self._gauges.setdefault(name, _Counter())
            g.samples[_label_key(labels)] = float(value)

    def observe(self, name: str, value_ms: float, *, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            hist = self._histograms.setdefault(name, _Histogram(buckets=_HIST_BUCKETS_MS))
            key = _label_key(labels)
            counts = hist.bucket_counts.setdefault(key, [0] * len(hist.buckets))
            for i, upper in enumerate(hist.buckets):
                if value_ms <= upper:
                    counts[i] += 1
            hist.sum_value[key] = hist.sum_value.get(key, 0.0) + value_ms
            hist.total_count[key] = hist.total_count.get(key, 0) + 1

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name, ctr in self._counters.items():
                lines.append(f"# TYPE {name} counter")
                for key, val in ctr.samples.items():
                    lines.append(f"{name}{_render_labels(key)} {val}")
            for name, g in self._gauges.items():
                lines.append(f"# TYPE {name} gauge")
                for key, val in g.samples.items():
                    lines.append(f"{name}{_render_labels(key)} {val}")
            for name, hist in self._histograms.items():
                lines.append(f"# TYPE {name} histogram")
                for key, counts in hist.bucket_counts.items():
                    cum = 0
                    for i, upper in enumerate(hist.buckets):
                        cum += counts[i]
                        le = "+Inf" if upper == math.inf else str(upper)
                        labels = key + (("le", le),)
                        lines.append(f"{name}_bucket{_render_labels(labels)} {cum}")
                    lines.append(f"{name}_sum{_render_labels(key)} {hist.sum_value.get(key, 0.0)}")
                    lines.append(f"{name}_count{_render_labels(key)} {hist.total_count.get(key, 0)}")
        return "\n".join(lines) + "\n"


def _label_key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted(labels.items()))


def _render_labels(key: tuple[tuple[str, str], ...]) -> str:
    if not key:
        return ""
    parts = ",".join(f'{k}="{_escape(v)}"' for k, v in key)
    return "{" + parts + "}"


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
