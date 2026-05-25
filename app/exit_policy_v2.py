from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .dynamic_hold_lab import run_dynamic_hold_lab
from .exit_labs import (
    ExitPolicy,
    run_exit_lab,
)
from .phase8_research_utils import FINAL_RECOMMENDATION, NO_LOOKAHEAD_STATUS, STOP_TP_SAME_BAR_RULE


@dataclass
class ExitPolicyV2Candidate:
    policy_name: str
    source_lab: str
    trades: int
    net_ev: float
    net_pf: float
    delta_ev_vs_baseline: float
    decision: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExitPolicyV2Report:
    hours: int
    timeframe: str
    symbols: list[str]
    baseline_net_ev: float
    best_policy: str
    best_policy_decision: str
    candidates: list[ExitPolicyV2Candidate] = field(default_factory=list)
    sensitivity_warning: str = "72h can suggest; 720h validates"
    final_recommendation: str = FINAL_RECOMMENDATION
    research_only: bool = True
    no_lookahead_status: str = NO_LOOKAHEAD_STATUS
    stop_tp_same_bar_rule: str = STOP_TP_SAME_BAR_RULE

    def as_dict(self) -> dict[str, Any]:
        return {
            "hours": self.hours,
            "timeframe": self.timeframe,
            "symbols": self.symbols,
            "baseline_net_ev": self.baseline_net_ev,
            "best_policy": self.best_policy,
            "best_policy_decision": self.best_policy_decision,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "sensitivity_warning": self.sensitivity_warning,
            "final_recommendation": self.final_recommendation,
            "research_only": self.research_only,
            "no_lookahead_status": self.no_lookahead_status,
            "stop_tp_same_bar_rule": self.stop_tp_same_bar_rule,
            "activation": "disabled",
        }


def exit_policy_v2_policies() -> tuple[ExitPolicy, ...]:
    return (
        ExitPolicy(name="baseline"),
        ExitPolicy(name="breakeven_after_0_5R", breakeven_after_mfe_pct=0.50),
        ExitPolicy(name="breakeven_after_0_8R", breakeven_after_mfe_pct=0.80),
        ExitPolicy(name="profit_lock_0_4R", profit_lock_threshold_pct=0.40),
        ExitPolicy(name="profit_lock_0_6R", profit_lock_threshold_pct=0.60),
        ExitPolicy(name="profit_lock_0_8R", profit_lock_threshold_pct=0.80),
        ExitPolicy(name="profit_lock_1_0R", profit_lock_threshold_pct=1.00),
        ExitPolicy(name="trailing_pct_0_30", trail_after_mfe_pct=0.50, trail_distance_pct=0.30),
        ExitPolicy(name="trailing_pct_0_50", trail_after_mfe_pct=0.80, trail_distance_pct=0.50),
        ExitPolicy(name="time_death_max_10", max_holding_bars_override=10),
        ExitPolicy(name="time_death_max_15", max_holding_bars_override=15),
        ExitPolicy(name="time_death_max_20", max_holding_bars_override=20),
        ExitPolicy(name="time_death_max_25", max_holding_bars_override=25),
    )


def run_exit_policy_v2(
    config: Any,
    db: Any,
    *,
    hours: int = 72,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
) -> ExitPolicyV2Report:
    symbol_list = _symbols(symbols)
    first_symbol = symbol_list[0] if symbol_list else "BTCUSDT"
    base_report = run_exit_lab(
        config,
        db,
        lab_name="exit_policy_v2",
        policies=exit_policy_v2_policies(),
        symbol=first_symbol,
        hours=hours,
        timeframe=timeframe,
    )
    dynamic = run_dynamic_hold_lab(config, db, hours=hours, timeframe=timeframe, symbols=symbol_list)
    candidates: list[ExitPolicyV2Candidate] = []
    for comparison in base_report.comparisons:
        candidates.append(ExitPolicyV2Candidate(
            policy_name=comparison.policy_name,
            source_lab="exit_policy_v2",
            trades=comparison.trades,
            net_ev=comparison.net_ev,
            net_pf=comparison.net_pf,
            delta_ev_vs_baseline=comparison.delta_ev_vs_baseline,
            decision=_normalise_decision(comparison.decision),
        ))
    for comparison in dynamic.policies:
        if comparison.policy_name == "baseline_current_exit":
            continue
        candidates.append(ExitPolicyV2Candidate(
            policy_name=comparison.policy_name,
            source_lab="dynamic_hold_lab",
            trades=comparison.trades,
            net_ev=comparison.net_ev,
            net_pf=comparison.net_pf,
            delta_ev_vs_baseline=comparison.net_ev - dynamic.baseline_net_ev,
            decision=_normalise_decision(comparison.decision),
        ))
    best = max(candidates, key=lambda item: item.delta_ev_vs_baseline, default=None)
    return ExitPolicyV2Report(
        hours=int(hours),
        timeframe=str(timeframe or "5m"),
        symbols=symbol_list,
        baseline_net_ev=base_report.baseline_net_ev,
        best_policy=best.policy_name if best else "none",
        best_policy_decision=best.decision if best else "NEED_MORE_DATA",
        candidates=sorted(candidates, key=lambda item: item.delta_ev_vs_baseline, reverse=True)[:20],
    )


def _normalise_decision(decision: str) -> str:
    if decision in {"IMPROVES_BASELINE", "IMPROVES_BASELINE_RESEARCH_ONLY"}:
        return "IMPROVES_BASELINE_RESEARCH_ONLY"
    if decision in {"WORSENS_BASELINE", "REJECT_WORSE_THAN_BASELINE"}:
        return "REJECT_WORSE_THAN_BASELINE"
    if decision in {"NO_TRADES", "REJECT_TOO_FEW_TRADES"}:
        return "REJECT_TOO_FEW_TRADES"
    return str(decision or "WATCH_ONLY")


def _symbols(symbols: str | list[str] | None) -> list[str]:
    if isinstance(symbols, str):
        return [part.strip().upper() for part in symbols.split(",") if part.strip()]
    if symbols:
        return [str(part).strip().upper() for part in symbols if str(part).strip()]
    return ["BTCUSDT", "ETHUSDT"]


def render_exit_policy_v2_text(report: ExitPolicyV2Report) -> str:
    lines = [
        "EXIT POLICY COMPARATOR V2 START",
        f"hours: {report.hours}",
        f"timeframe: {report.timeframe}",
        f"symbols: {','.join(report.symbols)}",
        f"baseline_net_ev: {report.baseline_net_ev:.6f}",
        f"best_policy: {report.best_policy}",
        f"best_policy_decision: {report.best_policy_decision}",
        "policy | lab | trades | net_ev | net_pf | delta_ev | decision",
    ]
    for candidate in report.candidates:
        lines.append(
            f"{candidate.policy_name} | {candidate.source_lab} | {candidate.trades} | "
            f"{candidate.net_ev:.6f} | {candidate.net_pf:.4f} | {candidate.delta_ev_vs_baseline:.6f} | {candidate.decision}"
        )
    lines.extend([
        f"sensitivity_warning: {report.sensitivity_warning}",
        "research_only: true",
        "activation: disabled",
        "final_recommendation: NO LIVE",
        "EXIT POLICY COMPARATOR V2 END",
    ])
    return "\n".join(lines)


def exit_policy_v2_text(config: Any, db: Any, *, hours: int = 72, timeframe: str = "5m", symbols: str | list[str] | None = None) -> str:
    return render_exit_policy_v2_text(run_exit_policy_v2(config, db, hours=hours, timeframe=timeframe, symbols=symbols))
