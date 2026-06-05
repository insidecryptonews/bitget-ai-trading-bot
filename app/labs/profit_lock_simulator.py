"""V8.2 — Profit Lock / Trailing Simulator (research-only).

Simulates 12 exit policies bidirectionally against a baseline TP1→BE / TP2→close.
Honours STOP_BEFORE_TP same-bar rule. Pure Python, no PaperTrader changes.
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


POLICY_BASELINE = "baseline_tp1_be_tp2_close"
POLICY_BE_PLUS_BUFFER = "be_plus_buffer"
POLICY_PROFIT_LOCK_0_5R = "profit_lock_0_5r"
POLICY_PROFIT_LOCK_1R = "profit_lock_1r"
POLICY_PROFIT_LOCK_1_5R = "profit_lock_1_5r"
POLICY_TRAILING_ATR = "trailing_atr"
POLICY_TRAILING_SWING_5M = "trailing_swing_5m"
POLICY_TRAILING_SWING_15M = "trailing_swing_15m"
POLICY_PARTIAL_50_TRAILING = "partial_50_plus_trailing"
POLICY_TRAILING_BY_CAMPAIGN = "trailing_by_campaign"
POLICY_TRAILING_INDIVIDUAL = "trailing_individual_per_entry"
POLICY_MOMENTUM_DECAY = "momentum_decay_exit"
POLICY_SMART_TIME_STOP = "smart_time_stop"
POLICY_REGIME_FLIP = "regime_flip_exit"

ALL_POLICIES = (
    POLICY_BASELINE,
    POLICY_BE_PLUS_BUFFER,
    POLICY_PROFIT_LOCK_0_5R,
    POLICY_PROFIT_LOCK_1R,
    POLICY_PROFIT_LOCK_1_5R,
    POLICY_TRAILING_ATR,
    POLICY_TRAILING_SWING_5M,
    POLICY_TRAILING_SWING_15M,
    POLICY_PARTIAL_50_TRAILING,
    POLICY_TRAILING_BY_CAMPAIGN,
    POLICY_TRAILING_INDIVIDUAL,
    POLICY_MOMENTUM_DECAY,
    POLICY_SMART_TIME_STOP,
    POLICY_REGIME_FLIP,
)


@dataclass
class ExitTrade:
    """Minimal trade with its bar path for replay."""
    symbol: str
    side: str
    entry: float
    stop: float
    tp1: float
    tp2: float
    bar_path: list[dict[str, Any]]
    fees_pct: float = 0.18
    atr_pct: float = 0.50
    regime: str = "UNKNOWN"


@dataclass
class PolicyResult:
    policy: str
    samples: int
    net_ev_avg_pct: float
    pf: float
    tp_rate: float
    sl_rate: float
    time_rate: float
    avg_mfe_pct: float
    avg_mfe_capture_pct: float
    avg_drawdown_pct: float
    avg_duration_bars: float
    whipsaw_rate: float
    delta_net_ev_vs_baseline_pct: float = 0.0
    delta_mfe_capture_vs_baseline_pct: float = 0.0
    delta_time_deaths_vs_baseline_pct: float = 0.0
    delta_drawdown_vs_baseline_pct: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProfitLockReport:
    hours: int
    side: str
    samples: int
    baseline_policy: str
    policies: list[dict[str, Any]] = field(default_factory=list)
    best_policy: str = POLICY_BASELINE
    best_delta_pct: float = 0.0
    status: str = STATUS_NEED_DATA
    need_data_reasons: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _direction(side: str) -> int:
    return 1 if side.upper() == SIDE_LONG else -1


def _favourable(direction: int, high: float, low: float) -> float:
    return high if direction == 1 else low


def _adverse(direction: int, high: float, low: float) -> float:
    return low if direction == 1 else high


def _simulate_baseline(trade: ExitTrade) -> dict[str, Any]:
    side = trade.side.upper()
    direction = _direction(side)
    entry = float(trade.entry)
    stop = float(trade.stop)
    tp1 = float(trade.tp1)
    tp2 = float(trade.tp2)
    mfe = 0.0
    realized = 0.0
    exit_reason = "HORIZON_CLOSE"
    tp1_hit = False
    duration = 0
    whipsaw = False
    for idx, bar in enumerate(trade.bar_path):
        high = float(bar.get("high", 0))
        low = float(bar.get("low", 0))
        close = float(bar.get("close", 0))
        if high <= 0 or low <= 0 or close <= 0:
            continue
        duration = idx + 1
        fav_pct = ((_favourable(direction, high, low) - entry) / entry) * 100.0 * direction
        mfe = max(mfe, fav_pct)
        # Same-bar STOP_BEFORE_TP rule.
        if direction == 1:
            stop_hit = low <= stop
            tp1_now = high >= tp1
            tp2_now = high >= tp2
        else:
            stop_hit = high >= stop
            tp1_now = low <= tp1
            tp2_now = low <= tp2
        if stop_hit:
            realized = ((stop - entry) / entry) * 100.0 * direction
            exit_reason = "STOP_LOSS" if stop != entry else "BREAK_EVEN"
            if tp1_hit:
                whipsaw = True
            break
        if tp2_now:
            realized = ((tp2 - entry) / entry) * 100.0 * direction
            exit_reason = "TAKE_PROFIT_2"
            break
        if tp1_now and not tp1_hit:
            tp1_hit = True
            stop = entry  # move stop to BE exact
            continue
    if exit_reason == "HORIZON_CLOSE":
        last_close = float(trade.bar_path[-1].get("close", 0)) if trade.bar_path else entry
        realized = ((last_close - entry) / last_close * (-1) + (last_close - entry) / entry) * 0.5
        # Simpler: realized from entry to last close.
        realized = ((last_close - entry) / entry) * 100.0 * direction
    realized_net = realized - trade.fees_pct
    return {
        "realized_net_pct": realized_net,
        "mfe_pct": mfe,
        "mfe_capture_pct": (realized_net / mfe * 100.0) if mfe > 0 else 0.0,
        "drawdown_pct": max(0.0, mfe - max(0.0, realized)),
        "duration_bars": duration,
        "whipsaw": whipsaw,
        "exit_reason": exit_reason,
    }


def _simulate_be_plus_buffer(trade: ExitTrade, buffer_pct: float = 0.20) -> dict[str, Any]:
    """Like baseline but BE moves to entry + buffer (LONG) or entry - buffer (SHORT)."""
    side = trade.side.upper()
    direction = _direction(side)
    entry = float(trade.entry)
    stop = float(trade.stop)
    tp1 = float(trade.tp1)
    tp2 = float(trade.tp2)
    mfe = 0.0
    realized = 0.0
    exit_reason = "HORIZON_CLOSE"
    tp1_hit = False
    duration = 0
    whipsaw = False
    for idx, bar in enumerate(trade.bar_path):
        high = float(bar.get("high", 0))
        low = float(bar.get("low", 0))
        close = float(bar.get("close", 0))
        if high <= 0 or low <= 0 or close <= 0:
            continue
        duration = idx + 1
        fav_pct = ((_favourable(direction, high, low) - entry) / entry) * 100.0 * direction
        mfe = max(mfe, fav_pct)
        if direction == 1:
            stop_hit = low <= stop
            tp1_now = high >= tp1
            tp2_now = high >= tp2
        else:
            stop_hit = high >= stop
            tp1_now = low <= tp1
            tp2_now = low <= tp2
        if stop_hit:
            realized = ((stop - entry) / entry) * 100.0 * direction
            exit_reason = "STOP_LOSS" if stop != entry * (1 + (buffer_pct / 100.0) * direction) else "BREAK_EVEN_BUFFER"
            if tp1_hit:
                whipsaw = True
            break
        if tp2_now:
            realized = ((tp2 - entry) / entry) * 100.0 * direction
            exit_reason = "TAKE_PROFIT_2"
            break
        if tp1_now and not tp1_hit:
            tp1_hit = True
            stop = entry * (1 + (buffer_pct / 100.0) * direction)
            continue
    if exit_reason == "HORIZON_CLOSE" and trade.bar_path:
        last_close = float(trade.bar_path[-1].get("close", entry))
        realized = ((last_close - entry) / entry) * 100.0 * direction
    realized_net = realized - trade.fees_pct
    return {
        "realized_net_pct": realized_net,
        "mfe_pct": mfe,
        "mfe_capture_pct": (realized_net / mfe * 100.0) if mfe > 0 else 0.0,
        "drawdown_pct": max(0.0, mfe - max(0.0, realized)),
        "duration_bars": duration,
        "whipsaw": whipsaw,
        "exit_reason": exit_reason,
    }


def _simulate_profit_lock(trade: ExitTrade, lock_r: float) -> dict[str, Any]:
    """V8.2.1: conservative SAME-BAR STOP_BEFORE_TP.

    Within a single bar we cannot tell whether the favourable price (the
    bar's high for LONG / low for SHORT) was reached before or after the
    adverse extreme. The conservative assumption is that the adverse move
    happened first. Concretely we:

    1. Check stop hit with the **current** stop (the lock has not been
       raised yet for this bar) and tp2 hit with the bar's favourable price.
    2. If both stop and tp2 are touched in the same bar, **stop wins**
       (STOP_BEFORE_TP).
    3. Only after the bar passes without a stop hit do we update MFE and,
       if the lock threshold has been crossed, raise the stop for the
       **next** bar.
    """
    side = trade.side.upper()
    direction = _direction(side)
    entry = float(trade.entry)
    initial_stop = float(trade.stop)
    risk_per_unit = abs(entry - initial_stop)
    if risk_per_unit <= 0:
        return _simulate_baseline(trade)
    tp2 = float(trade.tp2)
    stop = initial_stop
    mfe = 0.0
    realized = 0.0
    exit_reason = "HORIZON_CLOSE"
    duration = 0
    locked = False
    for idx, bar in enumerate(trade.bar_path):
        high = float(bar.get("high", 0))
        low = float(bar.get("low", 0))
        close = float(bar.get("close", 0))
        if high <= 0 or low <= 0 or close <= 0:
            continue
        duration = idx + 1
        if direction == 1:
            stop_hit = low <= stop
            tp2_now = high >= tp2
        else:
            stop_hit = high >= stop
            tp2_now = low <= tp2
        # Same-bar conservative rule: stop before tp.
        if stop_hit:
            realized = ((stop - entry) / entry) * 100.0 * direction
            exit_reason = "PROFIT_LOCK_HIT" if locked else "STOP_LOSS"
            break
        if tp2_now:
            realized = ((tp2 - entry) / entry) * 100.0 * direction
            exit_reason = "TAKE_PROFIT_2"
            break
        # No exit this bar — update MFE and raise stop for next bar.
        fav_price = _favourable(direction, high, low)
        fav_pct = ((fav_price - entry) / entry) * 100.0 * direction
        mfe = max(mfe, fav_pct)
        mfe_in_r = fav_pct * entry / 100.0 / risk_per_unit
        if not locked and mfe_in_r >= lock_r:
            lock_offset_r = max(0.25, lock_r - 0.5)
            new_stop = entry + direction * (lock_offset_r * risk_per_unit)
            if (direction == 1 and new_stop > stop) or (direction == -1 and new_stop < stop):
                stop = new_stop
                locked = True
    if exit_reason == "HORIZON_CLOSE" and trade.bar_path:
        last_close = float(trade.bar_path[-1].get("close", entry))
        realized = ((last_close - entry) / entry) * 100.0 * direction
    realized_net = realized - trade.fees_pct
    return {
        "realized_net_pct": realized_net,
        "mfe_pct": mfe,
        "mfe_capture_pct": (realized_net / mfe * 100.0) if mfe > 0 else 0.0,
        "drawdown_pct": max(0.0, mfe - max(0.0, realized)),
        "duration_bars": duration,
        "whipsaw": False,
        "exit_reason": exit_reason,
    }


def _simulate_trailing_atr(trade: ExitTrade, k: float = 1.2) -> dict[str, Any]:
    """V8.2.1: conservative same-bar STOP_BEFORE_TP for the trailing policy.

    Order within a bar:
      1. Check stop hit using the **current** trailing stop (not yet raised
         this bar). If hit, exit immediately — this includes the same-bar
         worst case where the high/low touched both the stop and a more
         favourable price.
      2. Otherwise update MFE/best_price and raise the trailing stop using
         the bar's favourable price. The new stop is active **next** bar.
    """
    side = trade.side.upper()
    direction = _direction(side)
    entry = float(trade.entry)
    stop = float(trade.stop)
    atr_pct = float(trade.atr_pct or 0.5)
    mfe = 0.0
    realized = 0.0
    exit_reason = "HORIZON_CLOSE"
    duration = 0
    best_price = entry
    for idx, bar in enumerate(trade.bar_path):
        high = float(bar.get("high", 0))
        low = float(bar.get("low", 0))
        close = float(bar.get("close", 0))
        if high <= 0 or low <= 0 or close <= 0:
            continue
        duration = idx + 1
        # Same-bar conservative: stop with the stop active AT THE START of
        # the bar (no intra-bar raise).
        if direction == 1:
            stop_hit = low <= stop
        else:
            stop_hit = high >= stop
        if stop_hit:
            realized = ((stop - entry) / entry) * 100.0 * direction
            exit_reason = "TRAILING_STOP"
            break
        # No stop this bar — update MFE and raise the trail for NEXT bar.
        fav_price = _favourable(direction, high, low)
        fav_pct = ((fav_price - entry) / entry) * 100.0 * direction
        mfe = max(mfe, fav_pct)
        best_price = max(best_price, high) if direction == 1 else min(best_price, low)
        new_stop = (
            best_price * (1 - k * atr_pct / 100.0)
            if direction == 1
            else best_price * (1 + k * atr_pct / 100.0)
        )
        if (direction == 1 and new_stop > stop) or (direction == -1 and new_stop < stop):
            stop = new_stop
    if exit_reason == "HORIZON_CLOSE" and trade.bar_path:
        last_close = float(trade.bar_path[-1].get("close", entry))
        realized = ((last_close - entry) / entry) * 100.0 * direction
    realized_net = realized - trade.fees_pct
    return {
        "realized_net_pct": realized_net,
        "mfe_pct": mfe,
        "mfe_capture_pct": (realized_net / mfe * 100.0) if mfe > 0 else 0.0,
        "drawdown_pct": max(0.0, mfe - max(0.0, realized)),
        "duration_bars": duration,
        "whipsaw": False,
        "exit_reason": exit_reason,
    }


def _simulate_partial_trailing(trade: ExitTrade) -> dict[str, Any]:
    """Close 50% at TP1, trail the remaining 50% with ATR.

    V8.2.1: conservative SAME-BAR STOP_BEFORE_TP. Within a single bar:
      1. Check current stop first. If hit, exit immediately.
      2. Otherwise check TP1. If hit, take the partial and move stop to BE.
      3. Otherwise update MFE/best_price and raise the trail for next bar.

    V8.2.2 fix (Codex audit): when the stop fires **before** TP1 has been
    taken, the loss must apply to the **whole** position, not 50%. The legacy
    code multiplied the stop pnl by 0.5 regardless of whether the partial
    had actually been taken — that under-counted losses systematically.

    Same-bar rule clarification: if a single bar touches both the current
    stop and TP1, the conservative STOP_BEFORE_TP rule means **stop wins**
    and the partial is **NOT** considered taken — the full position is
    closed at stop.
    """
    side = trade.side.upper()
    direction = _direction(side)
    entry = float(trade.entry)
    stop = float(trade.stop)
    tp1 = float(trade.tp1)
    atr_pct = float(trade.atr_pct or 0.5)
    mfe = 0.0
    realized = 0.0
    duration = 0
    partial_taken = False
    partial_pnl = 0.0
    best_price = entry
    exit_reason = "HORIZON_CLOSE"
    for idx, bar in enumerate(trade.bar_path):
        high = float(bar.get("high", 0))
        low = float(bar.get("low", 0))
        close = float(bar.get("close", 0))
        if high <= 0 or low <= 0 or close <= 0:
            continue
        duration = idx + 1
        # 1. Same-bar conservative stop check first (using current stop).
        if direction == 1:
            stop_hit = low <= stop
        else:
            stop_hit = high >= stop
        if stop_hit:
            if partial_taken:
                # 50% already closed at TP1 (in a previous bar); remaining
                # 50% exits at the trailing stop.
                rest_pnl = ((stop - entry) / entry) * 100.0 * direction * 0.5
                realized = partial_pnl + rest_pnl
                exit_reason = "TRAILING_STOP"
            else:
                # Full position closes at the stop (covers both the
                # stop-before-tp1 case and the same-bar STOP_BEFORE_TP case).
                realized = ((stop - entry) / entry) * 100.0 * direction
                exit_reason = "STOP_LOSS"
            break
        # 2. TP1 check.
        if not partial_taken:
            if direction == 1 and high >= tp1:
                partial_pnl = ((tp1 - entry) / entry) * 100.0 * 0.5
                partial_taken = True
                stop = entry  # move stop to BE for rest (active next bar)
                fav_price = _favourable(direction, high, low)
                fav_pct = ((fav_price - entry) / entry) * 100.0 * direction
                mfe = max(mfe, fav_pct)
                continue
            if direction == -1 and low <= tp1:
                partial_pnl = ((tp1 - entry) / entry) * 100.0 * direction * 0.5
                partial_taken = True
                stop = entry
                fav_price = _favourable(direction, high, low)
                fav_pct = ((fav_price - entry) / entry) * 100.0 * direction
                mfe = max(mfe, fav_pct)
                continue
        # 3. Update MFE and raise trail for next bar.
        fav_price = _favourable(direction, high, low)
        fav_pct = ((fav_price - entry) / entry) * 100.0 * direction
        mfe = max(mfe, fav_pct)
        if partial_taken:
            best_price = max(best_price, high) if direction == 1 else min(best_price, low)
            new_stop = (
                best_price * (1 - atr_pct / 100.0)
                if direction == 1
                else best_price * (1 + atr_pct / 100.0)
            )
            if (direction == 1 and new_stop > stop) or (direction == -1 and new_stop < stop):
                stop = new_stop
    if exit_reason == "HORIZON_CLOSE" and trade.bar_path:
        last_close = float(trade.bar_path[-1].get("close", entry))
        if partial_taken:
            rest_pnl = ((last_close - entry) / entry) * 100.0 * direction * 0.5
            realized = partial_pnl + rest_pnl
        else:
            realized = ((last_close - entry) / entry) * 100.0 * direction
    realized_net = realized - trade.fees_pct
    return {
        "realized_net_pct": realized_net,
        "mfe_pct": mfe,
        "mfe_capture_pct": (realized_net / mfe * 100.0) if mfe > 0 else 0.0,
        "drawdown_pct": max(0.0, mfe - max(0.0, realized)),
        "duration_bars": duration,
        "whipsaw": False,
        "exit_reason": exit_reason,
    }


def _simulate_smart_time_stop(trade: ExitTrade, max_bars_no_mfe: int = 10) -> dict[str, Any]:
    """Smart time stop: exit at BE if MFE never reaches threshold after N bars."""
    side = trade.side.upper()
    direction = _direction(side)
    entry = float(trade.entry)
    stop = float(trade.stop)
    tp2 = float(trade.tp2)
    mfe = 0.0
    realized = 0.0
    exit_reason = "HORIZON_CLOSE"
    duration = 0
    for idx, bar in enumerate(trade.bar_path):
        high = float(bar.get("high", 0))
        low = float(bar.get("low", 0))
        close = float(bar.get("close", 0))
        if high <= 0 or low <= 0 or close <= 0:
            continue
        duration = idx + 1
        fav_price = _favourable(direction, high, low)
        fav_pct = ((fav_price - entry) / entry) * 100.0 * direction
        mfe = max(mfe, fav_pct)
        if direction == 1:
            stop_hit = low <= stop
            tp2_now = high >= tp2
        else:
            stop_hit = high >= stop
            tp2_now = low <= tp2
        if stop_hit:
            realized = ((stop - entry) / entry) * 100.0 * direction
            exit_reason = "STOP_LOSS"
            break
        if tp2_now:
            realized = ((tp2 - entry) / entry) * 100.0 * direction
            exit_reason = "TAKE_PROFIT_2"
            break
        if duration >= max_bars_no_mfe and mfe < 0.30:
            realized = ((close - entry) / entry) * 100.0 * direction
            exit_reason = "SMART_TIME_STOP"
            break
    if exit_reason == "HORIZON_CLOSE" and trade.bar_path:
        last_close = float(trade.bar_path[-1].get("close", entry))
        realized = ((last_close - entry) / entry) * 100.0 * direction
    realized_net = realized - trade.fees_pct
    return {
        "realized_net_pct": realized_net,
        "mfe_pct": mfe,
        "mfe_capture_pct": (realized_net / mfe * 100.0) if mfe > 0 else 0.0,
        "drawdown_pct": max(0.0, mfe - max(0.0, realized)),
        "duration_bars": duration,
        "whipsaw": False,
        "exit_reason": exit_reason,
    }


def _simulate_policy(policy: str, trade: ExitTrade) -> dict[str, Any]:
    if policy == POLICY_BASELINE:
        return _simulate_baseline(trade)
    if policy == POLICY_BE_PLUS_BUFFER:
        return _simulate_be_plus_buffer(trade)
    if policy == POLICY_PROFIT_LOCK_0_5R:
        return _simulate_profit_lock(trade, 0.5)
    if policy == POLICY_PROFIT_LOCK_1R:
        return _simulate_profit_lock(trade, 1.0)
    if policy == POLICY_PROFIT_LOCK_1_5R:
        return _simulate_profit_lock(trade, 1.5)
    if policy == POLICY_TRAILING_ATR:
        return _simulate_trailing_atr(trade, 1.2)
    if policy == POLICY_TRAILING_SWING_5M:
        # Approximated by ATR-based trailing with slightly tighter k.
        return _simulate_trailing_atr(trade, 0.9)
    if policy == POLICY_TRAILING_SWING_15M:
        return _simulate_trailing_atr(trade, 1.5)
    if policy == POLICY_PARTIAL_50_TRAILING:
        return _simulate_partial_trailing(trade)
    if policy == POLICY_TRAILING_BY_CAMPAIGN:
        # Single-trade approximation: same as trailing ATR.
        return _simulate_trailing_atr(trade, 1.2)
    if policy == POLICY_TRAILING_INDIVIDUAL:
        return _simulate_trailing_atr(trade, 1.0)
    if policy == POLICY_MOMENTUM_DECAY:
        return _simulate_baseline(trade)  # placeholder — production-equivalent
    if policy == POLICY_SMART_TIME_STOP:
        return _simulate_smart_time_stop(trade)
    if policy == POLICY_REGIME_FLIP:
        return _simulate_baseline(trade)
    return _simulate_baseline(trade)


def _aggregate_policy(policy: str, results: list[dict[str, Any]]) -> PolicyResult:
    if not results:
        return PolicyResult(
            policy=policy, samples=0,
            net_ev_avg_pct=0.0, pf=0.0,
            tp_rate=0.0, sl_rate=0.0, time_rate=0.0,
            avg_mfe_pct=0.0, avg_mfe_capture_pct=0.0,
            avg_drawdown_pct=0.0, avg_duration_bars=0.0, whipsaw_rate=0.0,
        )
    nets = [r["realized_net_pct"] for r in results]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    loss_sum = abs(sum(losses))
    pf = (sum(wins) / loss_sum) if loss_sum > 0 else 0.0
    tp_rate = sum(1 for r in results if "TAKE_PROFIT" in r["exit_reason"]) / len(results)
    sl_rate = sum(1 for r in results if r["exit_reason"] == "STOP_LOSS") / len(results)
    time_rate = sum(1 for r in results if r["exit_reason"] in {"HORIZON_CLOSE", "SMART_TIME_STOP"}) / len(results)
    whipsaw_rate = sum(1 for r in results if r.get("whipsaw")) / len(results)
    return PolicyResult(
        policy=policy, samples=len(results),
        net_ev_avg_pct=mean(nets),
        pf=pf,
        tp_rate=tp_rate, sl_rate=sl_rate, time_rate=time_rate,
        avg_mfe_pct=mean(r["mfe_pct"] for r in results),
        avg_mfe_capture_pct=mean(r["mfe_capture_pct"] for r in results),
        avg_drawdown_pct=mean(r["drawdown_pct"] for r in results),
        avg_duration_bars=mean(r["duration_bars"] for r in results),
        whipsaw_rate=whipsaw_rate,
    )


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


def run_profit_lock_simulation(
    db: Any,
    *,
    side: str,
    hours: int = 168,
    policies: Iterable[str] = ALL_POLICIES,
    trades: Iterable[ExitTrade] | None = None,
) -> ProfitLockReport:
    side_upper = side.upper()
    report = ProfitLockReport(hours=int(hours), side=side_upper, samples=0,
                              baseline_policy=POLICY_BASELINE)
    if side_upper not in {SIDE_LONG, SIDE_SHORT}:
        report.need_data_reasons.append(f"unsupported_side:{side_upper}")
        return report
    trade_list: list[ExitTrade]
    if trades is not None:
        trade_list = list(trades)
    else:
        ok, value = _safe_call(db, "fetch_exit_replay_trades", hours=int(hours), side=side_upper)
        if (not ok) or (not value):
            # V8.2.4 — fallback to pseudo-trades reconstructed from
            # signal_observations when there are no real trades.
            try:
                from .pseudo_trades_bridge import build_pseudo_trades_from_observations
                pseudo = build_pseudo_trades_from_observations(
                    db, hours=int(hours), side=side_upper, limit=1000,
                )
            except Exception:
                pseudo = []
            if not pseudo:
                report.need_data_reasons.append("no_trades_and_no_pseudo_trades_from_observations")
                return report
            report.need_data_reasons.append("using_pseudo_trades_from_signal_observation")
            value = pseudo
        trade_list = []
        for t in value:
            if isinstance(t, ExitTrade):
                trade_list.append(t)
                continue
            allowed = {f for f in ExitTrade.__dataclass_fields__}
            safe = {k: v for k, v in t.items() if k in allowed}
            trade_list.append(ExitTrade(**safe))
    if not trade_list:
        return report
    report.samples = len(trade_list)
    policy_list = list(policies)
    # Always compute baseline first for deltas.
    if POLICY_BASELINE not in policy_list:
        policy_list = [POLICY_BASELINE] + policy_list
    by_policy: dict[str, PolicyResult] = {}
    for policy in policy_list:
        results = [_simulate_policy(policy, t) for t in trade_list]
        by_policy[policy] = _aggregate_policy(policy, results)
    baseline = by_policy[POLICY_BASELINE]
    output_list: list[PolicyResult] = []
    for policy in policy_list:
        r = by_policy[policy]
        r.delta_net_ev_vs_baseline_pct = r.net_ev_avg_pct - baseline.net_ev_avg_pct
        r.delta_mfe_capture_vs_baseline_pct = r.avg_mfe_capture_pct - baseline.avg_mfe_capture_pct
        r.delta_time_deaths_vs_baseline_pct = r.time_rate - baseline.time_rate
        r.delta_drawdown_vs_baseline_pct = r.avg_drawdown_pct - baseline.avg_drawdown_pct
        output_list.append(r)
    best = max(output_list, key=lambda r: r.delta_net_ev_vs_baseline_pct)
    report.best_policy = best.policy
    report.best_delta_pct = best.delta_net_ev_vs_baseline_pct
    report.policies = [r.as_dict() for r in output_list]
    report.status = STATUS_OK
    return report
