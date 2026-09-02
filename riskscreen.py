#!/usr/bin/env python3
"""bStocks LP Assistant -- volatility-aware LP advisor for Binance Web3 bStock pools.

Scoped to bStocks specifically (RWA list `type=3`, symbols suffixed "...B",
e.g. TSLAB, NVDAB) -- not Ondo ("...on") or xStocks ("...x") tokenized
stocks, which are separate providers on the same underlying tickers.
`fetch_stock_tokens()` defaults to `type_filter=BSTOCK_TYPE`; pass
`type_filter=None`/`--type 0` explicitly to browse other providers.

LP fee/incentive APY is compensation for impermanent loss (IL), and IL scales
with the volatility of the pooled assets. This tool re-ranks LP pools by
APY *net of* an estimated IL cost, so pools aren't compared on headline APY
alone. Stock-token pools are the clearest case: they're paired against a
stablecoin, so IL is driven almost entirely by the stock token's own
volatility (no cross-asset correlation term needed).

Scientific evaluation: LPing (full-range) is mathematically a continuously
delta-hedged short ATM straddle -- the fee APY is the "premium" you collect
for selling volatility. This tool computes each pool's *breakeven volatility*
sigma* (the realized vol at which fee income exactly offsets expected IL) and
scores pools by sigma_realized / sigma* -- the same "is realized vol richly
or cheaply priced" comparison options traders make with IV/RV. See
`breakeven_volatility` / `vol_richness_ratio` below.

Range modes: `range --side straddle` (default) sweeps symmetric market-making
ranges around the current price. `--side sell` / `--side buy` model a
concentrated position placed entirely on one side of the current price --
functionally a limit order that earns fees while it waits to execute.

Data sources (all public, no auth):
  - RWA stock token list / kline: bapi/defi public endpoints (Binance Web3)
Data source (needs an active `baw` session):
  - LP pool APY/TVL/composition: `baw defi investment-list` / `investment-info`

Usage:
  python riskscreen.py recommend                          # single entry point: one verdict
  python riskscreen.py stocks --limit 20
  python riskscreen.py vol --ticker TSLA --days 30
  python riskscreen.py scan --top 15 --capital 10000
  python riskscreen.py range --ticker TSLA --apy 0.30 --side straddle
  python riskscreen.py range --ticker TSLA --apy 0.30 --side sell --target-offset 0.15
  python riskscreen.py positions
  python riskscreen.py rebalance-check

Deliberately NOT included: deposit/withdraw execution. `scan`/`range` surface
the `investmentId` + token addresses; actually moving funds goes through
`binance-agentic-wallet`'s already-reviewed `defi deposit` / `defi lp-add` /
`defi redeem` / `defi lp-remove` flow (preview -> explicit user confirmation
-> execute). This tool only ever recommends.
"""

import argparse
import concurrent.futures
import json
import math
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone

MAX_CONCURRENT_BAW_CALLS = 8  # bounds parallel `baw`/kline fetches -- see run_scan's Pass 1

RWA_LIST_URL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/stock/detail/list/ai"
KLINE_URL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/kline/ai"
UA = "binance-web3/1.1 (Skill)"

DAYS_PER_YEAR = 365


HTTP_MAX_RETRIES = 3
HTTP_BASE_RETRY_DELAY = 0.5  # seconds; exponential backoff with jitter, see _get


def _get(url, params, max_retries=HTTP_MAX_RETRIES):
    """GET url?params as JSON, with bounded retry-with-jitter on transient failures (timeout,
    connection error, 5xx, 429 rate-limit) -- not on a 4xx client error otherwise, which won't
    fix itself on retry, and not on a malformed/wrong-shaped response body, which more likely
    means a real API contract problem than a network blip. Raises RuntimeError with the
    failure classified and enough context to diagnose (url, status if any, attempt count) once
    retries are exhausted, instead of a bare urllib traceback.
    """
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(full_url, headers={"Accept-Encoding": "identity", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            try:
                body = json.loads(raw)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"GET {full_url} returned non-JSON response: {raw[:200]!r}") from e
            if not isinstance(body, dict):
                raise RuntimeError(f"GET {full_url} returned unexpected JSON shape "
                                    f"(expected an object, got {type(body).__name__})")
            return body
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                last_error = e
            else:
                raise RuntimeError(f"GET {full_url} failed: HTTP {e.code} {e.reason}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_error = e
        if attempt < max_retries:
            delay = HTTP_BASE_RETRY_DELAY * (2 ** (attempt - 1)) * (1 + random.random())
            time.sleep(delay)
    raise RuntimeError(f"GET {full_url} failed after {max_retries} attempts: {last_error}") from last_error


BSTOCK_TYPE = 3  # RWA list `type`: 1=Ondo ("...on"), 2=xStocks ("...x"), 3=bStock ("...B")


def fetch_stock_tokens(type_filter=BSTOCK_TYPE):
    """Defaults to bStock (type=3) only -- this product is scoped to bStocks specifically,
    not tokenized-stock LPs on other providers. Pass type_filter=None for every platform,
    or 1/2 to browse Ondo/xStocks instead."""
    params = {"type": type_filter} if type_filter else {}
    body = _get(RWA_LIST_URL, params)
    if not body.get("success"):
        raise RuntimeError(f"RWA list fetch failed: {body}")
    return body["data"]


def fetch_klines(chain_id, contract_address, interval="1d", limit=90):
    body = _get(KLINE_URL, {
        "chainId": chain_id,
        "contractAddress": contract_address,
        "interval": interval,
        "limit": limit,
    })
    if not body.get("success"):
        raise RuntimeError(f"kline fetch failed: {body}")
    return body["data"]["klineInfos"]


INTERVAL_TO_ANNUALIZATION = {
    "1d": DAYS_PER_YEAR,
    "4h": DAYS_PER_YEAR * 6,
    "1h": DAYS_PER_YEAR * 24,
}


def _log_return_volatility(values, interval="1d"):
    """Shared core: annualized stdev of log-returns of a price-like series."""
    if len(values) < 3:
        return None
    log_returns = [math.log(values[i] / values[i - 1]) for i in range(1, len(values))]
    n = len(log_returns)
    mean = sum(log_returns) / n
    variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
    periods_per_year = INTERVAL_TO_ANNUALIZATION.get(interval, DAYS_PER_YEAR)
    return math.sqrt(variance * periods_per_year)


def annualized_volatility(klines, interval="1d"):
    closes = [float(c[4]) for c in klines if float(c[4]) > 0]
    return _log_return_volatility(closes, interval)


def _rogers_satchell_variance(o, h, l, c):
    """Per-candle Rogers-Satchell variance term -- drift-independent (Rogers & Satchell, 1991)."""
    return math.log(h / c) * math.log(h / o) + math.log(l / c) * math.log(l / o)


def yang_zhang_volatility(klines, interval="1d"):
    """Yang-Zhang (2000) OHLC volatility estimator: sigma_YZ^2 = sigma_overnight^2 +
    k*sigma_open_close^2 + (1-k)*sigma_RogersSatchell^2, k = 0.34/(1.34 + (n+1)/(n-1)).

    Source: Yang, D. and Zhang, Q., "Drift-Independent Volatility Estimation Based on High,
    Low, Open, and Close Prices", Journal of Business, 2000. Unlike annualized_volatility
    (close-to-close only), this uses all four OHLC prices our kline data already carries and
    is ~5-14x more statistically efficient at the same sample size (per Yang-Zhang and the
    range-based-estimator literature it builds on) -- meaningful here because our kline
    history is often short. It's also drift-independent (unbiased under a trending price,
    unlike naive close-to-close over a short window) and, structurally relevant to bStocks
    specifically: the overnight (close-to-open) term explicitly separates out the jump across
    a session gap instead of blending it uniformly into one return series -- a step toward
    the "session-aware volatility" idea in the README/Roadmap, not a full implementation of it
    (it doesn't yet distinguish *which* gaps are real trading-hours boundaries).

    This is the primary estimator for stablecoin-quoted pools now (annualized_volatility is
    kept as the fallback when OHLC data looks degenerate, and is still what
    relative_annualized_volatility uses for the non-stablecoin/ratio case -- there is no
    standard OHLC estimator for a *ratio* of two assets' prices in the literature, so that
    path is a documented, deliberate scope limit, not an oversight).
    """
    rows = [row for row in klines if all(float(row[i]) > 0 for i in (1, 2, 3, 4))]
    if len(rows) < 3:
        return None
    opens = [float(r[1]) for r in rows]
    highs = [float(r[2]) for r in rows]
    lows = [float(r[3]) for r in rows]
    closes = [float(r[4]) for r in rows]

    n = len(rows) - 1
    overnight = [math.log(opens[i] / closes[i - 1]) for i in range(1, len(rows))]
    open_close = [math.log(closes[i] / opens[i]) for i in range(1, len(rows))]
    rs_terms = [_rogers_satchell_variance(opens[i], highs[i], lows[i], closes[i]) for i in range(1, len(rows))]

    mean_on = sum(overnight) / n
    var_on = sum((x - mean_on) ** 2 for x in overnight) / (n - 1) if n > 1 else 0.0
    mean_oc = sum(open_close) / n
    var_oc = sum((x - mean_oc) ** 2 for x in open_close) / (n - 1) if n > 1 else 0.0
    var_rs = sum(rs_terms) / n

    k = 0.34 / (1.34 + (n + 1) / (n - 1)) if n > 1 else 0.34
    var_yz = max(var_on + k * var_oc + (1 - k) * var_rs, 0.0)
    periods_per_year = INTERVAL_TO_ANNUALIZATION.get(interval, DAYS_PER_YEAR)
    return math.sqrt(var_yz * periods_per_year)


def best_available_volatility(klines, interval="1d"):
    """Yang-Zhang when the OHLC data supports it, falling back to close-to-close otherwise --
    the single volatility function callers should reach for on a stablecoin-quoted pool."""
    yz = yang_zhang_volatility(klines, interval)
    return yz if yz is not None else annualized_volatility(klines, interval)


STABLECOIN_CONFIG_ENV_VAR = "BSTOCKS_STABLECOIN_CONFIG"
DEFAULT_STABLECOIN_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stablecoins.json")


def _load_stablecoin_addresses(config_path=None):
    """Load the (chainId, address) stablecoin set from a JSON config instead of a hardcoded
    constant in this file, so a new stablecoin -- or a wrong one -- is fixed by editing
    stablecoins.json, not shipping a code change. Path resolution: an explicit `config_path`
    argument, else the BSTOCKS_STABLECOIN_CONFIG env var, else the bundled stablecoins.json
    next to this script.

    Fails closed on a missing/malformed config: prints a warning and returns an empty set
    rather than crashing the whole script or silently keeping a stale in-code fallback. An
    empty set doesn't misclassify pools as unsafe -- is_stablecoin() already treats "not in
    the set" as non_stablecoin, so every pool just falls through to the stricter
    relative-volatility path (which converges to the same answer for a genuine stablecoin
    quote anyway, since its own volatility is ~0) instead of silently trusting a name.
    """
    path = config_path or os.environ.get(STABLECOIN_CONFIG_ENV_VAR) or DEFAULT_STABLECOIN_CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {(str(e["chainId"]), e["address"].lower()) for e in data["stablecoins"]}
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"warning: could not load stablecoin config from {path} ({e}) -- treating no "
              f"quote asset as a stablecoin until this is fixed (safer than guessing wrong)",
              file=sys.stderr)
        return set()


STABLECOIN_ADDRESSES = _load_stablecoin_addresses()


def is_stablecoin(chain_id, address):
    """An address NOT in STABLECOIN_ADDRESSES is never presumed to be a stablecoin -- an
    unrecognized quote asset always falls through to the stricter relative-volatility path."""
    return (str(chain_id), address.lower()) in STABLECOIN_ADDRESSES


MIN_ALIGNED_SAMPLES = 30
MIN_ALIGNED_COVERAGE_RATIO = 0.8

INTERVAL_TO_MS = {
    "1d": 24 * 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1h": 60 * 60 * 1000,
}


def relative_annualized_volatility(stock_klines, quote_klines, interval="1d"):
    """Annualized volatility of log(P_stock / P_quote), for pools quoted against a
    non-stablecoin asset (e.g. bStock/BNB). IL for such a pool depends on the *relative*
    price move between the two pooled assets, not the bStock's volatility alone -- ignoring
    the quote asset's own volatility understates risk whenever it moves too (BNB is not flat).

    Aligns the two kline series by candle open-time and uses only the overlapping window.
    Two independently-fetched series can intersect into something far sparser or gappier than
    either series alone (different listing dates, uneven on-chain activity, a missed candle on
    one leg) -- annualizing a handful of scattered points as if they were a dense daily series
    would silently understate the true variance and produce a misleadingly precise-looking
    number. So the result is only trusted once it clears two floors: at least
    MIN_ALIGNED_SAMPLES aligned candles, and at least MIN_ALIGNED_COVERAGE_RATIO of the
    theoretical fully-dense span between the first and last aligned candle actually present.
    Falling short of either floor returns sigma=None with `reason` set, for unscoreable
    reporting -- never a number computed from data too thin to trust.

    Returns a dict: {sigma, sample_count, coverage_ratio, latest_candle_at, reason}.
    """
    stock_by_time = {c[0]: float(c[4]) for c in stock_klines if float(c[4]) > 0}
    quote_by_time = {c[0]: float(c[4]) for c in quote_klines if float(c[4]) > 0}
    common_times = sorted(set(stock_by_time) & set(quote_by_time))
    sample_count = len(common_times)
    latest_candle_at = common_times[-1] if common_times else None

    if sample_count < 2:
        return {"sigma": None, "sample_count": sample_count, "coverage_ratio": 0.0,
                "latest_candle_at": latest_candle_at,
                "reason": f"only {sample_count} aligned candle(s) between the two legs"}

    step_ms = INTERVAL_TO_MS.get(interval, INTERVAL_TO_MS["1d"])
    span = common_times[-1] - common_times[0]
    expected_span = step_ms * (sample_count - 1)
    coverage_ratio = min(expected_span / span, 1.0) if span > 0 else 1.0

    if sample_count < MIN_ALIGNED_SAMPLES:
        return {"sigma": None, "sample_count": sample_count, "coverage_ratio": coverage_ratio,
                "latest_candle_at": latest_candle_at,
                "reason": f"only {sample_count} aligned candles (need >= {MIN_ALIGNED_SAMPLES})"}
    if coverage_ratio < MIN_ALIGNED_COVERAGE_RATIO:
        return {"sigma": None, "sample_count": sample_count, "coverage_ratio": coverage_ratio,
                "latest_candle_at": latest_candle_at,
                "reason": f"aligned candles too sparse/gappy ({coverage_ratio:.0%} coverage, "
                          f"need >= {MIN_ALIGNED_COVERAGE_RATIO:.0%})"}

    ratios = [stock_by_time[t] / quote_by_time[t] for t in common_times]
    sigma = _log_return_volatility(ratios, interval)
    return {"sigma": sigma, "sample_count": sample_count, "coverage_ratio": coverage_ratio,
            "latest_candle_at": latest_candle_at,
            "reason": None if sigma is not None else "degenerate ratio series"}


def resolve_pool_stock_and_quote(pool, info, stock_index):
    """Identify the confirmed bStock side and the paired ("quote") token of a pool from its
    on-chain `assetTokenList`, and classify the pair. Returns (stock, chain_id, quote_addr,
    pair_mode).

    Only pools with exactly 2 distinct on-chain assets, exactly one of which is a confirmed
    bStock, are supported: the IL model this tool uses (E[IL] ~ sigma^2/8, "LP as short ATM
    straddle") assumes a two-asset pool and doesn't generalize to 3+-asset weighted pools or
    dual-bStock pairs without a different formula. Anything else -- wrong asset count, zero
    confirmed bStocks (don't trust a name-match guess instead, see run_scan), or more than one
    -- returns stock=None, pair_mode="unsupported" rather than silently picking one asset out
    of several and mis-scoring the pool. `pair_mode` is "stablecoin"/"non_stablecoin" once
    `stock` is resolved. Shared by run_scan's Pass 1 and rebalance-check's fallback path for a
    held position outside the scanned market set, so both classify a pool's pair the same way.
    """
    chain_id = pool.get("binanceChainId") or info.get("binanceChainId")
    asset_list = info.get("assetTokenList") or []
    unique_addresses = {a["tokenAddress"].lower() for a in asset_list}
    if len(unique_addresses) != 2:
        return None, chain_id, None, "unsupported"

    bstock_matches = [stock_index[(chain_id, addr)] for addr in unique_addresses if (chain_id, addr) in stock_index]
    if len(bstock_matches) != 1:
        return None, chain_id, None, "unsupported"

    stock = bstock_matches[0]
    quote_addr = next(addr for addr in unique_addresses if addr != stock["contractAddress"].lower())
    pair_mode = "stablecoin" if is_stablecoin(chain_id, quote_addr) else "non_stablecoin"
    return stock, chain_id, quote_addr, pair_mode


def resolve_pool_volatility(stock, chain_id, quote_addr, pair_mode):
    """Sequential, single-pool volatility resolution -- for use where only one pool needs
    evaluating (e.g. rebalance-check's fallback), where run_scan's Pass 1 concurrent-batch
    machinery would be overkill for just one. Returns sigma, or None if it couldn't be computed."""
    if pair_mode == "unsupported" or stock is None:
        return None
    stock_klines = fetch_klines(stock["chainId"], stock["contractAddress"], limit=91)
    if pair_mode == "stablecoin":
        return best_available_volatility(stock_klines)
    quote_klines = fetch_klines(chain_id, quote_addr, limit=91)
    return relative_annualized_volatility(stock_klines, quote_klines)["sigma"]


def expected_il_fraction(sigma_annual):
    """Diffusion approximation for constant-product AMM IL: E[IL] ~= sigma^2 * T / 8.

    Valid for full-range (V2-style) liquidity over T=1 year, ignoring drift.
    Concentrated (V3) positions amplify this by roughly 1/range_width_factor;
    callers should treat this as a floor, not the realized IL for a narrow range.
    """
    return (sigma_annual ** 2) / 8


def breakeven_volatility(apy):
    """Volatility at which full-range fee APY exactly offsets expected IL (apy = sigma*^2/8).

    Providing full-range constant-product liquidity is mathematically equivalent to
    continuously delta-hedging a short ATM straddle -- fee APY is the premium collected
    for selling volatility. sigma* is the "implied volatility" that premium is pricing in.
    Concentration multiplies both fee income and IL by the same factor M, so M cancels
    out of this equation: sigma* is a property of the pool's quoted APY alone, independent
    of which range you'd choose to hold it in.
    """
    return math.sqrt(8 * apy) if apy > 0 else 0.0


def vol_richness_ratio(sigma_realized, apy):
    """sigma_realized / sigma* -- the options-trading-style "is this vol richly priced" ratio.

    <1: the pool pays more than the risk realized volatility implies you should need (rich premium).
    >1: fee income doesn't cover the volatility risk actually observed (cheap premium, bad deal).
    This is range-independent by construction -- see breakeven_volatility.
    """
    be = breakeven_volatility(apy)
    if be <= 0:
        return float("inf") if sigma_realized > 0 else None
    return sigma_realized / be


RICHNESS_BANDS = [(0.5, "Rich"), (1.0, "Fair")]  # else "Cheap"


def richness_grade(vol_ratio):
    """Qualitative tier for vol_ratio, the "Richness Score": Rich (<0.5) pays well above
    realized risk, Fair (0.5-1.0) a modest edge, Cheap (>=1.0) doesn't clear its own risk bar."""
    if vol_ratio is None:
        return "n/a"
    for ceiling, label in RICHNESS_BANDS:
        if vol_ratio < ceiling:
            return label
    return "Cheap"


def confidence_grade(p_active):
    """Qualitative tier for p_active (stay-in-range / execution probability)."""
    if p_active is None:
        return "n/a"
    if p_active >= 0.8:
        return "High"
    if p_active >= SAFETY_P_ACTIVE_FLOOR:
        return "Moderate"
    return "Low"


def risk_adjusted_apy(apy, sigma_annual):
    il = expected_il_fraction(sigma_annual)
    net = apy - il
    return {
        "apy": apy, "sigma_annual": sigma_annual, "expected_il": il, "model_net_apy": net,
        "vol_ratio": vol_richness_ratio(sigma_annual, apy),
    }


# `model_net_apy` is this tool's own estimate (platform apy minus modeled IL), not a promised
# or historical return. Checked directly against `defi investment-info`'s response shape (see
# README): the platform's `apy`/`apyBps` is a single blended figure with no fee-vs-incentive
# split, no as-of timestamp, and no lockup/redemption/incentive-expiry data available through
# this API on any pool sampled -- so this is a documented data-availability limit, not an
# oversight. An incentive-heavy apy can look attractive right up until the incentive program
# ends, with nothing in this tool able to see that coming.
MODEL_APY_CAVEAT = ("model_net_apy is a model estimate (platform apy minus modeled IL), not a "
                     "promised or historical return. The platform apy itself is a single blended "
                     "fee+incentive figure -- no breakdown, timestamp, or lockup/expiry data is "
                     "available from the API, so an incentive-heavy apy can collapse with no warning.")


def _normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _il_at_price_ratio(k):
    """Standard constant-product IL, as a positive loss fraction, at price ratio k = P1/P0."""
    return 1 - 2 * math.sqrt(k) / (1 + k)


def concentration_multiplier(pa, pb):
    """Capital-efficiency multiplier of range [pa, pb] (price ratios to current price) vs
    full range -- Uniswap V3 math. Works for any 0 < pa < pb, straddling the current price
    (pa < 1 < pb) or entirely on one side of it (pa >= 1 or pb <= 1)."""
    denom = 1 - math.sqrt(pa / pb)
    return 1 / denom if denom > 1e-9 else float("inf")


def _single_barrier_touch_probability(barrier_ratio, sigma_annual, years=1.0):
    """P(a driftless lognormal price ever crosses barrier_ratio = barrier/P0 within [0,T]),
    via the reflection principle."""
    if barrier_ratio <= 0:
        return 1.0
    s = sigma_annual * math.sqrt(years)
    if s <= 0:
        return 0.0
    a = abs(math.log(barrier_ratio)) / s
    return 2 * (1 - _normal_cdf(a))


def _union_bound_no_exit_probability(pa, pb, sigma_annual, years=1.0):
    """The original conservative approximation: 1 - P(touch pa) - P(touch pb). Kept as a
    fallback/reference; no_exit_probability now uses the exact reflection series below."""
    p_low = _single_barrier_touch_probability(pa, sigma_annual, years)
    p_high = _single_barrier_touch_probability(pb, sigma_annual, years)
    return max(0.0, 1 - p_low - p_high)


def _exact_double_barrier_no_exit_probability(pa, pb, sigma_annual, years=1.0, terms=15):
    """Exact probability a driftless lognormal price stays within [pa, pb] for the entire
    period, via the method-of-images reflection series for Brownian motion absorbed at two
    barriers -- the classical closed-form solution (see double-barrier option pricing
    literature, e.g. Kunitomo & Ikeda 1992), not an approximation. In log-price space with
    a=ln(pa) < 0 < b=ln(pb), w=b-a, s=sigma*sqrt(years):

        P(survive) = sum_{n=-N}^{N} { [Phi((b-2nw)/s) - Phi((a-2nw)/s)]
                                       - [Phi((b-2a+2nw)/s) - Phi((-a+2nw)/s)] }

    Terms decay ~Gaussian in n, so a truncated sum (default 15 terms each side) converges to
    machine precision for any realistic (pa, pb, sigma, years) combination here. Cross-checked
    against direct Monte Carlo path simulation in test_riskscreen.py -- if you touch this
    formula, re-run those tests; a silently-wrong "exact" result is worse than the honest
    approximation it replaces.
    """
    s = sigma_annual * math.sqrt(years)
    if s <= 0:
        return 1.0
    a, b = math.log(pa), math.log(pb)
    w = b - a
    total = 0.0
    for n in range(-terms, terms + 1):
        term1 = _normal_cdf((b - 2 * n * w) / s) - _normal_cdf((a - 2 * n * w) / s)
        term2 = _normal_cdf((b - 2 * a + 2 * n * w) / s) - _normal_cdf((-a + 2 * n * w) / s)
        total += term1 - term2
    return min(max(total, 0.0), 1.0)


def no_exit_probability(pa, pb, sigma_annual, years=1.0):
    """Probability that price stays within [pa, pb] for the *entire* period (not just the
    endpoint). Uses the exact reflection-series formula when the start price 1.0 is strictly
    between the barriers (the case it's derived for); falls back to the conservative
    union-bound approximation otherwise (degenerate/edge inputs)."""
    if not (pa < 1 < pb):
        return _union_bound_no_exit_probability(pa, pb, sigma_annual, years)
    return _exact_double_barrier_no_exit_probability(pa, pb, sigma_annual, years)


def range_metrics(pool_apy, sigma_annual, pa, pb, years=1.0):
    """Range-adjusted fee APY / IL / net APY for range [pa, pb] (price ratios to current
    price = 1.0), approximating the pool's reported APY as a full-range-equivalent baseline
    (the platform doesn't expose per-tick fee data, so this scales a blended pool APY rather
    than a true full-range rate -- see README/SKILL.md caveat).

    Straddling ranges (pa < 1 < pb) are market-making: `mode="market_making"`, `p_active` is
    the probability of never exiting the range (the "safety" of collecting fees the whole
    period). Single-sided ranges (pa >= 1 or pb <= 1) are yield-enhanced limit orders:
    `mode="sell_limit"`/`"buy_limit"`, `p_active` is the probability the order ever executes
    at all (touches the near boundary) -- a different question, and typically a materially
    higher number than an equivalent-width straddling range's stay-probability.

    Known simplification: for single-sided ranges the `expected_il` figure still uses the
    IL-vs-50/50-hold formula, which isn't quite the right comparison for a position that
    starts 100% in one asset. Treat it as a generic "cost of providing liquidity here" proxy,
    not a precise "execution price vs a plain limit order" model -- that refinement is not
    yet implemented.
    """
    if pa <= 0 or pb <= pa:
        raise ValueError("need 0 < pa < pb")
    m = concentration_multiplier(pa, pb)
    straddle = pa < 1 < pb
    if straddle:
        mode = "market_making"
        p_active = no_exit_probability(pa, pb, sigma_annual, years)
    else:
        mode = "sell_limit" if pa >= 1 else "buy_limit"
        near_barrier = pa if pa >= 1 else pb
        p_active = _single_barrier_touch_probability(near_barrier, sigma_annual, years)
    effective_apy = pool_apy * m * p_active
    il_diffusion = m * (sigma_annual ** 2) * years / 8
    il_boundary = max(_il_at_price_ratio(pa), _il_at_price_ratio(pb))
    expected_il = min(il_diffusion, il_boundary)
    net_apy = effective_apy - expected_il
    return {
        "pa": pa, "pb": pb, "mode": mode, "concentration": m, "p_active": p_active,
        "effective_apy": effective_apy, "expected_il": expected_il, "model_net_apy": net_apy,
        "vol_ratio": vol_richness_ratio(sigma_annual, pool_apy),
    }


DEFAULT_STRADDLE_WIDTHS = [0.05, 0.10, 0.20, 0.30, 0.50, 0.90]
DEFAULT_SIDED_OFFSETS = [0.05, 0.10, 0.20, 0.30, 0.50]
SIDED_BAND_WIDTH = 0.10
SAFETY_P_ACTIVE_FLOOR = 0.6


def recommend_range(pool_apy, sigma_annual, side="straddle", years=1.0,
                     target_offset=None, band_width=SIDED_BAND_WIDTH):
    """Sweep a set of candidate ranges and recommend the one with the highest net_apy among
    those meeting the SAFETY_P_ACTIVE_FLOOR probability floor. `side`: "straddle" (default,
    symmetric market-making ranges around the current price), "sell" (single-sided ranges
    above current price -- a limit-sell-style order), or "buy" (single-sided ranges below --
    a limit-buy-style order).

    `target_offset` (sell/buy only): if given, an exact offset from the current price (e.g.
    0.15 = 15% above/below) to evaluate *in addition to* the preset sweep -- for "I want to
    sell/buy at this specific price," not just "show me the standard offsets." `band_width`
    sets that target range's width (default SIDED_BAND_WIDTH); the row is marked
    `"is_target": True` so callers can highlight it distinctly from the sweep.
    """
    rows = []
    if side == "straddle":
        for w in DEFAULT_STRADDLE_WIDTHS:
            rows.append(range_metrics(pool_apy, sigma_annual, 1 - w, 1 + w, years))
        il = expected_il_fraction(sigma_annual)
        rows.append({
            "pa": None, "pb": None, "mode": "market_making", "concentration": 1.0,
            "p_active": 1.0, "effective_apy": pool_apy, "expected_il": il,
            "model_net_apy": pool_apy - il, "vol_ratio": vol_richness_ratio(sigma_annual, pool_apy),
        })
    elif side == "sell":
        for offset in DEFAULT_SIDED_OFFSETS:
            rows.append(range_metrics(pool_apy, sigma_annual, 1 + offset, 1 + offset + band_width, years))
        if target_offset is not None:
            row = range_metrics(pool_apy, sigma_annual, 1 + target_offset, 1 + target_offset + band_width, years)
            row["is_target"] = True
            rows.append(row)
    elif side == "buy":
        for offset in DEFAULT_SIDED_OFFSETS:
            pb = 1 - offset
            pa = max(pb - band_width, 0.01)
            rows.append(range_metrics(pool_apy, sigma_annual, pa, pb, years))
        if target_offset is not None:
            pb = 1 - target_offset
            pa = max(pb - band_width, 0.01)
            row = range_metrics(pool_apy, sigma_annual, pa, pb, years)
            row["is_target"] = True
            rows.append(row)
    else:
        raise ValueError(f"unknown side {side!r}")
    safe = [r for r in rows if r["p_active"] >= SAFETY_P_ACTIVE_FLOOR]
    best = max(safe or rows, key=lambda r: r["model_net_apy"])
    return rows, best


_BAW_SHELL_METACHARACTERS = set('&|<>^%!"\'\r\n\0')
_BAW_PATH = None


def _validate_baw_arg(arg):
    """Reject anything that could be interpreted as a shell metacharacter.

    Belt-and-suspenders even with shell=False: on Windows, `baw` resolves to a
    `baw.cmd` npm shim, and CreateProcess's documented fallback for .bat/.cmd targets
    internally re-invokes cmd.exe regardless of the Python-level shell= flag, so a
    value built from API data or a CLI flag (investmentId, defiProtocolId, ticker)
    could still reach cmd.exe's own parser. Blocking its metacharacters here closes
    that gap without depending on exactly how Windows dispatches the child process.
    """
    s = str(arg)
    if not s:
        raise ValueError("baw() received an empty argument")
    bad = _BAW_SHELL_METACHARACTERS & set(s)
    if bad:
        raise ValueError(f"baw() argument contains disallowed character(s) {sorted(bad)!r}: {s!r}")
    return s


def _resolve_baw_path():
    global _BAW_PATH
    if _BAW_PATH is None:
        path = shutil.which("baw")
        if not path:
            raise RuntimeError("baw CLI not found on PATH -- install the Binance Agentic Wallet CLI first")
        _BAW_PATH = path
    return _BAW_PATH


def baw(*args):
    """Shell out to the `baw` CLI and parse its --json output.

    Runs with shell=False against baw's resolved absolute path. On Windows, baw
    resolves to a `baw.cmd` npm shim, which needs cmd.exe as an interpreter; letting
    Windows' own CreateProcess .cmd fallback invoke that cmd.exe implicitly was tried
    and measured to corrupt non-ASCII output (confirmed live: real pool names came
    back as e.g. 'USDT-\u0163\ufffd\ufffd' instead of their Chinese text), because that
    implicit cmd.exe uses the system OEM codepage rather than UTF-8. So cmd.exe is
    invoked explicitly here with `chcp 65001` first, same as before -- the difference
    from the old implementation is that every argument is validated by
    _validate_baw_arg to exclude shell metacharacters before being embedded in the
    command string, which is what actually closes the injection risk (list2cmdline
    only does CRT-style argv quoting, not shell-metacharacter escaping).
    """
    baw_path = _resolve_baw_path()
    validated = [_validate_baw_arg(a) for a in args]
    if os.name == "nt":
        inner = subprocess.list2cmdline([baw_path, *validated, "--json"])
        comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        cmd = [comspec, "/d", "/c", f"chcp 65001>nul & {inner}"]
    else:
        cmd = [baw_path, *validated, "--json"]
    result = subprocess.run(cmd, capture_output=True, timeout=30, shell=False,
                             text=True, encoding="utf-8", errors="replace")
    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError(
            f"baw {' '.join(args)} produced no output (exit code {result.returncode}, stderr: {result.stderr.strip()})"
        )
    first_brace = stdout.find("{")
    try:
        return json.loads(stdout[first_brace:] if first_brace > 0 else stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"baw {' '.join(args)} produced non-JSON output (exit code {result.returncode}): {stdout[:500]!r}"
        ) from e


def fetch_lp_investments(max_pages=3):
    """Page through `defi investment-list` (max 100/page per the API) instead of reading only
    the first page. Sorted by apy DESC (the API default), so later pages surface lower-apy --
    but not necessarily lower risk-adjusted -- pools that a single 100-pool page would never
    reach. Stops early once a page comes back short (no more results) or `max_pages` is hit.
    """
    all_pools = []
    for page in range(1, max_pages + 1):
        body = baw("defi", "investment-list", "--investType", "LiquidityPool", "--size", "100", "--page", str(page))
        if not body.get("success"):
            raise RuntimeError(json.dumps(body.get("error", body)))
        page_list = body["data"]["list"]
        all_pools.extend(page_list)
        if len(page_list) < 100:
            break
    return all_pools


POOL_SHARE_WARNING_THRESHOLD = 0.20  # warn if an intended deposit would exceed this share of pool TVL


def position_sizing_note(capital, tvl, net_apy):
    """Concrete position-sizing advice for an intended deposit of `capital` USD into a pool
    with current `tvl` and expected `net_apy`. Returns None if no capital was given; otherwise
    a dict with your resulting share of the pool, the expected dollar return at the current
    rate, and a concentration warning when your share would be large enough that you'd
    dominate the pool -- your own entry/exit moves the price, and IL risk that would normally
    spread across many LPs concentrates on you instead."""
    if capital is None or capital <= 0:
        return None
    share = capital / (tvl + capital) if (tvl + capital) > 0 else 1.0
    warning = None
    if share > POOL_SHARE_WARNING_THRESHOLD:
        warning = (f"depositing ${capital:,.0f} would make you ~{share*100:.0f}% of this pool's "
                   f"${tvl:,.0f} TVL -- you'd dominate it: your own entry/exit moves the price, "
                   f"and IL risk concentrates on you instead of spreading across many LPs")
    return {"capital": capital, "share_pct": share, "dollar_return": capital * net_apy, "warning": warning}


MAX_SANE_FEE_RATE = 0.05      # 5% per swap -- generous; real fee tiers top out around 1%
MIN_SANE_TVL_USD = 5_000       # below this, a single trade can dominate the annualized apy
PEER_APY_OUTLIER_MULTIPLE = 5  # flag if > 5x the median apy of other pools on the same ticker
MIN_PROTOCOL_SECURITY_SCORE = 50  # `defi protocol-info` securityScore floor (0-100)

_PROTOCOL_ID_VERSION_RE = re.compile(r"(\d+)$")


def _protocol_carries_unaudited_hook_risk(protocol_id, protocol_name):
    """True if this protocol's version generation is 4 or later -- V4-style pluggable-hook
    architecture, i.e. custom logic outside the audited core AMM that this tool has no way to
    inspect. Checked primarily via the structured `defiProtocolId` (e.g. "uniswap4",
    "pancakeswap4"), not the display name: PancakeSwap's own V4 is marketed as "PancakeSwap
    Infinity" with no "v4"/"V4" substring anywhere in that name, so a pure name-match (which
    this replaced) silently let it through the hard block -- confirmed live, not hypothetical
    (defiProtocolId="pancakeswap4", protocolName="PancakeSwap Infinity", 3 such pools live on
    BSC at the time this was fixed). Falls back to a name-text match when defiProtocolId is
    missing or doesn't end in a version number, so an unrecognized id scheme doesn't silently
    disable the check.
    """
    if protocol_id:
        m = _PROTOCOL_ID_VERSION_RE.search(protocol_id)
        if m:
            return int(m.group(1)) >= 4
    return "v4" in (protocol_name or "").lower()


def pool_risk_flags(pool, info, peer_apys=None, protocol_security_score=None,
                     max_fee_rate=MAX_SANE_FEE_RATE, min_tvl_usd=MIN_SANE_TVL_USD,
                     peer_outlier_multiple=PEER_APY_OUTLIER_MULTIPLE,
                     min_security_score=MIN_PROTOCOL_SECURITY_SCORE,
                     block_unknown_v4_hooks=True):
    """Aggregate pre-deposit sanity/safety screen for one LP pool -- generalizes the original
    single feeRate check into independent signals for the two questions that actually matter
    before recommending a deposit: is the advertised yield even real (data-plausibility), and
    is the pool safe to put money into (deposit risk)? Returns a list of human-readable flag
    strings; empty = no flags raised. A pool can trip more than one signal.

    Data-plausibility signals (apy may not be real):
      - feeRate outside a sane per-swap range (catches the exact QQQB-USDC V4 case: a
        feeRate of 838.86%/swap produced apy=1658.77% vs 77.86% on the equivalent V3 pool).
        NOTE ON CAUSE: an extreme snapshot doesn't necessarily mean the pool is broken or
        malicious -- legitimate V4 protocols (e.g. Fables' "intelligent fees") run hooks that
        reprice the swap fee per-transaction from realized volatility, session/calendar state,
        or order flow direction, bounded and keeper-driven. A single feeRate read is then a
        live, momentary number, not a stable rate -- annualizing it as if it were static (which
        is what produces `apy`) is structurally wrong regardless of whether the hook itself is
        legitimate. Flag it as "not a static rate we can annualize," not as "probably malicious."
      - TVL below a floor where a single trade can dominate the annualized apy estimate.
      - apy is a large outlier versus other pools on the *same* underlying ticker -- this is
        the general form of the feeRate check: it catches any mechanism (stale data, a
        calculation bug, a temporary spike, a hook doing something unexpected) that produces
        an implausible apy, not just the one specific cause already identified once. `peer_apys`
        must already exclude this pool's own apy (by identity, e.g. investmentId) -- it is used
        as-is, not filtered by value, so two distinct pools that happen to share an apy don't
        wrongly exclude each other.

    Deposit-risk signals (money going in may not be safe):
      - `investable=false` -- delisted, no new deposits possible.
      - protocol-level `securityScore` (from `defi protocol-info`) below a floor.
        IMPORTANT LIMITATION: this score is per-*protocol*, not per-pool or per-hook -- Uniswap
        V3 and V4 pools both score 95.18 because it's the same organization, so it CANNOT catch
        a malicious/broken hook on an otherwise-reputable protocol (that's exactly the QQQB
        case: Uniswap's own score gives no warning). This signal is a weak floor against
        obviously disreputable protocols, not a substitute for the hook-level audit tracked in
        the README roadmap -- do not present it as though it clears a V4 pool as hook-safe.
      - `block_unknown_v4_hooks` (default True): V4-generation pools can carry an arbitrary
        custom hook -- logic outside the audited core AMM, and this product has no API access
        to a pool's hook address, permissions, or audit status (see the securityScore
        limitation above -- protocol-level score can't see it either). Per the PM/QA review,
        contract risk this unknown is a hard block by default, not just a caveat: every
        V4-generation pool is flagged until real hook-inspection data is available, not only
        ones with an already-visible symptom like an extreme feeRate. Detected primarily via
        the structured `defiProtocolId` (e.g. "uniswap4"), not the display name -- see
        `_protocol_carries_unaudited_hook_risk`. Pass False to disable for an already-vetted
        pool or explicit user override.
    """
    flags = []

    protocol_id = (pool.get("defiProtocolId") or info.get("defiProtocolId") or "")
    protocol_name = (pool.get("protocolName") or info.get("protocolName") or "")
    if block_unknown_v4_hooks and _protocol_carries_unaudited_hook_risk(protocol_id, protocol_name):
        flags.append(f"{protocol_name}: V4-generation pools can carry an arbitrary custom hook "
                      f"with unaudited logic, and this tool has no way to inspect the hook's address, "
                      f"permissions, or audit status. Blocked by default until that's available "
                      f"(pass block_unknown_v4_hooks=False / --allow-v4 to override)")

    fee_rate = info.get("feeRate")
    if fee_rate is not None:
        try:
            fee_rate = float(fee_rate)
            if fee_rate > max_fee_rate:
                flags.append(f"feeRate={fee_rate*100:.2f}%/swap exceeds a sane range (>{max_fee_rate*100:.0f}%) "
                              f"-- likely a dynamic/keeper-priced fee snapshot, not a static rate; "
                              f"annualizing it into apy is not meaningful either way")
        except (TypeError, ValueError):
            pass

    tvl = float(pool.get("tvl") or info.get("tvl") or 0)
    if tvl < min_tvl_usd:
        flags.append(f"TVL ${tvl:,.0f} is below ${min_tvl_usd:,.0f} -- apy from this little "
                      f"liquidity is statistically noisy, easily swung by a single trade")

    apy = float(info["apy"]) if info.get("apy") is not None else float(info.get("apyBps") or 0) / 10000
    if peer_apys:
        median = statistics.median(peer_apys)
        if median > 0 and apy > median * peer_outlier_multiple:
            flags.append(f"apy is {apy/median:.1f}x the median ({median*100:.1f}%) of other pools "
                          f"on the same token -- outlier, treat as unverified until explained")

    if info.get("investable") is False:
        flags.append("product is delisted (investable=false) -- no new deposits possible")

    if protocol_security_score is not None:
        try:
            score = float(protocol_security_score)
            if score < min_security_score:
                flags.append(f"protocol security score {score:.0f}/100 is below {min_security_score} "
                              f"-- elevated smart-contract risk at the protocol level")
        except (TypeError, ValueError):
            pass

    return flags


def evaluate_pool(pool, info, sigma, apy, peer_apys=None, protocol_security_score=None,
                   max_fee_rate=MAX_SANE_FEE_RATE, min_tvl_usd=MIN_SANE_TVL_USD,
                   peer_outlier_multiple=PEER_APY_OUTLIER_MULTIPLE,
                   min_security_score=MIN_PROTOCOL_SECURITY_SCORE, block_unknown_v4_hooks=True):
    """The one evaluation path for a pool with an already-resolved sigma/apy -- used by both
    `run_scan` and `rebalance-check`'s market comparison, so the two can never reach
    inconsistent safety conclusions about the same pool. (Before this existed, rebalance-check
    ran its own stripped-down check that skipped peer_apys/protocol_security_score entirely --
    a pool `scan` would flag could still show up there as a "better" rebalance target.)

    Returns {"flags": [...], "scored": {...}}. `scored` (from risk_adjusted_apy) is always
    computed, even when flags is non-empty, so a caller can show the numbers alongside a
    prominent "don't trust this" -- but never rank or recommend a pool with any flags.
    """
    flags = pool_risk_flags(pool, info, peer_apys=peer_apys, protocol_security_score=protocol_security_score,
                             max_fee_rate=max_fee_rate, min_tvl_usd=min_tvl_usd,
                             peer_outlier_multiple=peer_outlier_multiple,
                             min_security_score=min_security_score,
                             block_unknown_v4_hooks=block_unknown_v4_hooks)
    return {"flags": flags, "scored": risk_adjusted_apy(apy, sigma)}


def fetch_investment_info(investment_id):
    body = baw("defi", "investment-info", "--investmentId", investment_id)
    if not body.get("success"):
        raise RuntimeError(json.dumps(body.get("error", body)))
    return body["data"]


def fetch_protocol_security_score(defi_protocol_id, cache):
    """Cached lookup of a protocol's securityScore via `defi protocol-info`. Score is
    per-protocol (shared across e.g. Uniswap V3 and V4), so cache by defiProtocolId to
    avoid a repeat API call per pool on the same protocol."""
    if defi_protocol_id in cache:
        return cache[defi_protocol_id]
    try:
        body = baw("defi", "protocol-info", "--defiProtocolId", defi_protocol_id)
        score = body["data"].get("securityScore") if body.get("success") else None
    except Exception:
        score = None
    cache[defi_protocol_id] = score
    return score


def fetch_positions(refresh=False):
    args = ["defi", "position"] + (["--refresh"] if refresh else [])
    body = baw(*args)
    if not body.get("success"):
        raise RuntimeError(json.dumps(body.get("error", body)))
    return body["data"]


def build_stock_index(stock_tokens):
    return {(t["chainId"], t["contractAddress"].lower()): t for t in stock_tokens}


def lp_positions_on_stock_tokens(position_data, stock_index):
    """Flatten `defi position` down to LP positions whose pool includes a known stock token."""
    hits = []
    for protocol in position_data.get("deFiProtocolVOList", []):
        chain_id = protocol.get("binanceChainId")
        for pool in protocol.get("poolList", []):
            if pool.get("poolType") != "Liquidity Pool":
                continue
            for coll in pool.get("positionCollectionList", []):
                for pos in coll.get("positionList", []):
                    supply = pos.get("tokenList", {}).get("supply", [])
                    stock_tokens_here = [
                        stock_index[(chain_id, t["tokenAddress"].lower())]
                        for t in supply
                        if (chain_id, t.get("tokenAddress", "").lower()) in stock_index
                    ]
                    if stock_tokens_here:
                        hits.append({
                            "protocolName": protocol.get("protocolName"),
                            "investmentIds": pos.get("investmentIds", []),
                            "stock": stock_tokens_here[0],
                            "supply": supply,
                            "nftId": pos.get("positionDetail", {}).get("nftId"),
                        })
    return hits


def cmd_stocks(args):
    tokens = fetch_stock_tokens(type_filter=args.type)
    for t in tokens[: args.limit]:
        print(f"{t['ticker']:<8} {t['symbol']:<12} chain={t['chainId']:<3} {t['contractAddress']}")
    print(f"\n{len(tokens)} tokens total (showing {min(args.limit, len(tokens))})")


def cmd_vol(args):
    tokens = fetch_stock_tokens()
    matches = [t for t in tokens if t["ticker"].upper() == args.ticker.upper()]
    if not matches:
        print(f"no stock token found for ticker {args.ticker}", file=sys.stderr)
        sys.exit(1)
    for t in matches:
        klines = fetch_klines(t["chainId"], t["contractAddress"], limit=args.days + 1)
        sigma = best_available_volatility(klines)
        if sigma is None:
            print(f"{t['symbol']} (chain {t['chainId']}): not enough kline history")
            continue
        il = expected_il_fraction(sigma)
        if args.apy:
            grade = richness_grade(vol_richness_ratio(sigma, args.apy))
            be_str = f", Richness Score @ {args.apy*100:.0f}% APY = {grade}"
        else:
            be_str = ""
        print(f"{t['symbol']} (chain {t['chainId']}): annualized vol = {sigma*100:.2f}%, "
              f"est. full-range IL/yr = {il*100:.2f}%{be_str}")


def _classify_fetch_error(e):
    """Coarse classification of a concurrent-fetch failure (investment-info or kline), for
    unscoreable reasons and failure_summary -- so "N pools failed" doesn't hide whether that's
    a transient timeout/network blip (worth retrying the whole run) or a per-pool data problem
    (this specific pool is broken, retrying won't help)."""
    if isinstance(e, (subprocess.TimeoutExpired, TimeoutError)):
        return "timeout"
    if isinstance(e, (urllib.error.URLError, ConnectionError)):
        return "network error"
    if isinstance(e, ValueError):
        return "invalid data"
    return "error"


def _summarize_unscoreable(unscoreable):
    """Frequency count of unscoreable reasons -- lets a caller see 'what kind of problem, how
    many' at a glance (e.g. {"could not compute volatility (kline fetch failed (network error))": 5})
    instead of reading a wall of per-pool messages one at a time to notice a pattern."""
    return dict(Counter(u["reason"] for u in unscoreable))


def run_scan(max_pages=3, max_fee_rate=MAX_SANE_FEE_RATE, min_tvl=MIN_SANE_TVL_USD,
             peer_outlier_multiple=PEER_APY_OUTLIER_MULTIPLE, min_security_score=MIN_PROTOCOL_SECURITY_SCORE,
             block_unknown_v4_hooks=True, with_range=False, log=lambda msg: print(msg, file=sys.stderr)):
    """Core of `scan`, factored out so `recommend` and `rebalance_check`'s market-comparison
    side share one evaluation path instead of drifting apart (see evaluate_pool). Returns
    (results, flagged, unscoreable) sorted by net_apy descending; raises on an unrecoverable
    baw error (e.g. not signed in) -- callers decide how to present that. `unscoreable` lists
    (pool_name, reason) for candidates that were never scored at all (data fetch failed, no
    confirmed bStock, no usable kline overlap) -- distinct from `flagged`, which is pools that
    *were* scored but failed the risk/plausibility screen. Never silently drop one into the
    other: a user needs to tell "we couldn't evaluate this" apart from "we evaluated it and
    it's unsafe/implausible"."""
    log("fetching tokenized-stock list...")
    stock_tokens = fetch_stock_tokens()
    stock_index = build_stock_index(stock_tokens)
    ticker_by_symbol = {t["symbol"].lower(): t for t in stock_tokens}

    log("fetching LP pools (requires signed-in baw session)...")
    pools = fetch_lp_investments(max_pages=max_pages)

    ticker_by_ticker = {t["ticker"].lower(): t for t in stock_tokens}
    candidates = []
    for p in pools:
        name = p.get("investmentName", "")
        # widen the pre-filter net (false negatives here mean a real bStock pool is never
        # even checked): split on any of -/_ and whitespace, not just "-", and also match a
        # bare ticker (e.g. "GME") in case a pool is ever named without the "B" suffix. This
        # is only a cheap shortlist -- the authoritative check is the assetTokenList address
        # match below, so a wider net's false positives get corrected there, not compounded.
        name_tokens = [tok.lower() for tok in re.split(r"[-/_\s]+", name) if tok]
        is_candidate = any(tok in ticker_by_symbol or tok in ticker_by_ticker for tok in name_tokens)
        if is_candidate:
            candidates.append(p)

    log(f"found {len(candidates)}/{len(pools)} LP pools naming a tokenized-stock symbol")

    # Pass 1: resolve each candidate's stock/vol/apy, without deciding risk flags yet --
    # the peer-outlier check needs every ticker's full apy list first.
    #
    # Each `baw` call is a separate Node.js process spawn (~0.6s of pure process-startup
    # overhead, independent of the actual API latency) -- sequentially that's ~0.6s times
    # the candidate count, which is the dominant cost of this whole command once pagination
    # widens the candidate set. These are independent, stateless reads, so fetch them
    # concurrently instead; MAX_CONCURRENT_BAW_CALLS bounds how hard that hits the API.
    info_by_pool = {}
    info_fetch_errors = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_BAW_CALLS) as pool_executor:
        future_to_pool = {pool_executor.submit(fetch_investment_info, p["investmentId"]): p for p in candidates}
        for future in concurrent.futures.as_completed(future_to_pool):
            p = future_to_pool[future]
            try:
                info_by_pool[p["investmentId"]] = future.result()
            except Exception as e:
                info_fetch_errors[p["investmentId"]] = _classify_fetch_error(e)

    resolved = []  # (pool, info, stock, quote_addr, pair_mode) for confirmed-bStock candidates
    unscoreable = []  # (pool_name, reason) -- surfaced, never silently dropped (see run_scan docstring)
    kline_keys = set()
    for pool in candidates:
        info = info_by_pool.get(pool["investmentId"])
        if info is None:
            reason = info_fetch_errors.get(pool["investmentId"], "error")
            unscoreable.append((pool.get("investmentName"), f"investment-info fetch failed ({reason})"))
            continue
        stock, chain_id, quote_addr, pair_mode = resolve_pool_stock_and_quote(pool, info, stock_index)
        if stock is None:
            # name-matching was only ever a pre-filter guess (see the widened matcher above);
            # if the authoritative on-chain assetTokenList doesn't confirm exactly one bStock
            # paired with exactly one other asset, trusting the name guess anyway risks
            # attributing volatility/apy data to the wrong token, or applying a two-asset IL
            # model to a pool this tool can't actually model. Skip rather than silently
            # mis-score it.
            unscoreable.append((pool.get("investmentName"),
                                 "assetTokenList did not confirm exactly one bStock paired with one other asset "
                                 "(unsupported pool structure)"))
            continue
        resolved.append((pool, info, stock, chain_id, quote_addr, pair_mode))
        kline_keys.add((stock["chainId"], stock["contractAddress"]))
        if pair_mode == "non_stablecoin":
            kline_keys.add((chain_id, quote_addr))

    # Klines are plain HTTP (no subprocess spawn), but still worth fetching concurrently
    # for the same reason -- network round-trip latency, not local CPU, dominates.
    kline_cache = {}
    kline_fetch_errors = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_BAW_CALLS) as kline_executor:
        future_to_key = {
            kline_executor.submit(fetch_klines, chain_id, addr, limit=91): (chain_id, addr)
            for chain_id, addr in kline_keys
        }
        for future in concurrent.futures.as_completed(future_to_key):
            key = future_to_key[future]
            try:
                kline_cache[key] = future.result()
            except Exception as e:
                kline_cache[key] = None
                kline_fetch_errors[key] = _classify_fetch_error(e)

    prepared = []
    for pool, info, stock, chain_id, quote_addr, pair_mode in resolved:
        stock_key = (stock["chainId"], stock["contractAddress"])
        stock_klines = kline_cache.get(stock_key)
        vol_reason = None  # set below when sigma ends up None, so the unscoreable reason can
                            # distinguish "the kline fetch itself failed" from "it succeeded but
                            # the data was too thin/misaligned to trust" -- previously both
                            # collapsed into the same generic "insufficient kline history"
        if pair_mode == "stablecoin":
            # quote is a stablecoin -- its own volatility is ~0, so IL is driven almost
            # entirely by the bStock's own volatility (see README "The idea"). Yang-Zhang
            # (best_available_volatility) uses the OHLC data we already have instead of
            # close-only, for a more statistically efficient, drift-independent estimate.
            if stock_klines:
                sigma = best_available_volatility(stock_klines)
                vol_reason = None if sigma else "insufficient kline history"
            else:
                sigma = None
                vol_reason = f"kline fetch failed ({kline_fetch_errors.get(stock_key, 'error')})"
        else:
            # non-stablecoin quote (e.g. bStock/BNB): IL depends on the *relative* price move
            # between the two pooled assets, not the bStock's volatility alone. Using the
            # bStock-only volatility here would silently understate risk whenever the quote
            # asset moves too -- this was a real bug, not a simplification: NVDAB-BNB,
            # BNB-SPCXB, HOODB-BNB etc. were being scored as if BNB were a stablecoin.
            quote_key = (chain_id, quote_addr)
            quote_klines = kline_cache.get(quote_key)
            if stock_klines and quote_klines:
                rel_vol = relative_annualized_volatility(stock_klines, quote_klines)
                sigma, vol_reason = rel_vol["sigma"], rel_vol["reason"]
            elif stock_klines is None:
                sigma = None
                vol_reason = f"kline fetch failed ({kline_fetch_errors.get(stock_key, 'error')})"
            else:
                sigma = None
                vol_reason = f"quote kline fetch failed ({kline_fetch_errors.get(quote_key, 'error')})"
        if sigma is None or sigma <= 0:
            reason = vol_reason or "insufficient/no overlapping kline history"
            unscoreable.append((pool.get("investmentName"), f"could not compute volatility ({reason})"))
            continue

        # `investment-list`'s `apy` is null/0 for most concentrated (V3) LP pools;
        # `investment-info` carries the real fee-based rate as `apyBps` instead.
        if info.get("apy") is not None:
            apy = float(info["apy"])
        elif info.get("apyBps") is not None:
            apy = float(info["apyBps"]) / 10000
        else:
            apy = float(pool.get("apy") or 0)

        prepared.append({"pool": pool, "info": info, "stock": stock, "sigma": sigma, "apy": apy,
                          "pair_mode": pair_mode})

    # Pass 2: apply the risk/plausibility screen with full peer context, then score the survivors.
    # Keyed by investmentId (not just apy value) so a pool excludes only itself as a "peer" --
    # two distinct pools that happen to share an apy must not exclude each other.
    entries_by_ticker = {}
    for p in prepared:
        entries_by_ticker.setdefault(p["stock"]["ticker"], []).append((p["pool"]["investmentId"], p["apy"]))

    security_score_cache = {}
    results = []
    flagged = []
    for p in prepared:
        pool, info, stock, sigma, apy = p["pool"], p["info"], p["stock"], p["sigma"], p["apy"]
        protocol_id = pool.get("defiProtocolId") or info.get("defiProtocolId")
        security_score = fetch_protocol_security_score(protocol_id, security_score_cache) if protocol_id else None
        peer_apys = [a for inv_id, a in entries_by_ticker.get(stock["ticker"], []) if inv_id != pool["investmentId"]]
        evaluation = evaluate_pool(pool, info, sigma, apy, peer_apys=peer_apys, protocol_security_score=security_score,
                                    max_fee_rate=max_fee_rate, min_tvl_usd=min_tvl,
                                    peer_outlier_multiple=peer_outlier_multiple,
                                    min_security_score=min_security_score,
                                    block_unknown_v4_hooks=block_unknown_v4_hooks)
        if evaluation["flags"]:
            flagged.append({"investmentId": pool.get("investmentId"), "pool": pool.get("investmentName"),
                             "protocol": pool.get("protocolName"), "flags": evaluation["flags"]})
            continue

        scored = evaluation["scored"]
        result = {
            "protocol": pool.get("protocolName"),
            "pool": pool.get("investmentName"),
            "investmentId": pool.get("investmentId"),
            "stock_ticker": stock["ticker"],
            "tvl": float(pool.get("tvl") or 0),
            "grade": richness_grade(scored["vol_ratio"]),
            "pair_mode": p["pair_mode"],
            **scored,
        }
        if with_range:
            _, best = recommend_range(apy, sigma, side="straddle")
            best["confidence"] = confidence_grade(best["p_active"])
            result["best_range"] = best
        results.append(result)

    results.sort(key=lambda r: r["model_net_apy"], reverse=True)
    unscoreable_dicts = [{"pool": name, "reason": reason} for name, reason in unscoreable]
    return results, flagged, unscoreable_dicts


def cmd_scan(args):
    started = time.time()
    log = (lambda msg: None) if args.json else (lambda msg: print(msg, file=sys.stderr))
    try:
        results, flagged, unscoreable = run_scan(
            max_pages=args.max_pages, max_fee_rate=args.max_fee_rate, min_tvl=args.min_tvl,
            peer_outlier_multiple=args.peer_outlier_multiple, min_security_score=args.min_security_score,
            block_unknown_v4_hooks=not args.allow_v4, with_range=args.with_range, log=log,
        )
    except Exception as e:
        if args.json:
            print(json.dumps({"error": str(e), "hint": "run `baw auth signin` / `baw auth verify`"}, indent=2))
        else:
            print(f"could not fetch LP pools: {e}", file=sys.stderr)
            print("run `baw auth signin` / `baw auth verify` first, then re-run scan.", file=sys.stderr)
        sys.exit(1)

    capital_note = None
    if args.capital and results:
        top = results[0]
        capital_note = position_sizing_note(args.capital, top["tvl"], top["model_net_apy"])

    if args.json:
        # Pure JSON on stdout -- nothing else -- so a scheduler/pipeline can parse it directly.
        # Diagnostics (fetch progress, etc.) went to stderr above via `log`.
        print(json.dumps({
            "as_of": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.time() - started, 1),
            "results": results[: args.top],
            "flagged": flagged,
            "unscoreable": unscoreable,
            "failure_summary": _summarize_unscoreable(unscoreable),
            "capital_note": capital_note,
            "model_apy_caveat": MODEL_APY_CAVEAT,
            "v4_override_reason": args.allow_v4,
        }, indent=2))
        return

    if args.with_range:
        print(f"\n{'pool':<20}{'ticker':<8}{'apy':>9}{'vol':>9}{'grade':>7}"
              f"{'best +/-%':>10}{'range-net':>11}{'confidence':>12}{'tvl':>14}")
        for r in results[: args.top]:
            b = r["best_range"]
            width = f"{(b['pb']-1)*100:.0f}%" if b["pb"] is not None else "full"
            tag = "" if r["pair_mode"] == "stablecoin" else "  [non-stablecoin pair]"
            print(f"{r['pool']:<20}{r['stock_ticker']:<8}"
                  f"{r['apy']*100:>8.2f}%{r['sigma_annual']*100:>8.2f}%{r['grade']:>7}"
                  f"{width:>10}{b['model_net_apy']*100:>10.2f}%{b['confidence']:>12}"
                  f"{r['tvl']:>14,.0f}{tag}")
        print("\n(grade = Richness Score tier, vol_ratio bucketed Rich/Fair/Cheap; "
              "confidence = probability of the recommended range staying active a year, "
              "bucketed High/Moderate/Low. [non-stablecoin pair] = vol is the *relative* "
              "vol between the two pooled assets, not the bStock alone -- see README. "
              "Full numbers: --json.)")
    else:
        print(f"\n{'pool':<20}{'ticker':<8}{'apy':>9}{'vol':>9}{'net_apy':>10}{'grade':>7}{'tvl':>14}")
        for r in results[: args.top]:
            tag = "" if r["pair_mode"] == "stablecoin" else "  [non-stablecoin pair]"
            print(f"{r['pool']:<20}{r['stock_ticker']:<8}"
                  f"{r['apy']*100:>8.2f}%{r['sigma_annual']*100:>8.2f}%"
                  f"{r['model_net_apy']*100:>9.2f}%{r['grade']:>7}"
                  f"{r['tvl']:>14,.0f}{tag}")
        print("\n(grade = Richness Score tier -- realized vol vs. this pool's breakeven vol, "
              "bucketed Rich/Fair/Cheap. [non-stablecoin pair] = vol is the *relative* vol "
              "between the two pooled assets, not the bStock alone -- see README. Full numbers: --json.)")

    if capital_note:
        top = results[0]
        print(f"\nAt ${args.capital:,.0f} into the top pick ({top['pool']}): "
              f"~${capital_note['dollar_return']:,.0f}/yr at the current rate, "
              f"~{capital_note['share_pct']*100:.1f}% of its TVL.")
        if capital_note["warning"]:
            print(f"WARNING: {capital_note['warning']}")

    if flagged:
        print(f"\n{len(flagged)} pool(s) excluded from ranking -- pre-deposit screen flagged:")
        for f in flagged:
            print(f"  {f['pool']} ({f['protocol']}):")
            for reason in f["flags"]:
                print(f"    - {reason}")

    if unscoreable:
        print(f"\n{len(unscoreable)} pool(s) could not be evaluated at all (not the same as "
              f"'flagged' -- these were never scored, safe or not):")
        for u in unscoreable:
            print(f"  {u['pool']}: {u['reason']}")

    print(f"\n{MODEL_APY_CAVEAT}")


