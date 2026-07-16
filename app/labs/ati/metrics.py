"""ATI performance metrics, bootstrap uncertainty, and chronological OOS gates."""

from __future__ import annotations

import math
from statistics import median
from typing import Any, Iterable

import numpy as np


def _finite_values(values: Iterable[Any]) -> list[float]:
    result: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            result.append(number)
    return result


def bootstrap_mean_ci(values: Iterable[Any], *, seed: int = 7,
                      samples: int = 500) -> tuple[float | None, float | None]:
    clean = np.asarray(_finite_values(values), dtype=float)
    if len(clean) < 2:
        return None, None
    rng = np.random.default_rng(seed)
    means = np.empty(samples, dtype=float)
    for idx in range(samples):
        means[idx] = float(rng.choice(clean, size=len(clean), replace=True).mean())
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def summarize_trades(rows: list[dict[str, Any]], *, seed: int = 7) -> dict[str, Any]:
    returns = _finite_values(row.get("net_return") for row in rows)
    gross_returns = _finite_values(row.get("gross_return") for row in rows)
    if not returns:
        return {
            "trades": 0, "net_ev": None, "gross_ev": None, "profit_factor": None,
            "win_rate": None, "max_drawdown": None, "average_mfe": None,
            "average_mae": None, "median_holding_bars": None,
            "top_3_profit_concentration": None, "ci95_lower": None,
            "ci95_upper": None, "fees": 0.0, "slippage": 0.0,
            "funding": 0.0, "result_status": "INSUFFICIENT_DATA",
        }
    gains = sum(value for value in returns if value > 0)
    losses = -sum(value for value in returns if value <= 0)
    profit_factor = gains / losses if losses > 0 else None
    equity = peak = drawdown = 0.0
    for value in returns:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    positive = sorted((value for value in returns if value > 0), reverse=True)
    concentration = sum(positive[:3]) / gains if gains > 0 else None
    lower, upper = bootstrap_mean_ci(returns, seed=seed)
    result_status = "INSUFFICIENT_DATA" if len(returns) < 40 else "REJECTED"
    if (
        len(returns) >= 40
        and sum(returns) / len(returns) > 0
        and profit_factor is not None and profit_factor >= 1.15
        and lower is not None and lower > 0
        and concentration is not None and concentration <= 0.40
    ):
        result_status = "PROMISING_SHADOW_ONLY"
    return {
        "trades": len(returns),
        "net_ev": sum(returns) / len(returns),
        "gross_ev": (sum(gross_returns) / len(gross_returns) if gross_returns else None),
        "profit_factor": profit_factor,
        "win_rate": sum(value > 0 for value in returns) / len(returns),
        "max_drawdown": drawdown,
        "average_mfe": sum(_finite_values(row.get("mfe") for row in rows)) / len(rows),
        "average_mae": sum(_finite_values(row.get("mae") for row in rows)) / len(rows),
        "median_holding_bars": median(_finite_values(row.get("held_bars") for row in rows)),
        "top_3_profit_concentration": concentration,
        "ci95_lower": lower,
        "ci95_upper": upper,
        "fees": sum(_finite_values(row.get("fee_fraction") for row in rows)),
        "slippage": sum(_finite_values(row.get("slippage_fraction") for row in rows)),
        "funding": sum(_finite_values(row.get("funding_fraction") for row in rows)),
        "result_status": result_status,
    }


def group_metrics(rows: list[dict[str, Any]], keys: tuple[str, ...], *,
                  seed: int = 7) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        key = tuple(str(row.get(field) or "UNKNOWN") for field in keys)
        groups.setdefault(key, []).append(row)
    output: list[dict[str, Any]] = []
    for ordinal, (key, group) in enumerate(sorted(groups.items())):
        output.append({
            **{field: value for field, value in zip(keys, key)},
            **summarize_trades(group, seed=seed + ordinal),
        })
    return output


def chronological_validation(rows: list[dict[str, Any]], *, seed: int = 7) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (str(row.get("decision_ts")), str(row.get("signal_id"))))
    total = len(ordered)
    first = int(total * 0.60)
    second = int(total * 0.80)
    splits = {
        "train": ordered[:first],
        "validation": ordered[first:second],
        "test": ordered[second:],
    }
    metrics = {name: summarize_trades(group, seed=seed + idx) for idx, (name, group) in enumerate(splits.items())}
    blockers: list[str] = []
    if total < 40:
        blockers.append("minimum_40_trades_not_met")
    for name in ("validation", "test"):
        split = metrics[name]
        if split["trades"] < 8:
            blockers.append(f"{name}_sample_too_small")
        if split["net_ev"] is None or split["net_ev"] <= 0:
            blockers.append(f"{name}_net_ev_not_positive")
        if split["profit_factor"] is None or split["profit_factor"] < 1.15:
            blockers.append(f"{name}_pf_below_1_15")
    overall = summarize_trades(ordered, seed=seed)
    if overall["top_3_profit_concentration"] is None or overall["top_3_profit_concentration"] > 0.40:
        blockers.append("top_3_profit_concentration_above_40pct")
    status = "PASS_SHADOW_RESEARCH_ONLY" if not blockers else (
        "NEED_MORE_DATA" if total < 40 else "FAIL"
    )
    return {
        "method": "chronological_60_20_20_frozen_rules",
        "parameter_selection_uses_test": False,
        "status": status,
        "blockers": sorted(set(blockers)),
        "splits": metrics,
        "paper_ready": False,
        "live_ready": False,
        "final_recommendation": "NO LIVE",
    }
