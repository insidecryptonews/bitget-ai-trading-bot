"""ResearchOps V10.4 — provider verification + acquisition plan + intake +
edge hunter contract + near-real-time read-only trader terminal tests.

All synthetic. No DB, no network, no real data, no secrets, no API keys.
"""

from __future__ import annotations

import json
import logging
import re
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
    assert targets, "polling/warm endpoints must be defined"
    assert all(t.startswith("/api/researchops/v104/") for t in targets)
    # a single fetch call lives inside the getJSON helper, which hard-rejects
    # any path outside the read-only v104 namespace
    assert html.count("fetch(") == 1
    assert "blocked: non-readonly endpoint" in html


def test_dashboard_contract_declares_all_ten_endpoints():
    c = dashboard_contract()
    expected = {f"/api/researchops/v104/{e}" for e in (
        "overview", "safety", "data-readiness", "provider-readiness",
        "provider-verification", "candidates", "net-edge", "paper-monitor",
        "signal-monitor", "dashboard-state")}
    assert expected == set(c["readonly_api_endpoints"])
    assert c["polling_never_computes_heavy_work"] is True
    assert all(w.startswith("/api/researchops/v104/") for w in c["warm_endpoints"])


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


@pytest.fixture(scope="module")
def v104_server():
    from app.health_server import HealthState, start_health_server

    state = HealthState(mode="paper")
    port = 18977
    thread = start_health_server(
        state, port, logging.getLogger("test-v104"),
        config=_FakeConfig(), db=None, training_pulse=None, telegram_notifier=None,
    )
    assert thread.server_ready.wait(5)
    time.sleep(0.2)
    yield f"http://127.0.0.1:{port}"
    server = thread.server_box.get("server")
    if server is not None:
        server.shutdown()


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


def test_server_errors_are_sanitized_no_internal_paths(v104_server):
    """V10.4.1 (Codex P2): public error payloads never leak paths/details.
    With db=None the candidates lab fails internally; the public payload must
    be generic."""
    status, body = _get(v104_server, "/api/researchops/v104/candidates")
    assert status == 200
    payload = json.loads(body)
    text = json.dumps(payload)
    for leak in ("C:\\\\", "/home/", "Traceback", ".env", "site-packages",
                 "bitget-ai-trading-bot"):
        assert leak not in text, leak
    if payload.get("error"):
        assert payload["error"] in ("component_unavailable",
                                    "data_temporarily_unavailable",
                                    "research_endpoint_error")
    assert payload["final_recommendation"] == "NO LIVE"


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


# ---------------------------------------------------------------------------
# G. V10.4.1 (Codex P2) — real token-auth coverage
# ---------------------------------------------------------------------------

class _TokenConfig(_FakeConfig):
    dashboard_auth_token = "test-secret-token-v1041"


@pytest.fixture(scope="module")
def v104_auth_server():
    from app.health_server import HealthState, start_health_server

    state = HealthState(mode="paper")
    port = 18978
    thread = start_health_server(
        state, port, logging.getLogger("test-v104-auth"),
        config=_TokenConfig(), db=None, training_pulse=None, telegram_notifier=None,
    )
    assert thread.server_ready.wait(5)
    time.sleep(0.2)
    yield f"http://127.0.0.1:{port}"
    server = thread.server_box.get("server")
    if server is not None:
        server.shutdown()


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
