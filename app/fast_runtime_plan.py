from __future__ import annotations

from typing import Any

from .config import BotConfig
from .latency_audit import LatencyAudit


START = "FAST RUNTIME PLAN START"
END = "FAST RUNTIME PLAN END"


class FastRuntimePlan:
    def __init__(self, config: BotConfig, db: Any | None = None) -> None:
        self.config = config
        self.db = db

    def build(self, hours: int = 24) -> dict[str, Any]:
        latency = LatencyAudit(self.config, self.db).build(hours=hours) if self.db is not None else {}
        metrics = latency.get("metrics", {}) if isinstance(latency, dict) else {}
        market_fetch = metrics.get("market_fetch_ms", {})
        decision = metrics.get("decision_ms", {})
        signal_generation = metrics.get("signal_generation_ms", {})
        return {
            "hours": hours,
            "current_runtime": "Railway polling research/paper",
            "market_fetch_ms": market_fetch,
            "decision_ms": decision,
            "signal_generation_ms": signal_generation,
            "polling_interval_seconds": self.config.scan_interval_seconds,
            "railway_limitations": [
                "polling HTTP",
                "cold restarts possible",
                "shared memory ceiling",
                "not suitable for HFT",
            ],
            "vps_preparation": [
                "single worker lock",
                "data restore from R2",
                "dashboard health checks",
                "latency metrics",
            ],
            "future_plan": [
                "WebSocket market stream",
                "async signal worker",
                "persistent Bitget connection",
                "precomputed policy gate",
                "dashboard separado del worker",
                "hard risk kill switch",
                "paper execution latency metrics",
                "future live gate disabled until strict review",
            ],
            "is_hft_ready": False,
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        market_fetch = payload["market_fetch_ms"]
        decision = payload["decision_ms"]
        signal = payload["signal_generation_ms"]
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            f"current_runtime: {payload['current_runtime']}",
            f"polling_interval_seconds: {payload['polling_interval_seconds']}",
            f"market_fetch_ms_p95: {market_fetch.get('p95_ms', 0)}",
            f"decision_ms_p95: {decision.get('p95_ms', 0)}",
            f"signal_generation_ms_p95: {signal.get('p95_ms', 0)}",
            "railway_limitations:",
            *[f"- {item}" for item in payload["railway_limitations"]],
            "vps_preparation:",
            *[f"- {item}" for item in payload["vps_preparation"]],
            "future_plan:",
            *[f"- {item}" for item in payload["future_plan"]],
            "is_hft_ready: false",
            "final_recommendation: NO LIVE",
            END,
        ])
