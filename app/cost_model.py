from __future__ import annotations

import hashlib
from dataclasses import dataclass, asdict
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable

from .utils import safe_float


DEFAULT_FUNDING_TIMESTAMPS_UTC = (time(0, 0), time(8, 0), time(16, 0))
TIME_EXIT_ASSUMPTION = "close_at_horizon"
MARKET_PROBE_ACTIONABILITY = "NOT_ACTIONABLE_MARKET_PROBE"


@dataclass(frozen=True)
class FeeModel:
    product_type: str
    vip_tier: str
    maker_fee_bps: float
    taker_fee_bps: float


@dataclass(frozen=True)
class CostBreakdown:
    fee_component_bps: float
    slippage_component_bps: float
    funding_component_bps: float
    total_cost_bps: float
    fee_scenario: str
    funding_model_status: str
    funding_rate_source: str
    time_exit_assumption: str
    actionability: str
    cost_application_explanation: str
    double_counting_risk: str
    cost_trace_id: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def get_bitget_usdt_m_vip0_fee_model() -> FeeModel:
    return FeeModel(
        product_type="USDT-M Futures perpetual",
        vip_tier="VIP0",
        maker_fee_bps=2.0,
        taker_fee_bps=6.0,
    )


def round_trip_fee_bps(entry_type: str = "taker", exit_type: str = "taker") -> float:
    model = get_bitget_usdt_m_vip0_fee_model()
    return _side_fee_bps(entry_type, model) + _side_fee_bps(exit_type, model)


def estimate_fee_cost_bps(entry_type: str = "taker", exit_type: str = "taker") -> float:
    return round_trip_fee_bps(entry_type, exit_type)


def should_apply_funding(
    entry_time: Any,
    exit_time: Any,
    funding_timestamps: Iterable[time] = DEFAULT_FUNDING_TIMESTAMPS_UTC,
) -> bool:
    entry = _parse_datetime(entry_time)
    exit_dt = _parse_datetime(exit_time)
    if entry is None or exit_dt is None or exit_dt <= entry:
        return False
    day = entry.date()
    end_day = exit_dt.date()
    while day <= end_day:
        for funding_time in funding_timestamps:
            candidate = datetime.combine(day, funding_time, tzinfo=timezone.utc)
            if entry < candidate <= exit_dt:
                return True
        day = day + timedelta(days=1)
    return False


def estimate_funding_bps(side: str, funding_rate: Any, crosses_funding_timestamp: bool) -> float:
    if not crosses_funding_timestamp:
        return 0.0
    rate_bps = normalize_funding_rate_to_bps(funding_rate)
    if rate_bps == 0.0:
        return 0.0
    # Positive funding means longs pay shorts. Negative cost is income.
    return rate_bps if str(side or "").upper() == "LONG" else -rate_bps


def estimate_slippage_bps(symbol: str = "", liquidity_profile: str = "", execution_type: str = "taker_taker", base_slippage_bps: float = 3.0) -> float:
    del symbol
    profile = str(liquidity_profile or "").lower()
    multiplier = 1.0
    if profile in {"high", "deep", "liquid"}:
        multiplier = 0.5
    elif profile in {"low", "thin", "illiquid"}:
        multiplier = 1.5
    execution = str(execution_type or "").lower()
    if execution in {"maker_maker", "passive"}:
        return max(0.0, safe_float(base_slippage_bps) * multiplier * 0.5)
    if execution in {"maker_taker", "mixed"}:
        return max(0.0, safe_float(base_slippage_bps) * multiplier * 1.0)
    return max(0.0, safe_float(base_slippage_bps) * multiplier * 2.0)


def compute_net_ev(gross_ev: float, fee_bps: float, slippage_bps: float, funding_bps: float) -> float:
    return safe_float(gross_ev) - _bps_to_pct(safe_float(fee_bps) + safe_float(slippage_bps) + safe_float(funding_bps))


def compute_net_pf(gross_pf: float, costs: CostBreakdown | dict[str, Any], sample_stats: dict[str, Any]) -> float:
    returns = [safe_float(value) for value in sample_stats.get("returns", [])]
    if returns:
        total_cost_pct = _bps_to_pct(safe_float(_cost_value(costs, "total_cost_bps")))
        net_returns = [value - total_cost_pct for value in returns]
        gains = sum(value for value in net_returns if value > 0)
        losses = abs(sum(value for value in net_returns if value < 0))
        return gains / losses if losses > 0 else 999.0 if gains > 0 else 0.0
    gross = safe_float(gross_pf)
    total_cost_bps = safe_float(_cost_value(costs, "total_cost_bps"))
    # Fallback for aggregate rows only; detailed reports should use returns.
    return max(0.0, gross - (total_cost_bps / 100.0))


