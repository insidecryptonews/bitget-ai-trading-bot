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

TOOL_VERSION = "v10.45.4"
DOWNLOADER_VERSION = ("v10.45.4 (pagination end=batch_min live-probed; window "
                      "trim to requested range; download completeness contract; "
                      "raw per-candle validation; symlink+hardlink containment; "
                      "atomic fsync writes)")
DATA_SUBDIR = ("external_data", "staging", "klines_v10_45_4")
COMPLETENESS_TOLERANCE_BARS = 3   # last candle may still be open + boundary jitter
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
    """Build an output path and PROVE it stays inside the data directory —
    including when the data directory itself (or a parent) is a symlink or
    Windows junction pointing outside the repo (reparse-point escape)."""
    if venue not in ALLOWED_VENUES:
        raise ValueError(f"invalid venue: {str(venue)[:20]!r}")
    sym = validate_symbol(symbol)
    base = _data_dir()
    repo_real = Path(os.path.realpath(str(CE._repo_root())))
    base_real = Path(os.path.realpath(str(base)))
    if repo_real != base_real and repo_real not in base_real.parents:
        raise ValueError("data directory escapes the repository root "
                         "(symlink/junction detected)")
    p = Path(os.path.realpath(str(base / f"{venue}_{sym}_1m{suffix}")))
    if base_real != p.parent and base_real not in p.parents:
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


def fetch_bitget_1m(symbol: str, days: int, log=print,
                    end_ms: int | None = None) -> list[list]:
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
    now = end_ms if end_ms is not None else _now_ms()
    return paginate_klines(page, now, now - days * 86_400_000, 900, log=log,
                           label=f"bitget {symbol}")


def fetch_bybit_1m(symbol: str, days: int, log=print,
                   end_ms: int | None = None) -> list[list]:
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
    now = end_ms if end_ms is not None else _now_ms()
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


def validate_raw_candle(row: list) -> bool:
    """Per-candle validation BEFORE anything downstream touches it:
    finite OHLCV, non-negative volume/turnover, coherent OHLC, valid ts."""
    try:
        ts = int(row[0])
        o, h, l, c, v, t = (float(row[1]), float(row[2]), float(row[3]),
                            float(row[4]), float(row[5]), float(row[6]))
    except (TypeError, ValueError, IndexError):
        return False
    import math as _m
    if ts <= 0 or not all(_m.isfinite(x) for x in (o, h, l, c, v, t)):
        return False
    if v < 0 or t < 0 or o <= 0 or c <= 0 or l <= 0:
        return False
    if h < max(o, c) or l > min(o, c) or l > h:
        return False
    return True


def raw_quality_report(rows: list[list]) -> dict[str, Any]:
    """Raw-candle quality over the full download; resampling and research are
    only allowed on a PASS (5m/15m quality can never hide 1m defects)."""
    invalid = sum(1 for r in rows if not validate_raw_candle(r))
    ts_q = strict_quality([int(r[0]) for r in rows if validate_raw_candle(r)])
    return {**ts_q, "invalid_candles": invalid,
            "raw_quality_pass": bool(ts_q["quality_pass"] and invalid == 0)}


