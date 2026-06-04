"""ResearchOps V8.1 — Listing Tracker (research-only).

Detects symbols within the first N days after listing on a venue and emits
``listing`` events. Pure read-only: depends on whatever the project DB or a
caller can provide via ``listing_records(...)``; never calls private
endpoints; returns ``NEED_DATA`` when the source is unavailable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE


@dataclass
class ListingRecord:
    symbol: str
    venue: str
    listed_at_utc: datetime
    fdv_usd: float | None = None
    float_pct: float | None = None
    headline_size_usd: float | None = None

    def age_days(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        if self.listed_at_utc.tzinfo is None:
            base = self.listed_at_utc.replace(tzinfo=timezone.utc)
        else:
            base = self.listed_at_utc
        return max(0, (now.astimezone(timezone.utc) - base).days)


@dataclass
class ListingAuditReport:
    generated_at: str
    venue_filter: list[str]
    window_days: int
    records: list[dict[str, Any]] = field(default_factory=list)
    need_data_reasons: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    no_private_endpoints_used: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_call(db: Any, method: str, *args, **kwargs) -> tuple[bool, Any]:
    fn = getattr(db, method, None)
    if fn is None or not callable(fn):
        return False, None
    try:
        return True, fn(*args, **kwargs)
    except Exception:
        return False, None


def fetch_listing_records(
    db: Any,
    *,
    venues: Iterable[str] | None = None,
    window_days: int = 30,
) -> list[ListingRecord]:
    """Best-effort fetch of recent listings via the DB wrapper.

    Expects ``db.recent_listings(venues, window_days)`` to be available. If
    not, returns an empty list — caller decides the NEED_DATA reaction.
    """
    venues_list = list(venues) if venues else ["bitget", "binance", "bybit"]
    ok, value = _safe_call(db, "recent_listings", venues_list, window_days)
    if not ok or not value:
        return []
    records: list[ListingRecord] = []
    for raw in value:
        try:
            listed_at = raw.get("listed_at_utc")
            if isinstance(listed_at, str):
                listed_at = datetime.fromisoformat(listed_at)
            records.append(ListingRecord(
                symbol=str(raw.get("symbol", "")).upper(),
                venue=str(raw.get("venue", "")).lower(),
                listed_at_utc=listed_at,
                fdv_usd=raw.get("fdv_usd"),
                float_pct=raw.get("float_pct"),
                headline_size_usd=raw.get("headline_size_usd"),
            ))
        except Exception:
            continue
    return records


def build_listing_audit(
    db: Any,
    *,
    venues: Iterable[str] | None = None,
    window_days: int = 30,
    now: datetime | None = None,
) -> ListingAuditReport:
    """Build a research-only audit of recent listings."""
    venue_list = list(venues) if venues else ["bitget", "binance", "bybit"]
    records = fetch_listing_records(db, venues=venue_list, window_days=window_days)
    need_data: list[str] = []
    if not records:
        need_data.append("no_recent_listings_method_or_empty")
    payload: list[dict[str, Any]] = []
    for r in records:
        payload.append({
            "symbol": r.symbol,
            "venue": r.venue,
            "listed_at_utc": r.listed_at_utc.isoformat()
                if hasattr(r.listed_at_utc, "isoformat") else str(r.listed_at_utc),
            "age_days": r.age_days(now=now),
            "fdv_usd": r.fdv_usd,
            "float_pct": r.float_pct,
            "headline_size_usd": r.headline_size_usd,
        })
    return ListingAuditReport(
        generated_at=(now or datetime.now(timezone.utc)).isoformat(),
        venue_filter=venue_list,
        window_days=int(window_days),
        records=payload,
        need_data_reasons=need_data,
    )
