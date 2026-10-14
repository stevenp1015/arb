#!/usr/bin/env python3
"""Load Aster recorder shards and build causal 100 ms per-symbol caches."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import numpy as np
import polars as pl


BIN_MS = 100
MAX_STALE_MS = 2_000


def _dense_last(
    bins: np.ndarray,
    values: np.ndarray,
    nb: int,
    *,
    fill_value: float = np.nan,
) -> tuple[np.ndarray, np.ndarray]:
    """Forward-fill last observations and return their source-bin indices."""
    out = np.full(nb, fill_value, dtype=np.float32)
    source = np.full(nb, -1, dtype=np.int32)
    if len(bins) == 0:
        return out, source
    valid = (bins >= 0) & (bins < nb)
    bins = bins[valid].astype(np.int64, copy=False)
    values = values[valid]
    if len(bins) == 0:
        return out, source
    out[bins] = values.astype(np.float32, copy=False)
    source[bins] = bins.astype(np.int32, copy=False)
    np.maximum.accumulate(source, out=source)
    seen = source >= 0
    out[seen] = out[source[seen]]
    return out, source


def _sparse_values(
    bins: np.ndarray,
    values: np.ndarray,
    nb: int,
    *,
    fill_value: float,
) -> np.ndarray:
    out = np.full(nb, fill_value, dtype=np.float32)
    if len(bins):
        valid = (bins >= 0) & (bins < nb)
        out[bins[valid].astype(np.int64, copy=False)] = values[valid].astype(
            np.float32, copy=False
        )
    return out


def _bbo_bars(frame: pl.DataFrame, start_ms: int, nb: int) -> dict[str, np.ndarray]:
    if frame.is_empty():
        raise ValueError("empty BBO frame")
    grouped = (
        frame.filter(
            (pl.col("bpx") > 0)
            & (pl.col("apx") >= pl.col("bpx"))
            & (pl.col("r") >= start_ms)
            & (pl.col("r") < start_ms + nb * BIN_MS)
        )
        .sort("r")
        .with_columns(((pl.col("r") - start_ms) // BIN_MS).alias("bar"))
        .group_by("bar", maintain_order=True)
        .agg(
            pl.col("bpx").last().alias("bid"),
            pl.col("apx").last().alias("ask"),
            pl.col("bsz").last().alias("bid_size"),
            pl.col("asz").last().alias("ask_size"),
            pl.col("bpx").min().alias("bid_lo"),
            pl.col("bpx").max().alias("bid_hi"),
            pl.col("apx").min().alias("ask_lo"),
            pl.col("apx").max().alias("ask_hi"),
        )
        .sort("bar")
    )
    if grouped.is_empty():
        raise ValueError("no valid BBO rows")
    bins = grouped["bar"].to_numpy().astype(np.int64, copy=False)
    bid, source = _dense_last(bins, grouped["bid"].to_numpy(), nb)
    ask, _ = _dense_last(bins, grouped["ask"].to_numpy(), nb)
    bid_size, _ = _dense_last(bins, grouped["bid_size"].to_numpy(), nb)
    ask_size, _ = _dense_last(bins, grouped["ask_size"].to_numpy(), nb)

    # Empty bars inherit the most recently observable quote; real update bars
    # retain their within-bin extrema for pessimistic execution.
    bid_lo = bid.copy()
    bid_hi = bid.copy()
    ask_lo = ask.copy()
    ask_hi = ask.copy()
    for name, out in (
        ("bid_lo", bid_lo),
        ("bid_hi", bid_hi),
        ("ask_lo", ask_lo),
        ("ask_hi", ask_hi),
    ):
        out[bins] = grouped[name].to_numpy().astype(np.float32, copy=False)

    bar_number = np.arange(nb, dtype=np.int32)
    stale_bars = MAX_STALE_MS // BIN_MS
    dead = (source < 0) | ((bar_number - source) > stale_bars)
    bad = (~np.isfinite(bid)) | (~np.isfinite(ask)) | (bid <= 0) | (ask < bid)
    dead |= bad
    return {
        "bid": bid,
        "ask": ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "bid_lo": bid_lo,
        "bid_hi": bid_hi,
        "ask_lo": ask_lo,
        "ask_hi": ask_hi,
        "dead": dead,
    }


def _trade_bars(frame: pl.DataFrame, start_ms: int, nb: int) -> dict[str, np.ndarray]:
    if frame.is_empty():
        return {
            "trade_hi": np.full(nb, np.nan, np.float32),
            "trade_lo": np.full(nb, np.nan, np.float32),
            "buy_notional": np.zeros(nb, np.float32),
            "sell_notional": np.zeros(nb, np.float32),
        }
    grouped = (
        frame.filter(
            (pl.col("px") > 0)
            & (pl.col("sz") > 0)
            & (pl.col("r") >= start_ms)
            & (pl.col("r") < start_ms + nb * BIN_MS)
        )
        .with_columns(
            ((pl.col("r") - start_ms) // BIN_MS).alias("bar"),
            (pl.col("px") * pl.col("sz")).alias("notional"),
        )
        .group_by("bar")
        .agg(
            pl.col("px").max().alias("trade_hi"),
            pl.col("px").min().alias("trade_lo"),
            pl.when(pl.col("side") == "B")
            .then(pl.col("notional"))
            .otherwise(0.0)
            .sum()
            .alias("buy_notional"),
            pl.when(pl.col("side") == "A")
            .then(pl.col("notional"))
            .otherwise(0.0)
            .sum()
            .alias("sell_notional"),
        )
        .sort("bar")
    )
    bins = grouped["bar"].to_numpy().astype(np.int64, copy=False)
    return {
        "trade_hi": _sparse_values(
            bins, grouped["trade_hi"].to_numpy(), nb, fill_value=np.nan
        ),
        "trade_lo": _sparse_values(
            bins, grouped["trade_lo"].to_numpy(), nb, fill_value=np.nan
        ),
        "buy_notional": _sparse_values(
            bins, grouped["buy_notional"].to_numpy(), nb, fill_value=0.0
        ),
        "sell_notional": _sparse_values(
            bins, grouped["sell_notional"].to_numpy(), nb, fill_value=0.0
        ),
    }


def extract_day(
    day_dir: str | Path,
    cache_root: str | Path,
    symbols: Iterable[str],
    *,
    force: bool = False,
) -> list[Path]:
    day_dir = Path(day_dir)
    cache_root = Path(cache_root)
    cache_root.mkdir(parents=True, exist_ok=True)
    meta = json.loads((day_dir / "capture_meta.json").read_text())
    date = day_dir.name.split("_")[1]
    start_ms = int(
        np.datetime64(meta["started_utc"].replace("+00:00", "Z"), "ms").astype(np.int64)
    )
    nb = int(meta.get("duration_s", 86_400) * 1000 // BIN_MS)
    wanted = set(symbols) & set(meta.get("symbols", []))
    outputs: list[Path] = []

    for bbo_path in sorted(day_dir.glob("bbo_shard*.jsonl.zst")):
        shard = bbo_path.name.split("shard", 1)[1].split(".", 1)[0]
        trade_path = day_dir / f"trades_shard{shard}.jsonl.zst"
        bbo = pl.read_ndjson(bbo_path).filter(pl.col("c").is_in(sorted(wanted)))
        if bbo.is_empty():
            continue
        trades = (
            pl.read_ndjson(trade_path).filter(pl.col("c").is_in(sorted(wanted)))
            if trade_path.exists()
            else pl.DataFrame()
        )
        for symbol in sorted(set(bbo["c"].to_list())):
            out = cache_root / f"{date}_{symbol}.npz"
            outputs.append(out)
            if out.exists() and not force:
                continue
            b = _bbo_bars(bbo.filter(pl.col("c") == symbol), start_ms, nb)
            t = _trade_bars(
                trades.filter(pl.col("c") == symbol)
                if (not trades.is_empty() and "c" in trades.columns)
                else pl.DataFrame(),
                start_ms,
                nb,
            )
            np.savez_compressed(
                out,
                start_ms=np.int64(start_ms),
                bin_ms=np.int32(BIN_MS),
                **b,
                **t,
            )
    missing = wanted - {p.stem.split("_", 1)[1] for p in outputs}
    if missing:
        raise RuntimeError(f"{date}: missing requested symbols: {sorted(missing)}")
    return outputs


def load_cache(path: str | Path) -> dict[str, np.ndarray | int]:
    with np.load(path) as z:
        out = {k: z[k] for k in z.files}
    out["start_ms"] = int(out["start_ms"])
    out["bin_ms"] = int(out["bin_ms"])
    return out

