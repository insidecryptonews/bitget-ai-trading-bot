from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .dynamic_hold_lab import (
    DynamicHoldPolicy,
    _baseline_trade,
    _simulate_dynamic_trade,
    default_dynamic_hold_policies,
)
from .phase8_research_utils import (
    FINAL_RECOMMENDATION,
    NO_LOOKAHEAD_STATUS,
    STOP_TP_SAME_BAR_RULE,
    load_replay_trade_contexts,
    max_drawdown,
    net_pf,
    parse_symbols,
)
from .utils import safe_float


REJECT_NEGATIVE_EV = "REJECT_NEGATIVE_EV"
REJECT_COST_STRESS_FAIL = "REJECT_COST_STRESS_FAIL"
REJECT_WALK_FORWARD_FAIL = "REJECT_WALK_FORWARD_FAIL"
REJECT_OVERFIT_RISK = "REJECT_OVERFIT_RISK"
REJECT_TOO_FEW_TRADES = "REJECT_TOO_FEW_TRADES"
WATCH_ONLY = "WATCH_ONLY"
RESEARCH_PROMISING_NOT_ACTIONABLE = "RESEARCH_PROMISING_NOT_ACTIONABLE"
PAPER_DEMO_READY_MANUAL_REVIEW_ONLY = "PAPER_DEMO_READY_MANUAL_REVIEW_ONLY"

STATUS_PASS = "PASS"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"
STATUS_NEED_MORE_DATA = "NEED_MORE_DATA"

PHASE8_BASE_COST_PCT = 0.18
PHASE8_STRESS_022_PCT = 0.22
PHASE8_STRESS_025_PCT = 0.25
PHASE8_MAKER_MAKER_AUDIT_ONLY_PCT = 0.04


@dataclass(frozen=True)
class Phase8PolicySample:
    symbol: str
    policy_name: str
    timestamp: datetime
    gross_return_pct: float
    net_return_pct: float
    exit_reason: str = ""
    duration_bars: int = 0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data


@dataclass
class Phase8ScenarioMetrics:
    name: str
    cost_pct: float
    trades: int
    net_ev: float
    net_pf: float
    win_rate: float
    promotion_eligible: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Phase8CostStressResult:
    status: str
    scenarios: list[Phase8ScenarioMetrics] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cost_stress_status": self.status,
            "scenarios": [scenario.as_dict() for scenario in self.scenarios],
            "reasons": self.reasons,
        }


@dataclass
class Phase8WalkForwardFold:
    fold: int
    start: str
    end: str
    baseline_trades: int
    policy_trades: int
    baseline_net_ev: float
    policy_net_ev: float
    delta_ev: float
    pass_fold: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Phase8WalkForwardResult:
    status: str
    folds: list[Phase8WalkForwardFold] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "walk_forward_status": self.status,
            "folds": [fold.as_dict() for fold in self.folds],
            "reasons": self.reasons,
        }


@dataclass
class Phase8CandidateResult:
    candidate_id: str
    symbols: list[str]
    policy_name: str
    baseline_net_ev: float
    policy_net_ev: float
    delta_ev: float
    baseline_net_pf: float
    policy_net_pf: float
    trades: int
    sample_status: str
    cost_stress_status: str
    walk_forward_status: str
    anti_overfit_status: str
    sensitivity_status: str
    stability_status: str
    final_decision: str
    reasons: list[str] = field(default_factory=list)
    cost_stress: Phase8CostStressResult | None = None
    walk_forward: Phase8WalkForwardResult | None = None
    per_symbol_policy_net_ev: dict[str, float] = field(default_factory=dict)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "symbols": self.symbols,
            "policy_name": self.policy_name,
            "baseline_net_ev": self.baseline_net_ev,
            "policy_net_ev": self.policy_net_ev,
            "delta_ev": self.delta_ev,
            "baseline_net_pf": self.baseline_net_pf,
            "policy_net_pf": self.policy_net_pf,
            "trades": self.trades,
            "sample_status": self.sample_status,
            "cost_stress_status": self.cost_stress_status,
            "walk_forward_status": self.walk_forward_status,
            "anti_overfit_status": self.anti_overfit_status,
            "sensitivity_status": self.sensitivity_status,
            "stability_status": self.stability_status,
            "final_decision": self.final_decision,
            "reasons": self.reasons,
            "cost_stress": self.cost_stress.as_dict() if self.cost_stress else None,
            "walk_forward": self.walk_forward.as_dict() if self.walk_forward else None,
            "per_symbol_policy_net_ev": self.per_symbol_policy_net_ev,
            "research_only": self.research_only,
            "paper_filter_enabled": self.paper_filter_enabled,
            "can_send_real_orders": self.can_send_real_orders,
            "final_recommendation": self.final_recommendation,
        }


