#!/usr/bin/env python3
"""
fp_engine/pool_model.py — Round-1 election over the pooled June table.

- Walk-forward, expanding by day: early days teach, later days judge. No row
  from a judged day is ever in its own training set.
- REVERSED fold too (train on the late-June chop/grind, judge on the Jun 4-9
  crash) because June's phases differ violently; a rule must be seen in both
  directions before anyone gets excited.
- Per-phase reporting (Steven's labels): A crash Jun4-9, B chop Jun10-19,
  C down Jun20-30.
- Firing evaluation uses a 10-min per-coin cooldown (episode dedup) and is
  compared against the same fold's base rate (the empirical random-entry null,
  which absorbs drift). Fees: zero (Lighter). Symmetric geometry, so the luck
  baseline is the base rate itself.
- Thresholds for "top decile" are taken from the TRAINING fold's score
  distribution, never from the judged fold (fixes the mild look-ahead in
  aster_dedup_model.py).
"""
import warnings
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score
warnings.filterwarnings("ignore")

M = pd.read_parquet("/root/fp_engine/pool.parquet").sort_values(
    ["day", "coin", "ts_ms"]).reset_index(drop=True)
days = sorted(M.day.unique())
phase = np.where(M.day <= "20260609", "A-crash",
                 np.where(M.day <= "20260619", "B-chop", "C-down"))
META = {"ts_ms", "mid", "coin", "day"}
TGT = [c for c in M.columns if c[0] in "LS" and
       (c[1:4] == "mfe" or c[1:3] == "ok")] + ["lvl%d" % r for r in range(9)]
FEATS = sorted(c for c in M.columns if c not in META and c not in TGT)
print("rows=%d  days=%d  coins=%d  features=%d" %
      (len(M), len(days), M.coin.nunique(), len(FEATS)))
X = M[FEATS].astype(float).values


def outcome(side, r):
    win = M["%smfe%d" % (side, r)].values >= M["lvl%d" % r].values
    res = M["%sok%d" % (side, r)].values | win
    return win, res


def dedup(mask, cooldown_ms=600_000):
    keep = np.zeros(len(mask), bool)
    last = {}
    coin_day = M.coin.values.astype(object)
    dayv = M.day.values
    tsv = M.ts_ms.values
    for i in np.flatnonzero(mask):
        k = (coin_day[i], dayv[i])
        if k not in last or tsv[i] - last[k] >= cooldown_ms:
            keep[i] = True
            last[k] = tsv[i]
    return keep


def clf():
    return HistGradientBoostingClassifier(
        max_iter=200, learning_rate=0.07, max_leaf_nodes=31,
        l2_regularization=1.0, min_samples_leaf=200, random_state=7)


def report(tag, te_mask, win, res, oof, thr):
    m = te_mask & res & np.isfinite(oof)
    if m.sum() < 500:
        return
    auc = roc_auc_score(win[m], oof[m])
    base = win[m].mean()
    fired = dedup(m & (oof >= thr))
    n = fired.sum()
    p = win[fired].mean() if n else np.nan
    print("  %-28s n=%7d AUC=%.4f base=%5.1f%% | fired(dedup) n=%5d win=%5.1f%% "
          "edge=%+5.1fpp" % (tag, m.sum(), auc, base * 100, n, p * 100,
                             (p - base) * 100))
    for ph in ("A-crash", "B-chop", "C-down"):
        fm = fired & (phase == ph)
        if fm.sum() >= 100:
            bm = m & (phase == ph)
            print("      %-8s fired n=%5d win=%5.1f%% (phase base %5.1f%%)"
                  % (ph, fm.sum(), win[fm].mean() * 100, win[bm].mean() * 100))


for R in (3, 4):
    print("\n===== symmetric TP=SL=rung%d (mean %.3f%%) — luck baseline = base rate ====="
          % (R, M["lvl%d" % R].mean()))
    for side in ("L", "S"):
        win, res = outcome(side, R)
        # forward walk: expanding train, 4-day judge blocks
        oof = np.full(len(M), np.nan)
        thr_per_row = np.full(len(M), np.nan)
        for k in range(10, len(days), 4):
            tr_d, te_d = days[:k], days[k:k + 4]
            tr = M.day.isin(tr_d).values & res
            te = M.day.isin(te_d).values
            if not te.any():
                break
            c = clf().fit(X[tr], win[tr].astype(int))
            tr_scores = c.predict_proba(X[tr])[:, 1]
            oof[te] = c.predict_proba(X[te])[:, 1]
            thr_per_row[te] = np.quantile(tr_scores, 0.90)
        te_all = np.isfinite(oof)
        thr = np.nanmedian(thr_per_row)
        report("%s fwd (judge Jun14-30)" % ("LONG" if side == "L" else "SHORT"),
               te_all, win, res, oof, thr)
        # reversed: train late June, judge the crash
        tr = M.day.isin(days[10:]).values & res
        te = M.day.isin(days[:6]).values
        c = clf().fit(X[tr], win[tr].astype(int))
        oof_r = np.full(len(M), np.nan)
        oof_r[te] = c.predict_proba(X[te])[:, 1]
        thr_r = np.quantile(c.predict_proba(X[tr])[:, 1], 0.90)
        report("%s rev (judge Jun4-9 crash)" % ("LONG" if side == "L" else "SHORT"),
               te, win, res, oof_r, thr_r)

# which measurements carried the election (one representative target)
win, res = outcome("L", 3)
tr = M.day.isin(days[:14]).values & res
c = clf().fit(X[tr], win[tr].astype(int))
from sklearn.inspection import permutation_importance
te = M.day.isin(days[14:18]).values & res
sub = np.flatnonzero(te)
sub = sub[np.random.default_rng(1).choice(len(sub), min(60000, len(sub)), replace=False)]
imp = permutation_importance(c, X[sub], win[sub].astype(int), n_repeats=3,
                             random_state=1, scoring="roc_auc")
order = np.argsort(-imp.importances_mean)[:15]
print("\ntop-15 features by permutation importance (LONG rung3, judged on held-out days):")
for i in order:
    print("  %-16s %+.4f" % (FEATS[i], imp.importances_mean[i]))
print("\nPOOL-MODEL COMPLETE")
