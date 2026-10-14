# screener — live perp screener

Replaces the TradingView crypto screener workflow: sort every alt by % change
since the daily open, but at 2 updates per second instead of one manual refresh
every 10 seconds, and with a venue column TradingView cannot give you.

Runs on a Vultr Tokyo box at `/root/screener/`, reached by the SSH alias
**`tokyo2`**. Source of truth is this folder.

Which machine `tokyo2` means has changed three times. Since 2026-10-10 it is
`139.162.112.103`, an **Akamai Cloud (ex-Linode) Nanode 1GB in Tokyo**: Alpine,
1 core, 1 GB, $5/month flat, about 3ms from Binance. The alias name stayed
`tokyo2` because `desk.command`, `screener.command` and `sync_and_refresh.sh`
all dial it. Dead predecessors, none of them to be deployed to: the original
`tokyo` (`202.182.110.74`, bill lapsed 2026-08-31), the second Vultr box
(`207.148.114.69`, wiped by accident 2026-10-09), and `66.42.44.181` (Vultr
"tokyo3", lived one day — Vultr closed Steven's account on 2026-10-10 over the
unpaid bare-metal bill from the original box and linked the new account to it).
**Vultr is off the table**; a fresh account gets linked and closed the same way.

**If the box dies again:** spin up any Tokyo box with your SSH key on it and root
login allowed (Alpine, Debian or Ubuntu, not IPv6-only; Akamai's Nanode works as
is), point the
`tokyo2` entry in `~/.ssh/config` at its IP, and run `./deploy_fresh.sh`. It
installs Python, copies the code, sets the boot hook, fixes Alpine's tunnel
block, starts the server and waits for a live feed. About five minutes. A wipe
loses only `bars.db`, which reseeds ~8 hours on first boot; stars, notes, marks
and the watchlist live in the browser. It has to be a non-US box: Binance
futures answers HTTP 451 to US addresses, so this cannot run on the Mac.

---

## Getting at it from the Mac

The server binds to `127.0.0.1` on Tokyo, so it is not exposed to the internet.
Open an SSH tunnel, then use a normal browser tab:

```sh
ssh -N -L 8777:127.0.0.1:8777 tokyo2
```

Leave that running, then open <http://localhost:8777>.

Or just double-click `screener.command` in this folder — it opens the tunnel and
the browser tab, and holds the tunnel until you close the window.

---

## What the universe is

Every pair that is a live USDT perp on **Gate** or **Aster** *and* also exists as
a Binance USD-M perp. That is **515 pairs** as of 2026-08-29.

- Gate lists 942 live USDT perps, Aster lists 538.
- Their union is 994 distinct assets.
- 515 of those have a Binance perp; Binance has 524 USDT perps total, so the
  Gate/Aster union covers essentially all of Binance.
- The 479 that drop out are stock perps (AAPL, AMD, TSLA, …) and venue-native
  tokens (ANTHROPIC, ASTEROID, …). No Binance feed exists for them, so no data
  can be shown. The header counts them so nothing disappears silently.
- `1000PEPE` and `PEPE` are treated as the same asset when matching venues,
  since the multiplier does not change a percentage move.

The `venue` column tells you where you can actually take the position:
`G` = Gate, `A` = Aster, both badges = both.

The universe is rebuilt every hour, so new listings appear on their own. It was
515 pairs on 2026-08-29 and 517 on 2026-09-09; it drifts as venues list and
delist.

### The index-drift trap (fixed 2026-09-09, do not reintroduce)

Rows reach the browser as bare arrays keyed by **position in the sorted symbol
list**, not by name — it keeps the 2 Hz feed small. `build_universe()`
renumbers every pair by its new position, so adding or removing one pair shifts
the index of everything after it in the alphabet.

The browser only relearns that mapping when the server sends a snapshot. The
original `universe_loop` sent one `if added:` — additions only. So a **pure
delisting** silently shifted every index after it and told nobody, and the page
went on painting each price into its neighbour's row. The symptom is a handful
of pairs showing prices that look invented rather than merely stale, which then
heal by themselves whenever the next snapshot happens to fire.

It now re-snapshots on any change to the ordering. If you touch this code, the
rule is: **any reordering of `by_i` must be followed by a snapshot broadcast.**

---

## Where the numbers come from

All prices and volumes are Binance USD-M futures.

**The ticker websocket does not work from this box.** `!miniTicker@arr`,
`!ticker@arr`, `@aggTrade`, `@kline_1m`, `@markPrice` and `@forceOrder` all
complete the handshake (HTTP 101) and then send zero bytes, forever.

That block is narrower than it first looked. Re-tested 2026-08-29 evening:
`@bookTicker`, `@trade` and `@depth` **do** stream from this box, 6ms from
Binance's event timestamp to arrival, and a bulk `SUBSCRIBE` of 200 streams on
one connection runs at 2158 msg/s. Three connections cover all 515 pairs at
2574 updates/s.

**It is still not worth switching this dashboard to them.** Measured head to
head over the same 90-second window, the websocket sees 6.4× more distinct
price changes than the 2 Hz poll and not one displayed column changes: the poll
captures 104% of the websocket's 60-second `spike` figure, because `bookTicker`
gives bid/ask mid and mid is smoother than last trade. Nothing here reads
faster than a human glance, so 500ms of staleness is invisible. And on thin
pairs `bookTicker` goes quiet when the book stops moving — `ALLUSDT` sent 4
updates in 90 seconds against 180 poll samples — which would make the `still`
column lie.

So the live feed polls futures REST instead:

| what | endpoint | weight | how often |
| --- | --- | --- | --- |
| price, every symbol | `/fapi/v1/ticker/price` | 2 | 2× per second |
| 24h volume / high / low | `/fapi/v1/ticker/24hr` | 40 | every 15s |
| day open, day high/low, 7d open | `/fapi/v1/klines` `1d` | 1 × 515 | every 4 min |
| 4H and 1H candle opens | `/fapi/v1/klines` `5m` | 2 × 515 | every 20 min |
| warm-start history | `/fapi/v1/klines` `1m` | 1 × 515 | once at boot |

That is roughly 500 weight per minute against Binance's 2400/min cap, and the
poll round-trip from Tokyo measures ~5ms. Requests reuse one keep-alive TLS
connection; re-dialling twice a second would cost more than the request.

The websocket is retried every 10 minutes in the background. If Binance ever
unblocks `!miniTicker@arr`, the server switches to it automatically and the
polling stands down — the header pill tells you which feed is live.

Separately: `fomc_tape.py` restarted today would **not** record nothing, which
is what an earlier version of this file claimed. Its exact 15-stream URL still
delivers 23,941 `bookTicker` and 685 `depth20` messages in 25 seconds — mixing
dead streams into a `/stream?streams=` list does not poison the live ones. What
it loses is the trade tape, plus liquidations and mark price. Swapping
`@aggTrade` for `@trade` recovers trades at higher resolution.

---

## Columns

Lower-case timeframes are **rolling** (last N minutes from now).
Upper-case timeframes are **candle-aligned**, the way TradingView shows them.

| column | meaning |
| --- | --- |
| `1m` `5m` `15m` `1h` | rolling change over that many minutes |
| `4H` | change since the 4-hour candle open |
| `1D` | change since the daily candle open — the reset you already work off. It is 00:00 UTC, which is 8pm ET now and 7pm ET once daylight saving ends in November. The header countdown tracks UTC, so it stays right either way |
| `7D` | change since the daily open 7 days ago |
| `Δmark` | change since the price each pair was at when you last hit **mark now**. This is the "what happened while I was away" column |
| `Best↑` `Worst↓` | the best and worst each pair reached since the mark, not where it ended up. `Δmark` is a **net** figure, so a pair that ran +35% while you were away and gave it all back reads `0.00` and looks like it never moved — which is exactly the pair you wanted to see. These come from the on-disk minute bars, so they are right even if the tab was closed |
| `From 7dLo` | how far above its 7-day low the price is |
| `LowAge` | days since that 7-day low. Up 180% off a low set five days ago is late in the move; up 60% off yesterday's low is early. The screener used to show those two identically |
| `From 7dHi` | how far below its 7-day high the price is |
| `day hi` / `day lo` | day high and day low against the day open — how far it got at best and worst today |
| `range` | where price sits inside today's high–low band. Pinned right = still at the highs, pinned left = the pump faded |
| `→tgt` | distance to a target price you typed in, if you set one |
| `vol 24h` | 24h quote volume |
| `vol/hr` | quote volume this hour is on pace for |
| `rvol` | that pace divided by this pair's own median hour over the last day. Above ~3 means it just woke up |
| `spike` | biggest excursion inside any 60-second stretch of the last minute. Catches the fast wicks a 10s refresh walks straight past |
| `RSpike` | that same spike divided by **this pair's own** typical minute. `3.0` means three times jumpier than it normally is. Raw `spike` is not comparable across pairs — 2% on BTC is an event, 2% on an illiquid alt is a Tuesday — so sorting by raw spike keeps surfacing the same permanently-jumpy junk. This is the fixed version, and it is to `spike` what `rvol` is to volume |
| `Hits(1h)` `Hits(6h)` | how many **separate minutes** in that window moved at least 1%. This is the persistence measure: one spike is noise, forty spikes over six hours is a campaign. Non-overlapping minutes, so a single long move can't inflate the count |
| `MaxPrint` | largest single trade in the last 5 minutes, in dollars |
| `Big(1h)` | how many trades over $50k printed in the last hour |
| `Flow` | share of the last 5 minutes of notional that hit the ask rather than the bid. `+100` is all aggressive buying, `-100` all aggressive selling |
| `still` | seconds since the price last actually moved. A big number is a dead pair — worth checking before you plan an exit on it |

The header counts down to the next daily reset.

### Where the spike-persistence and trade-tape numbers come from

`Hits(1h)`, `Hits(6h)` and `RSpike` run off a per-pair record of **completed
minutes**: for each minute, how far price travelled inside it. Time is cut into
whole non-overlapping minutes on purpose, so one long grind can't be counted as
forty separate spikes.

That record is rebuilt from 1-minute klines every 30 minutes, and topped up live
in between. A candle's high-low range is the same measurement the live counter
makes, so the two agree. It also means both counters are correct the moment the
server boots rather than an hour later, and a restart costs nothing. The
backfill is 499 candles per pair — about 8 hours, which fully covers the 6-hour
window — at roughly 1000 weight per pass, which is nothing spread over half an
hour.

`RSpike` divides the current spike by the median minute from that same window,
which is why it means the same thing on every pair.

`MaxPrint`, `Big(1h)` and `Flow` come from the raw `@trade` websocket, on one
socket carrying every pair. **`@trade` works from Tokyo even though `@aggTrade`
does not** — re-verified from `tokyo2` on 2026-09-09 at 8ms lag. Measured cost
for all 718 Binance perps: 2319 messages/sec for 14% of one core, and the box
has one core, so the whole universe fits with room to spare. Trades are stored
as 10-second buckets rather than raw prints — BTC alone does about 75 trades a
second and keeping them all would buy nothing.

The header pill shows `tape` when that socket is live. If it drops, those three
columns go quiet and everything else carries on; nothing else depends on it.

### The on-disk bar store

One-minute bars for every pair go into `bars.db` (SQLite, WAL). About 744k rows
a day across 517 pairs, roughly 30 MB, kept for 30 days by default — against
24 GB free on the box. The old server was 95% full, which is the only reason
this was never done before.

It buys three things. History survives a restart. Lookback reaches days rather
than the ~93 minutes the memory buffers hold. And `Best↑`/`Worst↓` become exact
instead of only-while-the-tab-was-open.

It is **best-effort by design**: every call is wrapped, and if the database
misbehaves the screener carries on without it. Nothing on the live path depends
on it. Disable entirely with `--no-bars`, or change retention with
`--bars-days N`.

Seeded at boot from the same 1-minute klines that feed the spike counters, so a
fresh box has ~8 hours of bars immediately rather than starting empty.

