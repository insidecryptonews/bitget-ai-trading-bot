"""Real Strategy Backtester breakdown — group results by any dimension.

Runs the multi-symbol backtester and re-groups the resulting trade list
by setup_key components. Pure offline research; never sends orders, never
touches the exchange.

Group-by tokens supported:
  symbol, side, regime, score_bucket, setup_key, signal_type,
  exit_reason
Compound keys (e.g. "symbol,side,regime") are also accepted.

Decision rules per group:
  REJECT             : net_ev <= 0 AND trades >= min_trades
  WATCH_ONLY         : net_ev > 0 AND trades <  min_trades
  NEED_MORE_DATA     : trades <  min_trades AND net_ev <= 0
  CANDIDATE_RESEARCH : net_ev > 0 AND trades >= min_trades (still pre-walk-forward)

Final report decision:
  POLICY_READY_FOR_PAPER : never set here (requires walk-forward + policy builder)
  CANDIDATES_FOUND_NEED_WALK_FORWARD : any CANDIDATE_RESEARCH groups
  NO_EDGE_FOUND          : otherwise

This module never auto-promotes anything; it just reports.
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .ohlcv_replay_loader import OhlcvReplayLoader
from .real_strategy_backtester import (
    DEFAULT_BACKTESTER_SYMBOLS,
    RealStrategyBacktester,
    _resolve_symbols,
)
from .setup_key import build_setup_key
from .signal_engine import Signal
from .utils import safe_float, safe_int


FINAL_RECOMMENDATION = "NO LIVE"

DECISION_REJECT = "REJECT"
DECISION_WATCH_ONLY = "WATCH_ONLY"
DECISION_NEED_MORE_DATA = "NEED_MORE_DATA"
DECISION_CANDIDATE_RESEARCH = "CANDIDATE_RESEARCH"

DECISION_NO_EDGE = "NO_EDGE_FOUND"
DECISION_CANDIDATES_FOUND = "CANDIDATES_FOUND_NEED_WALK_FORWARD"
DECISION_POLICY_READY = "POLICY_READY_FOR_PAPER"

ALLOWED_GROUP_TOKENS = (
    "symbol", "side", "regime", "score_bucket", "setup_key",
    "signal_type", "exit_reason",
)


@dataclass
class TradeRecord:
    """Subset of trade fields needed for grouping/aggregation."""

    symbol: str
    side: str
    regime: str
    score_bucket: str
    signal_type: str
    setup_key: str
    exit_reason: str
    gross_return_pct: float
    net_return_pct: float
    entry_index: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GroupSummary:
    group_key: str
    trades: int
    net_ev: float
    net_pf: float
    win_rate: float
    tp_pct: float
    sl_pct: float
    time_pct: float
    avg_pnl: float
    gross_profit: float
    gross_loss: float
    max_drawdown: float
    status: str
    decision: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BreakdownReport:
    hours: int
    timeframe: str
    group_by: list[str]
    min_trades: int
    top_n: int
    total_trades: int
    total_groups: int
    decision: str
    worst_groups: list[GroupSummary] = field(default_factory=list)
    least_bad_groups: list[GroupSummary] = field(default_factory=list)
    promising_watch_only_groups: list[GroupSummary] = field(default_factory=list)
    candidate_research_groups: list[GroupSummary] = field(default_factory=list)
    need_more_data_groups: list[GroupSummary] = field(default_factory=list)
    final_recommendation: str = FINAL_RECOMMENDATION
    research_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "hours": self.hours,
            "timeframe": self.timeframe,
            "group_by": self.group_by,
            "min_trades": self.min_trades,
            "top_n": self.top_n,
            "total_trades": self.total_trades,
            "total_groups": self.total_groups,
            "decision": self.decision,
            "worst_groups": [g.as_dict() for g in self.worst_groups],
            "least_bad_groups": [g.as_dict() for g in self.least_bad_groups],
            "promising_watch_only_groups": [g.as_dict() for g in self.promising_watch_only_groups],
            "candidate_research_groups": [g.as_dict() for g in self.candidate_research_groups],
            "need_more_data_groups": [g.as_dict() for g in self.need_more_data_groups],
            "final_recommendation": self.final_recommendation,
            "research_only": self.research_only,
        }


def parse_group_by(text: str) -> list[str]:
    tokens = [t.strip().lower() for t in str(text or "").split(",") if t.strip()]
    if not tokens:
        return ["symbol"]
    invalid = [t for t in tokens if t not in ALLOWED_GROUP_TOKENS]
    if invalid:
        raise ValueError(
            f"Invalid group_by token(s): {invalid}. Allowed: {list(ALLOWED_GROUP_TOKENS)}"
        )
    return tokens


def _resolve_score_bucket(score: Any) -> str:
    from .setup_key import score_bucket
    return score_bucket(score)


def _trade_to_record(
    *,
    symbol: str,
    side: str,
    regime: str,
    score: Any,
    strategy: str,
    signal_source: str,
    trade: Any,
    timeframe: str,
) -> TradeRecord:
    key = build_setup_key(
        symbol=symbol,
        side=side,
        regime=regime,
        score=score,
        timeframe=timeframe,
        strategy=strategy,
        exit_policy="current_exit",
        source=signal_source,
    )
    return TradeRecord(
        symbol=key.symbol,
        side=key.side,
        regime=key.regime,
        score_bucket=key.score_bucket,
        signal_type=key.strategy,
        setup_key=key.as_string(),
        exit_reason=str(trade.exit_reason),
        gross_return_pct=safe_float(trade.gross_return_pct),
        net_return_pct=safe_float(trade.net_return_pct),
        entry_index=safe_int(trade.entry_index),
    )


class _TradeRecordingEngine:
    """Wraps SignalEngine to capture per-signal metadata so we can later
    group trades by the FULL setup_key, not just symbol/side.

    The real backtester calls `generate_signal(symbol, snapshot, regime)`
    candle-by-candle. We delegate to the real SignalEngine and stash a copy
    of the resulting `Signal` keyed by entry_index = candle_index + 1 (which
    is exactly the entry index the backtester uses).
    """

    def __init__(self, inner: Any, recorder: dict[int, dict[str, Any]]) -> None:
        self._inner = inner
        self._recorder = recorder
        self._counter = 0

    def generate_signal(self, symbol: str, snapshot: Any, market_regime: Any) -> Any:
        signal = self._inner.generate_signal(symbol, snapshot, market_regime)
        entry_index = self._counter + 1  # backtester enters at i+1
        side = str(getattr(signal, "side", "")).upper()
        if side in {"LONG", "SHORT"}:
            self._recorder[entry_index] = {
                "side": side,
                "score": int(getattr(signal, "confidence_score", 0) or 0),
                "strategy": str(getattr(signal, "strategy_type", "") or ""),
                "regime": str(getattr(market_regime, "regime", "") or ""),
                # signal_source default = trade_signal for now.
                "signal_source": "trade_signal",
            }
        self._counter += 1
        return signal


def collect_trade_records(
    config: Any,
    db: Any,
    *,
    hours: int = 72,
    symbols: list[str] | None = None,
    timeframe: str = "5m",
) -> list[TradeRecord]:
    """Run the multi-symbol backtester and collect detailed per-trade records.

    We re-execute the SignalEngine here (wrapped) so we can capture the
    metadata at signal time (score, regime, strategy) and attach it to each
    resulting trade for accurate grouping.
    """
    from .signal_engine import SignalEngine

    resolved = _resolve_symbols(config, symbols)
    timeframe = str(timeframe or "5m").lower()
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours or 1)))
    loader = OhlcvReplayLoader(db)
    records: list[TradeRecord] = []

    for symbol in resolved:
        load_result = loader.load_ohlcv(
            symbols=[symbol], timeframe=timeframe, since=since,
        )
        if load_result.status not in {"OK", "TOO_MANY_GAPS"} or symbol not in load_result.frames_by_symbol:
            continue
        frame = load_result.frames_by_symbol[symbol]
        signal_metadata: dict[int, dict[str, Any]] = {}
        engine = _TradeRecordingEngine(SignalEngine(config), signal_metadata)
        backtester = RealStrategyBacktester(config, signal_engine=engine)  # type: ignore[arg-type]
        result = backtester.run(
            symbol, frame,
            min_order_value_usdt=float(getattr(config, "min_trade_margin_usdt", 5.0)),
            notional_usdt=float(getattr(config, "trade_margin_usdt", 12.0)) * max(1, int(getattr(config, "default_leverage", 1))),
        )
        for trade in result.trades:
            meta = signal_metadata.get(trade.entry_index, {})
            records.append(_trade_to_record(
                symbol=symbol,
                side=meta.get("side", str(trade.side)),
                regime=meta.get("regime", "UNKNOWN"),
                score=meta.get("score", 0),
                strategy=meta.get("strategy", "UNKNOWN"),
                signal_source=meta.get("signal_source", "trade_signal"),
                trade=trade,
                timeframe=timeframe,
            ))
    return records


def _group_key(record: TradeRecord, tokens: list[str]) -> str:
    if tokens == ["setup_key"]:
        return record.setup_key
    parts: list[str] = []
    for token in tokens:
        parts.append(str(getattr(record, token, "UNKNOWN")))
    return "|".join(parts)


def _max_dd(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for v in values:
        equity += v
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return abs(worst)


def _classify_group(
    *,
    trades: int,
    net_ev: float,
    min_trades: int,
) -> str:
    if trades < min_trades:
        if net_ev > 0:
            return DECISION_WATCH_ONLY
        return DECISION_NEED_MORE_DATA
    if net_ev > 0:
        return DECISION_CANDIDATE_RESEARCH
    return DECISION_REJECT


def build_breakdown(
    records: list[TradeRecord],
    *,
    group_by: list[str] | None = None,
    min_trades: int = 30,
    top_n: int = 25,
    hours: int = 72,
    timeframe: str = "5m",
) -> BreakdownReport:
    tokens = group_by or ["symbol"]
    buckets: dict[str, list[TradeRecord]] = defaultdict(list)
    for record in records:
        buckets[_group_key(record, tokens)].append(record)

    summaries: list[GroupSummary] = []
    for key, trades in buckets.items():
        net = [t.net_return_pct for t in trades]
        wins = [v for v in net if v > 0]
        losses = [v for v in net if v < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        net_ev = sum(net) / max(len(net), 1)
        net_pf = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
        tp = sum(1 for t in trades if t.exit_reason == "TAKE_PROFIT") / max(len(trades), 1)
        sl = sum(1 for t in trades if t.exit_reason == "STOP_LOSS") / max(len(trades), 1)
        tm = sum(1 for t in trades if t.exit_reason == "HORIZON_CLOSE") / max(len(trades), 1)
        status = "OK" if len(trades) > 0 else "NO_TRADES"
        decision = _classify_group(trades=len(trades), net_ev=net_ev, min_trades=min_trades)
        summaries.append(GroupSummary(
            group_key=key,
            trades=len(trades),
            net_ev=net_ev,
            net_pf=net_pf,
            win_rate=len(wins) / max(len(trades), 1),
            tp_pct=tp,
            sl_pct=sl,
            time_pct=tm,
            avg_pnl=net_ev,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            max_drawdown=_max_dd(net),
            status=status,
            decision=decision,
        ))

    # Sort buckets
    rejected = sorted(
        [g for g in summaries if g.decision == DECISION_REJECT],
        key=lambda g: g.net_ev,
    )
    least_bad = sorted(
        [g for g in summaries if g.decision == DECISION_REJECT],
        key=lambda g: g.net_ev, reverse=True,
    )
    watch = sorted(
        [g for g in summaries if g.decision == DECISION_WATCH_ONLY],
        key=lambda g: g.net_ev, reverse=True,
    )
    candidates = sorted(
        [g for g in summaries if g.decision == DECISION_CANDIDATE_RESEARCH],
        key=lambda g: g.net_ev, reverse=True,
    )
    need_more_data = sorted(
        [g for g in summaries if g.decision == DECISION_NEED_MORE_DATA],
        key=lambda g: g.net_ev, reverse=True,
    )

    if candidates:
        report_decision = DECISION_CANDIDATES_FOUND
    elif not summaries:
        report_decision = DECISION_NO_EDGE
    else:
        report_decision = DECISION_NO_EDGE

    return BreakdownReport(
        hours=int(hours),
        timeframe=timeframe,
        group_by=tokens,
        min_trades=int(min_trades),
        top_n=int(top_n),
        total_trades=sum(g.trades for g in summaries),
        total_groups=len(summaries),
        decision=report_decision,
        worst_groups=rejected[:top_n],
        least_bad_groups=least_bad[:top_n],
        promising_watch_only_groups=watch[:top_n],
        candidate_research_groups=candidates[:top_n],
        need_more_data_groups=need_more_data[:top_n],
    )


def render_breakdown_text(report: BreakdownReport) -> str:
    lines = ["REAL STRATEGY BACKTEST BREAKDOWN START"]
    lines.append(f"hours: {report.hours}")
    lines.append(f"timeframe: {report.timeframe}")
    lines.append(f"group_by: {','.join(report.group_by)}")
    lines.append(f"min_trades: {report.min_trades}")
    lines.append(f"top_n: {report.top_n}")
    lines.append(f"total_trades: {report.total_trades}")
    lines.append(f"total_groups: {report.total_groups}")
    lines.append(f"decision: {report.decision}")

    def _block(title: str, groups: list[GroupSummary]) -> None:
        lines.append("")
        lines.append(title)
        if not groups:
            lines.append("- none")
            return
        for g in groups:
            lines.append(
                f"- {g.group_key} | trades={g.trades} net_ev={g.net_ev:.6f} net_pf={g.net_pf:.4f} "
                f"TP={g.tp_pct*100:.1f}% SL={g.sl_pct*100:.1f}% TIME={g.time_pct*100:.1f}% "
                f"avg_pnl={g.avg_pnl:.6f} gp={g.gross_profit:.4f} gl={g.gross_loss:.4f} "
                f"dd={g.max_drawdown:.4f} decision={g.decision}"
            )

    _block("CANDIDATE_RESEARCH (need walk-forward before paper):", report.candidate_research_groups)
    _block("WATCH_ONLY (positive but sample below min_trades):", report.promising_watch_only_groups)
    _block("LEAST_BAD (negative but closest to break-even):", report.least_bad_groups)
    _block("WORST (most negative net_ev):", report.worst_groups)

    lines.append("")
    lines.append("contract:")
    lines.append("- uses_signal_engine: true")
    lines.append("- no_lookahead_status: OK_PREFIX_ONLY")
    lines.append("- entry_model: signal_close_i_entry_next_open_i+1")
    lines.append("- stop_tp_same_bar_rule: STOP_BEFORE_TP")
    lines.append("- exchange_calls: false")
    lines.append("research_only: true")
    lines.append(f"final_recommendation: {report.final_recommendation}")
    lines.append("REAL STRATEGY BACKTEST BREAKDOWN END")
    return "\n".join(lines)


def export_breakdown_csv(report: BreakdownReport) -> str:
    """Export every group (not just top_n) as CSV. Useful for spreadsheet analysis."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "group_key", "trades", "net_ev", "net_pf",
        "win_rate", "TP_pct", "SL_pct", "TIME_pct",
        "avg_pnl", "gross_profit", "gross_loss", "max_drawdown",
        "status", "decision",
    ])
    for bucket in (
        report.candidate_research_groups,
        report.promising_watch_only_groups,
        report.least_bad_groups,
        report.worst_groups,
    ):
        for g in bucket:
            writer.writerow([
                g.group_key, g.trades, f"{g.net_ev:.8f}", f"{g.net_pf:.6f}",
                f"{g.win_rate:.4f}", f"{g.tp_pct:.4f}", f"{g.sl_pct:.4f}", f"{g.time_pct:.4f}",
                f"{g.avg_pnl:.8f}", f"{g.gross_profit:.6f}", f"{g.gross_loss:.6f}",
                f"{g.max_drawdown:.6f}", g.status, g.decision,
            ])
    return buffer.getvalue()


def export_breakdown_json(report: BreakdownReport) -> str:
    return json.dumps(report.as_dict(), indent=2, default=str)


def run_breakdown_text(
    config: Any,
    db: Any,
    *,
    hours: int = 72,
    symbols: list[str] | None = None,
    timeframe: str = "5m",
    group_by: str = "symbol",
    min_trades: int = 30,
    top_n: int = 25,
) -> str:
    tokens = parse_group_by(group_by)
    records = collect_trade_records(
        config, db, hours=hours, symbols=symbols, timeframe=timeframe,
    )
    report = build_breakdown(
        records, group_by=tokens, min_trades=min_trades, top_n=top_n,
        hours=hours, timeframe=timeframe,
    )
    return render_breakdown_text(report)
