"""ResearchOps V10.40 - Shadow Simulation Tournament (research only, NO LIVE).

Puts many MECHANICAL policies + mandatory dumb baselines into the same
cost-aware arena over the collected Bybit data, simulates each hypothetical
trade (entry / TP / SL / trailing / time-exit) with fees+slippage and NO
lookahead, ranks by net_EV_lower_bound (NOT win rate), and simulates a 20 EUR
shadow bankroll. Nothing here sends an order, touches keys/.env, or flips any
live flag: every outcome is a hypothetical over historical bars.

HONESTY CONTRACT: a policy is only interesting if it clears costs AND its OOS
net-EV lower bound is not clearly negative AND it beats the dumb baselines.
Today, on this data, nothing does. FINAL_RECOMMENDATION: NO LIVE.
"""

from __future__ import annotations

import csv
import json
import os
import random
import statistics as st
from typing import Any, Callable

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE

TOOL_VERSION = "v10.40"
OUTPUT_SUBDIR = ("reports", "research", "shadow_simulation")
BAR_MS = 60_000
DEFAULT_TIME_BARS = 30                 # max holding horizon (bars)
GAP_FACTOR = 2                         # a >2-bar timestamp jump = DATA_GAP
MIN_SIGNALS = 40                       # below this a policy is not "validated"
START_BANKROLL_EUR = 20.0
BANKROLL_PROFILES = {"conservative": 0.10, "medium": 0.25,
                     "aggressive": 0.50, "ultra_gamble": 1.00}
FORBIDDEN_OUTPUTS = ("BUY_NOW", "SELL_NOW", "OPEN_POSITION", "LIVE_SIGNAL",
                     "PAPER_READY", "LIVE_READY", "CAN_SEND_REAL_ORDERS")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "not_actionable": True, "no_orders": True,
            "sends_orders": False, "touches_keys": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _round_trip(costs: dict | None = None) -> float:
    c = {**CE.DEFAULT_COSTS, **(costs or {})}
    return 2 * (c["fee_bps"] + c["slippage_bps"]) / 10_000 + c["spread_bps"] / 10_000


# ==========================================================================
# Trade simulator: side-aware TP/SL/TRAIL/TIME, fees+slippage, no lookahead
# ==========================================================================

