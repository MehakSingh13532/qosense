#!/usr/bin/env bash
# Asserts the ct-mark -> CAKE tin mapping used by controller/common.py (1=Bulk 2=BE 3=Video 4=Voice)
# by sending a burst of UDP packets per mark through the lab router and reading per-tin packet counters.
# Run as root after `lab/netns.sh up`.
set -euo pipefail
NSP=${NSP:-qs}
RTR=${NSP}-rtr
x() { ip netns exec "$1" "${@:2}"; }

x "$RTR" tc qdisc replace dev r-wan root cake bandwidth 50mbit diffserv4 dual-srchost nat fwmark 0x0f regional
x "$RTR" nft flush chain inet qosense qfwd
x "$RTR" nft add rule inet qosense qfwd meta mark set ct mark \& 0x0f

echo "--- raw tc json from this kernel (field names)"
x "$RTR" tc -s -j qdisc show dev r-wan | python3 -c '
import json,sys
q=[d for d in json.load(sys.stdin) if d.get("kind")=="cake"][0]
print(json.dumps({k:v for k,v in q.items() if k!="tins"})[:500])
print(json.dumps(q["tins"][0])[:700])'

pkts() {   # per-tin sent packets, in tc's tin order, one per line
  x "$RTR" tc -s -j qdisc show dev r-wan | python3 -c '
import json,sys
q=[d for d in json.load(sys.stdin) if d.get("kind")=="cake"][0]
for t in q["tins"]: print(t.get("sent_packets", t.get("packets", 0)))'
}

fail=0
for mark in 1 2 3 4; do
  port=$((40000 + mark))     # UDP echo ports the lab servers listen on (lab/servers.py)
  before=($(pkts))
  x "${NSP}-h1" python3 - "$port" <<'PY' &
import socket, sys, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("198.51.100.10", int(sys.argv[1])))
s.settimeout(0.05)
for i in range(300):
    try:
        s.send(b"x" * 100)
    except OSError:
        pass
    try:
        s.recv(2048)
    except OSError:
        pass
    time.sleep(0.01)
PY
  bg=$!
  sleep 0.5
  x "$RTR" conntrack -U -p udp -d 198.51.100.10 --dport "$port" -m "$mark" >/dev/null
  wait $bg
  after=($(pkts))
  best=-1; bestd=0
  for i in "${!after[@]}"; do d=$(( after[i] - before[i] )); [ $d -gt $bestd ] && { bestd=$d; best=$i; }; done
  want=$((mark - 1))
  echo "mark=$mark -> tin index $best (delta pkts $bestd); expected index $want"
  [ "$best" = "$want" ] || fail=1
done
exit $fail
