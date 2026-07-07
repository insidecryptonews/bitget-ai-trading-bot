"""ResearchOps V10.42 - Data Reliability core (research only, NO LIVE).

Hardens the DATA layer that the whole research rests on:
  * segment the dataset into historical(backfill) vs forward segments
  * forward-only view (so a 2020 backfill day stops poisoning coverage)
  * data quality gate (is the data fit for coarse research / shadow forward?)
  * collector health / watchdog (freshness, gaps, dups, disk, mixed dataset)
  * gap repair plan (classify gaps; NEVER invents ticks)
  * bottleneck map (aggregate + honest priorities)

Pure/deterministic over injected bars where possible; reads local files only.
No network, no orders, no keys.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics as st
from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE
from . import data_gap_audit_v10_41 as DGA

TOOL_VERSION = "v10.42"
OUTPUT_SUBDIR = ("reports", "research", "reliability")
BAR_MS = 60_000
SEGMENT_SPLIT_MIN = 1440         # a >1-day gap splits a segment
MIXED_SPAN_DAYS = 7             # segments >7 days apart => MIXED_WITH_BACKFILL
STALE_MIN = 15                  # dataset file older than 15 min => stale-ish
COLLECTOR_DOWN_MIN = 45         # no fresh data for 45 min => collector down/PC off


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "not_actionable": True, "no_orders": True,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


# ==========================================================================
# Segmentation + forward-only view
# ==========================================================================

def _empty_segment_contract() -> dict[str, Any]:
    """Full, fail-closed contract for an empty dataset / unknown symbol so no
    downstream caller ever hits a KeyError."""
    return {"segments_meta": [], "forward": [], "n_segments": 0,
            "total_n_bars": 0, "forward_n_bars": 0, "span_days": 0.0,
            "mixed_with_backfill": False,
            "global_min_ts": None, "global_max_ts": None,
            "forward_min_ts": None, "forward_max_ts": None,
            "forward_coverage": 0.0, "max_contiguous_run": 0, "gap_count": 0,
            "status": "NO_DATA"}


def segment_dataset(bars: list[dict] | None) -> dict[str, Any]:
    """Split bars into contiguous-ish segments wherever a >1-day gap occurs.
    The most recent segment is the FORWARD segment; older ones are backfill.
    Empty / missing input returns the full NO_DATA contract (never KeyError)."""
    if not bars:
        return _empty_segment_contract()
    b = sorted(bars, key=lambda x: x["ts"])
    segs: list[list[dict]] = []
    cur = [b[0]]
    for i in range(1, len(b)):
        if (b[i]["ts"] - b[i - 1]["ts"]) // BAR_MS > SEGMENT_SPLIT_MIN:
            segs.append(cur); cur = [b[i]]
        else:
            cur.append(b[i])
    segs.append(cur)
    forward = segs[-1]
    span_days = (b[-1]["ts"] - b[0]["ts"]) / 86_400_000
    mixed = len(segs) > 1 and span_days > MIXED_SPAN_DAYS
    meta = [{"start_utc": datetime.fromtimestamp(s[0]["ts"] / 1000, timezone.utc).isoformat(),
             "end_utc": datetime.fromtimestamp(s[-1]["ts"] / 1000, timezone.utc).isoformat(),
             "n_bars": len(s)} for s in segs]
    return {"segments_meta": meta, "n_segments": len(segs), "forward": forward,
            "forward_n_bars": len(forward), "total_n_bars": len(b),
            "span_days": round(span_days, 2), "mixed_with_backfill": mixed,
            "global_min_ts": b[0]["ts"], "global_max_ts": b[-1]["ts"],
            "forward_min_ts": forward[0]["ts"], "forward_max_ts": forward[-1]["ts"],
            "forward_coverage": None, "max_contiguous_run": None, "gap_count": None,
            "status": "OK"}


def forward_dataset_view(symbol: str = "BTCUSDT", bars: list[dict] | None = None
                         ) -> dict[str, Any]:
    """Global vs FORWARD-ONLY continuity metrics. Uses the V10.41 gap audit on
    the forward segment only, so old backfill stops distorting coverage."""
    if bars is None:
        bars = CE.load_dataset(symbol).get("bars") or []
    seg = segment_dataset(bars)
    global_audit = DGA.audit(symbol, bars=bars)
    fwd_audit = DGA.audit(symbol, bars=seg["forward"]) if seg["forward"] else {"verdict": "NO_DATA"}
    total = seg["total_n_bars"]
    fwd_bars = seg["forward_n_bars"]
    fit_fine = bool(fwd_audit.get("fit_for_fine_backtest"))
    fit_shadow = bool(fwd_audit.get("fit_for_shadow_forward"))
    status = ("NO_DATA" if total == 0 else
              "INSUFFICIENT_FORWARD_DATA" if fwd_bars < 90 else
              fwd_audit.get("verdict") or "UNKNOWN")
    return {"tool_version": TOOL_VERSION, "symbol": symbol,
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "n_segments": seg["n_segments"], "segments_meta": seg["segments_meta"],
            "mixed_with_backfill": seg["mixed_with_backfill"],
            "total_n_bars": total, "forward_n_bars": fwd_bars,
            "global_coverage_ratio": global_audit.get("coverage_ratio"),
            "global_verdict": global_audit.get("verdict"),
            "forward_coverage_ratio": fwd_audit.get("coverage_ratio"),
            "forward_max_contiguous_run_bars": fwd_audit.get("max_contiguous_run_bars"),
            "forward_verdict": fwd_audit.get("verdict", "NO_DATA"),
            "forward_fit_for_shadow_forward": fit_shadow,
            "fit_for_fine_backtest": fit_fine, "fit_for_shadow_forward": fit_shadow,
            "note": ("forward-only metrics ignore historical backfill segments; "
                     "use forward_* for readiness, not global_*"),
            **_safety()}


# ==========================================================================
# Data quality gate
# ==========================================================================

DQ_STATES = ("DATA_OK_FOR_COLLECTION", "DATA_OK_FOR_COARSE_RESEARCH",
             "DATA_NOT_OK_FOR_FINE_BACKTEST", "DATA_NOT_OK_FOR_SHADOW_FORWARD",
             "DATASET_MIXED_WITH_BACKFILL", "TOO_GAPPY", "STALE",
             "INSUFFICIENT_FORWARD_DATA")


def data_quality_gate(view: dict) -> dict[str, Any]:
    """Turn the forward view into explicit fitness flags the tournament/research
    can cite so nobody trusts a result the data cannot support."""
    fwd_bars = view.get("forward_n_bars", 0)
    fwd_cov = view.get("forward_coverage_ratio") or 0.0
    fwd_run = view.get("forward_max_contiguous_run_bars") or 0
    states: list[str] = []
    if view.get("mixed_with_backfill"):
        states.append("DATASET_MIXED_WITH_BACKFILL")
    if fwd_bars < 90:
        states.append("INSUFFICIENT_FORWARD_DATA")
    if fwd_cov and fwd_cov < 0.60:
        states.append("TOO_GAPPY")
    ok_collection = fwd_bars >= 60
    ok_coarse = fwd_bars >= 90 and fwd_run >= 60
    ok_fine = fwd_cov >= 0.95 and fwd_run >= 120
    ok_shadow = view.get("forward_fit_for_shadow_forward") is True
    if ok_collection:
        states.append("DATA_OK_FOR_COLLECTION")
    if ok_coarse:
        states.append("DATA_OK_FOR_COARSE_RESEARCH")
    if not ok_fine:
        states.append("DATA_NOT_OK_FOR_FINE_BACKTEST")
    if not ok_shadow:
        states.append("DATA_NOT_OK_FOR_SHADOW_FORWARD")
    reliability = ("USABLE" if ok_shadow else
                   "EXPLORATORY" if ok_coarse else
                   "NOT_RELIABLE_GAPS" if fwd_cov and fwd_cov < 0.6 else
                   "NOT_RELIABLE_SAMPLE")
    return {"tool_version": TOOL_VERSION, "states": sorted(set(states)),
            "tournament_result_reliability": reliability,
            "fit_for_fine_backtest": ok_fine, "fit_for_shadow_forward": ok_shadow,
            **_safety()}


# ==========================================================================
# Collector health / watchdog (freshness-based; avoids process self-match)
# ==========================================================================

HEALTH_STATES = ("HEALTHY", "DEGRADED", "STALE", "TOO_GAPPY", "COLLECTOR_DOWN",
                 "DATASET_MIXED", "API_BACKOFF", "RATE_LIMIT_RISK",
                 "INSUFFICIENT_FORWARD_DATA", "NO_DATA", "UNKNOWN")


def collector_health(symbol: str = "BTCUSDT") -> dict[str, Any]:
    repo = CE._repo_root()
    base = repo / "external_data" / "staging" / "bybit_microstructure_v10_32" / "dataset"
    trades = base / "trades.csv"
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "symbol": symbol,
                           "ran_at": datetime.now(timezone.utc).isoformat(), **_safety()}
    if not trades.is_file():
        rep.update({"status": "COLLECTOR_DOWN", "reason": "no trades dataset"})
        return rep
    file_age_min = (datetime.now(timezone.utc).timestamp() - trades.stat().st_mtime) / 60
    view = forward_dataset_view(symbol)
    # disk
    du = shutil.disk_usage(str(base))
    disk_free_gb = round(du.free / 1024 ** 3, 1)
    # duplicate + source consistency (reuse audit streams)
    dq = data_quality_gate(view)
    states: list[str] = []
    if file_age_min > COLLECTOR_DOWN_MIN:
        states.append("COLLECTOR_DOWN")           # or PC off
    elif file_age_min > STALE_MIN:
        states.append("STALE")
    if view.get("mixed_with_backfill"):
        states.append("DATASET_MIXED")
    if (view.get("forward_coverage_ratio") or 1) < 0.6:
        states.append("TOO_GAPPY")
    if disk_free_gb < 5:
        states.append("RATE_LIMIT_RISK")          # placeholder: low disk = risk
    no_forward = (view.get("forward_n_bars") or 0) == 0
    if no_forward:                                # symbol has no data at all
        states.append("INSUFFICIENT_FORWARD_DATA")
    if not states:
        status = "HEALTHY"
    elif "COLLECTOR_DOWN" in states:
        status = "COLLECTOR_DOWN"
    elif no_forward:
        status = "INSUFFICIENT_FORWARD_DATA"
    elif "STALE" in states:
        status = "STALE"
    else:
        status = "DEGRADED"
    rep.update({
        "status": status, "sub_states": sorted(set(states)),
        "trades_file_age_min": round(file_age_min, 1),
        "collector_fresh": file_age_min <= STALE_MIN,
        "pc_off_or_down_inferred": file_age_min > COLLECTOR_DOWN_MIN,
        "forward_n_bars": view.get("forward_n_bars"),
        "forward_coverage_ratio": view.get("forward_coverage_ratio"),
        "forward_max_contiguous_run_bars": view.get("forward_max_contiguous_run_bars"),
        "mixed_with_backfill": view.get("mixed_with_backfill"),
        "disk_free_gb": disk_free_gb,
        "data_reliability": dq["tournament_result_reliability"],
        "note": ("freshness-based health; process check omitted on purpose to "
                 "avoid PowerShell self-match false positives"),
    })
    return rep


# ==========================================================================
# Gap repair plan (audit / dry-run; never invents ticks)
# ==========================================================================

def gap_repair_plan(symbol: str = "BTCUSDT", bars: list[dict] | None = None,
                    mode: str = "dry-run") -> dict[str, Any]:
    if bars is None:
        bars = CE.load_dataset(symbol).get("bars") or []
    seg = segment_dataset(bars)
    fwd = seg["forward"]
    classes = {"rest_cadence_le10min_UNREPAIRABLE_MICRO": 0,
               "pc_off_ge60min_UNREPAIRABLE_MICRO": 0,
               "backfill_span_gap_EXPECTED": max(0, seg["n_segments"] - 1),
               "full_past_day_REPAIRABLE_VIA_DAILY_DUMP": 0,
               "other": 0}
    if len(fwd) >= 2:
        ts = [x["ts"] for x in fwd]
        for i in range(1, len(ts)):
            gm = (ts[i] - ts[i - 1]) // BAR_MS
            if gm <= 1:
                continue
            if gm <= 10:
                classes["rest_cadence_le10min_UNREPAIRABLE_MICRO"] += 1
            elif gm >= 60:
                classes["pc_off_ge60min_UNREPAIRABLE_MICRO"] += 1
            else:
                classes["other"] += 1
    return {"tool_version": TOOL_VERSION, "symbol": symbol, "mode": mode,
            "gap_classes": classes,
            "apply_supported": False,
            "verdict": "UNREPAIRABLE_MICROSTRUCTURE_GAP",
            "explanation": ("public REST returns only the most recent trades, not "
                            "arbitrary past minutes; past microstructure gaps and "
                            "PC-off periods cannot be back-filled at tick level. "
                            "Only FULL past days are repairable via the official "
                            "daily trade dumps (V10.36). The real fix is going "
                            "forward: a continuous websocket trade collector."),
            "never_invents_ticks": True, **_safety()}


# ==========================================================================
# Bottleneck map (aggregate + honest priorities)
# ==========================================================================

STRATEGY_UNIVERSE = {
    "evaluable_now_on_gappy_data": ["baselines", "burst_momentum", "flow_imbalance",
                                    "trend_follow", "oi_confirmation", "funding_fade"],
    "pending_continuous_data": ["mean_reversion", "liquidation_reversal/continuation",
                                "breakout", "RSI", "EMA/SMA_cross", "Bollinger/squeeze",
                                "volatility_breakout", "candle_patterns_mechanical",
                                "fibonacci_programmatic", "session_time_of_day",
                                "orderbook_pressure", "volume_burst", "anti_chop",
                                "multitimeframe", "cooldown", "dynamic_TP_SL",
                                "trailing", "time_exit"],
    "discarded_noise_for_now": [],
}


def bottleneck_map(symbol: str = "BTCUSDT") -> dict[str, Any]:
    view = forward_dataset_view(symbol)
    dq = data_quality_gate(view)
    health = collector_health(symbol)
    repair = gap_repair_plan(symbol)
    bottlenecks = [
        {"area": "data", "issue": "forward dataset clustered (REST cadence) -> DATA_GAP",
         "status": "DETECTED", "fix": "continuous websocket trade collector (V10.42 B1)"},
        {"area": "data", "issue": "2020 backfill day mixed with 2026 forward",
         "status": "MITIGATED", "fix": "forward-only view separates them (this module)"},
        {"area": "collectors", "issue": "trades via REST ~1000/cycle, not continuous",
         "status": "DETECTED", "fix": "ws publicTrade collector"},
        {"area": "research", "issue": "cost ~18bps > gross ~10-12bps",
         "status": "PENDING", "fix": "cost-crusher after continuous data (B7 designed)"},
        {"area": "validation", "issue": "no validated edge; small forward sample",
         "status": "PENDING", "fix": "accumulate ~30d continuous + walk-forward"},
    ]
    return {"tool_version": TOOL_VERSION, "symbol": symbol,
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "data_quality_gate": dq["states"],
            "tournament_reliability": dq["tournament_result_reliability"],
            "collector_status": health["status"],
            "forward_verdict": view.get("forward_verdict"),
            "gap_repair_verdict": repair["verdict"],
            "bottlenecks": bottlenecks,
            "strategy_universe": STRATEGY_UNIVERSE,
            "top_priority": "continuous websocket trade collector (removes DATA_GAP)",
            **_safety()}


# ==========================================================================
# report writers
# ==========================================================================

def write_json_md(name: str, obj: dict, md: str) -> str:
    d = CE._repo_root().joinpath(*OUTPUT_SUBDIR)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (name + ".json.tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, d / (name + ".json"))
    (d / (name + ".md")).write_text(md, encoding="utf-8")
    return str(d).replace("\\", "/")
