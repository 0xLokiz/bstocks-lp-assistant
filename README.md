# LP Risk Screener

A volatility-aware LP advisor for Binance Web3 tokenized-stock pools. Four
parts:

1. **Market data** — pulls stock-token prices/kline and LP pool APY/TVL/composition
   straight from Binance's Web3 APIs and the Agentic Wallet (`baw`) CLI.
2. **Risk-adjusted recommendation** — computes impermanent-loss cost from
   volatility, at both full-range and a swept set of concentrated-liquidity
   ranges, and recommends the range that's actually "safe and high APY"
   rather than just high APY.
3. **Execution** — deliberately *not* reimplemented here. Deposits/withdrawals
   go through `binance-agentic-wallet`'s already-reviewed, confirmation-gated
   `defi deposit` / `defi lp-add` / `defi redeem` / `defi lp-remove` flow,
   using the `investmentId` and token addresses this tool surfaces.
4. **Portfolio + rebalance checks** — reads current LP positions via `baw defi
   position` and compares their risk-adjusted score against the live market,
   producing a report (never an auto-executed trade — see below).

**New here?** See [INSTALL.md](INSTALL.md) for how to add this to your own
Claude + Agent OS setup and use it conversationally, with no command-line
syntax to remember.

## The idea

LP fee/incentive APY is compensation for impermanent loss (IL), and IL scales
with the volatility of the pooled asset. Stock-token pools make this easy to
model cleanly: they're paired against a stablecoin, so IL is driven almost
entirely by the stock token's own volatility — no cross-asset correlation term
needed, unlike a crypto/crypto pair.

For a constant-product AMM, the standard diffusion approximation gives:

```
E[IL] ≈ σ² / 8      (annualized, full-range / V2-style liquidity)
```

where `σ` is the token's annualized volatility, estimated from its on-chain
kline history (log-return stdev, annualized by `sqrt(365)`).

The screener then ranks pools by:

```
net_apy = pool_apy - E[IL]
score   = net_apy / σ          (Sharpe-like, return per unit of risk taken)
```

A pool with a flashy headline APY on a highly volatile stock token can rank
*below* a lower-APY pool on a stable stock token once IL is priced in.

### Concentrated (V3) ranges: the "safe and high APY" question

A concentrated position on `[1-a%, 1+b%]` behaves like a leveraged full-range
position — fee income *and* IL both scale by the same Uniswap V3
concentration multiplier `M = 1 / (1 - √(Pa/Pb))`. Narrower range → higher M
→ higher APY *and* higher realized IL *and* a higher chance the price exits
the range entirely (at which point the position earns zero fees until
rebalanced). The tool sweeps `±5/10/20/30/50/90%` plus full-range and picks
the highest `net_apy` among ranges with **≥60% probability of staying in
range over a year** — that's the "safe" floor; "high APY" is the search
inside it. Both numbers are always shown together, never the APY alone.

## Caveats (read before trusting the ranking)

- **Historical vol is backward-looking.** Stock tokens can gap hard around
  earnings, dividends, splits, and trading halts — check
  `binance-tokenized-securities-info`'s asset-market-status API for upcoming
  corporate actions on any pool you're about to enter.
- **Full-range approximation.** Concentrated (V3-style) LP positions
  amplify realized IL versus this full-range estimate, roughly in proportion
  to how narrow the price range is. Treat `E[IL]` here as a floor, not a
  prediction, for concentrated positions.
- **No drift term.** The formula assumes zero expected price drift. It's a
  volatility-risk estimate, not a directional forecast.

## Usage

```bash
# --- market data (public API, no auth) ---
python riskscreen.py stocks --limit 20 --type 1
python riskscreen.py vol --ticker TSLA --days 30

# --- recommendation (needs a signed-in `baw` session) ---
python riskscreen.py scan --top 15 [--with-range] [--json]
python riskscreen.py range --investmentId <id>          # full range sweep, one pool
python riskscreen.py range --ticker TSLA --apy 0.30      # or without a live pool

# --- portfolio + rebalance (needs a signed-in `baw` session) ---
python riskscreen.py positions [--refresh] [--json]
python riskscreen.py rebalance-check
```

`scan`, `range --investmentId`, `positions`, and `rebalance-check` shell out
to `baw` (`defi investment-list` / `investment-info` / `defi position`), so
they require an active Agentic Wallet session (`baw auth signin` / `baw auth
verify`). `stocks`, `vol`, and `range --ticker/--apy` hit public Binance Web3
endpoints directly and need no auth.

