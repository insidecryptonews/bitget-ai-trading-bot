"""ResearchOps V10 — Edge Data Foundation (research-only).

Structures and validators for *external* edge data (funding, open
interest, liquidations, catalysts, unlocks, listings, news). This module
is the shared substrate for the V10 research labs:

- ``funding_oi_liquidation_research_v10``
- ``token_unlock_post_listing_research_v10``
- ``event_catalyst_layer_v10``
- ``edge_discovery_orchestrator_v10``

HARD CONTRACT — research only. This module:

- never opens orders / never calls private endpoints,
- never mutates ``LIVE_TRADING`` / ``ENABLE_PAPER_POLICY_FILTER`` /
  ``can_send_real_orders`` / ``allow_real_writes``,
- never writes to the database,
- never fabricates data: missing external data => ``NEED_DATA`` (not an
  error, not a fake candidate),
- defaults ``actionability`` to ``NOT_ACTIONABLE``.

The only I/O permitted is reading a *local* CSV/JSON file the operator
explicitly hands in via ``--external-data-path``. No network, no API
keys, no secrets.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE

# --------------------------------------------------------------------------
# Status / actionability constants
# --------------------------------------------------------------------------

DATA_OK = "OK"
DATA_WARNING = "WARNING"
DATA_BAD = "BAD"
DATA_NEED = "NEED_DATA"

FRESH = "FRESH"
STALE = "STALE"
FRESHNESS_UNKNOWN = "UNKNOWN"

ACT_NOT_ACTIONABLE = "NOT_ACTIONABLE"
ACT_WATCH_ONLY = "WATCH_ONLY"
ACT_SHADOW_RESEARCH_ONLY = "SHADOW_RESEARCH_ONLY"

# Minimum source reliability (0..1) below which a row can never be more
# than NOT_ACTIONABLE, regardless of severity.
MIN_RELIABLE_SOURCE = 0.50

# Default staleness horizon (hours) for fast market series (funding/OI).
DEFAULT_STALE_HOURS = 6.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp. Returns None when invalid."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _valid_symbol(value: Any) -> bool:
    return isinstance(value, str) and len(value.strip()) >= 3


def _f(value: Any) -> float | None:
    if _is_finite_number(value):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            f = float(value.strip())
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _freshness(ts: datetime | None, *, stale_hours: float, now: datetime | None = None) -> str:
    if ts is None:
        return FRESHNESS_UNKNOWN
    ref = now or _utcnow()
    age_h = (ref - ts).total_seconds() / 3600.0
    if age_h < 0:
        # Future timestamp — treat as unknown, never silently "fresh".
        return FRESHNESS_UNKNOWN
    return FRESH if age_h <= stale_hours else STALE


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------


@dataclass
class _BaseExternalPoint:
    symbol: str = ""
    timestamp: str = ""
    source: str = ""
    source_reliability: float = 0.0
    metric_value: float | None = None
    timeframe: str = ""
    freshness_status: str = FRESHNESS_UNKNOWN
    data_quality_status: str = DATA_NEED
    logical_key: str = ""
    duplicate_flag: bool = False
    research_only: bool = True
    actionability: str = ACT_NOT_ACTIONABLE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FundingPoint(_BaseExternalPoint):
    funding_rate: float | None = None
    funding_z: float | None = None
    funding_percentile: float | None = None


@dataclass
class OpenInterestPoint(_BaseExternalPoint):
    open_interest: float | None = None
    oi_z: float | None = None
    oi_percentile: float | None = None
    oi_momentum: float | None = None


@dataclass
class LiquidationPoint(_BaseExternalPoint):
    liquidation_usd: float | None = None
    side_liquidated: str = ""  # LONG / SHORT / NA
    cluster_flag: bool = False


@dataclass
class ExternalMarketSnapshot:
    symbol: str = ""
    timestamp: str = ""
    funding: FundingPoint | None = None
    open_interest: OpenInterestPoint | None = None
    liquidation: LiquidationPoint | None = None
    research_only: bool = True
    actionability: str = ACT_NOT_ACTIONABLE


@dataclass
class CatalystEvent:
    event_id: str = ""
    timestamp: str = ""
    event_type: str = ""
    source: str = ""
    source_reliability: float = 0.0
    confidence_score: float = 0.0
    severity_score: float = 0.0
    direction_bias: str = "unknown"  # LONG / SHORT / unknown
    affected_symbols: list[str] = field(default_factory=list)
    embargo_if_uncertain: bool = True
    technical_confirmation_required: bool = True
    data_quality_status: str = DATA_NEED
    logical_key: str = ""
    duplicate_flag: bool = False
    research_only: bool = True
    actionability: str = ACT_NOT_ACTIONABLE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TokenUnlockEvent:
    event_id: str = ""
    symbol: str = ""
    event_time: str = ""
    event_type: str = "unlock"
    source: str = ""
    source_reliability: float = 0.0
    unlock_pct_circulating: float | None = None
    unlock_value_usd: float | None = None
    market_cap: float | None = None
    fdv: float | None = None
    fdv_to_mcap: float | None = None
    listing_age_days: float | None = None
    direction_bias: str = "SHORT"  # dilution bias defaults short
    severity_score: float = 0.0
    confidence_score: float = 0.0
    data_quality_status: str = DATA_NEED
    logical_key: str = ""
    duplicate_flag: bool = False
    research_only: bool = True
    actionability: str = ACT_NOT_ACTIONABLE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ListingEvent:
    event_id: str = ""
    symbol: str = ""
    event_time: str = ""
    event_type: str = "listing"
    source: str = ""
    source_reliability: float = 0.0
    listing_age_days: float | None = None
    fdv: float | None = None
    market_cap: float | None = None
    fdv_to_mcap: float | None = None
    direction_bias: str = "unknown"
    severity_score: float = 0.0
    confidence_score: float = 0.0
    data_quality_status: str = DATA_NEED
    logical_key: str = ""
    duplicate_flag: bool = False
    research_only: bool = True
    actionability: str = ACT_NOT_ACTIONABLE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NewsEvent:
    event_id: str = ""
    timestamp: str = ""
    source: str = ""
    source_reliability: float = 0.0
    headline: str = ""
    direction_bias: str = "unknown"
    confidence_score: float = 0.0
    severity_score: float = 0.0
    affected_symbols: list[str] = field(default_factory=list)
    data_quality_status: str = DATA_NEED
    logical_key: str = ""
    duplicate_flag: bool = False
    research_only: bool = True
    actionability: str = ACT_NOT_ACTIONABLE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StrategyFamilySpec:
    family_id: str = ""
    title: str = ""
    description: str = ""
    required_data: list[str] = field(default_factory=list)
    research_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EdgeDiscoveryReadiness:
    generated_at: str = ""
    source_label: str = ""
    rows_loaded: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    duplicate_rows: int = 0
    stale_rows: int = 0
    data_available: bool = False
    data_quality_status: str = DATA_NEED
    freshness_status: str = FRESHNESS_UNKNOWN
    required_data_missing: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


REQUIRED_BASE_FIELDS = ("symbol", "timestamp", "source")


@dataclass
class ValidationResult:
    valid: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    duplicate_count: int = 0
    stale_count: int = 0
    nan_inf_count: int = 0
    missing_field_count: int = 0
    bad_symbol_count: int = 0
    bad_timestamp_count: int = 0
    empty_source_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _logical_key(row: dict[str, Any], *, value_fields: Iterable[str]) -> str:
    sym = str(row.get("symbol") or "").strip().upper()
    ts = str(row.get("timestamp") or row.get("event_time") or "").strip()
    src = str(row.get("source") or "").strip().lower()
    metric = ""
    for fld in value_fields:
        if row.get(fld) is not None:
            metric = f"{fld}={row.get(fld)}"
            break
    return f"{sym}|{ts}|{src}|{metric}"


def validate_external_rows(
    rows: Iterable[dict[str, Any]] | None,
    *,
    value_fields: Iterable[str] = ("metric_value",),
    stale_hours: float = DEFAULT_STALE_HOURS,
    now: datetime | None = None,
    ts_field: str = "timestamp",
) -> ValidationResult:
    """Validate raw external rows. Pure: never raises on bad data, never
    fabricates. Each row is classified valid/rejected and tagged with
    ``freshness_status``, ``logical_key`` and ``duplicate_flag``.

    Rejection reasons (mutually inclusive): missing required field, bad
    symbol, bad timestamp, empty source, NaN/inf in a numeric value,
    logical duplicate.
    """
    result = ValidationResult()
    seen_keys: set[str] = set()
    value_fields = tuple(value_fields)
    for raw in rows or []:
        row = dict(raw)
        reasons: list[str] = []

        # Required base fields.
        for fld in REQUIRED_BASE_FIELDS:
            target = ts_field if fld == "timestamp" else fld
            if not str(row.get(target) or "").strip():
                reasons.append(f"missing_{fld}")
        if any(r.startswith("missing_") for r in reasons):
            result.missing_field_count += 1

        # Symbol.
        if not _valid_symbol(row.get("symbol")):
            reasons.append("bad_symbol")
            result.bad_symbol_count += 1

        # Source.
        if not str(row.get("source") or "").strip():
            # already counted as missing_source above if empty; count once
            if "missing_source" not in reasons:
                reasons.append("empty_source")
            result.empty_source_count += 1

        # Timestamp.
        ts = _parse_ts(row.get(ts_field))
        if ts is None:
            reasons.append("bad_timestamp")
            result.bad_timestamp_count += 1

        # NaN / inf in numeric value fields.
        nan_inf = False
        for fld in value_fields:
            if fld in row and row.get(fld) is not None:
                if _f(row.get(fld)) is None:
                    nan_inf = True
        if nan_inf:
            reasons.append("nan_or_inf")
            result.nan_inf_count += 1

        # Freshness + logical key (computed even if rejected, for report).
        freshness = _freshness(ts, stale_hours=stale_hours, now=now)
        row["freshness_status"] = freshness
        if freshness == STALE:
            result.stale_count += 1
        key = _logical_key(row, value_fields=value_fields)
        row["logical_key"] = key

        if key in seen_keys:
            row["duplicate_flag"] = True
            reasons.append("logical_duplicate")
            result.duplicate_count += 1
        else:
            row["duplicate_flag"] = False
            seen_keys.add(key)

        # Source reliability gate (does not reject; caps actionability).
        rel = _f(row.get("source_reliability")) or 0.0
        row["source_reliability"] = rel
        row["research_only"] = True
        row.setdefault("actionability", ACT_NOT_ACTIONABLE)

        if reasons:
            row["reject_reasons"] = reasons
            result.rejected.append(row)
        else:
            result.valid.append(row)
    return result


def quality_from_validation(vr: ValidationResult) -> str:
    """Map a ValidationResult to a coarse data_quality_status."""
    total = len(vr.valid) + len(vr.rejected)
    if total == 0:
        return DATA_NEED
    if not vr.valid:
        return DATA_BAD
    bad_ratio = len(vr.rejected) / total
    if bad_ratio == 0 and vr.stale_count == 0:
        return DATA_OK
    if bad_ratio > 0.5:
        return DATA_BAD
    return DATA_WARNING


# --------------------------------------------------------------------------
# Local CSV / JSON ingest (no network, no APIs)
# --------------------------------------------------------------------------


def load_external_data(path: str | None) -> tuple[list[dict[str, Any]], str]:
    """Load a *local* CSV or JSON file of external rows.

    Returns ``(rows, source_label)``. When ``path`` is falsy or the file
    does not exist, returns ``([], "NO_EXTERNAL_DATA")`` — never raises,
    never fabricates. JSON may be a list of objects or an object with a
    ``rows`` / ``data`` list.
    """
    if not path:
        return [], "NO_EXTERNAL_DATA"
    p = Path(path)
    if not p.exists() or not p.is_file():
        return [], "MISSING_FILE"
    suffix = p.suffix.lower()
    try:
        if suffix == ".json":
            payload = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                rows = payload.get("rows") or payload.get("data") or []
            else:
                rows = payload
            if not isinstance(rows, list):
                return [], "BAD_JSON_SHAPE"
            out = [dict(r) for r in rows if isinstance(r, dict)]
            return out, f"json:{p.name}"
        if suffix in (".csv", ".tsv"):
            delim = "\t" if suffix == ".tsv" else ","
            with p.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh, delimiter=delim)
                out = [dict(r) for r in reader]
            return out, f"csv:{p.name}"
    except (OSError, ValueError, json.JSONDecodeError):
        return [], "UNREADABLE_FILE"
    return [], "UNSUPPORTED_FORMAT"


# --------------------------------------------------------------------------
# Foundation assessment
# --------------------------------------------------------------------------


def assess_foundation(
    rows: Iterable[dict[str, Any]] | None,
    *,
    source_label: str = "",
    required_data: Iterable[str] | None = None,
    value_fields: Iterable[str] = ("metric_value",),
    stale_hours: float = DEFAULT_STALE_HOURS,
    now: datetime | None = None,
    ts_field: str = "timestamp",
) -> EdgeDiscoveryReadiness:
    """Produce an EdgeDiscoveryReadiness from raw external rows."""
    row_list = list(rows or [])
    vr = validate_external_rows(
        row_list,
        value_fields=value_fields,
        stale_hours=stale_hours,
        now=now,
        ts_field=ts_field,
    )
    quality = quality_from_validation(vr)
    symbols = sorted({
        str(r.get("symbol") or "").strip().upper()
        for r in vr.valid if str(r.get("symbol") or "").strip()
    })
    valid_fresh = [r for r in vr.valid if r.get("freshness_status") == FRESH]
    if not vr.valid:
        freshness = FRESHNESS_UNKNOWN
    elif valid_fresh:
        freshness = FRESH
    else:
        freshness = STALE

    required = list(required_data or [])
    missing: list[str] = []
    if not row_list:
        # No external data at all → everything required is missing.
        missing = list(required)
        quality = DATA_NEED
        data_available = False
    else:
        data_available = bool(vr.valid)
        # A required field is "missing" if no valid row carries it.
        for fld in required:
            if not any(_present(r, fld) for r in vr.valid):
                missing.append(fld)

    notes: list[str] = []
    if vr.duplicate_count:
        notes.append(f"logical_duplicates_excluded={vr.duplicate_count}")
    if vr.stale_count:
        notes.append(f"stale_rows={vr.stale_count}")
    if vr.nan_inf_count:
        notes.append(f"nan_or_inf_rejected={vr.nan_inf_count}")
    if vr.bad_timestamp_count:
        notes.append(f"bad_timestamp_rejected={vr.bad_timestamp_count}")
    if vr.bad_symbol_count:
        notes.append(f"bad_symbol_rejected={vr.bad_symbol_count}")

    return EdgeDiscoveryReadiness(
        generated_at=_utcnow().isoformat(),
        source_label=source_label,
        rows_loaded=len(row_list),
        valid_rows=len(vr.valid),
        invalid_rows=len(vr.rejected),
        duplicate_rows=vr.duplicate_count,
        stale_rows=vr.stale_count,
        data_available=data_available,
        data_quality_status=quality,
        freshness_status=freshness,
        required_data_missing=missing,
        symbols=symbols,
        notes=notes,
    )


def _present(row: dict[str, Any], field_name: str) -> bool:
    val = row.get(field_name)
    if val is None:
        return False
    if isinstance(val, str):
        return bool(val.strip())
    return True


def cap_actionability(
    proposed: str,
    *,
    source_reliability: float,
    data_quality_status: str,
    embargo: bool = False,
) -> str:
    """Clamp a proposed actionability label down to what the data
    supports. The ceiling for V10 is ``SHADOW_RESEARCH_ONLY``; nothing
    here can ever become operative.

    - reliability below ``MIN_RELIABLE_SOURCE`` => NOT_ACTIONABLE
    - data quality BAD / NEED_DATA => NOT_ACTIONABLE
    - embargo (uncertain event) => at most WATCH_ONLY
    """
    rank = {ACT_NOT_ACTIONABLE: 0, ACT_WATCH_ONLY: 1, ACT_SHADOW_RESEARCH_ONLY: 2}
    proposed = proposed if proposed in rank else ACT_NOT_ACTIONABLE
    ceiling = ACT_SHADOW_RESEARCH_ONLY
    if (source_reliability or 0.0) < MIN_RELIABLE_SOURCE:
        return ACT_NOT_ACTIONABLE
    if data_quality_status in (DATA_BAD, DATA_NEED):
        return ACT_NOT_ACTIONABLE
    if embargo:
        ceiling = ACT_WATCH_ONLY
    if rank[proposed] <= rank[ceiling]:
        return proposed
    return ceiling


def render_readiness_text(title: str, r: EdgeDiscoveryReadiness) -> list[str]:
    """Standard CLI lines for a readiness report."""
    lines = [f"{title} START"]
    lines.append(f"source_label: {r.source_label or 'NONE'}")
    lines.append(f"rows_loaded: {r.rows_loaded}")
    lines.append(f"valid_rows: {r.valid_rows}")
    lines.append(f"invalid_rows: {r.invalid_rows}")
    lines.append(f"duplicate_rows: {r.duplicate_rows}")
    lines.append(f"stale_rows: {r.stale_rows}")
    lines.append(f"data_available: {str(r.data_available).lower()}")
    lines.append(f"data_quality_status: {r.data_quality_status}")
    lines.append(f"freshness_status: {r.freshness_status}")
    lines.append(
        "required_data_missing: "
        + (",".join(r.required_data_missing) if r.required_data_missing else "NONE")
    )
    lines.append("symbols: " + (",".join(r.symbols) if r.symbols else "NONE"))
    for note in r.notes:
        lines.append(f"note: {note}")
    lines.append("research_only: true")
    lines.append("paper_filter_enabled: false")
    lines.append("can_send_real_orders: false")
    lines.append(f"final_recommendation: {r.final_recommendation}")
    lines.append(f"{title} END")
    return lines
