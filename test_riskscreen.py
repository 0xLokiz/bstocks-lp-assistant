"""Unit tests for the pure-math / pure-logic parts of riskscreen.py.

Deliberately excludes anything that hits the network or `baw` -- those are
covered by manual CLI smoke tests (see README "Status"). Run with:
    pytest test_riskscreen.py -v
"""

import math

import pytest

from riskscreen import (
    annualized_volatility,
    breakeven_volatility,
    concentration_multiplier,
    confidence_grade,
    expected_il_fraction,
    no_exit_probability,
    pool_risk_flags,
    range_metrics,
    recommend_range,
    richness_grade,
    vol_richness_ratio,
    _il_at_price_ratio,
    _single_barrier_touch_probability,
)


def make_klines(closes):
    """Build minimal kline rows (only the close field, index 4, is read)."""
    return [[i, "0", "0", "0", str(c), "0", i] for i, c in enumerate(closes)]


# ---- annualized_volatility ----

def test_annualized_volatility_needs_at_least_3_closes():
    assert annualized_volatility(make_klines([100, 101])) is None


def test_annualized_volatility_zero_for_constant_price():
    assert annualized_volatility(make_klines([100] * 10)) == pytest.approx(0.0, abs=1e-9)


def test_annualized_volatility_positive_for_varying_price():
    closes = [100, 105, 98, 110, 95, 103, 99, 108, 101, 97]
    sigma = annualized_volatility(make_klines(closes))
    assert sigma > 0


def test_annualized_volatility_ignores_non_positive_closes():
    # a zero/negative close (bad data) must not crash the log-return calc
    closes = [100, 0, 105, 102]
    sigma = annualized_volatility(make_klines(closes))
    assert sigma is not None and sigma >= 0


# ---- expected_il_fraction / breakeven_volatility / vol_richness_ratio ----

def test_expected_il_fraction_scales_with_variance():
    assert expected_il_fraction(0.4) == pytest.approx(0.4 ** 2 / 8)
    assert expected_il_fraction(0.0) == 0.0


def test_breakeven_volatility_inverts_expected_il():
    apy = 0.30
    sigma_star = breakeven_volatility(apy)
    # by construction, plugging sigma* back into expected_il_fraction returns apy
    assert expected_il_fraction(sigma_star) == pytest.approx(apy)


def test_breakeven_volatility_zero_for_non_positive_apy():
    assert breakeven_volatility(0) == 0.0
    assert breakeven_volatility(-0.1) == 0.0


def test_vol_richness_ratio_below_one_is_rich():
    # realized vol well under breakeven -> pool pays more than the risk implies
    ratio = vol_richness_ratio(sigma_realized=0.20, apy=1.0)
    assert ratio < 1.0


def test_vol_richness_ratio_above_one_is_cheap():
    ratio = vol_richness_ratio(sigma_realized=2.0, apy=0.05)
    assert ratio > 1.0


def test_vol_richness_ratio_infinite_at_zero_apy_with_real_vol():
    ratio = vol_richness_ratio(sigma_realized=0.3, apy=0)
    assert ratio == float("inf")


def test_vol_richness_ratio_none_when_both_zero():
    assert vol_richness_ratio(sigma_realized=0.0, apy=0) is None


# ---- richness_grade / confidence_grade ----

@pytest.mark.parametrize("ratio,expected", [
    (0.1, "Rich"),
    (0.49, "Rich"),
    (0.5, "Fair"),
    (0.99, "Fair"),
    (1.0, "Cheap"),
    (5.0, "Cheap"),
    (float("inf"), "Cheap"),
    (None, "n/a"),
])
def test_richness_grade_bands(ratio, expected):
    assert richness_grade(ratio) == expected


@pytest.mark.parametrize("p,expected", [
    (0.9, "High"),
    (0.8, "High"),
    (0.79, "Moderate"),
    (0.6, "Moderate"),
    (0.59, "Low"),
    (0.0, "Low"),
    (None, "n/a"),
])
def test_confidence_grade_bands(p, expected):
    assert confidence_grade(p) == expected


# ---- concentration_multiplier ----

def test_concentration_multiplier_full_range_is_one():
    # a very wide range approximates the unconcentrated (full-range) case
    m = concentration_multiplier(1e-6, 1e6)
    assert m == pytest.approx(1.0, abs=1e-3)


def test_concentration_multiplier_increases_as_range_narrows():
    wide = concentration_multiplier(0.5, 1.5)
    narrow = concentration_multiplier(0.9, 1.1)
    assert narrow > wide > 1.0


# ---- barrier / no-exit probability ----

def test_single_barrier_touch_probability_zero_vol_never_touches_away_barrier():
    assert _single_barrier_touch_probability(2.0, sigma_annual=0.0) == 0.0


def test_single_barrier_touch_probability_at_the_money_is_certain():
    # a barrier exactly at the current price (ratio=1) is already touched
    assert _single_barrier_touch_probability(1.0, sigma_annual=0.4) == pytest.approx(1.0)


def test_single_barrier_touch_probability_higher_vol_touches_more_often():
    low = _single_barrier_touch_probability(1.5, sigma_annual=0.2)
    high = _single_barrier_touch_probability(1.5, sigma_annual=0.8)
    assert high > low


def test_no_exit_probability_narrower_range_is_less_safe():
    wide = no_exit_probability(0.5, 1.5, sigma_annual=0.4)
    narrow = no_exit_probability(0.9, 1.1, sigma_annual=0.4)
    assert 0.0 <= narrow < wide <= 1.0


def test_no_exit_probability_never_negative():
    # a very narrow range at high vol should clamp at 0, not go negative
    assert no_exit_probability(0.99, 1.01, sigma_annual=2.0) == 0.0


