"""Phase 9 — Net Profit Lock / TP Ladder research lab.

Fee-aware simulation of TP1/TP2/TP3 ladder exits and net-profit-lock variants.
The lab subtracts the cost-stress assumption from every gross exit before
classifying TP/SL hits, so "verde bruto / negativo neto" trades cannot count as
wins.

Research only:
- never opens orders
- never modifies exit policy in runtime
- only reads OHLCV via the existing replay loader

The maker_maker_audit_only scenario is kept ONLY as an audit comparison and is
explicitly marked `promotion_eligible=False`, mirroring the Phase 8B contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from .phase8_research_utils import (
    FINAL_RECOMMENDATION,
    NO_LOOKAHEAD_STATUS,
    STOP_TP_SAME_BAR_RULE,
    ReplayTradeContext,
    load_replay_trade_contexts,
    net_pf,
    parse_symbols,
    side_direction,
)
from .utils import safe_float


# Cost levels in PERCENT round-trip. Same constants as Phase 8B cost stress
# so the two labs report comparable numbers.
COST_BASE_PCT = 0.18
COST_022_PCT = 0.22
COST_025_PCT = 0.25
COST_MAKER_MAKER_AUDIT_ONLY_PCT = 0.04

# Default TP1/TP2/TP3 fractions of the original `take_profit_1` distance.
# Conservative defaults — designed to be re-runnable, not optimised.
DEFAULT_TP_FRACTIONS: tuple[float, float, float] = (0.50, 1.00, 1.50)

EXIT_TP_LADDER_TP1 = "TP_LADDER_TP1"
EXIT_TP_LADDER_TP2 = "TP_LADDER_TP2"
EXIT_TP_LADDER_TP3 = "TP_LADDER_TP3"
EXIT_NET_PROFIT_LOCK = "NET_PROFIT_LOCK"
EXIT_BREAK_EVEN_POST_FEES = "BREAK_EVEN_POST_FEES"
EXIT_STOP_LOSS = "STOP_LOSS"
EXIT_HORIZON_CLOSE = "HORIZON_CLOSE"


@dataclass
class NetProfitLockTrade:
    symbol: str
    side: str
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    gross_return_pct: float
    cost_pct: float
    net_return_pct: float
    exit_reason: str
    duration_bars: int
    tp1_hit: bool
    tp2_hit: bool
    tp3_hit: bool
    net_profit_lock_hit: bool
    break_even_after_fees_hit: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NetProfitLockScenarioSummary:
    scenario: str
    cost_pct: float
    promotion_eligible: bool
    gross_green_net_negative: bool
    trades: int
    gross_ev: float
    net_ev: float
    fees_total_pct: float
    win_rate: float
    net_pf: float
    tp1_hit_rate: float
    tp2_hit_rate: float
    tp3_hit_rate: float
    stop_hit_rate: float
    time_hit_rate: float
    net_profit_lock_hit_rate: float
    break_even_after_fees_rate: float
    avg_duration_bars: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NetProfitLockReport:
    symbols: list[str]
    timeframe: str
    hours: int
    tp_fractions: tuple[float, float, float]
    net_profit_lock_pct: float
    break_even_after_fees_buffer_pct: float
    contexts_count: int
    scenarios: list[NetProfitLockScenarioSummary] = field(default_factory=list)
    lock_sensitivity: list[NetProfitLockScenarioSummary] = field(default_factory=list)
    decision: str = "RESEARCH_ONLY"
    gross_green_net_negative: bool = False
    likely_issue: str = ""
    next_research: str = ""
    loader_statuses: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    research_only: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION
    no_lookahead_status: str = NO_LOOKAHEAD_STATUS
    stop_tp_same_bar_rule: str = STOP_TP_SAME_BAR_RULE

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "timeframe": self.timeframe,
            "hours": self.hours,
            "tp_fractions": list(self.tp_fractions),
            "net_profit_lock_pct": self.net_profit_lock_pct,
            "break_even_after_fees_buffer_pct": self.break_even_after_fees_buffer_pct,
            "contexts_count": self.contexts_count,
            "scenarios": [scenario.as_dict() for scenario in self.scenarios],
            "lock_sensitivity": [scenario.as_dict() for scenario in self.lock_sensitivity],
            "decision": self.decision,
            "gross_green_net_negative": self.gross_green_net_negative,
            "likely_issue": self.likely_issue,
            "next_research": self.next_research,
            "loader_statuses": self.loader_statuses,
            "warnings": self.warnings,
            "research_only": self.research_only,
            "final_recommendation": self.final_recommendation,
            "no_lookahead_status": self.no_lookahead_status,
            "stop_tp_same_bar_rule": self.stop_tp_same_bar_rule,
        }


def _price_at_pct_distance(side: str, entry_price: float, distance_pct: float) -> float:
    if entry_price <= 0:
        return 0.0
    direction = side_direction(side)
    return entry_price * (1.0 + (distance_pct / 100.0) * direction)


def _gross_return_from_prices(side: str, entry: float, exit_price: float) -> float:
    if entry <= 0:
        return 0.0
    return ((exit_price - entry) / entry) * 100.0 * side_direction(side)


def _simulate_one_ladder_trade(
    ctx: ReplayTradeContext,
    *,
    cost_pct: float,
    tp_fractions: tuple[float, float, float],
    net_profit_lock_pct: float,
    break_even_buffer_pct: float,
    max_holding_bars: int = 40,
) -> NetProfitLockTrade:
    trade = ctx.trade
    side = str(trade.side).upper()
    entry = safe_float(trade.entry_price)
    if entry <= 0:
        return NetProfitLockTrade(
            symbol=str(ctx.symbol).upper(),
            side=side,
            entry_index=int(trade.entry_index),
            exit_index=int(trade.exit_index),
            entry_price=entry,
            exit_price=0.0,
            gross_return_pct=0.0,
            cost_pct=cost_pct,
            net_return_pct=0.0,
            exit_reason=EXIT_HORIZON_CLOSE,
            duration_bars=0,
            tp1_hit=False, tp2_hit=False, tp3_hit=False,
            net_profit_lock_hit=False, break_even_after_fees_hit=False,
        )
    tp1_distance = abs(safe_float(trade.take_profit_1) - entry) / entry * 100.0 if safe_float(trade.take_profit_1) > 0 else 0.6
    tp1_pct = tp1_distance * tp_fractions[0]
    tp2_pct = tp1_distance * tp_fractions[1]
    tp3_pct = tp1_distance * tp_fractions[2]
    tp1_price = _price_at_pct_distance(side, entry, tp1_pct)
    tp2_price = _price_at_pct_distance(side, entry, tp2_pct)
    tp3_price = _price_at_pct_distance(side, entry, tp3_pct)
    stop_price = safe_float(trade.stop_loss)
    if stop_price <= 0:
        stop_price = _price_at_pct_distance(side, entry, -max(tp1_distance, 0.5))
    exit_index = min(int(trade.entry_index) + max_holding_bars, len(ctx.candles) - 1)
    exit_price = 0.0
    exit_reason = EXIT_HORIZON_CLOSE
    tp1_hit = False
    tp2_hit = False
    tp3_hit = False
    net_lock_hit = False
    be_hit = False
    last = min(len(ctx.candles), int(trade.entry_index) + max_holding_bars)
    for index in range(int(trade.entry_index), last):
        row = ctx.candles.iloc[index]
        high = safe_float(row.get("high"))
        low = safe_float(row.get("low"))
        if high <= 0 or low <= 0:
            continue
        if side == "LONG":
            stop_hit = low <= stop_price
            tp1_local = high >= tp1_price
            tp2_local = high >= tp2_price
            tp3_local = high >= tp3_price
            net_lock_local = high >= _price_at_pct_distance(side, entry, net_profit_lock_pct + cost_pct)
            be_local = high >= _price_at_pct_distance(side, entry, break_even_buffer_pct + cost_pct)
        else:
            stop_hit = high >= stop_price
            tp1_local = low <= tp1_price
            tp2_local = low <= tp2_price
            tp3_local = low <= tp3_price
            net_lock_local = low <= _price_at_pct_distance(side, entry, net_profit_lock_pct + cost_pct)
            be_local = low <= _price_at_pct_distance(side, entry, break_even_buffer_pct + cost_pct)
        # STOP_BEFORE_TP rule preserved.
        if stop_hit:
            exit_price = stop_price
            exit_reason = EXIT_STOP_LOSS
            exit_index = index
            break
        if tp3_local:
            tp1_hit = tp1_hit or tp1_local
            tp2_hit = tp2_hit or tp2_local
            tp3_hit = True
            exit_price = tp3_price
            exit_reason = EXIT_TP_LADDER_TP3
            exit_index = index
            break
        if net_lock_local and not net_lock_hit:
            net_lock_hit = True
            exit_price = _price_at_pct_distance(side, entry, net_profit_lock_pct + cost_pct)
            exit_reason = EXIT_NET_PROFIT_LOCK
            exit_index = index
            break
        if tp2_local:
            tp1_hit = tp1_hit or tp1_local
            tp2_hit = True
            exit_price = tp2_price
            exit_reason = EXIT_TP_LADDER_TP2
            exit_index = index
            break
        if tp1_local and not tp1_hit:
            tp1_hit = True
            # Stay in trade after TP1 — break-even moves to entry + cost buffer.
            stop_price = _price_at_pct_distance(side, entry, break_even_buffer_pct + cost_pct)
            be_hit = True
        if be_local and not be_hit:
            be_hit = True
    if exit_price <= 0:
        # Horizon close at the exit row of the original trade.
        try:
            exit_row = ctx.candles.iloc[int(trade.exit_index)]
            exit_price = safe_float(exit_row.get("close"))
        except Exception:
            exit_price = entry
        exit_reason = EXIT_HORIZON_CLOSE
        exit_index = int(trade.exit_index)
    gross = _gross_return_from_prices(side, entry, exit_price)
    net = gross - cost_pct
    return NetProfitLockTrade(
        symbol=str(ctx.symbol).upper(),
        side=side,
        entry_index=int(trade.entry_index),
        exit_index=int(exit_index),
        entry_price=entry,
        exit_price=exit_price,
        gross_return_pct=gross,
        cost_pct=cost_pct,
        net_return_pct=net,
        exit_reason=exit_reason,
        duration_bars=int(exit_index) - int(trade.entry_index) + 1,
        tp1_hit=tp1_hit,
        tp2_hit=tp2_hit,
        tp3_hit=tp3_hit,
        net_profit_lock_hit=net_lock_hit,
        break_even_after_fees_hit=be_hit,
    )


def _summarise_scenario(
    scenario_name: str,
    cost_pct: float,
    *,
    promotion_eligible: bool,
    trades: list[NetProfitLockTrade],
) -> NetProfitLockScenarioSummary:
    if not trades:
        return NetProfitLockScenarioSummary(
            scenario=scenario_name,
            cost_pct=cost_pct,
            promotion_eligible=False,
            gross_green_net_negative=False,
            trades=0,
            gross_ev=0.0,
            net_ev=0.0,
            fees_total_pct=0.0,
            win_rate=0.0,
            net_pf=0.0,
            tp1_hit_rate=0.0,
            tp2_hit_rate=0.0,
            tp3_hit_rate=0.0,
            stop_hit_rate=0.0,
            time_hit_rate=0.0,
            net_profit_lock_hit_rate=0.0,
            break_even_after_fees_rate=0.0,
            avg_duration_bars=0.0,
        )
    gross = [t.gross_return_pct for t in trades]
    net = [t.net_return_pct for t in trades]
    wins = [v for v in net if v > 0]
    gross_ev = sum(gross) / len(trades)
    net_ev = sum(net) / len(trades)
    net_pf_value = net_pf(net)
    gross_green_net_negative = bool(gross_ev > 0 and net_ev < 0)
    effective_promotion_eligible = bool(
        promotion_eligible
        and net_ev > 0
        and net_pf_value > 1.0
        and not gross_green_net_negative
    )
    return NetProfitLockScenarioSummary(
        scenario=scenario_name,
        cost_pct=cost_pct,
        promotion_eligible=effective_promotion_eligible,
        gross_green_net_negative=gross_green_net_negative,
        trades=len(trades),
        gross_ev=gross_ev,
        net_ev=net_ev,
        fees_total_pct=cost_pct * len(trades),
        win_rate=len(wins) / len(trades),
        net_pf=net_pf_value,
        tp1_hit_rate=sum(1 for t in trades if t.tp1_hit) / len(trades),
        tp2_hit_rate=sum(1 for t in trades if t.tp2_hit) / len(trades),
        tp3_hit_rate=sum(1 for t in trades if t.tp3_hit) / len(trades),
        stop_hit_rate=sum(1 for t in trades if t.exit_reason == EXIT_STOP_LOSS) / len(trades),
        time_hit_rate=sum(1 for t in trades if t.exit_reason == EXIT_HORIZON_CLOSE) / len(trades),
        net_profit_lock_hit_rate=sum(1 for t in trades if t.net_profit_lock_hit) / len(trades),
        break_even_after_fees_rate=sum(1 for t in trades if t.break_even_after_fees_hit) / len(trades),
        avg_duration_bars=sum(t.duration_bars for t in trades) / len(trades),
    )


def run_net_profit_lock_lab(
    config: Any,
    db: Any,
    *,
    hours: int = 168,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
    tp_fractions: tuple[float, float, float] = DEFAULT_TP_FRACTIONS,
    net_profit_lock_pct: float = 0.40,
    break_even_buffer_pct: float = 0.05,
    max_holding_bars: int = 40,
) -> NetProfitLockReport:
    symbol_list = parse_symbols(symbols, config)
    bundle = load_replay_trade_contexts(
        config, db, hours=hours, timeframe=timeframe, symbols=symbol_list,
    )
    scenarios = [
        ("base_cost_0_18", COST_BASE_PCT, True),
        ("stress_0_22", COST_022_PCT, True),
        ("stress_0_25", COST_025_PCT, True),
        ("maker_maker_audit_only", COST_MAKER_MAKER_AUDIT_ONLY_PCT, False),
    ]
    summaries: list[NetProfitLockScenarioSummary] = []
    for scenario_name, cost_pct, promotion_eligible in scenarios:
        trades = [
            _simulate_one_ladder_trade(
                ctx,
                cost_pct=cost_pct,
                tp_fractions=tp_fractions,
                net_profit_lock_pct=net_profit_lock_pct,
                break_even_buffer_pct=break_even_buffer_pct,
                max_holding_bars=max_holding_bars,
            )
            for ctx in bundle.contexts
        ]
        summaries.append(_summarise_scenario(
            scenario_name, cost_pct,
            promotion_eligible=promotion_eligible,
            trades=trades,
        ))
    lock_sensitivity: list[NetProfitLockScenarioSummary] = []
    for lock_pct in (0.40, 0.60, 0.80, 1.00, 1.20):
        trades = [
            _simulate_one_ladder_trade(
                ctx,
                cost_pct=COST_BASE_PCT,
                tp_fractions=tp_fractions,
                net_profit_lock_pct=lock_pct,
                break_even_buffer_pct=break_even_buffer_pct,
                max_holding_bars=max_holding_bars,
            )
            for ctx in bundle.contexts
        ]
        lock_sensitivity.append(_summarise_scenario(
            f"net_profit_lock_{str(lock_pct).replace('.', '_')}",
            COST_BASE_PCT,
            promotion_eligible=True,
            trades=trades,
        ))
    base = next((s for s in summaries if s.scenario == "base_cost_0_18"), None)
    decision = "RESEARCH_ONLY"
    gross_green_net_negative = bool(base and base.gross_green_net_negative)
    if gross_green_net_negative:
        decision = "RESEARCH_GREEN_GROSS_NEGATIVE_NET"
    elif base and base.net_ev <= 0:
        decision = "RESEARCH_NEGATIVE_NET"
    elif base and base.net_ev > 0:
        decision = "RESEARCH_POSITIVE_NET_BUT_NOT_PAPER_READY"
    return NetProfitLockReport(
        symbols=symbol_list,
        timeframe=timeframe,
        hours=int(hours),
        tp_fractions=tuple(float(value) for value in tp_fractions),
        net_profit_lock_pct=float(net_profit_lock_pct),
        break_even_after_fees_buffer_pct=float(break_even_buffer_pct),
        contexts_count=len(bundle.contexts),
        scenarios=summaries,
        lock_sensitivity=lock_sensitivity,
        decision=decision,
        gross_green_net_negative=gross_green_net_negative,
        likely_issue="profit_lock_or_exit_too_tight_after_costs" if gross_green_net_negative else "",
        next_research="test_wider_net_profit_locks_and_directional_hold",
        loader_statuses=bundle.loader_statuses,
        warnings=list(bundle.warnings),
    )


def render_net_profit_lock_text(report: NetProfitLockReport) -> str:
    lines = [
        "NET PROFIT LOCK LAB START",
        f"symbols: {','.join(report.symbols)}",
        f"timeframe: {report.timeframe}",
        f"hours: {report.hours}",
        f"tp_fractions: {report.tp_fractions}",
        f"net_profit_lock_pct: {report.net_profit_lock_pct}",
        f"break_even_after_fees_buffer_pct: {report.break_even_after_fees_buffer_pct}",
        f"contexts_count: {report.contexts_count}",
        f"decision: {report.decision}",
        f"gross_green_net_negative: {str(report.gross_green_net_negative).lower()}",
        f"likely_issue: {report.likely_issue or 'none'}",
        f"next_research: {report.next_research}",
        "scenario | cost | promotion_eligible | gross_green_net_negative | trades | gross_ev | net_ev | fees_total | win | pf | tp1 | tp2 | tp3 | stop | time | npl | be | avg_bars",
    ]
    for scenario in report.scenarios:
        lines.append(
            f"{scenario.scenario} | {scenario.cost_pct:.4f} | {scenario.promotion_eligible} | "
            f"{str(scenario.gross_green_net_negative).lower()} | "
            f"{scenario.trades} | {scenario.gross_ev:.6f} | {scenario.net_ev:.6f} | "
            f"{scenario.fees_total_pct:.4f} | {scenario.win_rate:.3f} | {scenario.net_pf:.4f} | "
            f"{scenario.tp1_hit_rate:.3f} | {scenario.tp2_hit_rate:.3f} | {scenario.tp3_hit_rate:.3f} | "
            f"{scenario.stop_hit_rate:.3f} | {scenario.time_hit_rate:.3f} | "
            f"{scenario.net_profit_lock_hit_rate:.3f} | {scenario.break_even_after_fees_rate:.3f} | "
            f"{scenario.avg_duration_bars:.1f}"
        )
    lines.append("lock_sensitivity_research_only:")
    for scenario in report.lock_sensitivity:
        lines.append(
            f"{scenario.scenario} | cost={scenario.cost_pct:.4f} | promotion_eligible={scenario.promotion_eligible} | "
            f"gross_green_net_negative={str(scenario.gross_green_net_negative).lower()} | "
            f"trades={scenario.trades} | gross_ev={scenario.gross_ev:.6f} | "
            f"net_ev={scenario.net_ev:.6f} | net_pf={scenario.net_pf:.4f}"
        )
    if report.warnings:
        lines.append("warnings:")
        for warning in report.warnings[:6]:
            lines.append(f"- {warning}")
    lines.extend([
        "maker_maker_audit_only_never_promotes: true",
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "activation: disabled",
        "final_recommendation: NO LIVE",
        "NET PROFIT LOCK LAB END",
    ])
    return "\n".join(lines)


def net_profit_lock_text(
    config: Any,
    db: Any,
    *,
    hours: int = 168,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
) -> str:
    return render_net_profit_lock_text(run_net_profit_lock_lab(
        config, db, hours=hours, timeframe=timeframe, symbols=symbols,
    ))
