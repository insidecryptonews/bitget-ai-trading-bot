"""ResearchOps V10.5 — Data Manifest Contract v10.5 + Data Readiness.

Extends the V10.4 acquisition manifest with funding/liquidation completeness,
timezone/timestamp metadata, schema versioning and import status — and builds
the V10.5 data-readiness summary that names the next required HUMAN action.

Read-only: no downloads, no network, no API keys, no DB writes. Every gate
from V10.4/V10.4.1 still applies (explicit human authorization, license,
coverage, gaps, duplicates, conservative OI policy). ``promote_allowed`` is
false by default and can only flip true through the full gate chain.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from .external_data_acquisition_plan_v10_4 import (
    MANIFEST_REQUIRED_FIELDS,
    ST_PROMOTE_ALLOWED,
    evaluate_acquisition_manifest,
)

SCHEMA_VERSION = "v10.5"

# V10.5 additions on top of the V10.4 required fields.
MANIFEST_V105_EXTRA_FIELDS = [
    "missing_funding_ratio",
    "missing_liquidations_ratio",
    "timezone",
    "timestamp_unit",
    "generated_at",
    "schema_version",
    "import_status",
]

MANIFEST_V105_REQUIRED_FIELDS = list(MANIFEST_REQUIRED_FIELDS) + MANIFEST_V105_EXTRA_FIELDS

# Conservative completeness ceilings for the new series.
MAX_MISSING_FUNDING_RATIO = 0.10
MAX_MISSING_LIQUIDATIONS_RATIO = 0.10

ST_INVALID_V105 = "INVALID_MANIFEST_V105"
ST_SERIES_INCOMPLETE = "SERIES_COMPLETENESS_FAIL"

# Data-readiness statuses.
READY_NEED_VERIFIED_PROVIDER = "NEED_VERIFIED_PROVIDER"
READY_NEED_LONG_HISTORY = "NEED_LONG_HISTORY"
READY_INITIAL_OK = "INITIAL_VALIDATION_READY"


@dataclass
class ManifestV105Evaluation:
    schema_version: str = SCHEMA_VERSION
    valid_manifest_v105: bool = False
    missing_fields: list[str] = field(default_factory=list)
    base_status: str = ""
    base_blockers: list[str] = field(default_factory=list)
    missing_funding_ratio: Any = "UNKNOWN"
    missing_liquidations_ratio: Any = "UNKNOWN"
    timezone_ok: bool = False
    timestamp_unit_ok: bool = False
    status: str = ST_INVALID_V105
    blockers: list[str] = field(default_factory=list)
    import_status: str = "BLOCKED"
    promote_allowed: bool = False
    do_not_replace_raw: bool = True
    research_only: bool = True
    paper_ready: bool = False
    live_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite_ratio(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")) or f < 0:
        return None
    return f


def evaluate_manifest_v105(manifest: dict[str, Any] | None) -> ManifestV105Evaluation:
    """Full V10.5 gate chain: V10.5 schema fields -> V10.4 gates (validity,
    coverage, history, quality, explicit human authorization) -> V10.5
    series-completeness gates. Never promotes by default."""
    ev = ManifestV105Evaluation()
    m = dict(manifest or {})

    missing = [f for f in MANIFEST_V105_REQUIRED_FIELDS if f not in m]
    ev.missing_fields = missing
    ev.valid_manifest_v105 = not missing
    if missing:
        ev.status = ST_INVALID_V105
        ev.blockers = ["invalid_or_missing_manifest_v105_fields"]
        ev.promote_allowed = False
        ev.do_not_replace_raw = True
        ev.import_status = "BLOCKED"
        return ev

    # Delegate every V10.4 gate (incl. explicit human authorization).
    base = evaluate_acquisition_manifest(m)
    ev.base_status = base.status
    ev.base_blockers = list(base.blockers)
    if base.status != ST_PROMOTE_ALLOWED:
        ev.status = base.status
        ev.blockers = list(base.blockers)
        ev.promote_allowed = False
        ev.do_not_replace_raw = True
        ev.import_status = "BLOCKED"
        return ev

    # V10.5 series-completeness + metadata gates.
    blockers: list[str] = []
    funding = _finite_ratio(m.get("missing_funding_ratio"))
    liq = _finite_ratio(m.get("missing_liquidations_ratio"))
    ev.missing_funding_ratio = funding if funding is not None else "UNKNOWN"
    ev.missing_liquidations_ratio = liq if liq is not None else "UNKNOWN"
    if funding is None or funding > MAX_MISSING_FUNDING_RATIO:
        blockers.append("missing_funding_ratio_invalid_or_too_high")
    if liq is None or liq > MAX_MISSING_LIQUIDATIONS_RATIO:
        blockers.append("missing_liquidations_ratio_invalid_or_too_high")
    ev.timezone_ok = str(m.get("timezone") or "").upper() == "UTC"
    if not ev.timezone_ok:
        blockers.append("timezone_must_be_utc")
    ev.timestamp_unit_ok = str(m.get("timestamp_unit") or "") in ("unix_ms", "unix_s")
    if not ev.timestamp_unit_ok:
        blockers.append("timestamp_unit_must_be_unix_ms_or_unix_s")
    if str(m.get("schema_version") or "") != SCHEMA_VERSION:
        blockers.append("schema_version_mismatch")

    if blockers:
        ev.status = ST_SERIES_INCOMPLETE
        ev.blockers = blockers
        ev.promote_allowed = False
        ev.do_not_replace_raw = True
        ev.import_status = "BLOCKED"
        return ev

    ev.status = ST_PROMOTE_ALLOWED
    ev.blockers = []
    ev.promote_allowed = True
    ev.do_not_replace_raw = False
    ev.import_status = "STAGED_READY_FOR_PROMOTE"
    return ev


@dataclass
class DataReadinessV105:
    status: str = READY_NEED_VERIFIED_PROVIDER
    clean_days: Any = "UNKNOWN"
    history_status: Any = "UNKNOWN"
    oi_status: Any = "UNKNOWN"
    oi_bucket_policy: Any = "BLOCK_OI_BUCKETS"
    funding_status: str = "UNKNOWN_NO_VERIFIED_SOURCE"
    liquidations_status: str = "UNKNOWN_NO_VERIFIED_SOURCE"
    backtester_readiness: Any = "NEED_LONG_HISTORY"
    provider_readiness: str = "NO_PROVIDER_VERIFIED"
    top_blockers: list[str] = field(default_factory=list)
    next_required_human_action: str = ""
    research_only: bool = True
    paper_ready: bool = False
    live_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_data_readiness_v105(
    *,
    data_readiness_snapshot: dict[str, Any] | None,
    provider_report: dict[str, Any] | None,
) -> DataReadinessV105:
    """Summarise the data foundation honestly. Without a verified provider or
    sufficient history the answer is NEED_VERIFIED_PROVIDER — never invented."""
    r = DataReadinessV105()
    snap = dict(data_readiness_snapshot or {})
    prov = dict(provider_report or {})

    if snap:
        r.clean_days = snap.get("current_clean_days", "UNKNOWN")
        r.history_status = snap.get("current_history_status", "UNKNOWN")
        r.oi_status = snap.get("missing_oi_status", "UNKNOWN")
        r.oi_bucket_policy = snap.get("oi_bucket_policy", "BLOCK_OI_BUCKETS")
        r.backtester_readiness = snap.get("backtester_readiness", "NEED_LONG_HISTORY")

    any_ready = bool(prov.get("any_provider_ready_for_authorization"))
    r.provider_readiness = ("READY_FOR_HUMAN_AUTHORIZATION" if any_ready
                            else "NO_PROVIDER_VERIFIED")

    blockers: list[str] = []
    clean = r.clean_days
    has_180d = isinstance(clean, (int, float)) and clean >= 180
    if not any_ready:
        blockers.append("no provider verified (Tardis.dev sample + manual checks pending)")
    if isinstance(clean, (int, float)):
        if clean < 180:
            blockers.append(f"clean_days={clean} < 180 minimum")
    else:
        blockers.append("history_depth_unknown (no data snapshot)")
    if str(r.oi_bucket_policy) == "BLOCK_OI_BUCKETS":
        blockers.append(f"OI buckets blocked (status={r.oi_status})")
    blockers.append("funding/liquidations history: no verified external source yet")
    r.top_blockers = blockers

    if not any_ready or not has_180d:
        r.status = READY_NEED_VERIFIED_PROVIDER if not any_ready else READY_NEED_LONG_HISTORY
    else:
        r.status = READY_INITIAL_OK  # still research-only; never paper/live

    r.next_required_human_action = (
        "Contact Tardis.dev (see docs/research_v10_5_provider_contact_pack.md): "
        "confirm Bitget USDT-perp coverage for the 10 research symbols, request "
        "BTCUSDT+ETHUSDT 7-30d sample, validate schema offline, then decide "
        "authorization. No payment before sample validation."
    )
    r.paper_ready = False
    r.live_ready = False
    return r
