from __future__ import annotations

from typing import Any

from .latency_audit import LatencyAudit


START = "FAST RUNTIME READINESS START"
END = "FAST RUNTIME READINESS END"


class FastRuntimeReadiness:
    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        latency = _safe(lambda: LatencyAudit(self.config, self.db).build(hours=hours), {})
        return {
            "hours": max(1, int(hours or 24)),
            "current_mode": "VPS research/paper",
            "is_hft_ready": False,
            "is_ok_for_research": True,
            "latency_audit": latency,
            "latency_budget": {
                "market_data_receive_ms": "future_websocket_metric",
                "signal_generation_ms": "measure_p50_p95_p99",
                "policy_gate_ms": "precompute_before_enforce",
                "paper_order_decision_ms": "paper_only",
            },
            "bottlenecks": latency.get("bottlenecks", []) if isinstance(latency, dict) else [],
            "requirements_before_fast_runtime": [
                "websocket market stream",
                "async signal worker",
                "persistent exchange connection for paper metrics only",
                "precomputed policy gate",
                "strict risk kill switch",
                "no duplicate worker lock violations",
            ],
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            f"current_mode: {payload['current_mode']}",
            "is_hft_ready=false",
            "is_ok_for_research=true",
            "latency_budget:",
            *[f"- {key}={value}" for key, value in payload["latency_budget"].items()],
            "requirements_before_fast_runtime:",
            *[f"- {item}" for item in payload["requirements_before_fast_runtime"]],
            "final_recommendation: NO LIVE",
            END,
        ])


def _safe(fn, fallback):
    try:
        return fn()
    except Exception:
        return fallback
