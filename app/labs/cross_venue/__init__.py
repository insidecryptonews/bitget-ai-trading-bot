"""Cross-venue public-market research and isolated paper simulation.

This package deliberately has no dependency on any exchange execution client.
All network adapters are public WebSocket readers and all account activity is a
local simulation kept outside the existing ATI/P11 ledgers.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

ACCOUNT_ID = "CROSS_VENUE_PAPER_50"
MODE = "RESEARCH_PAPER_ONLY"
POLICY_VERSION = "CROSS_VENUE_RESEARCH_V1"
FINAL_RECOMMENDATION = "NO LIVE"

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = REPO_ROOT / "config" / "cross_venue" / "CROSS_VENUE_RESEARCH_V1.json"
PROVIDER_INVENTORY_PATH = REPO_ROOT / "config" / "cross_venue" / "providers_v1.json"
STAGING_ROOT = REPO_ROOT / "external_data" / "staging" / "cross_venue_v1"
RUNTIME_ROOT = REPO_ROOT / "data" / "runtime" / "cross_venue"
LEDGER_PATH = RUNTIME_ROOT / "cross_venue_paper.sqlite"
ENGINE_STATUS_PATH = RUNTIME_ROOT / "engine_status.json"
ENGINE_SNAPSHOT_PATH = RUNTIME_ROOT / "dashboard_snapshot.json"


@lru_cache(maxsize=1)
def code_revision() -> str:
    """Return the code revision once, marking uncommitted runs explicitly."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True,
            text=True, timeout=3, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout.strip()
        return f"{head}-DIRTY" if dirty else head
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN_CODE_REVISION"


def safety_envelope() -> dict[str, Any]:
    return {
        "account_id": ACCOUNT_ID,
        "mode": MODE,
        "simulation_only": True,
        "research_only": True,
        "shadow_only": True,
        "paper_trading": True,
        "paper_filter_enabled": False,
        "live_trading": False,
        "can_send_real_orders": False,
        "uses_api_keys": False,
        "uses_private_endpoints": False,
        "sends_orders": False,
        "leverage_simulation_only": True,
        "paper_ready": False,
        "live_ready": False,
        "edge_validated": False,
        "not_actionable": True,
        "final_recommendation": FINAL_RECOMMENDATION,
    }
