"""Profit Lock Exit Lab — DESIGN SKELETON for Phase 7.4B.

This module is intentionally a minimal stub. The Phase 7.4A sprint covers
the foundation (audits + cleanups). The lab itself will be filled in once
OHLCV 5m is persisted and signal_path_metrics has bar_path data.

Goal of the lab when implemented:
  Compare baseline exit vs:
    - move stop to entry once MFE crosses X%,
    - trailing ATR-based stop,
    - signal-decay exit (close when score drops below threshold).

Metrics to compare:
  net_EV, net_PF, TP/SL/TIME mix, failed_winners count,
  MFE capture ratio, max drawdown.

THIS MODULE IS NOT WIRED TO RUNTIME. Paper trader and exec layer untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


FINAL_RECOMMENDATION = "NO LIVE"
STATUS_DESIGN_ONLY = "DESIGN_ONLY_NOT_IMPLEMENTED"


@dataclass
class ProfitLockLabResult:
    status: str = STATUS_DESIGN_ONLY
    policies_designed: list[str] = field(default_factory=lambda: [
        "baseline_tp1_tp2",
        "breakeven_after_mfe_050",
        "breakeven_after_mfe_080",
        "trailing_atr_1_2",
        "signal_decay_exit",
    ])
    required_inputs: list[str] = field(default_factory=lambda: [
        "ohlcv_5m_persisted",
        "signal_path_metrics_bar_path",
        "cost_model_breakdown",
    ])
    notes: list[str] = field(default_factory=lambda: [
        "lab_not_implemented_in_phase_7_4a",
        "depends_on_ohlcv_5m_foundation_track_d",
        "no_runtime_hook",
    ])
    research_only: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def design_summary() -> ProfitLockLabResult:
    return ProfitLockLabResult()


def render_design_text(result: ProfitLockLabResult | None = None) -> str:
    r = result or design_summary()
    lines = ["PROFIT LOCK EXIT LAB DESIGN START"]
    lines.append(f"status: {r.status}")
    lines.append("policies_designed:")
    for p in r.policies_designed:
        lines.append(f"- {p}")
    lines.append("required_inputs:")
    for inp in r.required_inputs:
        lines.append(f"- {inp}")
    lines.append("notes:")
    for note in r.notes:
        lines.append(f"- {note}")
    lines.append("research_only: true")
    lines.append("no_runtime_change: true")
    lines.append(f"final_recommendation: {r.final_recommendation}")
    lines.append("PROFIT LOCK EXIT LAB DESIGN END")
    return "\n".join(lines)
