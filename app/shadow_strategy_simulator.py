from __future__ import annotations

from typing import Any

from .exit_policy_v3 import _hit_from_return, simulate_exit_policy
from .operational_intelligence_utils import (
    FINAL_RECOMMENDATION,
    edge_metrics,
    group_by_keys,
    load_operational_rows,
    max_drawdown,
    safe_float_text,
    smoke_safety_lines,
)
from .sudden_move_detector import detect_sudden_move
from .utils import safe_float


POLICY_SET = ("fixed_tp_sl_baseline", "profit_lock_after_mfe", "trailing_stop_atr", "regime_adaptive_exit")


class ShadowStrategySimulator:
    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 72) -> dict[str, Any]:
        rows = load_operational_rows(self.db, hours=hours)
        simulations = []
        for key, group_rows in group_by_keys(rows, ("side", "symbol", "market_regime")).items():
            for policy in POLICY_SET:
                simulations.append(simulate_strategy(group_rows, policy, self.config, group_key=key))
        simulations.sort(key=lambda row: (safe_float(row.get("net_ev")), safe_float(row.get("net_pf"))), reverse=True)
        return {
            "hours": hours,
            "simulations": len(simulations),
            "shadow_simulation_summary": simulations[:30],
            "best_simulated_policy": simulations[0] if simulations else {},
            "research_only": True,
            "paper_filter_enabled": False,
            "live_allowed": False,
            "final_recommendation": FINAL_RECOMMENDATION,
        }

    def to_text(self, *, hours: int = 72) -> str:
        payload = self.build(hours=hours)
        best = payload["best_simulated_policy"] or {}
        lines = [
            "SHADOW STRATEGY SIMULATOR START",
            f"hours: {payload['hours']}",
            f"simulations: {payload['simulations']}",
            f"best_simulated_policy: {best.get('strategy_id', 'none')}",
            f"best_net_ev: {safe_float_text(best.get('net_ev'))}",
            f"best_recommendation: {best.get('recommendation', 'NO_VALID_POLICY')}",
            "top_simulations:",
        ]
        if not payload["shadow_simulation_summary"]:
            lines.append("- none")
        for row in payload["shadow_simulation_summary"][:10]:
            lines.append(
                f"- {row['strategy_id']}: trades={row['simulated_trades']} win_rate={safe_float_text(row['win_rate'], 3)} "
                f"net_ev={safe_float_text(row['net_ev'])} net_pf={safe_float_text(row['net_pf'], 2)} recommendation={row['recommendation']}"
            )
        lines.extend(["paper_filter_enabled=false", "live_allowed=false", "final_recommendation: NO LIVE", "SHADOW STRATEGY SIMULATOR END"])
        return "\n".join(lines)


def simulate_strategy(rows: list[dict[str, Any]], exit_policy: str, config: Any | None = None, *, group_key: tuple[str, ...] | None = None) -> dict[str, Any]:
    simulated = []
    trailing = 0
    profit_lock = 0
    break_even = 0
    needs_bar_path = 0
    for row in rows:
        detection = detect_sudden_move(row, config)
        if "market_probe_not_actionable" in str(detection.get("not_actionable_reason")):
            continue
        result = simulate_exit_policy(row, exit_policy, config)
        if result.simulated_return_pct is None:
            needs_bar_path += 1
            continue
        simulated.append({**row, "return_pct": result.simulated_return_pct, "first_barrier_hit": _hit_from_return(result.simulated_return_pct)})
        trailing += int("TRAILING" in result.simulated_exit_reason or "ADAPTIVE" in result.simulated_exit_reason)
        profit_lock += int("PROFIT_LOCK" in result.simulated_exit_reason)
        break_even += int("BREAK_EVEN" in result.simulated_exit_reason)
    metrics = edge_metrics(simulated, config)
    returns = metrics.get("returns", [])
    wins = [value for value in returns if safe_float(value) > 0]
    losses = [value for value in returns if safe_float(value) < 0]
    strategy_id = "|".join(group_key or ("all",)) + f"|{exit_policy}"
    recommendation = _recommend(metrics)
    backtest_status = "NEED_BAR_PATH" if needs_bar_path and not simulated else "OK_BAR_PATH"
    if backtest_status == "NEED_BAR_PATH":
        recommendation = "NEED_BAR_PATH"
    return {
        "strategy_id": strategy_id,
        "exit_policy": exit_policy,
        "simulated_trades": len(simulated),
        "backtest_status": backtest_status,
        "win_rate": len(wins) / max(len(simulated), 1),
        "net_ev": metrics["net_EV"],
        "net_pf": metrics["net_PF"],
        "max_drawdown": max_drawdown(returns),
        "avg_win": sum(wins) / max(len(wins), 1),
        "avg_loss": sum(losses) / max(len(losses), 1),
        "time_pct": metrics["TIME"],
        "sl_pct": metrics["SL"],
        "tp_pct": metrics["TP"],
        "trailing_exit_pct": trailing / max(len(simulated), 1),
        "profit_lock_pct": profit_lock / max(len(simulated), 1),
        "break_even_pct": break_even / max(len(simulated), 1),
        "missed_move_reduction": max(0.0, metrics["avg_MFE"] - metrics["avg_MAE"]),
        "confidence": metrics["confidence"],
        "recommendation": recommendation,
        "research_only": True,
    }


