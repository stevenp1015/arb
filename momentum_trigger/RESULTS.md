# Sustained-momentum experiment result

## Verdict

**FAIL. Do not promote this continuation strategy to live execution.**

The frozen Aster-native continuation model was negative on the June 21-30
holdout before fees and after fees. It also failed to beat its matched-random
control. The result is not explained by the deliberately pessimistic 100 ms
fill convention: an exact-timestamp post-trigger BBO sensitivity improved entry
by about 0.51 bps and remained significantly negative even at zero fees.

## Frozen model

- Impulse: 1,000 ms return above the development-only 99.5th percentile.
- Sustain: hold 100% of the impulse for another 500 ms.
- Flow: at least 55% same-direction aggressive notional.
- Trigger: contract-price breakout 5 bps beyond the confirmed extreme.
- Trigger TTL: 1,000 ms.
- Exit: 30 bps hard stop; activate after +20 bps; trail by 10 bps; maximum
  hold 30 seconds.

All choices were selected on June 11-20 and frozen before June 21-30 was opened.

## Primary holdout result

- 17,013 qualifying sustained moves armed.
- 1,130 crossed the native continuation trigger.
- 709 passed the $1,000 top-of-book capacity gate.
- 599 remained after one-second cross-symbol clustering.
- Counterparty-BBO: -9.68 bps net per clustered event, 95% day-block CI
  [-10.50, -8.83].
- Both five-day halves were negative and every holdout day was negative.
- Matched random: -9.54 bps net. The strategy was 0.15 bps worse.

The day-weighted zero-fee gross estimate for the primary model is approximately
-1.68 bps, so lowering fees to zero does not produce a positive expectation.

## What each layer did

Five-second executable markouts, net of 4 bps per taker leg:

- Impulse only: -9.77 bps.
- Impulse plus sustain: -9.52 bps.
- Impulse plus sustain plus trigger: -10.12 bps.

The sustain filter improved the naïve result by roughly 0.25 bps. Waiting for
the extra 5 bps breakout then made it about 0.59 bps worse. In this sample the
second-leg trigger selected exhaustion, not continuation.

## BBO mechanism sensitivities

- Counterparty BBO: -9.68 bps net; marketable/taker assumption.
- Stop market: -10.64 bps net; same trigger plus 1 bp entry slippage.
- Queue BBO: -5.40 bps net on 220 clustered fills; maker entry and taker exit.
  Its day-weighted zero-fee gross estimate is still about -1.40 bps.
- Exact post-trigger BBO: median observed BBO update lag 5 ms and about 0.51 bps
  better entry than the adverse 100 ms convention. Frozen-exit result: -9.31
  bps net; zero-fee gross -1.31 bps with 95% day-block CI [-1.97, -0.62].

The web UI's instant BBO fill behavior is therefore real enough to improve the
fill estimate, but not large enough to rescue this signal.

## Interpretation and next move

This experiment rejects the specific hypothesis "violent Aster move -> holds ->
breaks the extreme again -> profitable continuation." The data instead says the
extra breakout is, on average, an exhaustion point over these horizons.

The clean next hypothesis is a separately preregistered **failed-breakout fade**:
arm after the same sustained move, wait for a 5 bps break, then require price to
fall back inside the prior extreme before entering against the move with a
queue-side BBO limit. That must use untouched later data; the June 21-30 holdout
is now research data and cannot be reused as a clean test.

Before any live-risk experiment, measure the account's actual maker/taker fees,
order acknowledgement latency, partial-fill behavior, and BBO peg semantics in
shadow or paper execution. No live orders were sent during this work.
