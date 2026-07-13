"""V10.46.4 Trend Rider + P01-P12 + AI agents + meta-abstention: causal
features (no lookahead), directional confirmation, lateral abstention, valid
proposal contracts, and structured meta-abstention. Research only, NO LIVE."""

from __future__ import annotations

import pytest

from app.labs.v10_46 import agents as AG
from app.labs.v10_46 import contracts as C
from app.labs.v10_46 import features as F
from app.labs.v10_46 import strategies as ST

T0 = 1_700_000_400_000
BAR = 60_000


def _bars(fn, n=80):
    return [{"ts": T0 + i * BAR, **fn(i)} for i in range(n)]


def _up(i):
    p = 100.0 + i * 0.15
    return {"open": p, "high": p + 0.2, "low": p - 0.05, "close": p + 0.15,
            "volume": 10.0 + i * 0.1}


def _down(i):
    p = 130.0 - i * 0.15
    return {"open": p, "high": p + 0.05, "low": p - 0.2, "close": p - 0.15,
            "volume": 10.0 + i * 0.1}


def _flat(i):
    import math
    p = 100.0 + math.sin(i / 3.0) * 0.3
    return {"open": p, "high": p + 0.2, "low": p - 0.2, "close": p,
            "volume": 10.0}


def _fsnap(bars):
    dt = bars[-1]["ts"] + BAR
    return F.compute_features(bars, decision_time_ms=dt), dt


# ==========================================================================
# CAUSAL FEATURES
# ==========================================================================

def test_features_are_causal_and_labelled():
    bars = _bars(_up)
    fs, dt = _fsnap(bars)
    assert fs["quality"] == "OK"
    for name, meta in fs["feature_meta"].items():
        assert meta["available_time_ms"] <= dt         # nothing from the future
        assert meta["decision_time_ms"] == dt


def test_p_family_registry_covers_p01_p12():
    for i in range(1, 13):
        assert f"P{i:02d}" in ST.P_FAMILIES
    assert ST.P_FAMILIES["P01"][3] == "IMPLEMENTED"
    assert len(ST.TREND_RIDER_VARIANTS) == 8


# ==========================================================================
# TREND RIDER
# ==========================================================================

def _run(bars, **kw):
    fs, dt = _fsnap(bars)
    return ST.trend_rider(fs, symbol="BTCUSDT", venue="bitget",
                          timeframe="1m", event_id=f"BTCUSDT:{dt}",
                          decision_time_ms=dt, data_generation_id="g", **kw)


def test_trend_rider_proposes_long_on_confirmed_uptrend():
    p = _run(_bars(_up))
    assert p["contract"] == "AgentProposal"
    assert p["side"] == "LONG" and p["action"] == "PROPOSE"
    assert C.validate(p)[0]
    assert "TREND_UP_CONFIRMED" in p["reason_codes"]


def test_trend_rider_proposes_short_on_confirmed_downtrend():
    p = _run(_bars(_down))
    assert p["side"] == "SHORT" and p["action"] == "PROPOSE"
    assert C.validate(p)[0]


def test_trend_rider_abstains_in_range():
    d = _run(_bars(_flat))
    assert d["contract"] == "DecisionRecord"
    assert d["decision_action"].startswith("ABSTAIN")
    assert d["side"] == "FLAT"


def test_trend_rider_single_bar_up_is_not_enough():
    bars = _bars(_flat)
    bars[-1] = {"ts": bars[-1]["ts"], "open": 100.0, "high": 105.0,
                "low": 99.9, "close": 104.9, "volume": 50.0}  # one big up bar
    d = _run(bars)
    assert d["contract"] == "DecisionRecord"                # still abstains


# ==========================================================================
# AI AGENT PROPOSAL VALIDATION
# ==========================================================================

def test_ai_proposal_validation_rejects_expired_and_noncausal():
    p = _run(_bars(_up))
    ok, _ = AG.validate_ai_proposal(p, decision_time_ms=p["causal_cutoff_ms"],
                                    evidence_times_ms={p["event_id"]:
                                                       p["causal_cutoff_ms"] - 1})
    assert ok
    # expired
    ok2, r2 = AG.validate_ai_proposal(
        {**p, "expiry_ms": p["causal_cutoff_ms"] - 10},
        decision_time_ms=p["causal_cutoff_ms"])
    assert not ok2 and "EXPIRED" in r2
    # non-causal evidence
    ok3, r3 = AG.validate_ai_proposal(
        p, decision_time_ms=p["causal_cutoff_ms"],
        evidence_times_ms={p["event_id"]: p["causal_cutoff_ms"] + 1000})
    assert not ok3 and any("NON_CAUSAL" in x for x in r3)


# ==========================================================================
# META-ABSTENTION
# ==========================================================================

def _prop(side, prob):
    return C.make("AgentProposal", symbol="X", venue="bitget", timeframe="1m",
                  event_id="X:1", causal_cutoff_ms=1000, agent="A",
                  action="PROPOSE", side=side, calibrated_probability=prob,
                  expected_win_pct=0.6, expected_loss_pct=0.4,
                  expected_duration_ms=60000, fill_probability=0.9,
                  entry_zone={}, invalidation={}, target={},
                  cost_estimate_eur=0.01, evidence_ids=["X:1"], regime="TREND_UP",
                  reason_codes=["X"], expiry_ms=100000, model_version="m")


def _meta(props, **kw):
    return AG.meta_abstention(props, symbol="X", venue="bitget", timeframe="1m",
                              event_id="X:1", decision_time_ms=1000,
                              data_generation_id="g", **kw)


def test_meta_abstention_disagreement_and_trade():
    d = _meta([_prop("LONG", 0.7), _prop("SHORT", 0.65)])
    assert d["decision_action"] == "ABSTAIN_DISAGREEMENT"
    t = _meta([_prop("LONG", 0.7), _prop("LONG", 0.6)])
    assert t["decision_action"] == "TRADE" and t["side"] == "LONG"


def test_meta_abstention_cost_and_uncertainty():
    lowp = _meta([_prop("LONG", 0.51)])
    assert lowp["decision_action"] == "ABSTAIN_UNCERTAINTY"
    costly = _meta([_prop("LONG", 0.8)], expected_reward_eur=0.005,
                   cost_estimate_eur=0.02)
    assert costly["decision_action"] == "ABSTAIN_COST"
    empty = _meta([])
    assert empty["decision_action"].startswith("ABSTAIN")
