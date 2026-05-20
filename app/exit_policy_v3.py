from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .operational_intelligence_utils import (
    FINAL_RECOMMENDATION,
    edge_metrics,
    hit_class,
    load_operational_rows,
    normalize_row,
    row_return,
    safe_float_text,
    smoke_safety_lines,
)
from .utils import safe_float


POLICIES = (
    "fixed_tp_sl_baseline",
    "break_even_after_mfe",
    "profit_lock_after_mfe",
    "trailing_stop_atr",
    "trailing_stop_percent",
    "momentum_decay_exit",
    "hybrid_partial_tp_trailing",
    "time_decay_exit",
    "regime_adaptive_exit",
)


@dataclass(frozen=True)
class ExitPolicyV3Result:
    policy_id: str
    entry: float
    side: str
    regime: str
    fixed_tp: float
    fixed_sl: float
    dynamic_tp1: float
    dynamic_tp2: float
    break_even_trigger: float
    profit_lock_trigger: float
    trailing_distance: float
    expected_capture_ratio: float
    mfe_capture_pct: float
    mae_risk_pct: float
    simulated_exit_reason: str
    simulated_return_pct: float
    simulated_net_ev: float
    simulated_net_pf: float
    time_death_reduction_estimate: float
    decision: str
    research_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def simulate_exit_policy(row: dict[str, Any], policy_id: str = "fixed_tp_sl_baseline", config: Any | None = None) -> ExitPolicyV3Result:
    normalized = normalize_row(row)
    policy = policy_id if policy_id in POLICIES else "fixed_tp_sl_baseline"
    side = str(normalized.get("side") or "UNKNOWN").upper()
    regime = str(normalized.get("market_regime") or "UNKNOWN").upper()
    mfe = max(0.0, safe_float(normalized.get("mfe")))
    mae = abs(safe_float(normalized.get("mae")))
    base_return = row_return(normalized)
    baseline_hit = hit_class(normalized.get("first_barrier_hit"))
    fixed_tp = max(0.25, min(max(mfe, 0.25), 2.4))
    fixed_sl = max(0.25, min(max(mae, 0.25), 1.2))
    dynamic_tp1 = fixed_tp
    dynamic_tp2 = fixed_tp * 1.5
    break_even_trigger = 0.45
    profit_lock_trigger = 0.75
    trailing_distance = 0.35
    capture = 0.0
    exit_reason = baseline_hit
    time_reduction = 0.0

    if policy == "fixed_tp_sl_baseline":
        simulated = base_return
        capture = min(1.0, max(0.0, base_return / max(mfe, 0.0001))) if base_return > 0 else 0.0
    elif policy == "break_even_after_mfe":
        simulated = max(0.0, base_return) if mfe >= break_even_trigger else base_return
        capture = min(0.35, simulated / max(mfe, 0.0001)) if mfe > 0 else 0.0
        exit_reason = "BREAK_EVEN" if mfe >= break_even_trigger and base_return < 0 else baseline_hit
        time_reduction = 0.15 if baseline_hit == "TIME" and mfe >= break_even_trigger else 0.0
    elif policy == "profit_lock_after_mfe":
        locked = 0.25 if mfe >= profit_lock_trigger else base_return
        simulated = max(base_return, locked)
        capture = min(1.0, simulated / max(mfe, 0.0001)) if simulated > 0 else 0.0
        exit_reason = "PROFIT_LOCK" if mfe >= profit_lock_trigger else baseline_hit
        time_reduction = 0.25 if baseline_hit == "TIME" and mfe >= profit_lock_trigger else 0.0
    elif policy in {"trailing_stop_atr", "trailing_stop_percent"}:
        trend_fit = regime in {"TREND_UP", "TREND_DOWN"} or (regime == "RISK_OFF" and side == "SHORT")
        trailing_distance = 0.45 if policy == "trailing_stop_atr" else 0.30
        capture = 0.68 if trend_fit else 0.42
        simulated = max(base_return, mfe * capture - trailing_distance)
        exit_reason = "TRAILING_WINNER" if simulated > base_return else "TRAILING_WHIPSAW"
        time_reduction = 0.35 if baseline_hit == "TIME" and trend_fit and mfe > trailing_distance else 0.08
    elif policy == "momentum_decay_exit":
        capture = 0.48 if mfe > 0.4 else 0.0
        simulated = max(base_return, mfe * capture - 0.10) if mfe > 0.4 else min(base_return, -min(mae, 0.20))
        exit_reason = "MOMENTUM_DECAY"
        time_reduction = 0.25 if baseline_hit == "TIME" else 0.0
    elif policy == "hybrid_partial_tp_trailing":
        trend_fit = regime in {"TREND_UP", "TREND_DOWN", "RISK_OFF"}
        capture = 0.72 if trend_fit else 0.50
        simulated = max(base_return, (mfe * 0.45) + (mfe * capture * 0.55) - 0.18)
        dynamic_tp1 = max(0.35, fixed_tp * 0.85)
        dynamic_tp2 = max(dynamic_tp1 * 1.5, fixed_tp * 1.8)
        exit_reason = "PARTIAL_TP_TRAILING"
        time_reduction = 0.40 if baseline_hit == "TIME" and mfe > dynamic_tp1 else 0.0
    elif policy == "time_decay_exit":
        bars = safe_float(normalized.get("bars"))
        progress = mfe - mae
        simulated = base_return
        if bars >= 10 and progress <= 0.1:
            simulated = min(base_return, 0.0)
            exit_reason = "TIME_DECAY_FLAT"
            time_reduction = 0.50 if baseline_hit == "TIME" else 0.0
        capture = min(0.3, max(0.0, simulated / max(mfe, 0.0001))) if mfe > 0 else 0.0
    else:
        if regime in {"TREND_UP", "TREND_DOWN"} or (regime == "RISK_OFF" and side == "SHORT"):
            capture = 0.70
            dynamic_tp1 = fixed_tp * 1.25
            dynamic_tp2 = fixed_tp * 2.0
            simulated = max(base_return, mfe * capture - 0.25)
            exit_reason = "REGIME_ADAPTIVE_TREND"
            time_reduction = 0.35 if baseline_hit == "TIME" else 0.0
        elif regime == "RANGE":
            capture = 0.50
            dynamic_tp1 = max(0.20, fixed_tp * 0.75)
            dynamic_tp2 = fixed_tp * 1.1
            simulated = max(base_return, min(mfe, dynamic_tp1) - 0.12)
            exit_reason = "REGIME_ADAPTIVE_RANGE"
            time_reduction = 0.25 if baseline_hit == "TIME" else 0.0
        else:
            simulated = min(base_return, 0.0)
            exit_reason = "CHOPPY_RESEARCH_NO_TRADE"
            time_reduction = 0.10 if baseline_hit == "TIME" else 0.0

    metrics = edge_metrics([{**normalized, "return_pct": simulated, "first_barrier_hit": _hit_from_return(simulated)}], config)
    decision = "NEED_MORE_DATA" if safe_float(metrics["samples"]) < 2 else "WATCH_ONLY"
    return ExitPolicyV3Result(
        policy_id=policy,
        entry=0.0,
        side=side,
        regime=regime,
        fixed_tp=fixed_tp,
        fixed_sl=fixed_sl,
        dynamic_tp1=dynamic_tp1,
        dynamic_tp2=dynamic_tp2,
        break_even_trigger=break_even_trigger,
        profit_lock_trigger=profit_lock_trigger,
        trailing_distance=trailing_distance,
        expected_capture_ratio=capture,
        mfe_capture_pct=capture * 100.0,
        mae_risk_pct=mae,
        simulated_exit_reason=exit_reason,
        simulated_return_pct=simulated,
        simulated_net_ev=safe_float(metrics.get("net_EV")),
        simulated_net_pf=safe_float(metrics.get("net_PF")),
        time_death_reduction_estimate=max(0.0, min(time_reduction, 1.0)),
        decision=decision,
    )


