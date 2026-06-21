"""ResearchOps V10.8 - Adaptive Trailing Profit Exit Lab (research-only).

A PURE, offline, deterministic laboratory to compare dynamic exit policies
(break-even lock, ATR/percent/structure trailing, profit-protection ladder,
time-death) against a fixed TP/SL/time baseline, over a local V10.6 OHLCV+funding
sample. It also runs an AGGRESSIVE OPPORTUNITY (leverage) simulation that is
research-only and always recommends NO real leverage.

Hard guarantees (enforced by construction):
- no network, no DB, no .env, no private API, no orders, no leverage/sizing;
- entries are no-lookahead (decided on a closed bar, filled next bar open);
- the stop active DURING bar j is computed from data through bar j-1, so a
  trailing stop never uses the same bar's extreme to both move AND trigger;
- when a bar touches both the protective stop and the take-profit, the WORST
  case is assumed (loss first) for the base result;
- fees + slippage + (optional) funding are always subtracted;
- NOTHING here can set paper_ready/live_ready/can_send_real_orders true.

FINAL_RECOMMENDATION: NO LIVE.
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from . import FINAL_RECOMMENDATION_NO_LIVE

TOOL_VERSION = "v10.8"
OUTPUT_ROOT = "reports/research/v10_8"

ENTRY_FAMILIES = ("breakout_momentum", "trend_pullback", "volatility_expansion")
EXIT_POLICIES = ("fixed_tp_sl_time", "break_even_lock", "atr_trailing",
                 "percent_trailing", "structure_trailing", "hybrid_trailing",
                 "time_death_exit", "profit_protection_ladder")
SIDES = ("LONG", "SHORT")
EXIT_REASONS = ("STOP", "TP", "TRAILING", "BREAK_EVEN", "TIME_DEATH",
                "END_OF_DATA", "SAME_BAR_AMBIGUITY_WORST_CASE")
LEVERAGE_GRID = (1, 2, 3, 5, 10, 20)
DANGEROUS_LEVERAGE = 20

# Candidate verdicts - never APPROVED_FOR_PAPER/LIVE.
CAND_REJECTED = "REJECTED"
CAND_WEAK = "WEAK_RESEARCH_HYPOTHESIS"
CAND_RESEARCH_ONLY = "RESEARCH_CANDIDATE_ONLY"

# V10.8.1 - walk-forward modes (the old simple split is NOT a walk-forward).
WF_NONE = "none"
WF_SPLIT = "chronological_split"          # single chronological OOS split
WF_ROLLING = "rolling"                     # real rolling walk-forward
WF_STATUS_OK = "OK"
WF_STATUS_INSUFFICIENT = "INSUFFICIENT_FOLDS"
WF_STATUS_SPLIT_ONLY = "CHRONOLOGICAL_SPLIT_ONLY"
WF_STATUS_NONE = "NO_WALK_FORWARD"
# rolling WF defaults + pass thresholds
WF_TRAIN_FRAC = 0.5
WF_TEST_FRAC = 0.2
WF_STEP_FRAC = 0.15
WF_MIN_FOLDS = 3
MIN_OOS_PF = 1.0
WF_STRONG_PASS_RATE = 0.6     # >= -> eligible RESEARCH_CANDIDATE_ONLY
WF_WEAK_PASS_RATE = 0.34      # >= -> WEAK_RESEARCH_HYPOTHESIS; below -> rejected

# Data classifications carried from the V10.6 validator.
CLS_SAMPLE_ONLY = "SAMPLE_ONLY"
CLS_INTERMEDIATE = "INTERMEDIATE_RESEARCH_ONLY"
CLS_LONG_READY = "LONG_HISTORY_RESEARCH_READY"

# Indicator / generation defaults (causal).
_ATR_PERIOD = 14
_EMA_FAST = 12
_EMA_SLOW = 26
_WARMUP = 40
_DEFAULT_COOLDOWN = 6
_DEFAULT_MAX_ENTRIES = 40
_DEFAULT_MAX_HOLD = 30

# Anti-overfit thresholds.
MIN_NET_PF = 1.15
MAX_TIME_DEATH_FRACTION = 0.6
MIN_PROFIT_CAPTURE = 0.15
MAX_GIVEBACK = 0.85
MAX_DRAWDOWN_FRACTION = 0.5  # of summed |loss|; research heuristic


# --------------------------------------------------------------------------
# small pure helpers
# --------------------------------------------------------------------------

def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safety_block() -> dict[str, Any]:
    return {"research_only": True, "paper_ready": False, "live_ready": False,
            "can_send_real_orders": False, "paper_filter_enabled": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _pctile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


# --------------------------------------------------------------------------
# A. Data loading (V10.6 sample) - read-only
# --------------------------------------------------------------------------

def load_ohlcv(path: str) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                ts = _f(r.get("timestamp") or r.get("timestamp_ms"))
                o, h, l, c = (_f(r.get("open")), _f(r.get("high")),
                              _f(r.get("low")), _f(r.get("close")))
                v = _f(r.get("volume"))
                if ts is None or None in (o, h, l, c):
                    continue
                bars.append({"ts": int(ts), "open": o, "high": h, "low": l,
                             "close": c, "volume": v if v is not None else 0.0})
    except Exception:
        return []
    bars.sort(key=lambda b: b["ts"])
    # drop duplicate timestamps (keep first)
    seen, out = set(), []
    for b in bars:
        if b["ts"] in seen:
            continue
        seen.add(b["ts"])
        out.append(b)
    return out


def load_funding(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                ts = _f(r.get("timestamp") or r.get("funding_time_ms"))
                rate = _f(r.get("funding_rate"))
                if ts is None or rate is None:
                    continue
                rows.append({"ts": int(ts), "rate": rate})
    except Exception:
        return []
    rows.sort(key=lambda x: x["ts"])
    return rows


# --------------------------------------------------------------------------
# Causal indicators
# --------------------------------------------------------------------------

def atr_series(bars: list[dict[str, Any]], period: int = _ATR_PERIOD) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    trs: list[float] = []
    for i, b in enumerate(bars):
        if i == 0:
            tr = b["high"] - b["low"]
        else:
            pc = bars[i - 1]["close"]
            tr = max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc))
        trs.append(tr)
        if i >= period:
            out[i] = sum(trs[i - period + 1:i + 1]) / period
    return out


def ema_series(bars: list[dict[str, Any]], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    k = 2.0 / (period + 1)
    ema: float | None = None
    for i, b in enumerate(bars):
        c = b["close"]
        ema = c if ema is None else (c * k + ema * (1 - k))
        if i >= period:
            out[i] = ema
    return out


# --------------------------------------------------------------------------
# B. Entry families (no-lookahead: decided on bar i close, filled at i+1 open)
# --------------------------------------------------------------------------

@dataclass
class Costs:
    cost_bps: float = 6.0       # taker fee per side
    slippage_bps: float = 4.0   # slippage per side
    funding_mode: bool = True

    def round_trip_fraction(self, mult: float = 1.0) -> float:
        return (2.0 * self.cost_bps + 2.0 * self.slippage_bps) * mult / 10_000.0


def _funding_window(funding: list[dict[str, Any]], ts0: int, ts1: int) -> float:
    return sum(x["rate"] for x in funding if ts0 <= x["ts"] <= ts1)


def _regime_tags(emaf: float | None, emas: float | None, close: float,
                 atr: float | None, atr_med: float | None,
                 funding_rate: float | None) -> list[str]:
    tags: list[str] = []
    if emaf is not None and emas is not None:
        if emaf > emas and close >= emas:
            tags.append("trend_up")
        elif emaf < emas and close <= emas:
            tags.append("trend_down")
        else:
            tags.append("range")
    else:
        tags.append("range")
    if atr is not None and atr_med is not None and atr_med > 0:
        tags.append("high_volatility" if atr > 1.3 * atr_med else "low_volatility")
    if funding_rate is not None:
        if funding_rate >= 0.0005:
            tags.append("funding_extreme_positive")
        elif funding_rate <= -0.0005:
            tags.append("funding_extreme_negative")
        elif funding_rate > 0:
            tags.append("funding_positive")
        elif funding_rate < 0:
            tags.append("funding_negative")
    return tags


def generate_entries(*, symbol: str, timeframe: str, side: str, family: str,
                     bars: list[dict[str, Any]], funding: list[dict[str, Any]],
                     cooldown: int = _DEFAULT_COOLDOWN,
                     max_entries: int = _DEFAULT_MAX_ENTRIES,
                     breakout_n: int = 20, expansion_mult: float = 1.4,
                     pullback_lookback: int = 6) -> list[dict[str, Any]]:
    """Return no-lookahead entry signals. Each uses only bars[:i+1]."""
    if len(bars) < _WARMUP + 2:
        return []
    atr = atr_series(bars)
    emaf = ema_series(bars, _EMA_FAST)
    emas = ema_series(bars, _EMA_SLOW)
    is_long = side.upper() == "LONG"
    out: list[dict[str, Any]] = []
    last_entry_i = -10_000
    for i in range(_WARMUP, len(bars) - 1):  # need bar i+1 to fill
        if i - last_entry_i < cooldown:
            continue
        a = atr[i]
        if a is None or a <= 0:
            continue
        c = bars[i]["close"]
        ef, es = emaf[i], emas[i]
        signal = False
        reason = ""
        if family == "breakout_momentum":
            window_high = max(b["high"] for b in bars[i - breakout_n:i])
            window_low = min(b["low"] for b in bars[i - breakout_n:i])
            if is_long and c > window_high and (ef is None or es is None or ef > es):
                signal, reason = True, f"breakout_high_{breakout_n}"
            elif not is_long and c < window_low and (ef is None or es is None or ef < es):
                signal, reason = True, f"breakout_low_{breakout_n}"
        elif family == "trend_pullback":
            if ef is not None and es is not None:
                prior = bars[i - pullback_lookback:i]
                if is_long and ef > es:
                    dipped = any(b["low"] < ef for b in prior)
                    if dipped and c > bars[i - 1]["close"]:
                        signal, reason = True, "uptrend_pullback_recovery"
                elif not is_long and ef < es:
                    popped = any(b["high"] > ef for b in prior)
                    if popped and c < bars[i - 1]["close"]:
                        signal, reason = True, "downtrend_rebound_continuation"
        elif family == "volatility_expansion":
            rng = bars[i]["high"] - bars[i]["low"]
            recent = [bars[k]["high"] - bars[k]["low"] for k in range(i - 10, i)]
            avg_rng = sum(recent) / len(recent) if recent else 0.0
            if avg_rng > 0 and rng > expansion_mult * avg_rng:
                up = bars[i]["close"] >= bars[i]["open"]
                if is_long and up:
                    signal, reason = True, "vol_expansion_up"
                elif not is_long and not up:
                    signal, reason = True, "vol_expansion_down"
        if not signal:
            continue
        entry_idx = i + 1
        entry_price = bars[entry_idx]["open"]
        if entry_price <= 0:
            continue
        atr_med = None
        atrs_prior = [x for x in atr[max(0, i - 50):i] if x is not None]
        if atrs_prior:
            atr_med = _median(atrs_prior)
        fr = None
        if funding:
            prior_f = [x["rate"] for x in funding if x["ts"] <= bars[i]["ts"]]
            fr = prior_f[-1] if prior_f else None
        out.append({
            "symbol": symbol, "timeframe": timeframe, "side": side.upper(),
            "family": family, "signal_idx": i, "entry_idx": entry_idx,
            "entry_ts": bars[entry_idx]["ts"], "entry_price": entry_price,
            "entry_reason": reason, "atr": a,
            "regimes": _regime_tags(ef, es, c, a, atr_med, fr),
            "funding_snapshot": fr, "no_lookahead": True})
        last_entry_i = i
        if len(out) >= max_entries:
            break
    return out


# --------------------------------------------------------------------------
# C+D. Exit simulation - symmetric, monotonic stops, worst-case same-bar
# --------------------------------------------------------------------------

def _swing_level(bars: list[dict[str, Any]], upto: int, n: int, *, low: bool) -> float | None:
    seg = bars[max(0, upto - n):upto]
    if not seg:
        return None
    return min(b["low"] for b in seg) if low else max(b["high"] for b in seg)


def simulate_trade(bars: list[dict[str, Any]], atr: list[float | None],
                   entry: dict[str, Any], *, policy: str, params: dict[str, Any],
                   costs: Costs, funding: list[dict[str, Any]],
                   same_bar_policy: str = "worst_case",
                   gap_policy: str = "adverse_open") -> dict[str, Any] | None:
    side = entry["side"]
    is_long = side == "LONG"
    ei = entry["entry_idx"]
    if ei >= len(bars):
        return None
    ep = entry["entry_price"]
    sl_pct = float(params.get("sl_pct", 0.02))
    tp_pct = float(params.get("tp_pct", 0.06))
    max_hold = int(params.get("max_hold", _DEFAULT_MAX_HOLD))
    risk_dist = ep * sl_pct
    if risk_dist <= 0:
        return None

    init_stop = ep - risk_dist if is_long else ep + risk_dist
    has_tp = policy in ("fixed_tp_sl_time", "break_even_lock", "profit_protection_ladder")
    tp_price = (ep + tp_pct * ep) if is_long else (ep - tp_pct * ep)

    stop = init_stop
    high_since = ep
    low_since = ep
    be_active = False
    trail_active = False
    same_bar_ambig = False
    gap_adverse = False
    gap_slippage_bps = 0.0
    time_to_lock = None
    last = min(ei + max_hold, len(bars) - 1)
    exit_reason = "END_OF_DATA"
    exit_price = bars[last]["close"]
    exit_idx = last

    be_trigger_R = float(params.get("be_trigger_R", 1.0))
    atr_mult = float(params.get("atr_mult", 2.5))
    pct_trail = float(params.get("pct_trail", 0.03))
    struct_n = int(params.get("struct_n", 5))
    td_bars = int(params.get("time_death_bars", 8))
    td_min_mfe_R = float(params.get("time_death_min_mfe_R", 0.5))
    buffer_frac = costs.round_trip_fraction() / 2.0  # BE buffer covers ~fees

    for j in range(ei, last + 1):
        op, hi, lo, cl = (bars[j]["open"], bars[j]["high"], bars[j]["low"],
                          bars[j]["close"])
        # --- hit checks use the stop/tp active coming INTO bar j (no lookahead) ---
        hit_stop = (lo <= stop) if is_long else (hi >= stop)
        hit_tp = has_tp and ((hi >= tp_price) if is_long else (lo <= tp_price))

        # V10.8.1 - gap-adverse fill: if the bar OPENED beyond the stop, a real
        # exit fills at the worse open, never optimistically at the stop level.
        def _stop_fill() -> float:
            if gap_policy != "adverse_open":
                return stop
            return min(stop, op) if is_long else max(stop, op)

        if hit_stop and hit_tp:
            same_bar_ambig = True
            if same_bar_policy == "best_case":
                exit_reason, exit_price, exit_idx = "TP", tp_price, j
            else:  # worst_case (default) - the stop side, gap-adverse aware
                fill = _stop_fill()
                gap_adverse = fill != stop
                exit_reason, exit_price, exit_idx = "SAME_BAR_AMBIGUITY_WORST_CASE", fill, j
            break
        if hit_stop:
            fill = _stop_fill()
            gap_adverse = fill != stop
            exit_reason = ("BREAK_EVEN" if be_active and abs(stop - ep) <= buffer_frac * ep * 2
                           else "TRAILING" if trail_active else "STOP")
            exit_price, exit_idx = fill, j
            break
        if hit_tp:
            # TP stays conservative - no gap-favorable improvement in the base.
            exit_reason, exit_price, exit_idx = "TP", tp_price, j
            break

        # --- update favorable excursion with THIS bar, then recompute stop for NEXT bar ---
        high_since = max(high_since, hi)
        low_since = min(low_since, lo)
        mfe_now = (high_since - ep) / ep if is_long else (ep - low_since) / ep
        mfe_R = mfe_now / sl_pct if sl_pct > 0 else 0.0
        a = atr[j] if atr[j] is not None else entry["atr"]

        new_stop = stop
        if policy == "break_even_lock":
            if mfe_R >= be_trigger_R:
                be = ep + buffer_frac * ep if is_long else ep - buffer_frac * ep
                new_stop = max(stop, be) if is_long else min(stop, be)
                if not be_active:
                    be_active = True
                    time_to_lock = j - ei
        elif policy == "atr_trailing":
            cand = (high_since - atr_mult * a) if is_long else (low_since + atr_mult * a)
            new_stop = max(stop, cand) if is_long else min(stop, cand)
            if new_stop != stop:
                trail_active = True
        elif policy == "percent_trailing":
            cand = high_since * (1 - pct_trail) if is_long else low_since * (1 + pct_trail)
            new_stop = max(stop, cand) if is_long else min(stop, cand)
            if new_stop != stop:
                trail_active = True
        elif policy == "structure_trailing":
            sw = _swing_level(bars, j, struct_n, low=is_long)  # past bars only
            if sw is not None:
                new_stop = max(stop, sw) if is_long else min(stop, sw)
                if new_stop != stop:
                    trail_active = True
        elif policy == "hybrid_trailing":
            cands = [stop]
            if mfe_R >= be_trigger_R:
                cands.append(ep + buffer_frac * ep if is_long else ep - buffer_frac * ep)
                if not be_active:
                    be_active = True
                    time_to_lock = j - ei
            cands.append((high_since - atr_mult * a) if is_long else (low_since + atr_mult * a))
            sw = _swing_level(bars, j, struct_n, low=is_long)
            if sw is not None:
                cands.append(sw)
            cand = max(cands) if is_long else min(cands)
            new_stop = max(stop, cand) if is_long else min(stop, cand)
            if new_stop != stop:
                trail_active = True
            if (j - ei) >= td_bars and mfe_R < td_min_mfe_R:
                exit_reason, exit_price, exit_idx = "TIME_DEATH", cl, j
                break
        elif policy == "time_death_exit":
            if (j - ei) >= td_bars and mfe_R < td_min_mfe_R:
                exit_reason, exit_price, exit_idx = "TIME_DEATH", cl, j
                break
        elif policy == "profit_protection_ladder":
            lock = None
            if mfe_R >= 3.0:
                lock = high_since - atr_mult * a if is_long else low_since + atr_mult * a
            elif mfe_R >= 2.0:
                lock = ep + 1.0 * risk_dist if is_long else ep - 1.0 * risk_dist
            elif mfe_R >= 1.5:
                lock = ep + 0.5 * risk_dist if is_long else ep - 0.5 * risk_dist
            elif mfe_R >= 1.0:
                lock = ep + buffer_frac * ep if is_long else ep - buffer_frac * ep
            if lock is not None:
                new_stop = max(stop, lock) if is_long else min(stop, lock)
                if new_stop != stop:
                    trail_active = True
                    if not be_active:
                        be_active = True
                        time_to_lock = j - ei
        # monotonic guard (never loosen)
        stop = max(stop, new_stop) if is_long else min(stop, new_stop)

    # exit accounting
    gross_ret = ((exit_price - ep) / ep) if is_long else ((ep - exit_price) / ep)
    held = exit_idx - ei + 1
    cost = costs.round_trip_fraction()
    funding_frac = 0.0
    if costs.funding_mode and funding:
        fsum = _funding_window(funding, entry["entry_ts"], bars[exit_idx]["ts"])
        funding_frac = (-fsum) if is_long else (fsum)
    net_ret = gross_ret - cost + funding_frac
    mfe = (high_since - ep) / ep if is_long else (ep - low_since) / ep
    mae = (ep - low_since) / ep if is_long else (high_since - ep) / ep
    realized_fav = max(0.0, gross_ret)
    profit_capture = (realized_fav / mfe) if mfe > 1e-12 else 0.0
    giveback = ((mfe - realized_fav) / mfe) if mfe > 1e-12 else 0.0
    if gap_adverse:
        gap_slippage_bps = abs(stop - exit_price) / ep * 10_000.0
    return {
        "symbol": entry["symbol"], "timeframe": entry["timeframe"], "side": side,
        "family": entry["family"], "policy": policy, "entry_ts": entry["entry_ts"],
        "entry_price": ep, "exit_price": exit_price, "exit_reason": exit_reason,
        "gross_ret": gross_ret, "net_ret": net_ret, "R": net_ret / sl_pct,
        "held_bars": held, "mfe": mfe, "mae": mae,
        "profit_capture": min(profit_capture, 1.0), "giveback": max(0.0, giveback),
        "be_activated": be_active, "trail_activated": trail_active,
        "time_to_lock": time_to_lock, "same_bar_ambiguous": same_bar_ambig,
        "gap_adverse": gap_adverse, "gap_slippage_bps": round(gap_slippage_bps, 3),
        "gap_policy": gap_policy,
        "fee_frac": 2.0 * costs.cost_bps / 10_000.0,
        "slippage_frac": 2.0 * costs.slippage_bps / 10_000.0,
        "funding_frac": funding_frac, "regimes": entry["regimes"],
        "sl_pct": sl_pct}


# --------------------------------------------------------------------------
# E. Metrics
# --------------------------------------------------------------------------

def compute_metrics(trades: list[dict[str, Any]], costs: Costs) -> dict[str, Any]:
    n = len(trades)
    base = {"trades": n}
    if n == 0:
        base.update({"net_EV": 0.0, "net_PF": 0.0, "win_rate": 0.0})
        return base
    nets = [t["net_ret"] for t in trades]
    gros = [t["gross_ret"] for t in trades]
    Rs = sorted(t["R"] for t in trades)
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gains = sum(wins)
    loss_sum = -sum(losses)
    # equity curve drawdown (sum of net returns, research units)
    equity = peak = max_dd = 0.0
    streak = max_streak = 0
    for x in nets:
        equity += x
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if x <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    dist: dict[str, int] = {}
    for t in trades:
        dist[t["exit_reason"]] = dist.get(t["exit_reason"], 0) + 1
    net_ev = sum(nets) / n
    stress = {}
    for mult in (2, 3):
        extra = costs.round_trip_fraction() * (mult - 1)
        sn = [x - extra for x in nets]
        sw, sl = sum(v for v in sn if v > 0), -sum(v for v in sn if v <= 0)
        stress[f"net_EV_x{mult}"] = round(sum(sn) / n, 6)
        stress[f"net_PF_x{mult}"] = round((sw / sl) if sl > 0 else (math.inf if sw > 0 else 0.0), 4)
    holds = [t["held_bars"] for t in trades]
    return {
        "trades": n,
        "net_EV": round(net_ev, 6),
        "gross_EV": round(sum(gros) / n, 6),
        "net_profit": round(sum(nets), 6),
        "net_profit_bps": round(sum(nets) * 10_000.0, 2),
        "profit_factor_net": round((gains / loss_sum) if loss_sum > 0 else (math.inf if gains > 0 else 0.0), 4),
        "win_rate": round(len(wins) / n, 4),
        "avg_win": round(sum(wins) / len(wins), 6) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 6) if losses else 0.0,
        "payoff_ratio": round((sum(wins) / len(wins)) / (-sum(losses) / len(losses)), 4) if wins and losses else 0.0,
        "max_drawdown": round(max_dd, 6),
        "max_consecutive_losses": max_streak,
        "avg_R": round(sum(Rs) / n, 4),
        "median_R": round(_median(Rs), 4),
        "p10_R": round(_pctile(Rs, 0.10), 4),
        "p90_R": round(_pctile(Rs, 0.90), 4),
        "exposure_bars": sum(holds),
        "avg_holding_bars": round(sum(holds) / n, 2),
        "median_holding_bars": round(_median([float(h) for h in holds]), 1),
        "fees_total": round(sum(t["fee_frac"] for t in trades), 6),
        "slippage_total": round(sum(t["slippage_frac"] for t in trades), 6),
        "funding_total": round(sum(t["funding_frac"] for t in trades), 6),
        "MFE": round(sum(t["mfe"] for t in trades) / n, 6),
        "MAE": round(sum(t["mae"] for t in trades) / n, 6),
        "MFE_MAE_ratio": round((sum(t["mfe"] for t in trades) / max(1e-12, sum(t["mae"] for t in trades))), 4),
        "profit_capture_ratio": round(sum(t["profit_capture"] for t in trades) / n, 4),
        "giveback_ratio": round(sum(t["giveback"] for t in trades) / n, 4),
        "time_to_lock_profit": round(_median([float(t["time_to_lock"]) for t in trades if t["time_to_lock"] is not None]), 1) if any(t["time_to_lock"] is not None for t in trades) else None,
        "time_to_exit": round(sum(holds) / n, 2),
        "break_even_activation_rate": round(sum(1 for t in trades if t["be_activated"]) / n, 4),
        "trailing_activation_rate": round(sum(1 for t in trades if t["trail_activated"]) / n, 4),
        "exit_reason_distribution": dist,
        "same_bar_ambiguity_count": sum(1 for t in trades if t["same_bar_ambiguous"]),
        "gap_adverse_count": sum(1 for t in trades if t.get("gap_adverse")),
        "gap_adverse_slippage_bps_estimate": round(
            (sum(t.get("gap_slippage_bps", 0.0) for t in trades if t.get("gap_adverse"))
             / max(1, sum(1 for t in trades if t.get("gap_adverse")))), 3),
        "cost_stress_x2": stress["net_EV_x2"],
        "cost_stress_x3": stress["net_EV_x3"],
        "net_EV_after_cost_stress": {"x2": stress["net_EV_x2"], "x3": stress["net_EV_x3"]},
        "net_PF_after_cost_stress": {"x2": stress["net_PF_x2"], "x3": stress["net_PF_x3"]},
    }


# --------------------------------------------------------------------------
# G. Aggressive opportunity (leverage) simulation - research-only
# --------------------------------------------------------------------------

def leverage_simulation(metrics: dict[str, Any], *, sl_pct: float,
                        edge_validated: bool = False) -> dict[str, Any]:
    """Scale the (research) per-trade economics by leverage and flag danger.
    NEVER recommends real leverage; pure what-if arithmetic. Without a VALIDATED
    edge the whole simulation is BLOCKED (no leverage research is meaningful)."""
    net_ev = metrics.get("net_EV", 0.0)
    max_dd = metrics.get("max_drawdown", 0.0)
    pf2 = metrics.get("net_PF_after_cost_stress", {}).get("x2", 0.0)
    # maintenance margin proxy ~0.5%; liquidation distance ~ 1/lev - mm
    mm = 0.005
    rows = []
    for lev in LEVERAGE_GRID:
        liq_dist = max(0.0, (1.0 / lev) - mm)
        stop_inside_safe = sl_pct < liq_dist  # technical stop hit before liquidation
        dd_scaled = max_dd * lev
        dangerous = (lev >= DANGEROUS_LEVERAGE) or (not stop_inside_safe) \
            or (dd_scaled >= MAX_DRAWDOWN_FRACTION) or (net_ev <= 0) or (pf2 < 1.0)
        rows.append({
            "leverage": lev,
            "pnl_scaled": round(net_ev * lev, 6),
            "drawdown_scaled": round(dd_scaled, 6),
            "liquidation_distance_estimate": round(liq_dist, 5),
            "stop_distance_vs_liquidation_distance": round(sl_pct / liq_dist, 3) if liq_dist > 0 else "inf",
            "ruin_risk_proxy": round(min(1.0, (sl_pct / liq_dist) if liq_dist > 0 else 1.0), 4),
            "max_loss_streak_impact": round(metrics.get("max_consecutive_losses", 0) * sl_pct * lev, 4),
            "daily_loss_proxy": round(abs(metrics.get("avg_loss", 0.0)) * lev, 6),
            "cost_amplification": lev,
            "funding_amplification": lev,
            "whether_stop_is_inside_safe_zone": stop_inside_safe,
            "dangerous_leverage_flag": "DANGEROUS_RESEARCH_ONLY" if dangerous else "research_only",
        })
    return {
        "aggressive_opportunity_simulation": True,
        "leverage_grid": list(LEVERAGE_GRID),
        "rows": rows,
        "edge_validated": bool(edge_validated),
        "leverage_research_status": ("OPEN_RESEARCH" if edge_validated
                                     else "BLOCKED_NO_VALIDATED_EDGE"),
        "leverage_recommendation": "NO_REAL_LEVERAGE",
        "real_leverage_allowed": False,
        "uses_repeated_combo_trades_not_portfolio": True,
        **_safety_block(),
    }


# --------------------------------------------------------------------------
# H. Anti-overfit candidate gating + walk-forward
# --------------------------------------------------------------------------

def chronological_oos_single_split(trades: list[dict[str, Any]], train_ratio: float
                                   ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A SINGLE chronological train/OOS split. This is NOT a walk-forward; the
    name is explicit so it is never mistaken for rolling validation."""
    ordered = sorted(trades, key=lambda t: t["entry_ts"])
    cut = int(len(ordered) * max(0.1, min(0.9, train_ratio)))
    return ordered[:cut], ordered[cut:]


