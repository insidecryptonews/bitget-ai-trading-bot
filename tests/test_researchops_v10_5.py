"""ResearchOps V10.5 — provider verification + manifest v10.5 + data
readiness + Research Command Center dashboard tests.

All synthetic/offline. No network, no DB, no secrets, no API keys.
"""

from __future__ import annotations

import pathlib
import re

from app.labs.data_foundation_v10_5 import (
    MANIFEST_V105_REQUIRED_FIELDS,
    READY_NEED_VERIFIED_PROVIDER,
    SCHEMA_VERSION,
    ST_INVALID_V105,
    ST_SERIES_INCOMPLETE,
    build_data_readiness_v105,
    evaluate_manifest_v105,
)
from app.labs.external_data_acquisition_plan_v10_4 import ST_PROMOTE_ALLOWED
from app.labs.provider_verification_v10_5 import (
    NEEDS_MANUAL,
    REQUIRED_SYMBOLS,
    ST_PROXY_ONLY,
    ST_READY_FOR_HUMAN_AUTH,
    build_provider_scorecards,
    run_provider_verification_v105,
)
from app.labs.trader_dashboard_v104 import (
    DISABLED_CONTROLS,
    build_dashboard_view_model,
    dashboard_contract,
    render_dashboard_html,
)


# ---------------------------------------------------------------------------
# A. Provider verification V10.5
# ---------------------------------------------------------------------------

def test_provider_verification_roles_and_statuses():
    rep = run_provider_verification_v105()
    assert rep.primary == "Tardis.dev"
    assert rep.fallback == "CoinGlass"
    assert rep.cross_check == "Bitget official API"
    assert "proxy" in rep.proxy_only.lower()
    by_role = {p["role"]: p for p in rep.providers}
    assert by_role["proxy_only"]["status"] == ST_PROXY_ONLY
    assert by_role["proxy_only"]["bitget_perp_supported"] is False


def test_provider_verification_unknowns_stay_manual():
    """Unknown fields must be NEEDS_MANUAL_VERIFICATION — never invented."""
    for card in build_provider_scorecards():
        assert card.history_confirmed == NEEDS_MANUAL
        assert card.data_types_confirmed == NEEDS_MANUAL
        assert card.timeframes_confirmed == NEEDS_MANUAL
        assert card.quality_checks_pending  # all pending
        assert card.commercial_checks_pending


def test_provider_verification_never_authorizes_or_calls_out():
    rep = run_provider_verification_v105()
    assert rep.any_paid_download_authorized is False
    assert rep.any_provider_ready_for_authorization is False  # nothing verified yet
    assert rep.no_external_calls_made is True
    assert rep.paper_ready is False
    assert rep.live_ready is False
    assert rep.final_recommendation == "NO LIVE"
    assert all(p["status"] != ST_READY_FOR_HUMAN_AUTH for p in rep.providers)


def test_provider_verification_covers_research_symbols():
    assert len(REQUIRED_SYMBOLS) == 10
    rep = run_provider_verification_v105()
    for p in rep.providers:
        assert p["symbols_required"] == REQUIRED_SYMBOLS
        assert p["required_history"]["minimum_days"] == 180
        assert p["required_history"]["preferred_days"] == 365
        assert p["sample_requirement"]["must_obtain_sample_before_paid_download"] is True


def test_provider_module_makes_no_external_calls():
    src = pathlib.Path("app/labs/provider_verification_v10_5.py").read_text(encoding="utf-8")
    assert "urllib" not in src and "requests" not in src and "http.client" not in src
    assert "os.getenv" not in src and "os.environ" not in src


# ---------------------------------------------------------------------------
# B. Manifest contract v10.5
# ---------------------------------------------------------------------------

def _manifest_v105(**over):
    man = {f: 0 for f in MANIFEST_V105_REQUIRED_FIELDS}
    man.update({
        "source_provider": "tardis_dev", "license_terms": "research",
        "requested_range": "365d", "actual_covered_range": "365d",
        "symbols": ["BTCUSDT"], "timeframes": ["1h"],
        "data_types": ["ohlcv", "open_interest", "funding", "liquidations"],
        "rows_by_type": {"perp_market_state": 8760},
        "missing_oi_ratio": 0.02, "missing_oi_status": "DATA_OK",
        "gap_count": 0, "duplicate_count": 0,
        "coverage_ratio": 0.97, "clean_days": 365.0,
        "checksums_sha256": {"perp_market_state.csv": "ab" * 32},
        "explicit_human_authorization": True,
        "paid_download_authorized": True,
        "license_terms_confirmed": True,
        "authorization_reference": "HUMAN-V105-TEST-001",
        "missing_funding_ratio": 0.01,
        "missing_liquidations_ratio": 0.02,
        "timezone": "UTC", "timestamp_unit": "unix_ms",
        "generated_at": "2026-06-11T00:00:00Z",
        "schema_version": SCHEMA_VERSION, "import_status": "STAGED",
    })
    man.update(over)
    return man


