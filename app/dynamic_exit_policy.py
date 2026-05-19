from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .utils import safe_float


@dataclass(frozen=True)
class DynamicExitProposal:
    research_only: bool
    current_fixed_exit: dict[str, Any]
    dynamic_exit_candidate: dict[str, Any]
    reason: str
    expected_holding_risk: str
    time_death_risk: str
    regime_fit: str
    trailing_candidate: bool
    prefer_no_trade: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def propose_dynamic_tp_sl(
    *,
    symbol: str,
    side: str,
    regime: str,
    entry: float,
    atr: float,
    support: float | None = None,
    resistance: float | None = None,
    volatility: float | None = None,
    score: float = 0.0,
    context: dict[str, Any] | None = None,
) -> DynamicExitProposal:
    del symbol, support, resistance, context
    side_text = str(side or "").upper()
    regime_text = str(regime or "UNKNOWN").upper()
    entry_value = safe_float(entry)
    atr_value = safe_float(atr)
    volatility_value = safe_float(volatility)
    score_value = safe_float(score)
    risk_unit = max(atr_value * 1.4, entry_value * 0.006) if entry_value > 0 else 0.0
    current_tp1_r = 1.6
    current_tp2_r = 2.4
    tp1_r = 1.5
    tp2_r = 2.0
    trailing = False
    prefer_no_trade = False
    reason = "range_or_default_research_policy"
    holding_risk = "MEDIUM"
    time_death_risk = "MEDIUM"
    regime_fit = "NEUTRAL"

    if "BREAKOUT" in regime_text or "SUDDEN" in regime_text:
        tp1_r, tp2_r, trailing = 2.5, 4.0, True
        reason = "breakout_shadow_expansion"
        holding_risk = "HIGH"
        regime_fit = "GOOD"
    elif regime_text in {"TREND_UP", "TREND_DOWN"}:
        tp1_r, tp2_r, trailing = 2.0, 3.5, True
        reason = "trend_shadow_expansion"
        holding_risk = "MEDIUM"
        regime_fit = "GOOD" if (regime_text == "TREND_UP" and side_text == "LONG") or (regime_text == "TREND_DOWN" and side_text == "SHORT") else "WEAK"
    elif regime_text == "RISK_OFF" and side_text == "SHORT":
        tp1_r, tp2_r, trailing = 2.0, 3.0, True
        reason = "risk_off_short_shadow_expansion"
        holding_risk = "MEDIUM"
        regime_fit = "GOOD"
    elif regime_text == "RANGE":
        tp1_r, tp2_r, trailing = 1.4, 1.9, False
        reason = "range_uses_closer_targets"
        holding_risk = "LOW"
        time_death_risk = "MEDIUM"
        regime_fit = "FAIR"
    elif regime_text == "CHOPPY_MARKET":
        tp1_r, tp2_r, trailing = 1.2, 1.6, False
        prefer_no_trade = True
        reason = "choppy_prefers_no_trade"
        holding_risk = "HIGH"
        time_death_risk = "HIGH"
        regime_fit = "BAD"

    if volatility_value > 0.025:
        holding_risk = "HIGH"
    if score_value < 72:
        regime_fit = "WEAK" if regime_fit != "BAD" else regime_fit

    current = _levels(side_text, entry_value, risk_unit, current_tp1_r, current_tp2_r)
    dynamic = _levels(side_text, entry_value, risk_unit, tp1_r, tp2_r)
    dynamic.update(
        {
            "tp1_r": tp1_r,
            "tp2_r": tp2_r,
            "trailing_candidate": trailing,
            "apply_automatically": False,
            "activation": "DISABLED_SHADOW_ONLY",
        }
    )
    current.update({"tp1_r": current_tp1_r, "tp2_r": current_tp2_r})
    return DynamicExitProposal(
        research_only=True,
        current_fixed_exit=current,
        dynamic_exit_candidate=dynamic,
        reason=reason,
        expected_holding_risk=holding_risk,
        time_death_risk=time_death_risk,
        regime_fit=regime_fit,
        trailing_candidate=trailing,
        prefer_no_trade=prefer_no_trade,
    )


def dynamic_exit_policy_smoke_text() -> str:
    trend = propose_dynamic_tp_sl(symbol="ETHUSDT", side="SHORT", regime="TREND_DOWN", entry=100, atr=1, score=85)
    range_result = propose_dynamic_tp_sl(symbol="BTCUSDT", side="LONG", regime="RANGE", entry=100, atr=1, score=80)
    choppy = propose_dynamic_tp_sl(symbol="DOGEUSDT", side="LONG", regime="CHOPPY_MARKET", entry=100, atr=1, score=70)
    checks = {
        "trend_uses_wider_tp": trend.dynamic_exit_candidate["tp1_r"] == 2.0 and trend.dynamic_exit_candidate["tp2_r"] == 3.5,
        "range_uses_realistic_tp": 1.3 <= range_result.dynamic_exit_candidate["tp1_r"] <= 1.5,
        "choppy_prefers_no_trade": choppy.prefer_no_trade,
        "research_only": trend.research_only and range_result.research_only and choppy.research_only,
        "final_recommendation_no_live": True,
    }
    lines = ["DYNAMIC EXIT POLICY SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend(
        [
            "LIVE_TRADING=false",
            "DRY_RUN=true",
            "PAPER_TRADING=true",
            "final_recommendation: NO LIVE",
            f"result: {'PASS' if all(checks.values()) else 'FAIL'}",
            "DYNAMIC EXIT POLICY SMOKE TEST END",
        ]
    )
    return "\n".join(lines)


def _levels(side: str, entry: float, risk_unit: float, tp1_r: float, tp2_r: float) -> dict[str, Any]:
    if entry <= 0 or risk_unit <= 0:
        return {"tp1": 0.0, "tp2": 0.0}
    if side == "SHORT":
        return {"tp1": entry - risk_unit * tp1_r, "tp2": entry - risk_unit * tp2_r}
    return {"tp1": entry + risk_unit * tp1_r, "tp2": entry + risk_unit * tp2_r}
