from app.bot_integrity_audit import (
    BotIntegrityAudit,
    BotIntegrityAuditSmokeTest,
    DashboardAudit,
    LabelTimeAudit,
    SecurityAudit,
)
from app.config import BotConfig


class ReadOnlyFakeDB:
    def __init__(self):
        self.write_calls = 0

    def record_event(self, *args, **kwargs):
        self.write_calls += 1
        raise AssertionError("audit must not write events")

    def record_trade(self, *args, **kwargs):
        self.write_calls += 1
        raise AssertionError("audit must not write trades")

    def update_trade_status(self, *args, **kwargs):
        self.write_calls += 1
        raise AssertionError("audit must not mutate trades")

    def get_training_observation_summary_since(self, *args, **kwargs):
        return {
            "total": 10,
            "long_count": 2,
            "short_count": 1,
            "no_trade_count": 7,
            "high_score_count": 1,
            "operated_count": 0,
        }

    def get_high_score_label_summary_since(self, *args, **kwargs):
        return {
            "total_labels": 1,
            "tp1_count": 0,
            "tp2_count": 0,
            "sl_count": 1,
            "time_count": 0,
            "profit_factor": 0.0,
        }

    def get_signal_label_summary_since(self, *args, **kwargs):
        return {
            "total_labels": 3,
            "tp1_count": 1,
            "tp2_count": 0,
            "sl_count": 1,
            "time_count": 1,
            "profit_factor": 1.0,
        }

    def get_signal_path_metrics_summary_since(self, *args, **kwargs):
        return {"active_count": 0, "matured_count": 2, "insufficient_count": 0}

    def get_signal_path_metrics_source_summary_since(self, *args, **kwargs):
        return [
            {"source": "market_probe", "total": 1, "active_count": 0, "matured_count": 1},
            {"source": "trade_signal", "total": 1, "active_count": 0, "matured_count": 1},
        ]

    def fetch_labeled_signal_rows_since(self, *args, **kwargs):
        return [
            {"realized_return_pct": 0.01, "first_barrier_hit": "TP1"},
            {"realized_return_pct": -0.01, "first_barrier_hit": "SL"},
        ]

    def get_shadow_opportunity_group_summaries_since(self, *args, **kwargs):
        return [{"group_value": "BTCUSDT", "total_labels": 2, "profit_factor": 1.0}]

    def get_paper_trade_summary(self):
        return {"total": 0, "open": 0, "closed": 0}

    def fetch_open_paper_trades(self):
        return []

    def get_open_paper_positions_summary(self, limit=5):
        return []


def test_security_audit_current_defaults_are_safe_paper_only():
    text = SecurityAudit(BotConfig()).to_text()

    assert "SECURITY_AUDIT START" in text
    assert "can_send_real_orders: false" in text
    assert "live_execution_currently_reachable: false" in text
    assert "final_security_status: SAFE_PAPER_ONLY" in text
    assert "final_recommendation: NO LIVE" in text


def test_security_audit_detects_dangerous_live_config():
    config = BotConfig(paper_trading=False, live_trading=True, dry_run=False)
    text = SecurityAudit(config).to_text()

    assert "can_send_real_orders: true" in text
    assert "final_security_status: WARNING" in text or "final_security_status: UNSAFE" in text


def test_integrity_audit_is_read_only_for_fake_db():
    db = ReadOnlyFakeDB()
    text = BotIntegrityAudit(BotConfig(), db).to_text(hours=1)

    assert db.write_calls == 0
    assert "FINAL BOT AUDIT VERDICT START" in text
    assert "live_readiness: NO_LIVE" in text


def test_label_time_audit_marks_market_probe_as_not_actionable():
    text = LabelTimeAudit(BotConfig(), ReadOnlyFakeDB()).to_text(hours=1)

    assert "market_probe_mixed_with_signal_metrics: true" in text
    assert "market_probe rows must never be treated as actionable signal edge" in text


def test_dashboard_audit_does_not_overclaim_visual_quality():
    text = DashboardAudit(BotConfig()).to_text()

    assert "DASHBOARD_AUDIT START" in text
    assert "visual_quality_verified: false" in text
    assert "visual_status: FUNCTIONAL_NOT_VISUALLY_VERIFIED" in text
    assert "professional" in text


def test_bot_integrity_smoke_test_passes_and_keeps_no_live():
    text = BotIntegrityAuditSmokeTest(BotConfig(), ReadOnlyFakeDB()).to_text()

    assert "BOT INTEGRITY AUDIT SMOKE TEST START" in text
    assert "LIVE_TRADING_false: true" in text
    assert "DRY_RUN_true: true" in text
    assert "PAPER_TRADING_true: true" in text
    assert "result: PASS" in text
