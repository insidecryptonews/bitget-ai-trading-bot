"""V8.2 — Trend Campaign Simulator (research-only).

Simulates LONG / SHORT trend campaigns with disciplined add-ons (1+0, 1+1,
1+2, 1+3, 1+5, 1+8). All rules are non-martingale: an add is allowed only
when the base entry is in profit and a fresh confirmation appears.

Variants with >3 adds are marked ``HIGH_RISK_SIMULATION``. Nothing here opens
orders or modifies the PaperTrader; pure-Python projection of historical bar
paths.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import mean
from typing import Any, Iterable

from . import (
    FINAL_RECOMMENDATION_NO_LIVE,
    SIDE_LONG,
    SIDE_SHORT,
    STATUS_NEED_DATA,
    STATUS_OK,
)


HIGH_RISK_SIMULATION = "HIGH_RISK_SIMULATION"

DEFAULT_VARIANTS = (0, 1, 2, 3, 5, 8)


@dataclass
class CampaignTrade:
    """A trade or campaign sample.

    ``bar_path`` is a list of dicts with keys ``open``, ``high``, ``low``,
    ``close`` (and optional ``atr``).

    Important: ``atr_pct_at_entry`` is a **percent of entry price**, not an
    absolute price distance. Internally the simulator converts it to an
    absolute distance via ``entry_price * atr_pct_at_entry / 100``. Optionally
    pass ``atr_abs_at_entry`` to bypass the conversion and use the absolute
    distance directly (e.g. for symbols where percent semantics are awkward).
    """
    symbol: str
    side: str
    entry: float
    stop: float
    bar_path: list[dict[str, Any]]
    fees_pct: float = 0.18  # round-trip per leg
    atr_pct_at_entry: float = 0.50  # PERCENT — converted to absolute internally
    atr_abs_at_entry: float | None = None  # optional override in price units
    # Optional context for filtering
    regime: str = "UNKNOWN"


@dataclass
class VariantResult:
    adds_max: int
    samples: int
    net_ev_avg_pct: float
    pf: float
    hit_rate: float
    mfe_avg_pct: float
    mae_avg_pct: float
    max_drawdown_avg_pct: float
    fees_extra_pct: float
    avg_adds_executed: float
    pct_adds_that_helped: float
    high_risk_flag: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CampaignSimulationReport:
    hours: int
    side: str
    samples: int
    variants: list[dict[str, Any]] = field(default_factory=list)
    optimal_adds: int = 0
    insights: list[str] = field(default_factory=list)
    status: str = STATUS_NEED_DATA
    need_data_reasons: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---- Single-campaign simulation -------------------------------------------

def _direction(side: str) -> int:
    return 1 if side.upper() == SIDE_LONG else -1


def _is_in_profit(direction: int, entry: float, price: float, threshold_pct: float = 0.5) -> bool:
    """Is the base entry currently >= threshold_pct in profit?"""
    move_pct = ((price - entry) / entry) * 100.0 * direction
    return move_pct >= threshold_pct


def _stop_hit_for_position(direction: int, low: float, high: float, stop: float) -> bool:
    if direction == 1:
        return low <= stop
    return high >= stop


def simulate_campaign(
    trade: CampaignTrade,
    *,
    max_adds: int,
    min_profit_for_add_pct: float = 0.5,
    add_distance_atr_mult: float = 1.0,
    trailing_atr_mult: float = 0.5,
) -> dict[str, Any]:
    """Simulate one campaign with up to ``max_adds`` add-ons.

    Returns dict with realized net pct, MFE, MAE, drawdown, fees, adds_executed,
    adds_that_helped.
    """
    side = trade.side.upper()
    direction = _direction(side)
    entry = float(trade.entry)
    if entry <= 0:
        return {
            "realized_net_pct": 0.0, "mfe_pct": 0.0, "mae_pct": 0.0,
            "max_drawdown_pct": 0.0, "fees_pct": 0.0,
            "adds_executed": 0, "adds_that_helped": 0, "high_risk": False,
            "exit_reason": "INVALID_ENTRY",
        }
    # V8.2.1 fix: ``atr_pct_at_entry`` is a PERCENT. Convert to an absolute
    # price distance before comparing to the underlying price. The legacy
    # code in V8.2 used ``atr_pct`` directly as if it were an absolute
    # distance, which collapsed for low-priced symbols (DOGE/ADA/DOT) where
    # 0.5 USD is multiple full ranges away from a 0.10 USD entry.
    if trade.atr_abs_at_entry is not None and float(trade.atr_abs_at_entry) > 0:
        atr_abs = float(trade.atr_abs_at_entry)
    else:
        atr_pct = float(trade.atr_pct_at_entry or 0.5)
        atr_abs = max(entry * atr_pct / 100.0, 1e-9)
    # Each "position" is a tuple (entry, stop, weight)
    positions: list[tuple[float, float, float]] = [(entry, trade.stop, 1.0)]
    weight_total = 1.0
    last_add_price = entry
    adds_executed = 0
    adds_that_helped = 0
    mfe = 0.0
    mae = 0.0
    drawdown = 0.0
    peak_progress = 0.0
    fees_pct = trade.fees_pct
    exit_price = entry
    exit_reason = "HORIZON_CLOSE"

    for bar in trade.bar_path:
        try:
            high = float(bar.get("high", 0))
            low = float(bar.get("low", 0))
            close = float(bar.get("close", 0))
        except Exception:
            continue
        if high <= 0 or low <= 0 or close <= 0:
            continue
        favourable = high if direction == 1 else low
        adverse = low if direction == 1 else high

        # Update MFE / MAE on the BASE entry (for diagnostic).
        fav_pct = ((favourable - entry) / entry) * 100.0 * direction
        adv_pct = ((adverse - entry) / entry) * 100.0 * direction
        mfe = max(mfe, fav_pct)
        mae = min(mae, adv_pct)
        peak_progress = max(peak_progress, fav_pct)
        drawdown = max(drawdown, peak_progress - fav_pct)

        # Add-on logic — only if base entry is in profit + distance threshold met.
        # V8.2.1 fix: use ``atr_abs`` (absolute price distance) instead of
        # ``atr_pct`` which is a percent of entry and would otherwise be
        # treated as a raw price gap.
        if adds_executed < max_adds:
            in_profit = _is_in_profit(direction, entry, close, min_profit_for_add_pct)
            # LONG adds on continuation (new highs). SHORT adds on continuation
            # (new lows). Distance threshold uses ``atr_abs``.
            if direction == 1:
                continuation_ok = close >= last_add_price + (atr_abs * add_distance_atr_mult)
            else:
                continuation_ok = close <= last_add_price - (atr_abs * add_distance_atr_mult)
            if in_profit and continuation_ok:
                # Add at this close.
                add_entry = close
                # New stop for the add: ATR-based from add_entry (absolute distance).
                add_stop = (
                    add_entry - atr_abs * 1.2
                    if direction == 1
                    else add_entry + atr_abs * 1.2
                )
                positions.append((add_entry, add_stop, 1.0))
                weight_total += 1.0
                adds_executed += 1
                last_add_price = add_entry

        # Check stop hits on each leg.
        survivors = []
        for (p_entry, p_stop, p_weight) in positions:
            if _stop_hit_for_position(direction, low, high, p_stop):
                # Realize this leg's PnL at stop.
                leg_pct = ((p_stop - p_entry) / p_entry) * 100.0 * direction
                # Account into final pnl proportionally below.
                survivors.append(("STOPPED", p_entry, p_stop, leg_pct, p_weight))
            else:
                survivors.append(("ALIVE", p_entry, p_stop, 0.0, p_weight))
        # If ALL legs are stopped, exit campaign.
        if all(s[0] == "STOPPED" for s in survivors):
            pnl = 0.0
            for tag, p_entry, p_stop, leg_pct, p_weight in survivors:
                pnl += leg_pct * p_weight
            pnl_pct = pnl / max(weight_total, 1.0)
            # Subtract round-trip fees per leg.
            total_fees_pct = fees_pct * len(positions)
            realized = pnl_pct - total_fees_pct
            if realized > 0:
                adds_that_helped = adds_executed  # if profitable, count adds as having helped
            return {
                "realized_net_pct": realized,
                "mfe_pct": mfe,
                "mae_pct": mae,
                "max_drawdown_pct": drawdown,
                "fees_pct": total_fees_pct,
                "adds_executed": adds_executed,
                "adds_that_helped": adds_that_helped,
                "high_risk": max_adds > 3,
                "exit_reason": "ALL_STOPS_HIT",
            }
        # Keep alive positions only.
        positions = [(p_entry, p_stop, p_weight) for tag, p_entry, p_stop, _, p_weight in survivors if tag == "ALIVE"]
        weight_total = sum(p[2] for p in positions) or 1.0
        exit_price = close

    # Reached end of bar path → close everything at last price.
    realized_pct = 0.0
    for (p_entry, _stop, p_weight) in positions:
        leg_pct = ((exit_price - p_entry) / p_entry) * 100.0 * direction
        realized_pct += leg_pct * p_weight
    realized_pct = realized_pct / max(weight_total, 1.0)
    total_fees_pct = fees_pct * (1 + adds_executed)
    realized = realized_pct - total_fees_pct
    if realized > 0:
        adds_that_helped = adds_executed
    return {
        "realized_net_pct": realized,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "max_drawdown_pct": drawdown,
        "fees_pct": total_fees_pct,
        "adds_executed": adds_executed,
        "adds_that_helped": adds_that_helped,
        "high_risk": max_adds > 3,
        "exit_reason": exit_reason,
    }


# ---- Aggregation over many campaigns ---------------------------------------

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


def _aggregate(results: list[dict[str, Any]], adds_max: int) -> VariantResult:
    if not results:
        return VariantResult(
            adds_max=adds_max,
            samples=0,
            net_ev_avg_pct=0.0,
            pf=0.0,
            hit_rate=0.0,
            mfe_avg_pct=0.0,
            mae_avg_pct=0.0,
            max_drawdown_avg_pct=0.0,
            fees_extra_pct=0.0,
            avg_adds_executed=0.0,
            pct_adds_that_helped=0.0,
            high_risk_flag=adds_max > 3,
        )
    nets = [r["realized_net_pct"] for r in results]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    loss_sum = abs(sum(losses))
    pf = (sum(wins) / loss_sum) if loss_sum > 0 else 0.0
    return VariantResult(
        adds_max=adds_max,
        samples=len(results),
        net_ev_avg_pct=mean(nets),
        pf=pf,
        hit_rate=len(wins) / len(nets),
        mfe_avg_pct=mean(r["mfe_pct"] for r in results),
        mae_avg_pct=mean(r["mae_pct"] for r in results),
        max_drawdown_avg_pct=mean(r["max_drawdown_pct"] for r in results),
        fees_extra_pct=mean(r["fees_pct"] for r in results),
        avg_adds_executed=mean(r["adds_executed"] for r in results),
        pct_adds_that_helped=sum(r["adds_that_helped"] for r in results) / max(sum(r["adds_executed"] for r in results), 1),
        high_risk_flag=adds_max > 3,
    )


def run_campaign_simulation(
    db: Any,
    *,
    side: str,
    hours: int = 168,
    max_adds_variants: Iterable[int] = DEFAULT_VARIANTS,
    trades: Iterable[CampaignTrade] | None = None,
) -> CampaignSimulationReport:
    """Run variants 0/1/2/3/5/8 over a set of historical campaigns.

    ``trades`` may be passed directly (tests); otherwise will attempt
    ``db.fetch_campaign_trades(hours=..., side=...)`` and gracefully report
    ``NEED_DATA``.
    """
    side_upper = side.upper()
    report = CampaignSimulationReport(hours=int(hours), side=side_upper, samples=0)
    if side_upper not in {SIDE_LONG, SIDE_SHORT}:
        report.need_data_reasons.append(f"unsupported_side:{side_upper}")
        return report
    trade_list: list[CampaignTrade]
    if trades is not None:
        trade_list = list(trades)
    else:
        ok, value = _safe_call(db, "fetch_campaign_trades", hours=int(hours), side=side_upper)
        if not ok or not value:
            report.need_data_reasons.append("fetch_campaign_trades_method_missing_or_empty")
            return report
        trade_list = [t if isinstance(t, CampaignTrade) else CampaignTrade(**t) for t in value]

    if not trade_list:
        return report
    report.samples = len(trade_list)
    variants_list = list(max_adds_variants)
    variants_out: list[VariantResult] = []
    for adds_max in variants_list:
        results = [
            simulate_campaign(t, max_adds=adds_max) for t in trade_list
        ]
        variant_result = _aggregate(results, adds_max=adds_max)
        variants_out.append(variant_result)
    # Optimal adds = variant with highest net_ev_avg, breaking ties by lowest adds.
    if variants_out:
        best = max(variants_out, key=lambda v: (v.net_ev_avg_pct, -v.adds_max))
        report.optimal_adds = best.adds_max
        for v in variants_out:
            if v.high_risk_flag:
                report.insights.append(f"variant_adds_{v.adds_max}_marked_{HIGH_RISK_SIMULATION}")
        baseline = variants_out[0]
        if baseline.net_ev_avg_pct > 0:
            for v in variants_out[1:]:
                delta = v.net_ev_avg_pct - baseline.net_ev_avg_pct
                if delta > 0:
                    report.insights.append(f"adds_{v.adds_max}_improves_net_ev_by_{delta:.4f}")
                else:
                    report.insights.append(f"adds_{v.adds_max}_worsens_net_ev_by_{abs(delta):.4f}")
    report.variants = [v.as_dict() for v in variants_out]
    report.status = STATUS_OK if report.samples else STATUS_NEED_DATA
    return report
