"""Verifier: watches KPIs after a change and asks for a rollback if they got worse."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional


@dataclass
class Snapshot:
    up_kbit: int
    down_kbit: int
    marks: dict


class Verifier:
    """After `apply()` is recorded, compare p95 RTT over the verify window with the
    pre-change p95. If it is worse by > tolerance, `check()` returns the snapshot
    to roll back to (once)."""

    def __init__(self, window_s: float = 10.0, tolerance: float = 0.25, min_samples: int = 10):
        self.window_s = window_s
        self.tolerance = tolerance
        self.min_samples = min_samples
        self._samples: Deque[tuple] = deque(maxlen=2000)   # (t, rtt_ms)
        self._pending: Optional[tuple] = None   # (t_applied, baseline_p95, snapshot_before)
        self.rollbacks = 0

    def record_rtt(self, t: float, rtt_ms: float) -> None:
        self._samples.append((t, rtt_ms))

    def _p95(self, t0: float, t1: float) -> Optional[float]:
        xs = sorted(r for t, r in self._samples if t0 <= t < t1)
        if len(xs) < self.min_samples:
            return None
        return xs[min(len(xs) - 1, int(0.95 * len(xs)))]

    def applied(self, t: float, before: Snapshot) -> None:
        base = self._p95(t - self.window_s, t)
        self._pending = (t, base, before)

    def check(self, t: float) -> Optional[Snapshot]:
        if self._pending is None:
            return None
        t0, base, snap = self._pending
        if t - t0 < self.window_s:
            return None
        self._pending = None
        after = self._p95(t0, t)
        if base is None or after is None:
            return None
        if after > base * (1.0 + self.tolerance) and after - base > 5.0:
            self.rollbacks += 1
            return snap
        return None
