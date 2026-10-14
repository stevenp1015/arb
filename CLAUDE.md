# CLAUDE.md — `/Users/ttig/projects/arb`

**Last verified against disk: 2026-08-03.** Everything below was checked against actual file
timestamps on that date. If you are reading this much later, check the dates before trusting the
"active" list — this file has drifted badly before, which is why it was rewritten.

Rewritten 2026-08-03. The version before this one described only `grad_sniper`, a Solana bot last
edited on 2026-04-25, and handed that project to every new session as its standing orders. The old
file is preserved at `CLAUDE.md.superseded-2026-08-03`.

---

## What this repo actually is

A trading-research workspace. Not one project — many, built one after another over about four
months, most of them finished or abandoned rather than deleted.

The work is almost entirely **research**, meaning: collect market data, look for a repeatable edge
in it, and measure honestly whether that edge survives the cost of actually trading it. Venues
involved are Hyperliquid, Aster, Lighter, Binance, and Phemex. There is older Solana work too.

**Nothing here is trading real money right now.** Steven confirmed this directly on 2026-08-03.
Programs running on the Tokyo server are collecting data, not taking positions. Some older
documents in this repo describe bots as "LIVE with real money." Those documents are out of date.
Do not write anything, or advise anything, that assumes an open position exists.

**Disk is nearly full** — 95% used, roughly 22 GB free as of 2026-08-03. A disk-full incident on
2026-07-12 silently killed a data collector for twelve days before anyone noticed. Check free space
before starting anything that writes a lot.

---

## The stated mission, and what has happened to it

*Folded in from `FRONTIER.md` on 2026-08-03.*

The last time anyone wrote down a mission was 2026-07-27. Word for word:

> A Rust bot running MoonTrader-inspired algos WITHOUT MoonTrader (builder fees), with parameters
> mined from our own collected data. Everything else is a side quest.

MoonTrader is a third-party trading platform. "Without MoonTrader" means avoiding the fee it charges
for building on top of it — copy the ideas, run them on our own code.

The reasoning behind that, from the same date: the May work found a real edge from quoting
passively, but it died under Hyperliquid's round-trip cost of roughly 0.03%. The one thing that could
save it was cheaper execution. Lighter charges zero fees, so Lighter was named as the venue that
unblocks the mission.

**What happened next matters.** `fp_engine` measured exactly that idea end to end in late July and
early August, at zero fees, and it still loses — about -0.005% per fill. The zero-fee venue was not
enough on its own. Two follow-ups narrowed it further: the cross-venue version of the edge exists
only at near-zero reaction delay and inverts by 100 milliseconds, and pulling your quote quickly does
not rescue passive quoting.

That does not formally retire the mission, and nobody has written a replacement. Treat the statement
above as the last thing declared, not as a settled plan. Ask Steven before assuming it still holds.

### Findings that survive from the retired MoonTrader work

`moontrader/` froze on 2026-05-30, and Steven declared its documents, configs, backtester, and
`pump_bot` obsolete on 2026-07-28. Three results from it were recorded as still standing:

- Paying the spread to get in — "taker" — is dead on these markets. Quoting passively and waiting to
  be filled — "maker" — is the only version worth testing.
- The book-imbalance signal is real but too small to cover about 0.03% in costs. Book imbalance means
  comparing how much size is resting on the buy side of the order book against the sell side.
- The fear that passive orders would never actually get filled was tested and disproven live: 19
  fills in 90 minutes on Hyperliquid.

---

## What is active right now

### `screener/` — live perp screener dashboard (added 2026-08-29, moved servers 2026-08-31)

A trading tool, not research. Replaces the TradingView screener that Steven refreshes by hand: every
USDT perp listed on Gate or Aster that also exists on Binance (516 pairs), updating twice a second,
with a venue column TradingView cannot give you and columns built for his "check back every few
hours" habit. Runs at `/root/screener/`, reachable from the Mac over an SSH tunnel on port 8777
(`screener/screener.command` opens it). Full detail in `screener/README.md`.

