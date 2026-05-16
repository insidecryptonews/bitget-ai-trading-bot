from __future__ import annotations

from typing import Any

from .edge_hardening_utils import FINAL_NO_LIVE, apply_net_costs, cost_config, fetch_group_metrics, format_num, format_pct, since_iso
from .utils import safe_float, safe_int


START = "CANDIDATE RANKING START"
END = "CANDIDATE RANKING END"


class CandidateRanking:
    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        costs = cost_config(self.config)
        rows = [
            apply_net_costs(row, costs)
            for row in fetch_group_metrics(self.db, since=since_iso(hours), group_key="policy_id", limit=100, min_samples=1)
        ]
        ranked = []
        for row in rows:
            score, reason = _score(row, costs)
            item = dict(row)
            item["ranking_score"] = score
            item["reason"] = reason
            item["decision"] = _decision(item, costs)
            ranked.append(item)
        ranked.sort(key=lambda item: safe_float(item.get("ranking_score")), reverse=True)
        return {
            "hours": max(1, int(hours or 24)),
            "top_candidates": [row for row in ranked if row.get("decision") == "PAPER_CANDIDATE"][:5],
            "watch_list": [row for row in ranked if row.get("decision") in {"WATCH_ONLY", "SHADOW_CANDIDATE"}][:10],
            "reject_list": [row for row in ranked if row.get("decision") == "REJECT"][:10],
            "final_recommendation": FINAL_NO_LIVE,
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            "top_5_candidates:",
            *_lines(payload["top_candidates"]),
            "watch_list:",
            *_lines(payload["watch_list"]),
            "reject_list:",
            *_lines(payload["reject_list"]),
            "final_recommendation: NO LIVE",
            END,
        ])


def _score(row: dict[str, Any], costs: Any) -> tuple[float, str]:
    sample_score = min(1.0, safe_int(row.get("samples")) / max(costs.min_samples, 1))
    net_pf_score = min(1.0, safe_float(row.get("net_PF")) / max(costs.min_net_pf * 2.0, 1.0))
    ev_score = max(0.0, min(1.0, safe_float(row.get("net_EV")) + 0.5))
    tp_score = min(1.0, safe_float(row.get("tp_ratio")) / max(costs.min_tp_ratio, 0.001))
    penalty = safe_float(row.get("time_ratio")) * 0.30 + safe_float(row.get("sl_ratio")) * 0.35
    score = max(0.0, (sample_score * 0.20 + net_pf_score * 0.30 + ev_score * 0.25 + tp_score * 0.25) - penalty)
    if safe_int(row.get("samples")) < costs.min_samples:
        return score, "sample_too_small"
    if safe_float(row.get("net_EV")) <= 0:
        return score, "net_ev_not_positive"
    if safe_float(row.get("net_PF")) < costs.min_net_pf:
        return score, "net_pf_below_min"
    if safe_float(row.get("time_ratio")) > costs.max_time_ratio:
        return score, "time_death_risk"
    return score, "multi_metric_candidate"


def _decision(row: dict[str, Any], costs: Any) -> str:
    if row.get("reason") in {"net_ev_not_positive", "net_pf_below_min"}:
        return "REJECT"
    if row.get("reason") == "sample_too_small":
        return "WATCH_ONLY"
    if safe_float(row.get("ranking_score")) >= 0.55:
        return "PAPER_CANDIDATE"
    if safe_float(row.get("ranking_score")) >= 0.35:
        return "SHADOW_CANDIDATE"
    return "REJECT"


def _lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- policy_id={row.get('group_value')} score={format_num(row.get('ranking_score'))} "
            f"samples={row.get('samples')} net_PF={format_num(row.get('net_PF'))} net_EV={format_num(row.get('net_EV'), 4)} "
            f"TP={format_pct(row.get('tp_ratio'))} TIME={format_pct(row.get('time_ratio'))} "
            f"decision={row.get('decision')} reason={row.get('reason')}"
        )
        for row in rows
    ]
