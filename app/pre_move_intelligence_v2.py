from __future__ import annotations

from typing import Any

from .operational_intelligence_utils import (
    FINAL_RECOMMENDATION,
    conservative_decision,
    edge_metrics,
    group_by_keys,
    load_operational_rows,
    safe_float_text,
    smoke_safety_lines,
)
from .sudden_move_detector import detect_sudden_move
from .utils import safe_float


class PreMoveIntelligenceV2:
    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        rows = load_operational_rows(self.db, hours=hours)
        event_rows = []
        for row in rows:
            event = classify_pre_move_event(row)
            detection = detect_sudden_move(row, self.config)
            if event["event_type"] != "NO_EVENT" or safe_float(detection.get("sudden_move_score")) >= 0.55:
                event_rows.append({**row, **event, **{f"sudden_{k}": v for k, v in detection.items()}})
        groups = []
        for key, group_rows in group_by_keys(event_rows, ("symbol", "side", "market_regime", "score_bucket", "event_type")).items():
            metrics = edge_metrics(group_rows, self.config)
            fakeout_rate = sum(1 for row in group_rows if row.get("quality") in {"FAKEOUT", "CHOPPY_NOISE"}) / max(len(group_rows), 1)
            decision = _decision(metrics, fakeout_rate, str(group_rows[0].get("source") if group_rows else "trade_signal"))
            groups.append({
                "pattern_id": "|".join(key),
                "symbol": key[0],
                "side": key[1],
                "regime": key[2],
                "score_bucket": key[3],
                "event_type": key[4],
                "samples": metrics["samples"],
                "net_EV": metrics["net_EV"],
                "net_PF": metrics["net_PF"],
                "fakeout_rate": fakeout_rate,
                "quality": dominant([str(row.get("quality")) for row in group_rows]),
                "decision": decision,
            })
        groups.sort(key=lambda row: (safe_float(row.get("net_EV")), -safe_float(row.get("fakeout_rate"))), reverse=True)
        long_patterns = [row for row in groups if row.get("side") == "LONG"]
        short_patterns = [row for row in groups if row.get("side") == "SHORT"]
        false_positive = [row for row in groups if safe_float(row.get("fakeout_rate")) >= 0.35 or row.get("decision") == "REJECT"]
        return {
            "hours": hours,
            "rows": len(rows),
            "patterns_found": len(groups),
            "top_pre_move_patterns": groups[:20],
            "long_vs_short_edge": {
                "long_patterns": len(long_patterns),
                "short_patterns": len(short_patterns),
                "long_avg_net_EV": sum(safe_float(row.get("net_EV")) for row in long_patterns) / max(len(long_patterns), 1),
                "short_avg_net_EV": sum(safe_float(row.get("net_EV")) for row in short_patterns) / max(len(short_patterns), 1),
            },
            "symbol_specific_patterns": groups[:20],
            "regime_specific_patterns": _regime_summary(groups),
            "false_positive_patterns": false_positive[:20],
            "no_valid_patterns": not any(row.get("decision") in {"RESEARCH_POCKET", "SHADOW_CANDIDATE"} for row in groups),
            "research_only": True,
            "final_recommendation": FINAL_RECOMMENDATION,
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            "PRE MOVE INTELLIGENCE V2 START",
            f"hours: {payload['hours']}",
            f"rows: {payload['rows']}",
            f"patterns_found: {payload['patterns_found']}",
            f"no_valid_patterns: {str(payload['no_valid_patterns']).lower()}",
            f"long_vs_short_edge: {payload['long_vs_short_edge']}",
            "top_pre_move_patterns:",
        ]
        if not payload["top_pre_move_patterns"]:
            lines.append("- none")
        for row in payload["top_pre_move_patterns"][:10]:
            lines.append(
                f"- {row['pattern_id']}: samples={row['samples']} net_EV={safe_float_text(row['net_EV'])} "
                f"fakeout={safe_float_text(row['fakeout_rate'], 3)} decision={row['decision']}"
            )
        lines.extend(["research_only: true", "final_recommendation: NO LIVE", "PRE MOVE INTELLIGENCE V2 END"])
        return "\n".join(lines)