def explain_cost_breakdown(
    *,
    source: str = "trade_signal",
    side: str = "",
    entry_type: str = "taker",
    exit_type: str = "taker",
    slippage_bps: float = 3.0,
    entry_time: Any = None,
    exit_time: Any = None,
    holding_bars: Any = None,
    bar_minutes: int = 5,
    funding_rate: Any = None,
    outcome: str = "",
    time_exit_assumption: str = TIME_EXIT_ASSUMPTION,
    liquidity_profile: str = "",
    already_includes_costs: bool = False,
) -> CostBreakdown:
    source_text = str(source or "trade_signal").lower()
    outcome_text = str(outcome or "").upper()
    actionability = "ACTIONABLE_RESEARCH_SIGNAL"
    if source_text == "market_probe":
        actionability = MARKET_PROBE_ACTIONABILITY
        explanation = "market_probe is research-only; fees/slippage/funding are not applied as actionable trade costs"
        return _breakdown(0.0, 0.0, 0.0, entry_type, exit_type, "NOT_APPLIED_RESEARCH_PROBE", "UNKNOWN_OR_DEFAULT", time_exit_assumption, actionability, explanation, "LOW")
    if already_includes_costs:
        explanation = "input row already included costs; cost model avoids reapplying fee/slippage/funding"
        return _breakdown(0.0, 0.0, 0.0, entry_type, exit_type, "NOT_APPLIED_ALREADY_INCLUDED", "UNKNOWN_OR_DEFAULT", time_exit_assumption, actionability, explanation, "LOW")

    fee_bps = estimate_fee_cost_bps(entry_type, exit_type)
    slip_bps = estimate_slippage_bps(liquidity_profile=liquidity_profile, execution_type=f"{entry_type}_{exit_type}", base_slippage_bps=slippage_bps)
    inferred_exit = _infer_exit_time(entry_time, holding_bars, bar_minutes)
    exit_dt = _parse_datetime(exit_time) or inferred_exit
    crosses = should_apply_funding(entry_time, exit_dt)
    funding_rate_known = funding_rate not in {None, ""}
    funding_bps = estimate_funding_bps(side, funding_rate, crosses) if funding_rate_known else 0.0
    if not _parse_datetime(entry_time) or not exit_dt:
        funding_status = "UNKNOWN_HOLDING_TIME"
    elif not funding_rate_known:
        funding_status = "NEEDS_LIVE_RATE_SOURCE" if crosses else "OK"
    else:
        funding_status = "OK"
    funding_source = "REAL_OR_ROW_VALUE" if funding_rate_known else "UNKNOWN_OR_DEFAULT"
    if outcome_text == "TIME" and time_exit_assumption == "no_trade":
        fee_bps = 0.0
        slip_bps = 0.0
        funding_bps = 0.0
        explanation = "TIME row uses no_trade assumption; no actionable exit cost applied"
    else:
        explanation = (
            f"fees={entry_type}/{exit_type}; slippage separated; funding applies only if timestamp crossed; "
            f"TIME_EXIT_ASSUMPTION={time_exit_assumption}"
        )
    return _breakdown(fee_bps, slip_bps, funding_bps, entry_type, exit_type, funding_status, funding_source, time_exit_assumption, actionability, explanation, "LOW")


def calculate_net_metrics_for_returns(returns: list[float], breakdowns: list[CostBreakdown | dict[str, Any]]) -> dict[str, float]:
    net_returns = []
    for index, value in enumerate(returns):
        breakdown = breakdowns[index] if index < len(breakdowns) else {}
        net_returns.append(safe_float(value) - _bps_to_pct(safe_float(_cost_value(breakdown, "total_cost_bps"))))
    gains = sum(value for value in net_returns if value > 0)
    losses = abs(sum(value for value in net_returns if value < 0))
    return {
        "net_EV": sum(net_returns) / max(len(net_returns), 1),
        "net_PF": gains / losses if losses > 0 else 999.0 if gains > 0 else 0.0,
        "net_gains": gains,
        "net_losses": losses,
    }


def normalize_funding_rate_to_bps(value: Any) -> float:
    raw = safe_float(value)
    if raw == 0.0:
        return 0.0
    if abs(raw) <= 0.001:
        return raw * 10000.0
    if abs(raw) <= 1.0:
        return raw * 100.0
    return raw


def _side_fee_bps(order_type: str, model: FeeModel) -> float:
    text = str(order_type or "taker").lower()
    return model.maker_fee_bps if text == "maker" else model.taker_fee_bps


def _bps_to_pct(bps: float) -> float:
    return safe_float(bps) / 100.0


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _infer_exit_time(entry_time: Any, holding_bars: Any, bar_minutes: int) -> datetime | None:
    entry = _parse_datetime(entry_time)
    bars = safe_float(holding_bars)
    if entry is None or bars <= 0:
        return None
    return entry + timedelta(minutes=bars * max(1, int(bar_minutes or 5)))


def _breakdown(
    fee_bps: float,
    slippage_bps: float,
    funding_bps: float,
    entry_type: str,
    exit_type: str,
    funding_status: str,
    funding_source: str,
    time_exit_assumption: str,
    actionability: str,
    explanation: str,
    double_counting_risk: str,
) -> CostBreakdown:
    total = safe_float(fee_bps) + safe_float(slippage_bps) + safe_float(funding_bps)
    trace_raw = f"{entry_type}:{exit_type}:{fee_bps:.4f}:{slippage_bps:.4f}:{funding_bps:.4f}:{actionability}:{time_exit_assumption}"
    trace = hashlib.sha1(trace_raw.encode("utf-8")).hexdigest()[:12]
    return CostBreakdown(
        fee_component_bps=safe_float(fee_bps),
        slippage_component_bps=safe_float(slippage_bps),
        funding_component_bps=safe_float(funding_bps),
        total_cost_bps=total,
        fee_scenario=f"{entry_type}/{exit_type}",
        funding_model_status=funding_status,
        funding_rate_source=funding_source,
        time_exit_assumption=time_exit_assumption,
        actionability=actionability,
        cost_application_explanation=explanation,
        double_counting_risk=double_counting_risk,
        cost_trace_id=trace,
    )


def _cost_value(costs: CostBreakdown | dict[str, Any], key: str) -> Any:
    if isinstance(costs, CostBreakdown):
        return getattr(costs, key)
    return costs.get(key)
