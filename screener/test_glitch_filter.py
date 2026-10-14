#!/usr/bin/env python3
"""Regression test for the upstream price glitches.

Binance's bulk /fapi/v1/ticker/price is served by a fleet of cache nodes and
some of them lag. Measured from tokyo2 on 2026-09-17: 89 polls over 91 seconds
turned up 26 prices that jumped out and came straight back on the very next
poll, including NEARUSDT going 2.841 -> 3.178 -> 2.841, an 11.9% round trip
that never happened.

One such sample lands in the fast buffer and then:
  * Spike reads it for a full 60 seconds, because spike() is min/max over the
    window and a single outlier owns both ends of it
  * 1m reads it whenever it happens to be the sample nearest now-60s
  * `last` shows it for one frame

which is exactly "huge numbers that show up briefly and are not real".

Run:  python3 test_glitch_filter.py
"""
import importlib.util
import sys
import types


def load_sym():
    """server.py imports `websockets`, which is not needed for Sym. Stub it."""
    for name in ("websockets", "websockets.asyncio", "websockets.asyncio.server",
                 "websockets.asyncio.client", "websockets.exceptions",
                 "websockets.datastructures", "websockets.http11"):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            mod.__path__ = []          # make it a package so submodules resolve
            # server.py does `from X import Y` for a handful of names; any
            # attribute works because none of them are touched by Sym
            mod.__getattr__ = lambda attr: object
            sys.modules[name] = mod
    spec = importlib.util.spec_from_file_location("scr", "server.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def feed(sym, prices, t0=1_000_000.0, step=0.5):
    for n, p in enumerate(prices):
        sym.tick(p, t0 + n * step)
    return t0 + (len(prices) - 1) * step


def case(name, prices, expect_max_spike, expect_last):
    m = load_sym()
    s = m.Sym(0, "NEARUSDT", "NEAR", "G", "", "", 4)
    now = feed(s, prices)
    sp = s.spike(now)
    ok_spike = sp is None or abs(sp) <= expect_max_spike
    ok_last = abs(s.last / expect_last - 1) < 1e-9
    print("  %-46s spike=%-9s last=%-10s %s"
          % (name, None if sp is None else round(sp, 2), s.last,
             "PASS" if (ok_spike and ok_last) else "FAIL"))
    return ok_spike and ok_last


def main():
    flat = [2.841] * 40
    print("real NEARUSDT glitch, replayed through the real Sym:")
    ok = True

    # the glitch: one poll out and straight back, the way it actually arrived
    ok &= case("single-poll glitch must not register as a spike",
               flat[:20] + [3.178] + flat[:20], 0.25, 2.841)

    # it flip-flopped across four consecutive polls in the real capture
    ok &= case("flip-flopping glitch must not register either",
               flat[:12] + [3.178, 2.841, 3.178, 2.841] + flat[:12], 0.25, 2.841)

    # and the thing that must SURVIVE: a real move, which persists
    real = flat[:20] + [2.90, 2.95, 3.01, 3.05, 3.10, 3.178] + [3.178] * 12
    ok &= case("a REAL 11.9% move must still show up", real, 1e9, 3.178)

    # a real move that arrives in one step and stays is still real
    ok &= case("real one-step move that holds must still show up",
               flat[:20] + [3.178] * 20, 1e9, 3.178)

    # THE DANGEROUS ONE. A violent move steps more than GLITCH_PCT on every
    # single poll, so no poll ever "confirms" the one before it by landing near
    # it. A filter that only accepts on agreement would hold every sample and
    # freeze the price at the pre-crash value for the whole move — a screener
    # that goes blind exactly when something is happening is worse than one
    # that shows the occasional fake spike.
    crash = flat[:10] + [2.78, 2.70, 2.62, 2.54, 2.46, 2.38, 2.30, 2.22] + [2.22] * 8
    ok &= case("sustained crash (>1.5% every poll) must NOT freeze",
               crash, 1e9, 2.22)

    # The same upward, cut off mid-move. The newest sample of a move this fast
    # is ALWAYS still pending — that is the deal the filter makes, one poll of
    # lag in exchange for never showing a price that did not happen. So the
    # price must be tracking the ramp, not sitting at the pre-ramp value, and
    # the final tick lands as soon as anything confirms it.
    ramp = flat[:10] + [2.90, 2.99, 3.08, 3.17, 3.27, 3.37]
    ok &= case("violent ramp, cut off mid-move, tracks within one poll",
               ramp, 1e9, 3.27)
    ok &= case("...and the held tick lands on the very next poll",
               ramp + [3.37], 1e9, 3.37)

    print("\n%s" % ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
