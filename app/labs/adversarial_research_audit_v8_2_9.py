"""V8.2.9 — Adversarial Research Audit (research-only).

Audits V8.2.9 outputs for common failure modes before any ResearchOps
report is presented as evidence of edge. Checks include lookahead,
duplicate contamination, overfit, score misuse, exit-policy lookahead,
and operational safety.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK


AUDIT_PASS = "PASS_RESEARCH_ONLY"
AUDIT_FAIL_LOOKAHEAD = "FAIL_LOOKAHEAD"
AUDIT_FAIL_DUPLICATES = "FAIL_DUPLICATES"
AUDIT_FAIL_OVERFIT = "FAIL_OVERFIT"
AUDIT_FAIL_SCORE_MISUSE = "FAIL_SCORE_MISUSE"
AUDIT_FAIL_EXIT_LOOKAHEAD = "FAIL_EXIT_LOOKAHEAD"
AUDIT_FAIL_SAFETY = "FAIL_SAFETY"

LOOKAHEAD_FIELDS: frozenset[str] = frozenset({
    "ret_15m_pct", "ret_30m_pct", "ret_1h_pct", "ret_4h_pct", "ret_24h_pct",
    "mfe_pct", "mae_pct",
    "first_barrier_hit", "tp_before_sl", "sl_before_tp",
    "baseline_result", "baseline_gross_pnl", "baseline_net_pnl_est",
    "training_label",
})

DUPLICATE_RATIO_MAX = 0.30


@dataclass
class AdversarialFinding:
    category: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AdversarialAuditReport:
    hours: int
    generated_at: str
    audit_status: str = AUDIT_PASS
    findings: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    score_anti_calibrated: bool = True
    score_used_as_gate: bool = False
    duplicate_ratio_after: float = 0.0
    single_symbol_concentration: float = 0.0
    single_cluster_concentration: float = 0.0
    exit_policy_used_future_returns: bool = False
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _strings_in_function_body(fn: Callable[..., Any]) -> list[str]:
    """Return string literals reachable from ``fn``'s body, excluding the
    function-level docstring (so documenting forbidden field names in a
    docstring is allowed)."""
    try:
        src = inspect.getsource(fn)
    except OSError:
        return []
    tree = ast.parse(src)
    if not tree.body:
        return []
    func = tree.body[0]
    body = list(getattr(func, "body", []))
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    out: list[str] = []
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.append(node.value)
    return out


def _detector_uses_lookahead_fields() -> tuple[bool, list[str]]:
    """Scan the V8.2.9 prefix-only LONG rebound detector for lookahead
    field reads. Returns ``(violated, offending_fields)``."""
    from .rebound_long_candidate_extractor_v8_2_9 import (
        _build_prefix_context,
        detect_rebound_long_prefix_only,
    )
    literals = (
        _strings_in_function_body(detect_rebound_long_prefix_only)
        + _strings_in_function_body(_build_prefix_context)
    )
    offending = [s for s in literals if s in LOOKAHEAD_FIELDS]
    return (bool(offending), offending)


def audit_v829(
    *,
    hours: int = 168,
    score_anti_calibrated: bool = True,
    score_used_as_gate: bool = False,
    duplicate_ratio_after: float = 0.0,
    single_symbol_concentration: float = 0.0,
    single_cluster_concentration: float = 0.0,
    paper_filter_enabled: bool = False,
    can_send_real_orders: bool = False,
    live_trading: bool = False,
    paper_sandbox_candidates_count: int = 0,
    test_net_ev_after_stress_pct: float = 0.0,
    exit_policy_used_future_returns: bool = False,
    exit_policy_selected_on_test: bool = False,
    same_bar_resolution_conservative: bool = True,
) -> AdversarialAuditReport:
    """Run the V8.2.9 adversarial audit and return a structured report."""
    report = AdversarialAuditReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
        score_anti_calibrated=bool(score_anti_calibrated),
        score_used_as_gate=bool(score_used_as_gate),
        duplicate_ratio_after=float(duplicate_ratio_after),
        single_symbol_concentration=float(single_symbol_concentration),
        single_cluster_concentration=float(single_cluster_concentration),
        exit_policy_used_future_returns=bool(exit_policy_used_future_returns),
    )
    findings: list[AdversarialFinding] = []
    blockers: list[str] = []
    # 1. Lookahead audit.
    lookahead, offending = _detector_uses_lookahead_fields()
    if lookahead:
        findings.append(AdversarialFinding(
            "lookahead",
            f"prefix-only detector references forbidden fields: {offending}",
        ))
        blockers.append(AUDIT_FAIL_LOOKAHEAD)
    # 2. Duplicate contamination.
    if duplicate_ratio_after > DUPLICATE_RATIO_MAX:
        findings.append(AdversarialFinding(
            "duplicate_contamination",
            f"duplicate_ratio_after={duplicate_ratio_after:.4f} "
            f"above max {DUPLICATE_RATIO_MAX}",
        ))
        blockers.append(AUDIT_FAIL_DUPLICATES)
    # 3. Score misuse.
    if score_anti_calibrated and score_used_as_gate:
        findings.append(AdversarialFinding(
            "score_misuse",
            "score is anti-calibrated but used as a positive gate",
        ))
        blockers.append(AUDIT_FAIL_SCORE_MISUSE)
    # 4. Exit policy lookahead / misuse.
    if exit_policy_used_future_returns:
        findings.append(AdversarialFinding(
            "exit_policy_lookahead",
            "exit policy reads forward-return columns as detection inputs",
        ))
        blockers.append(AUDIT_FAIL_EXIT_LOOKAHEAD)
    if exit_policy_selected_on_test:
        findings.append(AdversarialFinding(
            "exit_policy_lookahead",
            "exit policy was selected by inspecting the test slice",
        ))
        blockers.append(AUDIT_FAIL_EXIT_LOOKAHEAD)
    if not same_bar_resolution_conservative:
        findings.append(AdversarialFinding(
            "exit_policy_lookahead",
            "same-bar ambiguity not resolved conservatively (STOP_BEFORE_TP)",
        ))
        blockers.append(AUDIT_FAIL_EXIT_LOOKAHEAD)
    # 5. Operational safety.
    if paper_filter_enabled or can_send_real_orders or live_trading:
        findings.append(AdversarialFinding(
            "operational_safety",
            f"safety flags violated: paper_filter_enabled={paper_filter_enabled} "
            f"can_send_real_orders={can_send_real_orders} live={live_trading}",
        ))
        blockers.append(AUDIT_FAIL_SAFETY)
    # 6. Overfit hint: paper sandbox emerged but failed stress / concentration.
    if (
        paper_sandbox_candidates_count > 0
        and (
            single_symbol_concentration > 0.50
            or single_cluster_concentration > 0.30
            or test_net_ev_after_stress_pct <= 0
        )
    ):
        findings.append(AdversarialFinding(
            "overfit",
            "paper sandbox candidate emerged but failed concentration / "
            "stress check",
        ))
        blockers.append(AUDIT_FAIL_OVERFIT)
    report.findings = [f.as_dict() for f in findings]
    # Deduplicate blockers while preserving order.
    seen: set[str] = set()
    unique_blockers: list[str] = []
    for b in blockers:
        if b not in seen:
            seen.add(b)
            unique_blockers.append(b)
    report.blockers = unique_blockers
    if not unique_blockers:
        report.audit_status = AUDIT_PASS
    else:
        for b in (
            AUDIT_FAIL_SAFETY, AUDIT_FAIL_LOOKAHEAD,
            AUDIT_FAIL_EXIT_LOOKAHEAD, AUDIT_FAIL_SCORE_MISUSE,
            AUDIT_FAIL_DUPLICATES, AUDIT_FAIL_OVERFIT,
        ):
            if b in unique_blockers:
                report.audit_status = b
                break
    report.status = STATUS_OK
    return report
