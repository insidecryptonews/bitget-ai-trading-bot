"""ResearchOps V5 — Fee-aware exit trainer (multi-symbol).

Wraps `net_profit_lock_lab` and tries a richer set of net-profit-lock
thresholds across multiple symbols, reporting per-symbol gross vs net and
labelling each profit-lock variant as promotion_eligible or not.

Hard rules:
  - never closes trades in runtime
  - never changes exit policy in the config
  - never opens orders
  - `maker_maker_audit_only` is never promotable
  - if `gross_green_net_negative`, the scenario is not promotable
  - if `net_ev <= 0`, the scenario is not promotable
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .net_profit_lock_lab import (
    DEFAULT_TP_FRACTIONS,
    NetProfitLockReport,
    NetProfitLockScenarioSummary,
    render_net_profit_lock_text,
    run_net_profit_lock_lab,
)
from .phase8_research_utils import FINAL_RECOMMENDATION, parse_symbols


# Net-profit-lock thresholds requested for V5: 0.40 / 0.60 / 0.80 / 1.00 /
# 1.20 / 1.50 / 2.00 (percent of price). Re-run for each symbol.
NET_PROFIT_LOCK_GRID: tuple[float, ...] = (0.40, 0.60, 0.80, 1.00, 1.20, 1.50, 2.00)


@dataclass
class FeeAwareExitSymbolResult:
    symbol: str
    net_profit_lock_pct: float
    decision: str
    promotion_eligible: bool
    scenarios: list[NetProfitLockScenarioSummary] = field(default_factory=list)
    best_scenario_name: str = ""
    best_net_ev: float = 0.0
    gross_green_net_negative: bool = False
    likely_issue: str = ""
    next_research: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "net_profit_lock_pct": self.net_profit_lock_pct,
            "decision": self.decision,
            "promotion_eligible": self.promotion_eligible,
            "scenarios": [scenario.as_dict() for scenario in self.scenarios],
            "best_scenario_name": self.best_scenario_name,
            "best_net_ev": self.best_net_ev,
            "gross_green_net_negative": self.gross_green_net_negative,
            "likely_issue": self.likely_issue,
            "next_research": self.next_research,
        }


@dataclass
class FeeAwareExitReport:
    symbols: list[str]
    timeframe: str
    hours: int
    net_profit_lock_grid: list[float]
    results: list[FeeAwareExitSymbolResult] = field(default_factory=list)
    best_per_symbol: dict[str, str] = field(default_factory=dict)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "timeframe": self.timeframe,
            "hours": self.hours,
            "net_profit_lock_grid": list(self.net_profit_lock_grid),
            "results": [result.as_dict() for result in self.results],
            "best_per_symbol": dict(self.best_per_symbol),
            "research_only": self.research_only,
            "paper_filter_enabled": self.paper_filter_enabled,
            "can_send_real_orders": self.can_send_real_orders,
            "final_recommendation": self.final_recommendation,
        }


def _promotion_eligible(scenario: NetProfitLockScenarioSummary) -> bool:
    """promotion_eligible is False if maker_maker_audit_only OR net_ev <= 0
    OR gross_green_net_negative."""
    if not getattr(scenario, "promotion_eligible", True):
        return False
    if str(scenario.scenario).startswith("maker_maker"):
        return False
    if scenario.net_ev <= 0:
        return False
    if getattr(scenario, "gross_green_net_negative", False):
        return False
    if scenario.net_pf <= 1.0:
        return False
    return True


def _best_scenario(report: NetProfitLockReport) -> NetProfitLockScenarioSummary | None:
    candidates = [s for s in report.scenarios if _promotion_eligible(s)]
    if candidates:
        return max(candidates, key=lambda s: s.net_ev)
    if report.scenarios:
        return max(report.scenarios, key=lambda s: s.net_ev)
    return None


def _decision_for_symbol(report: NetProfitLockReport, best: NetProfitLockScenarioSummary | None) -> str:
    if best is None or best.trades == 0:
        return "NEED_MORE_DATA"
    if _promotion_eligible(best):
        return "RESEARCH_PROMISING_NOT_ACTIONABLE"
    if best.net_ev <= 0 and best.gross_ev > 0:
        return "RESEARCH_GROSS_GREEN_NET_NEGATIVE"
    if best.net_ev <= 0:
        return "RESEARCH_NEGATIVE_NET"
    return "RESEARCH_ONLY"


def run_fee_aware_exit_trainer(
    config: Any,
    db: Any,
    *,
    hours: int = 720,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
    net_profit_lock_grid: tuple[float, ...] = NET_PROFIT_LOCK_GRID,
    tp_fractions: tuple[float, float, float] = DEFAULT_TP_FRACTIONS,
    break_even_buffer_pct: float = 0.05,
    max_holding_bars: int = 40,
) -> FeeAwareExitReport:
    symbol_list = parse_symbols(symbols, config)
    if not symbol_list:
        symbol_list = ["BTCUSDT", "ETHUSDT", "DOTUSDT"]
    results: list[FeeAwareExitSymbolResult] = []
    best_per_symbol: dict[str, str] = {}
    for symbol in symbol_list:
        # We run the net-profit-lock lab once per net_profit_lock_pct and keep
        # the best per-symbol scenario per threshold.
        best_for_symbol: FeeAwareExitSymbolResult | None = None
        for net_profit_lock in net_profit_lock_grid:
            inner = run_net_profit_lock_lab(
                config, db,
                hours=hours, timeframe=timeframe, symbols=[symbol],
                tp_fractions=tp_fractions,
                net_profit_lock_pct=float(net_profit_lock),
                break_even_buffer_pct=float(break_even_buffer_pct),
                max_holding_bars=int(max_holding_bars),
            )
            chosen = _best_scenario(inner)
            decision = _decision_for_symbol(inner, chosen)
            promotion_eligible = bool(chosen and _promotion_eligible(chosen))
            symbol_result = FeeAwareExitSymbolResult(
                symbol=symbol,
                net_profit_lock_pct=float(net_profit_lock),
                decision=decision,
                promotion_eligible=promotion_eligible,
                scenarios=list(inner.scenarios),
                best_scenario_name=chosen.scenario if chosen else "",
                best_net_ev=chosen.net_ev if chosen else 0.0,
                gross_green_net_negative=bool(getattr(inner, "gross_green_net_negative", False)),
                likely_issue=getattr(inner, "likely_issue", ""),
                next_research=getattr(inner, "next_research", ""),
            )
            results.append(symbol_result)
            if best_for_symbol is None or (symbol_result.promotion_eligible and not best_for_symbol.promotion_eligible) or symbol_result.best_net_ev > best_for_symbol.best_net_ev:
                best_for_symbol = symbol_result
        if best_for_symbol is not None:
            best_per_symbol[symbol] = (
                f"{best_for_symbol.best_scenario_name}@npl={best_for_symbol.net_profit_lock_pct:.2f}"
                f" net_ev={best_for_symbol.best_net_ev:.6f} promotable={best_for_symbol.promotion_eligible}"
            )
    return FeeAwareExitReport(
        symbols=symbol_list,
        timeframe=timeframe,
        hours=int(hours),
        net_profit_lock_grid=list(net_profit_lock_grid),
        results=results,
        best_per_symbol=best_per_symbol,
    )


def render_fee_aware_exit_text(report: FeeAwareExitReport) -> str:
    lines = [
        "FEE AWARE EXIT TRAINER START",
        f"symbols: {','.join(report.symbols)}",
        f"timeframe: {report.timeframe}",
        f"hours: {report.hours}",
        f"net_profit_lock_grid: {report.net_profit_lock_grid}",
        "symbol | npl% | decision | promotable | best_scenario | best_net_ev | gross_green_net_negative | likely_issue",
    ]
    for result in report.results:
        lines.append(
            f"{result.symbol} | {result.net_profit_lock_pct:.2f} | {result.decision} | "
            f"{str(result.promotion_eligible).lower()} | {result.best_scenario_name or '-'} | "
            f"{result.best_net_ev:.6f} | {str(result.gross_green_net_negative).lower()} | "
            f"{result.likely_issue or '-'}"
        )
    if report.best_per_symbol:
        lines.append("best_per_symbol:")
        for symbol in sorted(report.best_per_symbol.keys()):
            lines.append(f"- {symbol}: {report.best_per_symbol[symbol]}")
    lines.extend([
        "maker_maker_audit_only_never_promotes: true",
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "final_recommendation: NO LIVE",
        "FEE AWARE EXIT TRAINER END",
    ])
    return "\n".join(lines)
