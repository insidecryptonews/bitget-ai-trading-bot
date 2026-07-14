"""V10.47 executable strategy families P01–P12 + Trend Rider variants
(RESEARCH ONLY, causal). Each family is a decide-function over the causal bar
window that emits a structured DecisionRecord (TRADE side / ABSTAIN) — never a
raw order — and plugs into the SAME EventClock / SimOMS / tournament as V10.46.

Data honesty: families that truly need order-book / OI / funding / liquidation
/ news feeds are marked with a `data` status. When only public OHLCV is
available this run, those families run on **klines-derived proxies** clearly
labelled `PROXY`; a family with no usable public source (P12 news) is
`DATA_NOT_AVAILABLE` (adapter + fixture only, never faked results).
"""

from __future__ import annotations

import math
from typing import Any, Callable

from . import contracts as C

BAR_MS = 60_000


# ---------------------------------------------------------------- indicators
SIG_LOOKBACK = 260   # bounded window: every _sig call is O(SIG_LOOKBACK), not O(history)


def _sig(bars: list[dict]) -> dict:
    """Causal signal bundle from the bar window (last bar = decision bar).
    Only the last SIG_LOOKBACK bars are used, so a call is O(SIG_LOOKBACK)
    regardless of total history length (avoids O(history^2) in a full replay)."""
    if len(bars) > SIG_LOOKBACK:
        bars = bars[-SIG_LOOKBACK:]
    n = len(bars)
    if n < 25:
        return {"ok": False}
    c = [float(b["close"]) for b in bars]
    h = [float(b["high"]) for b in bars]
    lo = [float(b["low"]) for b in bars]
    v = [float(b.get("volume", 0.0)) for b in bars]
    last = c[-1]
    rets = [c[i] / c[i - 1] - 1.0 for i in range(1, n)]
    w = rets[-60:] if len(rets) > 60 else rets
    mean = sum(w) / len(w)
    vol = math.sqrt(sum((r - mean) ** 2 for r in w) / len(w))
    trs = [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
           for i in range(1, n)]
    atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else (sum(trs) / len(trs))
    atr_frac = atr / last if last else 0.0
    # atr percentile over the window
    tr_win = trs[-min(240, len(trs)):]
    atr_pct = (sum(1 for x in tr_win if x <= trs[-1]) / len(tr_win)) if tr_win else 0.5
    don_hi = max(h[-20:-1]) if n > 21 else max(h[:-1])
    don_lo = min(lo[-20:-1]) if n > 21 else min(lo[:-1])
    slope = 0.0
    seg = c[-20:]
    if len(seg) >= 2:
        mx = (len(seg) - 1) / 2
        my = sum(seg) / len(seg)
        num = sum((i - mx) * (seg[i] - my) for i in range(len(seg)))
        den = sum((i - mx) ** 2 for i in range(len(seg)))
        slope = (num / den) / last if den and last else 0.0
    up_frac = sum(1 for r in w if r > 0) / len(w)
    v_recent = sum(v[-5:]) / 5 if n >= 5 else (sum(v) / n)
    v_base = sum(v[-30:]) / min(30, n)
    vol_accel = v_recent / v_base if v_base else 1.0
    # RSI(14)
    gains = [max(0.0, rets[i]) for i in range(len(rets))][-14:]
    losses = [max(0.0, -rets[i]) for i in range(len(rets))][-14:]
    ag = sum(gains) / 14 if gains else 0.0
    al = sum(losses) / 14 if losses else 0.0
    rsi = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    hi_w, lo_w = max(h[-40:]), min(lo[-40:])
    bb = (last - lo_w) / (hi_w - lo_w) if hi_w > lo_w else 0.5
    last_bar = bars[-1]
    body = last_bar["close"] - last_bar["open"]
    lower_wick = min(last_bar["open"], last_bar["close"]) - last_bar["low"]
    upper_wick = last_bar["high"] - max(last_bar["open"], last_bar["close"])
    hour = int((int(bars[-1]["ts"]) // 3_600_000) % 24)
    return {"ok": True, "last": last, "vol": vol, "atr_frac": atr_frac,
            "atr_pct": atr_pct, "don_hi": don_hi, "don_lo": don_lo,
            "slope": slope, "up_frac": up_frac, "vol_accel": vol_accel,
            "rsi": rsi, "bb": bb, "ret_last": rets[-1],
            "ret_5": (c[-1] / c[-6] - 1.0) if n > 6 else 0.0,
            "ret_15": (c[-1] / c[-16] - 1.0) if n > 16 else 0.0,
            "body": body, "lower_wick": lower_wick, "upper_wick": upper_wick,
            "is_funding_hour": hour in (0, 8, 16),
            "move_consumed": bb}


def _mk(action, side, prob, *, symbol, venue, timeframe, event_id, dt, gen_id,
        reason, regime="ANY", spec_hash=None, policy_id=None):
    return C.make("DecisionRecord", symbol=symbol, venue=venue,
                  timeframe=timeframe, event_id=event_id, causal_cutoff_ms=dt,
                  data_generation_id=gen_id, spec_hash=spec_hash,
                  decision_action=action, side=side, reason_codes=[reason],
                  proposals_for=1 if action == "TRADE" else 0,
                  proposals_against=0, calibrated_probability=round(prob, 6),
                  regime=regime, policy_id=policy_id)


# ------------------------------------------------------------------ families
def _p01_trend(s, ctx):        # Trend Rider (structure + slope + persistence)
    if s["slope"] > 0.00005 and s["up_frac"] >= 0.58 and s["rsi"] > 52:
        return "TRADE", "LONG", 0.55
    if s["slope"] < -0.00005 and s["up_frac"] <= 0.42 and s["rsi"] < 48:
        return "TRADE", "SHORT", 0.55
    return "ABSTAIN_LOW_REWARD", "FLAT", 0.5


def _p02_pullback(s, ctx):     # pullback continuation in a trend
    if s["slope"] > 0.00005 and s["ret_5"] < 0 and s["rsi"] < 55 and s["up_frac"] > 0.5:
        return "TRADE", "LONG", 0.54
    if s["slope"] < -0.00005 and s["ret_5"] > 0 and s["rsi"] > 45 and s["up_frac"] < 0.5:
        return "TRADE", "SHORT", 0.54
    return "ABSTAIN_LOW_REWARD", "FLAT", 0.5


def _p03_breakout(s, ctx):     # donchian breakout + volume
    if s["last"] > s["don_hi"] and s["vol_accel"] > 1.3:
        return "TRADE", "LONG", 0.54
    if s["last"] < s["don_lo"] and s["vol_accel"] > 1.3:
        return "TRADE", "SHORT", 0.54
    return "ABSTAIN_LOW_REWARD", "FLAT", 0.5


def _p04_liq_cascade(s, ctx):  # PROXY: continuation after a violent move+vol
    if s["ret_5"] < -0.006 and s["vol_accel"] > 1.8:
        return "TRADE", "SHORT", 0.53
    if s["ret_5"] > 0.006 and s["vol_accel"] > 1.8:
        return "TRADE", "LONG", 0.53
    return "ABSTAIN_LOW_REWARD", "FLAT", 0.5


def _p05_liq_exhaust(s, ctx):  # PROXY: capitulation rebound (wick + spike)
    if s["ret_15"] < -0.012 and s["lower_wick"] > 1.5 * abs(s["body"]) and s["vol_accel"] > 1.5:
        return "TRADE", "LONG", 0.53
    if s["ret_15"] > 0.012 and s["upper_wick"] > 1.5 * abs(s["body"]) and s["vol_accel"] > 1.5:
        return "TRADE", "SHORT", 0.53
    return "ABSTAIN_LOW_REWARD", "FLAT", 0.5


def _p06_absorption(s, ctx):   # PROXY: large volume, small body (absorption)
    if s["vol_accel"] > 2.0 and abs(s["body"]) < 0.2 * (s["atr_frac"] * s["last"] + 1e-9):
        side = "LONG" if s["lower_wick"] > s["upper_wick"] else "SHORT"
        return "TRADE", side, 0.52
    return "ABSTAIN_LOW_REWARD", "FLAT", 0.5


def _p07_cross_venue(s, ctx):  # cross-venue lead/lag (needs ref bars)
    gap = ctx.get("xv_gap")
    if gap is None:
        return "ABSTAIN_DATA_QUALITY", "FLAT", 0.5
    if gap > 0.0008:
        return "TRADE", "LONG", 0.53
    if gap < -0.0008:
        return "TRADE", "SHORT", 0.53
    return "ABSTAIN_LOW_REWARD", "FLAT", 0.5


def _p08_oi_funding(s, ctx):   # PROXY: funding-hour reversion
    if s["is_funding_hour"] and s["ret_15"] > 0.005:
        return "TRADE", "SHORT", 0.52
    if s["is_funding_hour"] and s["ret_15"] < -0.005:
        return "TRADE", "LONG", 0.52
    return "ABSTAIN_LOW_REWARD", "FLAT", 0.5


def _p09_vol_expansion(s, ctx):  # low-vol -> breakout
    if s["atr_pct"] < 0.2 and s["last"] > s["don_hi"]:
        return "TRADE", "LONG", 0.53
    if s["atr_pct"] < 0.2 and s["last"] < s["don_lo"]:
        return "TRADE", "SHORT", 0.53
    return "ABSTAIN_REGIME", "FLAT", 0.5


def _p10_mean_reversion(s, ctx):  # range reversion (bollinger/rsi)
    if abs(s["slope"]) < 0.00003:
        if s["bb"] < 0.1 and s["rsi"] < 32:
            return "TRADE", "LONG", 0.53
        if s["bb"] > 0.9 and s["rsi"] > 68:
            return "TRADE", "SHORT", 0.53
    return "ABSTAIN_REGIME", "FLAT", 0.5


def _p11_crash_short(s, ctx):  # high-vol exhaustion SHORT
    if s["atr_pct"] > 0.85 and s["ret_15"] > 0.01 and s["upper_wick"] > 0.001:
        return "TRADE", "SHORT", 0.53
    return "ABSTAIN_LOW_REWARD", "FLAT", 0.5


def _p12_event_news(s, ctx):   # DATA_NOT_AVAILABLE: no public news feed wired
    return "ABSTAIN_DATA_QUALITY", "FLAT", 0.5


FAMILIES = {
    "P01": {"name": "Trend Rider LONG/SHORT", "fn": _p01_trend,
            "exit": {"stop_frac": 0.008, "tp_frac": 0.012, "time_exit": 20},
            "data": "OHLCV", "status": "IMPLEMENTED"},
    "P02": {"name": "Pullback Continuation", "fn": _p02_pullback,
            "exit": {"stop_frac": 0.006, "tp_frac": 0.010, "time_exit": 20},
            "data": "OHLCV", "status": "IMPLEMENTED"},
    "P03": {"name": "Breakout + Volume/Flow", "fn": _p03_breakout,
            "exit": {"stop_frac": 0.006, "tp_frac": 0.014, "time_exit": 24},
            "data": "OHLCV(+flow proxy)", "status": "IMPLEMENTED"},
    "P04": {"name": "Liquidation Cascade Continuation", "fn": _p04_liq_cascade,
            "exit": {"stop_frac": 0.008, "tp_frac": 0.010, "time_exit": 15},
            "data": "PROXY (no liquidation feed)", "status": "PROXY"},
    "P05": {"name": "Liquidation Exhaustion/Reversal", "fn": _p05_liq_exhaust,
            "exit": {"stop_frac": 0.010, "tp_frac": 0.014, "time_exit": 20},
            "data": "PROXY (no liquidation feed)", "status": "PROXY"},
    "P06": {"name": "Order-Book Absorption", "fn": _p06_absorption,
            "exit": {"stop_frac": 0.006, "tp_frac": 0.008, "time_exit": 12},
            "data": "PROXY (no book feed)", "status": "PROXY"},
    "P07": {"name": "Cross-Venue Lead/Lag", "fn": _p07_cross_venue,
            "exit": {"stop_frac": 0.004, "tp_frac": 0.006, "time_exit": 10},
            "data": "cross-venue ref bars", "status": "IMPLEMENTED"},
    "P08": {"name": "OI/Funding Divergence", "fn": _p08_oi_funding,
            "exit": {"stop_frac": 0.006, "tp_frac": 0.008, "time_exit": 20},
            "data": "PROXY (funding-hour; no OI feed)", "status": "PROXY"},
    "P09": {"name": "Volatility Expansion/Regime", "fn": _p09_vol_expansion,
            "exit": {"stop_frac": 0.006, "tp_frac": 0.014, "time_exit": 24},
            "data": "OHLCV", "status": "IMPLEMENTED"},
    "P10": {"name": "Mean Reversion (range)", "fn": _p10_mean_reversion,
            "exit": {"stop_frac": 0.006, "tp_frac": 0.008, "time_exit": 16},
            "data": "OHLCV", "status": "IMPLEMENTED"},
    "P11": {"name": "Crash/Panic SHORT", "fn": _p11_crash_short,
            "exit": {"stop_frac": 0.008, "tp_frac": 0.012, "time_exit": 15},
            "data": "OHLCV", "status": "IMPLEMENTED"},
    "P12": {"name": "Event/News Shock", "fn": _p12_event_news,
            "exit": {"stop_frac": 0.008, "tp_frac": 0.012, "time_exit": 15},
            "data": "DATA_NOT_AVAILABLE (no public news feed)",
            "status": "DATA_NOT_AVAILABLE"},
}


# ------------------------------------------------ Trend Rider variants A–J
def _tr_confirm_dir(s):
    if s["slope"] > 0.00005 and s["up_frac"] >= 0.58:
        return "LONG"
    if s["slope"] < -0.00005 and s["up_frac"] <= 0.42:
        return "SHORT"
    return None


def _trA_basic(s, ctx):
    d = _tr_confirm_dir(s)
    return ("TRADE", d, 0.55) if d else ("ABSTAIN_LOW_REWARD", "FLAT", 0.5)


def _trB_pullback(s, ctx):
    d = _tr_confirm_dir(s)
    if d == "LONG" and s["ret_5"] < 0 and s["rsi"] < 55:
        return "TRADE", "LONG", 0.55
    if d == "SHORT" and s["ret_5"] > 0 and s["rsi"] > 45:
        return "TRADE", "SHORT", 0.55
    return "ABSTAIN_LOW_REWARD", "FLAT", 0.5


def _trC_breakout(s, ctx):
    d = _tr_confirm_dir(s)
    if d == "LONG" and s["last"] > s["don_hi"]:
        return "TRADE", "LONG", 0.55
    if d == "SHORT" and s["last"] < s["don_lo"]:
        return "TRADE", "SHORT", 0.55
    return "ABSTAIN_LOW_REWARD", "FLAT", 0.5


def _trD_flow(s, ctx):
    d = _tr_confirm_dir(s)
    if d and s["vol_accel"] > 1.3:
        return "TRADE", d, 0.55
    return "ABSTAIN_LOW_REWARD", "FLAT", 0.5


def _trE_book(s, ctx):         # PROXY: wick-imbalance as book confirmation
    d = _tr_confirm_dir(s)
    if d == "LONG" and s["lower_wick"] >= s["upper_wick"]:
        return "TRADE", "LONG", 0.54
    if d == "SHORT" and s["upper_wick"] >= s["lower_wick"]:
        return "TRADE", "SHORT", 0.54
    return "ABSTAIN_LOW_REWARD", "FLAT", 0.5


def _trF_oi_liq(s, ctx):       # PROXY: volatility-expansion confirmation
    d = _tr_confirm_dir(s)
    if d and s["atr_pct"] > 0.6:
        return "TRADE", d, 0.54
    return "ABSTAIN_LOW_REWARD", "FLAT", 0.5


def _trG_cross_venue(s, ctx):  # needs ref bars
    d = _tr_confirm_dir(s)
    gap = ctx.get("xv_gap")
    if gap is None:
        return "ABSTAIN_DATA_QUALITY", "FLAT", 0.5
    if d == "LONG" and gap > 0:
        return "TRADE", "LONG", 0.54
    if d == "SHORT" and gap < 0:
        return "TRADE", "SHORT", 0.54
    return "ABSTAIN_LOW_REWARD", "FLAT", 0.5


def _trH_learner(s, ctx):      # trend + simple persistence/reward gate
    d = _tr_confirm_dir(s)
    if d and abs(s["slope"]) * 500 > 0.05 and s["move_consumed"] < 0.9:
        return "TRADE", d, 0.56
    return "ABSTAIN_LOW_REWARD", "FLAT", 0.5


def _trI_no_abstain(s, ctx):   # always takes a side by slope sign
    return "TRADE", ("LONG" if s["slope"] >= 0 else "SHORT"), 0.5


def _trJ_no_trade(s, ctx):
    return "ABSTAIN_LOW_REWARD", "FLAT", 0.5


TREND_VARIANTS = {
    "TR_A_basic": {"fn": _trA_basic, "data": "OHLCV"},
    "TR_B_pullback": {"fn": _trB_pullback, "data": "OHLCV"},
    "TR_C_breakout": {"fn": _trC_breakout, "data": "OHLCV"},
    "TR_D_flow": {"fn": _trD_flow, "data": "OHLCV(flow proxy)"},
    "TR_E_book": {"fn": _trE_book, "data": "PROXY (no book feed)"},
    "TR_F_oi_liq": {"fn": _trF_oi_liq, "data": "PROXY (no OI/liq feed)"},
    "TR_G_cross_venue": {"fn": _trG_cross_venue, "data": "cross-venue ref"},
    "TR_H_learner": {"fn": _trH_learner, "data": "OHLCV"},
    "TR_I_no_abstain": {"fn": _trI_no_abstain, "data": "OHLCV"},
    "TR_J_no_trade": {"fn": _trJ_no_trade, "data": "none"},
}
TREND_EXIT = {"stop_frac": 0.008, "tp_frac": 0.012, "time_exit": 20}


def family_decider(fid: str, *, symbol: str, venue: str, timeframe: str,
                   gen_id: str | None, direction: str | None = None,
                   ref_bars_by_ts: dict | None = None) -> Callable:
    """Return a decide_fn(feats, event_id, dt, cluster) for tournament._drive.
    `feats` carries {"bars_upto": [...]} injected by the edge_search driver.
    `direction` optionally restricts to LONG-only / SHORT-only research."""
    fam = FAMILIES[fid]
    spec_hash = C.canonical_hash({"family": fid, "exit": fam["exit"]})

    def decide_fn(feats, event_id, dt, cluster):
        # fast path: the driver precomputes _sig ONCE per bar and shares it;
        # fall back to computing from bars_upto for direct/unit-test callers
        s = feats.get("_sig")
        if s is None:
            s = _sig(feats.get("bars_upto") or [])
        if not s.get("ok"):
            return _mk("ABSTAIN_DATA_QUALITY", "FLAT", 0.5, symbol=symbol,
                       venue=venue, timeframe=timeframe, event_id=event_id,
                       dt=dt, gen_id=gen_id, reason="ABSTAIN_DATA_QUALITY",
                       spec_hash=spec_hash, policy_id=fid)
        ctx = {}
        if ref_bars_by_ts is not None:
            ts = feats.get("ts")
            if ts is None and feats.get("bars_upto"):
                ts = int(feats["bars_upto"][-1]["ts"])
            ref = ref_bars_by_ts.get(int(ts)) if ts is not None else None
            if ref is not None and s["last"]:
                ctx["xv_gap"] = (s["last"] - float(ref)) / s["last"]
        action, side, prob = fam["fn"](s, ctx)
        if action == "TRADE" and direction and side != direction:
            action, side, prob = "ABSTAIN_REGIME", "FLAT", 0.5
        return _mk(action, side, prob, symbol=symbol, venue=venue,
                   timeframe=timeframe, event_id=event_id, dt=dt, gen_id=gen_id,
                   reason=action, regime=("TREND_UP" if s["slope"] > 0 else
                                          "TREND_DOWN"),
                   spec_hash=spec_hash, policy_id=fid)
    return decide_fn
