"""Deterministic ATI V2 setup detection and componentized scoring."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Callable

import pandas as pd

from . import FEATURE_VERSION, POLICY_VERSION
from .features import feature_row_is_finite
from .levels import CausalLevelEngine, LevelSnapshot, level_snapshot


@dataclass(frozen=True)
class AtiCandidate:
    signal_id: str
    setup_id: str
    setup_variant: str
    symbol: str
    signal_idx: int
    decision_ts: str
    entry_ts: str | None
    entry_price: float | None
    direction: str
    decision: str
    exact_trigger: bool
    ati_score: int
    score_components: dict[str, int]
    trigger_components: dict[str, bool]
    support_level: float | None
    resistance_level: float | None
    invalidation_level: float | None
    timeframe_context: dict[str, Any]
    regime: str
    atr15: float
    dataset_source: str
    feature_version: str = FEATURE_VERSION
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _signal_id(symbol: str, setup: str, variant: str, decision_ts: str) -> str:
    raw = f"{POLICY_VERSION}|{symbol}|{setup}|{variant}|{decision_ts}"
    return "ati_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _context(row: pd.Series) -> dict[str, Any]:
    keys = (
        "h1_regime", "h4_regime", "h1_volatility_regime", "h4_volatility_regime",
        "h1_close", "h4_close", "h1_ema20", "h1_ema50", "h4_ema20", "h4_ema50",
    )
    return {key: (None if pd.isna(row.get(key)) else row.get(key)) for key in keys}


def _finite_or(value: Any, fallback: float) -> float:
    """Return a numeric HTF value only when it is genuinely available."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return numeric if math.isfinite(numeric) else float(fallback)


def _score(*, direction: str, exact_trigger: bool, repeated_level: bool,
           strong_body: bool, retest_holds: bool, row: pd.Series,
           ambiguous: bool, opposing_level_close: bool) -> tuple[int, dict[str, int]]:
    aligned = "TREND_UP" if direction == "LONG" else "TREND_DOWN"
    opposed = "TREND_DOWN" if direction == "LONG" else "TREND_UP"
    h1 = str(row.get("h1_regime") or "")
    h4 = str(row.get("h4_regime") or "")
    high_vol_without_structure = (
        str(row.get("volatility_regime") or "") == "HIGH_VOL" and not repeated_level
    )
    components = {
        "4h_direction_alignment": 1 if h4 == aligned else 0,
        "1h_direction_alignment": 1 if h1 == aligned else 0,
        "15m_exact_trigger": 2 if exact_trigger else 0,
        "repeated_level_test": 1 if repeated_level else 0,
        "strong_body_in_signal_direction": 1 if strong_body else 0,
        "retest_holds": 1 if retest_holds else 0,
        "timeframe_conflict": -2 if (h1 == opposed or h4 == opposed) else 0,
        "first_breakout_without_hold_or_retest": -2 if ambiguous and not retest_holds else 0,
        "trigger_is_vague": -2 if ambiguous else 0,
        "nearest_opposing_level_too_close": -1 if opposing_level_close else 0,
        "high_volatility_without_structure": -1 if high_vol_without_structure else 0,
    }
    return sum(components.values()), components


def _decision(score: int, *, exact_trigger: bool, has_next_bar: bool) -> str:
    if not has_next_bar:
        return "WAIT_NEXT_BAR"
    if exact_trigger and score >= 5:
        return "SHADOW_CANDIDATE"
    if score >= 3:
        return "WAIT"
    return "REJECT"


def _entry_risk_rejection(*, direction: str, entry: float | None,
                          invalidation: float | None) -> str | None:
    if entry is None or invalidation is None or not all(
        math.isfinite(value) and value > 0 for value in (entry, invalidation)
    ):
        return "REJECT_INVALID_STRUCTURAL_RISK"
    if direction == "LONG":
        if invalidation >= entry:
            return "REJECT_INVALIDATED_BEFORE_ENTRY"
        risk = entry - invalidation
    else:
        if invalidation <= entry:
            return "REJECT_INVALIDATED_BEFORE_ENTRY"
        risk = invalidation - entry
    risk_fraction = risk / entry
    if risk_fraction < 0.0002 or risk_fraction > 0.05:
        return "REJECT_INVALID_STRUCTURAL_RISK"
    return None


