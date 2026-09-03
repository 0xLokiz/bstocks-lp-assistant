"""Mocked integration tests: exercise run_scan/cmd_* end-to-end through baw()/_get() without
touching the real network or a real baw session. Complements test_riskscreen.py (pure functions
only, no I/O) -- this file is where the wiring between fetch/evaluate/report gets checked.

Mocking strategy: patch the two leaf I/O functions, baw() and _get(), with fake dispatchers
keyed by call signature. Every higher-level fetch_*/cmd_* function is exercised for real; only
the actual subprocess spawn / HTTP request is replaced.
"""

import json

import pytest

from bstocks_lp import api, cli, config, market_data, range_model, scan

DAY_MS = 24 * 60 * 60 * 1000
USDT_BSC = "0x55d398326f99059ff775485246999027b3197955"
BNB_NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"


def make_price_klines(n, start_price=100.0, daily_return=0.0, start_time=0):
    """n densely-spaced daily OHLC candles at a constant daily return -- realistic shape for
    integration fixtures where the exact volatility number doesn't matter, only that it's
    computable at all (clears the sample-count/coverage floors)."""
    rows = []
    price = start_price
    for i in range(n):
        t = start_time + i * DAY_MS
        o = price
        c = price * (1 + daily_return)
        h = max(o, c) * 1.01
        lo = min(o, c) * 0.99
        rows.append([t, str(o), str(h), str(lo), str(c), "1000", t])
        price = c
    return rows


class FakeBaw:
    """Routes baw(*args) to canned responses by subcommand, so run_scan/cmd_* can run for real
    without an actual baw session. Unrecognized calls raise loudly (AssertionError) rather than
    silently returning something misleading."""

    def __init__(self):
        self.investment_list_page1 = []
        self.investment_list_extra_pages: dict = {}  # page number (2+) -> pool list, default empty
        self.investment_list_total = None  # None -> defaults to len(page1), i.e. not truncated
        self.investment_info = {}       # investmentId -> info dict
        self.protocol_security_score = {}  # defiProtocolId -> score
        self.positions: dict = {"deFiProtocolVOList": []}
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        head = args[0] if args else None
        if head == "defi" and args[1] == "investment-list":
            page = int(args[args.index("--page") + 1])
            pools = self.investment_list_page1 if page == 1 else self.investment_list_extra_pages.get(page, [])
            total = (self.investment_list_total if self.investment_list_total is not None
                     else len(self.investment_list_page1))
            return {"success": True, "data": {"list": pools, "total": total}}
        if head == "defi" and args[1] == "investment-info":
            inv_id = args[args.index("--investmentId") + 1]
            info = self.investment_info.get(inv_id)
            if info is None:
                return {"success": False, "error": {"message": f"investment {inv_id} not found"}}
            return {"success": True, "data": info}
        if head == "defi" and args[1] == "protocol-info":
            pid = args[args.index("--defiProtocolId") + 1]
            score = self.protocol_security_score.get(pid, 90)
            return {"success": True, "data": {"securityScore": score}}
        if head == "defi" and args[1] == "position":
            return {"success": True, "data": self.positions}
        raise AssertionError(f"FakeBaw: unhandled call {args!r}")


class FakeGet:
    """Routes _get(url, params) to canned responses for the RWA stock list + kline endpoints."""

    def __init__(self):
        self.stock_tokens = []
        self.klines = {}  # (chainId, contractAddress-lowercased) -> kline rows

    def __call__(self, url, params):
        if url == market_data.RWA_LIST_URL:
            return {"success": True, "data": self.stock_tokens}
        if url == market_data.KLINE_URL:
            key = (str(params["chainId"]), params["contractAddress"].lower())
            return {"success": True, "data": {"klineInfos": self.klines.get(key, [])}}
        raise AssertionError(f"FakeGet: unhandled url {url!r}")


@pytest.fixture(autouse=True)
def _clear_memoized_fetch_caches():
    # fetch_stock_tokens/fetch_klines are functools.lru_cache'd at module scope (deliberately,
    # for a single real CLI process -- see bstocks_lp/market_data.py). Across tests in one
    # pytest process that cache would leak fixture data between tests, so clear it before and
    # after every test.
    market_data.fetch_stock_tokens.cache_clear()
    market_data.fetch_klines.cache_clear()
    yield
    market_data.fetch_stock_tokens.cache_clear()
    market_data.fetch_klines.cache_clear()


