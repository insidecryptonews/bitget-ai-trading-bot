from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .cost_model import explain_cost_breakdown
from .utils import safe_float, safe_int


DECISION_REJECT = "REJECT"
DECISION_WATCH = "WATCH_ONLY"
DECISION_SHADOW = "SHADOW_CANDIDATE"
DECISION_PAPER = "PAPER_CANDIDATE"
FINAL_NO_LIVE = "NO LIVE"


@dataclass(frozen=True)
class NetCostConfig:
    taker_fee_bps: float
    maker_fee_bps: float
    slippage_bps: float
    funding_bps_per_8h: float
    min_net_pf: float
    min_samples: int
    min_tp_ratio: float
    max_time_ratio: float


def since_iso(hours: int) -> str:
    hours = max(1, int(hours or 24))
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def cost_config(config: Any) -> NetCostConfig:
    return NetCostConfig(
        taker_fee_bps=safe_float(getattr(config, "net_edge_taker_fee_bps", 6.0)),
        maker_fee_bps=safe_float(getattr(config, "net_edge_maker_fee_bps", 2.0)),
        slippage_bps=safe_float(getattr(config, "net_edge_slippage_bps", 3.0)),
        funding_bps_per_8h=safe_float(getattr(config, "net_edge_funding_bps_per_8h", 1.0)),
        min_net_pf=safe_float(getattr(config, "net_edge_min_net_pf", 1.20)),
        min_samples=safe_int(getattr(config, "net_edge_min_samples", 500)),
        min_tp_ratio=safe_float(getattr(config, "net_edge_min_tp_ratio", 0.05)),
        max_time_ratio=safe_float(getattr(config, "net_edge_max_time_ratio", 0.80)),
    )


def score_bucket_expr() -> str:
    return """
        CASE
            WHEN COALESCE(so.confidence_score, 0) >= 95 THEN '95-100'
            WHEN COALESCE(so.confidence_score, 0) >= 90 THEN '90-94'
            WHEN COALESCE(so.confidence_score, 0) >= 80 THEN '80-89'
            WHEN COALESCE(so.confidence_score, 0) >= 70 THEN '70-79'
            WHEN COALESCE(so.confidence_score, 0) >= 60 THEN '60-69'
            ELSE '<60'
        END
    """


def fetch_group_metrics(
    db: Any,
    *,
    since: str,
    group_key: str,
    limit: int = 30,
    min_samples: int = 1,
) -> list[dict[str, Any]]:
    allowed = {
        "symbol": "COALESCE(so.symbol, 'NA')",
        "side": "COALESCE(so.side, 'NA')",
        "market_regime": "COALESCE(so.market_regime, 'NA')",
        "score_bucket": f"COALESCE(NULLIF(so.score_bucket, ''), {score_bucket_expr()})",
        "strategy": "COALESCE(so.strategy_type, 'NA')",
        "source": "COALESCE(spm.source, CASE WHEN COALESCE(so.shadow_strategy, 0) = 1 THEN 'shadow_signal' ELSE 'trade_signal' END)",
        "policy_id": (
            "'policy_' || COALESCE(so.symbol, 'NA') || '_' || COALESCE(so.side, 'NA') || '_' || "
            "COALESCE(so.market_regime, 'NA') || '_' || COALESCE(NULLIF(so.score_bucket, ''), "
            f"{score_bucket_expr()})"
        ),
    }
    group_expr = allowed.get(group_key)
    if not group_expr:
        return []
    sql = f"""
        SELECT
            {group_expr} AS group_value,
            COALESCE(so.symbol, 'NA') AS symbol,
            COALESCE(so.side, 'NA') AS side,
            COALESCE(so.market_regime, 'NA') AS market_regime,
            COALESCE(NULLIF(so.score_bucket, ''), {score_bucket_expr()}) AS score_bucket,
            COALESCE(so.strategy_type, 'NA') AS strategy,
            COALESCE(spm.source, CASE WHEN COALESCE(so.shadow_strategy, 0) = 1 THEN 'shadow_signal' ELSE 'trade_signal' END) AS source,
            COUNT(*) AS samples,
            SUM(CASE WHEN sl.first_barrier_hit = 'TIME' THEN 1 ELSE 0 END) AS time_count,
            SUM(CASE WHEN sl.first_barrier_hit = 'SL' THEN 1 ELSE 0 END) AS sl_count,
            SUM(CASE WHEN sl.first_barrier_hit = 'TP1' THEN 1 ELSE 0 END) AS tp1_count,
            SUM(CASE WHEN sl.first_barrier_hit = 'TP2' THEN 1 ELSE 0 END) AS tp2_count,
            AVG(COALESCE(sl.realized_return_pct, 0)) AS gross_expectancy,
            AVG(COALESCE(sl.bars_to_outcome, 0)) AS avg_bars_to_outcome,
            AVG(COALESCE(so.funding_rate, 0)) AS avg_funding_rate,
            AVG(COALESCE(so.spread_pct, 0)) AS avg_spread_pct,
            MIN(COALESCE(sl.realized_return_pct, 0)) AS worst_return,
            SUM(CASE WHEN COALESCE(sl.realized_return_pct, 0) > 0 THEN COALESCE(sl.realized_return_pct, 0) ELSE 0 END) AS gross_gains,
            SUM(CASE WHEN COALESCE(sl.realized_return_pct, 0) < 0 THEN COALESCE(sl.realized_return_pct, 0) ELSE 0 END) AS gross_losses
        FROM signal_labels sl
        JOIN signal_observations so ON so.id = sl.observation_id
        LEFT JOIN signal_path_metrics spm ON spm.observation_id = so.id
        WHERE sl.timestamp >= ?
        GROUP BY {group_expr}
        HAVING COUNT(*) >= ?
        ORDER BY samples DESC
        LIMIT ?
    """
    if getattr(db, "_use_postgres", False):
        sql = sql.replace("?", "%s")
    try:
        with db._connect() as conn:
            rows = db._fetchall_dicts(conn.execute(sql, (since, int(min_samples), int(limit))))
    except Exception:
        return []
    return [enrich_metrics(row, group_key=group_key) for row in rows]


