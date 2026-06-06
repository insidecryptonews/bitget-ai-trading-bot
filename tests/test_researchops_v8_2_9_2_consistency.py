"""V8.2.9.2 — Dedup ratio wiring fix + rebound outcome reconciliation +
exit replay hardening + consistency check tests.

All tests use synthetic dataset rows. No DB. No OHLCV reads.
"""

from __future__ import annotations

import csv
import json
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


def _row(
    *,
    ts: datetime | None = None,
    symbol: str = "BTCUSDT",
    side: str = "LONG",
    regime: str = "RISK_ON",
    score: int = 80,
    entry: float = 100.0,
    ret_1h: float = 0.5,
    ret_4h: float = 1.0,
    mfe: float = 1.0,
    mae: float = -0.2,
    first_barrier: str = "TP",
    net_pnl: float = 0.50,
    closed_by_horizon: bool = False,
) -> dict[str, Any]:
    bucket = (
        "90-100" if score >= 90 else
        "80-89" if score >= 80 else
        "70-79" if score >= 70 else "<70"
    )
    if ts is None:
        ts = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return {
        "signal_id": id(ts),
        "timestamp": ts.isoformat(),
        "symbol": symbol,
        "side": side,
        "regime": regime,
        "score": score,
        "score_bucket": bucket,
        "strategy": "TEST",
        "reason": "",
        "blocked_by": "",
        "edgeguard_reason": "",
        "source": "",
        "candidate_selected": False,
        "risk_approved": False,
        "entry_price": entry,
        "normalized_atr": 0.02,
        "ohlcv_available": True,
        "ret_1h_pct": ret_1h,
        "ret_4h_pct": ret_4h,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "first_barrier_hit": first_barrier,
        "tp_before_sl": first_barrier == "TP",
        "sl_before_tp": first_barrier == "SL",
        "baseline_result": first_barrier,
        "baseline_gross_pnl": net_pnl + 0.46,
        "baseline_net_pnl_est": net_pnl,
        "trailing_result": "trailing_proxy",
        "trailing_net_pnl_est": net_pnl + 0.1,
        "campaign_result": "1+1_proxy",
        "campaign_net_pnl_est": net_pnl * 1.3,
        "data_quality": "OK",
        "training_label": "GOOD_LONG" if net_pnl > 0 else "BAD_LONG",
        "final_use_for_training": True,
        "research_only": True,
        "final_recommendation": "NO LIVE",
        "closed_by_horizon": closed_by_horizon,
        "exit_reason": "HORIZON_CLOSE" if closed_by_horizon else "",
        "entry_time": ts.isoformat(),
        "exit_time": (ts + timedelta(minutes=30)).isoformat(),
        "exit_price": entry * (1 + net_pnl / 100.0),
        "net_pct": net_pnl,
        "bars": 6,
        "tp_pct": 1.0,
        "sl_pct": -1.0,
    }


# ---------------------------------------------------------------------------
# FIX 1 — Dedup Ratio Wiring
# ---------------------------------------------------------------------------

def test_strict_oos_reports_input_is_deduped_flag():
    """Strict OOS exposes ``strict_oos_input_is_deduped`` so the
    pipeline can verify whether the call site already deduplicated."""
    from app.labs.rebound_long_strict_oos_v8_2_9 import run_strict_oos_rebound

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candidates = [
        {
            "symbol": "BTCUSDT",
            "timestamp": (start + timedelta(hours=i)).isoformat(),
            "regime_before": "TREND_DOWN", "regime_now": "TREND_UP",
            "volatility_bucket": "normal",
            "trend_recovering_prefix": True,
            "net_pnl_est": 0.5 if i % 4 != 0 else -0.3,
        }
        for i in range(120)
    ]
    deduped = run_strict_oos_rebound(
        candidates, input_is_deduped=True, duplicate_ratio_after=0.0,
    )
    assert deduped.strict_oos_input_is_deduped is True
    assert deduped.duplicate_ratio_after == 0.0
    raw_input = run_strict_oos_rebound(candidates)
    assert raw_input.strict_oos_input_is_deduped is False


