# bStocks LP Assistant

[English](README.md) | [中文](README.zh-CN.md)

> **Not financial advice. DYOR.** This tool's numbers come from an
> automated model, not a human review, and the code itself is built and
> maintained with AI assistance -- it can be wrong. Every command's
> output ends with this same reminder. Treat every figure here as a
> model's estimate to verify independently, never as a fact to act on
> directly -- especially before depositing real funds.

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

*Full derivation, with every formula traced to its implementing function:
[MODEL.md](MODEL.md) (GitHub renders its math natively). What follows here
is the summary.*

LP fee/incentive APY is compensation for impermanent loss (IL), and IL scales
with the volatility of the pooled asset. Stock-token pools make this easy to
model cleanly: they're paired against a stablecoin, so IL is driven almost
entirely by the stock token's own volatility — no cross-asset correlation term
needed, unlike a crypto/crypto pair.

For a constant-product AMM, the standard diffusion approximation gives:

```
E[IL] ≈ σ² / 8      (annualized, full-range / V2-style liquidity -- only valid while this is < 1.0)
```

where `σ` is the token's annualized volatility, estimated from its on-chain
kline history. This is only a small-`σ` approximation, and it diverges past
`σ = √8 ≈ 283%` annualized -- past that point the model returns **N/A**
rather than a number -- a capped value would still be false precision
once the approximation has left the regime it was derived for, confirmed
on a real pool this volatile, not a synthetic edge case. Concentrated-range
figures don't have this problem -- they're anchored to the exact boundary
IL instead, which stays valid at any volatility.

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
  the period, via the exact double-barrier reflection-series formula (see
  MODEL.md §6.2) -- a conservative union-bound approximation is kept only
  as a fallback for degenerate inputs.
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

The stablecoin address list itself lives in [`stablecoins.json`](stablecoins.json),
not hardcoded in the script — edit that file (or point the
`BSTOCKS_STABLECOIN_CONFIG` env var at a different one) to add or fix an
entry without a code change. An address absent from it is never presumed
to be a stablecoin — it just falls through to the relative-volatility path
above, which converges to the same answer for a genuine stablecoin quote
anyway (its own volatility is ~0). A missing or malformed config fails
closed to an empty set (with a warning), not a crash or a silently stale
fallback.

## Every result is one of four verdicts

Every pool this tool ever reports on ends up as exactly one of
**`ENTER`** / **`WATCH`** / **`NO_TRADE`** / **`UNSCOREABLE`** — a single
`verdict` field, consistent across `scan`/`range`/`recommend`/
`rebalance-check`, instead of reconstructing the same judgment call from
scattered grade/flags/vol_ratio fields each time:

- **`ENTER`** — cleared the trade gate: positive `model_net_apy` **and**
  `vol_ratio < 1` (not graded Cheap). This is the bar `recommend`'s "Top
  pick" has to clear.
- **`WATCH`** — passed the pre-deposit safety screen (a legitimate pool)
  but doesn't clear the trade gate right now — negative net APY, or graded
  Cheap. Not a warning, just not attractive *today*; worth watching in
  case the apy/vol picture shifts, not avoiding.
- **`NO_TRADE`** — failed the pre-deposit safety/plausibility screen (see
  below), or (for `recommend` specifically) nothing at all cleared the
  trade gate. Before this gate existed, `recommend` would print a "Top
  pick" even when every candidate netted negative after IL or graded
  Cheap — which reads as an endorsement it never meant to make. When
  nothing clears the bar, `recommend` prints an explicit `NO_TRADE`
  verdict with the specific reason(s), shows the closest candidate for
  reference only (clearly labeled as not a recommendation), and — new —
  surfaces how many pools are sitting at `WATCH` instead of just going
  silent on them.
- **`UNSCOREABLE`** — never evaluated at all (a fetch failed, no confirmed
  bStock, insufficient kline data). Distinct from `NO_TRADE` on purpose:
  "we don't know" and "we checked and it's not worth it" are different
  claims, and collapsing them (which earlier versions did) makes it
  impossible to tell "this pool is risky" from "we simply have no data on
  it." `recommend` also refuses to give *any* verdict when too much of
  the market is `UNSCOREABLE` — see "Reliability" below.

## rebalance-check: a concrete alternative, not just a grade