@pytest.fixture
def fake_io(monkeypatch):
    fb, fg = FakeBaw(), FakeGet()
    monkeypatch.setattr(api, "baw", fb)
    monkeypatch.setattr(api, "_get", fg)
    return fb, fg


def nvda_token():
    return {"chainId": "56", "contractAddress": "0xaaa0000000000000000000000000000000000a",
            "symbol": "NVDAB", "ticker": "NVDA", "type": 3, "multiplier": "1.0"}


def gme_token():
    return {"chainId": "56", "contractAddress": "0xbbb0000000000000000000000000000000000b",
            "symbol": "GMEB", "ticker": "GME", "type": 3, "multiplier": "1.0"}


def pool_entry(investment_id, name, protocol_name, defi_protocol_id, tvl="50000", apy_bps=3000):
    return {"binanceChainId": "56", "defiProtocolId": defi_protocol_id, "protocolName": protocol_name,
            "investmentId": investment_id, "investmentName": name, "investType": "LiquidityPool",
            "apyType": "APR", "apy": None, "tvl": tvl, "apyBps": apy_bps,
            "apyDisplay": f"{apy_bps / 100:.2f}%"}


def info_entry(investment_name, protocol_name, defi_protocol_id, asset_token_list, tvl="50000",
                fee_rate="0.0025", apy_bps=3000):
    return {"binanceChainId": "56", "defiProtocolId": defi_protocol_id, "protocolName": protocol_name,
            "investmentName": investment_name, "investType": "LiquidityPool", "investable": True,
            "apy": None, "tvl": tvl, "poolAddress": "0xpool", "feeRate": fee_rate,
            "assetTokenList": asset_token_list, "rewardTokenList": [], "apyBps": apy_bps,
            "apyDisplay": f"{apy_bps / 100:.2f}%"}


# ---- run_scan end-to-end ----

def test_run_scan_clean_stablecoin_pool(fake_io):
    fb, fg = fake_io
    fg.stock_tokens = [nvda_token()]
    fb.investment_list_page1 = [pool_entry("inv1", "NVDAB-USDT", "PancakeSwap V3", "pancakeswap3")]
    fb.investment_info["inv1"] = info_entry(
        "NVDAB-USDT", "PancakeSwap V3", "pancakeswap3",
        [{"tokenAddress": nvda_token()["contractAddress"], "tokenSymbol": "NVDAB"},
         {"tokenAddress": USDT_BSC, "tokenSymbol": "USDT"}])
    fg.klines[("56", nvda_token()["contractAddress"].lower())] = make_price_klines(60, daily_return=0.01)

    results, flagged, unscoreable, coverage = scan.run_scan(max_pages=1)

    assert unscoreable == []
    assert flagged == []
    assert len(results) == 1
    assert results[0]["investmentId"] == "inv1"
    assert results[0]["pair_mode"] == "stablecoin"
    assert results[0]["stock_ticker"] == "NVDA"
    assert results[0]["model_net_apy"] > 0
    assert results[0]["verdict"] == scan.VERDICT_ENTER


def test_run_scan_blocks_v4_generation_pool_pancakeswap_infinity_fixture(fake_io):
    # Locks in a real finding from this session: PancakeSwap's own V4 is marketed as
    # "PancakeSwap Infinity" with no "v4"/"V4" substring in the display name anywhere --
    # defiProtocolId="pancakeswap4" is what actually identifies it. This is the exact live
    # response shape observed when the defiProtocolId-based detection fix was made.
    fb, fg = fake_io
    fg.stock_tokens = [gme_token()]
    fb.investment_list_page1 = [pool_entry("inv_infinity", "USDT-GMEB", "PancakeSwap Infinity", "pancakeswap4")]
    fb.investment_info["inv_infinity"] = info_entry(
        "USDT-GMEB", "PancakeSwap Infinity", "pancakeswap4",
        [{"tokenAddress": USDT_BSC, "tokenSymbol": "USDT"},
         {"tokenAddress": gme_token()["contractAddress"], "tokenSymbol": "GMEB"}])
    fg.klines[("56", gme_token()["contractAddress"].lower())] = make_price_klines(60, daily_return=0.005)

    results, flagged, unscoreable, coverage = scan.run_scan(max_pages=1)

    assert results == []
    assert unscoreable == []
    assert len(flagged) == 1
    assert flagged[0]["investmentId"] == "inv_infinity"
    assert any("V4-generation" in f for f in flagged[0]["flags"])
    assert flagged[0]["verdict"] == scan.VERDICT_NO_TRADE


