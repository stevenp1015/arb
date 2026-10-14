#!/usr/bin/env python3
"""THE_PLAN item 2 — the toll-booth first harvest.

Question (verbatim from THE_PLAN): per-signal, net, do live-paper liquidation fades
clear zero? And does the trash-only filter help (majors absorb, trash dents)?
Success bar, pre-registered: > +0.05% per signal net on trash-only.

The ledger's pnl_pct is already spread-honest BOTH legs (liq_paper.py enters at the
real ask/bid and exits against the exit-side quote). It does NOT include exchange
fees, so we charge those here.

Per-signal accounting, sitters (timeouts) counted, net of fees. Standing law #2.
"""
import csv, sys, math
from collections import defaultdict
from datetime import datetime

CSV = "paper_trades.csv"

# Aster fees. Maker 0%, taker 0.04% (0.005% on USD1-perps, not used here).
# Entry is always a taker cross. TP exit rests as a limit (maker), SL/timeout cross (taker).
FEE_TAKER = 0.04
FEE_MAKER = 0.00

# "Majors" = deep books that absorb a liquidation without denting. Everything else
# is trash for the purposes of this test. Stock perps get their own bucket because
# Aster lists them and they are a different animal entirely.
MAJORS = {
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT", "TRXUSDT", "DOTUSDT",
    "MATICUSDT", "BCHUSDT", "ETCUSDT", "ATOMUSDT", "UNIUSDT", "FILUSDT",
}
STOCKS = {
    "SKHYNIXUSDT", "SNDKUSDT", "NVDAUSDT", "TSLAUSDT", "AAPLUSDT", "MSFTUSDT",
    "AMZNUSDT", "GOOGLUSDT", "METAUSDT", "COINUSDT", "MSTRUSDT", "AMDUSDT",
    "PLTRUSDT", "NFLXUSDT", "SPYUSDT", "QQQUSDT", "HOODUSDT", "SMCIUSDT",
    "TSMUSDT", "AVGOUSDT", "MUUSDT", "INTCUSDT", "ARMUSDT", "IONQUSDT",
}


def load():
    rows = []
    with open(CSV) as f:
        for r in csv.DictReader(f):
            try:
                r["pnl"] = float(r["pnl_pct"])
                r["usd"] = float(r["cluster_usd"])
                r["secs"] = int(r["secs"])
                r["dt"] = datetime.fromisoformat(r["ts"])
            except (ValueError, KeyError):
                continue
            rows.append(r)
    return rows


def net_of_fees(pnl, outcome):
    """Charge the real cost of getting in and out."""
    cost = FEE_TAKER                                    # entry always crosses
    cost += FEE_MAKER if outcome == "tp" else FEE_TAKER  # TP rests, SL/timeout crosses
    return pnl - cost


