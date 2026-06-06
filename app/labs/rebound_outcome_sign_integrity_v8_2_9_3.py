"""V8.2.9.3 — Rebound Outcome Sign Integrity Audit (research-only).

Investigates the VPS V8.2.9.2 finding ``sign_bug_count=70`` /
``outcome_field_mismatch_count=149`` / ``winrate_dedup ~12.87%`` over
the LONG rebound universe. For each candidate, compares the expected
LONG direction against three signals — net PnL, future return, barrier
hit — and classifies any mismatch.

Hard contract:

- ex-post audit only;
- forward returns (``ret_*_pct``), MFE / MAE, barrier hits and baseline
  outcomes are read STRICTLY as diagnostic signals here. They are not
  fed into any detection / gating decision in the production
  pipeline.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK


# Classifications.
SIGN_OK = "SIGN_OK"
NET_PNL_SIGN_INVERTED = "NET_PNL_SIGN_INVERTED"
FUTURE_RETURN_DISAGREES_WITH_NET_PNL = "FUTURE_RETURN_DISAGREES_WITH_NET_PNL"
BARRIER_DISAGREES_WITH_NET_PNL = "BARRIER_DISAGREES_WITH_NET_PNL"
BASELINE_FIELD_MISMATCH = "BASELINE_FIELD_MISMATCH"
MISSING_OUTCOME_FIELDS = "MISSING_OUTCOME_FIELDS"
AMBIGUOUS_SAME_BAR = "AMBIGUOUS_SAME_BAR"
UNKNOWN = "UNKNOWN"
# V8.2.9.4 — join classifications.
MISSING_OR_AMBIGUOUS_JOIN = "MISSING_OR_AMBIGUOUS_JOIN"
JOIN_SYMBOL_MISMATCH = "JOIN_SYMBOL_MISMATCH"

# Join methods.
JOIN_METHOD_SIGNAL_ID = "signal_id"
JOIN_METHOD_SYMBOL_TIMESTAMP = "symbol_timestamp"
JOIN_METHOD_TIMESTAMP_UNIQUE_FALLBACK = "timestamp_unique_fallback"
JOIN_METHOD_MISSING_OR_AMBIGUOUS = "missing_or_ambiguous"

# Diagnostic thresholds — same as the canonicalizer.
RET_POSITIVE_THRESHOLD_PCT = 0.50
NET_PNL_NEGATIVE_THRESHOLD_PCT = 0.30
FIELD_MISMATCH_EPSILON = 0.01


@dataclass
class SignIntegrityRow:
    symbol: str
    timestamp: str
    side: str
    entry_price: float | None
    baseline_net_pnl_est: float | None
    baseline_gross_pnl: float | None
    ret_1h_pct_diagnostic: float | None
    ret_4h_pct_diagnostic: float | None
    mfe_pct_diagnostic: float | None
    mae_pct_diagnostic: float | None
    first_barrier_hit_diagnostic: str
    expected_long_direction: str
    outcome_sign_from_net_pnl: int
    outcome_sign_from_future_return: int
    outcome_sign_from_barrier: int
    mismatch_type: str
    reason: str
    # V8.2.9.4 — join trazability.
    join_method: str = JOIN_METHOD_MISSING_OR_AMBIGUOUS
    join_is_ambiguous: bool = True
    joined_raw_symbol: str = ""
    candidate_symbol: str = ""
    joined_raw_timestamp: str = ""
    candidate_timestamp: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SignIntegrityReport:
    hours: int
    generated_at: str
    total_candidates: int = 0
    sign_bug_count: int = 0
    sign_bug_ratio: float = 0.0
    outcome_field_mismatch_count: int = 0
    by_mismatch_type: dict[str, int] = field(default_factory=dict)
    mismatch_by_symbol: dict[str, int] = field(default_factory=dict)
    mismatch_by_regime: dict[str, int] = field(default_factory=dict)
    mismatch_by_candidate_reason: dict[str, int] = field(default_factory=dict)
    # V8.2.9.4 — join trazability roll-up.
    by_join_method: dict[str, int] = field(default_factory=dict)
    join_method_top: str = ""
    ambiguous_join_count: int = 0
    join_symbol_mismatch_count: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _sign(value: float | None) -> int:
    if value is None:
        return 0
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _classify_long(
    candidate: dict[str, Any], raw_row: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Classify the sign mismatch for a LONG candidate."""
    entry = _safe_float(raw_row.get("entry_price"))
    baseline = _safe_float(raw_row.get("baseline_net_pnl_est"))
    candidate_net = _safe_float(candidate.get("net_pnl_est"))
    ret_4h = _safe_float(raw_row.get("ret_4h_pct"))
    ret_1h = _safe_float(raw_row.get("ret_1h_pct"))
    mfe = _safe_float(raw_row.get("mfe_pct"))
    mae = _safe_float(raw_row.get("mae_pct"))
    barrier = str(raw_row.get("first_barrier_hit") or "")
    tp_before_sl = bool(raw_row.get("tp_before_sl"))
    sl_before_tp = bool(raw_row.get("sl_before_tp"))
    summary = {
        "outcome_sign_from_net_pnl": _sign(baseline),
        "outcome_sign_from_future_return": _sign(ret_4h),
        "outcome_sign_from_barrier": (
            1 if barrier == "TP" else (-1 if barrier == "SL" else 0)
        ),
    }
    # 1. Ambiguous same-bar — surface explicitly so the operator knows
    # the path didn't disambiguate.
    if tp_before_sl and sl_before_tp:
        return AMBIGUOUS_SAME_BAR, (
            "tp_before_sl_and_sl_before_tp_both_true"
        ), summary
    # 2. Missing critical fields.
    if baseline is None or entry is None:
        return MISSING_OUTCOME_FIELDS, "baseline_or_entry_missing", summary
    # 3. Baseline field mismatch.
    if (
        candidate_net is not None
        and abs(candidate_net - baseline) > FIELD_MISMATCH_EPSILON
    ):
        return BASELINE_FIELD_MISMATCH, (
            f"candidate_net={candidate_net:.4f}_vs_baseline={baseline:.4f}"
        ), summary
    # 4. Future-return disagreement (strongest indicator of sign bug).
    if (
        ret_4h is not None
        and ret_4h > RET_POSITIVE_THRESHOLD_PCT
        and baseline < -NET_PNL_NEGATIVE_THRESHOLD_PCT
    ):
        return FUTURE_RETURN_DISAGREES_WITH_NET_PNL, (
            f"ret_4h={ret_4h:.4f}_positive_but_baseline={baseline:.4f}_negative"
        ), summary
    # 5. Barrier disagreement (TP hit but net negative).
    if barrier == "TP" and baseline < -NET_PNL_NEGATIVE_THRESHOLD_PCT:
        return BARRIER_DISAGREES_WITH_NET_PNL, (
            f"barrier=TP_but_baseline={baseline:.4f}_negative"
        ), summary
    if barrier == "SL" and baseline > NET_PNL_NEGATIVE_THRESHOLD_PCT:
        return BARRIER_DISAGREES_WITH_NET_PNL, (
            f"barrier=SL_but_baseline={baseline:.4f}_positive"
        ), summary
    # 6. LONG-specific: MFE positive but barrier=SL and baseline very
    # negative may indicate net_pnl sign inversion.
    if (
        mfe is not None and mae is not None
        and mfe > 0.50 and mae < 0
        and barrier == "TP" and baseline < 0
    ):
        return NET_PNL_SIGN_INVERTED, (
            f"long_tp_mfe={mfe:.4f}_but_baseline={baseline:.4f}_negative"
        ), summary
    return SIGN_OK, "all_signals_consistent", summary


