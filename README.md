# bStocks LP Assistant

A volatility-aware LP advisor for Binance Web3 tokenized-stock (bStocks)
pools — built entirely on the **Binance MCP / Agent OS framework**: Binance's
Web3 market-data APIs plus the Agentic Wallet (`baw`) MCP/CLI surface, no
other data source or execution path.

## Built on Binance MCP — module by module

| # | Capability | Binance Agent OS module used |
|---|---|---|
| 1 | **Pull market data** via Binance's MCP/API | Binance Web3 APIs — public RWA stock-token list + on-chain kline (no auth) |
| 2 | **Compute IL/APY across price ranges from volatility, and recommend a deposit** — how to get a *safe and high* APY | This project's model (`riskscreen.py`): breakeven-volatility scoring + range sweep (see "The idea" below) |
| 3 | **Operate deposits/withdrawals directly** via Agentic Wallet | `binance-agentic-wallet`'s confirmed `defi deposit` / `defi lp-add` / `defi redeem` / `defi lp-remove` — this project hands off to it rather than reimplementing execution |
| 4 | **Read the global investment picture** via Agentic Wallet | `baw defi position` — held LP positions, filtered to tokenized-stock pairs |

Four parts, concretely:

1. **Market data** — pulls stock-token prices/kline and LP pool APY/TVL/composition
   straight from Binance's Web3 APIs and the Agentic Wallet (`baw`) CLI.
2. **Risk-adjusted recommendation** — computes impermanent-loss cost from
   volatility, at both full-range and a swept set of concentrated-liquidity
   ranges (symmetric market-making, or single-sided limit-order-style
   ranges), scores pools by a breakeven-volatility ratio, and recommends the
   range that's actually "safe and high APY" rather than just high APY.
3. **Execution** — deliberately *not* reimplemented here. Deposits/withdrawals
   go through `binance-agentic-wallet`'s already-reviewed, confirmation-gated
   `defi deposit` / `defi lp-add` / `defi redeem` / `defi lp-remove` flow,
   using the `investmentId` and token addresses this tool surfaces.
4. **Portfolio + rebalance checks** — reads current LP positions via `baw defi
   position` and compares their vol_ratio against the live market, producing
   a report (never an auto-executed trade — see below).

**New here?** See [INSTALL.md](INSTALL.md) for how to add this to your own
Claude + Agent OS setup and use it conversationally, with no command-line
syntax to remember.

## The idea

LP fee/incentive APY is compensation for impermanent loss (IL), and IL scales
with the volatility of the pooled asset. Stock-token pools make this easy to
model cleanly: they're paired against a stablecoin, so IL is driven almost
entirely by the stock token's own volatility — no cross-asset correlation term
needed, unlike a crypto/crypto pair.

For a constant-product AMM, the standard diffusion approximation gives:

```
E[IL] ≈ σ² / 8      (annualized, full-range / V2-style liquidity)
```

where `σ` is the token's annualized volatility, estimated from its on-chain
kline history (log-return stdev, annualized by `sqrt(365)`).

### The scientific comparison: breakeven volatility

Full-range LPing is mathematically equivalent to continuously delta-hedging
a **short ATM straddle** — fee APY is the premium collected for selling
volatility. That gives a proper way to answer "under different volatility
and APY combinations, which pool is actually more worthwhile" — the same
question options traders ask about implied vol vs. realized vol:

```
σ* (breakeven vol) = √(8 · apy)         -- the vol at which apy exactly offsets E[IL]
vol_ratio           = σ_realized / σ*    -- <1: pool pays more than the realized risk (rich, good deal)
                                          -- >1: fee income likely doesn't cover the realized risk (cheap, bad deal)
```

`vol_ratio` is **range-independent** — concentration multiplies fee income
and IL by the same factor, so it cancels out of the breakeven equation. It's
a property of the pool's own APY vs. the token's volatility, not of which
range you'd choose to hold it in — a much more apples-to-apples comparison
across pools with wildly different APY/vol magnitudes than a raw APY
ranking, or even the plain `net_apy` figure alone.

### Concentrated (V3) ranges: market-making vs. limit orders

A concentrated position on `[Pa, Pb]` behaves like a leveraged full-range
position — fee income *and* IL both scale by the same Uniswap V3
concentration multiplier `M = 1 / (1 - √(Pa/Pb))`.

- **Straddling range** (`Pa < 1 < Pb`, the default): ordinary
  market-making. `p_active` = probability of never exiting the range over
  the period (a conservative union-bound approximation).
- **Single-sided range** (`Pa ≥ 1` or `Pb ≤ 1`): a **yield-enhanced limit
  order** — a sell-side range only converts toward the stable asset (and
  earns fees) once price rises into it; a buy-side range, once price falls
  into it. `p_active` here means *probability the order ever executes*,
  computed as a single-barrier touch probability — a different, and
  typically higher, number than the straddling case's no-exit probability.

