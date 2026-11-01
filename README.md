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

## Quick start
```bash
# Linux with root (WSL2 works if its kernel has CAKE+netem; otherwise use the Azure VM)
pip install -r requirements.txt
make test                       # unit tests, no kernel needed
make lab                        # build the namespace lab (probes kernel modules first)
make train                      # collect lab traffic, train the classifier
make experiments && make report # S1-S6 and experiments/report.html
make demo                       # dashboard at http://127.0.0.1:8000
```
Full instructions, versions and the VM deployment are in [docs/SETUP.md](docs/SETUP.md).
Design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Honest limits: [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Status
See [docs/PROGRESS.md](docs/PROGRESS.md) for what has been verified where.
