"""ResearchOps V10.43C - Strategy Lab hardening (research only, NO LIVE).

Wraps the V10.43B Autonomous Strategy Lab and makes its verdicts more honest and
more useful WITHOUT overfitting or changing the (conservative) evaluation:

  * distinguishes ALL_NEEDS_MORE_DATA (nothing had enough sample) from
    NO_EDGE_ALL_REJECTED (things had sample and failed on merit);
  * normalizes each rejection into an explicit taxonomy
    (INSUFFICIENT_SAMPLE / NEGATIVE_NET_EV / LOWER_BOUND_NOT_POSITIVE /
     COST_DOMINATED / SLIPPAGE_SENSITIVE / BASELINE_NOT_BEATEN /
     DRAWDOWN_EXCESSIVE / DATA_GAP / OVERFIT_RISK / UNSTABLE_ACROSS_SPLITS);
  * runs a small, bounded sensitivity/ablation on the top candidate (base costs,
    costs +50%, shorter/longer horizon) — never a brute-force sweep;
  * still ranks by net_EV_lower_bound (never win-rate) and never promotes on a
    small sample.

Reuses the V10.43B lab primitives; adds NO new strategy families. NO LIVE.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE
from . import shadow_simulation_tournament_v10_40 as SH
from . import autonomous_strategy_lab_v10_43b as LAB

TOOL_VERSION = "v10.43c"
OUTPUT_SUBDIR = ("reports", "research", "strategy_lab_v10_43c")

GLOBAL_VERDICTS = ("STRATEGIES_UNDER_RESEARCH", "ALL_NEEDS_MORE_DATA",
                   "NO_EDGE_ALL_REJECTED", "INSUFFICIENT_SAMPLE", "NO_WS_DATA")
REASON_TAXONOMY = ("INSUFFICIENT_SAMPLE", "NEGATIVE_NET_EV", "LOWER_BOUND_NOT_POSITIVE",
                   "COST_DOMINATED", "SLIPPAGE_SENSITIVE", "BASELINE_NOT_BEATEN",
                   "DRAWDOWN_EXCESSIVE", "DATA_GAP", "OVERFIT_RISK",
                   "UNSTABLE_ACROSS_SPLITS", "PROMOTED", "OTHER")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "not_actionable": True, "no_orders": True,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _normalize_reason(c: dict) -> str:
    v = (c.get("verdict") or "").upper()
    if v in ("INCUBATE", "SHADOW_FORWARD_CANDIDATE"):
        return "PROMOTED"
    if v == "NEEDS_MORE_DATA":
        return "INSUFFICIENT_SAMPLE"
    r = (c.get("rejection_reason") or "").lower()
    if "drawdown" in r:
        return "DRAWDOWN_EXCESSIVE"
    if "lower_bound" in r:
        return "LOWER_BOUND_NOT_POSITIVE"
    if "baseline" in r:
        return "BASELINE_NOT_BEATEN"
    if "slippage" in r:
        return "SLIPPAGE_SENSITIVE"
    if "cost-dominated" in r or "cost_dominated" in r:
        return "COST_DOMINATED"
    if "net_ev<=0" in r or "net_ev" in r:
        return "NEGATIVE_NET_EV"
    return "OTHER"


def _global_verdict(base: dict) -> str:
    generated = base.get("candidates_generated") or 0
    if generated == 0:
        v = base.get("verdict")
        return v if v in GLOBAL_VERDICTS else "INSUFFICIENT_SAMPLE"
    counts = base.get("verdict_counts") or {}
    promoted = (counts.get("WATCHLIST", 0) + counts.get("INCUBATE", 0)
                + counts.get("SHADOW_FORWARD_CANDIDATE", 0))
    nmd = counts.get("NEEDS_MORE_DATA", 0)
    if promoted > 0:
        return "STRATEGIES_UNDER_RESEARCH"
    if nmd == generated:
        return "ALL_NEEDS_MORE_DATA"
    return "NO_EDGE_ALL_REJECTED"


def _sensitivity(symbol: str, data_source: str, best_name: str | None) -> dict[str, Any]:
    """Bounded ablation on the single best candidate: base costs, costs +50%,
    half horizon, double horizon. Reports whether the (non-)edge is stable — it
    is NOT a parameter sweep and it never promotes anything."""
    if not best_name:
        return {"status": "NO_BEST_CANDIDATE"}
    bars, eff_source, _ = LAB._load_bars(symbol, data_source)
    if len(bars) < 3 * LAB.MIN_SAMPLE:
        return {"status": "INSUFFICIENT_SAMPLE", "n_bars": len(bars)}
    strat = next((s for s in LAB._strategy_universe() if s["name"] == best_name), None)
    if strat is None:
        return {"status": "STRATEGY_NOT_FOUND"}
    feats = CE.build_features(bars)
    split = int(len(feats) * 0.6)
    thr = SH._train_thresholds(feats, split) if hasattr(SH, "_train_thresholds") \
        else LAB._thresholds(feats, split)
    dear = {"fee_bps": CE.DEFAULT_COSTS["fee_bps"] * LAB.SLIP_STRESS_MULT,
            "slippage_bps": CE.DEFAULT_COSTS["slippage_bps"] * LAB.SLIP_STRESS_MULT,
            "spread_bps": CE.DEFAULT_COSTS["spread_bps"]}

    def ev(costs=None, hz=None):
        s2 = dict(strat)
        if hz:
            s2["horizon"] = hz
        return LAB._eval_one(s2, feats, bars, split, thr, costs=costs).get("net_EV")

    settings = {
        "base": ev(),
        "costs_plus_50pct": ev(costs=dear),
        "horizon_half": ev(hz=max(5, strat["horizon"] // 2)),
        "horizon_double": ev(hz=strat["horizon"] * 2)}
    vals = [v for v in settings.values() if v is not None]
    robust_positive = bool(vals) and all(v > 0 for v in vals)
    signs = {1 if v > 0 else -1 for v in vals}
    return {"status": "OK", "best_candidate": best_name, "n_bars": len(bars),
            "effective_source": eff_source, "net_EV_by_setting": settings,
            "sign_stable": len(signs) <= 1, "robust_positive": robust_positive,
            "verdict": ("ROBUST_POSITIVE_SIM" if robust_positive else
                        "NOT_ROBUST_STILL_NO_EDGE")}


def run_hardened_lab(symbol: str = "BTCUSDT", data_source: str = "ws_persistent",
                     write_reports: bool = True) -> dict[str, Any]:
    base = LAB.run_lab(symbol, data_source=data_source, write_reports=False,
                       include_candidates=True)
    cands = base.get("candidates_detail", []) or []
    for c in cands:
        c["rejection_category"] = _normalize_reason(c)
    gv = _global_verdict(base)
    best_name = (cands[0]["strategy_name"] if cands else None)
    sens = _sensitivity(symbol, data_source, best_name)
    cat_counts: dict[str, int] = {}
    for c in cands:
        cat_counts[c["rejection_category"]] = cat_counts.get(c["rejection_category"], 0) + 1
    summary = {"tool_version": TOOL_VERSION, "symbol": symbol,
               "requested_source": data_source,
               "effective_source": base.get("effective_source"),
               "ran_at": datetime.now(timezone.utc).isoformat(),
               "n_bars": base.get("n_bars"),
               "candidates_generated": base.get("candidates_generated", 0),
               "verdict_counts": base.get("verdict_counts"),
               "rejection_category_counts": cat_counts,
               "watchlist_or_better": base.get("watchlist_or_better", 0),
               "best": base.get("best"),
               "best_net_EV_lower_bound": base.get("best_net_EV_lower_bound"),
               "sensitivity": sens,
               "ranking_key": "net_EV_lower_bound (win_rate secondary)",
               "global_verdict": gv,
               "note": base.get("note"),
               **_safety()}
    if write_reports:
        summary["reports_dir"] = _write(summary, cands)
    return summary


def _write(summary: dict, cands: list[dict]) -> str:
    d = CE._repo_root().joinpath(*OUTPUT_SUBDIR)
    d.mkdir(parents=True, exist_ok=True)
    cols = ["strategy_name", "family", "side", "sample_size", "net_EV",
            "net_EV_lower_bound", "profit_factor", "win_rate", "max_drawdown",
            "cost_sensitivity", "slippage_stress_lb", "baseline_comparison",
            "verdict", "rejection_reason", "rejection_category"]
    with open(d / "strategy_scoreboard_v1043c.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for c in cands:
            w.writerow({k: c.get(k) for k in cols})
    _wj(d / "strategy_rejection_reasons_v1043c.json",
        {"global_verdict": summary.get("global_verdict"),
         "rejection_category_counts": summary.get("rejection_category_counts"),
         "verdict_counts": summary.get("verdict_counts"),
         "sensitivity": summary.get("sensitivity"), **_safety()})
    (d / "strategy_research_memo_v1043c.md").write_text(_memo(summary, cands),
                                                        encoding="utf-8")
    return str(d).replace("\\", "/")


def _wj(path, obj) -> None:
    tmp = str(path) + ".tmp"
    from pathlib import Path
    Path(tmp).write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _memo(summary: dict, cands: list[dict]) -> str:
    lines = ["# Strategy Lab Hardening Memo (V10.43C) — RESEARCH ONLY, NO LIVE", "",
             f"source: {summary.get('effective_source')} · bars: {summary.get('n_bars')} · "
             f"candidates: {summary.get('candidates_generated')}",
             f"**global_verdict: {summary.get('global_verdict')}** "
             f"(ALL_NEEDS_MORE_DATA ≠ NO_EDGE_ALL_REJECTED)", "",
             f"rejection categories: {summary.get('rejection_category_counts')}", "",
             "## Sensitivity / ablation (top candidate, bounded)", ""]
    s = summary.get("sensitivity") or {}
    if s.get("status") == "OK":
        lines.append(f"- best_candidate: {s.get('best_candidate')}")
        lines.append(f"- net_EV_by_setting: {s.get('net_EV_by_setting')}")
        lines.append(f"- sign_stable: {s.get('sign_stable')} · "
                     f"robust_positive: {s.get('robust_positive')} · verdict: {s.get('verdict')}")
    else:
        lines.append(f"- sensitivity: {s.get('status')}")
    lines += ["", "| strategy | verdict | category | n | net_EV | net_EV_lb |",
              "|---|---|---|---|---|---|"]
    for c in cands[:14]:
        lines.append(f"| {c.get('strategy_name')} | {c.get('verdict')} | "
                     f"{c.get('rejection_category')} | {c.get('sample_size')} | "
                     f"{c.get('net_EV')} | {c.get('net_EV_lower_bound')} |")
    lines += ["", "Ranking by net_EV_lower_bound (win rate secondary). "
              "Nothing actionable. **FINAL_RECOMMENDATION=NO LIVE.**"]
    return "\n".join(lines)