@dataclass
class Phase8ValidatorReport:
    hours: int
    timeframe: str
    symbols: list[str]
    candidates: list[Phase8CandidateResult] = field(default_factory=list)
    loader_statuses: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    best_candidate_id: str = "none"
    best_decision: str = STATUS_NEED_MORE_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    no_lookahead_status: str = NO_LOOKAHEAD_STATUS
    stop_tp_same_bar_rule: str = STOP_TP_SAME_BAR_RULE
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return {
            "hours": self.hours,
            "timeframe": self.timeframe,
            "symbols": self.symbols,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "loader_statuses": self.loader_statuses,
            "warnings": self.warnings,
            "best_candidate_id": self.best_candidate_id,
            "best_decision": self.best_decision,
            "research_only": self.research_only,
            "paper_filter_enabled": self.paper_filter_enabled,
            "can_send_real_orders": self.can_send_real_orders,
            "no_lookahead_status": self.no_lookahead_status,
            "stop_tp_same_bar_rule": self.stop_tp_same_bar_rule,
            "final_recommendation": self.final_recommendation,
        }


def evaluate_phase8_cost_stress(samples: list[Phase8PolicySample]) -> Phase8CostStressResult:
    if not samples:
        return Phase8CostStressResult(STATUS_NEED_MORE_DATA, reasons=["no_policy_samples"])
    scenarios = [
        _scenario("base_cost", samples, PHASE8_BASE_COST_PCT, promotion_eligible=True),
        _scenario("stress_0_22", samples, PHASE8_STRESS_022_PCT, promotion_eligible=True),
        _scenario("stress_0_25", samples, PHASE8_STRESS_025_PCT, promotion_eligible=True),
        _scenario("maker_maker_audit_only", samples, PHASE8_MAKER_MAKER_AUDIT_ONLY_PCT, promotion_eligible=False),
    ]
    base, stress_022, stress_025 = scenarios[0], scenarios[1], scenarios[2]
    reasons = ["maker_maker_audit_only_never_promotes"]
    if base.net_ev <= 0:
        return Phase8CostStressResult(STATUS_FAIL, scenarios, ["base_cost_net_ev_not_positive", *reasons])
    if stress_022.net_ev <= 0:
        return Phase8CostStressResult(STATUS_FAIL, scenarios, ["stress_0_22_breaks_edge", *reasons])
    if stress_025.net_ev <= 0:
        return Phase8CostStressResult(STATUS_FAIL, scenarios, ["stress_0_25_breaks_edge", *reasons])
    return Phase8CostStressResult(STATUS_PASS, scenarios, ["base_022_025_all_positive", *reasons])


