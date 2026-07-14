"""V10.47.8 deterministic 1h/4h strategies (RESEARCH ONLY, NO LIVE).

Two pre-registered, fully deterministic, CAUSAL strategies built on the canonical
infra (EventClock timeframes, SimOMS, causal_ledger, causal_stats, registry). No
combinatorial search — one dimension per challenger.

  DET_EMA_ADX_PULLBACK  — regime by EMA50/EMA200 + ADX/DI, entry on a causal
                          pullback to EMA50 (ATR-normalised) with an RSI recovery
                          confirmed on CLOSED bars, next-bar open.
  DET_DONCHIAN_BREAKOUT — 20/55 Donchian channel EXCLUDING the current bar, with
                          EMA/ADX/DI regime and an >1-ATR "too extended" block,
                          next-bar open.

Indicators are computed only from CLOSED bars (index i uses bars[0..i]); the entry
is always the OPEN of bar i+1, so there is no lookahead. Multi-timeframe (1h entry
within 4h regime) is the intended design; the single-series form here is exact and
causal, and the runner gates the real evaluation on >= 2 years of verified data
(else INSUFFICIENT_DATA — never invented)."""

from __future__ import annotations

from . import families as FAM


# ------------------------------------------------------------- indicators
def _ema(vals: list[float], span: int) -> list[float]:
    k = 2.0 / (span + 1.0)
    out, e = [], None
    for v in vals:
        e = v if e is None else (v * k + e * (1 - k))
        out.append(e)
    return out


def _rsi(closes: list[float], period: int = 14) -> list[float]:
    out = [50.0] * len(closes)
    if len(closes) < period + 1:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    ag, al = gains / period, losses / period
    out[period] = 100.0 - 100.0 / (1 + (ag / al if al else 999))
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(ch, 0.0)) / period
        al = (al * (period - 1) + max(-ch, 0.0)) / period
        out[i] = 100.0 - 100.0 / (1 + (ag / al if al else 999))
    return out


def _atr_adx(bars: list[dict], period: int = 14):
    n = len(bars)
    atr = [0.0] * n
    adx = [0.0] * n
    plus_di = [0.0] * n
    minus_di = [0.0] * n
    if n < period + 1:
        return atr, adx, plus_di, minus_di
    tr_s = pdm_s = ndm_s = 0.0
    for i in range(1, period + 1):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        up = bars[i]["high"] - bars[i - 1]["high"]
        dn = bars[i - 1]["low"] - bars[i]["low"]
        tr_s += tr
        pdm_s += up if (up > dn and up > 0) else 0.0
        ndm_s += dn if (dn > up and dn > 0) else 0.0
    atr[period] = tr_s / period
    dx_hist = []
    for i in range(period + 1, n):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        up = bars[i]["high"] - bars[i - 1]["high"]
        dn = bars[i - 1]["low"] - bars[i]["low"]
        pdm = up if (up > dn and up > 0) else 0.0
        ndm = dn if (dn > up and dn > 0) else 0.0
        tr_s = tr_s - tr_s / period + tr
        pdm_s = pdm_s - pdm_s / period + pdm
        ndm_s = ndm_s - ndm_s / period + ndm
        atr[i] = tr_s / period
        pdi = 100.0 * pdm_s / tr_s if tr_s else 0.0
        ndi = 100.0 * ndm_s / tr_s if tr_s else 0.0
        plus_di[i], minus_di[i] = pdi, ndi
        di_sum = pdi + ndi
        dx = 100.0 * abs(pdi - ndi) / di_sum if di_sum else 0.0
        dx_hist.append(dx)
        if len(dx_hist) <= period:
            adx[i] = sum(dx_hist) / len(dx_hist)
        else:
            adx[i] = (adx[i - 1] * (period - 1) + dx) / period
    return atr, adx, plus_di, minus_di


def precompute_det_sig(bars: list[dict]) -> list[dict]:
    """Per-bar causal indicator bundle (index i uses bars[0..i] only)."""
    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200)
    rsi = _rsi(closes, 14)
    atr, adx, pdi, ndi = _atr_adx(bars, 14)
    n = len(bars)
    out = []
    for i in range(n):
        c = closes[i]
        # Donchian over prior 20 / 55 bars EXCLUDING the current bar
        lo20 = i - 20
        d20_hi = max(highs[max(0, lo20):i]) if i > 0 else c
        d20_lo = min(lows[max(0, lo20):i]) if i > 0 else c
        lo55 = i - 55
        d55_hi = max(highs[max(0, lo55):i]) if i > 0 else c
        d55_lo = min(lows[max(0, lo55):i]) if i > 0 else c
        a = atr[i] if atr[i] else (c * 0.001)
        out.append({
            "ok": i >= 200, "close": c, "ema50": ema50[i], "ema200": ema200[i],
            "rsi": rsi[i], "rsi_prev": rsi[i - 1] if i else 50.0,
            "atr": a, "atr_pct": a / c if c else 0.0,
            "adx": adx[i], "plus_di": pdi[i], "minus_di": ndi[i],
            "dist_ema50_atr": (c - ema50[i]) / a if a else 0.0,
            "don20_hi": d20_hi, "don20_lo": d20_lo,
            "don55_hi": d55_hi, "don55_lo": d55_lo})
    return out


from . import event_clock as EC


def precompute_det_sig_mtf(bars_entry: list[dict], *, entry_tf: str = "1h",
                           regime_tf: str = "4h") -> list[dict]:
    """Real multi-timeframe signal: entry features on the entry timeframe, regime
    from CLOSED higher-timeframe bars mapped ONLY to entry bars that open at/after
    the higher bar's close (no cross-timeframe lookahead). Each entry bar records
    `regime_4h_close_ts <= its own ts`. Closes Work audit finding P1.3 (4h→1h)."""
    entry_ms = EC.interval_ms_for(entry_tf)
    regime_ms = EC.interval_ms_for(regime_tf)
    base = precompute_det_sig(bars_entry)                 # entry-tf features
    # aggregate entry bars into wall-clock regime buckets
    buckets: dict[int, dict] = {}
    order: list[int] = []
    for b in bars_entry:
        k = int(b["ts"]) // regime_ms
        if k not in buckets:
            buckets[k] = {"ts": k * regime_ms, "open": b["open"], "high": b["high"],
                          "low": b["low"], "close": b["close"]}
            order.append(k)
        else:
            g = buckets[k]
            g["high"] = max(g["high"], b["high"])
            g["low"] = min(g["low"], b["low"])
            g["close"] = b["close"]
    regime_bars = [buckets[k] for k in order]             # ascending, causal order
    r_ema50 = _ema([r["close"] for r in regime_bars], 50)
    r_ema200 = _ema([r["close"] for r in regime_bars], 200)
    r_atr, r_adx, r_pdi, r_ndi = _atr_adx(regime_bars, 14)
    # regime published only at bucket close = bucket_start + regime_ms
    reg_at_close = []
    for j, r in enumerate(regime_bars):
        reg_at_close.append({
            "close_ts": r["ts"] + regime_ms, "ema50": r_ema50[j],
            "ema200": r_ema200[j], "adx": r_adx[j], "plus_di": r_pdi[j],
            "minus_di": r_ndi[j], "high": r["high"], "low": r["low"],
            "ready": j >= 200})
    out = []
    ri = -1                                               # last published regime idx
    for i, b in enumerate(bars_entry):
        ts = int(b["ts"])
        # advance ri to the latest regime whose CLOSE <= this bar's open ts
        while ri + 1 < len(reg_at_close) and reg_at_close[ri + 1]["close_ts"] <= ts:
            ri += 1
        s = dict(base[i])
        s["ts"] = ts
        if ri >= 0 and reg_at_close[ri]["ready"]:
            rg = reg_at_close[ri]
            s.update({"regime_ready": True, "regime_4h_close_ts": rg["close_ts"],
                      "ema50_4h": rg["ema50"], "ema200_4h": rg["ema200"],
                      "adx_4h": rg["adx"], "plus_di_4h": rg["plus_di"],
                      "minus_di_4h": rg["minus_di"],
                      "don20_4h_hi": max(r["high"] for r in regime_bars[max(0, ri-20):ri]) if ri > 0 else b["high"],
                      "don20_4h_lo": min(r["low"] for r in regime_bars[max(0, ri-20):ri]) if ri > 0 else b["low"]})
        else:
            s.update({"regime_ready": False, "regime_4h_close_ts": 0})
        out.append(s)
    return out


# ATR-based risk (Work audit P1.3): dynamic 2-ATR stop, trailing from +1R. NO
# fixed percentage stops. `stop_atr_mult`/`trail_atr_mult` are read by the ledger,
# which sizes each trade from the causal ATR available before entry.
DET_EXIT_ATR = {"stop_atr_mult": 2.0, "tp_atr_mult": 6.0, "trail_atr_mult": 2.0,
                "trail_activate_r": 1.0, "atr_period": 14, "time_exit": 24}

DET_EXIT = {"stop_frac": 0.02, "tp_frac": 0.06, "time_exit": 24,
            "trailing_frac": 0.02}          # legacy fixed-fraction (deprecated)

# pre-registered thresholds (one value each; no grid search)
ADX_MIN = 20.0
PULLBACK_ATR = 1.0        # within 1 ATR of EMA50
RSI_RECOVER = 45.0        # RSI crossing back up through ~45


def _mk(action, side, prob, *, symbol, venue, timeframe, event_id, dt, gen,
        policy_id, reason):
    return FAM._mk(action, side, prob, symbol=symbol, venue=venue,
                   timeframe=timeframe, event_id=event_id, dt=dt, gen_id=gen,
                   reason=reason, spec_hash=FAM.C.canonical_hash(
                       {"policy": policy_id}), policy_id=policy_id)


def ema_adx_pullback_decider(*, symbol, venue, timeframe, gen,
                             direction=None):
    pid = "DET_EMA_ADX_PULLBACK"

    def fn(feats, event_id, dt, cluster):
        s = feats["_sig"]
        if not s.get("ok"):
            return _mk("ABSTAIN_DATA_QUALITY", "FLAT", 0.5, symbol=symbol,
                       venue=venue, timeframe=timeframe, event_id=event_id,
                       dt=dt, gen=gen, policy_id=pid, reason="WARMUP")
        # REGIME from the higher timeframe (4h) when the MTF sig provides it;
        # falls back to the entry timeframe only for a single-series sig.
        if s.get("regime_ready"):
            r_ema50, r_ema200 = s["ema50_4h"], s["ema200_4h"]
            r_adx, r_pdi, r_ndi = s["adx_4h"], s["plus_di_4h"], s["minus_di_4h"]
        elif "regime_4h_close_ts" in s:          # MTF sig but regime not ready
            return _mk("ABSTAIN_DATA_QUALITY", "FLAT", 0.5, symbol=symbol,
                       venue=venue, timeframe=timeframe, event_id=event_id,
                       dt=dt, gen=gen, policy_id=pid, reason="REGIME_WARMUP")
        else:
            r_ema50, r_ema200 = s["ema50"], s["ema200"]
            r_adx, r_pdi, r_ndi = s["adx"], s["plus_di"], s["minus_di"]
        up_regime = (r_ema50 > r_ema200 and s["close"] > s["ema50"]
                     and r_adx >= ADX_MIN and r_pdi > r_ndi)
        dn_regime = (r_ema50 < r_ema200 and s["close"] < s["ema50"]
                     and r_adx >= ADX_MIN and r_ndi > r_pdi)
        side = None
        if up_regime and abs(s["dist_ema50_atr"]) <= PULLBACK_ATR \
                and s["rsi_prev"] < RSI_RECOVER <= s["rsi"]:
            side = "LONG"
        elif dn_regime and abs(s["dist_ema50_atr"]) <= PULLBACK_ATR \
                and s["rsi_prev"] > (100 - RSI_RECOVER) >= s["rsi"]:
            side = "SHORT"
        if side and direction and side != direction:
            side = None
        if side:
            return _mk("TRADE", side, 0.55, symbol=symbol, venue=venue,
                       timeframe=timeframe, event_id=event_id, dt=dt, gen=gen,
                       policy_id=pid, reason="PULLBACK")
        return _mk("ABSTAIN_LOW_REWARD", "FLAT", 0.5, symbol=symbol, venue=venue,
                   timeframe=timeframe, event_id=event_id, dt=dt, gen=gen,
                   policy_id=pid, reason="NO_SETUP")
    return fn


def donchian_breakout_decider(*, symbol, venue, timeframe, gen, direction=None):
    pid = "DET_DONCHIAN_BREAKOUT"

    def fn(feats, event_id, dt, cluster):
        s = feats["_sig"]
        if not s.get("ok"):
            return _mk("ABSTAIN_DATA_QUALITY", "FLAT", 0.5, symbol=symbol,
                       venue=venue, timeframe=timeframe, event_id=event_id,
                       dt=dt, gen=gen, policy_id=pid, reason="WARMUP")
        c, a = s["close"], s["atr"]
        # 4h regime + 4h Donchian channel when the MTF sig is present
        if s.get("regime_ready"):
            e50, e200, adx, pdi, ndi = (s["ema50_4h"], s["ema200_4h"], s["adx_4h"],
                                        s["plus_di_4h"], s["minus_di_4h"])
            hi_ch, lo_ch = s.get("don20_4h_hi", s["don20_hi"]), s.get("don20_4h_lo", s["don20_lo"])
        elif "regime_4h_close_ts" in s:
            return _mk("ABSTAIN_DATA_QUALITY", "FLAT", 0.5, symbol=symbol,
                       venue=venue, timeframe=timeframe, event_id=event_id,
                       dt=dt, gen=gen, policy_id=pid, reason="REGIME_WARMUP")
        else:
            e50, e200, adx, pdi, ndi = (s["ema50"], s["ema200"], s["adx"],
                                        s["plus_di"], s["minus_di"])
            hi_ch, lo_ch = s["don20_hi"], s["don20_lo"]
        long_regime = e50 > e200 and adx >= ADX_MIN and pdi > ndi
        short_regime = e50 < e200 and adx >= ADX_MIN and ndi > pdi
        side = None
        if long_regime and c >= hi_ch and (c - hi_ch) <= a:
            side = "LONG"
        elif short_regime and c <= lo_ch and (lo_ch - c) <= a:
            side = "SHORT"
        if side and direction and side != direction:
            side = None
        if side:
            return _mk("TRADE", side, 0.55, symbol=symbol, venue=venue,
                       timeframe=timeframe, event_id=event_id, dt=dt, gen=gen,
                       policy_id=pid, reason="BREAKOUT")
        return _mk("ABSTAIN_LOW_REWARD", "FLAT", 0.5, symbol=symbol, venue=venue,
                   timeframe=timeframe, event_id=event_id, dt=dt, gen=gen,
                   policy_id=pid, reason="NO_SETUP")
    return fn


DET_STRATEGIES = {
    "DET_EMA_ADX_PULLBACK_1H_4H": {"decider": ema_adx_pullback_decider,
                                   "exit": DET_EXIT_ATR, "entry_tf": "1h",
                                   "regime_tf": "4h", "mtf": True},
    "DET_DONCHIAN_BREAKOUT_4H": {"decider": donchian_breakout_decider,
                                 "exit": DET_EXIT_ATR, "entry_tf": "4h",
                                 "regime_tf": "4h", "mtf": False},
}
