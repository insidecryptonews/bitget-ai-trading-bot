"""ResearchOps V10.43B - WS Dataset Integration (research only, NO LIVE).

Bridges the CONTINUOUS Bybit websocket trade dataset
(external_data/staging/bybit_trades_ws_v10_42/trades.csv) into the research
pipeline so health / forward-view / tournament / strategy-lab can use continuous
ticks instead of the clustered REST V10.32 dataset.

Robust and fail-closed: missing/empty/corrupt/out-of-order/duplicate/unknown
symbol all handled without KeyError. Bars carry source tags and available_at is
the bar close (no lookahead). No network, no orders, no keys.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE
from . import data_reliability_v10_42 as DR
from . import data_gap_audit_v10_41 as DGA

TOOL_VERSION = "v10.43b"
WS_SUBDIR = ("external_data", "staging", "bybit_trades_ws_v10_42")
OUTPUT_SUBDIR = ("reports", "research", "ws_integration_v10_43b")
SOURCE_EXCHANGE = "bybit_linear_ws"
SOURCE_DATASET = "ws_v10_42"
STALE_MIN = 15


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "not_actionable": True, "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _ws_path(base=None):
    root = base if base is not None else CE._repo_root()
    return root.joinpath(*WS_SUBDIR) / "trades.csv"


def load_ws_bars(symbol: str = "BTCUSDT", bar_seconds: int = 60,
                 base=None) -> dict[str, Any]:
    """Read the WS trades.csv, drop corrupt rows, filter symbol, build bars.
    Returns {bars, meta}. Never raises on bad data."""
    p = _ws_path(base)
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
                    # validate the fields build_bars_from_trades needs
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
    bars = CE.build_bars_from_trades(rows, bar_seconds, symbol)   # sorts + dedups path
    for b in bars:                                               # source tags
        b["source_exchange"] = SOURCE_EXCHANGE
        b["source_dataset"] = SOURCE_DATASET
    return {"bars": bars, "meta": meta}


def ws_forward_dataset_view(symbol: str = "BTCUSDT", base=None) -> dict[str, Any]:
    """Forward-only continuity view computed on the WS (continuous) bars."""
    loaded = load_ws_bars(symbol, base=base)
    bars, meta = loaded["bars"], loaded["meta"]
    view = DR.forward_dataset_view(symbol, bars=bars)
    dq = DR.data_quality_gate(view)
    stale = (meta["ws_file_age_min"] is not None and meta["ws_file_age_min"] > STALE_MIN)
    verdict = ("NO_WS_DATA" if not bars else
               "WS_STALE" if stale else
               view.get("forward_verdict") or "UNKNOWN")
    return {"tool_version": TOOL_VERSION, "symbol": symbol, "source": "ws_v10_42",
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "ws_trades_used": meta["n_trades_used"],
            "ws_trades_raw": meta["n_trades_raw"], "dropped_rows": meta["dropped_rows"],
            "ws_file_age_min": meta["ws_file_age_min"], "ws_stale": stale,
            "bars_created": len(bars),
            "forward_bars": view.get("forward_n_bars"),
            "forward_coverage": view.get("forward_coverage_ratio"),
            "max_contiguous_run": view.get("forward_max_contiguous_run_bars"),
            "mixed_with_backfill": view.get("mixed_with_backfill"),
            "fit_for_fine_backtest": view.get("fit_for_fine_backtest"),
            "fit_for_shadow_forward": view.get("fit_for_shadow_forward"),
            "reliability": dq.get("tournament_result_reliability"),
            "data_quality_gate": dq.get("states"),
            "verdict": verdict, **_safety()}


def _source_metrics(symbol: str, bars: list[dict]) -> dict[str, Any]:
    # FORWARD-ONLY metrics so the 2020 backfill day / 6-year span never inflate
    # the comparison (apples-to-apples continuity of the most recent segment).
    view = DR.forward_dataset_view(symbol, bars=bars)
    return {"bars": len(bars),
            "forward_bars": view.get("forward_n_bars", 0),
            "max_contiguous_run": view.get("forward_max_contiguous_run_bars") or 0,
            "coverage": view.get("forward_coverage_ratio"),
            "verdict": view.get("forward_verdict"),
            "fit_for_shadow_forward": bool(view.get("fit_for_shadow_forward"))}


def dataset_source_compare(symbol: str = "BTCUSDT", base=None) -> dict[str, Any]:
    """Compare the REST (V10.32 clustered) vs WS (continuous) datasets and
    recommend which source research should prefer."""
    try:
        rest_bars = CE.load_dataset(symbol).get("bars") or []
    except Exception:
        rest_bars = []
    ws_bars = load_ws_bars(symbol, base=base)["bars"]
    rest = _source_metrics(symbol, rest_bars)
    ws = _source_metrics(symbol, ws_bars)
    blockers = []
    if not ws_bars:
        blockers.append("NO_WS_DATA")
    if ws["max_contiguous_run"] and ws["max_contiguous_run"] < 60:
        blockers.append("WS_RUN_TOO_SHORT")
    # prefer WS when it has a clearly longer contiguous run (continuity matters
    # most for forward simulation); else fall back to whichever is less bad
    if ws["max_contiguous_run"] > rest["max_contiguous_run"]:
        recommended = "ws"
    elif rest["max_contiguous_run"] > ws["max_contiguous_run"]:
        recommended = "rest"
    else:
        recommended = "ws" if ws_bars else "rest"
    which_better = ("ws" if ws["max_contiguous_run"] > rest["max_contiguous_run"]
                    else "rest" if rest["max_contiguous_run"] > ws["max_contiguous_run"]
                    else "tie")
    return {"tool_version": TOOL_VERSION, "symbol": symbol,
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "rest": rest, "ws": ws,
            "which_better_by_contiguity": which_better,
            "recommended_source": recommended,
            "blockers": blockers,
            "note": ("continuity (max_contiguous_run) is the deciding metric for "
                     "forward simulation; WS is preferred once its run exceeds REST"),
            **_safety()}
