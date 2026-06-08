"""ResearchOps V10 — Micro-TP viability tests.

Pure cost math. No DB. No network.
"""

from __future__ import annotations

from app.labs.micro_tp_viability_v10 import (
    DECISION_AUDIT_ONLY,
    DECISION_NEED_DATA,
    DECISION_NOT_CORE,
    DECISION_REJECT_COSTS,
    analyze_micro_tp_viability,
)


def test_tp_below_cost_is_reject_costs_too_high():
    # TP 0.03% <= every round-trip cost scenario => mechanically impossible.
    r = analyze_micro_tp_viability(tp_grid=[0.03], sl_grid=[0.25])
    assert r.decision == DECISION_REJECT_COSTS
    assert r.realistic_feasible_combos == 0
    assert r.maker_maker_feasible_combos == 0
    assert "tp_below_or_equal_roundtrip_cost" in r.blockers
    assert r.final_recommendation == "NO LIVE"


def test_only_maker_maker_feasible_is_audit_only_not_promotable():
    # TP=SL=0.10: feasible only under maker_maker (0.04), not maker_taker (0.08).
    r = analyze_micro_tp_viability(tp_grid=[0.10], sl_grid=[0.10])
    assert r.decision == DECISION_AUDIT_ONLY
    assert r.maker_only_required is True
    assert r.need_websocket is True
    assert r.realistic_feasible_combos == 0
    assert r.maker_maker_feasible_combos >= 1


def test_maker_maker_never_promotes_even_with_winrate():
    # Even handing a great observed winrate, maker_maker-only stays AUDIT_ONLY.
    r = analyze_micro_tp_viability(
        tp_grid=[0.10], sl_grid=[0.10], observed_winrate=0.99, observed_trades=500,
    )
    assert r.decision == DECISION_AUDIT_ONLY


def test_small_sample_is_need_more_data():
    r = analyze_micro_tp_viability(
        tp_grid=[1.0], sl_grid=[0.5], observed_winrate=0.7, observed_trades=10,
    )
    assert r.decision == DECISION_NEED_DATA
    assert "insufficient_observed_sample" in r.blockers


def test_negative_observed_ev_is_reject():
    r = analyze_micro_tp_viability(
        tp_grid=[1.0], sl_grid=[0.5], observed_winrate=0.30, observed_trades=200,
    )
    assert r.net_ev_pct is not None and r.net_ev_pct <= 0
    assert r.decision == DECISION_REJECT_COSTS


def test_viable_after_costs_caps_at_not_core():
    # Feasible + sufficient sample + positive net EV => still only NOT_CORE.
    r = analyze_micro_tp_viability(
        tp_grid=[1.0], sl_grid=[0.5], observed_winrate=0.75, observed_trades=200,
    )
    assert r.decision == DECISION_NOT_CORE
    assert r.viable_after_costs is True
    assert r.can_send_real_orders is False


def test_min_required_winrate_monotonic_in_cost():
    # Higher round-trip cost => higher break-even winrate for the same TP/SL.
    from app.labs.micro_tp_viability_v10 import _min_required_winrate
    low = _min_required_winrate(1.0, 0.5, 0.08)
    high = _min_required_winrate(1.0, 0.5, 0.25)
    assert low is not None and high is not None
    assert high > low
    # TP below cost is impossible (None).
    assert _min_required_winrate(0.05, 0.5, 0.12) is None


def test_best_realistic_min_winrate_in_range():
    r = analyze_micro_tp_viability(tp_grid=[1.0], sl_grid=[0.5])
    assert r.best_realistic_min_winrate is not None
    assert 0.0 < r.best_realistic_min_winrate < 1.0
