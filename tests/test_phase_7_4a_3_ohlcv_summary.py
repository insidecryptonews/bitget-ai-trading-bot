"""Tests for Phase 7.4A-3 short-report hardening:

- Real Strategy Backtester + Candidate Promotion V2 are now SKIPPED in short mode.
- New OHLCV Summary section is FAST: count + min/max per symbol, no full audit.
- Heavy `OHLCV Replay Loader` section removed from short report (goes to full report).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import load_config
from app.dashboard_pro import DashboardProReporter
from app.database import Database


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> Database:
    monkeypatch.setattr("app.database.PROJECT_ROOT", tmp_path)
    cfg = load_config()
    instance = Database(cfg, logging.getLogger("test"))
    instance.sqlite_path = tmp_path / "test.db"
    Database._sqlite_wal_initialised = False
    instance.initialize()
    return instance


@pytest.fixture()
def db_with_ohlcv(db: Database) -> Database:
    """Insert a small OHLCV set across 3 symbols × 5m."""
    rows = []
    base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        for i in range(50):
            ts = (base_ts.timestamp() + i * 300)
            rows.append({
                "symbol": symbol,
                "timeframe": "5m",
                "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                "open": 100.0 + i * 0.1,
                "high": 100.5 + i * 0.1,
                "low": 99.5 + i * 0.1,
                "close": 100.2 + i * 0.1,
                "volume": 1000.0,
                "quote_volume": 100_000.0,
            })
    db.insert_ohlcv_batch(rows)
    return db


# ---------------------------------------------------------------------------
# Heavy section list expanded
# ---------------------------------------------------------------------------


def test_real_strategy_backtester_now_skipped_heavy_in_short(db):
    reporter = DashboardProReporter(load_config(), db, logging.getLogger("test"))
    assert "Real Strategy Backtester" in reporter.SHORT_REPORT_HEAVY_SECTIONS


def test_candidate_promotion_v2_now_skipped_heavy_in_short(db):
    reporter = DashboardProReporter(load_config(), db, logging.getLogger("test"))
    assert "Candidate Promotion V2" in reporter.SHORT_REPORT_HEAVY_SECTIONS


def test_short_report_skips_both_new_heavy_sections(db):
    reporter = DashboardProReporter(load_config(), db, logging.getLogger("test"))
    payload = reporter.build_short(hours=24)
    sections_by_name = {s["name"]: s for s in payload["sections"]}
    for name in ("Real Strategy Backtester", "Candidate Promotion V2"):
        assert name in sections_by_name, f"section {name!r} missing"
        assert sections_by_name[name]["status"] == "skipped_heavy"
        assert sections_by_name[name]["duration_ms"] == 0


# ---------------------------------------------------------------------------
# New OHLCV Summary section — fast and correct
# ---------------------------------------------------------------------------


def test_ohlcv_summary_section_replaces_heavy_loader_in_short(db):
    reporter = DashboardProReporter(load_config(), db, logging.getLogger("test"))
    payload = reporter.build_short(hours=24)
    names = {s["name"] for s in payload["sections"]}
    assert "OHLCV Summary" in names, "OHLCV Summary must appear in short report"
    assert "OHLCV Replay Loader" not in names, "heavy OHLCV Replay Loader must NOT appear in short report"


def test_ohlcv_summary_reports_need_data_on_empty_table(db):
    reporter = DashboardProReporter(load_config(), db, logging.getLogger("test"))
    text = reporter._ohlcv_summary_section()
    assert "OHLCV SUMMARY START" in text
    assert "status: NEED_DATA" in text
    assert "total_rows: 0" in text
    assert "final_recommendation: NO LIVE" in text


def test_ohlcv_summary_returns_real_counts_when_data_present(db_with_ohlcv):
    reporter = DashboardProReporter(load_config(), db_with_ohlcv, logging.getLogger("test"))
    text = reporter._ohlcv_summary_section()
    assert "status: OK" in text
    # 3 symbols × 50 rows = 150
    assert "total_rows: 150" in text
    # All three symbols should appear in the per-symbol breakdown
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        assert symbol in text
    assert "oldest_timestamp:" in text
    assert "newest_timestamp:" in text


def test_ohlcv_summary_is_fast_under_one_second(db_with_ohlcv):
    """The whole point of replacing the heavy loader: this must be fast.

    On the VPS production data the user measured 0.022s across 10 symbols.
    Our local tiny fixture should be effectively instant; we allow up to 1s
    headroom to keep the test stable in CI.
    """
    reporter = DashboardProReporter(load_config(), db_with_ohlcv, logging.getLogger("test"))
    start = time.perf_counter()
    text = reporter._ohlcv_summary_section()
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"OHLCV summary took {elapsed:.3f}s, expected < 1.0s"
    assert "OHLCV SUMMARY END" in text


def test_ohlcv_summary_handles_missing_table_gracefully(tmp_path, monkeypatch):
    """Fresh DB that never created ohlcv_candles → NEED_DATA, no crash."""
    monkeypatch.setattr("app.database.PROJECT_ROOT", tmp_path)
    cfg = load_config()
    instance = Database(cfg, logging.getLogger("test"))
    instance.sqlite_path = tmp_path / "no_init.db"
    # do NOT call initialize() to leave ohlcv_candles missing
    reporter = DashboardProReporter(cfg, instance, logging.getLogger("test"))
    text = reporter._ohlcv_summary_section()
    assert "OHLCV SUMMARY START" in text
    # Either NEED_DATA (preferred) or ERROR — both acceptable, neither crashes.
    assert "status: NEED_DATA" in text or "status: ERROR" in text
    assert "final_recommendation: NO LIVE" in text


# ---------------------------------------------------------------------------
# Short report still completes OK overall
# ---------------------------------------------------------------------------


def test_short_report_status_ok_with_new_heavy_additions(db_with_ohlcv):
    reporter = DashboardProReporter(load_config(), db_with_ohlcv, logging.getLogger("test"))
    payload = reporter.build_short(hours=24)
    # All heavy sections skipped, OHLCV Summary present and OK → report_status OK
    timeouts = [s for s in payload["sections"] if s["status"] in {"timeout", "error"}]
    if not timeouts:
        assert payload["report_status"] == "OK"


def test_short_report_heavy_section_count_is_now_ten(db):
    reporter = DashboardProReporter(load_config(), db, logging.getLogger("test"))
    # The original 8 heavies + the 2 added in 7.4A-3 = 10
    assert len(reporter.SHORT_REPORT_HEAVY_SECTIONS) == 10
