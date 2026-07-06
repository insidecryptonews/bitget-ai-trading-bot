"""V10.39 no-leakage guards: resampling preserves availability, drops partial
tails, and eval_rule thresholds are TRAIN-only (OOS never selects)."""

from __future__ import annotations

from pathlib import Path

from app.labs import alpha_improvement_sprint_v10_39 as A
from app.labs import continuous_edge_factory_v10_38 as CE

T0 = 1_700_000_000_000
BAR = 60_000


def _trades(minutes, per_min=3):
    base = (T0 // BAR) * BAR
    out = []
    for m in range(minutes):
        for k in range(per_min):
            out.append({"timestamp": base + m * BAR + k * 15_000,
                        "price": 100 + m * 0.1 + k * 0.02, "size": 1,
                        "aggressor_side": "buy" if k % 2 else "sell",
                        "symbol": "BTCUSDT"})
    return out


def test_resample_preserves_availability_and_drops_partial_tail():
    bars = CE.build_bars_from_trades(_trades(22), 60)   # 22 one-minute bars
    r = A.resample_bars(bars, 5)
    assert len(r) == 4                                   # 20 used, last 2 dropped
    for i, rb in enumerate(r):
        grp = bars[i * 5:i * 5 + 5]
        assert rb["available_at"] == max(b["available_at"] for b in grp)
        assert rb["available_at"] == grp[-1]["available_at"]   # anchors to close
        assert rb["ts"] == grp[-1]["ts"]
        assert rb["high"] == max(b["high"] for b in grp)
        assert rb["low"] == min(b["low"] for b in grp)
        assert rb["available_at"] > rb["bar_start_ts"]
    # strictly increasing close timestamps
    assert all(r[i]["ts"] < r[i + 1]["ts"] for i in range(len(r) - 1))


def test_resample_does_not_use_future_bars():
    bars = CE.build_bars_from_trades(_trades(40), 60)
    r1 = A.resample_bars(bars, 5)
    mutated = [dict(b) for b in bars]
    for b in mutated[30:]:                               # perturb only the future
        b["close"] *= 4
        b["high"] *= 4
    r2 = A.resample_bars(mutated, 5)
    for a, b in zip(r1[:6], r2[:6]):                     # groups 0..5 = bars 0..29
        assert a == b


def test_features_labels_on_resampled_bars_pass_no_lookahead():
    bars = CE.build_bars_from_trades(_trades(200), 60)
    for tf in (1, 3, 5):
        rb = A.resample_bars(bars, tf)
        feats = CE.build_features(rb)
        labels = CE.build_labels(rb, side="long", time_bars=10)
        assert CE.assert_no_lookahead(feats, labels, rb) is True


def _feat_row(ts, burst):
    row = {"ts": ts, "available_at": ts, "close": 100.0}
    for k in CE.DISCOVERY_FEATURES:
        row[k] = 0.0
    row["burst_score"] = burst
    return row


def _lab_row(ts, outcome):
    return {"ts": ts, "label_available_at": ts + 1, "missing": False,
            "side": "long", "cost_adjusted_outcome": outcome}


def test_eval_rule_threshold_is_train_only():
    import random
    rng = random.Random(0)
    feats, labs = [], []
    for i in range(200):
        burst = rng.uniform(0, 1) if i < 120 else 50.0
        feats.append(_feat_row(T0 + i * BAR, burst))
        labs.append(_lab_row(T0 + i * BAR, 0.002 if burst > 0.66 else -0.001))
    m1 = A.eval_rule(feats, labs, labs, "burst_score", "long", 0.9)
    for f in feats[120:]:                                # mutate ONLY OOS
        f["burst_score"] *= 100
    m2 = A.eval_rule(feats, labs, labs, "burst_score", "long", 0.9)
    assert m1["threshold"] == m2["threshold"]
    assert m1["threshold_source"] == "train_only"


def test_module_has_no_dangerous_primitives_and_does_not_lower_costs():
    src = Path(A.__file__).read_text(encoding="utf-8")
    for tok in ["urllib", "websocket", "requests", "load_dotenv", "os.environ",
                "api_key", "place_order", "create_order", "set_leverage",
                "BUY_NOW", "SELL_NOW", "OPEN_POSITION", "LIVE_SIGNAL"]:
        assert tok not in src, tok
    # costs are inherited from V10.38 defaults, never redefined lower here
    expected_rt = (2 * (CE.DEFAULT_COSTS["fee_bps"]
                        + CE.DEFAULT_COSTS["slippage_bps"]) / 10_000
                   + CE.DEFAULT_COSTS["spread_bps"] / 10_000)
    assert A._round_trip_cost() == expected_rt
    assert "DEFAULT_COSTS" not in src or "CE.DEFAULT_COSTS" in src  # reuses V10.38
