"""ResearchOps V10.24 - Microstructure Sample Adapter (research only, OFFLINE).

Validate and normalize LOCAL microstructure samples (trades / orderbook L2 /
open interest / funding / liquidations) that a human downloaded from a provider.
This module NEVER touches the network, NO API keys, NO paid download, NO DB, NO
raw/production writes. It only READS local files and (optionally) writes
normalized CSVs into the v10_24 staging marker.

It produces a readiness scorecard so a later phase can decide whether
microstructure-aware research labs can be built -- but it builds NO strategy and
promotes nothing. FINAL_RECOMMENDATION: NO LIVE.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import statistics as st
from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE

TOOL_VERSION = "v10.24"
STAGING_MARKER = "microstructure_samples_v10_24"
DEFAULT_SAMPLE_DIR = f"external_data/staging/{STAGING_MARKER}"
OUTPUT_ROOT = "reports/research/v10_24"
DAY_MS = 86_400_000
MIN_HISTORY_DAYS = 30
MAX_ROWS = 2_000_000

# classification verdicts
C_NO_SAMPLE = "NO_SAMPLE"
C_INVALID = "INVALID_SAMPLE"
C_PARTIAL = "PARTIAL_SAMPLE"
C_READY = "MICROSTRUCTURE_RESEARCH_READY"
C_NEEDS_HISTORY = "NEEDS_MORE_HISTORY"
C_NEEDS_ORDERBOOK = "NEEDS_ORDERBOOK"
C_NEEDS_AGGRESSOR = "NEEDS_AGGRESSOR_SIDE"
C_NEEDS_LIQ = "NEEDS_LIQUIDATIONS"
C_NEEDS_OI = "NEEDS_OI"

_FORBIDDEN_SEG = ("raw", "backup", "backups", "vault", "vaults", "training_exports",
                  "secret", "secrets", "credential", "credentials", "db", "database",
                  ".git", "node_modules", "codex_result.md", "code_result.md")
_FORBIDDEN_SUF = (".env", ".db", ".sqlite", ".sqlite3", ".pem", ".key")

_TS_FIELDS = ("timestamp", "ts", "exchange_timestamp", "time", "datetime")
_SIZE_FIELDS = ("size", "qty", "amount", "quantity", "volume")
_SIDE_FIELDS = ("side", "aggressor_side", "taker_side", "maker_taker", "direction")
_OI_FIELDS = ("open_interest", "oi", "openinterest")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "paper_candidate_future": False,
            "makes_no_trades": True, "uses_network": False, "uses_db": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------
# Path safety
# --------------------------------------------------------------------------

def assert_safe_sample_dir(path: str) -> str:
    """Reject reading from dangerous locations (.env/db/raw/backups/vault/traversal)."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("empty sample dir")
    norm = path.replace("\\", "/")
    segs = [s for s in norm.split("/") if s and s not in (".",)]
    if ".." in segs:
        raise ValueError("path traversal blocked")
    for s in (x.lower() for x in segs):
        if s in _FORBIDDEN_SEG or s.endswith(_FORBIDDEN_SUF) or ".env" in s:
            raise ValueError(f"forbidden sample segment: {s}")
    return path


def safe_normalized_dir(run_id: str, base: str | None = None) -> str:
    """Normalized outputs may ONLY be written under the v10_24 staging marker."""
    root = base or DEFAULT_SAMPLE_DIR
    segs = [s for s in root.replace("\\", "/").split("/") if s]
    if ".." in segs or STAGING_MARKER not in segs:
        raise ValueError("normalization must live under the v10_24 staging marker")
    for s in (x.lower() for x in segs):
        if s in _FORBIDDEN_SEG or s.endswith(_FORBIDDEN_SUF):
            raise ValueError(f"forbidden normalized segment: {s}")
    return os.path.join(root, run_id, "normalized").replace("\\", "/")


