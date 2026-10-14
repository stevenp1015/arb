# Aster sustained-momentum native-trigger experiment

Status: preregistered before holdout evaluation.

## Question

After an unusually fast Aster-native price impulse, does requiring the move to
hold and then using a venue-native contract-price trigger to enter a second leg
produce positive executable return after fees?

This is a backtest only. It does not place, cancel, or modify live orders.

## Data split and universe

- Source: Tokyo archive under `/root/hl_data/aster/live_YYYYMMDD_*`.
- Development/calibration: 2026-06-11 through 2026-06-20.
- Sealed holdout: 2026-06-21 through 2026-06-30.
- Stable liquid universe present on every date: `BTCUSDT`, `ETHUSDT`,
  `SOLUSDT`, `HYPEUSDT`, `ASTERUSDT`, `BNBUSDT`, `XRPUSDT`, `DOGEUSDT`.
- Clock: recorder receive time `r`, binned to 100 ms. Exchange event time `t`
  remains diagnostic; decisions never use a message before its receive time.
- An order becomes armed 100 ms after the confirming observation. This is a
  conservative placement-latency allowance, not a measured round trip.

## Signal selection (development data only)

The detector is chosen in two deliberately separated stages.

### Stage 1: impulse and sustain

- Impulse windows: 500, 1,000, 2,000 ms.
- Per-symbol/per-window impulse cutoffs: the 99.5th and 99.9th percentiles of
  absolute 100 ms-grid returns measured only on development data.
- Sustain windows: 500, 1,000, 2,000 ms.
- Retention requirements: 50%, 75%, 100% of the observed impulse.
- Aggressive-flow requirement: none, or at least 55% of notional in the signal
  direction during the sustain interval.
- Spread at confirmation must be at most 5 bps.
- One armed event per symbol per 30 seconds.

Stage 1 is ranked by the net executable five-second markout from an immediate
counterparty-BBO entry after confirmation. The score is the equal-weight mean
of symbol-day means minus 0.5 standard errors across active symbol-days.
Candidates need at least 150 fills, eight active days, and five active symbols.
If none qualify, the same ranking is used with a 75-fill floor; if still none
qualify the experiment stops rather than silently weakening the rules again.

### Stage 2: second-leg native trigger

For the chosen impulse/sustain detector, arm a contract-price continuation
trigger beyond the most favorable trade extreme observed through confirmation.

- Trigger offsets: 1, 2, 5 bps.
- Trigger time-to-live: 1,000 or 3,000 ms.
- The chosen offset/TTL maximizes the same development-only stability score.
- At least 75 triggered counterparty-BBO fills are required.

The entire selected detector and trigger are frozen before holdout evaluation.

## Entry semantics

Three mechanisms are reported separately; they are never blended:

1. `counterparty_bbo`: conditional limit resolves to the opposite best quote at
   trigger time. It is treated as marketable/taker. Entry uses the unfavorable
   100 ms quote extreme and requires at least $1,000 displayed at the sampled
   top quote.
2. `stop_market`: same trigger, same conservative BBO fill, plus 1 bp entry
   slippage. It is treated as taker.
3. `queue_bbo`: conditional limit resolves to the same-side best quote and is
   treated as maker. It fills only after strict trade-through or after observed
   same-price aggressive notional consumes displayed queue ahead plus the
   $1,000 test order. It may rest for 2,000 ms. Because cancellations and order
   IDs are absent, this is explicitly an approximate queue model.

Fee assumptions are 4 bps per taker leg and 0 bps per maker leg. A marketable
BBO limit is not granted maker fees merely because its order type says LIMIT.
All values are parameters and the report includes gross as well as net results.

## Exit selection

Only development trades from the frozen triggered/counterparty entry are used
to choose the exit:

- Hard stop: 10, 20, 30 bps.
- Trailing activation: 10, 20, 40 bps favorable excursion.
- Trail distance: 10, 20, 30 bps.
- Maximum holding time: 5, 15, 30 seconds.

The 10 bp minimum trail matches Aster's documented 0.1% minimum callback rate.
Executable bid/ask paths are used. A same-bar high/low ambiguity is resolved
against the strategy and the unfavorable quote extreme is used for a stop fill.
The exit is taker in every entry model.

## Controls and inference

- Layer ablation at five seconds: impulse-only, impulse+sustain, and
  impulse+sustain+trigger.
- Matched random control: same date, symbol, side mix, entry mechanism, exit,
  and trade count, sampled from valid bars with a fixed seed.
- Event rows are also clustered across symbols within one second; the strongest
  normalized impulse represents a cluster so one market-wide jump is not eight
  independent observations.
- Uncertainty is a 95% day-block bootstrap interval (10,000 resamples). Raw
  snapshot count is not used as the independent sample size.

## Holdout pass gate

The frozen triggered/counterparty strategy passes only if all are true:

- at least 100 clustered holdout fills;
- net mean PnL is positive;
- the 95% day-block bootstrap lower bound is above zero;
- both five-day holdout halves have positive net mean PnL;
- net mean PnL exceeds the matched-random control.

Queue-BBO and stop-market are sensitivity analyses. They do not rescue a failed
primary counterparty-BBO gate. No holdout cell is optimized or substituted.

## Known limitations

- 100 ms aggregation cannot reproduce exact within-bin event ordering.
- Top-of-book data cannot model multi-level market impact.
- Queue fills cannot be exact without order-level queue/cancellation data.
- The 100 ms placement latency and 4 bp taker fee are assumptions, not account
  measurements. A later shadow/paper run should measure both before live risk.
- Results from these twenty days establish evidence for this sample, not a
  timeless property of the market.
