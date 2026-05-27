"""Phase 9 — DOT fold-1 diagnosis lab.

Goal: compare fold-1 (the losing fold reported by Phase 8B walk-forward) vs
folds 2..N (winning folds) for a single policy on DOTUSDT. The output is meant
to inform a regime filter, not to be promoted to paper.

Read-only:
- pulls OHLCV via OhlcvReplayLoader
- replays trades via RealStrategyBacktester
- simulates the candidate policy via dynamic_hold_lab._simulate_dynamic_trade
- never opens orders, never touches Bitget, never writes DB

Output shape is documented in `DotRegimeDiagnosisReport`. Folds are computed by
splitting the policy's trades chronologically into `folds` chunks (same logic
as Phase 8B walk-forward) so this lab and the validator agree on which trades
belong to fold 1.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from statistics import median
from typing import Any

import pandas as pd

from .dynamic_hold_lab import (
    DynamicHoldPolicy,
    _baseline_trade,
    _simulate_dynamic_trade,
    default_dynamic_hold_policies,
)
from .phase8_research_utils import (
    FINAL_RECOMMENDATION,
    NO_LOOKAHEAD_STATUS,
    STOP_TP_SAME_BAR_RULE,
    ReplayTradeContext,
    average_range_pct,
    load_replay_trade_contexts,
    parse_symbols,
    prior_side_move_pct,
)
from .utils import safe_float


@dataclass
class FoldRegimeSummary:
    fold: int
    trades: int
    start: str
    end: str
    policy_net_ev: float
    baseline_net_ev: float
    delta_ev: float
    win_rate: float
    tp_pct: float
    sl_pct: float
    time_pct: float
    avg_duration_bars: float
    avg_mfe_pct: float
    avg_mae_pct: float
    avg_atr_pct: float
    avg_prior_move_pct: float
    median_prior_move_pct: float
    long_pct: float
    short_pct: float
    late_entry_pct: float
    reversal_pct: float
    classification: str = "UNKNOWN"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DotRegimeDiagnosisReport:
    symbol: str
    timeframe: str
    hours: int
    folds: int
    policy_name: str
    trades_total: int
    fold_summaries: list[FoldRegimeSummary] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)
    candidate_filters: list[str] = field(default_factory=list)
    decision: str = "NEED_MORE_DATA"
    loader_statuses: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    research_only: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION
    no_lookahead_status: str = NO_LOOKAHEAD_STATUS
    stop_tp_same_bar_rule: str = STOP_TP_SAME_BAR_RULE

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "hours": self.hours,
            "folds": self.folds,
            "policy_name": self.policy_name,
            "trades_total": self.trades_total,
            "fold_summaries": [summary.as_dict() for summary in self.fold_summaries],
            "differences": list(self.differences),
            "candidate_filters": list(self.candidate_filters),
            "decision": self.decision,
            "loader_statuses": self.loader_statuses,
            "warnings": self.warnings,
            "research_only": self.research_only,
            "final_recommendation": self.final_recommendation,
            "no_lookahead_status": self.no_lookahead_status,
            "stop_tp_same_bar_rule": self.stop_tp_same_bar_rule,
        }


@dataclass
class _PolicySample:
    symbol: str
    side: str
    entry_index: int
    timestamp: datetime
    net_return_pct: float
    baseline_net_return_pct: float
    exit_reason: str
    duration_bars: int
    mfe_pct: float
    mae_pct: float
    atr_pct: float
    prior_move_pct: float
    candles: pd.DataFrame
    trade_entry_index: int


def _policy_by_name(policy_name: str) -> DynamicHoldPolicy:
    if policy_name == "baseline_current_exit":
        return DynamicHoldPolicy("baseline_current_exit")
    for policy in default_dynamic_hold_policies():
        if policy.name == policy_name:
            return policy
    raise ValueError(f"unknown_phase9_policy:{policy_name}")


def _entry_timestamp(ctx: ReplayTradeContext) -> datetime:
    try:
        raw = ctx.candles.iloc[int(ctx.trade.entry_index)].get("timestamp")
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _build_samples(
    contexts: list[ReplayTradeContext],
    policy: DynamicHoldPolicy,
) -> list[_PolicySample]:
    samples: list[_PolicySample] = []
    for ctx in contexts:
        try:
            baseline = _baseline_trade(ctx, "baseline_current_exit")
            if policy.name == "baseline_current_exit":
                simulated = baseline
            else:
                simulated = _simulate_dynamic_trade(ctx, policy)
            if simulated.blocked:
                continue
            atr_pct = average_range_pct(ctx.candles, int(ctx.trade.entry_index), 14)
            prior_move = prior_side_move_pct(
                ctx.candles, int(ctx.trade.entry_index), str(ctx.trade.side), 10,
            )
            samples.append(_PolicySample(
                symbol=str(ctx.symbol).upper(),
                side=str(ctx.trade.side).upper(),
                entry_index=int(ctx.trade.entry_index),
                timestamp=_entry_timestamp(ctx),
                net_return_pct=safe_float(simulated.net_return_pct),
                baseline_net_return_pct=safe_float(baseline.net_return_pct),
                exit_reason=str(simulated.exit_reason),
                duration_bars=int(simulated.duration_bars),
                mfe_pct=safe_float(simulated.mfe_pct),
                mae_pct=safe_float(simulated.mae_pct),
                atr_pct=atr_pct,
                prior_move_pct=prior_move,
                candles=ctx.candles,
                trade_entry_index=int(ctx.trade.entry_index),
            ))
        except Exception:
            continue
    samples.sort(key=lambda item: item.timestamp)
    return samples


def _classify_fold(net_ev: float, delta_ev: float) -> str:
    if net_ev < 0 and delta_ev < 0:
        return "FOLD_LOSS_AND_NO_IMPROVEMENT"
    if net_ev < 0:
        return "FOLD_LOSS_DESPITE_IMPROVEMENT"
    if net_ev > 0 and delta_ev > 0:
        return "FOLD_WIN_AND_IMPROVEMENT"
    if net_ev > 0:
        return "FOLD_WIN_BUT_DELTA_NEGATIVE"
    return "FOLD_FLAT"


def _avg(values: list[float]) -> float:
    return sum(values) / max(len(values), 1)


def _safe_late_entry_pct(samples: list[_PolicySample]) -> float:
    if not samples:
        return 0.0
    late = sum(1 for s in samples if s.prior_move_pct > max(1.0, s.atr_pct * 2.5))
    return late / len(samples)


def _safe_reversal_pct(samples: list[_PolicySample]) -> float:
    if not samples:
        return 0.0
    reversed_count = sum(
        1 for s in samples
        if s.prior_move_pct > max(1.0, s.atr_pct * 2.5)
        and s.exit_reason in {"STOP_LOSS", "HORIZON_CLOSE"}
    )
    return reversed_count / len(samples)


def _summarise_fold(
    fold_index: int,
    samples: list[_PolicySample],
    timeframe_warnings: list[str],
) -> FoldRegimeSummary:
    if not samples:
        return FoldRegimeSummary(
            fold=fold_index,
            trades=0,
            start="",
            end="",
            policy_net_ev=0.0,
            baseline_net_ev=0.0,
            delta_ev=0.0,
            win_rate=0.0,
            tp_pct=0.0,
            sl_pct=0.0,
            time_pct=0.0,
            avg_duration_bars=0.0,
            avg_mfe_pct=0.0,
            avg_mae_pct=0.0,
            avg_atr_pct=0.0,
            avg_prior_move_pct=0.0,
            median_prior_move_pct=0.0,
            long_pct=0.0,
            short_pct=0.0,
            late_entry_pct=0.0,
            reversal_pct=0.0,
            classification="FOLD_EMPTY",
        )
    policy_returns = [s.net_return_pct for s in samples]
    baseline_returns = [s.baseline_net_return_pct for s in samples]
    wins = [v for v in policy_returns if v > 0]
    tp = sum(1 for s in samples if s.exit_reason == "TAKE_PROFIT")
    sl = sum(1 for s in samples if s.exit_reason == "STOP_LOSS")
    tm = sum(1 for s in samples if s.exit_reason in {"HORIZON_CLOSE", "TIME_REDUCED"})
    longs = sum(1 for s in samples if s.side == "LONG")
    shorts = sum(1 for s in samples if s.side == "SHORT")
    policy_ev = _avg(policy_returns)
    baseline_ev = _avg(baseline_returns)
    if not samples:
        timeframe_warnings.append(f"fold_{fold_index}_empty_window")
    return FoldRegimeSummary(
        fold=fold_index,
        trades=len(samples),
        start=samples[0].timestamp.isoformat(),
        end=samples[-1].timestamp.isoformat(),
        policy_net_ev=policy_ev,
        baseline_net_ev=baseline_ev,
        delta_ev=policy_ev - baseline_ev,
        win_rate=len(wins) / max(len(samples), 1),
        tp_pct=tp / max(len(samples), 1),
        sl_pct=sl / max(len(samples), 1),
        time_pct=tm / max(len(samples), 1),
        avg_duration_bars=_avg([float(s.duration_bars) for s in samples]),
        avg_mfe_pct=_avg([s.mfe_pct for s in samples]),
        avg_mae_pct=_avg([s.mae_pct for s in samples]),
        avg_atr_pct=_avg([s.atr_pct for s in samples]),
        avg_prior_move_pct=_avg([s.prior_move_pct for s in samples]),
        median_prior_move_pct=median([s.prior_move_pct for s in samples]),
        long_pct=longs / max(len(samples), 1),
        short_pct=shorts / max(len(samples), 1),
        late_entry_pct=_safe_late_entry_pct(samples),
        reversal_pct=_safe_reversal_pct(samples),
        classification=_classify_fold(policy_ev, policy_ev - baseline_ev),
    )


def _diff_fold1_vs_others(
    fold_summaries: list[FoldRegimeSummary],
) -> tuple[list[str], list[str]]:
    if len(fold_summaries) < 2:
        return ["not_enough_folds_for_diff"], []
    fold1 = fold_summaries[0]
    rest = fold_summaries[1:]
    differences: list[str] = []
    filters: list[str] = []
    if fold1.trades < 30 or any(f.trades < 30 for f in rest):
        differences.append("fold_sample_size_small_use_with_caution")
    rest_atr = _avg([f.avg_atr_pct for f in rest])
    rest_prior_move = _avg([f.avg_prior_move_pct for f in rest])
    rest_late = _avg([f.late_entry_pct for f in rest])
    rest_reversal = _avg([f.reversal_pct for f in rest])
    rest_time = _avg([f.time_pct for f in rest])

    if fold1.avg_atr_pct > rest_atr * 1.25:
        differences.append(
            f"fold1_atr_higher_than_rest_avg:{fold1.avg_atr_pct:.4f}_vs_{rest_atr:.4f}"
        )
        filters.append("block_when_atr_pct_above_rolling_median_by_25pct")
    elif fold1.avg_atr_pct < rest_atr * 0.75:
        differences.append(
            f"fold1_atr_lower_than_rest_avg:{fold1.avg_atr_pct:.4f}_vs_{rest_atr:.4f}"
        )
        filters.append("watch_if_low_volatility_regime_keeps_baseline_better")
    if fold1.avg_prior_move_pct > rest_prior_move * 1.3:
        differences.append(
            f"fold1_prior_move_larger:{fold1.avg_prior_move_pct:.4f}_vs_{rest_prior_move:.4f}"
        )
        filters.append("block_when_prior_move_pct_above_2_5x_atr_pct")
    if fold1.late_entry_pct > rest_late + 0.05:
        differences.append(
            f"fold1_late_entry_share:{fold1.late_entry_pct:.4f}_vs_{rest_late:.4f}"
        )
        filters.append("tighten_late_entry_block_threshold")
    if fold1.reversal_pct > rest_reversal + 0.03:
        differences.append(
            f"fold1_reversal_share:{fold1.reversal_pct:.4f}_vs_{rest_reversal:.4f}"
        )
        filters.append("apply_reversal_risk_block_in_addition_to_late_entry_block")
    if fold1.time_pct > rest_time + 0.05:
        differences.append(
            f"fold1_time_exit_share:{fold1.time_pct:.4f}_vs_{rest_time:.4f}"
        )
        filters.append("apply_time_death_filter_or_shorter_holding_window")
    if fold1.long_pct > 0.7 and any(f.long_pct < 0.5 for f in rest):
        differences.append("fold1_long_dominated_others_more_balanced")
    if fold1.short_pct > 0.7 and any(f.short_pct < 0.5 for f in rest):
        differences.append("fold1_short_dominated_others_more_balanced")
    if not differences:
        differences.append("fold1_metrics_within_normal_band_no_actionable_regime_signal")
    if not filters:
        filters.append("no_filter_recommended_yet_collect_more_data")
    return differences, filters


def _decision(
    fold_summaries: list[FoldRegimeSummary],
    differences: list[str],
    trades_total: int,
    min_trades_for_decision: int = 120,
) -> str:
    if trades_total < min_trades_for_decision:
        return "NEED_MORE_DATA"
    if not fold_summaries:
        return "NEED_MORE_DATA"
    fold1 = fold_summaries[0]
    if fold1.classification == "FOLD_LOSS_AND_NO_IMPROVEMENT":
        return "FOLD1_LOSS_REQUIRES_FILTER"
    if fold1.classification == "FOLD_LOSS_DESPITE_IMPROVEMENT":
        return "FOLD1_DELTA_POSITIVE_BUT_EV_NEGATIVE_FILTER_RECOMMENDED"
    if "fold1_metrics_within_normal_band_no_actionable_regime_signal" in differences:
        return "FOLD1_NORMAL_NO_FILTER_NEEDED"
    return "INVESTIGATE_FILTERS_BEFORE_PROMOTION"


def run_dot_regime_diagnosis(
    config: Any,
    db: Any,
    *,
    hours: int = 720,
    timeframe: str = "5m",
    symbols: str | list[str] | None = "DOTUSDT",
    policy_name: str = "late_entry_block_plus_dynamic_hold",
    folds: int = 4,
    min_trades_per_fold: int = 20,
) -> DotRegimeDiagnosisReport:
    """Build the fold-by-fold diagnosis report for a single policy."""
    symbol_list = parse_symbols(symbols, config)
    if not symbol_list:
        symbol_list = ["DOTUSDT"]
    bundle = load_replay_trade_contexts(
        config, db, hours=hours, timeframe=timeframe, symbols=symbol_list,
    )
    policy = _policy_by_name(policy_name)
    samples = _build_samples(bundle.contexts, policy)
    folds = max(2, min(int(folds or 4), 12))
    if len(samples) < folds * max(1, min_trades_per_fold):
        return DotRegimeDiagnosisReport(
            symbol=",".join(symbol_list),
            timeframe=timeframe,
            hours=int(hours),
            folds=folds,
            policy_name=policy_name,
            trades_total=len(samples),
            fold_summaries=[],
            differences=["not_enough_trades_for_fold_diagnosis"],
            candidate_filters=["collect_more_trades_before_inferring_filters"],
            decision="NEED_MORE_DATA",
            loader_statuses=bundle.loader_statuses,
            warnings=list(bundle.warnings),
        )
    chunk = max(1, len(samples) // folds)
    fold_warnings: list[str] = list(bundle.warnings)
    fold_summaries: list[FoldRegimeSummary] = []
    for index in range(folds):
        start = index * chunk
        end = len(samples) if index == folds - 1 else min(len(samples), (index + 1) * chunk)
        fold_samples = samples[start:end]
        fold_summaries.append(_summarise_fold(index + 1, fold_samples, fold_warnings))
    differences, candidate_filters = _diff_fold1_vs_others(fold_summaries)
    decision = _decision(fold_summaries, differences, trades_total=len(samples))
    return DotRegimeDiagnosisReport(
        symbol=",".join(symbol_list),
        timeframe=timeframe,
        hours=int(hours),
        folds=folds,
        policy_name=policy_name,
        trades_total=len(samples),
        fold_summaries=fold_summaries,
        differences=differences,
        candidate_filters=candidate_filters,
        decision=decision,
        loader_statuses=bundle.loader_statuses,
        warnings=fold_warnings,
    )


def render_dot_regime_diagnosis_text(report: DotRegimeDiagnosisReport) -> str:
    lines = [
        "DOT REGIME DIAGNOSIS START",
        f"symbol: {report.symbol}",
        f"timeframe: {report.timeframe}",
        f"hours: {report.hours}",
        f"folds: {report.folds}",
        f"policy_name: {report.policy_name}",
        f"trades_total: {report.trades_total}",
        f"decision: {report.decision}",
        "fold | trades | start | end | policy_ev | baseline_ev | delta | win | TP | SL | TIME | dur | mfe | mae | atr | prior | long | short | late | reversal | classification",
    ]
    for f in report.fold_summaries:
        lines.append(
            f"{f.fold} | {f.trades} | {f.start} | {f.end} | {f.policy_net_ev:.6f} | "
            f"{f.baseline_net_ev:.6f} | {f.delta_ev:.6f} | {f.win_rate:.3f} | {f.tp_pct:.3f} | "
            f"{f.sl_pct:.3f} | {f.time_pct:.3f} | {f.avg_duration_bars:.2f} | "
            f"{f.avg_mfe_pct:.4f} | {f.avg_mae_pct:.4f} | {f.avg_atr_pct:.4f} | "
            f"{f.avg_prior_move_pct:.4f} | {f.long_pct:.3f} | {f.short_pct:.3f} | "
            f"{f.late_entry_pct:.3f} | {f.reversal_pct:.3f} | {f.classification}"
        )
    lines.append("differences:")
    for diff in report.differences:
        lines.append(f"- {diff}")
    lines.append("candidate_filters:")
    for filt in report.candidate_filters:
        lines.append(f"- {filt}")
    if report.warnings:
        lines.append("warnings:")
        for warning in report.warnings[:8]:
            lines.append(f"- {warning}")
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "activation: disabled",
        "final_recommendation: NO LIVE",
        "DOT REGIME DIAGNOSIS END",
    ])
    return "\n".join(lines)


def dot_regime_diagnosis_text(
    config: Any,
    db: Any,
    *,
    hours: int = 720,
    timeframe: str = "5m",
    symbols: str | list[str] | None = "DOTUSDT",
    folds: int = 4,
    policy_name: str = "late_entry_block_plus_dynamic_hold",
) -> str:
    return render_dot_regime_diagnosis_text(run_dot_regime_diagnosis(
        config, db,
        hours=hours, timeframe=timeframe, symbols=symbols,
        policy_name=policy_name, folds=folds,
    ))
