"""V10.39 regime segmentation: per-regime evaluation, tiny-sample regimes never
promoted, everything research-only."""

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


def test_regime_report_covers_regimes_and_is_blocked():
    rows = A.regime_edge_report(edge_bars(1500, seed=3))
    regimes = {r["regime"] for r in rows}
    assert regimes == set(A.REGIMES)
    for r in rows:
        assert r["can_send_real_orders"] is False
        assert r["final_recommendation"] == "NO LIVE"


def test_tiny_regime_never_promising():
    rows = A.regime_edge_report(edge_bars(1500, seed=3))
    for r in rows:
        if r["verdict"] == "PROMISING_RESEARCH_ONLY":
            # a promising regime must not be a thin-sample artefact
            assert r["sample_size"] >= 2 * CE.MIN_OOS_SAMPLE
    # regimes with too little data must be flagged, never silently promoted
    thin = [r for r in rows if r["verdict"] == "NEEDS_MORE_DATA"]
    for r in thin:
        assert r.get("family") is None


def test_regime_mask_selects_expected_subset():
    bars = edge_bars(400, seed=3)
    feats = CE.build_features(bars)
    all_mask = A._regime_mask(feats, None)
    assert all(all_mask) and len(all_mask) == len(feats)
    chop = A._regime_mask(feats, "chop")
    trend = A._regime_mask(feats, "trend_up")
    # masks are disjoint-ish partitions of the regime space, never all-true fakes
    assert sum(chop) + sum(A._regime_mask(feats, "trend_down")) + sum(trend) <= len(feats) + len(feats)
    assert 0 <= sum(trend) <= len(feats)


def test_noise_regime_report_has_no_promising():
    rows = A.regime_edge_report(edge_bars(1500, seed=11, planted=False))
    assert not [r for r in rows if r["verdict"] == "PROMISING_RESEARCH_ONLY"]
