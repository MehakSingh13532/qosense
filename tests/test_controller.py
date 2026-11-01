import json

from controller.classifier import RuleClassifier, StableClassifier
from controller.common import BULK, VOICE, WEB, FlowCounters, FlowKey
from controller.enforcer import (CircuitBreaker, Enforcer, conntrack_mark_cmd, tc_cmd)
from controller.estimator import CapacityEstimator, EstimatorConfig
from controller.features import FEATURES, FlowTracker
from controller.policy import IntentStore, decide
from controller.telemetry import parse_cake_stats, parse_conntrack
from controller.verifier import Snapshot, Verifier


def K(sport=1000, dport=443, proto="udp", src="10.0.0.2"):
    return FlowKey(proto, src, "198.51.100.10", sport, dport)


# ---- classifier -----------------------------------------------------------
def test_rules_use_ports_and_fail_on_unusual_port():
    r = RuleClassifier()
    assert r.classify(K(dport=3478))[0] == VOICE
    assert r.classify(K(dport=51234))[0] == WEB      # a call on an odd port is missed
    assert r.classify(K(dport=22, proto="tcp"))[0] == BULK


def test_hysteresis_needs_confirm_and_pins_win():
    class Flip:
        def __init__(self): self.out = [WEB, VOICE, VOICE, VOICE, VOICE]
        def classify(self, k, f): return self.out.pop(0), 0.9
    s = StableClassifier(Flip(), confirm=3)
    k = K()
    assert s.classify(k, None) == WEB
    assert s.classify(k, None) == WEB       # 1st voice vote
    assert s.classify(k, None) == WEB       # 2nd
    assert s.classify(k, None) == VOICE     # 3rd => switch
    s.pin(k, BULK)
    assert s.classify(k, None) == BULK and s.confidence(k) == 1.0


# ---- features ------------------------------------------------------------
def test_features_voice_like_flow():
    ft, k = FlowTracker(), K()
    for i in range(10):   # 50 pkt/s of 172 B, symmetric
        ft.update(i * 0.5, [FlowCounters(k, 25 * (i + 1), 25 * 172 * (i + 1), 25 * (i + 1), 25 * 172 * (i + 1))])
    f = ft.features(k, 5.0)
    assert set(f) == set(FEATURES)
    assert 90 < f["pkt_rate"] < 110 and 150 < f["mean_pkt_size"] < 190
    assert f["rate_cv"] < 0.05 and f["is_udp"] == 1.0


def test_conntrack_recycle_resets_window():
    ft, k = FlowTracker(), K()
    ft.update(0, [FlowCounters(k, 100, 1000, 100, 1000)])
    ft.update(1, [FlowCounters(k, 5, 50, 5, 50)])
    assert ft.features(k, 1) is None


# ---- estimator -------------------------------------------------------------
def test_estimator_clamps_and_probes_up():
    e = CapacityEstimator(EstimatorConfig(max_kbit=100_000), 50_000)
    assert e.step(2.0, 48_000) == 52_500          # +5% probe up
    r = e.step(80.0, 40_000)
    assert r >= 52_500 * 0.85 - 1                 # never more than -15%/tick


def test_cusum_cliff_recovers_faster_than_without():
    def run(use):
        e = CapacityEstimator(EstimatorConfig(use_cusum=use), 100_000)
        ticks = 0
        while e.rate > 25_000 and ticks < 60:
            ticks += 1
            e.step(120.0, min(e.rate, 20_000))    # true capacity 20 Mbit
        return ticks
    assert run(True) < run(False)


# ---- policy ------------------------------------------------------------------
def test_intent_validation_and_expiry():
    t = [0.0]
    s = IntentStore(clock=lambda: t[0])
    it = s.add("10.0.0.2", VOICE, 30)
    assert s.active() == [it]
    t[0] = 31
    assert s.active() == []
    for bad in (("10.0.0.2", "nonsense", 10), ("not-an-ip", VOICE, 10), ("any", VOICE, 99999)):
        try:
            s.add(*bad); raise AssertionError("accepted bad intent")
        except ValueError:
            pass


def test_decide_is_deterministic_and_intent_overrides():
    k1, k2 = K(1000, src="10.0.0.2"), K(1001, src="10.0.0.3")
    cls = {k1: WEB, k2: BULK}
    s = IntentStore(); s.add("10.0.0.2", VOICE, 60)
    d1 = decide(cls, {k1: 100, k2: 9000}, s.active(), 20_000, 90_000)
    d2 = decide(cls, {k1: 100, k2: 9000}, s.active(), 20_000, 90_000)
    assert d1.marks == d2.marks and d1.marks[k1] == 4 and d1.marks[k2] == 1


def test_bulk_floor_demotes_heaviest_priority_flow():
    a, b = K(1), K(2)
    d = decide({a: VOICE, b: VOICE}, {a: 9000, b: 100}, [], 1, 1)
    assert d.marks[a] == 2 and d.marks[b] == 4 and "bulk-floor" in d.reason


# ---- enforcer ------------------------------------------------------------------
def test_command_builders():
    assert tc_cmd("change", "r-wan", "up", 18000)[:6] == ["tc", "qdisc", "change", "dev", "r-wan", "root"]
    assert "dual-srchost" in tc_cmd("replace", "r-wan", "up", 1) and "dual-dsthost" in tc_cmd("replace", "br-lan", "down", 1)
    assert conntrack_mark_cmd(K(5, 6), 4)[-2:] == ["-m", "4"]


