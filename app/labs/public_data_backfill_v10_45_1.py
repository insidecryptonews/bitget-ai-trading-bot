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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE
from .ai_providers_v10_45_1 import _http_json, sanitize_error

TOOL_VERSION = "v10.45.1"
DATA_SUBDIR = ("external_data", "staging", "klines_v10_45_1")
BITGET_BASE = "https://api.bitget.com"
BYBIT_BASE = "https://api.bybit.com"
BAR_MS = 60_000
REQUEST_SLEEP_S = 0.15        # well under both venues' public IP limits


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


def fetch_bitget_1m(symbol: str, days: int, log=print) -> list[list]:
    """Paginate Bitget USDT-futures 1m candles backwards. Public, no keys.
    Row: [ts_ms, open, high, low, close, base_vol, quote_vol]."""
    rows: dict[int, list] = {}
    end = _now_ms()
    target_start = end - days * 86_400_000
    requests = 0
    while end > target_start and requests < 900:
        url = (f"{BITGET_BASE}/api/v2/mix/market/history-candles?symbol={symbol}"
               f"&productType=usdt-futures&granularity=1m&endTime={end}&limit=200")
        status, body, _ = _http_json(url, timeout=20)
        requests += 1
        data = (body or {}).get("data") or []
        if status != 200 or not data:
            log(f"  bitget stop: status={status} rows={len(data)} req={requests}")
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
            break
        end = batch_min - BAR_MS
        if requests % 50 == 0:
            log(f"  bitget {symbol}: {len(rows)} bars, back to "
                f"{datetime.fromtimestamp(end/1000, timezone.utc).isoformat()[:16]}")
        time.sleep(REQUEST_SLEEP_S)
    return [rows[k] for k in sorted(rows)]


def fetch_bybit_1m(symbol: str, days: int, log=print) -> list[list]:
    """Paginate Bybit linear 1m klines backwards (lists arrive NEWEST-FIRST;
    sorted ascending here). Row: [ts_ms, o, h, l, c, base_vol, turnover]."""
    rows: dict[int, list] = {}
    end = _now_ms()
    target_start = end - days * 86_400_000
    requests = 0
    while end > target_start and requests < 400:
        url = (f"{BYBIT_BASE}/v5/market/kline?category=linear&symbol={symbol}"
               f"&interval=1&limit=1000&end={end}")
        status, body, _ = _http_json(url, timeout=20)
        requests += 1
        data = ((body or {}).get("result") or {}).get("list") or []
        if status != 200 or not data:
            log(f"  bybit stop: status={status} rows={len(data)} req={requests}")
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
            break
        end = batch_min - BAR_MS
        if requests % 20 == 0:
            log(f"  bybit {symbol}: {len(rows)} bars, back to "
                f"{datetime.fromtimestamp(end/1000, timezone.utc).isoformat()[:16]}")
        time.sleep(REQUEST_SLEEP_S)
    return [rows[k] for k in sorted(rows)]


def _gap_report(ts_list: list[int]) -> dict[str, Any]:
    gaps = []
    for i in range(1, len(ts_list)):
        d = ts_list[i] - ts_list[i - 1]
        if d > BAR_MS:
            gaps.append({"after": ts_list[i - 1], "missing_bars": d // BAR_MS - 1})
    total_missing = sum(g["missing_bars"] for g in gaps)
    return {"gap_count": len(gaps), "missing_bars": total_missing,
            "largest_gap_bars": max((g["missing_bars"] for g in gaps), default=0),
            "recent_gaps": gaps[-5:]}


def save_dataset(venue: str, symbol: str, rows: list[list],
                 requested_days: int) -> dict[str, Any]:
    d = _data_dir()
    name = f"{venue}_{symbol}_1m"
    path = d / f"{name}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume", "turnover"])
        for r in rows:
            w.writerow(r)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    ts_list = [r[0] for r in rows]
    manifest = {
        "tool_version": TOOL_VERSION,
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
        "duplicates": 0,          # dict-keyed by ts during fetch -> deduped
        **_gap_report(ts_list),
        "sha256": sha,
        "availability_contract": "bar open ts T is available at T+60000ms (close)",
        "limitations": ("aggregated OHLCV only: no per-side flow, no book, no "
                        "trades; funding/OI not included; venue clock assumed UTC"),
        "license_note": ("public market-data endpoint, no auth, used for "
                         "personal research within venue API ToS rate limits"),
        "path": str(path).replace("\\", "/"),
        **_safety()}
    mpath = d / f"{name}_manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return manifest


def load_klines(venue: str, symbol: str) -> list[dict]:
    """Load a saved kline CSV as canonical bar dicts (ascending, deduped).
    available_at = bar close (open ts + 60s): strictly no lookahead."""
    path = _data_dir() / f"{venue}_{symbol}_1m.csv"
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
