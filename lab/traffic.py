#!/usr/bin/env python3
"""Traffic generators run inside host namespaces. Every command prints one JSON line.

  voice  : G.711-like call, 50 pps x 172 B, UDP echo, reports RTT percentiles + loss
  game   : 60 Hz x 120 B isochronous UDP echo
  video  : fetches N-byte segments every period over HTTP, reports per-segment time
  bulk   : iperf3 -P 8 (upload, or download with --reverse), reports Mbit/s
  web    : many small HTTP fetches, reports page-load-like timings

Port choice is explicit so the experiments can run "the same call on a well-known
port and on an unusual port". Ground-truth labels for the classifier dataset come
from the command that generated the flow (see models/collect.py), not from ports.
"""
import argparse
import json
import socket
import statistics
import struct
import subprocess
import threading
import time
import urllib.request


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100.0 * len(xs)))]


def udp_stream(dst, port, pps, size, duration, v6=False):
    fam = socket.AF_INET6 if v6 else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_DGRAM)
    s.settimeout(0.2)
    s.connect((dst, port))
    sent = {}
    rtts = []
    trace = []                      # (seconds since start, rtt ms) for time-series plots
    start = time.perf_counter()
    stop = start + duration
    pad = b"\0" * max(0, size - 12)

    def rx():
        while time.perf_counter() < stop + 1.0:
            try:
                d = s.recv(2048)
            except (socket.timeout, OSError):
                continue
            if len(d) >= 12:
                seq, t0 = struct.unpack("!Id", d[:12])
                if seq in sent:
                    rtts.append((time.perf_counter() - t0) * 1000.0)
                    trace.append((round(t0 - start, 2), round(rtts[-1], 2)))
                    sent.pop(seq, None)

    th = threading.Thread(target=rx, daemon=True)
    th.start()
    interval, seq, nxt = 1.0 / pps, 0, time.perf_counter()
    n_sent = 0
    while time.perf_counter() < stop:
        t0 = time.perf_counter()
        sent[seq] = t0
        try:
            s.send(struct.pack("!Id", seq, t0) + pad)
        except OSError:
            pass
        seq += 1; n_sent += 1
        nxt += interval
        time.sleep(max(0.0, nxt - time.perf_counter()))
    th.join(timeout=2.0)
    return {"sent": n_sent, "recv": len(rtts), "loss_pct": 100.0 * (1 - len(rtts) / max(1, n_sent)),
            "rtt_ms": {"min": min(rtts) if rtts else None, "p50": pct(rtts, 50), "p95": pct(rtts, 95),
                       "p99": pct(rtts, 99), "max": max(rtts) if rtts else None,
                       "mean": statistics.fmean(rtts) if rtts else None},
            "trace": trace}


def fetch(url, timeout=30):
    t0 = time.perf_counter()
    n = 0
    with urllib.request.urlopen(url, timeout=timeout) as r:
        while True:
            b = r.read(65536)
            if not b:
                break
            n += len(b)
    return time.perf_counter() - t0, n


def video(dst, seg_bytes, period, duration):
    times, end = [], time.perf_counter() + duration
    nxt = time.perf_counter()
    while time.perf_counter() < end:
        try:
            dt, _ = fetch(f"http://{dst}:8000/bytes/{seg_bytes}")
            times.append(dt)
        except Exception:
            times.append(float("inf"))
        nxt += period
        time.sleep(max(0.0, nxt - time.perf_counter()))
    ok = [t for t in times if t != float("inf")]
    # a segment "stalls" if it takes longer than its own duration to download
    return {"segments": len(times), "stalls": sum(1 for t in times if t > period),
            "seg_time_s": {"p50": pct(ok, 50), "p95": pct(ok, 95), "max": max(ok) if ok else None}}


def web(dst, duration, size=40_000, gap=0.4):
    times, end = [], time.perf_counter() + duration
    while time.perf_counter() < end:
        try:
            dt, _ = fetch(f"http://{dst}:8000/bytes/{size}", timeout=10)
            times.append(dt * 1000)
        except Exception:
            times.append(10_000.0)
        time.sleep(gap)
    return {"requests": len(times), "ms": {"p50": pct(times, 50), "p95": pct(times, 95), "max": max(times)}}


def bulk(dst, duration, reverse, streams=8, port=None):
    # one iperf3 server per direction (a server handles a single test at a time)
    port = port or (5201 if reverse else 5202)
    cmd = ["iperf3", "-c", dst, "-p", str(port), "-t", str(duration), "-P", str(streams), "-J"]
    if reverse:
        cmd.append("-R")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=duration + 30)
    try:
        j = json.loads(out.stdout)
        bps = j["end"]["sum_received" if reverse else "sum_sent"]["bits_per_second"]
        return {"mbit": bps / 1e6, "streams": streams, "reverse": reverse}
    except Exception:
        return {"mbit": None, "error": (out.stderr or out.stdout)[:200]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["voice", "game", "video", "bulk", "web"])
    ap.add_argument("--dst", default="198.51.100.10")
    ap.add_argument("--port", type=int, default=3478)
    ap.add_argument("--duration", type=float, default=20)
    ap.add_argument("--reverse", action="store_true")
    ap.add_argument("--v6", action="store_true")
    ap.add_argument("--streams", type=int, default=8)
    ap.add_argument("--bulk-port", type=int, default=None, help="iperf3 server port (default 5201 down / 5202 up)")
    ap.add_argument("--seg-bytes", type=int, default=1_500_000)
    ap.add_argument("--period", type=float, default=2.0)
    a = ap.parse_args()
    dst = a.dst if not a.v6 or ":" in a.dst else "fd00:3::10"
    if a.kind == "voice":
        r = udp_stream(dst, a.port, 50, 172, a.duration, a.v6)
    elif a.kind == "game":
        r = udp_stream(dst, a.port, 60, 120, a.duration, a.v6)
    elif a.kind == "video":
        r = video(dst, a.seg_bytes, a.period, a.duration)
    elif a.kind == "web":
        r = web(dst, a.duration)
    else:
        r = bulk(dst, a.duration, a.reverse, a.streams, a.bulk_port)
    r["kind"], r["port"] = a.kind, a.port
    print(json.dumps(r))


if __name__ == "__main__":
    main()
