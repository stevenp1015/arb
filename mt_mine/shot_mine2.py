#!/usr/bin/env python3
"""shot_mine2.py — mine MoonTrader Shot parameters from our own tape. CORRECTED.

WHY v2 EXISTS (read this before touching it)
v1 produced 22 fills in five days on ETH and ZERO on BTC and SOL. The bug was
conceptual, not arithmetic: v1 re-priced the resting order to exactly D% away on
every single 100ms bar, then asked whether the *next* bar traded through it. That
demands a D% move inside 100ms — a flash crash — so it essentially never filled.

A real Shot does the opposite. The order RESTS. It only moves when price leaves a
dead zone around it (the Buffer), and even then only after a delay. Price is meant
to drift toward the resting order over minutes and then get taken by a spike.
v1 implemented no Buffer, no Follow Price Delay and no Replace Delay at all —
which are precisely the parameters this whole exercise is supposed to derive.

THE STATE MACHINE (from MT_ALGO_PARAMS.md section 2)
  L        the resting order level
  anchor   the price at which L was last (re)placed
  Buffer   a corridor around anchor, HALF the width either side. While price
           stays inside it, L does not move.
  far boundary crossed  (price moved AWAY from L) -> after Follow Price Delay,
                        move L closer to price
  near boundary crossed (price moved TOWARD L)    -> after Replace Delay,
                        move L away from price
  An immediate reversal cancels a pending delay (docs are explicit about this).

Fill is checked BEFORE any repositioning inside a bar. Otherwise the order could
run away from a price that had already reached it — which would silently invent
free money.

FILL / EXIT MODEL
Entry is a resting limit, so we get our own price. A resting BUY is filled when
the ASK trades down through it, and is then marked out against the BID. Mirrored
for a SELL. Spread is therefore paid on both legs, same as liq_paper.py does live.

GATES
  - synthetic selftest with hand-checkable answers, including the cases v1 got wrong
  - independent brute-force cross-check, on a distance that actually fills
  - random-entry null from the same window
  - 60s episode dedup; sitters (timeouts) counted in every mean
"""
import json, sys, subprocess, argparse
from collections import defaultdict

# Shot "Distance" — how far the order rests from price, %
DISTANCES = [0.05, 0.10, 0.20, 0.30, 0.50]
# Shot "Buffer" — dead-zone width, % of price, split half either side
BUFFERS   = [0.02, 0.05, 0.10, 0.20]
TPS       = [0.02, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50]
SLS       = [0.05, 0.10, 0.20, 0.30, 0.50, 1.00]

BAR_MS       = 100
DEDUP_S      = 60
HORIZON_BARS = 36000      # 1 hour, matching the liq_paper timeout
FOLLOW_DELAY_BARS  = 5    # 0.5s; docs recommend 0.1-1s
REPLACE_DELAY_BARS = 5
FEE_STOP = 0.04           # a stop crosses the book (Aster taker); maker legs are 0


