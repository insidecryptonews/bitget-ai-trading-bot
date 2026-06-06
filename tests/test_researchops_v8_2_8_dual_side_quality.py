"""V8.2.8 — Dual-side root cause fix tests.

All tests use synthetic dataset rows. No DB, no OHLCV reads.
"""

from __future__ import annotations

import ast
import csv
import importlib
import pathlib
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
    strategy: str = "TEST",
    entry: float = 100.0,
    tp: float = 101.0,
    sl: float = 99.0,
    ret_1h: float = 0.5,
    ret_4h: float = 1.0,
    mfe: float = 1.0,
    mae: float = -0.2,
    first_barrier: str = "TP",
    net_pnl: float = 0.50,
    gross: float = 0.96,
    label: str = "GOOD_LONG",
    normalized_atr: float = 0.02,
) -> dict[str, Any]:
    bucket = (
        "90-100" if score >= 90 else
        "80-89" if score >= 80 else
        "70-79" if score >= 70 else
        "60-69" if score >= 60 else "<60"
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
        "strategy": strategy,
        "reason": "",
        "blocked_by": "",
        "edgeguard_reason": "",
        "candidate_selected": False,
        "risk_approved": False,
        "entry_price": entry,
        "take_profit_1": tp,
        "stop_loss": sl,
        "normalized_atr": normalized_atr,
        "ohlcv_available": True,
        "ret_15m_pct": ret_1h * 0.5,
        "ret_30m_pct": ret_1h * 0.75,
        "ret_1h_pct": ret_1h,
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
# Dual-side barrier audit
# ---------------------------------------------------------------------------

def test_long_tp_sl_orientation_correct():
    """LONG TP above entry and SL below entry → no_issue when classified."""
    from app.labs.dual_side_barrier_truth_audit_v8_2_8 import (
        LONG_NO_ISSUE,
        _classify_long,
    )

    row = _row(side="LONG", entry=100, tp=101, sl=99,
               first_barrier="TP", ret_4h=1.0, mfe=1.5, mae=-0.2)
    classification, _ = _classify_long(row)
    assert classification == LONG_NO_ISSUE


def test_short_tp_sl_orientation_correct():
    """SHORT TP below entry and SL above entry → no_issue when classified."""
    from app.labs.dual_side_barrier_truth_audit_v8_2_8 import (
        SHORT_NO_ISSUE,
        _classify_short,
    )

    row = _row(side="SHORT", entry=100, tp=99, sl=101,
               first_barrier="TP", ret_4h=-1.0, mfe=1.5, mae=-0.2)
    classification, _ = _classify_short(row)
    assert classification == SHORT_NO_ISSUE


def test_long_barrier_inverted_detected():
    """LONG with TP < entry and SL > entry is barrier-inverted."""
    from app.labs.dual_side_barrier_truth_audit_v8_2_8 import (
        LONG_BARRIER_INVERTED,
        _classify_long,
    )

    row = _row(side="LONG", entry=100, tp=99, sl=101, first_barrier="SL")
    classification, notes = _classify_long(row)
    assert classification == LONG_BARRIER_INVERTED
    assert "inverted" in notes.lower()


def test_short_barrier_inverted_detected():
    """SHORT with TP > entry and SL < entry is barrier-inverted."""
    from app.labs.dual_side_barrier_truth_audit_v8_2_8 import (
        SHORT_BARRIER_INVERTED,
        _classify_short,
    )

    row = _row(side="SHORT", entry=100, tp=101, sl=99, first_barrier="SL")
    classification, notes = _classify_short(row)
    assert classification == SHORT_BARRIER_INVERTED
    assert "inverted" in notes.lower()


def test_long_favorable_move_but_sl_classified():
    """LONG with ret_4h positive but SL hit → suspicious classification."""
    from app.labs.dual_side_barrier_truth_audit_v8_2_8 import (
        LONG_LEGITIMATE,
        LONG_POSSIBLE_LABEL_BUG,
        _classify_long,
    )

    # Legitimate stop-then-rise: MFE > |MAE|.
    legit = _row(side="LONG", entry=100, tp=101, sl=99,
                 first_barrier="SL", ret_4h=2.0, mfe=2.0, mae=-0.6)
    c1, _ = _classify_long(legit)
    assert c1 == LONG_LEGITIMATE

    # Possible label bug: MFE < |MAE| with favourable ret_4h.
    bug = _row(side="LONG", entry=100, tp=101, sl=99,
               first_barrier="SL", ret_4h=2.0, mfe=0.30, mae=-0.80)
    c2, _ = _classify_long(bug)
    assert c2 == LONG_POSSIBLE_LABEL_BUG


def test_short_favorable_move_but_sl_classified():
    from app.labs.dual_side_barrier_truth_audit_v8_2_8 import (
        SHORT_LEGITIMATE,
        SHORT_POSSIBLE_LABEL_BUG,
        _classify_short,
    )

    legit = _row(side="SHORT", entry=100, tp=99, sl=101,
                 first_barrier="SL", ret_4h=-2.0, mfe=2.0, mae=-0.6)
    c1, _ = _classify_short(legit)
    assert c1 == SHORT_LEGITIMATE

    bug = _row(side="SHORT", entry=100, tp=99, sl=101,
               first_barrier="SL", ret_4h=-2.0, mfe=0.30, mae=-0.80)
    c2, _ = _classify_short(bug)
    assert c2 == SHORT_POSSIBLE_LABEL_BUG


def test_audit_dual_side_emits_two_verdicts():
    from app.labs.dual_side_barrier_truth_audit_v8_2_8 import audit_dual_side_barriers

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        _row(ts=start + timedelta(minutes=i * 5), side="LONG", entry=100, tp=101, sl=99)
        for i in range(10)
    ] + [
        _row(ts=start + timedelta(minutes=100 + i * 5), side="SHORT",
             entry=100, tp=99, sl=101, ret_4h=-1.0, mfe=1.5, mae=-0.2)
        for i in range(10)
    ]
    r = audit_dual_side_barriers(None, rows=rows)
    assert r.long_metrics.evaluable_rows == 10
    assert r.short_metrics.evaluable_rows == 10
    assert r.long_verdict in {
        "LONG_SAFE_TO_USE_FOR_RESEARCH",
        "LONG_EXCLUDE_FROM_RULE_MINING",
        "LONG_BROKEN_FIX_REQUIRED",
    }
    assert r.short_verdict in {
        "SHORT_SAFE_TO_USE_FOR_RESEARCH",
        "SHORT_EXCLUDE_FROM_RULE_MINING",
        "SHORT_BROKEN_FIX_REQUIRED",
    }


