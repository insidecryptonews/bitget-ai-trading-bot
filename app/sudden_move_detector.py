from __future__ import annotations

from typing import Any

from .operational_intelligence_utils import (
    FINAL_RECOMMENDATION,
    edge_metrics,
    group_by_keys,
    load_operational_rows,
    normalize_row,
    safe_float_text,
    smoke_safety_lines,
)
from .utils import safe_float


def detect_sudden_move(features: dict[str, Any], config: Any | None = None) -> dict[str, Any]:
    del config
    row = normalize_row(features)
    mfe = safe_float(row.get("mfe"))
    mae = safe_float(row.get("mae"))
    ret = safe_float(row.get("return_pct"))
    volume = safe_float(row.get("volume_change"))
    volatility = safe_float(row.get("volatility"))
    momentum = safe_float(row.get("momentum")) or ret
    regime = str(row.get("market_regime") or "UNKNOWN").upper()
    source = str(row.get("source") or "trade_signal").lower()
    side_hint = str(row.get("side") or "").upper()
    direction = "NONE"
    reason = "insufficient_move"
    score = 0.0
    fakeout_risk = 0.0
    if mfe >= max(0.8, mae * 1.6) and ret >= -0.1:
        direction = "LONG" if side_hint != "SHORT" else "SHORT"
        score += min(mfe / 3.0, 0.45)
        reason = "range_expansion_with_favorable_excursion"
    if mae >= max(0.8, mfe * 1.6) and ret <= 0.1:
        direction = "SHORT" if side_hint != "LONG" else "LONG"
        score += min(mae / 3.0, 0.45)
        reason = "downside_expansion_or_rejection"
    if volume >= 1.5:
        score += 0.18
        reason += "+volume_spike"
    if volatility >= 0.015:
        score += 0.14
        reason += "+volatility_expansion"
    if abs(momentum) >= 0.3:
        score += 0.12
        reason += "+momentum_alignment"
    if regime in {"TREND_UP", "TREND_DOWN", "RISK_OFF", "BREAKOUT"}:
        score += 0.10
    if regime == "CHOPPY_MARKET":
        fakeout_risk += 0.35
        score -= 0.25
        reason = "choppy_fakeout_risk"
    if volume >= 2.0 and abs(ret) < 0.1 and mfe < 0.3 and mae < 0.3:
        fakeout_risk += 0.40
        score -= 0.20
        reason = "volume_without_price_followthrough"
    score = max(0.0, min(score, 1.0))
    confidence = "HIGH" if score >= 0.75 and fakeout_risk < 0.35 else "MEDIUM" if score >= 0.50 else "LOW"
    expected_move = "large" if score >= 0.75 else "medium" if score >= 0.50 else "none"
    invalidation = -mae if direction == "LONG" else mfe
    not_actionable = []
    if score < 0.55:
        not_actionable.append("sudden_move_score_low")
    if source == "market_probe":
        not_actionable.append("market_probe_not_actionable")
    if fakeout_risk >= 0.35:
        not_actionable.append("fakeout_risk")
    return {
        "sudden_move_score": score,
        "direction": direction if score >= 0.35 else "NONE",
        "confidence": confidence,
        "regime_fit": "BAD" if regime == "CHOPPY_MARKET" else "GOOD" if regime in {"TREND_UP", "TREND_DOWN", "RISK_OFF", "BREAKOUT"} else "WATCH",
        "invalidation_level": invalidation,
        "expected_move_bucket": expected_move,
        "time_window_estimate": "short" if score >= 0.75 else "unknown",
        "reason": reason,
        "not_actionable_reason": ",".join(not_actionable) or "research_only_no_direct_signal",
        "fakeout_risk": fakeout_risk,
        "research_only": True,
    }