def _extract_signal_id(row: dict[str, Any]) -> str | None:
    """V8.2.9.4 — primary identifier for cross-table join, in priority
    order. Returns the string form so different numeric/string types
    don't drift."""
    for key in ("signal_id", "observation_id", "id"):
        v = row.get(key)
        if v is None:
            continue
        s = str(v)
        if s:
            return s
    return None


def _build_join_indexes(
    dataset_rows: list[dict[str, Any]],
) -> tuple[dict, dict, dict, dict]:
    """Pre-index raw rows by ``signal_id`` (highest priority), by
    ``(symbol, timestamp)``, and by ``timestamp`` (with ``ts_counts`` so
    consumers can verify uniqueness before falling back). V8.2.9.4."""
    by_signal_id: dict[str, dict[str, Any]] = {}
    by_symbol_ts: dict[tuple[str, str], dict[str, Any]] = {}
    by_ts: dict[str, dict[str, Any]] = {}
    ts_counts: dict[str, int] = {}
    for r in dataset_rows:
        sid = _extract_signal_id(r)
        if sid is not None:
            by_signal_id.setdefault(sid, r)
        symbol = str(r.get("symbol") or "")
        ts = str(r.get("timestamp") or "")
        if symbol and ts:
            by_symbol_ts.setdefault((symbol, ts), r)
        if ts:
            ts_counts[ts] = ts_counts.get(ts, 0) + 1
            by_ts.setdefault(ts, r)
    return by_signal_id, by_symbol_ts, by_ts, ts_counts


