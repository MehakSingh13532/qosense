#!/usr/bin/env bash
# Fast-forward to origin/deploy (CI only pushes there after every test is green); restart on change.
set -euo pipefail
cd /opt/qosense
git fetch -q origin deploy
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/deploy)" ]; then
  git reset -q --hard origin/deploy
  .venv/bin/pip install -q -r requirements.txt
  systemctl restart qosense.service
  logger -t qosense-update "updated to $(git rev-parse --short HEAD)"
fi