def cmd_range(args):
    if args.investment_id:
        try:
            info = fetch_investment_info(args.investment_id)
        except Exception as e:
            print(f"could not fetch pool {args.investment_id}: {e}", file=sys.stderr)
            print("check the investmentId is correct, or run `baw auth signin` / `baw auth verify` "
                  "if the session has expired.", file=sys.stderr)
            sys.exit(1)
        flags = pool_risk_flags({}, info, block_unknown_v4_hooks=not args.allow_v4)
        if flags:
            print(f"WARNING: {info.get('investmentName')} ({info.get('protocolName')}) failed the pre-deposit screen:", file=sys.stderr)
            for f in flags:
                print(f"  - {f}", file=sys.stderr)
            print("Proceeding anyway since an investmentId was given explicitly, but treat every "
                  "number below as unreliable -- do not recommend this pool.\n", file=sys.stderr)
        apy = float(info["apy"]) if info.get("apy") is not None else float(info.get("apyBps") or 0) / 10000
        stock_tokens = fetch_stock_tokens()
        stock_index = build_stock_index(stock_tokens)
        stock, chain_id, quote_addr, pair_mode = resolve_pool_stock_and_quote({}, info, stock_index)
        if not stock:
            print("could not confirm exactly one bStock paired with exactly one other asset in this "
                  "pool's assetTokenList (unsupported pool structure) -- cannot compute IL, aborting.",
                  file=sys.stderr)
            sys.exit(1)
        sigma = resolve_pool_volatility(stock, chain_id, quote_addr, pair_mode)
        pair_note = "" if pair_mode == "stablecoin" else " -- non-stablecoin pair, vol is relative to the quote asset"
        label = f"{info['investmentName']} ({stock['ticker']}){pair_note}"
        pool_tvl = float(info.get("tvl") or 0)
    else:
        if args.apy is None or args.ticker is None:
            print("provide --investmentId, or both --ticker and --apy", file=sys.stderr)
            sys.exit(1)
        tokens = fetch_stock_tokens()
        matches = [t for t in tokens if t["ticker"].upper() == args.ticker.upper()]
        if not matches:
            print(f"no stock token found for ticker {args.ticker}", file=sys.stderr)
            sys.exit(1)
        stock = matches[0]
        klines = fetch_klines(stock["chainId"], stock["contractAddress"], limit=91)
        sigma = best_available_volatility(klines)
        apy = args.apy
        label = f"{stock['symbol']} @ {apy*100:.2f}% pool APY (assumes a stablecoin-quoted pair)"
        pool_tvl = None  # no live pool -- no TVL to size a position against

    if sigma is None:
        print("not enough kline history to estimate volatility", file=sys.stderr)
        sys.exit(1)

    vol_ratio = vol_richness_ratio(sigma, apy)
    grade = richness_grade(vol_ratio)
    vr_str = f"{vol_ratio:.2f}" if vol_ratio is not None and math.isfinite(vol_ratio) else "n/a (0% APY)"
    print(f"{label} -- vol {sigma*100:.1f}%  |  Richness Score: {grade} (vol_ratio {vr_str})\n")

    rows, best = recommend_range(apy, sigma, side=args.side,
                                  target_offset=args.target_offset, band_width=args.band_width)
    if best["model_net_apy"] <= 0:
        print("WARNING: every candidate range nets <=0% after estimated IL -- this pool's fee "
              "income does not currently cover the token's volatility risk. 'recommended' below "
              "is the least-bad option, not a genuine opportunity.\n", file=sys.stderr)

    if best["pa"] is not None:  # a synthetic full-range row has no [pa,pb] to stress-test
        print("Scenario check on the recommended range (same [pa,pb], vol scaled -- "
              "sigma is a backward-looking estimate, this shows how much that matters):")
        for label_s, mult in [("Neutral (1x vol)", 1.0), ("Elevated (1.5x vol)", 1.5), ("Stress (2x vol)", 2.0)]:
            stressed = range_metrics(apy, sigma * mult, best["pa"], best["pb"])
            print(f"  {label_s:<22} net_apy {stressed['model_net_apy']*100:>8.2f}%   "
                  f"confidence {confidence_grade(stressed['p_active'])}")
        print()
    if args.side == "straddle":
        print(f"{'range':>10}{'concentration':>14}{'confidence':>12}{'eff.apy':>10}{'net_apy':>10}")
        for r in rows:
            width = f"+/-{(r['pb']-1)*100:.0f}%" if r["pb"] is not None else "full"
            marker = "  <- recommended" if r is best else ""
            print(f"{width:>10}{r['concentration']:>14.2f}{confidence_grade(r['p_active']):>12}"
                  f"{r['effective_apy']*100:>9.2f}%{r['model_net_apy']*100:>9.2f}%{marker}")
        print(f"\n(confidence = probability of staying in range a year, bucketed High/Moderate/Low; "
              f"recommended = best net_apy at Moderate-or-better confidence. Full numbers: --json on scan.)")
    else:
        print(f"{'offset':>10}{'band':>18}{'concentration':>14}{'confidence':>12}{'net_apy':>10}")
        for r in rows:
            offset_pct = abs((r["pa"] if args.side == "sell" else r["pb"]) - 1) * 100
            band = f"[{r['pa']:.2f}, {r['pb']:.2f}]x"
            tag = " (your target)" if r.get("is_target") else ""
            marker = "  <- recommended" if r is best else ""
            print(f"{offset_pct:>9.0f}%{band:>18}{r['concentration']:>14.2f}{confidence_grade(r['p_active']):>12}"
                  f"{r['model_net_apy']*100:>9.2f}%{marker}{tag}")
        verb = "rises into" if args.side == "sell" else "falls into"
        print(f"\n(yield-enhanced limit {'sell' if args.side == 'sell' else 'buy'} order -- earns fees "
              f"only once price {verb} the band; confidence = probability that ever happens within a year.)")
        if args.target_offset is None:
            print(f"(want an exact target instead of these presets? add "
                  f"--target-offset 0.15 for +/-15% from current price, plus optional --band-width.)")

    if args.capital:
        if pool_tvl is None:
            print(f"\n(--capital given, but this is a --ticker/--apy estimate with no live pool "
                  f"TVL to size a position against -- use --investmentId for position sizing.)")
        else:
            note = position_sizing_note(args.capital, pool_tvl, best["model_net_apy"])
            print(f"\nAt ${args.capital:,.0f} in the recommended range: "
                  f"~${note['dollar_return']:,.0f}/yr at the current rate, "
                  f"~{note['share_pct']*100:.1f}% of this pool's TVL.")
            if note["warning"]:
                print(f"WARNING: {note['warning']}")


