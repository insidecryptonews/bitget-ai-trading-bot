"""V8.2.6 — Candidate Rule Miner + OOS/Walk-forward + Short Barrier Debug
+ Score Recalibration Sandbox.

All tests use synthetic rows. No DB / no OHLCV reads.
"""

from __future__ import annotations

import ast
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
    regime="RISK_OFF",
    score=80,
    strategy="TEST",
    entry=100.0,
    ret_1h=0.5,
    ret_4h=1.0,
    ret_24h=2.0,
    mfe=1.0,
    mae=-0.2,
    first_barrier="TP",
    baseline_result="TP",
    net_pnl=0.50,
    gross=0.96,
    label="GOOD_LONG",
    candidate_selected=False,
    risk_approved=False,
    stop_loss=None,
    take_profit_1=None,
    take_profit_2=None,
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
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "ohlcv_available": True,
        "ret_15m_pct": ret_1h * 0.5,
        "ret_30m_pct": ret_1h * 0.75,
        "ret_1h_pct": ret_1h,
        "ret_4h_pct": ret_4h,
        "ret_24h_pct": ret_24h,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "first_barrier_hit": first_barrier,
        "tp_before_sl": first_barrier == "TP",
        "sl_before_tp": first_barrier == "SL",
        "baseline_result": baseline_result,
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
# Candidate Rule Miner — forbidden features and gates
# ---------------------------------------------------------------------------

def test_miner_refuses_forbidden_features():
    """Passing ``first_barrier_hit`` or ``training_label`` as a grouping
    feature must raise — otherwise the rule miner would be learning from the
    label.
    """
    from app.labs.candidate_rule_miner_v8_2_6 import mine_candidate_rules

    for forbidden in ("training_label", "first_barrier_hit", "baseline_net_pnl_est",
                      "ret_1h_pct", "mfe_pct"):
        with pytest.raises(ValueError):
            mine_candidate_rules(None, rows=[], grouping_features=(forbidden,))


def test_miner_uses_only_ex_ante_features_by_default():
    from app.labs.candidate_rule_miner_v8_2_6 import (
        EX_ANTE_FEATURES,
        EX_POST_LABELS,
    )

    forbidden = set(EX_POST_LABELS)
    for feature in EX_ANTE_FEATURES:
        assert feature not in forbidden


def test_miner_rejects_low_sample_groups():
    from app.labs.candidate_rule_miner_v8_2_6 import (
        STATUS_REJECT,
        mine_candidate_rules,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(minutes=5 * i), symbol="XYZUSDT")
            for i in range(5)]
    r = mine_candidate_rules(None, rows=rows)
    assert r.total_rules == 1
    rule = (r.candidate_rules + r.watch_only_rules + r.rejected_rules)[0]
    assert rule["rule_status"] == STATUS_REJECT
    assert "samples" in rule["rule_reason"]


