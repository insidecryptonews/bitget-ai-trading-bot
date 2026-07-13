"""V10.46.3 SimOMS + euro money scenarios: market/limit/maker fills, maker
touch != fill, partial/non-fill, gap-through stop, TP, trailing, funding only
on settlement crossings, fees/spread/slippage exactly once, and the 5-EUR 1x
worst-case-loss invariant. Research only, NO LIVE."""

from __future__ import annotations

import random

import pytest

from app.labs.v10_46 import sim_oms as S

T0 = 1_700_000_400_000    # aligned; hour derived from epoch
BAR = 60_000


def _bar(ts, o, h, l, c):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c,
            "volume": 10.0}


# ==========================================================================
# FILL MODEL
# ==========================================================================

def test_market_fill_charges_each_cost_once():
    order = {"order_type": "market", "side": "LONG", "qty": 0.05,
             "limit_price": None, "ref": "o1"}
    f = S.simulate_fill(order, _bar(T0, 100.0, 100.5, 99.5, 100.2),
                        S.COST_SCENARIOS["observed"])
    assert f["fill_status"] == "FILLED"
    assert f["fee_eur"] > 0 and f["spread_eur"] > 0 and f["slippage_eur"] > 0
    # fill price is open + half-spread + slippage (LONG pays up), once
    op = 100.0
    exp = op * (1 + (1.0 / 2 + 2.0) / 10_000.0)
    assert abs(f["fill_price"] - exp) < 1e-9


def test_maker_touch_does_not_auto_fill():
    # limit BUY at 99.0; bar never trades down to 99.0 -> NONFILL
    order = {"order_type": "maker", "side": "LONG", "qty": 0.05,
             "limit_price": 99.0, "ref": "o2"}
    f = S.simulate_fill(order, _bar(T0, 100.0, 100.5, 99.5, 100.2),
                        S.COST_SCENARIOS["observed"])
    assert f["fill_status"] == "NONFILL" and f["fill_price"] is None


def test_maker_touch_with_queue_can_fill_or_miss():
    order = {"order_type": "maker", "side": "LONG", "qty": 0.05,
             "limit_price": 99.4, "ref": "o3"}
    bar = _bar(T0, 100.0, 100.5, 99.0, 100.2)      # trades through 99.4
    fills = [S.simulate_fill(order, bar, S.COST_SCENARIOS["observed"],
                             rng=random.Random(i))["fill_status"]
             for i in range(40)]
    assert "FILLED" in fills and "NONFILL" in fills   # queue is probabilistic
    # a maker fill happens AT the limit, and pays maker fee, no taker slippage
    ff = next(S.simulate_fill(order, bar, S.COST_SCENARIOS["observed"],
                              rng=random.Random(i))
              for i in range(40)
              if S.simulate_fill(order, bar, S.COST_SCENARIOS["observed"],
                                 rng=random.Random(i))["fill_status"] != "NONFILL")
    assert ff["slippage_eur"] == 0.0 and ff["spread_eur"] == 0.0


# ==========================================================================
# MONEY SCENARIOS + INVARIANT
# ==========================================================================

def test_five_eur_worst_case_loss_bounded():
    plan = S.plan_position("5eur", entry_price=100.0, stop_price=99.0,
                           side="LONG")
    assert plan["notional_eur"] == 5.0 and plan["leverage"] == 1.0
    assert plan["worst_case_loss_eur"] <= 5.0 + 1e-9   # cannot exceed exposure
    assert plan["allowed"] is True


def test_trade_rejected_when_worst_case_exceeds_planned():
    # a huge stop distance makes planned_max_loss exceed the exposure ceiling
    r = S.simulate_trade(side="LONG", entry_bar=_bar(T0, 100.0, 100.0, 100.0, 100.0),
                         exit_bars=[_bar(T0 + BAR, 100.0, 100.0, 100.0, 100.0)],
                         entry_ts_ms=T0, stop_frac=2.0, tp_frac=0.01,
                         time_exit=1, scenario_money="5eur")
    assert r["status"] == "REJECTED_RISK"


# ==========================================================================
# LIFECYCLE: stop, TP, gap-through, trailing, funding, fees once
# ==========================================================================

