"""Phase 9 — Paper portfolio allocator (DESIGN-ONLY, DISABLED).

This module documents the *future* allocator that may, after extensive manual
review, open paper positions for candidates that have passed:

  - Phase 8B candidate validator
  - Phase 9 paper readiness validator
  - Data freshness gate
  - Net profit lock lab (positive net EV)

It does NOT open positions. It does NOT touch the exchange. It does NOT call
PaperTrader.open_position. It does NOT change leverage / margin / sizing /
slots configuration. The dataclass below is a *plan*; runtime activation is
explicitly disabled and protected by a hard guard.

Calling `simulate_allocations(...)` returns the would-be allocations as
research output, never as an action. There is no path from this module to
order placement.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .phase9_paper_readiness_validator import (
    PHASE9_READY,
    Phase9CandidateVerdict,
    Phase9PaperReadinessReport,
)


PAPER_ALLOCATOR_DISABLED_REASON = "phase9_paper_allocator_disabled_by_design_until_manual_review"


@dataclass(frozen=True)
class PaperAllocatorPolicy:
    """Frozen design constants for the future allocator."""
    max_simultaneous_paper_positions: int = 2
    one_position_per_symbol: bool = True
    max_exposure_per_side: int = 2
    max_exposure_per_correlated_group: int = 1
    correlated_groups: tuple[tuple[str, ...], ...] = (
        ("BTCUSDT", "ETHUSDT", "BNBUSDT"),
        ("SOLUSDT", "AVAXUSDT", "DOTUSDT"),
        ("ADAUSDT", "XRPUSDT", "LINKUSDT"),
        ("DOGEUSDT",),
    )
    require_net_profit_lock: bool = True
    require_phase9_ready: bool = True
    require_data_fresh: bool = True
    disabled: bool = True
    allow_runtime_activation: bool = False  # NEVER FLIP TO TRUE WITHOUT MANUAL REVIEW
    final_recommendation: str = "NO LIVE"


@dataclass
class PlannedAllocation:
    candidate_id: str
    symbol: str
    side: str
    weight_pct: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PaperAllocatorPlan:
    enabled: bool
    policy: PaperAllocatorPolicy
    planned_allocations: list[PlannedAllocation] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=lambda: [PAPER_ALLOCATOR_DISABLED_REASON])
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = "NO LIVE"

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "policy": asdict(self.policy),
            "planned_allocations": [item.as_dict() for item in self.planned_allocations],
            "blocked_reasons": list(self.blocked_reasons),
            "research_only": self.research_only,
            "paper_filter_enabled": self.paper_filter_enabled,
            "can_send_real_orders": self.can_send_real_orders,
            "final_recommendation": self.final_recommendation,
        }


def _candidate_passes_phase9(candidate: Phase9CandidateVerdict) -> bool:
    if candidate.phase9_decision != PHASE9_READY:
        return False
    if not candidate.data_freshness_ok:
        return False
    if candidate.policy_net_ev <= 0:
        return False
    if not candidate.delta_ev_positive:
        return False
    return True


def _correlated_group(policy: PaperAllocatorPolicy, symbol: str) -> int | None:
    for index, group in enumerate(policy.correlated_groups):
        if symbol in group:
            return index
    return None


def simulate_allocations(
    readiness_report: Phase9PaperReadinessReport,
    *,
    policy: PaperAllocatorPolicy | None = None,
) -> PaperAllocatorPlan:
    """Produce a *plan* (not an action). The allocator stays disabled."""
    active_policy = policy or PaperAllocatorPolicy()
    plan = PaperAllocatorPlan(enabled=False, policy=active_policy)
    if active_policy.disabled or not active_policy.allow_runtime_activation:
        plan.blocked_reasons = [PAPER_ALLOCATOR_DISABLED_REASON]
        # We can still populate the *planned* slots so a human reviewing the
        # readiness can see what the future allocator would have chosen.
    eligible = [c for c in readiness_report.candidates if _candidate_passes_phase9(c)]
    if not eligible:
        plan.blocked_reasons.append("no_phase9_ready_candidates")
        return plan
    side_count = {"LONG": 0, "SHORT": 0}
    used_symbols: set[str] = set()
    used_groups: set[int] = set()
    eligible.sort(key=lambda c: (c.policy_net_ev, c.policy_net_pf, c.trades), reverse=True)
    for candidate in eligible:
        if len(plan.planned_allocations) >= active_policy.max_simultaneous_paper_positions:
            break
        for symbol in candidate.symbols:
            if symbol in used_symbols and active_policy.one_position_per_symbol:
                continue
            group_index = _correlated_group(active_policy, symbol)
            if group_index is not None and group_index in used_groups:
                continue
            side = "LONG"  # The validator does not yet model per-symbol side
            if side_count[side] >= active_policy.max_exposure_per_side:
                continue
            plan.planned_allocations.append(PlannedAllocation(
                candidate_id=candidate.candidate_id,
                symbol=symbol,
                side=side,
                weight_pct=1.0 / max(1, active_policy.max_simultaneous_paper_positions),
                reason="phase9_ready_manual_review_only",
            ))
            used_symbols.add(symbol)
            if group_index is not None:
                used_groups.add(group_index)
            side_count[side] += 1
    return plan


def render_paper_allocator_text(plan: PaperAllocatorPlan) -> str:
    lines = [
        "PAPER PORTFOLIO ALLOCATOR (DESIGN-ONLY) START",
        f"enabled: {str(plan.enabled).lower()}",
        f"policy.disabled: {plan.policy.disabled}",
        f"policy.allow_runtime_activation: {plan.policy.allow_runtime_activation}",
        f"policy.max_simultaneous_paper_positions: {plan.policy.max_simultaneous_paper_positions}",
        f"policy.one_position_per_symbol: {plan.policy.one_position_per_symbol}",
        f"policy.max_exposure_per_side: {plan.policy.max_exposure_per_side}",
        f"policy.max_exposure_per_correlated_group: {plan.policy.max_exposure_per_correlated_group}",
        f"policy.require_net_profit_lock: {plan.policy.require_net_profit_lock}",
        f"policy.require_phase9_ready: {plan.policy.require_phase9_ready}",
        f"policy.require_data_fresh: {plan.policy.require_data_fresh}",
        "planned_allocations:",
    ]
    if not plan.planned_allocations:
        lines.append("- none")
    else:
        for item in plan.planned_allocations:
            lines.append(
                f"- candidate_id={item.candidate_id} symbol={item.symbol} "
                f"side={item.side} weight_pct={item.weight_pct:.3f} reason={item.reason}"
            )
    lines.append("blocked_reasons:")
    for reason in plan.blocked_reasons:
        lines.append(f"- {reason}")
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "activation: disabled",
        "final_recommendation: NO LIVE",
        "PAPER PORTFOLIO ALLOCATOR (DESIGN-ONLY) END",
    ])
    return "\n".join(lines)
