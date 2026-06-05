"""V8.2.5 — Counterfactual Quality, Dedup, Short Sign and Score Calibration.

All tests use synthetic dataset rows (no DB / no OHLCV reads). Confirms:
- dedup reduces duplicates and recomputes metrics honestly.
- BNB-like artifact (raw winrate ~88% → dedup ~47%) is detected.
- SHORT classification works for legitimate / suspect / broken cases.
- Score anti-calibration detected.
- Cost stress reduces net EV as cost rises.
- Clean export sanitises and ZIPs only allowed file types.
- CLI parser still has no duplicate option strings after V8.2.5 additions.
- AST safety scan: no forbidden calls / literals.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import zipfile
from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Synthetic dataset rows builder
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
    final_use=True,
):
    return {
        "signal_id": id(ts),
        "timestamp": ts.isoformat(),
        "symbol": symbol,
        "side": side,
        "regime": regime,
        "score": score,
        "score_bucket": "70-79" if 70 <= score < 80 else "80-89" if 80 <= score < 90 else "90-100",
        "strategy": strategy,
        "reason": "",
        "blocked_by": "",
        "edgeguard_reason": "",
        "candidate_selected": False,
        "risk_approved": False,
        "entry_price": entry,
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
        "final_use_for_training": final_use,
        "research_only": True,
        "final_recommendation": "NO LIVE",
    }


# ---------------------------------------------------------------------------
# Dedup tests
# ---------------------------------------------------------------------------

def test_dedup_reduces_duplicate_rows():
    from app.labs.counterfactual_dedup_audit import audit_dedup

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start) for _ in range(10)]  # 10 identical rows.
    rows.append(_row(ts=start + timedelta(minutes=10)))  # 1 distinct.
    report = audit_dedup(None, rows=rows)
    assert report.evaluable_rows == 11
    assert report.unique_outcomes == 2
    assert report.duplicate_rows == 9
    assert report.duplicate_ratio == pytest.approx(9 / 11)


def test_raw_vs_dedup_metrics_differ_when_duplicates_exist():
    from app.labs.counterfactual_dedup_audit import audit_dedup

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start, net_pnl=0.5)] * 10
    rows.append(_row(ts=start + timedelta(minutes=10), net_pnl=-0.5))
    report = audit_dedup(None, rows=rows)
    # Raw mean is +0.41; dedup mean is 0.0 (one winner + one loser).
    assert report.raw_metrics["net_ev_avg_pct"] > 0.30
    assert abs(report.dedup_metrics["net_ev_avg_pct"]) < 0.01


def test_bnb_like_artifact_detected_with_mock():
    """BNB raw shows winrate 88% with +0.63%/trade, dedup collapses to 47%/-0.005."""
    from app.labs.counterfactual_dedup_audit import audit_dedup

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows: list[dict] = []
    # BNB: 8 identical winners (raw inflated) + 2 identical losers.
    for _ in range(8):
        rows.append(_row(ts=start, symbol="BNBUSDT", net_pnl=0.63,
                         ret_1h=0.5, ret_4h=1.0, mfe=1.0, mae=-0.2,
                         first_barrier="TP", baseline_result="TP"))
    for _ in range(2):
        rows.append(_row(
            ts=start + timedelta(minutes=10), symbol="BNBUSDT", net_pnl=-0.60,
            ret_1h=-0.5, ret_4h=-1.0, mfe=0.1, mae=-0.6,
            first_barrier="SL", baseline_result="SL",
        ))
    # Add some BTC rows that are NOT inflated.
    for i in range(20):
        ts = start + timedelta(minutes=15 + i * 5)
        rows.append(_row(ts=ts, symbol="BTCUSDT", net_pnl=0.10 if i % 2 else -0.05))
    report = audit_dedup(None, rows=rows)
    # The dedup must collapse BNB to 2 unique rows.
    bnb_entry = next(e for e in report.raw_vs_dedup_by_symbol if e["symbol"] == "BNBUSDT")
    assert bnb_entry["raw_count"] == 10
    assert bnb_entry["dedup_count"] == 2
    # Raw winrate 8/10 = 0.80; dedup 1/2 = 0.50 → drop 0.30.
    assert bnb_entry["winrate_drop"] >= 0.30 - 1e-9
    # And the inflated_symbols list must include BNB.
    assert any(e["symbol"] == "BNBUSDT" for e in report.inflated_symbols)


# ---------------------------------------------------------------------------
# Short sign / barrier audit
# ---------------------------------------------------------------------------

def test_short_with_favorable_4h_but_sl_flagged_as_short_sign_bug():
    from app.labs.short_sign_barrier_audit import (
        CLASS_SHORT_SIGN_BUG,
        VERDICT_SUSPECT,
        audit_short_sign,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # Suspicious: mfe < |mae| but both meaningful → SHORT_SIGN_BUG.
    rows = [
        _row(ts=start + timedelta(minutes=i), side="SHORT",
             ret_4h=-3.0, mfe=0.30, mae=-0.80,
             first_barrier="SL", baseline_result="SL",
             label="BAD_SHORT", net_pnl=-0.6)
        for i in range(10)
    ]
    # Add some non-suspicious shorts (no favourable ret_4h).
    rows += [
        _row(ts=start + timedelta(minutes=100 + i), side="SHORT",
             ret_4h=0.5, mfe=0.1, mae=-1.0,
             first_barrier="SL", baseline_result="SL",
             label="BAD_SHORT", net_pnl=-0.6)
        for i in range(5)
    ]
    r = audit_short_sign(None, rows=rows)
    assert r.by_classification.get(CLASS_SHORT_SIGN_BUG, 0) >= 1
    # 10/15 suspicious = 66.7% → BROKEN (> 0.40 threshold by default).
    assert r.verdict in {VERDICT_SUSPECT, "SHORT_LABELS_BROKEN"}


def test_short_legitimate_stop_before_drop_not_flagged_as_bug():
    """A SHORT with SL hit but MFE > |MAE| is legitimate stop-then-drop."""
    from app.labs.short_sign_barrier_audit import (
        CLASS_LEGITIMATE,
        CLASS_SHORT_SIGN_BUG,
        audit_short_sign,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(
        ts=start, side="SHORT",
        ret_4h=-1.0, mfe=2.0, mae=-0.6,   # MFE 2.0 > |MAE| 0.6 → legitimate
        first_barrier="SL", baseline_result="SL",
        label="BAD_SHORT", net_pnl=-0.6,
    )]
    r = audit_short_sign(None, rows=rows)
    assert r.by_classification.get(CLASS_LEGITIMATE, 0) == 1
    assert r.by_classification.get(CLASS_SHORT_SIGN_BUG, 0) == 0


def test_short_no_issue_when_first_barrier_is_tp():
    from app.labs.short_sign_barrier_audit import VERDICT_TRUSTED, audit_short_sign

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        _row(ts=start, side="SHORT",
             ret_4h=-1.5, mfe=2.0, mae=-0.2,
             first_barrier="TP", baseline_result="TP",
             label="GOOD_SHORT", net_pnl=0.6)
    ]
    r = audit_short_sign(None, rows=rows)
    assert r.verdict == VERDICT_TRUSTED


# ---------------------------------------------------------------------------
# Score calibration audit
# ---------------------------------------------------------------------------

def test_score_anti_calibration_detected():
    """High scores → LOWER outcomes ⇒ ANTI_CALIBRATED."""
    from app.labs.score_calibration_audit import (
        MONOTONIC_ANTI,
        audit_score_calibration,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows: list[dict] = []
    # Score 60 → +0.5, 70 → +0.3, 80 → +0.1, 90 → -0.2.
    for i in range(20):
        ts = start + timedelta(minutes=5 * i)
        rows.append(_row(ts=ts, score=60, net_pnl=0.5))
    for i in range(20):
        ts = start + timedelta(minutes=5 * (i + 25))
        rows.append(_row(ts=ts, score=70, net_pnl=0.3))
    for i in range(20):
        ts = start + timedelta(minutes=5 * (i + 50))
        rows.append(_row(ts=ts, score=80, net_pnl=0.1))
    for i in range(20):
        ts = start + timedelta(minutes=5 * (i + 75))
        rows.append(_row(ts=ts, score=90, net_pnl=-0.2))
    r = audit_score_calibration(None, dedup=False, rows=rows)
    assert r.monotonicity_status == MONOTONIC_ANTI
    assert any("anti-calibrated" in w.lower() for w in r.warnings)


def test_score_monotonic_pass_when_higher_score_higher_outcome():
    from app.labs.score_calibration_audit import (
        MONOTONIC_PASS,
        audit_score_calibration,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows: list[dict] = []
    for i in range(20):
        rows.append(_row(ts=start + timedelta(minutes=5 * i), score=60, net_pnl=-0.2))
    for i in range(20):
        rows.append(_row(ts=start + timedelta(minutes=5 * (i + 25)), score=70, net_pnl=0.1))
    for i in range(20):
        rows.append(_row(ts=start + timedelta(minutes=5 * (i + 50)), score=80, net_pnl=0.4))
    for i in range(20):
        rows.append(_row(ts=start + timedelta(minutes=5 * (i + 75)), score=90, net_pnl=0.7))
    r = audit_score_calibration(None, dedup=False, rows=rows)
    assert r.monotonicity_status == MONOTONIC_PASS


# ---------------------------------------------------------------------------
# Cost stress
# ---------------------------------------------------------------------------

def test_cost_stress_reduces_net_ev_as_cost_rises():
    from app.labs.counterfactual_cost_stress import stress_costs

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        _row(ts=start + timedelta(minutes=5 * i), gross=0.40 if i % 2 else -0.20,
             net_pnl=0.20 if i % 2 else -0.40)
        for i in range(40)
    ]
    r = stress_costs(None, rows=rows, dedup=False)
    # The cost_levels list is sorted ascending; net_ev must monotonically decrease.
    nets = [entry["net_ev_avg_pct"] for entry in r.by_cost_level]
    assert all(nets[i] >= nets[i + 1] for i in range(len(nets) - 1))


def test_cost_stress_marks_optimistic_only_groups():
    from app.labs.counterfactual_cost_stress import stress_costs

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # Group with gross 0.20%: positive at 0.18 cost, negative from 0.20+.
    rows = [
        _row(ts=start + timedelta(minutes=5 * i), symbol="XYZUSDT",
             side="LONG", regime="RISK_ON", strategy="STRAT",
             label="GOOD_LONG", gross=0.20, net_pnl=0.02)
        for i in range(15)
    ]
    r = stress_costs(None, rows=rows, dedup=False)
    # Survives 0.18% (0.20-0.18=0.02 > 0), fails 0.20% upward.
    assert any(
        g.get("symbol") == "XYZUSDT" for g in r.optimistic_only_groups
    )


# ---------------------------------------------------------------------------
# Clean export V2 + ZIP allow-list
# ---------------------------------------------------------------------------

def test_clean_export_v2_contains_only_csv_txt_json(tmp_path):
    from app.labs.counterfactual_clean_export_v2 import export_clean_v2

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(minutes=5 * i)) for i in range(20)]
    base = tmp_path / "ctd_v2"
    manifest = export_clean_v2(None, base_dir=base, rows=rows)
    zip_path = base / "research_v8_2_5_exports.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            assert name.endswith((".csv", ".txt", ".json")), f"forbidden in ZIP: {name}"
    # No `.env`/`.db` in base dir.
    for path in base.iterdir():
        assert path.suffix in {".csv", ".txt", ".json", ".zip"}


def test_clean_export_v2_does_not_leak_secrets(tmp_path):
    from app.labs.counterfactual_clean_export_v2 import export_clean_v2

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start)]
    rows[0]["bitget_api_secret"] = "SHOULD_BE_REDACTED"
    base = tmp_path / "ctd_v2_secrets"
    export_clean_v2(None, base_dir=base, rows=rows)
    main_csv = base / "counterfactual_training_dataset_dedup_v2.csv"
    text = main_csv.read_text(encoding="utf-8")
    assert "SHOULD_BE_REDACTED" not in text
    assert "bitget_api_secret" not in text.lower()


# ---------------------------------------------------------------------------
# CLI parser regression
# ---------------------------------------------------------------------------

def test_v82_5_cli_commands_parse():
    from app.research_lab import build_argument_parser

    parser = build_argument_parser()
    for argv in [
        ["counterfactual-dedup-audit", "--hours", "168"],
        ["short-sign-barrier-audit", "--hours", "168"],
        ["score-calibration-audit", "--hours", "168"],
        ["counterfactual-cost-stress", "--hours", "168"],
        ["export-counterfactual-clean-v2", "--hours", "168"],
        ["research-pack-counterfactual-quality-v1", "--hours", "168"],
    ]:
        ns = parser.parse_args(argv)
        assert ns.command == argv[0]


def test_parser_has_no_duplicate_option_strings():
    """Regression of the V8.2.3 fix: no duplicate option strings after
    V8.2.5 additions.
    """
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

V82_5_MODULES = [
    "app.labs.counterfactual_dedup_audit",
    "app.labs.short_sign_barrier_audit",
    "app.labs.score_calibration_audit",
    "app.labs.counterfactual_cost_stress",
    "app.labs.counterfactual_clean_export_v2",
]


def test_v82_5_modules_have_no_forbidden_calls():
    for mod in V82_5_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name not in FORBIDDEN_CALL_NAMES, f"{mod} calls {name}"


def test_v82_5_modules_have_no_forbidden_literal_true_assigns():
    for mod in V82_5_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                    if name in FORBIDDEN_ASSIGN_LITERALS and isinstance(node.value, ast.Constant):
                        assert node.value.value is not True, f"{mod} {name}=True"


def test_v82_5_outputs_carry_no_live():
    from app.labs.counterfactual_cost_stress import CostStressReport
    from app.labs.counterfactual_dedup_audit import DedupAuditReport
    from app.labs.score_calibration_audit import ScoreCalibrationReport
    from app.labs.short_sign_barrier_audit import ShortSignAuditReport

    for inst in [
        DedupAuditReport(hours=1, generated_at="t"),
        ShortSignAuditReport(hours=1, generated_at="t"),
        ScoreCalibrationReport(hours=1, generated_at="t"),
        CostStressReport(hours=1, generated_at="t"),
    ]:
        assert inst.research_only is True
        assert inst.paper_filter_enabled is False
        assert inst.can_send_real_orders is False
        assert inst.final_recommendation == "NO LIVE"
