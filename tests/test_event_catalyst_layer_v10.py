"""ResearchOps V10 — Event Catalyst Layer tests.

All synthetic. No DB. No network.
"""

from __future__ import annotations

from app.labs.edge_data_foundation_v10 import (
    ACT_NOT_ACTIONABLE,
    ACT_SHADOW_RESEARCH_ONLY,
    ACT_WATCH_ONLY,
)
from app.labs.event_catalyst_layer_v10 import (
    DECISION_NEED_DATA,
    DECISION_RESEARCH,
    DECISION_WATCH_ONLY,
    analyze_event_catalysts,
    run_event_catalyst_layer,
)


def _ev(eid, *, rel=0.9, conf=0.9, etype="token_unlock", tech="false",
        sym="ABCUSDT", t="2026-06-10T00:00:00+00:00", bias="SHORT"):
    return {
        "event_id": eid, "timestamp": t, "source": "official",
        "source_reliability": rel, "confidence_score": conf, "severity_score": 0.7,
        "event_type": etype, "affected_symbols": sym, "direction_bias": bias,
        "technical_confirmation_required": tech,
    }


def test_no_data_is_need_data():
    r = run_event_catalyst_layer(hours=720, external_data_path=None)
    assert r.decision == DECISION_NEED_DATA
    assert r.final_recommendation == "NO LIVE"
    assert r.can_send_real_orders is False


def test_low_reliability_not_actionable():
    r = analyze_event_catalysts([_ev("e1", rel=0.2, conf=0.9)], hours=720)
    assert r.events[0]["actionability"] == ACT_NOT_ACTIONABLE
    assert r.not_actionable_low_reliability == 1


def test_uncertain_event_embargoed():
    r = analyze_event_catalysts([_ev("e1", rel=0.95, conf=0.3, tech="true")], hours=720)
    assert r.events[0]["actionability"] == ACT_WATCH_ONLY
    assert r.embargoed_events == 1
    assert r.decision == DECISION_WATCH_ONLY


def test_reliable_confident_no_tech_required_is_shadow_research():
    r = analyze_event_catalysts([_ev("e1", rel=0.95, conf=0.9, tech="false")], hours=720)
    assert r.events[0]["actionability"] == ACT_SHADOW_RESEARCH_ONLY
    assert r.decision == DECISION_RESEARCH


def test_actionability_never_exceeds_shadow():
    # Across many events, nothing is ever operative.
    rows = [_ev(f"e{i}", rel=0.99, conf=0.99, tech="false") for i in range(20)]
    r = analyze_event_catalysts(rows, hours=720)
    allowed = {ACT_NOT_ACTIONABLE, ACT_WATCH_ONLY, ACT_SHADOW_RESEARCH_ONLY}
    for e in r.events:
        assert e["actionability"] in allowed


def test_macro_event_without_symbol_validates():
    rows = [{"event_id": "m1", "timestamp": "2026-06-12T00:00:00+00:00",
             "source": "fed", "source_reliability": 0.9, "confidence_score": 0.9,
             "severity_score": 0.9, "event_type": "macro", "direction_bias": "unknown",
             "technical_confirmation_required": "true"}]
    r = analyze_event_catalysts(rows, hours=720)
    assert r.valid_events == 1
    assert r.by_type.get("macro") == 1
