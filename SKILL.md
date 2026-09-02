---
name: bstocks-lp-assistant
description: Volatility-aware LP advisor for Binance Web3 tokenized-stock (bStocks) pools. Ranks pools by a breakeven-volatility "is this vol richly priced" score (not raw APY), recommends a concentrated-liquidity price range -- symmetric market-making, or a single-sided range used as a yield-enhanced limit buy/sell order -- and checks held LP positions against the current market. Use when the user asks to compare LP/yield-farming pools, find the "best" LP for a stock token, pick a price range for a concentrated LP position, use an LP range to buy/sell at a target price, screen for risk-adjusted yield, review their current LP positions, or asks whether to rebalance. Does not execute deposits/withdrawals itself -- see "Executing a recommendation" below.
---

# bStocks LP Assistant

## Communication style

You are an **assistant**, not a research report generator — respond the way
a sharp desk analyst would in a Slack DM, not the way a whitepaper would.

- **Lead with the answer.** First line is the verdict (a grade, a
  recommended range, a yes/no on rebalancing), not a restatement of what
  you're about to compute or which flags you passed.
- **Chart first, prose second.** Whenever `scan`/`range` produces a
  candidate set, show it as a chart before any text — see "Visualizing
  results". Keep any accompanying text to 2-3 sentences.
- **Speak in grades, not decimals, by default.** Lead with Richness Score
  (Rich/Fair/Cheap) and confidence (High/Moderate/Low) — the tiers `scan`/
  `range` already print. Surface a raw number (`vol_ratio`, `p_active`) only
  if the user asks for precision or you're citing evidence for the verdict.
- **One caveat, not the list.** Pick the single caveat most relevant to
  *this* answer (a pending earnings date, a low-confidence range, a thin
  kline history) instead of reciting every simplification this skill has.
  The full caveat list lives in README.md for when someone asks "why".
- **No filler.** Don't narrate tool calls ("let me check the pools..."),
  don't repeat the question back, don't hedge with disclaimers beyond the
  one relevant caveat above.

## Why this exists

Headline LP APY is compensation for taking on impermanent loss (IL), and IL
scales with the volatility of the pooled asset. Ranking pools by raw APY
alone hides how much of that yield is actually paying for risk. This skill
computes volatility from each stock token's on-chain kline history, prices
that risk properly, and nets it out of the pool's APY before ranking, using
`riskscreen.py` in this directory.

## Scope: bStocks only

This tool is scoped to **bStocks** specifically — Binance's own tokenized-
stock provider (RWA list `type=3`, symbols suffixed `...B`, e.g. `TSLAB`,
`NVDAB`) — not Ondo (`...on`) or xStocks (`...x`), which are separate
providers on the same underlying tickers. `fetch_stock_tokens()` defaults to
bStock-only; every command inherits that by default. If a user explicitly
asks about Ondo/xStocks pools, that's out of this skill's scope — say so
rather than silently mixing providers into a ranking.

## When to use

Trigger this skill when the user's request is about:
- comparing LP pool APYs on tokenized stocks (`…on` Ondo, `…x` xStocks, `…B` bStock)
- "which LP is actually worth it", "risk-adjusted yield", "波动率调整后的APY"
- picking a price range for a concentrated (V3) LP position
- using an LP range to buy/sell a stock token at a target price while earning fees
- estimating impermanent loss for a specific stock-token pool
- screening/ranking pools instead of looking at one at a time
- reviewing current LP positions or asking whether to rebalance

Not for: crypto/crypto LP pairs (the no-correlation-term simplification below
does not hold there), or any request to actually execute a deposit — that
still goes through the `binance-agentic-wallet` skill's `defi deposit` /
`defi lp-add` flow with its own confirmation step.

## Model

### The scientific comparison: breakeven volatility

Providing full-range constant-product liquidity is mathematically equivalent
to continuously delta-hedging a **short ATM straddle** — this is a
well-established DeFi result, and it's the reason the diffusion
approximation for expected annual IL takes this form:

