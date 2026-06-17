"""ResearchOps V10.4 — Near-Real-Time Read-Only Trader Terminal.

Builds a read-only dashboard view-model and renders a self-contained dark
"cyber/trader terminal" HTML page that refreshes itself by polling ONE
read-only GET endpoint. It is READ-ONLY by construction:

- no order buttons / no "go live" action,
- no leverage / margin / sizing controls,
- no mutable endpoints, no POST/PUT/DELETE, no forms,
- the only JS network call is ``fetch`` (GET) to
  ``/api/researchops/v104/dashboard-state``,
- "future action" buttons render ``disabled`` with a lock tooltip and have
  no handlers and no backend,
- it cannot change flags, config, the DB, or trading behaviour.

Safety values are DERIVED from the real config flags (never invented). If any
flag were unsafe the terminal shows a red violation notice instead of
pretending everything is fine. Paper PnL is always labelled as paper/shadow.
"""

from __future__ import annotations

import html
import json
import math
from typing import Any

FINAL_RECOMMENDATION_NO_LIVE = "NO LIVE"
LOCK_TOOLTIP = "Locked: requires explicit human approval + audit + gates"
POLL_ENDPOINT = "/api/researchops/v104/dashboard-state"
DEFAULT_REFRESH_SECONDS = 7

# Future-action buttons — ALL disabled, no backend, no-op. The last three are
# deliberately-locked "anti-features": they exist only to make explicit that
# copy trading, leverage controls and casino mechanics will NEVER be enabled
# from this dashboard.
DISABLED_CONTROLS = [
    "Enable Live", "Enable Paper Filter", "Run Paid Download", "Promote Candidate",
    "Start Backtester Operational", "Re-ingest Data", "Replace Raw Data",
    "Copy Trading", "Leverage Control", "777 Spin / Casino Mode",
]

READONLY_API_ENDPOINTS = [
    "/api/researchops/v104/overview",
    "/api/researchops/v104/safety",
    "/api/researchops/v104/data-readiness",
    "/api/researchops/v104/provider-readiness",
    "/api/researchops/v104/provider-verification",
    "/api/researchops/v104/candidates",
    "/api/researchops/v104/net-edge",
    "/api/researchops/v104/paper-monitor",
    "/api/researchops/v104/signal-monitor",
    "/api/researchops/v104/learning",
    "/api/researchops/v104/dashboard-state",
]

HEAVY_PANEL_NOTE = (
    "Heavy research panels use cached/read-only snapshots; "
    "run CLI reports for refresh."
)


def _safe(d: dict | None, key: str, default: Any) -> Any:
    if not isinstance(d, dict):
        return default
    value = d.get(key, default)
    return default if value is None else value


def derive_worker_lock_view(raw_lock: Any) -> dict[str, Any]:
    """V10.4.3 — derive the worker-lock panel from the bot's OWN /health
    worker_lock payload (single source of truth). The dashboard must NEVER
    recompute the lock with a fresh WorkerLockManager: a new manager gets a
    new instance_id and falsely reports ``blocked_duplicate`` against the
    real worker. Unknown stays unknown — never invented."""
    if isinstance(raw_lock, dict) and raw_lock:
        status = str(raw_lock.get("lock_status") or "unknown")
        acquired = raw_lock.get("acquired")
        warning = str(raw_lock.get("warning_if_duplicate_worker") or "")
        if status == "unknown" and acquired is None:
            return {"worker_lock": "unknown", "worker_acquired": "unknown",
                    "duplicate_worker": "UNKNOWN"}
        duplicate = "YES" if (warning or status == "blocked_duplicate") else "NO"
        return {
            "worker_lock": status,
            "worker_acquired": bool(acquired) if acquired is not None else "unknown",
            "duplicate_worker": duplicate,
        }
    if isinstance(raw_lock, str) and raw_lock and raw_lock != "unknown":
        if raw_lock == "blocked_duplicate":
            return {"worker_lock": raw_lock, "worker_acquired": False,
                    "duplicate_worker": "YES"}
        acquired = True if raw_lock in ("heartbeat", "acquired") else "unknown"
        return {"worker_lock": raw_lock, "worker_acquired": acquired,
                "duplicate_worker": "NO"}
    return {"worker_lock": "unknown", "worker_acquired": "unknown",
            "duplicate_worker": "UNKNOWN"}


_MARKET_PROBE_FIELDS = ("source", "origin", "signal_type")
_MIN_CANDIDATE_SAMPLES = 150
_MIN_CANDIDATE_NET_PF = 1.0
_MAX_CANDIDATE_TIME = 0.80


