"""V10.46 strategies: Trend Rider 24/7 + the P01–P12 family registry
(RESEARCH ONLY, causal).

No historical NUMERIC P01–P12 ids existed in the codebase (families were
referenced by descriptive names such as liquidation_cascade, absorption,
cross_venue). These P-ids are therefore NEW and each maps to the closest
existing implementation; the mapping is documented in P_FAMILIES.

Every strategy consumes a causal FeatureSnapshot and returns either an
AgentProposal (PROPOSE LONG/SHORT) or an ABSTAIN decision — never a raw order.
LONG may be researched freely; it stays BLOCKED at Paper Champion until it
proves OOS edge (paper_long_blocked=True).
"""

from __future__ import annotations

from typing import Any

from . import contracts as C

# ------------------------------------------------------------------ registry
# id -> (name, families/concept, canonical existing implementation, status)
P_FAMILIES = {
    "P01": ("Trend Rider LONG/SHORT", "trend continuation",
            "v10_46.strategies.trend_rider", "IMPLEMENTED"),
    "P02": ("Pullback Continuation", "trend pullback",
            "edge_discovery_engine: ema_pullback_* procedural", "MAPPED"),
    "P03": ("Breakout + Volume/Flow", "donchian breakout + vol_z",
            "edge_discovery_engine: donchian_break_* procedural", "MAPPED"),
    "P04": ("Liquidation Cascade Continuation", "liquidation cascade",
            "alpha_improvement_sprint_v10_39: liquidation_cascade", "MAPPED"),
    "P05": ("Liquidation Exhaustion/Reversal", "capitulation rebound",
            "edge_discovery_engine: capitulation_rebound_long", "MAPPED"),
    "P06": ("Order-Book Absorption", "absorption proxy",
            "multi_ai_orchestrator: MICROSTRUCTURE absorption", "PROXY_ONLY"),
    "P07": ("Cross-Venue Lead/Lag", "xv_ret_gap / xv_dislocation",
            "edge_discovery_engine: xv_leadlag_* procedural", "MAPPED"),
    "P08": ("OI/Funding Divergence", "funding-hour behaviour",
            "edge_discovery_engine: funding_hour_fade_* procedural", "MAPPED"),
    "P09": ("Volatility Expansion/Regime Transition", "atr percentile shift",
            "edge_discovery_engine: lowvol_breakout_long", "MAPPED"),
    "P10": ("Mean Reversion (range)", "bollinger / rsi reversion",
            "edge_discovery_engine: bb_touch_* / rsi_mr_* procedural",
            "MAPPED"),
    "P11": ("Crash/Panic SHORT", "high-vol exhaustion short",
            "edge_discovery_engine: highvol_exhaustion_short", "MAPPED"),
    "P12": ("Event/News Shock", "event shock (no free news feed)",
            "NOT_AVAILABLE (no public news feed wired)", "NOT_IMPLEMENTED"),
}

TREND_RIDER_VARIANTS = (
    "trend_static", "trend_adaptive", "trend_with_abstention",
    "trend_no_abstention", "trend_pullback", "trend_breakout",
    "trend_hf", "trend_lf",
)


def _proposal(side, prob, feats, meta, *, symbol, venue, timeframe, event_id,
              cutoff, gen_id, reason_codes, spec_hash, regime,
              expected_win=0.006, expected_loss=0.004, duration_ms=600_000,
              expiry_ms=None) -> dict:
    return C.make(
        "AgentProposal", symbol=symbol, venue=venue, timeframe=timeframe,
        event_id=event_id, causal_cutoff_ms=cutoff, data_generation_id=gen_id,
        spec_hash=spec_hash, agent="TREND_RIDER_24_7", action="PROPOSE",
        side=side, calibrated_probability=round(prob, 6),
        expected_win_pct=expected_win, expected_loss_pct=expected_loss,
        expected_duration_ms=duration_ms, fill_probability=0.9,
        entry_zone={"price": feats.get("close")},
        invalidation={"frac": expected_loss},
        target={"frac": expected_win}, cost_estimate_eur=0.02,
        evidence_ids=[event_id], regime=regime, reason_codes=reason_codes,
        expiry_ms=int(expiry_ms if expiry_ms is not None else cutoff + duration_ms),
        model_version="trend_rider.1")


