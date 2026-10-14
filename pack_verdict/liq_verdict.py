#!/usr/bin/env python3
"""Reproducible verdict for the liquidation-fade paper ledger.

The ledger records one completed paper trade per liquidation signal. Entry and
exit prices already pay the bid/ask spread, but ``pnl_pct`` excludes exchange
fees. The pre-registered live-paper gate in THE_PLAN.md is:

    trash-only mean net return > +0.05% per signal

"Trash-only" uses the exact majors exclusion from the earlier honest replay
(``/root/pack_rider/liq_fade3.py``). The Aster fee assumption is 0.08% round
trip: two taker legs at 0.04% each.

Usage:
    python3 liq_verdict.py paper_trades.csv
    ssh root@HOST 'cat /root/liq_paper/paper_trades.csv' | \
        python3 liq_verdict.py -
    python3 liq_verdict.py --selftest
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


MAJORS = {
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "HYPEUSDT",
    "ADAUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "LTCUSDT",
    "SUIUSDT",
    "TONUSDT",
    "BCHUSDT",
}
ROUND_TRIP_FEE_PCT = 0.08
SUCCESS_GATE_PCT = 0.05
REQUIRED_COLUMNS = {"ts", "sym", "outcome", "pnl_pct"}
VALID_OUTCOMES = {"tp", "sl", "timeout"}


@dataclass(frozen=True)
class Trade:
    timestamp: str
    symbol: str
    outcome: str
    gross_pct: float

    @property
    def day(self) -> str:
        return self.timestamp[:10]

    @property
    def is_trash(self) -> bool:
        return self.symbol not in MAJORS


def read_trades(stream: TextIO) -> list[Trade]:
    reader = csv.DictReader(stream)
    if reader.fieldnames is None:
        raise ValueError("ledger has no CSV header")
    missing = REQUIRED_COLUMNS - set(reader.fieldnames)
    if missing:
        raise ValueError("ledger is missing columns: " + ", ".join(sorted(missing)))

    trades: list[Trade] = []
    for line_number, row in enumerate(reader, start=2):
        try:
            timestamp = row["ts"].strip()
            symbol = row["sym"].strip().upper()
            outcome = row["outcome"].strip().lower()
            gross_pct = float(row["pnl_pct"])
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid ledger row {line_number}: {exc}") from exc
        if len(timestamp) < 10:
            raise ValueError(f"invalid timestamp on row {line_number}: {timestamp!r}")
        if not symbol:
            raise ValueError(f"missing symbol on row {line_number}")
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"invalid outcome on row {line_number}: {outcome!r}")
        trades.append(Trade(timestamp, symbol, outcome, gross_pct))

    if not trades:
        raise ValueError("ledger has no completed trades")
    return trades


def mean(values: list[float]) -> float:
    return statistics.fmean(values)


def print_group(name: str, trades: list[Trade], fee_pct: float) -> None:
    gross = [t.gross_pct for t in trades]
    net = [value - fee_pct for value in gross]
    outcomes = Counter(t.outcome for t in trades)
    print(f"{name}: n={len(trades)}")
    print(
        f"  gross mean {mean(gross):+.6f}%  median {statistics.median(gross):+.6f}%"
    )
    print(f"  net   mean {mean(net):+.6f}%  median {statistics.median(net):+.6f}%")
    print(
        f"  net-positive {sum(value > 0 for value in net)}/{len(net)}  "
        f"outcomes tp={outcomes['tp']} sl={outcomes['sl']} timeout={outcomes['timeout']}"
    )


def render(trades: list[Trade], fee_pct: float, gate_pct: float) -> bool:
    trash = [trade for trade in trades if trade.is_trash]
    if not trash:
        raise ValueError("ledger has no trades after the pre-registered majors exclusion")

    print("LIQUIDATION-FADE PAPER VERDICT")
    print(f"fee assumption: {fee_pct:.3f}% round trip")
    print(f"success gate: trash-only net mean > {gate_pct:+.3f}% per signal")
    print("majors excluded: " + ", ".join(sorted(MAJORS)))
    print()
    print_group("ALL COMPLETED SIGNALS", trades, fee_pct)
    print()
    print_group("PRE-REGISTERED TRASH-ONLY", trash, fee_pct)

    by_day: dict[str, list[float]] = defaultdict(list)
    for trade in trash:
        by_day[trade.day].append(trade.gross_pct - fee_pct)
    print("\ntrash-only by day:")
    for day in sorted(by_day):
        values = by_day[day]
        marker = "PASS" if mean(values) > gate_pct else "fail"
        print(f"  {day}  n={len(values):3d}  net={mean(values):+.6f}%  {marker}")

    midpoint = len(trash) // 2
    first = [trade.gross_pct - fee_pct for trade in trash[:midpoint]]
    second = [trade.gross_pct - fee_pct for trade in trash[midpoint:]]
    print("\ntrash-only stability:")
    print(f"  first half  n={len(first):3d}  net={mean(first):+.6f}%")
    print(f"  second half n={len(second):3d}  net={mean(second):+.6f}%")

    net_mean = mean([trade.gross_pct - fee_pct for trade in trash])
    passed = net_mean > gate_pct
    print("\n=== VERDICT: %s ===" % ("PASS" if passed else "FAIL"))
    print(
        f"trash-only net mean {net_mean:+.6f}% "
        f"vs required > {gate_pct:+.6f}%"
    )
    if not passed:
        print("No real-money unlock at this geometry.")
    return passed


def selftest() -> None:
    rows = """ts,sym,outcome,pnl_pct
2026-01-01T00:00:00+00:00,BTCUSDT,tp,1.000
2026-01-01T00:01:00+00:00,FOOUSDT,tp,0.200
2026-01-02T00:00:00+00:00,BARUSDT,sl,-0.100
2026-01-02T00:01:00+00:00,BAZUSDT,timeout,0.000
"""
    import io

    trades = read_trades(io.StringIO(rows))
    assert len(trades) == 4
    trash = [trade for trade in trades if trade.is_trash]
    assert len(trash) == 3
    assert Counter(trade.outcome for trade in trash) == {
        "tp": 1,
        "sl": 1,
        "timeout": 1,
    }
    gross = mean([trade.gross_pct for trade in trash])
    assert abs(gross - (0.1 / 3.0)) < 1e-12
    assert abs((gross - ROUND_TRIP_FEE_PCT) - (-0.04666666666666667)) < 1e-12
    print("SELFTEST PASS (classification, per-signal mean, fees, outcomes)")


def open_input(path: str) -> tuple[TextIO, bool]:
    if path == "-":
        return sys.stdin, False
    return Path(path).open(newline=""), True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", nargs="?", help="paper_trades.csv path, or - for stdin")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--fee-pct", type=float, default=ROUND_TRIP_FEE_PCT)
    parser.add_argument("--gate-pct", type=float, default=SUCCESS_GATE_PCT)
    args = parser.parse_args()

    if args.selftest:
        selftest()
        return 0
    if not args.ledger:
        parser.error("ledger is required unless --selftest is used")
    if args.fee_pct < 0:
        parser.error("--fee-pct cannot be negative")

    stream, should_close = open_input(args.ledger)
    try:
        trades = read_trades(stream)
    finally:
        if should_close:
            stream.close()
    render(trades, args.fee_pct, args.gate_pct)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
