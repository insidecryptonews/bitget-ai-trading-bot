"""Adrian Trading Intelligence V2, deterministic shadow research only.

The package is intentionally isolated from exchange clients, execution engines,
paper routing, configuration mutation, and database writes.  It consumes only
validated OHLCV snapshots and emits ignored research artefacts.
"""

from __future__ import annotations

FINAL_RECOMMENDATION = "NO LIVE"
MODE = "SHADOW_RESEARCH_ONLY"
POLICY_VERSION = "ATI_SHADOW_POLICY_V2"
FEATURE_VERSION = "ATI_FEATURES_V2"


def safety_envelope() -> dict[str, object]:
    return {
        "mode": MODE,
        "research_only": True,
        "shadow_only": True,
        "paper_trading": False,
        "paper_filter_enabled": False,
        "live_trading": False,
        "can_send_real_orders": False,
        "private_endpoints_used": False,
        "fills_are_simulated": True,
        "paper_ready": False,
        "live_ready": False,
        "activation": "disabled",
        "final_recommendation": FINAL_RECOMMENDATION,
    }
