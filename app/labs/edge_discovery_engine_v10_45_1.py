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

TOOL_VERSION = "v10.45.6"
OUTPUT_SUBDIR = ("reports", "research", "v10_45_6_edge_discovery")
HOLDOUT_SUBDIR = ("external_data", "staging", "holdout_v10_45_6")
GATE_VERSION = "v10.45.6"
CLUSTER_BLOCK_BARS = 30           # conservative temporal grouping (no event_id)

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
    from . import public_data_backfill_v10_45_1 as BF
    return BF.validated_dir(*OUTPUT_SUBDIR)


def _json_finite(obj):
    """Recursively replace non-finite floats with None so ledger/report JSON
    is always valid and a NaN can never masquerade as a number downstream."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_finite(x) for x in obj]
    return obj


def canonical_sha256(obj) -> str:
    """FULL SHA-256 over canonical JSON: sorted keys, compact separators,
    UTF-8, non-finite floats normalized to null, no volatile fields removed
    here (callers strip them). Any change to any field changes the hash."""
    return hashlib.sha256(json.dumps(_json_finite(obj), sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


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
    ref_bars (another venue) are aligned by open-ts for cross-venue features.
    Malformed input raises ValueError("INVALID_BAR_INPUT: ...") — never an
    AttributeError from deep inside an indicator."""
    _validate_bars_input(bars, "build_features")
    if ref_bars is not None:
        _validate_bars_input(ref_bars, "build_features.ref")
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

def _make_verified_factory():
    """Factory of UNFORGEABLE dataset-verification receipts. The receipt
    class lives inside this closure: external code cannot construct one
    without running the full finiteness scan, so no public argument can
    bypass validation (the old `validated=True` flag is gone)."""

    class _VerifiedDataset:
        __slots__ = ("_bars_id", "_feats_id", "n")

        def __init__(self, bars, feats):
            self._bars_id = id(bars)
            self._feats_id = id(feats)
            self.n = len(bars)

    def verify_finite_dataset(bars: list[dict], feats: list[dict]):
        assert_finite_dataset(bars, feats)     # raises on ANY non-finite
        return _VerifiedDataset(bars, feats)

    def _covers(receipt, bars, feats) -> bool:
        return (isinstance(receipt, _VerifiedDataset)
                and receipt._bars_id == id(bars)
                and receipt._feats_id == id(feats))

    return verify_finite_dataset, _covers


verify_finite_dataset, _receipt_covers = _make_verified_factory()


def _finite_window_ok(bars, feats, spec, i_start, i_end) -> bool:
    """Non-finite guard for one replay window: OHLC of every bar plus every
    feature the spec actually references must be finite. O(range)."""
    used = {c[0] for c in spec.get("conditions", ())}
    if (spec.get("stop", {}).get("type") == "atr"
            or spec.get("tp", {}).get("type") == "atr"
            or spec.get("trail", {}).get("type") == "atr"):
        used.add("atr_14")
    isf = math.isfinite
    for i in range(max(0, i_start), min(i_end + 1, len(bars))):
        b = bars[i]
        if not (isf(b["open"]) and isf(b["high"]) and isf(b["low"])
                and isf(b["close"])):
            return False
        fr = feats[i] if i < len(feats) else {}
        for name in used:
            v = fr.get(name)
            if isinstance(v, float) and not isf(v):
                return False
    return True


def replay(bars: list[dict], feats: list[dict], spec: dict,
           costs: dict | None = None, i_start: int = WARMUP,
           i_end: int | None = None, cooldown_override: int | None = None,
           entry_fill_prob: float = 1.0, extra_entry_slip_bps: float = 0.0,
           rng_seed: int = 7, verified=None) -> dict[str, Any]:
    """Deterministic bar replay of one compiled strategy over [i_start, i_end).

    Contract: signal on bar i close -> entry at bar i+1 open (+slippage+half
    spread). SL beats TP inside the same bar. Partial TP1 closes a tranche at
    tp1 and can move the stop to entry. Trailing stops are computed from
    COMPLETED bars only and applied to the next bar. Gaps: no entry across a
    gap; a gap discovered when the next bar arrives forces a CAUSAL stale
    exit at that next bar's open (STALE_EXIT, excluded from metrics). The
    final open position is closed at the last close and flagged censored."""
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
    # NON-FINITE inputs are refused, never traded. The per-replay scan is
    # skipped ONLY with an unforgeable receipt from verify_finite_dataset
    # covering these exact objects — no public flag can bypass validation.
    if not _receipt_covers(verified, bars, feats) and             not _finite_window_ok(bars, feats, spec, i_start, i_end):
        return {"trades": [], "n_trades": 0, "invalid_bar_fills": 0,
                "ok": False, "status": "INVALID_NON_FINITE_INPUT"}
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
        if not math.isfinite(net):
            # corrupt PnL can never enter the trade list as a number
            net = 0.0
            reason = "INVALID_NON_FINITE"
        trades.append({"entry_i": pos["entry_i"], "exit_i": i,
                       "side": spec["side"], "net_return": round(net, 8),
                       "exit_reason": reason, "bars_held": i - pos["entry_i"],
                       "tranches": len(pos["tranches"]), "censored": censored})
        last_exit_i = i
        pos = None

    invalid_bar_fills = 0                      # structured state, not asserts

    def _clamp_to_range(raw: float, lo: float, hi: float) -> tuple[float, bool]:
        """Production never trusts an assert: an out-of-range fill (corrupt
        bar) is clamped and FLAGGED so the trade is excluded as
        INVALID_EXECUTION downstream."""
        nonlocal invalid_bar_fills
        if not (math.isfinite(raw) and math.isfinite(lo) and math.isfinite(hi)):
            invalid_bar_fills += 1
            safe = lo if math.isfinite(lo) else (hi if math.isfinite(hi) else 0.0)
            return safe, False
        if lo - 1e-9 <= raw <= hi + 1e-9:
            return raw, True
        invalid_bar_fills += 1
        return min(max(raw, lo), hi), False

    def fill_stop_order(stop_px: float, bar_open: float, lo: float, hi: float,
                        long_pos: bool) -> tuple[float, bool]:
        """STOP (market-on-touch) fill. Gap-through -> OPEN or worse; an
        adverse gap can never become profit."""
        if long_pos:                           # stop sells
            raw = bar_open if bar_open <= stop_px else stop_px
        else:                                  # stop buys
            raw = bar_open if bar_open >= stop_px else stop_px
        return _clamp_to_range(raw, lo, hi)

    def fill_limit_take_profit(tp_px: float, bar_open: float, lo: float,
                               hi: float, long_pos: bool) -> tuple[float, bool]:
        """LIMIT take-profit fill. A FAVOURABLE gap fills at the executable
        open (price improvement is real for a resting limit), never below an
        already-better open for LONG (nor above for SHORT), and never outside
        [low, high]. Favourable gaps are normal — no error paths here."""
        if long_pos:                           # TP sells; open above limit = improvement
            raw = bar_open if bar_open >= tp_px else tp_px
        else:                                  # TP buys; open below limit = improvement
            raw = bar_open if bar_open <= tp_px else tp_px
        return _clamp_to_range(raw, lo, hi)

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
                fill, ok_bar = fill_stop_order(stop_px, op, lo, hi, long)
                reason = ("INVALID_BAR_FILL" if not ok_bar
                          else ("TRAIL" if pos.get("trail_moved")
                                else ("BE_STOP" if pos["be_moved"] else "SL")))
                _close_all(fill, reason, i)
            else:
                # 2) partial TP1 (only when the stop was not touched this bar)
                if pos["tp1_px"] is not None:
                    hit1 = (hi >= pos["tp1_px"]) if long else (lo <= pos["tp1_px"])
                    if hit1:
                        fill1, ok_bar = fill_limit_take_profit(
                            pos["tp1_px"], op, lo, hi, long)
                        frac = pos["tp1_frac"]
                        pos["tranches"].append(_tranche_ret(
                            pos["entry_eff"], fill1, frac,
                            i - pos["entry_i"], "TP1"))
                        pos["frac_open"] = round(pos["frac_open"] - frac, 6)
                        pos["tp1_px"] = None
                        if pos["move_be"]:
                            pos["stop_px"] = pos["entry_px"]
                            pos["be_moved"] = True
                            # CONSERVATIVE same-bar resolution: if this bar's
                            # low already reaches the fresh break-even stop,
                            # the remainder exits AT BE in this bar (we never
                            # assume the favourable ordering survived)
                            be_hit = (lo <= pos["stop_px"]) if long \
                                else (hi >= pos["stop_px"])
                            if pos is not None and be_hit:
                                fill_be, ok2 = fill_stop_order(
                                    pos["stop_px"], op, lo, hi, long)
                                _close_all(fill_be,
                                           "BE_STOP" if ok2 else "INVALID_BAR_FILL",
                                           i)
                # 3) final TP
                if pos is not None and pos["tp_px"] is not None:
                    hit_tp = (hi >= pos["tp_px"]) if long else (lo <= pos["tp_px"])
                    if hit_tp:
                        fill_tp, ok_bar = fill_limit_take_profit(
                            pos["tp_px"], op, lo, hi, long)
                        _close_all(fill_tp,
                                   "TP" if ok_bar else "INVALID_BAR_FILL", i)
                # 4) time exit
                if pos is not None and (i - pos["entry_i"]) >= spec["time_exit"]:
                    _close_all(bar["close"], "TIME", i)
                # 5) gap ahead -> CAUSAL stale exit: the gap is only KNOWN
                # when the next bar arrives, so the exit executes at the NEXT
                # available open with the NEXT index — never future time with
                # a past price. If no executable causal price exists the
                # trade is STALE_EXIT_INVALID and stays out of every metric.
                if pos is not None and gap:
                    nxt_open = nxt.get("open")
                    if isinstance(nxt_open, (int, float)) and                             math.isfinite(nxt_open) and nxt_open > 0:
                        _close_all(nxt_open, "STALE_EXIT", i + 1)
                    else:
                        _close_all(pos["entry_px"], "STALE_EXIT_INVALID",
                                   i + 1)
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
    return {"trades": trades, "n_trades": len(trades),
            "invalid_bar_fills": invalid_bar_fills}


