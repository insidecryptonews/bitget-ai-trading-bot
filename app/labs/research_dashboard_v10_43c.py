"""ResearchOps V10.43C - Persistent WS research dashboard (static/local only).

Adds the V10.43C persistent WS health/continuity layer, the 3-way REST vs WS vs
WS-persistent comparison, hardened strategy lab status and exit optimization.
Everything is local/static/reporting-only: no network, no orders, no keys, NO LIVE.
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
from . import ws_continuity_v10_43c as PWS
from . import strategy_lab_hardening_v10_43c as HARD
from . import exit_optimization_v10_43b as EXIT
from . import autonomous_strategy_lab_v10_43b as LAB

TOOL_VERSION = "v10.43c"
OUTPUT_SUBDIR = ("reports", "research", "dashboard_v10_43c")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "not_actionable": True, "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def gather_state(symbol: str = "BTCUSDT") -> dict[str, Any]:
    base = A.gather_state(symbol)
    try:
        health = PWS.ws_persistent_health(symbol)
    except Exception:
        health = {"status": "NO_DATA"}
    try:
        continuity = PWS.ws_continuity_audit(symbol)
    except Exception:
        continuity = {"verdict": "NO_WS_DATA", "max_contiguous_run": 0}
    try:
        compare = PWS.dataset_source_compare_3way(symbol)
    except Exception:
        compare = {"recommended_source": "rest", "blockers": ["COMPARE_FAILED"],
                   "rest": {}, "ws": {}, "ws_persistent": {}}
    try:
        strategy = HARD.run_hardened_lab(symbol, data_source="ws_persistent", write_reports=True)
    except Exception as e:
        strategy = {"global_verdict": "ERROR", "detail": str(e)[:160], "candidates_generated": 0}
    try:
        tournament = LAB.run_ws_tournament(symbol, source="ws_persistent", write_reports=True)
    except Exception as e:
        tournament = {"verdict": "ERROR", "detail": str(e)[:160], "micro_live_ready": False}
    try:
        exits = EXIT.run_exit_optimization(symbol, data_source="ws_persistent", write_reports=True)
    except Exception as e:
        exits = {"verdict": "ERROR", "detail": str(e)[:160], "watchlist_or_better": 0}
    readiness = _readiness(continuity, compare, strategy, exits)
    return {**base, "tool_version": TOOL_VERSION, "persistent_health": health,
            "persistent_continuity": continuity, "source_compare_3way": compare,
            "strategy_hardening": strategy, "ws_persistent_tournament": tournament,
            "exit_optimization": exits, "readiness_v1043c": readiness, **_safety()}


def _readiness(continuity: dict, compare: dict, strategy: dict, exits: dict) -> dict[str, Any]:
    states = ["RESEARCH_ONLY"]
    blockers = list(compare.get("blockers") or [])
    if continuity.get("verdict") not in ("WS_READY_FOR_SHADOW_FORWARD",):
        states.append("BLOCKED_BY_DATA_GAP")
    if strategy.get("watchlist_or_better", 0) <= 0:
        states.append("BLOCKED_BY_NO_STRATEGY_EDGE")
    if exits.get("watchlist_or_better", 0) <= 0:
        states.append("BLOCKED_BY_NO_EXIT_EDGE")
    primary = "BLOCKED_BY_DATA_GAP" if "BLOCKED_BY_DATA_GAP" in states else "RESEARCH_ONLY_NOT_ACTIONABLE"
    return {"primary": primary, "states": states, "blockers": blockers,
            "micro_live_ready": False, "paper_ready": False, "live_ready": False,
            **_safety()}


def _panel_persistent_ws(d: dict) -> str:
    h = d.get("persistent_health", {})
    c = d.get("persistent_continuity", {})
    return (
        A._kv("Collector status", h.get("status"), A._state_kind(h.get("status"))) +
        A._kv("Connected", h.get("connected")) +
        A._kv("Health file age (s)", h.get("health_file_age_seconds")) +
        A._kv("Dataset file age (min)", h.get("dataset_file_age_min")) +
        A._kv("Messages", h.get("messages_count")) +
        A._kv("Trades", c.get("trades")) +
        A._kv("Bars", c.get("bars")) +
        A._kv("Max contiguous run", c.get("max_contiguous_run")) +
        A._kv("Forward coverage", A._pct(c.get("forward_coverage"))) +
        A._kv("Continuity verdict", c.get("verdict"), A._state_kind(c.get("verdict"))) +
        A._kv("Fit shadow forward", c.get("fit_for_shadow_forward")))


def _panel_compare(d: dict) -> str:
    c = d.get("source_compare_3way", {})
    rows = []
    for name, label in (("rest", "REST"), ("ws", "WS v10.42"),
                        ("ws_persistent", "WS Persistent v10.43C")):
        m = c.get(name, {}) or {}
        rows.append(f'<tr><td>{label}</td><td>{m.get("bars","N/A")}</td>'
                    f'<td>{m.get("max_contiguous_run","N/A")}</td>'
                    f'<td>{A._pct(m.get("coverage"))}</td>'
                    f'<td>{html.escape(str(m.get("verdict","N/A")))}</td></tr>')
    blockers = ", ".join(c.get("blockers") or []) or "none"
    return (f'<table class="tbl"><tr><th>Source</th><th>Bars</th><th>Max run</th>'
            f'<th>Coverage</th><th>Verdict</th></tr>{"".join(rows)}</table>'
            f'<div class="sub">recommended_source: '
            f'{A._badge(c.get("recommended_source"), "warn")} · ready_for_shadow_forward='
            f'{html.escape(str(c.get("ready_for_shadow_forward")))}</div>'
            f'<div class="sub">blockers: {html.escape(blockers)}</div>')


def _panel_strategy(d: dict) -> str:
    s = d.get("strategy_hardening", {})
    best = s.get("best") or {}
    return (
        A._kv("Global verdict", s.get("global_verdict"), A._state_kind(s.get("global_verdict"))) +
        A._kv("Candidates", s.get("candidates_generated")) +
        A._kv("Verdict counts", s.get("verdict_counts")) +
        A._kv("Rejection categories", s.get("rejection_category_counts")) +
        A._kv("Best", best.get("strategy_name")) +
        A._kv("Best verdict", best.get("verdict"), A._state_kind(best.get("verdict"))) +
        A._kv("Best net_EV lower bound", s.get("best_net_EV_lower_bound")) +
        '<div class="sub">ranking: net_EV_lower_bound, robustness, sample, costs/slippage, baseline comparison</div>')


def _panel_exit(d: dict) -> str:
    e = d.get("exit_optimization", {})
    b = e.get("best_variant") or {}
    wl = e.get("winner_loser") or {}
    return (
        A._kv("Verdict", e.get("verdict"), A._state_kind(e.get("verdict"))) +
        A._kv("Entries", e.get("n_entries")) +
        A._kv("Variants x horizons", e.get("variants_x_horizons")) +
        A._kv("Best variant", f'{b.get("exit_variant")}@{b.get("horizon")}') +
        A._kv("Best verdict", b.get("verdict"), A._state_kind(b.get("verdict"))) +
        A._kv("Best net_EV lb", b.get("net_EV_lower_bound")) +
        A._kv("Partial TP model", b.get("partial_tp_model")) +
        A._kv("Avg MFE captured", wl.get("avg_pct_of_MFE_captured")) +
        '<div class="sub">exit variants are exits-only: no leverage, no sizing, no execution.</div>')


def _panel_lattice(d: dict) -> str:
    cont = d.get("persistent_continuity", {})
    tour = d.get("ws_persistent_tournament") or {}
    best = tour.get("best_strategy") or {}
    n = best.get("n_signals") or 0
    if cont.get("verdict") in ("NO_WS_DATA", "WS_TOO_GAPPY", "WS_STALE", "WS_COLLECTOR_DOWN"):
        state = "DATA_GAP" if cont.get("verdict") == "WS_TOO_GAPPY" else cont.get("verdict")
    elif n < 20:
        state = "INSUFFICIENT_SAMPLE"
    else:
        state = None
    cells = []
    for label in ("TP", "SL", "TIME", "NO_TRADE"):
        val = state or "N/A"
        cells.append(f'<div class="cell"><div class="cell-h">{label}</div>'
                     f'<div class="cell-v" style="font-size:13px">{html.escape(str(val))}</div></div>')
    return f'<div class="lattice">{"".join(cells)}</div><div class="sub">Probability lattice is blocked until real contiguous sample exists.</div>'


def render_html(d: dict) -> str:
    base = A.render_html({**d, "readiness": d.get("readiness_v1043c") or d.get("readiness")})
    base = _remove_legacy_probability_lattice(base)
    extra = _EXTRA.format(
        pws=_panel_persistent_ws(d), compare=_panel_compare(d),
        strategy=_panel_strategy(d), exits=_panel_exit(d),
        lattice=_panel_lattice(d),
        graph=A._relationship_graph((d.get("persistent_continuity") or {}).get("verdict")),
        gen=html.escape(datetime.now(timezone.utc).isoformat()))
    marker = '<div class="foot">'
    base = base.replace(marker, extra + marker, 1) if marker in base else base.replace("</body>", extra + "</body>", 1)
    return base.replace("V10.43A DASHBOARD", "V10.43C DASHBOARD")


def _remove_legacy_probability_lattice(rendered: str) -> str:
    """The V10.43A base includes a legacy 0%-filled probability card. V10.43C
    replaces it with a DATA_GAP/STALE/INSUFFICIENT_SAMPLE lattice, so the old
    card is removed to avoid false precision."""
    start = rendered.find('<div class="card"><h3>Probability Lattice</h3>')
    marker = '<div class="sub">outcome distribution of the best shadow policy (SIM, research-only)</div></div>'
    if start < 0:
        return rendered
    end = rendered.find(marker, start)
    if end < 0:
        return rendered
    return rendered[:start] + rendered[end + len(marker):]


_EXTRA = """
<div class="grid" style="margin-top:14px">
  <div class="card wide"><h3>Persistent WS Panel</h3>{pws}</div>
  <div class="card wide"><h3>REST vs WS vs WS Persistent</h3>{compare}</div>
  <div class="card"><h3>Strategy Lab Hardened</h3>{strategy}</div>
  <div class="card"><h3>Exit Optimization Panel</h3>{exits}</div>
  <div class="card"><h3>Probability Lattice</h3>{lattice}</div>
  <div class="card wide"><h3>Relationship Graph</h3>{graph}<div class="sub">No invented correlations. Alt symbols WAITING_DATA until collected.</div></div>
</div>
<div class="sub" style="text-align:center;margin-top:8px">V10.43C generated {gen} · RESEARCH_ONLY · NO LIVE</div>
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
        tmp = d / "dashboard_data_v10_43c.json.tmp"
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, d / "dashboard_data_v10_43c.json")
        result["html"] = str((d / "index.html")).replace("\\", "/")
        result["json"] = str((d / "dashboard_data_v10_43c.json")).replace("\\", "/")
        result["url"] = "file:///" + result["html"].lstrip("/")
    else:
        result["html_str"] = render_html(data)
    result["ws_persistent_verdict"] = data.get("persistent_continuity", {}).get("verdict")
    result["readiness"] = data.get("readiness_v1043c", {}).get("primary")
    return result