def test_breaker_opens_then_half_opens():
    t = [0.0]
    cb = CircuitBreaker(2, 5.0, clock=lambda: t[0])
    cb.fail(); cb.fail()
    assert cb.state == "open" and not cb.allow()
    t[0] = 6
    assert cb.state == "half-open" and cb.allow()
    cb.ok(); assert cb.state == "closed"


def test_enforcer_failure_trips_breaker_and_restore_rewrites_marks():
    calls, fail = [], [True]
    def ex(cmd, ns):
        calls.append(cmd)
        if fail[0]: raise RuntimeError("boom")
    e = Enforcer("r-wan", "br-lan", executor=ex, sleep=lambda s: None)
    from controller.common import Decision
    for _ in range(3):
        assert not e.apply(Decision(marks={K(): 4}, up_kbit=1000, down_kbit=2000))
    assert not e.breaker.allow()
    fail[0] = False; e.breaker.ok()
    assert e.apply(Decision(marks={K(): 4}, up_kbit=1000, down_kbit=2000))
    snap = e.snapshot(); calls.clear()
    assert e.restore(snap) and any("conntrack" in c[0] for c in calls)


# ---- verifier --------------------------------------------------------------------
def test_verifier_rolls_back_when_worse_once():
    v = Verifier(window_s=10, tolerance=0.25)
    for i in range(20): v.record_rtt(i * 0.5, 20.0)
    snap = Snapshot(1, 2, {})
    v.applied(10.0, snap)
    for i in range(20, 45): v.record_rtt(i * 0.5, 80.0)
    assert v.check(15.0) is None          # window not over
    assert v.check(21.0) is snap
    assert v.check(22.0) is None          # only once


def test_verifier_keeps_good_change():
    v = Verifier()
    for i in range(60): v.record_rtt(i * 0.5, 20.0 if i < 20 else 15.0)
    v.applied(10.0, Snapshot(1, 2, {}))
    assert v.check(21.0) is None and v.rollbacks == 0


# ---- telemetry parsers ------------------------------------------------------------
def test_parse_conntrack():
    line = ("ipv4     2 udp      17 25 src=10.0.0.2 dst=198.51.100.10 sport=40000 dport=3478 "
            "packets=100 bytes=17200 src=198.51.100.10 dst=192.0.2.2 sport=3478 dport=40000 "
            "packets=98 bytes=16856 mark=4 use=1")
    other = "ipv4 2 tcp 6 100 ESTABLISHED src=172.16.0.9 dst=1.1.1.1 sport=1 dport=2 packets=1 bytes=1 src=1.1.1.1 dst=172.16.0.9 sport=2 dport=1 packets=1 bytes=1 mark=0 use=1"
    got = parse_conntrack(line + "\n" + other)
    assert len(got) == 1 and got[0].key.dport == 3478 and got[0].mark == 4
    assert got[0].bytes_up == 17200 and got[0].pkts_down == 98


def test_parse_cake_json():
    js = json.dumps([{"kind": "cake", "options": {"bandwidth": 2500000}, "drops": 5,
                      "tins": [{"sent_packets": i, "sent_bytes": i * 100, "drops": 0,
                                "peak_delay_us": 10, "avg_delay_us": 5, "base_delay_us": 1}
                               for i in range(4)]}])
    s = parse_cake_stats(js)
    assert [t["name"] for t in s["tins"]] == ["bulk", "besteffort", "video", "voice"]
    assert s["tins"][3]["pkts"] == 3 and parse_cake_stats("not json") is None


def _sim_link(capacity_kbit, ticks, cfg=None, start=None, dt=0.5, estimator=None):
    """Fluid model of a bloated modem behind the shaper: rate above capacity builds a queue."""
    e = estimator or CapacityEstimator(cfg or EstimatorConfig(), start or int(capacity_kbit * 0.9))
    q_kbit, trace = 0.0, []
    for _ in range(ticks):
        q_kbit = max(0.0, q_kbit + (e.rate - capacity_kbit) * dt)
        delay_ms = q_kbit / capacity_kbit * 1000.0
        achieved = min(e.rate, capacity_kbit)
        e.step(delay_ms, achieved)
        trace.append((e.rate, delay_ms))
    return e, trace


def test_probe_overshoot_stays_small_in_steady_state():
    cfg = EstimatorConfig(max_kbit=150_000)            # headroom above the true 100 Mbit so probing is exercised
    old = EstimatorConfig(max_kbit=150_000, delay_thresh_ms=15, min_step_up=0.05, probe_delay_frac=1.0)
    peaks = {}
    for name, c in (("new", cfg), ("old", old)):
        _, tr = _sim_link(100_000, 500, cfg=c, start=90_000)
        tail = tr[250:]
        peaks[name] = max(d for _, d in tail)
        assert sum(r for r, _ in tail) / len(tail) > 0.93 * 100_000      # little throughput given up
    assert peaks["new"] < 18.0 and peaks["new"] < 0.85 * peaks["old"]   # approach the bad rate geometrically


def test_estimator_finds_higher_capacity_after_it_appears():
    e, _ = _sim_link(40_000, 80, cfg=EstimatorConfig(max_kbit=150_000), start=36_000)
    e2, _ = _sim_link(100_000, 300, estimator=e)
    assert e2.rate > 0.9 * 100_000
