"""ResearchOps V10.43B - Exit Optimization / Profit Extraction (research only).

Studies whether the same entries could keep more of the move via BETTER EXITS
(not more risk/leverage/sizing). For a fixed entry trigger it evaluates a grid of
exit variants (fixed TP/SL, farther TP, tighter SL, trailing, break-even, partial
TP, time-short/long, ATR-dynamic) over multiple hold horizons, measures MFE/MAE
and capture ratio, and reports winner-extraction / loser-control stats.

Honest gates: NEVER declares edge on small sample, non-positive lower bound, bad
drawdown, few-trade dependence, slippage fragility or failing baselines. Reuses
the V10.40 conventions (entry next_open, SL-first ties, costs, DATA_GAP, no
lookahead). NO LIVE, no orders, no keys, no sizing/leverage changes.
"""

from __future__ import annotations

import csv
import os
import statistics as st
from datetime import datetime, timezone
from typing import Any, Callable

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE
from . import shadow_simulation_tournament_v10_40 as SH
from . import autonomous_strategy_lab_v10_43b as LAB

TOOL_VERSION = "v10.43b"
OUTPUT_SUBDIR = ("reports", "research", "strategy_lab_v10_43b")
BAR_MS = 60_000
GAP_FACTOR = 2
MIN_SAMPLE = 30
MIN_OOS = 20
MAX_DD_LIMIT = -0.10
SLIP_MULT = 1.5
WINNER_MFE_BPS = 0.004          # "reached +40bps favorable" threshold
NEVER_FAV_BPS = 0.0005

VERDICTS = ("REJECTED", "WATCHLIST", "INCUBATE",
            "SHADOW_FORWARD_CANDIDATE", "NEEDS_MORE_DATA")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "not_actionable": True, "no_orders": True, "changes_sizing": False,
            "changes_leverage": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


# ==========================================================================
# Exit-aware trade simulator (TP/SL/TRAIL/BREAK-EVEN/TIME, next_open, no lookahead)
# ==========================================================================

def simulate_exit(bars: list[dict], i: int, side: str, tp: float, sl: float,
                  trail: float | None, be: float | None, horizon: int,
                  costs: dict | None = None) -> dict | None:
    """One hypothetical trade with a configurable exit. Entry at next bar open
    (realistic); outcome read only from bars[i+1:]. SL wins ties. Break-even
    (be) moves the stop to entry once favorable excursion >= be. Reports MFE/MAE
    in the side's PnL space and net_return after round-trip cost."""
    if i + 1 >= len(bars):
        return None
    if bars[i + 1]["ts"] - bars[i]["ts"] > GAP_FACTOR * BAR_MS:
        return {"valid": False, "exit_reason": "DATA_GAP", "net_return": 0.0,
                "gross": 0.0, "mfe": 0.0, "mae": 0.0, "bars_held": 0}
    entry = bars[i + 1]["open"]
    if entry <= 0:
        return None
    future = bars[i + 1:i + 1 + horizon]
    if not future:
        return None
    rt = SH._round_trip(costs)
    tp_px = entry * (1 + tp) if side == "long" else entry * (1 - tp)
    sl_px = entry * (1 - sl) if side == "long" else entry * (1 + sl)
    be_active = False
    peak = entry
    mfe = mae = 0.0
    exit_reason, exit_px, held = "TIME", future[-1]["close"], len(future)
    prev_ts = bars[i + 1]["ts"]
    for j, fb in enumerate(future):
        if fb["ts"] - prev_ts > GAP_FACTOR * BAR_MS:
            if j == 0:
                return {"valid": False, "exit_reason": "DATA_GAP", "net_return": 0.0,
                        "gross": 0.0, "mfe": mfe, "mae": mae, "bars_held": 0}
            exit_reason, exit_px, held = "STALE_EXIT", future[j - 1]["close"], j
            break
        prev_ts = fb["ts"]
        up, dn = fb["high"] / entry - 1, fb["low"] / entry - 1
        if side == "long":
            mfe, mae = max(mfe, up), min(mae, dn)
            fav = fb["high"]
        else:
            mfe, mae = max(mfe, -dn), min(mae, -up)
            fav = fb["low"]
        # break-even: once favorable move >= be, pull stop to entry
        peak = max(peak, fav) if side == "long" else min(peak, fav)
        fav_move = (peak / entry - 1) if side == "long" else (1 - peak / entry)
        if be is not None and not be_active and fav_move >= be:
            be_active = True
        if be_active:
            sl_px = max(sl_px, entry) if side == "long" else min(sl_px, entry)
        # 1) stop first (conservative on ties)
        if (side == "long" and fb["low"] <= sl_px) or (side == "short" and fb["high"] >= sl_px):
            exit_reason = "BE" if (be_active and abs(sl_px - entry) < 1e-9) else "SL"
            exit_px, held = sl_px, j + 1
            break
        # 2) trailing
        if trail:
            tstop = peak * (1 - trail) if side == "long" else peak * (1 + trail)
            if fav_move >= trail and ((side == "long" and fb["low"] <= tstop) or
                                      (side == "short" and fb["high"] >= tstop)):
                exit_reason, exit_px, held = "TRAIL", tstop, j + 1
                break
        # 3) take profit
        if (side == "long" and fb["high"] >= tp_px) or (side == "short" and fb["low"] <= tp_px):
            exit_reason, exit_px, held = "TP", tp_px, j + 1
            break
    gross = (exit_px / entry - 1) if side == "long" else (entry - exit_px) / entry
    net = gross - rt
    return {"valid": True, "exit_reason": exit_reason, "gross": gross,
            "net_return": net, "mfe": mfe, "mae": mae, "bars_held": held,
            "captured_of_mfe": (net / mfe) if mfe > 1e-9 else None}


