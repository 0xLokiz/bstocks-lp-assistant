"""Unit tests for the pure-math / pure-logic parts of riskscreen.py.

Deliberately excludes anything that hits the network or `baw` -- those are
covered by manual CLI smoke tests (see README "Status"). Run with:
    pytest test_riskscreen.py -v
"""

import argparse
import math
import random

import pytest

from riskscreen import (
    annualized_volatility,
    best_available_volatility,
    breakeven_volatility,
    concentration_multiplier,
    confidence_grade,
    evaluate_pool,
    expected_il_fraction,
    is_stablecoin,
    no_exit_probability,
    passes_trade_gate,
    pool_risk_flags,
    range_metrics,
    recommend_range,
    relative_annualized_volatility,
    resolve_pool_stock_and_quote,
    richness_grade,
    vol_richness_ratio,
    yang_zhang_volatility,
    _apy_fraction,
    _exact_double_barrier_no_exit_probability,
    _il_at_price_ratio,
    _nonneg_float,
    _offset_fraction,
    _peer_apys_for_ticker,
    _positive_int,
    _rogers_satchell_variance,
    _single_barrier_touch_probability,
    _union_bound_no_exit_probability,
)


def make_klines(closes):
    """Build minimal kline rows (only the close field, index 4, is read)."""
    return [[i, "0", "0", "0", str(c), "0", i] for i, c in enumerate(closes)]


def make_ohlc_klines(rows):
    """rows: list of (open, high, low, close) tuples -> full kline rows."""
    return [[i, str(o), str(h), str(l), str(c), "0", i] for i, (o, h, l, c) in enumerate(rows)]


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
    # a very narrow range at high vol should be ~0 (not literally negative) -- the exact
    # reflection-series formula correctly returns a tiny positive number here rather than
    # clamping to exactly 0 like the old union-bound approximation did
    result = no_exit_probability(0.99, 1.01, sigma_annual=2.0)
    assert 0.0 <= result < 0.001


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


# ---- is_stablecoin ----

def test_is_stablecoin_matches_known_bsc_usdt():
    assert is_stablecoin("56", "0x55d398326f99059fF775485246999027B3197955")  # mixed-case input
    assert is_stablecoin("56", "0x55d398326f99059ff775485246999027b3197955")  # lowercase


def test_is_stablecoin_false_for_bnb():
    assert not is_stablecoin("56", "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE")


def test_is_stablecoin_false_for_unknown_chain():
    assert not is_stablecoin("999", "0x55d398326f99059ff775485246999027b3197955")


# ---- relative_annualized_volatility (the non-stablecoin-pair fix) ----

DAY_MS = 24 * 60 * 60 * 1000


def make_klines_at(times, closes):
    """Kline rows with explicit open-time (ms) and close price -- for alignment/coverage tests."""
    return [[t, "0", "0", "0", str(c), "0", t] for t, c in zip(times, closes)]


def test_relative_volatility_zero_when_ratio_constant():
    # stock and quote move in lockstep -> the ratio series is flat -> zero relative vol,
    # even though each leg individually has nonzero volatility. 30 densely-spaced points to
    # clear both the sample-count and coverage floors.
    times = [i * DAY_MS for i in range(30)]
    stock = make_klines_at(times, [100 * (1.05 ** i) for i in range(30)])
    quote = make_klines_at(times, [10 * (1.05 ** i) for i in range(30)])
    result = relative_annualized_volatility(stock, quote)
    assert result["sigma"] == pytest.approx(0.0, abs=1e-6)
    assert result["sample_count"] == 30
    assert result["coverage_ratio"] == pytest.approx(1.0)
    assert result["latest_candle_at"] == times[-1]


def test_relative_volatility_positive_when_ratio_varies():
    times = [i * DAY_MS for i in range(30)]
    stock_closes = [100, 105, 98, 110, 95, 103] * 5
    stock = make_klines_at(times, stock_closes)
    quote = make_klines_at(times, [10] * 30)  # flat quote -> ratio vol == stock's own vol
    result = relative_annualized_volatility(stock, quote)
    stock_sigma = annualized_volatility(stock)
    assert result["sigma"] == pytest.approx(stock_sigma, rel=1e-9)


def test_relative_volatility_none_without_overlap():
    stock = make_klines_at(range(100, 105), [1] * 5)   # times 100..104
    quote = make_klines_at(range(200, 205), [1] * 5)   # times 200..204, no overlap
    result = relative_annualized_volatility(stock, quote)
    assert result["sigma"] is None
    assert result["sample_count"] == 0


