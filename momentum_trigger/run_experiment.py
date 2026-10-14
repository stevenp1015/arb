#!/usr/bin/env python3
"""Run the preregistered Aster sustained-momentum experiment end to end."""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict
from itertools import product
from pathlib import Path

import numpy as np

from data import extract_day, load_cache
from engine import (
    BIN_MS,
    PLACEMENT_LATENCY_MS,
    ExitConfig,
    SustainConfig,
    TriggerConfig,
    attach_triggers,
    calibrate_thresholds,
    cluster_events,
    counterparty_entries,
    day_block_ci,
    five_second_markout,
    impulse_events,
    queue_entries,
    stability_score,
    sustain_arms,
    trailing_exits,
)


SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "HYPEUSDT",
    "ASTERUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
)
DEV_DATES = tuple(f"202606{x:02d}" for x in range(11, 21))
HOLDOUT_DATES = tuple(f"202606{x:02d}" for x in range(21, 31))
IMPULSE_WINDOWS_MS = (500, 1_000, 2_000)
QUANTILES = (0.995, 0.999)
SUSTAIN_WINDOWS_MS = (500, 1_000, 2_000)
RETENTION = (0.50, 0.75, 1.00)
FLOW_MIN = (0.0, 0.55)
TRIGGER_OFFSETS_BPS = (1.0, 2.0, 5.0)
TRIGGER_TTLS_MS = (1_000, 3_000)
HARD_STOPS_BPS = (10.0, 20.0, 30.0)
ACTIVATIONS_BPS = (10.0, 20.0, 40.0)
TRAILS_BPS = (10.0, 20.0, 30.0)
MAX_HOLDS_MS = (5_000, 15_000, 30_000)


class Accumulator:
    def __init__(self) -> None:
        self.n = 0
        self.total = 0.0
        self.groups: dict[tuple[str, str], list[float]] = {}
        self.days: set[str] = set()
        self.symbols: set[str] = set()

    def update(self, rows: list[dict]) -> None:
        for row in rows:
            value = float(row["net_bps"])
            self.n += 1
            self.total += value
            key = (row["date"], row["symbol"])
            state = self.groups.setdefault(key, [0.0, 0.0])
            state[0] += value
            state[1] += 1.0
            self.days.add(row["date"])
            self.symbols.add(row["symbol"])

    def summary(self) -> dict:
        pseudo_rows = [
            {"date": d, "symbol": s, "net_bps": total / count}
            for (d, s), (total, count) in self.groups.items()
        ]
        score = stability_score(pseudo_rows)
        return {
            "n": self.n,
            "event_weighted_mean_bps": self.total / self.n if self.n else np.nan,
            "symbol_day_mean_bps": score["mean"],
            "symbol_day_se_bps": score["se"],
            "score": score["score"],
            "active_symbol_days": len(self.groups),
            "active_days": len(self.days),
            "active_symbols": len(self.symbols),
        }


def _json_dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=True) + "\n")


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    columns = sorted({key for row in rows for key in row})
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _cache_index(cache_root: Path) -> dict[tuple[str, str], str]:
    index: dict[tuple[str, str], str] = {}
    for path in cache_root.glob("*.npz"):
        date, symbol = path.stem.split("_", 1)
        index[(date, symbol)] = str(path)
    return index


def extract_all(raw_root: Path, cache_root: Path, *, force: bool = False) -> None:
    for date in DEV_DATES + HOLDOUT_DATES:
        matches = sorted(raw_root.glob(f"live_{date}_*"))
        if len(matches) != 1:
            raise RuntimeError(f"expected one archive for {date}, got {matches}")
        print(f"[extract] {date} {matches[0]}", flush=True)
        outputs = extract_day(matches[0], cache_root, SYMBOLS, force=force)
        print(f"  caches={len(outputs)}", flush=True)


