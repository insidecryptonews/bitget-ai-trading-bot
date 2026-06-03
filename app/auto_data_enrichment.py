"""ResearchOps V8/V9 — Auto Data Enrichment Layer (research-only).

Provides a single read-only entry point to enrich a symbol/timeframe window
with auxiliary context (funding, spread, mark/index, open interest, volatility
aggregate, BTC/ETH correlation, session/time-of-day).

Hard safety:
- never calls private endpoints,
- never writes to the DB from this module,
- never blocks the bot if a source is missing (returns NEED_DATA),
- never invents magnitudes,
- never bypasses the operator loop.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


FINAL_RECOMMENDATION_NO_LIVE = "NO LIVE"

ENRICHMENT_STATUS_OK = "OK"
ENRICHMENT_STATUS_NEED_DATA = "NEED_DATA"
ENRICHMENT_STATUS_PARTIAL = "PARTIAL"


SESSION_LABELS = {
    "asia": (0, 7),
    "europe": (7, 13),
    "us": (13, 21),
    "late_us": (21, 24),
}


@dataclass
class EnrichmentSource:
    name: str
    status: str
    value: float | str | None = None
    units: str = ""
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EnrichmentSnapshot:
    symbol: str
    timeframe: str
    hours: int
    generated_at: str
    sources: list[EnrichmentSource] = field(default_factory=list)
    overall_status: str = ENRICHMENT_STATUS_NEED_DATA
    need_data_reasons: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    no_private_endpoints_used: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "hours": self.hours,
            "generated_at": self.generated_at,
            "sources": [s.as_dict() for s in self.sources],
            "overall_status": self.overall_status,
            "need_data_reasons": list(self.need_data_reasons),
            "research_only": self.research_only,
            "paper_filter_enabled": self.paper_filter_enabled,
            "can_send_real_orders": self.can_send_real_orders,
            "no_private_endpoints_used": self.no_private_endpoints_used,
            "final_recommendation": self.final_recommendation,
        }


def _session_for(ts: datetime | None) -> str:
    if ts is None:
        return "unknown"
    h = ts.astimezone(timezone.utc).hour
    for label, (start, end) in SESSION_LABELS.items():
        if start <= h < end:
            return label
    return "unknown"


def _safe_call(db: Any, method_name: str, *args, **kwargs) -> tuple[bool, Any]:
    """Best-effort call. Returns (ok, value). Never raises."""
    method = getattr(db, method_name, None)
    if method is None or not callable(method):
        return False, None
    try:
        return True, method(*args, **kwargs)
    except Exception:
        return False, None


def _funding_source(db: Any, symbol: str, hours: int) -> EnrichmentSource:
    ok, value = _safe_call(db, "latest_funding_rate", symbol)
    if not ok or value is None:
        return EnrichmentSource(
            name="funding",
            status=ENRICHMENT_STATUS_NEED_DATA,
            notes="funding_rates_table_missing_or_empty",
        )
    try:
        rate = float(value)
    except Exception:
        return EnrichmentSource(name="funding", status=ENRICHMENT_STATUS_NEED_DATA, notes="non_numeric_value")
    return EnrichmentSource(name="funding", status=ENRICHMENT_STATUS_OK, value=rate, units="frac_per_8h")


def _spread_source(db: Any, symbol: str) -> EnrichmentSource:
    ok, value = _safe_call(db, "latest_bid_ask_spread_bps", symbol)
    if not ok or value is None:
        return EnrichmentSource(
            name="spread_bid_ask",
            status=ENRICHMENT_STATUS_NEED_DATA,
            notes="spread_table_missing_or_empty",
        )
    try:
        bps = float(value)
    except Exception:
        return EnrichmentSource(name="spread_bid_ask", status=ENRICHMENT_STATUS_NEED_DATA, notes="non_numeric_value")
    return EnrichmentSource(name="spread_bid_ask", status=ENRICHMENT_STATUS_OK, value=bps, units="bps")


def _mark_index_source(db: Any, symbol: str) -> EnrichmentSource:
    ok, value = _safe_call(db, "latest_mark_index_basis_pct", symbol)
    if not ok or value is None:
        return EnrichmentSource(
            name="mark_index_basis",
            status=ENRICHMENT_STATUS_NEED_DATA,
            notes="mark_index_table_missing_or_empty",
        )
    try:
        bps = float(value)
    except Exception:
        return EnrichmentSource(
            name="mark_index_basis", status=ENRICHMENT_STATUS_NEED_DATA, notes="non_numeric_value"
        )
    return EnrichmentSource(name="mark_index_basis", status=ENRICHMENT_STATUS_OK, value=bps, units="pct")


def _open_interest_source(db: Any, symbol: str) -> EnrichmentSource:
    ok, value = _safe_call(db, "latest_open_interest", symbol)
    if not ok or value is None:
        return EnrichmentSource(
            name="open_interest",
            status=ENRICHMENT_STATUS_NEED_DATA,
            notes="open_interest_table_missing_or_empty",
        )
    try:
        oi = float(value)
    except Exception:
        return EnrichmentSource(
            name="open_interest", status=ENRICHMENT_STATUS_NEED_DATA, notes="non_numeric_value"
        )
    return EnrichmentSource(name="open_interest", status=ENRICHMENT_STATUS_OK, value=oi, units="contract")


def _volatility_source(db: Any, symbol: str, hours: int) -> EnrichmentSource:
    ok, value = _safe_call(db, "realised_vol_pct", symbol, hours)
    if not ok or value is None:
        return EnrichmentSource(
            name="realised_volatility",
            status=ENRICHMENT_STATUS_NEED_DATA,
            notes="ohlcv_window_missing_or_empty",
        )
    try:
        vol = float(value)
    except Exception:
        return EnrichmentSource(
            name="realised_volatility", status=ENRICHMENT_STATUS_NEED_DATA, notes="non_numeric_value"
        )
    return EnrichmentSource(name="realised_volatility", status=ENRICHMENT_STATUS_OK, value=vol, units="pct")


def _btc_eth_corr_source(db: Any, symbol: str, hours: int) -> EnrichmentSource:
    ok, value = _safe_call(db, "btc_eth_correlation_pct", symbol, hours)
    if not ok or value is None:
        return EnrichmentSource(
            name="btc_eth_correlation",
            status=ENRICHMENT_STATUS_NEED_DATA,
            notes="cross_asset_window_missing",
        )
    try:
        corr = float(value)
    except Exception:
        return EnrichmentSource(
            name="btc_eth_correlation", status=ENRICHMENT_STATUS_NEED_DATA, notes="non_numeric_value"
        )
    return EnrichmentSource(name="btc_eth_correlation", status=ENRICHMENT_STATUS_OK, value=corr, units="pct")


def _session_source(now_utc: datetime | None) -> EnrichmentSource:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    label = _session_for(now_utc)
    if label == "unknown":
        return EnrichmentSource(
            name="session",
            status=ENRICHMENT_STATUS_NEED_DATA,
            notes="unknown_session_window",
        )
    return EnrichmentSource(name="session", status=ENRICHMENT_STATUS_OK, value=label)


def enrich_snapshot(
    db: Any,
    *,
    symbol: str,
    timeframe: str = "5m",
    hours: int = 24,
    now_utc: datetime | None = None,
) -> EnrichmentSnapshot:
    """Build a research-only enrichment snapshot for ``symbol``.

    Each source returns ``NEED_DATA`` independently when the underlying table
    or window is missing. No private endpoints, no writes.
    """

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    sources = [
        _funding_source(db, symbol, hours),
        _spread_source(db, symbol),
        _mark_index_source(db, symbol),
        _open_interest_source(db, symbol),
        _volatility_source(db, symbol, hours),
        _btc_eth_corr_source(db, symbol, hours),
        _session_source(now_utc),
    ]
    # ``session`` is informational (computed from the clock, not the DB) and
    # must not skew the overall status — otherwise an empty DB would always
    # report PARTIAL just because the session source is always OK.
    data_sources = [s for s in sources if s.name != "session"]
    ok_count = sum(1 for s in data_sources if s.status == ENRICHMENT_STATUS_OK)
    need = [s.name for s in data_sources if s.status == ENRICHMENT_STATUS_NEED_DATA]
    if ok_count == len(data_sources):
        overall = ENRICHMENT_STATUS_OK
    elif ok_count == 0:
        overall = ENRICHMENT_STATUS_NEED_DATA
    else:
        overall = ENRICHMENT_STATUS_PARTIAL
    return EnrichmentSnapshot(
        symbol=symbol,
        timeframe=timeframe,
        hours=int(hours),
        generated_at=now_utc.isoformat(),
        sources=sources,
        overall_status=overall,
        need_data_reasons=need,
    )


def summarise_enrichment(
    db: Any,
    *,
    symbols: Iterable[str],
    timeframe: str = "5m",
    hours: int = 24,
) -> dict[str, Any]:
    snaps = [enrich_snapshot(db, symbol=s, timeframe=timeframe, hours=hours) for s in symbols]
    return {
        "timeframe": timeframe,
        "hours": int(hours),
        "snapshots": [s.as_dict() for s in snaps],
        "symbols_ok": [s.symbol for s in snaps if s.overall_status == ENRICHMENT_STATUS_OK],
        "symbols_partial": [s.symbol for s in snaps if s.overall_status == ENRICHMENT_STATUS_PARTIAL],
        "symbols_need_data": [s.symbol for s in snaps if s.overall_status == ENRICHMENT_STATUS_NEED_DATA],
        "research_only": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "no_private_endpoints_used": True,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }
