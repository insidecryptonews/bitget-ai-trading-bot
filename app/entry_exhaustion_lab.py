from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from .phase8_research_utils import (
    FINAL_RECOMMENDATION,
    NO_LOOKAHEAD_STATUS,
    ReplayTradeContext,
    average_range_pct,
    favorable_adverse_from_price,
    load_replay_trade_contexts,
    prior_side_move_pct,
)
from .utils import safe_float


GOOD_MOMENTUM_CONTINUATION = "GOOD_MOMENTUM_CONTINUATION"
LATE_CHASE_ENTRY = "LATE_CHASE_ENTRY"
REVERSAL_RISK = "REVERSAL_RISK"
WAIT_FOR_CONFIRMATION = "WAIT_FOR_CONFIRMATION"
CLEAN_ENTRY = "CLEAN_ENTRY"
NEED_MORE_DATA = "NEED_MORE_DATA"


@dataclass
class EntryExhaustionItem:
    symbol: str
    side: str
    entry_index: int
    classification: str
    move_3_bars: float
    move_5_bars: float
    move_10_bars: float
    move_20_bars: float
    atr_normalized_move: float
    consecutive_same_direction: int
    wick_exhaustion_score: float
    mfe_after_entry: float
    mae_after_entry: float
    net_return_pct: float
    policy_suggestion: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EntryExhaustionReport:
    hours: int
    timeframe: str
    symbols: list[str]
    total_trades: int
    items: list[EntryExhaustionItem] = field(default_factory=list)
    by_classification: dict[str, int] = field(default_factory=dict)
    by_symbol: dict[str, dict[str, int]] = field(default_factory=dict)
    loader_statuses: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    final_recommendation: str = FINAL_RECOMMENDATION
    research_only: bool = True
    no_lookahead_status: str = NO_LOOKAHEAD_STATUS

    def as_dict(self) -> dict[str, Any]:
        return {
            "hours": self.hours,
            "timeframe": self.timeframe,
            "symbols": self.symbols,
            "total_trades": self.total_trades,
            "late_chase_count": self.by_classification.get(LATE_CHASE_ENTRY, 0),
            "reversal_risk_count": self.by_classification.get(REVERSAL_RISK, 0),
            "good_continuation_count": self.by_classification.get(GOOD_MOMENTUM_CONTINUATION, 0),
            "wait_for_confirmation_count": self.by_classification.get(WAIT_FOR_CONFIRMATION, 0),
            "items": [item.as_dict() for item in self.items],
            "by_classification": self.by_classification,
            "by_symbol": self.by_symbol,
            "loader_statuses": self.loader_statuses,
            "warnings": self.warnings,
            "final_recommendation": self.final_recommendation,
            "research_only": self.research_only,
            "no_lookahead_status": self.no_lookahead_status,
        }


def analyse_entry_exhaustion_trade(ctx: ReplayTradeContext) -> EntryExhaustionItem:
    trade = ctx.trade
    side = str(trade.side).upper()
    entry_index = int(trade.entry_index)
    moves = {bars: prior_side_move_pct(ctx.candles, entry_index, side, bars) for bars in (3, 5, 10, 20)}
    atr_like = average_range_pct(ctx.candles, entry_index, 14)
    atr_norm = moves[10] / max(atr_like, 0.01)
    consecutive = _consecutive_same_direction(ctx, bars=8)
    wick_score = _wick_exhaustion(ctx)
    segment = ctx.candles.iloc[entry_index: int(trade.exit_index) + 1]
    if segment.empty:
        mfe = mae = 0.0
    else:
        high = max(safe_float(row.get("high")) for _, row in segment.iterrows())
        low = min(safe_float(row.get("low")) for _, row in segment.iterrows())
        mfe, mae = favorable_adverse_from_price(side, safe_float(trade.entry_price), high, low)
    classification = _classify(
        move_10=moves[10],
        atr_norm=atr_norm,
        consecutive=consecutive,
        wick_score=wick_score,
        mfe=mfe,
        mae=mae,
        net=safe_float(trade.net_return_pct),
    )
    return EntryExhaustionItem(
        symbol=ctx.symbol,
        side=side,
        entry_index=entry_index,
        classification=classification,
        move_3_bars=moves[3],
        move_5_bars=moves[5],
        move_10_bars=moves[10],
        move_20_bars=moves[20],
        atr_normalized_move=atr_norm,
        consecutive_same_direction=consecutive,
        wick_exhaustion_score=wick_score,
        mfe_after_entry=mfe,
        mae_after_entry=mae,
        net_return_pct=safe_float(trade.net_return_pct),
        policy_suggestion=_policy_for(classification, side),
    )