def _safe_output_base(output_dir: str | None) -> str:
    base = output_dir or OUTPUT_ROOT
    segs = [s for s in base.replace("\\", "/").split("/") if s]
    if ".." in segs:
        return OUTPUT_ROOT
    for s in (x.lower() for x in segs):
        if s in _FORBIDDEN_SEG or s.endswith(_FORBIDDEN_SUF) or ".env" in s:
            return OUTPUT_ROOT
    return base


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def _to_ms(v: Any) -> int | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        n = float(s)
        if n <= 0:
            return None
        if n < 1e11:          # seconds
            return int(n * 1000)
        if n < 1e14:          # milliseconds
            return int(n)
        return int(n / 1000)  # microseconds
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None


def _first(row: dict[str, Any], fields: tuple[str, ...]) -> Any:
    low = {k.lower(): v for k, v in row.items()}
    for f in fields:
        if f in low and str(low[f]).strip() != "":
            return low[f]
    return None


def _norm_side(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("buy", "b", "bid", "long", "taker_buy", "buyer", "1"):
        return "buy"
    if s in ("sell", "s", "ask", "short", "taker_sell", "seller", "-1", "0"):
        return "sell"
    return None


def _read_csv(path: str) -> tuple[list[str], list[dict[str, str]], bool]:
    rows: list[dict[str, str]] = []
    header: list[str] = []
    truncated = False
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            header = [h for h in (reader.fieldnames or [])]
            for i, r in enumerate(reader):
                if i >= MAX_ROWS:
                    truncated = True
                    break
                rows.append(r)
    except Exception:
        return [], [], False
    return header, rows, truncated


# --------------------------------------------------------------------------
# Schema detection
# --------------------------------------------------------------------------

def detect_type(fname: str, header: list[str]) -> str:
    f = fname.lower()
    cols = {c.lower() for c in header}
    if "trade" in f:
        return "trades"
    if "orderbook" in f or "order_book" in f or "_l2" in f or "book" in f:
        return "orderbook"
    if "liquidation" in f or re.search(r"(^|[_\-])liq", f):
        return "liquidations"
    if "funding" in f:
        return "funding"
    if "open_interest" in f or re.search(r"(^|[_\-])oi([_\-.]|$)", f):
        return "oi"
    # column fallback
    if "funding_rate" in cols:
        return "funding"
    if cols & set(_OI_FIELDS):
        return "oi"
    if any(c.startswith("bid_price") for c in cols) or {"bids", "asks"} & cols:
        return "orderbook"
    if "price" in cols and (cols & set(_SIZE_FIELDS)):
        return "trades"
    return "unknown"


def _coverage(ts_list: list[int]) -> dict[str, Any]:
    if not ts_list:
        return {"rows": 0, "coverage_days": 0.0, "first_ts": None, "last_ts": None,
                "monotonic": True, "duplicates": 0, "future_ts": 0}
    s = sorted(ts_list)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) + DAY_MS
    return {"rows": len(ts_list),
            "coverage_days": round((s[-1] - s[0]) / DAY_MS, 2),
            "first_ts": s[0], "last_ts": s[-1],
            "monotonic": all(ts_list[i] <= ts_list[i + 1] for i in range(len(ts_list) - 1)),
            "duplicates": len(ts_list) - len(set(ts_list)),
            "future_ts": sum(1 for t in s if t > now_ms)}


# --------------------------------------------------------------------------
# Per-type validators
# --------------------------------------------------------------------------