# ---------------------------------------------------------------------------
# Duplicate root cause
# ---------------------------------------------------------------------------

def test_duplicate_root_cause_detects_repeated_cycle_logging():
    from app.labs.duplicate_source_root_cause_v8_2_8 import (
        ROOT_CAUSE_REPEATED_CYCLE,
        _classify_root_cause,
    )

    same_ts = "2026-06-01T00:00:00+00:00"
    rows = [
        {"timestamp": same_ts, "received_at": f"2026-06-01T00:00:{i:02d}+00:00",
         "symbol": "BTCUSDT", "side": "LONG", "reason": ""}
        for i in range(3)
    ]
    cause, _ = _classify_root_cause(rows)
    assert cause == ROOT_CAUSE_REPEATED_CYCLE


def test_duplicate_root_cause_detects_same_bar_resampling():
    from app.labs.duplicate_source_root_cause_v8_2_8 import (
        ROOT_CAUSE_SAME_BAR_RESAMPLING,
        _classify_root_cause,
    )

    same_ts = "2026-06-01T00:00:00+00:00"
    rows = [
        {"timestamp": same_ts, "symbol": "BTCUSDT", "side": "LONG", "reason": ""}
        for _ in range(5)
    ]
    cause, _ = _classify_root_cause(rows)
    assert cause == ROOT_CAUSE_SAME_BAR_RESAMPLING


def test_duplicate_root_cause_full_audit_proposes_fixes(monkeypatch):
    from app.labs.duplicate_source_root_cause_v8_2_8 import audit_duplicate_root_cause

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    same_ts = start.isoformat()
    rows = [
        _row(ts=start, symbol="BTCUSDT", side="LONG") | {"timestamp": same_ts}
        for _ in range(20)
    ]
    r = audit_duplicate_root_cause(None, rows=rows)
    assert r.evaluable_rows == 20
    assert r.duplicate_ratio > 0.5
    assert r.proposed_fixes  # at least one fix proposed


