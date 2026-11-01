"""Latency probe: UDP echo RTT from the router to the ISP-side server, in the best-effort tin.

The socket is created inside the router namespace with os.setns (Python >= 3.12)
so the controller can live in the root namespace and still serve the public API.
"""
from __future__ import annotations

import os
import socket
import struct
import threading
import time
from collections import deque
from typing import Deque, Optional, Tuple

CLONE_NEWNET = 0x40000000


def socket_in_ns(ns: Optional[str], family=socket.AF_INET, kind=socket.SOCK_DGRAM) -> socket.socket:
    if not ns:
        return socket.socket(family, kind)
    orig = os.open("/proc/self/ns/net", os.O_RDONLY)
    target = os.open(f"/var/run/netns/{ns}", os.O_RDONLY)
    try:
        os.setns(target, CLONE_NEWNET)
        return socket.socket(family, kind)
    finally:
        os.setns(orig, CLONE_NEWNET)
        os.close(orig); os.close(target)


class RttProbe:
    def __init__(self, dst: str = "198.51.100.10", port: int = 7777, ns: Optional[str] = None,
                 hz: float = 20.0, baseline_window_s: float = 60.0):
        self.dst, self.port, self.ns, self.hz = dst, port, ns, hz
        self.baseline_window_s = baseline_window_s
        self._samples: Deque[Tuple[float, float]] = deque(maxlen=int(hz * baseline_window_s * 2))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.sent = 0
        self.lost = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        s = socket_in_ns(self.ns)
        s.settimeout(0.5)
        s.connect((self.dst, self.port))
        seq = 0
        nxt = time.monotonic()
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                s.send(struct.pack("!Id", seq, t0))
                self.sent += 1
                d = s.recv(2048)
                sq, tt = struct.unpack("!Id", d[:12])
                if sq == seq:
                    self._samples.append((time.monotonic(), (time.monotonic() - tt) * 1000.0))
            except (socket.timeout, OSError):
                self.lost += 1
            seq += 1
            nxt += 1.0 / self.hz
            time.sleep(max(0.0, nxt - time.monotonic()))

    # -- readouts ---------------------------------------------------------------
    def recent(self, now: float, span_s: float = 0.6) -> list:
        return [r for t, r in self._samples if now - span_s <= t <= now]

    def baseline_ms(self, now: float) -> Optional[float]:
        xs = [r for t, r in self._samples if now - self.baseline_window_s <= t <= now]
        return min(xs) if xs else None

    def delay_ms(self, now: float) -> Optional[float]:
        """Median of the last ~0.6 s minus the 60 s minimum (loaded - idle RTT)."""
        rec, base = self.recent(now), self.baseline_ms(now)
        if not rec or base is None:
            return None
        rec.sort()
        return max(0.0, rec[len(rec) // 2] - base)

    def latest_rtt(self, now: float) -> Optional[float]:
        rec = self.recent(now)
        return rec[-1] if rec else None
