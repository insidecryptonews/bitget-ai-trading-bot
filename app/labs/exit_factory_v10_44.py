"""ResearchOps V10.44 - Exit Factory (research only).

Compares exit families for the Alpha Factory's top entry hypotheses. It does
not change PaperTrader, sizing, leverage or runtime execution. Results are
diagnostic only and remain blocked behind NO LIVE / no paper filter.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import alpha_factory_v10_44 as AF
from . import autonomous_strategy_lab_v10_43b as LAB
from . import shadow_simulation_tournament_v10_40 as SH

TOOL_VERSION = "v10.44"
OUTPUT_SUBDIR = ("reports", "research", "v10_44_alpha_sprint")


def _safety() -> dict[str, Any]:
    return {
        "research_only": True,
        "shadow_only": True,
        "paper_ready": False,
        "live_ready": False,
        "can_send_real_orders": False,
        "paper_filter_enabled": False,
        "edge_validated": False,
        "not_actionable": True,
        "no_orders": True,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


def _out() -> Path:
    return AF.CE._repo_root().joinpath(*OUTPUT_SUBDIR)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _exit_variants() -> list[dict[str, Any]]:
    return [
        {"exit_policy": "baseline_06_06_t30", "tp": 0.006, "sl": 0.006, "trail": None, "horizon": 30},
        {"exit_policy": "quick_profit_04_04_t15", "tp": 0.004, "sl": 0.004, "trail": None, "horizon": 15},
        {"exit_policy": "wider_profit_10_06_t60", "tp": 0.010, "sl": 0.006, "trail": None, "horizon": 60},
        {"exit_policy": "runner_trail_12_06_t90", "tp": 0.012, "sl": 0.006, "trail": 0.005, "horizon": 90},
        {"exit_policy": "defensive_trail_08_04_t45", "tp": 0.008, "sl": 0.004, "trail": 0.003, "horizon": 45},
        {"exit_policy": "time_death_06_05_t10", "tp": 0.006, "sl": 0.005, "trail": None, "horizon": 10},
    ]


def _load_alpha() -> dict[str, Any] | None:
    p = _out() / "alpha_factory_v10_44.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _rule_from_candidate(c: dict[str, Any]) -> dict[str, Any]:
    # Alpha report keeps human-readable rule text; for replay use the canonical
    # strategy name from the V10.44 rule universe.
    for r in AF._rule_defs():
        if r["name"] == c.get("strategy_name"):
            return r
    raise ValueError(f"unknown strategy_name: {c.get('strategy_name')}")


def run_exit_factory(symbols: str = "BTCUSDT", data_source: str = "ws_persistent",
                     write_reports: bool = True,
                     top_n: int = 8) -> dict[str, Any]:
    alpha = _load_alpha()
    if alpha is None:
        alpha = AF.run_alpha_factory(symbols=symbols, data_source=data_source,
                                     max_runtime_minutes=10, write_reports=write_reports)
    source_candidates = list(alpha.get("top_candidates") or [])[:top_n]
    rows: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for c in source_candidates:
        symbol = c.get("symbol") or (str(symbols).split(",")[0].strip().upper() or "BTCUSDT")
        if symbol not in grouped:
            grouped[symbol] = []
        grouped[symbol].append(c)
    n_tests_total = max(1, len(source_candidates) * len(_exit_variants()))
    for symbol, candidates in grouped.items():
        bars, eff_source, _meta = LAB._load_bars(symbol, data_source)
        if len(bars) < AF.MIN_BARS:
            continue
        feats = AF.build_alpha_features(bars)
        q = AF._quantiles(feats, int(len(feats) * 0.60))
        for c in candidates:
            try:
                rule = _rule_from_candidate(c)
            except ValueError:
                continue
            for ex in _exit_variants():
                sim = AF._simulate_candidate(rule, {
                    "exit_name": ex["exit_policy"],
                    "tp": ex["tp"],
                    "sl": ex["sl"],
                    "trail": ex["trail"],
                    "horizon": ex["horizon"],
                }, feats, bars, q, n_tests=n_tests_total)
                tm = sim["metrics_by_split"]["test"]
                vm = sim["metrics_by_split"]["validation"]
                status = _status(tm, vm, c)
                rows.append({
                    "candidate_id": c.get("candidate_id"),
                    "symbol": symbol,
                    "strategy_name": c.get("strategy_name"),
                    "side": c.get("side"),
                    "data_source": eff_source,
                    "exit_policy": ex["exit_policy"],
                    "exit_config": ex,
                    "test_metrics": tm,
                    "validation_metrics": vm,
                    "status": status,
                    "same_bar_policy": "STOP_BEFORE_TP",
                    "entry_timing": "next_open",
                    **_safety(),
                })
    rows.sort(key=lambda r: ((r["test_metrics"].get("net_EV_lower_bound") or -9),
                             (r["test_metrics"].get("net_EV") or -9)), reverse=True)
    best = rows[0] if rows else None
    summary = {
        "tool_version": TOOL_VERSION,
        "ran_at": _now(),
        "symbols": [s.strip().upper() for s in str(symbols).split(",") if s.strip()],
        "data_source": data_source,
        "alpha_source": "alpha_factory_v10_44.json" if _load_alpha() else "generated_inline",
        "variants_tested": len(rows),
        "best_exit": best,
        "status_counts": {s: sum(1 for r in rows if r["status"] == s)
                          for s in ("EXIT_WATCH_ONLY", "EXIT_IMPROVES_RESEARCH_ONLY",
                                    "EXIT_NEEDS_MORE_DATA", "EXIT_REJECTED")},
        "overall_verdict": _overall(rows),
        "reports_dir": str(_out()).replace("\\", "/"),
        **_safety(),
    }
    if write_reports:
        _write(summary, rows)
    return summary


def _status(test_m: dict, val_m: dict, source_candidate: dict) -> str:
    if (test_m.get("valid_trades") or 0) < AF.MIN_TEST_SIGNALS:
        return "EXIT_NEEDS_MORE_DATA"
    if (test_m.get("net_EV") or 0) <= 0 or (test_m.get("profit_factor") or 0) <= 1:
        return "EXIT_REJECTED"
    base = (source_candidate.get("metrics_test") or {}).get("net_EV")
    if base is not None and (test_m.get("net_EV") or 0) > float(base):
        if (val_m.get("net_EV") or 0) > 0 and (test_m.get("net_EV_lower_bound") or 0) > 0:
            return "EXIT_IMPROVES_RESEARCH_ONLY"
    return "EXIT_WATCH_ONLY"


def _overall(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "NEED_DATA"
    if any(r["status"] == "EXIT_IMPROVES_RESEARCH_ONLY" for r in rows):
        return "EXIT_RESEARCH_IMPROVEMENT_NOT_ACTIONABLE"
    if any(r["status"] == "EXIT_WATCH_ONLY" for r in rows):
        return "EXIT_WATCH_ONLY"
    if any(r["status"] == "EXIT_NEEDS_MORE_DATA" for r in rows):
        return "EXIT_NEEDS_MORE_DATA"
    return "NO_EXIT_EDGE_ALL_REJECTED"


def _write(summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    out = _out()
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "exit_factory_v10_44.json.tmp"
    tmp.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, out / "exit_factory_v10_44.json")
    with open(out / "exit_factory_v10_44.csv", "w", newline="", encoding="utf-8") as fh:
        fields = ["candidate_id", "symbol", "strategy_name", "side", "exit_policy",
                  "status", "test_net_EV", "test_lb", "test_pf", "test_trades",
                  "validation_net_EV"]
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            tm = r["test_metrics"]
            vm = r["validation_metrics"]
            w.writerow({"candidate_id": r["candidate_id"], "symbol": r["symbol"],
                        "strategy_name": r["strategy_name"], "side": r["side"],
                        "exit_policy": r["exit_policy"], "status": r["status"],
                        "test_net_EV": tm.get("net_EV"),
                        "test_lb": tm.get("net_EV_lower_bound"),
                        "test_pf": tm.get("profit_factor"),
                        "test_trades": tm.get("valid_trades"),
                        "validation_net_EV": vm.get("net_EV")})
    (out / "exit_factory_v10_44.md").write_text(_memo(summary), encoding="utf-8")


def _memo(summary: dict[str, Any]) -> str:
    lines = ["# V10.44 Exit Factory", "", f"- verdict: {summary.get('overall_verdict')}",
             f"- variants_tested: {summary.get('variants_tested')}",
             f"- final_recommendation: {FINAL_RECOMMENDATION_NO_LIVE}", ""]
    best = summary.get("best_exit") or {}
    if best:
        tm = best.get("test_metrics") or {}
        lines.append(f"- best: {best.get('candidate_id')} / {best.get('exit_policy')} status={best.get('status')} EV={tm.get('net_EV')} lb={tm.get('net_EV_lower_bound')}")
    else:
        lines.append("- best: NONE")
    lines.append("")
    lines.append("Research only. Exit findings are not runtime policy. NO LIVE.")
    return "\n".join(lines) + "\n"


def render_cli(summary: dict[str, Any]) -> str:
    best = summary.get("best_exit") or {}
    tm = best.get("test_metrics") or {}
    lines = ["EXIT FACTORY V10.44 START",
             f"overall_verdict: {summary.get('overall_verdict')}",
             f"variants_tested: {summary.get('variants_tested')}",
             f"best_candidate: {best.get('candidate_id') or 'NONE'}",
             f"best_exit_policy: {best.get('exit_policy') or 'NONE'}",
             f"best_exit_status: {best.get('status') or 'NONE'}",
             f"best_test_net_EV: {tm.get('net_EV')}",
             f"best_test_net_EV_lower_bound: {tm.get('net_EV_lower_bound')}",
             f"reports_dir: {summary.get('reports_dir')}",
             "research_only: true",
             "paper_filter_enabled: false",
             "can_send_real_orders: false",
             "paper_ready: false",
             "live_ready: false",
             "final_recommendation: NO LIVE",
             "EXIT FACTORY V10.44 END"]
    return "\n".join(lines)
