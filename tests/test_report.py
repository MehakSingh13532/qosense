"""Renders the report from a *synthetic* fixture to make sure report.py works. Never published."""
import importlib
import json

from experiments import report


def test_report_renders_from_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "RES", str(tmp_path))
    meta = {"down_mbit": 100, "up_mbit": 20, "quick": True, "kernel": "test", "time": "t"}
    cfgs = {"fifo": 0, "qosense_model": 1}
    s1 = {"scenario": "s1", "meta": meta, "configs": {
        c: {p: {"idle": {}, "loaded": {"p95": 30, "p99": 40, "loss_pct": 0}, "added_p95_ms": 100 - 90 * i,
                "bulk_down_mbit": 90, "bulk_up_mbit": 18} for p in ("known_port", "unusual_port")}
        for c, i in cfgs.items()}}
    s2 = {"scenario": "s2", "meta": meta, "down_mbit": 100, "cliff_down_mbit": 20, "configs": {
        c: {"baseline_p95_ms": 20, "peak_ms": 400, "recovery_s": None if i == 0 else 3, "recovered": bool(i),
            "call_p95_after_ms": 50, "t_cliff": 10, "binned_p95": [[t, 20 + (200 if t > 10 and not i else 0)] for t in range(30)]}
        for c, i in cfgs.items()}}
    (tmp_path / "s1.json").write_text(json.dumps(s1))
    (tmp_path / "s2.json").write_text(json.dumps(s2))
    out = report.build()
    assert "S1" in out and "S2" in out and "<svg" in out and "did not recover" in out
