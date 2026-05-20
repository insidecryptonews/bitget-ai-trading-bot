from __future__ import annotations

import time

from app.config import BotConfig
from app.dashboard_pro import DashboardProReporter


class DummyDb:
    sqlite_path = None

    def get_open_paper_positions_summary(self, limit=10):
        return []


class DummyLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


def test_short_report_section_timeout_returns_partial_section():
    reporter = DashboardProReporter(BotConfig(), DummyDb())

    section = reporter._run_section("slow", lambda: (time.sleep(0.08) or "late"), timeout_seconds=0.01)

    assert section.status == "timeout"
    assert "SECTION_TIMEOUT" in section.text
    assert "SECTION_TIMEOUT" in section.warning


def test_short_report_sanitizes_secrets_and_includes_no_live():
    reporter = DashboardProReporter(BotConfig(), DummyDb())

    section = reporter._run_section("secret", lambda: "API_KEY=abc123\nfinal_recommendation: NO LIVE", timeout_seconds=1.0)

    assert section.status == "ok"
    assert "abc123" not in section.text
    assert "***" in section.text
    assert "NO LIVE" in section.text


def test_dashboard_report_timeout_smoke_passes(tmp_path):
    from app.database import Database
    from app.dashboard_pro import build_dashboard_short_report
    from app.research_lab import ResearchLab

    config = BotConfig(data_vault_export_dir=str(tmp_path / "training_exports"))
    db = Database(config, logger=DummyLogger())
    db.sqlite_path = tmp_path / "report.db"
    db.initialize()

    payload = build_dashboard_short_report(config, db, hours=24)
    assert payload["report_status"] in {"OK", "PARTIAL_REPORT"}
    assert payload["final_recommendation"] == "NO LIVE"
    assert "NO LIVE" in payload["text"]

    text = ResearchLab(db, config, logger=DummyLogger()).dashboard_report_timeout_smoke_test()

    assert "result: PASS" in text
    assert "backup_restore_live_executed: false" in text
    assert "NO LIVE" in text
