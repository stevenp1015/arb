#!/usr/bin/env python3
"""
fp_engine/featurizer.py — the shelves (List A measurements at entry moments).

For each entry timestamp (taken from the labeler's output so rows join 1:1),
compute every measurement family available on this tape:

  F1 price motion   — return over each ladder window
  F2 aggression     — aggressor imbalance, trade-rate ratio, max-print ratio per window
  F3 book posture   — depth/imbalance at k=1,5,20; spread + its day-percentile;
                      wall size/distance; book-update rate per window
  F6 clock/regime   — hour, day-range-so-far, trailing-1h volatility

  (F4 cross-venue needs a day where Binance capture overlaps — Jun 11-30 only.
   F5 forced-flow needs the liq feed — exists only from Jul 27 on. Declared, not faked.)

Window ladder is DERIVED: floor/ceiling from the label file's own resolution-time
distribution (how fast the game actually resolves on this tape), geometric ratio 2.
Windows are (t-w, t] — a print at exactly t is known at t; nothing after t is touched.

Gates: synthetic selftest of window sums (the off-by-one graveyard) and a
mid-consistency check — the mid this script sees at each entry must equal the
labeler's recorded mid exactly, proving both index the tape identically.
"""
import argparse, csv, json, os
import numpy as np
from labeler import load_bbo, fmt_ts


# ---------------- tape loading ----------------

def load_trades(day_dir, coin):
    tag = '"c":"%s"' % coin
    ts, notion, isbuy = [], [], []
    for fn in sorted(os.listdir(day_dir)):
        if not fn.startswith("trades_shard"):
            continue
        with open(os.path.join(day_dir, fn)) as f:
            for line in f:
                if tag not in line:
                    continue
                o = json.loads(line)
                ts.append(o["t"]); notion.append(o["px"] * o["sz"])
                isbuy.append(1.0 if o["side"] == "B" else 0.0)
    ts = np.asarray(ts, dtype=np.int64)
    order = np.argsort(ts, kind="stable")
    return ts[order], np.asarray(notion)[order], np.asarray(isbuy)[order]


def load_book(day_dir, coin):
    """Stream book snapshots -> per-snapshot scalars (never keeps raw levels)."""
    tag = '"c":"%s"' % coin
    cols = {k: [] for k in ("ts", "spread", "d1b", "d1a", "d5b", "d5a", "d20b",
                            "d20a", "wallb", "walla", "wdistb", "wdista")}
    for fn in sorted(os.listdir(day_dir)):
        if not fn.startswith("book_shard"):
            continue
        with open(os.path.join(day_dir, fn)) as f:
            for line in f:
                if tag not in line:
                    continue
                o = json.loads(line)
                b, a = o.get("b"), o.get("a")
                if not b or not a or b[0][0] <= 0 or a[0][0] < b[0][0]:
                    continue
                mid = (b[0][0] + a[0][0]) / 2.0
                bn = [p * s for p, s in b[:20]]
                an = [p * s for p, s in a[:20]]
                wb, wa = int(np.argmax(bn)), int(np.argmax(an))
                cols["ts"].append(o["t"])
                cols["spread"].append((a[0][0] - b[0][0]) / mid)
                cols["d1b"].append(bn[0]); cols["d1a"].append(an[0])
                cols["d5b"].append(sum(bn[:5])); cols["d5a"].append(sum(an[:5]))
                cols["d20b"].append(sum(bn)); cols["d20a"].append(sum(an))
                med_b = float(np.median(bn)); med_a = float(np.median(an))
                cols["wallb"].append(bn[wb] / med_b if med_b > 0 else np.nan)
                cols["walla"].append(an[wa] / med_a if med_a > 0 else np.nan)
                cols["wdistb"].append((mid - b[wb][0]) / mid)
                cols["wdista"].append((a[wa][0] - mid) / mid)
    ts = np.asarray(cols["ts"], dtype=np.int64)
    order = np.argsort(ts, kind="stable")
    out = {k: np.asarray(v)[order] for k, v in cols.items()}
    out["ts"] = ts[order]
    return out


# ---------------- windowed machinery (the off-by-one graveyard) ----------------

def win_sum(ts, csum, t0, t1):
    """Sum of values with timestamp in (t0, t1]. csum[i] = sum of first i values."""
    lo = np.searchsorted(ts, t0, side="right")
    hi = np.searchsorted(ts, t1, side="right")
    return csum[hi] - csum[lo], lo, hi


