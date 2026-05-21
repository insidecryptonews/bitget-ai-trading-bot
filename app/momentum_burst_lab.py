"""Momentum Burst Lab — RESEARCH ONLY.

Detects sudden price impulses (long pump / short flush) with continuation
probability, plus a backtest framework. Designed for 1m OHLCV but works on
5m as a downgrade.

This module is NOT connected to the trading runtime.
NO order placement.
NO paper filter activation.
NO live activation.

Output: per-bar feature frame + signal series + lab summary metrics.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any, Iterable, Iterator

import numpy as np
import pandas as pd

from .outcome_engine import simulate_outcome_ohlcv, EXIT_HORIZON_CLOSE, EXIT_STOP_LOSS, EXIT_TAKE_PROFIT
from .utils import safe_float


FINAL_RECOMMENDATION = "NO LIVE"


def add_burst_features(df: pd.DataFrame, *, bar_minutes: int = 1) -> pd.DataFrame:
    """Compute microstructure / momentum features WITHOUT lookahead.

    Required columns: open, high, low, close, volume.
    Optional: timestamp (used only for time_of_day; not for features).
    """
    out = df.copy().reset_index(drop=True)
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(out.columns)
    if missing:
        raise ValueError(f"Burst features require columns {sorted(missing)} missing")

    close = out["close"]
    out["return_1m"] = close.pct_change(_bars_for_minutes(1, bar_minutes))
    out["return_3m"] = close.pct_change(_bars_for_minutes(3, bar_minutes))
    out["return_5m"] = close.pct_change(_bars_for_minutes(5, bar_minutes))
    out["return_8m"] = close.pct_change(_bars_for_minutes(8, bar_minutes))
    out["return_15m"] = close.pct_change(_bars_for_minutes(15, bar_minutes))
    # acceleration: latest bar return vs the average bar return inside the recent 5m move.
    # > 0 means the burst is still accelerating; ~0 steady; < 0 decelerating.
    out["acceleration"] = out["return_1m"] - (out["return_5m"] / 5.0)
    # volume context
    out["volume_ma_20"] = out["volume"].rolling(20, min_periods=1).mean()
    out["relative_volume"] = out["volume"] / out["volume_ma_20"].replace(0, np.nan)
    out["volume_spike"] = (out["relative_volume"] >= 2.5).astype(int)
    # candle structure
    body = (out["close"] - out["open"]).abs()
    rng = (out["high"] - out["low"]).replace(0, np.nan)
    out["candle_body_pct"] = body / out["close"].replace(0, np.nan)
    out["upper_wick_pct"] = (out["high"] - out[["open", "close"]].max(axis=1)) / out["close"].replace(0, np.nan)
    out["lower_wick_pct"] = (out[["open", "close"]].min(axis=1) - out["low"]) / out["close"].replace(0, np.nan)
    out["wick_rejection_up"] = ((out["upper_wick_pct"] > out["candle_body_pct"] * 1.5) & (close < out["open"])).astype(int)
    out["wick_rejection_down"] = ((out["lower_wick_pct"] > out["candle_body_pct"] * 1.5) & (close > out["open"])).astype(int)
    # EMAs / distance (re-implemented locally to avoid cross-module dependency)
    out["ema_21"] = close.ewm(span=21, adjust=False).mean()
    out["ema_50"] = close.ewm(span=50, adjust=False).mean()
    out["distance_to_ema_21"] = (close - out["ema_21"]) / out["ema_21"].replace(0, np.nan)
    out["distance_to_ema_50"] = (close - out["ema_50"]) / out["ema_50"].replace(0, np.nan)
    # volatility
    tr = pd.concat([
        out["high"] - out["low"],
        (out["high"] - close.shift()).abs(),
        (out["low"] - close.shift()).abs(),
    ], axis=1).max(axis=1)
    out["atr_14"] = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    out["normalized_atr"] = out["atr_14"] / close.replace(0, np.nan)
    # time of day (informational; not used by the detector itself)
    if "timestamp" in out.columns:
        ts = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
        out["hour_of_day_utc"] = ts.dt.hour
    return out.replace([np.inf, -np.inf], np.nan)


def _bars_for_minutes(minutes: int, bar_minutes: int) -> int:
    bar_minutes = max(1, int(bar_minutes or 1))
    return max(1, int(round(minutes / bar_minutes)))


@dataclass(frozen=True)
class BurstParams:
    """Tunable detection thresholds. Conservative defaults."""

    min_return_5m_pct: float = 0.80          # require recent strong move
    min_acceleration_pct: float = 0.20        # short-term faster than medium-term
    min_relative_volume: float = 2.5          # volume confirmation
    max_distance_ema_21_pct: float = 1.5      # avoid chasing too extended
    require_no_opposite_wick: bool = True     # block bursts with reversal wicks
    require_btc_alignment: bool = False       # set True for production-grade burst
    min_expected_move_to_cost_ratio: float = 3.0   # protect against fee-toxic entries
    cost_round_trip_pct: float = 0.18

    def for_short(self) -> "BurstParams":
        # Same numeric thresholds but signs flip downstream.
        return self


@dataclass
class BurstSignal:
    index: int
    timestamp: Any
    side: str            # 'LONG' or 'SHORT'
    return_5m_pct: float
    acceleration_pct: float
    relative_volume: float
    distance_to_ema_21_pct: float
    upper_wick_pct: float
    lower_wick_pct: float
    expected_move_pct: float
    expected_move_to_cost_ratio: float
    late_entry_risk: bool
    exhaustion_risk: bool
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_long_burst(features: pd.DataFrame, params: BurstParams = BurstParams(), *, suggested_tp_pct: float = 1.2) -> list[BurstSignal]:
    return _detect(features, params, side="LONG", suggested_tp_pct=suggested_tp_pct)


def detect_short_burst(features: pd.DataFrame, params: BurstParams | None = None, *, suggested_tp_pct: float = 1.2) -> list[BurstSignal]:
    params = params or BurstParams()
    return _detect(features, params, side="SHORT", suggested_tp_pct=suggested_tp_pct)


def _detect(features: pd.DataFrame, params: BurstParams, *, side: str, suggested_tp_pct: float) -> list[BurstSignal]:
    side = side.upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    cost_pct = max(params.cost_round_trip_pct, 0.01)
    min_em_ratio = max(params.min_expected_move_to_cost_ratio, 1.0)

    signals: list[BurstSignal] = []
    required = {
        "return_5m", "acceleration", "relative_volume",
        "distance_to_ema_21", "upper_wick_pct", "lower_wick_pct",
    }
    if not required.issubset(set(features.columns)):
        return signals

    for i in range(20, len(features)):
        row = features.iloc[i]
        r5 = safe_float(row.get("return_5m")) * 100.0
        accel = safe_float(row.get("acceleration")) * 100.0
        rvol = safe_float(row.get("relative_volume"))
        d21 = safe_float(row.get("distance_to_ema_21")) * 100.0
        uw = safe_float(row.get("upper_wick_pct")) * 100.0
        lw = safe_float(row.get("lower_wick_pct")) * 100.0
        notes: list[str] = []

        if side == "LONG":
            if r5 < params.min_return_5m_pct:
                continue
            if accel < params.min_acceleration_pct:
                continue
            if rvol < params.min_relative_volume:
                continue
            if abs(d21) > params.max_distance_ema_21_pct:
                notes.append("distance_too_extended")
                continue
            if params.require_no_opposite_wick and uw > 0.6:
                notes.append("upper_wick_rejection")
                continue
        else:
            if r5 > -params.min_return_5m_pct:
                continue
            if accel > -params.min_acceleration_pct:
                continue
            if rvol < params.min_relative_volume:
                continue
            if abs(d21) > params.max_distance_ema_21_pct:
                notes.append("distance_too_extended")
                continue
            if params.require_no_opposite_wick and lw > 0.6:
                notes.append("lower_wick_rejection")
                continue

        # expected_move check
        em_ratio = suggested_tp_pct / cost_pct
        late_entry = abs(r5) > 2.0 * params.min_return_5m_pct
        exhaustion = (
            (side == "LONG" and uw > params.min_acceleration_pct)
            or (side == "SHORT" and lw > params.min_acceleration_pct)
        )
        if em_ratio < min_em_ratio:
            notes.append("expected_move_below_cost_floor")
            continue

        signals.append(BurstSignal(
            index=i,
            timestamp=row.get("timestamp"),
            side=side,
            return_5m_pct=r5,
            acceleration_pct=accel,
            relative_volume=rvol,
            distance_to_ema_21_pct=d21,
            upper_wick_pct=uw,
            lower_wick_pct=lw,
            expected_move_pct=suggested_tp_pct,
            expected_move_to_cost_ratio=em_ratio,
            late_entry_risk=late_entry,
            exhaustion_risk=exhaustion,
            notes=notes,
        ))
    return signals


@dataclass
class BurstBacktestSummary:
    side: str
    signals: int
    trades: int
    gross_ev_pct: float
    net_ev_pct: float
    net_pf: float
    win_rate: float
    max_drawdown_pct: float
    tp_pct: float
    sl_pct: float
    time_pct: float
    avg_hold_bars: float
    cost_round_trip_pct: float
    opportunities_per_day: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def backtest_burst(
    features: pd.DataFrame,
    *,
    side: str,
    params: BurstParams | None = None,
    stop_pct: float = 0.8,
    take_profit_pct: float = 1.2,
    max_holding_bars: int = 30,
    slippage_bps: float = 3.0,
) -> BurstBacktestSummary:
    """Backtest burst signals on a feature-augmented OHLCV frame.

    Entry is on next-open (i+1). Cost stress applied via slippage_bps. No
    lookahead — features at index i depend only on rows <= i.
    """
    params = params or BurstParams()
    detect = detect_long_burst if side.upper() == "LONG" else detect_short_burst
    signals = detect(features, params, suggested_tp_pct=take_profit_pct)

    trades = 0
    outcomes: list[Any] = []
    for sig in signals:
        idx = sig.index
        if idx + 1 >= len(features):
            continue
        entry_row = features.iloc[idx + 1]
        entry_price = safe_float(entry_row.get("open"))
        if entry_price <= 0:
            continue
        if side.upper() == "LONG":
            stop = entry_price * (1.0 - stop_pct / 100.0)
            tp = entry_price * (1.0 + take_profit_pct / 100.0)
        else:
            stop = entry_price * (1.0 + stop_pct / 100.0)
            tp = entry_price * (1.0 - take_profit_pct / 100.0)
        post = features.iloc[idx + 1 :].reset_index(drop=True)
        try:
            outcome = simulate_outcome_ohlcv(
                side=side.upper(),
                entry_price=entry_price,
                stop_loss=stop,
                take_profit=tp,
                candles=post,
                max_holding_bars=max_holding_bars,
                slippage_bps=slippage_bps,
                entry_timestamp=entry_row.get("timestamp"),
            )
        except Exception:
            continue
        outcomes.append(outcome)
        trades += 1

    if not outcomes:
        return BurstBacktestSummary(
            side=side.upper(), signals=len(signals), trades=0,
            gross_ev_pct=0.0, net_ev_pct=0.0, net_pf=0.0, win_rate=0.0,
            max_drawdown_pct=0.0, tp_pct=0.0, sl_pct=0.0, time_pct=0.0,
            avg_hold_bars=0.0, cost_round_trip_pct=params.cost_round_trip_pct,
            opportunities_per_day=0.0,
        )

    gross = [o.gross_return_pct for o in outcomes]
    net = [o.net_return_pct for o in outcomes]
    wins = [v for v in net if v > 0]
    losses = [v for v in net if v < 0]
    tp = sum(1 for o in outcomes if o.exit_reason == EXIT_TAKE_PROFIT)
    sl = sum(1 for o in outcomes if o.exit_reason == EXIT_STOP_LOSS)
    tm = sum(1 for o in outcomes if o.exit_reason == EXIT_HORIZON_CLOSE)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for v in net:
        equity += v
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    # opportunities per day: signals / sample days
    if "timestamp" in features.columns and not features.empty:
        try:
            t0 = pd.to_datetime(features["timestamp"].iloc[0], utc=True)
            t1 = pd.to_datetime(features["timestamp"].iloc[-1], utc=True)
            days = max((t1 - t0).total_seconds() / 86400.0, 1.0)
        except Exception:
            days = 1.0
    else:
        days = 1.0

    return BurstBacktestSummary(
        side=side.upper(),
        signals=len(signals),
        trades=trades,
        gross_ev_pct=sum(gross) / trades,
        net_ev_pct=sum(net) / trades,
        net_pf=(sum(wins) / abs(sum(losses))) if losses else (999.0 if wins else 0.0),
        win_rate=len(wins) / trades,
        max_drawdown_pct=abs(max_dd),
        tp_pct=tp / trades,
        sl_pct=sl / trades,
        time_pct=tm / trades,
        avg_hold_bars=sum(o.bars_to_outcome for o in outcomes) / trades,
        cost_round_trip_pct=params.cost_round_trip_pct,
        opportunities_per_day=len(signals) / days,
    )


def render_summary_text(summary: BurstBacktestSummary) -> str:
    return "\n".join([
        "MOMENTUM BURST LAB SUMMARY START",
        f"side: {summary.side}",
        f"signals: {summary.signals}",
        f"trades: {summary.trades}",
        f"gross_ev_pct: {summary.gross_ev_pct:.4f}",
        f"net_ev_pct: {summary.net_ev_pct:.4f}",
        f"net_pf: {summary.net_pf:.3f}",
        f"win_rate: {summary.win_rate:.3f}",
        f"max_drawdown_pct: {summary.max_drawdown_pct:.3f}",
        f"tp_pct: {summary.tp_pct:.3f}",
        f"sl_pct: {summary.sl_pct:.3f}",
        f"time_pct: {summary.time_pct:.3f}",
        f"avg_hold_bars: {summary.avg_hold_bars:.2f}",
        f"opportunities_per_day: {summary.opportunities_per_day:.3f}",
        f"cost_round_trip_pct: {summary.cost_round_trip_pct:.4f}",
        "research_only: true",
        "no_runtime_change: true",
        "needs_1m_backfill_for_production_quality: true",
        f"final_recommendation: {FINAL_RECOMMENDATION}",
        "MOMENTUM BURST LAB SUMMARY END",
    ])