def _hit_from_return(value: float) -> str:
    if value > 0:
        return "TP"
    if value < 0:
        return "SL"
    return "TIME"


class ExitPolicyV3:
    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        rows = load_operational_rows(self.db, hours=hours)
        samples = rows[: min(len(rows), 2000)]
        by_policy: list[dict[str, Any]] = []
        for policy in POLICIES:
            simulated_rows = []
            results = [simulate_exit_policy(row, policy, self.config) for row in samples]
            for row, result in zip(samples, results):
                simulated_rows.append({
                    **row,
                    "return_pct": result.simulated_return_pct,
                    "first_barrier_hit": _hit_from_return(result.simulated_return_pct),
                    "source": row.get("source", "trade_signal"),
                })
            metrics = edge_metrics(simulated_rows, self.config) if simulated_rows else edge_metrics([], self.config)
            by_policy.append({
                "policy_id": policy,
                "samples": len(results),
                "simulated_net_ev": metrics["net_EV"],
                "simulated_net_pf": metrics["net_PF"],
                "time_death_reduction_estimate": sum(result.time_death_reduction_estimate for result in results) / max(len(results), 1),
                "expected_capture_ratio": sum(result.expected_capture_ratio for result in results) / max(len(results), 1),
                "decision": _policy_decision(metrics, len(results)),
                "research_only": True,
            })
        best = sorted(by_policy, key=lambda row: (safe_float(row["simulated_net_ev"]), safe_float(row["expected_capture_ratio"])), reverse=True)[:5]
        return {
            "hours": hours,
            "rows": len(rows),
            "policy_count": len(POLICIES),
            "best_exit_policies": best,
            "all_policies": by_policy,
            "exit_policy_v3_status": "SHADOW_READY" if rows else "NEED_DATA",
            "apply_automatically": False,
            "research_only": True,
            "final_recommendation": FINAL_RECOMMENDATION,
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            "EXIT POLICY V3 START",
            f"hours: {payload['hours']}",
            f"rows: {payload['rows']}",
            f"exit_policy_v3_status: {payload['exit_policy_v3_status']}",
            "best_exit_policies:",
        ]
        if not payload["best_exit_policies"]:
            lines.append("- none")
        for row in payload["best_exit_policies"]:
            lines.append(
                f"- {row['policy_id']}: samples={row['samples']} net_EV={safe_float_text(row['simulated_net_ev'])} "
                f"net_PF={safe_float_text(row['simulated_net_pf'], 2)} time_death_reduction={safe_float_text(row['time_death_reduction_estimate'], 3)} "
                f"decision={row['decision']}"
            )
        lines.extend([
            "research_only: true",
            "apply_automatically: false",
            "final_recommendation: NO LIVE",
            "EXIT POLICY V3 END",
        ])
        return "\n".join(lines)


