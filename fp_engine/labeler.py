#!/usr/bin/env python3
"""
fp_engine/labeler.py — the first-passage labeler (the judge).

For each entry moment: from entry mid, for a ladder of adverse levels Y,
report the maximum favorable excursion X reached BEFORE the adverse side
first breached Y. Both directions (long profile and short profile). No clock:
scan runs to resolution or end-of-data (censored, flagged).

Rung anchors are DERIVED from the tape and printed (constitution: candidates
from data limits, never chosen numbers):
  floor   = max(median spread pct, 2 * tick pct)   -- can't measure moves inside quote noise
  ceiling = the slice's full range pct              -- nothing bigger is observable here
  ladder  = geometric steps (ratio 1.5) floor -> ceiling

Verification built in and mandatory:
  1. synthetic hand-checkable series with hardcoded expected answers
  2. independent brute-force reimplementation compared on random entries

Usage:
  python3 labeler.py --selftest
  python3 labeler.py --day <aster live_ dir> --coin HYPEUSDT --entries 2000 \
      --out /root/fp_engine/out
"""
import argparse, json, os, sys
import numpy as np

CHUNK = 100_000


# ---------------- tape loading (Aster/Binance capture jsonl) ----------------

def load_bbo(day_dir, coin):
    """Return (ts_ms, mid, med_spread_pct, tick_pct) for one coin from bbo shards."""
    tag = '"c":"%s"' % coin
    ts, bid, ask = [], [], []
    for fn in sorted(os.listdir(day_dir)):
        if not fn.startswith("bbo_shard"):
            continue
        with open(os.path.join(day_dir, fn)) as f:
            for line in f:
                if tag not in line:
                    continue
                o = json.loads(line)
                b, a = o["bpx"], o["apx"]
                if b <= 0 or a <= 0 or a < b:
                    continue
                ts.append(o["t"]); bid.append(b); ask.append(a)
    if not ts:
        raise SystemExit("no rows for %s in %s" % (coin, day_dir))
    ts = np.asarray(ts, dtype=np.int64)
    bid = np.asarray(bid); ask = np.asarray(ask)
    order = np.argsort(ts, kind="stable")
    ts, bid, ask = ts[order], bid[order], ask[order]
    mid = (bid + ask) / 2.0
    spread_pct = float(np.median((ask - bid) / mid))
    ubid = np.unique(bid)
    d = np.diff(ubid)
    tick = float(d[d > 0].min()) if (d > 0).any() else 0.0
    tick_pct = tick / float(np.median(mid)) if tick else 0.0
    # keep only rows where mid changes (identical mids can't change any label)
    keep = np.ones(len(mid), dtype=bool)
    keep[1:] = mid[1:] != mid[:-1]
    return ts[keep], mid[keep], spread_pct, tick_pct


def derive_rungs(mid, spread_pct, tick_pct, ratio=1.5):
    floor = max(spread_pct, 2.0 * tick_pct)
    ceiling = float((mid.max() - mid.min()) / mid.min())
    if ceiling <= floor:
        raise SystemExit("slice range %.5f%% <= floor %.5f%%: nothing measurable"
                         % (ceiling * 100, floor * 100))
    rungs = [floor]
    while rungs[-1] * ratio < ceiling:
        rungs.append(rungs[-1] * ratio)
    return np.asarray(rungs), floor, ceiling


# ---------------- the engine ----------------

def profile_one_direction(rel, ts_fwd, rungs):
    """rel: forward relative series where NEGATIVE is adverse, POSITIVE favorable.
    Returns per rung: (maxX_before_breach, breach_dt_ms, resolved) and the
    final (max, min) seen, for censoring info."""
    n = len(rel)
    out = [None] * len(rungs)
    unresolved = list(range(len(rungs)))
    carry_max, carry_min = -np.inf, np.inf
    pos = 0
    while pos < n and unresolved:
        chunk = rel[pos:pos + CHUNK]
        cmax = np.maximum.accumulate(chunk)
        cmin = np.minimum.accumulate(chunk)
        if carry_max > -np.inf:
            cmax = np.maximum(cmax, carry_max)
            cmin = np.minimum(cmin, carry_min)
        still = []
        for k in unresolved:
            hit = cmin <= -rungs[k]
            if hit.any():
                i = int(np.argmax(hit))
                out[k] = (float(cmax[i]), int(ts_fwd[pos + i]), True)
            else:
                still.append(k)
        unresolved = still
        carry_max = float(cmax[-1]); carry_min = float(cmin[-1])
        pos += CHUNK
    for k in unresolved:  # censored: never breached before data end
        out[k] = (carry_max if carry_max > -np.inf else 0.0, -1, False)
    return out


