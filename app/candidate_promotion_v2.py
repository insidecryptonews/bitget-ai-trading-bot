from __future__ import annotations

from typing import Any

from .anti_overfit_matrix_v2 import evaluate_overfit_group
from .operational_intelligence_utils import (
    FINAL_RECOMMENDATION,
    conservative_decision,
    edge_metrics,
    group_by_keys,
    load_operational_rows,
    safe_float_text,
    smoke_safety_lines,
)
from .walk_forward_validator import validate_group
from .utils import safe_float, safe_int


GROUP_KEYS = ("symbol", "side", "market_regime", "score_bucket", "source")


class CandidatePromotionV2:
    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        rows = load_operational_rows(self.db, hours=hours)
        candidates = [promote_group(key, group_rows, self.config) for key, group_rows in group_by_keys(rows, GROUP_KEYS).items()]
        candidates.sort(key=lambda row: (row["state"] in {"PAPER_CANDIDATE_DISABLED", "SHADOW_CANDIDATE", "RESEARCH_POCKET"}, safe_float(row.get("net_EV"))), reverse=True)
        counts: dict[str, int] = {}
        for row in candidates:
            counts[str(row["state"])] = counts.get(str(row["state"]), 0) + 1
        return {
            "hours": hours,
            "candidates": len(candidates),
            "candidate_promotion_state_counts": counts,
            "candidate_promotion_states": candidates[:50],
            "paper_candidate_disabled": [row for row in candidates if row["state"] == "PAPER_CANDIDATE_DISABLED"][:10],
            "paper_filter_enabled": False,
            "live_allowed": False,
            "research_only": True,
            "final_recommendation": FINAL_RECOMMENDATION,
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            "CANDIDATE PROMOTION V2 START",
            f"hours: {payload['hours']}",
            f"candidates: {payload['candidates']}",
            f"candidate_promotion_state_counts: {payload['candidate_promotion_state_counts']}",
            "top_states:",
        ]
        if not payload["candidate_promotion_states"]:
            lines.append("- none")
        for row in payload["candidate_promotion_states"][:12]:
            lines.append(
                f"- {row['candidate_id']}: state={row['state']} samples={row['samples']} net_EV={safe_float_text(row['net_EV'])} "
                f"wf={row['walk_forward_decision']} anti={row['anti_overfit_decision']} reason={row['reason']}"
            )
        lines.extend([
            "paper_filter_enabled=false",
            "live_allowed=false",
            "research_only: true",
            "final_recommendation: NO LIVE",
            "CANDIDATE PROMOTION V2 END",
        ])
        return "\n".join(lines)


def promote_group(group_key: tuple[str, ...], rows: list[dict[str, Any]], config: Any | None = None) -> dict[str, Any]:
    metrics = edge_metrics(rows, config)
    wf = validate_group(rows, config)
    anti = evaluate_overfit_group(group_key, rows, config)
    source = str(group_key[4] if len(group_key) > 4 else rows[0].get("source") if rows else "trade_signal")
    base = conservative_decision(metrics, source=source)
    state, reason = _state(metrics, wf, anti, source, base)
    return {
        "candidate_id": "|".join(group_key),
        "symbol": group_key[0],
        "side": group_key[1],
        "market_regime": group_key[2],
        "score_bucket": group_key[3],
        "source": source,
        "samples": metrics["samples"],
        "TP": metrics["TP"],
        "SL": metrics["SL"],
        "TIME": metrics["TIME"],
        "net_EV": metrics["net_EV"],
        "net_PF": metrics["net_PF"],
        "gross_PF": metrics["gross_PF"],
        "walk_forward_decision": wf["decision"],
        "anti_overfit_decision": anti["decision"],
        "anti_overfit_flags": anti["flags"],
        "state": state,
        "reason": reason,
        "paper_filter_enabled": False,
        "live_allowed": False,
        "research_only": True,
    }


