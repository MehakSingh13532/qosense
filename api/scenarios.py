"""Demo scenario runner for the public dashboard.

Every scenario is time-boxed and *auto-resets*, so a visitor cannot leave the lab in a bad state.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRAFFIC = os.path.join(ROOT, "lab", "traffic.py")
NETNS = os.path.join(ROOT, "lab", "netns.sh")

# name -> (description, max seconds)
SCENARIOS = {
    "iso_download": ("Big download + upload on another device (30 s)", 30),
    "drop_wan": ("WAN capacity drops to 20% (30 s, then restored)", 30),
    "tv_night": ("Two TVs streaming + a download (30 s)", 30),
}
MODES = ("qosense", "cake_static", "fifo")


class Scenarios:
    def __init__(self, nsp="qs", down_mbit=100, up_mbit=20, runner=subprocess.Popen, shell=subprocess.run):
        self.nsp, self.down, self.up = nsp, down_mbit, up_mbit
        self._popen, self._run = runner, shell
        self.active: Dict[str, float] = {}      # name -> ends_at
        self.mode = "qosense"
        self._lock = threading.Lock()

    def status(self) -> dict:
        now = time.time()
        with self._lock:
            self.active = {k: v for k, v in self.active.items() if v > now}
            return {"active": {k: round(v - now) for k, v in self.active.items()}, "mode": self.mode,
                    "available": {k: d for k, (d, _) in SCENARIOS.items()}}

    def _gen(self, host: str, *args: str):
        return self._popen(["ip", "netns", "exec", f"{self.nsp}-{host}", "python3", TRAFFIC, *args],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def start(self, name: str) -> dict:
        if name not in SCENARIOS:
            raise ValueError(f"unknown scenario; choose one of {sorted(SCENARIOS)}")
        with self._lock:
            if name in self.active and self.active[name] > time.time():
                return {"started": False, "reason": "already running"}
            if len(self.active) >= 2:
                return {"started": False, "reason": "too many scenarios running"}
            dur = SCENARIOS[name][1]
            self.active[name] = time.time() + dur
        if name == "iso_download":
            self._gen("h2", "bulk", "--duration", str(dur), "--reverse")
            self._gen("h2", "bulk", "--duration", str(dur), "--streams", "4")
        elif name == "tv_night":
            self._gen("h2", "video", "--duration", str(dur), "--seg-bytes", "5000000")
            self._gen("h3", "video", "--duration", str(dur), "--seg-bytes", "3000000")
            self._gen("h2", "bulk", "--duration", str(dur), "--reverse", "--streams", "4")
        elif name == "drop_wan":
            self._modem(max(2, self.down // 5), self.up)
            threading.Timer(dur, lambda: self._modem(self.down, self.up)).start()   # auto-reset
            self._gen("h2", "bulk", "--duration", str(dur), "--reverse")
        return {"started": True, "seconds": dur}

    def _modem(self, down, up):
        self._run(["bash", NETNS, "modem", str(down), str(up)], capture_output=True)

    def set_mode(self, controller, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode
        if mode == "qosense":
            self._run(["bash", NETNS, "shaper", "cake", str(self.down), str(self.up)], capture_output=True)
            controller.enf.initial_setup(self.up * 950, self.down * 950)
            controller.enabled = True
        else:
            controller.enabled = False
            self._run(["bash", NETNS, "shaper", "fifo" if mode == "fifo" else "cake", str(self.down), str(self.up)],
                      capture_output=True)
            controller.enf.up_kbit = controller.enf.down_kbit = None
