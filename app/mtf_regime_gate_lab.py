"""MTF Regime Gate Lab — DESIGN SKELETON for Phase 7.4B.

Evaluates whether adding per-regime gates (BTC/ETH alignment, RISK_OFF block,
RANGE block, CHOPPY block, TREND_DOWN side-restrict) improves net_EV without
killing sample size.

NO RUNTIME HOOK. Compares hypothetical filters offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


FINAL_RECOMMENDATION = "NO LIVE"
STATUS_DESIGN_ONLY = "DESIGN_ONLY_NOT_IMPLEMENTED"


@dataclass
class MtfRegimeGateResult:
    status: str = STATUS_DESIGN_ONLY
    gates_designed: list[str] = field(default_factory=lambda: [
        "btc_15m_alignment_required",
        "eth_1h_alignment_required",
        "block_risk_off_longs",
        "block_range_both",
        "block_choppy_both",
        "trend_down_short_only",
    ])
    metrics_to_compare: list[str] = field(default_factory=lambda: [
        "net_ev_pct",
        "time_pct_reduction",
        "false_positive_reduction",
        "sample_size_remaining",
    ])
    notes: list[str] = field(default_factory=lambda: [
        "lab_not_implemented_in_phase_7_4a",
        "no_runtime_hook",
        "evaluation_must_use_setup_key_grouping_for_apples_to_apples",
    ])
    research_only: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def design_summary() -> MtfRegimeGateResult:
    return MtfRegimeGateResult()


def render_design_text(result: MtfRegimeGateResult | None = None) -> str:
    r = result or design_summary()
    lines = ["MTF REGIME GATE LAB DESIGN START"]
    lines.append(f"status: {r.status}")
    lines.append("gates_designed:")
    for g in r.gates_designed:
        lines.append(f"- {g}")
    lines.append("metrics_to_compare:")
    for m in r.metrics_to_compare:
        lines.append(f"- {m}")
    lines.append("notes:")
    for note in r.notes:
        lines.append(f"- {note}")
    lines.append("research_only: true")
    lines.append("no_runtime_change: true")
    lines.append(f"final_recommendation: {r.final_recommendation}")
    lines.append("MTF REGIME GATE LAB DESIGN END")
    return "\n".join(lines)
