"""V8.2.8 — Rebound / Regime Turn Readiness Lab (research-only).

V8.2.8.1 hotfix — *prefix-only detection*. Codex flagged the previous
implementation for reading ``ret_1h_pct`` / ``ret_4h_pct`` when deciding
whether a row was a rebound candidate. In this dataset those columns are
future / counterfactual returns, so using them as detection inputs
introduces lookahead.

The current version uses ONLY information available at or before the
signal's timestamp:

- ``side`` of the current row;
- ``regime`` of the current row (regime is classified from prior bars);
- ``regime`` of the prior rows of the same symbol (most recent first);
- ``entry_price`` of the current and prior rows for the same symbol
  (price at the signal moment — strictly observed before any forward
  return);
- ``normalized_atr`` of the current row (computed from prior bars);
- ``score_bucket``, ``strategy``, ``candidate_selected``, ``risk_approved``
  are surfaced as diagnostics only; they are not used as gating features.

The following fields are *forbidden as detection inputs* and the
prefix-only detector never reads them:

- ``ret_15m_pct``, ``ret_30m_pct``, ``ret_1h_pct``, ``ret_4h_pct``,
  ``ret_24h_pct``;
- ``mfe_pct``, ``mae_pct``;
- ``first_barrier_hit``, ``tp_before_sl``, ``sl_before_tp``;
- ``baseline_result``, ``baseline_gross_pnl``, ``baseline_net_pnl_est``;
- ``trailing_result``, ``trailing_net_pnl_est``;
- ``campaign_result``, ``campaign_net_pnl_est``;
- ``would_have_worked_*``;
- ``training_label``.

Outcome evaluation (``evaluate_rebound_outcome``) MAY read
``baseline_net_pnl_est`` strictly AFTER the candidate has been detected,
so the research export can label rebound candidates with their
retrospective outcome. The detector does not depend on it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK
from .counterfactual_dedup_audit import _is_evaluable, dedup_rows
from .counterfactual_training_dataset import build_dataset


REBOUND_NEED_MORE_DATA = "REBOUND_NEED_MORE_DATA"
REBOUND_RESEARCH_CANDIDATE = "REBOUND_RESEARCH_CANDIDATE"
REBOUND_NOT_READY = "REBOUND_NOT_READY"

DOWN_REGIMES = frozenset({"TREND_DOWN", "RISK_OFF", "HIGH_VOLATILITY"})

DETECTION_MODE_PREFIX_ONLY = "prefix_only"
DETECTION_MODE_NEED_DATA = "need_data"

DETECTION_REASON_OK = "prefix_features_ok"
DETECTION_REASON_NOT_LONG = "not_long_side"
DETECTION_REASON_INSUFFICIENT_HISTORY = "insufficient_prefix_history"
DETECTION_REASON_NO_PRIOR_DOWN_REGIME = "no_prior_down_regime"
DETECTION_REASON_MISSING_PREFIX = "missing_prefix_ohlcv_or_prefix_features"

# Minimum number of prior rows of the same symbol required before
# attempting prefix-only detection.
MIN_HISTORY_ROWS = 5


@dataclass
class ReboundCandidate:
    signal_id: Any
    timestamp: str
    symbol: str
    side: str
    regime_before: str
    regime_now: str
    score: float | None
    drawdown_from_recent_high_pct: float | None
    bounce_confirmation: bool
    trend_alignment_recovering: bool
    volatility_bucket: str
    rebound_label: str
    net_pnl: float | None
    detection_mode: str = DETECTION_MODE_PREFIX_ONLY
    detection_reason: str = DETECTION_REASON_OK
    used_future_return_features: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReboundRegimeTurnReport:
    hours: int
    generated_at: str
    rebound_candidates_count: int = 0
    rebound_good_count: int = 0
    rebound_bad_count: int = 0
    rebound_unknown_count: int = 0
    prefix_only_count: int = 0
    need_data_count: int = 0
    detection_reason_breakdown: dict[str, int] = field(default_factory=dict)
    net_ev_est_pct: float = 0.0
    readiness: str = REBOUND_NEED_MORE_DATA
    examples_top_100: list[dict[str, Any]] = field(default_factory=list)
    report_detection_mode: str = DETECTION_MODE_PREFIX_ONLY
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

    ``history`` is the chronological list of rows for the same symbol that
    precede the current one. Returns a dict carrying only quantities
    derived from data observed strictly before the current timestamp:

    - ``regime_before``: most recent non-empty regime from the last 5
      prior rows.
    - ``prior_entries``: list of ``entry_price`` values from the last 10
      prior rows (each entry_price was the price at that prior signal's
      moment, i.e. strictly before the current row).
    - ``prior_regimes``: list of regime strings from the last 5 prior
      rows (used to verify history depth).

    Returns an empty dict if there is no history.
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


def detect_rebound_candidate_prefix_only(
    row: dict[str, Any],
    prefix_context: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Decide whether the row is a LONG rebound candidate using only
    prefix-only features. The detector never reads forward-return
    columns, MFE / MAE columns, barrier-hit columns, baseline / trailing
    / campaign outcome columns, or the training-label column. The full
    list of forbidden ex-post fields is documented at the top of the
    module.

    Returns ``(is_candidate, info)``. ``info`` always carries the keys
    ``detection_mode`` and ``detection_reason`` so the caller can
    aggregate breakdowns.
    """
    side = str(row.get("side", "")).upper()
    if side != "LONG":
        return False, {
            "detection_mode": DETECTION_MODE_PREFIX_ONLY,
            "detection_reason": DETECTION_REASON_NOT_LONG,
        }
    if not prefix_context:
        return False, {
            "detection_mode": DETECTION_MODE_NEED_DATA,
            "detection_reason": DETECTION_REASON_INSUFFICIENT_HISTORY,
        }
    prior_entries = prefix_context.get("prior_entries") or []
    prior_regimes = prefix_context.get("prior_regimes") or []
    if len(prior_regimes) < MIN_HISTORY_ROWS or not prior_entries:
        return False, {
            "detection_mode": DETECTION_MODE_NEED_DATA,
            "detection_reason": DETECTION_REASON_MISSING_PREFIX,
        }
    regime_before = str(prefix_context.get("regime_before") or "").upper()
    if regime_before not in DOWN_REGIMES:
        return False, {
            "detection_mode": DETECTION_MODE_PREFIX_ONLY,
            "detection_reason": DETECTION_REASON_NO_PRIOR_DOWN_REGIME,
        }
    regime_now = str(row.get("regime") or "").upper() or "UNKNOWN"
    current_entry = row.get("entry_price")
    # Drawdown from recent prefix high — purely from prior entry-price
    # ladder. ``entry_price`` is the price at the moment of each prior
    # signal, observed strictly before the current row.
    drawdown_proxy: float | None = None
    if isinstance(current_entry, (int, float)) and prior_entries:
        prior_high = max(prior_entries)
        if prior_high > 0:
            drawdown_proxy = (float(current_entry) - prior_high) / prior_high
    # Higher-low bounce confirmation derived strictly from prior entries.
    last3_entries = prior_entries[-3:] if len(prior_entries) >= 3 else []
    higher_lows = (
        len(last3_entries) >= 3
        and last3_entries[0] < last3_entries[1] < last3_entries[2]
    )
    trend_alignment_recovering = (
        regime_now not in DOWN_REGIMES and regime_now != "UNKNOWN"
    )
    bounce_confirmation = bool(higher_lows or trend_alignment_recovering)
    info = {
        "regime_before": regime_before,
        "regime_now": regime_now,
        "drawdown_from_recent_high_pct": drawdown_proxy,
        "bounce_confirmation": bounce_confirmation,
        "trend_alignment_recovering": bool(trend_alignment_recovering),
        "volatility_bucket": _volatility_bucket(row.get("normalized_atr")),
        "detection_mode": DETECTION_MODE_PREFIX_ONLY,
        "detection_reason": DETECTION_REASON_OK,
    }
    return True, info


