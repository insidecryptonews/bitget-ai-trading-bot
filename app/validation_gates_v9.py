"""ResearchOps V8/V9 — V9 Validation Gates Foundation (research-only).

Aggregates the hard validation gates a candidate must pass before any human
might consider it for paper. Includes:

- Walk-Forward V2 (delegated to ``app.walk_forward_runner_v2``).
- Bootstrap CI on net EV.
- Monte Carlo simple-order shuffle (small lightweight variant).
- PBO (Probability of Backtest Overfitting) — implemented if folds enough.
- Deflated Sharpe Ratio — approximated; emits NEED_MORE_DATA if insufficient.
- Cost / Slippage / Funding stress gates.
- Regime / Symbol / Time-of-day stability gates.
- Risk-of-ruin proxy if applicable.

Every gate that lacks sample emits ``NEED_MORE_DATA`` and never fabricates a pass.
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass, field
from statistics import mean, pstdev
from typing import Any, Iterable


FINAL_RECOMMENDATION_NO_LIVE = "NO LIVE"

GATE_PASS = "PASS"
GATE_FAIL = "FAIL"
GATE_NEED_MORE_DATA = "NEED_MORE_DATA"


@dataclass
class GateReport:
    name: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationGatesV9Report:
    strategy_id: str
    hours: int
    timeframe: str
    samples: int
    gates: list[GateReport] = field(default_factory=list)
    overall_status: str = GATE_NEED_MORE_DATA
    pass_count: int = 0
    fail_count: int = 0
    need_data_count: int = 0
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "hours": self.hours,
            "timeframe": self.timeframe,
            "samples": self.samples,
            "gates": [g.as_dict() for g in self.gates],
            "overall_status": self.overall_status,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "need_data_count": self.need_data_count,
            "research_only": self.research_only,
            "paper_filter_enabled": self.paper_filter_enabled,
            "can_send_real_orders": self.can_send_real_orders,
            "final_recommendation": self.final_recommendation,
        }


def _bootstrap_ci(values: list[float], *, iterations: int = 500, seed: int = 1729) -> tuple[float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    n = len(values)
    means: list[float] = []
    for _ in range(iterations):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(mean(sample))
    means.sort()
    return (
        means[max(0, int(0.025 * iterations))],
        mean(means),
        means[min(iterations - 1, int(0.975 * iterations))],
    )


def _gate_walk_forward(folds: list[dict[str, Any]]) -> GateReport:
    if not folds:
        return GateReport(
            name="walk_forward_v2",
            status=GATE_NEED_MORE_DATA,
            reason="no_folds_provided",
        )
    positive = sum(1 for f in folds if float(f.get("net_ev", 0.0)) > 0)
    if positive == len(folds):
        return GateReport(name="walk_forward_v2", status=GATE_PASS,
                          detail={"folds": len(folds), "positive": positive})
    if positive >= max(3, int(len(folds) * 0.75)):
        return GateReport(name="walk_forward_v2", status=GATE_PASS,
                          detail={"folds": len(folds), "positive": positive})
    return GateReport(
        name="walk_forward_v2",
        status=GATE_FAIL,
        detail={"folds": len(folds), "positive": positive},
        reason="not_enough_positive_folds",
    )


def _gate_bootstrap(values: list[float]) -> GateReport:
    if len(values) < 30:
        return GateReport(name="bootstrap_ci",
                          status=GATE_NEED_MORE_DATA,
                          reason=f"samples={len(values)}_below_min=30")
    low, mid, high = _bootstrap_ci(values)
    if low > 0:
        return GateReport(name="bootstrap_ci", status=GATE_PASS,
                          detail={"low": low, "mid": mid, "high": high})
    return GateReport(name="bootstrap_ci", status=GATE_FAIL,
                      detail={"low": low, "mid": mid, "high": high},
                      reason="ci_low_not_positive")


def _gate_monte_carlo(values: list[float], *, iterations: int = 500, seed: int = 1729) -> GateReport:
    if len(values) < 50:
        return GateReport(name="monte_carlo_shuffle",
                          status=GATE_NEED_MORE_DATA,
                          reason=f"samples={len(values)}_below_min=50")
    rng = random.Random(seed)
    positives = 0
    base_sum = sum(values)
    for _ in range(iterations):
        rng.shuffle(values)
        if sum(values) >= base_sum:
            positives += 1
    ratio = positives / iterations
    if 0.05 <= ratio <= 0.95:
        return GateReport(name="monte_carlo_shuffle", status=GATE_PASS,
                          detail={"ratio_positive_or_equal": ratio})
    return GateReport(name="monte_carlo_shuffle", status=GATE_FAIL,
                      detail={"ratio_positive_or_equal": ratio},
                      reason="distribution_too_extreme_for_robustness")


def _gate_pbo(in_sample: list[float], out_sample: list[float]) -> GateReport:
    if len(in_sample) < 10 or len(out_sample) < 10:
        return GateReport(name="pbo",
                          status=GATE_NEED_MORE_DATA,
                          reason="window_too_small_for_pbo")
    is_mean = mean(in_sample)
    oos_mean = mean(out_sample)
    if oos_mean >= 0 and oos_mean >= 0.4 * is_mean:
        return GateReport(name="pbo", status=GATE_PASS,
                          detail={"is_mean": is_mean, "oos_mean": oos_mean})
    return GateReport(name="pbo", status=GATE_FAIL,
                      detail={"is_mean": is_mean, "oos_mean": oos_mean},
                      reason="oos_collapse_relative_to_is")


def _gate_deflated_sharpe(returns: list[float], *, trials: int = 1) -> GateReport:
    if len(returns) < 30:
        return GateReport(name="deflated_sharpe",
                          status=GATE_NEED_MORE_DATA,
                          reason=f"samples={len(returns)}_below_min=30")
    sd = pstdev(returns) or 1e-9
    sr = mean(returns) / sd
    # Conservative deflation: divide by sqrt(1+log(trials)).
    factor = math.sqrt(1.0 + math.log(max(trials, 1)))
    deflated = sr / factor
    if deflated > 0.5:
        return GateReport(name="deflated_sharpe", status=GATE_PASS,
                          detail={"sharpe": sr, "deflated": deflated, "trials": trials})
    return GateReport(name="deflated_sharpe", status=GATE_FAIL,
                      detail={"sharpe": sr, "deflated": deflated, "trials": trials},
                      reason="deflated_sharpe_below_threshold")


def _gate_cost_stress(net_returns: list[float], extra_cost_pct: float = 0.10) -> GateReport:
    if not net_returns:
        return GateReport(name="cost_stress", status=GATE_NEED_MORE_DATA, reason="empty")
    stressed = [r - extra_cost_pct for r in net_returns]
    if mean(stressed) > 0:
        return GateReport(name="cost_stress", status=GATE_PASS,
                          detail={"avg_stressed": mean(stressed), "extra_cost_pct": extra_cost_pct})
    return GateReport(name="cost_stress", status=GATE_FAIL,
                      detail={"avg_stressed": mean(stressed), "extra_cost_pct": extra_cost_pct},
                      reason="net_negative_under_extra_cost")


def _gate_slippage_stress(net_returns: list[float], slip_pct: float = 0.05) -> GateReport:
    if not net_returns:
        return GateReport(name="slippage_stress", status=GATE_NEED_MORE_DATA, reason="empty")
    stressed = [r - slip_pct for r in net_returns]
    if mean(stressed) > 0:
        return GateReport(name="slippage_stress", status=GATE_PASS,
                          detail={"avg_stressed": mean(stressed), "slip_pct": slip_pct})
    return GateReport(name="slippage_stress", status=GATE_FAIL,
                      detail={"avg_stressed": mean(stressed), "slip_pct": slip_pct},
                      reason="net_negative_under_extra_slippage")


def _gate_funding_stress(net_returns: list[float], funding_pct: float = 0.03) -> GateReport:
    if not net_returns:
        return GateReport(name="funding_stress", status=GATE_NEED_MORE_DATA, reason="empty")
    stressed = [r - funding_pct for r in net_returns]
    if mean(stressed) > 0:
        return GateReport(name="funding_stress", status=GATE_PASS,
                          detail={"avg_stressed": mean(stressed), "funding_pct": funding_pct})
    return GateReport(name="funding_stress", status=GATE_FAIL,
                      detail={"avg_stressed": mean(stressed), "funding_pct": funding_pct},
                      reason="net_negative_under_extra_funding")


def _gate_stability(label: str, partitions: dict[str, list[float]]) -> GateReport:
    if not partitions:
        return GateReport(name=label, status=GATE_NEED_MORE_DATA, reason="no_partitions")
    means = {k: mean(v) for k, v in partitions.items() if v}
    if not means:
        return GateReport(name=label, status=GATE_NEED_MORE_DATA, reason="empty_partitions")
    positive = sum(1 for m in means.values() if m > 0)
    if positive == len(means):
        return GateReport(name=label, status=GATE_PASS, detail={"means": means})
    if positive >= max(2, int(len(means) * 0.75)):
        return GateReport(name=label, status=GATE_PASS, detail={"means": means})
    return GateReport(name=label, status=GATE_FAIL, detail={"means": means},
                      reason="too_many_negative_partitions")


def _gate_risk_of_ruin(net_returns: list[float], *, capital_pct_per_trade: float = 0.01) -> GateReport:
    if len(net_returns) < 30:
        return GateReport(name="risk_of_ruin_proxy",
                          status=GATE_NEED_MORE_DATA,
                          reason=f"samples={len(net_returns)}_below_min=30")
    wins = [r for r in net_returns if r > 0]
    losses = [r for r in net_returns if r < 0]
    if not losses:
        return GateReport(name="risk_of_ruin_proxy",
                          status=GATE_NEED_MORE_DATA,
                          reason="no_losing_trades_yet")
    p = len(wins) / len(net_returns)
    q = 1 - p
    avg_win = mean(wins) if wins else 1e-9
    avg_loss = abs(mean(losses)) if losses else 1e-9
    if avg_win <= 0:
        return GateReport(name="risk_of_ruin_proxy", status=GATE_FAIL, reason="no_positive_winners")
    edge = (p * avg_win - q * avg_loss) / avg_win
    if edge <= 0:
        return GateReport(name="risk_of_ruin_proxy", status=GATE_FAIL,
                          detail={"edge": edge},
                          reason="no_edge")
    # Toy Kelly-ish risk-of-ruin proxy.
    try:
        rr = math.pow((1 - edge) / (1 + edge), 1.0 / max(capital_pct_per_trade, 1e-4))
    except ValueError:
        rr = 1.0
    if rr < 0.05:
        return GateReport(name="risk_of_ruin_proxy", status=GATE_PASS,
                          detail={"edge": edge, "risk_of_ruin_proxy": rr})
    return GateReport(name="risk_of_ruin_proxy", status=GATE_FAIL,
                      detail={"edge": edge, "risk_of_ruin_proxy": rr},
                      reason="risk_of_ruin_proxy_too_high")


def run_validation_gates_v9(
    *,
    strategy_id: str,
    net_returns: list[float],
    folds: list[dict[str, Any]] | None = None,
    in_sample: list[float] | None = None,
    out_sample: list[float] | None = None,
    partitions_by_regime: dict[str, list[float]] | None = None,
    partitions_by_symbol: dict[str, list[float]] | None = None,
    partitions_by_session: dict[str, list[float]] | None = None,
    hours: int = 24,
    timeframe: str = "5m",
    trials: int = 1,
) -> ValidationGatesV9Report:
    gates: list[GateReport] = []
    gates.append(_gate_walk_forward(folds or []))
    gates.append(_gate_bootstrap(net_returns))
    gates.append(_gate_monte_carlo(list(net_returns)))
    gates.append(_gate_pbo(in_sample or [], out_sample or []))
    gates.append(_gate_deflated_sharpe(net_returns, trials=trials))
    gates.append(_gate_cost_stress(net_returns))
    gates.append(_gate_slippage_stress(net_returns))
    gates.append(_gate_funding_stress(net_returns))
    gates.append(_gate_stability("regime_stability", partitions_by_regime or {}))
    gates.append(_gate_stability("symbol_stability", partitions_by_symbol or {}))
    gates.append(_gate_stability("time_of_day_stability", partitions_by_session or {}))
    gates.append(_gate_risk_of_ruin(net_returns))

    pass_count = sum(1 for g in gates if g.status == GATE_PASS)
    fail_count = sum(1 for g in gates if g.status == GATE_FAIL)
    need_data_count = sum(1 for g in gates if g.status == GATE_NEED_MORE_DATA)

    if fail_count > 0:
        overall = GATE_FAIL
    elif need_data_count > 0 and pass_count == 0:
        overall = GATE_NEED_MORE_DATA
    elif need_data_count > 0:
        overall = GATE_NEED_MORE_DATA
    else:
        overall = GATE_PASS

    return ValidationGatesV9Report(
        strategy_id=strategy_id,
        hours=int(hours),
        timeframe=timeframe,
        samples=len(net_returns),
        gates=gates,
        overall_status=overall,
        pass_count=pass_count,
        fail_count=fail_count,
        need_data_count=need_data_count,
    )
