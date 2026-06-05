"""V8.2.6 — Short Barrier Debug (research-only).

Investigates ``SHORT_LABELS_SUSPECT`` cases to confirm whether the labels
are trustworthy. Issues a verdict that controls whether SHORT groups can be
mined by the rule miner.

Hard contract: research-only. Never opens orders.
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
from .short_sign_barrier_audit import (
    CLASS_AMBIGUOUS_SAME_BAR,
    CLASS_BARRIER_ORDER_BUG,
    CLASS_LEGITIMATE,
    CLASS_NEEDS_PATH,
    CLASS_NO_ISSUE,
    CLASS_SHORT_SIGN_BUG,
    audit_short_sign,
    classify_short,
)


SHORT_SAFE = "SHORT_SAFE_TO_USE_FOR_RESEARCH"
SHORT_EXCLUDE = "SHORT_EXCLUDE_FROM_RULE_MINING"
SHORT_BROKEN = "SHORT_BROKEN_FIX_REQUIRED"


@dataclass
class ShortDebugCase:
    signal_id: Any
    timestamp: str
    symbol: str
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    ret_1h_pct: float | None
    ret_4h_pct: float | None
    ret_24h_pct: float | None
    mfe_pct: float | None
    mae_pct: float | None
    first_barrier_hit: str | None
    classification: str
    barrier_inverted: bool
    mfe_mae_orientation_ok: bool
    same_bar_suspected: bool
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ShortBarrierDebugReport:
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
    examples_top_100: list[dict[str, Any]] = field(default_factory=list)
    verdict: str = SHORT_EXCLUDE
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _orientation_ok_for_short(mfe: Any, mae: Any) -> bool:
    """For SHORT in our convention, ``mfe_pct`` is the magnitude of the
    favourable side-oriented move (≥ 0) and ``mae_pct`` is the adverse one
    (≤ 0). If both flip sign, orientation is wrong.
    """
    if not isinstance(mfe, (int, float)) or not isinstance(mae, (int, float)):
        return True  # cannot tell
    return float(mfe) >= -1e-9 and float(mae) <= 1e-9


def _barrier_inverted_for_short(entry: float, stop: float, tp1: float) -> bool:
    """For SHORT: stop > entry and tp1 < entry. If reversed, barriers are
    inverted.
    """
    if entry <= 0 or stop <= 0 or tp1 <= 0:
        return False
    return not (stop > entry and tp1 < entry)


def _same_bar_suspected(mfe: Any, mae: Any) -> bool:
    if not isinstance(mfe, (int, float)) or not isinstance(mae, (int, float)):
        return False
    # Tiny MFE + significant MAE: stop hit before any meaningful favourable move.
    return float(mfe) < 0.10 and abs(float(mae)) > 0.30


def _decide_verdict(
    suspicious_ratio: float,
    legitimate_ratio: float,
    barrier_bug_count: int,
    sign_bug_count: int,
) -> str:
    if suspicious_ratio < 0.10 and barrier_bug_count == 0 and sign_bug_count == 0:
        return SHORT_SAFE
    if barrier_bug_count > 0 and (barrier_bug_count / max(suspicious_ratio * 100, 1)) > 0.50:
        return SHORT_BROKEN
    if sign_bug_count > 0 and (sign_bug_count / max(suspicious_ratio * 100, 1)) > 0.50:
        return SHORT_BROKEN
    return SHORT_EXCLUDE


def debug_short_barriers(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    rows: Iterable[dict[str, Any]] | None = None,
) -> ShortBarrierDebugReport:
    """Run the V8.2.6 short barrier debug.

    Inputs are the V8.2.4 dataset rows; this function does not query OHLCV
    independently — it reuses the bar-derived fields already in the row
    (``mfe_pct``, ``mae_pct``, ``ret_1h_pct``...). For deeper bar-by-bar
    inspection callers can plug their own bar reconstructor downstream.
    """
    report = ShortBarrierDebugReport(
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
    cases: list[ShortDebugCase] = []
    counts = {
        "trusted": 0, "legitimate": 0, "sign_bug": 0,
        "barrier_bug": 0, "same_bar": 0, "needs_path": 0,
    }
    for r in evaluable:
        classification, base_notes = classify_short(r)
        # Independent orientation / barrier checks.
        try:
            entry = float(r.get("entry_price") or 0)
        except Exception:
            entry = 0.0
        stop_loss = 0.0
        take_profit_1 = 0.0
        take_profit_2 = 0.0
        # Optional fields not in the V8.2.4 dataset; tolerate absence.
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
    suspicious = (
        counts["sign_bug"] + counts["barrier_bug"]
        + counts["same_bar"] + counts["needs_path"]
    )
    suspicious_ratio = suspicious / max(report.evaluable_short_rows, 1)
    report.verdict = _decide_verdict(
        suspicious_ratio=suspicious_ratio,
        legitimate_ratio=counts["legitimate"] / max(report.evaluable_short_rows, 1),
        barrier_bug_count=counts["barrier_bug"],
        sign_bug_count=counts["sign_bug"],
    )
    report.examples_top_100 = [c.as_dict() for c in cases[:100]]
    report.status = STATUS_OK
    return report
