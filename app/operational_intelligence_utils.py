from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Any, Iterable

from .cost_model import calculate_net_metrics_for_returns, explain_cost_breakdown
from .edge_hardening_utils import cost_config
from .score_calibration import load_score_rows, score_bucket_for
from .utils import safe_float, safe_int


FINAL_RECOMMENDATION = "NO LIVE"
LOW_SAMPLE_SOFT = 250
LOW_SAMPLE_HARD = 750


def load_operational_rows(db: Any, *, hours: int = 24, limit: int = 50000) -> list[dict[str, Any]]:
    """Read-only loader used by Fase 7 labs.

    It reuses the score-calibration normalization so market_probe/trade_signal
    separation and cost-model behavior stay consistent across the research labs.
    """

    del limit
    try:
        rows = load_score_rows(db, hours=max(1, int(hours or 24)))
    except Exception:
        rows = []
    return [normalize_row(row) for row in rows if normalize_row(row)]


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    score = safe_float(row.get("score") if row.get("score") is not None else row.get("confidence_score"))
    source = str(row.get("source") or "trade_signal").lower()
    if "probe" in source:
        source = "market_probe"
    elif not source:
        source = "trade_signal"
    hit = hit_class(row.get("first_barrier_hit") or row.get("label") or row.get("outcome"))
    return {
        "observation_id": row.get("observation_id") or row.get("id"),
        "timestamp": row.get("timestamp") or row.get("created_at") or row.get("label_timestamp"),
        "symbol": str(row.get("symbol") or "NA").upper(),
        "side": str(row.get("side") or "UNKNOWN").upper(),
        "market_regime": str(row.get("market_regime") or row.get("regime") or "unknown").upper(),
        "score": score,
        "score_bucket": score_bucket_for(score, row.get("score_bucket")),
        "source": source,
        "strategy": str(row.get("strategy") or row.get("strategy_type") or "NA"),
        "first_barrier_hit": hit,
        "return_pct": row_return(row),
        "mfe": first_float(row, "mfe", "max_favorable_excursion", "max_favorable_pct"),
        "mae": abs(first_float(row, "mae", "max_adverse_excursion", "max_adverse_pct")),
        "bars": first_float(row, "bars", "bars_to_outcome", "bars_tracked", "holding_bars"),
        "funding_rate": safe_float(row.get("funding_rate")),
        "already_includes_costs": bool(row.get("already_includes_costs")),
        "volume_change": first_float(row, "volume_change", "volume_relative", "volume_spike_proxy"),
        "volatility": first_float(row, "volatility", "normalized_atr", "atr_proxy"),
        "momentum": first_float(row, "momentum", "momentum_5", "recent_return_proxy"),
    }


def first_float(row: dict[str, Any], *keys: str) -> float:
    for key in keys:
        if row.get(key) is not None:
            return safe_float(row.get(key))
    return 0.0


def row_return(row: dict[str, Any]) -> float:
    for key in ("return_pct", "realized_return_pct", "final_return_pct", "simulated_return_pct"):
        if row.get(key) is not None:
            return safe_float(row.get(key))
    hit = hit_class(row.get("first_barrier_hit") or row.get("label"))
    if hit == "TP":
        return max(0.15, first_float(row, "mfe", "max_favorable_pct"))
    if hit == "SL":
        return -max(0.15, abs(first_float(row, "mae", "max_adverse_pct")))
    return 0.0


def hit_class(value: Any) -> str:
    text = str(value or "").upper()
    if text.startswith("TP") or text in {"WIN", "1"}:
        return "TP"
    if text == "SL" or text in {"LOSS", "-1"}:
        return "SL"
    return "TIME"


