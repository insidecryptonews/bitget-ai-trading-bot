"""ResearchOps V10.43B - Autonomous Strategy Lab / Strategy Factory (research only).

Generates a controlled universe of MECHANICAL strategy candidates (triggers x
filters x exits), evaluates each with the V10.40 shadow simulator (entry
next_open, SL-first ties, costs+slippage, DATA_GAP blocking, no lookahead),
auto-rejects the bad ones and ranks the survivors by net_EV_lower_bound (never
by win rate). Runs over the WS continuous dataset or the REST dataset.

Also a first Delayed-Repricing / Lead-Lag layer (BTC-only today => internal
repricing hypotheses; multi-symbol lead-lag = WAITING_DATA, never invented).

Everything RESEARCH_ONLY, fail-closed, NO LIVE, no orders, no keys.
"""

from __future__ import annotations

import csv
import json
import os
import statistics as st
from datetime import datetime, timezone
from typing import Any, Callable

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE
from . import shadow_simulation_tournament_v10_40 as SH
from . import ws_dataset_integration_v10_43b as WS

TOOL_VERSION = "v10.43b"
OUTPUT_SUBDIR = ("reports", "research", "strategy_lab_v10_43b")
WS_TOUR_SUBDIR = ("reports", "research", "shadow_simulation_ws_v10_43b")
MIN_SAMPLE = 30
MIN_OOS = 20
MAX_DD_LIMIT = -0.10
SLIP_STRESS_MULT = 1.5

VERDICTS = ("REJECTED", "WATCHLIST", "INCUBATE",
            "SHADOW_FORWARD_CANDIDATE", "NEEDS_MORE_DATA")
WS_TOUR_VERDICTS = ("NO_WS_DATA", "WS_TOO_GAPPY", "INSUFFICIENT_SAMPLE",
                    "NO_EDGE_ALL_REJECTED", "WATCHLIST", "INCUBATE",
                    "SHADOW_FORWARD_CANDIDATE")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "not_actionable": True, "no_orders": True,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


# ==========================================================================
# Data source
# ==========================================================================

def _load_bars(symbol: str, data_source: str) -> tuple[list[dict], str, dict]:
    """Resolve data source and return (bars, effective_source, meta).

    Sources: rest (V10.32) | ws (V10.42) | ws_persistent (V10.43C) | auto. `auto`
    prefers the MOST CONTINUOUS available dataset (persistent > ws > rest)."""
    def rest():
        try:
            return CE.load_dataset(symbol).get("bars") or []
        except Exception:
            return []
    if data_source == "rest":
        return rest(), "rest", {}
    if data_source == "ws_persistent":
        from . import ws_continuity_v10_43c as PWS
        r = PWS.load_persistent_bars(symbol)
        return r["bars"], "ws_persistent", r["meta"]
    ws = WS.load_ws_bars(symbol)
    if data_source == "ws":
        return ws["bars"], "ws", ws["meta"]
    # auto: prefer the most continuous available source (persistent > ws > rest)
    try:
        from . import ws_continuity_v10_43c as PWS
        cmp3 = PWS.dataset_source_compare_3way(symbol)
        if cmp3["recommended_source"] == "ws_persistent":
            pr = PWS.load_persistent_bars(symbol)
            if pr["bars"]:
                return pr["bars"], "ws_persistent", pr["meta"]
        if cmp3["recommended_source"] == "ws" and ws["bars"]:
            return ws["bars"], "ws", ws["meta"]
    except Exception:
        pass
    cmp = WS.dataset_source_compare(symbol)
    if cmp["recommended_source"] == "ws" and ws["bars"]:
        return ws["bars"], "ws", ws["meta"]
    return rest(), "rest", {}


# ==========================================================================
# Strategy universe (mechanical; trigger x filter x side x exits)
# ==========================================================================

def _regime_ok(f: dict, filt: str | None) -> bool:
    if filt is None:
        return True
    if filt == "trend":
        return f.get("symbol_regime") == "trend"
    if filt == "chop":
        return f.get("symbol_regime") == "chop"
    if filt == "high_vol":
        return f.get("stress_mode", 0.0) == 1.0 or f.get("realized_volatility", 0) > 0
    if filt == "high_liquidity":
        return f.get("liquidity_regime", 0.0) == 1.0
    return True


