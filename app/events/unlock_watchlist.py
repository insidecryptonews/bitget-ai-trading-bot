"""ResearchOps V8.1 — Unlock Watchlist (research-only).

Tracks upcoming token unlocks. Cross-checks two independent sources
(``tokenomist`` and ``tokenunlocks`` by convention) and flags conflicts when
``size_pct_circ`` or ``unlock_date`` disagree.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE


@dataclass
class UnlockRecord:
    token: str
    unlock_date: str
    source: str
    headline_size_usd: float | None = None
    size_pct_circ: float | None = None
    float_pct: float | None = None
    fdv_usd: float | None = None
    notes: str = ""


@dataclass
class UnlockAuditReport:
    generated_at: str
    window_days: int
    records: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
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


def fetch_unlocks(db: Any, *, window_days: int = 60) -> list[UnlockRecord]:
    """Best-effort fetch. Returns empty list when no source is wired up."""
    ok, value = _safe_call(db, "upcoming_token_unlocks", int(window_days))
    if not ok or not value:
        return []
    records: list[UnlockRecord] = []
    for raw in value:
        try:
            records.append(UnlockRecord(
                token=str(raw.get("token", "")).upper(),
                unlock_date=str(raw.get("unlock_date", "")),
                source=str(raw.get("source", "unknown")).lower(),
                headline_size_usd=raw.get("headline_size_usd"),
                size_pct_circ=raw.get("size_pct_circ"),
                float_pct=raw.get("float_pct"),
                fdv_usd=raw.get("fdv_usd"),
                notes=str(raw.get("notes", "")),
            ))
        except Exception:
            continue
    return records


def _by_token(records: list[UnlockRecord]) -> dict[str, list[UnlockRecord]]:
    out: dict[str, list[UnlockRecord]] = {}
    for r in records:
        out.setdefault(r.token, []).append(r)
    return out


def cross_check_sources(records: list[UnlockRecord]) -> list[dict[str, Any]]:
    """Return conflict entries when two sources disagree on the same token."""
    conflicts: list[dict[str, Any]] = []
    grouped = _by_token(records)
    for token, rs in grouped.items():
        sources = {r.source for r in rs}
        if len(sources) < 2:
            continue
        # Compare every pair
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                a, b = rs[i], rs[j]
                if a.source == b.source:
                    continue
                if a.unlock_date != b.unlock_date:
                    conflicts.append({
                        "token": token,
                        "field": "unlock_date",
                        "source_a": a.source, "value_a": a.unlock_date,
                        "source_b": b.source, "value_b": b.unlock_date,
                    })
                if a.size_pct_circ is not None and b.size_pct_circ is not None:
                    denom = max(abs(a.size_pct_circ), abs(b.size_pct_circ), 1e-9)
                    if abs(a.size_pct_circ - b.size_pct_circ) / denom > 0.10:
                        conflicts.append({
                            "token": token,
                            "field": "size_pct_circ",
                            "source_a": a.source, "value_a": a.size_pct_circ,
                            "source_b": b.source, "value_b": b.size_pct_circ,
                        })
    return conflicts


def build_unlock_audit(
    db: Any,
    *,
    window_days: int = 60,
    now: datetime | None = None,
) -> UnlockAuditReport:
    records = fetch_unlocks(db, window_days=window_days)
    payload = [asdict(r) for r in records]
    need_data: list[str] = []
    if not records:
        need_data.append("no_upcoming_unlocks_method_or_empty")
    conflicts = cross_check_sources(records) if records else []
    return UnlockAuditReport(
        generated_at=(now or datetime.now(timezone.utc)).isoformat(),
        window_days=int(window_days),
        records=payload,
        conflicts=conflicts,
        need_data_reasons=need_data,
    )