def test_gap_through_stop_fills_at_open_not_stop():
    entry = _bar(T0, 100.0, 100.1, 99.9, 100.0)
    # next bar gaps down through the stop (~99): opens at 95
    gap = _bar(T0 + BAR, 95.0, 95.1, 94.5, 94.8)
    r = S.simulate_trade(side="LONG", entry_bar=entry, exit_bars=[gap],
                         entry_ts_ms=T0, stop_frac=0.01, tp_frac=0.05,
                         time_exit=5, scenario_money="5eur")
    assert r["exit_reason"] == "SL"
    assert r["exit_price"] == 95.0                     # gap open, worse than stop
    assert r["net_pnl_eur"] < 0


def test_tp_hit_and_fees_counted_once_each_side():
    entry = _bar(T0, 100.0, 100.1, 99.9, 100.0)
    up = _bar(T0 + BAR, 100.2, 101.0, 100.1, 100.9)    # hits +0.6% TP
    r = S.simulate_trade(side="LONG", entry_bar=entry, exit_bars=[up],
                         entry_ts_ms=T0, stop_frac=0.02, tp_frac=0.006,
                         time_exit=5, scenario_money="10eur")
    assert r["exit_reason"] == "TP"
    # fee is open+close, each once; gross - all costs = net
    assert abs(r["fee_eur"] - (r["fee_open_eur"] + r["fee_close_eur"])) < 1e-12
    recon = round(r["gross_pnl_eur"] - r["fee_eur"] - r["spread_eur"]
                  - r["slippage_eur"] - r["funding_eur"], 8)
    assert abs(recon - r["net_pnl_eur"]) < 1e-6


def test_funding_only_when_crossing_settlement():
    # a short trade fully inside one 8h window crosses 0 settlements
    inside = S.settlements_crossed(T0, T0 + 30 * BAR)
    # a long-held trade across >8h crosses at least one settlement
    across = S.settlements_crossed(T0, T0 + 9 * 3_600_000)
    assert inside == 0 and across >= 1
    r_short = S.simulate_trade(side="LONG", entry_bar=_bar(T0, 100, 100.1, 99.9, 100),
                               exit_bars=[_bar(T0 + BAR, 100, 100.05, 99.95, 100.0)],
                               entry_ts_ms=T0, stop_frac=0.02, tp_frac=0.02,
                               time_exit=1, scenario_money="5eur")
    assert r_short["settlements_crossed"] == 0 and r_short["funding_eur"] == 0.0


def test_trailing_exit_reason_and_no_double_slippage():
    entry = _bar(T0, 100.0, 100.1, 99.9, 100.0)
    rally = _bar(T0 + BAR, 100.1, 101.5, 100.0, 101.4)
    pull = _bar(T0 + 2 * BAR, 101.4, 101.4, 100.8, 100.9)   # trails out
    r = S.simulate_trade(side="LONG", entry_bar=entry, exit_bars=[rally, pull],
                         entry_ts_ms=T0, stop_frac=0.02, tp_frac=0.1,
                         time_exit=10, trailing_frac=0.004, scenario_money="5eur")
    assert r["exit_reason"] == "TRAIL"
    # slippage charged once per side only (2 * notional * slip)
    exp_slip = 2 * 5.0 * (2.0 / 10_000.0)
    assert abs(r["slippage_eur"] - exp_slip) < 1e-9


def test_three_cost_scenarios_monotone_net():
    entry = _bar(T0, 100.0, 100.1, 99.9, 100.0)
    up = _bar(T0 + BAR, 100.2, 101.0, 100.1, 100.9)
    nets = []
    for sc in ("observed", "conservative", "stress"):
        r = S.simulate_trade(side="LONG", entry_bar=entry, exit_bars=[up],
                             entry_ts_ms=T0, stop_frac=0.02, tp_frac=0.006,
                             time_exit=5, scenario_cost=sc, scenario_money="5eur")
        nets.append(r["net_pnl_eur"])
    # harsher cost scenarios never give a BETTER net than observed
    assert nets[0] >= nets[1] >= nets[2]
