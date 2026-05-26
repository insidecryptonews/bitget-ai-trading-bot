from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .dynamic_hold_lab import run_dynamic_hold_lab
from .exit_labs import ExitPolicy, run_exit_lab
from .phase8_research_utils import FINAL_RECOMMENDATION, NO_LOOKAHEAD_STATUS, STOP_TP_SAME_BAR_RULE, parse_symbols


@dataclass
class ExitPolicyV2SymbolBaseline:
    symbol: str
    source_lab: str
    baseline_trades: int
    baseline_net_ev: float
    baseline_net_pf: float
    baseline_tp_pct: float
    baseline_sl_pct: float
    baseline_time_pct: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExitPolicyV2SymbolPolicy:
    symbol: str
    policy_name: str
    source_lab: str
    policy_trades: int
    policy_net_ev: float
    policy_net_pf: float
    delta_ev_vs_symbol_baseline: float
    decision_symbol: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExitPolicyV2Candidate:
    policy_name: str
    source_lab: str
    aggregate_baseline_trades: int
    aggregate_baseline_net_ev: float
    aggregate_baseline_net_pf: float
    aggregate_policy_trades: int
    aggregate_policy_net_ev: float
    aggregate_policy_net_pf: float
    aggregate_delta_ev: float
    aggregate_decision: str
    symbol_decisions: dict[str, str] = field(default_factory=dict)

    @property
    def trades(self) -> int:
        return self.aggregate_policy_trades

    @property
    def net_ev(self) -> float:
        return self.aggregate_policy_net_ev

    @property
    def net_pf(self) -> float:
        return self.aggregate_policy_net_pf

    @property
    def delta_ev_vs_baseline(self) -> float:
        return self.aggregate_delta_ev

    @property
    def decision(self) -> str:
        return self.aggregate_decision

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update({
            "trades": self.trades,
            "net_ev": self.net_ev,
            "net_pf": self.net_pf,
            "delta_ev_vs_baseline": self.delta_ev_vs_baseline,
            "decision": self.decision,
        })
        return data