def selftest():
    ts = np.array([1000, 2000, 3000], dtype=np.int64)
    v = np.array([10.0, 20.0, 30.0])
    cs = np.concatenate(([0.0], np.cumsum(v)))
    s, lo, hi = win_sum(ts, cs, 3200 - 1500, 3200)   # (1700, 3200] -> 20+30
    assert s == 50.0 and (hi - lo) == 2
    s, lo, hi = win_sum(ts, cs, 3000 - 1000, 3000)   # (2000, 3000] -> 30 only (t-w excl)
    assert s == 30.0 and (hi - lo) == 1
    s, lo, hi = win_sum(ts, cs, 900, 1000)           # (900,1000] -> print AT t included
    assert s == 10.0
    s, lo, hi = win_sum(ts, cs, 3000, 4000)          # (3000,4000] -> nothing (no lookahead
    assert s == 0.0                                   # past last print, empty is empty)
    print("SELFTEST PASS (window boundary semantics)")


def derive_windows(label_rows, ratio=2.0, min_rate=0.8):
    """Ladder anchors from the labels' own resolution times, restricted to rungs
    that actually RESOLVE on this slice (>= min_rate of entries, both directions
    pooled). Rungs that mostly censor within the slice can't be played on it, so
    their timescales don't get to set feature windows. Rates printed for audit."""
    tags = sorted({c.split("@")[1] for c in label_rows[0] if "_dt@" in c}, key=float)
    usable = []
    print("rung resolution rates (usable >= %.0f%%):" % (min_rate * 100))
    for tag in tags:
        ok = [int(r[d + "_ok@" + tag]) for r in label_rows for d in ("L", "S")]
        dts = [int(r[d + "_dt@" + tag]) for r in label_rows for d in ("L", "S")
               if int(r[d + "_dt@" + tag]) > 0]
        rate = sum(ok) / len(ok)
        mark = "USE" if (rate >= min_rate and dts) else "  -"
        print("  %s  %s%%: %.0f%% resolve" % (mark, tag, rate * 100))
        if rate >= min_rate and dts:
            usable.append(dts)
    if not usable:
        raise SystemExit("no rung resolves >= %.0f%% on this slice" % (min_rate * 100))
    floor = max(200, int(np.percentile(usable[0], 25)))
    ceiling = int(np.percentile(usable[-1], 90))
    w = [floor]
    while w[-1] * ratio < ceiling:
        w.append(int(w[-1] * ratio))
    return w, floor, ceiling


# ---------------- features ----------------

