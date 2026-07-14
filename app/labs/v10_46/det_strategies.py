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


def aggregate_complete_regime_bars(bars_entry: list[dict], *,
                                   entry_tf: str = "1h",
                                   regime_tf: str = "4h") -> dict:
    """Aggregate only complete, consecutive, duplicate-free closed buckets."""
    entry_ms = EC.interval_ms_for(entry_tf)
    regime_ms = EC.interval_ms_for(regime_tf)
    expected_count, remainder = divmod(regime_ms, entry_ms)
    if remainder or expected_count < 1:
        raise ValueError("regime timeframe must be an integer multiple of entry timeframe")
    by_bucket: dict[int, list[dict]] = {}
    invalid_order_buckets: set[int] = set()
    previous_ts = None
    for raw in bars_entry:
        ts = int(raw["ts"])
        bucket_start = (ts // regime_ms) * regime_ms
        if previous_ts is not None and ts <= previous_ts:
            invalid_order_buckets.add(bucket_start)
        previous_ts = ts
        by_bucket.setdefault(bucket_start, []).append(raw)
    complete: list[dict] = []
    bucket_status: dict[int, dict] = {}
    diagnostics = {
        "total_buckets": 0,
        "complete_buckets": 0,
        "incomplete_buckets": 0,
        "gap_buckets": 0,
        "duplicate_buckets": 0,
        "out_of_order_buckets": 0,
    }
    bucket_starts = []
    if by_bucket:
        first_bucket, last_bucket = min(by_bucket), max(by_bucket)
        bucket_starts = list(range(first_bucket, last_bucket + regime_ms, regime_ms))
    diagnostics["total_buckets"] = len(bucket_starts)
    for bucket_start in bucket_starts:
        rows = by_bucket.get(bucket_start, [])
        timestamps = [int(row["ts"]) for row in rows]
        expected = [bucket_start + offset * entry_ms for offset in range(expected_count)]
        duplicate = len(timestamps) != len(set(timestamps))
        out_of_order = bucket_start in invalid_order_buckets
        gap = sorted(set(timestamps)) != expected
        incomplete = len(rows) != expected_count
        if duplicate:
            diagnostics["duplicate_buckets"] += 1
        if out_of_order:
            diagnostics["out_of_order_buckets"] += 1
        if gap:
            diagnostics["gap_buckets"] += 1
        if incomplete:
            diagnostics["incomplete_buckets"] += 1
        bucket_status[bucket_start] = {
            "bucket_start": bucket_start,
            "close_ts": bucket_start + regime_ms,
            "row_count": len(rows),
            "duplicate": duplicate,
            "out_of_order": out_of_order,
            "gap": gap,
            "incomplete": incomplete,
            "complete": not (duplicate or out_of_order or gap or incomplete),
        }
        if duplicate or out_of_order or gap or incomplete:
            continue
        ordered = sorted(rows, key=lambda row: int(row["ts"]))
        complete.append({
            "ts": bucket_start,
            "close_ts": bucket_start + regime_ms,
            "open": ordered[0]["open"],
            "high": max(row["high"] for row in ordered),
            "low": min(row["low"] for row in ordered),
            "close": ordered[-1]["close"],
            "volume": sum(float(row.get("volume", 0.0)) for row in ordered),
            "component_timestamps": timestamps,
            "component_count": expected_count,
            "complete": True,
        })
        diagnostics["complete_buckets"] += 1
    return {"bars": complete, "diagnostics": diagnostics,
            "bucket_status": bucket_status,
            "entry_tf": entry_tf, "regime_tf": regime_tf,
            "expected_components": expected_count}


def precompute_det_sig_mtf(bars_entry: list[dict], *, entry_tf: str = "1h",
                           regime_tf: str = "4h") -> list[dict]:
    """Real multi-timeframe signal: entry features on the entry timeframe, regime
    from CLOSED higher-timeframe bars mapped ONLY to entry bars that open at/after
    the higher bar's close (no cross-timeframe lookahead). Each entry bar records
    `regime_4h_close_ts <= its own ts`. Closes Work audit finding P1.3 (4h→1h)."""
    regime_ms = EC.interval_ms_for(regime_tf)
    base = precompute_det_sig(bars_entry)                 # entry-tf features
    aggregation = aggregate_complete_regime_bars(
        bars_entry, entry_tf=entry_tf, regime_tf=regime_tf
    )
    regime_bars = aggregation["bars"]
    r_ema50 = _ema([r["close"] for r in regime_bars], 50)
    r_ema200 = _ema([r["close"] for r in regime_bars], 200)
    r_atr, r_adx, r_pdi, r_ndi = _atr_adx(regime_bars, 14)
    # regime published only at bucket close = bucket_start + regime_ms
    reg_at_close = []
    contiguous_run = 0
    previous_bucket_start = None
    for j, r in enumerate(regime_bars):
        if previous_bucket_start is not None \
                and r["ts"] == previous_bucket_start + regime_ms:
            contiguous_run += 1
        else:
            contiguous_run = 1
        previous_bucket_start = r["ts"]
        reg_at_close.append({
            "bucket_start": r["ts"],
            "close_ts": r["close_ts"], "ema50": r_ema50[j],
            "ema200": r_ema200[j], "adx": r_adx[j], "plus_di": r_pdi[j],
            "minus_di": r_ndi[j], "high": r["high"], "low": r["low"],
            "contiguous_run": contiguous_run,
            "ready": contiguous_run >= 201})
    reg_by_start = {row["bucket_start"]: row for row in reg_at_close}
    regime_index_by_start = {
        row["bucket_start"]: index for index, row in enumerate(reg_at_close)
    }
    out = []
    for i, b in enumerate(bars_entry):
        ts = int(b["ts"])
        latest_closed_start = (ts // regime_ms) * regime_ms - regime_ms
        local_status = aggregation["bucket_status"].get(latest_closed_start)
        rg = reg_by_start.get(latest_closed_start)
        s = dict(base[i])
        s["ts"] = ts
        s["mtf_aggregation_diagnostics"] = {
            "latest_closed_bucket_start": latest_closed_start,
            "latest_closed_bucket_status": (
                "COMPLETE" if local_status and local_status["complete"]
                else "INCOMPLETE_OR_MISSING"
            ),
        }
        s["incomplete_bucket"] = bool(
            local_status is not None and not local_status["complete"]
        )
        if rg is not None and local_status and local_status["complete"] \
                and rg["ready"]:
            ri = regime_index_by_start[latest_closed_start]
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


def deterministic_mtf_experiment_registry() -> dict:
    """Independent preregistration, not part of the twelve intraday tournaments."""
    participants = [
        "DET_EMA_ADX_PULLBACK_1H_4H",
        "DET_DONCHIAN_BREAKOUT_4H",
        "NO_TRADE",
        "EXACT_MATCH_BASELINE",
        "TREND_RIDER_1H_4H",
    ]
    return {
        "experiment_id": "DETERMINISTIC_MTF_1H_4H",
        "status": "IMPLEMENTED",
        "scientific_evaluation": "INSUFFICIENT_DATA",
        "needs_2y_data": True,
        "participants": participants,
        "baseline_contract": "V10_47_21_EXACT_ONE_TO_ONE",
        "same_bar_rule": "STOP_BEFORE_TP",
        "entry_rule": "NEXT_OPEN",
        "research_only": True,
        "paper_ready": False,
        "live_ready": False,
        "final_recommendation": "NO LIVE",
    }