def _join_raw_for_candidate(
    candidate: dict[str, Any],
    indexes: tuple[dict, dict, dict, dict],
) -> tuple[dict[str, Any] | None, str, bool]:
    """Resolve the raw row that backs ``candidate``. Returns
    ``(raw, join_method, is_ambiguous)``.

    Priority:
    1. ``signal_id`` / ``observation_id`` / ``id`` exact match.
    2. ``(symbol, timestamp)`` exact match.
    3. ``timestamp`` unique fallback, but ONLY when the candidate has
       no symbol of its own (otherwise we'd risk cross-symbol joins).
    4. Otherwise ``MISSING_OR_AMBIGUOUS_JOIN`` — never produce a fake
       cross-symbol match.
    """
    by_signal_id, by_symbol_ts, by_ts, ts_counts = indexes
    sid = _extract_signal_id(candidate)
    if sid is not None and sid in by_signal_id:
        return by_signal_id[sid], JOIN_METHOD_SIGNAL_ID, False
    symbol = str(candidate.get("symbol") or "")
    ts = str(candidate.get("timestamp") or "")
    if symbol and ts and (symbol, ts) in by_symbol_ts:
        return by_symbol_ts[(symbol, ts)], JOIN_METHOD_SYMBOL_TIMESTAMP, False
    # Fallback to ``timestamp`` ONLY when the candidate has no symbol
    # AND the timestamp appears exactly once in the raw rows. Anything
    # else risks comparing apples to oranges.
    if not symbol and ts and ts_counts.get(ts, 0) == 1:
        return by_ts[ts], JOIN_METHOD_TIMESTAMP_UNIQUE_FALLBACK, False
    return None, JOIN_METHOD_MISSING_OR_AMBIGUOUS, True


def _zero_summary() -> dict[str, Any]:
    return {
        "outcome_sign_from_net_pnl": 0,
        "outcome_sign_from_future_return": 0,
        "outcome_sign_from_barrier": 0,
    }