def test_adversarial_audit_does_not_fail_on_high_raw_ratio_if_deduped():
    """V8.2.9.2 wiring: when the OOS input is deduped and the
    after-dedup ratio is <= 0.30, the raw ratio (e.g. 0.89) must NOT
    trigger FAIL_DUPLICATES."""
    from app.labs.adversarial_research_audit_v8_2_9 import (
        AUDIT_FAIL_DUPLICATES,
        AUDIT_PASS,
        audit_v829,
    )
    r = audit_v829(
        duplicate_ratio_raw=0.89,
        duplicate_ratio_after_dedup=0.0,
        strict_oos_input_is_deduped=True,
    )
    assert AUDIT_FAIL_DUPLICATES not in r.blockers
    assert r.adversarial_duplicate_source == "after"
    assert r.adversarial_duplicate_ratio_used == 0.0
    assert r.audit_status == AUDIT_PASS


def test_adversarial_audit_fails_if_not_deduped_and_raw_high():
    """When the OOS input is NOT deduped and raw ratio > 0.30, the
    audit must FAIL_DUPLICATES."""
    from app.labs.adversarial_research_audit_v8_2_9 import (
        AUDIT_FAIL_DUPLICATES,
        audit_v829,
    )
    r = audit_v829(
        duplicate_ratio_raw=0.89,
        duplicate_ratio_after_dedup=0.0,
        strict_oos_input_is_deduped=False,
    )
    assert AUDIT_FAIL_DUPLICATES in r.blockers
    assert r.adversarial_duplicate_source == "before"
    assert r.adversarial_duplicate_ratio_used == pytest.approx(0.89)


def test_adversarial_audit_fails_if_deduped_but_after_ratio_still_high():
    """If the dedup did not actually reduce duplicates below 0.30,
    even with the deduped flag the audit must fail."""
    from app.labs.adversarial_research_audit_v8_2_9 import (
        AUDIT_FAIL_DUPLICATES,
        audit_v829,
    )
    r = audit_v829(
        duplicate_ratio_raw=0.89,
        duplicate_ratio_after_dedup=0.55,
        strict_oos_input_is_deduped=True,
    )
    assert AUDIT_FAIL_DUPLICATES in r.blockers


def test_adversarial_audit_backward_compat_duplicate_ratio_after_kwarg():
    """V8.2.9 callers used ``duplicate_ratio_after=...`` only.  Behaviour
    on that arg must remain stable: > 0.30 still triggers
    ``FAIL_DUPLICATES``."""
    from app.labs.adversarial_research_audit_v8_2_9 import (
        AUDIT_FAIL_DUPLICATES,
        audit_v829,
    )
    r = audit_v829(duplicate_ratio_after=0.85)
    assert AUDIT_FAIL_DUPLICATES in r.blockers


def test_export_v829_2_summary_distinguishes_before_vs_after(tmp_path):
    """Summary surfaces the before / after dedup ratios + wiring
    flags so the operator can see which value drove the OOS."""
    from app.labs.research_export_v8_2_9 import export_research_v829
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(40)]
    base = tmp_path / "v8292_summary_dedup"
    export_research_v829(None, rows=rows, base_dir=base)
    summary = (base / "research_v8_2_9_summary.txt").read_text(encoding="utf-8")
    for key in (
        "duplicate_ratio_before",
        "duplicate_ratio_after",
        "strict_oos_input_is_deduped",
        "strict_oos_duplicate_ratio_used",
        "adversarial_duplicate_source",
        "adversarial_duplicate_ratio_used",
    ):
        assert key in summary, f"summary missing {key}"


# ---------------------------------------------------------------------------
# FIX 2 — Rebound Outcome Reconciliation
# ---------------------------------------------------------------------------

