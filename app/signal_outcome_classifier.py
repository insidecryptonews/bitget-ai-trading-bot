"""Signal Outcome Classifier — Learning Memory foundation.

Reads existing signal_observations + signal_labels + signal_path_metrics rows
and classifies each into an outcome_class that points to a CONCRETE action
the bot could take to improve.

This module is READ-ONLY over current tables. It can optionally write
classifications to a new `signal_outcomes` table for persistence and reports.

Classification taxonomy
-----------------------
For TAKEN signals (operated=1 OR has a label):
- CLEAN_WIN           : TP hit; net positive comfortably
- CLEAN_LOSS          : SL hit; loss within risk
- TIME_NEUTRAL        : TIME exit; |realized| < cost
- PRECOCIOUS_EXIT     : TP/TIME early but price kept moving favorable (MFE >> realized)
- LATE_EXIT           : MFE peaked early then reverted, exit at low realized
- STOP_TOO_TIGHT      : SL hit then price recovered favourably
- STOP_TOO_LOOSE      : SL hit at large negative realized vs ATR
- TP_TOO_CLOSE        : TP hit then price kept extending strongly
- TP_TOO_FAR          : TIME exit with MFE close to TP1 but not hitting
- FEE_TOXIC           : gross positive, net negative
- AMBIGUOUS           : insufficient info

For REJECTED signals (operated=0 with a hypothetical label):
- MISSED_WINNER       : would-have label = +1 with sample net positive
- AVOIDED_LOSER       : would-have label = -1
- AMBIGUOUS_REJECTION : insufficient info

For NO_TRADE signals (side == NO_TRADE):
- NO_TRADE_OK         : side intentionally NO_TRADE, no further analysis

This module DOES NOT change anything in runtime. It just classifies.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Iterable

from .setup_key import build_setup_key, score_bucket
from .utils import safe_float, safe_int


OUTCOME_CLEAN_WIN = "CLEAN_WIN"
OUTCOME_CLEAN_LOSS = "CLEAN_LOSS"
OUTCOME_TIME_NEUTRAL = "TIME_NEUTRAL"
OUTCOME_PRECOCIOUS_EXIT = "PRECOCIOUS_EXIT"
OUTCOME_LATE_EXIT = "LATE_EXIT"
OUTCOME_STOP_TOO_TIGHT = "STOP_TOO_TIGHT"
OUTCOME_STOP_TOO_LOOSE = "STOP_TOO_LOOSE"
OUTCOME_TP_TOO_CLOSE = "TP_TOO_CLOSE"
OUTCOME_TP_TOO_FAR = "TP_TOO_FAR"
OUTCOME_FEE_TOXIC = "FEE_TOXIC"
OUTCOME_MISSED_WINNER = "MISSED_WINNER"
OUTCOME_AVOIDED_LOSER = "AVOIDED_LOSER"
OUTCOME_AMBIGUOUS = "AMBIGUOUS"
OUTCOME_AMBIGUOUS_REJECTION = "AMBIGUOUS_REJECTION"
OUTCOME_NO_TRADE_OK = "NO_TRADE_OK"

ALL_OUTCOMES = {
    OUTCOME_CLEAN_WIN,
    OUTCOME_CLEAN_LOSS,
    OUTCOME_TIME_NEUTRAL,
    OUTCOME_PRECOCIOUS_EXIT,
    OUTCOME_LATE_EXIT,
    OUTCOME_STOP_TOO_TIGHT,
    OUTCOME_STOP_TOO_LOOSE,
    OUTCOME_TP_TOO_CLOSE,
    OUTCOME_TP_TOO_FAR,
    OUTCOME_FEE_TOXIC,
    OUTCOME_MISSED_WINNER,
    OUTCOME_AVOIDED_LOSER,
    OUTCOME_AMBIGUOUS,
    OUTCOME_AMBIGUOUS_REJECTION,
    OUTCOME_NO_TRADE_OK,
}


SUGGESTED_FIX = {
    OUTCOME_CLEAN_WIN: "none",
    OUTCOME_CLEAN_LOSS: "none",
    OUTCOME_TIME_NEUTRAL: "review_tp_distance_or_time_horizon",
    OUTCOME_PRECOCIOUS_EXIT: "consider_trailing_stop",
    OUTCOME_LATE_EXIT: "consider_quick_profit_exit",
    OUTCOME_STOP_TOO_TIGHT: "widen_stop_or_swing_stop",
    OUTCOME_STOP_TOO_LOOSE: "tighten_stop_or_lower_size",
    OUTCOME_TP_TOO_CLOSE: "extend_tp_or_partial_take",
    OUTCOME_TP_TOO_FAR: "lower_tp_or_dynamic_tp",
    OUTCOME_FEE_TOXIC: "add_min_expected_move_gate",
    OUTCOME_MISSED_WINNER: "review_rejection_filter",
    OUTCOME_AVOIDED_LOSER: "none",
    OUTCOME_AMBIGUOUS: "needs_more_data",
    OUTCOME_AMBIGUOUS_REJECTION: "needs_hypothetical_label",
    OUTCOME_NO_TRADE_OK: "none",
}


@dataclass
class SignalOutcome:
    """Classification result for one signal observation."""

    observation_id: int
    timestamp: str
    symbol: str
    side: str
    regime: str
    score: int
    score_bucket: str
    timeframe: str
    strategy: str
    source: str
    setup_key: str
    operated: int
    has_label: int
    realized_return_pct: float
    net_return_pct: float
    total_cost_pct: float
    mfe: float
    mae: float
    first_barrier_hit: str
    expected_move_pct: float
    expected_move_to_cost_ratio: float
    outcome_class: str
    suggested_fix: str
    notes: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _expected_move_pct(observation: dict[str, Any]) -> float:
    """Distance from entry to TP1, in pct of entry. Side-aware."""
    entry = safe_float(observation.get("entry_price"))
    tp1 = safe_float(observation.get("take_profit_1"))
    side = str(observation.get("side") or "").upper()
    if entry <= 0 or tp1 <= 0:
        return 0.0
    if side == "LONG":
        return (tp1 - entry) / entry * 100.0
    if side == "SHORT":
        return (entry - tp1) / entry * 100.0
    return 0.0


def _stop_distance_pct(observation: dict[str, Any]) -> float:
    """Distance from entry to stop, in pct."""
    entry = safe_float(observation.get("entry_price"))
    stop = safe_float(observation.get("stop_loss"))
    side = str(observation.get("side") or "").upper()
    if entry <= 0 or stop <= 0:
        return 0.0
    if side == "LONG":
        return (entry - stop) / entry * 100.0
    if side == "SHORT":
        return (stop - entry) / entry * 100.0
    return 0.0


def _classify_taken(
    realized_pct: float,
    net_pct: float,
    total_cost_pct: float,
    mfe: float,
    mae: float,
    first_barrier: str,
    expected_move_pct: float,
) -> tuple[str, str]:
    """Classify a TAKEN signal that has a label."""
    barrier = (first_barrier or "").upper()
    cost = max(total_cost_pct, 0.01)  # avoid divide-by-zero in heuristics

    # FEE_TOXIC first — overrides others
    if realized_pct > 0 and net_pct <= 0:
        return OUTCOME_FEE_TOXIC, "gross_positive_net_negative"

    if barrier in {"TP1", "TP2", "TAKE_PROFIT"}:
        # Did MFE keep extending much further than TP?
        if mfe >= max(2.0 * expected_move_pct, expected_move_pct + 2.0 * cost) and expected_move_pct > 0:
            return OUTCOME_TP_TOO_CLOSE, "tp_hit_but_mfe_kept_extending"
        return OUTCOME_CLEAN_WIN, "tp_hit_and_realized_positive"

    if barrier == "SL":
        # Did the price recover after stop?
        if mfe >= 0.5 and mfe > 2.0 * abs(realized_pct):
            return OUTCOME_STOP_TOO_TIGHT, "sl_hit_but_mfe_recovered_after"
        if abs(mae) > 1.5 * abs(realized_pct):
            return OUTCOME_STOP_TOO_LOOSE, "mae_much_worse_than_realized"
        return OUTCOME_CLEAN_LOSS, "sl_hit"

    if barrier == "TIME":
        # TIME with MFE near TP1 but no resolution -> TP_TOO_FAR
        if expected_move_pct > 0 and mfe >= 0.7 * expected_move_pct:
            return OUTCOME_TP_TOO_FAR, "time_exit_but_mfe_was_close_to_tp"
        # MFE significant, realized small -> PRECOCIOUS or LATE_EXIT depending
        if mfe >= 2 * cost and abs(realized_pct) < 0.5 * cost:
            return OUTCOME_LATE_EXIT, "mfe_existed_but_decayed_to_neutral"
        if abs(realized_pct) < cost:
            return OUTCOME_TIME_NEUTRAL, "time_exit_within_cost_band"
        return OUTCOME_TIME_NEUTRAL, "time_exit_default"

    return OUTCOME_AMBIGUOUS, "no_first_barrier_match"


def _classify_rejected(observation: dict[str, Any], label: dict[str, Any] | None) -> tuple[str, str]:
    """Classify a NON-operated signal that DOES have a hypothetical label."""
    if label is None:
        return OUTCOME_AMBIGUOUS_REJECTION, "no_hypothetical_label_available"
    label_value = safe_int(label.get("label"))
    if label_value == 1:
        return OUTCOME_MISSED_WINNER, "rejected_signal_would_have_hit_tp"
    if label_value == -1:
        return OUTCOME_AVOIDED_LOSER, "rejected_signal_would_have_hit_sl"
    return OUTCOME_AMBIGUOUS_REJECTION, "rejected_signal_label_inconclusive"


def classify_observation(
    observation: dict[str, Any],
    label: dict[str, Any] | None = None,
    *,
    cost_pct: float = 0.18,
) -> SignalOutcome:
    """Classify a single observation+label pair.

    cost_pct defaults to 0.18 (taker round-trip on Bitget VIP0 + 6 bps slippage).
    Caller can override per-symbol/per-regime if desired.
    """
    observation_id = safe_int(observation.get("id") or observation.get("observation_id"))
    side = str(observation.get("side") or "").upper()
    operated = safe_int(observation.get("operated"))
    has_label = 1 if label else 0
    realized_pct = safe_float((label or {}).get("realized_return_pct")) * 100.0  # convert fraction to pct
    mfe = safe_float((label or {}).get("max_favorable_excursion")) * 100.0
    mae = safe_float((label or {}).get("max_adverse_excursion")) * 100.0
    first_barrier = str((label or {}).get("first_barrier_hit") or "").upper()
    net_pct = realized_pct - cost_pct
    expected_move_pct = _expected_move_pct(observation)
    expected_move_to_cost = expected_move_pct / cost_pct if cost_pct > 0 else 0.0

    timeframe = str(observation.get("timeframe") or "5m").lower()
    key = build_setup_key(
        symbol=observation.get("symbol"),
        side=side,
        regime=observation.get("market_regime") or observation.get("regime"),
        score=observation.get("confidence_score"),
        timeframe=timeframe,
        strategy=observation.get("strategy_type"),
        exit_policy=observation.get("exit_policy") or "current_exit",
        source=observation.get("source") or "trade_signal",
    )

    if side == "NO_TRADE":
        outcome_class = OUTCOME_NO_TRADE_OK
        notes = "side_no_trade"
    elif operated == 1:
        # Real taken signal; require a label for meaningful classification.
        if has_label == 1:
            outcome_class, notes = _classify_taken(
                realized_pct=realized_pct,
                net_pct=net_pct,
                total_cost_pct=cost_pct,
                mfe=mfe,
                mae=mae,
                first_barrier=first_barrier,
                expected_move_pct=expected_move_pct,
            )
        else:
            outcome_class, notes = OUTCOME_AMBIGUOUS, "operated_without_label"
    else:
        # Rejected signal (operated == 0). Use hypothetical label if available.
        outcome_class, notes = _classify_rejected(observation, label)

    return SignalOutcome(
        observation_id=observation_id,
        timestamp=str(observation.get("timestamp") or ""),
        symbol=key.symbol,
        side=key.side,
        regime=key.regime,
        score=safe_int(observation.get("confidence_score")),
        score_bucket=key.score_bucket,
        timeframe=key.timeframe,
        strategy=key.strategy,
        source=key.source,
        setup_key=key.as_string(),
        operated=operated,
        has_label=has_label,
        realized_return_pct=realized_pct,
        net_return_pct=net_pct,
        total_cost_pct=cost_pct,
        mfe=mfe,
        mae=mae,
        first_barrier_hit=first_barrier,
        expected_move_pct=expected_move_pct,
        expected_move_to_cost_ratio=expected_move_to_cost,
        outcome_class=outcome_class,
        suggested_fix=SUGGESTED_FIX.get(outcome_class, "none"),
        notes=notes,
    )


def classify_batch(
    observations: Iterable[dict[str, Any]],
    labels_by_observation: dict[int, dict[str, Any]] | None = None,
    *,
    cost_pct: float = 0.18,
) -> list[SignalOutcome]:
    """Classify many observations at once."""
    labels_by_observation = labels_by_observation or {}
    results: list[SignalOutcome] = []
    for obs in observations:
        oid = safe_int(obs.get("id") or obs.get("observation_id"))
        label = labels_by_observation.get(oid)
        results.append(classify_observation(obs, label, cost_pct=cost_pct))
    return results


def summarize(outcomes: list[SignalOutcome]) -> dict[str, Any]:
    """Aggregate metrics from a batch — useful for reports."""
    if not outcomes:
        return {"total": 0, "by_class": {}, "by_setup_key": {}}
    by_class: dict[str, int] = {}
    by_setup: dict[str, dict[str, Any]] = {}
    for o in outcomes:
        by_class[o.outcome_class] = by_class.get(o.outcome_class, 0) + 1
        bucket = by_setup.setdefault(
            o.setup_key,
            {"samples": 0, "by_class": {}, "net_sum_pct": 0.0, "gross_sum_pct": 0.0},
        )
        bucket["samples"] += 1
        bucket["by_class"][o.outcome_class] = bucket["by_class"].get(o.outcome_class, 0) + 1
        bucket["net_sum_pct"] += o.net_return_pct
        bucket["gross_sum_pct"] += o.realized_return_pct
    # Compute averages per setup
    for setup, payload in by_setup.items():
        n = max(payload["samples"], 1)
        payload["net_ev_pct"] = round(payload["net_sum_pct"] / n, 6)
        payload["gross_ev_pct"] = round(payload["gross_sum_pct"] / n, 6)
    return {
        "total": len(outcomes),
        "by_class": dict(sorted(by_class.items(), key=lambda kv: -kv[1])),
        "by_setup_key": by_setup,
        "research_only": True,
        "no_runtime_change": True,
        "final_recommendation": "NO LIVE",
    }


def render_summary_text(summary: dict[str, Any]) -> str:
    lines = ["SIGNAL OUTCOME CLASSIFIER SUMMARY START"]
    lines.append(f"total: {summary.get('total', 0)}")
    lines.append("by_class:")
    for cls, count in summary.get("by_class", {}).items():
        lines.append(f"- {cls}: {count}")
    top_setups = list(summary.get("by_setup_key", {}).items())
    top_setups.sort(key=lambda kv: -kv[1].get("samples", 0))
    lines.append("top_setups_by_sample:")
    for setup_key_str, payload in top_setups[:20]:
        lines.append(
            f"- {setup_key_str} | samples={payload['samples']} "
            f"net_ev={payload.get('net_ev_pct', 0):.4f}% gross_ev={payload.get('gross_ev_pct', 0):.4f}%"
        )
    lines.append("research_only: true")
    lines.append("no_runtime_change: true")
    lines.append("final_recommendation: NO LIVE")
    lines.append("SIGNAL OUTCOME CLASSIFIER SUMMARY END")
    return "\n".join(lines)