def test_miner_rejects_negative_net_ev_after_realistic_cost():
    from app.labs.candidate_rule_miner_v8_2_6 import (
        STATUS_REJECT,
        mine_candidate_rules,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # Gross 0.20 with 25 samples → realistic cost (0.25) → -0.05 mean.
    rows = [
        _row(ts=start + timedelta(minutes=5 * i), symbol="XYZUSDT",
             gross=0.20, net_pnl=0.02)
        for i in range(25)
    ]
    r = mine_candidate_rules(None, rows=rows)
    rule = (r.candidate_rules + r.watch_only_rules + r.rejected_rules)[0]
    assert rule["rule_status"] == STATUS_REJECT
    assert "cost_realistic" in rule["rule_reason"]


def test_miner_marks_watch_only_when_cost_stress_fragile():
    from app.labs.candidate_rule_miner_v8_2_6 import (
        STATUS_WATCH_ONLY,
        mine_candidate_rules,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # 25 winners (gross 0.40) + 5 losers (gross -0.10). Average gross ≈ 0.317.
    # cost_realistic (0.25) → +0.067 (survives); cost_stress (0.35) → -0.033
    # (fragile). Mix of wins/losses keeps PF well above 1.2.
    # Spread one trade per hour so no timestamp cluster exceeds 30%.
    rows = [
        _row(ts=start + timedelta(hours=i), symbol="XYZUSDT",
             regime="RISK_OFF", strategy="TEST", score=80,
             gross=0.40, net_pnl=0.22)
        for i in range(25)
    ]
    rows += [
        _row(ts=start + timedelta(hours=100 + i), symbol="XYZUSDT",
             regime="RISK_OFF", strategy="TEST", score=80,
             gross=-0.10, net_pnl=-0.28)
        for i in range(5)
    ]
    r = mine_candidate_rules(None, rows=rows, score_calibration_ok=True)
    rule = (r.candidate_rules + r.watch_only_rules + r.rejected_rules)[0]
    assert rule["rule_status"] == STATUS_WATCH_ONLY
    assert "fragile" in rule["rule_reason"] or "stress" in rule["rule_reason"]


def test_miner_excludes_short_when_short_verdict_not_safe():
    from app.labs.candidate_rule_miner_v8_2_6 import mine_candidate_rules

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        _row(ts=start + timedelta(minutes=5 * i), side="SHORT", symbol="ETHUSDT")
        for i in range(40)
    ]
    rows += [
        _row(ts=start + timedelta(minutes=5 * (200 + i)), side="LONG", symbol="BTCUSDT")
        for i in range(40)
    ]
    r = mine_candidate_rules(
        None, rows=rows, short_verdict="SHORT_LABELS_SUSPECT",
    )
    assert r.short_excluded is True
    # No rule should have side=SHORT.
    all_rules = r.candidate_rules + r.watch_only_rules + r.rejected_rules
    for rule in all_rules:
        features = rule["features"]
        assert str(features.get("side")).upper() != "SHORT"


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------

def test_walkforward_fails_when_train_wins_but_test_loses():
    from app.labs.candidate_rule_walkforward_v8_2_6 import (
        WF_FAIL,
        run_walkforward,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # First 60% (train) winners; last 40% (test) losers.
    train_rows = [
        _row(ts=start + timedelta(minutes=5 * i), symbol="XYZUSDT",
             gross=0.96, net_pnl=0.70)
        for i in range(30)
    ]
    test_rows = [
        _row(ts=start + timedelta(minutes=5 * (300 + i)), symbol="XYZUSDT",
             gross=-0.60, net_pnl=-0.80)
        for i in range(20)
    ]
    rules = [{
        "rule_id": "test",
        "features": {
            "symbol": "XYZUSDT", "side": "LONG", "regime": "RISK_OFF",
            "strategy": "TEST", "score_bucket": "80-89",
            "candidate_selected": False, "risk_approved": False,
        },
    }]
    r = run_walkforward(None, rows=train_rows + test_rows, rules=rules)
    assert r.rules_evaluated == 1
    assert r.results[0]["decision"] == WF_FAIL


def test_walkforward_need_more_data_with_few_samples():
    from app.labs.candidate_rule_walkforward_v8_2_6 import (
        WF_NEED_MORE_DATA,
        run_walkforward,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(minutes=5 * i)) for i in range(8)]
    rules = [{
        "rule_id": "tiny",
        "features": {
            "symbol": "BTCUSDT", "side": "LONG", "regime": "RISK_OFF",
            "strategy": "TEST", "score_bucket": "80-89",
            "candidate_selected": False, "risk_approved": False,
        },
    }]
    r = run_walkforward(None, rows=rows, rules=rules)
    assert r.results[0]["decision"] == WF_NEED_MORE_DATA


# ---------------------------------------------------------------------------
# Short barrier debug
# ---------------------------------------------------------------------------

def test_short_barrier_debug_classifies_legitimate_vs_suspect():
    from app.labs.short_barrier_debug_v8_2_6 import (
        SHORT_EXCLUDE,
        SHORT_SAFE,
        debug_short_barriers,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        _row(ts=start, side="SHORT", first_barrier="TP", net_pnl=0.5,
             ret_4h=-1.5, mfe=2.0, mae=-0.3),  # trusted
        _row(ts=start + timedelta(minutes=5), side="SHORT",
             first_barrier="SL", net_pnl=-0.5,
             ret_4h=-3.0, mfe=2.5, mae=-0.6),  # legitimate
        _row(ts=start + timedelta(minutes=10), side="SHORT",
             first_barrier="SL", net_pnl=-0.5,
             ret_4h=-3.0, mfe=0.30, mae=-0.80),  # sign_bug
    ]
    r = debug_short_barriers(None, rows=rows)
    assert r.total_short_rows == 3
    assert r.trusted_count >= 1
    assert r.legitimate_stop_before_drop >= 1
    assert r.possible_sign_bug >= 1
    assert r.verdict in {SHORT_EXCLUDE, SHORT_SAFE, "SHORT_BROKEN_FIX_REQUIRED"}


def test_short_debug_orientation_check():
    """If MFE is negative or MAE positive, orientation is flagged."""
    from app.labs.short_barrier_debug_v8_2_6 import _orientation_ok_for_short

    assert _orientation_ok_for_short(2.0, -0.3) is True
    assert _orientation_ok_for_short(-2.0, -0.3) is False
    assert _orientation_ok_for_short(2.0, 0.3) is False


# ---------------------------------------------------------------------------
# Score recalibration sandbox
# ---------------------------------------------------------------------------

def test_score_recalibration_does_not_touch_production():
    """The sandbox must NOT import or call ``signal_engine`` /
    ``regime_detector`` / ``paper_trader``.
    """
    src = pathlib.Path(
        importlib.import_module("app.labs.score_recalibration_sandbox_v8_2_6").__file__
    ).read_text(encoding="utf-8")
    for forbidden in ("signal_engine", "regime_detector", "paper_trader"):
        assert forbidden not in src, f"sandbox must not import {forbidden}"


def test_score_recalibration_returns_recommendation_with_anti_calibration():
    from app.labs.score_recalibration_sandbox_v8_2_6 import sandbox_recalibration

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows: list[dict] = []
    # Anti-calibrated: high score → bad outcomes.
    for i in range(20):
        rows.append(_row(ts=start + timedelta(minutes=5 * i),
                         symbol=f"A{i}USDT", score=90, net_pnl=-0.2))
    for i in range(20):
        rows.append(_row(ts=start + timedelta(minutes=5 * (50 + i)),
                         symbol=f"B{i}USDT", score=60, net_pnl=0.5))
    r = sandbox_recalibration(None, rows=rows)
    assert r.samples >= 30
    assert r.recommendation in {
        "KEEP_SCORE_DISABLED_AS_GATE", "SCORE_RECALIBRATION_CANDIDATE",
        "SCORE_NOT_USEFUL",
    }


# ---------------------------------------------------------------------------
# Export V8.2.6 — sanitisation and ZIP allow-list
# ---------------------------------------------------------------------------

def test_export_v826_contains_only_csv_txt_json(tmp_path):
    from app.labs.research_export_v8_2_6 import export_research_v826

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(minutes=5 * i)) for i in range(20)]
    base = tmp_path / "v826"
    export_research_v826(None, rows=rows, base_dir=base)
    zip_path = base / "research_v8_2_6_exports.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            assert name.endswith((".csv", ".txt", ".json"))


