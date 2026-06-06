"""V8.2.8 — Dual-Side Barrier Truth Audit (research-only).

Audits LONG and SHORT signals symmetrically. For each side it checks:

- TP/SL orientation relative to entry (TP up + SL down for LONG; mirror for SHORT).
- MFE/MAE direction (LONG: MFE up / MAE down; SHORT: mirror).
- Same-bar STOP_BEFORE_TP ambiguity.
- Future return vs. realised barrier ("favourable move but stop hit").

Emits independent verdicts per side so downstream layers can decide
whether to include LONG, SHORT, or both in rule mining.

Hard contract: research-only. No order placement. No barrier mutations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK
from .counterfactual_training_dataset import build_dataset


# Side-specific classifications.
LONG_LEGITIMATE = "legitimate_stop_before_rise"
LONG_BARRIER_INVERTED = "long_barrier_inverted"
LONG_MFE_MAE_FLIPPED = "long_mfe_mae_flipped"
LONG_AMBIGUOUS_SAME_BAR = "long_ambiguous_same_bar"
LONG_POSSIBLE_LABEL_BUG = "possible_long_label_bug"
LONG_NO_ISSUE = "long_no_issue"
LONG_NEEDS_PATH = "long_needs_ohlcv_path"

SHORT_LEGITIMATE = "legitimate_stop_before_drop"
SHORT_BARRIER_INVERTED = "short_barrier_inverted"
SHORT_MFE_MAE_FLIPPED = "short_mfe_mae_flipped"
SHORT_AMBIGUOUS_SAME_BAR = "short_ambiguous_same_bar"
SHORT_POSSIBLE_LABEL_BUG = "possible_short_label_bug"
SHORT_NO_ISSUE = "short_no_issue"
SHORT_NEEDS_PATH = "short_needs_ohlcv_path"

# Side verdicts (mirror V8.2.7 vocabulary).
LONG_SAFE = "LONG_SAFE_TO_USE_FOR_RESEARCH"
LONG_EXCLUDE = "LONG_EXCLUDE_FROM_RULE_MINING"
LONG_BROKEN = "LONG_BROKEN_FIX_REQUIRED"
SHORT_SAFE = "SHORT_SAFE_TO_USE_FOR_RESEARCH"
SHORT_EXCLUDE = "SHORT_EXCLUDE_FROM_RULE_MINING"
SHORT_BROKEN = "SHORT_BROKEN_FIX_REQUIRED"

# Verdict thresholds.
SAFE_SUSPICIOUS_MAX = 0.10
SAFE_SIGN_BUG_MAX = 0.03
SAFE_BARRIER_BUG_MAX = 0.03
BROKEN_SIGN_BUG_MIN = 0.10
BROKEN_BARRIER_BUG_MIN = 0.10
BROKEN_SUSPICIOUS_MIN = 0.40


@dataclass
class BarrierAuditCase:
    signal_id: Any
    timestamp: str
    symbol: str
    side: str
    entry_price: float
    tp_price: float
    sl_price: float
    ret_1h_pct: float | None
    ret_4h_pct: float | None
    mfe_pct: float | None
    mae_pct: float | None
    first_barrier_hit: str | None
    classification: str
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SideAuditMetrics:
    side: str
    total_rows: int = 0
    evaluable_rows: int = 0
    legitimate_stop_before_move: int = 0
    barrier_bug: int = 0
    sign_bug: int = 0
    mfe_mae_flipped: int = 0
    same_bar_ambiguous: int = 0
    needs_path: int = 0
    no_issue: int = 0
    suspicious_ratio: float = 0.0
    sign_bug_ratio: float = 0.0
    barrier_bug_ratio: float = 0.0
    same_bar_ratio: float = 0.0
    legitimate_stop_before_move_ratio: float = 0.0
    verdict: str = LONG_EXCLUDE  # Overridden per side
    examples_top_100: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DualSideBarrierTruthReport:
    hours: int
    generated_at: str
    long_metrics: SideAuditMetrics = field(
        default_factory=lambda: SideAuditMetrics(side="LONG", verdict=LONG_EXCLUDE),
    )
    short_metrics: SideAuditMetrics = field(
        default_factory=lambda: SideAuditMetrics(side="SHORT", verdict=SHORT_EXCLUDE),
    )
    long_verdict: str = LONG_EXCLUDE
    short_verdict: str = SHORT_EXCLUDE
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---- Classification ------------------------------------------------------

def _orientation_ok(side: str, mfe: Any, mae: Any) -> bool:
    """For both LONG and SHORT we expect ``mfe_pct >= 0`` and ``mae_pct <= 0``
    in the side-oriented convention. If either flips sign, the orientation
    is wrong.
    """
    if not isinstance(mfe, (int, float)) or not isinstance(mae, (int, float)):
        return True
    return float(mfe) >= -1e-9 and float(mae) <= 1e-9


def _long_barrier_inverted(entry: float, sl: float, tp: float) -> bool:
    """LONG requires ``sl < entry < tp``. Anything else is inverted."""
    if entry <= 0 or sl <= 0 or tp <= 0:
        return False
    return not (sl < entry and tp > entry)


def _short_barrier_inverted(entry: float, sl: float, tp: float) -> bool:
    """SHORT requires ``tp < entry < sl``. Anything else is inverted."""
    if entry <= 0 or sl <= 0 or tp <= 0:
        return False
    return not (tp < entry and sl > entry)


def _same_bar_ambiguous(mfe: Any, mae: Any) -> bool:
    if not isinstance(mfe, (int, float)) or not isinstance(mae, (int, float)):
        return False
    return float(mfe) < 0.10 and abs(float(mae)) > 0.30


def _classify_long(row: dict[str, Any]) -> tuple[str, str]:
    first_barrier = str(row.get("first_barrier_hit") or "")
    ret_4h = row.get("ret_4h_pct")
    mfe = row.get("mfe_pct")
    mae = row.get("mae_pct")
    entry = float(row.get("entry_price") or 0)
    tp = float(row.get("take_profit_1") or row.get("tp_price") or 0)
    sl = float(row.get("stop_loss") or row.get("sl_price") or 0)
    notes_parts: list[str] = []

    if entry > 0 and tp > 0 and sl > 0 and _long_barrier_inverted(entry, sl, tp):
        notes_parts.append("long_barrier_levels_inverted")
        return LONG_BARRIER_INVERTED, " ; ".join(notes_parts)

    if not _orientation_ok("LONG", mfe, mae):
        notes_parts.append("mfe_mae_orientation_flipped")
        return LONG_MFE_MAE_FLIPPED, " ; ".join(notes_parts)

    if first_barrier != "SL":
        return LONG_NO_ISSUE, ""

    if ret_4h is None:
        return LONG_NEEDS_PATH, "missing_ret_4h"
    try:
        ret_4h_f = float(ret_4h)
    except Exception:
        return LONG_NEEDS_PATH, "ret_4h_not_numeric"
    # For LONG, favourable future = positive ret. SL hit + ret_4h strongly
    # positive → suspicious.
    if ret_4h_f <= 0.50:
        return LONG_NO_ISSUE, ""

    if _same_bar_ambiguous(mfe, mae):
        return LONG_AMBIGUOUS_SAME_BAR, "same_bar_stop_before_tp_likely"
    if (
        isinstance(mfe, (int, float)) and isinstance(mae, (int, float))
        and float(mfe) > abs(float(mae))
    ):
        return LONG_LEGITIMATE, "stop_then_rise_continuation"
    return LONG_POSSIBLE_LABEL_BUG, "ret_4h_favourable_but_SL"


def _classify_short(row: dict[str, Any]) -> tuple[str, str]:
    first_barrier = str(row.get("first_barrier_hit") or "")
    ret_4h = row.get("ret_4h_pct")
    mfe = row.get("mfe_pct")
    mae = row.get("mae_pct")
    entry = float(row.get("entry_price") or 0)
    tp = float(row.get("take_profit_1") or row.get("tp_price") or 0)
    sl = float(row.get("stop_loss") or row.get("sl_price") or 0)
    notes_parts: list[str] = []

    if entry > 0 and tp > 0 and sl > 0 and _short_barrier_inverted(entry, sl, tp):
        notes_parts.append("short_barrier_levels_inverted")
        return SHORT_BARRIER_INVERTED, " ; ".join(notes_parts)

    if not _orientation_ok("SHORT", mfe, mae):
        notes_parts.append("mfe_mae_orientation_flipped")
        return SHORT_MFE_MAE_FLIPPED, " ; ".join(notes_parts)

    if first_barrier != "SL":
        return SHORT_NO_ISSUE, ""

    if ret_4h is None:
        return SHORT_NEEDS_PATH, "missing_ret_4h"
    try:
        ret_4h_f = float(ret_4h)
    except Exception:
        return SHORT_NEEDS_PATH, "ret_4h_not_numeric"
    if ret_4h_f >= -0.50:
        return SHORT_NO_ISSUE, ""

    if _same_bar_ambiguous(mfe, mae):
        return SHORT_AMBIGUOUS_SAME_BAR, "same_bar_stop_before_tp_likely"
    if (
        isinstance(mfe, (int, float)) and isinstance(mae, (int, float))
        and float(mfe) > abs(float(mae))
    ):
        return SHORT_LEGITIMATE, "stop_then_drop_continuation"
    return SHORT_POSSIBLE_LABEL_BUG, "ret_4h_favourable_but_SL"


def _decide_verdict(
    *, suspicious_ratio: float, sign_bug_ratio: float,
    barrier_bug_ratio: float, side: str,
) -> str:
    safe = LONG_SAFE if side == "LONG" else SHORT_SAFE
    exclude = LONG_EXCLUDE if side == "LONG" else SHORT_EXCLUDE
    broken = LONG_BROKEN if side == "LONG" else SHORT_BROKEN
    if sign_bug_ratio >= BROKEN_SIGN_BUG_MIN:
        return broken
    if barrier_bug_ratio >= BROKEN_BARRIER_BUG_MIN:
        return broken
    if suspicious_ratio >= BROKEN_SUSPICIOUS_MIN:
        return broken
    if (
        suspicious_ratio < SAFE_SUSPICIOUS_MAX
        and sign_bug_ratio < SAFE_SIGN_BUG_MAX
        and barrier_bug_ratio < SAFE_BARRIER_BUG_MAX
    ):
        return safe
    return exclude


def _audit_side(rows: list[dict[str, Any]], side: str) -> SideAuditMetrics:
    metrics = SideAuditMetrics(
        side=side,
        verdict=LONG_EXCLUDE if side == "LONG" else SHORT_EXCLUDE,
    )
    side_rows = [r for r in rows if str(r.get("side", "")).upper() == side]
    metrics.total_rows = len(side_rows)
    evaluable = [r for r in side_rows if r.get("baseline_net_pnl_est") is not None]
    metrics.evaluable_rows = len(evaluable)
    if not evaluable:
        return metrics
    classifier = _classify_long if side == "LONG" else _classify_short
    cases: list[BarrierAuditCase] = []
    for r in evaluable:
        classification, notes = classifier(r)
        if side == "LONG":
            if classification == LONG_NO_ISSUE:
                metrics.no_issue += 1
                continue
            if classification == LONG_LEGITIMATE:
                metrics.legitimate_stop_before_move += 1
            elif classification == LONG_BARRIER_INVERTED:
                metrics.barrier_bug += 1
            elif classification == LONG_MFE_MAE_FLIPPED:
                metrics.mfe_mae_flipped += 1
            elif classification == LONG_AMBIGUOUS_SAME_BAR:
                metrics.same_bar_ambiguous += 1
            elif classification == LONG_POSSIBLE_LABEL_BUG:
                metrics.sign_bug += 1
            elif classification == LONG_NEEDS_PATH:
                metrics.needs_path += 1
        else:
            if classification == SHORT_NO_ISSUE:
                metrics.no_issue += 1
                continue
            if classification == SHORT_LEGITIMATE:
                metrics.legitimate_stop_before_move += 1
            elif classification == SHORT_BARRIER_INVERTED:
                metrics.barrier_bug += 1
            elif classification == SHORT_MFE_MAE_FLIPPED:
                metrics.mfe_mae_flipped += 1
            elif classification == SHORT_AMBIGUOUS_SAME_BAR:
                metrics.same_bar_ambiguous += 1
            elif classification == SHORT_POSSIBLE_LABEL_BUG:
                metrics.sign_bug += 1
            elif classification == SHORT_NEEDS_PATH:
                metrics.needs_path += 1
        try:
            entry = float(r.get("entry_price") or 0)
            tp = float(r.get("take_profit_1") or r.get("tp_price") or 0)
            sl = float(r.get("stop_loss") or r.get("sl_price") or 0)
        except Exception:
            entry, tp, sl = 0.0, 0.0, 0.0
        cases.append(BarrierAuditCase(
            signal_id=r.get("signal_id"),
            timestamp=str(r.get("timestamp", "")),
            symbol=str(r.get("symbol", "")),
            side=side,
            entry_price=entry,
            tp_price=tp,
            sl_price=sl,
            ret_1h_pct=r.get("ret_1h_pct"),
            ret_4h_pct=r.get("ret_4h_pct"),
            mfe_pct=r.get("mfe_pct"),
            mae_pct=r.get("mae_pct"),
            first_barrier_hit=r.get("first_barrier_hit"),
            classification=classification,
            notes=notes,
        ))
    n = max(metrics.evaluable_rows, 1)
    suspicious = (
        metrics.sign_bug + metrics.barrier_bug
        + metrics.same_bar_ambiguous + metrics.needs_path
        + metrics.mfe_mae_flipped
    )
    metrics.suspicious_ratio = suspicious / n
    metrics.sign_bug_ratio = metrics.sign_bug / n
    metrics.barrier_bug_ratio = metrics.barrier_bug / n
    metrics.same_bar_ratio = metrics.same_bar_ambiguous / n
    metrics.legitimate_stop_before_move_ratio = metrics.legitimate_stop_before_move / n
    metrics.verdict = _decide_verdict(
        suspicious_ratio=metrics.suspicious_ratio,
        sign_bug_ratio=metrics.sign_bug_ratio,
        barrier_bug_ratio=metrics.barrier_bug_ratio,
        side=side,
    )
    metrics.examples_top_100 = [c.as_dict() for c in cases[:100]]
    return metrics


def audit_dual_side_barriers(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    rows: Iterable[dict[str, Any]] | None = None,
) -> DualSideBarrierTruthReport:
    """Run the dual-side barrier truth audit."""
    report = DualSideBarrierTruthReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    if not dataset:
        return report
    report.long_metrics = _audit_side(dataset, "LONG")
    report.short_metrics = _audit_side(dataset, "SHORT")
    report.long_verdict = report.long_metrics.verdict
    report.short_verdict = report.short_metrics.verdict
    if report.long_metrics.evaluable_rows or report.short_metrics.evaluable_rows:
        report.status = STATUS_OK
    return report