def validate_trades(rows: list[dict[str, str]]) -> dict[str, Any]:
    ts, prices, sizes, sides, syms = [], [], [], [], set()
    for r in rows:
        t = _to_ms(_first(r, _TS_FIELDS))
        try:
            p = float(_first(r, ("price",)) or "nan")
            sz = float(_first(r, _SIZE_FIELDS) or "nan")
        except (TypeError, ValueError):
            p = sz = float("nan")
        if t is not None:
            ts.append(t)
        if not math.isnan(p):
            prices.append(p)
        if not math.isnan(sz):
            sizes.append(sz)
        sd = _norm_side(_first(r, _SIDE_FIELDS))
        if sd:
            sides.append(sd)
        sym = _first(r, ("symbol", "instrument", "pair"))
        if sym:
            syms.add(str(sym))
    cov = _coverage(ts)
    has_aggr = len(sides) >= max(1, int(0.5 * len(rows)))
    buys = sides.count("buy")
    sells = sides.count("sell")
    valid = bool(rows) and bool(prices) and all(p > 0 for p in prices[:5000]) and bool(sizes)
    return {"type": "trades", "valid": valid, "has_aggressor_side": has_aggr,
            "symbols": sorted(syms), "coverage": cov,
            "buy_sell_imbalance": round((buys - sells) / (buys + sells), 4) if (buys + sells) else None,
            "trades_per_min": round(len(ts) / max(1.0, cov["coverage_days"] * 1440), 3) if cov["coverage_days"] else None,
            "price_positive": all(p > 0 for p in prices[:5000]), "size_positive": all(s > 0 for s in sizes[:5000])}


def validate_orderbook(rows: list[dict[str, str]]) -> dict[str, Any]:
    ts, spreads, crossed, depth_levels = [], [], 0, 0
    syms = set()
    for r in rows:
        low = {k.lower(): v for k, v in r.items()}
        t = _to_ms(_first(r, _TS_FIELDS))
        if t is not None:
            ts.append(t)
        sym = _first(r, ("symbol", "instrument", "pair"))
        if sym:
            syms.add(str(sym))
        bid = ask = None
        if "bids" in low and "asks" in low:               # nested json string
            try:
                bids = json.loads(low["bids"]); asks = json.loads(low["asks"])
                bid = float(bids[0][0]); ask = float(asks[0][0])
                depth_levels = max(depth_levels, min(len(bids), len(asks)))
            except Exception:
                pass
        else:                                              # flat bid_price_1 / ask_price_1
            try:
                bid = float(low.get("bid_price_1"))
                ask = float(low.get("ask_price_1"))
            except (TypeError, ValueError):
                bid = ask = None
            lv = sum(1 for k in low if k.startswith("bid_price"))
            depth_levels = max(depth_levels, lv)
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            if bid >= ask:
                crossed += 1
            else:
                spreads.append((ask - bid) / ((ask + bid) / 2))
    cov = _coverage(ts)
    valid = bool(rows) and bool(spreads) and crossed == 0
    return {"type": "orderbook", "valid": valid, "symbols": sorted(syms), "coverage": cov,
            "depth_levels": depth_levels, "crossed_book_rows": crossed,
            "spread_median": round(st.median(spreads), 6) if spreads else None,
            "spread_p95": round(sorted(spreads)[int(len(spreads) * 0.95)], 6) if len(spreads) > 5 else None}


def validate_oi(rows: list[dict[str, str]]) -> dict[str, Any]:
    ts, vals, syms = [], 0, set()
    for r in rows:
        t = _to_ms(_first(r, _TS_FIELDS))
        if t is not None:
            ts.append(t)
        if _first(r, _OI_FIELDS) is not None:
            vals += 1
        sym = _first(r, ("symbol", "instrument", "pair"))
        if sym:
            syms.add(str(sym))
    cov = _coverage(ts)
    missing = round(1 - vals / len(rows), 4) if rows else 1.0
    return {"type": "oi", "valid": bool(rows) and vals > 0, "symbols": sorted(syms),
            "coverage": cov, "missing_ratio": missing}


def validate_funding(rows: list[dict[str, str]]) -> dict[str, Any]:
    ts, vals, syms = [], 0, set()
    for r in rows:
        t = _to_ms(_first(r, _TS_FIELDS))
        if t is not None:
            ts.append(t)
        low = {k.lower(): v for k, v in r.items()}
        if str(low.get("funding_rate", "")).strip() != "":
            vals += 1
        sym = _first(r, ("symbol", "instrument", "pair"))
        if sym:
            syms.add(str(sym))
    cov = _coverage(ts)
    return {"type": "funding", "valid": bool(rows) and vals > 0, "symbols": sorted(syms),
            "coverage": cov, "missing_ratio": round(1 - vals / len(rows), 4) if rows else 1.0}


