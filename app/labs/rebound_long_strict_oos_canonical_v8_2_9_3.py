"""V8.2.9.3 — Rebound LONG Strict OOS with Canonical Outcome.

Re-runs the V8.2.9.2 strict OOS but using the canonicalised outcome
from ``outcome_field_canonicalizer_v8_2_9_3`` as ``net_pnl_est`` per
candidate. Also gates the result by ``sign_bug_ratio`` so a rule
cannot earn ``PAPER_SANDBOX_CANDIDATE`` while the underlying outcome
labels are suspect.

Hard contract: research-only. Same forbidden-feature whitelist as the
V8.2.9.2 strict OOS.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK
from .outcome_field_canonicalizer_v8_2_9_3 import (
    CANONICAL_STATUS_OK,
    canonicalize_row,
)
from .rebound_long_strict_oos_v8_2_9 import (
    EX_ANTE_FEATURES,
    MIN_TRAIN_SAMPLES,
    MIN_VAL_SAMPLES,
    MIN_TEST_SAMPLES,
    STATUS_NEED_MORE_DATA,
    STATUS_PAPER_SANDBOX_CANDIDATE,
    STATUS_REJECT,
    STATUS_RESEARCH_CANDIDATE,
    STATUS_SINGLE_SYMBOL_RESEARCH_ONLY,
    STATUS_WATCH_ONLY,
    run_strict_oos_rebound,
)


# Sign-bug ratio above which strict OOS canonical refuses to promote.
MAX_SIGN_BUG_RATIO = 0.05
# Canonical OK ratio required to operate.
MIN_CANONICAL_OK_RATIO = 0.30


@dataclass
class StrictOosCanonicalReport:
    hours: int
    generated_at: str
    candidates_input: int = 0
    candidates_with_canonical_ok: int = 0
    canonical_ok_ratio: float = 0.0
    sign_bug_ratio: float = 0.0
    final_status_top_level: str = STATUS_NEED_MORE_DATA
    duplicate_ratio_after: float = 0.0
    strict_oos_input_is_deduped: bool = False
    paper_sandbox_candidates: list[dict[str, Any]] = field(default_factory=list)
    research_candidates: list[dict[str, Any]] = field(default_factory=list)
    watch_only: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    need_more_data: list[dict[str, Any]] = field(default_factory=list)
    by_final_status: dict[str, int] = field(default_factory=dict)
    rejected_for_sign_bug: bool = False
    rejected_for_canonical_insufficient: bool = False
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _canonicalise_for_oos(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Return ``(canonical_candidates, canonical_ok_count, sign_bug_count)``."""
    canonical_candidates: list[dict[str, Any]] = []
    ok_count = 0
    sign_bug_count = 0
    for c in candidates:
        canonical = canonicalize_row(c)
        if (
            canonical.canonical_outcome_status == CANONICAL_STATUS_OK
            and isinstance(canonical.canonical_net_pnl_est, (int, float))
        ):
            ok_count += 1
            replaced = dict(c)
            replaced["net_pnl_est"] = float(canonical.canonical_net_pnl_est)
            replaced["canonical_source"] = canonical.canonical_source
            canonical_candidates.append(replaced)
        elif canonical.canonical_outcome_status == "SIGN_SUSPECT":
            sign_bug_count += 1
            replaced = dict(c)
            replaced["net_pnl_est"] = float(canonical.canonical_net_pnl_est or 0.0)
            replaced["canonical_source"] = canonical.canonical_source
            replaced["sign_suspect"] = True
            canonical_candidates.append(replaced)
    return canonical_candidates, ok_count, sign_bug_count


def run_strict_oos_canonical(
    candidates: Iterable[dict[str, Any]] | None = None,
    *,
    hours: int = 168,
    score_anti_calibrated: bool = True,
    duplicate_ratio_after: float = 0.0,
    input_is_deduped: bool = True,
    grouping_features: Iterable[str] = EX_ANTE_FEATURES,
) -> StrictOosCanonicalReport:
    """Run strict OOS using canonicalised outcomes + sign-bug gate."""
    report = StrictOosCanonicalReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
        duplicate_ratio_after=float(duplicate_ratio_after),
        strict_oos_input_is_deduped=bool(input_is_deduped),
    )
    candidate_list = list(candidates or [])
    report.candidates_input = len(candidate_list)
    if not candidate_list:
        return report
    canonical_candidates, ok_count, sign_bug_count = _canonicalise_for_oos(
        candidate_list,
    )
    report.candidates_with_canonical_ok = ok_count
    report.canonical_ok_ratio = ok_count / max(len(candidate_list), 1)
    report.sign_bug_ratio = sign_bug_count / max(len(candidate_list), 1)

    # Sign-bug ratio above threshold → REJECT outright. Checked before
    # canonical-OK so a dataset full of sign-suspect rows is rejected as
    # data-quality failure rather than masked as NEED_MORE_DATA.
    if report.sign_bug_ratio > MAX_SIGN_BUG_RATIO:
        report.final_status_top_level = STATUS_REJECT
        report.rejected_for_sign_bug = True
        report.status = STATUS_OK
        return report
    # Insufficient canonical OK → NEED_MORE_DATA.
    if report.canonical_ok_ratio < MIN_CANONICAL_OK_RATIO:
        report.final_status_top_level = STATUS_NEED_MORE_DATA
        report.rejected_for_canonical_insufficient = True
        report.status = STATUS_OK
        return report

    inner = run_strict_oos_rebound(
        canonical_candidates,
        hours=hours,
        score_anti_calibrated=bool(score_anti_calibrated),
        grouping_features=grouping_features,
        duplicate_ratio_after=duplicate_ratio_after,
        input_is_deduped=bool(input_is_deduped),
    )
    report.paper_sandbox_candidates = inner.paper_sandbox_candidates
    report.research_candidates = inner.research_candidates
    report.watch_only = inner.watch_only
    report.rejected = inner.rejected
    report.need_more_data = inner.need_more_data
    report.by_final_status = inner.by_final_status
    # Propagate top-level status.
    if inner.paper_sandbox_candidates:
        report.final_status_top_level = STATUS_PAPER_SANDBOX_CANDIDATE
    elif inner.research_candidates:
        report.final_status_top_level = STATUS_RESEARCH_CANDIDATE
    elif inner.watch_only:
        report.final_status_top_level = STATUS_WATCH_ONLY
    elif inner.rejected:
        report.final_status_top_level = STATUS_REJECT
    else:
        report.final_status_top_level = STATUS_NEED_MORE_DATA
    report.status = STATUS_OK
    return report
