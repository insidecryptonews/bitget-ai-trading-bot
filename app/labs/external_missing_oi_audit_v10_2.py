"""ResearchOps V10.2 — Missing Open-Interest audit (research-only).

The clean ingest DROPS whole rows that lack ``oi_usd_close`` (it is a
required field), so missing-OI is invisible downstream. This tool audits
the RAW perp_market_state rows to characterise the missing-OI footprint
BEFORE deciding whether the OI-based buckets are trustworthy. It does NOT
reconstruct or refetch anything — it only measures and recommends.

HARD CONTRACT — research only: no orders, no private endpoints, no DB
writes, no runtime touched, no network. Reads only local raw rows.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from .external_edge_schemas_v10_1 import normalize_timestamp_to_ms

# Status vocabulary.
STATUS_DATA_OK = "DATA_OK"
STATUS_LOW = "MISSING_OI_LOW"
STATUS_MODERATE = "MISSING_OI_MODERATE"
STATUS_HIGH = "MISSING_OI_HIGH"
STATUS_CLUSTERED = "MISSING_OI_CLUSTERED"
STATUS_NEED_MORE = "NEED_MORE_DATA"

# Recommendation vocabulary.
REC_IGNORE_NON_OI = "IGNORE_FOR_NON_OI_BUCKETS"
REC_BLOCK_OI = "BLOCK_OI_BUCKETS"
REC_REFETCH = "REFETCH_HISTORY"
REC_CROSSCHECK = "CROSSCHECK_PROVIDER"
REC_RECONSTRUCT_IF_SAFE = "RECONSTRUCT_ONLY_IF_SAFE"

# Thresholds.
LOW_MAX = 0.05
MODERATE_MAX = 0.15
CLUSTER_MIN_RUN = 3            # consecutive missing bars => a "run"
CLUSTER_FRACTION_THRESHOLD = 0.50  # fraction of missing rows inside runs
FUNDING_Z_LOOKBACK = 168
FUNDING_Z_EXTREME = 1.5


def _is_finite(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return math.isfinite(float(v))
    if isinstance(v, str) and v.strip():
        try:
            return math.isfinite(float(v))
        except ValueError:
            return False
    return False


def _oi_missing(row: dict[str, Any]) -> bool:
    return not _is_finite(row.get("oi_usd_close"))


def _f(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v.strip():
        try:
            return float(v)
        except ValueError:
            return None
    return None


@dataclass
class MissingOiAuditReport:
    hours: int = 2160
    total_rows: int = 0
    rows_with_oi: int = 0
    rows_missing_oi: int = 0
    missing_ratio_global: float = 0.0
    per_symbol: dict[str, dict[str, Any]] = field(default_factory=dict)
    worst_symbol: str = ""
    eth_worse_than_btc: bool = False
    per_hour_missing_ratio: dict[str, float] = field(default_factory=dict)
    worst_day: str = ""
    worst_day_ratio: float = 0.0
    first_half_missing_ratio: float = 0.0
    second_half_missing_ratio: float = 0.0
    max_consecutive_missing: int = 0
    clustered_fraction: float = 0.0
    clustered: bool = False
    funding_extreme_bars: int = 0
    funding_extreme_with_missing_oi: int = 0
    funding_extreme_missing_ratio: float = 0.0
    notes: list[str] = field(default_factory=list)
    status: str = STATUS_NEED_MORE
    recommendations: list[str] = field(default_factory=list)
    primary_recommendation: str = ""
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _zscore(value: float, window: list[float]) -> float | None:
    if len(window) < 8:
        return None
    mu = statistics.fmean(window)
    sd = statistics.pstdev(window)
    if sd <= 0:
        return 0.0
    return (value - mu) / sd


def run_missing_oi_audit(raw_market_rows: list[dict[str, Any]] | None, *, hours: int = 2160) -> MissingOiAuditReport:
    """Audit missing ``oi_usd_close`` over RAW perp_market_state rows."""
    report = MissingOiAuditReport(hours=int(hours))
    rows = list(raw_market_rows or [])
    report.total_rows = len(rows)
    if not rows:
        report.status = STATUS_NEED_MORE
        report.notes.append("no_raw_market_rows")
        return report

    # Normalize + bucket by symbol with (ts, missing, funding).
    by_symbol: dict[str, list[tuple[int, bool, float | None]]] = {}
    all_ts: list[int] = []
    per_hour_counts: dict[int, list[int]] = {}   # hour -> [missing, total]
    per_day_counts: dict[str, list[int]] = {}    # date -> [missing, total]
    missing_total = 0
    for r in rows:
        ts = normalize_timestamp_to_ms(r.get("timestamp"))
        sym = str(r.get("symbol") or "").strip().upper()
        miss = _oi_missing(r)
        if miss:
            missing_total += 1
        if ts is None or not sym:
            continue
        all_ts.append(ts)
        by_symbol.setdefault(sym, []).append((ts, miss, _f(r.get("funding_rate"))))
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        hh = dt.hour
        per_hour_counts.setdefault(hh, [0, 0])
        per_hour_counts[hh][1] += 1
        if miss:
            per_hour_counts[hh][0] += 1
        day = dt.date().isoformat()
        per_day_counts.setdefault(day, [0, 0])
        per_day_counts[day][1] += 1
        if miss:
            per_day_counts[day][0] += 1

    report.rows_missing_oi = missing_total
    report.rows_with_oi = report.total_rows - missing_total
    report.missing_ratio_global = round(missing_total / report.total_rows, 4)

    # Per symbol.
    for sym, pts in by_symbol.items():
        m = sum(1 for _, miss, _ in pts if miss)
        t = len(pts)
        report.per_symbol[sym] = {"total": t, "missing": m, "ratio": round(m / t, 4) if t else 0.0}
    if report.per_symbol:
        report.worst_symbol = max(report.per_symbol, key=lambda s: report.per_symbol[s]["ratio"])
        eth = report.per_symbol.get("ETHUSDT", {}).get("ratio", 0.0)
        btc = report.per_symbol.get("BTCUSDT", {}).get("ratio", 0.0)
        report.eth_worse_than_btc = bool(eth > btc)

    # Per hour ratio.
    report.per_hour_missing_ratio = {
        str(h): round(c[0] / c[1], 4) for h, c in sorted(per_hour_counts.items()) if c[1]
    }
    # Worst day.
    if per_day_counts:
        worst = max(per_day_counts.items(), key=lambda kv: (kv[1][0] / kv[1][1]) if kv[1][1] else 0.0)
        report.worst_day = worst[0]
        report.worst_day_ratio = round(worst[1][0] / worst[1][1], 4) if worst[1][1] else 0.0

    # First/second half by calendar time.
    if all_ts:
        lo, hi = min(all_ts), max(all_ts)
        mid = lo + (hi - lo) / 2
        fh = [r for r in rows if (normalize_timestamp_to_ms(r.get("timestamp")) or hi) < mid]
        sh = [r for r in rows if (normalize_timestamp_to_ms(r.get("timestamp")) or lo) >= mid]
        report.first_half_missing_ratio = round(sum(1 for r in fh if _oi_missing(r)) / len(fh), 4) if fh else 0.0
        report.second_half_missing_ratio = round(sum(1 for r in sh if _oi_missing(r)) / len(sh), 4) if sh else 0.0

    # Clustering: per symbol, longest run of consecutive missing (time-sorted)
    # and fraction of missing rows that are part of runs >= CLUSTER_MIN_RUN.
    max_run = 0
    missing_in_runs = 0
    total_missing_for_cluster = 0
    funding_extreme = 0
    funding_extreme_missing = 0
    for sym, pts in by_symbol.items():
        pts.sort(key=lambda x: x[0])
        miss_flags = [m for _, m, _ in pts]
        # runs
        run = 0
        runs: list[int] = []
        for m in miss_flags:
            if m:
                run += 1
            else:
                if run:
                    runs.append(run)
                run = 0
        if run:
            runs.append(run)
        if runs:
            max_run = max(max_run, max(runs))
        missing_in_runs += sum(r for r in runs if r >= CLUSTER_MIN_RUN)
        total_missing_for_cluster += sum(runs)
        # funding-extreme proximity (funding present even when OI missing)
        fund = [f for _, _, f in pts]
        for i in range(FUNDING_Z_LOOKBACK, len(pts)):
            win = [x for x in fund[i - FUNDING_Z_LOOKBACK:i] if x is not None]
            fv = fund[i]
            if fv is None:
                continue
            z = _zscore(fv, win)
            if z is not None and abs(z) >= FUNDING_Z_EXTREME:
                funding_extreme += 1
                if miss_flags[i]:
                    funding_extreme_missing += 1
    report.max_consecutive_missing = max_run
    report.clustered_fraction = round(missing_in_runs / total_missing_for_cluster, 4) if total_missing_for_cluster else 0.0
    report.clustered = bool(report.max_consecutive_missing >= CLUSTER_MIN_RUN
                            and report.clustered_fraction >= CLUSTER_FRACTION_THRESHOLD
                            and report.missing_ratio_global > 0.02)
    report.funding_extreme_bars = funding_extreme
    report.funding_extreme_with_missing_oi = funding_extreme_missing
    report.funding_extreme_missing_ratio = round(funding_extreme_missing / funding_extreme, 4) if funding_extreme else 0.0

    # Notes.
    if report.eth_worse_than_btc:
        report.notes.append("eth_missing_oi_worse_than_btc")
    if report.funding_extreme_missing_ratio > report.missing_ratio_global + 0.05:
        report.notes.append("missing_oi_over_represented_near_funding_extremes")
    report.notes.append("oi_bucket_proximity_undecidable_when_oi_missing")

    # Status.
    ratio = report.missing_ratio_global
    if report.clustered:
        report.status = STATUS_CLUSTERED
    elif ratio > MODERATE_MAX:
        report.status = STATUS_HIGH
    elif ratio > LOW_MAX:
        report.status = STATUS_MODERATE
    elif ratio > 0.02:
        report.status = STATUS_LOW
    else:
        report.status = STATUS_DATA_OK

    # Recommendations.
    if report.status == STATUS_DATA_OK:
        recs = [REC_IGNORE_NON_OI]
    elif report.status == STATUS_LOW:
        recs = [REC_IGNORE_NON_OI]
    elif report.status == STATUS_MODERATE:
        recs = [REC_BLOCK_OI, REC_IGNORE_NON_OI]
    elif report.status == STATUS_HIGH:
        recs = [REC_BLOCK_OI, REC_REFETCH, REC_CROSSCHECK]
    else:  # CLUSTERED
        recs = [REC_BLOCK_OI, REC_REFETCH, REC_CROSSCHECK]
    report.recommendations = recs
    report.primary_recommendation = recs[0]
    return report


AUDIT_TABLE_COLUMNS = [
    "symbol", "total", "missing", "missing_ratio",
]


def audit_table_rows(report: MissingOiAuditReport) -> list[dict[str, Any]]:
    rows = []
    for sym, d in sorted(report.per_symbol.items()):
        rows.append({"symbol": sym, "total": d["total"], "missing": d["missing"],
                     "missing_ratio": d["ratio"]})
    return rows
