---
name: bstocks-lp-assistant
description: Volatility-aware LP advisor for Binance Web3 tokenized-stock (bStocks) pools. Ranks pools by a breakeven-volatility "is this vol richly priced" score (not raw APY) -- correctly using relative volatility for non-stablecoin pairs like bStock/BNB, not just the bStock's own vol -- recommends a concentrated-liquidity price range at an exact target price, sizes a position against pool TVL, hard-blocks Uniswap V4 pools over unknown hook safety, and can return an explicit NO_TRADE verdict when nothing clears a positive-net-APY-and-not-Cheap bar. Checks held LP positions against the current market via the same evaluation path as ranking (never a separately-drifted check), including a scheduled-monitoring mode. Use when the user asks to compare LP/yield-farming pools, find the "best" LP for a stock token, pick a price range for a concentrated LP position, use an LP range to buy/sell at a target price, screen for risk-adjusted yield, size a deposit, review their current LP positions, or asks whether to rebalance -- or asks an open-ended "what should I do with my bStocks LPs" question (use `recommend`). Does not execute deposits/withdrawals itself -- see "Executing a recommendation" below.
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
computes volatility from each stock token's on-chain kline history (Yang-Zhang
OHLC estimator for stablecoin-quoted pools, close-to-close ratio for
non-stablecoin pairs), prices that risk properly, and nets it out of the
pool's APY before ranking, using `riskscreen.py` in this directory.
`no_exit_probability` uses the exact double-barrier reflection-series
solution, not an approximation — see README "The idea" / "Recently shipped"
for both, including how the reflection formula was validated (Monte Carlo,
not just derived from memory) before shipping.

## Scope: bStocks only

This tool is scoped to **bStocks** specifically — Binance's own tokenized-
stock provider (RWA list `type=3`, symbols suffixed `...B`, e.g. `TSLAB`,
`NVDAB`) — not Ondo (`...on`) or xStocks (`...x`), which are separate
providers on the same underlying tickers. `fetch_stock_tokens()` defaults to
bStock-only; every command inherits that by default. If a user explicitly
asks about Ondo/xStocks pools, that's out of this skill's scope — say so
rather than silently mixing providers into a ranking.

## Model scope: stablecoin vs. non-stablecoin pairs

Not every bStock LP pool is quoted against a stablecoin — `NVDAB-BNB`,
`BNB-SPCXB`, `HOODB-BNB` etc. pair against BNB. IL for a non-stablecoin pair
depends on the *relative* price move between the two pooled assets, not the
bStock's volatility alone. `resolve_pool_stock_and_quote()` classifies every
pool's pair on its on-chain `assetTokenList`, and `relative_annualized_volatility()`
computes `vol(log(P_stock/P_quote))` from aligned klines for the non-stablecoin
case. Every result carries a `pair_mode` ("stablecoin"/"non_stablecoin") —
**always surface it** when presenting a non-stablecoin pool (`scan`/`range`
already print `[non-stablecoin pair]` / a label). Don't present a
stablecoin-pair result and a non-stablecoin-pair result with the same
confidence language — they're different models with different reliability,
even when the printed grade looks the same.

## Every pool is ENTER, WATCH, NO_TRADE, or UNSCOREABLE

Every pool this skill reports on carries a `verdict` field with exactly
one of these four values — use it, don't reconstruct the same judgment
from grade/flags/vol_ratio yourself:

- **`ENTER`** — cleared `passes_trade_gate()`: positive `model_net_apy`
  **and** `vol_ratio < 1` (not graded Cheap). This is what `recommend`'s
  "Top pick" is drawn from.
- **`WATCH`** — passed the pre-deposit safety screen (a legitimate pool)
  but doesn't clear the trade gate right now. **Present this as "not
  attractive today," not as a warning** — nothing is wrong with the pool,
  the numbers just don't currently clear the bar. `recommend` surfaces a
  count of these; mention it if the user asks what else exists besides
  the Top pick.
- **`NO_TRADE`** — either a specific pool failed the pre-deposit
  safety/plausibility screen (avoid it, and say why), or (for `recommend`
  specifically) nothing at all cleared the trade gate market-wide. **Treat
  a market-wide `NO_TRADE` as a legitimate, informative answer, not a
  failure to relay to the user apologetically** — it's the model doing its
  job when the market genuinely doesn't offer a clean opportunity right
  now.
- **`UNSCOREABLE`** — never evaluated at all (fetch failed, no confirmed
  bStock, insufficient kline data). **Never present this as if it were a
  safety verdict** — "we don't know" and "we checked, it's not worth it"
  are different claims. If too much of the market is `UNSCOREABLE`,
  `recommend` refuses to give any verdict at all rather than picking from
  an unrepresentative scoreable remainder — relay that refusal plainly,
  don't paper over it with whatever partial results exist.

## Uniswap V4 pools are hard-blocked by default

