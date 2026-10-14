#!/usr/bin/env python3
"""Post-hoc exact-timestamp BBO sensitivity for the already-frozen holdout.

This does not replace the preregistered adverse-100ms execution model. It asks
whether selecting the first post-trigger bookTicker message materially changes
the conclusion.
"""

from __future__ import annotations

import argparse
import csv
import json
from math import log
from pathlib import Path

import numpy as np
import polars as pl

from data import load_cache
from engine import (
    ORDER_NOTIONAL_USD,
    ExitConfig,
    SustainConfig,
    TriggerConfig,
    attach_triggers,
    cluster_events,
    day_block_ci,
    five_second_markout,
    sustain_arms,
    trailing_exits,
)
from run_experiment import HOLDOUT_DATES, SYMBOLS


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    columns = sorted({key for row in rows for key in row})
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def exact_entries(data: dict, triggered: list[dict], bbo: pl.DataFrame, trades: pl.DataFrame) -> list[dict]:
    bbo = bbo.sort("r")
    trades = trades.sort("r")
    br = bbo["r"].to_numpy().astype(np.int64, copy=False)
    bid = bbo["bpx"].to_numpy()
    ask = bbo["apx"].to_numpy()
    bsz = bbo["bsz"].to_numpy()
    asz = bbo["asz"].to_numpy()
    tr = trades["r"].to_numpy().astype(np.int64, copy=False)
    tp = trades["px"].to_numpy()
    start_ms = int(data["start_ms"])
    out: list[dict] = []
    for event in triggered:
        side = int(event["side"])
        # Match the preregistered model's refusal to use the arming bin itself.
        search_start = start_ms + (int(event["arm_bar"]) + 1) * 100
        search_end = start_ms + (int(event["arm_bar"]) * 100) + int(event["trigger_ttl_ms"])
        lo = int(np.searchsorted(tr, search_start, side="left"))
        hi = int(np.searchsorted(tr, search_end, side="right"))
        if lo >= hi:
            continue
        segment = tp[lo:hi]
        hit = (
            segment >= float(event["trigger_price"])
            if side > 0
            else segment <= float(event["trigger_price"])
        )
        positions = np.flatnonzero(hit)
        if len(positions) == 0:
            continue
        trade_pos = lo + int(positions[0])
        trigger_r = int(tr[trade_pos])
        # A conditional order executes after its triggering trade. The first
        # bookTicker received at/after that trade is the least-assumptive
        # observable proxy for the post-trigger BBO.
        book_pos = int(np.searchsorted(br, trigger_r, side="left"))
        if book_pos >= len(br) or int(br[book_pos]) - trigger_r > 250:
            continue
        price = float(ask[book_pos] if side > 0 else bid[book_pos])
        capacity = float((asz[book_pos] if side > 0 else bsz[book_pos]) * price)
        if price <= 0 or capacity < ORDER_NOTIONAL_USD:
            continue
        entry_r = int(br[book_pos])
        entry_bar = max(0, min(len(data["bid"]) - 1, (entry_r - start_ms) // 100))
        conservative = float(
            data["ask_hi"][entry_bar] if side > 0 else data["bid_lo"][entry_bar]
        )
        improvement = side * log(conservative / price) * 1e4
        row = dict(event)
        row.update(
            trigger_bar=int((trigger_r - start_ms) // 100),
            exact_trigger_r=trigger_r,
            exact_bbo_r=entry_r,
            exact_bbo_lag_ms=entry_r - trigger_r,
            entry_mode="exact_post_trigger_bbo",
            entry_bar=int(entry_bar),
            entry_price=price,
            entry_fee_bps=4.0,
            top_capacity_usd=capacity,
            vs_adverse_bar_improvement_bps=improvement,
        )
        out.append(row)
    return out


def summarize(rows: list[dict]) -> dict:
    clustered = cluster_events(rows)
    mean, low, high = day_block_ci(clustered)
    gross_day_rows = [dict(row, net_bps=float(row["gross_bps"])) for row in clustered]
    gross, gross_low, gross_high = day_block_ci(gross_day_rows)
    return {
        "raw_n": len(rows),
        "clustered_n": len(clustered),
        "net_mean_bps": mean,
        "net_ci95_bps": [low, high],
        "zero_fee_gross_mean_bps": gross,
        "zero_fee_gross_ci95_bps": [gross_low, gross_high],
        "mean_entry_improvement_bps": float(
            np.mean([r["vs_adverse_bar_improvement_bps"] for r in clustered])
        )
        if clustered
        else np.nan,
        "median_post_trigger_bbo_lag_ms": float(
            np.median([r["exact_bbo_lag_ms"] for r in clustered])
        )
        if clustered
        else np.nan,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-root", type=Path, default=Path("/root/hl_data/aster"))
    parser.add_argument(
        "--cache-root", type=Path, default=Path("/root/hl_data/momentum_trigger/cache")
    )
    parser.add_argument(
        "--results-root", type=Path, default=Path("/root/hl_data/momentum_trigger/results")
    )
    args = parser.parse_args()
    frozen = json.loads((args.results_root / "frozen_config.json").read_text())
    thresholds = json.loads((args.results_root / "thresholds_dev_only.json").read_text())
    sustain_cfg = SustainConfig(**frozen["sustain"])
    trigger_cfg = TriggerConfig(**frozen["trigger"])
    exit_cfg = ExitConfig(**frozen["exit"])
    exact_markouts: list[dict] = []
    exact_exits: list[dict] = []

    for date in HOLDOUT_DATES:
        day_dir = sorted(args.raw_root.glob(f"live_{date}_*"))[0]
        for shard_path in sorted(day_dir.glob("bbo_shard*.jsonl.zst")):
            shard = shard_path.name.split("shard", 1)[1].split(".", 1)[0]
            trade_path = day_dir / f"trades_shard{shard}.jsonl.zst"
            bbo_all = pl.read_ndjson(shard_path).filter(pl.col("c").is_in(list(SYMBOLS)))
            trades_all = pl.read_ndjson(trade_path).filter(pl.col("c").is_in(list(SYMBOLS)))
            for symbol in sorted(set(bbo_all["c"].to_list())):
                data = load_cache(args.cache_root / f"{date}_{symbol}.npz")
                threshold = thresholds[
                    f"{symbol}|{sustain_cfg.impulse_ms}|{sustain_cfg.quantile:.4f}"
                ]
                arms = sustain_arms(data, symbol, date, sustain_cfg, threshold)
                triggered, _ = attach_triggers(data, arms, trigger_cfg)
                entries = exact_entries(
                    data,
                    triggered,
                    bbo_all.filter(pl.col("c") == symbol),
                    trades_all.filter(pl.col("c") == symbol),
                )
                exact_markouts.extend(five_second_markout(data, entries))
                exact_exits.extend(trailing_exits(data, entries, exit_cfg))
        print(f"exact sensitivity complete {date}", flush=True)

    clustered_markouts = cluster_events(exact_markouts)
    clustered_exits = cluster_events(exact_exits)
    _write_csv(args.results_root / "holdout_exact_bbo_5s_events.csv", clustered_markouts)
    _write_csv(args.results_root / "holdout_exact_bbo_exit_events.csv", clustered_exits)
    report = {"five_second": summarize(exact_markouts), "frozen_exit": summarize(exact_exits)}
    (args.results_root / "exact_bbo_sensitivity.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=True) + "\n"
    )
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=True))


if __name__ == "__main__":
    main()