def test_relative_volatility_uses_only_overlapping_times():
    # quote has extra early candles the stock doesn't -- must not crash, must use only the overlap
    n = 30
    times = [i * DAY_MS for i in range(n)]
    stock = make_klines_at(times, [100 * (1.02 ** i) for i in range(n)])
    extra_times = [-DAY_MS * k for k in range(1, 6)]  # 5 extra early candles stock doesn't have
    quote = make_klines_at(extra_times + times, [5] * 5 + [10 * (1.01 ** i) for i in range(n)])
    result = relative_annualized_volatility(stock, quote)
    assert result["sigma"] is not None and result["sigma"] >= 0
    assert result["sample_count"] == n


def test_relative_volatility_none_when_too_few_aligned_samples():
    n = 10  # below MIN_ALIGNED_SAMPLES
    times = [i * DAY_MS for i in range(n)]
    stock = make_klines_at(times, [100 * (1.03 ** i) for i in range(n)])
    quote = make_klines_at(times, [10] * n)
    result = relative_annualized_volatility(stock, quote)
    assert result["sigma"] is None
    assert result["sample_count"] == n
    assert "need >=" in result["reason"]


def test_relative_volatility_none_when_coverage_too_sparse():
    n = 30  # clears the sample-count floor on its own
    times = [i * DAY_MS * 10 for i in range(n)]  # 10-day gaps between aligned candles
    stock = make_klines_at(times, [100 * (1.01 ** i) for i in range(n)])
    quote = make_klines_at(times, [10] * n)
    result = relative_annualized_volatility(stock, quote)
    assert result["sigma"] is None
    assert result["coverage_ratio"] == pytest.approx(0.1, rel=0.05)
    assert "coverage" in result["reason"]


# ---- resolve_pool_stock_and_quote ----

STOCK_INDEX = {
    ("56", "0xaaa"): {"ticker": "NVDA", "symbol": "NVDAB", "chainId": "56", "contractAddress": "0xaaa"},
    ("56", "0xbbb"): {"ticker": "HOOD", "symbol": "HOODB", "chainId": "56", "contractAddress": "0xbbb"},
}


def test_resolve_pool_stock_and_quote_stablecoin_pair():
    info = {"binanceChainId": "56", "assetTokenList": [
        {"tokenAddress": "0xaaa", "tokenSymbol": "NVDAB"},
        {"tokenAddress": "0x55d398326f99059ff775485246999027b3197955", "tokenSymbol": "USDT"},
    ]}
    stock, chain_id, quote_addr, pair_mode = resolve_pool_stock_and_quote({}, info, STOCK_INDEX)
    assert stock["ticker"] == "NVDA"
    assert pair_mode == "stablecoin"


