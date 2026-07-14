"""V10.47.8 cost data-truth (RESEARCH ONLY).

The V10.47 tournament labelled its costs "observed" while they were in fact a
FIXED basis-point table. That is MODELLED, not OBSERVED. This module states the
honest data-truth status of every cost component so no report can claim an
observed execution it does not have.

Status vocabulary: OBSERVED (measured from real book/prints at the trade time),
MODELLED (a fixed/parametric assumption), PROXY (derived from an unrelated but
available signal), UNAVAILABLE (no reproducible public source).
"""

from __future__ import annotations

from . import sim_oms as S


def cost_truth(scenario_cost: str = "observed") -> dict:
    """Return the data-truth status of each cost component for a cost scenario.
    NB: the SimOMS scenario key 'observed' is a MODELLED table, relabelled here."""
    c = S.COST_SCENARIOS[scenario_cost]
    common = {"symbol": None, "venue": None,
              "timestamp": "constant (not per-trade)", "confidence": "low"}
    return {
        "scenario_cost_key": scenario_cost,
        "note": "SimOMS cost tables are parametric assumptions, NOT observed "
                "execution. The scenario key name is not a data-truth claim.",
        "components": {
            "fee": {"value_bps_per_side": c["taker_fee_bps"], "method": "exchange "
                    "public taker schedule", "status": "MODELLED", **common},
            "spread": {"value_bps": c["spread_bps"], "method": "fixed assumption",
                       "status": "MODELLED", **common},
            "slippage": {"value_bps": c["slippage_bps"], "method": "fixed "
                         "assumption", "status": "MODELLED", **common},
            "funding": {"value_bps_per_8h": c["funding_bps_per_8h"],
                        "method": "fixed rate applied only on real 0/8/16 UTC "
                        "settlement crossings; real historical sign/rate not "
                        "sourced", "status": "PROXY", **common},
            "book_liquidity": {"status": "UNAVAILABLE", "method": "no reproducible "
                               "free historical L2 order book", **common},
            "real_open_interest": {"status": "UNAVAILABLE", "method": "no "
                                   "reproducible free historical OI feed", **common},
        },
        "summary": {"observed": 0, "modelled": 3, "proxy": 1, "unavailable": 2},
    }
