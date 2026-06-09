"""ResearchOps V10.2 — Long-history validation orchestrator (research-only).

Consolidates the V10.1 research tools over the EXISTING clean/raw data into
a single report so we can decide, with data, whether the ETH-SHORT
candidate survives and what the next research step should be. It reads
data; it never downloads, mutates, or writes to the DB. The ceiling is
research; never paper/live.

Pieces consolidated:
- data health (lightweight),
- funding/OI/liquidation diagnostics (V10.1),
- stability / OOS validator (V10.1),
- missing-OI audit (V10.2),
- history_status (is the window long enough?),
- a consolidated ``next_research_decision``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from .external_event_study_v10_1 import build_market_series
from .external_funding_oi_diagnostics_v10_1 import run_funding_oi_diagnostics
from .external_funding_oi_stability_v10_1 import (
    OI_BASED,
    STATUS_GREEN as STAB_GREEN,
    STATUS_MISSING_OI as STAB_MISSING_OI,
    STATUS_OOS_FAIL as STAB_OOS_FAIL,
    run_funding_oi_stability,
)
from .external_missing_oi_audit_v10_2 import (
    STATUS_CLUSTERED,
    STATUS_DATA_OK,
    STATUS_HIGH,
    STATUS_LOW,
    STATUS_MODERATE,
    run_missing_oi_audit,
)

STATUS_NEED_DATA = "NEED_DATA"
STATUS_OK_LABEL = "OK"

# History adequacy (days of coverage).
HISTORY_TOO_SHORT = "TOO_SHORT_FOR_FINAL_VALIDATION"
HISTORY_INTERMEDIATE = "ENOUGH_FOR_INTERMEDIATE_VALIDATION"
HISTORY_STRONGER = "ENOUGH_FOR_STRONGER_VALIDATION"
DAYS_INTERMEDIATE = 180.0
DAYS_STRONGER = 365.0
MS_PER_DAY = 86_400_000.0

# Consolidated suggestion vocabulary.
SUGG_EXTEND = "EXTEND_HISTORY_BTC_ETH"
SUGG_BACKTEST = "STRATEGY_BACKTEST_DESIGN"
SUGG_FIX_OI = "FIX_MISSING_OI_OR_PROVIDER_CROSSCHECK"
SUGG_REJECT_PIVOT = "REJECT_OR_PIVOT"
SUGG_ALT = "LIMITED_ALT_EXPANSION"
DASHBOARD_NEXT_PHASE = "TRADER_READONLY_AFTER_LONG_HISTORY_VALIDATION"
MAX_LABEL = "SHADOW_RESEARCH_ONLY_FUTURE"


@dataclass
class LongHistoryValidationReport:
    hours: int = 8760
    market_rows: int = 0
    liq_rows: int = 0
    symbols: list[str] = field(default_factory=list)
    days_covered: float = 0.0
    history_status: str = HISTORY_TOO_SHORT
    data_health: dict[str, Any] = field(default_factory=dict)
    candidate_diagnostics: dict[str, Any] = field(default_factory=dict)
    stability_summary: dict[str, Any] = field(default_factory=dict)
    missing_oi_audit: dict[str, Any] = field(default_factory=dict)
    next_research_decision: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    paper_ready: bool = False
    live_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _history_status(days: float) -> str:
    if days < DAYS_INTERMEDIATE:
        return HISTORY_TOO_SHORT
    if days < DAYS_STRONGER:
        return HISTORY_INTERMEDIATE
    return HISTORY_STRONGER


def build_consolidated_decision(
    *,
    history_status: str,
    stability_buckets: list[dict[str, Any]],
    missing_status: str,
) -> dict[str, Any]:
    """Decide the next research step. RECOMMENDS ONLY — implements nothing.
    Never recommends live or paper filter; ceiling SHADOW_RESEARCH_ONLY_FUTURE."""
    greens = [b for b in stability_buckets if b.get("stability_status") == STAB_GREEN]
    greens_non_oi = [b for b in greens if b.get("bucket_id") not in OI_BASED]
    oos_fails = [b for b in stability_buckets if b.get("stability_status") == STAB_OOS_FAIL]
    missing_blocked = [b for b in stability_buckets if b.get("stability_status") == STAB_MISSING_OI]
    missing_bad = missing_status in (STATUS_MODERATE, STATUS_HIGH, STATUS_CLUSTERED)

    decision = {
        "history_status": history_status,
        "any_stability_green": bool(greens),
        "eth_specific_candidate": bool(greens) and all(b.get("symbol_scope") == "ETHUSDT" for b in greens),
        "suggested_next_code_prompt_type": SUGG_EXTEND,
        "dashboard_next_phase": DASHBOARD_NEXT_PHASE,
        "max_label": MAX_LABEL,
        "rationale": "",
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }

    if history_status == HISTORY_TOO_SHORT:
        decision["suggested_next_code_prompt_type"] = SUGG_EXTEND
        decision["rationale"] = "History < 180 days: extend BTC/ETH history before any final validation."
    elif greens_non_oi and missing_status in (STATUS_DATA_OK, STATUS_LOW):
        decision["suggested_next_code_prompt_type"] = SUGG_BACKTEST
        decision["rationale"] = "Non-OI bucket(s) STABILITY_GREEN with acceptable missing-OI; design a read-only strategy backtest (no promotion)."
    elif (greens or missing_blocked) and missing_bad:
        decision["suggested_next_code_prompt_type"] = SUGG_FIX_OI
        decision["rationale"] = "Strong candidate depends on OI but missing-OI is material (>10% / clustered); fix or cross-check provider before judging."
    elif oos_fails and not greens:
        decision["suggested_next_code_prompt_type"] = SUGG_REJECT_PIVOT
        decision["rationale"] = "Candidates fail OOS; reject the funding/OI family for BTC/ETH or pivot."
    elif greens and history_status == HISTORY_STRONGER:
        decision["suggested_next_code_prompt_type"] = SUGG_ALT
        decision["rationale"] = "BTC/ETH validated on stronger history; consider a limited, gated alt expansion (research-only)."
    elif greens:
        decision["suggested_next_code_prompt_type"] = SUGG_BACKTEST
        decision["rationale"] = "Green candidate on intermediate history; design a read-only backtest while collecting more history."
    else:
        decision["suggested_next_code_prompt_type"] = SUGG_REJECT_PIVOT
        decision["rationale"] = "No surviving candidate; reject/pivot."
    return decision


def run_long_history_validation(
    market_clean: list[dict[str, Any]] | None,
    liq_clean: list[dict[str, Any]] | None,
    raw_market_rows: list[dict[str, Any]] | None,
    *,
    hours: int = 8760,
    bootstrap_n: int = 400,
    baseline_n: int = 250,
) -> LongHistoryValidationReport:
    report = LongHistoryValidationReport(hours=int(hours))
    rows = list(market_clean or [])
    report.market_rows = len(rows)
    report.liq_rows = len(list(liq_clean or []))

    # Missing-OI audit runs on RAW (even if clean is empty, raw may exist).
    audit = run_missing_oi_audit(raw_market_rows, hours=hours)
    report.missing_oi_audit = {
        "status": audit.status,
        "missing_ratio_global": audit.missing_ratio_global,
        "worst_symbol": audit.worst_symbol,
        "eth_worse_than_btc": audit.eth_worse_than_btc,
        "clustered": audit.clustered,
        "primary_recommendation": audit.primary_recommendation,
        "recommendations": audit.recommendations,
    }

    if not rows:
        report.status = STATUS_NEED_DATA
        report.data_health = {"status": STATUS_NEED_DATA, "market_rows": 0}
        report.history_status = HISTORY_TOO_SHORT
        report.next_research_decision = build_consolidated_decision(
            history_status=HISTORY_TOO_SHORT, stability_buckets=[], missing_status=audit.status)
        return report

    mbs = build_market_series(rows)
    report.symbols = sorted(mbs.keys())
    all_ts = sorted(t for s in mbs.values() for t in s["ts"])
    report.days_covered = round((all_ts[-1] - all_ts[0]) / MS_PER_DAY, 2) if len(all_ts) >= 2 else 0.0
    report.history_status = _history_status(report.days_covered)
    report.data_health = {
        "status": "DATA_AVAILABLE_RESEARCH_ONLY",
        "market_rows": report.market_rows,
        "liq_rows": report.liq_rows,
        "symbols": report.symbols,
        "days_covered": report.days_covered,
    }

    missing_ratio = audit.missing_ratio_global
    diag = run_funding_oi_diagnostics(rows, liq_clean, hours=hours,
                                      bootstrap_n=bootstrap_n, baseline_n=baseline_n, per_symbol=True)
    report.candidate_diagnostics = {
        "buckets_evaluated": diag.buckets_evaluated,
        "research_green": diag.research_green,
        "watch_only": diag.watch_only,
        "rejected_count": diag.rejected_count,
    }
    stab = run_funding_oi_stability(rows, liq_clean, hours=hours, missing_oi_ratio=missing_ratio,
                                    bootstrap_n=bootstrap_n, baseline_n=baseline_n)
    report.stability_summary = {
        "stability_green": stab.stability_green,
        "watch_only": stab.watch_only,
        "buckets": stab.buckets,
    }

    report.next_research_decision = build_consolidated_decision(
        history_status=report.history_status,
        stability_buckets=stab.buckets,
        missing_status=audit.status,
    )
    report.status = STATUS_OK_LABEL
    return report
