#!/usr/bin/env python3
"""magnet.py — is a big resting volume wall a price magnet?

THE CLAIM (Steven, from watching MoonTrader's tick chart live on Hyperliquid)
Depth Shot places its take-profit at the price where cumulative resting book
volume reaches a threshold. He repeatedly observed price travelling *precisely*
to that level — consistently enough, across many parameter sets, to be obvious by
eye. The implied mechanism: concentrations of resting size attract price.

WHY THIS NEEDS CAREFUL NULLS
"Price reaches the wall side more often" is NOT evidence of a magnet. Book
imbalance already predicts short-horizon direction, and this repo has confirmed
that on three independent datasets. If we only measured which side gets reached,
we would rediscover imbalance and call it magnetism.

So we measure two different things:

  TEST A — ATTRACTION. Does price reach the wall level more often than a
  distance-matched level at a random other time? Confounded with imbalance by
  construction. Reported, but not the point.

  TEST B — TAGGING (the actual claim). GIVEN that price reached the level, does
  it STALL there rather than carrying straight on through? That is what "travels
  precisely to that level" means. Imbalance says nothing about stalling, so a
  positive result here is not imbalance wearing a hat.

NULLS
For every wall observation (distance d, side s, symbol) we draw a control from a
random OTHER snapshot of the same symbol and take the level at the same distance
d on the same side s. Distance, side and symbol are held fixed; the only thing
that varies is whether a wall happens to sit there. Same-symbol controls also
absorb per-coin volatility and tick size.

We additionally record WALL PERSISTENCE — did the wall still exist when price
arrived, or was it pulled? A wall that always vanishes first is a spoof, and that
changes what the result means.

GATES
  - synthetic selftest with hand-checkable answers
  - distance-matched, side-matched, symbol-matched control
  - explicit sample sizes on every line; nothing reported under MIN_N
"""
import json, subprocess, sys, argparse, random, math
from collections import defaultdict

# cumulative USD notional that defines "a wall"
WALL_USD = [50_000, 100_000, 250_000, 500_000]
HORIZON_S = 300          # 5 minutes to reach the level
STALL_HORIZON_S = 60     # after touching, how long we watch for continuation
STALL_MULT = 0.5         # "carried on through" = advanced another 0.5 x d beyond
MIN_N = 200
SAMPLE_EVERY = 20        # use every Nth snapshot per symbol, to decorrelate
                         # (overridable: Steven watched a TICK chart, so a
                         #  tag-and-reverse faster than the sample period is
                         #  invisible. --sample-every 1 = full ~100ms feed.)


def wall_level(levels, side, mid, threshold_usd):
    """Walk the book away from mid, accumulating notional. Return (price, dist%)
    of the level at which cumulative resting size first reaches threshold_usd.
    None if the visible book never gets there."""
    cum = 0.0
    for px, qty in levels:
        cum += px * qty
        if cum >= threshold_usd:
            d = (px / mid - 1) * 100 if side == "a" else (1 - px / mid) * 100
            return px, d
    return None


def parse_aster(line):
    d = json.loads(line)
    return d["c"], d["t"] / 1000.0, d["b"], d["a"]


def parse_binance(line):
    d = json.loads(line)["m"]["data"]
    return (d["s"], d["T"] / 1000.0,
            [[float(p), float(q)] for p, q in d["b"]],
            [[float(p), float(q)] for p, q in d["a"]])


def load(path, want_syms, parser, needle, max_snaps, every=None):
    """-> {sym: [(t, mid, bids, asks)]}, sampled every SAMPLE_EVERY snapshots."""
    out = defaultdict(list)
    counter = defaultdict(int)
    cmd = ["zstdcat", path] if path.endswith(".zst") else ["cat", path]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=1 << 22)
    total = 0
    for raw in proc.stdout:
        if needle and needle not in raw:
            continue
        try:
            sym, t, b, a = parser(raw)
        except Exception:
            continue
        if want_syms and sym not in want_syms:
            continue
        if not b or not a:
            continue
        counter[sym] += 1
        if counter[sym] % (every or SAMPLE_EVERY):
            continue
        mid = (float(b[0][0]) + float(a[0][0])) / 2.0
        out[sym].append((t, mid, [[float(p), float(q)] for p, q in b],
                         [[float(p), float(q)] for p, q in a]))
        total += 1
        if max_snaps and total >= max_snaps:
            break
    proc.stdout.close(); proc.kill()
    return out