def test_reconciliation_cost_flip_classifies_as_cost_adjustment_flips_outcome():
    """net_ev positive before cost, negative after cost → reason is
    ``cost_adjustment_flips_outcome``."""
    from app.labs.rebound_outcome_reconciliation_v8_2_9 import (
        REASON_COST_FLIPS,
        _classify_reason,
    )
    reason, _ = _classify_reason(
        raw_count=100, dedup_count=100, v828_like_count=20,
        winrate_raw=0.55, winrate_dedup=0.55, winrate_v828_like=0.55,
        net_ev_before_cost=0.20, net_ev_after_cost=-0.05,
        sign_bug_count=0, outcome_field_mismatch_count=0,
    )
    assert reason == REASON_COST_FLIPS


def test_reconciliation_detects_dedup_removed_winners():
    """When dedup substantially reduces the winrate the classifier
    returns ``dedup_removed_winners``."""
    from app.labs.rebound_outcome_reconciliation_v8_2_9 import (
        REASON_DEDUP_REMOVED_WINNERS,
        _classify_reason,
    )
    reason, _ = _classify_reason(
        raw_count=200, dedup_count=120, v828_like_count=120,
        winrate_raw=0.80, winrate_dedup=0.50, winrate_v828_like=0.80,
        net_ev_before_cost=0.30, net_ev_after_cost=0.10,
        sign_bug_count=0, outcome_field_mismatch_count=0,
    )
    assert reason == REASON_DEDUP_REMOVED_WINNERS


def test_reconciliation_detects_sign_bug():
    """Significant fraction of candidates with positive ret_4h but
    negative net_pnl_est → ``sign_bug``."""
    from app.labs.rebound_outcome_reconciliation_v8_2_9 import (
        REASON_SIGN_BUG,
        _classify_reason,
    )
    reason, _ = _classify_reason(
        raw_count=100, dedup_count=100, v828_like_count=100,
        winrate_raw=0.45, winrate_dedup=0.45, winrate_v828_like=0.45,
        net_ev_before_cost=0.10, net_ev_after_cost=-0.10,
        sign_bug_count=30, outcome_field_mismatch_count=0,
    )
    assert reason == REASON_SIGN_BUG


