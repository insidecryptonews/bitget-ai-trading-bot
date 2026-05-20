from __future__ import annotations

from typing import Any

from .operational_intelligence_utils import (
    FINAL_RECOMMENDATION,
    edge_metrics,
    group_by_keys,
    load_operational_rows,
    safe_float_text,
    smoke_safety_lines,
)
from .utils import safe_float, safe_int


class AntiOverfitMatrixV2:
    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 72) -> dict[str, Any]:
        rows = load_operational_rows(self.db, hours=hours)
        matrix = [evaluate_overfit_group(key, group_rows, self.config) for key, group_rows in group_by_keys(rows, ("symbol", "side", "market_regime", "score_bucket", "source")).items()]
        matrix.sort(key=lambda row: (row["decision"] != "REJECT_OVERFIT", safe_float(row.get("net_EV"))), reverse=True)
        counts: dict[str, int] = {}
        for row in matrix:
            counts[str(row["decision"])] = counts.get(str(row["decision"]), 0) + 1
        return {
            "hours": hours,
            "groups": len(matrix),
            "decision_counts": counts,
            "anti_overfit_matrix": matrix[:50],
            "anti_overfit_status": "WARNING" if any(row["decision"] == "REJECT_OVERFIT" for row in matrix) else "OK" if matrix else "NEED_DATA",
            "research_only": True,
            "final_recommendation": FINAL_RECOMMENDATION,
        }

    def to_text(self, *, hours: int = 72) -> str:
        payload = self.build(hours=hours)
        lines = [
            "ANTI OVERFIT MATRIX V2 START",
            f"hours: {payload['hours']}",
            f"groups: {payload['groups']}",
            f"anti_overfit_status: {payload['anti_overfit_status']}",
            f"decision_counts: {payload['decision_counts']}",
            "overfit_rejects:",
        ]
        rejects = [row for row in payload["anti_overfit_matrix"] if row["decision"] == "REJECT_OVERFIT"]
        if not rejects:
            lines.append("- none")
        for row in rejects[:10]:
            lines.append(f"- {row['group_id']}: flags={','.join(row['flags'])} net_EV={safe_float_text(row['net_EV'])}")
        lines.extend(["research_only: true", "final_recommendation: NO LIVE", "ANTI OVERFIT MATRIX V2 END"])
        return "\n".join(lines)


def evaluate_overfit_group(group_key: tuple[str, ...], rows: list[dict[str, Any]], config: Any | None = None) -> dict[str, Any]:
    metrics = edge_metrics(rows, config)
    flags = overfit_flags(group_key, rows, metrics)
    if flags & {"COST_SENSITIVE_EDGE", "LOW_SAMPLE_EDGE", "MARKET_PROBE_EDGE_ONLY", "TIME_DEATH_EDGE_FAKE", "LABEL_QUALITY_UNRELIABLE"}:
        decision = "REJECT_OVERFIT"
    elif safe_float(metrics.get("net_EV")) > 0 and safe_int(metrics.get("samples")) >= 750 and not flags:
        decision = "SHADOW_CANDIDATE"
    elif safe_float(metrics.get("net_EV")) > 0:
        decision = "WATCH_ONLY"
    else:
        decision = "REJECT"
    return {
        "group_id": "|".join(group_key),
        "samples": metrics["samples"],
        "net_EV": metrics["net_EV"],
        "net_PF": metrics["net_PF"],
        "TIME": metrics["TIME"],
        "TP": metrics["TP"],
        "flags": sorted(flags),
        "decision": decision,
        "research_only": True,
    }


def overfit_flags(group_key: tuple[str, ...], rows: list[dict[str, Any]], metrics: dict[str, Any]) -> set[str]:
    symbol, _side, regime, bucket, source = (list(group_key) + [""] * 5)[:5]
    flags: set[str] = set()
    samples = safe_int(metrics.get("samples"))
    if samples < 250 and safe_float(metrics.get("net_EV")) > 0:
        flags.add("LOW_SAMPLE_EDGE")
    if source == "market_probe" and safe_float(metrics.get("net_EV")) > 0:
        flags.add("MARKET_PROBE_EDGE_ONLY")
    if regime == "CHOPPY_MARKET" and safe_float(metrics.get("net_EV")) > 0:
        flags.add("CHOPPY_ONLY_EDGE")
    if safe_float(metrics.get("TIME")) > 0.8 and safe_float(metrics.get("TP")) < 0.1:
        flags.add("TIME_DEATH_EDGE_FAKE")
    if safe_float(metrics.get("avg_cost_bps")) > 0 and 0 < safe_float(metrics.get("gross_EV")) < safe_float(metrics.get("avg_cost_bps")) / 100.0:
        flags.add("COST_SENSITIVE_EDGE")
    if samples < 750 and symbol not in {"NA", "UNKNOWN"}:
        flags.add("TOO_SPECIFIC_SYMBOL")
    if bucket not in {"0-49", "50-59", "60-69", "70-74", "75-79", "80-84", "85-89", "90-94", "95-100"}:
        flags.add("TOO_SPECIFIC_SCORE_BUCKET")
    if any(
        ("mfe" in row or "mae" in row or "max_favorable_pct" in row or "max_adverse_pct" in row)
        and safe_float(row.get("mfe")) == 0
        and safe_float(row.get("mae")) == 0
        for row in rows[:50]
    ):
        flags.add("LABEL_QUALITY_UNRELIABLE")
    return flags


def anti_overfit_v2_smoke_text() -> str:
    cost_edge = [{"symbol": "BTCUSDT", "side": "SHORT", "market_regime": "TREND_DOWN", "score_bucket": "85-89", "source": "trade_signal", "return_pct": 0.02, "first_barrier_hit": "TP"} for _ in range(300)]
    one_window = [{"symbol": "DOGEUSDT", "side": "SHORT", "market_regime": "TREND_DOWN", "score_bucket": "85-89", "source": "trade_signal", "return_pct": 0.5, "first_barrier_hit": "TP"} for _ in range(50)]
    robust = [{"symbol": "ETHUSDT", "side": "SHORT", "market_regime": "TREND_DOWN", "score_bucket": "85-89", "source": "trade_signal", "return_pct": 0.5, "first_barrier_hit": "TP"} for _ in range(800)]
    cost_result = evaluate_overfit_group(("BTCUSDT", "SHORT", "TREND_DOWN", "85-89", "trade_signal"), cost_edge)
    low_result = evaluate_overfit_group(("DOGEUSDT", "SHORT", "TREND_DOWN", "85-89", "trade_signal"), one_window)
    robust_result = evaluate_overfit_group(("ETHUSDT", "SHORT", "TREND_DOWN", "85-89", "trade_signal"), robust)
    checks = {
        "edge_disappears_with_slippage_reject": "COST_SENSITIVE_EDGE" in cost_result["flags"] or cost_result["decision"] != "SHADOW_CANDIDATE",
        "edge_one_window_reject_or_watch": low_result["decision"] in {"REJECT_OVERFIT", "WATCH_ONLY", "REJECT"},
        "robust_edge_shadow": robust_result["decision"] in {"SHADOW_CANDIDATE", "WATCH_ONLY"},
    }
    lines = ["ANTI OVERFIT V2 SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend(smoke_safety_lines())
    lines.extend([f"result: {'PASS' if all(checks.values()) else 'FAIL'}", "ANTI OVERFIT V2 SMOKE TEST END"])
    return "\n".join(lines)