def test_run_scan_allow_v4_override_unblocks_pancakeswap_infinity(fake_io):
    fb, fg = fake_io
    fg.stock_tokens = [gme_token()]
    fb.investment_list_page1 = [pool_entry("inv_infinity", "USDT-GMEB", "PancakeSwap Infinity", "pancakeswap4")]
    fb.investment_info["inv_infinity"] = info_entry(
        "USDT-GMEB", "PancakeSwap Infinity", "pancakeswap4",
        [{"tokenAddress": USDT_BSC, "tokenSymbol": "USDT"},
         {"tokenAddress": gme_token()["contractAddress"], "tokenSymbol": "GMEB"}])
    fg.klines[("56", gme_token()["contractAddress"].lower())] = make_price_klines(60, daily_return=0.005)

    results, flagged, unscoreable, coverage = scan.run_scan(max_pages=1, block_unknown_v4_hooks=False)

    assert flagged == []
    assert len(results) == 1
    assert results[0]["investmentId"] == "inv_infinity"


def test_run_scan_non_stablecoin_pair_scores_relative_volatility(fake_io):
    fb, fg = fake_io
    fg.stock_tokens = [nvda_token()]
    fb.investment_list_page1 = [pool_entry("inv_bnb", "NVDAB-BNB", "PancakeSwap V3", "pancakeswap3")]
    fb.investment_info["inv_bnb"] = info_entry(
        "NVDAB-BNB", "PancakeSwap V3", "pancakeswap3",
        [{"tokenAddress": nvda_token()["contractAddress"], "tokenSymbol": "NVDAB"},
         {"tokenAddress": BNB_NATIVE, "tokenSymbol": "BNB"}])
    fg.klines[("56", nvda_token()["contractAddress"].lower())] = make_price_klines(60, daily_return=0.01)
    fg.klines[("56", BNB_NATIVE.lower())] = make_price_klines(60, daily_return=0.003)

    results, flagged, unscoreable, coverage = scan.run_scan(max_pages=1)

    assert unscoreable == []
    assert len(results) == 1
    assert results[0]["pair_mode"] == "non_stablecoin"


def test_run_scan_unsupported_three_asset_pool_goes_to_unscoreable(fake_io):
    fb, fg = fake_io
    fg.stock_tokens = [nvda_token()]
    fb.investment_list_page1 = [pool_entry("inv_three", "NVDAB-USDT-BNB", "Balancer", "balancer2")]
    fb.investment_info["inv_three"] = info_entry(
        "NVDAB-USDT-BNB", "Balancer", "balancer2",
        [{"tokenAddress": nvda_token()["contractAddress"], "tokenSymbol": "NVDAB"},
         {"tokenAddress": USDT_BSC, "tokenSymbol": "USDT"},
         {"tokenAddress": BNB_NATIVE, "tokenSymbol": "BNB"}])

    results, flagged, unscoreable, coverage = scan.run_scan(max_pages=1)

    assert results == []
    assert flagged == []
    assert len(unscoreable) == 1
    assert "unsupported pool structure" in unscoreable[0]["reason"]


def test_run_scan_investment_info_fetch_failure_reported_not_dropped(fake_io):
    fb, fg = fake_io
    fg.stock_tokens = [nvda_token()]
    # investmentId deliberately absent from fb.investment_info -> FakeBaw returns success=False
    fb.investment_list_page1 = [pool_entry("inv_missing", "NVDAB-USDT", "PancakeSwap V3", "pancakeswap3")]

    results, flagged, unscoreable, coverage = scan.run_scan(max_pages=1)

    assert results == []
    assert flagged == []
    assert len(unscoreable) == 1
    assert "investment-info fetch failed" in unscoreable[0]["reason"]
    assert unscoreable[0]["verdict"] == scan.VERDICT_UNSCOREABLE


