"""ResearchOps V8.1 — Event Foundation (research-only).

This package introduces an event-driven research layer that runs in parallel
with the existing score-based pipeline. Nothing in this package opens orders,
flips paper-filter flags, mutates leverage/margin/sizing, or calls private
endpoints. All outputs end with ``FINAL_RECOMMENDATION = NO LIVE``.

Families supported in V8.1 (foundation):

- ``crowding_oi_funding`` — funding/OI/liquidation crowding + structure break.
- ``post_listing_high_fdv`` — post-listing high-FDV / low-float dump.
- ``token_unlock`` — token unlock short, with source-conflict control.
- ``macro_scheduled_context`` — context only, never actionable.
"""

from __future__ import annotations

FINAL_RECOMMENDATION_NO_LIVE = "NO LIVE"

FAMILY_CROWDING_OI_FUNDING = "crowding_oi_funding"
FAMILY_POST_LISTING_HIGH_FDV = "post_listing_high_fdv"
FAMILY_TOKEN_UNLOCK = "token_unlock"
FAMILY_MACRO_SCHEDULED_CONTEXT = "macro_scheduled_context"

SUPPORTED_FAMILIES: tuple[str, ...] = (
    FAMILY_CROWDING_OI_FUNDING,
    FAMILY_POST_LISTING_HIGH_FDV,
    FAMILY_TOKEN_UNLOCK,
    FAMILY_MACRO_SCHEDULED_CONTEXT,
)

STATUS_DETECTED = "DETECTED"
STATUS_NEED_DATA = "NEED_DATA"
STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"
STATUS_NOT_ACTIONABLE_NO_PERP = "NOT_ACTIONABLE_NO_PERP"
STATUS_LOW_SHORTABILITY = "LOW_SHORTABILITY"
STATUS_ACTIONABLE_LABEL_ONLY = "ACTIONABLE_LABEL_ONLY"
STATUS_REJECTED = "REJECTED"
STATUS_CONTEXT_ONLY = "CONTEXT_ONLY"

VALID_STATUSES: tuple[str, ...] = (
    STATUS_DETECTED,
    STATUS_NEED_DATA,
    STATUS_NEEDS_REVIEW,
    STATUS_NOT_ACTIONABLE_NO_PERP,
    STATUS_LOW_SHORTABILITY,
    STATUS_ACTIONABLE_LABEL_ONLY,
    STATUS_REJECTED,
    STATUS_CONTEXT_ONLY,
)