def _make_fn(feat: str, side: str, direction: str, filt: str | None) -> Callable:
    # direction 'above' fires when feat > +thr; 'below' when feat < -thr
    def fn(f, p, thr, rng):
        if not _regime_ok(f, filt):
            return None
        v = f.get(feat)
        if not isinstance(v, (int, float)):
            return None
        t = thr.get(feat + "_q90", 0.0)
        fired = (v > t) if direction == "above" else (v < -t)
        return side if fired else None
    return fn


def _strategy_universe() -> list[dict]:
    S = []

    def add(name, family, feat, side, direction, filt, tp, sl, trail, hz, cd):
        S.append({"name": name, "family": family, "trigger": feat, "side": side,
                  "direction": direction, "filter": filt, "tp": tp, "sl": sl,
                  "trail": trail, "horizon": hz, "cooldown": cd,
                  "fn": _make_fn(feat, side, direction, filt)})
    # momentum
    add("micro_burst_continuation_long_v1", "momentum", "burst_score", "long", "above", None, 0.006, 0.006, 0.004, 30, 5)
    add("micro_burst_exhaustion_short_v1", "mean_reversion", "burst_score", "short", "above", "chop", 0.006, 0.006, 0.004, 20, 5)
    add("flow_imbalance_continuation_long_v1", "momentum", "buy_sell_imbalance", "long", "above", None, 0.006, 0.006, 0.004, 30, 5)
    add("flow_imbalance_reversal_short_v1", "mean_reversion", "buy_sell_imbalance", "short", "above", "chop", 0.006, 0.006, 0.004, 20, 5)
    add("aggressive_flow_long_v1", "momentum", "aggressive_flow_proxy", "long", "above", None, 0.006, 0.006, 0.004, 30, 5)
    # volatility
    add("volatility_expansion_long_v1", "breakout", "realized_volatility", "long", "above", "trend", 0.008, 0.006, 0.005, 30, 6)
    add("volatility_breakout_trend_long_v1", "breakout", "trend_score", "long", "above", "high_vol", 0.008, 0.006, 0.005, 30, 6)
    # trend / reversion
    add("trend_pullback_long_v1", "trend", "trend_score", "long", "above", None, 0.006, 0.006, 0.004, 40, 6)
    add("trend_pullback_short_v1", "trend", "trend_score", "short", "below", None, 0.006, 0.006, 0.004, 40, 6)
    add("chop_mean_reversion_short_v1", "mean_reversion", "buy_sell_imbalance", "short", "above", "chop", 0.005, 0.005, None, 15, 4)
    # microstructure context
    add("orderbook_pressure_long_v1", "microstructure", "book_pressure", "long", "above", None, 0.006, 0.006, 0.004, 30, 5)
    add("oi_confirmation_long_v1", "oi_funding", "oi_change", "long", "above", "trend", 0.006, 0.006, 0.004, 30, 5)
    add("funding_fade_short_v1", "oi_funding", "funding_level", "short", "above", None, 0.006, 0.006, 0.004, 45, 8)
    add("liquidation_reversal_long_v1", "liquidations", "liquidation_side_imbalance", "long", "below", None, 0.006, 0.006, 0.004, 20, 5)
    return S


# ==========================================================================
# Evaluation (reuses the V10.40 shadow simulator via SH.run_policy)
# ==========================================================================

def _eval_one(strat: dict, feats: list[dict], bars: list[dict], split: int,
              thr: dict, costs: dict | None = None) -> dict:
    r = SH.run_policy(strat["name"], strat["family"], strat["fn"], feats, bars,
                      split, thr, tp_pct=strat["tp"], sl_pct=strat["sl"],
                      time_bars=strat["horizon"], trailing_pct=strat["trail"],
                      costs=costs, cooldown=strat["cooldown"], entry_mode="next_open")
    return r["metrics"]


