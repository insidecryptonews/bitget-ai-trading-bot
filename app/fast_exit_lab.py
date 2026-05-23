"""Fast Exit Lab — DESIGN SKELETON for Phase 7.4B.

Studies offline whether closing positions earlier improves edge when:
  - signal score decays below threshold,
  - opposite-side signal appears,
  - no follow-through after N bars,
  - spread widens beyond threshold,
  - BTC/ETH alignment flips against the position.

NO RUNTIME HOOK. Pure research lab. Depends on OHLCV 5m + signal_path_metrics
bar-by-bar data to be honest.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


FINAL_RECOMMENDATION = "NO LIVE"
STATUS_DESIGN_ONLY = "DESIGN_ONLY_NOT_IMPLEMENTED"


@dataclass
class FastExitLabResult:
    status: str = STATUS_DESIGN_ONLY
    triggers_designed: list[str] = field(default_factory=lambda: [
        "score_decay_below_threshold",
        "opposite_side_signal_present",
        "no_follow_through_after_N_bars",
        "spread_widening",
        "btc_eth_alignment_flip",
    ])
    required_inputs: list[str] = field(default_factory=lambda: [
        "ohlcv_5m_persisted",
        "signal_path_metrics_bar_path",
        "rolling_score_history",
        "btc_eth_alignment_per_bar",
        "spread_history_per_bar",
    ])
    notes: list[str] = field(default_factory=lambda: [
        "lab_not_implemented_in_phase_7_4a",
        "no_runtime_hook",
        "must_compare_vs_baseline_to_avoid_false_improvement",
    ])
    research_only: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def design_summary() -> FastExitLabResult:
    return FastExitLabResult()


def render_design_text(result: FastExitLabResult | None = None) -> str:
    r = result or design_summary()
    lines = ["FAST EXIT LAB DESIGN START"]
    lines.append(f"status: {r.status}")
    lines.append("triggers_designed:")
    for t in r.triggers_designed:
        lines.append(f"- {t}")
    lines.append("required_inputs:")
    for inp in r.required_inputs:
        lines.append(f"- {inp}")
    lines.append("notes:")
    for note in r.notes:
        lines.append(f"- {note}")
    lines.append("research_only: true")
    lines.append("no_runtime_change: true")
    lines.append(f"final_recommendation: {r.final_recommendation}")
    lines.append("FAST EXIT LAB DESIGN END")
    return "\n".join(lines)