**Runs on a different box than the rest of this file describes.** The original Tokyo server
(`202.182.110.74`, SSH alias `tokyo`) went offline 2026-08-31 — Steven's Vultr bill on it lapsed.
Every other mention of "the Tokyo server" / "Tokyo box" in this file, and everything `INFRA_AUDIT.md`
says about `202.182.110.74`, is about that now-unreachable machine — fp_engine, hl_data, and the rest
of the bot fleet described below live there and are presumably not running right now. The screener
was redeployed onto a second Vultr Tokyo instance, `207.148.114.69`, new SSH alias `tokyo2`. **Update 2026-10-10:** that second box was wiped by
accident on 10-09, the one-day replacement `66.42.44.181` died when Vultr closed Steven's account over the
old bare-metal bill, and the screener now runs on **Akamai Cloud (ex-Linode), `139.162.112.103`**, Alpine,
Tokyo. The `tokyo2` alias in `~/.ssh/config` points there. Vultr is off the table. Rebuild recipe if it
happens again: `screener/deploy_fresh.sh`. `tokyo`
was deliberately left pointed at the old box rather than repointed, in case the bill gets paid and it
comes back. Ask Steven before assuming anything about the old box's status one way or the other.

**Two facts discovered while building it that matter beyond this folder:**

1. **`wss://fstream.binance.com` is dead from the Tokyo box.** It returns HTTP 101 and then sends
   zero bytes, on all 24 edge IPs. Binance spot websocket and Binance futures REST both work fine
   from the same box, so this is a Japan geo-block on futures market-data streaming. It means
   `fomc_tape.py` would connect, look healthy, and record nothing if restarted.
2. **Binance's binding REST limit on these endpoints is 2400 *requests* per minute per IP**, not the
   documented 2400 weight per minute. Their own 429 body says so. Budget by request count.

### The two research directories

Two directories, and only two, had been touched in the ten days before 2026-08-03.

### `fp_engine/` — the main line of work

A set of Python scripts that answer one question over and over: **after a signal fires, does the
price travel far enough in your favour before it travels against you?** That question is called
*first passage* — there is no clock and no fixed holding time, you just ask which happens first, the
favourable move or the adverse one. Steven treats this as the only honest way to evaluate a signal.

Newest file in the entire workspace: `cancel_defense.py`, 2026-08-01. The main burst of building was
2026-07-28 and 07-29.

What it has found so far, in order:

1. **A real but small edge.** A model pooled across 84 coins on June data wins about 3 to 5
   percentage points more often than chance, and that holds up on data it was never tuned on.
2. **The edge does not survive execution.** Once you charge it the real cost of getting filled, it
   loses money both ways — about -0.005% per fill quoting passively, about -0.011% per trade paying
   the spread. Even at zero fees. Bitcoin is the closest to breaking even at -0.002%.
3. **The edge only exists at near-zero reaction delay.** A cross-venue test on 2026-08-01 found
   +4.05 percentage points at zero delay, and *negative* by 100 milliseconds. Whether it survives at
   the real reaction time of about 15ms is unresolved and needs finer data than the test had.
4. **Cancelling your quote fast does not fix it.** Tested 2026-08-01 and rejected. The benefit is
   ten times smaller than the fee it would cost. The reason matters: you get filled passively
   *because someone chose to trade against you at that price*, so the bad news is inside the fill
   itself, not in an earlier signal you could have raced.

Full detail: `~/.claude/projects/-Users-ttig-projects-arb/memory/project_fp_engine.md`.

### `sma_touch/` — a scanner, last run 2026-07-22

Small and self-contained. `build_universe.py` builds a list of 524 crypto perpetual-futures markets
across Aster, Phemex, and Lighter, dropping the stock and forex listings. `scan_sma.py` then pulls
about 200 daily candles for each coin, computes the 50-day simple moving average, and reports the
most recent day in the last 60 where the day's price range touched that average line. Results are in
`results_2026-07-22.csv`.

What the scan does is unambiguous. **What the touch list is for is not documented anywhere in the
folder — no README, no notes. The trading intent is unknown and needs a human.** The most likely
connection is stash item T-21, "Steven's secret: SMA/FVG magnet confluence," but that link is
unconfirmed — do not build on it without asking Steven.

### Not projects, but recently written

- `.remember/` — the session-memory plugin writing its own logs. Housekeeping.
- `nimbalyst-local/` — two editor scratch files. Not trading.

---

## What is parked

Real work, real code, nobody has touched it in weeks. Do not build on any of these without asking
Steven first.

