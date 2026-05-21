"""Canonical setup key for grouping signals/labels/outcomes consistently.

A setup is the tuple
    (symbol, side, regime, score_bucket, timeframe, strategy, exit_policy, source)

Different consumers (CandidateIncubator, SignalOutcomeClassifier, ShadowMonitor,
QuickProfitExitLab, MomentumBurstLab) must agree on this key to make grouped
statistics comparable across modules.

This module is pure helpers — no DB, no runtime side effects, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


SCORE_BUCKETS = (
    (0, 49, "0-49"),
    (50, 69, "50-69"),
    (70, 74, "70-74"),
    (75, 79, "75-79"),
    (80, 84, "80-84"),
    (85, 89, "85-89"),
    (90, 94, "90-94"),
    (95, 100, "95-100"),
)


VALID_SIDES = {"LONG", "SHORT", "NO_TRADE"}
VALID_SOURCES = {"trade_signal", "shadow_signal", "market_probe", "unknown"}
DEFAULT_EXIT_POLICY = "current_exit"


@dataclass(frozen=True)
class SetupKey:
    """Canonical setup identifier — hashable and serializable."""

    symbol: str
    side: str
    regime: str
    score_bucket: str
    timeframe: str
    strategy: str
    exit_policy: str
    source: str

    def as_tuple(self) -> tuple[str, ...]:
        return (
            self.symbol,
            self.side,
            self.regime,
            self.score_bucket,
            self.timeframe,
            self.strategy,
            self.exit_policy,
            self.source,
        )

    def as_dict(self) -> dict[str, str]:
        return asdict(self)

    def as_string(self) -> str:
        """Stable string representation for logs/reports."""
        return (
            f"{self.symbol}|{self.side}|{self.regime}|{self.score_bucket}|"
            f"{self.timeframe}|{self.strategy}|{self.exit_policy}|{self.source}"
        )

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.as_string()


def score_bucket(score: Any) -> str:
    """Map a numeric confidence_score to its canonical bucket label."""
    try:
        value = int(score)
    except (TypeError, ValueError):
        return "NA"
    for lo, hi, label in SCORE_BUCKETS:
        if lo <= value <= hi:
            return label
    if value < 0:
        return "0-49"
    return "95-100"


def normalize_symbol(symbol: Any) -> str:
    return str(symbol or "").upper().strip()


def normalize_side(side: Any) -> str:
    text = str(side or "").upper().strip()
    return text if text in VALID_SIDES else "NO_TRADE"


def normalize_regime(regime: Any) -> str:
    return str(regime or "UNKNOWN").upper().strip() or "UNKNOWN"


def normalize_timeframe(tf: Any) -> str:
    return str(tf or "").lower().strip() or "unknown"


def normalize_strategy(strategy: Any) -> str:
    return str(strategy or "unknown").upper().strip() or "UNKNOWN"


def normalize_exit_policy(policy: Any) -> str:
    return str(policy or DEFAULT_EXIT_POLICY).lower().strip() or DEFAULT_EXIT_POLICY


def normalize_source(source: Any) -> str:
    text = str(source or "unknown").lower().strip()
    return text if text in VALID_SOURCES else "unknown"


def build_setup_key(
    *,
    symbol: Any,
    side: Any,
    regime: Any,
    score: Any | None = None,
    score_bucket_label: str | None = None,
    timeframe: Any,
    strategy: Any = "unknown",
    exit_policy: Any = DEFAULT_EXIT_POLICY,
    source: Any = "trade_signal",
) -> SetupKey:
    """Build a canonical SetupKey from raw fields.

    Either `score` or `score_bucket_label` must be provided; `score` wins if both.
    """
    if score is not None:
        bucket = score_bucket(score)
    else:
        bucket = score_bucket_label or "NA"
    return SetupKey(
        symbol=normalize_symbol(symbol),
        side=normalize_side(side),
        regime=normalize_regime(regime),
        score_bucket=bucket,
        timeframe=normalize_timeframe(timeframe),
        strategy=normalize_strategy(strategy),
        exit_policy=normalize_exit_policy(exit_policy),
        source=normalize_source(source),
    )


def setup_key_from_observation(observation: dict[str, Any], *, timeframe: str = "5m", exit_policy: str = DEFAULT_EXIT_POLICY) -> SetupKey:
    """Convenience wrapper to derive a SetupKey from a signal_observation row."""
    return build_setup_key(
        symbol=observation.get("symbol"),
        side=observation.get("side"),
        regime=observation.get("market_regime") or observation.get("regime"),
        score=observation.get("confidence_score"),
        score_bucket_label=observation.get("score_bucket"),
        timeframe=observation.get("timeframe") or timeframe,
        strategy=observation.get("strategy_type") or observation.get("strategy"),
        exit_policy=observation.get("exit_policy") or exit_policy,
        source=observation.get("source") or "trade_signal",
    )
