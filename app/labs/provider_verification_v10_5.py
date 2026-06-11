"""ResearchOps V10.5 — Manual Provider Verification Scorecards.

A read-only/manual layer to verify data providers BEFORE any acquisition.
It makes NO network calls, does NO scraping, needs NO API keys, downloads
NOTHING and never invents pricing/limits/history: anything not verifiable
offline stays ``NEEDS_MANUAL_VERIFICATION``.

Roles: Tardis.dev (primary), CoinGlass (fallback), Bitget official
(cross-check), Binance/OKX (proxy_only — comparative proxy only, never a
direct substitute for Bitget when basis/market-structure mismatch exists).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE

NEEDS_MANUAL = "NEEDS_MANUAL_VERIFICATION"

# Final per-provider statuses.
ST_NEEDS_MANUAL = "NEEDS_MANUAL_VERIFICATION"
ST_SAMPLE_REQUIRED = "SAMPLE_REQUIRED"
ST_READY_FOR_HUMAN_AUTH = "READY_FOR_HUMAN_AUTHORIZATION"
ST_REJECTED = "REJECTED_PROVIDER"
ST_PROXY_ONLY = "PROXY_ONLY"

REQUIRED_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "BNBUSDT", "LINKUSDT", "AVAXUSDT", "ADAUSDT", "DOTUSDT",
]

REQUIRED_HISTORY = {"minimum_days": 180, "preferred_days": 365}

REQUIRED_DATA_TYPES = [
    "ohlcv_candles", "open_interest", "funding_rates", "liquidations",
    "mark_index_price_if_available", "trades_orderbook_optional",
]

REQUIRED_TIMEFRAMES = [
    "1m_if_available", "5m", "15m", "1h", "4h_optional", "1d_optional",
]

QUALITY_CHECKS = [
    "coverage_ratio", "gaps", "duplicates", "timezone",
    "symbol_normalization", "contract_type", "quote_base_naming",
    "timestamp_format", "data_latency", "oi_completeness",
    "liquidation_completeness", "funding_completeness",
]

COMMERCIAL_CHECKS = [
    "pricing", "subscription_type", "license_terms", "redistribution_terms",
    "api_limits", "bulk_export_support", "vendor_lock_in_risk",
    "cancellation_risk",
]

SAMPLE_REQUIREMENT = {
    "must_obtain_sample_before_paid_download": True,
    "sample_symbols": ["BTCUSDT", "ETHUSDT"],
    "sample_range": "7d-30d",
    "sample_must_pass_schema_validation_before_purchase": True,
}


@dataclass
class ProviderScorecard:
    provider_name: str = ""
    role: str = ""
    status: str = ST_NEEDS_MANUAL
    bitget_perp_supported: Any = NEEDS_MANUAL
    usdt_perp_supported: Any = NEEDS_MANUAL
    symbols_required: list[str] = field(default_factory=lambda: list(REQUIRED_SYMBOLS))
    symbols_confirmed: Any = NEEDS_MANUAL
    required_history: dict[str, int] = field(default_factory=lambda: dict(REQUIRED_HISTORY))
    history_confirmed: Any = NEEDS_MANUAL
    required_data_types: list[str] = field(default_factory=lambda: list(REQUIRED_DATA_TYPES))
    data_types_confirmed: Any = NEEDS_MANUAL
    required_timeframes: list[str] = field(default_factory=lambda: list(REQUIRED_TIMEFRAMES))
    timeframes_confirmed: Any = NEEDS_MANUAL
    quality_checks_pending: list[str] = field(default_factory=lambda: list(QUALITY_CHECKS))
    commercial_checks_pending: list[str] = field(default_factory=lambda: list(COMMERCIAL_CHECKS))
    sample_requirement: dict[str, Any] = field(default_factory=lambda: dict(SAMPLE_REQUIREMENT))
    sample_received: bool = False
    sample_validated: bool = False
    paid_download_authorized: bool = False  # never auto-true
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_provider_scorecards() -> list[ProviderScorecard]:
    """Offline scorecards. Claims from vendor marketing are recorded as
    claims pending verification — never as confirmed facts."""
    return [
        ProviderScorecard(
            provider_name="Tardis.dev",
            role="primary",
            status=ST_SAMPLE_REQUIRED,
            bitget_perp_supported="claimed_yes_pending_verification",
            usdt_perp_supported="claimed_yes_pending_verification",
            notes=("Vendor docs claim Bitget derivatives coverage incl. OI/"
                   "funding/liquidations. MUST request BTCUSDT+ETHUSDT sample "
                   "(7-30d) and validate schema before any payment."),
        ),
        ProviderScorecard(
            provider_name="CoinGlass",
            role="fallback",
            status=ST_NEEDS_MANUAL,
            bitget_perp_supported="claimed_yes_pending_verification",
            usdt_perp_supported="claimed_yes_pending_verification",
            notes=("Aggregator with Bitget listings. History depth per symbol, "
                   "export format and license unverified."),
        ),
        ProviderScorecard(
            provider_name="Bitget official API",
            role="cross_check",
            status=ST_NEEDS_MANUAL,
            bitget_perp_supported=True,
            usdt_perp_supported=True,
            notes=("Source of truth for cross-checking ANY external dataset. "
                   "Historical depth limits per endpoint unverified; free."),
        ),
        ProviderScorecard(
            provider_name="Binance/OKX (proxy)",
            role="proxy_only",
            status=ST_PROXY_ONLY,
            bitget_perp_supported=False,
            usdt_perp_supported=True,
            notes=("Comparative proxy ONLY. Never a direct substitute for "
                   "Bitget data when basis/market-structure mismatch exists. "
                   "Usable for regime/sanity cross-checks, not for final "
                   "validation of Bitget strategies."),
        ),
    ]


@dataclass
class ProviderVerificationV105Report:
    primary: str = "Tardis.dev"
    fallback: str = "CoinGlass"
    cross_check: str = "Bitget official API"
    proxy_only: str = "Binance/OKX (proxy)"
    providers: list[dict[str, Any]] = field(default_factory=list)
    any_provider_ready_for_authorization: bool = False
    any_paid_download_authorized: bool = False
    no_external_calls_made: bool = True
    research_only: bool = True
    paper_ready: bool = False
    live_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_provider_verification_v105() -> ProviderVerificationV105Report:
    rep = ProviderVerificationV105Report()
    cards = build_provider_scorecards()
    rep.providers = [c.as_dict() for c in cards]
    rep.any_provider_ready_for_authorization = any(
        c.status == ST_READY_FOR_HUMAN_AUTH for c in cards)
    rep.any_paid_download_authorized = any(c.paid_download_authorized for c in cards)
    # Hard invariants.
    rep.paper_ready = False
    rep.live_ready = False
    return rep