def label_entry(mid, ts, i, rungs):
    """Both directions for entry index i. Long: favorable=up. Short: favorable=down.
    Entry point itself is prepended at excursion 0 so best-favorable floors at 0."""
    if i + 1 >= len(mid):
        return None
    rel = np.concatenate(([0.0], mid[i + 1:] / mid[i] - 1.0))
    ts_fwd = np.concatenate(([ts[i]], ts[i + 1:]))
    long_p = profile_one_direction(rel, ts_fwd, rungs)
    short_p = profile_one_direction(-rel, ts_fwd, rungs)
    return long_p, short_p


def brute_force_entry(mid, ts, i, rungs):
    """Independent naive reimplementation (pure python, no numpy logic shared)."""
    rungs = list(rungs)
    res = {"L": [None] * len(rungs), "S": [None] * len(rungs)}
    mx = mn = 0.0
    unresolved_L = set(range(len(rungs)))
    unresolved_S = set(range(len(rungs)))
    e = mid[i]
    for j in range(i + 1, len(mid)):
        r = mid[j] / e - 1.0
        mx = max(mx, r); mn = min(mn, r)
        for k in sorted(unresolved_L):
            if mn <= -rungs[k]:
                res["L"][k] = (mx, int(ts[j]), True); unresolved_L.discard(k)
        for k in sorted(unresolved_S):
            if mx >= rungs[k]:
                res["S"][k] = (-mn, int(ts[j]), True); unresolved_S.discard(k)
        if not unresolved_L and not unresolved_S:
            break
    for k in unresolved_L:
        res["L"][k] = (mx, -1, False)
    for k in unresolved_S:
        res["S"][k] = (-mn, -1, False)
    return res


# ---------------- verification ----------------

def selftest():
    # Hand-checkable series, moves clearly beyond rungs (float-safe margins).
    # Entry at index 0, mid 100. path: +0.25%, -0.12%, +0.55%, -0.65%, +1.0%
    mid = np.array([100.0, 100.25, 99.88, 100.55, 99.35, 101.0])
    ts = np.arange(6, dtype=np.int64) * 1000
    rungs = np.array([0.001, 0.005])  # Y = 0.1%, 0.5%
    long_p, short_p = label_entry(mid, ts, 0, rungs)
    # LONG: before -0.1% (hit at t=2000, mid 99.88) best was +0.25%
    #       before -0.5% (hit at t=4000, mid 99.35) best was +0.55%
    assert abs(long_p[0][0] - 0.0025) < 1e-9 and long_p[0][1] == 2000 and long_p[0][2]
    assert abs(long_p[1][0] - 0.0055) < 1e-9 and long_p[1][1] == 4000 and long_p[1][2]
    # SHORT: adverse=up. before +0.1% (hit at t=1000, +0.25%) best down was 0.0%
    #        before +0.5% (hit at t=3000, +0.55%) best down was 0.12%
    assert abs(short_p[0][0] - 0.0) < 1e-9 and short_p[0][1] == 1000 and short_p[0][2]
    assert abs(short_p[1][0] - 0.0012) < 1e-9 and short_p[1][1] == 3000 and short_p[1][2]
    # censoring: rung larger than any move ever seen
    lp, sp = label_entry(mid, ts, 0, np.array([0.5]))
    assert lp[0][2] is False and abs(lp[0][0] - 0.01) < 1e-9
    print("SELFTEST PASS (hand-checked synthetic series, incl. censoring)")


def cross_check(mid, ts, rungs, n_checks, rng):
    idx = rng.choice(np.arange(0, len(mid) - 2), size=min(n_checks, len(mid) - 2),
                     replace=False)
    for i in idx:
        got = label_entry(mid, ts, int(i), rungs)
        want = brute_force_entry(mid, ts, int(i), rungs)
        for k in range(len(rungs)):
            for g, w in ((got[0][k], want["L"][k]), (got[1][k], want["S"][k])):
                if g[2] != w[2] or g[1] != w[1] or abs(g[0] - w[0]) > 1e-12:
                    raise SystemExit("CROSS-CHECK FAIL at entry %d rung %d: %s vs %s"
                                     % (i, k, g, w))
    print("CROSS-CHECK PASS (%d random entries vs independent brute force)" % len(idx))


# ---------------- audit story ----------------

