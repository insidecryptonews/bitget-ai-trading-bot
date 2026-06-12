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

_VALID_RANGE = {"start": "2025-06-11T00:00:00Z", "end": "2026-06-11T00:00:00Z"}


def _manifest_v105(**over):
    man = {f: 0 for f in MANIFEST_V105_REQUIRED_FIELDS}
    man.update({
        "source_provider": "tardis_dev", "license_terms": "research",
        "requested_range": dict(_VALID_RANGE),
        "actual_covered_range": dict(_VALID_RANGE),
        "symbols": ["BTCUSDT"], "timeframes": ["1h"],
        "data_types": ["ohlcv", "open_interest", "funding", "liquidations"],
        "rows_by_type": {"ohlcv": 8760, "open_interest": 8760,
                         "funding": 1095, "liquidations": 5000},
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
    # V10.5.1: None/invalid values now fail SEMANTIC validation (stricter).
    ev2 = evaluate_manifest_v105(_manifest_v105(missing_liquidations_ratio=None))
    assert ev2.promote_allowed is False
    assert "invalid_field:missing_liquidations_ratio" in ev2.blockers
    ev3 = evaluate_manifest_v105(_manifest_v105(timezone="Europe/Madrid"))
    assert ev3.promote_allowed is False
    assert "invalid_field:timezone_must_be_utc" in ev3.blockers
    ev4 = evaluate_manifest_v105(_manifest_v105(timestamp_unit="iso8601"))
    assert "invalid_field:timestamp_unit" in ev4.blockers
    ev5 = evaluate_manifest_v105(_manifest_v105(schema_version="v10.4"))
    assert "invalid_field:schema_version" in ev5.blockers


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


# ===========================================================================
# V10.5.1 (Codex hotfix) — fail-closed manifest + non-blocking dashboard
# ===========================================================================

import json  # noqa: E402
import logging  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402

import pytest  # noqa: E402

from app.labs.data_foundation_v10_5 import (  # noqa: E402
    READY_NEED_SERIES,
    READY_OI_BLOCKED,
    ST_SEMANTIC_FAIL,
    _to_finite_float,
    _to_non_negative_int,
    _valid_ratio,
    _valid_sha256,
)
from app.labs.trader_dashboard_v104 import derive_pipeline_stages  # noqa: E402

_NAN = float("nan")
_INF = float("inf")


class _EvilFloat:
    def __float__(self):
        raise RuntimeError("boom")


# --- P1-2: fail-closed manifest -------------------------------------------

def test_v1051_defensive_parsers_are_total():
    for bad in (None, _NAN, _INF, -_INF, True, False, "", "  ", "abc",
                [1], {"a": 1}, {1, 2}, 10 ** 10000, _EvilFloat()):
        assert _to_finite_float(bad) is None, repr(bad)
    assert _to_non_negative_int(-1) is None
    assert _to_non_negative_int(3.7) is None
    assert _to_non_negative_int(5.0) == 5
    assert _valid_ratio(1.5) is None
    assert _valid_ratio(-0.1) is None
    assert _valid_ratio(0.8) == 0.8
    assert _valid_sha256("a" * 64) is True
    assert _valid_sha256("xyz") is False
    assert _valid_sha256("a" * 63) is False


@pytest.mark.parametrize("field,value", [
    ("coverage_ratio", _NAN), ("coverage_ratio", _INF), ("coverage_ratio", "abc"),
    ("missing_oi_ratio", _NAN), ("missing_funding_ratio", _NAN),
    ("missing_liquidations_ratio", _NAN), ("missing_funding_ratio", -_INF),
    ("clean_days", _EvilFloat()), ("clean_days", 10 ** 10000),
    ("gap_count", -1), ("duplicate_count", _NAN),
], ids=["cov-nan", "cov-inf", "cov-str", "oi-nan", "fund-nan", "liq-nan",
        "fund-neginf", "clean-evil", "clean-hugeint", "gap-neg", "dup-nan"])
def test_v1051_hostile_numeric_fields_block_never_raise(field, value):
    ev = evaluate_manifest_v105(_manifest_v105(**{field: value}))
    assert ev.promote_allowed is False
    assert ev.status == ST_SEMANTIC_FAIL
    assert any("invalid_field:" in b for b in ev.blockers)


def test_v1051_structural_fields_block():
    assert evaluate_manifest_v105(_manifest_v105(symbols=[])).promote_allowed is False
    assert evaluate_manifest_v105(_manifest_v105(timeframes=[])).promote_allowed is False
    assert evaluate_manifest_v105(_manifest_v105(data_types=[])).promote_allowed is False
    assert evaluate_manifest_v105(_manifest_v105(rows_by_type={})).promote_allowed is False
    assert evaluate_manifest_v105(_manifest_v105(rows_by_type={"x": -5})).promote_allowed is False
    assert evaluate_manifest_v105(_manifest_v105(checksums_sha256={"f": "bad"})).promote_allowed is False
    assert evaluate_manifest_v105(_manifest_v105(generated_at="")).promote_allowed is False
    assert evaluate_manifest_v105(_manifest_v105(import_status="HACKED")).promote_allowed is False
    assert evaluate_manifest_v105(_manifest_v105(missing_oi_status="JUNK")).promote_allowed is False


def test_v1051_coverage_and_quality_thresholds_block():
    assert evaluate_manifest_v105(_manifest_v105(coverage_ratio=0.79)).promote_allowed is False
    # totals are 23615 rows: gap ratio 0.06 -> ~1417; dup ratio 0.03 -> ~709
    assert evaluate_manifest_v105(_manifest_v105(gap_count=1500)).promote_allowed is False
    assert evaluate_manifest_v105(_manifest_v105(duplicate_count=750)).promote_allowed is False


def test_v1051_promote_allowed_input_is_ignored():
    """A malicious manifest carrying promote_allowed=True cannot steer the
    evaluator: the result is recalculated from gates."""
    ev = evaluate_manifest_v105(_manifest_v105(promote_allowed=True, coverage_ratio=0.5))
    assert ev.promote_allowed is False
    ev2 = evaluate_manifest_v105({"promote_allowed": True})
    assert ev2.promote_allowed is False


def test_v1051_valid_manifest_promotes_research_only():
    ev = evaluate_manifest_v105(_manifest_v105())
    assert ev.status == ST_PROMOTE_ALLOWED
    assert ev.promote_allowed is True
    assert ev.paper_ready is False
    assert ev.live_ready is False
    assert ev.final_recommendation == "NO LIVE"


# --- P2-1: readiness never optimistic with blocked/unknown series ----------

_SNAP_180 = {"current_clean_days": 200.0,
             "current_history_status": "OK",
             "missing_oi_status": "DATA_OK",
             "oi_bucket_policy": "ALLOW_OI_BUCKETS_WITH_CARE",
             "backtester_readiness": "READY"}
_PROV_READY = {"any_provider_ready_for_authorization": True}


def test_v1051_readiness_oi_blocked_never_initial_ready():
    snap = dict(_SNAP_180, oi_bucket_policy="BLOCK_OI_BUCKETS",
                missing_oi_status="MISSING_OI_CLUSTERED")
    r = build_data_readiness_v105(
        data_readiness_snapshot=snap, provider_report=_PROV_READY,
        funding_verified=True, liquidations_verified=True)
    assert r.status == READY_OI_BLOCKED
    assert r.status != "INITIAL_VALIDATION_READY"


def test_v1051_readiness_funding_unknown_never_initial_ready():
    r = build_data_readiness_v105(
        data_readiness_snapshot=dict(_SNAP_180), provider_report=_PROV_READY,
        funding_verified=False, liquidations_verified=True)
    assert r.status == READY_NEED_SERIES


def test_v1051_readiness_liquidations_unknown_never_initial_ready():
    r = build_data_readiness_v105(
        data_readiness_snapshot=dict(_SNAP_180), provider_report=_PROV_READY,
        funding_verified=True, liquidations_verified=False)
    assert r.status == READY_NEED_SERIES


def test_v1051_readiness_all_series_green_is_initial_ready_research_only():
    manifest_eval = evaluate_manifest_v105(_manifest_v105()).as_dict()
    r = build_data_readiness_v105(
        data_readiness_snapshot=dict(_SNAP_180), provider_report=_PROV_READY,
        funding_verified=True, liquidations_verified=True,
        manifest_evaluation=manifest_eval)
    assert r.status == "INITIAL_VALIDATION_READY"
    assert r.paper_ready is False
    assert r.live_ready is False
    assert r.final_recommendation == "NO LIVE"


# --- P2-2: pipeline never says PASS without explicit fresh evidence --------

_SAFE = {"uptime": "100s"}


def test_v1051_pipeline_stale_data_guard_not_pass():
    stages = derive_pipeline_stages(
        safety=_SAFE, candidates={"data_status": "STALE_OR_PENDING"},
        net_edge={}, signal_monitor={"top_signals": ["s"], "top_blocks": []})
    guard = next(s for s in stages if s["name"] == "EDGE GUARD")
    assert guard["state"] != "PASS"
    netev = next(s for s in stages if s["name"] == "NET EV")
    assert netev["state"] == "NEEDS_DATA"


def test_v1051_pipeline_no_valid_candidates_guard_blocked():
    stages = derive_pipeline_stages(
        safety=_SAFE, candidates={"status": "NO_VALID_CANDIDATES"},
        net_edge={}, signal_monitor={"top_signals": ["s"], "top_blocks": []})
    guard = next(s for s in stages if s["name"] == "EDGE GUARD")
    assert guard["state"] == "BLOCKED"
    netev = next(s for s in stages if s["name"] == "NET EV")
    assert netev["state"] == "BLOCKED"


def test_v1051_pipeline_no_signals_guard_not_pass():
    stages = derive_pipeline_stages(
        safety=_SAFE, candidates={"status": "OK"},
        net_edge={"status": "OK"}, signal_monitor={"top_signals": [], "top_blocks": []})
    guard = next(s for s in stages if s["name"] == "EDGE GUARD")
    assert guard["state"] != "PASS"


def test_v1051_pipeline_pass_only_with_explicit_fresh_evidence():
    """V10.5.2 tightened: PASS needs validated count > 0 AND a positive fresh
    net-edge row — a generic status=OK is no longer evidence."""
    stages = derive_pipeline_stages(
        safety=_SAFE,
        candidates={"status": "OK", "validated_top_candidates_count": 1},
        net_edge={"status": "OK",
                  "top_candidates": [{"net_EV": 0.05, "net_PF": 1.4}]},
        signal_monitor={"top_signals": ["s"], "top_blocks": []})
    guard = next(s for s in stages if s["name"] == "EDGE GUARD")
    assert guard["state"] == "PASS"
    netev = next(s for s in stages if s["name"] == "NET EV")
    assert netev["state"] == "PASS"
    shadow = next(s for s in stages if s["name"] == "SHADOW / PAPER")
    assert shadow["state"] == "BLOCKED"  # always, by design


# --- P1-1: dashboard-state never touches the DB ----------------------------

class _V1051Config:
    enable_training_dashboard = True
    dashboard_auth_token = ""
    dashboard_refresh_seconds = 7
    live_trading = False
    dry_run = True
    paper_trading = True
    enable_paper_policy_filter = False
    require_single_worker_lock = True
    training_runtime_profile = "railway_lightweight"


class _SlowDb:
    def _connect(self):
        time.sleep(5)
        raise RuntimeError("slow db")


@pytest.fixture()
def v1051_server(monkeypatch):
    import app.health_server as hs
    import app.labs.runtime_audit_v10_4_3 as ra

    def _poisoned(db):
        raise AssertionError("count_db_tables called from polling path")

    monkeypatch.setattr(ra, "count_db_tables", _poisoned)
    hs._V104_CACHE.clear()
    state = hs.HealthState(mode="paper")
    thread = hs.start_health_server(
        state, 0, logging.getLogger("test-v1051"),
        config=_V1051Config(), db=_SlowDb(), training_pulse=None,
        telegram_notifier=None,
    )
    assert thread.server_ready.wait(5)
    server = thread.server_box.get("server")
    assert server is not None
    yield f"http://127.0.0.1:{server.server_address[1]}", hs
    server.shutdown()
    server.server_close()


def _get_json(base, path, timeout=10):
    started = time.perf_counter()
    with urllib.request.urlopen(base + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8")), time.perf_counter() - started


def test_v1051_dashboard_state_cold_cache_no_db(v1051_server):
    """Poisoned DB counters + slow DB: dashboard-state must answer fast with
    STALE_OR_PENDING and never call the count function."""
    base, hs = v1051_server
    payload, elapsed = _get_json(base, "/api/researchops/v104/dashboard-state")
    assert payload["learning"]["data_status"] == "STALE_OR_PENDING"
    assert elapsed < 1.0


def test_v1051_health_fast_while_polling(v1051_server):
    base, hs = v1051_server
    times = []

    def poll():
        for _ in range(4):
            _get_json(base, "/api/researchops/v104/dashboard-state")

    worker = threading.Thread(target=poll)
    worker.start()
    for _ in range(4):
        _payload, elapsed = _get_json(base, "/health")
        times.append(elapsed)
    worker.join()
    assert max(times) < 1.0


def test_v1051_learning_snapshot_served_from_cache(v1051_server):
    base, hs = v1051_server
    hs._V104_CACHE["learning_counts"] = (time.time(), {
        "observations": 148546, "labels": 0, "path_metrics": 675,
        "virtual_research_trades": 0,
        "learning_status": "LEARNING_INFRA_ACTIVE",
        "edge_status": "NO_EDGE_DEMONSTRATED",
        "final_recommendation": "NO LIVE"})
    payload, _ = _get_json(base, "/api/researchops/v104/dashboard-state")
    assert payload["learning"]["observations"] == 148546
    assert payload["learning"]["learning_status"] == "LEARNING_INFRA_ACTIVE"


def test_v1051_learning_cached_error_is_sanitized_stale(v1051_server):
    base, hs = v1051_server
    hs._V104_CACHE["learning_counts"] = (time.time(), {
        "error": "RuntimeError at C:\\secret\\repo\\.env",
        "final_recommendation": "NO LIVE"})
    payload, _ = _get_json(base, "/api/researchops/v104/dashboard-state")
    assert payload["learning"]["data_status"] == "ERROR_STALE"
    text = json.dumps(payload)
    assert "secret" not in text and ".env" not in text


def test_v1051_polling_path_source_is_db_free():
    """Contractual: the polling code path contains no DB primitives."""
    import inspect

    import app.health_server as hs

    src = (inspect.getsource(hs._v104_dashboard_state)
           + inspect.getsource(hs._v105_learning_status_cache_peek)
           + inspect.getsource(hs._v104_cache_peek))
    assert "_connect" not in src
    assert "count_db_tables" not in src
    assert "COUNT(" not in src


def test_v1051_provider_panel_distinguishes_v105_from_legacy():
    """P2-3: the dashboard shows real V10.5 verification, legacy registry is
    labelled as legacy."""
    html = _html()
    assert "V10.5 Provider Verification" in html
    assert "Legacy local registry (V10.3)" in html
    assert "NOT a completed verification" in html
    rep = run_provider_verification_v105()
    assert all(p["status"] != ST_READY_FOR_HUMAN_AUTH for p in rep.providers)
    assert rep.any_paid_download_authorized is False


# ===========================================================================
# V10.5.2 (Codex hotfix) — fully DB-free polling + strict manifest semantics
# ===========================================================================

from app.labs.data_foundation_v10_5 import (  # noqa: E402
    READY_NEED_VALID_MANIFEST,
    _parse_datetime,
    _valid_range,
)


# --- P1-4: strict manifest semantics ---------------------------------------

def test_v1052_real_date_parsing_rejects_garbage():
    assert _parse_datetime("2026-99-99garbage") is None
    assert _parse_datetime("") is None
    assert _parse_datetime("x") is None
    assert _parse_datetime("2026-02-30") is None  # impossible date
    assert _parse_datetime("2026-06-11T00:00:00Z") is not None
    assert _parse_datetime(1760000000) is not None       # unix seconds
    assert _parse_datetime(1760000000000) is not None    # unix milliseconds


def test_v1052_generated_at_garbage_blocks():
    for bad in ("2026-99-99garbage", "", "x"):
        ev = evaluate_manifest_v105(_manifest_v105(generated_at=bad))
        assert ev.promote_allowed is False, bad
        assert "invalid_field:generated_at" in ev.blockers


def test_v1052_symbols_content_validated():
    ev = evaluate_manifest_v105(_manifest_v105(symbols=[""]))
    assert ev.promote_allowed is False
    assert "invalid_field:symbols_pattern" in ev.blockers
    ev2 = evaluate_manifest_v105(_manifest_v105(symbols=["banana"]))
    assert ev2.promote_allowed is False
    assert "invalid_field:symbols_pattern" in ev2.blockers
    # A clean perp symbol passes the symbol gate (full manifest still valid).
    assert evaluate_manifest_v105(_manifest_v105(symbols=["BTCUSDT"])).promote_allowed is True


def test_v1052_timeframes_content_validated():
    for bad in ([""], ["banana"], ["1h", "banana"]):
        ev = evaluate_manifest_v105(_manifest_v105(timeframes=bad))
        assert ev.promote_allowed is False, bad
        assert "invalid_field:timeframes_not_allowed" in ev.blockers


def test_v1052_data_types_whitelist_and_minimum():
    ev = evaluate_manifest_v105(_manifest_v105(data_types=["banana"]))
    assert ev.promote_allowed is False
    assert "invalid_field:data_types_not_allowed" in ev.blockers
    for missing in ("open_interest", "funding", "liquidations"):
        types = [t for t in ("ohlcv", "open_interest", "funding", "liquidations")
                 if t != missing]
        rows = {t: 100 for t in types}
        ev2 = evaluate_manifest_v105(_manifest_v105(data_types=types, rows_by_type=rows))
        assert ev2.promote_allowed is False, missing
        assert any("data_types_missing_required" in b for b in ev2.blockers), missing


def test_v1052_ranges_must_be_structured_and_ordered():
    ev = evaluate_manifest_v105(_manifest_v105(requested_range="x"))
    assert ev.promote_allowed is False
    assert "invalid_field:requested_range" in ev.blockers
    bad_order = {"start": "2026-06-11T00:00:00Z", "end": "2025-06-11T00:00:00Z"}
    ev2 = evaluate_manifest_v105(_manifest_v105(requested_range=bad_order))
    assert ev2.promote_allowed is False
    ev3 = evaluate_manifest_v105(_manifest_v105(actual_covered_range={}))
    assert ev3.promote_allowed is False
    assert "invalid_field:actual_covered_range" in ev3.blockers
    assert _valid_range(dict(_VALID_RANGE)) is True


def test_v1052_rows_by_type_semantics():
    all_zero = {"ohlcv": 0, "open_interest": 0, "funding": 0, "liquidations": 0}
    ev = evaluate_manifest_v105(_manifest_v105(rows_by_type=all_zero))
    assert ev.promote_allowed is False
    assert any("rows_by_type_required_zero" in b for b in ev.blockers)
    missing_required = {"ohlcv": 100, "open_interest": 100, "funding": 100}
    ev2 = evaluate_manifest_v105(_manifest_v105(rows_by_type=missing_required))
    assert ev2.promote_allowed is False
    unknown_key = {"ohlcv": 100, "open_interest": 100, "funding": 100,
                   "liquidations": 100, "mystery_table": 5}
    ev3 = evaluate_manifest_v105(_manifest_v105(rows_by_type=unknown_key))
    assert ev3.promote_allowed is False
    assert any("rows_by_type_unknown_key" in b for b in ev3.blockers)


def test_v1052_checksums_empty_blocks():
    ev = evaluate_manifest_v105(_manifest_v105(checksums_sha256={}))
    assert ev.promote_allowed is False
    assert "invalid_field:checksums_sha256" in ev.blockers


def test_v1052_cross_field_clean_days_exceeding_covered_range_blocks():
    """Self-audit family: a physically impossible clean_days claim (larger
    than the covered range span) is a hostile/contradictory manifest."""
    one_month = {"start": "2026-05-11T00:00:00Z", "end": "2026-06-11T00:00:00Z"}
    ev = evaluate_manifest_v105(_manifest_v105(
        actual_covered_range=one_month, clean_days=365.0))
    assert ev.promote_allowed is False
    assert "inconsistent_field:clean_days_exceeds_covered_range" in ev.blockers
    # Coherent claim still passes (clean_days fits inside the range).
    ev2 = evaluate_manifest_v105(_manifest_v105(
        actual_covered_range=one_month, clean_days=30.0, coverage_ratio=0.97))
    assert "inconsistent_field:clean_days_exceeds_covered_range" not in ev2.blockers


def test_v1052_cross_field_oi_status_contradicting_ratio_blocks():
    """missing_oi_status=DATA_OK with missing_oi_ratio>0.10 is contradictory."""
    ev = evaluate_manifest_v105(_manifest_v105(
        missing_oi_status="DATA_OK", missing_oi_ratio=0.25))
    assert ev.promote_allowed is False
    assert ("inconsistent_field:oi_status_data_ok_with_high_missing_ratio"
            in ev.blockers)


def test_v1052_valid_manifest_still_research_only():
    ev = evaluate_manifest_v105(_manifest_v105())
    assert ev.status == ST_PROMOTE_ALLOWED
    assert ev.paper_ready is False
    assert ev.live_ready is False
    assert ev.final_recommendation == "NO LIVE"


# --- P2-1: readiness requires a valid manifest evaluation -------------------

def test_v1052_readiness_without_manifest_never_initial_ready():
    r = build_data_readiness_v105(
        data_readiness_snapshot=dict(_SNAP_180), provider_report=_PROV_READY,
        funding_verified=True, liquidations_verified=True,
        manifest_evaluation=None)
    assert r.status == READY_NEED_VALID_MANIFEST
    assert "valid_manifest_required" in r.top_blockers
    assert r.paper_ready is False and r.live_ready is False


def test_v1052_readiness_with_invalid_manifest_never_initial_ready():
    bad_eval = evaluate_manifest_v105(_manifest_v105(coverage_ratio=0.5)).as_dict()
    r = build_data_readiness_v105(
        data_readiness_snapshot=dict(_SNAP_180), provider_report=_PROV_READY,
        funding_verified=True, liquidations_verified=True,
        manifest_evaluation=bad_eval)
    assert r.status == READY_NEED_VALID_MANIFEST
    assert "valid_manifest_required" in r.top_blockers


# --- P2-2: EdgeGuard / NET EV truthfulness ----------------------------------

def test_v1052_guard_ok_with_zero_count_not_pass():
    stages = derive_pipeline_stages(
        safety=_SAFE, candidates={"status": "OK", "candidate_count": 0},
        net_edge={"status": "OK"},
        signal_monitor={"top_signals": ["s"], "top_blocks": []})
    guard = next(s for s in stages if s["name"] == "EDGE GUARD")
    assert guard["state"] != "PASS"


def test_v1052_guard_ok_without_count_not_pass():
    stages = derive_pipeline_stages(
        safety=_SAFE, candidates={"status": "OK"},
        net_edge={"status": "OK"},
        signal_monitor={"top_signals": ["s"], "top_blocks": []})
    guard = next(s for s in stages if s["name"] == "EDGE GUARD")
    assert guard["state"] != "PASS"


def test_v1052_netev_negative_or_zero_blocked():
    for net_ev in (-1.0, 0.0):
        stages = derive_pipeline_stages(
            safety=_SAFE,
            candidates={"status": "OK", "validated_top_candidates_count": 1},
            net_edge={"status": "OK",
                      "top_candidates": [{"net_EV": net_ev, "net_PF": 1.4}]},
            signal_monitor={"top_signals": ["s"], "top_blocks": []})
        netev = next(s for s in stages if s["name"] == "NET EV")
        assert netev["state"] == "BLOCKED", net_ev


def test_v1052_netev_low_pf_blocked():
    stages = derive_pipeline_stages(
        safety=_SAFE,
        candidates={"status": "OK", "validated_top_candidates_count": 1},
        net_edge={"status": "OK",
                  "top_candidates": [{"net_EV": 0.05, "net_PF": 0.8}]},
        signal_monitor={"top_signals": ["s"], "top_blocks": []})
    netev = next(s for s in stages if s["name"] == "NET EV")
    assert netev["state"] == "BLOCKED"


def test_v1052_netev_no_top_candidates_blocked():
    stages = derive_pipeline_stages(
        safety=_SAFE,
        candidates={"status": "OK", "validated_top_candidates_count": 1},
        net_edge={"status": "OK", "top_candidates": []},
        signal_monitor={"top_signals": ["s"], "top_blocks": []})
    netev = next(s for s in stages if s["name"] == "NET EV")
    assert netev["state"] == "BLOCKED"


def test_v1052_candidate_pending_requires_real_candidate():
    """The mission bar derives CANDIDATE PENDING from the server EDGE GUARD
    stage, which requires validated candidates — never a generic OK string."""
    html = _html()
    assert 'guardStage.state === "PASS"' in html
    assert 'cdStatus === "OK" ? "CANDIDATE PENDING"' not in html


# --- P1-1/P1-2/P1-3: DB-free polling, real-method poisoning -----------------

class _ExplodingDb:
    """Fake DB whose REAL method names explode if anything touches them."""

    def get_signal_label_summary_since(self, *_a, **_k):
        raise AssertionError("get_signal_label_summary_since called from polling")

    def get_open_paper_positions_summary(self, *_a, **_k):
        raise AssertionError("get_open_paper_positions_summary called from polling")

    def _connect(self, *_a, **_k):
        raise AssertionError("_connect called from polling")


@pytest.fixture()
def v1052_server(monkeypatch):
    import app.health_server as hs
    import app.labs.runtime_audit_v10_4_3 as ra

    def _poisoned(db):
        raise AssertionError("count_db_tables called from polling path")

    monkeypatch.setattr(ra, "count_db_tables", _poisoned)
    hs._V104_CACHE.clear()
    state = hs.HealthState(mode="paper")
    thread = hs.start_health_server(
        state, 0, logging.getLogger("test-v1052"),
        config=_V1051Config(), db=_ExplodingDb(), training_pulse=None,
        telegram_notifier=None,
    )
    assert thread.server_ready.wait(5)
    server = thread.server_box.get("server")
    assert server is not None
    yield f"http://127.0.0.1:{server.server_address[1]}", hs
    server.shutdown()
    server.server_close()


def test_v1052_dashboard_state_never_calls_real_db_methods(v1052_server):
    base, hs = v1052_server
    payload, elapsed = _get_json(base, "/api/researchops/v104/dashboard-state")
    assert elapsed < 1.0
    assert payload["paper_monitor"]["data_status"] == "STALE_OR_PENDING"
    assert payload["learning"]["data_status"] == "STALE_OR_PENDING"
    assert payload["final_recommendation"] == "NO LIVE"


def test_v1052_slow_real_db_methods_do_not_block(v1052_server):
    """Slow variants of the REAL DB methods must never be reached."""
    base, hs = v1052_server

    class SlowDb(_ExplodingDb):
        def get_signal_label_summary_since(self, *_a, **_k):
            time.sleep(3)
            return {}

        def get_open_paper_positions_summary(self, *_a, **_k):
            time.sleep(3)
            return []

    # Even swapping in a slow DB cannot matter: the polling path has no DB
    # call sites left. Measure both endpoints under repeated polling.
    times = []

    def poll():
        for _ in range(4):
            _get_json(base, "/api/researchops/v104/dashboard-state")

    worker = threading.Thread(target=poll)
    worker.start()
    for _ in range(4):
        _payload, elapsed = _get_json(base, "/health")
        times.append(elapsed)
    worker.join()
    assert max(times) < 1.0


def test_v1052_paper_monitor_snapshot_served_from_cache(v1052_server):
    base, hs = v1052_server
    hs._V104_CACHE["paper_monitor"] = (time.time(), {
        "open_positions_detail": [], "profit_factor": 1.1, "total_labels": 42,
        "final_recommendation": "NO LIVE"})
    payload, _ = _get_json(base, "/api/researchops/v104/dashboard-state")
    assert payload["paper_monitor"]["total_labels"] == 42
    assert payload["paper_monitor"]["paper_pnl_is_real_money"] is False


def test_v1052_learning_endpoint_no_nameerror(v1052_server):
    """P1-2: /learning answers snapshot-only without NameError and without
    touching the DB (the fake DB would explode)."""
    base, hs = v1052_server
    payload, _ = _get_json(base, "/api/researchops/v104/learning")
    assert payload.get("error") != "research_endpoint_error"
    assert payload["data_status"] == "STALE_OR_PENDING"
    assert payload["refresh_mode"] == "CLI_ONLY"
    assert payload["http_computation_disabled"] is True
    assert "learning-edge-diagnostic-v104" in payload["recommended_cli"]


def test_v1052_learning_not_called_by_automatic_js():
    html = _html()
    assert "/api/researchops/v104/learning" not in html  # not in any JS loop


def test_v1052_contract_call_graph_check_catches_db_reintroduction():
    """P1-3: the contract source-check must flag forbidden DB names if they
    appear anywhere in the polling call graph."""
    import inspect

    import app.health_server as hs
    from app.labs import trader_dashboard_v104 as td

    graph = [hs._v104_dashboard_state, hs._v104_safety,
             hs._v104_paper_monitor_cache_peek,
             hs._v105_learning_status_cache_peek, hs._v104_cache_peek,
             hs._v104_provider_readiness, hs._v105_provider_verification_light,
             hs._v104_signal_monitor, hs._v104_edge_focus, hs._v104_cached,
             td.derive_pipeline_stages, td.derive_safety_view,
             td.derive_worker_lock_view]
    src = "".join(inspect.getsource(f) for f in graph)
    forbidden = ["_v104_paper_monitor(", "get_signal_label_summary_since",
                 "get_open_paper_positions_summary", "Database(", "_connect(",
                 "count_db_tables", "SELECT COUNT", "signal_labels",
                 "paper_positions", "sqlite3"]
    hits = [f for f in forbidden if f in src]
    assert hits == [], f"DB primitives leaked into polling path: {hits}"
