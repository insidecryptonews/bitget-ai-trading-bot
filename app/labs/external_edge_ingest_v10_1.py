"""ResearchOps V10.1 — External Edge Data ingest + validation.

Reads *local* CSV / JSON-array / NDJSON files of external edge data,
validates each row against the V10.1 schemas, normalizes timestamps to
UNIX ms UTC, detects duplicates and gaps, classifies data quality, and
writes cleaned outputs + a research report.

HARD CONTRACT — research only:

- never opens orders, never calls private endpoints, never touches the
  main DB (no DB writes in this batch; a future explicit flag stays OFF),
- never calls any network / paid API,
- never reads ``.env`` or secrets,
- only reads local files and writes under ``external_data/clean`` and
  ``external_data/reports`` when a write dir is given,
- missing data => ``NEED_DATA`` (honest, never fabricated),
- always ``final_recommendation = NO LIVE``.
"""

from __future__ import annotations

import csv
import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .external_edge_schemas_v10_1 import (
    ALL_DATASETS,
    DS_PERP_MARKET,
    FINAL_RECOMMENDATION_NO_LIVE,
    detect_dataset_type,
    symbol_field,
    validate_row,
)

# Dataset-level statuses.
STATUS_NEED_DATA = "NEED_DATA"
STATUS_DATA_OK = "DATA_OK"
STATUS_BAD = "DATA_QUALITY_BAD"
STATUS_SCHEMA_INVALID = "SCHEMA_INVALID"
STATUS_STALE = "STALE"
STATUS_REJECT = "REJECT"

# Quality thresholds.
INVALID_RATE_REJECT = 0.50
DUPLICATE_RATE_BAD = 0.30
GAP_RATE_BAD = 0.30
# Staleness (latest clean timestamp older than this => STALE), in hours.
DEFAULT_STALE_HOURS = 24.0
# Gap detection: a delta beyond this multiple of the per-symbol median
# inter-timestamp delta counts as a gap.
GAP_MULTIPLE = 3.0
MIN_POINTS_FOR_GAPS = 5


@dataclass
class IngestReport:
    dataset: str = ""
    generated_at: str = ""
    inputs: list[str] = field(default_factory=list)
    rows_raw: int = 0
    rows_valid: int = 0
    rows_invalid: int = 0
    duplicate_count: int = 0
    gap_count: int = 0
    symbols: list[str] = field(default_factory=list)
    min_timestamp_ms: int | None = None
    max_timestamp_ms: int | None = None
    min_timestamp_iso: str = ""
    max_timestamp_iso: str = ""
    invalid_rate: float = 0.0
    duplicate_rate: float = 0.0
    gap_rate: float = 0.0
    data_quality_status: str = STATUS_NEED_DATA
    top_error: str = ""
    error_breakdown: dict[str, int] = field(default_factory=dict)
    output_clean_csv: str = ""
    output_clean_ndjson: str = ""
    output_report_json: str = ""
    db_writes: int = 0
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Local file reading (no network)
# --------------------------------------------------------------------------


def read_rows(path: str | Path) -> tuple[list[dict[str, Any]], str]:
    """Read a local CSV / JSON-array / NDJSON file. Returns (rows, fmt).
    Never raises; on error returns ([], "<error>")."""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return [], "MISSING_FILE"
    suffix = p.suffix.lower()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return [], "UNREADABLE_FILE"
    if suffix == ".csv" or suffix == ".tsv":
        delim = "\t" if suffix == ".tsv" else ","
        try:
            reader = csv.DictReader(text.splitlines(), delimiter=delim)
            return [dict(r) for r in reader], "csv"
        except (csv.Error, ValueError):
            return [], "BAD_CSV"
    if suffix == ".ndjson" or suffix == ".jsonl":
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out, "ndjson"
    if suffix == ".json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return [], "BAD_JSON"
        if isinstance(payload, dict):
            payload = payload.get("rows") or payload.get("data") or []
        if not isinstance(payload, list):
            return [], "BAD_JSON_SHAPE"
        return [dict(r) for r in payload if isinstance(r, dict)], "json"
    return [], "UNSUPPORTED_FORMAT"


