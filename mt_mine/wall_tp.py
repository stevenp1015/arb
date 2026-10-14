#!/usr/bin/env python3
"""wall_tp.py — can a sell wall tell you where to put your take-profit?

BACKGROUND
magnet.py found that price OVERSHOOTS LESS past an ask-side volume wall than past
a bare level the same distance away (20-39% less, effect growing with wall size).
It found NO attraction — walls do not pull price toward them. So the finding is a
barrier, not a magnet.

A barrier has one obvious use: it tells you where a move runs out. So a
take-profit placed JUST UNDER a sell wall should fill more reliably than one
placed JUST PAST it.

THE TRAP THIS IS DESIGNED AROUND
At ANY level, a nearer target fills more often than a further one. That is pure
geometry and says nothing about walls. So the statistic here is a
difference-in-differences:

    gap_wall = P(reach just-under | a wall is there)
             - P(reach just-past  | a wall is there)

    gap_ctrl = the same two probabilities at the SAME distances,
               at a random other moment in the same symbol

    effect   = gap_wall - gap_ctrl

Only `effect` is evidence about walls. gap_wall on its own is geometry.

We also price it: long from mid, take-profit at each candidate, stop at a fixed
multiple of the same distance, scored by first passage (which comes first, no
clock). That converts the finding into an expected value per trade, which is the
only thing that decides whether it is worth trading.

GATES: synthetic selftest, distance/side/symbol-matched control, explicit n.
"""
import json, subprocess, sys, argparse, random
from collections import defaultdict

WALL_USD  = [50_000, 100_000, 250_000]
# How far PAST the wall to place the further target, as a multiple of the
# distance already travelled. CORRECTED 2026-08-12: the first run used a single
# value of 0.15, but magnet.py shows the MEDIAN overshoot past a level is around
# 0.9-1.0x the distance. A target only 0.15x beyond is cleared almost always in
# both the wall and the control case, so it cannot discriminate. We now sweep
# across the region where the two distributions actually separate.
EPS_LIST  = [0.15, 0.50, 1.00, 1.50]
SL_MULTS  = [1.0, 2.0]   # stop distance as a multiple of the take-profit distance
HORIZON_S = 300
MIN_N     = 300
FEE_STOP  = 0.04      # a stop crosses the book (Aster taker); the TP rests (maker)


def wall_level(levels, side, mid, threshold_usd):
    cum = 0.0
    for px, qty in levels:
        cum += px * qty
        if cum >= threshold_usd:
            d = (px / mid - 1) * 100 if side == "a" else (1 - px / mid) * 100
            return px, d
    return None


def first_passage(series, i, up_pct, dn_pct):
    """Long from mid at i. Returns 'tp' / 'sl' / 'timeout'.
    Ties inside one observation are scored as the LOSS."""
    t0, mid = series[i][0], series[i][1]
    tp = mid * (1 + up_pct / 100.0)
    sl = mid * (1 - dn_pct / 100.0)
    for j in range(i + 1, len(series)):
        tj, m = series[j][0], series[j][1]
        if tj - t0 > HORIZON_S:
            return "timeout"
        if m <= sl:
            return "sl"
        if m >= tp:
            return "tp"
    return "timeout"


def load(path, want, max_snaps, every):
    out = defaultdict(list)
    cnt = defaultdict(int)
    cmd = ["zstdcat", path] if path.endswith(".zst") else ["cat", path]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=1 << 22)
    total = 0
    for raw in p.stdout:
        try:
            d = json.loads(raw)
            sym, t, b, a = d["c"], d["t"] / 1000.0, d["b"], d["a"]
        except Exception:
            continue
        if want and sym not in want:
            continue
        if not b or not a:
            continue
        cnt[sym] += 1
        if cnt[sym] % every:
            continue
        out[sym].append((t, (float(b[0][0]) + float(a[0][0])) / 2.0,
                         [[float(x), float(y)] for x, y in a]))
        total += 1
        if max_snaps and total >= max_snaps:
            break
    p.stdout.close(); p.kill()
    return out


def selftest():
    asks = [[100.0, 1.0], [101.0, 5.0]]
    px, d = wall_level(asks, "a", 100.0, 300)
    assert px == 101.0 and abs(d - 1.0) < 1e-9, f"wall_level wrong: {px},{d}"

    s = [(0, 100.0, []), (1, 100.6, []), (2, 99.0, [])]
    assert first_passage(s, 0, 0.5, 1.0) == "tp", "up first should be tp"
    s = [(0, 100.0, []), (1, 99.4, []), (2, 101.0, [])]
    assert first_passage(s, 0, 0.5, 0.5) == "sl", "down first should be sl"
    s = [(0, 100.0, []), (1, 100.1, [])]
    assert first_passage(s, 0, 5.0, 5.0) == "timeout", "neither should time out"
    print("selftest OK (4 checks)", file=sys.stderr)


