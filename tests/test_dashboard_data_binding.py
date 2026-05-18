from app.config import BotConfig, PROJECT_ROOT
from app.dashboard_data_binding_audit import DashboardDataBindingAudit, DashboardDataBindingSmokeTest


def test_dashboard_data_binding_source_guards_exist():
    payload = DashboardDataBindingAudit(BotConfig(), None).build(hours=24)

    assert payload["final_recommendation"] == "NO LIVE"
    assert payload["score_incubator_in_report"] is True
    assert payload["new_audits_in_report"] is True
    assert payload["overview_consistency"] == "OK"
    assert payload["refresh_consistency"] == "OK"
    assert payload["paper_filter_enabled"] is False
    assert payload["paper_policy_filter_mode"] == "shadow"


def test_dashboard_data_binding_smoke_test_passes():
    text = DashboardDataBindingSmokeTest(BotConfig()).to_text()

    assert "DASHBOARD DATA BINDING SMOKE TEST START" in text
    assert "no_false_zero_guard: true" in text
    assert "score_incubator_available: true" in text
    assert "no_activation_enabled: true" in text
    assert "final_recommendation: NO LIVE" in text
    assert "result: PASS" in text


def test_dashboard_css_prevents_layout_overflow_at_normal_zoom():
    css = (PROJECT_ROOT / "app" / "static" / "dashboard.css").read_text(encoding="utf-8")
    js = (PROJECT_ROOT / "app" / "static" / "dashboard.js").read_text(encoding="utf-8")

    assert "overflow-wrap: anywhere" in css
    assert "word-break: break-word" in css
    assert "report-details pre" in css
    assert "table-layout: fixed" in css
    assert "labelsReady" in js
    assert "pendingText" in js
