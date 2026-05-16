from __future__ import annotations

from typing import Any

from .edge_hardening_utils import (
    DECISION_PAPER,
    DECISION_REJECT,
    DECISION_SHADOW,
    DECISION_WATCH,
    FINAL_NO_LIVE,
    apply_net_costs,
    cost_config,
    fetch_group_metrics,
    format_num,
    format_pct,
    since_iso,
)
from .utils import safe_float, safe_int


START = "EV SLIPPAGE CALIBRATION GATE START"
END = "EV SLIPPAGE CALIBRATION GATE END"


class EvSlippageCalibrationGate:
    """Net EV and slippage guard for research candidates."""

    GROUPS = ("symbol", "side", "market_regime", "score_bucket", "policy_id")

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        costs = cost_config(self.config)
        rows: list[dict[str, Any]] = []
        recent_rows = {
            (group, str(row.get("group_value") or "")): row
            for group in self.GROUPS
            for row in fetch_group_metrics(self.db, since=since_iso(max(1, min(6, hours))), group_key=group, limit=80, min_samples=1)
        }
        for group in self.GROUPS:
            for row in fetch_group_metrics(self.db, since=since_iso(hours), group_key=group, limit=80, min_samples=1):
                item = apply_net_costs(row, costs)
                recent = recent_rows.get((group, str(item.get("group_value") or "")), {})
                item["recent_samples"] = safe_int(recent.get("samples"))
                item["recent_pf"] = safe_float(recent.get("gross_pf"))
                item["deterioration"] = bool(item["recent_pf"] and item["recent_pf"] < safe_float(item.get("gross_PF")) * 0.65)
                item["calibration_score"] = _calibration_score(item)
                item["final_decision"] = _decision(item, costs)
                item["reason"] = _reason(item, costs)
                rows.append(item)
        rows.sort(key=lambda item: (safe_float(item.get("calibration_score")), safe_float(item.get("net_PF"))), reverse=True)
        return {"hours": max(1, int(hours or 24)), "candidates": rows[:60], "final_recommendation": FINAL_NO_LIVE}

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            "candidates:",
            *_lines(payload["candidates"]),
            "final_recommendation: NO LIVE",
            END,
        ])


def _calibration_score(row: dict[str, Any]) -> float:
    net_pf = min(3.0, safe_float(row.get("net_PF"))) / 3.0
    net_ev = max(-1.0, min(1.0, safe_float(row.get("net_EV")))) * 0.5 + 0.5
    sample = min(1.0, safe_int(row.get("samples")) / 1000.0)
    penalty = safe_float(row.get("time_ratio")) * 0.25 + safe_float(row.get("sl_ratio")) * 0.35
    if row.get("deterioration"):
        penalty += 0.25
    return max(0.0, min(1.0, (net_pf * 0.35 + net_ev * 0.30 + sample * 0.35) - penalty))


def _decision(row: dict[str, Any], costs: Any) -> str:
    if safe_int(row.get("samples")) < costs.min_samples:
        return DECISION_WATCH
    if safe_float(row.get("net_EV")) <= 0:
        return DECISION_REJECT
    if safe_float(row.get("net_PF")) < costs.min_net_pf:
        return DECISION_REJECT
    if safe_float(row.get("estimated_slippage_cost")) > safe_float(row.get("net_EV")) and safe_float(row.get("net_EV")) < 0.10:
        return DECISION_REJECT
    if row.get("deterioration"):
        return DECISION_REJECT
    if safe_float(row.get("time_ratio")) > costs.max_time_ratio and safe_float(row.get("tp_ratio")) < costs.min_tp_ratio:
        return DECISION_REJECT
    if safe_float(row.get("calibration_score")) < 0.45:
        return DECISION_SHADOW
    return DECISION_PAPER


def _reason(row: dict[str, Any], costs: Any) -> str:
    if safe_int(row.get("samples")) < costs.min_samples:
        return "sample_too_small"
    if safe_float(row.get("net_EV")) <= 0:
        return "net_ev_negative"
    if safe_float(row.get("net_PF")) < costs.min_net_pf:
        return "net_pf_below_min"
    if row.get("deterioration"):
        return "recent_deterioration"
    if safe_float(row.get("time_ratio")) > costs.max_time_ratio:
        return "time_high_tp_low"
    return "ev_slippage_calibrated"


def _lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- {row.get('group_key')}={row.get('group_value')} samples={row.get('samples')} recent_samples={row.get('recent_samples')} "
            f"gross_EV={format_num(row.get('gross_EV'), 4)} net_EV={format_num(row.get('net_EV'), 4)} "
            f"gross_PF={format_num(row.get('gross_PF'))} net_PF={format_num(row.get('net_PF'))} "
            f"TP={format_pct(row.get('tp_ratio'))} SL={format_pct(row.get('sl_ratio'))} TIME={format_pct(row.get('time_ratio'))} "
            f"calibration_score={format_num(row.get('calibration_score'))} decision={row.get('final_decision')} reason={row.get('reason')}"
        )
        for row in rows[:20]
    ]
