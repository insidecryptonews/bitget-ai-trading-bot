"""V10.46 canonical, versioned, validated contracts (RESEARCH ONLY).

Every message that crosses a component boundary is one of these contracts.
They are plain dicts (stdlib only, JSON-serialisable) built and validated
through this module so that:

  * every record carries the common provenance block (schema_version,
    created_at, event_id, event_cluster_id, symbol, venue, timeframe,
    data_generation_id, repo_commit, policy_version, spec_hash,
    causal_cutoff_ms);
  * timestamps are UTC epoch milliseconds (never naive/zoneless);
  * decisions are structured enums + reason codes (never free text);
  * IDs are never silently optional — a missing id must carry an explicit
    *_status = NOT_APPLICABLE;
  * NO feature/evidence may reference information after causal_cutoff_ms
    (enforced by validate_causal()).

Nothing here can place an order or touch a private endpoint.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "v10.46.contracts.1"

SIDES = ("LONG", "SHORT", "FLAT")
AGENT_ACTIONS = ("PROPOSE", "VETO", "ABSTAIN", "CLOSE")
ABSTAIN_REASONS = ("ABSTAIN_UNCERTAINTY", "ABSTAIN_COST", "ABSTAIN_REGIME",
                   "ABSTAIN_DISAGREEMENT", "ABSTAIN_MOVE_CONSUMED",
                   "ABSTAIN_LOW_REWARD", "ABSTAIN_DATA_QUALITY")
DECISION_ACTIONS = ("TRADE",) + ABSTAIN_REASONS

CONTRACT_KINDS = (
    "MarketEvent", "MarketSnapshot", "FeatureSnapshot", "AgentProposal",
    "AgentCritique", "DecisionRecord", "SimOrder", "SimFill", "PositionEvent",
    "ExitDecision", "TradeAutopsy", "ExperienceRecord", "PolicyLineage",
    "TournamentResult", "PromotionDecision",
)

_COMMON = ("schema_version", "created_at_ms", "event_id", "event_cluster_id",
           "symbol", "venue", "timeframe", "data_generation_id",
           "repo_commit", "policy_version", "spec_hash", "causal_cutoff_ms")

# kind-specific REQUIRED fields (beyond the common block)
_REQUIRED: dict[str, tuple] = {
    "MarketEvent": ("event_type", "ts_ms", "payload"),
    "MarketSnapshot": ("ts_ms", "mid", "bid", "ask"),
    "FeatureSnapshot": ("decision_time_ms", "features", "feature_meta"),
    "AgentProposal": ("agent", "action", "side", "calibrated_probability",
                      "expected_win_pct", "expected_loss_pct",
                      "expected_duration_ms", "fill_probability",
                      "invalidation", "target", "cost_estimate_eur",
                      "evidence_ids", "regime", "reason_codes", "expiry_ms",
                      "model_version"),
    "AgentCritique": ("agent", "target_spec_hash", "verdict", "reason_codes"),
    "DecisionRecord": ("decision_action", "side", "reason_codes",
                       "proposals_for", "proposals_against",
                       "calibrated_probability"),
    "SimOrder": ("order_type", "side", "qty", "limit_price", "ts_ms",
                 "scenario"),
    "SimFill": ("order_ref", "fill_price", "fill_qty", "ts_ms", "fee_eur",
                "slippage_eur", "fill_status"),
    "PositionEvent": ("event", "ts_ms", "side", "qty", "price"),
    "ExitDecision": ("exit_action", "reason_codes", "ts_ms"),
    "TradeAutopsy": ("trade_id", "before", "during", "after"),
    "ExperienceRecord": ("trade_id", "features", "label", "bucket"),
    "PolicyLineage": ("policy_id", "parent_policy_id", "mutation",
                      "mutation_reason"),
    "TournamentResult": ("participant_id", "paired_key", "metrics"),
    "PromotionDecision": ("policy_id", "from_state", "to_state", "decision",
                          "gate_results"),
}

PROMOTION_STATES = ("REPLAY_CANDIDATE", "SHADOW_CANDIDATE", "VALIDATED_SHADOW",
                    "PAPER_CHALLENGER", "PAPER_CHAMPION", "LIVE_READINESS_ONLY")


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).isoformat()


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True,
                                     separators=(",", ":"),
                                     default=str).encode("utf-8")).hexdigest()


def _finite(x) -> bool:
    if isinstance(x, bool):
        return True
    if isinstance(x, (int, float)):
        return x == x and x not in (float("inf"), float("-inf"))
    return True


def make(kind: str, *, symbol: str, venue: str, timeframe: str,
         event_id: str, causal_cutoff_ms: int,
         event_cluster_id: str | None = None,
         data_generation_id: str | None = None,
         repo_commit: str | None = None,
         policy_version: str = "v10.46",
         spec_hash: str | None = None,
         created_at_ms: int | None = None,
         **fields) -> dict:
    """Build a contract record stamped with the common provenance block.
    Raises ValueError if the kind is unknown or a required field is missing."""
    if kind not in CONTRACT_KINDS:
        raise ValueError(f"unknown contract kind: {kind}")
    rec = {
        "contract": kind,
        "schema_version": SCHEMA_VERSION,
        "created_at_ms": int(created_at_ms if created_at_ms is not None
                             else now_ms()),
        "event_id": str(event_id),
        "event_cluster_id": (event_cluster_id if event_cluster_id is not None
                             else str(event_id)),
        "symbol": symbol, "venue": venue, "timeframe": timeframe,
        "data_generation_id": data_generation_id,
        "data_generation_status": ("PRESENT" if data_generation_id
                                   else "NOT_APPLICABLE"),
        "repo_commit": repo_commit,
        "policy_version": policy_version,
        "spec_hash": spec_hash,
        "spec_hash_status": "PRESENT" if spec_hash else "NOT_APPLICABLE",
        "causal_cutoff_ms": int(causal_cutoff_ms),
        **fields}
    ok, reasons = validate(rec)
    if not ok:
        raise ValueError(f"invalid {kind}: {reasons}")
    return rec


def validate(rec: dict) -> tuple[bool, list[str]]:
    """Structural validation: common block present, required fields present,
    timestamps are int epoch-ms, all numbers finite, enums respected, and no
    free-text where an enum is required."""
    reasons: list[str] = []
    kind = rec.get("contract")
    if kind not in CONTRACT_KINDS:
        return False, ["UNKNOWN_KIND"]
    for f in _COMMON:
        if f not in rec:
            reasons.append(f"MISSING_COMMON:{f}")
    for f in _REQUIRED.get(kind, ()):
        if f not in rec:
            reasons.append(f"MISSING_REQUIRED:{f}")
    for tk in ("created_at_ms", "causal_cutoff_ms", "ts_ms",
               "decision_time_ms", "expiry_ms", "expected_duration_ms"):
        if tk in rec and rec[tk] is not None and not isinstance(rec[tk], int):
            reasons.append(f"TIMESTAMP_NOT_EPOCH_MS:{tk}")
    for k, v in rec.items():
        if not _finite(v):
            reasons.append(f"NON_FINITE:{k}")
    if kind == "AgentProposal":
        if rec.get("action") not in AGENT_ACTIONS:
            reasons.append("BAD_ACTION")
        if rec.get("side") not in SIDES:
            reasons.append("BAD_SIDE")
        p = rec.get("calibrated_probability")
        if not isinstance(p, (int, float)) or not (0.0 <= p <= 1.0):
            reasons.append("PROB_NOT_CALIBRATED")
        if not isinstance(rec.get("evidence_ids"), list):
            reasons.append("EVIDENCE_NOT_LIST")
        if not isinstance(rec.get("reason_codes"), list) \
                or any(not isinstance(x, str) for x in rec.get("reason_codes", [])):
            reasons.append("REASON_CODES_NOT_ENUM_LIST")
    if kind == "DecisionRecord":
        if rec.get("decision_action") not in DECISION_ACTIONS:
            reasons.append("BAD_DECISION_ACTION")
        if rec.get("side") not in SIDES:
            reasons.append("BAD_SIDE")
    if kind == "PromotionDecision":
        if rec.get("from_state") not in PROMOTION_STATES \
                or rec.get("to_state") not in PROMOTION_STATES:
            reasons.append("BAD_PROMOTION_STATE")
        if rec.get("to_state") == "LIVE_READINESS_ONLY" \
                and rec.get("decision") == "PROMOTE" \
                and not rec.get("independent_audit_ref"):
            reasons.append("LIVE_READINESS_WITHOUT_AUDIT")
    return (len(reasons) == 0), reasons


def validate_causal(rec: dict, evidence_times_ms: dict[str, int]) -> tuple[bool, list[str]]:
    """No evidence referenced by the record may post-date causal_cutoff_ms:
    a feature/proposal that used future information is rejected."""
    cutoff = rec.get("causal_cutoff_ms")
    bad = []
    for eid in rec.get("evidence_ids", []) or []:
        t = evidence_times_ms.get(eid)
        if t is not None and t > cutoff:
            bad.append(eid)
    fm = rec.get("feature_meta") or {}
    for name, meta in fm.items():
        at = (meta or {}).get("available_time_ms")
        if at is not None and at > cutoff:
            bad.append(f"feature:{name}")
    return (len(bad) == 0), bad