def test_run_scan_peer_outlier_apy_flagged(fake_io):
    fb, fg = fake_io
    fg.stock_tokens = [nvda_token()]
    normal = pool_entry("inv_normal", "NVDAB-USDT", "PancakeSwap V3", "pancakeswap3", apy_bps=3000)
    outlier = pool_entry("inv_outlier", "NVDAB-USDT", "Uniswap V3", "uniswap3", apy_bps=100000)
    fb.investment_list_page1 = [normal, outlier]
    asset_list = [{"tokenAddress": nvda_token()["contractAddress"], "tokenSymbol": "NVDAB"},
                  {"tokenAddress": USDT_BSC, "tokenSymbol": "USDT"}]
    fb.investment_info["inv_normal"] = info_entry("NVDAB-USDT", "PancakeSwap V3", "pancakeswap3",
                                                   asset_list, apy_bps=3000)
    fb.investment_info["inv_outlier"] = info_entry("NVDAB-USDT", "Uniswap V3", "uniswap3",
                                                    asset_list, apy_bps=100000)
    fg.klines[("56", nvda_token()["contractAddress"].lower())] = make_price_klines(60, daily_return=0.01)

    results, flagged, unscoreable, coverage = scan.run_scan(max_pages=1)

    flagged_ids = {f["investmentId"] for f in flagged}
    result_ids = {r["investmentId"] for r in results}
    assert "inv_outlier" in flagged_ids
    assert "inv_normal" in result_ids


def test_run_scan_coverage_reports_truncation_when_max_pages_cuts_scan_short(fake_io):
    # Real gap this locks in: answering "did the scan cover every bStock pool?" used to require
    # a live manual investigation (see README) because nothing distinguished "max_pages cut this
    # off, more pools may exist" from "this really is the whole market." The API's own `total`
    # field (independent of how many pages were fetched) makes that distinction exact.
    fb, fg = fake_io
    fg.stock_tokens = [nvda_token()]
    fb.investment_list_page1 = [pool_entry("inv1", "NVDAB-USDT", "PancakeSwap V3", "pancakeswap3")]
    fb.investment_info["inv1"] = info_entry(
        "NVDAB-USDT", "PancakeSwap V3", "pancakeswap3",
        [{"tokenAddress": nvda_token()["contractAddress"], "tokenSymbol": "NVDAB"},
         {"tokenAddress": USDT_BSC, "tokenSymbol": "USDT"}])
    fg.klines[("56", nvda_token()["contractAddress"].lower())] = make_price_klines(60, daily_return=0.01)
    fb.investment_list_total = 250  # far more pools exist system-wide than this 1-pool page

    results, flagged, unscoreable, coverage = scan.run_scan(max_pages=1)

    assert coverage == {"pools_fetched": 1, "pools_total": 250, "truncated": True}


def test_run_scan_coverage_reports_complete_when_last_page_is_short(fake_io):
    fb, fg = fake_io
    fg.stock_tokens = [nvda_token()]
    fb.investment_list_page1 = [pool_entry("inv1", "NVDAB-USDT", "PancakeSwap V3", "pancakeswap3")]
    fb.investment_info["inv1"] = info_entry(
        "NVDAB-USDT", "PancakeSwap V3", "pancakeswap3",
        [{"tokenAddress": nvda_token()["contractAddress"], "tokenSymbol": "NVDAB"},
         {"tokenAddress": USDT_BSC, "tokenSymbol": "USDT"}])
    fg.klines[("56", nvda_token()["contractAddress"].lower())] = make_price_klines(60, daily_return=0.01)
    # investment_list_total defaults to len(page1) == 1 -- page came back short, so the scan
    # already saw the entire market even though max_pages=3 was never fully used.

    results, flagged, unscoreable, coverage = scan.run_scan(max_pages=3)

    assert coverage == {"pools_fetched": 1, "pools_total": 1, "truncated": False}


# ---- fetch_lp_investments: concurrent multi-page fetch ----

