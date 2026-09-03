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
`breakeven_volatility` / `vol_richness_ratio` in bstocks_lp/il_model.py.

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
import json
import math
import sys
import time

from bstocks_lp import (
    config,
    il_model,
    market_data,
    range_model,
    risk_screen,
    scan,
    volatility,
)


def _pct_or_na(x, width):
    """Right-align x*100 as a percentage in a field of `width` characters (numeral + '%'), or
    'N/A' in the same width when x is None -- see il_model.expected_il_fraction: at extreme
    volatility it returns None rather than a number the diffusion approximation can no longer
    justify, and this is the one place in text output that value can surface (cmd_range's
    straddle-mode full-range row; every other row/command is always numeric -- see run_scan,
    which routes a None model_net_apy to `unscoreable` before it ever reaches a results table)."""
    return (f"{x*100:.2f}%" if x is not None else "N/A").rjust(width)


def cmd_stocks(args):
    tokens = market_data.fetch_stock_tokens(type_filter=args.type)
    for t in tokens[: args.limit]:
        print(f"{t['ticker']:<8} {t['symbol']:<12} chain={t['chainId']:<3} {t['contractAddress']}")
    print(f"\n{len(tokens)} tokens total (showing {min(args.limit, len(tokens))})")


def cmd_vol(args):
    tokens = market_data.fetch_stock_tokens()
    matches = [t for t in tokens if t["ticker"].upper() == args.ticker.upper()]
    if not matches:
        print(f"no stock token found for ticker {args.ticker}", file=sys.stderr)
        sys.exit(1)
    for t in matches:
        klines = market_data.fetch_klines(t["chainId"], t["contractAddress"], limit=args.days + 1)
        sigma = volatility.best_available_volatility(klines)
        if sigma is None:
            print(f"{t['symbol']} (chain {t['chainId']}): not enough kline history")
            continue
        il = il_model.expected_il_fraction(sigma)
        if args.apy:
            grade = il_model.richness_grade(il_model.vol_richness_ratio(sigma, args.apy))
            be_str = f", Richness Score @ {args.apy*100:.0f}% APY = {grade}"
        else:
            be_str = ""
        il_str = f"{il*100:.2f}%" if il is not None else "N/A (volatility too extreme for the diffusion approximation)"
        print(f"{t['symbol']} (chain {t['chainId']}): annualized vol = {sigma*100:.2f}%, "
              f"est. full-range IL/yr = {il_str}{be_str}")


def cmd_scan(args):
    started = time.time()
    log = (lambda msg: None) if args.json else (lambda msg: print(msg, file=sys.stderr))
    try:
        results, flagged, unscoreable, coverage = scan.run_scan(
            max_pages=args.max_pages, max_fee_rate=args.max_fee_rate, min_tvl=args.min_tvl,
            peer_outlier_multiple=args.peer_outlier_multiple, min_security_score=args.min_security_score,
            block_unknown_v4_hooks=not args.allow_v4, with_range=args.with_range, log=log,
        )
    except Exception as e:
        if args.json:
            print(json.dumps(config._json_envelope("error", error=str(e),
                                                     hint="run `baw auth signin` / `baw auth verify`"), indent=2))
        else:
            print(f"could not fetch LP pools: {e}", file=sys.stderr)
            print("run `baw auth signin` / `baw auth verify` first, then re-run scan.", file=sys.stderr)
        sys.exit(1)

    capital_note = None
    if args.capital and results:
        top = results[0]
        capital_note = scan.position_sizing_note(args.capital, top["tvl"], top["model_net_apy"])

    if args.json:
        # Pure JSON on stdout -- nothing else -- so a scheduler/pipeline can parse it directly.
        # Diagnostics (fetch progress, etc.) went to stderr above via `log`.
        print(json.dumps(config._json_envelope(
            "ok",
            elapsed_seconds=round(time.time() - started, 1),
            results=results[: args.top],
            flagged=flagged,
            unscoreable=unscoreable,
            failure_summary=scan._summarize_unscoreable(unscoreable),
            capital_note=capital_note,
            coverage=coverage,
            model_apy_caveat=il_model.MODEL_APY_CAVEAT,
            v4_override_reason=args.allow_v4,
        ), indent=2))
        return

    if args.with_range:
        print(f"\n{'pool':<20}{'ticker':<8}{'apy':>9}{'vol':>9}{'grade':>7}"
              f"{'best +/-%':>10}{'range-net':>11}{'confidence':>12}{'tvl':>14}  verdict")
        for r in results[: args.top]:
            b = r["best_range"]
            width = f"{(b['pb']-1)*100:.0f}%" if b["pb"] is not None else "full"
            tag = "" if r["pair_mode"] == "stablecoin" else "  [non-stablecoin pair]"
            print(f"{r['pool']:<20}{r['stock_ticker']:<8}"
                  f"{r['apy']*100:>8.2f}%{r['sigma_annual']*100:>8.2f}%{r['grade']:>7}"
                  f"{width:>10}{b['model_net_apy']*100:>10.2f}%{b['confidence']:>12}"
                  f"{r['tvl']:>14,.0f}  {r['verdict']}{tag}")
        print("\n(grade = Richness Score tier, vol_ratio bucketed Rich/Fair/Cheap; "
              "confidence = probability of the recommended range staying active a year, "
              "bucketed High/Moderate/Low. verdict = ENTER (clears the trade gate) or WATCH "
              "(safe but not attractive right now). [non-stablecoin pair] = vol is the "
              "*relative* vol between the two pooled assets, not the bStock alone -- see "
              "README. Full numbers: --json.)")
    else:
        print(f"\n{'pool':<20}{'ticker':<8}{'apy':>9}{'vol':>9}{'net_apy':>10}{'grade':>7}{'tvl':>14}  verdict")
        for r in results[: args.top]:
            tag = "" if r["pair_mode"] == "stablecoin" else "  [non-stablecoin pair]"
            print(f"{r['pool']:<20}{r['stock_ticker']:<8}"
                  f"{r['apy']*100:>8.2f}%{r['sigma_annual']*100:>8.2f}%"
                  f"{r['model_net_apy']*100:>9.2f}%{r['grade']:>7}"
                  f"{r['tvl']:>14,.0f}  {r['verdict']}{tag}")
        print("\n(grade = Richness Score tier -- realized vol vs. this pool's breakeven vol, "
              "bucketed Rich/Fair/Cheap. verdict = ENTER (clears the trade gate) or WATCH "
              "(safe but not attractive right now). [non-stablecoin pair] = vol is the "
              "*relative* vol between the two pooled assets, not the bStock alone -- see "
              "README. Full numbers: --json.)")

    coverage_note = scan._coverage_note(coverage, args.max_pages)
    if coverage_note:
        print(f"\n{coverage_note}")

    if capital_note:
        top = results[0]
        print(f"\nAt ${args.capital:,.0f} into the top pick ({top['pool']}): "
              f"~${capital_note['dollar_return']:,.0f}/yr at the current rate, "
              f"~{capital_note['share_pct']*100:.1f}% of its TVL.")
        if capital_note["warning"]:
            print(f"WARNING: {capital_note['warning']}")

    if flagged:
        print(f"\n{len(flagged)} pool(s) excluded from ranking -- pre-deposit screen flagged:")
        for f in flagged:
            print(f"  {f['pool']} ({f['protocol']}):")
            for reason in f["flags"]:
                print(f"    - {reason}")

    if unscoreable:
        print(f"\n{len(unscoreable)} pool(s) could not be evaluated at all (not the same as "
              f"'flagged' -- these were never scored, safe or not):")
        for u in unscoreable:
            print(f"  {u['pool']}: {u['reason']}")

    print(f"\n{il_model.MODEL_APY_CAVEAT}")


