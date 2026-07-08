"""ResearchOps V10.43B - Trading Research Command Center (upgraded dashboard).

Extends the V10.43A dashboard (reuses its CSS + helpers, does not break it) with
WS Data Integration, REST-vs-WS comparison, Strategy Factory, Incubator
Watchlist, an upgraded Probability Lattice (explicit DATA_GAP / STALE /
INSUFFICIENT_SAMPLE) and a Relationship Graph that shows lead-lag only if real,
else WAITING_DATA. Honest, self-contained (no CDN), research-only, NO LIVE.
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE
from . import research_dashboard_v10_43a as A
from . import ws_dataset_integration_v10_43b as WS

TOOL_VERSION = "v10.43b"
OUTPUT_SUBDIR = ("reports", "research", "dashboard_v10_43b")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "not_actionable": True, "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def gather_state(symbol: str = "BTCUSDT") -> dict[str, Any]:
    base = A.gather_state(symbol)                       # system/data-quality/shadow/...
    rd = CE._repo_root().joinpath("reports", "research")
    try:
        ws_view = WS.ws_forward_dataset_view(symbol)
    except Exception:
        ws_view = {"verdict": "NO_WS_DATA", "bars_created": 0}
    try:
        compare = WS.dataset_source_compare(symbol)
    except Exception:
        compare = {"recommended_source": "rest", "rest": {}, "ws": {}}
    strat_summary = A._read_json(rd / "strategy_lab_v10_43b" / "strategy_rejection_reasons_v1043b.json")
    watchlist = A._read_csv(rd / "strategy_lab_v10_43b" / "strategy_incubator_watchlist_v1043b.csv")
    scoreboard = A._read_csv(rd / "strategy_lab_v10_43b" / "strategy_scoreboard_v1043b.csv")
    ws_tour = A._read_json(rd / "shadow_simulation_ws_v10_43b" / "shadow_summary_ws_v1043b.json")
    lead = A._read_json(rd / "strategy_lab_v10_43b" / "lead_lag_report_v1043b.json")
    return {**base, "tool_version": TOOL_VERSION, "ws_view": ws_view,
            "source_compare": compare, "strategy_rejection": strat_summary,
            "strategy_watchlist": watchlist, "strategy_scoreboard": scoreboard,
            "ws_tournament": ws_tour, "lead_lag": lead, **_safety()}


def _panel_ws(d: dict) -> str:
    v = d.get("ws_view", {})
    c = d.get("source_compare", {})
    rest, ws = c.get("rest", {}), c.get("ws", {})
    rows = (
        A._kv("WS trades used", v.get("ws_trades_used")) +
        A._kv("WS bars created", v.get("bars_created")) +
        A._kv("WS max contiguous run", v.get("max_contiguous_run"),
              A._state_kind("ok" if (v.get("max_contiguous_run") or 0) >= 60 else "warn")) +
        A._kv("WS forward coverage", A._pct(v.get("forward_coverage"))) +
        A._kv("WS file age (min)", v.get("ws_file_age_min")) +
        A._kv("WS reliability", v.get("reliability"), A._state_kind(v.get("reliability"))) +
        A._kv("WS verdict", v.get("verdict"), A._state_kind(v.get("verdict"))))
    cmp = (f'<table class="tbl"><tr><th>Source</th><th>Bars</th><th>Max run</th>'
           f'<th>Coverage</th></tr>'
           f'<tr><td>REST (V10.32)</td><td>{rest.get("bars","N/A")}</td>'
           f'<td>{rest.get("max_contiguous_run","N/A")}</td>'
           f'<td>{A._pct(rest.get("coverage"))}</td></tr>'
           f'<tr><td>WS (v10.42)</td><td>{ws.get("bars","N/A")}</td>'
           f'<td>{ws.get("max_contiguous_run","N/A")}</td>'
           f'<td>{A._pct(ws.get("coverage"))}</td></tr></table>'
           f'<div class="sub">recommended_source: '
           f'{A._badge(c.get("recommended_source","rest"), "warn")}</div>')
    return rows + '<div style="margin-top:8px"></div>' + cmp


def _panel_strategy(d: dict) -> str:
    rej = d.get("strategy_rejection") or {}
    counts = rej.get("verdict_counts")
    board = d.get("strategy_scoreboard", [])
    if not board:
        return '<div class="empty">WAITING_FOR_STRATEGY_LAB — run autonomous-strategy-lab-v1043b</div>'
    best = board[0]
    return (
        A._kv("Candidates generated", len(board)) +
        (A._kv("Verdict counts", counts) if counts else "") +
        A._kv("Best strategy", best.get("strategy_name")) +
        A._kv("Best family", best.get("family")) +
        A._kv("Best verdict", best.get("verdict"), A._state_kind(best.get("verdict"))) +
        A._kv("Best net_EV", best.get("net_EV")) +
        A._kv("Best net_EV_lower_bound", best.get("net_EV_lower_bound")) +
        '<div class="sub">top rejection reasons: ' +
        html.escape(", ".join(f"{r.get('reason')}({r.get('count')})"
                              for r in (rej.get("top_rejection_reasons") or [])[:3]) or "N/A") +
        '</div>')


def _panel_watchlist(d: dict) -> str:
    wl = d.get("strategy_watchlist", [])
    if not wl:
        return '<div class="empty">No WATCHLIST / INCUBATE candidate yet — nothing survived cost + baseline + lower-bound gates</div>'
    rows = "".join(
        f'<tr><td>{html.escape(str(r.get("strategy_name")))}</td>'
        f'<td>{html.escape(str(r.get("family")))}</td><td>{html.escape(str(r.get("side")))}</td>'
        f'<td>{r.get("sample_size")}</td><td>{r.get("net_EV")}</td>'
        f'<td>{r.get("net_EV_lower_bound")}</td>'
        f'<td>{A._badge(r.get("verdict"), A._state_kind(r.get("verdict")))}</td></tr>'
        for r in wl[:12])
    return (f'<table class="tbl"><tr><th>Strategy</th><th>Family</th><th>Side</th>'
            f'<th>N</th><th>net_EV</th><th>lower_bound</th><th>Verdict</th></tr>{rows}</table>')


def _panel_lattice(d: dict) -> str:
    v = d.get("ws_view", {})
    tour = d.get("ws_tournament") or {}
    best = (tour.get("best_strategy") or {})
    n = best.get("n_signals") or 0
    # explicit unreliable states
    state = None
    if not v.get("bars_created"):
        state = "NO_WS_DATA"
    elif v.get("ws_stale"):
        state = "STALE"
    elif v.get("reliability") == "NOT_RELIABLE_GAPS" or v.get("verdict") == "TOO_GAPPY":
        state = "DATA_GAP"
    elif n < 20:
        state = "INSUFFICIENT_SAMPLE"
    cells = []
    for label, key in (("TP", "tp_count"), ("SL", "sl_count"),
                       ("TIME", "time_count"), ("NO_TRADE", None)):
        if state or key is None:
            val = state or "N/A"
        else:
            c = best.get(key)
            val = f"{(c/n*100):.0f}%" if (c is not None and n) else "N/A"
        cells.append(f'<div class="cell"><div class="cell-h">{label}</div>'
                     f'<div class="cell-v" style="font-size:13px">{html.escape(str(val))}</div></div>')
    note = ("outcome distribution unavailable — " + state
            if state else "WS best-policy outcome distribution (SIM, research-only)")
    return f'<div class="lattice">{"".join(cells)}</div><div class="sub">{html.escape(note)}</div>'


def _panel_graph(d: dict) -> str:
    lead = d.get("lead_lag") or {}
    multi = lead.get("multi_symbol_lead_lag", "WAITING_DATA")
    return (A._relationship_graph(d.get("ws_view", {}).get("verdict")) +
            f'<div class="sub">lead-lag: {html.escape(str(lead.get("verdict","WAITING_DATA")))} · '
            f'multi-symbol: {html.escape(str(multi))} (no invented correlations)</div>')


def render_html(d: dict) -> str:
    # reuse A's base render, then append the V10.43B panels before </div></body>
    base = A.render_html(d)
    extra = _EXTRA.format(
        ws=_panel_ws(d), strat=_panel_strategy(d), wl=_panel_watchlist(d),
        lattice=_panel_lattice(d), graph=_panel_graph(d),
        gen=html.escape(datetime.now(timezone.utc).isoformat()))
    # inject before the closing footer/wrap
    marker = '<div class="foot">'
    if marker in base:
        base = base.replace(marker, extra + marker, 1)
    else:
        base = base.replace("</body>", extra + "</body>", 1)
    # relabel A -> B header
    base = base.replace("V10.43A DASHBOARD", "V10.43B DASHBOARD")
    return base


_EXTRA = """
<div class="grid" style="margin-top:14px">
  <div class="card wide"><h3>WS Data Integration</h3>{ws}</div>
  <div class="card"><h3>Strategy Factory</h3>{strat}</div>
  <div class="card full"><h3>Incubator Watchlist</h3>{wl}</div>
  <div class="card"><h3>Probability Lattice (WS)</h3>{lattice}</div>
  <div class="card wide"><h3>Relationship Graph</h3>{graph}</div>
</div>
<div class="sub" style="text-align:center;margin-top:8px">V10.43B generated {gen} · RESEARCH_ONLY · NO LIVE</div>
"""


def build_dashboard(symbol: str = "BTCUSDT", state: dict | None = None,
                    out_dir=None, write: bool = True) -> dict[str, Any]:
    data = state if state is not None else gather_state(symbol)
    data = {**data, **_safety()}
    result = {"tool_version": TOOL_VERSION, "mode": "RESEARCH_ONLY", **_safety()}
    if write:
        from pathlib import Path
        d = Path(out_dir) if out_dir is not None else CE._repo_root().joinpath(*OUTPUT_SUBDIR)
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(render_html(data), encoding="utf-8")
        tmp = d / "dashboard_data_v10_43b.json.tmp"
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, d / "dashboard_data_v10_43b.json")
        result["html"] = str((d / "index.html")).replace("\\", "/")
        result["json"] = str((d / "dashboard_data_v10_43b.json")).replace("\\", "/")
        result["url"] = "file:///" + result["html"].lstrip("/")
    else:
        result["html_str"] = render_html(data)
    result["ws_verdict"] = data.get("ws_view", {}).get("verdict")
    result["readiness"] = data.get("readiness", {}).get("primary")
    return result
