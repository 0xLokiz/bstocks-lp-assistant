"""The breakeven-volatility / Richness Score model: expected impermanent loss under a
diffusion approximation, the implied ("breakeven") volatility a pool's APY is pricing in, and
the vol_ratio comparison against realized volatility. See MODEL.md sections 3 and 5 for the
full derivation.
"""

import math


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


def risk_adjusted_apy(apy, sigma_annual):
    il = expected_il_fraction(sigma_annual)
    net = apy - il
    return {
        "apy": apy, "sigma_annual": sigma_annual, "expected_il": il, "model_net_apy": net,
        "vol_ratio": vol_richness_ratio(sigma_annual, apy),
    }


def _il_at_price_ratio(k):
    """Standard constant-product IL, as a positive loss fraction, at price ratio k = P1/P0."""
    return 1 - 2 * math.sqrt(k) / (1 + k)


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