def cmd_range(args):
    if args.investment_id:
        try:
            info = market_data.fetch_investment_info(args.investment_id)
        except Exception as e:
            print(f"could not fetch pool {args.investment_id}: {e}", file=sys.stderr)
            print("check the investmentId is correct, or run `baw auth signin` / `baw auth verify` "
                  "if the session has expired.", file=sys.stderr)
            sys.exit(1)
        flags = risk_screen.pool_risk_flags({}, info, block_unknown_v4_hooks=not args.allow_v4)
        if flags:
            print(f"WARNING: {info.get('investmentName')} ({info.get('protocolName')}) failed the pre-deposit screen:", file=sys.stderr)
            for f in flags:
                print(f"  - {f}", file=sys.stderr)
            print("Proceeding anyway since an investmentId was given explicitly, but treat every "
                  "number below as unreliable -- do not recommend this pool.\n", file=sys.stderr)
        apy = float(info["apy"]) if info.get("apy") is not None else float(info.get("apyBps") or 0) / 10000
        stock_tokens = market_data.fetch_stock_tokens()
        stock_index = market_data.build_stock_index(stock_tokens)
        stock, chain_id, quote_addr, pair_mode = market_data.resolve_pool_stock_and_quote({}, info, stock_index)
        if not stock:
            print("could not confirm exactly one bStock paired with exactly one other asset in this "
                  "pool's assetTokenList (unsupported pool structure) -- cannot compute IL, aborting.",
                  file=sys.stderr)
            sys.exit(1)
        sigma = volatility.resolve_pool_volatility(stock, chain_id, quote_addr, pair_mode)
        pair_note = "" if pair_mode == "stablecoin" else " -- non-stablecoin pair, vol is relative to the quote asset"
        label = f"{info['investmentName']} ({stock['ticker']}){pair_note}"
        pool_tvl = float(info.get("tvl") or 0)
    else:
        if args.apy is None or args.ticker is None:
            print("provide --investmentId, or both --ticker and --apy", file=sys.stderr)
            sys.exit(1)
        tokens = market_data.fetch_stock_tokens()
        matches = [t for t in tokens if t["ticker"].upper() == args.ticker.upper()]
        if not matches:
            print(f"no stock token found for ticker {args.ticker}", file=sys.stderr)
            sys.exit(1)
        stock = matches[0]
        klines = market_data.fetch_klines(stock["chainId"], stock["contractAddress"], limit=91)
        sigma = volatility.best_available_volatility(klines)
        apy = args.apy
        label = f"{stock['symbol']} @ {apy*100:.2f}% pool APY (assumes a stablecoin-quoted pair)"
        pool_tvl = None  # no live pool -- no TVL to size a position against

    if sigma is None:
        print("not enough kline history to estimate volatility", file=sys.stderr)
        sys.exit(1)

    vol_ratio = il_model.vol_richness_ratio(sigma, apy)
    grade = il_model.richness_grade(vol_ratio)
    vr_str = f"{vol_ratio:.2f}" if vol_ratio is not None and math.isfinite(vol_ratio) else "n/a (0% APY)"
    print(f"{label} -- vol {sigma*100:.1f}%  |  Richness Score: {grade} (vol_ratio {vr_str})\n")

    rows, best = range_model.recommend_range(apy, sigma, side=args.side,
                                              target_offset=args.target_offset, band_width=args.band_width)
    if best["model_net_apy"] <= 0:
        print("WARNING: every candidate range nets <=0% after estimated IL -- this pool's fee "
              "income does not currently cover the token's volatility risk. 'recommended' below "
              "is the least-bad option, not a genuine opportunity.\n", file=sys.stderr)

    if best["pa"] is not None:  # a synthetic full-range row has no [pa,pb] to stress-test
        print("Scenario check on the recommended range (same [pa,pb], vol scaled -- "
              "sigma is a backward-looking estimate, this shows how much that matters):")
        for label_s, mult in [("Neutral (1x vol)", 1.0), ("Elevated (1.5x vol)", 1.5), ("Stress (2x vol)", 2.0)]:
            stressed = range_model.range_metrics(apy, sigma * mult, best["pa"], best["pb"])
            print(f"  {label_s:<22} net_apy {stressed['model_net_apy']*100:>8.2f}%   "
                  f"confidence {range_model.confidence_grade(stressed['p_active'])}")
        print()
    dollar_col = "{:>14}".format(f"$/yr @{args.capital:,.0f}") if args.capital else ""
    if args.side == "straddle":
        print(f"{'range':>10}{'concentration':>14}{'confidence':>12}{'eff.apy':>10}{'il':>9}{'net_apy':>10}{dollar_col}")
        for r in rows:
            width = f"+/-{(r['pb']-1)*100:.0f}%" if r["pb"] is not None else "full"
            marker = "  <- recommended" if r is best else ""
            if args.capital and r["model_net_apy"] is not None:
                dollar_cell = f"{args.capital * r['model_net_apy']:>+14,.0f}"
            elif args.capital:
                dollar_cell = "N/A".rjust(14)
            else:
                dollar_cell = ""
            print(f"{width:>10}{r['concentration']:>14.2f}{range_model.confidence_grade(r['p_active']):>12}"
                  f"{r['effective_apy']*100:>9.2f}%{_pct_or_na(r['expected_il'], 9)}{_pct_or_na(r['model_net_apy'], 10)}"
                  f"{dollar_cell}{marker}")
        print("\n(concentration = leverage on fees *and* IL from concentrating liquidity into this "
              "range, vs full-range; confidence = probability of staying in range a year, bucketed "
              "High/Moderate/Low; il = expected impermanent loss for this range (already subtracted "
              "out of eff.apy to get net_apy); recommended = best net_apy at Moderate-or-better "
              "confidence. Full numbers: --json on scan.)")
        if any(r["model_net_apy"] is None for r in rows):
            print("(N/A = this range's expected IL couldn't be estimated -- volatility this high "
                  "(> ~283% annualized) is outside where the diffusion approximation this model "
                  "uses stays valid, so it returns no number rather than a guess dressed up as "
                  "one; the concentrated-range rows above use the exact boundary IL instead, "
                  "which stays valid at any volatility, and 'recommended' is always picked from "
                  "those.)")
    else:
        print(f"{'offset':>10}{'band':>18}{'concentration':>14}{'confidence':>12}{'il':>9}{'net_apy':>10}{dollar_col}")
        for r in rows:
            offset_pct = abs((r["pa"] if args.side == "sell" else r["pb"]) - 1) * 100
            band = f"[{r['pa']:.2f}, {r['pb']:.2f}]x"
            tag = " (your target)" if r.get("is_target") else ""
            marker = "  <- recommended" if r is best else ""
            dollar_cell = f"{args.capital * r['model_net_apy']:>+14,.0f}" if args.capital else ""
            print(f"{offset_pct:>9.0f}%{band:>18}{r['concentration']:>14.2f}{range_model.confidence_grade(r['p_active']):>12}"
                  f"{r['expected_il']*100:>8.2f}%{r['model_net_apy']*100:>9.2f}%{dollar_cell}{marker}{tag}")
        verb = "rises into" if args.side == "sell" else "falls into"
        print(f"\n(yield-enhanced limit {'sell' if args.side == 'sell' else 'buy'} order -- earns fees "
              f"only once price {verb} the band; il = expected impermanent loss for this range "
              f"(already subtracted out of net_apy); confidence = probability that ever happens "
              f"within a year.)")
        if args.target_offset is None:
            print("(want an exact target instead of these presets? add "
                  "--target-offset 0.15 for +/-15% from current price, plus optional --band-width.)")

    if args.capital:
        print(f"\n($/yr @{args.capital:,.0f} above is model_net_apy simulated on your deposit size "
              f"for each range -- a model estimate, not a promised return; see the caveat below.)")
        if pool_tvl is None:
            print("(--capital given, but this is a --ticker/--apy estimate with no live pool "
                  "TVL to size a position against -- use --investmentId for a TVL-share warning too.)")
        else:
            note = scan.position_sizing_note(args.capital, pool_tvl, best["model_net_apy"])
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
        results, flagged, unscoreable, coverage = scan.run_scan(
            max_pages=args.max_pages, block_unknown_v4_hooks=not args.allow_v4,
            with_range=True, log=lambda msg: None)
    except Exception as e:
        print(f"could not fetch LP pools: {e}", file=sys.stderr)
        print("run `baw auth signin` / `baw auth verify` first, then retry.", file=sys.stderr)
        sys.exit(1)

    coverage_note = scan._coverage_note(coverage, args.max_pages)
    if coverage_note:
        print(coverage_note)

    total_candidates = len(results) + len(flagged) + len(unscoreable)
    if total_candidates and len(unscoreable) / total_candidates > scan.UNSCOREABLE_RATIO_REFUSE_THRESHOLD:
        print(f"{scan.VERDICT_NO_TRADE} -- {len(unscoreable)}/{total_candidates} candidate pools could not even be "
              f"evaluated (data/coverage issue, not a safety verdict). That's too much of the market "
              f"unaccounted for to trust a verdict from the scoreable remainder right now.")
        print(f"({scan._summarize_unscoreable(unscoreable)})")
        print("Try again shortly, or run `scan --with-range` to see exactly what failed.")
        return

    if not results:
        print(f"{scan.VERDICT_NO_TRADE} -- no bStock LP pools passed the pre-deposit screen right now.")
        if flagged:
            print(f"({len(flagged)} pool(s) were excluded -- run `scan --with-range` for details.)")
        if unscoreable:
            print(f"({len(unscoreable)} pool(s) could not even be evaluated -- data/coverage issue, not a safety verdict.)")
        return

    tradeable = [r for r in results if r["verdict"] == scan.VERDICT_ENTER]
    watch_list = [r for r in results if r["verdict"] == scan.VERDICT_WATCH]
    if not tradeable:
        best = results[0]
        reasons = []
        if best["model_net_apy"] <= 0:
            reasons.append(f"best candidate ({best['pool']}) nets {best['model_net_apy']*100:.1f}% after IL -- negative")
        if best["vol_ratio"] is None or best["vol_ratio"] >= 1:
            vr_str = f"{best['vol_ratio']:.2f}" if best["vol_ratio"] is not None else "n/a"
            reasons.append(f"best candidate grades {best['grade']} (vol_ratio {vr_str}) -- "
                            f"fee income likely doesn't cover realized risk")
        print(f"{scan.VERDICT_NO_TRADE} -- nothing currently clears the bar (positive net APY and vol_ratio < 1).")
        for r in reasons:
            print(f"  - {r}")
        print(f"\nClosest candidate for reference: {best['pool']} ({best['stock_ticker']}), "
              f"{best['grade']}, {best['model_net_apy']*100:.1f}% net APY. Not a recommendation.")
        if watch_list:
            print(f"\n{len(watch_list)} pool(s) safe but not attractive right now ({scan.VERDICT_WATCH}) -- "
                  f"see `scan --with-range` to browse them.")
        print(f"\n{il_model.MODEL_APY_CAVEAT}")
        return

    top = tradeable[0]
    b = top["best_range"]
    width = f"+/-{(b['pb']-1)*100:.0f}%" if b["pb"] is not None else "full range"
    pair_note = "" if top["pair_mode"] == "stablecoin" else " (non-stablecoin pair -- vol is relative to the quote asset, see README)"
    print(f"{scan.VERDICT_ENTER}: {top['pool']} ({top['stock_ticker']}) -- {top['grade']}, "
          f"{width} range at {b['confidence']} confidence, {b['model_net_apy']*100:.1f}% net APY.{pair_note}\n")

    print(f"{'pool':<20}{'ticker':<8}{'grade':>7}{'net_apy':>10}{'tvl':>14}  verdict")
    for r in results[:3]:
        print(f"{r['pool']:<20}{r['stock_ticker']:<8}{r['grade']:>7}{r['model_net_apy']*100:>9.2f}%{r['tvl']:>14,.0f}  {r['verdict']}")

    if watch_list:
        print(f"\n{len(watch_list)} more pool(s) are {scan.VERDICT_WATCH} -- safe, but don't currently clear the "
              f"trade gate (see `scan --with-range` for the full list).")

    if args.capital:
        note = scan.position_sizing_note(args.capital, top["tvl"], top["model_net_apy"])
        print(f"\nAt ${args.capital:,.0f}: ~${note['dollar_return']:,.0f}/yr, "
              f"~{note['share_pct']*100:.1f}% of {top['pool']}'s TVL.")
        if note["warning"]:
            print(f"WARNING: {note['warning']}")

    try:
        data = market_data.fetch_positions()
        stock_tokens = market_data.fetch_stock_tokens()
        stock_index = market_data.build_stock_index(stock_tokens)
        held = market_data.lp_positions_on_stock_tokens(data, stock_index)
        portfolio_error = None
    except Exception as e:
        # A fetch failure (expired session, network error, malformed response) is not the
        # same claim as "we checked and you hold nothing" -- conflating them (as this used to
        # do by just falling back to held=[]) can tell a user they have no positions when the
        # real answer is "we don't know." Surface it distinctly instead.
        held, portfolio_error = [], str(e)

    if portfolio_error:
        print(f"\nCould not check your current bStock LP positions ({portfolio_error}) -- "
              f"this is NOT the same as confirming you hold none. Run `positions` directly to retry.")
    elif held:
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
    if unscoreable:
        print(f"({len(unscoreable)} pool(s) could not be evaluated at all -- data/coverage issue, not a safety verdict.)")

    print(f"\n{il_model.MODEL_APY_CAVEAT}")


