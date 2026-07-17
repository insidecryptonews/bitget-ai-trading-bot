"""Fail-closed append-only storage for cross-venue research streams."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import ctypes
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import STAGING_ROOT, safety_envelope
from .providers import load_config


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.replace(tmp, path)


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
    def __init__(self, venue: str, root: Path | str | None = None, *, dedup_capacity: int = 200_000):
        self.root = safe_staging_root(root)
        self.venue = str(venue).lower()
        if not self.venue.replace("_", "").isalnum():
            raise ValueError("CROSS_VENUE_INVALID_VENUE")
        self.venue_root = self.root / self.venue
        self.stream_path = self.venue_root / "normalized" / "current.jsonl"
        self.health_path = self.venue_root / "health.json"
        self.manifest_path = self.venue_root / "manifest.json"
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
        self.lease = WriterLease(self.root, self.venue)
        storage_config = load_config()
        self.minimum_free_disk_bytes = int(storage_config["minimum_free_disk_bytes"])
        self.maximum_stream_bytes = int(storage_config["maximum_stream_bytes_per_venue"])

    def _guard_capacity(self, incoming_bytes: int) -> None:
        free = shutil.disk_usage(self.root.parent if self.root.parent.exists() else self.root.parent.parent).free
        current = self.stream_path.stat().st_size if self.stream_path.is_file() else 0
        if free - max(0, incoming_bytes) < self.minimum_free_disk_bytes:
            raise RuntimeError("CROSS_VENUE_MINIMUM_FREE_DISK_GUARD")
        if current + max(0, incoming_bytes) > self.maximum_stream_bytes:
            raise RuntimeError("CROSS_VENUE_STREAM_SIZE_GUARD")

    def open(self) -> None:
        self.venue_root.mkdir(parents=True, exist_ok=True)
        self.lease.acquire()
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
        self._guard_capacity(len(encoded))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(encoded); handle.flush()
        self.raw_frames_written += 1
        return path

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
        payload = {
            **payload, "storage_rows_this_process": self.rows_written,
            "raw_frames_this_process": self.raw_frames_written,
            "storage_duplicates_this_process": self.duplicates,
            "last_hash": self.last_hash, "stream_path": str(self.stream_path),
            "reconnect_count_total": reconnect_total, "gaps_total": gaps_total,
            "heartbeat_at": utc_now(), **safety_envelope(),
        }
        atomic_json(self.health_path, payload)
        atomic_json(self.manifest_path, {
            "schema": "cross_venue_stream_manifest.v1", "venue": self.venue,
            "updated_at": payload["heartbeat_at"], "normalized_stream": str(self.stream_path),
            "last_hash": self.last_hash, "rows_this_process": self.rows_written,
            "raw_frames_this_process": self.raw_frames_written,
            "reconnect_count_total": reconnect_total, "gaps_total": gaps_total,
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
