"""Metadata-only flow features computed from conntrack counter deltas.

No payload is read and no port number is given to the ML model: it has to
recognise traffic by *behaviour* (packet rate, size, regularity, direction).
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from typing import Deque, Dict, List, Tuple

from .common import FlowCounters, FlowKey

# Order matters: models/train.py and the classifier both import this list.
FEATURES = [
    "pkt_rate",        # packets/s (both directions)
    "mean_pkt_size",   # bytes
    "up_down_ratio",   # bytes_up / (bytes_up + bytes_down)
    "rate_cv",         # coefficient of variation of per-window byte rate (burstiness)
    "pkt_size_cv",     # variation of per-window mean packet size
    "flow_age",        # seconds since first seen
    "is_udp",          # 1 for UDP, else 0
    "host_flows",      # concurrent flows of the same LAN host
    "bytes_rate",      # bytes/s (both directions)
]


def _cv(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if mean <= 0:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(var) / mean


class FlowTracker:
    """Keeps a rolling window of per-flow counter deltas."""

    def __init__(self, window: int = 20, idle_timeout: float = 10.0):
        self.window = window
        self.idle_timeout = idle_timeout
        self._last: Dict[FlowKey, Tuple[float, FlowCounters]] = {}
        self._first_seen: Dict[FlowKey, float] = {}
        self._last_active: Dict[FlowKey, float] = {}
        # per-window samples: (dt, pkts, bytes, bytes_up)
        self._win: Dict[FlowKey, Deque[Tuple[float, int, int, int]]] = defaultdict(
            lambda: deque(maxlen=self.window))

    def update(self, now: float, counters: List[FlowCounters]) -> None:
        seen = set()
        for c in counters:
            seen.add(c.key)
            prev = self._last.get(c.key)
            self._first_seen.setdefault(c.key, now)
            if prev is not None:
                t0, p = prev
                dt = max(now - t0, 1e-6)
                dpk = (c.pkts_up + c.pkts_down) - (p.pkts_up + p.pkts_down)
                dby = (c.bytes_up + c.bytes_down) - (p.bytes_up + p.bytes_down)
                dup = c.bytes_up - p.bytes_up
                if dpk < 0 or dby < 0:      # conntrack entry was recycled
                    self._win.pop(c.key, None)
                    self._first_seen[c.key] = now
                elif dpk > 0:
                    self._win[c.key].append((dt, dpk, dby, max(dup, 0)))
                    self._last_active[c.key] = now
                else:
                    self._win[c.key].append((dt, 0, 0, 0))
            self._last[c.key] = (now, c)
        # forget flows that vanished
        for k in [k for k in self._last if k not in seen]:
            self._forget(k)

    def _forget(self, k: FlowKey) -> None:
        for d in (self._last, self._first_seen, self._last_active, self._win):
            d.pop(k, None)

    def active_keys(self, now: float) -> List[FlowKey]:
        return [k for k, t in self._last_active.items() if now - t <= self.idle_timeout]

    def features(self, key: FlowKey, now: float) -> Dict[str, float] | None:
        win = self._win.get(key)
        if not win or len(win) < 3:
            return None
        tot_dt = sum(w[0] for w in win)
        tot_pk = sum(w[1] for w in win)
        tot_by = sum(w[2] for w in win)
        tot_up = sum(w[3] for w in win)
        if tot_pk == 0 or tot_dt <= 0:
            return None
        rates = [w[2] / w[0] for w in win]
        sizes = [w[2] / w[1] for w in win if w[1] > 0]
        host = key.src
        host_flows = sum(1 for k in self.active_keys(now) if k.src == host)
        return {
            "pkt_rate": tot_pk / tot_dt,
            "mean_pkt_size": tot_by / tot_pk,
            "up_down_ratio": tot_up / tot_by if tot_by else 0.5,
            "rate_cv": _cv(rates),
            "pkt_size_cv": _cv(sizes),
            "flow_age": now - self._first_seen.get(key, now),
            "is_udp": 1.0 if key.proto == "udp" else 0.0,
            "host_flows": float(host_flows),
            "bytes_rate": tot_by / tot_dt,
        }

    def vector(self, feats: Dict[str, float]) -> List[float]:
        return [feats[n] for n in FEATURES]
