"""V10.38 Shadow runner + paper gate: decisions only, always blocked."""

from __future__ import annotations

from app.labs import continuous_edge_factory_v10_38 as CE

CAND = {"candidate_id": "BTCUSDT_burst_long_1", "symbol": "BTCUSDT",
        "side": "long", "setup_name": "burst_score>+p90", "threshold": 1.0,
        "confidence": 0.6, "net_EV": 0.002, "data_quality": "ok",
        "verdict": "PROMISING_RESEARCH_ONLY", "max_drawdown": -0.01,
        "sample_size": 150}


def _row(**over):
    row = {"ts": 1_700_000_000_000, "burst_score": 5.0, "spread": 0.0,
           "stress_mode": 0.0, "symbol_regime": "trend"}
    row.update(over)
    return row


def test_shadow_decision_fires_but_is_not_actionable():
    d = CE.shadow_decide(CAND, _row())
    assert d["would_enter"] is True
    assert d["abstain_reason"] is None
    assert d["kind"] == "SHADOW_DECISION_ONLY_NOT_ACTIONABLE"
    assert d["can_send_real_orders"] is False
    assert d["final_recommendation"] == "NO LIVE"


def test_shadow_abstains_when_signal_not_fired():
    d = CE.shadow_decide(CAND, _row(burst_score=0.0))
    assert d["would_enter"] is False
    assert d["abstain_reason"] == "signal_not_fired"


def test_shadow_abstains_on_wide_spread():
    d = CE.shadow_decide(CAND, _row(spread=0.002))
    assert d["would_enter"] is False
    assert d["abstain_reason"] == "spread_too_wide"


def test_shadow_abstains_in_stress_regime_for_long():
    d = CE.shadow_decide(CAND, _row(stress_mode=1.0))
    assert d["would_enter"] is False
    assert d["abstain_reason"] == "stress_regime"


def test_shadow_output_has_no_order_primitives():
    d = CE.shadow_decide(CAND, _row())
    blob = str(d).lower()
    for banned in ("place_order", "client_order_id", "api_key", "leverage",
                   "buy_now", "sell_now", "open_position"):
        assert banned not in blob


def test_paper_gate_always_blocked():
    wf = {"verdict": "OOS_PASS_RESEARCH_ONLY"}
    strong = dict(CAND, sample_size=500, net_EV=0.003, max_drawdown=-0.01)
    gate = CE.paper_promotion_gate(strong, wf)
    assert gate["paper_gate_blocked"] is True
    assert gate["paper_filter_enabled"] is False
    # human approval is not encodable -> can never be met
    assert gate["checks"]["human_approval_required"] is False
    assert "human_approval_required" in gate["unmet"]
    assert gate["status"] in ("HUMAN_REVIEW_REQUIRED", "NEEDS_MORE_SHADOW",
                              "PAPER_PROMOTION_REJECTED")
    assert gate["final_recommendation"] == "NO LIVE"


def test_paper_gate_rejects_weak_candidate():
    weak = dict(CAND, sample_size=10, net_EV=-0.001)
    gate = CE.paper_promotion_gate(weak, None)
    assert gate["status"] == "PAPER_PROMOTION_REJECTED"
    assert gate["paper_gate_blocked"] is True
