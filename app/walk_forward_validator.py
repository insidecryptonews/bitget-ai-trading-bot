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
from .utils import safe_float


GROUP_KEYS = ("symbol", "side", "market_regime", "score_bucket", "strategy")


class WalkForwardValidator:
    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 72) -> dict[str, Any]:
        rows = load_operational_rows(self.db, hours=hours)
        candidates = [validate_group(group_rows, self.config, group_key=key) for key, group_rows in group_by_keys(rows, GROUP_KEYS).items()]
        candidates.sort(key=lambda row: (safe_float(row.get("stability_score")), safe_float(row.get("forward_net_ev"))), reverse=True)
        counts: dict[str, int] = {}
        for row in candidates:
            counts[str(row.get("decision"))] = counts.get(str(row.get("decision")), 0) + 1
        return {
            "hours": hours,
            "candidates": len(candidates),
            "decision_counts": counts,
            "walk_forward_candidates": candidates[:30],
            "research_only": True,
            "final_recommendation": FINAL_RECOMMENDATION,
        }

    def to_text(self, *, hours: int = 72) -> str:
        payload = self.build(hours=hours)
        lines = [
            "WALK FORWARD VALIDATOR START",
            f"hours: {payload['hours']}",
            f"candidates: {payload['candidates']}",
            f"decision_counts: {payload['decision_counts']}",
            "walk_forward_candidates:",
        ]
        if not payload["walk_forward_candidates"]:
            lines.append("- none")
        for row in payload["walk_forward_candidates"][:12]:
            lines.append(
                f"- {row['candidate_id']}: train_EV={safe_float_text(row['train_net_ev'])} "
                f"validation_EV={safe_float_text(row['validation_net_ev'])} forward_EV={safe_float_text(row['forward_net_ev'])} "
                f"stability={safe_float_text(row['stability_score'], 3)} decision={row['decision']}"
            )
        lines.extend(["research_only: true", "final_recommendation: NO LIVE", "WALK FORWARD VALIDATOR END"])
        return "\n".join(lines)


def validate_group(rows: list[dict[str, Any]], config: Any | None = None, *, group_key: tuple[str, ...] | None = None) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: str(row.get("timestamp") or ""))
    n = len(ordered)
    train = ordered[: max(1, int(n * 0.50))]
    validation = ordered[max(1, int(n * 0.50)): max(2, int(n * 0.75))]
    forward = ordered[max(2, int(n * 0.75)):]
    train_m = edge_metrics(train, config)
    val_m = edge_metrics(validation, config)
    fwd_m = edge_metrics(forward, config)
    stability = _stability(train_m, val_m, fwd_m)
    degradation = _degradation(train_m, fwd_m)
    decision = _decision(n, train_m, val_m, fwd_m, stability, degradation)
    candidate_id = "|".join(group_key or tuple(str(rows[0].get(key) or "NA") for key in GROUP_KEYS)) if rows else "empty"
    return {
        "candidate_id": candidate_id,
        "sample_count": n,
        "train_net_ev": train_m["net_EV"],
        "validation_net_ev": val_m["net_EV"],
        "forward_net_ev": fwd_m["net_EV"],
        "train_pf": train_m["net_PF"],
        "validation_pf": val_m["net_PF"],
        "forward_pf": fwd_m["net_PF"],
        "train_time_pct": train_m["TIME"],
        "forward_time_pct": fwd_m["TIME"],
        "stability_score": stability,
        "degradation_pct": degradation,
        "overfit_risk": "HIGH" if decision in {"REJECT", "OVERFIT_REJECT"} else "LOW" if stability >= 0.67 else "MEDIUM",
        "confidence": fwd_m["confidence"],
        "decision": decision,
        "research_only": True,
    }


def _stability(train: dict[str, Any], validation: dict[str, Any], forward: dict[str, Any]) -> float:
    positives = sum(1 for item in (train, validation, forward) if safe_float(item.get("net_EV")) > 0 and safe_float(item.get("net_PF")) > 1.0)
    return positives / 3.0


def _degradation(train: dict[str, Any], forward: dict[str, Any]) -> float:
    train_ev = safe_float(train.get("net_EV"))
    forward_ev = safe_float(forward.get("net_EV"))
    if train_ev <= 0:
        return 0.0
    return max(0.0, (train_ev - forward_ev) / max(abs(train_ev), 0.0001))


def _decision(samples: int, train: dict[str, Any], validation: dict[str, Any], forward: dict[str, Any], stability: float, degradation: float) -> str:
    if samples < 250:
        return "NEED_MORE_DATA"
    if safe_float(train.get("net_EV")) > 0 and safe_float(forward.get("net_EV")) <= 0:
        return "OVERFIT_REJECT"
    if safe_float(validation.get("net_EV")) <= 0 or safe_float(forward.get("net_EV")) <= 0:
        return "REJECT"
    if degradation > 0.75:
        return "OVERFIT_REJECT"
    if samples < 750:
        return "RESEARCH_POCKET" if stability >= 0.67 else "WATCH_ONLY"
    return "SHADOW_CANDIDATE" if stability >= 0.67 else "WATCH_ONLY"


def walk_forward_smoke_text() -> str:
    stable = [
        {"symbol": "ETHUSDT", "side": "SHORT", "market_regime": "TREND_DOWN", "score_bucket": "85-89", "strategy": "trend", "source": "trade_signal", "return_pct": 0.45, "first_barrier_hit": "TP", "timestamp": f"2026-01-01T00:{i:02d}:00+00:00"}
        for i in range(60)
    ] * 15
    overfit = [
        {"symbol": "SOLUSDT", "side": "LONG", "market_regime": "RANGE", "score_bucket": "90-94", "strategy": "breakout", "source": "trade_signal", "return_pct": 0.5 if i < 450 else -0.6, "first_barrier_hit": "TP" if i < 450 else "SL", "timestamp": f"2026-01-01T{i//60:02d}:{i%60:02d}:00+00:00"}
        for i in range(900)
    ]
    low = stable[:40]
    stable_result = validate_group(stable)
    overfit_result = validate_group(overfit)
    low_result = validate_group(low)
    checks = {
        "overfit_candidate_rejected": overfit_result["decision"] in {"OVERFIT_REJECT", "REJECT"},
        "stable_candidate_shadow": stable_result["decision"] in {"SHADOW_CANDIDATE", "RESEARCH_POCKET"},
        "low_sample_never_passes": low_result["decision"] == "NEED_MORE_DATA",
    }
    lines = ["WALK FORWARD SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend(smoke_safety_lines())
    lines.extend([f"result: {'PASS' if all(checks.values()) else 'FAIL'}", "WALK FORWARD SMOKE TEST END"])
    return "\n".join(lines)
