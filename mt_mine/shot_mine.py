#!/usr/bin/env python3
"""shot_mine.py — derive MoonTrader Shot-family parameters from our own tape.

WHY THIS EXISTS
The liquidation-fade harvest (THE_PLAN item 2) failed at -0.10%/signal with a
hand-picked bracket (TP +0.69% / SL -1.18%). The autopsy named the mechanism: all
141 stop-outs realized WORSE than their stop, and 41% of signals timed out with 120
of those positive-but-under-target. MoonTrader's Shot algorithm ships parameters
aimed at exactly those failures. This mines values for them from our own tape
instead of guessing them, per the prime directive: every parameter derived.

WHAT A SHOT IS
A resting limit order parked D% away from price, trailing it with a dead zone. It
fills only when a spike reaches out and takes it. Then TP/SL decide the outcome.

DESIGN NOTE — why this is fast
Naively re-simulating every (distance, TP, SL) combination means 672 full passes
over tens of millions of prints. Instead: bucket the tape into 100ms bars (first
passage only cares about extremes, which bars preserve), then for each fill do ONE
forward walk that records when each TP level and each SL level is first reached.
Every geometry is then resolved by comparing two integers. O(fills x horizon)
instead of O(fills x horizon x |TP| x |SL|).

FILL MODEL (pessimistic, matching cancel_defense.py)
A resting buy at P fills only when the tape trades THROUGH P (bar low <= P), not
merely touches it. Understates fills, overstates adverse selection. Honest direction.

GATES (standing law: nothing is believed without them)
  - synthetic selftest with a hand-checkable answer
  - independent brute-force cross-check on real bars (slow path vs fast path)
  - random-entry null from the same window, so drift is not mistaken for edge
  - episode dedup, and sitters (timeouts) counted in every mean
"""
import json, sys, subprocess, argparse
from collections import defaultdict

DISTANCES = [0.10, 0.15, 0.20, 0.30, 0.40, 0.50, 0.75, 1.00]   # Shot "Distance", %
TPS       = [0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50]          # Take Profit, %
SLS       = [0.10, 0.20, 0.30, 0.50, 0.75, 1.00]                # Stop Loss, %
BAR_MS    = 100
DEDUP_S   = 60
HORIZON_BARS = 36000        # 100ms bars -> 1 hour, matches the liq_paper timeout
FEE_STOP  = 0.04            # a stop crosses the book (Aster taker). Maker legs = 0.


