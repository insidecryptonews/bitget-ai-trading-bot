from __future__ import annotations

import pandas as pd

from app.entry_exhaustion_lab import LATE_CHASE_ENTRY, REVERSAL_RISK, analyse_entry_exhaustion_trade
from app.phase8_research_utils import ReplayTradeContext
from app.real_strategy_backtester import RealBacktestTrade


def _dump_frame() -> pd.DataFrame:
    rows = []
    price = 110.0
    for i in range(30):
        open_price = price
        close = price - 1.0 if i < 22 else price + 0.8
        high = max(open_price, close) + (0.2 if i != 21 else 1.5)
        low = min(open_price, close) - (0.2 if i != 21 else 2.0)
        rows.append({
            "timestamp": pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=5 * i),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": 2000,
        })
        price = close
    return pd.DataFrame(rows)


def test_entry_exhaustion_classifies_late_short_after_dump():
    frame = _dump_frame()
    trade = RealBacktestTrade(
        symbol="BTCUSDT",
        side="SHORT",
        signal_index=21,
        entry_index=22,
        exit_index=24,
        entry_price=float(frame.iloc[22]["open"]),
        exit_price=float(frame.iloc[24]["close"]),
        stop_loss=float(frame.iloc[22]["open"]) * 1.01,
        take_profit_1=float(frame.iloc[22]["open"]) * 0.98,
        gross_return_pct=-1.0,
        net_return_pct=-1.18,
        exit_reason="STOP_LOSS",
        fee_cost_bps=12.0,
        slippage_cost_bps=6.0,
        funding_component_bps=0.0,
        total_cost_bps=18.0,
    )
    item = analyse_entry_exhaustion_trade(ReplayTradeContext("BTCUSDT", "5m", trade, frame))
    assert item.classification in {LATE_CHASE_ENTRY, REVERSAL_RISK}
    assert "block_late_short" in item.policy_suggestion or "confirmation" in item.policy_suggestion


def test_entry_exhaustion_is_research_only_no_runtime_side_effect():
    frame = _dump_frame()
    trade = RealBacktestTrade(
        symbol="ETHUSDT",
        side="LONG",
        signal_index=10,
        entry_index=11,
        exit_index=15,
        entry_price=100,
        exit_price=101,
        stop_loss=98,
        take_profit_1=102,
        gross_return_pct=1,
        net_return_pct=0.82,
        exit_reason="TAKE_PROFIT",
        fee_cost_bps=12,
        slippage_cost_bps=6,
        funding_component_bps=0,
        total_cost_bps=18,
    )
    item = analyse_entry_exhaustion_trade(ReplayTradeContext("ETHUSDT", "5m", trade, frame))
    assert item.policy_suggestion
    assert "runtime" in item.policy_suggestion or "confirmation" in item.policy_suggestion or "block" in item.policy_suggestion
