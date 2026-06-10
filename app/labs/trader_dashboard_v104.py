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

# Future-action buttons — ALL disabled, no backend, no-op.
DISABLED_CONTROLS = [
    "Enable Live", "Enable Paper Filter", "Run Paid Download", "Promote Candidate",
    "Start Backtester Operational", "Re-ingest Data", "Replace Raw Data",
]

READONLY_API_ENDPOINTS = [
    "/api/researchops/v104/overview",
    "/api/researchops/v104/safety",
    "/api/researchops/v104/data-readiness",
    "/api/researchops/v104/provider-readiness",
    "/api/researchops/v104/candidates",
    "/api/researchops/v104/net-edge",
    "/api/researchops/v104/paper-monitor",
    "/api/researchops/v104/signal-monitor",
    "/api/researchops/v104/dashboard-state",
]


def _safe(d: dict | None, key: str, default: Any) -> Any:
    if not isinstance(d, dict):
        return default
    value = d.get(key, default)
    return default if value is None else value


def derive_safety_view(safety: dict[str, Any] | None) -> dict[str, Any]:
    """Derive the safety panel from REAL flags. Never invents safe values."""
    s = dict(safety or {})
    live = bool(_safe(s, "live_trading", False))
    dry = bool(_safe(s, "dry_run", True))
    paper = bool(_safe(s, "paper_trading", True))
    pfilter = bool(_safe(s, "paper_filter_enabled", False))
    can_send = live and not dry
    all_safe = (not live) and dry and paper and (not pfilter) and (not can_send)
    return {
        "mode": str(_safe(s, "mode", "paper")).upper(),
        "live_trading": live,
        "dry_run": dry,
        "paper_trading": paper,
        "paper_filter_enabled": pfilter,
        "can_send_real_orders": can_send,
        "all_safe": all_safe,
        "security_status": "SAFE_PAPER_ONLY" if all_safe else "SAFETY_REVIEW_REQUIRED",
        "paper_policy": "PAPER_ONLY" if all_safe else "REVIEW",
        "open_positions": int(_safe(s, "open_positions", 0) or 0),
        "circuit_breaker": bool(_safe(s, "circuit_breaker", False)),
        "worker_lock": str(_safe(s, "worker_lock", "unknown")),
        "duplicate_worker": str(_safe(s, "duplicate_worker", "NO")),
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
        "panels": ["mission_control", "safety", "data_readiness", "provider_readiness",
                   "candidate_edge", "net_edge_lab", "paper_monitor", "signal_monitor",
                   "strategy_research", "disabled_controls"],
        "readonly_api_endpoints": list(READONLY_API_ENDPOINTS),
        "mutable_endpoints": [],
        "post_forms": 0,
        "near_real_time": True,
        "poll_method": "GET",
        "poll_endpoint": POLL_ENDPOINT,
        "default_refresh_seconds": DEFAULT_REFRESH_SECONDS,
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
</style></head>
<body><div class="wrap">
<div class="banner"><h1>&#9673; RESEARCHOPS TERMINAL</h1>
<div class="ro">NO LIVE — RESEARCH ONLY</div></div>
<div class="statusbar">
  <span class="conn loading" id="conn"><span class="dot"></span><span id="conn-text">LOADING</span></span>
  <span>last update: <span id="last-update">never</span></span>
  <span>refresh: every {refresh}s (read-only GET polling)</span>
  <span id="stale-note" class="warn" style="display:none">data may be outdated</span>
</div>
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

  <div class="card col-8"><h2>Provider Readiness</h2>
    <table><thead><tr><th>provider</th><th>status</th><th>bitget</th><th>180d</th><th>365d</th><th>paid</th></tr></thead>
    <tbody id="pr-rows"><tr><td colspan="6" class="muted">loading…</td></tr></tbody></table>
    <div class="note" id="pr-next">recommended next: — · verify pricing/limits before any paid download</div>
  </div>

  <div class="card col-4"><h2>Data Blockers</h2><ul id="dr-blockers"><li>loading…</li></ul></div>

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
    <div class="kv"><span>research note</span><span class="v muted">no edge candidate is actionable until data + gates pass — the system reports this honestly</span></div>
  </div>

  <div class="card col-12"><h2>Disabled Controls — Future Actions (locked)</h2>
    <div>{btns}</div>
    <div class="note">All actions disabled. {_esc(vm.get("lock_tooltip"))}. No backend, no-op, read-only.</div>
  </div>
</div>

<div class="footer">ResearchOps Terminal · read-only · NO LIVE · research/paper data only ·
no real orders · no mutable controls · polling GET {_esc(POLL_ENDPOINT)} every {refresh}s</div>
</div>

<script id="initial-state" type="application/json">{initial_state}</script>
<script>
"use strict";
// READ-ONLY terminal. The ONLY network call below is a GET fetch to the
// read-only state endpoint. There are no mutable endpoints and no handlers
// on the locked buttons.
var REFRESH_MS = {refresh} * 1000;
var POLL_URL = "{POLL_ENDPOINT}";
var token = new URLSearchParams(window.location.search).get("token");
var lastOkAt = 0;

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
  txt("sf-dup", sf.duplicate_worker || "NO");
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
  txt("dr-class", dr.data_classification || "—");
  txt("dr-backtester", dr.backtester_readiness || "—");
  txt("dr-oi", ((Number(dr.current_missing_oi_ratio || 0) * 100).toFixed(1)) + "% · " + (dr.missing_oi_status || "—"));
  txt("dr-oi-policy", dr.oi_bucket_policy || "—");
  var bar = document.getElementById("dr-bar"); if (bar) bar.style.width = pct.toFixed(0) + "%";

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

  txt("cd-status", cd.status || cd.overall_status || "NOT_COMPUTED_YET");
  txt("cd-text", (cd.text || "").slice(0, 2200) || "no candidate-ranking output yet");
  txt("ne-status", ne.status || ne.overall_status || "NOT_COMPUTED_YET");
  txt("ne-text", (ne.text || "").slice(0, 2200) || "no net-edge output yet");

  var opens = pm.open_positions_detail || [];
  txt("pm-open", opens.length);
  txt("pm-pnl", Number(pm.paper_pnl || 0).toFixed(4));
  txt("pm-pf", Number(pm.profit_factor || 0).toFixed(2));
  txt("pm-labels", pm.total_labels || 0);
  var pos = "";
  opens.forEach(function (p) {{
    pos += "<li>" + esc(p.symbol) + " " + esc(p.side) + " @" + esc(p.entry_price) + "</li>";
  }});
  document.getElementById("pm-positions").innerHTML = pos || "<li>none</li>";

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
  var url = POLL_URL + (token ? "?token=" + encodeURIComponent(token) : "");
  // GET only — read-only state endpoint; no mutable routes exist for V10.4.
  fetch(url, {{ method: "GET", cache: "no-store" }})
    .then(function (r) {{
      if (!r.ok) throw new Error("http " + r.status);
      return r.json();
    }})
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
