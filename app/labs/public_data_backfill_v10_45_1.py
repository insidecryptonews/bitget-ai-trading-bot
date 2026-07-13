"""ResearchOps V10.45.5 - Public historical kline backfill (research only).

Downloads PUBLIC 1m klines from Bitget (target venue) and Bybit (cross-venue
reference) — no keys, no private endpoints, market-data endpoints only — and
publishes every dataset as a content-addressed GENERATION (CSV + manifest +
atomic CURRENT marker). Verification is derived from the CSV itself: the
manifest is only the expected contract, never the source of truth.

The point-in-time contract for downstream research: a bar with open time T is
COMPLETE and available at T + timeframe. Features may only use bars whose
close time <= decision time; entries execute at the NEXT bar open.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import stat as _stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import continuous_edge_factory_v10_38 as CE
from .ai_providers_v10_45_1 import _http_json, sanitize_error

TOOL_VERSION = "v10.45.6"
DOWNLOADER_VERSION = ("v10.45.5 (pagination end=batch_min live-probed; "
                      "content-addressed CSV+manifest generations with atomic "
                      "CURRENT marker; CSV-derived verification; strict "
                      "order-preserving loader; pre-mkdir path validation; "
                      "exclusive temp files with mandatory fsync)")
DATA_SUBDIR = ("external_data", "staging", "klines_v10_45_5")
COMPLETENESS_TOLERANCE_BARS = 3   # last candle may still be open + boundary jitter
BITGET_BASE = "https://api.bitget.com"
BYBIT_BASE = "https://api.bybit.com"
BAR_MS = 60_000
REQUEST_SLEEP_S = 0.15        # well under both venues' public IP limits
SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,20}$")
ALLOWED_VENUES = ("bitget", "bybit")
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} \
    | {f"LPT{i}" for i in range(1, 10)}
CSV_HEADER = ["ts", "open", "high", "low", "close", "volume", "turnover"]
CURRENT_MARKER = "CURRENT.json"
GEN_COMPLETE_MARKER = "_COMPLETE.json"   # written LAST inside a generation dir
SCHEMA_VERSION = "klines_schema_v1"
# fields of the manifest that form the VERIFIABLE CONTRACT (volatile fields
# like downloaded_at / repo_commit are provenance, not contract)
CONTRACT_FIELDS = ("venue", "symbol", "timeframe", "schema_version", "source",
                   "requested_start_ms", "requested_end_ms", "requested_days",
                   "expected_bars", "actual_bars", "n_bars", "coverage_ratio",
                   "gap_count", "duplicates", "out_of_order",
                   "irregular_deltas", "invalid_candles", "raw_quality_pass",
                   "download_complete", "actual_start_ms", "actual_end_ms",
                   "completeness_tolerance_bars", "sha256")


def manifest_contract_sha(manifest: dict) -> str:
    contract = {k: manifest.get(k) for k in CONTRACT_FIELDS}
    return hashlib.sha256(json.dumps(contract, sort_keys=True,
                                     separators=(",", ":"),
                                     default=str).encode("utf-8")).hexdigest()


def compute_generation_id(csv_sha: str, contract_sha: str, source: str,
                          symbol: str, timeframe: str) -> str:
    return hashlib.sha256(
        f"{csv_sha}|{contract_sha}|{SCHEMA_VERSION}|{source}|{symbol}|"
        f"{timeframe}".encode()).hexdigest()[:16]


def _fsync_dir(path) -> None:
    """Directory fsync (durability of the directory entry). On Windows,
    opening a directory handle via os.open is not supported by CPython, so
    this is BEST-EFFORT there and documented as such; on POSIX it is real."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def validate_symbol(symbol: str) -> str:
    """Strict whitelist: uppercase alnum, 3-20 chars, no path metacharacters,
    no reserved device names. Raises ValueError on anything else."""
    s = str(symbol or "")
    if not SYMBOL_RE.fullmatch(s) or s.upper() in WINDOWS_RESERVED:
        raise ValueError(f"invalid symbol: {s[:40]!r}")
    return s