def rolling_walk_forward(trades: list[dict[str, Any]], *, costs: Costs,
                         train_frac: float = WF_TRAIN_FRAC,
                         test_frac: float = WF_TEST_FRAC,
                         step_frac: float = WF_STEP_FRAC,
                         min_folds: int = WF_MIN_FOLDS, anchored: bool = False,
                         min_trades_per_fold: int = 5) -> dict[str, Any]:
    """REAL rolling walk-forward over the chronological time span of the trades.
    Each fold trains on a window strictly BEFORE its test window (no future
    leakage). anchored=True grows the train window from t0; otherwise it rolls."""
    ordered = sorted(trades, key=lambda t: t["entry_ts"])
    out: dict[str, Any] = {"wf_mode": WF_ROLLING, "wf_is_rolling": True,
                           "folds": [], "wf_folds_total": 0, "wf_folds_passed": 0,
                           "wf_pass_rate": 0.0, "walk_forward_status": WF_STATUS_INSUFFICIENT}
    if len(ordered) < min_trades_per_fold * 2:
        return out
    t0, t1 = ordered[0]["entry_ts"], ordered[-1]["entry_ts"]
    span = t1 - t0
    if span <= 0:
        return out
    train_ms = span * train_frac
    test_ms = span * test_frac
    step_ms = max(1.0, span * step_frac)
    folds = []
    a = float(t0)
    fold_id = 0
    while True:
        train_start = float(t0) if anchored else a
        train_end = a + train_ms
        test_start = train_end
        test_end = test_start + test_ms
        if test_start > t1:
            break
        tr_train = [t for t in ordered if train_start <= t["entry_ts"] < train_end]
        tr_test = [t for t in ordered if test_start <= t["entry_ts"] < min(test_end, t1 + 1)]
        # only count folds that actually have a test window with trades
        if tr_test:
            mtr = compute_metrics(tr_train, costs)
            mte = compute_metrics(tr_test, costs)
            te_pf = mte.get("profit_factor_net", 0.0)
            te_pf = te_pf if not isinstance(te_pf, str) else math.inf
            passed = (len(tr_test) >= min_trades_per_fold
                      and mte.get("net_EV", 0.0) > 0 and te_pf >= MIN_OOS_PF)
            reason = ""
            if len(tr_test) < min_trades_per_fold:
                reason = "too_few_test_trades"
            elif mte.get("net_EV", 0.0) <= 0:
                reason = "test_net_EV<=0"
            elif te_pf < MIN_OOS_PF:
                reason = "test_PF<1.0"
            folds.append({
                "fold_id": fold_id, "train_start": int(train_start),
                "train_end": int(train_end), "test_start": int(test_start),
                "test_end": int(min(test_end, t1)), "trades_train": mtr["trades"],
                "trades_test": mte["trades"], "net_EV_train": mtr.get("net_EV"),
                "net_EV_test": mte.get("net_EV"), "PF_train": mtr.get("profit_factor_net"),
                "PF_test": mte.get("profit_factor_net"),
                "drawdown_test": mte.get("max_drawdown"),
                "candidate_passed_fold": passed, "failure_reason": reason})
            fold_id += 1
        a += step_ms
        if a >= t1:
            break
    passed_n = sum(1 for f in folds if f["candidate_passed_fold"])
    out["folds"] = folds
    out["wf_folds_total"] = len(folds)
    out["wf_folds_passed"] = passed_n
    out["wf_pass_rate"] = round(passed_n / len(folds), 4) if folds else 0.0
    out["walk_forward_status"] = (WF_STATUS_OK if len(folds) >= min_folds
                                  else WF_STATUS_INSUFFICIENT)
    return out


