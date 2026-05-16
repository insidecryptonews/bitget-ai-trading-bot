from __future__ import annotations

from typing import Any


START = "WEBSOCKET MIGRATION PLAN START"
END = "WEBSOCKET MIGRATION PLAN END"


class WebsocketMigrationPlan:
    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        return {
            "hours": max(1, int(hours or 24)),
            "scope": "research_only_plan",
            "paper_only": True,
            "live_allowed": False,
            "architecture": [
                "websocket_market_stream",
                "async_signal_worker",
                "precomputed_policy_gate",
                "paper_execution_latency_metrics",
                "dashboard_process_separate_from_worker",
                "worker_lock_and_idempotency_keys",
                "strict_risk_kill_switch",
            ],
            "latency_budget": [
                "market data receive",
                "feature build",
                "signal generation",
                "policy gate",
                "paper order decision timestamp",
                "db write",
            ],
            "not_implemented_now": [
                "live order execution",
                "real websocket trading",
                "slot expansion",
                "leverage or margin changes",
            ],
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            "scope: research_only_plan",
            "paper_only=true",
            "live_allowed=false",
            "architecture:",
            *[f"- {item}" for item in payload["architecture"]],
            "latency_budget:",
            *[f"- {item}" for item in payload["latency_budget"]],
            "not_implemented_now:",
            *[f"- {item}" for item in payload["not_implemented_now"]],
            "final_recommendation: NO LIVE",
            END,
        ])
