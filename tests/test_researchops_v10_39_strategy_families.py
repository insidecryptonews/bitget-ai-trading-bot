"""V10.39 strategy family benchmark: one honest protocol, complexity penalty,
never actionable."""

from __future__ import annotations

import random

from app.labs import alpha_improvement_sprint_v10_39 as A

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


def test_benchmark_returns_one_row_per_family_all_blocked():
    rows = A.strategy_family_benchmark(edge_bars(1500, seed=3))
    fams = {r["family"] for r in rows}
    assert fams == set(A.STRATEGY_FAMILIES)
    for r in rows:
        assert r["verdict"] in A.FAMILY_VERDICTS
        assert r["can_send_real_orders"] is False
        assert r["final_recommendation"] == "NO LIVE"
    for banned in ("BUY_NOW", "SELL_NOW", "OPEN_POSITION", "LIVE_SIGNAL",
                   "LIVE_READY", "CAN_SEND_REAL_ORDERS=True"):
        assert banned not in str(rows)


def test_planted_momentum_edge_is_found():
    rows = A.strategy_family_benchmark(edge_bars(1500, seed=3))
    promising = [r for r in rows if r["verdict"] == "PROMISING_RESEARCH_ONLY"]
    assert promising
    assert any(r["family"] == "micro_momentum" for r in promising)
    for r in promising:
        # promising must clear min-edge PLUS the complexity penalty
        assert r["net_EV_lower_bound"] > A.MIN_EDGE + (r["complexity_penalty"] or 0)
        assert (r["baseline_delta"] or 0) > 0


def test_pure_noise_yields_no_promising_family():
    rows = A.strategy_family_benchmark(edge_bars(1500, seed=11, planted=False))
    assert not [r for r in rows if r["verdict"] == "PROMISING_RESEARCH_ONLY"]


def test_complexity_penalty_is_positive_and_recorded():
    rows = A.strategy_family_benchmark(edge_bars(1500, seed=3))
    scored = [r for r in rows if r.get("complexity_penalty") is not None]
    assert scored
    assert all(r["complexity_penalty"] > 0 for r in scored)


def test_verdict_helper_promotes_and_rejects_correctly():
    strong = {"sample_size": 200, "net_EV_lower_bound": 0.01, "net_EV": 0.012,
              "gross_EV": 0.014, "train_net_EV": 0.013, "baseline_delta": 0.005}
    assert A._verdict_for(dict(strong), 64)[0] == "PROMISING_RESEARCH_ONLY"
    overfit = dict(strong, net_EV=0.0002, net_EV_lower_bound=0.0001,
                   train_net_EV=0.01)
    assert A._verdict_for(overfit, 64)[0] == "REJECTED_OVERFIT_RISK"
    unstable = dict(strong, baseline_delta=-0.001)
    assert A._verdict_for(unstable, 64)[0] == "REJECTED_UNSTABLE"