def _base_rejections(full: dict[str, Any], trades: list[dict[str, Any]],
                     min_trades: int) -> list[str]:
    rej: list[str] = []
    if full["trades"] < min_trades:
        rej.append(f"too_few_trades:{full['trades']}<{min_trades}")
    if full.get("net_EV", 0.0) <= 0:
        rej.append("net_EV<=0")
    pf = full.get("profit_factor_net", 0.0)
    if not isinstance(pf, str) and pf < MIN_NET_PF:
        rej.append(f"net_PF<{MIN_NET_PF}")
    if full.get("cost_stress_x2", 0.0) <= 0 or full.get("cost_stress_x3", 0.0) <= 0:
        rej.append("cost_stress_x2_or_x3_kills_edge")
    if full.get("profit_capture_ratio", 0.0) < MIN_PROFIT_CAPTURE:
        rej.append("profit_capture_too_low")
    if full.get("giveback_ratio", 1.0) > MAX_GIVEBACK:
        rej.append("giveback_too_high")
    td = full.get("exit_reason_distribution", {}).get("TIME_DEATH", 0)
    if full["trades"] and td / full["trades"] > MAX_TIME_DEATH_FRACTION:
        rej.append("too_many_time_death_exits")
    by_sym: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        by_sym.setdefault(t["symbol"], []).append(t)
    profitable = sum(1 for ts in by_sym.values() if sum(x["net_ret"] for x in ts) > 0)
    if len(by_sym) >= 3 and profitable <= 1:
        rej.append("works_only_on_one_symbol")
    return rej


