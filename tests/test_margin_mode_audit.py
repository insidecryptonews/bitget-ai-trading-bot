from app.config import BotConfig
from app.margin_mode_audit import MarginModeAudit, MarginModeAuditSmokeTest, _UnsafeConfig


def test_margin_mode_audit_confirms_isolated_guards_or_blocks_cross():
    ok_payload = MarginModeAudit(BotConfig()).build()
    bad_payload = MarginModeAudit(_UnsafeConfig()).build()

    assert ok_payload["final_recommendation"] == "NO LIVE"
    assert ok_payload["configured_margin_mode"] == "isolated"
    assert ok_payload["ensure_isolated_margin_present"] is True
    assert ok_payload["order_params_checked"] is True
    assert ok_payload["risk_manager_blocks_cross"] is True
    assert ok_payload["execution_engine_blocks_cross"] is True
    assert bad_payload["margin_mode_status"] == "CROSS_DETECTED_BAD"
    assert bad_payload["recommended_action"] == "FIX_MARGIN_MODE_BEFORE_ANY_PAPER_FILTER"


def test_margin_mode_audit_smoke_test_passes():
    text = MarginModeAuditSmokeTest(BotConfig()).to_text()

    assert "MARGIN MODE AUDIT SMOKE TEST START" in text
    assert "cross_detected_bad: true" in text
    assert "ensure_isolated_margin_present: true" in text
    assert "final_recommendation: NO LIVE" in text
    assert "result: PASS" in text