def _candidate(*, frame: pd.DataFrame, idx: int, symbol: str, setup: str,
               variant: str, direction: str, exact: bool,
               trigger_components: dict[str, bool], repeated: bool,
               strong: bool, retest: bool, ambiguous: bool,
               snapshot: LevelSnapshot, invalidation: float | None,
               dataset_source: str) -> AtiCandidate:
    row = frame.iloc[idx]
    opposing = snapshot.support if direction == "SHORT" else snapshot.resistance
    opposing_close = bool(
        opposing is not None
        and abs(float(row["close"]) - opposing.price) <= max(snapshot.tolerance, float(row["atr14"]) * 0.5)
    )
    score, components = _score(
        direction=direction, exact_trigger=exact, repeated_level=repeated,
        strong_body=strong, retest_holds=retest, row=row, ambiguous=ambiguous,
        opposing_level_close=opposing_close,
    )
    decision_ts = pd.Timestamp(row["available_at"]).isoformat()
    has_next = idx + 1 < len(frame)
    entry_ts = pd.Timestamp(frame.iloc[idx + 1]["timestamp"]).isoformat() if has_next else None
    entry_price = float(frame.iloc[idx + 1]["open"]) if has_next else None
    decision = _decision(score, exact_trigger=exact, has_next_bar=has_next)
    if decision == "SHADOW_CANDIDATE":
        decision = _entry_risk_rejection(
            direction=direction, entry=entry_price, invalidation=invalidation,
        ) or decision
    return AtiCandidate(
        signal_id=_signal_id(symbol, setup, variant, decision_ts),
        setup_id=setup,
        setup_variant=variant,
        symbol=symbol,
        signal_idx=idx,
        decision_ts=decision_ts,
        entry_ts=entry_ts,
        entry_price=entry_price,
        direction=direction,
        decision=decision,
        exact_trigger=exact,
        ati_score=score,
        score_components=components,
        trigger_components=trigger_components,
        support_level=(snapshot.support.price if snapshot.support else None),
        resistance_level=(snapshot.resistance.price if snapshot.resistance else None),
        invalidation_level=invalidation,
        timeframe_context=_context(row),
        regime=str(row.get("h1_regime") or row.get("regime") or "UNKNOWN"),
        atr15=float(row["atr14"]),
        dataset_source=dataset_source,
    )