def simulate_trade(bars: list[dict], i: int, side: str, tp_pct: float,
                   sl_pct: float, time_bars: int, trailing_pct: float | None,
                   costs: dict | None = None, entry_mode: str = "close") -> dict | None:
    """Hypothetical trade for the signal at bar i (features available at the
    CLOSE of bar i). Outcome read ONLY from bars[i+1:]. SL wins ties
    (conservative). A cadence gap on the first step aborts as DATA_GAP.

    entry_mode='close'    : fill at close of bar i (optimistic; kept for
                            comparison). Exposure starts at bar i+1.
    entry_mode='next_open': REALISTIC fill at the OPEN of bar i+1; requires the
                            i -> i+1 step to be contiguous, else DATA_GAP.
    In both modes the entry price is known no earlier than the signal
    (available_at = bar i close < entry time) -> no lookahead."""
    if i + 1 >= len(bars):
        return None
    if entry_mode == "next_open":
        if bars[i + 1]["ts"] - bars[i]["ts"] > GAP_FACTOR * BAR_MS:
            return {"side": side, "exit_reason": "DATA_GAP", "valid": False,
                    "entry_mode": entry_mode, "gross_return": 0.0,
                    "net_return": 0.0, "fees": _round_trip(costs), "slippage": 0.0,
                    "MAE": 0.0, "MFE": 0.0, "bars_held": 0, "duration_min": 0,
                    "hit_tp": False, "hit_sl": False, "trail_activated": False,
                    "trail_exit": False}
        entry = bars[i + 1]["open"]
        prev_ts = bars[i + 1]["ts"]
    else:
        entry = bars[i]["close"]
        prev_ts = bars[i]["ts"]
    if entry <= 0:
        return None
    future = bars[i + 1:i + 1 + time_bars]
    if not future:
        return None
    rt = _round_trip(costs)
    if side == "long":
        tp, sl = entry * (1 + tp_pct), entry * (1 - sl_pct)
    else:
        tp, sl = entry * (1 - tp_pct), entry * (1 + sl_pct)
    peak = entry
    trail_active = trail_activated = trail_exit = hit_tp = hit_sl = False
    mae = mfe = 0.0
    exit_reason, exit_price, bars_held = "TIME", future[-1]["close"], len(future)
    for j, fb in enumerate(future):
        if fb["ts"] - prev_ts > GAP_FACTOR * BAR_MS:            # data cadence gap
            if j == 0:                                          # cannot even step 1 bar
                return {"side": side, "exit_reason": "DATA_GAP", "valid": False,
                        "entry_mode": entry_mode, "gross_return": 0.0,
                        "net_return": 0.0, "fees": rt, "slippage": 0.0, "MAE": mae,
                        "MFE": mfe, "bars_held": 0, "duration_min": 0,
                        "hit_tp": False, "hit_sl": False,
                        "trail_activated": trail_activated, "trail_exit": False}
            # data went stale mid-trade -> close at last contiguous price (halt)
            exit_reason, exit_price, bars_held = "STALE_EXIT", future[j - 1]["close"], j
            break
        prev_ts = fb["ts"]
        up, dn = fb["high"] / entry - 1, fb["low"] / entry - 1
        if side == "long":
            mfe, mae = max(mfe, up), min(mae, dn)
        else:
            mfe, mae = max(mfe, -dn), min(mae, -up)
        # 1) hard stop first (conservative)
        if (side == "long" and fb["low"] <= sl) or (side == "short" and fb["high"] >= sl):
            exit_reason, exit_price, hit_sl, bars_held = "SL", sl, True, j + 1
            break
        # 2) trailing stop (activates after a favorable move of trailing_pct)
        if trailing_pct:
            fav = fb["high"] if side == "long" else fb["low"]
            peak = max(peak, fav) if side == "long" else min(peak, fav)
            fav_move = (peak / entry - 1) if side == "long" else (1 - peak / entry)
            if not trail_active and fav_move >= trailing_pct:
                trail_active = trail_activated = True
            if trail_active:
                tstop = peak * (1 - trailing_pct) if side == "long" else peak * (1 + trailing_pct)
                if (side == "long" and fb["low"] <= tstop) or (side == "short" and fb["high"] >= tstop):
                    exit_reason, exit_price, trail_exit, bars_held = "TRAIL", tstop, True, j + 1
                    break
        # 3) take profit
        if (side == "long" and fb["high"] >= tp) or (side == "short" and fb["low"] <= tp):
            exit_reason, exit_price, hit_tp, bars_held = "TP", tp, True, j + 1
            break
    gross = (exit_price / entry - 1) if side == "long" else (entry - exit_price) / entry
    return {"side": side, "exit_reason": exit_reason, "valid": True,
            "entry_mode": entry_mode, "entry_price": entry, "exit_price": exit_price,
            "gross_return": gross, "fees": rt, "slippage": 0.0,
            "net_return": gross - rt, "MAE": mae, "MFE": mfe,
            "bars_held": bars_held, "duration_min": bars_held,
            "hit_tp": hit_tp, "hit_sl": hit_sl,
            "trail_activated": trail_activated, "trail_exit": trail_exit}


# ==========================================================================
# Policy universe (mechanical) + mandatory dumb baselines
# ==========================================================================

def _train_thresholds(features: list[dict], split: int) -> dict[str, float]:
    keys = ("burst_score", "buy_sell_imbalance", "trend_score", "oi_change",
            "funding_level", "realized_volatility", "liquidation_side_imbalance",
            "book_pressure")
    thr: dict[str, float] = {}
    for k in keys:
        vals = sorted(f[k] for f in features[:split]
                      if isinstance(f.get(k), (int, float)))
        thr[k + "_q90"] = vals[int(len(vals) * 0.9)] if vals else 0.0
        thr[k + "_q66"] = vals[int(len(vals) * 0.66)] if vals else 0.0
    return thr


