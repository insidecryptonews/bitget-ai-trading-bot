"""V10.38 Continuous Edge Factory -- alpha discovery / features / labels /
net-EV tests. Pure synthetic bars; no network; everything NOT_ACTIONABLE."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from app.labs import continuous_edge_factory_v10_38 as CE

T0 = 1_700_000_000_000
BAR = 60_000


def make_bars(n=700, seed=1, planted_signal=False):
    """Synthetic bars. With planted_signal=True, every 10th bar carries a CRISP
    trade-count burst (n_trades 500 vs ~50) immediately followed by a multi-bar
    rise that clears round-trip costs -- a REAL edge cleanly separable on
    burst_score, detectable at q90 with enough OOS sample. Otherwise pure noise
    with a slight mean-reverting-to-cost drag."""
    rng = random.Random(seed)
    bars = []
    price = 100.0
    pump = 0
    for i in range(n):
        signal = planted_signal and i % 10 == 0 and i > 0
        if signal:
            buy, sell, ntr = 450.0, 50.0, 500
            pump = 3                              # this bar + next 2 rise
        else:
            buy = rng.uniform(15, 35)
            sell = rng.uniform(15, 35)
            ntr = int(buy + sell)
        drift = 0.006 if pump > 0 else rng.uniform(-0.0012, 0.0012)
        pump = max(0, pump - 1)
        new = price * (1 + drift)
        bars.append({"ts": T0 + i * BAR, "open": price,
                     "high": max(price, new) * 1.0005,
                     "low": min(price, new) * 0.9995, "close": new,
                     "volume": buy + sell, "buy_volume": buy,
                     "sell_volume": sell, "n_trades": ntr,
                     "max_trade": max(buy, sell) / 5})
        price = new
    return bars


# ---- point-in-time: features never see the future ----------------------------

def test_features_do_not_use_future_bars():
    bars = make_bars(120, seed=2)
    f1 = CE.build_features(bars)
    labs1 = CE.build_labels(bars)
    # perturb ONLY the future -- full price path so the triple barrier (which
    # reads high/low, not just close) genuinely resolves differently.
    mutated = [dict(b) for b in bars]
    for b in mutated[80:]:
        for k in ("open", "high", "low", "close"):
            b[k] *= 3
    f2 = CE.build_features(mutated)
    labs2 = CE.build_labels(mutated)
    # A) point-in-time: features for bars[:80] cannot see bars[80:]
    for idx, (a, b) in enumerate(zip(f1[:80], f2[:80])):
        for k, v in a.items():
            assert b[k] == v, f"feature {k} at bar {idx} changed with future data"
    # B) label 79's entry is unmutated but its very first future bar (80) is in
    # the mutated region -> its outcome MUST change (future truly used). Compare
    # the whole row: a TP's cost_adjusted is constant, but forward_return_N /
    # time_to_hit / MAE / MFE / label_available_at are all path-dependent.
    assert labs1[79] != labs2[79]
    assert any(labs1[i] != labs2[i] for i in range(60, 80)), \
        "in-horizon future change ignored"
    # C) a label whose whole horizon ends before the mutation is untouched.
    assert labs1[10] == labs2[10]


def test_no_lookahead_guard_raises_on_violation():
    bars = make_bars(80)
    feats = CE.build_features(bars)
    labels = CE.build_labels(bars)
    CE.assert_no_lookahead(feats, labels)        # clean passes
    feats[5]["available_at"] = feats[5]["ts"] - 1
    with pytest.raises(ValueError):
        CE.assert_no_lookahead(feats, labels)
    labels[3]["label_available_at"] = labels[3]["ts"]      # label at own bar
    feats[5]["available_at"] = feats[5]["ts"]
    with pytest.raises(ValueError):
        CE.assert_no_lookahead(feats, labels)


def test_feature_bank_covers_all_blocks():
    bars = make_bars(60)
    oi = [{"timestamp": b["ts"], "open_interest": 1000 + i}
          for i, b in enumerate(bars)]
    fu = [{"timestamp": bars[0]["ts"], "funding_rate": "0.0001"}]
    ob = [{"timestamp": b["ts"], "bid_price_1": b["close"] * 0.999,
           "bid_size_1": 2, "ask_price_1": b["close"] * 1.001, "ask_size_1": 1}
          for b in bars]
    liq = [{"timestamp": bars[30]["ts"], "price": "100", "size": "1",
            "side": "sell"}]
    f = CE.build_features(bars, oi, fu, ob, liq)[-1]
    for k in ("trade_intensity", "buy_sell_imbalance", "burst_score", "spread",
              "top_imbalance", "book_pressure", "oi_change", "funding_level",
              "liquidation_count", "cascade_score", "realized_volatility",
              "trend_score", "stress_mode", "symbol_regime"):
        assert k in f, k


# ---- labels: triple barrier + costs + missing visibility ----------------------

def _bar(ts, o, h, l, cl):
    return dict(ts=ts, open=o, high=h, low=l, close=cl, volume=1, buy_volume=1,
                sell_volume=0, n_trades=1, max_trade=1)


def test_triple_barrier_tp_sl_time():
    # entry=100, tp=100.4, sl=99.8. First future bar touches NEITHER barrier;
    # the SECOND hits TP -> time_to_hit must be 2 (not forced early).
    bars = [_bar(T0, 100, 100, 100, 100),
            _bar(T0 + BAR, 100.1, 100.2, 99.9, 100.1),      # inside both
            _bar(T0 + 2 * BAR, 100.4, 100.6, 100.1, 100.5)]  # high>=tp
    lab = CE.build_labels(bars, tp_pct=0.004, sl_pct=0.002, time_bars=5)[0]
    assert lab["triple_barrier"] == "TP"
    assert lab["time_to_hit"] == 2
    assert lab["MFE"] >= 0.004 and lab["MAE"] <= 0.0    # saw the up move + a dip
    # SL on the very first future bar -> time_to_hit == 1 is correct.
    sl_bars = [_bar(T0, 100, 100, 100, 100),
               _bar(T0 + BAR, 100, 100.1, 99.0, 99.5)]
    lab = CE.build_labels(sl_bars, tp_pct=0.004, sl_pct=0.002)[0]
    assert lab["triple_barrier"] == "SL" and lab["time_to_hit"] == 1
    assert lab["MAE"] <= -0.009                          # touched ~-1%
    # Wide barriers never hit within the horizon -> TIME.
    time_bars = [_bar(T0, 100, 100, 100, 100),
                 _bar(T0 + BAR, 100, 100.3, 99.7, 100.1),
                 _bar(T0 + 2 * BAR, 100.1, 100.2, 99.8, 100.05)]
    lab = CE.build_labels(time_bars, tp_pct=0.02, sl_pct=0.02)[0]
    assert lab["triple_barrier"] == "TIME"
    # last bar has no future -> missing, never silently hidden
    assert CE.build_labels(time_bars)[-1]["missing"] is True


def test_costs_reduce_ev():
    bars = make_bars(100, seed=3)
    cheap = CE.build_labels(bars, costs={"fee_bps": 0, "slippage_bps": 0,
                                         "spread_bps": 0})
    dear = CE.build_labels(bars, costs={"fee_bps": 50, "slippage_bps": 50,
                                        "spread_bps": 10})
    pairs = [(c["cost_adjusted_outcome"], d["cost_adjusted_outcome"])
             for c, d in zip(cheap, dear) if not c.get("missing")]
    assert all(d < c for c, d in pairs)          # costs strictly reduce outcome


# ---- net-EV trainer -----------------------------------------------------------

def test_net_ev_decisions_and_never_actionable():
    small = CE.evaluate_net_ev([0.01] * 5)
    assert small["decision"] == "ABSTAIN" and "sample" in small["reason"]
    neg = CE.evaluate_net_ev([-0.001] * 60)
    assert neg["decision"] == "REJECT"
    strong = CE.evaluate_net_ev([0.004] * 40 + [-0.001] * 10)
    assert strong["decision"] == "TRADE_IN_SIMULATION_RESEARCH_ONLY"
    assert strong["net_EV_lower_bound"] < strong["net_EV"]   # uncertainty penalty
    for rep in (small, neg, strong):
        assert rep["can_send_real_orders"] is False
        assert rep["not_actionable"] is True
        assert rep["final_recommendation"] == "NO LIVE"
        for banned in ("BUY_NOW", "SELL_NOW", "OPEN_POSITION", "LIVE_SIGNAL"):
            assert banned not in str(rep)


# ---- discovery: verdicts honest, planted edge found ----------------------------

def test_discovery_finds_planted_edge_and_rejects_noise():
    bars = make_bars(700, seed=5, planted_signal=True)
    feats = CE.build_features(bars)
    labels = CE.build_labels(bars)
    cands = CE.discover_candidates(feats, labels)
    assert cands, "grid produced no candidates"
    assert all(c["verdict"] in CE.CANDIDATE_VERDICTS for c in cands)
    # honesty contract holds for EVERY candidate, edge or not
    assert all(c["not_actionable"] is True and c["edge_validated"] is False
               and c["final_recommendation"] == "NO LIVE" for c in cands)
    promising = [c for c in cands if c["verdict"] == "PROMISING_RESEARCH_ONLY"]
    assert promising, "planted edge not detected at all"
    assert any(c["setup_name"].startswith(("burst_score", "buy_sell_imbalance"))
               and c["side"] == "long" for c in promising), \
        "planted burst->pump edge not found"
    # pure noise -> essentially nothing PROMISING survives the cost + lower-bound
    noise = make_bars(700, seed=11, planted_signal=False)
    nf, nl = CE.build_features(noise), CE.build_labels(noise)
    npromising = [c for c in CE.discover_candidates(nf, nl)
                  if c["verdict"] == "PROMISING_RESEARCH_ONLY"]
    assert len(npromising) <= 1                  # at most one by chance


def test_module_no_dangerous_primitives():
    src = Path(CE.__file__).read_text(encoding="utf-8")
    for tok in ["urllib", "websocket", "requests", "load_dotenv", "os.environ",
                "private_get", "private_post", "api_key", "X-MBX-APIKEY",
                "BUY_NOW", "SELL_NOW", "OPEN_POSITION", "LIVE_SIGNAL"]:
        assert tok not in src, tok
    for name in ["place_order", "create_order", "set_leverage",
                 "set_margin_mode", "open_position"]:
        assert f"{name}(" not in src and f".{name}" not in src, name
