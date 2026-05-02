from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .indicators import add_indicators


@dataclass
class BacktestMetrics:
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    sharpe_approx: float = 0.0
    trades: int = 0
    pnl_total: float = 0.0
    pnl_pct: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    max_losing_streak: int = 0
    by_symbol: dict[str, float] = field(default_factory=dict)
    by_strategy: dict[str, float] = field(default_factory=dict)
    estimated_fees: float = 0.0
    estimated_slippage: float = 0.0


class Backtester:
    """Simple vector backtester for sanity checks before live.

    It is intentionally conservative: entry is next candle open, fees and
    slippage are charged both ways, and each trade uses a fixed risk fraction.
    """

    def __init__(self, starting_capital: float = 40.0, fee_rate: float = 0.0006, slippage_rate: float = 0.0003) -> None:
        self.starting_capital = starting_capital
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate

    def run_trend_breakout_baseline(self, df: pd.DataFrame, symbol: str = "BTCUSDT") -> BacktestMetrics:
        data = add_indicators(df).dropna().reset_index(drop=True)
        capital = self.starting_capital
        equity_curve = [capital]
        trade_pnls: list[float] = []
        losing_streak = 0
        max_losing_streak = 0
        fees_total = 0.0
        slippage_total = 0.0

        for i in range(60, len(data) - 1):
            row = data.iloc[i]
            next_open = float(data.iloc[i + 1]["open"])
            side = None
            if row["close"] > row["range_high_30"] and row["volume_relative"] > 1.5 and row["macd_hist"] > 0:
                side = "LONG"
            elif row["close"] < row["range_low_30"] and row["volume_relative"] > 1.5 and row["macd_hist"] < 0:
                side = "SHORT"
            elif row["close"] > row["ema_21"] > row["ema_50"] and 45 <= row["rsi_14"] <= 68:
                side = "LONG"
            elif row["close"] < row["ema_21"] < row["ema_50"] and 32 <= row["rsi_14"] <= 55:
                side = "SHORT"
            if not side:
                continue

            atr = float(row["atr_14"])
            stop = next_open - atr * 1.4 if side == "LONG" else next_open + atr * 1.4
            tp = next_open + abs(next_open - stop) * 1.7 if side == "LONG" else next_open - abs(next_open - stop) * 1.7
            size = (capital * 0.02) / abs(next_open - stop)
            exit_price = None
            for j in range(i + 1, min(i + 30, len(data))):
                candle = data.iloc[j]
                if side == "LONG":
                    if candle["low"] <= stop:
                        exit_price = stop
                        break
                    if candle["high"] >= tp:
                        exit_price = tp
                        break
                else:
                    if candle["high"] >= stop:
                        exit_price = stop
                        break
                    if candle["low"] <= tp:
                        exit_price = tp
                        break
            if exit_price is None:
                exit_price = float(data.iloc[min(i + 29, len(data) - 1)]["close"])

            direction = 1 if side == "LONG" else -1
            gross = (exit_price - next_open) * size * direction
            notional = next_open * size
            fees = notional * self.fee_rate * 2
            slip = notional * self.slippage_rate
            pnl = gross - fees - slip
            capital += pnl
            fees_total += fees
            slippage_total += slip
            trade_pnls.append(pnl)
            equity_curve.append(capital)
            losing_streak = losing_streak + 1 if pnl < 0 else 0
            max_losing_streak = max(max_losing_streak, losing_streak)

        if not trade_pnls:
            return BacktestMetrics(by_symbol={symbol: 0.0})
        wins = [p for p in trade_pnls if p > 0]
        losses = [p for p in trade_pnls if p < 0]
        returns = np.diff(equity_curve) / np.array(equity_curve[:-1])
        peak = np.maximum.accumulate(equity_curve)
        drawdowns = (np.array(equity_curve) - peak) / peak
        profit_factor = sum(wins) / abs(sum(losses)) if losses else float("inf")
        sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(252)) if len(returns) > 2 and np.std(returns) else 0.0
        pnl_total = capital - self.starting_capital
        return BacktestMetrics(
            win_rate=len(wins) / len(trade_pnls),
            profit_factor=profit_factor,
            max_drawdown=float(abs(drawdowns.min())),
            sharpe_approx=sharpe,
            trades=len(trade_pnls),
            pnl_total=pnl_total,
            pnl_pct=pnl_total / self.starting_capital,
            best_trade=max(trade_pnls),
            worst_trade=min(trade_pnls),
            max_losing_streak=max_losing_streak,
            by_symbol={symbol: pnl_total},
            by_strategy={"baseline_trend_breakout": pnl_total},
            estimated_fees=fees_total,
            estimated_slippage=slippage_total,
        )

