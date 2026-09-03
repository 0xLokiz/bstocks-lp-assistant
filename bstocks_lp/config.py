"""Shared constants, the JSON output envelope, and stablecoin-address config loading.

No dependencies on any other bstocks_lp module -- everything else in the package can safely
import this one without risking a cycle.
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone

DAYS_PER_YEAR = 365

# Bounds parallel `baw`/kline fetches in market_data.fetch_lp_investments (multi-page) and
# scan.run_scan (investment-info, klines, protocol-info) -- shared here rather than duplicated
# per module, since market_data and scan both need it and neither may import the other.
MAX_CONCURRENT_BAW_CALLS = 8

SCHEMA_VERSION = "1.0"

# Requested directly, after a real session that found and fixed two genuine calculation bugs
# in one sitting (see README "Recently shipped"): every command that produces a risk/yield
# judgment (scan, range, recommend, rebalance-check) must say, every time, that this tool's
# output is a model's estimate from actively-developed code, not a verified fact -- distinct
# from MODEL_APY_CAVEAT (il_model.py), which explains why one specific number can be wrong;
# this one says the tool itself can be wrong, and that's on the user to verify, not assume away.
DYOR_DISCLAIMER = ("This tool's numbers come from an automated model, not a human review, and "
                    "the code itself is under active development -- two real calculation bugs "
                    "were found and fixed in a single session of real usage (see README "
                    "\"Recently shipped\"). Nothing here is financial advice or a guarantee. "
                    "Always DYOR (do your own research) and verify independently before "
                    "depositing real funds -- treat every figure as a model's estimate, not a "
                    "fact.")

INTERVAL_TO_ANNUALIZATION = {
    "1d": DAYS_PER_YEAR,
    "4h": DAYS_PER_YEAR * 6,
    "1h": DAYS_PER_YEAR * 24,
}

BSTOCK_TYPE = 3  # RWA list `type`: 1=Ondo ("...on"), 2=xStocks ("...x"), 3=bStock ("...B")


def _json_envelope(status, **fields):
    """Consistent shape for every JSON blob this tool prints, success or error -- schema_version,
    status, run_id, and as_of are always present, so a consumer doesn't need different parsing
    logic for the error case than the success case. Before this, an error path printed a bare
    {"error": str(e)} with none of these fields, while a success path had no explicit status at
    all (implied only by the absence of "error") -- a real inconsistency, not a style nit: a
    scheduler parsing this output needed bespoke per-command logic just to reliably tell success
    from failure. `fields` are merged in on top (command-specific: results/positions/error/etc).
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "run_id": str(uuid.uuid4()),
        "as_of": datetime.now(timezone.utc).isoformat(),
        **fields,
    }


STABLECOIN_CONFIG_ENV_VAR = "BSTOCKS_STABLECOIN_CONFIG"
# __file__ here is bstocks_lp/config.py, so its own dirname is bstocks_lp/ -- stablecoins.json
# lives one directory up, at the repo root next to riskscreen.py. Getting this wrong fails
# *closed* (empty stablecoin set, silently falling every pool through to the stricter
# relative-volatility path) with only a stderr warning -- easy to miss, so this is deliberately
# spelled out rather than left as a one-line path expression a future edit could get wrong again.
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PACKAGE_DIR)
DEFAULT_STABLECOIN_CONFIG_PATH = os.path.join(_REPO_ROOT, "stablecoins.json")


def _load_stablecoin_addresses(config_path=None):
    """Load the (chainId, address) stablecoin set from a JSON config instead of a hardcoded
    constant in this file, so a new stablecoin -- or a wrong one -- is fixed by editing
    stablecoins.json, not shipping a code change. Path resolution: an explicit `config_path`
    argument, else the BSTOCKS_STABLECOIN_CONFIG env var, else the bundled stablecoins.json
    next to this script.

    Fails closed on a missing/malformed config: prints a warning and returns an empty set
    rather than crashing the whole script or silently keeping a stale in-code fallback. An
    empty set doesn't misclassify pools as unsafe -- is_stablecoin() already treats "not in
    the set" as non_stablecoin, so every pool just falls through to the stricter
    relative-volatility path (which converges to the same answer for a genuine stablecoin
    quote anyway, since its own volatility is ~0) instead of silently trusting a name.
    """
    path = config_path or os.environ.get(STABLECOIN_CONFIG_ENV_VAR) or DEFAULT_STABLECOIN_CONFIG_PATH
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {(str(e["chainId"]), e["address"].lower()) for e in data["stablecoins"]}
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"warning: could not load stablecoin config from {path} ({e}) -- treating no "
              f"quote asset as a stablecoin until this is fixed (safer than guessing wrong)",
              file=sys.stderr)
        return set()


STABLECOIN_ADDRESSES = _load_stablecoin_addresses()


def is_stablecoin(chain_id, address):
    """An address NOT in STABLECOIN_ADDRESSES is never presumed to be a stablecoin -- an
    unrecognized quote asset always falls through to the stricter relative-volatility path."""
    return (str(chain_id), address.lower()) in STABLECOIN_ADDRESSES