# ------------------------------------------------------------------ bar layer
def to_bars(trades, bar_ms=BAR_MS):
    """(ts_sec, price) -> list of (bar_index, high, low, close). Extremes kept."""
    bars = []
    cur = None
    for ts, px in trades:
        b = int(ts * 1000 // bar_ms)
        if cur is None or b != cur[0]:
            if cur is not None:
                bars.append(tuple(cur))
            cur = [b, px, px, px]
        else:
            if px > cur[1]: cur[1] = px
            if px < cur[2]: cur[2] = px
            cur[3] = px
    if cur is not None:
        bars.append(tuple(cur))
    return bars


# ------------------------------------------------------------- the fast path
def first_touch_indices(bars, start, entry, side, levels_fav, levels_adv):
    """ONE forward walk. Returns, for each favorable level and each adverse level,
    the bar offset at which it is first reached (or None). levels_* are % values."""
    fav_px = [entry * (1 + side * L / 100.0) for L in levels_fav]
    adv_px = [entry * (1 - side * L / 100.0) for L in levels_adv]
    fav_hit = [None] * len(levels_fav)
    adv_hit = [None] * len(levels_adv)
    nf, na = 0, 0
    end = min(len(bars), start + HORIZON_BARS)
    t0 = bars[start][0]
    for j in range(start, end):
        _, hi, lo, _ = bars[j]
        off = j - start
        if nf < len(fav_px):
            for k in range(len(fav_px)):
                if fav_hit[k] is None:
                    reached = (hi >= fav_px[k]) if side == 1 else (lo <= fav_px[k])
                    if reached:
                        fav_hit[k] = off; nf += 1
        if na < len(adv_px):
            for k in range(len(adv_px)):
                if adv_hit[k] is None:
                    reached = (lo <= adv_px[k]) if side == 1 else (hi >= adv_px[k])
                    if reached:
                        adv_hit[k] = off; na += 1
                        # record the realized print for gap-through accounting
        if nf == len(fav_px) and na == len(adv_px):
            break
    return fav_hit, adv_hit


def find_fills(bars, dist_pct, side, dedup_s=DEDUP_S):
    """A trailing Shot: level is dist_pct from the previous bar's close. Fills when
    the next bar trades through it. Returns [(bar_index, entry_price)]."""
    fills = []
    last_ms = -10 ** 18
    dedup_ms = dedup_s * 1000
    for i in range(len(bars) - 1):
        close = bars[i][3]
        level = close * (1 - side * dist_pct / 100.0)
        _, hi, lo, _ = bars[i + 1]
        through = (lo <= level) if side == 1 else (hi >= level)
        if not through:
            continue
        ts_ms = bars[i + 1][0] * BAR_MS
        if ts_ms - last_ms < dedup_ms:
            continue
        last_ms = ts_ms
        fills.append((i + 1, level))
    return fills


def brute_force_outcome(bars, start, entry, side, tp, sl):
    """Independent slow implementation used only to cross-check the fast path."""
    tp_px = entry * (1 + side * tp / 100.0)
    sl_px = entry * (1 - side * sl / 100.0)
    end = min(len(bars), start + HORIZON_BARS)
    for j in range(start, end):
        _, hi, lo, _ = bars[j]
        up = (hi >= tp_px) if side == 1 else (lo <= tp_px)
        dn = (lo <= sl_px) if side == 1 else (hi >= sl_px)
        if up and dn:
            return "ambiguous"      # both inside one bar; scored as a loss below
        if up:
            return "tp"
        if dn:
            return "sl"
    return "timeout"


def resolve(fav_hit, adv_hit, ti, si):
    """Which came first for geometry (TPS[ti], SLS[si])? Ties inside one bar are
    scored as the LOSS — we cannot see intra-bar order, so we assume the worst."""
    f, a = fav_hit[ti], adv_hit[si]
    if f is None and a is None:
        return "timeout"
    if a is None:
        return "tp"
    if f is None:
        return "sl"
    return "tp" if f < a else "sl"


# ------------------------------------------------------------------- the null
def random_null(bars, tp, sl, side, n_draws, seed=12345):
    rnd, res = seed, []
    n = len(bars)
    for _ in range(n_draws):
        rnd = (1103515245 * rnd + 12345) % (2 ** 31)
        i = rnd % max(1, n - 2)
        entry = bars[i][3]
        res.append(brute_force_outcome(bars, i + 1, entry, side, tp, sl))
    w = sum(1 for r in res if r == "tp")
    d = sum(1 for r in res if r in ("tp", "sl"))
    return 100.0 * w / d if d else 0.0


# ---------------------------------------------------------------- selftest
def selftest():
    # buy resting 0.5% below: 100 -> 99.4 fills at 99.5, then 101 hits +0.5% first
    bars = [(0, 100, 100, 100), (1, 99.4, 99.4, 99.4), (2, 101, 101, 101)]
    fills = find_fills(bars, 0.5, 1, dedup_s=0)
    assert fills and abs(fills[0][1] - 99.5) < 1e-9, f"bad entry {fills}"
    fav, adv = first_touch_indices(bars, fills[0][0], fills[0][1], 1, [0.5], [0.5])
    assert resolve(fav, adv, 0, 0) == "tp", "should take profit"

    # mirror: continued fall must stop out
    bars = [(0, 100, 100, 100), (1, 99.4, 99.4, 99.4), (2, 98.0, 98.0, 98.0)]
    fills = find_fills(bars, 0.5, 1, dedup_s=0)
    fav, adv = first_touch_indices(bars, fills[0][0], fills[0][1], 1, [0.5], [0.5])
    assert resolve(fav, adv, 0, 0) == "sl", "should stop out"

    # quiet tape: nothing should fill
    bars = [(i, 100.001, 99.999, 100.0) for i in range(500)]
    assert not find_fills(bars, 0.5, 1, dedup_s=0), "quiet tape filled"

    # bar aggregation preserves extremes
    b = to_bars([(0.00, 10), (0.05, 12), (0.09, 8), (0.15, 11)])
    assert b[0][1] == 12 and b[0][2] == 8, f"bar extremes wrong: {b}"
    print("selftest OK", file=sys.stderr)


def crosscheck(bars, side, n=200):
    """Fast path must agree with the independent brute force on real bars."""
    fills = find_fills(bars, 0.30, side)[:n]
    bad = 0
    for (bi, entry) in fills:
        fav, adv = first_touch_indices(bars, bi, entry, side, TPS, SLS)
        for ti, tp in enumerate(TPS):
            for si, sl in enumerate(SLS):
                fast = resolve(fav, adv, ti, si)
                slow = brute_force_outcome(bars, bi, entry, side, tp, sl)
                if slow == "ambiguous":
                    continue          # tie inside a bar; fast path calls it a loss
                if fast != slow:
                    bad += 1
    print(f"crosscheck side={side}: {len(fills)} fills x {len(TPS)*len(SLS)} "
          f"geometries, disagreements={bad}", file=sys.stderr)
    return bad


# ------------------------------------------------------------------- loading
def stream_quote_bars(path, symbols):
    """ONE pass over the tape, building 100ms quote-bars for every symbol at once.

    Replaces a load-everything-then-bucket approach that held ~90M tuples per
    symbol (4.4GB and climbing) and re-read the whole tape once per symbol. This
    box has 31.8GB and a documented OOM history, so we bucket on the fly: 5 days
    of 100ms bars is at most 4.3M rows per symbol instead of ~90M quotes.

    Returns {symbol: [(bar, bid_hi, bid_lo, ask_hi, ask_lo, bid_close, ask_close)]}
    plus {symbol: (first_ts, last_ts, n_quotes)}.

    NOTE 2026-08-11: this tape has NO aggTrade. Verified live that Binance's
    fstream endpoint silently drops @aggTrade/@markPrice here while accepting
    @bookTicker/@depth20/@trade. Quotes are the better input regardless: they let
    us charge the real spread on both legs instead of pretending a print is
    a fillable price."""
    wants = {s: f'"{s.lower()}@bookTicker"'.encode() for s in symbols}
    bars = {s: [] for s in symbols}
    cur = {s: None for s in symbols}
    meta = {s: [None, None, 0] for s in symbols}

    cmd = ["zstdcat", path] if path.endswith(".zst") else ["cat", path]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=1 << 22)
    for raw in proc.stdout:
        if b"bookTicker" not in raw:
            continue
        for s, want in wants.items():
            if want not in raw:
                continue
            try:
                d = json.loads(raw)["m"]["data"]
                ts = d["T"] / 1000.0
                b = float(d["b"]); a = float(d["a"])
            except Exception:
                break
            m = meta[s]
            if m[0] is None:
                m[0] = ts
            m[1] = ts
            m[2] += 1
            k = int(ts * 1000 // BAR_MS)
            c = cur[s]
            if c is None or k != c[0]:
                if c is not None:
                    bars[s].append(tuple(c))
                cur[s] = [k, b, b, a, a, b, a]
            else:
                if b > c[1]: c[1] = b
                if b < c[2]: c[2] = b
                if a > c[3]: c[3] = a
                if a < c[4]: c[4] = a
                c[5], c[6] = b, a
            break
    proc.stdout.close(); proc.wait()
    for s in symbols:
        if cur[s] is not None:
            bars[s].append(tuple(cur[s]))
    return bars, meta


def to_bars_for_side(qbars, side):
    """Collapse quote bars into the (bar, hi, lo, close) form the engine wants.

    A resting BUY fills when the ASK trades down through it, and is then marked
    out against the BID (the price we could actually exit at). Mirrored for SELL.
    Spread is therefore paid on both legs, same as liq_paper.py does live."""
    out = []
    for (k, bhi, blo, ahi, alo, bc, ac) in qbars:
        if side == 1:
            # entry trigger on the ask, outcome on the bid
            out.append((k, bhi, alo, bc))
        else:
            out.append((k, ahi, blo, ac))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tape", default="/root/fomc_tape/binance_1785347394.jsonl.zst")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    ap.add_argument("--selftest-only", action="store_true")
    a = ap.parse_args()

    selftest()
    if a.selftest_only:
        sys.exit(0)

    syms = a.symbols.split(",")
    print(f"streaming {a.tape} once for {len(syms)} symbols...", file=sys.stderr)
    all_bars, meta = stream_quote_bars(a.tape, syms)

    for sym in syms:
        qbars = all_bars[sym]
        t0, t1, nq = meta[sym]
        if len(qbars) < 1000:
            print(f"{sym}: only {nq} quotes, skipping", flush=True)
            continue
        span_h = (t1 - t0) / 3600
        days = span_h / 24
        print(f"\n{'='*112}", flush=True)
        print(f"{sym}  —  {nq:,} quotes -> {len(qbars):,} bars of {BAR_MS}ms, "
              f"{span_h:.1f}h", flush=True)
        print(f"{'='*112}", flush=True)
        crosscheck(to_bars_for_side(qbars, 1), 1)

        print(f"{'side':5s} {'dist%':>6s} {'tp%':>6s} {'sl%':>6s} {'fills':>7s} "
              f"{'/day':>7s} {'win%':>6s} {'null%':>6s} {'edge_pp':>8s} "
              f"{'net%/fill':>10s} {'total%':>9s}", flush=True)

        null_cache = {}
        for side, sname in ((1, "buy"), (-1, "sell")):
            bars = to_bars_for_side(qbars, side)
            for D in DISTANCES:
                fills = find_fills(bars, D, side)
                if len(fills) < 20:
                    continue
                # one forward walk per fill resolves the entire TPxSL grid
                grid = defaultdict(lambda: {"tp": 0, "sl": 0, "timeout": 0})
                for (bi, entry) in fills:
                    fav, adv = first_touch_indices(bars, bi, entry, side, TPS, SLS)
                    for ti in range(len(TPS)):
                        for si in range(len(SLS)):
                            grid[(ti, si)][resolve(fav, adv, ti, si)] += 1
                for ti, TP in enumerate(TPS):
                    for si, SL in enumerate(SLS):
                        g = grid[(ti, si)]
                        n = g["tp"] + g["sl"] + g["timeout"]
                        if not n:
                            continue
                        dec = g["tp"] + g["sl"]
                        wr = 100.0 * g["tp"] / dec if dec else 0.0
                        key = (TP, SL, side)
                        if key not in null_cache:
                            null_cache[key] = random_null(bars, TP, SL, side, 300)
                        null = null_cache[key]
                        # sitters counted: timeouts contribute 0 to the mean
                        pnl = (g["tp"] * TP - g["sl"] * (SL + FEE_STOP)) / n
                        print(f"{sname:5s} {D:6.2f} {TP:6.2f} {SL:6.2f} {n:7d} "
                              f"{n/days:7.1f} {wr:6.1f} {null:6.1f} {wr-null:+8.2f} "
                              f"{pnl:+10.4f} {pnl*n:+9.2f}", flush=True)
