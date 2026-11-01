"""Shared constants and small types for the QoSense controller."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Traffic classes the classifier can emit.
BULK, WEB, VIDEO, VOICE = "bulk", "web", "video", "voice"
CLASSES = [BULK, WEB, VIDEO, VOICE]

# ct-mark value -> CAKE diffserv4 tin (1-based through tin_order; see docs/ARCHITECTURE.md).
# The order Bulk, Best Effort, Video, Voice is derived from kernel logic and is
# asserted in CI from per-tin packet counters (tests/ci/test_tin_mapping.sh).
CLASS_TO_MARK = {BULK: 1, WEB: 2, VIDEO: 3, VOICE: 4}
MARK_TO_CLASS = {v: k for k, v in CLASS_TO_MARK.items()}
MARK_MASK = 0x0F

# Per-class latency budgets (added latency under load, ms). Used by the policy
# engine and by the verifier; also the SLO targets in the report.
LATENCY_BUDGET_MS = {VOICE: 30.0, VIDEO: 80.0, WEB: 150.0, BULK: 1000.0}


@dataclass(frozen=True)
class FlowKey:
    proto: str  # "tcp" | "udp" | "icmp"
    src: str
    dst: str
    sport: int
    dport: int

    def short(self) -> str:
        return f"{self.proto}:{self.src}:{self.sport}>{self.dst}:{self.dport}"

    @staticmethod
    def parse(text: str) -> "FlowKey":
        """Inverse of short(); raises ValueError on anything malformed (IPv4 only)."""
        import ipaddress
        try:
            left, right = text.split(">")
            proto, src, sport = left.split(":")
            dst, dport = right.split(":")
            if proto not in ("tcp", "udp"):
                raise ValueError("proto")
            ipaddress.ip_address(src); ipaddress.ip_address(dst)
            return FlowKey(proto, src, dst, int(sport), int(dport))
        except (ValueError, TypeError) as e:
            raise ValueError(f"bad flow key {text!r}") from e


@dataclass
class FlowCounters:
    """Cumulative conntrack counters for one flow at one instant."""
    key: FlowKey
    pkts_up: int = 0      # original direction (LAN -> WAN)
    bytes_up: int = 0
    pkts_down: int = 0    # reply direction (WAN -> LAN)
    bytes_down: int = 0
    mark: int = 0


@dataclass
class Decision:
    """What the policy engine wants enforced on one tick."""
    marks: dict = field(default_factory=dict)       # FlowKey -> ct mark value
    up_kbit: Optional[int] = None
    down_kbit: Optional[int] = None
    reason: str = ""