**Execution is intentionally out of scope for this script.** `rebalance-check`
prints a report and moves nothing. To act on any recommendation, run
`binance-agentic-wallet`'s `defi preview` → confirm with the user → `defi
deposit` / `defi lp-add` / `defi redeem` / `defi lp-remove`, using the
`investmentId` / token addresses this tool printed. See `SKILL.md` →
"Executing a recommendation" for the exact flow an agent should follow.

## Status

Validated end-to-end against a live `baw` session. Live output
(`python riskscreen.py scan --top 10`, BSC, 2026-09-02):

```
pool                ticker        apy      vol   est.IL   net_apy   score           tvl
GMEB-USDT           GME       452.84%   23.72%    0.70%   452.14%   19.06        18,448
AAPLB-USDT          AAPL      382.75%   32.62%    1.33%   381.42%   11.69        52,527
GMEB-USDT           GME       296.20%   23.72%    0.70%   295.50%   12.46       137,303
AAPLB-USDT          AAPL      230.28%   32.62%    1.33%   228.95%    7.02       431,446
NVDAB-BNB           NVDA      123.94%   39.37%    1.94%   122.00%    3.10        42,681
BNB-SPCXB           SPCX      125.01%   82.80%    8.57%   116.44%    1.41     1,339,931
HOODB-BNB           HOOD      112.87%   71.89%    6.46%   106.41%    1.48        17,274
NVDAB-USDT          NVDA      101.83%   39.37%    1.94%    99.89%    2.54        67,277
USDT-TSLAB          TSLA       97.97%   51.04%    3.26%    94.71%    1.86       319,066
USDT-HOODB          HOOD       95.10%   71.89%    6.46%    88.64%    1.23       161,799
```

**Finding along the way**: `defi investment-list`'s `apy` field is `null`/`0`
for essentially all concentrated-liquidity (V3) pools — it only reflects
promotional/reward APY, which most of these pools don't currently have.
The real fee-based rate lives in `investment-info`'s `apyBps` /
`apyDisplay`, which the screener now falls back to per pool. Ranking on the
list-level `apy` alone (our first pass) would have shown every stock-token
V3 pool as a straight loser once IL is priced in — which happened to look
plausible, but was actually a data-plumbing bug, not a real result. Worth
remembering: don't trust a "the risk-adjusted number is always negative"
result without checking whether the raw input was actually populated.

**Reading the live numbers**: pools also matched by exact on-chain
`assetTokenList` address (not just name-string matching), so `stock_ticker`
here is confirmed, not guessed. The very high scores at the top (GMEB-USDT,
AAPLB-USDT) sit on pools with comparatively small TVL — consistent with
tight concentrated-liquidity ranges, which earn outsized fee APY *and* carry
IL well above this screener's full-range floor. That's the SKILL.md caveat
showing up in real data, not a free-lunch signal.

Range sweep for one of those pools (`python riskscreen.py range
--investmentId 9c97...c907c22d405de7ec7f79d` — NVDAB-USDT on PancakeSwap V3):

```
NVDAB-USDT (NVDA) -- annualized vol 39.50%

     range concentration  p_stay   eff.apy   est.IL   net_apy   score
     +/-5%         20.49     10%   116.67%    0.03%   116.63%    0.65
    +/-10%         10.47     20%   118.63%    0.14%   118.49%    0.93
    +/-20%          5.45     39%   120.58%    0.62%   119.97%    1.30
    +/-30%          3.76     56%   119.56%    1.57%   117.99%    1.54
    +/-50%          2.37     81%   107.98%    4.61%   103.37%    1.70  <- recommended
    +/-90%          1.30     95%    69.48%    2.53%    66.95%    1.49
      full          1.00    100%    56.48%    1.95%    54.53%    1.38
```

The full-range headline APY on this pool is 56.48%. The tool's recommended
range (±50%, 81% chance of staying in range over a year) nearly doubles that
to 103.37% net — without dropping into the sub-20%-p_stay territory where the
±5–10% rows post even bigger numbers that are really lottery tickets, not
yield. This is the concrete answer to "how do I get a safe *and* high APY":
not the highest number on the sheet, the highest number above the safety
floor.

`positions` / `rebalance-check` ran clean against the live (currently empty)
wallet — both report "no LP positions on tokenized-stock pairs found" rather
than erroring, and will start producing real comparisons the moment a
position exists.

## Binance Agent OS Mini Hackathon — Track A submission

Built as a [Skill](SKILL.md) — an AI agent (this repo was built and driven
end-to-end by Claude) loads it on demand, matching the "Skill Hub" piece of
Agent OS. Maps directly onto Agent OS's own pillars
([binance.com/en/agent-os](https://www.binance.com/en/agent-os)):

| Agent OS pillar        | This project                                                          |
|-------------------------|------------------------------------------------------------------------|
| **Read the market**     | Web3 APIs — public RWA stock-token list + kline, for the vol/IL model  |
| **Track your portfolio**| `baw defi position` — held LP positions, filtered to stock-token pairs |
| **Operate on-chain**    | Recommendations hand off to Agentic Wallet's `defi deposit`/`lp-add`/`redeem`/`lp-remove`, confirmed each time — not reimplemented here |
| **Skill Hub**            | Packaged as `SKILL.md` + script, same shape as Binance's own published skills (`binance-agentic-wallet`, `binance-tokenized-securities-info`) |

The pitch: turn "what's the LP APY" into "what's the LP APY *worth taking
the risk for, and at what range*" — a volatility-aware advisor that treats
stock-token LPing as the market-making activity it actually is, recommends a
concrete range instead of a vague "APY looks good", and stays in an advisory
role — every fund movement is still a human-confirmed `baw` call.
