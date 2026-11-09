"""Latency percentiles shared by the relay, the drill, and the bench."""
from __future__ import annotations

import math


def percentiles(samples: list[float]) -> dict[str, float]:
    """Nearest-rank p50/p95/p99. Empty input is zero."""
    if not samples:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "n": 0}
    ordered = sorted(float(v) for v in samples if math.isfinite(v))
    if not ordered:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "n": 0}

    def pick(pct: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        rank = max(0, math.ceil(pct / 100.0 * len(ordered)) - 1)
        return ordered[min(rank, len(ordered) - 1)]

    return {"p50": pick(50), "p95": pick(95), "p99": pick(99), "n": float(len(ordered))}
