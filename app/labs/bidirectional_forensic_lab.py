"""V8.2 — Bidirectional Forensic Lab (research-only).

Analyses LONG and SHORT separately and together. Pure-Python helpers that
either consume rows passed by the caller (for tests) or attempt to read them
from the project DB via ``safe_call``; if the DB does not expose the needed
method, the helpers return ``NEED_DATA`` honestly.

No order placement, no DB writes from this module.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import (
    FINAL_RECOMMENDATION_NO_LIVE,
    SIDE_LONG,
    SIDE_NO_TRADE,
    SIDE_SHORT,
    STATUS_NEED_DATA,
    STATUS_OK,
    STATUS_PARTIAL,
)


# ---- Helpers ---------------------------------------------------------------

def _safe_call(db: Any, method: str, *args, **kwargs) -> tuple[bool, Any]:
    if db is None:
        return False, None
    fn = getattr(db, method, None)
    if fn is None or not callable(fn):
        return False, None
    try:
        return True, fn(*args, **kwargs)
    except Exception:
        return False, None


def _score_bucket(score: float | int | None) -> str:
    if score is None:
        return "unknown"
    try:
        s = int(score)
    except Exception:
        return "unknown"
    if s >= 90:
        return "90-100"
    if s >= 80:
        return "80-89"
    if s >= 70:
        return "70-79"
    if s >= 60:
        return "60-69"
    return "<60"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- Funnel ----------------------------------------------------------------

@dataclass
class FunnelReport:
    hours: int
    generated_at: str
    side_filter: str | None
    total_signals: int = 0
    by_side: dict[str, int] = field(default_factory=dict)
    by_regime: dict[str, int] = field(default_factory=dict)
    by_score_bucket: dict[str, int] = field(default_factory=dict)
    by_reject_reason: dict[str, int] = field(default_factory=dict)
    outcomes: dict[str, int] = field(default_factory=dict)
    gross_ev_avg_by_side: dict[str, float] = field(default_factory=dict)
    net_ev_avg_by_side: dict[str, float] = field(default_factory=dict)
    mfe_avg_by_side: dict[str, float] = field(default_factory=dict)
    mae_avg_by_side: dict[str, float] = field(default_factory=dict)
    status: str = STATUS_NEED_DATA
    need_data_reasons: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _signal_rows(
    db: Any,
    hours: int,
    rows: Iterable[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if rows is not None:
        return list(rows), []
    ok, value = _safe_call(db, "fetch_signal_observations", hours=int(hours))
    if not ok or value is None:
        return [], ["signal_observations_method_missing_or_empty"]
    return list(value), []


def build_funnel(
    db: Any,
    *,
    hours: int = 168,
    side_filter: str | None = None,
    rows: Iterable[dict[str, Any]] | None = None,
) -> FunnelReport:
    """Build the LONG/SHORT/NO_TRADE funnel.

    ``rows`` allows direct injection (tests); otherwise tries
    ``db.fetch_signal_observations(hours=...)``.
    """
    data, need = _signal_rows(db, hours, rows)
    report = FunnelReport(
        hours=int(hours),
        generated_at=_now(),
        side_filter=side_filter,
        need_data_reasons=list(need),
    )
    if not data:
        return report

    side_filter_upper = side_filter.upper() if side_filter else None
    filtered = []
    for r in data:
        side = str(r.get("side") or SIDE_NO_TRADE).upper()
        if side_filter_upper and side != side_filter_upper:
            continue
        filtered.append(r)

    report.total_signals = len(filtered)
    if not filtered:
        return report

    gross_sums: dict[str, list[float]] = {}
    net_sums: dict[str, list[float]] = {}
    mfe_sums: dict[str, list[float]] = {}
    mae_sums: dict[str, list[float]] = {}

    for r in filtered:
        side = str(r.get("side") or SIDE_NO_TRADE).upper()
        regime = str(r.get("market_regime") or r.get("regime") or "UNKNOWN").upper()
        bucket = _score_bucket(r.get("confidence_score") or r.get("score"))
        reject_reason = str(r.get("reason") or r.get("reject_reason") or "").strip()
        outcome = str(r.get("first_barrier_hit") or r.get("outcome") or "").upper()
        report.by_side[side] = report.by_side.get(side, 0) + 1
        report.by_regime[regime] = report.by_regime.get(regime, 0) + 1
        report.by_score_bucket[bucket] = report.by_score_bucket.get(bucket, 0) + 1
        if reject_reason:
            report.by_reject_reason[reject_reason] = report.by_reject_reason.get(reject_reason, 0) + 1
        if outcome:
            report.outcomes[outcome] = report.outcomes.get(outcome, 0) + 1
        gross = r.get("gross_ev_pct")
        net = r.get("net_ev_pct")
        mfe = r.get("mfe_pct")
        mae = r.get("mae_pct")
        if isinstance(gross, (int, float)):
            gross_sums.setdefault(side, []).append(float(gross))
        if isinstance(net, (int, float)):
            net_sums.setdefault(side, []).append(float(net))
        if isinstance(mfe, (int, float)):
            mfe_sums.setdefault(side, []).append(float(mfe))
        if isinstance(mae, (int, float)):
            mae_sums.setdefault(side, []).append(float(mae))

    report.gross_ev_avg_by_side = {k: sum(v) / max(len(v), 1) for k, v in gross_sums.items()}
    report.net_ev_avg_by_side = {k: sum(v) / max(len(v), 1) for k, v in net_sums.items()}
    report.mfe_avg_by_side = {k: sum(v) / max(len(v), 1) for k, v in mfe_sums.items()}
    report.mae_avg_by_side = {k: sum(v) / max(len(v), 1) for k, v in mae_sums.items()}
    report.status = STATUS_OK if not need else STATUS_PARTIAL
    return report


# ---- Missed opportunities --------------------------------------------------

@dataclass
class MissedOpportunity:
    event_time_utc: str
    symbol: str
    side: str
    regime: str
    score: float | None
    reason: str
    ret_15m_pct: float | None
    ret_30m_pct: float | None
    ret_1h_pct: float | None
    ret_4h_pct: float | None
    would_have_worked_estimate: str  # "True" / "False" / "NEED_DATA"


@dataclass
class MissedOpsReport:
    hours: int
    generated_at: str
    side: str
    top_n: int
    candidates: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_NEED_DATA
    need_data_reasons: list[str] = field(default_factory=list)
    # V8.2.2 — explicit calculability accounting so callers can distinguish
    # "I produced 20 candidates with no future returns" (NEED_DATA) from
    # "I produced 20 candidates, 15 with future returns" (PARTIAL or OK).
    calculable_count: int = 0
    need_data_count: int = 0
    need_data_ratio: float = 0.0
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _would_have_worked(side: str, fut_ret_pct: float | None, threshold: float = 0.30) -> str:
    """For SHORT: lower future price (negative return) = favorable.

    For LONG: higher future price (positive return) = favorable.
    ``threshold`` in pct (0.30 = +0.30% / -0.30%).
    """
    if fut_ret_pct is None:
        return "NEED_DATA"
    s = side.upper()
    if s == SIDE_LONG:
        return "True" if fut_ret_pct >= threshold else "False"
    if s == SIDE_SHORT:
        return "True" if fut_ret_pct <= -threshold else "False"
    return "NEED_DATA"


def missed_opportunities(
    db: Any,
    *,
    side: str,
    hours: int = 168,
    top_n: int = 20,
    rows: Iterable[dict[str, Any]] | None = None,
) -> MissedOpsReport:
    """Return top-N NO_TRADE (or rejected) signals of ``side`` whose future
    return would have been favourable.

    Each row needs ``ret_15m_pct``, ``ret_30m_pct``, ``ret_1h_pct``,
    ``ret_4h_pct`` (any can be None).
    """
    side_upper = side.upper()
    report = MissedOpsReport(
        hours=int(hours),
        generated_at=_now(),
        side=side_upper,
        top_n=int(top_n),
    )
    if side_upper not in {SIDE_LONG, SIDE_SHORT}:
        report.need_data_reasons.append(f"unsupported_side:{side_upper}")
        return report
    data, need = _signal_rows(db, hours, rows)
    report.need_data_reasons.extend(need)
    if not data:
        return report

    candidates: list[MissedOpportunity] = []
    for r in data:
        signal_side = str(r.get("proposed_side") or r.get("side_intended") or r.get("side") or "").upper()
        executed_side = str(r.get("side") or "").upper()
        # Only NO_TRADE or rejected of the requested side.
        if executed_side == side_upper and r.get("reason", "").lower().startswith(("tp", "sl", "time")):
            # Already executed; skip from missed.
            continue
        if signal_side != side_upper:
            continue
        if executed_side == side_upper and r.get("status") not in (None, "", "NO_TRADE", "REJECTED"):
            continue
        fut_h1 = r.get("ret_1h_pct")
        fut_h4 = r.get("ret_4h_pct")
        favourable = _would_have_worked(side_upper, fut_h1)
        # If 1h NEED_DATA but 4h available, use 4h as fallback.
        if favourable == "NEED_DATA" and fut_h4 is not None:
            favourable = _would_have_worked(side_upper, fut_h4)
        candidates.append(MissedOpportunity(
            event_time_utc=str(r.get("event_time_utc") or r.get("created_at") or ""),
            symbol=str(r.get("symbol") or ""),
            side=side_upper,
            regime=str(r.get("market_regime") or r.get("regime") or "UNKNOWN").upper(),
            score=r.get("confidence_score") if isinstance(r.get("confidence_score"), (int, float)) else r.get("score"),
            reason=str(r.get("reason") or ""),
            ret_15m_pct=r.get("ret_15m_pct"),
            ret_30m_pct=r.get("ret_30m_pct"),
            ret_1h_pct=fut_h1,
            ret_4h_pct=fut_h4,
            would_have_worked_estimate=favourable,
        ))

    # Sort by "True" first, then by magnitude of 1h move in favourable direction.
    def _sort_key(m: MissedOpportunity) -> tuple[int, float]:
        worked = 1 if m.would_have_worked_estimate == "True" else 0
        magnitude = 0.0
        if m.ret_1h_pct is not None:
            magnitude = -m.ret_1h_pct if side_upper == SIDE_SHORT else m.ret_1h_pct
        return (worked, magnitude)

    candidates.sort(key=_sort_key, reverse=True)
    report.candidates = [asdict(c) for c in candidates[: report.top_n]]
    # V8.2.2 — honest status: NEED_DATA when no candidate has a future return,
    # PARTIAL when some do, OK only when all do. Status must never be OK just
    # because we produced candidates — Codex caught this in the V8.2 audit.
    total = len(candidates)
    calculable = sum(
        1 for c in candidates if c.would_have_worked_estimate in {"True", "False"}
    )
    need_data = total - calculable
    report.calculable_count = calculable
    report.need_data_count = need_data
    report.need_data_ratio = (need_data / total) if total else 0.0
    if total == 0:
        report.status = STATUS_NEED_DATA
    elif calculable == 0:
        report.status = STATUS_NEED_DATA
        if "missing_future_returns" not in report.need_data_reasons:
            report.need_data_reasons.append("missing_future_returns")
    elif need_data > 0:
        report.status = STATUS_PARTIAL
    else:
        report.status = STATUS_OK
    return report


# ---- Blocked that would have worked ---------------------------------------

def blocked_that_would_have_worked(
    db: Any,
    *,
    side: str,
    hours: int = 168,
    top_n: int = 20,
    min_score: int = 60,
    rows: Iterable[dict[str, Any]] | None = None,
) -> MissedOpsReport:
    """Same as missed_opportunities but restricted to signals whose internal
    score >= ``min_score`` AND that were not executed.

    Useful to flag false negatives of the existing gates.
    """
    data, need = _signal_rows(db, hours, rows)
    report = MissedOpsReport(
        hours=int(hours), generated_at=_now(),
        side=side.upper(), top_n=int(top_n),
        need_data_reasons=list(need),
    )
    if not data:
        return report
    side_upper = side.upper()
    if side_upper not in {SIDE_LONG, SIDE_SHORT}:
        report.need_data_reasons.append(f"unsupported_side:{side_upper}")
        return report
    filtered = []
    for r in data:
        score = r.get("confidence_score") or r.get("score") or 0
        try:
            score = int(score)
        except Exception:
            score = 0
        if score < int(min_score):
            continue
        proposed = str(r.get("proposed_side") or r.get("side_intended") or r.get("side") or "").upper()
        executed = str(r.get("side") or "").upper()
        if proposed != side_upper:
            continue
        if executed == side_upper and r.get("status") not in (None, "", "NO_TRADE", "REJECTED"):
            continue
        filtered.append(r)
    if not filtered:
        report.status = STATUS_NEED_DATA
        return report
    sub = missed_opportunities(db, side=side_upper, hours=hours, top_n=top_n, rows=filtered)
    report.candidates = sub.candidates
    report.status = sub.status
    # V8.2.2 — propagate the calculability accounting from the inner call.
    report.calculable_count = sub.calculable_count
    report.need_data_count = sub.need_data_count
    report.need_data_ratio = sub.need_data_ratio
    if sub.need_data_reasons:
        for reason in sub.need_data_reasons:
            if reason not in report.need_data_reasons:
                report.need_data_reasons.append(reason)
    return report


# ---- Failed executed -------------------------------------------------------

@dataclass
class FailedExecuted:
    event_time_utc: str
    symbol: str
    side: str
    regime: str
    score: float | None
    outcome: str
    realized_pct: float | None
    mfe_pct: float | None
    mae_pct: float | None
    bars_open: int | None
    failure_reason: str


@dataclass
class FailedExecutedReport:
    hours: int
    generated_at: str
    side: str
    top_n: int
    failures: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_NEED_DATA
    need_data_reasons: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _failure_reason(outcome: str, realized: float | None, mfe: float | None, mae: float | None) -> str:
    out = (outcome or "").upper()
    if out in {"SL", "STOP_LOSS"}:
        if mfe is not None and mfe > 0.5 and mae is not None and abs(mae) > 0.5:
            return "stop_hit_after_significant_mfe_whipsaw"
        return "stop_loss_clean"
    if out in {"TIME", "TIME_DECAY_FLAT", "HORIZON_CLOSE"}:
        if mfe is not None and mfe > 1.0 and (realized is None or realized < 0.3):
            return "time_death_with_uncaptured_mfe"
        return "time_death_flat"
    if out == "BREAK_EVEN":
        return "break_even_after_tp1"
    if out in {"TP", "TP1", "TP2", "TAKE_PROFIT", "TAKE_PROFIT_2"} and realized is not None and realized < 0.2:
        return "tp_hit_but_costs_ate_edge"
    return "unknown_failure"


def failed_executed(
    db: Any,
    *,
    side: str,
    hours: int = 168,
    top_n: int = 20,
    rows: Iterable[dict[str, Any]] | None = None,
) -> FailedExecutedReport:
    side_upper = side.upper()
    report = FailedExecutedReport(
        hours=int(hours), generated_at=_now(), side=side_upper, top_n=int(top_n),
    )
    data, need = _signal_rows(db, hours, rows)
    report.need_data_reasons.extend(need)
    if not data:
        return report
    out: list[FailedExecuted] = []
    for r in data:
        executed_side = str(r.get("side") or "").upper()
        if executed_side != side_upper:
            continue
        outcome = str(r.get("first_barrier_hit") or r.get("outcome") or "").upper()
        if outcome in {"", "OPEN"}:
            continue
        realized = r.get("realized_pct") if isinstance(r.get("realized_pct"), (int, float)) else r.get("net_pnl_pct")
        if isinstance(realized, (int, float)):
            realized = float(realized)
        else:
            realized = None
        if realized is not None and realized > 0.2:
            continue  # successful execution, not a failure
        mfe = r.get("mfe_pct") if isinstance(r.get("mfe_pct"), (int, float)) else None
        mae = r.get("mae_pct") if isinstance(r.get("mae_pct"), (int, float)) else None
        out.append(FailedExecuted(
            event_time_utc=str(r.get("event_time_utc") or r.get("created_at") or ""),
            symbol=str(r.get("symbol") or ""),
            side=side_upper,
            regime=str(r.get("market_regime") or r.get("regime") or "UNKNOWN").upper(),
            score=r.get("confidence_score") if isinstance(r.get("confidence_score"), (int, float)) else r.get("score"),
            outcome=outcome,
            realized_pct=realized,
            mfe_pct=float(mfe) if isinstance(mfe, (int, float)) else None,
            mae_pct=float(mae) if isinstance(mae, (int, float)) else None,
            bars_open=int(r.get("bars_open")) if isinstance(r.get("bars_open"), (int, float)) else None,
            failure_reason=_failure_reason(outcome, realized, mfe, mae),
        ))
    out.sort(key=lambda f: (f.realized_pct if f.realized_pct is not None else 0.0))
    report.failures = [asdict(f) for f in out[: report.top_n]]
    report.status = STATUS_OK if report.failures else STATUS_NEED_DATA
    return report


# ---- Good not monetized ----------------------------------------------------

@dataclass
class GoodNotMonetized:
    event_time_utc: str
    symbol: str
    side: str
    regime: str
    score: float | None
    realized_pct: float | None
    mfe_pct: float
    mfe_capture_ratio: float
    bars_open: int | None
    likely_cause: str


@dataclass
class GoodNotMonetizedReport:
    hours: int
    generated_at: str
    side: str
    top_n: int
    cases: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_NEED_DATA
    need_data_reasons: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def good_not_monetized(
    db: Any,
    *,
    side: str,
    hours: int = 168,
    top_n: int = 20,
    rows: Iterable[dict[str, Any]] | None = None,
) -> GoodNotMonetizedReport:
    side_upper = side.upper()
    report = GoodNotMonetizedReport(
        hours=int(hours), generated_at=_now(), side=side_upper, top_n=int(top_n),
    )
    data, need = _signal_rows(db, hours, rows)
    report.need_data_reasons.extend(need)
    if not data:
        return report
    cases: list[GoodNotMonetized] = []
    for r in data:
        executed_side = str(r.get("side") or "").upper()
        if executed_side != side_upper:
            continue
        mfe = r.get("mfe_pct")
        realized = r.get("realized_pct") if isinstance(r.get("realized_pct"), (int, float)) else r.get("net_pnl_pct")
        if not isinstance(mfe, (int, float)) or mfe <= 0:
            continue
        mfe = float(mfe)
        realized_f = float(realized) if isinstance(realized, (int, float)) else 0.0
        capture = realized_f / mfe if mfe > 0 else 0.0
        if capture >= 0.55:
            continue  # captured most of MFE — not a "good not monetized" case
        outcome = str(r.get("first_barrier_hit") or r.get("outcome") or "").upper()
        if outcome in {"TIME", "HORIZON_CLOSE"}:
            cause = "time_death_no_trailing"
        elif outcome == "BREAK_EVEN":
            cause = "be_after_tp1_then_reversed"
        elif outcome in {"TP", "TP1", "TP2", "TAKE_PROFIT", "TAKE_PROFIT_2"}:
            cause = "tp_fixed_capped_upside"
        else:
            cause = "unknown_low_capture"
        cases.append(GoodNotMonetized(
            event_time_utc=str(r.get("event_time_utc") or r.get("created_at") or ""),
            symbol=str(r.get("symbol") or ""),
            side=side_upper,
            regime=str(r.get("market_regime") or r.get("regime") or "UNKNOWN").upper(),
            score=r.get("confidence_score") if isinstance(r.get("confidence_score"), (int, float)) else r.get("score"),
            realized_pct=realized_f,
            mfe_pct=mfe,
            mfe_capture_ratio=capture,
            bars_open=int(r.get("bars_open")) if isinstance(r.get("bars_open"), (int, float)) else None,
            likely_cause=cause,
        ))
    cases.sort(key=lambda c: c.mfe_pct - c.realized_pct, reverse=True)
    report.cases = [asdict(c) for c in cases[: report.top_n]]
    report.status = STATUS_OK if report.cases else STATUS_NEED_DATA
    return report
