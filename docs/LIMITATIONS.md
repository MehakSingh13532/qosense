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

## Found in our own measurements (CI runs `980fb51` and its revert, same code)
* **Run-to-run noise is large.** Two quick runs (15 s calls) of identical code differed a lot. Rules-only steady
  latency in S1 was +12.7 ms in one run and +78 ms in the other. S2 recovery without the cliff detector was 7.5 s
  in one run and "did not recover" in the other. Treat single numbers as indicative. Run `workflow_dispatch` full
  runs (30 s) several times before quoting small differences.
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
* **A fix we tried and reverted (run `6e7db3b`).** The fix had two parts:
  - a "futile cut" guard: stop cutting when repeated cuts don't lower the delay while the link still carries the
    full shaper rate;
  - remembering the highest rate of a bloat episode as the bad rate.
  It cut S7 AI-classifier loss from 20.5 % to 8.3 % and raised upload throughput from ~4 to ~12 Mbit/s. But S7 call
  latency rose from 34 to 88 ms, and S2 cliff recovery slowed: rules-only 8.5 → 14.5 s, no cliff detector
  7.5 → 16.5 s. Cliff recovery is the headline result, so we reverted it. The proper fix is to measure one-way delay
  per direction so delay is never blamed on the wrong shaper.
* **Thresholds are lab-tuned.** The 5 ms delay threshold, the probe ceiling and the CUSUM limits were tuned on
  this emulated link. Real lines with jittery DOCSIS/4G latency need different values (cake-autorate exposes
  the same knobs for this reason).
* **The AI classifier's advantage is narrow by design.** It helps only flows that both (a) use ports the rules
  don't know and (b) need more than their fair share. Video calls and cloud gaming fit; plain voice calls are
  already protected by CAKE's flow isolation.
