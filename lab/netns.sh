#!/usr/bin/env bash
# QoSense lab: a household + a bloated "modem" + an ISP/server side, all in network namespaces.
#
#   h1,h2,h3 --br-lan--[ rtr ]--r-wan--[ mdm ]--m-i--[ isp: servers ]
#                         ^ QoSense shapes here   ^ emulated bloated bottleneck (tbf + netem)
#
# Usage (as root):  lab/netns.sh up | down | status
#                   lab/netns.sh modem <down_mbit> <up_mbit> [bloat_ms] [delay_ms]
#                   lab/netns.sh shaper fifo|cake <down_mbit> <up_mbit>
#                   lab/netns.sh probe-modules
# Env: NSP (namespace prefix, default qs)  DOWN_MBIT (100)  UP_MBIT (20)  DELAY_MS (10)  BLOAT_MS (400)
set -euo pipefail

NSP=${NSP:-qs}
DOWN_MBIT=${DOWN_MBIT:-100}
UP_MBIT=${UP_MBIT:-20}
DELAY_MS=${DELAY_MS:-10}
BLOAT_MS=${BLOAT_MS:-400}
RTR=${NSP}-rtr; MDM=${NSP}-mdm; ISP=${NSP}-isp
HOSTS=(1 2 3)
MODULES=(sch_cake sch_netem sch_fq_codel sch_tbf sch_ingress ifb act_mirred cls_matchall cls_fw act_ctinfo veth nf_conntrack nf_tables nft_ct nft_masq bridge)

need_root() { [ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }; }
x() { ip netns exec "$1" "${@:2}"; }
HAVE_V6=0; [ -d /proc/sys/net/ipv6 ] && ip -6 addr show lo >/dev/null 2>&1 && HAVE_V6=1
v6() { if [ "$HAVE_V6" = 1 ]; then "$@"; fi; }   # IPv6 steps are skipped on kernels without IPv6

