"""Tests for Phase 7.4A-2 hotfix:
- Worker Health Audit: no false BAD when audit runs from a non-worker process.
- Worker Health Audit: tmux/bash wrappers no longer counted as real workers.
- Dashboard short report: heavy sections marked SKIPPED_HEAVY, report stays OK.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import load_config
from app.dashboard_pro import DashboardProReporter
from app.database import Database
from app.worker_health_audit import (
    WorkerHealthAudit,
    _classify_duplicate_status,
    _distinct_python_app_main_pids,
    _filter_real_python_workers,
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
# Worker process filtering
# ---------------------------------------------------------------------------


def test_filter_real_python_workers_drops_tmux_and_bash_wrappers():
    lines = [
        "12345 tmux new-session -d -s bot 'python -m app.main'",
        "12346 bash -c 'cd /home/ubuntu/bitget && python -m app.main'",
        "12347 .venv/bin/python -m app.main",
    ]
    real = _filter_real_python_workers(lines)
    assert len(real) == 1
    assert "12347" in real[0]
    assert ".venv/bin/python" in real[0]


def test_filter_real_python_workers_keeps_unix_and_windows_python():
    lines = [
        "100 /usr/bin/python3 -m app.main",
        "101 /home/u/.venv/bin/python -m app.main",
        "102 C:\\Python\\python.exe -m app.main",
    ]
    real = _filter_real_python_workers(lines)
    assert len(real) == 3


def test_filter_real_python_workers_drops_empty_and_other_executables():
    lines = [
        "",
        "200 grep app.main",
        "201 less /var/log/app.main.log",
        "202 python -m app.main",
    ]
    real = _filter_real_python_workers(lines)
    assert len(real) == 1
    assert "python -m app.main" in real[0]


# ---------------------------------------------------------------------------
# Duplicate classification — audit from non-worker process
# ---------------------------------------------------------------------------


def test_classify_audit_from_non_worker_process_returns_ok():
    """The VPS bug: report builder runs outside the worker. Lock says
    blocked_duplicate (the worker owns it) but distinct_pids is 1 because
    only one real worker exists. This must be OK, not BAD."""
    status, reason = _classify_duplicate_status(
        distinct_pids=1,
        lock_status="blocked_duplicate",
        lock_acquired=False,
        warning="duplicate_worker_detected",
        active_worker_instance="hostname:1234:abcd",
        lock_age_seconds=15.0,
        lock_ttl_seconds=120,
    )
    assert status == "OK"
    assert "non_worker_process" in reason


def test_classify_two_real_workers_blocked_duplicate_returns_bad():
    """Real conflict: two distinct python app.main processes + lock conflict."""
    status, reason = _classify_duplicate_status(
        distinct_pids=2,
        lock_status="blocked_duplicate",
        lock_acquired=False,
        warning="duplicate_worker_detected",
        active_worker_instance="host:1:a",
        lock_age_seconds=5.0,
        lock_ttl_seconds=120,
    )
    assert status == "BAD"
    assert "two_python_app_main" in reason


def test_classify_two_workers_lock_missing_returns_warning():
    status, reason = _classify_duplicate_status(
        distinct_pids=2,
        lock_status="missing",
        lock_acquired=False,
        warning="",
    )
    assert status == "WARNING"
    assert "without_active_lock" in reason


def test_classify_single_worker_lock_owned_returns_ok():
    status, _ = _classify_duplicate_status(
        distinct_pids=1,
        lock_status="owned",
        lock_acquired=True,
        warning="",
    )
    assert status == "OK"


def test_classify_blocked_duplicate_without_known_owner_returns_warning():
    """Edge case: lock reports blocked but no active_worker_instance — could
    be a stale lock that needs cleanup."""
    status, reason = _classify_duplicate_status(
        distinct_pids=0,
        lock_status="blocked_duplicate",
        lock_acquired=False,
        warning="",
        active_worker_instance="",
        lock_age_seconds=99999.0,
        lock_ttl_seconds=120,
    )
    assert status == "WARNING"
    assert "stale_lock" in reason


def test_classify_two_workers_lock_owned_returns_warning_not_ok():
    """If we see 2 Python app.main processes AND the lock is owned, that's
    suspicious — could be a real race. Should be WARNING (not OK)."""
    status, reason = _classify_duplicate_status(
        distinct_pids=2,
        lock_status="owned",
        lock_acquired=True,
        warning="",
    )
    assert status == "WARNING"


# ---------------------------------------------------------------------------
# End-to-end: simulate the exact VPS scenario
# ---------------------------------------------------------------------------


def test_audit_e2e_simulates_vps_scenario_no_false_bad(db):
    """Recreate the bug from the VPS report:
    - tmux + bash + python wrappers all detected by pgrep
    - audit running in a different process from the worker
    - lock_status = blocked_duplicate (worker owns it)
    - active_worker_instance fresh
    Expected: duplicate_worker_status = OK, worker_health_status != BAD.
    """
    fake_pgrep = [
        "12345 tmux new-session -d -s bot 'python -m app.main'",
        "12346 bash -c 'python -m app.main'",
        "12347 /home/ubuntu/.venv/bin/python -m app.main",
    ]
    fake_lock = {
        "enabled": True,
        "acquired": False,
        "lock_status": "blocked_duplicate",
        "active_worker_instance": "ubuntu:99:abcd",
        "lock_age_seconds": 15.0,
        "warning_if_duplicate_worker": "duplicate_worker_detected",
    }
    audit = WorkerHealthAudit(load_config(), db)
    with patch("app.worker_health_audit._worker_processes", return_value=fake_pgrep), \
         patch("app.worker_health_audit.worker_lock_status_payload", return_value=fake_lock):
        payload = audit.build(hours=24)
    assert payload["worker_process_count"] == 1, (
        f"expected 1 real python worker, got {payload['worker_process_count']} "
        f"(raw_count={payload['worker_process_raw_count']})"
    )
    assert payload["distinct_python_app_main_pids"] == 1
    assert payload["duplicate_worker_status"] == "OK", payload["duplicate_worker_reason"]
    assert payload["worker_health_status"] != "BAD"


def test_audit_e2e_detects_real_duplicate_workers(db):
    """Two real python app.main processes + lock conflict = BAD."""
    fake_pgrep = [
        "12345 /home/u/.venv/bin/python -m app.main",
        "12399 /home/u/.venv/bin/python -m app.main",  # second real worker
    ]
    fake_lock = {
        "enabled": True,
        "acquired": False,
        "lock_status": "blocked_duplicate",
        "active_worker_instance": "ubuntu:12345:abcd",
        "lock_age_seconds": 5.0,
        "warning_if_duplicate_worker": "duplicate_worker_detected",
    }
    audit = WorkerHealthAudit(load_config(), db)
    with patch("app.worker_health_audit._worker_processes", return_value=fake_pgrep), \
         patch("app.worker_health_audit.worker_lock_status_payload", return_value=fake_lock):
        payload = audit.build(hours=24)
    assert payload["distinct_python_app_main_pids"] == 2
    assert payload["duplicate_worker_status"] == "BAD"
    assert payload["worker_health_status"] == "BAD"


# ---------------------------------------------------------------------------
# Dashboard short report: heavy section skipping
# ---------------------------------------------------------------------------


def test_short_report_skips_heavy_sections_by_default(db):
    cfg = load_config()
    reporter = DashboardProReporter(cfg, db, logging.getLogger("test"))
    payload = reporter.build_short(hours=24)
    sections_by_name = {s["name"]: s for s in payload["sections"]}
    for heavy in reporter.SHORT_REPORT_HEAVY_SECTIONS:
        assert heavy in sections_by_name, f"heavy section {heavy!r} missing from short report"
        sec = sections_by_name[heavy]
        assert sec["status"] == "skipped_heavy", (
            f"{heavy} status was {sec['status']}, expected skipped_heavy"
        )
        assert "SKIPPED_HEAVY_SECTION" in sec["text"]


def test_short_report_status_ok_when_only_heavy_sections_skipped(db):
    cfg = load_config()
    reporter = DashboardProReporter(cfg, db, logging.getLogger("test"))
    payload = reporter.build_short(hours=24)
    # `skipped_heavy` MUST NOT produce PARTIAL_REPORT
    skipped_count = sum(
        1 for s in payload["sections"]
        if s["status"] == "skipped_heavy"
    )
    timeout_count = sum(
        1 for s in payload["sections"]
        if s["status"] in {"timeout", "error"}
    )
    assert skipped_count == len(reporter.SHORT_REPORT_HEAVY_SECTIONS)
    # report_status is PARTIAL_REPORT only if there were real errors/timeouts.
    if timeout_count == 0:
        assert payload["report_status"] == "OK", (
            f"expected OK, got {payload['report_status']} "
            f"(skipped={skipped_count}, timeouts={timeout_count})"
        )


def test_short_report_does_not_block_on_heavy_sections(db):
    """The short report must complete in significantly less time than the
    sum of heavy section timeouts (3s × 8 heavies = 24s). With skipping
    in place, total elapsed should be far below that."""
    cfg = load_config()
    reporter = DashboardProReporter(cfg, db, logging.getLogger("test"))
    payload = reporter.build_short(hours=24)
    # 8 heavies × 3s budget = 24s would-be wasted. With skip we expect well under that.
    # We don't pin a tight number to avoid flakiness on slow CI; we just assert
    # heavy sections recorded duration_ms == 0 (instant skip).
    for s in payload["sections"]:
        if s["name"] in reporter.SHORT_REPORT_HEAVY_SECTIONS:
            assert s["duration_ms"] == 0, (
                f"{s['name']} should be instant skip, got duration_ms={s['duration_ms']}"
            )


def test_short_report_include_heavy_true_runs_them(db):
    """Opt-in path: caller can still ask for heavy sections in short mode."""
    cfg = load_config()
    reporter = DashboardProReporter(cfg, db, logging.getLogger("test"))
    payload = reporter.build_short(hours=24, include_heavy=True)
    sections_by_name = {s["name"]: s for s in payload["sections"]}
    # At least one heavy section was ATTEMPTED (status not "skipped_heavy")
    attempted = [
        s for name, s in sections_by_name.items()
        if name in reporter.SHORT_REPORT_HEAVY_SECTIONS and s["status"] != "skipped_heavy"
    ]
    assert len(attempted) > 0


def test_short_report_helper_passes_include_heavy_flag(db):
    """The module-level helper exposes the flag."""
    from app.dashboard_pro import build_dashboard_short_report
    cfg = load_config()
    payload = build_dashboard_short_report(cfg, db, hours=24, include_heavy=False)
    sections_by_name = {s["name"]: s for s in payload["sections"]}
    for heavy in DashboardProReporter.SHORT_REPORT_HEAVY_SECTIONS:
        assert sections_by_name[heavy]["status"] == "skipped_heavy"


def test_short_report_text_marks_no_live(db):
    cfg = load_config()
    payload = DashboardProReporter(cfg, db, logging.getLogger("test")).build_short(hours=24)
    assert "final_recommendation: NO LIVE" in payload["text"]


# ---------------------------------------------------------------------------
# OHLCV 5m backfill — confirm 5m is supported and idempotent
# ---------------------------------------------------------------------------


def test_ohlcv_backfill_supports_5m_granularity():
    from app.ohlcv_backfill import GRANULARITY_API, GRANULARITY_MINUTES
    assert "5m" in GRANULARITY_API
    assert GRANULARITY_API["5m"] == "5m"
    assert GRANULARITY_MINUTES["5m"] == 5


def test_ohlcv_backfill_cli_help_includes_5m():
    """Verify the CLI surface accepts --timeframes 5m without crashing import."""
    from app.ohlcv_backfill import _parse_args
    args = _parse_args(["--symbols", "BTCUSDT", "--timeframes", "5m", "--days", "1", "--dry-run"])
    assert "5m" in args.timeframes
    assert args.dry_run is True
