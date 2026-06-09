#!/usr/bin/env python3
"""ResearchOps V10.2.1 — Coinalyze chunked safe fetcher + atomic staging.

Downloads long Coinalyze history (BTC/ETH, 180/365 days) in 30-day chunks
into an ISOLATED staging dir, and only publishes into ``external_data/raw``
when EVERY chunk succeeded. A mid-download API failure therefore can NEVER
corrupt the existing data.

HARD CONTRACT — research only / security:

- API key read ONLY from ``COINALYZE_API_KEY``; if absent -> NEED_KEY, exit 0,
- the key is NEVER printed and NEVER written to a file (header auth only),
- HTTP errors are SANITIZED: logical endpoint + status + reason + truncated,
  scrubbed body — never the key, never full auth headers,
- retry/backoff on 429/500/502/503/504; fast abort on 400/401/403,
- NEVER touches ``external_data/raw`` until all chunks are OK,
- default ``--publish-mode staging-only`` does NOT publish and does NOT
  archive/delete old data,
- no orders, no private exchange calls, no DB writes, no ``.env``, no runtime.

NO LIVE.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Reuse the audited pure builders from the v101 fetcher.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_coinalyze_v101 as v101  # noqa: E402

API_BASE = v101.API_BASE
RAW_MARKET_DIR = v101.RAW_MARKET_DIR
RAW_LIQ_DIR = v101.RAW_LIQ_DIR
ARCHIVE_BASE = "external_data/archive"
STAGING_BASE = "external_data/staging"
REPORTS_DIR = "external_data/reports"

RETRYABLE = frozenset({429, 500, 502, 503, 504})
FAST_ABORT = frozenset({400, 401, 403})

# Report statuses.
ST_OK = "OK"
ST_FAILED = "FAILED"
ST_PARTIAL_STAGING = "PARTIAL_STAGING_ONLY"
ST_NEED_KEY = "NEED_KEY"
ST_UNDERCOVERAGE = "UNDERCOVERAGE"

# Coverage gate: a download requested for N days must cover >= 80% (by days
# AND by rows) or it is UNDERCOVERAGE and publishing is blocked.
COVERAGE_MIN = 0.80
HOURS_PER_DAY = 24
# Overlap thresholds (on the OHLCV spine).
OVERLAP_DETECT = 0.02   # any non-trivial cross-chunk duplication
OVERLAP_CAP = 0.50      # heavy duplication => API likely caps/ignores from/to
MS_PER_DAY = 86_400_000.0

MARKET_HISTORY_ENDPOINTS = [
    ("/ohlcv-history", "ohlcv", None),
    ("/open-interest-history", "oi", {"convert_to_usd": "true"}),
    ("/funding-rate-history", "funding", None),
    ("/long-short-ratio-history", "lsr", None),   # optional
]
LIQ_ENDPOINT = ("/liquidation-history", "liq", {"convert_to_usd": "true"})
OPTIONAL_ENDPOINTS = frozenset({"lsr"})


@dataclass
class FetchError:
    status_code: int = 0
    reason: str = ""
    body: str = ""
    kind: str = ""
    attempts: int = 0
    endpoint: str = ""
    symbols: str = ""
    chunk_start: int = 0
    chunk_end: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sanitize(text: str, key: str | None) -> str:
    if not text:
        return ""
    out = str(text)
    if key:
        out = out.replace(key, "***REDACTED***")
    return out[:500]


def _http_get_json(
    session, path: str, params: dict, key: str, *,
    max_retries: int, retry_sleep: float, sleep_fn: Callable[[float], None] = time.sleep,
):
    """GET with retry/backoff. Returns (json, FetchError|None). Never raises,
    never leaks the key."""
    last_err: FetchError | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = session.get(API_BASE + path, params=params,
                               headers=v101._headers(key), timeout=30)
        except Exception as exc:  # network-level error (no secrets in type name)
            last_err = FetchError(status_code=0, reason=type(exc).__name__,
                                  body="", kind="network_error", attempts=attempt + 1,
                                  endpoint=path)
            sleep_fn(retry_sleep * (2 ** attempt))
            continue
        sc = int(getattr(resp, "status_code", 0))
        if sc == 200:
            try:
                return resp.json(), None
            except Exception:
                last_err = FetchError(status_code=sc, reason="bad_json", body="",
                                      kind="bad_json", attempts=attempt + 1, endpoint=path)
                sleep_fn(retry_sleep * (2 ** attempt))
                continue
        reason = str(getattr(resp, "reason", "") or "")
        body = _sanitize(getattr(resp, "text", "") or "", key)
        if sc in FAST_ABORT:
            return None, FetchError(status_code=sc, reason=reason, body=body,
                                    kind="auth_or_bad_request_abort", attempts=attempt + 1,
                                    endpoint=path)
        # retryable or unexpected => retry up to max
        last_err = FetchError(status_code=sc, reason=reason, body=body,
                              kind=("retryable" if sc in RETRYABLE else "unexpected_status"),
                              attempts=attempt + 1, endpoint=path)
        sleep_fn(retry_sleep * (2 ** attempt))
    return None, last_err


def chunk_ranges(frm_s: int, to_s: int, chunk_days: int) -> list[tuple[int, int]]:
    """Split [frm_s, to_s) into chunks of ``chunk_days`` days (last partial)."""
    step = max(1, int(chunk_days)) * 86400
    out: list[tuple[int, int]] = []
    a = frm_s
    while a < to_s:
        b = min(a + step, to_s)
        out.append((a, b))
        a = b
    return out


def n_chunks(days: int, chunk_days: int) -> int:
    to_s = days * 86400
    return len(chunk_ranges(0, to_s, chunk_days))


def _merge_history(acc: dict[str, dict[int, dict]], data: list[dict]) -> int:
    """Merge a history response into accumulator. Returns duplicate count."""
    dups = 0
    for entry in data or []:
        sym = entry.get("symbol")
        if not sym:
            continue
        bucket = acc.setdefault(sym, {})
        for p in entry.get("history") or []:
            t = p.get("t")
            if t is None:
                continue
            t = int(t)
            if t in bucket:
                dups += 1
            bucket[t] = p
    return dups


def _acc_to_list(acc: dict[str, dict[int, dict]]) -> list[dict]:
    return [{"symbol": s, "history": [pts[t] for t in sorted(pts)]} for s, pts in acc.items()]


def _iso_s(unix_s: int | None) -> str | None:
    if unix_s is None:
        return None
    return datetime.fromtimestamp(int(unix_s), tz=timezone.utc).isoformat()


def _iso_ms(unix_ms: int | None) -> str | None:
    if unix_ms is None:
        return None
    return datetime.fromtimestamp(int(unix_ms) / 1000, tz=timezone.utc).isoformat()


# endpoint accumulator key -> marker field name
_EP_NAME = {"ohlcv": "ohlcv", "oi": "open_interest", "funding": "funding",
            "liq": "liquidations", "lsr": "long_short_ratio"}


def _build_chunk_marker(idx, a, b, symbols, chunk_data, norm) -> dict[str, Any]:
    """Auditable per-chunk summary: lets us see empty chunks / API range caps."""
    endpoint_rows: dict[str, int] = {}
    empty: list[str] = []
    for akey, mname in _EP_NAME.items():
        rows = sum(len(e.get("history") or []) for e in (chunk_data.get(akey) or []))
        endpoint_rows[mname] = rows
        if rows == 0:
            empty.append(mname)
    ms_rows = v101.build_market_rows(
        ohlcv=chunk_data.get("ohlcv", []), oi=chunk_data.get("oi", []),
        funding=chunk_data.get("funding", []), lsr=chunk_data.get("lsr", []),
        coinalyze_to_norm=norm)
    pl = v101.price_lookup_from_market_rows(ms_rows)
    liq_rows, _ = v101.build_liquidation_rows(
        liquidations=chunk_data.get("liq", []), coinalyze_to_norm=norm, price_by_symbol_ts=pl)
    ts = [int(r["timestamp"]) for r in ms_rows] if ms_rows else []
    mn = min(ts) if ts else None
    mx = max(ts) if ts else None
    return {
        "chunk_index": idx,
        "chunk_start": a, "chunk_end": b,
        "chunk_start_iso": _iso_s(a), "chunk_end_iso": _iso_s(b),
        "symbols": list(symbols),
        "endpoint_rows": endpoint_rows,
        "market_state_rows_built": len(ms_rows),
        "liquidation_rows_built": len(liq_rows),
        "min_timestamp": mn, "max_timestamp": mx,
        "min_timestamp_iso": _iso_ms(mn), "max_timestamp_iso": _iso_ms(mx),
        "empty_endpoints": empty,
        "chunk_status": "OK" if len(ms_rows) > 0 else "EMPTY",
    }


@dataclass
class ChunkedReport:
    report_status: str = ST_NEED_KEY
    symbols: list[str] = field(default_factory=list)
    days: int = 0
    interval: str = "1hour"
    chunk_days: int = 30
    chunks_total: int = 0
    chunks_ok: int = 0
    chunks_failed: int = 0
    rows_market_state: int = 0
    rows_liquidations: int = 0
    min_timestamp: str = ""
    max_timestamp: str = ""
    duplicates_removed: int = 0
    # Coverage (V10.2.2 undercoverage detector).
    requested_days: int = 0
    actual_days_covered: float = 0.0
    expected_market_rows: int = 0
    actual_market_rows: int = 0
    coverage_ratio_by_days: float = 0.0
    coverage_ratio_by_rows: float = 0.0
    undercoverage: bool = False
    publish_allowed: bool = False
    publish_blocker: str = ""
    do_not_replace_raw: bool = False
    # Overlap / range-cap diagnostics.
    chunk_overlap_detected: bool = False
    overlap_ratio: float = 0.0
    unique_timestamps: int = 0
    raw_chunk_rows_before_dedup: int = 0
    possible_api_range_cap_or_ignored_from_to: bool = False
    staging_dir: str = ""
    publish_mode: str = "staging-only"
    published_files: list[str] = field(default_factory=list)
    old_data_touched: bool = False
    failure: dict[str, Any] = field(default_factory=dict)
    api_key_printed: bool = False
    db_writes: int = 0
    research_only: bool = True
    final_recommendation: str = "NO LIVE"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_chunked_fetch(
    symbols_map: dict[str, str],
    *,
    key: str,
    session,
    days: int = 180,
    interval: str = "1hour",
    chunk_days: int = 30,
    staging_dir: str,
    publish_mode: str = "staging-only",
    max_retries: int = 4,
    retry_sleep: float = 2.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    raw_market_dir: str = RAW_MARKET_DIR,
    raw_liq_dir: str = RAW_LIQ_DIR,
    archive_base: str = ARCHIVE_BASE,
    now_s: int | None = None,
) -> ChunkedReport:
    """Core chunked fetch + atomic staging/publish. ``session`` is injectable
    (tests pass a fake). Returns a ChunkedReport; raw data is only touched on
    a fully successful 'replace'/'append' publish."""
    rep = ChunkedReport(days=int(days), interval=interval, chunk_days=int(chunk_days),
                        publish_mode=publish_mode, staging_dir=staging_dir)
    norm = {v: k for k, v in symbols_map.items()}
    rep.symbols = sorted(symbols_map.keys())
    csymbols = list(symbols_map.values())
    sym_param = ",".join(csymbols)

    to_s = int(now_s if now_s is not None else datetime.now(timezone.utc).timestamp())
    frm_s = to_s - int(days) * 86400
    ranges = chunk_ranges(frm_s, to_s, chunk_days)
    rep.chunks_total = len(ranges)

    Path(staging_dir).mkdir(parents=True, exist_ok=True)

    ohlcv: dict[str, dict[int, dict]] = {}
    oi: dict[str, dict[int, dict]] = {}
    funding: dict[str, dict[int, dict]] = {}
    lsr: dict[str, dict[int, dict]] = {}
    liq: dict[str, dict[int, dict]] = {}
    acc_by_key = {"ohlcv": ohlcv, "oi": oi, "funding": funding, "lsr": lsr, "liq": liq}
    dups = 0
    raw_ohlcv_inserts = 0  # total OHLCV points fetched across chunks (pre-dedup)

    for idx, (a, b) in enumerate(ranges):
        marker = Path(staging_dir) / f"chunk_{idx:03d}.done.json"
        if marker.exists():  # --resume support
            try:
                cached = json.loads(marker.read_text(encoding="utf-8"))
                data_by_ep = cached.get("_endpoint_data") or {}
                for akey, data in data_by_ep.items():
                    if akey == "ohlcv":
                        raw_ohlcv_inserts += sum(len(e.get("history") or []) for e in data)
                    if akey in acc_by_key:
                        dups += _merge_history(acc_by_key[akey], data)
                rep.chunks_ok += 1
                continue
            except (OSError, ValueError, KeyError, TypeError):
                pass  # fall through and refetch this chunk
        chunk_data: dict[str, list] = {}
        for path, akey, extra in MARKET_HISTORY_ENDPOINTS + [LIQ_ENDPOINT]:
            params = {"symbols": sym_param, "interval": interval, "from": a, "to": b}
            if extra:
                params.update(extra)
            data, err = _http_get_json(session, path, params, key,
                                       max_retries=max_retries, retry_sleep=retry_sleep,
                                       sleep_fn=sleep_fn)
            if err is not None:
                if akey in OPTIONAL_ENDPOINTS:
                    chunk_data[akey] = []
                    continue
                # fatal: abort WITHOUT publishing; staging stays intact.
                err.symbols = sym_param
                err.chunk_start, err.chunk_end = a, b
                rep.chunks_failed += 1
                rep.failure = err.as_dict()
                rep.report_status = ST_FAILED
                rep.old_data_touched = False
                rep.publish_allowed = False
                rep.do_not_replace_raw = True
                rep.publish_blocker = "chunk_fetch_failed"
                _write_report(rep)
                return rep
            chunk_data[akey] = data
            if akey == "ohlcv":
                raw_ohlcv_inserts += sum(len(e.get("history") or []) for e in data)
            dups += _merge_history(acc_by_key[akey], data)
        marker_obj = _build_chunk_marker(idx, a, b, rep.symbols, chunk_data, norm)
        marker_obj["_endpoint_data"] = chunk_data  # kept for --resume
        marker.write_text(json.dumps(marker_obj, default=str), encoding="utf-8")
        rep.chunks_ok += 1
        sleep_fn(min(retry_sleep, 1.6))  # gentle pacing between chunks

    rep.duplicates_removed = dups

    # All chunks OK -> build final merged rows (dedup is implicit via dict keys).
    market_rows = v101.build_market_rows(
        ohlcv=_acc_to_list(ohlcv), oi=_acc_to_list(oi),
        funding=_acc_to_list(funding), lsr=_acc_to_list(lsr), coinalyze_to_norm=norm)
    price_lookup = v101.price_lookup_from_market_rows(market_rows)
    liq_rows, _skipped = v101.build_liquidation_rows(
        liquidations=_acc_to_list(liq), coinalyze_to_norm=norm, price_by_symbol_ts=price_lookup)
    rep.rows_market_state = len(market_rows)
    rep.rows_liquidations = len(liq_rows)
    ts_ms: list[int] = [int(r["timestamp"]) for r in market_rows] if market_rows else []
    if ts_ms:
        rep.min_timestamp = _iso_ms(min(ts_ms))
        rep.max_timestamp = _iso_ms(max(ts_ms))

    # ---- coverage (undercoverage detector) ----
    n_syms = max(1, len(symbols_map))
    rep.requested_days = int(days)
    rep.expected_market_rows = int(days) * HOURS_PER_DAY * n_syms
    rep.actual_market_rows = len(market_rows)
    if ts_ms:
        rep.actual_days_covered = round((max(ts_ms) - min(ts_ms)) / MS_PER_DAY, 2)
    rep.coverage_ratio_by_rows = round(rep.actual_market_rows / rep.expected_market_rows, 4) if rep.expected_market_rows else 0.0
    rep.coverage_ratio_by_days = round(rep.actual_days_covered / rep.requested_days, 4) if rep.requested_days else 0.0

    # ---- overlap / range-cap diagnostics (OHLCV spine) ----
    unique_ohlcv = sum(len(b) for b in ohlcv.values())
    rep.unique_timestamps = unique_ohlcv
    rep.raw_chunk_rows_before_dedup = raw_ohlcv_inserts
    ohlcv_dups = max(0, raw_ohlcv_inserts - unique_ohlcv)
    rep.overlap_ratio = round(ohlcv_dups / raw_ohlcv_inserts, 4) if raw_ohlcv_inserts else 0.0
    rep.chunk_overlap_detected = rep.overlap_ratio > OVERLAP_DETECT
    rep.possible_api_range_cap_or_ignored_from_to = bool(
        rep.overlap_ratio >= OVERLAP_CAP
        or (rep.chunk_overlap_detected and rep.coverage_ratio_by_days < 0.5))

    # Write final staging files (always, so the operator can inspect even on
    # undercoverage). This NEVER touches external_data/raw.
    final_dir = Path(staging_dir) / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    fm = final_dir / "perp_market_state.csv"
    fl = final_dir / "perp_liquidations.csv"
    v101._write_csv(market_rows, v101.MARKET_FIELDS, fm)
    v101._write_csv(liq_rows, v101.LIQ_FIELDS, fl)

    # ---- undercoverage gate: blocks ALL publishing (incl. replace) ----
    rep.undercoverage = bool(
        rep.coverage_ratio_by_days < COVERAGE_MIN or rep.coverage_ratio_by_rows < COVERAGE_MIN)
    if rep.undercoverage:
        rep.report_status = ST_UNDERCOVERAGE
        rep.publish_allowed = False
        rep.publish_blocker = "insufficient_history_coverage"
        rep.do_not_replace_raw = True
        rep.old_data_touched = False
        rep.published_files = []
        _write_report(rep)
        return rep

    rep.publish_allowed = True

    # Publish (coverage is sufficient and all chunks OK).
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if publish_mode == "staging-only":
        rep.report_status = ST_PARTIAL_STAGING
        rep.old_data_touched = False
    elif publish_mode in ("replace", "append"):
        if publish_mode == "replace":
            rep.old_data_touched = True
            for dataset, raw_dir in (("perp_market_state", raw_market_dir),
                                     ("perp_liquidations", raw_liq_dir)):
                arch = Path(archive_base) / f"pre_v10_2_2_{stamp}" / dataset
                src = Path(raw_dir)
                if src.exists():
                    arch.mkdir(parents=True, exist_ok=True)
                    for f in src.iterdir():
                        if f.is_file() and f.suffix.lower() in (".csv", ".ndjson", ".json", ".tsv", ".jsonl"):
                            shutil.move(str(f), str(arch / f.name))
        else:
            rep.old_data_touched = False
        for raw_dir, final_csv in ((raw_market_dir, fm), (raw_liq_dir, fl)):
            Path(raw_dir).mkdir(parents=True, exist_ok=True)
            dest = Path(raw_dir) / f"coinalyze_bitget_btc_eth_1h_{stamp}.csv"
            shutil.copy2(str(final_csv), str(dest))
            rep.published_files.append(str(dest))
        rep.report_status = ST_OK
    else:
        rep.report_status = ST_FAILED
        rep.publish_allowed = False
        rep.failure = {"kind": "unknown_publish_mode", "publish_mode": publish_mode}

    _write_report(rep)
    return rep


def _write_report(rep: ChunkedReport) -> str:
    try:
        rdir = Path(REPORTS_DIR)
        rdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        p = rdir / f"coinalyze_chunked_fetch_{stamp}.json"
        p.write_text(json.dumps(rep.as_dict(), indent=2, default=str), encoding="utf-8")
        cp = rdir / f"coinalyze_chunked_fetch_{stamp}.csv"
        with cp.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["report_status", "symbols", "days", "chunk_days", "chunks_total",
                        "chunks_ok", "chunks_failed", "rows_market_state", "rows_liquidations",
                        "duplicates_removed", "requested_days", "actual_days_covered",
                        "expected_market_rows", "actual_market_rows", "coverage_ratio_by_days",
                        "coverage_ratio_by_rows", "undercoverage", "publish_allowed",
                        "publish_blocker", "do_not_replace_raw", "chunk_overlap_detected",
                        "overlap_ratio", "possible_api_range_cap_or_ignored_from_to",
                        "publish_mode", "old_data_touched", "final_recommendation"])
            w.writerow([rep.report_status, ";".join(rep.symbols), rep.days, rep.chunk_days,
                        rep.chunks_total, rep.chunks_ok, rep.chunks_failed,
                        rep.rows_market_state, rep.rows_liquidations, rep.duplicates_removed,
                        rep.requested_days, rep.actual_days_covered, rep.expected_market_rows,
                        rep.actual_market_rows, rep.coverage_ratio_by_days,
                        rep.coverage_ratio_by_rows, str(rep.undercoverage).lower(),
                        str(rep.publish_allowed).lower(), rep.publish_blocker or "NONE",
                        str(rep.do_not_replace_raw).lower(), str(rep.chunk_overlap_detected).lower(),
                        rep.overlap_ratio, str(rep.possible_api_range_cap_or_ignored_from_to).lower(),
                        rep.publish_mode, str(rep.old_data_touched).lower(),
                        rep.final_recommendation])
        return str(p)
    except OSError:
        return "WRITE_FAILED"


def _print_report(rep: ChunkedReport) -> None:
    print("COINALYZE CHUNKED FETCH V10.2.1 START")
    print(f"report_status: {rep.report_status}")
    print("symbols: " + (",".join(rep.symbols) if rep.symbols else "NONE"))
    print(f"days: {rep.days} interval: {rep.interval} chunk_days: {rep.chunk_days}")
    print(f"chunks_total: {rep.chunks_total} chunks_ok: {rep.chunks_ok} chunks_failed: {rep.chunks_failed}")
    print(f"rows_market_state: {rep.rows_market_state} rows_liquidations: {rep.rows_liquidations}")
    print(f"duplicates_removed: {rep.duplicates_removed}")
    print(f"min_timestamp: {rep.min_timestamp or 'NONE'} max_timestamp: {rep.max_timestamp or 'NONE'}")
    print(f"requested_days: {rep.requested_days}")
    print(f"actual_days_covered: {rep.actual_days_covered}")
    print(f"expected_market_rows: {rep.expected_market_rows}")
    print(f"actual_market_rows: {rep.actual_market_rows}")
    print(f"coverage_ratio_by_days: {rep.coverage_ratio_by_days}")
    print(f"coverage_ratio_by_rows: {rep.coverage_ratio_by_rows}")
    print(f"undercoverage: {str(rep.undercoverage).lower()}")
    print(f"publish_allowed: {str(rep.publish_allowed).lower()}")
    print(f"publish_blocker: {rep.publish_blocker or 'NONE'}")
    print(f"do_not_replace_raw: {str(rep.do_not_replace_raw).lower()}")
    print(f"chunk_overlap_detected: {str(rep.chunk_overlap_detected).lower()} overlap_ratio: {rep.overlap_ratio}")
    print(f"unique_timestamps: {rep.unique_timestamps} raw_chunk_rows_before_dedup: {rep.raw_chunk_rows_before_dedup}")
    print(f"possible_api_range_cap_or_ignored_from_to: {str(rep.possible_api_range_cap_or_ignored_from_to).lower()}")
    print(f"staging_dir: {rep.staging_dir}")
    print(f"publish_mode: {rep.publish_mode}")
    print("published_files: " + (";".join(rep.published_files) if rep.published_files else "NONE"))
    print(f"old_data_touched: {str(rep.old_data_touched).lower()}")
    if rep.failure:
        f = rep.failure
        print(f"failure: endpoint={f.get('endpoint')} status={f.get('status_code')} "
              f"reason={f.get('reason')} kind={f.get('kind')} attempts={f.get('attempts')} "
              f"chunk=[{f.get('chunk_start')},{f.get('chunk_end')}] body={f.get('body')}")
    print(f"api_key_printed: {str(rep.api_key_printed).lower()}")
    print("db_writes: 0")
    print("research_only: true")
    print(f"final_recommendation: {rep.final_recommendation}")
    print("COINALYZE CHUNKED FETCH V10.2.1 END")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Coinalyze V10.2.1 chunked safe fetcher (research-only)")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--interval", default="1hour")
    ap.add_argument("--chunk-days", type=int, default=30)
    ap.add_argument("--coinalyze-symbols", default="BTCUSDT=BTCUSDT_PERP.A,ETHUSDT=ETHUSDT_PERP.A")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--max-retries", type=int, default=4)
    ap.add_argument("--retry-sleep", type=float, default=2.0)
    ap.add_argument("--staging-dir", default="")
    ap.add_argument("--publish-mode", default="staging-only",
                    choices=["staging-only", "replace", "append"])
    args = ap.parse_args(argv)

    key = os.environ.get("COINALYZE_API_KEY")
    if not key:
        rep = ChunkedReport(report_status=ST_NEED_KEY, days=int(args.days),
                            publish_mode=args.publish_mode)
        print("ABORT: COINALYZE_API_KEY is not set in the environment. (key never printed)")
        _print_report(rep)
        return 0

    try:
        import requests
        session = requests.Session()
    except Exception as exc:  # pragma: no cover
        print(f"ABORT: HTTP client unavailable ({type(exc).__name__}). NO LIVE.")
        return 0

    wanted = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
    sym_map = v101.parse_symbol_override(args.coinalyze_symbols)
    sym_map = {k: v for k, v in sym_map.items() if k in wanted}
    if not sym_map:
        try:
            sym_map = v101.discover_bitget_symbols(session, key, wanted)
        except Exception:
            sym_map = {}
    missing = sorted(wanted - set(sym_map.keys()))
    if missing:
        print(f"ABORT: missing required symbols {missing}. No files written. NO LIVE.")
        return 0

    staging = args.staging_dir or (
        f"{STAGING_BASE}/coinalyze_long_history_"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    rep = run_chunked_fetch(
        sym_map, key=key, session=session, days=args.days, interval=args.interval,
        chunk_days=args.chunk_days, staging_dir=staging, publish_mode=args.publish_mode,
        max_retries=args.max_retries, retry_sleep=args.retry_sleep)
    _print_report(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
