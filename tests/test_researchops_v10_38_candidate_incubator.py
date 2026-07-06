"""V10.38 Candidate incubator: fail-closed state machine, ranked, audited."""

from __future__ import annotations

import pytest

from app.labs import continuous_edge_factory_v10_38 as CE


def _cand(cid, lb):
    return {"candidate_id": cid, "symbol": "BTCUSDT", "side": "long",
            "setup_name": "burst_score>+p90", "net_EV_lower_bound": lb,
            "net_EV": lb + 0.001, "sample_size": 120, "verdict": "NOT_ACTIONABLE"}


def test_upsert_attaches_promotion_blockers():
    inc = CE.CandidateIncubator()
    e = inc.upsert(_cand("a", 0.001), "DISCOVERED")
    assert e["status"] == "DISCOVERED"
    assert "human_approval_required" in e["promotion_blockers"]
    assert "paper_filter_enabled=false" in e["promotion_blockers"]


@pytest.mark.parametrize("bad", sorted(CE.FORBIDDEN_STATES))
def test_forbidden_states_rejected_on_upsert_and_transition(bad):
    inc = CE.CandidateIncubator()
    with pytest.raises(ValueError):
        inc.upsert(_cand("x", 0.0), bad)
    inc.upsert(_cand("x", 0.0), "DISCOVERED")
    with pytest.raises(ValueError):
        inc.transition("x", bad, "should never be allowed")


def test_unknown_state_rejected():
    inc = CE.CandidateIncubator()
    with pytest.raises(ValueError):
        inc.upsert(_cand("y", 0.0), "TOTALLY_MADE_UP")


def test_transition_records_history():
    inc = CE.CandidateIncubator()
    inc.upsert(_cand("z", 0.0), "DISCOVERED")
    inc.transition("z", "INCUBATING", "needs more data")
    inc.transition("z", "SHADOW_ELIGIBLE", "oos passed research-only")
    c = inc.candidates["z"]
    assert c["status"] == "SHADOW_ELIGIBLE"
    assert [h["to"] for h in c["history"]] == ["INCUBATING", "SHADOW_ELIGIBLE"]
    assert c["history"][0]["from"] == "DISCOVERED"


def test_rank_orders_by_lower_bound_desc():
    inc = CE.CandidateIncubator()
    inc.upsert(_cand("lo", -0.01), "DISCOVERED")
    inc.upsert(_cand("hi", 0.02), "DISCOVERED")
    inc.upsert(_cand("mid", 0.005), "DISCOVERED")
    order = [c["candidate_id"] for c in inc.rank()]
    assert order == ["hi", "mid", "lo"]


def test_states_never_include_live():
    assert not (CE.CANDIDATE_STATES & CE.FORBIDDEN_STATES)
    for forbidden in ("LIVE", "LIVE_READY", "CAN_SEND_REAL_ORDERS"):
        assert forbidden not in CE.CANDIDATE_STATES