def cmd_positions(args):
    try:
        data = market_data.fetch_positions(refresh=args.refresh)
    except Exception as e:
        if args.json:
            print(json.dumps(config._json_envelope("error", error=str(e),
                                                     hint="run `baw auth signin` / `baw auth verify`"), indent=2))
        else:
            print(f"could not fetch positions: {e}", file=sys.stderr)
            print("run `baw auth signin` / `baw auth verify` first, then retry.", file=sys.stderr)
        sys.exit(1)
    total = float(data.get("deFiTotalValue") or 0)
    stock_tokens = market_data.fetch_stock_tokens()
    stock_index = market_data.build_stock_index(stock_tokens)
    hits = market_data.lp_positions_on_stock_tokens(data, stock_index)

    if args.json:
        print(json.dumps(config._json_envelope("ok", total_defi_value_usd=total, positions=hits),
                          indent=2, default=str))
        return

    print(f"total DeFi value: ${total:,.2f}\n")
    if not hits:
        print("no LP positions on tokenized-stock pairs found.")
        return
    for h in hits:
        supply_str = ", ".join(f"{t['tokenSymbol']} {t.get('tokenAmount', '?')} (${float(t.get('tokenValue') or 0):,.2f})"
                                for t in h["supply"])
        print(f"{h['protocolName']} | {h['stock']['ticker']} | nftId={h['nftId']} | {supply_str}")


