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
import re
import statistics
import subprocess
import sys
import time
import urllib.request

MAX_CONCURRENT_BAW_CALLS = 8  # bounds parallel `baw`/kline fetches -- see run_scan's Pass 1

RWA_LIST_URL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/stock/detail/list/ai"
KLINE_URL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/kline/ai"
UA = "binance-web3/1.1 (Skill)"

DAYS_PER_YEAR = 365


def _get(url, params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(f"{url}?{query}", headers={"Accept-Encoding": "identity", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


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


def annualized_volatility(klines, interval="1d"):
    closes = [float(c[4]) for c in klines if float(c[4]) > 0]
    if len(closes) < 3:
        return None
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(log_returns)
    mean = sum(log_returns) / n
    variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
    periods_per_year = INTERVAL_TO_ANNUALIZATION.get(interval, DAYS_PER_YEAR)
    return math.sqrt(variance * periods_per_year)


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
        "apy": apy, "sigma_annual": sigma_annual, "expected_il": il, "net_apy": net,
        "vol_ratio": vol_richness_ratio(sigma_annual, apy),
    }


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


def no_exit_probability(pa, pb, sigma_annual, years=1.0):
    """Conservative probability that price stays within [pa, pb] for the *entire* period
    (not just the endpoint), via a first-order union-bound approximation:
    1 - P(touch pa) - P(touch pb). Exact double-barrier first-passage needs an infinite
    reflection series; this approximation is safe-direction (slightly understates the true
    no-exit probability) as long as the two barriers aren't very close together -- appropriate
    for a "safety floor" metric, where understating safety is the conservative error to make.
    """
    p_low = _single_barrier_touch_probability(pa, sigma_annual, years)
    p_high = _single_barrier_touch_probability(pb, sigma_annual, years)
    return max(0.0, 1 - p_low - p_high)


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
        "effective_apy": effective_apy, "expected_il": expected_il, "net_apy": net_apy,
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
            "net_apy": pool_apy - il, "vol_ratio": vol_richness_ratio(sigma_annual, pool_apy),
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
    best = max(safe or rows, key=lambda r: r["net_apy"])
    return rows, best


def baw(*args):
    """Shell out to the `baw` CLI and parse its --json output.

    On Windows this goes through cmd.exe (shell=True is required to resolve baw.cmd on
    PATH), whose active codepage defaults to the system locale -- GBK/936 on Chinese
    Windows -- rather than UTF-8. `baw` (Node) itself writes UTF-8, so a codepage mismatch
    at the cmd.exe layer can corrupt any non-ASCII text (pool/company names, error messages)
    into mojibake even though decoding succeeds without raising an error. `chcp 65001` forces
    the spawned shell into UTF-8 before running the real command, closing that gap.
    """
    if os.name == "nt":
        cmd = "chcp 65001>nul & " + subprocess.list2cmdline(["baw", *args, "--json"])
    else:
        cmd = ["baw", *args, "--json"]
    result = subprocess.run(cmd, capture_output=True, timeout=30, shell=(os.name == "nt"))
    result.stdout = result.stdout.decode("utf-8", errors="replace")
    result.stderr = result.stderr.decode("utf-8", errors="replace")
    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError(f"baw {' '.join(args)} produced no output (stderr: {result.stderr.strip()})")
    first_brace = stdout.find("{")
    return json.loads(stdout[first_brace:] if first_brace > 0 else stdout)


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


def pool_risk_flags(pool, info, peer_apys=None, protocol_security_score=None,
                     max_fee_rate=MAX_SANE_FEE_RATE, min_tvl_usd=MIN_SANE_TVL_USD,
                     peer_outlier_multiple=PEER_APY_OUTLIER_MULTIPLE,
                     min_security_score=MIN_PROTOCOL_SECURITY_SCORE):
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
    """
    flags = []

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
        sigma = annualized_volatility(klines)
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


def run_scan(max_pages=3, max_fee_rate=MAX_SANE_FEE_RATE, min_tvl=MIN_SANE_TVL_USD,
             peer_outlier_multiple=PEER_APY_OUTLIER_MULTIPLE, min_security_score=MIN_PROTOCOL_SECURITY_SCORE,
             with_range=False, log=lambda msg: print(msg, file=sys.stderr)):
    """Core of `scan`, factored out so `recommend` can reuse it without going through argparse.
    Returns (results, flagged) sorted by net_apy descending; raises on an unrecoverable baw
    error (e.g. not signed in) -- callers decide how to present that."""
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
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_BAW_CALLS) as pool_executor:
        future_to_pool = {pool_executor.submit(fetch_investment_info, p["investmentId"]): p for p in candidates}
        for future in concurrent.futures.as_completed(future_to_pool):
            p = future_to_pool[future]
            try:
                info_by_pool[p["investmentId"]] = future.result()
            except Exception:
                pass

    resolved = []  # (pool, info, stock) for candidates whose assetTokenList confirms a bStock
    unique_stock_keys = set()
    for pool in candidates:
        info = info_by_pool.get(pool["investmentId"])
        if info is None:
            continue
        chain_id = pool.get("binanceChainId") or info.get("binanceChainId")
        asset_list = info.get("assetTokenList") or []
        stock = next(
            (stock_index[(chain_id, a["tokenAddress"].lower())]
             for a in asset_list if (chain_id, a["tokenAddress"].lower()) in stock_index),
            None,
        )
        if stock is None:
            # name-matching was only ever a pre-filter guess (see the widened matcher above);
            # if the authoritative on-chain assetTokenList doesn't confirm a bStock in this
            # pool, trusting the name guess anyway risks attributing volatility/apy data to
            # the wrong token entirely. Skip rather than silently mis-score the pool.
            continue
        resolved.append((pool, info, stock))
        unique_stock_keys.add((stock["chainId"], stock["contractAddress"]))

    # Klines are plain HTTP (no subprocess spawn), but still worth fetching concurrently
    # for the same reason -- network round-trip latency, not local CPU, dominates.
    vol_cache = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_BAW_CALLS) as kline_executor:
        future_to_key = {
            kline_executor.submit(fetch_klines, chain_id, addr, limit=91): (chain_id, addr)
            for chain_id, addr in unique_stock_keys
        }
        for future in concurrent.futures.as_completed(future_to_key):
            key = future_to_key[future]
            try:
                vol_cache[key] = annualized_volatility(future.result())
            except Exception:
                vol_cache[key] = None

    prepared = []
    for pool, info, stock in resolved:
        key = (stock["chainId"], stock["contractAddress"])
        sigma = vol_cache.get(key)
        if sigma is None or sigma <= 0:
            continue

        # `investment-list`'s `apy` is null/0 for most concentrated (V3) LP pools;
        # `investment-info` carries the real fee-based rate as `apyBps` instead.
        if info.get("apy") is not None:
            apy = float(info["apy"])
        elif info.get("apyBps") is not None:
            apy = float(info["apyBps"]) / 10000
        else:
            apy = float(pool.get("apy") or 0)

        prepared.append({"pool": pool, "info": info, "stock": stock, "sigma": sigma, "apy": apy})

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
        flags = pool_risk_flags(pool, info, peer_apys=peer_apys, protocol_security_score=security_score,
                                 max_fee_rate=max_fee_rate, min_tvl_usd=min_tvl,
                                 peer_outlier_multiple=peer_outlier_multiple,
                                 min_security_score=min_security_score)
        if flags:
            flagged.append((pool.get("investmentName"), pool.get("protocolName"), flags))
            continue

        scored = risk_adjusted_apy(apy, sigma)
        result = {
            "protocol": pool.get("protocolName"),
            "pool": pool.get("investmentName"),
            "investmentId": pool.get("investmentId"),
            "stock_ticker": stock["ticker"],
            "tvl": float(pool.get("tvl") or 0),
            "grade": richness_grade(scored["vol_ratio"]),
            **scored,
        }
        if with_range:
            _, best = recommend_range(apy, sigma, side="straddle")
            best["confidence"] = confidence_grade(best["p_active"])
            result["best_range"] = best
        results.append(result)

    results.sort(key=lambda r: r["net_apy"], reverse=True)
    return results, flagged


def cmd_scan(args):
    try:
        results, flagged = run_scan(
            max_pages=args.max_pages, max_fee_rate=args.max_fee_rate, min_tvl=args.min_tvl,
            peer_outlier_multiple=args.peer_outlier_multiple, min_security_score=args.min_security_score,
            with_range=args.with_range,
        )
    except Exception as e:
        print(f"could not fetch LP pools: {e}", file=sys.stderr)
        print("run `baw auth signin` / `baw auth verify` first, then re-run scan.", file=sys.stderr)
        sys.exit(1)

    if args.with_range:
        print(f"\n{'pool':<20}{'ticker':<8}{'apy':>9}{'vol':>9}{'grade':>7}"
              f"{'best +/-%':>10}{'range-net':>11}{'confidence':>12}{'tvl':>14}")
        for r in results[: args.top]:
            b = r["best_range"]
            width = f"{(b['pb']-1)*100:.0f}%" if b["pb"] is not None else "full"
            print(f"{r['pool']:<20}{r['stock_ticker']:<8}"
                  f"{r['apy']*100:>8.2f}%{r['sigma_annual']*100:>8.2f}%{r['grade']:>7}"
                  f"{width:>10}{b['net_apy']*100:>10.2f}%{b['confidence']:>12}"
                  f"{r['tvl']:>14,.0f}")
        print("\n(grade = Richness Score tier, vol_ratio bucketed Rich/Fair/Cheap; "
              "confidence = probability of the recommended range staying active a year, "
              "bucketed High/Moderate/Low. Full numbers: --json.)")
    else:
        print(f"\n{'pool':<20}{'ticker':<8}{'apy':>9}{'vol':>9}{'net_apy':>10}{'grade':>7}{'tvl':>14}")
        for r in results[: args.top]:
            print(f"{r['pool']:<20}{r['stock_ticker']:<8}"
                  f"{r['apy']*100:>8.2f}%{r['sigma_annual']*100:>8.2f}%"
                  f"{r['net_apy']*100:>9.2f}%{r['grade']:>7}"
                  f"{r['tvl']:>14,.0f}")
        print("\n(grade = Richness Score tier -- realized vol vs. this pool's breakeven vol, "
              "bucketed Rich/Fair/Cheap. Full numbers: --json.)")

    if args.capital and results:
        top = results[0]
        note = position_sizing_note(args.capital, top["tvl"], top["net_apy"])
        print(f"\nAt ${args.capital:,.0f} into the top pick ({top['pool']}): "
              f"~${note['dollar_return']:,.0f}/yr at the current rate, "
              f"~{note['share_pct']*100:.1f}% of its TVL.")
        if note["warning"]:
            print(f"WARNING: {note['warning']}")

    if flagged:
        print(f"\n{len(flagged)} pool(s) excluded from ranking -- pre-deposit screen flagged:")
        for name, protocol, flags in flagged:
            print(f"  {name} ({protocol}):")
            for f in flags:
                print(f"    - {f}")

    if args.json:
        print(json.dumps(results[: args.top], indent=2))


def cmd_range(args):
    if args.investment_id:
        try:
            info = fetch_investment_info(args.investment_id)
        except Exception as e:
            print(f"could not fetch pool {args.investment_id}: {e}", file=sys.stderr)
            print("check the investmentId is correct, or run `baw auth signin` / `baw auth verify` "
                  "if the session has expired.", file=sys.stderr)
            sys.exit(1)
        flags = pool_risk_flags({}, info)
        if flags:
            print(f"WARNING: {info.get('investmentName')} ({info.get('protocolName')}) failed the pre-deposit screen:", file=sys.stderr)
            for f in flags:
                print(f"  - {f}", file=sys.stderr)
            print("Proceeding anyway since an investmentId was given explicitly, but treat every "
                  "number below as unreliable -- do not recommend this pool.\n", file=sys.stderr)
        apy = float(info["apy"]) if info.get("apy") is not None else float(info.get("apyBps") or 0) / 10000
        asset_list = info.get("assetTokenList") or []
        chain_id = info.get("binanceChainId")
        stock_tokens = fetch_stock_tokens()
        stock_index = build_stock_index(stock_tokens)
        stock = next((stock_index[(chain_id, a["tokenAddress"].lower())]
                      for a in asset_list if (chain_id, a["tokenAddress"].lower()) in stock_index), None)
        if not stock:
            print("could not identify a tokenized-stock token in this pool's assetTokenList", file=sys.stderr)
            sys.exit(1)
        klines = fetch_klines(stock["chainId"], stock["contractAddress"], limit=91)
        sigma = annualized_volatility(klines)
        label = f"{info['investmentName']} ({stock['ticker']})"
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
        sigma = annualized_volatility(klines)
        apy = args.apy
        label = f"{stock['symbol']} @ {apy*100:.2f}% pool APY"
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
    if best["net_apy"] <= 0:
        print("WARNING: every candidate range nets <=0% after estimated IL -- this pool's fee "
              "income does not currently cover the token's volatility risk. 'recommended' below "
              "is the least-bad option, not a genuine opportunity.\n", file=sys.stderr)
    if args.side == "straddle":
        print(f"{'range':>10}{'concentration':>14}{'confidence':>12}{'eff.apy':>10}{'net_apy':>10}")
        for r in rows:
            width = f"+/-{(r['pb']-1)*100:.0f}%" if r["pb"] is not None else "full"
            marker = "  <- recommended" if r is best else ""
            print(f"{width:>10}{r['concentration']:>14.2f}{confidence_grade(r['p_active']):>12}"
                  f"{r['effective_apy']*100:>9.2f}%{r['net_apy']*100:>9.2f}%{marker}")
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
                  f"{r['net_apy']*100:>9.2f}%{marker}{tag}")
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
            note = position_sizing_note(args.capital, pool_tvl, best["net_apy"])
            print(f"\nAt ${args.capital:,.0f} in the recommended range: "
                  f"~${note['dollar_return']:,.0f}/yr at the current rate, "
                  f"~{note['share_pct']*100:.1f}% of this pool's TVL.")
            if note["warning"]:
                print(f"WARNING: {note['warning']}")


def cmd_recommend(args):
    """Single entry point: one verdict instead of deciding which of scan/range/positions to
    run. Ties together the market screen, the top pick's range recommendation, and (if any
    are held) a one-line check on existing bStock LP positions against that market."""
    try:
        results, flagged = run_scan(max_pages=args.max_pages, with_range=True, log=lambda msg: None)
    except Exception as e:
        print(f"could not fetch LP pools: {e}", file=sys.stderr)
        print("run `baw auth signin` / `baw auth verify` first, then retry.", file=sys.stderr)
        sys.exit(1)

    if not results:
        print("no bStock LP pools passed the pre-deposit screen right now.")
        if flagged:
            print(f"({len(flagged)} pool(s) were excluded -- run `scan --with-range` for details.)")
        return

    top = results[0]
    b = top["best_range"]
    width = f"+/-{(b['pb']-1)*100:.0f}%" if b["pb"] is not None else "full range"
    print(f"Top pick: {top['pool']} ({top['stock_ticker']}) -- {top['grade']}, "
          f"{width} range at {b['confidence']} confidence, {b['net_apy']*100:.1f}% net APY.\n")

    print(f"{'pool':<20}{'ticker':<8}{'grade':>7}{'net_apy':>10}{'tvl':>14}")
    for r in results[:3]:
        print(f"{r['pool']:<20}{r['stock_ticker']:<8}{r['grade']:>7}{r['net_apy']*100:>9.2f}%{r['tvl']:>14,.0f}")

    if args.capital:
        note = position_sizing_note(args.capital, top["tvl"], top["net_apy"])
        print(f"\nAt ${args.capital:,.0f}: ~${note['dollar_return']:,.0f}/yr, "
              f"~{note['share_pct']*100:.1f}% of {top['pool']}'s TVL.")
        if note["warning"]:
            print(f"WARNING: {note['warning']}")

    try:
        data = fetch_positions()
        stock_tokens = fetch_stock_tokens()
        stock_index = build_stock_index(stock_tokens)
        held = lp_positions_on_stock_tokens(data, stock_index)
    except Exception:
        held = []

    if held:
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


def cmd_positions(args):
    try:
        data = fetch_positions(refresh=args.refresh)
    except Exception as e:
        print(f"could not fetch positions: {e}", file=sys.stderr)
        print("run `baw auth signin` / `baw auth verify` first, then retry.", file=sys.stderr)
        sys.exit(1)
    total = float(data.get("deFiTotalValue") or 0)
    print(f"total DeFi value: ${total:,.2f}\n")
    stock_tokens = fetch_stock_tokens()
    stock_index = build_stock_index(stock_tokens)
    hits = lp_positions_on_stock_tokens(data, stock_index)
    if not hits:
        print("no LP positions on tokenized-stock pairs found.")
        return
    for h in hits:
        supply_str = ", ".join(f"{t['tokenSymbol']} {t.get('tokenAmount', '?')} (${float(t.get('tokenValue') or 0):,.2f})"
                                for t in h["supply"])
        print(f"{h['protocolName']} | {h['stock']['ticker']} | nftId={h['nftId']} | {supply_str}")
    if args.json:
        print(json.dumps(hits, indent=2, default=str))


REBALANCE_ATTENTION_GAP = 1.5  # flag "needs attention" if held vol_ratio is >1.5x the best market alternative


def cmd_rebalance_check(args):
    print("reading current positions...", file=sys.stderr)
    try:
        data = fetch_positions()
    except Exception as e:
        print(f"could not fetch positions: {e}", file=sys.stderr)
        print("run `baw auth signin` / `baw auth verify` first, then retry.", file=sys.stderr)
        sys.exit(1)
    stock_tokens = fetch_stock_tokens()
    stock_index = build_stock_index(stock_tokens)
    held = lp_positions_on_stock_tokens(data, stock_index)
    if not held:
        print("no LP positions on tokenized-stock pairs to check.")
        if args.json:
            print(json.dumps({"positions": [], "any_needs_attention": False}, indent=2))
        return

    print("scanning current market for comparison...", file=sys.stderr)
    try:
        market = fetch_lp_investments()
    except Exception as e:
        print(f"could not fetch market pools: {e}", file=sys.stderr)
        sys.exit(1)
    market_by_id = {m["investmentId"]: m for m in market}

    print(f"\n{'held pool (ticker)':<28}{'held grade':>14}{'best market grade':>20}")
    rows = []
    for h in held:
        inv_ids = h["investmentIds"] or []
        held_ratio = None
        held_flags = []
        for inv_id in inv_ids:
            m = market_by_id.get(inv_id)
            if not m:
                continue
            try:
                info = fetch_investment_info(inv_id)
            except Exception:
                continue
            held_flags = pool_risk_flags({}, info)
            for f in held_flags:
                print(f"  WARNING: your held {h['protocolName']} position itself: {f}")
            apy = float(info["apy"]) if info.get("apy") is not None else float(info.get("apyBps") or 0) / 10000
            klines = fetch_klines(h["stock"]["chainId"], h["stock"]["contractAddress"], limit=91)
            sigma = annualized_volatility(klines)
            if sigma:
                held_ratio = vol_richness_ratio(sigma, apy)
            break

        candidates = []
        for m in market:
            if m.get("investmentName", "").upper().find(h["stock"]["ticker"].upper()) == -1:
                continue
            try:
                info = fetch_investment_info(m["investmentId"])
            except Exception:
                continue
            if pool_risk_flags(m, info):
                continue
            apy = float(info["apy"]) if info.get("apy") is not None else float(info.get("apyBps") or 0) / 10000
            klines = fetch_klines(h["stock"]["chainId"], h["stock"]["contractAddress"], limit=91)
            sigma = annualized_volatility(klines)
            if sigma and sigma > 0:
                r = vol_richness_ratio(sigma, apy)
                if r is not None:
                    candidates.append(r)
            time.sleep(0.1)
        best_market_ratio = min(candidates) if candidates else None

        # "needs attention": the held pool itself is flagged, or the market has a meaningfully
        # richer alternative (>1.5x better vol_ratio) for the same ticker -- a bar deliberately
        # above "any tiny difference," so a scheduled check (see README) doesn't cry wolf daily.
        needs_attention = bool(held_flags) or (
            held_ratio is not None and best_market_ratio is not None and best_market_ratio > 0
            and held_ratio > best_market_ratio * REBALANCE_ATTENTION_GAP
        )
        label = f"{h['protocolName']} ({h['stock']['ticker']})"
        print(f"{label:<28}{richness_grade(held_ratio):>14}{richness_grade(best_market_ratio):>20}"
              f"{'  <- needs attention' if needs_attention else ''}")
        rows.append({
            "protocol": h["protocolName"], "ticker": h["stock"]["ticker"],
            "held_vol_ratio": held_ratio, "held_grade": richness_grade(held_ratio),
            "best_market_vol_ratio": best_market_ratio, "best_market_grade": richness_grade(best_market_ratio),
            "held_flags": held_flags, "needs_attention": needs_attention,
        })

    print("\nRecommendation only -- nothing moved. To act, use `defi redeem`/`lp-remove` then "
          "`defi deposit`/`lp-add` via binance-agentic-wallet's confirmed flow.")

    if args.json:
        print(json.dumps({"positions": rows, "any_needs_attention": any(r["needs_attention"] for r in rows)}, indent=2))


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
    p_vol.add_argument("--days", type=int, default=30)
    p_vol.add_argument("--apy", type=float, default=None, help="optional: also show breakeven vol at this pool APY (0.30 = 30%%)")
    p_vol.set_defaults(func=cmd_vol)

    p_scan = sub.add_parser("scan", help="rank stock-token LP pools by risk-adjusted (net of IL) APY")
    p_scan.add_argument("--top", type=int, default=15)
    p_scan.add_argument("--json", action="store_true")
    p_scan.add_argument("--capital", type=float, default=None,
                         help="optional: intended deposit size in USD -- shows expected $ return and "
                              "a concentration warning if you'd dominate the top pick's TVL")
    p_scan.add_argument("--max-pages", type=int, default=3,
                         help="pages of LP pools to fetch (100/page, sorted by apy DESC); higher "
                              "reaches lower-apy pools a single page would miss, at the cost of more API calls")
    p_scan.add_argument("--max-fee-rate", type=float, default=MAX_SANE_FEE_RATE,
                         help=f"pre-deposit screen: max sane feeRate per swap (default {MAX_SANE_FEE_RATE})")
    p_scan.add_argument("--min-tvl", type=float, default=MIN_SANE_TVL_USD,
                         help=f"pre-deposit screen: minimum pool TVL in USD (default {MIN_SANE_TVL_USD:.0f})")
    p_scan.add_argument("--peer-outlier-multiple", type=float, default=PEER_APY_OUTLIER_MULTIPLE,
                         help=f"pre-deposit screen: flag apy above this multiple of peer median (default {PEER_APY_OUTLIER_MULTIPLE})")
    p_scan.add_argument("--min-security-score", type=float, default=MIN_PROTOCOL_SECURITY_SCORE,
                         help=f"pre-deposit screen: minimum protocol securityScore, 0-100 (default {MIN_PROTOCOL_SECURITY_SCORE})")
    p_scan.add_argument("--with-range", action="store_true",
                         help="also compute the recommended concentrated-liquidity range per pool")
    p_scan.set_defaults(func=cmd_scan)

    p_range = sub.add_parser("range", help="range-by-range IL/APY breakdown + recommended range for one pool")
    p_range.add_argument("--investmentId", dest="investment_id", help="pull live apy/ticker for this pool")
    p_range.add_argument("--ticker", help="alternative to --investmentId: stock ticker")
    p_range.add_argument("--apy", type=float, help="alternative to --investmentId: pool APY as a decimal (0.30 = 30%%)")
    p_range.add_argument("--side", choices=["straddle", "sell", "buy"], default="straddle",
                          help="straddle=symmetric market-making (default), sell/buy=single-sided limit-order-style range")
    p_range.add_argument("--target-offset", type=float, default=None,
                          help="sell/buy only: exact offset from current price (0.15 = 15%%) to evaluate "
                               "in addition to the preset sweep -- for a specific target price, not just presets")
    p_range.add_argument("--band-width", type=float, default=SIDED_BAND_WIDTH,
                          help=f"sell/buy only: width of the --target-offset band (default {SIDED_BAND_WIDTH})")
    p_range.add_argument("--capital", type=float, default=None,
                          help="optional (--investmentId only): intended deposit size in USD -- shows "
                               "expected $ return and a concentration warning vs this pool's TVL")
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
    p_rebalance.set_defaults(func=cmd_rebalance_check)

    p_recommend = sub.add_parser("recommend",
                                  help="single entry point: top pick + range + a check on any held positions, one verdict")
    p_recommend.add_argument("--max-pages", type=int, default=1,
                              help="pages of LP pools to scan (default 1, for a fast verdict; "
                                   "use `scan --max-pages` directly for the thorough sweep)")
    p_recommend.add_argument("--capital", type=float, default=None,
                              help="optional: intended deposit size in USD -- shows expected $ return and a concentration warning")
    p_recommend.set_defaults(func=cmd_recommend)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
