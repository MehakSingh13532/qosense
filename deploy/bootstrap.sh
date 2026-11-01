#!/usr/bin/env bash
# One-command setup for an Ubuntu 24.04 VM (Azure B2ats_v2 / B1s). Idempotent. Run as root:
#   curl -fsSL https://raw.githubusercontent.com/<you>/qosense/main/deploy/bootstrap.sh | sudo REPO=<you>/qosense bash
set -euo pipefail
REPO=${REPO:?set REPO=<github-user>/qosense}
BRANCH=${BRANCH:-deploy}
HOST=${QOSENSE_HOST:-}          # e.g. qosense.centralindia.cloudapp.azure.com ; empty => plain HTTP on :80
DIR=/opt/qosense

echo "== swap (1 GiB RAM VM)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq iproute2 nftables conntrack iperf3 irtt python3-venv python3-pip git curl \
    debian-keyring debian-archive-keyring apt-transport-https gpg
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy
fi

echo "== code"
if [ -d "$DIR/.git" ]; then git -C "$DIR" fetch -q origin "$BRANCH" && git -C "$DIR" checkout -q "$BRANCH" && git -C "$DIR" reset -q --hard "origin/$BRANCH"
else git clone -q --branch "$BRANCH" "https://github.com/$REPO.git" "$DIR" || git clone -q "https://github.com/$REPO.git" "$DIR"; fi
chmod +x "$DIR"/deploy/*.sh "$DIR"/lab/*.sh "$DIR"/tests/ci/*.sh 2>/dev/null || true
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

echo "== kernel modules (probe-then-install)"
bash "$DIR/lab/netns.sh" probe-modules

echo "== config + services"
[ -f /etc/qosense.env ] || cat > /etc/qosense.env <<ENV
NSP=qs
DOWN_MBIT=40
UP_MBIT=10
DELAY_MS=10
BLOAT_MS=400
QOSENSE_HOST=${HOST}
ENV
install -m 644 "$DIR"/deploy/systemd/*.service "$DIR"/deploy/systemd/*.timer /etc/systemd/system/
install -m 644 "$DIR/deploy/caddy/Caddyfile" /etc/caddy/Caddyfile
mkdir -p /etc/systemd/system/caddy.service.d
printf '[Service]\nEnvironmentFile=/etc/qosense.env\n' > /etc/systemd/system/caddy.service.d/env.conf
systemctl daemon-reload
systemctl enable --now qosense-lab.service qosense.service qosense-watchdog.service qosense-update.timer
systemctl restart caddy
sleep 5
curl -fsS http://127.0.0.1:8000/healthz && echo && echo "QoSense is up. Public URL: ${HOST:-http://<this-ip>/}"
