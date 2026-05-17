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
        recent = {
            str(row.get("group_value") or ""): row
            for row in fetch_group_metrics(self.db, since=since_iso(max(1, min(6, hours))), group_key="policy_id", limit=120, min_samples=1)
        }
        pre_move = _pre_move_context(self.config, self.db, hours)
        ranked = []
        for row in rows:
            rec = recent.get(str(row.get("group_value") or ""), {})
            row["recent_pf"] = safe_float(rec.get("gross_pf"))
            row["recent_samples"] = safe_int(rec.get("samples"))
            row.update(_pre_move_for_policy(row, pre_move))
            score, reason = _score(row, costs)
            item = dict(row)
            item["ranking_score"] = score
            item["reason"] = reason
            item["decision"] = _decision(item, costs)
            ranked.append(item)
        ranked.sort(key=lambda item: safe_float(item.get("ranking_score")), reverse=True)
        return {
            "hours": max(1, int(hours or 24)),
            "status": "OK" if any(row.get("decision") == "PAPER_CANDIDATE" for row in ranked) else "NO_VALID_CANDIDATES",
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
            f"status: {payload['status']}",
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
    penalty = safe_float(row.get("time_ratio")) * 0.55 + safe_float(row.get("sl_ratio")) * 0.45
    policy_id = str(row.get("group_value") or "").upper()
    recent_pf = safe_float(row.get("recent_pf"))
    if recent_pf and recent_pf < safe_float(row.get("gross_PF")) * 0.65:
        penalty += 0.30
    if "_RISK_OFF_" in policy_id or policy_id.endswith("_RISK_OFF"):
        penalty += 0.20
    if "_LONG_" in policy_id and safe_float(row.get("tp_ratio")) < 0.02:
        penalty += 0.25
    if policy_id.endswith("_70-79") or "_70-79" in policy_id:
        penalty += 0.15 if safe_float(row.get("time_ratio")) > 0.85 else 0.0
    penalty += safe_float(row.get("trap_risk")) * 0.20
    penalty += safe_float(row.get("time_death_pattern_risk")) * 0.25
    pre_move_bonus = min(0.20, safe_float(row.get("pre_move_score")) * 0.10)
    score = max(0.0, (sample_score * 0.20 + net_pf_score * 0.30 + ev_score * 0.25 + tp_score * 0.25) - penalty)
    score += pre_move_bonus
    if safe_int(row.get("samples")) < costs.min_samples:
        return score, "sample_too_small"
    if safe_float(row.get("net_EV")) <= 0:
        return score, "net_ev_not_positive"
    if safe_float(row.get("net_PF")) < costs.min_net_pf:
        return score, "net_pf_below_min"
    if safe_float(row.get("time_ratio")) > costs.max_time_ratio:
        return score, "time_death_risk"
    if safe_float(row.get("trap_risk")) >= 0.35:
        return score, "trap_pattern_risk"
    if safe_float(row.get("time_death_pattern_risk")) >= 0.50:
        return score, "time_death_pattern_risk"
    if recent_pf and recent_pf < safe_float(row.get("gross_PF")) * 0.65:
        return score, "recent_deterioration"
    return score, "multi_metric_candidate"


def _decision(row: dict[str, Any], costs: Any) -> str:
    if row.get("reason") in {"net_ev_not_positive", "net_pf_below_min", "trap_pattern_risk", "time_death_pattern_risk"}:
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
            f"pre_move={format_num(row.get('pre_move_score'))} trap={format_pct(row.get('trap_risk'))} "
            f"decision={row.get('decision')} reason={row.get('reason')}"
        )
        for row in rows
    ]


def _pre_move_context(config: Any, db: Any, hours: int) -> dict[str, Any]:
    try:
        from .pre_move_pattern_miner import PreMovePatternMiner
        from .pre_move_similarity_scanner import PreMoveSimilarityScanner

        miner = PreMovePatternMiner(config, db).build(hours=hours)
        scanner = PreMoveSimilarityScanner(config, db).build(hours=max(1, min(6, hours)))
    except Exception:
        return {"patterns": {}, "similarity": {}}
    patterns: dict[str, dict[str, Any]] = {}
    for row in miner.get("patterns", []):
        policy_id = f"policy_{row.get('symbol')}_{row.get('direction')}_{row.get('regime')}_{row.get('score_bucket')}"
        patterns[policy_id.upper()] = row
    similarity: dict[str, dict[str, Any]] = {}
    for row in scanner.get("matches", []):
        key = str(row.get("symbol") or "").upper()
        current = similarity.get(key)
        if current is None or safe_float(row.get("similarity_score")) > safe_float(current.get("similarity_score")):
            similarity[key] = row
    return {"patterns": patterns, "similarity": similarity}


def _pre_move_for_policy(row: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    policy = str(row.get("group_value") or "").upper()
    parts = policy.replace("POLICY_", "", 1).split("_")
    symbol = parts[0].upper() if parts else ""
    pattern = context.get("patterns", {}).get(policy, {})
    similar = context.get("similarity", {}).get(symbol, {})
    decision = str(pattern.get("decision") or "")
    similarity_score = safe_float(similar.get("similarity_score"))
    long_score = 1.0 if decision == "LONG_PATTERN_CANDIDATE" else 0.0
    short_score = 1.0 if decision == "SHORT_PATTERN_CANDIDATE" else 0.0
    trap = safe_float(pattern.get("fakeout_rate"))
    time_risk = 1.0 if str(pattern.get("decision") or "") == "TIME_DEATH_PATTERN" else safe_float(pattern.get("TIME_after_signal"))
    return {
        "pre_move_score": max(long_score, short_score, similarity_score),
        "long_pattern_score": long_score,
        "short_pattern_score": short_score,
        "trap_risk": trap,
        "time_death_pattern_risk": time_risk,
        "similarity_score": similarity_score,
        "event_memory_support": bool(pattern or similar),
    }