def evaluate_rules_at(
    frame: pd.DataFrame,
    idx: int,
    *,
    symbol: str,
    dataset_source: str,
    snapshot_fn: Callable[[pd.DataFrame, int], LevelSnapshot] = level_snapshot,
) -> list[AtiCandidate]:
    if idx < 4 or idx >= len(frame) or not bool(frame.iloc[idx].get("feature_ready")):
        return []
    row, previous = frame.iloc[idx], frame.iloc[idx - 1]
    if not feature_row_is_finite(row):
        return []
    current = snapshot_fn(frame, idx)
    before_one = snapshot_fn(frame, idx - 1)
    before_two = snapshot_fn(frame, idx - 2)
    before_three = snapshot_fn(frame, idx - 3)
    candidates: list[AtiCandidate] = []
    bearish = float(row["close"]) < float(row["open"])
    bullish = float(row["close"]) > float(row["open"])
    strong = float(row["body_strength"]) >= 0.55

    # SHORT_R1: current closed bar rejects a repeatedly tested resistance.
    resistance = current.resistance
    if resistance and float(row["high"]) >= resistance.price - current.tolerance:
        parts = {
            "resistance_valid": resistance.touch_count >= 2,
            "price_tests_resistance": float(row["high"]) <= resistance.price + current.tolerance * 2,
            "bearish_signal_bar": bearish,
            "strong_body": strong,
            "closes_below_previous_low": float(row["close"]) < float(previous["low"]),
            "h4_not_strong_up": str(row.get("h4_regime")) != "TREND_UP",
            "h1_not_holding_above_resistance": _finite_or(row.get("h1_close"), row["close"]) <= resistance.price + current.break_buffer,
        }
        exact = all(parts.values())
        candidates.append(_candidate(
            frame=frame, idx=idx, symbol=symbol, setup="SHORT_R1", variant="REJECTION",
            direction="SHORT", exact=exact, trigger_components=parts,
            repeated=True, strong=bearish and strong, retest=False,
            ambiguous=not exact, snapshot=current,
            invalidation=resistance.price + current.break_buffer,
            dataset_source=dataset_source,
        ))

    # SHORT_S1: breakout happened on idx-1; idx confirms no recovery.
    support = before_two.support
    if support and float(previous["close"]) <= support.price + before_two.tolerance:
        parts = {
            "support_valid": support.touch_count >= 2,
            "support_fatigued": support.fatigue,
            "previous_close_breaks_support": float(previous["close"]) < support.price - before_two.break_buffer,
            "confirmation_does_not_recover": float(row["close"]) < support.price,
        }
        exact = all(parts.values())
        candidates.append(_candidate(
            frame=frame, idx=idx, symbol=symbol, setup="SHORT_S1", variant="NON_RECOVERY",
            direction="SHORT", exact=exact, trigger_components=parts,
            repeated=True, strong=bearish and strong, retest=False,
            ambiguous=not exact, snapshot=before_two,
            invalidation=support.price + before_two.break_buffer,
            dataset_source=dataset_source,
        ))

    # LONG_R1: breakout on idx-1 is never entered directly; idx must hold/retest.
    resistance = before_two.resistance
    if resistance and float(previous["close"]) >= resistance.price - before_two.tolerance:
        retest = float(row["low"]) <= resistance.price + before_two.tolerance
        variant = "RETEST" if retest else "HOLD"
        parts = {
            "previous_close_breaks_resistance": float(previous["close"]) > resistance.price + before_two.break_buffer,
            "confirmation_closes_above": float(row["close"]) > resistance.price,
            "hold_or_retest_defended": (float(row["low"]) > resistance.price if not retest else float(row["close"]) > resistance.price),
            "h1_not_strong_down": str(row.get("h1_regime")) != "TREND_DOWN",
        }
        exact = all(parts.values())
        candidates.append(_candidate(
            frame=frame, idx=idx, symbol=symbol, setup="LONG_R1", variant=variant,
            direction="LONG", exact=exact, trigger_components=parts,
            repeated=True, strong=bullish and strong, retest=retest and exact,
            ambiguous=not exact, snapshot=before_two,
            invalidation=resistance.price - before_two.break_buffer,
            dataset_source=dataset_source,
        ))

    # LONG_S1: support defence requires two non-lower lows and a reclaim close.
    support = before_three.support
    if support:
        row_m2 = frame.iloc[idx - 2]
        touched = min(float(row_m2["low"]), float(previous["low"]), float(row["low"])) <= support.price + before_three.tolerance
        if touched:
            parts = {
                "support_valid": support.touch_count >= 2,
                "support_touched": touched,
                "two_bars_without_new_low": float(previous["low"]) >= float(row_m2["low"]) and float(row["low"]) >= float(previous["low"]),
                "closes_above_previous_high": float(row["close"]) > float(previous["high"]),
                "h1_not_closed_below_support": _finite_or(row.get("h1_close"), row["close"]) >= support.price,
            }
            exact = all(parts.values())
            candidates.append(_candidate(
                frame=frame, idx=idx, symbol=symbol, setup="LONG_S1", variant="SUPPORT_DEFENCE",
                direction="LONG", exact=exact, trigger_components=parts,
                repeated=True, strong=bullish and strong, retest=True,
                ambiguous=not exact, snapshot=before_three,
                invalidation=support.price - before_three.break_buffer,
                dataset_source=dataset_source,
            ))
    return candidates


def generate_candidates(frame: pd.DataFrame, *, symbol: str,
                        dataset_source: str = "validated_snapshot") -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    level_engine = CausalLevelEngine(frame)
    for idx in range(len(frame)):
        candidates.extend(
            candidate.to_dict()
            for candidate in evaluate_rules_at(
                frame, idx, symbol=symbol, dataset_source=dataset_source,
                snapshot_fn=level_engine.snapshot,
            )
        )
    # A deterministic uniqueness guard prevents restart duplication.
    unique: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        canonical = json.dumps(candidate, sort_keys=True, default=str)
        existing = unique.get(candidate["signal_id"])
        if existing is not None and json.dumps(existing, sort_keys=True, default=str) != canonical:
            raise ValueError("ATI_SIGNAL_ID_COLLISION")
        unique[candidate["signal_id"]] = candidate
    return sorted(unique.values(), key=lambda item: (item["decision_ts"], item["signal_id"]))
