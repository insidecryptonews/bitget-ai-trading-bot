from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any

from .phase8_research_utils import (
    FINAL_RECOMMENDATION,
    NO_LOOKAHEAD_STATUS,
    STOP_TP_SAME_BAR_RULE,
    ReplayTradeContext,
    favorable_adverse_from_price,
    load_replay_trade_contexts,
    prior_side_move_pct,
    same_bar_stop_before_tp,
)
from .utils import safe_float


PREMATURE_TIME_EXIT_PROFIT_MISSED = "PREMATURE_TIME_EXIT_PROFIT_MISSED"
CORRECT_TIME_EXIT_AVOIDED_LOSS = "CORRECT_TIME_EXIT_AVOIDED_LOSS"
CORRECT_TIME_EXIT_NO_EDGE = "CORRECT_TIME_EXIT_NO_EDGE"
SHOULD_HAVE_EXITED_EARLIER = "SHOULD_HAVE_EXITED_EARLIER"
DIRECTION_STILL_VALID_HOLD_CANDIDATE = "DIRECTION_STILL_VALID_HOLD_CANDIDATE"
DIRECTION_INVALIDATED_EXIT_OK = "DIRECTION_INVALIDATED_EXIT_OK"
LATE_ENTRY_EXHAUSTION = "LATE_ENTRY_EXHAUSTION"
REVERSAL_RISK_AFTER_MOVE = "REVERSAL_RISK_AFTER_MOVE"
NEED_MORE_DATA = "NEED_MORE_DATA"


@dataclass
class TimeExitAutopsyItem:
    symbol: str
    side: str
    exit_reason: str
    entry_index: int
    exit_index: int
    classification: str
    missed_profit_pct: float
    avoided_loss_pct: float
    late_exit_risk_pct: float
    post_exit_mfe_pct: float
    post_exit_mae_pct: float
    would_tp_if_held: bool
    would_sl_if_held: bool
    bars_until_tp: int | None
    bars_until_sl: int | None
    best_counterfactual_exit_pct: float
    no_lookahead_status: str = NO_LOOKAHEAD_STATUS
    counterfactual_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TimeExitAutopsyReport:
    hours: int
    timeframe: str
    symbols: list[str]
    total_trades: int
    time_horizon_trades: int
    items: list[TimeExitAutopsyItem] = field(default_factory=list)
    by_symbol: dict[str, dict[str, int]] = field(default_factory=dict)
    by_side: dict[str, dict[str, int]] = field(default_factory=dict)
    by_regime: dict[str, dict[str, int]] = field(default_factory=dict)
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
            "total_trades": self.total_trades,
            "time_horizon_trades": self.time_horizon_trades,
            "premature_time_exit_count": self.count(PREMATURE_TIME_EXIT_PROFIT_MISSED),
            "premature_time_exit_pct": self.count(PREMATURE_TIME_EXIT_PROFIT_MISSED) / max(self.time_horizon_trades, 1),
            "missed_profit_average": self._average("missed_profit_pct"),
            "missed_profit_median": self._median("missed_profit_pct"),
            "avoided_loss_count": self.count(CORRECT_TIME_EXIT_AVOIDED_LOSS),
            "direction_still_valid_count": self.count(DIRECTION_STILL_VALID_HOLD_CANDIDATE),
            "direction_invalidated_count": self.count(DIRECTION_INVALIDATED_EXIT_OK),
            "late_entry_exhaustion_count": self.count(LATE_ENTRY_EXHAUSTION),
            "reversal_risk_count": self.count(REVERSAL_RISK_AFTER_MOVE),
            "items": [item.as_dict() for item in self.items],
            "by_symbol": self.by_symbol,
            "by_side": self.by_side,
            "by_regime": self.by_regime,
            "loader_statuses": self.loader_statuses,
            "warnings": self.warnings,
            "final_recommendation": self.final_recommendation,
            "research_only": self.research_only,
            "no_lookahead_status": self.no_lookahead_status,
            "stop_tp_same_bar_rule": self.stop_tp_same_bar_rule,
            "counterfactual_only": True,
        }

    def count(self, label: str) -> int:
        return sum(1 for item in self.items if item.classification == label)

    def _average(self, field_name: str) -> float:
        values = [safe_float(getattr(item, field_name)) for item in self.items if item.exit_reason == "HORIZON_CLOSE"]
        return sum(values) / max(len(values), 1)

    def _median(self, field_name: str) -> float:
        values = [safe_float(getattr(item, field_name)) for item in self.items if item.exit_reason == "HORIZON_CLOSE"]
        return float(median(values)) if values else 0.0


