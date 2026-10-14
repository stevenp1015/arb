# MoonTrader Algorithms — Source-of-Truth Parameter Reference

**Written 2026-08-11.** This is the single merged reference for every configurable parameter in the
MoonTrader algorithms. It exists because the official documentation is scattered and we hold **two
partial copies that disagree with each other**, so every future session was going to re-derive this
from scratch.

**Purpose:** we are building our own bot running MoonTrader-style algorithms without MoonTrader.
This page is the specification of what to build and, just as importantly, the list of every knob
whose value has to be **mined from our own tape rather than guessed** (prime directive:
`feedback_first_passage_eval.md`).

---

## 0. Provenance, and where the two copies disagree

| Source | What it is | Trust |
| --- | --- | --- |
| `moontrader/mtdocs/algorithms/*.md` | The **older, longer** scrape. ~2x the length of the other copy. Has the risk machinery (Trailing Stop, Second Stop Loss, SL Spread/Delay) that the newer copy drops. | **Fullest on risk + filters.** |
| `moontrader/updatedMTdocs/# X Algorithm.md` | The **newer, shorter** scrape, matching a later UI. Cleaner parameter names, better examples, but incomplete — it was still being written. | **Fullest on newer naming.** |
| `moontrader/updatedMTdocs/x-algo.md` | Byte-identical to the `mtdocs/algorithms/` copies. Duplicate, ignore. | Duplicate. |
| `moontrader/mtdocs/trading/*.md` | Shared machinery — Take-Profit, Stop Loss, Triggers, Performance Filter — written once and applying to all algorithms. | **Authoritative for shared machinery.** |
| `moontrader/old_algo_files/` | Older still. Superseded. | Ignore. |

**Known naming conflicts. When in doubt, implement the behaviour, not the label.**

| Concept | Older docs call it | Newer docs call it | Behaviour difference |
| --- | --- | --- | --- |
| Averages trigger | **Trigger Distance** — one value | **Trigger Range** — min and max | Newer is a band, older is a single threshold. Band is strictly more expressive; build the band. |
| Averages size cap | **Max. position size** | **Limit Position Size** | Same thing. Newer clarifies it applies **per pair**, so N pairs means N x the cap in total exposure. |
| Averages periods | **Long period / Short period** (seconds) | **Period (Long-Short) / Period (Short)** | Same. Newer allows 1s to 24h explicitly. |
| Restart control | **Auto restart** + **Restart delay** | **Auto Restart** with modes *Run once* / *Restart after delay* | Newer exposes "run once" as an explicit mode. |

Every doc page also carries an "Agent Instructions" footer offering a live query endpoint
(`GET https://guide.moontrader.com/...md?ask=<question>`). Untested by us — if a parameter here is
ambiguous, that is the place to resolve it rather than guessing.

---

## 1. Universal machinery (applies to every algorithm)

### 1.1 General settings

| Parameter | What it does | Notes for us |
| --- | --- | --- |
| **Name** | Algorithm label. `#` inserts auto-variables, e.g. `SG_##TYPE##_##SIDE##`. | Cosmetic. |
| **Auto start with core** | Starts on core boot. | Warning in docs: many auto-start algos after a resource-shortage restart all fire at once and cause another restart. |
| **Stop if trade latency out of range** | Halts and cancels while **exchange→core** data lag is outside min/max seconds. | Docs recommend 1–5s. **We want this much tighter.** Our whole cross-venue result says edge dies by 100ms. |
| **Stop if order latency out of range** | Halts while **round-trip order placement** time is outside range. | Same. Our measured fire-to-order-sent is ~15ms; anything above ~50ms means we are trading blind. |
| **Auto restart** | *Run once* (one entry per symbol until re-armed) or *Restart after delay*. | "Run once" is the honest default for a probation test — it bounds how much a broken config can cost. |
| **Restart delay** | Seconds before the logic re-arms after a fill. | **Mineable**: the right value is the decay time of whatever signal fired. |
| **Restart delay on critical errors** | Same, but triggered by an exchange error rather than a fill. | Protects against hammering a venue that is rejecting. Default 60s. |
| **Filter check frequency** | How often the engine re-scans the market for pairs passing the entry filters. | Trades off freshness against API load. Interacts with List Sorting Frequency, see 1.4. |
| **Order time window** | Seconds the exchange has to process the placement before it self-cancels. | Too small = frequent rejections. This is the "don't fill me at a stale price" guard. |
| **Use listing only** | Only trade newly listed pairs. | Not our game. |
| **Open chart on order fill / deal completion** | UI only. | Irrelevant to a headless bot. |
| **Emulation** | Paper mode, no orders sent. | **CRITICAL: the docs state commission is NOT counted in emulation reports.** Every emulation number must have fees subtracted by hand. This is exactly the trap that made the ASTER scalp look profitable. |

