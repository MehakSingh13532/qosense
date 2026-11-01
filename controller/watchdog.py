"""Watchdog: if the controller stops heart-beating, drop to a safe static CAKE state.

Runs as its own process (systemd unit qosense-watchdog). The controller touches
HEARTBEAT every tick; if it is older than TIMEOUT the watchdog runs safe-state.
Safe state = static CAKE at conservative rates and no per-flow overrides.
"""
from __future__ import annotations

import argparse
import os
import time

from .enforcer import default_exec, safe_state_cmds


def heartbeat_age(path: str, now: float | None = None) -> float:
    try:
        return (now if now is not None else time.time()) - os.stat(path).st_mtime
    except FileNotFoundError:
        return float("inf")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--heartbeat", default="/run/qosense/heartbeat")
    ap.add_argument("--timeout", type=float, default=1.5)
    ap.add_argument("--wan", default="r-wan")
    ap.add_argument("--lan", default="br-lan")
    ap.add_argument("--ns", default=None)
    ap.add_argument("--up-kbit", type=int, default=18_000)
    ap.add_argument("--down-kbit", type=int, default=90_000)
    a = ap.parse_args()
    tripped = False
    while True:
        age = heartbeat_age(a.heartbeat)
        if age > a.timeout and not tripped:
            for cmd in safe_state_cmds(a.wan, a.lan, a.up_kbit, a.down_kbit):
                try:
                    default_exec(cmd, a.ns)
                except Exception as e:  # noqa: BLE001
                    print("safe-state failed:", e, flush=True)
            print(f"safe state applied (heartbeat age {age:.1f}s)", flush=True)
            tripped = True
        elif age <= a.timeout:
            tripped = False
        time.sleep(0.25)


if __name__ == "__main__":
    main()
