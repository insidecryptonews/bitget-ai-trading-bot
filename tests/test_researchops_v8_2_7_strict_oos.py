"""V8.2.7 — Strict OOS rule selection + short verdict fix + final gate.

All tests run with synthetic dataset rows. No DB, no OHLCV reads.
"""

from __future__ import annotations

import ast
import csv
import importlib
import pathlib
import zipfile
from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Synthetic dataset rows
# ---------------------------------------------------------------------------

def _row(
    *,
    ts: datetime,
    symbol="BTCUSDT",
    side="LONG",
    regime="RISK_ON",
    score=80,
    strategy="TEST",
    entry=100.0,
    ret_4h=1.0,
    mfe=1.0,
    mae=-0.2,
    first_barrier="TP",
    net_pnl=0.50,
    gross=0.96,
    label="GOOD_LONG",
    candidate_selected=False,
    risk_approved=False,
):
    return {
        "signal_id": id(ts),
        "timestamp": ts.isoformat(),
        "symbol": symbol,
        "side": side,
        "regime": regime,
        "score": score,
        "score_bucket": (
            "90-100" if score >= 90 else
            "80-89" if score >= 80 else
            "70-79" if score >= 70 else
            "60-69" if score >= 60 else "<60"
        ),
        "strategy": strategy,
        "reason": "",
        "blocked_by": "",
        "edgeguard_reason": "",
        "candidate_selected": candidate_selected,
        "risk_approved": risk_approved,
        "entry_price": entry,
        "ohlcv_available": True,
        "ret_1h_pct": ret_4h * 0.5,
        "ret_4h_pct": ret_4h,
        "ret_24h_pct": ret_4h * 2.0,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "first_barrier_hit": first_barrier,
        "tp_before_sl": first_barrier == "TP",
        "sl_before_tp": first_barrier == "SL",
        "baseline_result": first_barrier,
        "baseline_gross_pnl": gross,
        "baseline_net_pnl_est": net_pnl,
        "trailing_result": "trailing_proxy",
        "trailing_net_pnl_est": net_pnl + 0.1,
        "campaign_result": "1+1_proxy",
        "campaign_net_pnl_est": net_pnl * 1.3,
        "would_have_worked_baseline": net_pnl > 0,
        "would_have_worked_trailing": True,
        "would_have_worked_campaign": net_pnl > 0,
        "data_quality": "OK",
        "label_confidence": 0.8,
        "training_label": label,
        "final_use_for_training": True,
        "research_only": True,
        "final_recommendation": "NO LIVE",
    }


# ---------------------------------------------------------------------------
# Strict OOS Selector — feature whitelist
# ---------------------------------------------------------------------------

def test_strict_selector_refuses_forbidden_features():
    from app.labs.strict_oos_rule_selector_v8_2_7 import select_rules_strict_oos

    for forbidden in (
        "training_label", "first_barrier_hit", "baseline_net_pnl_est",
        "baseline_gross_pnl", "ret_1h_pct", "ret_4h_pct", "ret_24h_pct",
        "mfe_pct", "mae_pct",
    ):
        with pytest.raises(ValueError):
            select_rules_strict_oos(None, rows=[], grouping_features=(forbidden,))


def test_strict_selector_default_features_are_ex_ante_only():
    from app.labs.candidate_rule_miner_v8_2_6 import EX_POST_LABELS
    from app.labs.strict_oos_rule_selector_v8_2_7 import EX_ANTE_FEATURES

    for feature in EX_ANTE_FEATURES:
        assert feature not in EX_POST_LABELS


# ---------------------------------------------------------------------------
# Strict OOS — train-only selection (verified by behaviour)
# ---------------------------------------------------------------------------

