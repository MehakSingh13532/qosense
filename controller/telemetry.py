"""Telemetry: parse `tc -s -j qdisc show` (CAKE per-tin stats) and `conntrack -L`.

Parsers are pure functions over text/JSON so they are unit-tested without a kernel.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Dict, List, Optional

from .common import FlowCounters, FlowKey

_KV = re.compile(r"(\w+)=(\S+)")


def run(cmd: List[str], ns: Optional[str] = None, timeout: float = 2.0) -> str:
    if ns:
        cmd = ["ip", "netns", "exec", ns] + cmd
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True).stdout


def parse_cake_stats(js: str) -> Optional[dict]:
    """Return {'bandwidth_bps', 'tins': [..per-tin dicts..], 'drops', 'backlog'} or None."""
    try:
        data = json.loads(js)
    except json.JSONDecodeError:
        return None
    for q in data:
        if q.get("kind") != "cake":
            continue
        opts = q.get("options", {})
        tins = q.get("tins", [])
        names = ["bulk", "besteffort", "video", "voice"]
        out = []
        for i, t in enumerate(tins):
            out.append({
                "name": names[i] if len(tins) == 4 and i < 4 else f"tin{i}",
                "thresh_bps": t.get("threshold_rate"),
                "pkts": t.get("sent_packets", t.get("packets", 0)),
                "bytes": t.get("sent_bytes", t.get("bytes", 0)),
                "drops": t.get("drops", 0),
                "marks": t.get("ecn_mark", t.get("marks", 0)),
                "peak_delay_us": t.get("peak_delay_us", t.get("pk_delay_us", 0)),
                "avg_delay_us": t.get("avg_delay_us", t.get("av_delay_us", 0)),
                "base_delay_us": t.get("base_delay_us", t.get("sp_delay_us", 0)),
                "backlog": t.get("backlog_bytes", 0),
            })
        return {"bandwidth_bps": opts.get("bandwidth"), "tins": out,
                "drops": q.get("drops", 0), "backlog": q.get("backlog", 0)}
    return None


def cake_stats(dev: str, ns: Optional[str] = None) -> Optional[dict]:
    try:
        return parse_cake_stats(run(["tc", "-s", "-j", "qdisc", "show", "dev", dev], ns))
    except (subprocess.SubprocessError, OSError):
        return None


def _flow_from(fields: List[Dict[str, str]], proto: str) -> Optional[FlowKey]:
    orig = fields[0]
    try:
        return FlowKey(proto, orig["src"], orig["dst"],
                       int(orig.get("sport", 0)), int(orig.get("dport", 0)))
    except (KeyError, ValueError):
        return None


def parse_conntrack(text: str, lan_prefix: str = "10.0.0.") -> List[FlowCounters]:
    """Parse `conntrack -L -o extended` lines (needs nf_conntrack_acct=1).

    Example:
    ipv4 2 tcp 6 117 ESTABLISHED src=10.0.0.2 dst=198.51.100.10 sport=51234 dport=5201
      packets=120 bytes=170000 src=198.51.100.10 dst=192.0.2.2 sport=5201 dport=51234
      packets=60 bytes=3000 [ASSURED] mark=1 use=1
    """
    out: List[FlowCounters] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        proto = parts[2]
        if proto not in ("tcp", "udp"):
            continue
        # split into original / reply tuples at the second 'src='
        idx = [i for i, p in enumerate(parts) if p.startswith("src=")]
        if len(idx) < 2:
            continue
        orig_kv = dict(_KV.findall(" ".join(parts[idx[0]:idx[1]])))
        reply_kv = dict(_KV.findall(" ".join(parts[idx[1]:])))
        key = _flow_from([orig_kv], proto)
        if key is None or not key.src.startswith(lan_prefix):
            continue
        mark = int(reply_kv.get("mark", orig_kv.get("mark", 0)) or 0)
        out.append(FlowCounters(
            key=key,
            pkts_up=int(orig_kv.get("packets", 0)), bytes_up=int(orig_kv.get("bytes", 0)),
            pkts_down=int(reply_kv.get("packets", 0)), bytes_down=int(reply_kv.get("bytes", 0)),
            mark=mark))
    return out


def read_conntrack(ns: Optional[str] = None, lan_prefix: str = "10.0.0.") -> List[FlowCounters]:
    try:
        return parse_conntrack(run(["conntrack", "-L", "-o", "extended"], ns, timeout=3.0), lan_prefix)
    except (subprocess.SubprocessError, OSError):
        return []
