#!/usr/bin/env python3
"""book_recon.py — can our data even express the volume-wall magnet hypothesis?

Steven's observation (from watching MoonTrader's tick chart live): price travels
precisely to the level where Depth Shot placed its take-profit, and that level is
derived from where cumulative resting volume in the book reaches a threshold.
The claim is that big resting size acts as a magnet.

Before testing that, one thing has to be true: the visible book must REACH far
enough for a "wall" to sit at a distance worth trading. Binance publishes only 20
levels per side on the depth20 stream. On a deep market like BTC those 20 levels
may span a hair either side of mid, in which case no wall we can see is ever more
than a rounding error away and the hypothesis is untestable on this feed.

This script measures exactly that, and nothing else. It is reconnaissance, not a
result.
"""
import json, subprocess, sys, statistics as st

TAPE = sys.argv[1] if len(sys.argv) > 1 else \
    "/root/fomc_tape/binance_1785347394.jsonl.zst"
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 60000

agg = {}
seen = 0
proc = subprocess.Popen(["zstdcat", TAPE], stdout=subprocess.PIPE, bufsize=1 << 22)
for raw in proc.stdout:
    if b"depth20" not in raw:
        continue
    try:
        d = json.loads(raw)["m"]["data"]
        bids, asks = d["b"], d["a"]
        if not bids or not asks:
            continue
        best_bid = float(bids[0][0]); best_ask = float(asks[0][0])
    except Exception:
        continue
    mid = (best_bid + best_ask) / 2.0
    rec = agg.setdefault(d["s"], {"spread": [], "aspan": [], "bspan": [],
                                  "ausd": [], "busd": [], "amax": [], "bmax": []})
    rec["spread"].append((best_ask - best_bid) / mid * 100)
    rec["aspan"].append((float(asks[-1][0]) / mid - 1) * 100)
    rec["bspan"].append((1 - float(bids[-1][0]) / mid) * 100)
    rec["ausd"].append(sum(float(p) * float(q) for p, q in asks))
    rec["busd"].append(sum(float(p) * float(q) for p, q in bids))
    # biggest single resting level, and how far away it sits
    ba = max(asks, key=lambda x: float(x[0]) * float(x[1]))
    bb = max(bids, key=lambda x: float(x[0]) * float(x[1]))
    rec["amax"].append((float(ba[0]) * float(ba[1]),
                        (float(ba[0]) / mid - 1) * 100))
    rec["bmax"].append((float(bb[0]) * float(bb[1]),
                        (1 - float(bb[0]) / mid) * 100))
    seen += 1
    if seen >= LIMIT:
        break
proc.stdout.close()
proc.kill()

print(f"sampled {seen:,} depth20 snapshots from {TAPE}\n")
print("HOW FAR DO 20 VISIBLE LEVELS REACH?  (medians)")
hdr = ("symbol", "n", "spread%", "askspan%", "bidspan%", "span/sprd",
       "ask_usd", "bid_usd")
print("  {:9s} {:>7s} {:>9s} {:>9s} {:>9s} {:>10s} {:>12s} {:>12s}".format(*hdr))
for s, r in agg.items():
    med = lambda k: st.median(r[k])
    ratio = med("aspan") / med("spread") if med("spread") else 0
    print(f"  {s:9s} {len(r['spread']):7d} {med('spread'):9.5f} "
          f"{med('aspan'):9.5f} {med('bspan'):9.5f} {ratio:10.1f} "
          f"{med('ausd'):12,.0f} {med('busd'):12,.0f}")

print("\nBIGGEST SINGLE RESTING LEVEL — size, and how far from mid  (medians)")
print("  {:9s} {:>14s} {:>10s} {:>14s} {:>10s}".format(
    "symbol", "ask wall $", "at %", "bid wall $", "at %"))
for s, r in agg.items():
    print(f"  {s:9s} {st.median([x[0] for x in r['amax']]):14,.0f} "
          f"{st.median([x[1] for x in r['amax']]):10.5f} "
          f"{st.median([x[0] for x in r['bmax']]):14,.0f} "
          f"{st.median([x[1] for x in r['bmax']]):10.5f}")

print("\nVERDICT INPUTS")
for s, r in agg.items():
    span = st.median(r["aspan"])
    print(f"  {s}: a wall can sit at most {span:.4f}% away. "
          f"For reference the spread is {st.median(r['spread']):.5f}%.")