def ev(counts, tp_pct, sl_pct):
    n = sum(counts.values())
    if not n:
        return 0.0, 0.0
    win = counts["tp"] / n
    return win * 100, (counts["tp"] * tp_pct - counts["sl"] * (sl_pct + FEE_STOP)) / n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tape", required=True)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--sample-every", type=int, default=1)
    ap.add_argument("--max-snaps", type=int, default=400000)
    ap.add_argument("--selftest-only", action="store_true")
    a = ap.parse_args()

    selftest()
    if a.selftest_only:
        sys.exit(0)

    want = set(s for s in a.symbols.split(",") if s)
    series = load(a.tape, want, a.max_snaps, a.sample_every)
    for s in sorted(series, key=lambda x: -len(series[x])):
        print(f"  {s:12s} {len(series[s]):7,d} snapshots", file=sys.stderr)

    rnd = random.Random(20260812)
    # key -> (thr, slm, which)  where which in {under_wall, over_wall, under_ctl, over_ctl}
    acc = defaultdict(lambda: defaultdict(int))

    for sym, ser in series.items():
        n = len(ser)
        if n < 200:
            continue
        for i in range(n - 1):
            asks = ser[i][2]
            for thr in WALL_USD:
                w = wall_level(asks, "a", ser[i][1], thr)
                if w is None:
                    continue
                _, d = w
                if d <= 0:
                    continue
                ci = rnd.randrange(n - 1)
                for eps in EPS_LIST:
                    under, over = d * (1 - eps) if eps < 1 else d * 0.85, d * (1 + eps)
                    for slm in SL_MULTS:
                        key = (thr, slm, eps)
                        acc[key + ("under_wall",)][first_passage(ser, i, under, d * slm)] += 1
                        acc[key + ("over_wall",)][first_passage(ser, i, over, d * slm)] += 1
                        acc[key + ("under_ctl",)][first_passage(ser, ci, under, d * slm)] += 1
                        acc[key + ("over_ctl",)][first_passage(ser, ci, over, d * slm)] += 1

    print(f"\n{'='*112}")
    print(f"TAKE-PROFIT PLACEMENT vs A SELL WALL — {a.tape.split('/')[-1]}")
    print("target BELOW the wall is fixed at 0.85x the wall distance;")
    print("target ABOVE sweeps 1.15x / 1.50x / 2.00x / 2.50x the wall distance")
    print(f"{'='*112}")
    print(f"{'wall $':>9s} {'SLx':>4s} {'past':>5s} | {'HIT under':>10s} {'HIT past':>9s} {'gap':>7s} | "
          f"{'ctl under':>10s} {'ctl past':>9s} {'gap':>7s} | {'EFFECT':>8s} {'n':>7s}")
    for thr in WALL_USD:
      for eps in EPS_LIST:
        for slm in SL_MULTS:
            k = lambda w: acc[(thr, slm, eps, w)]
            ns = [sum(k(w).values()) for w in
                  ("under_wall", "over_wall", "under_ctl", "over_ctl")]
            if min(ns) < MIN_N:
                continue
            uw = 100 * k("under_wall")["tp"] / ns[0]
            ow = 100 * k("over_wall")["tp"] / ns[1]
            uc = 100 * k("under_ctl")["tp"] / ns[2]
            oc = 100 * k("over_ctl")["tp"] / ns[3]
            print(f"{thr:9,d} {slm:4.1f} {1+eps:5.2f} | {uw:9.1f}% {ow:8.1f}% {uw-ow:+7.2f} | "
                  f"{uc:9.1f}% {oc:8.1f}% {uc-oc:+7.2f} | "
                  f"{(uw-ow)-(uc-oc):+8.2f} {ns[0]:7d}")

    print("\n  HIT   = reached the take-profit before the stop, within "
          f"{HORIZON_S}s, sitters counted")
    print("  gap   = how much more often the nearer target fills. Mostly geometry.")
    print("  EFFECT= gap at a wall MINUS gap at a bare level the same distance away.")
    print("          This is the only column that is evidence about walls.")
    print("          Positive = a wall really does cap the move where it sits.")