### 1.2 Market filter — which pairs at all

| Parameter | What it does | Notes |
| --- | --- | --- |
| **Market Type** | Spot / Futures / Delivery. | |
| **Quote Assets** | Trade *all* pairs quoted in these (e.g. `USDT`). | Docs warn: without an Active Markets cap this can mean hundreds of pairs → exchange ban, core overload. |
| **Whitelist** | Explicit pair list. Leave Quote Assets empty when using it. | |
| **Blacklist** | Exclusions. **Blacklist beats whitelist.** | |
| **Use Quantitative Rules** | Auto-pauses near exchange order-count limits to avoid a ban. | Docs say always enable when available. |

### 1.3 Delta filters — is this pair moving

Two distinct measures, and confusing them will silently break a strategy:

- **Absolute delta (price *range*)** = `(max − min) / price_at_start × 100`. Direction-blind. Measures
  volatility. **Positive values only.** Triggers even if open equals close.
- **Relative delta (price *change*)** = `(price_at_end − price_at_start) / price_at_start × 100`.
  Directional. **Positive and negative values.**

A pair that ran 100 → 103 → 100 has relative delta 0% and absolute delta 3%.

| Parameter | What it does | Notes |
| --- | --- | --- |
| **Delta Type** | Absolute or Relative. | |
| **Timeframe** | Measurement window, seconds through days. | **Mineable.** Multiple filters with different windows can be stacked (AND logic). |
| **Range** | Min to max, always smaller value in the left field. | |
| **Cancel if out of range** | Pulls resting orders when the pair leaves the band. | The only filter that acts on *live* orders rather than at placement. |
| **Other Symbols Delta Filter** | Same, but gated on a *different* pair — classically BTC. | This is the market-regime gate. "Don't trade alts while BTC is dumping." |

### 1.4 Active markets filter — how many pairs at once

| Parameter | What it does | Notes |
| --- | --- | --- |
| **Sorting criteria** | Relative delta, absolute delta, or 24h quote-asset volume, each ascending or descending. Multiple criteria combine by weighted list-sum. | |
| **Max active markets** | Cap on simultaneous pairs. | |
| **Ignore first** | Skip the top N of the sorted list. | Lets several algorithms carve non-overlapping slices (0–10, 10–20, 20–30). Also a clean way to *avoid* the most extreme movers. |
| **List sorting frequency** | Seconds between re-sorts of the pair list. | **Distinct from Filter Check Frequency.** Sorting decides *which pairs are in the list*; filter check decides *whether an order may rest* on a pair already in it. An order can be cancelled by list eviction on the sorting clock, or by a filter on the filter clock. |

### 1.5 Other entry filters

| Filter | What it does | Notes for us |
| --- | --- | --- |
| **Price step filter** | Range for the tick size as a percent of price. | Docs: "used to cut off *square* coins, on which the price step can reach several percent." **This is the blowup filter.** Our liquidation harvest's worst losses were CASHCAT −6.4%, GRAM −2.3%, ASTEROID −2.4% — all thin junk. |
| **Mark price filter** | Band for mark-price deviation from market price. | Accepts negative values. |
| **Funding rate filter** | Only trade pairs whose funding sits in a band. | |
| **Funding filter** | Start/pause N minutes **before** and **after** a funding payment, gated on a funding range. | Directly relevant to the carry track — this is a built-in "be flat across settlement" or "be in it across settlement" switch. |
| **Symbol QAV filter** | Min/max 24h volume for the pair. | Liquidity floor. |
| **Performance filter** | See 1.6. | |