def fetch_recent_event_counts(db: Any, *, since: str) -> dict[str, int]:
    try:
        return db.get_event_type_counts_since(since)
    except Exception:
        return {}


def enrich_metrics(row: dict[str, Any], *, group_key: str = "") -> dict[str, Any]:
    out = dict(row)
    samples = safe_float(out.get("samples") if "samples" in out else out.get("total_labels"))
    tp = safe_float(out.get("tp1_count")) + safe_float(out.get("tp2_count")) + safe_float(out.get("tp_count"))
    sl = safe_float(out.get("sl_count"))
    time_count = safe_float(out.get("time_count"))
    gains = safe_float(out.get("gross_gains") if "gross_gains" in out else out.get("gains"))
    losses = abs(safe_float(out.get("gross_losses") if "gross_losses" in out else out.get("losses")))
    out["group_key"] = group_key or str(out.get("group_key") or "")
    out["samples"] = int(samples)
    out["tp_count"] = int(tp)
    out["sl_count"] = int(sl)
    out["time_count"] = int(time_count)
    out["tp_ratio"] = tp / max(samples, 1.0) if samples else 0.0
    out["sl_ratio"] = sl / max(samples, 1.0) if samples else 0.0
    out["time_ratio"] = time_count / max(samples, 1.0) if samples else 0.0
    out["gross_expectancy"] = safe_float(out.get("gross_expectancy") if "gross_expectancy" in out else out.get("expectancy"))
    out["gross_pf"] = gains / losses if losses > 0 else 999.0 if gains > 0 else 0.0
    out["max_drawdown_proxy"] = abs(safe_float(out.get("worst_return")))
    out["avg_bars_to_outcome"] = safe_float(out.get("avg_bars_to_outcome"))
    return out


