"""ResearchOps V10 — Token Unlock / High-FDV / Post-Listing research.

Prepares a *systematic short-basket* research family targeting dilution
events: large token unlocks, high-FDV / low-float listings, post-listing
hype exhaustion, airdrops. This is a research scaffold, NOT a signal
source and NOT a trade generator.

HARD CONTRACT — research only:

- never opens orders, never calls private endpoints, never writes DB,
- never fabricates events: no event data => ``NEED_DATA``,
- low source reliability => ``NOT_ACTIONABLE``,
- uncertain event => embargo / ``WATCH_ONLY``,
- the only input is a *local* CSV/JSON calendar handed in by the operator,
- output is always ``final_recommendation = NO LIVE``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE
from .edge_data_foundation_v10 import (
    ACT_NOT_ACTIONABLE,
    ACT_SHADOW_RESEARCH_ONLY,
    ACT_WATCH_ONLY,
    DATA_BAD,
    DATA_NEED,
    DATA_OK,
    DATA_WARNING,
    MIN_RELIABLE_SOURCE,
    TokenUnlockEvent,
    _f,
    _parse_ts,
    cap_actionability,
    load_external_data,
    validate_external_rows,
)

DECISION_NEED_DATA = "NEED_DATA"
DECISION_WATCH_ONLY = "WATCH_ONLY"
DECISION_REJECT = "REJECT"
DECISION_IMPLEMENT_FIRST = "IMPLEMENT_FIRST_RESEARCH"

# Unlock as % of circulating supply that we consider material.
MATERIAL_UNLOCK_PCT = 5.0
# FDV/MCAP above which dilution overhang is considered high.
HIGH_FDV_TO_MCAP = 3.0
# Confidence below which the event is embargoed (uncertain).
EMBARGO_CONFIDENCE = 0.50
MIN_EVENTS_FOR_STUDY = 3


@dataclass
class UnlockPostListingReport:
    hours: int = 720
    generated_at: str = ""
    source_label: str = ""
    rows_loaded: int = 0
    events_loaded: int = 0
    valid_events: int = 0
    embargoed_events: int = 0
    not_actionable_low_reliability: int = 0
    affected_symbols: list[str] = field(default_factory=list)
    event_windows: list[dict[str, Any]] = field(default_factory=list)
    material_unlock_events: int = 0
    high_fdv_events: int = 0
    risk_score: float = 0.0
    short_bias_score: float = 0.0
    data_quality_status: str = DATA_NEED
    event_study_ready: bool = False
    required_data_missing: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    decision: str = DECISION_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _severity(unlock_pct: float | None, fdv_to_mcap: float | None) -> float:
    sev = 0.0
    if unlock_pct is not None:
        sev += min(unlock_pct / 20.0, 1.0) * 0.6
    if fdv_to_mcap is not None and fdv_to_mcap > 1.0:
        sev += min((fdv_to_mcap - 1.0) / 9.0, 1.0) * 0.4
    return round(min(sev, 1.0), 4)


def _build_event(row: dict[str, Any]) -> TokenUnlockEvent:
    unlock_pct = _f(row.get("unlock_pct_circulating"))
    mcap = _f(row.get("market_cap"))
    fdv = _f(row.get("fdv"))
    fdv_to_mcap = _f(row.get("fdv_to_mcap"))
    if fdv_to_mcap is None and fdv is not None and mcap and mcap > 0:
        fdv_to_mcap = round(fdv / mcap, 4)
    confidence = _f(row.get("confidence_score"))
    confidence = confidence if confidence is not None else 0.0
    reliability = _f(row.get("source_reliability")) or 0.0
    severity = _severity(unlock_pct, fdv_to_mcap)

    ev = TokenUnlockEvent(
        event_id=str(row.get("event_id") or row.get("logical_key") or ""),
        symbol=str(row.get("symbol") or "").strip().upper(),
        event_time=str(row.get("event_time") or row.get("timestamp") or ""),
        event_type=str(row.get("event_type") or "unlock"),
        source=str(row.get("source") or ""),
        source_reliability=reliability,
        unlock_pct_circulating=unlock_pct,
        unlock_value_usd=_f(row.get("unlock_value_usd")),
        market_cap=mcap,
        fdv=fdv,
        fdv_to_mcap=fdv_to_mcap,
        listing_age_days=_f(row.get("listing_age_days")),
        direction_bias=str(row.get("direction_bias") or "SHORT").upper(),
        severity_score=severity,
        confidence_score=confidence,
        data_quality_status=DATA_OK,
        logical_key=str(row.get("logical_key") or ""),
        duplicate_flag=bool(row.get("duplicate_flag")),
    )
    embargo = confidence < EMBARGO_CONFIDENCE
    ev.actionability = cap_actionability(
        ACT_SHADOW_RESEARCH_ONLY,
        source_reliability=reliability,
        data_quality_status=DATA_OK,
        embargo=embargo,
    )
    return ev


def analyze_unlock_post_listing(
    rows: Iterable[dict[str, Any]] | None,
    *,
    hours: int = 720,
    source_label: str = "",
) -> UnlockPostListingReport:
    report = UnlockPostListingReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_label=source_label,
    )
    row_list = list(rows or [])
    report.rows_loaded = len(row_list)
    if not row_list:
        report.required_data_missing = ["unlock_calendar", "token_metadata"]
        report.blockers = ["no_external_data"]
        report.decision = DECISION_NEED_DATA
        return report

    vr = validate_external_rows(
        row_list,
        value_fields=("unlock_pct_circulating", "fdv", "market_cap"),
        ts_field="event_time" if any("event_time" in r for r in row_list) else "timestamp",
    )
    report.events_loaded = len(row_list)

    events: list[TokenUnlockEvent] = [_build_event(r) for r in vr.valid]
    report.valid_events = len(events)
    report.affected_symbols = sorted({e.symbol for e in events if e.symbol})

    sev_sum = 0.0
    short_sum = 0.0
    for e in events:
        if e.actionability == ACT_WATCH_ONLY:
            report.embargoed_events += 1
        if e.source_reliability < MIN_RELIABLE_SOURCE:
            report.not_actionable_low_reliability += 1
        if e.unlock_pct_circulating is not None and e.unlock_pct_circulating >= MATERIAL_UNLOCK_PCT:
            report.material_unlock_events += 1
        if e.fdv_to_mcap is not None and e.fdv_to_mcap >= HIGH_FDV_TO_MCAP:
            report.high_fdv_events += 1
        sev_sum += e.severity_score
        if e.direction_bias == "SHORT":
            short_sum += e.severity_score
        ts = _parse_ts(e.event_time)
        report.event_windows.append({
            "event_id": e.event_id,
            "symbol": e.symbol,
            "event_time": e.event_time,
            "severity_score": e.severity_score,
            "direction_bias": e.direction_bias,
            "actionability": e.actionability,
            "valid_time": ts is not None,
        })

    if events:
        report.risk_score = round(sev_sum / len(events), 4)
        report.short_bias_score = round(short_sum / len(events), 4)

    # Data quality.
    if not vr.valid:
        report.data_quality_status = DATA_BAD
    else:
        bad_ratio = len(vr.rejected) / max(1, len(row_list))
        report.data_quality_status = (
            DATA_OK if bad_ratio == 0 else (DATA_WARNING if bad_ratio < 0.5 else DATA_BAD)
        )

    missing: list[str] = []
    if not any(e.unlock_pct_circulating is not None for e in events):
        missing.append("unlock_pct_circulating")
    if not any(e.fdv_to_mcap is not None for e in events):
        missing.append("fdv_to_mcap")
    report.required_data_missing = missing

    blockers: list[str] = []
    if report.data_quality_status == DATA_BAD:
        blockers.append("data_quality_bad")
    if report.valid_events < MIN_EVENTS_FOR_STUDY:
        blockers.append("insufficient_events_for_study")
    if report.material_unlock_events + report.high_fdv_events == 0:
        blockers.append("no_material_dilution_events")
    report.blockers = blockers

    report.event_study_ready = (
        report.valid_events >= MIN_EVENTS_FOR_STUDY
        and (report.material_unlock_events + report.high_fdv_events) > 0
        and report.data_quality_status != DATA_BAD
    )

    if report.valid_events == 0:
        report.decision = DECISION_NEED_DATA
    elif report.data_quality_status == DATA_BAD:
        report.decision = DECISION_NEED_DATA
    elif report.event_study_ready:
        report.decision = DECISION_IMPLEMENT_FIRST
    elif (report.material_unlock_events + report.high_fdv_events) == 0 and report.valid_events >= MIN_EVENTS_FOR_STUDY:
        report.decision = DECISION_REJECT
    else:
        report.decision = DECISION_WATCH_ONLY
    return report


def run_unlock_post_listing_research(
    *,
    hours: int = 720,
    external_data_path: str | None = None,
) -> UnlockPostListingReport:
    rows, source_label = load_external_data(external_data_path)
    return analyze_unlock_post_listing(rows, hours=hours, source_label=source_label)
