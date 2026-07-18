"""Layered, lossless storage optimization for Cross-Venue research data.

The module is deliberately outside collector and trading hot paths.  It may
compress closed files and create verified analytics derivatives, but it never
deletes raw audit data, changes a policy, opens an exchange connection, or
promotes a strategy.
"""

from __future__ import annotations

import ctypes
import gzip
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .cross_venue import REPO_ROOT, STAGING_ROOT, safety_envelope

TOOL_VERSION = "STORAGE_EFFICIENCY_V2"
CONFIG_PATH = REPO_ROOT / "config" / "research" / "STORAGE_EFFICIENCY_V2.json"
RUNTIME_ROOT = REPO_ROOT / "data" / "runtime" / "storage_efficiency_v2"
REPORT_ROOT = REPO_ROOT / "reports" / "research" / "storage_efficiency_v2"
ANALYTICS_ROOT = STAGING_ROOT / "derived" / "analytics_v2"
FEATURE_ROOT = STAGING_ROOT / "derived" / "features_v2"
MANIFEST_PATH = RUNTIME_ROOT / "storage_manifest.json"
STATUS_PATH = RUNTIME_ROOT / "storage_status.json"
ANALYTICS_MANIFEST_PATH = RUNTIME_ROOT / "analytics_manifest.json"
FEATURE_MANIFEST_PATH = RUNTIME_ROOT / "feature_manifest.json"
UTC_DATE = "%Y-%m-%d"

