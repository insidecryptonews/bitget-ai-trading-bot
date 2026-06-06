"""V8.2.9 — EdgeGuard Repeat Dedup for Research (research-only).

Reduces research duplicates caused by EdgeGuard repeat blocks without
touching runtime. The runtime EdgeGuard logic is NOT modified; this is
a research-side dataset cleanup operating purely on a copy of the
observation set.

Hard contract:

- Read-only over the input rows.
- No DB writes.
- No EdgeGuard enforce activation.
- No paper filter changes.
- Fingerprint uses ONLY prefix-only features (symbol, side, regime,
  strategy, candle bucket, edgeguard reason, entry_price bucket,
  candidate fingerprint). It never reads forward returns, MFE/MAE,
  barrier hits, or any other ex-post field.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK


DEFAULT_CANDLE_MINUTES = 5
DEFAULT_PRICE_BUCKET_BPS = 5  # 5 bps bucket — coarse enough to collapse
                              # micro-noise but tight enough to keep
                              # genuinely different price regimes apart.


def _candle_bucket(ts: str, minutes: int = DEFAULT_CANDLE_MINUTES) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        floored = (dt.minute // max(minutes, 1)) * max(minutes, 1)
        return dt.replace(minute=floored, second=0, microsecond=0).isoformat()
    except Exception:
        return str(ts)[:16]


def _price_bucket(price: Any, bps: int = DEFAULT_PRICE_BUCKET_BPS) -> str:
    if not isinstance(price, (int, float)):
        return ""
    try:
        p = float(price)
        if p <= 0:
            return ""
        step = p * (bps / 10000.0)
        if step <= 0:
            return f"{p:.6f}"
        return f"{int(p / step)}"
    except Exception:
        return ""


def _edgeguard_reason(row: dict[str, Any]) -> str:
    return str(
        row.get("edgeguard_reason", "")
        or row.get("reason", "")
        or row.get("blocked_by", "")
    ).lower()


def edgeguard_repeat_fingerprint(row: dict[str, Any]) -> str:
    """Prefix-only fingerprint for EdgeGuard repeat detection.

    Uses ONLY ex-ante features. Never reads forward returns, MFE/MAE,
    barrier hits, or any other ex-post field.
    """
    parts = [
        str(row.get("symbol", "")).upper(),
        str(row.get("side", "")).upper(),
        str(row.get("regime", "")).upper(),
        str(row.get("strategy", "")),
        _candle_bucket(str(row.get("timestamp", ""))),
        _edgeguard_reason(row),
        _price_bucket(row.get("entry_price")),
        str(row.get("candidate_fingerprint", "") or ""),
    ]
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class DedupReport:
    hours: int
    generated_at: str
    raw_rows: int = 0
    dedup_rows: int = 0
    duplicate_ratio_before: float = 0.0
    duplicate_ratio_after: float = 0.0
    edgeguard_repeat_blocks_removed: int = 0
    unique_independent_candidates: int = 0
    by_edgeguard_reason: dict[str, int] = field(default_factory=dict)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def dedup_edgeguard_repeats(
    rows: Iterable[dict[str, Any]],
    *,
    hours: int = 0,
) -> tuple[list[dict[str, Any]], DedupReport]:
    """Return ``(deduped_rows, report)`` for research.

    Each kept row is a copy of the first independent observation per
    fingerprint with the marker ``edgeguard_repeat_seen_again=False``.
    Repeated EdgeGuard-block rows are dropped from the research dataset
    but the runtime EdgeGuard pipeline is NOT touched.
    """
    rows_list = list(rows)
    report = DedupReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
        raw_rows=len(rows_list),
    )
    if not rows_list:
        return [], report
    seen: dict[str, dict[str, Any]] = {}
    edgeguard_count: dict[str, int] = {}
    edgeguard_removed = 0
    for r in rows_list:
        fp = edgeguard_repeat_fingerprint(r)
        reason = _edgeguard_reason(r)
        if reason:
            edgeguard_count[reason] = edgeguard_count.get(reason, 0) + 1
        if fp in seen:
            if "edge_guard" in reason or "watch_only" in reason:
                edgeguard_removed += 1
            continue
        kept = dict(r)
        kept["edgeguard_repeat_seen_again"] = False
        seen[fp] = kept
    dedup = list(seen.values())
    report.dedup_rows = len(dedup)
    report.duplicate_ratio_before = (
        (len(rows_list) - len(dedup)) / max(len(rows_list), 1)
    )
    # Post-dedup duplicate ratio is zero by construction (each kept row
    # has a unique fingerprint). We still expose the field for parity
    # with consumers.
    report.duplicate_ratio_after = 0.0
    report.edgeguard_repeat_blocks_removed = edgeguard_removed
    report.unique_independent_candidates = len(dedup)
    report.by_edgeguard_reason = edgeguard_count
    report.status = STATUS_OK
    return dedup, report