def _verdict(m: dict, best_baseline_lb: float, slip_lb) -> tuple[str, str]:
    n = m.get("n_signals", 0)
    nev = m.get("net_EV")
    lb = m.get("net_EV_lower_bound")
    dd = m.get("max_drawdown")
    if n < MIN_SAMPLE:
        return "NEEDS_MORE_DATA", f"sample<{MIN_SAMPLE}"
    if nev is None or nev <= 0:
        return "REJECTED", "net_EV<=0 (cost-dominated or negative)"
    if dd is not None and dd < MAX_DD_LIMIT:
        return "REJECTED", "drawdown_excessive"
    if lb is None or lb <= 0:
        return "WATCHLIST", "net_EV>0 but lower_bound<=0"
    if lb <= best_baseline_lb:
        return "WATCHLIST", "does_not_beat_baselines"
    if slip_lb is not None and slip_lb <= 0:
        return "WATCHLIST", "fails_slippage_stress"
    if n < 2 * MIN_OOS:
        return "INCUBATE", "clears_costs+baseline+slippage but small sample"
    return "SHADOW_FORWARD_CANDIDATE", "clears costs, baselines and slippage stress"


def run_lab(symbol: str = "BTCUSDT", data_source: str = "auto",
            write_reports: bool = True,
            include_candidates: bool = False) -> dict[str, Any]:
    bars, eff_source, meta = _load_bars(symbol, data_source)
    summary: dict[str, Any] = {"tool_version": TOOL_VERSION, "symbol": symbol,
                               "requested_source": data_source,
                               "effective_source": eff_source,
                               "ran_at": datetime.now(timezone.utc).isoformat(),
                               "n_bars": len(bars), **_safety()}
    if len(bars) < 3 * MIN_SAMPLE:
        summary["verdict"] = ("NO_WS_DATA" if eff_source == "ws" and not bars
                              else "INSUFFICIENT_SAMPLE")
        summary["note"] = f"only {len(bars)} bars from {eff_source}; keep collecting"
        summary["candidates"] = []
        if include_candidates:
            summary["candidates_detail"] = []
        if write_reports:
            _write(summary, [], [], {}, _lead_lag(symbol, bars, eff_source))
        return summary
    feats = CE.build_features(bars)
    split = int(len(feats) * 0.6)
    thr = SH._train_thresholds(feats, split) if hasattr(SH, "_train_thresholds") \
        else _thresholds(feats, split)
    # baselines (reuse SH policy universe baselines)
    base_lbs = []
    for name, (kind, fn) in SH._policies().items():
        if kind != "baseline":
            continue
        r = SH.run_policy(name, kind, fn, feats, bars, split, thr, tp_pct=0.006,
                          sl_pct=0.006, time_bars=30, trailing_pct=0.004,
                          costs=None, cooldown=5, entry_mode="next_open")
        lb = r["metrics"].get("net_EV_lower_bound")
        if lb is not None:
            base_lbs.append(lb)
    best_baseline_lb = max(base_lbs) if base_lbs else -9.0
    dear = {"fee_bps": CE.DEFAULT_COSTS["fee_bps"] * SLIP_STRESS_MULT,
            "slippage_bps": CE.DEFAULT_COSTS["slippage_bps"] * SLIP_STRESS_MULT,
            "spread_bps": CE.DEFAULT_COSTS["spread_bps"]}
    candidates = []
    for strat in _strategy_universe():
        m = _eval_one(strat, feats, bars, split, thr)
        m_slip = _eval_one(strat, feats, bars, split, thr, costs=dear)
        slip_lb = m_slip.get("net_EV_lower_bound")
        verdict, reason = _verdict(m, best_baseline_lb, slip_lb)
        candidates.append({
            "strategy_name": strat["name"], "family": strat["family"],
            "trigger": strat["trigger"], "side": strat["side"],
            "filter": strat["filter"], "timeframe": "1m",
            "entry_rule": f"{strat['trigger']} {strat['direction']} train-q90 (next_open)",
            "exit_rule": f"TP {strat['tp']} / SL {strat['sl']} / trail {strat['trail']} / {strat['horizon']}bars",
            "tp": strat["tp"], "sl": strat["sl"], "trailing": strat["trail"],
            "max_horizon": strat["horizon"], "cooldown": strat["cooldown"],
            "required_data_source": eff_source, "required_data_quality": "shadow_forward",
            "sample_size": m.get("n_signals"), "net_EV": m.get("net_EV"),
            "net_EV_lower_bound": m.get("net_EV_lower_bound"),
            "profit_factor": m.get("profit_factor"), "win_rate": m.get("win_rate"),
            "payoff_ratio": m.get("payoff_ratio"), "max_drawdown": m.get("max_drawdown"),
            "cost_sensitivity": _round(_delta(m.get("net_EV"), m_slip.get("net_EV"))),
            "slippage_stress_lb": slip_lb,
            "baseline_comparison": _round(_delta(m.get("net_EV_lower_bound"), best_baseline_lb)),
            "verdict": verdict, "rejection_reason": reason, **_safety()})
    candidates.sort(key=lambda c: (c["net_EV_lower_bound"] is not None,
                                   c["net_EV_lower_bound"] or -9), reverse=True)
    counts = {v: sum(1 for c in candidates if c["verdict"] == v) for v in VERDICTS}
    promoted = [c for c in candidates if c["verdict"] in
                ("WATCHLIST", "INCUBATE", "SHADOW_FORWARD_CANDIDATE")]
    lead = _lead_lag(symbol, bars, eff_source)
    summary.update({
        "candidates_generated": len(candidates),
        "verdict_counts": counts,
        "best": candidates[0] if candidates else None,
        "best_net_EV": candidates[0]["net_EV"] if candidates else None,
        "best_net_EV_lower_bound": candidates[0]["net_EV_lower_bound"] if candidates else None,
        "watchlist_or_better": len(promoted),
        "top_rejection_reasons": _top_reasons(candidates),
        "lead_lag": lead.get("verdict"),
        "best_baseline_lower_bound": round(best_baseline_lb, 8),
        "ranking_key": "net_EV_lower_bound (win_rate secondary)",
        "verdict": ("STRATEGIES_UNDER_RESEARCH" if promoted
                    else "NO_EDGE_ALL_REJECTED")})
    if include_candidates:
        summary["candidates_detail"] = candidates
    if write_reports:
        _write(summary, candidates, promoted, counts, lead)
        summary["reports_dir"] = str(CE._repo_root().joinpath(*OUTPUT_SUBDIR)).replace("\\", "/")
    return summary


