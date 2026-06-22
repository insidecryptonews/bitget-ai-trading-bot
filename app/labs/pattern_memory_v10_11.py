"""ResearchOps V10.11 — Pattern Memory & Similarity Decision Gate (shadow only).

Before the bot accepts a SHADOW trade it looks back: have I seen setups like
this before, how many times, what happened next, did they close green, what was
the net EV/PF/drawdown, do they survive cost stress, do they hold across several
windows and symbols, or is it a single-coin / single-window fluke?

This module builds an offline pattern memory from the V10.10 micro-scalp shadow
trades (each tagged with a CAUSAL feature vector computed at the signal bar),
then answers similarity queries and runs a fail-closed shadow gate. It is pure /
offline / deterministic: no orders, no leverage, no money, no DB, no .env, no
exchange. NOTHING flips paper_ready/live_ready or approves a candidate.

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
from . import micro_scalp_shadow_v10_10 as micro

TOOL_VERSION = "v10.11"
OUTPUT_ROOT = "reports/research/v10_11"
DAY_MS = 86_400_000

# numeric features used for the similarity distance (all causal)
_NUM_FEATURES = ("recent_return", "body_range_ratio", "atr_pct", "volume_proxy",
                 "breakout_distance", "pullback_distance", "setup_quality")
# categorical features that earn a match bonus
_CAT_FEATURES = ("vol_regime", "trend_regime", "funding_regime")

# shadow-gate thresholds (research heuristics)
MIN_SIMILAR_CASES = 30
MIN_NET_PF = 1.2
MIN_CLOSED_GREEN_RATE = 0.5
MAX_AVG_LOSS = 0.02          # fraction
MAX_DRAWDOWN_PROXY = 0.5
MIN_WINDOWS = 2
MONTH_MS = 30 * DAY_MS

_FORBIDDEN_SEG = ("raw", "backup", "backups", "vault", "vaults", "training_exports",
                  "secret", "secrets", "credential", "credentials")
_FORBIDDEN_SUF = (".env", ".db", ".sqlite", ".zip", ".tar", ".gz", ".pem", ".key")

# decision codes
PASS = "PASS_SHADOW_GATE"
F_FEW = "FAIL_INSUFFICIENT_SIMILAR_CASES"
F_EV = "FAIL_NEGATIVE_EV"
F_PF = "FAIL_PROFIT_FACTOR"
F_GREEN = "FAIL_CLOSED_GREEN_RATE"
F_COST = "FAIL_COST_STRESS"
F_WIN = "FAIL_WINDOW_CONCENTRATION"
F_SYM = "FAIL_SYMBOL_CONCENTRATION"
F_DD = "FAIL_DRAWDOWN"
F_FD = "FAIL_FALSE_DISCOVERY_RISK"
F_AVGLOSS = "FAIL_AVG_LOSS_TOO_HIGH"


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "candidates_are_hypotheses_not_signals": True,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


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
# 3. Causal feature vector for a setup (computed at the signal bar i)
# --------------------------------------------------------------------------

def feature_vector(bars, atr_list, emaf, emas, i, *, entry, costs) -> dict[str, Any]:
    b = bars[i]
    price = b["close"] or 1.0
    rng = (b["high"] - b["low"]) or 1e-9
    atr = atr_list[i] if atr_list[i] is not None else (entry.get("atr") or 1.0)
    atr_prior = [x for x in atr_list[max(0, i - 50):i] if x is not None]
    atr_med = micro._median(atr_prior) if atr_prior else atr  # type: ignore[attr-defined]
    recent_return = ((b["close"] - bars[i - 5]["close"]) / bars[i - 5]["close"]
                     if i >= 5 and bars[i - 5]["close"] else 0.0)
    vols = [bars[k]["volume"] for k in range(max(0, i - 10), i)]
    avg_vol = (sum(vols) / len(vols)) if vols else 0.0
    win_hi = max(b2["high"] for b2 in bars[max(0, i - 10):i]) if i >= 1 else b["high"]
    win_lo = min(b2["low"] for b2 in bars[max(0, i - 10):i]) if i >= 1 else b["low"]
    ef, es = emaf[i], emas[i]
    is_long = entry["side"] == "LONG"
    breakout = ((b["close"] - win_hi) if is_long else (win_lo - b["close"])) / atr
    pullback = ((b["close"] - ef) / atr) if ef is not None else 0.0
    fr = entry.get("funding_snapshot")
    if fr is None:
        funding_regime = "none"
    elif fr >= 0.0005:
        funding_regime = "funding_extreme_positive"
    elif fr <= -0.0005:
        funding_regime = "funding_extreme_negative"
    elif fr > 0:
        funding_regime = "funding_positive"
    elif fr < 0:
        funding_regime = "funding_negative"
    else:
        funding_regime = "flat"
    return {
        "symbol": entry["symbol"], "timeframe": entry["timeframe"], "side": entry["side"],
        "strategy_family": entry["strategy_family"], "entry_reason": entry["entry_reason"],
        "recent_return": round(recent_return, 6),
        "body_range_ratio": round(abs(b["close"] - b["open"]) / rng, 4),
        "atr_pct": round(atr / price, 6),
        "volume_proxy": round((b["volume"] / avg_vol) if avg_vol > 0 else 1.0, 4),
        "breakout_distance": round(breakout, 4), "pullback_distance": round(pullback, 4),
        "cost_proxy": round(costs.round_trip_fraction(), 6),
        "setup_quality": entry.get("setup_quality_score", 0.5),
        "vol_regime": ("high_volatility" if atr > 1.3 * atr_med else "low_volatility"),
        "trend_regime": micro._regime(ef, es, b["close"]),  # type: ignore[attr-defined]
        "funding_regime": funding_regime,
        "gap_risk": round(abs(b["open"] - bars[i - 1]["close"]) / atr, 4) if i >= 1 else 0.0,
        "liquidation_distance_estimate": None,  # research proxy; not used live
        "no_lookahead": True}


# --------------------------------------------------------------------------
# 4. Build the offline pattern memory from V10.10 shadow trades
# --------------------------------------------------------------------------

def resolve_micro_exit_policies(raw) -> tuple[list[str], str, list[str]]:
    """V10.11.1 — never silently build an empty memory. Returns
    (policies, source, warnings). If the caller passed no MICRO policies (e.g.
    the global V10.8 trailing default, or empty/garbage), fall back to the micro
    defaults; if it mixed micro + non-micro, keep the micro ones and warn."""
    raw = list(raw or [])
    micro_valid = [p for p in raw if p in micro.EXIT_POLICIES]
    non_micro = [p for p in raw if p not in micro.EXIT_POLICIES]
    warnings: list[str] = []
    if not micro_valid:
        policies, source = list(micro.EXIT_POLICIES), "micro_defaults"
        if non_micro:
            warnings.append("invalid_or_non_micro_exit_policies_ignored")
    else:
        policies, source = micro_valid, "user_micro_policies"
        if non_micro:
            warnings.append("invalid_or_non_micro_exit_policies_ignored")
    return policies, source, warnings


def build_pattern_memory(*, sample_dir, symbols, timeframes, sides,
                         strategy_families, exit_policies=None, cost_bps=6.0,
                         slippage_bps=4.0, spread_bps=2.0, latency_bars=1,
                         funding_mode=True, gap_policy="adverse_open",
                         max_entries_per_combo=60) -> dict[str, Any]:
    costs = micro.MicroCosts(cost_bps, slippage_bps, spread_bps, funding_mode, latency_bars)
    exit_policies, ep_source, ep_warnings = resolve_micro_exit_policies(exit_policies)
    families = [f for f in strategy_families if f in micro.STRATEGY_FAMILIES]
    sides = [s.upper() for s in sides if s.upper() in micro.SIDES]
    timeframes = [t.lower() for t in timeframes]
    report: dict[str, Any] = {
        "tool_version": TOOL_VERSION, "generated_at": _now_iso(), "sample_dir": sample_dir,
        "symbols": symbols, "timeframes": timeframes, "sides": sides,
        "strategy_families": families, "exit_policies": exit_policies,
        "exit_policies_source": ep_source, "exit_policies_used": exit_policies,
        "build_status": "EMPTY_MEMORY",
        "suggested_command": (
            "python -m app.research_lab pattern-memory-build-v1011 --sample-dir "
            "<sample> --symbols BTCUSDT,ETHUSDT --timeframes 4h,6h --sides LONG,SHORT "
            "--strategy-families micro_breakout,micro_reversal"),
        "orderbook_real": False, "missing_oi_historical": True,
        "missing_liquidations": True, "cases": [], "errors": [],
        "warnings": list(ep_warnings), **_safety()}
    if not (isinstance(sample_dir, str) and os.path.isdir(sample_dir)):
        report["errors"].append("sample_dir_not_found")
        return report
    if not (families and sides and timeframes and symbols):
        report["errors"].append("nothing_to_build")
        return report

    cases: list[dict[str, Any]] = []
    min_ts_global = None
    for sym in symbols:
        fp = os.path.join(sample_dir, f"{sym}_funding.csv")
        funding = lab.load_funding(fp) if os.path.isfile(fp) else []
        for tf in timeframes:
            op = os.path.join(sample_dir, f"{sym}_{tf}_ohlcv.csv")
            if not os.path.isfile(op):
                continue
            bars = lab.load_ohlcv(op)
            if len(bars) < micro._WARMUP + 5:  # type: ignore[attr-defined]
                continue
            atr_list = lab.atr_series(bars)
            emaf = lab.ema_series(bars, 12)
            emas = lab.ema_series(bars, 26)
            if min_ts_global is None or bars[0]["ts"] < min_ts_global:
                min_ts_global = bars[0]["ts"]
            for side in sides:
                for fam in families:
                    ents = micro.generate_micro_entries(
                        symbol=sym, timeframe=tf, side=side, family=fam,
                        bars=bars, funding=funding, max_entries=max_entries_per_combo)
                    for e in ents:
                        fv = feature_vector(bars, atr_list, emaf, emas, e["signal_idx"],
                                            entry=e, costs=costs)
                        for pol in exit_policies:
                            params = micro._param_sets(pol)[0]  # type: ignore[attr-defined]
                            tr = micro.simulate_micro_trade(
                                bars, atr_list, e, policy=pol, params=params,
                                costs=costs, funding=funding, gap_policy=gap_policy)
                            if tr is None:
                                continue
                            cases.append({
                                "pattern_id": hashlib.sha256(
                                    f"{sym}{tf}{side}{fam}{pol}{e['entry_ts']}".encode()).hexdigest()[:16],
                                "features": fv, "entry_ts": e["entry_ts"], "symbol": sym,
                                "timeframe": tf, "side": side, "strategy_family": fam,
                                "exit_policy": pol,
                                "params_hash": hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:10],
                                "net_result": tr["net_pnl"], "gross_result": tr["gross_pnl"],
                                "fees": tr["fee"], "slippage": tr["slippage"], "funding": tr["funding"],
                                "closed_green": tr["closed_green"], "went_green": tr["went_green"],
                                "green_to_red_failure": tr["green_to_red_failure"],
                                "MFE": tr["mfe"], "MAE": tr["mae"], "sl_pct": tr["sl_pct"],
                                "regime_tags": [fv["vol_regime"], fv["trend_regime"], fv["funding_regime"]],
                                "final_outcome": "green" if tr["closed_green"] else "red"})
    # tag month buckets (windows) relative to global start
    base_ts = min_ts_global or 0
    for c in cases:
        c["month_bucket"] = int((c["entry_ts"] - base_ts) // MONTH_MS)
    report["cases"] = cases
    report["n_cases"] = len(cases)
    report["cost_model"] = costs.as_dict()
    if cases:
        report["build_status"] = "MEMORY_BUILT"
    else:
        report["build_status"] = "EMPTY_MEMORY"
        if "pattern_memory_empty" not in report["warnings"]:
            report["warnings"].append("pattern_memory_empty")
    return report


# --------------------------------------------------------------------------
# 5. Similarity search
# --------------------------------------------------------------------------

def _numeric_stats(cases) -> dict[str, float]:
    stats = {}
    for k in _NUM_FEATURES:
        vals = [_f(c["features"].get(k)) for c in cases]
        vals = [v for v in vals if v is not None]
        if len(vals) > 1:
            mean = sum(vals) / len(vals)
            var = sum((v - mean) ** 2 for v in vals) / len(vals)
            stats[k] = math.sqrt(var) or 1.0
        else:
            stats[k] = 1.0
    return stats


def _distance(qf, cf, stds, *, match_regimes) -> float:
    d = 0.0
    for k in _NUM_FEATURES:
        qv, cv = _f(qf.get(k)), _f(cf.get(k))
        if qv is None or cv is None:
            continue
        d += ((qv - cv) / (stds.get(k) or 1.0)) ** 2
    dist = math.sqrt(d)
    if match_regimes:
        for k in _CAT_FEATURES:
            if qf.get(k) is not None and qf.get(k) == cf.get(k):
                dist *= 0.9   # small bonus (closer) for matching regime
    return dist


def query_similar(memory_cases, query_features, *, min_similar=MIN_SIMILAR_CASES,
                  same_symbol=False, match_regimes=True, top_k=0,
                  cost_bps=6.0, slippage_bps=4.0, spread_bps=2.0,
                  false_discovery_risk="LOW") -> dict[str, Any]:
    costs = micro.MicroCosts(cost_bps, slippage_bps, spread_bps)
    qf = query_features
    # exact filters: side, timeframe, strategy_family (always)
    pool = [c for c in memory_cases
            if c["side"] == qf.get("side") and c["timeframe"] == qf.get("timeframe")
            and c["strategy_family"] == qf.get("strategy_family")]
    if same_symbol:
        pool = [c for c in pool if c["symbol"] == qf.get("symbol")]
    stds = _numeric_stats(pool) if pool else {}
    scored = sorted(pool, key=lambda c: _distance(qf, c["features"], stds, match_regimes=match_regimes))
    similar = scored[:top_k] if top_k and top_k > 0 else scored
    n = len(similar)
    out: dict[str, Any] = {"query": {k: qf.get(k) for k in ("symbol", "timeframe", "side", "strategy_family")},
                           "similar_cases_count": n, **_safety()}
    if n == 0:
        out.update({"decision": F_FEW, "win_rate": 0.0, "closed_green_rate": 0.0})
        return out
    nets = [c["net_result"] for c in similar]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gains, loss_sum = sum(wins), -sum(losses)
    net_ev = sum(nets) / n
    eq = peak = max_dd = 0.0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    rt = costs.round_trip_fraction()
    cs2 = sum(x - rt for x in nets) / n
    cs3 = sum(x - 2 * rt for x in nets) / n
    windows = {c.get("month_bucket") for c in similar}
    syms = {c["symbol"] for c in similar}
    out.update({
        "win_rate": round(len(wins) / n, 4),
        "closed_green_rate": round(sum(1 for c in similar if c["closed_green"]) / n, 4),
        "green_to_red_rate": round(sum(1 for c in similar if c["green_to_red_failure"]) / n, 4),
        "net_EV": round(net_ev, 6),
        "profit_factor": round((gains / loss_sum) if loss_sum > 0 else (math.inf if gains > 0 else 0.0), 4),
        "avg_win": round(sum(wins) / len(wins), 6) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 6) if losses else 0.0,
        "max_drawdown_proxy": round(max_dd, 6),
        "cost_stress_x2": round(cs2, 6), "cost_stress_x3": round(cs3, 6),
        "windows_covered": len(windows), "symbols_covered": len(syms),
        "concentration_warnings": (
            (["SYMBOL_ONLY"] if len(syms) == 1 else []) +
            (["SINGLE_WINDOW"] if len(windows) < MIN_WINDOWS else [])),
        "false_discovery_risk": false_discovery_risk,
    })
    out["decision"] = _decide(out)
    return out


def _decide(q) -> str:
    if q["similar_cases_count"] < MIN_SIMILAR_CASES:
        return F_FEW
    if q.get("net_EV", 0) <= 0:
        return F_EV
    pf = q.get("profit_factor", 0)
    if not isinstance(pf, str) and pf < MIN_NET_PF:
        return F_PF
    if q.get("closed_green_rate", 0) < MIN_CLOSED_GREEN_RATE:
        return F_GREEN
    if abs(q.get("avg_loss", 0)) > MAX_AVG_LOSS:
        return F_AVGLOSS
    if q.get("cost_stress_x2", 0) <= 0 or q.get("cost_stress_x3", 0) <= 0:
        return F_COST
    if q.get("windows_covered", 0) < MIN_WINDOWS:
        return F_WIN
    if q.get("symbols_covered", 0) < 2:
        return F_SYM
    if q.get("max_drawdown_proxy", 0) >= MAX_DRAWDOWN_PROXY:
        return F_DD
    if q.get("false_discovery_risk") == "HIGH":
        return F_FD
    return PASS


# --------------------------------------------------------------------------
# 6/7. Shadow gate over candidate setups (one query per tf/side/strategy)
# --------------------------------------------------------------------------

def shadow_gate(memory, *, min_similar=MIN_SIMILAR_CASES, same_symbol=False,
                match_regimes=True) -> dict[str, Any]:
    cases = memory.get("cases", [])
    report: dict[str, Any] = {
        "tool_version": TOOL_VERSION, "sample_dir": memory.get("sample_dir"),
        "n_cases": len(cases), "orderbook_real": False,
        "missing_oi_historical": True, "missing_liquidations": True,
        "queries": [], "decisions": {}, "passed_queries": [], **_safety()}
    if not cases:
        report["errors"] = ["empty_pattern_memory"]
        return report
    # group candidate setups by (timeframe, side, strategy_family); use the most
    # recent case in each group as the representative "current setup" query.
    groups: dict[tuple, dict[str, Any]] = {}
    for c in cases:
        key = (c["timeframe"], c["side"], c["strategy_family"])
        if key not in groups or c["entry_ts"] > groups[key]["entry_ts"]:
            groups[key] = c
    cost = memory.get("cost_model", {})
    n_queries = len(groups)
    passed = []
    for key, rep_case in sorted(groups.items()):
        qf = dict(rep_case["features"])
        q = query_similar(cases, qf, min_similar=min_similar, same_symbol=same_symbol,
                          match_regimes=match_regimes, cost_bps=cost.get("cost_bps", 6.0),
                          slippage_bps=cost.get("slippage_bps", 4.0),
                          spread_bps=cost.get("spread_bps", 2.0),
                          false_discovery_risk="LOW")
        q["group"] = f"{key[0]}/{key[1]}/{key[2]}"
        report["queries"].append(q)
        report["decisions"][q["group"]] = q["decision"]
        if q["decision"] == PASS:
            passed.append(q["group"])
    # false-discovery overlay: many groups, few passes => HIGH; re-decide passes
    fd = "HIGH" if (n_queries >= 10 and len(passed) / max(1, n_queries) < 0.1) else (
        "MODERATE" if n_queries >= 10 else "LOW")
    report["false_discovery_risk"] = fd
    if fd == "HIGH":
        for q in report["queries"]:
            if q["decision"] == PASS:
                q["false_discovery_risk"] = "HIGH"
                q["decision"] = F_FD
                report["decisions"][q["group"]] = F_FD
        passed = [g for g in passed if report["decisions"][g] == PASS]
    report["passed_queries"] = passed
    report["n_queries"] = n_queries
    report["n_passed"] = len(passed)
    report["n_failed"] = n_queries - len(passed)
    sides_passed = {g.split("/")[1] for g in passed}
    report["side_concentration_warning"] = f"{sides_passed.pop()}_ONLY" if len(sides_passed) == 1 else ""
    # ranking
    ranked = sorted(report["queries"], key=lambda q: (q["decision"] == PASS, q.get("net_EV") or -9), reverse=True)
    report["pattern_candidate_ranking"] = ranked
    report["rejected_patterns"] = [q for q in report["queries"] if q["decision"] != PASS]
    return report


# --------------------------------------------------------------------------
# 8. Reports
# --------------------------------------------------------------------------

def _write_csv(path, rows, header):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})


def write_pattern_reports(memory, gate, output_dir=None) -> str:
    base = _safe_output_base(output_dir)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = os.path.join(base, run_id)
    os.makedirs(run_dir, exist_ok=True)
    # pattern_cases.csv
    case_rows = [{"pattern_id": c["pattern_id"], "symbol": c["symbol"],
                  "timeframe": c["timeframe"], "side": c["side"],
                  "strategy_family": c["strategy_family"], "exit_policy": c["exit_policy"],
                  "entry_ts": c["entry_ts"], "net_result": c["net_result"],
                  "closed_green": c["closed_green"], "went_green": c["went_green"],
                  "green_to_red_failure": c["green_to_red_failure"],
                  "month_bucket": c.get("month_bucket")} for c in memory.get("cases", [])]
    _write_csv(os.path.join(run_dir, "pattern_cases.csv"), case_rows,
               ["pattern_id", "symbol", "timeframe", "side", "strategy_family", "exit_policy",
                "entry_ts", "net_result", "closed_green", "went_green",
                "green_to_red_failure", "month_bucket"])
    qcols = ["group", "similar_cases_count", "win_rate", "closed_green_rate",
             "green_to_red_rate", "net_EV", "profit_factor", "avg_loss",
             "cost_stress_x2", "cost_stress_x3", "windows_covered", "symbols_covered",
             "decision"]
    _write_csv(os.path.join(run_dir, "similarity_queries.csv"), gate.get("queries", []), qcols)
    _write_csv(os.path.join(run_dir, "shadow_gate_decisions.csv"),
               [{"group": g, "decision": d} for g, d in gate.get("decisions", {}).items()],
               ["group", "decision"])
    _write_csv(os.path.join(run_dir, "pattern_candidate_ranking.csv"),
               gate.get("pattern_candidate_ranking", []), qcols)
    _write_csv(os.path.join(run_dir, "rejected_patterns.csv"), gate.get("rejected_patterns", []), qcols)
    summary = {"tool_version": TOOL_VERSION, "generated_at": _now_iso(),
               "sample_dir": memory.get("sample_dir"), "n_cases": memory.get("n_cases"),
               "n_queries": gate.get("n_queries"), "n_passed": gate.get("n_passed"),
               "n_failed": gate.get("n_failed"), "passed_queries": gate.get("passed_queries"),
               "false_discovery_risk": gate.get("false_discovery_risk"),
               "side_concentration_warning": gate.get("side_concentration_warning"),
               "decisions": gate.get("decisions"), **_safety()}
    with open(os.path.join(run_dir, "pattern_memory_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    _write_md(os.path.join(run_dir, "report.md"), memory, gate)
    return run_dir.replace("\\", "/")


def _write_md(path, memory, gate):
    lines = ["# ResearchOps V10.11 - Pattern Memory & Similarity Shadow Gate (SHADOW ONLY)",
             "",
             "> Candidates are HYPOTHESES, not signals. Shadow simulation, not a "
             "tradable portfolio. Edge NOT validated. NO LIVE.",
             "", f"- sample_dir: {memory.get('sample_dir')}",
             f"- pattern cases: {memory.get('n_cases')}",
             f"- queries: {gate.get('n_queries')} | passed_shadow_gate: {gate.get('n_passed')} | failed: {gate.get('n_failed')}",
             f"- false_discovery_risk: {gate.get('false_discovery_risk')}",
             f"- side_concentration_warning: {gate.get('side_concentration_warning')!r}",
             f"- orderbook_real: {memory.get('orderbook_real')} | missing_oi_historical: true | missing_liquidations: true",
             "", "## Shadow-gate decisions (hypotheses, not signals)"]
    for q in gate.get("pattern_candidate_ranking", [])[:20]:
        lines.append(f"- {q.get('group')}: {q['decision']} "
                     f"(cases={q.get('similar_cases_count')} net_EV={q.get('net_EV')} "
                     f"closed_green={q.get('closed_green_rate')} windows={q.get('windows_covered')} symbols={q.get('symbols_covered')})")
    lines += ["", "## Safety", "- research_only: true", "- shadow_only: true",
              "- edge_validated: false", "- paper_ready: false", "- live_ready: false",
              "- can_send_real_orders: false", "- approved_for_paper: false",
              "- approved_for_live: false", "- FINAL_RECOMMENDATION: NO LIVE", ""]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# --------------------------------------------------------------------------
# Plan + latest summary
# --------------------------------------------------------------------------

def pattern_memory_plan() -> dict[str, Any]:
    return {"tool_version": TOOL_VERSION,
            "objective": ("before accepting a SHADOW trade, look back at similar "
                          "historical setups and decide whether they actually worked "
                          "- net EV, closed-green, PF, drawdown, cost stress, across "
                          "windows and symbols - WITHOUT real trading"),
            "feature_vector": list(_NUM_FEATURES) + list(_CAT_FEATURES) + ["symbol", "timeframe", "side", "strategy_family", "gap_risk"],
            "gate_conditions": [f"similar_cases>={MIN_SIMILAR_CASES}", "net_EV>0",
                                f"profit_factor>={MIN_NET_PF}", f"closed_green_rate>={MIN_CLOSED_GREEN_RATE}",
                                "avg_loss controlled", "cost stress x2/x3 survive",
                                f">={MIN_WINDOWS} windows", ">=2 symbols",
                                "drawdown acceptable", "false_discovery not HIGH"],
            "decision_codes": [PASS, F_FEW, F_EV, F_PF, F_GREEN, F_AVGLOSS, F_COST,
                               F_WIN, F_SYM, F_DD, F_FD],
            "never": ["APPROVED_FOR_PAPER", "APPROVED_FOR_LIVE"],
            "orderbook_real": False, **_safety()}


def latest_pattern_summary(output_dir=None):
    base = output_dir or OUTPUT_ROOT
    try:
        if not os.path.isdir(base):
            return None
        runs = sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))
        for rid in reversed(runs):
            sp = os.path.join(base, rid, "pattern_memory_summary.json")
            if os.path.isfile(sp):
                with open(sp, "r", encoding="utf-8") as fh:
                    return json.load(fh)
    except Exception:
        return None
    return None
