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
  python riskscreen.py stocks --limit 20
  python riskscreen.py vol --ticker TSLA --days 30
  python riskscreen.py scan --top 15
  python riskscreen.py range --ticker TSLA --apy 0.30 --side straddle
  python riskscreen.py range --ticker TSLA --apy 0.30 --side sell
  python riskscreen.py positions
  python riskscreen.py rebalance-check

Deliberately NOT included: deposit/withdraw execution. `scan`/`range` surface
the `investmentId` + token addresses; actually moving funds goes through
`binance-agentic-wallet`'s already-reviewed `defi deposit` / `defi lp-add` /
`defi redeem` / `defi lp-remove` flow (preview -> explicit user confirmation
-> execute). This tool only ever recommends.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.request

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


def recommend_range(pool_apy, sigma_annual, side="straddle", years=1.0):
    """Sweep a set of candidate ranges and recommend the one with the highest net_apy among
    those meeting the SAFETY_P_ACTIVE_FLOOR probability floor. `side`: "straddle" (default,
    symmetric market-making ranges around the current price), "sell" (single-sided ranges
    above current price -- a limit-sell-style order), or "buy" (single-sided ranges below --
    a limit-buy-style order)."""
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
            rows.append(range_metrics(pool_apy, sigma_annual, 1 + offset, 1 + offset + SIDED_BAND_WIDTH, years))
    elif side == "buy":
        for offset in DEFAULT_SIDED_OFFSETS:
            pb = 1 - offset
            pa = max(pb - SIDED_BAND_WIDTH, 0.01)
            rows.append(range_metrics(pool_apy, sigma_annual, pa, pb, years))
    else:
        raise ValueError(f"unknown side {side!r}")
    safe = [r for r in rows if r["p_active"] >= SAFETY_P_ACTIVE_FLOOR]
    best = max(safe or rows, key=lambda r: r["net_apy"])
    return rows, best


def baw(*args):
    result = subprocess.run(["baw", *args, "--json"], capture_output=True, timeout=30,
                             shell=(os.name == "nt"))
    result.stdout = result.stdout.decode("utf-8", errors="replace")
    result.stderr = result.stderr.decode("utf-8", errors="replace")
    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError(f"baw {' '.join(args)} produced no output (stderr: {result.stderr.strip()})")
    first_brace = stdout.find("{")
    return json.loads(stdout[first_brace:] if first_brace > 0 else stdout)


def fetch_lp_investments():
    body = baw("defi", "investment-list", "--investType", "LiquidityPool", "--size", "100")
    if not body.get("success"):
        raise RuntimeError(json.dumps(body.get("error", body)))
    return body["data"]["list"]


MAX_SANE_FEE_RATE = 0.05  # 5% per swap -- generous; real fee tiers top out around 1%


def fee_rate_anomaly(info):
    """Flag a pool whose `feeRate` (fraction per swap) is outside a sane range -- a strong
    signal the platform's reported apy/apyBps for this pool is a data or dynamic-fee-hook
    artifact, not a durable rate. V4 pools can carry custom hooks with arbitrary (including
    broken or malicious) fee logic; this is a cheap sanity check, not a full hook audit --
    see README/SKILL.md roadmap for the latter. Returns a warning string, or None if sane.
    """
    fee_rate = info.get("feeRate")
    if fee_rate is None:
        return None
    try:
        fee_rate = float(fee_rate)
    except (TypeError, ValueError):
        return None
    if fee_rate > MAX_SANE_FEE_RATE:
        return (f"feeRate={fee_rate*100:.2f}% per swap is outside a sane range (>{MAX_SANE_FEE_RATE*100:.0f}%) "
                f"-- its apy figure is likely a data or dynamic-fee-hook artifact, not a trustworthy rate")
    return None


def fetch_investment_info(investment_id):
    body = baw("defi", "investment-info", "--investmentId", investment_id)
    if not body.get("success"):
        raise RuntimeError(json.dumps(body.get("error", body)))
    return body["data"]


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
        be = breakeven_volatility(args.apy) if args.apy else None
        be_str = f", breakeven vol @ {args.apy*100:.0f}% APY = {be*100:.2f}%" if be else ""
        print(f"{t['symbol']} (chain {t['chainId']}): annualized vol = {sigma*100:.2f}%, "
              f"est. full-range IL/yr = {il*100:.2f}%{be_str}")


