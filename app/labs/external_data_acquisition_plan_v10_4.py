"""ResearchOps V10.4 — Data Acquisition Plan + Importer Contract (research-only).

Designs a SAFE future data acquisition/import pipeline (staging -> validate ->
atomic promote -> rollback, with manifest + checksums + lineage + quality
gates). It implements ONLY the contract + a pure manifest evaluator. It does
NOT download anything, makes NO network calls, requires NO API key, writes NO
DB, and NEVER replaces good data with insufficient staging.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from .external_data_provider_registry_v10_3 import (
    CLASS_INITIAL,
    CLASS_INTERMEDIATE,
    CLASS_NO_DATA,
    CLASS_STRONGER,
    MISSING_OI_BLOCK_RATIO,
    OI_POLICY_ALLOW,
    OI_POLICY_BLOCK,
    OI_UNKNOWN_STATUSES,
    REQUIRED_MIN_HISTORY_DAYS,
    STRONGER_HISTORY_DAYS,
)
from .external_missing_oi_audit_v10_2 import STATUS_CLUSTERED, STATUS_HIGH, STATUS_MODERATE

# Directory layout (paths only; nothing is written by this module).
ACQUISITION_DIRS = {
    "staging": "external_data/staging",
    "raw_immutable": "external_data/raw",
    "processed": "external_data/processed",
    "manifests": "external_data/manifests",
    "archive": "external_data/archive",
}

MANIFEST_REQUIRED_FIELDS = [
    "source_provider", "license_terms", "requested_range", "actual_covered_range",
    "symbols", "timeframes", "data_types", "rows_by_type", "missing_oi_ratio",
    "missing_oi_status", "gap_count", "duplicate_count", "coverage_ratio",
    "clean_days", "checksums_sha256",
]

# V10.4.1 (Codex P1) — explicit authorization fields. A promote can NEVER be
# allowed without explicit human authorization, even if every quality gate
# passes. Absence of these fields means NOT authorized (never default-safe).
MANIFEST_AUTHORIZATION_FIELDS = [
    "explicit_human_authorization",   # must be exactly True
    "paid_download_authorized",       # must be exactly True for non-free sources
    "license_terms_confirmed",        # must be exactly True
    "authorization_reference",        # non-empty human approval reference
]

# Quality-gate thresholds.
MAX_GAP_RATIO = 0.05
MAX_DUP_RATIO = 0.02
MIN_COVERAGE_RATIO = 0.80

# Evaluation statuses.
ST_INVALID_MANIFEST = "INVALID_MANIFEST"
ST_UNDERCOVERAGE = "UNDERCOVERAGE_BLOCK"
ST_NEED_LONG_HISTORY = "NEED_LONG_HISTORY"
ST_QUALITY_FAIL = "QUALITY_GATE_FAIL"
ST_AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
ST_PROMOTE_ALLOWED = "PROMOTE_ALLOWED_RESEARCH_ONLY"

# Providers whose data is known free. Anything else (paid, freemium,
# enterprise, unknown, unlisted) requires paid_download_authorized=True.
_KNOWN_FREE_PROVIDERS = frozenset({"bitget_official", "binance_okx_proxy"})


def build_importer_contract() -> dict[str, Any]:
    """The contract a FUTURE importer must satisfy. Pure data; no execution."""
    return {
        "expected_input_files": [
            "perp_market_state.csv|ndjson", "perp_liquidations.csv|ndjson",
        ],
        "minimum_columns": {
            "perp_market_state": ["symbol", "exchange", "timestamp", "price_open",
                                   "price_high", "price_low", "price_close",
                                   "volume_usd", "funding_rate", "oi_usd_close", "source"],
            "perp_liquidations": ["symbol", "exchange", "timestamp", "side",
                                   "notional_usd", "price", "source"],
        },
        "validations": [
            "timestamp_normalized_to_unix_ms_utc",
            "bitget_symbol_normalization",
            "contract_instrument_normalization",
            "provider_specific_mapping",
            "reject_nan_inf",
            "logical_duplicate_detection",
            "gap_detection",
            "missing_oi_audit",
            "sha256_checksum_per_file",
            "coverage_ratio_vs_requested_range",
        ],
        "blocks_import": [
            "missing_required_columns",
            "invalid_or_missing_manifest",
            "coverage_ratio_below_0.80",
            "checksum_mismatch",
            "no_paid_download_authorization",
            "missing_explicit_human_authorization",
            "license_terms_not_confirmed",
        ],
        "authorization_required_fields": list(MANIFEST_AUTHORIZATION_FIELDS),
        "authorization_rule": "promote is NEVER allowed without "
                              "explicit_human_authorization=true + "
                              "license_terms_confirmed=true + non-empty "
                              "authorization_reference; paid/unknown-cost "
                              "sources additionally require "
                              "paid_download_authorized=true",
        "allows_research_only": [
            "intermediate_history_for_diagnostics",
            "staging_inspection_without_publish",
        ],
        "atomic_promote": "write staging -> validate -> manifest+checksums -> "
                          "archive current raw -> move processed into raw (only if all gates pass)",
        "rollback": "restore archived raw snapshot if any post-promote check fails",
        "lineage": "manifest records source_provider, license_terms, ranges, "
                   "symbols, timeframes, checksums, gates, timestamps",
        "never": [
            "replace_good_raw_with_insufficient_staging",
            "paid_download_without_explicit_authorization",
            "db_writes_to_runtime_tables",
            "mutate_env_or_secrets",
        ],
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


@dataclass
class AcquisitionEvaluation:
    valid_manifest: bool = False
    missing_fields: list[str] = field(default_factory=list)
    clean_days: float = 0.0
    coverage_ratio: float = 0.0
    gap_ratio: float = 0.0
    duplicate_ratio: float = 0.0
    missing_oi_ratio: float = 0.0
    missing_oi_status: str = ""
    data_classification: str = CLASS_NO_DATA
    oi_bucket_policy: str = OI_POLICY_BLOCK
    explicit_human_authorization: bool = False
    paid_download_authorized: bool = False
    license_terms_confirmed: bool = False
    authorization_reference: str = ""
    authorization_ok: bool = False
    promote_allowed: bool = False
    do_not_replace_raw: bool = True
    paid_download_requires_authorization: bool = True
    status: str = ST_INVALID_MANIFEST
    blockers: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_ready: bool = False
    live_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ratio(num: Any, den: Any) -> float:
    try:
        n = float(num)
        d = float(den)
        return round(n / d, 4) if d > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def evaluate_acquisition_manifest(manifest: dict[str, Any] | None) -> AcquisitionEvaluation:
    """Pure gate: decides whether a staged import may be promoted. Never
    promotes on insufficient/invalid data; never enables paper/live."""
    ev = AcquisitionEvaluation()
    m = dict(manifest or {})
    blockers: list[str] = []

    # 1) Manifest validity.
    missing = [f for f in MANIFEST_REQUIRED_FIELDS if f not in m]
    ev.missing_fields = missing
    ev.valid_manifest = not missing
    if missing:
        blockers.append("invalid_or_missing_manifest")
        ev.blockers = blockers
        ev.status = ST_INVALID_MANIFEST
        ev.promote_allowed = False
        return ev

    ev.clean_days = float(m.get("clean_days") or 0.0)
    ev.coverage_ratio = float(m.get("coverage_ratio") or 0.0)
    rows_total = sum(int(v or 0) for v in (m.get("rows_by_type") or {}).values()) or int(m.get("rows_total") or 0)
    ev.gap_ratio = _ratio(m.get("gap_count"), rows_total)
    ev.duplicate_ratio = _ratio(m.get("duplicate_count"), rows_total)
    ev.missing_oi_ratio = float(m.get("missing_oi_ratio") or 0.0)
    ev.missing_oi_status = str(m.get("missing_oi_status") or "")

    # OI bucket policy (conservative — same rule as V10.3.1).
    oi_unavailable = ev.missing_oi_status.upper() in OI_UNKNOWN_STATUSES
    oi_bad = (oi_unavailable
              or ev.missing_oi_status in (STATUS_CLUSTERED, STATUS_HIGH, STATUS_MODERATE)
              or ev.missing_oi_ratio > MISSING_OI_BLOCK_RATIO)
    ev.oi_bucket_policy = OI_POLICY_BLOCK if oi_bad else OI_POLICY_ALLOW

    # Data classification by clean days.
    d = ev.clean_days
    if d <= 0:
        ev.data_classification = CLASS_NO_DATA
    elif d < REQUIRED_MIN_HISTORY_DAYS:
        ev.data_classification = CLASS_INTERMEDIATE
    elif d < STRONGER_HISTORY_DAYS:
        ev.data_classification = CLASS_INITIAL
    else:
        ev.data_classification = CLASS_STRONGER

    # 2) Coverage gate (never replace good data with undercovered staging).
    if ev.coverage_ratio < MIN_COVERAGE_RATIO:
        blockers.append(f"coverage_ratio_below_0.80({ev.coverage_ratio})")
        ev.status = ST_UNDERCOVERAGE
        ev.promote_allowed = False
        ev.do_not_replace_raw = True
        ev.blockers = blockers
        return ev

    # 3) History gate.
    if d < REQUIRED_MIN_HISTORY_DAYS:
        blockers.append(f"clean_days_below_180({d})")
        ev.status = ST_NEED_LONG_HISTORY
        ev.promote_allowed = False
        ev.blockers = blockers
        return ev

    # 4) Quality gates.
    if ev.gap_ratio > MAX_GAP_RATIO:
        blockers.append(f"gap_ratio_too_high({ev.gap_ratio})")
    if ev.duplicate_ratio > MAX_DUP_RATIO:
        blockers.append(f"duplicate_ratio_too_high({ev.duplicate_ratio})")
    if not (m.get("checksums_sha256") or {}):
        blockers.append("missing_checksums")
    if blockers:
        ev.status = ST_QUALITY_FAIL
        ev.promote_allowed = False
        ev.blockers = blockers
        return ev

    # 5) V10.4.1 (Codex P1) — explicit human authorization gate. Even when
    # every quality gate passes, a promote is NEVER allowed without explicit
    # human authorization. Missing/None/falsy fields mean NOT authorized.
    ev.explicit_human_authorization = m.get("explicit_human_authorization") is True
    ev.license_terms_confirmed = m.get("license_terms_confirmed") is True
    ev.authorization_reference = str(m.get("authorization_reference")
                                     or m.get("human_approval_reference") or "").strip()
    paid_auth_field = m.get("paid_download_authorized") is True
    source = str(m.get("source_provider") or "").strip().lower()
    requires_paid_auth = source not in _KNOWN_FREE_PROVIDERS
    ev.paid_download_authorized = paid_auth_field

    if not ev.explicit_human_authorization:
        blockers.append("missing_explicit_human_authorization")
    if not ev.license_terms_confirmed:
        blockers.append("license_terms_not_confirmed")
    if not ev.authorization_reference:
        blockers.append("missing_authorization_reference")
    if requires_paid_auth and not paid_auth_field:
        blockers.append("paid_download_not_authorized")
    if blockers:
        ev.status = ST_AUTHORIZATION_REQUIRED
        ev.promote_allowed = False
        ev.do_not_replace_raw = True
        ev.authorization_ok = False
        ev.blockers = blockers
        return ev
    ev.authorization_ok = True

    # Promote allowed (research-only). Still NEVER paper/live ready, and OI
    # buckets remain blocked if OI is bad.
    ev.status = ST_PROMOTE_ALLOWED
    ev.promote_allowed = True
    ev.do_not_replace_raw = False  # valid + covered + quality + human-authorized
    ev.blockers = []
    return ev
