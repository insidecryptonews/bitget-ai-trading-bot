from __future__ import annotations

from typing import Any

from .research_lab import ResearchMetrics


class WalkForwardValidator:
    def __init__(self, min_samples: int = 100) -> None:
        self.min_samples = min_samples

    def validate(self, rows: list[dict[str, Any]], blocks: int = 5) -> dict[str, Any]:
        labeled = sorted([row for row in rows if row.get("label") is not None], key=lambda row: str(row.get("timestamp") or ""))
        if len(labeled) < self.min_samples:
            return {
                "samples": len(labeled),
                "blocks": 0,
                "consistency_score": 0.0,
                "overfit_risk": 1.0,
                "stable": False,
                "reason": "muestras insuficientes",
                "block_metrics": [],
            }
        block_size = max(1, len(labeled) // blocks)
        chunks = [labeled[index:index + block_size] for index in range(0, len(labeled), block_size) if labeled[index:index + block_size]]
        metrics = [ResearchMetrics.calculate(chunk) for chunk in chunks]
        positive = sum(1 for item in metrics if item["profit_factor"] >= 1.2 and item["expectancy"] > 0 and item["sl_ratio"] < 0.5)
        consistency = positive / max(len(metrics), 1)
        return {
            "samples": len(labeled),
            "blocks": len(metrics),
            "consistency_score": consistency,
            "overfit_risk": 1.0 - consistency,
            "stable": consistency >= 0.6,
            "reason": "estable" if consistency >= 0.6 else "no estable por bloques temporales",
            "block_metrics": metrics,
        }