The tool sweeps a set of candidate ranges/offsets per side and recommends
the highest `net_apy` among those with **≥60% chance of being active over a
year** — that 60% floor is the "safety" side of "safe and high APY"; a
narrower range can show a higher `net_apy` number but at a `p_active` so low
it's misleading to call it "safe". Both numbers are always shown together,
never the APY alone.

## Caveats (read before trusting the numbers)

- **Historical vol is backward-looking.** Stock tokens can gap hard around
  earnings, dividends, splits, and trading halts — check
  `binance-tokenized-securities-info`'s asset-market-status API for upcoming
  corporate actions on any pool you're about to enter. A `vol_ratio` from a
  short kline history is a noisy estimate, not a precise number.
- **The straddle no-exit probability is a loose, conservative bound** — it
  can read `0%` for narrow ranges even when the true probability is a small
  positive number. That's the union-bound approximation being loose, not a
  claim of literal impossibility.
- **Sided (limit-order) ranges reuse the IL-vs-hold formula as a generic
  cost proxy** — it does not yet model the effective average execution
  price versus a plain limit order at the boundary. A known simplification,
  not hidden.
- **No drift term.** All formulas assume zero expected price drift. This is
  a volatility-risk estimate, not a directional forecast.

## Visualizing results

When used through Claude, results are shown as a chart (safety-vs-yield
scatter for `range`, a vol_ratio comparison for `scan`), not just a raw
table — see `SKILL.md` → "Visualizing results". Example, from live NVDAB-USDT
data:

*(chart: p_active on the x-axis, net_apy on the y-axis, one point per
candidate range, the recommended ±50% range highlighted — see the skill in
action inside a Claude session for the rendered version)*

## Usage

```bash
# --- market data (public API, no auth) ---
python riskscreen.py stocks --limit 20 --type 1
python riskscreen.py vol --ticker TSLA --days 30 --apy 0.30

# --- recommendation (needs a signed-in `baw` session) ---
python riskscreen.py scan --top 15 [--with-range] [--json]
python riskscreen.py range --investmentId <id> [--side straddle|sell|buy]
python riskscreen.py range --ticker TSLA --apy 0.30 --side sell   # or without a live pool

# --- portfolio + rebalance (needs a signed-in `baw` session) ---
python riskscreen.py positions [--refresh] [--json]
python riskscreen.py rebalance-check
```

`scan`, `range --investmentId`, `positions`, and `rebalance-check` shell out
to `baw` (`defi investment-list` / `investment-info` / `defi position`), so
they require an active Agentic Wallet session (`baw auth signin` / `baw auth
verify`). `stocks`, `vol`, and `range --ticker/--apy` hit public Binance Web3
endpoints directly and need no auth.

