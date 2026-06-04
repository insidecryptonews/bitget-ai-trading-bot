"""ResearchOps V8.1 — Event Candidate Registry (research-only).

Thin orchestrator on top of :class:`EventStore`:

- exposes ``register_candidate`` (delegates to ``catalyst_layer.ingest_event``),
- exposes ``promote`` to move a candidate to ``ACTIONABLE_LABEL_ONLY`` only when
  every required gate has been passed,
- never promotes automatically,
- never opens orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import (
    FAMILY_MACRO_SCHEDULED_CONTEXT,
    FINAL_RECOMMENDATION_NO_LIVE,
    STATUS_ACTIONABLE_LABEL_ONLY,
    STATUS_CONTEXT_ONLY,
    STATUS_DETECTED,
    STATUS_LOW_SHORTABILITY,
    STATUS_NEED_DATA,
    STATUS_NEEDS_REVIEW,
    STATUS_NOT_ACTIONABLE_NO_PERP,
    STATUS_REJECTED,
    SUPPORTED_FAMILIES,
)
from .catalyst_layer import ingest_event
from .event_store import EventCandidate, EventStore
from .shortability_score import SHORTABILITY_THRESHOLD_LOW


def register_candidate(
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
    """Idempotent registration. Returns the resulting (or existing) candidate."""
    return ingest_event(
        store,
        family=family,
        symbol=symbol,
        event_time_utc=event_time_utc,
        payload_primary=payload_primary,
        source_primary=source_primary,
        payload_secondary=payload_secondary,
        source_secondary=source_secondary,
        trigger=trigger,
        venue=venue,
        day_n=day_n,
        unlock_date=unlock_date,
        macro_label=macro_label,
    )


def evaluate_candidate(
    store: EventStore,
    *,
    event_id: str,
) -> EventCandidate | None:
    """Re-evaluate a candidate's status using its current fields.

    Promotion rules:

    - ``macro_scheduled_context`` stays at ``CONTEXT_ONLY`` forever.
    - source conflict → ``NEEDS_REVIEW``.
    - no perp on bitget → ``NOT_ACTIONABLE_NO_PERP``.
    - shortability below threshold → ``LOW_SHORTABILITY``.
    - shortability missing or critical missing field → ``NEED_DATA``.
    - all gates pass → ``ACTIONABLE_LABEL_ONLY`` (research label only).
    """
    rec = store.get_candidate(event_id)
    if rec is None:
        return None

    new_status = rec.status
    notes: list[str] = list(rec.notes)

    if rec.family == FAMILY_MACRO_SCHEDULED_CONTEXT:
        new_status = STATUS_CONTEXT_ONLY
    elif rec.source_conflict_flag:
        new_status = STATUS_NEEDS_REVIEW
    elif not rec.perp_available_bitget:
        new_status = STATUS_NOT_ACTIONABLE_NO_PERP
    elif rec.shortability_score is None:
        new_status = STATUS_NEED_DATA
    elif rec.shortability_score < SHORTABILITY_THRESHOLD_LOW:
        new_status = STATUS_LOW_SHORTABILITY
    else:
        new_status = STATUS_ACTIONABLE_LABEL_ONLY
        notes.append("research label only — operator decides")

    rec.status = new_status
    rec.notes = notes
    return store.upsert_candidate(rec)


def reject(store: EventStore, *, event_id: str, reason: str) -> EventCandidate | None:
    rec = store.get_candidate(event_id)
    if rec is None:
        return None
    rec.status = STATUS_REJECTED
    rec.notes.append(f"rejected: {reason}")
    return store.upsert_candidate(rec)


def summarise(store: EventStore) -> dict[str, Any]:
    snap = store.snapshot()
    snap["final_recommendation"] = FINAL_RECOMMENDATION_NO_LIVE
    return snap
