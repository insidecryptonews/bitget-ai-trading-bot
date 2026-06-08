"""ResearchOps V10 — Event Catalyst Layer (research-only).

A research-only registry/classifier for market catalysts: token
unlocks, listings, delistings, hacks/exploits, whale transfers, exchange
inflows, macro events, regulatory news. It NEVER produces an operative
signal.

HARD CONTRACT — research only:

- never opens orders, never calls private endpoints, never writes DB,
- the only input is a *local* CSV/JSON file handed in by the operator,
- actionability is one of ``NOT_ACTIONABLE`` / ``WATCH_ONLY`` /
  ``SHADOW_RESEARCH_ONLY`` — never operative,
- uncertain events (low confidence) are embargoed to at most WATCH_ONLY,
- low source reliability => NOT_ACTIONABLE,
- missing data => ``NEED_DATA``.
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
    CatalystEvent,
    DATA_BAD,
    DATA_NEED,
    DATA_OK,
    DATA_WARNING,
    MIN_RELIABLE_SOURCE,
    _f,
    _parse_ts,
    cap_actionability,
    load_external_data,
    validate_external_rows,
)

DECISION_NEED_DATA = "NEED_DATA"
DECISION_WATCH_ONLY = "WATCH_ONLY"
DECISION_RESEARCH = "SHADOW_RESEARCH_ONLY"

EMBARGO_CONFIDENCE = 0.50

KNOWN_EVENT_TYPES = frozenset({
    "token_unlock", "unlock", "listing", "delisting", "hack", "exploit",
    "whale_transfer", "exchange_inflow", "macro", "regulatory", "news", "other",
})


@dataclass
class EventCatalystReport:
    hours: int = 720
    generated_at: str = ""
    source_label: str = ""
    rows_loaded: int = 0
    valid_events: int = 0
    invalid_events: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    by_actionability: dict[str, int] = field(default_factory=dict)
    embargoed_events: int = 0
    not_actionable_low_reliability: int = 0
    technical_confirmation_required_events: int = 0
    affected_symbols: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    data_quality_status: str = DATA_NEED
    decision: str = DECISION_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _norm_symbols(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        items = value
    elif isinstance(value, str):
        items = value.replace(";", ",").split(",")
    else:
        items = []
    return [str(s).strip().upper() for s in items if str(s).strip()]


def _build_event(row: dict[str, Any]) -> CatalystEvent:
    reliability = _f(row.get("source_reliability")) or 0.0
    confidence = _f(row.get("confidence_score"))
    confidence = confidence if confidence is not None else 0.0
    severity = _f(row.get("severity_score")) or 0.0
    etype = str(row.get("event_type") or "other").strip().lower()
    if etype not in KNOWN_EVENT_TYPES:
        etype = "other"
    embargo_default = str(row.get("embargo_if_uncertain", "true")).strip().lower() not in ("0", "false", "no")
    tech_req = str(row.get("technical_confirmation_required", "true")).strip().lower() not in ("0", "false", "no")
    embargo = embargo_default and confidence < EMBARGO_CONFIDENCE

    ev = CatalystEvent(
        event_id=str(row.get("event_id") or row.get("logical_key") or ""),
        timestamp=str(row.get("timestamp") or row.get("event_time") or ""),
        event_type=etype,
        source=str(row.get("source") or ""),
        source_reliability=reliability,
        confidence_score=confidence,
        severity_score=severity,
        direction_bias=str(row.get("direction_bias") or "unknown").upper(),
        affected_symbols=_norm_symbols(row.get("affected_symbols") or row.get("symbol")),
        embargo_if_uncertain=embargo_default,
        technical_confirmation_required=tech_req,
        data_quality_status=DATA_OK,
        logical_key=str(row.get("logical_key") or ""),
        duplicate_flag=bool(row.get("duplicate_flag")),
    )
    # Catalysts never exceed SHADOW_RESEARCH_ONLY, and only when reliable,
    # confident, and technically confirmable.
    proposed = ACT_SHADOW_RESEARCH_ONLY if tech_req is False else ACT_WATCH_ONLY
    ev.actionability = cap_actionability(
        proposed,
        source_reliability=reliability,
        data_quality_status=DATA_OK,
        embargo=embargo,
    )
    return ev


def analyze_event_catalysts(
    rows: Iterable[dict[str, Any]] | None,
    *,
    hours: int = 720,
    source_label: str = "",
) -> EventCatalystReport:
    report = EventCatalystReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_label=source_label,
    )
    row_list = list(rows or [])
    report.rows_loaded = len(row_list)
    if not row_list:
        report.decision = DECISION_NEED_DATA
        return report

    ts_field = "event_time" if any("event_time" in r for r in row_list) else "timestamp"
    # Catalysts carry ``affected_symbols`` (possibly empty for macro /
    # regulatory events), not a single ``symbol``. Derive a symbol token so
    # the strict base validator (dedup / NaN / timestamp / freshness) does
    # not reject market-wide events for "missing symbol".
    prepared: list[dict[str, Any]] = []
    for r in row_list:
        row = dict(r)
        if not str(row.get("symbol") or "").strip():
            syms = _norm_symbols(row.get("affected_symbols"))
            row["symbol"] = syms[0] if syms else "MARKET"
        prepared.append(row)
    vr = validate_external_rows(
        prepared,
        value_fields=("severity_score", "confidence_score"),
        ts_field=ts_field,
    )
    report.valid_events = len(vr.valid)
    report.invalid_events = len(vr.rejected)

    events = [_build_event(r) for r in vr.valid]
    by_type: dict[str, int] = {}
    by_act: dict[str, int] = {}
    symbols: set[str] = set()
    for e in events:
        by_type[e.event_type] = by_type.get(e.event_type, 0) + 1
        by_act[e.actionability] = by_act.get(e.actionability, 0) + 1
        symbols.update(e.affected_symbols)
        if e.actionability == ACT_WATCH_ONLY and e.confidence_score < EMBARGO_CONFIDENCE:
            report.embargoed_events += 1
        if e.source_reliability < MIN_RELIABLE_SOURCE:
            report.not_actionable_low_reliability += 1
        if e.technical_confirmation_required:
            report.technical_confirmation_required_events += 1
        report.events.append(e.as_dict())
    report.by_type = by_type
    report.by_actionability = by_act
    report.affected_symbols = sorted(symbols)

    if not vr.valid:
        report.data_quality_status = DATA_BAD
    else:
        bad_ratio = len(vr.rejected) / max(1, len(row_list))
        report.data_quality_status = (
            DATA_OK if bad_ratio == 0 else (DATA_WARNING if bad_ratio < 0.5 else DATA_BAD)
        )

    if report.valid_events == 0:
        report.decision = DECISION_NEED_DATA
    elif by_act.get(ACT_SHADOW_RESEARCH_ONLY, 0) > 0:
        report.decision = DECISION_RESEARCH
    else:
        report.decision = DECISION_WATCH_ONLY
    return report


def run_event_catalyst_layer(
    *,
    hours: int = 720,
    external_data_path: str | None = None,
) -> EventCatalystReport:
    rows, source_label = load_external_data(external_data_path)
    return analyze_event_catalysts(rows, hours=hours, source_label=source_label)