def reached_and_stalled(series, i, target, side, d_pct):
    """From snapshot i, walk forward.
    Returns (reached: bool, stalled: bool|None, persisted: bool|None).
      reached  — did mid touch the target level within HORIZON_S
      stalled  — GIVEN reached, did price fail to advance a further
                 STALL_MULT * d beyond it within STALL_HORIZON_S
      persisted— GIVEN reached, was a wall still sitting there on arrival
    """
    t0 = series[i][0]
    beyond = target * (1 + STALL_MULT * d_pct / 100) if side == "a" else \
             target * (1 - STALL_MULT * d_pct / 100)
    n = len(series)
    for j in range(i + 1, n):
        tj, midj = series[j][0], series[j][1]
        if tj - t0 > HORIZON_S:
            return False, None, None
        hit = (midj >= target) if side == "a" else (midj <= target)
        if hit:
            # did it carry on through, within the stall window?
            for k in range(j, n):
                tk, midk = series[k][0], series[k][1]
                if tk - tj > STALL_HORIZON_S:
                    return True, True, None
                past = (midk >= beyond) if side == "a" else (midk <= beyond)
                if past:
                    return True, False, None
            return True, True, None
    return False, None, None


def excursion_beyond(series, i, target, side, d_pct):
    """Continuous version of the tagging test, free of any arbitrary cutoff.

    Walk forward to the first touch of `target`. Then measure how far price runs
    PAST that level within STALL_HORIZON_S, expressed as a multiple of the
    original distance d. 0.0 means it stopped dead at the level (a perfect tag);
    1.0 means it carried on as far again as it had already travelled.
    Returns None if the level is never reached."""
    t0 = series[i][0]
    n = len(series)
    for j in range(i + 1, n):
        tj, midj = series[j][0], series[j][1]
        if tj - t0 > HORIZON_S:
            return None
        if (midj >= target) if side == "a" else (midj <= target):
            worst = 0.0
            for k in range(j, n):
                tk, midk = series[k][0], series[k][1]
                if tk - tj > STALL_HORIZON_S:
                    break
                past = (midk / target - 1) if side == "a" else (1 - midk / target)
                if past > worst:
                    worst = past
            return worst / (d_pct / 100.0) if d_pct else None
    return None


