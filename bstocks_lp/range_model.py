"""Concentrated-liquidity range model: the Uniswap V3 concentration multiplier, exact
stay-in-range / touch probabilities for a driftless lognormal price (double-barrier reflection
series and single-barrier reflection principle), range-adjusted yield/IL, and the range
recommender that sweeps a preset set of candidate widths/offsets. See MODEL.md section 6.
"""

import math

from bstocks_lp import il_model


def concentration_multiplier(pa, pb):
    """Capital-efficiency multiplier of range [pa, pb] (price ratios to current price) vs
    full range -- Uniswap V3 math. Works for any 0 < pa < pb, straddling the current price
    (pa < 1 < pb) or entirely on one side of it (pa >= 1 or pb <= 1)."""
    denom = 1 - math.sqrt(pa / pb)
    return 1 / denom if denom > 1e-9 else float("inf")


def _normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


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
    il_boundary = max(il_model._il_at_price_ratio(pa), il_model._il_at_price_ratio(pb))
    expected_il = min(il_diffusion, il_boundary)
    net_apy = effective_apy - expected_il
    return {
        "pa": pa, "pb": pb, "mode": mode, "concentration": m, "p_active": p_active,
        "effective_apy": effective_apy, "expected_il": expected_il, "model_net_apy": net_apy,
        "vol_ratio": il_model.vol_richness_ratio(sigma_annual, pool_apy),
    }


DEFAULT_STRADDLE_WIDTHS = [0.05, 0.10, 0.20, 0.30, 0.50, 0.90]
DEFAULT_SIDED_OFFSETS = [0.05, 0.10, 0.20, 0.30, 0.50]
SIDED_BAND_WIDTH = 0.10
SAFETY_P_ACTIVE_FLOOR = 0.6


def confidence_grade(p_active):
    """Qualitative tier for p_active (stay-in-range / execution probability)."""
    if p_active is None:
        return "n/a"
    if p_active >= 0.8:
        return "High"
    if p_active >= SAFETY_P_ACTIVE_FLOOR:
        return "Moderate"
    return "Low"


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
        il = il_model.expected_il_fraction(sigma_annual)
        rows.append({
            "pa": None, "pb": None, "mode": "market_making", "concentration": 1.0,
            "p_active": 1.0, "effective_apy": pool_apy, "expected_il": il,
            "model_net_apy": (pool_apy - il) if il is not None else None,
            "vol_ratio": il_model.vol_richness_ratio(sigma_annual, pool_apy),
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
    # A row with model_net_apy=None (only possible for the straddle full-range row, at
    # volatility beyond where expected_il_fraction can produce a valid estimate) can never be
    # "recommended" -- there's nothing to rank it by. The concentrated rows never hit this (they
    # fall back to the exact boundary IL, valid at any volatility -- see range_metrics), so
    # excluding None rows here still leaves a real candidate to pick from in every case this
    # function is actually called with.
    rankable = [r for r in rows if r["model_net_apy"] is not None]
    safe = [r for r in rankable if r["p_active"] >= SAFETY_P_ACTIVE_FLOOR]
    best = max(safe or rankable, key=lambda r: r["model_net_apy"])
    return rows, best
