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


START = "ANTI OVERFIT GATE START"
END = "ANTI OVERFIT GATE END"


class AntiOverfitGate:
    """Strict research gate for policy candidates. It never enables paper/live."""

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        costs = cost_config(self.config)
        raw_rows = fetch_group_metrics(self.db, since=since_iso(hours), group_key="policy_id", limit=80, min_samples=1)
        recent_rows = fetch_group_metrics(self.db, since=since_iso(max(1, min(6, hours))), group_key="policy_id", limit=120, min_samples=1)
        current_by_id = {str(row.get("group_value") or ""): row for row in raw_rows}
        recent_by_id = {str(row.get("group_value") or ""): row for row in recent_rows}
        rows = [
            apply_net_costs(row, costs)
            for row in raw_rows
        ]
        decisions = []
        for row in rows:
            policy_id = str(row.get("group_value") or "")
            current = current_by_id.get(policy_id, {})
            recent = recent_by_id.get(policy_id, {})
            train_pf = safe_float(current.get("gross_pf"))
            validation_pf = train_pf
            recent_pf = safe_float(recent.get("gross_pf"))
            strict = self.evaluate_row(row, train_pf=train_pf, validation_pf=validation_pf, recent_pf=recent_pf)
            item = dict(row)
            item.update(strict)
            item["train_pf"] = train_pf
            item["validation_pf"] = validation_pf
            item["recent_pf"] = recent_pf
            decisions.append(item)
        decisions.sort(key=lambda item: (_decision_rank(str(item.get("final_decision"))), safe_float(item.get("net_PF"))), reverse=True)
        return {
            "hours": max(1, int(hours or 24)),
            "candidates": decisions[:40],
            "final_recommendation": FINAL_NO_LIVE,
        }

    def evaluate_row(self, row: dict[str, Any], *, train_pf: float, validation_pf: float, recent_pf: float) -> dict[str, Any]:
        costs = cost_config(self.config)
        samples = safe_int(row.get("samples"))
        net_pf = safe_float(row.get("net_PF"))
        tp_ratio = safe_float(row.get("tp_ratio"))
        sl_ratio = safe_float(row.get("sl_ratio"))
        time_ratio = safe_float(row.get("time_ratio"))
        deterioration = recent_pf > 0 and validation_pf > 0 and recent_pf < validation_pf * 0.65
        reason = "multi_module_confirmed"
        decision = DECISION_PAPER
        if samples < costs.min_samples:
            decision, reason = DECISION_WATCH, "sample_too_small"
        if validation_pf and validation_pf < costs.min_net_pf:
            decision, reason = DECISION_REJECT, "validation_pf_low"
        if deterioration:
            decision, reason = DECISION_REJECT, "recent_deterioration"
        if net_pf < costs.min_net_pf:
            decision, reason = DECISION_REJECT, "net_pf_below_min"
        if time_ratio > costs.max_time_ratio and tp_ratio < costs.min_tp_ratio:
            decision, reason = DECISION_REJECT, "time_death_extreme"
        if sl_ratio > 0.15 and tp_ratio < 0.03:
            decision, reason = DECISION_REJECT, "sl_high_tp_low"
        if str(row.get("symbol") or "").upper() == "BNBUSDT" and sl_ratio > tp_ratio:
            decision, reason = DECISION_REJECT, "bnb_sl_high_tp_low"
        if str(row.get("market_regime") or "").upper() == "RISK_OFF" and (tp_ratio < costs.min_tp_ratio or time_ratio >= 1.0):
            decision, reason = DECISION_REJECT, "risk_off_low_tp"
        if decision == DECISION_PAPER and (train_pf < costs.min_net_pf or recent_pf < costs.min_net_pf):
            decision, reason = DECISION_SHADOW, "needs_multi_window_confirmation"
        return {
            "final_decision": decision,
            "reason": reason,
            "deterioration_detected": deterioration,
            "walk_forward_stability": _stability(train_pf, validation_pf, recent_pf),
        }

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

def _stability(train_pf: float, validation_pf: float, recent_pf: float) -> float:
    pfs = [value for value in (train_pf, validation_pf, recent_pf) if value > 0]
    if len(pfs) < 2:
        return 0.0
    spread = max(pfs) - min(pfs)
    return max(0.0, min(1.0, 1.0 - spread / max(max(pfs), 1.0)))


def _decision_rank(decision: str) -> int:
    return {DECISION_PAPER: 4, DECISION_SHADOW: 3, DECISION_WATCH: 2, DECISION_REJECT: 1}.get(decision, 0)


def _lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- policy_id={row.get('group_value')} samples={row.get('samples')} "
            f"train_pf={format_num(row.get('train_pf'))} validation_pf={format_num(row.get('validation_pf'))} "
            f"recent_pf={format_num(row.get('recent_pf'))} net_PF={format_num(row.get('net_PF'))} "
            f"TP={format_pct(row.get('tp_ratio'))} SL={format_pct(row.get('sl_ratio'))} TIME={format_pct(row.get('time_ratio'))} "
            f"decision={row.get('final_decision')} reason={row.get('reason')}"
        )
        for row in rows[:20]
    ]