def test_strict_selector_rejects_rule_that_wins_train_but_loses_test():
    from app.labs.strict_oos_rule_selector_v8_2_7 import (
        FINAL_REJECT,
        select_rules_strict_oos,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # First 60% (train) winners; middle 20% (val) winners; last 20% (test) losers.
    # 100 rows total → 60 train + 20 val + 20 test.
    rows: list[dict] = []
    for i in range(60):
        rows.append(_row(ts=start + timedelta(hours=i),
                         symbol="XYZUSDT", gross=0.96, net_pnl=0.70))
    for i in range(20):
        rows.append(_row(ts=start + timedelta(hours=60 + i),
                         symbol="XYZUSDT", gross=0.96, net_pnl=0.70))
    for i in range(20):
        rows.append(_row(ts=start + timedelta(hours=80 + i),
                         symbol="XYZUSDT", gross=-0.60, net_pnl=-0.80))
    r = select_rules_strict_oos(
        None, rows=rows, score_calibration_ok=True,
    )
    # Single feature combo → 1 rule.
    assert r.total_rules_evaluated >= 1
    statuses = (
        list(r.paper_sandbox_candidates) + list(r.research_candidates)
        + list(r.watch_only_rules) + list(r.rejected_rules)
        + list(r.need_more_data_rules)
    )
    # The single rule must be REJECT (test net EV negative).
    assert any(rule["final_gate"] == FINAL_REJECT for rule in statuses)


def test_strict_selector_marks_need_more_data_with_small_test_split():
    from app.labs.strict_oos_rule_selector_v8_2_7 import (
        FINAL_NEED_MORE_DATA,
        select_rules_strict_oos,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # Only 30 total → test split = 6, below MIN_TEST_SAMPLES (15).
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(30)]
    r = select_rules_strict_oos(None, rows=rows, score_calibration_ok=True)
    all_rules = (
        list(r.paper_sandbox_candidates) + list(r.research_candidates)
        + list(r.watch_only_rules) + list(r.rejected_rules)
        + list(r.need_more_data_rules)
    )
    assert any(rule["final_gate"] == FINAL_NEED_MORE_DATA for rule in all_rules)


def test_strict_selector_promotes_to_paper_sandbox_when_all_splits_positive():
    """When train/validation/test all show positive net EV with healthy PF
    and winrate, the rule can be tagged PAPER_SANDBOX_CANDIDATE (still
    research-only — no flag flips).
    """
    from app.labs.strict_oos_rule_selector_v8_2_7 import (
        FINAL_PAPER_SANDBOX_CANDIDATE,
        select_rules_strict_oos,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows: list[dict] = []
    # 100 rows interleaved 75% winners / 25% losers so every temporal split
    # has the same mix. One row per hour avoids timestamp clusters.
    for i in range(100):
        is_winner = (i % 4) != 3
        if is_winner:
            rows.append(_row(ts=start + timedelta(hours=i),
                             symbol="XYZUSDT", gross=0.95, net_pnl=0.70))
        else:
            rows.append(_row(ts=start + timedelta(hours=i),
                             symbol="XYZUSDT", gross=-0.20, net_pnl=-0.45))
    r = select_rules_strict_oos(
        None, rows=rows, score_calibration_ok=True,
    )
    all_rules = (
        list(r.paper_sandbox_candidates) + list(r.research_candidates)
        + list(r.watch_only_rules) + list(r.rejected_rules)
        + list(r.need_more_data_rules)
    )
    # At least one rule reaches PAPER_SANDBOX_CANDIDATE.
    assert any(rule["final_gate"] == FINAL_PAPER_SANDBOX_CANDIDATE for rule in all_rules)


def test_strict_selector_excludes_short_when_verdict_not_safe():
    from app.labs.strict_oos_rule_selector_v8_2_7 import select_rules_strict_oos

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        _row(ts=start + timedelta(hours=i), side="SHORT", symbol="ETHUSDT")
        for i in range(40)
    ]
    rows += [
        _row(ts=start + timedelta(hours=200 + i), side="LONG", symbol="BTCUSDT")
        for i in range(40)
    ]
    r = select_rules_strict_oos(
        None, rows=rows, short_verdict="SHORT_LABELS_SUSPECT",
        score_calibration_ok=True,
    )
    assert r.short_excluded is True
    # No SHORT rules should appear in any bucket.
    all_rules = (
        list(r.paper_sandbox_candidates) + list(r.research_candidates)
        + list(r.watch_only_rules) + list(r.rejected_rules)
        + list(r.need_more_data_rules)
    )
    for rule in all_rules:
        assert str(rule["features"].get("side")).upper() != "SHORT"


def test_strict_selector_score_calibration_fail_keeps_rules_at_research_candidate():
    """When score calibration fails, even rules that pass all numeric gates
    cannot reach PAPER_SANDBOX_CANDIDATE — they cap at RESEARCH_CANDIDATE.
    """
    from app.labs.strict_oos_rule_selector_v8_2_7 import (
        FINAL_PAPER_SANDBOX_CANDIDATE,
        FINAL_RESEARCH_CANDIDATE,
        select_rules_strict_oos,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows: list[dict] = []
    for i in range(100):
        is_winner = (i % 4) != 3
        if is_winner:
            rows.append(_row(ts=start + timedelta(hours=i),
                             symbol="XYZUSDT", gross=0.95, net_pnl=0.70))
        else:
            rows.append(_row(ts=start + timedelta(hours=i),
                             symbol="XYZUSDT", gross=-0.20, net_pnl=-0.45))
    r = select_rules_strict_oos(
        None, rows=rows, score_calibration_ok=False,
    )
    all_rules = (
        list(r.paper_sandbox_candidates) + list(r.research_candidates)
        + list(r.watch_only_rules) + list(r.rejected_rules)
        + list(r.need_more_data_rules)
    )
    # No rule may be PAPER_SANDBOX when calibration is FAIL.
    paper = [rule for rule in all_rules if rule["final_gate"] == FINAL_PAPER_SANDBOX_CANDIDATE]
    research = [rule for rule in all_rules if rule["final_gate"] == FINAL_RESEARCH_CANDIDATE]
    assert paper == []
    assert research, "expected at least one RESEARCH_CANDIDATE without calibration OK"


# ---------------------------------------------------------------------------
# Short verdict V8.2.7 — ratio-based
# ---------------------------------------------------------------------------

def test_short_verdict_safe_when_low_suspicious_and_low_bugs():
    from app.labs.short_barrier_debug_v8_2_7 import (
        SHORT_SAFE,
        decide_verdict_v827,
    )

    verdict = decide_verdict_v827(
        suspicious_ratio=0.05, sign_bug_ratio=0.01,
        barrier_bug_ratio=0.01, same_bar_ratio=0.03,
    )
    assert verdict == SHORT_SAFE


def test_short_verdict_broken_when_sign_bug_above_10pct():
    from app.labs.short_barrier_debug_v8_2_7 import (
        SHORT_BROKEN,
        decide_verdict_v827,
    )

    verdict = decide_verdict_v827(
        suspicious_ratio=0.15, sign_bug_ratio=0.12,
        barrier_bug_ratio=0.01, same_bar_ratio=0.02,
    )
    assert verdict == SHORT_BROKEN


def test_short_verdict_broken_when_barrier_bug_above_10pct():
    from app.labs.short_barrier_debug_v8_2_7 import (
        SHORT_BROKEN,
        decide_verdict_v827,
    )

    verdict = decide_verdict_v827(
        suspicious_ratio=0.15, sign_bug_ratio=0.01,
        barrier_bug_ratio=0.15, same_bar_ratio=0.02,
    )
    assert verdict == SHORT_BROKEN


def test_short_verdict_broken_when_suspicious_above_40pct():
    from app.labs.short_barrier_debug_v8_2_7 import (
        SHORT_BROKEN,
        decide_verdict_v827,
    )

    verdict = decide_verdict_v827(
        suspicious_ratio=0.45, sign_bug_ratio=0.05,
        barrier_bug_ratio=0.05, same_bar_ratio=0.20,
    )
    assert verdict == SHORT_BROKEN


def test_short_verdict_exclude_in_between():
    from app.labs.short_barrier_debug_v8_2_7 import (
        SHORT_EXCLUDE,
        decide_verdict_v827,
    )

    verdict = decide_verdict_v827(
        suspicious_ratio=0.20, sign_bug_ratio=0.05,
        barrier_bug_ratio=0.05, same_bar_ratio=0.05,
    )
    assert verdict == SHORT_EXCLUDE


# ---------------------------------------------------------------------------
# Final Rule Gate
# ---------------------------------------------------------------------------

def test_final_gate_emits_no_paper_candidates_marker_when_empty():
    from app.labs.final_rule_gate_v8_2_7 import (
        NO_PAPER_CANDIDATES_MARKER,
        run_final_gate,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # Only 20 rows → splits too small, no rules reach PAPER_SANDBOX.
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(20)]
    r = run_final_gate(None, rows=rows)
    assert r.paper_sandbox_candidates == 0
    assert r.no_paper_candidates_marker == NO_PAPER_CANDIDATES_MARKER


# ---------------------------------------------------------------------------
# Export V8.2.7
# ---------------------------------------------------------------------------

def test_export_v827_strict_oos_csv_has_separate_feature_columns(tmp_path):
    from app.labs.research_export_v8_2_7 import (
        STRICT_OOS_COLUMNS,
        export_research_v827,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(80)]
    base = tmp_path / "v827"
    export_research_v827(None, rows=rows, base_dir=base)
    csv_path = base / "strict_oos_rules_v1.csv"
    assert csv_path.exists()
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, [])
    for feature in ("symbol", "side", "regime", "strategy", "score_bucket",
                    "candidate_selected", "risk_approved"):
        assert feature in header, f"{feature} missing from strict_oos_rules header"
    # And no embedded ``rule_id`` column collapsing them.
    assert "rule_id" not in header


def test_export_v827_zip_contains_only_csv_txt_json(tmp_path):
    from app.labs.research_export_v8_2_7 import export_research_v827

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(20)]
    base = tmp_path / "v827_zip"
    export_research_v827(None, rows=rows, base_dir=base)
    zip_path = base / "research_v8_2_7_exports.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            assert name.endswith((".csv", ".txt", ".json"))