def classify_pre_move_event(row: dict[str, Any]) -> dict[str, str]:
    mfe = safe_float(row.get("mfe"))
    mae = abs(safe_float(row.get("mae")))
    side = str(row.get("side") or "UNKNOWN").upper()
    regime = str(row.get("market_regime") or "UNKNOWN").upper()
    quality = "UNKNOWN"
    event = "NO_EVENT"
    if mfe >= 1.0 and mfe >= mae * 1.8:
        event = "CLEAN_BREAKOUT_UP" if side == "LONG" else "CLEAN_BREAKOUT_DOWN"
        quality = "CLEAN_MOVE" if regime != "CHOPPY_MARKET" else "DIRTY_MOVE"
    elif mae >= 1.0 and mae >= mfe * 1.8:
        event = "REVERSAL_DOWN" if side == "LONG" else "REVERSAL_UP"
        quality = "FAKEOUT"
    elif mfe >= 0.6 and mae >= 0.6:
        event = "FAILED_BREAKOUT"
        quality = "CHOPPY_NOISE" if regime == "CHOPPY_MARKET" else "DIRTY_MOVE"
    return {"event_type": event, "quality": quality}


def _decision(metrics: dict[str, Any], fakeout_rate: float, source: str) -> str:
    if fakeout_rate >= 0.35:
        return "REJECT"
    base = conservative_decision(metrics, source=source)
    if base == "SHADOW_CANDIDATE":
        return "RESEARCH_POCKET"
    return base


def _regime_summary(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = []
    by_regime: dict[str, list[dict[str, Any]]] = {}
    for row in groups:
        by_regime.setdefault(str(row.get("regime") or "unknown"), []).append(row)
    for regime, rows in by_regime.items():
        summary.append({
            "regime": regime,
            "patterns": len(rows),
            "avg_net_EV": sum(safe_float(row.get("net_EV")) for row in rows) / max(len(rows), 1),
            "rejected": sum(1 for row in rows if str(row.get("decision")).startswith("REJECT")),
        })
    return sorted(summary, key=lambda row: safe_float(row.get("avg_net_EV")), reverse=True)


def dominant(values: list[str]) -> str:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts, key=counts.get) if counts else "UNKNOWN"


def pre_move_v2_smoke_text() -> str:
    clean = [{"symbol": "ETHUSDT", "side": "SHORT", "market_regime": "TREND_DOWN", "score_bucket": "85-89", "event_type": "CLEAN_BREAKOUT_DOWN", "mfe": 1.5, "mae": 0.2, "return_pct": 0.8, "first_barrier_hit": "TP", "source": "trade_signal"} for _ in range(300)]
    fake = [{"symbol": "SOLUSDT", "side": "LONG", "market_regime": "CHOPPY_MARKET", "score_bucket": "90-94", "mfe": 0.7, "mae": 1.4, "return_pct": -0.5, "first_barrier_hit": "SL", "source": "trade_signal"} for _ in range(300)]
    clean_metrics = edge_metrics(clean)
    fake_metrics = edge_metrics(fake)
    checks = {
        "false_positive_patterns_rejected": _decision(fake_metrics, 0.8, "trade_signal") == "REJECT",
        "clean_patterns_can_be_research_pocket": _decision(clean_metrics, 0.0, "trade_signal") in {"RESEARCH_POCKET", "SHADOW_CANDIDATE"},
        "long_and_short_evaluated_separately": classify_pre_move_event(clean[0])["event_type"] != classify_pre_move_event(fake[0])["event_type"],
    }
    lines = ["PRE MOVE V2 SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend(smoke_safety_lines())
    lines.extend([f"result: {'PASS' if all(checks.values()) else 'FAIL'}", "PRE MOVE V2 SMOKE TEST END"])
    return "\n".join(lines)
