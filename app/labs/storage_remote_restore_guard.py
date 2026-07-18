"""Disk-pressure and remote-restore guards for research storage.

Remote verification is opt-in, uses the existing Data Vault S3-compatible
configuration, and never deletes raw evidence. An upload is not considered a
backup verification until the object has been downloaded and its logical
contents have been replay-checked in an isolated temporary directory.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from app.data_vault import DataVaultExternalStorage, _external_configured

from .cross_venue import REPO_ROOT, STAGING_ROOT
from .storage_efficiency_v2 import (
    ANALYTICS_MANIFEST_PATH,
    FEATURE_MANIFEST_PATH,
    STATUS_PATH as STORAGE_STATUS_PATH,
    _canonical_row,
    _file_sha256,
    _iter_compressed_lines,
    _read_json,
    load_storage_config,
    storage_status,
)


RUNTIME_ROOT = REPO_ROOT / "data" / "runtime" / "storage_efficiency_v2"
DISK_GUARD_PATH = RUNTIME_ROOT / "disk_guard_status.json"
REMOTE_RESTORE_PATH = RUNTIME_ROOT / "remote_restore_status.json"
RESTORE_TEMP_ROOT = RUNTIME_ROOT / "remote_restore_tmp"

GIB = 1024 ** 3
WARNING_FREE_BYTES = 10 * GIB
CRITICAL_FREE_BYTES = 7 * GIB
ABSOLUTE_FREE_BYTES = 5 * GIB
INFO_ETA_HOURS = 12.0


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("STORAGE_GUARD_SYMLINK_BLOCKED")
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def research_safety() -> dict[str, Any]:
    return {
        "research_only": True, "simulation_only": True,
        "paper_filter_enabled": False, "can_send_real_orders": False,
        "raw_deleted": False, "delete_allowed": False,
        "retention_mode": "COMPRESSION_ONLY_NO_DELETE",
        "human_approval_required_for_retention": True,
        "final_recommendation": "NO LIVE",
    }


def disk_guard_status(
    *, free_bytes: int | None = None, eta_to_guard_hours: float | None = None,
    status_payload: dict[str, Any] | None = None, write: bool = False,
) -> dict[str, Any]:
    status = status_payload if status_payload is not None else storage_status()
    if free_bytes is None:
        free_bytes = int(status.get("free_disk_bytes") or shutil.disk_usage(REPO_ROOT).free)
    if eta_to_guard_hours is None:
        value = status.get("eta_to_guard_hours")
        try:
            eta_to_guard_hours = float(value) if value is not None else None
        except (TypeError, ValueError):
            eta_to_guard_hours = None
    if free_bytes < ABSOLUTE_FREE_BYTES:
        level = "ABSOLUTE_PROTECTION"
    elif free_bytes < CRITICAL_FREE_BYTES:
        level = "CRITICAL"
    elif free_bytes < WARNING_FREE_BYTES:
        level = "WARNING"
    elif eta_to_guard_hours is not None and math.isfinite(eta_to_guard_hours) and eta_to_guard_hours < INFO_ETA_HOURS:
        level = "INFO"
    else:
        level = "OK"
    result = {
        "schema": "research_storage_disk_guard.v1", "generated_at": utc_now(),
        "level": level, "free_disk_bytes": int(free_bytes),
        "eta_to_guard_hours": eta_to_guard_hours,
        "thresholds": {
            "info_eta_hours": INFO_ETA_HOURS,
            "warning_free_bytes": WARNING_FREE_BYTES,
            "critical_free_bytes": CRITICAL_FREE_BYTES,
            "absolute_free_bytes": ABSOLUTE_FREE_BYTES,
        },
        "allow_challenger": level in {"OK", "INFO"},
        "allow_heavy_research": level == "OK",
        "allow_new_derived_work": level in {"OK", "INFO", "WARNING"},
        "prioritize_compression": level in {"INFO", "WARNING", "CRITICAL", "ABSOLUTE_PROTECTION"},
        "prioritize_remote_upload": level in {"WARNING", "CRITICAL", "ABSOLUTE_PROTECTION"},
        "cooperative_collector_stop_required": level == "ABSOLUTE_PROTECTION",
        "flush_and_close_required": level == "ABSOLUTE_PROTECTION",
        "compression_queue": int(status.get("compression_queue") or 0),
        "analytics_queue": int(status.get("analytics_queue") or 0),
        **research_safety(),
    }
    if write:
        _atomic_json(DISK_GUARD_PATH, result)
    return result


def _select_candidate(
    analytics_path: Path = ANALYTICS_MANIFEST_PATH,
    features_path: Path = FEATURE_MANIFEST_PATH,
    source_id: str | None = None,
) -> dict[str, Any] | None:
    analytics = _read_json(analytics_path, {"segments": {}}) or {}
    features = _read_json(features_path, {"segments": {}}) or {}
    candidates: list[dict[str, Any]] = []
    for key, record in sorted((analytics.get("segments") or {}).items()):
        if source_id and key != source_id:
            continue
        feature = (features.get("segments") or {}).get(key) or {}
        if record.get("status") != "VERIFIED_PARQUET" or feature.get("status") != "VERIFIED_FEATURES":
            continue
        relative = str(record.get("source_path") or "")
        source = STAGING_ROOT / relative
        resolved = source.resolve(strict=False)
        root = STAGING_ROOT.resolve(strict=False)
        if root not in resolved.parents or source.is_symlink() or not source.is_file():
            continue
        actual = _file_sha256(source)
        if actual != record.get("source_compressed_sha256"):
            continue
        candidates.append({
            "source_partition_id": key, "source": source,
            "source_path": relative, "compressed_sha256": actual,
            "logical_sha256": record.get("source_logical_sha256"),
            "rows": int(record.get("source_rows") or 0),
            "compressed_bytes": source.stat().st_size,
            "parquet_outputs": len(record.get("outputs") or []),
            "feature_outputs": len(feature.get("outputs") or []),
        })
    return min(candidates, key=lambda item: item["compressed_bytes"]) if candidates else None


class _RemoteBackend:
    """Small wrapper around the existing Data Vault client/configuration."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.storage = DataVaultExternalStorage(config)

    @property
    def configured(self) -> bool:
        return bool(_external_configured(self.config))

    def upload(self, path: Path, object_key: str, metadata: dict[str, str]) -> dict[str, Any]:
        try:
            import boto3  # type: ignore

            client = self.storage._client(boto3)
            client.upload_file(
                str(path), self.config.data_vault_external_bucket, object_key,
                ExtraArgs={"Metadata": metadata},
            )
            head = client.head_object(Bucket=self.config.data_vault_external_bucket, Key=object_key)
            return {
                "uploaded": True, "object_key": object_key,
                "size": int(head.get("ContentLength") or 0),
                "etag": str(head.get("ETag") or "").strip('"'),
                "metadata": dict(head.get("Metadata") or {}),
            }
        except Exception as exc:
            return {"uploaded": False, "object_key": object_key, "error": type(exc).__name__}

    def download(self, object_key: str, target: Path) -> dict[str, Any]:
        try:
            import boto3  # type: ignore

            client = self.storage._client(boto3)
            client.download_file(self.config.data_vault_external_bucket, object_key, str(target))
            return {"downloaded": True, "bytes": target.stat().st_size}
        except Exception as exc:
            return {"downloaded": False, "error": type(exc).__name__}


