"""ResearchOps V5 — Capital / Margin / Notional / Leverage simulator.

Pure math, research-only. Produces a matrix of (margin × leverage) scenarios
for the OHLCV-replayed trades of a symbol and reports gross/net PnL in both
percent and USDT, plus break-even price moves and ROE.

Hard contract:
  - never call set_leverage / set_margin_mode
  - never change config.default_leverage / config.trade_margin_usdt
  - never enable paper filter, never open real orders
  - leverage is *informational*; the dashboard renders it as `simulated_leverage`
  - ROE high is NEVER promoted: if net_pnl_usdt <= 0 the scenario is
    marked `promotion_eligible=False`
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .phase8_research_utils import (
    FINAL_RECOMMENDATION,
    ReplayTradeContext,
    load_replay_trade_contexts,
    parse_symbols,
)
from .utils import safe_float


# Bitget VIP0 default round-trip cost in pct of notional. Tunable per scenario.
DEFAULT_BASE_COST_PCT = 0.18
DEFAULT_SLIPPAGE_BUFFER_PCT = 0.04


@dataclass
class CapitalLeverageScenario:
    capital_total_usdt: float
    margin_per_trade_usdt: float
    leverage: int
    notional_usdt: float
    base_cost_pct: float
    slippage_buffer_pct: float
    trades: int
    avg_price_move_pct: float
    gross_pnl_usdt: float
    fees_open_usdt: float
    fees_close_usdt: float
    slippage_usdt: float
    funding_estimate_usdt: float
    net_pnl_usdt: float
    net_pnl_pct_on_margin: float
    net_pnl_pct_on_notional: float
    roe_pct: float
    min_price_move_to_break_even_pct: float
    min_price_move_to_profit_after_buffer_pct: float
    liquidation_distance_estimate_pct: float
    promotion_eligible: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CapitalLeverageReport:
    symbols: list[str]
    timeframe: str
    hours: int
    capital_total_usdt: float
    margins: list[float]
    leverages: list[int]
    base_cost_pct: float
    slippage_buffer_pct: float
    scenarios: list[CapitalLeverageScenario] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION
    warning: str = "ROE_high_does_not_equal_edge_if_net_ev_negative"

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "timeframe": self.timeframe,
            "hours": self.hours,
            "capital_total_usdt": self.capital_total_usdt,
            "margins": list(self.margins),
            "leverages": list(self.leverages),
            "base_cost_pct": self.base_cost_pct,
            "slippage_buffer_pct": self.slippage_buffer_pct,
            "scenarios": [scenario.as_dict() for scenario in self.scenarios],
            "research_only": self.research_only,
            "paper_filter_enabled": self.paper_filter_enabled,
            "can_send_real_orders": self.can_send_real_orders,
            "final_recommendation": self.final_recommendation,
            "warning": self.warning,
        }


def _avg(values: list[float]) -> float:
    return sum(values) / max(len(values), 1)


def _trade_price_moves(contexts: Iterable[ReplayTradeContext]) -> list[float]:
    moves: list[float] = []
    for ctx in contexts:
        trade = ctx.trade
        entry = safe_float(trade.entry_price)
        exit_price = safe_float(trade.exit_price)
        if entry <= 0 or exit_price <= 0:
            continue
        direction = 1.0 if str(trade.side).upper() == "LONG" else -1.0
        moves.append(((exit_price - entry) / entry) * 100.0 * direction)
    return moves


def _liquidation_distance_estimate_pct(leverage: int) -> float:
    """Conservative liquidation-distance estimate for a USDT-M cross-isolated
    perp with 0% maintenance margin slack. ONLY informational."""
    if leverage <= 0:
        return 100.0
    # ~95% of (1/L) — leaves 5% buffer to represent maintenance margin.
    return (1.0 / float(leverage)) * 100.0 * 0.95


def _scenario_from_moves(
    *,
    capital_total: float,
    margin: float,
    leverage: int,
    base_cost_pct: float,
    slippage_buffer_pct: float,
    moves: list[float],
) -> CapitalLeverageScenario:
    notional = margin * max(1, leverage)
    avg_move = _avg(moves)
    gross_pnl_usdt = (avg_move / 100.0) * notional * max(1, len(moves))
    # Per-trade fees scale with notional. Use half the base round-trip per side.
    fee_per_side_pct = base_cost_pct / 2.0
    fees_open_usdt = (fee_per_side_pct / 100.0) * notional * max(1, len(moves))
    fees_close_usdt = fees_open_usdt
    slippage_usdt = (slippage_buffer_pct / 100.0) * notional * max(1, len(moves))
    funding_estimate_usdt = 0.0  # No funding model invoked; flagged as 0.0.
    net_pnl_usdt = gross_pnl_usdt - fees_open_usdt - fees_close_usdt - slippage_usdt - funding_estimate_usdt
    net_pnl_pct_on_margin = (net_pnl_usdt / (margin * max(1, len(moves)))) * 100.0 if margin > 0 else 0.0
    net_pnl_pct_on_notional = (net_pnl_usdt / (notional * max(1, len(moves)))) * 100.0 if notional > 0 else 0.0
    roe_pct = net_pnl_pct_on_margin  # ROE per trade aggregated.
    min_break_even = base_cost_pct + slippage_buffer_pct
    min_profit_after_buffer = min_break_even + 0.05
    liq_pct = _liquidation_distance_estimate_pct(leverage)
    return CapitalLeverageScenario(
        capital_total_usdt=capital_total,
        margin_per_trade_usdt=margin,
        leverage=int(leverage),
        notional_usdt=notional,
        base_cost_pct=base_cost_pct,
        slippage_buffer_pct=slippage_buffer_pct,
        trades=len(moves),
        avg_price_move_pct=avg_move,
        gross_pnl_usdt=gross_pnl_usdt,
        fees_open_usdt=fees_open_usdt,
        fees_close_usdt=fees_close_usdt,
        slippage_usdt=slippage_usdt,
        funding_estimate_usdt=funding_estimate_usdt,
        net_pnl_usdt=net_pnl_usdt,
        net_pnl_pct_on_margin=net_pnl_pct_on_margin,
        net_pnl_pct_on_notional=net_pnl_pct_on_notional,
        roe_pct=roe_pct,
        min_price_move_to_break_even_pct=min_break_even,
        min_price_move_to_profit_after_buffer_pct=min_profit_after_buffer,
        liquidation_distance_estimate_pct=liq_pct,
        promotion_eligible=(net_pnl_usdt > 0),
    )


def run_capital_leverage_simulator(
    config: Any,
    db: Any,
    *,
    hours: int = 720,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
    capital_total_usdt: float = 40.0,
    margins: tuple[float, ...] = (2.0, 5.0, 10.0, 20.0),
    leverages: tuple[int, ...] = (1, 3, 5, 10, 20, 50),
    base_cost_pct: float = DEFAULT_BASE_COST_PCT,
    slippage_buffer_pct: float = DEFAULT_SLIPPAGE_BUFFER_PCT,
) -> CapitalLeverageReport:
    symbol_list = parse_symbols(symbols, config)
    bundle = load_replay_trade_contexts(
        config, db, hours=hours, timeframe=timeframe, symbols=symbol_list,
    )
    moves = _trade_price_moves(bundle.contexts)
    scenarios: list[CapitalLeverageScenario] = []
    for margin in margins:
        for leverage in leverages:
            if (margin * max(1, leverage)) > capital_total_usdt * 10:
                # Skip unrealistic combinations where leveraged notional would
                # exceed 10x capital — still informational, not a hard block.
                pass
            scenarios.append(_scenario_from_moves(
                capital_total=capital_total_usdt,
                margin=margin,
                leverage=int(leverage),
                base_cost_pct=base_cost_pct,
                slippage_buffer_pct=slippage_buffer_pct,
                moves=moves,
            ))
    return CapitalLeverageReport(
        symbols=symbol_list,
        timeframe=timeframe,
        hours=int(hours),
        capital_total_usdt=float(capital_total_usdt),
        margins=[float(m) for m in margins],
        leverages=[int(l) for l in leverages],
        base_cost_pct=float(base_cost_pct),
        slippage_buffer_pct=float(slippage_buffer_pct),
        scenarios=scenarios,
    )


def render_capital_leverage_text(report: CapitalLeverageReport) -> str:
    lines = [
        "CAPITAL LEVERAGE SIMULATOR START",
        f"symbols: {','.join(report.symbols)}",
        f"timeframe: {report.timeframe}",
        f"hours: {report.hours}",
        f"capital_total_usdt: {report.capital_total_usdt:.2f}",
        f"base_cost_pct: {report.base_cost_pct:.4f}",
        f"slippage_buffer_pct: {report.slippage_buffer_pct:.4f}",
        f"warning: {report.warning}",
        "margin | leverage | notional | trades | avg_move% | gross_usdt | fees_usdt | slip_usdt | net_usdt | net_pct_margin | net_pct_notional | roe% | be% | min_profit% | liq_dist% | promotable",
    ]
    for scenario in report.scenarios:
        fees_total = scenario.fees_open_usdt + scenario.fees_close_usdt
        lines.append(
            f"{scenario.margin_per_trade_usdt:.2f} | {scenario.leverage} | "
            f"{scenario.notional_usdt:.2f} | {scenario.trades} | "
            f"{scenario.avg_price_move_pct:.4f} | "
            f"{scenario.gross_pnl_usdt:.4f} | {fees_total:.4f} | "
            f"{scenario.slippage_usdt:.4f} | {scenario.net_pnl_usdt:.4f} | "
            f"{scenario.net_pnl_pct_on_margin:.4f} | "
            f"{scenario.net_pnl_pct_on_notional:.4f} | "
            f"{scenario.roe_pct:.4f} | "
            f"{scenario.min_price_move_to_break_even_pct:.4f} | "
            f"{scenario.min_price_move_to_profit_after_buffer_pct:.4f} | "
            f"{scenario.liquidation_distance_estimate_pct:.4f} | "
            f"{str(scenario.promotion_eligible).lower()}"
        )
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "no_set_leverage_call: true",
        "no_set_margin_mode_call: true",
        "no_config_changes: true",
        "final_recommendation: NO LIVE",
        "CAPITAL LEVERAGE SIMULATOR END",
    ])
    return "\n".join(lines)