**Execution is intentionally out of scope for this script.** `rebalance-check`
prints a report and moves nothing. To act on any recommendation, run
`binance-agentic-wallet`'s `defi preview` → confirm with the user → `defi
deposit` / `defi lp-add` / `defi redeem` / `defi lp-remove`, using the
`investmentId` / token addresses this tool printed. See `SKILL.md` →
"Executing a recommendation" for the exact flow an agent should follow.

## Status

Validated end-to-end against a live `baw` session. Live output
(`python riskscreen.py scan --top 8 --with-range`, BSC, 2026-09-02):

```
pool                ticker        apy      vol vol_ratio  full-net best +/-%  range-net  p_active           tvl
GMEB-USDT           GME       452.84%   23.73%      0.04   452.14%       50%    972.27%       91%        19,879
AAPLB-USDT          AAPL      382.75%   32.64%      0.06   381.42%       50%    677.91%       75%        53,012
GMEB-USDT           GME       296.20%   23.73%      0.05   295.50%       50%    635.38%       91%       137,657
AAPLB-USDT          AAPL      230.28%   32.64%      0.08   228.95%       50%    406.61%       75%       438,462
NVDAB-BNB           NVDA      123.94%   39.58%      0.13   121.98%       50%    175.56%       61%        42,844
BNB-SPCXB           SPCX      125.01%   82.79%      0.26   116.44%      full    116.44%      100%     1,295,426
HOODB-BNB           HOOD      112.87%   71.76%      0.24   106.43%      full    106.43%      100%        17,221
NVDAB-USDT          NVDA      101.83%   39.58%      0.14    99.87%       50%    143.42%       61%        67,394
```

All eight of these top pools show `vol_ratio` well under 1 (0.04–0.26) —
confirming the huge headline APYs genuinely reflect a large premium over
realized volatility, not just an artifact of reading raw APY numbers.

Range sweep for NVDAB-USDT (`range --investmentId 9c97dee1...d405de7ec7f79d`):
breakeven vol 212.57% vs. realized 39.58% (`vol_ratio` 0.19, richly priced).
Full-range nets 54.51% APY; the recommended ±50% range (61% probability of
never exiting over a year — the "safety" floor) nets 77.48%. A single-sided
`--side sell` sweep on the same pool shows a tight ±5%-above-price band with
90% probability of ever executing and a >1000% net APY while active — the
"use an LP range as a limit sell order" case point 1 in the iteration list
asked for.

## Binance Agent OS Mini Hackathon — Track A submission

Built as a [Skill](SKILL.md) — an AI agent (this repo was built and driven
end-to-end by Claude) loads it on demand, matching the "Skill Hub" piece of
Agent OS. Maps directly onto Agent OS's own pillars
([binance.com/en/agent-os](https://www.binance.com/en/agent-os)):

| Agent OS pillar        | This project                                                          |
|-------------------------|------------------------------------------------------------------------|
| **Read the market**     | Web3 APIs — public RWA stock-token list + kline, for the vol/IL model  |
| **Track your portfolio**| `baw defi position` — held LP positions, filtered to stock-token pairs |
| **Operate on-chain**    | Recommendations hand off to Agentic Wallet's `defi deposit`/`lp-add`/`redeem`/`lp-remove`, confirmed each time — not reimplemented here |
| **Skill Hub**            | Packaged as `SKILL.md` + script, same shape as Binance's own published skills (`binance-agentic-wallet`, `binance-tokenized-securities-info`) |

The pitch: turn "what's the LP APY" into "what's the LP APY *worth taking
the risk for, and at what range*" — a volatility-aware advisor that treats
stock-token LPing as the market-making (or limit-order) activity it actually
is, scores pools the way an options desk would price a straddle, recommends
a concrete range instead of a vague "APY looks good", and stays in an
advisory role — every fund movement is still a human-confirmed `baw` call.

## Roadmap

The volatility/IL model prices *market* risk. It currently has nothing to
say about *pool* risk — a pool can be volatility-cheap and still be a bad
place to put money if the contract itself is unsafe. Closing that gap is
the near-term priority; everything below is ordered roughly by how directly
it extends what's already built.

- **Uniswap V4 hook safety screening.** V4 pools can carry arbitrary custom
  hook contracts — logic outside the audited core AMM, and a real vector for
  malicious or just poorly-written pools (fee-skimming hooks, hooks that
  block withdrawals, etc.). Before this tool recommends a V4 pool it should
  check whether the pool has a hook attached and, if so, run/report a
  security read (audit status, hook permissions, known-bad-hook lists) —
  the same "don't recommend a deposit outright" caution this project already
  applies to volatility risk, extended to contract risk. `query-token-audit`
  covers this checking pattern for tokens already; a pool-level analogue is
  the gap.
- **Robinhood Chain compatibility.** Robinhood has been building a chain for
  its own tokenized-stock offering. If/when RWA stock-token LPs exist there
  with data comparably accessible to Binance's Web3 APIs, extending `stocks`
  / `vol` / `scan` to include it turns this from a Binance-only screener into
  a cross-venue one — genuinely more useful for "which venue's LP on this
  same underlying is actually the better deal", not just which pool within
  one venue.
- **Exact double-barrier probability**, replacing the current conservative
  union-bound approximation for straddling ranges (`no_exit_probability`)
  with the proper reflection-principle series — tightens the `0%` readings
  narrow ranges currently get, without changing the safe-direction bias.
- **Effective execution-price model for single-sided ranges** — right now a
  `--side sell`/`buy` range reuses the IL-vs-hold formula as a cost proxy;
  the real question ("what average price do I actually sell/buy at, versus
  a plain limit order at the boundary") needs its own model, not a borrowed
  one.
- **Volatility-uncertainty-aware scoring.** `vol_ratio` currently uses a
  point estimate of realized vol; a short kline history makes that noisy.
  Widening `σ` by a confidence bound (or moving to a GARCH-style estimator)
  before computing `vol_ratio` would stop a thin data window from reading as
  false precision.
- **Automatic corporate-action gating.** The "check for upcoming corporate
  actions" step is currently a manual reminder in `SKILL.md`; it should be a
  direct call to `binance-tokenized-securities-info`'s asset-market-status
  API that widens the effective vol estimate or flags the pool outright when
  an earnings/dividend/split date falls inside the recommendation horizon.
- **Scheduled rebalance monitoring.** `rebalance-check` is pull-only today.
  Wiring it to a scheduled task (daily, say) that surfaces a notification
  when a held position's `vol_ratio` or `p_active` drifts past a threshold
  — still report-only, still routed through the confirmed `baw` execution
  flow — turns "check when I remember to ask" into "get told when it
  matters."
