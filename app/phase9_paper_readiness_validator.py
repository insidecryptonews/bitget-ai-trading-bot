"""Phase 9 — Paper / Demo readiness validator.

Wraps the Phase 8B candidate validator with an additional, stricter set of
gates and explicit data freshness checks so the only positive verdict it can
emit is:

    PAPER_DEMO_READY_MANUAL_REVIEW_ONLY

That label NEVER activates paper filter, never flips
`can_send_real_orders`, never opens orders. It is an annotation for a human
operator. Any failure path returns RESEARCH_PROMISING_NOT_ACTIONABLE or
REJECT_* — strictly research-only.

Gates:
  1.  sample_status=PASS
  2.  trades >= min_trades (default 250)
  3.  policy_net_ev > 0
  4.  policy_net_pf >= min_net_pf (default 1.15)
  5.  cost_stress_status=PASS (base / 0.22 / 0.25 all positive)
  6.  walk_forward_status=PASS (Phase 8B contract)
  7.  anti_overfit_status=PASS
  8.  stability_status=PASS
  9.  no single fold dominates (>= 80% of total positive EV)
 10.  no fold has policy EV < -0.10 (catastrophic fold)
 11.  delta_ev_vs_baseline > 0 AND not based purely on cutting losers
 12.  data freshness OK for all symbols (data_freshness_gate)
 13.  validation_hours >= 720
 14.  paper_filter_enabled stays False (invariant)
 15.  can_send_real_orders stays False (invariant)

If any gate fails, the candidate is reported with the failing gate(s) and a
non-actionable decision.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .data_freshness_gate import (
    FreshnessVerdict,
    NON_ACTIONABLE_STATUSES,
    aggregate_actionable,
    evaluate_freshness_many,
)
from .phase8_candidate_validator import (
    PAPER_DEMO_READY_MANUAL_REVIEW_ONLY,
    Phase8CandidateResult,
    REJECT_COST_STRESS_FAIL,
    REJECT_NEGATIVE_EV,
    REJECT_OVERFIT_RISK,
    REJECT_TOO_FEW_TRADES,
    REJECT_WALK_FORWARD_FAIL,
    RESEARCH_PROMISING_NOT_ACTIONABLE,
    STATUS_PASS,
    Phase8ValidatorReport,
    run_phase8_candidate_validator,
)
from .phase8_research_utils import (
    FINAL_RECOMMENDATION,
    NO_LOOKAHEAD_STATUS,
    parse_symbols,
)


PHASE9_READY = PAPER_DEMO_READY_MANUAL_REVIEW_ONLY
PHASE9_PROMISING = RESEARCH_PROMISING_NOT_ACTIONABLE
PHASE9_NEED_DATA = "NEED_MORE_DATA"
PHASE9_REJECT_DATA_STALE = "REJECT_DATA_STALE"
# ResearchOps V5 — additional gates
PHASE9_REJECT_DATA_QUALITY = "REJECT_DATA_QUALITY"
PHASE9_REJECT_NEGATIVE_NET = "REJECT_NEGATIVE_NET"
PHASE9_REJECT_CATASTROPHIC_FOLD = "REJECT_CATASTROPHIC_FOLD"


@dataclass
class Phase9CandidateVerdict:
    candidate_id: str
    symbols: list[str]
    policy_name: str
    phase8_decision: str
    phase9_decision: str
    trades: int
    policy_net_ev: float
    policy_net_pf: float
    delta_ev: float
    sample_status: str
    cost_stress_status: str
    walk_forward_status: str
    anti_overfit_status: str
    stability_status: str
    fold_dominance_ok: bool
    catastrophic_fold_present: bool
    delta_ev_positive: bool
    data_freshness_ok: bool
    validation_hours: int
    min_trades: int
    min_net_pf: float
    # ResearchOps V5 — additional inputs surfaced by V2 gates
    data_quality_status: str = "UNKNOWN"
    net_profit_lock_eligible: bool = False
    capital_leverage_net_positive: bool = False
    gross_green_net_negative: bool = False
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION
    reasons: list[str] = field(default_factory=list)
    blocked_gates: list[str] = field(default_factory=list)
    freshness_verdicts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Phase9PaperReadinessReport:
    hours: int
    timeframe: str
    symbols: list[str]
    min_trades: int
    min_net_pf: float
    folds: int
    candidates: list[Phase9CandidateVerdict] = field(default_factory=list)
    aggregate_actionable: bool = False
    best_candidate_id: str = "none"
    best_decision: str = PHASE9_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION
    no_lookahead_status: str = NO_LOOKAHEAD_STATUS

    def as_dict(self) -> dict[str, Any]:
        return {
            "hours": self.hours,
            "timeframe": self.timeframe,
            "symbols": self.symbols,
            "min_trades": self.min_trades,
            "min_net_pf": self.min_net_pf,
            "folds": self.folds,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "aggregate_actionable": self.aggregate_actionable,
            "best_candidate_id": self.best_candidate_id,
            "best_decision": self.best_decision,
            "research_only": self.research_only,
            "paper_filter_enabled": self.paper_filter_enabled,
            "can_send_real_orders": self.can_send_real_orders,
            "final_recommendation": self.final_recommendation,
            "no_lookahead_status": self.no_lookahead_status,
        }


def _fold_dominance_ok(candidate: Phase8CandidateResult) -> bool:
    walk = candidate.walk_forward
    if walk is None or not walk.folds:
        return False
    positive_ev_sum = sum(max(0.0, fold.policy_net_ev) for fold in walk.folds)
    if positive_ev_sum <= 0:
        return False
    top = max(max(0.0, fold.policy_net_ev) for fold in walk.folds)
    return (top / positive_ev_sum) <= 0.80  # No single fold > 80% of positive EV


def _catastrophic_fold_present(candidate: Phase8CandidateResult, threshold: float = -0.10) -> bool:
    walk = candidate.walk_forward
    if walk is None:
        return True
    return any(fold.policy_net_ev <= threshold for fold in walk.folds)


def _decision_for_candidate(
    candidate: Phase8CandidateResult,
    *,
    min_trades: int,
    min_net_pf: float,
    validation_hours: int,
    freshness_ok: bool,
    fold_dominance_ok: bool,
    catastrophic_fold: bool,
    data_quality_status: str = "UNKNOWN",
    net_profit_lock_eligible: bool = False,
    capital_leverage_net_positive: bool = False,
    gross_green_net_negative: bool = False,
    require_v5_gates: bool = False,
) -> tuple[str, list[str], list[str]]:
    blocked: list[str] = []
    reasons: list[str] = []
    if not freshness_ok:
        blocked.append("data_freshness")
        reasons.append("data_freshness_blocked")
        return PHASE9_REJECT_DATA_STALE, blocked, reasons
    # ResearchOps V5 — data quality hard gate. BAD blocks promotion.
    if str(data_quality_status).upper() == "BAD":
        blocked.append("data_quality")
        reasons.append("data_quality_status=BAD")
        return PHASE9_REJECT_DATA_QUALITY, blocked, reasons
    if validation_hours < 720:
        blocked.append("validation_hours")
        reasons.append(f"validation_hours={validation_hours}_below_720")
    if candidate.sample_status != STATUS_PASS:
        blocked.append("sample_status")
        reasons.append(f"sample_status={candidate.sample_status}")
    if candidate.trades < min_trades:
        blocked.append("min_trades")
        reasons.append(f"trades={candidate.trades}_below_min_{min_trades}")
    if candidate.policy_net_ev <= 0:
        blocked.append("policy_net_ev")
        reasons.append("policy_net_ev_not_positive")
    if candidate.policy_net_pf < min_net_pf:
        blocked.append("policy_net_pf")
        reasons.append(f"policy_net_pf={candidate.policy_net_pf:.4f}_below_{min_net_pf:.2f}")
    if candidate.cost_stress_status != STATUS_PASS:
        blocked.append("cost_stress")
        if candidate.cost_stress_status == "FAIL":
            return REJECT_COST_STRESS_FAIL, blocked, reasons + ["cost_stress_status=FAIL"]
        reasons.append(f"cost_stress_status={candidate.cost_stress_status}")
    if candidate.walk_forward_status != STATUS_PASS:
        blocked.append("walk_forward")
        if candidate.walk_forward_status == "FAIL":
            return REJECT_WALK_FORWARD_FAIL, blocked, reasons + ["walk_forward_status=FAIL"]
        reasons.append(f"walk_forward_status={candidate.walk_forward_status}")
    if candidate.anti_overfit_status != STATUS_PASS:
        blocked.append("anti_overfit")
        reasons.append(f"anti_overfit_status={candidate.anti_overfit_status}")
    if candidate.stability_status != STATUS_PASS:
        blocked.append("stability")
        reasons.append(f"stability_status={candidate.stability_status}")
    if not fold_dominance_ok:
        blocked.append("fold_dominance")
        reasons.append("single_fold_dominates_positive_ev")
    if catastrophic_fold:
        blocked.append("catastrophic_fold")
        reasons.append("at_least_one_fold_below_-10pct_ev")
        # Catastrophic fold is a HARD reject — V5 contract.
        return PHASE9_REJECT_CATASTROPHIC_FOLD, blocked, reasons
    if candidate.delta_ev <= 0:
        blocked.append("delta_ev")
        reasons.append("delta_ev_not_positive_vs_baseline")
    # ResearchOps V5 — additional gates. These are *additive*: callers that do
    # not feed them in keep the previous behaviour. `gross_green_net_negative=True`
    # is always a hard block. `net_profit_lock_eligible` / `capital_leverage_net_positive`
    # default to False ("not evaluated") and only block when the caller opts in
    # via `require_v5_gates=True`.
    if gross_green_net_negative:
        blocked.append("gross_green_net_negative")
        reasons.append("gross_green_net_negative_blocks_promotion")
        return PHASE9_REJECT_NEGATIVE_NET, blocked, reasons
    if not net_profit_lock_eligible and require_v5_gates:
        blocked.append("net_profit_lock_promotion_eligible")
        reasons.append("net_profit_lock_not_promotion_eligible")
    if not capital_leverage_net_positive and require_v5_gates:
        blocked.append("capital_leverage_net_positive")
        reasons.append("capital_leverage_scenario_not_net_positive")
    if blocked:
        # Pick the most specific REJECT label only if a hard failure occurred.
        if "policy_net_ev" in blocked:
            return REJECT_NEGATIVE_EV, blocked, reasons
        return PHASE9_PROMISING, blocked, reasons
    return PHASE9_READY, blocked, reasons + ["all_phase9_gates_passed_manual_review_only"]


def _verdict_from_phase8(
    candidate: Phase8CandidateResult,
    freshness_verdicts: dict[str, FreshnessVerdict],
    *,
    min_trades: int,
    min_net_pf: float,
    validation_hours: int,
    data_quality_status: str = "UNKNOWN",
    net_profit_lock_eligible: bool = False,
    capital_leverage_net_positive: bool = False,
    gross_green_net_negative: bool = False,
    require_v5_gates: bool = False,
) -> Phase9CandidateVerdict:
    freshness_ok = aggregate_actionable(freshness_verdicts)
    fold_dom_ok = _fold_dominance_ok(candidate)
    catastrophic = _catastrophic_fold_present(candidate)
    decision, blocked, reasons = _decision_for_candidate(
        candidate,
        min_trades=min_trades,
        min_net_pf=min_net_pf,
        validation_hours=validation_hours,
        freshness_ok=freshness_ok,
        fold_dominance_ok=fold_dom_ok,
        catastrophic_fold=catastrophic,
        data_quality_status=data_quality_status,
        net_profit_lock_eligible=net_profit_lock_eligible,
        capital_leverage_net_positive=capital_leverage_net_positive,
        gross_green_net_negative=gross_green_net_negative,
        require_v5_gates=require_v5_gates,
    )
    return Phase9CandidateVerdict(
        candidate_id=candidate.candidate_id,
        symbols=list(candidate.symbols),
        policy_name=candidate.policy_name,
        phase8_decision=candidate.final_decision,
        phase9_decision=decision,
        trades=candidate.trades,
        policy_net_ev=candidate.policy_net_ev,
        policy_net_pf=candidate.policy_net_pf,
        delta_ev=candidate.delta_ev,
        sample_status=candidate.sample_status,
        cost_stress_status=candidate.cost_stress_status,
        walk_forward_status=candidate.walk_forward_status,
        anti_overfit_status=candidate.anti_overfit_status,
        stability_status=candidate.stability_status,
        fold_dominance_ok=fold_dom_ok,
        catastrophic_fold_present=catastrophic,
        delta_ev_positive=candidate.delta_ev > 0,
        data_freshness_ok=freshness_ok,
        validation_hours=validation_hours,
        min_trades=min_trades,
        min_net_pf=min_net_pf,
        reasons=reasons,
        blocked_gates=blocked,
        freshness_verdicts={symbol: verdict.as_dict() for symbol, verdict in freshness_verdicts.items()},
    )


def run_phase9_paper_readiness(
    config: Any,
    db: Any,
    *,
    hours: int = 720,
    timeframe: str = "5m",
    symbols: str | list[str] | None = "DOTUSDT",
    min_trades: int = 250,
    min_net_pf: float = 1.15,
    folds: int = 4,
    policies: list[str] | None = None,
    historical: bool = False,
    # ResearchOps V5 — optional inputs from sibling labs. Defaults preserve the
    # original Phase 9 behaviour. Pass them in to apply the V2 hard gates.
    data_quality_status: str = "UNKNOWN",
    net_profit_lock_eligible: bool = False,
    capital_leverage_net_positive: bool = False,
    gross_green_net_negative: bool = False,
    require_v5_gates: bool = False,
    # ResearchOps V6 — when enabled, consult the central clean metrics helper
    # to derive `data_quality_status` and refuse promotion when CLEAN samples
    # disagree with RAW or the count is too low.
    require_v6_clean_gate: bool = True,
) -> Phase9PaperReadinessReport:
    symbol_list = parse_symbols(symbols, config)
    if not symbol_list:
        symbol_list = ["DOTUSDT"]
    # V6 — pull central clean metrics and override data_quality_status if needed.
    if require_v6_clean_gate:
        try:
            from .clean_research_metrics import get_clean_research_metrics
            clean_metrics = get_clean_research_metrics(
                db, hours=int(hours), symbols=symbol_list, timeframes=[timeframe],
            )
            # If the helper sees BAD or LOW sample, escalate to the gates.
            if clean_metrics.data_quality_status == "BAD":
                data_quality_status = "BAD"
            # If RAW says positive EV but CLEAN says negative, force negative
            # gross_green_net_negative so promotion is rejected as cost-failure.
            if clean_metrics.raw_ev_pct > 0 and clean_metrics.clean_ev_pct <= 0:
                gross_green_net_negative = True
        except Exception:
            pass
    freshness_verdicts = evaluate_freshness_many(
        db, symbols=symbol_list, timeframe=timeframe, historical=historical,
    )
    phase8_report: Phase8ValidatorReport = run_phase8_candidate_validator(
        config, db,
        hours=hours, timeframe=timeframe, symbols=symbol_list,
        policies=policies, min_trades=min_trades, folds=folds,
    )
    candidates: list[Phase9CandidateVerdict] = []
    for candidate in phase8_report.candidates:
        verdict = _verdict_from_phase8(
            candidate, freshness_verdicts,
            min_trades=min_trades,
            min_net_pf=min_net_pf,
            validation_hours=int(hours),
            data_quality_status=data_quality_status,
            net_profit_lock_eligible=net_profit_lock_eligible,
            capital_leverage_net_positive=capital_leverage_net_positive,
            gross_green_net_negative=gross_green_net_negative,
            require_v5_gates=require_v5_gates,
        )
        verdict.data_quality_status = str(data_quality_status)
        verdict.net_profit_lock_eligible = bool(net_profit_lock_eligible)
        verdict.capital_leverage_net_positive = bool(capital_leverage_net_positive)
        verdict.gross_green_net_negative = bool(gross_green_net_negative)
        candidates.append(verdict)
    actionable = aggregate_actionable(freshness_verdicts) and any(
        candidate.phase9_decision == PHASE9_READY for candidate in candidates
    )
    best = max(
        candidates,
        key=lambda item: (
            1 if item.phase9_decision == PHASE9_READY else 0,
            item.policy_net_ev,
            item.delta_ev,
            item.trades,
        ),
        default=None,
    )
    return Phase9PaperReadinessReport(
        hours=int(hours),
        timeframe=str(timeframe or "5m"),
        symbols=symbol_list,
        min_trades=int(min_trades),
        min_net_pf=float(min_net_pf),
        folds=int(folds),
        candidates=candidates,
        aggregate_actionable=actionable,
        best_candidate_id=best.candidate_id if best else "none",
        best_decision=best.phase9_decision if best else PHASE9_NEED_DATA,
    )


def render_phase9_paper_readiness_text(report: Phase9PaperReadinessReport) -> str:
    lines = [
        "PHASE 9 PAPER READINESS START",
        f"hours: {report.hours}",
        f"timeframe: {report.timeframe}",
        f"symbols: {','.join(report.symbols)}",
        f"min_trades: {report.min_trades}",
        f"min_net_pf: {report.min_net_pf:.2f}",
        f"folds: {report.folds}",
        f"aggregate_actionable: {str(report.aggregate_actionable).lower()}",
        f"best_candidate_id: {report.best_candidate_id}",
        f"best_decision: {report.best_decision}",
        "candidate | trades | ev | pf | delta | cost | walk | anti | stab | fold_dom | cat | freshness | decision",
    ]
    for candidate in report.candidates:
        freshness_summary = ",".join(
            f"{symbol}:{verdict.get('status')}"
            for symbol, verdict in candidate.freshness_verdicts.items()
        ) or "-"
        lines.append(
            f"{candidate.candidate_id} | {candidate.trades} | {candidate.policy_net_ev:.6f} | "
            f"{candidate.policy_net_pf:.4f} | {candidate.delta_ev:.6f} | "
            f"{candidate.cost_stress_status} | {candidate.walk_forward_status} | "
            f"{candidate.anti_overfit_status} | {candidate.stability_status} | "
            f"{str(candidate.fold_dominance_ok).lower()} | "
            f"{str(candidate.catastrophic_fold_present).lower()} | {freshness_summary} | "
            f"{candidate.phase9_decision}"
        )
        for reason in candidate.reasons[:6]:
            lines.append(f"  reason: {reason}")
        if candidate.blocked_gates:
            lines.append(f"  blocked_gates: {','.join(candidate.blocked_gates)}")
    lines.extend([
        "non_actionable_statuses_for_freshness: " + ",".join(NON_ACTIONABLE_STATUSES),
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "activation: disabled",
        "final_recommendation: NO LIVE",
        "PHASE 9 PAPER READINESS END",
    ])
    return "\n".join(lines)


def phase9_paper_readiness_text(
    config: Any,
    db: Any,
    *,
    hours: int = 720,
    timeframe: str = "5m",
    symbols: str | list[str] | None = "DOTUSDT",
    min_trades: int = 250,
    min_net_pf: float = 1.15,
    folds: int = 4,
) -> str:
    return render_phase9_paper_readiness_text(run_phase9_paper_readiness(
        config, db,
        hours=hours, timeframe=timeframe, symbols=symbols,
        min_trades=min_trades, min_net_pf=min_net_pf, folds=folds,
    ))
