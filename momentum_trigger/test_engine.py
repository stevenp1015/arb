#!/usr/bin/env python3
"""Small deterministic checks for the execution engine."""

from __future__ import annotations

import numpy as np

from engine import (
    ExitConfig,
    SustainConfig,
    TriggerConfig,
    attach_triggers,
    counterparty_entries,
    day_block_ci,
    queue_entries,
    sustain_arms,
    trailing_exits,
)


def fixture() -> dict:
    n = 500
    mid = np.full(n, 100.0, dtype=np.float32)
    mid[100:106] = np.linspace(100.0, 100.20, 6)
    mid[106:120] = 100.20
    mid[120:126] = np.linspace(100.20, 100.30, 6)
    mid[126:160] = np.linspace(100.30, 100.60, 34)
    mid[160:] = 100.60
    bid = mid - 0.005
    ask = mid + 0.005
    trade_hi = np.full(n, np.nan, np.float32)
    trade_lo = np.full(n, np.nan, np.float32)
    trade_hi[90:180] = mid[90:180]
    trade_lo[90:180] = mid[90:180]
    buy = np.zeros(n, np.float32)
    sell = np.zeros(n, np.float32)
    buy[100:130] = 10_000
    sell[100:130] = 1_000
    return {
        "start_ms": 1_000_000,
        "bin_ms": 100,
        "bid": bid,
        "ask": ask,
        "bid_lo": bid.copy(),
        "bid_hi": bid.copy(),
        "ask_lo": ask.copy(),
        "ask_hi": ask.copy(),
        "bid_size": np.full(n, 100.0, np.float32),
        "ask_size": np.full(n, 100.0, np.float32),
        "dead": np.zeros(n, bool),
        "trade_hi": trade_hi,
        "trade_lo": trade_lo,
        "buy_notional": buy,
        "sell_notional": sell,
    }


def test_signal_trigger_and_exit() -> None:
    data = fixture()
    cfg = SustainConfig(500, 0.995, 1_000, 0.50, 0.55)
    arms = sustain_arms(data, "TEST", "20260611", cfg, threshold_bps=8.0)
    assert arms, "sustained impulse should arm"
    triggered, n = attach_triggers(data, arms, TriggerConfig(1.0, 3_000))
    assert n > 0
    entries = counterparty_entries(data, triggered, "trigger_bar")
    assert entries and entries[0]["entry_price"] >= data["ask"][entries[0]["entry_bar"]]
    exits = trailing_exits(data, entries, ExitConfig(20.0, 10.0, 10.0, 15_000))
    assert exits and np.isfinite(exits[0]["net_bps"])


def test_queue_requires_trade_through() -> None:
    data = fixture()
    event = {
        "date": "20260611",
        "symbol": "TEST",
        "t_ms": 1_000_000,
        "side": 1,
        "trigger_bar": 200,
        "impulse_ratio": 1.0,
    }
    data["trade_lo"][205] = data["bid"][200] - 0.01
    fills = queue_entries(data, [event])
    assert len(fills) == 1
    assert fills[0]["entry_fee_bps"] == 0.0


def test_day_block_ci() -> None:
    rows = [
        {"date": "d1", "net_bps": 1.0},
        {"date": "d1", "net_bps": 3.0},
        {"date": "d2", "net_bps": 5.0},
    ]
    mean, low, high = day_block_ci(rows, resamples=1_000)
    assert mean == 3.5
    assert low <= mean <= high


def main() -> None:
    test_signal_trigger_and_exit()
    test_queue_requires_trade_through()
    test_day_block_ci()
    print("selftest: ok")


if __name__ == "__main__":
    main()

