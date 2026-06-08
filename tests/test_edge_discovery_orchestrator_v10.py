"""ResearchOps V10 — Edge Discovery Orchestrator tests.

All synthetic. No DB required (runner tolerates db=None). Verifies the
hard invariants: never PAPER_READY / LIVE_READY, clock_drift UNKNOWN
blocks pre-live, hard blockers demote families.
"""

from __future__ import annotations

from app.labs.edge_discovery_orchestrator_v10 import (
    FAM_FEATURE_ONLY,
    FAM_IMPLEMENT_FIRST,
    FAM_NEED_DATA,
    FAM_REJECT,
    FAM_WATCH_ONLY,
    aggregate_edge_families,
    run_edge_discovery_orchestrator,
)


def _clean_reports():
    return {
        "funding_oi_liquidation": {"decision": "IMPLEMENT_FIRST_RESEARCH", "blockers": [],
                                   "required_data_missing": ["liquidation_usd"]},
        "token_unlock_post_listing": {"decision": "WATCH_ONLY"},
        "intraday_volatility_breakdown": {"decision": "NEED_MORE_DATA"},
        "micro_tp": {"decision": "NOT_CORE"},
        "event_catalyst": {"decision": "SHADOW_RESEARCH_ONLY"},
    }


def test_never_returns_paper_or_live_ready():
    r = aggregate_edge_families(_clean_reports(), clock_drift_status="OK",
                               clean_n=100, data_quality_status="OK", ohlcv_freshness="FRESH")
    assert r.live_ready is False
    assert r.paper_ready is False
    assert r.final_recommendation == "NO LIVE"
    assert r.can_send_real_orders is False
    assert r.paper_filter_enabled is False


def test_clock_drift_unknown_blocks_pre_live():
    r = aggregate_edge_families(_clean_reports(), clock_drift_status="UNKNOWN",
                               clean_n=100, data_quality_status="OK", ohlcv_freshness="FRESH")
    assert r.pre_live_clock_gate == "BLOCKED_CLOCK_DRIFT_UNKNOWN"
    assert "clock_drift_not_ok_pre_live_blocked" in r.global_blockers
    # global blocker demotes even the best family to at most NEED_DATA
    assert r.best_family_status == FAM_NEED_DATA
    assert r.shadow_ready is False
    assert r.live_ready is False and r.paper_ready is False


def test_clock_ok_clean_allows_implement_first_but_not_promotion():
    r = aggregate_edge_families(_clean_reports(), clock_drift_status="OK",
                               clean_n=100, data_quality_status="OK", ohlcv_freshness="FRESH")
    assert r.best_family == "funding_oi_liquidation"
    assert r.best_family_status == FAM_IMPLEMENT_FIRST
    assert r.shadow_ready is True  # research shadow design only
    assert r.live_ready is False and r.paper_ready is False


def test_insufficient_clean_n_blocks():
    r = aggregate_edge_families(_clean_reports(), clock_drift_status="OK",
                               clean_n=10, data_quality_status="OK", ohlcv_freshness="FRESH")
    assert "clean_n_insufficient" in r.global_blockers
    assert r.best_family_status == FAM_NEED_DATA


def test_data_quality_bad_blocks_promotion():
    r = aggregate_edge_families(_clean_reports(), clock_drift_status="OK",
                               clean_n=100, data_quality_status="BAD", ohlcv_freshness="FRESH")
    assert "data_quality_bad" in r.global_blockers
    assert r.best_family_status == FAM_NEED_DATA


def test_stale_ohlcv_blocks():
    r = aggregate_edge_families(_clean_reports(), clock_drift_status="OK",
                               clean_n=100, data_quality_status="OK", ohlcv_freshness="STALE")
    assert "ohlcv_stale" in r.global_blockers


def test_market_probe_contamination_blocks():
    r = aggregate_edge_families(_clean_reports(), clock_drift_status="OK",
                               clean_n=100, data_quality_status="OK", ohlcv_freshness="FRESH",
                               market_probe_contaminated=True)
    assert "market_probe_contamination" in r.global_blockers
    assert r.best_family_status == FAM_NEED_DATA


def test_active_counted_as_real_blocks():
    r = aggregate_edge_families(_clean_reports(), clock_drift_status="OK",
                               clean_n=100, data_quality_status="OK", ohlcv_freshness="FRESH",
                               active_counted_as_real=True)
    assert "active_path_counted_as_real" in r.global_blockers


def test_proxy_only_blocks():
    r = aggregate_edge_families(_clean_reports(), clock_drift_status="OK",
                               clean_n=100, data_quality_status="OK", ohlcv_freshness="FRESH",
                               proxy_only=True)
    assert "proxy_only_outcomes" in r.global_blockers


def test_negative_net_ev_family_rejected():
    reps = {"funding_oi_liquidation": {"decision": "IMPLEMENT_FIRST_RESEARCH", "net_ev_pct": -0.5}}
    r = aggregate_edge_families(reps, clock_drift_status="OK", clean_n=100,
                               data_quality_status="OK", ohlcv_freshness="FRESH")
    assert r.families[0]["family_status"] == FAM_REJECT
    assert "funding_oi_liquidation" in r.rejected_families


def test_single_symbol_dominance_demotes():
    reps = {"intraday_volatility_breakdown": {"decision": "RESEARCH_POCKET", "concentration": 0.85}}
    r = aggregate_edge_families(reps, clock_drift_status="OK", clean_n=100,
                               data_quality_status="OK", ohlcv_freshness="FRESH")
    assert r.families[0]["family_status"] == FAM_WATCH_ONLY
    assert "single_symbol_dominance" in r.families[0]["blockers"]


def test_event_catalyst_is_feature_only():
    reps = {"event_catalyst": {"decision": "SHADOW_RESEARCH_ONLY"}}
    r = aggregate_edge_families(reps, clock_drift_status="OK", clean_n=100,
                               data_quality_status="OK", ohlcv_freshness="FRESH")
    assert r.families[0]["family_status"] == FAM_FEATURE_ONLY


def test_runner_with_no_db_and_no_data_is_safe():
    r = run_edge_discovery_orchestrator(db=None, hours=24, external_data_path=None,
                                       clock_drift_status="UNKNOWN", clean_n=0)
    assert r.live_ready is False and r.paper_ready is False
    assert r.final_recommendation == "NO LIVE"
    assert r.best_family_status == FAM_NEED_DATA
