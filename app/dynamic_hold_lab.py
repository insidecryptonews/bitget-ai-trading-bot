from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from .cost_model import explain_cost_breakdown
from .phase8_research_utils import (
    FINAL_RECOMMENDATION,
    NO_LOOKAHEAD_STATUS,
    STOP_TP_SAME_BAR_RULE,
    ReplayTradeContext,
    average_range_pct,
    gross_return_pct,
    load_replay_trade_contexts,
    max_drawdown,
    net_pf,
    prior_side_move_pct,
    same_bar_stop_before_tp,
)
from .utils import safe_float


REJECT_WORSE_THAN_BASELINE = "REJECT_WORSE_THAN_BASELINE"
REJECT_TOO_FEW_TRADES = "REJECT_TOO_FEW_TRADES"
REJECT_TOO_SENSITIVE = "REJECT_TOO_SENSITIVE"
REJECT_OVERFIT = "REJECT_OVERFIT"
WATCH_ONLY = "WATCH_ONLY"
IMPROVES_BASELINE_RESEARCH_ONLY = "IMPROVES_BASELINE_RESEARCH_ONLY"


@dataclass(frozen=True)
class DynamicHoldPolicy:
    name: str
    extend_bars: int = 0
    direction_required: bool = False
    profit_lock_pct: float | None = None
    trailing_pct: float | None = None
    time_decay_bars: int | None = None
    side_only: str | None = None
    block_late_entry: bool = False
    block_reversal_risk: bool = False


@dataclass
class DynamicHoldTrade:
    symbol: str
    side: str
    policy: str
    net_return_pct: float
    gross_return_pct: float
    exit_reason: str
    duration_bars: int
    mfe_pct: float
    mae_pct: float
    blocked: bool = False


@dataclass
class DynamicHoldPolicyResult:
    policy_name: str
    trades: int
    net_ev: float
    net_pf: float
    tp_pct: float
    sl_pct: float
    time_pct: float
    avg_bars: float
    max_bars: int
    avg_mfe: float
    avg_mae: float
    max_drawdown: float
    missed_profit_recovered_pct: float
    loss_worsening_pct: float
    exposure_increase_pct: float
    stability_by_symbol: dict[str, float]
    decision: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DynamicHoldReport:
    hours: int
    timeframe: str
    symbols: list[str]
    baseline_net_ev: float
    baseline_trades: int
    policies: list[DynamicHoldPolicyResult] = field(default_factory=list)
    loader_statuses: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    final_recommendation: str = FINAL_RECOMMENDATION
    research_only: bool = True
    no_lookahead_status: str = NO_LOOKAHEAD_STATUS
    stop_tp_same_bar_rule: str = STOP_TP_SAME_BAR_RULE

    def as_dict(self) -> dict[str, Any]:
        return {
            "hours": self.hours,
            "timeframe": self.timeframe,
            "symbols": self.symbols,
            "baseline_net_ev": self.baseline_net_ev,
            "baseline_trades": self.baseline_trades,
            "policies": [policy.as_dict() for policy in self.policies],
            "loader_statuses": self.loader_statuses,
            "warnings": self.warnings,
            "final_recommendation": self.final_recommendation,
            "research_only": self.research_only,
            "no_lookahead_status": self.no_lookahead_status,
            "stop_tp_same_bar_rule": self.stop_tp_same_bar_rule,
        }


def default_dynamic_hold_policies() -> tuple[DynamicHoldPolicy, ...]:
    return (
        DynamicHoldPolicy("baseline_current_exit"),
        DynamicHoldPolicy("fixed_extend_10_bars", extend_bars=10),
        DynamicHoldPolicy("fixed_extend_20_bars", extend_bars=20),
        DynamicHoldPolicy("hold_while_direction_valid", extend_bars=20, direction_required=True),
        DynamicHoldPolicy("hold_until_direction_invalidated", extend_bars=30, direction_required=True),
        DynamicHoldPolicy("hold_with_profit_lock", extend_bars=20, profit_lock_pct=0.60),
        DynamicHoldPolicy("hold_with_trailing_profit_lock", extend_bars=20, profit_lock_pct=0.50, trailing_pct=0.35),
        DynamicHoldPolicy("hold_with_time_decay_and_mfe", extend_bars=20, time_decay_bars=12, profit_lock_pct=0.40),
        DynamicHoldPolicy("hold_short_only_directional", extend_bars=20, direction_required=True, side_only="SHORT"),
        DynamicHoldPolicy("hold_long_only_directional", extend_bars=20, direction_required=True, side_only="LONG"),
        DynamicHoldPolicy("late_entry_block_plus_dynamic_hold", extend_bars=20, direction_required=True, block_late_entry=True),
        DynamicHoldPolicy("reversal_risk_block_plus_dynamic_hold", extend_bars=20, direction_required=True, block_reversal_risk=True),
    )


