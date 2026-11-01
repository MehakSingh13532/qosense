"""The control loop: sense -> decide -> act -> verify, every ~500 ms.

`Controller.tick()` is dependency-injected (conntrack/cake/rtt readers), so it can be
driven by tests with fake telemetry. `run_forever()` wires the real ones.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional

from . import telemetry
from .classifier import ModelClassifier, RuleClassifier, StableClassifier
from .common import MARK_TO_CLASS, FlowCounters, FlowKey
from .enforcer import Enforcer
from .estimator import CapacityEstimator, EstimatorConfig
from .features import FlowTracker
from .policy import IntentStore, decide
from .probe import RttProbe
from .verifier import Verifier

TICK_BUDGET_S = 0.4


class Controller:
    def __init__(self, enforcer: Enforcer, *, up_kbit: int, down_kbit: int,
                 conntrack_fn: Callable[[], List[FlowCounters]],
                 cake_fn: Callable[[str], Optional[dict]],
                 delay_fn: Callable[[float], Optional[float]],
                 rtt_fn: Callable[[float], Optional[float]],
                 model_path: str = "", classifier_mode: str = "model",
                 use_cusum: bool = True, heartbeat: Optional[str] = None,
                 clock=time.monotonic):
        self.enf = enforcer
        self.conntrack_fn, self.cake_fn, self.delay_fn, self.rtt_fn = conntrack_fn, cake_fn, delay_fn, rtt_fn
        self.clock, self.heartbeat = clock, heartbeat
        self.tracker = FlowTracker()
        self.set_classifier(classifier_mode, model_path)
        self.intents = IntentStore()
        self.verifier = Verifier()
        self.est_up = CapacityEstimator(EstimatorConfig(max_kbit=up_kbit, use_cusum=use_cusum), up_kbit * 95 // 100)
        self.est_dn = CapacityEstimator(EstimatorConfig(max_kbit=down_kbit, use_cusum=use_cusum), down_kbit * 95 // 100)
        self.enabled = True          # False => hold static rates, no per-flow marks
        self.estimator_enabled = True
        self._last_bytes: Dict[str, tuple] = {}
        self._cooldown_until = 0.0
        self.state: dict = {"flows": [], "kpi": {}, "tins": {}, "events": deque(maxlen=50)}
        self.tick_ms: Deque[float] = deque(maxlen=200)
        self.ticks = 0

    # -- config -------------------------------------------------------------------
    def set_classifier(self, mode: str, model_path: str = "") -> None:
        self.classifier_mode = mode
        base = ModelClassifier(model_path) if mode == "model" else RuleClassifier()
        if mode == "model" and not base.loaded:
            self.classifier_mode = "rules (model missing)"
        self.classifier = StableClassifier(base)
        self._model_path = model_path or getattr(self, "_model_path", "")

    def event(self, msg: str) -> None:
        self.state["events"].appendleft({"t": round(time.time(), 1), "msg": msg})

    # -- helpers ------------------------------------------------------------------
    def _rate_kbit(self, dev: str, now: float) -> float:
        st = self.cake_fn(dev)
        if not st:
            return 0.0
        tot = sum(t["bytes"] for t in st["tins"])
        prev = self._last_bytes.get(dev)
        self._last_bytes[dev] = (now, tot)
        if prev is None or now <= prev[0]:
            return 0.0
        return max(0.0, (tot - prev[1]) * 8 / 1000.0 / (now - prev[0]))

    # -- one iteration -------------------------------------------------------------
    def tick(self) -> None:
        t_start = time.perf_counter()
        now = self.clock()
        if self.heartbeat:
            try:
                os.makedirs(os.path.dirname(self.heartbeat), exist_ok=True)
                with open(self.heartbeat, "a"):
                    os.utime(self.heartbeat, None)
            except OSError:
                pass

        # sense
        counters = self.conntrack_fn()
        self.tracker.update(now, counters)
        delay = self.delay_fn(now)
        rtt = self.rtt_fn(now)
        if rtt is not None:
            self.verifier.record_rtt(now, rtt)
        up_ach = self._rate_kbit(self.enf.wan, now)
        dn_ach = self._rate_kbit(self.enf.lan, now)

        # classify
        flow_classes, flow_rates, rows = {}, {}, []
        live = set()
        for key in self.tracker.active_keys(now):
            f = self.tracker.features(key, now)
            if f is None:
                continue
            live.add(key)
            cls = self.classifier.classify(key, f)
            flow_classes[key] = cls
            flow_rates[key] = f["bytes_rate"] * 8 / 1000.0
            rows.append({"flow": key.short(), "host": key.src, "class": cls,
                         "conf": round(self.classifier.confidence(key), 2),
                         "kbit": round(flow_rates[key]), "pinned": key in self.classifier.pins,
                         "pkt_rate": round(f["pkt_rate"]), "pkt_size": round(f["mean_pkt_size"])})
        self.classifier.forget(live)

        # estimate
        up_kbit, dn_kbit = self.enf.up_kbit, self.enf.down_kbit
        if self.enabled and self.estimator_enabled and delay is not None and now >= self._cooldown_until:
            util_up = up_ach / max(1, self.est_up.rate)
            util_dn = dn_ach / max(1, self.est_dn.rate)
            d_up = delay if util_up >= util_dn else 0.0
            d_dn = delay if util_dn > util_up else 0.0
            new_up = self.est_up.step(d_up, up_ach)
            new_dn = self.est_dn.step(d_dn, dn_ach)
            if self.est_up.cliff or self.est_dn.cliff:
                self.event(f"cliff detected: up={new_up} down={new_dn} kbit")
            up_kbit, dn_kbit = new_up, new_dn

        # decide + act
        marks_enabled = self.enabled
        d = decide(flow_classes if marks_enabled else {}, flow_rates, self.intents.active(), up_kbit, dn_kbit)
        before = self.enf.snapshot()
        big_change = abs((d.up_kbit or 0) - (before.up_kbit or 0)) > 0.05 * max(1, before.up_kbit) or \
                     abs((d.down_kbit or 0) - (before.down_kbit or 0)) > 0.05 * max(1, before.down_kbit)
        if self.enabled:
            ok = self.enf.apply(d)
            if not ok and self.enf.breaker.state == "open":
                self.event(f"enforcer breaker open: {self.enf.last_error}")
            if big_change and ok:
                self.verifier.applied(now, before)

        # verify
        snap = self.verifier.check(now)
        if snap is not None:
            self.enf.restore(snap)
            self.est_up.rate, self.est_dn.rate = snap.up_kbit or self.est_up.rate, snap.down_kbit or self.est_dn.rate
            self._cooldown_until = now + 5.0
            self.event("verifier rollback: KPI worsened after change")

        # publish state
        self.state.update({
            "flows": sorted(rows, key=lambda r: -r["kbit"])[:30],
            "kpi": {"delay_ms": delay, "rtt_ms": rtt, "up_kbit": self.enf.up_kbit, "down_kbit": self.enf.down_kbit,
                    "up_achieved_kbit": round(up_ach), "down_achieved_kbit": round(dn_ach),
                    "cliff": self.est_up.cliff or self.est_dn.cliff, "rollbacks": self.verifier.rollbacks,
                    "breaker": self.enf.breaker.state, "classifier": self.classifier_mode,
                    "enabled": self.enabled, "reason": d.reason},
            "intents": [vars(i) for i in self.intents.active()],
        })
        self.ticks += 1
        self.tick_ms.append((time.perf_counter() - t_start) * 1000.0)

    def run_forever(self, period: float = 0.5, stop: Optional[threading.Event] = None) -> None:
        stop = stop or threading.Event()
        nxt = time.monotonic()
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - never die on one bad tick
                self.event(f"tick error: {e!r}")
            nxt += period
            delay = nxt - time.monotonic()
            if delay < 0:            # late tick: skip, never queue (deadline rule)
                nxt = time.monotonic()
            else:
                stop.wait(delay)


def build_real(ns: str = "qs-rtr", wan: str = "r-wan", lan: str = "br-lan", up_mbit: int = 20,
               down_mbit: int = 100, model_path: str = "models/model.txt", classifier_mode: str = "model",
               use_cusum: bool = True, heartbeat: str = "/run/qosense/heartbeat") -> "Controller":
    up_k, dn_k = up_mbit * 1000, down_mbit * 1000
    enf = Enforcer(wan, lan, ns=ns)
    enf.initial_setup(up_k * 95 // 100, dn_k * 95 // 100)
    probe = RttProbe(ns=ns)
    probe.start()
    c = Controller(enf, up_kbit=up_k, down_kbit=dn_k,
                   conntrack_fn=lambda: telemetry.read_conntrack(ns),
                   cake_fn=lambda dev: telemetry.cake_stats(dev, ns),
                   delay_fn=probe.delay_ms, rtt_fn=probe.latest_rtt,
                   model_path=model_path, classifier_mode=classifier_mode,
                   use_cusum=use_cusum, heartbeat=heartbeat)
    c.probe = probe
    return c


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--ns", default="qs-rtr")
    ap.add_argument("--up-mbit", type=int, default=20)
    ap.add_argument("--down-mbit", type=int, default=100)
    ap.add_argument("--model", default="models/model.txt")
    ap.add_argument("--classifier", default="model", choices=["model", "rules"])
    ap.add_argument("--no-cusum", action="store_true")
    a = ap.parse_args()
    build_real(a.ns, up_mbit=a.up_mbit, down_mbit=a.down_mbit, model_path=a.model,
               classifier_mode=a.classifier, use_cusum=not a.no_cusum).run_forever()
