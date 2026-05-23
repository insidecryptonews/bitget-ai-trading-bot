"""Tests for Phase 7.4A sprint:
- Track G: worker duplicate audit no false positive.
- Track B: data quality audit (no destructive writes, classifies dup vs density).
- Track C: label quality audit (classifies missed/inconsistent labels).
- Track H: data vault cleanup audit (read-only, never deletes).
- Track F: cost model — no double counting, scenario coverage.
- Track J: design skeleton labs return DESIGN_ONLY status.

Safety invariant: no test activates live, paper filter, runtime hooks or
touches Bitget endpoints.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import load_config
from app.cost_model import (
    estimate_fee_cost_bps,
    estimate_funding_bps,
    estimate_slippage_bps,
    explain_cost_breakdown,
    should_apply_funding,
)
from app.data_quality_audit import DataQualityAudit, render_report_text as render_dq
from app.data_vault_cleanup_audit import DataVaultCleanupAudit, render_report_text as render_vault
from app.database import Database
from app.label_quality_audit import LabelQualityAudit, render_report_text as render_lq
from app.worker_health_audit import (
    WorkerHealthAudit,
    _classify_duplicate_status,
    _distinct_python_app_main_pids,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> Database:
    monkeypatch.setattr("app.database.PROJECT_ROOT", tmp_path)
    cfg = load_config()
    instance = Database(cfg, logging.getLogger("test"))
    instance.sqlite_path = tmp_path / "test.db"
    Database._sqlite_wal_initialised = False
    instance.initialize()
    return instance


# ---------------------------------------------------------------------------
# Track G — worker duplicate audit
# ---------------------------------------------------------------------------


def test_distinct_pids_from_pgrep_output():
    lines = [
        "12345 python -m app.main",
        "12345 python -m app.main",  # duplicate line same PID
        "67890 python -m app.main",
    ]
    assert _distinct_python_app_main_pids(lines) == 2


def test_distinct_pids_handles_powershell_output_without_pid_prefix():
    lines = [
        "python -m app.main --foo",
        "python -m app.main --foo",  # exact dup → 1 entry after normalisation
        "python -m app.main --bar",
    ]
    # Each unique cmdline counts; both share an extra arg so we expect 2.
    assert _distinct_python_app_main_pids(lines) == 2


def test_classify_duplicate_status_ok_when_lock_owned():
    status, reason = _classify_duplicate_status(
        distinct_pids=1, lock_status="owned", lock_acquired=True, warning="",
    )
    assert status == "OK"


def test_classify_duplicate_status_ok_even_with_multiple_pids_if_lock_owned():
    status, reason = _classify_duplicate_status(
        distinct_pids=3, lock_status="owned", lock_acquired=True, warning="",
    )
    assert status == "OK"
    assert "process_count_high_but_lock_owned_single_instance" in reason


def test_classify_duplicate_status_bad_when_lock_blocked_duplicate():
    status, reason = _classify_duplicate_status(
        distinct_pids=2, lock_status="blocked_duplicate", lock_acquired=False,
        warning="duplicate_worker_detected",
    )
    assert status == "BAD"


def test_classify_duplicate_status_warning_when_multiple_pids_and_lock_missing():
    status, reason = _classify_duplicate_status(
        distinct_pids=2, lock_status="missing", lock_acquired=False, warning="",
    )
    assert status == "WARNING"


# ---------------------------------------------------------------------------
# Track B — data quality audit (read-only, no destructive writes)
# ---------------------------------------------------------------------------


def test_data_quality_audit_returns_clean_report_on_empty_db(db):
    audit = DataQualityAudit(db)
    report = audit.build(hours=24)
    assert report.overall_status in {"OK", "WARNING"}
    assert report.research_only is True
    assert report.can_delete is False
    text = render_dq(report)
    assert "DATA QUALITY AUDIT START" in text
    assert "can_delete: false" in text
    assert "NO LIVE" in text


def test_data_quality_audit_detects_exact_duplicates(db):
    # Inject 3 events with identical (timestamp, event_type, message) by
    # writing direct rows with the same timestamp — record_event() uses
    # microsecond-precision timestamps so calls don't naturally collide.
    same_ts = "2026-01-01T00:00:00+00:00"
    with db._connect() as conn:
        for _ in range(3):
            conn.execute(
                "INSERT INTO events(timestamp, level, event_type, message, payload_json) VALUES (?, ?, ?, ?, ?)",
                (same_ts, "INFO", "dup_test", "exact_same_message", "{}"),
            )
    audit = DataQualityAudit(db)
    report = audit.build(hours=24 * 365)  # wide window so 2026-01-01 is included
    events_table = next(t for t in report.tables if t.table == "events")
    assert events_table.exact_duplicate_count >= 2, (
        f"expected at least 2 dup rows, got {events_table.exact_duplicate_count} "
        f"(classification={events_table.duplicate_classification})"
    )


def test_data_quality_audit_never_writes(db):
    # Sanity: build the audit and verify no schema or rows were modified.
    audit = DataQualityAudit(db)
    # Seed a known row count.
    db.record_event("seed", "row1", payload={})
    db.record_event("seed", "row2", payload={})
    before = db.fetch_table_rows("events", limit=100)
    audit.build(hours=24)
    after = db.fetch_table_rows("events", limit=100)
    assert len(before) == len(after)


# ---------------------------------------------------------------------------
# Track C — label quality audit
# ---------------------------------------------------------------------------


def test_label_quality_audit_handles_empty_labels(db):
    audit = LabelQualityAudit(db)
    report = audit.build(hours=24)
    assert report.label_quality_status == "NO_DATA"
    assert report.recommended_action == "NEED_LABELS"
    text = render_lq(report)
    assert "LABEL QUALITY AUDIT START" in text
    assert "NO LIVE" in text


def test_label_quality_audit_safe_on_unrelated_dbs(db):
    # Insert a non-label event; the audit should not crash.
    db.record_event("unrelated", "msg", payload={})
    audit = LabelQualityAudit(db)
    report = audit.build(hours=24)
    assert report.total_labels == 0
    assert report.final_recommendation == "NO LIVE"


# ---------------------------------------------------------------------------
# Track H — data vault cleanup audit
# ---------------------------------------------------------------------------


def test_data_vault_cleanup_audit_handles_missing_dir(tmp_path: Path):
    class Cfg: pass
    cfg = Cfg()
    cfg.training_vault_dir = str(tmp_path / "does_not_exist")
    audit = DataVaultCleanupAudit(cfg)
    report = audit.build()
    assert report.can_delete is False
    assert report.research_only is True
    text = render_vault(report)
    assert "DATA VAULT CLEANUP AUDIT" in text
    assert "NO LIVE" in text


def test_data_vault_cleanup_audit_does_not_delete_anything(tmp_path: Path):
    # Create fake export dir with a work dir + a complete .zip
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    work = export_dir / "training_vault_20260101_000000_work"
    work.mkdir()
    (work / "leftover.tmp").write_bytes(b"x" * 1024)
    (export_dir / "training_vault_20260201_120000.zip").write_bytes(b"y" * 2048)

    class Cfg: pass
    cfg = Cfg()
    cfg.training_vault_dir = str(export_dir)

    before = list(export_dir.iterdir())
    audit = DataVaultCleanupAudit(cfg)
    report = audit.build()
    after = list(export_dir.iterdir())
    assert {p.name for p in before} == {p.name for p in after}, "audit must not delete files"
    assert report.can_delete is False
    assert len(report.incomplete_work_dirs) == 1
    entry = report.incomplete_work_dirs[0]
    # Work dir was created right now, so age < 48h; safe_to_delete should be False.
    assert entry.safe_to_delete is False


# ---------------------------------------------------------------------------
# Track F — cost model scenarios + no double counting
# ---------------------------------------------------------------------------


def test_cost_model_taker_round_trip_is_12_bps():
    assert estimate_fee_cost_bps("taker", "taker") == pytest.approx(12.0)


def test_cost_model_maker_maker_is_4_bps():
    assert estimate_fee_cost_bps("maker", "maker") == pytest.approx(4.0)


def test_cost_model_maker_taker_is_8_bps():
    assert estimate_fee_cost_bps("maker", "taker") == pytest.approx(8.0)


def test_cost_model_slippage_zero_for_passive_low_liquidity_floor():
    # passive execution + liquid pair = lowest slippage scenario
    bps = estimate_slippage_bps(liquidity_profile="liquid", execution_type="maker_maker", base_slippage_bps=3.0)
    assert bps == pytest.approx(0.75)


def test_cost_model_slippage_scales_for_thin_liquidity():
    thin = estimate_slippage_bps(liquidity_profile="thin", execution_type="taker_taker", base_slippage_bps=3.0)
    liquid = estimate_slippage_bps(liquidity_profile="liquid", execution_type="taker_taker", base_slippage_bps=3.0)
    assert thin > liquid


def test_cost_model_funding_only_if_timestamp_crossed():
    # Entry 23:00 UTC, exit 23:30 UTC → does NOT cross 00:00 funding window
    entry = "2026-01-01T23:00:00+00:00"
    exit_t = "2026-01-01T23:30:00+00:00"
    assert should_apply_funding(entry, exit_t) is False
    # Entry 23:00 UTC, exit 01:00 UTC → crosses 00:00 funding window
    exit_t2 = "2026-01-02T01:00:00+00:00"
    assert should_apply_funding(entry, exit_t2) is True


def test_cost_model_funding_direction_long_pays_positive_rate():
    bps = estimate_funding_bps("LONG", 0.0001, crosses_funding_timestamp=True)
    assert bps > 0   # longs pay shorts when funding > 0


def test_cost_model_funding_direction_short_receives_positive_rate():
    bps = estimate_funding_bps("SHORT", 0.0001, crosses_funding_timestamp=True)
    assert bps < 0   # shorts receive when funding > 0


def test_cost_model_market_probe_zero_cost():
    breakdown = explain_cost_breakdown(source="market_probe", side="LONG")
    assert breakdown.total_cost_bps == pytest.approx(0.0)
    assert breakdown.actionability == "NOT_ACTIONABLE_MARKET_PROBE"


def test_cost_model_already_includes_costs_returns_zero_to_avoid_double_count():
    breakdown = explain_cost_breakdown(
        source="trade_signal", side="LONG",
        already_includes_costs=True,
    )
    assert breakdown.total_cost_bps == pytest.approx(0.0)
    assert "already included" in breakdown.cost_application_explanation.lower()


def test_cost_model_time_exit_with_no_trade_assumption_zero_cost():
    breakdown = explain_cost_breakdown(
        source="trade_signal", side="LONG",
        outcome="TIME", time_exit_assumption="no_trade",
    )
    assert breakdown.total_cost_bps == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Track J — design skeletons return DESIGN_ONLY status
# ---------------------------------------------------------------------------


def test_profit_lock_exit_lab_is_design_only():
    from app.profit_lock_exit_lab import design_summary, render_design_text
    r = design_summary()
    assert r.status == "DESIGN_ONLY_NOT_IMPLEMENTED"
    assert r.research_only is True
    text = render_design_text(r)
    assert "NO LIVE" in text
    assert "no_runtime_change: true" in text


def test_fast_exit_lab_is_design_only():
    from app.fast_exit_lab import design_summary, render_design_text
    r = design_summary()
    assert r.status == "DESIGN_ONLY_NOT_IMPLEMENTED"
    text = render_design_text(r)
    assert "NO LIVE" in text


def test_mtf_regime_gate_lab_is_design_only():
    from app.mtf_regime_gate_lab import design_summary, render_design_text
    r = design_summary()
    assert r.status == "DESIGN_ONLY_NOT_IMPLEMENTED"
    text = render_design_text(r)
    assert "NO LIVE" in text


def test_momentum_burst_5m_lab_is_design_only():
    from app.momentum_burst_5m_lab import design_summary, render_design_text
    r = design_summary()
    assert r.status == "DESIGN_ONLY_NEED_OHLCV_5M"
    text = render_design_text(r)
    assert "NO LIVE" in text


def test_setup_key_trainer_is_design_only():
    from app.setup_key_trainer import design_summary, render_design_text
    r = design_summary()
    assert r.status == "DESIGN_ONLY_NOT_IMPLEMENTED"
    text = render_design_text(r)
    assert "NO LIVE" in text


def test_net_ev_trainer_is_design_only():
    from app.net_ev_trainer import design_summary, render_design_text
    r = design_summary()
    assert r.status == "DESIGN_ONLY_NOT_IMPLEMENTED"
    text = render_design_text(r)
    assert "NO LIVE" in text


def test_microstructure_roadmap_doc_exists():
    path = Path("docs/MICROSTRUCTURE_ROADMAP.md")
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "NO LIVE" in text
    assert "DESIGN ONLY" in text


# ---------------------------------------------------------------------------
# Safety invariants for the sprint as a whole
# ---------------------------------------------------------------------------


def test_phase_74a_modules_have_no_execution_imports():
    """Verify the new audit/design modules do not pull in Bitget/Execution code paths."""
    import inspect
    import app.data_quality_audit as dq
    import app.label_quality_audit as lq
    import app.data_vault_cleanup_audit as dv
    import app.profit_lock_exit_lab as pl
    import app.fast_exit_lab as fe
    import app.mtf_regime_gate_lab as mt
    import app.momentum_burst_5m_lab as mb
    import app.setup_key_trainer as sk
    import app.net_ev_trainer as ne

    for mod in (dq, lq, dv, pl, fe, mt, mb, sk, ne):
        src = inspect.getsource(mod)
        for forbidden in ("BitgetClient", "ExecutionEngine", "PaperTrader.open_position", "place_order", "place_tpsl_order"):
            assert forbidden not in src, f"{mod.__name__} contains forbidden token {forbidden}"


def test_safety_flags_unchanged_by_new_modules():
    cfg = load_config()
    assert cfg.live_trading is False
    assert cfg.dry_run is True
    assert cfg.paper_trading is True
    assert cfg.enable_paper_policy_filter is False
    assert cfg.enable_candidate_shadow_monitor is False
    assert cfg.can_send_real_orders is False