# ---------------------------------------------------------------------------
# Side-aware score calibration
# ---------------------------------------------------------------------------

def test_side_aware_score_detects_useful_long():
    from app.labs.side_aware_score_calibration_v8_2_8 import (
        SCORE_USEFUL_LONG,
        calibrate_score_by_side,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows: list[dict] = []
    for i in range(20):
        rows.append(_row(ts=start + timedelta(hours=i), side="LONG",
                         score=60, net_pnl=-0.2))
    for i in range(20):
        rows.append(_row(ts=start + timedelta(hours=25 + i), side="LONG",
                         score=70, net_pnl=0.05))
    for i in range(20):
        rows.append(_row(ts=start + timedelta(hours=50 + i), side="LONG",
                         score=80, net_pnl=0.30))
    for i in range(20):
        rows.append(_row(ts=start + timedelta(hours=75 + i), side="LONG",
                         score=90, net_pnl=0.60))
    r = calibrate_score_by_side(None, rows=rows)
    assert r.long_block.usefulness == SCORE_USEFUL_LONG
    assert r.score_usable_long is True


def test_side_aware_score_detects_anti_calibrated_short():
    from app.labs.side_aware_score_calibration_v8_2_8 import (
        SCORE_ANTI_CALIBRATED,
        calibrate_score_by_side,
    )

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows: list[dict] = []
    for i in range(20):
        rows.append(_row(ts=start + timedelta(hours=i), side="SHORT",
                         score=60, net_pnl=0.50))
    for i in range(20):
        rows.append(_row(ts=start + timedelta(hours=25 + i), side="SHORT",
                         score=70, net_pnl=0.30))
    for i in range(20):
        rows.append(_row(ts=start + timedelta(hours=50 + i), side="SHORT",
                         score=80, net_pnl=0.10))
    for i in range(20):
        rows.append(_row(ts=start + timedelta(hours=75 + i), side="SHORT",
                         score=90, net_pnl=-0.20))
    r = calibrate_score_by_side(None, rows=rows)
    assert r.short_block.usefulness == SCORE_ANTI_CALIBRATED


# ---------------------------------------------------------------------------
# Rebound lab
# ---------------------------------------------------------------------------

def test_rebound_lab_detects_candidate_with_mock():
    from app.labs.rebound_regime_turn_lab_v8_2_8 import detect_rebound_setups

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows: list[dict] = []
    # 10 prior rows in TREND_DOWN with rising ret_1h.
    for i in range(10):
        rows.append(_row(
            ts=start + timedelta(hours=i),
            symbol="BTCUSDT", side="LONG", regime="TREND_DOWN",
            ret_1h=0.5 if i >= 7 else -0.3,
            net_pnl=-0.20,
        ))
    # Rebound LONG with regime flipped to TREND_UP.
    for i in range(30):
        rows.append(_row(
            ts=start + timedelta(hours=10 + i),
            symbol="BTCUSDT", side="LONG", regime="TREND_UP",
            ret_1h=0.5, ret_4h=1.5, net_pnl=0.40,
        ))
    r = detect_rebound_setups(None, rows=rows)
    assert r.rebound_candidates_count >= 1


def test_rebound_lab_rejects_without_prior_down_regime():
    from app.labs.rebound_regime_turn_lab_v8_2_8 import detect_rebound_setups

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        _row(ts=start + timedelta(hours=i), symbol="BTCUSDT",
             side="LONG", regime="TREND_UP", net_pnl=0.40)
        for i in range(30)
    ]
    r = detect_rebound_setups(None, rows=rows)
    # No prior DOWN regime → no rebound candidates.
    assert r.rebound_candidates_count == 0


# ---------------------------------------------------------------------------
# V8.2.8.1 hotfix — prefix-only rebound detection
# ---------------------------------------------------------------------------

def _rebound_detector_source() -> str:
    import inspect
    from app.labs.rebound_regime_turn_lab_v8_2_8 import (
        _build_prefix_context,
        detect_rebound_candidate_prefix_only,
    )
    return (
        inspect.getsource(detect_rebound_candidate_prefix_only)
        + "\n"
        + inspect.getsource(_build_prefix_context)
    )