def test_manifest_v105_missing_fields_invalid():
    ev = evaluate_manifest_v105({})
    assert ev.status == ST_INVALID_V105
    assert ev.promote_allowed is False
    assert ev.import_status == "BLOCKED"
    ev2 = evaluate_manifest_v105(None)
    assert ev2.promote_allowed is False


def test_manifest_v105_no_human_authorization_no_promote():
    ev = evaluate_manifest_v105(_manifest_v105(
        explicit_human_authorization=None, authorization_reference=""))
    assert ev.promote_allowed is False
    assert ev.status == "AUTHORIZATION_REQUIRED"


def test_manifest_v105_paid_source_without_paid_auth_no_promote():
    ev = evaluate_manifest_v105(_manifest_v105(paid_download_authorized=False))
    assert ev.promote_allowed is False
    assert "paid_download_not_authorized" in ev.blockers


def test_manifest_v105_license_missing_no_promote():
    ev = evaluate_manifest_v105(_manifest_v105(license_terms_confirmed=False))
    assert ev.promote_allowed is False
    assert "license_terms_not_confirmed" in ev.blockers


def test_manifest_v105_bad_quality_no_promote():
    assert evaluate_manifest_v105(_manifest_v105(coverage_ratio=0.5)).promote_allowed is False
    assert evaluate_manifest_v105(_manifest_v105(gap_count=9000)).promote_allowed is False
    assert evaluate_manifest_v105(_manifest_v105(duplicate_count=9000)).promote_allowed is False


def test_manifest_v105_free_source_staged_but_not_promoted_without_human_auth():
    """A free source can exist as staging material, but promote still requires
    the explicit human authorization fields."""
    ev = evaluate_manifest_v105(_manifest_v105(
        source_provider="bitget_official", paid_download_authorized=False,
        explicit_human_authorization=None))
    assert ev.promote_allowed is False
    assert ev.status == "AUTHORIZATION_REQUIRED"
    # With full human auth the free source can promote (research-only).
    ev2 = evaluate_manifest_v105(_manifest_v105(
        source_provider="bitget_official", paid_download_authorized=False))
    assert ev2.status == ST_PROMOTE_ALLOWED
    assert ev2.paper_ready is False and ev2.live_ready is False


def test_manifest_v105_series_completeness_gates():
    ev = evaluate_manifest_v105(_manifest_v105(missing_funding_ratio=0.5))
    assert ev.status == ST_SERIES_INCOMPLETE
    assert "missing_funding_ratio_invalid_or_too_high" in ev.blockers
    ev2 = evaluate_manifest_v105(_manifest_v105(missing_liquidations_ratio=None))
    assert ev2.status == ST_SERIES_INCOMPLETE
    ev3 = evaluate_manifest_v105(_manifest_v105(timezone="Europe/Madrid"))
    assert "timezone_must_be_utc" in ev3.blockers
    ev4 = evaluate_manifest_v105(_manifest_v105(timestamp_unit="iso8601"))
    assert "timestamp_unit_must_be_unix_ms_or_unix_s" in ev4.blockers
    ev5 = evaluate_manifest_v105(_manifest_v105(schema_version="v10.4"))
    assert "schema_version_mismatch" in ev5.blockers


def test_manifest_v105_full_pass_is_research_only():
    ev = evaluate_manifest_v105(_manifest_v105())
    assert ev.status == ST_PROMOTE_ALLOWED
    assert ev.promote_allowed is True
    assert ev.import_status == "STAGED_READY_FOR_PROMOTE"
    assert ev.paper_ready is False
    assert ev.live_ready is False
    assert ev.final_recommendation == "NO LIVE"


# ---------------------------------------------------------------------------
# C. Data readiness V10.5
# ---------------------------------------------------------------------------

def test_data_readiness_no_external_data_needs_verified_provider():
    r = build_data_readiness_v105(data_readiness_snapshot=None, provider_report=None)
    assert r.status == READY_NEED_VERIFIED_PROVIDER
    assert r.provider_readiness == "NO_PROVIDER_VERIFIED"
    assert r.paper_ready is False
    assert r.live_ready is False
    assert r.final_recommendation == "NO LIVE"
    assert r.next_required_human_action


def test_data_readiness_short_history_not_ready():
    snap = {"current_clean_days": 63.46,
            "current_history_status": "TOO_SHORT_FOR_FINAL_VALIDATION",
            "missing_oi_status": "MISSING_OI_CLUSTERED",
            "oi_bucket_policy": "BLOCK_OI_BUCKETS",
            "backtester_readiness": "NEED_LONG_HISTORY"}
    r = build_data_readiness_v105(
        data_readiness_snapshot=snap,
        provider_report={"any_provider_ready_for_authorization": False})
    assert r.status == READY_NEED_VERIFIED_PROVIDER
    assert r.clean_days == 63.46
    blockers = " ".join(r.top_blockers)
    assert "63.46" in blockers and "180" in blockers
    assert "OI buckets blocked" in blockers
    assert r.paper_ready is False and r.live_ready is False


def test_data_readiness_oi_blocked_stays_blocked():
    snap = {"current_clean_days": 200.0, "oi_bucket_policy": "BLOCK_OI_BUCKETS",
            "missing_oi_status": "MISSING_OI_CLUSTERED"}
    r = build_data_readiness_v105(
        data_readiness_snapshot=snap,
        provider_report={"any_provider_ready_for_authorization": True})
    assert r.oi_bucket_policy == "BLOCK_OI_BUCKETS"
    assert any("OI buckets blocked" in b for b in r.top_blockers)
    assert r.final_recommendation == "NO LIVE"


# ---------------------------------------------------------------------------
# D. Research Command Center dashboard
# ---------------------------------------------------------------------------

def _html():
    return render_dashboard_html(build_dashboard_view_model())


def test_command_center_sections_present():
    html = _html()
    for marker in ("mission-bar", "pipeline", "Why No Trade / Why No Edge",
                   "Provider Readiness", "Learning Status",
                   "Strategy Research Lab", "SSH Tunnel Help"):
        assert marker in html, marker


def test_command_center_no_live_and_research_only():
    html = _html()
    assert "NO LIVE" in html
    assert "RESEARCH ONLY" in html


def test_command_center_locked_anti_features_present_and_disabled():
    html = _html()
    for control in ("Copy Trading", "Leverage Control", "777 Spin / Casino Mode"):
        assert control in DISABLED_CONTROLS
        assert control in html
    # every locked button is disabled with the tooltip; none has a handler
    assert html.count("disabled title=") == len(DISABLED_CONTROLS)
    assert "onclick" not in html.lower()


def test_command_center_get_only_no_forms_no_mutables():
    html = _html()
    lower = html.lower()
    assert "<form" not in lower
    assert '"post"' not in lower and '"put"' not in lower and '"delete"' not in lower
    targets = re.findall(r'"(/api/[^"]+)"', html)
    assert targets and all(t.startswith("/api/researchops/v104/") for t in targets)
    assert html.count("fetch(") == 1  # single getJSON helper


def test_command_center_does_not_expose_token():
    """The page never embeds a token value: it only reads it from the
    client's own URL at runtime."""
    html = _html()
    assert "URLSearchParams" in html  # read from browser URL only
    assert "dashboard_auth_token" not in html
    # The SSH help shows a placeholder, never a real token.
    assert "&lt;your_token&gt;" in html


def test_command_center_ssh_help_never_public_port():
    html = _html()
    assert "ssh -L 18080:127.0.0.1:8080 ubuntu@YOUR_VPS_IP" in html
    assert "Never expose port 8080 publicly" in html


def test_command_center_contract_declares_v105_panels():
    c = dashboard_contract()
    for panel in ("mission_bar", "pipeline", "why_no_edge",
                  "strategy_research_lab", "ssh_tunnel_help"):
        assert panel in c["panels"], panel
    assert c["mutable_endpoints"] == []
    assert c["post_forms"] == 0
    assert c["final_recommendation"] == "NO LIVE"


# ---------------------------------------------------------------------------
# E. Safety: V10.5 modules are pure/read-only
# ---------------------------------------------------------------------------

def test_v105_modules_no_keys_no_env_no_db_writes():
    for name in ("provider_verification_v10_5.py", "data_foundation_v10_5.py"):
        src = pathlib.Path("app/labs", name).read_text(encoding="utf-8")
        assert "os.getenv" not in src and "os.environ" not in src, name
        assert "API_KEY" not in src and "SECRET" not in src, name
        assert "INSERT INTO" not in src and "UPDATE " not in src, name
        assert "DELETE FROM" not in src and "DROP TABLE" not in src, name
        assert "import requests" not in src and "urllib" not in src, name


def test_v105_reports_never_paper_or_live_ready():
    assert run_provider_verification_v105().as_dict()["live_ready"] is False
    assert evaluate_manifest_v105(_manifest_v105()).as_dict()["live_ready"] is False
    r = build_data_readiness_v105(data_readiness_snapshot=None, provider_report=None)
    assert r.as_dict()["paper_ready"] is False
    assert r.as_dict()["live_ready"] is False