Two endpoints read it: `/api/since?ts=` returns each pair's high and low since
a timestamp (the browser turns those into `Best↑`/`Worst↓` against its own mark
price, since only the browser knows what it marked at), and
`/api/bars?sym=&hours=` returns a down-sampled series for one pair.

`vol/hr` and `rvol` come off the hourly candle, refreshed with the anchors every
4 minutes and nudged live in between by whatever the 24h volume counter has
gained. The obvious shortcut — subtracting the rolling 24h volume from itself 5
minutes ago — does not work: that counter *falls* whenever old volume ages out
of the window, so it reads zero volume on exactly the pairs that spiked a day
ago. The hourly candle only goes up.

## Split view

**split view** opens a second pane showing the same pairs sorted the other way —
top gainers on the left, top losers on the right, both live. The page still
opens single-pane; the button and everything below it are remembered across
reloads.

- **link sort** (on by default) mirrors the two panes. Click any column header
  in either pane and both panes sort by it, one ascending and one descending, in
  one click. If the other pane wasn't showing that column it gets added rather
  than sorting by something invisible. Turn linking off to sort the two panes by
  different columns.
- **stacked** flips the panes from side-by-side to top-and-bottom. Side-by-side
  gives you more rows; stacked gives each pane the full window width, so all 23
  columns fit without horizontal scrolling.
- **cols** on each pane picks that pane's columns, with `all` and `compact`
  presets. Compact is pair, venue, price, 1m, 5m, 15m, 1h, 1D, Δmark, rvol,
  spike, still — it fits beside the full pane with no horizontal scroll, and is
  what the second pane starts on. `pair` cannot be switched off. Hiding the
  column a pane is sorted by drops it back to `1D` rather than stranding it.

Search, venue, min-volume, top-N, star-only and freeze stay shared across both
panes on purpose — the point is to see one set of pairs from both ends, not two
different sets. Alerts flash the row in both panes and beep once.

Searching re-filters the whole universe. It used to only hide non-matches among
the rows a pane had already drawn, which is the list *after* the top-N cut — so
searching a pane sorted by up-spikes could never surface a pair that had just
spiked down, because it was never in that pane's rows to begin with. Fixed
2026-09-09.

## Watchlist

**watchlist** opens a fixed panel down the right-hand side. It is a small list
of pairs you choose by hand, and unlike the table it never reorders itself, so
the same pair stays under the same part of your eye. Each entry shows the
symbol, the live price, and exactly one percentage.

That percentage is measured from a **basis** you set per pair: `1m` `5m` `15m`
`1h` `4H` `1D` `1W` or `mark`. A pair you have not given one to follows the
default at the top of the panel, which starts at `1D`. Change that default and
every pair still following it moves with you; a pair you set explicitly stays
put.

`mark` means "since a price I picked", and it resolves in two steps:

- if that pair has its own mark, it uses that
- otherwise it falls back to the global **mark now** from the header

So the header mark keeps being the baseline for everything, and an individual
mark only overrides it for the one pair. The line under each entry tells you
which of the two you are looking at and what price it is measuring from.

Two ways to set a pair's own mark. The **mark** button takes the price right
now. The time box next to it back-dates — type `07:26` and it uses the price
the server actually recorded at 07:26. That reaches back about 93 minutes,
because it reads the same rolling buffers the percentage columns run on. Ask
for something older and it says how far back it can actually go rather than
inventing a number. A time later than the current clock is read as yesterday.

The add box is a picker, not a bare input: type and a dropdown appears with the
live price and 1D change for each match, and prefix matches sort above
substring ones, so `sol` gives SOL before SOLV before RESOLV. With the box
empty it lists the busiest pairs by 24h volume rather than the top of the
alphabet. Arrow keys move, Enter adds.

Each row in that dropdown has two separate actions. The **+** on the right adds
it to the watchlist and leaves the list open so you can add several. Clicking
the **name** hands the pair to the order ticket in the desk — see below. The
bare asset is enough either way, so `XTZ` finds `XTZUSDT`.

**+★** pulls in everything you have starred. The watchlist is separate from
stars on purpose — adding something here does not drag it to the top of the
main table. It is stored in the browser and survives a reload.

