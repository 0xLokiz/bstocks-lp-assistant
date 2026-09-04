"""The pre-deposit risk & plausibility screen, independent of the yield model: is the
advertised APY even real, and is the pool safe to put money into. See MODEL.md section 7.
"""

import re
import statistics

from bstocks_lp import il_model, market_data

MAX_SANE_FEE_RATE = 0.05      # 5% per swap -- generous; real fee tiers top out around 1%
MIN_SANE_TVL_USD = 5_000       # below this, a single trade can dominate the annualized apy
PEER_APY_OUTLIER_MULTIPLE = 5  # flag if > 5x the median apy of other pools on the same ticker
MIN_PEER_SAMPLE_SIZE = 3       # need at least this many *other* pools on the same ticker before
                                # "median" means anything -- see pool_risk_flags docstring
MIN_PROTOCOL_SECURITY_SCORE = 50  # `defi protocol-info` securityScore floor (0-100)

_PROTOCOL_ID_VERSION_RE = re.compile(r"(\d+)$")


def _protocol_carries_unaudited_hook_risk(protocol_id, protocol_name):
    """True if this protocol's version generation is 4 or later -- V4-style pluggable-hook
    architecture, i.e. custom logic outside the audited core AMM that this tool has no way to
    inspect. Checked primarily via the structured `defiProtocolId` (e.g. "uniswap4",
    "pancakeswap4"), not the display name: PancakeSwap's own V4 is marketed as "PancakeSwap
    Infinity" with no "v4"/"V4" substring anywhere in that name, so a pure name-match (which
    this replaced) silently let it through the hard block -- confirmed live, not hypothetical
    (defiProtocolId="pancakeswap4", protocolName="PancakeSwap Infinity", 3 such pools live on
    BSC at the time this was fixed). Falls back to a name-text match when defiProtocolId is
    missing or doesn't end in a version number, so an unrecognized id scheme doesn't silently
    disable the check.
    """
    if protocol_id:
        m = _PROTOCOL_ID_VERSION_RE.search(protocol_id)
        if m:
            return int(m.group(1)) >= 4
    return "v4" in (protocol_name or "").lower()


