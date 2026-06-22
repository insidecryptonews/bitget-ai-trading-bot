"""ResearchOps V10.9 — Coverage-Aware Data Readiness + Multi-Window Validation.

Pure/offline research utilities that make data limits HONEST and stop a partial
dataset from masquerading as full coverage:

- coverage_audit: per symbol/timeframe/data_type coverage vs requested days;
- sample_coverage: requested-days status for a V10.6 sample (no readiness flip);
- history_limits_probe: PUBLIC GET-only diagnosis of how much history Bitget
  actually returns (dry-run by default; reuses the V10.7 allowlist+transport);
- data_readiness_sample: evaluate a concrete sample dir (never 'provider verified');
- multi_window_validation: run the V10.8.1 trailing lab across several recent
  windows and keep only hypotheses stable across >= 2 windows;
- provider_gap_plan: the OI/liquidations data gap plan (no paid download).

Everything is research-only. NOTHING flips paper_ready/live_ready or approves a
candidate. FINAL_RECOMMENDATION: NO LIVE.
"""

from __future__ import annotations

import csv
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import bitget_public_data_v10_7 as pub
from . import adaptive_trailing_exit_v10_8 as lab
from .provider_sample_validator_v10_6 import build_sample_manifest, validate_sample_dir
from .real_replay_backtester_v10_6 import evaluate_backtester_readiness

TOOL_VERSION = "v10.9"
OUTPUT_ROOT = "reports/research/v10_9"
DAY_MS = 86_400_000
FULL_COVERAGE_RATIO = 0.95
INSUFFICIENT_COVERAGE_RATIO = 0.80


