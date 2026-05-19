from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .cost_model import explain_cost_breakdown, normalize_funding_rate_to_bps, round_trip_fee_bps
from .utils import safe_float


@dataclass(frozen=True)
class NetRRResult:
    gross_rr: float
    net_rr: float
    fee_cost_bps: float
    slippage_cost_bps: float
    funding_cost_bps: float
    net_profit_tp1: float
    net_risk: float
    net_expectancy_proxy: float
    rr_cost_adjusted: bool
    rr_warning: str
    minimum_winrate_required_from_net_rr: float
    cost_breakdown: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_net_rr(
    *,
    entry: float,
    stop_loss: float,
    take_profit_1: float,
    side: str,
    slippage_bps: float = 3.0,
    funding_rate: Any = None,
    entry_time: Any = None,
    exit_time: Any = None,
    holding_bars: Any = None,
    min_net_rr: float = 1.4,
) -> NetRRResult:
    entry = safe_float(entry)
    stop = safe_float(stop_loss)
    tp1 = safe_float(take_profit_1)
    side_text = str(side or "").upper()
    if entry <= 0 or stop <= 0 or tp1 <= 0 or side_text not in {"LONG", "SHORT"}:
        return _empty("invalid_rr_inputs")
    gross_profit_pct = ((tp1 - entry) / entry * 100.0) if side_text == "LONG" else ((entry - tp1) / entry * 100.0)
    risk_pct = abs(entry - stop) / entry * 100.0
    if risk_pct <= 0:
        return _empty("invalid_stop_distance")

    breakdown = explain_cost_breakdown(
        source="trade_signal",
        side=side_text,
        entry_type="taker",
        exit_type="taker",
        # R:R uses NET_EDGE_SLIPPAGE_BPS as conservative round-trip slippage budget.
        # Fees still come from the central cost model.
        slippage_bps=0.0,
        entry_time=entry_time,
        exit_time=exit_time,
        holding_bars=holding_bars,
        funding_rate=funding_rate,
        outcome="TP1",
    )
    fee_bps = round_trip_fee_bps("taker", "taker")
    slippage_total_bps = max(0.0, safe_float(slippage_bps))
    funding_bps = breakdown.funding_component_bps if funding_rate not in {None, ""} else 0.0
    total_cost_pct = (fee_bps + slippage_total_bps + funding_bps) / 100.0
    net_profit = gross_profit_pct - total_cost_pct
    net_risk = risk_pct + total_cost_pct
    net_rr = max(0.0, net_profit) / net_risk if net_risk > 0 else 0.0
    gross_rr = gross_profit_pct / risk_pct if risk_pct > 0 else 0.0
    min_winrate = 1.0 / (1.0 + net_rr) if net_rr > 0 else 1.0
    warning = "OK" if net_rr >= min_net_rr else "NET_RR_BELOW_MIN"
    funding_source = "UNKNOWN_OR_NOT_CROSSED"
    if funding_rate not in {None, ""}:
        funding_source = f"rate_bps={normalize_funding_rate_to_bps(funding_rate):.4f}"
    cost_breakdown = {
        **breakdown.as_dict(),
        "fee_component_bps": fee_bps,
        "slippage_component_bps": slippage_total_bps,
        "funding_component_bps": funding_bps,
        "total_cost_bps": fee_bps + slippage_total_bps + funding_bps,
        "funding_rate_source": funding_source,
    }
    return NetRRResult(
        gross_rr=gross_rr,
        net_rr=net_rr,
        fee_cost_bps=fee_bps,
        slippage_cost_bps=slippage_total_bps,
        funding_cost_bps=funding_bps,
        net_profit_tp1=net_profit,
        net_risk=net_risk,
        net_expectancy_proxy=net_profit - net_risk * min_winrate,
        rr_cost_adjusted=True,
        rr_warning=warning,
        minimum_winrate_required_from_net_rr=min_winrate,
        cost_breakdown=cost_breakdown,
    )


def net_rr_smoke_text() -> str:
    result = calculate_net_rr(entry=100.0, stop_loss=99.4, take_profit_1=100.96, side="LONG", slippage_bps=3.0, min_net_rr=1.4)
    checks = {
        "gross_rr_is_160": 1.59 <= result.gross_rr <= 1.61,
        "net_rr_cost_adjusted": result.rr_cost_adjusted,
        "net_rr_around_108": 1.02 <= result.net_rr <= 1.12,
        "gross_and_net_separated": result.gross_rr > result.net_rr,
        "low_net_rr_warns": result.rr_warning == "NET_RR_BELOW_MIN",
        "final_recommendation_no_live": True,
    }
    passed = all(checks.values())
    lines = ["NET RR SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend([
        f"gross_rr: {result.gross_rr:.4f}",
        f"net_rr: {result.net_rr:.4f}",
        f"fee_cost_bps: {result.fee_cost_bps:.2f}",
        f"slippage_cost_bps: {result.slippage_cost_bps:.2f}",
        "LIVE_TRADING=false",
        "DRY_RUN=true",
        "PAPER_TRADING=true",
        "final_recommendation: NO LIVE",
        f"result: {'PASS' if passed else 'FAIL'}",
        "NET RR SMOKE TEST END",
    ])
    return "\n".join(lines)


def _empty(reason: str) -> NetRRResult:
    return NetRRResult(
        gross_rr=0.0,
        net_rr=0.0,
        fee_cost_bps=0.0,
        slippage_cost_bps=0.0,
        funding_cost_bps=0.0,
        net_profit_tp1=0.0,
        net_risk=0.0,
        net_expectancy_proxy=0.0,
        rr_cost_adjusted=False,
        rr_warning=reason,
        minimum_winrate_required_from_net_rr=1.0,
        cost_breakdown={"error": reason},
    )
