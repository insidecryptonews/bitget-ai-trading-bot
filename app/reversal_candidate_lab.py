from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from .phase8_research_utils import (
    FINAL_RECOMMENDATION,
    NO_LOOKAHEAD_STATUS,
    ReplayTradeContext,
    favorable_adverse_from_price,
    load_replay_trade_contexts,
)
from .utils import safe_float


REJECT = "REJECT"
WATCH_ONLY = "WATCH_ONLY"
NEED_MORE_DATA = "NEED_MORE_DATA"
RESEARCH_PROMISING_NOT_ACTIONABLE = "RESEARCH_PROMISING_NOT_ACTIONABLE"


@dataclass
class ReversalCandidateItem:
    symbol: str
    original_side: str
    opposite_side: str
    exit_reason: str
    exit_index: int
    confirmation_bars: int | None
    reversal_mfe_pct: float
    reversal_mae_pct: float
    false_positive: bool
    decision: str
    auto_flip: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReversalCandidateReport:
    hours: int
    timeframe: str
    symbols: list[str]
    total_trades: int
    reversal_opportunities: int
    items: list[ReversalCandidateItem] = field(default_factory=list)
    by_decision: dict[str, int] = field(default_factory=dict)
    by_symbol: dict[str, dict[str, int]] = field(default_factory=dict)
    loader_statuses: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    final_recommendation: str = FINAL_RECOMMENDATION
    research_only: bool = True
    no_lookahead_status: str = NO_LOOKAHEAD_STATUS
    auto_flip_enabled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "hours": self.hours,
            "timeframe": self.timeframe,
            "symbols": self.symbols,
            "total_trades": self.total_trades,
            "reversal_opportunities": self.reversal_opportunities,
            "false_reversal_traps": sum(1 for item in self.items if item.false_positive),
            "avg_confirmation_bars": _avg([item.confirmation_bars for item in self.items if item.confirmation_bars is not None]),
            "items": [item.as_dict() for item in self.items],
            "by_decision": self.by_decision,
            "by_symbol": self.by_symbol,
            "loader_statuses": self.loader_statuses,
            "warnings": self.warnings,
            "final_recommendation": self.final_recommendation,
            "research_only": self.research_only,
            "no_lookahead_status": self.no_lookahead_status,
            "auto_flip_enabled": self.auto_flip_enabled,
        }


def analyse_reversal_candidate(ctx: ReplayTradeContext, *, future_bars: int = 30) -> ReversalCandidateItem:
    trade = ctx.trade
    original = str(trade.side).upper()
    opposite = "SHORT" if original == "LONG" else "LONG"
    start = int(trade.exit_index) + 1
    end = min(len(ctx.candles), start + max(1, int(future_bars)))
    post = ctx.candles.iloc[start:end]
    if post.empty:
        return ReversalCandidateItem(ctx.symbol, original, opposite, str(trade.exit_reason), int(trade.exit_index), None, 0.0, 0.0, False, NEED_MORE_DATA)
    max_high = max(safe_float(row.get("high")) for _, row in post.iterrows())
    min_low = min(safe_float(row.get("low")) for _, row in post.iterrows())
    reversal_mfe, reversal_mae = favorable_adverse_from_price(opposite, safe_float(trade.exit_price), max_high, min_low)
    confirmation = _confirmation_delay(ctx, opposite, start, end)
    false_positive = reversal_mae > max(0.35, reversal_mfe * 1.2)
    decision = _decision(reversal_mfe, reversal_mae, confirmation, false_positive)
    return ReversalCandidateItem(
        symbol=ctx.symbol,
        original_side=original,
        opposite_side=opposite,
        exit_reason=str(trade.exit_reason),
        exit_index=int(trade.exit_index),
        confirmation_bars=confirmation,
        reversal_mfe_pct=reversal_mfe,
        reversal_mae_pct=reversal_mae,
        false_positive=false_positive,
        decision=decision,
    )


def _confirmation_delay(ctx: ReplayTradeContext, side: str, start: int, end: int) -> int | None:
    closes = [safe_float(ctx.candles.iloc[index].get("close")) for index in range(start, end)]
    for idx in range(2, len(closes)):
        first, second, third = closes[idx - 2], closes[idx - 1], closes[idx]
        if min(first, second, third) <= 0:
            continue
        if side == "LONG" and first < second < third:
            return idx + 1
        if side == "SHORT" and first > second > third:
            return idx + 1
    return None


def _decision(mfe: float, mae: float, confirmation: int | None, false_positive: bool) -> str:
    if confirmation is None:
        return NEED_MORE_DATA
    if false_positive:
        return REJECT
    if mfe > 0.60 and mae < 0.45:
        return RESEARCH_PROMISING_NOT_ACTIONABLE
    if mfe > 0.30:
        return WATCH_ONLY
    return REJECT


def run_reversal_candidate_lab(
    config: Any,
    db: Any,
    *,
    hours: int = 72,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
) -> ReversalCandidateReport:
    bundle = load_replay_trade_contexts(config, db, hours=hours, timeframe=timeframe, symbols=symbols)
    candidates = [
        analyse_reversal_candidate(ctx)
        for ctx in bundle.contexts
        if str(ctx.trade.exit_reason) in {"STOP_LOSS", "HORIZON_CLOSE"}
    ]
    by_decision = Counter(item.decision for item in candidates)
    by_symbol: dict[str, Counter[str]] = defaultdict(Counter)
    for item in candidates:
        by_symbol[item.symbol][item.decision] += 1
    return ReversalCandidateReport(
        hours=bundle.hours,
        timeframe=bundle.timeframe,
        symbols=bundle.symbols,
        total_trades=len(bundle.contexts),
        reversal_opportunities=len(candidates),
        items=candidates,
        by_decision=dict(by_decision),
        by_symbol={symbol: dict(counts) for symbol, counts in by_symbol.items()},
        loader_statuses=bundle.loader_statuses,
        warnings=bundle.warnings,
    )


def _avg(values: list[int | float]) -> float:
    return sum(float(value) for value in values) / max(len(values), 1)


def render_reversal_candidate_lab_text(report: ReversalCandidateReport) -> str:
    data = report.as_dict()
    lines = [
        "REVERSAL CANDIDATE LAB START",
        f"hours: {report.hours}",
        f"timeframe: {report.timeframe}",
        f"symbols: {','.join(report.symbols)}",
        f"total_trades: {report.total_trades}",
        f"reversal_opportunities: {report.reversal_opportunities}",
        f"false_reversal_traps: {data['false_reversal_traps']}",
        f"avg_confirmation_bars: {data['avg_confirmation_bars']:.2f}",
        f"by_decision: {data['by_decision']}",
        f"by_symbol: {data['by_symbol']}",
        "auto_flip_enabled: false",
        "activation: disabled",
        "research_only: true",
        "no_lookahead_status: OK_PREFIX_ONLY",
        "final_recommendation: NO LIVE",
        "REVERSAL CANDIDATE LAB END",
    ]
    return "\n".join(lines)


def reversal_candidate_lab_text(config: Any, db: Any, *, hours: int = 72, timeframe: str = "5m", symbols: str | list[str] | None = None) -> str:
    return render_reversal_candidate_lab_text(run_reversal_candidate_lab(config, db, hours=hours, timeframe=timeframe, symbols=symbols))