def _safety() -> dict[str, Any]:
    return {"research_only": True, "paper_ready": False, "live_ready": False,
            "can_send_real_orders": False, "paper_filter_enabled": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _ratio(actual: float, requested_days: float) -> float:
    if requested_days <= 0:
        return 0.0
    return round(min(2.0, actual / requested_days), 4)


def _read_run_report(staging_dir: str) -> dict[str, Any] | None:
    p = os.path.join(staging_dir, "run_report.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


# --------------------------------------------------------------------------
# A. Coverage-aware staging audit
# --------------------------------------------------------------------------

def coverage_audit(staging_dir: str, expected_days: int = 365) -> dict[str, Any]:
    report: dict[str, Any] = {
        "tool_version": TOOL_VERSION, "staging_dir": staging_dir,
        "requested_days": int(expected_days), "audit_status": "STAGING_BLOCKED",
        "coverage_status": "UNKNOWN_REQUESTED_DAYS",
        "global_actual_days_covered": 0.0, "global_coverage_ratio": 0.0,
        "per_symbol_timeframe_coverage": [], "timeframe_coverage_summary": {},
        "min_coverage_ratio_by_timeframe": {}, "max_coverage_ratio_by_timeframe": {},
        "undercovered_timeframes": [], "undercovered_symbols": [],
        "coverage_warning_count": 0, "blockers": [], "warnings": [],
        "missing_oi_historical": True, "missing_liquidations": True,
        "edge_validated": False, "comparison_not_portfolio": True,
        **_safety()}
    # V10.7.1 path safety (read-only)
    block = pub.validate_bitget_public_staging_dir_v107(staging_dir, for_write=False)
    if block is not None:
        report["blockers"].append(block)
        return report
    if not os.path.isdir(staging_dir):
        report["blockers"].append("staging_dir_not_found")
        return report

    rr = _read_run_report(staging_dir)
    requested = int(expected_days)
    if rr and isinstance(rr.get("requested_days"), (int, float)):
        requested = int(rr["requested_days"])
        report["requested_days"] = requested
    if not rr:
        report["warnings"].append("run_report_missing_expected_data_unverifiable")

    # walk candle files: candles/<SYMBOL>/<tf>.csv
    per_st: list[dict[str, Any]] = []
    starts_all, ends_all = [], []
    tf_ratios: dict[str, list[float]] = {}
    sym_under: set[str] = set()
    for root, _dirs, files in os.walk(staging_dir):
        segs = root.replace("\\", "/").split("/")
        if "candles" not in segs:
            continue
        for fn in files:
            if not fn.lower().endswith(".csv"):
                continue
            sym = segs[-1].upper()
            tf = os.path.splitext(fn)[0].lower()
            bars = lab.load_ohlcv(os.path.join(root, fn))
            if not bars:
                continue
            mn, mx = bars[0]["ts"], bars[-1]["ts"]
            starts_all.append(mn)
            ends_all.append(mx)
            actual_days = round((mx - mn) / DAY_MS, 2)
            ratio = _ratio(actual_days, requested)
            per_st.append({"symbol": sym, "timeframe": tf, "rows": len(bars),
                           "actual_days_covered": actual_days, "coverage_ratio": ratio})
            tf_ratios.setdefault(tf, []).append(ratio)
            if ratio < FULL_COVERAGE_RATIO:
                report["warnings"].append(
                    f"symbol_timeframe_coverage_below_expected:{sym}:{tf}:{ratio}")
                sym_under.add(sym)

    if not per_st:
        report["blockers"].append("no_candle_files_for_coverage")
        return report

    report["per_symbol_timeframe_coverage"] = sorted(
        per_st, key=lambda x: (x["timeframe"], x["symbol"]))
    global_actual = round((max(ends_all) - min(starts_all)) / DAY_MS, 2)
    report["global_actual_days_covered"] = global_actual
    report["global_coverage_ratio"] = _ratio(global_actual, requested)
    for tf, ratios in sorted(tf_ratios.items()):
        report["timeframe_coverage_summary"][tf] = {
            "files": len(ratios), "min_ratio": round(min(ratios), 4),
            "max_ratio": round(max(ratios), 4),
            "mean_ratio": round(sum(ratios) / len(ratios), 4)}
        report["min_coverage_ratio_by_timeframe"][tf] = round(min(ratios), 4)
        report["max_coverage_ratio_by_timeframe"][tf] = round(max(ratios), 4)
        if min(ratios) < FULL_COVERAGE_RATIO:
            report["undercovered_timeframes"].append(tf)
            report["warnings"].append(
                f"timeframe_coverage_below_expected:{tf}:{round(min(ratios), 4)}")
    report["undercovered_symbols"] = sorted(sym_under)

    gr = report["global_coverage_ratio"]
    if requested <= 0:
        report["coverage_status"] = "UNKNOWN_REQUESTED_DAYS"
    elif gr >= FULL_COVERAGE_RATIO:
        report["coverage_status"] = "FULL_COVERAGE"
    elif gr >= INSUFFICIENT_COVERAGE_RATIO:
        report["coverage_status"] = "PARTIAL_COVERAGE"
    else:
        report["coverage_status"] = "INSUFFICIENT_COVERAGE"
    if requested > 0 and gr < FULL_COVERAGE_RATIO:
        report["warnings"].append("requested_days_undercovered")
        report["warnings"].append(f"coverage_ratio_below_expected:{gr}")
    report["requested_days_undercovered"] = bool(requested > 0 and gr < FULL_COVERAGE_RATIO)
    report["coverage_warning_count"] = len(report["warnings"])

    # status: clean OK only when full coverage AND no warnings/blockers
    if report["blockers"]:
        report["audit_status"] = "STAGING_BLOCKED"
    elif report["warnings"]:
        report["audit_status"] = "STAGING_HAS_WARNINGS"
    else:
        report["audit_status"] = "STAGING_OK"
    return report


# --------------------------------------------------------------------------
# B. Coverage-aware provider sample validation
# --------------------------------------------------------------------------

def _sample_timeframe_coverage(sample_dir: str, requested_days: int) -> tuple[list, dict]:
    """Per (symbol, tf) coverage from <SYM>_<tf>_ohlcv.csv sample files."""
    per: list[dict[str, Any]] = []
    tf_ratios: dict[str, list[float]] = {}
    try:
        files = [f for f in os.listdir(sample_dir) if f.lower().endswith("_ohlcv.csv")]
    except Exception:
        return per, tf_ratios
    for fn in sorted(files):
        stem = fn[:-len("_ohlcv.csv")]
        parts = stem.split("_")
        if len(parts) < 2:
            continue
        sym, tf = parts[0].upper(), parts[1].lower()
        bars = lab.load_ohlcv(os.path.join(sample_dir, fn))
        if not bars:
            continue
        actual_days = round((bars[-1]["ts"] - bars[0]["ts"]) / DAY_MS, 2)
        ratio = _ratio(actual_days, requested_days)
        per.append({"symbol": sym, "timeframe": tf, "actual_days_covered": actual_days,
                    "coverage_ratio": ratio})
        tf_ratios.setdefault(tf, []).append(ratio)
    return per, tf_ratios


def sample_coverage(sample_dir: str, expected_days: int = 365,
                    provider_id: str = "bitget_official") -> dict[str, Any]:
    v = validate_sample_dir(sample_dir, expected_days=expected_days, provider_id=provider_id)
    cov = v.get("coverage", {})
    actual = float(cov.get("actual_days_covered", 0.0) or 0.0)
    ratio = float(cov.get("coverage_ratio_by_days", 0.0) or 0.0)
    per, tf_ratios = _sample_timeframe_coverage(sample_dir, expected_days)
    human = list(v.get("human_warnings", []))
    if expected_days > 0 and actual < expected_days * FULL_COVERAGE_RATIO:
        status = ("FAILS_REQUESTED_DAYS" if actual < expected_days * INSUFFICIENT_COVERAGE_RATIO
                  else "PARTIAL_REQUESTED_DAYS")
        human.append("requested_days_undercovered")
    else:
        status = "MEETS_REQUESTED_DAYS"
    cov_warnings = []
    tf_cov = {}
    for tf, ratios in sorted(tf_ratios.items()):
        tf_cov[tf] = {"min_ratio": round(min(ratios), 4), "max_ratio": round(max(ratios), 4)}
        if min(ratios) < FULL_COVERAGE_RATIO:
            cov_warnings.append(f"timeframe_coverage_below_expected:{tf}:{round(min(ratios), 4)}")
    long_ready = v.get("data_classification") == lab.CLS_LONG_READY
    return {
        "tool_version": TOOL_VERSION, "sample_dir": sample_dir,
        "provider_id": provider_id, "provider_verified": False,
        "requested_days": int(expected_days), "actual_days_covered": actual,
        "coverage_ratio_by_days": ratio, "requested_days_status": status,
        "data_classification": v.get("data_classification"),
        "sample_ready": bool(v.get("sample_ready")),
        "required_types_missing": v.get("quality", {}).get("required_types_missing", []),
        "timeframe_coverage": tf_cov, "symbol_timeframe_coverage": per,
        "coverage_blockers_or_warnings": cov_warnings,
        "long_history_ready_but_underrequested": bool(long_ready and status != "MEETS_REQUESTED_DAYS"),
        "human_warnings": human, "missing_oi_historical": True,
        "missing_liquidations": True, "edge_validated": False,
        **_safety()}


# --------------------------------------------------------------------------
# C. Bitget public history-limit diagnosis (public GET only, dry-run default)
# --------------------------------------------------------------------------

def _probe_earliest(symbol: str, tf: str, transport: Callable, *,
                    max_requests: int = 12, timeout: float = 10.0) -> dict[str, Any]:
    gran = pub._GRANULARITY.get(tf, tf)
    end = _now_ms()
    earliest = None
    latest = None
    rows_total = 0
    requests_used = 0
    error = ""
    for _ in range(max_requests):
        try:
            payload = transport(pub.EP_CANDLES, {
                "symbol": symbol, "productType": pub.PRODUCT_TYPE, "granularity": gran,
                "endTime": end, "limit": 200}, timeout=timeout)
        except Exception as exc:
            error = f"{type(exc).__name__}:{str(exc)[:80]}"
            break
        requests_used += 1
        rows = pub.parse_candles(payload, symbol=symbol, timeframe=tf)
        if not rows:
            break
        rows_total += len(rows)
        tss = [r["timestamp_ms"] for r in rows]
        mn, mx = min(tss), max(tss)
        latest = mx if latest is None else max(latest, mx)
        new_earliest = mn if earliest is None else min(earliest, mn)
        if earliest is not None and new_earliest >= earliest:
            break  # no backward progress
        earliest = new_earliest
        if len(rows) < 200:
            break  # exhausted the available history
        end = mn - 1
    actual_days = round((latest - earliest) / DAY_MS, 2) if (earliest and latest) else 0.0
    return {"symbol": symbol, "timeframe": tf, "requests_used": requests_used,
            "rows_returned": rows_total, "first_ts": earliest, "last_ts": latest,
            "actual_days": actual_days, "inferred_provider_limit_days": actual_days,
            "inferred_row_limit": 200, "endpoint_error": error,
            "coverage_ratio": None}


def history_limits_probe(*, symbols: list[str], timeframes: list[str],
                         requested_days: list[int] | None = None,
                         apply: bool = False, transport: Callable | None = None,
                         output_dir: str | None = None,
                         max_requests: int = 12) -> dict[str, Any]:
    requested_days = requested_days or [180, 365, 540]
    syms = [str(s).strip().upper() for s in symbols if str(s).strip()]
    tfs = [str(t).strip().lower() for t in timeframes if str(t).strip()]
    report: dict[str, Any] = {
        "tool_version": TOOL_VERSION, "endpoint": pub.EP_CANDLES,
        "allowed_paths": sorted(pub.ALLOWED_PATHS), "dry_run": (not apply),
        "symbols": syms, "timeframes": tfs, "requested_days_probed": requested_days,
        "no_private_auth": True, "no_env": True, "public_get_only": True,
        "probes": [], "provider_public_history_limit_detected": False,
        "recommended_expected_days_by_timeframe": {}, "written_path": "",
        **_safety()}
    if not apply:
        report["note"] = ("dry-run: no network calls. Pass --apply for a bounded "
                          "public GET probe of real available history.")
        report["planned_probes"] = [f"{s}:{tf}" for s in syms for tf in tfs]
        return report

    tx = transport or pub.default_transport
    by_tf: dict[str, list[float]] = {}
    for s in syms:
        for tf in tfs:
            pr = _probe_earliest(s, tf, tx, max_requests=max_requests)
            report["probes"].append(pr)
            if pr["actual_days"] > 0:
                by_tf.setdefault(tf, []).append(pr["actual_days"])
    for tf, days in by_tf.items():
        rec = round(min(days), 0)  # conservative: the worst symbol
        report["recommended_expected_days_by_timeframe"][tf] = rec
    # if any timeframe's max available is materially under a year, flag a limit
    if any(min(days) < 360 for days in by_tf.values()):
        report["provider_public_history_limit_detected"] = True
        report["recommendation"] = [
            "use 6H for the longest history; 4H only up to its real coverage",
            "for full 365d OHLCV+OI+liquidations, a verified external provider is needed"]

    # write report (research-only safe dir)
    base = output_dir or OUTPUT_ROOT
    try:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = os.path.join(base, run_id)
        os.makedirs(run_dir, exist_ok=True)
        wp = os.path.join(run_dir, "history_limits.json")
        with open(wp, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        report["written_path"] = wp.replace("\\", "/")
    except Exception as exc:
        report["warnings"] = [f"report_write_failed:{type(exc).__name__}"]
    return report


# --------------------------------------------------------------------------
# E. Data readiness for a concrete sample dir (never 'provider verified')
# --------------------------------------------------------------------------

def data_readiness_sample(sample_dir: str, expected_days: int = 365,
                          provider_id: str = "bitget_official") -> dict[str, Any]:
    v = validate_sample_dir(sample_dir, expected_days=expected_days, provider_id=provider_id)
    man = build_sample_manifest(sample_dir, expected_days=expected_days,
                                provider_id=provider_id, write=False)
    br = evaluate_backtester_readiness(man).as_dict()
    cov = v.get("coverage", {})
    actual = float(cov.get("actual_days_covered", 0.0) or 0.0)
    sc = sample_coverage(sample_dir, expected_days, provider_id)
    return {
        "tool_version": TOOL_VERSION, "sample_dir": sample_dir,
        "dataset_hash": v.get("dataset_hash", ""),
        "clean_days": actual, "coverage_ratio": float(cov.get("coverage_ratio_by_days", 0.0) or 0.0),
        "requested_days": int(expected_days),
        "requested_days_status": sc["requested_days_status"],
        "missing_required_types": v.get("quality", {}).get("required_types_missing", []),
        "provider_verified": False,
        "data_classification": v.get("data_classification"),
        "manifest_gate_status": man.get("gate_status"),
        "manifest_promotable": bool(man.get("gate_promote_allowed")),
        "backtester_readiness": br.get("status"),
        "backtester_blockers": br.get("blockers", []),
        "missing_oi_historical": True, "missing_liquidations": True,
        "edge_validated": False,
        **_safety()}


# --------------------------------------------------------------------------
# D. Multi-window trailing validation (reuses the V10.8.1 lab)
# --------------------------------------------------------------------------

def _slice_sample(sample_dir: str, window_days: int, out_dir: str) -> str:
    """Write a window-sliced copy (most recent window_days) of the sample's
    OHLCV + funding into out_dir. Returns out_dir."""
    os.makedirs(out_dir, exist_ok=True)
    cutoff_by_file: dict[str, int] = {}
    for fn in sorted(os.listdir(sample_dir)):
        full = os.path.join(sample_dir, fn)
        if not os.path.isfile(full) or not fn.lower().endswith(".csv"):
            continue
        if fn.lower().endswith("_ohlcv.csv"):
            bars = lab.load_ohlcv(full)
            if not bars:
                continue
            cutoff = bars[-1]["ts"] - window_days * DAY_MS
            rows = [b for b in bars if b["ts"] >= cutoff]
            with open(os.path.join(out_dir, fn), "w", encoding="utf-8", newline="") as fh:
                fh.write("timestamp,open,high,low,close,volume\n")
                for b in rows:
                    fh.write(f"{b['ts']},{b['open']},{b['high']},{b['low']},{b['close']},{b['volume']}\n")
        elif fn.lower().endswith("_funding.csv"):
            fr = lab.load_funding(full)
            if not fr:
                continue
            cutoff = fr[-1]["ts"] - window_days * DAY_MS
            rows = [x for x in fr if x["ts"] >= cutoff]
            with open(os.path.join(out_dir, fn), "w", encoding="utf-8", newline="") as fh:
                fh.write("timestamp,funding_rate\n")
                for x in rows:
                    fh.write(f"{x['ts']},{x['rate']}\n")
    return out_dir


def _cand_key(c: dict[str, Any]) -> str:
    return f"{c['timeframe']}/{c['side']}/{c['entry_family']}/{c['exit_policy']}"


def multi_window_validation(*, sample_dir: str, windows: list[int],
                            symbols: list[str], timeframes: list[str],
                            sides: list[str], entry_families: list[str],
                            exit_policies: list[str], cost_bps: float = 6.0,
                            slippage_bps: float = 4.0, min_trades: int = 30,
                            walk_forward_mode: str = "rolling",
                            gap_policy: str = "adverse_open",
                            max_grid_combos: int = 500, seed: int = 7,
                            data_classification: str = lab.CLS_INTERMEDIATE
                            ) -> dict[str, Any]:
    report: dict[str, Any] = {
        "tool_version": TOOL_VERSION, "sample_dir": sample_dir,
        "windows": windows, "walk_forward_mode": walk_forward_mode,
        "gap_policy": gap_policy, "data_classification": data_classification,
        "edge_validated": False, "comparison_not_portfolio": True,
        "candidates_are_hypotheses_not_signals": True,
        "missing_oi_historical": True, "missing_liquidations": True,
        "window_results": [], "multi_window_candidates": [],
        "errors": [], "warnings": [], **_safety()}
    if not os.path.isdir(sample_dir):
        report["errors"].append("sample_dir_not_found")
        return report

    appear: dict[str, dict[str, Any]] = {}
    global_net_evs: list[float] = []
    tmp_root = tempfile.mkdtemp(prefix="v109_mw_")
    for w in windows:
        wdir = _slice_sample(sample_dir, int(w), os.path.join(tmp_root, f"w{w}"))
        rep = lab.run_trailing_exit_lab(
            sample_dir=wdir, symbols=symbols, timeframes=timeframes, sides=sides,
            entry_families=entry_families, exit_policies=exit_policies,
            cost_bps=cost_bps, slippage_bps=slippage_bps, min_trades=min_trades,
            walk_forward_mode=walk_forward_mode, gap_policy=gap_policy,
            max_grid_combos=max_grid_combos, seed=seed,
            data_classification=data_classification)
        g = rep.get("metrics_by", {}).get("global", {})
        global_net_evs.append(g.get("net_EV", 0.0) or 0.0)
        report["window_results"].append({
            "window_days": w, "trades_simulated": rep.get("trades_simulated"),
            "n_research_candidates": rep.get("n_research_candidates"),
            "n_rejected": rep.get("n_rejected_candidates"),
            "global_net_EV": g.get("net_EV"),
            "global_net_PF": g.get("profit_factor_net"),
            "gap_adverse_count": g.get("gap_adverse_count"),
            "side_concentration_warning": rep.get("side_concentration_warning"),
            "false_discovery_risk": rep.get("false_discovery_risk")})
        for c in rep.get("research_candidates", []):
            k = _cand_key(c)
            a = appear.setdefault(k, {"candidate_id": k, "windows_tested": 0,
                                      "windows_passed": 0, "net_EV_by_window": {},
                                      "PF_by_window": {}, "drawdown_by_window": {},
                                      "gap_adverse_by_window": {}, "side": c["side"]})
            a["net_EV_by_window"][str(w)] = c.get("net_EV")
            a["PF_by_window"][str(w)] = c.get("net_PF")
            a["drawdown_by_window"][str(w)] = c.get("max_drawdown")
            a["gap_adverse_by_window"][str(w)] = c.get("gap_adverse_count")
            if (c.get("net_EV") or 0) > 0:
                a["windows_passed"] += 1
    for k, a in appear.items():
        a["windows_tested"] = len(windows)
        a["pass_rate_by_window"] = round(a["windows_passed"] / max(1, len(windows)), 4)
        # multi-window tier: needs >=2 windows with positive EV for the top tier
        if a["windows_passed"] >= 2:
            tier = (lab.CAND_RESEARCH_ONLY if data_classification == lab.CLS_LONG_READY
                    else lab.CAND_WEAK)
        elif a["windows_passed"] == 1:
            tier = lab.CAND_WEAK
        else:
            tier = lab.CAND_REJECTED
        a["final_tier"] = tier

    cands = sorted(appear.values(), key=lambda a: (a["windows_passed"],
                   sum(v or 0 for v in a["net_EV_by_window"].values())), reverse=True)
    report["multi_window_candidates"] = cands
    report["n_multi_window_candidates"] = sum(
        1 for a in cands if a["final_tier"] != lab.CAND_REJECTED)
    report["n_research_candidate_only"] = sum(
        1 for a in cands if a["final_tier"] == lab.CAND_RESEARCH_ONLY)
    report["n_weak_research_hypothesis"] = sum(
        1 for a in cands if a["final_tier"] == lab.CAND_WEAK)
    surviving_sides = {a["side"] for a in cands if a["final_tier"] != lab.CAND_REJECTED}
    report["side_concentration_warning"] = (
        f"{surviving_sides.pop()}_ONLY" if len(surviving_sides) == 1 else "")
    report["global_policy_comparison_net_EV_by_window"] = global_net_evs
    report["any_window_positive_global_edge"] = any(x > 0 for x in global_net_evs)
    report["false_discovery_risk"] = "HIGH"
    report["regime_window_dependency_warning"] = True
    return report


def write_multi_window_reports(report: dict[str, Any], output_dir: str | None = None) -> str:
    base = output_dir or OUTPUT_ROOT
    norm = base.replace("\\", "/")
    if any(s in norm.split("/") for s in ("raw", "backups", "vault", "vaults")) or "%" in norm:
        base = OUTPUT_ROOT
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = os.path.join(base, run_id, "multi_window")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "multi_window_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    cands = report.get("multi_window_candidates", [])
    with open(os.path.join(run_dir, "multi_window_candidates.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["candidate_id", "side", "windows_tested", "windows_passed",
                    "pass_rate_by_window", "final_tier"])
        for a in cands:
            w.writerow([a["candidate_id"], a["side"], a["windows_tested"],
                        a["windows_passed"], a["pass_rate_by_window"], a["final_tier"]])
    with open(os.path.join(run_dir, "window_metrics.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["window_days", "trades_simulated", "n_research_candidates",
                    "global_net_EV", "global_net_PF", "false_discovery_risk"])
        for wr in report.get("window_results", []):
            w.writerow([wr["window_days"], wr["trades_simulated"], wr["n_research_candidates"],
                        wr["global_net_EV"], wr["global_net_PF"], wr["false_discovery_risk"]])
    md = ["# ResearchOps V10.9 — Multi-Window Trailing Validation (RESEARCH ONLY)",
          "",
          "> Candidates are HYPOTHESES, not signals. A policy comparison, not a "
          "portfolio. Edge is NOT validated. NO LIVE.",
          "", f"- windows: {report.get('windows')}",
          f"- walk_forward_mode: {report.get('walk_forward_mode')} | gap_policy: {report.get('gap_policy')}",
          f"- data_classification: {report.get('data_classification')}",
          f"- edge_validated: {report.get('edge_validated')}",
          f"- multi_window_candidates (non-rejected): {report.get('n_multi_window_candidates')}",
          f"- research_candidate_only: {report.get('n_research_candidate_only')} | weak: {report.get('n_weak_research_hypothesis')}",
          f"- side_concentration_warning: {report.get('side_concentration_warning')!r}",
          f"- any_window_positive_global_edge: {report.get('any_window_positive_global_edge')}",
          f"- false_discovery_risk: {report.get('false_discovery_risk')}",
          "", "## Candidates stable across windows (hypotheses, not signals)"]
    for a in cands[:15]:
        md.append(f"- [{a['final_tier']}] {a['candidate_id']} passed "
                  f"{a['windows_passed']}/{a['windows_tested']} windows")
    md += ["", "## Safety", "- research_only: true", "- paper_ready: false",
           "- live_ready: false", "- real_leverage_allowed: false",
           "- candidates are hypotheses, not signals", "- FINAL_RECOMMENDATION: NO LIVE", ""]
    with open(os.path.join(run_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))
    return run_dir.replace("\\", "/")


# --------------------------------------------------------------------------
# F. Provider gap plan (OI history + liquidations) — no paid download
# --------------------------------------------------------------------------

def provider_gap_plan() -> dict[str, Any]:
    return {
        "tool_version": TOOL_VERSION,
        "what_is_missing": ["long historical open interest (per-symbol series)",
                            "historical liquidations", "full 365d OHLCV on low TFs"],
        "why_bitget_public_insufficient": [
            "candles endpoint caps a single request at a 90-day interval",
            "public history is materially under a year on some symbols/timeframes",
            "no public historical OI series", "no public historical liquidations"],
        "minimum_required_data": ["OHLCV 365d", "funding 365d", "OI historical 365d",
                                  "liquidations 365d", "optional trades/orderbook"],
        "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
                    "BNBUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT"],
        "timeframes": ["1h", "4h", "6h", "1d"],
        "expected_format": "CSV/JSONL per <symbol>_<timeframe>_<datatype> with UTC unix_ms ts",
        "offline_validations": ["provider-sample-validate-v106", "provider-sample-coverage-v109",
                                "bitget-public-coverage-audit-v109", "data-readiness-sample-v109"],
        "no_paid_download_before_sample_validation": True,
        "candidate_providers": [
            {"name": "Tardis.dev", "status": "preferred_sample_candidate",
             "notes": "OHLCV/OI/funding/liquidations/trades; request a free sample first"},
            {"name": "CoinGlass", "status": "fallback", "notes": "OI/liquidations aggregated"},
            {"name": "Coinalyze", "status": "limited", "notes": "intraday retention cap ~84d"},
            {"name": "Kaiko / CryptoCompare", "status": "evaluate", "notes": "enterprise; verify ToS"}],
        "provider_checklist": ["Bitget USDT-perp coverage", "365d+ OHLCV", "OI history",
                               "liquidations history", "license allows research",
                               "sample before payment", "stable schema"],
        "sample_request_text": (
            "We need a 365-day historical SAMPLE for Bitget USDT perpetuals "
            "(BTC/ETH + 8 alts) at 1h/4h/6h: OHLCV, funding, OPEN INTEREST series "
            "and LIQUIDATIONS, UTC unix-ms timestamps, CSV/JSONL. Research/eval use; "
            "no payment before we validate the sample offline."),
        "rejection_criteria": ["no Bitget perps", "no OI history", "no liquidations",
                               "<365d", "license forbids research", "no sample",
                               "unstable/garbled schema"],
        "edge_validated": False, "missing_oi_historical": True,
        "missing_liquidations": True, **_safety()}


def write_provider_gap_plan(report: dict[str, Any]) -> tuple[str, str]:
    json_dir = os.path.join(OUTPUT_ROOT)
    os.makedirs(json_dir, exist_ok=True)
    json_path = os.path.join(json_dir, "provider_gap_plan.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    md = ["# ResearchOps V10.9 — Provider Gap Plan (OI history + liquidations)",
          "", "> Research-only. NO paid download before offline sample validation. NO LIVE.",
          "", "## What is missing"]
    md += [f"- {x}" for x in report["what_is_missing"]]
    md += ["", "## Why Bitget public is insufficient"]
    md += [f"- {x}" for x in report["why_bitget_public_insufficient"]]
    md += ["", "## Minimum required data"]
    md += [f"- {x}" for x in report["minimum_required_data"]]
    md += ["", "## Candidate providers"]
    md += [f"- {p['name']} ({p['status']}): {p['notes']}" for p in report["candidate_providers"]]
    md += ["", "## Provider checklist"]
    md += [f"- [ ] {x}" for x in report["provider_checklist"]]
    md += ["", "## Sample request text", "", report["sample_request_text"]]
    md += ["", "## Rejection criteria"]
    md += [f"- {x}" for x in report["rejection_criteria"]]
    md += ["", "## Safety", "- no_paid_download_before_sample_validation: true",
           "- research_only: true", "- paper_ready: false", "- live_ready: false",
           "- FINAL_RECOMMENDATION: NO LIVE", ""]
    md_path = os.path.join("docs", "research_v10_9_provider_gap_plan.md")
    os.makedirs("docs", exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))
    return md_path.replace("\\", "/"), json_path.replace("\\", "/")
