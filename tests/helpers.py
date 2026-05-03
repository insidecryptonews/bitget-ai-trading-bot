from datetime import datetime, timedelta, timezone

import pandas as pd

from app.market_data import MarketSnapshot
from app.signal_engine import Signal


class DummyLogger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def signal(side="LONG"):
    return Signal(
        symbol="BTCUSDT",
        side=side,
        strategy_type="BREAKOUT",
        confidence_score=88,
        entry_price=100.0,
        stop_loss=98.0 if side == "LONG" else 102.0,
        take_profit_1=103.0 if side == "LONG" else 97.0,
        take_profit_2=105.0 if side == "LONG" else 95.0,
        trailing_stop_enabled=True,
        trailing_stop_rule="ATR",
        risk_reward_ratio=1.5,
        leverage_recommendation=3,
        position_size=0,
        reason="test",
        confirmations=["trend", "volume", "rr"],
        warnings=[],
        timeframe_alignment="5m=bullish,15m=bullish,1h=neutral",
    )


def candles(prices):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index, (open_, high, low, close) in enumerate(prices):
        rows.append(
            {
                "timestamp": now + timedelta(minutes=5 * index),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": 100,
                "rsi_14": 55,
                "macd_hist": 0.1,
                "atr_14": 1.0,
                "normalized_atr": 0.01,
                "volume_relative": 1.5,
                "ema_21": 99,
                "ema_50": 98,
                "ema_200": 95,
                "distance_to_ema_200": 0.05,
                "momentum_5": 0.01,
                "momentum_15": 0.02,
                "range_width_pct": 0.03,
                "body_pct": 0.004,
                "upper_wick_pct": 0.002,
                "lower_wick_pct": 0.003,
                "bullish_rejection": False,
                "bearish_rejection": False,
            }
        )
    return pd.DataFrame(rows)


def snapshot():
    frame = candles([(100, 101, 99, 100), (100, 102, 99.5, 101)])
    return MarketSnapshot(
        symbol="BTCUSDT",
        candles={"5m": frame, "15m": frame},
        current_price=100,
        spread_pct=0.0005,
        volume_24h_usdt=100_000_000,
        funding_rate=0.0001,
        open_interest=10_000,
    )
