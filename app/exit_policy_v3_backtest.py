from __future__ import annotations

from typing import Any

from .exit_policy_v3 import POLICIES, _hit_from_return, simulate_exit_policy
from .operational_intelligence_utils import (
    FINAL_RECOMMENDATION,
    conservative_decision,
    edge_metrics,
    group_by_keys,
    load_operational_rows,
    safe_float_text,
    smoke_safety_lines,
)
from .utils import safe_float, safe_int


GROUP_KEYS = ("symbol", "side", "market_regime", "score_bucket", "strategy", "source")


class ExitPolicyV3Backtest:
    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        rows = load_operational_rows(self.db, hours=hours)
        results = [evaluate_group(group_rows, self.config, group_key=key) for key, group_rows in group_by_keys(rows, GROUP_KEYS).items()]
        results.sort(key=lambda row: (safe_float(row.get("improvement_score")), safe_float(row.get("dynamic_net_ev"))), reverse=True)
        counts: dict[str, int] = {}
        for row in results:
            counts[str(row.get("decision"))] = counts.get(str(row.get("decision")), 0) + 1
        return {
            "hours": hours,
            "groups": len(results),
            "decision_counts": counts,
            "best_exit_policies": results[:20],
            "research_only": True,
            "apply_automatically": False,
            "final_recommendation": FINAL_RECOMMENDATION,
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            "EXIT POLICY V3 BACKTEST START",
            f"hours: {payload['hours']}",
            f"groups: {payload['groups']}",
            f"decision_counts: {payload['decision_counts']}",
            "best_exit_policies:",
        ]
        if not payload["best_exit_policies"]:
            lines.append("- none")
        for row in payload["best_exit_policies"][:10]:
            lines.append(
                f"- {row['group_id']} policy={row['best_policy_id']} samples={row['sample_size']} "
                f"baseline_EV={safe_float_text(row['baseline_fixed_net_ev'])} dynamic_EV={safe_float_text(row['dynamic_net_ev'])} "
                f"improvement={safe_float_text(row['improvement_score'])} decision={row['decision']}"
            )
        lines.extend(["research_only: true", "apply_automatically: false", "final_recommendation: NO LIVE", "EXIT POLICY V3 BACKTEST END"])
        return "\n".join(lines)


def evaluate_group(rows: list[dict[str, Any]], config: Any | None = None, *, group_key: tuple[str, ...] | None = None) -> dict[str, Any]:
    baseline = edge_metrics(rows, config)
    best_policy = "fixed_tp_sl_baseline"
    best_rows = rows
    best_metrics = baseline
    premature = 0
    whipsaw = 0
    profit_locked = 0
    trailing_winners = 0
    for policy in POLICIES:
        simulated_rows = []
        policy_results = [simulate_exit_policy(row, policy, config) for row in rows]
        for row, result in zip(rows, policy_results):
            simulated_rows.append({**row, "return_pct": result.simulated_return_pct, "first_barrier_hit": _hit_from_return(result.simulated_return_pct)})
        metrics = edge_metrics(simulated_rows, config)
        if safe_float(metrics.get("net_EV")) > safe_float(best_metrics.get("net_EV")):
            best_policy = policy
            best_rows = simulated_rows
            best_metrics = metrics
            premature = sum(1 for item in policy_results if "DECAY" in item.simulated_exit_reason or "FLAT" in item.simulated_exit_reason)
            whipsaw = sum(1 for item in policy_results if "WHIPSAW" in item.simulated_exit_reason)
            profit_locked = sum(1 for item in policy_results if "PROFIT_LOCK" in item.simulated_exit_reason)
            trailing_winners = sum(1 for item in policy_results if "TRAILING" in item.simulated_exit_reason or "ADAPTIVE" in item.simulated_exit_reason)
    sample_size = len(rows)
    source = str(rows[0].get("source") if rows else "trade_signal")
    improvement = safe_float(best_metrics.get("net_EV")) - safe_float(baseline.get("net_EV"))
    decision = _decision(sample_size, baseline, best_metrics, source, improvement)
    group_id = "|".join(group_key or tuple(str(rows[0].get(key) or "NA") for key in GROUP_KEYS)) if rows else "empty"
    return {
        "group_id": group_id,
        "sample_size": sample_size,
        "baseline_fixed_net_ev": baseline["net_EV"],
        "baseline_fixed_net_pf": baseline["net_PF"],
        "baseline_time_pct": baseline["TIME"],
        "dynamic_net_ev": best_metrics["net_EV"],
        "dynamic_net_pf": best_metrics["net_PF"],
        "dynamic_time_pct": best_metrics["TIME"],
        "best_policy_id": best_policy,
        "tp_missed_recovered": max(0, safe_int(best_metrics.get("tp_count")) - safe_int(baseline.get("tp_count"))),
        "sl_avoided": max(0, safe_int(baseline.get("sl_count")) - safe_int(best_metrics.get("sl_count"))),
        "profit_locked_count": profit_locked,
        "trailing_winner_count": trailing_winners,
        "premature_exit_count": premature,
        "whipsaw_exit_count": whipsaw,
        "improvement_score": improvement,
        "confidence": best_metrics["confidence"],
        "decision": decision,
        "research_only": True,
    }