@dataclass
class ExitPolicyV2Report:
    hours: int
    timeframe: str
    symbols: list[str]
    baseline_net_ev: float
    best_policy: str
    best_policy_decision: str
    per_symbol_baseline: list[ExitPolicyV2SymbolBaseline] = field(default_factory=list)
    per_symbol_best_policy: list[ExitPolicyV2SymbolPolicy] = field(default_factory=list)
    candidates: list[ExitPolicyV2Candidate] = field(default_factory=list)
    aggregate_baseline_trades: int = 0
    aggregate_baseline_net_ev: float = 0.0
    aggregate_baseline_net_pf: float = 0.0
    aggregate_policy_trades: int = 0
    aggregate_policy_net_ev: float = 0.0
    aggregate_policy_net_pf: float = 0.0
    aggregate_delta_ev: float = 0.0
    aggregate_decision: str = "NEED_MORE_DATA"
    calculation_note: str = "aggregate_net_ev is weighted by trades; aggregate_net_pf is trade-count weighted summary for reporting only"
    warnings: list[str] = field(default_factory=lambda: [
        "aggregate_includes_multiple_symbols_when_requested",
        "per_symbol_and_aggregate_baselines_are_separated",
        "research_only_true",
        "not_paper_ready_without_cost_stress_and_walk_forward",
    ])
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
            "per_symbol_baseline": [item.as_dict() for item in self.per_symbol_baseline],
            "per_symbol_best_policy": [item.as_dict() for item in self.per_symbol_best_policy],
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "aggregate_baseline_trades": self.aggregate_baseline_trades,
            "aggregate_baseline_net_ev": self.aggregate_baseline_net_ev,
            "aggregate_baseline_net_pf": self.aggregate_baseline_net_pf,
            "aggregate_policy_trades": self.aggregate_policy_trades,
            "aggregate_policy_net_ev": self.aggregate_policy_net_ev,
            "aggregate_policy_net_pf": self.aggregate_policy_net_pf,
            "aggregate_delta_ev": self.aggregate_delta_ev,
            "aggregate_decision": self.aggregate_decision,
            "calculation_note": self.calculation_note,
            "warnings": self.warnings,
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
    symbol_list = parse_symbols(symbols, config)
    per_symbol_baselines: list[ExitPolicyV2SymbolBaseline] = []
    per_symbol_policies: list[ExitPolicyV2SymbolPolicy] = []

    for symbol in symbol_list:
        dynamic = run_dynamic_hold_lab(config, db, hours=hours, timeframe=timeframe, symbols=[symbol])
        dynamic_baseline = next((policy for policy in dynamic.policies if policy.policy_name == "baseline_current_exit"), None)
        if dynamic_baseline:
            per_symbol_baselines.append(ExitPolicyV2SymbolBaseline(
                symbol=symbol,
                source_lab="dynamic_hold_lab",
                baseline_trades=dynamic_baseline.trades,
                baseline_net_ev=dynamic_baseline.net_ev,
                baseline_net_pf=dynamic_baseline.net_pf,
                baseline_tp_pct=dynamic_baseline.tp_pct,
                baseline_sl_pct=dynamic_baseline.sl_pct,
                baseline_time_pct=dynamic_baseline.time_pct,
            ))
            for policy in dynamic.policies:
                if policy.policy_name == "baseline_current_exit":
                    continue
                per_symbol_policies.append(ExitPolicyV2SymbolPolicy(
                    symbol=symbol,
                    policy_name=policy.policy_name,
                    source_lab="dynamic_hold_lab",
                    policy_trades=policy.trades,
                    policy_net_ev=policy.net_ev,
                    policy_net_pf=policy.net_pf,
                    delta_ev_vs_symbol_baseline=policy.net_ev - dynamic_baseline.net_ev,
                    decision_symbol=_normalise_decision(policy.decision),
                ))

        exit_report = run_exit_lab(
            config,
            db,
            lab_name="exit_policy_v2",
            policies=exit_policy_v2_policies(),
            symbol=symbol,
            hours=hours,
            timeframe=timeframe,
        )
        baseline_comparison = next((item for item in exit_report.comparisons if item.policy_name == "baseline"), None)
        if baseline_comparison:
            per_symbol_baselines.append(ExitPolicyV2SymbolBaseline(
                symbol=symbol,
                source_lab="exit_policy_v2",
                baseline_trades=baseline_comparison.trades,
                baseline_net_ev=baseline_comparison.net_ev,
                baseline_net_pf=baseline_comparison.net_pf,
                baseline_tp_pct=baseline_comparison.tp_pct,
                baseline_sl_pct=baseline_comparison.sl_pct,
                baseline_time_pct=baseline_comparison.time_pct,
            ))
        for comparison in exit_report.comparisons:
            if comparison.policy_name == "baseline":
                continue
            per_symbol_policies.append(ExitPolicyV2SymbolPolicy(
                symbol=symbol,
                policy_name=comparison.policy_name,
                source_lab="exit_policy_v2",
                policy_trades=comparison.trades,
                policy_net_ev=comparison.net_ev,
                policy_net_pf=comparison.net_pf,
                delta_ev_vs_symbol_baseline=comparison.delta_ev_vs_baseline,
                decision_symbol=_normalise_decision(comparison.decision),
            ))

    candidates = _aggregate_candidates(per_symbol_baselines, per_symbol_policies)
    best = max(candidates, key=lambda item: item.aggregate_delta_ev, default=None)
    symbol_best = _best_per_symbol(per_symbol_policies)
    return ExitPolicyV2Report(
        hours=int(hours),
        timeframe=str(timeframe or "5m"),
        symbols=symbol_list,
        baseline_net_ev=best.aggregate_baseline_net_ev if best else _weighted_baseline(per_symbol_baselines, "dynamic_hold_lab"),
        best_policy=best.policy_name if best else "none",
        best_policy_decision=best.aggregate_decision if best else "NEED_MORE_DATA",
        per_symbol_baseline=per_symbol_baselines,
        per_symbol_best_policy=symbol_best,
        candidates=sorted(candidates, key=lambda item: item.aggregate_delta_ev, reverse=True)[:25],
        aggregate_baseline_trades=best.aggregate_baseline_trades if best else 0,
        aggregate_baseline_net_ev=best.aggregate_baseline_net_ev if best else 0.0,
        aggregate_baseline_net_pf=best.aggregate_baseline_net_pf if best else 0.0,
        aggregate_policy_trades=best.aggregate_policy_trades if best else 0,
        aggregate_policy_net_ev=best.aggregate_policy_net_ev if best else 0.0,
        aggregate_policy_net_pf=best.aggregate_policy_net_pf if best else 0.0,
        aggregate_delta_ev=best.aggregate_delta_ev if best else 0.0,
        aggregate_decision=best.aggregate_decision if best else "NEED_MORE_DATA",
    )


