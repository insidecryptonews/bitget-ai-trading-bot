"""V10.38.1 P3 -- SHORT labels are a REAL side-aware triple-barrier replay, not
a costed inversion of the long outcome. Approximate short stays blocked."""

from __future__ import annotations

import random

from app.labs import continuous_edge_factory_v10_38 as CE

T0 = 1_700_000_000_000
BAR = 60_000


def _bar(ts, o, h, l, cl):
    return dict(ts=ts, open=o, high=h, low=l, close=cl, volume=1, buy_volume=1,
                sell_volume=0, n_trades=1, max_trade=1)


def test_short_triple_barrier_real_side_aware():
    # entry=100 -> short tp=99.6 (price falls), short sl=100.2 (price rises)
    # TP first: first future bar's low pierces 99.6
    tp_bars = [_bar(T0, 100, 100, 100, 100),
               _bar(T0 + BAR, 100, 100.1, 99.5, 99.6)]
    lab = CE.build_labels(tp_bars, side="short", tp_pct=0.004, sl_pct=0.002)[0]
    assert lab["side"] == "short" and lab["side_label_method"] == "real_side_aware"
    assert lab["triple_barrier"] == "TP" and lab["time_to_hit"] == 1
    assert lab["gross_return"] == 0.004 and lab["MFE"] >= 0.004
    # SL first: first future bar's high pierces 100.2
    sl_bars = [_bar(T0, 100, 100, 100, 100),
               _bar(T0 + BAR, 100, 100.3, 99.9, 100.2)]
    lab = CE.build_labels(sl_bars, side="short", tp_pct=0.004, sl_pct=0.002)[0]
    assert lab["triple_barrier"] == "SL" and lab["time_to_hit"] == 1
    assert lab["gross_return"] == -0.002
    # TP on the SECOND future bar -> time_to_hit == 2
    tp2 = [_bar(T0, 100, 100, 100, 100),
           _bar(T0 + BAR, 100, 100.1, 99.7, 99.8),      # neither barrier
           _bar(T0 + 2 * BAR, 99.8, 99.9, 99.5, 99.6)]  # low <= 99.6 -> TP
    lab = CE.build_labels(tp2, side="short", tp_pct=0.004, sl_pct=0.002)[0]
    assert lab["triple_barrier"] == "TP" and lab["time_to_hit"] == 2


def test_short_not_inverse_of_costed_long():
    # Path rises: LONG reaches its TP only on bar 3, but SHORT is stopped out on
    # bar 1. Independent barrier replay => different outcome AND different timing,
    # which a simple costed inversion of the long label could never produce.
    bars = [_bar(T0, 100, 100, 100, 100),
            _bar(T0 + BAR, 100, 100.25, 100.0, 100.2),
            _bar(T0 + 2 * BAR, 100.2, 100.3, 100.1, 100.25),
            _bar(T0 + 3 * BAR, 100.25, 100.5, 100.2, 100.45)]
    long_lab = CE.build_labels(bars, side="long", tp_pct=0.004, sl_pct=0.002)[0]
    short_lab = CE.build_labels(bars, side="short", tp_pct=0.004, sl_pct=0.002)[0]
    assert long_lab["triple_barrier"] == "TP" and long_lab["time_to_hit"] == 3
    assert short_lab["triple_barrier"] == "SL" and short_lab["time_to_hit"] == 1
    # real short outcome is NOT the naive costed inversion of the long outcome
    approx = CE._approx_short_invert([long_lab["cost_adjusted_outcome"]])[0]
    assert abs(short_lab["cost_adjusted_outcome"] - approx) > 1e-9
    # and the sign/magnitude are the short barrier's own (SL = -sl_pct - cost)
    assert short_lab["gross_return"] == -0.002


def test_build_labels_rejects_bad_side():
    import pytest
    with pytest.raises(ValueError):
        CE.build_labels([_bar(T0, 100, 100, 100, 100)], side="sideways")


def edge_bars(n, seed=1, every=10, planted=True):
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


def test_approx_short_labels_cannot_be_promising():
    # discovery WITHOUT bars/labels_short -> short falls back to the blocked
    # approximate path; it must be flagged and never PROMISING.
    bars = edge_bars(700, seed=5, planted=True)
    feats, labels = CE.build_features(bars), CE.build_labels(bars)
    cands = CE.discover_candidates(feats, labels)              # no bars passed
    shorts = [c for c in cands if c["side"] == "short"]
    assert shorts
    for c in shorts:
        assert c["approximate_short_labels"] is True
        assert c["side_label_method"] == "approx_inverse_long"
        assert "SHORT_APPROXIMATE_LABELS" in c["promotion_blockers_extra"]
        assert c["verdict"] != "PROMISING_RESEARCH_ONLY"


def test_real_short_labels_used_when_bars_supplied():
    bars = edge_bars(700, seed=5, planted=True)
    feats, labels = CE.build_features(bars), CE.build_labels(bars)
    cands = CE.discover_candidates(feats, labels, bars=bars)   # real short path
    shorts = [c for c in cands if c["side"] == "short"]
    assert shorts
    for c in shorts:
        assert c["approximate_short_labels"] is False
        assert c["side_label_method"] == "real_side_aware"
        assert "SHORT_APPROXIMATE_LABELS" not in c["promotion_blockers_extra"]
        assert c["not_actionable"] is True