def _detector_string_literals_excluding_docstrings() -> list[str]:
    """Return string literals reachable from the prefix-only detector's
    code path, *excluding* function docstrings.

    Docstrings document the negative space (which fields are forbidden as
    inputs) and intentionally contain literal field names. We skip them
    so the AST scan only inspects executable code.
    """
    import inspect
    from app.labs.rebound_regime_turn_lab_v8_2_8 import (
        _build_prefix_context,
        detect_rebound_candidate_prefix_only,
    )
    out: list[str] = []
    for fn in (detect_rebound_candidate_prefix_only, _build_prefix_context):
        tree = ast.parse(inspect.getsource(fn))
        # FunctionDef is the only top-level node.
        func_def = tree.body[0]
        body = list(getattr(func_def, "body", []))
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        for stmt in body:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    out.append(node.value)
    return out


def test_rebound_detector_does_not_reference_ret_1h_or_ret_4h():
    """V8.2.8.1: detector must not read ret_1h_pct or ret_4h_pct."""
    literals = _detector_string_literals_excluding_docstrings()
    assert "ret_1h_pct" not in literals, "prefix-only detector references ret_1h_pct"
    assert "ret_4h_pct" not in literals, "prefix-only detector references ret_4h_pct"


def test_rebound_detector_does_not_reference_any_ret_field():
    """V8.2.8.1: detector must not read any ret_*_pct field as input."""
    literals = _detector_string_literals_excluding_docstrings()
    offending = [s for s in literals if s.startswith("ret_") and s.endswith("_pct")]
    assert not offending, f"prefix-only detector references future returns: {offending}"


def test_rebound_detector_does_not_reference_mfe_mae_barrier_baseline():
    """V8.2.8.1: detector must not read ex-post outcome fields as input."""
    forbidden = {
        "mfe_pct", "mae_pct",
        "first_barrier_hit", "tp_before_sl", "sl_before_tp",
        "baseline_result", "baseline_gross_pnl", "baseline_net_pnl_est",
        "trailing_result", "trailing_net_pnl_est",
        "campaign_result", "campaign_net_pnl_est",
        "would_have_worked_baseline", "would_have_worked_trailing",
        "would_have_worked_campaign",
        "training_label",
    }
    literals = _detector_string_literals_excluding_docstrings()
    offending = [s for s in literals if s in forbidden]
    assert not offending, (
        f"prefix-only detector references forbidden ex-post fields: {offending}"
    )


