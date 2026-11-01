#!/usr/bin/env python3
"""Automated experiments: reset -> configure -> impair -> collect -> save JSON.

Run as root with the lab up (lab/netns.sh up). Scenarios:
  s1  ISO download (bulk up+down) during a video call; call on a well-known port AND an unusual port
  s2  capacity cliff: modem drops from DOWN to DOWN/5 mid-run
  s3  three-TV household + a gamer
  s4  temporary intent: the odd-port call is fixed by a user intent, which then expires
  s6  controller crash -> watchdog applies the safe state

Configs: fifo | cake_static | qosense_rules | qosense_nocusum | qosense_model
Results: experiments/results/<scenario>.json, rendered by experiments/report.py.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
NSP = os.environ.get("NSP", "qs")
DOWN = int(os.environ.get("DOWN_MBIT", 100))
UP = int(os.environ.get("UP_MBIT", 20))
NETNS = os.path.join(ROOT, "lab", "netns.sh")
TRAFFIC = os.path.join(ROOT, "lab", "traffic.py")
RES = os.path.join(ROOT, "experiments", "results")
ODD_PORT, KNOWN_PORT = 51234, 3478
MODEL = os.path.join(ROOT, "models", "model.txt")


# ---------------------------------------------------------------- helpers
def sh(*cmd, ns=None, check=True):
    if ns:
        cmd = ("ip", "netns", "exec", ns) + cmd
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def spawn(host, *args):
    """Start a traffic generator in a host namespace; returns the Popen."""
    return subprocess.Popen(["ip", "netns", "exec", f"{NSP}-{host}", "python3", TRAFFIC, *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def result(p, timeout=120):
    out, err = p.communicate(timeout=timeout)
    try:
        return json.loads(out.strip().splitlines()[-1])
    except Exception:
        return {"error": (err or out)[:300]}


def pctl(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))] if xs else None


def summarize(call):
    r = call.get("rtt_ms") or {}
    return {k: r.get(k) for k in ("min", "p50", "p95", "p99", "max")} | {"loss_pct": call.get("loss_pct")}


class Ctrl:
    """Start/stop an in-process controller for one configuration."""

    def __init__(self, cfg):
        self.cfg, self.c, self.stop, self.thread = cfg, None, threading.Event(), None

    def __enter__(self):
        sh(NETNS, "shaper", "fifo" if self.cfg == "fifo" else "cake", str(DOWN), str(UP))
        sh("conntrack", "-F", ns=f"{NSP}-rtr", check=False)
        if self.cfg.startswith("qosense"):
            from controller.main import build_real
            mode = "rules" if self.cfg == "qosense_rules" else "model"
            self.c = build_real(ns=f"{NSP}-rtr", up_mbit=UP, down_mbit=DOWN, model_path=MODEL,
                                classifier_mode=mode, use_cusum=self.cfg != "qosense_nocusum",
                                heartbeat="/tmp/qosense-heartbeat")
            self.thread = threading.Thread(target=self.c.run_forever, args=(0.5, self.stop), daemon=True)
            self.thread.start()
            time.sleep(8)           # let the probe collect a baseline
        return self

    def __exit__(self, *a):
        self.stop.set()
        if self.c:
            self.c.probe.stop()
        if self.thread:
            self.thread.join(timeout=3)


def idle_baseline(port, secs=5):
    return result(spawn("h1", "voice", "--port", str(port), "--duration", str(secs)))


def load(kind="iso", secs=25):
    """Background load from h2: bulk download (ISO) and an upload (backup/photos)."""
    ps = [spawn("h2", "bulk", "--duration", str(secs), "--reverse"),
          spawn("h2", "bulk", "--duration", str(secs), "--streams", "4")]
    return ps


# ---------------------------------------------------------------- scenarios
def s1(cfgs, secs):
    out = {"scenario": "s1", "title": "ISO download during a video call", "configs": {}}
    for cfg in cfgs:
        entry = {}
        with Ctrl(cfg):
            for label, port in (("known_port", KNOWN_PORT), ("unusual_port", ODD_PORT)):
                base = summarize(idle_baseline(port))
                bg = load(secs=secs + 6)
                time.sleep(4)
                call = result(spawn("h1", "voice", "--port", str(port), "--duration", str(secs)))
                bulk = [result(p, timeout=secs + 60) for p in bg]
                s = summarize(call)
                entry[label] = {"idle": base, "loaded": s,
                                "added_p95_ms": (s["p95"] or 0) - (base["p50"] or 0),
                                "bulk_down_mbit": bulk[0].get("mbit"), "bulk_up_mbit": bulk[1].get("mbit")}
        out["configs"][cfg] = entry
        print("s1", cfg, json.dumps({k: v["added_p95_ms"] for k, v in entry.items()}), flush=True)
    return out


def s2(cfgs, secs):
    out = {"scenario": "s2", "title": "Capacity cliff: link drops to 20% mid-run", "down_mbit": DOWN,
           "cliff_down_mbit": max(2, DOWN // 5), "configs": {}}
    t_cliff = secs * 0.35
    for cfg in cfgs:
        with Ctrl(cfg):
            sh(NETNS, "modem", str(DOWN), str(UP))
            bg = [spawn("h2", "bulk", "--duration", str(secs + 6), "--reverse")]
            time.sleep(3)
            call_p = spawn("h1", "voice", "--port", str(ODD_PORT), "--duration", str(secs))
            time.sleep(t_cliff)
            sh(NETNS, "modem", str(out["cliff_down_mbit"]), str(UP))
            call = result(call_p, timeout=secs + 30)
            [result(p, timeout=secs + 60) for p in bg]
            sh(NETNS, "modem", str(DOWN), str(UP))
        tr = call.get("trace", [])
        pre = [r for t, r in tr if t < t_cliff - 2]
        base = pctl(pre, 95) or 0
        bins = {}
        for t, r in tr:
            bins.setdefault(int(t), []).append(r)
        rec = None
        sec_list = sorted(b for b in bins if b >= t_cliff)
        for i, b in enumerate(sec_list):
            ok = [pctl(bins[x], 95) < base + 40 for x in sec_list[i:i + 3] if x in bins]
            if len(ok) == 3 and all(ok):
                rec = b - t_cliff
                break
        post = [r for t, r in tr if t >= t_cliff]
        out["configs"][cfg] = {"baseline_p95_ms": base, "peak_ms": max(post) if post else None,
                               "recovery_s": rec, "recovered": rec is not None,
                               "call_p95_after_ms": pctl(post, 95), "t_cliff": t_cliff,
                               "binned_p95": [[b, pctl(v, 95)] for b, v in sorted(bins.items())]}
        print("s2", cfg, out["configs"][cfg]["recovery_s"], flush=True)
    return out


def s3(cfgs, secs):
    out = {"scenario": "s3", "title": "Three TVs + a gamer", "configs": {}}
    for cfg in cfgs:
        with Ctrl(cfg):
            tv = [spawn(h, "video", "--duration", str(secs), "--seg-bytes", "6000000", "--period", "2")
                  for h in ("h2", "h3")]
            tv.append(spawn("h2", "bulk", "--duration", str(secs), "--reverse", "--streams", "4"))
            time.sleep(3)
            game = result(spawn("h1", "game", "--port", str(ODD_PORT), "--duration", str(secs - 4)))
            vids = [result(p, timeout=secs + 60) for p in tv]
        out["configs"][cfg] = {"game": summarize(game),
                               "tv_stalls": [v.get("stalls") for v in vids[:2]],
                               "tv_seg_p95_s": [(v.get("seg_time_s") or {}).get("p95") for v in vids[:2]]}
        print("s3", cfg, out["configs"][cfg]["game"]["p95"], flush=True)
    return out


def s4(secs):
    """Odd-port call is mis-classified; a user intent fixes it, then expires on its own."""
    out = {"scenario": "s4", "title": "Temporary intent: prioritise my call for a while", "phases": {}}
    with Ctrl("qosense_rules") as cx:
        c = cx.c
        bg = load(secs=secs * 3 + 8)
        time.sleep(4)
        for name, action in (("before_intent", None), ("with_intent", "add"), ("after_expiry", "wait")):
            if action == "add":
                c.intents.add("10.0.0.2", "voice", ttl_s=secs + 2, note="my call")
            if action == "wait":
                time.sleep(4)
            call = result(spawn("h1", "voice", "--port", str(ODD_PORT), "--duration", str(secs)))
            out["phases"][name] = summarize(call)
            print("s4", name, out["phases"][name]["p95"], flush=True)
        [result(p, timeout=secs * 3 + 60) for p in bg]
    return out


def s6():
    """kill -9 the controller; the watchdog must apply the safe state within 2 s."""
    out = {"scenario": "s6", "title": "Controller crash -> safe state"}
    sh(NETNS, "shaper", "cake", str(DOWN), str(UP))
    ctl = subprocess.Popen(["python3", "-m", "controller.main", "--ns", f"{NSP}-rtr", "--up-mbit", str(UP),
                            "--down-mbit", str(DOWN), "--classifier", "rules"], cwd=ROOT)
    wd = subprocess.Popen(["python3", "-m", "controller.watchdog", "--ns", f"{NSP}-rtr", "--timeout", "1.5",
                           "--up-kbit", str(UP * 900), "--down-kbit", str(DOWN * 900)], cwd=ROOT)
    time.sleep(10)
    hb = "/run/qosense/heartbeat"
    out["heartbeat_present"] = os.path.exists(hb)
    t0 = time.time()
    ctl.kill()
    safe = False
    while time.time() - t0 < 6:
        q = sh("tc", "qdisc", "show", "dev", "r-wan", ns=f"{NSP}-rtr", check=False).stdout
        if f"bandwidth {UP * 900 // 1000}" in q.replace("Mbit", "") or f"{UP * 900}Kbit" in q:
            safe = True
            break
        time.sleep(0.1)
    out.update({"safe_state_applied": safe, "seconds_to_safe": round(time.time() - t0, 2) if safe else None})
    wd.kill()
    print("s6", out, flush=True)
    return out


# ---------------------------------------------------------------- main
ALL_CFGS = ["fifo", "cake_static", "qosense_rules", "qosense_nocusum", "qosense_model"]


def check(res):
    """Assertions used by CI. Thresholds are relative to the FIFO baseline, not absolute."""
    errs = []
    s1r = res.get("s1", {}).get("configs", {})
    if s1r.get("fifo") and s1r.get("qosense_model"):
        f = s1r["fifo"]["unusual_port"]["added_p95_ms"]
        q = s1r["qosense_model"]["unusual_port"]["added_p95_ms"]
        if not q <= 0.5 * f:
            errs.append(f"S1: QoSense(model) added p95 {q:.1f} ms not <= 50% of FIFO {f:.1f} ms")
        bd = s1r["qosense_model"]["unusual_port"]["bulk_down_mbit"] or 0
        if bd < 0.7 * DOWN:
            errs.append(f"S1: bulk download {bd:.1f} Mbit < 70% of {DOWN}")
    s2r = res.get("s2", {}).get("configs", {})
    if s2r.get("qosense_model") and not s2r["qosense_model"]["recovered"]:
        errs.append("S2: QoSense never recovered after the cliff")
    if "s6" in res and not res["s6"].get("safe_state_applied"):
        errs.append("S6: safe state not applied after controller crash")
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenarios", nargs="*", default=["s1", "s2", "s3", "s4", "s6"])
    ap.add_argument("--quick", action="store_true", help="short durations for CI")
    ap.add_argument("--configs", nargs="*", default=ALL_CFGS)
    ap.add_argument("--assert", dest="do_assert", action="store_true")
    a = ap.parse_args()
    os.makedirs(RES, exist_ok=True)
    secs = 15 if a.quick else 30
    res = {}
    for sc in a.scenarios:
        res[sc] = {"s1": lambda: s1(a.configs, secs), "s2": lambda: s2(a.configs, secs + 15),
                   "s3": lambda: s3(a.configs, secs), "s4": lambda: s4(max(8, secs // 2)),
                   "s6": s6}[sc]()
        res[sc]["meta"] = {"down_mbit": DOWN, "up_mbit": UP, "quick": a.quick,
                           "kernel": os.uname().release, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        with open(os.path.join(RES, f"{sc}.json"), "w") as fh:
            json.dump(res[sc], fh, indent=1)
    errs = check(res)
    for e in errs:
        print("FAIL:", e)
    if a.do_assert and errs:
        sys.exit(1)


if __name__ == "__main__":
    main()
