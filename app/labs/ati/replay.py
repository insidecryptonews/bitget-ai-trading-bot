"""Bar-by-bar ATI replay with conservative fills and explicit costs."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

HORIZON_BARS = {"15m": 1, "30m": 2, "1h": 4, "2h": 8, "4h": 16}


@dataclass(frozen=True)
class AtiCostModel:
    fee_bps_per_side: float = 6.0
    slippage_bps_per_side: float = 3.0
    spread_bps_round_trip: float = 2.0
    funding_bps_per_8h: float = 1.0

    @property
    def entry_exit_cost_fraction(self) -> float:
        return (
            2 * self.fee_bps_per_side
            + 2 * self.slippage_bps_per_side
            + self.spread_bps_round_trip
        ) / 10_000.0


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _side_return(side: str, entry: float, exit_price: float) -> float:
    return ((exit_price - entry) / entry) if side == "LONG" else ((entry - exit_price) / entry)


def _funding_fraction(model: AtiCostModel, held_bars: int) -> float:
    return model.funding_bps_per_8h / 10_000.0 * (held_bars * 0.25 / 8.0)


def _levels(candidate: dict[str, Any], entry: float) -> tuple[float, float, float]:
    side = str(candidate["direction"]).upper()
    atr = _finite(candidate.get("atr15")) or entry * 0.005
    invalidation = _finite(candidate.get("invalidation_level"))
    if side == "LONG":
        stop = invalidation if invalidation is not None and invalidation < entry else entry - atr
        risk = entry - stop
        target = entry + 1.5 * risk
    else:
        stop = invalidation if invalidation is not None and invalidation > entry else entry + atr
        risk = stop - entry
        target = entry - 1.5 * risk
    risk_fraction = risk / entry
    if not math.isfinite(risk_fraction) or risk_fraction < 0.0002 or risk_fraction > 0.05:
        raise ValueError("ATI_INVALID_STRUCTURAL_RISK")
    return stop, target, risk_fraction


def _horizon_returns(frame: pd.DataFrame, *, entry_idx: int, entry: float,
                     side: str, costs: AtiCostModel) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    for name, bars in HORIZON_BARS.items():
        idx = entry_idx + bars - 1
        if idx >= len(frame):
            values[f"gross_return_{name}"] = None
            values[f"net_return_{name}"] = None
            continue
        close = _finite(frame.iloc[idx]["close"])
        if close is None:
            values[f"gross_return_{name}"] = None
            values[f"net_return_{name}"] = None
            continue
        gross = _side_return(side, entry, close)
        net = gross - costs.entry_exit_cost_fraction - _funding_fraction(costs, bars)
        values[f"gross_return_{name}"] = gross
        values[f"net_return_{name}"] = net
    return values


def simulate_trade(
    frame: pd.DataFrame,
    candidate: dict[str, Any],
    *,
    costs: AtiCostModel | None = None,
    max_holding_bars: int = 16,
    trailing_activation: float | None = None,
    trailing_distance: float | None = None,
    trailing_atr_multiple: float | None = None,
    policy_name: str = "baseline_structural_1_5R",
) -> dict[str, Any] | None:
    """Simulate one candidate; decisions are at i and entry is open i+1.

    A trailing level is based only on favorable excursion from prior closed
    bars. Activation and a trailing hit on the same bar cannot be credited.
    """
    costs = costs or AtiCostModel()
    signal_idx = int(candidate["signal_idx"])
    entry_idx = signal_idx + 1
    if candidate.get("decision") != "SHADOW_CANDIDATE" or entry_idx >= len(frame):
        return None
    side = str(candidate["direction"]).upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("ATI_SIDE_INVALID")
    entry = _finite(frame.iloc[entry_idx]["open"])
    if entry is None or entry <= 0:
        raise ValueError("ATI_ENTRY_NON_FINITE")
    stop, target, risk_fraction = _levels(candidate, entry)
    planned_last_idx = entry_idx + max(1, max_holding_bars) - 1
    full_horizon_available = planned_last_idx < len(frame)
    last_idx = min(len(frame) - 1, planned_last_idx)
    peak = entry
    trough = entry
    trailing_stop: float | None = None
    exit_idx = last_idx
    exit_price = _finite(frame.iloc[last_idx]["close"]) or entry
    exit_reason = "TIME" if full_horizon_available else "INCOMPLETE"
    ambiguity_rule = "NONE"

    for idx in range(entry_idx, last_idx + 1):
        row = frame.iloc[idx]
        open_price, high, low, close = map(_finite, (row["open"], row["high"], row["low"], row["close"]))
        if None in (open_price, high, low, close):
            raise ValueError("ATI_REPLAY_NON_FINITE_BAR")
        assert open_price is not None and high is not None and low is not None and close is not None
        if high < max(open_price, close) or low > min(open_price, close) or low > high:
            raise ValueError("ATI_REPLAY_INVALID_BAR")

        # Existing stops are checked before this bar can update a trailing stop.
        effective_stop = stop
        if trailing_stop is not None:
            effective_stop = max(stop, trailing_stop) if side == "LONG" else min(stop, trailing_stop)
        if side == "LONG":
            if open_price <= effective_stop:
                exit_idx, exit_price, exit_reason = idx, open_price, "GAP_STOP"
                break
            if open_price >= target:
                exit_idx, exit_price, exit_reason = idx, target, "TP"
                break
            hit_stop, hit_tp = low <= effective_stop, high >= target
        else:
            if open_price >= effective_stop:
                exit_idx, exit_price, exit_reason = idx, open_price, "GAP_STOP"
                break
            if open_price <= target:
                exit_idx, exit_price, exit_reason = idx, target, "TP"
                break
            hit_stop, hit_tp = high >= effective_stop, low <= target
        if hit_stop and hit_tp:
            exit_idx, exit_price = idx, effective_stop
            exit_reason, ambiguity_rule = "STOP_BEFORE_TP", "STOP_BEFORE_TP"
            break
        if hit_stop:
            exit_idx, exit_price = idx, effective_stop
            exit_reason = "TRAIL" if trailing_stop is not None and effective_stop == trailing_stop else "SL"
            break
        if hit_tp:
            exit_idx, exit_price, exit_reason = idx, target, "TP"
            break

        # Update excursions after all exits, for use from the next bar onward.
        peak = max(peak, high)
        trough = min(trough, low)
        favorable = ((peak - entry) / entry) if side == "LONG" else ((entry - trough) / entry)
        activation = trailing_activation
        if trailing_atr_multiple is not None:
            activation = max(costs.entry_exit_cost_fraction, risk_fraction * 0.5)
            distance = (_finite(candidate.get("atr15")) or entry * 0.005) * trailing_atr_multiple
            candidate_trail = peak - distance if side == "LONG" else trough + distance
        else:
            distance_fraction = trailing_distance or 0.0
            candidate_trail = peak * (1 - distance_fraction) if side == "LONG" else trough * (1 + distance_fraction)
        if activation is not None and favorable >= activation:
            if side == "LONG":
                trailing_stop = candidate_trail if trailing_stop is None else max(trailing_stop, candidate_trail)
            else:
                trailing_stop = candidate_trail if trailing_stop is None else min(trailing_stop, candidate_trail)

    # ``peak``/``trough`` contain only bars fully observed before an exit. On
    # the exit bar, credit only the known fill direction; the opposite extreme
    # may have happened after the trade was already closed.
    if exit_reason == "TP":
        if side == "LONG":
            peak = max(peak, exit_price)
        else:
            trough = min(trough, exit_price)
    elif exit_reason in {"SL", "TRAIL", "GAP_STOP", "STOP_BEFORE_TP"}:
        if side == "LONG":
            trough = min(trough, exit_price)
        else:
            peak = max(peak, exit_price)
    mfe = ((peak - entry) / entry) if side == "LONG" else ((entry - trough) / entry)
    mae = ((entry - trough) / entry) if side == "LONG" else ((peak - entry) / entry)
    held_bars = exit_idx - entry_idx + 1
    gross = _side_return(side, entry, exit_price)
    funding = _funding_fraction(costs, held_bars)
    net = gross - costs.entry_exit_cost_fraction - funding
    return {
        "signal_id": candidate["signal_id"],
        "setup_id": candidate["setup_id"],
        "setup_variant": candidate.get("setup_variant", ""),
        "symbol": candidate["symbol"],
        "side": side,
        "regime": candidate.get("regime", "UNKNOWN"),
        "policy": policy_name,
        "signal_idx": signal_idx,
        "entry_idx": entry_idx,
        "exit_idx": exit_idx,
        "decision_ts": candidate["decision_ts"],
        "entry_ts": pd.Timestamp(frame.iloc[entry_idx]["timestamp"]).isoformat(),
        "exit_ts": pd.Timestamp(frame.iloc[exit_idx]["available_at"]).isoformat(),
        "entry_price": entry,
        "exit_price": exit_price,
        "stop_price": stop,
        "target_price": target,
        "risk_fraction": risk_fraction,
        "exit_reason": exit_reason,
        "outcome_complete": exit_reason != "INCOMPLETE",
        "ambiguity_rule": ambiguity_rule,
        "held_bars": held_bars,
        "gross_return": gross,
        "net_return": net,
        "mfe": mfe,
        "mae": mae,
        "fee_fraction": 2 * costs.fee_bps_per_side / 10_000.0,
        "slippage_fraction": 2 * costs.slippage_bps_per_side / 10_000.0,
        "spread_fraction": costs.spread_bps_round_trip / 10_000.0,
        "funding_fraction": funding,
        "cost_model": asdict(costs),
        "fills_are_simulated": True,
        **_horizon_returns(frame, entry_idx=entry_idx, entry=entry, side=side, costs=costs),
    }


def replay_candidates(frame: pd.DataFrame, candidates: list[dict[str, Any]], *,
                      costs: AtiCostModel | None = None,
                      include_trailing_grid: bool = True) -> list[dict[str, Any]]:
    costs = costs or AtiCostModel()
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        baseline = simulate_trade(frame, candidate, costs=costs)
        if baseline is None:
            continue
        rows.append(baseline)
        if not include_trailing_grid:
            continue
        for activation in (0.0015, 0.0025, 0.0040):
            for distance in (0.0010, 0.0020, 0.0030):
                result = simulate_trade(
                    frame, candidate, costs=costs,
                    trailing_activation=activation,
                    trailing_distance=distance,
                    policy_name=f"trail_a{activation:.4f}_d{distance:.4f}",
                )
                if result is not None:
                    rows.append(result)
        atr_result = simulate_trade(
            frame, candidate, costs=costs, trailing_atr_multiple=1.0,
            policy_name="trail_atr_1_0_after_costs",
        )
        if atr_result is not None:
            rows.append(atr_result)
    return rows
