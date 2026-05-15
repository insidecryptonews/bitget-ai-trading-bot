from __future__ import annotations

import os
import platform
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import BotConfig


LOCK_STATE_KEY = "active_worker_lock"


@dataclass
class WorkerLockStatus:
    enabled: bool
    acquired: bool
    current_instance_id: str
    active_worker_instance: str
    lock_status: str
    lock_age_seconds: float
    warning_if_duplicate_worker: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "acquired": self.acquired,
            "current_instance_id": self.current_instance_id,
            "active_worker_instance": self.active_worker_instance,
            "lock_status": self.lock_status,
            "lock_age_seconds": round(self.lock_age_seconds, 3),
            "warning_if_duplicate_worker": self.warning_if_duplicate_worker,
        }


class WorkerLockManager:
    def __init__(self, config: BotConfig, db: Any, logger: Any | None = None, *, instance_id: str | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger
        self.instance_id = instance_id or config.bot_instance_id or _generated_instance_id()

    def acquire(self) -> WorkerLockStatus:
        if not self.config.require_single_worker_lock:
            return WorkerLockStatus(False, True, self.instance_id, self.instance_id, "disabled", 0.0)
        existing = self._read()
        now = time.time()
        active_id = str(existing.get("instance_id") or "")
        age = _age_seconds(existing, now)
        expired = not active_id or age > max(1, int(self.config.worker_lock_ttl_seconds or 120))
        same_instance = active_id == self.instance_id
        if expired or same_instance:
            self._write(now)
            return WorkerLockStatus(True, True, self.instance_id, self.instance_id, "acquired", 0.0)
        return WorkerLockStatus(
            True,
            False,
            self.instance_id,
            active_id,
            "blocked_duplicate",
            age,
            "duplicate_worker_detected",
        )

    def heartbeat(self) -> WorkerLockStatus:
        if not self.config.require_single_worker_lock:
            return WorkerLockStatus(False, True, self.instance_id, self.instance_id, "disabled", 0.0)
        existing = self._read()
        active_id = str(existing.get("instance_id") or "")
        if not active_id or active_id == self.instance_id or _age_seconds(existing) > max(1, int(self.config.worker_lock_ttl_seconds or 120)):
            self._write(time.time())
            return WorkerLockStatus(True, True, self.instance_id, self.instance_id, "heartbeat", 0.0)
        return WorkerLockStatus(True, False, self.instance_id, active_id, "blocked_duplicate", _age_seconds(existing), "duplicate_worker_detected")

    def status(self) -> WorkerLockStatus:
        if not self.config.require_single_worker_lock:
            return WorkerLockStatus(False, True, self.instance_id, self.instance_id, "disabled", 0.0)
        existing = self._read()
        active_id = str(existing.get("instance_id") or "")
        age = _age_seconds(existing)
        if not active_id:
            return WorkerLockStatus(True, False, self.instance_id, "", "missing", 0.0)
        expired = age > max(1, int(self.config.worker_lock_ttl_seconds or 120))
        if active_id == self.instance_id:
            return WorkerLockStatus(True, True, self.instance_id, active_id, "owned", age)
        return WorkerLockStatus(
            True,
            expired,
            self.instance_id,
            active_id,
            "expired" if expired else "blocked_duplicate",
            age,
            "" if expired else "duplicate_worker_detected",
        )

    def release(self) -> None:
        if not self.config.require_single_worker_lock:
            return
        existing = self._read()
        if str(existing.get("instance_id") or "") == self.instance_id:
            try:
                self.db.delete_state(LOCK_STATE_KEY)
            except Exception:
                pass

    def _read(self) -> dict[str, Any]:
        try:
            value = self.db.get_state(LOCK_STATE_KEY, {})
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _write(self, timestamp: float) -> None:
        payload = {
            "instance_id": self.instance_id,
            "updated_at_ts": float(timestamp),
            "updated_at": datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(),
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "platform": platform.platform(),
        }
        self.db.set_state(LOCK_STATE_KEY, payload)


def worker_lock_status_payload(config: BotConfig, db: Any) -> dict[str, Any]:
    return WorkerLockManager(config, db).status().to_dict()


def _age_seconds(payload: dict[str, Any], now: float | None = None) -> float:
    raw = payload.get("updated_at_ts")
    if raw is None:
        return 0.0
    try:
        return max(0.0, float(now or time.time()) - float(raw))
    except Exception:
        return 0.0


def _generated_instance_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
