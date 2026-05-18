from app.candidate_incubator import CandidateIncubator
from app.candidate_incubator_smoke_test import _FakeIncubatorDb, candidate_incubator_smoke_text
from app.config import BotConfig


def test_candidate_incubator_statuses_and_market_probe_block():
    payload = CandidateIncubator(BotConfig(), _FakeIncubatorDb()).build(hours=24)
    statuses = {row["candidate_status"] for row in payload["candidates"]}
    probe_rows = [row for row in payload["candidates"] if row["source"] == "market_probe"]

    assert payload["final_recommendation"] == "NO LIVE"
    assert payload["paper_filter_enabled"] is False
    assert "REJECT" in statuses
    assert "WATCH_ONLY" in statuses
    assert "NEED_MORE_DATA" in statuses
    assert "SHADOW_ONLY" in statuses
    assert "PAPER_CANDIDATE_DISABLED" in statuses
    assert probe_rows
    assert all(row["candidate_status"] == "REJECT" for row in probe_rows)


def test_candidate_incubator_smoke_test_passes_and_never_activates():
    text = candidate_incubator_smoke_text(BotConfig())

    assert "CANDIDATE INCUBATOR SMOKE TEST START" in text
    assert "paper_candidate_disabled_no_activation: true" in text
    assert "market_probe_never_actionable: true" in text
    assert "paper_filter_enabled: false" in text
    assert "opened_real_trades: 0" in text
    assert "final_recommendation: NO LIVE" in text
    assert "result: PASS" in text
