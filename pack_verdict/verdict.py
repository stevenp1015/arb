#!/usr/bin/env python3
"""Probation verdict for pack_rider's patient exits (TP 0.4% / SL 1.4% / 100min).

Judged per-EVENT (one number per signal), sitters (timeouts) counted, from
paper_events.csv. Segments detected via eid resets (process restarts).
Cross-checked against paper_fills.csv.
"""
import csv, statistics
from datetime import datetime

def load(fn):
    with open(fn) as f:
        return list(csv.DictReader(f))

events = load("paper_events.csv")
fills = load("paper_fills.csv")

# --- segment detection via eid reset ---
segs = []
prev_eid = None
for r in events:
    eid = int(r["eid"])
    if prev_eid is None or eid < prev_eid:
        segs.append([])
    segs[-1].append(r)
    prev_eid = eid

print(f"{len(events)} resolved events across {len(segs)} process runs:")
for i, s in enumerate(segs):
    t0, t1 = s[0]["ts"][:16], s[-1]["ts"][:16]
    print(f"  run {i}: {len(s)} events, {t0} .. {t1}, eids {s[0]['eid']}..{s[-1]['eid']}")

# --- timeout-length check per run (old race = ~120s, patient = ~6000s) ---
# map fills into runs by timestamp boundaries
bounds = [datetime.fromisoformat(s[0]["ts"]) for s in segs]
def run_of(ts):
    t = datetime.fromisoformat(ts)
    k = 0
    for i, b in enumerate(bounds):
        if t >= b:
            k = i
    return k
to_secs = {}
for f in fills:
    if f["outcome"] in ("to_w", "to_l"):
        to_secs.setdefault(run_of(f["ts"]), []).append(float(f["secs"]))
for i in sorted(to_secs):
    m = statistics.median(to_secs[i])
    print(f"  run {i}: median timeout-exit at {m:.0f}s ({m/60:.0f} min)")

# --- the verdict: newest run only ---
new = segs[-1]
per_event = [float(r["avg_bps"]) for r in new]
n_pos = sum(1 for x in per_event if x > 0)
dirs = {"1": [], "-1": []}
for r in new:
    dirs[r["dir"]].append(float(r["avg_bps"]))
tot_fills = sum(int(r["n"]) for r in new)
tp = sum(int(r["tp"]) for r in new)
sl = sum(int(r["sl"]) for r in new)
tow = sum(int(r["to_w"]) for r in new)
tol = sum(int(r["to_l"]) for r in new)

print(f"\n=== VERDICT: newest run, {len(new)} events, {tot_fills} fills ===")
print(f"per-event avg return: mean {statistics.mean(per_event):+.1f} bps, "
      f"median {statistics.median(per_event):+.1f} bps, "
      f"stdev {statistics.stdev(per_event):.1f}")
print(f"events positive: {n_pos}/{len(new)} ({100*n_pos/len(new):.0f}%)")
print(f"worst {min(per_event):+.1f}, best {max(per_event):+.1f}")
for d, lbl in (("1", "UP-fires (long baskets)"), ("-1", "DOWN-fires (short baskets)")):
    v = dirs[d]
    if v:
        print(f"{lbl}: {len(v)} events, mean {statistics.mean(v):+.1f} bps, "
              f"{sum(1 for x in v if x>0)}/{len(v)} positive")
print(f"fill outcomes: {tp} TP ({100*tp/tot_fills:.1f}%), {sl} SL ({100*sl/tot_fills:.1f}%), "
      f"{tow} timeout-up ({100*tow/tot_fills:.1f}%), {tol} timeout-down ({100*tol/tot_fills:.1f}%)")

# stability: halves
h = len(new)//2
a, b = per_event[:h], per_event[h:]
print(f"first half: mean {statistics.mean(a):+.1f} ({sum(1 for x in a if x>0)}/{len(a)} pos) | "
      f"second half: mean {statistics.mean(b):+.1f} ({sum(1 for x in b if x>0)}/{len(b)} pos)")

# weekly buckets
from collections import defaultdict
wk = defaultdict(list)
for r in new:
    wk[r["ts"][:10]].append(float(r["avg_bps"]))
print("\nby day: date  n  mean_bps  pos")
for d in sorted(wk):
    v = wk[d]
    print(f"  {d}  {len(v):2d}  {statistics.mean(v):+7.1f}  {sum(1 for x in v if x>0)}/{len(v)}")

# fee sensitivity (ledger bps already pays the spread both ways, zero exchange fee)
for fee_bps in (0, 4, 9):
    adj = [x - fee_bps for x in per_event]
    print(f"minus {fee_bps} bps round-trip fees: mean {statistics.mean(adj):+.1f}, "
          f"{sum(1 for x in adj if x>0)}/{len(adj)} events positive")

# cross-check: recompute event avg from fills for the newest run
fr = defaultdict(list)
for f in fills:
    if run_of(f["ts"]) == len(segs)-1:
        fr[f["eid"]].append(float(f["bps"]))
mism = 0
for r in new:
    got = statistics.mean(fr[r["eid"]]) if fr.get(r["eid"]) else None
    if got is None or abs(got - float(r["avg_bps"])) > 0.5:
        mism += 1
        print(f"  MISMATCH eid {r['eid']}: events says {r['avg_bps']}, fills say {got}")
print(f"\ncross-check events-vs-fills: {len(new)-mism}/{len(new)} match")
# fills present in fills.csv but never resolved into an event row (lost at crash / still open)
orphans = set(fr) - {r["eid"] for r in new}
if orphans:
    o_bps = [statistics.mean(fr[e]) for e in orphans]
    print(f"orphan eids (fills logged, no event row): {sorted(int(x) for x in orphans)} "
          f"avg of their closed fills: {statistics.mean(o_bps):+.1f} bps")