def test_fetch_lp_investments_fetches_additional_pages_concurrently_when_page_one_is_full(fake_io):
    # Real perf fix this locks in: page 2+ used to be fetched one at a time inside a `for page in
    # range(...)` loop -- each a separate `baw` subprocess spawn, pure sequential wait for calls
    # that don't depend on each other. Now page 1 alone determines pools_total, then only the
    # pages that could actually exist (capped by both max_pages and ceil(pools_total/100)) are
    # fetched concurrently.
    fb, fg = fake_io
    fb.investment_list_page1 = [pool_entry(f"inv{i}", f"P{i}-USDT", "PancakeSwap V3", "pancakeswap3")
                                 for i in range(100)]
    fb.investment_list_extra_pages = {
        2: [pool_entry(f"inv2_{i}", f"Q{i}-USDT", "PancakeSwap V3", "pancakeswap3") for i in range(50)],
        3: [pool_entry("inv3_should_not_be_fetched", "R-USDT", "PancakeSwap V3", "pancakeswap3")],
    }
    fb.investment_list_total = 150  # only 2 pages' worth actually exist -- page 3 must not be requested

    pools, total = market_data.fetch_lp_investments(max_pages=5)

    assert total == 150
    assert len(pools) == 150  # page 1 (100) + page 2 (50)
    page_requests = [c for c in fb.calls if c[0] == "defi" and c[1] == "investment-list"]
    pages_requested = {c[c.index("--page") + 1] for c in page_requests}
    assert pages_requested == {"1", "2"}  # capped at ceil(150/100)=2 -- page 3 never requested


def test_fetch_lp_investments_stops_at_max_pages_even_when_more_exist(fake_io):
    fb, fg = fake_io
    fb.investment_list_page1 = [pool_entry(f"inv{i}", f"P{i}-USDT", "PancakeSwap V3", "pancakeswap3")
                                 for i in range(100)]
    fb.investment_list_extra_pages = {
        2: [pool_entry(f"inv2_{i}", f"Q{i}-USDT", "PancakeSwap V3", "pancakeswap3") for i in range(100)],
        3: [pool_entry("inv3_should_not_be_fetched", "R-USDT", "PancakeSwap V3", "pancakeswap3")],
    }
    fb.investment_list_total = 300  # 3 pages exist, but max_pages=2 caps the fetch

    pools, total = market_data.fetch_lp_investments(max_pages=2)

    assert total == 300
    assert len(pools) == 200
    page_requests = [c for c in fb.calls if c[0] == "defi" and c[1] == "investment-list"]
    pages_requested = {c[c.index("--page") + 1] for c in page_requests}
    assert pages_requested == {"1", "2"}


# ---- malformed / missing / type-drifted API responses ----

def test_fetch_stock_tokens_raises_clear_error_on_missing_success_field(fake_io, monkeypatch):
    fb, fg = fake_io
    monkeypatch.setattr(api, "_get", lambda url, params: {"data": []})  # no "success" key
    with pytest.raises(RuntimeError, match="RWA list fetch failed"):
        market_data.fetch_stock_tokens()


def test_fetch_klines_raises_clear_error_on_failed_body(fake_io, monkeypatch):
    monkeypatch.setattr(api, "_get", lambda url, params: {"success": False, "error": "boom"})
    with pytest.raises(RuntimeError, match="kline fetch failed"):
        market_data.fetch_klines("56", "0xabc")


def test_fetch_investment_info_raises_on_baw_error_envelope(fake_io):
    fb, fg = fake_io
    # investmentId not registered -> FakeBaw's own {"success": False, ...} envelope
    with pytest.raises(RuntimeError):
        market_data.fetch_investment_info("does-not-exist")


# ---- baw() itself: subprocess-level failures, without a real subprocess ----

def test_baw_raises_clear_error_on_nonzero_exit_no_json(monkeypatch):
    class FakeCompletedProcess:
        returncode = 1
        stdout = ""
        stderr = "Error: not signed in"

    monkeypatch.setattr(api, "_resolve_baw_path", lambda: "C:\\fake\\baw.CMD")
    monkeypatch.setattr(api.subprocess, "run", lambda *a, **k: FakeCompletedProcess())

    with pytest.raises(RuntimeError, match="not signed in"):
        api.baw("defi", "position")


def test_baw_rejects_shell_metacharacters_before_touching_subprocess(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "_resolve_baw_path", lambda: "C:\\fake\\baw.CMD")
    monkeypatch.setattr(api.subprocess, "run", lambda *a, **k: calls.append(1))

    with pytest.raises(ValueError):
        api.baw("defi", "investment-info", "--investmentId", "123 & calc.exe")
    assert calls == []  # subprocess.run must never have been reached


# ---- JSON stdout contract: valid JSON, consistent envelope, nothing else on stdout ----