def passes_trade_gate(result):
    """Hard gate for calling something a "Top pick": positive net_apy AND vol_ratio < 1
    (i.e. not graded Cheap). A pool can clear the pre-deposit safety screen (be in `results`
    at all) and still not be worth entering -- `results` answers "is this pool safe and
    plausible", this answers "is it actually worth doing". Before this existed, `recommend`
    would print a "Top pick" even when every candidate netted negative or graded Cheap,
    which reads as an endorsement it didn't mean to make."""
    return result["model_net_apy"] > 0 and result["vol_ratio"] is not None and result["vol_ratio"] < 1


UNSCOREABLE_RATIO_REFUSE_THRESHOLD = 0.5  # refuse a verdict when more than half the candidate
                                            # pools couldn't even be evaluated -- the scoreable
                                            # remainder may not be representative of the market


def cmd_recommend(args):
    """Single entry point: one verdict instead of deciding which of scan/range/positions to
    run. Ties together the market screen, the top pick's range recommendation, and (if any
    are held) a one-line check on existing bStock LP positions against that market."""
    try:
        results, flagged, unscoreable = run_scan(max_pages=args.max_pages, block_unknown_v4_hooks=not args.allow_v4,
                                                  with_range=True, log=lambda msg: None)
    except Exception as e:
        print(f"could not fetch LP pools: {e}", file=sys.stderr)
        print("run `baw auth signin` / `baw auth verify` first, then retry.", file=sys.stderr)
        sys.exit(1)

    total_candidates = len(results) + len(flagged) + len(unscoreable)
    if total_candidates and len(unscoreable) / total_candidates > UNSCOREABLE_RATIO_REFUSE_THRESHOLD:
        print(f"NO_TRADE -- {len(unscoreable)}/{total_candidates} candidate pools could not even be "
              f"evaluated (data/coverage issue, not a safety verdict). That's too much of the market "
              f"unaccounted for to trust a verdict from the scoreable remainder right now.")
        print(f"({_summarize_unscoreable(unscoreable)})")
        print("Try again shortly, or run `scan --with-range` to see exactly what failed.")
        return

    if not results:
        print("NO_TRADE -- no bStock LP pools passed the pre-deposit screen right now.")
        if flagged:
            print(f"({len(flagged)} pool(s) were excluded -- run `scan --with-range` for details.)")
        if unscoreable:
            print(f"({len(unscoreable)} pool(s) could not even be evaluated -- data/coverage issue, not a safety verdict.)")
        return

    tradeable = [r for r in results if passes_trade_gate(r)]
    if not tradeable:
        best = results[0]
        reasons = []
        if best["model_net_apy"] <= 0:
            reasons.append(f"best candidate ({best['pool']}) nets {best['model_net_apy']*100:.1f}% after IL -- negative")
        if best["vol_ratio"] is None or best["vol_ratio"] >= 1:
            vr_str = f"{best['vol_ratio']:.2f}" if best["vol_ratio"] is not None else "n/a"
            reasons.append(f"best candidate grades {best['grade']} (vol_ratio {vr_str}) -- "
                            f"fee income likely doesn't cover realized risk")
        print("NO_TRADE -- nothing currently clears the bar (positive net APY and vol_ratio < 1).")
        for r in reasons:
            print(f"  - {r}")
        print(f"\nClosest candidate for reference: {best['pool']} ({best['stock_ticker']}), "
              f"{best['grade']}, {best['model_net_apy']*100:.1f}% net APY. Not a recommendation.")
        print(f"\n{MODEL_APY_CAVEAT}")
        return

    top = tradeable[0]
    b = top["best_range"]
    width = f"+/-{(b['pb']-1)*100:.0f}%" if b["pb"] is not None else "full range"
    pair_note = "" if top["pair_mode"] == "stablecoin" else " (non-stablecoin pair -- vol is relative to the quote asset, see README)"
    print(f"Top pick: {top['pool']} ({top['stock_ticker']}) -- {top['grade']}, "
          f"{width} range at {b['confidence']} confidence, {b['model_net_apy']*100:.1f}% net APY.{pair_note}\n")

    print(f"{'pool':<20}{'ticker':<8}{'grade':>7}{'net_apy':>10}{'tvl':>14}")
    for r in results[:3]:
        print(f"{r['pool']:<20}{r['stock_ticker']:<8}{r['grade']:>7}{r['model_net_apy']*100:>9.2f}%{r['tvl']:>14,.0f}")

    if args.capital:
        note = position_sizing_note(args.capital, top["tvl"], top["model_net_apy"])
        print(f"\nAt ${args.capital:,.0f}: ~${note['dollar_return']:,.0f}/yr, "
              f"~{note['share_pct']*100:.1f}% of {top['pool']}'s TVL.")
        if note["warning"]:
            print(f"WARNING: {note['warning']}")

    try:
        data = fetch_positions()
        stock_tokens = fetch_stock_tokens()
        stock_index = build_stock_index(stock_tokens)
        held = lp_positions_on_stock_tokens(data, stock_index)
        portfolio_error = None
    except Exception as e:
        # A fetch failure (expired session, network error, malformed response) is not the
        # same claim as "we checked and you hold nothing" -- conflating them (as this used to
        # do by just falling back to held=[]) can tell a user they have no positions when the
        # real answer is "we don't know." Surface it distinctly instead.
        held, portfolio_error = [], str(e)

    if portfolio_error:
        print(f"\nCould not check your current bStock LP positions ({portfolio_error}) -- "
              f"this is NOT the same as confirming you hold none. Run `positions` directly to retry.")
    elif held:
        held_tickers = {h["stock"]["ticker"] for h in held}
        print(f"\nYou currently hold {len(held)} bStock LP position(s) ({', '.join(sorted(held_tickers))}). "
              f"Run `rebalance-check` to compare them against this market.")
    else:
        print(f"\nNo current bStock LP positions. See `range --investmentId {top['investmentId']}` "
              f"for the full range breakdown on the top pick, or `range --side sell/buy` to use it "
              f"as a limit order instead.")

    if flagged:
        print(f"\n({len(flagged)} pool(s) excluded by the pre-deposit screen -- "
              f"run `scan --with-range` for what and why.)")
    if unscoreable:
        print(f"({len(unscoreable)} pool(s) could not be evaluated at all -- data/coverage issue, not a safety verdict.)")

    print(f"\n{MODEL_APY_CAVEAT}")


