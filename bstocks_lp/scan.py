"""The core scan/evaluation pipeline (`run_scan`) that `scan`, `recommend`, and
`rebalance-check`'s market-comparison side all share, plus the ENTER/WATCH/NO_TRADE/
UNSCOREABLE verdict classification built on top of it.
"""

import concurrent.futures
import re
import subprocess
import sys
import urllib.error
from collections import Counter

from bstocks_lp import (
    config,
    il_model,
    market_data,
    range_model,
    risk_screen,
    volatility,
)

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


def _classify_fetch_error(e):
    """Coarse classification of a concurrent-fetch failure (investment-info or kline), for
    unscoreable reasons and failure_summary -- so "N pools failed" doesn't hide whether that's
    a transient timeout/network blip (worth retrying the whole run) or a per-pool data problem
    (this specific pool is broken, retrying won't help)."""
    if isinstance(e, (subprocess.TimeoutExpired, TimeoutError)):
        return "timeout"
    if isinstance(e, (urllib.error.URLError, ConnectionError)):
        return "network error"
    if isinstance(e, ValueError):
        return "invalid data"
    return "error"


def _summarize_unscoreable(unscoreable):
    """Frequency count of unscoreable reasons -- lets a caller see 'what kind of problem, how
    many' at a glance (e.g. {"could not compute volatility (kline fetch failed (network error))": 5})
    instead of reading a wall of per-pool messages one at a time to notice a pattern."""
    return dict(Counter(u["reason"] for u in unscoreable))


def _coverage_note(coverage, max_pages):
    """Human-readable heads-up for text output when a scan's `--max-pages` cut off part of the
    market -- None when the scan already saw every pool, so text mode stays quiet in the common
    case instead of printing a coverage line every run."""
    if not coverage["truncated"]:
        return None
    missed = coverage["pools_total"] - coverage["pools_fetched"]
    return (f"NOTE: scanned {coverage['pools_fetched']}/{coverage['pools_total']} LP pools "
            f"system-wide (--max-pages {max_pages}) -- {missed} more exist beyond this depth "
            f"(lower-apy pools sort last, so these are unlikely to be the best pick, but they "
            f"are unscanned, not screened out). Raise --max-pages to cover more of them.")


VERDICT_ENTER = "ENTER"
VERDICT_WATCH = "WATCH"
VERDICT_NO_TRADE = "NO_TRADE"
VERDICT_UNSCOREABLE = "UNSCOREABLE"


def passes_trade_gate(result):
    """Hard gate for calling something a "Top pick": positive net_apy AND vol_ratio < 1
    (i.e. not graded Cheap). A pool can clear the pre-deposit safety screen (be in `results`
    at all) and still not be worth entering -- `results` answers "is this pool safe and
    plausible", this answers "is it actually worth doing". Before this existed, `recommend`
    would print a "Top pick" even when every candidate netted negative or graded Cheap,
    which reads as an endorsement it didn't mean to make."""
    return result["model_net_apy"] > 0 and result["vol_ratio"] is not None and result["vol_ratio"] < 1


def classify_verdict(scored):
    """The one-of-four-labels reframing: every pool this tool ever reports on ends up as
    ENTER, WATCH, NO_TRADE, or UNSCOREABLE, so a caller can filter/sort on one field instead of
    reconstructing the same logic scattered across grade/flags/vol_ratio checks.

    This function only classifies the two "was actually scored" outcomes (ENTER vs WATCH) --
    NO_TRADE and UNSCOREABLE are assigned directly by run_scan at the point a pool gets flagged
    or fails to resolve, since there's no `scored` data to classify at that point. Built
    directly on passes_trade_gate's existing logic (same gate, just labeled) rather than a new
    threshold: ENTER when the pool clears it (positive model_net_apy AND vol_ratio < 1), WATCH
    otherwise. A WATCH pool already passed the pre-deposit safety screen (this is only ever
    called on `results` rows, never `flagged` ones) -- it's a legitimate pool, just not
    economically attractive right now, worth watching in case apy/vol shifts, not avoiding.
    """
    return VERDICT_ENTER if passes_trade_gate(scored) else VERDICT_WATCH


