# Progress log (resume point if a session is interrupted)

## Verified in the authoring sandbox (no CAKE/netem in its kernel)
* 27 unit tests pass (`make test`): classifier + hysteresis + pins, features, estimator + CUSUM, policy + intents,
  enforcer + circuit breaker + rollback, verifier, telemetry parsers, control loop with fake telemetry,
  training pipeline (synthetic CSV only), report renderer (synthetic fixture), API (rate limit, validation, WebSocket).
* Traffic generators and servers run on loopback.

## Verified on a real (partial) kernel in the sandbox: `QS_PARTIAL=1 lab/netns.sh up`
Topology, routing, NAT, nft rules, conntrack accounting and tbf work; CAKE/netem/IPv6 are absent there.
* Bugs this found and fixed: `fwd` is an nft keyword (chain renamed `qfwd`), nft needs multi-line chains,
  UDP echo must answer from the address it was sent to (else NAT leaves the flow UNREPLIED), the server launch
  held the caller's pipe open, `pkill -f` could kill unrelated shells.
* `telemetry.parse_conntrack` parses real `conntrack -L -o extended` output; `conntrack -U -m 4` really sets the mark.
* `models/collect.py` produced labelled rows from real lab flows; class medians separate cleanly
  (voice 111 pps x 174 B symmetric; bulk large down packets; video bursty; web sparse). A 3-run smoke training got
  voice recall 1.0 vs 0.0 for port rules on unusual ports. Tiny sample: indicative only, not a result.

## Verified on the GitHub Actions runner (full kernel, ubuntu-24.04)
* Lab up (CAKE, netem, IPv4 + IPv6), connectivity, and the tin mapping test: mark 1..4 lands in tin index 0..3
  (Bulk, Best Effort, Video, Voice), asserted from per-tin packet counters. `tc -s -j` prints CAKE `options`
  (incl. `fwmark`) and per-tin stats in a second JSON object.
* `models/collect.py` collects ~160-300 labelled rows per run under both FIFO and CAKE; `train.py` completes.
* Bug found there: the zip upload dropped the executable bit on `lab/netns.sh`. All callers now run it via `bash`,
  and `bootstrap.sh` / the systemd units no longer rely on the file mode.

## NOT yet verified
* `tc -s -j` field names in `parse_cake_stats` against the second JSON object printed above
* `experiments/run.py` thresholds in `check()` (relative to FIFO; tune after the first real numbers)
* `deploy/bootstrap.sh` on Azure; Let's Encrypt for the `cloudapp.azure.com` name

## Next
1. Push to GitHub, watch the `lab` job, fix whatever the real kernel disagrees with.
2. Run full (non-quick) experiments via workflow_dispatch; read the report.
3. Create the Azure VM, run bootstrap, smoke test the public URL.