REBALANCE_ATTENTION_GAP = 1.5  # fallback "needs attention" bar (vol_ratio multiple) when a dollar
                                 # switching estimate can't be computed -- see _switching_recommendation

# Rough BSC gas ballpark for a remove-liquidity + add-liquidity round trip -- a documented
# assumption, not a measured cost (this tool has no live gas-price feed). Does NOT include any
# IL realized at the moment of exit either, since there's no entry-price/cost-basis data
# available to compute that -- a known, stated gap, not hidden in the number.
ASSUMED_SWITCH_COST_USD = 2.0
SWITCH_PAYBACK_DAYS_WORTHWHILE = 30  # "switch" verdict only if the gap pays back this fast


def _best_alternative_for_ticker(ticker, market_results, exclude_investment_ids=()):
    """The best-vol_ratio market survivor for this ticker, excluding pools already held -- a
    concrete pool (protocol/tvl/apy/model_net_apy/vol_ratio/...), not just a bare ratio number,
    so rebalance-check can name an actual alternative instead of only comparing grades."""
    candidates = [r for r in market_results if r["stock_ticker"] == ticker
                  and r["investmentId"] not in exclude_investment_ids and r["vol_ratio"] is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda r: r["vol_ratio"])


def _switching_recommendation(position_usd, held_model_net_apy, alt_model_net_apy):
    """Whether switching to a concrete alternative pool is worth it, given the position's
    current USD value and the model_net_apy gap between what's held and the alternative.

    Returns {"verdict": "switch"|"stay", "reason", "annual_gap_usd", "payback_days",
    "switch_cost_usd_assumed"}. "switch" only when the estimated payback period clears
    SWITCH_PAYBACK_DAYS_WORTHWHILE -- a real per-position dollar estimate grounded in the
    position's own USD value, not a bare grade/ratio comparison, but still bounded by what this
    tool can actually measure: no live gas price, no realized-IL-at-exit data. The reason text
    says so explicitly whenever a "switch" verdict is given, so the number isn't mistaken for
    the full real-world cost of moving.
    """
    apy_gap = alt_model_net_apy - held_model_net_apy
    annual_gap_usd = position_usd * apy_gap
    if annual_gap_usd <= 0:
        return {"verdict": "stay", "reason": "the alternative doesn't actually net more after IL -- stay put",
                "annual_gap_usd": annual_gap_usd, "payback_days": None,
                "switch_cost_usd_assumed": ASSUMED_SWITCH_COST_USD}
    payback_days = ASSUMED_SWITCH_COST_USD / (annual_gap_usd / 365)
    if payback_days <= SWITCH_PAYBACK_DAYS_WORTHWHILE:
        return {"verdict": "switch",
                "reason": f"~${annual_gap_usd:,.0f}/yr gap pays back the assumed ${ASSUMED_SWITCH_COST_USD:.0f} "
                          f"switching cost in ~{payback_days:.0f} days -- doesn't include current gas price "
                          f"or any IL realized on exit",
                "annual_gap_usd": annual_gap_usd, "payback_days": payback_days,
                "switch_cost_usd_assumed": ASSUMED_SWITCH_COST_USD}
    return {"verdict": "stay",
            "reason": f"gap exists (~${annual_gap_usd:,.0f}/yr) but estimated payback "
                      f"(~{payback_days:.0f} days) is too slow to bother switching for",
            "annual_gap_usd": annual_gap_usd, "payback_days": payback_days,
            "switch_cost_usd_assumed": ASSUMED_SWITCH_COST_USD}