def cmd_positions(args):
    try:
        data = fetch_positions(refresh=args.refresh)
    except Exception as e:
        if args.json:
            print(json.dumps({"error": str(e)}, indent=2))
        else:
            print(f"could not fetch positions: {e}", file=sys.stderr)
            print("run `baw auth signin` / `baw auth verify` first, then retry.", file=sys.stderr)
        sys.exit(1)
    total = float(data.get("deFiTotalValue") or 0)
    stock_tokens = fetch_stock_tokens()
    stock_index = build_stock_index(stock_tokens)
    hits = lp_positions_on_stock_tokens(data, stock_index)

    if args.json:
        print(json.dumps({
            "as_of": datetime.now(timezone.utc).isoformat(),
            "total_defi_value_usd": total,
            "positions": hits,
        }, indent=2, default=str))
        return

    print(f"total DeFi value: ${total:,.2f}\n")
    if not hits:
        print("no LP positions on tokenized-stock pairs found.")
        return
    for h in hits:
        supply_str = ", ".join(f"{t['tokenSymbol']} {t.get('tokenAmount', '?')} (${float(t.get('tokenValue') or 0):,.2f})"
                                for t in h["supply"])
        print(f"{h['protocolName']} | {h['stock']['ticker']} | nftId={h['nftId']} | {supply_str}")


