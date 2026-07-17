"""ATI forward paper simulation, isolated from every exchange execution path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

ACCOUNT_ID = "ATI_PAPER_50"
MODE = "PAPER_FORWARD_SIMULATION"
EXECUTION_MODE = "SIMULATION_ONLY"
POLICY_VERSION = "ATI_PAPER_SIMULATION_V1"
SOURCE_POLICY_VERSION = "ATI_SHADOW_POLICY_V2"
FINAL_RECOMMENDATION = "NO LIVE"

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RUNTIME_DIR = REPO_ROOT / "data" / "runtime" / "ati_paper"
DEFAULT_DB_PATH = DEFAULT_RUNTIME_DIR / "ati_paper.sqlite"
DEFAULT_STATUS_PATH = DEFAULT_RUNTIME_DIR / "executor_status.json"
DEFAULT_SIGNAL_PATH = REPO_ROOT / "reports" / "research" / "ati" / "ati_forward_signals.jsonl"
DEFAULT_SHADOW_STATE_PATH = REPO_ROOT / "reports" / "research" / "ati" / "ati_forward_state.json"


def safety_envelope() -> dict[str, Any]:
    return {
        "account_id": ACCOUNT_ID,
        "mode": MODE,
        "execution_mode": EXECUTION_MODE,
        "simulation_only": True,
        "research_only": True,
        "paper_trading": True,
        "paper_filter_enabled": False,
        "live_trading": False,
        "can_send_real_orders": False,
        "private_endpoints_used": False,
        "paper_ready": False,
        "live_ready": False,
        "final_recommendation": FINAL_RECOMMENDATION,
    }