Comparing a held pool's grade against "the best grade on the market" told
you *that* something better might exist, not *what* it was or whether
moving was actually worth it. For each held position, `rebalance-check` now
names the actual best alternative pool (`best_alternative`: protocol, TVL,
apy, `model_net_apy`, `vol_ratio`) and computes a concrete switching
verdict (`switching`): the position's own USD value (from `defi position`)
times the `model_net_apy` gap gives an annual dollar gap, weighed against
`ASSUMED_SWITCH_COST_USD` (a documented ~$2 BSC gas ballpark for a
remove-liquidity + add-liquidity round trip — a stated assumption, not a
measured cost, and **not the full switching cost**: it excludes current
live gas price and any impermanent loss realized at the moment of exit,
since this tool has no entry-price/cost-basis data to compute that). A
"switch" verdict only fires when the estimated payback period clears
`SWITCH_PAYBACK_DAYS_WORTHWHILE` (30 days); otherwise `rebalance-check`
explicitly says **stay put**, with the gap and payback estimate shown so
that's a reasoned "not worth it," not silence. `needs_attention` now keys
off this verdict directly instead of a bare vol_ratio-multiple heuristic
(kept only as a fallback for when a held position's own apy couldn't be
evaluated at all, e.g. it's flagged).

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
| apy > 5x peer median, ≥3 same-ticker peers | real yield? | **the general case** — any mechanism, known or not |
| `investable = false` | safe? | delisted, no new deposits possible |
| protocol `securityScore` < 50 | safe? | obviously disreputable protocols (weak floor, see below) |
| protocol is V4-generation (`defiProtocolId`) | safe? | **hard-blocked by default** — see "V4 pools are hard-blocked" below |

**The peer-median check needs enough peers to mean anything.** `statistics.median`
of a 1-element list is just that element, so with a single same-ticker peer
"N times the median" is really just "N times one other pool's apy" — you
can't tell which of the two is actually the odd one out, and that lone peer
can itself be noisy. Confirmed live, not hypothetical: a GMEB-USDT
(PancakeSwap V3) pool was flagged as "6.0x the median" against its only
peer (a Uniswap V4 GMEB-USDT pool) — checked directly minutes later, that
peer's own apy had moved from 84.7% to 123.20% within the same session,
while the flagged pool's `feeRate` (0.25%, a standard PancakeSwap tier),
TVL ($245K), and lack of any reward-token incentive showed no actual
defect. The check now only applies once at least
`MIN_PEER_SAMPLE_SIZE = 3` other pools exist on the same ticker
(`--min-peer-sample` on `scan`) — a real multi-pool outlier (the QQQB-USDC
case this check was built for) stays caught either way, since it also has
enough same-ticker peers and an independently broken `feeRate`.

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
symptom like an extreme `feeRate`. Pass `--allow-v4 "REASON"` to override
for an explicit ask or an already-vetted pool — it takes a reason, not a
bare flag, recorded verbatim as `v4_override_reason` in `--json` output, so
overriding a block that exists specifically because this tool can't see
hook risk leaves a record of *why* someone decided to do it anyway. The
deeper fix (an actual hook audit, once that data is available) is still
tracked in Roadmap.

The case that motivated this: a Uniswap V4 QQQB-USDC pool showed
`apy=1,658.77%` against 77.86% on the equivalent V3 pool for the same pair
— traced to a `feeRate` of `8.38861` (838.86% per swap, not a valid fee
tier). A `feeRate` check alone only catches *that* mechanism; the
peer-outlier check (this pool's apy was 21x its own ticker's peer median)
independently flagged the same pool without needing to know the cause in
advance — that's the generalization this section is about.

**Not necessarily malicious.** [Fables](https://www.fables.fi) — a live
hook-native ve(3,3) DEX on Uniswap v4, also trading tokenized stocks — runs
"intelligent fees": hooks that reprice the swap fee per-transaction under one
of three named models (Calendar — session/overnight/weekend/holiday-aware;
Flat base; Directional — responds to order-flow direction). Its own docs
(fables.fi/docs/swap-fee) confirm even keeper-driven overrides are bounded
on-chain — capped at a 50% discount off the model rate, a 72-hour max
time-to-live, and an immutable absolute fee ceiling in the contract bytecode
— and describe a displayed fee as "the latest contract read, not a quote
that can bind a later transaction." A `feeRate` snapshot from a pool like
that is a live, momentary number — our static-rate annualization is
structurally the wrong tool for it, whether or not the hook is legitimate.
The flag means "we can't compute a meaningful apy for this," not an
accusation.

**Limitation worth stating plainly**: `securityScore` comes from
`defi protocol-info` and is per-*protocol*, not per-pool or per-hook —
Uniswap V3 and V4 both score 95.18 because it's the same organization, so
this signal gave zero warning on the QQQB case. It's a floor against
disreputable protocols, not a hook audit — see Roadmap for the deeper
V4-hook-safety item this doesn't replace. Worth noting the block isn't
excess caution over a hypothetical: Fables' own security page
(fables.fi/docs/security) states plainly that no Fables-specific audit
report is currently published, for a protocol sophisticated enough to ship
three bounded fee models — being a real, live V4 hook protocol was never
going to be proof that a given hook is safe to trust blindly.

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
  output prints this caveat (`model_apy_caveat` in `--json`). Independent
  corroboration: [Fables](https://www.fables.fi/docs/methodology), an
  unrelated live Uniswap-v4-hook exchange, documents annualizing its own
  headline pool APR the identical way (24h fee window over current TVL) and
  its own docs warn that figure isn't a forecast and doesn't net out
  divergence from holding — exactly why `model_net_apy` subtracts modeled IL
  from the raw platform `apy` instead of presenting it as-is.
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
scatter for `range`; a vol_ratio-vs-apy bubble scatter, sized by TVL and
colored by grade, for `scan`), not just a raw table — see `SKILL.md` →
"Visualizing results". Example, from live NVDAB-USDT data:

*(chart: p_active on the x-axis, model_net_apy on the y-axis, one point per
candidate range, the recommended ±50% range highlighted — see the skill in
action inside a Claude session for the rendered version)*

## Code layout

`riskscreen.py` at the repo root is a 5-line launcher (`from bstocks_lp.cli
import main`); the implementation lives in `bstocks_lp/`, layered so a new
feature has an obvious place to go:

| Module | Responsibility |
|---|---|
| `config.py` | Constants, the JSON output envelope, stablecoin-address config loading |
| `api.py` | Low-level I/O: the HTTP client (`_get`) and the `baw` CLI subprocess wrapper |
| `market_data.py` | Fetching/shaping market data -- stock tokens, klines, LP pool listings, positions |
| `volatility.py` | Realized-volatility estimators (Yang-Zhang OHLC, close-to-close, relative) |
| `il_model.py` | Breakeven volatility, the Richness Score (`vol_ratio`), diffusion-approximation IL |
| `range_model.py` | Concentrated-range math: concentration multiplier, stay-in-range probability, `recommend_range` |
| `risk_screen.py` | The pre-deposit risk & plausibility screen, independent of the yield model |
| `scan.py` | `run_scan` -- the shared evaluation pipeline -- and the ENTER/WATCH/NO_TRADE/UNSCOREABLE verdict |
| `cli.py` | The seven CLI commands, argument parsing, `rebalance-check`'s presentation helpers |

Dependency direction is one-way (`config -> api -> market_data ->
volatility -> {il_model, range_model} -> risk_screen -> scan -> cli`), and
every cross-module call goes through the qualified module (`market_data.
fetch_stock_tokens(...)`, never `from bstocks_lp.market_data import
fetch_stock_tokens`) -- that convention is what keeps every function
mockable by `monkeypatch.setattr(<module>, "<name>", fake)` in
`test_riskscreen_integration.py`, not just the ones already mocked today.
See MODEL.md for the model itself; this table is only the code map.

## Usage

```bash
# --- single entry point: one verdict (needs a signed-in `baw` session) ---
python riskscreen.py recommend [--capital 10000]

# --- market data (public API, no auth) ---
python riskscreen.py stocks --limit 20 --type 1
python riskscreen.py vol --ticker TSLA --days 30 --apy 0.30

# --- recommendation (needs a signed-in `baw` session) ---
python riskscreen.py scan --top 15 [--with-range] [--json] [--capital 10000] [--allow-v4 "REASON"]
  [--max-pages 3] [--max-fee-rate 0.05] [--min-tvl 5000] [--peer-outlier-multiple 5]
  [--min-peer-sample 3] [--min-security-score 50]
python riskscreen.py range --investmentId <id> [--side straddle|sell|buy] [--allow-v4 "REASON"]
  [--target-offset 0.15] [--band-width 0.10] [--capital 10000]
python riskscreen.py range --ticker TSLA --apy 0.30 --side sell   # or without a live pool

# --- portfolio + rebalance (needs a signed-in `baw` session) ---
python riskscreen.py positions [--refresh] [--json]
python riskscreen.py rebalance-check [--json] [--max-pages 3] [--allow-v4 "REASON"]   # --json for scheduled monitoring
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

**Testing**: `pip install -r requirements.txt && pytest test_riskscreen.py
test_riskscreen_integration.py` runs 165 tests total. `test_riskscreen.py`
(135) covers pure-math/pure-logic functions with no I/O at all.
`test_riskscreen_integration.py` (30) exercises `run_scan`/`cmd_*` end to
end — including the exact evaluation pipeline, JSON output, and CLI
argument parsing — by mocking only the two leaf I/O functions, `baw()` and
`_get()`, with fake dispatchers keyed by call signature; every
`fetch_*`/`resolve_*`/`evaluate_*` function in between runs for real. It
covers a clean pool end to end, a real fixture that locks in a live
finding (the PancakeSwap Infinity V4-detection case, so that bug can't
silently come back), non-stablecoin pairs, unsupported pool structures,
malformed/failed API responses, `baw()`'s nonzero-exit and
shell-metacharacter-rejection paths, `--json` output validity (parses as
pure JSON with nothing else on stdout) on every command, `recommend`'s
`NO_TRADE`/refuse-threshold/position-fetch-failure paths,
`rebalance-check`'s multi-investmentId and best-alternative paths, and two
property-style checks run against hundreds of randomized inputs (no
`hypothesis` dependency — plain `random`-driven loops) confirming
`no_exit_probability` never leaves `[0, 1]` and
`_switching_recommendation`'s dollar-gap sign always matches its verdict.
CI (`.github/workflows/test.yml`) runs both files plus `py_compile` on a
`ubuntu-latest` **and** `windows-latest` matrix — the Windows leg is what
actually exercises `baw()`'s Windows-specific `cmd.exe`-wrapping code path
in CI, previously untested there entirely despite being the most
security-sensitive branch in the file. A separate `lint` job installs
[`requirements-lint.txt`](requirements-lint.txt) (kept out of
`requirements.txt` on purpose — installing `ruff`/`mypy`/`pip-audit`'s
dependency trees measurably slows down `pip install -r requirements.txt`,
~19s extra on a fast connection, for tools the tests themselves never
touch) and runs `ruff` (see [`ruff.toml`](ruff.toml) for the
deliberately-scoped rule selection and why), `mypy`
(`check_untyped_defs`, see [`mypy.ini`](mypy.ini) — caught a real type
inconsistency in `_get()` during setup), and `pip-audit` against both
requirements files. Live-data smoke tests for every command are
documented in "Status" below.

**Execution is intentionally out of scope for this script.** `rebalance-check`
prints a report and moves nothing. To act on any recommendation, run
`binance-agentic-wallet`'s `defi preview` → confirm with the user → `defi
deposit` / `defi lp-add` / `defi redeem` / `defi lp-remove`, using the
`investmentId` / token addresses this tool printed. See `SKILL.md` →
"Executing a recommendation" for the exact flow an agent should follow.

## Reliability: retries, error classification, refusing a bad verdict

The public HTTP endpoints (RWA stock list, kline) go through `_get()`,
which now retries a transient failure (timeout, connection error, 5xx, 429
rate-limit) with exponential backoff and jitter before giving up, and
builds its query string with `urllib.parse.urlencode` instead of naive
string concatenation (which could corrupt the request or drop a parameter
entirely on a value containing `&`/`=`/a space). A 4xx error other than 429
isn't retried — it won't fix itself — and neither is a malformed or
wrong-shaped response body, which more likely means a real API contract
problem than a network blip; either way the eventual error names the URL,
status, and attempt count instead of a bare `urllib` traceback.

`run_scan`'s concurrent pool-info and kline fetches classify *why* an
individual fetch failed (timeout / network error / invalid data / other)
instead of collapsing every failure into the same generic "insufficient
data" message — the reason distinguishes "the kline fetch itself failed"
from "it succeeded but the data was too thin/misaligned to trust" (see
"Harden `relative_annualized_volatility`" above), so a transient batch of
timeouts doesn't read the same as individual pools genuinely lacking
history. `scan --json` includes a `failure_summary` — a frequency count of
`unscoreable` reasons — so a scheduler or a quick glance can see "what kind
of problem, how many" without reading every per-pool line.

`recommend` refuses to present a verdict at all when more than
`UNSCOREABLE_RATIO_REFUSE_THRESHOLD` (50%) of the candidate pools couldn't
even be evaluated — a `NO_TRADE` explaining that too much of the market is
unaccounted for, rather than confidently picking a "Top pick" out of
whatever scoreable sliver happened to survive a bad run (a symptom of a
systemic problem — network trouble, a `baw` session issue — not a reason
to trust the remainder as representative).

`fetch_stock_tokens()` and `fetch_klines()` are memoized in-process
(`functools.lru_cache`) — the token list and a given token's klines don't
change within the lifetime of one command, but were being refetched
regardless (`recommend` used to call `fetch_stock_tokens()` again just to
check held positions, after `run_scan()` had already fetched it
internally). No cross-invocation persistence or TTL: each CLI run is a
fresh process, so the cache is empty at the start of every run and there's
no staleness to manage. Pool status/APY/TVL (`defi investment-list` /
`investment-info`) is deliberately **not** cached even within a run — that
data changing fast is exactly what a risk screener needs to see, and
caching it would trade the one thing this tool is supposed to get right
for a speedup that matters far less than the `ThreadPoolExecutor`
concurrency fix below already delivered.

Every JSON output — `scan`/`positions`/`rebalance-check`, success or error
— now shares one envelope shape via `_json_envelope()`: `schema_version`,
`status` (`"ok"`/`"error"`), `run_id`, and `as_of` are always present,
with command-specific fields (`results`, `positions`, `error`, ...) merged
on top. Before this, an error path printed a bare `{"error": ...}` with
none of those fields while a success path had no explicit `status` at all
(only implied by the absence of `"error"`) — a real inconsistency, not a
style nit: a scheduler parsing this output needed different logic for the
error case than the success case just to tell them apart reliably.

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

Two more sequential-but-independent loops found the same way (profiling a
real run, not guessing) and fixed the same way:

- **Protocol security-score lookups** (`defi protocol-info`, one per
  *distinct* protocol among the survivors) were fetched one at a time
  inside `run_scan`'s scoring loop -- each its own `baw` subprocess spawn.
  Measured directly: 4 distinct protocols took 2.91s fetched sequentially
  vs the ~0.73s of the single slowest call once fetched concurrently
  (same `ThreadPoolExecutor` pattern as the pool-info/kline fetches).
  Paired before/after on the exact same code path
  (`run_scan(max_pages=1, with_range=True)`): **9.37s → 7.10s**.
- **Multi-page pool listing** (`fetch_lp_investments`) fetched page 2, 3,
  ... one at a time in a `for` loop -- also independent, stateless calls
  that don't need each other's result. Now page 1 alone determines
  `pools_total`, then only the pages that could actually contain data
  (`min(max_pages, ceil(pools_total / 100))`, never wastefully more) are
  fetched concurrently. Measured: 3 pages / 300 pools in 1.39s (one
  sequential page-1 call plus two pages fetched in parallel), vs. what
  was 3 fully sequential subprocess spawns before.

Both changes are pure internal reordering -- same data, same results,
verified against the full test suite plus a live end-to-end run
confirming identical output to before. If a run still feels slow,
`--max-pages` is still the main lever — each extra page adds ~100 more
candidate pools to fetch, and `MAX_CONCURRENT_BAW_CALLS` bounds how many
of any of these fetches run at once.

## Status

Validated end-to-end against a live `baw` session. Live output
(`python riskscreen.py scan --top 8 --with-range`, BSC, 2026-09-03):

```
pool                ticker  protocol              apy      vol  grade best +/-%  range-net  confidence           tvl  verdict
GMEB-USDT           GME     PancakeSwap V3    784.16%   40.46%   Rich       90%    900.37%        High       132,769  ENTER
USDT-MRNAB          MRNA    PancakeSwap V3    802.94%  158.96%   Rich      full    771.36%        High       133,819  ENTER
NVDAB-BNB           NVDA    PancakeSwap V3    385.75%   43.87%   Rich       90%    425.66%        High        63,226  ENTER  [non-stablecoin pair]
QQQB-BNB            QQQ     PancakeSwap V3    228.39%   30.08%   Rich       50%    430.19%        High        59,824  ENTER  [non-stablecoin pair]
NVDAB-USDT          NVDA    PancakeSwap V3    223.55%   50.12%   Rich       90%    227.91%    Moderate     2,064,242  ENTER
NVDAB-USDT          NVDA    PancakeSwap V3    213.27%   50.12%   Rich       90%    217.24%    Moderate       467,527  ENTER
USDT-SPCXB          SPCX    PancakeSwap V3    189.34%  100.60%   Rich      full    176.69%        High       308,724  ENTER
HOODB-BNB           HOOD    PancakeSwap V3    166.57%   69.80%   Rich      full    160.48%        High        17,838  ENTER  [non-stablecoin pair]

5 pool(s) excluded from ranking -- pre-deposit screen flagged, e.g.:
  GMEB-USDT (Uniswap V4): V4-generation pools can carry an arbitrary custom
  hook with unaudited logic -- blocked by default (--allow-v4 to override)
  BNB-SPCXB (PancakeSwap V3): apy is 5.6x the median (106.4%) of other pools
  on the same token -- outlier, treat as unverified until explained
```

All eight ranked pools grade **Rich** and clear **ENTER** — the headline
APYs genuinely reflect a large premium over realized volatility, not just
large-looking raw numbers. The `protocol` column shows every pool already
cleared the V4 hard block (see below); the five excluded pools show the
sanity filters working, not the ranking failing to find them — including
GMEB-USDT's Uniswap V4 sibling of the top-ranked PancakeSwap V3 pool above,
excluded on contract risk even though it's the same underlying stock.

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
  [Fables](https://www.fables.fi) is live on Robinhood Chain today (chain ID
  4663, per fables.fi/docs/addresses), trading tokenized stocks (NVDA/USDG,
  TSLA/USDG, AAPL/USDG, SPY/USDG, ...) against a public Blockscout explorer at
  `robinhoodchain.blockscout.com` — Blockscout instances standardly expose a
  REST/GraphQL API, which is a plausible near-term path to pulling comparable
  market/pool data the way `fetch_stock_tokens` / `fetch_klines` do for
  Binance's bapi. If that data proves comparably accessible, extending
  `stocks`/`vol`/`scan` to include it turns this from a Binance-only screener
  into a cross-venue one — genuinely more useful for "which venue's LP on this
  same underlying is actually the better deal," not just which pool within one
  venue.
- **Session-aware volatility.** Fables' documented fee models
  (fables.fi/docs/swap-fee) include a "Calendar" one that reprices
  differently for session/overnight/weekend/holiday state specifically
  *because* tokenized-stock trading behavior differs across those windows
  (the underlying only has real market-making during NYSE/NASDAQ hours) —
  one of three named models, alongside Flat base and Directional (order-flow-
  responsive). `annualized_volatility()` currently treats all klines as one
  homogeneous series; splitting realized vol into regular-hours vs. off-hours
  segments (using `binance-tokenized-securities-info`'s market-status API to
  label each candle) would likely sharpen `vol_ratio` and range
  recommendations for exactly the reason Fables built a whole fee model
  around it.
- **Effective execution-price model for single-sided ranges** — right now a
  `--side sell`/`buy` range reuses the IL-vs-hold formula as a cost proxy;
  the real question ("what average price do I actually sell/buy at, versus
  a plain limit order at the boundary") needs its own model, not a borrowed
  one.
- **Volatility-uncertainty-aware scoring.** `vol_ratio` still uses a point
  estimate of realized vol (Yang-Zhang, not just close-to-close, but a
  point estimate regardless); a short kline history makes that noisy.
  Widening `σ` by a confidence bound (realized
  variance's sampling distribution is chi-squared, so this is derivable in
  closed form) before computing `vol_ratio` would stop a thin data window
  from reading as false precision. A full GARCH/EWMA forecasting model is a
  further step past that, with more implementation cost for a less certain
  payoff on kline histories this short.
- **Automatic corporate-action gating** — the concrete first step of the
  "hard alerts" idea (corporate actions/depeg/liquidity crashes/incentive
  expiry). The "check for upcoming corporate actions" step is currently a
  manual reminder in `SKILL.md`; it should be a direct call to
  `binance-tokenized-securities-info`'s asset-market-status API that widens
  the effective vol estimate or flags the pool outright when an
  earnings/dividend/split date falls inside the recommendation horizon.
  Depeg/liquidity-crash/incentive-expiry alerts would each need their own
  signal source and are unscoped past the idea stage.
- **Decision snapshots.** Record each `recommend`/`scan` verdict (inputs,
  `verdict`, the numbers behind it) to durable local storage so it can be
  revisited later against what actually happened — "did this age well."
  Needs a schema and a persistence layer this stateless CLI script doesn't
  have yet; worth designing deliberately rather than bolting on quickly,
  and closely related to the historical-calibration item above (a snapshot
  store is most of what a paper-trading harness would need to replay
  against anyway).