def run_dynamic_hold_lab(
    config: Any,
    db: Any,
    *,
    hours: int = 72,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
) -> DynamicHoldReport:
    bundle = load_replay_trade_contexts(config, db, hours=hours, timeframe=timeframe, symbols=symbols)
    baseline_trades = [
        DynamicHoldTrade(
            symbol=ctx.symbol,
            side=str(ctx.trade.side).upper(),
            policy="baseline_current_exit",
            net_return_pct=safe_float(ctx.trade.net_return_pct),
            gross_return_pct=safe_float(ctx.trade.gross_return_pct),
            exit_reason=str(ctx.trade.exit_reason),
            duration_bars=int(ctx.trade.exit_index) - int(ctx.trade.entry_index) + 1,
            mfe_pct=_window_mfe_mae(ctx)[0],
            mae_pct=_window_mfe_mae(ctx)[1],
        )
        for ctx in bundle.contexts
    ]
    baseline_summary = _summarise_policy("baseline_current_exit", baseline_trades, baseline_trades)
    results: list[DynamicHoldPolicyResult] = [baseline_summary]
    for policy in default_dynamic_hold_policies()[1:]:
        simulated = [_simulate_dynamic_trade(ctx, policy) for ctx in bundle.contexts]
        simulated = [trade for trade in simulated if not trade.blocked]
        results.append(_summarise_policy(policy.name, simulated, baseline_trades))
    return DynamicHoldReport(
        hours=bundle.hours,
        timeframe=bundle.timeframe,
        symbols=bundle.symbols,
        baseline_net_ev=baseline_summary.net_ev,
        baseline_trades=baseline_summary.trades,
        policies=results,
        loader_statuses=bundle.loader_statuses,
        warnings=bundle.warnings,
    )


def _simulate_dynamic_trade(ctx: ReplayTradeContext, policy: DynamicHoldPolicy) -> DynamicHoldTrade:
    trade = ctx.trade
    side = str(trade.side).upper()
    if policy.side_only and side != policy.side_only:
        return _baseline_trade(ctx, policy.name)
    late_risk = _late_entry_risk(ctx)
    if policy.block_late_entry and late_risk:
        return _blocked_trade(ctx, policy.name)
    if policy.block_reversal_risk and _reversal_risk(ctx):
        return _blocked_trade(ctx, policy.name)

    base_horizon = int(trade.exit_index) - int(trade.entry_index) + 1
    max_horizon = max(1, base_horizon + max(0, int(policy.extend_bars)))
    last = min(len(ctx.candles), int(trade.entry_index) + max_horizon)
    exit_price = safe_float(trade.exit_price)
    exit_reason = str(trade.exit_reason)
    exit_index = int(trade.exit_index)
    current_stop = safe_float(trade.stop_loss)
    best_price = safe_float(trade.entry_price)
    mfe = 0.0
    mae = 0.0
    direction_invalid = False
    for index in range(int(trade.entry_index), last):
        row = ctx.candles.iloc[index]
        high = safe_float(row.get("high"))
        low = safe_float(row.get("low"))
        close = safe_float(row.get("close"))
        stop_hit, tp_hit, same_bar = same_bar_stop_before_tp(side, high, low, current_stop, safe_float(trade.take_profit_1))
        if side == "LONG":
            best_price = max(best_price, high)
            mfe = max(mfe, (high - safe_float(trade.entry_price)) / safe_float(trade.entry_price) * 100.0)
            mae = min(mae, (low - safe_float(trade.entry_price)) / safe_float(trade.entry_price) * 100.0)
        else:
            best_price = min(best_price, low) if best_price else low
            mfe = max(mfe, (safe_float(trade.entry_price) - low) / safe_float(trade.entry_price) * 100.0)
            mae = min(mae, (safe_float(trade.entry_price) - high) / safe_float(trade.entry_price) * 100.0)
        if stop_hit and (same_bar or not tp_hit):
            exit_price = current_stop
            exit_reason = "STOP_LOSS"
            exit_index = index
            break
        if tp_hit:
            exit_price = safe_float(trade.take_profit_1)
            exit_reason = "TAKE_PROFIT"
            exit_index = index
            break
        if policy.direction_required and index > int(trade.exit_index) and not _direction_valid(ctx.candles, index, side):
            direction_invalid = True
            exit_price = close
            exit_reason = "DIRECTION_INVALIDATED"
            exit_index = index
            break
        if policy.profit_lock_pct and mfe >= policy.profit_lock_pct:
            lock_price = _price_at_pct(side, safe_float(trade.entry_price), policy.profit_lock_pct)
            if _is_better_stop(side, lock_price, current_stop):
                current_stop = lock_price
        if policy.trailing_pct and mfe >= max(policy.trailing_pct, policy.profit_lock_pct or 0.0):
            trail = safe_float(trade.entry_price) * policy.trailing_pct / 100.0
            trailing_stop = (high - trail) if side == "LONG" else (low + trail)
            if _is_better_stop(side, trailing_stop, current_stop):
                current_stop = trailing_stop
        if policy.time_decay_bars and index - int(trade.entry_index) + 1 >= policy.time_decay_bars and mfe < 0.20:
            exit_price = close
            exit_reason = "TIME_DECAY"
            exit_index = index
            break
    else:
        if last > int(trade.entry_index):
            exit_index = last - 1
            exit_price = safe_float(ctx.candles.iloc[exit_index].get("close"))
            exit_reason = "DYNAMIC_HORIZON_CLOSE" if not direction_invalid else "DIRECTION_INVALIDATED"

    gross = gross_return_pct(side, safe_float(trade.entry_price), exit_price)
    breakdown = explain_cost_breakdown(
        source="trade_signal",
        side=side,
        entry_type="taker",
        exit_type="taker",
        slippage_bps=3.0,
        entry_time=ctx.candles.iloc[int(trade.entry_index)].get("timestamp") if "timestamp" in ctx.candles.columns else None,
        exit_time=ctx.candles.iloc[exit_index].get("timestamp") if "timestamp" in ctx.candles.columns and exit_index < len(ctx.candles) else None,
        outcome=exit_reason,
    )
    net = gross - breakdown.total_cost_bps / 100.0
    return DynamicHoldTrade(
        symbol=ctx.symbol,
        side=side,
        policy=policy.name,
        net_return_pct=net,
        gross_return_pct=gross,
        exit_reason=exit_reason,
        duration_bars=max(1, exit_index - int(trade.entry_index) + 1),
        mfe_pct=mfe,
        mae_pct=mae,
    )


