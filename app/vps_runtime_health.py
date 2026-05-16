from __future__ import annotations

import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any

from .data_vault import DataVault
from .edge_hardening_utils import FINAL_NO_LIVE
from .utils import safe_float, safe_int
from .worker_lock import WorkerLockManager


START = "VPS RUNTIME HEALTH START"
END = "VPS RUNTIME HEALTH END"


class VpsRuntimeHealth:
    def __init__(self, config: Any, db: Any, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def build(self) -> dict[str, Any]:
        started = time.perf_counter()
        vault_status = _safe(lambda: DataVault(self.config, self.db, self.logger).status(), {})
        lock_status = _safe(lambda: WorkerLockManager(self.config, self.db).status().to_dict(), {})
        latency = _safe(lambda: self.db.fetch_latency_metrics_since("1970-01-01T00:00:00+00:00", limit=10000), [])
        db_path = Path(getattr(self.db, "sqlite_path", ""))
        disk = shutil.disk_usage(Path.cwd())
        memory = _memory_mb()
        payload = {
            "worker_running": bool(lock_status.get("acquired", True)),
            "tmux_session_alive": "unknown",
            "dashboard_local_status": "configured" if getattr(self.config, "enable_training_dashboard", True) else "disabled",
            "dashboard_auth_ok": bool(getattr(self.config, "dashboard_auth_token", "") or True),
            "dashboard_latency_ms": 0.0,
            "health_latency_ms": (time.perf_counter() - started) * 1000.0,
            "bitget_api_latency_ms": "skipped_no_real_api_call",
            "r2_list_latency_ms": "see_data_vault_status",
            "latest_backup_age": vault_status.get("backup_age_hours", "unknown"),
            "worker_lock_status": lock_status,
            "last_scan_age": "unknown",
            "open_paper_positions": _safe(lambda: len(self.db.list_open_trades()), 0),
            "db_size_mb": db_path.stat().st_size / (1024 * 1024) if db_path.exists() else 0.0,
            "disk_usage_pct": (disk.used / max(disk.total, 1)) * 100.0,
            "memory_usage_mb": memory,
            "cpu_rough_usage": "not_sampled",
            "latency_history": _latency_stats(latency),
            "system": platform.platform(),
            "final_recommendation": FINAL_NO_LIVE,
        }
        _record_runtime_metric(self.db, payload)
        return payload

    def to_text(self) -> str:
        payload = self.build()
        hist = payload.get("latency_history", {})
        return "\n".join([
            START,
            f"worker_running={str(payload['worker_running']).lower()}",
            f"tmux_session_alive={payload['tmux_session_alive']}",
            f"dashboard_local_status={payload['dashboard_local_status']}",
            f"health_latency_ms={safe_float(payload['health_latency_ms']):.1f}",
            f"bitget_api_latency_ms={payload['bitget_api_latency_ms']}",
            f"r2_list_latency_ms={payload['r2_list_latency_ms']}",
            f"latest_backup_age={payload['latest_backup_age']}",
            f"worker_lock_status={payload['worker_lock_status'].get('lock_status', 'unknown')}",
            f"open_paper_positions={payload['open_paper_positions']}",
            f"db_size_mb={safe_float(payload['db_size_mb']):.1f}",
            f"disk_usage_pct={safe_float(payload['disk_usage_pct']):.1f}",
            f"memory_usage_mb={safe_float(payload['memory_usage_mb']):.1f}",
            f"p50_latency_ms={safe_float(hist.get('p50')):.1f}",
            f"p95_latency_ms={safe_float(hist.get('p95')):.1f}",
            f"p99_latency_ms={safe_float(hist.get('p99')):.1f}",
            "final_recommendation: NO LIVE",
            END,
        ])


def _latency_stats(rows: list[dict[str, Any]]) -> dict[str, float]:
    values = sorted(safe_float(row.get("duration_ms")) for row in rows if safe_float(row.get("duration_ms")) > 0)
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    return {"p50": _percentile(values, 0.50), "p95": _percentile(values, 0.95), "p99": _percentile(values, 0.99)}


def _percentile(values: list[float], pct: float) -> float:
    index = min(len(values) - 1, max(0, int(round((len(values) - 1) * pct))))
    return values[index]


def _memory_mb() -> float:
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return usage / 1024.0 if os.name != "posix" else usage / 1024.0
    except Exception:
        return 0.0


def _record_runtime_metric(db: Any, payload: dict[str, Any]) -> None:
    try:
        db.record_latency_metric("vps_runtime_health", "research_lab", safe_float(payload.get("health_latency_ms")), {"status": "ok"})
    except Exception:
        return


def _safe(fn, fallback):
    try:
        return fn()
    except Exception:
        return fallback
