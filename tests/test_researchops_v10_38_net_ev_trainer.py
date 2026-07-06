"""V10.38 Net-EV trainer: costs-first, abstention-by-default, never actionable."""

from __future__ import annotations

from app.labs import continuous_edge_factory_v10_38 as CE


def test_abstain_below_min_sample():
    rep = CE.evaluate_net_ev([0.02] * (CE.MIN_SAMPLE - 1))
    assert rep["decision"] == "ABSTAIN"
    assert rep["net_EV"] is None and rep["gross_EV"] is None
    assert f"<{CE.MIN_SAMPLE}" in rep["reason"]


def test_reject_negative_ev():
    rep = CE.evaluate_net_ev([-0.001] * 80)
    assert rep["decision"] == "REJECT"
    assert rep["net_EV"] < 0


def test_trade_in_simulation_only_when_lower_bound_beats_min_edge():
    rep = CE.evaluate_net_ev([0.004] * 45 + [-0.001] * 5)
    assert rep["decision"] == "TRADE_IN_SIMULATION_RESEARCH_ONLY"
    # the decision is gated on the LOWER bound, never the point estimate
    assert rep["net_EV_lower_bound"] > 2.0 / 10_000
    assert rep["net_EV_lower_bound"] < rep["net_EV"]        # uncertainty penalty


def test_positive_mean_thin_edge_abstains_not_trades():
    # mean > 0 but noisy enough that the lower bound sits under min edge
    rep = CE.evaluate_net_ev([0.02, -0.019] * 40)
    assert rep["net_EV"] is not None
    assert rep["decision"] in ("ABSTAIN", "REJECT")
    assert rep["decision"] != "TRADE_IN_SIMULATION_RESEARCH_ONLY"


def test_turnover_penalty_reduces_net_ev():
    base = CE.evaluate_net_ev([0.003] * 60, turnover=1.0)
    heavy = CE.evaluate_net_ev([0.003] * 60, turnover=50.0)
    assert heavy["net_EV"] < base["net_EV"]
    assert heavy["turnover_cost"] > base["turnover_cost"]


def test_win_rate_and_payoff_reported():
    rep = CE.evaluate_net_ev([0.01] * 30 + [-0.005] * 30)
    assert rep["win_rate"] == 0.5
    assert rep["payoff_ratio"] == round(0.01 / 0.005, 4)


def test_never_actionable_no_banned_tokens():
    for rep in (CE.evaluate_net_ev([0.004] * 50),
                CE.evaluate_net_ev([-0.004] * 50),
                CE.evaluate_net_ev([0.0])):
        assert rep["can_send_real_orders"] is False
        assert rep["not_actionable"] is True
        assert rep["edge_validated"] is False
        assert rep["final_recommendation"] == "NO LIVE"
        for banned in ("BUY_NOW", "SELL_NOW", "OPEN_POSITION", "LIVE_SIGNAL"):
            assert banned not in str(rep)