def _baseline_trade(ctx: ReplayTradeContext, policy: str) -> DynamicHoldTrade:
    mfe, mae = _window_mfe_mae(ctx)
    return DynamicHoldTrade(
        symbol=ctx.symbol,
        side=str(ctx.trade.side).upper(),
        policy=policy,
        net_return_pct=safe_float(ctx.trade.net_return_pct),
        gross_return_pct=safe_float(ctx.trade.gross_return_pct),
        exit_reason=str(ctx.trade.exit_reason),
        duration_bars=int(ctx.trade.exit_index) - int(ctx.trade.entry_index) + 1,
        mfe_pct=mfe,
        mae_pct=mae,
    )


def _blocked_trade(ctx: ReplayTradeContext, policy: str) -> DynamicHoldTrade:
    return DynamicHoldTrade(
        symbol=ctx.symbol,
        side=str(ctx.trade.side).upper(),
        policy=policy,
        net_return_pct=0.0,
        gross_return_pct=0.0,
        exit_reason="BLOCKED_RESEARCH_ONLY",
        duration_bars=0,
        mfe_pct=0.0,
        mae_pct=0.0,
        blocked=True,
    )


def _summarise_policy(policy_name: str, trades: list[DynamicHoldTrade], baseline: list[DynamicHoldTrade]) -> DynamicHoldPolicyResult:
    net = [trade.net_return_pct for trade in trades]
    baseline_net = [trade.net_return_pct for trade in baseline]
    baseline_ev = sum(baseline_net) / max(len(baseline_net), 1)
    avg_bars = sum(trade.duration_bars for trade in trades) / max(len(trades), 1)
    baseline_bars = sum(trade.duration_bars for trade in baseline) / max(len(baseline), 1)
    by_symbol: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        by_symbol[trade.symbol].append(trade.net_return_pct)
    stability = {symbol: sum(values) / max(len(values), 1) for symbol, values in by_symbol.items()}
    loss_worsening = max(0.0, baseline_ev - (sum(net) / max(len(net), 1)))
    exposure_increase = (avg_bars - baseline_bars) / max(baseline_bars, 1.0)
    result = DynamicHoldPolicyResult(
        policy_name=policy_name,
        trades=len(trades),
        net_ev=sum(net) / max(len(net), 1),
        net_pf=net_pf(net),
        tp_pct=sum(1 for trade in trades if trade.exit_reason == "TAKE_PROFIT") / max(len(trades), 1),
        sl_pct=sum(1 for trade in trades if trade.exit_reason == "STOP_LOSS") / max(len(trades), 1),
        time_pct=sum(1 for trade in trades if "HORIZON" in trade.exit_reason or "TIME" in trade.exit_reason) / max(len(trades), 1),
        avg_bars=avg_bars,
        max_bars=max([trade.duration_bars for trade in trades] or [0]),
        avg_mfe=sum(trade.mfe_pct for trade in trades) / max(len(trades), 1),
        avg_mae=sum(trade.mae_pct for trade in trades) / max(len(trades), 1),
        max_drawdown=max_drawdown(net),
        missed_profit_recovered_pct=max(0.0, (sum(net) / max(len(net), 1)) - baseline_ev),
        loss_worsening_pct=loss_worsening,
        exposure_increase_pct=exposure_increase,
        stability_by_symbol=stability,
        decision="",
    )
    result.decision = _decision(result, baseline_ev)
    return result


