"""ResearchOps V10.45.1 - Public historical kline backfill (research only).

Downloads PUBLIC 1m klines from Bitget (target venue) and Bybit (cross-venue
reference) — no keys, no private endpoints, market-data endpoints only — and
registers every dataset with a full manifest: source, venue, symbol, period,
timezone, gaps, duplicates, checksum, availability contract and limitations.

The point-in-time contract for downstream research: a bar with open time T is
COMPLETE and available at T + timeframe. Features may only use bars whose
close time <= decision time; entries execute at the NEXT bar open.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE
from .ai_providers_v10_45_1 import _http_json, sanitize_error

TOOL_VERSION = "v10.45.2"
DOWNLOADER_VERSION = "v10.45.2 (pagination end=batch_min-1: no candle dropped "
DOWNLOADER_VERSION += "at page boundaries; strict delta==T quality audit)"
DATA_SUBDIR = ("external_data", "staging", "klines_v10_45_2")
BITGET_BASE = "https://api.bitget.com"
BYBIT_BASE = "https://api.bybit.com"
BAR_MS = 60_000
REQUEST_SLEEP_S = 0.15        # well under both venues' public IP limits
SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,20}$")
ALLOWED_VENUES = ("bitget", "bybit")
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} \
    | {f"LPT{i}" for i in range(1, 10)}


def validate_symbol(symbol: str) -> str:
    """Strict whitelist: uppercase alnum, 3-20 chars, no path metacharacters,
    no reserved device names. Raises ValueError on anything else."""
    s = str(symbol or "")
    if not SYMBOL_RE.fullmatch(s) or s.upper() in WINDOWS_RESERVED:
        raise ValueError(f"invalid symbol: {s[:40]!r}")
    return s


def _contained_path(venue: str, symbol: str, suffix: str) -> Path:
    """Build an output path and PROVE it stays inside the data directory."""
    if venue not in ALLOWED_VENUES:
        raise ValueError(f"invalid venue: {str(venue)[:20]!r}")
    sym = validate_symbol(symbol)
    base = _data_dir().resolve()
    p = (base / f"{venue}_{sym}_1m{suffix}").resolve()
    if base not in p.parents:
        raise ValueError("path escapes data directory")
    return p


def _safety() -> dict[str, Any]:
    return {"research_only": True, "public_endpoints_only": True,
            "uses_api_keys": False, "can_send_real_orders": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _data_dir() -> Path:
    d = CE._repo_root().joinpath(*DATA_SUBDIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now_ms() -> int:
    return int(time.time() * 1000)


def paginate_klines(fetch_page, end_ms: int, target_start_ms: int,
                    max_requests: int, log=print, label: str = "") -> list[list]:
    """Generic backwards pagination with NO candle lost at page boundaries.

    fetch_page(end_ms) -> list of raw rows (any order). The next page continues
    from `end = batch_min` (the smallest open-ts of the batch). Probed LIVE on
    Bitget: /history-candles returns candles whose CLOSE <= endTime, so with
    endTime = batch_min the candle opening at batch_min - BAR_MS (closing at
    batch_min) is included and batch_min itself is not re-sent. For venues
    keyed on the open (inclusive or exclusive) the same endTime either
    re-sends batch_min (absorbed by the ts-keyed dict) or continues cleanly —
    no candle is lost under ANY of the three semantics. The V10.45.1 bug
    subtracted a full BAR_MS, silently dropping one candle per page (644
    phantom 'gaps' in 129k bars = exactly 1 per page)."""
    rows: dict[int, list] = {}
    end = end_ms
    requests = 0
    while end > target_start_ms and requests < max_requests:
        data = fetch_page(end)
        requests += 1
        if not data:
            log(f"  {label} stop: empty page at req={requests}")
            break
        batch_min = None
        for r in data:
            try:
                ts = int(r[0])
                rows[ts] = [ts, float(r[1]), float(r[2]), float(r[3]),
                            float(r[4]), float(r[5]), float(r[6])]
                batch_min = ts if batch_min is None else min(batch_min, ts)
            except (ValueError, IndexError, TypeError):
                continue
        if batch_min is None or batch_min >= end:
            break                              # no progress -> stop, never loop
        end = batch_min
        if requests % 50 == 0:
            log(f"  {label}: {len(rows)} bars, back to "
                f"{datetime.fromtimestamp(end/1000, timezone.utc).isoformat()[:16]}")
        time.sleep(REQUEST_SLEEP_S)
    return [rows[k] for k in sorted(rows)]


def fetch_bitget_1m(symbol: str, days: int, log=print) -> list[list]:
    """Paginate Bitget USDT-futures 1m candles backwards. Public, no keys."""
    symbol = validate_symbol(symbol)

    def page(end: int) -> list:
        url = (f"{BITGET_BASE}/api/v2/mix/market/history-candles?symbol={symbol}"
               f"&productType=usdt-futures&granularity=1m&endTime={end}&limit=200")
        status, body, _ = _http_json(url, timeout=20)
        if status != 200:
            log(f"  bitget HTTP {status}")
            return []
        return (body or {}).get("data") or []
    now = _now_ms()
    return paginate_klines(page, now, now - days * 86_400_000, 900, log=log,
                           label=f"bitget {symbol}")


def fetch_bybit_1m(symbol: str, days: int, log=print) -> list[list]:
    """Paginate Bybit linear 1m klines backwards (lists arrive NEWEST-FIRST)."""
    symbol = validate_symbol(symbol)

    def page(end: int) -> list:
        url = (f"{BYBIT_BASE}/v5/market/kline?category=linear&symbol={symbol}"
               f"&interval=1&limit=1000&end={end}")
        status, body, _ = _http_json(url, timeout=20)
        if status != 200:
            log(f"  bybit HTTP {status}")
            return []
        return ((body or {}).get("result") or {}).get("list") or []
    now = _now_ms()
    return paginate_klines(page, now, now - days * 86_400_000, 400, log=log,
                           label=f"bybit {symbol}")


def strict_quality(ts_list: list[int], bar_ms: int = BAR_MS) -> dict[str, Any]:
    """STRICT continuity: only delta == bar_ms is continuous. Anything else is
    a gap, duplicate or irregularity — a 2-minute step on 1m data is a gap."""
    gaps = dups = irregular = out_of_order = 0
    gap_list = []
    for i in range(1, len(ts_list)):
        d = ts_list[i] - ts_list[i - 1]
        if d == bar_ms:
            continue
        if d == 0:
            dups += 1
        elif d < 0:
            out_of_order += 1
        elif d % bar_ms == 0:
            gaps += 1
            gap_list.append({"after": ts_list[i - 1],
                             "missing_bars": d // bar_ms - 1})
        else:
            irregular += 1
    missing = sum(g["missing_bars"] for g in gap_list)
    span_bars = ((ts_list[-1] - ts_list[0]) // bar_ms + 1) if ts_list else 0
    coverage = len(ts_list) / span_bars if span_bars else 0.0
    return {"gap_count": gaps, "missing_bars": missing,
            "duplicates": dups, "out_of_order": out_of_order,
            "irregular_deltas": irregular,
            "largest_gap_bars": max((g["missing_bars"] for g in gap_list), default=0),
            "recent_gaps": gap_list[-5:],
            "coverage": round(coverage, 6),
            "quality_pass": (gaps == 0 and dups == 0 and out_of_order == 0
                             and irregular == 0 and coverage >= 0.999)}


def _repo_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(CE._repo_root()))
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def save_dataset(venue: str, symbol: str, rows: list[list],
                 requested_days: int) -> dict[str, Any]:
    path = _contained_path(venue, symbol, ".csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume", "turnover"])
        for r in rows:
            w.writerow(r)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    ts_list = [r[0] for r in rows]
    manifest = {
        "tool_version": TOOL_VERSION,
        "downloader_version": DOWNLOADER_VERSION,
        "repo_commit": _repo_commit(),
        "source": ("bitget public /api/v2/mix/market/history-candles"
                   if venue == "bitget" else "bybit public /v5/market/kline"),
        "venue": venue, "symbol": symbol, "timeframe": "1m",
        "timezone": "UTC (epoch ms)",
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "requested_days": requested_days,
        "n_bars": len(rows),
        "period_start": (datetime.fromtimestamp(ts_list[0] / 1000, timezone.utc)
                         .isoformat() if ts_list else None),
        "period_end": (datetime.fromtimestamp(ts_list[-1] / 1000, timezone.utc)
                       .isoformat() if ts_list else None),
        **strict_quality(ts_list),
        "sha256": sha,
        "availability_contract": "bar open ts T is available at T+60000ms (close)",
        "limitations": ("aggregated OHLCV only: no per-side flow, no book, no "
                        "trades; funding/OI not included; venue clock assumed UTC"),
        "license_note": ("public market-data endpoint, no auth, used for "
                         "personal research within venue API ToS rate limits"),
        "path": str(path).replace("\\", "/"),
        **_safety()}
    mpath = _contained_path(venue, symbol, "_manifest.json")
    mpath.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return manifest


def load_manifest(venue: str, symbol: str) -> dict[str, Any] | None:
    try:
        p = _contained_path(venue, symbol, "_manifest.json")
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_klines(venue: str, symbol: str) -> list[dict]:
    """Load a saved kline CSV as canonical bar dicts (ascending, deduped).
    available_at = bar close (open ts + 60s): strictly no lookahead."""
    path = _contained_path(venue, symbol, ".csv")
    if not path.is_file():
        return []
    bars: list[dict] = []
    with open(path, "r", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                ts = int(r["ts"])
                bars.append({"ts": ts, "bar_open_ts": ts,
                             "bar_close_ts": ts + BAR_MS,
                             "available_at": ts + BAR_MS,
                             "open": float(r["open"]), "high": float(r["high"]),
                             "low": float(r["low"]), "close": float(r["close"]),
                             "volume": float(r["volume"]),
                             "turnover": float(r["turnover"]),
                             "symbol": symbol, "venue": venue})
            except (KeyError, ValueError, TypeError):
                continue
    bars.sort(key=lambda b: b["ts"])
    return bars


def run_backfill(symbols_bitget: list[str], symbols_bybit: list[str],
                 days: int = 90, log=print) -> dict[str, Any]:
    manifests = []
    for sym in symbols_bitget:
        log(f"fetch bitget {sym} {days}d ...")
        rows = fetch_bitget_1m(sym, days, log=log)
        m = save_dataset("bitget", sym, rows, days)
        log(f"  -> {m['n_bars']} bars, {m['gap_count']} gaps, sha={m['sha256'][:12]}")
        manifests.append(m)
    for sym in symbols_bybit:
        log(f"fetch bybit {sym} {days}d ...")
        rows = fetch_bybit_1m(sym, days, log=log)
        m = save_dataset("bybit", sym, rows, days)
        log(f"  -> {m['n_bars']} bars, {m['gap_count']} gaps, sha={m['sha256'][:12]}")
        manifests.append(m)
    summary = {"tool_version": TOOL_VERSION,
               "ran_at": datetime.now(timezone.utc).isoformat(),
               "datasets": manifests, **_safety()}
    out = CE._repo_root().joinpath("reports", "research", "v10_45_1_edge_discovery")
    out.mkdir(parents=True, exist_ok=True)
    (out / "data_backfill_manifest_v10_45_1.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary
