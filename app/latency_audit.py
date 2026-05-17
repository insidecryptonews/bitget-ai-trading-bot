from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BotConfig
from .database import Database
from .utils import safe_float, safe_int


START = "LATENCY AUDIT START"
END = "LATENCY AUDIT END"


class LatencyAudit:
    """Compact latency report for VPS research/paper mode."""

    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.db.fetch_latency_metrics_since(since, limit=50000)
        by_metric = _summarize(rows)
        bottlenecks = sorted(by_metric.values(), key=lambda item: safe_float(item.get("p95_ms")), reverse=True)[:5]
        return {
            "hours": hours,
            "samples": len(rows),
            "metrics": by_metric,
            "bottlenecks": bottlenecks,
            "railway_status": {
                "status": "removed_or_disabled",
                "research_mode_ok": bool(self.config.worker_lightweight_mode and not self.config.live_trading),
                "paper_trading": bool(self.config.paper_trading),
            },
            "fast_execution_readiness": ["NOT_HFT", "RESEARCH_MODE_ONLY"],
            "recommendation": ["keep VPS for research/paper", "future VPS/WebSocket only after edge validation"],
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            f"samples: {payload['samples']}",
            "p50/p95/p99:",
            *_metric_lines(payload["metrics"]),
            "bottlenecks:",
            *_bottleneck_lines(payload["bottlenecks"]),
            "railway_status:",
            f"- status={payload['railway_status']['status']}",
            f"- research_mode_ok={str(payload['railway_status']['research_mode_ok']).lower()}",
            "fast_execution_readiness:",
            *[f"- {item}" for item in payload["fast_execution_readiness"]],
            "recommendation:",
            *[f"- {item}" for item in payload["recommendation"]],
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    index = (len(values) - 1) * max(0.0, min(1.0, pct))
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    fraction = index - lower
    return values[lower] * (1 - fraction) + values[upper] * fraction


def _summarize(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[float]] = {}
    for row in rows:
        name = str(row.get("metric_name") or "unknown")
        buckets.setdefault(name, []).append(safe_float(row.get("duration_ms")))
    out: dict[str, dict[str, Any]] = {}
    for name, values in buckets.items():
        out[name] = {
            "metric_name": name,
            "count": len(values),
            "p50_ms": percentile(values, 0.50),
            "p95_ms": percentile(values, 0.95),
            "p99_ms": percentile(values, 0.99),
            "max_ms": max(values) if values else 0.0,
        }
    return out


def _metric_lines(metrics: dict[str, dict[str, Any]]) -> list[str]:
    if not metrics:
        return ["- no latency metrics collected yet"]
    return [
        (
            f"- {name}: count={safe_int(row.get('count'))} "
            f"p50={safe_float(row.get('p50_ms')):.1f}ms p95={safe_float(row.get('p95_ms')):.1f}ms "
            f"p99={safe_float(row.get('p99_ms')):.1f}ms"
        )
        for name, row in sorted(metrics.items())
    ]


def _bottleneck_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        f"- {row.get('metric_name')} p95={safe_float(row.get('p95_ms')):.1f}ms max={safe_float(row.get('max_ms')):.1f}ms"
        for row in rows
    ]
