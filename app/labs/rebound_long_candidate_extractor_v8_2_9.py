"""V8.2.9 — Rebound LONG Candidate Extractor (research-only).

Stricter, focused version of the V8.2.8.1 rebound detector. Extracts
ONLY LONG signals that look like rebound setups after a TREND_DOWN /
RISK_OFF context, using exclusively prefix-only features.

Hard contract:

- LONG side only.
- Detection uses prefix-only features. No forward returns. No ex-post
  fields. No future outcomes.
- The outcome evaluation function is physically separate from the
  detector and may only be called AFTER detection.

The full list of forbidden ex-post fields is enforced at module level
and verified by an AST scan test in the V8.2.9 test suite.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK
from .counterfactual_dedup_audit import _is_evaluable


# Fields that MUST NOT appear in the prefix-only detection path.
FORBIDDEN_INPUT_FIELDS: frozenset[str] = frozenset({
    "ret_15m_pct", "ret_30m_pct", "ret_1h_pct", "ret_4h_pct", "ret_24h_pct",
    "mfe_pct", "mae_pct",
    "first_barrier_hit", "tp_before_sl", "sl_before_tp",
    "baseline_result", "baseline_gross_pnl", "baseline_net_pnl_est",
    "trailing_result", "trailing_net_pnl_est",
    "campaign_result", "campaign_net_pnl_est",
    "would_have_worked_baseline", "would_have_worked_trailing",
    "would_have_worked_campaign",
    "training_label",
})

DOWN_REGIMES = frozenset({"TREND_DOWN", "RISK_OFF", "HIGH_VOLATILITY"})

DETECTION_MODE_PREFIX_ONLY = "prefix_only"
DETECTION_MODE_NEED_DATA = "need_data"

CANDIDATE_REASON_OK = "rebound_long_after_down_regime"
CANDIDATE_REASON_NOT_LONG = "not_long_side"
CANDIDATE_REASON_NO_HISTORY = "insufficient_prefix_history"
CANDIDATE_REASON_NO_DOWN = "no_prior_down_regime"
CANDIDATE_REASON_MARKET_PROBE = "market_probe_source_excluded"

MIN_HISTORY_ROWS = 5


@dataclass
class ReboundLongCandidate:
    symbol: str
    timestamp: str
    regime_before: str
    regime_now: str
    entry_price: float | None
    drawdown_proxy_prefix: float | None
    higher_lows_prefix: bool
    trend_recovering_prefix: bool
    bounce_confirmation_prefix: bool
    volatility_bucket: str
    score_bucket_diagnostic: str
    candidate_reason: str
    detection_mode: str
    # V8.2.9.5 — join keys propagated so downstream bridges can attach
    # real outcomes from ``signal_path_metrics`` by ``observation_id``.
    observation_id: Any = None
    signal_id: Any = None
    used_future_return_features: bool = False
    # Outcome — populated ONLY by evaluate_long_outcome (ex-post).
    net_pnl_est: float | None = None
    outcome_winner_loser: str = ""
    mfe_pct_outcome: float | None = None
    mae_pct_outcome: float | None = None
    barrier_result_outcome: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReboundLongExtractorReport:
    hours: int
    generated_at: str
    raw_signals: int = 0
    long_signals: int = 0
    candidates_count: int = 0
    prefix_only_count: int = 0
    need_data_count: int = 0
    by_candidate_reason: dict[str, int] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    used_future_return_features: bool = False
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _volatility_bucket(atr_norm: Any) -> str:
    if not isinstance(atr_norm, (int, float)):
        return "unknown"
    v = float(atr_norm)
    if v >= 0.05:
        return "extreme"
    if v >= 0.025:
        return "high"
    if v >= 0.012:
        return "normal"
    return "low"


def _build_prefix_context(history: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract prefix-only context from prior rows of the same symbol.

    Only reads ``regime``, ``entry_price``, and the chronological order
    of prior signal rows — all strictly observed before the current
    timestamp.
    """
    if not history:
        return {}
    regime_before = ""
    for prev in reversed(history[-5:]):
        candidate = str(prev.get("regime") or "").upper()
        if candidate:
            regime_before = candidate
            break
    prior_entries: list[float] = []
    for prev in history[-10:]:
        ep = prev.get("entry_price")
        if isinstance(ep, (int, float)) and float(ep) > 0:
            prior_entries.append(float(ep))
    prior_regimes = [
        str(p.get("regime") or "").upper() for p in history[-5:]
    ]
    return {
        "regime_before": regime_before,
        "prior_entries": prior_entries,
        "prior_regimes": prior_regimes,
    }


