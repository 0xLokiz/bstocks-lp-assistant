---
name: lp-risk-screener
description: Rank Binance Web3 liquidity pools on tokenized-stock pairs by risk-adjusted APY, recommend a concentrated-liquidity price range that trades off safety vs yield, and check held LP positions against the current market. Use when the user asks to compare LP/yield-farming pools, find the "best" LP for a stock token, pick a price range for a concentrated LP position, screen for risk-adjusted yield, review their current LP positions, or asks whether to rebalance. Does not execute deposits/withdrawals itself — see "Executing a recommendation" below.
---

# LP Risk Screener

## Why this exists

Headline LP APY is compensation for taking on impermanent loss (IL), and IL
scales with the volatility of the pooled asset. Ranking pools by raw APY
alone hides how much of that yield is actually paying for risk. This skill
computes volatility from each stock token's on-chain kline history and nets
it out of the pool's APY before ranking, using
`riskscreen.py` in this directory.

## When to use

Trigger this skill when the user's request is about:
- comparing LP pool APYs on tokenized stocks (`…on` Ondo, `…x` xStocks, `…B` bStock)
- "which LP is actually worth it", "risk-adjusted yield", "波动率调整后的APY"
- estimating impermanent loss for a specific stock-token pool
- screening/ranking pools instead of looking at one at a time

Not for: crypto/crypto LP pairs (the no-correlation-term simplification below
does not hold there), or any request to actually execute a deposit — that
still goes through the `binance-agentic-wallet` skill's `defi deposit` /
`defi lp-add` flow with its own confirmation step.

## Model

For a constant-product AMM, full-range (V2-style) liquidity, the diffusion
approximation for expected annual IL is:

```
E[IL] ≈ σ² / 8
```

where `σ` is the stock token's annualized volatility (stdev of daily
log-returns × √365, from on-chain kline). This works cleanly for stock-token
pools because they're paired against a stablecoin — no cross-asset
correlation term is needed. Concentrated (V3) positions amplify realized IL
above this floor roughly in proportion to how narrow the price range is —
flag this to the user rather than silently correcting for it (the exact
amplification factor depends on the chosen range, which is a position-level,
not pool-level, choice).

Ranking metric:

```
net_apy = pool_apy - E[IL]
score   = net_apy / σ        # return per unit of volatility risk taken
```

### Range model (concentrated / V3 positions)

A concentrated position on `[1-lower%, 1+upper%]` around the current price
behaves like a leveraged full-range position: both fee income and IL scale
up by the same Uniswap V3 concentration multiplier `M = 1 / (1 - √(Pa/Pb))`,
while the pool's reported APY is treated as the full-range-equivalent
baseline (a stated simplification — the platform doesn't expose per-tick fee
data, so a pool whose existing LPs are already concentrated will make this
baseline run a little hot or cold; say so if asked).

```
p_stay        = P(price stays in range over 1yr | lognormal, σ, zero drift)
effective_apy = pool_apy × M × p_stay
expected_IL   = min(M × σ²/8, IL at the range boundary)   # capped, avoids blow-up on narrow ranges
net_apy       = effective_apy − expected_IL
```

`range` sweeps `±5/10/20/30/50/90%` plus full-range and recommends the
highest `net_apy` among ranges with **≥60% chance of staying in range over a
year** — that 60% floor is the "safety" side of "safe and high APY"; a
narrower range can show a higher `net_apy` number but at a p_stay so low
it's misleading to call it "safe". Always show `p_stay` next to any
recommended range, not just the net_apy figure.

## Commands

```bash
# tokenized-stock token list (public API, no auth)
python riskscreen.py stocks --limit 20 [--type 1|2|3]

# annualized volatility + est. IL for one ticker (public API, no auth)
python riskscreen.py vol --ticker <TICKER> [--days 30]

# rank stock-token LP pools by risk-adjusted APY (needs signed-in baw session)
python riskscreen.py scan --top 15 [--json] [--with-range]

# range-by-range IL/APY breakdown + recommended range for one pool
python riskscreen.py range --investmentId <id>
python riskscreen.py range --ticker TSLA --apy 0.30   # without a live pool

# current LP positions on tokenized-stock pairs (needs signed-in baw session)
python riskscreen.py positions [--refresh] [--json]

# compare held positions' risk-adjusted score against the current market
# (report only — see "Executing a recommendation")
python riskscreen.py rebalance-check
```

All commands that touch `baw` (`scan`, `range --investmentId`, `positions`,
`rebalance-check`) rely on an active Agentic Wallet session. If one returns
`NOT_LOGGED_IN` / `SESSION_EXPIRED`, tell the user to run the
`binance-agentic-wallet` skill's sign-in flow (`auth signin` → `auth verify`)
first, then retry — do not attempt to sign in without the user's explicit
go-ahead, per that skill's rules.

## Executing a recommendation

This skill never calls `defi deposit` / `defi lp-add` / `defi redeem` /
`defi lp-remove` itself, and `rebalance-check` never auto-executes — by
design, confirmed with the user, not an oversight. Once the user picks a
pool and range from this skill's output:

1. Use `binance-agentic-wallet`'s `defi preview --action LP-ADD ...` (or
   `DEPOSIT`) with the `investmentId` and token address this skill surfaced.
2. Show the preview's estimated fee, balance change, and warnings.
3. Only call `defi lp-add` / `defi deposit` after the user explicitly
   confirms — same rule as every other state-changing `baw` command.
4. For `rebalance-check` suggestions specifically: moving a position means
   `defi redeem` / `defi lp-remove` on the old one, then `defi deposit` /
   `defi lp-add` on the new one — two separate confirmed actions, not one.

## Before presenting results to the user

1. **Check for upcoming corporate actions** on any pool you're about to
   highlight — use `binance-tokenized-securities-info`'s asset-market-status
   API (earnings/dividend/split can invalidate the historical-vol estimate
   right when it matters most). Mention if one is pending.
2. **State the approximation**: full-range results use the full-range IL
   estimate; range results use the concentration-scaled model above. Say
   which one you're showing.
3. **Never recommend a deposit outright** — present the ranking/range and
   reasoning, let the user decide, and route any actual deposit through the
   confirmed flow in "Executing a recommendation" above.

## Data sources

| Data              | Source                                                              | Auth |
|-------------------|-----------------------------------------------------------------------|------|
| Stock token list   | `bapi/defi/.../rwa/stock/detail/list/ai`                             | none |
| Token kline (vol)  | `bapi/defi/.../dex/market/token/kline/ai`                            | none |
| LP pool APY/TVL    | `baw defi investment-list --investType LiquidityPool`                | `baw` session |
| Pool composition   | `baw defi investment-info --investmentId <id>`                       | `baw` session |
| Held positions      | `baw defi position`                                                   | `baw` session |

> **`apy` is null for most V3 pools in `investment-list`.** Concentrated-liquidity pools usually report `apy: null/0` there (it's promotional/reward APY only). The real fee-based rate is `investment-info`'s `apyBps` (basis points) / `apyDisplay`. `riskscreen.py` already falls back to `apyBps` — if you're calling these APIs directly instead, do the same, or every V3 stock-token pool will look like a guaranteed loser once IL is netted out.
