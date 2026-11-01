# QoSense: intent-aware adaptive QoS for mixed home broadband traffic

Prototype for Tata Elxsi Teliport Season 4, Case Study 3. A controller on a Linux home gateway senses
what traffic is flowing and how much capacity the line really has, then drives CAKE (`tc`) so calls and
games stay responsive while downloads and streams share what is left, and it can be told *what matters
right now* through a temporary-intent API.

* **Classifier.** Rule baseline (ports, DSCP) vs a LightGBM model that never sees a port number, only behaviour.
* **Capacity estimator.** cake-autorate-style delay-driven rate control plus a CUSUM cliff detector.
* **Policy engine.** Latency budgets per class, user intents with TTL, a 5% bulk floor, deterministic output.
* **Enforcer.** Conntrack mark, nft copies it to the packet mark, CAKE `diffserv4 fwmark` picks the tin; breaker, rollback, watchdog.
* **Lab.** Network namespaces: 3 LAN devices, router, an emulated bloated modem (tbf + netem), ISP/servers.
* **Experiments.** FIFO vs static CAKE vs QoSense, run automatically in CI and published as a report.

## Where the evidence lives
Every headline number is produced by CI on a clean GitHub runner and published, not typed by hand:
the **experiment report** (GitHub Pages), `experiments/results/*.json`, and `models/MODEL_CARD.md`.
A live demo (dashboard with A/B mode switch and scenario buttons) runs on a free Azure VM; it is a bonus
and the report is the record.

## Results (two CI runs of the same code, kernel 7.0.0-1017-azure, 100/20 Mbit/s lab)
Live report: **https://mehaksingh13532.github.io/qosense/** · raw data: `results/*.json` on the same site.
Each cell shows the range across two quick-mode CI runs (15 s calls) of identical code: commit `980fb51` and
its revert on 2026-11-01. The spread between those two runs shows the run-to-run noise.

| Scenario | FIFO (no QoS) | Static CAKE | QoSense, rules | QoSense, AI classifier |
|---|---|---|---|---|
| **S2** capacity cliff 100 → 20 Mbit/s: time for the call to recover | never (p95 after ≈ 8.4 s) | never (p95 after ≈ 3.3 s) | 3.5–8.5 s | **2.5–3.5 s** |
| **S7** video call, same laptop uploads a 16-stream backup, call on an **unusual port**: call packet loss | 25–26 % (RTT +4.5 s) | 47–48 % | 46–47 % | **15–20.5 %** |
| S7, same call on its **well-known port** (rules recognise it): loss | 26 % | 48–49 % | **6 %** | **0 %** |
| **S1** voice call, ISO download on another device: added p95 in steady state (unusual port) | +3.0–3.1 s | **+1.0 ms** | +12.7–78 ms | +10.4–16.9 ms |
| **S3** game RTT p95 next to two 4K TVs + a download | 592 ms | 20.3–20.5 ms | 20.5–27 ms | 33–42 ms |
| **S6** controller killed with SIGKILL: time to safe state | | | 1.14–1.35 s | |

What this shows:
* **Adapting to the real capacity is the biggest win.** When the line drops to 20 % mid-call, FIFO and static
  CAKE (tuned for the old rate) never recover: the call sits at seconds of delay. QoSense notices the cliff from
  delay and throughput and recovers in 2.5–3.5 s. Without the CUSUM cliff detector the same controller took 7.5 s
  in one run and did not recover within the test in the other, so the cliff detector is what makes recovery reliable.
* **Classification matters only when a flow needs more than its fair share.** CAKE's per-device and per-flow
  fairness already protects a small voice call (S1), so there a static CAKE at a known rate is best: +1 ms, against
  +10–17 ms for QoSense with the AI classifier, which pays for continuously probing the capacity. A ~2.4 Mbit/s video
  call next to a 16-stream upload on the same device is different (S7): in the best-effort tin it gets ~1/17 of the
  uplink and loses half its packets. Rules fix that only on well-known ports (6 % loss). The AI classifier, which
  never sees ports, also recognises the call on an unusual port and cuts loss from ~47 % to 15–20 %.
* **Honest caveat on S7.** In the AI-classifier runs, the capacity estimator over-reacted after the call was promoted:
  in the run we traced it cut the upload shaper from 20 to ~6 Mbit/s and recovered slowly. That cost upload throughput (4–7 vs 15 Mbit/s) and is why
  loss is 15–20 % rather than ~0 %. We tried a fix and reverted it; see [docs/LIMITATIONS.md](docs/LIMITATIONS.md).
* **Safety works.** The watchdog applies a static safe state 1.1–1.4 s after the controller dies.

## Quick start
```bash
# Linux with root (WSL2 works if its kernel has CAKE+netem; otherwise use the Azure VM)
pip install -r requirements.txt
make test                       # unit tests, no kernel needed
make lab                        # build the namespace lab (probes kernel modules first)
make train                      # collect lab traffic, train the classifier
make experiments && make report # S1-S4, S6, S7 and experiments/report.html
make demo                       # dashboard at http://127.0.0.1:8000
```
Full instructions, versions and the VM deployment are in [docs/SETUP.md](docs/SETUP.md).
Design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Honest limits: [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Status
See [docs/PROGRESS.md](docs/PROGRESS.md) for what has been verified where.
