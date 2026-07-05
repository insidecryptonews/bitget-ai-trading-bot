"""ResearchOps V10.36 - Bybit Official Backfill Importer + Coverage Probes.

Imports OFFICIAL Bybit daily trade dumps (public.bybit.com/trading/<SYM>/,
live-probed: 2020-03-25 through yesterday) and measures REAL OI/funding REST
coverage instead of assuming it. Everything is research acceleration only:

- trades dumps: APTO_PARA_RESEARCH (official, same exchange, tick-level);
- funding/OI REST: apt only per MEASURED coverage (OI 2020 returned empty in
  live probes -- never assume);
- orderbook/liquidations: forward-only (no free historical source) => backfill
  NEVER completes full microstructure readiness and never touches the V10.32
  forward dataset (separate staging, explicit-merge-only by design).

Safety: public GET only (exact allowlist), no keys, no `.env`, dry-run by
default, one day per invocation (NO mass download), download size caps, safe
gzip streaming, hardened staging, source attribution + sha256 in the manifest.
FINAL_RECOMMENDATION: NO LIVE.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import free_public_liquidations_ws_collector_v10_26 as V26

TOOL_VERSION = "v10.36"
STAGING_MARKER = "bybit_backfill_v10_36"
DEFAULT_STAGING_DIR = f"external_data/staging/{STAGING_MARKER}"
SOURCE_EXCHANGE = "bybit_linear"
_DUMP_HOST = "public.bybit.com"
_API_HOST = "api.bybit.com"
_SYM_RE = re.compile(r"^[A-Z0-9]{2,20}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024        # compressed cap per day
MAX_ROWS_PER_DAY = 5_000_000                  # sanity cap on decompressed rows

_FORBIDDEN_SEG = V26._FORBIDDEN_SEG
_FORBIDDEN_SUF = V26._FORBIDDEN_SUF

TRADES_HEADER = ["timestamp", "symbol", "price", "size", "aggressor_side",
                 "trade_id", "source_exchange", "backfill"]


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "paper_ready": False,
            "live_ready": False, "can_send_real_orders": False,
            "backfill_completes_readiness": False,
            "merged_into_forward_dataset": False,
            "source_exchange": SOURCE_EXCHANGE,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Allowlist + staging (established hardened patterns)
# --------------------------------------------------------------------------

def _dump_url(symbol: str, date: str) -> str:
    if not _SYM_RE.match(symbol):
        raise ValueError(f"symbol not allowlisted: {symbol}")
    if not _DATE_RE.match(date):
        raise ValueError(f"bad date (YYYY-MM-DD): {date}")
    return f"https://{_DUMP_HOST}/trading/{symbol}/{symbol}{date}.csv.gz"


def _listing_url(symbol: str) -> str:
    if not _SYM_RE.match(symbol):
        raise ValueError(f"symbol not allowlisted: {symbol}")
    return f"https://{_DUMP_HOST}/trading/{symbol}/"


def assert_safe_request(url: str, headers: dict | None = None) -> bool:
    p = urllib.parse.urlparse(url)
    if p.scheme != "https":
        raise ValueError(f"https only: {url}")
    if p.netloc == _DUMP_HOST:
        if not p.path.startswith("/trading/"):
            raise ValueError(f"dump path not allowlisted: {p.path}")
    elif p.netloc == _API_HOST:
        if p.path not in ("/v5/market/open-interest", "/v5/market/funding/history"):
            raise ValueError(f"api path not allowlisted: {p.path}")
    else:
        raise ValueError(f"host not allowlisted: {p.netloc}")
    for k in (headers or {}):
        lk = str(k).lower()
        if "key" in lk or "sign" in lk or "auth" in lk or lk == "cookie":
            raise ValueError(f"auth-like header blocked: {k}")
    return True


def default_transport(url: str, headers: dict[str, str]) -> bytes:
    assert_safe_request(url, headers)
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read(MAX_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"download exceeds cap ({MAX_DOWNLOAD_BYTES} bytes)")
        return data


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def safe_staging_dir(base: str | None = None) -> Path:
    root = base or DEFAULT_STAGING_DIR
    segs = [s for s in str(root).replace("\\", "/").split("/") if s]
    if ".." in segs:
        raise ValueError("staging traversal blocked")
    for s in (x.lower() for x in segs):
        if s in _FORBIDDEN_SEG or s.endswith(_FORBIDDEN_SUF) or ".env" in s:
            raise ValueError(f"forbidden staging segment: {s}")
    repo = _repo_root()
    logical = repo / "external_data" / "staging" / STAGING_MARKER
    target = Path(root)
    if not target.is_absolute():
        target = repo / target
    target = Path(os.path.normpath(str(target)))
    if target != logical and logical not in target.parents:
        raise ValueError(f"staging dir must be inside external_data/staging/{STAGING_MARKER}")
    for anc in [target, *target.parents]:
        if anc == repo or anc in repo.parents:
            break
        try:
            if anc.exists() and anc.is_symlink():
                raise ValueError(f"symlinked staging component blocked: {anc}")
        except OSError:
            break
    rtgt = target.resolve(strict=False)
    try:
        rtgt.relative_to(repo.resolve())
    except ValueError:
        raise ValueError("staging dir resolves outside the repo")
    return target


# --------------------------------------------------------------------------
# Plan / probe (no downloads)
# --------------------------------------------------------------------------

def plan() -> dict[str, Any]:
    return {"tool_version": TOOL_VERSION,
            "objective": ("import OFFICIAL Bybit daily trade dumps + measure real "
                          "OI/funding REST coverage to accelerate RESEARCH (years of "
                          "history) without touching forward readiness"),
            "classification": {
                "trades_dumps": "APTO_PARA_RESEARCH (official, tick-level, 2020->yesterday)",
                "funding_rest": "apt per MEASURED coverage",
                "oi_rest": "apt per MEASURED coverage (2020 probed EMPTY)",
                "orderbook": "forward-only (no free history)",
                "liquidations": "forward-only (no free history)"},
            "never": ["mass_download_by_default", "paid_providers", "keys",
                      "merge_into_forward_without_explicit_flag",
                      "READY_from_backfill", "invented_coverage"],
            "staging": DEFAULT_STAGING_DIR,
            "default_mode": "DRY_RUN", "writes_on_plan": False, **_safety()}


def probe_available_days(symbol: str = "BTCUSDT",
                         transport: Callable | None = None) -> dict[str, Any]:
    """List available dump days from the official directory listing (one small
    GET; downloads NOTHING)."""
    tr = transport or default_transport
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "symbol": symbol, **_safety()}
    try:
        html = tr(_listing_url(symbol), {"User-Agent": "researchops/1.0"}).decode(
            "utf-8", "ignore")
        days = re.findall(rf"{symbol}(\d{{4}}-\d{{2}}-\d{{2}})\.csv\.gz", html)
        rep["available_days"] = len(days)
        rep["first_day"] = min(days) if days else None
        rep["last_day"] = max(days) if days else None
        rep["source_url"] = _listing_url(symbol)
    except Exception as e:
        rep["error"] = f"{type(e).__name__}:{str(e)[:80]}"
    return rep


# --------------------------------------------------------------------------
# Import one day (download + safe gunzip + convert). NO mass download.
# --------------------------------------------------------------------------

def import_day(symbol: str, date: str, apply: bool = False,
               transport: Callable | None = None,
               output_dir: str | None = None) -> dict[str, Any]:
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "symbol": symbol,
                           "date": date, "apply": bool(apply), "errors": [],
                           "imported_rows": 0, **_safety()}
    try:
        url = _dump_url(symbol, date)
    except ValueError as e:
        rep["mode"] = "APPLY" if apply else "DRY_RUN"
        rep["errors"].append(str(e))
        return rep
    rep["source_url"] = url
    if not apply:
        rep["mode"] = "DRY_RUN"
        rep["note"] = "dry-run: no download, no writes; use --apply for ONE day"
        return rep
    rep["mode"] = "APPLY"
    try:
        base = safe_staging_dir(output_dir) / symbol
    except ValueError as e:
        rep["errors"].append(f"unsafe_output_dir:{e}")
        return rep
    out_csv = base / f"trades_{date}.csv"
    if out_csv.exists():
        rep["note"] = "already imported (idempotent skip); delete the file to re-import"
        rep["skipped_existing"] = True
        return rep
    tr = transport or default_transport
    try:
        raw = tr(url, {"User-Agent": "researchops/1.0"})
    except Exception as e:
        msg = str(e)
        tag = "RATE_LIMITED" if "429" in msg else type(e).__name__
        rep["errors"].append(f"download:{tag}:{msg[:80]}")
        return rep
    sha = hashlib.sha256(raw).hexdigest()
    os.makedirs(base, exist_ok=True)
    bad_rows = 0
    non_monotonic_input = 0
    prev_ts = None
    parsed: list[tuple[int, dict]] = []
    tmp = base / f"trades_{date}.csv.tmp"
    try:
        with gzip.open(io.BytesIO(raw), "rt", encoding="utf-8", errors="ignore") as gz:
            for r in csv.DictReader(gz):
                if len(parsed) >= MAX_ROWS_PER_DAY:
                    rep["errors"].append("row_cap_reached")
                    break
                try:
                    ts = int(float(r["timestamp"]) * 1000)     # dump: seconds
                    side = str(r.get("side") or "").lower()
                    price, size = r.get("price"), r.get("size")
                    if side not in ("buy", "sell") or float(price) <= 0 or float(size) <= 0:
                        bad_rows += 1
                        continue
                except (KeyError, TypeError, ValueError):
                    bad_rows += 1
                    continue
                if prev_ts is not None and ts < prev_ts:
                    non_monotonic_input += 1        # official dumps arrive DESC
                prev_ts = ts
                parsed.append((ts, {"timestamp": ts, "symbol": symbol, "price": price,
                                    "size": size, "aggressor_side": side,
                                    "trade_id": str(r.get("trdMatchID") or ""),
                                    "source_exchange": SOURCE_EXCHANGE,
                                    "backfill": "true"}))
        # write ASCENDING regardless of input order (row cap bounds memory)
        parsed.sort(key=lambda t: t[0])
        with open(tmp, "w", newline="", encoding="utf-8") as out:
            w = csv.DictWriter(out, fieldnames=TRADES_HEADER)
            w.writeheader()
            for _, row in parsed:
                w.writerow(row)
    except Exception as e:
        rep["errors"].append(f"gunzip_or_parse:{type(e).__name__}:{str(e)[:60]}")
        try:
            tmp.unlink()
        except OSError:
            pass
        return rep
    os.replace(tmp, out_csv)
    rep["imported_rows"] = len(parsed)
    rep["bad_rows"] = bad_rows
    rep["non_monotonic_input_pairs"] = non_monotonic_input   # diagnostic, visible
    rep["output_sorted_ascending"] = True
    rep["first_ts"] = parsed[0][0] if parsed else None
    rep["last_ts"] = parsed[-1][0] if parsed else None
    rep["file"] = str(out_csv).replace("\\", "/")
    # cumulative manifest with full source attribution
    man_path = base / "manifest.json"
    try:
        man = json.loads(man_path.read_text(encoding="utf-8"))
    except Exception:
        man = {"days": {}}
    man.update({"tool_version": TOOL_VERSION, "symbol": symbol,
                "source_exchange": SOURCE_EXCHANGE,
                "source": "official public.bybit.com daily trade dumps",
                "license_note": "Bybit public market-data dumps (free, official)",
                "backfill_completes_readiness": False,
                "research_only": True,
                "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE})
    man["days"][date] = {"url": url, "sha256": sha, "rows": rep["imported_rows"],
                         "bad_rows": bad_rows,
                         "non_monotonic_input_pairs": non_monotonic_input,
                         "output_sorted_ascending": True,
                         "first_ts": rep["first_ts"], "last_ts": rep["last_ts"],
                         "imported_at": _now_iso()}
    man_path.write_text(json.dumps(man, indent=2, default=str), encoding="utf-8")
    rep["manifest"] = str(man_path).replace("\\", "/")
    return rep


# --------------------------------------------------------------------------
# Coverage probes for OI / funding REST (measure, never assume)
# --------------------------------------------------------------------------

def coverage_probe(kind: str, symbol: str, start_ms: int, end_ms: int,
                   transport: Callable | None = None,
                   max_requests: int = 8) -> dict[str, Any]:
    """Windowed pagination probe. Verdicts: COVERAGE_OK / PARTIAL_COVERAGE /
    NO_DATA / RATE_LIMITED / UNKNOWN. Bounded requests; read-only."""
    rep: dict[str, Any] = {"tool_version": TOOL_VERSION, "kind": kind,
                           "symbol": symbol, "source_exchange": SOURCE_EXCHANGE,
                           "start_requested": start_ms, "end_requested": end_ms,
                           "rows": 0, "first_available": None,
                           "last_available": None, "requests_used": 0,
                           "rate_limited": False, **_safety()}
    if kind not in ("oi", "funding") or not _SYM_RE.match(symbol):
        rep["coverage_verdict"] = "UNKNOWN"
        rep["error"] = "bad kind or symbol"
        return rep
    tr = transport or default_transport
    s = urllib.parse.quote(symbol)
    if kind == "funding":
        base = (f"https://{_API_HOST}/v5/market/funding/history?category=linear"
                f"&symbol={s}&limit=200")
        ts_field, list_getter = "fundingRateTimestamp", None
    else:
        base = (f"https://{_API_HOST}/v5/market/open-interest?category=linear"
                f"&symbol={s}&intervalTime=1h&limit=200")
        ts_field, list_getter = "timestamp", None
    cursor_end = end_ms
    all_ts: list[int] = []
    for _ in range(max_requests):
        url = f"{base}&startTime={start_ms}&endTime={cursor_end}"
        try:
            payload = json.loads(tr(url, {"User-Agent": "researchops/1.0"}))
        except Exception as e:
            if "429" in str(e):
                rep["rate_limited"] = True
                rep["coverage_verdict"] = "RATE_LIMITED"
                return rep
            rep["coverage_verdict"] = "UNKNOWN"
            rep["error"] = f"{type(e).__name__}:{str(e)[:60]}"
            return rep
        rep["requests_used"] += 1
        lst = ((payload or {}).get("result") or {}).get("list") or []
        if not lst:
            break
        ts_batch = []
        for r in lst:
            try:
                ts_batch.append(int(float(r.get(ts_field))))
            except (TypeError, ValueError):
                continue
        if not ts_batch:
            break
        all_ts.extend(ts_batch)
        oldest = min(ts_batch)
        if oldest <= start_ms or len(lst) < 200:
            break
        cursor_end = oldest - 1
    if not all_ts:
        rep["coverage_verdict"] = "NO_DATA"
        return rep
    rep["rows"] = len(all_ts)
    rep["first_available"] = min(all_ts)
    rep["last_available"] = max(all_ts)
    span_covered = rep["last_available"] - rep["first_available"]
    span_requested = end_ms - start_ms
    # honest verdict: OK only if the OLD edge of the request is reached
    if rep["first_available"] <= start_ms + 86_400_000:
        rep["coverage_verdict"] = "COVERAGE_OK"
    elif span_covered > 0.2 * span_requested:
        rep["coverage_verdict"] = "PARTIAL_COVERAGE"
    else:
        rep["coverage_verdict"] = "PARTIAL_COVERAGE" if rep["rows"] else "NO_DATA"
    return rep
