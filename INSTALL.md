# Installing & Using This Skill

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

> "帮我装一下 https://github.com/0xLokiz/lp-risk-screener 这个 skill"
> ("help me install this skill: https://github.com/0xLokiz/lp-risk-screener")

Claude will confirm before doing anything (same pattern as installing any
other Binance skill, e.g. `query-token-info`), then clone this repo's
`SKILL.md` + `riskscreen.py` into `~/.claude/skills/lp-risk-screener/`.

Manual install works too, if you'd rather do it yourself:

```bash
git clone https://github.com/0xLokiz/lp-risk-screener ~/.claude/skills/lp-risk-screener
```

No restart needed beyond starting your next Claude session — skills are
scanned at session start.

## Step 2 — Just talk to Claude

Once installed, you don't need to remember any command syntax. Ask things
like:

- "哪个股票代币 LP 池风险调整后最划算？" — *which stock-token LP pool is
  actually worth it once you account for risk?*
- "TSLA 这个池子选多宽的区间比较好？" — *what price range should I pick for
  a TSLA LP position?*
- "我现在的 LP 仓位要不要调整？" — *should I rebalance my current LP
  positions?*
- "这个池子最近有没有财报之类的事件要注意？" — *any upcoming corporate
  action I should know about before entering this pool?*

Claude reads `SKILL.md`, decides which of the six `riskscreen.py` commands
to run (`stocks` / `vol` / `scan` / `range` / `positions` /
`rebalance-check`), and explains the result — including the caveats (full-
range vs. concentrated approximation, probability of staying in range,
pending corporate actions).

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
git clone https://github.com/0xLokiz/lp-risk-screener
cd lp-risk-screener
python riskscreen.py scan --top 15 --with-range   # needs a signed-in baw session
python riskscreen.py range --investmentId <id>
python riskscreen.py positions
python riskscreen.py rebalance-check
python riskscreen.py stocks --limit 20            # no auth needed
python riskscreen.py vol --ticker TSLA            # no auth needed
```

See [README.md](README.md) for the model behind the numbers and
[SKILL.md](SKILL.md) for the exact rules an agent follows when using this.