def _peer_apys_for_ticker(ticker, market_results, exclude_investment_id=None):
    """Peer apy values for the outlier check, drawn from the already-scanned market survivors
    on the same ticker. Narrower than run_scan's own peer set (which also includes pools that
    failed the screen, before they're excluded) since a flagged pool's apy isn't available
    here -- a documented, minor scope narrowing: it can only make the outlier check slightly
    less sensitive for this fallback path, never wrongly permissive in a way the rest of the
    screen wouldn't also independently catch."""
    return [r["apy"] for r in market_results
            if r["stock_ticker"] == ticker and r["investmentId"] != exclude_investment_id]


def _evaluate_held_investment_id(investment_id, stock_index, market_results, allow_v4):
    """Evaluate one held investmentId that fell outside the scanned market set (e.g. ranked
    outside the fetched pages), via the same evaluate_pool() path scan uses -- including
    peer_apys and protocol_security_score, so this fallback can't reach a laxer conclusion
    than scan would have for the same pool. Returns (vol_ratio, model_net_apy, flags, evaluated)
    -- both are None when flagged or unevaluated; model_net_apy alone can also be None on its
    own when the pool's volatility is too extreme for expected_il_fraction to produce a valid
    estimate (vol_ratio is unaffected either way -- see il_model.risk_adjusted_apy)."""
    try:
        info = market_data.fetch_investment_info(investment_id)
        stock, chain_id, quote_addr, pair_mode = market_data.resolve_pool_stock_and_quote({}, info, stock_index)
        sigma = volatility.resolve_pool_volatility(stock, chain_id, quote_addr, pair_mode) if stock else None
        if not (sigma and sigma > 0):
            return None, None, [], False
        apy = float(info["apy"]) if info.get("apy") is not None else float(info.get("apyBps") or 0) / 10000
        peer_apys = _peer_apys_for_ticker(stock["ticker"], market_results, exclude_investment_id=investment_id)
        protocol_id = info.get("defiProtocolId")
        security_score = market_data.fetch_protocol_security_score(protocol_id, {}) if protocol_id else None
        evaluation = risk_screen.evaluate_pool({}, info, sigma, apy, peer_apys=peer_apys,
                                                protocol_security_score=security_score,
                                                block_unknown_v4_hooks=not allow_v4)
        if evaluation["flags"]:
            return None, None, evaluation["flags"], True
        scored = evaluation["scored"]
        return scored["vol_ratio"], scored["model_net_apy"], [], True
    except Exception:
        return None, None, [], False