# ==========================================================================
# METRICS + GATES
# ==========================================================================

def _metrics_invalid(base: dict, status: str) -> dict[str, Any]:
    return {**base, "ok": False, "status": status,
            "promotion_allowed": False, "degenerate_returns": None,
            "net_EV": None, "net_EV_lower_bound": None,
            "profit_factor": None, "win_rate": None, "max_drawdown": None,
            "outlier_dependence": None, "stability_sign": None, "n_eff": 0}


def metrics(trades: list[dict], n_tests: int = 1) -> dict[str, Any]:
    """EV / PF / win-rate / DD over executable exits only. NEVER raises on
    corrupt data: any non-finite return or malformed trade yields a structured
    ok=false / INVALID_NON_FINITE_INPUT / promotion_allowed=false result."""
    try:
        return _metrics_inner(trades, n_tests)
    except Exception:
        return _metrics_invalid(
            {"n_trades": 0, "n_total_outcomes": len(trades)
             if isinstance(trades, list) else 0,
             "censored": 0, "invalid_execution": 0, "censored_ratio": 0.0},
            "INVALID_NON_FINITE_INPUT")


def _metrics_inner(trades: list[dict], n_tests: int = 1) -> dict[str, Any]:
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
    # ---- FINITENESS FIRST: corrupt PnL can never enter any statistic
    if any(not isinstance(x, (int, float)) or not math.isfinite(x)
           for x in xs):
        return _metrics_invalid(base, "INVALID_NON_FINITE_INPUT")
    if n == 0:
        return {**base, "ok": True, "status": "NO_TRADES",
                "promotion_allowed": False, "degenerate_returns": None,
                "net_EV": None, "net_EV_lower_bound": None,
                "profit_factor": None, "win_rate": None, "max_drawdown": None,
                "outlier_dependence": None, "stability_sign": None,
                "n_eff": 0}
    mean = st.mean(xs)
    sd = st.pstdev(xs) if n > 1 else 0.0
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
    # ---- effective sample size FIRST (the lower bound depends on it).
    # Conservative, documented; two independent shrink factors, harsher wins:
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
    # ---- (c) CLUSTER/EVENT dependence hierarchy:
    #   1. explicit event_id on every trade (same shock/liquidation event),
    #   2. explicit cluster_id on every trade,
    #   3. overlap chains + conservative temporal blocks (fallback).
    # n_eff_final is the CONSERVATIVE MINIMUM of every applicable component.
    ordered = sorted(valid, key=lambda t: t["entry_i"])
    overlap_chains = 0
    chain_end = None
    for t in ordered:
        if chain_end is None or t["entry_i"] > chain_end:
            overlap_chains += 1
            chain_end = t["exit_i"]
        else:
            chain_end = max(chain_end, t["exit_i"])
    n_temporal = len({t["entry_i"] // CLUSTER_BLOCK_BARS for t in valid})
    event_ids = [t.get("event_id") for t in valid]
    cluster_ids = [t.get("cluster_id") for t in valid]
    n_event = len(set(event_ids)) if event_ids and         all(e is not None for e in event_ids) else None
    n_cluster_id = len(set(cluster_ids)) if cluster_ids and         all(c is not None for c in cluster_ids) else None
    fallback_used = n_event is None and n_cluster_id is None
    if n_event is not None:
        cluster_source = "event_id"
    elif n_cluster_id is not None:
        cluster_source = "cluster_id"
    else:
        cluster_source = (f"overlap_chain+temporal_block_{CLUSTER_BLOCK_BARS}"
                          " (no event_id/cluster_id available)")
    components = [overlap_chains, n_temporal]
    if n_event is not None:
        components.append(n_event)
    if n_cluster_id is not None:
        components.append(n_cluster_id)
    n_cluster = max(1, min(components))
    n_overlap = max(1, int(n / occ_factor))
    n_acf = max(1, int(n / acf_factor))
    n_eff_proxy = n < 10                       # too small to estimate honestly
    if n_eff_proxy:
        n_eff = max(1, min(n // 2, n_cluster))  # conservative penalty
        n_eff_method = "N_EFF_PROXY_half_n (sample too small for estimation)"
    else:
        n_eff = max(1, min(n_overlap, n_acf, n_cluster))
        n_eff_method = (f"min(n_overlap={n_overlap}, n_acf={n_acf}, "
                        f"n_cluster={n_cluster}); occupancy={occ_factor:.3f}, "
                        f"acf={acf_factor:.3f}, lags 1-5 positive rho only")
    # ---- DEGENERATE RETURNS: near-zero variance, too few distinct outcomes
    # or a single event/cluster can never count as strong evidence
    unique_returns = len({round(x, 10) for x in xs})
    degenerate = bool((n >= 5 and unique_returns <= 2)
                      or (n > 3 and sd == 0.0)
                      or (n >= 5 and n_cluster == 1))
    # ---- uncertainty uses n_eff, never raw n: dependence widens the interval.
    # (Near-)identical returns collapse sd toward 0, which would yield an
    # overconfident lb == mean ("PF=999 as evidence"). A variance FLOOR keeps
    # the interval honest: identical outcomes cannot claim zero uncertainty.
    sd_eff = sd
    sd_floor_applied = False
    if n > 3 and sd < abs(mean) / 10:
        sd_eff = max(sd, abs(mean) / 2)
        sd_floor_applied = True
    lb = mean - 1.65 * sd_eff / math.sqrt(n_eff) \
        - math.sqrt(max(math.log(max(n_tests, 2)), 0.0)) * sd_eff / math.sqrt(n_eff)
    if not math.isfinite(lb):
        return _metrics_invalid(base, "INVALID_NON_FINITE_INPUT")
    return {**base, "ok": True, "status": "OK",
            "net_EV": round(mean, 8),
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
            "n_eff_is_proxy": n_eff_proxy,
            "sd_floor_applied": sd_floor_applied,
            "degenerate_returns": degenerate,
            "unique_returns": unique_returns,
            "promotion_allowed": not degenerate,
            "n_raw": n,
            "n_overlap": n_overlap,
            "n_acf": n_acf,
            "n_event": n_event,
            "n_cluster": n_cluster,
            "n_cluster_id": n_cluster_id,
            "n_temporal": n_temporal,
            "n_eff_final": n_eff,
            "cluster_source": cluster_source,
            "fallback_used": fallback_used,
            "acf_factor": round(acf_factor, 4),
            "overlap_factor": round(occ_factor, 4),
            "lb_sensitivity_n_vs_neff": {
                "lb_with_n_raw": round(
                    mean - (1.65 + math.sqrt(max(math.log(max(n_tests, 2)), 0.0)))
                    * sd / math.sqrt(max(n, 1)), 8) if sd >= 0 else None,
                "lb_with_n_eff": round(lb, 8)}}


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
    if val_m.get("ok") is False:
        reasons.append("INVALID_NON_FINITE_INPUT")
    if val_m.get("degenerate_returns"):
        reasons.append("DEGENERATE_RETURNS")
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


# ==========================================================================
# PHYSICALLY ISOLATED HOLDOUT: on-disk sealed artifact + HMAC one-use token
#
# THREAT MODEL (honest): this design prevents (a) accidental access, (b)
# access by strategy/pipeline code through normal arguments, (c) early access
# before validation gates, (d) token reuse, (e) cross-strategy and
# cross-descriptor tokens, and (f) tokens surviving a change of metrics,
# registry, dataset, artifact or gate version. There is NO importable raw
# issuer and NO importable signing secret: issuing and validation live inside
# one closure and only issue_if_all_gates_pass / open_with_token are exposed.
# It does NOT defend against a developer with full control of the Python
# process or the filesystem; that adversary is out of scope and no in-process
# scheme can stop them.
# ==========================================================================

HOLDOUT_CONTEXT_BARS = 300        # >= WARMUP so holdout features are valid


def holdout_bindings(*, run_id, sprint_id, strategy_id, compiled_spec_sha,
                     validation_metrics_sha, dataset_generation_id,
                     dataset_sha256, holdout_descriptor_sha, split_hash,
                     registry_sha256) -> dict:
    """Canonical binding set: a token is valid ONLY for this exact tuple."""
    return {"run_id": run_id, "sprint_id": sprint_id,
            "strategy_id": strategy_id,
            "compiled_spec_sha": compiled_spec_sha,
            "validation_metrics_sha": validation_metrics_sha,
            "dataset_generation_id": dataset_generation_id,
            "dataset_sha256": dataset_sha256,
            "holdout_descriptor_sha": holdout_descriptor_sha,
            "split_hash": split_hash,
            "registry_sha256": registry_sha256,
            "gate_version": GATE_VERSION}


def seal_holdout(bars_full: list[dict], ref_full: list[dict] | None,
                 h0: int, h1: int, *, dataset_generation_id: str,
                 dataset_sha256: str, symbol: str, timeframe: str) -> dict:
    """Write the holdout (plus the warmup CONTEXT that precedes it, which is
    already-known discovery/validation data) to a SEPARATE on-disk artifact
    and return an OPAQUE descriptor. The artifact itself embeds its identity
    facts (generation, dataset SHA, split hash, symbol, timeframe) so the
    loader can recompute and enforce them against the SIGNED token payload.
    The descriptor carries no bar values, counts or timestamps."""
    from . import public_data_backfill_v10_45_1 as BF
    ctx0 = max(0, h0 - HOLDOUT_CONTEXT_BARS)
    local_h0 = h0 - ctx0
    local_h1 = h1 - ctx0
    seg_bars = bars_full[ctx0:h1]
    seg_ref = None
    if ref_full:
        last_ts = seg_bars[-1]["ts"] if seg_bars else 0
        first_ts = seg_bars[0]["ts"] if seg_bars else 0
        seg_ref = [r for r in ref_full if first_ts <= r["ts"] <= last_ts]
    split_hash = hashlib.sha256(
        f"{dataset_sha256}|{symbol}|{timeframe}|{h0}|{h1}".encode()).hexdigest()
    dataset_id = hashlib.sha256(
        f"{split_hash}|holdout".encode()).hexdigest()[:24]
    payload = json.dumps({"dataset_id": dataset_id,
                          "generation_id": dataset_generation_id,
                          "dataset_sha256": dataset_sha256,
                          "split_hash": split_hash,
                          "gate_version": GATE_VERSION,
                          "symbol": symbol, "timeframe": timeframe,
                          "local_h0": local_h0, "local_h1": local_h1,
                          "bars": _json_finite(seg_bars),
                          "ref": _json_finite(seg_ref)},
                         default=str).encode("utf-8")
    d = BF.validated_dir(*HOLDOUT_SUBDIR)
    artifact_sha = BF.safe_atomic_write(d / f"{dataset_id}.json", payload)
    return {"sealed": True, "content": "opaque",
            "dataset_id": dataset_id,
            "generation_id": dataset_generation_id,
            "descriptor_sha256": artifact_sha,
            "split_hash": split_hash,
            "expected_dataset_sha256": dataset_sha256}


def _make_holdout_gateway():
    """Factory of the ONLY two holdout entry points. The HMAC key, the nonce
    ledger and the raw issue/redeem primitives are closure-local: no module
    attribute can mint or validate a token."""
    import hmac as _hmac
    _key = os.urandom(32)
    _issued: set = set()
    _consumed: set = set()

    def _mac(payload: dict) -> str:
        return _hmac.new(_key, json.dumps(payload, sort_keys=True,
                                          separators=(",", ":"),
                                          default=str).encode(),
                         hashlib.sha256).hexdigest()

    def issue_if_all_gates_pass(strategy_id: str, val_m: dict | None,
                                stress_ok: bool, data_quality_pass: bool,
                                baseline_best_lb: float | None,
                                matched_baseline_ev: float | None,
                                execution_proxies: tuple = EXECUTION_PROXIES,
                                bindings: dict | None = None,
                                registry_closed: bool | None = None,
                                trial_registered: bool | None = None,
                                dataset_verified: bool | None = None,
                                ttl_s: int = 900
                                ) -> tuple[dict | None, list[str]]:
        """The ONLY factory of holdout access. Every gate must pass HERE —
        there is no lower-level issuer to call around it."""
        reasons: list[str] = []
        if execution_proxies:
            reasons.append("EXECUTION_PROXIES_BLOCK_HOLDOUT_ACCESS")
        if baseline_best_lb is None:
            reasons.append("BASELINES_MISSING")
        if matched_baseline_ev is None:
            reasons.append("MATCHED_BASELINE_MISSING")
        if registry_closed is None:
            registry_closed = bool(RUN_CONTEXT.get("sprint_id")) and \
                registry_is_closed(RUN_CONTEXT.get("sprint_id"))
        if not registry_closed:
            reasons.append("REGISTRY_NOT_CLOSED")
        if trial_registered is None:
            trial_registered = bool(RUN_CONTEXT.get("registry_sha_at_close"))
        if not trial_registered:
            reasons.append("TRIAL_NOT_REGISTERED")
        if dataset_verified is None:
            dataset_verified = RUN_CONTEXT.get("dataset_verify_status") \
                == "DATASET_VERIFIED"
        if not dataset_verified:
            reasons.append("DATASET_NOT_VERIFIED")
        ok, more = validation_eligible_for_holdout(
            val_m, stress_ok, data_quality_pass, baseline_best_lb,
            matched_baseline_ev, execution_proxies=())
        reasons.extend(more)
        if reasons:
            return None, reasons
        if bindings is None:
            reasons.append("BINDINGS_MISSING")
            return None, reasons
        now = datetime.now(timezone.utc).timestamp()
        nonce = os.urandom(16).hex()
        payload = {**bindings, "nonce": nonce,
                   "issued_at_ms": int(now * 1000),
                   "expires_at_ms": int((now + ttl_s) * 1000),
                   "gate_version": GATE_VERSION,
                   "scope": "holdout_replay_once"}
        _issued.add(nonce)
        return {"payload": payload, "hmac": _mac(payload)}, []

    def open_with_token(descriptor: dict, token, expected: dict
                        ) -> tuple[list[dict], list[dict], int, int]:
        """Dedicated holdout loader. Validates the HMAC, consumes the nonce
        BEFORE loading anything, then RECOMPUTES every descriptor fact from
        the artifact on disk (artifact SHA, generation, dataset SHA, split
        hash, symbol, timeframe, gate version) and compares each against the
        SIGNED payload — caller-supplied dicts are never the source of truth.
        `expected` is only checked against the signed payload for the
        caller-context fields (run/sprint/strategy/spec/metrics)."""
        from . import public_data_backfill_v10_45_1 as BF
        if not isinstance(token, dict) or "payload" not in token \
                or "hmac" not in token:
            raise PermissionError("HOLDOUT_LOCKED: no valid token presented")
        payload = token["payload"]
        if not isinstance(payload, dict) or \
                not _hmac.compare_digest(_mac(payload),
                                         str(token.get("hmac"))):
            raise PermissionError("HOLDOUT_LOCKED: token HMAC invalid "
                                  "(forged or payload modified)")
        nonce = payload.get("nonce")
        if nonce in _consumed:
            raise PermissionError("HOLDOUT_LOCKED: token already consumed "
                                  "(one-use)")
        if nonce not in _issued:
            raise PermissionError("HOLDOUT_LOCKED: unknown token nonce")
        if payload.get("gate_version") != GATE_VERSION:
            raise PermissionError("HOLDOUT_LOCKED: gate version mismatch")
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if now_ms > int(payload.get("expires_at_ms") or 0):
            raise PermissionError("HOLDOUT_LOCKED: token expired")
        for k in ("run_id", "sprint_id", "strategy_id", "compiled_spec_sha",
                  "validation_metrics_sha", "registry_sha256"):
            if (expected or {}).get(k) != payload.get(k):
                raise PermissionError(f"HOLDOUT_LOCKED: signed payload bound "
                                      f"to different {k}")
        live_reg = registry_sha()
        if payload.get("registry_sha256") != live_reg:
            raise PermissionError("HOLDOUT_LOCKED: registry changed after "
                                  "token issuance")
        _consumed.add(nonce)                  # consume BEFORE any load
        _issued.discard(nonce)
        if not isinstance(descriptor, dict) or not descriptor.get("dataset_id"):
            raise PermissionError("HOLDOUT_LOCKED: invalid descriptor")
        d = BF.validated_dir(*HOLDOUT_SUBDIR)
        p = d / f"{descriptor['dataset_id']}.json"
        if not p.is_file():
            raise PermissionError("HOLDOUT_LOCKED: sealed artifact missing")
        raw = p.read_bytes()
        artifact_sha = hashlib.sha256(raw).hexdigest()
        # ---- RECOMPUTED facts vs SIGNED payload (never caller dicts) ------
        if artifact_sha != payload.get("holdout_descriptor_sha"):
            raise PermissionError("HOLDOUT_LOCKED: token signed for a "
                                  "different sealed artifact")
        if artifact_sha != descriptor.get("descriptor_sha256"):
            raise PermissionError("HOLDOUT_LOCKED: descriptor does not match "
                                  "the artifact on disk")
        obj = json.loads(raw.decode("utf-8"))
        facts = (("generation_id", "dataset_generation_id"),
                 ("dataset_sha256", "dataset_sha256"),
                 ("split_hash", "split_hash"),
                 ("gate_version", "gate_version"))
        for art_k, pay_k in facts:
            if obj.get(art_k) != payload.get(pay_k):
                raise PermissionError(f"HOLDOUT_LOCKED: artifact {art_k} "
                                      "does not match the signed payload")
        bars_ctx = obj["bars"]
        feats_ctx = build_features(bars_ctx, ref_bars=obj.get("ref"))
        ledger_append({"phase": "holdout_open", "dataset_id": obj["dataset_id"],
                       "strategy_id": payload.get("strategy_id"),
                       "holdout_accessed": True,
                       "token_nonce": nonce,
                       "validation_metrics_sha": payload.get(
                           "validation_metrics_sha")})
        return bars_ctx, feats_ctx, int(obj["local_h0"]), int(obj["local_h1"])

    return issue_if_all_gates_pass, open_with_token


issue_if_all_gates_pass, open_with_token = _make_holdout_gateway()


# ==========================================================================
# GLOBAL MULTIPLE-TESTING REGISTRY (persistent, per sprint)
# ==========================================================================

REGISTRY_FILE = "global_experiment_registry_v10_45_6.jsonl"


class RegistryError(ValueError):
    def __init__(self, status: str, detail: str = ""):
        super().__init__(f"{status}: {detail}" if detail else status)
        self.status = status


_REGISTRY_KINDS = ("sprint_open", "trial", "sprint_close", "single_run",
                   "sprint_member", "sprint_member_invalid", "note")


def _registry_records() -> list[dict]:
    """STRICT registry parser. Any corruption is fatal for the whole
    registry: empty interior lines, invalid JSON, schema violations,
    duplicate member ids, invalid OPEN/CLOSE sequences and truncation all
    RAISE — a corrupt registry can never quietly admit late trials."""
    p = _out() / REGISTRY_FILE
    if not p.is_file():
        return []
    raw = p.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise RegistryError("REGISTRY_TRUNCATED",
                            "last record has no terminating newline")
    out: list[dict] = []
    opened: set = set()
    closed: set = set()
    member_ids: set = set()
    for i, line in enumerate(raw.decode("utf-8").split("\n")[:-1], start=1):
        if not line.strip():
            raise RegistryError("REGISTRY_CORRUPT", f"empty line {i}")
        try:
            e = json.loads(line)
        except Exception:
            raise RegistryError("REGISTRY_CORRUPT",
                                f"line {i}: invalid JSON")
        if not isinstance(e, dict) or not isinstance(e.get("at"), str)                 or e.get("kind") not in _REGISTRY_KINDS:
            raise RegistryError("REGISTRY_SCHEMA_INVALID", f"line {i}")
        kind = e["kind"]
        sid = e.get("sprint_id")
        if kind == "sprint_open":
            if not sid:
                raise RegistryError("REGISTRY_SCHEMA_INVALID",
                                    f"line {i}: open without sprint_id")
            if sid in opened or sid in closed:
                raise RegistryError("REGISTRY_SEQUENCE_INVALID",
                                    f"line {i}: duplicate open {sid}")
            opened.add(sid)
        elif kind == "trial":
            if not sid or not e.get("member_id"):
                raise RegistryError("REGISTRY_SCHEMA_INVALID",
                                    f"line {i}: trial without ids")
            if sid not in opened:
                raise RegistryError("REGISTRY_SEQUENCE_INVALID",
                                    f"line {i}: trial before open")
            if sid in closed:
                raise RegistryError("REGISTRY_SEQUENCE_INVALID",
                                    f"line {i}: trial after close")
            key = (sid, e["member_id"])
            if key in member_ids:
                raise RegistryError("REGISTRY_SEQUENCE_INVALID",
                                    f"line {i}: duplicate member")
            member_ids.add(key)
        elif kind == "sprint_close":
            if not sid or not isinstance(e.get("m_global"), int):
                raise RegistryError("REGISTRY_SCHEMA_INVALID",
                                    f"line {i}: close without m_global")
            if sid in closed:
                raise RegistryError("REGISTRY_SEQUENCE_INVALID",
                                    f"line {i}: duplicate close {sid}")
            if sid not in opened:
                raise RegistryError("REGISTRY_SEQUENCE_INVALID",
                                    f"line {i}: close before open")
            closed.add(sid)
        out.append(e)
    return out


def _registry_closed_sprints() -> set:
    return {e.get("sprint_id") for e in _registry_records()
            if e.get("kind") == "sprint_close"}


class _RegistryLock:
    """Exclusive-create lock file: two concurrent writers can never interleave
    registry records. O_CREAT|O_EXCL is atomic on NTFS and POSIX."""

    def __init__(self, timeout_s: float = 10.0):
        self._path = _out() / (REGISTRY_FILE + ".lock")
        self._timeout = timeout_s
        self._fd = None

    def __enter__(self):
        import time as _t
        deadline = _t.monotonic() + self._timeout
        while True:
            try:
                self._fd = os.open(self._path,
                                   os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                if _t.monotonic() > deadline:
                    raise TimeoutError("registry lock busy")
                _t.sleep(0.05)

    def __exit__(self, *exc):
        try:
            if self._fd is not None:
                os.close(self._fd)
        finally:
            try:
                os.unlink(self._path)
            except OSError:
                pass


def _registry_write(entry: dict) -> None:
    p = _out() / REGISTRY_FILE
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _now(), **_json_finite(entry)},
                           default=str) + "\n")


def registry_append(entry: dict) -> None:
    """Append-only registry with a hard CLOSE: once a sprint_close record
    exists for a sprint_id, any further trial registration for that sprint
    RAISES — trials can never be added silently after the statistical total
    was fixed."""
    with _RegistryLock():
        sid = entry.get("sprint_id")
        if sid and entry.get("kind") != "sprint_close" \
                and sid in _registry_closed_sprints():
            raise ValueError(f"REGISTRY_CLOSED: sprint {sid} already closed; "
                             "no new trials can be registered")
        _registry_write(entry)


def trial_member_id(member: dict) -> str:
    """Content-addressed identity of ONE pre-registered trial."""
    keys = ("symbol", "timeframe", "side", "strategy_id", "signature",
            "kind", "variant", "parent_signature", "seed", "model",
            "prompt_sha", "origin")
    canon = {k: member.get(k) for k in keys}
    return hashlib.sha256(json.dumps(canon, sort_keys=True,
                                     default=str).encode()).hexdigest()[:24]


def registry_open(sprint_id: str, members: list[dict]) -> dict[str, Any]:
    """PHASE 1 of the two-phase registry: pre-register EVERY definitive trial
    (strategies, perturbations, screening/stress evaluations, baselines,
    seeds, models, prompts) BEFORE any discovery replay runs. Duplicate
    members, an already-open or already-closed sprint, or an empty member
    list all raise."""
    if not members:
        raise ValueError("REGISTRY_OPEN_EMPTY: no trials to pre-register")
    ids = [trial_member_id(m) for m in members]
    if len(set(ids)) != len(ids):
        dup = len(ids) - len(set(ids))
        raise ValueError(f"REGISTRY_DUPLICATE_MEMBERS: {dup} duplicates")
    with _RegistryLock():
        for r in _registry_records():
            if r.get("sprint_id") == sprint_id and \
                    r.get("kind") in ("sprint_open", "sprint_close"):
                raise ValueError(f"REGISTRY_ALREADY_{'CLOSED' if r['kind'] == 'sprint_close' else 'OPEN'}: {sprint_id}")
        _registry_write({"kind": "sprint_open", "sprint_id": sprint_id,
                         "state": "OPEN", "n_members": len(members)})
        for m, mid in zip(members, ids):
            _registry_write({**m, "trial_kind": m.get("kind"),
                             "kind": "trial", "sprint_id": sprint_id,
                             "member_id": mid})
    return {"sprint_id": sprint_id, "n_members": len(members),
            "member_ids": set(ids)}


def registry_close(sprint_id: str, m_global: int, run_ids: list) -> dict:
    """PHASE 2: statistical CLOSE. Requires an OPEN sprint with members,
    refuses a second close, and refuses any m_global that does not equal the
    unique pre-registered member count. Returns the frozen totals + SHA."""
    with _RegistryLock():
        recs = _registry_records()
        opened = any(r.get("kind") == "sprint_open"
                     and r.get("sprint_id") == sprint_id for r in recs)
        members = [r for r in recs if r.get("kind") == "trial"
                   and r.get("sprint_id") == sprint_id]
        if any(r.get("kind") == "sprint_close"
               and r.get("sprint_id") == sprint_id for r in recs):
            raise ValueError(f"REGISTRY_DOUBLE_CLOSE: {sprint_id}")
        if not opened or not members:
            raise ValueError(f"REGISTRY_CLOSE_WITHOUT_MEMBERS: {sprint_id}")
        unique = len({r.get("member_id") for r in members})
        if int(m_global) != unique:
            raise ValueError(f"REGISTRY_M_MISMATCH: m_global={m_global} but "
                             f"unique members={unique}")
        _registry_write({"kind": "sprint_close", "sprint_id": sprint_id,
                         "m_global_raw": unique, "m_global_effective": unique,
                         "m_method": ("unique pre-registered trials; no "
                                      "dependence discount (conservative)"),
                         "m_global": unique, "run_ids": run_ids,
                         "state": "CLOSED"})
    return {"sprint_id": sprint_id, "m_global": unique,
            "registry_sha256": registry_sha()}


def registry_members(sprint_id: str) -> set[str]:
    return {r.get("member_id") for r in _registry_records()
            if r.get("kind") == "trial" and r.get("sprint_id") == sprint_id}


def registry_is_closed(sprint_id: str) -> bool:
    return sprint_id in _registry_closed_sprints()


def registry_sha() -> str | None:
    p = _out() / REGISTRY_FILE
    if not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()[:32]


def state_from_reasons(reasons: list[str]) -> str:
    """Deterministic state for a candidate that did NOT reach the holdout."""
    if "DATA_QUALITY_FAIL" in reasons:
        return "INVALID_DATA"
    sample = {"N_TOO_SMALL", "N_EFF_TOO_SMALL", "CENSORED_EXCESSIVE",
              "NO_VALIDATION_METRICS", "N_EFF_PROXY", "DEGENERATE_RETURNS",
              "INVALID_NON_FINITE_INPUT"}
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


BASELINE_TOLERANCES = {"overlap_error": 0, "cluster_error": 0,
                       "session_error": 0, "hold_error": 0,
                       "exposure_error": 0, "censoring_error": 0}


def _overlap_pairs(intervals: list[tuple[int, int]]) -> int:
    n_p = 0
    xs = sorted(intervals)
    for a in range(len(xs)):
        for b in range(a + 1, len(xs)):
            if xs[b][0] <= xs[a][1]:
                n_p += 1
            else:
                break
    return n_p


def exposure_matched_baseline(bars: list[dict], spec: dict, val_trades: list[dict],
                              i0: int, i1: int, costs: dict | None = None,
                              n_seeds: int = 20) -> dict[str, Any]:
    """Baseline matched on the candidate's FULL exposure structure. The
    candidate's trades are grouped into OVERLAP CLUSTERS and each cluster is
    translated RIGIDLY (identical internal offsets and holds) to a random
    position inside the same session type: count, side, the complete holding
    distribution, the overlap GRAPH, cluster allocation, censoring treatment,
    costs and period are preserved exactly — only the cluster timing is
    randomized. Per-seed placement errors are measured against PREREGISTERED
    tolerances (all zero); any miss returns BASELINE_INCOMPLETE and blocks
    the holdout. Corrupt inputs return INVALID_NON_FINITE_INPUT."""
    try:
        return _exposure_matched_inner(bars, spec, val_trades, i0, i1,
                                       costs, n_seeds)
    except Exception:
        return {"status": "INVALID_NON_FINITE_INPUT", "mean_EV": None,
                "holdout_blocked": True}


def _exposure_matched_inner(bars, spec, val_trades, i0, i1, costs, n_seeds):
    valid = [t for t in val_trades if t["exit_reason"] in VALID_EXIT_REASONS
             and not t.get("censored")]
    if not valid:
        return {"status": "NO_TRADES", "mean_EV": None}
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

    def _session(ts: int) -> str:
        hour = int((ts // 3_600_000) % 24)
        return "ASIA" if hour < 8 else ("EU" if hour < 14 else "US")

    # ---- candidate structure: overlap clusters with rigid internal offsets
    ordered = sorted(valid, key=lambda t: t["entry_i"])
    clusters: list[list[dict]] = []
    chain_end = None
    for t in ordered:
        if chain_end is None or t["entry_i"] > chain_end:
            clusters.append([t])
            chain_end = t["exit_i"]
        else:
            clusters[-1].append(t)
            chain_end = max(chain_end, t["exit_i"])
    cand_intervals = [(t["entry_i"], t["exit_i"]) for t in ordered]
    cand_overlap_pairs = _overlap_pairs(cand_intervals)
    requested = len(ordered)
    total_hold = sum(max(1, t["bars_held"]) for t in ordered)
    by_session: dict[str, list[int]] = {"ASIA": [], "EU": [], "US": []}
    for i in range(i0, max(i0 + 1, i1 - 2)):
        by_session[_session(bars[i]["ts"])].append(i)
    means: list[float] = []
    placed_counts: list[int] = []
    per_seed: list[dict] = []
    err_max = {k: 0 for k in BASELINE_TOLERANCES}
    for seed in range(n_seeds):
        rng = random.Random(10_000 + seed)
        used_ranges: list[tuple[int, int]] = []
        placed: list[tuple[int, int]] = []      # (entry_i, hold)
        placed_sessions_ok = 0
        rets: list[float] = []
        reject: dict[str, int] = {}
        clusters_placed = 0
        for cl in clusters:
            c0 = cl[0]["entry_i"]
            pattern = [(t["entry_i"] - c0, max(1, t["bars_held"])) for t in cl]
            span = max(off + hold for off, hold in pattern)
            sess = _session(bars[min(c0, len(bars) - 1)]["ts"])
            pool = by_session.get(sess) or []
            ok_base = None
            for _ in range(300):
                if not pool:
                    reject["NO_POOL_FOR_SESSION"] = \
                        reject.get("NO_POOL_FOR_SESSION", 0) + 1
                    break
                base = pool[rng.randrange(len(pool))]
                if base + 1 + span >= i1:
                    continue
                if any(not (base + span < a or base > b)
                       for a, b in used_ranges):
                    continue                    # keep clusters disjoint
                cont = all(bars[j + 1]["ts"] - bars[j]["ts"] == interval
                           for j in range(base,
                                          min(base + span + 1, len(bars) - 1)))
                if not cont:
                    continue
                ok_base = base
                break
            if ok_base is None:
                reject["NO_FREE_WINDOW"] = reject.get("NO_FREE_WINDOW", 0) \
                    + len(cl)
                continue
            clusters_placed += 1
            used_ranges.append((ok_base, ok_base + span))
            for off, hold in pattern:
                e_i = ok_base + off
                e = bars[e_i + 1]["open"] * (1 + per_side) if long \
                    else bars[e_i + 1]["open"] * (1 - per_side)
                xr = bars[e_i + 1 + hold]["close"] * (1 - per_side) if long \
                    else bars[e_i + 1 + hold]["close"] * (1 + per_side)
                g = (xr / e - 1.0) if long else (e / xr - 1.0)
                rets.append(g - fund * hold)
                placed.append((e_i, hold))
                if _session(bars[e_i]["ts"]) == sess:
                    placed_sessions_ok += 1
        errors = {
            "overlap_error": abs(_overlap_pairs(
                [(p, p + hd) for p, hd in placed]) - cand_overlap_pairs)
            if len(placed) == requested else requested,
            "cluster_error": abs(clusters_placed - len(clusters)),
            "session_error": len(placed) - placed_sessions_ok,
            "hold_error": abs(sum(hd for _, hd in placed) - total_hold)
            if len(placed) == requested else total_hold,
            "exposure_error": abs(sum(hd for _, hd in placed) - total_hold),
            "censoring_error": 0}   # candidate censored trades are excluded
                                    # from EV on BOTH sides identically
        seed_ok = (len(placed) == requested and all(
            errors[k] <= BASELINE_TOLERANCES[k] for k in BASELINE_TOLERANCES))
        per_seed.append({"seed": 10_000 + seed, "requested": requested,
                         "placed": len(placed),
                         "rejected": requested - len(placed),
                         "rejection_reasons": reject, **errors,
                         "seed_ok": seed_ok})
        for k in err_max:
            err_max[k] = max(err_max[k], errors[k])
        if rets:
            means.append(st.mean(rets))
            placed_counts.append(len(placed))
    bad = [d for d in per_seed if not d["seed_ok"]]
    if bad:
        return {"status": "BASELINE_INCOMPLETE", "mean_EV": None,
                "requested": requested,
                "min_placed": min((d["placed"] for d in per_seed), default=0),
                "mean_placed_per_seed": round(st.mean(placed_counts), 2)
                if placed_counts else 0.0,
                "seeds_incomplete": len(bad), "n_seeds": n_seeds,
                "errors_max": err_max, "tolerances": BASELINE_TOLERANCES,
                "per_seed": per_seed[:5], "side": spec["side"],
                "holdout_blocked": True}
    if not means:
        return {"status": "NO_VALID_WINDOWS", "mean_EV": None,
                "per_seed": per_seed[:5], "holdout_blocked": True}
    means_sorted = sorted(means)

    def pct(p: float) -> float:
        return round(means_sorted[min(len(means_sorted) - 1,
                                      int(p * len(means_sorted)))], 8)
    sd_seeds = st.pstdev(means) if len(means) > 1 else 0.0
    return {"status": "OK", "mean_EV": round(st.mean(means), 8),
            "sd_across_seeds": round(sd_seeds, 8),
            "p25": pct(0.25), "p50": pct(0.50), "p75": pct(0.75),
            "upper_bound": round(st.mean(means) + 1.65 * sd_seeds, 8),
            "lower_bound": round(st.mean(means) - 1.65 * sd_seeds, 8),
            "n_seeds": len(means), "matched_entries": requested,
            "per_seed": per_seed[:5],
            "mean_placed_per_seed": round(st.mean(placed_counts), 2),
            "errors_max": err_max, "tolerances": BASELINE_TOLERANCES,
            "clusters_matched": len(clusters),
            "overlap_pairs_matched": cand_overlap_pairs,
            "hold_distribution_matched": True,
            "sessions_matched": True,
            "no_duplicate_timestamps": True,
            "side": spec["side"]}


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

RUNNER_VERSION = "edge_discovery_v10_45_6"
_PROVENANCE_FILES = ("edge_discovery_engine_v10_45_1.py",
                     "multi_ai_orchestrator_v10_45_1.py",
                     "public_data_backfill_v10_45_1.py",
                     "ai_providers_v10_45_1.py")


def _git(*args) -> str | None:
    import subprocess
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True,
                             timeout=10, cwd=str(CE._repo_root()))
        return out.stdout.strip() or None
    except Exception:
        return None


def code_identity() -> dict[str, Any]:
    """Code identity that is STABLE across LF/CRLF checkouts, git archive and
    clean worktrees. Primary identity = Git commit + tree OID + the blob OIDs
    of the relevant sources (git stores LF-normalized content); a secondary
    SEMANTIC hash re-derives the same value from working-tree bytes with line
    endings normalized. Raw working-tree bytes are never the identity."""
    base = Path(__file__).parent
    per_file_semantic = {}
    h = hashlib.sha256()
    for name in _PROVENANCE_FILES:
        try:
            data = (base / name).read_bytes().replace(b"\r\n", b"\n")
        except OSError:
            data = b""
        digest = hashlib.sha256(data).hexdigest()
        per_file_semantic[name] = digest[:16]
        h.update(digest.encode())
    blob_oids = {}
    for name in _PROVENANCE_FILES:
        oid = _git("hash-object", "--path", f"app/labs/{name}",
                   str(base / name))
        if oid:
            blob_oids[name] = oid[:16]
    dirty = None
    st_ = _git("status", "--porcelain", "-uno")
    dirty = bool(st_) if st_ is not None else (
        False if _git("rev-parse", "HEAD") else None)
    return {"repo_commit": _git("rev-parse", "HEAD"),
            "git_tree_oid": _git("rev-parse", "HEAD^{tree}"),
            "relevant_blob_oids": blob_oids,
            "semantic_code_hash": h.hexdigest()[:32],
            "code_tree_hash": h.hexdigest()[:32],
            "files": per_file_semantic,
            "runner_version": RUNNER_VERSION, "dirty_worktree": dirty}


def code_tree_hash() -> dict[str, Any]:
    """Back-compatible alias: identity now comes from code_identity()."""
    return code_identity()


def write_output_manifest(manifest_id: str, extra: dict | None = None
                          ) -> dict[str, Any]:
    """Versioned OUTPUT manifest: hashes every published artifact in the
    results directory plus the dataset/registry/ledger/code identity, then
    commits an atomic CURRENT_OUTPUT_MANIFEST pointer {id, sha}. Reports
    reference the manifest by its pre-chosen id (stable before hashing); the
    pointer + seal carry the hash."""
    from . import public_data_backfill_v10_45_1 as BF
    out = _out()
    artifacts = {}
    for p in sorted(out.iterdir()):
        if not p.is_file() or p.name.startswith("output_manifest_")                 or p.name == "CURRENT_OUTPUT_MANIFEST.json"                 or p.name.endswith(".lock") or p.name.endswith(".part"):
            continue
        artifacts[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    ident = code_identity()
    manifest = {"output_manifest_id": manifest_id,
                "tool_version": TOOL_VERSION,
                "created_at": _now(),
                "code": ident,
                "registry_file": REGISTRY_FILE,
                "registry_sha256": registry_sha(),
                "artifacts": artifacts,
                "environment_fingerprint": {
                    "python": ".".join(map(str, __import__("sys")
                                           .version_info[:3])),
                    "platform": __import__("platform").system()},
                "extra": _json_finite(extra or {}),
                "research_only": True, "can_send_real_orders": False,
                "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}
    body = json.dumps(manifest, indent=2, default=str).encode("utf-8")
    sha = BF.safe_atomic_write(out / f"output_manifest_{manifest_id}.json",
                               body)
    BF.safe_atomic_write(out / "CURRENT_OUTPUT_MANIFEST.json",
                         json.dumps({"output_manifest_id": manifest_id,
                                     "output_manifest_sha256": sha},
                                    indent=2).encode("utf-8"))
    return {**manifest, "output_manifest_sha256": sha}


def write_commit_seal(expected_commit: str | None = None,
                      output_manifest_sha: str | None = None
                      ) -> dict[str, Any]:
    """Versioned seal: certifies the CODE identity (commit + tree OID + blob
    OIDs + semantic hash) AND the published artifacts via the output manifest
    hash — never the code alone. `match` is true only when HEAD equals the
    expected commit AND no tracked file is modified."""
    from . import public_data_backfill_v10_45_1 as BF
    ident = code_identity()
    head = ident["repo_commit"]
    dirty_tracked = ident["dirty_worktree"]
    if output_manifest_sha is None:
        ptr = _out() / "CURRENT_OUTPUT_MANIFEST.json"
        if ptr.is_file():
            try:
                output_manifest_sha = json.loads(
                    ptr.read_text(encoding="utf-8")).get(
                        "output_manifest_sha256")
            except Exception:
                output_manifest_sha = None
    seal = {"sealed_at": _now(), "tool_version": TOOL_VERSION,
            "runner_version": RUNNER_VERSION,
            "repo_commit_head": head,
            "expected_commit": expected_commit,
            "git_tree_oid": ident["git_tree_oid"],
            "relevant_blob_oids": ident["relevant_blob_oids"],
            "semantic_code_hash": ident["semantic_code_hash"],
            "files": ident["files"],
            "dirty_tracked_files": dirty_tracked,
            "output_manifest_sha256": output_manifest_sha,
            "certifies": ["code_identity", "output_manifest",
                          "published_artifacts_via_manifest"],
            "ledger_file": "experiment_ledger_v10_45_6.jsonl",
            "registry_file": REGISTRY_FILE,
            "registry_sha256": registry_sha(),
            "match": bool(head and (expected_commit is None or
                                    head == expected_commit)
                          and dirty_tracked is False
                          and output_manifest_sha is not None),
            "research_only": True, "can_send_real_orders": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}
    BF.safe_atomic_write(_out() / "commit_seal_v10_45_6.json",
                         json.dumps(seal, indent=2, default=str)
                         .encode("utf-8"))
    return seal


RUN_CONTEXT: dict[str, Any] = {}     # run_id, commit, dataset shas, splits, costs...


def set_run_context(**kw) -> None:
    RUN_CONTEXT.clear()
    RUN_CONTEXT.update(kw)


LEDGER_FILE = "experiment_ledger_v10_45_6.jsonl"
_LEDGER_TXN: list[dict] | None = None     # active transaction buffer, else None


def _ledger_linkage(entry: dict) -> dict:
    """Explicit member/trial linkage for a ledger row. Every field is either
    populated or the whole block is marked NOT_APPLICABLE — never ambiguous."""
    ctx = RUN_CONTEXT
    sid = entry.get("strategy_id")
    reg = ctx.get("registry_sha_at_close")
    if not reg or not ctx.get("sprint_id"):
        return {"linkage_status": "NOT_APPLICABLE"}
    trial_id = None
    if sid:
        trial_id = hashlib.sha256(
            f"{ctx.get('sprint_id')}|{sid}|{entry.get('phase')}|"
            f"{entry.get('signature')}".encode()).hexdigest()[:24]
    return {"linkage_status": "LINKED" if sid else "NOT_APPLICABLE",
            "trial_id": trial_id,
            "member_id": entry.get("member_id"),
            "strategy_id": sid,
            "timeframe": ctx.get("timeframe"),
            "run_id": ctx.get("run_id"),
            "sprint_id": ctx.get("sprint_id"),
            "registry_sha256": reg,
            "repo_commit": ctx.get("repo_commit"),
            "tree_oid": ctx.get("git_tree_oid"),
            "dataset_generation_id": ctx.get("dataset_generation_id")}


def ledger_begin() -> None:
    """Open a LEDGER TRANSACTION: subsequent ledger_append calls buffer in
    memory instead of touching the file, so a crash mid-run leaves the
    previous official ledger fully intact. Seeds the buffer with any existing
    rows so commit rewrites a complete file."""
    global _LEDGER_TXN
    buf: list[dict] = []
    p = _out() / LEDGER_FILE
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                buf.append(json.loads(line))
            except Exception:
                pass
    _LEDGER_TXN = buf


def ledger_abort() -> None:
    """Discard the transaction buffer; the on-disk ledger is untouched."""
    global _LEDGER_TXN
    _LEDGER_TXN = None


def ledger_commit() -> str | None:
    """Publish the WHOLE buffered ledger atomically (staging temp -> SHA ->
    fsync -> replace). Complete or not published; the previous ledger is
    preserved on any failure. Returns the ledger SHA-256."""
    global _LEDGER_TXN
    from . import public_data_backfill_v10_45_1 as BF
    if _LEDGER_TXN is None:
        return None
    body = ("".join(json.dumps(_json_finite(r), default=str) + chr(10)
                    for r in _LEDGER_TXN)).encode("utf-8")
    sha = BF.safe_atomic_write(_out() / LEDGER_FILE, body)
    _LEDGER_TXN = None
    return sha


def ledger_append(entry: dict) -> None:
    """Append-only, reproducible: every entry carries the full run provenance
    (run_id, commit, dataset SHA, symbol, timeframe, splits, cost config,
    data quality) plus explicit member/trial linkage. Inside a ledger
    transaction rows buffer in memory and are published atomically on commit;
    otherwise the row is appended directly (back-compatible)."""
    row = _json_finite({"at": _now(), **RUN_CONTEXT, **entry,
                        "linkage": _ledger_linkage(entry)})
    if _LEDGER_TXN is not None:
        _LEDGER_TXN.append(row)
        return
    p = _out() / LEDGER_FILE
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + chr(10))


# ==========================================================================
# FUNNEL
# ==========================================================================

def _validate_bars_input(bars, where: str) -> None:
    """Structured input guard: bar sequences must be dicts with finite OHLC.
    Raises ValueError("INVALID_BAR_INPUT: ...") instead of AttributeError."""
    if not isinstance(bars, list):
        raise ValueError(f"INVALID_BAR_INPUT: {where}: not a list")
    for i, b in enumerate(bars):
        if not isinstance(b, dict):
            raise ValueError(f"INVALID_BAR_INPUT: {where}: item {i} not a bar")
        for k in ("ts", "open", "high", "low", "close"):
            v = b.get(k)
            if not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(
                    f"INVALID_BAR_INPUT: {where}: bar {i} field {k}")


def resample_bars(bars: list[dict], factor: int,
                  as_of_ms: int | None = None) -> list[dict]:
    """STRICT aggregation: a factor-minute bar exists ONLY when its bucket
    contains exactly `factor` consecutive, duplicate-free 1m bars aligned to
    the wall-clock boundary. Incomplete (4/5, 14/15), discontinuous or
    misaligned buckets are REJECTED.

    The trailing bucket is kept ONLY when it is complete AND its close has
    already happened relative to an EXPLICIT `as_of_ms`; without an as_of
    clock the trailing bucket is dropped (it may still be open)."""
    _validate_bars_input(bars, "resample_bars")
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
INVALID_EXECUTION_REASONS = ("STALE_EXIT", "STALE_EXIT_INVALID", "INVALID_BAR_FILL", "INVALID_NON_FINITE")


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


def assert_finite_dataset(bars: list[dict], feats: list[dict]) -> None:
    """One-shot NON-FINITE validation for a whole funnel dataset: every OHLC
    value and every numeric feature must be finite. Raises ValueError with a
    structured status; funnel replays then run with validated=True."""
    isf = math.isfinite
    for i, b in enumerate(bars):
        if not (isf(b["open"]) and isf(b["high"]) and isf(b["low"])
                and isf(b["close"]) and isf(b.get("volume", 0.0))):
            raise ValueError(f"INVALID_NON_FINITE_INPUT: bar {i}")
    for i, fr in enumerate(feats):
        for k, v in fr.items():
            if isinstance(v, float) and not isf(v):
                raise ValueError(f"INVALID_NON_FINITE_INPUT: feature {k}@{i}")


def _trial_record(spec: dict, kind: str, symbol: str, timeframe: str,
                  variant: str | None = None,
                  parent_signature: str | None = None,
                  strategy_id: str | None = None,
                  signature: str | None = "AUTO",
                  seed=None) -> dict:
    return {"symbol": symbol, "timeframe": timeframe,
            "side": spec.get("side"),
            "strategy_id": strategy_id or spec.get("strategy_id"),
            "signature": (spec.get("signature")
                          if signature == "AUTO" else signature),
            "kind": kind, "variant": variant,
            "parent_signature": parent_signature, "seed": seed,
            "model": spec.get("model"), "prompt_sha": spec.get("prompt_sha"),
            "origin": spec.get("origin")}


STRESS_VARIANTS = ("cost_plus_25", "cost_plus_50", "spread_x2", "slip_x2",
                   "nonfill10_latency")


def enumerate_trial_members(compiled: list[dict], symbol: str,
                            timeframe: str) -> list[dict]:
    """PHASE-1 enumeration of every definitive trial for one timeframe:
    discovery, all four perturbation children, the screening cost check, the
    validation evaluation, all stress variants, the exposure-matched baseline
    and the shared baselines. Registered BEFORE any replay runs."""
    members: list[dict] = []
    for spec in compiled:
        members.append(_trial_record(spec, "discovery", symbol, timeframe))
        for ms in (0.8, 1.2):
            for mt in (0.8, 1.2):
                members.append(_trial_record(
                    spec, "perturbation", symbol, timeframe,
                    variant=f"stop{int(ms*100)}_tp{int(mt*100)}",
                    parent_signature=spec.get("signature"),
                    strategy_id=(f"{spec['strategy_id']}"
                                 f"_p{int(ms*100)}s{int(mt*100)}t"),
                    signature=None))
        members.append(_trial_record(spec, "screening_cost25", symbol,
                                     timeframe, variant="cost25"))
        members.append(_trial_record(spec, "validation", symbol, timeframe))
        for v in STRESS_VARIANTS:
            members.append(_trial_record(spec, "stress", symbol, timeframe,
                                         variant=v))
        members.append(_trial_record(spec, "baseline_matched", symbol,
                                     timeframe, variant="seeds20",
                                     seed=10_000))
    for b in baseline_specs():
        members.append(_trial_record(b, "baseline", symbol, timeframe,
                                     signature=None))
    members.append(_trial_record({"side": "LONG",
                                  "strategy_id": "baseline_random"},
                                 "baseline_random", symbol, timeframe,
                                 signature=None, seed=99))
    members.append(_trial_record({"side": "LONG",
                                  "strategy_id": "baseline_buy_hold"},
                                 "baseline_buy_hold", symbol, timeframe,
                                 signature=None))
    return members


def _require_registered(reg_members: set, record: dict) -> None:
    mid = trial_member_id(record)
    if mid not in reg_members:
        raise ValueError(f"TRIAL_NOT_REGISTERED: {record.get('kind')}:"
                         f"{record.get('strategy_id')} (registry closed "
                         "before this trial was enumerated)")


def run_funnel_phase_a(bars_dv: list[dict], feats_dv: list[dict],
                       compiled: list[dict], seg: dict,
                       costs: dict | None = None,
                       promotion_allowed: bool = True,
                       log=print) -> dict[str, Any]:
    """Phase A: baselines + discovery + screening on the discovery/validation
    data ONLY (the holdout stays sealed elsewhere). Returns an intermediate
    state whose m_partial feeds the GLOBAL multiple-testing registry before
    any validation lower bound is computed."""
    d0, d1 = seg["discovery"]
    v0, v1 = seg["validation"]
    results: list[dict] = []
    n_universe = len(compiled)
    # ---- TWO-PHASE REGISTRY CONTRACT: no replay before the CLOSE ----------
    sprint_id = RUN_CONTEXT.get("sprint_id")
    if not sprint_id or not registry_is_closed(sprint_id):
        raise ValueError("REGISTRY_NOT_CLOSED: phase A requires every trial "
                         "pre-registered and the sprint registry CLOSED")
    if not RUN_CONTEXT.get("registry_sha_at_close"):
        raise ValueError("LEDGER_WITHOUT_REGISTRY_SHA: the run context must "
                         "pin the closed registry SHA before any trial runs")
    reg_members = registry_members(sprint_id)
    sym_r = RUN_CONTEXT.get("symbol")
    tf_r = RUN_CONTEXT.get("timeframe")
    # ---- NON-FINITE inputs blocked once for the whole funnel --------------
    receipt = verify_finite_dataset(bars_dv, feats_dv)
    dq = dataset_quality(bars_dv)
    quality_pass = bool(dq.get("quality_pass")) and bool(promotion_allowed)
    dq["promotion_allowed"] = bool(promotion_allowed)
    # ---------- BASELINES FIRST (same period/dataset/costs/censoring) -------
    base_out: dict[str, Any] = {}
    base_lbs: list[float] = []
    seen_b: set[str] = set()
    for b in baseline_specs():
        stt, cb = compile_strategy(b, seen_b)
        if stt != "OK":
            continue
        _require_registered(reg_members, _trial_record(
            cb, "baseline", sym_r, tf_r, signature=None))
        rb = replay(bars_dv, feats_dv, cb, costs=costs, i_start=v0, i_end=v1,
                    verified=receipt)
        m_b = metrics(rb["trades"])
        base_out[cb["strategy_id"]] = m_b
        if m_b.get("net_EV_lower_bound") is not None:
            base_lbs.append(m_b["net_EV_lower_bound"])
    _require_registered(reg_members, _trial_record(
        {"side": "LONG", "strategy_id": "baseline_random"}, "baseline_random",
        sym_r, tf_r, signature=None, seed=99))
    base_out["baseline_random"] = run_random_baseline(
        bars_dv, feats_dv, v0, v1, costs, freq=0.02)
    if base_out["baseline_random"].get("net_EV_lower_bound") is not None:
        base_lbs.append(base_out["baseline_random"]["net_EV_lower_bound"])
    _require_registered(reg_members, _trial_record(
        {"side": "LONG", "strategy_id": "baseline_buy_hold"},
        "baseline_buy_hold", sym_r, tf_r, signature=None))
    base_out["baseline_buy_hold_total_return"] = buy_and_hold_net(
        bars_dv, v0, v1, costs)
    base_out["baseline_no_trade"] = 0.0
    baseline_best_lb = max(base_lbs) if base_lbs else None
    # ---------- DISCOVERY ----------
    survivors = []
    for k, spec in enumerate(compiled):
        _require_registered(reg_members, _trial_record(
            spec, "discovery", sym_r, tf_r))
        r = replay(bars_dv, feats_dv, spec, costs=costs, i_start=d0, i_end=d1,
                   verified=receipt)
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
    # ---------- SCREENING (perturbation children + light cost stress) -------
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
                _require_registered(reg_members, _trial_record(
                    spec, "perturbation", sym_r, tf_r,
                    variant=f"stop{int(mult_stop*100)}_tp{int(mult_tp*100)}",
                    parent_signature=spec.get("signature"),
                    strategy_id=child["strategy_id"], signature=None))
                r = replay(bars_dv, feats_dv, child, costs=costs,
                           i_start=d0, i_end=d1, verified=receipt)
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
        _require_registered(reg_members, _trial_record(
            spec, "screening_cost25", sym_r, tf_r, variant="cost25"))
        r25 = replay(bars_dv, feats_dv, spec, costs=c25, i_start=d0, i_end=d1,
                     verified=receipt)
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
    m_partial = n_universe + perturbation_children + len(survivors) + len(screened)
    return {"bars_dv": bars_dv, "feats_dv": feats_dv, "seg": seg,
            "receipt": receipt,
            "costs": costs, "results": results,
            "n_universe": n_universe, "survivors": survivors,
            "screened": screened, "base_out": base_out,
            "baseline_best_lb": baseline_best_lb,
            "dq": dq, "quality_pass": quality_pass,
            "perturbation_children": perturbation_children,
            "m_partial": m_partial,
            "replays_run": n_universe + 5 * len(survivors)}


def run_funnel_phase_b(state: dict, sealed: dict,
                       m_global: int, log=print) -> dict[str, Any]:
    """Phase B: validation with the SPRINT-GLOBAL pre-registered total,
    exposure-matched baselines (exact placement required), HMAC one-use
    token-gated access to the ON-DISK sealed holdout, fail-closed gates and
    cost attribution. `sealed` is the opaque descriptor from seal_holdout."""
    bars_dv, feats_dv = state["bars_dv"], state["feats_dv"]
    seg, costs = state["seg"], state["costs"]
    v0, v1 = seg["validation"]
    results = state["results"]
    screened = state["screened"]
    survivors = state["survivors"]
    n_universe = state["n_universe"]
    quality_pass = state["quality_pass"]
    baseline_best_lb = state["baseline_best_lb"]
    base_out = state["base_out"]
    m_raw = state["m_partial"]
    m_effective = max(int(m_global), m_raw, 1)
    replays_run = state["replays_run"]
    base_c = {**DEFAULT_COSTS, **(costs or {})}
    receipt = state.get("receipt")
    reg_members = registry_members(RUN_CONTEXT.get("sprint_id"))
    sym_r = RUN_CONTEXT.get("symbol")
    tf_r = RUN_CONTEXT.get("timeframe")
    validated = []
    m_val_by_id: dict[str, dict] = {}
    finals = []
    for spec in screened:
        sid = spec["strategy_id"]
        _require_registered(reg_members, _trial_record(
            spec, "validation", sym_r, tf_r))
        r = replay(bars_dv, feats_dv, spec, costs=costs, i_start=v0, i_end=v1,
                   verified=receipt)
        m = metrics(r["trades"], n_tests=m_effective)
        replays_run += 1
        m_val_by_id[sid] = m
        # ---- full cost stress on the VALIDATION slice (pre-holdout)
        stress = {}
        variants = {
            "cost_plus_25": {k2: v2 * 1.25 for k2, v2 in base_c.items()},
            "cost_plus_50": {k2: v2 * 1.5 for k2, v2 in base_c.items()},
            "spread_x2": {**base_c, "spread_bps": base_c["spread_bps"] * 2},
            "slip_x2": {**base_c, "slippage_bps": base_c["slippage_bps"] * 2},
        }
        for name, cs in variants.items():
            _require_registered(reg_members, _trial_record(
                spec, "stress", sym_r, tf_r, variant=name))
            rs = replay(bars_dv, feats_dv, spec, costs=cs, i_start=v0,
                        i_end=v1, verified=receipt)
            stress[name] = metrics(rs["trades"])["net_EV"]
        _require_registered(reg_members, _trial_record(
            spec, "stress", sym_r, tf_r, variant="nonfill10_latency"))
        rl = replay(bars_dv, feats_dv, spec, costs=base_c, i_start=v0, i_end=v1,
                    entry_fill_prob=0.9, extra_entry_slip_bps=2.0, rng_seed=13,
                    verified=receipt)
        stress["nonfill10_latency"] = metrics(rl["trades"])["net_EV"]
        stress_ok = all((x or 0) > 0 for x in stress.values())
        replays_run += 5
        # ---- exposure-matched baseline: EXACT placement or fail-closed
        _require_registered(reg_members, _trial_record(
            spec, "baseline_matched", sym_r, tf_r, variant="seeds20",
            seed=10_000))
        matched = exposure_matched_baseline(bars_dv, spec, r["trades"], v0, v1,
                                            costs=costs)
        matched_ev = matched.get("mean_EV")             if matched.get("status") == "OK" else None
        # merit (proxy-independent) for reporting; token (access) is stricter
        eligible, reasons = validation_eligible_for_holdout(
            m, stress_ok, quality_pass, baseline_best_lb,
            matched_ev, execution_proxies=())
        if matched.get("status") == "BASELINE_INCOMPLETE":
            reasons = ["BASELINE_INCOMPLETE"] + reasons
            eligible = False
        val_hash = canonical_sha256(m)         # FULL SHA-256, canonical JSON
        entry = {"phase": "validation", "strategy_id": sid,
                 "metrics": m, "n_tests_applied": m_effective,
                 "m_raw_this_run": m_raw, "m_global": m_effective,
                 "cost_stress": stress, "stress_ok": stress_ok,
                 "exposure_matched_baseline": matched,
                 "holdout_eligible_on_merit": eligible,
                 "eligibility_reasons": reasons,
                 "state": ("SURVIVED_VALIDATION" if eligible
                           else state_from_reasons(reasons))}
        ledger_append(entry)
        results.append(entry)
        if eligible:
            validated.append(spec)
        # ---- HMAC one-use token bound to the FULL canonical spec, metrics,
        # dataset generation/SHA, artifact, split, registry and gate version
        spec_sha = canonical_sha256(spec)      # the COMPLETE compiled spec
        bindings = holdout_bindings(
            run_id=RUN_CONTEXT.get("run_id"),
            sprint_id=RUN_CONTEXT.get("sprint_id"),
            strategy_id=sid, compiled_spec_sha=spec_sha,
            validation_metrics_sha=val_hash,
            dataset_generation_id=(sealed.get("generation_id")
                                   if isinstance(sealed, dict) else None),
            dataset_sha256=(sealed.get("expected_dataset_sha256")
                            if isinstance(sealed, dict) else None),
            holdout_descriptor_sha=(sealed.get("descriptor_sha256")
                                    if isinstance(sealed, dict) else None),
            split_hash=(sealed.get("split_hash")
                        if isinstance(sealed, dict) else None),
            registry_sha256=RUN_CONTEXT.get("registry_sha_at_close"))
        token, treasons = issue_if_all_gates_pass(
            sid, m, stress_ok, quality_pass, baseline_best_lb,
            matched_ev, bindings=bindings)
        if matched.get("status") == "BASELINE_INCOMPLETE" and token is None:
            treasons = ["BASELINE_INCOMPLETE"] + treasons
        if token is None:
            ledger_append({"phase": "holdout_access", "strategy_id": sid,
                           "holdout_accessed": False,
                           "validation_metrics_sha256": val_hash,
                           "reason": "token_denied",
                           "denial_reasons": treasons})
            continue
        ledger_append({"phase": "holdout_access", "strategy_id": sid,
                       "holdout_accessed": True,
                       "validation_metrics_sha256": val_hash,
                       "compiled_spec_sha256": spec_sha,
                       "token_nonce": token["payload"].get("nonce"),
                       "reason": "all_gates_passed_token_issued"})
        bars_ctx, feats_ctx, h0, h1 = open_with_token(sealed, token, bindings)
        rh = replay(bars_ctx, feats_ctx, spec, costs=costs,
                    i_start=h0, i_end=h1)
        mh = metrics(rh["trades"], n_tests=m_effective)
        replays_run += 1
        state_final = gate(m, mh, stress_ok, data_quality_pass=quality_pass,
                           baseline_best_lb=baseline_best_lb,
                           matched_baseline_ev=matched_ev)
        # holdout-side stability/outlier gates (at least as strict as val)
        if state_final in ("SHADOW_CANDIDATE_RESEARCH_ONLY",
                           "PAPER_CANDIDATE_RESEARCH_ONLY"):
            if mh.get("stability_sign") == 0 or (
                    mh.get("outlier_dependence") is not None
                    and mh["outlier_dependence"] <= 0):
                state_final = "REJECTED"
        attribution = cost_attribution(bars_dv, feats_dv, spec, v0, v1,
                                       costs=costs)
        replays_run += 5
        entry = {"phase": "holdout", "strategy_id": sid,
                 "origin": spec.get("origin"),
                 "hypothesis": spec.get("hypothesis"),
                 "validation_metrics": m, "holdout_metrics": mh,
                 "cost_stress": stress, "stress_ok": stress_ok,
                 "exposure_matched_baseline": matched,
                 "cost_attribution": attribution,
                 "baseline_best_lb": baseline_best_lb,
                 "data_quality_pass": quality_pass,
                 "execution_proxies": list(EXECUTION_PROXIES),
                 "proxy_note": PROXY_NOTE,
                 "state": state_final}
        ledger_append(entry)
        results.append(entry)
        finals.append(entry)
    log(f"validation: {len(validated)}/{len(screened)} pass merit gates; "
        f"holdout accesses: {len(finals)}")
    # ---------- attribution for the BEST candidate even without finalists ----
    attribution_best = None
    if not finals:
        best_spec = None
        if screened:
            best_spec = max(screened,
                            key=lambda s: (m_val_by_id[s["strategy_id"]].get("net_EV")
                                           or -9))
        elif survivors:
            best_spec = survivors[0]
        elif state.get("n_universe"):
            best_spec = None
        if best_spec is not None:
            attribution_best = {"strategy_id": best_spec["strategy_id"],
                                **cost_attribution(bars_dv, feats_dv, best_spec,
                                                   v0, v1, costs=costs)}
            replays_run += 5
    if attribution_best is None and not finals and not screened and not survivors:
        # fall back to the first compiled strategy via discovery results
        disc = [e for e in results if e.get("phase") == "discovery"]
        if disc:
            # re-derive attribution for the least-bad discovery candidate
            best_id = max(disc, key=lambda e: (e["metrics"].get("net_EV") or -9)
                          )["strategy_id"]
            spec_b = None
            for e in results:
                if e.get("strategy_id") == best_id and e.get("phase") == "discovery":
                    spec_b = e
                    break
            # spec objects are not stored in results; attribution for the id is
            # resolved by the caller when needed
            attribution_best = {"strategy_id": best_id, "resolved": False}
    # sensitivity of the correction on the best validation candidate
    mt_sensitivity = None
    if m_val_by_id:
        best_id = max(m_val_by_id, key=lambda k: (m_val_by_id[k].get("net_EV")
                                                  or -9))
        bm = m_val_by_id[best_id]
        if bm.get("net_EV") is not None:
            spec_b = next(s for s in screened if s["strategy_id"] == best_id)
            r1 = replay(bars_dv, feats_dv, spec_b, costs=costs,
                        i_start=v0, i_end=v1)
            mt_sensitivity = {
                "strategy_id": best_id,
                "lb_at_m1": metrics(r1["trades"], n_tests=1)["net_EV_lower_bound"],
                "lb_at_m_raw": metrics(r1["trades"],
                                       n_tests=m_raw)["net_EV_lower_bound"],
                "lb_at_m_global": bm.get("net_EV_lower_bound"),
                "m_raw": m_raw, "m_global": m_effective,
                "method": ("m_raw auto-counted per run (universe + perturbation "
                           "children + light stress + validation evals); "
                           "m_global = registry sum across the whole sprint, "
                           "shared by every timeframe")}
            replays_run += 1
    return {"universe": n_universe, "discovery_survivors": len(survivors),
            "screening_survivors": len(screened),
            "validation_survivors": len(validated),
            "finalists": finals, "baselines": base_out,
            "baseline_best_lb": baseline_best_lb,
            "data_quality": state["dq"],
            "m_raw": m_raw, "m_effective": m_effective,
            "n_trials_total": m_effective,
            "replays_run": replays_run,
            "expected_random_survivors_at_5pct": round(0.05 * m_effective, 2),
            "cost_attribution_best": attribution_best,
            "multiple_testing_sensitivity": mt_sensitivity,
            "execution_proxies": list(EXECUTION_PROXIES),
            "holdout_accesses": len(finals),
            "splits": {k: v for k, v in seg.items()},
            "results": results}


def run_funnel(bars: list[dict], feats: list[dict], compiled: list[dict],
               costs: dict | None = None, n_trials_total: int | None = None,
               promotion_allowed: bool = True,
               log=print) -> dict[str, Any]:
    """Compatibility wrapper honouring the FULL two-phase contract on a
    single dataset: every trial is enumerated and the registry is OPENED
    and CLOSED before phase A runs; the holdout is sealed to an on-disk
    artifact and the full bar list is dropped from the run state."""
    import uuid as _uuid
    seg = split_indices(len(bars))
    v1 = seg["validation"][1]
    h0, h1 = seg["holdout"]
    bars_dv = bars[:v1]
    feats_dv = feats[:v1]
    symbol = RUN_CONTEXT.get("symbol") or bars[0].get("symbol") or "TEST"
    timeframe = RUN_CONTEXT.get("timeframe") or "1m"
    sprint_id = (RUN_CONTEXT.get("sprint_id")
                 or f"adhoc_{_uuid.uuid4().hex[:10]}")
    dataset_sha = RUN_CONTEXT.get("dataset_sha256") or hashlib.sha256(
        f"{bars[0]['ts']}|{bars[-1]['ts']}|{len(bars)}".encode()).hexdigest()
    generation_id = (RUN_CONTEXT.get("dataset_generation_id")
                     or dataset_sha[:16])
    sealed = seal_holdout(bars, None, h0, h1,
                          dataset_generation_id=generation_id,
                          dataset_sha256=dataset_sha,
                          symbol=symbol, timeframe=timeframe)
    del bars                                    # holdout leaves run memory
    members = enumerate_trial_members(compiled, symbol, timeframe)
    registry_open(sprint_id, members)
    closed = registry_close(sprint_id, len(members),
                            [RUN_CONTEXT.get("run_id") or sprint_id])
    RUN_CONTEXT.setdefault("run_id", sprint_id)
    RUN_CONTEXT["sprint_id"] = sprint_id
    RUN_CONTEXT["symbol"] = symbol
    RUN_CONTEXT["timeframe"] = timeframe
    RUN_CONTEXT["dataset_generation_id"] = generation_id
    RUN_CONTEXT["holdout_descriptor_sha"] = sealed["descriptor_sha256"]
    RUN_CONTEXT["registry_sha_at_close"] = closed["registry_sha256"]
    state = run_funnel_phase_a(bars_dv, feats_dv, compiled, seg, costs=costs,
                               promotion_allowed=promotion_allowed, log=log)
    m_global = closed["m_global"]
    if n_trials_total and n_trials_total > m_global:
        m_global = int(n_trials_total)          # caller may only INCREASE m
    out = run_funnel_phase_b(state, sealed, m_global, log=log)
    out["sprint_id"] = sprint_id
    out["registry_sha256"] = closed["registry_sha256"]
    return out
