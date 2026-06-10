"""ResearchOps V10.4 — Provider Manual Verification Layer (research-only).

Turns the V10.3 provider registry recommendation into a verifiable manual
checklist. It makes NO network calls, requires NO API key, downloads NOTHING,
writes NO DB. Anything not verifiable here stays ``NEEDS_MANUAL_VERIFICATION``
and a paid download is never allowed without explicit human authorization.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from .external_data_provider_registry_v10_3 import NEEDS_VERIFY, PROVIDERS

# Recommendation roles.
REC_PRIMARY = "primary"
REC_FALLBACK = "fallback"
REC_CROSS_CHECK = "cross_check"
REC_PROXY_ONLY = "proxy_only"
REC_CURRENT = "current"
REC_ENTERPRISE = "enterprise_gated"
REC_NEEDS_VERIFY = "needs_manual_verification"
REC_REJECT = "reject"

_RECOMMENDATION_BY_ID = {
    "tardis_dev": REC_PRIMARY,
    "coinglass": REC_FALLBACK,
    "bitget_official": REC_CROSS_CHECK,
    "binance_okx_proxy": REC_PROXY_ONLY,
    "coinalyze": REC_CURRENT,
    "kaiko": REC_ENTERPRISE,
    "coinapi": REC_NEEDS_VERIFY,
    "ccdata_cryptocompare": REC_NEEDS_VERIFY,
}

# Manual checks to complete BEFORE paying / committing to a provider.
MANUAL_CHECKS = [
    "verify_pricing",
    "verify_rate_limits",
    "verify_bitget_perp_history_depth_180d",
    "verify_bitget_perp_history_depth_365d",
    "verify_oi_completeness",
    "verify_funding_completeness",
    "verify_liquidations_completeness",
    "verify_license_and_terms",
    "verify_data_model_compatibility",
    "verify_vendor_lock_in_risk",
]


@dataclass
class ProviderVerification:
    provider_id: str = ""
    name: str = ""
    status: str = ""
    recommendation: str = ""
    bitget_perp_support: Any = None
    suitable_for_180d: Any = None
    suitable_for_365d: Any = None
    paid_data_risk: str = ""
    requires_api_key: bool = False
    manual_checks_pending: list[str] = field(default_factory=list)
    verification_complete: bool = False
    paid_download_authorized: bool = False  # never auto-true
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderVerificationReport:
    primary_candidate: str = ""
    fallback_candidate: str = ""
    cross_check_provider: str = ""
    proxy_provider: str = ""
    providers: list[dict[str, Any]] = field(default_factory=list)
    any_paid_download_authorized: bool = False
    no_paid_download_without_authorization: bool = True
    research_only: bool = True
    paper_ready: bool = False
    live_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pending_checks(p) -> list[str]:
    """Until manual verification is recorded, ALL checks are pending for any
    provider that requires verification (NEEDS_VERIFY anywhere) or is paid."""
    needs = (
        p.expected_history_days == NEEDS_VERIFY
        or p.paid_data_risk in (NEEDS_VERIFY, "paid_subscription", "enterprise")
        or p.bitget_perp_support == NEEDS_VERIFY
        or p.suitable_for_180d == NEEDS_VERIFY
    )
    return list(MANUAL_CHECKS) if needs else []


def run_provider_verification() -> ProviderVerificationReport:
    rep = ProviderVerificationReport()
    out: list[ProviderVerification] = []
    for p in PROVIDERS:
        rec = _RECOMMENDATION_BY_ID.get(p.provider_id, REC_NEEDS_VERIFY)
        pending = _pending_checks(p)
        out.append(ProviderVerification(
            provider_id=p.provider_id, name=p.name, status=p.status, recommendation=rec,
            bitget_perp_support=p.bitget_perp_support,
            suitable_for_180d=p.suitable_for_180d, suitable_for_365d=p.suitable_for_365d,
            paid_data_risk=p.paid_data_risk, requires_api_key=p.requires_api_key,
            manual_checks_pending=pending,
            verification_complete=(len(pending) == 0),
            paid_download_authorized=False,  # ALWAYS false here
            notes=p.notes,
        ))
        if rec == REC_PRIMARY:
            rep.primary_candidate = p.provider_id
        elif rec == REC_FALLBACK:
            rep.fallback_candidate = p.provider_id
        elif rec == REC_CROSS_CHECK:
            rep.cross_check_provider = p.provider_id
        elif rec == REC_PROXY_ONLY:
            rep.proxy_provider = p.provider_id
    rep.providers = [v.as_dict() for v in out]
    return rep
