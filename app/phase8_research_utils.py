from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .ohlcv_replay_loader import OhlcvReplayLoader
from .real_strategy_backtester import DEFAULT_BACKTESTER_SYMBOLS, RealBacktestTrade, RealStrategyBacktester
from .utils import safe_float


FINAL_RECOMMENDATION = "NO LIVE"
NO_LOOKAHEAD_STATUS = "OK_PREFIX_ONLY"
STOP_TP_SAME_BAR_RULE = "STOP_BEFORE_TP"


@dataclass
class ReplayTradeContext:
    symbol: str
    timeframe: str
    trade: RealBacktestTrade
    candles: pd.DataFrame
    regime: str = "UNKNOWN"
    source: str = "trade_signal"


@dataclass
class ReplayLoadBundle:
    contexts: list[ReplayTradeContext]
    loader_statuses: dict[str, str]
    warnings: list[str]
    hours: int
    timeframe: str
    symbols: list[str]


def parse_symbols(symbols: str | list[str] | tuple[str, ...] | None, config: Any | None = None) -> list[str]:
    if isinstance(symbols, str):
        values = [part.strip().upper() for part in symbols.split(",") if part.strip()]
    elif symbols:
        values = [str(part).strip().upper() for part in symbols if str(part).strip()]
    else:
        values = []
    if values:
        return values
    cfg_symbols = list(getattr(config, "symbols", None) or [])
    if cfg_symbols:
        return [str(symbol).strip().upper() for symbol in cfg_symbols if str(symbol).strip()]
    return list(DEFAULT_BACKTESTER_SYMBOLS)


def load_replay_trade_contexts(
    config: Any,
    db: Any,
    *,
    hours: int = 72,
    timeframe: str = "5m",
    symbols: str | list[str] | tuple[str, ...] | None = None,
    max_holding_bars: int = 30,
) -> ReplayLoadBundle:
    resolved = parse_symbols(symbols, config)
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours or 1)))
    loader = OhlcvReplayLoader(db)
    backtester = RealStrategyBacktester(config)
    contexts: list[ReplayTradeContext] = []
    statuses: dict[str, str] = {}
    warnings: list[str] = []
    notional = float(getattr(config, "trade_margin_usdt", 12.0)) * max(1, int(getattr(config, "default_leverage", 1)))
    min_notional = float(getattr(config, "min_trade_margin_usdt", 5.0))

    for symbol in resolved:
        try:
            loaded = loader.load_ohlcv(symbols=[symbol], timeframe=timeframe, since=since)
        except Exception as exc:  # pragma: no cover - defensive read-only guard
            statuses[symbol] = "LOADER_ERROR"
            warnings.append(f"{symbol}:loader_error:{str(exc)[:120]}")
            continue
        statuses[symbol] = loaded.status
        if loaded.status not in {"OK", "TOO_MANY_GAPS"} or symbol not in loaded.frames_by_symbol:
            if loaded.warnings:
                warnings.extend(f"{symbol}:{warning}" for warning in loaded.warnings[:3])
            continue
        frame = loaded.frames_by_symbol[symbol].reset_index(drop=True)
        try:
            result = backtester.run(
                symbol,
                frame,
                min_order_value_usdt=min_notional,
                notional_usdt=notional,
                max_holding_bars=max_holding_bars,
            )
        except Exception as exc:  # pragma: no cover - defensive read-only guard
            statuses[symbol] = "RUN_ERROR"
            warnings.append(f"{symbol}:run_error:{str(exc)[:120]}")
            continue
        contexts.extend(
            ReplayTradeContext(symbol=symbol, timeframe=timeframe, trade=trade, candles=frame)
            for trade in result.trades
        )
    return ReplayLoadBundle(
        contexts=contexts,
        loader_statuses=statuses,
        warnings=warnings,
        hours=int(hours),
        timeframe=str(timeframe or "5m").lower(),
        symbols=resolved,
    )


def side_direction(side: str) -> int:
    return 1 if str(side or "").upper() == "LONG" else -1


def gross_return_pct(side: str, entry_price: float, exit_price: float) -> float:
    if entry_price <= 0 or exit_price <= 0:
        return 0.0
    return ((exit_price - entry_price) / entry_price * 100.0) * side_direction(side)


def favorable_adverse_from_price(side: str, reference_price: float, high: float, low: float) -> tuple[float, float]:
    if reference_price <= 0:
        return 0.0, 0.0
    if str(side or "").upper() == "LONG":
        favorable = max(0.0, (high - reference_price) / reference_price * 100.0)
        adverse = max(0.0, (reference_price - low) / reference_price * 100.0)
    else:
        favorable = max(0.0, (reference_price - low) / reference_price * 100.0)
        adverse = max(0.0, (high - reference_price) / reference_price * 100.0)
    return favorable, adverse


def max_drawdown(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        equity += safe_float(value)
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return abs(worst)


def net_pf(values: list[float]) -> float:
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    loss_sum = abs(sum(losses))
    gain_sum = sum(wins)
    if loss_sum > 0:
        return gain_sum / loss_sum
    return 999.0 if gain_sum > 0 else 0.0


def summarise_returns(values: list[float]) -> dict[str, float]:
    if not values:
        return {"trades": 0, "net_ev": 0.0, "net_pf": 0.0, "win_rate": 0.0, "max_drawdown": 0.0}
    wins = [value for value in values if value > 0]
    return {
        "trades": float(len(values)),
        "net_ev": sum(values) / len(values),
        "net_pf": net_pf(values),
        "win_rate": len(wins) / len(values),
        "max_drawdown": max_drawdown(values),
    }


def prior_side_move_pct(candles: pd.DataFrame, entry_index: int, side: str, bars: int) -> float:
    start = max(0, entry_index - int(bars))
    if entry_index <= start or entry_index >= len(candles):
        return 0.0
    start_close = safe_float(candles.iloc[start].get("close"))
    entry_open = safe_float(candles.iloc[entry_index].get("open"))
    if start_close <= 0 or entry_open <= 0:
        return 0.0
    return gross_return_pct(side, start_close, entry_open)


def average_range_pct(candles: pd.DataFrame, end_index: int, bars: int = 14) -> float:
    start = max(0, end_index - int(bars))
    ranges: list[float] = []
    for _, row in candles.iloc[start:end_index].iterrows():
        close = safe_float(row.get("close"))
        if close <= 0:
            continue
        ranges.append((safe_float(row.get("high")) - safe_float(row.get("low"))) / close * 100.0)
    return sum(ranges) / max(len(ranges), 1)


def same_bar_stop_before_tp(side: str, high: float, low: float, stop: float, take_profit: float) -> tuple[bool, bool, bool]:
    side_upper = str(side or "").upper()
    if side_upper == "LONG":
        stop_hit = low <= stop
        tp_hit = high >= take_profit
    else:
        stop_hit = high >= stop
        tp_hit = low <= take_profit
    return stop_hit, tp_hit, bool(stop_hit and tp_hit)
