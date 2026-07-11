"""ResearchOps V10.45.1 - Multi-AI Edge Discovery ENGINE (research only, NO LIVE).

The deterministic half of the multi-AI sprint:

  * ex-ante FEATURE ENGINE over 1m OHLCV bars (indicators, price action,
    volatility, sessions, regimes, optional cross-venue and flow features);
  * strict STRATEGY SCHEMA + COMPILER: AI/procedural JSON -> deterministic
    rules; ambiguous, dangerous, duplicate or impossible strategies rejected;
  * one CANONICAL REPLAY for every candidate and baseline: next-open entry,
    fees+spread+slippage per side, funding pro-rata, SL-before-TP same-bar
    conservatism, partial TP1 by tranche, causal trailing, time exits, gap
    handling and end-of-replay censoring;
  * a staged FUNNEL: discovery -> screening (parameter perturbation + light
    cost stress) -> validation (non-overlapping trades, multiple-testing lower
    bound) -> LOCKED HOLDOUT (finalists only, never re-tuned) -> cost stress;
  * mandatory BASELINES through the exact same replay;
  * an append-only experiment LEDGER including every rejected strategy.

Gates cap at PAPER_CANDIDATE_RESEARCH_ONLY. A live-ready state does not exist
in this vocabulary.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import statistics as st
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE

TOOL_VERSION = "v10.45.3"
OUTPUT_SUBDIR = ("reports", "research", "v10_45_3_edge_discovery")

# execution-model elements that are still PROXIES: promotion is capped at
# SHADOW_CANDIDATE_RESEARCH_ONLY while any of these remain (fail-closed)
EXECUTION_PROXIES = (
    "funding_charged_as_abs_value_not_side_aware",
    "entry_fills_binary_no_partial_entry_fill",
    "latency_only_modeled_in_stress_variant",
    "tp1_before_remaining_stop_same_bar_uses_stop_first_conservatism",
)
PROXY_NOTE = "PROXY_NOT_PROMOTION_ELIGIBLE"

# fail-closed promotion thresholds (all must hold simultaneously)
MIN_PF = 1.15
MAX_DD = -0.10
MAX_CENSORED_RATIO = 0.20
BAR_MS = 60_000
WARMUP = 240                      # bars needed before first feature row is valid
EMBARGO_BARS = 240                # purge between funnel segments
MAX_CONDITIONS = 6
MIN_DISCOVERY_TRADES = 20
MIN_VALIDATION_TRADES = 15
MIN_HOLDOUT_TRADES = 10

DEFAULT_COSTS = {"taker_fee_bps": 6.0, "spread_bps": 1.0, "slippage_bps": 2.0,
                 "funding_bps_per_8h": 1.0}

ALLOWED_STATES = ("REJECTED", "DUPLICATE", "INVALID", "INVALID_DATA",
                  "NEED_MORE_DATA", "WATCHLIST_RESEARCH_ONLY",
                  "SHADOW_CANDIDATE_RESEARCH_ONLY",
                  "PAPER_CANDIDATE_RESEARCH_ONLY")
REGIMES = ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY", "LOW_VOLATILITY",
           "ASIA", "EU", "US", "ANY")
OPS = (">", "<", ">=", "<=", "cross_up", "cross_down")
FORBIDDEN_WORDS = ("order", "leverage", "margin", "live", "api_key", "secret",
                   "withdraw", "transfer", "position_size")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "simulation_only": True, "shadow_only": True,
            "can_send_real_orders": False, "paper_filter_enabled": False,
            "edge_validated": False, "not_actionable": True, "no_orders": True,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _out() -> Path:
    d = CE._repo_root().joinpath(*OUTPUT_SUBDIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ==========================================================================
# FEATURE ENGINE (all ex-ante: row i uses bars[0..i] only)
# ==========================================================================

def _ema_series(vals: list[float], n: int) -> list[float]:
    if not vals:
        return []
    k = 2.0 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def build_features(bars: list[dict], ref_bars: list[dict] | None = None
                   ) -> list[dict[str, Any]]:
    """Feature rows aligned to bars; row i is available at bars[i] close.
    ref_bars (another venue) are aligned by open-ts for cross-venue features."""
    n = len(bars)
    if n == 0:
        return []
    o = [b["open"] for b in bars]
    h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]
    c = [b["close"] for b in bars]
    v = [b.get("volume", 0.0) for b in bars]
    ts = [b["ts"] for b in bars]
    buyv = [b.get("buy_volume") for b in bars]
    has_flow = all(x is not None for x in buyv[:50]) if n >= 50 else False

    ema8 = _ema_series(c, 8)
    ema21 = _ema_series(c, 21)
    ema55 = _ema_series(c, 55)
    ema200 = _ema_series(c, 200)

    # Wilder-style incrementals
    rsi = [50.0] * n
    avg_g = avg_l = 0.0
    tr_list = [0.0] * n
    atr = [0.0] * n
    adx = [0.0] * n
    pdm_s = ndm_s = tr_s = 0.0
    dx_hist: list[float] = []
    macd_line = [0.0] * n
    ema12 = _ema_series(c, 12)
    ema26 = _ema_series(c, 26)
    for i in range(n):
        macd_line[i] = ema12[i] - ema26[i]
    macd_sig = _ema_series(macd_line, 9)

    ref_by_ts = {rb["ts"]: rb for rb in (ref_bars or [])}
    ref_c_prev: dict[int, float] = {}
    if ref_bars:
        rc = [rb["close"] for rb in ref_bars]
        rts = [rb["ts"] for rb in ref_bars]
        for j in range(len(ref_bars)):
            ref_c_prev[rts[j]] = rc[j - 5] if j >= 5 else rc[0]

    obv = 0.0
    obv_hist: list[float] = []
    feats: list[dict[str, Any]] = []
    day_anchor = None
    vwap_num = vwap_den = 0.0
    for i in range(n):
        close, high, low, open_ = c[i], h[i], l[i], o[i]
        # RSI(14) Wilder
        if i > 0:
            ch = close - c[i - 1]
            g, ls = max(ch, 0.0), max(-ch, 0.0)
            if i <= 14:
                avg_g += g / 14
                avg_l += ls / 14
            else:
                avg_g = (avg_g * 13 + g) / 14
                avg_l = (avg_l * 13 + ls) / 14
            rsi[i] = 100.0 - 100.0 / (1 + avg_g / avg_l) if avg_l > 1e-12 else 100.0
        # TR / ATR(14) / ADX(14)
        if i > 0:
            tr = max(high - low, abs(high - c[i - 1]), abs(low - c[i - 1]))
            pdm = max(high - h[i - 1], 0.0)
            ndm = max(l[i - 1] - low, 0.0)
            if pdm < ndm:
                pdm = 0.0
            elif ndm < pdm:
                ndm = 0.0
            if i <= 14:
                tr_s += tr; pdm_s += pdm; ndm_s += ndm
                atr[i] = tr_s / min(i, 14)
            else:
                tr_s = tr_s - tr_s / 14 + tr
                pdm_s = pdm_s - pdm_s / 14 + pdm
                ndm_s = ndm_s - ndm_s / 14 + ndm
                atr[i] = tr_s / 14
            pdi = 100 * pdm_s / tr_s if tr_s > 1e-12 else 0.0
            ndi = 100 * ndm_s / tr_s if tr_s > 1e-12 else 0.0
            dx = 100 * abs(pdi - ndi) / (pdi + ndi) if (pdi + ndi) > 1e-12 else 0.0
            dx_hist.append(dx)
            adx[i] = st.mean(dx_hist[-14:]) if len(dx_hist) >= 3 else 0.0
        # OBV
        if i > 0:
            obv += v[i] if close > c[i - 1] else (-v[i] if close < c[i - 1] else 0.0)
        obv_hist.append(obv)
        # session VWAP (UTC-day anchored)
        day = ts[i] // 86_400_000
        if day != day_anchor:
            day_anchor = day
            vwap_num = vwap_den = 0.0
        tp = (high + low + close) / 3.0
        vwap_num += tp * v[i]
        vwap_den += v[i]
        vwap = vwap_num / vwap_den if vwap_den > 1e-12 else close

        def ret(k: int) -> float:
            return close / c[i - k] - 1.0 if i >= k and c[i - k] > 0 else 0.0
        win20 = c[max(0, i - 19):i + 1]
        sma20 = st.mean(win20)
        sd20 = st.pstdev(win20) if len(win20) > 2 else 0.0
        hh20 = max(h[max(0, i - 19):i]) if i >= 1 else high      # PRIOR 20 highs
        ll20 = min(l[max(0, i - 19):i]) if i >= 1 else low
        hh55 = max(h[max(0, i - 54):i]) if i >= 1 else high
        ll55 = min(l[max(0, i - 54):i]) if i >= 1 else low
        atr_pct = atr[i] / close if close > 0 else 0.0
        rets30 = [c[j] / c[j - 1] - 1 for j in range(max(1, i - 29), i + 1)]
        rv30 = st.pstdev(rets30) if len(rets30) > 2 else 0.0
        vol_hist = [x for x in v[max(0, i - 29):i + 1]]
        vol_mean = st.mean(vol_hist) if vol_hist else 0.0
        vol_sd = st.pstdev(vol_hist) if len(vol_hist) > 2 else 0.0
        vol_z = (v[i] - vol_mean) / vol_sd if vol_sd > 1e-12 else 0.0
        # ATR percentile over 240 bars
        atr_win = [atr[j] for j in range(max(1, i - 239), i + 1)]
        atr_rank = (sum(1 for x in atr_win if x <= atr[i]) / len(atr_win)
                    if atr_win else 0.5)
        bb_up = sma20 + 2 * sd20
        bb_dn = sma20 - 2 * sd20
        bb_w = (bb_up - bb_dn) / close if close > 0 else 0.0
        kel_up = ema21[i] + 1.5 * atr[i]
        kel_dn = ema21[i] - 1.5 * atr[i]
        stoch_win = rsi[max(0, i - 13):i + 1]
        s_lo, s_hi = (min(stoch_win), max(stoch_win)) if stoch_win else (0, 100)
        hour = int((ts[i] // 3_600_000) % 24)
        dow = int(((ts[i] // 86_400_000) + 4) % 7)      # 1970-01-01 = Thursday
        macd_h = macd_line[i] - macd_sig[i]
        macd_h_prev = (macd_line[i - 1] - macd_sig[i - 1]) if i > 0 else macd_h
        obv_slope = ((obv_hist[-1] - obv_hist[-11]) / (abs(obv_hist[-11]) + 1e-9)
                     if len(obv_hist) > 11 else 0.0)
        # regime
        if atr_rank >= 0.8:
            vol_regime = "HIGH_VOLATILITY"
        elif atr_rank <= 0.2:
            vol_regime = "LOW_VOLATILITY"
        else:
            vol_regime = "MID_VOL"
        if ema21[i] > ema55[i] and close > ema200[i]:
            trend_regime = "TREND_UP"
        elif ema21[i] < ema55[i] and close < ema200[i]:
            trend_regime = "TREND_DOWN"
        else:
            trend_regime = "RANGE"
        session = "ASIA" if hour < 8 else ("EU" if hour < 14 else "US")
        # cross-venue (ref close at same bar ts; both known at bar close)
        rb = ref_by_ts.get(ts[i])
        if rb is not None and rb.get("close", 0) > 0 and i >= 5 and c[i - 5] > 0:
            ref_ret5 = rb["close"] / ref_c_prev.get(ts[i], rb["close"]) - 1.0
            xv_ret_gap = ref_ret5 - ret(5)
            xv_dislocation = (rb["close"] / close) - 1.0
        else:
            xv_ret_gap = 0.0
            xv_dislocation = 0.0
        f = {
            "i": i, "ts": ts[i], "available_at": bars[i].get("available_at", ts[i] + BAR_MS),
            "close": close,
            "ret_1": ret(1), "ret_3": ret(3), "ret_5": ret(5),
            "ret_15": ret(15), "ret_30": ret(30), "ret_60": ret(60),
            "rsi_14": rsi[i],
            "stoch_rsi": ((rsi[i] - s_lo) / (s_hi - s_lo) if s_hi > s_lo else 0.5),
            "cci_20": ((tp - st.mean([(h[j] + l[j] + c[j]) / 3 for j in range(max(0, i - 19), i + 1)]))
                       / (0.015 * (st.pstdev([(h[j] + l[j] + c[j]) / 3 for j in range(max(0, i - 19), i + 1)]) + 1e-9))
                       if i >= 3 else 0.0),
            "roc_10": ret(10),
            "macd_hist": macd_h, "macd_hist_prev": macd_h_prev,
            "macd_cross_up": 1.0 if macd_h > 0 >= macd_h_prev else 0.0,
            "macd_cross_down": 1.0 if macd_h < 0 <= macd_h_prev else 0.0,
            "adx_14": adx[i], "atr_14": atr[i], "atr_pct": atr_pct,
            "atr_percentile_240": atr_rank,
            "bb_pos": (close - bb_dn) / (bb_up - bb_dn) if bb_up > bb_dn else 0.5,
            "bb_width": bb_w,
            "keltner_pos": (close - kel_dn) / (kel_up - kel_dn) if kel_up > kel_dn else 0.5,
            "squeeze_on": 1.0 if (bb_up < kel_up and bb_dn > kel_dn) else 0.0,
            "donchian_pos_20": (close - ll20) / (hh20 - ll20) if hh20 > ll20 else 0.5,
            "donchian_break_up": 1.0 if close > hh20 else 0.0,
            "donchian_break_down": 1.0 if close < ll20 else 0.0,
            "donchian_break_up_55": 1.0 if close > hh55 else 0.0,
            "donchian_break_down_55": 1.0 if close < ll55 else 0.0,
            "ema_fast_slope": (ema8[i] / ema21[i] - 1.0) if ema21[i] > 0 else 0.0,
            "ema_slow_slope": (ema21[i] / ema55[i] - 1.0) if ema55[i] > 0 else 0.0,
            "price_vs_ema200": (close / ema200[i] - 1.0) if ema200[i] > 0 else 0.0,
            "ema_align_up": 1.0 if ema8[i] > ema21[i] > ema55[i] else 0.0,
            "ema_align_down": 1.0 if ema8[i] < ema21[i] < ema55[i] else 0.0,
            "vwap_dist": (close / vwap - 1.0) if vwap > 0 else 0.0,
            "obv_slope_10": obv_slope,
            "vol_z_30": vol_z, "rv_30": rv30,
            "range_pos_20": (close - ll20) / (hh20 - ll20) if hh20 > ll20 else 0.5,
            "body_pct": (close - open_) / close if close > 0 else 0.0,
            "upper_wick": (high - max(open_, close)) / close if close > 0 else 0.0,
            "lower_wick": (min(open_, close) - low) / close if close > 0 else 0.0,
            "compression": (bb_w / (atr_pct + 1e-9)) if atr_pct > 0 else 0.0,
            "hour_utc": float(hour), "dow": float(dow),
            "is_funding_hour": 1.0 if hour in (0, 8, 16) else 0.0,
            "session": session, "trend_regime": trend_regime,
            "vol_regime": vol_regime,
            "xv_ret_gap": xv_ret_gap, "xv_dislocation": xv_dislocation,
        }
        if has_flow:
            bv = float(bars[i].get("buy_volume") or 0.0)
            sv = float(bars[i].get("sell_volume") or 0.0)
            f["flow_imbalance"] = (bv - sv) / (bv + sv) if (bv + sv) > 0 else 0.0
        feats.append(f)
    return feats


FEATURE_REGISTRY = (
    "ret_1", "ret_3", "ret_5", "ret_15", "ret_30", "ret_60", "rsi_14",
    "stoch_rsi", "cci_20", "roc_10", "macd_hist", "macd_cross_up",
    "macd_cross_down", "adx_14", "atr_pct", "atr_percentile_240", "bb_pos",
    "bb_width", "keltner_pos", "squeeze_on", "donchian_pos_20",
    "donchian_break_up", "donchian_break_down", "donchian_break_up_55",
    "donchian_break_down_55", "ema_fast_slope", "ema_slow_slope",
    "price_vs_ema200", "ema_align_up", "ema_align_down", "vwap_dist",
    "obv_slope_10", "vol_z_30", "rv_30", "range_pos_20", "body_pct",
    "upper_wick", "lower_wick", "compression", "hour_utc", "dow",
    "is_funding_hour", "xv_ret_gap", "xv_dislocation", "flow_imbalance")


# ==========================================================================
# STRATEGY SCHEMA + COMPILER
# ==========================================================================

ALLOWED_TOP_KEYS = frozenset({
    "strategy_id", "hypothesis", "economic_rationale", "symbols", "side",
    "timeframe", "required_features", "regime_filter", "entry_conditions",
    "invalidation", "stop_policy", "take_profit_policy", "trailing_policy",
    "time_exit", "cooldown", "expected_failure_modes", "falsification_test",
    "origin"})
ALLOWED_COND_KEYS = frozenset({"feature", "op", "value"})
ALLOWED_STOP_KEYS = frozenset({"type", "value"})
ALLOWED_TP_KEYS = frozenset({"type", "value", "partial"})
ALLOWED_PARTIAL_KEYS = frozenset({"tp1_frac", "tp1_value", "move_stop_to_be"})
ALLOWED_TRAIL_KEYS = frozenset({"type", "value", "activate_after"})


def _finite(x) -> float:
    """float() that rejects NaN/Infinity/booleans instead of passing them on."""
    if isinstance(x, bool) or not isinstance(x, (int, float, str)):
        raise ValueError("non-numeric")
    v = float(x)
    if not math.isfinite(v):
        raise ValueError("non-finite")
    return v


def semantic_signature(spec: dict, symbol: str = "", timeframe: str = "") -> str:
    """Signature over the COMPILED, NORMALIZED spec — everything that alters
    execution: side, regime, conditions, stop/tp (incl partial), trailing,
    time_exit, cooldown, timeframe and symbol. Two different raw JSONs that
    compile identically ARE duplicates; a different cooldown is NOT."""
    payload = {
        "symbol": symbol, "timeframe": timeframe,
        "side": spec["side"], "regime": spec["regime_filter"],
        "conditions": sorted((f, o, round(v, 10)) for f, o, v in spec["conditions"]),
        "stop": {"type": spec["stop"]["type"], "value": round(spec["stop"]["value"], 10)},
        "tp": {"type": spec["tp"]["type"], "value": round(spec["tp"]["value"], 10),
               "partial": ({"tp1_frac": round(spec["tp"]["partial"]["tp1_frac"], 10),
                            "tp1_value": round(spec["tp"]["partial"]["tp1_value"], 10),
                            "move_stop_to_be": spec["tp"]["partial"]["move_stop_to_be"]}
                           if spec["tp"]["partial"] else None)},
        "trail": {"type": spec["trail"]["type"],
                  "value": round(spec["trail"]["value"], 10),
                  "activate_after": spec["trail"].get("activate_after")},
        "time_exit": spec["time_exit"], "cooldown": spec["cooldown"],
        "declared_symbols": spec.get("declared_symbols"),
        "declared_timeframe": spec.get("declared_timeframe"),
        "required_features": sorted(spec.get("required_features") or []) or None}
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def compile_strategy(s: dict, seen_signatures: set[str], symbol: str = "",
                     timeframe: str = "") -> tuple[str, dict | None]:
    """Validate + compile a strategy JSON -> deterministic spec.

    CLOSED schema: unknown fields anywhere are INVALID; NaN/Infinity are
    INVALID; cross_up/cross_down are honoured ONLY for macd_hist (never
    silently remapped); the compiled spec preserves the declared semantics.
    Dedup uses the semantic signature of the COMPILED spec."""
    if not isinstance(s, dict):
        return "INVALID", None
    if set(s.keys()) - ALLOWED_TOP_KEYS:
        return "INVALID", None                # unknown top-level fields
    blob = json.dumps(s, default=str).lower()
    for w in FORBIDDEN_WORDS:
        if f'"{w}' in blob or f"{w}(" in blob:
            return "INVALID", None
    side = str(s.get("side", "")).upper()
    if side not in ("LONG", "SHORT"):
        return "INVALID", None
    conds = s.get("entry_conditions")
    if not isinstance(conds, list) or not (1 <= len(conds) <= MAX_CONDITIONS):
        return "INVALID", None
    compiled_conds = []
    for cond in conds:
        if not isinstance(cond, dict) or set(cond.keys()) - ALLOWED_COND_KEYS:
            return "INVALID", None
        feat = cond.get("feature")
        op = cond.get("op")
        if feat not in FEATURE_REGISTRY or op not in OPS:
            return "INVALID", None
        if op in ("cross_up", "cross_down"):
            # a cross has explicit semantics ONLY for macd_hist (its cross
            # features exist); any other feature+cross is INVALID, never a
            # silent remap to something the author did not write
            if feat != "macd_hist":
                return "INVALID", None
            compiled_conds.append((f"macd_{op}", ">", 0.5))
            continue
        try:
            val = _finite(cond.get("value"))
        except (TypeError, ValueError):
            return "INVALID", None
        compiled_conds.append((feat, op, val))
    regime = s.get("regime_filter") or "ANY"
    if regime not in REGIMES:
        return "INVALID", None
    # ---- CLOSED CONTRACT: declared fields are honoured or the strategy is
    # rejected — nothing is silently ignored
    decl_symbols = s.get("symbols")
    if decl_symbols is not None:
        if not isinstance(decl_symbols, list) or not decl_symbols:
            return "INVALID", None
        wanted = [str(x).upper() for x in decl_symbols]
        if symbol and symbol.upper() not in wanted:
            return "INVALID", None             # SYMBOL_MISMATCH: never run
    decl_tf = s.get("timeframe")
    if decl_tf is not None and timeframe and str(decl_tf) != str(timeframe):
        return "INVALID", None                 # TIMEFRAME_MISMATCH: never run
    req_feats = s.get("required_features")
    if req_feats is not None:
        if not isinstance(req_feats, list) or \
                any(f not in FEATURE_REGISTRY for f in req_feats):
            return "INVALID", None             # unknown required feature
    invalidation = s.get("invalidation")
    if invalidation not in (None, "", [], {}):
        return "INVALID", None                 # UNSUPPORTED_FIELD: not implemented
    stop = s.get("stop_policy") or {}
    tp = s.get("take_profit_policy") or {}
    trail = s.get("trailing_policy") or {"type": "none", "value": 0.0}
    if not isinstance(stop, dict) or set(stop.keys()) - ALLOWED_STOP_KEYS:
        return "INVALID", None
    if not isinstance(tp, dict) or set(tp.keys()) - ALLOWED_TP_KEYS:
        return "INVALID", None
    if not isinstance(trail, dict) or set(trail.keys()) - ALLOWED_TRAIL_KEYS:
        return "INVALID", None
    try:
        stop_type = stop.get("type", "fixed")
        stop_val = _finite(stop.get("value", 0))
        tp_type = tp.get("type", "fixed")
        tp_val = _finite(tp.get("value", 0))
        partial = tp.get("partial") or None
        if partial is not None:
            if not isinstance(partial, dict) or \
                    set(partial.keys()) - ALLOWED_PARTIAL_KEYS:
                return "INVALID", None
            p_frac = _finite(partial.get("tp1_frac", 0))
            p_val = _finite(partial.get("tp1_value", 0))
            if not (0.1 <= p_frac <= 0.9) or p_val <= 0:
                return "INVALID", None
            partial = {"tp1_frac": p_frac, "tp1_value": p_val,
                       "move_stop_to_be": bool(partial.get("move_stop_to_be", True))}
        trail_type = trail.get("type", "none")
        trail_val = _finite(trail.get("value", 0) or 0)
        trail_act = trail.get("activate_after")
        if trail_act is not None:
            trail_act = _finite(trail_act)     # implemented: activation threshold
        time_exit = int(s.get("time_exit", 0) or 0)
        cooldown = int(s.get("cooldown", 5) or 5)
    except (TypeError, ValueError):
        return "INVALID", None
    if stop_type not in ("fixed", "atr") or tp_type not in ("fixed", "atr", "rr"):
        return "INVALID", None
    if trail_type not in ("none", "fixed", "atr"):
        return "INVALID", None
    # trailing ranges: a negative/zero/absurd trail once produced a fill at
    # 101.10 on a bar whose high was 100.10 — hard-reject out-of-range values
    if trail_type == "none":
        if trail_val != 0 or trail_act is not None:
            return "INVALID", None
    elif trail_type == "fixed":
        if not (0.0005 <= trail_val <= 0.05):
            return "INVALID", None
    else:                                      # atr multiple
        if not (0.25 <= trail_val <= 10.0):
            return "INVALID", None
    if trail_act is not None and not (0.0 <= trail_act <= 0.10):
        return "INVALID", None
    if stop_val <= 0 or tp_val <= 0 or not (1 <= time_exit <= 240):
        return "INVALID", None
    if stop_type == "fixed" and not (0.0005 <= stop_val <= 0.05):
        return "INVALID", None
    if tp_type == "fixed" and not (0.0005 <= tp_val <= 0.10):
        return "INVALID", None
    spec = {
        "strategy_id": str(s.get("strategy_id") or "unnamed")[:80],
        "hypothesis": str(s.get("hypothesis", ""))[:300],
        "economic_rationale": str(s.get("economic_rationale", ""))[:300],
        "origin": str(s.get("origin", "unknown"))[:40],
        "side": side, "regime_filter": regime,
        "conditions": compiled_conds,
        "stop": {"type": stop_type, "value": stop_val},
        "tp": {"type": tp_type, "value": tp_val, "partial": partial},
        "trail": {"type": trail_type, "value": trail_val,
                  "activate_after": trail_act},
        "time_exit": time_exit, "cooldown": max(1, cooldown),
        "declared_symbols": ([str(x).upper() for x in decl_symbols]
                             if decl_symbols else None),
        "declared_timeframe": str(decl_tf) if decl_tf is not None else None,
        "required_features": list(req_feats) if req_feats else None,
        "expected_failure_modes": [str(x)[:120] for x in
                                   (s.get("expected_failure_modes") or [])][:6],
        "falsification_test": str(s.get("falsification_test", ""))[:200]}
    sig = semantic_signature(spec, symbol=symbol, timeframe=timeframe)
    if sig in seen_signatures:
        return "DUPLICATE", None
    seen_signatures.add(sig)
    spec["signature"] = sig
    if spec["strategy_id"] == "unnamed":
        spec["strategy_id"] = f"strat_{sig}"
    return "OK", spec


def _regime_ok(f: dict, regime: str) -> bool:
    if regime == "ANY":
        return True
    if regime in ("TREND_UP", "TREND_DOWN", "RANGE"):
        return f.get("trend_regime") == regime
    if regime in ("HIGH_VOLATILITY", "LOW_VOLATILITY"):
        return f.get("vol_regime") == regime
    if regime in ("ASIA", "EU", "US"):
        return f.get("session") == regime
    return True


def _conditions_true(f: dict, conds: list[tuple]) -> bool:
    for feat, op, val in conds:
        x = f.get(feat)
        if not isinstance(x, (int, float)):
            return False
        if op == ">" and not x > val:
            return False
        if op == "<" and not x < val:
            return False
        if op == ">=" and not x >= val:
            return False
        if op == "<=" and not x <= val:
            return False
    return True


# ==========================================================================
# CANONICAL REPLAY (single logic for every candidate and baseline)
# ==========================================================================

def replay(bars: list[dict], feats: list[dict], spec: dict,
           costs: dict | None = None, i_start: int = WARMUP,
           i_end: int | None = None, cooldown_override: int | None = None,
           entry_fill_prob: float = 1.0, extra_entry_slip_bps: float = 0.0,
           rng_seed: int = 7) -> dict[str, Any]:
    """Deterministic bar replay of one compiled strategy over [i_start, i_end).

    Contract: signal on bar i close -> entry at bar i+1 open (+slippage+half
    spread). SL beats TP inside the same bar. Partial TP1 closes a tranche at
    tp1 and can move the stop to entry. Trailing stops are computed from
    COMPLETED bars only and applied to the next bar. Gaps: no entry across a
    gap; forced STALE exit at last close when a gap opens mid-trade. The final
    open position is closed at the last close and flagged censored."""
    cst = {**DEFAULT_COSTS, **(costs or {})}
    per_side = (cst["taker_fee_bps"] + cst["spread_bps"] / 2
                + cst["slippage_bps"]) / 10_000.0
    # infer the bar interval (1m/5m/15m...) so gap detection and pro-rata
    # funding stay correct on resampled data
    if len(bars) >= 3:
        diffs = sorted(bars[i + 1]["ts"] - bars[i]["ts"]
                       for i in range(min(200, len(bars) - 1)))
        bar_interval = max(diffs[len(diffs) // 2], BAR_MS)
    else:
        bar_interval = BAR_MS
    bars_per_8h = max(1.0, 8 * 3_600_000 / bar_interval)
    fund_per_bar = cst["funding_bps_per_8h"] / 10_000.0 / bars_per_8h
    i_end = min(i_end if i_end is not None else len(bars) - 1, len(bars) - 1)
    cooldown = cooldown_override if cooldown_override is not None else spec["cooldown"]
    rng = random.Random(rng_seed)
    trades: list[dict] = []
    pos: dict | None = None
    last_exit_i = -10 ** 9
    long = spec["side"] == "LONG"

    def _tranche_ret(entry_eff: float, exit_px: float, frac: float,
                     bars_held: int, exit_reason: str) -> dict:
        exit_eff = exit_px * (1 - per_side) if long else exit_px * (1 + per_side)
        gross = (exit_eff / entry_eff - 1.0) if long else (entry_eff / exit_eff - 1.0)
        net = gross - fund_per_bar * bars_held
        return {"frac": frac, "net": net, "exit_reason": exit_reason,
                "bars_held": bars_held}

    def _close_all(exit_px: float, reason: str, i: int, censored: bool = False):
        nonlocal pos, last_exit_i
        tr = _tranche_ret(pos["entry_eff"], exit_px, pos["frac_open"],
                          i - pos["entry_i"], reason)
        pos["tranches"].append(tr)
        net = sum(t["net"] * t["frac"] for t in pos["tranches"])
        trades.append({"entry_i": pos["entry_i"], "exit_i": i,
                       "side": spec["side"], "net_return": round(net, 8),
                       "exit_reason": reason, "bars_held": i - pos["entry_i"],
                       "tranches": len(pos["tranches"]), "censored": censored})
        last_exit_i = i
        pos = None

    def _fill_price(target_px: float, bar_open: float, lo: float, hi: float,
                    exit_is_sell: bool) -> float:
        """Realistic, range-contained fill for a stop/TP touched this bar.

        If the market gapped THROUGH the level (open already beyond it), the
        order fills at the OPEN — never at the untraded level. A bearish gap
        can never become profit; no fill may leave [low, high]."""
        if exit_is_sell:                       # LONG exits sell
            raw = bar_open if bar_open <= target_px else target_px
        else:                                  # SHORT exits buy
            raw = bar_open if bar_open >= target_px else target_px
        assert lo - 1e-9 <= raw <= hi + 1e-9, \
            f"fill {raw} outside traded range [{lo}, {hi}]"
        return raw

    for i in range(i_start, i_end):
        bar = bars[i]
        nxt = bars[i + 1]
        # STRICT: any delta other than exactly one bar interval is a gap —
        # a 2*T step means one candle is missing and must not be traded across
        gap = (nxt["ts"] - bar["ts"]) != bar_interval
        # ---- manage open position on the CURRENT bar
        if pos is not None:
            hi, lo = bar["high"], bar["low"]
            op = bar["open"]
            # 1) stop first (conservative on same-bar ambiguity)
            stop_px = pos["stop_px"]
            hit_stop = (lo <= stop_px) if long else (hi >= stop_px)
            if hit_stop:
                fill = _fill_price(stop_px, op, lo, hi, exit_is_sell=long)
                reason = ("TRAIL" if pos.get("trail_moved")
                          else ("BE_STOP" if pos["be_moved"] else "SL"))
                _close_all(fill, reason, i)
            else:
                # 2) partial TP1 (only when the stop was not touched this bar)
                if pos["tp1_px"] is not None:
                    hit1 = (hi >= pos["tp1_px"]) if long else (lo <= pos["tp1_px"])
                    if hit1:
                        fill1 = _fill_price(pos["tp1_px"], op, lo, hi,
                                            exit_is_sell=long)
                        frac = pos["tp1_frac"]
                        pos["tranches"].append(_tranche_ret(
                            pos["entry_eff"], fill1, frac,
                            i - pos["entry_i"], "TP1"))
                        pos["frac_open"] = round(pos["frac_open"] - frac, 6)
                        pos["tp1_px"] = None
                        if pos["move_be"]:
                            pos["stop_px"] = pos["entry_px"]
                            pos["be_moved"] = True
                # 3) final TP
                if pos is not None and pos["tp_px"] is not None:
                    hit_tp = (hi >= pos["tp_px"]) if long else (lo <= pos["tp_px"])
                    if hit_tp:
                        fill_tp = _fill_price(pos["tp_px"], op, lo, hi,
                                              exit_is_sell=long)
                        _close_all(fill_tp, "TP", i)
                # 4) time exit
                if pos is not None and (i - pos["entry_i"]) >= spec["time_exit"]:
                    _close_all(bar["close"], "TIME", i)
                # 5) gap ahead -> stale exit at current close
                if pos is not None and gap:
                    _close_all(bar["close"], "STALE_EXIT", i)
                # 6) causal trailing: update stop AFTER this completed bar
                if pos is not None and spec["trail"]["type"] != "none":
                    pos["hwm"] = max(pos["hwm"], hi) if long else min(pos["hwm"], lo)
                    tv = spec["trail"]["value"]
                    dist = tv if spec["trail"]["type"] == "fixed" \
                        else tv * (feats[i]["atr_pct"] or 0.001)
                    dist = max(dist, 1e-6)              # compiler bans <=0 anyway
                    cand = pos["hwm"] * (1 - dist) if long else pos["hwm"] * (1 + dist)
                    fav = (pos["hwm"] / pos["entry_px"] - 1.0) if long \
                        else (1.0 - pos["hwm"] / pos["entry_px"])
                    act = spec["trail"].get("activate_after")
                    threshold = act if act is not None else dist
                    if fav >= threshold:                # activation honoured
                        new_stop = max(pos["stop_px"], cand) if long \
                            else min(pos["stop_px"], cand)
                        if new_stop != pos["stop_px"]:
                            pos["trail_moved"] = True   # TRAIL != SL in reports
                        pos["stop_px"] = new_stop
        # ---- new entry decision at bar i close
        if pos is None and (i - last_exit_i) >= cooldown and not gap:
            f = feats[i]
            if _regime_ok(f, spec["regime_filter"]) and \
                    _conditions_true(f, spec["conditions"]):
                if entry_fill_prob < 1.0 and rng.random() > entry_fill_prob:
                    continue                              # modeled non-fill
                entry_px = nxt["open"]
                if entry_px <= 0:
                    continue
                slip = per_side + extra_entry_slip_bps / 10_000.0
                entry_eff = entry_px * (1 + slip) if long else entry_px * (1 - slip)
                atr_p = f["atr_pct"] or 0.001
                sv = spec["stop"]["value"]
                stop_dist = sv if spec["stop"]["type"] == "fixed" else sv * atr_p
                tpv = spec["tp"]["value"]
                if spec["tp"]["type"] == "fixed":
                    tp_dist = tpv
                elif spec["tp"]["type"] == "atr":
                    tp_dist = tpv * atr_p
                else:                                     # rr multiple of stop
                    tp_dist = tpv * stop_dist
                stop_dist = max(stop_dist, 0.0003)
                tp_dist = max(tp_dist, 0.0003)
                partial = spec["tp"]["partial"]
                pos = {"entry_i": i + 1, "entry_px": entry_px,
                       "entry_eff": entry_eff,
                       "stop_px": entry_px * (1 - stop_dist) if long
                       else entry_px * (1 + stop_dist),
                       "tp_px": entry_px * (1 + tp_dist) if long
                       else entry_px * (1 - tp_dist),
                       "tp1_px": (entry_px * (1 + partial["tp1_value"]) if long
                                  else entry_px * (1 - partial["tp1_value"]))
                       if partial else None,
                       "tp1_frac": partial["tp1_frac"] if partial else 0.0,
                       "move_be": partial["move_stop_to_be"] if partial else False,
                       "be_moved": False, "trail_moved": False, "frac_open": 1.0,
                       "hwm": entry_px, "tranches": []}
    if pos is not None:
        _close_all(bars[i_end]["close"], "END_CENSORED", i_end, censored=True)
    return {"trades": trades, "n_trades": len(trades)}


# ==========================================================================
# METRICS + GATES
# ==========================================================================

def metrics(trades: list[dict], n_tests: int = 1) -> dict[str, Any]:
    """EV / PF / win-rate / DD are computed ONLY over trades with an executable
    exit (TP/SL/BE/TRAIL/TIME). END_CENSORED and STALE_EXIT never enter the
    performance statistics — they are reported separately and count against
    promotion via censored_ratio."""
    valid = [t for t in trades if t["exit_reason"] in VALID_EXIT_REASONS
             and not t.get("censored")]
    censored = [t for t in trades if t.get("censored")
                or t["exit_reason"] in CENSORED_REASONS]
    invalid_exec = [t for t in trades
                    if t["exit_reason"] in INVALID_EXECUTION_REASONS]
    xs = [t["net_return"] for t in valid]
    n = len(xs)
    n_total = len(trades)
    base = {"n_trades": n, "n_total_outcomes": n_total,
            "censored": len(censored), "invalid_execution": len(invalid_exec),
            "censored_ratio": round((len(censored) + len(invalid_exec))
                                    / n_total, 4) if n_total else 0.0}
    if n == 0:
        return {**base, "net_EV": None, "net_EV_lower_bound": None,
                "profit_factor": None, "win_rate": None, "max_drawdown": None,
                "outlier_dependence": None, "stability_sign": None,
                "n_eff": 0}
    mean = st.mean(xs)
    sd = st.pstdev(xs) if n > 1 else 0.0
    lb = mean - 1.65 * sd / math.sqrt(n) \
        - math.sqrt(max(math.log(max(n_tests, 2)), 0.0)) * sd / math.sqrt(n)
    wins = [x for x in xs if x > 0]
    losses = [x for x in xs if x < 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else \
        (999.0 if wins else 0.0)
    cur = peak = dd = 0.0
    for x in xs:
        cur += x
        peak = max(peak, cur)
        dd = min(dd, cur - peak)
    # exclude exactly the 3 largest returns BY POSITION (a value-based filter
    # would wipe out every trade when returns are identical)
    rest = sorted(xs, reverse=True)[3:]
    ev_wo_top = st.mean(rest) if (n > 5 and rest) else None
    half = n // 2
    s1 = st.mean(xs[:half]) if half else 0.0
    s2 = st.mean(xs[half:]) if n - half else 0.0
    # ---- effective sample size: CONSERVATIVE, documented, never n by default.
    # Two independent shrink factors; the harsher one wins:
    #  (a) temporal occupancy: overlapping holding periods share information
    #  (b) autocorrelation of the trade-return series (Newey-West style,
    #      positive lags 1..5): serially dependent PnL shrinks n
    holds = [t["bars_held"] for t in valid]
    span = (valid[-1]["exit_i"] - valid[0]["entry_i"]) if n > 1 else sum(holds)
    occupancy = sum(holds) / span if span > 0 else 1.0
    occ_factor = max(1.0, occupancy)
    acf_factor = 1.0
    if n >= 10 and sd > 1e-12:
        centered = [x - mean for x in xs]
        denom = sum(c * c for c in centered)
        rho_sum = 0.0
        for lag in range(1, min(6, n // 2)):
            num = sum(centered[t] * centered[t - lag] for t in range(lag, n))
            rho = num / denom if denom > 1e-18 else 0.0
            rho_sum += max(rho, 0.0)           # only positive dependence shrinks
        acf_factor = 1.0 + 2.0 * rho_sum
    n_eff_proxy = n < 10                       # too small to estimate honestly
    if n_eff_proxy:
        n_eff = max(1, n // 2)                 # conservative penalty
        n_eff_method = "N_EFF_PROXY_half_n (sample too small for estimation)"
    else:
        n_eff = int(n / max(occ_factor, acf_factor))
        n_eff_method = (f"min-shrink(occupancy={occ_factor:.3f}, "
                        f"acf_factor={acf_factor:.3f}), lags 1-5, positive rho only")
    return {**base, "net_EV": round(mean, 8),
            "net_EV_lower_bound": round(lb, 8),
            "n_tests_applied": n_tests,
            "profit_factor": round(pf, 4),
            "win_rate": round(len(wins) / n, 4),
            "max_drawdown": round(dd, 8),
            "avg_hold": round(st.mean(holds), 2),
            "outlier_dependence": (round(ev_wo_top, 8)
                                   if ev_wo_top is not None else None),
            "stability_sign": (1 if (s1 > 0) == (s2 > 0) else 0) if n >= 10 else None,
            "n_eff": n_eff, "n_eff_method": n_eff_method,
            "n_eff_is_proxy": n_eff_proxy}


def validation_eligible_for_holdout(val_m: dict | None, stress_ok: bool,
                                    data_quality_pass: bool,
                                    baseline_best_lb: float | None = None,
                                    matched_baseline_ev: float | None = None,
                                    execution_proxies: tuple = EXECUTION_PROXIES
                                    ) -> tuple[bool, list[str]]:
    """FAIL-CLOSED gatekeeper for the LOCKED holdout. The holdout may be read
    ONLY when every requirement below holds simultaneously in validation.
    Returns (eligible, structured_failure_reasons)."""
    reasons: list[str] = []
    if not data_quality_pass:
        reasons.append("DATA_QUALITY_FAIL")
    if val_m is None:
        return False, reasons + ["NO_VALIDATION_METRICS"]
    if val_m.get("n_trades", 0) < MIN_VALIDATION_TRADES:
        reasons.append("N_TOO_SMALL")
    if val_m.get("n_eff", 0) < MIN_VALIDATION_TRADES:
        reasons.append("N_EFF_TOO_SMALL")
    if val_m.get("n_eff_is_proxy"):
        reasons.append("N_EFF_PROXY")
    if (val_m.get("censored_ratio") or 0) > MAX_CENSORED_RATIO:
        reasons.append("CENSORED_EXCESSIVE")
    if (val_m.get("net_EV") or 0) <= 0:
        reasons.append("EV_NOT_POSITIVE")
    if (val_m.get("net_EV_lower_bound") or 0) <= 0:
        reasons.append("LOWER_BOUND_NOT_POSITIVE_AFTER_MULTIPLE_TESTING")
    if (val_m.get("profit_factor") or 0) < MIN_PF:
        reasons.append("PF_TOO_LOW")
    if (val_m.get("max_drawdown") or 0) < MAX_DD:
        reasons.append("DRAWDOWN_EXCESSIVE")
    if baseline_best_lb is not None and \
            (val_m.get("net_EV_lower_bound") or 0) <= baseline_best_lb:
        reasons.append("BASELINE_NOT_BEATEN")
    if matched_baseline_ev is not None and \
            (val_m.get("net_EV") or 0) <= matched_baseline_ev:
        reasons.append("EXPOSURE_MATCHED_BASELINE_NOT_BEATEN")
    if not stress_ok:
        reasons.append("COST_STRESS_FAILED")
    if val_m.get("stability_sign") == 0:
        reasons.append("UNSTABLE_ACROSS_HALVES")
    if val_m.get("outlier_dependence") is not None and \
            val_m["outlier_dependence"] <= 0:
        reasons.append("OUTLIER_DEPENDENT")
    return (len(reasons) == 0), reasons


def state_from_reasons(reasons: list[str]) -> str:
    """Deterministic state for a candidate that did NOT reach the holdout."""
    if "DATA_QUALITY_FAIL" in reasons:
        return "INVALID_DATA"
    sample = {"N_TOO_SMALL", "N_EFF_TOO_SMALL", "CENSORED_EXCESSIVE",
              "NO_VALIDATION_METRICS", "N_EFF_PROXY"}
    if any(r in sample for r in reasons):
        return "NEED_MORE_DATA"
    hard = {"EV_NOT_POSITIVE", "PF_TOO_LOW", "DRAWDOWN_EXCESSIVE",
            "BASELINE_NOT_BEATEN", "EXPOSURE_MATCHED_BASELINE_NOT_BEATEN",
            "COST_STRESS_FAILED"}
    if any(r in hard for r in reasons):
        return "REJECTED"
    return "WATCHLIST_RESEARCH_ONLY"


def gate(val_m: dict, hold_m: dict | None, stress_ok: bool,
         data_quality_pass: bool = False,
         baseline_best_lb: float | None = None,
         matched_baseline_ev: float | None = None,
         execution_proxies: tuple = EXECUTION_PROXIES) -> str:
    """FAIL-CLOSED promotion gate. The holdout side is AT LEAST as strict as
    validation: n, n_eff, EV, lower bound, PF, drawdown and censoring are all
    required. Execution proxies cap promotion at SHADOW."""
    eligible, reasons = validation_eligible_for_holdout(
        val_m, stress_ok, data_quality_pass, baseline_best_lb,
        matched_baseline_ev, execution_proxies)
    if not eligible:
        return state_from_reasons(reasons)
    # ---- holdout is REQUIRED for anything above WATCHLIST
    if hold_m is None:
        return "WATCHLIST_RESEARCH_ONLY"     # holdout not executed
    if hold_m.get("n_trades", 0) < MIN_HOLDOUT_TRADES or \
            hold_m.get("n_eff", 0) < MIN_HOLDOUT_TRADES:
        return "NEED_MORE_DATA"              # zero/few/dependent holdout trades
    if (hold_m.get("censored_ratio") or 0) > MAX_CENSORED_RATIO:
        return "NEED_MORE_DATA"
    if (hold_m.get("max_drawdown") or 0) < MAX_DD:
        return "REJECTED"                    # e.g. the -90% drawdown case
    if (hold_m.get("net_EV") or 0) <= 0 or \
            (hold_m.get("net_EV_lower_bound") or 0) <= 0 or \
            (hold_m.get("profit_factor") or 0) < MIN_PF:
        return "REJECTED"
    # ---- everything passed; proxies cap promotion below PAPER
    if execution_proxies:
        return "SHADOW_CANDIDATE_RESEARCH_ONLY"
    return "PAPER_CANDIDATE_RESEARCH_ONLY"


def exposure_matched_baseline(bars: list[dict], spec: dict, val_trades: list[dict],
                              i0: int, i1: int, costs: dict | None = None,
                              n_seeds: int = 15) -> dict[str, Any]:
    """Baseline matched on EXPOSURE: same number of entries, same side, same
    (median) holding time, same window, same costs and censoring — only the
    TIMING is randomized. If the candidate cannot beat random timing with its
    own exposure profile, its entries carry no information."""
    valid = [t for t in val_trades if t["exit_reason"] in VALID_EXIT_REASONS
             and not t.get("censored")]
    if not valid:
        return {"status": "NO_TRADES", "mean_EV": None}
    n_entries = len(valid)
    hold = max(1, int(st.median([t["bars_held"] for t in valid])))
    long = spec["side"] == "LONG"
    cst = {**DEFAULT_COSTS, **(costs or {})}
    per_side = (cst["taker_fee_bps"] + cst["spread_bps"] / 2
                + cst["slippage_bps"]) / 10_000.0
    if len(bars) >= 3:
        diffs = sorted(bars[i + 1]["ts"] - bars[i]["ts"]
                       for i in range(min(200, len(bars) - 1)))
        interval = max(diffs[len(diffs) // 2], BAR_MS)
    else:
        interval = BAR_MS
    fund = cst["funding_bps_per_8h"] / 10_000.0 / max(1.0, 8 * 3_600_000 / interval)
    means = []
    for seed in range(n_seeds):
        rng = random.Random(10_000 + seed)
        rets = []
        attempts = 0
        while len(rets) < n_entries and attempts < n_entries * 20:
            attempts += 1
            i = rng.randrange(i0, max(i0 + 1, i1 - hold - 2))
            # STRICT continuity across the simulated hold (same censoring rule)
            ok = all(bars[j + 1]["ts"] - bars[j]["ts"] == interval
                     for j in range(i, i + hold + 1))
            if not ok:
                continue
            e = bars[i + 1]["open"] * (1 + per_side) if long \
                else bars[i + 1]["open"] * (1 - per_side)
            xr = bars[i + 1 + hold]["close"] * (1 - per_side) if long \
                else bars[i + 1 + hold]["close"] * (1 + per_side)
            g = (xr / e - 1.0) if long else (e / xr - 1.0)
            rets.append(g - fund * hold)
        if rets:
            means.append(st.mean(rets))
    if not means:
        return {"status": "NO_VALID_WINDOWS", "mean_EV": None}
    return {"status": "OK", "mean_EV": round(st.mean(means), 8),
            "sd_across_seeds": round(st.pstdev(means), 8) if len(means) > 1 else 0.0,
            "n_seeds": len(means), "matched_entries": n_entries,
            "matched_hold_bars": hold, "side": spec["side"]}


# ==========================================================================
# BASELINES (same replay, same costs, same windows)
# ==========================================================================

def baseline_specs() -> list[dict]:
    def mk(sid, side, conds, tp, sl, te, regime="ANY", trail_type="none",
           trail_val=0.0):
        return {"strategy_id": sid, "origin": "baseline", "side": side,
                "regime_filter": regime,
                "entry_conditions": conds,
                "stop_policy": {"type": "fixed", "value": sl},
                "take_profit_policy": {"type": "fixed", "value": tp},
                "trailing_policy": {"type": trail_type, "value": trail_val},
                "time_exit": te, "cooldown": 5,
                "hypothesis": "baseline", "economic_rationale": "baseline"}
    return [
        mk("baseline_always_long", "LONG",
           [{"feature": "ret_1", "op": ">=", "value": -1.0}], 0.006, 0.006, 30),
        mk("baseline_always_short", "SHORT",
           [{"feature": "ret_1", "op": ">=", "value": -1.0}], 0.006, 0.006, 30),
        mk("baseline_ema_cross_long", "LONG",
           [{"feature": "ema_fast_slope", "op": ">", "value": 0.0},
            {"feature": "ema_align_up", "op": ">", "value": 0.5}], 0.008, 0.006, 45),
        mk("baseline_rsi_meanrev_long", "LONG",
           [{"feature": "rsi_14", "op": "<", "value": 30.0}], 0.006, 0.006, 30),
        mk("baseline_rsi_meanrev_short", "SHORT",
           [{"feature": "rsi_14", "op": ">", "value": 70.0}], 0.006, 0.006, 30),
        mk("baseline_donchian_break_long", "LONG",
           [{"feature": "donchian_break_up", "op": ">", "value": 0.5}], 0.01, 0.006, 60),
    ]


def run_random_baseline(bars, feats, i_start, i_end, costs, freq: float,
                        seed: int = 99) -> dict:
    """Random entries at the given per-bar frequency through the SAME replay."""
    rng = random.Random(seed)
    spec = {"strategy_id": "baseline_random", "signature": "random",
            "side": "LONG", "regime_filter": "ANY",
            "conditions": [("ret_1", ">=", -1.0)],
            "stop": {"type": "fixed", "value": 0.006},
            "tp": {"type": "fixed", "value": 0.006, "partial": None},
            "trail": {"type": "none", "value": 0.0},
            "time_exit": 30, "cooldown": 1}
    marks = {i for i in range(i_start, i_end) if rng.random() < freq}
    fcopy = []
    for f in feats:
        g = dict(f)
        g["ret_1"] = 0.0 if f["i"] in marks else -2.0    # gate via condition
        fcopy.append(g)
    r = replay(bars, fcopy, spec, costs=costs, i_start=i_start, i_end=i_end)
    return metrics(r["trades"])


def buy_and_hold_net(bars, i_start, i_end, costs) -> float | None:
    cst = {**DEFAULT_COSTS, **(costs or {})}
    per_side = (cst["taker_fee_bps"] + cst["spread_bps"] / 2
                + cst["slippage_bps"]) / 10_000.0
    try:
        e = bars[i_start + 1]["open"] * (1 + per_side)
        x = bars[i_end]["close"] * (1 - per_side)
        held = i_end - i_start
        return x / e - 1.0 - cst["funding_bps_per_8h"] / 10_000.0 / 480.0 * held
    except Exception:
        return None


# ==========================================================================
# EXPERIMENT LEDGER (append-only; includes every rejected strategy)
# ==========================================================================

RUNNER_VERSION = "edge_discovery_v10_45_3"
_PROVENANCE_FILES = ("edge_discovery_engine_v10_45_1.py",
                     "multi_ai_orchestrator_v10_45_1.py",
                     "public_data_backfill_v10_45_1.py",
                     "ai_providers_v10_45_1.py")


def code_tree_hash() -> dict[str, Any]:
    """SHA256 over the exact source files that produce results, plus a
    worktree-dirty flag: results are attributable to CONTENT, not to whatever
    commit hash happened to be HEAD when the run started."""
    import subprocess
    base = Path(__file__).parent
    per_file = {}
    h = hashlib.sha256()
    for name in _PROVENANCE_FILES:
        try:
            data = (base / name).read_bytes()
        except OSError:
            data = b""
        digest = hashlib.sha256(data).hexdigest()
        per_file[name] = digest[:16]
        h.update(digest.encode())
    dirty = None
    try:
        out = subprocess.run(["git", "status", "--porcelain"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(CE._repo_root()))
        dirty = bool(out.stdout.strip())
    except Exception:
        pass
    return {"code_tree_hash": h.hexdigest()[:32], "files": per_file,
            "runner_version": RUNNER_VERSION, "dirty_worktree": dirty}


RUN_CONTEXT: dict[str, Any] = {}     # run_id, commit, dataset shas, splits, costs...


def set_run_context(**kw) -> None:
    RUN_CONTEXT.clear()
    RUN_CONTEXT.update(kw)


def ledger_append(entry: dict) -> None:
    """Append-only, reproducible: every entry carries the full run provenance
    (run_id, commit, dataset SHA, symbol, timeframe, splits, cost config,
    data quality) so any result can be re-derived. Failures included."""
    p = _out() / "experiment_ledger_v10_45_3.jsonl"
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _now(), **RUN_CONTEXT, **entry},
                           default=str) + "\n")


# ==========================================================================
# FUNNEL
# ==========================================================================

def resample_bars(bars: list[dict], factor: int,
                  as_of_ms: int | None = None) -> list[dict]:
    """STRICT aggregation: a factor-minute bar exists ONLY when its bucket
    contains exactly `factor` consecutive, duplicate-free 1m bars aligned to
    the wall-clock boundary. Incomplete (4/5, 14/15), discontinuous or
    misaligned buckets are REJECTED.

    The trailing bucket is kept ONLY when it is complete AND its close has
    already happened relative to an EXPLICIT `as_of_ms`; without an as_of
    clock the trailing bucket is dropped (it may still be open)."""
    if factor <= 1:
        return bars
    bucket_ms = factor * BAR_MS
    groups: dict[int, list[dict]] = {}
    for b in bars:
        groups.setdefault(b["ts"] // bucket_ms, []).append(b)
    out = []
    keys = sorted(groups)
    if keys and as_of_ms is not None:
        last_close = (keys[-1] + 1) * bucket_ms
        usable_keys = keys if last_close <= as_of_ms else keys[:-1]
    else:
        usable_keys = keys[:-1]            # no clock -> conservative drop
    for k in usable_keys:
        g = sorted(groups[k], key=lambda x: x["ts"])
        if len(g) != factor:
            continue                       # incomplete bucket -> rejected
        expected = [k * bucket_ms + j * BAR_MS for j in range(factor)]
        if [x["ts"] for x in g] != expected:
            continue                       # duplicate/discontinuous/misaligned
        out.append({"ts": k * bucket_ms,
                    "available_at": g[-1].get("available_at", g[-1]["ts"] + BAR_MS),
                    "open": g[0]["open"], "high": max(x["high"] for x in g),
                    "low": min(x["low"] for x in g), "close": g[-1]["close"],
                    "volume": sum(x.get("volume", 0.0) for x in g),
                    "turnover": sum(x.get("turnover", 0.0) for x in g),
                    "symbol": g[0].get("symbol"), "venue": g[0].get("venue")})
    return out


VALID_EXIT_REASONS = ("TP", "SL", "BE_STOP", "TRAIL", "TIME", "TP1")
CENSORED_REASONS = ("END_CENSORED",)
INVALID_EXECUTION_REASONS = ("STALE_EXIT",)


def dataset_quality(bars: list[dict], bar_ms: int | None = None) -> dict[str, Any]:
    """Strict delta==T quality gate over canonical bars (+ OHLC sanity)."""
    from . import public_data_backfill_v10_45_1 as BF
    if not bars:
        return {"quality_pass": False, "reason": "NO_BARS", "n_bars": 0}
    if bar_ms is None:
        diffs = sorted(bars[i + 1]["ts"] - bars[i]["ts"]
                       for i in range(min(500, len(bars) - 1)))
        bar_ms = max(diffs[len(diffs) // 2], BAR_MS) if diffs else BAR_MS
    q = BF.strict_quality([b["ts"] for b in bars], bar_ms=bar_ms)
    invalid_ohlc = sum(
        1 for b in bars
        if not (b["low"] <= min(b["open"], b["close"])
                and b["high"] >= max(b["open"], b["close"])
                and b["low"] <= b["high"] and b["low"] > 0))
    q["invalid_ohlc"] = invalid_ohlc
    q["bar_ms"] = bar_ms
    q["n_bars"] = len(bars)
    q["quality_pass"] = bool(q["quality_pass"] and invalid_ohlc == 0)
    return q


def longest_contiguous_segment(bars: list[dict], bar_ms: int = BAR_MS
                               ) -> list[dict]:
    """Longest strictly-contiguous (delta == bar_ms) slice. Time-based subset
    selection — decided BEFORE any strategy sees the data, so it cannot be
    outcome-biased. Lets the zero-gap promotion rule coexist with real
    exchange maintenance windows."""
    if len(bars) < 2:
        return bars
    best_s = best_e = cur_s = 0
    for i in range(1, len(bars)):
        if bars[i]["ts"] - bars[i - 1]["ts"] != bar_ms:
            if i - 1 - cur_s > best_e - best_s:
                best_s, best_e = cur_s, i - 1
            cur_s = i
    if len(bars) - 1 - cur_s > best_e - best_s:
        best_s, best_e = cur_s, len(bars) - 1
    return bars[best_s:best_e + 1]


def split_indices(n: int) -> dict[str, tuple[int, int]]:
    d_end = int(n * 0.55)
    v_start = d_end + EMBARGO_BARS
    v_end = int(n * 0.775)
    h_start = v_end + EMBARGO_BARS
    return {"discovery": (WARMUP, d_end),
            "validation": (v_start, v_end),
            "holdout": (h_start, n - 1)}


def cost_attribution(bars, feats, spec, i_start, i_end, costs=None,
                     cooldown=None) -> dict[str, Any]:
    """Decompose gross -> net: how much fees, spread, slippage and funding
    each destroy, on the SAME entries/exits logic."""
    base = {**DEFAULT_COSTS, **(costs or {})}
    layers = {
        "gross": {"taker_fee_bps": 0, "spread_bps": 0, "slippage_bps": 0,
                  "funding_bps_per_8h": 0},
        "fees_only": {**base, "spread_bps": 0, "slippage_bps": 0,
                      "funding_bps_per_8h": 0},
        "fees_spread": {**base, "slippage_bps": 0, "funding_bps_per_8h": 0},
        "fees_spread_slip": {**base, "funding_bps_per_8h": 0},
        "net_full": base}
    evs = {}
    for name, cst in layers.items():
        r = replay(bars, feats, spec, costs=cst, i_start=i_start, i_end=i_end,
                   cooldown_override=cooldown)
        evs[name] = metrics(r["trades"])["net_EV"]
    g = evs.get("gross")
    return {"gross_EV": g,
            "fee_impact": (None if g is None or evs["fees_only"] is None
                           else round(evs["fees_only"] - g, 8)),
            "spread_impact": (None if evs["fees_only"] is None or evs["fees_spread"] is None
                              else round(evs["fees_spread"] - evs["fees_only"], 8)),
            "slippage_impact": (None if evs["fees_spread"] is None or evs["fees_spread_slip"] is None
                                else round(evs["fees_spread_slip"] - evs["fees_spread"], 8)),
            "funding_impact": (None if evs["fees_spread_slip"] is None or evs["net_full"] is None
                               else round(evs["net_full"] - evs["fees_spread_slip"], 8)),
            "net_EV": evs.get("net_full")}


def run_funnel(bars: list[dict], feats: list[dict], compiled: list[dict],
               costs: dict | None = None, n_trials_total: int | None = None,
               promotion_allowed: bool = True,
               log=print) -> dict[str, Any]:
    """discovery -> screening (perturbation + light stress) -> validation
    (non-overlap, FULL multiple-testing lb) -> locked holdout -> full cost
    stress. Baselines run BEFORE the gate and are part of the decision.
    Data quality (and download completeness via promotion_allowed) is a hard
    precondition for promotion."""
    n = len(bars)
    seg = split_indices(n)
    d0, d1 = seg["discovery"]
    v0, v1 = seg["validation"]
    h0, h1 = seg["holdout"]
    results: list[dict] = []
    n_universe = len(compiled)
    dq = dataset_quality(bars)
    quality_pass = bool(dq.get("quality_pass")) and bool(promotion_allowed)
    dq["promotion_allowed"] = bool(promotion_allowed)
    # ---------- BASELINES FIRST (same period/dataset/costs/censoring; the
    # SAME compiled policy semantics as candidates — no cooldown override) ---
    base_out: dict[str, Any] = {}
    base_lbs: list[float] = []
    seen_b: set[str] = set()
    for b in baseline_specs():
        stt, cb = compile_strategy(b, seen_b)
        if stt != "OK":
            continue
        rb = replay(bars, feats, cb, costs=costs, i_start=v0, i_end=v1)
        m_b = metrics(rb["trades"])
        base_out[cb["strategy_id"]] = m_b
        if m_b.get("net_EV_lower_bound") is not None:
            base_lbs.append(m_b["net_EV_lower_bound"])
    base_out["baseline_random"] = run_random_baseline(
        bars, feats, v0, v1, costs, freq=0.02)
    if base_out["baseline_random"].get("net_EV_lower_bound") is not None:
        base_lbs.append(base_out["baseline_random"]["net_EV_lower_bound"])
    # buy&hold is a TOTAL RETURN over the window — a different unit than
    # per-trade EV, so it is reported separately and never enters the
    # per-trade baseline comparison
    base_out["baseline_buy_hold_total_return"] = buy_and_hold_net(bars, v0, v1, costs)
    base_out["baseline_no_trade"] = 0.0
    baseline_best_lb = max(base_lbs) if base_lbs else None
    # ---------- DISCOVERY ----------
    survivors = []
    for k, spec in enumerate(compiled):
        r = replay(bars, feats, spec, costs=costs, i_start=d0, i_end=d1)
        m = metrics(r["trades"], n_tests=1)
        entry = {"phase": "discovery", "strategy_id": spec["strategy_id"],
                 "origin": spec.get("origin"), "signature": spec["signature"],
                 "metrics": m}
        if m["n_trades"] < MIN_DISCOVERY_TRADES:
            entry["state"] = "NEED_MORE_DATA"
        elif (m["net_EV"] or 0) <= 0:
            entry["state"] = "REJECTED"
        else:
            entry["state"] = "SURVIVED_DISCOVERY"
            survivors.append(spec)
        ledger_append(entry)
        results.append(entry)
        if (k + 1) % 100 == 0:
            log(f"  discovery {k + 1}/{n_universe} (survivors so far: {len(survivors)})")
    log(f"discovery: {len(survivors)}/{n_universe} survived")
    # ---------- SCREENING (perturbation + light cost stress, discovery data) --
    # every perturbation is a CHILD strategy with its own id, parent link and
    # signature — never a silent in-place mutation of the parent's policy
    screened = []
    perturbation_children = 0
    for spec in survivors:
        perturb_ok = 0
        perturb_total = 0
        for mult_stop in (0.8, 1.2):
            for mult_tp in (0.8, 1.2):
                child = json.loads(json.dumps(spec))
                child["stop"]["value"] *= mult_stop
                child["tp"]["value"] *= mult_tp
                child["strategy_id"] = (f"{spec['strategy_id']}"
                                        f"_p{int(mult_stop*100)}s{int(mult_tp*100)}t")
                child["parent_strategy_id"] = spec["strategy_id"]
                child["signature"] = semantic_signature(child)
                r = replay(bars, feats, child, costs=costs, i_start=d0, i_end=d1)
                m = metrics(r["trades"])
                perturb_total += 1
                perturbation_children += 1
                ledger_append({"phase": "screening_perturbation",
                               "strategy_id": child["strategy_id"],
                               "parent_strategy_id": spec["strategy_id"],
                               "signature": child["signature"],
                               "net_EV": m["net_EV"], "n_trades": m["n_trades"]})
                if (m["net_EV"] or 0) > 0:
                    perturb_ok += 1
        c25 = {**DEFAULT_COSTS, **(costs or {})}
        c25 = {k2: (v2 * 1.25 if k2 != "funding_bps_per_8h" else v2)
               for k2, v2 in c25.items()}
        r25 = replay(bars, feats, spec, costs=c25, i_start=d0, i_end=d1)
        m25 = metrics(r25["trades"])
        ok = perturb_ok >= max(2, int(perturb_total * 0.5)) and (m25["net_EV"] or 0) > 0
        entry = {"phase": "screening", "strategy_id": spec["strategy_id"],
                 "perturb_ok": f"{perturb_ok}/{perturb_total}",
                 "cost25_net_EV": m25["net_EV"],
                 "state": "SURVIVED_SCREENING" if ok else "REJECTED"}
        ledger_append(entry)
        results.append(entry)
        if ok:
            screened.append(spec)
    log(f"screening: {len(screened)}/{len(survivors)} survived")
    # ---------- MULTIPLE TESTING: m computed AUTOMATICALLY from what actually
    # ran and influenced selection (universe + perturbation children + light
    # stress + upcoming validation evaluations), scaled by the number of runs
    # in the sprint (timeframes x symbols). Never a CLI constant.
    m_raw = n_universe + perturbation_children + len(survivors) + len(screened)
    sprint_runs = max(int(n_trials_total or 1), 1) if (n_trials_total or 0) <= 24 \
        else 1   # n_trials_total now carries the SPRINT RUN COUNT (small int)
    m_effective = max(m_raw * sprint_runs, 1)
    replays_run = n_universe + 5 * len(survivors)
    # ---------- VALIDATION (untouched slice; SAME compiled policy — no
    # silent cooldown override; dependence handled by conservative n_eff) ----
    validated = []
    m_val_by_id: dict[str, dict] = {}
    stress_by_id: dict[str, dict] = {}
    matched_by_id: dict[str, dict] = {}
    eligibility_by_id: dict[str, tuple] = {}
    base_c = {**DEFAULT_COSTS, **(costs or {})}
    for spec in screened:
        r = replay(bars, feats, spec, costs=costs, i_start=v0, i_end=v1)
        m = metrics(r["trades"], n_tests=m_effective)
        replays_run += 1
        m_val_by_id[spec["strategy_id"]] = m
        # ---- full cost stress on the VALIDATION slice (pre-holdout, part of
        # eligibility — the holdout can never be the thing that reveals it)
        stress = {}
        variants = {
            "cost_plus_25": {k2: v2 * 1.25 for k2, v2 in base_c.items()},
            "cost_plus_50": {k2: v2 * 1.5 for k2, v2 in base_c.items()},
            "spread_x2": {**base_c, "spread_bps": base_c["spread_bps"] * 2},
            "slip_x2": {**base_c, "slippage_bps": base_c["slippage_bps"] * 2},
        }
        for name, cs in variants.items():
            rs = replay(bars, feats, spec, costs=cs, i_start=v0, i_end=v1)
            stress[name] = metrics(rs["trades"])["net_EV"]
        rl = replay(bars, feats, spec, costs=base_c, i_start=v0, i_end=v1,
                    entry_fill_prob=0.9, extra_entry_slip_bps=2.0, rng_seed=13)
        stress["nonfill10_latency"] = metrics(rl["trades"])["net_EV"]
        stress_ok = all((x or 0) > 0 for x in stress.values())
        stress_by_id[spec["strategy_id"]] = {"stress": stress, "ok": stress_ok}
        replays_run += 5
        # ---- exposure-matched baseline (same n/side/hold/window/costs)
        matched = exposure_matched_baseline(bars, spec, r["trades"], v0, v1,
                                            costs=costs)
        matched_by_id[spec["strategy_id"]] = matched
        eligible, reasons = validation_eligible_for_holdout(
            m, stress_ok, quality_pass, baseline_best_lb,
            matched.get("mean_EV"))
        eligibility_by_id[spec["strategy_id"]] = (eligible, reasons)
        entry = {"phase": "validation", "strategy_id": spec["strategy_id"],
                 "metrics": m, "n_tests_applied": m_effective,
                 "cost_stress": stress, "stress_ok": stress_ok,
                 "exposure_matched_baseline": matched,
                 "holdout_eligible": eligible,
                 "eligibility_reasons": reasons,
                 "state": ("SURVIVED_VALIDATION" if eligible
                           else state_from_reasons(reasons))}
        ledger_append(entry)
        results.append(entry)
        if eligible:
            validated.append(spec)
    log(f"validation: {len(validated)}/{len(screened)} eligible for holdout")
    # ---------- LOCKED HOLDOUT: read ONLY for eligible candidates; every
    # access (and every non-access) is logged with the validation hash -------
    finals = []
    for spec in screened:
        sid = spec["strategy_id"]
        mv = m_val_by_id[sid]
        eligible, reasons = eligibility_by_id[sid]
        stress_info = stress_by_id[sid]
        matched = matched_by_id[sid]
        val_hash = hashlib.sha1(json.dumps(mv, sort_keys=True, default=str)
                                .encode("utf-8")).hexdigest()[:16]
        if not eligible:
            ledger_append({"phase": "holdout_access", "strategy_id": sid,
                           "holdout_accessed": False,
                           "validation_metrics_sha1": val_hash,
                           "reason": "validation_gates_not_passed",
                           "eligibility_reasons": reasons})
            continue
        ledger_append({"phase": "holdout_access", "strategy_id": sid,
                       "holdout_accessed": True,
                       "validation_metrics_sha1": val_hash,
                       "reason": "all_validation_gates_passed"})
        r = replay(bars, feats, spec, costs=costs, i_start=h0, i_end=h1)
        mh = metrics(r["trades"], n_tests=m_effective)
        replays_run += 1
        state = gate(mv, mh, stress_info["ok"], data_quality_pass=quality_pass,
                     baseline_best_lb=baseline_best_lb,
                     matched_baseline_ev=matched.get("mean_EV"))
        attribution = cost_attribution(bars, feats, spec, v0, v1, costs=costs)
        replays_run += 5
        entry = {"phase": "holdout", "strategy_id": sid,
                 "origin": spec.get("origin"),
                 "hypothesis": spec.get("hypothesis"),
                 "validation_metrics": mv, "holdout_metrics": mh,
                 "cost_stress": stress_info["stress"],
                 "stress_ok": stress_info["ok"],
                 "exposure_matched_baseline": matched,
                 "cost_attribution": attribution,
                 "baseline_best_lb": baseline_best_lb,
                 "data_quality_pass": quality_pass,
                 "execution_proxies": list(EXECUTION_PROXIES),
                 "proxy_note": PROXY_NOTE,
                 "state": state}
        ledger_append(entry)
        results.append(entry)
        finals.append(entry)
    # ---------- cost attribution for the BEST candidates even without
    # finalists (so "costs kill the edge" is SHOWN, not asserted) ----------
    attribution_best = None
    if not finals:
        best_spec = None
        if screened:
            best_spec = max(screened,
                            key=lambda s: (m_val_by_id[s["strategy_id"]].get("net_EV")
                                           or -9))
        elif survivors:
            best_spec = survivors[0]
        elif compiled:
            best_spec = compiled[0]
        if best_spec is not None:
            attribution_best = {"strategy_id": best_spec["strategy_id"],
                                **cost_attribution(bars, feats, best_spec,
                                                   v0, v1, costs=costs)}
            replays_run += 5
    # sensitivity of the correction: how the best validation lb moves with m
    mt_sensitivity = None
    if m_val_by_id:
        best_id = max(m_val_by_id, key=lambda k: (m_val_by_id[k].get("net_EV")
                                                  or -9))
        bm = m_val_by_id[best_id]
        if bm.get("net_EV") is not None:
            spec_b = next(s for s in screened if s["strategy_id"] == best_id)
            r1 = replay(bars, feats, spec_b, costs=costs, i_start=v0, i_end=v1)
            mt_sensitivity = {
                "strategy_id": best_id,
                "lb_at_m1": metrics(r1["trades"], n_tests=1)["net_EV_lower_bound"],
                "lb_at_m_raw": metrics(r1["trades"],
                                       n_tests=m_raw)["net_EV_lower_bound"],
                "lb_at_m_effective": bm.get("net_EV_lower_bound"),
                "m_raw": m_raw, "m_effective": m_effective,
                "method": ("m_raw = universe + perturbation children + light "
                           "stress + validation evals (auto-counted); "
                           "m_effective = m_raw x sprint runs")}
            replays_run += 1
    return {"universe": n_universe, "discovery_survivors": len(survivors),
            "screening_survivors": len(screened),
            "validation_survivors": len(validated),
            "finalists": finals, "baselines": base_out,
            "baseline_best_lb": baseline_best_lb,
            "data_quality": dq,
            "m_raw": m_raw, "m_effective": m_effective,
            "n_trials_total": m_effective,
            "replays_run": replays_run,
            "expected_random_survivors_at_5pct": round(0.05 * m_effective, 2),
            "cost_attribution_best": attribution_best,
            "multiple_testing_sensitivity": mt_sensitivity,
            "execution_proxies": list(EXECUTION_PROXIES),
            "splits": {k: v for k, v in seg.items()},
            "results": results}
