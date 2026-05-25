from __future__ import annotations

import pandas as pd

from app.real_strategy_backtester import RealBacktestTrade
from app.phase8_research_utils import ReplayTradeContext
from app.time_exit_autopsy_v2 import (
    CORRECT_TIME_EXIT_AVOIDED_LOSS,
    PREMATURE_TIME_EXIT_PROFIT_MISSED,
    analyse_time_exit_trade,
)


def _candles(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "timestamp": pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=5 * i),
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": 1000,
        }
        for i, (o, h, l, c) in enumerate(rows)
    ])


def _trade(side: str = "LONG", exit_reason: str = "HORIZON_CLOSE") -> RealBacktestTrade:
    return RealBacktestTrade(
        symbol="BTCUSDT",
        side=side,
        signal_index=1,
        entry_index=2,
        exit_index=4,
        entry_price=100.0,
        exit_price=100.0,
        stop_loss=98.0 if side == "LONG" else 102.0,
        take_profit_1=102.0 if side == "LONG" else 98.0,
        gross_return_pct=0.0,
        net_return_pct=-0.15,
        exit_reason=exit_reason,
        fee_cost_bps=12.0,
        slippage_cost_bps=6.0,
        funding_component_bps=0.0,
        total_cost_bps=18.0,
    )


def test_time_exit_autopsy_detects_premature_profit_missed_long():
    frame = _candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 103, 99.5, 102.5),
        (102, 103, 101, 102),
    ])
    item = analyse_time_exit_trade(ReplayTradeContext("BTCUSDT", "5m", _trade("LONG"), frame))
    assert item.classification == PREMATURE_TIME_EXIT_PROFIT_MISSED
    assert item.would_tp_if_held is True
    assert item.counterfactual_only is True
    assert item.no_lookahead_status == "OK_PREFIX_ONLY"


def test_time_exit_autopsy_stop_before_tp_same_bar_counterfactual():
    frame = _candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 103, 97, 101),
    ])
    item = analyse_time_exit_trade(ReplayTradeContext("BTCUSDT", "5m", _trade("LONG"), frame))
    assert item.classification == CORRECT_TIME_EXIT_AVOIDED_LOSS
    assert item.would_sl_if_held is True
    assert item.would_tp_if_held is False
    assert item.bars_until_sl == 1


def test_time_exit_autopsy_short_symmetry_detects_missed_profit():
    frame = _candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 100.5, 97.5, 98.0),
    ])
    item = analyse_time_exit_trade(ReplayTradeContext("ETHUSDT", "5m", _trade("SHORT"), frame))
    assert item.classification == PREMATURE_TIME_EXIT_PROFIT_MISSED
    assert item.would_tp_if_held is True
