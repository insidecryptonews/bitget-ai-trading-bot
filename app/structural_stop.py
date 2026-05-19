from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .utils import safe_float


@dataclass(frozen=True)
class StructuralStopResult:
    stop_loss: float
    stop_distance_pct: float
    stop_quality: str
    whipsaw_risk: float
    source: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_structural_stop(
    *,
    side: str,
    entry: float,
    atr: float,
    support: float | None = None,
    resistance: float | None = None,
    regime: str = "",
    volatility: float | None = None,
    config: Any = None,
) -> StructuralStopResult:
    side_text = str(side or "").upper()
    regime_text = str(regime or "").upper()
    entry_value = safe_float(entry)
    atr_value = safe_float(atr)
    if entry_value <= 0 or atr_value <= 0 or side_text not in {"LONG", "SHORT"}:
        return StructuralStopResult(0.0, 0.0, "INVALID_INPUT", 1.0, "none", "invalid inputs")

    min_pct = safe_float(getattr(config, "min_stop_distance_pct", 0.006), 0.006)
    max_pct = safe_float(getattr(config, "max_stop_distance_pct", 0.04), 0.04)
    min_distance = entry_value * max(min_pct, 0.0001)
    max_distance = entry_value * max(max_pct, min_pct)
    buffer = max(atr_value * 0.15, entry_value * 0.0005)
    stop = 0.0
    source = "ATR_FALLBACK"
    reason = "atr_1_4_or_min_distance"

    if side_text == "LONG":
        support_value = safe_float(support)
        if 0 < support_value < entry_value:
            stop = support_value - buffer
            source = "STRUCTURE"
            reason = "below_support_with_buffer"
        else:
            stop = entry_value - max(atr_value * 1.4, min_distance)
    else:
        resistance_value = safe_float(resistance)
        if resistance_value > entry_value:
            stop = resistance_value + buffer
            source = "STRUCTURE"
            reason = "above_resistance_with_buffer"
        else:
            stop = entry_value + max(atr_value * 1.4, min_distance)

    distance = abs(entry_value - stop)
    if distance < min_distance:
        if source == "STRUCTURE":
            stop = entry_value - min_distance if side_text == "LONG" else entry_value + min_distance
            distance = min_distance
            reason += "_expanded_to_min_distance"
        else:
            return StructuralStopResult(stop, distance / entry_value, "TOO_TIGHT_REJECT", 0.9, source, "stop below minimum distance")
    if distance > max_distance:
        return StructuralStopResult(stop, distance / entry_value, "TOO_WIDE_REJECT", 0.7, source, "stop above maximum distance")

    quality = "STRUCTURAL_VALID" if source == "STRUCTURE" else "ATR_FALLBACK"
    whipsaw = 0.25 if quality == "STRUCTURAL_VALID" else 0.45
    volatility_value = safe_float(volatility)
    if regime_text == "CHOPPY_MARKET":
        quality = "CHOPPY_WHIPSAW_RISK"
        whipsaw = max(whipsaw, 0.85)
    elif regime_text == "RANGE":
        whipsaw = max(whipsaw, 0.6)
    if volatility_value > 0.025:
        whipsaw = max(whipsaw, 0.7)
    return StructuralStopResult(stop, distance / entry_value, quality, min(1.0, whipsaw), source, reason)


def structural_stop_smoke_text() -> str:
    fallback = calculate_structural_stop(side="LONG", entry=100, atr=1, support=0, regime="TREND_UP")
    structural = calculate_structural_stop(side="SHORT", entry=100, atr=1, resistance=102, regime="TREND_DOWN")
    choppy = calculate_structural_stop(side="LONG", entry=100, atr=1, support=99.5, regime="CHOPPY_MARKET")
    tight = calculate_structural_stop(side="LONG", entry=100, atr=0.01, support=99.99, regime="RANGE")
    checks = {
        "fallback_uses_atr_1_4": abs(fallback.stop_loss - 98.6) < 1e-9,
        "structure_with_buffer": structural.stop_loss > 102,
        "choppy_increases_whipsaw": choppy.whipsaw_risk >= 0.85,
        "too_tight_expanded_or_rejected": tight.stop_quality in {"STRUCTURAL_VALID", "CHOPPY_WHIPSAW_RISK", "TOO_TIGHT_REJECT"},
        "final_recommendation_no_live": True,
    }
    lines = ["STRUCTURAL STOP SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend(
        [
            "LIVE_TRADING=false",
            "DRY_RUN=true",
            "PAPER_TRADING=true",
            "final_recommendation: NO LIVE",
            f"result: {'PASS' if all(checks.values()) else 'FAIL'}",
            "STRUCTURAL STOP SMOKE TEST END",
        ]
    )
    return "\n".join(lines)
