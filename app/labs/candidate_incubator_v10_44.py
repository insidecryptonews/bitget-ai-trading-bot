"""ResearchOps V10.44 - Candidate Incubator (research only).

Aggregates Alpha Factory and Exit Factory outputs into a fail-closed research
queue. It never writes policy registry entries, never enables paper filter and
never emits executable signals.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import alpha_factory_v10_44 as AF
from . import exit_factory_v10_44 as EF

TOOL_VERSION = "v10.44"
OUTPUT_SUBDIR = ("reports", "research", "v10_44_alpha_sprint")
ALLOWED_STATES = ("REJECTED", "NEEDS_MORE_DATA", "WATCH_ONLY", "INCUBATE",
                  "PAPER_CANDIDATE_RESEARCH_ONLY")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "not_actionable": True, "no_orders": True,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _out() -> Path:
    return AF.CE._repo_root().joinpath(*OUTPUT_SUBDIR)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(name: str) -> dict[str, Any] | None:
    try:
        return json.loads((_out() / name).read_text(encoding="utf-8"))
    except Exception:
        return None


def run_incubator(symbols: str = "BTCUSDT", data_source: str = "ws_persistent",
                  write_reports: bool = True) -> dict[str, Any]:
    alpha = _read("alpha_factory_v10_44.json")
    if alpha is None:
        alpha = AF.run_alpha_factory(symbols=symbols, data_source=data_source,
                                     max_runtime_minutes=10, write_reports=write_reports)
    exits = _read("exit_factory_v10_44.json")
    if exits is None:
        exits = EF.run_exit_factory(symbols=symbols, data_source=data_source,
                                    write_reports=write_reports)
    exit_by_candidate = {}
    best_exit = exits.get("best_exit") if isinstance(exits, dict) else None
    if best_exit:
        exit_by_candidate[best_exit.get("candidate_id")] = best_exit
    records: list[dict[str, Any]] = []
    for c in alpha.get("top_candidates") or []:
        state = _incubation_state(c, exit_by_candidate.get(c.get("candidate_id")))
        if state not in ALLOWED_STATES:
            state = "REJECTED"
        records.append({
            "candidate_id": c.get("candidate_id"),
            "symbol": c.get("symbol"),
            "strategy_name": c.get("strategy_name"),
            "side": c.get("side"),
            "alpha_status": c.get("status"),
            "exit_status": (exit_by_candidate.get(c.get("candidate_id")) or {}).get("status"),
            "incubator_state": state,
            "rank_score": _rank_score(c, exit_by_candidate.get(c.get("candidate_id"))),
            "blockers": _blockers(c, exit_by_candidate.get(c.get("candidate_id"))),
            "next_action": _next_action(state),
            "activation": "disabled",
            **_safety(),
        })
    records.sort(key=lambda r: (r["incubator_state"] in ("PAPER_CANDIDATE_RESEARCH_ONLY", "INCUBATE"),
                                r["rank_score"]), reverse=True)
    counts = {s: sum(1 for r in records if r["incubator_state"] == s) for s in ALLOWED_STATES}
    summary = {"tool_version": TOOL_VERSION, "ran_at": _now(),
               "symbols": [s.strip().upper() for s in str(symbols).split(",") if s.strip()],
               "data_source": data_source,
               "records": records,
               "state_counts": counts,
               "best_research_candidate": records[0] if records else None,
               "overall_verdict": _overall(counts),
               "activation": "disabled",
               "reports_dir": str(_out()).replace("\\", "/"),
               **_safety()}
    if write_reports:
        _write(summary)
    return summary


def _incubation_state(c: dict[str, Any], ex: dict[str, Any] | None) -> str:
    status = c.get("status")
    blockers = c.get("blockers") or []
    test_ev = c.get("metrics_test", {}).get("net_EV")
    if status == "REJECTED" and (
        "test_net_ev_not_positive" in blockers
        or "test_pf_too_low" in blockers
        or (test_ev is not None and float(test_ev) <= 0)
    ):
        return "REJECTED"
    if status == "PAPER_CANDIDATE_RESEARCH_ONLY":
        # Still only a research label; require exit not to contradict it.
        if ex and ex.get("status") in ("EXIT_REJECTED", "EXIT_NEEDS_MORE_DATA"):
            return "INCUBATE"
        return "PAPER_CANDIDATE_RESEARCH_ONLY"
    if status == "INCUBATE":
        return "INCUBATE"
    if status == "WATCH_ONLY" or (status == "REJECTED" and (test_ev or 0) > 0):
        return "WATCH_ONLY"
    if status == "NEEDS_MORE_DATA" or any("sample" in str(b) for b in blockers):
        return "NEEDS_MORE_DATA"
    return "REJECTED"


def _rank_score(c: dict[str, Any], ex: dict[str, Any] | None) -> int:
    score = int(c.get("score") or 0)
    if ex and ex.get("status") == "EXIT_IMPROVES_RESEARCH_ONLY":
        score += 10
    if c.get("status") == "PAPER_CANDIDATE_RESEARCH_ONLY":
        score += 10
    return max(0, min(100, score))


def _blockers(c: dict[str, Any], ex: dict[str, Any] | None) -> list[str]:
    blockers = list(c.get("blockers") or [])
    blockers.extend(["manual_review_required", "paper_filter_enabled=false",
                     "live_disabled", "shadow_forward_not_started"])
    if ex and ex.get("status") in ("EXIT_REJECTED", "EXIT_NEEDS_MORE_DATA"):
        blockers.append(f"exit_status={ex.get('status')}")
    return sorted(set(blockers))


def _next_action(state: str) -> str:
    if state == "PAPER_CANDIDATE_RESEARCH_ONLY":
        return "manual_review_then_shadow_forward_only"
    if state == "INCUBATE":
        return "collect_more_ws_data_and_retest"
    if state == "WATCH_ONLY":
        return "watch_only_no_activation"
    if state == "NEEDS_MORE_DATA":
        return "need_more_contiguous_sample"
    return "reject_or_redesign_hypothesis"


def _overall(counts: dict[str, int]) -> str:
    if counts.get("PAPER_CANDIDATE_RESEARCH_ONLY", 0):
        return "PAPER_CANDIDATE_RESEARCH_ONLY_BLOCKED_MANUAL_REVIEW"
    if counts.get("INCUBATE", 0):
        return "INCUBATE_RESEARCH_ONLY"
    if counts.get("WATCH_ONLY", 0):
        return "WATCH_ONLY"
    if counts.get("NEEDS_MORE_DATA", 0):
        return "NEEDS_MORE_DATA"
    return "NO_CANDIDATE_READY"


def _write(summary: dict[str, Any]) -> None:
    out = _out()
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "candidate_incubator_v10_44.json.tmp"
    tmp.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, out / "candidate_incubator_v10_44.json")
    (out / "candidate_incubator_v10_44.md").write_text(_memo(summary), encoding="utf-8")


def _memo(summary: dict[str, Any]) -> str:
    lines = ["# V10.44 Candidate Incubator", "",
             f"- verdict: {summary.get('overall_verdict')}",
             f"- final_recommendation: {FINAL_RECOMMENDATION_NO_LIVE}",
             "", "## Candidates"]
    for r in summary.get("records", [])[:12]:
        lines.append(f"- {r['candidate_id']}: {r['incubator_state']} score={r['rank_score']} next={r['next_action']}")
    if not summary.get("records"):
        lines.append("- NONE")
    lines.extend(["", "Research only. No auto-promotion. NO LIVE."])
    return "\n".join(lines) + "\n"


def render_cli(summary: dict[str, Any]) -> str:
    best = summary.get("best_research_candidate") or {}
    lines = ["CANDIDATE INCUBATOR V10.44 START",
             f"overall_verdict: {summary.get('overall_verdict')}",
             f"state_counts: {json.dumps(summary.get('state_counts') or {}, default=str)}",
             f"best_candidate: {best.get('candidate_id') or 'NONE'}",
             f"best_state: {best.get('incubator_state') or 'NONE'}",
             f"best_next_action: {best.get('next_action') or 'NONE'}",
             f"reports_dir: {summary.get('reports_dir')}",
             "activation: disabled",
             "research_only: true",
             "paper_filter_enabled: false",
             "can_send_real_orders: false",
             "paper_ready: false",
             "live_ready: false",
             "final_recommendation: NO LIVE",
             "CANDIDATE INCUBATOR V10.44 END"]
    return "\n".join(lines)