def test_reconciliation_does_not_use_ret_fields_as_detection_input():
    """The reconciliation module reads ret_4h_pct only as an ex-post
    audit signal, not as a detection input. Verified by checking the
    extractor source it relies on."""
    import ast
    import inspect
    from app.labs.rebound_long_candidate_extractor_v8_2_9 import (
        detect_rebound_long_prefix_only,
    )
    src = inspect.getsource(detect_rebound_long_prefix_only)
    tree = ast.parse(src)
    body = list(tree.body[0].body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not node.value.startswith("ret_"), (
                    f"detector references {node.value!r}"
                )


def test_reconciliation_full_pipeline_with_synthetic_dataset():
    """End-to-end: dataset with a clean rebound setup, the
    reconciliation runs to completion and emits a structured report."""
    from app.labs.rebound_outcome_reconciliation_v8_2_9 import (
        reconcile_rebound_outcome,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows: list[dict[str, Any]] = []
    for i in range(10):
        rows.append(_row(
            ts=start + timedelta(hours=i),
            side="LONG", regime="TREND_DOWN",
            entry=100.0 - i * 0.5,
        ))
    for i in range(30):
        rows.append(_row(
            ts=start + timedelta(hours=10 + i),
            side="LONG", regime="TREND_UP",
            entry=98.0 + i * 0.1,
            net_pnl=0.50 if i % 3 != 0 else -0.20,
        ))
    r = reconcile_rebound_outcome(None, rows=rows)
    assert r.candidates_v829_raw >= 1
    assert r.reason_for_gap in {
        "different_candidate_universe",
        "cost_adjustment_flips_outcome",
        "dedup_removed_winners",
        "outcome_field_mismatch",
        "sign_bug",
        "unknown",
    }
    assert r.used_future_return_features is False


# ---------------------------------------------------------------------------
# FIX 3 — Exit Monetization Hardening
# ---------------------------------------------------------------------------

def test_exit_audit_pass_with_approx_mode_does_not_become_paper_candidate():
    """Even if best_policy_test_status=PASS, while replay_mode is
    approximate the audit must not advertise a paper-sandbox-ready
    policy. ``exit_policy_candidate_status`` stays
    ``OOS_PASS_APPROX_ONLY`` and ``exit_policy_productive_ready`` stays
    False."""
    from app.labs.exit_monetization_audit_v8_2_9 import (
        EXIT_POLICY_STATUS_OOS_PASS_APPROX_ONLY,
        REPLAY_MODE_APPROXIMATE_MFE_MAE,
        run_exit_monetization_audit,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(60):
        rows.append(_row(
            ts=start + timedelta(hours=i),
            net_pnl=0.80, mfe=1.2, mae=-0.05,
            first_barrier="TP", closed_by_horizon=False,
        ))
    r = run_exit_monetization_audit(None, rows=rows)
    assert r.exit_policy_replay_mode == REPLAY_MODE_APPROXIMATE_MFE_MAE
    assert r.exit_policy_productive_ready is False
    assert r.requires_bar_by_bar_replay is True
    assert r.exit_policy_candidate_status in {
        EXIT_POLICY_STATUS_OOS_PASS_APPROX_ONLY,
        "NEED_BAR_BY_BAR_REPLAY",
    }
    assert r.answers.get("any_exit_paper_sandbox_candidate") is False
    assert r.answers.get("requires_bar_by_bar_replay") is True


def test_export_v829_2_summary_contains_replay_hardening_flags(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(40)]
    base = tmp_path / "v8292_summary_exit"
    export_research_v829(None, rows=rows, base_dir=base)
    summary = (base / "research_v8_2_9_summary.txt").read_text(encoding="utf-8")
    assert "exit_policy_replay_mode: approximate_mfe_mae" in summary
    assert "exit_policy_productive_ready: false" in summary
    assert "requires_bar_by_bar_replay: true" in summary
    assert "exit_policy_candidate_status:" in summary


# ---------------------------------------------------------------------------
# FIX 4 — Consistency Check
# ---------------------------------------------------------------------------

def test_consistency_check_pass_when_deduped_inputs_consistent():
    from app.labs.research_pack_consistency_check_v8_2_9_2 import (
        CONSISTENCY_PASS,
        run_consistency_check_v8_2_9_2,
    )
    r = run_consistency_check_v8_2_9_2(
        duplicate_ratio_before=0.89,
        duplicate_ratio_after=0.0,
        strict_oos_duplicate_ratio_used=0.0,
        strict_oos_input_is_deduped=True,
        strict_oos_status="NEED_MORE_DATA",
        paper_sandbox_candidates=0,
        adversarial_duplicate_source="after",
        adversarial_duplicate_ratio_used=0.0,
        exit_oos_status="PASS",
        exit_policy_replay_mode="approximate_mfe_mae",
        exit_policy_productive_ready=False,
        final_recommendation="NO LIVE",
    )
    assert r.consistency_check_status == CONSISTENCY_PASS
    assert r.consistency_findings == []


def test_consistency_check_fails_when_strict_oos_uses_wrong_ratio():
    from app.labs.research_pack_consistency_check_v8_2_9_2 import (
        CONSISTENCY_FAIL,
        run_consistency_check_v8_2_9_2,
    )
    r = run_consistency_check_v8_2_9_2(
        duplicate_ratio_before=0.89,
        duplicate_ratio_after=0.0,
        # Wiring bug: strict OOS received the BEFORE ratio.
        strict_oos_duplicate_ratio_used=0.89,
        strict_oos_input_is_deduped=True,
        strict_oos_status="NEED_MORE_DATA",
        paper_sandbox_candidates=0,
        adversarial_duplicate_source="after",
        adversarial_duplicate_ratio_used=0.0,
        exit_oos_status="PASS",
        exit_policy_replay_mode="approximate_mfe_mae",
        exit_policy_productive_ready=False,
        final_recommendation="NO LIVE",
    )
    assert r.consistency_check_status == CONSISTENCY_FAIL
    assert any("strict_oos_used_wrong_duplicate_ratio" in f
               for f in r.consistency_findings)


def test_consistency_check_fails_when_adversarial_source_mismatches_dedup_state():
    from app.labs.research_pack_consistency_check_v8_2_9_2 import (
        CONSISTENCY_FAIL,
        run_consistency_check_v8_2_9_2,
    )
    r = run_consistency_check_v8_2_9_2(
        duplicate_ratio_before=0.89,
        duplicate_ratio_after=0.0,
        strict_oos_duplicate_ratio_used=0.0,
        strict_oos_input_is_deduped=True,
        strict_oos_status="NEED_MORE_DATA",
        paper_sandbox_candidates=0,
        # Mismatch: deduped input but audit used the "before" source.
        adversarial_duplicate_source="before",
        adversarial_duplicate_ratio_used=0.89,
        exit_oos_status="PASS",
        exit_policy_replay_mode="approximate_mfe_mae",
        exit_policy_productive_ready=False,
        final_recommendation="NO LIVE",
    )
    assert r.consistency_check_status == CONSISTENCY_FAIL


def test_consistency_check_fails_when_paper_sandbox_emitted_without_pass():
    from app.labs.research_pack_consistency_check_v8_2_9_2 import (
        CONSISTENCY_FAIL,
        run_consistency_check_v8_2_9_2,
    )
    r = run_consistency_check_v8_2_9_2(
        duplicate_ratio_before=0.0,
        duplicate_ratio_after=0.0,
        strict_oos_duplicate_ratio_used=0.0,
        strict_oos_input_is_deduped=True,
        strict_oos_status="REJECT",
        # Inconsistency: paper sandbox candidates > 0 but top status is REJECT.
        paper_sandbox_candidates=3,
        adversarial_duplicate_source="after",
        adversarial_duplicate_ratio_used=0.0,
        exit_oos_status="PASS",
        exit_policy_replay_mode="approximate_mfe_mae",
        exit_policy_productive_ready=False,
        final_recommendation="NO LIVE",
    )
    assert r.consistency_check_status == CONSISTENCY_FAIL


def test_consistency_check_fails_when_exit_productive_with_approx_replay():
    from app.labs.research_pack_consistency_check_v8_2_9_2 import (
        CONSISTENCY_FAIL,
        run_consistency_check_v8_2_9_2,
    )
    r = run_consistency_check_v8_2_9_2(
        duplicate_ratio_before=0.0,
        duplicate_ratio_after=0.0,
        strict_oos_duplicate_ratio_used=0.0,
        strict_oos_input_is_deduped=True,
        strict_oos_status="NEED_MORE_DATA",
        paper_sandbox_candidates=0,
        adversarial_duplicate_source="after",
        adversarial_duplicate_ratio_used=0.0,
        exit_oos_status="PASS",
        exit_policy_replay_mode="approximate_mfe_mae",
        # Inconsistency: marked productive while replay is approximate.
        exit_policy_productive_ready=True,
        final_recommendation="NO LIVE",
    )
    assert r.consistency_check_status == CONSISTENCY_FAIL


def test_consistency_check_fails_when_final_recommendation_not_no_live():
    from app.labs.research_pack_consistency_check_v8_2_9_2 import (
        CONSISTENCY_FAIL,
        run_consistency_check_v8_2_9_2,
    )
    r = run_consistency_check_v8_2_9_2(
        duplicate_ratio_before=0.0,
        duplicate_ratio_after=0.0,
        strict_oos_duplicate_ratio_used=0.0,
        strict_oos_input_is_deduped=True,
        strict_oos_status="NEED_MORE_DATA",
        paper_sandbox_candidates=0,
        adversarial_duplicate_source="after",
        adversarial_duplicate_ratio_used=0.0,
        exit_oos_status="PASS",
        exit_policy_replay_mode="approximate_mfe_mae",
        exit_policy_productive_ready=False,
        final_recommendation="GO LIVE",
    )
    assert r.consistency_check_status == CONSISTENCY_FAIL


def test_adversarial_audit_fail_consistency_when_consistency_failed():
    from app.labs.adversarial_research_audit_v8_2_9 import (
        AUDIT_FAIL_CONSISTENCY,
        audit_v829,
    )
    r = audit_v829(
        consistency_check_failed=True,
        consistency_findings=["strict_oos_used_wrong_duplicate_ratio"],
    )
    assert AUDIT_FAIL_CONSISTENCY in r.blockers


def test_export_v829_2_summary_contains_consistency_status(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(40)]
    base = tmp_path / "v8292_summary_consistency"
    export_research_v829(None, rows=rows, base_dir=base)
    summary = (base / "research_v8_2_9_summary.txt").read_text(encoding="utf-8")
    assert "consistency_check_status:" in summary
    assert "rebound_reconciliation_reason:" in summary


def test_export_v829_2_zip_includes_new_csvs(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(40)]
    base = tmp_path / "v8292_zip"
    export_research_v829(None, rows=rows, base_dir=base)
    zip_path = base / "research_v8_2_9_exports.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "rebound_outcome_reconciliation_v1.csv" in names
    assert "consistency_check_v1.csv" in names
    for name in names:
        assert name.endswith((".csv", ".txt", ".json"))


def test_export_v829_2_manifest_v2_keys(tmp_path):
    from app.labs.research_export_v8_2_9 import export_research_v829
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(40)]
    base = tmp_path / "v8292_manifest"
    export_research_v829(None, rows=rows, base_dir=base)
    manifest = json.loads((base / "manifest_v1.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "v8.2.9.v2"
    for key in (
        "duplicate_ratio_before",
        "duplicate_ratio_after",
        "strict_oos_input_is_deduped",
        "strict_oos_duplicate_ratio_used",
        "adversarial_duplicate_source",
        "adversarial_duplicate_ratio_used",
        "exit_policy_replay_mode",
        "exit_policy_productive_ready",
        "requires_bar_by_bar_replay",
        "exit_policy_candidate_status",
        "rebound_reconciliation_reason",
        "consistency_check_status",
        "consistency_findings",
    ):
        assert key in manifest, f"manifest missing {key}"


# ---------------------------------------------------------------------------
# CLI parser regression
# ---------------------------------------------------------------------------

def test_v8292_cli_includes_reconciliation_command():
    from app.research_lab import build_argument_parser
    parser = build_argument_parser()
    ns = parser.parse_args(
        ["rebound-outcome-reconciliation-v829", "--hours", "168"]
    )
    assert ns.command == "rebound-outcome-reconciliation-v829"


def test_v8292_parser_no_duplicate_option_strings():
    from app.research_lab import build_argument_parser
    parser = build_argument_parser()
    counts: dict[str, int] = {}
    for action in parser._actions:
        for opt in action.option_strings or []:
            counts[opt] = counts.get(opt, 0) + 1
    duplicates = [opt for opt, c in counts.items() if c > 1]
    assert not duplicates, f"Duplicate option strings: {duplicates}"


# ---------------------------------------------------------------------------
# Safety AST scan
# ---------------------------------------------------------------------------

V8292_MODULES = [
    "app.labs.research_pack_consistency_check_v8_2_9_2",
    "app.labs.rebound_outcome_reconciliation_v8_2_9",
]


def test_v8292_modules_have_no_forbidden_calls():
    import ast
    import importlib
    import pathlib
    forbidden = {
        "place_order", "set_leverage", "set_margin_mode",
        "private_get", "private_post",
    }
    for mod in V8292_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name not in forbidden, f"{mod} calls {name}"


def test_v8292_modules_carry_no_live_flags():
    from app.labs.rebound_outcome_reconciliation_v8_2_9 import (
        ReconciliationReport,
    )
    from app.labs.research_pack_consistency_check_v8_2_9_2 import (
        ConsistencyCheckReport,
    )
    for inst in [
        ReconciliationReport(hours=1, generated_at="t"),
        ConsistencyCheckReport(hours=1, generated_at="t"),
    ]:
        assert inst.research_only is True
        assert inst.paper_filter_enabled is False
        assert inst.can_send_real_orders is False
        assert inst.final_recommendation == "NO LIVE"