def pool_risk_flags(pool, info, apy, peer_apys=None, protocol_security_score=None,
                     max_fee_rate=MAX_SANE_FEE_RATE, min_tvl_usd=MIN_SANE_TVL_USD,
                     peer_outlier_multiple=PEER_APY_OUTLIER_MULTIPLE,
                     min_peer_sample_size=MIN_PEER_SAMPLE_SIZE,
                     min_security_score=MIN_PROTOCOL_SECURITY_SCORE,
                     block_unknown_v4_hooks=True):
    """Aggregate pre-deposit sanity/safety screen for one LP pool -- generalizes the original
    single feeRate check into independent signals for the two questions that actually matter
    before recommending a deposit: is the advertised yield even real (data-plausibility), and
    is the pool safe to put money into (deposit risk)? Returns a list of human-readable flag
    strings; empty = no flags raised. A pool can trip more than one signal.

    Data-plausibility signals (apy may not be real):
      - feeRate outside a sane per-swap range (catches the exact QQQB-USDC V4 case: a
        feeRate of 838.86%/swap produced apy=1658.77% vs 77.86% on the equivalent V3 pool).
        NOTE ON CAUSE: an extreme snapshot doesn't necessarily mean the pool is broken or
        malicious -- legitimate V4 protocols run hooks that reprice the swap fee per-transaction
        from realized volatility, session/calendar state, or order flow direction. Fables
        (fables.fi/docs/swap-fee) is a real, live example, not a hypothetical: it names three
        such models (Calendar, Flat base, Directional) and documents that even its keeper-driven
        overrides are bounded on-chain -- capped at a 50% discount off the model rate, a 72-hour
        max time-to-live, and an immutable absolute fee ceiling in the contract bytecode. Those
        bounds are exactly the kind of hook-level detail this tool has no API access to for any
        given pool (see block_unknown_v4_hooks below) -- knowing that legitimate bounds *can*
        exist somewhere on-chain doesn't mean this tool can see them for the pool in front of it.
        A single feeRate read is then a live, momentary number, not a stable rate -- annualizing
        it as if it were static (which is what produces `apy`) is structurally wrong regardless
        of whether the hook itself is legitimate and bounded. Flag it as "not a static rate we
        can annualize," not as "probably malicious."
      - TVL below a floor where a single trade can dominate the annualized apy estimate.
      - apy is a large outlier versus other pools on the *same* underlying ticker -- this is
        the general form of the feeRate check: it catches any mechanism (stale data, a
        calculation bug, a temporary spike, a hook doing something unexpected) that produces
        an implausible apy, not just the one specific cause already identified once. Compared
        against the caller's already-resolved `apy` (via `market_data.resolve_pool_apy`), not
        recomputed here -- this function used to re-derive apy from `info` alone with a
        narrower 2-tier fallback, missing the 3rd (`pool.get("apy")`) tier the canonical
        resolution uses, so a pool whose `info` carried neither `apy` nor `apyBps` could
        silently outlier-check against apy=0.0 while every other reading of the same pool used
        its real value -- fixed by taking the resolved apy as a parameter instead. `peer_apys`
        must already exclude this pool's own apy (by identity, e.g. investmentId) -- it is used
        as-is, not filtered by value, so two distinct pools that happen to share an apy don't
        wrongly exclude each other. Only fires when at least `min_peer_sample_size` peers exist
        (default 3): `statistics.median` of a 1-element list is just that element, so with a
        single peer "N times the median" is really just "N times one other pool's apy" -- you
        can't tell which of the two is actually the odd one out, and that peer can itself be
        noisy. Confirmed live, not hypothetical: a GMEB-USDT (PancakeSwap V3) pool was flagged
        as "6.0x the median (84.7%)" against its only peer (the Uniswap V4 GMEB-USDT pool) --
        that peer's own apy had moved to 123.20% by the time it was checked directly minutes
        later, and the flagged pool's feeRate (0.25%, a standard PancakeSwap tier), TVL
        ($245K), and lack of any reward-token incentive showed no actual defect. A real
        multi-pool outlier (the QQQB-USDC case this check was built for) stays caught either
        way, since it also has enough same-ticker peers and an independently broken feeRate.

    Deposit-risk signals (money going in may not be safe):
      - `investable=false` -- delisted, no new deposits possible.
      - protocol-level `securityScore` (from `defi protocol-info`) below a floor.
        IMPORTANT LIMITATION: this score is per-*protocol*, not per-pool or per-hook -- Uniswap
        V3 and V4 pools both score 95.18 because it's the same organization, so it CANNOT catch
        a malicious/broken hook on an otherwise-reputable protocol (that's exactly the QQQB
        case: Uniswap's own score gives no warning). This signal is a weak floor against
        obviously disreputable protocols, not a substitute for the hook-level audit tracked in
        the README roadmap -- do not present it as though it clears a V4 pool as hook-safe.
      - `block_unknown_v4_hooks` (default True): V4-generation pools can carry an arbitrary
        custom hook -- logic outside the audited core AMM, and this product has no API access
        to a pool's hook address, permissions, or audit status (see the securityScore
        limitation above -- protocol-level score can't see it either). Per the PM/QA review,
        contract risk this unknown is a hard block by default, not just a caveat: every
        V4-generation pool is flagged until real hook-inspection data is available, not only
        ones with an already-visible symptom like an extreme feeRate. Detected primarily via
        the structured `defiProtocolId` (e.g. "uniswap4"), not the display name -- see
        `_protocol_carries_unaudited_hook_risk`. Pass False to disable for an already-vetted
        pool or explicit user override. This default isn't excess caution over a hypothetical:
        Fables' own security page (fables.fi/docs/security) states plainly that no
        Fables-specific audit report is currently published, for a protocol sophisticated
        enough to run three named fee models with bounded keeper overrides (see the feeRate
        note above). Being a real, live V4 hook protocol was never going to be evidence that
        a given hook is safe to trust blindly -- a live example admitting exactly that about
        itself confirms it.
    """
    flags = []

    protocol_id = (pool.get("defiProtocolId") or info.get("defiProtocolId") or "")
    protocol_name = (pool.get("protocolName") or info.get("protocolName") or "")
    if block_unknown_v4_hooks and _protocol_carries_unaudited_hook_risk(protocol_id, protocol_name):
        flags.append(f"{protocol_name}: V4-generation pools can carry an arbitrary custom hook "
                      f"with unaudited logic, and this tool has no way to inspect the hook's address, "
                      f"permissions, or audit status. Blocked by default until that's available "
                      f"(pass block_unknown_v4_hooks=False / --allow-v4 to override)")

    fee_rate = info.get("feeRate")
    if fee_rate is not None:
        try:
            fee_rate = float(fee_rate)
            if fee_rate > max_fee_rate:
                flags.append(f"feeRate={fee_rate*100:.2f}%/swap exceeds a sane range (>{max_fee_rate*100:.0f}%) "
                              f"-- likely a dynamic/keeper-priced fee snapshot, not a static rate; "
                              f"annualizing it into apy is not meaningful either way")
        except (TypeError, ValueError):
            pass

    tvl = market_data.resolve_pool_tvl(pool, info)
    if tvl < min_tvl_usd:
        flags.append(f"TVL ${tvl:,.0f} is below ${min_tvl_usd:,.0f} -- apy from this little "
                      f"liquidity is statistically noisy, easily swung by a single trade")

    if peer_apys and len(peer_apys) >= min_peer_sample_size:
        median = statistics.median(peer_apys)
        if median > 0 and apy > median * peer_outlier_multiple:
            flags.append(f"apy is {apy/median:.1f}x the median ({median*100:.1f}%) of other pools "
                          f"on the same token -- outlier, treat as unverified until explained")

    if info.get("investable") is False:
        flags.append("product is delisted (investable=false) -- no new deposits possible")

    if protocol_security_score is not None:
        try:
            score = float(protocol_security_score)
            if score < min_security_score:
                flags.append(f"protocol security score {score:.0f}/100 is below {min_security_score} "
                              f"-- elevated smart-contract risk at the protocol level")
        except (TypeError, ValueError):
            pass

    return flags