`pool_risk_flags(..., block_unknown_v4_hooks=True)` (the default) excludes
every V4-generation pool from ranking, unconditionally — not just ones with
an already-visible symptom like an extreme feeRate. Detection uses the
structured `defiProtocolId` (e.g. "uniswap4", "pancakeswap4"), not the
display name — PancakeSwap's own V4 is marketed as "PancakeSwap Infinity"
with no "v4" in the name anywhere, so a name-match alone would miss it.
Reason for the block itself: these pools can carry an arbitrary custom
hook, and this tool has no API access to a pool's hook address,
permissions, or audit status; the protocol-level `securityScore` signal
can't see it either (V3 and V4 score identically). `--allow-v4 "REASON"`
disables this for an explicit user override or an already-vetted pool — it
takes a reason string, not a bare flag, recorded as `v4_override_reason` in
`--json` output. Only pass it through on a clear, explicit ask ("show me V4
pools anyway"), never by default just because nothing else qualified.

## Never let scan and rebalance-check disagree

`rebalance-check` calls `run_scan()` internally for its market comparison —
the exact same evaluation path (`evaluate_pool()`, same thresholds, same V4
block) that `scan` uses, not a separate stripped-down check. If you ever see
`scan` and `rebalance-check` reach different safety conclusions about the
same pool, that's a bug to report, not a discrepancy to paper over — the
whole point of the shared path is that it can't happen by construction.

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
supporting evidence. `model_net_apy` (`apy - E[IL]`) is still computed as the
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

Known simplification for sided ranges: `model_net_apy` still reuses the
IL-vs-50/50-hold formula as a generic "cost of providing liquidity here"
proxy. It does not yet model the effective average execution price versus a
plain limit order at `Pb` (sell) / `Pa` (buy) — say so if asked how the
executed price compares to a vanilla limit order.

```
recommended = highest model_net_apy among candidate ranges with p_active >= 60%
```

That 60% floor is the "safety" side of "safe and high APY" — a narrower
range can show a higher `model_net_apy` number but at a `p_active` so low it's
misleading to call it safe. Always show `p_active` next to any recommended
range, not just the model_net_apy figure.

## Visualizing results

Use the Artifact tool or the visualize widget (see the `dataviz` skill for
house style):

- **`range` output**: a scatter of `p_active` (x) vs `model_net_apy` (y) across
  the candidate ranges, recommended point visually distinct (larger marker
  / accent color, gray for the rest) — the safety-vs-yield tradeoff the
  user is actually deciding between.
- **`scan` output**: a scatter/bubble chart, not a plain bar chart —
  `vol_ratio` on the x-axis, `model_net_apy` on the y-axis (higher =
  better, so y-up already reads correctly), bubble size = `tvl` (bigger
  bubble = more capital the pool can absorb before your deposit moves it),
  color = grade/verdict (green for Rich/`ENTER`, amber for Fair/`WATCH`,
  red for Cheap — never a single uniform color across every bubble).
  Label the x-axis "vol_ratio (lower = richer/cheaper)" explicitly, and
  add a vertical reference line at `vol_ratio = 1.0` marking the
  `ENTER`/`WATCH` boundary. This framing matters because a bare bar chart
  of raw `vol_ratio` is actively misleading, confirmed in practice (a real
  user's own chart, not a hypothetical): sorted ascending with bars scaled
  to `vol_ratio`, the *worst* pools (highest `vol_ratio`, Cheap-graded) get
  the *longest* bars — reading as "biggest = best" by default visual
  convention, when a lower `vol_ratio` is what's actually good. TVL and
  safety-tier were invisible too (buried in a text label, not encoded
  visually) — the bubble size/color above fix that same gap. If a bar/dot
  chart is truly unavoidable (e.g. a widget that can't do bubble scatter),
  sort so the best pool is first, color every bar by grade, and put the
  `vol_ratio` value directly in each bar's label — never ship a monochrome
  bar chart whose length alone implies "bigger is better" for a metric
  where lower is what's good.

Keep the numeric table in the response text too (some users want exact
figures) — the chart supplements it, it doesn't replace it.

## Commands

```bash
# single entry point -- one verdict, no need to pick which command to run (needs baw session)
python riskscreen.py recommend [--capital 10000] [--max-pages 1]

# tokenized-stock token list (public API, no auth)
python riskscreen.py stocks --limit 20 [--type 1|2|3]

# annualized volatility + est. IL (+ Richness Score if --apy given) for one ticker (public API, no auth)
python riskscreen.py vol --ticker <TICKER> [--days 30] [--apy 0.30]

# rank stock-token LP pools by vol_ratio / net APY (needs signed-in baw session)
python riskscreen.py scan --top 15 [--json] [--with-range] [--capital 10000] [--allow-v4 "REASON"]
  [--max-pages 3] [--max-fee-rate 0.05] [--min-tvl 5000]
  [--peer-outlier-multiple 5] [--min-security-score 50]

# range-by-range IL/APY breakdown + recommended range for one pool
python riskscreen.py range --investmentId <id> [--side straddle|sell|buy] [--allow-v4 "REASON"]
  [--target-offset 0.15] [--band-width 0.10] [--capital 10000]
python riskscreen.py range --ticker TSLA --apy 0.30 --side sell   # without a live pool

# current LP positions on tokenized-stock pairs (needs signed-in baw session)
python riskscreen.py positions [--refresh] [--json]

# compare held positions' vol_ratio against the current market -- same evaluation
# path as `scan` (report only — see "Executing a recommendation")
python riskscreen.py rebalance-check [--json] [--max-pages 3] [--allow-v4 "REASON"]
```

**`--json` on `scan`/`positions`/`rebalance-check` is pure JSON on stdout —
nothing else.** Progress/diagnostic messages go to stderr, so a scheduler or
pipeline consuming `--json` output never has to strip prose out of it. Every
JSON payload — success or error alike — shares one envelope: `schema_version`,
`status` (`"ok"`/`"error"`), `run_id`, and `as_of` (an ISO-8601 UTC timestamp
of when the data was pulled — surface it if a user asks "how fresh is this,"
don't just say "current") are always present, with command-specific fields
merged on top.

**`recommend` is the default answer** to an open-ended "what should I do"
question — it wraps `scan --with-range` (page 1 only, for speed) plus a
one-line check on any held positions, and prints a single verdict instead of
requiring you to pick which of the other commands to run. Reach for `scan`,
`range`, `positions`, or `rebalance-check` directly when the user's question
is already specific (a particular pool, a particular position, more pages of
coverage than `recommend`'s fast default).

**`--capital <usd>`** (on `recommend`/`scan`/`range`) turns the abstract
ranking into a concrete position-sizing check: expected $/yr at the current
rate, and — the important part — what share of the pool's TVL that deposit
would be. Above 20% share, a warning fires: at that size you're not really
diversified into the pool, you're moving its price and concentrating IL risk
on yourself. Always surface this warning if the user gives a deposit amount
and it fires; don't let a strong Richness Score grade overshadow it.

**`--target-offset`** (on `range --side sell/buy`) answers "I want to sell/buy
at roughly this price" directly, instead of making the user interpret which
of the preset ±5/10/20/30/50% rows is closest to what they meant.

**Threshold flags on `scan`** (`--max-fee-rate`, `--min-tvl`,
`--peer-outlier-multiple`, `--min-security-score`) let a user loosen or
tighten the pre-deposit screen's strictness. Defaults are reasonable; only
change them if the user explicitly asks for a stricter or looser screen —
don't silently loosen a threshold just because nothing passed the default one.

All commands that touch `baw` (`recommend`, `scan`, `range --investmentId`,
`positions`, `rebalance-check`) rely on an active Agentic Wallet session. If
one returns `NOT_LOGGED_IN` / `SESSION_EXPIRED`, tell the user to run the
`binance-agentic-wallet` skill's sign-in flow (`auth signin` → `auth verify`)
first, then retry — do not attempt to sign in without the user's explicit
go-ahead, per that skill's rules.

## Scheduled monitoring

`rebalance-check --json` emits `{"positions": [...], "any_needs_attention":
bool}` — `needs_attention` fires per position when it's itself flagged by the
pre-deposit screen, when some of it couldn't even be evaluated, or when
`switching.verdict == "switch"` (a concrete `best_alternative` pool exists
whose dollar payback period clears `SWITCH_PAYBACK_DAYS_WORTHWHILE`, see
README "rebalance-check: a concrete alternative, not just a grade" — falls
back to a bare vol_ratio-multiple check only when the held position's own
apy couldn't be evaluated). This is built for the
`schedule` skill: wire a recurring task that runs `rebalance-check --json`
and only messages the user when `any_needs_attention` is true, rather than a
daily report regardless of whether anything changed. Still report-only —
nothing here ever calls `defi redeem`/`deposit` on its own; a scheduled run
surfaces a suggestion, the user (or a follow-up confirmed request) still
drives any actual move.

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

**Distinct from this: `unscoreable`.** Some pools are never scored at all —
`investment-info` fetch failed, `assetTokenList` didn't confirm a bStock, or
there wasn't enough (overlapping) kline history to compute a volatility.
`scan`/`recommend` report these separately from `flagged` and never conflate
the two: "we couldn't evaluate this" (`unscoreable`) is a different claim
from "we evaluated it and it's unsafe/implausible" (`flagged`) — don't tell
a user a pool is "risky" when the honest answer is "we don't have data on it."

Signals, current set:

| Signal | Question | Catches |
|---|---|---|
| `feeRate` > 5%/swap | is the yield real? | a dynamic/keeper-priced fee read (see below) that a static-rate annualization can't handle |
| TVL < $5,000 | is the yield real? | statistically noisy apy from too little liquidity |
| apy > 5x peer median (same ticker) | is the yield real? | **the general case** — any mechanism producing an implausible apy, known or not |
| `investable = false` | is it safe? | delisted product, no new deposits possible |
| protocol `securityScore` < 50 | is it safe? | obviously disreputable protocols (weak floor — see limitation below) |
| protocol name contains "V4" | is it safe? | **hard block by default** — unknown hook risk, see "Uniswap V4 pools are hard-blocked" above |

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