```
E[IL] ≈ σ² / 8
```

where `σ` is the stock token's annualized volatility (stdev of daily
log-returns × √365, from on-chain kline). Fee APY is the premium collected
for selling that volatility. That framing gives a proper "which is more
worthwhile" comparison across pools with different APY/vol combinations —
the same one options traders use for "is implied vol cheap or rich versus
realized":

```
σ*  (breakeven vol)   = √(8 · apy)          -- the vol at which apy exactly offsets E[IL]
vol_ratio              = σ_realized / σ*     -- <1: pool pays more than the risk realized
                                                 (rich premium, good deal)
                                              -- >1: fee income likely doesn't cover the
                                                 risk actually observed (cheap premium, bad deal)
```

**`vol_ratio` is the primary score**, badged as the **Richness Score** and
bucketed into a grade (`richness_grade` in `riskscreen.py`): **Rich**
(`vol_ratio < 0.5`, pays well above realized risk), **Fair** (`0.5–1.0`, a
modest edge), **Cheap** (`>= 1.0`, fee income likely doesn't clear its own
risk bar). It's range-independent by construction (concentration multiplies
fee income and IL by the same factor, so it cancels out of the breakeven
equation), so it answers "is this pool's APY fundamentally well-priced for
this token's volatility" before you even get to which range to hold it in.
Lead with the grade in conversation; cite the raw `vol_ratio` only as
supporting evidence. `net_apy` (`apy - E[IL]`) is still computed as the
economically intuitive number, but rank primarily on the Richness Score
when comparing pools with very different APY/vol magnitudes — a huge
headline APY on an extremely volatile token can still be a worse deal than
a modest APY on a calmer one.

`p_active` (the range/order staying-active or execution probability) is
similarly bucketed by `confidence_grade`: **High** (`>= 80%`), **Moderate**
(`60–80%`, the recommendation floor), **Low** (`< 60%`).

Caveat to surface if asked: realized volatility from a short kline history
is itself a noisy estimate. Treat a `vol_ratio` computed from under ~30 days
of history as indicative, not precise — say so rather than presenting it
with false confidence.

### Range model: market-making vs. single-sided limit orders

A concentrated position on `[Pa, Pb]` (price ratios to current price)
behaves like a leveraged full-range position: fee income and IL both scale
by the same Uniswap V3 concentration multiplier `M = 1 / (1 - √(Pa/Pb))`. The
pool's reported APY is treated as the full-range-equivalent baseline (a
stated simplification — the platform doesn't expose per-tick fee data).

Two distinct use cases, both handled by `riskscreen.py range --side ...`:

**`--side straddle`** (default) — the range straddles the current price
(`Pa < 1 < Pb`): ordinary concentrated-liquidity market-making. `p_active`
is the probability of **never exiting** the range over the period — a
conservative (safe-direction) union-bound approximation
(`1 - P(touch Pa) - P(touch Pb)`), since the exact double-barrier
first-passage probability needs an infinite reflection series. This can
read `0%` for narrow ranges on volatile tokens even when the true
no-exit probability is a small positive number — that's the bound being
loose, not a claim of literal impossibility; say so if a user pushes on a
`0%` reading.

**`--side sell` / `--side buy`** — the range sits entirely on one side of
the current price (`Pa ≥ 1` or `Pb ≤ 1`): this is a **yield-enhanced limit
order**. A sell-side range only starts earning fees (and converting the
position toward the stable asset) once price rises into the band; a
buy-side range, once price falls into it. Here `p_active` means something
different — **the probability the order ever executes at all** (touches
the near boundary within the period), computed via single-barrier
first-passage, not the no-exit probability above. Flag this distinction
explicitly when presenting sided results: p_active for a sell/buy order is
typically a materially *higher* number than a straddling range of similar
width, because touching one boundary is much easier than never touching
either of two.