def test_rebound_returns_need_data_when_no_prefix_context():
    """V8.2.8.1: rows without prior history → REBOUND_NEED_MORE_DATA."""
    from app.labs.rebound_regime_turn_lab_v8_2_8 import (
        DETECTION_MODE_NEED_DATA,
        REBOUND_NEED_MORE_DATA,
        detect_rebound_setups,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # Single LONG row, no prior history of the same symbol.
    rows = [_row(ts=start, symbol="BTCUSDT", side="LONG", regime="TREND_UP")]
    r = detect_rebound_setups(None, rows=rows)
    assert r.readiness == REBOUND_NEED_MORE_DATA
    assert r.rebound_candidates_count == 0
    assert r.used_future_return_features is False
    assert r.report_detection_mode == DETECTION_MODE_NEED_DATA
    assert r.need_data_count >= 1


def test_rebound_returns_need_data_when_only_ret_features_present():
    """V8.2.8.1: presence of ret_* alone (no prefix history) must not
    create candidates. The detector must ignore ret_* and report
    REBOUND_NEED_MORE_DATA."""
    from app.labs.rebound_regime_turn_lab_v8_2_8 import (
        REBOUND_NEED_MORE_DATA,
        detect_rebound_setups,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # Two LONG rows on different symbols — each row has rich ret_* values
    # but no prior history of the same symbol.
    rows = [
        _row(ts=start + timedelta(hours=0), symbol="BTCUSDT", side="LONG",
             regime="TREND_UP", ret_1h=2.5, ret_4h=5.0),
        _row(ts=start + timedelta(hours=1), symbol="ETHUSDT", side="LONG",
             regime="TREND_UP", ret_1h=3.0, ret_4h=6.0),
    ]
    r = detect_rebound_setups(None, rows=rows)
    assert r.rebound_candidates_count == 0
    assert r.readiness == REBOUND_NEED_MORE_DATA
    assert r.used_future_return_features is False


def test_rebound_detects_candidate_with_prefix_only_context():
    """V8.2.8.1: with prefix-only regime history and entry-price ladder,
    detector finds at least one LONG rebound candidate after the regime
    flips from TREND_DOWN to TREND_UP."""
    from app.labs.rebound_regime_turn_lab_v8_2_8 import (
        DETECTION_MODE_PREFIX_ONLY,
        detect_rebound_setups,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows: list[dict] = []
    # 10 prior TREND_DOWN rows for BTCUSDT, entry prices stepping down.
    for i in range(10):
        rows.append(_row(
            ts=start + timedelta(hours=i),
            symbol="BTCUSDT", side="LONG", regime="TREND_DOWN",
            entry=100.0 - i * 0.5,
        ))
    # Flip to TREND_UP — single rebound row.
    rows.append(_row(
        ts=start + timedelta(hours=10),
        symbol="BTCUSDT", side="LONG", regime="TREND_UP",
        entry=98.0, net_pnl=0.40,
    ))
    r = detect_rebound_setups(None, rows=rows)
    assert r.rebound_candidates_count >= 1
    assert r.used_future_return_features is False
    assert r.report_detection_mode == DETECTION_MODE_PREFIX_ONLY
    first = r.examples_top_100[0]
    assert first["detection_mode"] == DETECTION_MODE_PREFIX_ONLY
    assert first["detection_reason"] == "prefix_features_ok"
    assert first["used_future_return_features"] is False


def test_evaluate_rebound_outcome_uses_net_pnl_ex_post():
    """V8.2.8.1: outcome evaluation MAY read baseline_net_pnl_est —
    but only AFTER detection. The contract is a separate function."""
    from app.labs.rebound_regime_turn_lab_v8_2_8 import evaluate_rebound_outcome
    label_good, net_good = evaluate_rebound_outcome(_row(net_pnl=0.50))
    label_bad, net_bad = evaluate_rebound_outcome(_row(net_pnl=-0.30))
    label_unknown, net_unknown = evaluate_rebound_outcome({"symbol": "BTCUSDT"})
    assert label_good == "good"
    assert net_good == 0.50
    assert label_bad == "bad"
    assert net_bad == -0.30
    assert label_unknown == "unknown"
    assert net_unknown is None


def test_rebound_export_csv_marks_no_future_features(tmp_path):
    """V8.2.8.1: rebound CSV includes detection_mode / detection_reason /
    used_future_return_features columns; no row marks
    used_future_return_features=true."""
    from app.labs.research_export_v8_2_8 import export_research_v828
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows: list[dict] = []
    for i in range(10):
        rows.append(_row(
            ts=start + timedelta(hours=i),
            symbol="BTCUSDT", side="LONG", regime="TREND_DOWN",
            entry=100.0 - i * 0.5,
        ))
    for i in range(30):
        rows.append(_row(
            ts=start + timedelta(hours=10 + i),
            symbol="BTCUSDT", side="LONG", regime="TREND_UP",
            entry=98.0 + i * 0.2, net_pnl=0.30,
        ))
    base = tmp_path / "v828_rebound_csv"
    export_research_v828(None, rows=rows, base_dir=base)
    rebound_csv = base / "rebound_regime_turn_v1.csv"
    text = rebound_csv.read_text(encoding="utf-8")
    assert "detection_mode" in text
    assert "detection_reason" in text
    assert "used_future_return_features" in text
    with rebound_csv.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            flag = str(row.get("used_future_return_features", "")).strip().lower()
            assert flag in {"false", "0", ""}, (
                f"rebound CSV row marks used_future_return_features={flag!r}"
            )


def test_rebound_summary_marks_prefix_only_and_no_future(tmp_path):
    """V8.2.8.1: summary indicates rebound_detection_mode +
    rebound_used_future_return_features=false."""
    from app.labs.research_export_v8_2_8 import export_research_v828
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(40)]
    base = tmp_path / "v828_summary_prefix"
    export_research_v828(None, rows=rows, base_dir=base)
    summary_txt = (base / "research_v8_2_8_summary.txt").read_text(encoding="utf-8")
    assert "rebound_detection_mode:" in summary_txt
    assert "rebound_used_future_return_features: false" in summary_txt
    assert "rebound_prefix_only_count:" in summary_txt
    assert "rebound_need_data_count:" in summary_txt


def test_rebound_report_carries_no_live_safety_flags():
    """V8.2.8.1: report always carries safety flags."""
    from app.labs.rebound_regime_turn_lab_v8_2_8 import detect_rebound_setups
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(5)]
    r = detect_rebound_setups(None, rows=rows)
    assert r.research_only is True
    assert r.paper_filter_enabled is False
    assert r.can_send_real_orders is False
    assert r.final_recommendation == "NO LIVE"
    assert r.used_future_return_features is False


# ---------------------------------------------------------------------------
# Export V8.2.8 — sanitised + ZIP allow-list
# ---------------------------------------------------------------------------

def test_export_v828_zip_only_csv_txt_json(tmp_path):
    from app.labs.research_export_v8_2_8 import export_research_v828

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(40)]
    base = tmp_path / "v828_zip"
    export_research_v828(None, rows=rows, base_dir=base)
    zip_path = base / "research_v8_2_8_exports.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            assert name.endswith((".csv", ".txt", ".json"))


