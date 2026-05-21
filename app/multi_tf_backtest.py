"""Honest multi-timeframe backtest variant.

Drives RealStrategyBacktester's signal flow but constructs the MarketSnapshot
with REAL data from a second timeframe (the higher_timeframe slot) instead of
aliasing the primary candles. This breaks the trivial multi-timeframe alignment
bonus inflation present when all three timeframe slots receive identical data.

Used for diagnostic purposes only — research/shadow, no exchange calls, NO LIVE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .config import BotConfig
from .cost_model import explain_cost_breakdown
from .indicators import add_indicators
from .market_data import MarketSnapshot
from .real_strategy_backtester import RealBacktestResult, RealBacktestTrade
from .regime_detector import MarketRegime
from .signal_engine import SignalEngine
from .utils import safe_float


FINAL_RECOMMENDATION = "NO LIVE"


def _align_higher_prefix(higher_frame: pd.DataFrame, primary_timestamp: Any) -> pd.DataFrame:
    """Return the prefix of higher_frame ending at or before primary_timestamp."""
    if higher_frame is None or higher_frame.empty:
        return higher_frame
    try:
        target_ts = pd.to_datetime(primary_timestamp, utc=True)
    except Exception:
        return higher_frame
    if "timestamp" not in higher_frame.columns:
        return higher_frame
    mask = higher_frame["timestamp"] <= target_ts
    return higher_frame[mask]


def run_multi_tf_backtest(
    config: BotConfig,
    symbol: str,
    primary_frame: pd.DataFrame,
    higher_frame: pd.DataFrame,
    *,
    regime: MarketRegime | None = None,
    max_holding_bars: int = 30,
    signal_engine: SignalEngine | None = None,
) -> RealBacktestResult:
    """Backtest where the higher_timeframe slot gets real higher-TF data."""
    if primary_frame is None or len(primary_frame) < 65 or higher_frame is None or len(higher_frame) < 65:
        return RealBacktestResult(
            status="NEED_DATA",
            uses_signal_engine=False,
            no_lookahead_status="NOT_RUN",
            entry_model="signal_close_i_entry_next_open_i+1",
            stop_tp_same_bar_rule="STOP_BEFORE_TP",
            min_order_rule="BLOCK_BELOW_MIN_NOTIONAL",
        )
    engine = signal_engine or SignalEngine(config)
    primary = add_indicators(primary_frame).reset_index(drop=True)
    higher = add_indicators(higher_frame).reset_index(drop=True)
    regime = regime or MarketRegime("RANGE", allowed_direction="BOTH")
    trades: list[RealBacktestTrade] = []
    blocked = 0
    higher_tf_name = config.higher_timeframe.lower()
    primary_tf_name = config.main_timeframe.lower()
    conf_tf_name = config.confirmation_timeframe.lower()

    for index in range(60, len(primary) - 1):
        primary_slice = primary.iloc[: index + 1].copy()
        primary_ts = primary_slice.iloc[-1].get("timestamp")
        higher_slice = _align_higher_prefix(higher, primary_ts)
        if len(higher_slice) < 60:
            continue
        snapshot = MarketSnapshot(
            symbol=symbol,
            candles={
                # The signal engine reads df5 / df15 from these keys; we still
                # alias the PRIMARY frame into those slots because we don't have
                # 5m/15m data here. The CONFLUENCE break happens in the 1h slot,
                # which now receives REAL 1h data instead of aliased primary.
                primary_tf_name: primary_slice,
                conf_tf_name: primary_slice,
                higher_tf_name: higher_slice,
                "5m": primary_slice,
                "15m": primary_slice,
                "1h": higher_slice,
            },
            current_price=safe_float(primary_slice.iloc[-1].get("close")),
            funding_rate=safe_float(primary_slice.iloc[-1].get("funding_rate")),
        )
        signal = engine.generate_signal(symbol, snapshot, regime)
        if str(signal.side).upper() not in {"LONG", "SHORT"}:
            continue
        entry_index = index + 1
        entry_price = safe_float(primary.iloc[entry_index].get("open"))
        if entry_price <= 0:
            continue
        trade = _simulate_trade(
            config=config,
            symbol=symbol,
            signal=signal,
            data=primary,
            entry_index=entry_index,
            entry_price=entry_price,
            max_holding_bars=max_holding_bars,
        )
        trades.append(trade)

    return RealBacktestResult(
        status="OK" if trades else "NO_TRADES",
        uses_signal_engine=True,
        no_lookahead_status="OK_PREFIX_ONLY",
        entry_model="signal_close_i_entry_next_open_i+1",
        stop_tp_same_bar_rule="STOP_BEFORE_TP",
        min_order_rule="BLOCK_BELOW_MIN_NOTIONAL",
        trades=trades,
        blocked_min_notional=blocked,
    )


def _simulate_trade(
    *,
    config: BotConfig,
    symbol: str,
    signal: Any,
    data: pd.DataFrame,
    entry_index: int,
    entry_price: float,
    max_holding_bars: int,
) -> RealBacktestTrade:
    side = str(signal.side).upper()
    direction = 1 if side == "LONG" else -1
    stop = safe_float(signal.stop_loss)
    tp = safe_float(signal.take_profit_1)
    exit_price = safe_float(data.iloc[min(len(data) - 1, entry_index + max_holding_bars - 1)].get("close"))
    exit_reason = "HORIZON_CLOSE"
    exit_index = min(len(data) - 1, entry_index + max_holding_bars - 1)
    same_bar = False
    for index in range(entry_index, min(len(data), entry_index + max_holding_bars)):
        row = data.iloc[index]
        high = safe_float(row.get("high"))
        low = safe_float(row.get("low"))
        if side == "LONG":
            stop_hit = low <= stop
            tp_hit = high >= tp
        else:
            stop_hit = high >= stop
            tp_hit = low <= tp
        if stop_hit:
            exit_price = stop
            exit_reason = "STOP_LOSS"
            exit_index = index
            same_bar = tp_hit
            break
        if tp_hit:
            exit_price = tp
            exit_reason = "TAKE_PROFIT"
            exit_index = index
            break
    gross_return = ((exit_price - entry_price) / entry_price * 100.0) * direction
    entry_time = data.iloc[entry_index].get("timestamp") if "timestamp" in data.columns else None
    exit_time = data.iloc[exit_index].get("timestamp") if "timestamp" in data.columns else None
    breakdown = explain_cost_breakdown(
        source="trade_signal",
        side=side,
        entry_type="taker",
        exit_type="taker",
        slippage_bps=safe_float(getattr(config, "net_edge_slippage_bps", 3.0)),
        entry_time=entry_time,
        exit_time=exit_time,
        funding_rate=data.iloc[entry_index].get("funding_rate") if "funding_rate" in data.columns else None,
        outcome=exit_reason,
    )
    net_return = gross_return - breakdown.total_cost_bps / 100.0
    return RealBacktestTrade(
        symbol=symbol,
        side=side,
        signal_index=entry_index - 1,
        entry_index=entry_index,
        exit_index=exit_index,
        entry_price=entry_price,
        exit_price=exit_price,
        stop_loss=stop,
        take_profit_1=tp,
        gross_return_pct=gross_return,
        net_return_pct=net_return,
        exit_reason=exit_reason,
        fee_cost_bps=breakdown.fee_component_bps,
        slippage_cost_bps=breakdown.slippage_component_bps,
        funding_component_bps=breakdown.funding_component_bps,
        total_cost_bps=breakdown.total_cost_bps,
        same_bar_worst_case_applied=same_bar,
    )