def read_input_dir(input_dir: str | Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Read every supported file in a directory (non-recursive)."""
    d = Path(input_dir)
    rows: list[dict[str, Any]] = []
    used: list[str] = []
    if not d.exists() or not d.is_dir():
        return rows, used
    for p in sorted(d.iterdir()):
        if p.suffix.lower() in (".csv", ".tsv", ".json", ".ndjson", ".jsonl"):
            r, _ = read_rows(p)
            if r:
                rows.extend(r)
                used.append(p.name)
    return rows, used


# --------------------------------------------------------------------------
# Gap detection
# --------------------------------------------------------------------------


def detect_gaps(timestamps_by_symbol: dict[str, list[int]]) -> int:
    """Count inter-timestamp gaps per symbol (delta > GAP_MULTIPLE * median).
    Only meaningful for regular time series."""
    gaps = 0
    for sym, ts in timestamps_by_symbol.items():
        s = sorted(set(ts))
        if len(s) < MIN_POINTS_FOR_GAPS:
            continue
        deltas = [b - a for a, b in zip(s, s[1:]) if b > a]
        if len(deltas) < MIN_POINTS_FOR_GAPS - 1:
            continue
        med = statistics.median(deltas)
        if med <= 0:
            continue
        for d in deltas:
            if d > GAP_MULTIPLE * med:
                gaps += 1
    return gaps


# --------------------------------------------------------------------------
# Core ingest
# --------------------------------------------------------------------------


def ingest_rows(
    rows: Iterable[dict[str, Any]] | None,
    dataset: str,
    *,
    now_ms: int | None = None,
    stale_hours: float = DEFAULT_STALE_HOURS,
) -> tuple[IngestReport, list[dict[str, Any]]]:
    """Validate + classify rows. Returns (report, clean_rows). Pure: no I/O."""
    report = IngestReport(
        dataset=dataset,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    row_list = [dict(r) for r in (rows or [])]
    report.rows_raw = len(row_list)
    if dataset not in ALL_DATASETS:
        report.data_quality_status = STATUS_SCHEMA_INVALID
        report.top_error = f"unknown_dataset:{dataset}"
        return report, []
    if not row_list:
        report.data_quality_status = STATUS_NEED_DATA
        return report, []

    seen: set[str] = set()
    clean: list[dict[str, Any]] = []
    errors: dict[str, int] = {}
    ts_by_symbol: dict[str, list[int]] = {}
    symbols: set[str] = set()
    invalid = 0
    duplicates = 0

    for raw in row_list:
        v = validate_row(raw, dataset)
        if not v["valid"]:
            invalid += 1
            for e in v["errors"]:
                key = e.split(":")[0]
                errors[key] = errors.get(key, 0) + 1
            continue
        key = v["logical_key"]
        if key in seen:
            duplicates += 1
            errors["logical_duplicate"] = errors.get("logical_duplicate", 0) + 1
            continue
        seen.add(key)
        out = dict(raw)
        out["timestamp_ms"] = v["timestamp_ms"]
        out["logical_key"] = key
        out["data_quality_status"] = STATUS_DATA_OK
        out["validation_errors"] = ""
        clean.append(out)
        sym = str(raw.get(symbol_field(dataset)) or "").strip().upper()
        if sym:
            symbols.add(sym)
            if v["timestamp_ms"] is not None:
                ts_by_symbol.setdefault(sym, []).append(v["timestamp_ms"])

    report.rows_invalid = invalid
    report.duplicate_count = duplicates
    report.rows_valid = len(clean)
    report.symbols = sorted(symbols)
    report.error_breakdown = dict(sorted(errors.items(), key=lambda kv: kv[1], reverse=True))
    if report.error_breakdown:
        report.top_error = next(iter(report.error_breakdown))

    all_ts = [t for ts in ts_by_symbol.values() for t in ts]
    if all_ts:
        report.min_timestamp_ms = min(all_ts)
        report.max_timestamp_ms = max(all_ts)
        report.min_timestamp_iso = _ms_to_iso(report.min_timestamp_ms)
        report.max_timestamp_iso = _ms_to_iso(report.max_timestamp_ms)

    # Gaps only meaningful for the regular market-state series.
    if dataset == DS_PERP_MARKET:
        report.gap_count = detect_gaps(ts_by_symbol)

    denom = max(report.rows_raw, 1)
    report.invalid_rate = round(invalid / denom, 4)
    report.duplicate_rate = round(duplicates / denom, 4)
    n_clean = max(report.rows_valid, 1)
    report.gap_rate = round(report.gap_count / n_clean, 4)

    report.data_quality_status = _classify_quality(report, now_ms=now_ms, stale_hours=stale_hours)
    return report, clean


def _classify_quality(report: IngestReport, *, now_ms: int | None, stale_hours: float) -> str:
    if report.rows_valid == 0:
        # Everything invalid: schema if dominated by missing/bad-field, else reject.
        if report.rows_raw == 0:
            return STATUS_NEED_DATA
        return STATUS_SCHEMA_INVALID
    if report.invalid_rate >= INVALID_RATE_REJECT:
        return STATUS_REJECT
    if report.duplicate_rate >= DUPLICATE_RATE_BAD or report.gap_rate >= GAP_RATE_BAD:
        return STATUS_BAD
    # Staleness.
    if report.max_timestamp_ms is not None:
        ref = now_ms if now_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
        age_h = (ref - report.max_timestamp_ms) / 3_600_000.0
        if age_h > stale_hours:
            return STATUS_STALE
    return STATUS_DATA_OK


def _ms_to_iso(ms: int | None) -> str:
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Clean output writing
# --------------------------------------------------------------------------


def write_clean_outputs(
    clean_rows: list[dict[str, Any]],
    dataset: str,
    *,
    clean_dir: str | Path,
    report: IngestReport | None = None,
    reports_dir: str | Path | None = None,
) -> dict[str, str]:
    """Write cleaned CSV + NDJSON (and optional report JSON). Creates dirs.
    NEVER writes to the main DB."""
    out: dict[str, str] = {}
    cdir = Path(clean_dir) / dataset
    cdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    csv_path = cdir / f"{dataset}_clean_{stamp}.csv"
    ndjson_path = cdir / f"{dataset}_clean_{stamp}.ndjson"

    if clean_rows:
        # Stable union of keys.
        keys: list[str] = []
        for r in clean_rows:
            for k in r:
                if k not in keys:
                    keys.append(k)
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            for r in clean_rows:
                w.writerow({k: r.get(k, "") for k in keys})
        with ndjson_path.open("w", encoding="utf-8") as fh:
            for r in clean_rows:
                fh.write(json.dumps(r, default=str) + "\n")
        out["clean_csv"] = str(csv_path)
        out["clean_ndjson"] = str(ndjson_path)

    if report is not None and reports_dir is not None:
        rdir = Path(reports_dir)
        rdir.mkdir(parents=True, exist_ok=True)
        rjson = rdir / f"{dataset}_ingest_report_{stamp}.json"
        rjson.write_text(json.dumps(report.as_dict(), indent=2, default=str), encoding="utf-8")
        out["report_json"] = str(rjson)
    return out


def ingest_file_or_dir(
    dataset: str,
    *,
    input_path: str | None = None,
    input_dir: str | None = None,
    clean_dir: str | None = None,
    reports_dir: str | None = None,
    write: bool = True,
) -> IngestReport:
    """High-level entry: read -> validate -> (optionally) write clean."""
    rows: list[dict[str, Any]] = []
    inputs: list[str] = []
    if input_path:
        r, _ = read_rows(input_path)
        rows.extend(r)
        if r:
            inputs.append(Path(input_path).name)
    if input_dir:
        r, used = read_input_dir(input_dir)
        rows.extend(r)
        inputs.extend(used)

    if dataset == "auto" and rows:
        dataset = detect_dataset_type(rows[0]) or "unknown"

    report, clean = ingest_rows(rows, dataset)
    report.inputs = inputs

    if write and clean and clean_dir:
        paths = write_clean_outputs(
            clean, dataset, clean_dir=clean_dir, report=report, reports_dir=reports_dir)
        report.output_clean_csv = paths.get("clean_csv", "")
        report.output_clean_ndjson = paths.get("clean_ndjson", "")
        report.output_report_json = paths.get("report_json", "")
    return report