def apply_net_costs(row: dict[str, Any], costs: NetCostConfig) -> dict[str, Any]:
    out = dict(row)
    samples = max(1, safe_int(out.get("samples")))
    avg_bars = max(0.0, safe_float(out.get("avg_bars_to_outcome")))
    breakdown = explain_cost_breakdown(
        source=str(out.get("source") or "trade_signal"),
        side=str(out.get("side") or ""),
        entry_type="taker",
        exit_type="taker",
        slippage_bps=costs.slippage_bps,
        holding_bars=avg_bars,
        funding_rate=out.get("avg_funding_rate") if safe_float(out.get("avg_funding_rate")) else None,
        outcome=str(out.get("first_barrier_hit") or ""),
    )
    fee_pct = breakdown.fee_component_bps / 100.0
    slippage_pct = breakdown.slippage_component_bps / 100.0
    funding_pct = breakdown.funding_component_bps / 100.0
    total_cost_pct = breakdown.total_cost_bps / 100.0
    gross_gains = safe_float(out.get("gross_gains") if "gross_gains" in out else out.get("gains"))
    gross_losses = abs(safe_float(out.get("gross_losses") if "gross_losses" in out else out.get("losses")))
    net_gains = max(0.0, gross_gains - total_cost_pct * samples)
    net_losses = gross_losses + total_cost_pct * samples
    out["estimated_fee_cost"] = fee_pct
    out["estimated_slippage_cost"] = slippage_pct
    out["estimated_funding_cost"] = funding_pct
    out["estimated_total_cost"] = total_cost_pct
    out["gross_PF"] = safe_float(out.get("gross_pf"))
    out["net_PF"] = net_gains / net_losses if net_losses > 0 else 999.0 if net_gains > 0 else 0.0
    out["gross_expectancy"] = safe_float(out.get("gross_expectancy"))
    out["net_expectancy"] = out["gross_expectancy"] - total_cost_pct
    out["gross_EV"] = out["gross_expectancy"]
    out["net_EV"] = out["net_expectancy"]
    out["net_edge_after_costs"] = out["net_EV"]
    out["fee_component_bps"] = breakdown.fee_component_bps
    out["slippage_component_bps"] = breakdown.slippage_component_bps
    out["funding_component_bps"] = breakdown.funding_component_bps
    out["total_cost_bps"] = breakdown.total_cost_bps
    out["funding_model_status"] = breakdown.funding_model_status
    out["cost_trace_id"] = breakdown.cost_trace_id
    out["cost_application_explanation"] = breakdown.cost_application_explanation
    out["double_counting_risk"] = breakdown.double_counting_risk
    out["actionability"] = breakdown.actionability
    out["confidence_class"] = confidence_class(samples)
    out["final_decision"] = net_decision(out, costs)
    return out


def confidence_class(samples: int) -> str:
    if samples >= 1000:
        return "HIGH"
    if samples >= 500:
        return "MEDIUM"
    return "LOW"


def net_decision(row: dict[str, Any], costs: NetCostConfig) -> str:
    samples = safe_int(row.get("samples"))
    net_pf = safe_float(row.get("net_PF"))
    net_ev = safe_float(row.get("net_EV"))
    tp_ratio = safe_float(row.get("tp_ratio"))
    time_ratio = safe_float(row.get("time_ratio"))
    sl_ratio = safe_float(row.get("sl_ratio"))
    if samples < costs.min_samples:
        return DECISION_WATCH if net_ev > 0 else DECISION_REJECT
    if net_ev <= 0 or net_pf < costs.min_net_pf:
        return DECISION_REJECT
    if tp_ratio < costs.min_tp_ratio:
        return DECISION_SHADOW
    if time_ratio > costs.max_time_ratio and tp_ratio < costs.min_tp_ratio * 1.5:
        return DECISION_SHADOW
    if sl_ratio > 0.20 and tp_ratio < 0.05:
        return DECISION_REJECT
    if net_pf >= costs.min_net_pf and net_ev > 0:
        return DECISION_PAPER
    return DECISION_WATCH


def decision_reason(row: dict[str, Any], costs: NetCostConfig) -> str:
    if safe_int(row.get("samples")) < costs.min_samples:
        return "sample_too_small"
    if safe_float(row.get("net_EV")) <= 0:
        return "net_ev_not_positive"
    if safe_float(row.get("net_PF")) < costs.min_net_pf:
        return "net_pf_below_min"
    if safe_float(row.get("tp_ratio")) < costs.min_tp_ratio:
        return "tp_ratio_too_low"
    if safe_float(row.get("time_ratio")) > costs.max_time_ratio:
        return "time_death_risk"
    return "net_edge_confirmed"


def format_pct(value: Any) -> str:
    return f"{safe_float(value) * 100:.1f}%"


def format_num(value: Any, digits: int = 2) -> str:
    return f"{safe_float(value):.{digits}f}"


def safe_top(rows: list[dict[str, Any]], n: int = 10) -> list[dict[str, Any]]:
    return rows[: max(0, int(n))]