def fmt_ts(ms):
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc
                                           ).strftime("%H:%M:%S.%f")[:-3]


def audit_story(mid, ts, i, rungs, f):
    long_p, short_p = label_entry(mid, ts, i, rungs)
    f.write("ENTRY %s UTC  mid=%.6g\n" % (fmt_ts(ts[i]), mid[i]))
    for name, prof, word in (("LONG", long_p, "dropping"), ("SHORT", short_p, "rising")):
        f.write("  %s:\n" % name)
        for k, y in enumerate(rungs):
            x, tms, ok = prof[k]
            if ok:
                f.write("    before %s %.3f%% (at %s, %+.0fs later): best favorable %.3f%%\n"
                        % (word, y * 100, fmt_ts(tms), (tms - ts[i]) / 1000, x * 100))
            else:
                f.write("    %s %.3f%% NEVER happened before data end (censored); "
                        "best favorable seen %.3f%%\n" % (word, y * 100, x * 100))
    f.write("\n")


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--day"); ap.add_argument("--coin")
    ap.add_argument("--entries", type=int, default=2000)
    ap.add_argument("--checks", type=int, default=40)
    ap.add_argument("--out", default="out")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()

    selftest()
    if a.selftest:
        return

    print("loading %s from %s ..." % (a.coin, a.day))
    ts, mid, spread_pct, tick_pct = load_bbo(a.day, a.coin)
    print("  %d mid-changes  %s -> %s UTC" % (len(mid), fmt_ts(ts[0]), fmt_ts(ts[-1])))
    rungs, floor, ceiling = derive_rungs(mid, spread_pct, tick_pct)
    print("ANCHORS (derived, declared): median spread %.4f%%  tick %.5f%%  "
          "floor %.4f%%  slice range %.3f%%" % (spread_pct * 100, tick_pct * 100,
                                                floor * 100, ceiling * 100))
    print("RUNGS: " + ", ".join("%.4f%%" % (r * 100) for r in rungs))

    rng = np.random.default_rng(a.seed)
    # entries drawn from first 80% of slice so most labels can resolve.
    # Only indices that are the LAST update within their millisecond are eligible:
    # that is the only state a live bot (or any timestamp join) can see at time t.
    hi = int(len(mid) * 0.8)
    last_at_ts = np.ones(len(mid), dtype=bool)
    last_at_ts[:-1] = ts[:-1] != ts[1:]
    cand = np.flatnonzero(last_at_ts[:hi])
    cand = cand[cand >= 1]
    entries = np.sort(rng.choice(cand, size=min(a.entries, len(cand)),
                                 replace=False))

    # verification on the REAL tape before any labeling is trusted
    small = min(len(mid), 200_000)
    cross_check(mid[:small], ts[:small], rungs, a.checks, rng)

    os.makedirs(a.out, exist_ok=True)
    rows = []
    t0 = __import__("time").time()
    for n, i in enumerate(entries):
        lab = label_entry(mid, ts, int(i), rungs)
        if lab is None:
            continue
        long_p, short_p = lab
        row = {"ts_ms": int(ts[i]), "mid": float(mid[i])}
        for k, y in enumerate(rungs):
            tag = ("%.4f" % (y * 100)).rstrip("0").rstrip(".")
            for d, prof in (("L", long_p), ("S", short_p)):
                x, tms, ok = prof[k]
                row["%s_X@%s" % (d, tag)] = round(x, 8)
                row["%s_dt@%s" % (d, tag)] = (tms - int(ts[i])) if ok else -1
                row["%s_ok@%s" % (d, tag)] = int(ok)
        rows.append(row)
        if (n + 1) % 500 == 0:
            print("  labeled %d/%d (%.0fs)" % (n + 1, len(entries),
                                               __import__("time").time() - t0))

    import csv
    out_csv = os.path.join(a.out, "%s_labels.csv" % a.coin)
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    out_audit = os.path.join(a.out, "%s_audit.txt" % a.coin)
    with open(out_audit, "w") as f:
        f.write("AUDIT SAMPLE — %s — raw tickets for human verification\n" % a.coin)
        f.write("anchors: spread %.4f%% tick %.5f%% floor %.4f%% range %.3f%%\n\n"
                % (spread_pct * 100, tick_pct * 100, floor * 100, ceiling * 100))
        for i in rng.choice(entries, size=min(3, len(entries)), replace=False):
            audit_story(mid, ts, int(i), rungs, f)
    print("WROTE %s (%d rows) and %s" % (out_csv, len(rows), out_audit))


if __name__ == "__main__":
    main()