def analyse_time_exit_trade(ctx: ReplayTradeContext, *, future_bars: int = 30) -> TimeExitAutopsyItem:
    trade = ctx.trade
    side = str(trade.side).upper()
    post_start = int(trade.exit_index) + 1
    post_end = min(len(ctx.candles), post_start + max(1, int(future_bars)))
    post = ctx.candles.iloc[post_start:post_end]
    if post.empty:
        return TimeExitAutopsyItem(
            symbol=ctx.symbol,
            side=side,
            exit_reason=str(trade.exit_reason),
            entry_index=int(trade.entry_index),
            exit_index=int(trade.exit_index),
            classification=NEED_MORE_DATA,
            missed_profit_pct=0.0,
            avoided_loss_pct=0.0,
            late_exit_risk_pct=0.0,
            post_exit_mfe_pct=0.0,
            post_exit_mae_pct=0.0,
            would_tp_if_held=False,
            would_sl_if_held=False,
            bars_until_tp=None,
            bars_until_sl=None,
            best_counterfactual_exit_pct=safe_float(trade.net_return_pct),
        )

    max_high = max(safe_float(row.get("high")) for _, row in post.iterrows())
    min_low = min(safe_float(row.get("low")) for _, row in post.iterrows())
    post_mfe, post_mae = favorable_adverse_from_price(side, safe_float(trade.exit_price), max_high, min_low)
    bars_until_tp: int | None = None
    bars_until_sl: int | None = None
    same_bar_counterfactual = False
    for offset, (_, row) in enumerate(post.iterrows(), start=1):
        stop_hit, tp_hit, same_bar = same_bar_stop_before_tp(
            side,
            safe_float(row.get("high")),
            safe_float(row.get("low")),
            safe_float(trade.stop_loss),
            safe_float(trade.take_profit_1),
        )
        if same_bar:
            same_bar_counterfactual = True
        if stop_hit and bars_until_sl is None:
            bars_until_sl = offset
        if tp_hit and bars_until_tp is None and not (stop_hit and same_bar):
            bars_until_tp = offset
        if bars_until_sl is not None and bars_until_tp is not None:
            break

    missed_profit = post_mfe if str(trade.exit_reason) == "HORIZON_CLOSE" else 0.0
    avoided_loss = post_mae if str(trade.exit_reason) == "HORIZON_CLOSE" and (bars_until_sl is not None or post_mae > post_mfe) else 0.0
    prior_move = prior_side_move_pct(ctx.candles, int(trade.entry_index), side, 10)
    best_counterfactual = safe_float(trade.net_return_pct) + max(0.0, post_mfe - 0.15)
    classification = _classify_time_exit(
        trade_exit_reason=str(trade.exit_reason),
        missed_profit=missed_profit,
        avoided_loss=avoided_loss,
        post_mfe=post_mfe,
        post_mae=post_mae,
        would_tp=bars_until_tp is not None,
        would_sl=bars_until_sl is not None,
        prior_move=prior_move,
        net_return=safe_float(trade.net_return_pct),
        same_bar_counterfactual=same_bar_counterfactual,
    )
    return TimeExitAutopsyItem(
        symbol=ctx.symbol,
        side=side,
        exit_reason=str(trade.exit_reason),
        entry_index=int(trade.entry_index),
        exit_index=int(trade.exit_index),
        classification=classification,
        missed_profit_pct=missed_profit,
        avoided_loss_pct=avoided_loss,
        late_exit_risk_pct=max(0.0, post_mae - post_mfe),
        post_exit_mfe_pct=post_mfe,
        post_exit_mae_pct=post_mae,
        would_tp_if_held=bars_until_tp is not None,
        would_sl_if_held=bars_until_sl is not None,
        bars_until_tp=bars_until_tp,
        bars_until_sl=bars_until_sl,
        best_counterfactual_exit_pct=best_counterfactual,
    )