REBALANCE_ATTENTION_GAP = 1.5  # fallback "needs attention" bar (vol_ratio multiple) when a dollar
                                 # switching estimate can't be computed -- see _switching_recommendation

# Rough BSC gas ballpark for a remove-liquidity + add-liquidity round trip -- a documented
# assumption, not a measured cost (this tool has no live gas-price feed). Does NOT include any
# IL realized at the moment of exit either, since there's no entry-price/cost-basis data
# available to compute that -- a known, stated gap, not hidden in the number.
ASSUMED_SWITCH_COST_USD = 2.0
SWITCH_PAYBACK_DAYS_WORTHWHILE = 30  # "switch" verdict only if the gap pays back this fast


def _best_alternative_for_ticker(ticker, market_results, exclude_investment_ids=()):
    """The best-vol_ratio market survivor for this ticker, excluding pools already held -- a
    concrete pool (protocol/tvl/apy/model_net_apy/vol_ratio/...), not just a bare ratio number,
    so rebalance-check can name an actual alternative instead of only comparing grades."""
    candidates = [r for r in market_results if r["stock_ticker"] == ticker
                  and r["investmentId"] not in exclude_investment_ids and r["vol_ratio"] is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda r: r["vol_ratio"])


def _switching_recommendation(position_usd, held_model_net_apy, alt_model_net_apy):
    """Whether switching to a concrete alternative pool is worth it, given the position's
    current USD value and the model_net_apy gap between what's held and the alternative.

    Returns {"verdict": "switch"|"stay", "reason", "annual_gap_usd", "payback_days",
    "switch_cost_usd_assumed"}. "switch" only when the estimated payback period clears
    SWITCH_PAYBACK_DAYS_WORTHWHILE -- a real per-position dollar estimate grounded in the
    position's own USD value, not a bare grade/ratio comparison, but still bounded by what this
    tool can actually measure: no live gas price, no realized-IL-at-exit data. The reason text
    says so explicitly whenever a "switch" verdict is given, so the number isn't mistaken for
    the full real-world cost of moving.
    """
    apy_gap = alt_model_net_apy - held_model_net_apy
    annual_gap_usd = position_usd * apy_gap
    if annual_gap_usd <= 0:
        return {"verdict": "stay", "reason": "the alternative doesn't actually net more after IL -- stay put",
                "annual_gap_usd": annual_gap_usd, "payback_days": None,
                "switch_cost_usd_assumed": ASSUMED_SWITCH_COST_USD}
    payback_days = ASSUMED_SWITCH_COST_USD / (annual_gap_usd / 365)
    if payback_days <= SWITCH_PAYBACK_DAYS_WORTHWHILE:
        return {"verdict": "switch",
                "reason": f"~${annual_gap_usd:,.0f}/yr gap pays back the assumed ${ASSUMED_SWITCH_COST_USD:.0f} "
                          f"switching cost in ~{payback_days:.0f} days -- doesn't include current gas price "
                          f"or any IL realized on exit",
                "annual_gap_usd": annual_gap_usd, "payback_days": payback_days,
                "switch_cost_usd_assumed": ASSUMED_SWITCH_COST_USD}
    return {"verdict": "stay",
            "reason": f"gap exists (~${annual_gap_usd:,.0f}/yr) but estimated payback "
                      f"(~{payback_days:.0f} days) is too slow to bother switching for",
            "annual_gap_usd": annual_gap_usd, "payback_days": payback_days,
            "switch_cost_usd_assumed": ASSUMED_SWITCH_COST_USD}


def _peer_apys_for_ticker(ticker, market_results, exclude_investment_id=None):
    """Peer apy values for the outlier check, drawn from the already-scanned market survivors
    on the same ticker. Narrower than run_scan's own peer set (which also includes pools that
    failed the screen, before they're excluded) since a flagged pool's apy isn't available
    here -- a documented, minor scope narrowing: it can only make the outlier check slightly
    less sensitive for this fallback path, never wrongly permissive in a way the rest of the
    screen wouldn't also independently catch."""
    return [r["apy"] for r in market_results
            if r["stock_ticker"] == ticker and r["investmentId"] != exclude_investment_id]


def _evaluate_held_investment_id(investment_id, stock_index, market_results, allow_v4):
    """Evaluate one held investmentId that fell outside the scanned market set (e.g. ranked
    outside the fetched pages), via the same evaluate_pool() path scan uses -- including
    peer_apys and protocol_security_score, so this fallback can't reach a laxer conclusion
    than scan would have for the same pool. Returns (vol_ratio, model_net_apy, flags, evaluated)
    -- the first two are None when flagged or unevaluated."""
    try:
        info = fetch_investment_info(investment_id)
        stock, chain_id, quote_addr, pair_mode = resolve_pool_stock_and_quote({}, info, stock_index)
        sigma = resolve_pool_volatility(stock, chain_id, quote_addr, pair_mode) if stock else None
        if not (sigma and sigma > 0):
            return None, None, [], False
        apy = float(info["apy"]) if info.get("apy") is not None else float(info.get("apyBps") or 0) / 10000
        peer_apys = _peer_apys_for_ticker(stock["ticker"], market_results, exclude_investment_id=investment_id)
        protocol_id = info.get("defiProtocolId")
        security_score = fetch_protocol_security_score(protocol_id, {}) if protocol_id else None
        evaluation = evaluate_pool({}, info, sigma, apy, peer_apys=peer_apys, protocol_security_score=security_score,
                                    block_unknown_v4_hooks=not allow_v4)
        if evaluation["flags"]:
            return None, None, evaluation["flags"], True
        scored = evaluation["scored"]
        return scored["vol_ratio"], scored["model_net_apy"], [], True
    except Exception:
        return None, None, [], False


