"""ResearchOps V7 — Research-safe duplicate guard.

Deterministic observation fingerprint helper. The guard is **research-only**:

  - never writes to the DB
  - never opens orders
  - never changes runtime config

It exposes:

  - `fingerprint(observation: dict)` → SHA1 hash
  - `is_market_probe(observation: dict)` → bool
  - `is_trade_signal(observation: dict)` → bool
  - `deduplicate(observations)` → list[dict] (in-memory dedupe)
  - `classify_duplicate(prev, curr)` → str

The fingerprint includes (when present): source / symbol / timeframe / side /
strategy_type / market_regime / score bucket / timestamp minute bucket /
reason. Setups that differ in any of those fields are **kept separate** —
the guard never collapses different setups onto the same minute.

`market_probe` rows are kept but flagged `actionable=False`.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


FINAL_RECOMMENDATION = "NO LIVE"

FINGERPRINT_FIELDS_ORDER: tuple[str, ...] = (
    "source",
    "strategy_type",
    "symbol",
    "timeframe",
    "side",
    "market_regime",
    "score_bucket",
    "reject_reason",
    "timestamp_minute_bucket",
    "feature_hash",
)


@dataclass
class GuardVerdict:
    fingerprint: str
    duplicate_class: str   # NEW / EXACT_DUPLICATE / SEMANTIC_DUPLICATE / BENIGN_SCAN_REPEAT
    is_market_probe: bool
    is_trade_signal: bool
    actionable: bool
    reason: str
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _score_bucket(value: Any) -> str:
    try:
        score = int(value)
    except Exception:
        return "unknown"
    if score < 50:
        return "score<50"
    if score < 70:
        return "score_50_70"
    if score < 85:
        return "score_70_85"
    if score < 95:
        return "score_85_95"
    return "score>=95"


def _timestamp_minute_bucket(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "unknown"
    # ISO minute resolution: yyyy-mm-ddThh:mm
    if len(raw) >= 16:
        return raw[:16]
    return raw


def _feature_hash(observation: dict[str, Any]) -> str:
    """Hash of the small set of features that uniquely identify a setup."""
    features = []
    for key in (
        "entry_price", "stop_loss", "take_profit_1", "atr_14",
        "rsi_14", "macd_hist", "volume_relative",
        "distance_to_ema_200", "momentum_5", "momentum_15",
    ):
        try:
            value = observation.get(key)
        except Exception:
            value = None
        features.append(f"{key}={value}")
    payload = "|".join(features).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:16]


def fingerprint(observation: dict[str, Any]) -> str:
    obs = dict(observation or {})
    fields = {
        "source": str(obs.get("source") or obs.get("strategy_type") or "unknown").lower(),
        "strategy_type": str(obs.get("strategy_type") or obs.get("strategy") or "unknown").lower(),
        "symbol": str(obs.get("symbol") or "").upper(),
        "timeframe": str(obs.get("timeframe") or obs.get("tf") or "5m").lower(),
        "side": str(obs.get("side") or "").upper(),
        "market_regime": str(obs.get("market_regime") or obs.get("regime") or "unknown").lower(),
        "score_bucket": _score_bucket(obs.get("confidence_score") or obs.get("score")),
        "reject_reason": str(obs.get("reject_reason") or obs.get("reason") or "").lower()[:32],
        "timestamp_minute_bucket": _timestamp_minute_bucket(obs.get("timestamp")),
        "feature_hash": _feature_hash(obs),
    }
    payload = "|".join(f"{key}={fields[key]}" for key in FINGERPRINT_FIELDS_ORDER).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def is_market_probe(observation: dict[str, Any]) -> bool:
    """A row is a market_probe when its strategy/source explicitly says so."""
    source = str(observation.get("source") or "").lower()
    strategy = str(observation.get("strategy_type") or observation.get("strategy") or "").lower()
    if "market_probe" in source or "market_probe" in strategy:
        return True
    if source in {"probe", "market_probe"} or strategy in {"probe", "market_probe"}:
        return True
    return False


def is_trade_signal(observation: dict[str, Any]) -> bool:
    if is_market_probe(observation):
        return False
    source = str(observation.get("source") or "").lower()
    strategy = str(observation.get("strategy_type") or observation.get("strategy") or "").lower()
    # default to trade_signal when source is empty but strategy is set.
    if source in {"", "trade_signal"} and strategy:
        return True
    if source == "trade_signal":
        return True
    return False


def classify_duplicate(prev: dict[str, Any], curr: dict[str, Any]) -> str:
    if not prev:
        return "NEW"
    if fingerprint(prev) == fingerprint(curr):
        return "EXACT_DUPLICATE"
    prev_minute = _timestamp_minute_bucket(prev.get("timestamp"))
    curr_minute = _timestamp_minute_bucket(curr.get("timestamp"))
    prev_setup = (
        str(prev.get("symbol") or "").upper(),
        str(prev.get("side") or "").upper(),
        str(prev.get("strategy_type") or "").lower(),
        str(prev.get("market_regime") or "").lower(),
    )
    curr_setup = (
        str(curr.get("symbol") or "").upper(),
        str(curr.get("side") or "").upper(),
        str(curr.get("strategy_type") or "").lower(),
        str(curr.get("market_regime") or "").lower(),
    )
    if prev_setup == curr_setup and prev_minute == curr_minute:
        return "BENIGN_SCAN_REPEAT"
    if prev_setup == curr_setup:
        return "SEMANTIC_DUPLICATE"
    return "NEW"


def evaluate(observation: dict[str, Any], *, last_seen: dict[str, Any] | None = None) -> GuardVerdict:
    """Compute a guard verdict for one observation."""
    fp = fingerprint(observation)
    market_probe = is_market_probe(observation)
    trade_signal = is_trade_signal(observation)
    duplicate_class = classify_duplicate(last_seen or {}, observation) if last_seen else "NEW"
    if duplicate_class == "NEW":
        reason = "first_occurrence_in_window"
    elif duplicate_class == "EXACT_DUPLICATE":
        reason = "identical_fingerprint_with_previous_observation"
    elif duplicate_class == "SEMANTIC_DUPLICATE":
        reason = "same_setup_different_minute_dedupe_in_clean_view"
    else:
        reason = "scan_re-evaluated_same_candle_benign"
    actionable = False  # research-only — never actionable.
    return GuardVerdict(
        fingerprint=fp,
        duplicate_class=duplicate_class,
        is_market_probe=market_probe,
        is_trade_signal=trade_signal,
        actionable=actionable,
        reason=reason,
    )


def deduplicate(observations: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """In-memory dedupe — keeps the first occurrence per fingerprint.

    Does NOT modify the input or the DB. Returns a new list with the
    annotated `_guard_*` fields so callers can audit the decision.
    """
    seen: dict[str, dict[str, Any]] = {}
    output: list[dict[str, Any]] = []
    last_seen: dict[str, Any] | None = None
    for observation in observations:
        verdict = evaluate(observation, last_seen=last_seen)
        last_seen = observation
        if verdict.fingerprint in seen:
            continue
        seen[verdict.fingerprint] = observation
        annotated = dict(observation)
        annotated["_guard_fingerprint"] = verdict.fingerprint
        annotated["_guard_duplicate_class"] = verdict.duplicate_class
        annotated["_guard_is_market_probe"] = verdict.is_market_probe
        annotated["_guard_is_trade_signal"] = verdict.is_trade_signal
        annotated["_guard_actionable"] = verdict.actionable
        annotated["_guard_reason"] = verdict.reason
        output.append(annotated)
    return output
