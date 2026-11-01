#!/usr/bin/env python3
"""Render experiments/results/*.json into one self-contained report.html (inline SVG, no CDN)."""
import html
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
COLORS = {"fifo": "#b3261e", "cake_static": "#8a6d00", "qosense_rules": "#5b6b7a",
          "qosense_nocusum": "#1b6ec2", "qosense_model": "#0a7d4f"}
LABEL = {"fifo": "FIFO (no QoS)", "cake_static": "Static CAKE", "qosense_rules": "QoSense, rules only",
         "qosense_nocusum": "QoSense, no cliff detector", "qosense_model": "QoSense (AI classifier + cliff detector)"}


def load(name):
    p = os.path.join(RES, f"{name}.json")
    return json.load(open(p)) if os.path.exists(p) else None


def f(x, d=1):
    return "–" if x is None else f"{x:.{d}f}"


def line_chart(series, w=720, h=240, ymax=None, title="", xlabel="seconds", ylabel="call RTT p95 (ms)", vline=None):
    pts = [p for s in series.values() for p in s]
    if not pts:
        return ""
    xmax = max(p[0] for p in pts) or 1
    ymax = ymax or max(p[1] for p in pts if p[1] is not None) * 1.1 or 1
    L, R, T, B = 52, 12, 22, 34
    sx = lambda x: L + (w - L - R) * x / xmax
    sy = lambda y: h - B - (h - T - B) * min(y, ymax) / ymax
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{html.escape(title)}">']
    for i in range(5):
        y = ymax * i / 4
        out.append(f'<line x1="{L}" x2="{w-R}" y1="{sy(y):.1f}" y2="{sy(y):.1f}" class="grid"/>'
                   f'<text x="{L-6}" y="{sy(y)+4:.1f}" class="tick" text-anchor="end">{y:.0f}</text>')
    for i in range(0, int(xmax) + 1, max(1, int(xmax // 8) or 1)):
        out.append(f'<text x="{sx(i):.1f}" y="{h-B+16}" class="tick" text-anchor="middle">{i}</text>')
    if vline is not None:
        out.append(f'<line x1="{sx(vline):.1f}" x2="{sx(vline):.1f}" y1="{T}" y2="{h-B}" class="vline"/>'
                   f'<text x="{sx(vline)+4:.1f}" y="{T+10}" class="tick">capacity cliff</text>')
    for name, s in series.items():
        d = " ".join(f"{'M' if i == 0 else 'L'}{sx(x):.1f},{sy(y):.1f}" for i, (x, y) in enumerate(s) if y is not None)
        out.append(f'<path d="{d}" fill="none" stroke="{COLORS.get(name, "#333")}" stroke-width="2"/>')
    out.append(f'<text x="{L}" y="12" class="tick">{html.escape(ylabel)}</text>'
               f'<text x="{w-R}" y="{h-4}" class="tick" text-anchor="end">{html.escape(xlabel)}</text></svg>')
    return "".join(out)


def legend(names):
    return '<p class="legend">' + " ".join(
        f'<span><i style="background:{COLORS.get(n, "#333")}"></i>{html.escape(LABEL.get(n, n))}</span>' for n in names) + "</p>"


def table(head, rows):
    h = "".join(f"<th>{html.escape(x)}</th>" for x in head)
    b = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<div class=scroll><table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table></div>"


def call_table(sc):
    rows = []
    for cfg, e in sc["configs"].items():
        k, u = e["known_port"], e["unusual_port"]
        ctl = u.get("controller") or {}
        rng = "–" if not ctl.get("down_kbit_min") else f"{ctl['down_kbit_min'] / 1000:.0f}–{ctl['down_kbit_max'] / 1000:.0f}"
        rows.append([LABEL.get(cfg, cfg), f(k["added_p95_ms"]), f(k.get("steady_added_p95_ms")), f(u["added_p95_ms"]),
                     f(u.get("steady_added_p95_ms")), f(u["loaded"]["p99"]), f(u["loaded"]["loss_pct"]),
                     f(u["bulk_down_mbit"], 0), f(u["bulk_up_mbit"], 0), rng])
    return table(["Configuration", "Added p95 known port (ms)", "…steady", "Added p95 unusual port (ms)", "…steady",
                  "p99 RTT unusual (ms)", "Loss % (unusual)", "Bulk ↓ Mbit/s", "Bulk ↑ Mbit/s", "Shaper ↓ range (Mbit/s)"], rows)


def build():
    s1, s2, s3, s4, s6, s7 = (load(n) for n in ("s1", "s2", "s3", "s4", "s6", "s7"))
    parts = []
    meta = next((x["meta"] for x in (s1, s2, s3, s4, s6, s7) if x and "meta" in x), {})
    if s1:
        parts.append("<h2>S1 · ISO download on another device during a video call</h2><p>Call = 50 pps × 172 B UDP echo. "
                     "<b>Added p95 latency</b> = loaded p95 RTT minus idle median RTT; <b>steady</b> excludes the first "
                     "7 s (classification warm-up). The download runs on a different device from the call, and CAKE "
                     "already isolates devices from each other, so this is the easy case: a fixed-rate static CAKE "
                     "does very well here. The adaptive controller trades a little latency for ~5% more throughput "
                     "(it probes the real capacity instead of trusting a configured number).</p>" + call_table(s1))
    if s7:
        parts.append("<h2>S7 · ISO download on the <i>same</i> device, on a port no rule knows</h2><p>The download and "
                     "the call share one device, and the download uses ports the rule baseline cannot recognise, so the "
                     "rules leave it in best-effort next to the call. A classifier that recognises behaviour can put "
                     "the download in the bulk tin and the call in the voice tin.</p>" + call_table(s7))
    if s2:
        ser = {c: [(b, v) for b, v in e["binned_p95"] if v is not None] for c, e in s2["configs"].items()}
        rows = [[LABEL.get(c, c), f(e["baseline_p95_ms"]), f(e["peak_ms"], 0),
                 (f(e["recovery_s"], 0) + " s") if e["recovered"] else "did not recover",
                 f(e["call_p95_after_ms"], 0)] for c, e in s2["configs"].items()]
        t_cliff = next(iter(s2["configs"].values()))["t_cliff"]
        parts.append(f"<h2>S2 · Capacity cliff ({s2['down_mbit']} → {s2['cliff_down_mbit']} Mbit/s)</h2>" +
                     legend(ser) + line_chart(ser, vline=t_cliff, title="Call RTT during capacity cliff") +
                     table(["Configuration", "Baseline p95 (ms)", "Peak RTT (ms)", "Time to recover", "p95 after cliff (ms)"], rows))
    if s3:
        rows = [[LABEL.get(c, c), f(e["game"]["p95"]), f(e["game"]["p99"]), str(e["tv_stalls"]),
                 ", ".join(f(x, 2) for x in e["tv_seg_p95_s"])] for c, e in s3["configs"].items()]
        parts.append("<h2>S3 · Three TVs and a gamer</h2>" +
                     table(["Configuration", "Game RTT p95 (ms)", "Game RTT p99 (ms)", "TV stalls (TV2, TV3)",
                            "TV segment time p95 (s)"], rows))
    if s4:
        rows = [[n.replace("_", " "), f(v["p50"]), f(v["p95"]), f(v["p99"])] for n, v in s4["phases"].items()]
        parts.append("<h2>S4 · Temporary intent</h2><p>An odd-port call starts out best-effort. "
                     "A user intent promotes it, then expires by itself.</p>" +
                     table(["Phase", "p50 (ms)", "p95 (ms)", "p99 (ms)"], rows))
    if s6:
        parts.append("<h2>S6 · Controller crash</h2><p>Controller killed with SIGKILL while shaping. "
                     f"Watchdog applied safe state: <b>{s6.get('safe_state_applied')}</b>"
                     f" in <b>{f(s6.get('seconds_to_safe'), 2)} s</b>.</p>")
    if not parts:
        parts.append("<p>No results yet. Run <code>make experiments</code>.</p>")
    css = """:root{--bg:#fff;--fg:#1b1f23;--mut:#5b6670;--line:#d8dee4;--acc:#0a7d4f}
@media(prefers-color-scheme:dark){:root{--bg:#111418;--fg:#e6e9ec;--mut:#9aa5b0;--line:#2c333a;--acc:#4cc38a}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px}h1{font-size:1.6rem}h2{margin-top:2rem;font-size:1.2rem}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;white-space:nowrap}
svg{width:100%;height:auto}.grid{stroke:var(--line)}.tick{fill:var(--mut);font-size:11px}.vline{stroke:var(--mut);stroke-dasharray:4}
.legend span{margin-right:14px;font-size:.85rem}.legend i{display:inline-block;width:10px;height:10px;margin-right:5px}
footer{color:var(--mut);font-size:.85rem;margin-top:2rem}"""
    foot = (f"Kernel {html.escape(str(meta.get('kernel', '?')))} · lab {meta.get('down_mbit', '?')}/{meta.get('up_mbit', '?')} Mbit/s "
            f"· generated {html.escape(str(meta.get('time', '?')))} · {'quick run' if meta.get('quick') else 'full run'}. "
            "Network-namespace emulation, not a real access network; see docs/LIMITATIONS.md.")
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>QoSense experiment report</title><style>{css}</style></head><body><main>"
            f"<h1>QoSense · experiment report</h1><p>Intent-aware adaptive QoS for home broadband: "
            f"measured with an automated Linux-namespace lab.</p>{''.join(parts)}<footer>{foot}</footer></main></body></html>")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "report.html")
    open(out, "w").write(build())
    print("wrote", out)
