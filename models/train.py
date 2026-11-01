#!/usr/bin/env python3
"""Train the LightGBM flow classifier (behavioural features only) and compare with the rule baseline.

Split is by *run*, never by row, so near-duplicate consecutive windows of one flow can't leak
from train to test. Writes model.txt, metrics.json and MODEL_CARD.md.
"""
import argparse
import json
import os
import sys

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import classification_report, f1_score

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from controller.classifier import RuleClassifier, VOICE_PORTS            # noqa: E402
from controller.common import CLASSES, FlowKey                           # noqa: E402
from controller.features import FEATURES                                 # noqa: E402


def split_by_run(df, test_frac=0.34, seed=3):
    runs = sorted(df["run"].unique())
    rng = np.random.default_rng(seed)
    rng.shuffle(runs)
    n_test = max(1, int(round(len(runs) * test_frac)))
    test_runs = set(runs[:n_test])
    return df[~df["run"].isin(test_runs)], df[df["run"].isin(test_runs)]


def rule_predict(df):
    r = RuleClassifier()
    out = []
    for p, proto in zip(df["dport"], np.where(df["is_udp"] > 0.5, "udp", "tcp")):
        out.append(r.classify(FlowKey(proto, "10.0.0.2", "198.51.100.10", 1, int(p)))[0])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="models/model.txt")
    ap.add_argument("--card", default="models/MODEL_CARD.md")
    ap.add_argument("--metrics", default=None)
    a = ap.parse_args()
    df = pd.read_csv(a.data)
    df["y"] = df["label"].map({c: i for i, c in enumerate(CLASSES)})
    tr, te = split_by_run(df)
    params = dict(objective="multiclass", num_class=len(CLASSES), learning_rate=0.08, num_leaves=15,
                  min_data_in_leaf=10, feature_fraction=0.9, verbose=-1, seed=3, num_threads=2)
    # bulk has many parallel streams, so it dominates the rows: weight classes inversely to frequency
    counts = tr["y"].value_counts()
    weights = tr["y"].map(lambda c: len(tr) / (len(counts) * counts[c]))
    booster = lgb.train(params, lgb.Dataset(tr[FEATURES], tr["y"], weight=weights), num_boost_round=150)
    booster.save_model(a.out)

    proba = booster.predict(te[FEATURES])
    pred_ml = [CLASSES[i] for i in proba.argmax(1)]
    pred_rule = rule_predict(te)
    truth = list(te["label"])
    odd = ~te["dport"].isin(VOICE_PORTS)         # flows on ports no rule knows (incl. unusual-port calls)

    def macro(y, p):
        return float(f1_score(y, p, labels=CLASSES, average="macro", zero_division=0))

    def voice_recall(y, p, mask):
        idx = [i for i, (t, m) in enumerate(zip(y, mask)) if t == "voice" and m]
        return float(np.mean([p[i] == "voice" for i in idx])) if idx else None

    metrics = {
        "rows_train": int(len(tr)), "rows_test": int(len(te)), "runs": int(df["run"].nunique()),
        "macro_f1": {"lightgbm": macro(truth, pred_ml), "rules": macro(truth, pred_rule)},
        "voice_recall_unusual_port": {"lightgbm": voice_recall(truth, pred_ml, odd.values),
                                      "rules": voice_recall(truth, pred_rule, odd.values)},
        "voice_recall_all": {"lightgbm": voice_recall(truth, pred_ml, [True] * len(te)),
                             "rules": voice_recall(truth, pred_rule, [True] * len(te))},
        "feature_importance": dict(sorted(zip(FEATURES, map(float, booster.feature_importance("gain"))),
                                          key=lambda kv: -kv[1])),
        "report_lightgbm": classification_report(truth, pred_ml, labels=CLASSES, output_dict=True, zero_division=0),
    }
    mp = a.metrics or os.path.join(os.path.dirname(a.out) or ".", "metrics.json")
    json.dump(metrics, open(mp, "w"), indent=1)
    imp = "\n".join(f"- `{k}`: {v:.0f}" for k, v in list(metrics["feature_importance"].items())[:5])
    card = f"""# Model card: QoSense flow classifier

**Task.** Classify a LAN flow into `bulk`, `web`, `video` or `voice` (calls and games) every 500 ms.
**Model.** LightGBM multiclass, 150 trees, {len(FEATURES)} features.
**Features (no ports, no payload, no IPs).** {', '.join(f'`{f}`' for f in FEATURES)}.
**Data.** {metrics['rows_train'] + metrics['rows_test']} windows from {metrics['runs']} runs of the
automated lab (`models/collect.py`). Labels come from the generator that started the flow. Server
ports are randomised per run. Train/test are split **by run**.

| | LightGBM | Port rules |
|---|---|---|
| Macro-F1 (held-out runs) | {metrics['macro_f1']['lightgbm']:.3f} | {metrics['macro_f1']['rules']:.3f} |
| Voice recall, all ports | {metrics['voice_recall_all']['lightgbm']} | {metrics['voice_recall_all']['rules']} |
| Voice recall, ports no rule knows | {metrics['voice_recall_unusual_port']['lightgbm']} | {metrics['voice_recall_unusual_port']['rules']} |

Top features by gain:
{imp}

**Intended use.** Choosing a CAKE tin for a flow; always behind hysteresis, user pins and a
rule fallback when confidence is below 0.55.
**Limitations.** Trained only on synthetic lab generators (UDP echo calls, HTTP segments, iperf3).
Real apps (QUIC video, WebRTC with FEC/simulcast, encrypted games) differ; expect lower accuracy
until retrained on real captures such as CESNET-QUIC22. Not evaluated for fairness or adversarial
flows (a bulk flow imitating call-like packets would be promoted; the policy's bulk floor limits damage).
"""
    open(a.card, "w").write(card)
    print(json.dumps({k: metrics[k] for k in ("macro_f1", "voice_recall_unusual_port", "rows_test")}, indent=1))


if __name__ == "__main__":
    main()
