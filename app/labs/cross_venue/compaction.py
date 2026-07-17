"""Offline derived Parquet compaction for Cross-Venue research partitions.

This job is deliberately separate from collectors and never deletes or mutates
the append-only JSONL audit source.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import safety_envelope
from .providers import load_config
from .storage import atomic_json, safe_staging_root

_TOKEN = re.compile(r"^[A-Za-z0-9_]+$")


def _safe_token(value: str, label: str) -> str:
    token = str(value).strip()
    if not token or not _TOKEN.fullmatch(token):
        raise ValueError(f"CROSS_VENUE_COMPACTION_INVALID_{label.upper()}")
    return token


def _contained(root: Path, path: Path) -> Path:
    root_resolved = root.resolve(strict=False)
    resolved = path.resolve(strict=False)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError("CROSS_VENUE_COMPACTION_PATH_ESCAPE")
    cursor = path
    while cursor != root:
        if cursor.exists() and cursor.is_symlink():
            raise ValueError("CROSS_VENUE_COMPACTION_SYMLINK_BLOCKED")
        cursor = cursor.parent
    return path


def compact_partition(
    *, venue: str, symbol: str, event_type: str, date: str,
    root: Path | str | None = None, batch_rows: int = 50_000,
) -> dict[str, Any]:
    """Create one derived Parquet partition while preserving JSONL source."""
    safe_root = safe_staging_root(root)
    venue = _safe_token(venue.lower(), "venue")
    symbol = _safe_token(symbol.upper(), "symbol")
    event_type = _safe_token(event_type.lower(), "event_type")
    try:
        parsed_date = datetime.strptime(str(date), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("CROSS_VENUE_COMPACTION_INVALID_DATE") from exc
    if venue not in set(load_config().get("active_venues", [])):
        raise ValueError("CROSS_VENUE_COMPACTION_VENUE_NOT_ACTIVE")
    source = _contained(
        safe_root,
        safe_root / venue / "normalized" / symbol / event_type / parsed_date / "events.jsonl",
    )
    output = _contained(
        safe_root,
        safe_root / "derived" / "parquet" / venue / symbol / event_type / parsed_date / "events.parquet",
    )
    status_path = _contained(safe_root, safe_root / "derived" / "compaction_status.json")
    base = {
        "schema": "cross_venue_compaction.v1",
        "venue": venue,
        "symbol": symbol,
        "event_type": event_type,
        "date": parsed_date,
        "source": str(source),
        "output": str(output),
        "hot_path": False,
        "raw_deleted": False,
        "raw_mutated": False,
        **safety_envelope(),
    }
    if not source.is_file() or source.is_symlink():
        result = {**base, "status": "NEED_DATA", "reason": "SOURCE_PARTITION_MISSING"}
        atomic_json(status_path, result)
        return result
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except ImportError:
        result = {**base, "status": "NEED_DEPENDENCY", "reason": "PYARROW_NOT_INSTALLED"}
        atomic_json(status_path, result)
        return result
    batch_rows = max(1, min(int(batch_rows), 250_000))
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f"{output.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    writer = None
    row_count = 0
    source_hash = hashlib.sha256()
    try:
        batch: list[dict[str, Any]] = []
        with source.open("rb") as handle:
            for raw_line in handle:
                if not raw_line.endswith(b"\n"):
                    raise ValueError("CROSS_VENUE_COMPACTION_PARTIAL_JSONL_LINE")
                source_hash.update(raw_line)
                try:
                    row = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise ValueError("CROSS_VENUE_COMPACTION_CORRUPT_JSONL") from exc
                if not isinstance(row, dict):
                    raise ValueError("CROSS_VENUE_COMPACTION_NON_OBJECT_ROW")
                batch.append(row)
                if len(batch) >= batch_rows:
                    table = pa.Table.from_pylist(batch)
                    writer = writer or pq.ParquetWriter(tmp, table.schema, compression="zstd")
                    writer.write_table(table)
                    row_count += len(batch)
                    batch.clear()
            if batch:
                table = pa.Table.from_pylist(batch)
                writer = writer or pq.ParquetWriter(tmp, table.schema, compression="zstd")
                writer.write_table(table)
                row_count += len(batch)
        if writer is None or row_count == 0:
            raise ValueError("CROSS_VENUE_COMPACTION_EMPTY_SOURCE")
        writer.close()
        writer = None
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, output)
        output_hash = hashlib.sha256(output.read_bytes()).hexdigest()
        result = {
            **base,
            "status": "COMPLETED",
            "row_count": row_count,
            "source_sha256": source_hash.hexdigest(),
            "output_sha256": output_hash,
            "compression": "zstd",
            "completed_at": datetime.utcnow().isoformat() + "Z",
        }
        atomic_json(status_path, result)
        return result
    except Exception as exc:
        if writer is not None:
            writer.close()
        tmp.unlink(missing_ok=True)
        result = {**base, "status": "ERROR", "error": f"{type(exc).__name__}:{str(exc)[:240]}"}
        atomic_json(status_path, result)
        return result
    finally:
        tmp.unlink(missing_ok=True)
