#!/usr/bin/env python3
"""
fp_engine/cont_probe.py — CROSS-VENUE CONTINUATION probe (strategy B).

Question: after Binance makes a move, does the OTHER venue's price keep going that
way far enough to pay for crossing its spread twice? This is NOT stale-quote sniping
(that one is dead) — we pay the venue's current executable price and profit from what
happens next.

Honest execution, same convention as fp_v2/hardened_probe.py:
  long  = BUY at the venue ASK, then exit by SELLING into the venue BID
  short = SELL at the venue BID, then exit by BUYING the venue ASK
So the spread is paid on BOTH legs and lives entirely inside the win rate. Zero fees
(Lighter/Aster-maker world); a fee can be added later as a flat haircut.

First-passage per the law: from the entry price, for a ladder of adverse distances Y,
record the max favourable excursion X reached before the exit-side quote first
breached Y. No clock. Censored (unresolved at data end) flagged and dropped.

Threshold: SESSION-ADAPTIVE and causal (Steven, 2026-07-29 — activity varies hugely by
session: NY 09:30-16:00 ET wild, Asia mild, London wild, weekends different again).
A single day-fraction quantile would be estimated on busy hours and applied to quiet
ones, biasing it high. Instead the window is cut into BLOCK_MIN blocks and each block's
firing threshold is the quantile of |return| over the PRECEDING TRAIL_BLOCKS blocks only
— no future data, self-recalibrating to whatever session it's in, and exactly what a
live bot can compute. Results are also broken out by UTC hour so session dependence is
visible rather than averaged away.

Null: an equal number of random entries drawn from the judged part, same geometry,
same execution convention — this absorbs drift (in a falling tape random shorts win).

Input: leadlag_aster.csv-style rows  t_ms,venue,sym,bid,ask  (50ms change-sampled grid)
Usage:
  python3 cont_probe.py --selftest
  python3 cont_probe.py --split          # one pass, write per-symbol files
  python3 cont_probe.py --run [--min-rows 20000] [--jobs 3]
"""
import argparse, csv, os, sys
import numpy as np

SRC = "/root/hl_data/binance_all/leadlag_aster.csv"
SPLIT = "/root/fp_engine/ll_split"
OUT = "/root/fp_engine/cont_out"
GRID_MS = 50
LEAD, LAG = "bn", "as"

# candidate ladders (candidates, not choices — the data elects; see the law)
WINDOWS_MS = (250, 500, 1000)
DELAYS_MS = (0, 100, 500)
QS = (0.999,)
COOLDOWNS_MS = (10_000,)
BLOCK_MIN = 30            # session-adaptive threshold block length
TRAIL_BLOCKS = 4          # threshold from the trailing 2 hours, causal
MAX_HOLD_MS = 300_000     # forward scan cap; resolution rate reported per rung
MIN_RESOLVE = 0.70        # rungs resolving less than this are not reported


# ---------------- split ----------------

def split_source():
    os.makedirs(SPLIT, exist_ok=True)
    h = {}
    n = 0
    with open(SRC) as f:
        f.readline()
        for line in f:
            p1 = line.find(",")
            p2 = line.find(",", p1 + 1)
            p3 = line.find(",", p2 + 1)
            sym = line[p2 + 1:p3]
            fh = h.get(sym)
            if fh is None:
                if len(h) > 900:
                    continue
                fh = open(os.path.join(SPLIT, sym + ".csv"), "w")
                h[sym] = fh
            fh.write(line)
            n += 1
    for fh in h.values():
        fh.close()
    print("SPLIT %d rows into %d symbol files" % (n, len(h)), flush=True)


# ---------------- grid build ----------------

