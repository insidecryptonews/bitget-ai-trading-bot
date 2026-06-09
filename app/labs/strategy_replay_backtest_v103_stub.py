"""ResearchOps V10.2.2 — Strategy Replay Backtester STUB (guard-only).

This is NOT the backtester. It is a safe guard that returns the hard
blockers from the design contract so we cannot fool ourselves into
"backtesting" on insufficient/undercovered data. It never simulates a
trade, never optimises, never promotes.

Returns one of:
- ``NEED_LONG_HISTORY``    — < 180 days of clean data.
- ``UNDERCOVERAGE_BLOCK``  — latest fetch reported undercoverage.
- ``MISSING_OI_RISK``      — strategy uses OI and missing OI > 10%.
- ``RESEARCH_ONLY``        — guards pass; engine still NOT implemented.

HARD CONTRACT — research only: no orders, no private endpoints, no DB
writes, no runtime touched. paper_ready / live_ready always False. NO LIVE.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from .external_event_study_v10_1 import build_market_series

STATUS_NEED_LONG_HISTORY = "NEED_LONG_HISTORY"
STATUS_UNDERCOVERAGE_BLOCK = "UNDERCOVERAGE_BLOCK"
STATUS_MISSING_OI_RISK = "MISSING_OI_RISK"
STATUS_RESEARCH_ONLY = "RESEARCH_ONLY"

MIN_DAYS_FOR_BACKTEST = 180.0
MISSING_OI_THRESHOLD = 0.10
MS_PER_DAY = 86_400_000.0


@dataclass
class ReplayStubReport:
    candidate: str = "ETHUSDT SHORT crowded_longs_flush_z15"
    days_covered: float = 0.0
    min_days_required: float = MIN_DAYS_FOR_BACKTEST
    undercoverage: bool = False
    missing_oi_ratio: float = 0.0
    uses_oi: bool = False
    engine_implemented: bool = False
    status: str = STATUS_NEED_LONG_HISTORY
    blocker: str = ""
    note: str = "Replay backtester engine NOT implemented yet (guard stub only)."
    promotion_ladder: list[str] = field(default_factory=lambda: [
        "RESEARCH_ONLY", "BACKTEST_CANDIDATE", "WALK_FORWARD_CANDIDATE",
        "SHADOW_RESEARCH_ONLY_FUTURE", "PAPER_ELIGIBLE_FUTURE"])
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    paper_ready: bool = False
    live_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_replay_backtest_stub(
    market_clean: list[dict[str, Any]] | None,
    *,
    undercoverage: bool = False,
    missing_oi_ratio: float = 0.0,
    uses_oi: bool = False,
    candidate: str = "ETHUSDT SHORT crowded_longs_flush_z15",
) -> ReplayStubReport:
    rep = ReplayStubReport(candidate=candidate, undercoverage=bool(undercoverage),
                           missing_oi_ratio=round(missing_oi_ratio, 4), uses_oi=bool(uses_oi))
    rows = list(market_clean or [])
    if rows:
        mbs = build_market_series(rows)
        all_ts = sorted(t for s in mbs.values() for t in s["ts"])
        if len(all_ts) >= 2:
            rep.days_covered = round((all_ts[-1] - all_ts[0]) / MS_PER_DAY, 2)

    # Guard precedence (most blocking first).
    if undercoverage:
        rep.status = STATUS_UNDERCOVERAGE_BLOCK
        rep.blocker = "latest_fetch_reported_undercoverage"
        return rep
    if rep.days_covered < MIN_DAYS_FOR_BACKTEST:
        rep.status = STATUS_NEED_LONG_HISTORY
        rep.blocker = f"days_covered({rep.days_covered})<{int(MIN_DAYS_FOR_BACKTEST)}"
        return rep
    if uses_oi and missing_oi_ratio > MISSING_OI_THRESHOLD:
        rep.status = STATUS_MISSING_OI_RISK
        rep.blocker = "oi_strategy_with_missing_oi_gt_10pct"
        return rep
    # Guards pass — but the engine is intentionally not implemented yet.
    rep.status = STATUS_RESEARCH_ONLY
    rep.blocker = "engine_not_implemented"
    return rep
