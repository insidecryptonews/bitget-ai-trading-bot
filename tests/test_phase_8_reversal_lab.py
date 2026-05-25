from __future__ import annotations

import pandas as pd

from app.phase8_research_utils import ReplayTradeContext
from app.real_strategy_backtester import RealBacktestTrade
from app.reversal_candidate_lab import RESEARCH_PROMISING_NOT_ACTIONABLE, WATCH_ONLY, analyse_reversal_candidate


def test_reversal_lab_never_auto_flips_and_can_find_research_candidate():
    rows = []
    price = 100.0
    for i in range(16):
        if i <= 6:
            close = price - 0.2
        else:
            close = price + 0.8
        rows.append({
            "timestamp": pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=5 * i),
            "open": price,
            "high": max(price, close) + 0.2,
            "low": min(price, close) - 0.2,
            "close": close,
            "volume": 1000,
        })
        price = close
    frame = pd.DataFrame(rows)
    trade = RealBacktestTrade(
        symbol="BTCUSDT",
        side="SHORT",
        signal_index=2,
        entry_index=3,
        exit_index=6,
        entry_price=99.5,
        exit_price=100.5,
        stop_loss=100.5,
        take_profit_1=97.5,
        gross_return_pct=-1.0,
        net_return_pct=-1.18,
        exit_reason="STOP_LOSS",
        fee_cost_bps=12,
        slippage_cost_bps=6,
        funding_component_bps=0,
        total_cost_bps=18,
    )
    item = analyse_reversal_candidate(ReplayTradeContext("BTCUSDT", "5m", trade, frame))
    assert item.auto_flip is False
    assert item.decision in {WATCH_ONLY, RESEARCH_PROMISING_NOT_ACTIONABLE}
    assert item.opposite_side == "LONG"
