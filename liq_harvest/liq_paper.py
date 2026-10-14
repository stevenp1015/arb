#!/usr/bin/env python3
"""liq_paper — LIVE paper-trading of the liquidation-fade (toll booth, fake money).
Watches the liq tank file as it grows; on each BIG cluster (>= $1,800 within 10s,
same sym+side): waits 5s, paper-enters AT THE REAL ASK (long) / BID (short) from
Aster bookTicker — spread paid, honestly. Exits: TP 0.69% / SL 1.18% (the tape-
derived pair), checked against the exit-side quote. 60min timeout at market side.
Telegram in plain English. CSV ledger. Stopfile /tmp/liq_paper.stop"""
import json, os, time, glob, datetime, urllib.request, urllib.parse

TOK = os.environ.get("TG_TOKEN", ""); CHAT = os.environ.get("TG_CHAT", "")
MIN_USD = 1800.0; DELAY = 5.0; TP = 0.0069; SL = 0.0118; TIMEOUT = 3600
DIR = "/root/liq_paper"; STOP = "/tmp/liq_paper.stop"

def log(m):
    print(f"{datetime.datetime.now(datetime.timezone.utc).strftime('%m-%d %H:%M:%S')} {m}", flush=True)

def tg(t):
    if not TOK: return
    try:
        d = urllib.parse.urlencode({"chat_id": CHAT, "text": t}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TOK}/sendMessage", data=d), timeout=8)
    except Exception as e:
        log(f"tg err {e}")

def quote(sym):
    try:
        with urllib.request.urlopen(f"https://fapi.asterdex.com/fapi/v1/ticker/bookTicker?symbol={sym}", timeout=8) as r:
            d = json.loads(r.read())
        return float(d["bidPrice"]), float(d["askPrice"])
    except Exception:
        return None, None

def ledger(row):
    p = os.path.join(DIR, "paper_trades.csv")
    new = not os.path.exists(p)
    with open(p, "a") as f:
        if new:
            f.write("ts,sym,liq_side,cluster_usd,fade_dir,entry,tp_px,sl_px,exit,outcome,pnl_pct,secs\n")
        f.write(row + "\n")

os.makedirs(DIR, exist_ok=True)
open_pos = []          # dicts
pend = {}              # (sym,side) -> cluster
seen_offset = {}
log("liq_paper starting")
tg("Toll booth OPEN (paper): fading every big liquidation (>= $1,800) on Aster, 5s reaction, "
   "entries at the real ask/bid, TP 0.69% / SL 1.18% from the tape study. No real money.")

def process_line(line):
    try:
        d = json.loads(line); o = d["e"]["o"]
        sym, side = o["s"], o["S"]
        usd = float(o["ap"]) * float(o["z"]); t = d["t"] / 1000
    except Exception:
        return
    k = (sym, side)
    c = pend.get(k)
    if c and t - c["tl"] <= 10:
        c["usd"] += usd; c["tl"] = t
    else:
        pend[k] = {"usd": usd, "tl": t}

def fire_ready(now):
    for k, c in list(pend.items()):
        if now - c["tl"] >= DELAY:
            del pend[k]
            if c["usd"] < MIN_USD:
                continue
            sym, side = k
            if any(p["sym"] == sym for p in open_pos):
                continue
            bid, ask = quote(sym)
            if not bid:
                continue
            fade = 1 if side == "SELL" else -1      # forced selling -> we long
            entry = ask if fade == 1 else bid
            open_pos.append({"sym": sym, "fade": fade, "entry": entry, "usd": c["usd"],
                             "t": now, "liq_side": side,
                             "tp": entry * (1 + TP * fade), "sl": entry * (1 - SL * fade)})
            log(f"ENTER {sym} fade={'long' if fade==1 else 'short'} entry {entry:g} cluster ${c['usd']:,.0f}")
            tg(f"Toll booth: {'bought' if fade==1 else 'shorted'} {sym.replace('USDT','')} at {entry:g} "
               f"5s after a ${c['usd']:,.0f} forced liquidation. Target +0.69%, stop -1.18%. (Paper.)")

def manage(now):
    for p in list(open_pos):
        bid, ask = quote(p["sym"])
        if not bid:
            continue
        mark = bid if p["fade"] == 1 else ask       # exit side
        pnl = (mark / p["entry"] - 1) * p["fade"]
        out = None
        if (p["fade"] == 1 and mark >= p["tp"]) or (p["fade"] == -1 and mark <= p["tp"]):
            out = "tp"
        elif (p["fade"] == 1 and mark <= p["sl"]) or (p["fade"] == -1 and mark >= p["sl"]):
            out = "sl"
        elif now - p["t"] > TIMEOUT:
            out = "timeout"
        if out:
            open_pos.remove(p)
            secs = int(now - p["t"])
            ledger(f"{datetime.datetime.now(datetime.timezone.utc).isoformat()},{p['sym']},{p['liq_side']},"
                   f"{p['usd']:.0f},{'long' if p['fade']==1 else 'short'},{p['entry']:g},{p['tp']:g},{p['sl']:g},"
                   f"{mark:g},{out},{pnl*100:.3f},{secs}")
            word = {"tp": "hit the target", "sl": "hit the stop", "timeout": "timed out after an hour"}[out]
            log(f"EXIT {p['sym']} {out} {pnl*100:+.3f}% after {secs}s")
            tg(f"Toll booth result: {p['sym'].replace('USDT','')} {word} — {pnl*100:+.2f}% after "
               f"{secs//60}min. (Paper.)")

while not os.path.exists(STOP):
    files = sorted(glob.glob("/root/liq_capture/liq_*.jsonl"))
    for fn in files[-2:]:
        sz = os.path.getsize(fn)
        off = seen_offset.get(fn, sz if fn not in seen_offset and fn != files[-1] else seen_offset.get(fn, 0))
        if fn not in seen_offset:
            seen_offset[fn] = sz if fn != files[-1] else sz   # start from NOW: skip history
            continue
        if sz > seen_offset[fn]:
            with open(fn) as f:
                f.seek(seen_offset[fn])
                for line in f:
                    process_line(line)
            seen_offset[fn] = sz
    now = time.time()
    fire_ready(now)
    if open_pos:
        manage(now)
    time.sleep(2)
log("stopped")
tg("Toll booth closed (stopfile). Paper ledger saved.")
