#!/usr/bin/env python3
"""pack_rider v1 (PAPER) — leader-agnostic pack-follower. The magnum opus.

Mined basis (leader_mine/leader_mine2, 48h x 525 pairs): after the FIRST pair
crosses its own q99.95 |ret_3s|, the not-yet-moved pack wins a +/-0.30% race
74.9% (next-print entries) vs 51.1% baseline; edge decays ~1.5pp/s, alive at +5s.

Live logic:
  - all-USDT-perp bookTicker via 4 sharded WS conns; per-sym mid ring (10s)
  - crossing: |mid ret_3s| >= per-sym threshold (from thresholds.json, mined),
    60s per-sym cooldown
  - confirmation gate (live can't see the future): first crossing arms a pending
    event; >=4 more same-direction crossings within 1.5s => FIRE (mine says an
    avg event has ~66 followers, half inside 1s; local one-pair spikes die here)
  - basket: every sym with fresh quote (<=5s), spread <=0.4%, not crossed in 60s,
    not already open; PAPER entry at the touched side (ask for up / bid for down)
  - exits per entry: TP bid>=entry*1.003 (mirror for shorts), SL 0.997,
    timeout 120s at market side. Spread is PAID both ways => stricter than mine.
  - logs: paper_fills.csv, paper_events.csv; Telegram on fire/resolve.
Stopfile: /tmp/pack_rider.stop  PAPER ONLY — no orders are sent anywhere.
"""
import asyncio, json, os, time, datetime, urllib.request, urllib.parse
from collections import deque

import websockets

DIR = "/root/pack_rider"
STOP = "/tmp/pack_rider.stop"
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
CONF_N = 4          # extra same-dir crossings to confirm
CONF_MS = 1500
COOL_SYM = 60.0
COOL_EVENT = 120.0
FRESH_S = 5.0
MAX_SPREAD = 0.004
TP = 0.004
SL = 0.014
TIMEOUT_S = 6000.0
BASKET_CAP = 150
SPIKE_MIN = 0.007          # instant alert floor: >=1% in 3s (or 2x the coin's own threshold)
SPIKE_COOL = 1800.0       # one instant alert per coin per 30 min
VENUES = {}

def log(msg):
    print(f"{datetime.datetime.now(datetime.timezone.utc).strftime('%m-%d %H:%M:%S')} {msg}", flush=True)

def telegram(text):
    if not TG_TOKEN:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data), timeout=8)
    except Exception as e:
        log(f"tg err {e}")

def csv_append(fn, header, row):
    p = os.path.join(DIR, fn)
    new = not os.path.exists(p)
    with open(p, "a") as f:
        if new:
            f.write(header + "\n")
        f.write(row + "\n")

class S:
    __slots__ = ("bid", "ask", "t", "ring", "last_cross", "last_spike")
    def __init__(self):
        self.bid = self.ask = 0.0
        self.t = 0.0
        self.ring = deque()
        self.last_cross = 0.0
        self.last_spike = 0.0

state = {}
THR = {}
pending = None          # {"dir","t0","syms"}
last_event_t = 0.0
open_pos = {}           # sym -> dict
event_meta = {}         # eid -> dict
eid_counter = 0

def on_tick(sym, bid, ask, now):
    global pending, last_event_t, eid_counter
    st = state[sym]
    st.bid, st.ask, st.t = bid, ask, now
    mid = (bid + ask) / 2
    ring = st.ring
    ring.append((now, mid))
    while ring and now - ring[0][0] > 10.0:
        ring.popleft()
    if sym in open_pos:
        check_exit(sym, now)
    base = None
    for tt, m in ring:
        if now - tt >= 3.0:
            base = m
        else:
            break
    if base is None or base <= 0:
        return
    ret3 = mid / base - 1
    thr = THR.get(sym)
    if thr is None or abs(ret3) < thr:
        return
    if now - st.last_cross < COOL_SYM:
        return
    st.last_cross = now
    d = 1 if ret3 > 0 else -1
    csv_append("crossings.csv", "ts_ms,sym,dir,ret3",
               f"{int(now*1000)},{sym},{d},{ret3:.5f}")
    if sym in VENUES and abs(ret3) >= max(1.5 * THR.get(sym, 1), SPIKE_MIN) and now - st.last_spike > SPIKE_COOL:
        st.last_spike = now
        log(f"SPIKE {sym} {ret3*100:+.1f}%/3s")
        telegram(f"SPIKE: {sym.replace('USDT','')} just moved {'UP' if ret3 > 0 else 'DOWN'} "
                 f"{abs(ret3)*100:.1f}% in ~3 seconds. Trades on: {VENUES[sym]}. Price {mid:.6g}. "
                 f"(Instant + unvalidated — could be a fakeout. React accordingly.)")
    if pending and now - pending["t0"] <= CONF_MS / 1000 and pending["dir"] == d:
        pending["syms"].add(sym)
        if len(pending["syms"]) >= 1 + CONF_N and now - last_event_t > COOL_EVENT:
            last_event_t = now
            eid_counter += 1
            fire(eid_counter, d, set(pending["syms"]), now, pending.get("leader", "?"))
            pending = None
    elif not pending or now - pending["t0"] > CONF_MS / 1000:
        pending = {"dir": d, "t0": now, "syms": {sym}, "leader": sym}

