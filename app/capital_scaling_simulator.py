"""ResearchOps V7 — Capital Scaling Simulator.

Pure math, research-only. Shows that bigger capital amplifies BOTH gains and
losses; it does NOT magically fix a negative EV.

Hard contract:
  - never calls set_leverage / set_margin_mode
  - never modifies config (capital, sizing, slots)
  - never opens orders
  - DO_NOT_SCALE returned when EV ≤ 0, data quality BAD, or OHLCV stale
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any


FINAL_RECOMMENDATION = "NO LIVE"

DEFAULT_CAPITAL_LADDER: tuple[float, ...] = (20.0, 50.0, 100.0, 250.0, 500.0)
DEFAULT_RISK_PCT: tuple[float, ...] = (0.0025, 0.005, 0.01)
DEFAULT_LEVERAGES: tuple[int, ...] = (1, 3, 5, 10)
DEFAULT_REINVEST_FRACTIONS: tuple[float, ...] = (0.0, 0.5, 1.0)


@dataclass
class CapitalScalingScenario:
    capital_usdt: float
    risk_pct: float
    leverage: int
    reinvestment_fraction: float
    base_clean_net_ev_pct: float
    base_clean_pf: float
    trades_per_window: int
    expected_net_pnl_usdt: float
    max_drawdown_estimate_usdt: float
    risk_of_ruin_proxy: float
    scale_up_eligible: bool
    next_capital_level: float
    do_not_scale_reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CapitalScalingReport:
    capital_ladder: list[float]
    risk_pct_ladder: list[float]
    leverages: list[int]
    reinvestment_fractions: list[float]
    base_clean_net_ev_pct: float
    base_clean_pf: float
    data_quality_status: str
    ohlcv_actionable: bool
    scenarios: list[CapitalScalingScenario] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION
    warning: str = "more_capital_amplifies_pnl_does_not_fix_negative_EV"

    def as_dict(self) -> dict[str, Any]:
        return {
            "capital_ladder": list(self.capital_ladder),
            "risk_pct_ladder": list(self.risk_pct_ladder),
            "leverages": list(self.leverages),
            "reinvestment_fractions": list(self.reinvestment_fractions),
            "base_clean_net_ev_pct": self.base_clean_net_ev_pct,
            "base_clean_pf": self.base_clean_pf,
            "data_quality_status": self.data_quality_status,
            "ohlcv_actionable": self.ohlcv_actionable,
            "scenarios": [s.as_dict() for s in self.scenarios],
            "research_only": self.research_only,
            "paper_filter_enabled": self.paper_filter_enabled,
            "can_send_real_orders": self.can_send_real_orders,
            "final_recommendation": self.final_recommendation,
            "warning": self.warning,
        }


def _risk_of_ruin_proxy(net_ev_pct: float, pf: float, trades: int) -> float:
    """Conservative risk-of-ruin proxy based on Kelly-fraction heuristics.

    Caps at 0.99 — if the underlying EV is non-positive we report 0.99 because
    in expectation the strategy ruins."""
    if net_ev_pct <= 0:
        return 0.99
    if pf <= 1.0:
        return 0.95
    # Heuristic: ruin probability decays with sqrt(trades) and grows with risk pct.
    base = 1.0 / max(pf, 1.0)
    return max(0.001, min(0.95, base / max(1.0, math.sqrt(max(1, trades)))))


def _scenario(
    *,
    capital: float,
    risk_pct: float,
    leverage: int,
    reinvest: float,
    base_net_ev_pct: float,
    base_pf: float,
    trades: int,
    data_quality_bad: bool,
    ohlcv_stale: bool,
) -> CapitalScalingScenario:
    reason = ""
    if data_quality_bad:
        reason = "data_quality_bad_do_not_scale"
    elif ohlcv_stale:
        reason = "ohlcv_stale_do_not_scale"
    elif base_net_ev_pct <= 0:
        reason = "base_clean_net_ev_not_positive_do_not_scale"
    elif base_pf < 1.0:
        reason = "base_clean_pf_below_1_do_not_scale"
    eligible = not reason
    # Expected PnL = capital * risk_pct * leverage * net_ev_pct/100 * trades
    expected_pnl = capital * risk_pct * max(1, leverage) * (base_net_ev_pct / 100.0) * max(1, trades)
    # With reinvestment, compound for `trades` steps using the per-trade EV.
    if reinvest > 0 and base_net_ev_pct != 0:
        per_trade_pct = (base_net_ev_pct / 100.0) * risk_pct * max(1, leverage)
        compound = (1.0 + per_trade_pct * reinvest) ** max(1, trades)
        expected_pnl = capital * (compound - 1.0)
    # Drawdown estimate: SL distance per trade × consecutive worst case 3.
    drawdown = capital * risk_pct * max(1, leverage) * 3.0
    ruin = _risk_of_ruin_proxy(base_net_ev_pct, base_pf, trades)
    next_level = capital * 2.5 if eligible else 0.0
    return CapitalScalingScenario(
        capital_usdt=float(capital),
        risk_pct=float(risk_pct),
        leverage=int(leverage),
        reinvestment_fraction=float(reinvest),
        base_clean_net_ev_pct=base_net_ev_pct,
        base_clean_pf=base_pf,
        trades_per_window=int(trades),
        expected_net_pnl_usdt=expected_pnl,
        max_drawdown_estimate_usdt=drawdown,
        risk_of_ruin_proxy=ruin,
        scale_up_eligible=eligible,
        next_capital_level=next_level,
        do_not_scale_reason=reason or "scenario_eligible_label_only_no_activation",
    )


def run_capital_scaling_simulator(
    *,
    base_clean_net_ev_pct: float,
    base_clean_pf: float,
    trades_per_window: int = 100,
    capital_ladder: tuple[float, ...] = DEFAULT_CAPITAL_LADDER,
    risk_pct: tuple[float, ...] = DEFAULT_RISK_PCT,
    leverages: tuple[int, ...] = DEFAULT_LEVERAGES,
    reinvest_fractions: tuple[float, ...] = DEFAULT_REINVEST_FRACTIONS,
    data_quality_status: str = "UNKNOWN",
    ohlcv_actionable: bool = False,
) -> CapitalScalingReport:
    data_quality_bad = str(data_quality_status).upper() == "BAD"
    ohlcv_stale = not bool(ohlcv_actionable)
    scenarios: list[CapitalScalingScenario] = []
    for capital in capital_ladder:
        for r in risk_pct:
            for lev in leverages:
                for reinvest in reinvest_fractions:
                    scenarios.append(_scenario(
                        capital=capital, risk_pct=r, leverage=lev, reinvest=reinvest,
                        base_net_ev_pct=base_clean_net_ev_pct,
                        base_pf=base_clean_pf,
                        trades=trades_per_window,
                        data_quality_bad=data_quality_bad,
                        ohlcv_stale=ohlcv_stale,
                    ))
    return CapitalScalingReport(
        capital_ladder=list(capital_ladder),
        risk_pct_ladder=list(risk_pct),
        leverages=list(leverages),
        reinvestment_fractions=list(reinvest_fractions),
        base_clean_net_ev_pct=base_clean_net_ev_pct,
        base_clean_pf=base_clean_pf,
        data_quality_status=data_quality_status,
        ohlcv_actionable=ohlcv_actionable,
        scenarios=scenarios,
    )


def render_capital_scaling_text(report: CapitalScalingReport) -> str:
    lines = [
        "CAPITAL SCALING SIMULATOR V7 START",
        f"warning: {report.warning}",
        f"base_clean_net_ev_pct: {report.base_clean_net_ev_pct:.6f}",
        f"base_clean_pf: {report.base_clean_pf:.4f}",
        f"data_quality_status: {report.data_quality_status}",
        f"ohlcv_actionable: {str(report.ohlcv_actionable).lower()}",
        f"capital_ladder: {report.capital_ladder}",
        f"risk_pct_ladder: {report.risk_pct_ladder}",
        f"leverages: {report.leverages}",
        f"reinvestment_fractions: {report.reinvestment_fractions}",
        "capital | risk% | leverage | reinvest | expected_pnl | dd | ruin | eligible | reason",
    ]
    for s in report.scenarios[:48]:
        lines.append(
            f"{s.capital_usdt:.2f} | {s.risk_pct:.4f} | {s.leverage} | "
            f"{s.reinvestment_fraction:.2f} | {s.expected_net_pnl_usdt:.4f} | "
            f"{s.max_drawdown_estimate_usdt:.4f} | {s.risk_of_ruin_proxy:.4f} | "
            f"{str(s.scale_up_eligible).lower()} | {s.do_not_scale_reason}"
        )
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "no_set_leverage_call: true",
        "no_set_margin_mode_call: true",
        "no_config_changes: true",
        "final_recommendation: NO LIVE",
        "CAPITAL SCALING SIMULATOR V7 END",
    ])
    return "\n".join(lines)
