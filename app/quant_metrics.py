from __future__ import annotations

import math
from typing import Any

from .utils import safe_float


def bps_to_fraction(bps: float) -> float:
    return safe_float(bps) / 10000.0


def bps_to_percent_points(bps: float) -> float:
    return safe_float(bps) / 100.0


def annualization_factor(timeframe: str | None) -> dict[str, Any]:
    tf = str(timeframe or "").lower().strip()
    if tf in {"1d", "1day", "daily", "day"}:
        return {"factor": math.sqrt(252), "sharpe_status": "OK_DAILY"}
    if tf in {"1h", "60m", "hourly"}:
        return {"factor": math.sqrt(365 * 24), "sharpe_status": "OK_CRYPTO_HOURLY"}
    if tf in {"5m", "5min"}:
        return {"factor": math.sqrt(365 * 24 * 12), "sharpe_status": "OK_CRYPTO_5M"}
    if tf in {"15m", "15min"}:
        return {"factor": math.sqrt(365 * 24 * 4), "sharpe_status": "OK_CRYPTO_15M"}
    return {"factor": 0.0, "sharpe_status": "UNKNOWN_TIMEFRAME"}


def profit_factor_with_status(returns: list[float]) -> dict[str, Any]:
    values = [safe_float(value) for value in returns]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    if not values:
        return {"profit_factor": 0.0, "pf_status": "NO_TRADES", "metric_reliability": "LOW_SAMPLE"}
    if not losses and wins:
        return {"profit_factor": float("inf"), "pf_status": "INSUFFICIENT_LOSSES", "metric_reliability": sample_reliability(len(values))}
    pf = sum(wins) / abs(sum(losses)) if losses else 0.0
    return {"profit_factor": pf, "pf_status": "OK", "metric_reliability": sample_reliability(len(values))}


def sample_reliability(trades: int) -> str:
    if trades < 100:
        return "LOW_SAMPLE"
    if trades < 500:
        return "NO_LIVE_READINESS"
    return "OOS_REQUIRED"


def cost_sensitive_edge(gross_ev_fraction: float, cost_bps: float) -> bool:
    return 0 < safe_float(gross_ev_fraction) < bps_to_fraction(cost_bps)
