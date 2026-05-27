"""Phase 9 — DOT regime filter candidate lab.

Tries a small, documented set of abstention filters on top of the late-entry
block + dynamic hold policy and reports trade-count / EV / cost stress impact.
The lab is intentionally conservative: it does NOT grid-search parameters and
does NOT promote anything to paper.

Each filter is a pure function `(sample) -> bool`. When True, the trade is
abstained from. The lab simulates the policy with and without each filter,
folds the surviving trades the same way Phase 8B walk-forward does, and
classifies the filter as REJECT / NEEDS_MORE_DATA / SHADOW_ONLY /
READY_FOR_VALIDATOR_CANDIDATE.

The cost stress evaluation reuses `evaluate_phase8_cost_stress` so the gate
agrees with the Phase 8B validator.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd

from .dot_regime_diagnosis import _PolicySample, _build_samples, _policy_by_name
from .phase8_candidate_validator import (
    Phase8PolicySample,
    evaluate_phase8_cost_stress,
    evaluate_phase8_walk_forward,
)
from .phase8_research_utils import (
    FINAL_RECOMMENDATION,
    NO_LOOKAHEAD_STATUS,
    STOP_TP_SAME_BAR_RULE,
    load_replay_trade_contexts,
    net_pf,
    parse_symbols,
)


FILTER_REJECT_OVERFIT = "FILTER_REJECT_OVERFIT"
FILTER_NEEDS_MORE_DATA = "FILTER_NEEDS_MORE_DATA"
FILTER_PROMISING_SHADOW_ONLY = "FILTER_PROMISING_SHADOW_ONLY"
FILTER_READY_FOR_VALIDATOR_CANDIDATE = "FILTER_READY_FOR_VALIDATOR_CANDIDATE"


@dataclass
class FilterRecipe:
    name: str
    description: str
    predicate_id: str


@dataclass
class FilterEvalResult:
    name: str
    description: str
    trades_before: int
    trades_after: int
    trades_removed: int
    baseline_ev_before: float
    policy_ev_before: float
    policy_ev_after: float
    delta_ev_vs_unfiltered: float
    win_rate_after: float
    net_pf_after: float
    cost_stress_status: str
    cost_stress_reasons: list[str]
    walk_forward_status: str
    walk_forward_reasons: list[str]
    fold_pass_count: int
    fold_total: int
    decision: str
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DotRegimeFilterLabReport:
    symbol: str
    timeframe: str
    hours: int
    folds: int
    base_policy: str
    trades_before: int
    unfiltered_policy_ev: float
    unfiltered_baseline_ev: float
    filters: list[FilterEvalResult] = field(default_factory=list)
    best_filter: str = "none"
    best_decision: str = FILTER_NEEDS_MORE_DATA
    loader_statuses: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    research_only: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION
    no_lookahead_status: str = NO_LOOKAHEAD_STATUS
    stop_tp_same_bar_rule: str = STOP_TP_SAME_BAR_RULE

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "hours": self.hours,
            "folds": self.folds,
            "base_policy": self.base_policy,
            "trades_before": self.trades_before,
            "unfiltered_policy_ev": self.unfiltered_policy_ev,
            "unfiltered_baseline_ev": self.unfiltered_baseline_ev,
            "filters": [item.as_dict() for item in self.filters],
            "best_filter": self.best_filter,
            "best_decision": self.best_decision,
            "loader_statuses": self.loader_statuses,
            "warnings": self.warnings,
            "research_only": self.research_only,
            "final_recommendation": self.final_recommendation,
            "no_lookahead_status": self.no_lookahead_status,
            "stop_tp_same_bar_rule": self.stop_tp_same_bar_rule,
        }


# ---------------------------------------------------------------------------
# Predicate catalog
#
# Each predicate returns True when the sample should be ABSTAINED FROM
# (i.e. filtered out). Predicates avoid lookahead by only using metrics
# already computed at entry time (atr, prior move, side, exit reason of the
# *original* trade is preserved because we abstain entirely, not replay).
# ---------------------------------------------------------------------------


def _filter_block_high_volatility_pct(threshold_pct: float = 1.2) -> Callable[[_PolicySample], bool]:
    def predicate(sample: _PolicySample) -> bool:
        return sample.atr_pct >= threshold_pct
    predicate.__name__ = f"block_atr_pct_ge_{threshold_pct:.2f}"
    return predicate


def _filter_block_prior_move_extreme(multiple: float = 2.5) -> Callable[[_PolicySample], bool]:
    def predicate(sample: _PolicySample) -> bool:
        return sample.prior_move_pct > max(1.0, sample.atr_pct * multiple)
    predicate.__name__ = f"block_prior_move_above_{multiple:.1f}x_atr"
    return predicate


def _filter_block_choppy_low_trend(threshold_pct: float = 0.35) -> Callable[[_PolicySample], bool]:
    def predicate(sample: _PolicySample) -> bool:
        # Treat very low ATR as choppy/range; only abstain when both ATR low
        # AND prior move below noise floor.
        return sample.atr_pct <= threshold_pct and abs(sample.prior_move_pct) < threshold_pct
    predicate.__name__ = f"block_choppy_atr_pct_le_{threshold_pct:.2f}"
    return predicate


def _filter_block_long_when_prior_move_negative() -> Callable[[_PolicySample], bool]:
    def predicate(sample: _PolicySample) -> bool:
        return sample.side == "LONG" and sample.prior_move_pct < -0.5
    predicate.__name__ = "block_long_when_prior_move_negative"
    return predicate


def _filter_block_short_when_prior_move_positive() -> Callable[[_PolicySample], bool]:
    def predicate(sample: _PolicySample) -> bool:
        return sample.side == "SHORT" and sample.prior_move_pct < -0.5
    predicate.__name__ = "block_short_when_prior_move_extending_against"
    return predicate


CANDIDATE_FILTERS: tuple[FilterRecipe, ...] = (
    FilterRecipe(
        name="block_high_volatility_atr",
        description="Abstain when 14-bar ATR% >= 1.20%",
        predicate_id="block_atr_pct_ge_1.20",
    ),
    FilterRecipe(
        name="block_prior_move_extreme",
        description="Abstain when prior move > 2.5x ATR (late entry / extended)",
        predicate_id="block_prior_move_above_2.5x_atr",
    ),
    FilterRecipe(
        name="block_choppy_low_trend",
        description="Abstain when both ATR% and |prior move| < 0.35%",
        predicate_id="block_choppy_atr_pct_le_0.35",
    ),
    FilterRecipe(
        name="block_long_counter_prior_move",
        description="Abstain when LONG entry while last 10 bars moved against",
        predicate_id="block_long_when_prior_move_negative",
    ),
    FilterRecipe(
        name="block_short_counter_prior_move",
        description="Abstain when SHORT entry while last 10 bars moved against",
        predicate_id="block_short_when_prior_move_extending_against",
    ),
)


def _build_predicate(recipe: FilterRecipe) -> Callable[[_PolicySample], bool]:
    if recipe.predicate_id == "block_atr_pct_ge_1.20":
        return _filter_block_high_volatility_pct(1.20)
    if recipe.predicate_id == "block_prior_move_above_2.5x_atr":
        return _filter_block_prior_move_extreme(2.5)
    if recipe.predicate_id == "block_choppy_atr_pct_le_0.35":
        return _filter_block_choppy_low_trend(0.35)
    if recipe.predicate_id == "block_long_when_prior_move_negative":
        return _filter_block_long_when_prior_move_negative()
    if recipe.predicate_id == "block_short_when_prior_move_extending_against":
        return _filter_block_short_when_prior_move_positive()
    def _noop(_: _PolicySample) -> bool:
        return False
    _noop.__name__ = "noop_filter"
    return _noop


def _to_phase8_sample(sample: _PolicySample, policy_name: str) -> Phase8PolicySample:
    return Phase8PolicySample(
        symbol=sample.symbol,
        policy_name=policy_name,
        timestamp=sample.timestamp,
        gross_return_pct=sample.net_return_pct + 0.18,  # Approx: invert the 0.18% base cost used by phase 8 cost stress
        net_return_pct=sample.net_return_pct,
        exit_reason=sample.exit_reason,
        duration_bars=sample.duration_bars,
    )


def _avg(values: list[float]) -> float:
    return sum(values) / max(len(values), 1)


def _classify_filter(
    *,
    trades_after: int,
    trades_removed_share: float,
    delta_ev_vs_unfiltered: float,
    policy_ev_after: float,
    cost_stress_status: str,
    walk_forward_status: str,
    fold_pass_count: int,
    fold_total: int,
    min_trades_after: int = 80,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if trades_after < min_trades_after:
        reasons.append(f"trades_after={trades_after}_below_min_{min_trades_after}")
        return FILTER_NEEDS_MORE_DATA, reasons
    if trades_removed_share > 0.50:
        reasons.append(f"removed_share={trades_removed_share:.2f}_over_50pct_overfit_risk")
        return FILTER_REJECT_OVERFIT, reasons
    if policy_ev_after <= 0:
        reasons.append("policy_ev_after_not_positive")
        return FILTER_REJECT_OVERFIT, reasons
    if delta_ev_vs_unfiltered <= 0:
        reasons.append("delta_ev_not_positive_vs_unfiltered")
        return FILTER_REJECT_OVERFIT, reasons
    if cost_stress_status != "PASS":
        reasons.append(f"cost_stress_status={cost_stress_status}")
        return FILTER_PROMISING_SHADOW_ONLY, reasons
    if walk_forward_status == "FAIL":
        reasons.append("walk_forward_fail")
        return FILTER_REJECT_OVERFIT, reasons
    if walk_forward_status != "PASS":
        reasons.append(f"walk_forward_status={walk_forward_status}")
        return FILTER_PROMISING_SHADOW_ONLY, reasons
    if fold_total > 0 and fold_pass_count == fold_total:
        return FILTER_READY_FOR_VALIDATOR_CANDIDATE, reasons + ["all_folds_pass"]
    return FILTER_PROMISING_SHADOW_ONLY, reasons + ["filter_mostly_positive_keep_in_shadow"]


def _filter_result(
    recipe: FilterRecipe,
    samples: list[_PolicySample],
    *,
    policy_name: str,
    folds: int,
) -> FilterEvalResult:
    predicate = _build_predicate(recipe)
    surviving = [s for s in samples if not predicate(s)]
    trades_before = len(samples)
    trades_after = len(surviving)
    policy_ev_before = _avg([s.net_return_pct for s in samples])
    baseline_ev_before = _avg([s.baseline_net_return_pct for s in samples])
    policy_ev_after = _avg([s.net_return_pct for s in surviving]) if surviving else 0.0
    delta = policy_ev_after - policy_ev_before
    wins_after = [v for v in (s.net_return_pct for s in surviving) if v > 0]
    win_rate_after = (len(wins_after) / trades_after) if trades_after > 0 else 0.0
    pf_after = net_pf([s.net_return_pct for s in surviving]) if surviving else 0.0
    phase8_surviving = [_to_phase8_sample(s, policy_name) for s in surviving]
    phase8_baseline = [_to_phase8_sample(s, "baseline_current_exit") for s in samples]
    cost = evaluate_phase8_cost_stress(phase8_surviving)
    walk = evaluate_phase8_walk_forward(
        phase8_baseline, phase8_surviving, folds=folds, min_policy_trades_per_fold=20,
    )
    fold_total = len(walk.folds)
    fold_pass = sum(1 for f in walk.folds if f.pass_fold)
    trades_removed_share = (trades_before - trades_after) / max(trades_before, 1)
    decision, classification_reasons = _classify_filter(
        trades_after=trades_after,
        trades_removed_share=trades_removed_share,
        delta_ev_vs_unfiltered=delta,
        policy_ev_after=policy_ev_after,
        cost_stress_status=cost.status,
        walk_forward_status=walk.status,
        fold_pass_count=fold_pass,
        fold_total=fold_total,
    )
    return FilterEvalResult(
        name=recipe.name,
        description=recipe.description,
        trades_before=trades_before,
        trades_after=trades_after,
        trades_removed=trades_before - trades_after,
        baseline_ev_before=baseline_ev_before,
        policy_ev_before=policy_ev_before,
        policy_ev_after=policy_ev_after,
        delta_ev_vs_unfiltered=delta,
        win_rate_after=win_rate_after,
        net_pf_after=pf_after,
        cost_stress_status=cost.status,
        cost_stress_reasons=list(cost.reasons),
        walk_forward_status=walk.status,
        walk_forward_reasons=list(walk.reasons),
        fold_pass_count=fold_pass,
        fold_total=fold_total,
        decision=decision,
        reasons=classification_reasons,
    )


def run_dot_regime_filter_lab(
    config: Any,
    db: Any,
    *,
    hours: int = 720,
    timeframe: str = "5m",
    symbols: str | list[str] | None = "DOTUSDT",
    base_policy: str = "late_entry_block_plus_dynamic_hold",
    folds: int = 4,
) -> DotRegimeFilterLabReport:
    symbol_list = parse_symbols(symbols, config)
    if not symbol_list:
        symbol_list = ["DOTUSDT"]
    bundle = load_replay_trade_contexts(
        config, db, hours=hours, timeframe=timeframe, symbols=symbol_list,
    )
    policy = _policy_by_name(base_policy)
    samples = _build_samples(bundle.contexts, policy)
    folds = max(2, min(int(folds or 4), 12))
    if not samples:
        return DotRegimeFilterLabReport(
            symbol=",".join(symbol_list),
            timeframe=timeframe,
            hours=int(hours),
            folds=folds,
            base_policy=base_policy,
            trades_before=0,
            unfiltered_policy_ev=0.0,
            unfiltered_baseline_ev=0.0,
            filters=[],
            best_filter="none",
            best_decision=FILTER_NEEDS_MORE_DATA,
            loader_statuses=bundle.loader_statuses,
            warnings=list(bundle.warnings) + ["no_policy_samples_for_filter_lab"],
        )
    results = [
        _filter_result(recipe, samples, policy_name=base_policy, folds=folds)
        for recipe in CANDIDATE_FILTERS
    ]
    ready = [r for r in results if r.decision == FILTER_READY_FOR_VALIDATOR_CANDIDATE]
    if ready:
        best = max(ready, key=lambda r: r.delta_ev_vs_unfiltered)
        best_decision = FILTER_READY_FOR_VALIDATOR_CANDIDATE
    else:
        shadow = [r for r in results if r.decision == FILTER_PROMISING_SHADOW_ONLY]
        if shadow:
            best = max(shadow, key=lambda r: r.delta_ev_vs_unfiltered)
            best_decision = FILTER_PROMISING_SHADOW_ONLY
        else:
            best = max(results, key=lambda r: r.delta_ev_vs_unfiltered, default=None)
            best_decision = best.decision if best else FILTER_NEEDS_MORE_DATA
    best_filter = best.name if best else "none"
    return DotRegimeFilterLabReport(
        symbol=",".join(symbol_list),
        timeframe=timeframe,
        hours=int(hours),
        folds=folds,
        base_policy=base_policy,
        trades_before=len(samples),
        unfiltered_policy_ev=_avg([s.net_return_pct for s in samples]),
        unfiltered_baseline_ev=_avg([s.baseline_net_return_pct for s in samples]),
        filters=results,
        best_filter=best_filter,
        best_decision=best_decision,
        loader_statuses=bundle.loader_statuses,
        warnings=list(bundle.warnings),
    )


def render_dot_regime_filter_lab_text(report: DotRegimeFilterLabReport) -> str:
    lines = [
        "DOT REGIME FILTER LAB START",
        f"symbol: {report.symbol}",
        f"timeframe: {report.timeframe}",
        f"hours: {report.hours}",
        f"folds: {report.folds}",
        f"base_policy: {report.base_policy}",
        f"trades_before: {report.trades_before}",
        f"unfiltered_policy_ev: {report.unfiltered_policy_ev:.6f}",
        f"unfiltered_baseline_ev: {report.unfiltered_baseline_ev:.6f}",
        f"best_filter: {report.best_filter}",
        f"best_decision: {report.best_decision}",
        "filter | trades_before | trades_after | removed | policy_ev_before | policy_ev_after | delta | pf | win | cost | walk | folds_pass | decision",
    ]
    for result in report.filters:
        lines.append(
            f"{result.name} | {result.trades_before} | {result.trades_after} | "
            f"{result.trades_removed} | {result.policy_ev_before:.6f} | "
            f"{result.policy_ev_after:.6f} | {result.delta_ev_vs_unfiltered:.6f} | "
            f"{result.net_pf_after:.4f} | {result.win_rate_after:.3f} | "
            f"{result.cost_stress_status} | {result.walk_forward_status} | "
            f"{result.fold_pass_count}/{result.fold_total} | {result.decision}"
        )
        for reason in result.reasons[:4]:
            lines.append(f"  reason: {reason}")
    if report.warnings:
        lines.append("warnings:")
        for warning in report.warnings[:6]:
            lines.append(f"- {warning}")
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "activation: disabled",
        "final_recommendation: NO LIVE",
        "DOT REGIME FILTER LAB END",
    ])
    return "\n".join(lines)


def dot_regime_filter_lab_text(
    config: Any,
    db: Any,
    *,
    hours: int = 720,
    timeframe: str = "5m",
    symbols: str | list[str] | None = "DOTUSDT",
    base_policy: str = "late_entry_block_plus_dynamic_hold",
    folds: int = 4,
) -> str:
    return render_dot_regime_filter_lab_text(run_dot_regime_filter_lab(
        config, db,
        hours=hours, timeframe=timeframe, symbols=symbols,
        base_policy=base_policy, folds=folds,
    ))
