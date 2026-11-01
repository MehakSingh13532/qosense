#!/usr/bin/env python3
"""Collect a labelled flow-feature dataset from the lab (run as root, lab up).

Labels come from *which generator started the flow* (voice/game -> voice, video, bulk, web),
never from ports. Server ports are chosen at random per run among several, so a model that
leaned on a port number would not generalise. `dport` is stored only so the rule baseline
can be scored on the same rows; it is NOT a model feature (see controller/features.py).
"""
import argparse
import csv
import os
import random
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from controller import telemetry                                    # noqa: E402
from controller.common import BULK, VIDEO, VOICE, WEB               # noqa: E402
from controller.features import FEATURES, FlowTracker               # noqa: E402

NSP = os.environ.get("NSP", "qs")
TRAFFIC = os.path.join(ROOT, "lab", "traffic.py")
NETNS = os.path.join(ROOT, "lab", "netns.sh")
ECHO_PORTS = [3478, 7777, 44444, 51234, 40000, 40001, 40002, 40003, 40004]
HOSTIP = {"h1": "10.0.0.2", "h2": "10.0.0.3", "h3": "10.0.0.4"}

# (host, generator args, label, server dport that identifies the flow, min bytes/s to be kept)
def mixes(secs):
    d = str(secs)
    v1, v2 = random.sample(ECHO_PORTS, 2)
    return [
        [("h1", ["voice", "--port", str(v1), "--duration", d], VOICE, v1, 0),
         ("h2", ["bulk", "--duration", d, "--reverse"], BULK, 5201, 50_000),
         ("h3", ["web", "--duration", d], WEB, 8000, 0)],
        [("h1", ["game", "--port", str(v1), "--duration", d], VOICE, v1, 0),
         ("h2", ["video", "--duration", d, "--seg-bytes", "4000000", "--period", "2"], VIDEO, 8000, 0),
         ("h3", ["video", "--duration", d, "--seg-bytes", "2500000", "--period", "2"], VIDEO, 8000, 0),
         ("h2", ["bulk", "--duration", d, "--streams", "4"], BULK, 5202, 50_000)],
        [("h1", ["voice", "--port", str(v1), "--duration", d], VOICE, v1, 0),
         ("h2", ["game", "--port", str(v2), "--duration", d], VOICE, v2, 0),
         ("h3", ["bulk", "--duration", d, "--reverse", "--streams", "6"], BULK, 5201, 50_000),
         ("h3", ["web", "--duration", d], WEB, 8000, 0)],
    ]


def collect_run(run_id, procs_spec, secs, w):
    sh = lambda *c: subprocess.run(c, capture_output=True, text=True)
    sh("ip", "netns", "exec", f"{NSP}-rtr", "conntrack", "-F")
    procs = [subprocess.Popen(["ip", "netns", "exec", f"{NSP}-{h}", "python3", TRAFFIC, *args],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for h, args, *_ in procs_spec]
    want = {(HOSTIP[h], dport): (label, minb) for h, _, label, dport, minb in procs_spec}
    tracker, t0, n = FlowTracker(), time.monotonic(), 0
    while time.monotonic() - t0 < secs - 1:
        time.sleep(0.5)
        now = time.monotonic()
        tracker.update(now, telemetry.read_conntrack(f"{NSP}-rtr"))
        if now - t0 < 4:          # warm-up: windows not full yet
            continue
        for key in tracker.active_keys(now):
            lab = want.get((key.src, key.dport))
            f = tracker.features(key, now)
            if lab is None or f is None or f["bytes_rate"] < lab[1]:
                continue
            w.writerow([run_id, lab[0], key.dport] + [f[x] for x in FEATURES]); n += 1
    for p in procs:
        p.wait(timeout=secs + 40)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="models/lab_flows.csv")
    ap.add_argument("--secs", type=int, default=14)
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--shapers", nargs="*", default=["fifo", "cake"], help="router shapers to collect under")
    a = ap.parse_args()
    random.seed(a.seed)
    with open(a.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["run", "label", "dport"] + FEATURES)
        run_id = 0
        for shaper in a.shapers:
            subprocess.run(["bash", NETNS, "shaper", shaper, os.environ.get("DOWN_MBIT", "100"),
                            os.environ.get("UP_MBIT", "20")], check=True)
            for _ in range(a.repeats):
                for mix in mixes(a.secs):
                    run_id += 1
                    rows = collect_run(run_id, mix, a.secs, w)
                    print(f"run {run_id} ({shaper}): {rows} rows", flush=True)
    print("wrote", a.out)


if __name__ == "__main__":
    main()