def _thresholds(feats, split):
    keys = ("burst_score", "buy_sell_imbalance", "aggressive_flow_proxy",
            "trend_score", "realized_volatility", "book_pressure", "oi_change",
            "funding_level", "liquidation_side_imbalance")
    thr = {}
    for k in keys:
        vals = sorted(f[k] for f in feats[:split] if isinstance(f.get(k), (int, float)))
        thr[k + "_q90"] = vals[int(len(vals) * 0.9)] if vals else 0.0
    return thr


def _delta(a, b):
    return None if a is None or b is None else a - b


def _round(x):
    return None if x is None else round(x, 8)


def _top_reasons(cands):
    from collections import Counter
    c = Counter(x["rejection_reason"] for x in cands if x["verdict"] == "REJECTED")
    return [{"reason": r, "count": n} for r, n in c.most_common(5)]


# ==========================================================================
# Delayed repricing / lead-lag (BTC-only => internal follow-through hypotheses)
# ==========================================================================

def _lead_lag(symbol: str, bars: list[dict], source: str) -> dict[str, Any]:
    if len(bars) < 3 * MIN_SAMPLE:
        return {"verdict": "WAITING_DATA", "reason": "insufficient bars",
                "multi_symbol_lead_lag": "WAITING_DATA", **_safety()}
    feats = CE.build_features(bars)
    labels = CE.build_labels(bars, side="long", time_bars=10)
    rt = SH._round_trip()

    def follow_through(cond: Callable) -> dict:
        rets = [l["cost_adjusted_outcome"] for f, l in zip(feats, labels)
                if not l.get("missing") and cond(f)]
        n = len(rets)
        return {"n": n, "mean_net_after_cost": round(st.mean(rets), 6) if rets else None,
                "beats_cost": bool(rets and st.mean(rets) > 0)}
    hyp = {
        "burst_then_continuation": follow_through(lambda f: f.get("burst_score", 0) > 2),
        "strong_imbalance_delayed_move": follow_through(lambda f: f.get("buy_sell_imbalance", 0) > 0.1),
        "volatility_expansion_followthrough": follow_through(lambda f: f.get("stress_mode", 0) == 1.0),
        "failed_move_reversal": follow_through(lambda f: f.get("chop_score", 0) > 0.2),
    }
    return {"tool_version": TOOL_VERSION, "symbol": symbol, "source": source,
            "round_trip_cost": round(rt, 6),
            "internal_repricing_hypotheses": hyp,
            "multi_symbol_lead_lag": "WAITING_DATA (only BTCUSDT collected)",
            "verdict": "INTERNAL_REPRICING_MEASURED",
            "note": "no invented cross-asset correlations; single-symbol follow-through only",
            **_safety()}


