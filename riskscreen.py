#!/usr/bin/env python3
"""Risk-adjusted APY screener for Binance Web3 LP pools on tokenized-stock pairs.

LP fee/incentive APY is compensation for impermanent loss (IL), and IL scales
with the volatility of the pooled assets. This tool re-ranks LP pools by
APY *net of* an estimated IL cost, so pools aren't compared on headline APY
alone. Stock-token pools are the clearest case: they're paired against a
stablecoin, so IL is driven almost entirely by the stock token's own
volatility (no cross-asset correlation term needed).

Data sources (all public, no auth):
  - RWA stock token list / kline: bapi/defi public endpoints (Binance Web3)
Data source (needs an active `baw` session):
  - LP pool APY/TVL/composition: `baw defi investment-list` / `investment-info`

Usage:
  python riskscreen.py stocks --limit 20
  python riskscreen.py vol --ticker TSLA --days 30
  python riskscreen.py scan --top 15
  python riskscreen.py range --ticker TSLA --apy 0.30
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


def fetch_stock_tokens(type_filter=None):
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


def risk_adjusted_apy(apy, sigma_annual):
    il = expected_il_fraction(sigma_annual)
    net = apy - il
    score = net / sigma_annual if sigma_annual > 0 else None
    return {"apy": apy, "sigma_annual": sigma_annual, "expected_il": il, "net_apy": net, "score": score}


def _normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _il_at_price_ratio(k):
    """Standard constant-product IL, as a positive loss fraction, at price ratio k = P1/P0."""
    return 1 - 2 * math.sqrt(k) / (1 + k)


def concentration_multiplier(lower_pct, upper_pct):
    """Capital-efficiency multiplier of a [1-lower_pct, 1+upper_pct] range vs full range (Uniswap V3 math)."""
    pa, pb = 1 - lower_pct, 1 + upper_pct
    denom = 1 - math.sqrt(pa / pb)
    return 1 / denom if denom > 1e-9 else float("inf")


def stay_in_range_probability(lower_pct, upper_pct, sigma_annual, years=1.0):
    """P(price stays within [1-lower_pct, 1+upper_pct]) under zero-drift lognormal diffusion."""
    pa, pb = 1 - lower_pct, 1 + upper_pct
    s = sigma_annual * math.sqrt(years)
    if s <= 0:
        return 1.0
    d_low = math.log(pa) / s
    d_high = math.log(pb) / s
    return _normal_cdf(d_high) - _normal_cdf(d_low)


def range_adjusted_metrics(pool_apy, sigma_annual, lower_pct, upper_pct, years=1.0):
    """Range-adjusted fee APY / IL / net APY, approximating the pool's reported APY as a
    full-range-equivalent baseline (see README/SKILL.md caveat: the platform doesn't expose
    per-tick fee data, so this scales a blended pool APY rather than a true full-range rate).
    """
    if lower_pct >= 1 or upper_pct <= -1:
        raise ValueError("range must keep price positive")
    m = concentration_multiplier(lower_pct, upper_pct)
    p_stay = stay_in_range_probability(lower_pct, upper_pct, sigma_annual, years)
    effective_apy = pool_apy * m * p_stay
    il_diffusion = m * (sigma_annual ** 2) * years / 8
    il_boundary = max(_il_at_price_ratio(1 - lower_pct), _il_at_price_ratio(1 + upper_pct))
    expected_il = min(il_diffusion, il_boundary)
    net_apy = effective_apy - expected_il
    sigma_effective = sigma_annual * math.sqrt(m)
    score = net_apy / sigma_effective if sigma_effective > 0 else None
    return {
        "lower_pct": lower_pct, "upper_pct": upper_pct, "concentration": m,
        "p_stay_in_range": p_stay, "effective_apy": effective_apy,
        "expected_il": expected_il, "net_apy": net_apy, "score": score,
    }


DEFAULT_RANGE_WIDTHS = [0.05, 0.10, 0.20, 0.30, 0.50, 0.90]
SAFETY_P_STAY_FLOOR = 0.6


def recommend_range(pool_apy, sigma_annual, widths=None, years=1.0):
    widths = widths or DEFAULT_RANGE_WIDTHS
    rows = [range_adjusted_metrics(pool_apy, sigma_annual, w, w, years) for w in widths]
    full_range = risk_adjusted_apy(pool_apy, sigma_annual)
    rows.append({
        "lower_pct": None, "upper_pct": None, "concentration": 1.0, "p_stay_in_range": 1.0,
        "effective_apy": pool_apy, "expected_il": full_range["expected_il"],
        "net_apy": full_range["net_apy"], "score": full_range["score"],
    })
    safe = [r for r in rows if r["p_stay_in_range"] >= SAFETY_P_STAY_FLOOR]
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
        print(f"{t['symbol']} (chain {t['chainId']}): annualized vol = {sigma*100:.2f}%, "
              f"est. full-range IL/yr = {il*100:.2f}%")


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
    vol_cache = {}
    for pool, name_hit in candidates:
        try:
            info = fetch_investment_info(pool["investmentId"])
        except Exception:
            continue
        time.sleep(0.1)

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
            _, best = recommend_range(apy, sigma)
            result["best_range"] = best
        results.append(result)

    results.sort(key=lambda r: r["net_apy"], reverse=True)

    if args.with_range:
        print(f"\n{'pool':<20}{'ticker':<8}{'apy':>9}{'vol':>9}"
              f"{'full-net':>10}{'best +/-%':>10}{'range-net':>11}{'p_stay':>8}{'tvl':>14}")
        for r in results[: args.top]:
            b = r["best_range"]
            width = f"{b['upper_pct']*100:.0f}%" if b["upper_pct"] is not None else "full"
            print(f"{r['pool']:<20}{r['stock_ticker']:<8}"
                  f"{r['apy']*100:>8.2f}%{r['sigma_annual']*100:>8.2f}%"
                  f"{r['net_apy']*100:>9.2f}%{width:>10}"
                  f"{b['net_apy']*100:>10.2f}%{b['p_stay_in_range']*100:>7.0f}%"
                  f"{r['tvl']:>14,.0f}")
        print("\n('best +/-%' = recommended symmetric range width; 'range-net' assumes an LP "
              "actively holding that range; run `range --investmentId <id>` for the full "
              "width-by-width breakdown on one pool.)")
    else:
        print(f"\n{'pool':<20}{'ticker':<8}{'apy':>9}{'vol':>9}{'est.IL':>9}{'net_apy':>10}{'score':>8}{'tvl':>14}")
        for r in results[: args.top]:
            print(f"{r['pool']:<20}{r['stock_ticker']:<8}"
                  f"{r['apy']*100:>8.2f}%{r['sigma_annual']*100:>8.2f}%{r['expected_il']*100:>8.2f}%"
                  f"{r['net_apy']*100:>9.2f}%{r['score'] if r['score'] is not None else 0:>8.2f}"
                  f"{r['tvl']:>14,.0f}")

    if args.json:
        print(json.dumps(results[: args.top], indent=2))


def cmd_range(args):
    if args.investment_id:
        info = fetch_investment_info(args.investment_id)
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

    rows, best = recommend_range(apy, sigma)
    print(f"{label} -- annualized vol {sigma*100:.2f}%\n")
    print(f"{'range':>10}{'concentration':>14}{'p_stay':>8}{'eff.apy':>10}{'est.IL':>9}{'net_apy':>10}{'score':>8}")
    for r in rows:
        width = f"+/-{r['upper_pct']*100:.0f}%" if r["upper_pct"] is not None else "full"
        marker = "  <- recommended" if r is best else ""
        print(f"{width:>10}{r['concentration']:>14.2f}{r['p_stay_in_range']*100:>7.0f}%"
              f"{r['effective_apy']*100:>9.2f}%{r['expected_il']*100:>8.2f}%"
              f"{r['net_apy']*100:>9.2f}%{r['score'] if r['score'] is not None else 0:>8.2f}{marker}")
    print(f"\n(recommended = highest net_apy among ranges with >={SAFETY_P_STAY_FLOOR*100:.0f}% "
          f"chance of staying in range over 1yr -- that's the 'safety' floor. Narrower ranges "
          f"earn more fee APY per dollar but exit the range more often, at which point they stop "
          f"earning fees entirely until rebalanced.)")


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

    print(f"\n{'held pool (ticker)':<28}{'current score':>15}{'best market score':>20}{'gap':>10}")
    for h in held:
        inv_ids = h["investmentIds"] or []
        held_score = None
        for inv_id in inv_ids:
            m = market_by_id.get(inv_id)
            if not m:
                continue
            try:
                info = fetch_investment_info(inv_id)
            except Exception:
                continue
            apy = float(info["apy"]) if info.get("apy") is not None else float(info.get("apyBps") or 0) / 10000
            klines = fetch_klines(h["stock"]["chainId"], h["stock"]["contractAddress"], limit=91)
            sigma = annualized_volatility(klines)
            if sigma:
                held_score = risk_adjusted_apy(apy, sigma)["score"]
            break

        candidates = []
        for m in market:
            if m.get("investmentName", "").upper().find(h["stock"]["ticker"].upper()) == -1:
                continue
            try:
                info = fetch_investment_info(m["investmentId"])
            except Exception:
                continue
            apy = float(info["apy"]) if info.get("apy") is not None else float(info.get("apyBps") or 0) / 10000
            klines = fetch_klines(h["stock"]["chainId"], h["stock"]["contractAddress"], limit=91)
            sigma = annualized_volatility(klines)
            if sigma and sigma > 0:
                candidates.append(risk_adjusted_apy(apy, sigma)["score"])
            time.sleep(0.1)
        best_market_score = max(candidates) if candidates else None

        label = f"{h['protocolName']} ({h['stock']['ticker']})"
        hs = f"{held_score:.2f}" if held_score is not None else "n/a"
        bs = f"{best_market_score:.2f}" if best_market_score is not None else "n/a"
        gap = f"{best_market_score - held_score:+.2f}" if held_score is not None and best_market_score is not None else "n/a"
        print(f"{label:<28}{hs:>15}{bs:>20}{gap:>10}")

    print("\nThis is a recommendation only -- nothing was moved. To act on a suggestion, use "
          "`baw defi redeem` / `defi lp-remove` then `defi deposit` / `defi lp-add` via the "
          "binance-agentic-wallet skill, with its normal preview + confirmation flow.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_stocks = sub.add_parser("stocks", help="list tokenized-stock tokens")
    p_stocks.add_argument("--type", type=int, default=None, help="1=Ondo, 2=xStocks, 3=bStock")
    p_stocks.add_argument("--limit", type=int, default=20)
    p_stocks.set_defaults(func=cmd_stocks)

    p_vol = sub.add_parser("vol", help="compute annualized volatility + est. IL for one ticker")
    p_vol.add_argument("--ticker", required=True)
    p_vol.add_argument("--days", type=int, default=30)
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
