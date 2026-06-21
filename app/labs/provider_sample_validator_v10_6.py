"""ResearchOps V10.6 — Provider Sample Validator + Content-Aware Manifest.

Validates a LOCAL sample directory of provider data WITHOUT touching raw,
without re-ingest/replace, without DB writes and without network. Computes
real SHA-256, coverage, gaps, duplicates and per-type content sanity
(OHLCV/OI/funding/liquidations), then can build a research-only manifest
(written to a reports dir, never raw) that is gated by the V10.5.6 validator.

Dependency-light: stdlib only (csv, json, hashlib, datetime). Parquet is
documented future work — not supported here to avoid heavy deps. Never raises
on bad input: malformed rows are counted, not crashed on.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from .data_foundation_v10_5 import (
    ALLOWED_TIMEFRAMES,
    REQUIRED_INVENTORY_TYPES,
    SCHEMA_VERSION,
    classify_file_path,
    evaluate_manifest_v105,
    normalize_data_type,
)

TOOL_VERSION = "v10.6"
VALIDATION_VERSION = "v10.6.0"

_SYMBOL_RE = __import__("re").compile(r"^[A-Z0-9]{2,15}USDT$")
_TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
          "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
_SUPPORTED_EXT = (".csv", ".jsonl", ".ndjson")
_MAX_OI_MISSING_RATIO = 0.02
_CLUSTERED_MISSING_RUN = 5      # consecutive missing >= this => clustered
_EXTREME_FUNDING = 0.01         # |funding| above this is flagged

# data classification
CLS_SAMPLE_ONLY = "SAMPLE_ONLY"
CLS_INTERMEDIATE = "INTERMEDIATE_RESEARCH_ONLY"
CLS_LONG_READY = "LONG_HISTORY_RESEARCH_READY"


def _finite(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _parse_ts_ms(value: Any) -> int | None:
    """Parse a timestamp to unix ms. Accepts unix s/ms or ISO-8601. None on fail."""
    f = _finite(value)
    if f is not None and f > 0:
        return int(f * 1000) if f < 1e12 else int(f)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    return None


def _sha256_file(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _read_rows(path: str) -> list[dict[str, Any]]:
    """Read CSV or JSONL into list[dict]. Never raises; bad lines skipped."""
    rows: list[dict[str, Any]] = []
    lower = path.lower()
    try:
        if lower.endswith(".csv"):
            with open(path, "r", encoding="utf-8", newline="") as fh:
                for r in csv.DictReader(fh):
                    rows.append(dict(r))
        elif lower.endswith((".jsonl", ".ndjson")):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            rows.append(obj)
                    except Exception:
                        continue
    except Exception:
        return rows
    return rows


def _infer_identity(filename: str) -> tuple[str | None, str | None, str | None]:
    """Infer (symbol, timeframe, canonical_data_type) from the filename tokens."""
    stem = os.path.basename(filename)
    for ext in _SUPPORTED_EXT:
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    tokens = [t for t in stem.replace("-", "_").split("_") if t]
    symbol = next((t.upper() for t in tokens if _SYMBOL_RE.match(t.upper())), None)
    timeframe = next((t.lower() for t in tokens if t.lower() in ALLOWED_TIMEFRAMES), None)
    data_type = None
    for t in tokens:
        canon = normalize_data_type(t)
        if canon is not None:
            data_type = canon
            break
    return symbol, timeframe, data_type


def _col(row: dict[str, Any], *names: str) -> Any:
    low = {str(k).strip().lower(): v for k, v in row.items()}
    for n in names:
        if n in low:
            return low[n]
    return None


def _check_ohlcv(rows: list[dict[str, Any]]) -> list[str]:
    bad: list[str] = []
    invalid = 0
    for r in rows:
        o, h, l, c = (_finite(_col(r, "open", "price_open")),
                      _finite(_col(r, "high", "price_high")),
                      _finite(_col(r, "low", "price_low")),
                      _finite(_col(r, "close", "price_close")))
        v = _finite(_col(r, "volume", "volume_usd", "vol"))
        if None in (o, h, l, c) or v is None or v < 0:
            invalid += 1
            continue
        if h < l or h < max(o, c) or l > min(o, c):
            invalid += 1
    if invalid:
        bad.append(f"ohlcv_invalid_rows:{invalid}")
    return bad


def _check_oi(rows: list[dict[str, Any]]) -> tuple[float, int, bool]:
    """Return (missing_ratio, max_consecutive_missing, clustered)."""
    total = len(rows) or 1
    missing = 0
    run = 0
    max_run = 0
    for r in rows:
        oi = _finite(_col(r, "open_interest", "oi", "oi_usd_close", "openinterest"))
        if oi is None or oi < 0:
            missing += 1
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    return round(missing / total, 4), max_run, max_run >= _CLUSTERED_MISSING_RUN


def _check_funding(rows: list[dict[str, Any]]) -> list[str]:
    bad: list[str] = []
    invalid = extreme = 0
    for r in rows:
        f = _finite(_col(r, "funding_rate", "funding", "fundingrate"))
        if f is None:
            invalid += 1
        elif abs(f) > _EXTREME_FUNDING:
            extreme += 1
    if invalid:
        bad.append(f"funding_invalid_rows:{invalid}")
    if extreme:
        bad.append(f"funding_extreme_rows:{extreme}")
    return bad


def _check_liquidations(rows: list[dict[str, Any]]) -> list[str]:
    bad: list[str] = []
    invalid = 0
    for r in rows:
        notional = _finite(_col(r, "notional_usd", "notional", "qty", "amount"))
        if notional is None or notional < 0:
            invalid += 1
        side = _col(r, "side", "direction")
        if side is not None and str(side).strip().lower() not in (
                "", "buy", "sell", "long", "short", "b", "s"):
            invalid += 1
    if invalid:
        bad.append(f"liquidations_invalid_rows:{invalid}")
    return bad


@dataclass
class SampleFileReport:
    path: str = ""
    data_type: str = ""
    symbol: str = ""
    timeframe: str = ""
    rows: int = 0
    sha256: str = ""
    start_ts: Any = None
    end_ts: Any = None
    duplicates: int = 0
    monotonic: bool = True
    gap_count: int = 0
    max_gap_ms: int = 0
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validate_file(full_path: str, rel_path: str) -> SampleFileReport:
    rep = SampleFileReport(path=rel_path)
    # Path safety (V10.5.6) on the declared relative path.
    path_block = classify_file_path(rel_path)
    if path_block is not None:
        rep.blockers.append(path_block)
        return rep
    sha = _sha256_file(full_path)
    if sha is None:
        rep.blockers.append("sha256_unreadable")
        return rep
    rep.sha256 = sha
    symbol, timeframe, data_type = _infer_identity(rel_path)
    if data_type is None:
        rep.blockers.append("unrecognized_data_type")
        return rep
    rep.data_type = data_type
    rep.symbol = symbol or ""
    rep.timeframe = timeframe or ""
    if symbol is None:
        rep.warnings.append("symbol_not_inferred")
    rows = _read_rows(full_path)
    rep.rows = len(rows)
    if rep.rows <= 0:
        rep.blockers.append("zero_rows")
        return rep

    # Timestamps: monotonic + duplicates + gaps.
    tss = []
    for r in rows:
        ts = _parse_ts_ms(_col(r, "timestamp", "ts", "time", "open_time", "datetime"))
        if ts is not None:
            tss.append(ts)
    if not tss:
        rep.blockers.append("no_parseable_timestamps")
        return rep
    rep.start_ts, rep.end_ts = min(tss), max(tss)
    rep.duplicates = len(tss) - len(set(tss))
    if rep.duplicates > 0:
        rep.blockers.append(f"duplicate_timestamps:{rep.duplicates}")
    rep.monotonic = all(b >= a for a, b in zip(tss, tss[1:]))
    if not rep.monotonic:
        rep.blockers.append("timestamps_not_monotonic")
    if timeframe and timeframe in _TF_MS and len(tss) > 1:
        bar = _TF_MS[timeframe]
        ordered = sorted(set(tss))
        gaps = 0
        max_gap = 0
        for a, b in zip(ordered, ordered[1:]):
            delta = b - a
            if delta > bar:
                gaps += int(delta // bar) - 1
                max_gap = max(max_gap, delta)
        rep.gap_count = gaps
        rep.max_gap_ms = max_gap

    # Per-type content sanity.
    if data_type == "ohlcv":
        rep.blockers.extend(_check_ohlcv(rows))
    elif data_type == "open_interest":
        ratio, max_run, clustered = _check_oi(rows)
        rep.warnings.append(f"missing_oi_ratio:{ratio}")
        rep.warnings.append(f"max_consecutive_missing:{max_run}")
        if clustered:
            rep.blockers.append("oi_missing_clustered")
        elif ratio > _MAX_OI_MISSING_RATIO:
            rep.blockers.append(f"oi_missing_ratio_high:{ratio}")
    elif data_type == "funding":
        rep.warnings.extend(_check_funding(rows))
    elif data_type == "liquidations":
        rep.blockers.extend(_check_liquidations(rows))
    return rep


def _list_sample_files(sample_dir: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for root, _dirs, files in os.walk(sample_dir):
        for fn in files:
            if fn.lower().endswith(_SUPPORTED_EXT):
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, sample_dir).replace("\\", "/")
                out.append((full, rel))
    return sorted(out)


def validate_sample_dir(sample_dir: str, expected_days: int = 180,
                        provider_id: str = "") -> dict[str, Any]:
    """Validate a local sample directory. Read-only; never writes/ingests."""
    report: dict[str, Any] = {
        "provider_id": provider_id, "sample_dir": sample_dir,
        "expected_days": int(expected_days), "tool_version": TOOL_VERSION,
        "validation_version": VALIDATION_VERSION,
        "dataset_hash": "", "files": [], "rows_total": 0,
        "coverage": {}, "quality": {}, "blockers": [], "warnings": [],
        "sample_ready": False, "data_classification": CLS_SAMPLE_ONLY,
        "paper_ready": False, "live_ready": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }
    if not (isinstance(sample_dir, str) and sample_dir and os.path.isdir(sample_dir)):
        report["blockers"].append("sample_dir_not_found")
        return report
    files = _list_sample_files(sample_dir)
    if not files:
        report["blockers"].append("no_supported_files (csv/jsonl/ndjson)")
        return report

    file_reports: list[SampleFileReport] = []
    seen_paths: set[str] = set()
    seen_shas: set[str] = set()
    for full, rel in files:
        fr = _validate_file(full, rel)
        norm = rel.strip().lower()
        if norm in seen_paths:
            fr.blockers.append("duplicate_file_path_in_inventory")
        seen_paths.add(norm)
        if fr.sha256:
            if fr.sha256 in seen_shas:
                fr.blockers.append("duplicate_file_sha256_across_data_types")
            seen_shas.add(fr.sha256)
        file_reports.append(fr)

    report["files"] = [fr.as_dict() for fr in file_reports]
    report["rows_total"] = sum(fr.rows for fr in file_reports)

    # Aggregate dataset hash from per-file shas (order-independent, stable).
    digest = hashlib.sha256()
    for sha in sorted(fr.sha256 for fr in file_reports if fr.sha256):
        digest.update(sha.encode())
    report["dataset_hash"] = digest.hexdigest()

    # Coverage: use the widest [start,end] across files that parsed timestamps.
    starts = [fr.start_ts for fr in file_reports if fr.start_ts is not None]
    ends = [fr.end_ts for fr in file_reports if fr.end_ts is not None]
    if starts and ends:
        start_ms, end_ms = min(starts), max(ends)
        actual_days = round((end_ms - start_ms) / 86_400_000.0, 2)
        report["coverage"] = {
            "start_ts": start_ms, "end_ts": end_ms,
            "actual_days_covered": actual_days, "expected_days": int(expected_days),
            "coverage_ratio_by_days": round(min(1.0, actual_days / max(1, expected_days)), 4),
        }
    else:
        report["coverage"] = {"actual_days_covered": 0.0,
                              "expected_days": int(expected_days),
                              "coverage_ratio_by_days": 0.0}
        report["blockers"].append("no_coverage_window")

    present_types = {fr.data_type for fr in file_reports if fr.data_type}
    report["quality"] = {
        "data_types_present": sorted(present_types),
        "required_types_missing": sorted(REQUIRED_INVENTORY_TYPES - present_types),
        "total_duplicates": sum(fr.duplicates for fr in file_reports),
        "total_gap_count": sum(fr.gap_count for fr in file_reports),
        "files_with_blockers": sum(1 for fr in file_reports if fr.blockers),
    }

    file_blockers = [b for fr in file_reports for b in fr.blockers]
    report["blockers"].extend(file_blockers)
    report["warnings"].extend(w for fr in file_reports for w in fr.warnings)

    actual_days = report["coverage"].get("actual_days_covered", 0.0)
    if actual_days >= 180 and not report["blockers"]:
        report["data_classification"] = CLS_LONG_READY
    elif actual_days >= 30:
        report["data_classification"] = CLS_INTERMEDIATE
    else:
        report["data_classification"] = CLS_SAMPLE_ONLY

    report["sample_ready"] = (not report["blockers"]
                              and report["rows_total"] > 0
                              and not report["quality"]["required_types_missing"])
    # V10.7.2 — human-readable clarity (additive; does NOT change gates). Make a
    # missing required type — especially ohlcv — explicit for a human reader so
    # "blockers: NONE" is never mistaken for "sample is complete".
    human: list[str] = []
    for t in report["quality"]["required_types_missing"]:
        human.append(f"missing_required_type:{t}")
    if "ohlcv" in report["quality"]["required_types_missing"]:
        human.append("no_price_ohlcv_present_sample_not_usable_for_price_research")
    if not report["sample_ready"]:
        human.append("sample_not_ready (manifest will not be promotable)")
    report["human_warnings"] = human
    report["paper_ready"] = False
    report["live_ready"] = False
    return report


# ---------------------------------------------------------------------------
# C. Content-aware manifest builder (writes to a reports dir, never raw)
# ---------------------------------------------------------------------------

MANIFEST_OUTPUT_DIR = "external_data/reports/v10_6_manifests"


def build_sample_manifest(sample_dir: str, expected_days: int = 180,
                          provider_id: str = "",
                          write: bool = False) -> dict[str, Any]:
    """Build a research-only manifest from a validated sample. Optionally write
    it to a reports dir (never raw, never DB). Gated by evaluate_manifest_v105."""
    v = validate_sample_dir(sample_dir, expected_days=expected_days,
                            provider_id=provider_id)
    cov = v.get("coverage", {})
    start_ms, end_ms = cov.get("start_ts"), cov.get("end_ts")

    def _iso(ms: Any) -> str:
        try:
            return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
        except Exception:
            return ""

    files_inv = []
    rows_by_type: dict[str, int] = {}
    checksums: dict[str, str] = {}
    oi_ratio = 0.0
    oi_status = "NEED_MORE_DATA"
    for fr in v.get("files", []):
        if not fr.get("data_type") or not fr.get("sha256") or fr.get("rows", 0) <= 0:
            continue
        files_inv.append({"path": fr["path"], "data_type": fr["data_type"],
                          "symbol": fr.get("symbol", ""),
                          "timeframe": fr.get("timeframe", ""),
                          "rows": fr["rows"], "sha256": fr["sha256"],
                          "start_ts": fr.get("start_ts"), "end_ts": fr.get("end_ts")})
        rows_by_type[fr["data_type"]] = rows_by_type.get(fr["data_type"], 0) + fr["rows"]
        checksums[fr["path"]] = fr["sha256"]
        for w in fr.get("warnings", []):
            if isinstance(w, str) and w.startswith("missing_oi_ratio:"):
                try:
                    oi_ratio = float(w.split(":", 1)[1])
                except ValueError:
                    pass
    if "oi_missing_clustered" in v.get("blockers", []):
        oi_status = "MISSING_OI_CLUSTERED"
    elif oi_ratio <= _MAX_OI_MISSING_RATIO and "open_interest" in rows_by_type:
        oi_status = "DATA_OK"

    actual_days = float(cov.get("actual_days_covered", 0.0) or 0.0)
    manifest = {
        "provider_id": provider_id, "source_provider": provider_id or "unknown",
        "license_terms": "NEEDS_MANUAL_VERIFICATION",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tool_version": TOOL_VERSION, "validation_version": VALIDATION_VERSION,
        "schema_version": SCHEMA_VERSION,
        "dataset_hash": v.get("dataset_hash", ""),
        "input_sample_hash": v.get("dataset_hash", ""),
        "files": files_inv,
        "requested_range": {"start": _iso(start_ms), "end": _iso(end_ms)} if start_ms else {},
        "actual_covered_range": {"start": _iso(start_ms), "end": _iso(end_ms)} if start_ms else {},
        "requested_days": int(expected_days),
        "clean_days": actual_days,
        "coverage_ratio": float(cov.get("coverage_ratio_by_days", 0.0) or 0.0),
        "coverage_ratio_by_days": float(cov.get("coverage_ratio_by_days", 0.0) or 0.0),
        "symbols": sorted({f["symbol"] for f in files_inv if f.get("symbol")}),
        "timeframes": sorted({f["timeframe"] for f in files_inv if f.get("timeframe")}),
        "data_types": sorted(rows_by_type.keys()),
        "rows_by_type": rows_by_type,
        "gap_count": int(v.get("quality", {}).get("total_gap_count", 0) or 0),
        "duplicate_count": int(v.get("quality", {}).get("total_duplicates", 0) or 0),
        "missing_oi_ratio": oi_ratio,
        "missing_oi_status": oi_status,
        "missing_funding_ratio": 0.0,
        "missing_liquidations_ratio": 0.0,
        "timezone": "UTC", "timestamp_unit": "unix_ms",
        "checksums_sha256": checksums,
        "import_status": "STAGED",  # never auto STAGED_READY_FOR_PROMOTE
        "explicit_human_authorization": False,  # human-only, never auto
        "paid_download_authorized": False,
        "license_terms_confirmed": False,
        "authorization_reference": "",
        "validation_sample_ready": bool(v.get("sample_ready")),
        "validation_blockers": list(v.get("blockers", [])),
        "data_classification": v.get("data_classification"),
        "paper_ready": False, "live_ready": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }
    # Gate evaluation (research-only). It will report AUTHORIZATION_REQUIRED
    # because the human-auth fields are false by design.
    gate = evaluate_manifest_v105(manifest)
    manifest["gate_status"] = gate.status
    manifest["gate_promote_allowed"] = gate.promote_allowed
    manifest["gate_blockers"] = list(gate.blockers)

    written_path = ""
    if write:
        try:
            os.makedirs(MANIFEST_OUTPUT_DIR, exist_ok=True)
            safe_provider = "".join(ch for ch in (provider_id or "sample")
                                    if ch.isalnum() or ch in ("_", "-")) or "sample"
            fname = f"manifest_{safe_provider}_{manifest['dataset_hash'][:12] or 'nohash'}.json"
            written_path = os.path.join(MANIFEST_OUTPUT_DIR, fname)
            with open(written_path, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, indent=2, default=str)
        except Exception as exc:
            manifest["write_error"] = str(type(exc).__name__)
            written_path = ""
    manifest["written_path"] = written_path
    return manifest