def _safety() -> dict[str, Any]:
    return {"research_only": True, "public_endpoints_only": True,
            "uses_api_keys": False, "can_send_real_orders": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def _now_ms() -> int:
    return int(time.time() * 1000)


# ==========================================================================
# PATH SAFETY: validate root and every parent BEFORE any mkdir
# ==========================================================================

_REPARSE_ATTR = 0x400          # FILE_ATTRIBUTE_REPARSE_POINT (junctions too)


def _is_link_like(p) -> bool:
    """True for symlinks AND Windows junctions/reparse points (lstat-based,
    never follows the link)."""
    try:
        st_ = os.lstat(p)
    except OSError:
        return False
    if getattr(st_, "st_file_attributes", 0) & _REPARSE_ATTR:
        return True
    return _stat.S_ISLNK(st_.st_mode)


def validated_dir(*parts: str) -> Path:
    """Return repo_root/<parts...> creating missing components ONE at a time,
    validating each existing component BEFORE mkdir: no symlink, junction or
    reparse point anywhere in the chain, no traversal metacharacters, and the
    final real path must stay inside the repository root. mkdir never follows
    a link because the parent was validated first."""
    logical_root = Path(str(CE._repo_root()))
    # the LOGICAL root itself must not be a link/junction: resolving it first
    # and validating the resolved path would silently follow the link
    if _is_link_like(logical_root):
        raise ValueError("repository root is a symlink/junction/reparse point")
    root = Path(os.path.realpath(str(logical_root)))
    if not root.is_dir():
        raise ValueError("repository root is not a directory")
    cur = root
    for part in parts:
        p = str(part)
        if not p or p in (".", "..") or any(c in p for c in "/\\:"):
            raise ValueError(f"invalid path component: {p[:40]!r}")
        cur = cur / p
        if os.path.lexists(cur):
            if _is_link_like(cur):
                raise ValueError(f"link/junction/reparse point in path: {p!r}")
            if not cur.is_dir():
                raise ValueError(f"path component is not a directory: {p!r}")
        else:
            os.mkdir(cur)                     # parent already validated
        real = Path(os.path.realpath(str(cur)))
        if root != real and root not in real.parents:
            raise ValueError("directory escapes repository root")
    return cur


def _dataset_dir(venue: str, symbol: str) -> Path:
    if venue not in ALLOWED_VENUES:
        raise ValueError(f"invalid venue: {str(venue)[:20]!r}")
    sym = validate_symbol(symbol)
    return validated_dir(*DATA_SUBDIR, f"{venue}_{sym}_1m")


# ==========================================================================
# EXCLUSIVE TEMP FILES + ATOMIC, VERIFIED WRITES
# ==========================================================================

def _between_write_and_replace(path) -> None:
    """Deterministic test seam for the TOCTOU window between the last
    hardlink check and os.replace. No-op in production."""
    return None


def safe_atomic_write(path, data: bytes) -> str:
    """Atomic write with an EXCLUSIVE random-named temp file:

    * destination parent validated (containment, no link/reparse);
    * destination itself refused if it is a link or hardlinked (st_nlink>1);
    * temp created by tempfile.mkstemp (O_CREAT|O_EXCL) inside the validated
      directory, handle kept from creation through write;
    * write -> flush -> fsync (an fsync failure RAISES, never fake success);
    * temp content verified by SHA before the atomic replace;
    * destination verified by SHA after the replace;
    * the temp file is removed on ANY exception."""
    path = Path(path)
    parent = path.parent
    logical_root = Path(str(CE._repo_root()))
    if _is_link_like(logical_root):
        raise ValueError("repository root is a symlink/junction/reparse point")
    base = Path(os.path.realpath(str(parent)))
    repo_real = Path(os.path.realpath(str(logical_root)))
    if repo_real != base and repo_real not in base.parents:
        raise ValueError("write target escapes repository root")
    if _is_link_like(parent):
        raise ValueError("destination directory is a link/reparse point")
    if os.path.lexists(path):
        if _is_link_like(path):
            raise ValueError("destination is a symlink/reparse point")
        st_ = os.stat(path)
        if getattr(st_, "st_nlink", 1) > 1:
            raise ValueError("refusing to overwrite a hardlinked file")
    expected = hashlib.sha256(data).hexdigest()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part",
                                    dir=str(base))
    tmp = Path(tmp_name)
    fd_open = True
    try:
        st_t = os.fstat(fd)
        if getattr(st_t, "st_nlink", 1) > 1:
            raise ValueError("temp file is hardlinked")
        os.write(fd, data)
        os.fsync(fd)                          # failure RAISES: no fake success
        st_fd = os.fstat(fd)
        if getattr(st_fd, "st_nlink", 1) > 1:
            raise ValueError("temp file was hardlinked during write")
        os.close(fd)
        fd_open = False
        if hashlib.sha256(tmp.read_bytes()).hexdigest() != expected:
            raise IOError("temp content verification failed")
        _between_write_and_replace(path)      # test seam (no-op in prod)
        # ---- TOCTOU recheck IMMEDIATELY before the replace ----------------
        # (a) revalidate the TEMP itself: it was verified earlier, but the
        # seam is exactly where an attacker could hardlink it. Its link
        # count, size, SHA and existence must be unchanged or we abort.
        if _is_link_like(tmp):
            raise ValueError("temp became a link before replace")
        st_tmp = os.stat(tmp)                 # raises FileNotFoundError if gone
        if getattr(st_tmp, "st_nlink", 1) > 1:
            raise ValueError("temp was hardlinked before replace")
        if st_tmp.st_size != len(data):
            raise IOError("temp size changed before replace")
        if hashlib.sha256(tmp.read_bytes()).hexdigest() != expected:
            raise IOError("temp content changed before replace")
        # (b) an alias or link created on the DESTINATION after the first
        # check must never receive the swap
        if os.path.lexists(path):
            if _is_link_like(path):
                raise ValueError("destination became a link before replace")
            st_now = os.stat(path)
            if getattr(st_now, "st_nlink", 1) > 1:
                raise ValueError("destination was hardlinked before replace")
        os.replace(tmp, path)
        _fsync_dir(base)                      # durability of the dir entry
    except BaseException:
        if fd_open:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if tmp.exists():
                os.unlink(tmp)
        except OSError:
            pass
        raise
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if sha != expected:
        raise IOError("post-write SHA verification failed")
    return sha


