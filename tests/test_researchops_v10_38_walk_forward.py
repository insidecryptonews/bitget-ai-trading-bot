"""V10.38 Walk-forward / anti-overfit: OOS windows, baselines, honest verdicts."""

from __future__ import annotations

import random

from app.labs import continuous_edge_factory_v10_38 as CE

T0 = 1_700_000_000_000
BAR = 60_000


def edge_bars(n, seed=1, every=5, planted=True):
    """Compact synthetic bars: every `every`-th bar carries a crisp trade-count
    burst followed by a multi-bar rise (edge on burst_score) when planted."""
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


def test_needs_more_data_when_too_few_bars():
    bars = edge_bars(60, planted=False)
    f, l = CE.build_features(bars), CE.build_labels(bars)
    rep = CE.walk_forward(f, l, "burst_score", "long", 1.0)
    assert rep["verdict"] == "NEEDS_MORE_DATA"
    assert rep["can_send_real_orders"] is False


def test_planted_edge_passes_oos_and_beats_random():
    # bursts spaced WELL beyond the label horizon so a random entry rarely
    # catches a pump -> entering ON the burst is a genuine TIMING edge, not just
    # "be long in an up market". Short horizon (time_bars=5) keeps it honest.
    bars = edge_bars(3000, seed=3, every=15)
    f = CE.build_features(bars)
    l = CE.build_labels(bars, time_bars=5)
    rep = CE.walk_forward(f, l, "burst_score", "long", 1.0)
    assert rep["net_EV_OOS"] is not None and rep["net_EV_OOS"] > 0
    assert rep["stability_score"] >= 0.75
    assert rep["verdict"] == "OOS_PASS_RESEARCH_ONLY"
    # every window compares against a random baseline drawn from the same pool
    assert rep["windows"] and all("random_net_EV" in w for w in rep["windows"])


def test_pure_noise_does_not_pass_oos():
    bars = edge_bars(900, seed=9, planted=False)
    f, l = CE.build_features(bars), CE.build_labels(bars)
    rep = CE.walk_forward(f, l, "burst_score", "long", 1.0)
    assert rep["verdict"] != "OOS_PASS_RESEARCH_ONLY"


def test_walk_forward_is_never_actionable():
    bars = edge_bars(900, seed=3, every=5)
    f, l = CE.build_features(bars), CE.build_labels(bars)
    rep = CE.walk_forward(f, l, "burst_score", "long", 1.0)
    assert rep["research_only"] is True and rep["shadow_only"] is True
    assert rep["edge_validated"] is False
    assert rep["final_recommendation"] == "NO LIVE"
    for banned in ("BUY_NOW", "SELL_NOW", "OPEN_POSITION", "LIVE_SIGNAL"):
        assert banned not in str(rep)