def cmd_scan(args):
    print("fetching tokenized-stock list...", file=sys.stderr)
    stock_tokens = fetch_stock_tokens()
    stock_index = build_stock_index(stock_tokens)
    ticker_by_symbol = {t["symbol"].lower(): t for t in stock_tokens}

    print("fetching LP pools (requires signed-in baw session)...", file=sys.stderr)
    try:
        pools = fetch_lp_investments()
    except Exception as e:
        print(f"could not fetch LP pools: {e}", file=sys.stderr)
        print("run `baw auth signin` / `baw auth verify` first, then re-run scan.", file=sys.stderr)
        sys.exit(1)

    candidates = []
    for p in pools:
        name = p.get("investmentName", "")
        name_tokens = [tok.lower() for tok in name.replace("-", "/").split("/")]
        hit = next((ticker_by_symbol[tok] for tok in name_tokens if tok in ticker_by_symbol), None)
        if hit:
            candidates.append((p, hit))

    print(f"found {len(candidates)}/{len(pools)} LP pools naming a tokenized-stock symbol", file=sys.stderr)

    results = []
    flagged = []
    vol_cache = {}
    for pool, name_hit in candidates:
        try:
            info = fetch_investment_info(pool["investmentId"])
        except Exception:
            continue
        time.sleep(0.1)

        anomaly = fee_rate_anomaly(info)
        if anomaly:
            flagged.append((pool.get("investmentName"), pool.get("protocolName"), anomaly))
            continue

        chain_id = pool.get("binanceChainId") or info.get("binanceChainId")
        asset_list = info.get("assetTokenList") or []
        stock = next(
            (stock_index[(chain_id, a["tokenAddress"].lower())]
             for a in asset_list if (chain_id, a["tokenAddress"].lower()) in stock_index),
            name_hit,
        )

        key = (stock["chainId"], stock["contractAddress"])
        if key not in vol_cache:
            try:
                klines = fetch_klines(stock["chainId"], stock["contractAddress"], limit=91)
                vol_cache[key] = annualized_volatility(klines)
            except Exception:
                vol_cache[key] = None
            time.sleep(0.1)
        sigma = vol_cache[key]
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

        scored = risk_adjusted_apy(apy, sigma)
        result = {
            "protocol": pool.get("protocolName"),
            "pool": pool.get("investmentName"),
            "investmentId": pool.get("investmentId"),
            "stock_ticker": stock["ticker"],
            "tvl": float(pool.get("tvl") or 0),
            **scored,
        }
        if args.with_range:
            _, best = recommend_range(apy, sigma, side="straddle")
            result["best_range"] = best
        results.append(result)

    results.sort(key=lambda r: r["net_apy"], reverse=True)

    if args.with_range:
        print(f"\n{'pool':<20}{'ticker':<8}{'apy':>9}{'vol':>9}{'vol_ratio':>10}"
              f"{'full-net':>10}{'best +/-%':>10}{'range-net':>11}{'p_active':>10}{'tvl':>14}")
        for r in results[: args.top]:
            b = r["best_range"]
            width = f"{(b['pb']-1)*100:.0f}%" if b["pb"] is not None else "full"
            vr = f"{r['vol_ratio']:.2f}" if r["vol_ratio"] is not None else "n/a"
            print(f"{r['pool']:<20}{r['stock_ticker']:<8}"
                  f"{r['apy']*100:>8.2f}%{r['sigma_annual']*100:>8.2f}%{vr:>10}"
                  f"{r['net_apy']*100:>9.2f}%{width:>10}"
                  f"{b['net_apy']*100:>10.2f}%{b['p_active']*100:>9.0f}%"
                  f"{r['tvl']:>14,.0f}")
        print("\n('vol_ratio' = realized vol / breakeven vol -- <1 means the pool pays more "
              "than the realized risk implies, the 'scientifically cheap' signal; "
              "'best +/-%' = recommended symmetric range width; 'range-net' assumes an LP "
              "actively holding that range; run `range --investmentId <id>` for the full "
              "width-by-width breakdown, or `--side sell/buy` for single-sided limit-order-style "
              "ranges, on one pool.)")
    else:
        print(f"\n{'pool':<20}{'ticker':<8}{'apy':>9}{'vol':>9}{'est.IL':>9}{'net_apy':>10}{'vol_ratio':>10}{'tvl':>14}")
        for r in results[: args.top]:
            vr = f"{r['vol_ratio']:.2f}" if r["vol_ratio"] is not None else "n/a"
            print(f"{r['pool']:<20}{r['stock_ticker']:<8}"
                  f"{r['apy']*100:>8.2f}%{r['sigma_annual']*100:>8.2f}%{r['expected_il']*100:>8.2f}%"
                  f"{r['net_apy']*100:>9.2f}%{vr:>10}"
                  f"{r['tvl']:>14,.0f}")
        print("\n('vol_ratio' = realized vol / breakeven vol, the pool's own is-it-cheap "
              "signal, independent of what range you'd hold it in -- see README.)")

    if flagged:
        print(f"\n{len(flagged)} pool(s) excluded from ranking -- anomalous feeRate:")
        for name, protocol, anomaly in flagged:
            print(f"  {name} ({protocol}): {anomaly}")

    if args.json:
        print(json.dumps(results[: args.top], indent=2))