def test_export_v826_does_not_leak_secrets(tmp_path):
    from app.labs.research_export_v8_2_6 import export_research_v826

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start)]
    rows[0]["bitget_api_secret"] = "SHOULD_BE_REDACTED"
    base = tmp_path / "v826_sec"
    export_research_v826(None, rows=rows, base_dir=base)
    candidate_csv = base / "candidate_rules_v1.csv"
    if candidate_csv.exists():
        text = candidate_csv.read_text(encoding="utf-8")
        assert "SHOULD_BE_REDACTED" not in text
        assert "bitget_api_secret" not in text.lower()


# ---------------------------------------------------------------------------
# CLI parser regression
# ---------------------------------------------------------------------------

def test_v826_cli_commands_parse():
    from app.research_lab import build_argument_parser

    parser = build_argument_parser()
    for argv in [
        ["candidate-rule-miner-v826", "--hours", "168"],
        ["candidate-rule-walkforward-v826", "--hours", "168"],
        ["short-barrier-debug-v826", "--hours", "168"],
        ["score-recalibration-sandbox-v826", "--hours", "168"],
        ["export-research-v826", "--hours", "168"],
        ["research-pack-v826", "--hours", "168"],
    ]:
        ns = parser.parse_args(argv)
        assert ns.command == argv[0]


def test_parser_has_no_duplicate_option_strings_after_v826():
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

V826_MODULES = [
    "app.labs.candidate_rule_miner_v8_2_6",
    "app.labs.candidate_rule_walkforward_v8_2_6",
    "app.labs.short_barrier_debug_v8_2_6",
    "app.labs.score_recalibration_sandbox_v8_2_6",
    "app.labs.research_export_v8_2_6",
]


def test_v826_modules_have_no_forbidden_calls():
    for mod in V826_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name not in FORBIDDEN_CALL_NAMES, f"{mod} calls {name}"


def test_v826_modules_have_no_forbidden_literal_true_assigns():
    for mod in V826_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                    if name in FORBIDDEN_ASSIGN_LITERALS and isinstance(node.value, ast.Constant):
                        assert node.value.value is not True, f"{mod} {name}=True"


def test_v826_outputs_carry_no_live():
    from app.labs.candidate_rule_miner_v8_2_6 import CandidateRuleMinerReport
    from app.labs.candidate_rule_walkforward_v8_2_6 import WalkForwardReport
    from app.labs.score_recalibration_sandbox_v8_2_6 import ScoreRecalibrationReport
    from app.labs.short_barrier_debug_v8_2_6 import ShortBarrierDebugReport

    for inst in [
        CandidateRuleMinerReport(hours=1, generated_at="t"),
        WalkForwardReport(hours=1, generated_at="t"),
        ScoreRecalibrationReport(hours=1, generated_at="t"),
        ShortBarrierDebugReport(hours=1, generated_at="t"),
    ]:
        assert inst.research_only is True
        assert inst.paper_filter_enabled is False
        assert inst.can_send_real_orders is False
        assert inst.final_recommendation == "NO LIVE"