def test_export_v827_does_not_leak_secrets(tmp_path):
    from app.labs.research_export_v8_2_7 import export_research_v827

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(20)]
    rows[0]["bitget_api_secret"] = "SHOULD_BE_REDACTED"
    base = tmp_path / "v827_secrets"
    export_research_v827(None, rows=rows, base_dir=base)
    for path in base.glob("*.csv"):
        text = path.read_text(encoding="utf-8")
        assert "SHOULD_BE_REDACTED" not in text
        assert "bitget_api_secret" not in text.lower()


# ---------------------------------------------------------------------------
# CLI parser regression
# ---------------------------------------------------------------------------

def test_v827_cli_commands_parse():
    from app.research_lab import build_argument_parser

    parser = build_argument_parser()
    for argv in [
        ["strict-oos-rule-selector-v827", "--hours", "168"],
        ["short-barrier-debug-v827", "--hours", "168"],
        ["final-rule-gate-v827", "--hours", "168"],
        ["export-research-v827", "--hours", "168"],
        ["research-pack-v827", "--hours", "168"],
    ]:
        ns = parser.parse_args(argv)
        assert ns.command == argv[0]


def test_parser_has_no_duplicate_option_strings_after_v827():
    from app.research_lab import build_argument_parser

    parser = build_argument_parser()
    counts: dict[str, int] = {}
    for action in parser._actions:
        for opt in action.option_strings or []:
            counts[opt] = counts.get(opt, 0) + 1
    duplicates = [opt for opt, count in counts.items() if count > 1]
    assert not duplicates, f"Duplicate option strings: {duplicates}"


