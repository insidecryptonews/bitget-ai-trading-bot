"""ResearchOps V8.1 — Pack ``event-v1`` (research-only).

Read-only export for sharing with ChatGPT or saving offline. Never includes
secrets, ``.env`` values or DB dumps.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, SUPPORTED_FAMILIES, VALID_STATUSES
from .event_store import EventStore
from .listing_tracker import build_listing_audit
from .perp_availability_checker import (
    batch_check_perp_availability,
    summarise_perp_audit,
)
from .shortability_score import batch_shortability, summarise_shortability
from .unlock_watchlist import build_unlock_audit


def build_event_pack_v1(
    config: Any,
    db: Any,
    *,
    store: EventStore | None = None,
    sample_symbols: Iterable[str] | None = None,
    window_days: int = 60,
    listing_window_days: int = 30,
) -> dict[str, Any]:
    store = store or EventStore()
    snap = store.snapshot()

    listing = build_listing_audit(db, window_days=listing_window_days).as_dict()
    unlocks = build_unlock_audit(db, window_days=window_days).as_dict()

    sample_list = list(sample_symbols or ["BTCUSDT", "ETHUSDT", "DOTUSDT"])
    perp_results = batch_check_perp_availability(db, symbols=sample_list)
    perp_summary = summarise_perp_audit(perp_results)
    short_results = batch_shortability(
        db,
        symbols_with_perp=[(r.symbol, r.perp_available_bitget) for r in perp_results],
    )
    short_summary = summarise_shortability(short_results)

    candidates = store.list_candidates()
    by_family = {f: sum(1 for c in candidates if c.family == f) for f in SUPPORTED_FAMILIES}
    by_status = {s: sum(1 for c in candidates if c.status == s) for s in VALID_STATUSES}
    source_conflicts = sum(1 for c in candidates if c.source_conflict_flag)
    need_data_count = sum(1 for c in candidates if c.status == "NEED_DATA")

    pack = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pack_version": "event_v1",
        "total_events": len(candidates),
        "events_by_family": by_family,
        "candidates_by_status": by_status,
        "source_conflicts": source_conflicts,
        "perp_availability_summary": perp_summary,
        "top_shortability_scores": short_summary.get("top", []),
        "need_data_counts": {
            "candidates_need_data": need_data_count,
            "shortability_need_data": short_summary.get("need_data", 0),
            "listings_need_data": listing.get("need_data_reasons", []),
            "unlocks_need_data": unlocks.get("need_data_reasons", []),
        },
        "listing_audit": listing,
        "unlock_audit": unlocks,
        "store_snapshot": snap,
        "research_only": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "no_private_endpoints_used": True,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }
    return pack


def render_event_pack_v1_text(payload: dict[str, Any]) -> str:
    lines = ["RESEARCH PACK EVENT V1 START"]
    lines.append(f"generated_at: {payload.get('generated_at')}")
    lines.append(f"pack_version: {payload.get('pack_version')}")
    lines.append(f"total_events: {payload.get('total_events')}")
    fam = payload.get("events_by_family") or {}
    for k in sorted(fam):
        lines.append(f"events_by_family {k}: {fam[k]}")
    stat = payload.get("candidates_by_status") or {}
    for k in sorted(stat):
        lines.append(f"candidates_by_status {k}: {stat[k]}")
    lines.append(f"source_conflicts: {payload.get('source_conflicts')}")
    perp = payload.get("perp_availability_summary") or {}
    lines.append(
        f"perp_availability: total={perp.get('total')} "
        f"with_perp={perp.get('with_perp_bitget')} without={perp.get('without_perp_bitget')}"
    )
    lines.append(f"top_shortability_count: {len(payload.get('top_shortability_scores') or [])}")
    nd = payload.get("need_data_counts") or {}
    for k in sorted(nd):
        lines.append(f"need_data {k}: {nd[k]}")
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "no_private_endpoints_used: true",
        f"final_recommendation: {payload.get('final_recommendation')}",
        "RESEARCH PACK EVENT V1 END",
    ])
    return "\n".join(lines)