def evaluate_phase8_walk_forward(
    baseline_samples: list[Phase8PolicySample],
    policy_samples: list[Phase8PolicySample],
    *,
    folds: int = 4,
    min_policy_trades_per_fold: int = 20,
) -> Phase8WalkForwardResult:
    if len(policy_samples) < max(1, folds) * min_policy_trades_per_fold:
        return Phase8WalkForwardResult(STATUS_NEED_MORE_DATA, reasons=["not_enough_policy_trades_for_folds"])
    ordered = sorted(policy_samples, key=lambda sample: sample.timestamp)
    chunk_size = max(1, len(ordered) // max(1, folds))
    fold_rows: list[Phase8WalkForwardFold] = []
    for fold_index in range(folds):
        start_idx = fold_index * chunk_size
        end_idx = len(ordered) if fold_index == folds - 1 else min(len(ordered), (fold_index + 1) * chunk_size)
        chunk = ordered[start_idx:end_idx]
        if not chunk:
            continue
        start_time = chunk[0].timestamp
        end_time = chunk[-1].timestamp
        baseline_chunk = [sample for sample in baseline_samples if start_time <= sample.timestamp <= end_time]
        policy_ev = _avg([sample.net_return_pct for sample in chunk])
        baseline_ev = _avg([sample.net_return_pct for sample in baseline_chunk])
        delta = policy_ev - baseline_ev
        fold_rows.append(Phase8WalkForwardFold(
            fold=fold_index + 1,
            start=start_time.isoformat(),
            end=end_time.isoformat(),
            baseline_trades=len(baseline_chunk),
            policy_trades=len(chunk),
            baseline_net_ev=baseline_ev,
            policy_net_ev=policy_ev,
            delta_ev=delta,
            pass_fold=bool(policy_ev > 0 and delta > 0),
        ))
    if len(fold_rows) < 3:
        return Phase8WalkForwardResult(STATUS_NEED_MORE_DATA, fold_rows, ["less_than_3_valid_folds"])
    passed = sum(1 for fold in fold_rows if fold.pass_fold)
    positive_policy = sum(1 for fold in fold_rows if fold.policy_net_ev > 0)
    if passed >= max(3, int(len(fold_rows) * 0.75)) and positive_policy == len(fold_rows):
        return Phase8WalkForwardResult(STATUS_PASS, fold_rows, ["majority_folds_positive_and_delta_positive"])
    if passed >= max(2, len(fold_rows) // 2):
        return Phase8WalkForwardResult(STATUS_WARN, fold_rows, ["mixed_folds_not_enough_for_promotion"])
    return Phase8WalkForwardResult(STATUS_FAIL, fold_rows, ["majority_folds_fail"])


def validate_phase8_candidate_from_samples(
    *,
    candidate_id: str,
    symbols: list[str],
    policy_name: str,
    baseline_samples: list[Phase8PolicySample],
    policy_samples: list[Phase8PolicySample],
    min_trades: int = 200,
    folds: int = 4,
) -> Phase8CandidateResult:
    baseline_returns = [sample.net_return_pct for sample in baseline_samples]
    policy_returns = [sample.net_return_pct for sample in policy_samples]
    baseline_ev = _avg(baseline_returns)
    policy_ev = _avg(policy_returns)
    delta_ev = policy_ev - baseline_ev
    sample_status = STATUS_PASS if len(policy_samples) >= min_trades else STATUS_NEED_MORE_DATA
    cost = evaluate_phase8_cost_stress(policy_samples)
    walk = evaluate_phase8_walk_forward(baseline_samples, policy_samples, folds=folds)
    per_symbol = {
        symbol: _avg([sample.net_return_pct for sample in policy_samples if sample.symbol == symbol])
        for symbol in symbols
    }
    anti_status, anti_reasons = _anti_overfit_status(symbols, per_symbol, walk)
    stability_status, stability_reasons = _stability_status(policy_samples)
    sensitivity_status = STATUS_PASS if cost.status == STATUS_PASS else cost.status
    reasons: list[str] = []
    if sample_status != STATUS_PASS:
        reasons.append(f"sample_status={sample_status}")
    if policy_ev <= 0:
        reasons.append("policy_net_ev_not_positive")
    if cost.status != STATUS_PASS:
        reasons.append(f"cost_stress_status={cost.status}")
    if walk.status != STATUS_PASS:
        reasons.append(f"walk_forward_status={walk.status}")
    if anti_status != STATUS_PASS:
        reasons.append(f"anti_overfit_status={anti_status}")
    if stability_status != STATUS_PASS:
        reasons.append(f"stability_status={stability_status}")
    reasons.extend(anti_reasons)
    reasons.extend(stability_reasons)
    decision = _candidate_decision(
        policy_ev=policy_ev,
        sample_status=sample_status,
        cost_status=cost.status,
        walk_status=walk.status,
        anti_status=anti_status,
        stability_status=stability_status,
        trades=len(policy_samples),
        min_trades=min_trades,
    )
    return Phase8CandidateResult(
        candidate_id=candidate_id,
        symbols=symbols,
        policy_name=policy_name,
        baseline_net_ev=baseline_ev,
        policy_net_ev=policy_ev,
        delta_ev=delta_ev,
        baseline_net_pf=net_pf(baseline_returns),
        policy_net_pf=net_pf(policy_returns),
        trades=len(policy_samples),
        sample_status=sample_status,
        cost_stress_status=cost.status,
        walk_forward_status=walk.status,
        anti_overfit_status=anti_status,
        sensitivity_status=sensitivity_status,
        stability_status=stability_status,
        final_decision=decision,
        reasons=reasons or ["all_phase8_validator_gates_passed_manual_review_only"],
        cost_stress=cost,
        walk_forward=walk,
        per_symbol_policy_net_ev=per_symbol,
    )


def run_phase8_candidate_validator(
    config: Any,
    db: Any,
    *,
    hours: int = 720,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
    policies: list[str] | None = None,
    min_trades: int = 200,
    folds: int = 4,
) -> Phase8ValidatorReport:
    symbol_list = parse_symbols(symbols, config)
    requested_policies = policies or [
        "late_entry_block_plus_dynamic_hold",
        "reversal_risk_block_plus_dynamic_hold",
    ]
    bundle = load_replay_trade_contexts(config, db, hours=hours, timeframe=timeframe, symbols=symbol_list)
    baseline_samples = _samples_for_policy(bundle.contexts, "baseline_current_exit")
    results: list[Phase8CandidateResult] = []
    for policy in requested_policies:
        policy_samples = _samples_for_policy(bundle.contexts, policy)
        candidate_id = f"{'+'.join(symbol_list)}::{policy}::{timeframe}::{int(hours)}h"
        results.append(validate_phase8_candidate_from_samples(
            candidate_id=candidate_id,
            symbols=symbol_list,
            policy_name=policy,
            baseline_samples=baseline_samples,
            policy_samples=policy_samples,
            min_trades=min_trades,
            folds=folds,
        ))
    best = max(results, key=lambda item: (item.policy_net_ev, item.delta_ev, item.trades), default=None)
    return Phase8ValidatorReport(
        hours=int(hours),
        timeframe=str(timeframe or "5m"),
        symbols=symbol_list,
        candidates=results,
        loader_statuses=bundle.loader_statuses,
        warnings=bundle.warnings,
        best_candidate_id=best.candidate_id if best else "none",
        best_decision=best.final_decision if best else STATUS_NEED_MORE_DATA,
    )


def render_phase8_validator_text(report: Phase8ValidatorReport) -> str:
    lines = [
        "PHASE 8 CANDIDATE VALIDATOR START",
        f"hours: {report.hours}",
        f"timeframe: {report.timeframe}",
        f"symbols: {','.join(report.symbols)}",
        f"best_candidate_id: {report.best_candidate_id}",
        f"best_decision: {report.best_decision}",
        "candidate | trades | base_ev | policy_ev | delta_ev | pf | cost | walk_forward | anti_overfit | stability | decision",
    ]
    for candidate in report.candidates:
        lines.append(
            f"{candidate.candidate_id} | {candidate.trades} | {candidate.baseline_net_ev:.6f} | "
            f"{candidate.policy_net_ev:.6f} | {candidate.delta_ev:.6f} | {candidate.policy_net_pf:.4f} | "
            f"{candidate.cost_stress_status} | {candidate.walk_forward_status} | "
            f"{candidate.anti_overfit_status} | {candidate.stability_status} | {candidate.final_decision}"
        )
        for reason in candidate.reasons[:8]:
            lines.append(f"  reason: {reason}")
    lines.extend([
        f"loader_statuses: {report.loader_statuses}",
        f"warnings: {', '.join(report.warnings) if report.warnings else 'none'}",
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "activation: disabled",
        "final_recommendation: NO LIVE",
        "PHASE 8 CANDIDATE VALIDATOR END",
    ])
    return "\n".join(lines)


def phase8_candidate_validator_text(
    config: Any,
    db: Any,
    *,
    hours: int = 720,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
    min_trades: int = 200,
    folds: int = 4,
) -> str:
    return render_phase8_validator_text(run_phase8_candidate_validator(
        config,
        db,
        hours=hours,
        timeframe=timeframe,
        symbols=symbols,
        min_trades=min_trades,
        folds=folds,
    ))


def phase8_cost_stress_text(
    config: Any,
    db: Any,
    *,
    hours: int = 720,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
    policy: str = "late_entry_block_plus_dynamic_hold",
) -> str:
    symbol_list = parse_symbols(symbols, config)
    bundle = load_replay_trade_contexts(config, db, hours=hours, timeframe=timeframe, symbols=symbol_list)
    samples = _samples_for_policy(bundle.contexts, policy)
    report = evaluate_phase8_cost_stress(samples)
    lines = [
        "PHASE 8 COST STRESS START",
        f"hours: {hours}",
        f"timeframe: {timeframe}",
        f"symbols: {','.join(symbol_list)}",
        f"policy_name: {policy}",
        f"trades: {len(samples)}",
        f"cost_stress_status: {report.status}",
        "scenario | cost_pct | trades | net_ev | net_pf | win_rate | promotion_eligible",
    ]
    for scenario in report.scenarios:
        lines.append(
            f"{scenario.name} | {scenario.cost_pct:.4f} | {scenario.trades} | "
            f"{scenario.net_ev:.6f} | {scenario.net_pf:.4f} | {scenario.win_rate:.4f} | {scenario.promotion_eligible}"
        )
    for reason in report.reasons:
        lines.append(f"reason: {reason}")
    lines.extend([
        "maker_maker_audit_only: never_promotes",
        "research_only: true",
        "final_recommendation: NO LIVE",
        "PHASE 8 COST STRESS END",
    ])
    return "\n".join(lines)


def _samples_for_policy(contexts: list[Any], policy_name: str) -> list[Phase8PolicySample]:
    policy = _policy_by_name(policy_name)
    samples: list[Phase8PolicySample] = []
    for ctx in contexts:
        if policy_name == "baseline_current_exit":
            simulated = _baseline_trade(ctx, policy_name)
        else:
            simulated = _simulate_dynamic_trade(ctx, policy)
        if simulated.blocked:
            continue
        timestamp = _entry_timestamp(ctx)
        samples.append(Phase8PolicySample(
            symbol=str(ctx.symbol).upper(),
            policy_name=policy_name,
            timestamp=timestamp,
            gross_return_pct=safe_float(simulated.gross_return_pct),
            net_return_pct=safe_float(simulated.net_return_pct),
            exit_reason=str(simulated.exit_reason),
            duration_bars=int(simulated.duration_bars),
        ))
    return samples


def _policy_by_name(policy_name: str) -> DynamicHoldPolicy:
    if policy_name == "baseline_current_exit":
        return DynamicHoldPolicy("baseline_current_exit")
    for policy in default_dynamic_hold_policies():
        if policy.name == policy_name:
            return policy
    raise ValueError(f"unknown_phase8_policy:{policy_name}")


def _entry_timestamp(ctx: Any) -> datetime:
    try:
        raw = ctx.candles.iloc[int(ctx.trade.entry_index)].get("timestamp")
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _scenario(name: str, samples: list[Phase8PolicySample], cost_pct: float, *, promotion_eligible: bool) -> Phase8ScenarioMetrics:
    net_returns = [sample.gross_return_pct - cost_pct for sample in samples]
    wins = [value for value in net_returns if value > 0]
    return Phase8ScenarioMetrics(
        name=name,
        cost_pct=cost_pct,
        trades=len(samples),
        net_ev=_avg(net_returns),
        net_pf=net_pf(net_returns),
        win_rate=len(wins) / max(len(net_returns), 1),
        promotion_eligible=promotion_eligible,
    )


def _candidate_decision(
    *,
    policy_ev: float,
    sample_status: str,
    cost_status: str,
    walk_status: str,
    anti_status: str,
    stability_status: str,
    trades: int,
    min_trades: int,
) -> str:
    if trades < min_trades or sample_status != STATUS_PASS:
        return REJECT_TOO_FEW_TRADES
    if policy_ev <= 0:
        return REJECT_NEGATIVE_EV
    if cost_status == STATUS_FAIL:
        return REJECT_COST_STRESS_FAIL
    if cost_status != STATUS_PASS:
        return RESEARCH_PROMISING_NOT_ACTIONABLE
    if walk_status == STATUS_FAIL:
        return REJECT_WALK_FORWARD_FAIL
    if walk_status != STATUS_PASS:
        return RESEARCH_PROMISING_NOT_ACTIONABLE
    if anti_status == STATUS_FAIL:
        return REJECT_OVERFIT_RISK
    if anti_status != STATUS_PASS or stability_status != STATUS_PASS:
        return RESEARCH_PROMISING_NOT_ACTIONABLE
    return PAPER_DEMO_READY_MANUAL_REVIEW_ONLY


def _anti_overfit_status(symbols: list[str], per_symbol: dict[str, float], walk: Phase8WalkForwardResult) -> tuple[str, list[str]]:
    if walk.status == STATUS_FAIL:
        return STATUS_FAIL, ["walk_forward_fail_is_overfit_risk"]
    if len(symbols) <= 1:
        return (STATUS_PASS if walk.status == STATUS_PASS else STATUS_WARN), []
    non_positive = [symbol for symbol, ev in per_symbol.items() if ev <= 0]
    if non_positive:
        return STATUS_WARN, [f"non_positive_symbol_ev={','.join(non_positive)}"]
    return (STATUS_PASS if walk.status == STATUS_PASS else STATUS_WARN), []


def _stability_status(samples: list[Phase8PolicySample]) -> tuple[str, list[str]]:
    if len(samples) < 200:
        return STATUS_NEED_MORE_DATA, ["low_sample_for_stability"]
    returns = [sample.net_return_pct for sample in samples]
    if max_drawdown(returns) > max(10.0, abs(sum(returns)) * 2.5):
        return STATUS_WARN, ["drawdown_proxy_high_relative_to_edge"]
    return STATUS_PASS, []


def _avg(values: list[float]) -> float:
    return sum(values) / max(len(values), 1)