def _pipeline_finite(value: Any) -> float | None:
    """Finite float or None. Never raises (pure, defensive)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _is_market_probe(row: dict[str, Any]) -> bool:
    for field_name in _MARKET_PROBE_FIELDS:
        if str(row.get(field_name) or "").strip().lower() == "market_probe":
            return True
    return False


def _alias_pair_ok(row: dict[str, Any], a: str, b: str) -> bool:
    """If both aliases are present they must agree (finite, equal ±1e-12)."""
    if a in row and b in row:
        fa, fb = _pipeline_finite(row.get(a)), _pipeline_finite(row.get(b))
        if fa is None or fb is None or abs(fa - fb) > 1e-12:
            return False
    return True


def strict_validated_candidate(row: Any) -> dict[str, Any] | None:
    """V10.5.4 (Codex A4/A5/A6) — return the row ONLY if it is a real,
    fully-evidenced candidate; otherwise None. Never trusts declarative
    counts. Pure, never raises."""
    if not isinstance(row, dict):
        return None
    cid = str(row.get("candidate_id") or row.get("policy_id")
              or row.get("group_value") or "").strip()
    if not cid:
        return None
    if _is_market_probe(row):
        return None
    decision = str(row.get("decision") or "").upper()
    if decision in ("REJECT", "BLOCK", "NO_VALID_CANDIDATE"):
        return None
    if not (_alias_pair_ok(row, "net_EV", "net_ev")
            and _alias_pair_ok(row, "net_PF", "net_pf")):
        return None
    net_ev = _pipeline_finite(row.get("net_EV", row.get("net_ev")))
    net_pf = _pipeline_finite(row.get("net_PF", row.get("net_pf")))
    samples = _pipeline_finite(row.get("samples", row.get("sample_count")))
    has_time = any(k in row for k in ("time_ratio", "TIME", "time_pct"))
    time_ratio = _pipeline_finite(row.get("time_ratio",
                                          row.get("TIME", row.get("time_pct"))))
    if net_ev is None or net_ev <= 0:
        return None
    if net_pf is None or net_pf < _MIN_CANDIDATE_NET_PF:
        return None
    if samples is None or samples < _MIN_CANDIDATE_SAMPLES:
        return None
    if not has_time or time_ratio is None or time_ratio >= _MAX_CANDIDATE_TIME:
        return None
    return {"candidate_id": cid, "net_EV": net_ev, "net_PF": net_pf,
            "samples": samples, "time_ratio": time_ratio}


def derive_pipeline_stages(
    *,
    safety: dict[str, Any] | None,
    candidates: dict[str, Any] | None,
    net_edge: dict[str, Any] | None,
    signal_monitor: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """V10.5.4 (Codex A4–A7) — pure pipeline derivation with NO declarative
    promotion. Counts (candidate_count / validated_top_candidates_count /
    status=OK) are metadata only and never produce PASS. EDGE GUARD PASS
    requires a real top_candidates row that survives strict revalidation;
    NET EV PASS requires a fresh net_edge row for the SAME candidate_id with
    finite net_EV>0 and net_PF>=1.0. market_probe never qualifies."""
    sf = dict(safety or {})
    cd = dict(candidates or {})
    sg = dict(signal_monitor or {})
    ne = dict(net_edge or {})

    cand_status = str(cd.get("status") or cd.get("data_status") or "")
    cand_fresh = cand_status not in ("", "STALE", "STALE_OR_PENDING", "ERROR_STALE")
    signals = len(sg.get("top_signals") or [])
    blocks = len(sg.get("top_blocks") or [])

    def stage(name: str, state: str, why: str) -> dict[str, str]:
        return {"name": name, "state": state, "why": why}

    # Real validated candidate from a row (never from a count).
    validated = None
    raw_claim = False
    for row in (cd.get("top_candidates") or []):
        if isinstance(row, dict):
            raw_claim = True
            v = strict_validated_candidate(row)
            if v is not None:
                validated = v
                break
    # A declarative count with no revalidable row is an unvalidated claim.
    if not raw_claim:
        for key in ("candidate_count", "validated_top_candidates_count",
                    "valid_candidates_count", "top_candidates_count"):
            try:
                if float(cd.get(key)) > 0:
                    raw_claim = True
                    break
            except (TypeError, ValueError):
                continue

    ne_status = str(ne.get("status") or ne.get("data_status") or "")
    ne_fresh = ne_status not in ("", "STALE", "STALE_OR_PENDING", "ERROR_STALE")

    def _net_edge_pass_for(cid: str) -> bool:
        """A fresh net_edge row for the SAME candidate_id, fully evidenced."""
        for row in (ne.get("top_candidates") or []):
            v = strict_validated_candidate(row)
            if v is not None and v["candidate_id"] == cid:
                return True
        return False

    scan = (stage("SCAN", "PASS", f"worker up {sf.get('uptime')}")
            if sf.get("uptime") else stage("SCAN", "STALE", "no runtime payload"))
    signal = (stage("SIGNAL", "PASS", f"{signals} signals in window")
              if signals > 0 else stage("SIGNAL", "STALE", "no recent signal snapshot"))

    if blocks > 0:
        guard = stage("EDGE GUARD", "BLOCKED", "low RR / quality gates blocking")
    elif cand_status == "NO_VALID_CANDIDATES":
        guard = stage("EDGE GUARD", "BLOCKED", "no valid candidates")
    elif not cand_fresh or signals == 0:
        guard = stage("EDGE GUARD", "STALE", "no fresh evidence; run CLI report")
    elif validated is not None:
        guard = stage("EDGE GUARD", "PASS",
                      f"validated candidate {validated['candidate_id']} in fresh snapshot")
    elif raw_claim:
        guard = stage("EDGE GUARD", "BLOCKED",
                      "candidate claim present but none survives revalidation")
    else:
        guard = stage("EDGE GUARD", "STALE",
                      "status OK but zero validated candidates — not evidence")

    edge_status = str(ne.get("edge_status") or "")
    if not cand_fresh:
        netev = stage("NET EV", "NEEDS_DATA", "no cached snapshot; run CLI")
        policy = stage("POLICY", "NEEDS_DATA", "awaiting candidate snapshot")
    elif (validated is not None and guard["state"] == "PASS"
          and ne_fresh and ne_status == "OK"
          and edge_status != "NO_EDGE_DEMONSTRATED"
          and _net_edge_pass_for(validated["candidate_id"])):
        netev = stage("NET EV", "PASS",
                      f"net_EV>0 & net_PF>=1.0 for candidate {validated['candidate_id']}")
        policy = stage("POLICY", "BLOCKED", "human gates pending")
    else:
        netev = stage("NET EV", "BLOCKED",
                      "no fresh net-edge evidence for a validated candidate")
        policy = stage("POLICY", "BLOCKED", "no valid candidates")

    shadow = stage("SHADOW / PAPER", "BLOCKED", "paper filter disabled by design")
    return [scan, signal, guard, netev, policy, shadow]


def derive_safety_view(safety: dict[str, Any] | None) -> dict[str, Any]:
    """Derive the safety panel from REAL flags. Never invents safe values."""
    s = dict(safety or {})
    live = bool(_safe(s, "live_trading", False))
    dry = bool(_safe(s, "dry_run", True))
    paper = bool(_safe(s, "paper_trading", True))
    pfilter = bool(_safe(s, "paper_filter_enabled", False))
    can_send = live and not dry
    all_safe = (not live) and dry and paper and (not pfilter) and (not can_send)
    worker = derive_worker_lock_view(s.get("worker_lock"))
    security = "SAFE_PAPER_ONLY" if all_safe else "SAFETY_REVIEW_REQUIRED"
    return {
        "mode": str(_safe(s, "mode", "paper")).upper(),
        "live_trading": live,
        "dry_run": dry,
        "paper_trading": paper,
        "paper_filter_enabled": pfilter,
        # Explicit uppercase aliases so external consumers/scripts that read
        # the config-flag spelling never see None (V10.4.3 truth fix).
        "LIVE_TRADING": live,
        "DRY_RUN": dry,
        "PAPER_TRADING": paper,
        "can_send_real_orders": can_send,
        "all_safe": all_safe,
        "security_status": security,
        "security": security,
        "paper_policy": "PAPER_ONLY" if all_safe else "REVIEW",
        "open_positions": int(_safe(s, "open_positions", 0) or 0),
        "circuit_breaker": bool(_safe(s, "circuit_breaker", False)),
        "worker_lock": worker["worker_lock"],
        "worker_acquired": worker["worker_acquired"],
        "duplicate_worker": worker["duplicate_worker"],
        "uptime": str(_safe(s, "uptime", "")),
    }


def build_dashboard_view_model(
    *,
    safety: dict[str, Any] | None = None,
    data_readiness: dict[str, Any] | None = None,
    provider_readiness: dict[str, Any] | None = None,
    candidates: dict[str, Any] | None = None,
    net_edge: dict[str, Any] | None = None,
    paper_monitor: dict[str, Any] | None = None,
    signal_monitor: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the read-only view-model. Pure; no IO."""
    vm = {
        "title": "ResearchOps Trader Terminal V1 — READ ONLY",
        "banner": "NO LIVE — RESEARCH ONLY",
        "read_only": True,
        "live_allowed": False,
        "safety": derive_safety_view(safety),
        "data_readiness": data_readiness or {
            "current_provider": "coinalyze", "current_clean_days": 0.0,
            "required_min_history_days": 180, "stronger_history_days": 365,
            "current_history_status": "NO_CLEAN_DATA",
            "current_missing_oi_ratio": 0.0, "missing_oi_status": "NEED_MORE_DATA",
            "oi_bucket_policy": "BLOCK_OI_BUCKETS",
            "data_classification": "NO_CLEAN_DATA",
            "backtester_readiness": "NEED_LONG_HISTORY",
            "data_blockers": [],
        },
        "provider_readiness": provider_readiness or {"providers": [], "recommended_next_provider": ""},
        "candidates": candidates or {"status": "NOT_COMPUTED_YET", "text": ""},
        "net_edge": net_edge or {"status": "NOT_COMPUTED_YET", "text": ""},
        "paper_monitor": paper_monitor or {"open_positions_detail": [], "paper_pnl": 0.0,
                                           "profit_factor": 0.0, "total_labels": 0,
                                           "note": "paper/shadow only — NOT real money"},
        "signal_monitor": signal_monitor or {"top_signals": [], "top_blocks": []},
        "disabled_controls": list(DISABLED_CONTROLS),
        "lock_tooltip": LOCK_TOOLTIP,
        "poll_endpoint": POLL_ENDPOINT,
        "meta": meta or {},
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }
    return vm


