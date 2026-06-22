"""ResearchOps V10.10 — Micro-Scalp Shadow Tournament (research/shadow only).

A PURE, offline, deterministic engine that simulates the user's micro-scalping
idea — many small trades, fast entry, protect profit, close in green, repeat —
as if it were trading, measuring everything, WITHOUT touching the exchange or
real money. It is a SHADOW simulation: no orders, no leverage, no sizing, no
paper filter, no live.

Hard guarantees:
- entries are no-lookahead (decided on a closed bar, filled next bar open);
- same-bar stop+TP => worst case; adverse gaps fill at the worse open;
- fees + slippage + spread + (optional) funding are always subtracted;
- candidates are HYPOTHESES, capped at SHADOW_TEST_CANDIDATE and — with the
  current public/intermediate data (no OI/liquidations, provider unverified) —
  in practice never above WEAK_RESEARCH_HYPOTHESIS;
- NOTHING flips paper_ready/live_ready/can_send_real_orders.

FINAL_RECOMMENDATION: NO LIVE.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import adaptive_trailing_exit_v10_8 as lab

TOOL_VERSION = "v10.10"
OUTPUT_ROOT = "reports/research/v10_10"
DAY_MS = 86_400_000

STRATEGY_FAMILIES = ("micro_breakout", "micro_reversal", "momentum_continuation",
                     "mean_reversion_snapback", "orderbook_pressure_proxy",
                     "trend_micro_pullback", "volatility_burst_scalp")
EXIT_POLICIES = ("instant_green_lock", "micro_profit_take", "green_or_scratch",
                 "runner_mode", "kill_if_not_green_fast", "max_loss_hard_stop")
SIDES = ("LONG", "SHORT")
LEVERAGE_GRID = (1, 2, 3, 5, 10, 20)
DANGEROUS_LEVERAGE = 20

CAND_REJECTED = "REJECTED"
CAND_WEAK = "WEAK_RESEARCH_HYPOTHESIS"
CAND_SHADOW = "SHADOW_TEST_CANDIDATE"           # never APPROVED_FOR_PAPER/LIVE
_TIER_RANK = {CAND_REJECTED: 0, CAND_WEAK: 1, CAND_SHADOW: 2}

# micro-scalp gates (research heuristics)
MIN_NET_PF = 1.2
MIN_CLOSED_GREEN_RATE = 0.5
MAX_GREEN_TO_RED_RATE = 0.35
MAX_DRAWDOWN_FRACTION = 0.5
_WARMUP = 30
_FORBIDDEN_SEG = ("raw", "backup", "backups", "vault", "vaults", "training_exports",
                  "secret", "secrets", "credential", "credentials")
_FORBIDDEN_SUF = (".env", ".db", ".sqlite", ".zip", ".tar", ".gz", ".pem", ".key")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


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


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _safe_output_base(output_dir: str | None) -> str:
    base = output_dir or OUTPUT_ROOT
    if not isinstance(base, str) or not base.strip() or "%" in base:
        return OUTPUT_ROOT
    if ".." in base.replace("\\", "/").split("/"):
        return OUTPUT_ROOT
    try:
        real = os.path.realpath(base).replace("\\", "/")
    except Exception:
        return OUTPUT_ROOT
    for s in (x.lower() for x in real.split("/") if x):
        if s in _FORBIDDEN_SEG or s.endswith(_FORBIDDEN_SUF) or ".env" in s:
            return OUTPUT_ROOT
    return base


# --------------------------------------------------------------------------
# Cost model
# --------------------------------------------------------------------------

class MicroCosts:
    def __init__(self, cost_bps=6.0, slippage_bps=4.0, spread_bps=2.0,
                 funding_mode=True, latency_bars=1):
        self.cost_bps = float(cost_bps)
        self.slippage_bps = float(slippage_bps)
        self.spread_bps = float(spread_bps)
        self.funding_mode = bool(funding_mode)
        self.latency_bars = max(1, int(latency_bars))

    def round_trip_fraction(self, mult: float = 1.0) -> float:
        return ((2.0 * self.cost_bps + 2.0 * self.slippage_bps + self.spread_bps)
                * mult / 10_000.0)

    def as_dict(self) -> dict[str, Any]:
        return {"cost_bps": self.cost_bps, "slippage_bps": self.slippage_bps,
                "spread_bps": self.spread_bps, "funding_mode": self.funding_mode,
                "latency_bars": self.latency_bars}


# --------------------------------------------------------------------------
# G. Micro-scalp entry families (no-lookahead)
# --------------------------------------------------------------------------

def generate_micro_entries(*, symbol, timeframe, side, family, bars, funding,
                           cooldown=3, max_entries=80, n=10) -> list[dict[str, Any]]:
    if len(bars) < _WARMUP + 2:
        return []
    atr = lab.atr_series(bars)
    emaf = lab.ema_series(bars, 12)
    emas = lab.ema_series(bars, 26)
    is_long = side.upper() == "LONG"
    out: list[dict[str, Any]] = []
    last_i = -10_000
    for i in range(_WARMUP, len(bars) - 1):
        if i - last_i < cooldown:
            continue
        a = atr[i]
        if a is None or a <= 0:
            continue
        c, o = bars[i]["close"], bars[i]["open"]
        ef, es = emaf[i], emas[i]
        rng = bars[i]["high"] - bars[i]["low"]
        recent = [bars[k]["high"] - bars[k]["low"] for k in range(i - 10, i)]
        avg_rng = (sum(recent) / len(recent)) if recent else 0.0
        win_hi = max(b["high"] for b in bars[i - n:i])
        win_lo = min(b["low"] for b in bars[i - n:i])
        vols = [bars[k]["volume"] for k in range(i - 10, i)]
        avg_vol = (sum(vols) / len(vols)) if vols else 0.0
        sig, reason, q = False, "", 0.5
        if family == "micro_breakout":
            if is_long and c > win_hi:
                sig, reason, q = True, "break_high", 0.6
            elif not is_long and c < win_lo:
                sig, reason, q = True, "break_low", 0.6
        elif family == "micro_reversal":
            # exhaustion sweep of recent extreme, close back inside (contrarian)
            if is_long and bars[i]["low"] < win_lo and c > o:
                sig, reason, q = True, "sweep_low_reclaim", 0.55
            elif not is_long and bars[i]["high"] > win_hi and c < o:
                sig, reason, q = True, "sweep_high_reject", 0.55
        elif family == "momentum_continuation":
            strong = avg_vol > 0 and bars[i]["volume"] > 1.3 * avg_vol
            if is_long and c > o and (ef is None or es is None or ef > es) and strong:
                sig, reason, q = True, "momentum_up_vol", 0.6
            elif not is_long and c < o and (ef is None or es is None or ef < es) and strong:
                sig, reason, q = True, "momentum_down_vol", 0.6
        elif family == "mean_reversion_snapback":
            if es is not None:
                dev = (c - es) / es if es else 0.0
                if is_long and dev < -0.02 and c > bars[i - 1]["close"]:
                    sig, reason, q = True, "snapback_up", 0.5
                elif not is_long and dev > 0.02 and c < bars[i - 1]["close"]:
                    sig, reason, q = True, "snapback_down", 0.5
        elif family == "orderbook_pressure_proxy":
            # NO real orderbook -> proxy via close position in bar range + volume
            pos = ((c - bars[i]["low"]) / rng) if rng > 0 else 0.5
            press = avg_vol > 0 and bars[i]["volume"] > 1.2 * avg_vol
            if is_long and pos > 0.7 and press:
                sig, reason, q = True, "buy_pressure_proxy", 0.5
            elif not is_long and pos < 0.3 and press:
                sig, reason, q = True, "sell_pressure_proxy", 0.5
        elif family == "trend_micro_pullback":
            if ef is not None and es is not None:
                if is_long and ef > es and bars[i - 1]["low"] < ef <= c:
                    sig, reason, q = True, "micro_pullback_up", 0.55
                elif not is_long and ef < es and bars[i - 1]["high"] > ef >= c:
                    sig, reason, q = True, "micro_pullback_down", 0.55
        elif family == "volatility_burst_scalp":
            if avg_rng > 0 and rng > 1.5 * avg_rng:
                if is_long and c >= o:
                    sig, reason, q = True, "vol_burst_up", 0.55
                elif not is_long and c < o:
                    sig, reason, q = True, "vol_burst_down", 0.55
        if not sig:
            continue
        ei = i + 1
        if ei >= len(bars) or bars[ei]["open"] <= 0:
            continue
        fr = None
        if funding:
            prior = [x["rate"] for x in funding if x["ts"] <= bars[i]["ts"]]
            fr = prior[-1] if prior else None
        out.append({"symbol": symbol, "timeframe": timeframe, "side": side.upper(),
                    "strategy_family": family, "signal_idx": i, "entry_idx": ei,
                    "entry_ts": bars[ei]["ts"], "entry_price": bars[ei]["open"],
                    "entry_reason": reason, "atr": a, "setup_quality_score": q,
                    "regime_snapshot": _regime(ef, es, c),
                    "orderbook_real": False, "funding_snapshot": fr,
                    "no_lookahead": True})
        last_i = i
        if len(out) >= max_entries:
            break
    return out


def _regime(ef, es, c):
    if ef is None or es is None:
        return "range"
    if ef > es and c >= es:
        return "trend_up"
    if ef < es and c <= es:
        return "trend_down"
    return "range"


# --------------------------------------------------------------------------
# H+I. Shadow execution simulation with close-in-green policies
# --------------------------------------------------------------------------

def simulate_micro_trade(bars, atr, entry, *, policy, params, costs: MicroCosts,
                         funding, gap_policy="adverse_open", same_bar="worst_case"):
    side = entry["side"]
    is_long = side == "LONG"
    ei = entry["entry_idx"] + (costs.latency_bars - 1)  # latency
    if ei >= len(bars):
        return None
    ep = bars[ei]["open"]
    if ep <= 0:
        return None
    tp_pct = float(params.get("tp_pct", 0.004))      # small targets
    sl_pct = float(params.get("sl_pct", 0.005))
    max_hold = int(params.get("max_hold", 12))
    green_bps = float(params.get("green_lock_bps", 8.0))
    kill_bars = int(params.get("kill_bars", 4))
    runner_atr = float(params.get("runner_atr", 1.5))
    risk = ep * sl_pct
    if risk <= 0:
        return None
    rt = costs.round_trip_fraction()
    buf = rt / 2.0
    stop = ep - risk if is_long else ep + risk
    tp = ep + tp_pct * ep if is_long else ep - tp_pct * ep
    high_since = low_since = ep
    be_locked = False
    went_green = False
    last = min(ei + max_hold, len(bars) - 1)
    exit_reason, exit_price, exit_idx = "END_OF_DATA", bars[last]["close"], last
    gap_adverse = same_bar_amb = False

    def fav_frac(px):
        return ((px - ep) / ep) if is_long else ((ep - px) / ep)

    for j in range(ei, last + 1):
        op, hi, lo, cl = bars[j]["open"], bars[j]["high"], bars[j]["low"], bars[j]["close"]
        hit_stop = (lo <= stop) if is_long else (hi >= stop)
        hit_tp = (hi >= tp) if is_long else (lo <= tp)
        stop_fill = (min(stop, op) if is_long else max(stop, op)) if gap_policy == "adverse_open" else stop
        if hit_stop and hit_tp:
            same_bar_amb = True
            gap_adverse = stop_fill != stop
            exit_reason, exit_price, exit_idx = "SAME_BAR_WORST_CASE", stop_fill, j
            break
        if hit_stop:
            gap_adverse = stop_fill != stop
            exit_reason = "BREAK_EVEN" if be_locked and abs(stop - ep) <= buf * ep * 2 else "STOP"
            exit_price, exit_idx = stop_fill, j
            break
        if hit_tp:
            exit_reason, exit_price, exit_idx = "TP", tp, j
            break
        # update excursions, then manage for NEXT bar (no lookahead)
        high_since, low_since = max(high_since, hi), min(low_since, lo)
        mfe = fav_frac(high_since if is_long else low_since)
        if mfe * 10_000.0 >= green_bps:
            went_green = True
        held = j - ei
        # policy management
        if policy in ("instant_green_lock", "green_or_scratch", "runner_mode") and mfe * 10_000.0 >= green_bps:
            be = ep + buf * ep if is_long else ep - buf * ep
            new = max(stop, be) if is_long else min(stop, be)
            if new != stop:
                stop, be_locked = new, True
        if policy == "micro_profit_take":
            pass  # relies on fixed tp
        if policy == "green_or_scratch" and be_locked and held >= 1:
            # if it went green then pulls back to BE, we exit scratch on a close
            cur = fav_frac(cl)
            if cur <= buf:
                exit_reason, exit_price, exit_idx = "GREEN_OR_SCRATCH", cl, j
                break
        if policy == "runner_mode" and be_locked:
            a = atr[j] if atr[j] is not None else entry["atr"]
            cand = (high_since - runner_atr * a) if is_long else (low_since + runner_atr * a)
            stop = (max(stop, cand) if is_long else min(stop, cand))
        if policy == "kill_if_not_green_fast" and held >= kill_bars and not went_green:
            exit_reason, exit_price, exit_idx = "KILL_NOT_GREEN", cl, j
            break
        # max_loss_hard_stop uses the fixed stop only (already handled by hit_stop)

    gross = ((exit_price - ep) / ep) if is_long else ((ep - exit_price) / ep)
    held = exit_idx - ei + 1
    funding_frac = 0.0
    if costs.funding_mode and funding:
        fsum = sum(x["rate"] for x in funding if entry["entry_ts"] <= x["ts"] <= bars[exit_idx]["ts"])
        funding_frac = (-fsum) if is_long else fsum
    net = gross - rt + funding_frac
    mfe = fav_frac(high_since if is_long else low_since)
    mae = ((ep - low_since) / ep) if is_long else ((high_since - ep) / ep)
    closed_green = net > 0
    return {"symbol": entry["symbol"], "timeframe": entry["timeframe"], "side": side,
            "strategy_family": entry["strategy_family"], "policy": policy,
            "entry_ts": entry["entry_ts"], "entry_price": ep, "exit_price": exit_price,
            "exit_reason": exit_reason, "gross_pnl": gross, "net_pnl": net,
            "net_pnl_bps": net * 10_000.0, "R": net / sl_pct, "time_in_trade": held,
            "mfe": mfe, "mae": mae,
            "profit_capture": (max(0.0, gross) / mfe) if mfe > 1e-12 else 0.0,
            "giveback": ((mfe - max(0.0, gross)) / mfe) if mfe > 1e-12 else 0.0,
            "went_green": went_green, "green_to_red_failure": bool(went_green and net <= 0),
            "break_even_locked": be_locked, "closed_green": closed_green,
            "closed_red": (not closed_green), "same_bar_ambiguity": same_bar_amb,
            "gap_adverse": gap_adverse, "fee": 2.0 * costs.cost_bps / 10_000.0,
            "slippage": 2.0 * costs.slippage_bps / 10_000.0,
            "spread_cost": costs.spread_bps / 10_000.0, "funding": funding_frac,
            "sl_pct": sl_pct, "orderbook_real": False,
            "liquidation_distance_estimate": None, "ruin_risk_proxy": None,
            "regime_snapshot": entry["regime_snapshot"], "params": params}


# --------------------------------------------------------------------------
# Metrics (micro-scalp specific incl closed-green)
# --------------------------------------------------------------------------

def micro_metrics(trades, costs: MicroCosts) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "net_EV": 0.0, "net_PF": 0.0, "closed_green_rate": 0.0}
    nets = [t["net_pnl"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gains, loss_sum = sum(wins), -sum(losses)
    eq = peak = max_dd = 0.0
    streak = max_streak = 0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        streak = streak + 1 if x <= 0 else 0
        max_streak = max(max_streak, streak)
    dist: dict[str, int] = {}
    for t in trades:
        dist[t["exit_reason"]] = dist.get(t["exit_reason"], 0) + 1
    stress = {}
    for m in (2, 3):
        extra = costs.round_trip_fraction() * (m - 1)
        sn = [x - extra for x in nets]
        stress[f"x{m}"] = round(sum(sn) / n, 6)
    fees = sum(t["fee"] + t["slippage"] + t["spread_cost"] for t in trades)
    return {
        "trades": n, "net_EV": round(sum(nets) / n, 6),
        "net_EV_bps": round(sum(nets) / n * 10_000.0, 3),
        "gross_EV": round(sum(t["gross_pnl"] for t in trades) / n, 6),
        "net_PF": round((gains / loss_sum) if loss_sum > 0 else (math.inf if gains > 0 else 0.0), 4),
        "win_rate": round(len(wins) / n, 4),
        "went_green_rate": round(sum(1 for t in trades if t["went_green"]) / n, 4),
        "closed_green_rate": round(sum(1 for t in trades if t["closed_green"]) / n, 4),
        "green_to_red_rate": round(sum(1 for t in trades if t["green_to_red_failure"]) / n, 4),
        "avg_win": round(sum(wins) / len(wins), 6) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 6) if losses else 0.0,
        "max_drawdown": round(max_dd, 6), "max_consecutive_losses": max_streak,
        "avg_time_in_trade": round(sum(t["time_in_trade"] for t in trades) / n, 2),
        "fee_drag_total": round(fees, 6),
        "profit_capture_ratio": round(sum(t["profit_capture"] for t in trades) / n, 4),
        "giveback_ratio": round(sum(t["giveback"] for t in trades) / n, 4),
        "same_bar_ambiguity_count": sum(1 for t in trades if t["same_bar_ambiguity"]),
        "gap_adverse_count": sum(1 for t in trades if t["gap_adverse"]),
        "exit_reason_distribution": dist,
        "cost_stress_x2": stress["x2"], "cost_stress_x3": stress["x3"]}


# --------------------------------------------------------------------------
# J. Compounding simulation
# --------------------------------------------------------------------------

def compounding_sim(trades, *, initial_capital=100.0, risk_per_trade=0.01,
                    max_daily_loss=0.1, max_consecutive_losses=8,
                    compound_mode="capped_fraction") -> dict[str, Any]:
    nets = [t["net_pnl"] for t in sorted(trades, key=lambda x: x["entry_ts"])]
    n = len(nets)
    ev = (sum(nets) / n) if n else 0.0
    equity = initial_capital
    peak = initial_capital
    max_dd = 0.0
    curve = [round(equity, 4)]
    streak = max_streak = 0
    breaches = 0
    for x in nets:
        if compound_mode == "none":
            stake = initial_capital
        elif compound_mode == "fixed_fraction":
            stake = equity * risk_per_trade / max(1e-9, 0.005)
        else:  # capped_fraction
            stake = min(equity, equity * risk_per_trade / max(1e-9, 0.005))
        equity += stake * x
        equity = max(0.0, equity)
        curve.append(round(equity, 4))
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak > 0 else 0.0)
        streak = streak + 1 if x <= 0 else 0
        max_streak = max(max_streak, streak)
        if equity <= initial_capital * (1 - max_daily_loss):
            breaches += 1
        if equity <= 0 or streak >= max_consecutive_losses:
            pass
    status = "OK"
    if ev <= 0:
        status = "COMPOUNDING_DANGEROUS_NEGATIVE_EV"
    elif max_dd >= MAX_DRAWDOWN_FRACTION:
        status = "COMPOUNDING_HIGH_DRAWDOWN"
    return {"initial_capital": initial_capital, "final_equity": round(equity, 4),
            "compound_mode": compound_mode, "net_EV_per_trade": round(ev, 6),
            "max_drawdown_fraction": round(max_dd, 4),
            "longest_losing_streak": max_streak, "daily_loss_breaches": breaches,
            "risk_of_ruin_proxy": round(min(1.0, max_streak * risk_per_trade), 4),
            "equity_curve": curve[:500],
            "compounding_helped_or_hurt": ("hurt" if equity < initial_capital else "helped"),
            "compounding_status": status, **_safety()}


def leverage_sim(metrics, *, sl_pct, edge_validated=False) -> dict[str, Any]:
    rows = []
    net_ev = metrics.get("net_EV", 0.0)
    mm = 0.005
    for lev in LEVERAGE_GRID:
        liq = max(0.0, (1.0 / lev) - mm)
        safe = sl_pct < liq
        dang = (lev >= DANGEROUS_LEVERAGE) or not safe or net_ev <= 0
        rows.append({"leverage": lev, "pnl_scaled": round(net_ev * lev, 6),
                     "liquidation_distance_estimate": round(liq, 5),
                     "stop_inside_safe_zone": safe,
                     "dangerous_leverage_flag": "DANGEROUS_RESEARCH_ONLY" if dang else "research_only"})
    return {"leverage_grid": list(LEVERAGE_GRID), "rows": rows,
            "edge_validated": bool(edge_validated),
            "leverage_research_status": "OPEN_RESEARCH" if edge_validated else "BLOCKED_NO_VALIDATED_EDGE",
            "leverage_recommendation": "NO_REAL_LEVERAGE", "real_leverage_allowed": False,
            **_safety()}


# --------------------------------------------------------------------------
# K. Tournament + gating
# --------------------------------------------------------------------------

def _param_sets(policy: str) -> list[dict[str, Any]]:
    base = {"tp_pct": 0.004, "sl_pct": 0.005, "max_hold": 12, "green_lock_bps": 8.0,
            "kill_bars": 4, "runner_atr": 1.5}
    out = [dict(base)]
    if policy == "micro_profit_take":
        out.append({**base, "tp_pct": 0.006})
    elif policy == "runner_mode":
        out.append({**base, "runner_atr": 2.0})
    return out


def _cid(c, *, gap_policy, wf_mode, costs: MicroCosts) -> str:
    ph = hashlib.sha256(json.dumps(c.get("params", {}), sort_keys=True).encode()).hexdigest()[:10]
    return (f"{c['timeframe']}/{c['side']}/{c['strategy_family']}/{c['policy']}"
            f"/p{ph}/gp:{gap_policy}/wf:{wf_mode}/c{costs.cost_bps}-{costs.slippage_bps}-{costs.spread_bps}")


def _window_pass(trades, window_days, costs) -> bool:
    if not trades:
        return False
    end = max(t["entry_ts"] for t in trades)
    cutoff = end - window_days * DAY_MS
    sub = [t for t in trades if t["entry_ts"] >= cutoff]
    if len(sub) < 5:
        return False
    return micro_metrics(sub, costs).get("net_EV", 0.0) > 0


def _evaluate(trades, *, costs, min_trades, windows, max_tier) -> dict[str, Any]:
    m = micro_metrics(trades, costs)
    rej = []
    if m["trades"] < min_trades:
        rej.append(f"too_few_trades:{m['trades']}<{min_trades}")
    if m.get("net_EV", 0) <= 0:
        rej.append("net_EV<=0")
    pf = m.get("net_PF", 0)
    if not isinstance(pf, str) and pf < MIN_NET_PF:
        rej.append(f"net_PF<{MIN_NET_PF}")
    if m.get("closed_green_rate", 0) < MIN_CLOSED_GREEN_RATE:
        rej.append("closed_green_rate_too_low")
    if m.get("green_to_red_rate", 1) > MAX_GREEN_TO_RED_RATE:
        rej.append("green_to_red_rate_too_high")
    if m.get("cost_stress_x2", 0) <= 0 or m.get("cost_stress_x3", 0) <= 0:
        rej.append("cost_stress_kills_edge")
    if m.get("max_drawdown", 0) >= MAX_DRAWDOWN_FRACTION:
        rej.append("max_drawdown_too_high")
    by_sym: dict[str, list] = {}
    for t in trades:
        by_sym.setdefault(t["symbol"], []).append(t)
    if len(by_sym) >= 3 and sum(1 for v in by_sym.values() if sum(x["net_pnl"] for x in v) > 0) <= 1:
        rej.append("works_only_on_one_symbol")
    wf = lab.rolling_walk_forward(
        [{"entry_ts": t["entry_ts"], "net_ret": t["net_pnl"], "gross_ret": t["gross_pnl"],
          "R": t["R"], "held_bars": t["time_in_trade"], "exit_reason": t["exit_reason"],
          "mfe": t["mfe"], "mae": t["mae"], "profit_capture": t["profit_capture"],
          "giveback": t["giveback"], "be_activated": t["break_even_locked"],
          "trail_activated": False, "time_to_lock": None, "same_bar_ambiguous": t["same_bar_ambiguity"],
          "fee_frac": t["fee"], "slippage_frac": t["slippage"], "funding_frac": t["funding"],
          "regimes": [t["regime_snapshot"]], "sl_pct": t["sl_pct"], "symbol": t["symbol"]}
         for t in trades],
        costs=lab.Costs(costs.cost_bps, costs.slippage_bps, costs.funding_mode))
    windows_passed = sum(1 for w in windows if _window_pass(trades, w, costs))
    if wf["walk_forward_status"] != lab.WF_STATUS_OK or wf["wf_pass_rate"] < 0.34:
        rej.append("rolling_wf_failed_or_insufficient")
    if windows_passed < 2:
        rej.append("not_stable_across_2_windows")
    if rej:
        tier = CAND_REJECTED
    else:
        base = CAND_SHADOW if (windows_passed >= 2 and wf["wf_pass_rate"] >= 0.6) else CAND_WEAK
        tier = base if _TIER_RANK[base] <= _TIER_RANK[max_tier] else max_tier
    return {"tier": tier, "rejection_reasons": rej, "metrics": m,
            "wf_pass_rate": wf["wf_pass_rate"], "wf_status": wf["walk_forward_status"],
            "windows_passed": windows_passed, "windows_tested": len(windows)}


def run_micro_scalp_tournament(*, sample_dir, symbols, timeframes, sides,
                               strategy_families, cost_bps=6.0, slippage_bps=4.0,
                               spread_bps=2.0, latency_bars=1, funding_mode=True,
                               min_trades=30, windows=None, walk_forward_mode="rolling",
                               gap_policy="adverse_open", max_grid_combos=500, seed=7,
                               initial_capital=100.0, risk_per_trade=0.01,
                               max_daily_loss=0.1, max_consecutive_losses=8,
                               compound_mode="capped_fraction",
                               data_classification=lab.CLS_INTERMEDIATE) -> dict[str, Any]:
    windows = windows or [90, 180]
    costs = MicroCosts(cost_bps, slippage_bps, spread_bps, funding_mode, latency_bars)
    families = [f for f in strategy_families if f in STRATEGY_FAMILIES]
    sides = [s.upper() for s in sides if s.upper() in SIDES]
    timeframes = [t.lower() for t in timeframes]
    report: dict[str, Any] = {
        "tool_version": TOOL_VERSION, "generated_at": _now_iso(), "sample_dir": sample_dir,
        "symbols": symbols, "timeframes": timeframes, "sides": sides,
        "strategy_families": families, "cost_model": costs.as_dict(),
        "gap_policy": gap_policy, "walk_forward_mode": walk_forward_mode,
        "windows": windows, "data_classification": data_classification,
        "edge_validated": False, "comparison_not_portfolio": True,
        "candidates_are_hypotheses_not_signals": True, "orderbook_real": False,
        "missing_oi_historical": True, "missing_liquidations": True,
        "errors": [], "warnings": [], "candidates": [], "_all_trades": [],
        "_journal": [], **_safety()}
    if not (isinstance(sample_dir, str) and os.path.isdir(sample_dir)):
        report["errors"].append("sample_dir_not_found")
        return report
    if not (families and sides and timeframes and symbols):
        report["errors"].append("nothing_to_run")
        return report

    # current data => SHADOW_TEST_CANDIDATE not attainable (no OI/liq, unverified)
    max_tier = CAND_WEAK
    report["max_candidate_quality_tier"] = max_tier
    report["warnings"].append("candidate_tier_capped_to_weak (no OI/liquidations, provider unverified, intermediate data)")

    bars_cache, atr_cache, fund_cache, entries = {}, {}, {}, {}
    for sym in symbols:
        fp = os.path.join(sample_dir, f"{sym}_funding.csv")
        fund_cache[sym] = lab.load_funding(fp) if os.path.isfile(fp) else []
        for tf in timeframes:
            op = os.path.join(sample_dir, f"{sym}_{tf}_ohlcv.csv")
            if not os.path.isfile(op):
                continue
            bars = lab.load_ohlcv(op)
            if len(bars) < _WARMUP + 5:
                continue
            bars_cache[(sym, tf)] = bars
            atr_cache[(sym, tf)] = lab.atr_series(bars)
            for side in sides:
                for fam in families:
                    entries[(sym, tf, side, fam)] = generate_micro_entries(
                        symbol=sym, timeframe=tf, side=side, family=fam,
                        bars=bars, funding=fund_cache[sym])

    combos = []
    for tf in timeframes:
        for side in sides:
            for fam in families:
                for pol in EXIT_POLICIES:
                    for params in _param_sets(pol):
                        combos.append((tf, side, fam, pol, params))
    import random as _r
    if max_grid_combos and len(combos) > max_grid_combos:
        rng = _r.Random(seed)
        rng.shuffle(combos)
        combos = combos[:max_grid_combos]
    report["combos_evaluated"] = len(combos)

    all_trades = []
    candidates = []
    for (tf, side, fam, pol, params) in combos:
        ctrades = []
        for sym in symbols:
            ents = entries.get((sym, tf, side, fam))
            if not ents:
                continue
            bars, atr = bars_cache[(sym, tf)], atr_cache[(sym, tf)]
            for e in ents:
                tr = simulate_micro_trade(bars, atr, e, policy=pol, params=params,
                                          costs=costs, funding=fund_cache[sym],
                                          gap_policy=gap_policy)
                if tr is not None:
                    ctrades.append(tr)
        if not ctrades:
            continue
        all_trades.extend(ctrades)
        ev = _evaluate(ctrades, costs=costs, min_trades=min_trades, windows=windows, max_tier=max_tier)
        m = ev["metrics"]
        candidates.append({
            "candidate_id": _cid(ctrades[0], gap_policy=gap_policy, wf_mode=walk_forward_mode, costs=costs),
            "timeframe": tf, "side": side, "strategy_family": fam, "exit_policy": pol,
            "params": params, "final_tier": ev["tier"], "rejection_reasons": ev["rejection_reasons"],
            "trades": m["trades"], "net_EV": m.get("net_EV"), "net_EV_bps": m.get("net_EV_bps"),
            "net_PF": m.get("net_PF"), "closed_green_rate": m.get("closed_green_rate"),
            "green_to_red_rate": m.get("green_to_red_rate"), "max_drawdown": m.get("max_drawdown"),
            "cost_stress_x2": m.get("cost_stress_x2"), "wf_pass_rate": ev["wf_pass_rate"],
            "windows_passed": ev["windows_passed"], "windows_tested": ev["windows_tested"],
            "metrics": m})

    report["trades_simulated"] = len(all_trades)
    accepted = [c for c in candidates if c["final_tier"] != CAND_REJECTED]
    rejected = [c for c in candidates if c["final_tier"] == CAND_REJECTED]
    accepted.sort(key=lambda c: (_TIER_RANK.get(c["final_tier"], 0), c.get("net_EV") or -9), reverse=True)
    report["candidates"] = accepted[:25]
    report["rejected_candidates"] = rejected[:50]
    report["n_candidates"] = len(accepted)
    report["n_shadow_test_candidate"] = sum(1 for c in accepted if c["final_tier"] == CAND_SHADOW)
    report["n_weak"] = sum(1 for c in accepted if c["final_tier"] == CAND_WEAK)
    report["n_rejected"] = len(rejected)
    g = micro_metrics(all_trades, costs)
    report["global_metrics"] = g
    report["global_closed_green_rate"] = g.get("closed_green_rate")
    report["global_net_EV"] = g.get("net_EV")
    acc_sides = {c["side"] for c in accepted}
    report["side_concentration_warning"] = f"{acc_sides.pop()}_ONLY" if len(acc_sides) == 1 else ""
    nc = report.get("combos_evaluated", 0)
    report["false_discovery_risk"] = "HIGH" if (nc >= 20 and len(accepted) / max(1, nc) < 0.1) else ("MODERATE" if nc >= 20 else "LOW")
    best = accepted[0] if accepted else (candidates[0] if candidates else None)
    if best is not None:
        report["compounding"] = compounding_sim(
            [t for t in all_trades], initial_capital=initial_capital,
            risk_per_trade=risk_per_trade, max_daily_loss=max_daily_loss,
            max_consecutive_losses=max_consecutive_losses, compound_mode=compound_mode)
        report["aggressive_opportunity"] = leverage_sim(
            best["metrics"], sl_pct=float(best["params"].get("sl_pct", 0.005)), edge_validated=False)
    # shadow journal rows (sample of would-be decisions)
    for t in all_trades[:2000]:
        report["_journal"].append({
            "timestamp": t["entry_ts"], "would_enter": True, "would_exit": True,
            "symbol": t["symbol"], "side": t["side"], "strategy": t["strategy_family"],
            "reason": t.get("exit_reason"), "entry_price": t["entry_price"],
            "exit_price": t["exit_price"], "net_result": t["net_pnl"],
            "closed_green": t["closed_green"], "why_skipped": "",
            "why_closed": t["exit_reason"], "confidence_score": 0.0,
            "setup_quality": t.get("params", {}).get("tp_pct"), "final_decision": "SHADOW_ONLY"})
    report["_all_trades"] = all_trades
    return report


# --------------------------------------------------------------------------
# L. Reports + shadow journal writers
# --------------------------------------------------------------------------

def _write_csv(path, rows, header):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})


def write_micro_reports(report, output_dir=None) -> str:
    base = _safe_output_base(output_dir)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = os.path.join(base, run_id)
    os.makedirs(run_dir, exist_ok=True)
    trades = report.pop("_all_trades", [])
    journal = report.pop("_journal", [])
    if trades:
        _write_csv(os.path.join(run_dir, "micro_scalp_trades.csv"), trades,
                   ["symbol", "timeframe", "side", "strategy_family", "policy", "entry_ts",
                    "entry_price", "exit_price", "exit_reason", "net_pnl", "net_pnl_bps",
                    "went_green", "closed_green", "green_to_red_failure", "time_in_trade",
                    "same_bar_ambiguity", "gap_adverse"])
    cand_cols = ["candidate_id", "timeframe", "side", "strategy_family", "exit_policy",
                 "final_tier", "trades", "net_EV", "net_PF", "closed_green_rate",
                 "green_to_red_rate", "max_drawdown", "cost_stress_x2", "wf_pass_rate",
                 "windows_passed", "windows_tested", "rejection_reasons"]

    def rows(items):
        return [{**{k: c.get(k) for k in cand_cols if k != "rejection_reasons"},
                 "rejection_reasons": ";".join(c.get("rejection_reasons", []))} for c in items]
    _write_csv(os.path.join(run_dir, "micro_scalp_candidate_ranking.csv"), rows(report.get("candidates", [])), cand_cols)
    _write_csv(os.path.join(run_dir, "micro_scalp_rejected.csv"), rows(report.get("rejected_candidates", [])), cand_cols)
    sm = report.get("global_metrics", {})
    _write_csv(os.path.join(run_dir, "micro_scalp_strategy_metrics.csv"),
               [{"scope": "global", **{k: v for k, v in sm.items() if not isinstance(v, (dict, list))}}],
               ["scope"] + [k for k in sm if not isinstance(sm[k], (dict, list))])
    comp = report.get("compounding", {})
    _write_csv(os.path.join(run_dir, "micro_scalp_equity_curves.csv"),
               [{"i": i, "equity": e} for i, e in enumerate(comp.get("equity_curve", []))], ["i", "equity"])
    _write_csv(os.path.join(run_dir, "micro_scalp_window_stability.csv"),
               [{"candidate_id": c["candidate_id"], "windows_passed": c["windows_passed"],
                 "windows_tested": c["windows_tested"], "final_tier": c["final_tier"]}
                for c in report.get("candidates", []) + report.get("rejected_candidates", [])[:50]],
               ["candidate_id", "windows_passed", "windows_tested", "final_tier"])
    summary = {k: v for k, v in report.items() if k != "global_metrics"}
    summary["global_metrics"] = sm
    with open(os.path.join(run_dir, "micro_scalp_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    # shadow journal (separate dir)
    jdir = os.path.join(run_dir, "shadow_journal")
    _write_csv(os.path.join(jdir, "journal.csv"), journal,
               ["timestamp", "would_enter", "would_exit", "symbol", "side", "strategy",
                "reason", "entry_price", "exit_price", "net_result", "closed_green",
                "why_skipped", "why_closed", "confidence_score", "setup_quality", "final_decision"])
    _write_md(os.path.join(run_dir, "report.md"), report)
    return run_dir.replace("\\", "/")


def _write_md(path, report):
    g = report.get("global_metrics", {})
    lines = ["# ResearchOps V10.10 - Micro-Scalp Shadow Tournament (SHADOW ONLY)",
             "",
             "> Candidates are HYPOTHESES, not signals. SHADOW simulation, not a "
             "tradable portfolio. Edge NOT validated. NO LIVE.",
             "", f"- generated_at: {report.get('generated_at')}",
             f"- data_classification: {report.get('data_classification')}",
             f"- trades_simulated: {report.get('trades_simulated')}",
             f"- candidates: {report.get('n_candidates')} (shadow_test={report.get('n_shadow_test_candidate')}, weak={report.get('n_weak')})",
             f"- rejected: {report.get('n_rejected')}",
             f"- global net_EV: {g.get('net_EV')} | closed_green_rate: {g.get('closed_green_rate')} | net_PF: {g.get('net_PF')}",
             f"- side_concentration_warning: {report.get('side_concentration_warning')!r}",
             f"- false_discovery_risk: {report.get('false_discovery_risk')}",
             f"- compounding_status: {report.get('compounding', {}).get('compounding_status')}",
             f"- leverage_research_status: {report.get('aggressive_opportunity', {}).get('leverage_research_status')}",
             f"- orderbook_real: {report.get('orderbook_real')}",
             "", "## Top shadow candidates (hypotheses, not signals)"]
    for c in report.get("candidates", [])[:10]:
        lines.append(f"- [{c['final_tier']}] {c['timeframe']}/{c['side']}/{c['strategy_family']}/{c['exit_policy']} "
                     f"net_EV={c['net_EV']} closed_green={c['closed_green_rate']} windows={c['windows_passed']}/{c['windows_tested']}")
    lines += ["", "## Safety", "- research_only: true", "- shadow_only: true",
              "- paper_ready: false", "- live_ready: false", "- can_send_real_orders: false",
              "- real_leverage_allowed: false", "- candidates are hypotheses, not signals",
              "- FINAL_RECOMMENDATION: NO LIVE", ""]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# --------------------------------------------------------------------------
# M. Plan + summary (pure)
# --------------------------------------------------------------------------

def micro_scalp_plan() -> dict[str, Any]:
    return {
        "tool_version": TOOL_VERSION,
        "objective": ("SHADOW-only simulation of micro-scalping: many small trades, "
                      "fast entry, protect profit, close in green, repeat, measure "
                      "everything - WITHOUT real orders/leverage/money"),
        "strategy_families": list(STRATEGY_FAMILIES),
        "exit_policies": list(EXIT_POLICIES),
        "gates": ["min_trades", "net_EV>0", f"net_PF>={MIN_NET_PF}",
                  f"closed_green_rate>={MIN_CLOSED_GREEN_RATE}",
                  f"green_to_red_rate<={MAX_GREEN_TO_RED_RATE}", "cost stress x2/x3 survives",
                  "rolling WF passes", "stable across >=2 windows", "not single-symbol"],
        "candidate_tiers": [CAND_REJECTED, CAND_WEAK, CAND_SHADOW,
                            "never APPROVED_FOR_PAPER", "never APPROVED_FOR_LIVE"],
        "risks": ["no orderbook (proxy only)", "no OI/liquidations", "intermediate data",
                  "fee drag dominates tiny targets", "false discovery from many combos",
                  "compounding amplifies negative EV"],
        "leverage_grid": list(LEVERAGE_GRID), "leverage_recommendation": "NO_REAL_LEVERAGE",
        "orderbook_real": False, **_safety()}


def summarize_micro(summary) -> dict[str, Any]:
    cands = summary.get("candidates", [])
    return {"data_classification": summary.get("data_classification"),
            "trades_simulated": summary.get("trades_simulated"),
            "n_candidates": summary.get("n_candidates"),
            "n_shadow_test_candidate": summary.get("n_shadow_test_candidate"),
            "n_weak": summary.get("n_weak"), "n_rejected": summary.get("n_rejected"),
            "global_net_EV": summary.get("global_net_EV"),
            "global_closed_green_rate": summary.get("global_closed_green_rate"),
            "side_concentration_warning": summary.get("side_concentration_warning"),
            "false_discovery_risk": summary.get("false_discovery_risk"),
            "compounding_status": summary.get("compounding", {}).get("compounding_status"),
            "leverage_research_status": summary.get("aggressive_opportunity", {}).get("leverage_research_status"),
            "top_candidates": cands[:10], "edge_validated": False,
            "approved_for_paper": False, "approved_for_live": False, **_safety()}


def latest_micro_summary(output_dir=None):
    base = output_dir or OUTPUT_ROOT
    try:
        if not os.path.isdir(base):
            return None
        runs = sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))
        for rid in reversed(runs):
            sp = os.path.join(base, rid, "micro_scalp_summary.json")
            if os.path.isfile(sp):
                with open(sp, "r", encoding="utf-8") as fh:
                    return json.load(fh)
    except Exception:
        return None
    return None
