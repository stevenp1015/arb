#!/usr/bin/env python3
"""Retrain rung3 exactly as pool_model.py did and DUMP the fired signals
(train-derived threshold, 10-min per-coin dedup) for the fill simulator."""
import warnings
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
warnings.filterwarnings("ignore")

M = pd.read_parquet("/root/fp_engine/pool.parquet").sort_values(
    ["day", "coin", "ts_ms"]).reset_index(drop=True)
days = sorted(M.day.unique())
META = {"ts_ms", "mid", "coin", "day"}
TGT = [c for c in M.columns if c[0] in "LS" and
       (c[1:4] == "mfe" or c[1:3] == "ok")] + ["lvl%d" % r for r in range(9)]
FEATS = sorted(c for c in M.columns if c not in META and c not in TGT)
X = M[FEATS].astype(float).values
R = 3


def outcome(side):
    win = M["%smfe%d" % (side, R)].values >= M["lvl%d" % R].values
    return win, (M["%sok%d" % (side, R)].values | win)


def dedup(mask, cd=600_000):
    keep = np.zeros(len(mask), bool)
    last = {}
    cv, dv, tv = M.coin.values, M.day.values, M.ts_ms.values
    for i in np.flatnonzero(mask):
        k = (cv[i], dv[i])
        if k not in last or tv[i] - last[k] >= cd:
            keep[i] = True
            last[k] = tv[i]
    return keep


def clf():
    return HistGradientBoostingClassifier(
        max_iter=200, learning_rate=0.07, max_leaf_nodes=31,
        l2_regularization=1.0, min_samples_leaf=200, random_state=7)


rows = []
for side in ("L", "S"):
    win, res = outcome(side)
    for tag, folds in (("fwd", [(days[:k], days[k:k + 4])
                                for k in range(10, len(days), 4)]),
                       ("rev", [(days[10:], days[:6])])):
        oof = np.full(len(M), np.nan)
        thr_row = np.full(len(M), np.nan)
        for tr_d, te_d in folds:
            if not te_d:
                continue
            tr = M.day.isin(tr_d).values & res
            te = M.day.isin(te_d).values
            c = clf().fit(X[tr], win[tr].astype(int))
            oof[te] = c.predict_proba(X[te])[:, 1]
            thr_row[te] = np.quantile(c.predict_proba(X[tr])[:, 1], 0.90)
        fired = dedup(np.isfinite(oof) & res & (oof >= thr_row))
        idx = np.flatnonzero(fired)
        for i in idx:
            rows.append(dict(coin=M.coin.values[i], day=M.day.values[i],
                             ts_ms=int(M.ts_ms.values[i]), side=side, fold=tag,
                             lvl=float(M["lvl%d" % R].values[i]),
                             mid_win=int(win[i])))
        print("%s %s: fired %d" % (side, tag, len(idx)), flush=True)

F = pd.DataFrame(rows)
F.to_csv("/root/fp_engine/fired_r3.csv", index=False)
print("DUMP-FIRED COMPLETE: %d signals, coins with most: %s"
      % (len(F), F.coin.value_counts().head(8).to_dict()))