### 1.6 Performance filter — stop trading what is losing

| Parameter | What it does |
| --- | --- |
| **Source** | *Global by symbol* / *Algo symbol* / *Algo total* / *Custom key*. |
| **Source (custom key only)** | Current algo, manual trades, liquidations, all, or a named other algorithm. |
| **Market type, Quote assets/Symbols** | Scope narrowing, custom key only. |
| **Timeframe** | Window the performance is measured over. |
| **Validate by** | *Profit* (USDT), *Price Δ %*, or *Profit Factor* (profit ÷ loss). |
| **Value** | Threshold, or a range. |
| **Action** | *Stop till next run*, or *Pause*. |
| **Sleep time** | Seconds paused before re-checking. |

**Note for us:** this is a circuit breaker expressed as a filter, and it is the one piece of the
MoonTrader design our own bots have never had. Every failed strategy in this repo ran to its full
sample because nothing was watching its P&L.

### 1.7 Take Profit

| Parameter | What it does | Notes |
| --- | --- | --- |
| **Enable** | On by default. | |
| **Percentage** | Distance from fill price. **Negative closes at market immediately.** | Docs stress it must exceed round-trip commission. On Aster maker-in/maker-out that floor is 0%; a stop exit costs 0.04%. |
| **Order type** | *Limit* = real order on the exchange (green `r`). *Market* = virtual in core (purple `v`), sent only when touched. | **Hard rule: of the TP/SL pair, one must be virtual and the other real.** This single constraint decides your maker/taker mix and therefore your fees. Limit TP + virtual SL is the maker-friendly combination. |
| **Iceberg** | Hides TP size. Spot only. | |

**Take Profit modes differ by algorithm:**

- **Shots, Averages, Signal, manual** — Classic only: fixed % from entry.
- **Depth Shots** — three modes:
  - **Classic** — fixed % of entry price.
  - **Historic** — % of the distance price travelled **in the last 2 seconds including the spike**.
    Travelled distance is 100%; you specify the pullback fraction. Default 50%.
  - **Depth** — % of the **spike alone**, excluding the preceding 2 seconds. Default 50%.
- **Vector** — **% of the frame spread, not of price.** For a buy it is measured from the lower spread
  boundary of the current frame. Because Vector can fire on a 1% range or a 5% range with the same
  setting, a price-based TP would make no sense.
- **Vector, Adaptive Take Profit** — adds the average frame-to-frame boundary change to the TP price,
  so a steadily rising market gets a further target. Multidirectional frames average near zero and
  the adaptation does nothing.

**Auto Price Down** — walks the target *down* over time toward a floor.

| Parameter | What it does | Notes |
| --- | --- | --- |
| **Timer** | Seconds between each lowering step. Fractional allowed. Counts from order placement. | |
| **Step** | Size of each reduction, **always in % of entry price**, even in Depth Shots and Vector where the initial level was computed dynamically. | Easy thing to get wrong when reimplementing. |
| **Limit** | Floor. **May be negative**, in which case it will eventually close at a loss. | |

> **Why this matters to us specifically.** The liquidation-fade harvest found **41.4% of signals timed
> out at the hour, and 120 of those were positive but never reached the fixed target, averaging
> +0.257%.** Auto Price Down is the mechanism that banks exactly that money. It is the single
> highest-value untested parameter on this page.

### 1.8 Stop Loss