def fire(eid, d, movers, now, leader="?"):
    entries = 0
    for sym, st in state.items():
        if entries >= BASKET_CAP:
            break
        if sym in movers or sym in open_pos:
            continue
        if now - st.t > FRESH_S or st.bid <= 0:
            continue
        mid = (st.bid + st.ask) / 2
        if (st.ask - st.bid) / mid > MAX_SPREAD:
            continue
        if now - st.last_cross < COOL_SYM:
            continue
        entry = st.ask if d == 1 else st.bid
        open_pos[sym] = {"eid": eid, "dir": d, "entry": entry, "t": now}
        entries += 1
    event_meta[eid] = {"dir": d, "t": now, "n": entries, "movers": len(movers),
                       "tp": 0, "sl": 0, "to_w": 0, "to_l": 0, "bps": []}
    act = "bought" if d == 1 else "shorted"
    word = "UP" if d == 1 else "DOWN"
    msg = (f"Practice trade #{eid}: {len(movers)} coins just spiked {word} together (FIRST MOVER: {leader.replace(chr(85)+chr(83)+chr(68)+chr(84),chr(39)*0) if False else leader}), "
           f"so I pretend-{act} {entries} coins that had not moved yet, expecting them to follow. "
           f"Result in about 2 minutes. (No real money.)")
    log(msg)
    telegram(msg)

def close_pos(sym, px, outcome, now):
    p = open_pos.pop(sym)
    m = event_meta[p["eid"]]
    bps = (px / p["entry"] - 1) * p["dir"] * 1e4
    m["bps"].append(bps)
    m[outcome] += 1
    csv_append("paper_fills.csv", "ts,eid,sym,dir,entry,exit,bps,outcome,secs",
               f"{datetime.datetime.now(datetime.timezone.utc).isoformat()},{p['eid']},{sym},"
               f"{p['dir']},{p['entry']:.8g},{px:.8g},{bps:.1f},{outcome},{now-p['t']:.0f}")
    if m["tp"] + m["sl"] + m["to_w"] + m["to_l"] >= m["n"]:
        resolve(p["eid"])

def resolve(eid):
    m = event_meta.pop(eid, None)
    if not m or not m["bps"]:
        return
    import statistics
    wr = 100 * (m["tp"] + m["to_w"]) / len(m["bps"])
    avg = statistics.mean(m["bps"])
    msg = (f"Practice trade #{eid} result: {wr:.0f}% of {len(m['bps'])} positions finished profitable, "
           f"averaging {avg/100:+.3f}% each. {m['tp']} hit the full profit target, {m['sl']} hit the stop-loss, "
           f"the rest timed out after 100 minutes ({m['to_w']} slightly up, {m['to_l']} slightly down). (Fake money.)")
    log(msg)
    telegram(msg)
    csv_append("paper_events.csv", "ts,eid,dir,n,tp,sl,to_w,to_l,win_pct,avg_bps",
               f"{datetime.datetime.now(datetime.timezone.utc).isoformat()},{eid},{m['dir']},"
               f"{len(m['bps'])},{m['tp']},{m['sl']},{m['to_w']},{m['to_l']},{wr:.1f},{avg:.2f}")

def check_exit(sym, now):
    p = open_pos.get(sym)
    if not p:
        return
    st = state[sym]
    if p["dir"] == 1:
        mark = st.bid
        if mark >= p["entry"] * (1 + TP):
            return close_pos(sym, mark, "tp", now)
        if mark <= p["entry"] * (1 - SL):
            return close_pos(sym, mark, "sl", now)
    else:
        mark = st.ask
        if mark <= p["entry"] * (1 - TP):
            return close_pos(sym, mark, "tp", now)
        if mark >= p["entry"] * (1 + SL):
            return close_pos(sym, mark, "sl", now)
    if now - p["t"] > TIMEOUT_S:
        bps = (mark / p["entry"] - 1) * p["dir"]
        close_pos(sym, mark, "to_w" if bps > 0 else "to_l", now)

