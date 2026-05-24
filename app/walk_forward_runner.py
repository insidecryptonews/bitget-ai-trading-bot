"""Walk-Forward Runner — offline temporal splits over backtester trades.

Splits the multi-symbol backtester output into temporal windows (train/test)
and reports stability metrics per setup. Pure offline; no exchange calls,
no runtime modification.

Gates report one of:
  PASS                : net_ev positive in test windows + sample sufficient
  NEED_MORE_FOLDS     : fewer windows than min required
  FAIL                : negative net_ev or instability
  NOT_RUN             : no input data
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict, field
from typing import Any

from .backtest_breakdown import TradeRecord
from .utils import safe_float


FINAL_RECOMMENDATION = "NO LIVE"

WF_PASS = "PASS"
WF_FAIL = "FAIL"
WF_NEED_MORE_FOLDS = "NEED_MORE_FOLDS"
WF_NOT_RUN = "NOT_RUN"


@dataclass
class WindowMetric:
    window_index: int
    trades: int
    net_ev: float
    net_pf: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WalkForwardSetup:
    group_key: str
    total_trades: int
    windows: list[WindowMetric]
    positive_windows: int
    negative_windows: int
    test_net_ev: float
    test_net_pf: float
    stability_score: float
    status: str
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["windows"] = [w.as_dict() for w in self.windows]
        return payload


@dataclass
class WalkForwardReport:
    folds: int
    min_trades_per_setup: int
    setups: list[WalkForwardSetup] = field(default_factory=list)
    overall_status: str = WF_NOT_RUN
    overall_reasons: list[str] = field(default_factory=list)
    final_recommendation: str = FINAL_RECOMMENDATION
    research_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["setups"] = [s.as_dict() for s in self.setups]
        return payload


def _split_indices(n: int, folds: int) -> list[tuple[int, int]]:
    if folds <= 1 or n < folds:
        return [(0, n)]
    size = n // folds
    edges = [(i * size, (i + 1) * size) for i in range(folds - 1)]
    edges.append(((folds - 1) * size, n))
    return edges


def _window_metric(trades: list[TradeRecord], window_index: int) -> WindowMetric:
    if not trades:
        return WindowMetric(window_index=window_index, trades=0, net_ev=0.0, net_pf=0.0)
    net = [t.net_return_pct for t in trades]
    wins = [v for v in net if v > 0]
    losses = [v for v in net if v < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    return WindowMetric(
        window_index=window_index,
        trades=len(trades),
        net_ev=sum(net) / len(net),
        net_pf=pf,
    )


def _stability_score(windows: list[WindowMetric]) -> float:
    """Variance-aware stability score: 1.0 = perfectly stable, 0.0 = chaotic.

    Computed as `1 - normalised_stdev_of_net_ev`. Capped to [0,1].
    """
    if not windows:
        return 0.0
    values = [w.net_ev for w in windows]
    mean = sum(values) / len(values)
    if len(values) < 2:
        return 1.0 if mean > 0 else 0.0
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    std = var ** 0.5
    if abs(mean) < 1e-9:
        return 0.0
    normalised = std / max(abs(mean), 1e-6)
    return max(0.0, min(1.0, 1.0 - normalised))


def build_walk_forward(
    records: list[TradeRecord],
    *,
    folds: int = 4,
    min_trades_per_setup: int = 100,
    min_positive_windows: int = 2,
) -> WalkForwardReport:
    if not records:
        return WalkForwardReport(
            folds=folds,
            min_trades_per_setup=min_trades_per_setup,
            setups=[],
            overall_status=WF_NOT_RUN,
            overall_reasons=["no_trade_records_provided"],
        )

    # Sort records by entry_index, group by setup_key.
    grouped: dict[str, list[TradeRecord]] = defaultdict(list)
    for record in records:
        grouped[record.setup_key].append(record)
    for key in grouped:
        grouped[key].sort(key=lambda t: t.entry_index)

    setups: list[WalkForwardSetup] = []
    for key, trades in grouped.items():
        if len(trades) < min_trades_per_setup:
            setups.append(WalkForwardSetup(
                group_key=key,
                total_trades=len(trades),
                windows=[],
                positive_windows=0,
                negative_windows=0,
                test_net_ev=0.0,
                test_net_pf=0.0,
                stability_score=0.0,
                status=WF_NEED_MORE_FOLDS,
                reasons=[f"trades<{min_trades_per_setup}"],
            ))
            continue
        edges = _split_indices(len(trades), folds)
        if len(edges) < folds:
            setups.append(WalkForwardSetup(
                group_key=key,
                total_trades=len(trades),
                windows=[],
                positive_windows=0,
                negative_windows=0,
                test_net_ev=0.0,
                test_net_pf=0.0,
                stability_score=0.0,
                status=WF_NEED_MORE_FOLDS,
                reasons=["insufficient_folds_for_split"],
            ))
            continue
        windows = [
            _window_metric(trades[start:end], index)
            for index, (start, end) in enumerate(edges)
        ]
        positive = sum(1 for w in windows if w.net_ev > 0)
        negative = sum(1 for w in windows if w.net_ev < 0)
        # Use the LAST fold as out-of-sample test window
        test_window = windows[-1]
        stability = _stability_score(windows)

        status = WF_PASS
        reasons: list[str] = []
        if positive < min_positive_windows:
            status = WF_FAIL
            reasons.append(f"positive_windows={positive}<{min_positive_windows}")
        if test_window.net_ev <= 0:
            status = WF_FAIL
            reasons.append(f"test_net_ev={test_window.net_ev:.6f}<=0")
        if stability < 0.3:
            status = WF_FAIL
            reasons.append(f"stability_score={stability:.3f}<0.3")
        if not reasons:
            reasons.append("all_windows_within_stability_band")

        setups.append(WalkForwardSetup(
            group_key=key,
            total_trades=len(trades),
            windows=windows,
            positive_windows=positive,
            negative_windows=negative,
            test_net_ev=test_window.net_ev,
            test_net_pf=test_window.net_pf,
            stability_score=stability,
            status=status,
            reasons=reasons,
        ))

    # Overall verdict: PASS if at least one setup passes, else worst-of-children
    pass_count = sum(1 for s in setups if s.status == WF_PASS)
    fail_count = sum(1 for s in setups if s.status == WF_FAIL)
    if pass_count > 0:
        overall = WF_PASS
        overall_reasons = [f"setups_passing={pass_count}"]
    elif fail_count > 0:
        overall = WF_FAIL
        overall_reasons = [f"setups_failing={fail_count}"]
    else:
        overall = WF_NEED_MORE_FOLDS
        overall_reasons = ["no_setup_had_sufficient_sample_for_walk_forward"]

    return WalkForwardReport(
        folds=folds,
        min_trades_per_setup=min_trades_per_setup,
        setups=setups,
        overall_status=overall,
        overall_reasons=overall_reasons,
    )


def render_walk_forward_text(report: WalkForwardReport) -> str:
    lines = ["WALK FORWARD RUNNER START"]
    lines.append(f"folds: {report.folds}")
    lines.append(f"min_trades_per_setup: {report.min_trades_per_setup}")
    lines.append(f"setups_evaluated: {len(report.setups)}")
    lines.append(f"overall_status: {report.overall_status}")
    lines.append(f"overall_reasons: {','.join(report.overall_reasons)}")
    lines.append("")
    # Show passing first, then failing
    for status in (WF_PASS, WF_FAIL, WF_NEED_MORE_FOLDS):
        block = [s for s in report.setups if s.status == status]
        if not block:
            continue
        lines.append(f"{status} ({len(block)}):")
        for s in block[:25]:
            window_text = " ".join(f"w{w.window_index}={w.net_ev:.4f}" for w in s.windows)
            lines.append(
                f"- {s.group_key} | trades={s.total_trades} positive_windows={s.positive_windows}/{s.negative_windows} "
                f"test_net_ev={s.test_net_ev:.6f} test_net_pf={s.test_net_pf:.4f} "
                f"stability={s.stability_score:.3f} reasons={','.join(s.reasons)}"
            )
            if window_text:
                lines.append(f"  windows: {window_text}")
        lines.append("")
    lines.append("research_only: true")
    lines.append(f"final_recommendation: {report.final_recommendation}")
    lines.append("WALK FORWARD RUNNER END")
    return "\n".join(lines)
