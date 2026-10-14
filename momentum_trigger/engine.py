#!/usr/bin/env python3
"""Signal, native-trigger, fill, exit, clustering, and inference primitives."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import exp, log, sqrt
from typing import Iterable

import numpy as np


BIN_MS = 100
PLACEMENT_LATENCY_MS = 100
COOLDOWN_MS = 30_000
MAX_SPREAD_BPS = 5.0
ORDER_NOTIONAL_USD = 1_000.0
QUEUE_TTL_MS = 2_000


@dataclass(frozen=True)
class SustainConfig:
    impulse_ms: int
    quantile: float
    sustain_ms: int
    retain: float
    flow_min: float

    @property
    def key(self) -> str:
        flow = "none" if self.flow_min <= 0 else f"{self.flow_min:.2f}"
        return (
            f"w{self.impulse_ms}_q{self.quantile:.4f}_s{self.sustain_ms}"
            f"_r{self.retain:.2f}_f{flow}"
        )


@dataclass(frozen=True)
class TriggerConfig:
    offset_bps: float
    ttl_ms: int

    @property
    def key(self) -> str:
        return f"o{self.offset_bps:g}_ttl{self.ttl_ms}"


@dataclass(frozen=True)
class ExitConfig:
    hard_stop_bps: float
    activation_bps: float
    trail_bps: float
    max_hold_ms: int

    @property
    def key(self) -> str:
        return (
            f"h{self.hard_stop_bps:g}_a{self.activation_bps:g}"
            f"_t{self.trail_bps:g}_m{self.max_hold_ms}"
        )


def config_dict(config: object) -> dict:
    return asdict(config)  # type: ignore[arg-type]


def _mid_log(data: dict) -> np.ndarray:
    return np.log((data["bid"].astype(np.float64) + data["ask"].astype(np.float64)) * 0.5)


def calibrate_thresholds(
    cache_paths: dict[tuple[str, str], str],
    dates: Iterable[str],
    symbols: Iterable[str],
    windows_ms: Iterable[int],
    quantiles: Iterable[float],
    loader,
) -> dict[str, float]:
    """Exact development-only per-symbol/window return quantiles."""
    thresholds: dict[str, float] = {}
    for symbol in symbols:
        for window_ms in windows_ms:
            w = window_ms // BIN_MS
            chunks: list[np.ndarray] = []
            for date in dates:
                data = loader(cache_paths[(date, symbol)])
                lm = _mid_log(data)
                dead = data["dead"].astype(bool)
                valid = (~dead[w:]) & (~dead[:-w]) & np.isfinite(lm[w:]) & np.isfinite(lm[:-w])
                vals = np.abs(lm[w:][valid] - lm[:-w][valid]) * 1e4
                if len(vals):
                    # Every second observation keeps the exact tail effectively
                    # unchanged while bounding peak memory on active symbols.
                    chunks.append(vals[::2].astype(np.float32, copy=False))
            if not chunks:
                raise RuntimeError(f"no calibration returns for {symbol} {window_ms}ms")
            all_values = np.concatenate(chunks)
            for q in quantiles:
                thresholds[f"{symbol}|{window_ms}|{q:.4f}"] = float(
                    np.quantile(all_values, q)
                )
    return thresholds


def _range_sum(prefix: np.ndarray, start: np.ndarray, end: np.ndarray) -> np.ndarray:
    return prefix[end + 1] - prefix[start]


def _dedup_positions(event_bars: np.ndarray, cooldown_bars: int) -> np.ndarray:
    if len(event_bars) == 0:
        return np.zeros(0, dtype=np.int64)
    keep: list[int] = []
    last = -10**18
    for pos, bar in enumerate(event_bars):
        if int(bar) >= last + cooldown_bars:
            keep.append(pos)
            last = int(bar)
    return np.asarray(keep, dtype=np.int64)


def sustain_arms(
    data: dict,
    symbol: str,
    date: str,
    config: SustainConfig,
    threshold_bps: float,
) -> list[dict]:
    """Return de-duplicated events that pass impulse, sustain, flow and spread."""
    lm = _mid_log(data)
    dead = data["dead"].astype(bool)
    n = len(lm)
    w = config.impulse_ms // BIN_MS
    s = config.sustain_ms // BIN_MS
    if n <= w + s + 2:
        return []

    ret = np.full(n, np.nan, dtype=np.float64)
    ret[w:] = lm[w:] - lm[:-w]
    abs_bps = np.abs(ret) * 1e4
    idx = np.flatnonzero((abs_bps >= threshold_bps) & np.isfinite(ret))
    idx = idx[(idx >= w) & (idx + s + PLACEMENT_LATENCY_MS // BIN_MS + 2 < n)]
    if len(idx) == 0:
        return []
    base = idx - w
    conf = idx + s
    side = np.where(ret[idx] > 0, 1, -1).astype(np.int8)

    dead_prefix = np.r_[0, np.cumsum(dead.astype(np.int32))]
    continuous = _range_sum(dead_prefix, base, conf) == 0
    retained = side * (lm[conf] - lm[base]) * 1e4
    retain_ratio = retained / np.maximum(abs_bps[idx], 1e-9)
    spread_bps = (data["ask"][conf] - data["bid"][conf]) / (
        (data["ask"][conf] + data["bid"][conf]) * 0.5
    ) * 1e4

    buy_prefix = np.r_[0.0, np.cumsum(data["buy_notional"].astype(np.float64))]
    sell_prefix = np.r_[0.0, np.cumsum(data["sell_notional"].astype(np.float64))]
    buy = _range_sum(buy_prefix, idx, conf)
    sell = _range_sum(sell_prefix, idx, conf)
    total = buy + sell
    directional = np.where(side > 0, buy, sell)
    flow_share = np.divide(
        directional,
        total,
        out=np.full_like(directional, np.nan),
        where=total > 0,
    )

    ok = continuous & (retain_ratio >= config.retain) & (spread_bps <= MAX_SPREAD_BPS)
    if config.flow_min > 0:
        ok &= flow_share >= config.flow_min
    idx, base, conf, side = idx[ok], base[ok], conf[ok], side[ok]
    abs_kept = abs_bps[idx]
    rr_kept = retain_ratio[ok]
    flow_kept = flow_share[ok]
    spread_kept = spread_bps[ok]
    if len(idx) == 0:
        return []

    take = _dedup_positions(idx, COOLDOWN_MS // BIN_MS)
    start_ms = int(data["start_ms"])
    events: list[dict] = []
    for p in take:
        events.append(
            {
                "date": date,
                "symbol": symbol,
                "signal_bar": int(idx[p]),
                "base_bar": int(base[p]),
                "confirm_bar": int(conf[p]),
                "t_ms": start_ms + int(idx[p]) * BIN_MS,
                "side": int(side[p]),
                "impulse_bps": float(abs_kept[p]),
                "threshold_bps": float(threshold_bps),
                "impulse_ratio": float(abs_kept[p] / threshold_bps),
                "retain_ratio": float(rr_kept[p]),
                "flow_share": float(flow_kept[p]),
                "spread_bps": float(spread_kept[p]),
            }
        )
    return events


def impulse_events(
    data: dict,
    symbol: str,
    date: str,
    impulse_ms: int,
    threshold_bps: float,
) -> list[dict]:
    """Impulse-only ablation with the same per-symbol cooldown."""
    lm = _mid_log(data)
    dead = data["dead"].astype(bool)
    n = len(lm)
    w = impulse_ms // BIN_MS
    ret = np.full(n, np.nan, dtype=np.float64)
    ret[w:] = lm[w:] - lm[:-w]
    abs_bps = np.abs(ret) * 1e4
    idx = np.flatnonzero(
        (abs_bps >= threshold_bps)
        & np.isfinite(ret)
        & (~dead)
        & (~np.r_[np.ones(w, dtype=bool), dead[:-w]])
    )
    idx = idx[idx + PLACEMENT_LATENCY_MS // BIN_MS + 2 < n]
    if len(idx) == 0:
        return []
    take = _dedup_positions(idx, COOLDOWN_MS // BIN_MS)
    start_ms = int(data["start_ms"])
    out: list[dict] = []
    for p in take:
        i = int(idx[p])
        out.append(
            {
                "date": date,
                "symbol": symbol,
                "signal_bar": i,
                "base_bar": i - w,
                "confirm_bar": i,
                "t_ms": start_ms + i * BIN_MS,
                "side": 1 if ret[i] > 0 else -1,
                "impulse_bps": float(abs_bps[i]),
                "threshold_bps": float(threshold_bps),
                "impulse_ratio": float(abs_bps[i] / threshold_bps),
                "retain_ratio": np.nan,
                "flow_share": np.nan,
                "spread_bps": float(
                    (data["ask"][i] - data["bid"][i])
                    / ((data["ask"][i] + data["bid"][i]) * 0.5)
                    * 1e4
                ),
            }
        )
    return out


def counterparty_entries(
    data: dict,
    events: list[dict],
    bar_field: str,
    *,
    mode: str = "counterparty_bbo",
    taker_fee_bps: float = 4.0,
    stop_slippage_bps: float = 1.0,
) -> list[dict]:
    """Attach conservative executable entry prices to event copies."""
    out: list[dict] = []
    n = len(data["bid"])
    for event in events:
        bar = int(event[bar_field])
        if bar < 0 or bar >= n or bool(data["dead"][bar]):
            continue
        side = int(event["side"])
        if side > 0:
            price = float(data["ask_hi"][bar])
            capacity = float(data["ask_size"][bar] * data["ask"][bar])
        else:
            price = float(data["bid_lo"][bar])
            capacity = float(data["bid_size"][bar] * data["bid"][bar])
        if not np.isfinite(price) or price <= 0 or capacity < ORDER_NOTIONAL_USD:
            continue
        if mode == "stop_market":
            price *= exp(side * stop_slippage_bps * 1e-4)
        row = dict(event)
        row.update(
            entry_mode=mode,
            entry_bar=bar,
            entry_price=price,
            entry_fee_bps=float(taker_fee_bps),
            top_capacity_usd=capacity,
        )
        out.append(row)
    return out


def five_second_markout(
    data: dict,
    entries: list[dict],
    *,
    exit_fee_bps: float = 4.0,
    horizon_ms: int = 5_000,
) -> list[dict]:
    out: list[dict] = []
    h = horizon_ms // BIN_MS
    n = len(data["bid"])
    for event in entries:
        end = int(event["entry_bar"]) + h
        if end >= n or bool(data["dead"][end]):
            continue
        side = int(event["side"])
        exit_price = float(data["bid"][end] if side > 0 else data["ask"][end])
        gross = side * log(exit_price / float(event["entry_price"])) * 1e4
        row = dict(event)
        row.update(
            exit_bar=end,
            exit_price=exit_price,
            gross_bps=gross,
            net_bps=gross - float(event["entry_fee_bps"]) - exit_fee_bps,
            exit_reason=f"markout_{horizon_ms}ms",
        )
        out.append(row)
    return out


def attach_triggers(
    data: dict,
    arms: list[dict],
    config: TriggerConfig,
) -> tuple[list[dict], int]:
    """Apply a venue-native contract-price continuation trigger."""
    out: list[dict] = []
    ttl = config.ttl_ms // BIN_MS
    latency = PLACEMENT_LATENCY_MS // BIN_MS
    n = len(data["bid"])
    n_triggered = 0
    for event in arms:
        i = int(event["signal_bar"])
        conf = int(event["confirm_bar"])
        side = int(event["side"])
        segment = (
            data["trade_hi"][i : conf + 1]
            if side > 0
            else data["trade_lo"][i : conf + 1]
        )
        finite = segment[np.isfinite(segment)]
        if len(finite) == 0:
            continue
        extreme = float(np.max(finite) if side > 0 else np.min(finite))
        trigger_price = extreme * exp(side * config.offset_bps * 1e-4)
        arm_bar = conf + latency
        end = min(arm_bar + ttl, n - 1)
        trigger_bar = -1
        for bar in range(arm_bar + 1, end + 1):
            px = data["trade_hi"][bar] if side > 0 else data["trade_lo"][bar]
            if np.isfinite(px) and ((side > 0 and px >= trigger_price) or (side < 0 and px <= trigger_price)):
                trigger_bar = bar
                break
        row = dict(event)
        row.update(
            arm_bar=arm_bar,
            trigger_price=trigger_price,
            trigger_bar=trigger_bar,
            trigger_offset_bps=float(config.offset_bps),
            trigger_ttl_ms=int(config.ttl_ms),
        )
        out.append(row)
        if trigger_bar >= 0:
            n_triggered += 1
    return out, n_triggered


def queue_entries(
    data: dict,
    triggered: list[dict],
    *,
    maker_fee_bps: float = 0.0,
) -> list[dict]:
    """Approximate QUEUE_1 fills using strict trade-through or queue depletion."""
    out: list[dict] = []
    ttl = QUEUE_TTL_MS // BIN_MS
    n = len(data["bid"])
    for event in triggered:
        t = int(event.get("trigger_bar", -1))
        if t < 0 or t >= n or bool(data["dead"][t]):
            continue
        side = int(event["side"])
        limit = float(data["bid"][t] if side > 0 else data["ask"][t])
        queue_ahead = float(
            (data["bid_size"][t] if side > 0 else data["ask_size"][t]) * limit
        )
        consumed = 0.0
        fill_bar = -1
        for bar in range(t + 1, min(t + ttl, n - 1) + 1):
            trade_px = data["trade_lo"][bar] if side > 0 else data["trade_hi"][bar]
            if not np.isfinite(trade_px):
                continue
            strict = trade_px < limit if side > 0 else trade_px > limit
            touched = trade_px <= limit if side > 0 else trade_px >= limit
            if strict:
                fill_bar = bar
                break
            if touched:
                consumed += float(
                    data["sell_notional"][bar]
                    if side > 0
                    else data["buy_notional"][bar]
                )
                if consumed >= queue_ahead + ORDER_NOTIONAL_USD:
                    fill_bar = bar
                    break
        if fill_bar < 0:
            continue
        row = dict(event)
        row.update(
            entry_mode="queue_bbo",
            entry_bar=fill_bar,
            entry_price=limit,
            entry_fee_bps=float(maker_fee_bps),
            queue_ahead_usd=queue_ahead,
        )
        out.append(row)
    return out


def trailing_exits(
    data: dict,
    entries: list[dict],
    config: ExitConfig,
    *,
    exit_fee_bps: float = 4.0,
) -> list[dict]:
    """Pessimistic 100 ms hard-stop plus activated trailing-stop replay."""
    out: list[dict] = []
    n = len(data["bid"])
    horizon = config.max_hold_ms // BIN_MS
    for event in entries:
        e = int(event["entry_bar"])
        if e + 1 >= n:
            continue
        side = int(event["side"])
        entry = float(event["entry_price"])
        end = min(e + horizon, n - 1)
        peak = entry
        activated = False
        exit_bar = end
        exit_reason = "timeout"
        exit_price = float(data["bid"][end] if side > 0 else data["ask"][end])
        hard = entry * exp(-side * config.hard_stop_bps * 1e-4)
        for bar in range(e + 1, end + 1):
            if bool(data["dead"][bar]):
                exit_bar = bar
                exit_reason = "data_gap"
                exit_price = float(data["bid"][bar] if side > 0 else data["ask"][bar])
                break
            favorable = float(data["bid_hi"][bar] if side > 0 else data["ask_lo"][bar])
            unfavorable = float(data["bid_lo"][bar] if side > 0 else data["ask_hi"][bar])
            peak = max(peak, favorable) if side > 0 else min(peak, favorable)
            if side * log(peak / entry) * 1e4 >= config.activation_bps:
                activated = True
            trail = peak * exp(-side * config.trail_bps * 1e-4)
            stop = max(hard, trail) if side > 0 and activated else hard
            if side < 0 and activated:
                stop = min(hard, trail)
            hit = unfavorable <= stop if side > 0 else unfavorable >= stop
            if hit:
                exit_bar = bar
                exit_reason = "trailing" if activated and stop != hard else "hard_stop"
                # The adverse within-bin extreme is pessimistic and resolves
                # any high/low ordering ambiguity against the strategy.
                exit_price = unfavorable
                break
        if not np.isfinite(exit_price) or exit_price <= 0:
            continue
        gross = side * log(exit_price / entry) * 1e4
        row = dict(event)
        row.update(
            exit_bar=exit_bar,
            exit_price=exit_price,
            gross_bps=gross,
            net_bps=gross - float(event["entry_fee_bps"]) - exit_fee_bps,
            exit_reason=exit_reason,
            hold_ms=(exit_bar - e) * BIN_MS,
        )
        out.append(row)
    return out


def stability_score(rows: list[dict]) -> dict[str, float]:
    """Equal-weight symbol-day mean minus half a standard error."""
    groups: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        groups.setdefault((row["date"], row["symbol"]), []).append(float(row["net_bps"]))
    means = np.asarray([np.mean(v) for v in groups.values()], dtype=np.float64)
    if len(means) == 0:
        return {"score": -np.inf, "mean": np.nan, "se": np.nan, "groups": 0}
    se = float(np.std(means, ddof=1) / sqrt(len(means))) if len(means) > 1 else np.inf
    mean = float(np.mean(means))
    return {"score": mean - 0.5 * se, "mean": mean, "se": se, "groups": len(means)}


def cluster_events(rows: list[dict], window_ms: int = 1_000) -> list[dict]:
    """Collapse market-wide bursts; keep the strongest normalized impulse."""
    if not rows:
        return []
    ordered = sorted(rows, key=lambda r: (r["date"], int(r["t_ms"])))
    clusters: list[list[dict]] = []
    current: list[dict] = []
    last_date = ""
    last_t = -10**18
    for row in ordered:
        date, t = row["date"], int(row["t_ms"])
        if current and (date != last_date or t - last_t > window_ms):
            clusters.append(current)
            current = []
        current.append(row)
        last_date, last_t = date, t
    if current:
        clusters.append(current)
    return [max(c, key=lambda r: float(r.get("impulse_ratio", 0.0))) for c in clusters]


def day_block_ci(
    rows: list[dict],
    *,
    field: str = "net_bps",
    seed: int = 73_129,
    resamples: int = 10_000,
) -> tuple[float, float, float]:
    by_day: dict[str, list[float]] = {}
    for row in rows:
        by_day.setdefault(row["date"], []).append(float(row[field]))
    days = sorted(by_day)
    if not days:
        return np.nan, np.nan, np.nan
    means = np.asarray([np.mean(by_day[d]) for d in days], dtype=np.float64)
    rng = np.random.default_rng(seed)
    sampled = means[rng.integers(0, len(means), size=(resamples, len(means)))].mean(axis=1)
    return float(means.mean()), float(np.quantile(sampled, 0.025)), float(np.quantile(sampled, 0.975))