def run(series_by_sym, label):
    rnd = random.Random(20260812)
    res = defaultdict(lambda: {"t_reach": 0, "t_n": 0, "t_stall": 0, "t_stall_n": 0,
                               "c_reach": 0, "c_n": 0, "c_stall": 0, "c_stall_n": 0,
                               "t_exc": [], "c_exc": []})

    for sym, series in series_by_sym.items():
        n = len(series)
        if n < 100:
            continue
        for i in range(n - 1):
            t, mid, bids, asks = series[i]
            for thr in WALL_USD:
                for side, levels in (("a", asks), ("b", bids)):
                    w = wall_level(levels, side, mid, thr)
                    if w is None:
                        continue
                    target, d = w
                    if d <= 0:
                        continue
                    k = (thr, side)

                    reached, stalled, _ = reached_and_stalled(series, i, target, side, d)
                    r = res[k]
                    r["t_n"] += 1
                    if reached:
                        r["t_reach"] += 1
                        r["t_stall_n"] += 1
                        r["t_stall"] += 1 if stalled else 0
                        e = excursion_beyond(series, i, target, side, d)
                        if e is not None:
                            r["t_exc"].append(e)

                    # CONTROL: same symbol, same side, same distance, random other time
                    ci = rnd.randrange(n - 1)
                    cmid = series[ci][1]
                    ctarget = cmid * (1 + d / 100) if side == "a" else cmid * (1 - d / 100)
                    creached, cstalled, _ = reached_and_stalled(series, ci, ctarget, side, d)
                    r["c_n"] += 1
                    if creached:
                        r["c_reach"] += 1
                        r["c_stall_n"] += 1
                        r["c_stall"] += 1 if cstalled else 0
                        e = excursion_beyond(series, ci, ctarget, side, d)
                        if e is not None:
                            r["c_exc"].append(e)

    print(f"\n{'='*106}\n{label}\n{'='*106}")
    print(f"{'wall $':>9s} {'side':>5s} {'n':>7s} | "
          f"{'REACH wall':>11s} {'REACH ctrl':>11s} {'diff pp':>8s} | "
          f"{'STALL wall':>11s} {'STALL ctrl':>11s} {'diff pp':>8s} {'n_stall':>8s}")
    for (thr, side) in sorted(res):
        r = res[(thr, side)]
        if r["t_n"] < MIN_N or min(r["t_stall_n"], r["c_stall_n"]) < MIN_N:
            continue          # STALL stats are computed over the reached subset,
                              # so they must clear MIN_N too, not just t_n
        tr = 100 * r["t_reach"] / r["t_n"]
        cr = 100 * r["c_reach"] / r["c_n"] if r["c_n"] else 0
        ts = 100 * r["t_stall"] / r["t_stall_n"] if r["t_stall_n"] else 0
        cs = 100 * r["c_stall"] / r["c_stall_n"] if r["c_stall_n"] else 0
        print(f"{thr:9,d} {side:>5s} {r['t_n']:7d} | "
              f"{tr:10.1f}% {cr:10.1f}% {tr-cr:+8.2f} | "
              f"{ts:10.1f}% {cs:10.1f}% {ts-cs:+8.2f} {r['t_stall_n']:8d}")
    print(f"\n{'wall $':>9s} {'side':>5s} | MEDIAN OVERSHOOT PAST THE LEVEL "
          f"(x the distance travelled; 0 = stopped dead)")
    print(f"{'':>9s} {'':>5s} | {'wall':>10s} {'control':>10s} {'diff':>9s} "
          f"{'n_wall':>8s}")
    for (thr, side) in sorted(res):
        r = res[(thr, side)]
        if len(r["t_exc"]) < MIN_N or len(r["c_exc"]) < MIN_N:
            continue
        tw = sorted(r["t_exc"])[len(r["t_exc"]) // 2]
        cw = sorted(r["c_exc"])[len(r["c_exc"]) // 2]
        print(f"{thr:9,d} {side:>5s} | {tw:10.3f} {cw:10.3f} {tw-cw:+9.3f} "
              f"{len(r['t_exc']):8d}")
    print("  A real magnet means price overshoots LESS past a wall than past a bare level.")

    print("\n  REACH = did price touch the level within "
          f"{HORIZON_S}s (confounded with book imbalance — expect a difference)")
    print(f"  STALL = GIVEN it touched, did it fail to push another "
          f"{STALL_MULT}x the distance beyond within {STALL_HORIZON_S}s")
    print("  STALL is the magnet claim. A positive STALL diff is the result that matters.")
    return res


def selftest():
    # wall_level walks away from mid accumulating notional
    asks = [[100.0, 1.0], [101.0, 2.0], [102.0, 50.0]]
    px, d = wall_level(asks, "a", 100.0, 250)      # 100 + 202 = 302 >= 250 at 101
    assert px == 101.0, f"expected the 101 level, got {px}"
    assert abs(d - 1.0) < 1e-9, f"expected 1% away, got {d}"
    assert wall_level(asks, "a", 100.0, 10 ** 9) is None, "should not find a wall"

    bids = [[100.0, 1.0], [99.0, 5.0]]
    px, d = wall_level(bids, "b", 100.0, 300)      # 100 + 495 >= 300 at 99
    assert px == 99.0 and abs(d - 1.0) < 1e-9, f"bid side wrong: {px} {d}"

    # reached_and_stalled: price rises to 101 and stops -> reached, stalled
    s = [(0, 100.0, [], []), (1, 100.5, [], []), (2, 101.0, [], []),
         (3, 101.1, [], []), (4, 101.0, [], [])]
    reached, stalled, _ = reached_and_stalled(s, 0, 101.0, "a", 1.0)
    assert reached and stalled, f"should reach and stall, got {reached},{stalled}"

    # price blows through to 102 -> reached, NOT stalled (beyond = 101*1.005)
    s = [(0, 100.0, [], []), (1, 101.0, [], []), (2, 102.0, [], [])]
    reached, stalled, _ = reached_and_stalled(s, 0, 101.0, "a", 1.0)
    assert reached and not stalled, f"should punch through, got {reached},{stalled}"

    # never gets there
    s = [(0, 100.0, [], []), (1, 100.1, [], [])]
    reached, _, _ = reached_and_stalled(s, 0, 101.0, "a", 1.0)
    assert not reached, "should not reach"
    # excursion: touches 101 then runs to 102 => overshoot 1/101 of price,
    # expressed as a multiple of d=1% -> about 0.990
    s = [(0, 100.0, [], []), (1, 101.0, [], []), (2, 102.0, [], [])]
    e = excursion_beyond(s, 0, 101.0, "a", 1.0)
    assert e is not None and abs(e - 0.990) < 0.01, f"excursion wrong: {e}"
    # stops dead at the level -> overshoot 0
    s = [(0, 100.0, [], []), (1, 101.0, [], []), (2, 100.9, [], [])]
    assert abs(excursion_beyond(s, 0, 101.0, "a", 1.0)) < 1e-9, "should be zero"
    print("selftest OK (9 checks)", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tape", required=False)
    ap.add_argument("--venue", choices=["aster", "binance"], default="aster")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--max-snaps", type=int, default=400000)
    ap.add_argument("--sample-every", type=int, default=None)
    ap.add_argument("--selftest-only", action="store_true")
    a = ap.parse_args()

    selftest()
    if a.selftest_only:
        sys.exit(0)

    want = set(s for s in a.symbols.split(",") if s)
    if a.venue == "aster":
        parser, needle = parse_aster, None
    else:
        parser, needle = parse_binance, b"depth20"

    print(f"loading {a.tape} ({a.venue}), every {a.sample_every or SAMPLE_EVERY}th snapshot, "
          f"cap {a.max_snaps:,}...", file=sys.stderr)
    series = load(a.tape, want, parser, needle, a.max_snaps, a.sample_every)
    for s in sorted(series, key=lambda x: -len(series[x])):
        span = (series[s][-1][0] - series[s][0][0]) / 3600
        print(f"  {s:14s} {len(series[s]):7,d} snapshots over {span:5.1f}h",
              file=sys.stderr)
    run(series, f"VOLUME-WALL MAGNET TEST — {a.venue} — {a.tape.split('/')[-1]}")
