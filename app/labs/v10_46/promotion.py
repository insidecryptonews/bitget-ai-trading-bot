"""V10.46 deterministic Promotion Controller (RESEARCH ONLY).

Advances a policy along:
  REPLAY_CANDIDATE -> SHADOW_CANDIDATE -> VALIDATED_SHADOW
  -> PAPER_CHALLENGER -> PAPER_CHAMPION -> LIVE_READINESS_ONLY

LIVE is NEVER an executable state. Reaching LIVE_READINESS_ONLY only produces a
readiness REPORT; it requires an independent audit reference and is not an
authorisation to trade. No AI can promote; only this deterministic controller.
"""

from __future__ import annotations

from typing import Any

from . import contracts as C

STATES = C.PROMOTION_STATES

# gate thresholds (euro-first; deliberately strict)
GATES = {
    "min_clusters": 30,
    "min_n_eff": 30,
    "min_net_pnl_eur": 0.0,      # must be net positive
    "min_paired_lb_eur": 0.0,    # beats champion out-of-sample (paired lb > 0)
    "max_drawdown_eur": -1.0,    # tolerate up to 1 EUR dd on 5 EUR exposure
    "max_brier": 0.30,
    "min_win_rate": 0.0,
    "require_beats_no_trade": True,
    "require_beats_random": True,
    "require_top3_robust": True,  # net without top-3 events still >= 0
}


def evaluate_gates(metrics: dict, *, paired_lb_eur: float | None,
                   no_trade_net: float, random_net: float,
                   dataset_verified: bool, registry_closed: bool,
                   holdout_single_use_ok: bool) -> dict:
    """Return every gate result; a policy only advances when ALL pass."""
    g: dict[str, bool] = {}
    g["dataset_verified"] = bool(dataset_verified)
    g["registry_closed"] = bool(registry_closed)
    g["holdout_single_use"] = bool(holdout_single_use_ok)
    g["clusters"] = metrics.get("clusters", 0) >= GATES["min_clusters"]
    g["n_eff"] = metrics.get("n_eff", 0) >= GATES["min_n_eff"]
    g["net_positive"] = metrics.get("net_pnl_eur", -1) > GATES["min_net_pnl_eur"]
    g["drawdown"] = metrics.get("max_drawdown_eur", -9) >= GATES["max_drawdown_eur"]
    br = metrics.get("brier")
    g["calibration"] = (br is None) or (br <= GATES["max_brier"])
    g["beats_no_trade"] = metrics.get("net_pnl_eur", -1) > no_trade_net \
        if GATES["require_beats_no_trade"] else True
    g["beats_random"] = metrics.get("net_pnl_eur", -1) > random_net \
        if GATES["require_beats_random"] else True
    g["paired_beats_champion"] = (paired_lb_eur is not None
                                  and paired_lb_eur > GATES["min_paired_lb_eur"])
    g["top3_robust"] = (metrics.get("net_without_top3_eur", -1) >= 0) \
        if GATES["require_top3_robust"] else True
    g["all_pass"] = all(v for k, v in g.items() if k != "all_pass")
    return g


def promotion_decision(policy_id: str, from_state: str, metrics: dict, *,
                       symbol: str, venue: str, timeframe: str,
                       event_id: str, decision_time_ms: int,
                       data_generation_id: str | None,
                       paired_lb_eur: float | None, no_trade_net: float,
                       random_net: float, dataset_verified: bool,
                       registry_closed: bool, holdout_single_use_ok: bool,
                       independent_audit_ref: str | None = None) -> dict:
    """Deterministic promote/hold/reject. Promotion to LIVE_READINESS_ONLY
    additionally requires an independent audit reference — and is still NOT an
    authorisation to trade live."""
    gates = evaluate_gates(metrics, paired_lb_eur=paired_lb_eur,
                           no_trade_net=no_trade_net, random_net=random_net,
                           dataset_verified=dataset_verified,
                           registry_closed=registry_closed,
                           holdout_single_use_ok=holdout_single_use_ok)
    idx = STATES.index(from_state)
    to_state = STATES[min(idx + 1, len(STATES) - 1)]
    decision = "PROMOTE" if gates["all_pass"] else "HOLD"
    extra = {}
    if to_state == "LIVE_READINESS_ONLY" and decision == "PROMOTE":
        if not independent_audit_ref:
            decision = "HOLD"                 # readiness report needs an audit
        else:
            extra["independent_audit_ref"] = independent_audit_ref
    return C.make("PromotionDecision", symbol=symbol, venue=venue,
                  timeframe=timeframe, event_id=event_id,
                  causal_cutoff_ms=decision_time_ms,
                  data_generation_id=data_generation_id, policy_id=policy_id,
                  from_state=from_state,
                  to_state=to_state if decision == "PROMOTE" else from_state,
                  decision=decision, gate_results=gates,
                  can_send_real_orders=False, live_trading=False,
                  final_recommendation="NO LIVE", **extra)
