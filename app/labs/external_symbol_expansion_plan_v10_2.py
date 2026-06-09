"""ResearchOps V10.2 — Controlled symbol-expansion PLAN (research-only).

This module ONLY describes a future, gated alt-symbol expansion. It does
NOT download anything, does NOT add symbols to any pipeline, and does NOT
change runtime. Expansion stays blocked until BTC/ETH pass long-history
stability validation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE

STATUS_BLOCKED = "BLOCKED_UNTIL_BTC_ETH_EXTENDED_HISTORY"
STATUS_GATED_OPEN = "GATED_OPEN_FOR_LIMITED_ALT_RESEARCH"

# Future candidate alts (NOT downloaded, NOT activated).
CANDIDATE_ALT_SYMBOLS = ["SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "LINKUSDT"]
MAX_ALT_SYMBOLS_NEXT_PHASE = 3

# Inclusion criteria a candidate alt MUST meet before any download.
ALT_INCLUSION_CRITERIA = [
    "high_liquidity",
    "coinalyze_data_available",
    "oi_funding_liquidations_available",
    "missing_oi_ratio_below_10pct",
    "tolerable_spreads_and_fees",
    "not_low_liquidity_symbol",
]


@dataclass
class AltExpansionPlan:
    alt_expansion_status: str = STATUS_BLOCKED
    candidate_alt_symbols: list[str] = field(default_factory=lambda: list(CANDIDATE_ALT_SYMBOLS))
    max_alt_symbols_next_phase: int = MAX_ALT_SYMBOLS_NEXT_PHASE
    inclusion_criteria: list[str] = field(default_factory=lambda: list(ALT_INCLUSION_CRITERIA))
    btc_eth_validated: bool = False
    notes: list[str] = field(default_factory=list)
    research_only: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_alt_expansion_plan(*, btc_eth_validated: bool = False) -> AltExpansionPlan:
    """Return the gated alt-expansion plan. Even when BTC/ETH are validated
    the status only becomes 'gated open for LIMITED alt research' — never an
    instruction to trade, and never more than ``MAX_ALT_SYMBOLS_NEXT_PHASE``."""
    plan = AltExpansionPlan(btc_eth_validated=bool(btc_eth_validated))
    if btc_eth_validated:
        plan.alt_expansion_status = STATUS_GATED_OPEN
        plan.notes.append("Limited alt research may begin (research-only); max 3-5 alts; each must meet inclusion criteria.")
    else:
        plan.alt_expansion_status = STATUS_BLOCKED
        plan.notes.append("Alt expansion blocked until BTC/ETH pass long-history STABILITY_GREEN without missing-OI risk.")
    return plan