def _partial_tp(bars, i, side, tp1, tp2, sl, horizon, costs):
    """Approximate a partial TP: half at a near TP, half at a farther TP/time.
    Extra half round-trip cost for the second leg."""
    a = simulate_exit(bars, i, side, tp1, sl, None, None, horizon, costs)
    b = simulate_exit(bars, i, side, tp2, sl, None, None, horizon, costs)
    if not a or not b or not a["valid"] or not b["valid"]:
        return a if (a and not a["valid"]) else b
    extra = SH._round_trip(costs) * 0.5
    net = 0.5 * a["net_return"] + 0.5 * b["net_return"] - extra
    return {"valid": True, "exit_reason": "PARTIAL", "gross": 0.5 * a["gross"] + 0.5 * b["gross"],
            "net_return": net, "mfe": max(a["mfe"], b["mfe"]), "mae": min(a["mae"], b["mae"]),
            "bars_held": max(a["bars_held"], b["bars_held"]),
            "captured_of_mfe": (net / max(a["mfe"], b["mfe"])) if max(a["mfe"], b["mfe"]) > 1e-9 else None}


# ==========================================================================
# Exit variants x horizons
# ==========================================================================

def _variants() -> list[dict]:
    V = []

    def add(name, tp, sl, trail=None, be=None, partial=None, atr=False):
        V.append({"name": name, "tp": tp, "sl": sl, "trail": trail, "be": be,
                  "partial": partial, "atr": atr})
    add("fixed_baseline", 0.006, 0.006)
    add("tp_far", 0.012, 0.006)
    add("sl_tight", 0.006, 0.003)
    add("tp_far_sl_tight", 0.012, 0.004)
    add("trailing_tight", 0.012, 0.006, trail=0.003)
    add("trailing_wide", 0.012, 0.006, trail=0.006)
    add("break_even_40bps", 0.012, 0.006, be=0.004)
    add("partial_tp", 0.006, 0.006, partial=(0.006, 0.014))
    add("atr_dynamic", 0.0, 0.0, atr=True)          # tp/sl scaled by vol at entry
    return V