# ==========================================================================
# DOWNLOAD (unchanged pagination semantics, live-probed in V10.45.2)
# ==========================================================================

def paginate_klines(fetch_page, end_ms: int, target_start_ms: int,
                    max_requests: int, log=print, label: str = "") -> list[list]:
    """Generic backwards pagination with NO candle lost at page boundaries.

    fetch_page(end_ms) -> list of raw rows (any order). The next page continues
    from `end = batch_min` (the smallest open-ts of the batch). Probed LIVE on
    Bitget: /history-candles returns candles whose CLOSE <= endTime, so with
    endTime = batch_min the candle opening at batch_min - BAR_MS (closing at
    batch_min) is included and batch_min itself is not re-sent. INGEST is the
    only place allowed to sort/dedupe (exchange pages arrive newest-first);
    everything downstream of the published CSV preserves order and fails on
    any violation instead."""
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


# ==========================================================================
# RAW QUALITY (shared by ingest and CSV-derived verification)
# ==========================================================================

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
    if ts <= 0 or not all(math.isfinite(x) for x in (o, h, l, c, v, t)):
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


# ==========================================================================
# GENERATION PUBLISH: CSV + manifest as ONE logical transaction
# ==========================================================================

def _repo_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(CE._repo_root()))
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _gen_complete(gdir: Path) -> dict | None:
    """Return the verified COMPLETE receipt of a generation dir, or None when
    the generation is INCOMPLETE (no marker, marker corrupt, or the marker's
    hashes do not match the files on disk). A generation is only immutable and
    reusable once it carries a valid COMPLETE marker."""
    mk = gdir / GEN_COMPLETE_MARKER
    csv_p, man_p = gdir / "data.csv", gdir / "manifest.json"
    if not mk.is_file() or not csv_p.is_file() or not man_p.is_file():
        return None
    try:
        rec = json.loads(mk.read_text(encoding="utf-8"))
    except Exception:
        return None
    if rec.get("state") != "COMPLETE":
        return None
    if hashlib.sha256(csv_p.read_bytes()).hexdigest() != rec.get("csv_sha256"):
        return None
    if hashlib.sha256(man_p.read_bytes()).hexdigest() != rec.get("manifest_sha256"):
        return None
    return rec


def _remove_incomplete_gen(gdir: Path) -> bool:
    """Safely delete an INCOMPLETE generation dir (never one with a valid
    COMPLETE marker). Returns True if removed."""
    if _gen_complete(gdir) is not None:
        return False
    try:
        for p in sorted(gdir.iterdir(), reverse=True):
            if p.is_file() or _is_link_like(p):
                p.unlink()
        gdir.rmdir()
        return True
    except OSError:
        return False


