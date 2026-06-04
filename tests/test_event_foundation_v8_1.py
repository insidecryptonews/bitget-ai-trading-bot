"""Tests for ResearchOps V8.1 Event Foundation modules.

Covers:
- event_id stability + idempotence
- registering the same event twice does not duplicate
- source conflict → source_conflict_flag=True
- symbol without perp on bitget → NOT_ACTIONABLE_NO_PERP
- NEED_DATA path when external sources are missing
- CLI commands respond without raising
- pack does not leak secrets
- AST safety scan on all new modules
"""

from __future__ import annotations

import ast
import importlib
import pathlib
from datetime import datetime, timezone

import pytest

from app.events import (
    FAMILY_CROWDING_OI_FUNDING,
    FAMILY_MACRO_SCHEDULED_CONTEXT,
    FAMILY_POST_LISTING_HIGH_FDV,
    FAMILY_TOKEN_UNLOCK,
    STATUS_ACTIONABLE_LABEL_ONLY,
    STATUS_CONTEXT_ONLY,
    STATUS_DETECTED,
    STATUS_LOW_SHORTABILITY,
    STATUS_NEED_DATA,
    STATUS_NEEDS_REVIEW,
    STATUS_NOT_ACTIONABLE_NO_PERP,
)
from app.events.catalyst_layer import (
    detect_source_conflict,
    event_id_crowding,
    event_id_listing,
    event_id_unlock,
    ingest_event,
)
from app.events.event_candidate_registry import (
    evaluate_candidate,
    register_candidate,
    summarise,
)
from app.events.event_store import EventCandidate, EventStore
from app.events.listing_tracker import build_listing_audit
from app.events.perp_availability_checker import (
    batch_check_perp_availability,
    check_perp_availability,
    summarise_perp_audit,
)
from app.events.research_pack_event_v1 import (
    build_event_pack_v1,
    render_event_pack_v1_text,
)
from app.events.shortability_score import (
    SCORE_STATUS_NEED_DATA,
    SCORE_STATUS_NO_PERP,
    SCORE_STATUS_OK,
    compute_shortability,
)
from app.events.unlock_watchlist import build_unlock_audit, cross_check_sources


UTC = timezone.utc


# ---------------------------------------------------------------------------
# event_id stability and idempotence
# ---------------------------------------------------------------------------

def test_event_id_crowding_is_stable():
    ts = datetime(2026, 6, 4, 1, 23, 45, tzinfo=UTC)
    eid1 = event_id_crowding("BTCUSDT", ts, "funding_oi_break")
    eid2 = event_id_crowding("btcusdt", ts.replace(minute=58), "FUNDING_OI_BREAK")
    # Floor to hour normalises minutes.
    assert eid1 == "crowding:BTCUSDT:2026-06-04T01:00:00Z:funding_oi_break"
    assert eid1 == eid2


def test_event_id_listing_is_stable():
    eid = event_id_listing("xyzusdt", "Bitget", 3)
    assert eid == "listing:XYZUSDT:bitget:launch_day_3"


def test_event_id_unlock_is_stable():
    eid = event_id_unlock("LAB", "2026-08-15", "Tokenomist")
    assert eid == "unlock:LAB:2026-08-15:tokenomist"


# ---------------------------------------------------------------------------
# Registry idempotence and conflict detection
# ---------------------------------------------------------------------------

class _NoopDB:
    pass


def _payload(perp_available: bool = True, **overrides):
    base = dict(
        headline_size_usd=10_000_000.0,
        size_pct_circ=0.05,
        fdv_usd=500_000_000.0,
        perp_available_bitget=perp_available,
        venue_count=3,
        shortability_score=0.55,
    )
    base.update(overrides)
    return base