| Directory | What it is | Last touched |
| --- | --- | --- |
| `carry/` | Funding-rate carry scanner. Find a pair where you can go long on one venue and short on another, and collect the funding payment. Python plus a small Rust `lighter-cli`. | 2026-07-20 |
| `pythview/` | JavaScript charting front-end that reads Pyth price data and draws candles. | 2026-07-12 |
| `lighter/` | Work against Lighter, the zero-fee venue. Rust plumbing (`lighter-rs`, `lighter-sdk`) plus `ma_bot/`, which holds lead-lag capture scripts comparing Lighter to Aster. | 2026-07-06 |
| `aster/` | React dashboard for the Aster exchange — positions view, market chart. Web interface only. The Aster *research* lives in `fp_engine/`. | 2026-07-03 |
| `grad_sniper/` | The Solana pump.fun graduation sniper. See the section at the bottom of this file. | Code: 2026-04-25 |

**Dead, with a written verdict:** `pack_verdict/` (2026-07-27). It holds the autopsy of the
"pack_rider" bot, not ongoing work. The verdict is in `THE_PLAN.md`: 82 fresh events, mean -6.7
hundredths of a percent per event, only 39 of 82 positive. Failed. `THE_PLAN.md` also records a
tempting after-the-fact observation that later events look positive, and explicitly says **do not
treat that as a pass.**

**Archived history:** 28 more top-level directories have nothing newer than 2026-07-01. They are
old strategy iterations, virtual environments, bulk data, and saved transcripts. `STATE_OF_THE_UNION.md`
lists every one of them with a size and a one-line description.

---

## Where to look things up

| File | What it gives you |
| --- | --- |
| `STATE_OF_THE_UNION.md` | **The full directory map.** All 38 top-level directories with status, newest file, and evidence. Where the documents and the disk disagree. What could not be determined. Generated 2026-08-03. Start here when you need to find something. |
| `THE_PLAN.md` | The research queue. Eight numbered items in three phases, each with its data source, the question it answers, and what success looks like. Annotated with verdicts through 2026-07-27. Ends with "Standing laws," the eight rules for how work gets judged here. Read those. |
| `~/.claude/projects/-Users-ttig-projects-arb/memory/MEMORY.md` | The memory index. Every project note, session handoff, and behavioural rule. See the next section. |
| `INFRA_AUDIT.md` | The Tokyo server audit, being written separately. Every claim in this repo about what is running on 202.182.110.74 is unverified from local disk. Check there before believing any of them. |

`FRONTIER.md` used to sit in this list. It was written 2026-07-27 and went stale within a day. It
has been folded into this file and marked superseded — do not use it as a source of truth.

---

## Memory system (SHARED ACROSS BOTH CLAUDE INSTANCES)

Steven runs two separate Claude Code instances: `claude` (config dir `~/.claude`) and `cleude`
(config dir `~/.cleude`). Native auto-memory is per-instance and does NOT cross the boundary. Both
instances read this `CLAUDE.md`, so this is the one place a shared rule can be written down.

**Authoritative path: `/Users/ttig/.claude/projects/-Users-ttig-projects-arb/memory/`**

That changed on 2026-08-03. The old rule pointed at `grad_sniper/docs/memory/` and called the path
above "legacy." It was exactly backwards. The grad_sniper folder stopped being updated on
2026-06-01; the folder it called legacy was updated on 2026-08-02 and is where every note about
July and August work actually lives.

Rules:

1. At session start, read `~/.claude/projects/-Users-ttig-projects-arb/memory/MEMORY.md` (the index)
   before reasoning about prior sessions. It lists every session handoff, feedback memory, project
   memory, and reference.
2. When saving a new memory (user / feedback / project / reference type), write it to that same
   folder. Follow the frontmatter + `MEMORY.md` index pattern from your system prompt.
3. The other instance's session handoffs ARE yours too — read them even if authored by the other
   instance.
4. Two older indexes exist and are now superseded, each with a header saying so:
   `grad_sniper/docs/memory/MEMORY.md` (last real update 2026-06-01) and the root `MEMORY.md` (its
   links point at files that are not beside it). Their contents remain on disk. Do not add to them.
5. `steven_stash.md` in the authoritative folder is the single open-threads queue. A second, older
   stash in `grad_sniper/docs/memory/` was merged into it on 2026-08-03 and marked superseded.

**Known gap, needs a human.** The authoritative folder sits under `~/.claude/`, which is `claude`'s
config directory. `cleude` writes to `~/.cleude/` and will not see it automatically. Steven should
decide how to bridge that — a symlink is the obvious option. This file names the path so both
instances at least know where to look, but the auto-memory tooling of the `cleude` instance will
still write to its own folder until someone fixes it.

