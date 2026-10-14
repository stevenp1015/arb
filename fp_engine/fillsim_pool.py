#!/usr/bin/env python3
"""
fp_engine/fillsim_pool.py — the fill-realism gate on the pooled model's fired
signals, per-row geometry, two fee worlds (Lighter 0 / Aster 0.04% on stops).
Mechanics identical to the audited aster_fillsim.py:
  entry: resting order joining best bid (long) / best ask (short)
  fill models: front (front of queue), queue (behind displayed size), sweep
    (level must trade strictly through)
  exits: TP resting at +lvl from entry price (fills when the touch reaches it —
    known-optimistic, so a FAIL here is extra trustworthy, a PASS is provisional);
    SL crossed as taker at -lvl (+fee); 1h cap marked at last quote.
"""
import os
import numpy as np, pandas as pd

TAPE = "/root/fp_engine/tape5"
F = pd.read_csv("/root/fp_engine/fired_r3.csv")
COINS = sorted(set(os.listdir(TAPE) and
                   {f.split("_")[0] for d in os.listdir(TAPE)
                    for f in os.listdir(os.path.join(TAPE, d))}))
F = F[F.coin.isin(COINS)].reset_index(drop=True)
print("signals with tape: %d across %s" % (len(F), sorted(F.coin.unique())))
MAXHOLD = 3600_000


def load(day, coin):
    p = os.path.join(TAPE, day)
    try:
        b = pd.read_json(os.path.join(p, "%s_bbo.jsonl" % coin), lines=True)
        t = pd.read_json(os.path.join(p, "%s_trades.jsonl" % coin), lines=True)
    except (ValueError, FileNotFoundError):
        return None
    b = b[(b.bpx > 0) & (b.apx > b.bpx)].sort_values("t")
    t = t.sort_values("t")
    return (b.t.values.astype(np.int64), b.bpx.values, b.bsz.values,
            b.apx.values, b.asz.values, t.t.values.astype(np.int64),
            t.px.values, t.sz.values, (t.side.values == "A"))


def run(model, life_s, taker):
    out = []
    for (day, coin), g in F.groupby(["day", "coin"]):
        day = str(day)
        tape = load(day, coin)
        if tape is None:
            continue
        bt, bpx, bsz, apx, asz, tt, tpx, tsz, tsell = tape
        for _, r in g.iterrows():
            t0 = int(r.ts_ms)
            lng = r.side == "L"
            G = r.lvl / 100.0
            k = int(np.searchsorted(bt, t0, "right") - 1)
            if k < 0 or k >= len(bt) - 10:
                continue
            B, Q, A, QA = bpx[k], bsz[k], apx[k], asz[k]
            P = B if lng else A
            hi = int(np.searchsorted(bt, t0 + life_s * 1000, "right"))
            fill_t = None
            swept = (bpx[k:hi] < B - 1e-12) if lng else (apx[k:hi] > A + 1e-12)
            if model == "sweep":
                if swept.any():
                    fill_t = int(bt[k + int(np.argmax(swept))])
            else:
                i0 = int(np.searchsorted(tt, t0, "right"))
                need = 0.0 if model == "front" else (Q if lng else QA)
                acc = 0.0
                for j in range(i0, len(tt)):
                    if tt[j] > t0 + life_s * 1000:
                        break
                    if lng and tsell[j] and tpx[j] <= B + 1e-12:
                        acc += tsz[j]
                    elif (not lng) and (not tsell[j]) and tpx[j] >= A - 1e-12:
                        acc += tsz[j]
                    if acc > need:
                        fill_t = int(tt[j])
                        break
                if swept.any():
                    tsw = int(bt[k + int(np.argmax(swept))])
                    fill_t = tsw if fill_t is None else min(fill_t, tsw)
            if fill_t is None:
                out.append(dict(coin=coin, filled=0, pnl=np.nan,
                                mid_win=r.mid_win, fold=r.fold))
                continue
            f = int(np.searchsorted(bt, fill_t, "right") - 1)
            tp = P * (1 + G) if lng else P * (1 - G)
            sl = P * (1 - G) if lng else P * (1 + G)
            end = int(np.searchsorted(bt, fill_t + MAXHOLD, "right"))
            bb, aa = bpx[f:end], apx[f:end]
            hit_tp = (bb >= tp) if lng else (aa <= tp)
            hit_sl = (bb <= sl) if lng else (aa >= sl)
            itp = int(np.argmax(hit_tp)) if hit_tp.any() else 10 ** 9
            isl = int(np.argmax(hit_sl)) if hit_sl.any() else 10 ** 9
            if itp < isl:
                pnl = G
            elif isl < itp:
                pnl = -G - taker
            else:
                px = bb[-1] if lng else aa[-1]
                pnl = ((px - P) / P if lng else (P - px) / P) - taker
            q1 = int(np.searchsorted(bt, fill_t + 5000, "right") - 1)
            m0 = (bpx[f] + apx[f]) / 2
            mk5 = (((bpx[q1] + apx[q1]) / 2 - m0) / m0) * (1 if lng else -1) * 100
            out.append(dict(coin=coin, filled=1, pnl=pnl * 100,
                            mid_win=r.mid_win, fold=r.fold, mk5=mk5))
    return pd.DataFrame(out)


for taker, world in ((0.0, "LIGHTER fees=0"), (0.0004, "ASTER taker=0.04%")):
    print("\n############ %s ############" % world)
    for model in ("front", "queue", "sweep"):
        for life in (30, 120):
            R = run(model, life, taker)
            if R.empty:
                continue
            Fl = R[R.filled == 1]
            line = ("%s life %3ds  fill %5.1f%%  n=%5d" %
                    (model.ljust(6), life, 100 * len(Fl) / len(R), len(Fl)))
            if len(Fl):
                nf = R[R.filled == 0]
                line += (" | win %5.1f%%  EV/fill %+.4f%%  EV/signal %+.4f%%"
                         "  mk5 %+.4f | midwin filled %.1f%% vs unfilled %.1f%%"
                         % (100 * (Fl.pnl > 0).mean(), Fl.pnl.mean(),
                            Fl.pnl.sum() / len(R), Fl.mk5.mean(),
                            100 * Fl.mid_win.mean(),
                            100 * nf.mid_win.mean() if len(nf) else float("nan")))
            print(line, flush=True)
    # per-coin at the middle assumption
    R = run("queue", 120, taker)
    Fl = R[R.filled == 1]
    print("per-coin (queue/120s): " + "  ".join(
        "%s %+0.4f%%(n=%d)" % (c, g.pnl.mean(), len(g))
        for c, g in Fl.groupby("coin")))
print("\nFILLSIM-POOL COMPLETE")