async def housekeeper():
    while not os.path.exists(STOP):
        now = time.time()
        for sym in list(open_pos.keys()):
            if now - open_pos[sym]["t"] > TIMEOUT_S:
                check_exit(sym, now)
        for eid in list(event_meta.keys()):
            if now - event_meta[eid]["t"] > TIMEOUT_S + 60:
                for sym in [s for s, p in open_pos.items() if p["eid"] == eid]:
                    st = state[sym]
                    mark = st.bid if open_pos[sym]["dir"] == 1 else st.ask
                    close_pos(sym, mark, "to_w" if (mark / open_pos[sym]["entry"] - 1) * open_pos[sym]["dir"] > 0 else "to_l", now)
                resolve(eid)
        await asyncio.sleep(5)

async def shard(syms, sid):
    sub = [f"{s.lower()}@bookTicker" for s in syms]
    while not os.path.exists(STOP):
        try:
            async with websockets.connect("wss://fstream.binance.com/ws",
                                          ping_interval=20, max_size=2**20) as ws:
                for i in range(0, len(sub), 50):
                    await ws.send(json.dumps({"method": "SUBSCRIBE",
                                              "params": sub[i:i + 50], "id": i + 1}))
                    await asyncio.sleep(0.3)
                log(f"shard{sid} connected ({len(syms)} subs)")
                async for raw in ws:
                    if os.path.exists(STOP):
                        return
                    m = json.loads(raw)
                    s = m.get("s")
                    if not s:
                        continue
                    try:
                        on_tick(s, float(m["b"]), float(m["a"]), time.time())
                    except Exception as e:
                        log(f"tick err {s}: {e}")
        except Exception as e:
            log(f"shard{sid} err: {e}")
            await asyncio.sleep(5)

def fetch_venues():
    v = {}
    def add(sym, name):
        v.setdefault(sym, []).append(name)
    for attempt in range(3):
        try:
            d = json.loads(urllib.request.urlopen("https://fstream.asterdex.com/fapi/v1/exchangeInfo", timeout=30).read())
            for x in d.get("symbols", []):
                add(x["symbol"], "Aster")
            break
        except Exception as e:
            log(f"venue aster err try{attempt} {e}")
    try:
        d = json.loads(urllib.request.urlopen("https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails", timeout=15).read())
        for m in d.get("order_book_details", []):
            sname = m.get("symbol", "")
            if sname:
                base = "1000" + sname[1:] if sname.startswith("k") else sname
                add(base.upper() + "USDT", "Lighter")
    except Exception as e:
        log(f"venue lighter err {e}")
    try:
        req = urllib.request.Request("https://api.hyperliquid.xyz/info",
                                     data=json.dumps({"type": "meta"}).encode(),
                                     headers={"Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=15).read())
        for u in d.get("universe", []):
            sname = u.get("name", "")
            if sname:
                base = "1000" + sname[1:] if sname.startswith("k") else sname
                add(base.upper() + "USDT", "Hyperliquid")
    except Exception as e:
        log(f"venue hl err {e}")
    try:
        d = json.loads(urllib.request.urlopen("https://api.phemex.com/public/products", timeout=15).read())
        for pr in d.get("data", {}).get("products", []):
            sname = pr.get("symbol", "")
            if sname.endswith("USDT"):
                add(sname, "Phemex")
    except Exception as e:
        log(f"venue phemex err {e}")
    return {k: ", ".join(sorted(set(vv))) for k, vv in v.items()}

async def main():
    os.makedirs(DIR, exist_ok=True)
    global THR, VENUES
    VENUES = fetch_venues()
    log(f"venue map: {len(VENUES)} symbols tradeable on Steven's exchanges")
    THR = json.load(open(os.path.join(DIR, "thresholds.json")))
    med = THR.pop("_median")
    with urllib.request.urlopen("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=15) as r:
        info = json.loads(r.read())
    universe = [x["symbol"] for x in info["symbols"]
                if x.get("status") == "TRADING" and x.get("quoteAsset") == "USDT"
                and x.get("contractType") == "PERPETUAL"]
    for s in universe:
        state[s] = S()
        if s not in THR:
            THR[s] = med
    log(f"universe {len(universe)} syms, thresholds mined={sum(1 for s in universe if s in THR)}")
    telegram(f"pack_rider v1 PAPER live on Tokyo — {len(universe)} pairs, "
             f"confirm {CONF_N}+1 in {CONF_MS}ms, race TP {TP*100:.1f}%/SL {SL*100:.1f}%. "
             f"Instant SPIKE alerts armed for {len(VENUES)} venue-tradeable coins. "
             f"Stopfile: {STOP}")
    shards = [universe[i::4] for i in range(4)]
    await asyncio.gather(housekeeper(), *(shard(sh, i) for i, sh in enumerate(shards)))
    log("stopped")
    telegram("pack_rider stopped (stopfile).")

asyncio.run(main())
