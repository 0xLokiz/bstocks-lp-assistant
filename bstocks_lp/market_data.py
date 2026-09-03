"""Fetching and shaping market data: the RWA stock-token list, klines, LP pool listings, and
the user's own DeFi positions. Everything here is a thin, cached-where-appropriate layer over
`api.baw`/`api._get` -- no volatility/IL/risk math lives in this module.
"""

import functools
import json

from bstocks_lp import api, config

RWA_LIST_URL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/stock/detail/list/ai"
KLINE_URL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/kline/ai"


@functools.cache
def fetch_stock_tokens(type_filter=config.BSTOCK_TYPE):
    """Defaults to bStock (type=3) only -- this product is scoped to bStocks specifically,
    not tokenized-stock LPs on other providers. Pass type_filter=None for every platform,
    or 1/2 to browse Ondo/xStocks instead.

    Memoized (in-process only, no cross-invocation persistence -- each CLI run is a fresh
    process, so there's no staleness to manage): the token list doesn't change within the
    lifetime of a single command, but was being refetched multiple times per run regardless
    (e.g. recommend calling it again after run_scan's own internal fetch, just to check held
    positions). The list only has ~100 entries and the return value is only ever read, never
    mutated in place, so caching the exact object is safe.
    """
    params = {"type": type_filter} if type_filter else {}
    body = api._get(RWA_LIST_URL, params)
    if not body.get("success"):
        raise RuntimeError(f"RWA list fetch failed: {body}")
    return body["data"]


@functools.cache
def fetch_klines(chain_id, contract_address, interval="1d", limit=90):
    """Memoized in-process for the same reason as fetch_stock_tokens: the same token's klines
    can be requested more than once per command (run_scan's own batch plus a held-position
    fallback evaluation on the same ticker, for instance) -- caching by the exact call
    signature eliminates the duplicate network round-trip. thread-safe (lru_cache has an
    internal lock); a rare race under concurrent first-access from two different
    ThreadPoolExecutors could still produce one redundant fetch, never a correctness issue.
    """
    body = api._get(KLINE_URL, {
        "chainId": chain_id,
        "contractAddress": contract_address,
        "interval": interval,
        "limit": limit,
    })
    if not body.get("success"):
        raise RuntimeError(f"kline fetch failed: {body}")
    return body["data"]["klineInfos"]


def fetch_lp_investments(max_pages=3):
    """Page through `defi investment-list` (max 100/page per the API) instead of reading only
    the first page. Sorted by apy DESC (the API default), so later pages surface lower-apy --
    but not necessarily lower risk-adjusted -- pools that a single 100-pool page would never
    reach. Stops early once a page comes back short (no more results) or `max_pages` is hit.

    Returns (pools, pools_total): `pools_total` is the API's own count of every LiquidityPool
    investment system-wide (the `total` field, present on every page), independent of how many
    pages were actually fetched. Without this, `len(pools) < max_pages * 100` was the only way
    to even suspect a scan was cut short -- a real gap: answering "did this cover every pool?"
    took a live manual investigation before pools_total existed. Callers compare it against
    len(pools) to tell a genuinely-complete scan apart from one `max_pages` truncated.
    """
    all_pools = []
    pools_total = 0
    for page in range(1, max_pages + 1):
        body = api.baw("defi", "investment-list", "--investType", "LiquidityPool", "--size", "100", "--page", str(page))
        if not body.get("success"):
            raise RuntimeError(json.dumps(body.get("error", body)))
        data = body["data"]
        page_list = data["list"]
        pools_total = data.get("total", pools_total)
        all_pools.extend(page_list)
        if len(page_list) < 100:
            break
    return all_pools, pools_total


def fetch_investment_info(investment_id):
    body = api.baw("defi", "investment-info", "--investmentId", investment_id)
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
        body = api.baw("defi", "protocol-info", "--defiProtocolId", defi_protocol_id)
        score = body["data"].get("securityScore") if body.get("success") else None
    except Exception:
        score = None
    cache[defi_protocol_id] = score
    return score


def fetch_positions(refresh=False):
    args = ["defi", "position"] + (["--refresh"] if refresh else [])
    body = api.baw(*args)
    if not body.get("success"):
        raise RuntimeError(json.dumps(body.get("error", body)))
    return body["data"]


def build_stock_index(stock_tokens):
    return {(t["chainId"], t["contractAddress"].lower()): t for t in stock_tokens}


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
    pair_mode = "stablecoin" if config.is_stablecoin(chain_id, quote_addr) else "non_stablecoin"
    return stock, chain_id, quote_addr, pair_mode


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