def evaluate_rebound_outcome(row: dict[str, Any]) -> tuple[str, float | None]:
    """Ex-post outcome evaluation. MUST only be called AFTER detection.

    Reads ``baseline_net_pnl_est`` (an ex-post counterfactual outcome) to
    label whether the rebound candidate ended up positive or negative.
    Returns ``(label, net_pnl)`` where ``label`` is ``"good"`` /
    ``"bad"`` / ``"unknown"``.

    This function is intentionally separate from the detector so the
    ex-ante decision boundary stays clean — no caller can accidentally
    leak the outcome into detection.
    """
    net = row.get("baseline_net_pnl_est")
    if not isinstance(net, (int, float)):
        return "unknown", None
    return ("good" if float(net) > 0 else "bad"), float(net)


def detect_rebound_setups(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    rows: Iterable[dict[str, Any]] | None = None,
) -> ReboundRegimeTurnReport:
    """Scan the dataset for LONG rebound setups using prefix-only features."""
    report = ReboundRegimeTurnReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    evaluable = [r for r in dataset if _is_evaluable(r)]
    evaluable = dedup_rows(evaluable)
    if not evaluable:
        return report
    # Sort by (symbol, timestamp) so per-symbol history is well-defined.
    evaluable.sort(key=lambda r: (str(r.get("symbol", "")), str(r.get("timestamp", ""))))
    per_symbol_history: dict[str, list[dict[str, Any]]] = {}
    candidates: list[ReboundCandidate] = []
    net_total = 0.0
    counted = 0
    reason_counts: dict[str, int] = {}
    for r in evaluable:
        symbol = str(r.get("symbol") or "")
        history = per_symbol_history.setdefault(symbol, [])
        prefix_context = _build_prefix_context(history)
        is_rebound, info = detect_rebound_candidate_prefix_only(r, prefix_context)
        # Append AFTER detection so the current row never appears in its
        # own prefix context.
        history.append(r)
        reason = str(info.get("detection_reason") or "")
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if info.get("detection_mode") == DETECTION_MODE_NEED_DATA:
            report.need_data_count += 1
        if not is_rebound:
            continue
        report.prefix_only_count += 1
        outcome, net = evaluate_rebound_outcome(r)
        if outcome == "good":
            report.rebound_good_count += 1
        elif outcome == "bad":
            report.rebound_bad_count += 1
        else:
            report.rebound_unknown_count += 1
        if net is not None:
            net_total += net
            counted += 1
        candidates.append(ReboundCandidate(
            signal_id=r.get("signal_id"),
            timestamp=str(r.get("timestamp", "")),
            symbol=symbol,
            side="LONG",
            regime_before=info["regime_before"],
            regime_now=info["regime_now"],
            score=r.get("score"),
            drawdown_from_recent_high_pct=info["drawdown_from_recent_high_pct"],
            bounce_confirmation=info["bounce_confirmation"],
            trend_alignment_recovering=info["trend_alignment_recovering"],
            volatility_bucket=info["volatility_bucket"],
            rebound_label=outcome,
            net_pnl=net,
            detection_mode=info["detection_mode"],
            detection_reason=info["detection_reason"],
            used_future_return_features=False,
            note=("" if info["bounce_confirmation"] else "lacks_bounce_confirmation"),
        ))

    report.rebound_candidates_count = len(candidates)
    if counted > 0:
        report.net_ev_est_pct = net_total / counted
    report.examples_top_100 = [c.as_dict() for c in candidates[:100]]
    report.detection_reason_breakdown = reason_counts
    report.used_future_return_features = False
    # Top-level mode: prefix_only when we managed to evaluate at least one
    # row with prefix context, regardless of whether it became a
    # candidate. ``need_data`` if every row hit a NEED_DATA reason.
    if report.prefix_only_count == 0 and report.need_data_count > 0:
        report.report_detection_mode = DETECTION_MODE_NEED_DATA
    else:
        report.report_detection_mode = DETECTION_MODE_PREFIX_ONLY

    if report.rebound_candidates_count < 20:
        report.readiness = REBOUND_NEED_MORE_DATA
    elif (
        report.net_ev_est_pct > 0
        and report.rebound_good_count >= report.rebound_bad_count
    ):
        report.readiness = REBOUND_RESEARCH_CANDIDATE
    else:
        report.readiness = REBOUND_NOT_READY
    report.status = STATUS_OK
    return report
