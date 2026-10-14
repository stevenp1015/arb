#!/usr/bin/env python3
"""btc_milestones — the quiet referee for Steven's $5-at-1001x BTC short (entry 66,904).

He is deliberately NOT looking at the position. This bot looks for him and speaks
ONLY when BTC crosses a downward milestone for the first time. No bounce alerts,
no liquidation warnings — silence in every other universe.
Stopfile: /tmp/btc_milestones.stop"""
import json, os, time, datetime, urllib.request, urllib.parse

ENTRY = 66904.0
NOTIONAL = 5 * 1001.0          # $5 margin, 1001x
MAX_PAYOUT = NOTIONAL
MILESTONES = [62000, 60000, 58000, 55000, 50000, 45000, 40000, 35000,
              30000, 25000, 20000, 15000, 10000, 5000, 1000]
DIR = "/root/btc_milestones"
STATE = os.path.join(DIR, "state.json")
STOP = "/tmp/btc_milestones.stop"
TOK = os.environ.get("TG_TOKEN", ""); CHAT = os.environ.get("TG_CHAT", "")

def log(m):
    print(f"{datetime.datetime.now(datetime.timezone.utc).strftime('%m-%d %H:%M:%S')} {m}", flush=True)

def tg(t):
    if not TOK:
        return
    try:
        d = urllib.parse.urlencode({"chat_id": CHAT, "text": t}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TOK}/sendMessage", data=d), timeout=8)
    except Exception as e:
        log(f"tg err {e}")

def price():
    try:
        with urllib.request.urlopen(
                "https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT", timeout=8) as r:
            return float(json.loads(r.read())["price"])
    except Exception:
        return None

def payout(p):
    return NOTIONAL * (ENTRY - p) / ENTRY

os.makedirs(DIR, exist_ok=True)
fired = set()
if os.path.exists(STATE):
    fired = set(json.load(open(STATE)))
log(f"btc_milestones armed; already fired: {sorted(fired)}")
tg("Quiet referee armed for the 66,904 ticket. I only speak when BTC breaks new depths. "
   "Every silent day means nothing you need to see. Go live your life.")

while not os.path.exists(STOP):
    p = price()
    if p:
        for m in MILESTONES:
            if m not in fired and p <= m:
                fired.add(m)
                json.dump(sorted(fired), open(STATE, "w"))
                v = payout(p)
                log(f"MILESTONE {m} broken at {p:.0f}, ticket ~${v:,.0f}")
                tg(f"DEPTH REPORT: BTC just broke {m:,}. The 66,904 ticket is now worth "
                   f"~${v:,.0f} of a max ${MAX_PAYOUT:,.0f}. Still not looking? Respect.")
    time.sleep(30)
log("stopped")
