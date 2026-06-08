"""ResearchOps V10 — Funding / Open Interest / Liquidation research.

Prepares the *Leverage Stress Trades* research family: exploiting badly
positioned leverage (crowded longs/shorts, extreme funding, high OI,
liquidation cascades). This is a research scaffold, NOT a signal source.

HARD CONTRACT — research only:

- never calls private endpoints, never opens orders, never writes DB,
- never fabricates data: no funding/OI/liquidation rows => ``NEED_DATA``,
- the only input is a *local* CSV/JSON handed in by the operator,
- output is always ``final_recommendation = NO LIVE``.

External rows are routed by a ``data_type`` column (``funding`` /
``open_interest`` / ``liquidation``); when absent, the type is inferred
from the present numeric fields. Z-scores and percentiles are computed
per symbol *only from the provided series* — no lookahead, no synthetic
fill.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE
from .edge_data_foundation_v10 import (
    DATA_BAD,
    DATA_NEED,
    DATA_OK,
    DATA_WARNING,
    FRESHNESS_UNKNOWN,
    _f,
    _parse_ts,
    load_external_data,
    validate_external_rows,
)

DECISION_NEED_DATA = "NEED_DATA"
DECISION_WATCH_ONLY = "WATCH_ONLY"
DECISION_REJECT = "REJECT"
DECISION_IMPLEMENT_FIRST = "IMPLEMENT_FIRST_RESEARCH"

# Extreme-funding threshold on the per-symbol z-score.
FUNDING_Z_EXTREME = 2.0
# Extreme OI threshold on the per-symbol z-score.
OI_Z_EXTREME = 2.0
# Minimum points per symbol to trust a z-score / percentile.
MIN_POINTS_FOR_STATS = 8
# Minimum total events to consider an event study viable.
MIN_EVENTS_FOR_STUDY = 5


@dataclass
class FundingOiLiquidationReport:
    hours: int = 24
    generated_at: str = ""
    source_label: str = ""
    rows_loaded: int = 0
    valid_rows: int = 0
    symbols: list[str] = field(default_factory=list)
    funding_points: int = 0
    oi_points: int = 0
    liquidation_points: int = 0
    funding_extreme_events: int = 0
    oi_extreme_events: int = 0
    oi_price_divergence_events: int = 0
    liquidation_cluster_events: int = 0
    crowded_long_flush_events: int = 0
    crowded_short_squeeze_events: int = 0
    event_count: int = 0
    data_quality_status: str = DATA_NEED
    freshness_status: str = FRESHNESS_UNKNOWN
    event_study_ready: bool = False
    backtest_ready: bool = False
    best_hypothesis: str = ""
    required_data_missing: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    decision: str = DECISION_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _infer_type(row: dict[str, Any]) -> str:
    dt = str(row.get("data_type") or "").strip().lower()
    if dt in ("funding", "open_interest", "oi", "liquidation"):
        return "open_interest" if dt == "oi" else dt
    if row.get("funding_rate") is not None:
        return "funding"
    if row.get("open_interest") is not None:
        return "open_interest"
    if row.get("liquidation_usd") is not None:
        return "liquidation"
    return "unknown"


def _zscore(value: float, series: list[float]) -> float | None:
    if len(series) < MIN_POINTS_FOR_STATS:
        return None
    try:
        mu = statistics.fmean(series)
        sd = statistics.pstdev(series)
    except statistics.StatisticsError:
        return None
    if sd <= 0:
        return 0.0
    return (value - mu) / sd


def _percentile_rank(value: float, series: list[float]) -> float | None:
    if len(series) < MIN_POINTS_FOR_STATS:
        return None
    below = sum(1 for x in series if x <= value)
    return below / len(series)


def analyze_funding_oi_liquidation(
    rows: Iterable[dict[str, Any]] | None,
    *,
    hours: int = 24,
    source_label: str = "",
) -> FundingOiLiquidationReport:
    """Pure analysis over already-loaded external rows."""
    report = FundingOiLiquidationReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_label=source_label,
    )
    row_list = list(rows or [])
    report.rows_loaded = len(row_list)

    if not row_list:
        report.required_data_missing = ["funding_rate", "open_interest", "liquidation_usd"]
        report.blockers = ["no_external_data"]
        report.decision = DECISION_NEED_DATA
        report.best_hypothesis = "NONE_NEED_DATA"
        return report

    vr = validate_external_rows(
        row_list,
        value_fields=("funding_rate", "open_interest", "liquidation_usd", "metric_value"),
    )
    report.valid_rows = len(vr.valid)
    report.freshness_status = (
        "FRESH" if any(r.get("freshness_status") == "FRESH" for r in vr.valid)
        else ("STALE" if vr.valid else FRESHNESS_UNKNOWN)
    )

    # Bucket valid rows by type and symbol.
    funding_by_sym: dict[str, list[tuple[datetime | None, float]]] = {}
    oi_by_sym: dict[str, list[tuple[datetime | None, float]]] = {}
    liq_rows: list[dict[str, Any]] = []
    price_by_sym: dict[str, list[tuple[datetime | None, float]]] = {}

    for r in vr.valid:
        sym = str(r.get("symbol") or "").strip().upper()
        ts = _parse_ts(r.get("timestamp"))
        kind = _infer_type(r)
        price = _f(r.get("close")) if r.get("close") is not None else _f(r.get("price"))
        if price is not None:
            price_by_sym.setdefault(sym, []).append((ts, price))
        if kind == "funding":
            fr = _f(r.get("funding_rate"))
            if fr is None:
                fr = _f(r.get("metric_value"))
            if fr is not None:
                funding_by_sym.setdefault(sym, []).append((ts, fr))
        elif kind == "open_interest":
            oi = _f(r.get("open_interest"))
            if oi is None:
                oi = _f(r.get("metric_value"))
            if oi is not None:
                oi_by_sym.setdefault(sym, []).append((ts, oi))
        elif kind == "liquidation":
            liq_rows.append(r)

    report.funding_points = sum(len(v) for v in funding_by_sym.values())
    report.oi_points = sum(len(v) for v in oi_by_sym.values())
    report.liquidation_points = len(liq_rows)
    report.symbols = sorted(
        set(funding_by_sym) | set(oi_by_sym)
        | {str(r.get("symbol") or "").strip().upper() for r in liq_rows}
    )

    # Funding extremes (per symbol z-score).
    for sym, series in funding_by_sym.items():
        vals = [v for _, v in series]
        for _, v in series:
            z = _zscore(v, vals)
            if z is not None and abs(z) >= FUNDING_Z_EXTREME:
                report.funding_extreme_events += 1
                # Crowded longs => funding strongly positive (longs pay).
                if z >= FUNDING_Z_EXTREME:
                    report.crowded_long_flush_events += 1
                elif z <= -FUNDING_Z_EXTREME:
                    report.crowded_short_squeeze_events += 1

    # OI extremes + momentum.
    for sym, series in oi_by_sym.items():
        vals = [v for _, v in series]
        for _, v in series:
            z = _zscore(v, vals)
            if z is not None and abs(z) >= OI_Z_EXTREME:
                report.oi_extreme_events += 1
        # OI-price divergence: OI rising while price falling (or vice versa).
        prices = sorted(price_by_sym.get(sym, []), key=lambda t: (t[0] or datetime.min.replace(tzinfo=timezone.utc)))
        ois = sorted(series, key=lambda t: (t[0] or datetime.min.replace(tzinfo=timezone.utc)))
        if len(prices) >= 2 and len(ois) >= 2:
            d_price = prices[-1][1] - prices[0][1]
            d_oi = ois[-1][1] - ois[0][1]
            if d_price != 0 and d_oi != 0 and (d_price > 0) != (d_oi > 0):
                report.oi_price_divergence_events += 1

    # Liquidation clusters (placeholder): rows explicitly flagged or with
    # a cluster_flag / above-zero usd grouped close in time.
    for r in liq_rows:
        if str(r.get("cluster_flag")).strip().lower() in ("1", "true", "yes"):
            report.liquidation_cluster_events += 1

    report.event_count = (
        report.funding_extreme_events
        + report.oi_extreme_events
        + report.oi_price_divergence_events
        + report.liquidation_cluster_events
    )

    # Data quality.
    if not vr.valid:
        report.data_quality_status = DATA_BAD
    else:
        bad_ratio = len(vr.rejected) / max(1, len(row_list))
        report.data_quality_status = (
            DATA_OK if bad_ratio == 0 else (DATA_WARNING if bad_ratio < 0.5 else DATA_BAD)
        )

    # Readiness + decision.
    missing: list[str] = []
    if report.funding_points == 0:
        missing.append("funding_rate")
    if report.oi_points == 0:
        missing.append("open_interest")
    if report.liquidation_points == 0:
        missing.append("liquidation_usd")
    if not any(price_by_sym.values()):
        missing.append("price_for_divergence")
    report.required_data_missing = missing

    blockers: list[str] = []
    if report.data_quality_status == DATA_BAD:
        blockers.append("data_quality_bad")
    if report.event_count < MIN_EVENTS_FOR_STUDY:
        blockers.append("insufficient_events_for_study")
    if report.freshness_status == "STALE":
        blockers.append("stale_external_data")
    report.blockers = blockers

    report.event_study_ready = (
        report.event_count >= MIN_EVENTS_FOR_STUDY
        and report.data_quality_status != DATA_BAD
    )
    # Backtesting leverage-stress trades requires aligned OHLCV which this
    # research scaffold does not yet join → always False here (honest).
    report.backtest_ready = False

    # Best hypothesis (descriptive, never actionable).
    if report.crowded_long_flush_events >= report.crowded_short_squeeze_events and report.crowded_long_flush_events > 0:
        report.best_hypothesis = "CROWDED_LONG_FLUSH_SHORT_BIAS"
    elif report.crowded_short_squeeze_events > 0:
        report.best_hypothesis = "CROWDED_SHORT_SQUEEZE_LONG_BIAS"
    elif report.oi_price_divergence_events > 0:
        report.best_hypothesis = "OI_PRICE_DIVERGENCE"
    else:
        report.best_hypothesis = "NONE_YET"

    # Decision.
    if report.valid_rows == 0:
        report.decision = DECISION_NEED_DATA
    elif report.data_quality_status == DATA_BAD:
        report.decision = DECISION_NEED_DATA
    elif report.event_study_ready:
        report.decision = DECISION_IMPLEMENT_FIRST
    elif report.event_count == 0 and report.funding_points + report.oi_points >= 3 * MIN_POINTS_FOR_STATS:
        # Enough sample but zero exploitable structure → honest REJECT.
        report.decision = DECISION_REJECT
    else:
        report.decision = DECISION_WATCH_ONLY
    return report


def run_funding_oi_liquidation_research(
    *,
    hours: int = 24,
    external_data_path: str | None = None,
) -> FundingOiLiquidationReport:
    """Entry point: load local external data (if any) and analyze."""
    rows, source_label = load_external_data(external_data_path)
    return analyze_funding_oi_liquidation(rows, hours=hours, source_label=source_label)
