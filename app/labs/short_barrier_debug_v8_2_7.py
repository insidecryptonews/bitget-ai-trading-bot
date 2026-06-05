"""V8.2.7 — Short Barrier Debug with ratio-based verdict (research-only).

Fixes the V8.2.6 ``_decide_verdict`` formula that mixed counts and
percentages, potentially over-excluding SHORT. V8.2.7 uses pure ratios.

Hard contract: research-only.
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
from .short_barrier_debug_v8_2_6 import (
    SHORT_BROKEN,
    SHORT_EXCLUDE,
    SHORT_SAFE,
    ShortDebugCase,
    _barrier_inverted_for_short,
    _orientation_ok_for_short,
    _same_bar_suspected,
)
from .short_sign_barrier_audit import (
    CLASS_AMBIGUOUS_SAME_BAR,
    CLASS_BARRIER_ORDER_BUG,
    CLASS_LEGITIMATE,
    CLASS_NEEDS_PATH,
    CLASS_NO_ISSUE,
    CLASS_SHORT_SIGN_BUG,
    classify_short,
)


# Ratio thresholds.
SAFE_SUSPICIOUS_MAX = 0.10
SAFE_SIGN_BUG_MAX = 0.03
SAFE_BARRIER_BUG_MAX = 0.03
BROKEN_SIGN_BUG_MIN = 0.10
BROKEN_BARRIER_BUG_MIN = 0.10
BROKEN_SUSPICIOUS_MIN = 0.40


@dataclass
class ShortBarrierDebugReportV2:
    hours: int
    generated_at: str
    total_short_rows: int = 0
    evaluable_short_rows: int = 0
    trusted_count: int = 0
    legitimate_stop_before_drop: int = 0
    possible_sign_bug: int = 0
    possible_barrier_bug: int = 0
    same_bar_ambiguous: int = 0
    needs_path: int = 0
    suspicious_ratio: float = 0.0
    sign_bug_ratio: float = 0.0
    barrier_bug_ratio: float = 0.0
    same_bar_ratio: float = 0.0
    examples_top_100: list[dict[str, Any]] = field(default_factory=list)
    verdict: str = SHORT_EXCLUDE
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_verdict_v827(
    *,
    suspicious_ratio: float,
    sign_bug_ratio: float,
    barrier_bug_ratio: float,
    same_bar_ratio: float,
) -> str:
    """V8.2.7 verdict using pure ratios."""
    # BROKEN gates take priority — a single significant bug class can break
    # SHORT regardless of overall suspicious count.
    if sign_bug_ratio >= BROKEN_SIGN_BUG_MIN:
        return SHORT_BROKEN
    if barrier_bug_ratio >= BROKEN_BARRIER_BUG_MIN:
        return SHORT_BROKEN
    if suspicious_ratio >= BROKEN_SUSPICIOUS_MIN:
        return SHORT_BROKEN
    # SAFE: low overall AND very low specific bug classes.
    if (
        suspicious_ratio < SAFE_SUSPICIOUS_MAX
        and sign_bug_ratio < SAFE_SIGN_BUG_MAX
        and barrier_bug_ratio < SAFE_BARRIER_BUG_MAX
    ):
        return SHORT_SAFE
    # Anything in between → EXCLUDE.
    return SHORT_EXCLUDE


def debug_short_barriers_v827(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    rows: Iterable[dict[str, Any]] | None = None,
) -> ShortBarrierDebugReportV2:
    report = ShortBarrierDebugReportV2(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    short_rows = [r for r in dataset if str(r.get("side", "")).upper() == SIDE_SHORT]
    report.total_short_rows = len(short_rows)
    evaluable = [r for r in short_rows if r.get("baseline_net_pnl_est") is not None]
    report.evaluable_short_rows = len(evaluable)
    if not evaluable:
        report.status = STATUS_NEED_DATA
        return report
    counts = {
        "trusted": 0, "legitimate": 0, "sign_bug": 0,
        "barrier_bug": 0, "same_bar": 0, "needs_path": 0,
    }
    cases: list[ShortDebugCase] = []
    for r in evaluable:
        classification, base_notes = classify_short(r)
        try:
            entry = float(r.get("entry_price") or 0)
        except Exception:
            entry = 0.0
        stop_loss = 0.0
        take_profit_1 = 0.0
        take_profit_2 = 0.0
        for key in ("stop_loss", "take_profit_1", "take_profit_2"):
            try:
                if r.get(key) is not None:
                    if key == "stop_loss":
                        stop_loss = float(r[key])
                    elif key == "take_profit_1":
                        take_profit_1 = float(r[key])
                    else:
                        take_profit_2 = float(r[key])
            except Exception:
                pass
        barrier_inverted = (
            _barrier_inverted_for_short(entry, stop_loss, take_profit_1)
            if (entry > 0 and stop_loss > 0 and take_profit_1 > 0)
            else False
        )
        mfe = r.get("mfe_pct")
        mae = r.get("mae_pct")
        orientation_ok = _orientation_ok_for_short(mfe, mae)
        same_bar = _same_bar_suspected(mfe, mae)
        notes_parts = [base_notes] if base_notes else []
        if barrier_inverted:
            notes_parts.append("barrier_levels_appear_inverted")
        if not orientation_ok:
            notes_parts.append("mfe_mae_orientation_flipped")
        if same_bar:
            notes_parts.append("tiny_mfe_significant_mae_same_bar")
        if classification == CLASS_NO_ISSUE:
            counts["trusted"] += 1
            continue
        if classification == CLASS_LEGITIMATE:
            counts["legitimate"] += 1
        elif classification == CLASS_SHORT_SIGN_BUG:
            counts["sign_bug"] += 1
        elif classification == CLASS_BARRIER_ORDER_BUG:
            counts["barrier_bug"] += 1
        elif classification == CLASS_AMBIGUOUS_SAME_BAR:
            counts["same_bar"] += 1
        elif classification == CLASS_NEEDS_PATH:
            counts["needs_path"] += 1
        cases.append(ShortDebugCase(
            signal_id=r.get("signal_id"),
            timestamp=str(r.get("timestamp", "")),
            symbol=str(r.get("symbol", "")),
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit_1=take_profit_1,
            take_profit_2=take_profit_2,
            ret_1h_pct=r.get("ret_1h_pct"),
            ret_4h_pct=r.get("ret_4h_pct"),
            ret_24h_pct=r.get("ret_24h_pct"),
            mfe_pct=mfe,
            mae_pct=mae,
            first_barrier_hit=r.get("first_barrier_hit"),
            classification=classification,
            barrier_inverted=barrier_inverted,
            mfe_mae_orientation_ok=orientation_ok,
            same_bar_suspected=same_bar,
            notes=" ; ".join(notes_parts),
        ))
    report.trusted_count = counts["trusted"]
    report.legitimate_stop_before_drop = counts["legitimate"]
    report.possible_sign_bug = counts["sign_bug"]
    report.possible_barrier_bug = counts["barrier_bug"]
    report.same_bar_ambiguous = counts["same_bar"]
    report.needs_path = counts["needs_path"]

    n = max(report.evaluable_short_rows, 1)
    suspicious = (
        counts["sign_bug"] + counts["barrier_bug"]
        + counts["same_bar"] + counts["needs_path"]
    )
    report.suspicious_ratio = suspicious / n
    report.sign_bug_ratio = counts["sign_bug"] / n
    report.barrier_bug_ratio = counts["barrier_bug"] / n
    report.same_bar_ratio = counts["same_bar"] / n
    report.verdict = decide_verdict_v827(
        suspicious_ratio=report.suspicious_ratio,
        sign_bug_ratio=report.sign_bug_ratio,
        barrier_bug_ratio=report.barrier_bug_ratio,
        same_bar_ratio=report.same_bar_ratio,
    )
    report.examples_top_100 = [c.as_dict() for c in cases[:100]]
    report.status = STATUS_OK
    return report