def evaluate_candidate(trades: list[dict[str, Any]], *, costs: Costs,
                       min_trades: int, train_ratio: float = 0.6,
                       data_classification: str = CLS_INTERMEDIATE,
                       wf_mode: str = WF_ROLLING,
                       wf_train_frac: float = WF_TRAIN_FRAC,
                       wf_test_frac: float = WF_TEST_FRAC,
                       wf_step_frac: float = WF_STEP_FRAC,
                       wf_min_folds: int = WF_MIN_FOLDS,
                       wf_anchored: bool = False) -> dict[str, Any]:
    """Tiered, fail-closed candidate evaluation. Tiers: REJECTED <
    WEAK_RESEARCH_HYPOTHESIS < RESEARCH_CANDIDATE_ONLY. NEVER approved."""
    full = compute_metrics(trades, costs)
    rejection = _base_rejections(full, trades, min_trades)
    warnings: list[str] = []
    min_per_fold = max(5, min_trades // 3)

    wf: dict[str, Any] = {"wf_mode": wf_mode, "wf_is_rolling": wf_mode == WF_ROLLING,
                          "wf_folds_total": 0, "wf_folds_passed": 0,
                          "wf_pass_rate": 0.0, "folds": []}
    oos_metrics: dict[str, Any] = {}
    tier_ceiling = CAND_RESEARCH_ONLY  # best attainable given WF evidence

    if wf_mode == WF_ROLLING:
        wf = rolling_walk_forward(trades, costs=costs, train_frac=wf_train_frac,
                                  test_frac=wf_test_frac, step_frac=wf_step_frac,
                                  min_folds=wf_min_folds, anchored=wf_anchored,
                                  min_trades_per_fold=min_per_fold)
        if wf["walk_forward_status"] == WF_STATUS_INSUFFICIENT:
            warnings.append("insufficient_wf_folds")
            tier_ceiling = CAND_WEAK
        elif wf["wf_pass_rate"] >= WF_STRONG_PASS_RATE:
            tier_ceiling = CAND_RESEARCH_ONLY
        elif wf["wf_pass_rate"] >= WF_WEAK_PASS_RATE:
            tier_ceiling = CAND_WEAK
        else:
            rejection.append(f"rolling_wf_pass_rate_too_low:{wf['wf_pass_rate']}")
    elif wf_mode == WF_SPLIT:
        warnings.append("walk_forward_not_rolling_single_split_only")
        wf["walk_forward_status"] = WF_STATUS_SPLIT_ONLY
        train, oos = chronological_oos_single_split(trades, train_ratio)
        oos_metrics = compute_metrics(oos, costs)
        if len(oos) >= min_per_fold:
            if oos_metrics.get("net_EV", 0.0) <= 0:
                rejection.append("oos_net_EV<=0")
        else:
            rejection.append("insufficient_oos_trades")
        tier_ceiling = CAND_WEAK  # a single split can never earn the top tier
    else:  # WF_NONE
        warnings.append("no_oos_validation")
        wf["walk_forward_status"] = WF_STATUS_NONE
        tier_ceiling = CAND_WEAK

    if rejection:
        tier = CAND_REJECTED
    else:
        tier = tier_ceiling
        # an INTERMEDIATE (not LONG_READY) dataset can never reach the top tier
        if data_classification != CLS_LONG_READY and tier == CAND_RESEARCH_ONLY:
            tier = CAND_WEAK
            warnings.append("dataset_not_long_ready_capped_to_weak")

    return {
        "candidate_status": tier,
        "candidate_quality_tier": tier,
        "rejection_reasons": rejection,
        "warnings": warnings,
        "metrics_full": full,
        "metrics_oos": oos_metrics,
        "walk_forward": wf,
        "approved_for_paper": False,
        "approved_for_live": False,
        **_safety_block(),
    }


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def _policy_param_sets(policy: str) -> list[dict[str, Any]]:
    base = {"sl_pct": 0.02, "tp_pct": 0.06, "max_hold": _DEFAULT_MAX_HOLD,
            "be_trigger_R": 1.0, "atr_mult": 2.5, "pct_trail": 0.03,
            "struct_n": 5, "time_death_bars": 8, "time_death_min_mfe_R": 0.5}
    variants = [dict(base)]
    if policy == "atr_trailing":
        variants.append({**base, "atr_mult": 3.5})
    elif policy == "percent_trailing":
        variants.append({**base, "pct_trail": 0.05})
    elif policy == "break_even_lock":
        variants.append({**base, "be_trigger_R": 1.5})
    elif policy == "fixed_tp_sl_time":
        variants.append({**base, "tp_pct": 0.04})
    return variants


def _build_combos(families, policies, timeframes, sides, max_grid_combos, seed):
    combos = []
    for tf in timeframes:
        for side in sides:
            for fam in families:
                for pol in policies:
                    for params in _policy_param_sets(pol):
                        combos.append((tf, side, fam, pol, params))
    if max_grid_combos and len(combos) > max_grid_combos:
        rng = random.Random(seed)
        rng.shuffle(combos)
        combos = combos[:max_grid_combos]
        combos.sort(key=lambda c: (c[0], c[1], c[2], c[3]))
    return combos


def run_trailing_exit_lab(*, sample_dir: str, symbols: list[str],
                          timeframes: list[str], sides: list[str],
                          entry_families: list[str], exit_policies: list[str],
                          cost_bps: float = 6.0, slippage_bps: float = 4.0,
                          funding_mode: bool = True, min_trades: int = 30,
                          train_ratio: float = 0.6,
                          walk_forward_mode: str = WF_ROLLING,
                          wf_train_frac: float = WF_TRAIN_FRAC,
                          wf_test_frac: float = WF_TEST_FRAC,
                          wf_step_frac: float = WF_STEP_FRAC,
                          wf_min_folds: int = WF_MIN_FOLDS,
                          wf_anchored: bool = False,
                          gap_policy: str = "adverse_open",
                          max_grid_combos: int = 500, seed: int = 7,
                          data_classification: str = CLS_INTERMEDIATE,
                          aggressive: bool = True) -> dict[str, Any]:
    costs = Costs(cost_bps=cost_bps, slippage_bps=slippage_bps, funding_mode=funding_mode)
    families = [f for f in entry_families if f in ENTRY_FAMILIES]
    policies = [p for p in exit_policies if p in EXIT_POLICIES]
    sides = [s.upper() for s in sides if s.upper() in SIDES]
    timeframes = [t.lower() for t in timeframes]
    if walk_forward_mode not in (WF_NONE, WF_SPLIT, WF_ROLLING):
        walk_forward_mode = WF_ROLLING

    report: dict[str, Any] = {
        "tool_version": TOOL_VERSION, "generated_at": _now_iso(),
        "sample_dir": sample_dir, "symbols": symbols, "timeframes": timeframes,
        "sides": sides, "entry_families": families, "exit_policies": policies,
        "cost_bps": cost_bps, "slippage_bps": slippage_bps,
        "funding_mode": funding_mode, "funding_applied": funding_mode,
        "same_bar_policy": "worst_case", "gap_policy": gap_policy,
        "train_ratio": train_ratio, "walk_forward_mode": walk_forward_mode,
        "wf_is_rolling": walk_forward_mode == WF_ROLLING,
        "wf_train_frac": wf_train_frac, "wf_test_frac": wf_test_frac,
        "wf_step_frac": wf_step_frac, "wf_min_folds": wf_min_folds,
        "wf_anchored": wf_anchored, "seed": seed,
        "data_classification": data_classification,
        "oi_regime_available": False, "liquidations_available": False,
        "strategy_ready": False, "edge_validated": False,
        "evaluation_type": "exit_policy_research_on_baseline_entries",
        # honesty disclaimers - these results are NOT a tradable portfolio
        "comparison_not_portfolio": True,
        "entries_are_baseline_not_validated_edge": True,
        "candidates_are_hypotheses_not_signals": True,
        "regime_window_dependency_warning": True,
        "errors": [], "warnings": [],
        **_safety_block(),
    }
    if walk_forward_mode == WF_SPLIT:
        report["warnings"].append("deprecated_walk_forward_bool_maps_to_chronological_split")
        report["warnings"].append("walk_forward_not_rolling_single_split_only")
    if data_classification != CLS_LONG_READY:
        report["warnings"].append(
            f"data_classification={data_classification}: results are "
            "RESEARCH_CANDIDATE_ONLY (not strategy-ready, not paper/live)")
    if not (isinstance(sample_dir, str) and sample_dir and os.path.isdir(sample_dir)):
        report["errors"].append("sample_dir_not_found")
        return report
    if not (families and policies and sides and timeframes and symbols):
        report["errors"].append("nothing_to_run (need symbols/timeframes/sides/families/policies)")
        return report

    # Load data + generate baseline entries per (symbol, tf, side, family).
    entries_by_key: dict[tuple, list[dict[str, Any]]] = {}
    bars_cache: dict[tuple, list[dict[str, Any]]] = {}
    atr_cache: dict[tuple, list[float | None]] = {}
    funding_cache: dict[str, list[dict[str, Any]]] = {}
    total_entries = 0
    for sym in symbols:
        fpath = os.path.join(sample_dir, f"{sym}_funding.csv")
        funding_cache[sym] = load_funding(fpath) if os.path.isfile(fpath) else []
        for tf in timeframes:
            opath = os.path.join(sample_dir, f"{sym}_{tf}_ohlcv.csv")
            if not os.path.isfile(opath):
                report["warnings"].append(f"missing_ohlcv:{sym}_{tf}")
                continue
            bars = load_ohlcv(opath)
            if len(bars) < _WARMUP + 5:
                report["warnings"].append(f"too_few_bars:{sym}_{tf}:{len(bars)}")
                continue
            bars_cache[(sym, tf)] = bars
            atr_cache[(sym, tf)] = atr_series(bars)
            for side in sides:
                for fam in families:
                    ents = generate_entries(symbol=sym, timeframe=tf, side=side,
                                            family=fam, bars=bars,
                                            funding=funding_cache[sym])
                    entries_by_key[(sym, tf, side, fam)] = ents
                    total_entries += len(ents)
    report["total_baseline_entries"] = total_entries

    # Evaluate each candidate (tf, side, family, policy, params) across symbols.
    combos = _build_combos(families, policies, timeframes, sides, max_grid_combos, seed)
    report["combos_evaluated"] = len(combos)
    all_trades: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for (tf, side, fam, pol, params) in combos:
        ctrades: list[dict[str, Any]] = []
        for sym in symbols:
            ents = entries_by_key.get((sym, tf, side, fam))
            if not ents:
                continue
            bars = bars_cache[(sym, tf)]
            atr = atr_cache[(sym, tf)]
            for e in ents:
                tr = simulate_trade(bars, atr, e, policy=pol, params=params,
                                    costs=costs, funding=funding_cache[sym],
                                    gap_policy=gap_policy)
                if tr is not None:
                    tr["param_tag"] = json.dumps(params, sort_keys=True)
                    ctrades.append(tr)
        if not ctrades:
            continue
        all_trades.extend(ctrades)
        evaluation = evaluate_candidate(
            ctrades, costs=costs, min_trades=min_trades, train_ratio=train_ratio,
            data_classification=data_classification, wf_mode=walk_forward_mode,
            wf_train_frac=wf_train_frac, wf_test_frac=wf_test_frac,
            wf_step_frac=wf_step_frac, wf_min_folds=wf_min_folds, wf_anchored=wf_anchored)
        m = evaluation["metrics_full"]
        wf = evaluation["walk_forward"]
        candidates.append({
            "timeframe": tf, "side": side, "entry_family": fam, "exit_policy": pol,
            "params": params, "candidate_status": evaluation["candidate_status"],
            "candidate_quality_tier": evaluation["candidate_quality_tier"],
            "rejection_reasons": evaluation["rejection_reasons"],
            "warnings": evaluation.get("warnings", []),
            "trades": m["trades"], "net_EV": m.get("net_EV"),
            "net_PF": m.get("profit_factor_net"), "win_rate": m.get("win_rate"),
            "max_drawdown": m.get("max_drawdown"),
            "profit_capture_ratio": m.get("profit_capture_ratio"),
            "giveback_ratio": m.get("giveback_ratio"),
            "cost_stress_x2": m.get("cost_stress_x2"),
            "gap_adverse_count": m.get("gap_adverse_count"),
            "gap_adverse_slippage_bps_estimate": m.get("gap_adverse_slippage_bps_estimate"),
            "oos_net_EV": evaluation.get("metrics_oos", {}).get("net_EV"),
            "wf_mode": wf.get("wf_mode"), "wf_is_rolling": wf.get("wf_is_rolling"),
            "wf_folds_total": wf.get("wf_folds_total"),
            "wf_folds_passed": wf.get("wf_folds_passed"),
            "wf_pass_rate": wf.get("wf_pass_rate"),
            "walk_forward_status": wf.get("walk_forward_status"),
            "wf_folds": wf.get("folds", []),
            "exit_reason_distribution": m.get("exit_reason_distribution"),
            "metrics_full": m,
        })

    report["trades_simulated"] = len(all_trades)
    # accepted = any non-REJECTED tier (WEAK or RESEARCH_CANDIDATE_ONLY)
    accepted = [c for c in candidates if c["candidate_status"] != CAND_REJECTED]
    rejected = [c for c in candidates if c["candidate_status"] == CAND_REJECTED]
    tier_rank = {CAND_RESEARCH_ONLY: 2, CAND_WEAK: 1}
    accepted.sort(key=lambda c: (tier_rank.get(c["candidate_status"], 0),
                                 c.get("net_EV") or -9, c.get("wf_pass_rate") or 0),
                  reverse=True)
    rejected.sort(key=lambda c: (c.get("net_EV") or -9), reverse=True)
    report["research_candidates"] = accepted[:25]
    report["rejected_candidates"] = rejected[:50]
    report["n_research_candidates"] = len(accepted)
    report["n_rejected_candidates"] = len(rejected)
    report["n_research_candidate_only"] = sum(
        1 for c in accepted if c["candidate_status"] == CAND_RESEARCH_ONLY)
    report["n_weak_research_hypothesis"] = sum(
        1 for c in accepted if c["candidate_status"] == CAND_WEAK)

    # side concentration warning
    acc_sides = {c["side"] for c in accepted}
    if accepted and len(acc_sides) == 1:
        report["side_concentration_warning"] = f"{acc_sides.pop()}_ONLY"
    else:
        report["side_concentration_warning"] = ""
    # multiple-comparisons / false-discovery (n_combos = full grid attempted)
    n_combos = max(report.get("combos_evaluated", 0), len(candidates))
    n_acc = len(accepted)
    report["multiple_comparisons_warning"] = True
    report["n_combos_tested"] = n_combos
    report["n_candidates_after_gates"] = n_acc
    if n_combos >= 20 and (n_acc / max(1, n_combos)) < 0.1:
        report["false_discovery_risk"] = "HIGH"
    elif n_combos >= 20:
        report["false_discovery_risk"] = "MODERATE"
    else:
        report["false_discovery_risk"] = "LOW"
    # collect WF folds of the top accepted candidates for the folds report
    report["walk_forward_folds"] = []
    for c in accepted[:10]:
        for f in c.get("wf_folds", []):
            report["walk_forward_folds"].append({
                "combo": f"{c['timeframe']}/{c['side']}/{c['entry_family']}/{c['exit_policy']}",
                **f})
    report["walk_forward_status"] = (
        accepted[0]["walk_forward_status"] if accepted else
        (WF_STATUS_OK if walk_forward_mode == WF_ROLLING else WF_STATUS_SPLIT_ONLY
         if walk_forward_mode == WF_SPLIT else WF_STATUS_NONE))

    # aggregate metric tables (POLICY COMPARISON - not a tradable portfolio)
    report["metrics_by"] = _aggregate_tables(all_trades, costs)
    report["global_policy_comparison_metrics"] = report["metrics_by"]["global"]
    # leverage sim over the best accepted candidate - edge is NEVER validated
    # here, so the whole simulation reports BLOCKED_NO_VALIDATED_EDGE.
    best = accepted[0] if accepted else (candidates[0] if candidates else None)
    if best is not None:
        report["aggressive_opportunity"] = leverage_simulation(
            best["metrics_full"], sl_pct=float(best["params"].get("sl_pct", 0.02)),
            edge_validated=False)
    # trim heavy per-candidate fold lists from the listed candidates (the folds
    # already live in report["walk_forward_folds"]); keep summary.json lean.
    for c in report["research_candidates"] + report["rejected_candidates"]:
        c.pop("wf_folds", None)
    report["_all_trades"] = all_trades  # internal, for writers (not for JSON dump as-is)
    return report


def _aggregate_tables(trades: list[dict[str, Any]], costs: Costs) -> dict[str, Any]:
    def grp(keyfn):
        buckets: dict[str, list[dict[str, Any]]] = {}
        for t in trades:
            for k in keyfn(t):
                buckets.setdefault(k, []).append(t)
        return {k: compute_metrics(v, costs) for k, v in sorted(buckets.items())}
    return {
        "global": compute_metrics(trades, costs),
        "by_symbol": grp(lambda t: [t["symbol"]]),
        "by_timeframe": grp(lambda t: [t["timeframe"]]),
        "by_side": grp(lambda t: [t["side"]]),
        "by_regime": grp(lambda t: t["regimes"]),
        "by_exit_policy": grp(lambda t: [t["policy"]]),
    }


# --------------------------------------------------------------------------
# J. Report writers (reports/research/v10_8/<run_id>/) - never raw/DB/.env
# --------------------------------------------------------------------------

def _safe_output_dir(output_dir: str | None) -> str:
    base = output_dir or OUTPUT_ROOT
    norm = base.replace("\\", "/")
    segs = [s for s in norm.split("/") if s]
    forbidden = {"raw", "backups", "backup", "vault", "vaults", "training_exports", ".env"}
    if any(s in forbidden for s in segs) or "%" in norm or ".." in segs:
        base = OUTPUT_ROOT  # fail-safe to the canonical research dir
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(base, run_id)


def _write_csv(path: str, rows: list[dict[str, Any]], header: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})