def stats(rows, label, bar=None):
    if not rows:
        print(f"  {label:28s}  n=0")
        return None
    n = len(rows)
    gross = [r["pnl"] for r in rows]
    net = [net_of_fees(r["pnl"], r["outcome"]) for r in rows]
    mg, mn = sum(gross) / n, sum(net) / n
    wins = sum(1 for x in net if x > 0)
    srt = sorted(net)
    med = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2
    sd = math.sqrt(sum((x - mn) ** 2 for x in net) / (n - 1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n else 0.0
    t = mn / se if se else 0.0
    flag = ""
    if bar is not None:
        flag = "  <<< CLEARS BAR" if mn > bar else "  (below bar)"
    print(f"  {label:28s}  n={n:4d}  gross={mg:+.4f}%  net={mn:+.4f}%  "
          f"med={med:+.3f}%  win={100*wins/n:4.1f}%  t={t:+.2f}{flag}")
    return {"n": n, "net": mn, "t": t}


def section(title):
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)


rows = load()
if not rows:
    sys.exit("no rows")

rows.sort(key=lambda r: r["dt"])
span_days = (rows[-1]["dt"] - rows[0]["dt"]).total_seconds() / 86400

print(f"""
TOLL-BOOTH HARVEST — THE_PLAN.md Phase 1 item 2
Ledger: {rows[0]['dt']:%Y-%m-%d %H:%M} -> {rows[-1]['dt']:%Y-%m-%d %H:%M} UTC ({span_days:.1f} days)
Completed signals: {len(rows)}   ({len(rows)/span_days:.1f}/day)
Geometry as traded: TP +0.69% / SL -1.18% / 60min timeout, 5s reaction delay, >=$1,800 cluster
Fees charged here: entry taker {FEE_TAKER}%, exit {FEE_MAKER}% if TP else {FEE_TAKER}%
Pre-registered success bar: > +0.05% per signal, net, on trash-only
""")

section("1. THE HEADLINE — does it clear zero?")
stats(rows, "ALL SIGNALS", bar=0.05)

section("2. THE TRASH-ONLY FILTER (the thing THE_PLAN asked about)")
maj = [r for r in rows if r["sym"] in MAJORS]
stk = [r for r in rows if r["sym"] in STOCKS]
trash = [r for r in rows if r["sym"] not in MAJORS and r["sym"] not in STOCKS]
stats(maj, "majors (deep books)")
stats(stk, "stock perps")
stats(trash, "TRASH-ONLY", bar=0.05)
stats(trash + stk, "trash + stocks", bar=0.05)

section("3. OUTCOME MIX — where the money actually goes")
for oc in ("tp", "sl", "timeout"):
    sub = [r for r in rows if r["outcome"] == oc]
    if sub:
        share = 100 * len(sub) / len(rows)
        contrib = sum(net_of_fees(r["pnl"], r["outcome"]) for r in sub) / len(rows)
        print(f"  {oc:10s}  n={len(sub):4d} ({share:4.1f}% of signals)   "
              f"avg={sum(net_of_fees(r['pnl'], r['outcome']) for r in sub)/len(sub):+.3f}%   "
              f"contributes {contrib:+.4f}% to the per-signal mean")

section("4. DIRECTION — is one side carrying it?")
for d in ("long", "short"):
    stats([r for r in rows if r["fade_dir"] == d], f"fade {d}")
    stats([r for r in trash if r["fade_dir"] == d], f"  trash-only, fade {d}")

section("5. DOSE-RESPONSE — does a bigger liquidation pay more?")
srt = sorted(rows, key=lambda r: r["usd"])
q = len(srt) // 5
for i in range(5):
    lo, hi = i * q, (i + 1) * q if i < 4 else len(srt)
    sub = srt[lo:hi]
    stats(sub, f"cluster Q{i+1}  ${sub[0]['usd']:>9,.0f}-${sub[-1]['usd']:>10,.0f}")

section("6. STABILITY — is it the same trade week to week?")
byday = defaultdict(list)
for r in rows:
    byday[r["dt"].strftime("%Y-%m-%d")].append(r)
days = sorted(byday)
half = len(days) // 2
stats([r for d in days[:half] for r in byday[d]], f"first half ({days[0]} -> {days[half-1]})")
stats([r for d in days[half:] for r in byday[d]], f"second half ({days[half]} -> {days[-1]})")
print()
for d in days:
    s = byday[d]
    m = sum(net_of_fees(r["pnl"], r["outcome"]) for r in s) / len(s)
    bar = "#" * min(40, int(abs(m) * 60))
    print(f"  {d}  n={len(s):3d}  {m:+.4f}%  {'':>0}{bar if m>0 else '':<40}{bar if m<0 else ''}")

section("7. CORRELATION CHECK — is the effective sample smaller than it looks?")
# A market-wide cascade fires many symbols at once. Those are not independent bets.
rows_s = sorted(rows, key=lambda r: r["dt"])
clusters, cur = [], [rows_s[0]]
for a, b in zip(rows_s, rows_s[1:]):
    if (b["dt"] - a["dt"]).total_seconds() <= 60:
        cur.append(b)
    else:
        clusters.append(cur); cur = [b]
clusters.append(cur)
multi = [c for c in clusters if len(c) > 1]
print(f"  signals: {len(rows)}   independent 60s episodes: {len(clusters)}")
print(f"  episodes with >1 signal: {len(multi)}  (largest: {max(len(c) for c in clusters)} symbols at once)")
ep_means = [sum(net_of_fees(r["pnl"], r["outcome"]) for r in c) / len(c) for c in clusters]
n_ep = len(ep_means)
m_ep = sum(ep_means) / n_ep
sd_ep = math.sqrt(sum((x - m_ep) ** 2 for x in ep_means) / (n_ep - 1))
print(f"  PER-EPISODE mean (the honest n): {m_ep:+.4f}%  n={n_ep}  "
      f"t={m_ep/(sd_ep/math.sqrt(n_ep)):+.2f}")

section("8. WORST SINGLE SIGNALS — what does the tail look like?")
for r in sorted(rows, key=lambda r: r["pnl"])[:8]:
    print(f"  {r['dt']:%m-%d %H:%M}  {r['sym']:16s} {r['fade_dir']:5s} "
          f"${r['usd']:>10,.0f}  {r['outcome']:8s} {r['pnl']:+8.3f}%  {r['secs']}s")

section("9. PER-SYMBOL — anything repeatable?")
bysym = defaultdict(list)
for r in rows:
    bysym[r["sym"]].append(r)
ranked = sorted(((s, v) for s, v in bysym.items() if len(v) >= 5),
                key=lambda kv: -sum(net_of_fees(r["pnl"], r["outcome"]) for r in kv[1]) / len(kv[1]))
print("  best 6 (n>=5):")
for s, v in ranked[:6]:
    stats(v, f"    {s}")
print("  worst 6 (n>=5):")
for s, v in ranked[-6:]:
    stats(v, f"    {s}")
