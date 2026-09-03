# Installing & Using bStocks LP Assistant

This guide is for someone who already has **Claude connected to Binance
Agent OS** — i.e. the `baw` (Binance Agentic Wallet) CLI is installed and
signed in, the way the `binance-agentic-wallet` skill sets it up. That's the
only hard dependency; this skill adds a recommendation layer on top of it.

## Prerequisites

| Requirement                          | Why                                                                |
|---------------------------------------|---------------------------------------------------------------------|
| Claude Code / Claude Desktop           | Runs the skill and talks to you in natural language                |
| `baw` CLI installed and signed in     | Source of live LP pool data and (separately) wallet execution      |
| Python 3                              | Runs `riskscreen.py`, which the skill shells out to                |

If you can already ask Claude things like "check my wallet balance" or "swap
BNB to USDT" and it works, you have everything above except this skill
itself.

## Step 1 — Install the skill

Skills live in `~/.claude/skills/<skill-name>/` and Claude discovers them
automatically. The simplest way to install this one is to just ask Claude:

> "帮我装一下 https://github.com/0xLokiz/bstocks-lp-assistant 这个 skill"
> ("help me install this skill: https://github.com/0xLokiz/bstocks-lp-assistant")

Claude will confirm before doing anything (same pattern as installing any
other Binance skill, e.g. `query-token-info`), then clone this repo's
files -- `SKILL.md`, `riskscreen.py`, the `bstocks_lp/` package it imports,
and `stablecoins.json` -- into `~/.claude/skills/bstocks-lp-assistant/`.

Manual install works too, if you'd rather do it yourself:

```bash
git clone https://github.com/0xLokiz/bstocks-lp-assistant ~/.claude/skills/bstocks-lp-assistant
```

No restart needed beyond starting your next Claude session — skills are
scanned at session start.

## Step 2 — Just talk to Claude

Once installed, you don't need to remember any command syntax. If you don't
know where to start, just ask:

- "我该怎么配置我的 bStocks LP？" — *what should I do with my bStocks LPs?*
  (this is the one to lead with — Claude runs `recommend` and gives you one
  verdict instead of making you pick a more specific question first)

Or ask something more specific:

- "哪个股票代币 LP 池风险调整后最划算？我打算存 1 万刀" — *which stock-token
  LP pool is actually worth it, accounting for risk? I'm putting in $10k*
  (the dollar amount gets you a concrete $/yr estimate and a warning if
  you'd end up owning too much of the pool)
- "TSLA 这个池子选多宽的区间比较好？" — *what price range should I pick for
  a TSLA LP position?*
- "我想在 NVDA 涨到 $310 的时候卖出，能不能用 LP 顺便赚点手续费？" —
  *I want to sell NVDA once it hits $310 — can an LP range do that while
  earning fees along the way?* (an exact target price, not just presets)
- "我现在的 LP 仓位要不要调整？" — *should I rebalance my current LP
  positions?*
- "这个池子最近有没有财报之类的事件要注意？" — *any upcoming corporate
  action I should know about before entering this pool?*

Claude reads `SKILL.md`, decides which of the seven `riskscreen.py` commands
to run (`recommend` / `stocks` / `vol` / `scan` / `range` / `positions` /
`rebalance-check`), and shows the result as a chart plus a short
explanation — including the caveats (which range model produced the number,
a breakeven-volatility read on whether the pool is a good deal, pending
corporate actions).

## Step 3 — Acting on a recommendation

This skill never moves funds itself. When you decide to act on a
recommendation, Claude switches to the `binance-agentic-wallet` skill's
normal flow:

1. `defi preview` — shows the estimated fee and balance change for the
   deposit/LP-add you're about to make.
2. You explicitly confirm ("yes", "confirm", "go ahead" — anything less
   than a clear yes gets re-prompted, never assumed).
3. `defi deposit` / `defi lp-add` actually executes.

Moving an existing position (a `rebalance-check` suggestion) is two separate
confirmed steps — redeem/remove the old position, then deposit/add the new
one — never a single silent swap.

## Using it without Claude (direct CLI)

If you'd rather run it yourself:

```bash
git clone https://github.com/0xLokiz/bstocks-lp-assistant
cd bstocks-lp-assistant
python riskscreen.py recommend --capital 10000          # one verdict, needs a signed-in baw session
python riskscreen.py scan --top 15 --with-range          # the thorough version
python riskscreen.py range --investmentId <id>            # market-making ranges
python riskscreen.py range --investmentId <id> --side sell --target-offset 0.15   # sell at +15%
python riskscreen.py positions
python riskscreen.py rebalance-check --json               # --json for scheduled monitoring
python riskscreen.py stocks --limit 20                    # no auth needed
python riskscreen.py vol --ticker TSLA --apy 0.30          # no auth needed
```

See [README.md](README.md) for the model behind the numbers and
[SKILL.md](SKILL.md) for the exact rules an agent follows when using this.
