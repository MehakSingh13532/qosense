"""Checks the training pipeline end to end on a *synthetic* CSV (never used as the shipped model)."""
import json
import subprocess
import sys

import numpy as np
import pandas as pd

from controller.features import FEATURES


def synth(n_runs=9, per=40, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for run in range(1, n_runs + 1):
        for label, dport, g in (
            ("voice", int(rng.choice([3478, 51234, 44444])), lambda: [100, 172, .5, .03, .02, 8, 1, 3, 17000]),
            ("bulk", 5201, lambda: [900, 1300, .1, .2, .1, 8, 0, 2, 1_200_000]),
            ("video", 8000, lambda: [250, 1200, .02, 1.2, .2, 8, 0, 2, 300_000]),
            ("web", 8000, lambda: [20, 700, .1, 1.5, .8, 8, 0, 2, 14_000]),
        ):
            for _ in range(per):
                v = np.array(g()) * rng.normal(1, 0.12, len(FEATURES))
                rows.append([run, label, dport] + list(np.abs(v)))
    return pd.DataFrame(rows, columns=["run", "label", "dport"] + FEATURES)


def test_train_pipeline_and_model_beats_rules_on_unusual_ports(tmp_path):
    csv = tmp_path / "d.csv"
    synth().to_csv(csv, index=False)
    out = tmp_path / "m.txt"
    subprocess.run([sys.executable, "models/train.py", "--data", str(csv), "--out", str(out),
                    "--card", str(tmp_path / "card.md")], check=True, capture_output=True)
    m = json.load(open(tmp_path / "metrics.json"))
    assert m["macro_f1"]["lightgbm"] > m["macro_f1"]["rules"]
    assert out.exists() and "Model card" in (tmp_path / "card.md").read_text()

    from controller.classifier import ModelClassifier
    from controller.common import FlowKey
    mc = ModelClassifier(str(out))
    assert mc.loaded
    f = dict(zip(FEATURES, [100, 172, .5, .03, .02, 8, 1, 3, 17000]))
    cls, conf = mc.classify(FlowKey("udp", "10.0.0.2", "198.51.100.10", 1, 51234), f)   # odd port
    assert cls == "voice" and conf > 0.55
