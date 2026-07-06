"""V10.39 cost-aware / multi-timeframe horizon scan. Costs are never lowered;
the scan only reports whether any timeframe/horizon clears the floor."""

from __future__ import annotations

import random

from app.labs import alpha_improvement_sprint_v10_39 as A
from app.labs import continuous_edge_factory_v10_38 as CE

T0 = 1_700_000_000_000
BAR = 60_000


def edge_bars(n, seed=1, every=15, planted=True):
    rng = random.Random(seed)
    price, pump, bars = 100.0, 0, []
    for i in range(n):
        sig = planted and i % every == 0 and i > 0
        ntr = 500 if sig else int(rng.uniform(15, 35) + rng.uniform(15, 35))
        if sig:
            pump = 3
        drift = 0.006 if pump > 0 else rng.uniform(-0.0012, 0.0012)
        pump = max(0, pump - 1)
        new = price * (1 + drift)
        bars.append({"ts": T0 + i * BAR, "open": price,
                     "high": max(price, new) * 1.0005,
                     "low": min(price, new) * 0.9995, "close": new,
                     "volume": ntr, "buy_volume": ntr * (0.9 if sig else 0.5),
                     "sell_volume": ntr * (0.1 if sig else 0.5),
                     "n_trades": ntr, "max_trade": ntr / 5})
        price = new
    return bars


def test_round_trip_cost_is_the_v1038_floor():
    assert round(A._round_trip_cost(), 8) == 0.0018      # 2*(5.5+3)+1 bps


def test_scan_covers_all_timeframes_and_is_not_actionable():
    scan = A.cost_aware_horizon_scan(edge_bars(1500, seed=3))
    tfs = {r["timeframe_min"] for r in scan["rows"]}
    assert tfs == set(A.TIMEFRAMES)
    for r in scan["rows"]:
        assert r.get("final_recommendation", "NO LIVE") == "NO LIVE"
    assert scan["can_send_real_orders"] is False
    assert scan["n_combinations"] > 0


def test_scan_finds_planted_edge_but_only_research_only():
    scan = A.cost_aware_horizon_scan(edge_bars(1500, seed=3))
    assert scan["any_promising"] is True
    best = scan["best_cell"]
    assert best["verdict"] == "PROMISING_RESEARCH_ONLY"
    assert best["net_EV_lower_bound"] > A.MIN_EDGE
    for banned in ("BUY_NOW", "SELL_NOW", "OPEN_POSITION", "LIVE_SIGNAL"):
        assert banned not in str(scan)


def test_pure_noise_scan_has_no_promising_cell():
    scan = A.cost_aware_horizon_scan(edge_bars(1500, seed=9, planted=False))
    assert scan["any_promising"] is False


def test_verdict_distinguishes_cost_floor_from_no_signal():
    # gross positive but eaten by costs -> REJECTED_COSTS_TOO_HIGH
    m = {"sample_size": 100, "net_EV_lower_bound": -0.001, "net_EV": -0.0005,
         "gross_EV": 0.0012, "train_net_EV": -0.0005, "baseline_delta": 0.0}
    v, _ = A._verdict_for(dict(m), 64)
    assert v == "REJECTED_COSTS_TOO_HIGH"
    # gross also negative -> plain negative EV
    m2 = dict(m, gross_EV=-0.0003)
    assert A._verdict_for(m2, 64)[0] == "REJECTED_NEGATIVE_EV"
    # tiny sample -> needs more data
    assert A._verdict_for({"sample_size": 3}, 64)[0] == "NEEDS_MORE_DATA"


def test_higher_timeframe_reduces_bar_count():
    bars = edge_bars(600)
    assert len(A.resample_bars(bars, 5)) == len(bars) // 5
    assert len(A.resample_bars(bars, 1)) == len(bars)
