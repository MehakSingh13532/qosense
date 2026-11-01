"""Enforcer: turns a Decision into tc / conntrack commands.

Command builders are pure (unit-tested). The executor wraps them in a circuit
breaker with exponential backoff + jitter so a failing kernel interface cannot
cause a retry storm: after N consecutive failures the breaker opens and the
enforcer holds the last-known-good state until a half-open probe succeeds.
"""
from __future__ import annotations

import random
import subprocess
import time
from typing import Callable, Dict, List, Optional

from .common import MARK_MASK, Decision, FlowKey
from .verifier import Snapshot

CAKE_COMMON = "diffserv4 nat fwmark 0x0f"


def cake_args(direction: str, kbit: int, rtt: str = "regional") -> List[str]:
    """tc args after `qdisc <replace|change> dev X root`."""
    mode = "dual-srchost" if direction == "up" else "dual-dsthost"
    return ["cake", "bandwidth", f"{int(kbit)}kbit", "diffserv4", mode, "nat",
            "fwmark", "0x0f", rtt]


def tc_cmd(op: str, dev: str, direction: str, kbit: int, rtt: str = "regional") -> List[str]:
    return ["tc", "qdisc", op, "dev", dev, "root"] + cake_args(direction, kbit, rtt)


def conntrack_mark_cmd(key: FlowKey, mark: int) -> List[str]:
    cmd = ["conntrack", "-U", "-p", key.proto, "-s", key.src, "-d", key.dst]
    if key.proto in ("tcp", "udp"):
        cmd += ["--sport", str(key.sport), "--dport", str(key.dport)]
    cmd += ["-m", str(mark & MARK_MASK)]
    return cmd


def safe_state_cmds(wan: str, lan: str, up_kbit: int, down_kbit: int) -> List[List[str]]:
    """Static CAKE, no overrides: what the watchdog applies when the daemon dies."""
    return [["nft", "flush", "chain", "inet", "qosense", "qfwd"],     # drop all per-flow overrides
            tc_cmd("replace", wan, "up", up_kbit), tc_cmd("replace", lan, "down", down_kbit)]


MARK_RULE = ["nft", "add", "rule", "inet", "qosense", "qfwd", "meta", "mark", "set", "ct", "mark", "&", "0x0f"]
FLUSH_RULES = ["nft", "flush", "chain", "inet", "qosense", "qfwd"]


class CircuitBreaker:
    def __init__(self, threshold: int = 3, reset_s: float = 5.0, clock=time.monotonic):
        self.threshold, self.reset_s, self._clock = threshold, reset_s, clock
        self.failures = 0
        self.opened_at: Optional[float] = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half-open" if self._clock() - self.opened_at >= self.reset_s else "open"

    def allow(self) -> bool:
        return self.state in ("closed", "half-open")

    def ok(self) -> None:
        self.failures, self.opened_at = 0, None

    def fail(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self._clock()


def default_exec(cmd: List[str], ns: Optional[str] = None) -> None:
    if ns:
        cmd = ["ip", "netns", "exec", ns] + cmd
    subprocess.run(cmd, check=True, capture_output=True, timeout=2.0)


class Enforcer:
    def __init__(self, wan: str, lan: str, ns: Optional[str] = None,
                 executor: Callable = default_exec, sleep=time.sleep, rtt: str = "regional"):
        self.wan, self.lan, self.ns, self.rtt = wan, lan, ns, rtt
        self._exec, self._sleep = executor, sleep
        self.breaker = CircuitBreaker()
        self.up_kbit: Optional[int] = None
        self.down_kbit: Optional[int] = None
        self.marks: Dict[FlowKey, int] = {}
        self.last_error = ""

    def snapshot(self) -> Snapshot:
        return Snapshot(self.up_kbit or 0, self.down_kbit or 0, dict(self.marks))

    def _try(self, cmd: List[str], attempts: int = 3) -> bool:
        delay = 0.02
        for i in range(attempts):
            try:
                self._exec(cmd, self.ns)
                return True
            except Exception as e:  # noqa: BLE001 - any executor failure counts
                self.last_error = f"{' '.join(cmd[:4])}: {e}"
                if i < attempts - 1:
                    self._sleep(delay * (1 + random.random()))   # jittered backoff
                    delay *= 2
        return False

    def initial_setup(self, up_kbit: int, down_kbit: int) -> bool:
        # (re)install the ct-mark -> skb-mark rule; the watchdog may have flushed it
        self._try(FLUSH_RULES, 1); self._try(MARK_RULE, 1)
        ok = self._try(tc_cmd("replace", self.wan, "up", up_kbit, self.rtt)) and \
             self._try(tc_cmd("replace", self.lan, "down", down_kbit, self.rtt))
        if ok:
            self.up_kbit, self.down_kbit = up_kbit, down_kbit
        return ok

    def apply(self, d: Decision) -> bool:
        """Apply rates + marks. Returns False if the breaker is open or a step failed."""
        if not self.breaker.allow():
            return False
        ok = True
        if d.up_kbit and d.up_kbit != self.up_kbit:
            if self._try(tc_cmd("change", self.wan, "up", d.up_kbit, self.rtt), 2):
                self.up_kbit = d.up_kbit
            else:
                ok = False
        if d.down_kbit and d.down_kbit != self.down_kbit:
            if self._try(tc_cmd("change", self.lan, "down", d.down_kbit, self.rtt), 2):
                self.down_kbit = d.down_kbit
            else:
                ok = False
        for key, mark in d.marks.items():
            if self.marks.get(key) != mark:
                if self._try(conntrack_mark_cmd(key, mark), 1):
                    self.marks[key] = mark
                else:
                    ok = False
        for k in [k for k in self.marks if k not in d.marks]:
            self.marks.pop(k)
        (self.breaker.ok if ok else self.breaker.fail)()
        return ok

    def restore(self, snap: Snapshot) -> bool:
        d = Decision(marks=dict(snap.marks), up_kbit=snap.up_kbit or None,
                     down_kbit=snap.down_kbit or None, reason="rollback")
        self.marks = {}      # force re-write of all marks
        return self.apply(d)
