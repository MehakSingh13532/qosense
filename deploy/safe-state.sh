#!/usr/bin/env bash
# Manual safe state: drop all per-flow overrides and fall back to static CAKE just under the modem rates.
set -euo pipefail
source /etc/qosense.env
R=${NSP}-rtr
ip netns exec "$R" nft flush chain inet qosense qfwd
ip netns exec "$R" tc qdisc replace dev r-wan  root cake bandwidth "$((UP_MBIT*900))kbit"   diffserv4 dual-srchost nat fwmark 0x0f regional
ip netns exec "$R" tc qdisc replace dev br-lan root cake bandwidth "$((DOWN_MBIT*900))kbit" diffserv4 dual-dsthost nat fwmark 0x0f regional
echo "safe state applied"
