#!/usr/bin/env python3
"""
Append-only ledger — the single source of provable truth for the carry bot.

Every fill, fee, funding credit/debit, alert, and state transition is one JSONL
line, fsync'd. Nothing is ever mutated. report() folds it into per-pair
net-of-EVERYTHING P&L so profit is provable straight from the raw ledger, which
is exactly the accounting standard Steven enforces.
"""
import os, json, time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.abspath(os.path.join(HERE, "..", "state"))
os.makedirs(STATE, exist_ok=True)
LEDGER = os.path.join(STATE, "ledger.jsonl")


def _now():
    return time.time()


def record(event, **fields):
    """Append one event. event in {fill,fee,funding,alert,transition,note}."""
    row = {"ts": _now(), "event": event, **fields}
    with open(LEDGER, "a") as f:
        f.write(json.dumps(row) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return row


def read_all():
    if not os.path.exists(LEDGER):
        return []
    out = []
    with open(LEDGER) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def seen_funding_keys():
    """Dedup set for funding reconciliation: (venue, market, funding_ts)."""
    keys = set()
    for r in read_all():
        if r["event"] == "funding":
            keys.add((r.get("venue"), str(r.get("market")), int(r.get("funding_ts", 0))))
    return keys


def report():
    """Per-pair net P&L from the raw ledger. Returns dict + prints a table."""
    rows = read_all()
    pairs = {}

    def bucket(coin):
        return pairs.setdefault(coin, {"funding": 0.0, "fees": 0.0,
                                       "realized": 0.0, "n_fund": 0, "n_fill": 0})

    for r in rows:
        coin = r.get("coin")
        if r["event"] == "funding" and coin:
            b = bucket(coin)
            b["funding"] += float(r.get("usd", 0))
            b["n_fund"] += 1
        elif r["event"] == "fee" and coin:
            b = bucket(coin)
            b["fees"] += float(r.get("usd", 0))
        elif r["event"] == "fill" and coin:
            bucket(coin)["n_fill"] += 1
        elif r["event"] == "realized" and coin:
            bucket(coin)["realized"] += float(r.get("usd", 0))

    print(f"\n{'coin':10} {'funding$':>9} {'fees$':>8} {'realized$':>9} "
          f"{'NET$':>8} {'#fund':>5} {'#fill':>5}")
    grand = 0.0
    for coin, b in sorted(pairs.items()):
        net = b["funding"] - abs(b["fees"]) + b["realized"]
        grand += net
        print(f"{coin:10} {b['funding']:9.4f} {b['fees']:8.4f} {b['realized']:9.4f} "
              f"{net:8.4f} {b['n_fund']:5d} {b['n_fill']:5d}")
    print(f"{'TOTAL':10} {'':>9} {'':>8} {'':>9} {grand:8.4f}")
    return {"pairs": pairs, "net": grand}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        report()
    else:
        record("note", msg="ledger self-test")
        print("wrote note; total events:", len(read_all()))
