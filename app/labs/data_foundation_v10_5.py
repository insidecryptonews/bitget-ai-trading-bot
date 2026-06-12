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

import math
import re as _re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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
ST_SEMANTIC_FAIL = "SEMANTIC_VALIDATION_FAIL"
ST_SERIES_INCOMPLETE = "SERIES_COMPLETENESS_FAIL"

# Whitelists (fail-closed: anything outside them is invalid).
ALLOWED_TIMESTAMP_UNITS = frozenset({"unix_ms", "unix_s"})
ALLOWED_IMPORT_STATUSES = frozenset({"BLOCKED", "STAGED", "VALIDATING",
                                     "STAGED_READY_FOR_PROMOTE"})
ALLOWED_OI_STATUSES = frozenset({
    "DATA_OK", "MISSING_OI_LOW", "MISSING_OI_MODERATE", "MISSING_OI_HIGH",
    "MISSING_OI_CLUSTERED", "NEED_DATA", "NEED_MORE_DATA", "UNKNOWN",
    "NO_AUDIT", "NO_RAW_OI", "NOT_AVAILABLE",
})

# V10.5.2 (Codex P1-4) — semantic whitelists. Anything outside blocks.
ALLOWED_TIMEFRAMES = frozenset({"1m", "5m", "15m", "30m", "1h", "4h", "1d"})
ALLOWED_DATA_TYPES = frozenset({
    "ohlcv", "open_interest", "funding", "liquidations",
    "mark_price", "index_price", "trades", "orderbook",
})
REQUIRED_DATA_TYPES_MIN = frozenset({"ohlcv", "open_interest", "funding",
                                     "liquidations"})
_SYMBOL_RE = _re.compile(r"^[A-Z0-9]{2,15}USDT$")

# Data-readiness statuses.
READY_NEED_VERIFIED_PROVIDER = "NEED_VERIFIED_PROVIDER"
READY_NEED_LONG_HISTORY = "NEED_LONG_HISTORY"
READY_OI_BLOCKED = "OI_BLOCKED"
READY_NEED_SERIES = "NEED_SERIES_COMPLETENESS"
READY_NEED_VALID_MANIFEST = "NEED_VALID_MANIFEST"
READY_INITIAL_OK = "INITIAL_VALIDATION_READY"

_SHA256_RE = _re.compile(r"^[0-9a-fA-F]{64}$")
_DATE_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}")


# ---------------------------------------------------------------------------
# V10.5.1 (Codex P1-2) — TOTAL defensive parsers. Never raise; reject
# anything hostile, malformed or ambiguous. If in doubt: invalid.
# ---------------------------------------------------------------------------

