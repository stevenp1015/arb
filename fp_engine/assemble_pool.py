#!/usr/bin/env python3
"""
fp_engine/assemble_pool.py — pool all 803 coin-day matrices into one table.

Normalizations (the lesson from aster_v2.py, applied fleet-wide):
- feature windows renamed ms -> rung index (ret_1617 -> ret_w0 ...) since each
  coin-day derives its own ladder; index k means "k geometric steps above this
  tape's own resolution floor" — market-relative by construction.
- label rungs likewise -> Lmfe0..8 / Lok0..8 / lvl0..8 (lowest 9 rungs).
- coin identity is NOT a feature: the model must generalize across markets.
  day + coin kept as metadata columns only.
"""
import glob, os, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")

NR = 9
FAMS = ("ret_", "aimb_", "trate_", "maxprint_", "brate_")
OUT = "/root/fp_engine/pool.parquet"

frames, skipped = [], []
files = sorted(glob.glob("/root/fp_engine/out/sweep/live_*/[A-Z]*_matrix.csv"))
for f in files:
    coin = os.path.basename(f).replace("_matrix.csv", "")
    day = f.split("/")[-2][5:13]
    d = pd.read_csv(f)
    lv = sorted({float(c.split("@")[1]) for c in d.columns if c.startswith("L_X@")})
    ws = sorted({int(c.rsplit("_", 1)[1]) for c in d.columns
                 if c.startswith("ret_") and c.rsplit("_", 1)[1].isdigit()})
    if len(lv) < NR or len(ws) < 10:
        skipped.append("%s/%s(rungs=%d,wins=%d)" % (day, coin, len(lv), len(ws)))
        continue
    new = {}
    for r, L in enumerate(lv[:NR]):
        for s in ("L", "S"):
            new["%smfe%d" % (s, r)] = d["%s_X@%s" % (s, L)].values * 100.0
            new["%sok%d" % (s, r)] = d["%s_ok@%s" % (s, L)].values.astype(bool)
        new["lvl%d" % r] = L
    ren = {}
    for fam in FAMS:
        fws = sorted({int(c.rsplit("_", 1)[1]) for c in d.columns
                      if c.startswith(fam) and c.rsplit("_", 1)[1].isdigit()})
        for k, w in enumerate(fws[:10]):
            ren["%s%d" % (fam, w)] = "%sw%d" % (fam, k)
    d = d.rename(columns=ren)
    keep = [c for c in d.columns if "@" not in c and
            not (c.rsplit("_", 1)[-1].isdigit() and any(c.startswith(x) for x in FAMS))]
    base = d[keep].copy()
    for k, v in new.items():
        base[k] = v
    base["w0_secs"] = ws[0] / 1000.0          # ladder anchors as context features
    base["w9_secs"] = ws[9] / 1000.0
    base["coin"] = coin
    base["day"] = day
    frames.append(base)

M = pd.concat(frames, ignore_index=True)
M.to_parquet(OUT)
print("POOLED %d coin-days -> %s: %d rows, %d cols  (skipped %d: %s)"
      % (len(frames), OUT, len(M), M.shape[1], len(skipped), ",".join(skipped[:6])))
lvl_mean = {r: M["lvl%d" % r].mean() for r in range(NR)}
print("mean rung values (%): " + "  ".join("r%d=%.3f" % (r, v) for r, v in lvl_mean.items()))
