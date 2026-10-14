#!/usr/bin/env python3
"""
Real-time perp screener.

Universe : every pair listed as a USDT perp on Gate OR Aster that also exists
           as a Binance USD-M perp (currently ~515 pairs).
Data     : Binance USD-M futures.
           Preferred feed is the !miniTicker@arr websocket.  As of 2026-08-29
           that stream handshakes and then sends nothing from the Tokyo IP
           (Binance nulls futures WS market data for JP addresses; spot WS and
           futures REST both still work), so the live feed falls back to
           polling /fapi/v1/ticker/price at 2 Hz and /fapi/v1/ticker/24hr every
           15s.  The websocket is retried in the background and takes over
           automatically the moment it starts delivering again.
           REST klines supply the candle-aligned anchors (4H / 1D / 7D opens,
           day high/low) and seed the rolling windows at boot.
Serves   : a single HTML page + a websocket feed on one port.

No third-party deps except `websockets`.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import http.client
import json
import os
import re
import sqlite3
import ssl
import threading
import time
import traceback
import urllib.parse
import urllib.request
from typing import Any

import websockets
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")

GATE_CONTRACTS = "https://api.gateio.ws/api/v4/futures/usdt/contracts"
ASTER_INFO = "https://fapi.asterdex.com/fapi/v1/exchangeInfo"
BINANCE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BINANCE_KLINES = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_HOST = "fapi.binance.com"
PATH_PRICE = "/fapi/v1/ticker/price"      # weight 2, every symbol
PATH_24HR = "/fapi/v1/ticker/24hr"        # weight 40, every symbol
BINANCE_WS = "wss://fstream.binance.com/ws/!miniTicker@arr"
WS_STREAM = "wss://fstream.binance.com"

UA = "arb-screener/1.0"

# rolling-window buffers
FAST_MAX = 800          # ~6.5 min at the 2 Hz poll rate: 1m window + spikes
SLOW_STEP = 5.0         # seconds between slow samples
SLOW_MAX = 1120         # ~93 min: covers the 1h rolling window with slack

SPIKE_WINDOW = 60.0     # seconds: biggest excursion inside any 60s stretch

# -- upstream price glitches ---------------------------------------------
# Binance's bulk /fapi/v1/ticker/price is served by a fleet of cache nodes and
# some of them lag.  Measured from tokyo2 2026-09-17: 89 polls over 91 seconds
# carried 26 prices that jumped out and came straight back on the very next
# poll — NEARUSDT went 2.841 -> 3.178 -> 2.841, an 11.9% round trip that never
# happened, and MARA/ONE/PYTH/HOOD/TUT/AKT did smaller versions of the same.
#
# One such sample owns both ends of the 60s min/max, so Spike reads it for a
# full minute, and 1m reads it whenever it lands nearest the now-60s mark.
#
# So a price that disagrees with the previous one by more than GLITCH_PCT is
# held back for one poll instead of being trusted.  Confirmed by the next poll
# it goes in, first sample and all, so a real move keeps its true magnitude and
# arrives ~0.5s late.  Contradicted, it is dropped and never seen.
# 2.4 is Steven's call. For reference, the glitches measured on 2026-09-17 ran
# 2.49% (PYTH) to 11.86% (NEAR), so 2.4 catches all of them with little room to
# spare — a 2% glitch would still get through, and shows up as a 2% fake spike.
# Raise it to hold fewer real ticks, lower it to catch smaller glitches.
GLITCH_PCT = 2.4        # % disagreement that has to be confirmed before it counts
GLITCH_MAX_GAP = 10.0   # s; past this the previous price is too old to argue with
SPARK_POINTS = 30
SPARK_SPAN = 1800.0     # sparkline covers the last 30 minutes

# -- spike persistence ---------------------------------------------------
# Counting how often a pair spikes, rather than whether it is spiking right
# now.  Time is cut into whole non-overlapping minutes so one long move can't
# be counted twice, and each minute is scored by its own high-low excursion.
MIN_BIN = 60.0          # one bin per minute
BIN_KEEP = 6.5 * 3600   # keep a little over 6h of them
SPIKE_HIT = 1.0         # a minute counts as a spike at this % excursion
KLINE_MINS = 499        # 1m candles per backfill; 499 keeps the call at weight 2

# -- trade tape ----------------------------------------------------------
# Measured on tokyo2 2026-09-09: all 718 Binance perps on @trade run at
# 2319 msg/s for 14% of one core, so the whole universe fits on one socket.
BIG_PRINT = 50_000.0    # USD notional that counts as a large trade
FLOW_BIN = 10.0         # seconds per buy/sell bucket
FLOW_SPAN = 300.0       # max-print and buy/sell pressure look back this far
BIG_SPAN = 3600.0       # large-print count looks back this far

MULT_RE = re.compile(r"^(1000000|100000|10000|1000|1M|1B|1K)(.+)$")


def strip_mult(base: str) -> str:
    """1000PEPE and PEPE are the same asset for listing-overlap purposes."""
    m = MULT_RE.match(base)
    return m.group(2) if m else base


def log(*a: Any) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- REST helpers

_SSL = ssl.create_default_context()


def http_json(url: str, timeout: float = 25.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
        return json.loads(r.read().decode())


class KeepAlive:
    """One long-lived TLS connection, reused for the high-frequency polls.
    Re-dialling twice a second would cost more than the request itself."""

    def __init__(self, host: str) -> None:
        self.host = host
        self.conn: http.client.HTTPSConnection | None = None
        self.lock = threading.Lock()
        self.reconnects = 0

    def get(self, path: str) -> Any:
        with self.lock:
            last: Exception | None = None
            for attempt in (0, 1):
                try:
                    if self.conn is None:
                        self.conn = http.client.HTTPSConnection(
                            self.host, timeout=15, context=_SSL)
                        self.reconnects += 1
                    self.conn.request("GET", path, headers={
                        "User-Agent": UA, "Accept": "application/json",
                        "Connection": "keep-alive"})
                    r = self.conn.getresponse()
                    body = r.read()
                    if r.status != 200:
                        raise RuntimeError(f"http {r.status} {body[:160]!r}")
                    return json.loads(body)
                except Exception as e:
                    last = e
                    try:
                        if self.conn:
                            self.conn.close()
                    except Exception:
                        pass
                    self.conn = None
            raise last if last else RuntimeError("unreachable")


class Limiter:
    """Binance's binding limit on these endpoints is REQUESTS per minute per IP
    (2400), not the documented weight budget — a 429 from 2026-08-29 says so in
    as many words.  So count requests, and leave a wide margin."""

    def __init__(self, per_min: int = 900) -> None:
        self.cap = per_min
        self.used: list[float] = []
        self.lock = asyncio.Lock()
        self.blocked_until = 0.0

    async def take(self, n: int = 1) -> None:
        async with self.lock:
            while True:
                now = time.time()
                if now < self.blocked_until:
                    await asyncio.sleep(min(1.0, self.blocked_until - now))
                    continue
                cut = now - 60.0
                if self.used and self.used[0] < cut:
                    k = bisect.bisect_left(self.used, cut)
                    del self.used[:k]
                if len(self.used) + 1 <= self.cap:
                    self.used.append(now)
                    return
                await asyncio.sleep(0.2)

    def penalize(self, secs: float) -> None:
        self.blocked_until = max(self.blocked_until, time.time() + secs)


# ---------------------------------------------------------------- symbol state


class Sym:
    __slots__ = (
        "i", "symbol", "base", "venues", "gate_name", "aster_symbol", "prec",
        "last", "q24", "h24", "l24", "seen", "changed",
        "fts", "fpx", "sts", "spx", "sq", "pending", "glitches",
        "next_slow",
        "day_open", "day_high", "day_low", "day_qv0", "open_4h", "open_1h", "open_7d",
        "anchor_ok", "seeded",
        "hour_qv", "hour_open", "hour_ref_q24", "med_hour_qv",
        "bins_ts", "bins_ex", "med_min_ex", "next_bin",
        "flow_ts", "flow_buy", "flow_sell", "flow_max", "big_ts",
        "low_7d", "low_7d_ts", "high_7d", "high_7d_ts",
    )

    def __init__(self, i: int, symbol: str, base: str, venues: str,
                 gate_name: str, aster_symbol: str, prec: int) -> None:
        self.i = i
        self.symbol = symbol
        self.base = base
        self.venues = venues
        self.gate_name = gate_name
        self.aster_symbol = aster_symbol
        self.prec = prec

        self.last = 0.0
        self.q24 = 0.0
        self.h24 = 0.0
        self.l24 = 0.0
        self.seen = 0.0
        self.changed = 0.0

        self.fts: list[float] = []
        self.fpx: list[float] = []
        # a price that disagrees with the last one by more than GLITCH_PCT waits
        # here for one poll to see whether the next one backs it up
        self.pending: tuple[float, float] | None = None
        self.glitches = 0
        self.sts: list[float] = []
        self.spx: list[float] = []
        self.sq: list[float] = []
        self.next_slow = 0.0

        self.day_open = 0.0
        self.day_high = 0.0
        self.day_low = 0.0
        self.day_qv0 = 0.0     # 24h-rolling quote vol observed when day volume was read
        self.open_4h = 0.0
        self.open_1h = 0.0
        self.open_7d = 0.0
        self.anchor_ok = False
        self.seeded = False

        self.hour_qv = 0.0        # quote volume inside the current hour candle
        self.hour_open = 0.0      # epoch seconds that hour started
        self.hour_ref_q24 = 0.0   # 24h volume when hour_qv was read
        self.med_hour_qv = 0.0    # median completed-hour volume over the last day

        # one entry per completed minute: when it started, and how far price
        # travelled inside it as a percentage
        self.bins_ts: list[float] = []
        self.bins_ex: list[float] = []
        self.med_min_ex = 0.0     # this pair's own typical minute, for normalising
        self.next_bin = 0.0

        # trade tape, kept as buckets rather than raw prints: BTC alone does
        # ~75 trades a second and storing them all would be pointless
        self.flow_ts: list[float] = []
        self.flow_buy: list[float] = []
        self.flow_sell: list[float] = []
        self.flow_max: list[float] = []
        self.big_ts: list[float] = []   # timestamps of prints >= BIG_PRINT

        # where the pair sits in a multi-day move: how far off its 7-day low,
        # and how long ago that low was
        self.low_7d = 0.0
        self.low_7d_ts = 0.0
        self.high_7d = 0.0
        self.high_7d_ts = 0.0

    # -- ingest ------------------------------------------------------------
    def tick(self, px: float, now: float, q: float | None = None,
             h: float | None = None, l: float | None = None) -> None:
        # -- upstream glitch gate (see GLITCH_PCT) --------------------------
        # A lagging Binance cache node hands back a price from minutes ago. It
        # reverts on the very next poll, so one poll of patience tells a glitch
        # from a real move. Nothing here runs unless a price disagrees with the
        # one before it by more than GLITCH_PCT.
        if px <= 0:
            return
        if self.last > 0 and (now - self.seen) <= GLITCH_MAX_GAP:
            off = abs(px / self.last - 1.0) * 100.0
            held = self.pending
            if held is not None:
                # Something was waiting. Two ways this poll backs it up.
                settled = abs(px / held[0] - 1.0) * 100.0 <= GLITCH_PCT
                # A violent move steps more than GLITCH_PCT on EVERY poll, so
                # it never "settles" next to the sample before it. What it does
                # instead is keep going: each price is further from the last
                # good one, the same way. Requiring agreement alone would hold
                # every sample of a crash and freeze the price at its pre-crash
                # value — blind exactly when something is happening.
                moved = px - self.last
                held_moved = held[0] - self.last
                carrying_on = (moved * held_moved > 0
                               and abs(moved) >= abs(held_moved))
                if settled or carrying_on:
                    # admit the held sample first, so the move keeps its real
                    # size and its real start time
                    self._commit(held[0], held[1])
                    self.pending = None
                else:
                    # it went out and came back: a glitch, and it never existed
                    self.pending = None
                    self.glitches += 1
                    if off > GLITCH_PCT:
                        # this one disagrees with the last good price too, so it
                        # is the next candidate rather than something to trust
                        self.pending = (px, now)
                        return
            elif off > GLITCH_PCT:
                self.pending = (px, now)
                # the 24h figures are not the thing that glitches, and dropping
                # them would leave the volume columns a poll staler for nothing
                if q is not None:
                    self.q24 = q
                if h is not None:
                    self.h24 = h
                if l is not None:
                    self.l24 = l
                return
        self._commit(px, now, q, h, l)

    def _commit(self, px: float, now: float,
                q: float | None = None, h: float | None = None,
                l: float | None = None) -> None:
        # we poll every symbol on a fixed cadence, so "when did we last hear
        # about it" is meaningless.  What's useful is when the price last
        # actually moved — that's what marks a pair as dead.
        if px != self.last:
            self.changed = now
        self.last = px
        if q is not None:
            self.q24 = q
        if h is not None:
            self.h24 = h
        if l is not None:
            self.l24 = l
        self.seen = now

        self.fts.append(now)
        self.fpx.append(px)
        if len(self.fts) > FAST_MAX * 2:
            del self.fts[:-FAST_MAX]
            del self.fpx[:-FAST_MAX]

        if now >= self.next_slow:
            self.next_slow = now + SLOW_STEP
            self.sts.append(now)
            self.spx.append(px)
            # q is None on a price-only poll; store the last known 24h volume
            # so the volume-delta columns never see a hole
            self.sq.append(self.q24)
            if len(self.sts) > SLOW_MAX * 2:
                del self.sts[:-SLOW_MAX]
                del self.spx[:-SLOW_MAX]
                del self.sq[:-SLOW_MAX]

        if self.day_open:
            if px > self.day_high:
                self.day_high = px
            if self.day_low <= 0 or px < self.day_low:
                self.day_low = px

    def seed(self, pts: list[tuple[float, float, float]]) -> None:
        """Backfill history from klines so the rolling windows are populated
        the moment the page opens, instead of an hour later.  Live samples may
        already exist (the price poller starts immediately), so only splice in
        the seed points that predate what we already have."""
        if not pts:
            return
        pts.sort()
        if self.sts:
            cutoff = self.sts[0]
            pts = [p for p in pts if p[0] < cutoff]
            if not pts:
                self.seeded = True
                return
        self.sts[:0] = [p[0] for p in pts]
        self.spx[:0] = [p[1] for p in pts]
        self.sq[:0] = [p[2] for p in pts]
        self.seeded = True

    # -- reads -------------------------------------------------------------
    def _px_at(self, target: float) -> float:
        """Price at (or just before) `target` epoch seconds."""
        if self.fts and self.fts[0] <= target:
            k = bisect.bisect_right(self.fts, target) - 1
            if k >= 0:
                return self.fpx[k]
        if not self.sts:
            return 0.0
        k = bisect.bisect_right(self.sts, target) - 1
        if k < 0:
            return 0.0
        # refuse to quote a window we don't actually have history for
        if target - self.sts[k] > 180.0 and k == 0:
            return 0.0
        return self.spx[k]

    def rolling(self, now: float, lag: float) -> float | None:
        base = self._px_at(now - lag)
        if base <= 0 or self.last <= 0:
            return None
        return (self.last / base - 1.0) * 100.0

    def aligned(self, anchor: float) -> float | None:
        if anchor <= 0 or self.last <= 0:
            return None
        return (self.last / anchor - 1.0) * 100.0

    def spike(self, now: float) -> float | None:
        """Largest signed excursion within the last SPIKE_WINDOW seconds."""
        if not self.fts:
            return None
        k = bisect.bisect_left(self.fts, now - SPIKE_WINDOW)
        px = self.fpx[k:]
        if len(px) < 3:
            return None
        lo = min(px)
        hi = max(px)
        if lo <= 0:
            return None
        up = (hi / lo - 1.0) * 100.0
        if up < 0.01:
            return 0.0
        return up if px.index(hi) > px.index(lo) else -up

    # -- spike persistence -------------------------------------------------
    def add_bin(self, ts: float, ex: float) -> None:
        if self.bins_ts and ts <= self.bins_ts[-1]:
            return
        self.bins_ts.append(ts)
        self.bins_ex.append(ex)
        if len(self.bins_ts) > 600:
            k = bisect.bisect_left(self.bins_ts, ts - BIN_KEEP)
            if k:
                del self.bins_ts[:k]
                del self.bins_ex[:k]

    def close_bin(self, now: float):
        """Score the minute that just ended and file it away.

        Returns the finished bar as (ts, open, high, low, close) so the caller
        can persist it, or None if the minute had too few samples to trust.
        """
        cur = (now // MIN_BIN) * MIN_BIN
        if self.next_bin == 0.0:
            self.next_bin = cur
            return None
        if cur <= self.next_bin:
            return None
        start = self.next_bin
        self.next_bin = cur
        if not self.fts:
            return None
        a = bisect.bisect_left(self.fts, start)
        b = bisect.bisect_left(self.fts, start + MIN_BIN)
        px = self.fpx[a:b]
        if len(px) < 3:
            return None
        lo = min(px)
        if lo <= 0:
            return None
        hi = max(px)
        self.add_bin(start, (hi / lo - 1.0) * 100.0)
        return (int(start), px[0], hi, lo, px[-1])

    def spike_hits(self, now: float, span: float) -> int:
        """How many separate minutes in the window moved at least SPIKE_HIT."""
        k = bisect.bisect_left(self.bins_ts, now - span)
        n = 0
        for j in range(k, len(self.bins_ex)):
            if self.bins_ex[j] >= SPIKE_HIT:
                n += 1
        return n

    def rspike(self, now: float) -> float | None:
        """Current 60s excursion as a multiple of this pair's own typical
        minute.  A raw 2% lurch means something very different on BTC than on
        an illiquid alt; this makes the number comparable across pairs, the
        same way rvol does for volume."""
        if self.med_min_ex <= 0:
            return None
        sp = self.spike(now)
        if sp is None:
            return None
        return abs(sp) / self.med_min_ex

    # -- where in the multi-day move --------------------------------------
    def off_low_7d(self) -> float | None:
        """How far above its 7-day low the price is. A pair up 180% off the
        low that bottomed five days ago is a very different trade from one up
        60% that bottomed yesterday, and the screener showed them the same."""
        if self.low_7d <= 0 or self.last <= 0:
            return None
        return (self.last / self.low_7d - 1.0) * 100.0

    def low_age_days(self, now: float) -> float | None:
        if not self.low_7d_ts:
            return None
        return (now - self.low_7d_ts) / 86400.0

    def off_high_7d(self) -> float | None:
        if self.high_7d <= 0 or self.last <= 0:
            return None
        return (self.last / self.high_7d - 1.0) * 100.0

    # -- trade tape --------------------------------------------------------
    def note_trade(self, ts: float, notional: float, is_buy: bool) -> None:
        b = (ts // FLOW_BIN) * FLOW_BIN
        if not self.flow_ts or b > self.flow_ts[-1]:
            self.flow_ts.append(b)
            self.flow_buy.append(0.0)
            self.flow_sell.append(0.0)
            self.flow_max.append(0.0)
            keep = int(FLOW_SPAN / FLOW_BIN) + 2
            if len(self.flow_ts) > keep * 3:
                del self.flow_ts[:-keep]
                del self.flow_buy[:-keep]
                del self.flow_sell[:-keep]
                del self.flow_max[:-keep]
        if is_buy:
            self.flow_buy[-1] += notional
        else:
            self.flow_sell[-1] += notional
        if notional > self.flow_max[-1]:
            self.flow_max[-1] = notional
        if notional >= BIG_PRINT:
            self.big_ts.append(ts)
            if len(self.big_ts) > 4000:
                del self.big_ts[:-2000]

    def max_print(self, now: float) -> float | None:
        cut = now - FLOW_SPAN
        m = 0.0
        for t, v in zip(self.flow_ts, self.flow_max):
            if t >= cut and v > m:
                m = v
        return m if m > 0 else None

    def big_count(self, now: float) -> int:
        k = bisect.bisect_left(self.big_ts, now - BIG_SPAN)
        return len(self.big_ts) - k

    def flow(self, now: float) -> float | None:
        """Share of recent notional that hit the ask rather than the bid,
        as -100 (all aggressive selling) to +100 (all aggressive buying)."""
        cut = now - FLOW_SPAN
        buy = sell = 0.0
        for t, b, s in zip(self.flow_ts, self.flow_buy, self.flow_sell):
            if t >= cut:
                buy += b
                sell += s
        tot = buy + sell
        if tot <= 0:
            return None
        return (buy - sell) / tot * 100.0

    def vol_rate(self, now: float) -> float | None:
        """Quote volume the current hour is on pace for.

        Taken from the hourly candle, not by differencing the rolling 24h
        counter — that counter falls whenever old volume ages out of the
        window, which reads as "zero volume" on exactly the pairs that spiked
        a day ago.  The hourly candle only goes up.

        Refreshed with the anchors every 4 minutes, then nudged live by
        whatever the 24h counter has gained since, so a burst shows up
        straight away instead of on the next refresh.
        """
        if not self.hour_open or self.hour_qv <= 0:
            return None
        HOUR = 3600.0
        cur_hour_open = (now // HOUR) * HOUR
        if cur_hour_open > self.hour_open:
            # the hour rolled over since the last refresh; only the live gain counts
            base = 0.0
            start = cur_hour_open
        else:
            base = self.hour_qv
            start = self.hour_open
        # only trust the live nudge once we have a real reference reading;
        # at boot the anchors land before the first volume poll, and treating
        # a reference of zero as valid adds the entire 24h volume to this hour
        gain = 0.0
        if self.hour_ref_q24 > 0:
            g = self.q24 - self.hour_ref_q24
            if g > 0:
                gain = g
        vol = base + gain
        elapsed = now - start
        if elapsed < 60.0:
            elapsed = 60.0
        return vol * HOUR / elapsed

    def rvol(self, now: float) -> float | None:
        rate = self.vol_rate(now)
        if rate is None or self.med_hour_qv <= 0:
            return None
        return rate / self.med_hour_qv

    def spark(self, now: float) -> list[float]:
        if not self.sts or self.last <= 0:
            return []
        step = SPARK_SPAN / SPARK_POINTS
        out: list[float] = []
        for n in range(SPARK_POINTS):
            t = now - SPARK_SPAN + step * (n + 1)
            p = self._px_at(t)
            out.append(p)
        if not any(out):
            return []
        first = next((p for p in out if p > 0), 0.0)
        if first <= 0:
            return []
        return [round((p / first - 1.0) * 100.0, 3) if p > 0 else 0.0 for p in out]


# ---------------------------------------------------------------- bar store
class BarStore:
    """One-minute bars on disk, so history survives a restart and reaches back
    further than the in-memory buffers.

    The rolling buffers hold ~93 minutes and die with the process. Campaigns
    run for days. 517 pairs x 1440 minutes is ~744k rows a day, roughly 30 MB,
    against 24 GB free on the box — cheap enough that not doing it was only
    ever a hangover from the old disk-full server.

    Everything here is best-effort: if the database misbehaves the screener
    carries on without it. Nothing on the live path may depend on it.
    """

    def __init__(self, path: str, keep_days: float = 30.0) -> None:
        self.path = path
        self.keep = keep_days * 86400.0
        self.ok = False
        self.rows = 0
        self.lock = threading.Lock()
        self.db = None
        try:
            self.db = sqlite3.connect(path, check_same_thread=False)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS bars ("
                "sym TEXT NOT NULL, ts INTEGER NOT NULL,"
                "o REAL, h REAL, l REAL, c REAL,"
                "PRIMARY KEY (sym, ts)) WITHOUT ROWID")
            self.db.commit()
            self.ok = True
        except Exception as e:
            log(f"bar store disabled: {e!r}")

    def write(self, batch) -> None:
        """batch is (sym, ts, o, h, l, c) tuples."""
        if not self.ok or not batch:
            return
        try:
            with self.lock:
                self.db.executemany(
                    "INSERT OR REPLACE INTO bars(sym,ts,o,h,l,c) VALUES (?,?,?,?,?,?)", batch)
                self.db.commit()
            self.rows += len(batch)
        except Exception as e:
            log(f"bar write failed: {e!r}")

    def extremes_since(self, ts: float):
        """{sym: (high, low, first_open)} across every bar at or after ts.

        This is what makes "best and worst since I marked" exact rather than
        only-while-the-tab-was-open.
        """
        if not self.ok:
            return {}
        try:
            with self.lock:
                cur = self.db.execute(
                    "SELECT sym, MAX(h), MIN(l) FROM bars WHERE ts >= ? GROUP BY sym",
                    (int(ts),))
                return {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        except Exception as e:
            log(f"bar read failed: {e!r}")
            return {}

    def series(self, sym: str, since: float, step: int = 3600):
        """Down-sampled closes for one pair, for a long-range sparkline."""
        if not self.ok:
            return []
        try:
            with self.lock:
                cur = self.db.execute(
                    "SELECT ts/? AS b, MIN(ts), AVG(c) FROM bars "
                    "WHERE sym=? AND ts>=? GROUP BY b ORDER BY b", (step, sym, int(since)))
                return [(int(r[1]), r[2]) for r in cur.fetchall()]
        except Exception:
            return []

    def prune(self) -> None:
        if not self.ok:
            return
        try:
            with self.lock:
                self.db.execute("DELETE FROM bars WHERE ts < ?",
                                (int(time.time() - self.keep),))
                self.db.commit()
        except Exception as e:
            log(f"bar prune failed: {e!r}")

    def stats(self):
        if not self.ok:
            return {"ok": False}
        try:
            with self.lock:
                n, lo = self.db.execute("SELECT COUNT(*), MIN(ts) FROM bars").fetchone()
            size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
            return {"ok": True, "bars": n or 0, "mb": round(size / 1e6, 1),
                    "oldest_h": round((time.time() - lo) / 3600, 1) if lo else None}
        except Exception:
            return {"ok": False}


# ---------------------------------------------------------------- the screener


class Screener:
    def __init__(self) -> None:
        self.syms: dict[str, Sym] = {}
        self.by_i: list[Sym] = []
        self.clients: set[Any] = set()
        self.limiter = Limiter()
        self.fast_http = KeepAlive(BINANCE_HOST)
        self.stats_http = KeepAlive(BINANCE_HOST)
        self.ws_up = False
        self.ws_msgs = 0
        self.last_ws_msg = 0.0
        self.src = "starting"
        self.poll_ok = 0
        self.poll_err = 0
        self.last_poll = 0.0
        self.poll_ms = 0.0
        self.boot_ts = time.time()
        self.anchor_ts = 0.0
        self.skipped = {"gate_only": 0, "aster_only": 0}
        self.universe_ts = 0.0
        self.trades_up = False
        self.trade_msgs = 0
        self.last_trade_msg = 0.0
        self.bins_ts = 0.0
        self.bars = None            # set in main(), may stay None if disabled

    # -- universe ----------------------------------------------------------
    async def build_universe(self) -> None:
        gate, aster, bn = await asyncio.gather(
            asyncio.to_thread(http_json, GATE_CONTRACTS),
            asyncio.to_thread(http_json, ASTER_INFO),
            asyncio.to_thread(http_json, BINANCE_INFO),
        )

        gate_map: dict[str, str] = {}
        for c in gate:
            name = c.get("name", "")
            if c.get("in_delisting") or not name.endswith("_USDT"):
                continue
            gate_map[strip_mult(name[:-5])] = name

        aster_map: dict[str, str] = {}
        for s in aster.get("symbols", []):
            if s.get("status") != "TRADING" or s.get("contractType") != "PERPETUAL":
                continue
            if s.get("quoteAsset") != "USDT":
                continue
            aster_map[strip_mult(s["baseAsset"])] = s["symbol"]

        rows: list[tuple[str, str, str, str, str, int]] = []
        bn_bases: set[str] = set()
        for s in bn.get("symbols", []):
            if s.get("status") != "TRADING" or s.get("contractType") != "PERPETUAL":
                continue
            if s.get("quoteAsset") != "USDT":
                continue
            key = strip_mult(s["baseAsset"])
            bn_bases.add(key)
            g = gate_map.get(key)
            a = aster_map.get(key)
            if not g and not a:
                continue
            venues = ("G" if g else "") + ("A" if a else "")
            rows.append((s["symbol"], s["baseAsset"], venues, g or "", a or "",
                         int(s.get("pricePrecision", 4))))

        self.skipped["gate_only"] = len(set(gate_map) - bn_bases)
        self.skipped["aster_only"] = len(set(aster_map) - bn_bases)

        rows.sort(key=lambda r: r[0])
        keep = {r[0]: self.syms.get(r[0]) for r in rows}
        new_by_i: list[Sym] = []
        for i, (sym, base, venues, g, a, prec) in enumerate(rows):
            old = keep.get(sym)
            if old is not None:
                old.i = i
                old.venues = venues
                old.gate_name = g
                old.aster_symbol = a
                new_by_i.append(old)
            else:
                new_by_i.append(Sym(i, sym, base, venues, g, a, prec))
        self.by_i = new_by_i
        self.syms = {s.symbol: s for s in new_by_i}
        self.universe_ts = time.time()
        log(f"universe: {len(rows)} pairs "
            f"(gate-only w/o binance {self.skipped['gate_only']}, "
            f"aster-only w/o binance {self.skipped['aster_only']})")

    # -- anchors -----------------------------------------------------------
    async def _klines(self, symbol: str, interval: str, limit: int) -> Any:
        await self.limiter.take()
        # three Binance perps have non-ASCII base assets (币安人生, 我踏马来了, 龙虾)
        sym = urllib.parse.quote(symbol, safe="")
        url = f"{BINANCE_KLINES}?symbol={sym}&interval={interval}&limit={limit}"
        for attempt in range(3):
            try:
                return await asyncio.to_thread(http_json, url)
            except Exception as e:
                if "429" in str(e) or "418" in str(e):
                    self.limiter.penalize(15.0)
                if attempt == 2:
                    raise
                await asyncio.sleep(1.0 + attempt * 2.0)
        return None

    async def load_anchors(self, s: Sym) -> None:
        """One request for every candle-aligned number we show.

        200 hourly candles is 8.3 days, which always contains midnight 7 days
        ago no matter what time of day it is.  Hour boundaries line up with the
        4-hour and daily boundaries, so 1H / 4H / 1D opens all fall out of the
        same array, and today's high and low are just the max and min across
        the hourly candles since 00:00 UTC.
        """
        try:
            k = await self._klines(s.symbol, "1h", 200)
        except Exception:
            return
        if not k:
            return
        HOUR = 3_600_000
        DAY = 86_400_000
        now_ms = k[-1][0]
        hour_ms = (now_ms // HOUR) * HOUR
        h4_ms = (now_ms // (4 * HOUR)) * (4 * HOUR)
        day_ms = (now_ms // DAY) * DAY
        d7_ms = day_ms - 7 * DAY

        hi = 0.0
        lo = 0.0
        # the same 200 hourly candles already cover 8.3 days, so the 7-day
        # extremes are free — no extra request
        w7_lo = 0.0
        w7_lo_ts = 0.0
        w7_hi = 0.0
        w7_hi_ts = 0.0
        for row in k:
            if row[0] < now_ms - 7 * DAY:
                continue
            h = float(row[2])
            l = float(row[3])
            if w7_lo <= 0 or l < w7_lo:
                w7_lo = l
                w7_lo_ts = row[0] / 1000.0
            if h > w7_hi:
                w7_hi = h
                w7_hi_ts = row[0] / 1000.0
        s.low_7d = w7_lo
        s.low_7d_ts = w7_lo_ts
        s.high_7d = w7_hi
        s.high_7d_ts = w7_hi_ts

        hour_vols: list[float] = []
        for row in k:
            ot = row[0]
            if ot == hour_ms:
                s.hour_qv = float(row[7])
                s.hour_open = ot / 1000.0
                s.hour_ref_q24 = s.q24
            else:
                hour_vols.append(float(row[7]))
            if ot == hour_ms:
                s.open_1h = float(row[1])
            if ot == h4_ms:
                s.open_4h = float(row[1])
            if ot == day_ms:
                s.day_open = float(row[1])
            if ot == d7_ms:
                s.open_7d = float(row[1])
            if ot >= day_ms:
                h = float(row[2])
                l = float(row[3])
                if h > hi:
                    hi = h
                if lo <= 0 or l < lo:
                    lo = l
        if hi:
            s.day_high = max(hi, s.day_high or 0.0)
        if lo:
            s.day_low = lo if s.day_low <= 0 else min(s.day_low, lo)
        recent = hour_vols[-24:]
        if recent:
            recent = sorted(recent)
            s.med_hour_qv = recent[len(recent) // 2]
        s.anchor_ok = bool(s.day_open)

    async def load_seed(self, s: Sym) -> None:
        """2 hours of 1-minute candles -> a warm rolling history, so the
        1m/5m/15m/1h columns are meaningful from the first frame."""
        if s.seeded:
            return
        try:
            k = await self._klines(s.symbol, "1m", 120)
        except Exception:
            return
        if not k:
            return
        # walk the 24h-rolling quote volume backwards through the candles so
        # the volume-delta columns have a believable history too
        cum = sum(float(r[7]) for r in k)
        back = (s.q24 or 0.0) - cum
        pts: list[tuple[float, float, float]] = []
        for row in k:
            back += float(row[7])
            pts.append((row[6] / 1000.0, float(row[4]), max(back, 0.0)))
        s.seed(pts)

    async def load_bins(self, s: Sym) -> None:
        """Rebuild a pair's minute history from 1m candles.

        Each candle's high-low range is the same measurement the live spike
        counter makes, so klines and live counting agree.  Doing it this way
        means the 1h and 6h counters are correct the moment the server boots
        instead of an hour later, and a restart costs nothing.
        """
        try:
            k = await self._klines(s.symbol, "1m", KLINE_MINS)
        except Exception:
            return
        if not k or len(k) < 2:
            return
        ts_l: list[float] = []
        ex_l: list[float] = []
        bars: list[tuple] = []
        for row in k[:-1]:            # the last candle is still open
            o = float(row[1])
            if o <= 0:
                continue
            hi = float(row[2])
            lo = float(row[3])
            ts_l.append(row[0] / 1000.0)
            ex_l.append((hi - lo) / o * 100.0)
            bars.append((s.symbol, int(row[0] / 1000), o, hi, lo, float(row[4])))
        if not ex_l:
            return
        # the same candles seed the on-disk history, so a fresh box has 8
        # hours of bars immediately instead of starting empty
        if self.bars and bars:
            self.bars.write(bars)
        # klines are authoritative for everything they cover; keep only the
        # live bins that are newer than the last candle we got
        last_k = ts_l[-1]
        keep = [(t, e) for t, e in zip(s.bins_ts, s.bins_ex) if t > last_k]
        s.bins_ts = ts_l + [t for t, _ in keep]
        s.bins_ex = ex_l + [e for _, e in keep]
        srt = sorted(ex_l)
        med = srt[len(srt) // 2]
        if med <= 0:
            pos = [x for x in srt if x > 0]
            med = pos[len(pos) // 2] if pos else 0.0
        s.med_min_ex = med

    async def refresh_bins(self) -> None:
        t0 = time.time()
        sem = asyncio.Semaphore(8)

        async def one(s: Sym) -> None:
            async with sem:
                await self.load_bins(s)

        await asyncio.gather(*(one(s) for s in list(self.by_i)), return_exceptions=True)
        ok = sum(1 for s in self.by_i if s.med_min_ex > 0)
        log(f"spike history refreshed {ok}/{len(self.by_i)} in {time.time() - t0:.1f}s")

    async def bins_loop(self) -> None:
        # every 30 min: re-truths the counters against klines and refreshes
        # each pair's typical-minute baseline.  ~1000 weight a pass, which is
        # nothing spread over half an hour.
        while True:
            await asyncio.sleep(1800)
            try:
                await self.refresh_bins()
            except Exception:
                traceback.print_exc()

    async def bin_close_loop(self) -> None:
        # close out finished minutes between kline refreshes so the counters
        # stay live rather than jumping every 30 minutes, and persist each
        # finished bar
        while True:
            await asyncio.sleep(5)
            now = time.time()
            batch = []
            for s in self.by_i:
                bar = s.close_bin(now)
                if bar:
                    batch.append((s.symbol, bar[0], bar[1], bar[2], bar[3], bar[4]))
            if batch and self.bars:
                await asyncio.to_thread(self.bars.write, batch)

    async def prune_loop(self) -> None:
        while True:
            await asyncio.sleep(6 * 3600)
            if self.bars:
                await asyncio.to_thread(self.bars.prune)

    async def trade_loop(self) -> None:
        """Raw @trade for every pair, on one socket.

        This is what turns "that spike was probably a big trade" into a
        measurement.  @aggTrade is blocked from Tokyo but @trade is not —
        re-verified from tokyo2 on 2026-09-09 at 8ms lag.
        """
        while True:
            syms = [s.symbol for s in self.by_i]
            if not syms:
                await asyncio.sleep(5)
                continue
            url = (WS_STREAM + "/stream?streams="
                   + "/".join(urllib.parse.quote(x, safe="").lower() + "@trade" for x in syms))
            n = 0
            try:
                async with websockets.connect(
                        url, open_timeout=25, ping_interval=20, ping_timeout=20,
                        max_queue=None) as ws:
                    log(f"trade tape connected for {len(syms)} pairs")
                    self.trades_up = True
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=90)
                        d = json.loads(raw).get("data")
                        if not d or d.get("e") != "trade":
                            continue
                        s = self.syms.get(d.get("s", ""))
                        if s is None:
                            continue
                        try:
                            notional = float(d["p"]) * float(d["q"])
                            ts = d["T"] / 1000.0
                        except (KeyError, ValueError, TypeError):
                            continue
                        # m=True means the buyer was the maker, so the
                        # aggressor was selling
                        s.note_trade(ts, notional, not d.get("m", False))
                        self.trade_msgs += 1
                        self.last_trade_msg = time.time()
                        n += 1
                        if (n & 8191) == 0 and len(self.by_i) != len(syms):
                            log("universe changed — reconnecting the trade tape")
                            break
            except Exception as e:
                log(f"trade tape dropped: {e!r}")
            self.trades_up = False
            await asyncio.sleep(5)

    async def refresh_anchors(self) -> None:
        t0 = time.time()
        sem = asyncio.Semaphore(10)

        async def one(s: Sym) -> None:
            async with sem:
                await self.load_anchors(s)
                if not s.seeded:
                    await self.load_seed(s)

        await asyncio.gather(*(one(s) for s in list(self.by_i)), return_exceptions=True)
        self.anchor_ts = time.time()
        ok = sum(1 for s in self.by_i if s.anchor_ok)
        log(f"anchors refreshed {ok}/{len(self.by_i)} in {time.time() - t0:.1f}s")

    async def anchor_loop(self) -> None:
        # every 4 minutes: keeps the day open correct across the UTC rollover,
        # rolls the 1H/4H opens forward, and refreshes the day high/low.
        while True:
            await asyncio.sleep(240)
            try:
                await self.refresh_anchors()
            except Exception:
                traceback.print_exc()

    async def universe_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                # ORDERED, because index i is literally position i in this list
                before = list(self.syms)
                await self.build_universe()
                after = list(self.syms)
                added = set(after) - set(before)
                removed = set(before) - set(after)
                # Rows reach the browser keyed by index, and build_universe()
                # renumbers by alphabetical position.  A delisting therefore
                # shifts every pair after it down by one.  Re-snapshot on ANY
                # change to the ordering, not just on additions: the old code
                # only checked `added`, so a pure delisting left the page
                # painting each price into its neighbour's row until something
                # else forced a snapshot.
                if after != before:
                    if added:
                        log("new listings:", ", ".join(sorted(added)))
                    if removed:
                        log("delisted:", ", ".join(sorted(removed)))
                    await self.broadcast_snapshot()
                    for sym in added:
                        s = self.syms.get(sym)
                        if s:
                            await self.load_anchors(s)
                            await self.load_seed(s)
            except Exception:
                traceback.print_exc()

    # -- feed A: binance futures websocket (preferred, currently blocked) ---
    async def stream_loop(self) -> None:
        """Keep trying fstream.  If it ever starts delivering, it wins:
        poll_price_loop stands down while ws_up is True."""
        first = True
        while True:
            try:
                async with websockets.connect(
                    BINANCE_WS, ping_interval=20, ping_timeout=20,
                    max_queue=4096, open_timeout=20,
                ) as ws:
                    # prove it actually delivers before trusting it
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=20)
                    except asyncio.TimeoutError:
                        if first:
                            log("fstream handshakes but sends nothing "
                                "(binance blocks futures WS from this IP) — using REST polling")
                            first = False
                        await ws.close()
                        await asyncio.sleep(600)
                        continue
                    self.ws_up = True
                    log("binance futures websocket is delivering — switching to it")
                    while True:
                        now = time.time()
                        self.last_ws_msg = now
                        self.ws_msgs += 1
                        try:
                            arr = json.loads(raw)
                        except Exception:
                            arr = None
                        if isinstance(arr, list):
                            syms = self.syms
                            for t in arr:
                                s = syms.get(t.get("s", ""))
                                if s is None:
                                    continue
                                try:
                                    s.tick(float(t["c"]), now, float(t["q"]),
                                           float(t["h"]), float(t["l"]))
                                except (KeyError, ValueError):
                                    continue
                        raw = await ws.recv()
            except Exception as e:
                if self.ws_up:
                    log(f"binance websocket dropped: {e!r}")
                self.ws_up = False
                await asyncio.sleep(30)

    # -- feed B: binance futures REST polling (the one that works) ---------
    async def poll_price_loop(self, hz: float) -> None:
        period = 1.0 / hz
        while True:
            t0 = time.time()
            if not self.ws_up:
                try:
                    await self.limiter.take()
                    data = await asyncio.to_thread(self.fast_http.get, PATH_PRICE)
                    now = time.time()
                    self.poll_ms = (now - t0) * 1000.0
                    self.last_poll = now
                    self.poll_ok += 1
                    syms = self.syms
                    for t in data:
                        s = syms.get(t.get("symbol", ""))
                        if s is None:
                            continue
                        try:
                            s.tick(float(t["price"]), now)
                        except (KeyError, ValueError):
                            continue
                    self.src = "rest"
                except Exception as e:
                    self.poll_err += 1
                    if "429" in str(e) or "418" in str(e):
                        self.limiter.penalize(15.0)
                    if self.poll_err % 10 == 1:
                        log(f"price poll failed: {e!r}")
                    await asyncio.sleep(2.0)
            else:
                self.src = "ws"
            slp = period - (time.time() - t0)
            if slp > 0:
                await asyncio.sleep(slp)

    async def poll_stats_once(self) -> None:
        await self.limiter.take()
        data = await asyncio.to_thread(self.stats_http.get, PATH_24HR)
        now = time.time()
        syms = self.syms
        for t in data:
            s = syms.get(t.get("symbol", ""))
            if s is None:
                continue
            try:
                s.tick(float(t["lastPrice"]), now,
                       float(t["quoteVolume"]),
                       float(t["highPrice"]), float(t["lowPrice"]))
            except (KeyError, ValueError):
                continue

    async def poll_stats_loop(self, every: float = 15.0) -> None:
        while True:
            await asyncio.sleep(every)
            if self.ws_up:
                continue
            try:
                await self.poll_stats_once()
            except Exception as e:
                if "429" in str(e) or "418" in str(e):
                    self.limiter.penalize(15.0)
                log(f"24hr poll failed: {e!r}")

    async def watchdog(self) -> None:
        while True:
            await asyncio.sleep(15)
            if self.ws_up and self.last_ws_msg and time.time() - self.last_ws_msg > 30:
                log("websocket silent >30s — dropping back to REST polling")
                self.ws_up = False

    # -- wire format -------------------------------------------------------
    def row(self, s: Sym, now: float) -> list[Any]:
        def r2(v: float | None) -> float | None:
            return None if v is None else round(v, 2)

        vph = s.vol_rate(now)
        rv = s.rvol(now)
        rsp = s.rspike(now)
        mp = s.max_print(now)
        la = s.low_age_days(now)

        day_pct = s.aligned(s.day_open)
        hi_pct = (s.day_high / s.day_open - 1.0) * 100.0 if s.day_open and s.day_high else None
        lo_pct = (s.day_low / s.day_open - 1.0) * 100.0 if s.day_open and s.day_low else None
        pos = None
        if s.day_high and s.day_low and s.day_high > s.day_low and s.last:
            pos = round((s.last - s.day_low) / (s.day_high - s.day_low) * 100.0, 1)

        return [
            s.i,
            s.last,
            r2(s.rolling(now, 60.0)),
            r2(s.rolling(now, 300.0)),
            r2(s.rolling(now, 900.0)),
            r2(s.rolling(now, 3600.0)),
            r2(s.aligned(s.open_4h)),
            r2(day_pct),
            r2(s.aligned(s.open_7d)),
            round(s.q24) if s.q24 else 0,
            round(vph) if vph is not None else None,
            round(rv, 2) if rv is not None else None,
            r2(s.spike(now)),
            r2(hi_pct),
            r2(lo_pct),
            pos,
            round(now - s.changed, 1) if s.changed else None,
            s.spike_hits(now, 3600.0),
            s.spike_hits(now, 21600.0),
            round(rsp, 1) if rsp is not None else None,
            round(mp) if mp else None,
            s.big_count(now),
            r2(s.flow(now)),
            r2(s.off_low_7d()),
            round(la, 1) if la is not None else None,
            r2(s.off_high_7d()),
        ]

    def snapshot(self) -> dict[str, Any]:
        return {
            "t": "snap",
            "ts": time.time(),
            "cols": ["i", "last", "m1", "m5", "m15", "h1", "h4", "d1", "d7",
                     "q24", "vph", "rvol", "spk", "dhi", "dlo", "pos", "age",
                     "sph1", "sph6", "rspk", "mprint", "big1h", "flow",
                     "off7lo", "loage", "off7hi"],
            "syms": [
                {"i": s.i, "s": s.symbol, "b": s.base, "v": s.venues,
                 "p": s.prec, "g": s.gate_name, "a": s.aster_symbol}
                for s in self.by_i
            ],
            "skipped": self.skipped,
            "boot": self.boot_ts,
        }

    def alive(self) -> bool:
        if self.ws_up and self.last_ws_msg:
            return time.time() - self.last_ws_msg < 30
        return bool(self.last_poll) and time.time() - self.last_poll < 15

    def status(self) -> dict[str, Any]:
        return {
            "t": "status",
            "ws": self.alive(),
            "src": self.src,
            "lat": round(self.poll_ms),
            "msgs": self.ws_msgs,
            "polls": self.poll_ok,
            "poll_err": self.poll_err,
            # upstream prices caught going out and straight back (see
            # GLITCH_PCT). Nonzero is normal and means the filter is working;
            # it was ~26 per 90s across the universe when this was measured.
            "glitches": sum(s.glitches for s in self.by_i),
            "anchor_age": round(time.time() - self.anchor_ts) if self.anchor_ts else None,
            "pairs": len(self.by_i),
            "clients": len(self.clients),
            "tape": self.trades_up,
            "tape_msgs": self.trade_msgs,
            "bars": self.bars.stats() if self.bars else {"ok": False},
            "server_ts": time.time(),
        }

    async def broadcast_snapshot(self) -> None:
        if not self.clients:
            return
        msg = json.dumps(self.snapshot())
        await asyncio.gather(*(self._send(c, msg) for c in list(self.clients)),
                             return_exceptions=True)

    @staticmethod
    async def _send(c: Any, msg: str) -> None:
        try:
            await c.send(msg)
        except Exception:
            pass

    async def push_loop(self, hz: float) -> None:
        period = 1.0 / hz
        last_sent: dict[int, tuple] = {}
        n = 0
        while True:
            await asyncio.sleep(period)
            if not self.clients:
                last_sent.clear()
                continue
            now = time.time()
            deltas = []
            for s in self.by_i:
                if not s.last:
                    continue
                try:
                    row = self.row(s, now)
                except Exception:
                    # one bad symbol must never take the dashboard down
                    traceback.print_exc()
                    continue
                key = tuple(row[1:16])
                if last_sent.get(s.i) == key:
                    continue
                last_sent[s.i] = key
                deltas.append(row)
            n += 1
            payloads = []
            if deltas:
                payloads.append(json.dumps({"t": "upd", "ts": now, "d": deltas}))
            if n % max(1, int(hz * 5)) == 0:
                payloads.append(json.dumps(self.status()))
            if n % max(1, int(hz * 10)) == 0:
                sp = {s.i: s.spark(now) for s in self.by_i if s.last}
                payloads.append(json.dumps({"t": "spark", "d": sp}))
            if not payloads:
                continue
            dead = []
            for c in list(self.clients):
                try:
                    for p in payloads:
                        await c.send(p)
                except Exception:
                    dead.append(c)
            for c in dead:
                self.clients.discard(c)

    # -- client handler ----------------------------------------------------
    async def handler(self, conn: Any) -> None:
        path = getattr(conn.request, "path", "/")
        if not path.startswith("/ws"):
            await conn.close()
            return
        self.clients.add(conn)
        try:
            await conn.send(json.dumps(self.snapshot()))
            now = time.time()
            rows = [self.row(s, now) for s in self.by_i if s.last]
            if rows:
                await conn.send(json.dumps({"t": "upd", "ts": now, "d": rows}))
            await conn.send(json.dumps({"t": "spark",
                                        "d": {s.i: s.spark(now) for s in self.by_i if s.last}}))
            await conn.send(json.dumps(self.status()))
            async for _ in conn:
                pass
        except Exception:
            pass
        finally:
            self.clients.discard(conn)


# ---------------------------------------------------------------- http glue


def make_process_request(scr: Screener):
    def process_request(conn: Any, request: Any):
        path = request.path.split("?")[0]
        if path.startswith("/ws"):
            return None
        if path in ("/", "/index.html"):
            try:
                with open(INDEX, "rb") as f:
                    body = f.read()
            except OSError:
                return _resp(conn, 500, b"index.html missing", "text/plain")
            return _resp(conn, 200, body, "text/html; charset=utf-8")
        if path == "/api/state":
            now = time.time()
            data = {
                "snap": scr.snapshot(),
                "rows": [scr.row(s, now) for s in scr.by_i if s.last],
                "status": scr.status(),
            }
            return _resp(conn, 200, json.dumps(data).encode(), "application/json")
        if path == "/api/since":
            # best and worst each pair reached since a timestamp — this is what
            # makes "what did I miss while I was away" honest. Net change hides
            # a pair that ran 35% and gave it all back; that is exactly the one
            # worth seeing.
            qs = urllib.parse.parse_qs(request.path.partition("?")[2])
            try:
                ts = float((qs.get("ts") or ["0"])[0])
            except ValueError:
                ts = 0.0
            if ts <= 0 or not scr.bars:
                return _resp(conn, 200, json.dumps({"ok": False, "d": {}}).encode(),
                             "application/json")
            # raw high/low prices, keyed by wire index. The percentage has to
            # be worked out against the price the browser marked at, which only
            # the browser knows.
            ex = scr.bars.extremes_since(ts)
            out = {}
            for s in scr.by_i:
                hl = ex.get(s.symbol)
                if not hl:
                    continue
                hi, lo = hl
                if hi and lo:
                    out[s.i] = [hi, lo]
            return _resp(conn, 200, json.dumps({"ok": True, "d": out}).encode(),
                         "application/json")
        if path == "/api/bars":
            qs = urllib.parse.parse_qs(request.path.partition("?")[2])
            sym = (qs.get("sym") or [""])[0].upper()
            try:
                hours = min(720.0, max(1.0, float((qs.get("hours") or ["168"])[0])))
            except ValueError:
                hours = 168.0
            if not scr.bars or sym not in scr.syms:
                return _resp(conn, 200, json.dumps({"d": []}).encode(), "application/json")
            step = 3600 if hours > 48 else 300
            rows = scr.bars.series(sym, time.time() - hours * 3600, step)
            return _resp(conn, 200, json.dumps({"sym": sym, "step": step, "d": rows}).encode(),
                         "application/json")
        if path == "/api/pxat":
            # Price this pair traded at some past moment, for back-dating a
            # watchlist mark.  Served straight off the rolling buffers the
            # percentage columns already run on, so it costs nothing extra and
            # reaches back only as far as those do (~93 min).
            qs = urllib.parse.parse_qs(request.path.partition("?")[2])
            sym = (qs.get("sym") or [""])[0].upper()
            try:
                ts = float((qs.get("ts") or ["0"])[0])
            except ValueError:
                ts = 0.0
            s = scr.syms.get(sym)
            if s is None:
                return _resp(conn, 404, json.dumps({"err": "unknown symbol"}).encode(),
                             "application/json")
            if ts <= 0 or ts > time.time() + 5:
                return _resp(conn, 400, json.dumps({"err": "bad timestamp"}).encode(),
                             "application/json")
            px = s._px_at(ts)
            if px <= 0:
                oldest = s.sts[0] if s.sts else None
                return _resp(conn, 404, json.dumps(
                    {"err": "no history that far back", "oldest": oldest}).encode(),
                    "application/json")
            return _resp(conn, 200, json.dumps({"sym": sym, "ts": ts, "px": px}).encode(),
                         "application/json")
        if path == "/healthz":
            return _resp(conn, 200 if scr.alive() else 503,
                         json.dumps(scr.status()).encode(), "application/json")
        return _resp(conn, 404, b"not found", "text/plain")

    return process_request


def _resp(conn: Any, status: int, body: bytes, ctype: str) -> Response:
    headers = Headers()
    headers["Content-Type"] = ctype
    headers["Content-Length"] = str(len(body))
    headers["Cache-Control"] = "no-store"
    return Response(status, "OK" if status == 200 else "ERR", headers, body)


# ---------------------------------------------------------------- main


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="127.0.0.1 (default, reach it over an SSH tunnel) or 0.0.0.0")
    ap.add_argument("--hz", type=float, default=2.0, help="browser push frames per second")
    ap.add_argument("--poll-hz", type=float, default=2.0,
                    help="binance REST price polls per second (weight 2 each, cap is 2400/min)")
    ap.add_argument("--bars-days", type=float, default=30.0,
                    help="days of 1-minute history to keep on disk (~30MB/day)")
    ap.add_argument("--no-bars", action="store_true",
                    help="disable the on-disk bar store entirely")
    args = ap.parse_args()

    scr = Screener()
    if not args.no_bars:
        scr.bars = BarStore(os.path.join(HERE, "bars.db"), args.bars_days)
        st = scr.bars.stats()
        if st.get("ok"):
            log("bar store: %s bars, %s MB on disk, oldest %sh back"
                % (st["bars"], st["mb"], st["oldest_h"]))
    log("building universe...")
    await scr.build_universe()

    # volumes first: the anchor pass records the 24h volume as its reference
    # point for the live vol/hr nudge, so it needs a real reading to sit on
    try:
        await scr.poll_stats_once()
    except Exception as e:
        log(f"initial volume poll failed: {e!r}")

    log("loading anchors + warm history for every pair...")

    async def boot_load() -> None:
        await scr.refresh_anchors()
        # spike history second: the anchor pass is what the page needs first,
        # and both hammer the same kline endpoint
        log("backfilling spike history from 1m candles...")
        await scr.refresh_bins()

    boot = asyncio.create_task(boot_load())

    tasks = [
        asyncio.create_task(scr.stream_loop()),
        asyncio.create_task(scr.poll_price_loop(args.poll_hz)),
        asyncio.create_task(scr.poll_stats_loop()),
        asyncio.create_task(scr.push_loop(args.hz)),
        asyncio.create_task(scr.anchor_loop()),
        asyncio.create_task(scr.universe_loop()),
        asyncio.create_task(scr.watchdog()),
        asyncio.create_task(scr.bins_loop()),
        asyncio.create_task(scr.bin_close_loop()),
        asyncio.create_task(scr.trade_loop()),
        asyncio.create_task(scr.prune_loop()),
        boot,
    ]

    async with serve(
        scr.handler, args.bind, args.port,
        process_request=make_process_request(scr),
        ping_interval=20, ping_timeout=20, max_size=2 ** 20,
    ):
        log(f"http+ws listening on {args.bind}:{args.port}")
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
