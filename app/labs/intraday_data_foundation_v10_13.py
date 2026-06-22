"""ResearchOps V10.13 - Intraday Data Foundation + Microstructure Provider Readiness.

The bottleneck for serious micro-scalping research is DATA, not strategies:
no 1m/5m candles, no real trades, no orderbook, no historical OI, no
liquidations, no verified provider. This module builds the FOUNDATION to fix
that honestly: it audits whatever intraday/microstructure data exists, scores a
provider-readiness matrix (configuration/plan only - no purchase, no download),
defines canonical schemas, offers a SAFE public Bitget 1m/5m probe (dry-run by
default, staging-only writes), a sample builder, and a bridge that says whether
the data can yet feed V10.10/V10.11/V10.12.

Pure / offline / deterministic except the optional public-GET probe (allowlisted,
no auth, no keys). No orders, no leverage, no money, no DB, no .env, no raw
writes, no paid download. NOTHING flips paper_ready/live_ready.

FINAL_RECOMMENDATION: NO LIVE.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import bitget_public_data_v10_7 as b7

TOOL_VERSION = "v10.13"
OUTPUT_ROOT = "reports/research/v10_13"
STAGING_ROOT = "external_data/staging/bitget_public_intraday_v10_13"
DAY_MS = 86_400_000

INTRADAY_TFS = ("1m", "3m", "5m", "15m")
FALLBACK_TFS = ("4h", "6h")
TF_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
         "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
         "6h": 21_600_000, "12h": 43_200_000, "1d": 86_400_000}

# readiness states
NO_INTRADAY = "NO_INTRADAY_DATA"
PARTIAL = "PARTIAL_INTRADAY_DATA"
INTRADAY_READY = "INTRADAY_RESEARCH_READY"
MICRO_PARTIAL = "MICROSTRUCTURE_PARTIAL"
MICRO_READY = "MICROSTRUCTURE_READY"
SCALPING_NOT_READY = "SCALPING_DATA_NOT_READY"

# provider states
P_NOT_CHECKED = "NOT_CHECKED"
P_PUBLIC_LIMITED = "PUBLIC_LIMITED"
P_NEEDS_MANUAL = "NEEDS_MANUAL_VERIFICATION"
P_CANDIDATE = "CANDIDATE_PROVIDER"
P_REJECTED = "REJECTED_PROVIDER"
P_VERIFIED_SAMPLE = "VERIFIED_PROVIDER_SAMPLE_REQUIRED"

# sample-build states
SB_SKIPPED = "SAMPLE_BUILD_SKIPPED_NEED_DATA"
SB_STAGED = "INTRADAY_SAMPLE_STAGED"
SB_WARNINGS = "INTRADAY_SAMPLE_HAS_WARNINGS"
SB_REJECTED = "INTRADAY_SAMPLE_REJECTED"

# bridge states
BR_NO_INTRADAY = "NOT_READY_NO_INTRADAY"
BR_NO_MICRO = "NOT_READY_NO_MICROSTRUCTURE"
BR_RESEARCH_SHADOW = "READY_FOR_RESEARCH_ONLY_SHADOW"
BR_PATTERN_REBUILD = "READY_FOR_PATTERN_MEMORY_REBUILD"
BR_MICRO_REPLAY = "READY_FOR_MICRO_SCALP_REPLAY"

_FORBIDDEN_SEG = ("raw", "backup", "backups", "vault", "vaults", "training_exports",
                  "secret", "secrets", "credential", "credentials",
                  "codex_result.md", "code_result.md")
_FORBIDDEN_SUF = (".env", ".db", ".sqlite", ".sqlite3", ".zip", ".tar", ".gz",
                  ".tgz", ".pem", ".key")
_STAGING_MARKER = ("external_data", "staging", "bitget_public_intraday_v10_13")

MIN_READY_DAYS = 14.0
MIN_READY_SYMBOLS = 2


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "edge_validated": False,
            "paper_candidate_future": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _segs(path: str) -> list[str]:
    return [s for s in str(path).replace("\\", "/").split("/") if s]


def _safe_output_base(output_dir: str | None) -> str:
    base = output_dir or OUTPUT_ROOT
    if not isinstance(base, str) or not base.strip() or "%" in base:
        return OUTPUT_ROOT
    if ".." in _segs(base):
        return OUTPUT_ROOT
    try:
        real = os.path.realpath(base).replace("\\", "/")
    except Exception:
        return OUTPUT_ROOT
    for s in (x.lower() for x in _segs(real)):
        if s in _FORBIDDEN_SEG or s.endswith(_FORBIDDEN_SUF) or ".env" in s:
            return OUTPUT_ROOT
    return base


def _subseq(seq: list[str], sub) -> bool:
    sub = list(sub)
    n, m = len(seq), len(sub)
    return any(seq[i:i + m] == sub for i in range(n - m + 1))


def safe_intraday_staging_dir(staging_dir: Any) -> str | None:
    """Fail-closed gate for intraday staging WRITES. Returns a rejection reason
    string, or None if the path is a legal intraday staging target. Must sit
    under the intraday staging marker and contain no raw/backup/db/.env segment."""
    if not isinstance(staging_dir, str) or not staging_dir.strip() or "%" in staging_dir:
        return "empty_or_percent_path"
    segs = _segs(staging_dir)
    low = [s.lower() for s in segs]
    if ".." in segs:
        return "path_traversal"
    for s in low:
        if s in _FORBIDDEN_SEG or s.endswith(_FORBIDDEN_SUF) or ".env" in s:
            return f"forbidden_segment:{s}"
    if not _subseq(low, [m.lower() for m in _STAGING_MARKER]):
        return "not_under_intraday_staging_marker"
    # V10.13.1 hardening: relative paths under the marker are fine; an ABSOLUTE
    # path must still resolve INSIDE the current repo (no odd absolute roots).
    if os.path.isabs(staging_dir):
        try:
            real = os.path.realpath(staging_dir)
            cwd = os.path.realpath(os.getcwd())
            if os.path.commonpath([real, cwd]) != cwd:
                return "absolute_path_outside_repo"
        except Exception:
            return "unresolvable_absolute_path"
    return None


# --------------------------------------------------------------------------
# 2. Intraday OHLCV quality analysis (tolerant raw reader + checks)
# --------------------------------------------------------------------------

def _read_ohlcv_raw(path: str) -> tuple[list[dict[str, Any]], int]:
    rows, parse_errors = [], 0
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            for r in rd:
                try:
                    ts = int(float(r.get("timestamp") or r.get("ts") or r.get("timestamp_ms")))
                    o = float(r["open"]); h = float(r["high"])
                    lo = float(r["low"]); c = float(r["close"])
                    v = float(r.get("volume") or r.get("volume_base") or 0.0)
                except (TypeError, ValueError, KeyError):
                    parse_errors += 1
                    continue
                rows.append({"ts": ts, "open": o, "high": h, "low": lo, "close": c, "volume": v})
    except Exception:
        return [], parse_errors
    return rows, parse_errors


def analyze_ohlcv_series(rows, tf, parse_errors=0) -> dict[str, Any]:
    n = len(rows)
    out = {"timeframe": tf, "rows": n, "parse_errors": parse_errors,
           "days_covered": 0.0, "duplicates": 0, "non_monotonic": 0,
           "invalid_ohlc": 0, "zero_volume": 0, "gaps": 0, "continuity_ratio": 0.0,
           "utc_ms_consistent": True, "expected_interval_ms": TF_MS.get(tf, 0)}
    if n == 0:
        return out
    interval = TF_MS.get(tf, 0)
    seen, dups, nonmono, invalid, zerov, gaps = set(), 0, 0, 0, 0, 0
    prev = None
    for r in rows:
        ts, o, h, lo, c, v = r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"]
        if ts in seen:
            dups += 1
        seen.add(ts)
        if ts < 1_000_000_000_000:   # not millis since ~2001 -> suspect seconds/secs
            out["utc_ms_consistent"] = False
        if prev is not None and ts <= prev:
            nonmono += 1
        if (h < max(o, c)) or (lo > min(o, c)) or (h < lo) or min(o, h, lo, c) <= 0:
            invalid += 1
        if v == 0:
            zerov += 1
        if prev is not None and interval > 0 and (ts - prev) > 1.5 * interval:
            gaps += 1
        prev = ts
    span = rows[-1]["ts"] - rows[0]["ts"]
    out["days_covered"] = round(span / DAY_MS, 3) if span > 0 else 0.0
    expected = (span // interval + 1) if interval > 0 and span > 0 else n
    out["continuity_ratio"] = round(min(1.0, n / expected), 4) if expected > 0 else 0.0
    out.update({"duplicates": dups, "non_monotonic": nonmono, "invalid_ohlc": invalid,
                "zero_volume": zerov, "gaps": gaps})
    return out


def _scan_files(sample_dir, symbols):
    try:
        files = os.listdir(sample_dir)
    except Exception:
        return None
    return files


def intraday_data_readiness(sample_dir, symbols=None) -> dict[str, Any]:
    rep: dict[str, Any] = {
        "tool_version": TOOL_VERSION, "generated_at": _now_iso(), "sample_dir": sample_dir,
        "intraday_timeframes_present": [], "fallback_only": False,
        "coverage_rows": [], "quality_issues": [],
        "has_funding": False, "has_oi_historical": False, "has_liquidations": False,
        "has_trades": False, "has_orderbook": False, "has_real_spread": False,
        "provider_verified": False, "status": NO_INTRADAY,
        "scalping_data_status": SCALPING_NOT_READY, **_safety()}
    files = _scan_files(sample_dir, symbols)
    if files is None:
        rep["errors"] = ["sample_dir_not_found"]
        return rep

    present_intraday: set[str] = set()
    present_fallback: set[str] = set()
    syms_with_intraday: set[str] = set()
    days_by_series = []
    all_tfs = INTRADAY_TFS + FALLBACK_TFS
    for f in files:
        for tf in all_tfs:
            suf = f"_{tf}_ohlcv.csv"
            if not f.endswith(suf):
                continue
            sym = f[: -len(suf)]
            if symbols and sym not in symbols:
                continue
            rows, perr = _read_ohlcv_raw(os.path.join(sample_dir, f))
            q = analyze_ohlcv_series(rows, tf, perr)
            q.update({"symbol": sym})
            rep["coverage_rows"].append(q)
            issues = {k: q[k] for k in ("parse_errors", "duplicates", "non_monotonic",
                                        "invalid_ohlc", "zero_volume", "gaps")
                      if q[k]}
            if not q["utc_ms_consistent"]:
                issues["utc_ms_inconsistent"] = True
            if issues:
                rep["quality_issues"].append({"symbol": sym, "timeframe": tf, **issues})
            if tf in INTRADAY_TFS:
                if q["rows"] > 0:
                    present_intraday.add(tf)
                    syms_with_intraday.add(sym)
                    days_by_series.append(q["days_covered"])
            else:
                if q["rows"] > 0:
                    present_fallback.add(tf)

    rep["intraday_timeframes_present"] = sorted(present_intraday)
    rep["fallback_timeframes_present"] = sorted(present_fallback)
    rep["fallback_only"] = bool(present_fallback) and not present_intraday
    # microstructure / derived data presence
    low = [f.lower() for f in files]
    rep["has_funding"] = any(f.endswith("_funding.csv") for f in low)
    rep["has_oi_historical"] = any("open_interest" in f or "_oi" in f for f in low)
    rep["has_liquidations"] = any("liquidation" in f for f in low)
    rep["has_trades"] = any(f.endswith("_trades.csv") for f in low)
    rep["has_orderbook"] = any("orderbook" in f or "_book" in f for f in low)
    rep["has_real_spread"] = any("spread" in f for f in low) or rep["has_orderbook"]
    rep["missing_oi_historical"] = not rep["has_oi_historical"]
    rep["missing_liquidations"] = not rep["has_liquidations"]

    min_days = min(days_by_series) if days_by_series else 0.0
    n_syms = len(syms_with_intraday)
    has_1m_5m = bool({"1m", "5m"} & present_intraday)
    enough = has_1m_5m and min_days >= MIN_READY_DAYS and n_syms >= MIN_READY_SYMBOLS
    micro_full = rep["has_trades"] and rep["has_orderbook"] and rep["has_oi_historical"] and rep["has_liquidations"]
    micro_some = rep["has_trades"] or rep["has_orderbook"]

    if not present_intraday:
        rep["status"] = NO_INTRADAY
    elif enough and micro_full:
        rep["status"] = MICRO_READY
    elif enough and micro_some:
        rep["status"] = MICRO_PARTIAL
    elif enough:
        rep["status"] = INTRADAY_READY
    else:
        rep["status"] = PARTIAL
    # scalping conclusion requires intraday-ready AND at least partial microstructure
    rep["scalping_data_status"] = (
        "SCALPING_DATA_READY" if (rep["status"] in (MICRO_PARTIAL, MICRO_READY))
        else SCALPING_NOT_READY)
    rep["min_intraday_days"] = round(min_days, 3)
    rep["intraday_symbols"] = sorted(syms_with_intraday)
    rep["n_intraday_symbols"] = n_syms
    return rep


# --------------------------------------------------------------------------
# 3. Provider readiness matrix (config/plan only - NO purchase, NO download)
# --------------------------------------------------------------------------

def provider_readiness_matrix() -> dict[str, Any]:
    providers = [
        {"name": "bitget_public", "public": True, "requires_api_key": False,
         "ohlcv_1m_5m": True, "history_depth_days": "1m/5m ~1 month (queryable cap)",
         "trades_history": False, "orderbook_snapshots": False, "orderbook_l2": False,
         "liquidations": False, "oi_historical": False, "funding": True,
         "rate_limits": "~20 req/s/IP (we use <=3)", "estimated_cost": "free",
         "license_risk": "low (public market data)", "ingestion_ease": "high (V10.7 collector)",
         "expected_format": "json arrays -> csv", "known_gaps": "no deep 1m history, no trades/orderbook/OI/liq",
         "state": P_PUBLIC_LIMITED,
         "recommendation": "use for recent 1m/5m smoke probe only; not enough for validation"},
        {"name": "coinalyze", "public": False, "requires_api_key": True,
         "ohlcv_1m_5m": True, "history_depth_days": "NEEDS_MANUAL_VERIFICATION",
         "trades_history": False, "orderbook_snapshots": False, "orderbook_l2": False,
         "liquidations": True, "oi_historical": True, "funding": True,
         "rate_limits": "NEEDS_MANUAL_VERIFICATION", "estimated_cost": "free tier + paid (DO NOT PAY)",
         "license_risk": "NEEDS_MANUAL_VERIFICATION", "ingestion_ease": "medium",
         "expected_format": "json", "known_gaps": "no raw trades / L2 orderbook",
         "state": P_NEEDS_MANUAL,
         "recommendation": "good for OI/liquidations/funding; verify key terms before any sample"},
        {"name": "tardis_dev", "public": False, "requires_api_key": True,
         "ohlcv_1m_5m": True, "history_depth_days": "365+ (paid)",
         "trades_history": True, "orderbook_snapshots": True, "orderbook_l2": True,
         "liquidations": True, "oi_historical": True, "funding": True,
         "rate_limits": "plan-dependent", "estimated_cost": "paid (DO NOT PAY without approval)",
         "license_risk": "commercial license - review terms", "ingestion_ease": "medium (large files)",
         "expected_format": "csv.gz normalized", "known_gaps": "cost; needs human sign-off",
         "state": P_VERIFIED_SAMPLE,
         "recommendation": "best microstructure coverage; request a 180/365d SAMPLE (human action)"},
        {"name": "coinglass", "public": False, "requires_api_key": True,
         "ohlcv_1m_5m": False, "history_depth_days": "NEEDS_MANUAL_VERIFICATION",
         "trades_history": False, "orderbook_snapshots": False, "orderbook_l2": False,
         "liquidations": True, "oi_historical": True, "funding": True,
         "rate_limits": "NEEDS_MANUAL_VERIFICATION", "estimated_cost": "free tier + paid (DO NOT PAY)",
         "license_risk": "NEEDS_MANUAL_VERIFICATION", "ingestion_ease": "medium",
         "expected_format": "json", "known_gaps": "no fine OHLCV / trades / orderbook",
         "state": P_NEEDS_MANUAL,
         "recommendation": "complementary OI/liquidations source only"},
    ]
    return {"tool_version": TOOL_VERSION, "generated_at": _now_iso(),
            "providers": providers,
            "no_paid_download": True, "no_paid_activation": True,
            "no_api_key_usage": True, "verified_count": 0,
            "note": "configuration/plan only; nothing purchased, downloaded, or activated",
            **_safety()}


# --------------------------------------------------------------------------
# 4. Canonical intraday + microstructure schemas
# --------------------------------------------------------------------------

def canonical_intraday_schemas() -> dict[str, Any]:
    return {
        "ohlcv_intraday": ["provider", "symbol", "timeframe", "ts", "open", "high",
                           "low", "close", "volume", "quote_volume", "trades_count",
                           "source_run_id", "quality_flags"],
        "trades": ["provider", "symbol", "ts", "price", "size", "side_aggressor",
                   "trade_id", "quality_flags"],
        "orderbook": ["provider", "symbol", "ts", "bid_price_1", "bid_size_1",
                      "ask_price_1", "ask_size_1", "spread_bps", "depth_snapshot",
                      "quality_flags"],
        "open_interest": ["provider", "symbol", "ts", "open_interest",
                          "open_interest_value", "quality_flags"],
        "liquidations": ["provider", "symbol", "ts", "side", "price", "quantity",
                         "notional", "quality_flags"],
        **_safety()}


def render_schema_md(schemas) -> str:
    lines = ["# ResearchOps V10.13 - Canonical Intraday & Microstructure Schemas", "",
             "RESEARCH ONLY. No raw writes, no DB writes. Staging/reports only.", ""]
    for name in ("ohlcv_intraday", "trades", "orderbook", "open_interest", "liquidations"):
        lines.append(f"## {name}")
        for col in schemas[name]:
            lines.append(f"- {col}")
        lines.append("")
    lines += ["research_only: true", "shadow_only: true", "paper_ready: false",
              "live_ready: false", "can_send_real_orders: false",
              "final_recommendation: NO LIVE"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 5. Bitget public intraday probe (dry-run default; staging-only writes)
# --------------------------------------------------------------------------

def bitget_intraday_plan(symbols=None, timeframes=None, days=7) -> dict[str, Any]:
    symbols = symbols or ["BTCUSDT", "ETHUSDT"]
    timeframes = [t for t in (timeframes or ["1m", "5m"]) if t in INTRADAY_TFS]
    planned = [f"candles:{s}:{tf}" for s in symbols for tf in timeframes]
    return {"tool_version": TOOL_VERSION, "generated_at": _now_iso(),
            "data_source": "bitget_public", "endpoint": b7.EP_CANDLES,
            "symbols": symbols, "timeframes": timeframes, "requested_days": int(days),
            "planned_fetches": planned, "method": "GET", "auth": "none",
            "public_only": True, "dry_run_by_default": True, "no_network": True,
            "coverage_notes": {tf: b7.COVERAGE_NOTES.get(tf) for tf in timeframes},
            "honest_expectation": "bitget public 1m/5m is queryable-limited (~1 month) -> PUBLIC_LIMITED",
            "staging_target": STAGING_ROOT + "/<run_id>/", **_safety()}


def bitget_intraday_probe(*, symbols, timeframes, days=2, max_requests=6, apply=False,
                          transport=None, staging_root=None, limit=200) -> dict[str, Any]:
    tfs = [t for t in timeframes if t in INTRADAY_TFS]
    syms = [str(s).strip().upper() for s in symbols if str(s).strip()]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rep: dict[str, Any] = {
        "tool_version": TOOL_VERSION, "run_id": run_id, "data_source": "bitget_public",
        "symbols": syms, "timeframes": tfs, "requested_days": int(days),
        "max_requests": int(max_requests), "dry_run": (not apply),
        "endpoints_called": [], "files": [], "rows_written": {}, "errors": [],
        "warnings": [], "staging_dir": "", "public_state": P_PUBLIC_LIMITED, **_safety()}
    if not syms or not tfs:
        rep["errors"].append("nothing_to_probe (need symbols + 1m/3m/5m/15m timeframes)")
        return rep
    if not apply:
        rep["planned_fetches"] = [f"candles:{s}:{tf}" for s in syms for tf in tfs]
        rep["note"] = "dry-run: no network, no writes. Pass --apply for a small public probe."
        return rep

    base = staging_root or STAGING_ROOT
    run_dir = os.path.join(base, run_id).replace("\\", "/")
    block = safe_intraday_staging_dir(run_dir)
    if block is not None:
        rep["errors"].append(f"staging_dir_rejected:{block}")
        rep["blocked"] = True
        return rep
    tx = transport or b7.default_transport
    os.makedirs(run_dir, exist_ok=True)
    rep["staging_dir"] = run_dir
    requests_made = 0
    for s in syms:
        for tf in tfs:
            if requests_made >= int(max_requests):
                rep["warnings"].append("max_requests_reached")
                break
            gran = b7._GRANULARITY.get(tf, tf)  # type: ignore[attr-defined]
            try:
                payload = tx(b7.EP_CANDLES, {"symbol": s, "productType": b7.PRODUCT_TYPE,
                                             "granularity": gran, "limit": int(limit)})
                requests_made += 1
                rep["endpoints_called"].append(f"{b7.EP_CANDLES}?symbol={s}&granularity={gran}")
            except Exception as exc:
                rep["errors"].append(f"fetch_failed:{s}:{tf}:{type(exc).__name__}")
                requests_made += 1
                continue
            rows = b7.parse_candles(payload, symbol=s, timeframe=tf)
            if not rows:
                rep["warnings"].append(f"empty:{s}:{tf}")
                continue
            rows.sort(key=lambda r: r["timestamp_ms"])
            path = os.path.join(run_dir, f"{s}_{tf}_ohlcv.csv")
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
                for r in rows:
                    w.writerow([r["timestamp_ms"], r["open"], r["high"], r["low"],
                                r["close"], r["volume_base"]])
            span_days = round((rows[-1]["timestamp_ms"] - rows[0]["timestamp_ms"]) / DAY_MS, 3)
            rep["files"].append({"file": f"{s}_{tf}_ohlcv.csv", "rows": len(rows),
                                 "days_covered": span_days})
            rep["rows_written"][f"{s}_{tf}"] = len(rows)
    rep["note"] = ("public 1m/5m is queryable-limited; a single page covers only a small "
                   "recent window -> confirms PUBLIC_LIMITED, not enough for validation")
    return rep


def bitget_intraday_audit(staging_dir, symbols=None) -> dict[str, Any]:
    rep = intraday_data_readiness(staging_dir, symbols)
    rep["audit_of"] = staging_dir
    rep["audit_symbols_filter"] = list(symbols) if symbols else "ALL"
    rep["tool_version"] = TOOL_VERSION
    return rep


# --------------------------------------------------------------------------
# 6. Intraday sample builder (validate/normalize/manifest; staging/reports only)
# --------------------------------------------------------------------------

def intraday_sample_build(staging_dir, *, output_dir=None, apply=False) -> dict[str, Any]:
    readiness = intraday_data_readiness(staging_dir)
    rep: dict[str, Any] = {
        "tool_version": TOOL_VERSION, "generated_at": _now_iso(),
        "staging_dir": staging_dir, "dry_run": (not apply),
        "intraday_status": readiness.get("status"),
        "coverage_rows": readiness.get("coverage_rows", []),
        "quality_issues": readiness.get("quality_issues", []),
        "schemas": [k for k in canonical_intraday_schemas() if not k.startswith("research")
                    and k not in ("shadow_only", "paper_ready", "live_ready",
                                  "can_send_real_orders", "paper_filter_enabled",
                                  "edge_validated", "paper_candidate_future",
                                  "final_recommendation")],
        "manifest": {}, "dataset_hash": None, "status": SB_SKIPPED, "errors": [],
        **_safety()}
    if readiness.get("errors"):
        rep["errors"] = readiness["errors"]
        rep["status"] = SB_SKIPPED
        return rep
    if readiness.get("status") == NO_INTRADAY or not readiness.get("coverage_rows"):
        rep["status"] = SB_SKIPPED
        rep["note"] = "no intraday OHLCV present; nothing to build"
        return rep
    # classify
    n_invalid = sum(1 for q in readiness["coverage_rows"]
                    if q.get("invalid_ohlc", 0) or q.get("non_monotonic", 0))
    n_warn = len(readiness.get("quality_issues", []))
    series = sorted(f"{q['symbol']}_{q['timeframe']}:{q['rows']}:{q['days_covered']}"
                    for q in readiness["coverage_rows"])
    rep["manifest"] = {"series": series, "n_series": len(series),
                       "intraday_status": readiness.get("status"),
                       "schemas_version": TOOL_VERSION}
    rep["dataset_hash"] = hashlib.sha256("|".join(series).encode()).hexdigest()
    if n_invalid > 0:
        rep["status"] = SB_REJECTED
    elif n_warn > 0:
        rep["status"] = SB_WARNINGS
    else:
        rep["status"] = SB_STAGED
    # writes go ONLY to a safe report/staging dir, never raw/DB
    if apply:
        base = _safe_output_base(output_dir)
        run_dir = os.path.join(base, "sample_build_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")).replace("\\", "/")
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "intraday_sample_manifest.json"), "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in rep.items() if k != "coverage_rows"}, f, indent=2, default=str)
        rep["manifest_dir"] = run_dir
    return rep


# --------------------------------------------------------------------------
# 7. Bridge: can the intraday sample yet feed V10.10 / V10.11 / V10.12 ?
# --------------------------------------------------------------------------

def intraday_to_shadow_readiness(sample_dir, symbols=None, *, require_microstructure=False) -> dict[str, Any]:
    r = intraday_data_readiness(sample_dir, symbols)
    status = r.get("status")
    has_intraday = bool(r.get("intraday_timeframes_present"))
    min_days = r.get("min_intraday_days", 0.0)
    n_syms = r.get("n_intraday_symbols", 0)
    micro_some = r.get("has_trades") or r.get("has_orderbook")
    enough = has_intraday and min_days >= MIN_READY_DAYS and n_syms >= MIN_READY_SYMBOLS

    if not has_intraday:
        bridge = BR_NO_INTRADAY
    elif require_microstructure and not micro_some:
        bridge = BR_NO_MICRO
    elif enough:
        bridge = BR_MICRO_REPLAY
    elif has_intraday and min_days > 0:
        bridge = BR_PATTERN_REBUILD
    else:
        bridge = BR_RESEARCH_SHADOW
    out = {
        "tool_version": TOOL_VERSION, "generated_at": _now_iso(), "sample_dir": sample_dir,
        "intraday_status": status, "bridge_status": bridge,
        "can_feed_v1010_micro_scalp": bridge in (BR_MICRO_REPLAY, BR_PATTERN_REBUILD, BR_RESEARCH_SHADOW) and has_intraday,
        "can_feed_v1011_pattern_memory": bridge in (BR_MICRO_REPLAY, BR_PATTERN_REBUILD) and has_intraday,
        "can_feed_v1012_intelligent_shadow": bridge == BR_MICRO_REPLAY,
        "ready_for_paper": False, "ready_for_live": False,
        "min_intraday_days": min_days, "n_intraday_symbols": n_syms,
        "missing_microstructure": not micro_some, **_safety()}
    return out


# --------------------------------------------------------------------------
# 8. Reports
# --------------------------------------------------------------------------

def _write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_v1013_reports(readiness, matrix, *, bridge=None, output_dir=None) -> str:
    base = _safe_output_base(output_dir)
    run_dir = os.path.join(base, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")).replace("\\", "/")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "intraday_data_readiness_summary.json"), "w", encoding="utf-8") as f:
        slim = {k: v for k, v in readiness.items() if k not in ("coverage_rows", "quality_issues")}
        if bridge:
            slim["bridge"] = bridge
        json.dump(slim, f, indent=2, default=str)
    _write_csv(os.path.join(run_dir, "intraday_coverage_by_symbol_timeframe.csv"),
               readiness.get("coverage_rows", []),
               ["symbol", "timeframe", "rows", "days_covered", "continuity_ratio",
                "gaps", "duplicates", "non_monotonic", "invalid_ohlc", "zero_volume",
                "parse_errors", "utc_ms_consistent"])
    _write_csv(os.path.join(run_dir, "intraday_quality_issues.csv"),
               readiness.get("quality_issues", []),
               ["symbol", "timeframe", "parse_errors", "duplicates", "non_monotonic",
                "invalid_ohlc", "zero_volume", "gaps", "utc_ms_inconsistent"])
    with open(os.path.join(run_dir, "provider_readiness_matrix.json"), "w", encoding="utf-8") as f:
        json.dump(matrix, f, indent=2, default=str)
    _write_csv(os.path.join(run_dir, "provider_readiness_matrix.csv"), matrix.get("providers", []),
               ["name", "public", "requires_api_key", "ohlcv_1m_5m", "history_depth_days",
                "trades_history", "orderbook_snapshots", "orderbook_l2", "liquidations",
                "oi_historical", "funding", "rate_limits", "estimated_cost", "license_risk",
                "ingestion_ease", "expected_format", "known_gaps", "state", "recommendation"])
    with open(os.path.join(run_dir, "provider_gap_plan.md"), "w", encoding="utf-8") as f:
        f.write(_render_gap_plan_md(readiness, matrix))
    with open(os.path.join(run_dir, "canonical_intraday_schema.md"), "w", encoding="utf-8") as f:
        f.write(render_schema_md(canonical_intraday_schemas()))
    with open(os.path.join(run_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(_render_report_md(readiness, matrix, bridge))
    return run_dir


def _render_gap_plan_md(readiness, matrix) -> str:
    lines = ["# ResearchOps V10.13 - Provider Gap Plan", "",
             "NO paid download. NO paid activation. NO API key usage. Human action required.", "",
             f"- intraday_status: {readiness.get('status')}",
             f"- scalping_data_status: {readiness.get('scalping_data_status')}",
             "", "## What is missing for serious micro-scalping",
             "- fine OHLCV (1m/5m) with multi-month depth",
             "- raw trades (aggressor side)", "- orderbook snapshots / L2 + real spread",
             "- historical open interest", "- liquidations", "", "## Recommended path"]
    for p in matrix.get("providers", []):
        lines.append(f"- {p['name']} [{p['state']}]: {p['recommendation']}")
    lines += ["", "## Unblocking human action",
              "- request a 180/365d SAMPLE from a microstructure provider (e.g. Tardis.dev)",
              "- verify license terms BEFORE any download; do NOT pay without approval",
              "", "research_only: true", "shadow_only: true", "paper_ready: false",
              "live_ready: false", "can_send_real_orders: false", "final_recommendation: NO LIVE"]
    return "\n".join(lines) + "\n"


def _render_report_md(readiness, matrix, bridge) -> str:
    lines = ["# ResearchOps V10.13 - Intraday Data Foundation", "",
             "RESEARCH ONLY / SHADOW ONLY. NO LIVE. NO PAPER. NO ORDERS. NO PAID DOWNLOAD.", "",
             f"- intraday_status: {readiness.get('status')}",
             f"- scalping_data_status: {readiness.get('scalping_data_status')}",
             f"- intraday_timeframes_present: {readiness.get('intraday_timeframes_present')}",
             f"- fallback_only: {readiness.get('fallback_only')}",
             f"- has_trades: {readiness.get('has_trades')} | has_orderbook: {readiness.get('has_orderbook')}",
             f"- has_oi_historical: {readiness.get('has_oi_historical')} | has_liquidations: {readiness.get('has_liquidations')}",
             f"- provider_verified: {readiness.get('provider_verified')}",
             f"- quality_issues: {len(readiness.get('quality_issues', []))}"]
    if bridge:
        lines.append(f"- bridge_status: {bridge.get('bridge_status')}")
    lines += ["", "Conclusion: with the current data, intraday micro-scalping is NOT validatable.",
              "These are data-readiness findings, not signals. No edge validated.", "",
              "research_only: true", "shadow_only: true", "paper_ready: false",
              "live_ready: false", "can_send_real_orders: false", "paper_filter_enabled: false",
              "paper_candidate_future: false", "final_recommendation: NO LIVE"]
    return "\n".join(lines) + "\n"


def latest_v1013_summary(output_dir=None) -> dict[str, Any] | None:
    base = _safe_output_base(output_dir)
    if not os.path.isdir(base):
        return None
    runs = sorted((d for d in os.listdir(base)
                   if os.path.isfile(os.path.join(base, d, "intraday_data_readiness_summary.json"))),
                  reverse=True)
    if not runs:
        return None
    try:
        with open(os.path.join(base, runs[0], "intraday_data_readiness_summary.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