| Parameter | What it does | Notes |
| --- | --- | --- |
| **Enable** | On by default. | |
| **Percentage** | Distance from entry, always positive; below entry for long, above for short. | |
| **Spread** | Extra offset **past** the trigger at which the limit order is placed, so it actually fills. **Re-applied every 60ms while price keeps running.** Limit and stop-limit only; market orders ignore it. | **This is the answer to our measured failure.** All 141 stop-outs in the liquidation harvest realized worse than their stop (−1.362% average against a −1.180% setting). Note carefully: Spread does not *prevent* slippage, it *chases* — you are guaranteed a fill at a progressively worse price. It converts an unbounded stuck-order risk into a bounded chase. |
| **Delay** | Seconds after entry before the stop is placed at all. Fractional allowed. | Docs explicitly recommend **1–2 seconds for Shots/Depth Shots**, so you are not stopped out inside the very spike that filled you. **Mineable, and directly aimed at our loss bucket.** |
| **Order type** | *Limit* (virtual, becomes real limit on touch, uses Spread) / *Market* (virtual, closes at market) / *Stop Limit* (real order resting on the exchange, uses Spread). | Availability is exchange-dependent. |
| **Liquidation offset** | If the exchange's liquidation price is nearer than your stop, the stop is moved to `liquidation_price − offset`. | Only matters with leverage. Without it a leveraged position liquidates before the stop ever triggers. |

**Trailing Stop**

| Parameter | What it does |
| --- | --- |
| **Spread** | How far price must move in your favour before the stop moves by the same amount. It never moves back. |

Worked example from the docs: long at 100, stop 2%, trailing spread 1%. At 101 the stop goes 98→99;
at 102, 99→100 (breakeven); at 103, 100→101 (profit locked).

**Second Stop Loss** — replaces the first stop once you are far enough in profit.

| Parameter | What it does |
| --- | --- |
| **Trigger distance** | % move in your favour that fires the replacement. |
| **Percentage** | Distance back from the trigger for the new stop. **Formula: `second_stop = trigger − percentage`.** Trigger 2% / pct 2% = breakeven. Trigger 3% / pct 2% = +1% locked in. |
| **Second stop trailing + spread** | Same trailing behaviour, applied to the replacement stop only. |

### 1.9 Order parameters (shared)

| Parameter | What it does | Notes |
| --- | --- | --- |
| **Side** | Buy/long rests in the bid book; Sell/short rests in the ask book. | |
| **Client order type / Stop price** | Order type sent, and the trigger for stop-limit. | |
| **Order size** | In USDT, **inclusive of leverage**. A $45 order at 125x uses $0.36 of your own funds. | Set slightly above the exchange minimum or orders get rejected. |
| **Iceberg** | Hides true size, fills in pieces. **Spot only.** | Useless on the perp venues we actually trade. |
| **Auto join** | Merges positions on the same pair, recomputing weighted average entry and re-deriving TP/SL from it. | |
| **Join key** | Restricts merging to algorithms sharing the key. | **Without a key it merges everything on the pair, including manual positions.** This is the bug class that produced fake wins and blended entry prices in our July Lighter work. Always set a key. |

### 1.10 Triggers and Actions — the orchestration layer

Conditions (any one fires — **OR**): Market Watcher signal, Order filled, Take-Profit filled,
Stop-Loss filled, Trade closed, Alert.

Actions (all run together — **AND**): Start algorithm, Start algorithm clone, Stop algorithm,
Panic sell, Pause algorithm.

| Parameter | What it does | Notes |
| --- | --- | --- |
| **Trigger key** | Free-text string linking a condition to an algorithm. | **A trigger fires only if the key AND the condition type both match on each side.** Mismatched keys fail silently. |
| **Clone: lifetime** | Seconds the temporary copy runs. | |
| **Clone: limit** | Max simultaneous clones per pair. | Docs: always set this. |
| **Pause: duration, type** | Seconds, and scope — trigger pair / whitelist / whole algorithm. | |
| **Panic sell scope** | Trigger pair or whitelist. | **Closes positions only. Resting orders survive.** |

**Note for us:** this is how a cooldown-after-a-loss-streak or a stop-everything-if-BTC-craters rule
gets expressed. Our own bots have never had either.

---

## 2. Shot

**What it is.** A limit order parked a fixed % from price, trailing it inside a dead zone. It fills
only when a spike reaches out and takes it, then targets the pullback.

