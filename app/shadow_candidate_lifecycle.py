"""ResearchOps V8/V9 — Shadow Candidate Lifecycle (research-only).

Encodes the lifecycle of a research candidate from ``DETECTED`` up to (but not
beyond) ``PAPER_CANDIDATE_LABEL_ONLY``. The lifecycle is a pure state machine
plus a gate evaluator. It never activates paper filter, never opens orders.

Promotion to ``PAPER_CANDIDATE_LABEL_ONLY`` is a label. The bot still does not
trade with the candidate; the operator must take a human decision later.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


FINAL_RECOMMENDATION_NO_LIVE = "NO LIVE"

LC_STATE_DETECTED = "DETECTED"
LC_STATE_WATCH_ONLY = "WATCH_ONLY"
LC_STATE_NEED_MORE_DATA = "NEED_MORE_DATA"
LC_STATE_SHADOW_CANDIDATE = "SHADOW_CANDIDATE"
LC_STATE_REJECTED = "REJECTED"
LC_STATE_PAPER_CANDIDATE_LABEL_ONLY = "PAPER_CANDIDATE_LABEL_ONLY"

VALID_STATES: tuple[str, ...] = (
    LC_STATE_DETECTED,
    LC_STATE_WATCH_ONLY,
    LC_STATE_NEED_MORE_DATA,
    LC_STATE_SHADOW_CANDIDATE,
    LC_STATE_REJECTED,
    LC_STATE_PAPER_CANDIDATE_LABEL_ONLY,
)


GATE_DATA_QUALITY = "data_quality_ok"
GATE_OHLCV_FRESH = "ohlcv_fresh"
GATE_NO_DUPLICATES = "no_duplicates_contamination"
GATE_NO_LOOKAHEAD = "no_lookahead_detected"
GATE_NET_EV_POSITIVE = "net_ev_positive"
GATE_NET_PF_POSITIVE = "net_pf_positive"
GATE_SAMPLE_SUFFICIENT = "sample_sufficient"
GATE_COST_STRESS_OK = "cost_stress_ok"
GATE_SLIPPAGE_STRESS_OK = "slippage_stress_ok"
GATE_FUNDING_STRESS_OK = "funding_stress_ok"
GATE_WALK_FORWARD_OK = "walk_forward_ok"
GATE_REGIME_STABILITY = "regime_stability_ok"
GATE_SYMBOL_STABILITY = "symbol_stability_ok"
GATE_TIME_STABILITY = "time_of_day_stability_ok"
GATE_NO_SINGLE_FOLD_DOMINANCE = "no_single_fold_dominance"

ALL_GATES: tuple[str, ...] = (
    GATE_DATA_QUALITY,
    GATE_OHLCV_FRESH,
    GATE_NO_DUPLICATES,
    GATE_NO_LOOKAHEAD,
    GATE_NET_EV_POSITIVE,
    GATE_NET_PF_POSITIVE,
    GATE_SAMPLE_SUFFICIENT,
    GATE_COST_STRESS_OK,
    GATE_SLIPPAGE_STRESS_OK,
    GATE_FUNDING_STRESS_OK,
    GATE_WALK_FORWARD_OK,
    GATE_REGIME_STABILITY,
    GATE_SYMBOL_STABILITY,
    GATE_TIME_STABILITY,
    GATE_NO_SINGLE_FOLD_DOMINANCE,
)


@dataclass
class GateOutcome:
    gate: str
    passed: bool
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LifecycleVerdict:
    candidate_id: str
    current_state: str
    proposed_state: str
    gates: list[GateOutcome] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    notes: str = ""
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "current_state": self.current_state,
            "proposed_state": self.proposed_state,
            "gates": [g.as_dict() for g in self.gates],
            "blockers": list(self.blockers),
            "notes": self.notes,
            "research_only": self.research_only,
            "paper_filter_enabled": self.paper_filter_enabled,
            "can_send_real_orders": self.can_send_real_orders,
            "final_recommendation": self.final_recommendation,
        }


def _gate_from_metrics(metrics: dict[str, Any]) -> list[GateOutcome]:
    outcomes: list[GateOutcome] = []

    def _g(gate: str, ok: bool, reason: str = "") -> None:
        outcomes.append(GateOutcome(gate=gate, passed=bool(ok), reason=reason))

    _g(GATE_DATA_QUALITY, bool(metrics.get("data_quality_ok", False)),
       "data_quality_bad" if not metrics.get("data_quality_ok", False) else "")
    _g(GATE_OHLCV_FRESH, bool(metrics.get("ohlcv_fresh", False)),
       "ohlcv_stale" if not metrics.get("ohlcv_fresh", False) else "")
    _g(GATE_NO_DUPLICATES, bool(metrics.get("no_duplicates", False)),
       "duplicates_above_safe_threshold" if not metrics.get("no_duplicates", False) else "")
    _g(GATE_NO_LOOKAHEAD, bool(metrics.get("no_lookahead", True)),
       "lookahead_detected" if not metrics.get("no_lookahead", True) else "")

    net_ev = float(metrics.get("net_ev_pct", 0.0) or 0.0)
    _g(GATE_NET_EV_POSITIVE, net_ev > 0,
       f"net_ev_not_positive={net_ev:.4f}" if not (net_ev > 0) else "")
    net_pf = float(metrics.get("net_pf", 0.0) or 0.0)
    _g(GATE_NET_PF_POSITIVE, net_pf > 1.0,
       f"net_pf_below_1={net_pf:.4f}" if not (net_pf > 1.0) else "")

    samples = int(metrics.get("samples_clean", 0) or 0)
    min_samples = int(metrics.get("min_samples", 150) or 150)
    _g(GATE_SAMPLE_SUFFICIENT, samples >= min_samples,
       f"samples={samples}_below_min={min_samples}" if samples < min_samples else "")

    _g(GATE_COST_STRESS_OK, bool(metrics.get("cost_stress_ok", False)),
       "cost_stress_failed" if not metrics.get("cost_stress_ok", False) else "")
    _g(GATE_SLIPPAGE_STRESS_OK, bool(metrics.get("slippage_stress_ok", False)),
       "slippage_stress_failed" if not metrics.get("slippage_stress_ok", False) else "")
    _g(GATE_FUNDING_STRESS_OK, bool(metrics.get("funding_stress_ok", True)),
       "funding_stress_failed" if not metrics.get("funding_stress_ok", True) else "")

    _g(GATE_WALK_FORWARD_OK, bool(metrics.get("walk_forward_ok", False)),
       "walk_forward_failed" if not metrics.get("walk_forward_ok", False) else "")
    _g(GATE_REGIME_STABILITY, bool(metrics.get("regime_stability_ok", False)),
       "regime_instability" if not metrics.get("regime_stability_ok", False) else "")
    _g(GATE_SYMBOL_STABILITY, bool(metrics.get("symbol_stability_ok", False)),
       "symbol_instability" if not metrics.get("symbol_stability_ok", False) else "")
    _g(GATE_TIME_STABILITY, bool(metrics.get("time_stability_ok", False)),
       "time_of_day_instability" if not metrics.get("time_stability_ok", False) else "")
    _g(GATE_NO_SINGLE_FOLD_DOMINANCE, bool(metrics.get("no_single_fold_dominance", False)),
       "single_fold_dominance" if not metrics.get("no_single_fold_dominance", False) else "")

    return outcomes


def _propose_state(current: str, gates: list[GateOutcome], samples: int) -> tuple[str, str]:
    failed = [g for g in gates if not g.passed]
    # Hard blockers — anything related to data integrity goes straight to NEED_MORE_DATA or REJECTED.
    blocker_gates = {
        GATE_DATA_QUALITY, GATE_OHLCV_FRESH, GATE_NO_DUPLICATES, GATE_NO_LOOKAHEAD,
    }
    hard_failed = [g for g in failed if g.gate in blocker_gates]
    if hard_failed:
        return LC_STATE_NEED_MORE_DATA, "hard_data_gates_failed"
    if samples < 50:
        return LC_STATE_NEED_MORE_DATA, "sample_too_small_for_any_decision"
    soft_failed_count = len(failed)
    if soft_failed_count == 0:
        return LC_STATE_PAPER_CANDIDATE_LABEL_ONLY, "all_gates_pass_label_only_no_activation"
    if soft_failed_count <= 2:
        return LC_STATE_SHADOW_CANDIDATE, "shadow_candidate_promising_label_only"
    if soft_failed_count <= 5:
        return LC_STATE_WATCH_ONLY, "watch_only_partial_gates"
    return LC_STATE_REJECTED, "too_many_soft_gates_failed"


def evaluate_candidate(
    *,
    candidate_id: str,
    current_state: str,
    metrics: dict[str, Any],
) -> LifecycleVerdict:
    if current_state not in VALID_STATES:
        current_state = LC_STATE_DETECTED
    gates = _gate_from_metrics(metrics)
    blockers = [g.reason or g.gate for g in gates if not g.passed]
    samples = int(metrics.get("samples_clean", 0) or 0)
    proposed, note = _propose_state(current_state, gates, samples)
    return LifecycleVerdict(
        candidate_id=candidate_id,
        current_state=current_state,
        proposed_state=proposed,
        gates=gates,
        blockers=blockers,
        notes=note,
    )


def summarise_lifecycle(verdicts: Iterable[LifecycleVerdict]) -> dict[str, Any]:
    vs = list(verdicts)
    return {
        "total": len(vs),
        "by_proposed_state": {
            s: sum(1 for v in vs if v.proposed_state == s) for s in VALID_STATES
        },
        "verdicts": [v.as_dict() for v in vs],
        "research_only": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }
