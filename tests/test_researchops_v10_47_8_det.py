"""V10.47.8 deterministic 1h/4h strategies — causal, deterministic, canonical
infra. Research only, NO LIVE."""

from __future__ import annotations

import random

from app.labs.v10_46 import det_strategies as DET
from app.labs.v10_46 import causal_ledger as CL


def _trend_bars(n=1200, seed=7, interval_ms=3_600_000):
    rng = random.Random(seed)
    price, bars = 100.0, []
    T0 = 1_700_000_000_000
    for i in range(n):
        ph = (i // 200) % 2
        drift = 0.002 if ph == 0 else -0.002
        new = price * (1 + drift + rng.uniform(-0.003, 0.003))
        bars.append({"ts": T0 + i * interval_ms, "open": price,
                     "high": max(price, new) * 1.002,
                     "low": min(price, new) * 0.998, "close": new,
                     "volume": 100.0})
        price = new
    return bars


def test_indicators_are_finite_and_causal_length():
    bars = _trend_bars()
    sig = DET.precompute_det_sig(bars)
    assert len(sig) == len(bars)
    for s in sig[300:305]:
        for k in ("ema50", "ema200", "rsi", "atr", "adx", "plus_di", "minus_di"):
            assert isinstance(s[k], float)
        assert 0.0 <= s["rsi"] <= 100.0
        assert 0.0 <= s["adx"] <= 100.0


def test_warmup_abstains_before_200_bars():
    bars = _trend_bars()
    sig = DET.precompute_det_sig(bars)
    assert sig[10]["ok"] is False and sig[250]["ok"] is True
    dec = DET.ema_adx_pullback_decider(symbol="X", venue="bitget",
                                       timeframe="1h", gen="g")
    d = dec({"_sig": sig[10], "ts": bars[10]["ts"]}, "X:1", bars[10]["ts"], "c")
    assert d["decision_action"].startswith("ABSTAIN")


def test_donchian_excludes_current_bar():
    """The 20-bar channel at i must be computed from bars[i-20:i], NOT including
    bar i, so a bar making a new high still 'breaks out' of the prior channel."""
    bars = _trend_bars()
    sig = DET.precompute_det_sig(bars)
    i = 400
    prior_hi = max(b["high"] for b in bars[i - 20:i])
    assert abs(sig[i]["don20_hi"] - prior_hi) < 1e-6


def test_both_strategies_run_causally_and_deterministically():
    bars = _trend_bars()
    sig = DET.precompute_det_sig(bars)
    for name, spec in DET.DET_STRATEGIES.items():
        dec = spec["decider"](symbol="X", venue="bitget", timeframe="1h", gen="g")
        a = CL.drive_causal(bars, sig, dec, spec["exit"], symbol="X", timeframe="1h")
        b = CL.drive_causal(bars, sig, dec, spec["exit"], symbol="X", timeframe="1h")
        assert sum(t["net_eur"] for t in a["trades"]) == \
            sum(t["net_eur"] for t in b["trades"])           # deterministic
        # single-position invariant holds
        assert a["counters"]["n_executed"] <= a["counters"]["n_signals_eligible"]


def test_direction_restriction_never_flips_side():
    bars = _trend_bars()
    sig = DET.precompute_det_sig(bars)
    decS = DET.donchian_breakout_decider(symbol="X", venue="bitget",
                                         timeframe="4h", gen="g", direction="SHORT")
    out = CL.drive_causal(bars, sig, decS, DET.DET_EXIT, symbol="X", timeframe="4h")
    assert all(t["side"] == "SHORT" for t in out["trades"])


def test_strategies_registered():
    assert "DET_EMA_ADX_PULLBACK_1H_4H" in DET.DET_STRATEGIES
    assert "DET_DONCHIAN_BREAKOUT_4H" in DET.DET_STRATEGIES
    ema = DET.DET_STRATEGIES["DET_EMA_ADX_PULLBACK_1H_4H"]
    assert ema["entry_tf"] == "1h" and ema["regime_tf"] == "4h" and ema["mtf"]
    don = DET.DET_STRATEGIES["DET_DONCHIAN_BREAKOUT_4H"]
    assert don["entry_tf"] == "4h"
    # ATR-based risk, not fixed percentage stops
    assert "stop_atr_mult" in ema["exit"] and "stop_frac" not in ema["exit"]
