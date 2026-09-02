# bStocks LP Assistant

[English](README.md) | [中文](README.zh-CN.md)

A volatility-aware LP advisor for Binance Web3 **bStocks** pools — built
entirely on the **Binance MCP / Agent OS framework**: Binance's Web3
market-data APIs plus the Agentic Wallet (`baw`) MCP/CLI surface, no other
data source or execution path.

Scoped to bStocks specifically (RWA list `type=3`, symbols suffixed `...B`,
e.g. `TSLAB`, `NVDAB`) — not Ondo (`...on`) or xStocks (`...x`), which are
separate tokenized-stock providers on the same underlying tickers.
`fetch_stock_tokens()` defaults to bStock-only, and every command inherits
that scope by default (`stocks --type` can browse other providers
explicitly if asked).

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
kline history.

**Volatility estimator**: for stablecoin-quoted pools, `σ` now comes from
the **Yang-Zhang estimator** (Yang & Zhang, 2000, *"Drift-Independent
Volatility Estimation Based on High, Low, Open, and Close Prices"*, Journal
of Business) — it uses all four OHLC prices our kline data already carries
instead of close-only, is drift-independent (unbiased under a trending
price), and is ~5-14x more statistically efficient than close-to-close at
the same sample size (per the range-based-estimator literature — see also
[Parkinson 1980](https://www.jstor.org/stable/2352357) and
[Garman-Klass 1980](https://doi.org/10.1086/296083), which Yang-Zhang
builds on). It falls back to plain close-to-close (`annualized_volatility`)
when the OHLC data looks degenerate. Non-stablecoin pairs still use
close-to-close on the price *ratio* — there's no standard OHLC estimator
for a ratio of two assets in the literature, a documented scope limit, not
an oversight.

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

`vol_ratio` — badged the **Richness Score** and bucketed **Rich**
(`<0.5`) / **Fair** (`0.5–1.0`) / **Cheap** (`>=1.0`) — is
**range-independent**: concentration multiplies fee income and IL by the
same factor, so it cancels out of the breakeven equation. It's a property
of the pool's own APY vs. the token's volatility, not of which range you'd
choose to hold it in — a much more apples-to-apples comparison across pools
with wildly different APY/vol magnitudes than a raw APY ranking, or even
the plain `model_net_apy` figure alone.

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
the highest `model_net_apy` among those with **≥60% chance of being active over a
year** — that 60% floor is the "safety" side of "safe and high APY"; a
narrower range can show a higher `model_net_apy` number but at a `p_active` so low
it's misleading to call it "safe". Both numbers are always shown together,
never the APY alone. `range` also prints a **scenario check** on the
recommended range — the same `[Pa, Pb]` re-priced at 1x/1.5x/2x the
estimated `σ` (Neutral/Elevated/Stress) — since `σ` is itself a
backward-looking estimate; this shows how much the recommendation actually
depends on that estimate being right, rather than presenting one number as
if it were certain.

### Not every pool is quoted against a stablecoin

`NVDAB-BNB`, `BNB-SPCXB`, `HOODB-BNB`, and others pair a bStock against BNB,
not a stablecoin. **This was a real bug, not a documented simplification**:
earlier versions computed IL from the bStock's own volatility alone for
every pool, silently treating the quote asset as flat even when it wasn't —
understating risk for every non-stablecoin pair. `resolve_pool_stock_and_quote()`
now classifies each pool's pair from its on-chain `assetTokenList`, and for a
non-stablecoin quote, `relative_annualized_volatility()` computes the
volatility of `log(P_stock / P_quote)` from time-aligned klines of *both*
assets — IL for such a pool depends on the relative move between the two
pooled assets, not either one alone. Every result carries a `pair_mode`
(`"stablecoin"` / `"non_stablecoin"`); `scan`/`range` label non-stablecoin
results explicitly (`[non-stablecoin pair]`) rather than let them look the
same as a stablecoin-quoted result.

## No candidate has to mean NO_TRADE

Passing the pre-deposit screen (below) answers "is this pool safe and
plausible to consider" — it doesn't answer "is it actually worth doing."
`recommend` now gates its "Top pick" behind `passes_trade_gate()`: `model_net_apy`
must be positive **and** `vol_ratio < 1` (not graded Cheap). Before this,
`recommend` would print a "Top pick" even when every candidate netted
negative after IL or graded Cheap — which reads as an endorsement it never
meant to make. When nothing clears both bars, `recommend` prints an explicit
`NO_TRADE` verdict with the specific reason(s), and shows the closest
candidate for reference only, clearly labeled as not a recommendation.

## Pre-deposit risk & plausibility screen

V3/V4 pools can report wildly unreliable `apy` figures, and no single field
check catches every way that happens — so every pool goes through
`pool_risk_flags()`, a set of *independent* signals for two questions:
**is the yield real**, and **is the pool safe**. A pool tripping any signal
is excluded from ranking, with the reason reported, not silently dropped.

| Signal | Question | What it catches |
|---|---|---|
| `feeRate` > 5%/swap | real yield? | a dynamic/keeper-priced fee read that a static-rate annualization can't handle |
| TVL < $5,000 | real yield? | statistically noisy apy from too little liquidity |
| apy > 5x peer median, same ticker | real yield? | **the general case** — any mechanism, known or not |
| `investable = false` | safe? | delisted, no new deposits possible |
| protocol `securityScore` < 50 | safe? | obviously disreputable protocols (weak floor, see below) |
| protocol name contains "V4" | safe? | **hard-blocked by default** — see "V4 pools are hard-blocked" below |

**Distinct from `flagged`: `unscoreable`.** Some pools are never evaluated
at all — an `investment-info` fetch failed, `assetTokenList` didn't confirm
a bStock, or there wasn't enough overlapping kline history. `scan` and
`recommend` report these in their own list, separate from `flagged` — "we
couldn't evaluate this" and "we evaluated it and it's unsafe/implausible"
are different claims, and collapsing them into one silent drop (which is
what earlier versions did) makes it impossible to tell "this pool is risky"
from "we simply have no data on it."

### Uniswap V4 pools are hard-blocked by default

V4 pools can carry an arbitrary custom hook — logic outside the audited
core AMM. This tool has no API access to a pool's hook address,
permissions, or audit status, and the protocol-level `securityScore` can't
see it either (V3 and V4 score identically — see the limitation note
below). Rather than leave this as a caveat, every V4 pool is excluded from
ranking by default, unconditionally — not only ones with an already-visible
symptom like an extreme `feeRate`. Pass `--allow-v4` to override for an
explicit ask or an already-vetted pool; the deeper fix (an actual hook
audit, once that data is available) is still tracked in Roadmap.

The case that motivated this: a Uniswap V4 QQQB-USDC pool showed
`apy=1,658.77%` against 77.86% on the equivalent V3 pool for the same pair
— traced to a `feeRate` of `8.38861` (838.86% per swap, not a valid fee
tier). A `feeRate` check alone only catches *that* mechanism; the
peer-outlier check (this pool's apy was 21x its own ticker's peer median)
independently flagged the same pool without needing to know the cause in
advance — that's the generalization this section is about.

**Not necessarily malicious.** [Fables](https://www.fables.fi) — a live
hook-native ve(3,3) DEX on Uniswap v4, also trading tokenized stocks — runs
"intelligent fees": hooks that reprice the swap fee per-transaction from
realized volatility, calendar/session state, or order-flow direction,
bounded and keeper-driven; their own docs describe a displayed fee as "the
latest contract read, not a quote that can bind a later transaction." A
`feeRate` snapshot from a pool like that is a live, momentary number — our
static-rate annualization is structurally the wrong tool for it, whether or
not the hook is legitimate. The flag means "we can't compute a meaningful
apy for this," not an accusation.

**Limitation worth stating plainly**: `securityScore` comes from
`defi protocol-info` and is per-*protocol*, not per-pool or per-hook —
Uniswap V3 and V4 both score 95.18 because it's the same organization, so
this signal gave zero warning on the QQQB case. It's a floor against
disreputable protocols, not a hook audit — see Roadmap for the deeper
V4-hook-safety item this doesn't replace.

## Caveats (read before trusting the numbers)

- **`model_net_apy` is a model estimate, not a promised or historical
  return** — it's the platform's `apy` minus this tool's modeled IL cost,
  named `model_net_apy` (not `net_apy`) precisely so it doesn't read as a
  return you're guaranteed to realize. Separately: the platform's `apy`
  figure itself is a single blended number. Checked directly against
  `defi investment-info`'s response on every bStock pool sampled — there is
  no fee-vs-incentive breakdown, no as-of timestamp, and no
  lockup/redemption/incentive-expiry field anywhere in that API. That's a
  confirmed data-availability limit, not an oversight: an incentive-heavy
  `apy` can look attractive right up until the incentive program ends, with
  nothing in this tool able to see it coming. Every `scan`/`recommend`
  output prints this caveat (`model_apy_caveat` in `--json`).
- **Historical vol is backward-looking.** Stock tokens can gap hard around
  earnings, dividends, splits, and trading halts — check
  `binance-tokenized-securities-info`'s asset-market-status API for upcoming
  corporate actions on any pool you're about to enter. A `vol_ratio` from a
  short kline history is a noisy estimate, not a precise number.
- **The straddle no-exit probability is now the exact closed-form value**
  (a method-of-images reflection series, not an approximation), validated
  against direct Monte Carlo path simulation in the test suite. It replaced
  a looser union-bound approximation that used to read `0%` for narrow
  ranges even when the true probability was a small positive number.
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

*(chart: p_active on the x-axis, model_net_apy on the y-axis, one point per
candidate range, the recommended ±50% range highlighted — see the skill in
action inside a Claude session for the rendered version)*

## Usage

```bash
# --- single entry point: one verdict (needs a signed-in `baw` session) ---
python riskscreen.py recommend [--capital 10000]

# --- market data (public API, no auth) ---
python riskscreen.py stocks --limit 20 --type 1
python riskscreen.py vol --ticker TSLA --days 30 --apy 0.30

# --- recommendation (needs a signed-in `baw` session) ---
python riskscreen.py scan --top 15 [--with-range] [--json] [--capital 10000] [--allow-v4]
  [--max-pages 3] [--max-fee-rate 0.05] [--min-tvl 5000] [--peer-outlier-multiple 5]
python riskscreen.py range --investmentId <id> [--side straddle|sell|buy] [--allow-v4]
  [--target-offset 0.15] [--band-width 0.10] [--capital 10000]
python riskscreen.py range --ticker TSLA --apy 0.30 --side sell   # or without a live pool

# --- portfolio + rebalance (needs a signed-in `baw` session) ---
python riskscreen.py positions [--refresh] [--json]
python riskscreen.py rebalance-check [--json] [--max-pages 3] [--allow-v4]   # --json for scheduled monitoring
```

`recommend`, `scan`, `range --investmentId`, `positions`, and
`rebalance-check` shell out to `baw` (`defi investment-list` /
`investment-info` / `defi position` / `defi protocol-info`), so they require
an active Agentic Wallet session (`baw auth signin` / `baw auth verify`).
`stocks`, `vol`, and `range --ticker/--apy` hit public Binance Web3 endpoints
directly and need no auth. `--allow-v4` overrides the default V4 hard-block
(see "Uniswap V4 pools are hard-blocked" above) — pass it only on an
explicit ask.

**`--json` is pure JSON on stdout** (on `scan`/`positions`/`rebalance-check`)
— all progress/diagnostic output goes to stderr, and every payload carries
an `as_of` UTC timestamp plus (on `scan`) `elapsed_seconds`, `flagged`, and
`unscoreable`. Safe to pipe directly into `jq` or a scheduler without
stripping table text out of it first.

**Testing**: `pip install -r requirements.txt && pytest test_riskscreen.py`
runs 105 unit tests over the pure-math/pure-logic functions (no network/baw
needed) — CI (`.github/workflows/test.yml`) runs this plus `py_compile` on
every push. Live-data smoke tests for every command
are documented in "Status" below.

**Execution is intentionally out of scope for this script.** `rebalance-check`
prints a report and moves nothing. To act on any recommendation, run
`binance-agentic-wallet`'s `defi preview` → confirm with the user → `defi
deposit` / `defi lp-add` / `defi redeem` / `defi lp-remove`, using the
`investmentId` / token addresses this tool printed. See `SKILL.md` →
"Executing a recommendation" for the exact flow an agent should follow.

## Performance

Every `baw` call is a separate Node.js process spawn (~0.6s of pure
startup overhead, before any actual API latency). `scan`/`recommend` need
one such call per candidate pool, so run sequentially that's roughly
`0.6s × pool count` — noticeable once pagination (below) widens the
candidate set. `run_scan`'s pool-info and kline fetches now run
concurrently (`concurrent.futures.ThreadPoolExecutor`, capped at
`MAX_CONCURRENT_BAW_CALLS = 8`) instead of one at a time. Measured on the
same machine: `scan --top 5` (default 3-page sweep, ~39 candidate pools)
**43.7s → 10.1s**; `recommend` (1-page default) **down to ~7s**. If a run
still feels slow, `--max-pages` is the main lever — each extra page adds
~100 more candidate pools to fetch.

## Status

Validated end-to-end against a live `baw` session. Live output
(`python riskscreen.py scan --top 8 --with-range`, BSC, 2026-09-02):

```
pool                ticker        apy      vol  grade best +/-%  range-net  confidence           tvl
GMEB-USDT           GME       426.06%   23.81%   Rich       50%    913.40%        High        19,848
AAPLB-USDT          AAPL      364.19%   32.57%   Rich       50%    646.12%    Moderate        54,040
GMEB-USDT           GME       256.48%   23.81%   Rich       50%    549.18%        High       136,619
AAPLB-USDT          AAPL      219.90%   32.57%   Rich       50%    388.89%    Moderate       441,293
NVDAB-BNB           NVDA      125.82%   39.59%   Rich       50%    178.25%    Moderate        42,924
BNB-SPCXB           SPCX      125.87%   82.78%   Rich      full    117.30%        High     1,303,516
HOODB-BNB           HOOD      123.21%   71.84%   Rich      full    116.76%        High        17,299
NVDAB-USDT          NVDA      104.17%   39.59%   Rich       50%    146.78%    Moderate        67,397

1 pool(s) excluded from ranking -- anomalous feeRate:
  QQQB-USDC (Uniswap V4): feeRate=838.86% per swap outside a sane range, and apy is
  21.3x the peer median -- see "Pre-deposit risk & plausibility screen"
```

All eight ranked pools grade **Rich** — the headline APYs genuinely reflect
a large premium over realized volatility, not just large-looking raw
numbers. The excluded ninth (QQQB-USDC, Uniswap V4) shows the sanity filter
working, not the ranking failing to find it.

Range sweep for NVDAB-USDT (`range --investmentId 9c97dee1...d405de7ec7f79d`):
Richness Score **Rich** (`vol_ratio` 0.18). Full-range nets 59.18% APY at
**High** confidence; the recommended ±50% range trades down to **Moderate**
confidence for 84.23% net. A `--side sell` sweep on the same pool shows a
tight ±5%-above-price band at **High** confidence (>1000% net APY while
active) — the "use an LP range as a limit sell order" case from the
iteration history.

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

The volatility/IL model prices *market* risk. The pre-deposit screen (above)
now catches implausible-yield and obviously-disreputable-protocol cases, but
it's still a data-plausibility + reputation check, not a *pool contract*
security audit — a pool can pass every signal above and still be unsafe if
the contract itself is broken. Closing that gap is the near-term priority;
everything below is ordered roughly by how directly it extends what's
already built.

### Still open

- **Uniswap V4 hook safety audit.** The interim mitigation shipped (every V4
  pool is hard-blocked by default, see above) — what's still missing is the
  actual audit capability that would let the block be lifted selectively
  instead of blanket: whether a specific pool's hook is attached, its audit
  status, permissions, and any known-bad-hook list match, the same
  "don't recommend a deposit outright" caution this project already applies
  to volatility risk, extended one layer deeper to contract risk.
  `query-token-audit` covers this checking pattern for tokens already; a
  pool-level analogue is the gap. Until it exists, `--allow-v4` is a manual
  override, not a substitute.
- **Historical calibration / backtesting.** Every range/scenario number here
  is a model estimate as of "now" (`as_of` in `--json` output) — none of it
  has been checked against what actually happened afterward. A paper-trading
  or historical-replay harness (record a recommendation, revisit it N days
  later against realized pool/price data) would validate or correct the
  model's assumptions (the diffusion approximations, the full-range-baseline
  treatment of platform apy, the relative-volatility pair model) instead of
  leaving them as untested theory. Meaningfully larger scope than everything
  else on this list — flagged, not attempted, in this pass.
- **Robinhood Chain compatibility.** Confirmed concrete, not speculative:
  [Fables](https://www.fables.fi) is live on Robinhood Chain today, trading
  tokenized stocks (NVDA/USDG, TSLA/USDG, AAPL/USDG, SPY/USDG, ...) against
  a public Blockscout explorer at `robinhoodchain.blockscout.com` — Blockscout
  instances standardly expose a REST/GraphQL API, which is a plausible near-
  term path to pulling comparable market/pool data the way `fetch_stock_tokens`
  / `fetch_klines` do for Binance's bapi. If that data proves comparably
  accessible, extending `stocks`/`vol`/`scan` to include it turns this from a
  Binance-only screener into a cross-venue one — genuinely more useful for
  "which venue's LP on this same underlying is actually the better deal,"
  not just which pool within one venue.
- **Session-aware volatility.** Fables' "Calendar" fee model reprices
  differently for session/overnight/weekend/holiday state specifically
  *because* tokenized-stock trading behavior differs across those windows
  (the underlying only has real market-making during NYSE/NASDAQ hours).
  `annualized_volatility()` currently treats all klines as one homogeneous
  series; splitting realized vol into regular-hours vs. off-hours segments
  (using `binance-tokenized-securities-info`'s market-status API to label
  each candle) would likely sharpen `vol_ratio` and range recommendations
  for exactly the reason Fables built a whole fee model around it.
- **Effective execution-price model for single-sided ranges** — right now a
  `--side sell`/`buy` range reuses the IL-vs-hold formula as a cost proxy;
  the real question ("what average price do I actually sell/buy at, versus
  a plain limit order at the boundary") needs its own model, not a borrowed
  one.
- **Volatility-uncertainty-aware scoring.** `vol_ratio` still uses a point
  estimate of realized vol (now Yang-Zhang instead of close-to-close — see
  "Recently shipped" — but a point estimate regardless); a short kline
  history makes that noisy. Widening `σ` by a confidence bound (realized
  variance's sampling distribution is chi-squared, so this is derivable in
  closed form) before computing `vol_ratio` would stop a thin data window
  from reading as false precision. A full GARCH/EWMA forecasting model is a
  further step past that, with more implementation cost for a less certain
  payoff on kline histories this short.
- **Automatic corporate-action gating.** The "check for upcoming corporate
  actions" step is currently a manual reminder in `SKILL.md`; it should be a
  direct call to `binance-tokenized-securities-info`'s asset-market-status
  API that widens the effective vol estimate or flags the pool outright when
  an earnings/dividend/split date falls inside the recommendation horizon.

### Recently shipped (pre-release blockers, from a second external review)

A follow-up review of the codebase itself (not just CLI output) flagged four
issues as blockers before trusting this for real money. Fixed in the order
the review prioritized them:

- **Windows command-injection risk in `baw()` (the top-priority fix).**
  `baw()` built a single shell command string via concatenation and ran it
  with `shell=True` — `subprocess.list2cmdline` only does CRT-style argv
  quoting, not shell-metacharacter escaping, so a value flowing into an
  argument (a CLI `--investmentId` flag, or an id pulled from an API
  response) containing `&`, `|`, `^`, etc. could break out of the intended
  command. Every argument is now validated against a shell-metacharacter
  blocklist before it can reach a command line at all, `baw`'s absolute path
  is resolved via `shutil.which` instead of shell PATH search, and
  `subprocess.run` uses `shell=False` throughout. Verified live: a
  deliberate `"123 & calc.exe"` payload is now rejected with `ValueError`
  before subprocess is ever invoked; non-ASCII output decoding (Chinese
  pool/company names) stayed byte-identical to before, confirmed by diffing
  against the pre-fix code path directly — no regression there.
- **`relative_annualized_volatility` hardened against sparse/misaligned
  klines.** The non-stablecoin-pair path aligned two independently-fetched
  kline series by open-time intersection with no floor on how few or gappy
  that overlap could be — two series can intersect into something far
  sparser than either alone (different listing dates, uneven on-chain
  activity), silently understating variance while still producing a
  normal-looking number. Now requires ≥30 aligned candles *and* ≥80%
  coverage of the theoretical fully-dense span between the first and last
  aligned candle; falling short of either returns `sigma=None` with a
  specific reason, routed into the existing `unscoreable` reporting instead
  of a misleadingly precise number.
- **Two-asset pool assumption validated in `resolve_pool_stock_and_quote`.**
  It picked the first bStock match and the first differing-address token as
  "the" quote asset with no check that a pool actually had exactly 2 assets
  or exactly 1 bStock — a 3-asset weighted pool or a dual-bStock pool would
  silently get treated as an ordinary 2-asset pair, which this tool's
  `E[IL] ~ σ²/8` model doesn't generalize to. Now requires exactly 2
  distinct on-chain addresses and exactly 1 confirmed bStock; anything else
  returns `pair_mode="unsupported"`, routed into `unscoreable` with a
  specific reason rather than silently mis-scored.
- **`net_apy` renamed to `model_net_apy`, plus `MODEL_APY_CAVEAT`** — see
  the Caveats section above. Checked live against `defi investment-info` on
  100 sampled pools to confirm no fee-vs-incentive breakdown, timestamp, or
  lockup/expiry data actually exists in the API before documenting that as a
  limit rather than fabricating a breakdown.

6 new tests plus several rewritten fixtures (105 total, up from 99). Every
fix was verified against the real `baw` CLI and live market data, not just
the test suite, before shipping.

### Recently shipped (from an external code review)

A read-only external review of the codebase (not just CLI behavior) found a
real correctness bug and several design gaps. Fixed, in the order the
review itself recommended:

- **Yang-Zhang OHLC volatility estimator**, replacing close-to-close for
  stablecoin-quoted pools — drift-independent, ~5-14x more statistically
  efficient at the same sample size, using the O/H/L/C data our klines
  already carry (see "The idea" above). Researched and grounded in the
  published estimator literature, not implemented from memory alone.
- **Exact double-barrier no-exit probability**, replacing the conservative
  union-bound approximation with the closed-form reflection-series
  solution — validated against direct Monte Carlo path simulation in
  `test_riskscreen.py` before shipping (a wrong "exact" formula would be
  worse than the honest approximation it replaced).
- **Non-stablecoin pair volatility (P0, real bug, not a caveat).**
  `NVDAB-BNB`/`BNB-SPCXB`/`HOODB-BNB`-style pools were scored using the
  bStock's own volatility alone, silently treating BNB as flat. Now computed
  correctly via `resolve_pool_stock_and_quote()` + `relative_annualized_volatility()`
  — see "Not every pool is quoted against a stablecoin" above.
- **`NO_TRADE` as an explicit outcome (P0).** `recommend` no longer prints a
  "Top pick" when nothing clears `passes_trade_gate()` (positive model_net_apy,
  vol_ratio < 1) — see "No candidate has to mean NO_TRADE" above.
- **Scenario stress check on `range`'s recommended range** — Neutral/Elevated/
  Stress at 1x/1.5x/2x the estimated σ, so the recommendation's sensitivity
  to the vol estimate is visible, not implicit. (Full historical
  backtesting/calibration is explicitly out of scope for this pass — see
  Roadmap.)
- **Pure JSON `--json` contract** on `scan`/`positions`/`rebalance-check`,
  with `as_of`, `elapsed_seconds`, `flagged`, and `unscoreable` fields —
  previously `--json` printed the human table first, so a scheduler
  couldn't parse stdout directly.
- **Unified evaluation path.** `rebalance-check` now calls `run_scan()`
  internally instead of running its own separate (and previously
  inconsistent — it skipped `peer_apys`/`protocol_security_score` entirely)
  market-comparison loop. `scan` and `rebalance-check` structurally cannot
  reach different safety conclusions about the same pool now.
- **`unscoreable` reporting** — a pool that fails to fetch, can't be
  confirmed as a bStock, or has no usable kline overlap is now reported
  separately from `flagged`, not silently dropped and indistinguishable
  from "we checked it and it's fine."
- **Uniswap V4 hard-blocked by default** (`--allow-v4` to override) — see
  "Uniswap V4 pools are hard-blocked" above.
- **CLI argument validation** — negative APY, negative/degenerate offsets,
  `--max-pages <= 0` etc. now rejected at the argparse layer
  (`_positive_int`/`_nonneg_float`/`_apy_fraction`/`_offset_fraction`)
  instead of propagating into the model.
- **Repo infra**: `LICENSE` (MIT), `requirements.txt`, and a GitHub Actions
  CI workflow (`.github/workflows/test.yml`) running `py_compile` + pytest
  on every push — none of these existed before.
- **30 new unit tests** (83 total) covering the relative-volatility fix,
  the V4 block, `evaluate_pool`, `passes_trade_gate`, and the CLI validators.

### Recently shipped (from a PM/QA pass)

A full product review plus a full test pass produced these, in addition to
the crash/correctness bugfixes covered in "Status" above:

- **`recommend`** — the single-entry-point gap is closed. One command, one
  verdict: top pick, its range, a check on any held positions, no need to
  know which of `scan`/`range`/`positions` to run first.
- **`--capital <usd>`** on `recommend`/`scan`/`range` — concrete position
  sizing: expected $/yr, and a concentration warning if the deposit would
  exceed 20% of the pool's TVL (`position_sizing_note`).
- **`--target-offset`** on `range --side sell/buy` — an exact target price
  offset instead of only the preset ±5/10/20/30/50% sweep.
- **Configurable pre-deposit thresholds** (`--max-fee-rate`, `--min-tvl`,
  `--peer-outlier-multiple`, `--min-security-score` on `scan`) — the screen's
  strictness is no longer hardcoded.
- **Pagination** (`--max-pages` on `scan`, default 3) — pool discovery is no
  longer capped at the first 100 pools by apy; a lower-apy-but-cheaper-vol
  pool 150th in line is now reachable.
- **Pool-matching robustness** — the pre-filter now splits on `-/_` and
  whitespace (not just `-`) and also matches bare tickers, and a pool whose
  on-chain `assetTokenList` doesn't confirm a bStock is now skipped rather
  than trusted on the name-match guess alone (a real, if rare, mis-attribution
  bug fixed alongside the widening).
- **`rebalance-check --json`** with a `needs_attention` flag per position —
  built for the `schedule` skill: wire a recurring check that only messages
  the user when something actually changed, instead of a report regardless.
- **`test_riskscreen.py`** — 53 unit tests over every pure-math function
  (`breakeven_volatility`, `vol_richness_ratio`, `no_exit_probability`,
  `pool_risk_flags`, ...). Run with `pytest test_riskscreen.py`.
- **Parallelized `baw`/kline fetches** — pagination (above) made `scan`'s
  sequential per-pool calls the dominant cost; see "Performance" for the
  43.7s → 10.1s measurement.
