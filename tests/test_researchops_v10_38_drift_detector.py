"""V10.38 Drift/decay detector: research actions only, never touches real orders."""

from __future__ import annotations

from app.labs import continuous_edge_factory_v10_38 as CE


def test_ev_sign_flip_pauses_shadow():
    rep = CE.drift_check([-0.002] * 20, [0.003] * 20)
    assert "EV_SIGN_FLIP" in rep["signals"]
    assert rep["action"] == "PAUSE_CANDIDATE_SHADOW"


def test_ev_decay_requires_revalidation():
    # positive but well under half of the reference EV, same sign
    rep = CE.drift_check([0.0005] * 20, [0.004] * 20)
    assert "EV_DECAY" in rep["signals"] or "HIT_RATE_DROP" in rep["signals"]
    assert rep["action"] == "REQUIRE_REVALIDATION"


def test_feature_distribution_drift_blocks_promotion():
    stable = [0.001] * 20                       # identical outcomes -> no EV flag
    rep = CE.drift_check(stable, stable,
                         recent_features=[5.0] * 12,
                         reference_features=[0.0] * 20)
    assert rep["signals"] == ["FEATURE_DISTRIBUTION_DRIFT"]
    assert rep["action"] == "BLOCK_PROMOTION"


def test_no_signal_defaults_to_alert_only():
    stable = [0.001] * 20
    rep = CE.drift_check(stable, stable)
    assert rep["signals"] == []
    assert rep["action"] == "ALERT_ONLY"


def test_action_always_in_allowed_set_and_research_only():
    for rep in (CE.drift_check([-0.002] * 20, [0.003] * 20),
                CE.drift_check([0.0005] * 20, [0.004] * 20),
                CE.drift_check([0.001] * 20, [0.001] * 20)):
        assert rep["action"] in CE.DRIFT_ACTIONS
        assert rep["can_send_real_orders"] is False
        assert rep["final_recommendation"] == "NO LIVE"
        # explicitly no real-execution verbs anywhere in the report
        for banned in ("close_position", "cancel_order", "set_leverage",
                       "send_order", "BUY_NOW", "SELL_NOW"):
            assert banned not in str(rep)
        assert "FUTURE_REAL_POLICY" in rep["future_real_policy_note"]
