"""V8.2.7 — Final Rule Gate (research-only).

Single decision layer combining:
- strict OOS selection results
- SHORT verdict (V8.2.7)
- score calibration status
- cost stress
- duplicate / cluster ratios

Emits ``NO_PAPER_CANDIDATES`` explicitly when no rule reaches
``PAPER_SANDBOX_CANDIDATE`` so downstream layers cannot misinterpret an
empty list as "ready for paper".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK
from .counterfactual_dedup_audit import audit_dedup
from .counterfactual_training_dataset import build_dataset
from .score_calibration_audit import audit_score_calibration
from .short_barrier_debug_v8_2_7 import debug_short_barriers_v827
from .strict_oos_rule_selector_v8_2_7 import (
    FINAL_NEED_MORE_DATA,
    FINAL_PAPER_SANDBOX_CANDIDATE,
    FINAL_REJECT,
    FINAL_RESEARCH_CANDIDATE,
    FINAL_WATCH_ONLY,
    select_rules_strict_oos,
)


NO_PAPER_CANDIDATES_MARKER = "NO_PAPER_CANDIDATES"

# V8.2.7.1 — duplicate-ratio hard gate. The V8.2.5 dataset in production
# showed ``duplicate_ratio ≈ 0.8562``; once dedup is applied the metrics
# collapse. We block promotion to PAPER_SANDBOX whenever the underlying
# dataset is still too duplicated to trust the OOS evaluation.
MAX_DUPLICATE_RATIO_FOR_PAPER = 0.30
DUPLICATE_RATIO_GATE_PASS = "PASS"
DUPLICATE_RATIO_GATE_FAIL = "FAIL"
DUPLICATE_RATIO_TOO_HIGH_REASON = "duplicate_ratio_too_high"


@dataclass
class FinalRuleGateReport:
    hours: int
    generated_at: str
    short_verdict: str = ""
    score_monotonicity: str = ""
    duplicate_ratio: float = 0.0
    # V8.2.7.1 — duplicate-ratio gate fields.
    duplicate_ratio_gate: float = MAX_DUPLICATE_RATIO_FOR_PAPER
    duplicate_ratio_gate_status: str = DUPLICATE_RATIO_GATE_PASS
    total_rules_mined: int = 0
    rejected: int = 0
    watch_only: int = 0
    research_candidates: int = 0
    paper_sandbox_candidates: int = 0
    need_more_data: int = 0
    reasons_top: list[dict[str, Any]] = field(default_factory=list)
    no_paper_candidates_marker: str = ""
    paper_sandbox_rules: list[dict[str, Any]] = field(default_factory=list)
    research_candidate_rules: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _aggregate_reasons(rules: Iterable[dict[str, Any]], top_n: int = 10) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for r in rules:
        reason = str(r.get("reject_reason") or "").strip()
        if not reason:
            continue
        counts[reason] = counts.get(reason, 0) + 1
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    ][: int(top_n)]


def run_final_gate(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    rows: Iterable[dict[str, Any]] | None = None,
) -> FinalRuleGateReport:
    """Run the consolidated V8.2.7 gate. Returns the report only — never
    activates anything in production.
    """
    report = FinalRuleGateReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    if not dataset:
        report.no_paper_candidates_marker = NO_PAPER_CANDIDATES_MARKER
        return report

    short = debug_short_barriers_v827(db, hours=hours, limit=limit, rows=dataset)
    recal = audit_score_calibration(db, hours=hours, limit=limit, rows=dataset)
    dedup = audit_dedup(db, hours=hours, limit=limit, rows=dataset)
    score_ok = recal.monotonicity_status == "PASS"
    selector = select_rules_strict_oos(
        db, hours=hours, limit=limit, rows=dataset,
        short_verdict=short.verdict,
        score_calibration_ok=score_ok,
    )

    report.short_verdict = short.verdict
    report.score_monotonicity = recal.monotonicity_status
    report.duplicate_ratio = dedup.duplicate_ratio
    report.duplicate_ratio_gate = MAX_DUPLICATE_RATIO_FOR_PAPER
    if dedup.duplicate_ratio > MAX_DUPLICATE_RATIO_FOR_PAPER:
        report.duplicate_ratio_gate_status = DUPLICATE_RATIO_GATE_FAIL
    else:
        report.duplicate_ratio_gate_status = DUPLICATE_RATIO_GATE_PASS

    paper_sandbox = list(selector.paper_sandbox_candidates)
    research_candidates = list(selector.research_candidates)

    # V8.2.7.1 hard gate — when duplicate ratio is too high, demote every
    # PAPER_SANDBOX_CANDIDATE down to RESEARCH_CANDIDATE with an explicit
    # reason so the operator sees why. We do NOT delete any rule; the gate
    # only forbids promotion to paper sandbox.
    if report.duplicate_ratio_gate_status == DUPLICATE_RATIO_GATE_FAIL:
        for rule in paper_sandbox:
            rule["final_gate"] = FINAL_RESEARCH_CANDIDATE
            existing_reason = str(rule.get("reject_reason") or "")
            rule["reject_reason"] = (
                f"{existing_reason}|{DUPLICATE_RATIO_TOO_HIGH_REASON}"
                if existing_reason else DUPLICATE_RATIO_TOO_HIGH_REASON
            )
        research_candidates = paper_sandbox + research_candidates
        paper_sandbox = []

    report.total_rules_mined = selector.total_rules_evaluated
    report.rejected = selector.by_final_gate.get(FINAL_REJECT, 0)
    report.watch_only = selector.by_final_gate.get(FINAL_WATCH_ONLY, 0)
    report.research_candidates = len(research_candidates)
    report.paper_sandbox_candidates = len(paper_sandbox)
    report.need_more_data = selector.by_final_gate.get(FINAL_NEED_MORE_DATA, 0)
    report.paper_sandbox_rules = paper_sandbox
    report.research_candidate_rules = research_candidates
    report.reasons_top = _aggregate_reasons(
        selector.rejected_rules + selector.watch_only_rules,
    )
    if report.paper_sandbox_candidates == 0:
        report.no_paper_candidates_marker = NO_PAPER_CANDIDATES_MARKER
    report.status = STATUS_OK
    return report