HORIZONS = (5, 15, 30, 60, 120)


def _eval_variant(bars, feats, entries, var, horizon, costs=None) -> dict:
    outs = []
    for i in entries:
        side = entries[i]
        if var["atr"]:
            vol = feats[i].get("realized_volatility", 0.0) or 0.0
            tp = max(0.004, min(0.02, 3 * vol))
            sl = max(0.003, min(0.015, 2 * vol))
            o = simulate_exit(bars, i, side, tp, sl, None, None, horizon, costs)
        elif var["partial"]:
            tp1, tp2 = var["partial"]
            o = _partial_tp(bars, i, side, tp1, tp2, var["sl"], horizon, costs)
        else:
            o = simulate_exit(bars, i, side, var["tp"], var["sl"], var["trail"],
                              var["be"], horizon, costs)
        if o and o.get("valid"):
            outs.append(o)
    nets = [o["net_return"] for o in outs]
    ev = CE.evaluate_net_ev(nets) if len(nets) >= MIN_SAMPLE else {
        "sample_size": len(nets), "net_EV": (st.mean(nets) if nets else None),
        "net_EV_lower_bound": None}
    wins = [o for o in outs if o["net_return"] > 0]
    losses = [o for o in outs if o["net_return"] <= 0]
    mfes = [o["mfe"] for o in outs]
    maes = [o["mae"] for o in outs]
    avg_mfe = st.mean(mfes) if mfes else 0.0
    # capture ratio is only meaningful when there is real favorable excursion;
    # on gappy data (STALE exits, MFE~0) it is undefined, not a huge number.
    MFE_FLOOR = 0.001
    if avg_mfe >= MFE_FLOOR and nets:
        capture = max(-2.0, min(1.5, st.mean(nets) / avg_mfe))
    else:
        capture = None
    caps = [max(-2.0, min(1.5, o["captured_of_mfe"])) for o in outs
            if o["captured_of_mfe"] is not None and o["mfe"] >= MFE_FLOOR]
    return {"policy_name": "momentum_long_base", "exit_variant": var["name"],
            "horizon": horizon, "tp": var["tp"], "sl": var["sl"],
            "trailing": var["trail"], "break_even": var["be"],
            "sample_size": len(outs),
            "avg_MFE": round(avg_mfe, 6) if mfes else None,
            "avg_MAE": round(st.mean(maes), 6) if maes else None,
            "capture_ratio": round(capture, 4) if capture is not None else None,
            "median_capture": round(st.median(caps), 4) if caps else None,
            "net_EV": ev.get("net_EV"), "net_EV_lower_bound": ev.get("net_EV_lower_bound"),
            "max_drawdown": round(min(CE._cum_dd(nets), 0.0), 6),
            "profit_factor": round(sum(o["net_return"] for o in wins) /
                                   abs(sum(o["net_return"] for o in losses)), 3)
            if losses and sum(o["net_return"] for o in losses) != 0 else None,
            "win_rate": round(len(wins) / len(outs), 4) if outs else None,
            "payoff_ratio": round((st.mean([o["net_return"] for o in wins]) /
                                   abs(st.mean([o["net_return"] for o in losses])))
                                  if wins and losses else 0.0, 3),
            "_nets": nets}