def cmd_range(args):
    if args.investment_id:
        info = fetch_investment_info(args.investment_id)
        anomaly = fee_rate_anomaly(info)
        if anomaly:
            print(f"WARNING: {info.get('investmentName')} ({info.get('protocolName')}): {anomaly}", file=sys.stderr)
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

    if sigma is None:
        print("not enough kline history to estimate volatility", file=sys.stderr)
        sys.exit(1)

    vol_ratio = vol_richness_ratio(sigma, apy)
    vr_str = f"{vol_ratio:.2f}" if vol_ratio is not None else "n/a"
    verdict = "richly priced (pays more than realized risk)" if (vol_ratio is not None and vol_ratio < 1) \
        else "cheaply priced (fee income may not cover realized risk)"
    print(f"{label} -- annualized vol {sigma*100:.2f}%")
    print(f"breakeven vol (sigma*) {breakeven_volatility(apy)*100:.2f}%  |  "
          f"vol_ratio (realized/breakeven) = {vr_str}  ->  {verdict}")
    print("(vol_ratio is range-independent -- it's a property of this pool's APY vs the "
          "token's own volatility, not of which range below you'd pick.)\n")

    rows, best = recommend_range(apy, sigma, side=args.side)
    if args.side == "straddle":
        print(f"{'range':>10}{'concentration':>14}{'p_stay':>8}{'eff.apy':>10}{'est.IL':>9}{'net_apy':>10}")
        for r in rows:
            width = f"+/-{(r['pb']-1)*100:.0f}%" if r["pb"] is not None else "full"
            marker = "  <- recommended" if r is best else ""
            print(f"{width:>10}{r['concentration']:>14.2f}{r['p_active']*100:>7.0f}%"
                  f"{r['effective_apy']*100:>9.2f}%{r['expected_il']*100:>8.2f}%"
                  f"{r['net_apy']*100:>9.2f}%{marker}")
        print(f"\n(recommended = highest net_apy among ranges with >={SAFETY_P_ACTIVE_FLOOR*100:.0f}% "
              f"chance of staying in range over 1yr -- that's the 'safety' floor. Narrower ranges "
              f"earn more fee APY per dollar but exit the range more often, at which point they stop "
              f"earning fees entirely until rebalanced.)")
    else:
        verb = "rises to" if args.side == "sell" else "falls to"
        print(f"{'offset':>10}{'band':>18}{'concentration':>14}{'p_execute':>10}{'eff.apy':>10}{'net_apy':>10}")
        for r in rows:
            offset_pct = abs((r["pa"] if args.side == "sell" else r["pb"]) - 1) * 100
            band = f"[{r['pa']:.2f}, {r['pb']:.2f}]x"
            marker = "  <- recommended" if r is best else ""
            print(f"{offset_pct:>9.0f}%{band:>18}{r['concentration']:>14.2f}{r['p_active']*100:>9.0f}%"
                  f"{r['effective_apy']*100:>9.2f}%{r['net_apy']*100:>9.2f}%{marker}")
        print(f"\n(this places a concentrated range entirely {'above' if args.side == 'sell' else 'below'} "
              f"the current price -- a yield-enhanced limit {'sell' if args.side == 'sell' else 'buy'} order: "
              f"it only earns fees once price {verb} the band, and 'p_execute' is the probability "
              f"that ever happens within a year. Known simplification: 'net_apy' still uses the "
              f"IL-vs-hold formula as a generic liquidity-cost proxy, not a precise effective-execution-"
              f"price model -- see SKILL.md.)")


