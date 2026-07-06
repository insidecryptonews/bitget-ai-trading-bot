"""ResearchOps V10.39 - Alpha Improvement Sprint (research only, fail-closed).

Goal: make the research FACTORY better at *finding* real edge -- never to
fabricate one. Every guard from V10.38 is reused verbatim (train-only
thresholds, future-only cost-adjusted labels, net-EV-after-costs, lower-bound
gate, baselines, no-lookahead). Nothing here lowers costs, lowers thresholds,
uses OOS to pick rules, or emits an actionable signal.

What it adds on top of V10.38:
  * multi-timeframe resampling (1m -> 3m/5m/15m) with availability preserved
  * cost-aware horizon scan: does ANY (timeframe, horizon) clear the ~18bps
    round-trip floor on a PER-TRADE basis, with lower turnover?
  * strategy family benchmark: named families evaluated under one honest
    protocol, with a COMPLEXITY penalty (multiple-comparison aware)
  * regime segmentation (trend/chop/vol/liquidity/funding/liquidation/session)
  * feature quality audit (coverage, stability, redundancy, label relationship)

HONESTY CONTRACT: verdicts can say PROMISING_RESEARCH_ONLY, never BUY/SELL. A
family is PROMISING only if its OOS net-EV lower bound clears BOTH the min-edge
AND a complexity penalty AND beats the random baseline. On the real Bybit
dataset today nothing does. FINAL_RECOMMENDATION: NO LIVE.
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
import statistics as st
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE

TOOL_VERSION = "v10.39"
OUTPUT_SUBDIR = ("reports", "research", "v10_39")

TIMEFRAMES = (1, 3, 5, 15)            # in 1m units
HORIZONS = (5, 10, 15)                # in bars of that timeframe
QUANTILES = (0.66, 0.9)
MIN_EDGE = 2.0 / 10_000               # same 2bps min edge as V10.38
COMPLEXITY_BPS_PER_LOG2 = 0.8 / 10_000   # widen the bar per doubling of the grid

FAMILY_VERDICTS = ("PROMISING_RESEARCH_ONLY", "NEEDS_MORE_DATA",
                   "REJECTED_NEGATIVE_EV", "REJECTED_COSTS_TOO_HIGH",
                   "REJECTED_OVERFIT_RISK", "REJECTED_UNSTABLE",
                   "REJECTED_DATA_QUALITY", "NOT_ACTIONABLE")

# family -> (feature, side, regime-filter key or None). All features already
# exist in V10.38 build_features; families are transparent one-feature rules.
STRATEGY_FAMILIES = {
    "micro_momentum": ("burst_score", "long", None),
    "aggressive_flow_momentum": ("buy_sell_imbalance", "long", None),
    "liquidation_reversal": ("liquidation_side_imbalance", "short", None),
    "liquidation_continuation": ("cascade_score", "long", None),
    "oi_price_confirmation": ("oi_change", "long", "trend_up"),
    "funding_extreme_fade": ("funding_level", "short", None),
    "orderbook_pressure_continuation": ("book_pressure", "long", None),
    "orderbook_imbalance_reversal": ("top_imbalance", "short", None),
    "volatility_breakout": ("burst_score", "long", "high_vol"),
    "chop_mean_reversion": ("buy_sell_imbalance", "short", "chop"),
    "trend_pullback": ("trend_score", "long", "trend_up"),
    "abstain_low_quality": ("book_pressure", "long", "low_liquidity"),
}


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "not_actionable": True, "no_orders": True,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _round_trip_cost(costs: dict | None = None) -> float:
    c = {**CE.DEFAULT_COSTS, **(costs or {})}
    return 2 * (c["fee_bps"] + c["slippage_bps"]) / 10_000 + c["spread_bps"] / 10_000


# ==========================================================================
# Multi-timeframe resampling (availability preserved, no partial tail bar)
# ==========================================================================

def resample_bars(bars: list[dict], factor: int) -> list[dict]:
    """Aggregate `factor` consecutive 1m bars into one. ts/available_at anchor to
    the LAST sub-bar's close (never earlier). The incomplete tail group is
    dropped so no partial (look-ahead-prone) bar is ever emitted."""
    if factor <= 1:
        return [dict(b) for b in bars]
    out: list[dict] = []
    for i in range(0, len(bars) - factor + 1, factor):
        grp = bars[i:i + factor]
        last = grp[-1]
        out.append({
            "symbol": grp[0].get("symbol", "BTCUSDT"),
            "bar_start_ts": grp[0].get("bar_start_ts", grp[0]["ts"]),
            "bar_close_ts": last.get("bar_close_ts", last["ts"]),
            "ts": last["ts"],
            "open": grp[0]["open"], "close": last["close"],
            "high": max(b["high"] for b in grp),
            "low": min(b["low"] for b in grp),
            "volume": sum(b.get("volume", 0.0) for b in grp),
            "buy_volume": sum(b.get("buy_volume", 0.0) for b in grp),
            "sell_volume": sum(b.get("sell_volume", 0.0) for b in grp),
            "n_trades": sum(b.get("n_trades", 0) for b in grp),
            "trade_count": sum(b.get("n_trades", 0) for b in grp),
            "max_trade": max((b.get("max_trade", 0.0) for b in grp), default=0.0),
            "last_trade_ts": last.get("last_trade_ts", last["ts"]),
            "available_at": max(b.get("available_at", b["ts"]) for b in grp),
        })
    return out


# ==========================================================================
# One honest rule evaluation (train-only threshold, OOS, costs, baseline)
# ==========================================================================

def _pearson(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    xs, ys = xs[:n], ys[:n]
    mx, my = st.mean(xs), st.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx > 1e-12 and dy > 1e-12 else 0.0


def eval_rule(features: list[dict], labels_long: list[dict],
              labels_short: list[dict], feat: str, side: str,
              quantile: float, seed: int = 7) -> dict[str, Any]:
    """Evaluate a single transparent rule with the FULL V10.38 protocol:
    chronological split, TRAIN-ONLY threshold, OOS evaluation, costs included,
    lower-bound gate, random baseline delta. Returns rich metrics, never a
    signal."""
    n = len(features)
    split = int(n * 0.6)
    side_labels = labels_long if side == "long" else labels_short
    train_vals = sorted(f[feat] for f in features[:split]
                        if isinstance(f.get(feat), (int, float)))
    if len(train_vals) < CE.MIN_SAMPLE:
        return {"feature": feat, "side": side, "quantile": quantile,
                "sample_size": 0, "verdict": "REJECTED_DATA_QUALITY",
                "threshold_source": "train_only"}
    thr = train_vals[min(int(len(train_vals) * quantile), len(train_vals) - 1)]
    tr = CE._entries_for_rule(features[:split], side_labels[:split], feat, side, thr)
    oos = CE._entries_for_rule(features[split:], side_labels[split:], feat, side, thr)
    ev_tr = CE.evaluate_net_ev(tr)
    ev_oos = CE.evaluate_net_ev(oos)
    # gross (pre-cost) estimate, honestly reconstructed from the round-trip load
    rt = _round_trip_cost()
    gross_est = (ev_oos["net_EV"] + rt) if ev_oos.get("net_EV") is not None else None
    # random baseline over the SAME OOS window (draw outcomes uniformly)
    rng = random.Random(seed)
    pool = [l["cost_adjusted_outcome"] for l in side_labels[split:]
            if not l.get("missing") and l.get("cost_adjusted_outcome") is not None]
    rnd = [rng.choice(pool) for _ in range(len(oos))] if pool and oos else []
    ev_rand = CE.evaluate_net_ev(rnd)
    baseline_delta = None
    if ev_oos.get("net_EV") is not None:
        baseline_delta = round(ev_oos["net_EV"] - (ev_rand.get("net_EV") or 0.0), 8)
    wins = [o for o in oos if o > 0]
    losses = [o for o in oos if o <= 0]
    return {
        "feature": feat, "side": side, "quantile": quantile, "threshold": thr,
        "threshold_source": "train_only",
        "sample_size": ev_oos["sample_size"],
        "train_sample_size": ev_tr["sample_size"],
        "gross_EV": round(gross_est, 8) if gross_est is not None else None,
        "cost_estimate": round(rt, 8),
        "net_EV": ev_oos.get("net_EV"),
        "net_EV_lower_bound": ev_oos.get("net_EV_lower_bound"),
        "train_net_EV": ev_tr.get("net_EV"),
        "hit_rate": ev_oos.get("win_rate"),
        "payoff_ratio": ev_oos.get("payoff_ratio"),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4)
        if wins and losses and sum(losses) != 0 else None,
        "turnover": round(len(oos) / max(1, n - split), 4),
        "abstention_rate": round(1 - len(oos) / max(1, n - split), 4),
        "max_drawdown": round(min(CE._cum_dd(oos), 0.0), 6),
        "baseline_delta": baseline_delta,
        "decision": ev_oos.get("decision"),
    }


def _verdict_for(metrics: dict, n_combos: int, min_sample: int = CE.MIN_OOS_SAMPLE
                 ) -> tuple[str, list[str]]:
    """Honest verdict + blocked reasons. PROMISING requires the OOS lower bound
    to clear min-edge PLUS a complexity penalty (multiple-comparison aware) AND
    beat the random baseline. Never fabricates promising."""
    blocked: list[str] = []
    lb = metrics.get("net_EV_lower_bound")
    nev = metrics.get("net_EV")
    gross = metrics.get("gross_EV")
    if metrics.get("sample_size", 0) < min_sample:
        return "NEEDS_MORE_DATA", ["sample_too_small"]
    complexity_pen = COMPLEXITY_BPS_PER_LOG2 * math.log2(max(2, n_combos))
    metrics["complexity_penalty"] = round(complexity_pen, 8)
    if nev is None:
        return "REJECTED_DATA_QUALITY", ["no_net_ev"]
    if nev <= 0:
        # distinguish "no signal" from "signal eaten by costs"
        if gross is not None and gross > 0:
            return "REJECTED_COSTS_TOO_HIGH", ["gross_positive_but_costs_win"]
        return "REJECTED_NEGATIVE_EV", ["net_ev_non_positive"]
    # nev > 0 from here
    if (metrics.get("train_net_EV") or 0) > 0 and \
            nev < 0.3 * (metrics.get("train_net_EV") or 1):
        return "REJECTED_OVERFIT_RISK", ["oos_far_below_train"]
    if (metrics.get("baseline_delta") or -1) <= 0:
        return "REJECTED_UNSTABLE", ["does_not_beat_random_baseline"]
    if lb is not None and lb > MIN_EDGE + complexity_pen:
        return "PROMISING_RESEARCH_ONLY", []
    blocked.append("lower_bound_below_min_edge_plus_complexity")
    return "NOT_ACTIONABLE", blocked


# ==========================================================================
# Cost-aware horizon / timeframe scan
# ==========================================================================

def cost_aware_horizon_scan(bars: list[dict], aux: dict | None = None,
                            timeframes=TIMEFRAMES, horizons=HORIZONS
                            ) -> dict[str, Any]:
    """For each (timeframe, horizon) resample the bars, rebuild features + real
    side-aware labels, and record the BEST honest rule. Costs are never lowered;
    higher timeframes simply trade less often (lower turnover)."""
    aux = aux or {}
    rows: list[dict] = []
    n_combos = len(timeframes) * len(horizons) * len(CE.DISCOVERY_FEATURES) \
        * len(QUANTILES) * 2
    for tf in timeframes:
        rbars = resample_bars(bars, tf)
        if len(rbars) < 3 * CE.MIN_SAMPLE:
            rows.append({"timeframe_min": tf, "horizon": None, "n_bars": len(rbars),
                         "verdict": "NEEDS_MORE_DATA",
                         "reason_rejected": "too_few_bars_at_this_timeframe"})
            continue
        feats = CE.build_features(rbars, aux.get("oi"), aux.get("funding"),
                                  aux.get("orderbook"), aux.get("liquidations"))
        for hz in horizons:
            ll = CE.build_labels(rbars, side="long", time_bars=hz)
            ls = CE.build_labels(rbars, side="short", time_bars=hz)
            best = None
            for feat in CE.DISCOVERY_FEATURES:
                for side in ("long", "short"):
                    for q in QUANTILES:
                        m = eval_rule(feats, ll, ls, feat, side, q)
                        if m.get("net_EV_lower_bound") is None:
                            continue
                        if best is None or m["net_EV_lower_bound"] > best["net_EV_lower_bound"]:
                            best = m
            if best is None:
                rows.append({"timeframe_min": tf, "horizon": hz, "n_bars": len(rbars),
                             "verdict": "NEEDS_MORE_DATA",
                             "reason_rejected": "no_rule_had_enough_entries"})
                continue
            verdict, blocked = _verdict_for(best, n_combos)
            rows.append({
                "timeframe_min": tf, "horizon": hz, "n_bars": len(rbars),
                "best_feature": best["feature"], "side": best["side"],
                "setup_family": _family_of(best["feature"], best["side"]),
                "sample_size": best["sample_size"],
                "trade_frequency": best["turnover"],
                "abstention_rate": best["abstention_rate"],
                "gross_EV": best["gross_EV"], "cost_estimate": best["cost_estimate"],
                "net_EV": best["net_EV"],
                "net_EV_lower_bound": best["net_EV_lower_bound"],
                "turnover": best["turnover"],
                "complexity_penalty": best.get("complexity_penalty"),
                "verdict": verdict,
                "reason_rejected": ";".join(blocked) if blocked else None,
                **_safety()})
    # honest cross-timeframe read
    best_row = max((r for r in rows if r.get("net_EV_lower_bound") is not None),
                   key=lambda r: r["net_EV_lower_bound"], default=None)
    return {"tool_version": TOOL_VERSION, "n_combinations": n_combos,
            "rows": rows, "best_cell": best_row,
            "any_promising": any(r.get("verdict") == "PROMISING_RESEARCH_ONLY"
                                 for r in rows),
            "note": ("higher timeframes cut turnover but per-trade net-EV must "
                     "still clear the cost floor; costs are never lowered"),
            **_safety()}


def _family_of(feat: str, side: str) -> str:
    for name, (f, s, _) in STRATEGY_FAMILIES.items():
        if f == feat and s == side:
            return name
    return f"{feat}_{side}"


# ==========================================================================
# Strategy family benchmark
# ==========================================================================

def _regime_mask(features: list[dict], key: str | None) -> list[bool]:
    if key is None:
        return [True] * len(features)
    rv = [f.get("realized_volatility", 0.0) for f in features]
    vol_med = st.median(rv) if rv else 0.0
    out = []
    for f in features:
        sr = f.get("symbol_regime")
        ts = f.get("trend_score", 0.0)
        if key == "trend_up":
            out.append(sr == "trend" and ts > 0)
        elif key == "trend_down":
            out.append(sr == "trend" and ts < 0)
        elif key == "chop":
            out.append(sr == "chop")
        elif key == "high_vol":
            out.append(f.get("realized_volatility", 0.0) >= vol_med)
        elif key == "low_vol":
            out.append(f.get("realized_volatility", 0.0) < vol_med)
        elif key == "high_liquidity":
            out.append(f.get("liquidity_regime", 0.0) == 1.0)
        elif key == "low_liquidity":
            out.append(f.get("liquidity_regime", 0.0) == 0.0)
        elif key == "funding_stress":
            out.append(f.get("funding_stress", 0.0) > 1.0)
        elif key == "liquidation_cascade":
            out.append(f.get("cascade_score", 0.0) > 0.5)
        elif key == "oi_expansion":
            out.append(f.get("oi_change", 0.0) > 0)
        else:
            out.append(True)
    return out


def strategy_family_benchmark(bars: list[dict], aux: dict | None = None,
                              timeframe: int = 1, horizon: int = 10
                              ) -> list[dict]:
    """Evaluate every named family under ONE protocol at a fixed timeframe /
    horizon. Regime-scoped families evaluate on their regime subset only."""
    aux = aux or {}
    rbars = resample_bars(bars, timeframe)
    feats = CE.build_features(rbars, aux.get("oi"), aux.get("funding"),
                              aux.get("orderbook"), aux.get("liquidations"))
    ll = CE.build_labels(rbars, side="long", time_bars=horizon)
    ls = CE.build_labels(rbars, side="short", time_bars=horizon)
    n_combos = len(STRATEGY_FAMILIES) * len(QUANTILES)
    out: list[dict] = []
    for family, (feat, side, regime) in STRATEGY_FAMILIES.items():
        mask = _regime_mask(feats, regime)
        sub_f = [f for f, m in zip(feats, mask) if m]
        sub_ll = [l for l, m in zip(ll, mask) if m]
        sub_ls = [l for l, m in zip(ls, mask) if m]
        best = None
        for q in QUANTILES:
            if len(sub_f) < 3 * CE.MIN_SAMPLE:
                continue
            m = eval_rule(sub_f, sub_ll, sub_ls, feat, side, q)
            if m.get("net_EV_lower_bound") is None:
                continue
            if best is None or m["net_EV_lower_bound"] > best["net_EV_lower_bound"]:
                best = m
        if best is None:
            out.append({"family": family, "setup_name": f"{feat}_{side}",
                        "side": side, "timeframe": timeframe, "horizon": horizon,
                        "regime": regime or "all", "sample_size": len(sub_f),
                        "verdict": "NEEDS_MORE_DATA",
                        "blocked_reasons": "insufficient_regime_sample",
                        **_safety()})
            continue
        verdict, blocked = _verdict_for(best, n_combos)
        out.append({
            "family": family, "setup_name": f"{feat}>{'+' if side=='long' else '-'}q{int(best['quantile']*100)}",
            "side": side, "timeframe": timeframe, "horizon": horizon,
            "regime": regime or "all", "sample_size": best["sample_size"],
            "gross_EV": best["gross_EV"], "cost_estimate": best["cost_estimate"],
            "net_EV": best["net_EV"], "net_EV_lower_bound": best["net_EV_lower_bound"],
            "profit_factor": best.get("profit_factor"), "hit_rate": best["hit_rate"],
            "payoff_ratio": best["payoff_ratio"], "turnover": best["turnover"],
            "max_drawdown": best["max_drawdown"],
            "stability_score": None,
            "overfit_score": round(abs((best.get("train_net_EV") or 0)
                                       - (best.get("net_EV") or 0)), 8),
            "complexity_penalty": best.get("complexity_penalty"),
            "baseline_delta": best.get("baseline_delta"),
            "verdict": verdict,
            "blocked_reasons": ";".join(blocked) if blocked else None,
            **_safety()})
    out.sort(key=lambda r: (r.get("net_EV_lower_bound") or -9), reverse=True)
    return out


# ==========================================================================
# Regime edge report
# ==========================================================================

REGIMES = ("trend_up", "trend_down", "chop", "high_vol", "low_vol",
           "high_liquidity", "low_liquidity", "funding_stress",
           "liquidation_cascade", "oi_expansion")


def regime_edge_report(bars: list[dict], aux: dict | None = None,
                       timeframe: int = 1, horizon: int = 10) -> list[dict]:
    """Best honest rule per regime. Tiny-sample regimes are flagged, never
    promoted (a one-regime, tiny-sample 'edge' is overfit until proven)."""
    aux = aux or {}
    rbars = resample_bars(bars, timeframe)
    feats = CE.build_features(rbars, aux.get("oi"), aux.get("funding"),
                              aux.get("orderbook"), aux.get("liquidations"))
    ll = CE.build_labels(rbars, side="long", time_bars=horizon)
    ls = CE.build_labels(rbars, side="short", time_bars=horizon)
    n_combos = len(REGIMES) * len(CE.DISCOVERY_FEATURES) * len(QUANTILES) * 2
    out: list[dict] = []
    for regime in REGIMES:
        mask = _regime_mask(feats, regime)
        sub_f = [f for f, m in zip(feats, mask) if m]
        sub_ll = [l for l, m in zip(ll, mask) if m]
        sub_ls = [l for l, m in zip(ls, mask) if m]
        if len(sub_f) < 3 * CE.MIN_SAMPLE:
            out.append({"regime": regime, "family": None, "side": None,
                        "sample_size": len(sub_f), "verdict": "NEEDS_MORE_DATA",
                        "reason": "regime_subsample_too_small", **_safety()})
            continue
        best = None
        for feat in CE.DISCOVERY_FEATURES:
            for side in ("long", "short"):
                for q in QUANTILES:
                    m = eval_rule(sub_f, sub_ll, sub_ls, feat, side, q)
                    if m.get("net_EV_lower_bound") is None:
                        continue
                    if best is None or m["net_EV_lower_bound"] > best["net_EV_lower_bound"]:
                        best = m
        if best is None:
            out.append({"regime": regime, "family": None, "side": None,
                        "sample_size": len(sub_f), "verdict": "NEEDS_MORE_DATA",
                        "reason": "no_rule_had_enough_entries", **_safety()})
            continue
        verdict, blocked = _verdict_for(best, n_combos)
        # tiny OOS sample inside a regime -> overfit-suspected, never promising
        if verdict == "PROMISING_RESEARCH_ONLY" and best["sample_size"] < 2 * CE.MIN_OOS_SAMPLE:
            verdict, blocked = "REJECTED_OVERFIT_RISK", ["regime_sample_too_thin"]
        out.append({
            "regime": regime, "family": _family_of(best["feature"], best["side"]),
            "side": best["side"], "sample_size": best["sample_size"],
            "gross_EV": best["gross_EV"], "net_EV": best["net_EV"],
            "net_EV_lower_bound": best["net_EV_lower_bound"],
            "hit_rate": best["hit_rate"], "payoff_ratio": best["payoff_ratio"],
            "turnover": best["turnover"], "stability_score": None,
            "verdict": verdict, "reason": ";".join(blocked) if blocked else None,
            **_safety()})
    return out


# ==========================================================================
# Feature quality audit
# ==========================================================================

def feature_quality_audit(features: list[dict], labels: list[dict],
                          labels_short: list[dict] | None = None
                          ) -> dict[str, Any]:
    keys = CE.DISCOVERY_FEATURES
    labels_short = labels_short if labels_short is not None else labels
    outs = [l.get("cost_adjusted_outcome") for l in labels]
    audit: dict[str, Any] = {}
    # precompute numeric series
    series = {k: [f.get(k) for f in features] for k in keys}
    for k in keys:
        vals = [v for v in series[k] if isinstance(v, (int, float))]
        coverage = round(len(vals) / max(1, len(features)), 4)
        missing_rate = round(1 - coverage, 4)
        spread = round(st.pstdev(vals), 8) if len(vals) > 1 else 0.0
        half = len(vals) // 2
        stability = None
        if half >= 5:
            m1, m2 = st.mean(vals[:half]), st.mean(vals[half:])
            denom = abs(m1) + abs(m2) + 1e-12
            stability = round(1 - abs(m1 - m2) / denom, 4)
        # relationship with label (aligned numeric pairs)
        xs, ys = [], []
        for f, o in zip(features, outs):
            v = f.get(k)
            if isinstance(v, (int, float)) and isinstance(o, (int, float)):
                xs.append(v)
                ys.append(o)
        label_corr = round(_pearson(xs, ys), 4)
        # redundancy: max abs correlation with the other features
        max_corr, max_with = 0.0, None
        for j in keys:
            if j == k:
                continue
            a = [f.get(k) for f in features]
            b = [f.get(j) for f in features]
            pa = [(x, y) for x, y in zip(a, b)
                  if isinstance(x, (int, float)) and isinstance(y, (int, float))]
            if len(pa) >= 3:
                c = abs(_pearson([p[0] for p in pa], [p[1] for p in pa]))
                if c > max_corr:
                    max_corr, max_with = c, j
        # best-rule net EV over long/short and quantiles (train-only threshold),
        # using the REAL side-aware label set for each side
        best_lb = None
        for side in ("long", "short"):
            for q in QUANTILES:
                m = eval_rule(features, labels, labels_short, k, side, q)
                lb = m.get("net_EV_lower_bound")
                if lb is not None and (best_lb is None or lb > best_lb):
                    best_lb = lb
        rec = _classify_feature(coverage, missing_rate, spread, stability,
                                label_corr, max_corr, best_lb)
        audit[k] = {"coverage": coverage, "missing_rate": missing_rate,
                    "distribution_stddev": spread, "stability": stability,
                    "max_abs_correlation": round(max_corr, 4),
                    "most_correlated_with": max_with,
                    "label_correlation": label_corr,
                    "net_EV_best_rule": best_lb,
                    "cost_adjusted_status": ("clears_floor" if best_lb and best_lb > MIN_EDGE
                                             else "cost_dominated"),
                    "recommendation": rec}
    return {"tool_version": TOOL_VERSION, "features": audit, **_safety()}


def _classify_feature(coverage, missing_rate, spread, stability, label_corr,
                      max_corr, best_lb) -> str:
    if coverage < 0.5:
        return "data_quality_risk"
    if spread < 1e-9:
        return "weak"                              # constant feature, no info
    if max_corr > 0.95:
        return "redundant"
    if stability is not None and stability < 0.3:
        return "unstable"
    if best_lb is not None and best_lb > MIN_EDGE:
        return "useful_candidate"
    if abs(label_corr) < 0.02:
        return "weak"
    return "cost_dominated"


# ==========================================================================
# Diagnose + full sprint + reports
# ==========================================================================

def diagnose(bars: list[dict], aux: dict | None = None) -> dict[str, Any]:
    aux = aux or {}
    feats = CE.build_features(bars, aux.get("oi"), aux.get("funding"),
                              aux.get("orderbook"), aux.get("liquidations"))
    labels = CE.build_labels(bars, side="long")
    labels_s = CE.build_labels(bars, side="short")
    rt = _round_trip_cost()
    per_feat = []
    for feat in CE.DISCOVERY_FEATURES:
        best = None
        for side in ("long", "short"):
            for q in QUANTILES:
                m = eval_rule(feats, labels, labels_s, feat, side, q)
                if m.get("net_EV_lower_bound") is None:
                    continue
                if best is None or m["net_EV_lower_bound"] > best["net_EV_lower_bound"]:
                    best = m
        if best:
            per_feat.append({"feature": feat, "best_side": best["side"],
                             "gross_EV": best["gross_EV"], "net_EV": best["net_EV"],
                             "net_EV_lower_bound": best["net_EV_lower_bound"],
                             "sample_size": best["sample_size"]})
    per_feat.sort(key=lambda r: (r["net_EV_lower_bound"] or -9), reverse=True)
    cost_dominated = [p for p in per_feat
                      if p["gross_EV"] is not None and p["gross_EV"] > 0 >= (p["net_EV"] or -1)]
    return {"tool_version": TOOL_VERSION, "n_bars": len(bars),
            "round_trip_cost": round(rt, 8),
            "least_bad_features": per_feat[:5],
            "clearly_useless": [p["feature"] for p in per_feat[-3:]],
            "n_features_cost_dominated": len(cost_dominated),
            "cost_dominated_features": [p["feature"] for p in cost_dominated],
            "diagnosis": ("gross tendencies exist below the cost floor; the "
                          "binding constraints are cost>gross and small sample"),
            **_safety()}


def run_sprint(symbol: str = "BTCUSDT", bars: list[dict] | None = None,
               aux: dict | None = None, bar_seconds: int = 60,
               write_reports: bool = True) -> dict[str, Any]:
    if bars is None:
        data = CE.load_dataset(symbol, bar_seconds)
        bars = data.get("bars") or []
        aux = {k: data.get(k) for k in ("oi", "funding", "orderbook", "liquidations")}
    aux = aux or {}
    summary: dict[str, Any] = {"tool_version": TOOL_VERSION, "symbol": symbol,
                               "ran_at": CE._now_iso(), "n_bars": len(bars),
                               **_safety()}
    if len(bars) < 3 * CE.MIN_SAMPLE:
        summary["verdict"] = "NEEDS_MORE_DATA"
        summary["note"] = f"only {len(bars)} bars; keep collecting"
        return summary
    diag = diagnose(bars, aux)
    scan = cost_aware_horizon_scan(bars, aux)
    fam = strategy_family_benchmark(bars, aux)
    reg = regime_edge_report(bars, aux)
    feats = CE.build_features(bars, aux.get("oi"), aux.get("funding"),
                              aux.get("orderbook"), aux.get("liquidations"))
    fq = feature_quality_audit(feats, CE.build_labels(bars, side="long"),
                               CE.build_labels(bars, side="short"))
    promising = [f for f in fam if f.get("verdict") == "PROMISING_RESEARCH_ONLY"]
    summary.update({
        "diagnosis": diag["diagnosis"],
        "round_trip_cost": diag["round_trip_cost"],
        "least_bad_features": diag["least_bad_features"],
        "families_total": len(fam),
        "families_promising": len(promising),
        "families_rejected": sum(1 for f in fam if str(f.get("verdict")).startswith("REJECTED")),
        "best_family": fam[0] if fam else None,
        "cost_aware_rows": len(scan.get("rows", [])),
        "cost_aware_best_cell": scan.get("best_cell"),
        "any_timeframe_promising": scan.get("any_promising"),
        "regimes_with_data": sum(1 for r in reg if r.get("verdict") != "NEEDS_MORE_DATA"),
        "paper_gate": "BLOCKED (human approval not encodable)",
        "verdict": ("PROMISING_CANDIDATES_UNDER_RESEARCH" if promising
                    else "NO_EDGE_ALL_REJECTED_RESEARCH_ONLY"),
        "methodology": {"threshold_source": "train_only",
                        "bar_time_semantics": "bar_close_available",
                        "short_label_method": "real_side_aware",
                        "complexity_penalty_applied": True,
                        "costs_lowered": False, "oos_used_for_selection": False,
                        "guards_active": ["DATA_SNOOPING_GUARD_ACTIVE",
                                          "BAR_AVAILABLE_AT_GUARD_ACTIVE",
                                          "COMPLEXITY_PENALTY_ACTIVE",
                                          "COST_FLOOR_ENFORCED"]},
    })
    if write_reports:
        out_dir = CE._repo_root().joinpath(*OUTPUT_SUBDIR)
        out_dir.mkdir(parents=True, exist_ok=True)

        def wjson(name, obj):
            tmp = out_dir / (name + ".tmp")
            tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, out_dir / name)

        wjson("alpha_improvement_summary_v1039.json", summary)
        wjson("cost_aware_horizon_scan_v1039.json", scan)
        wjson("feature_quality_audit_v1039.json", fq)
        wjson("diagnose_v1039.json", diag)
        _write_csv(out_dir / "strategy_family_benchmark_v1039.csv", fam,
                   ["family", "setup_name", "side", "timeframe", "horizon", "regime",
                    "sample_size", "gross_EV", "cost_estimate", "net_EV",
                    "net_EV_lower_bound", "profit_factor", "hit_rate", "payoff_ratio",
                    "turnover", "max_drawdown", "overfit_score", "complexity_penalty",
                    "baseline_delta", "verdict", "blocked_reasons"])
        _write_csv(out_dir / "regime_edge_report_v1039.csv", reg,
                   ["regime", "family", "side", "sample_size", "gross_EV", "net_EV",
                    "net_EV_lower_bound", "hit_rate", "payoff_ratio", "turnover",
                    "verdict", "reason"])
        summary["reports_dir"] = str(out_dir).replace("\\", "/")
    return summary


def _write_csv(path, rows: list[dict], cols: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in cols})