def recover_staging(venue: str, symbol: str) -> dict[str, Any]:
    """Startup/publish recovery, idempotent and fail-safe:

    * remove orphan exclusive temp files (*.part);
    * remove INCOMPLETE generation directories (no valid COMPLETE marker),
      EXCEPT the one the current CURRENT marker points at, which is never
      touched;
    * never delete a COMPLETE generation and never delete the current one.

    After a crash mid-publish, this lets an identical retry re-create the
    generation cleanly instead of hitting a spurious GENERATION_CONFLICT."""
    d = _dataset_dir(venue, symbol)
    removed_tmp = 0
    for p in d.rglob("*.part"):
        try:
            p.unlink()
            removed_tmp += 1
        except OSError:
            pass
    cur = _read_current(d)
    cur_gid = (cur or {}).get("generation_id")
    orphans, removed_incomplete = [], []
    for g in sorted(d.glob("gen_*")):
        if not g.is_dir():
            continue
        gid = g.name[len("gen_"):]
        if gid == cur_gid:
            continue
        if _gen_complete(g) is not None:
            orphans.append(g.name)                 # complete but not current
        elif _remove_incomplete_gen(g):
            removed_incomplete.append(g.name)      # partial: cleaned up
    return {"removed_temp_files": removed_tmp, "orphan_generations": orphans,
            "removed_incomplete_generations": removed_incomplete,
            "current_generation": cur_gid}


def _read_current(dsdir: Path) -> dict | None:
    p = dsdir / CURRENT_MARKER
    if not p.is_file():
        return None
    try:
        cur = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not cur.get("generation_id") or not cur.get("csv_sha256") \
            or not cur.get("manifest_sha256"):
        return None
    return cur


def current_generation(venue: str, symbol: str) -> dict | None:
    """Resolve the CURRENT generation and re-verify BOTH content hashes
    against the marker. A generation is usable only when marker, CSV and
    manifest all agree; anything else is treated as absent (fail-closed)."""
    d = _dataset_dir(venue, symbol)
    cur = _read_current(d)
    if cur is None:
        return None
    g = d / f"gen_{cur['generation_id']}"
    csv_p, man_p = g / "data.csv", g / "manifest.json"
    if not csv_p.is_file() or not man_p.is_file():
        return None
    if hashlib.sha256(csv_p.read_bytes()).hexdigest() != cur["csv_sha256"]:
        return None
    if hashlib.sha256(man_p.read_bytes()).hexdigest() != cur["manifest_sha256"]:
        return None
    return {**cur, "csv_path": csv_p, "manifest_path": man_p}