def _abstain(reason, feats, *, symbol, venue, timeframe, event_id, cutoff,
             gen_id, regime) -> dict:
    return C.make(
        "DecisionRecord", symbol=symbol, venue=venue, timeframe=timeframe,
        event_id=event_id, causal_cutoff_ms=cutoff, data_generation_id=gen_id,
        decision_action=reason, side="FLAT", reason_codes=[reason],
        proposals_for=0, proposals_against=0, calibrated_probability=0.5,
        regime=regime)


def trend_rider(fsnap: dict, *, symbol: str, venue: str, timeframe: str,
                event_id: str, decision_time_ms: int,
                data_generation_id: str | None, variant: str = "trend_adaptive",
                params: dict | None = None) -> dict:
    """TREND_RIDER_24_7: confirmed up-trend -> study LONG; confirmed
    down-trend -> study SHORT; range/exhausted/ambiguous -> ABSTAIN. A single
    rising/falling bar is never enough; multiple confirmations are required."""
    p = {"slope_min": 0.00003, "up_frac_long": 0.55, "up_frac_short": 0.45,
         "move_consumed_max": 0.85, "prob_base": 0.52, **(params or {})}
    feats = fsnap.get("features") or {}
    regime = fsnap.get("regime", "RANGE")
    meta = fsnap.get("feature_meta") or {}
    common = dict(symbol=symbol, venue=venue, timeframe=timeframe,
                  event_id=event_id, cutoff=decision_time_ms,
                  gen_id=data_generation_id, regime=regime)
    if fsnap.get("quality") != "OK":
        return _abstain("ABSTAIN_DATA_QUALITY", feats, **common)
    slope = feats.get("slope", 0.0)
    up_frac = feats.get("up_fraction", 0.5)
    move_consumed = feats.get("move_consumed", 0.5)
    hh, hl = feats.get("higher_high", 0.0), feats.get("higher_low", 0.0)
    ll, lh = feats.get("lower_low", 0.0), feats.get("lower_high", 0.0)
    spec_hash = C.canonical_hash({"strategy": "trend_rider", "variant": variant,
                                  "params": p})
    abst = variant not in ("trend_no_abstention",)
    # a CONFIRMED trend (structure + slope + persistence + regime) is checked
    # FIRST: a trend rider trades continuation/breakout even at new highs.
    long_ok = (slope > p["slope_min"] and up_frac >= p["up_frac_long"]
               and hh and hl and regime == "TREND_UP")
    short_ok = (slope < -p["slope_min"] and up_frac <= p["up_frac_short"]
                and ll and lh and regime == "TREND_DOWN")
    # movement consumed only blocks when there is NO fresh directional
    # structure to ride (e.g. a stalled push with no new HH/HL)
    if abst and not long_ok and not short_ok:
        if move_consumed > p["move_consumed_max"] and slope > 0:
            return _abstain("ABSTAIN_MOVE_CONSUMED", feats, **common)
        if move_consumed < (1 - p["move_consumed_max"]) and slope < 0:
            return _abstain("ABSTAIN_MOVE_CONSUMED", feats, **common)
    if long_ok:
        prob = min(0.85, p["prob_base"] + up_frac * 0.2 + min(abs(slope) * 50, 0.1))
        return _proposal("LONG", prob, feats, meta, spec_hash=spec_hash,
                         reason_codes=["TREND_UP_CONFIRMED", "HH_HL"],
                         regime=regime, **_c(common))
    if short_ok:
        prob = min(0.85, p["prob_base"] + (1 - up_frac) * 0.2 + min(abs(slope) * 50, 0.1))
        return _proposal("SHORT", prob, feats, meta, spec_hash=spec_hash,
                         reason_codes=["TREND_DOWN_CONFIRMED", "LL_LH"],
                         regime=regime, **_c(common))
    if abst and regime in ("RANGE", "HIGH_VOLATILITY"):
        return _abstain("ABSTAIN_REGIME", feats, **common)
    return _abstain("ABSTAIN_LOW_REWARD", feats, **common)


def _c(common: dict) -> dict:
    """Adapt the abstain-common dict to the proposal kwarg names."""
    return {"symbol": common["symbol"], "venue": common["venue"],
            "timeframe": common["timeframe"], "event_id": common["event_id"],
            "cutoff": common["cutoff"], "gen_id": common["gen_id"]}


def paper_long_blocked() -> bool:
    """LONG stays blocked at Paper Champion until it proves OOS edge."""
    return True