def _aggregate_candidates(
    baselines: list[ExitPolicyV2SymbolBaseline],
    policies: list[ExitPolicyV2SymbolPolicy],
) -> list[ExitPolicyV2Candidate]:
    by_key: dict[tuple[str, str], list[ExitPolicyV2SymbolPolicy]] = {}
    for policy in policies:
        by_key.setdefault((policy.policy_name, policy.source_lab), []).append(policy)
    candidates: list[ExitPolicyV2Candidate] = []
    for (policy_name, source_lab), rows in by_key.items():
        source_baselines = [baseline for baseline in baselines if baseline.source_lab == source_lab]
        baseline_trades = sum(item.baseline_trades for item in source_baselines)
        policy_trades = sum(item.policy_trades for item in rows)
        baseline_ev = _weighted_avg(
            [(item.baseline_net_ev, item.baseline_trades) for item in source_baselines]
        )
        policy_ev = _weighted_avg([(item.policy_net_ev, item.policy_trades) for item in rows])
        baseline_pf = _weighted_avg([(item.baseline_net_pf, item.baseline_trades) for item in source_baselines])
        policy_pf = _weighted_avg([(item.policy_net_pf, item.policy_trades) for item in rows])
        delta = policy_ev - baseline_ev
        candidates.append(ExitPolicyV2Candidate(
            policy_name=policy_name,
            source_lab=source_lab,
            aggregate_baseline_trades=baseline_trades,
            aggregate_baseline_net_ev=baseline_ev,
            aggregate_baseline_net_pf=baseline_pf,
            aggregate_policy_trades=policy_trades,
            aggregate_policy_net_ev=policy_ev,
            aggregate_policy_net_pf=policy_pf,
            aggregate_delta_ev=delta,
            aggregate_decision=_aggregate_decision(rows, policy_ev, delta),
            symbol_decisions={row.symbol: row.decision_symbol for row in rows},
        ))
    return candidates


def _best_per_symbol(policies: list[ExitPolicyV2SymbolPolicy]) -> list[ExitPolicyV2SymbolPolicy]:
    best: dict[str, ExitPolicyV2SymbolPolicy] = {}
    for row in policies:
        current = best.get(row.symbol)
        if current is None or row.delta_ev_vs_symbol_baseline > current.delta_ev_vs_symbol_baseline:
            best[row.symbol] = row
    return [best[symbol] for symbol in sorted(best)]


def _aggregate_decision(rows: list[ExitPolicyV2SymbolPolicy], net_ev: float, delta: float) -> str:
    if sum(row.policy_trades for row in rows) < 10:
        return "REJECT_TOO_FEW_TRADES"
    if net_ev > 0 and delta > 0 and all(row.policy_net_ev > 0 for row in rows):
        return "IMPROVES_BASELINE_RESEARCH_ONLY"
    if net_ev > 0 and delta > 0:
        return "WATCH_ONLY_MIXED_SYMBOLS"
    if delta > 0:
        return "WATCH_ONLY_REDUCES_LOSSES"
    return "REJECT_WORSE_THAN_BASELINE"


def _normalise_decision(decision: str) -> str:
    if decision in {"IMPROVES_BASELINE", "IMPROVES_BASELINE_RESEARCH_ONLY"}:
        return "IMPROVES_BASELINE_RESEARCH_ONLY"
    if decision in {"WORSENS_BASELINE", "REJECT_WORSE_THAN_BASELINE"}:
        return "REJECT_WORSE_THAN_BASELINE"
    if decision in {"NO_TRADES", "REJECT_TOO_FEW_TRADES"}:
        return "REJECT_TOO_FEW_TRADES"
    if decision == "BASELINE":
        return "BASELINE"
    return str(decision or "WATCH_ONLY")