def cmd_rebalance_check(args):
    started = time.time()
    log = (lambda msg: None) if args.json else (lambda msg: print(msg, file=sys.stderr))
    log("reading current positions...")
    try:
        data = market_data.fetch_positions()
    except Exception as e:
        if args.json:
            print(json.dumps(config._json_envelope("error", error=str(e),
                                                     hint="run `baw auth signin` / `baw auth verify`"), indent=2))
        else:
            print(f"could not fetch positions: {e}", file=sys.stderr)
            print("run `baw auth signin` / `baw auth verify` first, then retry.", file=sys.stderr)
        sys.exit(1)
    stock_tokens = market_data.fetch_stock_tokens()
    stock_index = market_data.build_stock_index(stock_tokens)
    held = market_data.lp_positions_on_stock_tokens(data, stock_index)
    if not held:
        payload = config._json_envelope("ok", positions=[], any_needs_attention=False, v4_override_reason=args.allow_v4)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print("no LP positions on tokenized-stock pairs to check.")
        return

    # Same evaluation path as `scan` (run_scan -> evaluate_pool), not a separate stripped-down
    # check -- before this, rebalance-check's own market-comparison loop skipped peer_apys and
    # protocol_security_score entirely, so a pool `scan` would flag (or now, an unknown-hook V4
    # pool) could still show up here as a "better" rebalance target. One evaluation path means
    # that can't happen by construction.
    log("scanning current market for comparison (shared evaluation path with `scan`)...")
    try:
        market_results, market_flagged, _, market_coverage = scan.run_scan(
            max_pages=args.max_pages, block_unknown_v4_hooks=not args.allow_v4,
            with_range=False, log=lambda msg: None)
    except Exception as e:
        if args.json:
            print(json.dumps(config._json_envelope("error", error=str(e)), indent=2))
        else:
            print(f"could not fetch market pools: {e}", file=sys.stderr)
        sys.exit(1)

    coverage_note = scan._coverage_note(market_coverage, args.max_pages)
    if coverage_note and not args.json:
        print(coverage_note, file=sys.stderr)

    market_by_id = {r["investmentId"]: r for r in market_results}
    flagged_by_id = {f["investmentId"]: f for f in market_flagged}

    if not args.json:
        print(f"\n{'held pool (ticker)':<28}{'held grade':>14}{'best market grade':>20}")
    rows = []
    for h in held:
        inv_ids = h["investmentIds"] or []
        per_id = []
        for inv_id in inv_ids:
            if inv_id in market_by_id:
                per_id.append({"investmentId": inv_id, "vol_ratio": market_by_id[inv_id]["vol_ratio"],
                                "model_net_apy": market_by_id[inv_id]["model_net_apy"],
                                "flags": [], "evaluated": True})
            elif inv_id in flagged_by_id:
                per_id.append({"investmentId": inv_id, "vol_ratio": None, "model_net_apy": None,
                                "flags": flagged_by_id[inv_id]["flags"], "evaluated": True})
            else:
                # Wasn't in the scanned market set at all (e.g. ranked outside the fetched
                # pages) -- evaluate it directly via the same evaluate_pool() path rather than
                # silently skip it.
                ratio, model_apy, flags, evaluated = _evaluate_held_investment_id(
                    inv_id, stock_index, market_results, args.allow_v4)
                per_id.append({"investmentId": inv_id, "vol_ratio": ratio, "model_net_apy": model_apy,
                                "flags": flags, "evaluated": evaluated})

        # A held "position" can carry more than one investmentId (see lp_positions_on_stock_tokens);
        # evaluating only the first silently hid risk on the rest. Report every id, and take the
        # worst case across all of them -- a flag on ANY of them means the position is flagged,
        # the vol_ratio shown is the worst (highest) among the ones that could be scored, and the
        # model_net_apy used for the switching-cost estimate below is the worst (lowest) among
        # them too -- never an average that a bad instance could hide behind a good one.
        held_flags = [f for r in per_id for f in r["flags"]]
        evaluated_ratios = [r["vol_ratio"] for r in per_id if r["vol_ratio"] is not None]
        held_ratio = max(evaluated_ratios) if evaluated_ratios else None
        evaluated_apys = [r["model_net_apy"] for r in per_id if r["model_net_apy"] is not None]
        held_model_net_apy = min(evaluated_apys) if evaluated_apys else None
        unevaluated_count = sum(1 for r in per_id if not r["evaluated"]) if inv_ids else 1

        for f in held_flags:
            log(f"  WARNING: your held {h['protocolName']} position itself: {f}")
        if unevaluated_count:
            log(f"  WARNING: {unevaluated_count}/{len(inv_ids) or 1} investmentId(s) on your held "
                f"{h['protocolName']} ({h['stock']['ticker']}) position could not be evaluated at all")

        position_usd = sum(float(t.get("tokenValue") or 0) for t in h["supply"])
        best_alt = _best_alternative_for_ticker(h["stock"]["ticker"], market_results,
                                                 exclude_investment_ids=set(inv_ids))
        best_market_ratio = best_alt["vol_ratio"] if best_alt else None
        switching = None
        if best_alt is not None and held_model_net_apy is not None:
            switching = _switching_recommendation(position_usd, held_model_net_apy, best_alt["model_net_apy"])

        # "needs attention": the held pool itself is flagged, some of it couldn't even be
        # evaluated (silence here is not reassurance), or a concrete alternative's dollar payback
        # clears the bar in _switching_recommendation. Falls back to the older bare vol_ratio-gap
        # heuristic only when a dollar estimate couldn't be computed at all (e.g. the held
        # position's own apy is unavailable) -- still better than no signal.
        if switching is not None:
            worth_switching = switching["verdict"] == "switch"
        else:
            worth_switching = (
                held_ratio is not None and best_market_ratio is not None and best_market_ratio > 0
                and held_ratio > best_market_ratio * REBALANCE_ATTENTION_GAP
            )
        needs_attention = bool(held_flags) or bool(unevaluated_count) or worth_switching

        if not args.json:
            label = f"{h['protocolName']} ({h['stock']['ticker']})"
            tag = "  <- needs attention" if needs_attention else ""
            if unevaluated_count:
                tag += f" ({unevaluated_count}/{len(inv_ids) or 1} investmentId(s) unevaluated)"
            print(f"{label:<28}{il_model.richness_grade(held_ratio):>14}{il_model.richness_grade(best_market_ratio):>20}{tag}")
            if best_alt:
                print(f"  best alternative: {best_alt['pool']} ({best_alt['protocol']}, "
                      f"${best_alt['tvl']:,.0f} TVL, {best_alt['model_net_apy']*100:.1f}% net APY)")
            if switching:
                verdict_label = "SWITCH" if switching["verdict"] == "switch" else "stay put"
                print(f"  {verdict_label}: {switching['reason']}")
        rows.append({
            "protocol": h["protocolName"], "ticker": h["stock"]["ticker"], "position_usd": position_usd,
            "held_vol_ratio": held_ratio, "held_grade": il_model.richness_grade(held_ratio),
            "held_model_net_apy": held_model_net_apy,
            "best_market_vol_ratio": best_market_ratio, "best_market_grade": il_model.richness_grade(best_market_ratio),
            "best_alternative": best_alt, "switching": switching,
            "held_flags": held_flags, "investment_ids": per_id,
            "unevaluated_count": unevaluated_count, "needs_attention": needs_attention,
        })

    if args.json:
        print(json.dumps(config._json_envelope(
            "ok",
            elapsed_seconds=round(time.time() - started, 1),
            positions=rows,
            any_needs_attention=any(r["needs_attention"] for r in rows),
            market_coverage=market_coverage,
            v4_override_reason=args.allow_v4,
        ), indent=2))
    else:
        print("\nRecommendation only -- nothing moved. To act, use `defi redeem`/`lp-remove` then "
              "`defi deposit`/`lp-add` via binance-agentic-wallet's confirmed flow.")