def load_sym(path):
    """-> (t0, n_ticks, bn_mid, as_bid, as_ask, as_fresh) on a common GRID_MS grid."""
    ts, ven, bid, ask = [], [], [], []
    with open(path) as f:
        for line in f:
            a = line.rstrip("\n").split(",")
            if len(a) != 5:
                continue
            try:
                b = float(a[3]); k = float(a[4])
            except ValueError:
                continue
            if b <= 0 or k < b:
                continue
            ts.append(int(a[0])); ven.append(a[1]); bid.append(b); ask.append(k)
    if not ts:
        return None
    ts = np.asarray(ts, dtype=np.int64)
    ven = np.asarray(ven)
    bid = np.asarray(bid); ask = np.asarray(ask)
    o = np.argsort(ts, kind="stable")
    ts, ven, bid, ask = ts[o], ven[o], bid[o], ask[o]
    t0 = int(ts[0]); t1 = int(ts[-1])
    nb = (t1 - t0) // GRID_MS + 1
    if nb < 10_000:
        return None

    def fill(mask, vals):
        """last value at-or-before each grid tick; NaN before the first."""
        out = np.full(nb, np.nan)
        if not mask.any():
            return out, np.full(nb, np.iinfo(np.int64).max)
        tt = ts[mask]; vv = vals[mask]
        idx = (tt - t0) // GRID_MS
        # last write wins within a tick
        out[idx] = vv
        stamp = np.full(nb, -1, dtype=np.int64)
        stamp[idx] = tt
        # forward fill
        valid = ~np.isnan(out)
        pos = np.where(valid, np.arange(nb), -1)
        pos = np.maximum.accumulate(pos)
        good = pos >= 0
        res = np.full(nb, np.nan)
        res[good] = out[pos[good]]
        st = np.full(nb, np.iinfo(np.int64).max, dtype=np.int64)
        gt = t0 + np.arange(nb) * GRID_MS
        st[good] = gt[good] - stamp[pos[good]]
        return res, st

    isb = ven == LEAD
    isa = ven == LAG
    bnb, _ = fill(isb, bid)
    bna, _ = fill(isb, ask)
    asb, age_b = fill(isa, bid)
    asa, age_a = fill(isa, ask)
    bn_mid = (bnb + bna) / 2.0
    fresh = np.maximum(age_b, age_a) <= 5_000     # venue quote not minutes-stale
    return t0, nb, bn_mid, asb, asa, fresh


# ---------------- first passage ----------------

def fp_profile(exit_series, entry_px, sign, rungs):
    """sign=+1 long (exit=bid, favourable=up), -1 short (exit=ask, favourable=down).
    Returns per rung (maxX_before_breach, resolved)."""
    # excursion 0 prepended at the entry instant: best-favourable floors at 0 and
    # no barrier can be breached before any quote has moved (matches labeler.py)
    rel = np.concatenate(([0.0], (exit_series / entry_px - 1.0) * sign))
    n = len(rel)
    out = np.empty((len(rungs), 2))
    if n == 0:
        out[:, 0] = 0.0; out[:, 1] = 0.0
        return out
    cmax = np.maximum.accumulate(rel)
    cmin = np.minimum.accumulate(rel)
    for k, y in enumerate(rungs):
        hit = cmin <= -y
        if hit.any():
            i = int(np.argmax(hit))
            out[k] = (cmax[i], 1.0)
        else:
            out[k] = (cmax[-1], 0.0)
    return out


def brute_fp(exit_series, entry_px, sign, rungs):
    """independent naive reimplementation for the cross-check gate"""
    res = []
    for y in rungs:
        mx = 0.0
        done = False
        for v in exit_series:
            r = (v / entry_px - 1.0) * sign
            if r > mx:
                mx = r
            if r <= -y:
                res.append((mx, 1.0)); done = True; break
        if not done:
            res.append((mx, 0.0))
    return np.asarray(res)


def selftest():
    # long entered at 100; bid path goes +0.3%, -0.15%, +0.6%, -0.4%
    bid = np.array([100.3, 99.85, 100.6, 99.6])
    rungs = np.array([0.001, 0.003])
    p = fp_profile(bid, 100.0, +1, rungs)
    assert abs(p[0][0] - 0.003) < 1e-9 and p[0][1] == 1.0, p
    assert abs(p[1][0] - 0.006) < 1e-9 and p[1][1] == 1.0, p
    p = fp_profile(bid, 100.0, +1, np.array([0.05]))
    assert p[0][1] == 0.0 and abs(p[0][0] - 0.006) < 1e-9
    # short entered at 100; ask path mirrored
    ask = np.array([99.7, 100.15, 99.4, 100.4])
    p = fp_profile(ask, 100.0, -1, rungs)
    assert abs(p[0][0] - 0.003) < 1e-9 and p[0][1] == 1.0, p
    rng = np.random.default_rng(0)
    for _ in range(30):
        s = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, 200)))
        sg = rng.choice([1, -1])
        r = np.array([0.0005, 0.002, 0.01])
        a = fp_profile(s, 100.0, sg, r)
        b = brute_fp(s, 100.0, sg, r)
        assert np.allclose(a, b, atol=1e-12), "CROSS-CHECK FAIL"
    print("SELFTEST PASS (first-passage profile + 30 random cross-checks)")