def cmd_positions(args):
    data = fetch_positions(refresh=args.refresh)
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


def cmd_rebalance_check(args):
    print("reading current positions...", file=sys.stderr)
    data = fetch_positions()
    stock_tokens = fetch_stock_tokens()
    stock_index = build_stock_index(stock_tokens)
    held = lp_positions_on_stock_tokens(data, stock_index)
    if not held:
        print("no LP positions on tokenized-stock pairs to check.")
        return

    print("scanning current market for comparison...", file=sys.stderr)
    market = fetch_lp_investments()
    market_by_id = {m["investmentId"]: m for m in market}

    print(f"\n{'held pool (ticker)':<28}{'held vol_ratio':>16}{'best market vol_ratio':>24}{'gap':>10}")
    print("(vol_ratio = realized vol / breakeven vol -- lower is better; a held position with a "
          "notably higher ratio than the best market option is the one worth reconsidering)\n")
    for h in held:
        inv_ids = h["investmentIds"] or []
        held_ratio = None
        for inv_id in inv_ids:
            m = market_by_id.get(inv_id)
            if not m:
                continue
            try:
                info = fetch_investment_info(inv_id)
            except Exception:
                continue
            held_anomaly = fee_rate_anomaly(info)
            if held_anomaly:
                print(f"  WARNING: your held {h['protocolName']} position itself: {held_anomaly}")
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
            if fee_rate_anomaly(info):
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

        label = f"{h['protocolName']} ({h['stock']['ticker']})"
        hs = f"{held_ratio:.2f}" if held_ratio is not None else "n/a"
        bs = f"{best_market_ratio:.2f}" if best_market_ratio is not None else "n/a"
        gap = f"{held_ratio - best_market_ratio:+.2f}" if held_ratio is not None and best_market_ratio is not None else "n/a"
        print(f"{label:<28}{hs:>16}{bs:>24}{gap:>10}")

    print("\nThis is a recommendation only -- nothing was moved. To act on a suggestion, use "
          "`baw defi redeem` / `defi lp-remove` then `defi deposit` / `defi lp-add` via the "
          "binance-agentic-wallet skill, with its normal preview + confirmation flow.")


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
    p_scan.add_argument("--with-range", action="store_true",
                         help="also compute the recommended concentrated-liquidity range per pool")
    p_scan.set_defaults(func=cmd_scan)

    p_range = sub.add_parser("range", help="range-by-range IL/APY breakdown + recommended range for one pool")
    p_range.add_argument("--investmentId", dest="investment_id", help="pull live apy/ticker for this pool")
    p_range.add_argument("--ticker", help="alternative to --investmentId: stock ticker")
    p_range.add_argument("--apy", type=float, help="alternative to --investmentId: pool APY as a decimal (0.30 = 30%%)")
    p_range.add_argument("--side", choices=["straddle", "sell", "buy"], default="straddle",
                          help="straddle=symmetric market-making (default), sell/buy=single-sided limit-order-style range")
    p_range.set_defaults(func=cmd_range)

    p_positions = sub.add_parser("positions", help="show current LP positions on tokenized-stock pairs")
    p_positions.add_argument("--refresh", action="store_true")
    p_positions.add_argument("--json", action="store_true")
    p_positions.set_defaults(func=cmd_positions)

    p_rebalance = sub.add_parser("rebalance-check",
                                  help="compare held stock-token LP positions against current market (report only, no execution)")
    p_rebalance.set_defaults(func=cmd_rebalance_check)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