def _configs() -> tuple[list[SustainConfig], list[TriggerConfig], list[ExitConfig]]:
    sustain = [
        SustainConfig(*args)
        for args in product(
            IMPULSE_WINDOWS_MS,
            QUANTILES,
            SUSTAIN_WINDOWS_MS,
            RETENTION,
            FLOW_MIN,
        )
    ]
    triggers = [TriggerConfig(*args) for args in product(TRIGGER_OFFSETS_BPS, TRIGGER_TTLS_MS)]
    exits = [
        ExitConfig(*args)
        for args in product(HARD_STOPS_BPS, ACTIVATIONS_BPS, TRAILS_BPS, MAX_HOLDS_MS)
    ]
    return sustain, triggers, exits


def _threshold(thresholds: dict[str, float], symbol: str, cfg: SustainConfig) -> float:
    return thresholds[f"{symbol}|{cfg.impulse_ms}|{cfg.quantile:.4f}"]


def _immediate_entries(data: dict, arms: list[dict], mode: str = "counterparty_bbo") -> list[dict]:
    latency = PLACEMENT_LATENCY_MS // BIN_MS
    ready = []
    for event in arms:
        row = dict(event)
        row["immediate_bar"] = int(event["confirm_bar"]) + latency
        ready.append(row)
    return counterparty_entries(data, ready, "immediate_bar", mode=mode)


def _rank(accs: dict[str, Accumulator]) -> list[dict]:
    rows = []
    for key, accumulator in accs.items():
        row = {"key": key, **accumulator.summary()}
        rows.append(row)
    return sorted(rows, key=lambda r: (float(r["score"]), int(r["n"])), reverse=True)


def _choose(rows: list[dict], *, primary_n: int, fallback_n: int) -> dict:
    for floor in (primary_n, fallback_n):
        eligible = [
            row
            for row in rows
            if int(row["n"]) >= floor
            and int(row["active_days"]) >= 8
            and int(row["active_symbols"]) >= 5
        ]
        if eligible:
            chosen = dict(eligible[0])
            chosen["selection_floor"] = floor
            return chosen
    raise RuntimeError(
        f"no development configuration met n>={fallback_n}, days>=8, symbols>=5"
    )


