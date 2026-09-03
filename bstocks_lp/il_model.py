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

    Returns None once the formula's own output would reach or exceed 1.0 (100%), i.e. past
    sigma = sqrt(8) ~= 283% annualized -- rather than silently clamping to 1.0. A first version
    of this function did clamp: true IL asymptotically approaches but never reaches 100% even in
    the limit of an infinitely wide range (see _il_at_price_ratio, whose exact closed-form value
    tends to 1.0 as k -> 0 or k -> infinity, never past it), so 1.0 looked like a defensible
    ceiling. But the diffusion formula is only a small-sigma Taylor approximation, and past this
    threshold it hasn't just hit a ceiling -- it has left the regime it was ever derived to
    describe, so *any* single number here (including a "safe-looking" 1.0) is false precision
    dressed up as an estimate. Returning None and letting the caller say so explicitly is more
    honest than presenting a guess. Confirmed live, not hypothetical: a real pool (a Trump Media
    stock token, sigma ~= 310% annualized -- DJT is genuinely that volatile) hit this. Callers
    that can (range_metrics(), for a concrete finite range) fall back to the exact boundary IL
    instead, which stays valid at any volatility -- see range_model.range_metrics.
    """
    diffusion = (sigma_annual ** 2) / 8
    return diffusion if diffusion < 1.0 else None


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
    """`expected_il`/`model_net_apy` are None when expected_il_fraction can't produce a valid
    estimate at this volatility (see its docstring) -- `vol_ratio` is unaffected either way,
    since it's a function of sigma/apy alone, not of the IL estimate."""
    il = expected_il_fraction(sigma_annual)
    net = apy - il if il is not None else None
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
# split, no as-of timestamp, no trading-volume figure, and no lockup/redemption/incentive-expiry
# data available through this API on any pool sampled -- so this is a documented
# data-availability limit, not an oversight. Without a volume figure or a time window, this tool
# has no way to tell a durable fee rate apart from one a single large trade or a brief volume
# spike happened to produce right before this snapshot was taken -- confirmed live: the same
# pool's TVL and apy both swung sharply (TVL nearly halved, apy roughly tripled) between two
# checks minutes apart, consistent with the same fee revenue landing on much less liquidity, not
# necessarily a durable change. An incentive-heavy apy can also look attractive right up until
# the incentive program ends, with nothing in this tool able to see either coming.
#
# Independent corroboration, not just our own inference: Fables (fables.fi/docs), a live
# Uniswap-v4-hook exchange, documents its own headline "Pool swap APR" the exact same way this
# tool suspected -- the latest complete 24-hour fee window annualized against current pool TVL
# (fables.fi/docs/methodology) -- and its own APR page states plainly that this figure is not a
# forecast of future results and does not net out the value difference from simply holding the
# original assets (fables.fi/docs/apr, fables.fi/docs/price-moves). That second point is exactly
# why model_net_apy exists here: it already subtracts modeled IL from the raw platform apy, which
# a bare platform-reported apy figure (Fables' or otherwise) does not do on its own -- but the
# subtraction is still a model, not a settled result, so the same caveat applies to it too.
MODEL_APY_CAVEAT = ("model_net_apy is a model estimate (platform apy minus modeled IL), not a "
                     "promised or historical return. The platform apy itself is a single blended "
                     "fee+incentive figure with no breakdown, timestamp, trading-volume figure, or "
                     "lockup/expiry data available from the API -- it can swing sharply on a TVL "
                     "change, a single large trade, or an incentive program starting/ending, with "
                     "no way from this data alone to tell a durable rate from a transient spike "
                     "(an unrelated live protocol, Fables, documents annualizing the same way and "
                     "warns its own APR figure isn't a forecast -- see fables.fi/docs/apr).")