def verify_dataset(venue: str, symbol: str) -> dict[str, Any]:
    """FAIL-CLOSED dataset verification, mandatory before resampling or
    feature construction. Structured statuses, never silent trimming."""
    try:
        csv_path = _contained_path(venue, symbol, ".csv")
        man_path = _contained_path(venue, symbol, "_manifest.json")
    except ValueError as exc:
        return {"ok": False, "status": "INVALID_DATA_PATH",
                "detail": str(exc)[:120]}
    if not man_path.is_file():
        return {"ok": False, "status": "INVALID_DATA_MANIFEST_MISSING"}
    try:
        manifest = json.loads(man_path.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": False, "status": "INVALID_DATA_MANIFEST_CORRUPT"}
    if not csv_path.is_file():
        return {"ok": False, "status": "INVALID_DATA_CSV_MISSING"}
    sha = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    if sha != manifest.get("sha256"):
        return {"ok": False, "status": "INVALID_DATA_SHA_MISMATCH",
                "manifest_sha": str(manifest.get("sha256"))[:16],
                "actual_sha": sha[:16]}
    if manifest.get("download_complete") is not True:
        return {"ok": False, "status": "INVALID_DATA_DOWNLOAD_INCOMPLETE",
                "coverage_ratio": manifest.get("coverage_ratio")}
    if manifest.get("raw_quality_pass") is not True:
        return {"ok": False, "status": "INVALID_DATA_RAW_QUALITY",
                "invalid_candles": manifest.get("invalid_candles")}
    if (manifest.get("gap_count") or 0) > 0:
        return {"ok": False, "status": "INVALID_DATA_GAPS",
                "gap_count": manifest.get("gap_count")}
    if (manifest.get("duplicates") or 0) > 0 or \
            (manifest.get("out_of_order") or 0) > 0 or \
            (manifest.get("irregular_deltas") or 0) > 0:
        return {"ok": False, "status": "INVALID_DATA_IRREGULAR"}
    return {"ok": True, "status": "DATASET_VERIFIED", "sha256": sha,
            "manifest": manifest}


def safe_atomic_write(path, data: bytes) -> str:
    """Containment-validated atomic write: temp file inside the validated
    root, flush + fsync, atomic replace, post-write SHA verification. Refuses
    to overwrite hardlinked destinations and never opens the final path for
    destructive writing directly."""
    path = Path(path)
    base = Path(os.path.realpath(str(path.parent)))
    repo_real = Path(os.path.realpath(str(CE._repo_root())))
    if repo_real != base and repo_real not in base.parents:
        raise ValueError("write target escapes repository root")
    if path.exists():
        st_ = os.stat(path)
        if getattr(st_, "st_nlink", 1) > 1:
            raise ValueError("refusing to overwrite a hardlinked file")
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = hashlib.sha256(data).hexdigest()
    if sha != expected:
        raise IOError("post-write SHA verification failed")
    return sha


def _repo_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(CE._repo_root()))
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def save_dataset(venue: str, symbol: str, rows: list[list],
                 requested_days: int,
                 requested_start_ms: int | None = None,
                 requested_end_ms: int | None = None) -> dict[str, Any]:
    """Trim to the REQUESTED window, validate every raw candle, record the
    full completeness contract and write CSV + manifest atomically."""
    if requested_end_ms is None:
        requested_end_ms = (_now_ms() // BAR_MS) * BAR_MS
    if requested_start_ms is None:
        requested_start_ms = requested_end_ms - requested_days * 86_400_000
    # hard trim: never keep bars outside the requested interval
    rows = [r for r in rows
            if requested_start_ms <= int(r[0]) < requested_end_ms]
    raw_q = raw_quality_report(rows)
    path = _contained_path(venue, symbol, ".csv")
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["ts", "open", "high", "low", "close", "volume", "turnover"])
    for r in rows:
        w.writerow(r)
    sha = safe_atomic_write(path, buf.getvalue().encode("utf-8"))
    ts_list = [r[0] for r in rows]
    expected_bars = max(0, (requested_end_ms - requested_start_ms) // BAR_MS)
    coverage_ratio = (len(rows) / expected_bars) if expected_bars else 0.0
    download_complete = (expected_bars - len(rows)) <= COMPLETENESS_TOLERANCE_BARS \
        and raw_q["raw_quality_pass"]
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
        "requested_start_ms": int(requested_start_ms),
        "requested_end_ms": int(requested_end_ms),
        "requested_start": datetime.fromtimestamp(
            requested_start_ms / 1000, timezone.utc).isoformat(),
        "requested_end": datetime.fromtimestamp(
            requested_end_ms / 1000, timezone.utc).isoformat(),
        "actual_start": (datetime.fromtimestamp(ts_list[0] / 1000, timezone.utc)
                         .isoformat() if ts_list else None),
        "actual_end": (datetime.fromtimestamp(ts_list[-1] / 1000, timezone.utc)
                       .isoformat() if ts_list else None),
        "actual_coverage_seconds": ((ts_list[-1] - ts_list[0]) // 1000 + 60
                                    if ts_list else 0),
        "expected_bars": expected_bars,
        "actual_bars": len(rows),
        "coverage_ratio": round(coverage_ratio, 6),
        "completeness_tolerance_bars": COMPLETENESS_TOLERANCE_BARS,
        "download_complete": download_complete,
        "n_bars": len(rows),
        "period_start": (datetime.fromtimestamp(ts_list[0] / 1000, timezone.utc)
                         .isoformat() if ts_list else None),
        "period_end": (datetime.fromtimestamp(ts_list[-1] / 1000, timezone.utc)
                       .isoformat() if ts_list else None),
        "invalid_candles": raw_q["invalid_candles"],
        "raw_quality_pass": raw_q["raw_quality_pass"],
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
    safe_atomic_write(mpath, json.dumps(manifest, indent=2,
                                        default=str).encode("utf-8"))
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
    # ONE shared requested window (minute-aligned) for every dataset in the
    # run, so all manifests state the same explicit interval and every CSV is
    # trimmed to exactly that window
    req_end = (_now_ms() // BAR_MS) * BAR_MS
    req_start = req_end - days * 86_400_000
    manifests = []
    for sym in symbols_bitget:
        log(f"fetch bitget {sym} {days}d ...")
        rows = fetch_bitget_1m(sym, days, log=log, end_ms=req_end)
        m = save_dataset("bitget", sym, rows, days,
                         requested_start_ms=req_start, requested_end_ms=req_end)
        log(f"  -> {m['n_bars']}/{m['expected_bars']} bars, gaps={m['gap_count']}, "
            f"complete={m['download_complete']}, sha={m['sha256'][:12]}")
        manifests.append(m)
    for sym in symbols_bybit:
        log(f"fetch bybit {sym} {days}d ...")
        rows = fetch_bybit_1m(sym, days, log=log, end_ms=req_end)
        m = save_dataset("bybit", sym, rows, days,
                         requested_start_ms=req_start, requested_end_ms=req_end)
        log(f"  -> {m['n_bars']}/{m['expected_bars']} bars, gaps={m['gap_count']}, "
            f"complete={m['download_complete']}, sha={m['sha256'][:12]}")
        manifests.append(m)
    summary = {"tool_version": TOOL_VERSION,
               "ran_at": datetime.now(timezone.utc).isoformat(),
               "datasets": manifests, **_safety()}
    out = CE._repo_root().joinpath("reports", "research", "v10_45_4_edge_discovery")
    out.mkdir(parents=True, exist_ok=True)
    (out / "data_backfill_manifest_v10_45_4.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary
