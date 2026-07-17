"""ResearchOps V10.43A - Trading Research Command Center (dashboard builder).

Generates a dark, self-contained HTML dashboard (no CDN, no network, no server)
from the REAL research reports (V10.42 reliability + V10.40 shadow tournament).
100% honest and research-only: missing data shows WAITING/N/A/blocker states,
never fabricated PnL / win-rate / AI-confidence. NO LIVE, no orders, no keys.
"""

from __future__ import annotations

import csv
import html
import json
import os
from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE
from . import data_reliability_v10_42 as DR

TOOL_VERSION = "v10.43a"
OUTPUT_SUBDIR = ("reports", "research", "dashboard_v10_43a")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "not_actionable": True, "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _git_head() -> str:
    try:
        g = CE._repo_root() / ".git"
        head = (g / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            return (g / ref).read_text(encoding="utf-8").strip()[:10]
        return head[:10]
    except Exception:
        return "unknown"


def _read_json(path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_csv(path) -> list[dict]:
    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _ws_dataset_meta() -> dict:
    p = CE._repo_root() / "external_data" / "staging" / "bybit_trades_ws_v10_42" / "trades.csv"
    if not p.is_file():
        return {"exists": False}
    st = p.stat()
    age_min = (datetime.now(timezone.utc).timestamp() - st.st_mtime) / 60
    return {"exists": True, "size_kb": round(st.st_size / 1024, 1),
            "age_min": round(age_min, 1)}


# ==========================================================================
# State gathering (fail-closed; every source optional)
# ==========================================================================

def gather_state(symbol: str = "BTCUSDT") -> dict[str, Any]:
    rd = CE._repo_root().joinpath("reports", "research")
    # live cheap reads (dataset-based); never raise
    try:
        health = DR.collector_health(symbol)
    except Exception:
        health = {"status": "UNKNOWN", "sub_states": []}
    try:
        view = DR.forward_dataset_view(symbol)
    except Exception:
        view = {"status": "NO_DATA", "forward_n_bars": 0}
    try:
        dq = DR.data_quality_gate(view)
    except Exception:
        dq = {"states": ["INSUFFICIENT_FORWARD_DATA"],
              "tournament_result_reliability": "NOT_RELIABLE_SAMPLE"}
    # heavier reports: read from disk if present
    shadow = _read_json(rd / "shadow_simulation" / "shadow_summary_v1040.json")
    scoreboard = _read_csv(rd / "shadow_simulation" / "shadow_scoreboard_v1040.csv")
    bankroll = _read_json(rd / "shadow_simulation" / "shadow_bankroll_20eur_v1040.json")
    ati_health = _read_json(rd / "ati" / "ati_health.json")
    ati_summary = _read_json(rd / "ati" / "ati_summary.json")
    ati_forward = _read_json(rd / "ati" / "ati_forward_state.json")
    readiness = _readiness(view, dq, shadow)
    return {"tool_version": TOOL_VERSION, "symbol": symbol,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_head": _git_head(),
            "health": health, "view": view, "data_quality": dq,
            "shadow": shadow, "scoreboard": scoreboard, "bankroll": bankroll,
            "ati": {"health": ati_health, "summary": ati_summary,
                    "forward": ati_forward},
            "ws_dataset": _ws_dataset_meta(),
            "readiness": readiness, **_safety()}


def _readiness(view: dict, dq: dict, shadow: dict | None) -> dict:
    states = ["RESEARCH_ONLY"]
    fwd = view.get("forward_n_bars") or 0
    verdict = view.get("forward_verdict")
    reliability = dq.get("tournament_result_reliability")
    if fwd == 0 or view.get("status") == "NO_DATA":
        states.append("DATA_NOT_READY")
    if fwd and fwd < 90:
        states.append("BLOCKED_BY_SMALL_SAMPLE")
    if verdict == "TOO_GAPPY" or reliability == "NOT_RELIABLE_GAPS":
        states.append("BLOCKED_BY_DATA_GAP")
    if shadow is not None and shadow.get("any_strategy_beats_baseline_and_costs") is False:
        states.append("BLOCKED_BY_NEGATIVE_EV")
    primary = ("DATA_NOT_READY" if "DATA_NOT_READY" in states else
               "BLOCKED_BY_DATA_GAP" if "BLOCKED_BY_DATA_GAP" in states else
               "BLOCKED_BY_SMALL_SAMPLE" if "BLOCKED_BY_SMALL_SAMPLE" in states else
               "BLOCKED_BY_NEGATIVE_EV" if "BLOCKED_BY_NEGATIVE_EV" in states else
               "RESEARCH_ONLY")
    return {"primary": primary, "states": states, "micro_live_ready": False}


# ==========================================================================
# HTML render (dark command center, self-contained, no CDN)
# ==========================================================================

def _badge(text: str, kind: str) -> str:
    return f'<span class="badge {kind}">{html.escape(str(text))}</span>'


def _state_kind(status: str) -> str:
    s = (status or "").upper()
    if s in ("HEALTHY", "USABLE", "OK", "CONTINUOUS_ENOUGH", "DATA_OK"):
        return "ok"
    if s in ("DEGRADED", "STALE", "EXPLORATORY", "USABLE_WITH_GAPS", "WAITING_DATA"):
        return "warn"
    if s in ("TOO_GAPPY", "COLLECTOR_DOWN", "NOT_RELIABLE_GAPS", "NOT_RELIABLE_SAMPLE",
             "BLOCKED_BY_DATA_GAP", "DATA_NOT_READY"):
        return "bad"
    return "muted"


def _kv(label: str, value: Any, kind: str = "") -> str:
    v = "N/A" if value is None else html.escape(str(value))
    cls = f" val-{kind}" if kind else ""
    return f'<div class="kv"><span class="k">{html.escape(label)}</span><span class="v{cls}">{v}</span></div>'


def render_html(d: dict) -> str:
    h = d.get("health", {})
    v = d.get("view", {})
    dq = d.get("data_quality", {})
    sh = d.get("shadow")
    board = d.get("scoreboard", [])
    bank = d.get("bankroll")
    ati = d.get("ati") or {}
    ws = d.get("ws_dataset", {})
    rd = d.get("readiness", {})

    # --- system status panel
    coll_status = h.get("status", "UNKNOWN")
    ws_status = "DATA_OK" if ws.get("exists") else "WAITING_DATA"
    sys_rows = (
        _kv("Collector (research dataset)", coll_status, _state_kind(coll_status)) +
        _kv("Bybit Micro fresh", h.get("collector_fresh")) +
        _kv("Bybit Trades WS", ws_status, _state_kind(ws_status)) +
        _kv("WS ticks written", f"{ws.get('size_kb','N/A')} KB" if ws.get("exists") else "waiting") +
        _kv("Trades file age (min)", h.get("trades_file_age_min")) +
        _kv("Security", "SAFE_PAPER_ONLY", "ok") +
        _kv("can_send_real_orders", "false", "ok"))

    # --- data quality panel
    dqk = _state_kind(v.get("forward_verdict"))
    dq_rows = (
        _kv("Forward bars", v.get("forward_n_bars")) +
        _kv("Total bars", v.get("total_n_bars")) +
        _kv("REST forward coverage", _pct(v.get("forward_coverage_ratio"))) +
        _kv("Max contiguous run", v.get("forward_max_contiguous_run_bars")) +
        _kv("Forward verdict", v.get("forward_verdict"), dqk) +
        _kv("Reliability", dq.get("tournament_result_reliability"),
            _state_kind(dq.get("tournament_result_reliability"))) +
        _kv("Mixed w/ backfill", v.get("mixed_with_backfill")) +
        _kv("Fit fine backtest", v.get("fit_for_fine_backtest")) +
        _kv("Fit shadow forward", v.get("fit_for_shadow_forward")))
    dq_bar = _coverage_bar(v.get("forward_coverage_ratio"))

    # --- market snapshot (only symbols with real reports; BTC live)
    snap = (
        f'<tr><td>{html.escape(d.get("symbol","BTCUSDT"))}</td>'
        f'<td>{_badge(v.get("forward_verdict","N/A"), dqk)}</td>'
        f'<td>{v.get("forward_n_bars","N/A")}</td>'
        f'<td>{v.get("forward_max_contiguous_run_bars","N/A")}</td>'
        f'<td>{_badge("false","muted")}</td></tr>')
    for s in ("ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"):
        snap += (f'<tr><td>{s}</td><td>{_badge("WAITING_DATA","warn")}</td>'
                 f'<td>N/A</td><td>N/A</td><td>{_badge("false","muted")}</td></tr>')

    # --- strategy tournament
    if sh:
        best = sh.get("best_strategy") or {}
        tour = (
            _kv("Policies tested", sh.get("policies_total")) +
            _kv("Any beats baseline+costs", sh.get("any_strategy_beats_baseline_and_costs"),
                "bad" if sh.get("any_strategy_beats_baseline_and_costs") is False else "") +
            _kv("Best policy", best.get("policy")) +
            _kv("Best verdict", best.get("verdict"), _state_kind(best.get("verdict"))) +
            _kv("Best net_EV", best.get("net_EV")) +
            _kv("Best net_EV_lower_bound", best.get("net_EV_lower_bound")) +
            _kv("Best profit_factor", best.get("profit_factor")) +
            _kv("Best win_rate", best.get("win_rate")) +
            _kv("Best max_drawdown", best.get("max_drawdown")) +
            _kv("Best sample size", best.get("n_signals")) +
            _kv("micro_live_ready", sh.get("micro_live_ready"), "bad"))
    else:
        tour = '<div class="empty">NO RELIABLE TOURNAMENT YET — run shadow-simulation-tournament-v1040</div>'

    # --- 20 EUR shadow bankroll
    if bank and bank.get("profiles"):
        rows = "".join(
            f'<tr><td>{html.escape(p)}</td><td>{vv.get("final_eur")}€</td>'
            f'<td>{vv.get("return_pct")}%</td><td>{vv.get("max_drawdown_pct")}%</td>'
            f'<td>{vv.get("n_trades")}</td><td>{_badge("false","muted")}</td></tr>'
            for p, vv in bank["profiles"].items())
        bankp = (f'<div class="sub">Initial: {bank.get("start_eur","20")}€ (FAKE / SIM)</div>'
                 f'<table class="tbl"><tr><th>Profile</th><th>Final</th><th>Return</th>'
                 f'<th>Max DD</th><th>Trades</th><th>Defensible</th></tr>{rows}</table>')
    else:
        bankp = '<div class="empty">WAITING_FOR_SHADOW_BANKROLL_REPORT</div>'

    # --- probability lattice (real TP/SL/TIME from best policy, else N/A)
    lattice = _lattice(sh)

    ati_health = ati.get("health") or {}
    ati_summary = ati.get("summary") or {}
    ati_forward = ati.get("forward") or {}
    ati_overall = ati_summary.get("overall_baseline") or {}
    ati_has_summary = bool(ati_summary)
    ati_card = (
        _kv("Engine", ati_health.get("status") or "NO_DATA",
            _state_kind(ati_health.get("status") or "NO_DATA")) +
        _kv("Evidence", ati_summary.get("status") or "INSUFFICIENT_DATA") +
        _kv("Historical signals", ati_health.get("signals_total") if ati_has_summary else "N/A") +
        _kv("Forward signals", ati_forward.get("signals_total") if ati_forward else "N/A") +
        _kv("Closed forward outcomes", ati_forward.get("closed_outcomes") if ati_forward else "N/A") +
        _kv("Open simulated positions", ati_forward.get("open_positions") if ati_forward else "N/A") +
        _kv("Net EV after costs", ati_overall.get("net_ev")) +
        _kv("Profit factor", ati_overall.get("profit_factor")) +
        _kv("Win rate", ati_overall.get("win_rate")) +
        _kv("Average MFE", ati_overall.get("average_mfe")) +
        _kv("Average MAE", ati_overall.get("average_mae")) +
        _kv("Max drawdown", ati_overall.get("max_drawdown")) +
        _kv("Fees / slippage / funding",
            f"{ati_overall.get('fees')} / {ati_overall.get('slippage')} / {ati_overall.get('funding')}") +
        _kv("Policy / feature",
            f"{(ati_summary.get('policy') or {}).get('policy_version')} / {(ati_summary.get('policy') or {}).get('feature_version')}") +
        _kv("Dataset source", ati_summary.get("dataset_source_mode") or "N/A") +
        _kv("Dataset last bar", ati_health.get("dataset_last_bar_at")) +
        _kv("Observer", ati_forward.get("observer_status") or "NOT_RUNNING") +
        _kv("Cache", ati_forward.get("cache_status") or "STALE_UNKNOWN") +
        _kv("Existing strategy comparison", "SEPARATE BASELINES; NO PROMOTION") +
        _kv("can_send_real_orders", "false", "ok") +
        _kv("Final recommendation", "NO LIVE", "bad")
    )

    # --- relationship graph
    graph = _relationship_graph(v.get("forward_verdict"))

    # --- readiness gate
    prim = rd.get("primary", "RESEARCH_ONLY")
    gate_kind = _state_kind(prim)
    gate_badges = "".join(_badge(s, _state_kind(s)) for s in rd.get("states", []))

    # --- next action / logs
    memo_p = CE._repo_root().joinpath("reports", "research", "shadow_simulation",
                                      "shadow_research_memo_v1040.md")
    next_actions = [
        "Keep the Bybit collectors running (Micro + WS) while the PC is on.",
        "Wait for forward coverage / max contiguous run to grow (needs continuous WS ticks).",
        "Re-run collector-health-v1042 and shadow-simulation-tournament-v1040 periodically.",
        "No strategy candidate yet — nothing is actionable.",
    ]
    logs = "".join(f"<li>{html.escape(a)}</li>" for a in next_actions)
    memo_note = ("shadow_research_memo_v1040.md present"
                 if memo_p.is_file() else "no tournament memo yet")

    return _PAGE.format(
        css=_CSS, symbol=html.escape(d.get("symbol", "BTCUSDT")),
        git=html.escape(d.get("git_head", "unknown")),
        generated=html.escape(d.get("generated_at", "")),
        sys_rows=sys_rows, dq_rows=dq_rows, dq_bar=dq_bar, snap=snap,
        tour=tour, bankp=bankp, lattice=lattice, graph=graph,
        ati_card=ati_card,
        gate_primary=html.escape(prim), gate_kind=gate_kind,
        gate_badges=gate_badges, logs=logs, memo_note=html.escape(memo_note))


def _pct(x) -> str:
    return "N/A" if x is None else f"{x*100:.1f}%"


def _coverage_bar(cov) -> str:
    if cov is None:
        return '<div class="bar"><div class="bar-fill muted" style="width:0%"></div>' \
               '<span class="bar-lbl">N/A</span></div>'
    pctv = max(0.0, min(1.0, cov)) * 100
    kind = "ok" if cov >= 0.95 else "warn" if cov >= 0.6 else "bad"
    return (f'<div class="bar"><div class="bar-fill {kind}" style="width:{pctv:.0f}%"></div>'
            f'<span class="bar-lbl">{cov*100:.1f}% forward coverage</span></div>')


def _lattice(sh: dict | None) -> str:
    best = (sh or {}).get("best_strategy") or {}
    n = best.get("n_signals") or 0
    cells = []
    for label, key in (("TP", "tp_count"), ("SL", "sl_count"),
                       ("TIME", "time_count"), ("TRAIL", "trail_count")):
        c = best.get(key)
        frac = f"{(c/n*100):.0f}%" if (c is not None and n) else "N/A"
        cells.append(f'<div class="cell"><div class="cell-h">{label}</div>'
                     f'<div class="cell-v">{frac}</div></div>')
    note = ("outcome distribution of the best shadow policy (SIM, research-only)"
            if best else
            "Probability lattice unavailable — insufficient validated data")
    grid = "".join(cells) if best else \
        "".join(f'<div class="cell"><div class="cell-h">{l}</div>'
                f'<div class="cell-v">N/A</div></div>'
                for l in ("TP", "SL", "TIME", "NO_TRADE"))
    return f'<div class="lattice">{grid}</div><div class="sub">{html.escape(note)}</div>'


def _relationship_graph(btc_verdict) -> str:
    btc_kind = _state_kind(btc_verdict)
    fill = {"ok": "#39d98a", "warn": "#f2b134", "bad": "#f2555a", "muted": "#5b6472"}
    # BTC hub + alt nodes (alts WAITING_DATA -> no invented correlations/edges)
    hub = fill.get(btc_kind, "#5b6472")
    alts = [("ETH", 140, 60), ("SOL", 300, 60), ("XRP", 140, 170), ("DOGE", 300, 170)]
    edges = "".join(
        f'<line x1="220" y1="115" x2="{x}" y2="{y}" stroke="#2a3340" '
        f'stroke-width="1" stroke-dasharray="4 4"/>' for _, x, y in alts)
    nodes = "".join(
        f'<circle cx="{x}" cy="{y}" r="16" fill="#141a22" stroke="#5b6472"/>'
        f'<text x="{x}" y="{y+4}" text-anchor="middle" fill="#8b94a3" '
        f'font-size="10">{name}</text>' for name, x, y in alts)
    return (
        '<svg viewBox="0 0 440 230" width="100%" height="200">'
        f'{edges}'
        f'<circle cx="220" cy="115" r="26" fill="#141a22" stroke="{hub}" stroke-width="2"/>'
        f'<text x="220" y="119" text-anchor="middle" fill="#e6ebf2" font-size="12">BTC</text>'
        f'{nodes}</svg>'
        '<div class="sub">Alt nodes WAITING_DATA — lead-lag / correlation engine not '
        'built yet (no invented edges).</div>')


def build_dashboard(symbol: str = "BTCUSDT", state: dict | None = None,
                    out_dir=None, write: bool = True) -> dict[str, Any]:
    data = state if state is not None else gather_state(symbol)
    data = {**data, **_safety()}                # safety contract always present
    d = out_dir if out_dir is not None else CE._repo_root().joinpath(*OUTPUT_SUBDIR)
    result = {"tool_version": TOOL_VERSION, "mode": "RESEARCH_ONLY", **_safety()}
    if write:
        from pathlib import Path
        d = Path(d)
        d.mkdir(parents=True, exist_ok=True)
        html_str = render_html(data)
        (d / "index.html").write_text(html_str, encoding="utf-8")
        tmp = d / "dashboard_data_v10_43a.json.tmp"
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, d / "dashboard_data_v10_43a.json")
        result["html"] = str((d / "index.html")).replace("\\", "/")
        result["json"] = str((d / "dashboard_data_v10_43a.json")).replace("\\", "/")
        result["url"] = "file:///" + result["html"].lstrip("/")
    else:
        result["html_str"] = render_html(data)
    result["readiness"] = data.get("readiness", {}).get("primary")
    return result


# ==========================================================================
# Embedded CSS + page template (no external fonts/CDN)
# ==========================================================================

_CSS = """
:root{--bg:#0a0e14;--panel:#111823;--panel2:#0e141d;--line:#1e2733;--txt:#e6ebf2;
--muted:#8b94a3;--ok:#39d98a;--warn:#f2b134;--bad:#f2555a;--accent:#5b8def;}
*{box-sizing:border-box}body{margin:0;background:
radial-gradient(1200px 600px at 20% -10%,#101a2b 0%,var(--bg) 60%);
color:var(--txt);font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:13px}
.wrap{max-width:1280px;margin:0 auto;padding:18px}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;
border:1px solid var(--line);background:linear-gradient(180deg,#121b28,#0d1420);
border-radius:14px;padding:14px 18px;margin-bottom:14px}
.brand{font-size:18px;font-weight:700;letter-spacing:.3px}
.brand small{display:block;color:var(--muted);font-size:11px;font-weight:400;letter-spacing:2px}
.chips{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}
.card{grid-column:span 4;border:1px solid var(--line);background:var(--panel);
border-radius:14px;padding:14px;min-height:120px;min-width:0}
.card.wide{grid-column:span 8}.card.full{grid-column:span 12}
.card h3{margin:0 0 10px;font-size:12px;letter-spacing:1.5px;color:var(--muted);
text-transform:uppercase;border-bottom:1px solid var(--line);padding-bottom:8px}
.kv{display:flex;justify-content:space-between;gap:10px;min-width:0;padding:3px 0;border-bottom:1px dashed #141c27}
.kv .k{color:var(--muted);min-width:0}.kv .v{min-width:0;font-variant-numeric:tabular-nums;overflow-wrap:anywhere;word-break:break-word;text-align:right}
.val-ok{color:var(--ok)}.val-warn{color:var(--warn)}.val-bad{color:var(--bad)}.val-muted{color:var(--muted)}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;
font-weight:600;border:1px solid}
.badge.ok{color:var(--ok);border-color:#1c5c40;background:#0e2419}
.badge.warn{color:var(--warn);border-color:#5c4a1c;background:#241d0e}
.badge.bad{color:var(--bad);border-color:#5c1c1f;background:#240e10}
.badge.muted{color:var(--muted);border-color:#2a3340;background:#121822}
.tbl{width:100%;border-collapse:collapse;font-size:12px}
.tbl th{text-align:left;color:var(--muted);font-weight:500;padding:5px 6px;border-bottom:1px solid var(--line)}
.tbl td{padding:5px 6px;border-bottom:1px solid #131a25;font-variant-numeric:tabular-nums}
.empty{color:var(--muted);font-style:italic;padding:14px 4px}
.sub{color:var(--muted);font-size:11px;margin-top:8px}
.bar{position:relative;height:22px;background:#0c121b;border:1px solid var(--line);
border-radius:6px;overflow:hidden;margin:8px 0}
.bar-fill{position:absolute;left:0;top:0;bottom:0}
.bar-fill.ok{background:#173d2b}.bar-fill.warn{background:#3d331a}.bar-fill.bad{background:#3d1a1c}.bar-fill.muted{background:#1a2230}
.bar-lbl{position:absolute;left:8px;top:3px;font-size:11px;color:var(--txt)}
.lattice{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:6px}
.cell{border:1px solid var(--line);border-radius:8px;background:#0d141d;padding:10px;text-align:center}
.cell-h{color:var(--muted);font-size:11px;letter-spacing:1px}
.cell-v{font-size:18px;font-weight:700;margin-top:4px}
.gate{grid-column:span 4;border-radius:14px;padding:16px;border:1px solid;min-width:0}
.gate.ok{border-color:#1c5c40;background:linear-gradient(180deg,#0f2a1e,#0c1a14)}
.gate.warn{border-color:#5c4a1c;background:linear-gradient(180deg,#2a220f,#1a150c)}
.gate.bad{border-color:#5c1c1f;background:linear-gradient(180deg,#2a0f11,#1a0c0d)}
.gate.muted{border-color:#2a3340;background:#0f1620}
.gate .big{font-size:20px;font-weight:800;letter-spacing:.5px;margin:6px 0 10px;overflow-wrap:anywhere;word-break:break-word}
ul.next{margin:6px 0 0;padding-left:18px}ul.next li{margin:4px 0;color:#c6cdd8}
.foot{color:var(--muted);text-align:center;margin:16px 0 4px;font-size:11px}
@media(max-width:900px){.card,.card.wide,.card.full,.gate{grid-column:span 12}}
"""

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bitget AI Trading Research Bot — Command Center V10.43A</title>
<style>{css}</style></head><body><div class="wrap">
<div class="top">
  <div class="brand">Bitget AI Trading Research Bot
    <small>V10.43A DASHBOARD · TRADING RESEARCH COMMAND CENTER</small></div>
  <div class="chips">
    <span class="badge muted">MODE: PAPER / RESEARCH</span>
    <span class="badge ok">SAFE_PAPER_ONLY</span>
    <span class="badge bad">NO LIVE</span>
    <span class="badge muted">HEAD {git}</span>
  </div>
</div>

<div class="grid">
  <div class="card"><h3>System Status</h3>{sys_rows}
    <div class="sub">Generated {generated} · RESEARCH_ONLY</div></div>

  <div class="card"><h3>Data Quality</h3>{dq_bar}{dq_rows}</div>

  <div class="gate {gate_kind}"><h3 style="color:inherit;border-color:#0003">Readiness Gate</h3>
    <div class="big">{gate_primary}</div>
    <div class="chips">{gate_badges}</div>
    <div class="sub">Every advance is gated on data + evidence. Today: research only, NO LIVE.</div>
  </div>

  <div class="card wide"><h3>Market Snapshot</h3>
    <table class="tbl"><tr><th>Symbol</th><th>Verdict</th><th>Fwd bars</th>
    <th>Max run</th><th>Actionable</th></tr>{snap}</table>
    <div class="sub">Only BTCUSDT has a research dataset; other symbols WAITING_DATA (not invented).</div></div>

  <div class="card"><h3>20 EUR Shadow Bankroll</h3>{bankp}</div>

  <div class="card"><h3>Strategy Tournament</h3>{tour}</div>

  <div class="card"><h3>Probability Lattice</h3>{lattice}</div>

  <div class="card"><h3>Adrian Trading Intelligence — Shadow</h3>{ati_card}
    <div class="sub">SHADOW ONLY · next-bar-open · costs included · no auto-promotion</div></div>

  <div class="card"><h3>Relationship Graph</h3>{graph}</div>

  <div class="card full"><h3>Logs / Next Action</h3>
    <ul class="next">{logs}</ul>
    <div class="sub">{memo_note} · RESEARCH_ONLY · edge_validated=false · can_send_real_orders=false · FINAL_RECOMMENDATION=NO LIVE</div>
  </div>
</div>
<div class="foot">Honest research dashboard — no fabricated PnL / win-rate / AI-confidence.
If data is missing it shows WAITING / N/A / blocker. NO LIVE.</div>
</div></body></html>"""