# ---- range_metrics ----

def test_range_metrics_rejects_invalid_bounds():
    with pytest.raises(ValueError):
        range_metrics(0.3, 0.4, pa=1.2, pb=0.8)  # pb <= pa
    with pytest.raises(ValueError):
        range_metrics(0.3, 0.4, pa=0, pb=1.5)  # pa <= 0


def test_range_metrics_straddle_mode():
    m = range_metrics(pool_apy=0.5, sigma_annual=0.4, pa=0.8, pb=1.2)
    assert m["mode"] == "market_making"
    assert 0.0 <= m["p_active"] <= 1.0


def test_range_metrics_sell_limit_mode_above_price():
    m = range_metrics(pool_apy=0.5, sigma_annual=0.4, pa=1.1, pb=1.3)
    assert m["mode"] == "sell_limit"


def test_range_metrics_buy_limit_mode_below_price():
    m = range_metrics(pool_apy=0.5, sigma_annual=0.4, pa=0.7, pb=0.9)
    assert m["mode"] == "buy_limit"


def test_range_metrics_narrower_range_has_higher_concentration():
    wide = range_metrics(0.5, 0.4, pa=0.5, pb=1.5)
    narrow = range_metrics(0.5, 0.4, pa=0.9, pb=1.1)
    assert narrow["concentration"] > wide["concentration"]


def test_il_at_price_ratio_zero_at_no_move():
    assert _il_at_price_ratio(1.0) == pytest.approx(0.0, abs=1e-9)


def test_il_at_price_ratio_positive_away_from_one():
    assert _il_at_price_ratio(2.0) > 0
    assert _il_at_price_ratio(0.5) > 0


# ---- recommend_range ----

def test_recommend_range_straddle_includes_full_range_row():
    rows, best = recommend_range(pool_apy=0.5, sigma_annual=0.4, side="straddle")
    assert any(r["pb"] is None for r in rows)  # the synthetic full-range row
    assert best in rows


def test_recommend_range_sell_side_rows_are_all_above_price():
    rows, best = recommend_range(pool_apy=0.5, sigma_annual=0.4, side="sell")
    assert all(r["pa"] >= 1.0 for r in rows)


def test_recommend_range_buy_side_rows_are_all_below_price():
    rows, best = recommend_range(pool_apy=0.5, sigma_annual=0.4, side="buy")
    assert all(r["pb"] <= 1.0 for r in rows)


def test_recommend_range_rejects_unknown_side():
    with pytest.raises(ValueError):
        recommend_range(0.5, 0.4, side="sideways")


def test_recommend_range_falls_back_when_nothing_meets_safety_floor():
    # extremely high vol -> every candidate likely fails the confidence floor;
    # recommend_range must still return *some* best row, not crash/return None
    rows, best = recommend_range(pool_apy=0.1, sigma_annual=5.0, side="straddle")
    assert best is not None
    assert best in rows


# ---- pool_risk_flags ----

def test_pool_risk_flags_clean_pool_has_no_flags():
    pool = {"tvl": "50000"}
    info = {"feeRate": "0.003", "tvl": "50000", "apy": "0.5", "investable": True}
    assert pool_risk_flags(pool, info) == []


def test_pool_risk_flags_catches_extreme_fee_rate():
    pool = {"tvl": "271479"}
    info = {"feeRate": "8.38861", "tvl": "271479", "apyBps": "165877"}
    flags = pool_risk_flags(pool, info)
    assert any("feeRate" in f for f in flags)


def test_pool_risk_flags_catches_low_tvl():
    pool = {"tvl": "100"}
    info = {"feeRate": "0.003", "tvl": "100", "apy": "0.2"}
    flags = pool_risk_flags(pool, info)
    assert any("TVL" in f for f in flags)


def test_pool_risk_flags_catches_delisted_product():
    pool = {"tvl": "50000"}
    info = {"tvl": "50000", "apy": "0.2", "investable": False}
    flags = pool_risk_flags(pool, info)
    assert any("delisted" in f for f in flags)


def test_pool_risk_flags_catches_low_protocol_security_score():
    pool = {"tvl": "50000"}
    info = {"tvl": "50000", "apy": "0.2"}
    flags = pool_risk_flags(pool, info, protocol_security_score=30)
    assert any("security score" in f for f in flags)


def test_pool_risk_flags_catches_peer_outlier():
    pool = {"tvl": "50000"}
    info = {"tvl": "50000", "apy": "1.0"}  # 100% apy
    peers = [0.10, 0.12]  # peer median 11%, this pool is ~9x that
    flags = pool_risk_flags(pool, info, peer_apys=peers)
    assert any("outlier" in f for f in flags)


def test_pool_risk_flags_does_not_flag_within_peer_range():
    pool = {"tvl": "50000"}
    info = {"tvl": "50000", "apy": "0.15"}
    peers = [0.10, 0.12, 0.18]
    assert pool_risk_flags(pool, info, peer_apys=peers) == []


def test_pool_risk_flags_peer_check_excludes_self_by_construction():
    # regression test: peer_apys must be pre-excluded by the caller (identity,
    # not value) -- pool_risk_flags must not re-filter by apy value, so a
    # peer list that happens to still include this pool's own apy is used
    # as-is (this documents the contract, not a bug in pool_risk_flags itself)
    pool = {"tvl": "50000"}
    info = {"tvl": "50000", "apy": "0.5"}
    peers_including_self = [0.5, 0.5, 0.5]
    flags = pool_risk_flags(pool, info, peer_apys=peers_including_self)
    assert flags == []  # median == apy, not an outlier either way


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