def test_resolve_pool_stock_and_quote_non_stablecoin_pair():
    # this is the exact NVDAB-BNB / BNB-SPCXB / HOODB-BNB case from the review
    info = {"binanceChainId": "56", "assetTokenList": [
        {"tokenAddress": "0xaaa", "tokenSymbol": "NVDAB"},
        {"tokenAddress": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE", "tokenSymbol": "BNB"},
    ]}
    stock, chain_id, quote_addr, pair_mode = resolve_pool_stock_and_quote({}, info, STOCK_INDEX)
    assert stock["ticker"] == "NVDA"
    assert pair_mode == "non_stablecoin"


def test_resolve_pool_stock_and_quote_no_confirmed_bstock():
    info = {"binanceChainId": "56", "assetTokenList": [
        {"tokenAddress": "0xzzz", "tokenSymbol": "RANDOM"},
        {"tokenAddress": "0x55d398326f99059ff775485246999027b3197955", "tokenSymbol": "USDT"},
    ]}
    stock, chain_id, quote_addr, pair_mode = resolve_pool_stock_and_quote({}, info, STOCK_INDEX)
    assert stock is None
    assert pair_mode == "unsupported"


def test_resolve_pool_stock_and_quote_no_second_token():
    # only 1 unique asset -- not a 2-asset pool this tool can model
    info = {"binanceChainId": "56", "assetTokenList": [{"tokenAddress": "0xaaa", "tokenSymbol": "NVDAB"}]}
    stock, chain_id, quote_addr, pair_mode = resolve_pool_stock_and_quote({}, info, STOCK_INDEX)
    assert stock is None
    assert pair_mode == "unsupported"


def test_resolve_pool_stock_and_quote_three_assets_unsupported():
    # a 3-asset weighted pool -- the two-asset E[IL] ~ sigma^2/8 model doesn't generalize to this
    info = {"binanceChainId": "56", "assetTokenList": [
        {"tokenAddress": "0xaaa", "tokenSymbol": "NVDAB"},
        {"tokenAddress": "0x55d398326f99059ff775485246999027b3197955", "tokenSymbol": "USDT"},
        {"tokenAddress": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE", "tokenSymbol": "BNB"},
    ]}
    stock, chain_id, quote_addr, pair_mode = resolve_pool_stock_and_quote({}, info, STOCK_INDEX)
    assert stock is None
    assert pair_mode == "unsupported"


def test_resolve_pool_stock_and_quote_dual_bstock_unsupported():
    # two confirmed bStocks in the same pool -- ambiguous which one is "the" stock leg
    info = {"binanceChainId": "56", "assetTokenList": [
        {"tokenAddress": "0xaaa", "tokenSymbol": "NVDAB"},
        {"tokenAddress": "0xbbb", "tokenSymbol": "HOODB"},
    ]}
    stock, chain_id, quote_addr, pair_mode = resolve_pool_stock_and_quote({}, info, STOCK_INDEX)
    assert stock is None
    assert pair_mode == "unsupported"


def test_resolve_pool_stock_and_quote_duplicate_address_unsupported():
    # same token listed twice (data glitch) -- only 1 unique asset, not a real 2-asset pool
    info = {"binanceChainId": "56", "assetTokenList": [
        {"tokenAddress": "0xaaa", "tokenSymbol": "NVDAB"},
        {"tokenAddress": "0xAAA", "tokenSymbol": "NVDAB"},
    ]}
    stock, chain_id, quote_addr, pair_mode = resolve_pool_stock_and_quote({}, info, STOCK_INDEX)
    assert stock is None
    assert pair_mode == "unsupported"


# ---- pool_risk_flags: V4 hard block ----

def test_pool_risk_flags_blocks_v4_by_default():
    pool = {"tvl": "50000", "protocolName": "Uniswap V4"}
    info = {"tvl": "50000", "apy": "0.5", "feeRate": "0.003"}
    flags = pool_risk_flags(pool, info)
    assert any("V4" in f for f in flags)


def test_pool_risk_flags_v4_block_is_case_insensitive_and_checks_info_too():
    pool = {"tvl": "50000"}
    info = {"tvl": "50000", "apy": "0.5", "feeRate": "0.003", "protocolName": "uniswap v4"}
    flags = pool_risk_flags(pool, info)
    assert any("v4" in f.lower() for f in flags)


def test_pool_risk_flags_v3_not_blocked():
    pool = {"tvl": "50000", "protocolName": "Uniswap V3"}
    info = {"tvl": "50000", "apy": "0.5", "feeRate": "0.003"}
    assert pool_risk_flags(pool, info) == []


def test_pool_risk_flags_v4_override_disables_block():
    pool = {"tvl": "50000", "protocolName": "Uniswap V4"}
    info = {"tvl": "50000", "apy": "0.5", "feeRate": "0.003"}
    flags = pool_risk_flags(pool, info, block_unknown_v4_hooks=False)
    assert flags == []


# ---- evaluate_pool ----

def test_evaluate_pool_clean_returns_no_flags_and_scored_data():
    pool = {"tvl": "50000", "protocolName": "PancakeSwap V3"}
    info = {"tvl": "50000", "apy": "0.5", "feeRate": "0.003"}
    result = evaluate_pool(pool, info, sigma=0.4, apy=0.5)
    assert result["flags"] == []
    assert result["scored"]["apy"] == 0.5
    assert "vol_ratio" in result["scored"]


def test_evaluate_pool_scored_uses_model_net_apy_key_not_net_apy():
    # the field is named model_net_apy (not net_apy) precisely so it doesn't read as a
    # promised/realized return -- see MODEL_APY_CAVEAT. Lock in the contract.
    pool = {"tvl": "50000", "protocolName": "PancakeSwap V3"}
    info = {"tvl": "50000", "apy": "0.5", "feeRate": "0.003"}
    result = evaluate_pool(pool, info, sigma=0.4, apy=0.5)
    assert "model_net_apy" in result["scored"]
    assert "net_apy" not in result["scored"]


def test_evaluate_pool_flagged_still_returns_scored_for_transparency():
    pool = {"tvl": "50000", "protocolName": "Uniswap V4"}
    info = {"tvl": "50000", "apy": "0.5", "feeRate": "0.003"}
    result = evaluate_pool(pool, info, sigma=0.4, apy=0.5)
    assert result["flags"] != []
    assert result["scored"]["apy"] == 0.5  # still computed, caller decides whether to show it


# ---- _peer_apys_for_ticker (rebalance-check's fallback path getting real peer context) ----

def test_peer_apys_for_ticker_filters_by_ticker_and_excludes_self():
    market_results = [
        {"stock_ticker": "NVDA", "investmentId": "a", "apy": 0.3},
        {"stock_ticker": "NVDA", "investmentId": "b", "apy": 0.5},
        {"stock_ticker": "TSLA", "investmentId": "c", "apy": 0.9},
    ]
    peers = _peer_apys_for_ticker("NVDA", market_results, exclude_investment_id="a")
    assert peers == [0.5]


def test_peer_apys_for_ticker_empty_when_no_other_pools_on_ticker():
    market_results = [{"stock_ticker": "NVDA", "investmentId": "a", "apy": 0.3}]
    assert _peer_apys_for_ticker("NVDA", market_results, exclude_investment_id="a") == []


# ---- passes_trade_gate (the NO_TRADE fix) ----

def test_passes_trade_gate_true_for_rich_positive_net_apy():
    result = {"model_net_apy": 0.5, "vol_ratio": 0.2}
    assert passes_trade_gate(result)


def test_passes_trade_gate_false_for_negative_net_apy():
    result = {"model_net_apy": -0.01, "vol_ratio": 0.2}
    assert not passes_trade_gate(result)


def test_passes_trade_gate_false_for_cheap_grade():
    result = {"model_net_apy": 0.1, "vol_ratio": 1.5}  # >= 1 -> Cheap
    assert not passes_trade_gate(result)


def test_passes_trade_gate_false_for_unknown_vol_ratio():
    result = {"model_net_apy": 0.1, "vol_ratio": None}
    assert not passes_trade_gate(result)


# ---- CLI argument validators ----

def test_positive_int_accepts_positive():
    assert _positive_int("3") == 3


def test_positive_int_rejects_zero_and_negative():
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("0")
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("-1")


def test_nonneg_float_accepts_zero_and_positive():
    assert _nonneg_float("0") == 0.0
    assert _nonneg_float("5.5") == 5.5


def test_nonneg_float_rejects_negative():
    with pytest.raises(argparse.ArgumentTypeError):
        _nonneg_float("-0.01")


def test_apy_fraction_rejects_negative():
    with pytest.raises(argparse.ArgumentTypeError):
        _apy_fraction("-0.1")


def test_apy_fraction_accepts_zero_and_reasonable_values():
    assert _apy_fraction("0") == 0.0
    assert _apy_fraction("0.3") == 0.3


def test_apy_fraction_rejects_absurd_magnitude():
    with pytest.raises(argparse.ArgumentTypeError):
        _apy_fraction("1000")


def test_offset_fraction_rejects_price_crushing_offset():
    with pytest.raises(argparse.ArgumentTypeError):
        _offset_fraction("-1")  # would drive price to exactly zero
    with pytest.raises(argparse.ArgumentTypeError):
        _offset_fraction("-5")


def test_offset_fraction_accepts_normal_range():
    assert _offset_fraction("0.15") == 0.15
    assert _offset_fraction("-0.5") == -0.5


# ---- Rogers-Satchell / Yang-Zhang volatility ----

def test_rogers_satchell_zero_for_flat_candle():
    # open == high == low == close -> no intraday range -> zero contribution
    assert _rogers_satchell_variance(100, 100, 100, 100) == pytest.approx(0.0, abs=1e-12)


def test_rogers_satchell_positive_for_normal_candle():
    assert _rogers_satchell_variance(o=100, h=105, l=98, c=102) > 0


def test_yang_zhang_needs_at_least_3_candles():
    assert yang_zhang_volatility(make_ohlc_klines([(100, 101, 99, 100)] * 2)) is None


def test_yang_zhang_zero_for_perfectly_flat_series():
    flat = [(100, 100, 100, 100)] * 10
    sigma = yang_zhang_volatility(make_ohlc_klines(flat))
    assert sigma == pytest.approx(0.0, abs=1e-9)


def test_yang_zhang_positive_for_varying_series():
    rows = [(100, 103, 98, 101), (101, 106, 99, 104), (104, 105, 96, 97),
            (97, 102, 95, 100), (100, 108, 99, 106), (106, 107, 100, 102)]
    sigma = yang_zhang_volatility(make_ohlc_klines(rows))
    assert sigma is not None and sigma > 0


def test_yang_zhang_falls_back_to_none_on_degenerate_ohlc():
    # a zero/negative field must not crash -- best_available_volatility handles the fallback
    rows = make_ohlc_klines([(100, 101, 99, 100), (0, 101, 99, 100), (101, 102, 100, 101)])
    assert yang_zhang_volatility(rows) is None


def test_best_available_volatility_prefers_yang_zhang_when_available():
    rows = [(100, 103, 98, 101), (101, 106, 99, 104), (104, 105, 96, 97), (97, 102, 95, 100)]
    klines = make_ohlc_klines(rows)
    assert best_available_volatility(klines) == yang_zhang_volatility(klines)


def test_best_available_volatility_falls_back_to_close_to_close():
    # degenerate OHLC (a zero field) but valid closes -- yang_zhang_volatility returns None,
    # best_available_volatility must still return the close-to-close answer, not None
    closes = [100, 105, 98, 110, 95, 103]
    bad_ohlc = [[i, "0", str(h), str(h - 1), str(c), "0", i] for i, (c, h) in enumerate(zip(closes, [c + 5 for c in closes]))]
    result = best_available_volatility(bad_ohlc)
    assert result is not None
    assert result == annualized_volatility(bad_ohlc)


# ---- Exact double-barrier probability, validated against Monte Carlo ----

def _monte_carlo_no_exit_probability(pa, pb, sigma, years=1.0, n_paths=4000, n_steps=100, seed=1234):
    """Reference implementation via direct discretized GBM path simulation (driftless, log-
    space random walk) -- ground truth to validate the closed-form reflection series against.
    Pure stdlib (random.gauss), no numpy, consistent with the rest of this project."""
    rng = random.Random(seed)
    dt = years / n_steps
    step_scale = sigma * math.sqrt(dt)
    survived = 0
    log_pa, log_pb = math.log(pa), math.log(pb)
    for _ in range(n_paths):
        log_p = 0.0
        exited = False
        for _ in range(n_steps):
            log_p += step_scale * rng.gauss(0, 1)
            if log_p <= log_pa or log_p >= log_pb:
                exited = True
                break
        if not exited:
            survived += 1
    return survived / n_paths


@pytest.mark.parametrize("pa,pb,sigma", [
    (0.8, 1.2, 0.3),
    (0.5, 1.5, 0.4),
    (0.9, 1.1, 0.5),
    (0.7, 1.3, 0.2),
])
def test_exact_double_barrier_matches_monte_carlo(pa, pb, sigma):
    exact = _exact_double_barrier_no_exit_probability(pa, pb, sigma, years=1.0)
    mc = _monte_carlo_no_exit_probability(pa, pb, sigma, years=1.0)
    # n_paths=4000 at p~0.5 has a standard error of ~0.008; 0.05 is a ~6-sigma margin,
    # generous enough to not be flaky while still catching a genuinely wrong formula
    assert abs(exact - mc) < 0.05, f"exact={exact:.4f} vs monte carlo={mc:.4f} for pa={pa} pb={pb} sigma={sigma}"


def test_exact_double_barrier_tighter_than_union_bound():
    # the exact probability must be >= the conservative union-bound approximation it replaced
    # (the union bound is a proven lower bound on the true survival probability)
    pa, pb, sigma = 0.8, 1.2, 0.4
    exact = _exact_double_barrier_no_exit_probability(pa, pb, sigma)
    bound = _union_bound_no_exit_probability(pa, pb, sigma)
    assert exact >= bound - 1e-9


def test_exact_double_barrier_bounded_zero_one():
    for sigma in [0.1, 0.5, 1.0, 2.0, 5.0]:
        p = _exact_double_barrier_no_exit_probability(0.9, 1.1, sigma)
        assert 0.0 <= p <= 1.0


def test_no_exit_probability_uses_exact_when_straddling():
    pa, pb, sigma = 0.8, 1.2, 0.4
    assert no_exit_probability(pa, pb, sigma) == pytest.approx(
        _exact_double_barrier_no_exit_probability(pa, pb, sigma))


def test_no_exit_probability_falls_back_when_not_straddling():
    # pa >= 1 -- not a valid input to the straddling reflection formula, must use the fallback
    result = no_exit_probability(1.1, 1.3, 0.4)
    assert result == pytest.approx(_union_bound_no_exit_probability(1.1, 1.3, 0.4))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