def validate_liquidations(rows: list[dict[str, str]]) -> dict[str, Any]:
    ts, sides, notional_ok, syms = [], [], 0, set()
    for r in rows:
        t = _to_ms(_first(r, _TS_FIELDS))
        if t is not None:
            ts.append(t)
        sd = _norm_side(_first(r, _SIDE_FIELDS))
        if sd:
            sides.append(sd)
        low = {k.lower(): v for k, v in r.items()}
        try:
            if str(low.get("notional", "")).strip() != "":
                notional_ok += 1
            else:
                p = float(_first(r, ("price",)) or "nan")
                sz = float(_first(r, _SIZE_FIELDS) or "nan")
                if not math.isnan(p) and not math.isnan(sz):
                    notional_ok += 1
        except (TypeError, ValueError):
            pass
        sym = _first(r, ("symbol", "instrument", "pair"))
        if sym:
            syms.add(str(sym))
    cov = _coverage(ts)
    return {"type": "liquidations", "valid": bool(rows) and bool(sides), "symbols": sorted(syms),
            "coverage": cov, "side_valid": bool(sides), "notional_calculable": notional_ok > 0}


_VALIDATORS = {"trades": validate_trades, "orderbook": validate_orderbook,
               "oi": validate_oi, "funding": validate_funding,
               "liquidations": validate_liquidations}


# --------------------------------------------------------------------------
# Plan / validate / classify
# --------------------------------------------------------------------------

def microstructure_plan() -> dict[str, Any]:
    return {
        "tool_version": TOOL_VERSION,
        "objective": "validate + normalize LOCAL microstructure samples; build NO strategy",
        "reads_only_local_files": True, "uses_network": False, "uses_db": False,
        "default_sample_dir": DEFAULT_SAMPLE_DIR,
        "expected_types": ["trades", "orderbook", "oi", "funding", "liquidations"],
        "expected_fields": {
            "trades": ["timestamp", "symbol", "price", "size|qty|amount", "side|aggressor_side"],
            "orderbook": ["timestamp", "symbol", "bid_price_1..N/ask_price_1..N or bids/asks json"],
            "oi": ["timestamp", "symbol", "open_interest|oi"],
            "funding": ["timestamp", "symbol", "funding_rate"],
            "liquidations": ["timestamp", "symbol", "side", "price", "size|qty", "notional?"]},
        "verdicts": [C_NO_SAMPLE, C_INVALID, C_PARTIAL, C_READY, C_NEEDS_HISTORY,
                     C_NEEDS_ORDERBOOK, C_NEEDS_AGGRESSOR, C_NEEDS_LIQ, C_NEEDS_OI],
        "never": ["place_order", "create_order", "set_leverage", "private_get", "private_post",
                  "paid_download", "db_write", "raw_write", "PAPER_READY", "LIVE_READY"],
        "writes_on_plan": False, **_safety()}


def _future_research_ready(present: dict[str, dict]) -> dict[str, bool]:
    ob = present.get("orderbook", {}).get("valid", False)
    tr = present.get("trades", {})
    tr_aggr = tr.get("valid", False) and tr.get("has_aggressor_side", False)
    return {
        "orderbook_pressure": ob,
        "spread_slippage_estimator": ob,
        "liquidation_cluster_detector": present.get("liquidations", {}).get("valid", False),
        "aggressive_flow_imbalance": tr_aggr,
        "oi_expansion_contraction": present.get("oi", {}).get("valid", False),
        "funding_crowding": present.get("funding", {}).get("valid", False),
        "microstructure_aware_exit_simulation": bool(ob and tr_aggr)}