def _classify_time_exit(
    *,
    trade_exit_reason: str,
    missed_profit: float,
    avoided_loss: float,
    post_mfe: float,
    post_mae: float,
    would_tp: bool,
    would_sl: bool,
    prior_move: float,
    net_return: float,
    same_bar_counterfactual: bool,
) -> str:
    if trade_exit_reason != "HORIZON_CLOSE":
        if prior_move > 1.2 and net_return < 0:
            return LATE_ENTRY_EXHAUSTION
        return NEED_MORE_DATA
    if same_bar_counterfactual or (would_sl and (post_mae >= post_mfe or not would_tp)):
        return CORRECT_TIME_EXIT_AVOIDED_LOSS
    if would_tp and missed_profit >= 0.20:
        return PREMATURE_TIME_EXIT_PROFIT_MISSED
    if prior_move > 1.5 and post_mae > post_mfe:
        return REVERSAL_RISK_AFTER_MOVE
    if post_mae >= max(0.25, post_mfe * 1.35):
        return DIRECTION_INVALIDATED_EXIT_OK
    if post_mfe >= max(0.20, post_mae * 1.35):
        return DIRECTION_STILL_VALID_HOLD_CANDIDATE
    if net_return < -0.25 and post_mae > post_mfe:
        return SHOULD_HAVE_EXITED_EARLIER
    return CORRECT_TIME_EXIT_NO_EDGE


def run_time_exit_autopsy_v2(
    config: Any,
    db: Any,
    *,
    hours: int = 72,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
    future_bars: int = 30,
) -> TimeExitAutopsyReport:
    bundle = load_replay_trade_contexts(config, db, hours=hours, timeframe=timeframe, symbols=symbols)
    items = [analyse_time_exit_trade(ctx, future_bars=future_bars) for ctx in bundle.contexts]
    by_symbol = _breakdown(items, "symbol")
    by_side = _breakdown(items, "side")
    by_regime = {"UNKNOWN": dict(Counter(item.classification for item in items))}
    return TimeExitAutopsyReport(
        hours=bundle.hours,
        timeframe=bundle.timeframe,
        symbols=bundle.symbols,
        total_trades=len(bundle.contexts),
        time_horizon_trades=sum(1 for item in items if item.exit_reason == "HORIZON_CLOSE"),
        items=items,
        by_symbol=by_symbol,
        by_side=by_side,
        by_regime=by_regime,
        loader_statuses=bundle.loader_statuses,
        warnings=bundle.warnings,
    )


def _breakdown(items: list[TimeExitAutopsyItem], field_name: str) -> dict[str, dict[str, int]]:
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    for item in items:
        grouped[str(getattr(item, field_name))][item.classification] += 1
    return {key: dict(value) for key, value in grouped.items()}


def render_time_exit_autopsy_v2_text(report: TimeExitAutopsyReport) -> str:
    data = report.as_dict()
    lines = [
        "TIME EXIT AUTOPSY V2 START",
        f"hours: {report.hours}",
        f"timeframe: {report.timeframe}",
        f"symbols: {','.join(report.symbols)}",
        f"total_trades: {report.total_trades}",
        f"time_horizon_trades: {report.time_horizon_trades}",
        f"premature_time_exit_count: {data['premature_time_exit_count']}",
        f"premature_time_exit_pct: {data['premature_time_exit_pct']:.4f}",
        f"missed_profit_average: {data['missed_profit_average']:.6f}",
        f"missed_profit_median: {data['missed_profit_median']:.6f}",
        f"avoided_loss_count: {data['avoided_loss_count']}",
        f"direction_still_valid_count: {data['direction_still_valid_count']}",
        f"direction_invalidated_count: {data['direction_invalidated_count']}",
        f"late_entry_exhaustion_count: {data['late_entry_exhaustion_count']}",
        f"reversal_risk_count: {data['reversal_risk_count']}",
        f"by_symbol: {data['by_symbol']}",
        f"by_side: {data['by_side']}",
        f"by_regime: {data['by_regime']}",
        f"loader_statuses: {data['loader_statuses']}",
        f"warnings: {', '.join(report.warnings) if report.warnings else 'none'}",
        "counterfactual_only: true",
        "no_lookahead_status: OK_PREFIX_ONLY",
        "research_only: true",
        "final_recommendation: NO LIVE",
        "TIME EXIT AUTOPSY V2 END",
    ]
    return "\n".join(lines)


def time_exit_autopsy_v2_text(config: Any, db: Any, *, hours: int = 72, timeframe: str = "5m", symbols: str | list[str] | None = None) -> str:
    return render_time_exit_autopsy_v2_text(
        run_time_exit_autopsy_v2(config, db, hours=hours, timeframe=timeframe, symbols=symbols)
    )
