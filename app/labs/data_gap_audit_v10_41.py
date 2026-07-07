"""ResearchOps V10.41 - Data Gap Audit (research only, NO LIVE).

Measures EXACTLY how continuous the collected Bybit forward dataset is, so we
know whether it is fit for fine backtesting / shadow-forward simulation. Pure
read-only: builds 1m bars from the collected trades and analyses the timestamp
gaps between consecutive bars. No network, no orders, no keys.
"""

from __future__ import annotations

import csv
import json
import os
import statistics as st
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE

TOOL_VERSION = "v10.41"
OUTPUT_SUBDIR = ("reports", "research", "data_quality")
BAR_MS = 60_000
PC_OFF_GAP_MIN = 60          # a gap >= 60 min is very likely PC off, not cadence
REST_CADENCE_MAX_MIN = 10    # 3-10 min gaps look like REST cluster boundaries


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "edge_validated": False, "not_actionable": True, "no_orders": True,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _stream_rows(symbol: str) -> dict[str, int]:
    repo = CE._repo_root()
    base = repo / "external_data" / "staging" / "bybit_microstructure_v10_32" / "dataset"
    out: dict[str, int] = {}
    for name, fn in (("trades", "trades.csv"), ("orderbook", "orderbook_l2.csv"),
                     ("open_interest", "open_interest.csv"), ("funding", "funding.csv"),
                     ("liquidations", "liquidations.csv")):
        p = base / fn
        if p.is_file() and not p.is_symlink():
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                out[name] = max(0, sum(1 for _ in f) - 1)
        else:
            out[name] = -1                      # missing stream
    return out


def audit(symbol: str = "BTCUSDT", bar_seconds: int = 60,
          bars: list[dict] | None = None) -> dict[str, Any]:
    if bars is None:
        bars = CE.load_dataset(symbol, bar_seconds).get("bars") or []
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "symbol": symbol,
                           "ran_at": datetime.now(timezone.utc).isoformat(),
                           "n_bars": len(bars), **_safety()}
    if len(bars) < 2:
        rep["verdict"] = "NO_DATA"
        return rep
    ts = [b["ts"] for b in bars]
    min_ts, max_ts = min(ts), max(ts)
    span_bars = (max_ts - min_ts) // (bar_seconds * 1000) + 1
    gaps = [(ts[i] - ts[i - 1]) // (bar_seconds * 1000) for i in range(1, len(ts))]
    gap_over_1 = [g for g in gaps if g > 1]
    dist = dict(sorted(Counter(gaps).items())[:12])
    # contiguous runs of 1-min bars
    runs, run = [], 1
    for g in gaps:
        if g == 1:
            run += 1
        else:
            runs.append(run); run = 1
    runs.append(run)
    # gaps by hour-of-day (of the later bar) + PC-off vs cadence classification
    by_hour: Counter = Counter()
    pc_off = cadence = other = 0
    for i in range(1, len(ts)):
        g = gaps[i - 1]
        if g <= 1:
            continue
        by_hour[datetime.fromtimestamp(ts[i] / 1000, timezone.utc).hour] += 1
        gm = g  # minutes (bar_seconds=60)
        if gm >= PC_OFF_GAP_MIN:
            pc_off += 1
        elif gm <= REST_CADENCE_MAX_MIN:
            cadence += 1
        else:
            other += 1
    coverage = round(len(bars) / span_bars, 4) if span_bars else 0.0
    missing_bars = span_bars - len(bars)
    streams = _stream_rows(symbol)
    fine_backtest = coverage >= 0.95 and max(runs) >= 120
    shadow_forward = coverage >= 0.60 and max(runs) >= 60
    rep.update({
        "min_ts_utc": datetime.fromtimestamp(min_ts / 1000, timezone.utc).isoformat(),
        "max_ts_utc": datetime.fromtimestamp(max_ts / 1000, timezone.utc).isoformat(),
        "expected_bars_between_min_max": span_bars,
        "coverage_ratio": coverage, "missing_bars": missing_bars,
        "n_gaps": len(gap_over_1),
        "gap_distribution_min": dist,
        "max_gap_min": max(gaps), "mean_gap_min": round(st.mean(gaps), 2),
        "median_gap_min": st.median(gaps),
        "max_contiguous_run_bars": max(runs),
        "mean_contiguous_run_bars": round(st.mean(runs), 1),
        "n_contiguous_runs": len(runs),
        "gaps_by_hour_utc": dict(sorted(by_hour.items())),
        "gap_cause_estimate": {"pc_off_like_ge60min": pc_off,
                               "rest_cadence_like_le10min": cadence,
                               "other": other},
        "streams_row_counts": streams,
        "streams_missing": [k for k, v in streams.items() if v < 0],
        "fit_for_fine_backtest": fine_backtest,
        "fit_for_shadow_forward": shadow_forward,
        "verdict": ("CONTINUOUS_ENOUGH" if fine_backtest else
                    "USABLE_WITH_GAPS" if shadow_forward else "TOO_GAPPY"),
        "recommendation": ("dataset is clustered (REST ~1000 trades/cycle); a "
                           "continuous 24/7 websocket trade collector would raise "
                           "coverage and remove cadence gaps"),
    })
    return rep


def write_reports(rep: dict) -> str:
    d = CE._repo_root().joinpath(*OUTPUT_SUBDIR)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "data_gap_audit_v1041.json.tmp"
    tmp.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, d / "data_gap_audit_v1041.json")
    (d / "data_gap_audit_v1041.md").write_text(_md(rep), encoding="utf-8")
    return str(d).replace("\\", "/")


def _md(r: dict) -> str:
    if r.get("verdict") == "NO_DATA":
        return "# Data Gap Audit V10.41\n\nNO_DATA.\n\nNO LIVE."
    lines = [
        "# Data Gap Audit (V10.41) — RESEARCH ONLY, NO LIVE", "",
        f"symbol: {r['symbol']} · ran: {r['ran_at']}", "",
        f"- n_bars (1m): **{r['n_bars']}**",
        f"- span (expected bars {r['min_ts_utc']} → {r['max_ts_utc']}): "
        f"**{r['expected_bars_between_min_max']}**",
        f"- coverage: **{r['coverage_ratio']*100:.1f}%** · missing bars: {r['missing_bars']}",
        f"- gaps (>1min): **{r['n_gaps']}** · max gap: {r['max_gap_min']}min · "
        f"mean: {r['mean_gap_min']}min · median: {r['median_gap_min']}min",
        f"- longest contiguous 1m run: **{r['max_contiguous_run_bars']}** bars · "
        f"mean run: {r['mean_contiguous_run_bars']} · runs: {r['n_contiguous_runs']}",
        f"- gap cause estimate: PC-off-like(≥60m)={r['gap_cause_estimate']['pc_off_like_ge60min']} · "
        f"REST-cadence(≤10m)={r['gap_cause_estimate']['rest_cadence_like_le10min']} · "
        f"other={r['gap_cause_estimate']['other']}",
        f"- streams: {r['streams_row_counts']}" +
        (f" · MISSING: {r['streams_missing']}" if r['streams_missing'] else ""),
        f"- gap distribution (min→count): {r['gap_distribution_min']}", "",
        f"**fit_for_fine_backtest: {r['fit_for_fine_backtest']}** · "
        f"**fit_for_shadow_forward: {r['fit_for_shadow_forward']}** · "
        f"verdict: **{r['verdict']}**", "",
        r["recommendation"], "", "**FINAL_RECOMMENDATION=NO LIVE.**"]
    return "\n".join(lines)
