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

*Full derivation, with every formula traced to its implementing function:
[MODEL.md](MODEL.md) (or [MODEL.pdf](MODEL.pdf) for a typeset copy). What
follows here is the summary.*

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
| apy > 5x peer median, same ticker | real yield? | **the general case** — any mechanism, known or not |
| `investable = false` | safe? | delisted, no new deposits possible |
| protocol `securityScore` < 50 | safe? | obviously disreputable protocols (weak floor, see below) |
| protocol is V4-generation (`defiProtocolId`) | safe? | **hard-blocked by default** — see "V4 pools are hard-blocked" below |

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
test_riskscreen_integration.py` runs 160 tests total. `test_riskscreen.py`
(132) covers pure-math/pure-logic functions with no I/O at all.
`test_riskscreen_integration.py` (28) exercises `run_scan`/`cmd_*` end to
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
  and closely related to the historical-calibration item below (a snapshot
  store is most of what a paper-trading harness would need to replay
  against anyway).

### Recently shipped (two more sequential-but-independent loops parallelized)

Requested directly: installing and running felt slow. Profiled a real
`recommend`-style run rather than guessing (39 `baw` calls, 9.37s) and
found two more loops with the same shape as the concurrency fix already
in "Performance" -- independent, stateless `baw` calls being fetched one
at a time: `run_scan`'s per-protocol security-score lookups, and
`fetch_lp_investments`'s page 2+ fetches. Both now use the same
`ThreadPoolExecutor` pattern the pool-info/kline fetches already use.
See "Performance" above for the measured before/after. 2 new tests
(160 total, up from 158).

### Recently shipped (split riskscreen.py into a bstocks_lp/ package)

Requested directly: put the architecture in order now, since more features
are coming. `riskscreen.py` had grown to 2055 lines -- HTTP/subprocess I/O,
volatility estimators, the IL/breakeven model, the range model, the
pre-deposit screen, the scan pipeline, and all seven CLI commands, all in
one file. Split into a layered `bstocks_lp/` package (see "Code layout"
above) with `riskscreen.py` reduced to a 5-line launcher --
`python riskscreen.py <command>` is unchanged, no install step added.

The load-bearing constraint: `test_riskscreen_integration.py` mocks I/O
via `monkeypatch.setattr(riskscreen, "baw", fake)`, which only works
because every caller looks `baw` up in one shared module namespace at
call time. Across separate files that breaks unless every cross-module
call goes through the qualified module (`api.baw(...)`, never `from
bstocks_lp.api import baw`) -- adopted as a blanket rule for every call in
the package, not just the six names mocked today. Caught and fixed one
real bug the split would otherwise have introduced silently: the
stablecoin-config default path is `__file__`-relative, and moving that
code into `bstocks_lp/config.py` would have pointed it one directory
wrong (fails *closed* -- an empty stablecoin set -- with only a stderr
warning, so no existing test would have caught it).

Verified beyond the test suite: `ruff`/`mypy` clean across the new
package, a direct check that the stablecoin config still loads
(non-zero address count), and live `scan`/`recommend`/`range` runs
against the real API/`baw` session confirming byte-for-byte the same
output shape as before the refactor.

### Recently shipped (a standalone model paper — MODEL.md / MODEL.pdf)

The README's "The idea" section was always a summary; a user asked for the
full mathematical derivation in standard paper format, so it now exists
as its own document: [MODEL.md](MODEL.md) (Abstract through a numbered
§1-10, every formula traced to the `riskscreen.py` function that
implements it -- checked line-by-line against the actual code, not
written from memory) plus a typeset [MODEL.pdf](MODEL.pdf) rendered from
the same source. Caught and fixed one real drift in the process: this
README's own "The idea" section still described the straddling-range
`p_active` as "a conservative union-bound approximation" -- stale since
that was upgraded to the exact double-barrier reflection-series formula
several rounds ago (the union-bound is now only a degenerate-input
fallback); `README.zh-CN.md` already had this right, so the two READMEs
had quietly drifted apart on this one point until this pass caught it.

### Recently shipped (a third round on chart legibility — the labels still overlapped)

Round two's fix (a few candidate offset positions plus a leader line) still
wasn't enough once tested against real, dense data: several top-10 pools
sat within a few pixels of each other vertically, and no small fixed
offset in 4-6 directions avoids a neighbor that close. Two changes, both
in `SKILL.md`: (1) the `scan` chart's x-axis (`vol_ratio`) is now
logarithmic whenever the scanned range spans more than ~5x, which spreads
the dense, actually-relevant cluster out instead of letting 1-2 outlier
`WATCH` pools waste most of the linear axis's width; (2) label placement
no longer searches for open space near each point — it stacks labels in a
single column in the chart's margin, sorted by natural vertical position
with a minimum row gap enforced by a greedy push-down pass, which
guarantees zero label-label overlap by construction regardless of cluster
density. The trade-off is a longer leader line for points far from their
assigned row — confirmed as the right trade with the same real user
("引导线可以更长一点" — leader lines can be longer, that's fine).

### Recently shipped (a user asked "does this cover every bStock pool?")

A live investigation to answer that question found: 496 LP pools exist
system-wide, 40 of them name a bStock, `scan`'s default `--max-pages 3`
sees 38 of the 40 (the 2 missed were both near-zero-APY Uniswap V4 pools,
already hard-blocked regardless), and `recommend`'s default `--max-pages
1` sees far fewer still. Nothing in the output said so — a `max_pages`
cutoff and genuine market completeness looked identical. Fixed at the
source: `fetch_lp_investments` now reads the API's own `total` field
(the true pool count, independent of pages fetched) alongside the pools
it fetched, and `run_scan` returns a `coverage`
(`scan`/`recommend`)/`market_coverage` (`rebalance-check`) object —
`{pools_fetched, pools_total, truncated}` — through every command's
`--json` output. Text mode prints a `NOTE: scanned X/Y LP pools...` line
whenever a scan was actually cut short, so the answer is now in the
output itself instead of requiring a manual re-investigation each time.

3 new tests (158 total, up from 155).

### Recently shipped (leader lines — offset labels alone weren't enough)

A same-direction offset label next to each point (the previous round's
fix) turned out not to be enough on its own: in a tight cluster, a label
can still sit ambiguously between two points or overlap a neighbor.
`SKILL.md` now specifies actual collision-avoiding label placement (try a
few candidate offsets, skip ones that would overlap an already-placed
label) plus a leader line — a thin stroke from the point's edge to
wherever the label ends up — so the association is unambiguous regardless
of final position. Confirmed by rendering the corrected version for the
same real user.

### Recently shipped (a fourth round from real usage)

- **`SKILL.md` no longer tells the agent to invent a capital amount.**
  The previous guidance said to "pick one reasonable illustrative figure"
  when the user hadn't given a deposit size — a real user pushed back:
  even labeled as an example, an invented dollar figure reads as
  presuming something about their money they never said. Now: percentage
  figures (net APY, IL) by default, `$/yr` only once the user actually
  states an amount.
- **The default "top pools" depth changed from 2-3 to 10** — but sourced
  the efficient way: one `scan --top 10 --with-range --json` call (whose
  `best_range` per result already carries the recommended width,
  confidence, IL, and net APY) rather than ten separate
  `range --investmentId` calls, which would have quietly reintroduced the
  exact slow-invocation problem fixed two rounds ago. The full
  every-candidate-width `range` sweep is now explicitly scoped to one
  pool at a time (the top pick, or whichever pool the user asks about),
  not fanned out across ten.
- **Chart bubble sizing guidance tightened** (max radius ~24px → ~14px)
  after real dense clusters overlapped badly enough to swallow their own
  labels — confirmed by rendering the corrected version.

### Recently shipped (three more findings from real usage)

- **`range`'s text table now shows an explicit `il` column.** Impermanent
  loss was previously only implicit (`eff.apy` minus `net_apy`, never
  shown as its own number) — a user asked to see it directly rather than
  do the subtraction themselves. Added to both the straddle and
  sided-range (`--side sell`/`buy`) tables.
- **`--capital` now simulates `$/yr` for every range width, not just the
  recommended one.** Previously only the single recommended range got a
  dollar figure (in a summary line below the table); now every row gets
  its own `$/yr @<capital>` column, so "what's the actual dollar
  difference between ±20% and ±50%" is a glance, not five separate
  mental multiplications.
- **`SKILL.md` guidance strengthened on two more real gaps**: chart
  points must carry a visible identity (a label next to the point),
  never hover-only — a correct bubble chart is still a failure if you
  can't tell which bubble is which pool without hovering. And every
  chart/response now needs to briefly define `vol_ratio`/`p_active`/
  grade/verdict terms rather than assume the reader already knows them.
  Presenting a recommendation now has an explicit "full picture" bar:
  the market-wide chart *and* a range/IL/simulated-return comparison
  across the top 2-3 pools, not just the single top pick in isolation.

1 new test (155 total, up from 154).

### Recently shipped (two bugs found by a user's own testing)

- **Installing just to run the tests got noticeably slower** after
  `ruff`/`mypy`/`pip-audit` were added to `requirements.txt` — measured
  ~19s extra on a fast connection for tools the tests themselves never
  touch. Split into `requirements.txt` (`pytest` only, ~3.4s to install)
  and [`requirements-lint.txt`](requirements-lint.txt) (the lint-only
  tools, installed by CI's separate `lint` job) — see "Testing" above.
- **`scan`'s suggested chart was actively misleading.** The prior
  guidance (a bar chart of raw `vol_ratio`, sorted ascending) produced
  exactly the confusion a real user reported from their own screenshot:
  the *worst* pools (highest `vol_ratio`, Cheap-graded) got the *longest*
  bars, reading as "biggest = best" by default visual convention, when a
  *lower* `vol_ratio` is what's actually good — and TVL/safety-tier were
  buried in text labels, not shown visually at all. Replaced with a
  prescriptive bubble-scatter spec in `SKILL.md` (`vol_ratio` x-axis,
  `model_net_apy` y-axis, bubble size = TVL, color = grade, a reference
  line at the `ENTER`/`WATCH` boundary) — see "Visualizing results" in
  `SKILL.md`.

### Recently shipped (the ENTER/WATCH/NO_TRADE/UNSCOREABLE verdict system)

The first piece of the review's "product next stage" tier — the one the
review itself called longer-term — scoped down to what's concretely
buildable without a new external data source or a persistence layer: a
consistent `verdict` field built directly on `passes_trade_gate`'s
existing logic, not a new threshold. See "Every result is one of four
verdicts" above for the full picture.

- **Every `results`/`flagged`/`unscoreable` row now carries a `verdict`**
  (`ENTER`/`WATCH`/`NO_TRADE`/`UNSCOREABLE`) — previously only
  `recommend`'s "Top pick" selection computed `passes_trade_gate()`
  ad hoc; a `scan --json` consumer had no field to filter/sort on for "is
  this one actually worth it," only `grade` (vol_ratio alone, blind to
  whether net APY was even positive).
- **`recommend` now surfaces its `WATCH` pools** instead of going silent
  on everything that isn't the Top pick or an explicit `NO_TRADE` — both
  in the ranked table and as a one-line count in the `NO_TRADE` case, so
  "nothing to enter right now" and "nothing at all worth looking at" read
  as the different situations they are.
- **`scan`'s text tables gained a `verdict` column** (`--with-range` and
  plain), so a human reading the table gets the same label a `--json`
  consumer would parse.

Deliberately **not** attempted in this pass, and why: **decision
snapshots** (record a recommendation, revisit it later) needs a
persistence layer this stateless CLI script doesn't have yet, and a
schema decision worth getting right rather than bolting on quickly.
**Hard alerts** for corporate actions/depeg/liquidity crashes/incentive
expiry need a new external data source
(`binance-tokenized-securities-info`'s asset-market-status API, currently
only a manual reminder in `SKILL.md`) wired into the automated screen, not
just a one-off check. **Paper-trading/historical-replay** is the review's
own largest-scoped item — already tracked in Roadmap as "flagged, not
attempted" before this pass and still true after it. All three are real
product decisions, not bug fixes, and building them without confirming
the intended design first risks a lot of wasted effort on something that
doesn't match what's actually wanted.

5 new tests (154 total, up from 149) plus verdict assertions added to
several existing integration tests.

### Recently shipped (testing & engineering gaps, from the same review)

The review's fourth tier, closing out its concrete engineering asks: real
mocked coverage of the wiring between functions (not just the functions
themselves), and CI that actually exercises the platform-specific code
this tool's own security fix lives in. See "Testing" above for the full
picture; in brief:

- **`test_riskscreen_integration.py`** (22 tests, new file) — mocked
  `baw()`/`_get()` integration tests for `run_scan`/`cmd_*` end to end:
  malformed/failed API responses, `baw()`'s nonzero-exit and
  shell-metacharacter-rejection paths without a real subprocess, `--json`
  output validity on every command, `recommend`'s `NO_TRADE`/
  refuse-threshold/position-fetch-failure paths, `rebalance-check`'s
  multi-investmentId and best-alternative paths, and a real fixture
  (PancakeSwap Infinity) locking in a live finding from this review as a
  permanent regression test.
- **Property-style tests** for `no_exit_probability` (stays in `[0, 1]`
  across 200 randomized parameter combinations) and
  `_switching_recommendation` (dollar-gap sign always matches its verdict)
  — no new dependency (`hypothesis`), plain `random`-driven loops.
- **`build_parser()` split out of `main()`** so tests build real argparse
  `Namespace` objects (`build_parser().parse_args([...])`) instead of
  hand-constructing ones that can silently drift from the actual CLI.
- **Windows added to the CI matrix** — previously Ubuntu-only, so `baw()`'s
  Windows-specific `cmd.exe`-wrapping branch (the security-sensitive one
  from the command-injection fix) had zero CI coverage despite being the
  most sensitive code path in the file.
- **`ruff`, `mypy`, and `pip-audit`** added as a separate CI `lint` job.
  Fixed everything both tools found on the actual codebase (an
  inconsistently-typed variable in `_get()`, a couple of missing dict
  annotations, redundant f-strings, an unnecessary `open()` mode argument,
  ambiguous single-letter OHLC variable names) rather than just adding the
  tools and leaving pre-existing findings unaddressed. `ruff.toml`/
  `mypy.ini` deliberately scope out a few rule families that fight this
  codebase's intentional style (broad-but-translated exception handling
  at I/O boundaries, in particular) — documented inline in each config
  file, not silently suppressed.

22 new tests (149 total, up from 127). Every check in this section was
run locally against the real codebase before being added to CI, not
added speculatively and left to fail in CI first.

### Recently shipped (stability, speed & observability, from the same review)

The review's third tier: making failures diagnosable and refusing to
present a confident verdict built on too little data, rather than adding
new checks. See "Reliability: retries, error classification, refusing a
bad verdict" above for the full picture; in brief:

- **`_get()` retries transient HTTP failures** with exponential
  backoff+jitter (timeout, connection error, 5xx, 429), and builds its
  query string with `urllib.parse.urlencode` instead of naive string
  concatenation.
- **Concurrent fetch failures in `run_scan` are classified**, not
  collapsed into one generic message — `unscoreable` reasons now say
  *why* (timeout / network error / invalid data), and distinguish a
  failed kline fetch from a successful-but-too-thin one.
- **`scan --json` gains `failure_summary`** — a frequency count of
  `unscoreable` reasons for an at-a-glance view of what's failing and how
  much.
- **`recommend` refuses a verdict** (explicit `NO_TRADE`) when more than
  50% of candidate pools couldn't be evaluated at all, instead of quietly
  picking a "Top pick" from an unrepresentative scoreable sliver.
- **`fetch_stock_tokens()`/`fetch_klines()` are memoized in-process** —
  no more re-fetching the token list a second time just to check held
  positions after `run_scan()` already fetched it. Deliberately not
  extended to pool status/APY/TVL — see "Reliability" above for why.
- **Every JSON output shares one envelope shape** (`schema_version`,
  `status`, `run_id`, `as_of`, always present on success or error) via
  `_json_envelope()` — previously an error path printed a bare
  `{"error": ...}` with none of those fields, while success had no
  explicit `status` at all.

5 new tests (127 total). Verified live (successful fetch, URL-encoding of
special characters, a genuinely unreachable host retrying then failing
with a clear diagnosable error, counting actual HTTP calls across a full
`recommend` run to confirm the caching fix's second `fetch_stock_tokens()`
call became a cache hit rather than a real fetch, and every JSON output
site — success and error, all three commands — checked directly for the
new envelope fields) and via two synthetic end-to-end scenarios for the
`recommend` refuse-threshold (normal case unaffected, high-failure case
correctly refuses).

### Recently shipped (important logic & UX issues, from the same review)

The same review's second tier — correctness/completeness issues short of a
release blocker, but real gaps in what the tool actually checks:

- **`recommend` no longer masks a position-fetch failure as "no
  positions."** An expired session, a network error, or a malformed
  response used to fall back to `held=[]` silently — indistinguishable
  from genuinely holding nothing. Now prints an explicit "could not check
  your positions" message instead.
- **`rebalance-check` evaluates every `investmentId` on a held position,
  not just the first**, with the same peer-apy and protocol-security
  context `scan` uses even in the fallback path (for a held pool outside
  the scanned page range) — previously that fallback skipped both
  entirely, so a pool relying on the peer-outlier or security-score check
  to get flagged could sail through undetected. Multiple ids on one
  position now aggregate worst-case: any flagged id flags the whole
  position, the reported `vol_ratio` is the worst among the scoreable
  ones, and ids that can't be evaluated at all are counted and reported,
  not silently dropped.
- **V4-generation pools are now detected via the structured
  `defiProtocolId`, not the display name.** Checking the live API surface
  for a real structured field (rather than assuming none existed) turned
  up an actual gap: PancakeSwap's own V4 is marketed as "PancakeSwap
  Infinity," with no "v4"/"V4" substring anywhere in that name — the old
  name-match was silently letting it through the hard block. Confirmed
  live: `defiProtocolId="pancakeswap4"`, 3 such pools live on BSC at the
  time this was found. None happened to be bStock pools yet, but the gap
  was real, not hypothetical.
- **The stablecoin address list moved to [`stablecoins.json`](stablecoins.json)**,
  overridable via `BSTOCKS_STABLECOIN_CONFIG` — see "Not every pool is
  quoted against a stablecoin" above.
- **`--allow-v4` now requires a reason, not a bare flag** —
  `--allow-v4 "already audited by X"`, recorded verbatim as
  `v4_override_reason` in `--json` output on every command that produces
  it. Overriding a block that exists specifically because this tool can't
  see hook risk should leave a record of *why*, not just that someone did
  it.
- **`rebalance-check` names a concrete `best_alternative` and computes a
  real switching verdict** instead of only comparing grades — see
  "rebalance-check: a concrete alternative, not just a grade" above.

17 new tests (122 total, up from 105). Every fix verified against live
market data — including, for the V4 case, confirming the exact real-world
pool that the old logic missed.

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
  vol_ratio < 1) — see "Every result is one of four verdicts" above (later
  generalized into the full ENTER/WATCH/NO_TRADE/UNSCOREABLE verdict system).
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
