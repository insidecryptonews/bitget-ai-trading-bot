"""ResearchOps V10.12 - Quality-Gated Intelligent Shadow Scalper (shadow only).

The bot stops simulating thousands of weak trades and starts behaving like a
decision machine: detect a raw setup, run a QUALITY PRE-GATE (cost-vs-target,
volatility, range, spread proxy, recent move, dead-bar/noise, duplicate, stop
size), then consult the V10.11 pattern memory (have similar past setups had
positive net EV, did they close green, are they concentrated in one coin/window,
is false-discovery risk high?). Only setups that clear BOTH gates become shadow
trades, simulated with the V10.10 no-lookahead engine and journalled.

This is pure / offline / deterministic. No orders, no leverage, no money, no
DB, no .env, no exchange, no private endpoints. NOTHING flips
paper_ready/live_ready/can_send_real_orders and a strategy is NEVER approved for
paper or live - paper_candidate_future is always False for now.

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
from . import pattern_memory_v10_11 as pmem

TOOL_VERSION = "v10.12"
OUTPUT_ROOT = "reports/research/v10_12"
JOURNAL_ROOT = "reports/research/v10_12/shadow_forward_journal"
DAY_MS = 86_400_000

MODES = ("offline-replay", "latest-snapshot", "forward-shadow")
SCALPING_TIMEFRAMES = ("1m", "3m", "5m")

# quality-gate decision codes
Q_PASS = "PASS_QUALITY_SHADOW"
Q_COST = "FAIL_COST_TOO_HIGH"
Q_VOL = "FAIL_VOL_TOO_LOW"
Q_RANGE = "FAIL_RANGE_TOO_LOW"
Q_PATTERN = "FAIL_PATTERN_MEMORY"
Q_EV = "FAIL_NEGATIVE_EV"
Q_SPREAD = "FAIL_SPREAD_TOO_HIGH"
Q_NOSIM = "FAIL_NO_SIMILAR_CASES"
Q_RISK = "FAIL_RISK_TOO_HIGH"
Q_DUP = "FAIL_DUPLICATE_SIGNAL"
Q_FD = "FAIL_FALSE_DISCOVERY_RISK"

_FORBIDDEN_SEG = ("raw", "backup", "backups", "vault", "vaults", "training_exports",
                  "secret", "secrets", "credential", "credentials")
_FORBIDDEN_SUF = (".env", ".db", ".sqlite", ".zip", ".tar", ".gz", ".pem", ".key")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "paper_candidate_future": False,
            "candidates_are_hypotheses_not_signals": True,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
# Quality thresholds (research heuristics; all tunable from the CLI)
# --------------------------------------------------------------------------

class QualityThresholds:
    def __init__(self, *, min_quality_score=0.55, min_atr_pct=0.0025,
                 min_range_ratio=0.5, max_spread_ratio=0.35, max_sl_pct=0.02,
                 max_cost_to_target=0.6, dup_min_gap=3, min_similar_cases=30,
                 min_net_ev=0.0, min_profit_factor=1.2, min_closed_green_rate=0.5,
                 vol_ref=0.006, range_ref=1.0, move_ref=0.01):
        self.min_quality_score = float(min_quality_score)
        self.min_atr_pct = float(min_atr_pct)
        self.min_range_ratio = float(min_range_ratio)
        self.max_spread_ratio = float(max_spread_ratio)
        self.max_sl_pct = float(max_sl_pct)
        self.max_cost_to_target = float(max_cost_to_target)
        self.dup_min_gap = int(dup_min_gap)
        self.min_similar_cases = int(min_similar_cases)
        self.min_net_ev = float(min_net_ev)
        self.min_profit_factor = float(min_profit_factor)
        self.min_closed_green_rate = float(min_closed_green_rate)
        self.vol_ref = float(vol_ref)
        self.range_ref = float(range_ref)
        self.move_ref = float(move_ref)

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in (
            "min_quality_score", "min_atr_pct", "min_range_ratio", "max_spread_ratio",
            "max_sl_pct", "max_cost_to_target", "dup_min_gap", "min_similar_cases",
            "min_net_ev", "min_profit_factor", "min_closed_green_rate",
            "vol_ref", "range_ref", "move_ref")}


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


# --------------------------------------------------------------------------
# 1. No-lookahead pattern memory: a candidate may only see STRICTLY-PAST cases.
# --------------------------------------------------------------------------

def prefix_memory_cases_for_candidate(memory_cases, candidate_entry_ts,
                                      candidate_pattern_id=None):
    """Return only the pattern-memory cases that a decision at
    ``candidate_entry_ts`` could legitimately have observed: strictly earlier
    entries. Same-timestamp cases and the candidate's own case are excluded.

    If ``candidate_entry_ts`` is missing we FAIL CLOSED: no memory is used (we
    never fall back to the full sample, which would leak the future).

    Returns (past_cases, meta) where meta carries the audit trail."""
    total = len(memory_cases or [])
    if candidate_entry_ts is None:
        return [], {"memory_cases_total": total, "memory_cases_prefix_used": 0,
                    "memory_cases_future_excluded": 0, "same_timestamp_excluded": 0,
                    "self_excluded": 0, "candidate_entry_ts": None,
                    "no_lookahead_status": "FAIL_MISSING_CANDIDATE_ENTRY_TS",
                    "warnings": ["missing_candidate_entry_ts_no_memory_used"]}
    past, future_excl, same_excl, self_excl = [], 0, 0, 0
    for c in (memory_cases or []):
        ts = c.get("entry_ts")
        if ts is None:
            continue
        if candidate_pattern_id is not None and c.get("pattern_id") == candidate_pattern_id:
            self_excl += 1
            continue
        if ts == candidate_entry_ts:
            same_excl += 1
            continue
        if ts > candidate_entry_ts:
            future_excl += 1
            continue
        past.append(c)
    return past, {"memory_cases_total": total, "memory_cases_prefix_used": len(past),
                  "memory_cases_future_excluded": future_excl,
                  "same_timestamp_excluded": same_excl, "self_excluded": self_excl,
                  "candidate_entry_ts": candidate_entry_ts,
                  "no_lookahead_status": "OK_PREFIX_ONLY", "warnings": []}


# --------------------------------------------------------------------------
# 2. Quality Pre-Gate (per setup). Fail-closed, ordered priority.
# --------------------------------------------------------------------------

def quality_pre_gate(*, side, timeframe, strategy_family, signal_idx,
                     tp_pct, sl_pct, atr_pct, range_to_atr, recent_return,
                     round_trip_fraction, spread_fraction, setup_quality,
                     features=None, memory_cases=None, candidate_entry_ts=None,
                     candidate_pattern_id=None,
                     false_discovery_risk="LOW", dup_last_idx=None,
                     thresholds=None, cost_bps=6.0, slippage_bps=4.0,
                     spread_bps=2.0) -> dict[str, Any]:
    th = thresholds or QualityThresholds()
    reasons: list[str] = []
    tp_pct = float(tp_pct)
    cost_to_target = (round_trip_fraction / tp_pct) if tp_pct > 0 else math.inf
    expected_move_vs_cost = (tp_pct / round_trip_fraction) if round_trip_fraction > 0 else math.inf

    # ranking score (does not by itself approve anything)
    vol_score = _clamp01(atr_pct / th.vol_ref) if th.vol_ref > 0 else 0.0
    range_score = _clamp01(range_to_atr / th.range_ref) if th.range_ref > 0 else 0.0
    cost_score = _clamp01(1.0 - cost_to_target) if math.isfinite(cost_to_target) else 0.0
    move_score = _clamp01(abs(recent_return) / th.move_ref) if th.move_ref > 0 else 0.0
    quality_score = round(0.25 * vol_score + 0.20 * range_score + 0.30 * cost_score
                          + 0.15 * move_score + 0.10 * float(setup_quality or 0.0), 4)

    out: dict[str, Any] = {
        "side": side, "timeframe": timeframe, "strategy_family": strategy_family,
        "signal_idx": signal_idx, "setup_quality_score": quality_score,
        "cost_to_target_ratio": round(cost_to_target, 4) if math.isfinite(cost_to_target) else None,
        "expected_move_vs_cost": round(expected_move_vs_cost, 4) if math.isfinite(expected_move_vs_cost) else None,
        "atr_pct": round(float(atr_pct), 6), "range_to_atr": round(float(range_to_atr), 4),
        "sl_pct": round(float(sl_pct), 6), "pattern_memory_decision": "NOT_CHECKED",
        "candidate_entry_ts": candidate_entry_ts, "no_lookahead_status": "NOT_APPLICABLE_NO_MEMORY",
        "memory_cases_total": 0, "memory_cases_prefix_used": 0,
        "memory_cases_future_excluded": 0, "same_timestamp_excluded": 0,
        "quality_gate_reasons": reasons, **_safety()}

    def finish(decision: str) -> dict[str, Any]:
        out["quality_gate_decision"] = decision
        out["shadow_allowed"] = (decision == Q_PASS)
        return out

    # 1. cost eats the target
    if tp_pct <= 0 or cost_to_target >= th.max_cost_to_target:
        reasons.append(f"cost_to_target={out['cost_to_target_ratio']}>={th.max_cost_to_target}")
        return finish(Q_COST)
    # 2. volatility too low (no room to move)
    if atr_pct < th.min_atr_pct:
        reasons.append(f"atr_pct={out['atr_pct']}<{th.min_atr_pct}")
        return finish(Q_VOL)
    # 3. range too low / dead bar / pure noise
    if range_to_atr < th.min_range_ratio:
        reasons.append(f"range_to_atr={out['range_to_atr']}<{th.min_range_ratio}")
        return finish(Q_RANGE)
    # 4. spread proxy too large relative to target
    spread_ratio = (spread_fraction / tp_pct) if tp_pct > 0 else math.inf
    if spread_ratio > th.max_spread_ratio:
        reasons.append(f"spread_ratio={round(spread_ratio,4)}>{th.max_spread_ratio}")
        return finish(Q_SPREAD)
    # 5. stop required is too big
    if sl_pct > th.max_sl_pct:
        reasons.append(f"sl_pct={out['sl_pct']}>{th.max_sl_pct}")
        return finish(Q_RISK)
    # 6. duplicate signal too close to a previously accepted one (same group)
    if dup_last_idx is not None and (signal_idx - dup_last_idx) < th.dup_min_gap:
        reasons.append(f"signal_gap={signal_idx - dup_last_idx}<{th.dup_min_gap}")
        return finish(Q_DUP)
    # 7. aggregate quality below floor -> attribute to weakest dimension
    if quality_score < th.min_quality_score:
        worst = min((("vol", vol_score), ("range", range_score),
                     ("cost", cost_score), ("move", move_score)), key=lambda kv: kv[1])[0]
        reasons.append(f"quality_score={quality_score}<{th.min_quality_score}|weakest={worst}")
        return finish({"vol": Q_VOL, "range": Q_RANGE, "cost": Q_COST, "move": Q_RANGE}[worst])
    # 8. historical pattern memory (V10.11) - PREFIX ONLY, no lookahead
    if memory_cases is not None and features is not None:
        past_cases, meta = prefix_memory_cases_for_candidate(
            memory_cases, candidate_entry_ts, candidate_pattern_id)
        out["no_lookahead_status"] = meta["no_lookahead_status"]
        out["memory_cases_total"] = meta["memory_cases_total"]
        out["memory_cases_prefix_used"] = meta["memory_cases_prefix_used"]
        out["memory_cases_future_excluded"] = meta["memory_cases_future_excluded"]
        out["same_timestamp_excluded"] = meta["same_timestamp_excluded"]
        if meta["no_lookahead_status"] != "OK_PREFIX_ONLY":
            # missing candidate_entry_ts -> fail closed, never use full memory
            reasons.extend(meta["warnings"])
            out["pattern_memory_decision"] = "FAIL_MISSING_TIMESTAMP"
            return finish(Q_NOSIM)
        q = pmem.query_similar(
            past_cases, features, min_similar=th.min_similar_cases,
            cost_bps=cost_bps, slippage_bps=slippage_bps, spread_bps=spread_bps,
            false_discovery_risk=false_discovery_risk)
        dec = q.get("decision")
        out["pattern_memory_decision"] = dec
        out["pattern_similar_cases"] = q.get("similar_cases_count")
        out["pattern_net_EV"] = q.get("net_EV")
        out["pattern_profit_factor"] = q.get("profit_factor")
        out["pattern_closed_green_rate"] = q.get("closed_green_rate")
        if dec == pmem.F_FEW:
            reasons.append("pattern_memory_insufficient_similar_cases")
            return finish(Q_NOSIM)
        if dec == pmem.F_EV:
            reasons.append("pattern_memory_negative_ev")
            return finish(Q_EV)
        if dec == pmem.F_FD:
            reasons.append("pattern_memory_false_discovery_high")
            return finish(Q_FD)
        if dec != pmem.PASS:
            reasons.append(f"pattern_memory_{dec}")
            return finish(Q_PATTERN)
        # even when the memory gate passes, enforce the run's own thresholds
        nev = q.get("net_EV", 0.0) or 0.0
        pf = q.get("profit_factor", 0.0)
        cg = q.get("closed_green_rate", 0.0) or 0.0
        if nev <= 0 or nev < th.min_net_ev:
            reasons.append(f"pattern_net_EV={nev}<min_net_ev={th.min_net_ev}")
            return finish(Q_EV)
        if (not isinstance(pf, str)) and pf < th.min_profit_factor:
            reasons.append(f"pattern_PF={pf}<min_pf={th.min_profit_factor}")
            return finish(Q_PATTERN)
        if cg < th.min_closed_green_rate:
            reasons.append(f"pattern_closed_green={cg}<min={th.min_closed_green_rate}")
            return finish(Q_PATTERN)
    elif memory_cases is not None and features is None:
        reasons.append("no_feature_vector_for_pattern_query")
        return finish(Q_NOSIM)
    else:
        out["pattern_memory_decision"] = "SKIPPED_NO_MEMORY"
    # 9. run-level false-discovery overlay
    if str(false_discovery_risk).upper() == "HIGH":
        reasons.append("false_discovery_risk_high")
        return finish(Q_FD)
    reasons.append("all_quality_and_pattern_checks_passed")
    return finish(Q_PASS)


# --------------------------------------------------------------------------
# 3. Intraday data readiness (research-only; no network, no invented data)
# --------------------------------------------------------------------------

def _tf_days_covered(path: str) -> float:
    try:
        bars = lab.load_ohlcv(path)
    except Exception:
        return 0.0
    if len(bars) < 2:
        return 0.0
    span = bars[-1]["ts"] - bars[0]["ts"]
    return round(span / DAY_MS, 2) if span > 0 else 0.0


def intraday_data_readiness(sample_dir, symbols=None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "tool_version": TOOL_VERSION, "generated_at": _now_iso(),
        "sample_dir": sample_dir, "has_1m": False, "has_5m": False,
        "has_3m": False, "intraday_symbols": [], "intraday_days_by_tf": {},
        "missing_orderbook": True, "missing_trades": True,
        "missing_oi_historical": True, "missing_liquidations": True,
        "scalping_ready": False, "microstructure_ready": False,
        "status": "NO_INTRADAY_DATA", **_safety()}
    if not (isinstance(sample_dir, str) and os.path.isdir(sample_dir)):
        report["errors"] = ["sample_dir_not_found"]
        return report
    try:
        files = os.listdir(sample_dir)
    except Exception:
        report["errors"] = ["sample_dir_unreadable"]
        return report
    syms_found: set[str] = set()
    days: dict[str, list[float]] = {}
    for tf in SCALPING_TIMEFRAMES:
        suffix = f"_{tf}_ohlcv.csv"
        matched = [f for f in files if f.endswith(suffix)]
        for f in matched:
            sym = f[: -len(suffix)]
            if symbols and sym not in symbols:
                continue
            syms_found.add(sym)
            days.setdefault(tf, []).append(_tf_days_covered(os.path.join(sample_dir, f)))
        if days.get(tf):
            report["intraday_days_by_tf"][tf] = round(min(days[tf]), 2)
    report["has_1m"] = bool(days.get("1m"))
    report["has_3m"] = bool(days.get("3m"))
    report["has_5m"] = bool(days.get("5m"))
    report["intraday_symbols"] = sorted(syms_found)
    # microstructure files (orderbook / trades) - look but do not require
    report["missing_orderbook"] = not any("orderbook" in f.lower() for f in files)
    report["missing_trades"] = not any(f.lower().endswith("_trades.csv") for f in files)
    report["missing_oi_historical"] = not any("open_interest" in f.lower() or "_oi" in f.lower() for f in files)
    report["missing_liquidations"] = not any("liquidation" in f.lower() for f in files)

    has_any_intraday = report["has_1m"] or report["has_5m"] or report["has_3m"]
    has_micro = (not report["missing_orderbook"]) and (not report["missing_trades"])
    min_days = min((min(v) for v in days.values()), default=0.0)
    enough_symbols = len(syms_found) >= 2
    enough_days = min_days >= 14.0
    if not has_any_intraday:
        report["status"] = "NO_INTRADAY_DATA"
        report["scalping_ready"] = False
    elif has_micro and report["has_1m"] and enough_symbols and enough_days:
        report["status"] = "MICROSTRUCTURE_READY"
        report["scalping_ready"] = True
        report["microstructure_ready"] = True
    elif report["has_1m"] or report["has_5m"]:
        if enough_symbols and enough_days:
            report["status"] = "INTRADAY_RESEARCH_READY"
            report["scalping_ready"] = True
        else:
            report["status"] = "PARTIAL_INTRADAY_DATA"
            report["scalping_not_ready_reason"] = "PARTIAL_NOT_SCALPING_READY"
    else:
        report["status"] = "PARTIAL_INTRADAY_DATA"
    return report


# --------------------------------------------------------------------------
# 6. Future paper-readiness criteria (DEFINED only; never activated now)
# --------------------------------------------------------------------------

def paper_readiness_criteria() -> dict[str, Any]:
    return {
        "min_shadow_trades": 200, "min_net_ev_after_costs": 0.0,
        "min_profit_factor": 1.2, "min_closed_green_rate": 0.5,
        "max_avg_loss_fraction": 0.02, "max_drawdown_proxy": 0.5,
        "cost_stress_x2_positive": True, "cost_stress_x3_positive": True,
        "min_windows": 2, "min_symbols": 2, "no_single_coin_dependence": True,
        "no_single_regime_dependence": True, "false_discovery_not_high": True,
        "no_missing_critical_data": True, "min_forward_shadow_days": 30,
        "note": "criteria are DEFINED for the future only; paper is NOT activated",
        "paper_candidate_future": False, **_safety()}


def evaluate_paper_candidate(metrics, *, n_shadow_trades, windows, symbols_covered,
                             false_discovery_risk, missing_critical_data) -> dict[str, Any]:
    crit = paper_readiness_criteria()
    checks = {
        "shadow_trades_ok": n_shadow_trades >= crit["min_shadow_trades"],
        "net_ev_ok": (metrics.get("net_EV", 0.0) or 0.0) > 0.0,
        "profit_factor_ok": (not isinstance(metrics.get("net_PF"), str))
                            and (metrics.get("net_PF", 0.0) or 0.0) >= crit["min_profit_factor"],
        "closed_green_ok": (metrics.get("closed_green_rate", 0.0) or 0.0) >= crit["min_closed_green_rate"],
        "avg_loss_ok": abs(metrics.get("avg_loss", 0.0) or 0.0) <= crit["max_avg_loss_fraction"],
        "cost_stress_ok": (metrics.get("cost_stress_x2", -1) or -1) > 0
                          and (metrics.get("cost_stress_x3", -1) or -1) > 0,
        "windows_ok": windows >= crit["min_windows"],
        "symbols_ok": symbols_covered >= crit["min_symbols"],
        "false_discovery_ok": str(false_discovery_risk).upper() != "HIGH",
        "data_ok": not missing_critical_data}
    # Even if every check passed, the engine NEVER auto-promotes: paper stays off.
    return {"criteria": crit, "checks": checks, "all_checks_passed": all(checks.values()),
            "paper_candidate_future": False, **_safety()}


# --------------------------------------------------------------------------
# 5. Integrated engine: detect -> quality gate -> pattern memory -> simulate
# --------------------------------------------------------------------------

def _candidate_setups(bars, atr_list, emaf, emas, ents, *, mode, costs, exit_policy):
    """Attach a feature vector + range_to_atr to each raw entry. In
    latest-snapshot / forward-shadow only the most recent entry is evaluated."""
    if mode in ("latest-snapshot", "forward-shadow") and ents:
        ents = [max(ents, key=lambda e: e["signal_idx"])]
    setups = []
    params = micro._param_sets(exit_policy)[0]  # type: ignore[attr-defined]
    for e in ents:
        i = e["signal_idx"]
        a = atr_list[i] if atr_list[i] is not None else (e.get("atr") or 0.0)
        price = bars[i]["close"] or 1.0
        rng = bars[i]["high"] - bars[i]["low"]
        feat = pmem.feature_vector(bars, atr_list, emaf, emas, i, entry=e, costs=costs)
        setups.append({
            "entry": e, "features": feat, "params": params,
            "atr_pct": (a / price) if price else 0.0,
            "range_to_atr": (rng / a) if a > 0 else 0.0,
            "recent_return": feat.get("recent_return", 0.0),
            "setup_quality": e.get("setup_quality_score", 0.5)})
    return setups


def run_intelligent_shadow(*, sample_dir, symbols, timeframes, sides, strategy_families,
                           mode="offline-replay", cost_bps=6.0, slippage_bps=4.0,
                           spread_bps=2.0, latency_bars=1, gap_policy="adverse_open",
                           max_candidates_per_run=400, exit_policy="micro_profit_take",
                           thresholds=None) -> dict[str, Any]:
    th = thresholds or QualityThresholds()
    mode = mode if mode in MODES else "offline-replay"
    costs = micro.MicroCosts(cost_bps, slippage_bps, spread_bps, True, latency_bars)
    families = [f for f in strategy_families if f in micro.STRATEGY_FAMILIES]
    sides = [s.upper() for s in sides if s.upper() in micro.SIDES]
    timeframes = [t.lower() for t in timeframes]
    rt = costs.round_trip_fraction()
    spread_fraction = spread_bps / 10_000.0

    report: dict[str, Any] = {
        "tool_version": TOOL_VERSION, "generated_at": _now_iso(), "sample_dir": sample_dir,
        "mode": mode, "symbols": symbols, "timeframes": timeframes, "sides": sides,
        "strategy_families": families, "exit_policy": exit_policy,
        "thresholds": th.as_dict(), "cost_model": costs.as_dict(),
        "orderbook_real": False, "missing_oi_historical": True,
        "missing_liquidations": True, "errors": [], "warnings": [],
        "quality_decisions": [], "pattern_decisions": [], "shadow_trades": [],
        "rejected_setups": [], **_safety()}

    if not (isinstance(sample_dir, str) and os.path.isdir(sample_dir)):
        report["errors"].append("sample_dir_not_found")
        return report
    if not (families and sides and timeframes and symbols):
        report["errors"].append("nothing_to_run")
        return report

    # intraday readiness: scalping conclusions require 1m/5m data
    intraday = intraday_data_readiness(sample_dir, symbols)
    report["intraday_status"] = intraday["status"]
    report["scalping_conclusive"] = bool(intraday.get("scalping_ready"))
    if not intraday.get("scalping_ready"):
        report["warnings"].append("INTRADAY_DATA_REQUIRED")
        report["data_disclaimer"] = ("no 1m/5m data: results on 4h/6h are a NON-CONCLUSIVE demo; "
                                     "scalping is NOT validated")

    # 1. build the historical pattern memory once, derive false-discovery risk
    memory = pmem.build_pattern_memory(
        sample_dir=sample_dir, symbols=symbols, timeframes=timeframes, sides=sides,
        strategy_families=families, exit_policies=[exit_policy], cost_bps=cost_bps,
        slippage_bps=slippage_bps, spread_bps=spread_bps, latency_bars=latency_bars,
        gap_policy=gap_policy)
    mem_cases = memory.get("cases", [])
    report["pattern_memory_cases"] = len(mem_cases)
    report["pattern_memory_build_status"] = memory.get("build_status")
    gate = pmem.shadow_gate(memory) if mem_cases else {"false_discovery_risk": "LOW",
                                                       "n_passed": 0, "n_queries": 0}
    fdr = gate.get("false_discovery_risk", "LOW")
    report["false_discovery_risk"] = fdr
    report["pattern_gate_n_queries"] = gate.get("n_queries", 0)
    report["pattern_gate_n_passed"] = gate.get("n_passed", 0)

    # 2. iterate the sample, evaluate candidate setups through both gates.
    # Each candidate may only see STRICTLY-PAST memory (no lookahead).
    raw = 0
    passed_structural = 0
    passed_pattern = 0
    passed_full = 0
    future_excluded_total = 0
    same_ts_excluded_total = 0
    no_lookahead_ok = True
    shadow_trades: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    quality_rows: list[dict[str, Any]] = []
    dup_tracker: dict[tuple, int] = {}
    budget = max(1, int(max_candidates_per_run))

    for sym in symbols:
        if raw >= budget:
            break
        fpf = os.path.join(sample_dir, f"{sym}_funding.csv")
        funding = lab.load_funding(fpf) if os.path.isfile(fpf) else []
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
            for side in sides:
                for fam in families:
                    ents = micro.generate_micro_entries(
                        symbol=sym, timeframe=tf, side=side, family=fam,
                        bars=bars, funding=funding, max_entries=budget)
                    setups = _candidate_setups(bars, atr_list, emaf, emas, ents,
                                               mode=mode, costs=costs, exit_policy=exit_policy)
                    grp = (sym, tf, side, fam)
                    for s in setups:
                        if raw >= budget:
                            break
                        raw += 1
                        e, feat, params = s["entry"], s["features"], s["params"]
                        qg = quality_pre_gate(
                            side=side, timeframe=tf, strategy_family=fam,
                            signal_idx=e["signal_idx"], tp_pct=params.get("tp_pct", 0.004),
                            sl_pct=params.get("sl_pct", 0.005), atr_pct=s["atr_pct"],
                            range_to_atr=s["range_to_atr"], recent_return=s["recent_return"],
                            round_trip_fraction=rt, spread_fraction=spread_fraction,
                            setup_quality=s["setup_quality"], features=feat,
                            memory_cases=mem_cases, candidate_entry_ts=e["entry_ts"],
                            false_discovery_risk=fdr,
                            dup_last_idx=dup_tracker.get(grp), thresholds=th,
                            cost_bps=cost_bps, slippage_bps=slippage_bps, spread_bps=spread_bps)
                        qg["symbol"] = sym
                        dup_tracker[grp] = e["signal_idx"]
                        quality_rows.append(qg)
                        future_excluded_total += qg.get("memory_cases_future_excluded", 0)
                        same_ts_excluded_total += qg.get("same_timestamp_excluded", 0)
                        if qg.get("no_lookahead_status") == "FAIL_MISSING_CANDIDATE_ENTRY_TS":
                            no_lookahead_ok = False
                        report["pattern_decisions"].append({
                            "symbol": sym, "group": f"{tf}/{side}/{fam}",
                            "pattern_memory_decision": qg.get("pattern_memory_decision"),
                            "memory_cases_prefix_used": qg.get("memory_cases_prefix_used"),
                            "memory_cases_future_excluded": qg.get("memory_cases_future_excluded"),
                            "pattern_net_EV": qg.get("pattern_net_EV"),
                            "pattern_closed_green_rate": qg.get("pattern_closed_green_rate")})
                        if qg["quality_gate_decision"] not in (Q_COST, Q_VOL, Q_RANGE,
                                                               Q_SPREAD, Q_RISK, Q_DUP):
                            passed_structural += 1
                        if qg.get("pattern_memory_decision") == pmem.PASS:
                            passed_pattern += 1
                        if qg["shadow_allowed"]:
                            passed_full += 1
                        if qg["shadow_allowed"]:
                            tr = micro.simulate_micro_trade(
                                bars, atr_list, e, policy=exit_policy, params=params,
                                costs=costs, funding=funding, gap_policy=gap_policy)
                            if tr is not None:
                                tr["would_enter"] = True
                                tr["quality_gate_decision"] = qg["quality_gate_decision"]
                                tr["pattern_memory_decision"] = qg.get("pattern_memory_decision")
                                tr["why_entered"] = "passed quality + pattern memory"
                                tr["why_closed"] = tr["exit_reason"]
                                shadow_trades.append(tr)
                        else:
                            rejected.append({
                                "symbol": sym, "timeframe": tf, "side": side,
                                "strategy_family": fam, "signal_idx": e["signal_idx"],
                                "setup_quality_score": qg["setup_quality_score"],
                                "quality_gate_decision": qg["quality_gate_decision"],
                                "pattern_memory_decision": qg.get("pattern_memory_decision"),
                                "why_skipped": ";".join(qg["quality_gate_reasons"])})

    metrics = micro.micro_metrics(shadow_trades, costs)
    report["quality_decisions"] = quality_rows
    report["shadow_trades"] = shadow_trades
    report["rejected_setups"] = rejected
    report["raw_setups"] = raw
    # Clear, non-misleading funnel metrics (V10.12.1): "structural" is NOT
    # "high quality final" - it only means the cheap structural checks passed.
    report["passed_structural_pre_gate"] = passed_structural
    report["failed_structural_pre_gate"] = raw - passed_structural
    report["passed_pattern_memory_gate"] = passed_pattern
    report["failed_pattern_memory_gate"] = passed_structural - passed_pattern
    report["passed_full_quality_and_pattern_gate"] = passed_full
    report["n_shadow_trades"] = len(shadow_trades)
    report["n_rejected"] = len(rejected)
    # legacy alias kept for back-compat; documented as STRUCTURAL only
    report["passed_quality_gate_legacy_alias"] = passed_structural
    # no-lookahead audit trail
    report["no_lookahead_status"] = "OK_PREFIX_ONLY" if no_lookahead_ok else "FAIL_LOOKAHEAD_DETECTED"
    report["memory_cases_total"] = len(mem_cases)
    report["memory_cases_future_excluded"] = future_excluded_total
    report["same_timestamp_excluded"] = same_ts_excluded_total
    report["metrics"] = metrics
    # rejection breakdown
    breakdown: dict[str, int] = {}
    for r in rejected:
        d = r["quality_gate_decision"]
        breakdown[d] = breakdown.get(d, 0) + 1
    report["rejection_breakdown"] = breakdown
    windows = len({c.get("month_bucket") for c in mem_cases}) if mem_cases else 0
    symbols_covered = len({t["symbol"] for t in shadow_trades})
    report["paper_candidate"] = evaluate_paper_candidate(
        metrics, n_shadow_trades=len(shadow_trades), windows=windows,
        symbols_covered=symbols_covered, false_discovery_risk=fdr,
        missing_critical_data=not intraday.get("scalping_ready"))
    return report


# --------------------------------------------------------------------------
# Reporting (main run dir + forward-shadow journal). Path-safe, gitignored.
# --------------------------------------------------------------------------

def _run_id(report) -> str:
    h = hashlib.sha256(json.dumps(report.get("symbols", []), sort_keys=True).encode()).hexdigest()[:8]
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + h


def _write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_v1012_reports(report, output_dir=None) -> str:
    base = _safe_output_base(output_dir)
    run_dir = os.path.join(base, _run_id(report)).replace("\\", "/")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "intelligent_shadow_summary.json"), "w", encoding="utf-8") as f:
        slim = {k: v for k, v in report.items()
                if k not in ("quality_decisions", "shadow_trades", "rejected_setups",
                             "pattern_decisions")}
        json.dump(slim, f, indent=2, default=str)
    _write_csv(os.path.join(run_dir, "quality_gate_decisions.csv"), report.get("quality_decisions", []),
               ["symbol", "timeframe", "side", "strategy_family", "signal_idx",
                "setup_quality_score", "cost_to_target_ratio", "expected_move_vs_cost",
                "atr_pct", "range_to_atr", "sl_pct", "quality_gate_decision",
                "pattern_memory_decision", "no_lookahead_status", "candidate_entry_ts",
                "memory_cases_total", "memory_cases_prefix_used",
                "memory_cases_future_excluded", "same_timestamp_excluded", "shadow_allowed"])
    _write_csv(os.path.join(run_dir, "pattern_memory_decisions.csv"), report.get("pattern_decisions", []),
               ["symbol", "group", "pattern_memory_decision", "memory_cases_prefix_used",
                "memory_cases_future_excluded", "pattern_net_EV", "pattern_closed_green_rate"])
    _write_csv(os.path.join(run_dir, "shadow_trades.csv"), report.get("shadow_trades", []),
               ["symbol", "timeframe", "side", "strategy_family", "policy", "entry_ts",
                "entry_price", "exit_price", "exit_reason", "net_pnl", "gross_pnl",
                "closed_green", "went_green", "green_to_red_failure", "mfe", "mae",
                "fee", "slippage", "spread_cost", "would_enter", "why_entered", "why_closed"])
    _write_csv(os.path.join(run_dir, "rejected_setups.csv"), report.get("rejected_setups", []),
               ["symbol", "timeframe", "side", "strategy_family", "signal_idx",
                "setup_quality_score", "quality_gate_decision", "pattern_memory_decision",
                "why_skipped"])
    ranking = sorted(report.get("quality_decisions", []),
                     key=lambda q: q.get("setup_quality_score", 0.0), reverse=True)[:50]
    _write_csv(os.path.join(run_dir, "candidate_quality_ranking.csv"), ranking,
               ["symbol", "timeframe", "side", "strategy_family", "setup_quality_score",
                "cost_to_target_ratio", "quality_gate_decision", "pattern_memory_decision"])
    _write_report_md(os.path.join(run_dir, "report.md"), report)
    return run_dir


def write_shadow_journal(report, output_dir=None) -> str:
    base = _safe_output_base(output_dir or JOURNAL_ROOT)
    run_dir = os.path.join(base, _run_id(report)).replace("\\", "/")
    os.makedirs(run_dir, exist_ok=True)
    rows = []
    for t in report.get("shadow_trades", []):
        rows.append({
            "timestamp": t.get("entry_ts"), "symbol": t.get("symbol"),
            "timeframe": t.get("timeframe"), "side": t.get("side"),
            "strategy_family": t.get("strategy_family"),
            "quality_gate_decision": t.get("quality_gate_decision"),
            "quality_gate_reasons": "passed",
            "pattern_memory_decision": t.get("pattern_memory_decision"),
            "would_enter": True, "entry_price": t.get("entry_price"),
            "simulated_exit_price": t.get("exit_price"), "exit_reason": t.get("exit_reason"),
            "closed_green": t.get("closed_green"), "net_result_after_costs": t.get("net_pnl"),
            "fees": t.get("fee"), "slippage": t.get("slippage"),
            "spread_cost": t.get("spread_cost"), "MFE": t.get("mfe"), "MAE": t.get("mae"),
            "green_to_red_failure": t.get("green_to_red_failure"),
            "why_skipped": "", "why_entered": t.get("why_entered"),
            "why_closed": t.get("why_closed"), "final_recommendation": "NO LIVE"})
    for r in report.get("rejected_setups", []):
        rows.append({
            "timestamp": "", "symbol": r.get("symbol"), "timeframe": r.get("timeframe"),
            "side": r.get("side"), "strategy_family": r.get("strategy_family"),
            "quality_gate_decision": r.get("quality_gate_decision"),
            "quality_gate_reasons": r.get("why_skipped"),
            "pattern_memory_decision": r.get("pattern_memory_decision"),
            "would_enter": False, "entry_price": "", "simulated_exit_price": "",
            "exit_reason": "", "closed_green": "", "net_result_after_costs": "",
            "fees": "", "slippage": "", "spread_cost": "", "MFE": "", "MAE": "",
            "green_to_red_failure": "", "why_skipped": r.get("why_skipped"),
            "why_entered": "", "why_closed": "", "final_recommendation": "NO LIVE"})
    _write_csv(os.path.join(run_dir, "journal.csv"), rows,
               ["timestamp", "symbol", "timeframe", "side", "strategy_family",
                "quality_gate_decision", "quality_gate_reasons", "pattern_memory_decision",
                "would_enter", "entry_price", "simulated_exit_price", "exit_reason",
                "closed_green", "net_result_after_costs", "fees", "slippage", "spread_cost",
                "MFE", "MAE", "green_to_red_failure", "why_skipped", "why_entered",
                "why_closed", "final_recommendation"])
    with open(os.path.join(run_dir, "journal_summary.json"), "w", encoding="utf-8") as f:
        slim = {k: v for k, v in report.items()
                if k not in ("quality_decisions", "shadow_trades", "rejected_setups",
                             "pattern_decisions")}
        json.dump(slim, f, indent=2, default=str)
    return run_dir


def _write_report_md(path, report):
    m = report.get("metrics", {})
    lines = [
        "# ResearchOps V10.12 - Quality-Gated Intelligent Shadow Scalper",
        "", "RESEARCH ONLY / SHADOW ONLY. NO LIVE. NO PAPER. NO ORDERS.",
        "Candidates are hypotheses, NOT signals. Nothing is approved for paper or live.",
        "", f"- mode: {report.get('mode')}",
        f"- sample_dir: {report.get('sample_dir')}",
        f"- intraday_status: {report.get('intraday_status')}",
        f"- scalping_conclusive: {report.get('scalping_conclusive')}",
        f"- false_discovery_risk: {report.get('false_discovery_risk')}",
        "", "## No-lookahead audit",
        f"- no_lookahead_status: {report.get('no_lookahead_status')}",
        f"- memory_cases_total: {report.get('memory_cases_total')}",
        f"- memory_cases_future_excluded: {report.get('memory_cases_future_excluded')}",
        f"- same_timestamp_excluded: {report.get('same_timestamp_excluded')}",
        "", "## Funnel (structural pre-gate is NOT 'high quality final')",
        f"- raw_setups: {report.get('raw_setups')}",
        f"- passed_structural_pre_gate: {report.get('passed_structural_pre_gate')}",
        f"- failed_structural_pre_gate: {report.get('failed_structural_pre_gate')}",
        f"- passed_pattern_memory_gate: {report.get('passed_pattern_memory_gate')}",
        f"- failed_pattern_memory_gate: {report.get('failed_pattern_memory_gate')}",
        f"- passed_full_quality_and_pattern_gate: {report.get('passed_full_quality_and_pattern_gate')}",
        f"- n_shadow_trades: {report.get('n_shadow_trades')}",
        f"- rejected: {report.get('n_rejected')}",
        f"- rejection_breakdown: {report.get('rejection_breakdown')}",
        "", "## Shadow trade metrics (passed both gates)",
        f"- net_EV: {m.get('net_EV')}",
        f"- net_PF: {m.get('net_PF')}",
        f"- closed_green_rate: {m.get('closed_green_rate')}",
        f"- green_to_red_rate: {m.get('green_to_red_rate')}",
        "", "## Paper readiness (future only)",
        f"- paper_candidate_future: {report.get('paper_candidate', {}).get('paper_candidate_future')}",
        "", "These setups are not signals. With the current data there is no validated edge.",
        "", "research_only: true", "shadow_only: true", "paper_ready: false",
        "live_ready: false", "can_send_real_orders: false", "paper_filter_enabled: false",
        "final_recommendation: NO LIVE"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def latest_v1012_summary(output_dir=None) -> dict[str, Any] | None:
    base = _safe_output_base(output_dir)
    if not os.path.isdir(base):
        return None
    runs = sorted((d for d in os.listdir(base)
                   if os.path.isfile(os.path.join(base, d, "intelligent_shadow_summary.json"))),
                  reverse=True)
    if not runs:
        return None
    try:
        with open(os.path.join(base, runs[0], "intelligent_shadow_summary.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def intelligent_shadow_plan() -> dict[str, Any]:
    return {
        "objective": ("detect a setup, run the quality pre-gate, consult the V10.11 "
                      "pattern memory, only then simulate a shadow trade; reject anything "
                      "without positive net EV; NEVER trade real, NEVER activate paper"),
        "flow": ["detect_raw_setup", "quality_pre_gate_v1012", "pattern_memory_v1011",
                 "simulate_micro_trade_v1010", "record_journal", "recompute_metrics"],
        "quality_gate_decisions": [Q_PASS, Q_COST, Q_VOL, Q_RANGE, Q_PATTERN, Q_EV,
                                   Q_SPREAD, Q_NOSIM, Q_RISK, Q_DUP, Q_FD],
        "modes": list(MODES),
        "intraday_states": ["NO_INTRADAY_DATA", "PARTIAL_INTRADAY_DATA",
                            "INTRADAY_RESEARCH_READY", "MICROSTRUCTURE_READY"],
        "never": ["place_order", "create_order", "set_leverage", "set_margin_mode",
                  "ExecutionEngine.execute", "PaperTrader.open_position",
                  "APPROVED_FOR_PAPER", "APPROVED_FOR_LIVE", "real_orders", "live"],
        "orderbook_real": False, **_safety()}
