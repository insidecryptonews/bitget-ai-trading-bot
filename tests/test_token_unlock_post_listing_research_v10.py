"""ResearchOps V10 — Token Unlock / Post-Listing research tests.

All synthetic. No DB. No network.
"""

from __future__ import annotations

from app.labs.edge_data_foundation_v10 import ACT_NOT_ACTIONABLE, ACT_WATCH_ONLY
from app.labs.token_unlock_post_listing_research_v10 import (
    DECISION_IMPLEMENT_FIRST,
    DECISION_NEED_DATA,
    analyze_unlock_post_listing,
    run_unlock_post_listing_research,
)


def _unlock(eid, sym, *, pct=12.0, mcap=1e8, fdv=5e8, conf=0.8, rel=0.9, bias="SHORT", t="2026-06-10T00:00:00+00:00"):
    return {
        "event_id": eid, "symbol": sym, "event_time": t, "source": "unlocks.app",
        "source_reliability": rel, "unlock_pct_circulating": pct,
        "market_cap": mcap, "fdv": fdv, "confidence_score": conf, "direction_bias": bias,
    }


def test_no_data_is_need_data():
    r = run_unlock_post_listing_research(hours=720, external_data_path=None)
    assert r.decision == DECISION_NEED_DATA
    assert r.final_recommendation == "NO LIVE"
    assert r.can_send_real_orders is False


def test_low_source_reliability_not_actionable():
    rows = [_unlock("u1", "ABCUSDT", rel=0.2, conf=0.9)]
    r = analyze_unlock_post_listing(rows, hours=720)
    assert r.event_windows[0]["actionability"] == ACT_NOT_ACTIONABLE
    assert r.not_actionable_low_reliability == 1


def test_uncertain_event_embargoed_to_watch_only():
    # high reliability but low confidence => embargo => WATCH_ONLY ceiling
    rows = [_unlock("u1", "ABCUSDT", rel=0.95, conf=0.3)]
    r = analyze_unlock_post_listing(rows, hours=720)
    assert r.event_windows[0]["actionability"] == ACT_WATCH_ONLY
    assert r.embargoed_events == 1


def test_material_unlocks_implement_first_research():
    rows = [
        _unlock("u1", "ABCUSDT", pct=12.0, mcap=1e8, fdv=5e8, conf=0.8),
        _unlock("u2", "XYZUSDT", pct=8.0, mcap=2e8, fdv=8e8, conf=0.7),
        _unlock("u3", "QQQUSDT", pct=20.0, mcap=5e7, fdv=6e8, conf=0.9),
    ]
    r = analyze_unlock_post_listing(rows, hours=720, source_label="csv:test")
    assert r.material_unlock_events == 3
    assert r.high_fdv_events >= 2
    assert r.event_study_ready is True
    assert r.decision == DECISION_IMPLEMENT_FIRST
    assert r.short_bias_score > 0
    assert r.final_recommendation == "NO LIVE"


def test_short_basket_direction_bias_defaults_short():
    rows = [_unlock("u1", "ABCUSDT")]
    r = analyze_unlock_post_listing(rows, hours=720)
    assert r.event_windows[0]["direction_bias"] == "SHORT"