STRING_FIELDS = (
    "venue", "symbol", "canonical_symbol", "product_type", "quote_asset",
    "event_type", "local_receive_wall_ts", "sequence_id", "trade_id",
    "taker_side", "connection_id", "source_status", "snapshot_kind",
    "raw_schema", "event_id",
)
INT_FIELDS = (
    "exchange_event_ts", "exchange_publish_ts", "local_receive_wall_ms",
    "local_receive_monotonic_ns", "reconnect_count",
)
FLOAT_FIELDS = (
    "local_wall_minus_monotonic_ms", "price", "size", "best_bid",
    "best_ask", "bid_size", "ask_size", "mark_price", "index_price",
    "funding_rate", "open_interest",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_storage_config(path: Path | str | None = None) -> dict[str, Any]:
    target = Path(path) if path else CONFIG_PATH
    value = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("mode") != "COMPRESSION_ONLY_NO_DELETE":
        raise ValueError("STORAGE_EFFICIENCY_UNSAFE_MODE")
    if value.get("no_delete_without_verified_remote_backup") is not True:
        raise ValueError("STORAGE_EFFICIENCY_DELETE_GUARD_REQUIRED")
    if value.get("can_send_real_orders") is not False:
        raise ValueError("STORAGE_EFFICIENCY_REAL_ORDERS_BLOCKED")
    if value.get("paper_filter_enabled") is not False:
        raise ValueError("STORAGE_EFFICIENCY_PAPER_FILTER_BLOCKED")
    if value.get("final_recommendation") != "NO LIVE":
        raise ValueError("STORAGE_EFFICIENCY_NO_LIVE_REQUIRED")
    for key in (
        "normalized_hot_max_bytes", "raw_hot_max_bytes",
        "compression_worker_interval_seconds", "minimum_free_disk_bytes",
    ):
        try:
            number = int(value[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"STORAGE_EFFICIENCY_CONFIG_INVALID:{key}") from exc
        if number <= 0:
            raise ValueError(f"STORAGE_EFFICIENCY_CONFIG_INVALID:{key}")
    return value


def research_safety() -> dict[str, Any]:
    return {
        **safety_envelope(),
        "storage_mode": "COMPRESSION_ONLY_NO_DELETE",
        "no_delete_without_verified_remote_backup": True,
        "delete_allowed": False,
        "raw_deleted": False,
        "raw_mutated": False,
        "activation": "research_storage_only",
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("STORAGE_EFFICIENCY_SYMLINK_BLOCKED")
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return value


def _contained(root: Path, path: Path) -> Path:
    root_resolved = root.resolve(strict=False)
    resolved = path.resolve(strict=False)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError("STORAGE_EFFICIENCY_PATH_ESCAPE")
    cursor = path
    while cursor != root:
        if cursor.exists() and cursor.is_symlink():
            raise ValueError("STORAGE_EFFICIENCY_SYMLINK_BLOCKED")
        cursor = cursor.parent
    if root.exists() and root.is_symlink():
        raise ValueError("STORAGE_EFFICIENCY_ROOT_SYMLINK_BLOCKED")
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _physical_bytes(path: Path) -> int:
    """Best-effort allocated size, including transparent NTFS compression."""
    if os.name != "nt":
        stat = path.stat()
        return int(getattr(stat, "st_blocks", 0) * 512 or stat.st_size)
    high = ctypes.c_ulong(0)
    low = ctypes.windll.kernel32.GetCompressedFileSizeW(str(path), ctypes.byref(high))
    if low == 0xFFFFFFFF and ctypes.GetLastError() != 0:
        return path.stat().st_size
    return int((high.value << 32) | low)


def _is_ntfs_compressed(path: Path) -> bool:
    if os.name != "nt":
        return False
    return bool(path.stat().st_file_attributes & 0x800)


def _closed_jsonl_candidates(root: Path = STAGING_ROOT) -> list[Path]:
    safe_root = _contained(STAGING_ROOT, Path(root))
    today = datetime.now(timezone.utc).strftime(UTC_DATE)
    candidates: list[Path] = []
    for path in safe_root.rglob("*.jsonl"):
        if not path.is_file() or path.is_symlink() or path.name == "current.jsonl":
            continue
        relative = path.relative_to(safe_root)
        if "consumed_hot_segments" in relative.parts:
            candidates.append(path)
            continue
        date_tokens = [part for part in relative.parts if len(part) == 10 and part[4:5] == "-" and part[7:8] == "-"]
        if any(token < today for token in date_tokens):
            candidates.append(path)
            continue
        if path.name.startswith("frames_"):
            candidates.append(path)
    return sorted(set(candidates))


def _line_contract(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    rows = 0
    first: dict[str, Any] | None = None
    last: dict[str, Any] | None = None
    final = b""
    with path.open("rb") as handle:
        for raw_line in handle:
            final = raw_line[-1:]
            if not raw_line.endswith(b"\n"):
                raise ValueError("STORAGE_EFFICIENCY_PARTIAL_JSONL_LINE")
            digest.update(raw_line)
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError("STORAGE_EFFICIENCY_CORRUPT_JSONL") from exc
            if not isinstance(value, dict):
                raise ValueError("STORAGE_EFFICIENCY_NON_OBJECT_JSONL")
            rows += 1
            first = value if first is None else first
            last = value
    if rows == 0 or final != b"\n":
        raise ValueError("STORAGE_EFFICIENCY_EMPTY_OR_PARTIAL_JSONL")
    return {
        "logical_bytes": path.stat().st_size,
        "rows": rows,
        "sha256": digest.hexdigest(),
        "first_timestamp_ms": _event_timestamp(first or {}),
        "last_timestamp_ms": _event_timestamp(last or {}),
        "first_sequence": _event_sequence(first or {}),
        "last_sequence": _event_sequence(last or {}),
    }


def _event_timestamp(row: dict[str, Any]) -> int | None:
    for key in ("local_receive_wall_ms", "exchange_event_ts", "received_at_ms"):
        try:
            value = int(row.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _event_sequence(row: dict[str, Any]) -> str | None:
    value = row.get("sequence_id") or row.get("sequence") or row.get("trade_id") or row.get("event_id")
    return str(value) if value not in (None, "") else None


def compress_closed_partitions(
    *, root: Path | str = STAGING_ROOT, apply: bool = False,
    max_files: int | None = None,
) -> dict[str, Any]:
    """Apply transparent NTFS compression to closed files only.

    Logical bytes, paths and contents remain unchanged.  On non-Windows hosts
    the function is an honest dry-run because transparent compression is not a
    portable contract.
    """
    safe_root = _contained(STAGING_ROOT, Path(root))
    config = load_storage_config()
    manifest = _read_json(MANIFEST_PATH, {"schema": "storage_efficiency_v2.manifest.v1", "partitions": {}})
    if not isinstance(manifest, dict) or not isinstance(manifest.get("partitions"), dict):
        raise ValueError("STORAGE_EFFICIENCY_MANIFEST_INVALID")
    candidates = _closed_jsonl_candidates(safe_root)
    if max_files is not None:
        candidates = candidates[: max(0, int(max_files))]
    rows: list[dict[str, Any]] = []
    free_before = shutil.disk_usage(REPO_ROOT).free
    for path in candidates:
        relative = path.relative_to(safe_root).as_posix()
        before = {
            "logical_bytes": path.stat().st_size,
            "physical_bytes": _physical_bytes(path),
            "compressed": _is_ntfs_compressed(path),
        }
        previous = manifest["partitions"].get(relative)
        contract = previous if isinstance(previous, dict) and previous.get("sha256") else None
        if contract is None:
            contract = _line_contract(path)
        status = "ALREADY_COMPRESSED" if before["compressed"] else "DRY_RUN"
        error = None
        if apply and not before["compressed"]:
            if os.name != "nt":
                status = "NOT_SUPPORTED_ON_PLATFORM"
            else:
                completed = subprocess.run(
                    ["compact.exe", "/C", "/I", "/F", "/Q", str(path)],
                    cwd=REPO_ROOT, capture_output=True, text=True, timeout=900,
                )
                if completed.returncode != 0:
                    status = "ERROR"
                    error = (completed.stderr or completed.stdout or "compact failed")[-300:]
                else:
                    after_contract = _line_contract(path)
                    if any(after_contract[key] != contract[key] for key in ("logical_bytes", "rows", "sha256")):
                        raise RuntimeError("STORAGE_EFFICIENCY_COMPRESSION_EQUIVALENCE_FAILED")
                    status = "VERIFIED_TRANSPARENT_COMPRESSION"
        after = {
            "logical_bytes": path.stat().st_size,
            "physical_bytes": _physical_bytes(path),
            "compressed": _is_ntfs_compressed(path),
        }
        record = {
            **contract,
            "path": relative,
            "venue": relative.split("/", 1)[0],
            "status": status,
            "physical_bytes": after["physical_bytes"],
            "bytes_saved": max(0, after["logical_bytes"] - after["physical_bytes"]),
            "compression": "NTFS_TRANSPARENT" if after["compressed"] else "NONE",
            "verified_at": utc_now() if after["compressed"] else None,
            "error": error,
            "raw_audit_source_preserved": True,
        }
        manifest["partitions"][relative] = record
        rows.append({"before": before, "after": after, **record})
    manifest.update({"updated_at": utc_now(), "mode": config["mode"], **research_safety()})
    if apply or not MANIFEST_PATH.exists():
        _atomic_json(MANIFEST_PATH, manifest)
    free_after = shutil.disk_usage(REPO_ROOT).free
    result = {
        "schema": "storage_efficiency_v2.compression_cycle.v1",
        "generated_at": utc_now(),
        "apply": bool(apply),
        "candidate_files": len(rows),
        "verified_files": sum(row["after"]["compressed"] for row in rows),
        "failed_files": sum(row["status"] == "ERROR" for row in rows),
        "logical_bytes": sum(row["after"]["logical_bytes"] for row in rows),
        "physical_bytes": sum(row["after"]["physical_bytes"] for row in rows),
        "bytes_saved": sum(row["bytes_saved"] for row in rows),
        "free_before": free_before,
        "free_after": free_after,
        "rows": rows,
        **research_safety(),
    }
    return result


def benchmark_compression(
    source: Path | str, *, sample_sizes_mb: Iterable[int] = (64, 128, 256, 512),
) -> dict[str, Any]:
    """Compare portable codecs on bounded prefixes; temporary outputs are removed."""
    path = Path(source)
    if not path.is_file() or path.is_symlink():
        raise ValueError("STORAGE_EFFICIENCY_BENCHMARK_SOURCE_INVALID")
    sizes = sorted({max(1, min(int(value), 512)) for value in sample_sizes_mb})
    results: list[dict[str, Any]] = []
    try:
        import lz4.frame as lz4_frame  # type: ignore
    except ImportError:
        lz4_frame = None
    try:
        import zstandard as zstd  # type: ignore
    except ImportError:
        zstd = None
    with tempfile.TemporaryDirectory(prefix="storage-v2-", dir=REPORT_ROOT) as temp_name:
        temp = Path(temp_name)
        for size_mb in sizes:
            limit = min(path.stat().st_size, size_mb * 1024 * 1024)
            with path.open("rb") as handle:
                sample = handle.read(limit)
            codecs: list[tuple[str, Any]] = []
            for level in (1, 3, 5, 7):
                codecs.append((f"gzip_{level}", lambda data, level=level: gzip.compress(data, compresslevel=level, mtime=0)))
            if zstd is not None:
                for level in (1, 3, 5, 7):
                    codecs.append((f"zstd_{level}", lambda data, level=level: zstd.ZstdCompressor(level=level).compress(data)))
            if lz4_frame is not None:
                codecs.append(("lz4", lambda data: lz4_frame.compress(data)))
            for name, encode in codecs:
                started = time.perf_counter()
                compressed = encode(sample)
                compression_seconds = time.perf_counter() - started
                target = temp / f"{size_mb}_{name}.bin"
                target.write_bytes(compressed)
                started = time.perf_counter()
                if name.startswith("gzip"):
                    restored = gzip.decompress(compressed)
                elif name.startswith("zstd"):
                    restored = zstd.ZstdDecompressor().decompress(compressed)
                else:
                    restored = lz4_frame.decompress(compressed)
                decompression_seconds = time.perf_counter() - started
                if restored != sample:
                    raise RuntimeError("STORAGE_EFFICIENCY_CODEC_ROUNDTRIP_FAILED")
                results.append({
                    "sample_mb": size_mb,
                    "codec": name,
                    "input_bytes": len(sample),
                    "compressed_bytes": len(compressed),
                    "ratio": len(compressed) / max(1, len(sample)),
                    "compression_seconds": compression_seconds,
                    "compression_mb_s": len(sample) / 1024 / 1024 / max(compression_seconds, 1e-9),
                    "decompression_seconds": decompression_seconds,
                    "decompression_mb_s": len(sample) / 1024 / 1024 / max(decompression_seconds, 1e-9),
                    "roundtrip_sha256": hashlib.sha256(restored).hexdigest(),
                })
    by_size = defaultdict(list)
    for row in results:
        by_size[row["sample_mb"]].append(row)
    selected = min(
        (row for row in results if row["sample_mb"] == max(sizes)),
        key=lambda row: (row["ratio"] + 0.002 * row["compression_seconds"], row["compression_seconds"]),
    )
    return {
        "schema": "storage_efficiency_v2.compression_benchmark.v1",
        "generated_at": utc_now(),
        "source": str(path),
        "results": results,
        "selected_codec": selected["codec"],
        "selected_rotation_bytes": 256 * 1024 * 1024,
        "selection_note": "256MB balances recovery time, manifest overhead and bounded hot loss exposure",
        **research_safety(),
    }


def _arrow_schema():
    import pyarrow as pa  # type: ignore

    fields = [pa.field(name, pa.string()) for name in STRING_FIELDS]
    fields += [pa.field(name, pa.int64()) for name in INT_FIELDS]
    fields += [pa.field(name, pa.float64()) for name in FLOAT_FIELDS]
    fields += [
        pa.field("source_partition_id", pa.string()),
        pa.field("source_sha256", pa.string()),
    ]
    return pa.schema(fields)


def _canonical_row(row: dict[str, Any], source_id: str, source_sha: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in STRING_FIELDS:
        value = row.get(key)
        result[key] = None if value is None else str(value)
    for key in INT_FIELDS:
        value = row.get(key)
        try:
            result[key] = None if value is None else int(value)
        except (TypeError, ValueError):
            raise ValueError(f"STORAGE_EFFICIENCY_INTEGER_INVALID:{key}") from None
    for key in FLOAT_FIELDS:
        value = row.get(key)
        try:
            number = None if value is None else float(value)
        except (TypeError, ValueError):
            raise ValueError(f"STORAGE_EFFICIENCY_FLOAT_INVALID:{key}") from None
        if number is not None and not math.isfinite(number):
            raise ValueError(f"STORAGE_EFFICIENCY_NON_FINITE:{key}")
        result[key] = number
    result["source_partition_id"] = source_id
    result["source_sha256"] = source_sha
    return result


def _row_fingerprint(row: dict[str, Any]) -> bytes:
    keys = STRING_FIELDS + INT_FIELDS + FLOAT_FIELDS + ("source_partition_id", "source_sha256")
    payload = {key: row.get(key) for key in keys}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _verified_segments(root: Path = STAGING_ROOT) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for venue_dir in sorted(path for path in root.iterdir() if path.is_dir() and path.name != "derived"):
        manifest_path = venue_dir / "normalized" / "rollover_manifest.json"
        manifest = _read_json(manifest_path, {})
        for item in manifest.get("segments", []) if isinstance(manifest, dict) else []:
            if item.get("state") != "GZIP_VERIFIED_DERIVED_SEGMENT":
                continue
            relative = item.get("compressed_path")
            if not relative:
                continue
            source = _contained(root, venue_dir / str(relative))
            if not source.is_file() or source.is_symlink():
                continue
            rows.append({"venue": venue_dir.name, "source": source, **item})
    return sorted(rows, key=lambda row: (str(row.get("rotated_at") or ""), row["venue"]))


def compress_pending_rollover_segments(
    *, root: Path | str = STAGING_ROOT, apply: bool = False, max_segments: int = 2,
) -> dict[str, Any]:
    """Compress consumed normalized hot segments in a separate process.

    These segments are derived normalized copies.  Their exact gzip replacement
    is verified before the uncompressed derived copy is removed; the independent
    raw audit frames are never touched by this operation.
    """
    safe_root = _contained(STAGING_ROOT, Path(root))
    pending: list[tuple[str, Path, Path, dict[str, Any], Path]] = []
    for venue_dir in sorted(path for path in safe_root.iterdir() if path.is_dir() and path.name != "derived"):
        manifest_path = venue_dir / "normalized" / "rollover_manifest.json"
        manifest = _read_json(manifest_path, {})
        for row in manifest.get("segments", []) if isinstance(manifest, dict) else []:
            if row.get("state") != "CONSUMED_DERIVED_SEGMENT_PENDING_GZIP":
                continue
            source = _contained(safe_root, venue_dir / str(row.get("segment_path") or ""))
            if source.is_file() and not source.is_symlink():
                pending.append((venue_dir.name, source, source.with_suffix(source.suffix + ".gz"), row, manifest_path))
    pending = pending[: max(0, int(max_segments))]
    if not apply:
        return {"status": "DRY_RUN", "pending_segments": len(pending), "segments": [source.name for _, source, _, _, _ in pending], **research_safety()}
    completed = []
    for venue, source, target, row, manifest_path in pending:
        raw_digest = hashlib.sha256()
        rows = 0
        temp = target.with_name(f"{target.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
        try:
            with source.open("rb") as source_handle, temp.open("wb") as raw_target:
                with gzip.GzipFile(filename=source.name, mode="wb", fileobj=raw_target, compresslevel=3, mtime=0) as compressed:
                    for raw_line in source_handle:
                        if not raw_line.endswith(b"\n"):
                            raise ValueError("STORAGE_EFFICIENCY_PARTIAL_ROLLOVER_LINE")
                        raw_digest.update(raw_line)
                        rows += 1
                        compressed.write(raw_line)
                raw_target.flush()
                os.fsync(raw_target.fileno())
            verify = hashlib.sha256()
            verify_rows = 0
            with gzip.open(temp, "rb") as handle:
                for raw_line in handle:
                    verify.update(raw_line)
                    verify_rows += 1
            if verify.digest() != raw_digest.digest() or verify_rows != rows:
                raise RuntimeError("STORAGE_EFFICIENCY_GZIP_ROUNDTRIP_FAILED")
            os.replace(temp, target)
            compressed_sha = _file_sha256(target)
            manifest = _read_json(manifest_path, {})
            matched = False
            for current in manifest.get("segments", []) if isinstance(manifest, dict) else []:
                if current.get("segment_path") == row.get("segment_path"):
                    current.update({
                        "state": "GZIP_VERIFIED_DERIVED_SEGMENT",
                        "compressed_path": target.relative_to(manifest_path.parents[1]).as_posix(),
                        "raw_sha256": raw_digest.hexdigest(),
                        "compressed_sha256": compressed_sha,
                        "compressed_bytes": target.stat().st_size,
                        "row_count": rows,
                        "compressed_at": utc_now(),
                        "compression_worker": "STORAGE_EFFICIENCY_V2",
                        "raw_audit_sources_untouched": True,
                    })
                    matched = True
                    break
            if not matched:
                raise RuntimeError("STORAGE_EFFICIENCY_ROLLOVER_MANIFEST_ENTRY_MISSING")
            manifest["updated_at"] = utc_now()
            _atomic_json(manifest_path, manifest)
            source.unlink()
            completed.append({"venue": venue, "source": source.name, "compressed": target.name, "rows": rows, "raw_sha256": raw_digest.hexdigest(), "compressed_sha256": compressed_sha, "logical_bytes": int(row.get("stream_bytes") or 0), "compressed_bytes": target.stat().st_size})
        finally:
            temp.unlink(missing_ok=True)
    return {"status": "COMPLETED" if completed else "NO_NEW_SEGMENTS", "completed_segments": len(completed), "segments": completed, "raw_deleted": False, "derived_source_replaced_after_roundtrip": bool(completed), **research_safety()}


def compact_verified_segments(
    *, root: Path | str = STAGING_ROOT, apply: bool = False, max_segments: int = 1,
) -> dict[str, Any]:
    """Convert verified gzip rollover segments to partitioned Parquet exactly once."""
    safe_root = _contained(STAGING_ROOT, Path(root))
    manifest = _read_json(ANALYTICS_MANIFEST_PATH, {"schema": "storage_efficiency_v2.analytics_manifest.v1", "segments": {}})
    if not isinstance(manifest, dict) or not isinstance(manifest.get("segments"), dict):
        raise ValueError("STORAGE_EFFICIENCY_ANALYTICS_MANIFEST_INVALID")
    pending = []
    for item in _verified_segments(safe_root):
        source_id = f"{item['venue']}:{item['source'].name.removesuffix('.jsonl.gz')}"
        if manifest["segments"].get(source_id, {}).get("status") == "VERIFIED_PARQUET":
            continue
        pending.append((source_id, item))
    pending = pending[: max(0, int(max_segments))]
    if not apply:
        return {
            "schema": "storage_efficiency_v2.analytics_cycle.v1",
            "status": "DRY_RUN",
            "pending_segments": len(pending),
            "segments": [source_id for source_id, _ in pending],
            **research_safety(),
        }
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except ImportError:
        return {"status": "NEED_DEPENDENCY", "reason": "PYARROW_NOT_INSTALLED", **research_safety()}
    completed: list[dict[str, Any]] = []
    for source_id, item in pending:
        source: Path = item["source"]
        compressed_sha = _file_sha256(source)
        if item.get("compressed_sha256") and compressed_sha != item["compressed_sha256"]:
            raise RuntimeError("STORAGE_EFFICIENCY_COMPRESSED_SHA_MISMATCH")
        source_sha = str(item.get("raw_sha256") or "")
        run_token = uuid.uuid4().hex[:12]
        temp_root = ANALYTICS_ROOT / ".tmp" / f"{source_id.replace(':', '_')}_{run_token}"
        writers: dict[tuple[str, str, str], Any] = {}
        temp_paths: dict[tuple[str, str, str], Path] = {}
        counts: dict[tuple[str, str, str], int] = defaultdict(int)
        input_hashes: dict[tuple[str, str, str], Any] = defaultdict(hashlib.sha256)
        first_ts: dict[tuple[str, str, str], int | None] = {}
        last_ts: dict[tuple[str, str, str], int | None] = {}
        batches: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        logical_hash = hashlib.sha256()
        total = 0

        def flush(key: tuple[str, str, str]) -> None:
            batch = batches[key]
            if not batch:
                return
            table = pa.Table.from_pylist(batch, schema=_arrow_schema())
            writer = writers.get(key)
            if writer is None:
                venue, symbol, event_type = key
                date = datetime.fromtimestamp((first_ts[key] or 0) / 1000, timezone.utc).strftime(UTC_DATE)
                target = temp_root / venue / symbol / event_type / date / f"{source_id.split(':', 1)[1]}.parquet"
                target.parent.mkdir(parents=True, exist_ok=True)
                writer = pq.ParquetWriter(
                    target, table.schema, compression="zstd", compression_level=3,
                    use_dictionary=list(STRING_FIELDS), write_statistics=True,
                )
                writers[key] = writer
                temp_paths[key] = target
            writer.write_table(table, row_group_size=50_000)
            batch.clear()

        try:
            with gzip.open(source, "rb") as handle:
                for raw_line in handle:
                    if not raw_line.endswith(b"\n"):
                        raise ValueError("STORAGE_EFFICIENCY_PARTIAL_GZIP_JSONL")
                    logical_hash.update(raw_line)
                    try:
                        raw = json.loads(raw_line)
                    except json.JSONDecodeError as exc:
                        raise ValueError("STORAGE_EFFICIENCY_CORRUPT_GZIP_JSONL") from exc
                    if not isinstance(raw, dict):
                        raise ValueError("STORAGE_EFFICIENCY_NON_OBJECT_GZIP_JSONL")
                    row = _canonical_row(raw, source_id, source_sha)
                    venue = str(row.get("venue") or item["venue"]).lower()
                    symbol = str(row.get("canonical_symbol") or "UNKNOWN").upper()
                    event_type = str(row.get("event_type") or "unknown").lower()
                    key = (venue, symbol, event_type)
                    timestamp = int(row.get("local_receive_wall_ms") or 0)
                    first_ts.setdefault(key, timestamp)
                    last_ts[key] = timestamp
                    input_hashes[key].update(_row_fingerprint(row))
                    batches[key].append(row)
                    counts[key] += 1
                    total += 1
                    if len(batches[key]) >= 50_000:
                        flush(key)
            for key in list(batches):
                flush(key)
            for writer in writers.values():
                writer.close()
            writers.clear()
            if source_sha and logical_hash.hexdigest() != source_sha:
                raise RuntimeError("STORAGE_EFFICIENCY_LOGICAL_SHA_MISMATCH")
            outputs: list[dict[str, Any]] = []
            for key, temp_path in temp_paths.items():
                table = pq.read_table(temp_path)
                rows = table.to_pylist()
                output_hash = hashlib.sha256()
                for row in rows:
                    output_hash.update(_row_fingerprint(row))
                if len(rows) != counts[key] or output_hash.hexdigest() != input_hashes[key].hexdigest():
                    raise RuntimeError("STORAGE_EFFICIENCY_PARQUET_EQUIVALENCE_FAILED")
                venue, symbol, event_type = key
                date = datetime.fromtimestamp((first_ts[key] or 0) / 1000, timezone.utc).strftime(UTC_DATE)
                final_path = _contained(
                    STAGING_ROOT,
                    ANALYTICS_ROOT / venue / symbol / event_type / date / temp_path.name,
                )
                final_path.parent.mkdir(parents=True, exist_ok=True)
                if final_path.exists() and _file_sha256(final_path) != _file_sha256(temp_path):
                    raise RuntimeError("STORAGE_EFFICIENCY_PARQUET_TARGET_CONFLICT")
                os.replace(temp_path, final_path)
                outputs.append({
                    "path": final_path.relative_to(STAGING_ROOT).as_posix(),
                    "rows": len(rows),
                    "bytes": final_path.stat().st_size,
                    "sha256": _file_sha256(final_path),
                    "semantic_sha256": output_hash.hexdigest(),
                    "first_timestamp_ms": first_ts[key],
                    "last_timestamp_ms": last_ts[key],
                    "venue": venue,
                    "symbol": symbol,
                    "event_type": event_type,
                    "date": date,
                })
            record = {
                "status": "VERIFIED_PARQUET",
                "source_partition_id": source_id,
                "source_path": source.relative_to(STAGING_ROOT).as_posix(),
                "source_compressed_sha256": compressed_sha,
                "source_logical_sha256": logical_hash.hexdigest(),
                "source_rows": total,
                "outputs": outputs,
                "verified_at": utc_now(),
                "source_preserved": True,
            }
            manifest["segments"][source_id] = record
            completed.append(record)
        finally:
            for writer in writers.values():
                writer.close()
            shutil.rmtree(temp_root, ignore_errors=True)
    manifest.update({"updated_at": utc_now(), **research_safety()})
    _atomic_json(ANALYTICS_MANIFEST_PATH, manifest)
    return {
        "schema": "storage_efficiency_v2.analytics_cycle.v1",
        "status": "COMPLETED" if completed else "NO_NEW_SEGMENTS",
        "completed_segments": len(completed),
        "segments": completed,
        **research_safety(),
    }


def build_incremental_features(*, apply: bool = False, max_segments: int = 1) -> dict[str, Any]:
    """Materialize causal, event-driven features from verified Parquet only."""
    analytics = _read_json(ANALYTICS_MANIFEST_PATH, {"segments": {}})
    features = _read_json(FEATURE_MANIFEST_PATH, {"schema": "storage_efficiency_v2.feature_manifest.v1", "segments": {}})
    config = load_storage_config()
    pending = []
    for source_id, record in sorted((analytics.get("segments") or {}).items()):
        if record.get("status") != "VERIFIED_PARQUET":
            continue
        if (features.get("segments") or {}).get(source_id, {}).get("status") == "VERIFIED_FEATURES":
            continue
        pending.append((source_id, record))
    pending = pending[: max(0, int(max_segments))]
    if not apply:
        return {"status": "DRY_RUN", "pending_segments": len(pending), "segments": [row[0] for row in pending], **research_safety()}
    try:
        import duckdb  # type: ignore
    except ImportError:
        return {"status": "NEED_DEPENDENCY", "reason": "DUCKDB_NOT_INSTALLED", **research_safety()}
    completed = []
    horizons = [int(value) for value in config["materialized_feature_horizons_ms"]]
    for source_id, record in pending:
        paths = [STAGING_ROOT / output["path"] for output in record.get("outputs", [])]
        if not paths:
            continue
        dataset_hash = hashlib.sha256("".join(sorted(output["sha256"] for output in record["outputs"])).encode("ascii")).hexdigest()
        con = duckdb.connect(database=":memory:")
        output_rows = []
        try:
            path_sql = "[" + ",".join("'" + str(path).replace("'", "''") + "'" for path in paths) + "]"
            for horizon in horizons:
                target = _contained(STAGING_ROOT, FEATURE_ROOT / f"horizon_ms={horizon}" / f"{source_id.replace(':', '_')}.parquet")
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(f"{target.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
                query = f"""
                    SELECT
                      venue, canonical_symbol,
                      CAST(FLOOR(local_receive_wall_ms / {horizon}) * {horizon} AS BIGINT) AS bucket_start_ms,
                      MIN(local_receive_wall_ms) AS first_event_timestamp_ms,
                      MAX(local_receive_wall_ms) AS last_event_timestamp_ms,
                      MAX(local_receive_wall_ms) AS causal_cutoff_ms,
                      COUNT(*) AS event_count,
                      ARG_MIN(COALESCE((best_bid + best_ask) / 2.0, price, mark_price), local_receive_wall_ms) AS first_midpoint,
                      ARG_MAX(COALESCE((best_bid + best_ask) / 2.0, price, mark_price), local_receive_wall_ms) AS last_midpoint,
                      ARG_MAX(best_bid, local_receive_wall_ms) AS best_bid,
                      ARG_MAX(best_ask, local_receive_wall_ms) AS best_ask,
                      AVG(CASE WHEN best_bid > 0 AND best_ask >= best_bid THEN (best_ask-best_bid)/((best_ask+best_bid)/2.0)*10000 ELSE NULL END) AS spread_bps,
                      ARG_MAX(CASE WHEN bid_size + ask_size > 0 THEN (best_ask*bid_size + best_bid*ask_size)/(bid_size+ask_size) ELSE NULL END, local_receive_wall_ms) AS microprice,
                      ARG_MAX(CASE WHEN bid_size + ask_size > 0 THEN (bid_size-ask_size)/(bid_size+ask_size) ELSE NULL END, local_receive_wall_ms) AS book_imbalance,
                      SUM(CASE WHEN event_type='trade' AND taker_side='BUY' THEN COALESCE(size,0) ELSE 0 END) AS aggressive_buy_volume,
                      SUM(CASE WHEN event_type='trade' AND taker_side='SELL' THEN COALESCE(size,0) ELSE 0 END) AS aggressive_sell_volume,
                      COUNT(*) FILTER (WHERE event_type='trade') AS trade_count,
                      AVG(size) FILTER (WHERE event_type='trade') AS average_trade_size,
                      ARG_MAX(open_interest, local_receive_wall_ms)-ARG_MIN(open_interest, local_receive_wall_ms) AS open_interest_change,
                      ARG_MAX(funding_rate, local_receive_wall_ms) AS funding_rate,
                      ARG_MAX(mark_price-index_price, local_receive_wall_ms) AS mark_index_basis,
                      MAX(reconnect_count) AS reconnect_count,
                      MAX(CASE WHEN source_status IS NULL OR source_status='OK' THEN 0 ELSE 1 END) AS gap_flag,
                      '{source_id}' AS source_partition_id,
                      '{dataset_hash}' AS dataset_hash,
                      'storage_efficiency_v2.features.v1' AS feature_version,
                      '{utc_now()}' AS created_at
                    FROM read_parquet({path_sql}, union_by_name=true)
                    WHERE local_receive_wall_ms IS NOT NULL
                    GROUP BY venue, canonical_symbol, bucket_start_ms
                    ORDER BY canonical_symbol, bucket_start_ms, venue
                """
                con.execute(f"COPY ({query}) TO '{str(tmp).replace("'", "''")}' (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 3)")
                rows = int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{str(tmp).replace("'", "''")}')").fetchone()[0])
                os.replace(tmp, target)
                output_rows.append({"horizon_ms": horizon, "path": target.relative_to(STAGING_ROOT).as_posix(), "rows": rows, "bytes": target.stat().st_size, "sha256": _file_sha256(target)})
            item = {"status": "VERIFIED_FEATURES", "source_partition_id": source_id, "dataset_hash": dataset_hash, "outputs": output_rows, "created_at": utc_now(), "causal": True, "interpolation_used": False}
            features.setdefault("segments", {})[source_id] = item
            completed.append(item)
        finally:
            con.close()
    features.update({"updated_at": utc_now(), "available_on_demand_horizons_ms": config["available_on_demand_horizons_ms"], **research_safety()})
    _atomic_json(FEATURE_MANIFEST_PATH, features)
    return {"schema": "storage_efficiency_v2.feature_cycle.v1", "status": "COMPLETED" if completed else "NO_NEW_SEGMENTS", "completed_segments": len(completed), "segments": completed, **research_safety()}


def storage_status() -> dict[str, Any]:
    config = load_storage_config()
    disk = shutil.disk_usage(REPO_ROOT)
    logical = physical = 0
    categories = defaultdict(lambda: {"files": 0, "logical_bytes": 0, "physical_bytes": 0})
    for path in STAGING_ROOT.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(STAGING_ROOT)
        if relative.parts and relative.parts[0] == "derived":
            category = "features" if "features_v2" in relative.parts else "parquet" if path.suffix == ".parquet" else "derived_other"
        elif "raw" in relative.parts:
            category = "raw"
        elif path.name == "current.jsonl":
            category = "hot"
        elif path.suffix == ".gz":
            category = "warm_compressed"
        else:
            category = "normalized_derived"
        size = path.stat().st_size
        allocated = _physical_bytes(path)
        logical += size
        physical += allocated
        categories[category]["files"] += 1
        categories[category]["logical_bytes"] += size
        categories[category]["physical_bytes"] += allocated
    compression_manifest = _read_json(MANIFEST_PATH, {"partitions": {}})
    analytics_manifest = _read_json(ANALYTICS_MANIFEST_PATH, {"segments": {}})
    feature_manifest = _read_json(FEATURE_MANIFEST_PATH, {"segments": {}})
    pending_compression = sum(not _is_ntfs_compressed(path) for path in _closed_jsonl_candidates())
    pending_analytics = sum(
        f"{item['venue']}:{item['source'].name.removesuffix('.jsonl.gz')}" not in (analytics_manifest.get("segments") or {})
        for item in _verified_segments()
    )
    growth_per_hour = 0.0
    for venue in ("bitget", "binance", "bybit", "okx", "hyperliquid"):
        health = _read_json(STAGING_ROOT / venue / "health.json", {})
        growth_per_hour += float(health.get("stream_growth_bytes_per_hour_this_process") or 0)
    eta_guard = (disk.free - int(config["minimum_free_disk_bytes"])) / growth_per_hour if growth_per_hour > 0 else None
    eta_full = disk.free / growth_per_hour if growth_per_hour > 0 else None
    result = {
        "schema": "storage_efficiency_v2.status.v1",
        "generated_at": utc_now(),
        "mode": config["mode"],
        "raw_hot_bytes": categories["hot"]["logical_bytes"],
        "raw_logical_bytes": categories["raw"]["logical_bytes"],
        "raw_physical_bytes": categories["raw"]["physical_bytes"],
        "raw_compressed_bytes": categories["warm_compressed"]["logical_bytes"],
        "parquet_bytes": categories["parquet"]["logical_bytes"],
        "feature_store_bytes": categories["features"]["logical_bytes"],
        "logical_bytes": logical,
        "physical_bytes": physical,
        "bytes_saved": max(0, logical - physical),
        "compression_ratio": physical / logical if logical else None,
        "growth_bytes_per_hour": growth_per_hour,
        "growth_bytes_per_day": growth_per_hour * 24,
        "free_disk_bytes": disk.free,
        "minimum_free_disk_bytes": int(config["minimum_free_disk_bytes"]),
        "eta_to_guard_hours": eta_guard,
        "eta_to_full_hours": eta_full,
        "compression_queue": pending_compression,
        "analytics_queue": pending_analytics,
        "failed_partitions": sum(row.get("status") == "ERROR" for row in (compression_manifest.get("partitions") or {}).values()),
        "hash_verification": "PASS" if all(row.get("sha256") for row in (compression_manifest.get("partitions") or {}).values()) else "NEED_MORE_DATA",
        "manifest_status": "OK" if isinstance(compression_manifest.get("partitions"), dict) else "ERROR",
        "r2_verified": bool(config.get("r2_verified")),
        "delete_allowed": False,
        "categories": dict(categories),
        **research_safety(),
    }
    _atomic_json(STATUS_PATH, result)
    return result


def run_storage_cycle(*, apply: bool = False) -> dict[str, Any]:
    config = load_storage_config()
    compression = compress_closed_partitions(
        apply=apply, max_files=int(config["compression_max_files_per_cycle"]),
    )
    analytics = compact_verified_segments(
        apply=apply, max_segments=int(config["analytics_max_segments_per_cycle"]),
    )
    features = build_incremental_features(
        apply=apply, max_segments=int(config["analytics_max_segments_per_cycle"]),
    )
    status = storage_status()
    return {
        "schema": "storage_efficiency_v2.cycle.v1",
        "generated_at": utc_now(),
        "apply": bool(apply),
        "compression": compression,
        "analytics": analytics,
        "features": features,
        "status": status,
        **research_safety(),
    }