| Parameter | What it does | Default / guidance | Mineable? |
| --- | --- | --- | --- |
| **Distance** | How far from current price to keep the order, in %. | Docs: 0.5–1% liquid, 1.5–3% illiquid. **Negative distance opens instantly at market.** | **Yes — the core knob.** Fill rate against pullback size. |
| **Buffer** | Width of the dead zone in % of price, **split in half** either side. Price inside it does not move the order. | 0.2–0.4% precise/heavy, 0.8%+ loose/light. | **Yes.** |
| **Follow price delay** | Seconds the order will not move *toward* price after the far boundary is crossed. If price returns inside during the delay, no move. If price crosses the *other* way, it moves immediately regardless. | Docs: use small values, 0.1–1s. | **Yes.** |
| **Replace delay** | Seconds the order will not move *away* from price after the near boundary is crossed. Same immediate-reversal exemption. | Small values. | **Yes.** |
| **Data source** | *Ticker price* (0.5s updates, light) or *Trades data* (every trade, heavy). | Trades for tight distances, ticker for wide/many. | Our tape decides which resolution the distance actually needs. |

Plus all of §1.7 Take Profit (Classic only), §1.8 Stop Loss, §1.9 order parameters.

---

## 3. Depth Shot

**What it is.** Shot, but the order is placed where the **order book has accumulated N dollars of
resting volume**, not at a fixed distance. The premise: to reach you, someone must eat through N
dollars of liquidity, so N is a direct measure of how violent the move was. Better on sparse books
where spike sizes vary a lot.

| Parameter | What it does | Notes | Mineable? |
| --- | --- | --- | --- |
| **Quote asset volume** | Target cumulative book volume, in the pair's quote currency. Order goes where the book reaches this figure. | Plain integers, no abbreviations — one million is `1000000`. | **Yes.** Our fomc tape has 20 book levels at 100ms for BTC/ETH/SOL, which is exactly the input this needs. |
| **Min / Max distance** | Bounds, in % from price, within which the volume-derived level may sit. | If the level falls outside, behaviour depends on *Stop if out of range*. | **Yes.** |
| **Volume buffer** | Corridor in quote units around the target volume where fluctuation does not move the order. **Split in half** either side. | Volume 50,000 with buffer 4,000 → moves only outside 48,000–52,000. | **Yes.** |
| **Min / Max buffer** | Caps the buffer width in % of price, applied to each boundary independently. | Min stops the corridor collapsing against a book wall; Max stops it ballooning on a sparse book. | **Yes.** |
| **Stop if out of range** | Off: order pins to the Min/Max boundary. On: order is cancelled and rechecked after the critical-event restart delay (default 60s). | | |
| **Use high-frequency order book** | Off: 1s spot / 0.5s futures. On: 0.1s all markets. | Docs: enable for 1–5 pairs, disable at 10+. Risks desync and API exhaustion. | |
| **Follow price delay / Replace delay** | As Shot, but triggered by volume-boundary crossings rather than price. | | **Yes.** |

Take Profit here has the three modes (Classic / Historic / Depth) described in §1.7 — this is the
only algorithm with adaptive exits built in, and **the reason Depth Shot is the most interesting one
for us.**

---

## 4. Vector

**What it is.** A volatility-burst detector. Chop time into tiny frames, require *every* frame in the
window to show a big enough price range with enough trades and volume, then place orders inside the
range.

