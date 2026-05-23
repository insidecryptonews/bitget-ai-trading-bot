"""Net EV Trainer — DESIGN SKELETON for Phase 7.4B.

Ranks setups by realistic net_EV, penalising:
  - high TIME%,
  - small sample,
  - market_probe-only origin,
  - cost sensitivity (failing at 0.22% / 0.25% stress),
  - overfit (single-month carry > 60%).

NO RUNTIME HOOK. Operates over signal_outcomes + candidate_incubator_v2 output.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


FINAL_RECOMMENDATION = "NO LIVE"
STATUS_DESIGN_ONLY = "DESIGN_ONLY_NOT_IMPLEMENTED"


@dataclass
class NetEvTrainerDesign:
    status: str = STATUS_DESIGN_ONLY
    ranking_inputs: list[str] = field(default_factory=lambda: [
        "candidate_incubator_v2_setup_metrics",
        "signal_outcomes_summary",
        "cost_model_breakdown",
        "monthly_walk_forward",
    ])
    penalty_rules: list[str] = field(default_factory=lambda: [
        "time_pct_above_0_85_penalty",
        "samples_below_min_penalty",
        "market_probe_only_zero_credit",
        "cost_sensitivity_022_failure_penalty",
        "cost_sensitivity_025_failure_penalty",
        "single_month_carry_above_0_6_penalty",
    ])
    output_design: list[str] = field(default_factory=lambda: [
        "ranked_setups_by_robustness_score",
        "promotion_recommendation_only_no_activation",
        "final_recommendation_always_NO_LIVE",
    ])
    notes: list[str] = field(default_factory=lambda: [
        "trainer_designed_but_not_executed_in_7_4a",
        "no_runtime_hook",
        "never_auto_activate_paper_filter",
    ])
    research_only: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def design_summary() -> NetEvTrainerDesign:
    return NetEvTrainerDesign()


def render_design_text(result: NetEvTrainerDesign | None = None) -> str:
    r = result or design_summary()
    lines = ["NET EV TRAINER DESIGN START"]
    lines.append(f"status: {r.status}")
    lines.append("ranking_inputs:")
    for i in r.ranking_inputs:
        lines.append(f"- {i}")
    lines.append("penalty_rules:")
    for p in r.penalty_rules:
        lines.append(f"- {p}")
    lines.append("output_design:")
    for o in r.output_design:
        lines.append(f"- {o}")
    lines.append("notes:")
    for note in r.notes:
        lines.append(f"- {note}")
    lines.append("research_only: true")
    lines.append("no_runtime_change: true")
    lines.append(f"final_recommendation: {r.final_recommendation}")
    lines.append("NET EV TRAINER DESIGN END")
    return "\n".join(lines)