def test_register_candidate_is_idempotent(tmp_path):
    store = EventStore(base_path=tmp_path / "ev1")
    ts = datetime(2026, 6, 4, 1, 0, tzinfo=UTC)
    a = register_candidate(
        store,
        family=FAMILY_CROWDING_OI_FUNDING,
        symbol="BTCUSDT",
        event_time_utc=ts,
        payload_primary=_payload(),
        source_primary="bitget_public",
        trigger="funding_oi_break",
    )
    b = register_candidate(
        store,
        family=FAMILY_CROWDING_OI_FUNDING,
        symbol="BTCUSDT",
        event_time_utc=ts.replace(minute=30),
        payload_primary=_payload(),
        source_primary="bitget_public",
        trigger="funding_oi_break",
    )
    assert a.event_id == b.event_id
    assert len(store.list_candidates()) == 1
    assert a.research_only is True
    assert a.can_send_real_orders is False
    assert a.final_recommendation == "NO LIVE"


def test_source_conflict_flags_when_size_diverges(tmp_path):
    store = EventStore(base_path=tmp_path / "ev2")
    ts = datetime(2026, 6, 5, tzinfo=UTC)
    rec = register_candidate(
        store,
        family=FAMILY_TOKEN_UNLOCK,
        symbol="LAB",
        event_time_utc=ts,
        payload_primary={
            "headline_size_usd": 10_000_000.0,
            "size_pct_circ": 0.10,
            "perp_available_bitget": True,
            "shortability_score": 0.6,
        },
        source_primary="tokenomist",
        payload_secondary={
            "headline_size_usd": 15_000_000.0,
            "size_pct_circ": 0.18,
            "perp_available_bitget": True,
        },
        source_secondary="tokenunlocks",
        unlock_date="2026-08-15",
    )
    assert rec.source_conflict_flag is True
    assert rec.status == STATUS_NEEDS_REVIEW
    assert "size_pct_circ" in rec.conflict_reason or "headline_size_usd" in rec.conflict_reason


def test_detect_source_conflict_returns_no_conflict_when_within_tolerance():
    conflict, reason = detect_source_conflict(
        primary_payload={"headline_size_usd": 10_000_000.0},
        secondary_payload={"headline_size_usd": 10_500_000.0},
    )
    assert conflict is False
    assert reason == ""


def test_macro_event_is_context_only(tmp_path):
    store = EventStore(base_path=tmp_path / "ev_macro")
    ts = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    rec = register_candidate(
        store,
        family=FAMILY_MACRO_SCHEDULED_CONTEXT,
        symbol="MACRO",
        event_time_utc=ts,
        payload_primary={"event": "cpi_us"},
        source_primary="manual_entry",
        macro_label="cpi_us",
    )
    assert rec.status == STATUS_CONTEXT_ONLY


# ---------------------------------------------------------------------------
# No-perp gate
# ---------------------------------------------------------------------------

def test_candidate_without_perp_is_not_actionable(tmp_path):
    store = EventStore(base_path=tmp_path / "ev_noperp")
    ts = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    rec = register_candidate(
        store,
        family=FAMILY_POST_LISTING_HIGH_FDV,
        symbol="NEW",
        event_time_utc=ts,
        payload_primary={
            "fdv_usd": 8_000_000_000.0,
            "float_pct": 0.05,
            "age_days_since_listing": 2,
            "perp_available_bitget": False,
            "venue_count": 1,
            "shortability_score": None,
        },
        source_primary="binance_public",
        venue="binance",
        day_n=2,
    )
    assert rec.perp_available_bitget is False
    assert rec.status == STATUS_NOT_ACTIONABLE_NO_PERP
    # Evaluation must keep it non-actionable.
    v = evaluate_candidate(store, event_id=rec.event_id)
    assert v.status == STATUS_NOT_ACTIONABLE_NO_PERP


def test_full_gates_promote_to_label_only(tmp_path):
    store = EventStore(base_path=tmp_path / "ev_actionable")
    ts = datetime(2026, 6, 4, 1, 0, tzinfo=UTC)
    rec = register_candidate(
        store,
        family=FAMILY_CROWDING_OI_FUNDING,
        symbol="DOTUSDT",
        event_time_utc=ts,
        payload_primary=_payload(perp_available=True, shortability_score=0.8),
        source_primary="bitget_public",
        trigger="funding_oi_break",
    )
    v = evaluate_candidate(store, event_id=rec.event_id)
    assert v.status == STATUS_ACTIONABLE_LABEL_ONLY
    assert v.research_only is True
    assert v.can_send_real_orders is False
    assert v.final_recommendation == "NO LIVE"


