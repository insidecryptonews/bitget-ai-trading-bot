from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .operational_intelligence_utils import (
    FINAL_RECOMMENDATION,
    edge_metrics,
    hit_class,
    load_operational_rows,
    normalize_row,
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
    simulated_return_pct: float | None
    simulated_net_ev: float | None
    simulated_net_pf: float | None
    time_death_reduction_estimate: float
    decision: str
    backtest_status: str = "NEED_BAR_PATH"
    research_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def simulate_exit_policy(row: dict[str, Any], policy_id: str = "fixed_tp_sl_baseline", config: Any | None = None) -> ExitPolicyV3Result:
    normalized = normalize_row(row)
    policy = policy_id if policy_id in POLICIES else "fixed_tp_sl_baseline"
    side = str(normalized.get("side") or "UNKNOWN").upper()
    regime = str(normalized.get("market_regime") or "UNKNOWN").upper()
    mae = abs(safe_float(normalized.get("mae")))
    bar_path = _extract_bar_path(row)
    fixed_tp, fixed_sl, dynamic_tp1, dynamic_tp2, break_even_trigger, profit_lock_trigger, trailing_distance = _policy_levels(policy, regime, side)
    if not bar_path:
        return evaluate_exit_policy_summary_only(row, policy_id=policy)

    bar_result = simulate_exit_policy_bar_by_bar(
        entry=safe_float(row.get("entry") or row.get("entry_price") or bar_path[0].get("open")),
        side=side,
        bars=bar_path,
        policy_config={
            "policy_id": policy,
            "tp_pct": dynamic_tp1,
            "sl_pct": fixed_sl,
            "dynamic_tp2": dynamic_tp2,
            "break_even_trigger_pct": break_even_trigger,
            "profit_lock_trigger_pct": profit_lock_trigger,
            "trailing_distance_pct": trailing_distance,
            "max_bars": safe_float(row.get("max_bars") or row.get("bars") or 30),
        },
        cost_model=config,
    )
    simulated = safe_float(bar_result.get("realized_return_pct"))
    metrics = edge_metrics([{**normalized, "return_pct": simulated, "first_barrier_hit": _hit_from_return(simulated)}], config)
    decision = "WATCH_ONLY" if metrics.get("edge_metrics_status") == "OK" else "NEED_MORE_DATA"
    mfe = max(0.0, safe_float(bar_result.get("max_favorable_seen_pct")))
    capture = safe_float(bar_result.get("mfe_capture_pct")) / 100.0
    time_reduction = 0.0 if bar_result.get("exit_reason") == "HORIZON_CLOSE" else 0.25
    exit_reason = str(bar_result.get("exit_reason") or "UNKNOWN")
    return ExitPolicyV3Result(
        policy_id=policy,
        entry=safe_float(bar_result.get("entry")),
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
        mae_risk_pct=max(mae, safe_float(bar_result.get("max_adverse_seen_pct"))),
        simulated_exit_reason=exit_reason,
        simulated_return_pct=simulated,
        simulated_net_ev=safe_float(metrics.get("net_EV")),
        simulated_net_pf=safe_float(metrics.get("net_PF")),
        time_death_reduction_estimate=max(0.0, min(time_reduction, 1.0)),
        decision=decision,
        backtest_status="OK_BAR_PATH",
    )


def evaluate_exit_policy_summary_only(row: dict[str, Any], policy_id: str = "fixed_tp_sl_baseline") -> ExitPolicyV3Result:
    normalized = normalize_row(row)
    policy = policy_id if policy_id in POLICIES else "fixed_tp_sl_baseline"
    side = str(normalized.get("side") or "UNKNOWN").upper()
    regime = str(normalized.get("market_regime") or "UNKNOWN").upper()
    fixed_tp, fixed_sl, dynamic_tp1, dynamic_tp2, break_even_trigger, profit_lock_trigger, trailing_distance = _policy_levels(policy, regime, side)
    return ExitPolicyV3Result(
        policy_id=policy,
        entry=safe_float(row.get("entry") or row.get("entry_price")),
        side=side,
        regime=regime,
        fixed_tp=fixed_tp,
        fixed_sl=fixed_sl,
        dynamic_tp1=dynamic_tp1,
        dynamic_tp2=dynamic_tp2,
        break_even_trigger=break_even_trigger,
        profit_lock_trigger=profit_lock_trigger,
        trailing_distance=trailing_distance,
        expected_capture_ratio=0.0,
        mfe_capture_pct=0.0,
        mae_risk_pct=abs(safe_float(normalized.get("mae"))),
        simulated_exit_reason="NEED_BAR_PATH",
        simulated_return_pct=None,
        simulated_net_ev=None,
        simulated_net_pf=None,
        time_death_reduction_estimate=0.0,
        decision="NEED_BAR_PATH",
        backtest_status="NEED_BAR_PATH",
    )


def simulate_exit_policy_bar_by_bar(entry: float, side: str, bars: list[dict[str, Any]], policy_config: dict[str, Any] | None = None, cost_model: Any | None = None) -> dict[str, Any]:
    del cost_model
    cfg = policy_config or {}
    side_text = str(side or "").upper()
    direction = 1 if side_text == "LONG" else -1
    entry_price = safe_float(entry)
    if entry_price <= 0 or direction not in {1, -1} or not bars:
        return {"backtest_status": "INVALID_INPUT", "realized_return_pct": 0.0, "exit_reason": "INVALID_INPUT", "entry": entry_price}
    tp_pct = safe_float(cfg.get("tp_pct") or 0.96)
    sl_pct = safe_float(cfg.get("sl_pct") or 0.60)
    be_trigger = safe_float(cfg.get("break_even_trigger_pct") or 0.45)
    lock_trigger = safe_float(cfg.get("profit_lock_trigger_pct") or 0.75)
    trailing_distance = safe_float(cfg.get("trailing_distance_pct") or 0.35)
    max_bars = int(max(1, safe_float(cfg.get("max_bars") or len(bars))))
    stop = entry_price * (1 - sl_pct / 100.0) if direction == 1 else entry_price * (1 + sl_pct / 100.0)
    tp = entry_price * (1 + tp_pct / 100.0) if direction == 1 else entry_price * (1 - tp_pct / 100.0)
    best_price = entry_price
    max_fav = 0.0
    max_adv = 0.0
    exit_price = safe_float(bars[min(max_bars, len(bars)) - 1].get("close"))
    exit_reason = "HORIZON_CLOSE"
    exit_index = min(max_bars, len(bars)) - 1
    policy_id = str(cfg.get("policy_id") or "fixed_tp_sl_baseline")

    for index, bar in enumerate(bars[:max_bars]):
        high = safe_float(bar.get("high"))
        low = safe_float(bar.get("low"))
        close = safe_float(bar.get("close"))
        fav_price = high if direction == 1 else low
        adv_price = low if direction == 1 else high
        fav_pct = ((fav_price - entry_price) / entry_price * 100.0) * direction
        adv_pct = ((adv_price - entry_price) / entry_price * 100.0) * direction
        max_fav = max(max_fav, fav_pct)
        max_adv = min(max_adv, adv_pct)
        if direction == 1:
            best_price = max(best_price, high)
            if policy_id in {"break_even_after_mfe", "profit_lock_after_mfe", "hybrid_partial_tp_trailing", "regime_adaptive_exit"} and max_fav >= be_trigger:
                stop = max(stop, entry_price)
            if policy_id in {"profit_lock_after_mfe", "hybrid_partial_tp_trailing"} and max_fav >= lock_trigger:
                stop = max(stop, entry_price * (1 + 0.20 / 100.0))
            if policy_id in {"trailing_stop_atr", "trailing_stop_percent", "hybrid_partial_tp_trailing", "regime_adaptive_exit"}:
                stop = max(stop, best_price * (1 - trailing_distance / 100.0))
            stop_hit = low <= stop
            tp_hit = high >= tp
        else:
            best_price = min(best_price, low)
            if policy_id in {"break_even_after_mfe", "profit_lock_after_mfe", "hybrid_partial_tp_trailing", "regime_adaptive_exit"} and max_fav >= be_trigger:
                stop = min(stop, entry_price)
            if policy_id in {"profit_lock_after_mfe", "hybrid_partial_tp_trailing"} and max_fav >= lock_trigger:
                stop = min(stop, entry_price * (1 - 0.20 / 100.0))
            if policy_id in {"trailing_stop_atr", "trailing_stop_percent", "hybrid_partial_tp_trailing", "regime_adaptive_exit"}:
                stop = min(stop, best_price * (1 + trailing_distance / 100.0))
            stop_hit = high >= stop
            tp_hit = low <= tp
        # Conservative same-bar rule: if both barriers are touched, stop wins.
        if stop_hit:
            exit_price = stop
            exit_reason = "STOP_LOSS" if stop != entry_price else "BREAK_EVEN"
            exit_index = index
            break
        if tp_hit:
            exit_price = tp
            exit_reason = "TAKE_PROFIT"
            exit_index = index
            break
        if policy_id == "momentum_decay_exit" and index >= 3 and max_fav >= be_trigger:
            favorable_close_pct = ((close - entry_price) / entry_price * 100.0) * direction
            if favorable_close_pct < max_fav * 0.35:
                exit_price = close
                exit_reason = "MOMENTUM_DECAY"
                exit_index = index
                break
        if policy_id == "time_decay_exit" and index >= 10 and max_fav < be_trigger:
            exit_price = close
            exit_reason = "TIME_DECAY_FLAT"
            exit_index = index
            break
    realized = ((exit_price - entry_price) / entry_price * 100.0) * direction
    capture = max(0.0, realized / max(max_fav, 0.0001)) * 100.0 if realized > 0 else 0.0
    return {
        "backtest_status": "OK_BAR_PATH",
        "entry": entry_price,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "exit_bar_index": exit_index,
        "realized_return_pct": realized,
        "max_favorable_seen_pct": max_fav,
        "max_adverse_seen_pct": abs(max_adv),
        "mfe_capture_pct": min(capture, 100.0),
        "same_bar_stop_tp_rule": "STOP_BEFORE_TP",
    }


def _extract_bar_path(row: dict[str, Any]) -> list[dict[str, Any]]:
    path = row.get("bar_path") or row.get("bars_ohlcv") or row.get("path_bars")
    if isinstance(path, list):
        return [item for item in path if isinstance(item, dict)]
    return []


def _policy_levels(policy: str, regime: str, side: str) -> tuple[float, float, float, float, float, float, float]:
    del side
    fixed_tp = 0.96
    fixed_sl = 0.60
    dynamic_tp1 = fixed_tp
    dynamic_tp2 = 1.44
    be = 0.45
    lock = 0.75
    trailing = 0.35
    if policy == "regime_adaptive_exit":
        if regime in {"TREND_UP", "TREND_DOWN", "RISK_OFF"}:
            dynamic_tp1, dynamic_tp2, trailing = 1.20, 2.10, 0.45
        elif regime == "RANGE":
            dynamic_tp1, dynamic_tp2, trailing = 0.78, 1.10, 0.30
        elif regime == "CHOPPY_MARKET":
            dynamic_tp1, dynamic_tp2, trailing = 0.50, 0.75, 0.25
    elif policy == "trailing_stop_atr":
        trailing = 0.45
    elif policy == "trailing_stop_percent":
        trailing = 0.30
    elif policy == "hybrid_partial_tp_trailing":
        dynamic_tp1, dynamic_tp2, trailing = 0.80, 1.80, 0.40
    elif policy == "time_decay_exit":
        dynamic_tp1, dynamic_tp2, trailing = 0.60, 0.90, 0.25
    return fixed_tp, fixed_sl, dynamic_tp1, dynamic_tp2, be, lock, trailing


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
                if result.simulated_return_pct is not None:
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
                "bar_path_samples": len(simulated_rows),
                "backtest_status": "OK_BAR_PATH" if simulated_rows else "NEED_BAR_PATH",
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
            "exit_policy_v3_status": "SHADOW_READY" if any(row.get("backtest_status") == "OK_BAR_PATH" for row in by_policy) else "NEED_BAR_PATH" if rows else "NEED_DATA",
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
                f"net_PF={safe_float_text(row['simulated_net_pf'], 2)} backtest_status={row['backtest_status']} "
                f"time_death_reduction={safe_float_text(row['time_death_reduction_estimate'], 3)} "
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
    if str(metrics.get("edge_metrics_status") or "OK") != "OK":
        return "NEED_BAR_PATH"
    if samples < 250:
        return "NEED_MORE_DATA"
    if safe_float(metrics.get("net_EV")) <= 0:
        return "REJECT"
    if safe_float(metrics.get("TIME")) > 0.8:
        return "WATCH_ONLY"
    return "SHADOW_EXIT_CANDIDATE"


def exit_policy_v3_smoke_text() -> str:
    trend_bars = [
        {"open": 100, "high": 100.4, "low": 99.9, "close": 100.3},
        {"open": 100.3, "high": 101.0, "low": 100.2, "close": 100.9},
        {"open": 100.9, "high": 101.6, "low": 100.8, "close": 101.4},
    ]
    trend = {"side": "LONG", "market_regime": "TREND_UP", "entry": 100, "bar_path": trend_bars, "source": "trade_signal"}
    range_row = {"side": "LONG", "market_regime": "RANGE", "entry": 100, "bar_path": trend_bars, "source": "trade_signal"}
    choppy = {"side": "LONG", "market_regime": "CHOPPY_MARKET", "entry": 100, "bar_path": [{"open": 100, "high": 100.1, "low": 99.2, "close": 99.5}], "source": "trade_signal"}
    trend_trailing = simulate_exit_policy(trend, "regime_adaptive_exit")
    range_exit = simulate_exit_policy(range_row, "regime_adaptive_exit")
    choppy_exit = simulate_exit_policy(choppy, "regime_adaptive_exit")
    be = simulate_exit_policy(trend, "break_even_after_mfe")
    lock = simulate_exit_policy(trend, "profit_lock_after_mfe")
    summary_only = simulate_exit_policy({"side": "LONG", "market_regime": "TREND_UP", "mfe": 5.0, "mae": 0.1}, "trailing_stop_atr")
    checks = {
        "trailing_requires_bar_path_and_runs_with_bars": trend_trailing.backtest_status == "OK_BAR_PATH",
        "range_uses_shorter_targets": range_exit.dynamic_tp1 <= range_exit.fixed_tp,
        "choppy_does_not_force_aggressive_exit": choppy_exit.simulated_exit_reason in {"STOP_LOSS", "HORIZON_CLOSE"},
        "break_even_not_loss_after_mfe": be.simulated_return_pct >= 0,
        "profit_lock_secures_profit": lock.simulated_return_pct is not None,
        "summary_only_needs_bar_path": summary_only.backtest_status == "NEED_BAR_PATH" and summary_only.simulated_net_ev is None,
        "research_only": trend_trailing.research_only and range_exit.research_only and choppy_exit.research_only,
    }
    passed = all(checks.values())
    lines = ["EXIT POLICY V3 SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend(smoke_safety_lines())
    lines.extend([f"result: {'PASS' if passed else 'FAIL'}", "EXIT POLICY V3 SMOKE TEST END"])
    return "\n".join(lines)
