"""ResearchOps V10.4 — provider verification + acquisition plan + intake +
edge hunter contract + near-real-time read-only trader terminal tests.

All synthetic. No DB, no network, no real data, no secrets, no API keys.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request

import pytest

from app.labs.edge_hunter_contract_v10_4 import (
    GATE_NEED_LONG_HISTORY,
    GATE_NEED_MORE_SAMPLES,
    GATE_OI_BLOCKED,
    GATE_REJECT,
    GATE_SHADOW,
    build_edge_hunter_contract,
    evaluate_edge_hunter_gate,
)
from app.labs.external_data_acquisition_plan_v10_4 import (
    MANIFEST_AUTHORIZATION_FIELDS,
    MANIFEST_REQUIRED_FIELDS,
    ST_AUTHORIZATION_REQUIRED,
    ST_INVALID_MANIFEST,
    ST_NEED_LONG_HISTORY,
    ST_PROMOTE_ALLOWED,
    ST_QUALITY_FAIL,
    ST_UNDERCOVERAGE,
    build_importer_contract,
    evaluate_acquisition_manifest,
)
from app.labs.external_provider_verification_v10_4 import (
    MANUAL_CHECKS,
    run_provider_verification,
)
from app.labs.external_research_intake_v10_4 import (
    NEEDS_BACKTEST,
    NEEDS_DATA,
    NEEDS_RISK_REVIEW,
    NEEDS_WALK_FORWARD,
    PAPER_CANDIDATE_PENDING,
    REJECT_LOOKAHEAD,
    REJECT_OVERFIT,
    REJECT_UNTRADABLE,
    SHADOW_ELIGIBLE,
    ResearchIdea,
    classify_idea,
    run_research_intake,
)
from app.labs.trader_dashboard_v104 import (
    DISABLED_CONTROLS,
    LOCK_TOOLTIP,
    build_dashboard_view_model,
    dashboard_contract,
    derive_safety_view,
    render_dashboard_html,
)


# ---------------------------------------------------------------------------
# A. Provider verification
# ---------------------------------------------------------------------------

def test_provider_verification_roles():
    rep = run_provider_verification()
    assert rep.primary_candidate == "tardis_dev"
    assert rep.fallback_candidate == "coinglass"
    assert rep.cross_check_provider == "bitget_official"
    assert rep.proxy_provider == "binance_okx_proxy"
    assert len(rep.providers) == 8


def test_provider_verification_candidates_require_manual_checks():
    rep = run_provider_verification()
    by_id = {p["provider_id"]: p for p in rep.providers}
    for pid in ("tardis_dev", "coinglass"):
        assert by_id[pid]["verification_complete"] is False
        assert set(by_id[pid]["manual_checks_pending"]) == set(MANUAL_CHECKS)


def test_provider_verification_never_authorizes_paid_download():
    rep = run_provider_verification()
    assert rep.any_paid_download_authorized is False
    assert rep.no_paid_download_without_authorization is True
    assert all(p["paid_download_authorized"] is False for p in rep.providers)


def test_provider_verification_never_paper_or_live():
    rep = run_provider_verification()
    assert rep.paper_ready is False
    assert rep.live_ready is False
    assert rep.final_recommendation == "NO LIVE"


def test_v104_modules_do_not_read_api_keys_from_env():
    """V10.4 read-only modules must not read env vars or request API keys."""
    import pathlib

    labs = pathlib.Path("app/labs")
    files = [
        "external_provider_verification_v10_4.py",
        "external_data_acquisition_plan_v10_4.py",
        "external_research_intake_v10_4.py",
        "edge_hunter_contract_v10_4.py",
        "trader_dashboard_v104.py",
    ]
    for name in files:
        src = (labs / name).read_text(encoding="utf-8")
        assert "os.getenv" not in src and "os.environ" not in src, name
        assert "urllib.request" not in src and "http.client" not in src, name
        assert "requests.get" not in src and "requests.post" not in src, name
        assert "import requests" not in src and "import httpx" not in src, name


def test_coinalyze_still_insufficient_for_180d():
    rep = run_provider_verification()
    coin = next(p for p in rep.providers if p["provider_id"] == "coinalyze")
    assert coin["suitable_for_180d"] is False
    assert coin["suitable_for_365d"] is False


# ---------------------------------------------------------------------------
# B. Data acquisition plan
# ---------------------------------------------------------------------------

def _manifest(**over):
    man = {f: 0 for f in MANIFEST_REQUIRED_FIELDS}
    man.update({
        "source_provider": "tardis_dev", "license_terms": "research",
        "requested_range": "180d", "actual_covered_range": "180d",
        "symbols": ["ETHUSDT"], "timeframes": ["1h"],
        "data_types": ["ohlcv", "open_interest", "funding", "liquidations"],
        "rows_by_type": {"perp_market_state": 4320},
        "missing_oi_ratio": 0.02, "missing_oi_status": "DATA_OK",
        "gap_count": 0, "duplicate_count": 0,
        "coverage_ratio": 0.97, "clean_days": 200.0,
        "checksums_sha256": {"perp_market_state.csv": "ab" * 32},
        # V10.4.1 — explicit human authorization (tests opt OUT to verify the gate)
        "explicit_human_authorization": True,
        "paid_download_authorized": True,
        "license_terms_confirmed": True,
        "authorization_reference": "HUMAN-APPROVAL-TEST-001",
    })
    man.update(over)
    return man


def test_acquisition_contract_blocks_paid_download_and_replacement():
    c = build_importer_contract()
    assert "no_paid_download_authorization" in c["blocks_import"]
    assert "replace_good_raw_with_insufficient_staging" in c["never"]
    assert c["final_recommendation"] == "NO LIVE"


def test_acquisition_invalid_manifest_blocks_promote():
    ev = evaluate_acquisition_manifest({})
    assert ev.status == ST_INVALID_MANIFEST
    assert ev.promote_allowed is False
    assert ev.do_not_replace_raw is True


def test_acquisition_undercoverage_blocks_even_with_long_history():
    ev = evaluate_acquisition_manifest(_manifest(coverage_ratio=0.5, clean_days=400))
    assert ev.status == ST_UNDERCOVERAGE
    assert ev.promote_allowed is False
    assert ev.do_not_replace_raw is True


def test_acquisition_short_history_blocks_promote():
    ev = evaluate_acquisition_manifest(_manifest(clean_days=63.46))
    assert ev.status == ST_NEED_LONG_HISTORY
    assert ev.promote_allowed is False


def test_acquisition_quality_gate_failures_block():
    ev = evaluate_acquisition_manifest(_manifest(gap_count=400))
    assert ev.status == ST_QUALITY_FAIL
    ev2 = evaluate_acquisition_manifest(_manifest(checksums_sha256={}))
    assert ev2.status == ST_QUALITY_FAIL


def test_acquisition_oi_unknown_or_clustered_blocks_oi_buckets():
    for status in ("NEED_MORE_DATA", "UNKNOWN", "NO_AUDIT", "", "MISSING_OI_CLUSTERED"):
        ev = evaluate_acquisition_manifest(_manifest(missing_oi_status=status))
        assert ev.oi_bucket_policy == "BLOCK_OI_BUCKETS", status
    ev_high = evaluate_acquisition_manifest(_manifest(missing_oi_ratio=0.2467))
    assert ev_high.oi_bucket_policy == "BLOCK_OI_BUCKETS"


def test_acquisition_promote_allowed_is_research_only():
    ev = evaluate_acquisition_manifest(_manifest())
    assert ev.status == ST_PROMOTE_ALLOWED
    assert ev.promote_allowed is True
    assert ev.authorization_ok is True
    assert ev.oi_bucket_policy == "ALLOW_OI_BUCKETS_WITH_CARE"
    assert ev.paper_ready is False
    assert ev.live_ready is False
    assert ev.final_recommendation == "NO LIVE"


# --- V10.4.1 (Codex P1): explicit human authorization gate ---

def test_acquisition_perfect_quality_without_authorization_is_blocked():
    """Even with every quality gate passing, no authorization => no promote."""
    ev = evaluate_acquisition_manifest(_manifest(
        explicit_human_authorization=None, paid_download_authorized=None,
        license_terms_confirmed=None, authorization_reference=None,
    ))
    assert ev.status == ST_AUTHORIZATION_REQUIRED
    assert ev.promote_allowed is False
    assert ev.do_not_replace_raw is True
    assert "missing_explicit_human_authorization" in ev.blockers


def test_acquisition_missing_authorization_fields_means_not_authorized():
    """Absent fields are NOT treated as safe defaults."""
    man = _manifest()
    for f in MANIFEST_AUTHORIZATION_FIELDS:
        man.pop(f, None)
    ev = evaluate_acquisition_manifest(man)
    assert ev.status == ST_AUTHORIZATION_REQUIRED
    assert ev.promote_allowed is False


def test_acquisition_paid_provider_requires_paid_download_authorization():
    ev = evaluate_acquisition_manifest(_manifest(paid_download_authorized=False))
    assert ev.status == ST_AUTHORIZATION_REQUIRED
    assert "paid_download_not_authorized" in ev.blockers


def test_acquisition_unknown_provider_treated_as_paid():
    ev = evaluate_acquisition_manifest(_manifest(
        source_provider="some_new_vendor", paid_download_authorized=False,
    ))
    assert ev.status == ST_AUTHORIZATION_REQUIRED
    assert "paid_download_not_authorized" in ev.blockers


def test_acquisition_license_and_reference_required():
    ev = evaluate_acquisition_manifest(_manifest(license_terms_confirmed=False))
    assert ev.status == ST_AUTHORIZATION_REQUIRED
    assert "license_terms_not_confirmed" in ev.blockers
    ev2 = evaluate_acquisition_manifest(_manifest(authorization_reference="  "))
    assert ev2.status == ST_AUTHORIZATION_REQUIRED
    assert "missing_authorization_reference" in ev2.blockers


def test_acquisition_authorization_truthy_strings_are_not_enough():
    """Only the exact boolean True authorizes — '1'/'yes' strings do not."""
    ev = evaluate_acquisition_manifest(_manifest(explicit_human_authorization="yes"))
    assert ev.status == ST_AUTHORIZATION_REQUIRED


def test_acquisition_contract_declares_authorization_rule():
    c = build_importer_contract()
    assert "missing_explicit_human_authorization" in c["blocks_import"]
    assert set(c["authorization_required_fields"]) == set(MANIFEST_AUTHORIZATION_FIELDS)


# ---------------------------------------------------------------------------
# C. External research intake
# ---------------------------------------------------------------------------

def _idea(**over):
    base = dict(source_name="paper", source_type="paper", claim="funding edge",
                symbols=["ETHUSDT"], side="SHORT", timeframe="1h",
                lookahead_risk="low", overfit_risk="low",
                tradable_on_bitget=True)
    base.update(over)
    return ResearchIdea(**base)


def test_intake_rejects_lookahead_and_overfit_first():
    assert classify_idea(_idea(lookahead_risk="high")) == REJECT_LOOKAHEAD
    assert classify_idea(_idea(overfit_risk="HIGH")) == REJECT_OVERFIT


def test_intake_rejects_untradable():
    assert classify_idea(_idea(tradable_on_bitget=False)) == REJECT_UNTRADABLE
    assert classify_idea(_idea(symbols=[])) == REJECT_UNTRADABLE
    assert classify_idea(_idea(side="")) == REJECT_UNTRADABLE


def test_intake_progression_to_shadow_ceiling():
    assert classify_idea(_idea(data_requirements=["oi_180d"], data_available=False)) == NEEDS_DATA
    assert classify_idea(_idea(backtested=False)) == NEEDS_BACKTEST
    assert classify_idea(_idea(backtested=True, walk_forward_passed=False)) == NEEDS_WALK_FORWARD
    assert classify_idea(_idea(backtested=True, walk_forward_passed=True)) == SHADOW_ELIGIBLE


# --- V10.4.1 (Codex P1): unknown risk is not safe ---

def test_intake_unknown_lookahead_risk_never_reaches_shadow():
    idea = _idea(backtested=True, walk_forward_passed=True, lookahead_risk="unknown")
    status = classify_idea(idea)
    assert status == NEEDS_RISK_REVIEW
    assert status not in (SHADOW_ELIGIBLE, PAPER_CANDIDATE_PENDING)


def test_intake_unknown_overfit_risk_never_reaches_shadow():
    idea = _idea(backtested=True, walk_forward_passed=True, overfit_risk="unknown")
    assert classify_idea(idea) == NEEDS_RISK_REVIEW


def test_intake_missing_or_empty_risks_never_reach_shadow():
    for risk in ("", None, "  ", "medium", "tbd", "n/a"):
        idea = _idea(backtested=True, walk_forward_passed=True,
                     lookahead_risk=risk, overfit_risk="low")
        assert classify_idea(idea) == NEEDS_RISK_REVIEW, repr(risk)
        idea2 = _idea(backtested=True, walk_forward_passed=True,
                      lookahead_risk="low", overfit_risk=risk)
        assert classify_idea(idea2) == NEEDS_RISK_REVIEW, repr(risk)


def test_intake_high_risks_still_rejected_not_parked():
    assert classify_idea(_idea(lookahead_risk="high")) == REJECT_LOOKAHEAD
    assert classify_idea(_idea(overfit_risk="severe")) == REJECT_OVERFIT


def test_intake_only_explicit_low_risks_reach_shadow_max():
    idea = _idea(backtested=True, walk_forward_passed=True,
                 lookahead_risk="low", overfit_risk="controlled")
    assert classify_idea(idea) == SHADOW_ELIGIBLE  # ceiling, never paper/live
    rep = run_research_intake([idea])
    assert rep.paper_ready is False
    assert rep.live_ready is False


def test_intake_never_returns_live_or_paper_ready():
    rep = run_research_intake([
        _idea(backtested=True, walk_forward_passed=True),
        _idea(lookahead_risk="high"),
    ])
    assert rep.paper_ready is False
    assert rep.live_ready is False
    assert rep.paper_filter_enabled is False
    assert rep.final_recommendation == "NO LIVE"
    for idea in rep.ideas:
        assert idea["final_status"] != "LIVE_READY"


# ---------------------------------------------------------------------------
# D. Edge hunter contract
# ---------------------------------------------------------------------------

def test_edge_hunter_is_contract_not_operational():
    c = build_edge_hunter_contract()
    assert c["output_ceiling"] == GATE_SHADOW
    assert "live_readiness" in c["never"]
    assert "auto_promotion" in c["never"]
    assert c["final_recommendation"] == "NO LIVE"


def test_edge_hunter_gate_order_and_ceiling():
    g = evaluate_edge_hunter_gate(clean_days=63, samples=500, net_ev=1.0,
                                  net_pf=2.0, cost_x2_pass=True, oos_pass=True)
    assert g.verdict == GATE_NEED_LONG_HISTORY
    g = evaluate_edge_hunter_gate(clean_days=200, samples=10, net_ev=1.0,
                                  net_pf=2.0, cost_x2_pass=True, oos_pass=True)
    assert g.verdict == GATE_NEED_MORE_SAMPLES
    g = evaluate_edge_hunter_gate(clean_days=200, samples=200, net_ev=-0.1,
                                  net_pf=2.0, cost_x2_pass=True, oos_pass=True)
    assert g.verdict == GATE_REJECT
    g = evaluate_edge_hunter_gate(clean_days=200, samples=200, net_ev=1.0,
                                  net_pf=2.0, cost_x2_pass=True, oos_pass=True,
                                  uses_oi=True, missing_oi_blocked=True)
    assert g.verdict == GATE_OI_BLOCKED


def test_edge_hunter_never_live_ready_even_on_best_case():
    g = evaluate_edge_hunter_gate(candidate="best", clean_days=400, samples=500,
                                  net_ev=2.0, net_pf=2.5, gross_pf=3.0,
                                  time_death_rate=0.1, one_trade_dominance=0.05,
                                  cost_x2_pass=True, oos_pass=True)
    assert g.verdict == GATE_SHADOW  # ceiling
    assert g.paper_ready is False
    assert g.live_ready is False
    assert g.final_recommendation == "NO LIVE"


# ---------------------------------------------------------------------------
# E. Dashboard view-model + HTML (static guarantees)
# ---------------------------------------------------------------------------

def _html():
    return render_dashboard_html(build_dashboard_view_model())


def test_dashboard_contract_readonly_no_mutables():
    c = dashboard_contract()
    assert c["read_only"] is True
    assert c["mutable_endpoints"] == []
    assert c["post_forms"] == 0
    assert c["near_real_time"] is True
    assert c["poll_method"] == "GET"
    assert c["poll_endpoint"].startswith("/api/researchops/v104/")
    assert c["final_recommendation"] == "NO LIVE"


def test_dashboard_html_banner_and_research_only():
    html = _html()
    assert "NO LIVE" in html
    assert "RESEARCH ONLY" in html


def test_dashboard_html_disabled_controls_with_lock_tooltip():
    html = _html()
    assert html.count('disabled title="' + LOCK_TOOLTIP + '"') == len(DISABLED_CONTROLS)
    for label in DISABLED_CONTROLS:
        assert label in html
    # no click handlers anywhere
    assert "onclick" not in html.lower()
    assert "addEventListener('click'" not in html
    assert 'addEventListener("click"' not in html


def test_dashboard_html_no_forms_no_mutable_methods():
    html = _html()
    lower = html.lower()
    assert "<form" not in lower
    assert '"post"' not in lower
    assert '"put"' not in lower
    assert '"delete"' not in lower
    assert "method: \"get\"" in lower


def test_dashboard_html_fetch_only_readonly_get_endpoints():
    html = _html()
    targets = re.findall(r'"(/api/[^"]+)"', html)
    assert targets, "polling endpoint must be defined"
    assert all(t.startswith("/api/researchops/v104/") for t in targets)
    poll_targets = re.findall(r'POLL_URL\s*=\s*"([^"]+)"', html)
    assert poll_targets == ["/api/researchops/v104/dashboard-state"]
    assert html.count("fetch(") == 1
    assert "blocked: non-readonly endpoint" in html
    assert "function warm" not in html
    assert "WARM_URLS" not in html
    assert "setInterval(warm" not in html
    for heavy in ("data-readiness", "candidates", "net-edge"):
        assert f"/api/researchops/v104/{heavy}" not in html


def test_dashboard_contract_declares_all_ten_endpoints():
    c = dashboard_contract()
    expected = {f"/api/researchops/v104/{e}" for e in (
        "overview", "safety", "data-readiness", "provider-readiness",
        "provider-verification", "candidates", "net-edge", "paper-monitor",
        "signal-monitor", "dashboard-state")}
    assert expected == set(c["readonly_api_endpoints"])
    assert c["polling_never_computes_heavy_work"] is True
    assert c["automatic_endpoints"] == ["/api/researchops/v104/dashboard-state"]
    assert c["heavy_panels_mode"] == "CACHE_PEEK_ONLY"
    assert c["heavy_refresh_mode"] == "CLI_ONLY"


def test_dashboard_html_has_update_timestamp_and_connection_states():
    html = _html()
    assert "last-update" in html
    for state in ("LOADING", "LIVE-POLL", "STALE", "ERROR"):
        assert state in html
    assert "data may be outdated" in html


def test_dashboard_html_paper_pnl_labelled_not_real():
    html = _html()
    assert "NOT real" in html


def test_dashboard_safety_derivation_is_honest():
    safe = derive_safety_view({"live_trading": False, "dry_run": True,
                               "paper_trading": True, "paper_filter_enabled": False})
    assert safe["can_send_real_orders"] is False
    assert safe["all_safe"] is True
    assert safe["security_status"] == "SAFE_PAPER_ONLY"
    unsafe = derive_safety_view({"live_trading": True, "dry_run": False,
                                 "paper_trading": False, "paper_filter_enabled": True})
    assert unsafe["can_send_real_orders"] is True
    assert unsafe["all_safe"] is False
    assert unsafe["security_status"] == "SAFETY_REVIEW_REQUIRED"


def test_dashboard_view_model_never_allows_live():
    vm = build_dashboard_view_model()
    assert vm["read_only"] is True
    assert vm["live_allowed"] is False
    assert vm["final_recommendation"] == "NO LIVE"


# ---------------------------------------------------------------------------
# F. Live server routes (read-only GET; uses an ephemeral local port)
# ---------------------------------------------------------------------------

class _FakeConfig:
    enable_training_dashboard = True
    dashboard_auth_token = ""
    dashboard_refresh_seconds = 7
    live_trading = False
    dry_run = True
    paper_trading = True
    enable_paper_policy_filter = False
    require_single_worker_lock = True
    training_runtime_profile = "railway_lightweight"


def _start_test_server(config):
    """V10.4.3.1 (Codex P2) — ephemeral port (0) to avoid fixed-port
    flakiness on Windows; caller must close via the returned closer."""
    from app.health_server import HealthState, start_health_server

    state = HealthState(mode="paper")
    thread = start_health_server(
        state, 0, logging.getLogger("test-v104"),
        config=config, db=None, training_pulse=None, telegram_notifier=None,
    )
    assert thread.server_ready.wait(5)
    server = thread.server_box.get("server")
    assert server is not None, "test server failed to bind an ephemeral port"
    port = server.server_address[1]
    time.sleep(0.1)

    def closer():
        try:
            server.shutdown()
        finally:
            server.server_close()

    return f"http://127.0.0.1:{port}", closer, state


@pytest.fixture(scope="module")
def v104_server():
    base, closer, _state = _start_test_server(_FakeConfig())
    try:
        yield base
    finally:
        closer()


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=20) as resp:
        return resp.status, resp.read().decode("utf-8")


def test_server_trader_terminal_loads(v104_server):
    status, html = _get(v104_server, "/trader-terminal")
    assert status == 200
    assert "NO LIVE" in html
    assert "RESEARCH ONLY" in html
    assert "disabled" in html
    assert LOCK_TOOLTIP in html
    assert "<form" not in html.lower()
    assert "last-update" in html


def test_server_dashboard_state_is_readonly_no_live(v104_server):
    status, body = _get(v104_server, "/api/researchops/v104/dashboard-state")
    assert status == 200
    payload = json.loads(body)
    assert payload["final_recommendation"] == "NO LIVE"
    assert payload["read_only"] is True
    assert payload["live_allowed"] is False
    assert payload["safety"]["can_send_real_orders"] is False
    assert payload["safety"]["live_trading"] is False
    assert payload["safety"]["dry_run"] is True
    assert payload["safety"]["paper_trading"] is True
    assert payload["safety"]["paper_filter_enabled"] is False
    assert payload["paper_monitor"]["paper_pnl_is_real_money"] is False


def test_server_v104_endpoints_all_respond_no_live(v104_server):
    for ep in ("overview", "safety", "data-readiness", "provider-readiness",
               "provider-verification", "candidates", "net-edge",
               "paper-monitor", "signal-monitor"):
        status, body = _get(v104_server, f"/api/researchops/v104/{ep}")
        assert status == 200, ep
        payload = json.loads(body)
        text = json.dumps(payload)
        assert "NO LIVE" in text or payload.get("final_recommendation") == "NO LIVE", ep


def test_server_heavy_endpoints_are_cache_peek_only(v104_server):
    import app.health_server as hs

    for key, endpoint in (
        ("data_readiness", "data-readiness"),
        ("candidates", "candidates"),
        ("net_edge", "net-edge"),
    ):
        hs._V104_CACHE.pop(key, None)
        status, body = _get(v104_server, f"/api/researchops/v104/{endpoint}")
        assert status == 200
        payload = json.loads(body)
        assert payload["data_status"] == "STALE_OR_PENDING"
        assert payload["needs_manual_refresh"] is True
        assert payload["refresh_mode"] == "CLI_ONLY"
        assert payload["http_computation_disabled"] is True
        assert payload["recommended_cli"].startswith("python -m app.research_lab ")
        assert payload["final_recommendation"] == "NO LIVE"


def test_server_unknown_v104_endpoint_returns_404(v104_server):
    """V10.4.1 (Codex P2): unknown v104 endpoints are 404, payload sanitized."""
    try:
        _get(v104_server, "/api/researchops/v104/enable-live")
        raise AssertionError("expected HTTP 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
        payload = json.loads(exc.read().decode("utf-8"))
        assert payload["error"] == "unknown_researchops_v104_endpoint"
        assert payload["final_recommendation"] == "NO LIVE"


def test_server_errors_and_logs_are_sanitized(
    v104_server, monkeypatch, caplog,
):
    """Public responses and internal logs must never echo exception text."""
    import app.health_server as hs

    secret_error = (
        r"C:\secret\repo\.env /home/ubuntu/bitget-ai-trading-bot/.env "
        "API_KEY=SUPERSECRET SECRET=abc TOKEN=xyz PASSPHRASE=123"
    )

    def explode():
        raise RuntimeError(secret_error)

    monkeypatch.setattr(hs, "_v104_provider_readiness", explode)
    caplog.set_level(logging.WARNING, logger="app.health_server.v104")
    status, body = _get(v104_server, "/api/researchops/v104/provider-readiness")
    assert status == 200
    payload = json.loads(body)
    assert payload == {
        "error": "research_endpoint_error",
        "final_recommendation": "NO LIVE",
    }
    combined = body + "\n" + caplog.text
    for leak in (
        r"C:\secret\repo\.env",
        "/home/ubuntu/bitget-ai-trading-bot/.env",
        "SUPERSECRET",
        "SECRET=abc",
        "TOKEN=xyz",
        "PASSPHRASE=123",
    ):
        assert leak not in combined
    assert "research_endpoint_error" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text

    sanitized = hs._v104_sanitize(
        {"error": secret_error, "details": secret_error},
        "synthetic-payload",
    )
    assert sanitized == {
        "error": "component_unavailable",
        "final_recommendation": "NO LIVE",
    }
    assert "SUPERSECRET" not in caplog.text


def test_server_dashboard_state_polling_never_computes_heavy(v104_server):
    """V10.4.1 (Codex P2): with cold caches, dashboard-state must answer with
    STALE_OR_PENDING placeholders instead of running heavy builders."""
    import app.health_server as hs

    hs._V104_CACHE.pop("data_readiness", None)
    hs._V104_CACHE.pop("candidates", None)
    hs._V104_CACHE.pop("net_edge", None)
    started = time.perf_counter()
    status, body = _get(v104_server, "/api/researchops/v104/dashboard-state")
    elapsed = time.perf_counter() - started
    assert status == 200
    payload = json.loads(body)
    assert payload["candidates"].get("data_status") == "STALE_OR_PENDING"
    assert payload["net_edge"].get("data_status") == "STALE_OR_PENDING"
    assert payload["data_readiness"].get("data_status") == "STALE_OR_PENDING"
    assert elapsed < 5.0  # light compose, no heavy lab work


def test_heavy_http_request_cannot_block_health(
    v104_server, monkeypatch,
):
    """Regression: old candidate endpoint ran _lab_payload and blocked health."""
    import app.health_server as hs

    builder_called = threading.Event()

    def forbidden_slow_builder(*args, **kwargs):
        builder_called.set()
        time.sleep(2.5)
        return {"final_recommendation": "NO LIVE"}

    monkeypatch.setattr(hs, "_lab_payload", forbidden_slow_builder)
    hs._V104_CACHE.pop("candidates", None)
    candidate_result = {}

    def fetch_candidate():
        started = time.perf_counter()
        candidate_result["status"], candidate_result["body"] = _get(
            v104_server, "/api/researchops/v104/candidates",
        )
        candidate_result["elapsed"] = time.perf_counter() - started

    request_thread = threading.Thread(target=fetch_candidate)
    request_thread.start()
    health_started = time.perf_counter()
    status, _body = _get(v104_server, "/health")
    health_elapsed = time.perf_counter() - health_started
    request_thread.join(3)

    assert status == 200
    assert health_elapsed < 1.0
    assert candidate_result["status"] == 200
    assert candidate_result["elapsed"] < 1.0
    assert builder_called.is_set() is False
    payload = json.loads(candidate_result["body"])
    assert payload["data_status"] == "STALE_OR_PENDING"
    assert payload["needs_manual_refresh"] is True


def test_server_health_stays_fast_alongside_v104(v104_server):
    started = time.perf_counter()
    status, _body = _get(v104_server, "/health")
    assert status == 200
    assert (time.perf_counter() - started) < 2.0


def test_server_post_to_v104_routes_is_rejected(v104_server):
    req = urllib.request.Request(
        v104_server + "/api/researchops/v104/dashboard-state",
        data=b"{}", method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status >= 400  # no POST handler exists


def test_health_server_source_has_no_mutable_v104_routes():
    import pathlib

    src = pathlib.Path("app/health_server.py").read_text(encoding="utf-8")
    assert "do_POST" not in src
    assert "do_PUT" not in src
    assert "do_DELETE" not in src


def test_v104_source_never_logs_raw_exception_messages():
    import pathlib

    src = pathlib.Path("app/health_server.py").read_text(encoding="utf-8")
    v104 = src.split("# ResearchOps V10.4", 1)[1]
    assert "repr(exc)" not in v104
    assert "str(exc)" not in v104
    assert "%r" not in v104
    assert "logger.exception" not in v104
    assert "_v104_sanitize_error_for_log(exc)" in v104


# ---------------------------------------------------------------------------
# G. V10.4.1 (Codex P2) — real token-auth coverage
# ---------------------------------------------------------------------------

class _TokenConfig(_FakeConfig):
    dashboard_auth_token = "test-secret-token-v1041"


@pytest.fixture(scope="module")
def v104_auth_server():
    base, closer, _state = _start_test_server(_TokenConfig())
    try:
        yield base
    finally:
        closer()


def _get_code(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_auth_trader_terminal_requires_token(v104_auth_server):
    code, _ = _get_code(v104_auth_server, "/trader-terminal")
    assert code == 401
    code, body = _get_code(v104_auth_server, "/trader-terminal?token=test-secret-token-v1041")
    assert code == 200
    assert "NO LIVE" in body


def test_auth_v104_api_requires_token(v104_auth_server):
    code, _ = _get_code(v104_auth_server, "/api/researchops/v104/safety")
    assert code == 401
    code, body = _get_code(v104_auth_server, "/api/researchops/v104/safety?token=test-secret-token-v1041")
    assert code == 200
    assert json.loads(body)["final_recommendation"] == "NO LIVE"


def test_auth_wrong_token_rejected(v104_auth_server):
    code, _ = _get_code(v104_auth_server, "/api/researchops/v104/safety?token=wrong")
    assert code == 401


def test_auth_unknown_endpoint_auth_first_then_404(v104_auth_server):
    code, _ = _get_code(v104_auth_server, "/api/researchops/v104/unknown-thing")
    assert code == 401  # auth gate first, no information leak
    code, body = _get_code(
        v104_auth_server, "/api/researchops/v104/unknown-thing?token=test-secret-token-v1041")
    assert code == 404
    assert json.loads(body)["error"] == "unknown_researchops_v104_endpoint"


def test_auth_health_stays_public(v104_auth_server):
    code, _ = _get_code(v104_auth_server, "/health")
    assert code == 200


# ---------------------------------------------------------------------------
# H. V10.4.3 — dashboard truth fixes + runtime/learning audits
# ---------------------------------------------------------------------------

from app.labs.runtime_audit_v10_4_3 import (  # noqa: E402
    DB_AUDIT_TABLES,
    VERDICT_ATTENTION,
    VERDICT_OK,
    VERDICT_UNSAFE,
    VERDICT_WARN,
    build_learning_edge_diagnostic,
    build_runtime_efficiency,
    build_runtime_health_audit,
    count_db_tables,
    detect_false_hope,
)
from app.labs.trader_dashboard_v104 import derive_worker_lock_view  # noqa: E402


def test_worker_lock_view_heartbeat_means_no_duplicate():
    view = derive_worker_lock_view({
        "enabled": True, "acquired": True, "lock_status": "heartbeat",
        "warning_if_duplicate_worker": "",
    })
    assert view["worker_lock"] == "heartbeat"
    assert view["worker_acquired"] is True
    assert view["duplicate_worker"] == "NO"


def test_worker_lock_view_blocked_duplicate_means_yes():
    view = derive_worker_lock_view({
        "enabled": True, "acquired": False, "lock_status": "blocked_duplicate",
        "warning_if_duplicate_worker": "another worker holds the lock",
    })
    assert view["worker_lock"] == "blocked_duplicate"
    assert view["worker_acquired"] is False
    assert view["duplicate_worker"] == "YES"


def test_worker_lock_view_missing_is_unknown_not_invented():
    for raw in (None, {}, "", "unknown"):
        view = derive_worker_lock_view(raw)
        assert view["worker_lock"] == "unknown"
        assert view["worker_acquired"] == "unknown"
        assert view["duplicate_worker"] == "UNKNOWN"


def test_safety_view_exposes_uppercase_flag_aliases():
    view = derive_safety_view({
        "live_trading": False, "dry_run": True, "paper_trading": True,
        "paper_filter_enabled": False,
        "worker_lock": {"acquired": True, "lock_status": "heartbeat",
                        "warning_if_duplicate_worker": ""},
    })
    assert view["LIVE_TRADING"] is False
    assert view["DRY_RUN"] is True
    assert view["PAPER_TRADING"] is True
    assert view["security"] == "SAFE_PAPER_ONLY"
    assert view["worker_acquired"] is True
    assert view["duplicate_worker"] == "NO"


def test_v104_safety_does_not_recompute_worker_lock(monkeypatch):
    """The dashboard must NOT build a fresh WorkerLockManager (truth fix)."""
    import app.health_server as hs

    def _boom(*_a, **_k):
        raise AssertionError("V10.4 safety must not call _worker_lock_status_payload")

    monkeypatch.setattr(hs, "_worker_lock_status_payload", _boom)
    state = hs.HealthState(mode="paper")
    state.extra["worker_lock"] = {"acquired": True, "lock_status": "heartbeat",
                                  "warning_if_duplicate_worker": ""}
    view = hs._v104_safety(_FakeConfig(), None, state)
    assert view["worker_lock"] == "heartbeat"
    assert view["duplicate_worker"] == "NO"
    # And without worker_lock in the payload it stays unknown (no recompute).
    state2 = hs.HealthState(mode="paper")
    view2 = hs._v104_safety(_FakeConfig(), None, state2)
    assert view2["worker_lock"] == "unknown"
    assert view2["duplicate_worker"] == "UNKNOWN"


def test_server_dashboard_state_matches_health_worker_lock(v104_server):
    """dashboard-state must reflect the same worker-lock truth as /health."""
    status, body = _get(v104_server, "/health")
    health = json.loads(body)
    status, body = _get(v104_server, "/api/researchops/v104/dashboard-state")
    safety = json.loads(body)["safety"]
    health_lock = health.get("worker_lock")
    if isinstance(health_lock, dict):
        assert safety["worker_lock"] == health_lock.get("lock_status")
    else:
        assert safety["worker_lock"] == "unknown"
        assert safety["duplicate_worker"] == "UNKNOWN"
    assert safety["LIVE_TRADING"] is False
    assert safety["DRY_RUN"] is True
    assert safety["PAPER_TRADING"] is True
    assert safety["paper_filter_enabled"] is False
    assert safety["can_send_real_orders"] is False


def test_server_dashboard_state_has_edge_focus(v104_server):
    status, body = _get(v104_server, "/api/researchops/v104/dashboard-state")
    payload = json.loads(body)
    focus = payload["edge_focus"]
    assert focus["final_recommendation"] == "NO LIVE"
    assert isinstance(focus["what_is_blocking_edge"], list)
    assert "next_best_research_action" in focus


def test_runtime_health_audit_unsafe_on_live_flags():
    class UnsafeConfig:
        live_trading = True
        dry_run = False
        paper_trading = False
        enable_paper_policy_filter = False

    report = build_runtime_health_audit(
        config=UnsafeConfig(), db_counts={}, health={}, health_source="unavailable")
    assert report["verdict"] == VERDICT_UNSAFE
    assert report["final_recommendation"] == "NO LIVE"


def test_runtime_health_audit_attention_on_real_blocked_duplicate():
    class SafeConfig:
        live_trading = False
        dry_run = True
        paper_trading = True
        enable_paper_policy_filter = False

    report = build_runtime_health_audit(
        config=SafeConfig(), db_counts={"trades": 1},
        health={"mode": "paper", "last_scan": "x",
                "worker_lock": {"lock_status": "blocked_duplicate",
                                "warning_if_duplicate_worker": "dup"}},
        health_source="ok")
    assert report["verdict"] == VERDICT_ATTENTION
    assert "worker_lock_blocked_duplicate" in report["attention"]


def test_runtime_health_audit_warns_without_runtime_context():
    class SafeConfig:
        live_trading = False
        dry_run = True
        paper_trading = True
        enable_paper_policy_filter = False

    report = build_runtime_health_audit(
        config=SafeConfig(), db_counts={t: "db_unavailable" for t in DB_AUDIT_TABLES},
        health={}, health_source="unavailable")
    assert report["verdict"] == VERDICT_WARN
    assert report["paper_ready"] is False
    assert report["live_ready"] is False


def test_runtime_health_audit_ok_when_everything_healthy():
    class SafeConfig:
        live_trading = False
        dry_run = True
        paper_trading = True
        enable_paper_policy_filter = False

    report = build_runtime_health_audit(
        config=SafeConfig(), db_counts={"trades": 5},
        health={"mode": "paper", "last_scan": "2026-06-10T22:00:00Z",
                "circuit_breaker": False,
                "worker_lock": {"lock_status": "heartbeat", "acquired": True,
                                "warning_if_duplicate_worker": ""}},
        health_source="ok", log_audit="0_errors")
    assert report["verdict"] == VERDICT_OK


def test_count_db_tables_degrades_gracefully():
    from contextlib import contextmanager

    assert count_db_tables(None) == {t: "db_unavailable" for t in DB_AUDIT_TABLES}

    class FakeRow(dict):
        def keys(self):
            return ["n"]

        def __getitem__(self, k):
            return 7

    class FakeCursor:
        def fetchone(self):
            return FakeRow()

    class FakeConn:
        def execute(self, sql):
            return FakeCursor()

    class FakeDb:
        @contextmanager
        def _connect(self):
            yield FakeConn()

    counts = count_db_tables(FakeDb())
    assert all(v == 7 for v in counts.values())

    class MissingConn:
        def execute(self, sql):
            raise RuntimeError("no such table: x")

    class MissingDb:
        @contextmanager
        def _connect(self):
            yield MissingConn()

    assert all(v == "missing" for v in count_db_tables(MissingDb()).values())


def test_false_hope_detector_names_the_traps():
    rows = [
        {"group_value": "RANGE", "samples": 880, "gross_PF": 5.15,
         "net_EV": -0.176, "net_PF": 0.0, "time_ratio": 0.999},
        {"group_value": "ADA_SHORT", "samples": 72, "gross_PF": 999.0,
         "net_EV": -0.1636, "net_PF": 0.0, "time_ratio": 0.194},
        {"group_value": "TINY", "samples": 20, "gross_PF": 1.8,
         "net_EV": 0.01, "net_PF": 1.2, "time_ratio": 0.1},
    ]
    warnings = detect_false_hope(rows)
    text = " ".join(warnings)
    assert "RANGE" in text and "NOT edge" in text
    assert "no-SL-in-sample artifact" in text
    assert "TIME-death" in text
    assert "noise" in text


def test_learning_edge_diagnostic_honest_when_no_candidates():
    report = build_learning_edge_diagnostic(
        db_counts={"signal_observations": 100, "signal_labels": 0,
                   "signal_path_metrics": 50, "latency_metrics": 0},
        ranking={"status": "NO_VALID_CANDIDATES", "top_candidates": [],
                 "watch_list": [], "reject_list": []},
        net_edge={"rejects": []},
    )
    assert report["edge_status"] == "NO_EDGE_DEMONSTRATED"
    assert report["top_candidates_count"] == 0
    assert report["paper_ready"] is False
    assert report["live_ready"] is False
    assert report["final_recommendation"] == "NO LIVE"
    assert any("gross_PF" in w for w in report["what_not_to_do"])


def test_runtime_efficiency_never_auto_tunes():
    class Cfg:
        scan_interval_seconds = 30
        worker_lightweight_mode = True

    report = build_runtime_efficiency(config=Cfg(), db_counts={}, memory_mb=None)
    assert report["auto_tuning_applied"] is False
    assert report["memory_mb"] == "needs_vps_snapshot"
    assert report["final_recommendation"] == "NO LIVE"


def test_v1043_module_is_readonly_no_secrets():
    import pathlib

    src = pathlib.Path("app/labs/runtime_audit_v10_4_3.py").read_text(encoding="utf-8")
    assert "os.getenv" not in src and "os.environ" not in src
    assert "INSERT" not in src and "UPDATE " not in src and "DELETE FROM" not in src
    assert "import requests" not in src and "urllib" not in src


# ---------------------------------------------------------------------------
# I. V10.4.3.1 (Codex hotfix) — conservative runtime/edge audits
# ---------------------------------------------------------------------------

class _SafePaperConfig:
    live_trading = False
    dry_run = True
    paper_trading = True
    enable_paper_policy_filter = False


_HEALTHY_LOCK = {"enabled": True, "acquired": True, "lock_status": "heartbeat",
                 "warning_if_duplicate_worker": ""}
_HEALTHY_HEALTH = {"mode": "paper", "last_scan": "2026-06-10T22:00:00Z",
                   "circuit_breaker": False, "worker_lock": dict(_HEALTHY_LOCK)}


def test_runtime_audit_paper_filter_enabled_is_unsafe_stop():
    """Codex P1-1: paper filter ON can never end in OK_RESEARCH_RUNTIME."""
    class FilterOnConfig(_SafePaperConfig):
        enable_paper_policy_filter = True

    report = build_runtime_health_audit(
        config=FilterOnConfig(), db_counts={"trades": 1},
        health=dict(_HEALTHY_HEALTH), health_source="ok", log_audit="0_errors")
    assert report["verdict"] == VERDICT_UNSAFE
    assert "paper_filter_enabled_unexpected" in report["unsafe_blockers"]
    assert "paper_filter_must_remain_disabled" in report["unsafe_blockers"]
    assert report["final_recommendation"] == "NO LIVE"


def test_runtime_audit_lock_enabled_not_acquired_blocks_ok():
    """Codex P1-2: enabled lock without acquisition can never be OK."""
    health = dict(_HEALTHY_HEALTH)
    health["worker_lock"] = {"enabled": True, "acquired": False,
                             "lock_status": "expired",
                             "warning_if_duplicate_worker": ""}
    report = build_runtime_health_audit(
        config=_SafePaperConfig(), db_counts={"trades": 1},
        health=health, health_source="ok", log_audit="0_errors")
    assert report["verdict"] != VERDICT_OK
    assert report["verdict"] == VERDICT_ATTENTION
    assert "worker_lock_not_acquired" in report["attention"]


def test_runtime_audit_blocked_duplicate_minimum_needs_attention():
    """Codex P1-2: blocked_duplicate => at least NEEDS_ATTENTION."""
    health = dict(_HEALTHY_HEALTH)
    health["worker_lock"] = {"enabled": True, "acquired": False,
                             "lock_status": "blocked_duplicate",
                             "warning_if_duplicate_worker": "dup"}
    report = build_runtime_health_audit(
        config=_SafePaperConfig(), db_counts={"trades": 1},
        health=health, health_source="ok", log_audit="0_errors")
    assert report["verdict"] in (VERDICT_ATTENTION, VERDICT_UNSAFE)
    assert "duplicate_worker_detected" in report["attention"]


def test_runtime_audit_unknown_lock_blocks_ok():
    """Codex P1-2 (tightened in V10.4.3.2): unknown/missing acquired state
    escalates to NEEDS_ATTENTION — heartbeat/status strings prove nothing."""
    health = dict(_HEALTHY_HEALTH)
    health["worker_lock"] = {"lock_status": "unknown"}
    report = build_runtime_health_audit(
        config=_SafePaperConfig(), db_counts={"trades": 1},
        health=health, health_source="ok", log_audit="0_errors")
    assert report["verdict"] != VERDICT_OK
    assert "worker_lock_acquired_missing" in report["attention"]


def _top_row(**over):
    row = {"group_value": "policy_TEST_SHORT", "samples": 300, "net_EV": 0.05,
           "net_PF": 1.4, "gross_PF": 1.9, "time_ratio": 0.40,
           "decision": "PAPER_CANDIDATE", "reason": "ok"}
    row.update(over)
    return row


def _diag_with_top(top_rows):
    return build_learning_edge_diagnostic(
        db_counts={"signal_observations": 100, "signal_path_metrics": 50},
        ranking={"status": "OK", "top_candidates": top_rows,
                 "watch_list": [], "reject_list": []},
        net_edge={"rejects": []},
        data_readiness={"current_clean_days": 63.46,
                        "current_history_status": "TOO_SHORT_FOR_FINAL_VALIDATION",
                        "current_missing_oi_ratio": 0.2467,
                        "missing_oi_status": "MISSING_OI_CLUSTERED",
                        "backtester_readiness": "NEED_LONG_HISTORY",
                        "oi_bucket_policy": "BLOCK_OI_BUCKETS"},
    )


def test_top_candidate_negative_net_ev_is_not_edge():
    """Codex P1-3: a top candidate with net_EV=-0.20 is NOT pending edge."""
    report = _diag_with_top([_top_row(net_EV=-0.20)])
    assert report["edge_status"] == "NO_EDGE_DEMONSTRATED"
    assert report["validated_top_candidates_count"] == 0
    warnings = " ".join(report["false_hope_warnings"])
    assert "top_candidate_failed_revalidation" in warnings
    assert "negative_net_ev" in warnings


def test_top_candidate_small_sample_is_not_edge():
    report = _diag_with_top([_top_row(samples=40)])
    assert report["edge_status"] == "NO_EDGE_DEMONSTRATED"
    assert "insufficient_samples" in " ".join(report["false_hope_warnings"])


def test_top_candidate_full_time_death_is_not_edge():
    report = _diag_with_top([_top_row(time_ratio=1.0)])
    assert report["edge_status"] == "NO_EDGE_DEMONSTRATED"
    assert "high_time_death" in " ".join(report["false_hope_warnings"])


def test_top_candidate_reject_decision_is_not_edge():
    report = _diag_with_top([_top_row(decision="REJECT")])
    assert report["edge_status"] == "NO_EDGE_DEMONSTRATED"
    assert "rejected_decision" in " ".join(report["false_hope_warnings"])


def test_top_candidate_disqualifying_reason_is_not_edge():
    report = _diag_with_top([_top_row(reason="sample_too_small")])
    assert report["edge_status"] == "NO_EDGE_DEMONSTRATED"
    assert "reject_reason:sample_too_small" in " ".join(report["false_hope_warnings"])


def test_top_candidate_missing_time_data_needs_review():
    row = _top_row()
    row.pop("time_ratio")
    report = _diag_with_top([row])
    assert report["edge_status"] == "NO_EDGE_DEMONSTRATED"
    assert "needs_time_death_review" in " ".join(report["false_hope_warnings"])


def test_top_candidate_passing_all_gates_is_pending_validation_only():
    report = _diag_with_top([_top_row()])
    assert report["edge_status"] == "EDGE_CANDIDATE_PRESENT_PENDING_VALIDATION"
    assert report["validated_top_candidates_count"] == 1
    assert report["paper_ready"] is False
    assert report["live_ready"] is False
    assert report["final_recommendation"] == "NO LIVE"


def test_blockers_derived_not_hardcoded():
    """Codex P1-4: no invented history figures. With a snapshot the numbers
    come from it; without one the diagnostic says UNKNOWN/unavailable."""
    with_snapshot = _diag_with_top([])
    blockers = " ".join(with_snapshot["top_blockers"])
    assert "clean_days=63.46" in blockers  # derived from the snapshot input
    assert with_snapshot["data_readiness_derived"]["snapshot_available"] is True

    without = build_learning_edge_diagnostic(
        db_counts={}, ranking={"top_candidates": []}, net_edge={},
        data_readiness=None)
    blockers2 = " ".join(without["top_blockers"])
    assert "data_readiness_snapshot_unavailable" in blockers2
    assert "history_depth_unknown" in blockers2
    assert "63" not in blockers2  # no invented figure
    assert without["data_readiness_derived"]["clean_days"] == "UNKNOWN"


def test_http_fixture_uses_ephemeral_port_and_closes(v104_server):
    """Codex P2: the fixture binds port 0 (ephemeral) and exposes a live
    server; closing is exercised by the module teardown."""
    port = int(v104_server.rsplit(":", 1)[1])
    assert port != 0
    assert port not in (18977, 18978)  # no fixed test ports anymore
    status, _ = _get(v104_server, "/health")
    assert status == 200


# ---------------------------------------------------------------------------
# J. V10.4.3.2 (Codex hotfix) — invalid metrics + worker lock strictness
# ---------------------------------------------------------------------------

from app.labs.runtime_audit_v10_4_3 import (  # noqa: E402
    _to_finite_float,
    revalidate_top_candidates,
)

_NAN = float("nan")
_INF = float("inf")


def test_to_finite_float_rejects_garbage_never_raises():
    for bad in (None, _NAN, _INF, -_INF, "", "abc", "nan", "inf", True, False,
                [], {}, object()):
        assert _to_finite_float(bad) is None, repr(bad)
    assert _to_finite_float(1) == 1.0
    assert _to_finite_float(-0.5) == -0.5
    assert _to_finite_float("150") == 150.0
    assert _to_finite_float("  0.05 ") == 0.05


@pytest.mark.parametrize("field,value,expected_reason", [
    ("net_EV", None, "invalid_metric:net_EV"),
    ("net_EV", _NAN, "invalid_metric:net_EV"),
    ("net_EV", _INF, "invalid_metric:net_EV"),
    ("net_EV", "garbage", "invalid_metric:net_EV"),
    ("net_PF", None, "invalid_metric:net_PF"),
    ("net_PF", _NAN, "invalid_metric:net_PF"),
    ("net_PF", _INF, "invalid_metric:net_PF"),
    ("samples", None, "invalid_metric:samples"),
    ("samples", _NAN, "invalid_metric:samples"),
    ("samples", _INF, "invalid_metric:samples"),
    ("samples", "abc", "invalid_metric:samples"),
    ("time_ratio", None, "needs_time_death_review"),
    ("time_ratio", _NAN, "invalid_metric:TIME"),
    ("time_ratio", _INF, "invalid_metric:TIME"),
])
def test_invalid_metric_never_validates_candidate(field, value, expected_reason):
    """Codex P1-1: missing/NaN/inf/non-numeric metrics fail revalidation
    and never raise (the samples=NaN case used to raise ValueError)."""
    row = _top_row(**{field: value})
    validated, failures = revalidate_top_candidates([row])
    assert validated == []
    assert failures, "a failure warning must be emitted"
    assert "top_candidate_failed_revalidation" in failures[0]
    assert expected_reason in failures[0]
    report = _diag_with_top([row])
    assert report["edge_status"] == "NO_EDGE_DEMONSTRATED"
    assert report["validated_top_candidates_count"] == 0


def test_samples_149_9_is_insufficient():
    """Documented rule: samples must be >=150 as a finite float."""
    validated, failures = revalidate_top_candidates([_top_row(samples=149.9)])
    assert validated == []
    assert "insufficient_samples" in failures[0]


def test_fully_valid_candidate_is_pending_only_never_ready():
    validated, failures = revalidate_top_candidates([_top_row()])
    assert len(validated) == 1 and not failures
    report = _diag_with_top([_top_row()])
    assert report["edge_status"] == "EDGE_CANDIDATE_PRESENT_PENDING_VALIDATION"
    assert report["paper_ready"] is False
    assert report["live_ready"] is False
    assert report["final_recommendation"] == "NO LIVE"


def _lock_audit(lock, config=None):
    health = {"mode": "paper", "last_scan": "x", "circuit_breaker": False}
    if lock is not None:
        health["worker_lock"] = lock
    return build_runtime_health_audit(
        config=config or _SafePaperConfig(), db_counts={"trades": 1},
        health=health, health_source="ok", log_audit="0_errors")


def test_lock_heartbeat_without_acquired_is_not_ok():
    """Codex P1-2: lock_status=heartbeat alone proves nothing."""
    report = _lock_audit({"lock_status": "heartbeat"})
    assert report["verdict"] != VERDICT_OK
    assert report["verdict"] == VERDICT_ATTENTION
    assert "worker_lock_acquired_missing" in report["attention"]
    assert "worker_lock_not_acquired" in report["attention"]


def test_lock_enabled_heartbeat_without_acquired_is_not_ok():
    report = _lock_audit({"enabled": True, "lock_status": "heartbeat"})
    assert report["verdict"] != VERDICT_OK
    assert "worker_lock_acquired_missing" in report["attention"]


def test_lock_empty_dict_is_not_ok():
    report = _lock_audit({})
    assert report["verdict"] != VERDICT_OK
    assert "worker_lock_unknown" in report["warnings"]


def test_lock_absent_from_payload_is_not_ok():
    report = _lock_audit(None)
    assert report["verdict"] != VERDICT_OK
    assert "worker_lock_unknown" in report["warnings"]


def test_lock_fully_healthy_allows_ok():
    report = _lock_audit({"enabled": True, "acquired": True,
                          "lock_status": "heartbeat",
                          "warning_if_duplicate_worker": ""})
    assert report["verdict"] == VERDICT_OK


def test_lock_disabled_but_required_by_config_is_flagged():
    class RequireLockConfig(_SafePaperConfig):
        require_single_worker_lock = True

    report = _lock_audit({"enabled": False, "lock_status": "disabled"},
                         config=RequireLockConfig())
    assert report["verdict"] != VERDICT_OK
    assert "single_worker_lock_disabled_but_required" in report["attention"]
    # Deliberately disabled lock must NOT claim a duplicate worker.
    assert "duplicate_worker_detected" not in report["attention"]
