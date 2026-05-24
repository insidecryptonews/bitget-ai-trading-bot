"""Final Research Policy Builder — offline aggregator across labs.

Consumes the multi-symbol backtester breakdown + walk-forward + data quality
audits, and emits a JSON-serialisable candidate policy. NEVER auto-activates
paper filter, NEVER touches runtime, NEVER writes to .env.

The policy is purely declarative: it describes what would be allowed if a
human approves it later, but it cannot toggle anything itself.

Final decision is one of:
  POLICY_READY_FOR_PAPER : all gates pass (human still has to flip the switch)
  NEED_MORE_DATA        : enough signal but sample too small
  NO_EDGE_FOUND         : no group passes the gates
  DATA_QUALITY_BLOCKER  : data audits failed, do not trust any policy yet
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any

from .backtest_breakdown import (
    BreakdownReport,
    DECISION_CANDIDATE_RESEARCH,
    DECISION_WATCH_ONLY,
)
from .utils import iso_utc


FINAL_RECOMMENDATION = "NO LIVE"

POLICY_READY_FOR_PAPER = "POLICY_READY_FOR_PAPER"
NEED_MORE_DATA = "NEED_MORE_DATA"
NO_EDGE_FOUND = "NO_EDGE_FOUND"
DATA_QUALITY_BLOCKER = "DATA_QUALITY_BLOCKER"


@dataclass
class PolicyGates:
    """Tunable thresholds for promotion. Conservative defaults."""

    min_trades_per_setup: int = 100
    min_net_ev: float = 0.05
    min_net_pf: float = 1.20
    max_drawdown_pct: float = 8.0
    max_time_pct: float = 0.85
    min_walk_forward_positive_windows: int = 2
    cost_stress_net_ev_min_022: float = 0.0
    cost_stress_net_ev_min_025: float = -0.05
    paper_filter_never_auto_activate: bool = True   # invariant


@dataclass
class CandidatePolicy:
    candidate_policy_id: str
    allowed_symbols: list[str]
    allowed_sides: list[str]
    allowed_regimes: list[str]
    allowed_score_buckets: list[str]
    blocked_setups: list[str]
    exit_policy_candidate: str
    min_trades: int
    net_ev: float
    net_pf: float
    tp_pct: float
    sl_pct: float
    time_pct: float
    max_drawdown: float
    confidence: str
    decision: str
    reasons: list[str] = field(default_factory=list)
    data_quality_status: str = "UNKNOWN"
    walk_forward_status: str = "NOT_RUN"
    final_recommendation: str = FINAL_RECOMMENDATION
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    generated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PolicyBuildInput:
    breakdown: BreakdownReport
    data_quality_status: str = "UNKNOWN"   # OK / WARNING / BAD
    label_quality_status: str = "UNKNOWN"  # OK / WARNING / BAD
    walk_forward_status: str = "NOT_RUN"   # NOT_RUN / FAIL / PASS / NEED_MORE_FOLDS


def _empty_policy(decision: str, reasons: list[str], data_quality: str = "UNKNOWN", walk_forward: str = "NOT_RUN") -> CandidatePolicy:
    return CandidatePolicy(
        candidate_policy_id=f"empty_{decision.lower()}",
        allowed_symbols=[],
        allowed_sides=[],
        allowed_regimes=[],
        allowed_score_buckets=[],
        blocked_setups=[],
        exit_policy_candidate="current_exit",
        min_trades=0,
        net_ev=0.0,
        net_pf=0.0,
        tp_pct=0.0,
        sl_pct=0.0,
        time_pct=0.0,
        max_drawdown=0.0,
        confidence="NONE",
        decision=decision,
        reasons=reasons,
        data_quality_status=data_quality,
        walk_forward_status=walk_forward,
        generated_at=iso_utc(),
    )


def build_policy(
    inputs: PolicyBuildInput,
    *,
    gates: PolicyGates | None = None,
) -> CandidatePolicy:
    """Apply gates to a breakdown report + audit statuses and emit a policy."""
    gates = gates or PolicyGates()
    reasons: list[str] = []

    # Data quality guard — blocking
    if inputs.data_quality_status == "BAD" or inputs.label_quality_status == "BAD":
        return _empty_policy(
            DATA_QUALITY_BLOCKER,
            reasons=[
                f"data_quality_status={inputs.data_quality_status}",
                f"label_quality_status={inputs.label_quality_status}",
                "do_not_trust_policy_until_repair",
            ],
            data_quality=inputs.data_quality_status,
            walk_forward=inputs.walk_forward_status,
        )

    breakdown = inputs.breakdown
    blocked_setups = [g.group_key for g in breakdown.worst_groups]

    candidates = breakdown.candidate_research_groups
    if not candidates:
        # Maybe we have watch-only with positive EV but small sample
        if breakdown.promising_watch_only_groups:
            watch = breakdown.promising_watch_only_groups[0]
            return CandidatePolicy(
                candidate_policy_id=f"watch_only_{watch.group_key.replace('|','_')[:48]}",
                allowed_symbols=[],
                allowed_sides=[],
                allowed_regimes=[],
                allowed_score_buckets=[],
                blocked_setups=blocked_setups,
                exit_policy_candidate="current_exit",
                min_trades=watch.trades,
                net_ev=watch.net_ev,
                net_pf=watch.net_pf,
                tp_pct=watch.tp_pct,
                sl_pct=watch.sl_pct,
                time_pct=watch.time_pct,
                max_drawdown=watch.max_drawdown,
                confidence="LOW",
                decision=NEED_MORE_DATA,
                reasons=["only_watch_only_setups_present_sample_too_small"],
                data_quality_status=inputs.data_quality_status,
                walk_forward_status=inputs.walk_forward_status,
                generated_at=iso_utc(),
            )
        return _empty_policy(
            NO_EDGE_FOUND,
            reasons=["no_candidate_research_groups_in_breakdown"],
            data_quality=inputs.data_quality_status,
            walk_forward=inputs.walk_forward_status,
        )

    # Apply per-candidate gates
    survivors: list[Any] = []
    for cand in candidates:
        if cand.trades < gates.min_trades_per_setup:
            continue
        if cand.net_ev < gates.min_net_ev:
            continue
        if cand.net_pf < gates.min_net_pf:
            continue
        if cand.max_drawdown > gates.max_drawdown_pct:
            continue
        if cand.time_pct > gates.max_time_pct:
            continue
        survivors.append(cand)

    if not survivors:
        return _empty_policy(
            NO_EDGE_FOUND,
            reasons=[
                "candidates_failed_gates",
                f"min_trades={gates.min_trades_per_setup}",
                f"min_net_ev={gates.min_net_ev}",
                f"min_net_pf={gates.min_net_pf}",
                f"max_drawdown_pct={gates.max_drawdown_pct}",
                f"max_time_pct={gates.max_time_pct}",
            ],
            data_quality=inputs.data_quality_status,
            walk_forward=inputs.walk_forward_status,
        )

    # Walk-forward gate
    if inputs.walk_forward_status not in {"PASS"}:
        # Even with gross gates passing, we refuse PAPER_READY unless walk-forward passed.
        best = survivors[0]
        return CandidatePolicy(
            candidate_policy_id=f"need_walk_forward_{best.group_key.replace('|','_')[:48]}",
            allowed_symbols=[],
            allowed_sides=[],
            allowed_regimes=[],
            allowed_score_buckets=[],
            blocked_setups=blocked_setups,
            exit_policy_candidate="current_exit",
            min_trades=best.trades,
            net_ev=best.net_ev,
            net_pf=best.net_pf,
            tp_pct=best.tp_pct,
            sl_pct=best.sl_pct,
            time_pct=best.time_pct,
            max_drawdown=best.max_drawdown,
            confidence="MEDIUM",
            decision=NEED_MORE_DATA,
            reasons=[
                "candidates_pass_gross_gates",
                f"walk_forward_status={inputs.walk_forward_status}",
                "promote_to_PAPER_READY_blocked_until_walk_forward_pass",
            ],
            data_quality_status=inputs.data_quality_status,
            walk_forward_status=inputs.walk_forward_status,
            generated_at=iso_utc(),
        )

    # All gates passed — but we STILL do not auto-activate; flag the policy as
    # ready and the operator must flip the switch manually.
    best = survivors[0]
    return CandidatePolicy(
        candidate_policy_id=f"paper_ready_{best.group_key.replace('|','_')[:48]}",
        allowed_symbols=sorted({_token(g.group_key, 0) for g in survivors}),
        allowed_sides=sorted({_token(g.group_key, 1) for g in survivors if "|" in g.group_key}),
        allowed_regimes=sorted({_token(g.group_key, 2) for g in survivors if g.group_key.count("|") >= 2}),
        allowed_score_buckets=sorted({_token(g.group_key, 3) for g in survivors if g.group_key.count("|") >= 3}),
        blocked_setups=blocked_setups,
        exit_policy_candidate="current_exit",
        min_trades=best.trades,
        net_ev=best.net_ev,
        net_pf=best.net_pf,
        tp_pct=best.tp_pct,
        sl_pct=best.sl_pct,
        time_pct=best.time_pct,
        max_drawdown=best.max_drawdown,
        confidence="HIGH",
        decision=POLICY_READY_FOR_PAPER,
        reasons=[
            "candidates_pass_gross_gates",
            "walk_forward_status_pass",
            "data_quality_acceptable",
            "still_requires_human_activation_of_paper_filter",
        ],
        data_quality_status=inputs.data_quality_status,
        walk_forward_status=inputs.walk_forward_status,
        generated_at=iso_utc(),
    )


def _token(key: str, index: int) -> str:
    parts = key.split("|")
    if 0 <= index < len(parts):
        return parts[index]
    return "UNKNOWN"


def render_policy_text(policy: CandidatePolicy) -> str:
    lines = ["FINAL RESEARCH POLICY BUILDER START"]
    lines.append(f"generated_at: {policy.generated_at}")
    lines.append(f"candidate_policy_id: {policy.candidate_policy_id}")
    lines.append(f"decision: {policy.decision}")
    lines.append(f"confidence: {policy.confidence}")
    lines.append(f"data_quality_status: {policy.data_quality_status}")
    lines.append(f"walk_forward_status: {policy.walk_forward_status}")
    lines.append(f"min_trades: {policy.min_trades}")
    lines.append(f"net_ev: {policy.net_ev:.6f}")
    lines.append(f"net_pf: {policy.net_pf:.4f}")
    lines.append(f"tp_pct: {policy.tp_pct:.4f}")
    lines.append(f"sl_pct: {policy.sl_pct:.4f}")
    lines.append(f"time_pct: {policy.time_pct:.4f}")
    lines.append(f"max_drawdown: {policy.max_drawdown:.4f}")
    lines.append(f"allowed_symbols: {','.join(policy.allowed_symbols) if policy.allowed_symbols else 'none'}")
    lines.append(f"allowed_sides: {','.join(policy.allowed_sides) if policy.allowed_sides else 'none'}")
    lines.append(f"allowed_regimes: {','.join(policy.allowed_regimes) if policy.allowed_regimes else 'none'}")
    lines.append(f"allowed_score_buckets: {','.join(policy.allowed_score_buckets) if policy.allowed_score_buckets else 'none'}")
    lines.append(f"blocked_setups_count: {len(policy.blocked_setups)}")
    if policy.blocked_setups[:10]:
        lines.append("blocked_setups_top_10:")
        for s in policy.blocked_setups[:10]:
            lines.append(f"- {s}")
    lines.append(f"exit_policy_candidate: {policy.exit_policy_candidate}")
    lines.append("reasons:")
    for r in policy.reasons:
        lines.append(f"- {r}")
    lines.append("paper_filter_enabled: false")
    lines.append("can_send_real_orders: false")
    lines.append("research_only: true")
    lines.append("auto_activation: never")
    lines.append(f"final_recommendation: {policy.final_recommendation}")
    lines.append("FINAL RESEARCH POLICY BUILDER END")
    return "\n".join(lines)


def export_policy_json(policy: CandidatePolicy) -> str:
    return json.dumps(policy.as_dict(), indent=2, default=str)
