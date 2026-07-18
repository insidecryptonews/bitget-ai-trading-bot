"""Fail-closed append-only storage for cross-venue research streams."""

from __future__ import annotations

import hashlib
import gzip
import json
import os
import shutil
import threading
import time
import ctypes
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from . import RUNTIME_ROOT, STAGING_ROOT, safety_envelope
from .providers import load_config


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True, default=str))
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.025 * (attempt + 1))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def safe_staging_root(root: Path | str | None = None) -> Path:
    expected = STAGING_ROOT
    target = Path(root) if root is not None else expected
    expected_parent = expected.parent.resolve()
    if expected.exists() and expected.is_symlink():
        raise ValueError("CROSS_VENUE_STAGING_ROOT_SYMLINK_BLOCKED")
    resolved = target.resolve(strict=False)
    expected_resolved = expected.resolve(strict=False)
    if resolved != expected_resolved:
        raise ValueError("CROSS_VENUE_STAGING_ROOT_OUTSIDE_ALLOWLIST")
    if resolved.parent != expected_parent:
        raise ValueError("CROSS_VENUE_STAGING_ROOT_PARENT_MISMATCH")
    cursor = target
    while cursor != cursor.parent:
        if cursor.exists() and cursor.is_symlink():
            raise ValueError("CROSS_VENUE_STAGING_SYMLINK_BLOCKED")
        if cursor.resolve(strict=False) == expected_parent:
            break
        cursor = cursor.parent
    return target


class StreamRolloverLock:
    """Cross-process mutex for stream cursors and derived spool rollover."""

    def __init__(self, root: Path):
        self.path = root / "locks" / "stream_rollover.guard"
        self._handle: Any | None = None

    def __enter__(self) -> "StreamRolloverLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("CROSS_VENUE_ROLLOVER_LOCK_SYMLINK_BLOCKED")
        self._handle = self.path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"0")
            self._handle.flush()
        self._handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def stream_rollover_lock(root: Path) -> StreamRolloverLock:
    return StreamRolloverLock(root)


def _json_line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode("utf-8")


class WriterLease:
    def __init__(self, root: Path, venue: str, *, ttl_seconds: int = 30):
        self.path = root / "locks" / f"{venue}.lock"
        self.venue = venue
        self.ttl_seconds = ttl_seconds
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    current = json.loads(self.path.read_text(encoding="utf-8"))
                    age = time.time() - float(current.get("heartbeat_epoch") or 0)
                except Exception:
                    age, current = self.ttl_seconds + 1, {}
                owner_pid = int(current.get("pid") or -1)
                owner_alive = self._pid_alive(owner_pid)
                if age <= self.ttl_seconds and owner_alive:
                    raise RuntimeError(f"CROSS_VENUE_WRITER_ALREADY_ACTIVE:{self.venue}")
                if attempt == 0:
                    self.path.unlink(missing_ok=True); continue
                raise RuntimeError(f"CROSS_VENUE_WRITER_LOCK_RACE:{self.venue}")
            else:
                try:
                    payload = json.dumps({"venue": self.venue, "pid": os.getpid(),
                                          "heartbeat_at": utc_now(), "heartbeat_epoch": time.time(),
                                          **safety_envelope()}, sort_keys=True).encode("utf-8")
                    os.write(descriptor, payload); os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self.acquired = True
                return
        raise RuntimeError(f"CROSS_VENUE_WRITER_LOCK_UNAVAILABLE:{self.venue}")

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            process_query_limited_information = 0x1000
            still_active = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information, False, pid,
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == still_active
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    def heartbeat(self) -> None:
        atomic_json(self.path, {
            "venue": self.venue, "pid": os.getpid(), "heartbeat_at": utc_now(),
            "heartbeat_epoch": time.time(), **safety_envelope(),
        })

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            current = json.loads(self.path.read_text(encoding="utf-8"))
            if int(current.get("pid") or -1) == os.getpid():
                self.path.unlink(missing_ok=True)
        except Exception:
            pass
        self.acquired = False


class StreamStore:
    def __init__(self, venue: str, root: Path | str | None = None, *, dedup_capacity: int = 200_000,
                 consumer_offsets_path: Path | str | None = None):
        self.root = safe_staging_root(root)
        self.venue = str(venue).lower()
        if not self.venue.replace("_", "").isalnum():
            raise ValueError("CROSS_VENUE_INVALID_VENUE")
        self.venue_root = self.root / self.venue
        self.stream_path = self.venue_root / "normalized" / "current.jsonl"
        self.segments_dir = self.venue_root / "normalized" / "consumed_hot_segments"
        self.rollover_manifest_path = self.venue_root / "normalized" / "rollover_manifest.json"
        self.rollover_journal_path = self.venue_root / "normalized" / "rollover_journal.json"
        self.health_path = self.venue_root / "health.json"
        self.manifest_path = self.venue_root / "manifest.json"
        self.consumer_offsets_path = Path(
            consumer_offsets_path or (RUNTIME_ROOT / "stream_offsets.json")
        )
        previous_manifest = read_json(self.manifest_path, {}) or {}
        self.reconnect_base = int(previous_manifest.get("reconnect_count_total") or 0)
        self.gap_base = int(previous_manifest.get("gaps_total") or 0)
        self._seen_order: deque[str] = deque(maxlen=dedup_capacity)
        self._seen: set[str] = set()
        self.rows_written = 0
        self.raw_frames_written = 0
        self.duplicates = 0
        self.last_hash: str | None = None
        self._last_fsync_monotonic = 0.0
        self._last_raw_fsync_monotonic = 0.0
        self._opened_monotonic = time.monotonic()
        self._initial_stream_bytes = self.stream_path.stat().st_size if self.stream_path.is_file() else 0
        self.lease = WriterLease(self.root, self.venue)
        storage_config = load_config()
        from ..storage_efficiency_v2 import load_storage_config

        efficiency_config = load_storage_config()
        self.minimum_free_disk_bytes = int(storage_config["minimum_free_disk_bytes"])
        self.maximum_stream_bytes = min(
            int(storage_config["maximum_stream_bytes_per_venue"]),
            int(efficiency_config["normalized_hot_max_bytes"]),
        )
        self.maximum_raw_bytes = int(efficiency_config["raw_hot_max_bytes"])
        self.write_partitioned_normalized_jsonl = bool(
            efficiency_config["write_partitioned_normalized_jsonl"]
        )
        self.raw_rollover_manifest_path = self.venue_root / "raw" / "rollover_manifest.json"
        self.raw_rollover_journal_path = self.venue_root / "raw" / "rollover_journal.json"
        self.retention_days = int(storage_config.get("retention_days", 14))
        self._compression_threads: list[threading.Thread] = []
        self._compressing_segments: set[Path] = set()
        self._compression_errors: list[str] = []

    def _guard_disk(self, incoming_bytes: int) -> None:
        free = shutil.disk_usage(self.root.parent if self.root.parent.exists() else self.root.parent.parent).free
        if free - max(0, incoming_bytes) < self.minimum_free_disk_bytes:
            raise RuntimeError("CROSS_VENUE_MINIMUM_FREE_DISK_GUARD")

    def _rollover_manifest(self) -> dict[str, Any]:
        value = read_json(self.rollover_manifest_path, {}) or {}
        rows = value.get("segments") if isinstance(value, dict) else None
        return {
            "schema": "cross_venue_hot_stream_rollover.v1",
            "venue": self.venue,
            "segments": list(rows) if isinstance(rows, list) else [],
            "raw_audit_sources_untouched": True,
            "partitioned_normalized_sources_untouched": True,
            "updated_at": utc_now(),
            **safety_envelope(),
        }

    def _register_rollover(self, segment: Path, stream_bytes: int) -> None:
        manifest = self._rollover_manifest()
        relative = segment.relative_to(self.venue_root).as_posix()
        if not any(str(row.get("segment_path")) == relative for row in manifest["segments"]):
            manifest["segments"].append({
                "segment_path": relative,
                "stream_bytes": stream_bytes,
                "rotated_at": utc_now(),
                "state": "CONSUMED_DERIVED_SEGMENT_PENDING_GZIP",
                "derived_hot_stream": True,
                "raw_audit_sources_untouched": True,
            })
        manifest["updated_at"] = utc_now()
        atomic_json(self.rollover_manifest_path, manifest)

    def _compress_segment(self, segment: Path) -> None:
        compressed = segment.with_suffix(segment.suffix + ".gz")
        tmp = compressed.with_name(f"{compressed.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
        raw_digest = hashlib.sha256()
        try:
            with segment.open("rb") as source, tmp.open("wb") as raw_target:
                with gzip.GzipFile(filename=segment.name, mode="wb", fileobj=raw_target, mtime=0) as target:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        raw_digest.update(chunk)
                        target.write(chunk)
                raw_target.flush()
                os.fsync(raw_target.fileno())
            os.replace(tmp, compressed)
            compressed_digest = hashlib.sha256()
            with compressed.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    compressed_digest.update(chunk)
            with stream_rollover_lock(self.root):
                manifest = self._rollover_manifest()
                relative = segment.relative_to(self.venue_root).as_posix()
                for row in manifest["segments"]:
                    if str(row.get("segment_path")) == relative:
                        row.update({
                            "state": "GZIP_VERIFIED_DERIVED_SEGMENT",
                            "compressed_path": compressed.relative_to(self.venue_root).as_posix(),
                            "raw_sha256": raw_digest.hexdigest(),
                            "compressed_sha256": compressed_digest.hexdigest(),
                            "compressed_bytes": compressed.stat().st_size,
                            "compressed_at": utc_now(),
                        })
                        break
                segment.unlink(missing_ok=True)
                manifest["updated_at"] = utc_now()
                atomic_json(self.rollover_manifest_path, manifest)
        except Exception as exc:
            error = f"{type(exc).__name__}:{str(exc)[:200]}"
            self._compression_errors.append(error)
            try:
                with stream_rollover_lock(self.root):
                    manifest = self._rollover_manifest()
                    relative = segment.relative_to(self.venue_root).as_posix()
                    for row in manifest["segments"]:
                        if str(row.get("segment_path")) == relative:
                            row.update({
                                "state": "GZIP_ERROR_DERIVED_SEGMENT_RETAINED",
                                "compression_error": error,
                                "compression_failed_at": utc_now(),
                            })
                            break
                    manifest["updated_at"] = utc_now()
                    atomic_json(self.rollover_manifest_path, manifest)
            except Exception:
                pass
        finally:
            tmp.unlink(missing_ok=True)
            self._compressing_segments.discard(segment)

    def _schedule_segment_compression(self, segment: Path) -> None:
        # Storage Efficiency V2 moves compression out of collector processes.
        # The dedicated low-priority worker consumes the manifest state; the
        # collector only performs the bounded atomic rollover.
        return

    def _recover_rollover(self) -> None:
        journal = read_json(self.rollover_journal_path, {}) or {}
        if isinstance(journal, dict) and journal.get("segment_path"):
            segment = self.venue_root / str(journal["segment_path"])
            if segment.resolve(strict=False).parent != self.segments_dir.resolve(strict=False):
                raise ValueError("CROSS_VENUE_ROLLOVER_JOURNAL_PATH_INVALID")
            with stream_rollover_lock(self.root):
                if segment.is_file():
                    self.stream_path.parent.mkdir(parents=True, exist_ok=True)
                    self.stream_path.touch(exist_ok=True)
                    offsets = read_json(self.consumer_offsets_path, {}) or {}
                    if not isinstance(offsets, dict) or not str(
                        offsets.get("_schema") or ""
                    ).startswith("cross_venue_stream_offsets.v"):
                        raise RuntimeError(
                            "CROSS_VENUE_ROLLOVER_RECOVERY_CONSUMER_STATE_UNAVAILABLE"
                        )
                    offsets[self.venue] = 0
                    offsets["updated_at"] = utc_now()
                    atomic_json(self.consumer_offsets_path, offsets)
                    self._register_rollover(segment, segment.stat().st_size)
                self.rollover_journal_path.unlink(missing_ok=True)
        if self.segments_dir.is_dir() and not self.segments_dir.is_symlink():
            for segment in self.segments_dir.glob("*.jsonl"):
                if segment.is_file() and not segment.is_symlink():
                    self._schedule_segment_compression(segment)

    def _register_raw_rollover(
        self, segment: Path, logical_bytes: int, date: str,
    ) -> None:
        manifest = read_json(self.raw_rollover_manifest_path, {}) or {}
        rows = manifest.get("segments") if isinstance(manifest, dict) else None
        rows = list(rows) if isinstance(rows, list) else []
        relative = segment.relative_to(self.venue_root).as_posix()
        if not any(str(row.get("segment_path")) == relative for row in rows):
            rows.append({
                "segment_path": relative,
                "logical_bytes": int(logical_bytes),
                "date": date,
                "rotated_at": utc_now(),
                "state": "CLOSED_RAW_PENDING_TRANSPARENT_COMPRESSION",
                "raw_audit_source": True,
                "delete_allowed": False,
            })
        atomic_json(self.raw_rollover_manifest_path, {
            "schema": "cross_venue_raw_rollover.v1",
            "venue": self.venue,
            "segments": rows,
            "updated_at": utc_now(),
            "mode": "COMPRESSION_ONLY_NO_DELETE",
            **safety_envelope(),
        })

    def _recover_raw_rollover(self) -> None:
        journal = read_json(self.raw_rollover_journal_path, {}) or {}
        if not isinstance(journal, dict) or not journal.get("segment_path"):
            return
        segment = self.venue_root / str(journal["segment_path"])
        raw_root = (self.venue_root / "raw").resolve(strict=False)
        if raw_root not in segment.resolve(strict=False).parents:
            raise ValueError("CROSS_VENUE_RAW_ROLLOVER_JOURNAL_PATH_INVALID")
        if segment.is_file() and not segment.is_symlink():
            source = self.venue_root / str(journal.get("active_path") or "")
            if raw_root not in source.resolve(strict=False).parents:
                raise ValueError("CROSS_VENUE_RAW_ROLLOVER_ACTIVE_PATH_INVALID")
            source.parent.mkdir(parents=True, exist_ok=True)
            source.touch(exist_ok=True)
            self._register_raw_rollover(
                segment,
                int(journal.get("logical_bytes") or segment.stat().st_size),
                str(journal.get("date") or segment.parent.name),
            )
        self.raw_rollover_journal_path.unlink(missing_ok=True)

    def _guard_capacity(self, incoming_bytes: int) -> None:
        self._guard_disk(incoming_bytes)
        incoming_bytes = max(0, incoming_bytes)
        current = self.stream_path.stat().st_size if self.stream_path.is_file() else 0
        if current + incoming_bytes <= self.maximum_stream_bytes:
            return
        if incoming_bytes > self.maximum_stream_bytes:
            raise RuntimeError("CROSS_VENUE_STREAM_SIZE_GUARD_BATCH_TOO_LARGE")
        with stream_rollover_lock(self.root):
            current = self.stream_path.stat().st_size if self.stream_path.is_file() else 0
            if current + incoming_bytes <= self.maximum_stream_bytes:
                return
            offsets = read_json(self.consumer_offsets_path, {}) or {}
            if not isinstance(offsets, dict) or not str(offsets.get("_schema") or "").startswith(
                "cross_venue_stream_offsets.v"
            ):
                raise RuntimeError("CROSS_VENUE_STREAM_SIZE_GUARD_CONSUMER_STATE_UNAVAILABLE")
            try:
                consumed_offset = int(offsets.get(self.venue))
            except (TypeError, ValueError):
                raise RuntimeError("CROSS_VENUE_STREAM_SIZE_GUARD_CONSUMER_STATE_UNAVAILABLE") from None
            if consumed_offset != current:
                raise RuntimeError("CROSS_VENUE_STREAM_SIZE_GUARD_CONSUMER_LAG")
            if self.stream_path.is_symlink() or self.segments_dir.is_symlink():
                raise ValueError("CROSS_VENUE_ROLLOVER_SYMLINK_BLOCKED")
            self.segments_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            segment = self.segments_dir / f"stream_{stamp}_{uuid.uuid4().hex[:8]}.jsonl"
            atomic_json(self.rollover_journal_path, {
                "schema": "cross_venue_hot_stream_rollover_journal.v1",
                "venue": self.venue,
                "segment_path": segment.relative_to(self.venue_root).as_posix(),
                "stream_bytes": current,
                "consumer_offset": consumed_offset,
                "prepared_at": utc_now(),
                **safety_envelope(),
            })
            os.replace(self.stream_path, segment)
            self.stream_path.touch(exist_ok=False)
            offsets[self.venue] = 0
            offsets["updated_at"] = utc_now()
            atomic_json(self.consumer_offsets_path, offsets)
            self._register_rollover(segment, current)
            self.rollover_journal_path.unlink(missing_ok=True)
            self._initial_stream_bytes = 0
        self._schedule_segment_compression(segment)

    def open(self) -> None:
        self.venue_root.mkdir(parents=True, exist_ok=True)
        self.lease.acquire()
        self._recover_rollover()
        self._recover_raw_rollover()
        self._seed_seen()

    def close(self) -> None:
        self.lease.release()

    def _seed_seen(self, limit: int = 20_000) -> None:
        if not self.stream_path.is_file():
            return
        tail: deque[str] = deque(maxlen=limit)
        try:
            with self.stream_path.open("rb") as handle:
                for line in handle:
                    if not line.endswith(b"\n"):
                        raise ValueError("CROSS_VENUE_PARTIAL_STREAM_LINE")
                    row = json.loads(line)
                    event_id = str(row.get("event_id") or "")
                    if event_id:
                        tail.append(event_id)
        except (OSError, json.JSONDecodeError, ValueError):
            raise ValueError("CROSS_VENUE_EXISTING_STREAM_INVALID")
        for event_id in tail:
            self._remember(event_id)

    def _remember(self, event_id: str) -> bool:
        if event_id in self._seen:
            return False
        if len(self._seen_order) == self._seen_order.maxlen and self._seen_order:
            self._seen.discard(self._seen_order[0])
        self._seen_order.append(event_id)
        self._seen.add(event_id)
        return True

    def append_raw(self, frame: dict[str, Any], receive_wall_ms: int, connection_id: str) -> Path:
        date = datetime.fromtimestamp(receive_wall_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        path = self.venue_root / "raw" / date / "frames.jsonl"
        payload = {
            "venue": self.venue, "received_at_ms": receive_wall_ms,
            "connection_id": connection_id, "frame": frame,
            "research_only": True,
        }
        encoded = _json_line(payload)
        self._guard_disk(len(encoded))
        path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_raw_if_needed(path, len(encoded), date)
        with path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            now = time.monotonic()
            if now - self._last_raw_fsync_monotonic >= 1.0:
                os.fsync(handle.fileno())
                self._last_raw_fsync_monotonic = now
        self.raw_frames_written += 1
        return path

    def _rotate_raw_if_needed(self, path: Path, incoming_bytes: int, date: str) -> None:
        current = path.stat().st_size if path.is_file() else 0
        if current + incoming_bytes <= self.maximum_raw_bytes:
            return
        if incoming_bytes > self.maximum_raw_bytes:
            raise RuntimeError("CROSS_VENUE_RAW_SIZE_GUARD_BATCH_TOO_LARGE")
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("CROSS_VENUE_RAW_ROLLOVER_SYMLINK_BLOCKED")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        segment = path.parent / f"frames_{stamp}_{uuid.uuid4().hex[:8]}.jsonl"
        atomic_json(self.raw_rollover_journal_path, {
            "schema": "cross_venue_raw_rollover_journal.v1",
            "venue": self.venue,
            "active_path": path.relative_to(self.venue_root).as_posix(),
            "segment_path": segment.relative_to(self.venue_root).as_posix(),
            "logical_bytes": current,
            "date": date,
            "prepared_at": utc_now(),
            **safety_envelope(),
        })
        os.replace(path, segment)
        path.touch(exist_ok=False)
        self._register_raw_rollover(segment, current, date)
        self.raw_rollover_journal_path.unlink(missing_ok=True)

    def append_events(self, rows: Iterable[dict[str, Any]]) -> int:
        accepted: list[dict[str, Any]] = []
        batch_seen: set[str] = set()
        for row in rows:
            event_id = str(row.get("event_id") or "")
            if not event_id:
                raise ValueError("CROSS_VENUE_EVENT_ID_REQUIRED")
            if event_id in self._seen or event_id in batch_seen:
                self.duplicates += 1
                continue
            batch_seen.add(event_id)
            accepted.append(row)
        if not accepted:
            return 0
        encoded_rows = [(row, _json_line(row)) for row in accepted]
        self._guard_capacity(sum(len(encoded) for _, encoded in encoded_rows))
        self.stream_path.parent.mkdir(parents=True, exist_ok=True)
        partition_handles: dict[Path, Any] = {}
        try:
            with self.stream_path.open("ab") as stream:
                for row, encoded in encoded_rows:
                    stream.write(encoded)
                    self._remember(str(row["event_id"]))
                    ts = int(row.get("local_receive_wall_ms") or 0)
                    date = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    event_type = str(row.get("event_type") or "unknown").replace("/", "_")
                    symbol = str(row.get("canonical_symbol") or "unknown").replace("/", "_")
                    if self.write_partitioned_normalized_jsonl:
                        part = self.venue_root / "normalized" / symbol / event_type / date / "events.jsonl"
                        if part not in partition_handles:
                            part.parent.mkdir(parents=True, exist_ok=True)
                            partition_handles[part] = part.open("ab")
                        partition_handles[part].write(encoded)
                    digest = hashlib.sha256((self.last_hash or "").encode("ascii") + encoded).hexdigest()
                    self.last_hash = digest
                stream.flush()
                now = time.monotonic()
                if now - self._last_fsync_monotonic >= 1.0:
                    os.fsync(stream.fileno())
                    self._last_fsync_monotonic = now
        finally:
            for handle in partition_handles.values():
                handle.flush(); handle.close()
        self.rows_written += len(accepted)
        return len(accepted)

    def write_health(self, payload: dict[str, Any]) -> None:
        reconnect_total = self.reconnect_base + int(payload.get("reconnect_count") or 0)
        gaps_total = self.gap_base + int(payload.get("gaps") or 0)
        stream_bytes = self.stream_path.stat().st_size if self.stream_path.is_file() else 0
        elapsed_hours = max((time.monotonic() - self._opened_monotonic) / 3600.0, 1.0 / 3600.0)
        growth_per_hour = max(0, stream_bytes - self._initial_stream_bytes) / elapsed_hours
        disk = shutil.disk_usage(self.root.parent if self.root.parent.exists() else self.root.parent.parent)
        rollover = self._rollover_manifest()
        rollover_rows = list(rollover.get("segments") or [])
        payload = {
            **payload, "storage_rows_this_process": self.rows_written,
            "raw_frames_this_process": self.raw_frames_written,
            "storage_duplicates_this_process": self.duplicates,
            "last_hash": self.last_hash, "stream_path": str(self.stream_path),
            "reconnect_count_total": reconnect_total, "gaps_total": gaps_total,
            "stream_size_bytes": stream_bytes,
            "stream_growth_bytes_per_hour_this_process": growth_per_hour,
            "disk_free_bytes": disk.free,
            "rotation_state": "UTC_DATE_PARTITIONS_AND_BOUNDED_HOT_STREAM_ACTIVE",
            "normalized_hot_max_bytes": self.maximum_stream_bytes,
            "raw_hot_max_bytes": self.maximum_raw_bytes,
            "partitioned_normalized_jsonl_enabled": self.write_partitioned_normalized_jsonl,
            "hot_stream_rollovers": len(rollover_rows),
            "hot_stream_compression_pending": sum(
                str(row.get("state")) == "CONSUMED_DERIVED_SEGMENT_PENDING_GZIP"
                for row in rollover_rows
            ),
            "hot_stream_compression_errors": list(self._compression_errors[-10:]),
            "hot_stream_compression_worker": "SEPARATE_LOW_PRIORITY_PROCESS",
            "raw_compression": "NONE_APPEND_ONLY_AUDIT_SOURCE",
            "derived_compaction_status": "SEPARATE_JOB_NOT_IN_HOT_PATH",
            "retention_days_configured": self.retention_days,
            "heartbeat_at": utc_now(), **safety_envelope(),
        }
        atomic_json(self.health_path, payload)
        atomic_json(self.manifest_path, {
            "schema": "cross_venue_stream_manifest.v1", "venue": self.venue,
            "updated_at": payload["heartbeat_at"], "normalized_stream": str(self.stream_path),
            "last_hash": self.last_hash, "rows_this_process": self.rows_written,
            "raw_frames_this_process": self.raw_frames_written,
            "reconnect_count_total": reconnect_total, "gaps_total": gaps_total,
            "stream_size_bytes": stream_bytes,
            "stream_growth_bytes_per_hour_this_process": growth_per_hour,
            "disk_free_bytes": disk.free,
            "rotation_state": "UTC_DATE_PARTITIONS_AND_BOUNDED_HOT_STREAM_ACTIVE",
            "normalized_hot_max_bytes": self.maximum_stream_bytes,
            "raw_hot_max_bytes": self.maximum_raw_bytes,
            "partitioned_normalized_jsonl_enabled": self.write_partitioned_normalized_jsonl,
            "hot_stream_rollovers": len(rollover_rows),
            "hot_stream_compression_pending": sum(
                str(row.get("state")) == "CONSUMED_DERIVED_SEGMENT_PENDING_GZIP"
                for row in rollover_rows
            ),
            "hot_stream_compression_errors": list(self._compression_errors[-10:]),
            "hot_stream_compression_worker": "SEPARATE_LOW_PRIORITY_PROCESS",
            "raw_compression": "NONE_APPEND_ONLY_AUDIT_SOURCE",
            "derived_compaction_status": "SEPARATE_JOB_NOT_IN_HOT_PATH",
            "retention_days_configured": self.retention_days,
            "append_only": True, "partitioned_by": ["venue", "symbol", "event_type", "utc_date"],
            **safety_envelope(),
        })
        self.lease.heartbeat()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file() or path.is_symlink():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def storage_status(root: Path | str | None = None) -> dict[str, Any]:
    """Return bounded storage telemetry without scanning stream contents."""
    safe_root = safe_staging_root(root)
    config = load_config()
    venues: list[dict[str, Any]] = []
    total_stream_bytes = 0
    for venue in config.get("active_venues", []):
        venue_name = str(venue).lower()
        manifest = read_json(safe_root / venue_name / "manifest.json", {}) or {}
        health = read_json(safe_root / venue_name / "health.json", {}) or {}
        rollover = read_json(
            safe_root / venue_name / "normalized" / "rollover_manifest.json", {}
        ) or {}
        rollover_rows = list(rollover.get("segments") or []) if isinstance(rollover, dict) else []
        stream = safe_root / venue_name / "normalized" / "current.jsonl"
        stream_bytes = stream.stat().st_size if stream.is_file() and not stream.is_symlink() else 0
        compressed_bytes = sum(int(row.get("compressed_bytes") or 0) for row in rollover_rows)
        archived_source_bytes = sum(int(row.get("stream_bytes") or 0) for row in rollover_rows)
        total_stream_bytes += stream_bytes
        venues.append({
            "venue": venue_name,
            "stream_size_bytes": stream_bytes,
            "stream_growth_bytes_per_hour_this_process": manifest.get(
                "stream_growth_bytes_per_hour_this_process",
                health.get("stream_growth_bytes_per_hour_this_process"),
            ),
            "rows_this_process": manifest.get("rows_this_process"),
            "raw_frames_this_process": manifest.get("raw_frames_this_process"),
            "last_hash": manifest.get("last_hash"),
            "rotation_state": manifest.get(
                "rotation_state", "UTC_DATE_PARTITIONS_AND_BOUNDED_HOT_STREAM_ACTIVE"
            ),
            "hot_stream_rollovers": len(rollover_rows),
            "hot_stream_archived_source_bytes": archived_source_bytes,
            "hot_stream_compressed_bytes": compressed_bytes,
            "hot_stream_compression_pending": sum(
                str(row.get("state")) == "CONSUMED_DERIVED_SEGMENT_PENDING_GZIP"
                for row in rollover_rows
            ),
            "hot_stream_compression_errors": [
                row.get("compression_error") for row in rollover_rows
                if row.get("compression_error")
            ],
            "raw_compression": manifest.get("raw_compression", "NONE_APPEND_ONLY_AUDIT_SOURCE"),
            "derived_compaction_status": manifest.get(
                "derived_compaction_status", "SEPARATE_JOB_NOT_IN_HOT_PATH"
            ),
            "retention_days_configured": manifest.get(
                "retention_days_configured", config.get("retention_days")
            ),
            "heartbeat_at": health.get("heartbeat_at"),
            "collector_status": health.get("status", "NEED_DATA"),
        })
    disk_target = safe_root.parent if safe_root.parent.exists() else safe_root.parent.parent
    disk = shutil.disk_usage(disk_target)
    compaction = read_json(safe_root / "derived" / "compaction_status.json", {}) or {}
    return {
        "schema": "cross_venue_storage_status.v1",
        "status": "OK" if any(row["stream_size_bytes"] > 0 for row in venues) else "NEED_DATA",
        "root": str(safe_root),
        "active_venue_count": len(venues),
        "total_stream_size_bytes": total_stream_bytes,
        "disk_free_bytes": disk.free,
        "minimum_free_disk_bytes": int(config.get("minimum_free_disk_bytes", 0)),
        "rotation_state": "UTC_DATE_PARTITIONS_AND_BOUNDED_HOT_STREAM_ACTIVE",
        "raw_storage_contract": "APPEND_ONLY_JSONL_NO_DELETION",
        "derived_compaction": compaction or {
            "status": "NOT_RUN",
            "hot_path": False,
            "raw_deleted": False,
        },
        "venues": venues,
        **safety_envelope(),
    }


def read_new_jsonl(path: Path, offset: int, *, max_rows: int = 50_000) -> tuple[list[dict[str, Any]], int, str | None]:
    if not path.is_file() or path.is_symlink():
        return [], 0, None
    size = path.stat().st_size
    if offset < 0 or offset > size:
        return [], 0, "OFFSET_RESET_FILE_CHANGED"
    rows: list[dict[str, Any]] = []
    error: str | None = None
    with path.open("rb") as handle:
        handle.seek(offset)
        while len(rows) < max_rows:
            start = handle.tell(); line = handle.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                handle.seek(start); error = "PARTIAL_LINE_WAITING"; break
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                error = "CORRUPT_JSONL_LINE"; break
            if not isinstance(value, dict):
                error = "NON_OBJECT_JSONL_LINE"; break
            rows.append(value)
        new_offset = handle.tell()
    return rows, new_offset, error


def read_next_jsonl_record(
    handle: BinaryIO, *, snapshot_size: int,
) -> tuple[dict[str, Any] | None, int, str | None]:
    """Read one complete object without crossing the cycle's file-size snapshot."""
    start = handle.tell()
    if start >= snapshot_size:
        return None, start, None
    line = handle.readline()
    end = handle.tell()
    if not line:
        return None, start, None
    if end > snapshot_size or not line.endswith(b"\n"):
        handle.seek(start)
        return None, start, "PARTIAL_LINE_WAITING"
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        handle.seek(start)
        return None, start, "CORRUPT_JSONL_LINE"
    if not isinstance(value, dict):
        handle.seek(start)
        return None, start, "NON_OBJECT_JSONL_LINE"
    return value, end, None
