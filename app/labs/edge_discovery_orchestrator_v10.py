"""ResearchOps V10 — Edge Discovery Orchestrator (research-only).

Unites the V10 research families and the existing data-reliability
signals into a single prioritized verdict: which family to investigate
first, what data is missing, and what blocks progress. It NEVER returns a
paper- or live-ready status.

HARD CONTRACT — research only:

- never opens orders, never calls private endpoints, never writes DB,
- ``live_ready`` and ``paper_ready`` are ALWAYS False,
- hard blockers (BAD data quality, stale OHLCV, insufficient clean N,
  negative net EV, low net PF, market_probe contamination, proxy-only,
  single-symbol / single-event dominance, ``clock_drift`` UNKNOWN for
  pre-live, active path counted as real) force a family down,
- the ceiling for any family is research prioritization, never promotion.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE

# Orchestrator family statuses (ordered best -> worst).
FAM_IMPLEMENT_FIRST = "IMPLEMENT_FIRST_RESEARCH"
FAM_INVESTIGATE_MORE = "INVESTIGATE_MORE"
FAM_WATCH_ONLY = "WATCH_ONLY"
FAM_FEATURE_ONLY = "FEATURE_ONLY"
FAM_NEED_DATA = "NEED_DATA"
FAM_NOT_CORE = "NOT_CORE"
FAM_REJECT = "REJECT"

FAMILY_RANK = {
    FAM_IMPLEMENT_FIRST: 6,
    FAM_INVESTIGATE_MORE: 5,
    FAM_WATCH_ONLY: 4,
    FAM_FEATURE_ONLY: 3,
    FAM_NEED_DATA: 2,
    FAM_NOT_CORE: 1,
    FAM_REJECT: 0,
}

# Map each lab's native decision string to an orchestrator family status.
_DECISION_TO_FAMILY = {
    "IMPLEMENT_FIRST_RESEARCH": FAM_IMPLEMENT_FIRST,
    "RESEARCH_POCKET": FAM_INVESTIGATE_MORE,
    "WATCH_ONLY": FAM_WATCH_ONLY,
    "SHADOW_RESEARCH_ONLY": FAM_FEATURE_ONLY,
    "NEED_DATA": FAM_NEED_DATA,
    "NEED_MORE_DATA": FAM_NEED_DATA,
    "NOT_CORE": FAM_NOT_CORE,
    "AUDIT_ONLY_NOT_PROMOTABLE": FAM_NOT_CORE,
    "REJECT": FAM_REJECT,
    "REJECT_COSTS_TOO_HIGH": FAM_REJECT,
}

MIN_CLEAN_N = 40


@dataclass
class FamilyVerdict:
    family_id: str = ""
    title: str = ""
    native_decision: str = ""
    family_status: str = FAM_NEED_DATA
    required_data: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EdgeDiscoveryOrchestratorReport:
    hours: int = 24
    generated_at: str = ""
    families: list[dict[str, Any]] = field(default_factory=list)
    best_family: str = ""
    best_family_status: str = FAM_NEED_DATA
    best_next_experiment: str = ""
    rejected_families: list[str] = field(default_factory=list)
    required_data: list[str] = field(default_factory=list)
    global_blockers: list[str] = field(default_factory=list)
    next_action: str = ""
    # Hard invariants — never flipped by this lab.
    clock_drift_status: str = "UNKNOWN"
    pre_live_clock_gate: str = "BLOCKED_CLOCK_DRIFT_UNKNOWN"
    live_ready: bool = False
    paper_ready: bool = False
    shadow_ready: bool = False
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _family_from_decision(decision: str) -> str:
    return _DECISION_TO_FAMILY.get(str(decision or "").strip().upper(), FAM_NEED_DATA)


def aggregate_edge_families(
    lab_reports: dict[str, dict[str, Any]],
    *,
    hours: int = 24,
    clock_drift_status: str = "UNKNOWN",
    clean_n: int = 0,
    data_quality_status: str = "NEED_DATA",
    ohlcv_freshness: str = "UNKNOWN",
    market_probe_contaminated: bool = False,
    active_counted_as_real: bool = False,
    proxy_only: bool = False,
    timeout: bool = False,
) -> EdgeDiscoveryOrchestratorReport:
    """Pure aggregation. ``lab_reports`` maps a family id to a dict with at
    least ``decision``; optional ``blockers`` / ``required_data_missing`` /
    ``concentration`` / ``net_ev_pct`` / ``net_pf`` are honoured."""
    report = EdgeDiscoveryOrchestratorReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )

    clock = str(clock_drift_status or "UNKNOWN").upper()
    report.clock_drift_status = clock
    report.pre_live_clock_gate = "OK" if clock == "OK" else f"BLOCKED_CLOCK_DRIFT_{clock}"

    # Global hard blockers (do not depend on a single family).
    global_blockers: list[str] = []
    if str(data_quality_status or "").upper() == "BAD":
        global_blockers.append("data_quality_bad")
    if str(ohlcv_freshness or "").upper() == "STALE":
        global_blockers.append("ohlcv_stale")
    if int(clean_n or 0) < MIN_CLEAN_N:
        global_blockers.append("clean_n_insufficient")
    if market_probe_contaminated:
        global_blockers.append("market_probe_contamination")
    if active_counted_as_real:
        global_blockers.append("active_path_counted_as_real")
    if proxy_only:
        global_blockers.append("proxy_only_outcomes")
    if timeout:
        global_blockers.append("report_timeout")
    if clock != "OK":
        global_blockers.append("clock_drift_not_ok_pre_live_blocked")
    report.global_blockers = global_blockers

    titles = {
        "funding_oi_liquidation": "Funding / OI / Liquidation (leverage stress)",
        "token_unlock_post_listing": "Token unlock / high-FDV / post-listing shorts",
        "intraday_volatility_breakdown": "Intraday volatility breakdown",
        "micro_tp": "Micro-TP viability",
        "event_catalyst": "Event catalyst layer",
    }
    # event_catalyst is an overlay/feature, never a standalone family.
    feature_only_families = {"event_catalyst"}

    verdicts: list[FamilyVerdict] = []
    for fam_id, rep in lab_reports.items():
        decision = str(rep.get("decision") or "")
        fam_status = _family_from_decision(decision)
        if fam_id in feature_only_families and fam_status not in (FAM_NEED_DATA, FAM_REJECT):
            fam_status = FAM_FEATURE_ONLY

        blockers = list(rep.get("blockers") or [])
        required = list(rep.get("required_data_missing") or [])

        # Per-family demotions from hard data conditions.
        net_ev = rep.get("net_ev_pct")
        net_pf = rep.get("net_pf")
        conc = rep.get("concentration")
        if isinstance(net_ev, (int, float)) and not isinstance(net_ev, bool) and net_ev <= 0:
            if fam_status in (FAM_IMPLEMENT_FIRST, FAM_INVESTIGATE_MORE, FAM_WATCH_ONLY):
                fam_status = FAM_REJECT
                blockers.append("net_ev_non_positive")
        if isinstance(net_pf, (int, float)) and not isinstance(net_pf, bool) and 0 < net_pf < 1.2:
            if fam_status in (FAM_IMPLEMENT_FIRST, FAM_INVESTIGATE_MORE):
                fam_status = FAM_WATCH_ONLY
                blockers.append("net_pf_low")
        if isinstance(conc, (int, float)) and not isinstance(conc, bool) and conc > 0.70:
            if fam_status in (FAM_IMPLEMENT_FIRST, FAM_INVESTIGATE_MORE):
                fam_status = FAM_WATCH_ONLY
                blockers.append("single_symbol_dominance")

        # Global blockers cap every family at NEED_DATA at most.
        if global_blockers and fam_status in (FAM_IMPLEMENT_FIRST, FAM_INVESTIGATE_MORE, FAM_WATCH_ONLY, FAM_FEATURE_ONLY):
            fam_status = FAM_NEED_DATA
            blockers = list(dict.fromkeys(blockers + ["global_blocker_present"]))

        verdicts.append(FamilyVerdict(
            family_id=fam_id,
            title=titles.get(fam_id, fam_id),
            native_decision=decision,
            family_status=fam_status,
            required_data=required,
            blockers=blockers,
        ))

    verdicts.sort(key=lambda v: FAMILY_RANK.get(v.family_status, 0), reverse=True)
    report.families = [v.as_dict() for v in verdicts]
    report.rejected_families = [v.family_id for v in verdicts if v.family_status == FAM_REJECT]
    # Aggregate required data across families that still need it.
    req: list[str] = []
    for v in verdicts:
        if v.family_status in (FAM_NEED_DATA, FAM_IMPLEMENT_FIRST, FAM_INVESTIGATE_MORE):
            for d in v.required_data:
                if d not in req:
                    req.append(d)
    report.required_data = req

    if verdicts:
        best = verdicts[0]
        report.best_family = best.family_id
        report.best_family_status = best.family_status
        if best.family_status == FAM_IMPLEMENT_FIRST:
            report.best_next_experiment = f"event_study::{best.family_id}"
            report.next_action = f"Design read-only event study for {best.title}"
        elif best.family_status == FAM_INVESTIGATE_MORE:
            report.best_next_experiment = f"deepen_backtest::{best.family_id}"
            report.next_action = f"Expand no-lookahead backtest for {best.title}"
        elif best.family_status == FAM_NEED_DATA:
            report.best_next_experiment = "collect_external_data"
            report.next_action = "Collect missing external/clean data; keep running PAPER/RESEARCH"
        else:
            report.best_next_experiment = "keep_collecting"
            report.next_action = "No family ready; keep collecting clean research data"
    else:
        report.best_family = ""
        report.best_family_status = FAM_NEED_DATA
        report.next_action = "No labs produced output; collect data"

    # Shadow readiness: only if a family is INVESTIGATE_MORE+ AND there are
    # zero global blockers AND clock gate OK. Even then this is research
    # shadow design, not promotion — and paper/live remain hard-False.
    report.shadow_ready = bool(
        not global_blockers
        and report.pre_live_clock_gate == "OK"
        and FAMILY_RANK.get(report.best_family_status, 0) >= FAMILY_RANK[FAM_INVESTIGATE_MORE]
    )
    report.live_ready = False
    report.paper_ready = False
    return report


def run_edge_discovery_orchestrator(
    db: Any = None,
    *,
    hours: int = 24,
    external_data_path: str | None = None,
    clock_drift_status: str = "UNKNOWN",
    clean_n: int = 0,
    data_quality_status: str = "NEED_DATA",
    ohlcv_freshness: str = "UNKNOWN",
    symbols: list[str] | None = None,
    timeframe: str = "5m",
    run_volatility: bool = False,
) -> EdgeDiscoveryOrchestratorReport:
    """Build the V10 lab reports (research-only) and aggregate them.

    By default the heavy OHLCV volatility backtest is skipped
    (``run_volatility=False``) to keep the orchestrator light; pass
    ``run_volatility=True`` to include it.
    """
    from .event_catalyst_layer_v10 import run_event_catalyst_layer
    from .funding_oi_liquidation_research_v10 import (
        run_funding_oi_liquidation_research,
    )
    from .micro_tp_viability_v10 import run_micro_tp_viability
    from .token_unlock_post_listing_research_v10 import (
        run_unlock_post_listing_research,
    )

    lab_reports: dict[str, dict[str, Any]] = {}

    def _safe(fn) -> dict[str, Any]:
        try:
            return fn().as_dict()
        except Exception as exc:  # never let one lab break the orchestrator
            return {"decision": "NEED_DATA", "blockers": [f"lab_error:{type(exc).__name__}"]}

    lab_reports["funding_oi_liquidation"] = _safe(
        lambda: run_funding_oi_liquidation_research(hours=hours, external_data_path=external_data_path)
    )
    lab_reports["token_unlock_post_listing"] = _safe(
        lambda: run_unlock_post_listing_research(hours=max(hours, 720), external_data_path=external_data_path)
    )
    lab_reports["micro_tp"] = _safe(lambda: run_micro_tp_viability(hours=hours))
    lab_reports["event_catalyst"] = _safe(
        lambda: run_event_catalyst_layer(hours=max(hours, 720), external_data_path=external_data_path)
    )

    if run_volatility and db is not None:
        from .intraday_volatility_breakdown_v10 import (
            run_intraday_volatility_breakdown,
        )
        try:
            vol = run_intraday_volatility_breakdown(
                db, symbols=symbols, timeframe=timeframe, hours=hours,
            )
            vd = vol.as_dict()
            best_rule = vol.best_rule or {}
            lab_reports["intraday_volatility_breakdown"] = {
                "decision": vd.get("decision"),
                "blockers": vd.get("blockers") or [],
                "net_ev_pct": best_rule.get("net_ev_pct"),
                "net_pf": best_rule.get("net_pf"),
                "concentration": best_rule.get("concentration"),
            }
            if vd.get("freshness_status") == "STALE":
                ohlcv_freshness = "STALE"
        except Exception as exc:
            lab_reports["intraday_volatility_breakdown"] = {
                "decision": "NEED_MORE_DATA", "blockers": [f"lab_error:{type(exc).__name__}"],
            }
    else:
        lab_reports["intraday_volatility_breakdown"] = {
            "decision": "NEED_MORE_DATA", "blockers": ["volatility_backtest_not_run"],
        }

    return aggregate_edge_families(
        lab_reports,
        hours=hours,
        clock_drift_status=clock_drift_status,
        clean_n=clean_n,
        data_quality_status=data_quality_status,
        ohlcv_freshness=ohlcv_freshness,
    )
