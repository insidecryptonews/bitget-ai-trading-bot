"""Quick Profit Exit Lab — RESEARCH ONLY.

Simulates alternative exit policies over historical OHLCV and measures whether
quick-profit umbrals would have improved net_EV vs the current TP1/TP2/TIME baseline.

This module is NOT connected to the trading runtime. PaperTrader and
ExecutionEngine are untouched. No order is placed. No paper filter toggled.

Policies covered:
- baseline:               current TP1 / SL / TIME behaviour (no change)
- quick_profit_040:       close on MFE >= 0.40%
- quick_profit_060:       close on MFE >= 0.60%
- quick_profit_080:       close on MFE >= 0.80%
- quick_profit_100:       close on MFE >= 1.00%
- breakeven_after_050:    move stop to entry once MFE >= 0.50%
- breakeven_after_080:    move stop to entry once MFE >= 0.80%
- trailing_after_080:     break-even after 0.80% and then add trail = 0.40%
- euro_net_threshold:     quick exit when net_unrealized in EUR >= configured value

Each policy is simulated independently against the same baseline candle set.

Cost stress: every policy is evaluated at default cost (0.18%) and the
caller can re-run at 0.22%/0.25% to measure sensitivity.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

import pandas as pd

from .outcome_engine import (
    EXIT_BREAKEVEN,
    EXIT_HORIZON_CLOSE,
    EXIT_QUICK_PROFIT,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    EXIT_TRAILING,
    OutcomeResult,
    simulate_outcome_ohlcv,
)
from .utils import safe_float


FINAL_RECOMMENDATION = "NO LIVE"


@dataclass(frozen=True)
class QuickProfitPolicy:
    name: str
    quick_profit_threshold_pct: float | None = None
    breakeven_after_pct: float | None = None
    trail_after_pct: float | None = None
    trail_distance_pct: float | None = None
    euro_net_threshold: float | None = None
    notional_usdt: float | None = None  # required for euro_net thresholds
    # Honesty flags: True means the simulation is not a faithful intra-bar
    # backtest of the policy. Used so downstream consumers don't read the
    # numbers as production-grade.
    is_approximation_only: bool = False
    approximation_reason: str = ""


BASELINE = QuickProfitPolicy(name="baseline")

DEFAULT_POLICIES: tuple[QuickProfitPolicy, ...] = (
    BASELINE,
    QuickProfitPolicy(name="quick_profit_040", quick_profit_threshold_pct=0.40),
    QuickProfitPolicy(name="quick_profit_060", quick_profit_threshold_pct=0.60),
    QuickProfitPolicy(name="quick_profit_080", quick_profit_threshold_pct=0.80),
    QuickProfitPolicy(name="quick_profit_100", quick_profit_threshold_pct=1.00),
    QuickProfitPolicy(name="breakeven_after_050", breakeven_after_pct=0.50),
    QuickProfitPolicy(name="breakeven_after_080", breakeven_after_pct=0.80),
    QuickProfitPolicy(
        name="trailing_after_080",
        breakeven_after_pct=0.80,
        trail_after_pct=0.80,
        trail_distance_pct=0.40,
        # The lab currently approximates trailing by collapsing it to a
        # breakeven move after MFE crosses 0.80%. There is no bar-by-bar
        # trail of the stop. The summary will surface this so consumers
        # don't read this row as faithful trailing backtest.
        is_approximation_only=True,
        approximation_reason="trailing_collapsed_to_breakeven_no_bar_by_bar_trail",
    ),
)


@dataclass
class PolicySummary:
    policy: str
    trades: int
    gross_ev_pct: float
    net_ev_pct: float
    net_pf: float
    win_rate: float
    max_drawdown_pct: float
    tp_pct: float
    sl_pct: float
    time_pct: float
    quick_profit_pct: float
    breakeven_count: int
    avg_hold_bars: float
    cost_drag_pct: float
    count_gross_win_net_loss: int
    count_saved_from_loss: int
    count_cut_too_early: int
    approximation_only: bool = False
    approximation_reason: str = ""
    warning: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LabResult:
    summaries: list[PolicySummary] = field(default_factory=list)
    cost_assumption_bps: float = 18.0
    research_only: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return {
            "cost_assumption_bps": self.cost_assumption_bps,
            "research_only": self.research_only,
            "final_recommendation": self.final_recommendation,
            "summaries": [s.as_dict() for s in self.summaries],
        }


@dataclass
class _TradeInput:
    side: str
    entry_price: float
    stop_loss: float
    take_profit: float
    candles: pd.DataFrame
    max_holding_bars: int = 30
    funding_rate: Any = None
    entry_timestamp: Any = None


def _simulate_with_policy(trade: _TradeInput, policy: QuickProfitPolicy, *, slippage_bps: float = 3.0) -> OutcomeResult:
    """Run a single trade outcome under one policy."""
    qp = policy.quick_profit_threshold_pct
    be = policy.breakeven_after_pct
    # NOTE: trailing logic is approximated as breakeven for now; full trailing
    # belongs to a future RUNTIME implementation, this lab only flags candidates.
    if policy.trail_after_pct is not None and be is None:
        be = policy.trail_after_pct
    outcome = simulate_outcome_ohlcv(
        side=trade.side,
        entry_price=trade.entry_price,
        stop_loss=trade.stop_loss,
        take_profit=trade.take_profit,
        candles=trade.candles,
        max_holding_bars=trade.max_holding_bars,
        slippage_bps=slippage_bps,
        funding_rate=trade.funding_rate,
        entry_timestamp=trade.entry_timestamp,
        quick_profit_threshold_pct=qp,
        breakeven_after_pct=be,
    )
    # Euro-net override applies if BOTH notional and threshold provided.
    # NOTE: this is an APPROXIMATION — we evaluate the threshold against the
    # final realized outcome, not the first intra-bar moment when the net
    # PnL in EUR crosses the threshold. The summary will surface this.
    if policy.euro_net_threshold and policy.notional_usdt:
        euro_unrealized = outcome.net_return_pct / 100.0 * float(policy.notional_usdt)
        if outcome.exit_reason != EXIT_QUICK_PROFIT and euro_unrealized >= policy.euro_net_threshold:
            outcome.exit_reason = EXIT_QUICK_PROFIT
            outcome.notes.append(
                f"euro_net_threshold_{policy.euro_net_threshold:.2f}_approximation_only_final_outcome_basis"
            )
    return outcome


def evaluate_policy(
    trades: Iterable[_TradeInput],
    policy: QuickProfitPolicy,
    *,
    slippage_bps: float = 3.0,
    cost_assumption_bps: float = 18.0,
) -> PolicySummary:
    outcomes: list[OutcomeResult] = []
    baseline_outcomes: list[OutcomeResult] = [] if policy.name != BASELINE.name else []
    for trade in trades:
        outcomes.append(_simulate_with_policy(trade, policy, slippage_bps=slippage_bps))
        if policy.name != BASELINE.name:
            baseline_outcomes.append(_simulate_with_policy(trade, BASELINE, slippage_bps=slippage_bps))
    # Approximation flag composition: trailing_after_080 sets it directly;
    # any policy that uses euro_net_threshold also collapses to approximation.
    approx = bool(policy.is_approximation_only) or bool(policy.euro_net_threshold)
    approx_reason = policy.approximation_reason or (
        "euro_net_threshold_evaluated_on_final_outcome_not_first_intrabar_touch"
        if policy.euro_net_threshold
        else ""
    )
    warning = (
        "APPROXIMATION_ONLY: this policy is NOT a faithful intra-bar backtest "
        "of trailing/euro-net behaviour. Do not use these numbers for live decisions."
    ) if approx else ""

    if not outcomes:
        return PolicySummary(
            policy=policy.name,
            trades=0, gross_ev_pct=0.0, net_ev_pct=0.0, net_pf=0.0,
            win_rate=0.0, max_drawdown_pct=0.0, tp_pct=0.0, sl_pct=0.0,
            time_pct=0.0, quick_profit_pct=0.0, breakeven_count=0,
            avg_hold_bars=0.0, cost_drag_pct=0.0,
            count_gross_win_net_loss=0, count_saved_from_loss=0,
            count_cut_too_early=0,
            approximation_only=approx, approximation_reason=approx_reason, warning=warning,
        )

    n = len(outcomes)
    gross = [o.gross_return_pct for o in outcomes]
    net = [o.net_return_pct for o in outcomes]
    wins = [v for v in net if v > 0]
    losses = [v for v in net if v < 0]
    tp = sum(1 for o in outcomes if o.exit_reason == EXIT_TAKE_PROFIT)
    sl = sum(1 for o in outcomes if o.exit_reason == EXIT_STOP_LOSS)
    tm = sum(1 for o in outcomes if o.exit_reason == EXIT_HORIZON_CLOSE)
    qp = sum(1 for o in outcomes if o.exit_reason == EXIT_QUICK_PROFIT)
    be = sum(1 for o in outcomes if o.exit_reason == EXIT_BREAKEVEN or EXIT_TRAILING in o.notes)
    avg_hold = sum(o.bars_to_outcome for o in outcomes) / n
    cost_drag = sum(o.total_cost_bps for o in outcomes) / max(n, 1) / 100.0

    # equity curve / drawdown
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for v in net:
        equity += v
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    # diagnostic counters
    count_gross_win_net_loss = sum(1 for o in outcomes if o.gross_return_pct > 0 and o.net_return_pct <= 0)
    count_saved_from_loss = 0
    count_cut_too_early = 0
    if baseline_outcomes:
        for new, base in zip(outcomes, baseline_outcomes):
            if base.net_return_pct < 0 and new.net_return_pct >= 0:
                count_saved_from_loss += 1
            elif base.net_return_pct > new.net_return_pct + 0.05:
                # the policy capped a winner that would have gained more
                count_cut_too_early += 1

    return PolicySummary(
        policy=policy.name,
        trades=n,
        gross_ev_pct=sum(gross) / n,
        net_ev_pct=sum(net) / n,
        net_pf=(sum(wins) / abs(sum(losses))) if losses else (999.0 if wins else 0.0),
        win_rate=len(wins) / n,
        max_drawdown_pct=abs(max_dd),
        tp_pct=tp / n,
        sl_pct=sl / n,
        time_pct=tm / n,
        quick_profit_pct=qp / n,
        breakeven_count=be,
        avg_hold_bars=avg_hold,
        cost_drag_pct=cost_drag,
        count_gross_win_net_loss=count_gross_win_net_loss,
        count_saved_from_loss=count_saved_from_loss,
        count_cut_too_early=count_cut_too_early,
        approximation_only=approx,
        approximation_reason=approx_reason,
        warning=warning,
    )


def run_lab(
    trades: Iterable[_TradeInput],
    *,
    policies: tuple[QuickProfitPolicy, ...] = DEFAULT_POLICIES,
    cost_assumption_bps: float = 18.0,
    slippage_bps: float = 3.0,
) -> LabResult:
    trade_list = list(trades)
    summaries = [
        evaluate_policy(trade_list, policy, slippage_bps=slippage_bps, cost_assumption_bps=cost_assumption_bps)
        for policy in policies
    ]
    return LabResult(
        summaries=summaries,
        cost_assumption_bps=cost_assumption_bps,
        research_only=True,
        final_recommendation=FINAL_RECOMMENDATION,
    )


def build_trade_inputs_from_dataframe(
    candles: pd.DataFrame,
    *,
    side: str,
    entry_indices: Iterable[int],
    stop_pct: float,
    tp_pct: float,
    max_holding_bars: int = 30,
) -> list[_TradeInput]:
    """Helper for tests: builds trade inputs from a single OHLCV frame.

    entry_indices: list of integer indices into `candles` where we synthetically
    open a trade. Stop and TP are computed as pct of entry.
    """
    inputs: list[_TradeInput] = []
    for idx in entry_indices:
        if idx + 1 >= len(candles):
            continue
        entry_row = candles.iloc[idx + 1]
        entry_price = safe_float(entry_row.get("open"))
        if entry_price <= 0:
            continue
        if side.upper() == "LONG":
            stop = entry_price * (1.0 - stop_pct / 100.0)
            tp = entry_price * (1.0 + tp_pct / 100.0)
        else:
            stop = entry_price * (1.0 + stop_pct / 100.0)
            tp = entry_price * (1.0 - tp_pct / 100.0)
        post = candles.iloc[idx + 1 :].reset_index(drop=True)
        inputs.append(_TradeInput(
            side=side.upper(),
            entry_price=entry_price,
            stop_loss=stop,
            take_profit=tp,
            candles=post,
            max_holding_bars=max_holding_bars,
            entry_timestamp=entry_row.get("timestamp"),
        ))
    return inputs


def render_lab_text(result: LabResult) -> str:
    lines = ["QUICK PROFIT EXIT LAB START"]
    lines.append(f"cost_assumption_bps: {result.cost_assumption_bps}")
    has_approximations = any(s.approximation_only for s in result.summaries)
    lines.append(f"contains_approximation_only_policies: {str(has_approximations).lower()}")
    lines.append("policies:")
    for s in result.summaries:
        lines.append(
            f"- name={s.policy} trades={s.trades} gross_ev={s.gross_ev_pct:.4f}% "
            f"net_ev={s.net_ev_pct:.4f}% PF={s.net_pf:.3f} win={s.win_rate:.3f} "
            f"max_dd={s.max_drawdown_pct:.2f} TP={s.tp_pct:.1%} SL={s.sl_pct:.1%} "
            f"TIME={s.time_pct:.1%} QP={s.quick_profit_pct:.1%} "
            f"saved_from_loss={s.count_saved_from_loss} cut_too_early={s.count_cut_too_early} "
            f"approximation_only={str(s.approximation_only).lower()}"
            + (f" approximation_reason={s.approximation_reason}" if s.approximation_only else "")
        )
    if has_approximations:
        lines.append("warning: APPROXIMATION_ONLY policies are NOT faithful intra-bar backtests.")
        lines.append("warning: trailing simulated by collapsing to break-even after threshold; no bar-by-bar stop trail.")
        lines.append("warning: euro_net_threshold evaluated on final outcome, not first intra-bar touch of EUR threshold.")
        lines.append("warning: do_not_use_approximation_only_metrics_for_live_decisions: true")
    lines.append("research_only: true")
    lines.append("no_runtime_change: true")
    lines.append(f"final_recommendation: {result.final_recommendation}")
    lines.append("QUICK PROFIT EXIT LAB END")
    return "\n".join(lines)