def detect_rebound_long_prefix_only(
    row: dict[str, Any],
    prefix_context: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Pure prefix-only LONG rebound detection.

    Never reads forward-return columns, MFE / MAE columns, barrier-hit
    columns, baseline / trailing / campaign outcome columns, or the
    training-label column. The complete forbidden list is documented at
    the top of the module.
    """
    side = str(row.get("side", "")).upper()
    if side != "LONG":
        return False, {
            "detection_mode": DETECTION_MODE_PREFIX_ONLY,
            "candidate_reason": CANDIDATE_REASON_NOT_LONG,
        }
    if "market_probe" in str(row.get("source", "")).lower():
        # market_probe is explicitly excluded — never actionable.
        return False, {
            "detection_mode": DETECTION_MODE_PREFIX_ONLY,
            "candidate_reason": CANDIDATE_REASON_MARKET_PROBE,
        }
    if not prefix_context:
        return False, {
            "detection_mode": DETECTION_MODE_NEED_DATA,
            "candidate_reason": CANDIDATE_REASON_NO_HISTORY,
        }
    prior_entries = prefix_context.get("prior_entries") or []
    prior_regimes = prefix_context.get("prior_regimes") or []
    if len(prior_regimes) < MIN_HISTORY_ROWS or not prior_entries:
        return False, {
            "detection_mode": DETECTION_MODE_NEED_DATA,
            "candidate_reason": CANDIDATE_REASON_NO_HISTORY,
        }
    regime_before = str(prefix_context.get("regime_before") or "").upper()
    if regime_before not in DOWN_REGIMES:
        return False, {
            "detection_mode": DETECTION_MODE_PREFIX_ONLY,
            "candidate_reason": CANDIDATE_REASON_NO_DOWN,
        }
    regime_now = str(row.get("regime") or "").upper() or "UNKNOWN"
    current_entry = row.get("entry_price")
    drawdown_proxy: float | None = None
    if isinstance(current_entry, (int, float)) and prior_entries:
        prior_high = max(prior_entries)
        if prior_high > 0:
            drawdown_proxy = (float(current_entry) - prior_high) / prior_high
    last3_entries = prior_entries[-3:] if len(prior_entries) >= 3 else []
    higher_lows = (
        len(last3_entries) >= 3
        and last3_entries[0] < last3_entries[1] < last3_entries[2]
    )
    trend_recovering = (
        regime_now not in DOWN_REGIMES and regime_now != "UNKNOWN"
    )
    bounce_confirmation = bool(higher_lows or trend_recovering)
    info = {
        "detection_mode": DETECTION_MODE_PREFIX_ONLY,
        "candidate_reason": CANDIDATE_REASON_OK,
        "regime_before": regime_before,
        "regime_now": regime_now,
        "drawdown_proxy_prefix": drawdown_proxy,
        "higher_lows_prefix": higher_lows,
        "trend_recovering_prefix": bool(trend_recovering),
        "bounce_confirmation_prefix": bounce_confirmation,
        "volatility_bucket": _volatility_bucket(row.get("normalized_atr")),
        "score_bucket_diagnostic": str(row.get("score_bucket") or "unknown"),
    }
    return True, info


def evaluate_long_outcome(row: dict[str, Any]) -> dict[str, Any]:
    """Ex-post outcome evaluation. MUST only be invoked AFTER detection.

    Reads the counterfactual baseline outcome / MFE / MAE / barrier hit
    purely to label the retrospective outcome of an already-detected
    candidate. The detector never invokes this function.
    """
    net = row.get("baseline_net_pnl_est")
    if isinstance(net, (int, float)):
        winner = "winner" if float(net) > 0 else "loser"
        net_val = float(net)
    else:
        winner = "unknown"
        net_val = None
    mfe = row.get("mfe_pct")
    mae = row.get("mae_pct")
    return {
        "net_pnl_est": net_val,
        "outcome_winner_loser": winner,
        "mfe_pct_outcome": float(mfe) if isinstance(mfe, (int, float)) else None,
        "mae_pct_outcome": float(mae) if isinstance(mae, (int, float)) else None,
        "barrier_result_outcome": str(row.get("first_barrier_hit") or ""),
    }


def extract_rebound_long_candidates(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    rows: Iterable[dict[str, Any]] | None = None,
) -> ReboundLongExtractorReport:
    """Scan the dataset for LONG rebound candidates using prefix-only features."""
    report = ReboundLongExtractorReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if rows is None:
        from .counterfactual_training_dataset import build_dataset
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    report.raw_signals = len(dataset)
    evaluable = [r for r in dataset if _is_evaluable(r)]
    long_subset = [r for r in evaluable if str(r.get("side", "")).upper() == "LONG"]
    report.long_signals = len(long_subset)
    if not long_subset:
        return report
    long_subset.sort(key=lambda r: (str(r.get("symbol", "")), str(r.get("timestamp", ""))))
    per_symbol_history: dict[str, list[dict[str, Any]]] = {}
    reason_counts: dict[str, int] = {}
    for r in long_subset:
        symbol = str(r.get("symbol") or "")
        history = per_symbol_history.setdefault(symbol, [])
        prefix_context = _build_prefix_context(history)
        is_cand, info = detect_rebound_long_prefix_only(r, prefix_context)
        # Append AFTER detection so the current row never appears in its own
        # prefix context.
        history.append(r)
        reason = str(info.get("candidate_reason") or "")
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if info.get("detection_mode") == DETECTION_MODE_NEED_DATA:
            report.need_data_count += 1
        if not is_cand:
            continue
        report.prefix_only_count += 1
        outcome = evaluate_long_outcome(r)
        cand = ReboundLongCandidate(
            symbol=symbol,
            timestamp=str(r.get("timestamp", "")),
            regime_before=info["regime_before"],
            regime_now=info["regime_now"],
            entry_price=float(r["entry_price"])
            if isinstance(r.get("entry_price"), (int, float)) else None,
            drawdown_proxy_prefix=info["drawdown_proxy_prefix"],
            higher_lows_prefix=info["higher_lows_prefix"],
            trend_recovering_prefix=info["trend_recovering_prefix"],
            bounce_confirmation_prefix=info["bounce_confirmation_prefix"],
            volatility_bucket=info["volatility_bucket"],
            score_bucket_diagnostic=info["score_bucket_diagnostic"],
            candidate_reason=info["candidate_reason"],
            detection_mode=info["detection_mode"],
            observation_id=r.get("observation_id") or r.get("signal_id"),
            signal_id=r.get("signal_id"),
            used_future_return_features=False,
            **outcome,
        )
        report.candidates.append(cand.as_dict())
    report.candidates_count = len(report.candidates)
    report.by_candidate_reason = reason_counts
    report.status = STATUS_OK
    return report
