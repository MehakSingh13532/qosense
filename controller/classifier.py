"""Flow classifiers.

* RuleClassifier  - the baseline. Uses destination port and protocol, like
  qosify-style heuristics. It is *allowed* to look at ports.
* ModelClassifier - LightGBM on behavioural features only (no ports, no payload).
  Falls back to the rules when the model is missing or not confident.
* Both are wrapped by StableClassifier, which adds hysteresis (a class change
  needs N consecutive agreeing predictions) and user pins ("this flow is a call").
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Dict, Optional, Tuple

from .common import BULK, CLASSES, VIDEO, VOICE, WEB, FlowKey
from .features import FEATURES

# Well-known real-time / video / bulk ports. Deliberately incomplete: the point of
# the "call on an unusual port" experiment is that this list is never complete.
VOICE_PORTS = {3478, 3479, 3480, 3481, 5060, 5061, 5004, 19302, 19303, 19304, 19305,
               19306, 19307, 19308, 19309, 8801, 8802, 8803, 8804, 8805, 8806, 8807,
               8808, 8809, 8810, 27015, 3074, 3659, 9987}
VIDEO_PORTS = {1935, 8554, 554, 8008, 8009}
BULK_PORTS = {20, 21, 22, 873, 989, 990, 6881, 6882, 6883, 6884, 6885, 6886, 6887,
              6888, 6889, 5201}


class RuleClassifier:
    name = "rules"

    def classify(self, key: FlowKey, feats: Optional[dict] = None) -> Tuple[str, float]:
        p = key.dport
        if p in VOICE_PORTS and key.proto == "udp":
            return VOICE, 0.9
        if p in VIDEO_PORTS:
            return VIDEO, 0.8
        if p in BULK_PORTS:
            return BULK, 0.8
        return WEB, 0.5   # unknown => best effort


class ModelClassifier:
    name = "lightgbm"

    def __init__(self, model_path: str, min_conf: float = 0.55):
        self.min_conf = min_conf
        self.booster = None
        self.rules = RuleClassifier()
        self.classes = CLASSES
        if model_path and os.path.exists(model_path):
            import lightgbm as lgb
            self.booster = lgb.Booster(model_file=model_path)

    @property
    def loaded(self) -> bool:
        return self.booster is not None

    def classify(self, key: FlowKey, feats: Optional[dict]) -> Tuple[str, float]:
        if self.booster is None or feats is None:
            return self.rules.classify(key, feats)
        row = [[feats[n] for n in FEATURES]]
        proba = self.booster.predict(row)[0]
        idx = int(max(range(len(proba)), key=lambda i: proba[i]))
        conf = float(proba[idx])
        if conf < self.min_conf:           # graceful degradation
            return self.rules.classify(key, feats)
        return self.classes[idx], conf


class StableClassifier:
    """Hysteresis + pins on top of any base classifier."""

    def __init__(self, base, confirm: int = 3):
        self.base = base
        self.confirm = confirm
        self._cur: Dict[FlowKey, str] = {}
        self._cand: Dict[FlowKey, Tuple[str, int]] = {}
        self._conf: Dict[FlowKey, float] = {}
        self.pins: Dict[FlowKey, str] = {}

    def pin(self, key: FlowKey, cls: str) -> None:
        if cls not in CLASSES:
            raise ValueError(f"unknown class {cls!r}")
        self.pins[key] = cls

    def unpin(self, key: FlowKey) -> None:
        self.pins.pop(key, None)

    def confidence(self, key: FlowKey) -> float:
        return self._conf.get(key, 0.0)

    def current(self, key: FlowKey) -> Optional[str]:
        return self.pins.get(key) or self._cur.get(key)

    def classify(self, key: FlowKey, feats: Optional[dict]) -> str:
        if key in self.pins:
            self._conf[key] = 1.0
            return self.pins[key]
        cls, conf = self.base.classify(key, feats)
        self._conf[key] = conf
        cur = self._cur.get(key)
        if cur is None:
            self._cur[key] = cls
            return cls
        if cls == cur:
            self._cand.pop(key, None)
            return cur
        cand, n = self._cand.get(key, (cls, 0))
        n = n + 1 if cand == cls else 1
        if n >= self.confirm:
            self._cur[key] = cls
            self._cand.pop(key, None)
            return cls
        self._cand[key] = (cls, n)
        return cur

    def forget(self, live: set) -> None:
        for d in (self._cur, self._cand, self._conf):
            for k in [k for k in d if k not in live]:
                d.pop(k, None)