def _to_finite_float(value: Any) -> float | None:
    """Finite float or None. Rejects None/bool/NaN/inf/blank/garbage strings,
    containers, huge ints (OverflowError) and hostile __float__ objects.
    Catches Exception (never BaseException). Never raises."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        f = float(value)
    except Exception:
        return None
    try:
        if not math.isfinite(f):
            return None
    except Exception:
        return None
    return f


def _to_non_negative_int(value: Any) -> int | None:
    """Non-negative integer (int or integral float) or None. Never raises."""
    f = _to_finite_float(value)
    if f is None or f < 0:
        return None
    try:
        i = int(f)
    except Exception:
        return None
    if float(i) != f:  # 3.7 is not a count
        return None
    return i


def _valid_ratio(value: Any) -> float | None:
    """Finite ratio inside [0, 1] or None. Never raises."""
    f = _to_finite_float(value)
    if f is None or f < 0.0 or f > 1.0:
        return None
    return f


def _valid_non_empty_str(value: Any) -> bool:
    try:
        return isinstance(value, str) and bool(value.strip())
    except Exception:
        return False


def _valid_non_empty_list(value: Any) -> bool:
    try:
        return isinstance(value, (list, tuple)) and len(value) > 0
    except Exception:
        return False


def _valid_sha256(value: Any) -> bool:
    try:
        return isinstance(value, str) and bool(_SHA256_RE.match(value))
    except Exception:
        return False


def _parse_datetime(value: Any) -> datetime | None:
    """V10.5.2 (Codex P1-4) — REAL date parsing, never a regex shortcut.
    Accepts true ISO-8601 strings (Z suffix ok) or positive finite unix
    timestamps (seconds or milliseconds). Returns None for anything else:
    '2026-99-99garbage', '', 'x', impossible dates… Never raises."""
    try:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            return datetime.fromisoformat(text)
        f = _to_finite_float(value)
        if f is None or f <= 0:
            return None
        if f > 1e12:  # unix milliseconds
            f = f / 1000.0
        return datetime.fromtimestamp(f, tz=timezone.utc)
    except Exception:
        return None


def _valid_date_or_ts(value: Any) -> bool:
    return _parse_datetime(value) is not None


def _valid_range(value: Any) -> bool:
    """A range must be a dict with parseable start/end and start < end.
    Free-form strings like 'x' or '365d' are NOT acceptable ranges."""
    try:
        if not isinstance(value, dict):
            return False
        start = _parse_datetime(value.get("start"))
        end = _parse_datetime(value.get("end"))
        return start is not None and end is not None and start < end
    except Exception:
        return False


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


def _semantic_blockers(m: dict[str, Any]) -> list[str]:
    """V10.5.1 (Codex P1-2) — full semantic validation BEFORE any other gate.
    Every field is parsed defensively; any invalid value blocks. Never raises."""
    bad: list[str] = []

    for name in ("source_provider", "license_terms"):
        if not _valid_non_empty_str(m.get(name)):
            bad.append(f"invalid_field:{name}")
    # V10.5.2 — ranges are structured dicts with parseable start < end; a
    # free-form string ('x', '365d') is not a range.
    for name in ("requested_range", "actual_covered_range"):
        if not _valid_range(m.get(name)):
            bad.append(f"invalid_field:{name}")
    # Authorization fields: ABSENT/None/empty is handled by the V10.4 gate
    # (AUTHORIZATION_REQUIRED — more actionable); a present-but-garbage TYPE
    # (string "yes", number, list…) is a semantic failure here.
    for name in ("explicit_human_authorization", "paid_download_authorized",
                 "license_terms_confirmed"):
        value = m.get(name)
        if value is not None and not isinstance(value, bool):
            bad.append(f"invalid_field:{name}")
    ref = m.get("authorization_reference")
    if ref is not None and not isinstance(ref, str):
        bad.append("invalid_field:authorization_reference")

    clean_days = _to_finite_float(m.get("clean_days"))
    if clean_days is None or clean_days < 0:
        bad.append("invalid_field:clean_days")

    # V10.5.2 — list CONTENT is validated, not just non-emptiness.
    symbols = m.get("symbols")
    if not _valid_non_empty_list(symbols):
        bad.append("invalid_field:symbols")
    elif not all(isinstance(s, str) and _SYMBOL_RE.match(s) for s in symbols):
        bad.append("invalid_field:symbols_pattern")

    timeframes = m.get("timeframes")
    if not _valid_non_empty_list(timeframes):
        bad.append("invalid_field:timeframes")
    elif not all(isinstance(t, str) and t in ALLOWED_TIMEFRAMES for t in timeframes):
        bad.append("invalid_field:timeframes_not_allowed")

    data_types = m.get("data_types")
    if not _valid_non_empty_list(data_types):
        bad.append("invalid_field:data_types")
    else:
        if not all(isinstance(d, str) and d in ALLOWED_DATA_TYPES for d in data_types):
            bad.append("invalid_field:data_types_not_allowed")
        missing_required = REQUIRED_DATA_TYPES_MIN - set(
            d for d in data_types if isinstance(d, str))
        if missing_required:
            bad.append("invalid_field:data_types_missing_required:"
                       + ",".join(sorted(missing_required)))

    # V10.5.2 — rows_by_type must be semantically meaningful: known keys,
    # non-negative ints, and >0 rows for every mandatory data type.
    rows = m.get("rows_by_type")
    if not isinstance(rows, dict) or not rows:
        bad.append("invalid_field:rows_by_type")
    else:
        for key, val in rows.items():
            if not isinstance(key, str) or key not in ALLOWED_DATA_TYPES:
                bad.append(f"invalid_field:rows_by_type_unknown_key:{key}")
                break
            if _to_non_negative_int(val) is None:
                bad.append(f"invalid_field:rows_by_type.{key}")
                break
        else:
            for required in sorted(REQUIRED_DATA_TYPES_MIN):
                count = _to_non_negative_int(rows.get(required))
                if count is None or count <= 0:
                    bad.append(f"invalid_field:rows_by_type_required_zero:{required}")

    for name in ("coverage_ratio", "missing_oi_ratio",
                 "missing_funding_ratio", "missing_liquidations_ratio"):
        if _valid_ratio(m.get(name)) is None:
            bad.append(f"invalid_field:{name}")
    for name in ("gap_count", "duplicate_count"):
        if _to_non_negative_int(m.get(name)) is None:
            bad.append(f"invalid_field:{name}")

    oi_status = m.get("missing_oi_status")
    if not (_valid_non_empty_str(oi_status)
            and str(oi_status).upper() in ALLOWED_OI_STATUSES):
        bad.append("invalid_field:missing_oi_status")

    if not (_valid_non_empty_str(m.get("timezone"))
            and str(m.get("timezone")).strip().upper() == "UTC"):
        bad.append("invalid_field:timezone_must_be_utc")
    if str(m.get("timestamp_unit") or "") not in ALLOWED_TIMESTAMP_UNITS:
        bad.append("invalid_field:timestamp_unit")
    if str(m.get("schema_version") or "") != SCHEMA_VERSION:
        bad.append("invalid_field:schema_version")
    if str(m.get("import_status") or "") not in ALLOWED_IMPORT_STATUSES:
        bad.append("invalid_field:import_status")
    if not _valid_date_or_ts(m.get("generated_at")):
        bad.append("invalid_field:generated_at")

    checksums = m.get("checksums_sha256")
    if not isinstance(checksums, dict) or not checksums:
        bad.append("invalid_field:checksums_sha256")
    else:
        for fname, digest in checksums.items():
            if not _valid_non_empty_str(fname) or not _valid_sha256(digest):
                bad.append("invalid_field:checksums_sha256_not_sha256_hex")
                break

    # V10.5.2 self-audit — CROSS-FIELD consistency (contradictory manifests
    # are hostile manifests). Only checked when the individual fields parsed.
    clean = _to_finite_float(m.get("clean_days"))
    covered = m.get("actual_covered_range")
    if clean is not None and isinstance(covered, dict):
        start = _parse_datetime(covered.get("start"))
        end = _parse_datetime(covered.get("end"))
        if start is not None and end is not None and end > start:
            span_days = (end - start).total_seconds() / 86400.0
            if clean > span_days + 1.0:  # physically impossible claim
                bad.append("inconsistent_field:clean_days_exceeds_covered_range")
    oi_ratio = _valid_ratio(m.get("missing_oi_ratio"))
    oi_status = str(m.get("missing_oi_status") or "").upper()
    if oi_ratio is not None and oi_status == "DATA_OK" and oi_ratio > 0.10:
        bad.append("inconsistent_field:oi_status_data_ok_with_high_missing_ratio")
    return bad


def _fail_closed(ev: ManifestV105Evaluation, status: str,
                 blockers: list[str]) -> ManifestV105Evaluation:
    ev.status = status
    ev.blockers = blockers
    ev.promote_allowed = False
    ev.do_not_replace_raw = True
    ev.import_status = "BLOCKED"
    return ev


def evaluate_manifest_v105(manifest: dict[str, Any] | None) -> ManifestV105Evaluation:
    """Full FAIL-CLOSED V10.5 gate chain (Codex P1-2):

    1. schema completeness, 2. TOTAL semantic validation of every field
    (hostile/NaN/inf/garbage values block, never raise), 3. all V10.4 gates
    (coverage, history, quality, explicit human authorization), 4. V10.5
    series-completeness. ``promote_allowed`` is RECALCULATED from gates —
    any ``promote_allowed`` value inside the manifest input is ignored.
    Never raises; any internal error returns a blocked evaluation.
    """
    ev = ManifestV105Evaluation()
    try:
        if not isinstance(manifest, dict) or not manifest:
            return _fail_closed(ev, ST_INVALID_V105,
                                ["invalid_or_missing_manifest_v105_fields"])
        m = dict(manifest)
        m.pop("promote_allowed", None)  # input can never steer the result

        missing = [f for f in MANIFEST_V105_REQUIRED_FIELDS if f not in m]
        ev.missing_fields = missing
        if missing:
            return _fail_closed(ev, ST_INVALID_V105,
                                ["invalid_or_missing_manifest_v105_fields"])
        ev.valid_manifest_v105 = True

        # 2) Semantic validation BEFORE any downstream gate (fail-closed).
        semantic = _semantic_blockers(m)
        if semantic:
            return _fail_closed(ev, ST_SEMANTIC_FAIL, semantic)

        # 3) Every V10.4 gate (incl. explicit human authorization).
        base = evaluate_acquisition_manifest(m)
        ev.base_status = base.status
        ev.base_blockers = list(base.blockers)
        if base.status != ST_PROMOTE_ALLOWED:
            return _fail_closed(ev, base.status, list(base.blockers))

        # 4) V10.5 series-completeness (ratios already validated finite/[0,1]).
        blockers: list[str] = []
        funding = _valid_ratio(m.get("missing_funding_ratio"))
        liq = _valid_ratio(m.get("missing_liquidations_ratio"))
        ev.missing_funding_ratio = funding if funding is not None else "UNKNOWN"
        ev.missing_liquidations_ratio = liq if liq is not None else "UNKNOWN"
        if funding is None or funding > MAX_MISSING_FUNDING_RATIO:
            blockers.append("missing_funding_ratio_invalid_or_too_high")
        if liq is None or liq > MAX_MISSING_LIQUIDATIONS_RATIO:
            blockers.append("missing_liquidations_ratio_invalid_or_too_high")
        ev.timezone_ok = True
        ev.timestamp_unit_ok = True
        if blockers:
            return _fail_closed(ev, ST_SERIES_INCOMPLETE, blockers)

        ev.status = ST_PROMOTE_ALLOWED
        ev.blockers = []
        ev.promote_allowed = True
        ev.do_not_replace_raw = False
        ev.import_status = "STAGED_READY_FOR_PROMOTE"
        return ev
    except Exception:
        # Absolute fail-closed backstop: any unexpected error blocks.
        return _fail_closed(ManifestV105Evaluation(), ST_SEMANTIC_FAIL,
                            ["manifest_validation_error"])


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
    funding_verified: bool = False,
    liquidations_verified: bool = False,
    manifest_evaluation: dict[str, Any] | None = None,
) -> DataReadinessV105:
    """Summarise the data foundation honestly. Without a verified provider or
    sufficient history the answer is NEED_VERIFIED_PROVIDER — never invented.

    V10.5.1 (Codex P2-1): INITIAL_VALIDATION_READY requires ALL of: provider
    ready for authorization, clean_days>=180, OI NOT blocked, funding verified
    AND liquidations verified. Any unknown/blocked mandatory series keeps the
    status conservative (OI_BLOCKED / NEED_SERIES_COMPLETENESS / ...).

    V10.5.2 (Codex P2-1): additionally requires a VALID manifest evaluation
    (full gate chain passed). Without one => NEED_VALID_MANIFEST with blocker
    valid_manifest_required — INITIAL_VALIDATION_READY is unreachable."""
    r = DataReadinessV105()
    snap = dict(data_readiness_snapshot or {})
    prov = dict(provider_report or {})

    if snap:
        r.clean_days = snap.get("current_clean_days", "UNKNOWN")
        r.history_status = snap.get("current_history_status", "UNKNOWN")
        r.oi_status = snap.get("missing_oi_status", "UNKNOWN")
        r.oi_bucket_policy = snap.get("oi_bucket_policy", "BLOCK_OI_BUCKETS")
        r.backtester_readiness = snap.get("backtester_readiness", "NEED_LONG_HISTORY")

    r.funding_status = "VERIFIED" if funding_verified else "UNKNOWN_NO_VERIFIED_SOURCE"
    r.liquidations_status = ("VERIFIED" if liquidations_verified
                             else "UNKNOWN_NO_VERIFIED_SOURCE")

    any_ready = bool(prov.get("any_provider_ready_for_authorization"))
    r.provider_readiness = ("READY_FOR_HUMAN_AUTHORIZATION" if any_ready
                            else "NO_PROVIDER_VERIFIED")

    blockers: list[str] = []
    clean = r.clean_days
    has_180d = isinstance(clean, (int, float)) and clean >= 180
    oi_blocked = str(r.oi_bucket_policy) == "BLOCK_OI_BUCKETS"
    if not any_ready:
        blockers.append("no provider verified (Tardis.dev sample + manual checks pending)")
    if isinstance(clean, (int, float)):
        if clean < 180:
            blockers.append(f"clean_days={clean} < 180 minimum")
    else:
        blockers.append("history_depth_unknown (no data snapshot)")
    if oi_blocked:
        blockers.append(f"OI buckets blocked (status={r.oi_status})")
    if not funding_verified:
        blockers.append("funding history not verified")
    if not liquidations_verified:
        blockers.append("liquidations history not verified")
    manifest_eval = dict(manifest_evaluation or {})
    manifest_ok = (bool(manifest_eval)
                   and manifest_eval.get("valid_manifest_v105") is True
                   and manifest_eval.get("promote_allowed") is True)
    if not manifest_ok:
        blockers.append("valid_manifest_required")
    r.top_blockers = blockers

    # Conservative status ladder — every mandatory gate must be green.
    if not any_ready:
        r.status = READY_NEED_VERIFIED_PROVIDER
    elif not has_180d:
        r.status = READY_NEED_LONG_HISTORY
    elif oi_blocked:
        r.status = READY_OI_BLOCKED
    elif not (funding_verified and liquidations_verified):
        r.status = READY_NEED_SERIES
    elif not manifest_ok:
        r.status = READY_NEED_VALID_MANIFEST
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
