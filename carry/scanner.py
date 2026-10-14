#!/usr/bin/env python3
"""
Cross-venue funding-carry scanner  (stdlib only, runs on box system python3).

What it does
------------
Every tick it pulls funding rates for every perp on Aster and Lighter, normalizes
them to "%/day", and ranks the coins that trade on BOTH venues by how much a
delta-neutral hedge (long the venue where longs get paid, short the venue where
shorts get paid) would collect per day, price-agnostic.

Two modes:
  scan            one-shot, prints the current board, exits.
  watch           daemon: append board snapshot to state/board.jsonl every
                  --interval seconds, AND maintain a paper ledger that accrues
                  what a hedged $1k would have banked, so we can answer
                  "is this actually money or pennies?" with receipts.

Sign convention (both venues, standard): funding rate > 0 => longs pay shorts.
So you WANT to be:
    LONG  the venue with the most-negative rate  (shorts pay you)
    SHORT the venue with the most-positive rate   (longs pay you)
net carry %/day = (short_leg_rate - long_leg_rate), both normalized to %/day.
"""
import json, urllib.request, time, os, sys, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
os.makedirs(STATE, exist_ok=True)

ASTER = "https://fapi.asterdex.com"
LIGHTER = "https://mainnet.zklighter.elliot.ai"

# ---- entry gate defaults (see config discussion) ----
MIN_NET_DAILY = 0.15          # %/day floor to even consider a pair
MIN_ASTER_VOL = 100_000       # $ 24h quote volume on Aster leg (liquidity guard)
TRUST_INTERVALS = {8}         # funding intervals (h) we treat as reliable; 1h/4h flagged noisy


def _get(url, data=None, timeout=12):
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data else None,
        headers={"Content-Type": "application/json", "User-Agent": "curl/8.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def norm_coin(sym: str) -> str:
    s = sym.upper()
    for suf in ("USDT", "USDC", "USD1", "PERP", "-PERP", "_PERP"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    if s.startswith("1000"):
        s = "k" + s[4:]
    return s


def fetch_aster():
    """Return {coin: {'rate_day', 'raw', 'interval_h', 'sym', 'vol24'}}."""
    interval = {}
    try:
        for r in _get(f"{ASTER}/fapi/v1/fundingInfo"):
            interval[r["symbol"]] = float(r.get("fundingIntervalHours", 8))
    except Exception:
        pass  # only non-8h symbols are listed; absent => 8h
    vol = {}
    try:
        for r in _get(f"{ASTER}/fapi/v1/ticker/24hr"):
            vol[r["symbol"]] = float(r.get("quoteVolume", 0))
    except Exception:
        pass
    out = {}
    for r in _get(f"{ASTER}/fapi/v1/premiumIndex"):
        sym = r["symbol"]
        if "_" in sym:               # dated/quarterly contracts
            continue
        raw = float(r["lastFundingRate"] or 0)
        iv = interval.get(sym, 8)
        c = norm_coin(sym)
        rec = {"rate_day": raw * (24 / iv) * 100, "raw": raw,
               "interval_h": iv, "sym": sym, "vol24": vol.get(sym, 0)}
        # if a coin maps from multiple symbols keep the most extreme funder
        if c not in out or abs(rec["rate_day"]) > abs(out[c]["rate_day"]):
            out[c] = rec
    return out


def fetch_lighter():
    """Lighter /funding-rates returns decimal-per-8h for exchange=='lighter'."""
    out = {}
    resp = _get(f"{LIGHTER}/api/v1/funding-rates")
    for r in resp.get("funding_rates", []):
        if r.get("exchange") != "lighter":
            continue
        raw = float(r["rate"])
        c = norm_coin(r["symbol"])
        out[c] = {"rate_day": raw * 3 * 100, "raw": raw, "interval_h": 8,
                  "sym": r["symbol"], "vol24": None}
    return out


def build_board(gate=True):
    ast = fetch_aster()
    ltr = fetch_lighter()
    rows = []
    for c in set(ast) & set(ltr):
        a, l = ast[c], ltr[c]
        legs = [("AST", a["rate_day"], a), ("LTR", l["rate_day"], l)]
        short_leg = max(legs, key=lambda x: x[1])   # most positive -> we short, longs pay us
        long_leg = min(legs, key=lambda x: x[1])    # most negative -> we long, shorts pay us
        net = short_leg[1] - long_leg[1]
        # reliability: both legs 8h => trustworthy; any 1h/4h leg => noisy annualization
        noisy = not (a["interval_h"] in TRUST_INTERVALS and l["interval_h"] in TRUST_INTERVALS)
        av = a["vol24"] or 0
        row = {
            "coin": c, "net_day": net,
            "short_venue": short_leg[0], "short_rate": short_leg[1],
            "long_venue": long_leg[0], "long_rate": long_leg[1],
            "aster_vol24": av, "aster_interval_h": a["interval_h"],
            "lighter_interval_h": l["interval_h"], "noisy": noisy,
            "aster_sym": a["sym"], "lighter_sym": l["sym"],
        }
        if gate:
            if net < MIN_NET_DAILY:
                continue
            if av < MIN_ASTER_VOL:
                continue
        rows.append(row)
    rows.sort(key=lambda r: r["net_day"], reverse=True)
    return rows


def print_board(rows, n=20):
    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"\nFUNDING CARRY BOARD  {ts}   (gated, top {n})")
    print(f"{'COIN':10} {'net%/d':>7} {'$/d@1k':>7} {'@3x':>6}  "
          f"{'SHORT':13} {'LONG':13} {'astVol':>8} flag")
    for r in rows[:n]:
        d1 = r["net_day"] / 100 * 1000
        vs = f"${r['aster_vol24']/1e6:.1f}M" if r["aster_vol24"] else "-"
        flag = "noisy" if r["noisy"] else ""
        print(f"{r['coin']:10} {r['net_day']:7.3f} {d1:7.2f} {d1*3:6.2f}  "
              f"{r['short_venue']}{r['short_rate']:+.3f}    {r['long_venue']}{r['long_rate']:+.3f}    "
              f"{vs:>8} {flag}")
    if not rows:
        print("  (nothing clears the entry gate right now)")


# ---------------- paper ledger ----------------
# Tracks hypothetical hedged $1k positions and accrues funding by time-weighting
# the live posted rate (rate_%/day * dt). This APPROXIMATES the discrete funding
# payments; good enough to size the opportunity honestly. Two tracks:
#   static_<coin> : entered once, held; shows decay of a fixed position.
#   rotate        : always sits in the current #1 gated pair (rotation cost ignored,
#                   flagged in the report).
PAPER = os.path.join(STATE, "paper_ledger.jsonl")
PAPER_STATE = os.path.join(STATE, "paper_state.json")
NOTIONAL = 1000.0             # per-leg notional the paper position simulates


def _load_paper_state():
    if os.path.exists(PAPER_STATE):
        with open(PAPER_STATE) as f:
            return json.load(f)
    return None


def _save_paper_state(s):
    tmp = PAPER_STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, PAPER_STATE)


