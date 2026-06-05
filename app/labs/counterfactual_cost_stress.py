"""V8.2.5 — Counterfactual Cost Stress (research-only).

Recomputes net EV across stressed cost levels (0.18%, 0.20%, 0.25%, 0.30%,
0.35%) using the gross PnL from the V8.2.4 dataset, separating by side,
symbol, regime, strategy and training_label.

Hard contract: read-only research. Cost levels are inputs to ``net_ev =
gross_pnl − cost_pct``; no production cost configuration is changed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import (
    FINAL_RECOMMENDATION_NO_LIVE,
    STATUS_NEED_DATA,
    STATUS_OK,
)
from .counterfactual_dedup_audit import _is_evaluable, dedup_rows
from .counterfactual_training_dataset import build_dataset


COST_STRESS_LEVELS_PCT: tuple[float, ...] = (0.18, 0.20, 0.25, 0.30, 0.35)
GROUPING_DIMENSIONS: tuple[str, ...] = (
    "side", "symbol", "regime", "strategy", "training_label",
)
MIN_GROUP_SIZE = 10


@dataclass
class CostStressReport:
    hours: int
    generated_at: str
    samples: int = 0
    cost_levels_pct: list[float] = field(default_factory=list)
    by_cost_level: list[dict[str, Any]] = field(default_factory=list)
    surviving_groups: list[dict[str, Any]] = field(default_factory=list)
    optimistic_only_groups: list[dict[str, Any]] = field(default_factory=list)
    dedup_used: bool = True
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _stress_one(gross: Any, cost_pct: float) -> float | None:
    if gross is None:
        return None
    try:
        return float(gross) - float(cost_pct)
    except Exception:
        return None


def _agg_at_cost(rows: list[dict[str, Any]], cost_pct: float) -> dict[str, Any]:
    nets: list[float] = []
    for r in rows:
        net = _stress_one(r.get("baseline_gross_pnl"), cost_pct)
        if net is None:
            continue
        nets.append(net)
    if not nets:
        return {"count": 0, "net_ev_avg_pct": 0.0, "survives": False, "cost_pct": cost_pct}
    avg = sum(nets) / len(nets)
    return {
        "count": len(nets),
        "net_ev_avg_pct": avg,
        "survives": avg > 0,
        "cost_pct": cost_pct,
    }


def stress_costs(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    dedup: bool = True,
    cost_levels: Iterable[float] | None = None,
    rows: Iterable[dict[str, Any]] | None = None,
) -> CostStressReport:
    levels = sorted(set(float(c) for c in (cost_levels or COST_STRESS_LEVELS_PCT)))
    report = CostStressReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
        dedup_used=dedup,
        cost_levels_pct=levels,
    )
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    evaluable = [r for r in dataset if _is_evaluable(r)]
    if dedup:
        evaluable = dedup_rows(evaluable)
    if not evaluable:
        report.status = STATUS_NEED_DATA
        return report
    report.samples = len(evaluable)
    for cost in levels:
        report.by_cost_level.append(_agg_at_cost(evaluable, cost))

    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for r in evaluable:
        key = tuple(str(r.get(d, "UNKNOWN")).upper() for d in GROUPING_DIMENSIONS)
        groups.setdefault(key, []).append(r)
    surviving: list[dict[str, Any]] = []
    optimistic_only: list[dict[str, Any]] = []
    highest = max(levels)
    lowest = min(levels)
    for key, rs in groups.items():
        if len(rs) < MIN_GROUP_SIZE:
            continue
        per_level = {f"cost_{c:.2f}": _agg_at_cost(rs, c) for c in levels}
        survives_high = per_level[f"cost_{highest:.2f}"]["survives"]
        survives_low = per_level[f"cost_{lowest:.2f}"]["survives"]
        entry = {
            **{d: v for d, v in zip(GROUPING_DIMENSIONS, key)},
            "count": len(rs),
            **per_level,
        }
        if survives_high:
            surviving.append(entry)
        elif survives_low and not survives_high:
            optimistic_only.append(entry)
    surviving.sort(
        key=lambda g: g[f"cost_{highest:.2f}"]["net_ev_avg_pct"],
        reverse=True,
    )
    optimistic_only.sort(key=lambda g: g["count"], reverse=True)
    report.surviving_groups = surviving[:50]
    report.optimistic_only_groups = optimistic_only[:50]
    report.status = STATUS_OK
    return report