# ---------------- per-symbol run ----------------

def block_thresholds(absret, nb, blk_ticks, q):
    """Causal, session-adaptive threshold per tick: for each block, the q-quantile of
    |ret| over the preceding TRAIL_BLOCKS blocks. Early blocks -> inf (never fires)."""
    nblk = nb // blk_ticks + 1
    thr = np.full(nb, np.inf)
    per = np.full(nblk, np.nan)
    for b in range(nblk):
        s, e = b * blk_ticks, min((b + 1) * blk_ticks, nb)
        if e <= s:
            continue
        v = absret[s:e]
        v = v[np.isfinite(v)]
        if len(v) > 50:
            per[b] = np.quantile(v, q)
    for b in range(TRAIL_BLOCKS, nblk):
        hist = per[b - TRAIL_BLOCKS:b]
        hist = hist[np.isfinite(hist)]
        if len(hist) == 0:
            continue
        s, e = b * blk_ticks, min((b + 1) * blk_ticks, nb)
        thr[s:e] = float(np.mean(hist))
    return thr


def run_sym(path):
    g = load_sym(path)
    if g is None:
        return None
    t0, nb, bn_mid, asb, asa, fresh = g
    sym = os.path.basename(path)[:-4]
    max_hold_ticks = MAX_HOLD_MS // GRID_MS
    blk_ticks = BLOCK_MIN * 60_000 // GRID_MS
    mid_a = (asb + asa) / 2.0
    ok = np.isfinite(bn_mid) & np.isfinite(mid_a) & fresh
    if ok.sum() < 20_000:
        return None
    spread = np.nanmedian((asa - asb)[ok] / mid_a[ok])
    # rung ladder derived from the venue's own spread (floor) — geometric
    rungs = np.array([spread * (1.6 ** k) for k in range(8)])
    hour_utc = ((t0 + np.arange(nb) * GRID_MS) // 1000 % 86400) // 3600
    rows = []
    for W in WINDOWS_MS:
        w = max(1, W // GRID_MS)
        ret = np.full(nb, np.nan)
        ret[w:] = bn_mid[w:] / bn_mid[:-w] - 1.0
        if np.isfinite(ret).sum() < 20_000:
            continue
        for q in QS:
            thr_arr = block_thresholds(np.abs(ret), nb, blk_ticks, q)
            thr = float(np.median(thr_arr[np.isfinite(thr_arr)])) if np.isfinite(
                thr_arr).any() else np.nan
            if not np.isfinite(thr) or thr <= 0:
                continue
            cand = np.flatnonzero(np.isfinite(ret) & (np.abs(ret) >= thr_arr) & ok)
            if len(cand) < 30:
                continue
            for D in DELAYS_MS:
                d = D // GRID_MS
                for cd in COOLDOWNS_MS:
                    step = cd // GRID_MS
                    fired, last = [], -10 ** 9
                    for i in cand:
                        if i - last >= step:
                            fired.append(i); last = i
                    fired = np.asarray(fired)
                    ent = fired + d
                    ent = ent[(ent + 10 < nb) & ok[np.clip(ent, 0, nb - 1)]]
                    if len(ent) < 30:
                        continue
                    sgn = np.sign(ret[ent - d]).astype(int)
                    acc = np.zeros((len(rungs), 4))   # wins, resolved, seen, sumX
                    # per-session-bucket tallies: NY 13-21 UTC, London 07-13, Asia 21-07
                    sess = np.zeros((3, len(rungs), 2))
                    for j, i in enumerate(ent):
                        s = sgn[j]
                        if s == 0:
                            continue
                        px = asa[i] if s > 0 else asb[i]
                        ser = asb[i:i + max_hold_ticks] if s > 0 else asa[i:i + max_hold_ticks]
                        ser = ser[np.isfinite(ser)]
                        if len(ser) < 2 or not np.isfinite(px):
                            continue
                        h = int(hour_utc[i])
                        sb = 0 if 13 <= h < 21 else (1 if 7 <= h < 13 else 2)
                        p = fp_profile(ser, px, s, rungs)
                        for k, y in enumerate(rungs):
                            acc[k][2] += 1
                            if p[k][1] > 0 or p[k][0] >= y:
                                acc[k][1] += 1
                                acc[k][3] += p[k][0]
                                sess[sb][k][1] += 1
                                if p[k][0] >= y:
                                    acc[k][0] += 1
                                    sess[sb][k][0] += 1
                    # null: random entries, same count, judged region only
                    rng = np.random.default_rng(abs(hash((sym, W, q, D, cd))) % 2 ** 31)
                    pool = np.flatnonzero(ok)
                    # null pool: same eligibility window as the fired set (first
                    # TRAIL_BLOCKS blocks can never fire, so exclude them here too)
                    pool = pool[(pool >= TRAIL_BLOCKS * blk_ticks) &
                                (pool + max_hold_ticks // 10 < nb)]
                    if len(pool) < len(ent):
                        continue
                    rnd = rng.choice(pool, size=len(ent), replace=False)
                    rsg = rng.choice([1, -1], size=len(rnd))
                    nacc = np.zeros((len(rungs), 2))
                    for j, i in enumerate(rnd):
                        s = int(rsg[j])
                        px = asa[i] if s > 0 else asb[i]
                        ser = asb[i:i + max_hold_ticks] if s > 0 else asa[i:i + max_hold_ticks]
                        ser = ser[np.isfinite(ser)]
                        if len(ser) < 2 or not np.isfinite(px):
                            continue
                        p = fp_profile(ser, px, s, rungs)
                        for k, y in enumerate(rungs):
                            if p[k][1] > 0 or p[k][0] >= y:
                                nacc[k][1] += 1
                                if p[k][0] >= y:
                                    nacc[k][0] += 1
                    for k, y in enumerate(rungs):
                        if acc[k][1] < 30 or nacc[k][1] < 30 or acc[k][2] == 0:
                            continue
                        resolve = acc[k][1] / acc[k][2]
                        if resolve < MIN_RESOLVE:
                            continue
                        p_sig = acc[k][0] / acc[k][1]
                        p_null = nacc[k][0] / nacc[k][1]
                        # symmetric geometry X = Y = y, zero fees: EV = (2p-1)*y
                        ev = (2 * p_sig - 1) * y
                        ev_null = (2 * p_null - 1) * y
                        r = dict(
                            sym=sym, spread_pct=spread * 100, win_ms=W, q=q, delay_ms=D,
                            cooldown_ms=cd, thr_pct=thr * 100, geom_pct=y * 100,
                            geom_x_spread=y / spread if spread > 0 else np.nan,
                            n=int(acc[k][1]), resolve_pct=resolve * 100,
                            win=p_sig * 100, null_win=p_null * 100,
                            edge_pp=(p_sig - p_null) * 100, ev_pct=ev * 100,
                            ev_net_pct=(ev - ev_null) * 100)
                        for bi, bn in enumerate(("ny", "lon", "asia")):
                            tot = sess[bi][k][1]
                            r["n_" + bn] = int(tot)
                            r["win_" + bn] = (sess[bi][k][0] / tot * 100) if tot >= 20 else np.nan
                        rows.append(r)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--split", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--min-rows", type=int, default=20_000)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    selftest()
    if a.selftest:
        return
    if a.split:
        split_source()
        return
    if not a.run:
        return
    os.makedirs(OUT, exist_ok=True)
    files = []
    for fn in sorted(os.listdir(SPLIT)):
        p = os.path.join(SPLIT, fn)
        if os.path.getsize(p) > a.min_rows * 30:
            files.append(p)
    if a.limit:
        files = files[:a.limit]
    print("RUN over %d symbol files" % len(files), flush=True)
    allrows = []
    for i, p in enumerate(files):
        try:
            r = run_sym(p)
        except Exception as e:
            print("ERR %s %r" % (p, e), flush=True); continue
        if r:
            allrows.extend(r)
        if (i + 1) % 20 == 0:
            print("  %d/%d symbols, %d result rows" % (i + 1, len(files), len(allrows)),
                  flush=True)
    if not allrows:
        print("NO RESULTS"); return
    out = os.path.join(OUT, "cont_results.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(allrows[0].keys()))
        w.writeheader(); w.writerows(allrows)
    print("WROTE %s (%d rows)" % (out, len(allrows)), flush=True)
    print("CONT-PROBE COMPLETE", flush=True)


if __name__ == "__main__":
    main()