def _weighted_baseline(baselines: list[ExitPolicyV2SymbolBaseline], source_lab: str) -> float:
    return _weighted_avg([
        (item.baseline_net_ev, item.baseline_trades)
        for item in baselines
        if item.source_lab == source_lab
    ])


def _weighted_avg(values: list[tuple[float, int]]) -> float:
    total_weight = sum(max(0, int(weight)) for _, weight in values)
    if total_weight <= 0:
        return 0.0
    return sum(float(value) * max(0, int(weight)) for value, weight in values) / total_weight


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_exit_policy_v2_text(report: ExitPolicyV2Report) -> str:
    lines = [
        "EXIT POLICY COMPARATOR V2 START",
        f"hours: {report.hours}",
        f"timeframe: {report.timeframe}",
        f"symbols: {','.join(report.symbols)}",
        "warnings:",
    ]
    for warning in report.warnings:
        lines.append(f"- {warning}")
    lines.extend([
        "PER-SYMBOL BASELINE",
        "symbol | lab | baseline_trades | baseline_net_ev | baseline_net_pf | TP% | SL% | TIME%",
    ])
    for item in report.per_symbol_baseline:
        lines.append(
            f"{item.symbol} | {item.source_lab} | {item.baseline_trades} | {item.baseline_net_ev:.6f} | "
            f"{item.baseline_net_pf:.4f} | {_pct(item.baseline_tp_pct)} | {_pct(item.baseline_sl_pct)} | {_pct(item.baseline_time_pct)}"
        )
    lines.extend([
        "PER-SYMBOL BEST POLICY",
        "symbol | policy | lab | trades | net_ev | net_pf | delta_vs_symbol_baseline | decision",
    ])
    for item in report.per_symbol_best_policy:
        lines.append(
            f"{item.symbol} | {item.policy_name} | {item.source_lab} | {item.policy_trades} | "
            f"{item.policy_net_ev:.6f} | {item.policy_net_pf:.4f} | {item.delta_ev_vs_symbol_baseline:.6f} | {item.decision_symbol}"
        )
    lines.extend([
        "AGGREGATE BEST POLICY",
        f"aggregate_baseline_trades: {report.aggregate_baseline_trades}",
        f"aggregate_baseline_net_ev: {report.aggregate_baseline_net_ev:.6f}",
        f"aggregate_baseline_net_pf: {report.aggregate_baseline_net_pf:.4f}",
        f"aggregate_policy_trades: {report.aggregate_policy_trades}",
        f"aggregate_policy_net_ev: {report.aggregate_policy_net_ev:.6f}",
        f"aggregate_policy_net_pf: {report.aggregate_policy_net_pf:.4f}",
        f"aggregate_delta_ev: {report.aggregate_delta_ev:.6f}",
        f"aggregate_decision: {report.aggregate_decision}",
        f"calculation_note: {report.calculation_note}",
        "CANDIDATES",
        "policy | lab | base_trades | base_ev | policy_trades | policy_ev | policy_pf | delta | decision",
    ])
    for candidate in report.candidates:
        lines.append(
            f"{candidate.policy_name} | {candidate.source_lab} | {candidate.aggregate_baseline_trades} | "
            f"{candidate.aggregate_baseline_net_ev:.6f} | {candidate.aggregate_policy_trades} | "
            f"{candidate.aggregate_policy_net_ev:.6f} | {candidate.aggregate_policy_net_pf:.4f} | "
            f"{candidate.aggregate_delta_ev:.6f} | {candidate.aggregate_decision}"
        )
    lines.extend([
        f"baseline_net_ev_compat: {report.baseline_net_ev:.6f}",
        f"best_policy: {report.best_policy}",
        f"best_policy_decision: {report.best_policy_decision}",
        f"sensitivity_warning: {report.sensitivity_warning}",
        "research_only: true",
        "activation: disabled",
        "final_recommendation: NO LIVE",
        "EXIT POLICY COMPARATOR V2 END",
    ])
    return "\n".join(lines)


def exit_policy_v2_text(config: Any, db: Any, *, hours: int = 72, timeframe: str = "5m", symbols: str | list[str] | None = None) -> str:
    return render_exit_policy_v2_text(run_exit_policy_v2(config, db, hours=hours, timeframe=timeframe, symbols=symbols))
