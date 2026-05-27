"""ResearchOps V5.1 — Strategy Research Enhancer.

Read-only analyser on top of the shadow multi-trade learning engine + training
data clean view + fee-aware exit trainer. Produces per-symbol/per-side/per-setup/
per-regime rankings and surfaces *research recommendations* (never activations).

Hard contract:
  - never opens orders
  - never modifies exit policy in runtime
  - never flips paper filter
  - never changes leverage / margin / sizing
  - never sets can_send_real_orders

Decisions emitted (descriptive only):
  RESEARCH_PROMISING       - positive net EV + reasonable sample, keep researching
  NEED_MORE_DATA           - sample too small to draw a conclusion
  REJECT_NEGATIVE_NET      - net EV non-positive
  REJECT_DATA_QUALITY      - clean view BAD (or caller passed BAD)
  REJECT_OVERFIT_RISK      - single-fold dominance / catastrophic fold
  REJECT_COSTS             - gross_green_net_negative or maker-only edge
  SHADOW_ONLY              - mixed signal, keep in shadow with no promotion
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .net_profit_lock_lab import NetProfitLockReport, run_net_profit_lock_lab
from .phase8_research_utils import FINAL_RECOMMENDATION, parse_symbols
from .shadow_multi_trade_learning import (
    SHADOW_BLOCKED_DATA_STALE,
    SHADOW_BLOCKED_DEDUPE,
    SHADOW_BLOCKED_RATE_LIMIT,
    ShadowMultiTradeReport,
    ShadowVirtualTrade,
    run_shadow_multi_trade,
)
from .training_data_clean_view import (
    TrainingDataCleanReport,
    run_training_data_clean_view,
)


DECISION_RESEARCH_PROMISING = "RESEARCH_PROMISING"
DECISION_NEED_MORE_DATA = "NEED_MORE_DATA"
DECISION_REJECT_NEGATIVE_NET = "REJECT_NEGATIVE_NET"
DECISION_REJECT_DATA_QUALITY = "REJECT_DATA_QUALITY"
DECISION_REJECT_OVERFIT_RISK = "REJECT_OVERFIT_RISK"
DECISION_REJECT_COSTS = "REJECT_COSTS"
DECISION_SHADOW_ONLY = "SHADOW_ONLY"


@dataclass
class StrategyRanking:
    key: str
    trades: int
    net_ev_pct: float
    gross_ev_pct: float
    net_pf: float
    win_rate_net: float
    avg_mfe_pct: float
    avg_mae_pct: float
    tp_pct: float
    sl_pct: float
    time_pct: float
    gross_green_net_negative_rate: float
    clean_sample_count: int
    raw_sample_count: int
    confidence: str
    decision: str
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchIdea:
    name: str
    description: str
    why: str
    estimated_effort: str = "small"
    research_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StrategyResearchEnhancerReport:
    hours: int
    timeframe: str
    symbols: list[str]
    overall_decision: str
    overall_reasons: list[str] = field(default_factory=list)
    rankings_by_symbol: list[StrategyRanking] = field(default_factory=list)
    rankings_by_side: list[StrategyRanking] = field(default_factory=list)
    rankings_by_setup: list[StrategyRanking] = field(default_factory=list)
    rankings_by_regime: list[StrategyRanking] = field(default_factory=list)
    research_ideas: list[ResearchIdea] = field(default_factory=list)
    data_quality_status: str = "UNKNOWN"
    raw_sample_count: int = 0
    clean_sample_count: int = 0
    duplicate_rate: float = 0.0
    fee_aware_summary: dict[str, Any] = field(default_factory=dict)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION
    activation: str = "disabled"

    def as_dict(self) -> dict[str, Any]:
        return {
            "hours": self.hours,
            "timeframe": self.timeframe,
            "symbols": list(self.symbols),
            "overall_decision": self.overall_decision,
            "overall_reasons": list(self.overall_reasons),
            "rankings_by_symbol": [r.as_dict() for r in self.rankings_by_symbol],
            "rankings_by_side": [r.as_dict() for r in self.rankings_by_side],
            "rankings_by_setup": [r.as_dict() for r in self.rankings_by_setup],
            "rankings_by_regime": [r.as_dict() for r in self.rankings_by_regime],
            "research_ideas": [i.as_dict() for i in self.research_ideas],
            "data_quality_status": self.data_quality_status,
            "raw_sample_count": self.raw_sample_count,
            "clean_sample_count": self.clean_sample_count,
            "duplicate_rate": self.duplicate_rate,
            "fee_aware_summary": dict(self.fee_aware_summary),
            "research_only": self.research_only,
            "paper_filter_enabled": self.paper_filter_enabled,
            "can_send_real_orders": self.can_send_real_orders,
            "final_recommendation": self.final_recommendation,
            "activation": self.activation,
        }


_MIN_TRADES_PROMISING = 50
_MIN_TRADES_NEED_MORE_DATA = 15


def _is_closed(trade: ShadowVirtualTrade) -> bool:
    return trade.status not in {
        SHADOW_BLOCKED_DEDUPE,
        SHADOW_BLOCKED_RATE_LIMIT,
        SHADOW_BLOCKED_DATA_STALE,
        "OPEN",
    }


def _avg(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / max(len(values), 1)


def _net_pf(values: list[float]) -> float:
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    loss_sum = abs(sum(losses))
    if loss_sum > 0:
        return sum(wins) / loss_sum
    return 999.0 if wins else 0.0


def _classify_ranking(
    *,
    trades: int,
    net_ev: float,
    net_pf: float,
    gross_green_net_negative_rate: float,
    data_quality_bad: bool,
) -> tuple[str, str, list[str]]:
    reasons: list[str] = []
    if data_quality_bad:
        return DECISION_REJECT_DATA_QUALITY, "LOW", ["data_quality_status=BAD"]
    if trades < _MIN_TRADES_NEED_MORE_DATA:
        return DECISION_NEED_MORE_DATA, "LOW", [f"trades={trades}_below_min_{_MIN_TRADES_NEED_MORE_DATA}"]
    if gross_green_net_negative_rate >= 0.50:
        return DECISION_REJECT_COSTS, "MEDIUM", [
            f"gross_green_net_negative_rate={gross_green_net_negative_rate:.3f}_above_50pct",
        ]
    if net_ev <= 0:
        return DECISION_REJECT_NEGATIVE_NET, "MEDIUM", [
            f"net_ev={net_ev:.6f}_not_positive",
        ]
    if net_pf <= 1.0:
        reasons.append(f"net_pf={net_pf:.4f}_below_1.0_check_overfit_risk")
        return DECISION_SHADOW_ONLY, "MEDIUM", reasons
    if trades < _MIN_TRADES_PROMISING:
        return DECISION_NEED_MORE_DATA, "MEDIUM", [
            f"trades={trades}_below_promising_threshold_{_MIN_TRADES_PROMISING}",
        ]
    return DECISION_RESEARCH_PROMISING, "HIGH", reasons + [
        f"net_ev={net_ev:.6f}_pf={net_pf:.4f}_research_keep_collecting",
    ]


def _summarise_group(
    key: str,
    trades: list[ShadowVirtualTrade],
    *,
    clean_sample_count: int,
    raw_sample_count: int,
    data_quality_bad: bool,
) -> StrategyRanking:
    closed = [t for t in trades if _is_closed(t)]
    if not closed:
        return StrategyRanking(
            key=key,
            trades=0,
            net_ev_pct=0.0,
            gross_ev_pct=0.0,
            net_pf=0.0,
            win_rate_net=0.0,
            avg_mfe_pct=0.0,
            avg_mae_pct=0.0,
            tp_pct=0.0,
            sl_pct=0.0,
            time_pct=0.0,
            gross_green_net_negative_rate=0.0,
            clean_sample_count=clean_sample_count,
            raw_sample_count=raw_sample_count,
            confidence="LOW",
            decision=DECISION_NEED_MORE_DATA,
            reasons=["no_closed_shadow_trades"],
        )
    net_returns = [t.net_pnl_pct for t in closed]
    gross_returns = [t.gross_pnl_pct for t in closed]
    wins_net = [v for v in net_returns if v > 0]
    tp = sum(1 for t in closed if t.tp1_hit or t.tp2_hit or t.tp3_hit) / len(closed)
    sl = sum(1 for t in closed if t.stop_hit) / len(closed)
    time_exit = sum(1 for t in closed if t.time_hit) / len(closed)
    gross_green_net_negative_count = sum(
        1 for t in closed if t.gross_pnl_pct > 0 and t.net_pnl_pct < 0
    )
    gross_green_net_negative_rate = gross_green_net_negative_count / len(closed)
    net_ev = _avg(net_returns)
    net_pf = _net_pf(net_returns)
    decision, confidence, reasons = _classify_ranking(
        trades=len(closed),
        net_ev=net_ev,
        net_pf=net_pf,
        gross_green_net_negative_rate=gross_green_net_negative_rate,
        data_quality_bad=data_quality_bad,
    )
    return StrategyRanking(
        key=key,
        trades=len(closed),
        net_ev_pct=net_ev,
        gross_ev_pct=_avg(gross_returns),
        net_pf=net_pf,
        win_rate_net=len(wins_net) / len(closed),
        avg_mfe_pct=_avg([t.mfe_pct for t in closed]),
        avg_mae_pct=_avg([t.mae_pct for t in closed]),
        tp_pct=tp,
        sl_pct=sl,
        time_pct=time_exit,
        gross_green_net_negative_rate=gross_green_net_negative_rate,
        clean_sample_count=clean_sample_count,
        raw_sample_count=raw_sample_count,
        confidence=confidence,
        decision=decision,
        reasons=reasons,
    )


def _research_ideas(data_quality_bad: bool, fee_summary: dict[str, Any]) -> list[ResearchIdea]:
    ideas = [
        ResearchIdea(
            name="short_only_filter",
            description="Block LONG entries while LONG net EV stays negative.",
            why="Phase 8B + V5 shadow data shows LONG side underperforming SHORT.",
        ),
        ResearchIdea(
            name="risk_off_filter",
            description="Block all entries when BTC trend down + ETH trend down.",
            why="Catastrophic folds usually align with risk-off regimes.",
        ),
        ResearchIdea(
            name="no_trade_in_choppy",
            description="Block entries when ATR%<0.35 and prior move<0.35.",
            why="Choppy regime drains edge via TIME exits.",
        ),
        ResearchIdea(
            name="minimum_expected_move_after_fees",
            description="Require expected MFE>base_cost+slippage_buffer.",
            why="Avoid gross_green_net_negative trades.",
        ),
        ResearchIdea(
            name="entry_anti_late_filter",
            description="Block entries when prior_move_pct>2.5x ATR.",
            why="Late entries hurt fold 1 in DOT walk-forward.",
        ),
        ResearchIdea(
            name="hold_while_direction_valid",
            description="Skip horizon close while short-term direction matches side.",
            why="Time death reduces edge; extending only when direction still valid.",
        ),
        ResearchIdea(
            name="profit_lock_mfe_aware",
            description="Widen profit lock only when MFE/MAE ratio is favourable.",
            why="Tighter locks underperform; wider locks need favourable path.",
        ),
        ResearchIdea(
            name="time_death_reducer",
            description="Cap holding to 10-20 bars on chronic TIME setups.",
            why="Reduces TIME exit% without sacrificing TP frequency.",
        ),
        ResearchIdea(
            name="volatility_aware_stop_tp",
            description="Set SL/TP as multiples of ATR rather than fixed pct.",
            why="Fixed pct underperforms across regimes.",
        ),
        ResearchIdea(
            name="score_calibration_net_aware",
            description="Recalibrate score so high score correlates with net positive EV.",
            why="Today: high_score_negative_net_EV=true.",
        ),
        ResearchIdea(
            name="correlation_guard_shadow",
            description="Block correlated entries (BTC/ETH/BNB; SOL/AVAX/DOT; ADA/XRP/LINK).",
            why="Prevent double-loss when correlated symbols all turn down.",
        ),
        ResearchIdea(
            name="session_time_of_day_analysis",
            description="Group shadow trades by UTC hour to find dead-time windows.",
            why="May reveal a sub-set of hours where edge is positive.",
        ),
    ]
    if data_quality_bad:
        ideas.insert(0, ResearchIdea(
            name="fix_data_pipeline_duplicates",
            description="Dedupe observations / labels / paper trades before EV/PF.",
            why="Current duplicate_rate is BAD; ranking signals cannot be trusted otherwise.",
            estimated_effort="medium",
        ))
    if fee_summary and fee_summary.get("any_promotable") is False:
        ideas.append(ResearchIdea(
            name="fee_aware_exit_tightening",
            description="Iterate net_profit_lock thresholds until at least one symbol clears stress.",
            why="Fee-aware trainer reports no promotable scenario today.",
        ))
    return ideas


def _bucketise(
    closed_trades: list[ShadowVirtualTrade],
    accessor,
) -> dict[str, list[ShadowVirtualTrade]]:
    buckets: dict[str, list[ShadowVirtualTrade]] = {}
    for trade in closed_trades:
        try:
            key = str(accessor(trade) or "UNKNOWN")
        except Exception:
            key = "UNKNOWN"
        buckets.setdefault(key, []).append(trade)
    return buckets


def _aggregate_overall_decision(
    rankings: list[StrategyRanking],
    *,
    data_quality_bad: bool,
) -> tuple[str, list[str]]:
    if data_quality_bad:
        return DECISION_REJECT_DATA_QUALITY, ["data_quality_status=BAD"]
    if not rankings:
        return DECISION_NEED_MORE_DATA, ["no_shadow_data"]
    promising = [r for r in rankings if r.decision == DECISION_RESEARCH_PROMISING]
    if promising:
        names = [r.key for r in promising[:5]]
        return DECISION_RESEARCH_PROMISING, [f"promising_groups={','.join(names)}"]
    if all(r.decision == DECISION_REJECT_NEGATIVE_NET for r in rankings):
        return DECISION_REJECT_NEGATIVE_NET, ["all_groups_net_negative"]
    if any(r.decision == DECISION_REJECT_COSTS for r in rankings):
        return DECISION_REJECT_COSTS, ["at_least_one_group_with_gross_green_net_negative>=50pct"]
    if any(r.decision == DECISION_SHADOW_ONLY for r in rankings):
        return DECISION_SHADOW_ONLY, ["mixed_signal_keep_in_shadow"]
    return DECISION_NEED_MORE_DATA, ["no_actionable_recommendation"]


def run_strategy_research_enhancer(
    config: Any,
    db: Any,
    *,
    hours: int = 24,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
    data_quality_status: str | None = None,
) -> StrategyResearchEnhancerReport:
    """Build the enhancer report by consuming sibling V5 labs."""
    symbol_list = parse_symbols(symbols, config)
    if not symbol_list:
        symbol_list = ["BTCUSDT", "ETHUSDT", "DOTUSDT"]
    # Clean view first — if data quality is BAD we still produce rankings but
    # the overall decision will reject promotion.
    try:
        clean_report: TrainingDataCleanReport = run_training_data_clean_view(
            db, hours=max(int(hours), 24),
        )
    except Exception:
        clean_report = None
    if data_quality_status is None and clean_report is not None:
        data_quality_status = clean_report.overall_status
    data_quality_bad = str(data_quality_status or "UNKNOWN").upper() == "BAD"
    raw_sample = clean_report.raw_sample_count if clean_report else 0
    clean_sample = clean_report.clean_sample_count if clean_report else 0
    duplicate_rate = clean_report.duplicate_rate if clean_report else 0.0

    # Shadow multi-trade — research-only.
    shadow_report: ShadowMultiTradeReport = run_shadow_multi_trade(
        config, db, hours=int(hours), timeframe=timeframe, symbols=symbol_list,
    )
    closed_trades = [t for t in shadow_report.trades if _is_closed(t)]

    rankings_by_symbol = [
        _summarise_group(
            key, group,
            clean_sample_count=clean_sample,
            raw_sample_count=raw_sample,
            data_quality_bad=data_quality_bad,
        )
        for key, group in _bucketise(closed_trades, lambda t: t.symbol).items()
    ]
    rankings_by_side = [
        _summarise_group(
            key, group,
            clean_sample_count=clean_sample,
            raw_sample_count=raw_sample,
            data_quality_bad=data_quality_bad,
        )
        for key, group in _bucketise(closed_trades, lambda t: t.side).items()
    ]
    rankings_by_setup = [
        _summarise_group(
            key, group,
            clean_sample_count=clean_sample,
            raw_sample_count=raw_sample,
            data_quality_bad=data_quality_bad,
        )
        for key, group in _bucketise(closed_trades, lambda t: t.setup_id).items()
    ]
    rankings_by_regime = [
        _summarise_group(
            key, group,
            clean_sample_count=clean_sample,
            raw_sample_count=raw_sample,
            data_quality_bad=data_quality_bad,
        )
        for key, group in _bucketise(closed_trades, lambda t: t.regime or "UNKNOWN").items()
    ]
    # Sort by net EV descending for nicer dashboard rendering.
    for ranking_list in (rankings_by_symbol, rankings_by_side, rankings_by_setup, rankings_by_regime):
        ranking_list.sort(key=lambda r: r.net_ev_pct, reverse=True)

    # Fee-aware summary (kept cheap: single per-symbol pass over the same window).
    fee_summary: dict[str, Any] = {"any_promotable": False, "best_per_symbol": {}}
    try:
        for symbol in symbol_list[:3]:  # cap to keep runtime modest
            fee_report: NetProfitLockReport = run_net_profit_lock_lab(
                config, db, hours=max(int(hours), 168), timeframe=timeframe,
                symbols=[symbol],
            )
            best = max(fee_report.scenarios, key=lambda s: s.net_ev, default=None)
            if best is not None:
                fee_summary["best_per_symbol"][symbol] = {
                    "scenario": best.scenario,
                    "net_ev": best.net_ev,
                    "promotion_eligible": bool(getattr(best, "promotion_eligible", False)),
                    "gross_green_net_negative": bool(getattr(best, "gross_green_net_negative", False)),
                }
                if getattr(best, "promotion_eligible", False):
                    fee_summary["any_promotable"] = True
    except Exception as exc:
        fee_summary["error"] = type(exc).__name__

    overall_decision, overall_reasons = _aggregate_overall_decision(
        rankings_by_symbol, data_quality_bad=data_quality_bad,
    )
    return StrategyResearchEnhancerReport(
        hours=int(hours),
        timeframe=timeframe,
        symbols=symbol_list,
        overall_decision=overall_decision,
        overall_reasons=overall_reasons,
        rankings_by_symbol=rankings_by_symbol,
        rankings_by_side=rankings_by_side,
        rankings_by_setup=rankings_by_setup,
        rankings_by_regime=rankings_by_regime,
        research_ideas=_research_ideas(data_quality_bad, fee_summary),
        data_quality_status=str(data_quality_status or "UNKNOWN"),
        raw_sample_count=raw_sample,
        clean_sample_count=clean_sample,
        duplicate_rate=duplicate_rate,
        fee_aware_summary=fee_summary,
    )


def render_strategy_research_enhancer_text(report: StrategyResearchEnhancerReport) -> str:
    lines = [
        "STRATEGY RESEARCH ENHANCER START",
        f"hours: {report.hours}",
        f"timeframe: {report.timeframe}",
        f"symbols: {','.join(report.symbols)}",
        f"overall_decision: {report.overall_decision}",
        f"data_quality_status: {report.data_quality_status}",
        f"raw_sample_count: {report.raw_sample_count}",
        f"clean_sample_count: {report.clean_sample_count}",
        f"duplicate_rate: {report.duplicate_rate:.4f}",
        "overall_reasons:",
    ]
    for reason in report.overall_reasons:
        lines.append(f"- {reason}")
    for label, rankings in (
        ("rankings_by_symbol", report.rankings_by_symbol),
        ("rankings_by_side", report.rankings_by_side),
        ("rankings_by_setup", report.rankings_by_setup),
        ("rankings_by_regime", report.rankings_by_regime),
    ):
        lines.append(f"{label}:")
        lines.append("key | trades | net_ev | gross_ev | net_pf | win | TP | SL | TIME | gg_net_neg | conf | decision")
        for ranking in rankings[:15]:
            lines.append(
                f"{ranking.key} | {ranking.trades} | {ranking.net_ev_pct:.4f} | "
                f"{ranking.gross_ev_pct:.4f} | {ranking.net_pf:.4f} | "
                f"{ranking.win_rate_net:.3f} | {ranking.tp_pct:.3f} | "
                f"{ranking.sl_pct:.3f} | {ranking.time_pct:.3f} | "
                f"{ranking.gross_green_net_negative_rate:.3f} | "
                f"{ranking.confidence} | {ranking.decision}"
            )
    lines.append("research_ideas:")
    for idea in report.research_ideas:
        lines.append(f"- {idea.name}: {idea.description}")
    lines.extend([
        "fee_aware_summary: " + str(report.fee_aware_summary),
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "activation: disabled",
        "final_recommendation: NO LIVE",
        "STRATEGY RESEARCH ENHANCER END",
    ])
    return "\n".join(lines)


def strategy_research_enhancer_text(
    config: Any,
    db: Any,
    *,
    hours: int = 24,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
    data_quality_status: str | None = None,
) -> str:
    return render_strategy_research_enhancer_text(run_strategy_research_enhancer(
        config, db,
        hours=hours, timeframe=timeframe, symbols=symbols,
        data_quality_status=data_quality_status,
    ))