def audit_sign_integrity(
    candidates: Iterable[dict[str, Any]] | None = None,
    *,
    dataset_rows: Iterable[dict[str, Any]] | None = None,
    hours: int = 168,
) -> SignIntegrityReport:
    """Audit each LONG rebound candidate against three independent
    outcome signals (net PnL, future return, barrier).

    V8.2.9.4 — join now uses ``signal_id`` → ``(symbol, timestamp)`` →
    ``timestamp_unique_fallback`` cascade, with explicit detection of
    cross-symbol joins (``JOIN_SYMBOL_MISMATCH``) and ambiguous matches
    (``MISSING_OR_AMBIGUOUS_JOIN``). The previous timestamp-only join
    could cross-match candidates from different symbols when their
    timestamps were synchronised, contaminating ``sign_bug_ratio``.
    """
    report = SignIntegrityReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    candidate_list = list(candidates or [])
    raw_rows = list(dataset_rows or [])
    indexes = _build_join_indexes(raw_rows)
    if not candidate_list:
        return report
    report.total_candidates = len(candidate_list)
    by_mismatch: dict[str, int] = {}
    by_symbol: dict[str, int] = {}
    by_regime: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    by_join: dict[str, int] = {}
    for c in candidate_list:
        ts = str(c.get("timestamp") or "")
        symbol = str(c.get("symbol") or "")
        side = str(c.get("side") or "LONG").upper()
        candidate_reason = str(c.get("candidate_reason") or "")
        regime = str(c.get("regime_now") or "")
        raw, join_method, join_is_ambiguous = _join_raw_for_candidate(
            c, indexes,
        )
        joined_raw_symbol = str(raw.get("symbol") or "") if raw else ""
        joined_raw_timestamp = str(raw.get("timestamp") or "") if raw else ""
        by_join[join_method] = by_join.get(join_method, 0) + 1

        if raw is None or join_is_ambiguous:
            mismatch = MISSING_OR_AMBIGUOUS_JOIN
            reason = f"join_method={join_method}"
            summary = _zero_summary()
            # Per-row contributes to ambiguous count but NOT to
            # ``sign_bug_count`` — we refuse to call it a bug without a
            # reliable match.
            report.ambiguous_join_count += 1
        else:
            # Defend against cross-symbol joins (e.g. timestamp_unique
            # fallback landing on the wrong symbol; or signal_id reuse
            # across symbols).
            if (
                symbol and joined_raw_symbol
                and symbol != joined_raw_symbol
            ):
                mismatch = JOIN_SYMBOL_MISMATCH
                reason = (
                    f"raw_symbol={joined_raw_symbol}_vs_candidate={symbol}_"
                    f"via={join_method}"
                )
                summary = _zero_summary()
                report.join_symbol_mismatch_count += 1
            else:
                regime = regime or str(raw.get("regime") or "")
                if side != "LONG":
                    mismatch = MISSING_OUTCOME_FIELDS
                    reason = "non_long_side_in_long_audit"
                    summary = _zero_summary()
                else:
                    mismatch, reason, summary = _classify_long(c, raw)
        raw_ref = raw if raw is not None else c
        sign_row = SignIntegrityRow(
            symbol=symbol,
            timestamp=ts,
            side=side,
            entry_price=_safe_float(raw_ref.get("entry_price")),
            baseline_net_pnl_est=_safe_float(
                raw_ref.get("baseline_net_pnl_est")
            ),
            baseline_gross_pnl=_safe_float(raw_ref.get("baseline_gross_pnl")),
            ret_1h_pct_diagnostic=_safe_float(raw_ref.get("ret_1h_pct")),
            ret_4h_pct_diagnostic=_safe_float(raw_ref.get("ret_4h_pct")),
            mfe_pct_diagnostic=_safe_float(raw_ref.get("mfe_pct")),
            mae_pct_diagnostic=_safe_float(raw_ref.get("mae_pct")),
            first_barrier_hit_diagnostic=str(
                raw_ref.get("first_barrier_hit") or ""
            ),
            expected_long_direction="up",
            outcome_sign_from_net_pnl=summary["outcome_sign_from_net_pnl"],
            outcome_sign_from_future_return=summary[
                "outcome_sign_from_future_return"
            ],
            outcome_sign_from_barrier=summary["outcome_sign_from_barrier"],
            mismatch_type=mismatch,
            reason=reason,
            join_method=join_method,
            join_is_ambiguous=join_is_ambiguous,
            joined_raw_symbol=joined_raw_symbol,
            candidate_symbol=symbol,
            joined_raw_timestamp=joined_raw_timestamp,
            candidate_timestamp=ts,
        )
        report.rows.append(sign_row.as_dict())
        by_mismatch[mismatch] = by_mismatch.get(mismatch, 0) + 1
        if mismatch != SIGN_OK:
            by_symbol[symbol] = by_symbol.get(symbol, 0) + 1
            by_regime[regime] = by_regime.get(regime, 0) + 1
            by_reason[candidate_reason] = by_reason.get(
                candidate_reason, 0,
            ) + 1
        if mismatch in {
            NET_PNL_SIGN_INVERTED,
            FUTURE_RETURN_DISAGREES_WITH_NET_PNL,
            BARRIER_DISAGREES_WITH_NET_PNL,
        }:
            report.sign_bug_count += 1
        if mismatch == BASELINE_FIELD_MISMATCH:
            report.outcome_field_mismatch_count += 1
    report.by_mismatch_type = by_mismatch
    report.mismatch_by_symbol = by_symbol
    report.mismatch_by_regime = by_regime
    report.mismatch_by_candidate_reason = by_reason
    report.by_join_method = by_join
    if by_join:
        report.join_method_top = max(
            by_join.items(), key=lambda kv: kv[1],
        )[0]
    if report.total_candidates > 0:
        report.sign_bug_ratio = (
            report.sign_bug_count / report.total_candidates
        )
    report.rows = report.rows[:5000]
    report.status = STATUS_OK
    return report
