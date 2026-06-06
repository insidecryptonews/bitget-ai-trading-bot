"""V8.2.8 — Side-Aware Score Calibration (research-only).

Re-calibrates the bot's score per LONG/SHORT and per regime, asking
whether the score is useful in each segment.

Hard contract: read-only. Never touches production scoring.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK
from .counterfactual_dedup_audit import _is_evaluable, dedup_rows
from .counterfactual_training_dataset import build_dataset
from .score_calibration_audit import (
    MONOTONIC_ANTI,
    MONOTONIC_FAIL,
    MONOTONIC_PASS,
    SCORE_BUCKETS,
    _bucket_metrics,
    _monotonic_check,
    _pearson,
)


SCORE_USEFUL_LONG = "SCORE_USEFUL_LONG"
SCORE_USEFUL_SHORT = "SCORE_USEFUL_SHORT"
SCORE_NOT_USEFUL = "SCORE_NOT_USEFUL"
SCORE_ANTI_CALIBRATED = "SCORE_ANTI_CALIBRATED"
NEED_MORE_DATA = "NEED_MORE_DATA"


@dataclass
class SideCalibrationBlock:
    side: str
    samples: int = 0
    correlation_score_vs_net_pnl: float = 0.0
    correlation_score_vs_win: float = 0.0
    monotonicity_status: str = MONOTONIC_FAIL
    bucket_table: list[dict[str, Any]] = field(default_factory=list)
    by_regime: dict[str, dict[str, Any]] = field(default_factory=dict)
    usefulness: str = NEED_MORE_DATA

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SideAwareScoreReport:
    hours: int
    generated_at: str
    samples: int = 0
    global_correlation_score_vs_net_pnl: float = 0.0
    global_correlation_score_vs_win: float = 0.0
    long_block: SideCalibrationBlock = field(
        default_factory=lambda: SideCalibrationBlock(side="LONG"),
    )
    short_block: SideCalibrationBlock = field(
        default_factory=lambda: SideCalibrationBlock(side="SHORT"),
    )
    score_usable_long: bool = False
    score_usable_short: bool = False
    score_only_diagnostic: bool = False
    score_excluded_as_gate: bool = True
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _classify_side_usefulness(
    side: str, monotonicity: str, samples: int,
) -> str:
    if samples < 30:
        return NEED_MORE_DATA
    if monotonicity == MONOTONIC_PASS:
        return SCORE_USEFUL_LONG if side == "LONG" else SCORE_USEFUL_SHORT
    if monotonicity == MONOTONIC_ANTI:
        return SCORE_ANTI_CALIBRATED
    return SCORE_NOT_USEFUL


def _build_side_block(rows: list[dict[str, Any]], side: str) -> SideCalibrationBlock:
    block = SideCalibrationBlock(side=side)
    side_rows = [r for r in rows if str(r.get("side", "")).upper() == side]
    block.samples = len(side_rows)
    if not side_rows:
        return block
    scores: list[float] = []
    pnls: list[float] = []
    wins: list[float] = []
    for r in side_rows:
        try:
            scores.append(float(r.get("score") or 0))
            net = float(r.get("baseline_net_pnl_est") or 0)
        except Exception:
            continue
        pnls.append(net)
        wins.append(1.0 if net > 0 else 0.0)
    if not scores:
        return block
    block.correlation_score_vs_net_pnl = _pearson(scores, pnls)
    block.correlation_score_vs_win = _pearson(scores, wins)
    buckets = _bucket_metrics(side_rows)
    block.bucket_table = [b.as_dict() for b in buckets]
    block.monotonicity_status = _monotonic_check(buckets)

    regimes = sorted({str(r.get("regime", "UNKNOWN")).upper() for r in side_rows})
    for regime in regimes:
        rs = [r for r in side_rows if str(r.get("regime", "")).upper() == regime]
        if len(rs) >= 10:
            rb = _bucket_metrics(rs)
            block.by_regime[regime] = {
                "samples": len(rs),
                "buckets": [b.as_dict() for b in rb],
                "monotonicity": _monotonic_check(rb),
            }
    block.usefulness = _classify_side_usefulness(
        side, block.monotonicity_status, block.samples,
    )
    return block


def calibrate_score_by_side(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    rows: Iterable[dict[str, Any]] | None = None,
) -> SideAwareScoreReport:
    report = SideAwareScoreReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    evaluable = [r for r in dataset if _is_evaluable(r)]
    evaluable = dedup_rows(evaluable)
    report.samples = len(evaluable)
    if not evaluable:
        return report

    scores: list[float] = []
    pnls: list[float] = []
    wins: list[float] = []
    for r in evaluable:
        try:
            scores.append(float(r.get("score") or 0))
            net = float(r.get("baseline_net_pnl_est") or 0)
            pnls.append(net)
            wins.append(1.0 if net > 0 else 0.0)
        except Exception:
            continue
    if scores:
        report.global_correlation_score_vs_net_pnl = _pearson(scores, pnls)
        report.global_correlation_score_vs_win = _pearson(scores, wins)

    report.long_block = _build_side_block(evaluable, "LONG")
    report.short_block = _build_side_block(evaluable, "SHORT")

    report.score_usable_long = report.long_block.usefulness == SCORE_USEFUL_LONG
    report.score_usable_short = report.short_block.usefulness == SCORE_USEFUL_SHORT
    if report.score_usable_long or report.score_usable_short:
        report.score_excluded_as_gate = False
        report.score_only_diagnostic = False
    elif report.long_block.usefulness == SCORE_ANTI_CALIBRATED or report.short_block.usefulness == SCORE_ANTI_CALIBRATED:
        report.score_excluded_as_gate = True
        report.score_only_diagnostic = False
    else:
        report.score_excluded_as_gate = True
        report.score_only_diagnostic = True

    report.status = STATUS_OK
    return report