def write_reports(report: dict[str, Any], output_dir: str | None = None) -> str:
    run_dir = _safe_output_dir(output_dir)
    os.makedirs(run_dir, exist_ok=True)
    trades = report.pop("_all_trades", [])

    # trades.csv
    if trades:
        tcols = ["symbol", "timeframe", "side", "family", "policy", "entry_ts",
                 "entry_price", "exit_price", "exit_reason", "gross_ret",
                 "net_ret", "R", "held_bars", "mfe", "mae", "profit_capture",
                 "giveback", "be_activated", "trail_activated",
                 "same_bar_ambiguous", "funding_frac"]
        _write_csv(os.path.join(run_dir, "trades.csv"), trades, tcols)

    # policy_metrics.csv (by_exit_policy)
    pol_rows = [{"exit_policy": k, **v} for k, v in
                report.get("metrics_by", {}).get("by_exit_policy", {}).items()]
    if pol_rows:
        keys = sorted({k for r in pol_rows for k in r
                       if not isinstance(r[k], (dict, list))})
        _write_csv(os.path.join(run_dir, "policy_metrics.csv"), pol_rows, keys)

    # candidate_ranking.csv + rejected_candidates.csv
    def _cand_rows(items):
        return [{"timeframe": c["timeframe"], "side": c["side"],
                 "entry_family": c["entry_family"], "exit_policy": c["exit_policy"],
                 "candidate_status": c["candidate_status"],
                 "candidate_quality_tier": c.get("candidate_quality_tier"),
                 "trades": c["trades"], "net_EV": c["net_EV"], "net_PF": c["net_PF"],
                 "win_rate": c["win_rate"], "max_drawdown": c["max_drawdown"],
                 "profit_capture_ratio": c["profit_capture_ratio"],
                 "gap_adverse_count": c.get("gap_adverse_count"),
                 "wf_mode": c.get("wf_mode"), "wf_folds_total": c.get("wf_folds_total"),
                 "wf_folds_passed": c.get("wf_folds_passed"),
                 "wf_pass_rate": c.get("wf_pass_rate"),
                 "walk_forward_status": c.get("walk_forward_status"),
                 "oos_net_EV": c.get("oos_net_EV"),
                 "rejection_reasons": ";".join(c["rejection_reasons"])} for c in items]
    ccols = ["timeframe", "side", "entry_family", "exit_policy", "candidate_status",
             "candidate_quality_tier", "trades", "net_EV", "net_PF", "win_rate",
             "max_drawdown", "profit_capture_ratio", "gap_adverse_count", "wf_mode",
             "wf_folds_total", "wf_folds_passed", "wf_pass_rate", "walk_forward_status",
             "oos_net_EV", "rejection_reasons"]
    _write_csv(os.path.join(run_dir, "candidate_ranking.csv"),
               _cand_rows(report.get("research_candidates", [])), ccols)
    _write_csv(os.path.join(run_dir, "rejected_candidates.csv"),
               _cand_rows(report.get("rejected_candidates", [])), ccols)

    # walk_forward_folds.csv (rolling WF evidence for the top candidates)
    wf_rows = report.get("walk_forward_folds", [])
    _write_csv(os.path.join(run_dir, "walk_forward_folds.csv"), wf_rows,
               ["combo", "fold_id", "train_start", "train_end", "test_start",
                "test_end", "trades_train", "trades_test", "net_EV_train",
                "net_EV_test", "PF_train", "PF_test", "drawdown_test",
                "candidate_passed_fold", "failure_reason"])

    # stability_matrix.csv (by symbol/tf/side/regime)
    stab_rows = []
    mb = report.get("metrics_by", {})
    for dim in ("by_symbol", "by_timeframe", "by_side", "by_regime"):
        for k, v in mb.get(dim, {}).items():
            stab_rows.append({"dimension": dim, "bucket": k,
                              "trades": v.get("trades"), "net_EV": v.get("net_EV"),
                              "net_PF": v.get("profit_factor_net"),
                              "win_rate": v.get("win_rate"),
                              "max_drawdown": v.get("max_drawdown")})
    _write_csv(os.path.join(run_dir, "stability_matrix.csv"), stab_rows,
               ["dimension", "bucket", "trades", "net_EV", "net_PF", "win_rate", "max_drawdown"])

    # summary.json (trimmed) + report.md
    summary = {k: v for k, v in report.items() if k not in ("metrics_by",)}
    summary["global_metrics"] = report.get("metrics_by", {}).get("global", {})
    with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    _write_md(os.path.join(run_dir, "report.md"), report)
    return run_dir.replace("\\", "/")


