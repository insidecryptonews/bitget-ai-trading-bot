"""ResearchOps V8.1 — Catalyst Layer (research-only).

Generates idempotent ``event_id`` strings, canonicalises raw events, and
routes them to the appropriate family handler. Pure functions: no DB writes
beyond the :class:`EventStore`, no network calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import (
    FAMILY_CROWDING_OI_FUNDING,
    FAMILY_MACRO_SCHEDULED_CONTEXT,
    FAMILY_POST_LISTING_HIGH_FDV,
    FAMILY_TOKEN_UNLOCK,
    FINAL_RECOMMENDATION_NO_LIVE,
    STATUS_CONTEXT_ONLY,
    STATUS_DETECTED,
    STATUS_NEED_DATA,
    STATUS_NEEDS_REVIEW,
    STATUS_NOT_ACTIONABLE_NO_PERP,
    SUPPORTED_FAMILIES,
)
from .event_store import EventCandidate, EventStore


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_hour(dt: datetime) -> str:
    floored = _utc(dt).replace(minute=0, second=0, microsecond=0)
    return floored.strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_date(dt: datetime | str) -> str:
    if isinstance(dt, str):
        return dt[:10]
    return _utc(dt).strftime("%Y-%m-%d")


# ---- event_id generators ----

def event_id_crowding(symbol: str, ts_utc: datetime, trigger: str) -> str:
    """Stable id for a crowding/funding/OI break event.

    Example: ``crowding:BTCUSDT:2026-06-04T01:00:00Z:funding_oi_break``.
    """
    if not symbol or not trigger:
        raise ValueError("symbol and trigger are required")
    return f"crowding:{symbol.upper()}:{_iso_hour(ts_utc)}:{trigger.lower()}"


def event_id_listing(symbol: str, venue: str, day_n: int) -> str:
    """Stable id for a post-listing event.

    Example: ``listing:XYZUSDT:bitget:launch_day_3``.
    """
    if not symbol or not venue:
        raise ValueError("symbol and venue are required")
    return f"listing:{symbol.upper()}:{venue.lower()}:launch_day_{int(day_n)}"


def event_id_unlock(token: str, unlock_date: datetime | str, source: str) -> str:
    """Stable id for a token unlock event.

    Example: ``unlock:LAB:2026-08-15:tokenomist``.
    """
    if not token or not source:
        raise ValueError("token and source are required")
    return f"unlock:{token.upper()}:{_iso_date(unlock_date)}:{source.lower()}"


def event_id_macro(label: str, ts_utc: datetime) -> str:
    """Stable id for macro scheduled context."""
    if not label:
        raise ValueError("label required")
    return f"macro:{label.lower()}:{_iso_hour(ts_utc)}:context"


# ---- Canonical record ----

@dataclass
class CanonicalEvent:
    event_id: str
    family: str
    symbol: str
    event_time_utc: str
    payload: dict[str, Any]
    sources: list[str]
    research_only: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "family": self.family,
            "symbol": self.symbol,
            "event_time_utc": self.event_time_utc,
            "payload": self.payload,
            "sources": list(self.sources),
            "research_only": self.research_only,
            "final_recommendation": self.final_recommendation,
        }


def canonicalize(
    *,
    family: str,
    symbol: str,
    event_time_utc: datetime,
    payload: dict[str, Any],
    sources: list[str],
    trigger: str | None = None,
    venue: str | None = None,
    day_n: int | None = None,
    unlock_date: datetime | str | None = None,
    macro_label: str | None = None,
) -> CanonicalEvent:
    """Produce a canonical event with a deterministic ``event_id``."""

    if family not in SUPPORTED_FAMILIES:
        raise ValueError(f"unsupported family: {family}")
    if family == FAMILY_CROWDING_OI_FUNDING:
        if not trigger:
            raise ValueError("trigger is required for crowding events")
        eid = event_id_crowding(symbol, event_time_utc, trigger)
    elif family == FAMILY_POST_LISTING_HIGH_FDV:
        if not venue or day_n is None:
            raise ValueError("venue and day_n required for listing events")
        eid = event_id_listing(symbol, venue, day_n)
    elif family == FAMILY_TOKEN_UNLOCK:
        if not unlock_date:
            raise ValueError("unlock_date is required for unlock events")
        primary = (sources[0] if sources else "unknown").lower()
        eid = event_id_unlock(symbol, unlock_date, primary)
    elif family == FAMILY_MACRO_SCHEDULED_CONTEXT:
        if not macro_label:
            raise ValueError("macro_label is required for macro events")
        eid = event_id_macro(macro_label, event_time_utc)
    else:  # defensive
        eid = f"unknown:{symbol.upper()}:{_iso_hour(event_time_utc)}"

    return CanonicalEvent(
        event_id=eid,
        family=family,
        symbol=symbol.upper(),
        event_time_utc=_utc(event_time_utc).isoformat(),
        payload=dict(payload),
        sources=list(sources),
    )


# ---- Source conflict detection ----

def detect_source_conflict(
    *,
    primary_payload: dict[str, Any],
    secondary_payload: dict[str, Any] | None,
    numeric_fields: tuple[str, ...] = ("headline_size_usd", "size_pct_circ", "fdv_usd"),
    tolerance_pct: float = 0.10,
) -> tuple[bool, str]:
    """Compare two source payloads and flag a conflict when divergence exceeds
    ``tolerance_pct``.

    Returns (conflict_flag, reason).
    """
    if not secondary_payload:
        return False, ""
    reasons: list[str] = []
    for f in numeric_fields:
        a = primary_payload.get(f)
        b = secondary_payload.get(f)
        if a is None or b is None:
            continue
        try:
            a, b = float(a), float(b)
        except Exception:
            continue
        if a == 0 and b == 0:
            continue
        denom = max(abs(a), abs(b), 1e-9)
        delta = abs(a - b) / denom
        if delta > tolerance_pct:
            reasons.append(f"{f}_delta_{delta:.2f}")
    if reasons:
        return True, ",".join(reasons)
    return False, ""


# ---- Catalyst layer entry point ----

def ingest_event(
    store: EventStore,
    *,
    family: str,
    symbol: str,
    event_time_utc: datetime,
    payload_primary: dict[str, Any],
    source_primary: str,
    payload_secondary: dict[str, Any] | None = None,
    source_secondary: str | None = None,
    trigger: str | None = None,
    venue: str | None = None,
    day_n: int | None = None,
    unlock_date: datetime | str | None = None,
    macro_label: str | None = None,
) -> EventCandidate:
    """Ingest a raw event and upsert the corresponding candidate.

    Idempotent: calling twice with the same parameters returns the same
    ``event_id`` and does not duplicate the candidate.
    """

    if family not in SUPPORTED_FAMILIES:
        raise ValueError(f"unsupported family: {family}")
    # 1. Raw record
    store.insert_raw({
        "family": family,
        "symbol": symbol.upper(),
        "event_time_utc": _utc(event_time_utc).isoformat(),
        "source": source_primary,
        "payload": payload_primary,
    })
    if payload_secondary is not None and source_secondary:
        store.insert_raw({
            "family": family,
            "symbol": symbol.upper(),
            "event_time_utc": _utc(event_time_utc).isoformat(),
            "source": source_secondary,
            "payload": payload_secondary,
        })

    # 2. Canonicalise
    sources = [source_primary] + ([source_secondary] if source_secondary else [])
    canonical = canonicalize(
        family=family, symbol=symbol, event_time_utc=event_time_utc,
        payload=payload_primary, sources=sources, trigger=trigger,
        venue=venue, day_n=day_n, unlock_date=unlock_date, macro_label=macro_label,
    )
    store.upsert_canonical(canonical.event_id, canonical.as_dict())
    store.record_source(canonical.event_id, source_primary, payload_primary)
    if payload_secondary is not None and source_secondary:
        store.record_source(canonical.event_id, source_secondary, payload_secondary)

    # 3. Detect source conflict
    conflict, reason = detect_source_conflict(
        primary_payload=payload_primary,
        secondary_payload=payload_secondary,
    )

    # 4. Build candidate
    candidate = EventCandidate(
        event_id=canonical.event_id,
        family=family,
        symbol=symbol.upper(),
        event_time_utc=canonical.event_time_utc,
        source_primary=source_primary,
        source_secondary=source_secondary,
        source_conflict_flag=conflict,
        conflict_reason=reason,
        headline_size_usd=payload_primary.get("headline_size_usd"),
        effective_size_usd=payload_primary.get("effective_size_usd"),
        size_pct_circ=payload_primary.get("size_pct_circ"),
        float_pct=payload_primary.get("float_pct"),
        fdv_usd=payload_primary.get("fdv_usd"),
        age_days_since_listing=payload_primary.get("age_days_since_listing"),
        perp_available_bitget=bool(payload_primary.get("perp_available_bitget", False)),
        venue_count=int(payload_primary.get("venue_count", 0)),
        shortability_score=payload_primary.get("shortability_score"),
    )

    # 5. Initial status routing
    if family == FAMILY_MACRO_SCHEDULED_CONTEXT:
        candidate.status = STATUS_CONTEXT_ONLY
        candidate.notes.append("macro context is never actionable")
    elif conflict:
        candidate.status = STATUS_NEEDS_REVIEW
        candidate.notes.append(f"source conflict: {reason}")
    elif not candidate.perp_available_bitget:
        candidate.status = STATUS_NOT_ACTIONABLE_NO_PERP
        candidate.notes.append("no perp available on bitget")
    elif _has_missing_critical_field(candidate):
        candidate.status = STATUS_NEED_DATA
        candidate.notes.append("missing critical field")
    else:
        candidate.status = STATUS_DETECTED

    return store.upsert_candidate(candidate)


def _has_missing_critical_field(c: EventCandidate) -> bool:
    """Critical fields per family for an initial routing decision."""
    if c.family == FAMILY_CROWDING_OI_FUNDING:
        return c.shortability_score is None
    if c.family == FAMILY_POST_LISTING_HIGH_FDV:
        return c.fdv_usd is None or c.age_days_since_listing is None
    if c.family == FAMILY_TOKEN_UNLOCK:
        return c.size_pct_circ is None and c.headline_size_usd is None
    return False