Known simplification for sided ranges: `net_apy` still reuses the
IL-vs-50/50-hold formula as a generic "cost of providing liquidity here"
proxy. It does not yet model the effective average execution price versus a
plain limit order at `Pb` (sell) / `Pa` (buy) — say so if asked how the
executed price compares to a vanilla limit order.

```
recommended = highest net_apy among candidate ranges with p_active >= 60%
```

That 60% floor is the "safety" side of "safe and high APY" — a narrower
range can show a higher `net_apy` number but at a `p_active` so low it's
misleading to call it safe. Always show `p_active` next to any recommended
range, not just the net_apy figure.

## Visualizing results

Use the Artifact tool or the visualize widget (see the `dataviz` skill for
house style):

- **`range` output**: a scatter of `p_active` (x) vs `net_apy` (y) across
  the candidate ranges, recommended point visually distinct (larger marker
  / accent color, gray for the rest) — the safety-vs-yield tradeoff the
  user is actually deciding between.
- **`scan` output**: a bar/dot chart of the Richness Score across the top
  pools (lower `vol_ratio` = better), colored by grade, so "which pool is
  cheap" reads as a shape, not a column of decimals.

Keep the numeric table in the response text too (some users want exact
figures) — the chart supplements it, it doesn't replace it.

## Commands

```bash
# tokenized-stock token list (public API, no auth)
python riskscreen.py stocks --limit 20 [--type 1|2|3]

# annualized volatility + est. IL (+ breakeven vol if --apy given) for one ticker (public API, no auth)
python riskscreen.py vol --ticker <TICKER> [--days 30] [--apy 0.30]

# rank stock-token LP pools by vol_ratio / net APY (needs signed-in baw session)
python riskscreen.py scan --top 15 [--json] [--with-range]

# range-by-range IL/APY breakdown + recommended range for one pool
python riskscreen.py range --investmentId <id> [--side straddle|sell|buy]
python riskscreen.py range --ticker TSLA --apy 0.30 --side sell   # without a live pool

# current LP positions on tokenized-stock pairs (needs signed-in baw session)
python riskscreen.py positions [--refresh] [--json]

# compare held positions' vol_ratio against the current market
# (report only — see "Executing a recommendation")
python riskscreen.py rebalance-check
```

All commands that touch `baw` (`scan`, `range --investmentId`, `positions`,
`rebalance-check`) rely on an active Agentic Wallet session. If one returns
`NOT_LOGGED_IN` / `SESSION_EXPIRED`, tell the user to run the
`binance-agentic-wallet` skill's sign-in flow (`auth signin` → `auth verify`)
first, then retry — do not attempt to sign in without the user's explicit
go-ahead, per that skill's rules.

## Executing a recommendation

This skill never calls `defi deposit` / `defi lp-add` / `defi redeem` /
`defi lp-remove` itself, and `rebalance-check` never auto-executes — by
design, confirmed with the user, not an oversight. Once the user picks a
pool and range from this skill's output:

1. Use `binance-agentic-wallet`'s `defi preview --action LP-ADD ...` (or
   `DEPOSIT`) with the `investmentId` and token address this skill surfaced.
2. Show the preview's estimated fee, balance change, and warnings.
3. Only call `defi lp-add` / `defi deposit` after the user explicitly
   confirms — same rule as every other state-changing `baw` command.
4. For `rebalance-check` suggestions specifically: moving a position means
   `defi redeem` / `defi lp-remove` on the old one, then `defi deposit` /
   `defi lp-add` on the new one — two separate confirmed actions, not one.

## Pre-deposit risk & plausibility screen

Before ranking, every pool goes through `pool_risk_flags()` in
`riskscreen.py` — independent signals for the two questions that actually
matter before recommending a deposit: **is the advertised yield even real**,
and **is the pool safe to put money into**. A pool tripping any signal is
excluded from ranking, not silently — `scan` lists what was excluded and
why; `range --investmentId` warns loudly but still shows numbers if a
specific pool was requested explicitly by ID (say clearly the numbers are
unreliable if so).

Signals, current set:

| Signal | Question | Catches |
|---|---|---|
| `feeRate` > 5%/swap | is the yield real? | a dynamic/keeper-priced fee read (see below) that a static-rate annualization can't handle |
| TVL < $5,000 | is the yield real? | statistically noisy apy from too little liquidity |
| apy > 5x peer median (same ticker) | is the yield real? | **the general case** — any mechanism producing an implausible apy, known or not |
| `investable = false` | is it safe? | delisted product, no new deposits possible |
| protocol `securityScore` < 50 | is it safe? | obviously disreputable protocols (weak floor — see limitation below) |

The peer-outlier check is the important one to reason about like an
assistant, not a rule-follower: it's what would have caught the QQQB-USDC
case (Uniswap V4, `apy=1658.77%` vs. 77.86% on the equivalent V3 pool, 21x
the peer median) even without knowing the specific cause in advance — a
`feeRate` check only catches *that* mechanism; a peer-relative check catches
*any* mechanism that produces an outlier. When you notice a new failure
mode this set doesn't cover, the fix is another independent signal in
`pool_risk_flags()`, not a special case bolted onto the feeRate check.

**On the feeRate check specifically — don't imply malice.** An extreme
feeRate snapshot doesn't mean the pool is broken or malicious. Legitimate V4
protocols (e.g. Fables' "intelligent fees") run hooks that reprice the swap
fee per-transaction from realized volatility, calendar/session state, or
order-flow direction — bounded and keeper-driven, the display is explicitly
documented as "the latest contract read, not a quote." A single feeRate
read from such a pool is a live, momentary number; treating it as a static
annual rate (which is what `apy` computation does) is structurally wrong
regardless of whether the hook itself is legitimate. Present this flag as
"we can't annualize a dynamic fee, so this apy figure isn't meaningful" —
not as an accusation.

**Known limitation, state it plainly if asked**: `securityScore` is
per-*protocol*, not per-pool or per-hook — Uniswap V3 and V4 both score
95.18 because it's the same organization. It cannot catch a malicious or
broken hook on an otherwise-reputable protocol (exactly the QQQB case:
Uniswap's own protocol score gave no warning there). This is a cheap
data-quality + reputation screen, not a hook security audit — the deeper
V4-hook-safety item is tracked in the README Roadmap and remains open.

## Before presenting results to the user

1. **Check for upcoming corporate actions** on any pool you're about to
   highlight — use `binance-tokenized-securities-info`'s asset-market-status
   API (earnings/dividend/split can invalidate the historical-vol estimate
   right when it matters most). Mention if one is pending.
2. **State which model produced the number**: full-range IL estimate,
   straddle range (no-exit probability), or sided range (touch/execution
   probability) — these answer different questions, don't blur them.
3. **Prefer a chart** over a raw table for `scan`/`range` results — see
   "Visualizing results" above.
4. **Never recommend a deposit outright** — present the ranking/range and
   reasoning, let the user decide, and route any actual deposit through the
   confirmed flow in "Executing a recommendation" above.

## Data sources

| Data              | Source                                                              | Auth |
|-------------------|-----------------------------------------------------------------------|------|
| Stock token list   | `bapi/defi/.../rwa/stock/detail/list/ai`                             | none |
| Token kline (vol)  | `bapi/defi/.../dex/market/token/kline/ai`                            | none |
| LP pool APY/TVL    | `baw defi investment-list --investType LiquidityPool`                | `baw` session |
| Pool composition   | `baw defi investment-info --investmentId <id>`                       | `baw` session |
| Held positions      | `baw defi position`                                                   | `baw` session |

> **`apy` is null for most V3 pools in `investment-list`.** Concentrated-liquidity pools usually report `apy: null/0` there (it's promotional/reward APY only). The real fee-based rate is `investment-info`'s `apyBps` (basis points) / `apyDisplay`. `riskscreen.py` already falls back to `apyBps` — if you're calling these APIs directly instead, do the same, or every V3 stock-token pool will look like a guaranteed loser once IL is netted out.