def test_scan_json_is_pure_json_with_envelope(fake_io, capsys):
    fb, fg = fake_io
    fg.stock_tokens = [nvda_token()]
    fb.investment_list_page1 = [pool_entry("inv1", "NVDAB-USDT", "PancakeSwap V3", "pancakeswap3")]
    fb.investment_info["inv1"] = info_entry(
        "NVDAB-USDT", "PancakeSwap V3", "pancakeswap3",
        [{"tokenAddress": nvda_token()["contractAddress"], "tokenSymbol": "NVDAB"},
         {"tokenAddress": USDT_BSC, "tokenSymbol": "USDT"}])
    fg.klines[("56", nvda_token()["contractAddress"].lower())] = make_price_klines(60, daily_return=0.01)

    args = cli.build_parser().parse_args(["scan", "--top", "5", "--json"])
    cli.cmd_scan(args)

    out = capsys.readouterr().out
    data = json.loads(out)  # raises if anything but pure JSON was on stdout
    assert data["schema_version"] == config.SCHEMA_VERSION
    assert data["status"] == "ok"
    assert "run_id" in data and "as_of" in data
    assert len(data["results"]) == 1
    assert data["coverage"] == {"pools_fetched": 1, "pools_total": 1, "truncated": False}


def test_scan_text_output_notes_truncated_coverage(fake_io, capsys):
    fb, fg = fake_io
    fg.stock_tokens = [nvda_token()]
    fb.investment_list_page1 = [pool_entry("inv1", "NVDAB-USDT", "PancakeSwap V3", "pancakeswap3")]
    fb.investment_info["inv1"] = info_entry(
        "NVDAB-USDT", "PancakeSwap V3", "pancakeswap3",
        [{"tokenAddress": nvda_token()["contractAddress"], "tokenSymbol": "NVDAB"},
         {"tokenAddress": USDT_BSC, "tokenSymbol": "USDT"}])
    fg.klines[("56", nvda_token()["contractAddress"].lower())] = make_price_klines(60, daily_return=0.01)
    fb.investment_list_total = 250  # 1-page scan is only a fraction of the full market

    args = cli.build_parser().parse_args(["scan", "--top", "5", "--max-pages", "1"])
    cli.cmd_scan(args)

    out = capsys.readouterr().out
    assert "scanned 1/250 LP pools" in out
    assert "249 more exist" in out


def test_positions_json_is_pure_json_with_envelope(fake_io, capsys):
    fb, fg = fake_io
    fg.stock_tokens = []
    fb.positions = {"deFiProtocolVOList": [], "deFiTotalValue": "0"}

    args = cli.build_parser().parse_args(["positions", "--json"])
    cli.cmd_positions(args)

    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "ok"
    assert data["positions"] == []