# ==========================================================================
# WS tournament wrapper (B2) - reuses V10.40 run_tournament on WS bars
# ==========================================================================

def run_ws_tournament(symbol: str = "BTCUSDT", write_reports: bool = True,
                      source: str = "ws") -> dict[str, Any]:
    if source == "ws_persistent":
        from . import ws_continuity_v10_43c as PWS
        loaded = PWS.load_persistent_bars(symbol)
        _src_label = "ws_persistent_v10_43c"
    else:
        loaded = WS.load_ws_bars(symbol)
        _src_label = "ws_v10_42"
    bars = loaded["bars"]
    if not bars:
        rep = {"tool_version": TOOL_VERSION, "symbol": symbol, "verdict": "NO_WS_DATA",
               "source": _src_label, "n_bars": 0, **_safety()}
        if write_reports:
            _write_ws_tour(rep, {})
        return rep
    if source == "ws_persistent":
        from . import ws_continuity_v10_43c as PWS
        ca = PWS.ws_continuity_audit(symbol)
        view = {"verdict": ca.get("verdict"), "reliability": ca.get("reliability"),
                "max_contiguous_run": ca.get("max_contiguous_run")}
        gappy = ca.get("verdict") in ("WS_TOO_GAPPY", "WS_STALE", "WS_COLLECTOR_DOWN")
    else:
        view = WS.ws_forward_dataset_view(symbol)
        gappy = (view.get("verdict") == "WS_TOO_GAPPY"
                 or view.get("reliability") == "NOT_RELIABLE_GAPS")
    t = SH.run_tournament(symbol, bars=bars, entry_mode="next_open", write_reports=False)
    n_best = ((t.get("best_strategy") or {}).get("n_signals")) or 0
    if t.get("verdict") == "NEEDS_MORE_DATA" or n_best < MIN_OOS:
        verdict = "INSUFFICIENT_SAMPLE"
    elif t.get("any_strategy_beats_baseline_and_costs"):
        verdict = "SHADOW_FORWARD_CANDIDATE"
    elif gappy:
        verdict = "WS_TOO_GAPPY"
    else:
        verdict = "NO_EDGE_ALL_REJECTED"
    rep = {"tool_version": TOOL_VERSION, "symbol": symbol, "source": _src_label,
           "n_bars": len(bars), "ws_forward_verdict": view.get("verdict"),
           "ws_max_contiguous_run": view.get("max_contiguous_run"),
           "any_strategy_beats_baseline_and_costs": t.get("any_strategy_beats_baseline_and_costs"),
           "best_strategy": t.get("best_strategy"),
           "scoreboard_top": t.get("scoreboard_top", [])[:8],
           "micro_live_ready": False, "verdict": verdict, **_safety()}
    if write_reports:
        _write_ws_tour(rep, t)
    return rep


# ==========================================================================
# Report writers
# ==========================================================================

