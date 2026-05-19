from app.candidate_incubator import CandidateIncubator
from app.candidate_incubator_smoke_test import _FakeIncubatorDb
from app.config import BotConfig


def test_market_probe_positive_micro_pocket_is_not_actionable_candidate():
    payload = CandidateIncubator(BotConfig(), _FakeIncubatorDb()).build(hours=24)
    probe_rows = [row for row in payload["candidates"] if row["source"] == "market_probe"]

    assert probe_rows
    assert all(row["actionability"] == "NOT_ACTIONABLE_MARKET_PROBE" for row in probe_rows)
    assert all(row["candidate_status"] not in {"SHADOW_ONLY", "PAPER_CANDIDATE_DISABLED"} for row in probe_rows)
    assert any(row["candidate_category"] in {"NEED_MORE_DATA_NOT_ACTIONABLE", "WATCH_ONLY_MARKET_PROBE"} for row in probe_rows)
    assert payload["paper_filter_enabled"] is False


def test_candidate_incubator_exposes_research_pockets_separately():
    payload = CandidateIncubator(BotConfig(), _FakeIncubatorDb()).build(hours=24)

    assert "promising_not_actionable" in payload
    assert "market_probe_only" in payload
    assert payload["actionable_candidates"] == []
    assert payload["final_recommendation"] == "NO LIVE"