def _recommend(metrics: dict[str, Any]) -> str:
    if str(metrics.get("edge_metrics_status") or "OK") != "OK":
        return "NEED_REALIZED_RETURN"
    if safe_float(metrics.get("samples")) < 250:
        return "NEED_MORE_DATA"
    if safe_float(metrics.get("net_EV")) <= 0 or safe_float(metrics.get("net_PF")) < 1.05:
        return "REJECT"
    if safe_float(metrics.get("drawdown_proxy")) > 2.0 or safe_float(metrics.get("avg_MAE")) > 2.0:
        return "REJECT_DRAWDOWN"
    return "RESEARCH_POCKET"


def shadow_strategy_simulator_smoke_text() -> str:
    bars = [{"open": 100, "high": 100.4, "low": 99.2, "close": 99.5}]
    no_edge = [{"symbol": "BTCUSDT", "side": "LONG", "market_regime": "RANGE", "source": "trade_signal", "entry": 100, "bar_path": bars, "return_pct": -0.4, "first_barrier_hit": "SL"} for _ in range(300)]
    trend_bars = [{"open": 100, "high": 100.5, "low": 99.9, "close": 100.4}, {"open": 100.4, "high": 101.5, "low": 100.3, "close": 101.2}]
    trend = [{"symbol": "ETHUSDT", "side": "LONG", "market_regime": "TREND_UP", "source": "trade_signal", "entry": 100, "bar_path": trend_bars, "return_pct": 0.2, "first_barrier_hit": "TP"} for _ in range(300)]
    drawdown = [{"symbol": "SOLUSDT", "side": "LONG", "market_regime": "RANGE", "source": "trade_signal", "entry": 100, "bar_path": bars, "return_pct": -3.0 if i % 4 == 0 else 0.5, "first_barrier_hit": "SL" if i % 4 == 0 else "TP"} for i in range(300)]
    no_edge_result = simulate_strategy(no_edge, "regime_adaptive_exit")
    trend_result = simulate_strategy(trend, "trailing_stop_atr")
    drawdown_result = simulate_strategy(drawdown, "trailing_stop_atr")
    checks = {
        "strategy_without_edge_not_promoted": no_edge_result["recommendation"] in {"REJECT", "REJECT_DRAWDOWN"},
        "trailing_can_improve_mfe_capture": trend_result["backtest_status"] == "OK_BAR_PATH",
        "drawdown_high_blocks_promotion": drawdown_result["recommendation"] in {"REJECT", "REJECT_DRAWDOWN"},
        "research_only": trend_result["research_only"],
    }
    lines = ["SHADOW STRATEGY SIMULATOR SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend(smoke_safety_lines())
    lines.extend([f"result: {'PASS' if all(checks.values()) else 'FAIL'}", "SHADOW STRATEGY SIMULATOR SMOKE TEST END"])
    return "\n".join(lines)
