"""V10.46 AI agents: proposal-contract validation + meta-abstention
(RESEARCH ONLY).

AI agents may only PROPOSE / VETO / ABSTAIN / CLOSE through the validated
AgentProposal contract. They can NEVER send an order, promote themselves,
modify the Champion, change sizing, access the holdout or edit running code.
The meta-abstention aggregator turns a set of proposals + critiques into a
single structured DecisionRecord (TRADE or one of the ABSTAIN_* reasons).
"""

from __future__ import annotations

from typing import Any

from . import contracts as C

PROPOSER_AGENTS = ("FlowAgent", "LiquidationAgent", "MomentumAgent",
                   "TrendAgent", "MeanReversionAgent", "CrossVenueAgent",
                   "VolatilityAgent", "CrashAgent", "EventAgent")
CONTROL_AGENTS = ("EntryTimingAgent", "ExitAgent", "RiskAgent",
                  "SkepticAgent", "MetaAbstentionAgent")


def validate_ai_proposal(raw: dict, *, decision_time_ms: int,
                         evidence_times_ms: dict[str, int] | None = None
                         ) -> tuple[bool, list[str]]:
    """Validate an AI-emitted proposal against the contract AND causality.
    Rejects missing fields, uncalibrated probabilities, non-finite numbers,
    free-text where enums are required, expired decisions, and any evidence
    that post-dates the decision time."""
    reasons: list[str] = []
    if not isinstance(raw, dict):
        return False, ["NOT_A_DICT"]
    if raw.get("contract") != "AgentProposal":
        # allow bare dicts by stamping the kind for validation
        raw = {**raw, "contract": "AgentProposal"}
    ok, vr = C.validate(raw)
    reasons.extend(vr)
    exp = raw.get("expiry_ms")
    if isinstance(exp, int) and exp < decision_time_ms:
        reasons.append("EXPIRED")
    okc, bad = C.validate_causal(raw, evidence_times_ms or {})
    if not okc:
        reasons.append("NON_CAUSAL_EVIDENCE:" + ",".join(map(str, bad[:3])))
    return (len(reasons) == 0), reasons


def meta_abstention(proposals: list[dict], *, symbol: str, venue: str,
                    timeframe: str, event_id: str, decision_time_ms: int,
                    data_generation_id: str | None,
                    min_prob: float = 0.55, min_agree: int = 1,
                    cost_estimate_eur: float = 0.02,
                    expected_reward_eur: float = 0.03,
                    regime: str = "ANY") -> dict:
    """Aggregate proposals into ONE DecisionRecord. Abstention is a valid,
    first-class decision with an explicit reason code."""
    valids = [p for p in proposals if p.get("contract") == "AgentProposal"
              and p.get("action") == "PROPOSE"]
    longs = [p for p in valids if p.get("side") == "LONG"]
    shorts = [p for p in valids if p.get("side") == "SHORT"]

    def _dec(action, side, prob, fors, against):
        return C.make("DecisionRecord", symbol=symbol, venue=venue,
                      timeframe=timeframe, event_id=event_id,
                      causal_cutoff_ms=decision_time_ms,
                      data_generation_id=data_generation_id,
                      decision_action=action, side=side,
                      reason_codes=[action], proposals_for=fors,
                      proposals_against=against,
                      calibrated_probability=round(prob, 6), regime=regime)

    if not valids:
        return _dec("ABSTAIN_LOW_REWARD", "FLAT", 0.5, 0, 0)
    # data quality gate
    if any(p.get("quality") == "INSUFFICIENT_HISTORY" for p in valids):
        return _dec("ABSTAIN_DATA_QUALITY", "FLAT", 0.5, 0, len(valids))
    # disagreement gate
    if longs and shorts:
        return _dec("ABSTAIN_DISAGREEMENT", "FLAT", 0.5, len(longs),
                    len(shorts))
    side_group = longs or shorts
    side = "LONG" if longs else "SHORT"
    if len(side_group) < min_agree:
        return _dec("ABSTAIN_UNCERTAINTY", "FLAT", 0.5, len(side_group), 0)
    best_prob = max(p["calibrated_probability"] for p in side_group)
    if best_prob < min_prob:
        return _dec("ABSTAIN_UNCERTAINTY", "FLAT", best_prob, len(side_group), 0)
    # reward-vs-cost gate
    if expected_reward_eur <= cost_estimate_eur:
        return _dec("ABSTAIN_COST", "FLAT", best_prob, len(side_group), 0)
    if regime in ("HIGH_VOLATILITY",) and best_prob < min_prob + 0.1:
        return _dec("ABSTAIN_REGIME", "FLAT", best_prob, len(side_group), 0)
    return _dec("TRADE", side, best_prob, len(side_group),
                len(valids) - len(side_group))