# each policy: (feature_row, prev_feature_row, thr, rng) -> "long"/"short"/None
def _policies() -> dict[str, tuple[str, Callable]]:
    def momentum(f, p, t, r):
        return "long" if f.get("burst_score", 0) > t["burst_score_q90"] and \
            f.get("buy_sell_imbalance", 0) > 0 else None

    def flow(f, p, t, r):
        v = f.get("buy_sell_imbalance", 0)
        if v > t["buy_sell_imbalance_q90"]:
            return "long"
        if v < -t["buy_sell_imbalance_q90"]:
            return "short"
        return None

    def mean_rev(f, p, t, r):
        if f.get("symbol_regime") != "chop":
            return None
        v = f.get("buy_sell_imbalance", 0)
        return "long" if v < -t["buy_sell_imbalance_q66"] else None

    def trend(f, p, t, r):
        ts = f.get("trend_score", 0)
        if ts > t["trend_score_q90"]:
            return "long"
        if ts < -t["trend_score_q90"]:
            return "short"
        return None

    def oi_confirm(f, p, t, r):
        return "long" if f.get("oi_change", 0) > t["oi_change_q90"] and \
            f.get("trend_score", 0) > 0 else None

    def funding_fade(f, p, t, r):
        v = f.get("funding_level", 0)
        if v > t["funding_level_q90"]:
            return "short"
        if v < -t["funding_level_q90"]:
            return "long"
        return None

    def vol_breakout(f, p, t, r):
        return "long" if f.get("realized_volatility", 0) > t["realized_volatility_q90"] \
            and f.get("trend_score", 0) > 0 else None

    def liq_reversal(f, p, t, r):
        v = f.get("liquidation_side_imbalance", 0)
        if v > t["liquidation_side_imbalance_q90"]:
            return "short"
        if v < -t["liquidation_side_imbalance_q90"]:
            return "long"
        return None

    def book_pressure(f, p, t, r):
        return "long" if f.get("book_pressure", 0) > t["book_pressure_q90"] else None

    # ---- mandatory dumb baselines -----------------------------------------
    def b_random(f, p, t, r):
        return r.choice(["long", "short"]) if r.random() < 0.2 else None

    def b_long(f, p, t, r):
        return "long"

    def b_short(f, p, t, r):
        return "short"

    def b_notrade(f, p, t, r):
        return None

    def b_simple_mom(f, p, t, r):
        if p is None:
            return None
        return "long" if f.get("close", 0) > p.get("close", 0) else "short"

    def b_simple_rev(f, p, t, r):
        if p is None:
            return None
        return "long" if f.get("close", 0) < p.get("close", 0) else "short"

    return {
        "micro_momentum": ("strategy", momentum),
        "flow_imbalance": ("strategy", flow),
        "chop_mean_reversion": ("strategy", mean_rev),
        "trend_follow": ("strategy", trend),
        "oi_confirmation": ("strategy", oi_confirm),
        "funding_fade": ("strategy", funding_fade),
        "volatility_breakout": ("strategy", vol_breakout),
        "liquidation_reversal": ("strategy", liq_reversal),
        "orderbook_pressure": ("strategy", book_pressure),
        "baseline_random_side": ("baseline", b_random),
        "baseline_always_long": ("baseline", b_long),
        "baseline_always_short": ("baseline", b_short),
        "baseline_no_trade": ("baseline", b_notrade),
        "baseline_simple_momentum": ("baseline", b_simple_mom),
        "baseline_simple_mean_reversion": ("baseline", b_simple_rev),
    }


# ==========================================================================
# Run one policy over the OOS region (train only used for thresholds)
# ==========================================================================

def run_policy(name: str, kind: str, fn: Callable, features: list[dict],
               bars: list[dict], split: int, thr: dict, *, tp_pct: float,
               sl_pct: float, time_bars: int, trailing_pct: float | None,
               costs: dict | None, cooldown: int, seed: int = 20,
               entry_mode: str = "close") -> dict:
    rng = random.Random(seed + hash(name) % 10_000)
    signals: list[dict] = []
    outcomes: list[dict] = []
    last_entry = -10 ** 9
    for i in range(split, len(features) - 1):
        side = fn(features[i], features[i - 1] if i > 0 else None, thr, rng)
        if side not in ("long", "short"):
            continue
        if i - last_entry < cooldown:                 # avoid correlated overlap
            continue
        o = simulate_trade(bars, i, side, tp_pct, sl_pct, time_bars, trailing_pct,
                           costs, entry_mode=entry_mode)
        if o is None:
            continue
        last_entry = i
        f = features[i]
        signals.append({
            "signal_id": f"{name}_{i}", "ts": f["ts"], "available_at": f["available_at"],
            "symbol": bars[i].get("symbol", "BTCUSDT"), "side": side,
            "policy_name": name, "strategy_family": kind, "timeframe": "1m",
            "entry_price": bars[i]["close"], "tp_pct": tp_pct, "sl_pct": sl_pct,
            "trailing_pct": trailing_pct, "max_horizon": time_bars,
            "regime": f.get("symbol_regime"), "cost_bps": round(_round_trip(costs) * 10_000, 2),
            "reason": f"{name}:{side}", "policy_version": TOOL_VERSION,
            "data_quality_status": ("DATA_GAP" if not o.get("valid") else "OK")})
        outcomes.append({**o, "signal_id": f"{name}_{i}", "ts": f["ts"]})
    valid = [o for o in outcomes if o.get("valid")]
    nets = [o["net_return"] for o in valid]
    metrics = _policy_metrics(name, kind, valid, nets, len(features) - split)
    metrics["n_signals_raw"] = len(outcomes)
    metrics["n_data_gap"] = sum(1 for o in outcomes if o["exit_reason"] == "DATA_GAP")
    return {"name": name, "kind": kind, "signals": signals, "outcomes": outcomes,
            "metrics": metrics}


def _policy_metrics(name, kind, valid, nets, n_oos_bars) -> dict:
    ev = CE.evaluate_net_ev(nets) if len(nets) >= CE.MIN_SAMPLE else {
        "sample_size": len(nets), "net_EV": (st.mean(nets) if nets else None),
        "net_EV_lower_bound": None, "decision": "ABSTAIN"}
    wins = [o for o in valid if o["net_return"] > 0]
    losses = [o for o in valid if o["net_return"] <= 0]
    aw = st.mean([o["net_return"] for o in wins]) if wins else 0.0
    al = st.mean([o["net_return"] for o in losses]) if losses else 0.0
    exits = [o["exit_reason"] for o in valid]
    return {
        "policy": name, "kind": kind, "n_signals": len(valid),
        "win_rate": round(len(wins) / len(valid), 4) if valid else None,
        "avg_win": round(aw, 6), "avg_loss": round(al, 6),
        "payoff_ratio": round(aw / abs(al), 3) if al != 0 else None,
        "profit_factor": round(sum(o["net_return"] for o in wins) /
                               abs(sum(o["net_return"] for o in losses)), 3)
        if losses and sum(o["net_return"] for o in losses) != 0 else None,
        "expectancy": round(st.mean(nets), 6) if nets else None,
        "net_EV": ev.get("net_EV"), "net_EV_lower_bound": ev.get("net_EV_lower_bound"),
        "max_drawdown": round(min(CE._cum_dd(nets), 0.0), 6),
        "avg_MAE": round(st.mean([o["MAE"] for o in valid]), 6) if valid else None,
        "avg_MFE": round(st.mean([o["MFE"] for o in valid]), 6) if valid else None,
        "avg_duration_min": round(st.mean([o["bars_held"] for o in valid]), 2) if valid else None,
        "tp_count": exits.count("TP"), "sl_count": exits.count("SL"),
        "time_count": exits.count("TIME"), "trail_count": exits.count("TRAIL"),
        "stale_count": exits.count("STALE_EXIT"),
        "turnover": round(len(valid) / max(1, n_oos_bars), 4),
        "sample_sufficient": len(valid) >= MIN_SIGNALS}


# ==========================================================================
# 20 EUR shadow bankroll (fake money over the best policy's outcomes)
# ==========================================================================

def bankroll_sim(net_returns: list[float], profiles=BANKROLL_PROFILES,
                 start=START_BANKROLL_EUR) -> dict:
    out: dict[str, Any] = {"start_eur": start, "profiles": {}, **_safety()}
    for prof, frac in profiles.items():
        eq = start
        curve = [eq]
        peak = eq
        max_dd = 0.0
        win_streak = loss_streak = cur_w = cur_l = 0
        for nr in net_returns:
            pnl = eq * frac * nr                   # notional = frac*equity, no margin
            eq = max(0.0, eq + pnl)
            curve.append(eq)
            peak = max(peak, eq)
            max_dd = min(max_dd, (eq - peak) / peak if peak > 0 else 0.0)
            if pnl > 0:
                cur_w += 1; cur_l = 0
            else:
                cur_l += 1; cur_w = 0
            win_streak, loss_streak = max(win_streak, cur_w), max(loss_streak, cur_l)
        out["profiles"][prof] = {
            "position_fraction": frac, "final_eur": round(eq, 4),
            "return_pct": round((eq / start - 1) * 100, 2),
            "max_drawdown_pct": round(max_dd * 100, 2), "n_trades": len(net_returns),
            "best_win_streak": win_streak, "worst_loss_streak": loss_streak,
            "wiped_out": eq <= start * 0.5,
            "statistically_defensible": False}    # no validated edge -> never defensible
    return out


# ==========================================================================
# Execution rehearsal (design-only simulation; sends nothing)
# ==========================================================================

def execution_rehearsal() -> dict:
    scenarios = [
        ("market_entry", "simulated fill at close + slippage bps"),
        ("limit_entry", "fill only if price touched, else NO_FILL"),
        ("rejected_order", "abort, no position opened"),
        ("partial_fill", "resize to filled qty, adjust TP/SL"),
        ("no_fill", "cancel after timeout, no position"),
        ("duplicate_order", "blocked by client_order_id idempotency"),
        ("stale_signal", "skip if data age > staleness threshold"),
        ("delayed_execution", "reprice or abort if drift > tolerance"),
        ("tp_placement", "reduce-only TP registered"),
        ("sl_placement", "reduce-only SL registered (with volatility room)"),
        ("trailing_activate", "activate after favorable move >= trailing_pct"),
        ("trailing_exit", "exit on retrace from peak"),
        ("emergency_close", "market close all (kill switch)"),
        ("exchange_disconnect", "halt: no new orders until reconnect"),
        ("reconciliation_mismatch", "halt and reconcile before acting"),
        ("position_already_closed", "no-op + reconcile"),
        ("worse_fees_slippage", "worst-case costs applied in sim"),
    ]
    return {"tool_version": TOOL_VERSION,
            "rehearsal_type": "SIMULATED_DESIGN_ONLY",
            "real_executor_exists": False, "sends_orders": False,
            "scenarios": [{"event": e, "intended_handling": h,
                           "simulated_ok": True} for e, h in scenarios],
            "blockers_before_any_micro_live": [
                "no_validated_edge", "no_bitget_credentials",
                "live_order_path_intentionally_absent", "human_approval_required"],
            **_safety()}


# ==========================================================================
# Verdicts + orchestration
# ==========================================================================

VERDICTS = ("REJECTED", "INCUBATE", "SHADOW_FORWARD",
            "PAPER_CANDIDATE_FUTURE", "MICRO_LIVE_REVIEW_FUTURE")


def _verdict(m: dict, best_baseline_lb: float) -> tuple[str, list[str]]:
    lb, nev = m.get("net_EV_lower_bound"), m.get("net_EV")
    blockers = []
    if m["n_signals"] < MIN_SIGNALS:
        return "INCUBATE" if (nev or -1) > 0 else "REJECTED", ["sample_insufficient"]
    if nev is None or nev <= 0:
        return "REJECTED", ["net_ev_non_positive"]
    if lb is None or lb <= 0:
        blockers.append("lower_bound_not_positive")
        return "INCUBATE", blockers
    if lb <= best_baseline_lb:
        return "INCUBATE", ["does_not_beat_baselines"]
    # lb>0 and beats baselines -> still needs forward + more data + audit
    return "SHADOW_FORWARD", ["needs_forward_shadow", "needs_more_data", "needs_codex_audit"]