def dashboard_contract() -> dict[str, Any]:
    return {
        "name": "ResearchOps Trader Terminal V1",
        "read_only": True,
        "route": "/trader-terminal",
        "panels": ["mission_bar", "pipeline", "mission_control", "safety",
                   "data_readiness", "provider_readiness", "why_no_edge",
                   "candidate_edge", "net_edge_lab", "paper_monitor", "signal_monitor",
                   "strategy_research", "strategy_research_lab", "ssh_tunnel_help",
                   "disabled_controls"],
        "readonly_api_endpoints": list(READONLY_API_ENDPOINTS),
        "mutable_endpoints": [],
        "post_forms": 0,
        "near_real_time": True,
        "poll_method": "GET",
        "poll_endpoint": POLL_ENDPOINT,
        "default_refresh_seconds": DEFAULT_REFRESH_SECONDS,
        "automatic_endpoints": [POLL_ENDPOINT],
        "heavy_panels_mode": "CACHE_PEEK_ONLY",
        "heavy_refresh_mode": "CLI_ONLY",
        "polling_never_computes_heavy_work": True,
        "unknown_endpoint_behavior": "HTTP 404 + sanitized payload",
        "errors_sanitized": True,
        "disabled_controls": list(DISABLED_CONTROLS),
        "guarantees": ["no_order_buttons", "no_go_live", "no_leverage_margin_sizing_controls",
                       "no_env_edit", "no_db_writes", "no_post_put_delete_routes",
                       "js_fetch_only_to_readonly_get_endpoints",
                       "paper_pnl_not_confused_with_real_pnl"],
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


# --------------------------------------------------------------------------
# HTML rendering — self-contained dark cyber terminal + GET-only polling JS
# --------------------------------------------------------------------------

def _esc(v: Any) -> str:
    return html.escape(str(v))


RING_RADIUS = 52
RING_CIRC = 2 * math.pi * RING_RADIUS


def render_dashboard_html(vm: dict[str, Any], refresh_seconds: int = DEFAULT_REFRESH_SECONDS) -> str:
    refresh = max(3, min(60, int(refresh_seconds or DEFAULT_REFRESH_SECONDS)))
    initial_state = json.dumps(vm, ensure_ascii=True, default=str)
    # </script> breaking out of the JSON block would be an injection vector.
    initial_state = initial_state.replace("</", "<\\/")
    btns = "".join(
        f'<button class="locked" disabled title="{_esc(vm.get("lock_tooltip"))}">'
        f'&#128274; {_esc(b)}</button>' for b in vm.get("disabled_controls", []))

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(vm.get("title"))}</title>
<style>
:root{{--bg:#0a0e14;--panel:#0f151e;--panel2:#121a25;--line:#1d2733;--txt:#c8d4e0;
--muted:#7f8da0;--accent:#36e2b4;--accent2:#5aa9ff;--bad:#ff5c6c;--warn:#ffc24b;}}
*{{box-sizing:border-box}}
body{{margin:0;background:radial-gradient(1200px 600px at 70% -10%,#10202b 0,var(--bg) 60%);
color:var(--txt);font-family:ui-monospace,"SFMono-Regular",Menlo,Consolas,monospace;font-size:13px}}
.wrap{{max-width:1280px;margin:0 auto;padding:18px}}
.banner{{background:linear-gradient(90deg,#1a2a25,#13202b);border:1px solid var(--accent);
border-radius:10px;padding:12px 18px;display:flex;justify-content:space-between;align-items:center;
flex-wrap:wrap;gap:8px;box-shadow:0 0 24px rgba(54,226,180,.12)}}
.banner h1{{margin:0;font-size:17px;letter-spacing:2px;color:var(--accent)}}
.banner .ro{{color:var(--warn);font-weight:700;letter-spacing:1px}}
.statusbar{{display:flex;gap:14px;align-items:center;margin-top:10px;color:var(--muted);
font-size:11px;flex-wrap:wrap}}
.conn{{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:14px;
border:1px solid var(--line);background:#101823}}
.conn .dot{{width:8px;height:8px}}
.conn.live{{border-color:var(--accent)}}.conn.live .dot{{background:var(--accent);box-shadow:0 0 8px var(--accent)}}
.conn.loading{{border-color:var(--accent2)}}.conn.loading .dot{{background:var(--accent2)}}
.conn.stale{{border-color:var(--warn)}}.conn.stale .dot{{background:var(--warn);box-shadow:0 0 8px var(--warn)}}
.conn.error{{border-color:var(--bad)}}.conn.error .dot{{background:var(--bad);box-shadow:0 0 8px var(--bad)}}
.violation{{display:none;background:#2a1216;border:1px solid var(--bad);color:var(--bad);
border-radius:10px;padding:10px 16px;margin-top:10px;font-weight:700;letter-spacing:1px}}
.grid{{display:grid;grid-template-columns:repeat(12,1fr);gap:14px;margin-top:14px}}
.card{{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);
border-radius:12px;padding:14px 16px;box-shadow:0 0 18px rgba(0,0,0,.35)}}
.card h2{{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:1.5px;color:var(--accent2)}}
.col-3{{grid-column:span 3}}.col-4{{grid-column:span 4}}.col-6{{grid-column:span 6}}
.col-8{{grid-column:span 8}}.col-12{{grid-column:span 12}}
@media(max-width:920px){{.col-3,.col-4,.col-6,.col-8{{grid-column:span 12}}}}
.kv{{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px dashed #182230;gap:10px}}
.kv .v{{color:#e9f1f7;text-align:right;word-break:break-word}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;vertical-align:middle}}
.dot.ok{{background:var(--accent);box-shadow:0 0 8px var(--accent)}}
.dot.bad{{background:var(--bad);box-shadow:0 0 8px var(--bad)}}
.dot.warn{{background:var(--warn);box-shadow:0 0 8px var(--warn)}}
.ring{{text-align:center}}.ring-val{{fill:#eaf3f8;font-size:18px;font-weight:700}}
.ring-pct{{fill:var(--muted);font-size:11px}}.ring-cap{{color:var(--muted);margin-top:4px;font-size:11px}}
.bar{{height:8px;background:#15212e;border-radius:6px;overflow:hidden;margin:6px 0}}
.bar>span{{display:block;height:100%;background:linear-gradient(90deg,var(--accent2),var(--accent));
border-radius:6px;transition:width .6s}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}}
th{{color:var(--muted);font-weight:600;text-transform:uppercase;font-size:10px;letter-spacing:1px}}
.tag{{background:#15212e;border:1px solid var(--line);border-radius:6px;padding:1px 7px;color:var(--accent2)}}
.locked{{background:#14202c;color:#5b6b7d;border:1px dashed #2a3a4a;border-radius:8px;
padding:8px 10px;margin:4px;cursor:not-allowed;font-family:inherit;font-size:11px}}
.funnel{{display:flex;flex-wrap:wrap;align-items:center;gap:6px}}
.funnel-step{{background:#13202b;border:1px solid var(--line);border-radius:8px;padding:6px 10px;color:#bcd}}
.funnel-arrow{{color:var(--muted)}}
.muted{{color:var(--muted)}}.warn{{color:var(--warn)}}.bad{{color:var(--bad)}}.good{{color:var(--accent)}}
ul{{margin:6px 0;padding-left:18px}}li{{margin:2px 0;color:var(--muted)}}
.note{{color:var(--warn);font-size:12px;margin-top:6px}}
pre.mini{{white-space:pre-wrap;color:var(--muted);font-size:11px;max-height:180px;overflow:auto;
background:#0c121a;border:1px solid var(--line);border-radius:8px;padding:8px}}
.footer{{margin-top:18px;text-align:center;color:var(--muted);font-size:11px}}
/* V10.5 Research Command Center */
.mission-bar{{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}}
.chip{{flex:1 1 110px;min-width:110px;background:linear-gradient(180deg,#101822,#0d141d);
border:1px solid var(--line);border-radius:12px;padding:10px 12px;text-align:center;
box-shadow:0 0 14px rgba(90,169,255,.06)}}
.chip .k{{font-size:9px;letter-spacing:1.5px;color:var(--muted);text-transform:uppercase}}
.chip .v{{font-size:15px;font-weight:700;margin-top:4px;color:#e9f1f7;text-shadow:0 0 12px rgba(54,226,180,.25)}}
.chip.ok .v{{color:var(--accent)}}.chip.block .v{{color:var(--bad)}}.chip.warn .v{{color:var(--warn)}}
.pipeline{{display:flex;flex-wrap:wrap;gap:6px;align-items:stretch;margin-top:12px}}
.pl-step{{flex:1 1 140px;min-width:140px;background:#0e1620;border:1px solid var(--line);
border-radius:10px;padding:8px 10px;position:relative}}
.pl-step .name{{font-size:10px;letter-spacing:1.2px;color:var(--accent2);text-transform:uppercase}}
.pl-step .st{{font-size:13px;font-weight:700;margin-top:3px}}
.pl-step .why{{font-size:10px;color:var(--muted);margin-top:2px}}
.pl-step.pass{{border-color:var(--accent)}}.pl-step.pass .st{{color:var(--accent)}}
.pl-step.block{{border-color:var(--bad)}}.pl-step.block .st{{color:var(--bad)}}
.pl-step.stale{{border-color:var(--warn)}}.pl-step.stale .st{{color:var(--warn)}}
.pl-step.needs{{border-color:var(--accent2)}}.pl-step.needs .st{{color:var(--accent2)}}
code.ssh{{display:block;background:#0c121a;border:1px solid var(--line);border-radius:8px;
padding:8px;color:var(--accent);font-size:11px;margin:6px 0;word-break:break-all}}
</style></head>
<body><div class="wrap">
<div class="banner"><h1>&#9673; RESEARCH COMMAND CENTER</h1>
<div class="ro">NO LIVE — RESEARCH ONLY</div></div>

<!-- V10.5 TOP MISSION BAR -->
<div class="mission-bar" id="mission-bar">
  <div class="chip ok"><div class="k">Mode</div><div class="v" id="mb-mode">PAPER</div></div>
  <div class="chip block"><div class="k">Live</div><div class="v" id="mb-live">BLOCKED</div></div>
  <div class="chip ok"><div class="k">Paper Filter</div><div class="v" id="mb-filter">OFF</div></div>
  <div class="chip"><div class="k">Open Positions</div><div class="v" id="mb-pos">0</div></div>
  <div class="chip ok"><div class="k">Worker</div><div class="v" id="mb-worker">—</div></div>
  <div class="chip warn"><div class="k">Edge</div><div class="v" id="mb-edge">NOT DEMONSTRATED</div></div>
  <div class="chip warn"><div class="k">Data</div><div class="v" id="mb-data">NEEDS 180/365D</div></div>
  <div class="chip block"><div class="k">Final</div><div class="v" id="mb-final">NO LIVE</div></div>
</div>

<!-- V10.5 PIPELINE -->
<div class="pipeline" id="pipeline">
  <div class="pl-step" id="pl-scan"><div class="name">Scan</div><div class="st">—</div><div class="why">—</div></div>
  <div class="pl-step" id="pl-signal"><div class="name">Signal</div><div class="st">—</div><div class="why">—</div></div>
  <div class="pl-step" id="pl-guard"><div class="name">Edge Guard</div><div class="st">—</div><div class="why">—</div></div>
  <div class="pl-step" id="pl-netev"><div class="name">Net EV</div><div class="st">—</div><div class="why">—</div></div>
  <div class="pl-step" id="pl-policy"><div class="name">Policy</div><div class="st">—</div><div class="why">—</div></div>
  <div class="pl-step" id="pl-shadow"><div class="name">Shadow / Paper</div><div class="st">—</div><div class="why">—</div></div>
</div>
<div class="statusbar">
  <span class="conn loading" id="conn"><span class="dot"></span><span id="conn-text">LOADING</span></span>
  <span>last update: <span id="last-update">never</span></span>
  <span>refresh: every {refresh}s (read-only GET polling)</span>
  <span id="stale-note" class="warn" style="display:none">data may be outdated</span>
</div>
<div class="note">{_esc(HEAVY_PANEL_NOTE)}</div>
<div class="violation" id="violation">&#9888; SAFETY REVIEW REQUIRED — a safety flag is not in its safe position</div>

<div class="grid">
  <div class="card col-4"><h2>Mission Control</h2>
    <div class="kv"><span>mode</span><span class="v good" id="mc-mode">PAPER</span></div>
    <div class="kv"><span>uptime</span><span class="v" id="mc-uptime">—</span></div>
    <div class="kv"><span>live_allowed</span><span class="v" id="mc-live-allowed"><span class="dot ok"></span>false</span></div>
    <div class="kv"><span>can_send_real_orders</span><span class="v" id="mc-real-orders"><span class="dot ok"></span>false</span></div>
    <div class="kv"><span>open_positions</span><span class="v" id="mc-open-pos">0</span></div>
    <div class="kv"><span>circuit_breaker</span><span class="v" id="mc-circuit">false</span></div>
    <div class="kv"><span>worker_lock</span><span class="v" id="mc-worker">—</span></div>
    <div class="kv"><span>final_recommendation</span><span class="v warn" id="mc-final">NO LIVE</span></div>
  </div>

  <div class="card col-4"><h2>Safety Panel</h2>
    <div class="kv"><span>security</span><span class="v good" id="sf-security">—</span></div>
    <div class="kv"><span>paper_policy</span><span class="v" id="sf-policy">—</span></div>
    <div class="kv"><span>LIVE_TRADING</span><span class="v" id="sf-live">—</span></div>
    <div class="kv"><span>DRY_RUN</span><span class="v" id="sf-dry">—</span></div>
    <div class="kv"><span>PAPER_TRADING</span><span class="v" id="sf-paper">—</span></div>
    <div class="kv"><span>paper_filter_enabled</span><span class="v" id="sf-filter">—</span></div>
    <div class="kv"><span>worker_acquired</span><span class="v" id="sf-acquired">—</span></div>
    <div class="kv"><span>duplicate_worker</span><span class="v" id="sf-dup">—</span></div>
  </div>

  <div class="card col-4"><h2>Data Readiness</h2>
    <div class="ring"><svg viewBox="0 0 130 130" width="130" height="130">
      <circle cx="65" cy="65" r="{RING_RADIUS}" stroke="#1d2733" stroke-width="12" fill="none"/>
      <circle id="ring-arc" cx="65" cy="65" r="{RING_RADIUS}" stroke="#36e2b4" stroke-width="12" fill="none"
        stroke-linecap="round" stroke-dasharray="0 {RING_CIRC:.1f}" transform="rotate(-90 65 65)"/>
      <text x="65" y="62" text-anchor="middle" class="ring-val" id="ring-days">0.0</text>
      <text x="65" y="82" text-anchor="middle" class="ring-pct" id="ring-pct">0%</text>
    </svg><div class="ring-cap" id="ring-cap">clean days / 180 required</div></div>
    <div class="kv"><span>provider</span><span class="v" id="dr-provider">—</span></div>
    <div class="kv"><span>classification</span><span class="v" id="dr-class">—</span></div>
    <div class="kv"><span>backtester</span><span class="v warn" id="dr-backtester">—</span></div>
    <div class="kv"><span>missing OI</span><span class="v" id="dr-oi">—</span></div>
    <div class="kv"><span>OI policy</span><span class="v bad" id="dr-oi-policy">—</span></div>
    <div class="bar"><span id="dr-bar" style="width:0%"></span></div>
  </div>

  <div class="card col-8"><h2>V10.5 Provider Verification</h2>
    <table><thead><tr><th>provider</th><th>role</th><th>status</th><th>paid authorized</th></tr></thead>
    <tbody id="pv105-rows"><tr><td colspan="4" class="muted">loading…</td></tr></tbody></table>
    <div class="note">Real verification status (V10.5 scorecards): nothing is verified or
    paid-authorized until a human validates a sample offline.</div>
    <div class="kv" style="margin-top:8px"><span class="muted">Legacy local registry (V10.3) — current ingest source, NOT a completed verification</span><span class="v muted">Provider Readiness (legacy)</span></div>
    <table><thead><tr><th>provider</th><th>status</th><th>bitget</th><th>180d</th><th>365d</th><th>paid</th></tr></thead>
    <tbody id="pr-rows"><tr><td colspan="6" class="muted">loading…</td></tr></tbody></table>
    <div class="note" id="pr-next">recommended next: — · verify pricing/limits before any paid download</div>
  </div>

  <div class="card col-4"><h2>Why No Trade / Why No Edge</h2>
    <ul id="wn-list"><li>loading…</li></ul>
    <div class="kv"><span>data blockers</span><span class="v" id="wn-count">—</span></div>
    <ul id="dr-blockers"><li>loading…</li></ul>
  </div>

  <div class="card col-8"><h2>Candidate / Edge Funnel</h2>
    <div class="funnel">
      <div class="funnel-step">raw signals</div><div class="funnel-arrow">&#8250;</div>
      <div class="funnel-step">watched</div><div class="funnel-arrow">&#8250;</div>
      <div class="funnel-step">rejected</div><div class="funnel-arrow">&#8250;</div>
      <div class="funnel-step">shadow</div><div class="funnel-arrow">&#8250;</div>
      <div class="funnel-step">candidate</div><div class="funnel-arrow">&#8250;</div>
      <div class="funnel-step">paper-ready</div>
    </div>
    <div class="kv" style="margin-top:10px"><span>candidate-ranking</span><span class="v bad" id="cd-status">—</span></div>
    <pre class="mini" id="cd-text">loading…</pre>
  </div>

  <div class="card col-4"><h2>Paper Monitor</h2>
    <div class="kv"><span>open paper positions</span><span class="v" id="pm-open">0</span></div>
    <div class="kv"><span>paper PnL (paper/shadow — NOT real)</span><span class="v" id="pm-pnl">0.0</span></div>
    <div class="kv"><span>label profit factor (6h)</span><span class="v" id="pm-pf">—</span></div>
    <div class="kv"><span>labels (6h)</span><span class="v" id="pm-labels">0</span></div>
    <ul id="pm-positions"><li>none</li></ul>
    <div class="note">Paper/shadow PnL only — NOT real money.</div>
  </div>

  <div class="card col-6"><h2>Net Edge Lab</h2>
    <div class="kv"><span>status</span><span class="v" id="ne-status">—</span></div>
    <pre class="mini" id="ne-text">loading…</pre>
  </div>

  <div class="card col-6"><h2>Learning Status</h2>
    <div class="kv"><span>observations</span><span class="v" id="ln-obs">—</span></div>
    <div class="kv"><span>labels</span><span class="v" id="ln-labels">—</span></div>
    <div class="kv"><span>path metrics (MFE/MAE)</span><span class="v" id="ln-path">—</span></div>
    <div class="kv"><span>virtual research trades</span><span class="v" id="ln-virtual">—</span></div>
    <div class="kv"><span>learning_status</span><span class="v" id="ln-status">—</span></div>
    <div class="kv"><span>edge_status</span><span class="v bad" id="ln-edge">NO_EDGE_DEMONSTRATED</span></div>
  </div>

  <div class="card col-6"><h2>Signal Monitor</h2>
    <div class="kv"><span>recent signals</span><span class="v" id="sg-count">0</span></div>
    <ul id="sg-signals"><li>none</li></ul>
    <div class="kv"><span>recent EdgeGuard blocks</span><span class="v" id="sg-blocks-count">0</span></div>
    <ul id="sg-blocks"><li>none</li></ul>
  </div>

  <div class="card col-12"><h2>Strategy / Research Panel</h2>
    <div class="kv"><span>promotion ladder</span><span class="v">RESEARCH_ONLY &#8250; BACKTEST_CANDIDATE &#8250; WALK_FORWARD_CANDIDATE &#8250; SHADOW_ONLY &#8250; PAPER_ELIGIBLE_FUTURE</span></div>
    <div class="kv"><span>history target</span><span class="v">&#8805;180d clean (initial) · &#8805;365d clean (stronger)</span></div>
    <div class="kv"><span>OI rule</span><span class="v">if OI is not audited, OI buckets stay blocked</span></div>
    <div class="kv"><span>what is blocking edge</span><span class="v warn" id="sr-blocking">—</span></div>
    <div class="kv"><span>next best research action</span><span class="v" id="sr-next">—</span></div>
    <div class="kv"><span>research note</span><span class="v muted">no edge candidate is actionable until data + gates pass — the system reports this honestly</span></div>
  </div>

  <div class="card col-8"><h2>Strategy Research Lab</h2>
    <div class="kv"><span>research backlog</span><span class="v">external ideas enter via intake (ceiling: SHADOW_ELIGIBLE; unknown risk parks in NEEDS_RISK_REVIEW)</span></div>
    <div class="kv"><span>active hypothesis</span><span class="v warn" id="lab-hypothesis">TIME-death dominates exits; net EV negative after costs on every bucket</span></div>
    <div class="kv"><span>required tests</span><span class="v">180/365d replay &#8250; cost x1/x2/x3 &#8250; walk-forward monthly+rolling &#8250; OOS &#8250; stability matrix</span></div>
    <div class="kv"><span>anti-overfit status</span><span class="v" id="lab-overfit">gates armed: min 150 samples · net PF&#8805;1.30 · no same-window tuning</span></div>
    <div class="kv"><span>promotion blocked reason</span><span class="v bad" id="lab-blocked">net_EV&#8804;0 + history&lt;180d + OI blocked</span></div>
    <div class="note">Research only: this lab never generates executable runtime code and never activates bots.</div>
  </div>

  <div class="card col-4"><h2>SSH Tunnel Help</h2>
    <div class="kv"><span>access</span><span class="v warn">dashboard only via SSH tunnel</span></div>
    <code class="ssh">ssh -L 18080:127.0.0.1:8080 ubuntu@YOUR_VPS_IP</code>
    <code class="ssh">http://127.0.0.1:18080/trader-terminal?token=&lt;your_token&gt;</code>
    <div class="note">Never expose port 8080 publicly. Never share or print the token.</div>
  </div>

  <div class="card col-12"><h2>Disabled Controls — Future Actions (locked)</h2>
    <div>{btns}</div>
    <div class="note">All actions disabled. {_esc(vm.get("lock_tooltip"))}. No backend, no-op, read-only.
    Copy trading, leverage controls and casino mechanics are permanently locked anti-features.</div>
  </div>
</div>

<div class="footer">ResearchOps Terminal · read-only · NO LIVE · research/paper data only ·
no real orders · no mutable controls · polling GET {_esc(POLL_ENDPOINT)} every {refresh}s</div>
</div>

<script id="initial-state" type="application/json">{initial_state}</script>
<script>
"use strict";
// READ-ONLY terminal. The only automatic request is the ultra-light
// dashboard-state GET. Heavy reports are refreshed through CLI/runbooks.
var REFRESH_MS = {refresh} * 1000;
var POLL_URL = "{POLL_ENDPOINT}";
var token = new URLSearchParams(window.location.search).get("token");
var lastOkAt = 0;

function getJSON(path) {{
  if (path.indexOf("/api/researchops/v104/") !== 0) {{
    return Promise.reject(new Error("blocked: non-readonly endpoint"));
  }}
  var url = path + (token ? "?token=" + encodeURIComponent(token) : "");
  return fetch(url, {{ method: "GET", cache: "no-store" }}).then(function (r) {{
    if (!r.ok) throw new Error("http " + r.status);
    return r.json();
  }});
}}

function esc(v) {{
  return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {{
    return {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c];
  }});
}}
function txt(id, v) {{ var el = document.getElementById(id); if (el) el.textContent = String(v); }}
function semaphore(id, ok, label) {{
  var el = document.getElementById(id); if (!el) return;
  el.innerHTML = '<span class="dot ' + (ok ? "ok" : "bad") + '"></span>' + esc(label);
}}
function setConn(state, label) {{
  var el = document.getElementById("conn");
  el.className = "conn " + state;
  txt("conn-text", label);
  document.getElementById("stale-note").style.display =
    (state === "stale" || state === "error") ? "inline" : "none";
}}

function applyState(s) {{
  if (!s || typeof s !== "object") return;
  var sf = s.safety || {{}};
  var dr = s.data_readiness || {{}};
  var pr = s.provider_readiness || {{}};
  var cd = s.candidates || {{}};
  var ne = s.net_edge || {{}};
  var pm = s.paper_monitor || {{}};
  var sg = s.signal_monitor || {{}};

  txt("mc-mode", sf.mode || "PAPER");
  txt("mc-uptime", sf.uptime || "—");
  semaphore("mc-live-allowed", !s.live_allowed, String(!!s.live_allowed));
  semaphore("mc-real-orders", !sf.can_send_real_orders, String(!!sf.can_send_real_orders));
  txt("mc-open-pos", sf.open_positions || 0);
  txt("mc-circuit", String(!!sf.circuit_breaker));
  txt("mc-worker", sf.worker_lock || "unknown");
  txt("mc-final", s.final_recommendation || "NO LIVE");

  txt("sf-security", sf.security_status || "—");
  txt("sf-policy", sf.paper_policy || "—");
  semaphore("sf-live", !sf.live_trading, String(!!sf.live_trading));
  semaphore("sf-dry", !!sf.dry_run, String(!!sf.dry_run));
  semaphore("sf-paper", !!sf.paper_trading, String(!!sf.paper_trading));
  semaphore("sf-filter", !sf.paper_filter_enabled, String(!!sf.paper_filter_enabled));
  txt("sf-acquired", sf.worker_acquired === undefined ? "unknown" : String(sf.worker_acquired));
  txt("sf-dup", sf.duplicate_worker || "UNKNOWN");
  document.getElementById("violation").style.display = sf.all_safe === false ? "block" : "none";

  var clean = Number(dr.current_clean_days || 0);
  var req = Number(dr.required_min_history_days || 180);
  var pct = Math.max(0, Math.min(100, req ? (clean / req) * 100 : 0));
  var circ = {RING_CIRC:.1f};
  var arc = document.getElementById("ring-arc");
  if (arc) arc.setAttribute("stroke-dasharray", (circ * pct / 100).toFixed(1) + " " + circ.toFixed(1));
  txt("ring-days", clean.toFixed(1));
  txt("ring-pct", pct.toFixed(0) + "%");
  txt("ring-cap", "clean days / " + req + " required");
  txt("dr-provider", dr.current_provider || "—");
  txt("dr-class", dr.data_classification || dr.data_status || "—");
  txt("dr-backtester", dr.backtester_readiness || dr.data_status || "—");
  txt("dr-oi", ((Number(dr.current_missing_oi_ratio || 0) * 100).toFixed(1)) + "% · " + (dr.missing_oi_status || "—"));
  txt("dr-oi-policy", dr.oi_bucket_policy || "—");
  var bar = document.getElementById("dr-bar"); if (bar) bar.style.width = pct.toFixed(0) + "%";

  // V10.5 verification scorecards (the real status — distinct from legacy).
  var pv = s.provider_verification_v105 || {{}};
  var pvRows = "";
  (pv.providers || []).forEach(function (p) {{
    pvRows += "<tr><td>" + esc(p.name) + "</td><td>" + esc(p.role) +
      "</td><td><span class='tag'>" + esc(p.status) + "</span></td><td>" +
      esc(String(!!p.paid_download_authorized)) + "</td></tr>";
  }});
  var pvEl = document.getElementById("pv105-rows");
  if (pvEl) pvEl.innerHTML = pvRows || "<tr><td colspan='4' class='muted'>no scorecards</td></tr>";

  var rows = "";
  (pr.providers || []).forEach(function (p) {{
    rows += "<tr><td>" + esc(p.name || p.provider_id) + "</td><td><span class='tag'>" +
      esc(p.status) + "</span></td><td>" + esc(p.bitget_perp_support) + "</td><td>" +
      esc(p.suitable_for_180d) + "</td><td>" + esc(p.suitable_for_365d) + "</td><td>" +
      esc(p.paid_data_risk) + "</td></tr>";
  }});
  document.getElementById("pr-rows").innerHTML =
    rows || "<tr><td colspan='6' class='muted'>no providers</td></tr>";
  txt("pr-next", "recommended next: " + (pr.recommended_next_provider || "NEEDS_MANUAL_VERIFICATION") +
    " · verify pricing/limits before any paid download");

  var blockers = "";
  (dr.data_blockers || []).forEach(function (b) {{ blockers += "<li>" + esc(b) + "</li>"; }});
  document.getElementById("dr-blockers").innerHTML = blockers || "<li>none</li>";

  var ef = s.edge_focus || {{}};
  txt("sr-blocking", (ef.what_is_blocking_edge || []).join(" · ") || "—");
  txt("sr-next", ef.next_best_research_action || "—");

  // V10.5 — mission bar (top KPI chips).
  txt("mb-mode", sf.mode || "PAPER");
  txt("mb-live", s.live_allowed ? "DANGER" : "BLOCKED");
  txt("mb-filter", sf.paper_filter_enabled ? "ON (REVIEW!)" : "OFF");
  txt("mb-pos", sf.open_positions || 0);
  txt("mb-worker", String(sf.worker_lock || "unknown").toUpperCase());
  var cdStatus = String(cd.status || cd.data_status || "");
  // V10.5.3 — CANDIDATE PENDING requires BOTH server stages to PASS:
  // EDGE GUARD (validated candidate) AND NET EV (positive net edge).
  // Guard PASS alone shows EDGE REVIEW, never a pending candidate.
  var plStages = s.pipeline || [];
  var guardStage = plStages[2] || {{}};
  var netevStage = plStages[3] || {{}};
  var edgeChip = "NOT DEMONSTRATED";
  if (guardStage.state === "PASS" && netevStage.state === "PASS") {{
    edgeChip = "CANDIDATE PENDING";
  }} else if (guardStage.state === "PASS") {{
    edgeChip = "EDGE REVIEW";
  }}
  txt("mb-edge", edgeChip);
  var drStatus = String(dr.backtester_readiness || dr.data_status || "");
  txt("mb-data", drStatus === "READY" ? "READY" : "NEEDS 180/365D");
  txt("mb-final", s.final_recommendation || "NO LIVE");

  // V10.5.1 — pipeline stages come from the SERVER (conservative derivation;
  // PASS only with explicit fresh evidence). The client just paints them.
  function stage(id, st, why) {{
    var el = document.getElementById(id); if (!el) return;
    var cls = {{PASS: "pass", BLOCKED: "block", STALE: "stale", NEEDS_DATA: "needs"}}[st] || "stale";
    el.className = "pl-step " + cls;
    el.children[1].textContent = st || "STALE";
    el.children[2].textContent = why || "no evidence";
  }}
  var plIds = ["pl-scan", "pl-signal", "pl-guard", "pl-netev", "pl-policy", "pl-shadow"];
  var pl = s.pipeline || [];
  for (var i = 0; i < plIds.length; i++) {{
    var st = pl[i] || {{}};
    stage(plIds[i], st.state || "STALE", st.why || "no server snapshot");
  }}
  var blocks2 = (sg.top_blocks || []).length;

  // V10.5 — why no trade / why no edge.
  var why = [];
  (ef.what_is_blocking_edge || []).forEach(function (b) {{ why.push(b); }});
  if (blocks2 > 0) why.push("EdgeGuard: low RR blocks in window");
  why.push("sample_too_small / high TIME-death on observed buckets");
  var whyHtml = "";
  why.slice(0, 8).forEach(function (w) {{ whyHtml += "<li>" + esc(w) + "</li>"; }});
  var wnEl = document.getElementById("wn-list");
  if (wnEl) wnEl.innerHTML = whyHtml || "<li>none</li>";
  txt("wn-count", (dr.data_blockers || []).length);

  txt("cd-status", cd.status || cd.overall_status || cd.data_status || "NOT_COMPUTED_YET");
  txt("cd-text", (cd.text || "").slice(0, 2200) || "no cached candidate-ranking output; run the CLI report");
  txt("ne-status", ne.status || ne.overall_status || ne.data_status || "NOT_COMPUTED_YET");
  txt("ne-text", (ne.text || "").slice(0, 2200) || "no cached net-edge output; run the CLI report");

  // V10.5.2 — paper monitor is snapshot-only in polling; fresh values are
  // limited to the in-memory runtime PnL counter.
  var pmPending = pm.data_status === "STALE_OR_PENDING" || pm.data_status === "ERROR_STALE";
  var opens = pm.open_positions_detail || [];
  txt("pm-open", pmPending ? "STALE_OR_PENDING" : opens.length);
  var pmPnl = (pm.paper_pnl_runtime !== undefined ? pm.paper_pnl_runtime : pm.paper_pnl);
  txt("pm-pnl", Number(pmPnl || 0).toFixed(4));
  txt("pm-pf", pmPending ? "—" : Number(pm.profit_factor || 0).toFixed(2));
  txt("pm-labels", pmPending ? "—" : (pm.total_labels || 0));
  var pos = "";
  opens.forEach(function (p) {{
    pos += "<li>" + esc(p.symbol) + " " + esc(p.side) + " @" + esc(p.entry_price) + "</li>";
  }});
  document.getElementById("pm-positions").innerHTML =
    pos || (pmPending ? "<li>no snapshot; run CLI report</li>" : "<li>none</li>");

  var ln = s.learning || {{}};
  txt("ln-obs", ln.observations === undefined ? "—" : ln.observations);
  txt("ln-labels", ln.labels === undefined ? "—" : ln.labels);
  txt("ln-path", ln.path_metrics === undefined ? "—" : ln.path_metrics);
  txt("ln-virtual", ln.virtual_research_trades === undefined ? "—" : ln.virtual_research_trades);
  txt("ln-status", ln.learning_status || "—");
  txt("ln-edge", ln.edge_status || "NO_EDGE_DEMONSTRATED");

  var sigs = sg.top_signals || [];
  var blocks = sg.top_blocks || [];
  txt("sg-count", sigs.length);
  txt("sg-blocks-count", blocks.length);
  var sigHtml = "";
  sigs.slice(0, 6).forEach(function (x) {{ sigHtml += "<li>" + esc(typeof x === "string" ? x : JSON.stringify(x).slice(0, 120)) + "</li>"; }});
  document.getElementById("sg-signals").innerHTML = sigHtml || "<li>none</li>";
  var blkHtml = "";
  blocks.slice(0, 6).forEach(function (x) {{ blkHtml += "<li>" + esc(typeof x === "string" ? x : JSON.stringify(x).slice(0, 120)) + "</li>"; }});
  document.getElementById("sg-blocks").innerHTML = blkHtml || "<li>none</li>";
}}

function poll() {{
  // GET only — ultra-light read-only state endpoint (server cache peek).
  getJSON(POLL_URL)
    .then(function (s) {{
      applyState(s);
      lastOkAt = Date.now();
      txt("last-update", new Date().toISOString().replace("T", " ").slice(0, 19) + " UTC");
      setConn("live", "LIVE-POLL");
    }})
    .catch(function () {{
      var age = Date.now() - lastOkAt;
      if (!lastOkAt || age > REFRESH_MS * 3) setConn("error", "ERROR");
      else setConn("stale", "STALE");
    }});
}}

try {{
  var seed = JSON.parse(document.getElementById("initial-state").textContent);
  applyState(seed);
  txt("last-update", "server render (loading live state…)");
}} catch (e) {{ /* keep server-rendered values */ }}
setConn("loading", "LOADING");
poll();
setInterval(poll, REFRESH_MS);
</script>
</body></html>"""
