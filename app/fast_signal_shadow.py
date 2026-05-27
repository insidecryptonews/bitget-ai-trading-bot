"""Phase 9 — Fast Signal Shadow module.

Inspired by ideas observed in the auxiliary `bitget-futures-signal-lab` project,
but rewritten as a strict research/shadow adapter:

- never opens orders
- never calls exchange-private endpoints
- never reads leverage / margin / sizing config to take action
- always respects the data freshness gate
- always returns shadow-only verdicts

The model produces a `FastSignal` per evaluated context with one of these
actionability states:

  ENTER_NOW                 - inside the entry zone, data fresh
  WAIT                      - price not at the entry zone yet
  LATE                      - price already past the entry zone too much
  OUT_OF_ZONE               - price drifted far from entry, do not chase
  NO_ACTIONABLE_DATA_STALE  - data freshness gate blocked
  NEED_DATA                 - no OHLCV row available
  LOADER_ERROR              - loader failed
  HISTORICAL_RESEARCH_ONLY  - historical window, not live shadow

The leverage value is reported as `simulated_leverage` so dashboards make
clear it is informational only. There is no path in this module that would
call `set_leverage`, `set_margin_mode`, `place_order`, or anything similar.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .data_freshness_gate import FreshnessVerdict, evaluate_freshness
from .phase8_research_utils import (
    FINAL_RECOMMENDATION,
    NO_LOOKAHEAD_STATUS,
    ReplayTradeContext,
    average_range_pct,
    load_replay_trade_contexts,
    parse_symbols,
    side_direction,
)
from .utils import safe_float


ACTIONABILITY_ENTER_NOW = "ENTER_NOW"
ACTIONABILITY_WAIT = "WAIT"
ACTIONABILITY_LATE = "LATE"
ACTIONABILITY_OUT_OF_ZONE = "OUT_OF_ZONE"
ACTIONABILITY_NO_ACTIONABLE_DATA_STALE = "NO_ACTIONABLE_DATA_STALE"
ACTIONABILITY_NEED_DATA = "NEED_DATA"
ACTIONABILITY_LOADER_ERROR = "LOADER_ERROR"
ACTIONABILITY_HISTORICAL = "HISTORICAL_RESEARCH_ONLY"

# Distance thresholds expressed as fractions of the entry → TP distance.
ZONE_ENTER_ABS = 0.25   # |price - entry| <= 25% of TP distance → ENTER_NOW
ZONE_WAIT_NEAR = 0.75   # entry not reached yet but within 75% of TP distance → WAIT
ZONE_LATE = 1.10        # already past TP → LATE
ZONE_OUT = 1.50         # far beyond → OUT_OF_ZONE


@dataclass
class FastSignal:
    symbol: str
    timeframe: str
    side: str
    score: int
    actionability: str
    entry_price: float
    stop_price: float
    tp1: float
    tp2: float
    tp3: float
    current_price: float
    distance_to_entry_pct: float
    distance_to_tp_pct: float
    distance_to_stop_pct: float
    distance_to_entry_atr_x: float
    atr_pct: float
    timestamp: str
    simulated_leverage: int
    research_only: bool = True
    can_send_real_orders: bool = False
    reasons: list[str] = field(default_factory=list)
    freshness: dict[str, Any] = field(default_factory=dict)
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FastSignalShadowReport:
    symbols: list[str]
    timeframe: str
    hours: int
    signals: list[FastSignal] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    loader_statuses: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION
    no_lookahead_status: str = NO_LOOKAHEAD_STATUS

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "timeframe": self.timeframe,
            "hours": self.hours,
            "signals": [signal.as_dict() for signal in self.signals],
            "summary": dict(self.summary),
            "loader_statuses": self.loader_statuses,
            "warnings": self.warnings,
            "research_only": self.research_only,
            "paper_filter_enabled": self.paper_filter_enabled,
            "can_send_real_orders": self.can_send_real_orders,
            "final_recommendation": self.final_recommendation,
            "no_lookahead_status": self.no_lookahead_status,
        }


def _ladder_prices(side: str, entry: float, tp_distance: float, fractions: tuple[float, float, float] = (0.5, 1.0, 1.5)) -> tuple[float, float, float]:
    direction = side_direction(side)
    tp1 = entry * (1.0 + (tp_distance * fractions[0]) / 100.0 * direction)
    tp2 = entry * (1.0 + (tp_distance * fractions[1]) / 100.0 * direction)
    tp3 = entry * (1.0 + (tp_distance * fractions[2]) / 100.0 * direction)
    return tp1, tp2, tp3


def _classify_actionability(
    *,
    side: str,
    current_price: float,
    entry_price: float,
    tp1_price: float,
    stop_price: float,
) -> tuple[str, list[str]]:
    if entry_price <= 0 or current_price <= 0:
        return ACTIONABILITY_OUT_OF_ZONE, ["invalid_prices"]
    tp_distance = abs(tp1_price - entry_price)
    if tp_distance <= 0:
        return ACTIONABILITY_OUT_OF_ZONE, ["zero_tp_distance"]
    direction = side_direction(side)
    progress = ((current_price - entry_price) / tp_distance) * direction
    # progress < 0 => price not at entry yet
    # progress = 0 => exactly at entry
    # progress > 0 => price already moved in trade direction beyond entry
    if progress <= -ZONE_OUT:
        return ACTIONABILITY_OUT_OF_ZONE, [f"progress={progress:.3f}_below_out_threshold"]
    if progress >= ZONE_OUT:
        return ACTIONABILITY_OUT_OF_ZONE, [f"progress={progress:.3f}_above_out_threshold"]
    if progress >= ZONE_LATE:
        return ACTIONABILITY_LATE, [f"progress={progress:.3f}_above_late_threshold"]
    if abs(progress) <= ZONE_ENTER_ABS:
        # Inside the entry zone — but only ENTER_NOW if current price has not
        # crossed the stop on the wrong side.
        if (side == "LONG" and current_price <= stop_price) or (
            side == "SHORT" and current_price >= stop_price
        ):
            return ACTIONABILITY_OUT_OF_ZONE, ["current_price_past_stop"]
        return ACTIONABILITY_ENTER_NOW, [f"progress={progress:.3f}_within_enter_band"]
    if progress < 0 and abs(progress) <= ZONE_WAIT_NEAR:
        return ACTIONABILITY_WAIT, [f"progress={progress:.3f}_below_entry_within_wait_band"]
    return ACTIONABILITY_WAIT, [f"progress={progress:.3f}_within_wait_band"]


def _current_price(candles: pd.DataFrame) -> float:
    if candles is None or candles.empty:
        return 0.0
    try:
        return safe_float(candles.iloc[-1].get("close"))
    except Exception:
        return 0.0


def _newest_timestamp(candles: pd.DataFrame) -> str:
    if candles is None or candles.empty:
        return ""
    try:
        raw = candles.iloc[-1].get("timestamp")
        if isinstance(raw, datetime):
            return (raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)).isoformat()
        return str(raw)
    except Exception:
        return ""


def _shadow_signal_from_context(
    ctx: ReplayTradeContext,
    freshness: FreshnessVerdict,
    *,
    simulated_leverage: int,
) -> FastSignal:
    trade = ctx.trade
    side = str(trade.side).upper()
    entry = safe_float(trade.entry_price)
    stop = safe_float(trade.stop_loss)
    tp1_raw = safe_float(trade.take_profit_1)
    tp_distance = abs(tp1_raw - entry) / entry * 100.0 if entry > 0 and tp1_raw > 0 else 0.6
    tp1, tp2, tp3 = _ladder_prices(side, entry, tp_distance)
    atr_pct = average_range_pct(ctx.candles, int(trade.entry_index), 14)
    current = _current_price(ctx.candles)
    distance_to_entry_pct = (current - entry) / entry * 100.0 if entry > 0 else 0.0
    distance_to_tp_pct = (tp1 - current) / current * 100.0 if current > 0 else 0.0
    distance_to_stop_pct = (current - stop) / current * 100.0 if current > 0 and stop > 0 else 0.0
    atr_distance_x = abs(distance_to_entry_pct) / atr_pct if atr_pct > 0 else 0.0
    if freshness.status == "STALE":
        actionability = ACTIONABILITY_NO_ACTIONABLE_DATA_STALE
        reasons = list(freshness.reasons) + ["freshness_gate_blocked"]
    elif freshness.status == "NEED_DATA":
        actionability = ACTIONABILITY_NEED_DATA
        reasons = list(freshness.reasons)
    elif freshness.status == "LOADER_ERROR":
        actionability = ACTIONABILITY_LOADER_ERROR
        reasons = list(freshness.reasons)
    elif freshness.status == "HISTORICAL_RESEARCH_ONLY":
        actionability = ACTIONABILITY_HISTORICAL
        reasons = list(freshness.reasons)
    else:
        actionability, reasons = _classify_actionability(
            side=side,
            current_price=current,
            entry_price=entry,
            tp1_price=tp1,
            stop_price=stop,
        )
    return FastSignal(
        symbol=str(ctx.symbol).upper(),
        timeframe=str(ctx.timeframe),
        side=side,
        score=0,
        actionability=actionability,
        entry_price=entry,
        stop_price=stop,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        current_price=current,
        distance_to_entry_pct=distance_to_entry_pct,
        distance_to_tp_pct=distance_to_tp_pct,
        distance_to_stop_pct=distance_to_stop_pct,
        distance_to_entry_atr_x=atr_distance_x,
        atr_pct=atr_pct,
        timestamp=_newest_timestamp(ctx.candles),
        simulated_leverage=int(simulated_leverage),
        reasons=reasons,
        freshness=freshness.as_dict(),
    )


def run_fast_signal_shadow(
    config: Any,
    db: Any,
    *,
    hours: int = 72,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
    historical: bool = False,
    simulated_leverage: int | None = None,
) -> FastSignalShadowReport:
    symbol_list = parse_symbols(symbols, config)
    bundle = load_replay_trade_contexts(
        config, db, hours=hours, timeframe=timeframe, symbols=symbol_list,
    )
    simulated_leverage = int(simulated_leverage or getattr(config, "default_leverage", 1) or 1)
    # Keep the most recent context per symbol as the "current" shadow signal.
    most_recent: dict[str, ReplayTradeContext] = {}
    for ctx in bundle.contexts:
        most_recent[str(ctx.symbol).upper()] = ctx
    signals: list[FastSignal] = []
    for symbol in symbol_list:
        freshness = evaluate_freshness(
            db, symbol=symbol, timeframe=timeframe, historical=historical,
        )
        ctx = most_recent.get(symbol)
        if ctx is None:
            # Build a degenerate signal so the dashboard knows the symbol was
            # asked for but has no replay context yet.
            if freshness.status == "STALE":
                actionability = ACTIONABILITY_NO_ACTIONABLE_DATA_STALE
            elif freshness.status == "NEED_DATA":
                actionability = ACTIONABILITY_NEED_DATA
            elif freshness.status == "HISTORICAL_RESEARCH_ONLY":
                actionability = ACTIONABILITY_HISTORICAL
            else:
                actionability = ACTIONABILITY_LOADER_ERROR
            signals.append(FastSignal(
                symbol=symbol,
                timeframe=timeframe,
                side="UNKNOWN",
                score=0,
                actionability=actionability,
                entry_price=0.0,
                stop_price=0.0,
                tp1=0.0, tp2=0.0, tp3=0.0,
                current_price=0.0,
                distance_to_entry_pct=0.0,
                distance_to_tp_pct=0.0,
                distance_to_stop_pct=0.0,
                distance_to_entry_atr_x=0.0,
                atr_pct=0.0,
                timestamp="",
                simulated_leverage=simulated_leverage,
                reasons=["no_replay_context"] + list(freshness.reasons),
                freshness=freshness.as_dict(),
            ))
            continue
        signals.append(_shadow_signal_from_context(
            ctx, freshness, simulated_leverage=simulated_leverage,
        ))
    summary: dict[str, int] = {}
    for signal in signals:
        summary[signal.actionability] = summary.get(signal.actionability, 0) + 1
    return FastSignalShadowReport(
        symbols=symbol_list,
        timeframe=timeframe,
        hours=int(hours),
        signals=signals,
        summary=summary,
        loader_statuses=bundle.loader_statuses,
        warnings=list(bundle.warnings),
    )


def render_fast_signal_shadow_text(report: FastSignalShadowReport) -> str:
    lines = [
        "FAST SIGNAL SHADOW START",
        f"symbols: {','.join(report.symbols)}",
        f"timeframe: {report.timeframe}",
        f"hours: {report.hours}",
        f"summary: {report.summary}",
        "symbol | side | actionability | entry | current | tp1 | stop | dist_entry% | dist_tp% | dist_stop% | atr% | atr_x | leverage_sim | freshness",
    ]
    for signal in report.signals:
        lines.append(
            f"{signal.symbol} | {signal.side} | {signal.actionability} | "
            f"{signal.entry_price:.6f} | {signal.current_price:.6f} | {signal.tp1:.6f} | {signal.stop_price:.6f} | "
            f"{signal.distance_to_entry_pct:.4f} | {signal.distance_to_tp_pct:.4f} | "
            f"{signal.distance_to_stop_pct:.4f} | {signal.atr_pct:.4f} | "
            f"{signal.distance_to_entry_atr_x:.2f} | {signal.simulated_leverage}x | "
            f"{signal.freshness.get('status', 'UNKNOWN')}"
        )
    if report.warnings:
        lines.append("warnings:")
        for warning in report.warnings[:6]:
            lines.append(f"- {warning}")
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "activation: disabled",
        "final_recommendation: NO LIVE",
        "FAST SIGNAL SHADOW END",
    ])
    return "\n".join(lines)


def fast_signal_shadow_text(
    config: Any,
    db: Any,
    *,
    hours: int = 72,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
) -> str:
    return render_fast_signal_shadow_text(run_fast_signal_shadow(
        config, db, hours=hours, timeframe=timeframe, symbols=symbols,
    ))