def _append(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")
        f.flush()
        os.fsync(f.fileno())


def paper_tick(rows, now):
    """Update paper positions using the current board."""
    board = {r["coin"]: r for r in rows}
    st = _load_paper_state()
    if st is None:
        # first run: seed static tracks from the current top-3 gated pairs
        seeds = [r["coin"] for r in rows[:3]]
        st = {"last_ts": now, "positions": {}}
        for c in seeds:
            st["positions"][f"static_{c}"] = {
                "coin": c, "accrued_usd": 0.0, "entry_ts": now,
                "entry_net": board[c]["net_day"], "mode": "static", "alive": True,
            }
        st["positions"]["rotate"] = {
            "coin": rows[0]["coin"] if rows else None, "accrued_usd": 0.0,
            "entry_ts": now, "mode": "rotate", "alive": True,
        }
        _save_paper_state(st)
        _append(PAPER, {"ts": now, "event": "seed", "positions": list(st["positions"])})
        return st

    dt_days = (now - st["last_ts"]) / 86400.0
    if dt_days <= 0:
        return st
    for name, p in st["positions"].items():
        if not p.get("alive"):
            continue
        if p["mode"] == "rotate":
            # jump to whatever is #1 now (honest: rotation cost not modeled here)
            if rows:
                p["coin"] = rows[0]["coin"]
        c = p["coin"]
        r = board.get(c)
        if r is None:
            # coin fell off the gated board; static position holds but earns
            # only if we still had a live rate. Mark it dormant this tick.
            continue
        net = r["net_day"]
        # funding-flip stop for static tracks: net<0 two ticks -> close
        if p["mode"] == "static":
            if net < 0:
                p["neg_ticks"] = p.get("neg_ticks", 0) + 1
                if p["neg_ticks"] >= 2:
                    p["alive"] = False
                    _append(PAPER, {"ts": now, "event": "flip_close",
                                    "pos": name, "coin": c, "final_usd": p["accrued_usd"]})
                    continue
            else:
                p["neg_ticks"] = 0
        p["accrued_usd"] += NOTIONAL * (net / 100.0) * dt_days
    st["last_ts"] = now
    _save_paper_state(st)
    return st


def paper_report():
    st = _load_paper_state()
    if not st:
        print("no paper positions yet")
        return
    print("\nPAPER LEDGER  (simulated $1000/leg hedged positions)")
    print(f"{'position':18} {'coin':8} {'alive':6} {'hours':>6} {'accrued$':>9} {'$/day':>7}")
    for name, p in st["positions"].items():
        hrs = (st["last_ts"] - p["entry_ts"]) / 3600.0
        perday = p["accrued_usd"] / (hrs / 24.0) if hrs > 0 else 0.0
        print(f"{name:18} {str(p['coin']):8} {str(p.get('alive')):6} "
              f"{hrs:6.1f} {p['accrued_usd']:9.4f} {perday:7.3f}")


def watch(interval, board_n):
    board_path = os.path.join(STATE, "board.jsonl")
    print(f"[watch] logging every {interval}s -> {board_path}")
    while True:
        try:
            now = time.time()
            rows = build_board(gate=True)
            _append(board_path, {"ts": now, "top": rows[:board_n]})
            paper_tick(rows, now)
            top = rows[0] if rows else None
            msg = (f"top={top['coin']} {top['net_day']:.3f}%/d" if top else "board empty")
            print(f"[{time.strftime('%H:%M:%S', time.gmtime(now))}] {len(rows)} gated  {msg}")
        except Exception as e:
            print(f"[watch] error: {e}")
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["scan", "watch", "report"], nargs="?", default="scan")
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--all", action="store_true", help="scan: show ungated board")
    a = ap.parse_args()
    if a.mode == "scan":
        print_board(build_board(gate=not a.all), n=a.n)
    elif a.mode == "watch":
        watch(a.interval, a.n)
    elif a.mode == "report":
        paper_report()


if __name__ == "__main__":
    main()
