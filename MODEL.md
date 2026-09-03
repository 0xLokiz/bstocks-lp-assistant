# A Volatility-Implied Model for Screening Tokenized-Stock Liquidity Pools

*bStocks LP Assistant — model documentation. Companion to [README.md](README.md) and [SKILL.md](SKILL.md), which describe the product; this document derives and justifies the mathematics behind it.*

## Abstract

Automated market maker (AMM) liquidity provision pays a fee yield in exchange for bearing impermanent loss (IL), a cost driven by the realized volatility of the pooled assets. A quoted APY alone is not comparable across pools, because it says nothing about the risk taken to earn it. This document derives the model `riskscreen.py` uses to convert a pool's quoted APY and an estimate of the underlying asset's realized volatility into a single, risk-adjusted judgment of whether a pool is a "good LP": the **breakeven (implied) volatility** an APY is pricing in, the **Richness Score** comparing that to realized volatility, an **expected-impermanent-loss** model for both full-range and concentrated positions, a closed-form **stay-in-range probability** for choosing a concentrated range, and the resulting **ENTER / WATCH / NO_TRADE / UNSCOREABLE** decision rule. Every formula below is implemented in `riskscreen.py`; section headers cite the corresponding function.

## 1. Introduction

Constant-product AMM liquidity provision is, in expectation, equivalent to continuously delta-hedging a short at-the-money straddle on the pooled assets: the LP collects a fee stream (the option premium) in exchange for absorbing the cost of the underlying moving away from the price at which liquidity was deposited (the option's loss on exercise) — impermanent loss. This is a known result in the AMM-as-options-strategy literature; it is the load-bearing idea behind everything that follows.

Two consequences follow directly:

1. **A pool's quoted APY is, by itself, uninformative about whether it is a good deal.** A 40% APY pool on a low-volatility stock can be a much better trade than a 200% APY pool on a high-volatility one, because the second pool's underlying moved far enough to erase the fee income and then some. Ranking pools by raw APY, the product's original naive approach, actively selects for the *worst* risk-adjusted trades whenever APY and volatility are correlated (which they usually are — the market prices risk into fees, imperfectly).
2. **A yield can be reframed as an implied volatility.** Just as an option's premium implies a volatility the market is pricing, a pool's APY implies a volatility the pool's fee income is compensating for. Comparing that *implied* volatility to the asset's *realized* volatility is the same diagnostic options traders use to judge whether an option is cheap or rich — and it is dimensionless and range-independent, so it is comparable across every pool regardless of size or concentration.

The rest of this document builds that comparison, and the machinery layered on top of it: a proper (not diffusion-only) IL model for concentrated ranges, a closed-form probability that a chosen range survives a year without the price exiting it, and the safety/plausibility screen that runs independently of all of it.

## 2. Notation and data

| Symbol | Meaning | Source |
|---|---|---|
| `apy` | Pool's quoted (blended fee + incentive) annual yield, as a decimal | `defi investment-list` / `investment-info` (`apy` or `apyBps`) |
| $\sigma$ | Annualized realized volatility of the relevant price series | Computed from Binance Web3 kline data — §4 |
| $\sigma^*$ | Breakeven (implied) volatility: the $\sigma$ at which `apy` exactly offsets expected IL | Derived, §3.3 |
| `vol_ratio` | $\sigma / \sigma^*$ — the Richness Score | §5 |
| $[p_a, p_b]$ | A concentrated range's bounds, as price ratios to the current price ($p_a<1<p_b$ for a straddling range) | User/sweep input |
| $M$ | Capital-efficiency multiplier of range $[p_a,p_b]$ vs. full range | §6.1 |
| `p_active` | Probability the price stays inside (or, for a single-sided range, ever reaches) the range within the horizon | §6.2–6.3 |
| `model_net_apy` | `apy` (or range-adjusted `effective_apy`) minus modeled expected IL | §3.2, §6.4 |

All probability/volatility results use a one-year horizon ($T=1$) unless stated otherwise, and treat the underlying as a driftless lognormal diffusion — a standard, deliberate simplification (§9).

## 3. The core model: LP as an implied-volatility trade

### 3.1 Exact impermanent loss at a given price move

For a constant-product pool, if the pooled asset's price moves by ratio $k = P_1/P_0$ relative to the price at deposit, the value of the LP position relative to simply holding the two assets is the standard closed form:

$$\mathrm{IL}(k) = 1 - \frac{2\sqrt{k}}{1+k}$$

implemented directly as `_il_at_price_ratio(k)`. This is exact for a full-range position; it is also used, at the range boundary, as an upper bound on IL for a concentrated position (§6.4).

### 3.2 Expected IL under a diffusion approximation

For a driftless lognormal price with annualized volatility $\sigma$ over horizon $T$ (in years), the expected impermanent loss for a **full-range** position is well-approximated by

$$\mathbb{E}[\mathrm{IL}] \approx \frac{\sigma^2 T}{8} \qquad \text{valid only while this is} < 1$$

implemented as `expected_il_fraction(sigma_annual)` (with $T=1$). This is the standard diffusion approximation of constant-product IL — a small-move Taylor expansion of $\mathrm{IL}(k)$ around $k=1$, integrated over a lognormal price path — and it is *only* that: a small-$\sigma$ approximation. It diverges past $\sigma=\sqrt{8}\approx283\%$ annualized, at which point the true IL (§3.1, which asymptotically approaches but never reaches $100\%$ even at $p_a\to0,\ p_b\to\infty$) is no longer something this formula can honestly estimate. An earlier version of this function clamped the result to $1$ once it crossed that threshold; that was rejected on review — a value beyond $\sigma=\sqrt{8}$ hasn't just hit a ceiling, the approximation has left the regime it was ever derived for, so presenting *any* single number (a "safe-looking" $1.0$ included) is false precision. `expected_il_fraction` instead returns `None` past this threshold, and every caller that reaches a real user (`model_net_apy`, `--json` output, table rows) reports it as `N/A` rather than inventing a figure — confirmed on a real pool (a Trump Media stock-token pool, $\sigma\approx310\%$ annualized) whose diffusion estimate would have been $\approx120\%$, a value the model's own IL definition says is impossible. It is a floor/approximation, not the exact realized IL for a narrow (concentrated) range; §6.4 gives the range-adjusted version actually used for concentrated positions, which stays valid at any volatility because it's anchored to the exact boundary IL, not this approximation.

**Model net APY** for a full-range position is then

$$\texttt{model\_net\_apy} = \texttt{apy} - \mathbb{E}[\mathrm{IL}] = \texttt{apy} - \frac{\sigma^2}{8}$$

implemented as `risk_adjusted_apy(apy, sigma_annual)`.

### 3.3 Breakeven (implied) volatility $\sigma^*$

Providing full-range liquidity is, in expectation, equivalent to continuously delta-hedging a short at-the-money straddle: `apy` is the premium collected for selling volatility, and $\mathbb{E}[\mathrm{IL}]=\sigma^2/8$ is the cost of having sold it. The volatility level at which the premium exactly breaks even against that cost — the pool's own **implied volatility** — solves

$$\texttt{apy} = \frac{(\sigma^*)^2}{8} \quad\Longrightarrow\quad \sigma^* = \sqrt{8 \cdot \texttt{apy}}$$

implemented as `breakeven_volatility(apy)`. A useful structural fact: concentrating a position multiplies both fee income and IL by the same capital-efficiency factor $M$ (§6.1), so $M$ cancels out of this equation — $\sigma^*$ is a property of the pool's quoted APY alone, independent of which range one would choose to hold it in. This is what makes it a fair, range-independent basis for comparison across every pool in a scan.

## 4. Estimating realized volatility $\sigma$

$\sigma^*$ is only useful once compared against a trustworthy estimate of the asset's *actual* realized volatility. Two estimators are used depending on what the pool is quoted against.

### 4.1 Stablecoin-quoted pools: Yang-Zhang OHLC volatility

When the quote asset is a stablecoin (its own volatility $\approx 0$), IL is driven almost entirely by the bStock's own volatility. `best_available_volatility()` uses the **Yang-Zhang (2000)** estimator — drift-independent, and 5–14x more statistically efficient than close-to-close at the same sample size because it uses all four OHLC prices instead of only the close:

$$\sigma_{YZ}^2 = \sigma_{\text{overnight}}^2 + k\,\sigma_{\text{open-close}}^2 + (1-k)\,\sigma_{RS}^2, \qquad k = \frac{0.34}{1.34 + \frac{n+1}{n-1}}$$

where $\sigma_{RS}^2$ is the Rogers-Satchell term (`_rogers_satchell_variance`, drift-independent by construction) and $n$ is the sample size. Implemented in `yang_zhang_volatility()`; falls back to plain close-to-close log-return volatility (`annualized_volatility()`) when the OHLC data looks degenerate.

### 4.2 Non-stablecoin-quoted pools: relative volatility

For a pool quoted against a non-stablecoin asset (e.g. `NVDAB/BNB`), IL depends on the *relative* price move between the two pooled assets, not the bStock's volatility alone — a quote asset that moves too is real risk a bStock-only estimate would silently miss. `relative_annualized_volatility()` computes the annualized volatility of $\log(P_{\text{stock}}/P_{\text{quote}})$ over the time-aligned intersection of both klines series.

### 4.3 Data-sufficiency floors

An estimate from too little or too gappy data is worse than no estimate. `relative_annualized_volatility()` requires at least `MIN_ALIGNED_SAMPLES = 30` aligned candles *and* at least `MIN_ALIGNED_COVERAGE_RATIO = 80%` of the theoretical fully-dense span between them; falling short returns `sigma=None` with a stated reason, which propagates to an `UNSCOREABLE` verdict (§8) rather than a number computed from data too thin to trust.

## 5. The Richness Score (`vol_ratio`)

$$\texttt{vol\_ratio} = \frac{\sigma_{\text{realized}}}{\sigma^*}$$

implemented as `vol_richness_ratio()` — the options-trading-style "is this volatility richly priced" ratio, and the tool's single risk-adjusted headline number:

- $\texttt{vol\_ratio} < 1$: the pool pays more than the realized risk implies you should need — a rich premium, a good trade.
- $\texttt{vol\_ratio} \geq 1$: fee income doesn't cover the volatility actually observed — a cheap premium, a bad trade.

`richness_grade()` buckets this into three qualitative tiers used throughout the product's output:

| Grade | `vol_ratio` | Interpretation |
|---|---|---|
| **Rich** | $< 0.5$ | Pays well above realized risk |
| **Fair** | $[0.5, 1.0)$ | A modest edge |
| **Cheap** | $\geq 1.0$ | Doesn't clear its own risk bar |

Because $\sigma^*$ is range-independent (§3.3), `vol_ratio` is comparable across every pool in a scan regardless of size, concentration, or which underlying it trades — it is the axis a `scan` bubble chart plots pools against (see `SKILL.md`).

## 6. Concentrated liquidity: the range model

A full-range position is rarely optimal — concentrating liquidity into a narrower price range multiplies fee income, but also multiplies IL, by the same factor, and introduces a genuinely new risk: the price can leave the range entirely, after which the position earns no further fees until (if ever) it re-enters. `range_metrics()` and `recommend_range()` model this trade-off.

### 6.1 Concentration multiplier

For a range $[p_a, p_b]$ (price ratios to the current price), the Uniswap V3 capital-efficiency multiplier versus full-range liquidity is

$$M(p_a,p_b) = \frac{1}{1-\sqrt{p_a/p_b}}$$

implemented as `concentration_multiplier()`. A narrower range (larger $p_a/p_b$ closer to 1) gives a larger $M$: more fee income per dollar deposited, and proportionally more IL exposure while the price stays inside the range.

### 6.2 Probability of staying in range: exact double-barrier formula

For a straddling range ($p_a < 1 < p_b$), `p_active` is the probability a driftless lognormal price with volatility $\sigma$ **never** exits $[p_a,p_b]$ over the full horizon $T$ — not merely the probability it ends inside the range, since a position that exits and re-enters still stopped earning fees while it was out. In log-price space with $a=\ln p_a < 0 < b = \ln p_b$, $s = \sigma\sqrt{T}$, $w=b-a$, this is the classical double-barrier absorption probability for Brownian motion, solved exactly via the method-of-images reflection series:

$$P(\text{survive}) = \sum_{n=-N}^{N}\Big\{\big[\Phi(\tfrac{b-2nw}{s})-\Phi(\tfrac{a-2nw}{s})\big] - \big[\Phi(\tfrac{b-2a+2nw}{s})-\Phi(\tfrac{-a+2nw}{s})\big]\Big\}$$

implemented in `_exact_double_barrier_no_exit_probability()` (truncated at $N=15$, converging to machine precision — cross-checked against Monte Carlo path simulation in the test suite). This replaced an earlier, more conservative union-bound approximation ($1 - P(\text{touch }p_a) - P(\text{touch }p_b)$, `_union_bound_no_exit_probability`), kept as a fallback for degenerate inputs.

### 6.3 Single-sided ranges: a different question

A range entirely on one side of the current price ($p_a \geq 1$ or $p_b \leq 1$) behaves like a limit order — it earns nothing until price reaches it, then behaves as a sell/buy fill plus ongoing fee income. Here `p_active` asks a different question: not "does it survive," but "does it ever execute at all" — the probability the price ever *touches* the near boundary, via the single-barrier reflection-principle formula

$$P(\text{touch}) = 2\big(1-\Phi(|\ln(\text{barrier}/P_0)|/s)\big)$$

(`_single_barrier_touch_probability()`). This is typically a materially higher number than an equivalent-width straddling range's stay-probability, because touching once is a much weaker condition than never leaving.

### 6.4 Range-adjusted yield and IL; recommending a range

For a given range, `range_metrics()` combines the above into:

$$\texttt{effective\_apy} = \texttt{apy} \cdot M \cdot \texttt{p\_active}$$

$$\texttt{expected\_il} = \min\!\Big(\underbrace{M \cdot \tfrac{\sigma^2 T}{8}}_{\text{diffusion, scaled by }M},\ \underbrace{\max(\mathrm{IL}(p_a),\,\mathrm{IL}(p_b))}_{\text{IL if it ran to the boundary}}\Big)$$

$$\texttt{model\_net\_apy} = \texttt{effective\_apy} - \texttt{expected\_il}$$

The IL term takes the *minimum* of the concentration-scaled diffusion estimate and the exact boundary IL (§3.1) — a position that stays inside a narrow range never actually experiences the full boundary loss, so bounding by it prevents the diffusion scaling from overstating IL for very narrow ranges.

`recommend_range()` sweeps a fixed set of candidate widths — $\pm\{5,10,20,30,50,90\}\%$ straddling the price, plus full range, by default (`DEFAULT_STRADDLE_WIDTHS`; sided sweeps use $\{5,10,20,30,50\}\%$ offsets with a fixed $10\%$ band, `DEFAULT_SIDED_OFFSETS`/`SIDED_BAND_WIDTH`) — and recommends the one with the highest `model_net_apy` **among those clearing a safety floor**, `p_active ≥ SAFETY_P_ACTIVE_FLOOR = 0.6`. A range that would maximize modeled yield but has a low probability of actually surviving the year is not recommended over one that does; the floor is only relaxed (falling back to ranking all candidates by yield) when *no* candidate clears it.

## 7. Pre-deposit risk & plausibility screen

Independently of the yield model above, every candidate pool passes through `pool_risk_flags()` — a pool can be well risk-adjusted on paper and still fail this screen, in which case it is excluded (`NO_TRADE`) regardless of `vol_ratio`. Signals, each independently checkable:

**Data-plausibility** (is the advertised `apy` even real):
- `feeRate` per swap exceeding `MAX_SANE_FEE_RATE = 5%` — catches a dynamic/keeper-priced fee snapshot being misread as a static annualizable rate (observed live: a momentary `feeRate=838.86%/swap` on a V4 pool produced `apy=1658.77%`).
- Pool TVL below `MIN_SANE_TVL_USD = 5,000 USD` — below this, a single trade can dominate the annualized `apy` estimate.
- `apy` more than `PEER_APY_OUTLIER_MULTIPLE = 5x` the median `apy` of other pools on the same underlying ticker.

**Deposit-risk** (is putting money in actually safe):
- `investable = false` (delisted).
- Protocol-level `securityScore` below `MIN_PROTOCOL_SECURITY_SCORE = 50` (0-100 scale).
- **V4-generation hook risk**, hard-blocked by default (`block_unknown_v4_hooks=True`): a V4-style pool can carry an arbitrary custom hook — logic outside the audited core AMM — with no API-exposed way to inspect its address, permissions, or audit status. Detected structurally via `defiProtocolId` version suffix (e.g. `"uniswap4"`, `"pancakeswap4"`), not display name, since at least one major protocol markets its V4 under a name with no "V4" substring anywhere in it. Not a hypothetical risk: Fables (fables.fi/docs/security), a live production V4-hook exchange sophisticated enough to run three named dynamic-fee models with on-chain-bounded keeper overrides, states plainly on its own security page that no Fables-specific audit report is currently published — direct evidence that "a real V4 hook protocol" is not itself proof a given hook is safe to trust.

## 8. The decision rule

Every pool the product ever reports on resolves to exactly one of four verdicts:

| Verdict | Condition |
|---|---|
| `UNSCOREABLE` | Sigma or apy could not be computed at all (data fetch failed, no confirmed bStock pairing, insufficient/gappy kline history — §4.3), **or** sigma was computed but is too extreme (> ~283% annualized) for `expected_il_fraction` to produce a valid full-range IL estimate — §3.2 |
| `NO_TRADE` | Scored, but `pool_risk_flags()` raised at least one flag (§7) |
| `ENTER` | Cleared the risk screen **and** `model_net_apy > 0` **and** `vol_ratio < 1` |
| `WATCH` | Cleared the risk screen, but fails the `ENTER` yield/richness bar |

The `ENTER` bar (`passes_trade_gate()`) is deliberately a conjunction, not either condition alone: a pool can have positive `model_net_apy` while still being `Cheap` (fee income barely exceeds a modest IL cost without pricing in the risk actually observed), or have `vol_ratio<1` while still netting negative (the richness ratio is range-independent and doesn't itself guarantee the diffusion-approximation IL term nets out positive at full range). `WATCH` is not a negative judgment — it is a legitimate, safe pool that simply isn't attractive enough right now to call a "top pick"; only `NO_TRADE` and `UNSCOREABLE` pools are ever excluded from ranking.

`recommend`'s single verdict additionally refuses to answer at all when `UNSCOREABLE` pools exceed `UNSCOREABLE_RATIO_REFUSE_THRESHOLD = 50%` of all candidates — a scoreable remainder that small a fraction of the market may not be representative, and a confident-sounding verdict from it would overstate what the data actually supports.

## 9. Model limitations (explicit, by design)

These are documented in-code (`MODEL_APY_CAVEAT` and surrounding comments) and repeated here deliberately, because presenting `model_net_apy` without them overstates the model's certainty:

- **`apy` itself is a single blended fee+incentive figure.** The API exposes no breakdown, as-of timestamp, trading-volume figure, or lockup/incentive-expiry data on any pool sampled — there is no way from this data alone to tell a durable fee rate apart from one a single large trade or a brief volume spike happened to produce right before the snapshot, and an incentive-heavy `apy` can collapse with no warning either. Confirmed live, not hypothetical: the same pool's TVL and `apy` both swung sharply (TVL nearly halved, `apy` roughly tripled) between two checks minutes apart. Independently corroborated: Fables (fables.fi/docs/methodology), an unrelated live Uniswap-v4-hook exchange, documents annualizing its own headline pool APR the same way — a 24-hour fee window over current TVL — and its own docs (fables.fi/docs/apr) caution that figure is not a forecast and does not net out the value difference from holding, which is exactly why this model subtracts modeled IL to get `model_net_apy` rather than presenting the raw platform `apy`.
- **Driftless lognormal diffusion is a simplification.** Real prices drift and can jump (earnings, corporate actions); the diffusion approximation and the double-barrier probability both assume driftless geometric Brownian motion.
- **The `apy` used as a range's baseline is the platform's blended, full-range-equivalent figure**, scaled by $M$ — not a true per-tick fee rate, because the API does not expose one.
- **Single-sided range IL still reuses the straddling IL-vs-hold formula** as a cost proxy, not a model of actual execution price versus a plain limit order at the boundary — a known, explicitly scoped-out refinement (see `README.md` roadmap).
- **`securityScore` is per-protocol, not per-pool or per-hook** — it cannot distinguish a malicious/broken V4 hook from a benign one on the same reputable protocol, which is exactly why V4 hook risk is a separate hard block (§7) rather than folded into the security-score floor.
- **Point-estimate volatility.** `vol_ratio` uses a single realized-volatility estimate with no confidence interval; a short kline history makes that estimate noisier than it looks (tracked as a roadmap item — widening $\sigma$ by its sampling distribution before use).

## 10. Summary: what makes an LP "good" under this model

Collecting the pieces above into the actual definition this product uses: a pool is a **good LP** when the volatility its `apy` is willing to pay for ($\sigma^*$) meaningfully exceeds the volatility actually observed ($\sigma$) — i.e. `vol_ratio` well below 1 — *and* that richness survives being netted against a properly modeled expected impermanent loss at the position's actual concentration, with a stay-in-range probability high enough that the yield is likely to actually be realized, *and* the pool clears an independent plausibility/safety screen that has nothing to do with yield at all. None of these four conditions is sufficient on its own; the model is precisely their conjunction.
