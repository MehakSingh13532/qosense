"""Policy engine: classes + intents + capacity -> a deterministic Decision.

Deterministic on purpose (same inputs => same output): easy to test and explain.
Guardrails:
  * intents are validated (class whitelist, TTL cap) and expire on their own;
  * a bulk floor: if latency-sensitive classes would consume > MAX_PRIORITY_SHARE
    of the measured rate, the heaviest priority flow is demoted to best effort so
    bulk and web are never starved.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .common import (BULK, CLASS_TO_MARK, CLASSES, VIDEO, VOICE, WEB, Decision,
                     FlowKey)

MAX_INTENT_TTL_S = 3600
MAX_PRIORITY_SHARE = 0.95   # => bulk floor of 5%


@dataclass
class Intent:
    id: str
    target: str            # LAN host IP or "any"
    cls: str               # class to force: voice | video | web | bulk
    expires_at: float
    note: str = ""


class IntentStore:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._intents: Dict[str, Intent] = {}
        self._n = 0

    def add(self, target: str, cls: str, ttl_s: float, note: str = "") -> Intent:
        if cls not in CLASSES:
            raise ValueError(f"class must be one of {CLASSES}")
        if not (1 <= ttl_s <= MAX_INTENT_TTL_S):
            raise ValueError(f"ttl_s must be between 1 and {MAX_INTENT_TTL_S}")
        if target != "any":
            import ipaddress
            ipaddress.ip_address(target)    # raises ValueError if not an IP
        self._n += 1
        it = Intent(f"i{self._n}", target, cls, self._clock() + ttl_s, note[:80])
        self._intents[it.id] = it
        return it

    def remove(self, iid: str) -> bool:
        return self._intents.pop(iid, None) is not None

    def active(self) -> List[Intent]:
        now = self._clock()
        for iid in [i for i, it in self._intents.items() if it.expires_at <= now]:
            del self._intents[iid]
        return list(self._intents.values())


def decide(flow_classes: Dict[FlowKey, str], flow_rates_kbit: Dict[FlowKey, float],
           intents: List[Intent], up_kbit: Optional[int], down_kbit: Optional[int]) -> Decision:
    """Pure function. flow_classes comes from the classifier (pins already applied)."""
    d = Decision(up_kbit=up_kbit, down_kbit=down_kbit)
    final: Dict[FlowKey, str] = {}
    for key, cls in flow_classes.items():
        for it in intents:
            if it.target in ("any", key.src):
                cls = it.cls            # latest matching intent wins (list order)
        final[key] = cls

    total = sum(flow_rates_kbit.get(k, 0.0) for k in final) or 0.0
    prio = [k for k, c in final.items() if c in (VOICE, VIDEO)]
    prio_rate = sum(flow_rates_kbit.get(k, 0.0) for k in prio)
    notes = []
    if total > 0 and prio_rate / total > MAX_PRIORITY_SHARE and len(prio) > 1:
        heaviest = max(prio, key=lambda k: flow_rates_kbit.get(k, 0.0))
        final[heaviest] = WEB
        notes.append(f"bulk-floor: demoted {heaviest.short()}")

    d.marks = {k: CLASS_TO_MARK[c] for k, c in final.items()}
    d.reason = "; ".join(notes) or "ok"
    return d