# ---------------------------------------------------------------------------
# Safety AST scan
# ---------------------------------------------------------------------------

FORBIDDEN_CALL_NAMES = {
    "place_order", "set_leverage", "set_margin_mode",
    "private_get", "private_post",
}
FORBIDDEN_ASSIGN_LITERALS = {
    "LIVE_TRADING", "ENABLE_PAPER_POLICY_FILTER",
    "can_send_real_orders", "allow_real_writes",
}

V827_MODULES = [
    "app.labs.strict_oos_rule_selector_v8_2_7",
    "app.labs.short_barrier_debug_v8_2_7",
    "app.labs.final_rule_gate_v8_2_7",
    "app.labs.research_export_v8_2_7",
]


def test_v827_modules_have_no_forbidden_calls():
    for mod in V827_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name not in FORBIDDEN_CALL_NAMES, f"{mod} calls {name}"


def test_v827_modules_have_no_forbidden_literal_true_assigns():
    for mod in V827_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                    if name in FORBIDDEN_ASSIGN_LITERALS and isinstance(node.value, ast.Constant):
                        assert node.value.value is not True, f"{mod} {name}=True"


def test_v827_outputs_carry_no_live():
    from app.labs.final_rule_gate_v8_2_7 import FinalRuleGateReport
    from app.labs.short_barrier_debug_v8_2_7 import ShortBarrierDebugReportV2
    from app.labs.strict_oos_rule_selector_v8_2_7 import StrictOosSelectorReport

    for inst in [
        StrictOosSelectorReport(hours=1, generated_at="t"),
        ShortBarrierDebugReportV2(hours=1, generated_at="t"),
        FinalRuleGateReport(hours=1, generated_at="t"),
    ]:
        assert inst.research_only is True
        assert inst.paper_filter_enabled is False
        assert inst.can_send_real_orders is False
        assert inst.final_recommendation == "NO LIVE"