def classify_sample(present: dict[str, dict], files_seen: int) -> dict[str, Any]:
    gaps: list[str] = []
    valid_types = {k for k, v in present.items() if v.get("valid")}
    if files_seen == 0 or not present:
        return {"verdict": C_NO_SAMPLE, "gaps": [], "valid_types": [],
                "future_research_ready_if_sample_passes": False}
    if not valid_types:
        return {"verdict": C_INVALID, "gaps": ["no valid recognized data files"], "valid_types": [],
                "future_research_ready_if_sample_passes": False}
    max_cov = max((v.get("coverage", {}).get("coverage_days", 0) or 0) for v in present.values())
    tr = present.get("trades", {})
    if tr.get("valid") and not tr.get("has_aggressor_side"):
        gaps.append(C_NEEDS_AGGRESSOR)
    if "trades" not in valid_types:
        gaps.append(C_NEEDS_AGGRESSOR)
    if "orderbook" not in valid_types:
        gaps.append(C_NEEDS_ORDERBOOK)
    if "oi" not in valid_types:
        gaps.append(C_NEEDS_OI)
    if "liquidations" not in valid_types:
        gaps.append(C_NEEDS_LIQ)
    if max_cov < MIN_HISTORY_DAYS:
        gaps.append(C_NEEDS_HISTORY)
    ready = (tr.get("valid") and tr.get("has_aggressor_side") and "orderbook" in valid_types
             and ("oi" in valid_types or "funding" in valid_types) and max_cov >= MIN_HISTORY_DAYS)
    if ready:
        verdict = C_READY
    elif max_cov < MIN_HISTORY_DAYS and valid_types:
        verdict = C_NEEDS_HISTORY
    elif tr.get("valid") and not tr.get("has_aggressor_side"):
        verdict = C_NEEDS_AGGRESSOR
    elif "orderbook" not in valid_types:
        verdict = C_NEEDS_ORDERBOOK
    elif "oi" not in valid_types and "funding" not in valid_types:
        verdict = C_NEEDS_OI
    else:
        verdict = C_PARTIAL
    return {"verdict": verdict, "gaps": sorted(set(gaps)), "valid_types": sorted(valid_types),
            "max_coverage_days": max_cov,
            "future_research_ready_if_sample_passes": bool(ready),
            "future_labs_ready": _future_research_ready(present)}


def validate_sample(sample_dir: str, apply_normalization: bool = False) -> dict[str, Any]:
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "generated_at": _now_stamp(),
                           "sample_dir": sample_dir, "timezone": "UTC (timestamps normalized to ms)",
                           "files": [], "by_type": {}, "errors": [], **_safety()}
    try:
        assert_safe_sample_dir(sample_dir)
    except ValueError as e:
        rep["errors"].append(f"unsafe_sample_dir:{e}")
        rep["classification"] = {"verdict": C_INVALID, "gaps": ["unsafe sample dir"],
                                 "future_research_ready_if_sample_passes": False}
        return rep
    if not os.path.isdir(sample_dir):
        rep["classification"] = {"verdict": C_NO_SAMPLE, "gaps": [],
                                 "future_research_ready_if_sample_passes": False}
        return rep
    present: dict[str, dict] = {}
    files_seen = 0
    for name in sorted(os.listdir(sample_dir)):
        path = os.path.join(sample_dir, name)
        if not os.path.isfile(path) or not name.lower().endswith((".csv", ".tsv")):
            continue
        files_seen += 1
        header, rows, truncated = _read_csv(path)
        dtype = detect_type(name, header)
        info = {"file": name, "detected_type": dtype, "rows": len(rows),
                "truncated": truncated, "columns": header}
        if dtype in _VALIDATORS and rows:
            res = _VALIDATORS[dtype](rows)
            info.update({"valid": res["valid"], "metrics": res})
            # keep the richest validation per type
            if dtype not in present or (res["valid"] and not present[dtype].get("valid")):
                present[dtype] = res
        else:
            info["valid"] = False
        rep["files"].append(info)
    rep["by_type"] = {k: v for k, v in present.items()}
    rep["classification"] = classify_sample(present, files_seen)
    if apply_normalization and rep["classification"]["valid_types"]:
        rep["normalization"] = _write_normalized(sample_dir, rep)
    else:
        rep["normalization"] = {"applied": False, "reason": "flag off or no valid types"}
    return rep