# --------------------------------------------------------------- the Shot itself
def simulate_shot(bars, dist_pct, buf_pct, side,
                  follow_delay=FOLLOW_DELAY_BARS, replace_delay=REPLACE_DELAY_BARS,
                  dedup_bars=DEDUP_S * 1000 // BAR_MS):
    """Walk the tape with ONE continuously-resting, trailing order.

    bars: (k, fav_extreme, adv_extreme, close) already oriented for `side` by
    to_bars_for_side. For a BUY: fav=bid high, adv=ask low, close=bid close.
    Returns [(bar_index, entry_price)].
    """
    n = len(bars)
    if n < 2:
        return []
    fills = []
    d = dist_pct / 100.0
    halfbuf = (buf_pct / 100.0) / 2.0

    anchor = bars[0][3]
    L = anchor * (1 - side * d)
    pending = 0          # >0 move-closer armed, <0 move-away armed
    pending_at = 0
    last_fill = -10 ** 9

    for i in range(1, n):
        _, fav, adv, close = bars[i]

        # 1. FILL FIRST. A resting buy is taken when the ask trades down to L.
        hit = (adv <= L) if side == 1 else (adv >= L)
        if hit and (i - last_fill) >= dedup_bars:
            fills.append((i, L))
            last_fill = i
            # re-arm at the new price, as auto-restart would
            anchor = close
            L = anchor * (1 - side * d)
            pending = 0
            continue

        # 2. Otherwise decide whether the order should move.
        #    For a BUY (order below price): price rising = moving AWAY from the
        #    order = "far boundary" = follow price. Price falling = approaching
        #    the order = "near boundary" = replace further away.
        moved_away = (close > anchor * (1 + halfbuf)) if side == 1 else \
                     (close < anchor * (1 - halfbuf))
        moved_toward = (close < anchor * (1 - halfbuf)) if side == 1 else \
                       (close > anchor * (1 + halfbuf))

        if moved_away:
            if pending <= 0:                 # reversal cancels the opposite timer
                pending, pending_at = 1, i
            elif i - pending_at >= follow_delay:
                anchor = close
                L = anchor * (1 - side * d)
                pending = 0
        elif moved_toward:
            if pending >= 0:
                pending, pending_at = -1, i
            elif i - pending_at >= replace_delay:
                anchor = close
                L = anchor * (1 - side * d)
                pending = 0
        else:
            pending = 0                      # back inside the dead zone

    return fills


# ------------------------------------------------------------- outcome resolution
def first_touch_indices(bars, start, entry, side, levels_fav, levels_adv):
    """ONE forward walk recording when each TP and each SL level is first reached."""
    fav_px = [entry * (1 + side * L / 100.0) for L in levels_fav]
    adv_px = [entry * (1 - side * L / 100.0) for L in levels_adv]
    fav_hit = [None] * len(levels_fav)
    adv_hit = [None] * len(levels_adv)
    nf = na = 0
    end = min(len(bars), start + HORIZON_BARS)
    for j in range(start, end):
        _, fav, adv, _ = bars[j]
        off = j - start
        if nf < len(fav_px):
            for k in range(len(fav_px)):
                if fav_hit[k] is None:
                    if (fav >= fav_px[k]) if side == 1 else (fav <= fav_px[k]):
                        fav_hit[k] = off; nf += 1
        if na < len(adv_px):
            for k in range(len(adv_px)):
                if adv_hit[k] is None:
                    if (adv <= adv_px[k]) if side == 1 else (adv >= adv_px[k]):
                        adv_hit[k] = off; na += 1
        if nf == len(fav_px) and na == len(adv_px):
            break
    return fav_hit, adv_hit


def resolve(fav_hit, adv_hit, ti, si):
    """Ties inside one bar are scored as the LOSS: we cannot see intra-bar order."""
    f, a = fav_hit[ti], adv_hit[si]
    if f is None and a is None:
        return "timeout"
    if a is None:
        return "tp"
    if f is None:
        return "sl"
    return "tp" if f < a else "sl"


def brute_force_outcome(bars, start, entry, side, tp, sl):
    """Independent slow path, used only to cross-check resolve()."""
    tp_px = entry * (1 + side * tp / 100.0)
    sl_px = entry * (1 - side * sl / 100.0)
    for j in range(start, min(len(bars), start + HORIZON_BARS)):
        _, fav, adv, _ = bars[j]
        up = (fav >= tp_px) if side == 1 else (fav <= tp_px)
        dn = (adv <= sl_px) if side == 1 else (adv >= sl_px)
        if up and dn:
            return "ambiguous"
        if up:
            return "tp"
        if dn:
            return "sl"
    return "timeout"


def random_null(bars, tp, sl, side, n_draws, seed=12345):
    rnd, res = seed, []
    n = len(bars)
    for _ in range(n_draws):
        rnd = (1103515245 * rnd + 12345) % (2 ** 31)
        i = rnd % max(1, n - 2)
        res.append(brute_force_outcome(bars, i + 1, bars[i][3], side, tp, sl))
    w = sum(1 for r in res if r == "tp")
    d = sum(1 for r in res if r in ("tp", "sl"))
    return 100.0 * w / d if d else 0.0


# ------------------------------------------------------------------- data layer
def stream_quote_bars(path, symbols):
    """ONE pass, bucketing every symbol into 100ms quote-bars on the fly.

    This box has 31.8GB and a documented OOM history; holding ~100M quote tuples
    per symbol hit 4.4GB and climbing, so we bucket during the read instead.

    NOTE: this tape has NO trade stream. Verified live that Binance's fstream
    endpoint silently drops @aggTrade/@markPrice here while accepting
    @bookTicker/@depth20/@trade. See MT_ALGO_PARAMS.md section 7.
    """
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
            m[1] = ts; m[2] += 1
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
    """(k, bid_hi, bid_lo, ask_hi, ask_lo, bid_close, ask_close)
       -> (k, favourable_extreme, adverse_extreme, close)

    BUY:  filled when the ASK comes down to us; marked out on the BID.
    SELL: filled when the BID comes up to us; marked out on the ASK.
    Spread paid on both legs."""
    if side == 1:
        return [(k, bhi, alo, bc) for (k, bhi, blo, ahi, alo, bc, ac) in qbars]
    return [(k, blo, ahi, ac) for (k, bhi, blo, ahi, alo, bc, ac) in qbars]


# ---------------------------------------------------------------------- selftest
def _mk(prices, spread=0.0):
    """Build side-oriented bars from a plain price path."""
    return [(i, p + spread, p - spread, p) for i, p in enumerate(prices)]


def selftest():
    # 1. THE CASE v1 GOT WRONG. Price drifts down gently to the resting level.
    #    A buy resting 1% below 100 sits at 99. A slow walk to 98.9 must FILL,
    #    even though no single bar moves anything like 1%.
    path = [100.0] + [100.0 - 0.05 * i for i in range(1, 30)]
    f = simulate_shot(_mk(path), dist_pct=1.0, buf_pct=100.0, side=1)
    assert f, "v1's bug: a gradual drift onto a resting order must fill"
    assert abs(f[0][1] - 99.0) < 1e-9, f"should fill at 99.0, got {f[0][1]}"

    # 2. A flat tape must never fill.
    assert not simulate_shot(_mk([100.0] * 500), 1.0, 0.05, 1), "flat tape filled"

    # 3. A GENTLE DRIFT DOWN MUST NOT FILL when the buffer is tight. This is the
    #    real Shot behaviour and it is counter-intuitive: for a buy resting BELOW
    #    price, a falling price is "approaching the order", so Replace Delay walks
    #    the order DOWN ahead of it. Slow money never catches it. Only a move that
    #    outruns the delay does. This is precisely why the algorithm is called a
    #    Shot and why it profits from spikes rather than trends.
    drift = [100.0 - 0.02 * i for i in range(200)]
    assert not simulate_shot(_mk(drift), 1.0, 0.02, 1), \
        "a slow drift must NOT fill a fleeing order"

    # 4. The order must TRAIL upward, then be taken by a spike that outruns it.
    #    Climb 100 -> 110 (a 1% buy order should trail up to roughly 108), then
    #    drop hard to 107 in one bar. It must fill well above 100, which proves
    #    the level moved rather than staying pinned at its original 99.
    up = [100.0 + 0.5 * i for i in range(21)]           # 100 -> 110
    crash = [107.0, 105.0, 103.0, 101.0, 99.0, 97.0]
    f = simulate_shot(_mk(up + crash), dist_pct=1.0, buf_pct=0.05, side=1)
    assert f, "order failed to trail price upward"
    assert f[0][1] > 105.0, f"trailed order should rest above 105, got {f[0][1]}"

    # 5. Buffer must actually suppress movement. With a huge buffer the level
    #    never moves, so the identical path fills far lower — at the original 99.
    f_wide = simulate_shot(_mk(up + crash), dist_pct=1.0, buf_pct=100.0, side=1)
    assert f_wide, "pinned order should still fill when price reaches it"
    assert abs(f_wide[0][1] - 99.0) < 1e-9, f"pinned level should be 99, got {f_wide[0][1]}"
    assert f_wide[0][1] < f[0][1], "wide buffer must pin the level lower"

    # 6. Mirror side: a sell rests above, pinned by a wide buffer, fills on a drift up.
    path = [100.0] + [100.0 + 0.05 * i for i in range(1, 30)]
    f = simulate_shot(_mk(path), dist_pct=1.0, buf_pct=100.0, side=-1)
    assert f and abs(f[0][1] - 101.0) < 1e-9, f"sell should fill at 101, got {f}"

    # 7. Outcome resolution, hand-checkable.
    bars = _mk([100.0, 99.0, 101.0])
    fav, adv = first_touch_indices(bars, 1, 100.0, 1, [0.5], [0.5])
    assert resolve(fav, adv, 0, 0) == "sl", "down first must be a stop"
    bars = _mk([100.0, 100.6, 99.0])
    fav, adv = first_touch_indices(bars, 1, 100.0, 1, [0.5], [0.5])
    assert resolve(fav, adv, 0, 0) == "tp", "up first must be a take-profit"

    print("selftest OK (7 cases, incl. the drift case v1 failed)", file=sys.stderr)


def crosscheck(bars, side, fills, label):
    bad = checked = 0
    for (bi, entry) in fills[:150]:
        fav, adv = first_touch_indices(bars, bi, entry, side, TPS, SLS)
        for ti, tp in enumerate(TPS):
            for si, sl in enumerate(SLS):
                slow = brute_force_outcome(bars, bi, entry, side, tp, sl)
                if slow == "ambiguous":
                    continue
                checked += 1
                if resolve(fav, adv, ti, si) != slow:
                    bad += 1
    print(f"crosscheck {label}: {checked} comparisons, disagreements={bad}",
          flush=True)
    return bad


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
        days = (t1 - t0) / 86400
        print(f"\n{'='*118}\n{sym}  —  {nq:,} quotes -> {len(qbars):,} bars "
              f"of {BAR_MS}ms, {days*24:.1f}h\n{'='*118}", flush=True)

        did_crosscheck = False
        print(f"{'side':5s} {'dist%':>6s} {'buf%':>6s} {'tp%':>6s} {'sl%':>6s} "
              f"{'fills':>7s} {'/day':>7s} {'win%':>6s} {'null%':>6s} "
              f"{'edge_pp':>8s} {'net%/fill':>10s} {'total%':>9s}", flush=True)

        null_cache = {}
        for side, sname in ((1, "buy"), (-1, "sell")):
            bars = to_bars_for_side(qbars, side)
            for D in DISTANCES:
                for B in BUFFERS:
                    fills = simulate_shot(bars, D, B, side)
                    if len(fills) < 30:
                        continue
                    if not did_crosscheck:
                        crosscheck(bars, side, fills, f"{sname} d={D} b={B}")
                        did_crosscheck = True
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
                            dec = g["tp"] + g["sl"]
                            if not n or not dec:
                                continue
                            wr = 100.0 * g["tp"] / dec
                            key = (TP, SL, side)
                            if key not in null_cache:
                                null_cache[key] = random_null(bars, TP, SL, side, 300)
                            pnl = (g["tp"] * TP - g["sl"] * (SL + FEE_STOP)) / n
                            print(f"{sname:5s} {D:6.2f} {B:6.2f} {TP:6.2f} {SL:6.2f} "
                                  f"{n:7d} {n/days:7.1f} {wr:6.1f} {null_cache[key]:6.1f} "
                                  f"{wr-null_cache[key]:+8.2f} {pnl:+10.4f} "
                                  f"{pnl*n:+9.2f}", flush=True)
