"""ResearchOps V10 — Micro-TP / high-take-profit-rate viability lab.

Determines whether a micro-TP / high-hit-rate scalping family is viable
after costs, or whether it must be discarded. The strategic prior
(Perplexity) is that micro-TP is NOT a core family and very likely dies
to costs; this lab exists to *demonstrate* that, not to promote it.

HARD CONTRACT — research only:

- never opens orders, never calls private endpoints, never writes DB,
- the ``maker_maker`` cost scenario is AUDIT-ONLY and can NEVER drive a
  promotable verdict,
- a micro-TP whose target is <= round-trip cost is mechanically
  impossible => ``REJECT_COSTS_TOO_HIGH``,
- small observed sample => ``NEED_MORE_DATA`` (never a fake green),
- the best a viable micro-TP can earn here is ``NOT_CORE`` — never
  paper/live, never a candidate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE

DECISION_REJECT_COSTS = "REJECT_COSTS_TOO_HIGH"
DECISION_WATCH_ONLY = "WATCH_ONLY"
DECISION_NEED_DATA = "NEED_MORE_DATA"
DECISION_NOT_CORE = "NOT_CORE"
DECISION_AUDIT_ONLY = "AUDIT_ONLY_NOT_PROMOTABLE"

DEFAULT_TP = [0.10, 0.15, 0.20, 0.25, 0.35, 0.50, 0.75, 1.00]
DEFAULT_SL = [0.15, 0.25, 0.35, 0.50, 0.75, 1.00]
DEFAULT_HOLD = [3, 5, 8, 10, 15, 20, 30]

# Round-trip cost (%) per scenario. ``maker_maker`` is audit-only.
COST_SCENARIOS: dict[str, float] = {
    "taker_taker": 0.12,
    "maker_taker": 0.08,
    "maker_maker": 0.04,   # AUDIT ONLY — never promotable
    "stress_018": 0.18,
    "stress_022": 0.22,
    "stress_025": 0.25,
}
AUDIT_ONLY_SCENARIOS = frozenset({"maker_maker"})

# A min-required winrate above this is not realistically achievable.
MAX_PLAUSIBLE_WINRATE = 0.75
MIN_TRADES = 40


@dataclass
class MicroTpCombo:
    tp_pct: float = 0.0
    sl_pct: float = 0.0
    scenario: str = ""
    cost_pct: float = 0.0
    audit_only: bool = False
    tp_le_cost: bool = False
    minimum_required_winrate: float | None = None
    feasible: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MicroTpViabilityReport:
    hours: int = 168
    generated_at: str = ""
    tp_grid: list[float] = field(default_factory=list)
    sl_grid: list[float] = field(default_factory=list)
    combos_evaluated: int = 0
    realistic_feasible_combos: int = 0
    maker_maker_feasible_combos: int = 0
    best_realistic_min_winrate: float | None = None
    best_realistic_combo: dict[str, Any] = field(default_factory=dict)
    observed_trades: int = 0
    observed_winrate: float | None = None
    net_ev_pct: float | None = None
    net_pf: float | None = None
    tp_rate: float | None = None
    sl_rate: float | None = None
    time_rate: float | None = None
    gross_green_net_negative: bool = False
    viable_after_costs: bool = False
    maker_only_required: bool = False
    need_websocket: bool = False
    blockers: list[str] = field(default_factory=list)
    decision: str = DECISION_REJECT_COSTS
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _min_required_winrate(tp: float, sl: float, cost: float) -> float | None:
    """Break-even winrate for a TP/SL trade paying round-trip ``cost``.

    Net per win = tp - cost ; net per loss = -(sl + cost). Break-even p
    solves p*(tp-cost) = (1-p)*(sl+cost). If tp <= cost, no winrate can
    make it positive (returns >= 1.0 / None for impossible)."""
    if tp <= cost:
        return None  # impossible: even 100% wins lose money
    denom = (tp - cost) + (sl + cost)
    if denom <= 0:
        return None
    p = (sl + cost) / denom
    return round(p, 4)


def analyze_micro_tp_viability(
    *,
    hours: int = 168,
    tp_grid: Iterable[float] | None = None,
    sl_grid: Iterable[float] | None = None,
    cost_scenarios: dict[str, float] | None = None,
    observed_winrate: float | None = None,
    observed_trades: int = 0,
    observed_tp_rate: float | None = None,
    observed_sl_rate: float | None = None,
    observed_time_rate: float | None = None,
) -> MicroTpViabilityReport:
    tps = list(tp_grid or DEFAULT_TP)
    sls = list(sl_grid or DEFAULT_SL)
    scenarios = dict(cost_scenarios or COST_SCENARIOS)
    report = MicroTpViabilityReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
        tp_grid=tps,
        sl_grid=sls,
        observed_trades=int(observed_trades or 0),
        observed_winrate=observed_winrate,
        tp_rate=observed_tp_rate,
        sl_rate=observed_sl_rate,
        time_rate=observed_time_rate,
    )

    combos: list[MicroTpCombo] = []
    realistic_feasible: list[MicroTpCombo] = []
    maker_maker_feasible: list[MicroTpCombo] = []
    for tp in tps:
        for sl in sls:
            for name, cost in scenarios.items():
                audit_only = name in AUDIT_ONLY_SCENARIOS
                mrw = _min_required_winrate(tp, sl, cost)
                feasible = mrw is not None and mrw <= MAX_PLAUSIBLE_WINRATE
                combo = MicroTpCombo(
                    tp_pct=tp, sl_pct=sl, scenario=name, cost_pct=cost,
                    audit_only=audit_only, tp_le_cost=(tp <= cost),
                    minimum_required_winrate=mrw, feasible=feasible,
                )
                combos.append(combo)
                if feasible:
                    if audit_only:
                        maker_maker_feasible.append(combo)
                    else:
                        realistic_feasible.append(combo)
    report.combos_evaluated = len(combos)
    report.realistic_feasible_combos = len(realistic_feasible)
    report.maker_maker_feasible_combos = len(maker_maker_feasible)

    if realistic_feasible:
        best = min(realistic_feasible, key=lambda c: c.minimum_required_winrate)
        report.best_realistic_min_winrate = best.minimum_required_winrate
        report.best_realistic_combo = best.as_dict()

    # Observed economics (if a winrate was supplied).
    if observed_winrate is not None and report.best_realistic_combo:
        bc = report.best_realistic_combo
        tp, sl, cost = bc["tp_pct"], bc["sl_pct"], bc["cost_pct"]
        p = float(observed_winrate)
        net_win = tp - cost
        net_loss = -(sl + cost)
        report.net_ev_pct = round(p * net_win + (1 - p) * net_loss, 4)
        gross_ev = round(p * tp - (1 - p) * sl, 4)
        report.gross_green_net_negative = bool(gross_ev > 0 and (report.net_ev_pct or 0) <= 0)
        gw = p * net_win
        gl = abs((1 - p) * net_loss)
        report.net_pf = round(gw / gl, 4) if gl > 0 else (999.0 if gw > 0 else 0.0)

    # Flags.
    report.maker_only_required = bool(not realistic_feasible and maker_maker_feasible)
    report.need_websocket = report.maker_only_required  # maker-only needs WS quoting
    report.viable_after_costs = bool(realistic_feasible) and (
        report.net_ev_pct is None or report.net_ev_pct > 0
    )

    # Decision precedence.
    blockers: list[str] = []
    if not realistic_feasible and not maker_maker_feasible:
        blockers.append("tp_below_or_equal_roundtrip_cost")
        report.decision = DECISION_REJECT_COSTS
    elif not realistic_feasible and maker_maker_feasible:
        blockers.append("only_viable_under_maker_maker")
        report.decision = DECISION_AUDIT_ONLY
    elif observed_winrate is not None and report.observed_trades < MIN_TRADES:
        blockers.append("insufficient_observed_sample")
        report.decision = DECISION_NEED_DATA
    elif report.net_ev_pct is not None and report.net_ev_pct <= 0:
        blockers.append("observed_net_ev_non_positive")
        report.decision = DECISION_REJECT_COSTS
    else:
        # Marginally feasible after costs, but Perplexity prior: not core.
        report.decision = DECISION_NOT_CORE
    report.blockers = blockers
    return report


def run_micro_tp_viability(*, hours: int = 168, **kwargs: Any) -> MicroTpViabilityReport:
    return analyze_micro_tp_viability(hours=hours, **kwargs)