def _write_normalized(sample_dir: str, rep: dict[str, Any]) -> dict[str, Any]:
    run_id = _now_stamp()
    out_dir = safe_normalized_dir(run_id)
    os.makedirs(out_dir, exist_ok=True)
    written = []
    # re-read each recognized file and emit a canonical normalized CSV per type
    headers = {"trades": ["ts_ms", "symbol", "price", "size", "side"],
               "orderbook": ["ts_ms", "symbol", "bid_price_1", "ask_price_1", "spread"],
               "oi": ["ts_ms", "symbol", "open_interest"],
               "funding": ["ts_ms", "symbol", "funding_rate"],
               "liquidations": ["ts_ms", "symbol", "side", "price", "size", "notional"]}
    for finfo in rep["files"]:
        dtype = finfo["detected_type"]
        if dtype not in headers or not finfo.get("valid"):
            continue
        header, rows, _ = _read_csv(os.path.join(sample_dir, finfo["file"]))
        out_path = os.path.join(out_dir, f"{dtype}_normalized.csv").replace("\\", "/")
        new_file = not os.path.exists(out_path)
        with open(out_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(headers[dtype])
            for r in rows:
                w.writerow(_normalize_row(dtype, r))
        if out_path not in written:
            written.append(out_path)
    return {"applied": True, "out_dir": out_dir, "files": written,
            "wrote_only_staging_marker": STAGING_MARKER in out_dir}


def _normalize_row(dtype: str, r: dict[str, str]) -> list[Any]:
    ts = _to_ms(_first(r, _TS_FIELDS))
    sym = _first(r, ("symbol", "instrument", "pair")) or ""
    if dtype == "trades":
        return [ts, sym, _first(r, ("price",)), _first(r, _SIZE_FIELDS), _norm_side(_first(r, _SIDE_FIELDS))]
    if dtype == "orderbook":
        low = {k.lower(): v for k, v in r.items()}
        try:
            bid = float(low.get("bid_price_1")); ask = float(low.get("ask_price_1"))
            spread = (ask - bid) / ((ask + bid) / 2) if (bid > 0 and ask > 0) else ""
        except (TypeError, ValueError):
            bid = ask = spread = ""
        return [ts, sym, low.get("bid_price_1", ""), low.get("ask_price_1", ""), spread]
    if dtype == "oi":
        return [ts, sym, _first(r, _OI_FIELDS)]
    if dtype == "funding":
        return [ts, sym, {k.lower(): v for k, v in r.items()}.get("funding_rate", "")]
    if dtype == "liquidations":
        low = {k.lower(): v for k, v in r.items()}
        return [ts, sym, _norm_side(_first(r, _SIDE_FIELDS)), _first(r, ("price",)),
                _first(r, _SIZE_FIELDS), low.get("notional", "")]
    return [ts, sym]


def write_reports(rep: dict[str, Any], output_dir: str | None = None) -> dict[str, str]:
    base = _safe_output_base(output_dir)
    os.makedirs(base, exist_ok=True)
    sc = os.path.join(base, "microstructure_sample_scorecard.json").replace("\\", "/")
    with open(sc, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2, default=str)
    cls = rep.get("classification", {})
    md = os.path.join(base, "microstructure_sample_readiness_report.md").replace("\\", "/")
    lines = ["# V10.24 Microstructure Sample Readiness (research only)", "",
             f"sample_dir: {rep.get('sample_dir')}", f"verdict: {cls.get('verdict')}",
             f"valid_types: {cls.get('valid_types')}", f"gaps: {cls.get('gaps')}",
             f"max_coverage_days: {cls.get('max_coverage_days')}",
             f"future_research_ready_if_sample_passes: {cls.get('future_research_ready_if_sample_passes')}",
             "", "## future labs ready (only if a passing sample arrives)"]
    for lab, ok in (cls.get("future_labs_ready") or {}).items():
        lines.append(f"- {lab}: {ok}")
    lines += ["", "final_recommendation: NO LIVE"]
    with open(md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return {"scorecard": sc, "report": md}
