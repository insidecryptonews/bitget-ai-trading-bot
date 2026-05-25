"""Trade Replay Export — JSON payload for future chart/UI consumption.

Produces a structured JSON describing:
- OHLCV candles for a symbol + timeframe + range,
- simulated trades (entry/exit/SL/TP/outcome) from the real backtester.

NEVER places orders. NEVER touches exchange. NEVER reads private endpoints.
The output is meant to be consumed by a future SVG/Canvas/Chart.js front-end
that the dashboard team can plug in without touching the backend again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .ohlcv_replay_loader import OhlcvReplayLoader
from .real_strategy_backtester import RealStrategyBacktester
from .signal_engine import SignalEngine
from .utils import safe_float, safe_int


FINAL_RECOMMENDATION = "NO LIVE"


@dataclass
class ReplayCandle:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReplayTrade:
    symbol: str
    side: str
    entry_index: int
    entry_time: str
    entry_price: float
    exit_index: int
    exit_time: str
    exit_price: float
    stop_loss: float
    take_profit_1: float
    exit_reason: str
    gross_return_pct: float
    net_return_pct: float
    same_bar_stop_tp_applied: bool
    # Phase 7B extensions for dashboard/chart consumption.
    duration_bars: int = 0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    score: int = 0
    regime: str = ""
    signal_type: str = ""
    setup_key: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReplayPayload:
    symbol: str
    timeframe: str
    hours: int
    candles: list[ReplayCandle] = field(default_factory=list)
    trades: list[ReplayTrade] = field(default_factory=list)
    contract: dict[str, Any] = field(default_factory=dict)
    final_recommendation: str = FINAL_RECOMMENDATION
    research_only: bool = True
    real_orders: bool = False
    exchange_calls: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["candles"] = [c.as_dict() for c in self.candles]
        payload["trades"] = [t.as_dict() for t in self.trades]
        return payload


def _candle_from_row(row: dict[str, Any]) -> ReplayCandle:
    return ReplayCandle(
        timestamp=str(row.get("timestamp") or ""),
        open=safe_float(row.get("open")),
        high=safe_float(row.get("high")),
        low=safe_float(row.get("low")),
        close=safe_float(row.get("close")),
        volume=safe_float(row.get("volume")),
    )


def build_replay_payload(
    config: Any,
    db: Any,
    *,
    symbol: str,
    hours: int = 72,
    timeframe: str = "5m",
    max_candles: int = 1200,
    max_trades: int = 200,
) -> ReplayPayload:
    """Construct a replay payload (candles + simulated trades) for one symbol."""
    symbol = str(symbol or "").upper()
    timeframe = str(timeframe or "5m").lower()
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours or 1)))

    payload = ReplayPayload(
        symbol=symbol, timeframe=timeframe, hours=int(hours),
        contract={
            "uses_signal_engine": True,
            "no_lookahead_status": "OK_PREFIX_ONLY",
            "entry_model": "signal_close_i_entry_next_open_i+1",
            "stop_tp_same_bar_rule": "STOP_BEFORE_TP",
            "min_order_rule": "BLOCK_BELOW_MIN_NOTIONAL",
            "no_mfe_mae_fallback": True,
            "exchange_calls": False,
            "real_orders": False,
        },
    )

    loader = OhlcvReplayLoader(db)
    load_result = loader.load_ohlcv(symbols=[symbol], timeframe=timeframe, since=since)
    if load_result.status not in {"OK", "TOO_MANY_GAPS"} or symbol not in load_result.frames_by_symbol:
        return payload

    frame = load_result.frames_by_symbol[symbol]
    # Trim candles to max_candles, keep the most recent.
    if len(frame) > max_candles:
        frame = frame.iloc[-max_candles:].reset_index(drop=True)

    for _, row in frame.iterrows():
        payload.candles.append(_candle_from_row(row.to_dict()))

    backtester = RealStrategyBacktester(config)
    result = backtester.run(
        symbol, frame,
        min_order_value_usdt=float(getattr(config, "min_trade_margin_usdt", 5.0)),
        notional_usdt=float(getattr(config, "trade_margin_usdt", 12.0)) * max(1, int(getattr(config, "default_leverage", 1))),
    )
    trades_iter = result.trades[-max_trades:] if len(result.trades) > max_trades else result.trades
    for trade in trades_iter:
        try:
            entry_row = frame.iloc[trade.entry_index]
            exit_row = frame.iloc[min(trade.exit_index, len(frame) - 1)]
        except IndexError:
            continue
        mfe_pct, mae_pct = _mfe_mae_pct(frame, trade)
        payload.trades.append(ReplayTrade(
            symbol=symbol,
            side=str(trade.side),
            entry_index=safe_int(trade.entry_index),
            entry_time=str(entry_row.get("timestamp") or ""),
            entry_price=safe_float(trade.entry_price),
            exit_index=safe_int(trade.exit_index),
            exit_time=str(exit_row.get("timestamp") or ""),
            exit_price=safe_float(trade.exit_price),
            stop_loss=safe_float(trade.stop_loss),
            take_profit_1=safe_float(trade.take_profit_1),
            exit_reason=str(trade.exit_reason),
            gross_return_pct=safe_float(trade.gross_return_pct),
            net_return_pct=safe_float(trade.net_return_pct),
            same_bar_stop_tp_applied=bool(trade.same_bar_worst_case_applied),
            duration_bars=safe_int(trade.exit_index) - safe_int(trade.entry_index) + 1,
            mfe_pct=mfe_pct,
            mae_pct=mae_pct,
            score=0,
            regime="",
            signal_type="",
            setup_key="",
        ))

    return payload


def _mfe_mae_pct(frame: pd.DataFrame, trade: Any) -> tuple[float, float]:
    """Compute MFE/MAE from the actual candles spanning entry..exit indices."""
    try:
        entry_price = safe_float(trade.entry_price)
        if entry_price <= 0:
            return 0.0, 0.0
        start = max(0, int(trade.entry_index))
        end = min(len(frame), int(trade.exit_index) + 1)
        window = frame.iloc[start:end]
        if window.empty:
            return 0.0, 0.0
        high_max = safe_float(window["high"].max())
        low_min = safe_float(window["low"].min())
        side = str(getattr(trade, "side", "")).upper()
        if side == "LONG":
            mfe = (high_max - entry_price) / entry_price * 100.0
            mae = (low_min - entry_price) / entry_price * 100.0
        else:
            mfe = (entry_price - low_min) / entry_price * 100.0
            mae = (entry_price - high_max) / entry_price * 100.0
        return mfe, mae
    except Exception:
        return 0.0, 0.0


def export_replay_json(payload: ReplayPayload) -> str:
    return json.dumps(payload.as_dict(), indent=2, default=str)


def render_replay_summary(payload: ReplayPayload) -> str:
    lines = ["TRADE REPLAY EXPORT START"]
    lines.append(f"symbol: {payload.symbol}")
    lines.append(f"timeframe: {payload.timeframe}")
    lines.append(f"hours: {payload.hours}")
    lines.append(f"candles: {len(payload.candles)}")
    lines.append(f"trades: {len(payload.trades)}")
    if payload.candles:
        lines.append(f"first_candle: {payload.candles[0].timestamp}")
        lines.append(f"last_candle: {payload.candles[-1].timestamp}")
    if payload.trades:
        wins = sum(1 for t in payload.trades if t.net_return_pct > 0)
        losses = sum(1 for t in payload.trades if t.net_return_pct < 0)
        ties = len(payload.trades) - wins - losses
        lines.append(f"wins: {wins}")
        lines.append(f"losses: {losses}")
        lines.append(f"ties: {ties}")
        lines.append(f"first_trade_entry: {payload.trades[0].entry_time}")
        lines.append(f"last_trade_exit: {payload.trades[-1].exit_time}")
    lines.append("real_orders: false")
    lines.append("exchange_calls: false")
    lines.append("research_only: true")
    lines.append(f"final_recommendation: {payload.final_recommendation}")
    lines.append("TRADE REPLAY EXPORT END")
    return "\n".join(lines)