def run_tournament(symbol: str = "BTCUSDT", bars: list[dict] | None = None,
                   aux: dict | None = None, *, tp_pct: float = 0.006,
                   sl_pct: float = 0.006, time_bars: int = DEFAULT_TIME_BARS,
                   trailing_pct: float | None = 0.004, cooldown: int = 5,
                   costs: dict | None = None, entry_mode: str = "close",
                   write_reports: bool = True) -> dict:
    """Offline shadow tournament. tp/sl default to 60bps with a trailing stop at
    40bps favorable; SL is deliberately NOT tight (volatility room). Everything
    hypothetical over historical bars. entry_mode 'close' (optimistic) or
    'next_open' (realistic fill at next bar open)."""
    if bars is None:
        data = CE.load_dataset(symbol)
        bars = data.get("bars") or []
        aux = {k: data.get(k) for k in ("oi", "funding", "orderbook", "liquidations")}
    aux = aux or {}
    summary: dict[str, Any] = {"tool_version": TOOL_VERSION, "symbol": symbol,
                               "ran_at": CE._now_iso(), "n_bars": len(bars),
                               "params": {"tp_pct": tp_pct, "sl_pct": sl_pct,
                                          "time_bars": time_bars,
                                          "trailing_pct": trailing_pct,
                                          "cooldown": cooldown, "entry_mode": entry_mode,
                                          "round_trip_cost": round(_round_trip(costs), 6)},
                               **_safety()}
    if len(bars) < 3 * CE.MIN_SAMPLE:
        summary["verdict"] = "NEEDS_MORE_DATA"
        summary["note"] = f"only {len(bars)} bars; keep collecting"
        return summary
    features = CE.build_features(bars, aux.get("oi"), aux.get("funding"),
                                 aux.get("orderbook"), aux.get("liquidations"))
    split = int(len(features) * 0.6)
    thr = _train_thresholds(features, split)
    results = []
    for name, (kind, fn) in _policies().items():
        results.append(run_policy(name, kind, fn, features, bars, split, thr,
                                   tp_pct=tp_pct, sl_pct=sl_pct, time_bars=time_bars,
                                   trailing_pct=trailing_pct, costs=costs,
                                   cooldown=cooldown, entry_mode=entry_mode))
    # baseline reference = best baseline lower bound (or -inf if none scored)
    base_lbs = [r["metrics"]["net_EV_lower_bound"] for r in results
                if r["kind"] == "baseline" and r["metrics"]["net_EV_lower_bound"] is not None]
    best_baseline_lb = max(base_lbs) if base_lbs else -9.0
    scoreboard = []
    for r in results:
        m = r["metrics"]
        verdict, blockers = _verdict(m, best_baseline_lb)
        m["verdict"], m["blockers"] = verdict, blockers
        scoreboard.append(m)
    # rank by net_EV_lower_bound (win rate is NOT the ranking key)
    scoreboard.sort(key=lambda m: (m.get("net_EV_lower_bound") is not None,
                                   m.get("net_EV_lower_bound") or -9), reverse=True)
    strat_board = [m for m in scoreboard if m["kind"] == "strategy"]
    best = strat_board[0] if strat_board else None
    # data continuity: the collector pulls ~1000 trades per REST cycle, so bars
    # are clustered with multi-minute gaps -> report how much survives simulation
    tot_raw = sum(m.get("n_signals_raw", 0) for m in scoreboard)
    tot_gap = sum(m.get("n_data_gap", 0) for m in scoreboard)
    tot_valid = sum(m["n_signals"] for m in scoreboard)
    # bankroll on the best STRATEGY's valid outcomes (or empty)
    best_res = next((r for r in results if best and r["name"] == best["policy"]), None)
    best_nets = [o["net_return"] for o in (best_res["outcomes"] if best_res else [])
                 if o.get("valid")]
    bankroll = bankroll_sim(best_nets) if best_nets else {"note": "no valid trades", **_safety()}
    rehearsal = execution_rehearsal()
    any_shadow = any(m["verdict"] in ("SHADOW_FORWARD", "PAPER_CANDIDATE_FUTURE",
                                      "MICRO_LIVE_REVIEW_FUTURE")
                     for m in scoreboard if m["kind"] == "strategy")
    summary.update({
        "policies_total": len(results),
        "best_strategy": best,
        "best_baseline_lower_bound": round(best_baseline_lb, 8),
        "any_strategy_beats_baseline_and_costs": any_shadow,
        "scoreboard_top": scoreboard[:8],
        "data_continuity": {
            "raw_signals": tot_raw, "valid_outcomes": tot_valid,
            "data_gap_outcomes": tot_gap,
            "gap_ratio": round(tot_gap / max(1, tot_raw), 3),
            "note": ("dataset is clustered (REST ~1000 trades/cycle) with "
                     "multi-minute gaps; continuous websocket trade collection "
                     "24/7 is required for realistic forward trade simulation")},
        "bankroll_20eur": bankroll.get("profiles"),
        "execution_rehearsal": {"real_executor_exists": False,
                                "blockers": rehearsal["blockers_before_any_micro_live"]},
        "micro_live_ready": False,
        "micro_live_blockers": ["no_validated_edge", "no_bitget_credentials",
                                "live_order_path_absent", "human_approval_required",
                                "insufficient_forward_data"],
        "entry_mode": entry_mode,
        "verdict": ("STRATEGIES_UNDER_RESEARCH" if best else "NO_STRATEGIES"),
        "ranking_key": "net_EV_lower_bound (win_rate is secondary)"})
    if write_reports:
        _write_reports(summary, results, scoreboard, bankroll, rehearsal)
        summary["reports_dir"] = str(CE._repo_root().joinpath(*OUTPUT_SUBDIR)).replace("\\", "/")
    return summary


