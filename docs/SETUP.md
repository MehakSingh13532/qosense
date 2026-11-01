# Setup guide

## 0. What you need
* Linux with root and kernel modules `sch_cake`, `sch_netem`, `sch_tbf`, `cls_*`, `veth`, `nf_tables`. GitHub's Ubuntu 24.04
  runners and Ubuntu 24.04 on Azure work. `lab/netns.sh probe-modules` checks this by *function* (it adds a CAKE and a netem qdisc),
  and installs `linux-modules-extra-$(uname -r)` only on kernels older than 7.x where that package still exists.
* WSL2: run `zcat /proc/config.gz | grep -E 'SCH_CAKE|SCH_NETEM|CONFIG_NET_NS|NF_TABLES='`. If CAKE/netem are not `y` or `m`,
  develop the lab on the VM (VS Code Remote-SSH) and use WSL2 for Python, tests and the dashboard.
* Python 3.12+, `apt install iproute2 nftables conntrack iperf3 irtt`.

## 1. Run locally
```bash
git clone https://github.com/<you>/qosense && cd qosense
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt pytest
make test
sudo -E env PATH=$PATH make lab        # builds namespaces qs-rtr qs-mdm qs-isp qs-h1..h3
sudo -E env PATH=$PATH make train      # ~3 min: collects labelled lab traffic, trains models/model.txt
sudo -E env PATH=$PATH make experiments
make report && xdg-open experiments/report.html
sudo -E env PATH=$PATH make demo       # http://127.0.0.1:8000
```
Rates: `DOWN_MBIT=40 UP_MBIT=10 make lab` for small machines.

## 2. Free public deployment (Azure for Students + GitHub)
1. GitHub: create a **public** repo `qosense`, push this code to `main`. Settings → Pages → Source: *GitHub Actions*.
   CI runs, and on success updates the `deploy` branch and publishes the report.
2. Azure for Students (https://azure.microsoft.com/free/students, no card): check *Subscription → Usage + quotas* for
   Standard Basv2 vCPUs, then use the *Free services* page to create **Standard_B2ats_v2**, Ubuntu 24.04, **64 GiB** disk,
   Standard public IP with DNS name label `qosense`, SSH key, inbound 22/80/443. Set a budget alert (the public IP costs ≈ $0.005/h of your credit).
   Fallbacks: another region, then Standard_B1s with `DOWN_MBIT=20 UP_MBIT=5`.
3. On the VM:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/<you>/qosense/main/deploy/bootstrap.sh | \
     sudo REPO=<you>/qosense QOSENSE_HOST=qosense.<region>.cloudapp.azure.com bash
   ```
   Caddy obtains the certificate automatically. A timer pulls the CI-approved `deploy` branch every 5 minutes.
4. If HTTPS issuance fails: `sudo bash /opt/qosense/deploy/tunnel.sh` prints a temporary `trycloudflare.com` URL.

## 3. Operations
| Task | Command |
|---|---|
| Logs | `journalctl -u qosense -f` |
| Manual safe state | `sudo bash /opt/qosense/deploy/safe-state.sh` |
| Change lab rates | edit `/etc/qosense.env`, `sudo systemctl restart qosense-lab qosense` |
| Crash test | `sudo kill -9 $(systemctl show -p MainPID --value qosense)`; the watchdog applies the safe state within ~2 s |
