"""Realized-volatility estimators: Yang-Zhang OHLC (stablecoin-quoted pools), close-to-close
log-return volatility (fallback and the ratio-series case), and the relative-volatility
estimator for non-stablecoin-quoted pairs.
"""

import math

from bstocks_lp import config, market_data

MIN_ALIGNED_SAMPLES = 30
MIN_ALIGNED_COVERAGE_RATIO = 0.8

INTERVAL_TO_MS = {
    "1d": 24 * 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1h": 60 * 60 * 1000,
}


def _log_return_volatility(values, interval="1d"):
    """Shared core: annualized stdev of log-returns of a price-like series."""
    if len(values) < 3:
        return None
    log_returns = [math.log(values[i] / values[i - 1]) for i in range(1, len(values))]
    n = len(log_returns)
    mean = sum(log_returns) / n
    variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
    periods_per_year = config.INTERVAL_TO_ANNUALIZATION.get(interval, config.DAYS_PER_YEAR)
    return math.sqrt(variance * periods_per_year)


def annualized_volatility(klines, interval="1d"):
    closes = [float(c[4]) for c in klines if float(c[4]) > 0]
    return _log_return_volatility(closes, interval)


def _rogers_satchell_variance(o, h, lo, c):
    """Per-candle Rogers-Satchell variance term -- drift-independent (Rogers & Satchell, 1991)."""
    return math.log(h / c) * math.log(h / o) + math.log(lo / c) * math.log(lo / o)


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
    periods_per_year = config.INTERVAL_TO_ANNUALIZATION.get(interval, config.DAYS_PER_YEAR)
    return math.sqrt(var_yz * periods_per_year)


def best_available_volatility(klines, interval="1d"):
    """Yang-Zhang when the OHLC data supports it, falling back to close-to-close otherwise --
    the single volatility function callers should reach for on a stablecoin-quoted pool."""
    yz = yang_zhang_volatility(klines, interval)
    return yz if yz is not None else annualized_volatility(klines, interval)


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


def resolve_pool_volatility(stock, chain_id, quote_addr, pair_mode):
    """Sequential, single-pool volatility resolution -- for use where only one pool needs
    evaluating (e.g. rebalance-check's fallback), where run_scan's Pass 1 concurrent-batch
    machinery would be overkill for just one. Returns sigma, or None if it couldn't be computed."""
    if pair_mode == "unsupported" or stock is None:
        return None
    stock_klines = market_data.fetch_klines(stock["chainId"], stock["contractAddress"], limit=91)
    if pair_mode == "stablecoin":
        return best_available_volatility(stock_klines)
    quote_klines = market_data.fetch_klines(chain_id, quote_addr, limit=91)
    return relative_annualized_volatility(stock_klines, quote_klines)["sigma"]