def test_export_v828_summary_includes_dual_verdict(tmp_path):
    from app.labs.research_export_v8_2_8 import export_research_v828

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [_row(ts=start + timedelta(hours=i)) for i in range(40)]
    base = tmp_path / "v828_summary"
    export_research_v828(None, rows=rows, base_dir=base)
    summary_txt = (base / "research_v8_2_8_summary.txt").read_text(encoding="utf-8")
    for key in (
        "long_verdict", "short_verdict",
        "duplicate_ratio", "duplicate_root_cause",
        "score_status_long", "score_status_short",
        "rebound_status",
        "paper_sandbox_candidates_after_quality",
        "final_recommendation: NO LIVE",
    ):
        assert key in summary_txt


# ---------------------------------------------------------------------------
# CLI parser regression
# ---------------------------------------------------------------------------

def test_v828_cli_commands_parse():
    from app.research_lab import build_argument_parser

    parser = build_argument_parser()
    for argv in [
        ["dual-side-barrier-audit-v828", "--hours", "168"],
        ["duplicate-root-cause-v828", "--hours", "168"],
        ["side-aware-score-calibration-v828", "--hours", "168"],
        ["rebound-regime-turn-lab-v828", "--hours", "168"],
        ["export-research-v828", "--hours", "168"],
        ["research-pack-v828", "--hours", "168"],
    ]:
        ns = parser.parse_args(argv)
        assert ns.command == argv[0]


def test_parser_has_no_duplicate_option_strings_after_v828():
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

V828_MODULES = [
    "app.labs.dual_side_barrier_truth_audit_v8_2_8",
    "app.labs.duplicate_source_root_cause_v8_2_8",
    "app.labs.side_aware_score_calibration_v8_2_8",
    "app.labs.rebound_regime_turn_lab_v8_2_8",
    "app.labs.research_export_v8_2_8",
]


def test_v828_modules_have_no_forbidden_calls():
    for mod in V828_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name not in FORBIDDEN_CALL_NAMES, f"{mod} calls {name}"


def test_v828_modules_have_no_forbidden_literal_true_assigns():
    for mod in V828_MODULES:
        path = pathlib.Path(importlib.import_module(mod).__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                    if name in FORBIDDEN_ASSIGN_LITERALS and isinstance(node.value, ast.Constant):
                        assert node.value.value is not True, f"{mod} {name}=True"


def test_v828_outputs_carry_no_live():
    from app.labs.dual_side_barrier_truth_audit_v8_2_8 import (
        DualSideBarrierTruthReport,
    )
    from app.labs.duplicate_source_root_cause_v8_2_8 import (
        DuplicateRootCauseReport,
    )
    from app.labs.rebound_regime_turn_lab_v8_2_8 import (
        ReboundRegimeTurnReport,
    )
    from app.labs.side_aware_score_calibration_v8_2_8 import (
        SideAwareScoreReport,
    )

    for inst in [
        DualSideBarrierTruthReport(hours=1, generated_at="t"),
        DuplicateRootCauseReport(hours=1, generated_at="t"),
        SideAwareScoreReport(hours=1, generated_at="t"),
        ReboundRegimeTurnReport(hours=1, generated_at="t"),
    ]:
        assert inst.research_only is True
        assert inst.paper_filter_enabled is False
        assert inst.can_send_real_orders is False
        assert inst.final_recommendation == "NO LIVE"
