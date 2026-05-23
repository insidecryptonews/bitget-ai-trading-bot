"""Setup Key Trainer — DESIGN SKELETON for Phase 7.4B.

Learns which (symbol+side+regime+score_bucket+source+exit_policy)
combinations are noise vs. need-more-data vs. shadow-candidate.

NO RUNTIME HOOK. Builds on existing app/setup_key.py + signal_outcome_classifier.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


FINAL_RECOMMENDATION = "NO LIVE"
STATUS_DESIGN_ONLY = "DESIGN_ONLY_NOT_IMPLEMENTED"


@dataclass
class SetupKeyTrainerDesign:
    status: str = STATUS_DESIGN_ONLY
    labels_to_apply: list[str] = field(default_factory=lambda: [
        "BASURA",
        "INSUFFICIENT_DATA",
        "WATCH",
        "SHADOW_CANDIDATE",
        "PAPER_CANDIDATE_BLOCKED",
        "MARKET_PROBE_ONLY",
    ])
    inputs_required: list[str] = field(default_factory=lambda: [
        "signal_observations_with_setup_key",
        "signal_labels",
        "signal_outcomes_from_classifier",
        "cost_model_breakdown",
    ])
    rules_designed: list[str] = field(default_factory=lambda: [
        "market_probe_never_promoted",
        "min_samples_per_setup_threshold",
        "monthly_stability_check",
        "cost_sensitivity_022_and_025",
        "expected_move_to_cost_ratio_minimum",
    ])
    notes: list[str] = field(default_factory=lambda: [
        "implementation_partial_in_candidate_incubator_v2",
        "trainer_layer_planned_for_7_4b",
        "no_runtime_hook",
    ])
    research_only: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def design_summary() -> SetupKeyTrainerDesign:
    return SetupKeyTrainerDesign()


def render_design_text(result: SetupKeyTrainerDesign | None = None) -> str:
    r = result or design_summary()
    lines = ["SETUP KEY TRAINER DESIGN START"]
    lines.append(f"status: {r.status}")
    lines.append("labels_to_apply:")
    for l in r.labels_to_apply:
        lines.append(f"- {l}")
    lines.append("inputs_required:")
    for i in r.inputs_required:
        lines.append(f"- {i}")
    lines.append("rules_designed:")
    for rule in r.rules_designed:
        lines.append(f"- {rule}")
    lines.append("notes:")
    for note in r.notes:
        lines.append(f"- {note}")
    lines.append("research_only: true")
    lines.append("no_runtime_change: true")
    lines.append(f"final_recommendation: {r.final_recommendation}")
    lines.append("SETUP KEY TRAINER DESIGN END")
    return "\n".join(lines)