def _write(summary, candidates, promoted, counts, lead) -> None:
    d = CE._repo_root().joinpath(*OUTPUT_SUBDIR)
    d.mkdir(parents=True, exist_ok=True)
    _wj(d / "strategy_research_memo_v1043b.md.tmp", None)  # placeholder guard
    with open(d / "strategy_candidates_v1043b.csv", "w", newline="", encoding="utf-8") as f:
        cols = ["strategy_name", "family", "trigger", "side", "filter", "entry_rule",
                "exit_rule", "max_horizon", "cooldown", "required_data_source",
                "sample_size", "net_EV", "net_EV_lower_bound", "profit_factor",
                "win_rate", "payoff_ratio", "max_drawdown", "cost_sensitivity",
                "slippage_stress_lb", "baseline_comparison", "verdict", "rejection_reason"]
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for c in candidates:
            w.writerow({k: c.get(k) for k in cols})
    with open(d / "strategy_scoreboard_v1043b.csv", "w", newline="", encoding="utf-8") as f:
        cols = ["strategy_name", "family", "side", "verdict", "sample_size",
                "net_EV", "net_EV_lower_bound", "profit_factor", "win_rate",
                "max_drawdown"]
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for c in candidates:
            w.writerow({k: c.get(k) for k in cols})
    with open(d / "strategy_incubator_watchlist_v1043b.csv", "w", newline="", encoding="utf-8") as f:
        cols = ["strategy_name", "family", "side", "sample_size", "net_EV",
                "net_EV_lower_bound", "verdict", "rejection_reason"]
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for c in promoted:
            w.writerow({k: c.get(k) for k in cols})
    _wj(d / "strategy_rejection_reasons_v1043b.json",
        {"top_rejection_reasons": summary.get("top_rejection_reasons", []),
         "verdict_counts": counts, **_safety()})
    _wj(d / "lead_lag_report_v1043b.json", lead)
    (d / "strategy_research_memo_v1043b.md").write_text(_memo(summary, candidates),
                                                        encoding="utf-8")
    try:
        os.remove(d / "strategy_research_memo_v1043b.md.tmp")
    except Exception:
        pass


def _write_ws_tour(rep, full) -> None:
    d = CE._repo_root().joinpath(*WS_TOUR_SUBDIR)
    d.mkdir(parents=True, exist_ok=True)
    _wj(d / "shadow_summary_ws_v1043b.json", rep)
    with open(d / "shadow_scoreboard_ws_v1043b.csv", "w", newline="", encoding="utf-8") as f:
        cols = ["policy", "kind", "verdict", "n_signals", "net_EV",
                "net_EV_lower_bound", "win_rate", "max_drawdown"]
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for m in rep.get("scoreboard_top", []):
            w.writerow({k: m.get(k) for k in cols})
    (d / "shadow_research_memo_ws_v1043b.md").write_text(
        f"# WS Shadow Tournament Memo (V10.43B)\n\nverdict: {rep.get('verdict')}\n"
        f"source: ws_v10_42 · n_bars: {rep.get('n_bars')} · "
        f"ws_max_run: {rep.get('ws_max_contiguous_run')}\n"
        f"any_strategy_beats_baseline_and_costs: {rep.get('any_strategy_beats_baseline_and_costs')}\n\n"
        f"**FINAL_RECOMMENDATION=NO LIVE.**\n", encoding="utf-8")


def _wj(path, obj):
    if obj is None:
        return
    tmp = str(path) + ".tmp"
    from pathlib import Path
    Path(tmp).write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _memo(summary, candidates) -> str:
    lines = ["# Autonomous Strategy Lab Memo (V10.43B) — RESEARCH ONLY, NO LIVE", "",
             f"source: {summary.get('effective_source')} · bars: {summary.get('n_bars')} · "
             f"candidates: {summary.get('candidates_generated')} · "
             f"verdict: {summary.get('verdict')}", "",
             f"verdict_counts: {summary.get('verdict_counts')}", "",
             "| strategy | family | side | verdict | n | net_EV | net_EV_lb | reason |",
             "|---|---|---|---|---|---|---|---|"]
    for c in candidates[:14]:
        lines.append(f"| {c['strategy_name']} | {c['family']} | {c['side']} | "
                     f"{c['verdict']} | {c['sample_size']} | {c['net_EV']} | "
                     f"{c['net_EV_lower_bound']} | {c['rejection_reason']} |")
    lines += ["", "Ranking by net_EV_lower_bound (win rate secondary). "
              "Nothing actionable. **FINAL_RECOMMENDATION=NO LIVE.**"]
    return "\n".join(lines)