def _positive_int(s):
    v = int(s)
    if v <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {v}")
    return v


def _nonneg_float(s):
    v = float(s)
    if v < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {v}")
    return v


def _apy_fraction(s):
    """APY as a decimal fraction (0.30 = 30%). Fee APY can't be negative -- unlike net_apy
    (which nets out estimated IL and legitimately can be negative), the raw fee rate itself
    is bounded below by zero. Cap the top end at a generous but finite ceiling to catch a
    stray extra zero (e.g. 300000%) rather than let it silently propagate."""
    v = float(s)
    if not (0 <= v <= 100):
        raise argparse.ArgumentTypeError(f"must be a non-negative decimal fraction, 0-100 (0.30 = 30%%), got {v}")
    return v


def _offset_fraction(s):
    """A price-ratio offset/width (e.g. --target-offset, --band-width). Must keep price
    positive (> -1) and stay within a sane range -- not literally unbounded."""
    v = float(s)
    if not (-0.99 < v < 10):
        raise argparse.ArgumentTypeError(f"must be between -0.99 and 10 (0.15 = 15%%), got {v}")
    return v


def _v4_override_reason(s):
    """--allow-v4 takes a reason, not a bare flag -- overriding a hard block that exists
    specifically because this tool cannot see hook risk should leave a record of *why* someone
    decided to do it anyway, not just that they did. Recorded verbatim in --json output
    (v4_override_reason) wherever the command produces JSON."""
    s = s.strip()
    if not s:
        raise argparse.ArgumentTypeError(
            "requires a non-empty reason, e.g. --allow-v4 \"already audited by X\"")
    return s


