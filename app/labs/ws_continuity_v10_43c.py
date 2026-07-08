"""ResearchOps V10.43C - WS Continuity + 3-way source comparison (research only).

Reads the NEW persistent WS dataset (bybit_trades_ws_persistent_v10_43c), builds
bars, and measures whether it is actually more continuous than the V10.42 WS and
the V10.32 REST datasets. The comparator is explicit that a `recommended_source`
is NOT the same as "ready": if a WS source wins on contiguity but is still gappy,
it emits the blocker WS_TOO_GAPPY_FOR_SHADOW_FORWARD.

Fail-closed on missing/empty/corrupt data. No network, no orders, no keys, NO LIVE.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE
from . import data_reliability_v10_42 as DR
from . import ws_dataset_integration_v10_43b as WS
from . import bybit_trades_ws_persistent_v10_43c as PWS

TOOL_VERSION = "v10.43c"
OUTPUT_SUBDIR = ("reports", "research", "ws_continuity_v10_43c")
SOURCE_EXCHANGE = "bybit_linear_ws_persistent"
SOURCE_DATASET = "ws_persistent_v10_43c"
STALE_MIN = 15

CONTINUITY_VERDICTS = ("NO_WS_DATA", "WS_COLLECTOR_DOWN", "WS_STALE", "WS_TOO_GAPPY",
                       "WS_EXPLORATORY", "WS_IMPROVING",
                       "WS_USABLE_FOR_EXPLORATORY_RESEARCH", "WS_READY_FOR_SHADOW_FORWARD")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "not_actionable": True, "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _persistent_path(base=None):
    root = base if base is not None else CE._repo_root()
    return (root.joinpath(*PWS.WS_PERSISTENT_SUBDIR) / "trades.csv" if base is None
            else root / "trades.csv")


def load_persistent_bars(symbol: str = "BTCUSDT", bar_seconds: int = 60,
                         base=None) -> dict[str, Any]:
    """Read the persistent WS trades.csv robustly and build bars (no lookahead;
    available_at = bar close). Never raises on bad data."""
    p = _persistent_path(base)
    meta: dict[str, Any] = {"exists": p.is_file(), "n_trades_raw": 0,
                            "n_trades_used": 0, "dropped_rows": 0,
                            "ws_file_age_min": None, "source_exchange": SOURCE_EXCHANGE,
                            "source_dataset": SOURCE_DATASET}
    if not p.is_file() or p.is_symlink():
        return {"bars": [], "meta": meta}
    rows: list[dict] = []
    dropped = 0
    try:
        with open(p, "r", newline="", encoding="utf-8", errors="ignore") as f:
            for r in csv.DictReader(f):
                meta["n_trades_raw"] += 1
                try:
                    if str(r.get("symbol", "")).upper() != symbol.upper():
                        continue
                    int(float(r["timestamp"])); float(r["price"]); float(r["size"])
                    rows.append(r)
                except (KeyError, TypeError, ValueError):
                    dropped += 1
    except Exception:
        return {"bars": [], "meta": {**meta, "read_error": True}}
    meta["dropped_rows"] = dropped
    meta["n_trades_used"] = len(rows)
    meta["ws_file_age_min"] = round(
        (datetime.now(timezone.utc).timestamp() - p.stat().st_mtime) / 60, 1)
    bars = CE.build_bars_from_trades(rows, bar_seconds, symbol)
    for b in bars:
        b["source_exchange"] = SOURCE_EXCHANGE
        b["source_dataset"] = SOURCE_DATASET
    return {"bars": bars, "meta": meta}


def ws_persistent_health(symbol: str = "BTCUSDT", base=None) -> dict[str, Any]:
    """Collector health from the persistent collector's own health.json, enriched
    with dataset file stats (size, age)."""
    h = PWS.read_health(base=base)
    p = _persistent_path(base)
    if p.is_file():
        stt = p.stat()
        h["current_file_size"] = stt.st_size
        h["dataset_file_age_min"] = round(
            (datetime.now(timezone.utc).timestamp() - stt.st_mtime) / 60, 1)
        h["write_path"] = str(p).replace("\\", "/")
    else:
        h["dataset_file_age_min"] = None
    h["symbol"] = symbol
    return h


def _forward_metrics(symbol: str, bars: list[dict]) -> dict[str, Any]:
    view = DR.forward_dataset_view(symbol, bars=bars)
    dq = DR.data_quality_gate(view)
    return {"bars": len(bars),
            "forward_bars": view.get("forward_n_bars", 0),
            "max_contiguous_run": view.get("forward_max_contiguous_run_bars") or 0,
            "coverage": view.get("forward_coverage_ratio"),
            "verdict": view.get("forward_verdict"),
            "reliability": dq.get("tournament_result_reliability"),
            "fit_for_fine_backtest": bool(view.get("fit_for_fine_backtest")),
            "fit_for_shadow_forward": bool(view.get("fit_for_shadow_forward"))}


def ws_continuity_audit(symbol: str = "BTCUSDT", base=None) -> dict[str, Any]:
    """Continuity verdict for the persistent WS dataset, on the honest ladder from
    NO_WS_DATA up to WS_READY_FOR_SHADOW_FORWARD. `improving` compares the
    persistent contiguity against the older V10.42 WS dataset."""
    loaded = load_persistent_bars(symbol, base=base)
    bars, meta = loaded["bars"], loaded["meta"]
    m = _forward_metrics(symbol, bars)
    health = ws_persistent_health(symbol, base=base)
    # reference: the older 60s-cycle WS dataset (to detect real improvement)
    try:
        ref = WS.load_ws_bars(symbol)["bars"]
        ref_run = DR.forward_dataset_view(symbol, bars=ref).get("forward_max_contiguous_run_bars") or 0
    except Exception:
        ref_run = 0
    improving = bool(bars) and m["max_contiguous_run"] > ref_run
    stale = (meta["ws_file_age_min"] is not None and meta["ws_file_age_min"] > STALE_MIN)
    down = health.get("status") in ("DISCONNECTED", "NO_DATA")
    cov = m["coverage"] or 0.0
    coarse_ok = m["forward_bars"] >= 90 and m["max_contiguous_run"] >= 60
    if not bars:
        verdict = "NO_WS_DATA"
    elif down and stale:
        verdict = "WS_COLLECTOR_DOWN"
    elif stale:
        verdict = "WS_STALE"
    elif m["fit_for_shadow_forward"]:
        verdict = "WS_READY_FOR_SHADOW_FORWARD"
    elif coarse_ok:
        verdict = "WS_USABLE_FOR_EXPLORATORY_RESEARCH"
    elif cov < 0.60:
        verdict = "WS_IMPROVING" if improving else "WS_TOO_GAPPY"
    else:
        verdict = "WS_EXPLORATORY"
    gaps = _gap_summary(bars)
    return {"tool_version": TOOL_VERSION, "symbol": symbol, "source": SOURCE_DATASET,
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "trades": meta["n_trades_used"], "trades_raw": meta["n_trades_raw"],
            "dropped_rows": meta["dropped_rows"], "bars": m["bars"],
            "forward_bars": m["forward_bars"],
            "max_contiguous_run": m["max_contiguous_run"],
            "forward_coverage": m["coverage"], "reliability": m["reliability"],
            "fit_for_fine_backtest": m["fit_for_fine_backtest"],
            "fit_for_shadow_forward": m["fit_for_shadow_forward"],
            "gap_count": gaps["gap_count"], "recent_gaps": gaps["recent_gaps"],
            "ws_file_age_min": meta["ws_file_age_min"], "ws_stale": stale,
            "collector_status": health.get("status"),
            "reconnect_count": health.get("reconnect_count"),
            "ref_v1042_max_run": ref_run, "improving_vs_v1042": improving,
            "verdict": verdict, **_safety()}


def _gap_summary(bars: list[dict], gap_bars: int = 2) -> dict[str, Any]:
    seg = DR.segment_dataset(bars)
    fwd = seg.get("forward") or []
    gaps = []
    for i in range(1, len(fwd)):
        gm = (fwd[i]["ts"] - fwd[i - 1]["ts"]) // 60_000
        if gm > gap_bars:
            gaps.append({"after_ts": fwd[i - 1]["ts"], "gap_minutes": int(gm)})
    return {"gap_count": len(gaps), "recent_gaps": gaps[-5:]}


def dataset_source_compare_3way(symbol: str = "BTCUSDT", base=None) -> dict[str, Any]:
    """Compare REST (V10.32) vs WS (V10.42) vs WS-persistent (V10.43C) on forward
    continuity, recommend a source, and — crucially — surface explicit blockers so
    `recommended_source` is never mistaken for 'ready for shadow-forward'."""
    try:
        rest_bars = CE.load_dataset(symbol).get("bars") or []
    except Exception:
        rest_bars = []
    try:
        ws_bars = WS.load_ws_bars(symbol)["bars"]
    except Exception:
        ws_bars = []
    pers_bars = load_persistent_bars(symbol, base=base)["bars"]
    rest = _forward_metrics(symbol, rest_bars)
    ws = _forward_metrics(symbol, ws_bars)
    pers = _forward_metrics(symbol, pers_bars)
    ranked = sorted(
        [("ws_persistent", pers), ("ws", ws), ("rest", rest)],
        key=lambda kv: kv[1]["max_contiguous_run"], reverse=True)
    recommended, rec_m = ranked[0]
    # if nothing has data at all -> rest by default (still fail-closed downstream)
    if rec_m["max_contiguous_run"] == 0 and not pers_bars and not ws_bars:
        recommended, rec_m = "rest", rest
    blockers: list[str] = []
    if not pers_bars:
        blockers.append("WS_PERSISTENT_NOT_YET_COLLECTED")
    if not ws_bars and not pers_bars:
        blockers.append("NO_WS_DATA")
    if recommended in ("ws", "ws_persistent") and not rec_m["fit_for_shadow_forward"]:
        if (rec_m["coverage"] or 0) < 0.60 or rec_m.get("verdict") == "TOO_GAPPY" \
                or rec_m.get("reliability") == "NOT_RELIABLE_GAPS":
            blockers.append("WS_TOO_GAPPY_FOR_SHADOW_FORWARD")
        else:
            blockers.append("WS_INSUFFICIENT_FOR_SHADOW_FORWARD")
    ready = bool(rec_m["fit_for_shadow_forward"])
    return {"tool_version": TOOL_VERSION, "symbol": symbol,
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "rest": rest, "ws": ws, "ws_persistent": pers,
            "recommended_source": recommended,
            "recommended_max_run": rec_m["max_contiguous_run"],
            "ready_for_shadow_forward": ready,
            "blockers": blockers,
            "note": ("recommended_source is the MOST CONTINUOUS dataset, NOT a "
                     "readiness signal; readiness requires ready_for_shadow_forward=true "
                     "and an empty blocker list"),
            **_safety()}


def write_reports(symbol: str = "BTCUSDT") -> str:
    import json
    import os
    d = CE._repo_root().joinpath(*OUTPUT_SUBDIR)
    d.mkdir(parents=True, exist_ok=True)
    audit = ws_continuity_audit(symbol)
    compare = dataset_source_compare_3way(symbol)
    health = ws_persistent_health(symbol)
    for name, obj in (("ws_continuity_audit_v1043c", audit),
                      ("dataset_source_compare_v1043c", compare),
                      ("ws_persistent_health_v1043c", health)):
        tmp = d / (name + ".json.tmp")
        tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, d / (name + ".json"))
    return str(d).replace("\\", "/")