def _restore_replay(source: Path, candidate: dict[str, Any], temp_root: Path) -> dict[str, Any]:
    logical = hashlib.sha256()
    rows = 0
    first_ts: int | None = None
    last_ts: int | None = None
    first_sequence: str | None = None
    last_sequence: str | None = None
    event_counts: dict[str, int] = {}
    parquet_path = temp_root / "reconstructed.parquet"
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except ImportError:
        return {"status": "NEED_DEPENDENCY", "reason": "PYARROW_NOT_INSTALLED"}
    writer = None
    batch: list[dict[str, Any]] = []
    try:
        for index, raw_line in enumerate(_iter_compressed_lines(source)):
            if not raw_line.endswith(b"\n"):
                raise ValueError("REMOTE_RESTORE_PARTIAL_JSONL")
            logical.update(raw_line)
            raw = json.loads(raw_line)
            if not isinstance(raw, dict):
                raise ValueError("REMOTE_RESTORE_NON_OBJECT_JSONL")
            canonical = _canonical_row(
                raw, str(candidate["source_partition_id"]),
                str(candidate.get("logical_sha256") or ""), index,
                raw_line[:-1].decode("utf-8"),
            )
            timestamp = int(canonical.get("local_receive_wall_ms") or 0)
            if timestamp <= 0:
                raise ValueError("REMOTE_RESTORE_TIMESTAMP_REQUIRED")
            first_ts = timestamp if first_ts is None else min(first_ts, timestamp)
            last_ts = timestamp if last_ts is None else max(last_ts, timestamp)
            sequence = str(canonical.get("sequence_id") or "") or None
            first_sequence = first_sequence or sequence
            last_sequence = sequence or last_sequence
            event = str(canonical.get("event_type") or "unknown")
            event_counts[event] = event_counts.get(event, 0) + 1
            batch.append(canonical)
            rows += 1
            if len(batch) >= 25_000:
                table = pa.Table.from_pylist(batch)
                if writer is None:
                    writer = pq.ParquetWriter(parquet_path, table.schema, compression="zstd")
                writer.write_table(table)
                batch.clear()
        if batch:
            table = pa.Table.from_pylist(batch)
            if writer is None:
                writer = pq.ParquetWriter(parquet_path, table.schema, compression="zstd")
            writer.write_table(table)
            batch.clear()
    finally:
        if writer is not None:
            writer.close()
    logical_ok = logical.hexdigest() == candidate.get("logical_sha256")
    rows_ok = rows == int(candidate.get("rows") or -1)
    parquet_rows = 0
    if parquet_path.is_file():
        parquet_rows = int(pq.ParquetFile(parquet_path).metadata.num_rows)
    return {
        "status": "PASS" if logical_ok and rows_ok and parquet_rows == rows else "FAIL",
        "logical_sha256": logical.hexdigest(), "logical_sha256_match": logical_ok,
        "rows": rows, "rows_match": rows_ok, "parquet_rows": parquet_rows,
        "parquet_reconstructed": parquet_rows == rows and rows > 0,
        "features_replay_ready": False,
        "feature_rebuild_status": "NOT_RUN_REQUIRES_ISOLATED_FEATURE_PIPELINE",
        "event_counts": event_counts, "first_timestamp_ms": first_ts,
        "last_timestamp_ms": last_ts, "first_sequence": first_sequence,
        "last_sequence": last_sequence,
    }


