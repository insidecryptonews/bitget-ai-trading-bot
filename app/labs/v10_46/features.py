"""V10.46 causal feature store (RESEARCH ONLY, no lookahead).

Features are computed ONLY from bars whose close (available_time) is <= the
decision time. Every feature carries event_time / available_time / decision_time
metadata so validate_causal() can prove no future information was used.

This is a compact, transparent feature set (structure, trend, volatility,
momentum, volume, movement-consumed, distance-to-invalidation). It reuses the
same bar dicts produced by the verified dataset generations.
"""

from __future__ import annotations

import math
from typing import Any

BAR_MS = 60_000


def _slope(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2.0
    my = sum(xs) / n
    num = sum((i - mx) * (xs[i] - my) for i in range(n))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


def compute_features(bars_upto: list[dict], *, decision_time_ms: int,
                     lookback: int = 60) -> dict[str, Any]:
    """Compute causal features from bars whose close <= decision_time_ms.
    `bars_upto` must already be filtered to available bars (EventClock does
    this). Returns {"features": {...}, "feature_meta": {...}}."""
    win = bars_upto[-lookback:] if len(bars_upto) > lookback else list(bars_upto)
    if len(win) < 5:
        return {"features": {}, "feature_meta": {},
                "quality": "INSUFFICIENT_HISTORY"}
    closes = [float(b["close"]) for b in win]
    highs = [float(b["high"]) for b in win]
    lows = [float(b["low"]) for b in win]
    vols = [float(b.get("volume", 0.0)) for b in win]
    last = closes[-1]
    rets = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes))]
    mean_ret = sum(rets) / len(rets) if rets else 0.0
    var = sum((r - mean_ret) ** 2 for r in rets) / len(rets) if rets else 0.0
    vol = math.sqrt(var)
    # higher-highs / higher-lows structure over the last third
    third = max(3, len(win) // 3)
    hh = highs[-1] > max(highs[-third:-1]) if len(highs) > third else False
    hl = lows[-1] > min(lows[-third:-1]) if len(lows) > third else False
    ll = lows[-1] < min(lows[-third:-1]) if len(lows) > third else False
    lh = highs[-1] < max(highs[-third:-1]) if len(highs) > third else False
    slope = _slope(closes[-min(20, len(closes)):]) / last if last else 0.0
    # persistence: fraction of up bars in the window
    up_frac = sum(1 for r in rets if r > 0) / len(rets) if rets else 0.5
    # volume acceleration
    v_recent = sum(vols[-5:]) / 5 if len(vols) >= 5 else (sum(vols) / len(vols))
    v_base = sum(vols) / len(vols) if vols else 0.0
    vol_accel = (v_recent / v_base) if v_base else 1.0
    hi_w, lo_w = max(highs), min(lows)
    rng = (hi_w - lo_w) / last if last else 0.0
    # movement consumed: how far into the window's range we already are
    move_consumed = ((last - lo_w) / (hi_w - lo_w)) if hi_w > lo_w else 0.5
    # regime
    if slope > vol * 0.15 and up_frac > 0.55:
        regime = "TREND_UP"
    elif slope < -vol * 0.15 and up_frac < 0.45:
        regime = "TREND_DOWN"
    elif vol > 0 and rng > 4 * vol:
        regime = "HIGH_VOLATILITY"
    else:
        regime = "RANGE"
    feats = {
        "close": last, "ret_1": rets[-1] if rets else 0.0,
        "ret_mean": mean_ret, "volatility": vol, "slope": slope,
        "up_fraction": up_frac, "vol_accel": vol_accel, "range_frac": rng,
        "higher_high": float(hh), "higher_low": float(hl),
        "lower_low": float(ll), "lower_high": float(lh),
        "move_consumed": move_consumed, "regime": regime,
        "dist_to_hi": (hi_w - last) / last if last else 0.0,
        "dist_to_lo": (last - lo_w) / last if last else 0.0,
    }
    meta = {k: {"event_time_ms": int(win[-1]["ts"]),
                "available_time_ms": int(win[-1]["ts"]) + BAR_MS,
                "decision_time_ms": int(decision_time_ms),
                "lookback": len(win), "source": "klines_generation"}
            for k in feats}
    return {"features": feats, "feature_meta": meta, "quality": "OK",
            "regime": regime}