probe_modules() {
  # Works on both runner kernels: 6.x (maybe -extra) and 7.x (everything in linux-modules).
  local missing=()
  for m in "${MODULES[@]}"; do modprobe "$m" 2>/dev/null || missing+=("$m"); done
  if [ ${#missing[@]} -gt 0 ]; then
    local kver major; kver=$(uname -r); major=${kver%%.*}
    echo "modules not loadable: ${missing[*]} (kernel $kver)"
    if [ "$major" -lt 7 ] && command -v apt-get >/dev/null; then
      apt-get update -qq && apt-get install -y -qq "linux-modules-extra-$kver" || true
      missing=(); for m in "${MODULES[@]}"; do modprobe "$m" 2>/dev/null || missing+=("$m"); done
    fi
  fi
  # built-in modules are fine: judge by function, not by modprobe exit code.
  ip link add qsprobe0 type dummy 2>/dev/null || true
  local ok=1
  tc qdisc add dev qsprobe0 root cake bandwidth 10mbit 2>/dev/null || { echo "FATAL: sch_cake unavailable"; ok=0; }
  tc qdisc replace dev qsprobe0 root netem delay 1ms 2>/dev/null || { echo "FATAL: netem unavailable"; ok=0; }
  ip link del qsprobe0 2>/dev/null || true
  [ $ok = 1 ] && echo "kernel $(uname -r): cake + netem OK (unloadable names: ${missing[*]:-none})"
  [ $ok = 1 ]
}

up() {
  need_root; down 2>/dev/null || true
  # QS_PARTIAL=1: bring up topology/NAT/conntrack only (no CAKE, no netem) on kernels that lack them.
  # Used to smoke-test the plumbing in restricted sandboxes; experiments need the full lab.
  [ "${QS_PARTIAL:-0}" = 1 ] || probe_modules
  for ns in "$RTR" "$MDM" "$ISP"; do ip netns add "$ns"; x "$ns" ip link set lo up; done
  for h in "${HOSTS[@]}"; do ip netns add "${NSP}-h$h"; x "${NSP}-h$h" ip link set lo up; done

  # --- LAN: hosts <-> bridge in rtr
  x "$RTR" ip link add br-lan type bridge
  x "$RTR" ip addr add 10.0.0.1/24 dev br-lan
  v6 x "$RTR" ip addr add fd00:1::1/64 dev br-lan nodad
  for h in "${HOSTS[@]}"; do
    ip link add "h$h-r" netns "${NSP}-h$h" type veth peer name "r-h$h" netns "$RTR"
    x "$RTR" ip link set "r-h$h" master br-lan up
    x "${NSP}-h$h" ip addr add "10.0.0.$((h+1))/24" dev "h$h-r"
    v6 x "${NSP}-h$h" ip addr add "fd00:1::$((h+1))/64" dev "h$h-r" nodad
    x "${NSP}-h$h" ip link set "h$h-r" up
    x "${NSP}-h$h" ip route add default via 10.0.0.1
    v6 x "${NSP}-h$h" ip -6 route add default via fd00:1::1
  done
  x "$RTR" ip link set br-lan up

  # --- WAN: rtr <-> mdm <-> isp
  ip link add r-wan netns "$RTR" type veth peer name m-r netns "$MDM"
  ip link add m-i netns "$MDM" type veth peer name i-m netns "$ISP"
  x "$RTR" ip addr add 192.0.2.2/30 dev r-wan;  v6 x "$RTR" ip addr add fd00:2::2/64 dev r-wan nodad
  x "$MDM" ip addr add 192.0.2.1/30 dev m-r;    v6 x "$MDM" ip addr add fd00:2::1/64 dev m-r nodad
  x "$MDM" ip addr add 192.0.2.5/30 dev m-i;    v6 x "$MDM" ip addr add fd00:4::1/64 dev m-i nodad
  x "$ISP" ip addr add 192.0.2.6/30 dev i-m;    v6 x "$ISP" ip addr add fd00:4::2/64 dev i-m nodad
  x "$ISP" ip addr add 198.51.100.10/32 dev lo; v6 x "$ISP" ip addr add fd00:3::10/128 dev lo nodad
  for pair in "$RTR r-wan" "$MDM m-r" "$MDM m-i" "$ISP i-m"; do set -- $pair; x "$1" ip link set "$2" up; done
  x "$RTR" ip route add default via 192.0.2.1;      v6 x "$RTR" ip -6 route add default via fd00:2::1
  x "$MDM" ip route add default via 192.0.2.6;      v6 x "$MDM" ip -6 route add default via fd00:4::2
  v6 x "$MDM" ip -6 route add fd00:1::/64 via fd00:2::2
  x "$ISP" ip route add 192.0.2.0/30 via 192.0.2.5; v6 x "$ISP" ip -6 route add fd00:1::/64 via fd00:4::1
  v6 x "$ISP" ip -6 route add fd00:2::/64 via fd00:4::1
  for ns in "$RTR" "$MDM" "$ISP"; do
    x "$ns" sysctl -qw net.ipv4.ip_forward=1
    v6 x "$ns" sysctl -qw net.ipv6.conf.all.forwarding=1
  done
  x "$RTR" sysctl -qw net.netfilter.nf_conntrack_acct=1 net.netfilter.nf_conntrack_timestamp=1 || true

  # --- router netfilter: NAT + ct mark -> skb mark (CAKE fwmark reads skb->mark)
  x "$RTR" nft -f - <<'EOF'
flush ruleset
table ip nat {
  chain post {
    type nat hook postrouting priority srcnat; policy accept;
    oifname "r-wan" masquerade
  }
}
table inet qosense {
  chain qfwd {
    type filter hook forward priority mangle; policy accept;
    meta mark set ct mark & 0x0f
  }
}
EOF
  modem "$DOWN_MBIT" "$UP_MBIT" "$BLOAT_MS" "$DELAY_MS"
  shaper fifo "$DOWN_MBIT" "$UP_MBIT"
  start_servers
  echo "lab up: down=${DOWN_MBIT}Mbit up=${UP_MBIT}Mbit delay=${DELAY_MS}ms bloat=${BLOAT_MS}ms"
}

# Emulated bloated modem: a deep tbf (bloat_ms worth of buffering) + netem base delay, each direction.
modem() {
  local down=$1 up=$2 bloat=${3:-$BLOAT_MS} delay=${4:-$DELAY_MS}
  one() {  # ns dev mbit
    local ns=$1 dev=$2 mbit=$3
    local limit=$(( mbit * 125 * bloat ))        # bytes = rate(B/ms) * bloat_ms
    [ $limit -lt 30000 ] && limit=30000
    x "$ns" tc qdisc replace dev "$dev" root handle 1: tbf rate "${mbit}mbit" burst 64kb limit "$limit"
    [ "${QS_PARTIAL:-0}" = 1 ] || x "$ns" tc qdisc replace dev "$dev" parent 1:1 handle 10: netem delay "${delay}ms" limit 100000
  }
  one "$MDM" m-r "$down"     # downlink: mdm -> rtr
  one "$MDM" m-i "$up"       # uplink:   mdm -> isp
  echo "modem: down=${down} up=${up} Mbit"
}

shaper() {
  local kind=$1 down=$2 up=$3
  case "$kind" in
    fifo)
      x "$RTR" tc qdisc replace dev r-wan root pfifo limit 1000
      x "$RTR" tc qdisc replace dev br-lan root pfifo limit 1000 ;;
    cake)   # static CAKE just below the modem rates; DSCP only unless the controller marks flows
      local d=$(( down * 950 )) u=$(( up * 950 ))
      x "$RTR" tc qdisc replace dev r-wan root cake bandwidth "${u}kbit" diffserv4 dual-srchost nat fwmark 0x0f regional
      x "$RTR" tc qdisc replace dev br-lan root cake bandwidth "${d}kbit" diffserv4 dual-dsthost nat fwmark 0x0f regional ;;
    *) echo "shaper fifo|cake" >&2; exit 2 ;;
  esac
}

start_servers() {
  # UDP echo on several ports + HTTP bytes server + iperf3, all in the ISP namespace.
  x "$ISP" bash -c "cd '$(dirname "$0")' && nohup python3 servers.py >/tmp/${NSP}-servers.log 2>&1 &"
  x "$ISP" bash -c "nohup iperf3 -s -p 5201 >/tmp/${NSP}-iperf3.log 2>&1 &"
  sleep 0.5
}

down() {
  need_root
  # kill whatever runs inside the lab namespaces (never pkill -f: that can match unrelated shells)
  for ns in "$RTR" "$MDM" "$ISP" $(for h in "${HOSTS[@]}"; do echo "${NSP}-h$h"; done); do
    ip netns pids "$ns" 2>/dev/null | xargs -r kill 2>/dev/null || true
  done
  for ns in "$RTR" "$MDM" "$ISP" $(for h in "${HOSTS[@]}"; do echo "${NSP}-h$h"; done); do
    ip netns del "$ns" 2>/dev/null || true
  done
}

status() {
  for pair in "$RTR r-wan" "$RTR br-lan" "$MDM m-r" "$MDM m-i"; do set -- $pair
    echo "== $1 $2"; x "$1" tc -s qdisc show dev "$2" | head -12
  done
}

case "${1:-}" in
  up) up ;; down) down ;; status) status ;;
  modem) shift; need_root; modem "$@" ;;
  shaper) shift; need_root; shaper "$@" ;;
  probe-modules) need_root; probe_modules ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