def select_development(
    cache_root: Path,
    output_root: Path,
    thresholds: dict[str, float],
) -> tuple[SustainConfig, TriggerConfig, ExitConfig, dict]:
    sustain_cfgs, trigger_cfgs, exit_cfgs = _configs()
    index = _cache_index(cache_root)

    print(f"[dev stage 1] sustain grid={len(sustain_cfgs)}", flush=True)
    stage1 = {cfg.key: Accumulator() for cfg in sustain_cfgs}
    for date in DEV_DATES:
        for symbol in SYMBOLS:
            data = load_cache(index[(date, symbol)])
            for cfg in sustain_cfgs:
                arms = sustain_arms(data, symbol, date, cfg, _threshold(thresholds, symbol, cfg))
                marks = five_second_markout(data, _immediate_entries(data, arms))
                stage1[cfg.key].update(marks)
        print(f"  stage1 complete {date}", flush=True)
    stage1_rank = _rank(stage1)
    _write_csv(output_root / "dev_stage1_ranking.csv", stage1_rank)
    chosen1 = _choose(stage1_rank, primary_n=150, fallback_n=75)
    sustain_cfg = next(cfg for cfg in sustain_cfgs if cfg.key == chosen1["key"])
    print(f"  selected sustain {sustain_cfg.key} score={chosen1['score']:.3f}", flush=True)

    print(f"[dev stage 2] trigger grid={len(trigger_cfgs)}", flush=True)
    stage2 = {cfg.key: Accumulator() for cfg in trigger_cfgs}
    stage2_counts = {cfg.key: {"arms": 0, "triggered": 0} for cfg in trigger_cfgs}
    for date in DEV_DATES:
        for symbol in SYMBOLS:
            data = load_cache(index[(date, symbol)])
            arms = sustain_arms(
                data, symbol, date, sustain_cfg, _threshold(thresholds, symbol, sustain_cfg)
            )
            for cfg in trigger_cfgs:
                triggered, n_triggered = attach_triggers(data, arms, cfg)
                entries = counterparty_entries(data, triggered, "trigger_bar")
                stage2[cfg.key].update(five_second_markout(data, entries))
                stage2_counts[cfg.key]["arms"] += len(arms)
                stage2_counts[cfg.key]["triggered"] += n_triggered
        print(f"  stage2 complete {date}", flush=True)
    stage2_rank = _rank(stage2)
    for row in stage2_rank:
        row.update(stage2_counts[row["key"]])
        row["trigger_rate"] = row["triggered"] / row["arms"] if row["arms"] else np.nan
    _write_csv(output_root / "dev_stage2_ranking.csv", stage2_rank)
    chosen2 = _choose(stage2_rank, primary_n=75, fallback_n=50)
    trigger_cfg = next(cfg for cfg in trigger_cfgs if cfg.key == chosen2["key"])
    print(f"  selected trigger {trigger_cfg.key} score={chosen2['score']:.3f}", flush=True)

    print(f"[dev stage 3] exit grid={len(exit_cfgs)}", flush=True)
    stage3 = {cfg.key: Accumulator() for cfg in exit_cfgs}
    for date in DEV_DATES:
        for symbol in SYMBOLS:
            data = load_cache(index[(date, symbol)])
            arms = sustain_arms(
                data, symbol, date, sustain_cfg, _threshold(thresholds, symbol, sustain_cfg)
            )
            triggered, _ = attach_triggers(data, arms, trigger_cfg)
            entries = counterparty_entries(data, triggered, "trigger_bar")
            for cfg in exit_cfgs:
                stage3[cfg.key].update(trailing_exits(data, entries, cfg))
        print(f"  stage3 complete {date}", flush=True)
    stage3_rank = _rank(stage3)
    _write_csv(output_root / "dev_stage3_ranking.csv", stage3_rank)
    chosen3 = _choose(stage3_rank, primary_n=75, fallback_n=50)
    exit_cfg = next(cfg for cfg in exit_cfgs if cfg.key == chosen3["key"])
    print(f"  selected exit {exit_cfg.key} score={chosen3['score']:.3f}", flush=True)

    frozen = {
        "sustain": asdict(sustain_cfg),
        "trigger": asdict(trigger_cfg),
        "exit": asdict(exit_cfg),
        "stage1_selection": chosen1,
        "stage2_selection": chosen2,
        "stage3_selection": chosen3,
        "fee_assumptions_bps": {"taker_each_leg": 4.0, "maker": 0.0},
        "placement_latency_ms": PLACEMENT_LATENCY_MS,
        "holdout_opened": False,
    }
    _json_dump(output_root / "frozen_config.json", frozen)
    return sustain_cfg, trigger_cfg, exit_cfg, frozen


def _random_entries_for_group(
    data: dict,
    reference_entries: list[dict],
    rng: np.random.Generator,
) -> list[dict]:
    if not reference_entries:
        return []
    margin = 31_000 // BIN_MS
    valid = np.flatnonzero(~data["dead"])
    valid = valid[(valid >= margin) & (valid < len(data["dead"]) - margin)]
    if not len(valid):
        return []
    chosen = rng.choice(valid, size=len(reference_entries), replace=len(valid) < len(reference_entries))
    random_events = []
    for bar, reference in zip(chosen, reference_entries):
        row = dict(reference)
        row["t_ms"] = int(data["start_ms"]) + int(bar) * BIN_MS
        row["trigger_bar"] = int(bar)
        row["signal_bar"] = int(bar)
        row["impulse_ratio"] = 0.0
        random_events.append(row)
    return counterparty_entries(data, random_events, "trigger_bar")


def _summarize_rows(rows: list[dict]) -> dict:
    clustered = cluster_events(rows)
    mean, lo, hi = day_block_ci(clustered)
    gross_mean = float(np.mean([r["gross_bps"] for r in clustered])) if clustered else np.nan
    return {
        "raw_n": len(rows),
        "clustered_n": len(clustered),
        "net_mean_bps": mean,
        "net_ci95_low_bps": lo,
        "net_ci95_high_bps": hi,
        "gross_mean_bps": gross_mean,
        "positive_day_fraction": float(
            np.mean(
                [
                    np.mean([r["net_bps"] for r in clustered if r["date"] == d]) > 0
                    for d in sorted({r["date"] for r in clustered})
                ]
            )
        )
        if clustered
        else np.nan,
    }


