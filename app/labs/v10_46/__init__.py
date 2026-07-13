"""V10.46 — FINAL INTEGRATED EDGE & RAPID LEARNING (RESEARCH ONLY).

This package is the integrated research/simulation/shadow/paper architecture:
EventClock, canonical contracts, a single SimOMS, causal features, strategies,
AI agents, a prequential learner, champion/challenger, paired tournament, and
a promotion controller.

HARD SAFETY CONTRACT — enforced by tests, never relaxed:
  * PAPER_TRADING=True, LIVE_TRADING=False, DRY_RUN=True
  * can_send_real_orders is FALSE and there is NO code path that can flip it
  * no real order execution, no private exchange endpoints, no .env access,
    no leverage/margin/sizing changes to the production bot
  * every component here is REPLAY / SIMULATION / SHADOW / PAPER RESEARCH ONLY

`LIVE` is never an executable state. Live readiness is a REPORT plus a runbook
gated by an independent audit; enabling live is a human decision outside the
scope of this package.
"""

from __future__ import annotations

VERSION = "v10.46"
FINAL_RECOMMENDATION = "NO LIVE"

# Immutable safety state. Anything that would make live execution possible must
# fail the safety tests. These booleans are read-only research facts.
SAFETY_STATE = {
    "paper_trading": True,
    "live_trading": False,
    "dry_run": True,
    "can_send_real_orders": False,
    "paper_filter_enabled": False,
    "uses_private_endpoints": False,
    "reads_or_writes_dotenv": False,
    "connects_real_execution_engine": False,
    "modifies_leverage_margin_sizing": False,
    "mode": "REPLAY_SIMULATION_SHADOW_PAPER_RESEARCH_ONLY",
    "final_recommendation": FINAL_RECOMMENDATION,
}

# Tokens that, if they appeared as real call sites in this package's source,
# would indicate a live/private/order path. The safety test scans for them.
FORBIDDEN_SOURCE_TOKENS = (
    "ExecutionEngine", "place_order", "create_order", "submit_order",
    "cancel_order(", "/api/v2/mix/order", "/api/v2/spot/trade",
    "private/order", "set_leverage", "set_margin", "adjust_position",
    "LIVE_TRADING = True", "LIVE_TRADING=True",
    "can_send_real_orders = True", "can_send_real_orders=True",
    '"can_send_real_orders": True',
)


def assert_research_only() -> dict:
    """Return the safety state, raising if any live-enabling flag is set.
    Called at the top of every executable entry point in this package."""
    s = SAFETY_STATE
    if s["live_trading"] or s["can_send_real_orders"] \
            or s["uses_private_endpoints"] or s["connects_real_execution_engine"] \
            or not s["paper_trading"] or not s["dry_run"]:
        raise RuntimeError("SAFETY_VIOLATION: research-only invariant broken")
    return dict(s)


def safety_banner() -> str:
    return ("RESEARCH ONLY · NO LIVE · can_send_real_orders=false · "
            "PAPER_TRADING=True · LIVE_TRADING=False · DRY_RUN=True")