## Running inside the desk

This page is embedded as an iframe by the desk app in
`~/projects/cleude/desk`, which puts the Gate positions table underneath it.
Start both with `~/projects/cleude/desk.command`.

It is embedded **from its own origin** (`localhost:8777`) rather than proxied
under the desk's. That is deliberate: `localStorage` is per-origin, so serving
this page from anywhere else would orphan every star, note, target, column
layout and the global mark. One origin, one set of state, whether you open it
inside the desk or on its own.

The consequence is that the two pages are cross-origin and cannot read each
other, so they talk over `postMessage`. This page posts
`{source:'screener', type:'pick'|'trade', sym, base, gate, venues, last}` when
a table row is clicked, or when a name is clicked in the watchlist dropdown.
The desk answers by loading that market into the order ticket. Nothing is
placed by a click — it only fills the form in.

Opened on its own there is no parent frame, `EMBEDDED` is false, and every one
of those paths falls back to what it always did (TradingView in a new tab).

One thing the desk knows and this page does not: the `venue` badge here comes
from Gate **CEX**, and trading happens on Gate **Web3**, which is a different
product with a different market list. 56 of the pairs badged `G` do not exist
on Web3. The desk checks and says so plainly instead of letting the order fail.

## Controls

- Click any header to sort. Click again to flip direction. Default is `1D` descending.
- **freeze order** stops rows jumping around while you read, without freezing the numbers.
- **mark now** snapshots every pair's price. The `Δmark` column then shows the change since that moment — set it when you walk away, read it when you come back. It is also the fallback baseline for watchlist entries set to `mark`.
- **★** stars a pair. Starred rows pin to the top and survive a reload. **★ only** hides everything else.
- **✎** attaches a note and a target price to a pair. The target drives the `→tgt` column.
- **alert |1m| ≥ x%** flashes a row when it moves that much in a minute; **sound** adds a ping.
- **campaign ≥ N hits/h** is the better alert. It fires **once**, when a pair crosses N spike-minutes inside the last hour — the campaign starting, rather than every individual twitch. A single-minute alert on a jumpy alt fires constantly and teaches you to ignore it; this one fires on the state change. Anything that fires is logged to a **Woke up:** strip under the header, so a ping you missed is still there when you look back. Re-arms after 15 minutes per pair.
- **Campaign Hunt** sets up the layout in one click: split view, both panes sorted by `spike` in opposite directions, and the campaign columns switched on in both. That is the arrangement being rebuilt by hand every session.
- Venue filter does the thing TradingView cannot: Gate only, Aster only, or both.
- Clicking a pair name opens its Binance perp chart on TradingView.

Stars, notes, targets and the mark are stored in the browser, not on the server.

---

## Running it

```sh
ssh tokyo2
/root/screener/run.sh start | stop | restart | status | log
```

It runs under a supervisor loop that restarts the process if it dies. Logs go
to `/root/screener/screener.log`. Boot takes ~75 seconds to load anchors for all
515 pairs; the page is usable before that finishes and fills in as it goes.

To deploy a change from the Mac:

```sh
scp server.py index.html tokyo2:/root/screener/ && ssh tokyo2 /root/screener/run.sh restart
```

`index.html` is read from disk per request, so front-end edits only need the
`scp` and a browser refresh. `sync_and_refresh.sh` watches `index.html` and does
exactly that on every save, including reloading the browser tab.

Endpoints: `/` the page, `/ws` the feed, `/api/state` a JSON snapshot for
debugging, `/healthz` 200 when a feed is live and 503 when it is not,
`/api/pxat?sym=&ts=` the price a pair traded at a past epoch-second (used to
back-date a watchlist mark; reaches back as far as the rolling buffers do,
about 93 minutes, and 404s with the oldest timestamp it does have if you ask
for more).

Options: `--port 8777`, `--bind 127.0.0.1`, `--hz 2` browser push rate,
`--poll-hz 2` Binance poll rate. Raising `--poll-hz` above 4 starts eating into
the Binance weight budget the kline refreshes need.