def remote_restore_status() -> dict[str, Any]:
    value = _read_json(REMOTE_RESTORE_PATH, {}) or {}
    if value:
        return value
    return {
        "status": "BLOCKED_R2_CONFIG_UNAVAILABLE",
        "blockers": ["R2_CONFIG_UNAVAILABLE"], "remote_restore_verified": False,
        "verified_remote_restorable_partitions": 0, **research_safety(),
    }


def verify_remote_restore(
    *, apply: bool = False, source_id: str | None = None,
    backend: Any | None = None, temp_root: Path = RESTORE_TEMP_ROOT,
    write: bool = True,
) -> dict[str, Any]:
    candidate = _select_candidate(source_id=source_id)
    base = {
        "schema": "storage_remote_restore_verification.v1", "generated_at": utc_now(),
        "mode": "APPLY" if apply else "DRY_RUN", "candidate": (
            {key: value for key, value in candidate.items() if key != "source"}
            if candidate else None
        ),
        "remote_restore_verified": False,
        "verified_remote_restorable_partitions": 0,
        **research_safety(),
    }
    if candidate is None:
        result = {**base, "status": "NEED_MORE_DATA", "blockers": ["NO_VERIFIED_COMPRESSED_PARQUET_FEATURE_CANDIDATE"]}
        if write:
            _atomic_json(REMOTE_RESTORE_PATH, result)
        return result
    if not apply:
        result = {**base, "status": "DRY_RUN", "blockers": ["APPLY_REQUIRED_FOR_REMOTE_ROUNDTRIP"]}
        if write:
            _atomic_json(REMOTE_RESTORE_PATH, result)
        return result
    if backend is None:
        try:
            from app.config import load_config

            config = load_config()
            backend = _RemoteBackend(config)
        except Exception as exc:
            result = {**base, "status": "BLOCKED_R2_CONFIG_UNAVAILABLE", "blockers": ["R2_CONFIG_UNAVAILABLE"], "error": type(exc).__name__}
            if write:
                _atomic_json(REMOTE_RESTORE_PATH, result)
            return result
    if not bool(getattr(backend, "configured", False)):
        result = {**base, "status": "BLOCKED_R2_CONFIG_UNAVAILABLE", "blockers": ["R2_CONFIG_UNAVAILABLE"]}
        if write:
            _atomic_json(REMOTE_RESTORE_PATH, result)
        return result
    guard = disk_guard_status()
    required_free = max(ABSOLUTE_FREE_BYTES, int(candidate["compressed_bytes"]) * 4)
    if int(guard["free_disk_bytes"]) <= required_free:
        result = {**base, "status": "BLOCKED_DISK_GUARD", "blockers": ["REMOTE_RESTORE_TEMP_SPACE_INSUFFICIENT"], "disk_guard": guard}
        if write:
            _atomic_json(REMOTE_RESTORE_PATH, result)
        return result
    object_key = (
        "bitget-ai-trading-bot/storage-restore-v1/"
        f"{str(candidate['source_partition_id']).replace(':', '_')}/"
        f"{candidate['compressed_sha256']}{candidate['source'].suffix}"
    )
    metadata = {
        "compressed-sha256": str(candidate["compressed_sha256"]),
        "logical-sha256": str(candidate.get("logical_sha256") or ""),
        "source-id-sha256": hashlib.sha256(str(candidate["source_partition_id"]).encode("utf-8")).hexdigest(),
    }
    temp_root.mkdir(parents=True, exist_ok=True)
    if temp_root.is_symlink():
        raise ValueError("STORAGE_GUARD_TEMP_ROOT_SYMLINK_BLOCKED")
    with tempfile.TemporaryDirectory(prefix="r2-restore-", dir=temp_root) as temp_name:
        upload = backend.upload(candidate["source"], object_key, metadata)
        if not upload.get("uploaded"):
            result = {**base, "status": "REMOTE_UPLOAD_FAILED", "blockers": ["R2_UPLOAD_FAILED"], "upload": upload}
        else:
            restored = Path(temp_name) / candidate["source"].name
            download = backend.download(object_key, restored)
            physical_ok = bool(download.get("downloaded")) and _file_sha256(restored) == candidate["compressed_sha256"]
            replay = _restore_replay(restored, candidate, Path(temp_name)) if physical_ok else {"status": "NOT_RUN"}
            verified = bool(
                physical_ok and replay.get("status") == "PASS"
                and int(upload.get("size") or 0) == int(candidate["compressed_bytes"])
            )
            result = {
                **base, "status": "VERIFIED_REMOTE_RESTORABLE" if verified else "REMOTE_RESTORE_FAILED",
                "blockers": [] if verified else ["REMOTE_ROUNDTRIP_NOT_EQUIVALENT"],
                "upload": upload, "download": download,
                "physical_sha256_match": physical_ok, "replay": replay,
                "remote_restore_verified": verified,
                "verified_remote_restorable_partitions": 1 if verified else 0,
                "retention_candidate_eligible": verified,
                "delete_allowed": False,
            }
    if write:
        _atomic_json(REMOTE_RESTORE_PATH, result)
    return result
