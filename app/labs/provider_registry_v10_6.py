"""ResearchOps V10.6 — Provider Strategy & Data Source Matrix (research-only).

A pure, offline registry comparing candidate data providers for Bitget USDT
perpetuals. Makes NO network calls, downloads NOTHING, needs NO API key,
writes NO DB and never touches .env. Vendor marketing claims are recorded as
``claimed_*`` (pending verification) — never as confirmed facts. The conclusion
is always a SAMPLE-first, human-gated recommendation; final NO LIVE.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE

NEEDS_MANUAL = "NEEDS_MANUAL_VERIFICATION"

# integration_status values
ST_CANDIDATE = "candidate"
ST_SAMPLE_REQUIRED = "sample_required"
ST_BLOCKED = "blocked"
ST_VERIFIED = "verified"

# recommendation values
REC_PREFERRED_SAMPLE = "preferred_sample_candidate"
REC_FALLBACK = "fallback"
REC_CROSS_CHECK = "cross_check"
REC_NOT_ENOUGH_INFO = "not_enough_info"

_SAFETY = {
    "no_auto_download": True,
    "no_paid_download": True,
    "no_env_write": True,
    "no_db_write": True,
}


@dataclass
class ProviderV106:
    provider_id: str = ""
    name: str = ""
    data_types_supported: list[str] = field(default_factory=list)
    expected_history_days_free: Any = NEEDS_MANUAL
    expected_history_days_paid: Any = NEEDS_MANUAL
    access_mode: list[str] = field(default_factory=list)
    api_key_required: Any = NEEDS_MANUAL
    paid_required: Any = NEEDS_MANUAL
    manual_sample_supported: Any = NEEDS_MANUAL
    symbols_supported_notes: str = ""
    timeframes_supported: list[str] = field(default_factory=list)
    license_notes: str = ""
    quality_risks: list[str] = field(default_factory=list)
    missing_data_risks: list[str] = field(default_factory=list)
    rate_limit_risks: list[str] = field(default_factory=list)
    integration_status: str = ST_CANDIDATE
    recommendation: str = REC_NOT_ENOUGH_INFO
    safety: dict[str, Any] = field(default_factory=lambda: dict(_SAFETY))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_provider_matrix() -> list[ProviderV106]:
    """Offline matrix. Claims are pending verification; nothing is 'verified'
    until a human validates a sample with provider-sample-validate-v106."""
    full_tf = ["1m", "5m", "15m", "1h", "4h", "1d"]
    return [
        ProviderV106(
            provider_id="tardis_dev", name="Tardis.dev",
            data_types_supported=["ohlcv", "open_interest", "funding",
                                  "liquidations", "trades", "orderbook"],
            expected_history_days_free="trial_only_" + NEEDS_MANUAL,
            expected_history_days_paid="claimed_multi_year_" + NEEDS_MANUAL,
            access_mode=["api", "bulk_download", "csv_export"],
            api_key_required=True, paid_required=True, manual_sample_supported=True,
            symbols_supported_notes="claims Bitget derivatives incl. perps; "
                                    "exact symbol coverage NEEDS_MANUAL_VERIFICATION",
            timeframes_supported=full_tf,
            license_notes="commercial license; redistribution terms " + NEEDS_MANUAL,
            quality_risks=["vendor-normalized symbols may differ from Bitget native"],
            missing_data_risks=["OI/liquidations completeness " + NEEDS_MANUAL],
            rate_limit_risks=["bulk vs api limits " + NEEDS_MANUAL],
            integration_status=ST_SAMPLE_REQUIRED,
            recommendation=REC_PREFERRED_SAMPLE),
        ProviderV106(
            provider_id="coinglass", name="CoinGlass",
            data_types_supported=["ohlcv", "open_interest", "funding",
                                  "liquidations"],
            expected_history_days_free="limited_" + NEEDS_MANUAL,
            expected_history_days_paid="claimed_long_" + NEEDS_MANUAL,
            access_mode=["api"],
            api_key_required=True, paid_required=True, manual_sample_supported=NEEDS_MANUAL,
            symbols_supported_notes="aggregator with Bitget listings; depth per "
                                    "symbol " + NEEDS_MANUAL,
            timeframes_supported=["5m", "15m", "1h", "4h", "1d"],
            license_notes=NEEDS_MANUAL,
            quality_risks=["aggregated/derived series may smooth exchange specifics"],
            missing_data_risks=["per-symbol OI history depth " + NEEDS_MANUAL],
            rate_limit_risks=["tiered API limits " + NEEDS_MANUAL],
            integration_status=ST_CANDIDATE, recommendation=REC_FALLBACK),
        ProviderV106(
            provider_id="coinalyze", name="Coinalyze",
            data_types_supported=["ohlcv", "open_interest", "funding",
                                  "liquidations"],
            expected_history_days_free="~84d_intraday_retention_cap",
            expected_history_days_paid=NEEDS_MANUAL,
            access_mode=["api"],
            api_key_required=True, paid_required=False, manual_sample_supported=False,
            symbols_supported_notes="current local source; Bitget perps via letter "
                                    "codes (exchange:'A')",
            timeframes_supported=["5m", "15m", "1h", "4h", "1d"],
            license_notes="freemium",
            quality_risks=["intraday retention cap ~1500-2000 datapoints"],
            missing_data_risks=["insufficient for 180/365d intraday; OI clustered "
                                "missing observed (24.67% on ETH sample)"],
            rate_limit_risks=["free-tier request caps"],
            integration_status=ST_BLOCKED, recommendation=REC_NOT_ENOUGH_INFO),
        ProviderV106(
            provider_id="bitget_official", name="Bitget public market data",
            data_types_supported=["ohlcv", "open_interest", "funding"],
            expected_history_days_free="endpoint_lookback_limited_" + NEEDS_MANUAL,
            expected_history_days_paid="n/a_free",
            access_mode=["api"],
            api_key_required=False, paid_required=False, manual_sample_supported=True,
            symbols_supported_notes="ground truth for Bitget perps (native symbols)",
            timeframes_supported=full_tf,
            license_notes="public market data; ToS " + NEEDS_MANUAL,
            quality_risks=["per-endpoint max lookback / pagination limits"],
            missing_data_risks=["historical liquidations exposure " + NEEDS_MANUAL],
            rate_limit_risks=["per-IP public limits"],
            integration_status=ST_CANDIDATE, recommendation=REC_CROSS_CHECK),
        ProviderV106(
            provider_id="binance_okx_proxy", name="Binance/OKX (proxy)",
            data_types_supported=["ohlcv", "open_interest", "funding",
                                  "liquidations"],
            expected_history_days_free="long_" + NEEDS_MANUAL,
            expected_history_days_paid="n/a",
            access_mode=["api", "bulk_download"],
            api_key_required=False, paid_required=False, manual_sample_supported=True,
            symbols_supported_notes="NOT Bitget; comparative proxy only — basis/"
                                    "microstructure mismatch makes it unsafe as a "
                                    "direct substitute",
            timeframes_supported=full_tf,
            license_notes="public",
            quality_risks=["different venue: basis, funding and liquidation "
                           "dynamics differ from Bitget"],
            missing_data_risks=["does not represent Bitget order flow"],
            rate_limit_risks=["public limits"],
            integration_status=ST_BLOCKED, recommendation=REC_NOT_ENOUGH_INFO),
    ]


@dataclass
class ProviderMatrixReportV106:
    providers: list[dict[str, Any]] = field(default_factory=list)
    preferred_sample_candidate: str = ""
    fallback: str = ""
    cross_check: str = ""
    any_verified: bool = False
    any_paid_download_authorized: bool = False
    no_network_calls: bool = True
    research_only: bool = True
    paper_ready: bool = False
    live_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_provider_matrix_v106() -> ProviderMatrixReportV106:
    rep = ProviderMatrixReportV106()
    providers = build_provider_matrix()
    rep.providers = [p.as_dict() for p in providers]
    for p in providers:
        if p.recommendation == REC_PREFERRED_SAMPLE and not rep.preferred_sample_candidate:
            rep.preferred_sample_candidate = p.provider_id
        elif p.recommendation == REC_FALLBACK and not rep.fallback:
            rep.fallback = p.provider_id
        elif p.recommendation == REC_CROSS_CHECK and not rep.cross_check:
            rep.cross_check = p.provider_id
    rep.any_verified = any(p.integration_status == ST_VERIFIED for p in providers)
    rep.paper_ready = False
    rep.live_ready = False
    return rep
