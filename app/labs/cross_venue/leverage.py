"""Offline leverage scenarios derived from identical closed simulated fills."""

from __future__ import annotations

import json
import math
import statistics
from typing import Any

from . import safety_envelope
from .ledger import CrossVenueLedger


def simulate_trade(trade: dict[str, Any], leverage: int, equity_before: float, *, maintenance_margin: float = 0.005) -> dict[str, Any]:
    if leverage <= 0:
        raise ValueError("CROSS_VENUE_LEVERAGE_INVALID")
    margin = min(float(trade["notional"]), equity_before)
    leveraged_notional = margin * leverage
    gross_bps = float(trade["gross_return_bps"]); cost_bps = float(trade["total_cost_bps"])
    if not math.isfinite(gross_bps) or not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("CROSS_VENUE_LEVERAGE_TRADE_INPUT_INVALID")
    mae_bps = max(0.0, float(trade.get("mae_bps") or 0.0))
    liquidation_distance = math.inf if leverage == 1 else max(0.0, (1.0 / leverage - maintenance_margin) * 10_000)
    liquidated = leverage > 1 and mae_bps >= liquidation_distance
    pnl = -margin if liquidated else leveraged_notional * (gross_bps - cost_bps) / 10_000
    return {
        "trade_id": trade["trade_id"], "leverage": leverage, "same_fill_base": True,
        "same_market_path": True, "margin": margin, "leveraged_notional": leveraged_notional,
        "gross_return_bps": gross_bps, "cost_bps_per_notional": cost_bps,
        "unlevered_net_return_bps": gross_bps - cost_bps, "mae_bps": mae_bps,
        "maintenance_margin_fraction": maintenance_margin,
        "maintenance_tier_status": "CONSERVATIVE_STATIC_UNVERIFIED_NOT_PRODUCTIVE",
        "liquidation_distance_bps": liquidation_distance, "liquidated": liquidated,
        "pnl": pnl, "equity_before": equity_before, "equity_after": max(0.0, equity_before + pnl),
        **safety_envelope(),
    }


class LeverageLab:
    def __init__(self, config: dict[str, Any], ledger: CrossVenueLedger):
        self.config = config; self.ledger = ledger

    def refresh(self) -> dict[str, Any]:
        trades = list(reversed(self.ledger.rows("trades", 5000)))
        scenarios: list[dict[str, Any]] = []
        for leverage in [int(item) for item in self.config.get("leverage_scenarios", [1, 2, 3, 5, 10, 20, 50])]:
            equity = float(self.config.get("paper_initial_balance_usdt", 50.0)); peak = equity
            max_drawdown = 0.0
            results = []
            for trade in trades:
                result = simulate_trade(trade, leverage, equity)
                equity = result["equity_after"]; peak = max(peak, equity)
                max_drawdown = max(max_drawdown, (peak - equity) / peak if peak else 0.0)
                results.append(result)
                with self.ledger.transaction() as conn:
                    conn.execute("""INSERT OR IGNORE INTO leverage_results VALUES (?,?,?,?,?,?,?)""", (
                        trade["trade_id"], leverage, result["pnl"], equity, int(result["liquidated"]),
                        result["liquidation_distance_bps"], json.dumps(result, sort_keys=True, default=str),
                    ))
            pnls = [row["pnl"] for row in results]; wins = [x for x in pnls if x > 0]; losses = [x for x in pnls if x < 0]
            gross_profit, gross_loss = sum(wins), abs(sum(losses))
            net_ev = sum(pnls) / len(pnls) if pnls else None
            tail_count = max(1, math.ceil(len(pnls) * 0.05)) if pnls else 0
            expected_shortfall = sum(sorted(pnls)[:tail_count]) / tail_count if tail_count else None
            ruin_observed = any(row["equity_after"] <= 0 for row in results)
            scenarios.append({
                "leverage": leverage, "trades": len(results), "pnl": sum(pnls), "equity": equity,
                "return_on_equity_pct": (equity / float(self.config.get("paper_initial_balance_usdt", 50.0)) - 1) * 100,
                "net_ev": net_ev, "profit_factor": gross_profit / gross_loss if gross_loss else None,
                "win_rate": len(wins) / len(pnls) if pnls else None,
                "average_win": sum(wins) / len(wins) if wins else None,
                "average_loss": sum(losses) / len(losses) if losses else None,
                "payoff_ratio": (sum(wins) / len(wins)) / abs(sum(losses) / len(losses)) if wins and losses else None,
                "pnl_volatility": statistics.pstdev(pnls) if len(pnls) >= 2 else None,
                "expected_shortfall_5pct": expected_shortfall,
                "worst_trade": min(pnls) if pnls else None,
                "profit_concentration": max(wins) / sum(wins) if wins and sum(wins) > 0 else None,
                "liquidations": sum(1 for row in results if row["liquidated"]),
                "ruin_observed": ruin_observed,
                "probability_of_ruin": None,
                "probability_of_ruin_status": "NEEDS_RESAMPLED_PATHS_NOT_INFERRED_FROM_ONE_PATH",
                "max_drawdown_pct": max_drawdown,
                "status": "NEED_MORE_DATA" if len(results) < 200 else "RESEARCH_ONLY",
                "promotion_allowed": False,
            })
        return {
            "schema": "cross_venue_leverage_lab.v1", "scenarios": scenarios,
            "base_trade_count": len(trades), "same_fill_and_path_for_all_scenarios": True,
            "real_leverage_changed": False, **safety_envelope(),
        }