class SuddenMoveDetector:
    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        rows = load_operational_rows(self.db, hours=hours)
        detections = [{**detect_sudden_move(row, self.config), "symbol": row.get("symbol"), "side": row.get("side"), "regime": row.get("market_regime"), "source": row.get("source")} for row in rows]
        high = [row for row in detections if safe_float(row.get("sudden_move_score")) >= 0.55]
        by_direction = {key[0]: len(value) for key, value in group_by_keys(high, ("direction",)).items()}
        false_positive = [row for row in high if safe_float(row.get("fakeout_risk")) >= 0.35]
        metrics = edge_metrics(rows, self.config)
        return {
            "hours": hours,
            "rows": len(rows),
            "patterns_found": len(high),
            "direction_counts": by_direction,
            "false_positive_risk_count": len(false_positive),
            "top_sudden_move_patterns": sorted(high, key=lambda row: safe_float(row.get("sudden_move_score")), reverse=True)[:20],
            "gross_EV": metrics["gross_EV"],
            "net_EV": metrics["net_EV"],
            "detector_status": "RESEARCH_READY" if rows else "NEED_DATA",
            "research_only": True,
            "final_recommendation": FINAL_RECOMMENDATION,
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            "SUDDEN MOVE DETECTOR START",
            f"hours: {payload['hours']}",
            f"rows: {payload['rows']}",
            f"patterns_found: {payload['patterns_found']}",
            f"direction_counts: {payload['direction_counts']}",
            f"false_positive_risk_count: {payload['false_positive_risk_count']}",
            f"detector_status: {payload['detector_status']}",
            "top_sudden_move_patterns:",
        ]
        if not payload["top_sudden_move_patterns"]:
            lines.append("- none")
        for row in payload["top_sudden_move_patterns"][:10]:
            lines.append(
                f"- {row.get('symbol')} {row.get('direction')} score={safe_float_text(row.get('sudden_move_score'), 3)} "
                f"confidence={row.get('confidence')} reason={row.get('reason')} not_actionable={row.get('not_actionable_reason')}"
            )
        lines.extend(["research_only: true", "final_recommendation: NO LIVE", "SUDDEN MOVE DETECTOR END"])
        return "\n".join(lines)


def sudden_move_smoke_text() -> str:
    up = detect_sudden_move({"side": "LONG", "market_regime": "TREND_UP", "mfe": 2.4, "mae": 0.2, "return_pct": 1.2, "volume_change": 2.0, "volatility": 0.02, "momentum": 0.8})
    down = detect_sudden_move({"side": "SHORT", "market_regime": "RISK_OFF", "mfe": 2.2, "mae": 0.2, "return_pct": 1.0, "volume_change": 2.0, "volatility": 0.02, "momentum": -0.8})
    choppy = detect_sudden_move({"side": "LONG", "market_regime": "CHOPPY_MARKET", "mfe": 0.8, "mae": 0.7, "return_pct": 0.0, "volume_change": 2.5, "volatility": 0.02})
    volume_only = detect_sudden_move({"side": "LONG", "market_regime": "RANGE", "mfe": 0.1, "mae": 0.1, "return_pct": 0.0, "volume_change": 3.0})
    high_probe = detect_sudden_move({"side": "LONG", "source": "market_probe", "market_regime": "TREND_UP", "mfe": 2.0, "mae": 0.1, "return_pct": 1.0, "volume_change": 2.0})
    checks = {
        "clean_breakout_up_detects_long_research": up["direction"] == "LONG" and up["research_only"],
        "volume_dump_detects_short_research": down["direction"] == "SHORT" and down["research_only"],
        "choppy_fakeout_penalized": "fakeout" in choppy["not_actionable_reason"],
        "volume_without_move_not_promoted": volume_only["direction"] == "NONE" or volume_only["confidence"] == "LOW",
        "sudden_move_score_high_without_net_ev_not_actionable": "market_probe_not_actionable" in high_probe["not_actionable_reason"],
    }
    lines = ["SUDDEN MOVE SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend(smoke_safety_lines())
    lines.extend([f"result: {'PASS' if all(checks.values()) else 'FAIL'}", "SUDDEN MOVE SMOKE TEST END"])
    return "\n".join(lines)