def _write_md(path: str, report: dict[str, Any]) -> None:
    g = report.get("metrics_by", {}).get("global", {})
    lines = [
        "# ResearchOps V10.8 - Adaptive Trailing Exit Lab (RESEARCH ONLY)",
        "",
        "> These candidates are HYPOTHESES, not signals. This is a policy "
        "comparison, NOT a tradable portfolio. Entries are baseline and the "
        "edge is NOT validated. NO LIVE.",
        "", f"- generated_at: {report.get('generated_at')}",
        f"- data_classification: {report.get('data_classification')}",
        f"- evaluation_type: {report.get('evaluation_type')}",
        f"- walk_forward_mode: {report.get('walk_forward_mode')} (is_rolling={report.get('wf_is_rolling')})",
        f"- walk_forward_status: {report.get('walk_forward_status')}",
        f"- gap_policy: {report.get('gap_policy')}",
        f"- comparison_not_portfolio: {report.get('comparison_not_portfolio')}",
        f"- entries_are_baseline_not_validated_edge: {report.get('entries_are_baseline_not_validated_edge')}",
        f"- candidates_are_hypotheses_not_signals: {report.get('candidates_are_hypotheses_not_signals')}",
        f"- edge_validated: {report.get('edge_validated')}",
        f"- trades_simulated: {report.get('trades_simulated')}",
        f"- research_candidates: {report.get('n_research_candidates')} "
        f"(research_candidate_only={report.get('n_research_candidate_only')}, "
        f"weak={report.get('n_weak_research_hypothesis')})",
        f"- rejected_candidates: {report.get('n_rejected_candidates')}",
        f"- side_concentration_warning: {report.get('side_concentration_warning')!r}",
        f"- regime_window_dependency_warning: {report.get('regime_window_dependency_warning')}",
        f"- multiple_comparisons_warning: {report.get('multiple_comparisons_warning')} "
        f"(n_combos_tested={report.get('n_combos_tested')}, "
        f"n_candidates_after_gates={report.get('n_candidates_after_gates')}, "
        f"false_discovery_risk={report.get('false_discovery_risk')})",
        f"- global_policy_comparison net_EV: {g.get('net_EV')} | net_PF: {g.get('profit_factor_net')}",
        f"- gap_adverse_count (global): {g.get('gap_adverse_count')}",
        f"- oi_regime_available: {report.get('oi_regime_available')}",
        f"- liquidations_available: {report.get('liquidations_available')}",
        "", "## Top research candidates (HYPOTHESES - NOT approved for paper/live, NOT signals)",
    ]
    for c in report.get("research_candidates", [])[:10]:
        lines.append(f"- [{c.get('candidate_quality_tier')}] "
                     f"{c['timeframe']}/{c['side']}/{c['entry_family']}/{c['exit_policy']}"
                     f" -> net_EV={c['net_EV']} net_PF={c['net_PF']} trades={c['trades']}"
                     f" wf_pass_rate={c.get('wf_pass_rate')} wf_folds={c.get('wf_folds_passed')}/{c.get('wf_folds_total')}")
    lines += ["", "## Safety",
              "- research_only: true", "- paper_ready: false", "- live_ready: false",
              "- can_send_real_orders: false", "- real_leverage_allowed: false",
              "- leverage_research_status: BLOCKED_NO_VALIDATED_EDGE",
              "- candidates are hypotheses, not signals",
              "- FINAL_RECOMMENDATION: NO LIVE", ""]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# --------------------------------------------------------------------------
