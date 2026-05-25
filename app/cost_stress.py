"""Cost Stress — re-evaluate a list of trades under stricter cost assumptions.

Pure offline. Takes a list of trade gross returns and recomputes net metrics
at multiple cost levels:
  - base_cost  : default Bitget VIP0 taker round-trip (~0.18%)
  - 0.22%      : moderate adversarial slippage scenario
  - 0.25%      : aggressive adversarial slippage scenario
  - maker_maker: 4 bps + low slippage (best case, audit only)

For each scenario it returns net_ev / net_pf / win_rate. A `cost_stress_status`
summarises whether the setup survives:
  - PASS   : net_ev positive at base AND at 0.22%
  - WARN   : positive at base, negative at 0.22% (regime-sensitive)
  - FAIL   : negative at base (already not viable)
  - UNKNOWN: insufficient data
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any, Iterable

from .utils import safe_float


FINAL_RECOMMENDATION = "NO LIVE"

# Cost levels in PERCENT (not bps). Bitget VIP0 taker round-trip ~= 0.18%.
BASE_COST_PCT = 0.18
COST_022_PCT = 0.22
COST_025_PCT = 0.25
MAKER_MAKER_COST_PCT = 0.04

STATUS_PASS = "PASS"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"
STATUS_UNKNOWN = "UNKNOWN"


@dataclass
class ScenarioMetrics:
    name: str
    cost_pct: float
    trades: int
    net_ev: float
    net_pf: float
    win_rate: float
    gross_profit_sum: float
    gross_loss_sum: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CostStressReport:
    trades: int
    base_cost_pct: float
    scenarios: list[ScenarioMetrics] = field(default_factory=list)
    cost_stress_status: str = STATUS_UNKNOWN
    reasons: list[str] = field(default_factory=list)
    final_recommendation: str = FINAL_RECOMMENDATION
    research_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "trades": self.trades,
            "base_cost_pct": self.base_cost_pct,
            "scenarios": [s.as_dict() for s in self.scenarios],
            "cost_stress_status": self.cost_stress_status,
            "reasons": self.reasons,
            "final_recommendation": self.final_recommendation,
            "research_only": self.research_only,
        }


def _scenario_metrics(name: str, gross_returns: list[float], cost_pct: float) -> ScenarioMetrics:
    net = [g - cost_pct for g in gross_returns]
    wins = [v for v in net if v > 0]
    losses = [v for v in net if v < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    return ScenarioMetrics(
        name=name,
        cost_pct=cost_pct,
        trades=len(net),
        net_ev=sum(net) / max(len(net), 1),
        net_pf=pf,
        win_rate=len(wins) / max(len(net), 1),
        gross_profit_sum=gross_profit,
        gross_loss_sum=gross_loss,
    )


def evaluate_cost_stress(
    gross_returns_pct: Iterable[float],
    *,
    base_cost_pct: float = BASE_COST_PCT,
    extra_scenarios: dict[str, float] | None = None,
) -> CostStressReport:
    """Build the cost stress report from a list of gross returns (in percent).

    Caller passes GROSS returns per trade (in pct of price). We then derive
    net at each cost scenario.

    If `extra_scenarios` is provided it ADDS scenarios without removing the
    canonical ones (base / 0.22% / 0.25% / maker_maker).
    """
    grosses = [safe_float(v) for v in gross_returns_pct]
    if not grosses:
        return CostStressReport(
            trades=0,
            base_cost_pct=base_cost_pct,
            scenarios=[],
            cost_stress_status=STATUS_UNKNOWN,
            reasons=["no_trade_data_provided"],
        )

    scenarios = [
        _scenario_metrics("base_cost", grosses, base_cost_pct),
        _scenario_metrics("stress_0_22", grosses, COST_022_PCT),
        _scenario_metrics("stress_0_25", grosses, COST_025_PCT),
        _scenario_metrics("maker_maker_audit_only", grosses, MAKER_MAKER_COST_PCT),
    ]
    if extra_scenarios:
        for name, cost in extra_scenarios.items():
            scenarios.append(_scenario_metrics(name, grosses, float(cost)))

    base = scenarios[0]
    s_022 = scenarios[1]
    s_025 = scenarios[2]

    reasons: list[str] = []
    if base.net_ev <= 0:
        status = STATUS_FAIL
        reasons.append("base_cost_net_ev_not_positive")
    elif s_022.net_ev > 0 and s_025.net_ev > -0.05:
        status = STATUS_PASS
        reasons.append("survives_base_and_0_22_with_022_positive")
        if s_025.net_ev <= 0:
            reasons.append("note_0_25_below_zero_but_within_tolerance")
    elif s_022.net_ev > 0:
        status = STATUS_PASS
        reasons.append("survives_base_and_0_22_warning_at_0_25")
        if s_025.net_ev <= -0.05:
            reasons.append("note_0_25_drops_substantially")
    elif s_022.net_ev > -0.05:
        status = STATUS_WARN
        reasons.append("base_positive_but_0_22_marginal_negative_check_cost_assumption")
    else:
        status = STATUS_FAIL
        reasons.append("base_positive_but_collapses_at_0_22_cost_stress")

    # Note: maker_maker is informational only — never used to validate edge.
    reasons.append("maker_maker_scenario_is_audit_only_never_used_for_promotion")

    return CostStressReport(
        trades=len(grosses),
        base_cost_pct=base_cost_pct,
        scenarios=scenarios,
        cost_stress_status=status,
        reasons=reasons,
    )


def render_cost_stress_text(report: CostStressReport) -> str:
    lines = ["COST STRESS REPORT START"]
    lines.append(f"trades: {report.trades}")
    lines.append(f"base_cost_pct: {report.base_cost_pct}")
    lines.append(f"cost_stress_status: {report.cost_stress_status}")
    lines.append("scenarios:")
    for s in report.scenarios:
        lines.append(
            f"- {s.name}: cost={s.cost_pct:.4f}% trades={s.trades} "
            f"net_ev={s.net_ev:.6f} net_pf={s.net_pf:.4f} win={s.win_rate:.3f}"
        )
    lines.append("reasons:")
    for r in report.reasons:
        lines.append(f"- {r}")
    lines.append("research_only: true")
    lines.append(f"final_recommendation: {report.final_recommendation}")
    lines.append("COST STRESS REPORT END")
    return "\n".join(lines)
