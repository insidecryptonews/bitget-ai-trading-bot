from __future__ import annotations

from typing import Any

from .config import BotConfig
from .database import Database


START = "FAST EXECUTION READINESS START"
END = "FAST EXECUTION READINESS END"


class FastExecutionReadiness:
    def __init__(self, config: BotConfig, db: Database | None = None) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        return {
            "current_mode": "Railway research/paper",
            "is_hft_ready": False,
            "is_ok_for_research": bool(self.config.worker_lightweight_mode and self.config.paper_trading and not self.config.live_trading),
            "future_vps_plan": [
                "websocket_market_stream",
                "async_execution_worker",
                "persistent_exchange_connection",
                "latency_metrics",
                "precomputed_policy_gate",
                "strict_risk_kill_switch",
            ],
            "migration_conditions": [
                "paper policy PF stable",
                "walk-forward stable",
                "no news risk block",
                "low drawdown",
                "enough samples",
            ],
            "final_recommendation": "STAY_ON_RAILWAY_FOR_NOW",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"current_mode: {payload['current_mode']}",
            f"is_hft_ready: {str(payload['is_hft_ready']).lower()}",
            f"is_ok_for_research: {str(payload['is_ok_for_research']).lower()}",
            "future_vps_plan:",
            *[f"- {item}" for item in payload["future_vps_plan"]],
            "migration_conditions:",
            *[f"- {item}" for item in payload["migration_conditions"]],
            f"final_recommendation: {payload['final_recommendation']}",
            END,
        ])
