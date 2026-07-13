"""V10.46 Policy: a frozen, hashable decision bundle (RESEARCH ONLY).

A Policy maps a causal FeatureSnapshot to a structured decision (TRADE side +
calibrated probability, or ABSTAIN). The Champion is FROZEN (immutable hash).
A Challenger is a Champion plus EXACTLY ONE mutation. Policies never place
orders; they only emit decisions the SimOMS simulates.
"""

from __future__ import annotations

import copy
import math
from typing import Any

from . import contracts as C
from . import strategies as ST

MUTABLE_DIMS = ("threshold", "stop_frac", "tp_frac", "time_exit",
                "trailing_frac", "abstention", "trend_variant",
                "slope_min", "cooldown_clusters")


def default_policy(policy_id: str, *, kind: str = "static",
                   abstention: bool = True) -> dict:
    return {
        "policy_id": policy_id,
        "kind": kind,                       # "static" | "learning"
        "abstention": abstention,
        "threshold": 0.55,
        "stop_frac": 0.008,
        "tp_frac": 0.012,
        "time_exit": 20,
        "trailing_frac": None,
        "trend_variant": "trend_adaptive",
        "slope_min": 0.00003,
        "cooldown_clusters": 1,
        "weights": None,                    # learning model weights (or None)
        "scenario_money": "5eur",
        "scenario_cost": "observed",
        "frozen": kind == "static",
    }


def policy_hash(pol: dict) -> str:
    core = {k: pol.get(k) for k in
            ("kind", "abstention", "threshold", "stop_frac", "tp_frac",
             "time_exit", "trailing_frac", "trend_variant", "slope_min",
             "cooldown_clusters", "weights", "scenario_money", "scenario_cost")}
    return C.canonical_hash(core)


def freeze(pol: dict) -> dict:
    p = copy.deepcopy(pol)
    p["frozen"] = True
    p["policy_hash"] = policy_hash(p)
    return p


def mutate(parent: dict, dim: str, value: Any, *, policy_id: str) -> dict:
    """Return a Challenger = parent + EXACTLY ONE mutation. Raises if `dim` is
    not a single allowed mutable dimension."""
    if dim not in MUTABLE_DIMS:
        raise ValueError(f"not a mutable dimension: {dim}")
    child = copy.deepcopy(parent)
    child["policy_id"] = policy_id
    child["parent_policy_id"] = parent["policy_id"]
    child["mutation"] = {"dim": dim, "from": parent.get(dim), "to": value}
    child[dim] = value
    child["frozen"] = False
    child["policy_hash"] = policy_hash(child)
    return child


def _feature_vector(feats: dict) -> list[float]:
    return [
        1.0,
        min(max(feats.get("slope", 0.0) * 500.0, -3), 3),
        (feats.get("up_fraction", 0.5) - 0.5) * 2,
        min(max(feats.get("vol_accel", 1.0) - 1.0, -2), 2),
        (feats.get("move_consumed", 0.5) - 0.5) * 2,
        min(max(feats.get("volatility", 0.0) * 200.0, 0), 3),
    ]


def _sigmoid(z: float) -> float:
    if z < -35:
        return 0.0
    if z > 35:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def model_prob(pol: dict, feats: dict) -> float | None:
    """Learned P(net>0 | features) for the proposed side, or None when the
    policy has no trained model yet."""
    w = pol.get("weights")
    if not w:
        return None
    x = _feature_vector(feats)
    if len(w) != len(x):
        return None
    return _sigmoid(sum(wi * xi for wi, xi in zip(w, x)))


def decide(pol: dict, fsnap: dict, *, symbol: str, venue: str, timeframe: str,
           event_id: str, decision_time_ms: int,
           data_generation_id: str | None) -> dict:
    """Produce a DecisionRecord (TRADE side / ABSTAIN) for this policy at this
    causal decision point."""
    feats = fsnap.get("features") or {}
    regime = fsnap.get("regime", "RANGE")

    def _dec(action, side, prob):
        return C.make("DecisionRecord", symbol=symbol, venue=venue,
                      timeframe=timeframe, event_id=event_id,
                      causal_cutoff_ms=decision_time_ms,
                      data_generation_id=data_generation_id,
                      spec_hash=pol.get("policy_hash"),
                      decision_action=action, side=side,
                      reason_codes=[action], proposals_for=1 if action == "TRADE" else 0,
                      proposals_against=0, calibrated_probability=round(prob, 6),
                      regime=regime, policy_id=pol["policy_id"])

    variant = "trend_no_abstention" if not pol["abstention"] else pol["trend_variant"]
    prop = ST.trend_rider(fsnap, symbol=symbol, venue=venue, timeframe=timeframe,
                          event_id=event_id, decision_time_ms=decision_time_ms,
                          data_generation_id=data_generation_id, variant=variant,
                          params={"slope_min": pol["slope_min"]})
    if prop.get("contract") == "DecisionRecord":     # trend rider abstained
        if not pol["abstention"]:
            # no-abstention policy still needs a side: follow slope sign
            slope = feats.get("slope", 0.0)
            side = "LONG" if slope >= 0 else "SHORT"
            prob = model_prob(pol, feats)
            prob = prob if prob is not None else 0.5
            return _dec("TRADE", side, prob)
        return prop
    side = prop["side"]
    # probability: learned model if available, else the strategy's estimate
    prob = model_prob(pol, feats)
    if prob is None:
        prob = prop["calibrated_probability"]
    if pol["abstention"] and prob < pol["threshold"]:
        return _dec("ABSTAIN_UNCERTAINTY", "FLAT", prob)
    return _dec("TRADE", side, prob)
