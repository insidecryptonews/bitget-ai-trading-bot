"""V8.2.5 — Short Sign / Barrier Audit (research-only).

Investigates SHORT signals labelled as ``SL`` whose future 4h return is
actually favourable for a short (negative return). Classifies each case so
the operator can tell apart legitimate stop-before-drop from possible
labelling / sign / barrier-order bugs.

Hard contract: read-only. No live. No PaperTrader changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import (
    FINAL_RECOMMENDATION_NO_LIVE,
    SIDE_SHORT,
    STATUS_NEED_DATA,
    STATUS_OK,
)
from .counterfactual_training_dataset import build_dataset


VERDICT_TRUSTED = "SHORT_LABELS_TRUSTED"
VERDICT_SUSPECT = "SHORT_LABELS_SUSPECT"
VERDICT_BROKEN = "SHORT_LABELS_BROKEN"

CLASS_NO_ISSUE = "no_issue"
CLASS_LEGITIMATE = "legitimate_stop_before_drop"
CLASS_SHORT_SIGN_BUG = "possible_short_sign_bug"
CLASS_BARRIER_ORDER_BUG = "possible_barrier_order_bug"
CLASS_AMBIGUOUS_SAME_BAR = "ambiguous_same_bar"
CLASS_NEEDS_PATH = "needs_ohlcv_path"

SUSPICIOUS_CLASSES = {
    CLASS_SHORT_SIGN_BUG, CLASS_BARRIER_ORDER_BUG,
    CLASS_AMBIGUOUS_SAME_BAR, CLASS_NEEDS_PATH,
}


@dataclass
class ShortAuditCase:
    signal_id: Any
    timestamp: str
    symbol: str
    entry_price: float
    ret_1h_pct: float | None
    ret_4h_pct: float | None
    ret_24h_pct: float | None
    mfe_pct: float | None
    mae_pct: float | None
    first_barrier_hit: str | None
    baseline_result: str
    baseline_net_pnl: float | None
    classification: str
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ShortSignAuditReport:
    hours: int
    generated_at: str
    total_short_rows: int = 0
    evaluable_short_rows: int = 0
    suspicious_short_rows: int = 0
    suspicious_ratio: float = 0.0
    by_classification: dict[str, int] = field(default_factory=dict)
    examples_top_50: list[dict[str, Any]] = field(default_factory=list)
    verdict: str = VERDICT_TRUSTED
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_short_evaluable(row: dict[str, Any]) -> bool:
    side = str(row.get("side", "")).upper()
    if side != SIDE_SHORT:
        return False
    label = str(row.get("training_label", ""))
    if label in {"NEED_DATA", "UNCERTAIN"}:
        return False
    return row.get("baseline_net_pnl_est") is not None


def classify_short(row: dict[str, Any]) -> tuple[str, str]:
    """Classify a SHORT row.

    Returns ``(classification, notes)``. A SHORT is "no issue" unless it has
    ``first_barrier_hit=SL`` AND ``ret_4h_pct`` is strongly favourable
    (negative) — in which case we try to tell apart legitimate
    stop-before-drop from labelling/sign/barrier bugs.
    """
    first_barrier = str(row.get("first_barrier_hit") or "")
    ret_4h = row.get("ret_4h_pct")
    mfe = row.get("mfe_pct")
    mae = row.get("mae_pct")
    if first_barrier != "SL":
        return CLASS_NO_ISSUE, ""
    if ret_4h is None:
        return CLASS_NEEDS_PATH, "missing_ret_4h"
    try:
        ret_4h_f = float(ret_4h)
    except Exception:
        return CLASS_NEEDS_PATH, "ret_4h_not_numeric"
    if ret_4h_f >= -0.50:
        return CLASS_NO_ISSUE, ""
    # SL hit + strongly favourable future → suspicious.
    if isinstance(mae, (int, float)) and float(mae) > -0.30:
        return CLASS_BARRIER_ORDER_BUG, "MAE_small_but_SL_hit"
    if isinstance(mfe, (int, float)) and float(mfe) < 0.10:
        return CLASS_AMBIGUOUS_SAME_BAR, "same_bar_stop_before_tp_likely"
    if (
        isinstance(mfe, (int, float))
        and isinstance(mae, (int, float))
        and float(mfe) > abs(float(mae))
    ):
        return CLASS_LEGITIMATE, "stop_then_drop_continuation"
    return CLASS_SHORT_SIGN_BUG, "ret_4h_favourable_but_SL"


def _verdict_from_ratio(ratio: float, suspect: float, broken: float) -> str:
    if ratio >= broken:
        return VERDICT_BROKEN
    if ratio >= suspect:
        return VERDICT_SUSPECT
    return VERDICT_TRUSTED


def audit_short_sign(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    suspect_threshold: float = 0.15,
    broken_threshold: float = 0.40,
    rows: Iterable[dict[str, Any]] | None = None,
) -> ShortSignAuditReport:
    report = ShortSignAuditReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    short_rows = [r for r in dataset if str(r.get("side", "")).upper() == SIDE_SHORT]
    report.total_short_rows = len(short_rows)
    evaluable = [r for r in short_rows if _is_short_evaluable(r)]
    report.evaluable_short_rows = len(evaluable)
    if not evaluable:
        report.status = STATUS_NEED_DATA
        return report
    cases: list[ShortAuditCase] = []
    classifications: dict[str, int] = {}
    for r in evaluable:
        classification, notes = classify_short(r)
        classifications[classification] = classifications.get(classification, 0) + 1
        if classification == CLASS_NO_ISSUE:
            continue
        try:
            entry_price = float(r.get("entry_price") or 0)
        except Exception:
            entry_price = 0.0
        cases.append(ShortAuditCase(
            signal_id=r.get("signal_id"),
            timestamp=str(r.get("timestamp", "")),
            symbol=str(r.get("symbol", "")),
            entry_price=entry_price,
            ret_1h_pct=r.get("ret_1h_pct"),
            ret_4h_pct=r.get("ret_4h_pct"),
            ret_24h_pct=r.get("ret_24h_pct"),
            mfe_pct=r.get("mfe_pct"),
            mae_pct=r.get("mae_pct"),
            first_barrier_hit=r.get("first_barrier_hit"),
            baseline_result=str(r.get("baseline_result", "")),
            baseline_net_pnl=r.get("baseline_net_pnl_est"),
            classification=classification,
            notes=notes,
        ))
    suspicious_count = sum(
        1 for c in cases if c.classification in SUSPICIOUS_CLASSES
    )
    report.suspicious_short_rows = suspicious_count
    report.suspicious_ratio = suspicious_count / max(report.evaluable_short_rows, 1)
    report.by_classification = classifications
    cases.sort(
        key=lambda c: (
            1 if c.classification in SUSPICIOUS_CLASSES else 0,
            -(float(c.ret_4h_pct) if isinstance(c.ret_4h_pct, (int, float)) else 0.0),
        ),
        reverse=True,
    )
    report.examples_top_50 = [c.as_dict() for c in cases[:50]]
    report.verdict = _verdict_from_ratio(
        report.suspicious_ratio, suspect_threshold, broken_threshold,
    )
    report.status = STATUS_OK
    return report
