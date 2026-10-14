#!/usr/bin/env python3
"""
fp_engine/cancel_defense.py — does an INSTANT CANCEL fix maker adverse selection?

Every maker attempt in this project has died the same way: the orders that fill are
the ones the market was about to run over (ASTER: filled 54.7% win vs UNFILLED 77-93%).
Lighter Premium sells 0ms maker/cancel latency while holding takers 200ms. So:

  If we pull the resting quote the moment Binance moves against us, do the poisoned
  fills disappear — and does what's left make money?

Simulation (all on the aligned 50ms Binance+venue grid, one clock):
  ENTRY  passive BUY resting at the venue best bid B; fills at the first later tick
         where best bid trades BELOW B (someone sold through us — the conservative
         "swept" model, no queue-position credit).
  EXIT   TP = resting maker SELL at B*(1+g), fills when best ask trades ABOVE it.
         SL = taker, cross and sell into the bid when bid <= B*(1-g).
         Whichever comes first. Mirror-image for the short side.
  So the spread is EARNED on entry and on TP, and PAID on the stop. That is the real
  maker round trip, not a mid-price fantasy.

CANCEL RULE: quote is pulled if Binance moved adversely by >= thr over a trailing
window, and that move completed at least CANCEL_LAG_TICKS full grid ticks before the
fill. On a 50ms grid that means we only claim to dodge a fill when we provably saw the
move a whole tick earlier — pessimistic about our own speed (real detection ~10ms),
which is the safe direction to be wrong.

Reported: all fills vs KEPT fills vs CANCELLED fills. The hypothesis predicts the
cancelled bucket is sharply negative and the kept bucket clears zero.

Usage: python3 cancel_defense.py --selftest | --run [--limit N] [--samples N]
"""
import argparse, csv, os, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cont_probe import load_sym, GRID_MS, SPLIT

OUT = "/root/fp_engine/cont_out/cancel_defense.csv"
CANCEL_LAG_TICKS = 1          # must have seen the move >= 1 full tick before the fill
TRIG_WINDOWS_MS = (250, 500)  # trailing window for the adverse-move detector
TRIG_QS = (0.99, 0.999)       # detector sensitivity (candidates; data elects)
MAX_WAIT_TICKS = 1200         # 60s to get filled, else the quote is abandoned
MAX_HOLD_TICKS = 6000         # 5min forward scan for the exit
PREMIUM_MAKER_FEE = 0.00004   # 0.0040% — Lighter Premium's price for 0ms cancel


def simulate(bid, ask, i, g, side, max_wait, max_hold):
    """Returns (fill_tick, pnl_frac) or None if never filled / never resolved."""
    n = len(bid)
    if side > 0:
        B = bid[i]
        if not np.isfinite(B):
            return None
        hi = min(i + max_wait, n)
        seg = bid[i + 1:hi]
        hit = np.isfinite(seg) & (seg < B - 1e-15)
        if not hit.any():
            return None
        j = i + 1 + int(np.argmax(hit))
        tp, sl = B * (1 + g), B * (1 - g)
        e = min(j + max_hold, n)
        a, b = ask[j:e], bid[j:e]
        htp = np.isfinite(a) & (a > tp)
        hsl = np.isfinite(b) & (b <= sl)
        itp = int(np.argmax(htp)) if htp.any() else 10 ** 9
        isl = int(np.argmax(hsl)) if hsl.any() else 10 ** 9
        if itp == isl == 10 ** 9:
            return None
        if itp < isl:
            return j, g
        return j, (b[isl] - B) / B
    else:
        A = ask[i]
        if not np.isfinite(A):
            return None
        hi = min(i + max_wait, n)
        seg = ask[i + 1:hi]
        hit = np.isfinite(seg) & (seg > A + 1e-15)
        if not hit.any():
            return None
        j = i + 1 + int(np.argmax(hit))
        tp, sl = A * (1 - g), A * (1 + g)
        e = min(j + max_hold, n)
        a, b = ask[j:e], bid[j:e]
        htp = np.isfinite(b) & (b < tp)
        hsl = np.isfinite(a) & (a >= sl)
        itp = int(np.argmax(htp)) if htp.any() else 10 ** 9
        isl = int(np.argmax(hsl)) if hsl.any() else 10 ** 9
        if itp == isl == 10 ** 9:
            return None
        if itp < isl:
            return j, g
        return j, (A - a[isl]) / A