def _policy_decision(metrics: dict[str, Any], samples: int) -> str:
    if samples < 250:
        return "NEED_MORE_DATA"
    if safe_float(metrics.get("net_EV")) <= 0:
        return "REJECT"
    if safe_float(metrics.get("TIME")) > 0.8:
        return "WATCH_ONLY"
    return "SHADOW_EXIT_CANDIDATE"


def exit_policy_v3_smoke_text() -> str:
    trend = {"side": "SHORT", "market_regime": "TREND_DOWN", "mfe": 2.0, "mae": 0.3, "return_pct": 0.4, "first_barrier_hit": "TP", "source": "trade_signal"}
    range_row = {"side": "LONG", "market_regime": "RANGE", "mfe": 0.8, "mae": 0.4, "return_pct": 0.0, "first_barrier_hit": "TIME", "source": "trade_signal"}
    choppy = {"side": "LONG", "market_regime": "CHOPPY_MARKET", "mfe": 0.2, "mae": 0.7, "return_pct": -0.3, "first_barrier_hit": "SL", "source": "trade_signal"}
    trend_trailing = simulate_exit_policy(trend, "regime_adaptive_exit")
    range_exit = simulate_exit_policy(range_row, "regime_adaptive_exit")
    choppy_exit = simulate_exit_policy(choppy, "regime_adaptive_exit")
    be = simulate_exit_policy({**trend, "return_pct": -0.2, "first_barrier_hit": "SL"}, "break_even_after_mfe")
    lock = simulate_exit_policy(trend, "profit_lock_after_mfe")
    checks = {
        "trailing_leaves_trend_more_room": trend_trailing.expected_capture_ratio >= 0.65,
        "range_uses_shorter_targets": range_exit.dynamic_tp1 <= range_exit.fixed_tp,
        "choppy_does_not_force_aggressive_exit": choppy_exit.simulated_exit_reason == "CHOPPY_RESEARCH_NO_TRADE",
        "break_even_not_loss_after_mfe": be.simulated_return_pct >= 0,
        "profit_lock_secures_profit": lock.simulated_return_pct >= 0.25,
        "research_only": trend_trailing.research_only and range_exit.research_only and choppy_exit.research_only,
    }
    passed = all(checks.values())
    lines = ["EXIT POLICY V3 SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend(smoke_safety_lines())
    lines.extend([f"result: {'PASS' if passed else 'FAIL'}", "EXIT POLICY V3 SMOKE TEST END"])
    return "\n".join(lines)
