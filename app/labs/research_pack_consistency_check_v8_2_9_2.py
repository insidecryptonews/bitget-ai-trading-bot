"""V8.2.9.2 — Research Pack Consistency Check (research-only).

Verifies that the V8.2.9 export pipeline wires the right numbers from
each lab into the next, so the operator cannot read contradictory
metrics (e.g. EdgeGuard dedup reports
``duplicate_ratio_after = 0.00`` while strict OOS reports
``duplicate_ratio_after = 0.89``).

Hard contract: research-only. The check produces a structured report
that the adversarial audit consumes via the ``consistency_check_failed``
flag.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK


CONSISTENCY_PASS = "PASS"
CONSISTENCY_FAIL = "FAIL"


@dataclass
class ConsistencyCheckReport:
    hours: int
    generated_at: str
    consistency_check_status: str = CONSISTENCY_PASS
    consistency_findings: list[str] = field(default_factory=list)
    duplicate_ratio_before: float = 0.0
    duplicate_ratio_after: float = 0.0
    strict_oos_duplicate_ratio_used: float = 0.0
    strict_oos_input_is_deduped: bool = False
    strict_oos_status: str = ""
    paper_sandbox_candidates: int = 0
    adversarial_duplicate_source: str = ""
    adversarial_duplicate_ratio_used: float = 0.0
    exit_oos_status: str = ""
    exit_policy_replay_mode: str = ""
    exit_policy_productive_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _approx_equal(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(float(a) - float(b)) <= tol


def run_consistency_check_v8_2_9_2(
    *,
    hours: int = 168,
    duplicate_ratio_before: float,
    duplicate_ratio_after: float,
    strict_oos_duplicate_ratio_used: float,
    strict_oos_input_is_deduped: bool,
    strict_oos_status: str,
    paper_sandbox_candidates: int,
    adversarial_duplicate_source: str,
    adversarial_duplicate_ratio_used: float,
    exit_oos_status: str,
    exit_policy_replay_mode: str,
    exit_policy_productive_ready: bool,
    final_recommendation: str,
) -> ConsistencyCheckReport:
    """Audit cross-pipeline consistency for the V8.2.9 export.

    Findings are short machine-readable strings. ``consistency_check_status``
    is ``FAIL`` whenever any finding is recorded.
    """
    report = ConsistencyCheckReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
        duplicate_ratio_before=float(duplicate_ratio_before),
        duplicate_ratio_after=float(duplicate_ratio_after),
        strict_oos_duplicate_ratio_used=float(strict_oos_duplicate_ratio_used),
        strict_oos_input_is_deduped=bool(strict_oos_input_is_deduped),
        strict_oos_status=str(strict_oos_status or ""),
        paper_sandbox_candidates=int(paper_sandbox_candidates),
        adversarial_duplicate_source=str(adversarial_duplicate_source or ""),
        adversarial_duplicate_ratio_used=float(adversarial_duplicate_ratio_used),
        exit_oos_status=str(exit_oos_status or ""),
        exit_policy_replay_mode=str(exit_policy_replay_mode or ""),
        exit_policy_productive_ready=bool(exit_policy_productive_ready),
        final_recommendation=str(final_recommendation or ""),
    )
    findings: list[str] = []

    # 1. When the OOS input is deduped, the duplicate-ratio the OOS
    #    actually used must equal the after-dedup ratio. Catches the
    #    V8.2.9.1 wiring bug where ``duplicate_ratio_before`` was passed
    #    by mistake.
    if strict_oos_input_is_deduped:
        if not _approx_equal(
            strict_oos_duplicate_ratio_used, duplicate_ratio_after,
        ):
            findings.append(
                "strict_oos_used_wrong_duplicate_ratio: "
                f"used={strict_oos_duplicate_ratio_used:.4f} "
                f"expected_after_dedup={duplicate_ratio_after:.4f}"
            )

    # 2. Adversarial duplicate-ratio source must match dedup wiring.
    if strict_oos_input_is_deduped:
        if adversarial_duplicate_source != "after":
            findings.append(
                "adversarial_used_pre_dedup_ratio_despite_deduped_input"
            )
        if not _approx_equal(
            adversarial_duplicate_ratio_used, duplicate_ratio_after,
        ):
            findings.append(
                "adversarial_ratio_disagrees_with_after_dedup: "
                f"used={adversarial_duplicate_ratio_used:.4f} "
                f"after_dedup={duplicate_ratio_after:.4f}"
            )
    else:
        if adversarial_duplicate_source != "before":
            findings.append(
                "adversarial_used_after_dedup_ratio_despite_raw_input"
            )

    # 3. ``paper_sandbox_candidates`` must be zero unless strict OOS
    #    promoted at least one rule to ``PAPER_SANDBOX_CANDIDATE``.
    if paper_sandbox_candidates > 0 and strict_oos_status != "PAPER_SANDBOX_CANDIDATE":
        findings.append(
            "paper_sandbox_emitted_but_strict_oos_top_level_not_pass: "
            f"status={strict_oos_status}"
        )

    # 4. Approximate exit replay must not advertise productive readiness.
    if (
        exit_oos_status == "PASS"
        and exit_policy_replay_mode == "approximate_mfe_mae"
        and exit_policy_productive_ready
    ):
        findings.append(
            "exit_policy_productive_ready_true_but_replay_is_approximate"
        )

    # 5. ``final_recommendation`` must remain ``NO LIVE`` at all times.
    if final_recommendation != FINAL_RECOMMENDATION_NO_LIVE:
        findings.append(
            "final_recommendation_not_no_live: "
            f"got={final_recommendation!r}"
        )

    report.consistency_findings = findings
    report.consistency_check_status = (
        CONSISTENCY_FAIL if findings else CONSISTENCY_PASS
    )
    report.status = STATUS_OK
    return report
