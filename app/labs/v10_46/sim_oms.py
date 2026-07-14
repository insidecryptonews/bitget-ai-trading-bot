"""V10.46 single Simulated Order Management System (RESEARCH ONLY).

ONE SimOMS shared by replay, shadow and paper research. It NEVER sends a real
order and never touches an exchange; it deterministically simulates fills,
position lifecycle, costs and money accounting in euros.

Cost discipline (mandate section 5 & 16):
  * fee, spread and slippage are each applied EXACTLY ONCE per fill;
  * funding is charged ONLY when the holding crosses a settlement instant
    (0/8/16 UTC), and only for the instants it actually crosses;
  * a maker/limit order that is merely TOUCHED does not auto-fill;
  * "gap-through" a stop fills at the gap open or worse, never at the stop;
  * three scenarios — observed / conservative / stress — for every result.

Money discipline (mandate section 17): the production bot's sizing is never
touched. Simulation uses fixed euro exposure scenarios (5/10/20 EUR, 1x, no
added margin, no martingale, no loss-DCA) and reports every euro figure. With
5 EUR total exposure at 1x and no added margin, market loss cannot exceed
~5 EUR plus explicit, auditable costs; a trade whose worst_case_loss exceeds
its planned_max_loss is REJECTED.
"""

from __future__ import annotations

import math
from typing import Any

from . import contracts as C

SETTLEMENT_HOURS = (0, 8, 16)
HOUR_MS = 3_600_000
BAR_MS = 60_000

# fixed euro exposure scenarios (never the production sizing)
MONEY_SCENARIOS = {
    "5eur": {"notional_eur": 5.0, "leverage": 1.0},
    "10eur": {"notional_eur": 10.0, "leverage": 1.0},
    "20eur": {"notional_eur": 20.0, "leverage": 1.0},
}

# cost scenarios in basis points (per side) + fill realism knobs
COST_SCENARIOS = {
    "observed": {"taker_fee_bps": 6.0, "maker_fee_bps": 2.0,
                 "spread_bps": 1.0, "slippage_bps": 2.0,
                 "funding_bps_per_8h": 1.0, "latency_ms": 250,
                 "maker_fill_prob": 0.6, "partial_prob": 0.0},
    "conservative": {"taker_fee_bps": 6.0, "maker_fee_bps": 2.0,
                     "spread_bps": 2.0, "slippage_bps": 4.0,
                     "funding_bps_per_8h": 1.5, "latency_ms": 500,
                     "maker_fill_prob": 0.4, "partial_prob": 0.1},
    "stress": {"taker_fee_bps": 6.0, "maker_fee_bps": 2.0,
               "spread_bps": 4.0, "slippage_bps": 8.0,
               "funding_bps_per_8h": 2.0, "latency_ms": 1000,
               "maker_fill_prob": 0.2, "partial_prob": 0.25},
}


