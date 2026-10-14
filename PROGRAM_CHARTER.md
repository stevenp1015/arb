# PROGRAM CHARTER — the $20k algorithm program

**Written:** 2026-10-04 by Fable, at Steven's direction. **Owner:** Fable (lead). Steven = hands + funds.
**Goal:** one trading algorithm that is net profitable on real fills, after fees, and stays positive after
tuning stops. Nothing less counts. This file is the constitution for the program; THE_PLAN.md's eight
standing laws apply in full and are not repeated here. Read them.

## Why three lanes and not one

Three directional edges collapsed in 2026 under honest accounting (liq-fade, pack_rider, fp_engine).
Effort was not the bottleneck. The approach was. The only thing making money today is Steven trading
manually on the Gate/Aster desk. So lanes are ranked by probability of money, and capital follows rank.

## Lane 1 — Codify Steven (highest priority, starts today, needs no server)
1. Pull all closed positions from Gate Web3 (`/apiw/v2/futures/positions/history`) and Aster.
2. Measure: entry vs SMA rungs, hold time, win rate, PnL by venue/coin/hour/session, what winners share.
3. Build the **bracket placer**: on any new position, attach stop + targets within 10s. Protect-only.
4. Build the **shadow judge**: hourly, says what a disciplined Steven would do, keeps score vs. actual.
   Promotion rule: beats Steven for one week -> may CLOSE or PROTECT. Never open, never enlarge. Ever.

## Lane 2 — Collect rent (first real dollars go here)
Funding-rate carry across Gate / Aster / Lighter / Hyperliquid / Phemex. Long where paid, short where
charged. Revive `carry/`. Verify rates live before anything. Risk is execution + basis, not direction.

## Lane 3 — Directional, paper only until it earns otherwise
- SMA ladder rule through the full first-passage test WITH costs. Never been done.
- Liq-fade timeout bucket with Auto-Price-Down exit, accumulated forward (needs `liq_paper.py` restarted
  on a server). Cannot be tested retrospectively; paths were never archived.

## Capital ladder (per lane)
| Stage | Size | Unlock |
|---|---|---|
| 0 | paper | code runs, heartbeat, ledger writes |
| 1 | $500 | 20+ live fills measured (real slippage, real fill rate) |
| 2 | $2k-$5k | positive net after fees over 50+ trades; raw ledger offered to Steven |
| 3 | <= $10k | two consecutive positive weeks at stage 2 |
Stage 3 caps at half the pot. No lane may exceed it. Moving between stages is Fable's call, logged here.

## Team rules
- Fable leads. Opus workers get scoped briefs, non-overlapping files, and return evidence, not claims.
- Every quantitative conclusion is bounced off Codex before it locks (Steven's standing rule).
- Fable personally owns: what counts as evidence, anything touching sizing, anything live-money.
- Law 7 applies to agents: a worker that reports nothing must prove nothing happened.
- Daily plain-English ledger summary to Steven with raw tickets attached. Immediate message on any
  real-money action or any breakage.

## Infrastructure decisions
- tokyo2 (207.148.114.69): screener only. 28 GB disk. Not a research box.
- Old Tokyo bare metal (202.182.110.74): reinstate for $200 IF Vultr still has it (518 GB June tape +
  fp_engine matrices are irreplaceable). If destroyed, do not chase; rent a Tokyo VPS + 500 GB block
  volume (~$40/mo) and start recording again.
- Programmatic trading: Aster (API wallet, works), Lighter (SignerClient, JP egress), Hyperliquid (API).
  Gate Web3 is browser-token-bound (1h bearer via Tampermonkey keeper) and is NOT suitable for an
  unattended bot until that is solved. Lane 1 reads from Gate; bots trade elsewhere first.

## Open items for Steven's hands
1. Vultr: does the old instance exist? Reinstate or confirm gone.
2. Confirm/override the capital ladder. Silence = proceed.
3. Check-in cadence. Default: daily summary + immediate on money/breakage.

## Decision log
- 2026-10-04: charter written. Lane 1 begins with the closed-position pull.
- 2026-10-04: old Tokyo bare metal quoted at $380 to restore. PARKED by Steven. June tape treated as lost
  unless he revisits. tokyo2 saves only 1-minute OHLC bars (no volume, no raw trades) since 2026-09-10.
- 2026-10-04: desk proxy slowness root-caused to the Swiss exit's DNS; fixed with local resolution.
