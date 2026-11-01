# Model card: QoSense flow classifier

**Task.** Classify a LAN flow into `bulk`, `web`, `video` or `voice` (calls and games) every 500 ms.
**Model.** LightGBM multiclass, 150 trees, 9 features.
**Features (no ports, no payload, no IPs).** `pkt_rate`, `mean_pkt_size`, `up_down_ratio`, `rate_cv`, `pkt_size_cv`, `flow_age`, `is_udp`, `host_flows`, `bytes_rate`.
**Data.** 8504 windows from 30 runs of the
automated lab (`models/collect.py`). Labels come from the generator that started the flow. Server
ports are randomised per run. Train/test are split **by run**.

| | LightGBM | Port rules |
|---|---|---|
| Macro-F1 (held-out runs) | 0.996 | 0.553 |
| Voice recall, all ports | 1.0 | 0.3076923076923077 |
| Voice recall, ports no rule knows | 1.0 | 0.0 |

Top features by gain:
- `up_down_ratio`: 24966
- `is_udp`: 24229
- `pkt_rate`: 21912
- `host_flows`: 9638
- `rate_cv`: 7157

**Intended use.** Choosing a CAKE tin for a flow; always behind hysteresis, user pins and a
rule fallback when confidence is below 0.55.
**Limitations.** Trained only on synthetic lab generators (UDP echo calls, HTTP segments, iperf3).
Real apps (QUIC video, WebRTC with FEC/simulcast, encrypted games) differ; expect lower accuracy
until retrained on real captures such as CESNET-QUIC22. Not evaluated for fairness or adversarial
flows (a bulk flow imitating call-like packets would be promoted; the policy's bulk floor limits damage).
