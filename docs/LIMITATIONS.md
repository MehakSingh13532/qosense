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
