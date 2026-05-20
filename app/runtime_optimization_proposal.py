from __future__ import annotations

from typing import Any

from .operational_intelligence_utils import FINAL_RECOMMENDATION


class RuntimeOptimizationProposal:
    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        del hours, self.config, self.db
        return {
            "fetch_parallel_enabled": False,
            "implementation_mode": "proposal_only",
            "reason": "No se toca el worker principal hasta medir 429/timeouts en shadow.",
            "safe_plan": [
                "ThreadPoolExecutor conservador solo en audit/research.",
                "max_workers bajo y timeouts por simbolo.",
                "error parcial no rompe ciclo.",
                "guard explicito para API 429.",
                "sin aumentar frecuencia, slots ni ejecucion.",
            ],
            "api_429_guard": "REQUIRED_BEFORE_ENABLE",
            "websocket_status": "PROPOSAL_ONLY",
            "research_only": True,
            "final_recommendation": FINAL_RECOMMENDATION,
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            "RUNTIME OPTIMIZATION PROPOSAL START",
            f"fetch_parallel_enabled: {str(payload['fetch_parallel_enabled']).lower()}",
            f"implementation_mode: {payload['implementation_mode']}",
            f"reason: {payload['reason']}",
            "safe_plan:",
            *[f"- {item}" for item in payload["safe_plan"]],
            "no_slots_changed=true",
            "no_frequency_increase=true",
            "final_recommendation: NO LIVE",
            "RUNTIME OPTIMIZATION PROPOSAL END",
        ]
        return "\n".join(lines)