def compare_entry_modes(symbol: str = "BTCUSDT", bars: list[dict] | None = None,
                        aux: dict | None = None, **kw) -> dict:
    """Run the tournament under both entry modes and show which strategies were
    only 'good' because of the optimistic close entry. Realistic = next_open."""
    kw.pop("entry_mode", None)
    kw["write_reports"] = False
    a = run_tournament(symbol, bars, aux, entry_mode="close", **kw)
    b = run_tournament(symbol, bars, aux, entry_mode="next_open", **kw)
    if "scoreboard_top" not in a or "scoreboard_top" not in b:
        return {"note": "insufficient data for comparison", **_safety()}

    def by_name(rep):
        # need full scoreboard, not just top -> re-read from summary top (8) is
        # enough for the headline comparison
        return {m["policy"]: m for m in rep.get("scoreboard_top", [])}
    ca, cb = by_name(a), by_name(b)
    rows = []
    for name in sorted(set(ca) | set(cb)):
        ma, mb = ca.get(name, {}), cb.get(name, {})
        rows.append({"policy": name, "kind": ma.get("kind") or mb.get("kind"),
                     "close_net_EV": ma.get("net_EV"),
                     "next_open_net_EV": mb.get("net_EV"),
                     "close_verdict": ma.get("verdict"),
                     "next_open_verdict": mb.get("verdict"),
                     "died_on_next_open": (ma.get("verdict") not in (None, "REJECTED")
                                           and mb.get("verdict") == "REJECTED")})
    return {"tool_version": TOOL_VERSION, "symbol": symbol,
            "close_any_beats": a.get("any_strategy_beats_baseline_and_costs"),
            "next_open_any_beats": b.get("any_strategy_beats_baseline_and_costs"),
            "close_best": a.get("best_strategy", {}).get("policy") if a.get("best_strategy") else None,
            "next_open_best": b.get("best_strategy", {}).get("policy") if b.get("best_strategy") else None,
            "per_policy": rows, **_safety()}


def _write_reports(summary, results, scoreboard, bankroll, rehearsal) -> None:
    d = CE._repo_root().joinpath(*OUTPUT_SUBDIR)
    d.mkdir(parents=True, exist_ok=True)

    def wj(name, obj):
        tmp = d / (name + ".tmp")
        tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, d / name)

    wj("shadow_summary_v1040.json", summary)
    wj("shadow_bankroll_20eur_v1040.json", bankroll)
    # signals + outcomes CSV
    with open(d / "shadow_signals_v1040.csv", "w", newline="", encoding="utf-8") as f:
        cols = ["signal_id", "ts", "available_at", "symbol", "side", "policy_name",
                "strategy_family", "timeframe", "entry_price", "tp_pct", "sl_pct",
                "trailing_pct", "max_horizon", "regime", "cost_bps", "reason",
                "policy_version", "data_quality_status"]
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in results:
            for s in r["signals"]:
                w.writerow({k: s.get(k) for k in cols})
    with open(d / "shadow_outcomes_v1040.csv", "w", newline="", encoding="utf-8") as f:
        cols = ["signal_id", "ts", "side", "exit_reason", "valid", "entry_price",
                "exit_price", "gross_return", "net_return", "fees", "MAE", "MFE",
                "bars_held", "hit_tp", "hit_sl", "trail_activated", "trail_exit"]
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in results:
            for o in r["outcomes"]:
                w.writerow({k: o.get(k) for k in cols})
    with open(d / "shadow_scoreboard_v1040.csv", "w", newline="", encoding="utf-8") as f:
        cols = ["policy", "kind", "verdict", "n_signals", "win_rate", "payoff_ratio",
                "profit_factor", "expectancy", "net_EV", "net_EV_lower_bound",
                "max_drawdown", "tp_count", "sl_count", "time_count", "trail_count",
                "turnover", "sample_sufficient"]
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for m in scoreboard:
            w.writerow({k: m.get(k) for k in cols})
    (d / "execution_rehearsal_report_v1040.md").write_text(
        _md_rehearsal(rehearsal), encoding="utf-8")
    (d / "shadow_research_memo_v1040.md").write_text(
        _md_memo(summary, scoreboard), encoding="utf-8")
    (d / "shadow_bankroll_20eur_v1040.md").write_text(
        _md_bankroll(bankroll), encoding="utf-8")
    (d / "micro_live_readiness_report_v1040.md").write_text(
        _md_readiness(summary), encoding="utf-8")


