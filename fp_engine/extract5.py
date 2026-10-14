#!/usr/bin/env python3
"""One streaming pass per June day: pull bbo+trades for the 5 best
movement-per-cost coins into /root/fp_engine/tape5/<day>/<coin>_{bbo,trades}.jsonl"""
import os

COINS = ("BTCUSDT", "ZECUSDT", "HYPEUSDT", "DOGEUSDT", "SOLUSDT")
ASTER = "/root/hl_data/aster"
OUT = "/root/fp_engine/tape5"
TAGS = {c: '"c":"%s"' % c for c in COINS}

for day in sorted(d for d in os.listdir(ASTER) if d.startswith("live_2")):
    dd = os.path.join(ASTER, day)
    if not os.path.isdir(dd):
        continue
    day8 = day[5:13]
    od = os.path.join(OUT, day8)
    if os.path.isdir(od) and len(os.listdir(od)) >= 2 * len(COINS):
        print("skip %s (done)" % day8, flush=True)
        continue
    os.makedirs(od, exist_ok=True)
    h = {}
    for c in COINS:
        h[(c, "bbo")] = open(os.path.join(od, "%s_bbo.jsonl" % c), "w")
        h[(c, "trades")] = open(os.path.join(od, "%s_trades.jsonl" % c), "w")
    for fn in sorted(os.listdir(dd)):
        kind = "bbo" if fn.startswith("bbo_shard") else (
            "trades" if fn.startswith("trades_shard") else None)
        if kind is None:
            continue
        with open(os.path.join(dd, fn)) as f:
            for line in f:
                for c, tag in TAGS.items():
                    if tag in line:
                        h[(c, kind)].write(line)
                        break
    for x in h.values():
        x.close()
    print("extracted %s" % day8, flush=True)
print("EXTRACT5 COMPLETE")