UNSCOREABLE_RATIO_REFUSE_THRESHOLD = 0.5  # refuse a verdict when more than half the candidate
                                            # pools couldn't even be evaluated -- the scoreable
                                            # remainder may not be representative of the market


def run_scan(max_pages=3, max_fee_rate=risk_screen.MAX_SANE_FEE_RATE, min_tvl=risk_screen.MIN_SANE_TVL_USD,
             peer_outlier_multiple=risk_screen.PEER_APY_OUTLIER_MULTIPLE,
             min_security_score=risk_screen.MIN_PROTOCOL_SECURITY_SCORE,
             block_unknown_v4_hooks=True, with_range=False, log=lambda msg: print(msg, file=sys.stderr)):
    """Core of `scan`, factored out so `recommend` and `rebalance_check`'s market-comparison
    side share one evaluation path instead of drifting apart (see evaluate_pool). Returns
    (results, flagged, unscoreable, coverage) sorted by net_apy descending; raises on an
    unrecoverable baw error (e.g. not signed in) -- callers decide how to present that.
    `unscoreable` lists (pool_name, reason) for candidates that were never scored at all (data
    fetch failed, no confirmed bStock, no usable kline overlap) -- distinct from `flagged`,
    which is pools that *were* scored but failed the risk/plausibility screen. Never silently
    drop one into the other: a user needs to tell "we couldn't evaluate this" apart from "we
    evaluated it and it's unsafe/implausible". `coverage` is {pools_fetched, pools_total,
    truncated} -- see fetch_lp_investments -- so callers can tell the user when `max_pages`
    left part of the market unscanned, instead of that being invisible."""
    log("fetching tokenized-stock list...")
    stock_tokens = market_data.fetch_stock_tokens()
    stock_index = market_data.build_stock_index(stock_tokens)
    ticker_by_symbol = {t["symbol"].lower(): t for t in stock_tokens}

    log("fetching LP pools (requires signed-in baw session)...")
    pools, pools_total = market_data.fetch_lp_investments(max_pages=max_pages)
    coverage = {"pools_fetched": len(pools), "pools_total": pools_total,
                "truncated": len(pools) < pools_total}

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
    info_fetch_errors = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_BAW_CALLS) as pool_executor:
        future_to_pool = {pool_executor.submit(market_data.fetch_investment_info, p["investmentId"]): p
                           for p in candidates}
        for future in concurrent.futures.as_completed(future_to_pool):
            p = future_to_pool[future]
            try:
                info_by_pool[p["investmentId"]] = future.result()
            except Exception as e:
                info_fetch_errors[p["investmentId"]] = _classify_fetch_error(e)

    resolved = []  # (pool, info, stock, quote_addr, pair_mode) for confirmed-bStock candidates
    unscoreable = []  # (pool_name, reason) -- surfaced, never silently dropped (see run_scan docstring)
    kline_keys = set()
    for pool in candidates:
        info = info_by_pool.get(pool["investmentId"])
        if info is None:
            reason = info_fetch_errors.get(pool["investmentId"], "error")
            unscoreable.append((pool.get("investmentName"), f"investment-info fetch failed ({reason})"))
            continue
        stock, chain_id, quote_addr, pair_mode = market_data.resolve_pool_stock_and_quote(pool, info, stock_index)
        if stock is None:
            # name-matching was only ever a pre-filter guess (see the widened matcher above);
            # if the authoritative on-chain assetTokenList doesn't confirm exactly one bStock
            # paired with exactly one other asset, trusting the name guess anyway risks
            # attributing volatility/apy data to the wrong token, or applying a two-asset IL
            # model to a pool this tool can't actually model. Skip rather than silently
            # mis-score it.
            unscoreable.append((pool.get("investmentName"),
                                 ("assetTokenList did not confirm exactly one bStock paired with one other asset "
                                  "(unsupported pool structure)")))
            continue
        resolved.append((pool, info, stock, chain_id, quote_addr, pair_mode))
        kline_keys.add((stock["chainId"], stock["contractAddress"]))
        if pair_mode == "non_stablecoin":
            kline_keys.add((chain_id, quote_addr))

    # Klines are plain HTTP (no subprocess spawn), but still worth fetching concurrently
    # for the same reason -- network round-trip latency, not local CPU, dominates.
    kline_cache = {}
    kline_fetch_errors = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_BAW_CALLS) as kline_executor:
        future_to_key = {
            kline_executor.submit(market_data.fetch_klines, chain_id, addr, limit=91): (chain_id, addr)
            for chain_id, addr in kline_keys
        }
        for future in concurrent.futures.as_completed(future_to_key):
            key = future_to_key[future]
            try:
                kline_cache[key] = future.result()
            except Exception as e:
                kline_cache[key] = None
                kline_fetch_errors[key] = _classify_fetch_error(e)

    prepared = []
    for pool, info, stock, chain_id, quote_addr, pair_mode in resolved:
        stock_key = (stock["chainId"], stock["contractAddress"])
        stock_klines = kline_cache.get(stock_key)
        vol_reason = None  # set below when sigma ends up None, so the unscoreable reason can
                            # distinguish "the kline fetch itself failed" from "it succeeded but
                            # the data was too thin/misaligned to trust" -- previously both
                            # collapsed into the same generic "insufficient kline history"
        if pair_mode == "stablecoin":
            # quote is a stablecoin -- its own volatility is ~0, so IL is driven almost
            # entirely by the bStock's own volatility (see README "The idea"). Yang-Zhang
            # (best_available_volatility) uses the OHLC data we already have instead of
            # close-only, for a more statistically efficient, drift-independent estimate.
            if stock_klines:
                sigma = volatility.best_available_volatility(stock_klines)
                vol_reason = None if sigma else "insufficient kline history"
            else:
                sigma = None
                vol_reason = f"kline fetch failed ({kline_fetch_errors.get(stock_key, 'error')})"
        else:
            # non-stablecoin quote (e.g. bStock/BNB): IL depends on the *relative* price move
            # between the two pooled assets, not the bStock's volatility alone. Using the
            # bStock-only volatility here would silently understate risk whenever the quote
            # asset moves too -- this was a real bug, not a simplification: NVDAB-BNB,
            # BNB-SPCXB, HOODB-BNB etc. were being scored as if BNB were a stablecoin.
            quote_key = (chain_id, quote_addr)
            quote_klines = kline_cache.get(quote_key)
            if stock_klines and quote_klines:
                rel_vol = volatility.relative_annualized_volatility(stock_klines, quote_klines)
                sigma, vol_reason = rel_vol["sigma"], rel_vol["reason"]
            elif stock_klines is None:
                sigma = None
                vol_reason = f"kline fetch failed ({kline_fetch_errors.get(stock_key, 'error')})"
            else:
                sigma = None
                vol_reason = f"quote kline fetch failed ({kline_fetch_errors.get(quote_key, 'error')})"
        if sigma is None or sigma <= 0:
            reason = vol_reason or "insufficient/no overlapping kline history"
            unscoreable.append((pool.get("investmentName"), f"could not compute volatility ({reason})"))
            continue

        # `investment-list`'s `apy` is null/0 for most concentrated (V3) LP pools;
        # `investment-info` carries the real fee-based rate as `apyBps` instead.
        if info.get("apy") is not None:
            apy = float(info["apy"])
        elif info.get("apyBps") is not None:
            apy = float(info["apyBps"]) / 10000
        else:
            apy = float(pool.get("apy") or 0)

        prepared.append({"pool": pool, "info": info, "stock": stock, "sigma": sigma, "apy": apy,
                          "pair_mode": pair_mode})

    # Pass 2: apply the risk/plausibility screen with full peer context, then score the survivors.
    # Keyed by investmentId (not just apy value) so a pool excludes only itself as a "peer" --
    # two distinct pools that happen to share an apy must not exclude each other.
    entries_by_ticker: dict[str, list[tuple[str, float]]] = {}
    for p in prepared:
        entries_by_ticker.setdefault(p["stock"]["ticker"], []).append((p["pool"]["investmentId"], p["apy"]))

    # Protocol security scores are looked up once per *distinct* protocol (fetch_protocol_security_score's
    # own cache already dedupes that), but fetching them one at a time inside this loop -- each a
    # separate baw subprocess spawn, the same ~0.6s+ overhead Pass 1 already pays concurrently for
    # -- turned into real sequential wait: 4 distinct protocols measured at ~2.9s fetched serially,
    # vs ~0.7s (the slowest single call) fetched concurrently. Same fix as Pass 1: resolve every
    # distinct protocol's score up front via a thread pool, then the scoring loop below is a pure
    # cache lookup, no I/O.
    distinct_protocol_ids = {pid for pid in
                              (p["pool"].get("defiProtocolId") or p["info"].get("defiProtocolId") for p in prepared)
                              if pid}
    security_score_cache: dict[str, float | None] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_BAW_CALLS) as score_executor:
        future_to_pid = {score_executor.submit(market_data.fetch_protocol_security_score, pid, {}): pid
                          for pid in distinct_protocol_ids}
        for future in concurrent.futures.as_completed(future_to_pid):
            pid = future_to_pid[future]
            try:
                security_score_cache[pid] = future.result()
            except Exception:
                security_score_cache[pid] = None

    results = []
    flagged = []
    for p in prepared:
        pool, info, stock, sigma, apy = p["pool"], p["info"], p["stock"], p["sigma"], p["apy"]
        protocol_id = pool.get("defiProtocolId") or info.get("defiProtocolId")
        security_score = security_score_cache.get(protocol_id) if protocol_id else None
        peer_apys = [a for inv_id, a in entries_by_ticker.get(stock["ticker"], []) if inv_id != pool["investmentId"]]
        evaluation = risk_screen.evaluate_pool(
            pool, info, sigma, apy, peer_apys=peer_apys, protocol_security_score=security_score,
            max_fee_rate=max_fee_rate, min_tvl_usd=min_tvl,
            peer_outlier_multiple=peer_outlier_multiple,
            min_security_score=min_security_score,
            block_unknown_v4_hooks=block_unknown_v4_hooks)
        if evaluation["flags"]:
            flagged.append({"investmentId": pool.get("investmentId"), "pool": pool.get("investmentName"),
                             "protocol": pool.get("protocolName"), "flags": evaluation["flags"],
                             "verdict": VERDICT_NO_TRADE})
            continue

        scored = evaluation["scored"]
        if scored["model_net_apy"] is None:
            # sigma/apy were both resolvable (this pool isn't in Pass 1's unscoreable list), but
            # volatility is high enough that expected_il_fraction can't produce a valid full-range
            # IL estimate (see its docstring) -- surfaced the same way as any other "couldn't
            # score this" reason, not silently dropped or forced into a WATCH/ENTER verdict with
            # a numeric net_apy that doesn't actually exist.
            unscoreable.append((pool.get("investmentName"),
                                 f"volatility too extreme ({sigma*100:.0f}% annualized) for the "
                                 f"diffusion IL approximation to stay valid -- model_net_apy cannot "
                                 f"be estimated for a full-range position at this pool's volatility"))
            continue

        result = {
            "protocol": pool.get("protocolName"),
            "pool": pool.get("investmentName"),
            "investmentId": pool.get("investmentId"),
            "stock_ticker": stock["ticker"],
            "tvl": float(pool.get("tvl") or 0),
            "grade": il_model.richness_grade(scored["vol_ratio"]),
            "verdict": classify_verdict(scored),
            "pair_mode": p["pair_mode"],
            **scored,
        }
        if with_range:
            _, best = range_model.recommend_range(apy, sigma, side="straddle")
            best["confidence"] = range_model.confidence_grade(best["p_active"])
            result["best_range"] = best
        results.append(result)

    results.sort(key=lambda r: r["model_net_apy"], reverse=True)
    unscoreable_dicts = [{"pool": name, "reason": reason, "verdict": VERDICT_UNSCOREABLE} for name, reason in unscoreable]
    return results, flagged, unscoreable_dicts, coverage