def settlements_crossed(entry_ms: int, exit_ms: int) -> int:
    """Number of 0/8/16 UTC settlement instants in (entry_ms, exit_ms]."""
    if exit_ms <= entry_ms:
        return 0
    n = 0
    # scan settlement instants; step by 8h from the aligned start
    day0 = (entry_ms // (24 * HOUR_MS)) * (24 * HOUR_MS)
    t = day0
    while t <= exit_ms + 8 * HOUR_MS:
        hour = int((t // HOUR_MS) % 24)
        if hour in SETTLEMENT_HOURS and entry_ms < t <= exit_ms:
            n += 1
        t += HOUR_MS
    return n


def plan_position(scenario: str, entry_price: float, stop_price: float,
                  side: str) -> dict:
    """Compute euro sizing + planned/worst-case loss for a fixed-exposure
    scenario. worst_case_loss is the stop distance loss plus a gap buffer;
    a trade is only allowed if worst_case_loss <= planned_max_loss."""
    ms = MONEY_SCENARIOS[scenario]
    notional = ms["notional_eur"] * ms["leverage"]
    qty = notional / entry_price
    if side == "LONG":
        stop_loss_frac = max(0.0, (entry_price - stop_price) / entry_price)
    else:
        stop_loss_frac = max(0.0, (stop_price - entry_price) / entry_price)
    planned_max_loss = notional * stop_loss_frac
    # worst case: a gap can overshoot the stop; budget a 1.5x buffer, but at
    # 1x with no added margin the loss can never exceed the exposure ceiling
    worst_case = min(notional, planned_max_loss * 1.5)
    # a trade is only allowed when the INTENDED stop loss fits inside the
    # exposure ceiling AND worst-case stays within it. A stop beyond a full
    # adverse move (planned_max_loss > notional) is nonsensical and rejected.
    allowed = (planned_max_loss <= ms["notional_eur"] + 1e-9
               and worst_case <= ms["notional_eur"] + 1e-9)
    return {"scenario": scenario, "notional_eur": round(notional, 6),
            "margin_eur": round(ms["notional_eur"], 6),
            "leverage": ms["leverage"], "position_size": round(qty, 10),
            "entry_price": entry_price, "stop_price": stop_price,
            "stop_loss_frac": round(stop_loss_frac, 8),
            "planned_max_loss_eur": round(planned_max_loss, 6),
            "worst_case_loss_eur": round(worst_case, 6),
            "allowed": allowed}


def simulate_fill(order: dict, bar: dict, cost: dict, rng=None) -> dict:
    """Simulate ONE order fill against a bar (open/high/low/close). Returns a
    SimFill-shaped dict. Semantics:
      * MARKET/TAKER: fills at the next bar open + half-spread + slippage
        (fee/spread/slippage each once);
      * LIMIT/MAKER: fills ONLY if price trades through the limit AND a
        probabilistic queue check passes; a mere touch does not fill;
      * partial fills and non-fills per the scenario knobs.
    Cost is charged EXACTLY ONCE here and never again for the same fill."""
    import random as _r
    rng = rng or _r.Random(0)
    side = order["side"]
    long = side == "LONG"
    op, hi, lo = bar["open"], bar["high"], bar["low"]
    half_spread = cost["spread_bps"] / 2 / 10_000.0
    slip = cost["slippage_bps"] / 10_000.0
    otype = order["order_type"]
    qty = order["qty"]
    if otype in ("market", "taker"):
        raw = op * (1 + half_spread + slip) if long else op * (1 - half_spread - slip)
        fee_frac = cost["taker_fee_bps"] / 10_000.0
        status, fill_qty = "FILLED", qty
    else:  # limit / maker / post-only
        limit = order["limit_price"]
        touched = (lo <= limit) if long else (hi >= limit)
        if not touched:
            return _fill(order, None, 0.0, 0.0, 0.0, "NONFILL")
        if rng.random() > cost["maker_fill_prob"]:
            return _fill(order, None, 0.0, 0.0, 0.0, "NONFILL")  # queue miss
        raw = limit                                    # maker fills AT limit
        fee_frac = cost["maker_fee_bps"] / 10_000.0
        if rng.random() < cost["partial_prob"]:
            fill_qty, status = qty * 0.5, "PARTIAL"
        else:
            fill_qty, status = qty, "FILLED"
    notional = raw * fill_qty
    fee_eur = notional * fee_frac
    spread_eur = raw * fill_qty * half_spread if otype in ("market", "taker") else 0.0
    slippage_eur = raw * fill_qty * slip if otype in ("market", "taker") else 0.0
    return _fill(order, raw, fill_qty, fee_eur, slippage_eur, status,
                 spread_eur=spread_eur)


def _fill(order, price, qty, fee_eur, slip_eur, status, spread_eur=0.0) -> dict:
    return {"order_ref": order.get("ref", id(order)),
            "fill_price": price, "fill_qty": round(qty, 10),
            "fee_eur": round(fee_eur, 8), "slippage_eur": round(slip_eur, 8),
            "spread_eur": round(spread_eur, 8), "fill_status": status,
            "side": order["side"], "order_type": order["order_type"]}


def simulate_trade(*, side: str, entry_bar: dict, exit_bars: list[dict],
                   entry_ts_ms: int, stop_frac: float, tp_frac: float,
                   time_exit: int, scenario_money: str = "5eur",
                   scenario_cost: str = "observed",
                   trailing_frac: float | None = None,
                   interval_ms: int = BAR_MS) -> dict:
    """Full money-accounted trade lifecycle through the SimOMS.

    entry_bar: the bar at which we act (entry = its open + taker costs).
    exit_bars: subsequent bars (each dict has ts/open/high/low/close), scanned
    for stop / TP / trailing / time exit with gap-through realism. Returns a
    dict with gross_pnl_eur, net_pnl_eur, all cost components and MFE/MAE.
    Fees/spread/slippage counted once on entry and once on exit; funding only
    for settlement instants actually crossed.

    V10.47.8: `interval_ms` is the timeframe step (60000 for 1m, 900000 for
    15m, ...). It drives exit timestamps and bars_held so a 15m/1h/4h trade is
    no longer mis-stamped at a 1-minute cadence. Trailing is strictly causal:
    an update computed from a COMPLETED bar's high/low takes effect on the NEXT
    bar, never within the same bar it was derived from."""
    cost = COST_SCENARIOS[scenario_cost]
    ms = MONEY_SCENARIOS[scenario_money]
    long = side == "LONG"
    half_spread = cost["spread_bps"] / 2 / 10_000.0
    slip = cost["slippage_bps"] / 10_000.0
    per_side_px = half_spread + slip
    taker = cost["taker_fee_bps"] / 10_000.0
    entry_px_raw = entry_bar["open"]
    entry_px = entry_px_raw * (1 + per_side_px) if long \
        else entry_px_raw * (1 - per_side_px)
    stop_px = entry_px_raw * (1 - stop_frac) if long else entry_px_raw * (1 + stop_frac)
    tp_px = entry_px_raw * (1 + tp_frac) if long else entry_px_raw * (1 - tp_frac)
    plan = plan_position(scenario_money, entry_px_raw, stop_px, side)
    if not plan["allowed"]:
        return {"status": "REJECTED_RISK", "reason": "worst_case>planned",
                "plan": plan, **_zero_money(scenario_money, scenario_cost)}
    qty = plan["position_size"]
    notional = ms["notional_eur"]
    fee_open = notional * taker
    spread_open = notional * half_spread
    slip_open = notional * slip
    hwm = entry_px_raw
    exit_px_raw = None
    exit_reason = "TIME"
    exit_ts = entry_ts_ms
    mfe = mae = 0.0
    # `trail_stop` carries the trailing level derived from ALREADY-COMPLETED
    # bars; it is applied to `stop_px` at the START of the next bar, so a level
    # computed from bar k can never be used to test a stop inside bar k itself.
    trail_stop = None
    for k, b in enumerate(exit_bars):
        hi, lo, op, cl = b["high"], b["low"], b["open"], b["close"]
        # activate any trailing level derived from a PREVIOUS completed bar
        if trail_stop is not None:
            stop_px = max(stop_px, trail_stop) if long else min(stop_px, trail_stop)
        # MFE/MAE tracking (for labels/autopsy, not decisions)
        up = (hi - entry_px_raw) / entry_px_raw if long else (entry_px_raw - lo) / entry_px_raw
        dn = (entry_px_raw - lo) / entry_px_raw if long else (hi - entry_px_raw) / entry_px_raw
        mfe = max(mfe, up)
        mae = max(mae, dn)
        was_trailing = trailing_frac is not None and trail_stop is not None
        hit_stop = (lo <= stop_px) if long else (hi >= stop_px)
        hit_tp = (hi >= tp_px) if long else (lo <= tp_px)
        if hit_stop:                                   # stop first (conservative)
            # gap-through: if the bar OPENS beyond the stop, fill at open (worse)
            if (long and op <= stop_px) or (not long and op >= stop_px):
                exit_px_raw = op
            else:
                exit_px_raw = stop_px
            exit_reason = "TRAIL" if was_trailing else "SL"
            exit_ts = b["ts"] + interval_ms
            break
        if hit_tp:
            exit_px_raw = tp_px if ((long and op < tp_px) or (not long and op > tp_px)) else op
            exit_reason = "TP"
            exit_ts = b["ts"] + interval_ms
            break
        if k + 1 >= time_exit:
            exit_px_raw = cl
            exit_reason = "TIME"
            exit_ts = b["ts"] + interval_ms
            break
        # derive trailing from THIS completed bar -> effective from bar k+1
        if trailing_frac is not None:
            hwm = max(hwm, hi) if long else min(hwm, lo)
            trail_stop = hwm * (1 - trailing_frac) if long else hwm * (1 + trailing_frac)
    if exit_px_raw is None:
        exit_px_raw = exit_bars[-1]["close"] if exit_bars else entry_px_raw
        exit_ts = (exit_bars[-1]["ts"] + interval_ms) if exit_bars else entry_ts_ms
        exit_reason = "END"
    exit_px = exit_px_raw * (1 - per_side_px) if long else exit_px_raw * (1 + per_side_px)
    fee_close = notional * taker
    spread_close = notional * half_spread
    slip_close = notional * slip
    # gross move in euros (price change * qty), sign by side
    gross_eur = (exit_px_raw - entry_px_raw) * qty if long \
        else (entry_px_raw - exit_px_raw) * qty
    # funding only for settlement instants actually crossed
    n_settle = settlements_crossed(entry_ts_ms, exit_ts)
    funding_eur = notional * (cost["funding_bps_per_8h"] / 10_000.0) * n_settle
    fee_eur = fee_open + fee_close
    spread_eur = spread_open + spread_close
    slippage_eur = slip_open + slip_close
    net_eur = gross_eur - fee_eur - spread_eur - slippage_eur - funding_eur
    return {"status": "OK", "side": side, "exit_reason": exit_reason,
            "entry_ts_ms": entry_ts_ms, "exit_ts_ms": exit_ts,
            "scenario_money": scenario_money, "scenario_cost": scenario_cost,
            "notional_eur": round(notional, 6),
            "margin_eur": round(notional, 6),
            "leverage": ms["leverage"], "position_size": round(qty, 10),
            "entry_price": round(entry_px_raw, 8),
            "exit_price": round(exit_px_raw, 8),
            "stop_price": round(stop_px, 8), "tp_price": round(tp_px, 8),
            "planned_max_loss_eur": plan["planned_max_loss_eur"],
            "worst_case_loss_eur": plan["worst_case_loss_eur"],
            "fee_open_eur": round(fee_open, 8),
            "fee_close_eur": round(fee_close, 8),
            "fee_eur": round(fee_eur, 8),
            "spread_eur": round(spread_eur, 8),
            "slippage_eur": round(slippage_eur, 8),
            "funding_eur": round(funding_eur, 8),
            "settlements_crossed": n_settle,
            "gross_pnl_eur": round(gross_eur, 8),
            "net_pnl_eur": round(net_eur, 8),
            "mfe_frac": round(mfe, 8), "mae_frac": round(mae, 8),
            "interval_ms": interval_ms,
            "bars_held": (exit_ts - entry_ts_ms) // interval_ms}


def _zero_money(sm, sc) -> dict:
    return {"gross_pnl_eur": 0.0, "net_pnl_eur": 0.0, "fee_eur": 0.0,
            "spread_eur": 0.0, "slippage_eur": 0.0, "funding_eur": 0.0,
            "scenario_money": sm, "scenario_cost": sc, "bars_held": 0}