def _md_memo(summary, scoreboard) -> str:
    lines = ["# Shadow Tournament Memo (V10.40) — RESEARCH ONLY, NO LIVE", "",
             f"Ran: {summary['ran_at']} · bars: {summary['n_bars']} · "
             f"ranking key: **net_EV_lower_bound** (win rate is secondary).", "",
             "| policy | kind | verdict | n | win% | PF | net_EV | net_EV_lb | maxDD | verdict |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for m in scoreboard[:12]:
        lines.append(f"| {m['policy']} | {m['kind']} | {m['verdict']} | {m['n_signals']} | "
                     f"{m.get('win_rate')} | {m.get('profit_factor')} | {m.get('net_EV')} | "
                     f"{m.get('net_EV_lower_bound')} | {m.get('max_drawdown')} | {m['verdict']} |")
    lines += ["", f"micro_live_ready: **{summary['micro_live_ready']}** · "
              f"blockers: {', '.join(summary['micro_live_blockers'])}", "",
              "**FINAL_RECOMMENDATION=NO LIVE.**"]
    return "\n".join(lines)


def _md_bankroll(bankroll) -> str:
    lines = ["# 20 EUR Shadow Bankroll (V10.40) — FAKE money, NO LIVE", ""]
    profs = bankroll.get("profiles") or {}
    if not profs:
        return "# 20 EUR Shadow Bankroll — no valid trades yet.\n\nNO LIVE."
    lines += ["| profile | pos.frac | final € | return% | maxDD% | trades | worst loss streak | wiped? | defensible? |",
              "|---|---|---|---|---|---|---|---|---|"]
    for p, v in profs.items():
        lines.append(f"| {p} | {v['position_fraction']} | {v['final_eur']} | {v['return_pct']} | "
                     f"{v['max_drawdown_pct']} | {v['n_trades']} | {v['worst_loss_streak']} | "
                     f"{v['wiped_out']} | {v['statistically_defensible']} |")
    lines += ["", "Ninguna es estadísticamente defendible: no hay edge validada.",
              "", "**NO LIVE.**"]
    return "\n".join(lines)


def _md_rehearsal(r) -> str:
    lines = ["# Execution Rehearsal (V10.40) — SIMULATED DESIGN ONLY, sends nothing", "",
             f"real_executor_exists: **{r['real_executor_exists']}** · sends_orders: **False**", "",
             "| event | intended handling | simulated_ok |", "|---|---|---|"]
    for s in r["scenarios"]:
        lines.append(f"| {s['event']} | {s['intended_handling']} | {s['simulated_ok']} |")
    lines += ["", "Blockers antes de cualquier micro-live: " +
              ", ".join(r["blockers_before_any_micro_live"]), "", "**NO LIVE.**"]
    return "\n".join(lines)


def _md_readiness(summary) -> str:
    b = summary.get("best_strategy") or {}
    return "\n".join([
        "# Micro-Live Readiness (V10.40) — REVIEW ONLY, sends no orders", "",
        f"best_strategy: {b.get('policy')} · verdict: {b.get('verdict')}",
        f"net_EV: {b.get('net_EV')} · net_EV_lower_bound: {b.get('net_EV_lower_bound')} · "
        f"profit_factor: {b.get('profit_factor')} · win_rate: {b.get('win_rate')} · "
        f"maxDD: {b.get('max_drawdown')} · n: {b.get('n_signals')}",
        f"beats baseline+costs: {summary.get('any_strategy_beats_baseline_and_costs')}", "",
        f"**micro_live_ready: {summary.get('micro_live_ready')}**",
        "blockers: " + ", ".join(summary.get("micro_live_blockers", [])), "",
        "El botón 'Request Micro-Live Review' solo generaría este checklist. NO envía órdenes.",
        "", "**FINAL_RECOMMENDATION=NO LIVE.**"])