def evaluate_pool(pool, info, sigma, apy, peer_apys=None, protocol_security_score=None,
                   max_fee_rate=MAX_SANE_FEE_RATE, min_tvl_usd=MIN_SANE_TVL_USD,
                   peer_outlier_multiple=PEER_APY_OUTLIER_MULTIPLE,
                   min_peer_sample_size=MIN_PEER_SAMPLE_SIZE,
                   min_security_score=MIN_PROTOCOL_SECURITY_SCORE, block_unknown_v4_hooks=True):
    """The one evaluation path for a pool with an already-resolved sigma/apy -- used by both
    `run_scan` and `rebalance-check`'s market comparison, so the two can never reach
    inconsistent safety conclusions about the same pool. (Before this existed, rebalance-check
    ran its own stripped-down check that skipped peer_apys/protocol_security_score entirely --
    a pool `scan` would flag could still show up there as a "better" rebalance target.)

    Returns {"flags": [...], "scored": {...}}. `scored` (from risk_adjusted_apy) is always
    computed, even when flags is non-empty, so a caller can show the numbers alongside a
    prominent "don't trust this" -- but never rank or recommend a pool with any flags.
    """
    flags = pool_risk_flags(pool, info, apy, peer_apys=peer_apys, protocol_security_score=protocol_security_score,
                             max_fee_rate=max_fee_rate, min_tvl_usd=min_tvl_usd,
                             peer_outlier_multiple=peer_outlier_multiple,
                             min_peer_sample_size=min_peer_sample_size,
                             min_security_score=min_security_score,
                             block_unknown_v4_hooks=block_unknown_v4_hooks)
    return {"flags": flags, "scored": il_model.risk_adjusted_apy(apy, sigma)}
