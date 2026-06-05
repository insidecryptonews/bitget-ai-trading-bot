"""V8.2.6 — Score Recalibration Sandbox (research-only).

Offline experiment: tests whether a per-bucket recalibration of the bot's
score correlates better with the realised outcome than the raw score.

Hard contract: this lab **never** changes production scoring. It only
reports correlations + a recommendation.
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
    _score_to_bucket,
)


REC_KEEP_DISABLED = "KEEP_SCORE_DISABLED_AS_GATE"
REC_CANDIDATE = "SCORE_RECALIBRATION_CANDIDATE"
REC_NOT_USEFUL = "SCORE_NOT_USEFUL"


@dataclass
class ScoreRecalibrationReport:
    hours: int
    generated_at: str
    samples: int = 0
    old_correlation: float = 0.0
    recalibrated_correlation: float = 0.0
    old_monotonicity: str = MONOTONIC_FAIL
    recalibrated_monotonicity: str = MONOTONIC_FAIL
    bucket_to_recalibrated_score: dict[str, float] = field(default_factory=dict)
    bucket_table_old: list[dict[str, Any]] = field(default_factory=list)
    bucket_table_recalibrated: list[dict[str, Any]] = field(default_factory=list)
    delta_correlation: float = 0.0
    recommendation: str = REC_KEEP_DISABLED
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bucket_winrate(rows: list[dict[str, Any]]) -> float:
    nets: list[float] = []
    for r in rows:
        try:
            nets.append(float(r.get("baseline_net_pnl_est") or 0))
        except Exception:
            continue
    if not nets:
        return 0.0
    wins = [n for n in nets if n > 0]
    return len(wins) / len(nets)


def _build_recalibration_mapping(rows: list[dict[str, Any]]) -> dict[str, float]:
    """For each bucket, the recalibrated score is the bucket's observed
    winrate × 100 (so the metric is comparable to the original score scale).
    """
    by_bucket: dict[str, list[dict[str, Any]]] = {b: [] for b in SCORE_BUCKETS}
    for r in rows:
        bucket = _score_to_bucket(r.get("score"))
        by_bucket.setdefault(bucket, []).append(r)
    mapping: dict[str, float] = {}
    for b in SCORE_BUCKETS:
        rs = by_bucket.get(b, [])
        mapping[b] = _bucket_winrate(rs) * 100.0
    return mapping


def _apply_recalibration(rows: list[dict[str, Any]], mapping: dict[str, float]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        bucket = _score_to_bucket(r.get("score"))
        clone = dict(r)
        clone["score_recalibrated"] = mapping.get(bucket, 0.0)
        out.append(clone)
    return out


def _recalibrated_buckets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map recalibrated scores back into the same 5 buckets just for
    table display.
    """
    # We bucket by the recalibrated score using the same thresholds.
    by_bucket: dict[str, list[dict[str, Any]]] = {b: [] for b in SCORE_BUCKETS}
    for r in rows:
        bucket = _score_to_bucket(r.get("score_recalibrated", 0))
        by_bucket.setdefault(bucket, []).append(r)
    return [
        {
            "bucket": b,
            "count": len(by_bucket.get(b, [])),
            "winrate": _bucket_winrate(by_bucket.get(b, [])),
            "net_ev_avg_pct": (
                sum(
                    float(r.get("baseline_net_pnl_est") or 0)
                    for r in by_bucket.get(b, [])
                ) / max(len(by_bucket.get(b, [])), 1)
            ) if by_bucket.get(b) else 0.0,
        }
        for b in SCORE_BUCKETS
    ]


def _recommend(
    old_corr: float, new_corr: float,
    old_mono: str, new_mono: str,
) -> str:
    if old_mono == MONOTONIC_PASS and new_mono != MONOTONIC_ANTI:
        # Already good; recalibration adds nothing.
        return REC_NOT_USEFUL
    if new_mono == MONOTONIC_PASS and abs(new_corr) > abs(old_corr) + 0.05:
        return REC_CANDIDATE
    if abs(new_corr) <= abs(old_corr) + 0.01:
        return REC_KEEP_DISABLED
    return REC_KEEP_DISABLED


def sandbox_recalibration(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    rows: Iterable[dict[str, Any]] | None = None,
) -> ScoreRecalibrationReport:
    report = ScoreRecalibrationReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    evaluable = [r for r in dataset if _is_evaluable(r)]
    evaluable = dedup_rows(evaluable)
    if not evaluable:
        return report
    report.samples = len(evaluable)

    scores: list[float] = []
    pnls: list[float] = []
    for r in evaluable:
        try:
            scores.append(float(r.get("score") or 0))
            pnls.append(float(r.get("baseline_net_pnl_est") or 0))
        except Exception:
            continue
    if not scores:
        return report
    report.old_correlation = _pearson(scores, pnls)
    old_buckets = _bucket_metrics(evaluable)
    report.bucket_table_old = [b.as_dict() for b in old_buckets]
    report.old_monotonicity = _monotonic_check(old_buckets)

    mapping = _build_recalibration_mapping(evaluable)
    report.bucket_to_recalibrated_score = mapping
    recalibrated = _apply_recalibration(evaluable, mapping)

    rec_scores: list[float] = []
    rec_pnls: list[float] = []
    for r in recalibrated:
        try:
            rec_scores.append(float(r.get("score_recalibrated") or 0))
            rec_pnls.append(float(r.get("baseline_net_pnl_est") or 0))
        except Exception:
            continue
    report.recalibrated_correlation = _pearson(rec_scores, rec_pnls)
    rec_table = _recalibrated_buckets(recalibrated)
    report.bucket_table_recalibrated = rec_table
    # Build a temporary ScoreBucketRow-like list for monotonic check.
    from .score_calibration_audit import ScoreBucketRow
    rec_rows = [
        ScoreBucketRow(
            bucket=b["bucket"], count=b["count"], winrate=b["winrate"],
            net_ev_avg_pct=b["net_ev_avg_pct"],
        )
        for b in rec_table
    ]
    report.recalibrated_monotonicity = _monotonic_check(rec_rows)
    report.delta_correlation = report.recalibrated_correlation - report.old_correlation
    report.recommendation = _recommend(
        report.old_correlation, report.recalibrated_correlation,
        report.old_monotonicity, report.recalibrated_monotonicity,
    )
    report.status = STATUS_OK
    return report