# I. Plan + report-summary (pure)
# --------------------------------------------------------------------------

def trailing_exit_plan() -> dict[str, Any]:
    return {
        "objective": ("research-only comparison of dynamic profit-protection "
                      "exits (break-even lock, ATR/percent/structure trailing, "
                      "profit ladder, time-death) vs a fixed TP/SL/time baseline; "
                      "protect winners, let trends run, never give a winner back "
                      "to a loss - WITHOUT real trading"),
        "dataset_requirements": [
            "V10.6 sample dir from bitget-public-to-sample-v107",
            "<symbol>_<timeframe>_ohlcv.csv (timestamp,open,high,low,close,volume)",
            "<symbol>_funding.csv (timestamp,funding_rate) optional",
            "LONG_HISTORY_RESEARCH_READY needed for anything beyond research-only"],
        "entry_families": list(ENTRY_FAMILIES),
        "exit_policies": list(EXIT_POLICIES),
        "metrics": ["net_EV", "net_PF", "win_rate", "max_drawdown", "avg_R",
                    "profit_capture_ratio", "giveback_ratio", "MFE/MAE",
                    "exit_reason_distribution", "cost_stress_x2/x3"],
        "gates": ["min_trades", "net_EV>0", f"net_PF>={MIN_NET_PF}", "OOS net_EV>0",
                  "cost stress x2/x3 survives", "not single-symbol",
                  "profit_capture>=%.2f" % MIN_PROFIT_CAPTURE,
                  "giveback<=%.2f" % MAX_GIVEBACK, "time_death not dominant"],
        "same_bar_policy": "worst_case (default); best_case/open_path only as sensitivity",
        "gap_policy": "adverse_open (default): an adverse gap fills at the worse "
                      "open, never optimistically at the stop; favorable gaps do "
                      "NOT improve the base TP",
        "walk_forward_modes": {
            WF_NONE: "no OOS validation (weak at best)",
            WF_SPLIT: ("single chronological_oos_single_split - NOT a walk-forward; "
                       "caps candidates at WEAK_RESEARCH_HYPOTHESIS"),
            WF_ROLLING: ("REAL rolling walk-forward: multiple train->test folds, "
                         "test always after train, no future leakage; needs "
                         f">= {WF_MIN_FOLDS} folds for the top tier")},
        "regimes": ["trend_up", "trend_down", "range", "high_volatility",
                    "low_volatility", "funding_positive", "funding_negative",
                    "funding_extreme_positive", "funding_extreme_negative"],
        "aggressive_opportunity_simulation": True,
        "leverage_grid": list(LEVERAGE_GRID),
        "leverage_recommendation": "NO_REAL_LEVERAGE",
        "leverage_research_status_without_edge": "BLOCKED_NO_VALIDATED_EDGE",
        "candidates_are_hypotheses_not_signals": True,
        "comparison_not_portfolio": True,
        "limitations": ["no OI history", "no historical liquidations",
                        "exit research on BASELINE entries - entry edge NOT proven",
                        "INTERMEDIATE samples never exceed RESEARCH_CANDIDATE_ONLY",
                        "candidates are HYPOTHESES, not signals",
                        "global metrics are a POLICY COMPARISON, not a portfolio",
                        "no paper/live readiness, no backtester-for-real-money"],
        "candidate_quality_tiers": [CAND_REJECTED, CAND_WEAK, CAND_RESEARCH_ONLY,
                                    "never APPROVED_FOR_PAPER", "never APPROVED_FOR_LIVE"],
        **_safety_block(),
    }


