#!/usr/bin/env bash
# Fallback if Let's Encrypt for the Azure hostname fails: free Cloudflare quick tunnel (no account).
# Limits: ~200 in-flight requests, no SSE (we use WebSockets), URL changes whenever cloudflared restarts.
set -euo pipefail
if ! command -v cloudflared >/dev/null; then
  arch=$(dpkg --print-architecture)
  curl -fsSL -o /tmp/cloudflared.deb "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}.deb"
  dpkg -i /tmp/cloudflared.deb
fi
exec cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000
