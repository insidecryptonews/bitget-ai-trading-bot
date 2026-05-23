"""Momentum Burst 5m Lab — DESIGN SKELETON for Phase 7.4B.

Wrapper that hooks the existing app/momentum_burst_lab.py into 5m OHLCV when
the table is populated. Until OHLCV 5m is persisted, this lab returns
NEED_DATA explicitly.

NO RUNTIME HOOK. Research only.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


FINAL_RECOMMENDATION = "NO LIVE"
STATUS_DESIGN_ONLY = "DESIGN_ONLY_NEED_OHLCV_5M"


@dataclass
class MomentumBurst5mDesign:
    status: str = STATUS_DESIGN_ONLY
    timeframes_planned: list[str] = field(default_factory=lambda: ["1m", "5m"])
    features_required: list[str] = field(default_factory=lambda: [
        "return_1m_3m_5m_8m_15m",
        "acceleration_return_1m_minus_return_5m_over_5",
        "volume_spike_relative_volume",
        "candle_body_pct",
        "upper_lower_wick_rejection",
        "atr_normalized",
        "btc_eth_alignment",
        "expected_move_to_cost_ratio",
    ])
    blocking_conditions: list[str] = field(default_factory=lambda: [
        "late_entry_if_return_5m_above_2x_threshold",
        "exhaustion_if_wick_against_direction",
        "fee_toxic_if_expected_move_lt_3x_cost",
    ])
    depends_on: list[str] = field(default_factory=lambda: [
        "ohlcv_5m_persisted_track_d",
        "cost_model_validated_track_f",
    ])
    notes: list[str] = field(default_factory=lambda: [
        "implementation_exists_in_app_momentum_burst_lab",
        "5m_specialisation_pending_after_track_d",
        "no_runtime_hook",
    ])
    research_only: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def design_summary() -> MomentumBurst5mDesign:
    return MomentumBurst5mDesign()


def render_design_text(result: MomentumBurst5mDesign | None = None) -> str:
    r = result or design_summary()
    lines = ["MOMENTUM BURST 5M LAB DESIGN START"]
    lines.append(f"status: {r.status}")
    lines.append("timeframes_planned:")
    for t in r.timeframes_planned:
        lines.append(f"- {t}")
    lines.append("features_required:")
    for f in r.features_required:
        lines.append(f"- {f}")
    lines.append("blocking_conditions:")
    for b in r.blocking_conditions:
        lines.append(f"- {b}")
    lines.append("depends_on:")
    for dep in r.depends_on:
        lines.append(f"- {dep}")
    lines.append("notes:")
    for note in r.notes:
        lines.append(f"- {note}")
    lines.append("research_only: true")
    lines.append("no_runtime_change: true")
    lines.append(f"final_recommendation: {r.final_recommendation}")
    lines.append("MOMENTUM BURST 5M LAB DESIGN END")
    return "\n".join(lines)