def summarize_run(summary: dict[str, Any]) -> dict[str, Any]:
    cands = summary.get("research_candidates", [])
    rej = summary.get("rejected_candidates", [])
    best_by = {}
    for c in cands:
        key = f"{c['timeframe']}/{c['side']}"
        if key not in best_by:
            best_by[key] = f"{c['entry_family']}/{c['exit_policy']} net_EV={c['net_EV']}"
    worst = sorted(rej, key=lambda c: (c.get("max_drawdown") or 0), reverse=True)[:5]
    return {
        "data_classification": summary.get("data_classification"),
        "edge_validated": summary.get("edge_validated", False),
        "candidates_are_hypotheses_not_signals": True,
        "comparison_not_portfolio": True,
        "walk_forward_mode": summary.get("walk_forward_mode"),
        "wf_is_rolling": summary.get("wf_is_rolling"),
        "walk_forward_status": summary.get("walk_forward_status"),
        "gap_policy": summary.get("gap_policy"),
        "trades_simulated": summary.get("trades_simulated"),
        "n_research_candidates": summary.get("n_research_candidates"),
        "n_research_candidate_only": summary.get("n_research_candidate_only"),
        "n_weak_research_hypothesis": summary.get("n_weak_research_hypothesis"),
        "n_rejected_candidates": summary.get("n_rejected_candidates"),
        "side_concentration_warning": summary.get("side_concentration_warning"),
        "regime_window_dependency_warning": summary.get("regime_window_dependency_warning"),
        "multiple_comparisons_warning": summary.get("multiple_comparisons_warning"),
        "n_combos_tested": summary.get("n_combos_tested"),
        "n_candidates_after_gates": summary.get("n_candidates_after_gates"),
        "false_discovery_risk": summary.get("false_discovery_risk"),
        "leverage_research_status": summary.get("aggressive_opportunity", {}).get(
            "leverage_research_status", "BLOCKED_NO_VALIDATED_EDGE"),
        "top_research_candidates": cands[:10],
        "best_policy_by_side_timeframe": best_by,
        "worst_fragility": [{"combo": f"{c['timeframe']}/{c['side']}/{c['entry_family']}/{c['exit_policy']}",
                             "max_drawdown": c.get("max_drawdown"),
                             "why": ";".join(c.get("rejection_reasons", []))} for c in worst],
        "approved_for_paper": False, "approved_for_live": False,
        "real_leverage_allowed": False,
        **_safety_block(),
    }


def latest_run_summary(output_dir: str | None = None) -> dict[str, Any] | None:
    base = output_dir or OUTPUT_ROOT
    try:
        if not os.path.isdir(base):
            return None
        runs = sorted(d for d in os.listdir(base)
                      if os.path.isdir(os.path.join(base, d)))
        if not runs:
            return None
        spath = os.path.join(base, runs[-1], "summary.json")
        if not os.path.isfile(spath):
            return None
        with open(spath, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None