def build_parser():
    """Split out from main() so tests can build a real argparse Namespace
    (build_parser().parse_args([...])) instead of hand-constructing one -- guarantees test args
    match the actual CLI contract (including every default) and can't silently drift from it."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_stocks = sub.add_parser("stocks", help="list tokenized-stock tokens")
    p_stocks.add_argument("--type", type=int, default=config.BSTOCK_TYPE,
                           help="1=Ondo, 2=xStocks, 3=bStock (default -- this product's focus); 0=all platforms")
    p_stocks.add_argument("--limit", type=int, default=20)
    p_stocks.set_defaults(func=cmd_stocks)

    p_vol = sub.add_parser("vol", help="compute annualized volatility + est. IL for one ticker")
    p_vol.add_argument("--ticker", required=True)
    p_vol.add_argument("--days", type=_positive_int, default=30)
    p_vol.add_argument("--apy", type=_apy_fraction, default=None, help="optional: also show breakeven vol at this pool APY (0.30 = 30%%)")
    p_vol.set_defaults(func=cmd_vol)

    p_scan = sub.add_parser("scan", help="rank stock-token LP pools by risk-adjusted (net of IL) APY")
    p_scan.add_argument("--top", type=_positive_int, default=15)
    p_scan.add_argument("--json", action="store_true")
    p_scan.add_argument("--capital", type=_nonneg_float, default=None,
                         help="optional: intended deposit size in USD -- shows expected $ return and "
                              "a concentration warning if you'd dominate the top pick's TVL")
    p_scan.add_argument("--max-pages", type=_positive_int, default=3,
                         help="pages of LP pools to fetch (100/page, sorted by apy DESC); higher "
                              "reaches lower-apy pools a single page would miss, at the cost of more API calls")
    p_scan.add_argument("--max-fee-rate", type=_nonneg_float, default=risk_screen.MAX_SANE_FEE_RATE,
                         help=f"pre-deposit screen: max sane feeRate per swap (default {risk_screen.MAX_SANE_FEE_RATE})")
    p_scan.add_argument("--min-tvl", type=_nonneg_float, default=risk_screen.MIN_SANE_TVL_USD,
                         help=f"pre-deposit screen: minimum pool TVL in USD (default {risk_screen.MIN_SANE_TVL_USD:.0f})")
    p_scan.add_argument("--peer-outlier-multiple", type=_nonneg_float, default=risk_screen.PEER_APY_OUTLIER_MULTIPLE,
                         help=f"pre-deposit screen: flag apy above this multiple of peer median (default {risk_screen.PEER_APY_OUTLIER_MULTIPLE})")
    p_scan.add_argument("--min-security-score", type=_nonneg_float, default=risk_screen.MIN_PROTOCOL_SECURITY_SCORE,
                         help=f"pre-deposit screen: minimum protocol securityScore, 0-100 (default {risk_screen.MIN_PROTOCOL_SECURITY_SCORE})")
    p_scan.add_argument("--allow-v4", type=_v4_override_reason, default=None, metavar="REASON",
                         help="override the V4-generation hard block (see README) for an already-vetted pool "
                              "or explicit ask -- requires a reason, recorded in --json output "
                              "(e.g. --allow-v4 \"audited by X\")")
    p_scan.add_argument("--with-range", action="store_true",
                         help="also compute the recommended concentrated-liquidity range per pool")
    p_scan.set_defaults(func=cmd_scan)

    p_range = sub.add_parser("range", help="range-by-range IL/APY breakdown + recommended range for one pool")
    p_range.add_argument("--investmentId", dest="investment_id", help="pull live apy/ticker for this pool")
    p_range.add_argument("--ticker", help="alternative to --investmentId: stock ticker")
    p_range.add_argument("--apy", type=_apy_fraction, help="alternative to --investmentId: pool APY as a decimal (0.30 = 30%%)")
    p_range.add_argument("--side", choices=["straddle", "sell", "buy"], default="straddle",
                          help="straddle=symmetric market-making (default), sell/buy=single-sided limit-order-style range")
    p_range.add_argument("--target-offset", type=_offset_fraction, default=None,
                          help="sell/buy only: exact offset from current price (0.15 = 15%%) to evaluate "
                               "in addition to the preset sweep -- for a specific target price, not just presets")
    p_range.add_argument("--band-width", type=_offset_fraction, default=range_model.SIDED_BAND_WIDTH,
                          help=f"sell/buy only: width of the --target-offset band (default {range_model.SIDED_BAND_WIDTH})")
    p_range.add_argument("--capital", type=_nonneg_float, default=None,
                          help="optional (--investmentId only): intended deposit size in USD -- shows "
                               "expected $ return and a concentration warning vs this pool's TVL")
    p_range.add_argument("--allow-v4", type=_v4_override_reason, default=None, metavar="REASON",
                          help="override the V4-generation hard block -- requires a reason "
                               "(e.g. --allow-v4 \"audited by X\"); the warning still prints either way "
                               "for an explicit --investmentId, this only affects whether the flag is raised")
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
    p_rebalance.add_argument("--max-pages", type=_positive_int, default=3,
                              help="pages of the market to scan for comparison (default 3)")
    p_rebalance.add_argument("--allow-v4", type=_v4_override_reason, default=None, metavar="REASON",
                              help="override the V4-generation hard block when comparing -- requires a reason, "
                                   "recorded in --json output (e.g. --allow-v4 \"audited by X\")")
    p_rebalance.set_defaults(func=cmd_rebalance_check)

    p_recommend = sub.add_parser("recommend",
                                  help="single entry point: top pick + range + a check on any held positions, one verdict")
    p_recommend.add_argument("--max-pages", type=_positive_int, default=1,
                              help="pages of LP pools to scan (default 1, for a fast verdict; "
                                   "use `scan --max-pages` directly for the thorough sweep)")
    p_recommend.add_argument("--capital", type=_nonneg_float, default=None,
                              help="optional: intended deposit size in USD -- shows expected $ return and a concentration warning")
    p_recommend.add_argument("--allow-v4", type=_v4_override_reason, default=None, metavar="REASON",
                              help="override the V4-generation hard block -- requires a reason "
                                   "(e.g. --allow-v4 \"audited by X\")")
    p_recommend.set_defaults(func=cmd_recommend)

    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)
