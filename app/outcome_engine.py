"""Common outcome simulation for measurement parity.

PaperTrader currently uses last_price for TP/SL detection; backtester and
TripleBarrierLabeler use high/low intra-bar. This module provides a single
authoritative `simulate_outcome` function that both can converge on (eventually),
plus a `compare_outcomes` helper so we can measure the gap before changing
runtime.

NO RUNTIME HOOK in this module. Pure library.
NO order placement.
NO exchange call.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Any

import pandas as pd

from .cost_model import explain_cost_breakdown
from .utils import safe_float


EXIT_TAKE_PROFIT = "TAKE_PROFIT"
EXIT_STOP_LOSS = "STOP_LOSS"
EXIT_HORIZON_CLOSE = "HORIZON_CLOSE"
EXIT_QUICK_PROFIT = "QUICK_PROFIT"
EXIT_BREAKEVEN = "BREAKEVEN"
EXIT_TRAILING = "TRAILING"
EXIT_UNKNOWN = "UNKNOWN"

KNOWN_EXIT_REASONS = {
    EXIT_TAKE_PROFIT,
    EXIT_STOP_LOSS,
    EXIT_HORIZON_CLOSE,
    EXIT_QUICK_PROFIT,
    EXIT_BREAKEVEN,
    EXIT_TRAILING,
    EXIT_UNKNOWN,
}

SAME_BAR_RULE_STOP_BEFORE_TP = "STOP_BEFORE_TP"


@dataclass
class OutcomeResult:
    """Single outcome from a simulated trade.

    All percentages are in PERCENT (e.g. 1.50 means 1.50%), not fractions.
    """

    side: str
    entry_price: float
    exit_price: float
    exit_reason: str
    gross_return_pct: float
    net_return_pct: float
    fee_cost_bps: float
    slippage_cost_bps: float
    funding_component_bps: float
    total_cost_bps: float
    mfe: float
    mae: float
    bars_to_outcome: int
    same_bar_stop_tp_applied: bool
    same_bar_rule: str
    entry_timestamp: str
    exit_timestamp: str
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ts_to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _direction(side: str) -> int:
    return 1 if str(side or "").upper() == "LONG" else -1


def _validate_side(side: str) -> str:
    text = str(side or "").upper().strip()
    if text not in {"LONG", "SHORT"}:
        raise ValueError(f"OutcomeEngine: side must be LONG or SHORT, got '{side}'")
    return text


def simulate_outcome_ohlcv(
    *,
    side: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    candles: pd.DataFrame,
    max_holding_bars: int = 30,
    cost_source: str = "trade_signal",
    slippage_bps: float = 3.0,
    funding_rate: Any = None,
    entry_timestamp: Any = None,
    quick_profit_threshold_pct: float | None = None,
    breakeven_after_pct: float | None = None,
) -> OutcomeResult:
    """High/low intra-bar outcome simulation — backtest/labeler-grade.

    Iterates bar-by-bar over `candles`, tracking MFE/MAE and resolving
    TP/SL/QUICK_PROFIT/BREAKEVEN/HORIZON_CLOSE. Applies STOP_BEFORE_TP rule
    when both touch in the same bar (worst case for the trader).

    Optional:
        quick_profit_threshold_pct: if MFE reaches this pct in price-move terms,
            simulate a QUICK_PROFIT exit at threshold price.
        breakeven_after_pct: if MFE reaches this pct, move stop to entry.

    Parameters
    ----------
    side : 'LONG' | 'SHORT'
    candles : DataFrame with columns timestamp/open/high/low/close (must be
        AFTER entry — caller is responsible for slicing).
    """
    side = _validate_side(side)
    if entry_price <= 0:
        raise ValueError("OutcomeEngine: entry_price must be > 0")
    if stop_loss <= 0 or take_profit <= 0:
        raise ValueError("OutcomeEngine: stop_loss and take_profit must be > 0")
    if candles is None or candles.empty:
        raise ValueError("OutcomeEngine: candles required for OHLCV simulation")

    direction = _direction(side)
    entry_ts = _ts_to_str(entry_timestamp)
    notes: list[str] = []

    current_stop = stop_loss
    exit_price = 0.0
    exit_reason = EXIT_UNKNOWN
    exit_index = -1
    same_bar = False
    same_bar_rule = SAME_BAR_RULE_STOP_BEFORE_TP if (side == "LONG" and stop_loss < take_profit) or (side == "SHORT" and stop_loss > take_profit) else ""

    mfe = 0.0
    mae = 0.0

    horizon = min(len(candles), max(1, int(max_holding_bars or 0)))
    for index in range(horizon):
        row = candles.iloc[index]
        high = safe_float(row.get("high"))
        low = safe_float(row.get("low"))
        if high <= 0 or low <= 0:
            continue

        if side == "LONG":
            mfe = max(mfe, (high - entry_price) / entry_price * 100.0)
            mae = min(mae, (low - entry_price) / entry_price * 100.0)
            stop_hit = low <= current_stop
            tp_hit = high >= take_profit
            qp_hit = (
                quick_profit_threshold_pct is not None
                and quick_profit_threshold_pct > 0
                and mfe >= quick_profit_threshold_pct
            )
            should_move_to_be = (
                breakeven_after_pct is not None
                and breakeven_after_pct > 0
                and mfe >= breakeven_after_pct
                and current_stop < entry_price
            )
        else:
            mfe = max(mfe, (entry_price - low) / entry_price * 100.0)
            mae = min(mae, (entry_price - high) / entry_price * 100.0)
            stop_hit = high >= current_stop
            tp_hit = low <= take_profit
            qp_hit = (
                quick_profit_threshold_pct is not None
                and quick_profit_threshold_pct > 0
                and mfe >= quick_profit_threshold_pct
            )
            should_move_to_be = (
                breakeven_after_pct is not None
                and breakeven_after_pct > 0
                and mfe >= breakeven_after_pct
                and current_stop > entry_price
            )

        # STOP_BEFORE_TP same-bar worst case
        if stop_hit and tp_hit:
            exit_price = current_stop
            exit_reason = EXIT_STOP_LOSS
            exit_index = index
            same_bar = True
            notes.append("same_bar_stop_tp_applied_stop_first")
            break

        if stop_hit:
            exit_price = current_stop
            exit_reason = EXIT_STOP_LOSS
            exit_index = index
            break

        # Quick profit fires before structural TP if threshold lower
        if qp_hit:
            # Exit price approximation: entry +/- threshold pct
            if side == "LONG":
                exit_price = entry_price * (1.0 + quick_profit_threshold_pct / 100.0)
            else:
                exit_price = entry_price * (1.0 - quick_profit_threshold_pct / 100.0)
            exit_reason = EXIT_QUICK_PROFIT
            exit_index = index
            notes.append(f"quick_profit_at_{quick_profit_threshold_pct:.4f}pct")
            break

        if tp_hit:
            exit_price = take_profit
            exit_reason = EXIT_TAKE_PROFIT
            exit_index = index
            break

        if should_move_to_be:
            current_stop = entry_price
            notes.append(f"breakeven_after_{breakeven_after_pct:.4f}pct")

    if exit_index < 0:
        # Horizon close — use close of last bar in horizon
        last_idx = horizon - 1
        exit_price = safe_float(candles.iloc[last_idx].get("close"))
        exit_reason = EXIT_HORIZON_CLOSE
        exit_index = last_idx

    gross_return_pct = ((exit_price - entry_price) / entry_price * 100.0) * direction

    breakdown = explain_cost_breakdown(
        source=cost_source,
        side=side,
        entry_type="taker",
        exit_type="taker",
        slippage_bps=slippage_bps,
        entry_time=entry_timestamp,
        exit_time=candles.iloc[exit_index].get("timestamp") if "timestamp" in candles.columns else None,
        holding_bars=exit_index + 1,
        bar_minutes=5,
        funding_rate=funding_rate,
        outcome=exit_reason,
    )
    net_return_pct = gross_return_pct - breakdown.total_cost_bps / 100.0

    return OutcomeResult(
        side=side,
        entry_price=entry_price,
        exit_price=exit_price,
        exit_reason=exit_reason,
        gross_return_pct=gross_return_pct,
        net_return_pct=net_return_pct,
        fee_cost_bps=breakdown.fee_component_bps,
        slippage_cost_bps=breakdown.slippage_component_bps,
        funding_component_bps=breakdown.funding_component_bps,
        total_cost_bps=breakdown.total_cost_bps,
        mfe=mfe,
        mae=mae,
        bars_to_outcome=exit_index + 1,
        same_bar_stop_tp_applied=same_bar,
        same_bar_rule=same_bar_rule,
        entry_timestamp=entry_ts,
        exit_timestamp=_ts_to_str(candles.iloc[exit_index].get("timestamp") if "timestamp" in candles.columns else None),
        notes=notes,
    )


def simulate_outcome_last_price(
    *,
    side: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    last_prices: list[float],
    max_holding_bars: int = 30,
    cost_source: str = "trade_signal",
    slippage_bps: float = 3.0,
    funding_rate: Any = None,
    entry_timestamp: Any = None,
) -> OutcomeResult:
    """Last-price outcome simulation — PaperTrader.monitor() semantics.

    Mirrors what the current paper runtime does: a stream of observed last
    prices, no intra-bar high/low. Used to MEASURE the gap vs OHLCV outcome,
    not to change PaperTrader behavior.
    """
    side = _validate_side(side)
    if entry_price <= 0:
        raise ValueError("OutcomeEngine: entry_price must be > 0")
    if not last_prices:
        raise ValueError("OutcomeEngine: last_prices required")

    direction = _direction(side)
    entry_ts = _ts_to_str(entry_timestamp)
    exit_price = 0.0
    exit_reason = EXIT_UNKNOWN
    exit_index = -1
    mfe = 0.0
    mae = 0.0

    horizon = min(len(last_prices), max(1, int(max_holding_bars or 0)))
    for index in range(horizon):
        price = safe_float(last_prices[index])
        if price <= 0:
            continue
        if side == "LONG":
            mfe = max(mfe, (price - entry_price) / entry_price * 100.0)
            mae = min(mae, (price - entry_price) / entry_price * 100.0)
            if price <= stop_loss:
                exit_price = price
                exit_reason = EXIT_STOP_LOSS
                exit_index = index
                break
            if price >= take_profit:
                exit_price = price
                exit_reason = EXIT_TAKE_PROFIT
                exit_index = index
                break
        else:
            mfe = max(mfe, (entry_price - price) / entry_price * 100.0)
            mae = min(mae, (entry_price - price) / entry_price * 100.0)
            if price >= stop_loss:
                exit_price = price
                exit_reason = EXIT_STOP_LOSS
                exit_index = index
                break
            if price <= take_profit:
                exit_price = price
                exit_reason = EXIT_TAKE_PROFIT
                exit_index = index
                break

    if exit_index < 0:
        exit_price = safe_float(last_prices[horizon - 1])
        exit_reason = EXIT_HORIZON_CLOSE
        exit_index = horizon - 1

    gross_return_pct = ((exit_price - entry_price) / entry_price * 100.0) * direction
    breakdown = explain_cost_breakdown(
        source=cost_source,
        side=side,
        entry_type="taker",
        exit_type="taker",
        slippage_bps=slippage_bps,
        entry_time=entry_timestamp,
        exit_time=None,
        holding_bars=exit_index + 1,
        bar_minutes=5,
        funding_rate=funding_rate,
        outcome=exit_reason,
    )
    net_return_pct = gross_return_pct - breakdown.total_cost_bps / 100.0

    return OutcomeResult(
        side=side,
        entry_price=entry_price,
        exit_price=exit_price,
        exit_reason=exit_reason,
        gross_return_pct=gross_return_pct,
        net_return_pct=net_return_pct,
        fee_cost_bps=breakdown.fee_component_bps,
        slippage_cost_bps=breakdown.slippage_component_bps,
        funding_component_bps=breakdown.funding_component_bps,
        total_cost_bps=breakdown.total_cost_bps,
        mfe=mfe,
        mae=mae,
        bars_to_outcome=exit_index + 1,
        same_bar_stop_tp_applied=False,
        same_bar_rule="",
        entry_timestamp=entry_ts,
        exit_timestamp="",
        notes=["last_price_simulation_no_intrabar"],
    )


def compare_outcomes(ohlcv: OutcomeResult, last_price: OutcomeResult) -> dict[str, Any]:
    """Quantify the gap between OHLCV-grade and last-price-grade simulations."""
    return {
        "exit_reason_match": ohlcv.exit_reason == last_price.exit_reason,
        "ohlcv_exit_reason": ohlcv.exit_reason,
        "last_price_exit_reason": last_price.exit_reason,
        "gross_return_diff_pct": round(ohlcv.gross_return_pct - last_price.gross_return_pct, 6),
        "net_return_diff_pct": round(ohlcv.net_return_pct - last_price.net_return_pct, 6),
        "mfe_diff": round(ohlcv.mfe - last_price.mfe, 6),
        "mae_diff": round(ohlcv.mae - last_price.mae, 6),
        "bars_diff": ohlcv.bars_to_outcome - last_price.bars_to_outcome,
        "ohlcv_captures_wick": ohlcv.exit_reason in {EXIT_STOP_LOSS, EXIT_TAKE_PROFIT}
        and last_price.exit_reason == EXIT_HORIZON_CLOSE,
        "research_only": True,
        "no_runtime_change": True,
    }
