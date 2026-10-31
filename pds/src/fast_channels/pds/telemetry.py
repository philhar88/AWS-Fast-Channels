"""The sidecar's metrics and its one-minute stats line.

Powertools ``Metrics`` keeps one shared metric set and is not thread-safe, and the endpoints run on the server's
worker threads. So they count into :class:`Counters` (a lock and a dict), which the event loop emits every
:data:`REPORT_SECONDS` as embedded-metric-format lines through Powertools: one line for the plain counts, one per
dimension value. CloudWatch Logs extracts the metrics from the container's log group as it does from a function's.

:class:`Window` keeps the minute's samples (per-endpoint latency, the thread pool's queue wait, the event loop's lag)
for the stats line the sidecar logs each minute (``pds stats``): what sizing and the herd measurements read. It is not
a decision record: the lake subscriptions match ``record`` only.
"""

import math
import resource
import threading
import time
from collections import defaultdict
from collections.abc import Sequence
from typing import Any, Final

from aws_lambda_powertools import Logger, Metrics, single_metric
from aws_lambda_powertools.metrics import MetricUnit

REPORT_SECONDS: Final = 60.0
"""How often the counts are emitted and the stats line is logged."""
NAMESPACE: Final = "FastChannels"
"""The metrics' namespace, the functions' (``infra.constructs.functions.METRICS_NAMESPACE``)."""

logger = Logger(child=True)

type Series = tuple[str, MetricUnit, tuple[str, str] | None]
"""A metric's name, unit and optional dimension (name, value)."""


class Counters:
    """Counts added from any thread, emitted from one (module docstring)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[Series, float] = defaultdict(float)

    def add(
        self,
        name: str,
        value: float = 1,
        *,
        unit: MetricUnit = MetricUnit.Count,
        dimension: tuple[str, str] | None = None,
    ) -> None:
        with self._lock:
            self._counts[name, unit, dimension] += value

    def take(self) -> dict[Series, float]:
        """The counts since the last take, which start again from zero."""
        with self._lock:
            counts, self._counts = dict(self._counts), defaultdict(float)
        return counts

    def flush(self) -> int:
        """Emit the counts since the last flush; returns how many series it emitted."""
        counts = self.take()
        plain = {(name, unit): value for (name, unit, dimension), value in counts.items() if dimension is None}
        if plain:
            metrics = Metrics(namespace=NAMESPACE)
            for (name, unit), value in plain.items():
                metrics.add_metric(name=name, unit=unit, value=value)
            metrics.flush_metrics()
        for (name, unit, dimension), value in counts.items():
            if dimension is not None:
                with single_metric(name=name, unit=unit, value=value, namespace=NAMESPACE) as metric:
                    metric.add_dimension(name=dimension[0], value=dimension[1])
        return len(counts)


def percentile(values: Sequence[float], share: float) -> float:
    """The nearest-rank ``share`` percentile of ``values`` (0 for none)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def summary(values: Sequence[float]) -> dict[str, float | int]:
    """Count, p50, p99 and max of a minute's samples, in milliseconds to 0.1."""
    return {
        "n": len(values),
        "p50": round(percentile(values, 0.5), 1),
        "p99": round(percentile(values, 0.99), 1),
        "max": round(max(values, default=0.0), 1),
    }


class Window:
    """The minute's samples in milliseconds, by name (module docstring); thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._samples: dict[str, list[float]] = defaultdict(list)
        self._cpu = _cpu_seconds()
        self._since = time.monotonic()

    def add(self, name: str, milliseconds: float) -> None:
        with self._lock:
            self._samples[name].append(milliseconds)

    def take(self) -> dict[str, Any]:
        """The stats of the minute since the last take: each sample's summary, and the process's CPU share (of one
        vCPU) and peak resident memory."""
        now, cpu = time.monotonic(), _cpu_seconds()
        with self._lock:
            samples, self._samples = self._samples, defaultdict(list)
            elapsed, used = now - self._since, cpu - self._cpu
            self._since, self._cpu = now, cpu
        return {
            **{name: summary(values) for name, values in sorted(samples.items())},
            "cpu_pct": round(100 * used / elapsed, 1) if elapsed > 0 else 0.0,
            "max_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        }


def _cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


counters = Counters()
"""The sidecar's counts."""
window = Window()
"""The sidecar's minute of samples."""