| Parameter | What it does | Default | Mineable? |
| --- | --- | --- | --- |
| **Frame size** | Length of one micro-interval, seconds. | 0.2s (min 0.001) | **Yes.** |
| **Time frame** | Total analysis window. Divided by frame size = number of frames. | 1s (min 0.001) | **Yes.** Short windows find repeated spikes; long windows find sustained volatility. |
| **Min spread size** | Minimum high-minus-low *within each frame*, as %. | 0.5% | **Yes.** |
| **Upper border range** | Min/max allowed change of the frame **high** between adjacent frames, as % of the last frame's range. All transitions must fit. | 0 to 0.5% | **Yes.** |
| **Lower border range** | Same for the frame **low**. | 0 to 0.5% | **Yes.** Set both positive to demand a clean uptrend. |
| **Min trades per frame** | Trade-count floor per frame. | 2 | **Yes.** |
| **Min quote asset volume** | Volume floor per frame. | 10,000 | **Yes.** |
| **Order distance** | Placement as a **% of the range**, measured from the lower boundary for buys. Negative places outside the range; above 100% crosses to the far side. | 5% | **Yes.** |
| **Use adaptive order distance** | Adds the average frame-to-frame boundary change to the order price, leaning into the trend. | off | |
| **Order lifetime** | Seconds before an unfilled order is cancelled. | 1s | **Yes — and the prime directive names order lifetime explicitly as a must-derive parameter.** |
| **Max orders** | Cap on unfilled orders **plus** open positions, counted together. | 3 | |
| **Order frequency** | Seconds between placements while the trigger stays active. Replaces the usual restart delay. | 0.1s | **Yes.** |
| **Detect shot** | Alternative mode: examine only the **last** frame for a sharp spike, then expect a pullback of the given % of the move. **Disables Time Frame and both Border Range filters.** | 80% pullback | **Yes.** |
| **Shot direction** | Up or Down, for Detect Shot. | | |

Take Profit is **% of the frame spread**, not of price — see §1.7. Stop Loss is % of the order
placement price, as normal.

> **Note.** Vector needs sub-second frames with per-frame trade counts and volumes. Our fomc tape
> has **no trade stream** (see §6), so Vector can only be mined from the June archive's
> `trades_shard*.jsonl`, not from the fomc capture.

---

## 5. Averages

**What it is.** Compare a long moving average against a short one. When the gap lands in your band,
place orders — optionally a grid underneath for averaging down. This is the mean-reversion / DCA one.

| Parameter | What it does | Default | Notes |
| --- | --- | --- | --- |
| **Period (long)** | Long averaging window, 1s to 24h. | 1 min | Scalping 30s–2min; short-term 5–15min; medium 1–4h. |
| **Period (short)** | Short window. | 10 s | Keep it 3–10x smaller than the long one. **If short exceeds long, the delta sign inverts.** |
| **Trigger range** | Band of long-minus-short divergence, in %, that fires the order. Smaller value first or the band is empty. | | Positive delta = short-term downtrend. Negative delta = short-term uptrend. Older docs expose a single **Trigger Distance** instead. |
| **Add parameter** | Extra period-pairs with their own ranges. **AND logic** — all must hold simultaneously. | | This is multi-timeframe confirmation. |
| **Order price distance** | Offset from the trigger price, in %. **Sign is absolute, not relative to side:** positive is above price, negative is below. | | So `+1%` with Side=Buy is an **instant market buy**. Same trap as Shot's negative distance. |
| **Cancel delay** | Seconds before unfilled orders are cancelled. | | Fast 10–60s, moderate 60–300s, patient = disabled. **Mineable.** |
| **Do not cancel if executed** | If one grid order fills, the rest survive the cancel delay. | | |
| **Cancel on first deal** | Cancels the whole remaining grid when the first position **closes** (TP or SL). | | Enable = lock profit, do not average. Disable = keep averaging. Takes priority over the previous flag. |
| **Limit position size** | Max combined order+position volume **per pair**. | | 15 pairs at a 100 USDT cap is 1,500 USDT of total exposure. Counts only this algorithm's orders. |

**Multiple orders (the grid)**

| Parameter | What it does |
| --- | --- |
| **Relative to** | *First* → sizes 100, 200, 300, 400 (`S(n) = S(1) × (1 + (n−1)×%)`). *Previous* → 100, 200, 400, 800 (`S(n) = S(n−1) × (1 + %)`). |
| **Count** | Number of **additional** orders. `3` means four orders total. |
| **Distance** | % step between grid levels. Positive walks away from price, negative walks toward it. |
| **Size increase** | % change per step. Aggressive 50–150%, moderate 20–50%, uniform 0%. |

> **Standing caution.** A martingale grid is the one thing on this page that can lose more than the
> position size, and `cancel_defense.py` already measured what thin books do on the short side
> (−54%, unbounded on a pump). If Averages ever gets built, the grid goes on majors with a hard
> **Limit position size**, or not at all.