def _state(metrics: dict[str, Any], wf: dict[str, Any], anti: dict[str, Any], source: str, base: str) -> tuple[str, str]:
    samples = safe_int(metrics.get("samples"))
    net_ev = safe_float(metrics.get("net_EV"))
    net_pf = safe_float(metrics.get("net_PF"))
    time_ratio = safe_float(metrics.get("TIME"))
    if source == "market_probe":
        return ("NEED_MORE_DATA_NOT_ACTIONABLE", "market_probe_not_actionable") if net_ev > 0 else ("REJECT_BAD_EDGE", "market_probe_negative_edge")
    if samples < 250:
        return ("NEED_MORE_DATA", "sample_too_small_positive") if net_ev > 0 else ("REJECT_BAD_EDGE", "sample_too_small_negative")
    if time_ratio > 0.80:
        return "REJECT_TIME_DEATH", "high_time_death"
    if net_ev <= 0 or net_pf < 1.05:
        return "REJECT_BAD_EDGE", "net_ev_or_net_pf_failed"
    if str(anti.get("decision")) == "REJECT_OVERFIT":
        return "REJECT_OVERFIT", "anti_overfit_flags"
    if str(wf.get("decision")) in {"OVERFIT_REJECT", "REJECT"}:
        return "REJECT_OVERFIT", "walk_forward_failed"
    if samples < 750:
        return "RESEARCH_POCKET", "positive_but_needs_more_out_of_sample"
    if str(wf.get("decision")) == "SHADOW_CANDIDATE" and str(anti.get("decision")) == "SHADOW_CANDIDATE" and base == "SHADOW_CANDIDATE":
        return "PAPER_CANDIDATE_DISABLED", "all_gates_prelim_passed_but_activation_disabled"
    return "SHADOW_CANDIDATE", "preliminary_positive_shadow_only"


def candidate_promotion_v2_smoke_text() -> str:
    low = [{"symbol": "DOGEUSDT", "side": "SHORT", "market_regime": "TREND_DOWN", "score_bucket": "85-89", "source": "trade_signal", "return_pct": 0.5, "first_barrier_hit": "TP"} for _ in range(40)]
    probe = [{**row, "source": "market_probe"} for row in low]
    stable = [{"symbol": "ETHUSDT", "side": "SHORT", "market_regime": "TREND_DOWN", "score_bucket": "85-89", "source": "trade_signal", "return_pct": 0.5, "first_barrier_hit": "TP"} for _ in range(900)]
    bad = [{"symbol": "SOLUSDT", "side": "LONG", "market_regime": "RANGE", "score_bucket": "90-94", "source": "trade_signal", "return_pct": -0.5, "first_barrier_hit": "SL"} for _ in range(300)]
    low_result = promote_group(("DOGEUSDT", "SHORT", "TREND_DOWN", "85-89", "trade_signal"), low)
    probe_result = promote_group(("DOGEUSDT", "SHORT", "TREND_DOWN", "85-89", "market_probe"), probe)
    stable_result = promote_group(("ETHUSDT", "SHORT", "TREND_DOWN", "85-89", "trade_signal"), stable)
    bad_result = promote_group(("SOLUSDT", "LONG", "RANGE", "90-94", "trade_signal"), bad)
    checks = {
        "micro_positive_low_sample_needs_more_data": low_result["state"] == "NEED_MORE_DATA",
        "market_probe_positive_not_actionable": probe_result["state"] == "NEED_MORE_DATA_NOT_ACTIONABLE",
        "stable_setup_can_reach_disabled_candidate": stable_result["state"] in {"PAPER_CANDIDATE_DISABLED", "SHADOW_CANDIDATE"},
        "bad_edge_rejected": bad_result["state"] == "REJECT_BAD_EDGE",
        "paper_filter_remains_disabled": not stable_result["paper_filter_enabled"],
    }
    lines = ["CANDIDATE PROMOTION V2 SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend(smoke_safety_lines())
    lines.extend([f"result: {'PASS' if all(checks.values()) else 'FAIL'}", "CANDIDATE PROMOTION V2 SMOKE TEST END"])
    return "\n".join(lines)