def _classify(*, move_10: float, atr_norm: float, consecutive: int, wick_score: float, mfe: float, mae: float, net: float) -> str:
    if move_10 == 0 and mfe == 0 and mae == 0:
        return NEED_MORE_DATA
    extended = move_10 > 0.9 and atr_norm > 2.0
    exhausted = consecutive >= 4 or wick_score > 0.55
    if extended and exhausted and (mae > mfe or net < 0):
        return LATE_CHASE_ENTRY
    if extended and mae > max(0.25, mfe * 1.3):
        return REVERSAL_RISK
    if extended and net <= 0:
        return WAIT_FOR_CONFIRMATION
    if move_10 > 0.3 and net > 0 and mfe >= mae:
        return GOOD_MOMENTUM_CONTINUATION
    return CLEAN_ENTRY


def _policy_for(classification: str, side: str) -> str:
    if classification == LATE_CHASE_ENTRY:
        return "block_late_short_after_dump" if side == "SHORT" else "block_late_long_after_pump"
    if classification == REVERSAL_RISK:
        return "require_reversal_or_pullback_confirmation"
    if classification == WAIT_FOR_CONFIRMATION:
        return "require_continuation_confirmation"
    return "research_only_no_runtime_change"


def _consecutive_same_direction(ctx: ReplayTradeContext, *, bars: int) -> int:
    side = str(ctx.trade.side).upper()
    entry = int(ctx.trade.entry_index)
    start = max(1, entry - bars)
    count = 0
    for idx in range(entry - 1, start - 1, -1):
        prev_close = safe_float(ctx.candles.iloc[idx - 1].get("close"))
        close = safe_float(ctx.candles.iloc[idx].get("close"))
        if prev_close <= 0 or close <= 0:
            break
        same = close > prev_close if side == "LONG" else close < prev_close
        if not same:
            break
        count += 1
    return count


def _wick_exhaustion(ctx: ReplayTradeContext) -> float:
    idx = max(0, int(ctx.trade.entry_index) - 1)
    row = ctx.candles.iloc[idx]
    high = safe_float(row.get("high"))
    low = safe_float(row.get("low"))
    open_price = safe_float(row.get("open"))
    close = safe_float(row.get("close"))
    candle_range = max(high - low, 0.0000001)
    upper_wick = high - max(open_price, close)
    lower_wick = min(open_price, close) - low
    if str(ctx.trade.side).upper() == "LONG":
        return max(0.0, upper_wick / candle_range)
    return max(0.0, lower_wick / candle_range)


def run_entry_exhaustion_lab(
    config: Any,
    db: Any,
    *,
    hours: int = 72,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
) -> EntryExhaustionReport:
    bundle = load_replay_trade_contexts(config, db, hours=hours, timeframe=timeframe, symbols=symbols)
    items = [analyse_entry_exhaustion_trade(ctx) for ctx in bundle.contexts]
    by_class = Counter(item.classification for item in items)
    by_symbol: dict[str, Counter[str]] = defaultdict(Counter)
    for item in items:
        by_symbol[item.symbol][item.classification] += 1
    return EntryExhaustionReport(
        hours=bundle.hours,
        timeframe=bundle.timeframe,
        symbols=bundle.symbols,
        total_trades=len(items),
        items=items,
        by_classification=dict(by_class),
        by_symbol={symbol: dict(counts) for symbol, counts in by_symbol.items()},
        loader_statuses=bundle.loader_statuses,
        warnings=bundle.warnings,
    )


def render_entry_exhaustion_lab_text(report: EntryExhaustionReport) -> str:
    data = report.as_dict()
    lines = [
        "ENTRY EXHAUSTION LAB START",
        f"hours: {report.hours}",
        f"timeframe: {report.timeframe}",
        f"symbols: {','.join(report.symbols)}",
        f"total_trades: {report.total_trades}",
        f"late_chase_count: {data['late_chase_count']}",
        f"reversal_risk_count: {data['reversal_risk_count']}",
        f"good_continuation_count: {data['good_continuation_count']}",
        f"wait_for_confirmation_count: {data['wait_for_confirmation_count']}",
        f"by_classification: {data['by_classification']}",
        f"by_symbol: {data['by_symbol']}",
        "policies: block_late_short_after_dump, block_late_long_after_pump, require_pullback_confirmation, require_continuation_confirmation",
        "activation: disabled",
        "research_only: true",
        "no_lookahead_status: OK_PREFIX_ONLY",
        "final_recommendation: NO LIVE",
        "ENTRY EXHAUSTION LAB END",
    ]
    return "\n".join(lines)


def entry_exhaustion_lab_text(config: Any, db: Any, *, hours: int = 72, timeframe: str = "5m", symbols: str | list[str] | None = None) -> str:
    return render_entry_exhaustion_lab_text(run_entry_exhaustion_lab(config, db, hours=hours, timeframe=timeframe, symbols=symbols))