def test_rebalance_check_json_error_envelope_on_position_fetch_failure(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("session expired")
    monkeypatch.setattr(market_data, "fetch_positions", boom)

    args = cli.build_parser().parse_args(["rebalance-check", "--json"])
    with pytest.raises(SystemExit):
        cli.cmd_rebalance_check(args)

    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "error"
    assert data["schema_version"] == config.SCHEMA_VERSION
    assert "session expired" in data["error"]


# ---- range: explicit IL column + per-row capital simulation ----

def test_range_text_output_shows_il_column_and_capital_simulation(fake_io, capsys):
    fb, fg = fake_io
    fg.stock_tokens = [nvda_token()]
    fb.investment_info["inv1"] = info_entry(
        "NVDAB-USDT", "PancakeSwap V3", "pancakeswap3",
        [{"tokenAddress": nvda_token()["contractAddress"], "tokenSymbol": "NVDAB"},
         {"tokenAddress": USDT_BSC, "tokenSymbol": "USDT"}], apy_bps=3000)
    # alternating returns for a real, non-zero sigma (a constant daily_return gives sigma=0,
    # which would make every range width look equally "safe" and defeat the point of this test)
    returns = [0.02, -0.015] * 30
    rows = []
    price = 100.0
    t = 0
    for r in returns:
        o = price
        c = price * (1 + r)
        h = max(o, c) * 1.01
        lo = min(o, c) * 0.99
        rows.append([t, str(o), str(h), str(lo), str(c), "1000", t])
        price = c
        t += DAY_MS
    fg.klines[("56", nvda_token()["contractAddress"].lower())] = rows

    args = cli.build_parser().parse_args(
        ["range", "--investmentId", "inv1", "--capital", "10000"])
    cli.cmd_range(args)

    out = capsys.readouterr().out
    header_line = next(line for line in out.splitlines() if "concentration" in line and "il" in line)
    il_col = header_line.index("il")
    net_apy_col = header_line.index("net_apy")
    assert il_col < net_apy_col  # il column comes before net_apy, as documented
    assert "$/yr @10,000" in out
    # every data row (marked by a leading "+/-" or "full") should carry a dollar figure
    data_lines = [line for line in out.splitlines() if line.strip().startswith(("+/-", "full"))]
    assert len(data_lines) >= 5
    for line in data_lines:
        assert "%" in line  # il/net_apy percentages present
    assert "model estimate, not a promised return" in out


# ---- recommend: NO_TRADE, refuse-threshold, position-fetch-failure ----

FAKE_FULL_COVERAGE = {"pools_fetched": 1, "pools_total": 1, "truncated": False}


def test_recommend_no_trade_when_nothing_passes_gate(monkeypatch, capsys):
    negative_apy_result = {
        "pool": "NVDAB-USDT", "stock_ticker": "NVDA", "investmentId": "inv1",
        "grade": "Cheap", "model_net_apy": -0.02, "vol_ratio": 1.5, "tvl": 50000,
        "pair_mode": "stablecoin", "verdict": scan.VERDICT_WATCH,
    }
    monkeypatch.setattr(scan, "run_scan",
                         lambda **kw: ([negative_apy_result], [], [], FAKE_FULL_COVERAGE))

    args = cli.build_parser().parse_args(["recommend"])
    cli.cmd_recommend(args)

    out = capsys.readouterr().out
    assert "NO_TRADE" in out
    assert "Not a recommendation" in out


def test_recommend_refuses_when_unscoreable_ratio_too_high(monkeypatch, capsys):
    ok_result = {
        "pool": "NVDAB-USDT", "stock_ticker": "NVDA", "investmentId": "inv1",
        "grade": "Rich", "model_net_apy": 0.3, "vol_ratio": 0.2, "tvl": 50000,
        "pair_mode": "stablecoin", "verdict": scan.VERDICT_ENTER,
    }
    unscoreable = [{"pool": f"X{i}-USDT", "reason": "investment-info fetch failed (network error)"}
                   for i in range(5)]
    monkeypatch.setattr(scan, "run_scan",
                         lambda **kw: ([ok_result], [], unscoreable, FAKE_FULL_COVERAGE))

    args = cli.build_parser().parse_args(["recommend"])
    cli.cmd_recommend(args)

    out = capsys.readouterr().out
    assert "NO_TRADE" in out
    assert "5/6" in out


def test_recommend_does_not_mask_position_fetch_failure_as_no_positions(monkeypatch, capsys):
    top_result = {
        "pool": "NVDAB-USDT", "stock_ticker": "NVDA", "investmentId": "inv1",
        "grade": "Rich", "model_net_apy": 0.3, "vol_ratio": 0.2, "tvl": 50000,
        "pair_mode": "stablecoin", "best_range": {"pb": None, "confidence": "High", "model_net_apy": 0.3},
        "verdict": scan.VERDICT_ENTER,
    }
    monkeypatch.setattr(scan, "run_scan", lambda **kw: ([top_result], [], [], FAKE_FULL_COVERAGE))

    def boom():
        raise RuntimeError("session expired")
    monkeypatch.setattr(market_data, "fetch_positions", boom)

    args = cli.build_parser().parse_args(["recommend"])
    cli.cmd_recommend(args)

    out = capsys.readouterr().out
    assert "Could not check your current bStock LP positions" in out
    assert "session expired" in out
    assert "No current bStock LP positions" not in out


# ---- rebalance-check: multi-investmentId worst-case, best_alternative + switching ----

def test_rebalance_check_evaluates_every_investment_id_worst_case(monkeypatch, capsys):
    def fake_fetch_positions():
        return {"deFiProtocolVOList": [{
            "protocolName": "PancakeSwap V3", "binanceChainId": "56",
            "poolList": [{"poolType": "Liquidity Pool", "positionCollectionList": [{"positionList": [{
                "investmentIds": ["good", "bad"],
                "positionDetail": {"nftId": "n1"},
                "tokenList": {"supply": [{"tokenAddress": nvda_token()["contractAddress"],
                                           "tokenAmount": "1", "tokenValue": "100"}]},
            }]}]}],
        }]}
    monkeypatch.setattr(market_data, "fetch_positions", fake_fetch_positions)
    monkeypatch.setattr(market_data, "fetch_stock_tokens", lambda type_filter=None: [nvda_token()])

    def fake_run_scan(**kwargs):
        market_results = [{"investmentId": "good", "stock_ticker": "NVDA", "vol_ratio": 0.3,
                            "apy": 0.4, "model_net_apy": 0.35}]
        return market_results, [], [], FAKE_FULL_COVERAGE
    monkeypatch.setattr(scan, "run_scan", fake_run_scan)

    def fake_fetch_investment_info(inv_id):
        raise RuntimeError("pool not found")
    monkeypatch.setattr(market_data, "fetch_investment_info", fake_fetch_investment_info)

    args = cli.build_parser().parse_args(["rebalance-check", "--json"])
    cli.cmd_rebalance_check(args)

    data = json.loads(capsys.readouterr().out)
    pos = data["positions"][0]
    ids_by_id = {r["investmentId"]: r for r in pos["investment_ids"]}
    assert ids_by_id["good"]["vol_ratio"] == 0.3
    assert ids_by_id["bad"]["evaluated"] is False
    assert pos["unevaluated_count"] == 1
    assert pos["needs_attention"] is True


def test_rebalance_check_names_best_alternative_and_switch_verdict(monkeypatch, capsys):
    def fake_fetch_positions():
        return {"deFiProtocolVOList": [{
            "protocolName": "PancakeSwap V3", "binanceChainId": "56",
            "poolList": [{"poolType": "Liquidity Pool", "positionCollectionList": [{"positionList": [{
                "investmentIds": ["held1"],
                "positionDetail": {"nftId": "n1"},
                "tokenList": {"supply": [{"tokenAddress": nvda_token()["contractAddress"],
                                           "tokenAmount": "100", "tokenValue": "20000"}]},
            }]}]}],
        }]}
    monkeypatch.setattr(market_data, "fetch_positions", fake_fetch_positions)
    monkeypatch.setattr(market_data, "fetch_stock_tokens", lambda type_filter=None: [nvda_token()])

    def fake_run_scan(**kwargs):
        market_results = [
            {"investmentId": "held1", "stock_ticker": "NVDA", "vol_ratio": 0.9, "apy": 0.10,
             "model_net_apy": 0.03, "pool": "NVDAB-USDT (held)", "protocol": "PancakeSwap V3",
             "tvl": 20000, "pair_mode": "stablecoin", "grade": "Fair"},
            {"investmentId": "alt1", "stock_ticker": "NVDA", "vol_ratio": 0.2, "apy": 0.40,
             "model_net_apy": 0.35, "pool": "NVDAB-USDT (better)", "protocol": "Uniswap V3",
             "tvl": 80000, "pair_mode": "stablecoin", "grade": "Rich"},
        ]
        return market_results, [], [], FAKE_FULL_COVERAGE
    monkeypatch.setattr(scan, "run_scan", fake_run_scan)

    args = cli.build_parser().parse_args(["rebalance-check", "--json"])
    cli.cmd_rebalance_check(args)

    data = json.loads(capsys.readouterr().out)
    pos = data["positions"][0]
    assert pos["best_alternative"]["investmentId"] == "alt1"
    assert pos["switching"]["verdict"] == "switch"
    assert pos["needs_attention"] is True


# ---- property-style checks (randomized, no hypothesis dependency) ----

def test_no_exit_probability_stays_in_unit_interval_property():
    import random
    rng = random.Random(42)
    for _ in range(200):
        pa = rng.uniform(0.5, 0.99)
        pb = rng.uniform(1.01, 2.0)
        sigma = rng.uniform(0.05, 3.0)
        years = rng.uniform(0.1, 3.0)
        result = range_model.no_exit_probability(pa, pb, sigma, years)
        assert 0.0 <= result <= 1.0, f"out of range for pa={pa} pb={pb} sigma={sigma} years={years}"


def test_switching_recommendation_verdict_matches_gap_sign_property():
    import random
    rng = random.Random(7)
    for _ in range(200):
        position_usd = rng.uniform(1, 200_000)
        held = rng.uniform(-0.5, 1.0)
        alt = rng.uniform(-0.5, 1.0)
        result = cli._switching_recommendation(position_usd, held, alt)
        gap = position_usd * (alt - held)
        if gap <= 0:
            assert result["verdict"] == "stay"
        assert result["annual_gap_usd"] == pytest.approx(gap)