---

## 6. Signal

Reads trade calls out of Telegram channels via BotClient and places orders from them. No market
analysis of its own.

| Parameter | What it does |
| --- | --- |
| **Channels** | Which Telegram channels to listen to. Must also be activated in BotClient. |
| **Max signal latency** | Seconds; a signal older than this is discarded. Also applies to exchange data latency. |
| **Do not trigger if active** | Ignore new signals while a previous order is still open. |
| **Parsing rules per channel** | Words to ignore / buy / sell / spot / USDT-M / COIN-M. |

TP/SL precedence: signal values win over algorithm values; if the signal has none and the algorithm
has none, **no protection is placed at all**.

**Not relevant to us.** Recorded for completeness only.

---

## 7. Data we hold against what these parameters need

| Algorithm | What mining it requires | Do we have it? |
| --- | --- | --- |
| **Shot** | A price/quote series at sub-second resolution + a fill model. | **Yes.** fomc tape (BTC/ETH/SOL, 5 days, every best-bid/ask change) and the June archive `bbo_shard*`. Mine is running. |
| **Depth Shot** | Cumulative order-book depth over time. | **Yes.** fomc tape has 20 levels both sides at 100ms; June archive has `book_shard*`. |
| **Vector** | Sub-second frames with **trade counts and volumes**. | **Only from the June archive** (`trades_shard*`). The fomc tape has no trade stream. |
| **Averages** | Price series only. | **Yes**, trivially. |

### The fomc tape gap — verified 2026-08-11

`fomc_tape.py` subscribed to five Binance streams per symbol. **Only two ever arrived.** The tape
contains `@bookTicker` and `@depth20@100ms` and nothing else — no trades, no liquidations, no funding
rate — across all 257,851,483 messages.

Reproduced live on the identical URL, then tested each stream name alone:

| Stream requested | Delivered? |
| --- | --- |
| `btcusdt@bookTicker` | yes |
| `btcusdt@depth20@100ms` | yes |
| `btcusdt@trade` | **yes — 36 msgs / 12s** |
| `btcusdt@aggTrade` | **no — silent, zero messages** |
| `btcusdt@markPrice@1s` | **no — silent** |
| `btcusdt@markPrice` | **no — silent** |
| `btcusdt@forceOrder` | zero, but inconclusive (only fires on an actual liquidation) |

Binance drops the names it does not accept **without any error**, so the recorder logged a clean
connection and 290 million messages while quietly missing 3 of its 5 feeds for five days.

**Consequences.** Any future capture should use `@trade` rather than `@aggTrade`, and must verify
which streams actually arrive before being left to run. The June archive is unaffected — it was
written by a different script and does contain `trades_shard*` files.

---

## 8. The short version — what to build first, and why

The liquidation-fade harvest (2026-08-11, `THE_PLAN.md` item 2) lost −0.10% per signal with a
hand-picked fixed bracket, and the autopsy named four distinct failure modes. This page contains a
purpose-built mechanism for each one, and **not one of them has ever been tested here**:

| Measured failure | Mechanism | Section |
| --- | --- | --- |
| All 141 stop-outs realized worse than the stop (−1.362% vs −1.180% set) | **Stop Loss → Spread** and **Stop Loss → Delay** (1–2s, so you are not stopped inside the spike that filled you) | 1.8 |
| 120 signals positive at the hour but under target, averaging +0.257% | **Auto Price Down** | 1.7 |
| Winners giving back gains before reaching a fixed target | **Second Stop Loss** (`trigger − percentage`) and **Trailing Stop** | 1.8 |
| Blowups on junk — CASHCAT −6.4%, GRAM, ASTEROID | **Price step filter**, **Symbol QAV filter**, **Active markets filter** | 1.5, 1.4 |

Ranked by expected value, the order to mine is: **Depth Shot's adaptive take-profit modes**
(Historic and Depth), then **Auto Price Down**, then **Stop Loss Delay**, then the junk filters.
The first three attack the exit, and every failure this repo has recorded has been an exit failure
rather than a signal failure.