def _decision(samples: int, baseline: dict[str, Any], dynamic: dict[str, Any], source: str, improvement: float) -> str:
    base = conservative_decision(dynamic, source=source)
    if source == "market_probe":
        return "NEED_MORE_DATA" if improvement > 0 else "REJECT"
    if samples < 250:
        return "NEED_MORE_DATA"
    if improvement <= 0 or safe_float(dynamic.get("net_EV")) <= safe_float(baseline.get("net_EV")):
        return "REJECT"
    if safe_float(dynamic.get("drawdown_proxy")) > safe_float(baseline.get("drawdown_proxy")) * 1.5 and safe_float(baseline.get("drawdown_proxy")) > 0:
        return "WATCH_ONLY"
    if base == "SHADOW_CANDIDATE":
        return "SHADOW_EXIT_CANDIDATE"
    return "RESEARCH_POCKET" if base == "RESEARCH_POCKET" else "WATCH_ONLY"


def exit_policy_v3_backtest_smoke_text() -> str:
    trend_rows = [
        {"symbol": "ETHUSDT", "side": "SHORT", "market_regime": "TREND_DOWN", "score_bucket": "85-89", "strategy": "trend", "source": "trade_signal", "mfe": 2.0, "mae": 0.2, "return_pct": 0.2, "first_barrier_hit": "TP"}
        for _ in range(300)
    ]
    whipsaw_rows = [
        {"symbol": "BTCUSDT", "side": "LONG", "market_regime": "CHOPPY_MARKET", "score_bucket": "80-84", "strategy": "breakout", "source": "trade_signal", "mfe": 0.4, "mae": 0.7, "return_pct": -0.3, "first_barrier_hit": "SL"}
        for _ in range(300)
    ]
    low_sample = trend_rows[:20]
    probe = [{**row, "source": "market_probe"} for row in trend_rows]
    trend = evaluate_group(trend_rows)
    whipsaw = evaluate_group(whipsaw_rows)
    low = evaluate_group(low_sample)
    probe_result = evaluate_group(probe)
    checks = {
        "fixed_tp_loses_trend_and_trailing_improves": safe_float(trend["improvement_score"]) > 0,
        "trailing_too_close_generates_whipsaw_or_reject": whipsaw["decision"] in {"REJECT", "WATCH_ONLY"},
        "low_sample_not_promoted": low["decision"] == "NEED_MORE_DATA",
        "market_probe_not_actionable": probe_result["decision"] in {"NEED_MORE_DATA", "REJECT"},
        "research_only": all(item["research_only"] for item in (trend, whipsaw, low, probe_result)),
    }
    lines = ["EXIT POLICY V3 BACKTEST SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend(smoke_safety_lines())
    lines.extend([f"result: {'PASS' if all(checks.values()) else 'FAIL'}", "EXIT POLICY V3 BACKTEST SMOKE TEST END"])
    return "\n".join(lines)
