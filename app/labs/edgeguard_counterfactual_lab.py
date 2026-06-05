"""V8.2.4 — EdgeGuard Counterfactual Lab (research-only).

Reads ``signal_observations`` that were blocked by EdgeGuard / WATCH_ONLY /
no_edge_group_evidence / market_probe_not_actionable, then uses the future
returns bridge to estimate whether each block would have been a winner or a
loser **net** of fees + slippage + (best-effort) funding.

No PaperTrader changes, no live, no private endpoints.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import (
    FINAL_RECOMMENDATION_NO_LIVE,
    SIDE_LONG,
    SIDE_NO_TRADE,
    SIDE_SHORT,
    STATUS_NEED_DATA,
    STATUS_OK,
    STATUS_PARTIAL,
)
from .future_returns_bridge import (
    DEFAULT_HORIZONS_MINUTES,
    DEFAULT_MAX_BARS,
    DEFAULT_SL_PCT,
    DEFAULT_TIMEFRAME,
    DEFAULT_TP_PCT,
    FutureReturnsResult,
    compute_future_returns,
)


EDGEGUARD_REASON_TOKENS = (
    "edge_guard", "edgeguard", "watch_only", "no_edge_group_evidence",
    "market_probe_not_actionable", "shadow_only", "block_paper",
    "candidate_ranking_no_valid_candidates",
)


# Default cost model (bps) — conservative public values.
DEFAULT_FEE_BPS_ROUND_TRIP = 12.0   # taker/taker round trip on VIP0
DEFAULT_SLIPPAGE_BPS = 3.0
DEFAULT_FUNDING_BPS_PER_CROSSING = 1.0


@dataclass
class CounterfactualOutcome:
    signal_id: int | None
    timestamp: str
    symbol: str
    side: str
    regime: str
    score: int | None
    reason: str
    edgeguard_reason: str
    entry_price: float
    mfe_pct: float | None
    mae_pct: float | None
    first_barrier_hit: str | None
    gross_ev_est_pct: float | None
    net_ev_est_pct: float | None
    fee_cost_pct: float
    slippage_cost_pct: float
    funding_cost_pct: float
    classification: str  # blocked_winner / blocked_loser / unclear_need_data
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE


@dataclass
class EdgeGuardCounterfactualReport:
    hours: int
    generated_at: str
    total_edgeguard_blocks: int = 0
    blocks_by_side: dict[str, int] = field(default_factory=dict)
    blocks_by_symbol: dict[str, int] = field(default_factory=dict)
    blocks_by_regime: dict[str, int] = field(default_factory=dict)
    blocks_by_reason: dict[str, int] = field(default_factory=dict)
    estimated_winners: int = 0
    estimated_losers: int = 0
    need_data: int = 0
    gross_ev_avg_pct: float = 0.0
    net_ev_avg_pct: float = 0.0
    top_blocked_winners: list[dict[str, Any]] = field(default_factory=list)
    top_blocked_losers: list[dict[str, Any]] = field(default_factory=list)
    top_unclear_need_data: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_NEED_DATA
    need_data_reasons: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_call(db: Any, method: str, *args, **kwargs) -> tuple[bool, Any]:
    if db is None:
        return False, None
    fn = getattr(db, method, None)
    if fn is None or not callable(fn):
        return False, None
    try:
        return True, fn(*args, **kwargs)
    except Exception:
        return False, None


def _is_edgeguard_blocked(row: dict[str, Any]) -> tuple[bool, str]:
    reason = str(row.get("reason") or "").lower()
    block_reason = str(row.get("block_reason") or "").lower()
    blob = f"{reason} {block_reason}"
    for token in EDGEGUARD_REASON_TOKENS:
        if token in blob:
            return True, reason or block_reason
    # NO_TRADE caused by score threshold or risk is NOT an EdgeGuard block.
    return False, ""


def _classify(net_ev_est_pct: float | None) -> str:
    if net_ev_est_pct is None:
        return "unclear_need_data"
    if net_ev_est_pct > 0:
        return "blocked_winner"
    return "blocked_loser"


def _cost_estimate_pct(side: str, future: FutureReturnsResult) -> tuple[float, float, float]:
    fee_pct = DEFAULT_FEE_BPS_ROUND_TRIP / 100.0
    slip_pct = DEFAULT_SLIPPAGE_BPS / 100.0
    # Funding: 1 bps per 8h crossing approximation if we held > 8h. Bars in
    # 5m timeframe → 96 bars ≈ 8h. ``future.bars_to_tp/sl`` may be None.
    duration_bars = (
        future.bars_to_tp or future.bars_to_sl or future.bars_evaluated or 0
    )
    funding_pct = (DEFAULT_FUNDING_BPS_PER_CROSSING / 100.0) * max(0, duration_bars // 96)
    # SHORT gets a small funding tailwind when funding>0; we keep it
    # conservative (cost only) for V8.2.4 — never invent a tailwind.
    return fee_pct, slip_pct, funding_pct


def _gross_ev_est_pct(future: FutureReturnsResult, tp_pct: float, sl_pct: float) -> float | None:
    """Estimate the gross EV the entry would have produced.

    - If first_barrier_hit == TP → +tp_pct.
    - If first_barrier_hit == SL → -sl_pct.
    - If TIME → close-based 1h return as a proxy (already side-oriented).
    - If NEED_DATA → None.
    """
    hit = future.first_barrier_hit
    if hit == "TP":
        return float(tp_pct)
    if hit == "SL":
        return -float(sl_pct)
    if hit == "TIME":
        ret_1h = future.returns_by_horizon_pct.get("60m")
        if isinstance(ret_1h, (int, float)):
            return float(ret_1h)
        # Fall back to 4h then 24h.
        ret_4h = future.returns_by_horizon_pct.get("240m")
        if isinstance(ret_4h, (int, float)):
            return float(ret_4h)
        ret_24h = future.returns_by_horizon_pct.get("1440m")
        if isinstance(ret_24h, (int, float)):
            return float(ret_24h)
    return None


def analyze_edgeguard_blocks(
    db: Any,
    *,
    hours: int = 168,
    top_n: int = 20,
    rows: Iterable[dict[str, Any]] | None = None,
    tp_pct: float = DEFAULT_TP_PCT,
    sl_pct: float = DEFAULT_SL_PCT,
    timeframe: str = DEFAULT_TIMEFRAME,
    max_bars: int = DEFAULT_MAX_BARS,
) -> EdgeGuardCounterfactualReport:
    """Analyse EdgeGuard-blocked signals.

    ``rows`` allows test injection. In production we read from
    ``db.fetch_signal_observations(hours=...)``.
    """
    report = EdgeGuardCounterfactualReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if rows is None:
        ok, value = _safe_call(db, "fetch_signal_observations", hours=int(hours))
        if not ok or value is None:
            report.need_data_reasons.append("signal_observations_method_missing_or_empty")
            return report
        rows_list = list(value)
    else:
        rows_list = list(rows)
    if not rows_list:
        return report
    # Filter to EdgeGuard-style blocks.
    blocked: list[dict[str, Any]] = []
    for r in rows_list:
        is_eg, eg_reason = _is_edgeguard_blocked(r)
        if is_eg:
            r["_edgeguard_reason"] = eg_reason
            blocked.append(r)
    report.total_edgeguard_blocks = len(blocked)
    if not blocked:
        report.status = STATUS_NEED_DATA
        report.need_data_reasons.append("no_edgeguard_blocked_signals_found")
        return report
    # Aggregate distributions.
    for r in blocked:
        side = str(r.get("side") or SIDE_NO_TRADE).upper()
        regime = str(r.get("market_regime") or r.get("regime") or "UNKNOWN").upper()
        symbol = str(r.get("symbol") or "UNKNOWN").upper()
        reason = str(r.get("_edgeguard_reason") or "unknown")
        report.blocks_by_side[side] = report.blocks_by_side.get(side, 0) + 1
        report.blocks_by_regime[regime] = report.blocks_by_regime.get(regime, 0) + 1
        report.blocks_by_symbol[symbol] = report.blocks_by_symbol.get(symbol, 0) + 1
        report.blocks_by_reason[reason] = report.blocks_by_reason.get(reason, 0) + 1
    # Compute future returns counterfactual for each.
    outcomes: list[CounterfactualOutcome] = []
    gross_sum = 0.0
    net_sum = 0.0
    counted = 0
    for r in blocked:
        side = str(r.get("side") or r.get("proposed_side") or "").upper()
        if side not in {SIDE_LONG, SIDE_SHORT}:
            outcomes.append(CounterfactualOutcome(
                signal_id=int(r.get("id")) if isinstance(r.get("id"), (int, float)) else None,
                timestamp=str(r.get("timestamp") or ""),
                symbol=str(r.get("symbol") or ""),
                side=side or "NO_TRADE",
                regime=str(r.get("market_regime") or r.get("regime") or "UNKNOWN").upper(),
                score=int(r.get("confidence_score") or 0) or None,
                reason=str(r.get("reason") or ""),
                edgeguard_reason=str(r.get("_edgeguard_reason") or ""),
                entry_price=0.0,
                mfe_pct=None, mae_pct=None, first_barrier_hit=None,
                gross_ev_est_pct=None, net_ev_est_pct=None,
                fee_cost_pct=0.0, slippage_cost_pct=0.0, funding_cost_pct=0.0,
                classification="unclear_need_data",
            ))
            report.need_data += 1
            continue
        future = compute_future_returns(
            db, observation=r,
            tp_pct=tp_pct, sl_pct=sl_pct,
            timeframe=timeframe, max_bars=max_bars,
        )
        gross = _gross_ev_est_pct(future, tp_pct, sl_pct)
        fee_pct, slip_pct, funding_pct = _cost_estimate_pct(side, future)
        net = (gross - fee_pct - slip_pct - funding_pct) if gross is not None else None
        classification = _classify(net)
        outcome = CounterfactualOutcome(
            signal_id=int(r.get("id")) if isinstance(r.get("id"), (int, float)) else None,
            timestamp=str(r.get("timestamp") or ""),
            symbol=str(r.get("symbol") or ""),
            side=side,
            regime=str(r.get("market_regime") or r.get("regime") or "UNKNOWN").upper(),
            score=int(r.get("confidence_score") or 0) or None,
            reason=str(r.get("reason") or ""),
            edgeguard_reason=str(r.get("_edgeguard_reason") or ""),
            entry_price=future.entry_price,
            mfe_pct=future.mfe_pct,
            mae_pct=future.mae_pct,
            first_barrier_hit=future.first_barrier_hit,
            gross_ev_est_pct=gross,
            net_ev_est_pct=net,
            fee_cost_pct=fee_pct,
            slippage_cost_pct=slip_pct,
            funding_cost_pct=funding_pct,
            classification=classification,
        )
        outcomes.append(outcome)
        if classification == "blocked_winner":
            report.estimated_winners += 1
            gross_sum += float(gross or 0.0)
            net_sum += float(net or 0.0)
            counted += 1
        elif classification == "blocked_loser":
            report.estimated_losers += 1
            gross_sum += float(gross or 0.0)
            net_sum += float(net or 0.0)
            counted += 1
        else:
            report.need_data += 1
    if counted > 0:
        report.gross_ev_avg_pct = gross_sum / counted
        report.net_ev_avg_pct = net_sum / counted
    winners = sorted(
        [o for o in outcomes if o.classification == "blocked_winner"],
        key=lambda o: float(o.net_ev_est_pct or 0.0), reverse=True,
    )
    losers = sorted(
        [o for o in outcomes if o.classification == "blocked_loser"],
        key=lambda o: float(o.net_ev_est_pct or 0.0),
    )
    unclear = [o for o in outcomes if o.classification == "unclear_need_data"]
    report.top_blocked_winners = [asdict(o) for o in winners[: int(top_n)]]
    report.top_blocked_losers = [asdict(o) for o in losers[: int(top_n)]]
    report.top_unclear_need_data = [asdict(o) for o in unclear[: int(top_n)]]
    if counted == 0 and report.need_data == 0:
        report.status = STATUS_NEED_DATA
    elif report.need_data > 0 and counted > 0:
        report.status = STATUS_PARTIAL
    elif counted > 0:
        report.status = STATUS_OK
    else:
        report.status = STATUS_NEED_DATA
    return report