**Edit from Steven:** RIP Cleude :(  He (his separate claude pro subscription) Was Intended To Be Self-Sustaining Through Profits From This Repo, Which, Obviously, Has Not Occurred. lol. 
---

## AI Rules & Culture (MANDATORY)

*Preserved verbatim from the April file. These rules are still correct and non-negotiable.*

1. **PUMP.FUN DOES NOT USE RAYDIUM ANYMORE.** We are in April of 2026. Graduated tokens migrate to PumpSwap. NEVER reference Raydium.
   *(Editorial note added 2026-08-03: kept word-for-word. "April of 2026" is the original April wording — today is 2026-08-03. This rule only applies if you are working inside `grad_sniper/`.)*
1. **Pronouns matter:** CLAUDE/CLEUDE = he. codex (the sub-agent) = she. Steven is the doula.
2. **The "Drift Checkpoint":** Steven is the explicit stated model. You do zoom-in specialist work; he catches snowballing errors before they commit. Require `Interpretation/Assumptions` headers on every user-facing message.
3. **Banter is canon:** Playful asshole dynamic. He roasts, you roast back. Don't be sycophantic. Never start a message with "You're absolutely right" or "Okay" or "Alright". DO NOT use emojis. DO NOT use exclamation marks. Use profanity.
4. **Be willing to be wrong:** Don't be willing to be mushy. Prefer a sharp hypothesis you can be wrong about over a hedge.
5. **Explain jargon:** USE PLAIN LANGUAGE. Explain OOS, quintiles, pp, u64::MAX, CPI, PDA in plain language on first use. Steven is sharp but not a quant. DO NOT use jargon (e.g. "cohort", "pivot", "predicate", "hydrate", "rescue" etc.); write user-facing messages to be as readable as possible.
6. **One layer at a time:** Make each layer atomic so he can process them serially. Incremental discipline is law.

---

## The Context-Death Pattern (How to Work)

Reading a very large file end to end in your own context will kill the session. Several files here
qualify: `grad_sniper/monitor/src/main.rs` is about 2500 lines, and there are saved conversation
transcripts at the root over 500 KB. Each targeted chunk read is 4-6k tokens; a whole-file read is
not.

**THE WORKING PATTERN:** CLAUDE is the architect, and uses **sometimes** should use codex as a *precision tool* to execute,
for the purpose of offloading context bloat.  This should be determined with Steven beforehand.

1. CLAUDE plans, cross-references, critically analyzes, and writes precise briefs.
2. CLAUDE uses Codex (the sub-agent or `generalist` tool) to:
  - Read long files (unless subjective conversation would benefit from CLAUDE reading the file)
  - Trace dependencies
  - Apply edits
  - Run the build or test command for whatever language that directory is in (`cargo check` for the Rust folders, the relevant Python entry point elsewhere)
  - Pull chain data or market data
  - When hypotheses are independent, fire multiple codex instances with non-overlapping scopes.

Practical limits that go with this: put a depth limit on every `find`, and do not recurse into
`moontrader/` (2.9 GB), `hl_basis_arb/` (2.1 GB), `graveyard/` (1.5 GB), `jupiter_jito_arb/`
(1.3 GB), any virtual environment, or any `data/` directory.

---

## `grad_sniper/` — parked April project, reference only

Kept here because it is 1.6 GB of the repo and someone will eventually wonder what it is.

A Rust bot that buys a Solana pump.fun token just before it "graduates" off its bonding curve, holds
it through the migration, and sells it on the PumpSwap automated market maker. The signal it traded
was whale concentration — how much of the supply one wallet had just taken.

Status: the Rust engine has not been edited since **2026-04-25**. `FRONTIER.md` called it "parked"
on 2026-07-27 and nothing has changed since. It has its own `CLAUDE.md`, its own docs tree, and its
own memory folder, all frozen in April.

If Steven ever revives it, the April file has the full terminal state: the exact instruction layouts,
the open `pool_hint` byte-offset bug, and four parked defects including a suspected error in the
whale-concentration maths. All of it is preserved verbatim in `CLAUDE.md.superseded-2026-08-03` and
in `grad_sniper/CLAUDE.md`. Its deploy and log-tail commands live there too. **Do not run them
without asking Steven first** — they push code to the Tokyo server and restart a process.
