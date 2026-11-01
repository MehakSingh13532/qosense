# Known limitations (stated up front)

* **Emulation, not an access network.** The lab is Linux namespaces with a tbf+netem "modem". No DOCSIS/GPON
  scheduling, no Wi-Fi airtime contention, no real ISP shaping or cross-traffic.
* **Synthetic traffic.** Calls and games are UDP echo at fixed rates, video is HTTP segments, bulk is iperf3.
  Real WebRTC, QUIC video and encrypted games behave differently; the classifier needs retraining on real captures.
* **Classifier scope.** Four classes. A bulk flow shaped to look like a call would be promoted; the bulk floor and
  verifier limit the damage but do not remove it.
* **Capacity estimator** is a simplified re-implementation of the delay-based idea in cake-autorate, using one
  UDP probe in the best-effort tin; it assumes the access link is the bottleneck.
* **Download shaping** in the lab uses the LAN egress; the real-WAN IFB path is documented and demonstrated only as a command.
* **Not built:** eBPF classifier, learned capacity predictor, natural-language intents, RDK-B/prpl packaging,
  multi-gateway fleet management, IPv6 flow-level control (IPv6 connectivity is tested; marking uses IPv4 conntrack keys).
* **Throughput of the VM.** The free Azure B-series VM has 1 GiB RAM and burst CPU credits, so the live lab is shaped to 40/10 Mbit;
  100/20 Mbit sweeps run in CI.
* **Prior art we did not reproduce:** qosify (eBPF DSCP heuristics + CAKE) and sqm-scripts layer_cake. We did not find an
  open-source combination of CAKE + ML flow classification + an intent API; that is a search result, not proof none exists.

## Found in our own measurements (CI run `980fb51`)
* **Single-run numbers.** The CI report is one quick run (15 s calls) per scenario. Expect ±10 ms run-to-run
  variation in steady-state latency. Use `workflow_dispatch` full runs (30 s) and repeat them before quoting
  small differences.
* **Static CAKE beats QoSense when the rate is known and stable.** In S1, static CAKE at 95 % of the line adds
  ~1 ms at p95; QoSense adds ~10–13 ms because it keeps probing for more capacity, at 5 ms of delay before
  backing off. QoSense earns its place when capacity changes (S2), not on a fixed line.
* **Estimator over-reaction in S7.** With the video call promoted to the voice tin by the AI classifier, the
  probe delay rose to 15–57 ms and the estimator cut the upload shaper from 20 to ~6 Mbit/s. It then crept back
  slowly, because it only re-probes up to 97 % of the last rate that caused bufferbloat, which by then was the
  already-reduced rate. We have not yet proven why the delay rose. Two likely causes:
  - the controller assigns the probe delay to whichever direction looks busier, and the measured upload use reads
    above 100 %, so download-side delay can be blamed on the upload;
  - each step of a cascade overwrites the remembered "bad rate" with a lower one.
  Planned fix: remember the highest rate in a bloat episode, and assign delay by direction using one-way delay.
* **Thresholds are lab-tuned.** The 5 ms delay threshold, the probe ceiling and the CUSUM limits were tuned on
  this emulated link. Real lines with jittery DOCSIS/4G latency need different values (cake-autorate exposes
  the same knobs for this reason).
* **The AI classifier's advantage is narrow by design.** It helps only flows that both (a) use ports the rules
  don't know and (b) need more than their fair share. Video calls and cloud gaming fit; plain voice calls are
  already protected by CAKE's flow isolation.
