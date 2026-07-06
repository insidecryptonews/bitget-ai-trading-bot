"""V10.39 multi-timeframe contract: resampling preserves bar-close availability,
higher-timeframe features do not look ahead, the cost-aware scan stays
research-only, and small samples are never promising."""

from __future__ import annotations

import random

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
                        "price": 100 + m * 0.05 + k * 0.02, "size": 1,
                        "aggressor_side": "buy" if k % 2 else "sell",
                        "symbol": "BTCUSDT"})
    return out


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


def test_resample_bars_preserves_available_at_contract():
    bars = CE.build_bars_from_trades(_trades(15), 60)     # 15 one-minute bars
    r = A.resample_bars(bars, 3)                          # -> 5 three-minute bars
    assert len(r) == 5
    for i, rb in enumerate(r):
        grp = bars[i * 3:i * 3 + 3]
        src_avail = max(b["available_at"] for b in grp)
        assert rb["available_at"] >= src_avail            # not earlier than source
        assert rb["available_at"] == src_avail            # == last sub-bar close
        assert rb["available_at"] >= grp[-1]["bar_close_ts"]
        assert rb["available_at"] != rb["bar_start_ts"]   # never the bucket start
        assert rb["available_at"] > rb["bar_start_ts"]
        # aggregated OHLC really span the whole group (unknowable at its start)
        assert rb["high"] == max(b["high"] for b in grp)
        assert rb["low"] == min(b["low"] for b in grp)
        assert rb["close"] == grp[-1]["close"]


def test_multitimeframe_features_do_not_lookahead():
    bars = CE.build_bars_from_trades(_trades(200), 60)
    r = A.resample_bars(bars, 5)                          # 40 five-minute bars
    f1 = CE.build_features(r)
    # feature availability equals its source bar's availability (never earlier)
    for feat, rb in zip(f1, r):
        assert feat["available_at"] == rb["available_at"]
        assert feat["available_at"] >= feat["ts"]
    # perturb ONLY the future resampled bars -> earlier features cannot move
    mutated = [dict(b) for b in r]
    for b in mutated[25:]:
        b["close"] *= 3
        b["high"] *= 3
    f2 = CE.build_features(mutated)
    for a, b in zip(f1[:25], f2[:25]):
        assert a == b


def test_cost_aware_horizon_scan_multitimeframe_outputs_research_only():
    scan = A.cost_aware_horizon_scan(edge_bars(1500, seed=3), timeframes=(1, 3, 5))
    tfs = {row["timeframe_min"] for row in scan["rows"]}
    assert tfs == {1, 3, 5}
    for row in scan["rows"]:
        assert "timeframe_min" in row and "horizon" in row
        assert row.get("final_recommendation", "NO LIVE") == "NO LIVE"
    assert scan["can_send_real_orders"] is False
    assert scan["final_recommendation"] == "NO LIVE"
    for banned in ("BUY_NOW", "SELL_NOW", "OPEN_POSITION", "LIVE_SIGNAL",
                   "LIVE_READY", "CAN_SEND_REAL_ORDERS"):
        assert banned not in str(scan)


def test_multitimeframe_small_sample_never_promising():
    scan = A.cost_aware_horizon_scan(edge_bars(100, seed=3, planted=False),
                                     timeframes=(1, 3, 5))
    assert scan["any_promising"] is False
    for row in scan["rows"]:
        assert row["verdict"] != "PROMISING_RESEARCH_ONLY"
        assert row["verdict"] in ("NEEDS_MORE_DATA", "REJECTED_DATA_QUALITY",
                                  "REJECTED_NEGATIVE_EV", "REJECTED_COSTS_TOO_HIGH",
                                  "REJECTED_OVERFIT_RISK", "REJECTED_UNSTABLE",
                                  "NOT_ACTIONABLE")


def test_resample_factor_one_is_identity_copy():
    bars = edge_bars(50)
    r = A.resample_bars(bars, 1)
    assert len(r) == len(bars)
    assert r == [dict(b) for b in bars]
    r[0]["close"] = -999                                  # copy, not alias
    assert bars[0]["close"] != -999
