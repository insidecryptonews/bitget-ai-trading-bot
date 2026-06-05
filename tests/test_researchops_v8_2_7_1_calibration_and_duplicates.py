"""V8.2.7.1 hotfix tests — real score calibration in export + duplicate ratio
hard gate in the final rule gate.

All tests run with synthetic dataset rows. No DB / no OHLCV reads.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Synthetic dataset builder
# ---------------------------------------------------------------------------

def _row(
    *,
    ts: datetime,
    symbol: str = "BTCUSDT",
    side: str = "LONG",
    regime: str = "RISK_ON",
    score: int = 80,
    strategy: str = "TEST",
    entry: float = 100.0,
    ret_4h: float = 1.0,
    mfe: float = 1.0,
    mae: float = -0.2,
    first_barrier: str = "TP",
    net_pnl: float = 0.50,
    gross: float = 0.96,
    label: str = "GOOD_LONG",
) -> dict[str, Any]:
    bucket = (
        "90-100" if score >= 90 else
        "80-89" if score >= 80 else
        "70-79" if score >= 70 else
        "60-69" if score >= 60 else "<60"
    )
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


def _winning_rule_dataset(start: datetime, n: int = 100) -> list[dict[str, Any]]:
    """Interleaved 75% winners / 25% losers, one row per hour, single
    symbol/feature combo so the strict OOS selector mines a single rule.
    """
    rows: list[dict[str, Any]] = []
    for i in range(n):
        is_winner = (i % 4) != 3
        if is_winner:
            rows.append(_row(ts=start + timedelta(hours=i),
                             symbol="XYZUSDT", gross=0.95, net_pnl=0.70))
        else:
            rows.append(_row(ts=start + timedelta(hours=i),
                             symbol="XYZUSDT", gross=-0.20, net_pnl=-0.45))
    return rows


# ---------------------------------------------------------------------------
# Fix 1 — score calibration real en export
# ---------------------------------------------------------------------------

def test_export_v827_no_longer_hardcodes_calibration_false():
    """Source-level assertion: the export must NOT pass
    ``score_calibration_ok=False`` to the strict OOS selector.
    """
    import pathlib
    src = pathlib.Path(
        __import__("app.labs.research_export_v8_2_7", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")
    assert "score_calibration_ok=False" not in src, (
        "research_export_v8_2_7 must compute calibration honestly via "
        "audit_score_calibration, not hardcode False."
    )


def test_export_v827_summary_includes_score_calibration_status(tmp_path):
    from app.labs.research_export_v8_2_7 import export_research_v827

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = _winning_rule_dataset(start, n=100)
    base = tmp_path / "v8271_calibration"
    manifest = export_research_v827(None, rows=rows, base_dir=base)
    # Summary contains the two new keys.
    summary_txt = (base / "research_v8_2_7_summary.txt").read_text(encoding="utf-8")
    assert "score_calibration_status:" in summary_txt
    assert "score_calibration_ok:" in summary_txt
    # Manifest carries them too.
    assert "score_calibration_status" in manifest
    assert "score_calibration_ok" in manifest


def test_export_v827_final_gate_csv_includes_calibration_columns(tmp_path):
    from app.labs.research_export_v8_2_7 import export_research_v827

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = _winning_rule_dataset(start, n=100)
    base = tmp_path / "v8271_csv"
    export_research_v827(None, rows=rows, base_dir=base)
    csv_path = base / "final_rule_gate_v1.csv"
    with csv_path.open(encoding="utf-8") as f:
        metrics = {row["metric"]: row["value"] for row in csv.DictReader(f)}
    assert "score_calibration_status" in metrics
    assert "score_calibration_ok" in metrics
    assert "duplicate_ratio_gate" in metrics
    assert "duplicate_ratio_gate_status" in metrics


# ---------------------------------------------------------------------------
# Fix 2 — duplicate ratio hard gate
# ---------------------------------------------------------------------------

def test_final_gate_blocks_paper_sandbox_when_duplicate_ratio_too_high(monkeypatch):
    """When duplicate_ratio > MAX_DUPLICATE_RATIO_FOR_PAPER (0.30), every
    PAPER_SANDBOX_CANDIDATE must be demoted to RESEARCH_CANDIDATE and
    tagged with the explicit reason ``duplicate_ratio_too_high``.

    The test forces calibration=PASS so the rule reaches
    PAPER_SANDBOX_CANDIDATE before the duplicate gate runs; otherwise the
    rule would already be RESEARCH_CANDIDATE for another reason and the
    duplicate gate would be a no-op.
    """
    from app.labs import final_rule_gate_v8_2_7 as gate_mod
    from app.labs.final_rule_gate_v8_2_7 import (
        DUPLICATE_RATIO_GATE_FAIL,
        DUPLICATE_RATIO_TOO_HIGH_REASON,
        run_final_gate,
    )
    from app.labs.score_calibration_audit import (
        MONOTONIC_PASS,
        ScoreCalibrationReport,
    )

    # Force calibration=PASS so a winning rule would normally promote to
    # PAPER_SANDBOX_CANDIDATE — then the duplicate gate must block it.
    class _FakeDedup:
        duplicate_ratio = 0.86  # mimics V8.2.5 real (0.8562)

    fake_recal = ScoreCalibrationReport(hours=1, generated_at="t")
    fake_recal.monotonicity_status = MONOTONIC_PASS

    monkeypatch.setattr(gate_mod, "audit_dedup", lambda *a, **k: _FakeDedup())
    monkeypatch.setattr(gate_mod, "audit_score_calibration", lambda *a, **k: fake_recal)

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = _winning_rule_dataset(start, n=100)
    r = run_final_gate(None, rows=rows)
    assert r.duplicate_ratio == pytest.approx(0.86)
    assert r.duplicate_ratio_gate_status == DUPLICATE_RATIO_GATE_FAIL
    assert r.paper_sandbox_candidates == 0
    # At least one rule was demoted with the duplicate reason.
    demoted = [
        rule for rule in r.research_candidate_rules
        if DUPLICATE_RATIO_TOO_HIGH_REASON in str(rule.get("reject_reason"))
    ]
    assert demoted, "expected at least one rule demoted with duplicate_ratio_too_high reason"


def test_final_gate_allows_paper_sandbox_when_duplicate_ratio_low(monkeypatch):
    """With low duplicate ratio and score calibration ok, a winning rule
    can still reach PAPER_SANDBOX_CANDIDATE.
    """
    from app.labs import final_rule_gate_v8_2_7 as gate_mod
    from app.labs.final_rule_gate_v8_2_7 import (
        DUPLICATE_RATIO_GATE_PASS,
        run_final_gate,
    )
    from app.labs.score_calibration_audit import MONOTONIC_PASS, ScoreCalibrationReport

    # Force good calibration + low duplicate ratio.
    class _FakeDedup:
        duplicate_ratio = 0.05

    fake_recal = ScoreCalibrationReport(hours=1, generated_at="t")
    fake_recal.monotonicity_status = MONOTONIC_PASS

    monkeypatch.setattr(gate_mod, "audit_dedup", lambda *a, **k: _FakeDedup())
    monkeypatch.setattr(gate_mod, "audit_score_calibration", lambda *a, **k: fake_recal)

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = _winning_rule_dataset(start, n=100)
    r = run_final_gate(None, rows=rows)
    assert r.duplicate_ratio_gate_status == DUPLICATE_RATIO_GATE_PASS
    assert r.paper_sandbox_candidates >= 1


def test_final_gate_summary_reports_duplicate_ratio_gate_status(monkeypatch, tmp_path):
    """The export summary surfaces ``duplicate_ratio_gate_status`` even
    when the underlying ratio is high so the operator sees the FAIL
    upfront.
    """
    from app.labs import final_rule_gate_v8_2_7 as gate_mod
    from app.labs.research_export_v8_2_7 import export_research_v827

    class _FakeDedup:
        duplicate_ratio = 0.86

    monkeypatch.setattr(gate_mod, "audit_dedup", lambda *a, **k: _FakeDedup())

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = _winning_rule_dataset(start, n=100)
    base = tmp_path / "v8271_duplicate_summary"
    export_research_v827(None, rows=rows, base_dir=base)
    summary_txt = (base / "research_v8_2_7_summary.txt").read_text(encoding="utf-8")
    assert "duplicate_ratio_gate_status:" in summary_txt
    # And the value should be FAIL given the forced 0.86.
    assert "FAIL" in summary_txt


def test_duplicate_ratio_gate_threshold_constant_is_conservative():
    from app.labs.final_rule_gate_v8_2_7 import MAX_DUPLICATE_RATIO_FOR_PAPER

    assert 0.10 <= MAX_DUPLICATE_RATIO_FOR_PAPER <= 0.40, (
        "duplicate-ratio threshold should be conservative (0.10–0.40 range); "
        "V8.2.5 real production was 0.8562 so threshold must block it."
    )


def test_duplicate_ratio_too_high_reason_marker_present():
    """The reason constant must exist and be surfaced where reject_reason
    is written.
    """
    from app.labs.final_rule_gate_v8_2_7 import DUPLICATE_RATIO_TOO_HIGH_REASON

    assert DUPLICATE_RATIO_TOO_HIGH_REASON == "duplicate_ratio_too_high"


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

def test_v8271_outputs_carry_no_live(monkeypatch):
    from app.labs import final_rule_gate_v8_2_7 as gate_mod
    from app.labs.final_rule_gate_v8_2_7 import run_final_gate

    class _FakeDedup:
        duplicate_ratio = 0.05

    monkeypatch.setattr(gate_mod, "audit_dedup", lambda *a, **k: _FakeDedup())
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = _winning_rule_dataset(start, n=80)
    r = run_final_gate(None, rows=rows)
    assert r.research_only is True
    assert r.paper_filter_enabled is False
    assert r.can_send_real_orders is False
    assert r.final_recommendation == "NO LIVE"
