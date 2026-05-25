from __future__ import annotations

import pandas as pd

from app.dynamic_hold_lab import (
    REJECT_TOO_FEW_TRADES,
    REJECT_WORSE_THAN_BASELINE,
    DynamicHoldPolicy,
    DynamicHoldTrade,
    _simulate_dynamic_trade,
    _summarise_policy,
)
from app.phase8_research_utils import ReplayTradeContext
from app.real_strategy_backtester import RealBacktestTrade


def _frame(extra: int = 0) -> pd.DataFrame:
    rows = []
    price = 100.0
    for i in range(12 + extra):
        open_price = price
        close = price + 0.15
        rows.append({
            "timestamp": pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=5 * i),
            "open": open_price,
            "high": close + 0.30,
            "low": open_price - 0.10,
            "close": close,
            "volume": 1000,
        })
        price = close
    return pd.DataFrame(rows)


def _trade() -> RealBacktestTrade:
    return RealBacktestTrade(
        symbol="BTCUSDT",
        side="LONG",
        signal_index=1,
        entry_index=2,
        exit_index=4,
        entry_price=100.0,
        exit_price=100.2,
        stop_loss=98.0,
        take_profit_1=105.0,
        gross_return_pct=0.2,
        net_return_pct=0.02,
        exit_reason="HORIZON_CLOSE",
        fee_cost_bps=12.0,
        slippage_cost_bps=6.0,
        funding_component_bps=0.0,
        total_cost_bps=18.0,
    )


def test_dynamic_hold_uses_prefix_horizon_not_future_tail():
    policy = DynamicHoldPolicy("fixed_extend_10_bars", extend_bars=2)
    base_ctx = ReplayTradeContext("BTCUSDT", "5m", _trade(), _frame(extra=0))
    with_tail_ctx = ReplayTradeContext("BTCUSDT", "5m", _trade(), _frame(extra=40))
    first = _simulate_dynamic_trade(base_ctx, policy)
    second = _simulate_dynamic_trade(with_tail_ctx, policy)
    assert first.duration_bars == second.duration_bars
    assert first.net_return_pct == second.net_return_pct


def test_dynamic_hold_low_sample_never_passes():
    candidate = [DynamicHoldTrade("BTCUSDT", "LONG", "p", 1.0, 1.0, "TAKE_PROFIT", 5, 1.0, 0.0)]
    baseline = [DynamicHoldTrade("BTCUSDT", "LONG", "b", 0.1, 0.1, "HORIZON_CLOSE", 5, 0.2, -0.1)]
    result = _summarise_policy("p", candidate, baseline)
    assert result.decision == REJECT_TOO_FEW_TRADES


def test_dynamic_hold_cannot_worsen_baseline_and_pass():
    candidate = [DynamicHoldTrade("BTCUSDT", "LONG", "p", -0.2, -0.2, "STOP_LOSS", 10, 0.1, -0.5) for _ in range(20)]
    baseline = [DynamicHoldTrade("BTCUSDT", "LONG", "b", 0.1, 0.1, "HORIZON_CLOSE", 5, 0.2, -0.1) for _ in range(20)]
    result = _summarise_policy("p", candidate, baseline)
    assert result.decision == REJECT_WORSE_THAN_BASELINE