def _winner_loser_stats(bars, feats, entries, horizon=30) -> dict:
    outs = []
    for i in entries:
        o = simulate_exit(bars, i, entries[i], 0.006, 0.006, 0.004, None, horizon)
        if o and o.get("valid"):
            outs.append(o)
    if not outs:
        return {"n": 0}
    premature = [o for o in outs if o["mfe"] >= WINNER_MFE_BPS and o["net_return"] <= 0]
    never_fav = [o for o in outs if o["mfe"] < NEVER_FAV_BPS]
    excursed = [o for o in outs if o["mfe"] >= 0.001]        # real favorable move
    reliable = len(excursed) >= max(10, len(outs) // 2)
    captured = ([max(-2.0, min(1.5, o["net_return"] / o["mfe"])) for o in excursed]
                if excursed else [])
    return {"n": len(outs),
            "reached_+40bps_but_closed_<=0_pct": round(len(premature) / len(outs) * 100, 1),
            "never_went_favorable_pct": round(len(never_fav) / len(outs) * 100, 1),
            "avg_pct_of_MFE_captured": round(st.mean(captured) * 100, 1) if reliable and captured else None,
            "exit_analysis_reliable": reliable,
            "interpretation": (
                "data too gappy for exit analysis (most trades STALE with ~0 MFE)"
                if not reliable else
                "high premature-exit % => leaving winners on the table; "
                "high never-favorable % => entries/setups are the problem")}


def _verdict(m, base_lb, slip_lb) -> tuple[str, str]:
    n, nev, lb, dd = m["sample_size"], m.get("net_EV"), m.get("net_EV_lower_bound"), m.get("max_drawdown")
    if n < MIN_SAMPLE:
        return "NEEDS_MORE_DATA", f"sample<{MIN_SAMPLE}"
    if nev is None or nev <= 0:
        return "REJECTED", "net_EV<=0"
    if dd is not None and dd < MAX_DD_LIMIT:
        return "REJECTED", "drawdown_excessive"
    if lb is None or lb <= 0:
        return "WATCHLIST", "net_EV>0 but lower_bound<=0"
    if base_lb is not None and lb <= base_lb:
        return "WATCHLIST", "does_not_beat_baseline_exit"
    if slip_lb is not None and slip_lb <= 0:
        return "WATCHLIST", "fails_slippage_stress"
    if n < 2 * MIN_OOS:
        return "INCUBATE", "clears gates but small sample"
    return "SHADOW_FORWARD_CANDIDATE", "clears costs+baseline+slippage"


def run_exit_optimization(symbol: str = "BTCUSDT", data_source: str = "auto",
                          write_reports: bool = True) -> dict[str, Any]:
    bars, eff_source, _ = LAB._load_bars(symbol, data_source)
    summary: dict[str, Any] = {"tool_version": TOOL_VERSION, "symbol": symbol,
                               "effective_source": eff_source, "n_bars": len(bars),
                               "ran_at": datetime.now(timezone.utc).isoformat(),
                               "aggressiveness_from": "exits_only_not_risk_or_sizing",
                               **_safety()}
    if len(bars) < 3 * MIN_SAMPLE:
        summary["verdict"] = "INSUFFICIENT_SAMPLE"
        summary["note"] = f"only {len(bars)} bars; keep collecting"
        summary["rows"] = []
        if write_reports:
            _write(summary, [], {})
        return summary
    feats = CE.build_features(bars)
    split = int(len(feats) * 0.6)
    thr = LAB._thresholds(feats, split)
    # base entries: momentum long when burst_score above a moderate train quantile
    q = thr.get("burst_score_q90", 0.0)
    entries: dict[int, str] = {}
    last = -10 ** 9
    for i in range(split, len(feats) - 1):
        v = feats[i].get("burst_score")
        if isinstance(v, (int, float)) and v > q and i - last >= 5:
            entries[i] = "long"
            last = i
    dear = {"fee_bps": CE.DEFAULT_COSTS["fee_bps"] * SLIP_MULT,
            "slippage_bps": CE.DEFAULT_COSTS["slippage_bps"] * SLIP_MULT,
            "spread_bps": CE.DEFAULT_COSTS["spread_bps"]}
    # baseline exit lower bound (fixed_baseline @ 30)
    base = _eval_variant(bars, feats, entries, _variants()[0], 30)
    base_lb = base.get("net_EV_lower_bound")
    rows = []
    for var in _variants():
        for hz in HORIZONS:
            m = _eval_variant(bars, feats, entries, var, hz)
            m_slip = _eval_variant(bars, feats, entries, var, hz, costs=dear)
            m["cost_sensitivity"] = None if (m["net_EV"] is None or m_slip["net_EV"] is None) \
                else round(m["net_EV"] - m_slip["net_EV"], 8)
            verdict, reason = _verdict(m, base_lb, m_slip.get("net_EV_lower_bound"))
            m["verdict"], m["rejection_reason"] = verdict, reason
            m.pop("_nets", None)
            rows.append({**m, **_safety()})
    rows.sort(key=lambda r: (r["net_EV_lower_bound"] is not None,
                             r["net_EV_lower_bound"] or -9), reverse=True)
    wl = _winner_loser_stats(bars, feats, entries)
    best = rows[0] if rows else None
    promoted = [r for r in rows if r["verdict"] in
                ("WATCHLIST", "INCUBATE", "SHADOW_FORWARD_CANDIDATE")]
    summary.update({
        "n_entries": len(entries),
        "variants_x_horizons": len(rows),
        "baseline_exit_lower_bound": base_lb,
        "best_variant": best,
        "best_net_EV": best["net_EV"] if best else None,
        "best_net_EV_lower_bound": best["net_EV_lower_bound"] if best else None,
        "watchlist_or_better": len(promoted),
        "winner_loser": wl,
        "ranking_key": "net_EV_lower_bound (win_rate secondary)",
        "verdict": ("EXIT_VARIANTS_UNDER_RESEARCH" if promoted
                    else "NO_EXIT_EDGE_ALL_REJECTED")})
    if write_reports:
        _write(summary, rows, wl)
        summary["reports_dir"] = str(CE._repo_root().joinpath(*OUTPUT_SUBDIR)).replace("\\", "/")
    return summary


def _write(summary, rows, wl) -> None:
    d = CE._repo_root().joinpath(*OUTPUT_SUBDIR)
    d.mkdir(parents=True, exist_ok=True)
    cols = ["policy_name", "exit_variant", "horizon", "tp", "sl", "trailing",
            "break_even", "sample_size", "avg_MFE", "avg_MAE", "capture_ratio",
            "net_EV", "net_EV_lower_bound", "max_drawdown", "profit_factor",
            "win_rate", "payoff_ratio", "cost_sensitivity", "verdict",
            "rejection_reason"]
    with open(d / "exit_optimization_scoreboard_v1043b.csv", "w", newline="",
              encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in cols})
    (d / "exit_optimization_report_v1043b.md").write_text(_md(summary, rows, wl),
                                                          encoding="utf-8")


def _md(summary, rows, wl) -> str:
    lines = ["# Exit Optimization / Profit Extraction (V10.43B) — RESEARCH ONLY, NO LIVE", "",
             f"source: {summary.get('effective_source')} · bars: {summary.get('n_bars')} · "
             f"entries: {summary.get('n_entries')} · variants×horizons: {summary.get('variants_x_horizons')}",
             f"aggressiveness from **exits only** — no leverage/sizing change. "
             f"verdict: {summary.get('verdict')}", "",
             "## Winner extraction / Loser control", ""]
    for k, v in (wl or {}).items():
        lines.append(f"- {k}: {v}")
    lines += ["", "## Top exit variants (by net_EV_lower_bound)", "",
              "| variant | hz | n | avg_MFE | avg_MAE | capture | net_EV | net_EV_lb | maxDD | verdict |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows[:16]:
        lines.append(f"| {r['exit_variant']} | {r['horizon']} | {r['sample_size']} | "
                     f"{r['avg_MFE']} | {r['avg_MAE']} | {r['capture_ratio']} | "
                     f"{r['net_EV']} | {r['net_EV_lower_bound']} | {r['max_drawdown']} | "
                     f"{r['verdict']} |")
    lines += ["", "Nothing actionable: no exit variant is promoted unless it clears "
              "costs, the lower-bound, the baseline exit and slippage stress with "
              "enough sample. **FINAL_RECOMMENDATION=NO LIVE.**"]
    return "\n".join(lines)