def _decision(result: DynamicHoldPolicyResult, baseline_ev: float) -> str:
    if result.trades < 10:
        return REJECT_TOO_FEW_TRADES
    delta = result.net_ev - baseline_ev
    if delta < -0.01:
        return REJECT_WORSE_THAN_BASELINE
    if result.exposure_increase_pct > 0.80 and delta < 0.03:
        return REJECT_TOO_SENSITIVE
    if len([value for value in result.stability_by_symbol.values() if value > 0]) <= 1 and result.trades < 100:
        return REJECT_OVERFIT
    if result.net_ev > 0 and delta > 0.01 and result.net_pf > 1.0:
        return IMPROVES_BASELINE_RESEARCH_ONLY
    return WATCH_ONLY


def _direction_valid(candles: pd.DataFrame, index: int, side: str) -> bool:
    if index < 3:
        return True
    recent = candles.iloc[index - 3:index + 1]
    first = safe_float(recent.iloc[0].get("close"))
    last = safe_float(recent.iloc[-1].get("close"))
    if first <= 0 or last <= 0:
        return True
    move = gross_return_pct(side, first, last)
    range_pct = average_range_pct(candles, index, 8)
    return move >= -max(0.10, range_pct * 0.8)


def _price_at_pct(side: str, entry: float, pct: float) -> float:
    return entry * (1.0 + pct / 100.0) if side == "LONG" else entry * (1.0 - pct / 100.0)


def _is_better_stop(side: str, candidate: float, current: float) -> bool:
    return candidate > current if side == "LONG" else candidate < current


def _window_mfe_mae(ctx: ReplayTradeContext) -> tuple[float, float]:
    trade = ctx.trade
    segment = ctx.candles.iloc[int(trade.entry_index): int(trade.exit_index) + 1]
    if segment.empty:
        return 0.0, 0.0
    high = max(safe_float(row.get("high")) for _, row in segment.iterrows())
    low = min(safe_float(row.get("low")) for _, row in segment.iterrows())
    entry = safe_float(trade.entry_price)
    if str(trade.side).upper() == "LONG":
        return (high - entry) / entry * 100.0, (low - entry) / entry * 100.0
    return (entry - low) / entry * 100.0, (entry - high) / entry * 100.0


def _late_entry_risk(ctx: ReplayTradeContext) -> bool:
    move = prior_side_move_pct(ctx.candles, int(ctx.trade.entry_index), str(ctx.trade.side), 10)
    atr_like = average_range_pct(ctx.candles, int(ctx.trade.entry_index), 14)
    return move > max(1.0, atr_like * 2.5) and safe_float(ctx.trade.net_return_pct) <= 0


def _reversal_risk(ctx: ReplayTradeContext) -> bool:
    return _late_entry_risk(ctx) and str(ctx.trade.exit_reason) in {"STOP_LOSS", "HORIZON_CLOSE"}


def render_dynamic_hold_lab_text(report: DynamicHoldReport) -> str:
    lines = [
        "DYNAMIC HOLD LAB START",
        f"hours: {report.hours}",
        f"timeframe: {report.timeframe}",
        f"symbols: {','.join(report.symbols)}",
        f"baseline_trades: {report.baseline_trades}",
        f"baseline_net_ev: {report.baseline_net_ev:.6f}",
        "policy | trades | net_ev | net_pf | TP% | SL% | TIME% | avg_bars | decision",
    ]
    for policy in report.policies:
        lines.append(
            f"{policy.policy_name} | {policy.trades} | {policy.net_ev:.6f} | {policy.net_pf:.4f} | "
            f"{policy.tp_pct*100:.1f} | {policy.sl_pct*100:.1f} | {policy.time_pct*100:.1f} | "
            f"{policy.avg_bars:.2f} | {policy.decision}"
        )
    lines.extend([
        f"loader_statuses: {report.loader_statuses}",
        f"warnings: {', '.join(report.warnings) if report.warnings else 'none'}",
        "research_only: true",
        "no_lookahead_status: OK_PREFIX_ONLY",
        "final_recommendation: NO LIVE",
        "DYNAMIC HOLD LAB END",
    ])
    return "\n".join(lines)


def dynamic_hold_lab_text(config: Any, db: Any, *, hours: int = 72, timeframe: str = "5m", symbols: str | list[str] | None = None) -> str:
    return render_dynamic_hold_lab_text(run_dynamic_hold_lab(config, db, hours=hours, timeframe=timeframe, symbols=symbols))
