"""ResearchOps V8/V9 — MFE/MAE Exit Intelligence Lab (research-only).

Evaluates a basket of exit policies against historical shadow trades to learn
which policies reduce TIME deaths and improve net efficiency. Pure simulation:
never applies live exits, never opens orders, never touches the executor.

Output: ``ExitPolicyResult`` per policy + an aggregate ``ExitIntelligenceReport``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import mean
from typing import Any, Iterable


FINAL_RECOMMENDATION_NO_LIVE = "NO LIVE"

EXIT_POLICY_BASELINE = "baseline_tp_sl"
EXIT_POLICY_ATR_TPSL = "atr_tp_sl"
EXIT_POLICY_PROFIT_LOCK = "profit_lock_after_fees"
EXIT_POLICY_BE_AFTER_MFE = "be_after_min_mfe"
EXIT_POLICY_TRAILING_ATR = "trailing_atr"
EXIT_POLICY_DYNAMIC_HOLD = "dynamic_hold_bars"
EXIT_POLICY_TIME_STOP_SMART = "time_stop_smart"
EXIT_POLICY_REGIME_FLIP_EXIT = "regime_flip_exit"
EXIT_POLICY_BTC_REVERSAL_EXIT = "btc_reversal_exit"
EXIT_POLICY_ANTI_LATE_ENTRY = "anti_late_entry"
EXIT_POLICY_ANTI_CHOP = "anti_chop"
EXIT_POLICY_PARTIAL_TP = "partial_tp_simulation"

DEFAULT_POLICIES: tuple[str, ...] = (
    EXIT_POLICY_BASELINE,
    EXIT_POLICY_ATR_TPSL,
    EXIT_POLICY_PROFIT_LOCK,
    EXIT_POLICY_BE_AFTER_MFE,
    EXIT_POLICY_TRAILING_ATR,
    EXIT_POLICY_DYNAMIC_HOLD,
    EXIT_POLICY_TIME_STOP_SMART,
    EXIT_POLICY_REGIME_FLIP_EXIT,
    EXIT_POLICY_BTC_REVERSAL_EXIT,
    EXIT_POLICY_ANTI_LATE_ENTRY,
    EXIT_POLICY_ANTI_CHOP,
    EXIT_POLICY_PARTIAL_TP,
)


@dataclass
class SimulatedTradeInput:
    """Minimal trade shape needed for exit simulation."""
    symbol: str
    side: str
    entry_price: float
    tp1_pct: float
    sl_pct: float
    bars_open: int
    mfe_pct: float
    mae_pct: float
    net_pnl_pct: float
    gross_pnl_pct: float
    stop_hit: bool = False
    tp_hit: bool = False
    time_hit: bool = False
    regime: str = "UNKNOWN"
    btc_aligned: bool = True


@dataclass
class ExitPolicyResult:
    policy: str
    sample_count: int
    avg_net_pct: float
    median_net_pct: float
    time_deaths_pct: float
    tp_pct: float
    sl_pct: float
    delta_net_vs_baseline_pct: float
    bars_open_avg: float
    notes: str = ""
    research_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExitIntelligenceReport:
    hours: int
    timeframe: str
    symbols: list[str]
    policies: list[ExitPolicyResult] = field(default_factory=list)
    best_policy: str = EXIT_POLICY_BASELINE
    best_delta_pct: float = 0.0
    samples: int = 0
    need_more_data: bool = False
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return {
            "hours": self.hours,
            "timeframe": self.timeframe,
            "symbols": list(self.symbols),
            "policies": [p.as_dict() for p in self.policies],
            "best_policy": self.best_policy,
            "best_delta_pct": self.best_delta_pct,
            "samples": self.samples,
            "need_more_data": self.need_more_data,
            "research_only": self.research_only,
            "paper_filter_enabled": self.paper_filter_enabled,
            "can_send_real_orders": self.can_send_real_orders,
            "final_recommendation": self.final_recommendation,
        }


def _baseline_net(t: SimulatedTradeInput) -> float:
    return float(t.net_pnl_pct)


def _atr_tpsl_net(t: SimulatedTradeInput) -> float:
    # If MFE >= 1.5x tp1_pct, lock at MFE * 0.6 ; else baseline.
    if t.tp_hit:
        return t.net_pnl_pct
    if t.mfe_pct >= t.tp1_pct * 1.5:
        return max(t.net_pnl_pct, t.mfe_pct * 0.6 - 0.18)
    return t.net_pnl_pct


def _profit_lock_net(t: SimulatedTradeInput, fee_buffer: float = 0.20) -> float:
    # If MFE >= fee_buffer, lock fee_buffer net minimum.
    if t.tp_hit:
        return t.net_pnl_pct
    if t.mfe_pct >= fee_buffer and t.net_pnl_pct < 0:
        return 0.0
    return t.net_pnl_pct


def _be_after_min_mfe_net(t: SimulatedTradeInput, threshold: float = 0.30) -> float:
    if t.tp_hit:
        return t.net_pnl_pct
    if t.mfe_pct >= threshold and t.net_pnl_pct < 0:
        return -0.18  # break-even minus fees
    return t.net_pnl_pct


def _trailing_atr_net(t: SimulatedTradeInput) -> float:
    if t.tp_hit:
        return t.net_pnl_pct
    if t.mfe_pct > t.tp1_pct:
        # Trail: keep 0.5 of MFE
        return max(t.net_pnl_pct, t.mfe_pct * 0.5 - 0.18)
    return t.net_pnl_pct


def _dynamic_hold_net(t: SimulatedTradeInput, max_bars: int = 24) -> float:
    if t.bars_open > max_bars and t.time_hit:
        # Cut earlier — assume that would have locked at half of MFE if positive.
        if t.mfe_pct > 0:
            return max(t.net_pnl_pct, t.mfe_pct * 0.5 - 0.18)
    return t.net_pnl_pct


def _time_stop_smart_net(t: SimulatedTradeInput) -> float:
    if t.time_hit and t.mae_pct >= -0.30 and t.mfe_pct <= 0.20:
        # Bored exit: avoid time death churning.
        return -0.18
    return t.net_pnl_pct


def _regime_flip_exit_net(t: SimulatedTradeInput) -> float:
    if t.regime in {"RANGE", "SIDEWAYS"} and not t.tp_hit:
        # Pretend a regime-aware exit would close earlier at MFE * 0.4.
        if t.mfe_pct > 0:
            return max(t.net_pnl_pct, t.mfe_pct * 0.4 - 0.18)
    return t.net_pnl_pct


def _btc_reversal_exit_net(t: SimulatedTradeInput) -> float:
    if not t.btc_aligned and not t.tp_hit:
        if t.mfe_pct > 0:
            return max(t.net_pnl_pct, t.mfe_pct * 0.3 - 0.18)
        return -0.18
    return t.net_pnl_pct


def _anti_late_entry_net(t: SimulatedTradeInput) -> float:
    # Discard trades that opened too late (no MFE early on) by zeroing them out.
    if t.mfe_pct < 0.05 and t.net_pnl_pct < 0:
        return 0.0  # would have skipped entry
    return t.net_pnl_pct


def _anti_chop_net(t: SimulatedTradeInput) -> float:
    # If MAE close to MFE (chop), would have skipped.
    if t.mfe_pct > 0 and abs(t.mae_pct) >= t.mfe_pct * 0.8 and t.net_pnl_pct < 0:
        return 0.0
    return t.net_pnl_pct


def _partial_tp_net(t: SimulatedTradeInput) -> float:
    # Take 30% at MFE/2, ride rest.
    if t.mfe_pct <= 0:
        return t.net_pnl_pct
    partial = 0.30 * (t.mfe_pct / 2.0)
    rest = 0.70 * t.net_pnl_pct
    return partial + rest - 0.18 * 0.3  # fees on partial


POLICY_FN = {
    EXIT_POLICY_BASELINE: _baseline_net,
    EXIT_POLICY_ATR_TPSL: _atr_tpsl_net,
    EXIT_POLICY_PROFIT_LOCK: _profit_lock_net,
    EXIT_POLICY_BE_AFTER_MFE: _be_after_min_mfe_net,
    EXIT_POLICY_TRAILING_ATR: _trailing_atr_net,
    EXIT_POLICY_DYNAMIC_HOLD: _dynamic_hold_net,
    EXIT_POLICY_TIME_STOP_SMART: _time_stop_smart_net,
    EXIT_POLICY_REGIME_FLIP_EXIT: _regime_flip_exit_net,
    EXIT_POLICY_BTC_REVERSAL_EXIT: _btc_reversal_exit_net,
    EXIT_POLICY_ANTI_LATE_ENTRY: _anti_late_entry_net,
    EXIT_POLICY_ANTI_CHOP: _anti_chop_net,
    EXIT_POLICY_PARTIAL_TP: _partial_tp_net,
}


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def evaluate_policy(policy: str, trades: list[SimulatedTradeInput]) -> ExitPolicyResult:
    fn = POLICY_FN.get(policy, _baseline_net)
    sim = [fn(t) for t in trades]
    n = len(trades)
    if n == 0:
        return ExitPolicyResult(
            policy=policy,
            sample_count=0,
            avg_net_pct=0.0,
            median_net_pct=0.0,
            time_deaths_pct=0.0,
            tp_pct=0.0,
            sl_pct=0.0,
            delta_net_vs_baseline_pct=0.0,
            bars_open_avg=0.0,
            notes="empty_sample",
        )
    tp = sum(1 for t in trades if t.tp_hit) / n
    sl = sum(1 for t in trades if t.stop_hit) / n
    time_dead = sum(1 for t in trades if t.time_hit) / n
    bars_avg = mean(t.bars_open for t in trades)
    return ExitPolicyResult(
        policy=policy,
        sample_count=n,
        avg_net_pct=mean(sim),
        median_net_pct=_median(sim),
        time_deaths_pct=time_dead,
        tp_pct=tp,
        sl_pct=sl,
        delta_net_vs_baseline_pct=0.0,
        bars_open_avg=float(bars_avg),
    )


def run_exit_intelligence(
    trades: list[SimulatedTradeInput],
    *,
    hours: int = 24,
    timeframe: str = "5m",
    symbols: Iterable[str] | None = None,
    policies: Iterable[str] | None = None,
) -> ExitIntelligenceReport:
    policies_list = list(policies or DEFAULT_POLICIES)
    symbol_list = sorted({t.symbol for t in trades}) if symbols is None else list(symbols)
    results: list[ExitPolicyResult] = []
    if not trades:
        return ExitIntelligenceReport(
            hours=int(hours),
            timeframe=timeframe,
            symbols=symbol_list,
            policies=[],
            best_policy=EXIT_POLICY_BASELINE,
            best_delta_pct=0.0,
            samples=0,
            need_more_data=True,
        )
    baseline = evaluate_policy(EXIT_POLICY_BASELINE, trades)
    for p in policies_list:
        r = evaluate_policy(p, trades)
        r.delta_net_vs_baseline_pct = r.avg_net_pct - baseline.avg_net_pct
        results.append(r)
    best = max(results, key=lambda r: r.delta_net_vs_baseline_pct)
    need_more = len(trades) < 50
    return ExitIntelligenceReport(
        hours=int(hours),
        timeframe=timeframe,
        symbols=symbol_list,
        policies=results,
        best_policy=best.policy,
        best_delta_pct=best.delta_net_vs_baseline_pct,
        samples=len(trades),
        need_more_data=need_more,
    )
