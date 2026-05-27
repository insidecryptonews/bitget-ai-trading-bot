"""ResearchOps V5 — Shadow Multi-Trade Learning Engine.

Goal: register and replay many *virtual* trades in parallel so the bot can
accumulate setup statistics faster, without ever touching:

  - PaperTrader.open_position
  - ExecutionEngine
  - paper slots
  - real orders
  - leverage / margin / sizing config

The engine produces an in-memory list of `ShadowVirtualTrade` records and a
deterministic replay over the existing OHLCV history. The dashboard surfaces
this strictly as a research panel; it is never confused with paper positions.

Hard contract:
  - research_only = True
  - activation = "shadow_only"
  - paper_filter_enabled = False
  - can_send_real_orders = False
  - no DB writes from this module (callers may persist via a future shadow
    table behind an explicit, disabled-by-default flag; not implemented here)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .data_freshness_gate import (
    NON_ACTIONABLE_STATUSES,
    aggregate_actionable,
    evaluate_freshness_many,
)
from .phase8_research_utils import (
    FINAL_RECOMMENDATION,
    NO_LOOKAHEAD_STATUS,
    ReplayTradeContext,
    load_replay_trade_contexts,
    parse_symbols,
    side_direction,
)
from .utils import safe_float


SHADOW_OPEN = "OPEN"
SHADOW_CLOSED_TP1 = "CLOSED_TP1"
SHADOW_CLOSED_TP2 = "CLOSED_TP2"
SHADOW_CLOSED_TP3 = "CLOSED_TP3"
SHADOW_CLOSED_STOP = "CLOSED_STOP"
SHADOW_CLOSED_TIME = "CLOSED_TIME"
SHADOW_CLOSED_NET_PROFIT_LOCK = "CLOSED_NET_PROFIT_LOCK"
SHADOW_BLOCKED_DEDUPE = "BLOCKED_DEDUPE"
SHADOW_BLOCKED_RATE_LIMIT = "BLOCKED_RATE_LIMIT"
SHADOW_BLOCKED_DATA_STALE = "BLOCKED_DATA_STALE"


@dataclass
class ShadowTradePolicy:
    capital_total_usdt: float = 40.0
    margin_per_trade_usdt: float = 5.0
    simulated_leverage: int = 5
    base_cost_pct: float = 0.18
    net_profit_lock_pct: float = 0.60
    break_even_after_fees_buffer_pct: float = 0.05
    tp_fractions: tuple[float, float, float] = (0.5, 1.0, 1.5)
    max_holding_bars: int = 40
    max_shadow_trades_per_symbol_per_hour: int = 4
    max_total_shadow_open: int = 30
    cooldown_minutes_per_setup: int = 10
    research_only: bool = True
    activation: str = "shadow_only"
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False


@dataclass
class ShadowVirtualTrade:
    shadow_id: str
    symbol: str
    timeframe: str
    side: str
    setup_id: str
    entry_index: int
    exit_index: int
    entry_price: float
    stop_price: float
    tp1: float
    tp2: float
    tp3: float
    score: int
    regime: str
    cost_model: str
    capital_scenario_id: str
    created_at: str
    closed_at: str
    data_freshness_status: str
    actionability: str
    status: str
    reason: str
    no_execution: bool
    mfe_pct: float
    mae_pct: float
    bars_open: int
    tp1_hit: bool
    tp2_hit: bool
    tp3_hit: bool
    stop_hit: bool
    time_hit: bool
    net_profit_lock_hit: bool
    break_even_after_fees_hit: bool
    gross_pnl_pct: float
    net_pnl_pct: float
    gross_pnl_usdt: float
    net_pnl_usdt: float
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ShadowMultiTradeReport:
    symbols: list[str]
    timeframe: str
    hours: int
    policy: ShadowTradePolicy
    trades: list[ShadowVirtualTrade] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    pnl_summary: dict[str, float] = field(default_factory=dict)
    loader_statuses: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    no_db_writes: bool = True
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    activation: str = "shadow_only"
    no_lookahead_status: str = NO_LOOKAHEAD_STATUS
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "timeframe": self.timeframe,
            "hours": self.hours,
            "policy": asdict(self.policy),
            "trades": [trade.as_dict() for trade in self.trades],
            "summary": dict(self.summary),
            "pnl_summary": dict(self.pnl_summary),
            "loader_statuses": self.loader_statuses,
            "warnings": self.warnings,
            "no_db_writes": self.no_db_writes,
            "research_only": self.research_only,
            "paper_filter_enabled": self.paper_filter_enabled,
            "can_send_real_orders": self.can_send_real_orders,
            "activation": self.activation,
            "no_lookahead_status": self.no_lookahead_status,
            "final_recommendation": self.final_recommendation,
        }


def _price_at_pct(side: str, entry: float, pct: float) -> float:
    direction = side_direction(side)
    return entry * (1.0 + (pct / 100.0) * direction)


def _entry_timestamp(ctx: ReplayTradeContext) -> datetime:
    try:
        raw = ctx.candles.iloc[int(ctx.trade.entry_index)].get("timestamp")
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _shadow_id(ctx: ReplayTradeContext, setup_id: str, capital_scenario_id: str) -> str:
    timestamp = _entry_timestamp(ctx).strftime("%Y%m%dT%H%M%S")
    return (
        f"{ctx.symbol.upper()}|{str(ctx.trade.side).upper()}|{setup_id}|"
        f"{ctx.timeframe}|{int(ctx.trade.entry_index)}|{capital_scenario_id}|{timestamp}"
    )


def _simulate_one_shadow_trade(
    ctx: ReplayTradeContext,
    policy: ShadowTradePolicy,
    *,
    setup_id: str,
    capital_scenario_id: str,
    cost_pct: float,
) -> ShadowVirtualTrade:
    trade = ctx.trade
    side = str(trade.side).upper()
    entry = safe_float(trade.entry_price)
    if entry <= 0:
        return _empty_blocked(ctx, setup_id, capital_scenario_id, cost_pct, "invalid_entry_price")
    tp1_raw = safe_float(trade.take_profit_1)
    tp_distance = abs(tp1_raw - entry) / entry * 100.0 if tp1_raw > 0 else 0.6
    tp1_pct = tp_distance * policy.tp_fractions[0]
    tp2_pct = tp_distance * policy.tp_fractions[1]
    tp3_pct = tp_distance * policy.tp_fractions[2]
    tp1_price = _price_at_pct(side, entry, tp1_pct)
    tp2_price = _price_at_pct(side, entry, tp2_pct)
    tp3_price = _price_at_pct(side, entry, tp3_pct)
    stop_price = safe_float(trade.stop_loss)
    if stop_price <= 0:
        stop_price = _price_at_pct(side, entry, -max(tp_distance, 0.5))
    horizon = min(len(ctx.candles), int(trade.entry_index) + policy.max_holding_bars)
    mfe_pct = 0.0
    mae_pct = 0.0
    tp1_hit = False
    tp2_hit = False
    tp3_hit = False
    stop_hit = False
    time_hit = False
    net_lock_hit = False
    be_hit = False
    exit_price = entry
    exit_index = int(trade.entry_index)
    exit_reason = "TIME"
    for index in range(int(trade.entry_index), horizon):
        row = ctx.candles.iloc[index]
        high = safe_float(row.get("high"))
        low = safe_float(row.get("low"))
        if high <= 0 or low <= 0:
            continue
        if side == "LONG":
            mfe_pct = max(mfe_pct, (high - entry) / entry * 100.0)
            mae_pct = min(mae_pct, (low - entry) / entry * 100.0)
            local_stop = low <= stop_price
            local_tp1 = high >= tp1_price
            local_tp2 = high >= tp2_price
            local_tp3 = high >= tp3_price
            local_net = high >= _price_at_pct(side, entry, policy.net_profit_lock_pct + cost_pct)
            local_be = high >= _price_at_pct(side, entry, policy.break_even_after_fees_buffer_pct + cost_pct)
        else:
            mfe_pct = max(mfe_pct, (entry - low) / entry * 100.0)
            mae_pct = min(mae_pct, (entry - high) / entry * 100.0)
            local_stop = high >= stop_price
            local_tp1 = low <= tp1_price
            local_tp2 = low <= tp2_price
            local_tp3 = low <= tp3_price
            local_net = low <= _price_at_pct(side, entry, -(policy.net_profit_lock_pct + cost_pct))
            local_be = low <= _price_at_pct(side, entry, -(policy.break_even_after_fees_buffer_pct + cost_pct))
        # STOP_BEFORE_TP same-bar rule (Phase 8B contract).
        if local_stop:
            stop_hit = True
            exit_price = stop_price
            exit_reason = "STOP"
            exit_index = index
            break
        if local_tp3:
            tp1_hit = tp1_hit or local_tp1
            tp2_hit = tp2_hit or local_tp2
            tp3_hit = True
            exit_price = tp3_price
            exit_reason = "TP3"
            exit_index = index
            break
        if local_net and not net_lock_hit:
            net_lock_hit = True
            exit_price = _price_at_pct(side, entry, policy.net_profit_lock_pct + cost_pct)
            exit_reason = "NET_PROFIT_LOCK"
            exit_index = index
            break
        if local_tp2:
            tp1_hit = tp1_hit or local_tp1
            tp2_hit = True
            exit_price = tp2_price
            exit_reason = "TP2"
            exit_index = index
            break
        if local_tp1 and not tp1_hit:
            tp1_hit = True
            stop_price = _price_at_pct(
                side, entry,
                (policy.break_even_after_fees_buffer_pct + cost_pct) * side_direction(side),
            )
            be_hit = True
        if local_be and not be_hit:
            be_hit = True
    if exit_reason == "TIME":
        time_hit = True
        try:
            exit_row = ctx.candles.iloc[min(horizon - 1, len(ctx.candles) - 1)]
            exit_price = safe_float(exit_row.get("close")) or entry
        except Exception:
            exit_price = entry
        exit_index = horizon - 1
    gross = ((exit_price - entry) / entry) * 100.0 * side_direction(side)
    net = gross - cost_pct
    notional = policy.margin_per_trade_usdt * max(1, policy.simulated_leverage)
    gross_pnl_usdt = (gross / 100.0) * notional
    net_pnl_usdt = (net / 100.0) * notional
    status = {
        "TP3": SHADOW_CLOSED_TP3, "TP2": SHADOW_CLOSED_TP2, "TP1": SHADOW_CLOSED_TP1,
        "STOP": SHADOW_CLOSED_STOP, "NET_PROFIT_LOCK": SHADOW_CLOSED_NET_PROFIT_LOCK,
        "TIME": SHADOW_CLOSED_TIME,
    }.get(exit_reason, SHADOW_CLOSED_TIME)
    closed_at = ""
    try:
        closed_row = ctx.candles.iloc[exit_index]
        raw = closed_row.get("timestamp")
        if isinstance(raw, datetime):
            closed_at = (raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)).isoformat()
        else:
            closed_at = str(raw)
    except Exception:
        closed_at = ""
    return ShadowVirtualTrade(
        shadow_id=_shadow_id(ctx, setup_id, capital_scenario_id),
        symbol=str(ctx.symbol).upper(),
        timeframe=str(ctx.timeframe),
        side=side,
        setup_id=setup_id,
        entry_index=int(trade.entry_index),
        exit_index=int(exit_index),
        entry_price=entry,
        stop_price=stop_price,
        tp1=tp1_price,
        tp2=tp2_price,
        tp3=tp3_price,
        score=0,
        regime=str(ctx.regime or "UNKNOWN"),
        cost_model=f"base_cost_pct={cost_pct:.4f}",
        capital_scenario_id=capital_scenario_id,
        created_at=_entry_timestamp(ctx).isoformat(),
        closed_at=closed_at,
        data_freshness_status="OK",
        actionability="SHADOW_RECORDED",
        status=status,
        reason=exit_reason,
        no_execution=True,
        mfe_pct=mfe_pct,
        mae_pct=mae_pct,
        bars_open=int(exit_index) - int(trade.entry_index) + 1,
        tp1_hit=tp1_hit,
        tp2_hit=tp2_hit,
        tp3_hit=tp3_hit,
        stop_hit=stop_hit,
        time_hit=time_hit,
        net_profit_lock_hit=net_lock_hit,
        break_even_after_fees_hit=be_hit,
        gross_pnl_pct=gross,
        net_pnl_pct=net,
        gross_pnl_usdt=gross_pnl_usdt,
        net_pnl_usdt=net_pnl_usdt,
    )


def _empty_blocked(
    ctx: ReplayTradeContext,
    setup_id: str,
    capital_scenario_id: str,
    cost_pct: float,
    reason: str,
) -> ShadowVirtualTrade:
    return ShadowVirtualTrade(
        shadow_id=_shadow_id(ctx, setup_id, capital_scenario_id),
        symbol=str(ctx.symbol).upper(),
        timeframe=str(ctx.timeframe),
        side=str(ctx.trade.side).upper(),
        setup_id=setup_id,
        entry_index=int(ctx.trade.entry_index),
        exit_index=int(ctx.trade.entry_index),
        entry_price=0.0,
        stop_price=0.0,
        tp1=0.0, tp2=0.0, tp3=0.0,
        score=0,
        regime=str(ctx.regime or "UNKNOWN"),
        cost_model=f"base_cost_pct={cost_pct:.4f}",
        capital_scenario_id=capital_scenario_id,
        created_at=_entry_timestamp(ctx).isoformat(),
        closed_at="",
        data_freshness_status="UNKNOWN",
        actionability="BLOCKED",
        status=SHADOW_BLOCKED_DEDUPE,
        reason=reason,
        no_execution=True,
        mfe_pct=0.0, mae_pct=0.0, bars_open=0,
        tp1_hit=False, tp2_hit=False, tp3_hit=False,
        stop_hit=False, time_hit=False, net_profit_lock_hit=False,
        break_even_after_fees_hit=False,
        gross_pnl_pct=0.0, net_pnl_pct=0.0,
        gross_pnl_usdt=0.0, net_pnl_usdt=0.0,
    )


def _dedupe_key(ctx: ReplayTradeContext, setup_id: str) -> str:
    timestamp = _entry_timestamp(ctx).strftime("%Y%m%dT%H%M")
    return f"{ctx.symbol.upper()}|{str(ctx.trade.side).upper()}|{setup_id}|{ctx.timeframe}|{timestamp}"


def _rate_limit_key(symbol: str, ts: datetime) -> str:
    return f"{symbol}|{ts.strftime('%Y%m%dT%H')}"


def run_shadow_multi_trade(
    config: Any,
    db: Any,
    *,
    hours: int = 720,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
    policy: ShadowTradePolicy | None = None,
    setup_id: str = "researchops_v5_default",
    capital_scenario_id: str = "capital_40_margin_5_leverage_5",
    historical: bool = True,
) -> ShadowMultiTradeReport:
    """Generate shadow virtual trades over the OHLCV history.

    `historical=True` (default) lets the replay run on the existing OHLCV
    history regardless of freshness — the dashboard treats this as a research
    panel. Set False to also evaluate live freshness for the symbols.
    """
    active_policy = policy or ShadowTradePolicy()
    symbol_list = parse_symbols(symbols, config)
    if not symbol_list:
        symbol_list = ["BTCUSDT", "ETHUSDT", "DOTUSDT"]
    bundle = load_replay_trade_contexts(
        config, db, hours=hours, timeframe=timeframe, symbols=symbol_list,
    )
    freshness_verdicts = evaluate_freshness_many(
        db, symbols=symbol_list, timeframe=timeframe, historical=historical,
    )
    dedupe_seen: set[str] = set()
    per_hour_count: dict[str, int] = {}
    trades: list[ShadowVirtualTrade] = []
    open_count = 0
    for ctx in bundle.contexts:
        if open_count >= active_policy.max_total_shadow_open and ctx.trade.exit_index <= ctx.trade.entry_index:
            trades.append(_empty_blocked(ctx, setup_id, capital_scenario_id, active_policy.base_cost_pct, "max_total_shadow_open"))
            continue
        verdict = freshness_verdicts.get(str(ctx.symbol).upper())
        if verdict is not None and verdict.status in NON_ACTIONABLE_STATUSES and not historical:
            blocked = _empty_blocked(ctx, setup_id, capital_scenario_id, active_policy.base_cost_pct, f"freshness={verdict.status}")
            blocked.status = SHADOW_BLOCKED_DATA_STALE
            blocked.data_freshness_status = verdict.status
            trades.append(blocked)
            continue
        dedup_key = _dedupe_key(ctx, setup_id)
        if dedup_key in dedupe_seen:
            blocked = _empty_blocked(ctx, setup_id, capital_scenario_id, active_policy.base_cost_pct, "dedupe_collision")
            blocked.status = SHADOW_BLOCKED_DEDUPE
            trades.append(blocked)
            continue
        rl_key = _rate_limit_key(str(ctx.symbol).upper(), _entry_timestamp(ctx))
        if per_hour_count.get(rl_key, 0) >= active_policy.max_shadow_trades_per_symbol_per_hour:
            blocked = _empty_blocked(ctx, setup_id, capital_scenario_id, active_policy.base_cost_pct, "rate_limit_per_symbol_per_hour")
            blocked.status = SHADOW_BLOCKED_RATE_LIMIT
            trades.append(blocked)
            continue
        dedupe_seen.add(dedup_key)
        per_hour_count[rl_key] = per_hour_count.get(rl_key, 0) + 1
        sim = _simulate_one_shadow_trade(
            ctx, active_policy,
            setup_id=setup_id,
            capital_scenario_id=capital_scenario_id,
            cost_pct=active_policy.base_cost_pct,
        )
        sim.data_freshness_status = verdict.status if verdict is not None else "UNKNOWN"
        trades.append(sim)
        open_count += 1 if sim.status == SHADOW_OPEN else 0
    summary: dict[str, int] = {}
    for trade in trades:
        summary[trade.status] = summary.get(trade.status, 0) + 1
    closed = [t for t in trades if t.status.startswith("CLOSED_")]
    pnl_summary = {
        "closed_count": float(len(closed)),
        "gross_pnl_pct_sum": sum(t.gross_pnl_pct for t in closed),
        "net_pnl_pct_sum": sum(t.net_pnl_pct for t in closed),
        "gross_pnl_usdt_sum": sum(t.gross_pnl_usdt for t in closed),
        "net_pnl_usdt_sum": sum(t.net_pnl_usdt for t in closed),
        "wins_net": float(sum(1 for t in closed if t.net_pnl_pct > 0)),
        "losses_net": float(sum(1 for t in closed if t.net_pnl_pct < 0)),
    }
    return ShadowMultiTradeReport(
        symbols=symbol_list,
        timeframe=timeframe,
        hours=int(hours),
        policy=active_policy,
        trades=trades,
        summary=summary,
        pnl_summary=pnl_summary,
        loader_statuses=bundle.loader_statuses,
        warnings=list(bundle.warnings),
    )


def render_shadow_multi_trade_text(report: ShadowMultiTradeReport) -> str:
    lines = [
        "SHADOW MULTI-TRADE LEARNING START",
        f"symbols: {','.join(report.symbols)}",
        f"timeframe: {report.timeframe}",
        f"hours: {report.hours}",
        f"summary: {report.summary}",
        f"pnl_summary: {report.pnl_summary}",
        "shadow_id | symbol | side | setup | status | reason | gross_pct | net_pct | gross_usdt | net_usdt | mfe | mae | bars | freshness",
    ]
    for trade in report.trades[:120]:
        lines.append(
            f"{trade.shadow_id} | {trade.symbol} | {trade.side} | {trade.setup_id} | "
            f"{trade.status} | {trade.reason} | "
            f"{trade.gross_pnl_pct:.4f} | {trade.net_pnl_pct:.4f} | "
            f"{trade.gross_pnl_usdt:.4f} | {trade.net_pnl_usdt:.4f} | "
            f"{trade.mfe_pct:.4f} | {trade.mae_pct:.4f} | {trade.bars_open} | "
            f"{trade.data_freshness_status}"
        )
    lines.extend([
        "no_execution: true",
        "no_db_writes: true",
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "activation: shadow_only",
        "final_recommendation: NO LIVE",
        "SHADOW MULTI-TRADE LEARNING END",
    ])
    return "\n".join(lines)
