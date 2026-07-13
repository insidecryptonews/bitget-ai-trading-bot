"""V10.46 integrated dashboard (RESEARCH ONLY).

Pure rendering: `render(report)` turns a pre-computed integrated report dict
into a self-contained HTML string. It does NO heavy replay and makes NO
execution calls — it only reads already-computed results. Every page shows
NO LIVE and can_send_real_orders=false.

Sections: Overview, Market, Decision, Position, Tournament, Learning, Reports.
"""

from __future__ import annotations

import html
import json
from typing import Any

from . import safety_banner


def _esc(x: Any) -> str:
    return html.escape(str(x))


def _kv(label: str, value: Any) -> str:
    return (f'<div class="kv"><span class="k">{_esc(label)}</span>'
            f'<span class="v">{_esc(value)}</span></div>')


def _section(title: str, body: str) -> str:
    return f'<section><h2>{_esc(title)}</h2>{body}</section>'


def render(report: dict) -> str:
    """Render the integrated dashboard HTML from a computed report dict.
    Never runs the tournament; only formats results."""
    prov = report.get("provenance", {})
    safety = report.get("safety", {})
    market = report.get("market", {})
    decision = report.get("decision", {})
    position = report.get("position", {})
    tourn = report.get("tournament", {})
    learning = report.get("learning", {})
    verdict = report.get("verdict", "")

    overview = (
        _kv("Mode", safety.get("mode", "REPLAY/SIM/SHADOW/PAPER RESEARCH ONLY")) +
        _kv("NO LIVE", "YES — LIVE_TRADING=False") +
        _kv("can_send_real_orders", safety.get("can_send_real_orders", False)) +
        _kv("commit", prov.get("repo_commit")) +
        _kv("tree_oid", prov.get("tree_oid")) +
        _kv("dataset_generation", prov.get("data_generation_id")) +
        _kv("output_manifest_sha", prov.get("output_manifest_sha256")) +
        _kv("seal_match", prov.get("seal_match")) +
        _kv("collectors", prov.get("collectors", "public data generations")) +
        _kv("replay/shadow/paper", prov.get("run_modes", "replay")))

    mkt = "".join(_kv(k, market.get(k)) for k in
                  ("regime", "trend", "volatility", "flow", "liquidations",
                   "oi", "funding", "order_book", "cross_venue",
                   "move_consumed")) or "<div class='sub'>no market snapshot</div>"

    dec = "".join(_kv(k, decision.get(k)) for k in
                  ("agents_for", "agents_against", "veto", "abstention",
                   "calibrated_probability", "entry", "invalidation",
                   "target", "expiry")) or "<div class='sub'>no decision</div>"

    pos = "".join(_kv(k, position.get(k)) for k in
                  ("exposure_eur", "margin_eur", "leverage", "notional_eur",
                   "planned_max_loss_eur", "gross_pnl_eur", "costs_eur",
                   "net_pnl_eur", "mfe", "mae", "reason")) \
        or "<div class='sub'>flat / no open position</div>"

    rows = []
    for name, m in (tourn.get("participants") or {}).items():
        rows.append(
            f"<tr><td>{_esc(name)}</td><td>{_esc(m.get('trades'))}</td>"
            f"<td>{_esc(m.get('net_pnl_eur'))}</td>"
            f"<td>{_esc(m.get('ev_per_trade_eur'))}</td>"
            f"<td>{_esc(m.get('n_eff'))}</td>"
            f"<td>{_esc(m.get('max_drawdown_eur'))}</td>"
            f"<td>{_esc(m.get('brier'))}</td></tr>")
    ttable = ("<table><tr><th>participant</th><th>trades</th><th>net €</th>"
              "<th>EV/trade €</th><th>n_eff</th><th>maxDD €</th><th>Brier</th></tr>"
              + "".join(rows) + "</table>")
    paired = tourn.get("paired", {}).get("B_vs_A", {})
    tourn_body = (_kv("champion", tourn.get("champion")) +
                  _kv("paired B vs A mean € (per event)", paired.get("mean_diff_eur")) +
                  _kv("paired B vs A lower bound €", paired.get("lower_bound_eur")) +
                  _kv("promotion_status", tourn.get("promotion_status")) +
                  ttable)

    learn = (_kv("last autopsy cause", learning.get("last_cause")) +
             _kv("last lesson", learning.get("lesson")) +
             _kv("mutation", learning.get("mutation")) +
             _kv("accepted/rejected", learning.get("mutation_status")) +
             _kv("memory composition", learning.get("memory")) +
             _kv("challenger Brier", learning.get("challenger_brier")))

    reports = "".join(_kv(k, v) for k, v in (report.get("reports") or {}).items()) \
        or "<div class='sub'>see reports/research/v10_46_final_integrated</div>"

    body = (
        _section("Overview", overview) +
        _section("Market", mkt) +
        _section("Decision", dec) +
        _section("Position", pos) +
        _section("Tournament", tourn_body) +
        _section("Learning", learn) +
        _section("Reports", reports) +
        f'<div class="verdict">{_esc(verdict)}</div>')

    css = """
    :root{--bg:#0f1117;--fg:#e6e6e6;--mut:#8a90a2;--acc:#4da3ff;--ok:#3ecf8e;
    --bad:#ff6b6b;--card:#171a23}
    @media (prefers-color-scheme:light){:root{--bg:#f6f7f9;--fg:#1a1d24;
    --mut:#5b6270;--card:#ffffff}}
    *{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,sans-serif;
    background:var(--bg);color:var(--fg)}
    header{padding:16px 20px;background:var(--card);border-bottom:1px solid #0003}
    header h1{margin:0;font-size:18px}
    .banner{color:var(--ok);font-weight:600;margin-top:6px;font-size:13px}
    main{padding:16px 20px;display:grid;grid-template-columns:repeat(auto-fit,
    minmax(320px,1fr));gap:14px;max-width:1400px}
    section{background:var(--card);border:1px solid #0002;border-radius:10px;
    padding:14px 16px;overflow-x:auto}
    section h2{margin:0 0 10px;font-size:14px;color:var(--acc);
    text-transform:uppercase;letter-spacing:.05em}
    .kv{display:flex;justify-content:space-between;gap:10px;padding:3px 0;
    border-bottom:1px dashed #0001}
    .k{color:var(--mut)}.v{font-variant-numeric:tabular-nums;text-align:right}
    .sub{color:var(--mut);font-size:12px}
    table{width:100%;border-collapse:collapse;font-size:12px}
    th,td{padding:4px 6px;text-align:right;border-bottom:1px solid #0001}
    th:first-child,td:first-child{text-align:left}
    .verdict{grid-column:1/-1;background:var(--card);border-radius:10px;
    padding:14px 16px;font-weight:600;border:1px solid var(--acc)}
    """
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>V10.46 Integrated Research Dashboard</title>"
            f"<style>{css}</style></head><body>"
            f"<header><h1>Bitget AI Trading Bot — V10.46 Integrated Research</h1>"
            f"<div class='banner'>{_esc(safety_banner())}</div></header>"
            f"<main>{body}</main></body></html>")


def build_dashboard(report: dict, out_path) -> str:
    """Write the dashboard HTML atomically via the verified data layer's
    safe writer and return the path."""
    from ..public_data_backfill_v10_45_1 import safe_atomic_write
    from pathlib import Path
    html_str = render(report)
    out_path = Path(out_path)
    safe_atomic_write(out_path, html_str.encode("utf-8"))
    return str(out_path)