def selftest():
    # long: bid steps down (fills us at 100), then ask rises through the TP
    bid = np.array([100.0, 99.9, 99.95, 100.4, 100.6])
    ask = np.array([100.1, 100.0, 100.05, 100.5, 100.9])
    r = simulate(bid, ask, 0, 0.005, +1, 10, 10)
    assert r is not None and r[0] == 1 and abs(r[1] - 0.005) < 1e-12, r
    # long: bid steps down then keeps falling through the stop
    bid = np.array([100.0, 99.9, 99.0, 98.0])
    ask = np.array([100.1, 100.0, 99.1, 98.1])
    r = simulate(bid, ask, 0, 0.005, +1, 10, 10)
    assert r is not None and r[0] == 1 and r[1] < -0.004, r
    # never filled (bid never trades below our level)
    bid = np.array([100.0, 100.1, 100.2]); ask = np.array([100.1, 100.2, 100.3])
    assert simulate(bid, ask, 0, 0.005, +1, 10, 10) is None
    # short mirror: ask steps up (fills us at 100.1), bid falls through TP
    bid = np.array([100.0, 100.1, 99.4]); ask = np.array([100.1, 100.2, 99.5])
    r = simulate(bid, ask, 0, 0.005, -1, 10, 10)
    assert r is not None and r[0] == 1 and abs(r[1] - 0.005) < 1e-12, r
    print("SELFTEST PASS (maker fill + TP/SL resolution, both sides, no-fill case)")


def run_sym(path, samples, rng):
    g0 = load_sym(path)
    if g0 is None:
        return []
    t0, nb, bn_mid, asb, asa, fresh = g0
    sym = os.path.basename(path)[:-4]
    mid = (asb + asa) / 2.0
    ok = np.isfinite(bn_mid) & np.isfinite(mid) & fresh
    if ok.sum() < 20_000:
        return []
    spread = float(np.nanmedian((asa - asb)[ok] / mid[ok]))
    if not np.isfinite(spread) or spread <= 0:
        return []
    rows = []
    pool = np.flatnonzero(ok)
    pool = pool[pool + MAX_WAIT_TICKS + MAX_HOLD_TICKS < nb]
    if len(pool) < 500:
        return []
    idx = rng.choice(pool, size=min(samples, len(pool)), replace=False)
    idx.sort()
    for W in TRIG_WINDOWS_MS:
        w = max(1, W // GRID_MS)
        ret = np.full(nb, np.nan)
        ret[w:] = bn_mid[w:] / bn_mid[:-w] - 1.0
        fin = np.isfinite(ret)
        if fin.sum() < 20_000:
            continue
        for q in TRIG_QS:
            thr = float(np.quantile(np.abs(ret[fin]), q))
            if not np.isfinite(thr) or thr <= 0:
                continue
            down = fin & (ret <= -thr)      # adverse for a resting BUY
            up = fin & (ret >= thr)         # adverse for a resting SELL
            cdn = np.concatenate(([0], np.cumsum(down.astype(np.int64))))
            cup = np.concatenate(([0], np.cumsum(up.astype(np.int64))))
            for gm in (2.5, 4.0, 7.0):
                g = spread * gm
                for side in (+1, -1):
                    acc = {"all": [], "kept": [], "cxl": []}
                    for i in idx:
                        r = simulate(asb, asa, int(i), g, side,
                                     MAX_WAIT_TICKS, MAX_HOLD_TICKS)
                        if r is None:
                            continue
                        j, pnl = r
                        lo, hi = int(i), max(int(i), j - CANCEL_LAG_TICKS)
                        c = cdn if side > 0 else cup
                        pulled = (c[hi] - c[lo]) > 0
                        acc["all"].append(pnl)
                        acc["cxl" if pulled else "kept"].append(pnl)
                    if len(acc["all"]) < 50:
                        continue
                    d = dict(sym=sym, spread_pct=spread * 100, trig_ms=W, trig_q=q,
                             geom_x_spread=gm, geom_pct=g * 100,
                             side="long" if side > 0 else "short")
                    for k in ("all", "kept", "cxl"):
                        v = np.asarray(acc[k])
                        d["n_" + k] = len(v)
                        d["win_" + k] = float((v > 0).mean() * 100) if len(v) else np.nan
                        d["ev_" + k] = float(v.mean() * 100) if len(v) else np.nan
                    d["cancel_rate"] = 100.0 * d["n_cxl"] / d["n_all"]
                    d["ev_kept_prem"] = (d["ev_kept"] - 2 * PREMIUM_MAKER_FEE * 100
                                         if np.isfinite(d["ev_kept"]) else np.nan)
                    rows.append(d)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--samples", type=int, default=1500)
    a = ap.parse_args()
    selftest()
    if a.selftest or not a.run:
        return
    files = [os.path.join(SPLIT, f) for f in sorted(os.listdir(SPLIT))
             if os.path.getsize(os.path.join(SPLIT, f)) > 600_000]
    if a.limit:
        files = files[:a.limit]
    print("RUN over %d symbols, %d quote placements each" % (len(files), a.samples),
          flush=True)
    rng = np.random.default_rng(11)
    out = []
    for n, p in enumerate(files):
        try:
            out.extend(run_sym(p, a.samples, rng))
        except Exception as e:
            print("ERR %s %r" % (p, e), flush=True)
        if (n + 1) % 20 == 0:
            print("  %d/%d, %d rows" % (n + 1, len(files), len(out)), flush=True)
    if not out:
        print("NO RESULTS"); return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader(); w.writerows(out)
    print("WROTE %s (%d rows)" % (OUT, len(out)), flush=True)
    print("CANCEL-DEFENSE COMPLETE", flush=True)


if __name__ == "__main__":
    main()