def evaluate_holdout(
    cache_root: Path,
    output_root: Path,
    thresholds: dict[str, float],
    sustain_cfg: SustainConfig,
    trigger_cfg: TriggerConfig,
    exit_cfg: ExitConfig,
    frozen: dict,
) -> dict:
    index = _cache_index(cache_root)
    layers: dict[str, list[dict]] = {
        "impulse_only_5s": [],
        "sustain_immediate_5s": [],
        "triggered_5s": [],
    }
    strategies: dict[str, list[dict]] = {
        "counterparty_bbo": [],
        "stop_market": [],
        "queue_bbo": [],
        "matched_random": [],
    }
    rng = np.random.default_rng(73_129)
    diagnostics = {"arms": 0, "triggered": 0, "counterparty_fills": 0, "queue_fills": 0}

    print("[holdout] opening sealed 20260621..20260630", flush=True)
    for date in HOLDOUT_DATES:
        for symbol in SYMBOLS:
            data = load_cache(index[(date, symbol)])
            threshold = _threshold(thresholds, symbol, sustain_cfg)

            impulse = impulse_events(data, symbol, date, sustain_cfg.impulse_ms, threshold)
            layers["impulse_only_5s"].extend(
                five_second_markout(data, _immediate_entries(data, impulse))
            )

            arms = sustain_arms(data, symbol, date, sustain_cfg, threshold)
            diagnostics["arms"] += len(arms)
            layers["sustain_immediate_5s"].extend(
                five_second_markout(data, _immediate_entries(data, arms))
            )

            triggered, n_triggered = attach_triggers(data, arms, trigger_cfg)
            diagnostics["triggered"] += n_triggered
            counterparty = counterparty_entries(data, triggered, "trigger_bar")
            stop_market = counterparty_entries(
                data, triggered, "trigger_bar", mode="stop_market"
            )
            queue = queue_entries(data, triggered)
            diagnostics["counterparty_fills"] += len(counterparty)
            diagnostics["queue_fills"] += len(queue)
            layers["triggered_5s"].extend(five_second_markout(data, counterparty))
            strategies["counterparty_bbo"].extend(trailing_exits(data, counterparty, exit_cfg))
            strategies["stop_market"].extend(trailing_exits(data, stop_market, exit_cfg))
            strategies["queue_bbo"].extend(trailing_exits(data, queue, exit_cfg))
            random_entries = _random_entries_for_group(data, counterparty, rng)
            strategies["matched_random"].extend(trailing_exits(data, random_entries, exit_cfg))
        print(f"  holdout complete {date}", flush=True)

    for key, rows in layers.items():
        _write_csv(output_root / f"holdout_{key}_events.csv", cluster_events(rows))
    for key, rows in strategies.items():
        _write_csv(output_root / f"holdout_{key}_events.csv", cluster_events(rows))

    layer_summary = {key: _summarize_rows(rows) for key, rows in layers.items()}
    strategy_summary = {key: _summarize_rows(rows) for key, rows in strategies.items()}
    primary = cluster_events(strategies["counterparty_bbo"])
    first = [row for row in primary if row["date"] <= "20260625"]
    second = [row for row in primary if row["date"] >= "20260626"]
    first_mean = float(np.mean([r["net_bps"] for r in first])) if first else np.nan
    second_mean = float(np.mean([r["net_bps"] for r in second])) if second else np.nan
    p = strategy_summary["counterparty_bbo"]
    random_mean = strategy_summary["matched_random"]["net_mean_bps"]
    gate_checks = {
        "n_at_least_100": p["clustered_n"] >= 100,
        "net_mean_positive": p["net_mean_bps"] > 0,
        "ci95_lower_positive": p["net_ci95_low_bps"] > 0,
        "both_halves_positive": first_mean > 0 and second_mean > 0,
        "beats_matched_random": p["net_mean_bps"] > random_mean,
    }
    report = {
        "frozen_config": frozen,
        "diagnostics": diagnostics,
        "layer_ablation": layer_summary,
        "entry_mode_sensitivity": strategy_summary,
        "primary_half_means_bps": {"20260621_25": first_mean, "20260626_30": second_mean},
        "gate_checks": gate_checks,
        "pass": all(gate_checks.values()),
    }
    report["frozen_config"]["holdout_opened"] = True
    _json_dump(output_root / "report.json", report)
    _write_report_md(output_root / "REPORT.md", report)
    return report