def group_by_keys(rows: Iterable[dict[str, Any]], keys: tuple[str, ...]) -> dict[tuple[str, ...], list[dict[str, Any]]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[tuple(str(row.get(key) or "NA") for key in keys)].append(row)
    return dict(groups)


def profit_factor(returns: Iterable[float]) -> float:
    values = [safe_float(value) for value in returns]
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    return gains / losses if losses > 0 else 999.0 if gains > 0 else 0.0


def max_drawdown(values: Iterable[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        equity += safe_float(value)
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return abs(worst)


def edge_metrics(rows: list[dict[str, Any]], config: Any | None = None) -> dict[str, Any]:
    costs = cost_config(config)
    samples = len(rows)
    returns = [row_return(row) for row in rows]
    hits = Counter(hit_class(row.get("first_barrier_hit")) for row in rows)
    breakdowns = [
        explain_cost_breakdown(
            source=str(row.get("source") or "trade_signal"),
            side=str(row.get("side") or ""),
            entry_type="taker",
            exit_type="taker",
            slippage_bps=safe_float(getattr(costs, "slippage_bps", 3.0)),
            entry_time=row.get("timestamp"),
            holding_bars=row.get("bars"),
            funding_rate=row.get("funding_rate") if safe_float(row.get("funding_rate")) else None,
            outcome=str(row.get("first_barrier_hit") or ""),
            already_includes_costs=bool(row.get("already_includes_costs")),
        )
        for row in rows
    ]
    net = calculate_net_metrics_for_returns(returns, breakdowns)
    mfes = [safe_float(row.get("mfe")) for row in rows]
    maes = [abs(safe_float(row.get("mae"))) for row in rows]
    time_ratio = hits["TIME"] / max(samples, 1)
    tp_ratio = hits["TP"] / max(samples, 1)
    sl_ratio = hits["SL"] / max(samples, 1)
    avg_cost_bps = sum(item.total_cost_bps for item in breakdowns) / max(len(breakdowns), 1)
    actionability = Counter(item.actionability for item in breakdowns).most_common(1)[0][0] if breakdowns else "UNKNOWN"
    return {
        "samples": samples,
        "tp_count": hits["TP"],
        "sl_count": hits["SL"],
        "time_count": hits["TIME"],
        "TP": tp_ratio,
        "SL": sl_ratio,
        "TIME": time_ratio,
        "gross_EV": sum(returns) / max(samples, 1),
        "gross_PF": profit_factor(returns),
        "net_EV": net["net_EV"],
        "net_PF": net["net_PF"],
        "avg_MFE": sum(mfes) / max(len(mfes), 1),
        "avg_MAE": sum(maes) / max(len(maes), 1),
        "median_MFE": median(mfes) if mfes else 0.0,
        "median_MAE": median(maes) if maes else 0.0,
        "drawdown_proxy": max_drawdown(returns),
        "avg_cost_bps": avg_cost_bps,
        "actionability": actionability,
        "confidence": confidence_class(samples),
        "returns": returns,
    }


def confidence_class(samples: int) -> str:
    if samples >= LOW_SAMPLE_HARD:
        return "HIGH"
    if samples >= LOW_SAMPLE_SOFT:
        return "MEDIUM"
    return "LOW"


def conservative_decision(metrics: dict[str, Any], *, source: str = "trade_signal") -> str:
    samples = safe_int(metrics.get("samples"))
    net_ev = safe_float(metrics.get("net_EV"))
    net_pf = safe_float(metrics.get("net_PF"))
    time_ratio = safe_float(metrics.get("TIME"))
    tp_ratio = safe_float(metrics.get("TP"))
    source_text = str(source or "trade_signal").lower()
    if source_text == "market_probe":
        return "NEED_MORE_DATA_NOT_ACTIONABLE" if net_ev > 0 else "REJECT_BAD_EDGE"
    if samples < LOW_SAMPLE_SOFT:
        return "NEED_MORE_DATA" if net_ev > 0 else "REJECT_BAD_EDGE"
    if net_ev <= 0 or net_pf < 1.05:
        return "REJECT_BAD_EDGE"
    if time_ratio > 0.8 and tp_ratio < 0.1:
        return "REJECT_TIME_DEATH"
    if samples < LOW_SAMPLE_HARD:
        return "RESEARCH_POCKET"
    return "SHADOW_CANDIDATE"


def safe_float_text(value: Any, digits: int = 4) -> str:
    number = safe_float(value)
    if not math.isfinite(number):
        number = 0.0
    return f"{number:.{digits}f}"


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def smoke_safety_lines() -> list[str]:
    return [
        "LIVE_TRADING=false",
        "DRY_RUN=true",
        "PAPER_TRADING=true",
        "ENABLE_PAPER_POLICY_FILTER=false",
        "can_send_real_orders=false",
        "final_recommendation: NO LIVE",
    ]