def save_dataset(venue: str, symbol: str, rows: list[list],
                 requested_days: int,
                 requested_start_ms: int | None = None,
                 requested_end_ms: int | None = None) -> dict[str, Any]:
    """Publish a content-addressed generation: CSV and manifest are written
    and verified in a staging generation directory, then a single atomic
    CURRENT marker commits BOTH. A crash at any earlier point leaves the
    previous generation untouched and current."""
    if requested_end_ms is None:
        requested_end_ms = (_now_ms() // BAR_MS) * BAR_MS
    if requested_start_ms is None:
        requested_start_ms = requested_end_ms - requested_days * 86_400_000
    # hard trim: never keep bars outside the requested interval
    rows = [r for r in rows
            if requested_start_ms <= int(r[0]) < requested_end_ms]
    raw_q = raw_quality_report(rows)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADER)
    for r in rows:
        w.writerow(r)
    csv_bytes = buf.getvalue().encode("utf-8")
    csv_sha = hashlib.sha256(csv_bytes).hexdigest()
    ts_list = [int(r[0]) for r in rows]
    expected_bars = max(0, (requested_end_ms - requested_start_ms) // BAR_MS)
    coverage_ratio = (len(rows) / expected_bars) if expected_bars else 0.0
    download_complete = (expected_bars - len(rows)) <= COMPLETENESS_TOLERANCE_BARS \
        and raw_q["raw_quality_pass"]
    source = ("bitget public /api/v2/mix/market/history-candles"
              if venue == "bitget" else "bybit public /v5/market/kline")
    manifest = {
        "tool_version": TOOL_VERSION,
        "downloader_version": DOWNLOADER_VERSION,
        "repo_commit": _repo_commit(),
        "schema_version": SCHEMA_VERSION,
        "source": source,
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
        "actual_start_ms": ts_list[0] if ts_list else None,
        "actual_end_ms": ts_list[-1] if ts_list else None,
        "actual_start": (datetime.fromtimestamp(ts_list[0] / 1000, timezone.utc)
                         .isoformat() if ts_list else None),
        "actual_end": (datetime.fromtimestamp(ts_list[-1] / 1000, timezone.utc)
                       .isoformat() if ts_list else None),
        "expected_bars": expected_bars,
        "actual_bars": len(rows),
        "n_bars": len(rows),
        "coverage_ratio": round(coverage_ratio, 6),
        "completeness_tolerance_bars": COMPLETENESS_TOLERANCE_BARS,
        "download_complete": download_complete,
        "invalid_candles": raw_q["invalid_candles"],
        "raw_quality_pass": raw_q["raw_quality_pass"],
        **strict_quality(ts_list),
        "sha256": csv_sha,
        "availability_contract": "bar open ts T is available at T+60000ms (close)",
        "limitations": ("aggregated OHLCV only: no per-side flow, no book, no "
                        "trades; funding/OI not included; venue clock assumed UTC"),
        "license_note": ("public market-data endpoint, no auth, used for "
                         "personal research within venue API ToS rate limits"),
        **_safety()}
    # generation identity depends on CSV bytes AND the manifest CONTRACT AND
    # schema/source/symbol/timeframe — a different contract can never reuse
    # (or overwrite) an existing generation directory
    contract_sha = manifest_contract_sha(manifest)
    generation_id = compute_generation_id(csv_sha, contract_sha, source,
                                          symbol, "1m")
    manifest["generation_id"] = generation_id
    manifest["contract_sha256"] = contract_sha
    dsdir = _dataset_dir(venue, symbol)
    recover_staging(venue, symbol)             # clean any incomplete staging
    gdir_path = dsdir / f"gen_{generation_id}"
    man_bytes = json.dumps(manifest, indent=2, default=str).encode("utf-8")
    complete = _gen_complete(gdir_path) if gdir_path.exists() else None
    if complete is not None:
        # A COMPLETE, immutable generation already exists under this id. By
        # construction the id derives from content, so it must be identical;
        # a content difference (impossible normally) is a hard conflict.
        if complete.get("csv_sha256") != csv_sha \
                or complete.get("contract_sha256") != contract_sha:
            raise IOError(f"GENERATION_CONFLICT: gen_{generation_id} is "
                          "COMPLETE with different content; immutable")
        written_csv_sha = complete["csv_sha256"]
        written_man_sha = complete["manifest_sha256"]
        gdir = gdir_path
    else:
        # No complete generation (fresh, or a partial left by a prior crash
        # that recover_staging just removed). Write CSV + manifest, verify,
        # then stamp the COMPLETE marker LAST — CURRENT is only updated after.
        if gdir_path.exists():
            _remove_incomplete_gen(gdir_path)
        gdir = validated_dir(*DATA_SUBDIR, f"{venue}_{symbol}_1m",
                             f"gen_{generation_id}")
        written_csv_sha = safe_atomic_write(gdir / "data.csv", csv_bytes)
        written_man_sha = safe_atomic_write(gdir / "manifest.json", man_bytes)
        _fsync_dir(gdir)
        # staging re-verification of BOTH artifacts before COMPLETE
        if hashlib.sha256((gdir / "data.csv").read_bytes()).hexdigest() != csv_sha \
                or hashlib.sha256((gdir / "manifest.json").read_bytes()) \
                .hexdigest() != written_man_sha:
            raise IOError("staging verification failed; generation NOT "
                          "committed")
        complete_rec = {"state": "COMPLETE", "generation_id": generation_id,
                        "csv_sha256": written_csv_sha,
                        "manifest_sha256": written_man_sha,
                        "contract_sha256": contract_sha,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "tool_version": TOOL_VERSION}
        safe_atomic_write(gdir / GEN_COMPLETE_MARKER,
                          json.dumps(complete_rec, indent=2).encode("utf-8"))
        _fsync_dir(gdir)
    # CURRENT is swapped ONLY after the generation is COMPLETE on disk
    marker = {"generation_id": generation_id,
              "csv_sha256": written_csv_sha,
              "manifest_sha256": written_man_sha,
              "committed_at": datetime.now(timezone.utc).isoformat(),
              "tool_version": TOOL_VERSION}
    safe_atomic_write(dsdir / CURRENT_MARKER,
                      json.dumps(marker, indent=2).encode("utf-8"))
    _fsync_dir(dsdir)                          # durability of CURRENT swap
    return {**manifest, "path": str(gdir / "data.csv").replace(chr(92), "/")}


def load_manifest(venue: str, symbol: str) -> dict[str, Any] | None:
    cur = current_generation(venue, symbol)
    if cur is None:
        return None
    try:
        return json.loads(cur["manifest_path"].read_text(encoding="utf-8"))
    except Exception:
        return None


# ==========================================================================
# STRICT LOADER: preserves CSV order, fails on the FIRST violation
# ==========================================================================

class DatasetError(ValueError):
    def __init__(self, status: str, detail: str = ""):
        super().__init__(f"{status}: {detail}" if detail else status)
        self.status = status
        self.detail = detail


def load_klines(venue: str, symbol: str) -> list[dict]:
    """Load the CURRENT generation CSV as canonical bar dicts, PRESERVING file
    order. Never sorts, never skips a row, never repairs: the first schema,
    finiteness, OHLC, order, duplicate or interval violation raises a
    structured DatasetError. Returns [] only when no generation exists."""
    cur = current_generation(venue, symbol)
    if cur is None:
        return []
    bars: list[dict] = []
    prev_ts = None
    with open(cur["csv_path"], "r", newline="", encoding="utf-8") as f:
        rd = csv.reader(f)
        header = next(rd, None)
        if header != CSV_HEADER:
            raise DatasetError("INVALID_SCHEMA", f"header={header!r}")
        for i, r in enumerate(rd, start=2):
            if len(r) != 7:
                raise DatasetError("INVALID_SCHEMA", f"line {i}: {len(r)} fields")
            try:
                ts = int(r[0])
                o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
                v, t = float(r[5]), float(r[6])
            except (ValueError, TypeError):
                raise DatasetError("INVALID_SCHEMA", f"line {i}: unparseable")
            if not all(math.isfinite(x) for x in (o, h, l, c, v, t)):
                raise DatasetError("INVALID_NON_FINITE", f"line {i}")
            if v < 0:
                raise DatasetError("INVALID_NEGATIVE_VOLUME", f"line {i}")
            if t < 0:
                raise DatasetError("INVALID_NEGATIVE_TURNOVER", f"line {i}")
            if ts <= 0 or o <= 0 or c <= 0 or l <= 0 or h < max(o, c) \
                    or l > min(o, c) or l > h:
                raise DatasetError("INVALID_OHLC", f"line {i}")
            if prev_ts is not None:
                if ts == prev_ts:
                    raise DatasetError("INVALID_DUPLICATE", f"line {i}")
                if ts < prev_ts:
                    raise DatasetError("INVALID_TIMESTAMP_ORDER", f"line {i}")
            prev_ts = ts
            bars.append({"ts": ts, "bar_open_ts": ts,
                         "bar_close_ts": ts + BAR_MS,
                         "available_at": ts + BAR_MS,
                         "open": o, "high": h, "low": l, "close": c,
                         "volume": v, "turnover": t,
                         "symbol": symbol, "venue": venue})
    return bars


# ==========================================================================
# VERIFICATION DERIVED FROM THE CSV (manifest = expected contract only)
# ==========================================================================

def _fail(status: str, **kw) -> dict[str, Any]:
    return {"ok": False, "status": status, **kw}


# machine-readable reason for every contractual discrepancy (the granular
# status is kept for backward compatibility; `reason` is the canonical field)
_CONTRACT_REASON = {
    "n_bars": "ACTUAL_BARS_MISMATCH", "actual_bars": "ACTUAL_BARS_MISMATCH",
    "expected_bars": "EXPECTED_BARS_MISMATCH",
    "coverage_ratio": "COVERAGE_MISMATCH",
    "schema_version": "SCHEMA_MISMATCH",
    "completeness_tolerance_bars": "TOLERANCE_MISMATCH",
    "gap_count": "GAP_COUNT_MISMATCH", "duplicates": "DUPLICATES_MISMATCH",
    "out_of_order": "IRREGULARITY_MISMATCH",
    "irregular_deltas": "IRREGULARITY_MISMATCH",
    "invalid_candles": "IRREGULARITY_MISMATCH",
    "raw_quality_pass": "IRREGULARITY_MISMATCH",
    "download_complete": "COVERAGE_MISMATCH",
    "actual_start_ms": "TIMESTAMP_RANGE_MISMATCH",
    "actual_end_ms": "TIMESTAMP_RANGE_MISMATCH",
    "generation_id": "GENERATION_ID_MISMATCH"}


def verify_dataset(venue: str, symbol: str,
                   expected_timeframe: str = "1m") -> dict[str, Any]:
    """FAIL-CLOSED verification computed FROM THE CSV. The manifest states the
    expected contract; every quality figure is recomputed from the actual
    bytes and rows and compared against it. No sorting, no row skipping, no
    repair, no longest-segment rescue. Mandatory before resample/features/
    splits/discovery."""
    try:
        d = _dataset_dir(venue, symbol)
    except ValueError as exc:
        return _fail("INVALID_VENUE" if "venue" in str(exc) else "INVALID_SYMBOL",
                     detail=str(exc)[:120])
    cur = _read_current(d)
    if cur is None:
        return _fail("INVALID_MANIFEST_CONTRACT", detail="NO_CURRENT_GENERATION")
    g = d / f"gen_{cur['generation_id']}"
    csv_p, man_p = g / "data.csv", g / "manifest.json"
    if not man_p.is_file():
        return _fail("INVALID_MANIFEST_CONTRACT", detail="MANIFEST_MISSING")
    if not csv_p.is_file():
        return _fail("INVALID_MANIFEST_CONTRACT", detail="CSV_MISSING")
    man_bytes = man_p.read_bytes()
    if hashlib.sha256(man_bytes).hexdigest() != cur["manifest_sha256"]:
        return _fail("INVALID_SHA", detail="manifest hash != CURRENT marker")
    try:
        manifest = json.loads(man_bytes.decode("utf-8"))
    except Exception:
        return _fail("INVALID_MANIFEST_CONTRACT", detail="MANIFEST_CORRUPT")
    # ---- 1) SHA of the actual CSV bytes
    csv_bytes = csv_p.read_bytes()
    sha = hashlib.sha256(csv_bytes).hexdigest()
    if sha != cur["csv_sha256"]:
        return _fail("INVALID_SHA", reason="CSV_SHA_MISMATCH",
                     detail="csv hash != CURRENT marker", actual_sha=sha[:16])
    if sha != manifest.get("sha256"):
        return _fail("INVALID_SHA", reason="CSV_SHA_MISMATCH",
                     detail="csv hash != manifest contract", actual_sha=sha[:16])
    # ---- 2) identity contract
    if manifest.get("venue") != venue:
        return _fail("INVALID_VENUE", reason="VENUE_MISMATCH", manifest_venue=manifest.get("venue"))
    if manifest.get("symbol") != symbol:
        return _fail("INVALID_SYMBOL", reason="SYMBOL_MISMATCH", manifest_symbol=manifest.get("symbol"))
    if manifest.get("timeframe") != expected_timeframe:
        return _fail("INVALID_TIMEFRAME", reason="TIMEFRAME_MISMATCH",
                     manifest_timeframe=manifest.get("timeframe"))
    # ---- 3) parse EVERY row strictly, in file order, recomputing everything
    n_rows = 0
    first_ts = last_ts = None
    prev_ts = None
    try:
        rd = csv.reader(io.StringIO(csv_bytes.decode("utf-8")))
        header = next(rd, None)
        if header != CSV_HEADER:
            return _fail("INVALID_SCHEMA", detail=f"header={header!r}")
        for i, r in enumerate(rd, start=2):
            if len(r) != 7:
                return _fail("INVALID_SCHEMA", detail=f"line {i}: {len(r)} fields")
            try:
                ts = int(r[0])
                o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
                v, t = float(r[5]), float(r[6])
            except (ValueError, TypeError):
                return _fail("INVALID_SCHEMA", detail=f"line {i}: unparseable")
            if not all(math.isfinite(x) for x in (o, h, l, c, v, t)):
                return _fail("INVALID_NON_FINITE", detail=f"line {i}")
            if v < 0:
                return _fail("INVALID_NEGATIVE_VOLUME", detail=f"line {i}")
            if t < 0:
                return _fail("INVALID_NEGATIVE_TURNOVER", detail=f"line {i}")
            if ts <= 0 or o <= 0 or c <= 0 or l <= 0 or h < max(o, c) \
                    or l > min(o, c) or l > h:
                return _fail("INVALID_OHLC", detail=f"line {i}")
            if prev_ts is not None:
                delta = ts - prev_ts
                if delta == 0:
                    return _fail("INVALID_DUPLICATE", detail=f"line {i}")
                if delta < 0:
                    return _fail("INVALID_TIMESTAMP_ORDER", detail=f"line {i}")
                if delta % BAR_MS != 0:
                    return _fail("INVALID_TIMESTAMP_INTERVAL",
                                 detail=f"line {i}: delta={delta}")
                if delta != BAR_MS:
                    return _fail("INVALID_GAP",
                                 detail=f"line {i}: missing={delta // BAR_MS - 1}")
            first_ts = ts if first_ts is None else first_ts
            last_ts = ts
            prev_ts = ts
            n_rows += 1
    except UnicodeDecodeError:
        return _fail("INVALID_SCHEMA", detail="not utf-8")
    if n_rows == 0:
        return _fail("INVALID_COVERAGE", detail="zero rows")
    # ---- 4) window / coverage / as_of recomputed vs contract
    req_start = manifest.get("requested_start_ms")
    req_end = manifest.get("requested_end_ms")
    if not isinstance(req_start, int) or not isinstance(req_end, int) \
            or req_end <= req_start:
        return _fail("INVALID_MANIFEST_CONTRACT", detail="requested window")
    if req_end % BAR_MS != 0:
        return _fail("INVALID_AS_OF", reason="AS_OF_MISMATCH",
                     detail="as_of not bar-aligned")
    if first_ts < req_start or last_ts >= req_end:
        return _fail("INVALID_COVERAGE", reason="TIMESTAMP_RANGE_MISMATCH",
                     detail=f"rows outside requested window "
                            f"[{req_start},{req_end})")
    if last_ts + BAR_MS > req_end:
        return _fail("INVALID_AS_OF", reason="AS_OF_MISMATCH",
                     detail="last bar closes after as_of")
    expected_bars = (req_end - req_start) // BAR_MS
    missing = expected_bars - n_rows
    if missing > COMPLETENESS_TOLERANCE_BARS:
        return _fail("INVALID_COVERAGE", reason="COVERAGE_MISMATCH",
                     detail=f"{n_rows}/{expected_bars} rows")
    # ---- 5) manifest contract must MATCH the recomputed truth
    recomputed = {"n_rows": n_rows, "first_ts": first_ts, "last_ts": last_ts,
                  "gap_count": 0, "duplicates": 0, "out_of_order": 0,
                  "irregular_deltas": 0, "invalid_candles": 0,
                  "expected_bars": expected_bars,
                  "coverage_ratio": round(n_rows / expected_bars, 6)
                  if expected_bars else 0.0}
    contract_checks = (
        ("n_bars", n_rows), ("actual_bars", n_rows),
        ("expected_bars", expected_bars),
        ("coverage_ratio", round(n_rows / expected_bars, 6)
         if expected_bars else 0.0),
        ("schema_version", SCHEMA_VERSION),
        ("completeness_tolerance_bars", COMPLETENESS_TOLERANCE_BARS),
        ("gap_count", 0), ("duplicates", 0), ("out_of_order", 0),
        ("irregular_deltas", 0), ("invalid_candles", 0),
        ("raw_quality_pass", True), ("download_complete", True),
        ("actual_start_ms", first_ts), ("actual_end_ms", last_ts),
        ("generation_id", cur["generation_id"]))
    for key, truth in contract_checks:
        if manifest.get(key) != truth:
            return _fail("INVALID_MANIFEST_CONTRACT",
                         reason=_CONTRACT_REASON.get(key, "SCHEMA_MISMATCH"),
                         detail=f"{key}: manifest={manifest.get(key)!r} "
                                f"recomputed={truth!r}")
    # the generation id itself must re-derive from CSV + contract + schema:
    # a marker pointing at a directory whose id does not match its content
    # is a forged or corrupted generation
    recomputed_gid = compute_generation_id(
        sha, manifest_contract_sha(manifest), manifest.get("source", ""),
        symbol, expected_timeframe)
    if recomputed_gid != cur["generation_id"]:
        return _fail("INVALID_MANIFEST_CONTRACT", reason="GENERATION_ID_MISMATCH",
                     detail=f"generation_id: marker={cur['generation_id']} "
                            f"recomputed={recomputed_gid}")
    return {"ok": True, "status": "DATASET_VERIFIED", "sha256": sha,
            "generation_id": cur["generation_id"],
            "recomputed": recomputed, "manifest": manifest,
            "as_of_ms": req_end}


def run_backfill(symbols_bitget: list[str], symbols_bybit: list[str],
                 days: int = 90, log=print) -> dict[str, Any]:
    # ONE shared requested window (minute-aligned) for every dataset in the
    # run, so all manifests state the same explicit interval and every CSV is
    # trimmed to exactly that window
    req_end = (_now_ms() // BAR_MS) * BAR_MS
    req_start = req_end - days * 86_400_000
    manifests = []
    for venue, syms in (("bitget", symbols_bitget), ("bybit", symbols_bybit)):
        for sym in syms:
            log(f"fetch {venue} {sym} {days}d ...")
            rows = (fetch_bitget_1m if venue == "bitget" else fetch_bybit_1m)(
                sym, days, log=log, end_ms=req_end)
            m = save_dataset(venue, sym, rows, days,
                             requested_start_ms=req_start,
                             requested_end_ms=req_end)
            log(f"  -> {m['n_bars']}/{m['expected_bars']} bars, "
                f"gaps={m['gap_count']}, complete={m['download_complete']}, "
                f"gen={m['generation_id']}, sha={m['sha256'][:12]}")
            manifests.append(m)
    summary = {"tool_version": TOOL_VERSION,
               "ran_at": datetime.now(timezone.utc).isoformat(),
               "datasets": manifests, **_safety()}
    out = validated_dir("reports", "research", "v10_45_5_edge_discovery")
    safe_atomic_write(out / "data_backfill_manifest_v10_45_5.json",
                      json.dumps(summary, indent=2, default=str).encode("utf-8"))
    return summary