def test_low_shortability_is_blocked(tmp_path):
    store = EventStore(base_path=tmp_path / "ev_lowshort")
    ts = datetime(2026, 6, 4, 1, 0, tzinfo=UTC)
    rec = register_candidate(
        store,
        family=FAMILY_CROWDING_OI_FUNDING,
        symbol="DOTUSDT",
        event_time_utc=ts,
        payload_primary=_payload(shortability_score=0.10),
        source_primary="bitget_public",
        trigger="funding_oi_break",
    )
    v = evaluate_candidate(store, event_id=rec.event_id)
    assert v.status == STATUS_LOW_SHORTABILITY


# ---------------------------------------------------------------------------
# NEED_DATA paths
# ---------------------------------------------------------------------------

def test_perp_availability_need_data_when_methods_missing():
    db = _NoopDB()
    result = check_perp_availability(db, symbol="UNKNOWNUSDT")
    assert result.perp_available_bitget is False
    assert "bitget_perp_listing_method_missing" in result.notes
    assert result.research_only is True
    assert result.final_recommendation == "NO LIVE"


def test_shortability_no_perp_returns_no_perp():
    result = compute_shortability(_NoopDB(), symbol="WHATEVER", perp_available=False)
    assert result.score_status == SCORE_STATUS_NO_PERP
    assert result.shortability_score is None


def test_shortability_need_data_when_inputs_missing():
    result = compute_shortability(_NoopDB(), symbol="BTCUSDT", perp_available=True)
    assert result.score_status == SCORE_STATUS_NEED_DATA
    assert result.shortability_score is None


def test_shortability_ok_with_inputs():
    class _DB:
        def latest_bid_ask_spread_bps(self, s): return 5.0
        def top_of_book_depth_usd(self, s): return 200_000.0
        def volume_24h_usd(self, s): return 50_000_000.0
        def latest_funding_rate(self, s): return -0.0002  # paid to short

    result = compute_shortability(_DB(), symbol="BTCUSDT", perp_available=True)
    assert result.score_status == SCORE_STATUS_OK
    assert 0.0 < result.shortability_score <= 1.0


def test_listing_audit_need_data_when_no_method():
    report = build_listing_audit(_NoopDB(), window_days=30)
    assert "no_recent_listings_method_or_empty" in report.need_data_reasons
    assert report.research_only is True
    assert report.final_recommendation == "NO LIVE"


def test_unlock_audit_need_data_when_no_method():
    report = build_unlock_audit(_NoopDB(), window_days=60)
    assert "no_upcoming_unlocks_method_or_empty" in report.need_data_reasons


def test_unlock_cross_check_finds_conflicts_when_sources_disagree():
    from app.events.unlock_watchlist import UnlockRecord
    records = [
        UnlockRecord(token="LAB", unlock_date="2026-08-15", source="tokenomist",
                     size_pct_circ=0.10),
        UnlockRecord(token="LAB", unlock_date="2026-08-16", source="tokenunlocks",
                     size_pct_circ=0.18),
    ]
    conflicts = cross_check_sources(records)
    assert any(c["field"] == "unlock_date" for c in conflicts)
    assert any(c["field"] == "size_pct_circ" for c in conflicts)


# ---------------------------------------------------------------------------
# Pack
# ---------------------------------------------------------------------------

def test_event_pack_v1_renders_and_has_no_secrets(tmp_path):
    store = EventStore(base_path=tmp_path / "ev_pack")
    ts = datetime(2026, 6, 4, 1, 0, tzinfo=UTC)
    register_candidate(
        store,
        family=FAMILY_CROWDING_OI_FUNDING,
        symbol="BTCUSDT",
        event_time_utc=ts,
        payload_primary=_payload(),
        source_primary="bitget_public",
        trigger="funding_oi_break",
    )
    payload = build_event_pack_v1(
        config=None, db=_NoopDB(), store=store, sample_symbols=["BTCUSDT"],
    )
    text = render_event_pack_v1_text(payload)
    assert "RESEARCH PACK EVENT V1 START" in text
    assert "RESEARCH PACK EVENT V1 END" in text
    assert payload["pack_version"] == "event_v1"
    assert payload["total_events"] == 1
    assert payload["final_recommendation"] == "NO LIVE"
    blob = (text + " " + str(payload)).lower()
    for forbidden in ("api_key", "api_secret", "passphrase", "private_key"):
        assert forbidden not in blob


# ---------------------------------------------------------------------------
# CLI smoke tests via ResearchLab
# ---------------------------------------------------------------------------

def test_cli_event_methods_respond_without_raising():
    from app.research_lab import ResearchLab

    lab = ResearchLab(config=None, db=_NoopDB())
    assert "EVENT CATALYST STATUS START" in lab.event_catalyst_status()
    assert "LISTING TRACKER AUDIT START" in lab.listing_tracker_audit()
    assert "UNLOCK WATCHLIST AUDIT START" in lab.unlock_watchlist_audit()
    assert "PERP AVAILABILITY AUDIT START" in lab.perp_availability_audit(
        symbols=["BTCUSDT"]
    )
    assert "SHORTABILITY SCORE AUDIT START" in lab.shortability_score_audit(
        symbols=["BTCUSDT"]
    )
    assert "EVENT CANDIDATE REGISTRY STATUS START" in lab.event_candidate_registry_status()
    out = lab.research_pack_event_v1(symbols=["BTCUSDT"])
    assert "RESEARCH PACK EVENT V1 START" in out
    assert "final_recommendation: NO LIVE" in out


# ---------------------------------------------------------------------------
# Safety AST scan
# ---------------------------------------------------------------------------

FORBIDDEN_CALL_NAMES = {
    "place_order", "set_leverage", "set_margin_mode",
    "private_get", "private_post",
}

FORBIDDEN_ASSIGN_LITERALS = {
    "LIVE_TRADING", "ENABLE_PAPER_POLICY_FILTER",
    "can_send_real_orders", "allow_real_writes", "apply",
}

V8_1_MODULES = [
    "app.events",
    "app.events.event_store",
    "app.events.catalyst_layer",
    "app.events.listing_tracker",
    "app.events.unlock_watchlist",
    "app.events.perp_availability_checker",
    "app.events.shortability_score",
    "app.events.event_candidate_registry",
    "app.events.research_pack_event_v1",
]


def _module_path(modname: str) -> pathlib.Path:
    return pathlib.Path(importlib.import_module(modname).__file__)


def test_v8_1_modules_have_no_forbidden_calls():
    for mod in V8_1_MODULES:
        tree = ast.parse(_module_path(mod).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name not in FORBIDDEN_CALL_NAMES, (
                    f"{mod} must not call {name}"
                )


def test_v8_1_modules_have_no_forbidden_literal_true_assigns():
    for mod in V8_1_MODULES:
        tree = ast.parse(_module_path(mod).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                    if name in FORBIDDEN_ASSIGN_LITERALS and isinstance(node.value, ast.Constant):
                        assert node.value.value is not True, (
                            f"{mod} contains forbidden {name}=True"
                        )


def test_event_candidate_safety_invariants_are_baked_in(tmp_path):
    store = EventStore(base_path=tmp_path / "ev_inv")
    ts = datetime(2026, 6, 4, 1, 0, tzinfo=UTC)
    rec = register_candidate(
        store,
        family=FAMILY_CROWDING_OI_FUNDING,
        symbol="BTCUSDT",
        event_time_utc=ts,
        payload_primary=_payload(),
        source_primary="bitget_public",
        trigger="funding_oi_break",
    )
    snap = summarise(store)
    assert snap["research_only"] is True
    assert snap["paper_filter_enabled"] is False
    assert snap["can_send_real_orders"] is False
    assert snap["final_recommendation"] == "NO LIVE"
    assert rec.research_only is True
    assert rec.paper_filter_enabled is False
    assert rec.can_send_real_orders is False
    assert rec.final_recommendation == "NO LIVE"