def _fmt(value) -> str:
    if isinstance(value, float):
        return "nan" if not np.isfinite(value) else f"{value:.3f}"
    return str(value)


def _write_report_md(path: Path, report: dict) -> None:
    frozen = report["frozen_config"]
    lines = [
        "# Aster sustained-momentum holdout report",
        "",
        f"**Verdict: {'PASS' if report['pass'] else 'FAIL'}**",
        "",
        "The verdict applies to the preregistered primary counterparty-BBO model only.",
        "",
        "## Frozen configuration",
        "",
        f"- Sustain: `{json.dumps(frozen['sustain'], sort_keys=True)}`",
        f"- Trigger: `{json.dumps(frozen['trigger'], sort_keys=True)}`",
        f"- Exit: `{json.dumps(frozen['exit'], sort_keys=True)}`",
        "",
        "## Five-second layer ablation",
        "",
        "| layer | clustered n | net mean bps | 95% day-block CI |",
        "|---|---:|---:|---:|",
    ]
    for name, result in report["layer_ablation"].items():
        lines.append(
            f"| {name} | {result['clustered_n']} | {_fmt(result['net_mean_bps'])} | "
            f"[{_fmt(result['net_ci95_low_bps'])}, {_fmt(result['net_ci95_high_bps'])}] |"
        )
    lines.extend(
        [
            "",
            "## Frozen exit by entry mechanism",
            "",
            "| mechanism | clustered n | gross mean bps | net mean bps | 95% day-block CI |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for name, result in report["entry_mode_sensitivity"].items():
        lines.append(
            f"| {name} | {result['clustered_n']} | {_fmt(result['gross_mean_bps'])} | "
            f"{_fmt(result['net_mean_bps'])} | [{_fmt(result['net_ci95_low_bps'])}, "
            f"{_fmt(result['net_ci95_high_bps'])}] |"
        )
    lines.extend(["", "## Gate", ""])
    for name, passed in report["gate_checks"].items():
        lines.append(f"- {'PASS' if passed else 'FAIL'}: `{name}`")
    lines.extend(
        [
            "",
            "## Diagnostics",
            "",
            *[f"- {key}: {value}" for key, value in report["diagnostics"].items()],
            "",
            "See `EXPERIMENT.md` for assumptions and known limitations.",
        ]
    )
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("extract", "run", "all"))
    parser.add_argument("--raw-root", type=Path, default=Path("/root/hl_data/aster"))
    parser.add_argument(
        "--cache-root", type=Path, default=Path("/root/hl_data/momentum_trigger/cache")
    )
    parser.add_argument(
        "--output-root", type=Path, default=Path("/root/hl_data/momentum_trigger/results")
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.command in ("extract", "all"):
        extract_all(args.raw_root, args.cache_root, force=args.force)
    if args.command in ("run", "all"):
        args.output_root.mkdir(parents=True, exist_ok=True)
        index = _cache_index(args.cache_root)
        expected = {(d, s) for d in DEV_DATES + HOLDOUT_DATES for s in SYMBOLS}
        missing = expected - set(index)
        if missing:
            raise RuntimeError(f"missing {len(missing)} caches; run extract first")
        print("[calibration] development-only impulse thresholds", flush=True)
        thresholds = calibrate_thresholds(
            index,
            DEV_DATES,
            SYMBOLS,
            IMPULSE_WINDOWS_MS,
            QUANTILES,
            load_cache,
        )
        _json_dump(args.output_root / "thresholds_dev_only.json", thresholds)
        sustain_cfg, trigger_cfg, exit_cfg, frozen = select_development(
            args.cache_root, args.output_root, thresholds
        )
        report = evaluate_holdout(
            args.cache_root,
            args.output_root,
            thresholds,
            sustain_cfg,
            trigger_cfg,
            exit_cfg,
            frozen,
        )
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=True), flush=True)


if __name__ == "__main__":
    main()