def build(day_dir, coin, labels_csv, out_dir):
    label_rows = list(csv.DictReader(open(labels_csv)))
    entries = np.array([int(r["ts_ms"]) for r in label_rows], dtype=np.int64)
    lab_mid = np.array([float(r["mid"]) for r in label_rows])

    windows, wfloor, wceil = derive_windows(label_rows)
    print("WINDOW LADDER (derived from label resolution times): floor %dms  "
          "ceiling %dms (p90 slow-rung)  rungs: %s"
          % (wfloor, wceil, ", ".join("%.1fs" % (w / 1000) for w in windows)))

    print("loading tape ...")
    ts_m, mid, spread_pct, tick_pct = load_bbo(day_dir, coin)
    ts_t, notion, isbuy = load_trades(day_dir, coin)
    book = load_book(day_dir, coin)
    print("  %d mids, %d trades, %d book snaps" % (len(mid), len(ts_t), len(book["ts"])))

    # GATE: the mid this script sees at each entry must equal the labeler's mid.
    pos = np.searchsorted(ts_m, entries, side="right") - 1
    assert (pos >= 0).all()
    if not np.allclose(mid[pos], lab_mid, rtol=0, atol=1e-12):
        raise SystemExit("MID-CONSISTENCY FAIL: featurizer and labeler disagree on the tape")
    print("MID-CONSISTENCY PASS (featurizer sees the labeler's exact mids)")

    cs_not = np.concatenate(([0.0], np.cumsum(notion)))
    cs_buy = np.concatenate(([0.0], np.cumsum(notion * isbuy)))
    cs_one = np.concatenate(([0.0], np.cumsum(np.ones_like(notion))))
    cs_book = np.concatenate(([0.0], np.cumsum(np.ones_like(book["ts"], dtype=float))))
    day_med_print = float(np.median(notion)) if len(notion) else np.nan
    day_rate = len(notion) / max(1.0, (ts_t[-1] - ts_t[0]) / 1000) if len(notion) else np.nan
    day_book_rate = len(book["ts"]) / max(1.0, (book["ts"][-1] - book["ts"][0]) / 1000)
    sorted_spread = np.sort(book["spread"])
    run_max = np.maximum.accumulate(mid); run_min = np.minimum.accumulate(mid)

    # per-second grid for trailing-1h vol
    t0g = int(ts_m[0] // 1000); t1g = int(ts_m[-1] // 1000)
    grid_ts = np.arange(t0g, t1g + 1, dtype=np.int64) * 1000
    gpos = np.searchsorted(ts_m, grid_ts + 999, side="right") - 1
    gmid = mid[np.clip(gpos, 0, None)]
    gret = np.diff(np.log(gmid), prepend=np.log(gmid[0]))
    cs_r = np.concatenate(([0.0], np.cumsum(gret)))
    cs_r2 = np.concatenate(([0.0], np.cumsum(gret ** 2)))

    rows = []
    for e_i, t in enumerate(entries):
        f = {"ts_ms": int(t)}
        i = int(np.searchsorted(ts_m, t, side="right") - 1)
        m0 = mid[i]
        # F1 price motion
        for w in windows:
            j = int(np.searchsorted(ts_m, t - w, side="right") - 1)
            f["ret_%d" % w] = (m0 / mid[j] - 1.0) if j >= 0 else np.nan
        # F2 aggression
        for w in windows:
            tot, lo, hi = win_sum(ts_t, cs_not, t - w, t)
            buy = cs_buy[hi] - cs_buy[lo]
            n = cs_one[hi] - cs_one[lo]
            f["aimb_%d" % w] = (2 * buy - tot) / tot if tot > 0 else np.nan
            f["trate_%d" % w] = (n / (w / 1000)) / day_rate if day_rate else np.nan
            f["maxprint_%d" % w] = (float(notion[lo:hi].max()) / day_med_print
                                    if hi > lo else np.nan)
        # F3 book posture (latest snapshot at-or-before t)
        k = int(np.searchsorted(book["ts"], t, side="right") - 1)
        if k >= 0:
            for k1, k2, name in (("d1b", "d1a", "bimb1"), ("d5b", "d5a", "bimb5"),
                                 ("d20b", "d20a", "bimb20")):
                b, a = book[k1][k], book[k2][k]
                f[name] = (b - a) / (b + a) if (b + a) > 0 else np.nan
            f["spread"] = book["spread"][k]
            f["spread_pctl"] = float(np.searchsorted(sorted_spread, book["spread"][k])
                                     ) / len(sorted_spread)
            f["depth5_usd"] = book["d5b"][k] + book["d5a"][k]
            for c in ("wallb", "walla", "wdistb", "wdista"):
                f[c] = book[c][k]
        else:
            for c in ("bimb1", "bimb5", "bimb20", "spread", "spread_pctl",
                      "depth5_usd", "wallb", "walla", "wdistb", "wdista"):
                f[c] = np.nan
        for w in windows:
            n, _, _ = win_sum(book["ts"], cs_book, t - w, t)
            f["brate_%d" % w] = (n / (w / 1000)) / day_book_rate
        # F6 clock/regime
        f["hour_utc"] = ((t // 1000) % 86400) / 3600.0
        f["range_so_far"] = (run_max[i] - run_min[i]) / run_min[i]
        g = int((t // 1000) - t0g)
        if g >= 3600:
            mu = (cs_r[g + 1] - cs_r[g + 1 - 3600]) / 3600
            var = (cs_r2[g + 1] - cs_r2[g + 1 - 3600]) / 3600 - mu * mu
            f["vol_1h"] = float(np.sqrt(max(var, 0.0)))
        else:
            f["vol_1h"] = np.nan
        rows.append(f)

    os.makedirs(out_dir, exist_ok=True)
    feat_cols = list(rows[0].keys())
    out_feat = os.path.join(out_dir, "%s_features.csv" % coin)
    with open(out_feat, "w", newline="") as fo:
        w = csv.DictWriter(fo, fieldnames=feat_cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if isinstance(v, float) and np.isnan(v) else v)
                        for k, v in r.items()})
    # merged matrix: features + labels joined on ts_ms (order identical by construction)
    out_mat = os.path.join(out_dir, "%s_matrix.csv" % coin)
    lab_cols = [c for c in label_rows[0] if c != "ts_ms"]
    with open(out_mat, "w", newline="") as fo:
        w = csv.writer(fo)
        w.writerow(feat_cols + lab_cols)
        for fr, lr in zip(rows, label_rows):
            w.writerow([fr[c] if not (isinstance(fr[c], float) and np.isnan(fr[c]))
                        else "" for c in feat_cols] + [lr[c] for c in lab_cols])
    nan_pct = {c: 100.0 * sum(1 for r in rows if isinstance(r[c], float) and
               np.isnan(r[c])) / len(rows) for c in feat_cols}
    worst = sorted(nan_pct.items(), key=lambda kv: -kv[1])[:5]
    print("WROTE %s and %s  (%d rows x %d features)"
          % (out_feat, out_mat, len(rows), len(feat_cols) - 1))
    print("highest NaN rates: " + ", ".join("%s %.0f%%" % kv for kv in worst))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--day"); ap.add_argument("--coin")
    ap.add_argument("--labels"); ap.add_argument("--out", default="out")
    a = ap.parse_args()
    selftest()
    if a.selftest:
        return
    build(a.day, a.coin, a.labels, a.out)


if __name__ == "__main__":
    main()
