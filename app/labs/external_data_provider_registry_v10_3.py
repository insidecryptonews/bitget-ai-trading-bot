"""ResearchOps V10.3 — External historical data provider registry + audit.

Research-only. This module documents candidate historical-data providers in
code and computes an objective data-readiness audit so the system can state,
without self-deception: we have a promising candidate, but NOT enough clean
history, and the backtester stays blocked until 180d+ clean data exists.

It makes NO network calls, stores NO secrets, writes NO DB, touches NO
runtime. Pricing / exact rate-limits / exact history depth that cannot be
verified here are marked ``NEEDS_MANUAL_VERIFICATION`` — never invented.

Grounding (from public docs at build time; verify before paying):
- Coinalyze keeps only ~1500-2000 intraday datapoints (1m..12h) and deletes
  old intraday data daily => ~60-80 days at 1h. This is exactly why a 180d
  request only returned ~84d. Daily granularity is retained long-term.
- Tardis.dev has Bitget Futures history since 2024-11-08 (tick-level OI /
  funding / liquidations / derivative tickers).
- CoinGlass exposes OI/funding/liquidation OHLC history (docs say back to
  2019) incl. Bitget.
- Bitget official API exposes historical funding rate; OI/liquidation
  historical depth is not clearly documented (verify).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from .external_event_study_v10_1 import build_market_series
from .external_missing_oi_audit_v10_2 import (
    STATUS_CLUSTERED,
    STATUS_HIGH,
    STATUS_MODERATE,
    run_missing_oi_audit,
)

NEEDS_VERIFY = "NEEDS_MANUAL_VERIFICATION"

# Provider status vocabulary.
ST_CURRENT = "CURRENT"
ST_CANDIDATE = "CANDIDATE"
ST_PROXY_ONLY = "PROXY_ONLY"
ST_ENTERPRISE_ONLY = "ENTERPRISE_ONLY"
ST_NEEDS_VERIFY = "NEEDS_MANUAL_VERIFICATION"
ST_NOT_RECOMMENDED = "NOT_RECOMMENDED"

# Data-readiness thresholds.
REQUIRED_MIN_HISTORY_DAYS = 180
STRONGER_HISTORY_DAYS = 365
MISSING_OI_BLOCK_RATIO = 0.10
MS_PER_DAY = 86_400_000.0

# Backtester readiness / data classification vocab.
READY_NEED_LONG_HISTORY = "NEED_LONG_HISTORY"
READY_NON_OI_ONLY = "READY_FOR_INITIAL_BACKTEST_NON_OI_ONLY"
READY_INITIAL = "READY_FOR_INITIAL_BACKTEST"
CLASS_NO_DATA = "NO_CLEAN_DATA"
CLASS_INTERMEDIATE = "INTERMEDIATE_RESEARCH_ONLY"
CLASS_INITIAL = "INITIAL_VALIDATION_READY"
CLASS_STRONGER = "STRONGER_VALIDATION_READY"
OI_POLICY_BLOCK = "BLOCK_OI_BUCKETS"
OI_POLICY_ALLOW = "ALLOW_OI_BUCKETS_WITH_CARE"


@dataclass
class ProviderRecord:
    provider_id: str
    name: str
    datasets_supported: list[str]
    bitget_perp_support: Any            # True / False / NEEDS_VERIFY
    expected_history_days: Any          # int / NEEDS_VERIFY
    requires_api_key: bool
    paid_data_risk: str                 # free / freemium / paid_subscription / enterprise / NEEDS_VERIFY
    suitable_for_180d: Any
    suitable_for_365d: Any
    suitable_for_oi: Any
    suitable_for_liquidations: Any
    suitable_for_funding: Any
    suitable_for_ohlcv: Any
    notes: str
    status: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# Objective registry. Pricing / exact limits => NEEDS_MANUAL_VERIFICATION.
PROVIDERS: list[ProviderRecord] = [
    ProviderRecord(
        provider_id="coinalyze", name="Coinalyze",
        datasets_supported=["ohlcv", "open_interest", "funding", "liquidations", "long_short_ratio"],
        bitget_perp_support=True, expected_history_days=84,  # ~60-80d at 1h intraday cap (observed ~84)
        requires_api_key=True, paid_data_risk="freemium",
        suitable_for_180d=False, suitable_for_365d=False,
        suitable_for_oi=True, suitable_for_liquidations=True, suitable_for_funding=True,
        suitable_for_ohlcv=True,
        notes="CURRENT provider. Intraday (1m-12h) retention ~1500-2000 datapoints, "
              "old intraday deleted daily => ~60-80d at 1h (explains the ~84d cap; "
              "NOT a from/to bug). Daily granularity retained long-term but too coarse "
              "for the 1h candidate. Good for INTERMEDIATE research only.",
        status=ST_CURRENT),
    ProviderRecord(
        provider_id="tardis_dev", name="Tardis.dev",
        datasets_supported=["ohlcv(from_trades)", "open_interest", "funding", "liquidations",
                            "orderbook", "trades", "derivative_ticker"],
        bitget_perp_support=True, expected_history_days=NEEDS_VERIFY,  # Bitget futures since 2024-11-08
        requires_api_key=True, paid_data_risk="paid_subscription",
        suitable_for_180d=True, suitable_for_365d=True,
        suitable_for_oi=True, suitable_for_liquidations=True, suitable_for_funding=True,
        suitable_for_ohlcv=True,
        notes="Bitget Futures history since 2024-11-08 (tick-level; resample to 1h). "
              "First-day-of-month CSV downloadable without key (sampling). Pricing / "
              "exact plan limits = NEEDS_MANUAL_VERIFICATION before any paid download.",
        status=ST_CANDIDATE),
    ProviderRecord(
        provider_id="coinglass", name="CoinGlass",
        datasets_supported=["open_interest_ohlc", "funding_ohlc", "liquidations", "ohlcv"],
        bitget_perp_support=True, expected_history_days=NEEDS_VERIFY,  # docs: history back to 2019
        requires_api_key=True, paid_data_risk=NEEDS_VERIFY,
        suitable_for_180d=True, suitable_for_365d=True,
        suitable_for_oi=True, suitable_for_liquidations=True, suitable_for_funding=True,
        suitable_for_ohlcv=True,
        notes="Institutional OI/funding/liquidation OHLC history (docs: since 2019), "
              "Bitget supported. Pricing tiers + per-endpoint rate limits = "
              "NEEDS_MANUAL_VERIFICATION.",
        status=ST_CANDIDATE),
    ProviderRecord(
        provider_id="coinapi", name="CoinAPI",
        datasets_supported=["ohlcv", "funding", "open_interest", "trades", "orderbook"],
        bitget_perp_support=NEEDS_VERIFY, expected_history_days=NEEDS_VERIFY,
        requires_api_key=True, paid_data_risk=NEEDS_VERIFY,
        suitable_for_180d=NEEDS_VERIFY, suitable_for_365d=NEEDS_VERIFY,
        suitable_for_oi=True, suitable_for_liquidations=NEEDS_VERIFY, suitable_for_funding=True,
        suitable_for_ohlcv=True,
        notes="Historical funding + OI documented. Bitget perp coverage depth + "
              "liquidations support = NEEDS_MANUAL_VERIFICATION.",
        status=ST_NEEDS_VERIFY),
    ProviderRecord(
        provider_id="kaiko", name="Kaiko",
        datasets_supported=["ohlcv", "funding", "open_interest", "derivatives_analytics", "trades", "orderbook"],
        bitget_perp_support=NEEDS_VERIFY, expected_history_days=NEEDS_VERIFY,
        requires_api_key=True, paid_data_risk="enterprise",
        suitable_for_180d=True, suitable_for_365d=True,
        suitable_for_oi=True, suitable_for_liquidations=NEEDS_VERIFY, suitable_for_funding=True,
        suitable_for_ohlcv=True,
        notes="Enterprise institutional data; strong derivatives history + normalization. "
              "Likely high cost / enterprise contract. Bitget-specific coverage + pricing = "
              "NEEDS_MANUAL_VERIFICATION.",
        status=ST_ENTERPRISE_ONLY),
    ProviderRecord(
        provider_id="ccdata_cryptocompare", name="CCData (CryptoCompare)",
        datasets_supported=["ohlcv", "funding", "open_interest", "trades"],
        bitget_perp_support=NEEDS_VERIFY, expected_history_days=NEEDS_VERIFY,
        requires_api_key=True, paid_data_risk=NEEDS_VERIFY,
        suitable_for_180d=NEEDS_VERIFY, suitable_for_365d=NEEDS_VERIFY,
        suitable_for_oi=NEEDS_VERIFY, suitable_for_liquidations=NEEDS_VERIFY, suitable_for_funding=True,
        suitable_for_ohlcv=True,
        notes="Broad exchange coverage incl. Bitget (per vendor academy). Derivatives "
              "history depth (OI/liquidations) for Bitget perps = NEEDS_MANUAL_VERIFICATION.",
        status=ST_NEEDS_VERIFY),
    ProviderRecord(
        provider_id="bitget_official", name="Bitget Official API",
        datasets_supported=["ohlcv", "funding(history)", "open_interest(current)", "interest_rate"],
        bitget_perp_support=True, expected_history_days=NEEDS_VERIFY,
        requires_api_key=False, paid_data_risk="free",
        suitable_for_180d=NEEDS_VERIFY, suitable_for_365d=NEEDS_VERIFY,
        suitable_for_oi=NEEDS_VERIFY, suitable_for_liquidations=False, suitable_for_funding=True,
        suitable_for_ohlcv=True,
        notes="Source of truth for Bitget. history-fund-rate endpoint exists (retention/"
              "lookback not clearly documented = NEEDS_MANUAL_VERIFICATION). Historical OI "
              "depth uncertain; liquidations history not provided. Best used to CROSS-CHECK "
              "funding from source.",
        status=ST_CANDIDATE),
    ProviderRecord(
        provider_id="binance_okx_proxy", name="Binance/OKX (research proxy)",
        datasets_supported=["ohlcv", "funding", "open_interest", "liquidations(partial)"],
        bitget_perp_support=False, expected_history_days=NEEDS_VERIFY,
        requires_api_key=False, paid_data_risk="free",
        suitable_for_180d=True, suitable_for_365d=True,
        suitable_for_oi=True, suitable_for_liquidations=NEEDS_VERIFY, suitable_for_funding=True,
        suitable_for_ohlcv=True,
        notes="Deep public history, BUT this is NOT Bitget. Use ONLY as a research proxy "
              "for regime/feature study and cross-market context — NEVER as a direct Bitget "
              "trading signal (basis/funding/liquidity differ).",
        status=ST_PROXY_ONLY),
]


def registry_rows() -> list[dict[str, Any]]:
    return [p.as_dict() for p in PROVIDERS]


def recommended_next_providers() -> list[str]:
    """Objective shortlist: candidates that support Bitget perps, cover OI +
    funding + liquidations, and are suitable for 180d. Pricing/limits still
    require manual verification before any paid download."""
    out = []
    for p in PROVIDERS:
        if p.status in (ST_CANDIDATE,) and p.bitget_perp_support is True \
                and p.suitable_for_180d is True \
                and p.suitable_for_oi is True and p.suitable_for_funding is True \
                and p.suitable_for_liquidations is True:
            out.append(p.provider_id)
    return out  # expected: tardis_dev, coinglass


@dataclass
class DataSourceAuditReport:
    current_provider: str = "coinalyze"
    current_clean_days: float = 0.0
    required_min_history_days: int = REQUIRED_MIN_HISTORY_DAYS
    stronger_history_days: int = STRONGER_HISTORY_DAYS
    current_history_status: str = "TOO_SHORT_FOR_FINAL_VALIDATION"
    current_missing_oi_ratio: float = 0.0
    missing_oi_status: str = ""
    oi_bucket_policy: str = OI_POLICY_BLOCK
    data_classification: str = CLASS_NO_DATA
    backtester_readiness: str = READY_NEED_LONG_HISTORY
    recommended_next_provider: str = ""
    provider_candidates: list[str] = field(default_factory=list)
    data_blockers: list[str] = field(default_factory=list)
    allowed_actions: list[str] = field(default_factory=list)
    blocked_actions: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    paper_ready: bool = False
    live_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_data_source_audit(
    market_clean: list[dict[str, Any]] | None,
    raw_market_rows: list[dict[str, Any]] | None,
    *,
    hours: int = 8760,
    current_provider: str = "coinalyze",
) -> DataSourceAuditReport:
    rep = DataSourceAuditReport(current_provider=current_provider)
    rows = list(market_clean or [])

    # Clean days covered.
    if rows:
        mbs = build_market_series(rows)
        all_ts = sorted(t for s in mbs.values() for t in s["ts"])
        if len(all_ts) >= 2:
            rep.current_clean_days = round((all_ts[-1] - all_ts[0]) / MS_PER_DAY, 2)

    # Missing OI (from raw, where it is visible).
    audit = run_missing_oi_audit(raw_market_rows, hours=hours)
    rep.current_missing_oi_ratio = audit.missing_ratio_global
    rep.missing_oi_status = audit.status
    missing_bad = audit.status in (STATUS_CLUSTERED, STATUS_HIGH, STATUS_MODERATE) \
        or audit.missing_ratio_global > MISSING_OI_BLOCK_RATIO
    rep.oi_bucket_policy = OI_POLICY_BLOCK if missing_bad else OI_POLICY_ALLOW

    # History status + classification (strict).
    d = rep.current_clean_days
    if d <= 0:
        rep.current_history_status = "NO_CLEAN_DATA"
        rep.data_classification = CLASS_NO_DATA
    elif d < REQUIRED_MIN_HISTORY_DAYS:
        rep.current_history_status = "TOO_SHORT_FOR_FINAL_VALIDATION"
        rep.data_classification = CLASS_INTERMEDIATE
    elif d < STRONGER_HISTORY_DAYS:
        rep.current_history_status = "ENOUGH_FOR_INITIAL_VALIDATION"
        rep.data_classification = CLASS_INITIAL
    else:
        rep.current_history_status = "ENOUGH_FOR_STRONGER_VALIDATION"
        rep.data_classification = CLASS_STRONGER

    # Backtester readiness (never LIVE).
    if d < REQUIRED_MIN_HISTORY_DAYS:
        rep.backtester_readiness = READY_NEED_LONG_HISTORY
    elif missing_bad:
        rep.backtester_readiness = READY_NON_OI_ONLY
    else:
        rep.backtester_readiness = READY_INITIAL

    rep.provider_candidates = recommended_next_providers()
    rep.recommended_next_provider = rep.provider_candidates[0] if rep.provider_candidates else NEEDS_VERIFY

    # Blockers.
    blockers: list[str] = []
    if d < REQUIRED_MIN_HISTORY_DAYS:
        blockers.append(f"insufficient_clean_history(days={d}<{REQUIRED_MIN_HISTORY_DAYS})")
    if missing_bad:
        blockers.append(f"missing_oi({audit.status},{audit.missing_ratio_global})")
    if current_provider == "coinalyze":
        blockers.append("current_provider_intraday_retention_cap_~84d")
    rep.data_blockers = blockers

    rep.allowed_actions = [
        "run_intermediate_diagnostics_research_only",
        "run_stability_research_on_intermediate_data",
        "manual_provider_verification(pricing,limits,bitget_depth)",
        "collect_more_clean_history_via_chunked_fetcher_staging_only",
    ]
    rep.blocked_actions = [
        "operational_backtester",
        "paper_trading",
        "live_trading",
        "enable_paper_policy_filter",
        "dashboard_trader",
        "replace_raw_with_undercovered_staging",
        "paid_data_download_without_authorization",
    ]
    if rep.oi_bucket_policy == OI_POLICY_BLOCK:
        rep.blocked_actions.append("oi_pure_buckets_promotion")

    # Hard invariants in this phase.
    rep.paper_ready = False
    rep.live_ready = False
    return rep


@dataclass
class ProviderReadinessReport:
    current_provider: str = "coinalyze"
    required_min_history_days: int = REQUIRED_MIN_HISTORY_DAYS
    stronger_history_days: int = STRONGER_HISTORY_DAYS
    providers: list[dict[str, Any]] = field(default_factory=list)
    recommended_next_provider: str = ""
    provider_candidates: list[str] = field(default_factory=list)
    needs_manual_verification: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_ready: bool = False
    live_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_provider_readiness() -> ProviderReadinessReport:
    rep = ProviderReadinessReport()
    rep.providers = registry_rows()
    rep.provider_candidates = recommended_next_providers()
    rep.recommended_next_provider = rep.provider_candidates[0] if rep.provider_candidates else NEEDS_VERIFY
    rep.needs_manual_verification = [
        p.provider_id for p in PROVIDERS
        if p.status == ST_NEEDS_VERIFY or p.expected_history_days == NEEDS_VERIFY
        or p.paid_data_risk == NEEDS_VERIFY
    ]
    return rep