def cmd_rebalance_check(args):
    started = time.time()
    log = (lambda msg: None) if args.json else (lambda msg: print(msg, file=sys.stderr))
    log("reading current positions...")
    try:
        data = fetch_positions()
    except Exception as e:
        if args.json:
            print(json.dumps({"error": str(e)}, indent=2))
        else:
            print(f"could not fetch positions: {e}", file=sys.stderr)
            print("run `baw auth signin` / `baw auth verify` first, then retry.", file=sys.stderr)
        sys.exit(1)
    stock_tokens = fetch_stock_tokens()
    stock_index = build_stock_index(stock_tokens)
    held = lp_positions_on_stock_tokens(data, stock_index)
    if not held:
        payload = {"as_of": datetime.now(timezone.utc).isoformat(), "positions": [], "any_needs_attention": False,
                   "v4_override_reason": args.allow_v4}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print("no LP positions on tokenized-stock pairs to check.")
        return

    # Same evaluation path as `scan` (run_scan -> evaluate_pool), not a separate stripped-down
    # check -- before this, rebalance-check's own market-comparison loop skipped peer_apys and
    # protocol_security_score entirely, so a pool `scan` would flag (or now, an unknown-hook V4
    # pool) could still show up here as a "better" rebalance target. One evaluation path means
    # that can't happen by construction.
    log("scanning current market for comparison (shared evaluation path with `scan`)...")
    try:
        market_results, market_flagged, _ = run_scan(max_pages=args.max_pages,
                                                       block_unknown_v4_hooks=not args.allow_v4,
                                                       with_range=False, log=lambda msg: None)
    except Exception as e:
        if args.json:
            print(json.dumps({"error": str(e)}, indent=2))
        else:
            print(f"could not fetch market pools: {e}", file=sys.stderr)
        sys.exit(1)

    market_by_id = {r["investmentId"]: r for r in market_results}
    flagged_by_id = {f["investmentId"]: f for f in market_flagged}

    if not args.json:
        print(f"\n{'held pool (ticker)':<28}{'held grade':>14}{'best market grade':>20}")
    rows = []
    for h in held:
        inv_ids = h["investmentIds"] or []
        per_id = []
        for inv_id in inv_ids:
            if inv_id in market_by_id:
                per_id.append({"investmentId": inv_id, "vol_ratio": market_by_id[inv_id]["vol_ratio"],
                                "model_net_apy": market_by_id[inv_id]["model_net_apy"],
                                "flags": [], "evaluated": True})
            elif inv_id in flagged_by_id:
                per_id.append({"investmentId": inv_id, "vol_ratio": None, "model_net_apy": None,
                                "flags": flagged_by_id[inv_id]["flags"], "evaluated": True})
            else:
                # Wasn't in the scanned market set at all (e.g. ranked outside the fetched
                # pages) -- evaluate it directly via the same evaluate_pool() path rather than
                # silently skip it.
                ratio, model_apy, flags, evaluated = _evaluate_held_investment_id(
                    inv_id, stock_index, market_results, args.allow_v4)
                per_id.append({"investmentId": inv_id, "vol_ratio": ratio, "model_net_apy": model_apy,
                                "flags": flags, "evaluated": evaluated})

        # A held "position" can carry more than one investmentId (see lp_positions_on_stock_tokens);
        # evaluating only the first silently hid risk on the rest. Report every id, and take the
        # worst case across all of them -- a flag on ANY of them means the position is flagged,
        # the vol_ratio shown is the worst (highest) among the ones that could be scored, and the
        # model_net_apy used for the switching-cost estimate below is the worst (lowest) among
        # them too -- never an average that a bad instance could hide behind a good one.
        held_flags = [f for r in per_id for f in r["flags"]]
        evaluated_ratios = [r["vol_ratio"] for r in per_id if r["vol_ratio"] is not None]
        held_ratio = max(evaluated_ratios) if evaluated_ratios else None
        evaluated_apys = [r["model_net_apy"] for r in per_id if r["model_net_apy"] is not None]
        held_model_net_apy = min(evaluated_apys) if evaluated_apys else None
        unevaluated_count = sum(1 for r in per_id if not r["evaluated"]) if inv_ids else 1

        for f in held_flags:
            log(f"  WARNING: your held {h['protocolName']} position itself: {f}")
        if unevaluated_count:
            log(f"  WARNING: {unevaluated_count}/{len(inv_ids) or 1} investmentId(s) on your held "
                f"{h['protocolName']} ({h['stock']['ticker']}) position could not be evaluated at all")

        position_usd = sum(float(t.get("tokenValue") or 0) for t in h["supply"])
        best_alt = _best_alternative_for_ticker(h["stock"]["ticker"], market_results,
                                                 exclude_investment_ids=set(inv_ids))
        best_market_ratio = best_alt["vol_ratio"] if best_alt else None
        switching = None
        if best_alt is not None and held_model_net_apy is not None:
            switching = _switching_recommendation(position_usd, held_model_net_apy, best_alt["model_net_apy"])

        # "needs attention": the held pool itself is flagged, some of it couldn't even be
        # evaluated (silence here is not reassurance), or a concrete alternative's dollar payback
        # clears the bar in _switching_recommendation. Falls back to the older bare vol_ratio-gap
        # heuristic only when a dollar estimate couldn't be computed at all (e.g. the held
        # position's own apy is unavailable) -- still better than no signal.
        if switching is not None:
            worth_switching = switching["verdict"] == "switch"
        else:
            worth_switching = (
                held_ratio is not None and best_market_ratio is not None and best_market_ratio > 0
                and held_ratio > best_market_ratio * REBALANCE_ATTENTION_GAP
            )
        needs_attention = bool(held_flags) or bool(unevaluated_count) or worth_switching

        if not args.json:
            label = f"{h['protocolName']} ({h['stock']['ticker']})"
            tag = "  <- needs attention" if needs_attention else ""
            if unevaluated_count:
                tag += f" ({unevaluated_count}/{len(inv_ids) or 1} investmentId(s) unevaluated)"
            print(f"{label:<28}{richness_grade(held_ratio):>14}{richness_grade(best_market_ratio):>20}{tag}")
            if best_alt:
                print(f"  best alternative: {best_alt['pool']} ({best_alt['protocol']}, "
                      f"${best_alt['tvl']:,.0f} TVL, {best_alt['model_net_apy']*100:.1f}% net APY)")
            if switching:
                verdict_label = "SWITCH" if switching["verdict"] == "switch" else "stay put"
                print(f"  {verdict_label}: {switching['reason']}")
        rows.append({
            "protocol": h["protocolName"], "ticker": h["stock"]["ticker"], "position_usd": position_usd,
            "held_vol_ratio": held_ratio, "held_grade": richness_grade(held_ratio),
            "held_model_net_apy": held_model_net_apy,
            "best_market_vol_ratio": best_market_ratio, "best_market_grade": richness_grade(best_market_ratio),
            "best_alternative": best_alt, "switching": switching,
            "held_flags": held_flags, "investment_ids": per_id,
            "unevaluated_count": unevaluated_count, "needs_attention": needs_attention,
        })

    if args.json:
        print(json.dumps({
            "as_of": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.time() - started, 1),
            "positions": rows,
            "any_needs_attention": any(r["needs_attention"] for r in rows),
            "v4_override_reason": args.allow_v4,
        }, indent=2))
    else:
        print("\nRecommendation only -- nothing moved. To act, use `defi redeem`/`lp-remove` then "
              "`defi deposit`/`lp-add` via binance-agentic-wallet's confirmed flow.")


def _positive_int(s):
    v = int(s)
    if v <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {v}")
    return v


def _nonneg_float(s):
    v = float(s)
    if v < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {v}")
    return v


def _apy_fraction(s):
    """APY as a decimal fraction (0.30 = 30%). Fee APY can't be negative -- unlike net_apy
    (which nets out estimated IL and legitimately can be negative), the raw fee rate itself
    is bounded below by zero. Cap the top end at a generous but finite ceiling to catch a
    stray extra zero (e.g. 300000%) rather than let it silently propagate."""
    v = float(s)
    if not (0 <= v <= 100):
        raise argparse.ArgumentTypeError(f"must be a non-negative decimal fraction, 0-100 (0.30 = 30%%), got {v}")
    return v


def _offset_fraction(s):
    """A price-ratio offset/width (e.g. --target-offset, --band-width). Must keep price
    positive (> -1) and stay within a sane range -- not literally unbounded."""
    v = float(s)
    if not (-0.99 < v < 10):
        raise argparse.ArgumentTypeError(f"must be between -0.99 and 10 (0.15 = 15%%), got {v}")
    return v


def _v4_override_reason(s):
    """--allow-v4 takes a reason, not a bare flag -- overriding a hard block that exists
    specifically because this tool cannot see hook risk should leave a record of *why* someone
    decided to do it anyway, not just that they did. Recorded verbatim in --json output
    (v4_override_reason) wherever the command produces JSON."""
    s = s.strip()
    if not s:
        raise argparse.ArgumentTypeError(
            "requires a non-empty reason, e.g. --allow-v4 \"already audited by X\"")
    return s


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_stocks = sub.add_parser("stocks", help="list tokenized-stock tokens")
    p_stocks.add_argument("--type", type=int, default=BSTOCK_TYPE,
                           help="1=Ondo, 2=xStocks, 3=bStock (default -- this product's focus); 0=all platforms")
    p_stocks.add_argument("--limit", type=int, default=20)
    p_stocks.set_defaults(func=cmd_stocks)

    p_vol = sub.add_parser("vol", help="compute annualized volatility + est. IL for one ticker")
    p_vol.add_argument("--ticker", required=True)
    p_vol.add_argument("--days", type=_positive_int, default=30)
    p_vol.add_argument("--apy", type=_apy_fraction, default=None, help="optional: also show breakeven vol at this pool APY (0.30 = 30%%)")
    p_vol.set_defaults(func=cmd_vol)

    p_scan = sub.add_parser("scan", help="rank stock-token LP pools by risk-adjusted (net of IL) APY")
    p_scan.add_argument("--top", type=_positive_int, default=15)
    p_scan.add_argument("--json", action="store_true")
    p_scan.add_argument("--capital", type=_nonneg_float, default=None,
                         help="optional: intended deposit size in USD -- shows expected $ return and "
                              "a concentration warning if you'd dominate the top pick's TVL")
    p_scan.add_argument("--max-pages", type=_positive_int, default=3,
                         help="pages of LP pools to fetch (100/page, sorted by apy DESC); higher "
                              "reaches lower-apy pools a single page would miss, at the cost of more API calls")
    p_scan.add_argument("--max-fee-rate", type=_nonneg_float, default=MAX_SANE_FEE_RATE,
                         help=f"pre-deposit screen: max sane feeRate per swap (default {MAX_SANE_FEE_RATE})")
    p_scan.add_argument("--min-tvl", type=_nonneg_float, default=MIN_SANE_TVL_USD,
                         help=f"pre-deposit screen: minimum pool TVL in USD (default {MIN_SANE_TVL_USD:.0f})")
    p_scan.add_argument("--peer-outlier-multiple", type=_nonneg_float, default=PEER_APY_OUTLIER_MULTIPLE,
                         help=f"pre-deposit screen: flag apy above this multiple of peer median (default {PEER_APY_OUTLIER_MULTIPLE})")
    p_scan.add_argument("--min-security-score", type=_nonneg_float, default=MIN_PROTOCOL_SECURITY_SCORE,
                         help=f"pre-deposit screen: minimum protocol securityScore, 0-100 (default {MIN_PROTOCOL_SECURITY_SCORE})")
    p_scan.add_argument("--allow-v4", type=_v4_override_reason, default=None, metavar="REASON",
                         help="override the V4-generation hard block (see README) for an already-vetted pool "
                              "or explicit ask -- requires a reason, recorded in --json output "
                              "(e.g. --allow-v4 \"audited by X\")")
    p_scan.add_argument("--with-range", action="store_true",
                         help="also compute the recommended concentrated-liquidity range per pool")
    p_scan.set_defaults(func=cmd_scan)

    p_range = sub.add_parser("range", help="range-by-range IL/APY breakdown + recommended range for one pool")
    p_range.add_argument("--investmentId", dest="investment_id", help="pull live apy/ticker for this pool")
    p_range.add_argument("--ticker", help="alternative to --investmentId: stock ticker")
    p_range.add_argument("--apy", type=_apy_fraction, help="alternative to --investmentId: pool APY as a decimal (0.30 = 30%%)")
    p_range.add_argument("--side", choices=["straddle", "sell", "buy"], default="straddle",
                          help="straddle=symmetric market-making (default), sell/buy=single-sided limit-order-style range")
    p_range.add_argument("--target-offset", type=_offset_fraction, default=None,
                          help="sell/buy only: exact offset from current price (0.15 = 15%%) to evaluate "
                               "in addition to the preset sweep -- for a specific target price, not just presets")
    p_range.add_argument("--band-width", type=_offset_fraction, default=SIDED_BAND_WIDTH,
                          help=f"sell/buy only: width of the --target-offset band (default {SIDED_BAND_WIDTH})")
    p_range.add_argument("--capital", type=_nonneg_float, default=None,
                          help="optional (--investmentId only): intended deposit size in USD -- shows "
                               "expected $ return and a concentration warning vs this pool's TVL")
    p_range.add_argument("--allow-v4", type=_v4_override_reason, default=None, metavar="REASON",
                          help="override the V4-generation hard block -- requires a reason "
                               "(e.g. --allow-v4 \"audited by X\"); the warning still prints either way "
                               "for an explicit --investmentId, this only affects whether the flag is raised")
    p_range.set_defaults(func=cmd_range)

    p_positions = sub.add_parser("positions", help="show current LP positions on tokenized-stock pairs")
    p_positions.add_argument("--refresh", action="store_true")
    p_positions.add_argument("--json", action="store_true")
    p_positions.set_defaults(func=cmd_positions)

    p_rebalance = sub.add_parser("rebalance-check",
                                  help="compare held stock-token LP positions against current market (report only, no execution)")
    p_rebalance.add_argument("--json", action="store_true",
                              help="machine-readable output with a needs_attention flag per position -- "
                                   "for wiring into a scheduled check, see README")
    p_rebalance.add_argument("--max-pages", type=_positive_int, default=3,
                              help="pages of the market to scan for comparison (default 3)")
    p_rebalance.add_argument("--allow-v4", type=_v4_override_reason, default=None, metavar="REASON",
                              help="override the V4-generation hard block when comparing -- requires a reason, "
                                   "recorded in --json output (e.g. --allow-v4 \"audited by X\")")
    p_rebalance.set_defaults(func=cmd_rebalance_check)

    p_recommend = sub.add_parser("recommend",
                                  help="single entry point: top pick + range + a check on any held positions, one verdict")
    p_recommend.add_argument("--max-pages", type=_positive_int, default=1,
                              help="pages of LP pools to scan (default 1, for a fast verdict; "
                                   "use `scan --max-pages` directly for the thorough sweep)")
    p_recommend.add_argument("--capital", type=_nonneg_float, default=None,
                              help="optional: intended deposit size in USD -- shows expected $ return and a concentration warning")
    p_recommend.add_argument("--allow-v4", type=_v4_override_reason, default=None, metavar="REASON",
                              help="override the V4-generation hard block -- requires a reason "
                                   "(e.g. --allow-v4 \"audited by X\")")
    p_recommend.set_defaults(func=cmd_recommend)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
