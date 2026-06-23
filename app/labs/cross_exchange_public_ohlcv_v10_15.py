"""ResearchOps V10.15 - Cross-Exchange PUBLIC OHLCV collector (research only).

Purpose: validate the V10.14 lead (1h / SHORT / trend_pullback) on LONGER public
history from OTHER venues, to tell a real pattern from a one-month Bitget regime
artifact. Bitget public caps ~31 days for intraday TFs; Binance USDT-M futures and
Bybit linear public klines serve a full year of 1h.

Hard guarantees (mirrors the V10.7 public collector):
- ONLY HTTPS GET to a tiny allowlist of PUBLIC market-data endpoints;
- NO auth, NO API keys, NO private endpoints, NO .env;
- NO raw writes, NO DB writes; staging-only, fail-closed path gate;
- bounded backward/forward pagination, rate-limited, retry-limited, timeout;
- NOTHING flips paper_ready/live_ready/can_send_real_orders.

FINAL_RECOMMENDATION: NO LIVE.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from . import FINAL_RECOMMENDATION_NO_LIVE

TOOL_VERSION = "v10.15"
OUTPUT_ROOT = "reports/research/v10_15"
STAGING_ROOT = "external_data/staging/cross_exchange_public_ohlcv_v10_15"
DAY_MS = 86_400_000
DEFAULT_TIMEOUT_S = 12.0
DEFAULT_RATE_PER_S = 5.0
MAX_REQUESTS_HARD = 2000

TF_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
         "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
         "6h": 21_600_000, "12h": 43_200_000, "1d": 86_400_000}

# ---- exchange registry (PUBLIC endpoints only) ----------------------------
# Each: host, path, page_limit, interval map, param builder, parser.
_BINANCE_INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "2h": "2h",
                     "4h": "4h", "6h": "6h", "12h": "12h", "1d": "1d"}
_BYBIT_INTERVAL = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "2h": "120",
                   "4h": "240", "6h": "360", "12h": "720", "1d": "D"}

EXCHANGES = ("binance_futures", "bybit_linear")
_ALLOW = {
    "binance_futures": ("fapi.binance.com", "/fapi/v1/klines"),
    "bybit_linear": ("api.bybit.com", "/v5/market/kline"),
}
_PAGE_LIMIT = {"binance_futures": 1500, "bybit_linear": 1000}
_FORBIDDEN_HEADERS = frozenset({"access-key", "access-sign", "access-passphrase",
                                "access-timestamp", "x-access-key", "x-bapi-api-key",
                                "x-bapi-sign", "x-mbx-apikey", "authorization"})
_FORBIDDEN_SEG = ("raw", "backup", "backups", "vault", "vaults", "training_exports",
                  "secret", "secrets", "credential", "credentials",
                  "codex_result.md", "code_result.md")
_FORBIDDEN_SUF = (".env", ".db", ".sqlite", ".sqlite3", ".zip", ".tar", ".gz",
                  ".tgz", ".pem", ".key")
_STAGING_MARKER = ("external_data", "staging", "cross_exchange_public_ohlcv_v10_15")


class UnsafeRequestError(Exception):
    pass


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "paper_filter_enabled": False, "paper_candidate_future": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _segs(path: str) -> list[str]:
    return [s for s in str(path).replace("\\", "/").split("/") if s]


def _subseq(seq, sub) -> bool:
    sub = list(sub)
    m = len(sub)
    return any(seq[i:i + m] == sub for i in range(len(seq) - m + 1))


def safe_staging_dir(staging_dir: Any) -> str | None:
    """Fail-closed gate for cross-exchange staging WRITES."""
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
        return "not_under_cross_exchange_staging_marker"
    if os.path.isabs(staging_dir):
        try:
            real = os.path.realpath(staging_dir)
            cwd = os.path.realpath(os.getcwd())
            if os.path.commonpath([real, cwd]) != cwd:
                return "absolute_path_outside_repo"
        except Exception:
            return "unresolvable_absolute_path"
    return None


def assert_safe_request(method: str, url: str, headers=None) -> bool:
    if str(method).upper() != "GET":
        raise UnsafeRequestError(f"method_not_allowed:{method}")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UnsafeRequestError(f"scheme_not_https:{parts.scheme}")
    allowed = {(_ALLOW[e][0], _ALLOW[e][1]) for e in EXCHANGES}
    if (parts.netloc, parts.path) not in allowed:
        raise UnsafeRequestError(f"endpoint_not_allowed:{parts.netloc}{parts.path}")
    for frag in ("/order", "/account", "/position", "/private", "/trade", "/v5/order", "/v5/account"):
        if frag in parts.path:
            raise UnsafeRequestError(f"forbidden_fragment:{frag}")
    for k in (headers or {}):
        if str(k).strip().lower() in _FORBIDDEN_HEADERS:
            raise UnsafeRequestError(f"forbidden_auth_header:{k}")
    return True


def _raw_get(url: str, timeout: float) -> Any:
    req = Request(url, method="GET", headers={
        "User-Agent": "researchops-v10_15-public/1.0", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https+allowlisted)
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raise URLError(f"http_{exc.code}") from None


def default_transport(exchange: str, params: dict, *, timeout: float = DEFAULT_TIMEOUT_S) -> Any:
    host, path = _ALLOW[exchange]
    url = "https://" + host + path + "?" + urlencode({k: v for k, v in params.items() if v is not None})
    assert_safe_request("GET", url, headers={})
    return _raw_get(url, timeout=timeout)


Transport = Callable[..., Any]


# ---- parsers (tolerant, never raise) --------------------------------------

def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_binance(payload) -> list[dict[str, Any]]:
    rows = []
    if not isinstance(payload, list):
        return rows
    for k in payload:
        if not isinstance(k, (list, tuple)) or len(k) < 6:
            continue
        ts = k[0]
        o, h, l, c, v = _f(k[1]), _f(k[2]), _f(k[3]), _f(k[4]), _f(k[5])
        if ts is None or None in (o, h, l, c):
            continue
        rows.append({"ts": int(ts), "open": o, "high": h, "low": l, "close": c,
                     "volume": v if v is not None else 0.0})
    return rows


def parse_bybit(payload) -> list[dict[str, Any]]:
    rows = []
    lst = ((payload or {}).get("result") or {}).get("list") or []
    for k in lst:
        if not isinstance(k, (list, tuple)) or len(k) < 6:
            continue
        ts = k[0]
        o, h, l, c, v = _f(k[1]), _f(k[2]), _f(k[3]), _f(k[4]), _f(k[5])
        if ts is None or None in (o, h, l, c):
            continue
        rows.append({"ts": int(ts), "open": o, "high": h, "low": l, "close": c,
                     "volume": v if v is not None else 0.0})
    return rows


def _build_params(exchange, symbol, tf, start_ms, end_ms, limit):
    if exchange == "binance_futures":
        return {"symbol": symbol, "interval": _BINANCE_INTERVAL.get(tf, tf),
                "startTime": start_ms, "endTime": end_ms, "limit": limit}
    return {"category": "linear", "symbol": symbol,
            "interval": _BYBIT_INTERVAL.get(tf, "60"),
            "start": start_ms, "end": end_ms, "limit": limit}


def _parse(exchange, payload):
    return parse_binance(payload) if exchange == "binance_futures" else parse_bybit(payload)


# ---- bounded forward pagination -------------------------------------------

def fetch_series(tx, exchange, symbol, tf, *, days, request_budget,
                 rate_per_s, rep) -> tuple[list[dict[str, Any]], int]:
    bar_ms = TF_MS[tf]
    limit = _PAGE_LIMIT[exchange]
    window_ms = limit * bar_ms
    end_ms = _now_ms()
    start_ms = end_ms - int(days) * DAY_MS
    by_ts: dict[int, dict[str, Any]] = {}
    cursor, used, empties = start_ms, 0, 0
    while cursor < end_ms and used < request_budget:
        win_end = min(cursor + window_ms, end_ms)
        try:
            payload = tx(exchange, _build_params(exchange, symbol, tf, cursor, win_end, limit))
            used += 1
        except Exception as exc:
            rep["errors"].append(f"fetch_failed:{exchange}:{symbol}:{type(exc).__name__}")
            used += 1
            break
        page = _parse(exchange, payload)
        if not page:
            empties += 1
            if empties >= 2:
                break
            cursor = win_end
            continue
        empties = 0
        newest = max(r["ts"] for r in page)
        for r in page:
            by_ts[r["ts"]] = r
        if newest + bar_ms <= cursor:
            break  # no forward progress
        cursor = max(win_end, newest + bar_ms)
        if rate_per_s > 0:
            time.sleep(1.0 / rate_per_s)
    return [by_ts[k] for k in sorted(by_ts)], used


# ---- plan / fetch ---------------------------------------------------------

def cross_exchange_plan(exchanges=None, symbols=None, timeframe="1h", days=365) -> dict[str, Any]:
    exs = [e for e in (exchanges or list(EXCHANGES)) if e in EXCHANGES]
    syms = [str(s).strip().upper() for s in (symbols or []) if str(s).strip()]
    return {"tool_version": TOOL_VERSION, "generated_at": _now_iso(),
            "exchanges": exs, "symbols": syms, "timeframe": timeframe,
            "requested_days": int(days), "method": "GET", "auth": "none",
            "public_only": True, "no_network": True, "dry_run_by_default": True,
            "endpoints": {e: "https://" + _ALLOW[e][0] + _ALLOW[e][1] for e in exs},
            "planned_fetches": [f"{e}:{s}:{timeframe}" for e in exs for s in syms],
            "staging_target": STAGING_ROOT + "/<exchange>/<run_id>/",
            "note": "Binance/Bybit public klines serve ~1y of 1h; used to validate the "
                    "V10.14 lead beyond Bitget's ~31d cap.", **_safety()}


def cross_exchange_fetch(*, exchanges, symbols, timeframe="1h", days=365,
                         max_requests=400, apply=False, transport=None,
                         staging_root=None, rate_per_s=DEFAULT_RATE_PER_S) -> dict[str, Any]:
    exs = [e for e in exchanges if e in EXCHANGES]
    syms = [str(s).strip().upper() for s in symbols if str(s).strip()]
    tf = timeframe if timeframe in TF_MS else "1h"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    budget = min(MAX_REQUESTS_HARD, max(1, int(max_requests)))
    rep: dict[str, Any] = {
        "tool_version": TOOL_VERSION, "run_id": run_id, "exchanges": exs,
        "symbols": syms, "timeframe": tf, "requested_days": int(days),
        "max_requests": budget, "dry_run": (not apply), "requests_made": 0,
        "staging_dirs": {}, "coverage": [], "files": [], "errors": [],
        "warnings": [], "missing_symbols": [], **_safety()}
    if not exs or not syms:
        rep["errors"].append("nothing_to_fetch (need exchanges + symbols)")
        return rep
    if not apply:
        rep["planned_fetches"] = [f"{e}:{s}:{tf}" for e in exs for s in syms]
        rep["note"] = "dry-run: no network, no writes. Pass --apply to fetch public klines."
        return rep

    tx = transport or default_transport
    total_used = 0
    for ex in exs:
        run_dir = os.path.join(staging_root or STAGING_ROOT, ex, run_id).replace("\\", "/")
        block = safe_staging_dir(run_dir)
        if block is not None:
            rep["errors"].append(f"staging_dir_rejected:{ex}:{block}")
            rep["blocked"] = True
            return rep
        os.makedirs(run_dir, exist_ok=True)
        rep["staging_dirs"][ex] = run_dir
        for s in syms:
            remaining = budget - total_used
            if remaining <= 0:
                rep["warnings"].append("max_requests_reached")
                break
            rows, used = fetch_series(tx, ex, s, tf, days=days, request_budget=remaining,
                                      rate_per_s=rate_per_s, rep=rep)
            total_used += used
            if not rows:
                rep["missing_symbols"].append(f"{ex}:{s}")
                rep["warnings"].append(f"empty:{ex}:{s}")
                continue
            path = os.path.join(run_dir, f"{s}_{tf}_ohlcv.csv")
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
                for r in rows:
                    w.writerow([r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"]])
            span_days = round((rows[-1]["ts"] - rows[0]["ts"]) / DAY_MS, 2)
            q = analyze_series(rows, tf)
            rep["files"].append({"exchange": ex, "file": f"{s}_{tf}_ohlcv.csv",
                                 "rows": len(rows), "days_covered": span_days})
            rep["coverage"].append({"exchange": ex, "symbol": s, "rows": len(rows),
                                    "days_covered": span_days, "first_ts": rows[0]["ts"],
                                    "last_ts": rows[-1]["ts"], **q})
    rep["requests_made"] = total_used
    rep["note"] = "public GET klines only; flat OHLCV staged; no trades/orderbook/OI/liq."
    return rep


# ---- quality + break-even -------------------------------------------------

def analyze_series(rows, tf) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"gaps": 0, "duplicates": 0, "non_monotonic": 0, "invalid_ohlc": 0,
                "zero_volume": 0}
    interval = TF_MS.get(tf, 0)
    seen, dups, nonmono, invalid, zerov, gaps = set(), 0, 0, 0, 0, 0
    prev = None
    for r in rows:
        ts, o, h, l, c, v = r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"]
        if ts in seen:
            dups += 1
        seen.add(ts)
        if prev is not None and ts <= prev:
            nonmono += 1
        if (h < max(o, c)) or (l > min(o, c)) or (h < l) or min(o, h, l, c) <= 0:
            invalid += 1
        if v == 0:
            zerov += 1
        if prev is not None and interval > 0 and (ts - prev) > 1.5 * interval:
            gaps += 1
        prev = ts
    return {"gaps": gaps, "duplicates": dups, "non_monotonic": nonmono,
            "invalid_ohlc": invalid, "zero_volume": zerov}


def write_manifest(rep, *, output_dir=None) -> list[str]:
    base = output_dir or OUTPUT_ROOT
    # output_dir only ever a reports dir; never raw/db
    if not isinstance(base, str) or any(s in _FORBIDDEN_SEG or s.endswith(_FORBIDDEN_SUF)
                                        for s in (x.lower() for x in _segs(base))):
        base = OUTPUT_ROOT
    os.makedirs(base, exist_ok=True)
    written = []
    for ex in rep.get("exchanges", []):
        cov = [c for c in rep.get("coverage", []) if c["exchange"] == ex]
        if not cov:
            continue
        series = sorted(f"{c['symbol']}:{c['rows']}:{c['days_covered']}" for c in cov)
        manifest = {"exchange": ex, "run_id": rep.get("run_id"), "timeframe": rep.get("timeframe"),
                    "coverage": cov, "dataset_hash": hashlib.sha256("|".join(series).encode()).hexdigest(),
                    "staging_dir": rep.get("staging_dirs", {}).get(ex), **_safety()}
        p = os.path.join(base, f"{ex}_{rep.get('run_id')}_manifest.json").replace("\\", "/")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, default=str)
        written.append(p)
    return written
