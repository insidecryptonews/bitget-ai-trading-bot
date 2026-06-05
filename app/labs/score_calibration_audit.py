"""V8.2.5 — Score Calibration Audit (research-only).

Tests whether the bot's internal score predicts realised net outcome on the
deduplicated counterfactual dataset.

Hard contract: read-only research. No production changes are made; the audit
emits a recommendation only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import (
    FINAL_RECOMMENDATION_NO_LIVE,
    STATUS_NEED_DATA,
    STATUS_OK,
)
from .counterfactual_dedup_audit import _is_evaluable, dedup_rows
from .counterfactual_training_dataset import build_dataset


MONOTONIC_PASS = "PASS"
MONOTONIC_FAIL = "FAIL"
MONOTONIC_ANTI = "ANTI_CALIBRATED"

SCORE_BUCKETS: tuple[str, ...] = ("<60", "60-69", "70-79", "80-89", "90-100")


def _score_to_bucket(score: Any) -> str:
    try:
        s = int(score)
    except Exception:
        return "<60"
    if s >= 90:
        return "90-100"
    if s >= 80:
        return "80-89"
    if s >= 70:
        return "70-79"
    if s >= 60:
        return "60-69"
    return "<60"


@dataclass
class ScoreBucketRow:
    bucket: str
    count: int
    winrate: float
    net_ev_avg_pct: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScoreCalibrationReport:
    hours: int
    generated_at: str
    samples: int = 0
    correlation_score_vs_net_pnl: float = 0.0
    correlation_score_vs_win: float = 0.0
    score_bucket_table: list[dict[str, Any]] = field(default_factory=list)
    monotonicity_status: str = MONOTONIC_FAIL
    side_specific_calibration: dict[str, Any] = field(default_factory=dict)
    regime_specific_calibration: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    recommendation: str = ""
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pearson(x: list[float], y: list[float]) -> float:
    if len(x) < 2 or len(x) != len(y):
        return 0.0
    mean_x = sum(x) / len(x)
    mean_y = sum(y) / len(y)
    cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    var_x = sum((xi - mean_x) ** 2 for xi in x)
    var_y = sum((yi - mean_y) ** 2 for yi in y)
    denom = (var_x * var_y) ** 0.5
    if denom == 0:
        return 0.0
    return cov / denom


def _bucket_metrics(rows: list[dict[str, Any]]) -> list[ScoreBucketRow]:
    by_bucket: dict[str, list[dict[str, Any]]] = {b: [] for b in SCORE_BUCKETS}
    for r in rows:
        bucket = _score_to_bucket(r.get("score"))
        by_bucket.setdefault(bucket, []).append(r)
    out: list[ScoreBucketRow] = []
    for b in SCORE_BUCKETS:
        rs = by_bucket.get(b, [])
        nets: list[float] = []
        for r in rs:
            try:
                nets.append(float(r.get("baseline_net_pnl_est") or 0))
            except Exception:
                continue
        wins = [n for n in nets if n > 0]
        out.append(ScoreBucketRow(
            bucket=b,
            count=len(rs),
            winrate=(len(wins) / max(len(nets), 1)) if nets else 0.0,
            net_ev_avg_pct=(sum(nets) / max(len(nets), 1)) if nets else 0.0,
        ))
    return out


def _monotonic_check(buckets: list[ScoreBucketRow], min_count: int = 5,
                     tolerance: float = 0.05) -> str:
    valid = [b for b in buckets if b.count >= min_count]
    if len(valid) < 3:
        return MONOTONIC_FAIL
    increasing = all(
        valid[i].net_ev_avg_pct <= valid[i + 1].net_ev_avg_pct + tolerance
        for i in range(len(valid) - 1)
    )
    decreasing = all(
        valid[i].net_ev_avg_pct + tolerance >= valid[i + 1].net_ev_avg_pct
        for i in range(len(valid) - 1)
    )
    if increasing and not decreasing:
        return MONOTONIC_PASS
    if decreasing and not increasing:
        return MONOTONIC_ANTI
    return MONOTONIC_FAIL


def audit_score_calibration(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    dedup: bool = True,
    rows: Iterable[dict[str, Any]] | None = None,
) -> ScoreCalibrationReport:
    report = ScoreCalibrationReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    evaluable = [r for r in dataset if _is_evaluable(r)]
    if dedup:
        evaluable = dedup_rows(evaluable)
    if not evaluable:
        report.status = STATUS_NEED_DATA
        return report
    report.samples = len(evaluable)
    scores: list[float] = []
    pnls: list[float] = []
    wins: list[float] = []
    for r in evaluable:
        try:
            s = float(r.get("score") or 0)
            net = float(r.get("baseline_net_pnl_est") or 0)
        except Exception:
            continue
        scores.append(s)
        pnls.append(net)
        wins.append(1.0 if net > 0 else 0.0)
    report.correlation_score_vs_net_pnl = _pearson(scores, pnls)
    report.correlation_score_vs_win = _pearson(scores, wins)
    buckets = _bucket_metrics(evaluable)
    report.score_bucket_table = [b.as_dict() for b in buckets]
    report.monotonicity_status = _monotonic_check(buckets)

    for side in ("LONG", "SHORT"):
        side_rows = [r for r in evaluable if str(r.get("side", "")).upper() == side]
        if side_rows:
            sb = _bucket_metrics(side_rows)
            report.side_specific_calibration[side] = {
                "buckets": [b.as_dict() for b in sb],
                "monotonicity": _monotonic_check(sb),
            }

    regimes = sorted({str(r.get("regime", "UNKNOWN")).upper() for r in evaluable})
    for regime in regimes:
        rs = [r for r in evaluable if str(r.get("regime", "")).upper() == regime]
        if len(rs) >= 10:
            rb = _bucket_metrics(rs)
            report.regime_specific_calibration[regime] = {
                "buckets": [b.as_dict() for b in rb],
                "monotonicity": _monotonic_check(rb),
            }

    if report.monotonicity_status == MONOTONIC_ANTI:
        report.warnings.append(
            "score is anti-calibrated: raising min_score_to_trade would WORSEN results"
        )
        report.recommendation = (
            "do NOT raise min_score_to_trade; investigate scoring asymmetry "
            "before any production change"
        )
    elif report.monotonicity_status == MONOTONIC_FAIL:
        report.warnings.append(
            "score does not monotonically order outcomes across buckets"
        )
        report.recommendation = (
            "recalibrate scoring research-only; do not change production thresholds"
        )
    else:
        report.recommendation = (
            "calibration looks monotonic; still validate via walk-forward before "
            "any production change"
        )
    report.status = STATUS_OK
    return report
