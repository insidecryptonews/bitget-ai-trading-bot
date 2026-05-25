"""Exit Labs — real bar-by-bar offline simulations.

Three labs share a common bar-by-bar simulator over OHLCV:

  - Profit Lock Lab          : break-even / trailing / fixed profit lock
  - Fast Exit Lab            : exit on no-followthrough or N-bar timeout
  - Time Death Reducer       : shorten holding window for chronic TIME exits

Each policy is simulated against the EXACT same entry/exit set as the
baseline backtester. STOP_BEFORE_TP same-bar rule preserved. Entry at i+1
open. Fees + slippage applied via `app.cost_model.explain_cost_breakdown`.

NO RUNTIME HOOK. PaperTrader/ExecutionEngine untouched. No exchange calls.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any, Iterable

import pandas as pd

from .cost_model import explain_cost_breakdown
from .ohlcv_replay_loader import OhlcvReplayLoader
from .real_strategy_backtester import RealStrategyBacktester, _resolve_symbols
from .signal_engine import SignalEngine
from .utils import safe_float


FINAL_RECOMMENDATION = "NO LIVE"

EXIT_TAKE_PROFIT = "TAKE_PROFIT"
EXIT_STOP_LOSS = "STOP_LOSS"
EXIT_HORIZON_CLOSE = "HORIZON_CLOSE"
EXIT_PROFIT_LOCK = "PROFIT_LOCK"
EXIT_BREAKEVEN_HIT = "BREAKEVEN_HIT"
EXIT_TRAILING_HIT = "TRAILING_HIT"
EXIT_FAST_EXIT = "FAST_EXIT"
EXIT_TIME_REDUCED = "TIME_REDUCED"


@dataclass(frozen=True)
class ExitPolicy:
    """Tunable exit policy.

    Any of the optional fields, when set, replace the baseline behaviour for
    that single mechanism. Setting all to None reproduces baseline.
    """

    name: str
    # Profit lock: close as soon as MFE >= threshold_pct.
    profit_lock_threshold_pct: float | None = None
    # Break-even: move stop to entry once MFE >= threshold_pct.
    breakeven_after_mfe_pct: float | None = None
    # Trailing stop: after MFE >= start_pct, follow with `trail_distance_pct`.
    trail_after_mfe_pct: float | None = None
    trail_distance_pct: float | None = None
    # Fast exit: close after `no_followthrough_bars` if MFE never crosses
    # `no_followthrough_min_mfe_pct`.
    no_followthrough_bars: int | None = None
    no_followthrough_min_mfe_pct: float | None = None
    # Time death reducer: cap holding to N bars (replaces max_holding_bars).
    max_holding_bars_override: int | None = None


BASELINE_POLICY = ExitPolicy(name="baseline")


@dataclass
class SimulatedTrade:
    side: str
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    exit_reason: str
    gross_return_pct: float
    net_return_pct: float
    mfe_pct: float
    mae_pct: float
    duration_bars: int
    same_bar_stop_tp_applied: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PolicyComparison:
    policy_name: str
    trades: int
    net_ev: float
    net_pf: float
    win_rate: float
    tp_pct: float
    sl_pct: float
    time_pct: float
    profit_lock_pct: float
    fast_exit_pct: float
    avg_duration_bars: float
    delta_ev_vs_baseline: float
    delta_time_vs_baseline: float
    delta_sl_vs_baseline: float
    decision: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExitLabReport:
    lab_name: str
    symbol: str
    timeframe: str
    hours: int
    baseline_trades: int
    baseline_net_ev: float
    comparisons: list[PolicyComparison] = field(default_factory=list)
    final_recommendation: str = FINAL_RECOMMENDATION
    research_only: bool = True
    no_lookahead_status: str = "OK_PREFIX_ONLY"
    stop_tp_same_bar_rule: str = "STOP_BEFORE_TP"

    def as_dict(self) -> dict[str, Any]:
        return {
            "lab_name": self.lab_name,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "hours": self.hours,
            "baseline_trades": self.baseline_trades,
            "baseline_net_ev": self.baseline_net_ev,
            "comparisons": [c.as_dict() for c in self.comparisons],
            "final_recommendation": self.final_recommendation,
            "research_only": self.research_only,
            "no_lookahead_status": self.no_lookahead_status,
            "stop_tp_same_bar_rule": self.stop_tp_same_bar_rule,
        }


def _direction(side: str) -> int:
    return 1 if str(side or "").upper() == "LONG" else -1


def _simulate_one_trade(
    *,
    side: str,
    entry_index: int,
    entry_price: float,
    stop: float,
    take_profit: float,
    candles: pd.DataFrame,
    policy: ExitPolicy,
    max_holding_bars: int,
    slippage_bps: float = 3.0,
) -> SimulatedTrade:
    """Bar-by-bar simulation honouring STOP_BEFORE_TP rule and the given policy.

    Preserves no-lookahead: only `candles.iloc[entry_index : entry_index+H]`
    are read; never anything later than H bars after entry.
    """
    direction = _direction(side)
    side_upper = str(side or "").upper()
    horizon = int(policy.max_holding_bars_override or max_holding_bars or 0)
    if horizon <= 0:
        horizon = max_holding_bars
    current_stop = stop
    mfe_pct = 0.0
    mae_pct = 0.0
    exit_price = 0.0
    exit_reason = EXIT_HORIZON_CLOSE
    exit_index = min(entry_index + horizon - 1, len(candles) - 1)
    same_bar = False
    bars_since_entry = 0

    last = min(len(candles), entry_index + horizon)
    for index in range(entry_index, last):
        row = candles.iloc[index]
        high = safe_float(row.get("high"))
        low = safe_float(row.get("low"))
        if high <= 0 or low <= 0:
            continue

        # Maintain MFE/MAE (in pct of entry, sign-aware to side).
        if side_upper == "LONG":
            mfe_pct = max(mfe_pct, (high - entry_price) / entry_price * 100.0 * direction)
            mae_pct = min(mae_pct, (low - entry_price) / entry_price * 100.0 * direction)
            stop_hit = low <= current_stop
            tp_hit = high >= take_profit
            curr_mfe_for_policy = (high - entry_price) / entry_price * 100.0
        else:
            mfe_pct = max(mfe_pct, (entry_price - low) / entry_price * 100.0)
            mae_pct = min(mae_pct, (entry_price - high) / entry_price * 100.0)
            stop_hit = high >= current_stop
            tp_hit = low <= take_profit
            curr_mfe_for_policy = (entry_price - low) / entry_price * 100.0

        # STOP_BEFORE_TP same-bar rule preserved.
        if stop_hit and tp_hit:
            exit_price = current_stop
            exit_reason = EXIT_STOP_LOSS
            exit_index = index
            same_bar = True
            break
        if stop_hit:
            exit_price = current_stop
            exit_reason = EXIT_STOP_LOSS
            exit_index = index
            break

        # Profit lock: closes when MFE crosses threshold, but only after the
        # stop checks above. If stop and profit-lock are touched in the same
        # candle, unknown intrabar order is treated conservatively as stop first.
        if (
            policy.profit_lock_threshold_pct is not None
            and policy.profit_lock_threshold_pct > 0
            and curr_mfe_for_policy >= policy.profit_lock_threshold_pct
        ):
            if side_upper == "LONG":
                exit_price = entry_price * (1.0 + policy.profit_lock_threshold_pct / 100.0)
            else:
                exit_price = entry_price * (1.0 - policy.profit_lock_threshold_pct / 100.0)
            exit_reason = EXIT_PROFIT_LOCK
            exit_index = index
            break
        if tp_hit:
            exit_price = take_profit
            exit_reason = EXIT_TAKE_PROFIT
            exit_index = index
            break

        # Break-even after MFE: move stop to entry if MFE crossed threshold.
        if (
            policy.breakeven_after_mfe_pct is not None
            and policy.breakeven_after_mfe_pct > 0
            and curr_mfe_for_policy >= policy.breakeven_after_mfe_pct
        ):
            if side_upper == "LONG" and current_stop < entry_price:
                current_stop = entry_price
            elif side_upper == "SHORT" and current_stop > entry_price:
                current_stop = entry_price

        # Trailing stop: after a start MFE threshold, the stop follows.
        if (
            policy.trail_after_mfe_pct is not None
            and policy.trail_after_mfe_pct > 0
            and policy.trail_distance_pct is not None
            and policy.trail_distance_pct > 0
            and curr_mfe_for_policy >= policy.trail_after_mfe_pct
        ):
            trail_distance = entry_price * policy.trail_distance_pct / 100.0
            if side_upper == "LONG":
                desired_stop = high - trail_distance
                if desired_stop > current_stop:
                    current_stop = desired_stop
            else:
                desired_stop = low + trail_distance
                if desired_stop < current_stop:
                    current_stop = desired_stop

        # Fast exit: close after N bars if MFE hasn't reached min threshold.
        if (
            policy.no_followthrough_bars is not None
            and policy.no_followthrough_bars > 0
        ):
            bars_since_entry += 1
            if (
                bars_since_entry >= policy.no_followthrough_bars
                and policy.no_followthrough_min_mfe_pct is not None
                and curr_mfe_for_policy < policy.no_followthrough_min_mfe_pct
            ):
                exit_price = safe_float(row.get("close"))
                exit_reason = EXIT_FAST_EXIT
                exit_index = index
                break

    if exit_reason == EXIT_HORIZON_CLOSE:
        exit_index = min(entry_index + horizon - 1, len(candles) - 1)
        try:
            exit_price = safe_float(candles.iloc[exit_index].get("close"))
        except IndexError:
            exit_price = entry_price
        if policy.max_holding_bars_override is not None and policy.max_holding_bars_override < (max_holding_bars or 9999):
            exit_reason = EXIT_TIME_REDUCED

    gross_return = ((exit_price - entry_price) / entry_price * 100.0) * direction
    breakdown = explain_cost_breakdown(
        source="trade_signal",
        side=side_upper,
        entry_type="taker",
        exit_type="taker",
        slippage_bps=slippage_bps,
        entry_time=candles.iloc[entry_index].get("timestamp") if "timestamp" in candles.columns else None,
        exit_time=candles.iloc[exit_index].get("timestamp") if "timestamp" in candles.columns else None,
        outcome=exit_reason,
    )
    net_return = gross_return - breakdown.total_cost_bps / 100.0
    return SimulatedTrade(
        side=side_upper,
        entry_index=entry_index,
        exit_index=exit_index,
        entry_price=entry_price,
        exit_price=exit_price,
        stop_loss=stop,
        take_profit=take_profit,
        exit_reason=exit_reason,
        gross_return_pct=gross_return,
        net_return_pct=net_return,
        mfe_pct=mfe_pct,
        mae_pct=mae_pct,
        duration_bars=exit_index - entry_index + 1,
        same_bar_stop_tp_applied=same_bar,
    )


def _baseline_entries(config: Any, symbol: str, frame: pd.DataFrame) -> list[tuple[int, str, float, float, float]]:
    """Generate baseline entries using the real backtester.

    Returns list of (entry_index, side, entry_price, stop, take_profit).
    """
    backtester = RealStrategyBacktester(config)
    result = backtester.run(
        symbol, frame,
        min_order_value_usdt=float(getattr(config, "min_trade_margin_usdt", 5.0)),
        notional_usdt=float(getattr(config, "trade_margin_usdt", 12.0)) * max(1, int(getattr(config, "default_leverage", 1))),
    )
    return [
        (t.entry_index, str(t.side), float(t.entry_price), float(t.stop_loss), float(t.take_profit_1))
        for t in result.trades
    ]


def _simulate_policy(
    entries: list[tuple[int, str, float, float, float]],
    candles: pd.DataFrame,
    policy: ExitPolicy,
    *,
    max_holding_bars: int,
    slippage_bps: float,
) -> list[SimulatedTrade]:
    trades: list[SimulatedTrade] = []
    for (entry_index, side, entry_price, stop, take_profit) in entries:
        if entry_index >= len(candles) or entry_price <= 0:
            continue
        trade = _simulate_one_trade(
            side=side,
            entry_index=entry_index,
            entry_price=entry_price,
            stop=stop,
            take_profit=take_profit,
            candles=candles,
            policy=policy,
            max_holding_bars=max_holding_bars,
            slippage_bps=slippage_bps,
        )
        trades.append(trade)
    return trades


def _summarise(trades: list[SimulatedTrade]) -> dict[str, Any]:
    if not trades:
        return {
            "trades": 0, "net_ev": 0.0, "net_pf": 0.0, "win_rate": 0.0,
            "tp_pct": 0.0, "sl_pct": 0.0, "time_pct": 0.0,
            "profit_lock_pct": 0.0, "fast_exit_pct": 0.0,
            "avg_duration_bars": 0.0,
        }
    net = [t.net_return_pct for t in trades]
    wins = [v for v in net if v > 0]
    losses = [v for v in net if v < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    tp_n = sum(1 for t in trades if t.exit_reason == EXIT_TAKE_PROFIT)
    sl_n = sum(1 for t in trades if t.exit_reason == EXIT_STOP_LOSS)
    tm_n = sum(1 for t in trades if t.exit_reason in {EXIT_HORIZON_CLOSE, EXIT_TIME_REDUCED})
    pl_n = sum(1 for t in trades if t.exit_reason == EXIT_PROFIT_LOCK)
    fe_n = sum(1 for t in trades if t.exit_reason == EXIT_FAST_EXIT)
    n = len(trades)
    return {
        "trades": n,
        "net_ev": sum(net) / n,
        "net_pf": pf,
        "win_rate": len(wins) / n,
        "tp_pct": tp_n / n,
        "sl_pct": sl_n / n,
        "time_pct": tm_n / n,
        "profit_lock_pct": pl_n / n,
        "fast_exit_pct": fe_n / n,
        "avg_duration_bars": sum(t.duration_bars for t in trades) / n,
    }


def _classify_policy(*, baseline: dict[str, Any], candidate: dict[str, Any]) -> str:
    """Decide whether the candidate exit policy is worth recommending."""
    if candidate["trades"] == 0:
        return "NO_TRADES"
    delta_ev = candidate["net_ev"] - baseline["net_ev"]
    if delta_ev > 0.01 and candidate["net_ev"] > 0:
        return "IMPROVES_BASELINE"
    if delta_ev > 0.01:
        return "REDUCES_LOSSES"
    if delta_ev < -0.01:
        return "WORSENS_BASELINE"
    return "MARGINAL"


# Predefined policy catalogs --------------------------------------------------


def profit_lock_policies() -> tuple[ExitPolicy, ...]:
    return (
        BASELINE_POLICY,
        ExitPolicy(name="profit_lock_0_40", profit_lock_threshold_pct=0.40),
        ExitPolicy(name="profit_lock_0_60", profit_lock_threshold_pct=0.60),
        ExitPolicy(name="profit_lock_0_80", profit_lock_threshold_pct=0.80),
        ExitPolicy(name="profit_lock_1_00", profit_lock_threshold_pct=1.00),
        ExitPolicy(name="breakeven_0_50", breakeven_after_mfe_pct=0.50),
        ExitPolicy(name="breakeven_0_80", breakeven_after_mfe_pct=0.80),
        ExitPolicy(
            name="trail_0_80_dist_0_40",
            trail_after_mfe_pct=0.80, trail_distance_pct=0.40,
        ),
    )


def fast_exit_policies() -> tuple[ExitPolicy, ...]:
    return (
        BASELINE_POLICY,
        ExitPolicy(name="fast_exit_3bars_no_mfe_0_2",
                   no_followthrough_bars=3, no_followthrough_min_mfe_pct=0.2),
        ExitPolicy(name="fast_exit_5bars_no_mfe_0_2",
                   no_followthrough_bars=5, no_followthrough_min_mfe_pct=0.2),
        ExitPolicy(name="fast_exit_5bars_no_mfe_0_4",
                   no_followthrough_bars=5, no_followthrough_min_mfe_pct=0.4),
        ExitPolicy(name="fast_exit_8bars_no_mfe_0_5",
                   no_followthrough_bars=8, no_followthrough_min_mfe_pct=0.5),
    )


def time_death_policies() -> tuple[ExitPolicy, ...]:
    return (
        BASELINE_POLICY,
        ExitPolicy(name="time_death_max_10", max_holding_bars_override=10),
        ExitPolicy(name="time_death_max_15", max_holding_bars_override=15),
        ExitPolicy(name="time_death_max_20", max_holding_bars_override=20),
        ExitPolicy(name="time_death_max_25", max_holding_bars_override=25),
    )


def run_exit_lab(
    config: Any,
    db: Any,
    *,
    lab_name: str,
    policies: tuple[ExitPolicy, ...],
    symbol: str,
    hours: int = 72,
    timeframe: str = "5m",
    max_holding_bars: int = 30,
    slippage_bps: float = 3.0,
) -> ExitLabReport:
    """Generic exit lab runner. Same entries (from baseline backtester) re-run
    under each policy. Returns per-policy comparison vs baseline."""
    from datetime import datetime, timedelta, timezone

    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours or 1)))
    loader = OhlcvReplayLoader(db)
    load_result = loader.load_ohlcv(symbols=[symbol], timeframe=timeframe, since=since)
    if load_result.status not in {"OK", "TOO_MANY_GAPS"} or symbol not in load_result.frames_by_symbol:
        return ExitLabReport(
            lab_name=lab_name,
            symbol=symbol,
            timeframe=timeframe,
            hours=int(hours),
            baseline_trades=0,
            baseline_net_ev=0.0,
            comparisons=[],
        )
    frame = load_result.frames_by_symbol[symbol].reset_index(drop=True)
    entries = _baseline_entries(config, symbol, frame)
    if not entries:
        return ExitLabReport(
            lab_name=lab_name,
            symbol=symbol,
            timeframe=timeframe,
            hours=int(hours),
            baseline_trades=0,
            baseline_net_ev=0.0,
            comparisons=[],
        )

    baseline_trades = _simulate_policy(entries, frame, BASELINE_POLICY, max_holding_bars=max_holding_bars, slippage_bps=slippage_bps)
    baseline_summary = _summarise(baseline_trades)

    comparisons: list[PolicyComparison] = []
    for policy in policies:
        trades = _simulate_policy(entries, frame, policy, max_holding_bars=max_holding_bars, slippage_bps=slippage_bps)
        summary = _summarise(trades)
        decision = _classify_policy(baseline=baseline_summary, candidate=summary) if policy.name != "baseline" else "BASELINE"
        comparisons.append(PolicyComparison(
            policy_name=policy.name,
            trades=summary["trades"],
            net_ev=summary["net_ev"],
            net_pf=summary["net_pf"],
            win_rate=summary["win_rate"],
            tp_pct=summary["tp_pct"],
            sl_pct=summary["sl_pct"],
            time_pct=summary["time_pct"],
            profit_lock_pct=summary["profit_lock_pct"],
            fast_exit_pct=summary["fast_exit_pct"],
            avg_duration_bars=summary["avg_duration_bars"],
            delta_ev_vs_baseline=summary["net_ev"] - baseline_summary["net_ev"],
            delta_time_vs_baseline=summary["time_pct"] - baseline_summary["time_pct"],
            delta_sl_vs_baseline=summary["sl_pct"] - baseline_summary["sl_pct"],
            decision=decision,
        ))
    return ExitLabReport(
        lab_name=lab_name,
        symbol=symbol.upper(),
        timeframe=timeframe,
        hours=int(hours),
        baseline_trades=baseline_summary["trades"],
        baseline_net_ev=baseline_summary["net_ev"],
        comparisons=comparisons,
    )


def render_exit_lab_text(report: ExitLabReport) -> str:
    lines = [f"EXIT LAB ({report.lab_name}) START"]
    lines.append(f"symbol: {report.symbol}")
    lines.append(f"timeframe: {report.timeframe}")
    lines.append(f"hours: {report.hours}")
    lines.append(f"baseline_trades: {report.baseline_trades}")
    lines.append(f"baseline_net_ev: {report.baseline_net_ev:.6f}")
    lines.append(f"no_lookahead_status: {report.no_lookahead_status}")
    lines.append(f"stop_tp_same_bar_rule: {report.stop_tp_same_bar_rule}")
    lines.append("comparisons:")
    for c in report.comparisons:
        lines.append(
            f"- {c.policy_name}: trades={c.trades} net_ev={c.net_ev:.6f} "
            f"d_ev={c.delta_ev_vs_baseline:+.6f} net_pf={c.net_pf:.4f} "
            f"TP={c.tp_pct*100:.1f}% SL={c.sl_pct*100:.1f}% TIME={c.time_pct*100:.1f}% "
            f"PL={c.profit_lock_pct*100:.1f}% FE={c.fast_exit_pct*100:.1f}% "
            f"avg_bars={c.avg_duration_bars:.1f} decision={c.decision}"
        )
    lines.append("research_only: true")
    lines.append(f"final_recommendation: {report.final_recommendation}")
    lines.append(f"EXIT LAB ({report.lab_name}) END")
    return "\n".join(lines)


# Convenience runners ---------------------------------------------------------


def run_profit_lock_lab(config: Any, db: Any, *, symbol: str, hours: int = 72, timeframe: str = "5m") -> ExitLabReport:
    return run_exit_lab(config, db, lab_name="profit_lock", policies=profit_lock_policies(),
                        symbol=symbol, hours=hours, timeframe=timeframe)


def run_fast_exit_lab(config: Any, db: Any, *, symbol: str, hours: int = 72, timeframe: str = "5m") -> ExitLabReport:
    return run_exit_lab(config, db, lab_name="fast_exit", policies=fast_exit_policies(),
                        symbol=symbol, hours=hours, timeframe=timeframe)


def run_time_death_reducer_lab(config: Any, db: Any, *, symbol: str, hours: int = 72, timeframe: str = "5m") -> ExitLabReport:
    return run_exit_lab(config, db, lab_name="time_death_reducer", policies=time_death_policies(),
                        symbol=symbol, hours=hours, timeframe=timeframe)
