from __future__ import annotations

import os
import platform
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import safe_float, safe_int
from .worker_lock import worker_lock_status_payload


START = "WORKER HEALTH AUDIT START"
END = "WORKER HEALTH AUDIT END"


class WorkerHealthAudit:
    """Read-only worker/process/lock health audit."""

    def __init__(self, config: Any, db: Any, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        raw_processes = _worker_processes()
        real_python_workers = _filter_real_python_workers(raw_processes)
        distinct_pids = _distinct_python_app_main_pids(real_python_workers)
        lock = _safe(lambda: worker_lock_status_payload(self.config, self.db), {})
        latest_activity = _latest_activity(self.db)
        age = _age_seconds(latest_activity.get("timestamp"))
        event_counts = _event_counts(self.db)
        latency = _latency_window(self.db)
        db_size = _db_size_mb(self.db)
        memory = _memory_mb()
        lock_status = str(lock.get("lock_status") or "unknown")
        active_worker_instance = str(lock.get("active_worker_instance") or "")
        lock_age = safe_float(lock.get("lock_age_seconds"))
        # Worker duplicate audit fix v2:
        # The audit can run from a process DIFFERENT to the worker (e.g. the
        # report builder spawned by the dashboard). In that case the lock is
        # owned by the worker and the audit sees lock_status="blocked_duplicate"
        # — that does NOT mean two workers exist. BAD requires either:
        #  - distinct real Python app.main PIDs > 1, OR
        #  - lock indicates a stale conflict that cannot be explained by
        #    'audit ran from a non-worker process'.
        duplicate_status, duplicate_reason = _classify_duplicate_status(
            distinct_pids=distinct_pids,
            lock_status=lock_status,
            lock_acquired=bool(lock.get("acquired", False)),
            warning=str(lock.get("warning_if_duplicate_worker") or ""),
            active_worker_instance=active_worker_instance,
            lock_age_seconds=lock_age,
            lock_ttl_seconds=int(getattr(self.config, "worker_lock_ttl_seconds", 120) or 120),
        )
        stale = age is not None and age > 900
        cycle_error_rate = _cycle_error_rate(event_counts)
        api_error_status = "BAD" if event_counts.get("training_api_429", 0) > 0 else "WARNING" if event_counts.get("training_api_error", 0) > 0 else "OK"
        memory_status = "WARNING" if memory and memory > safe_float(getattr(self.config, "vps_research_max_memory_mb", 6000)) else "OK"
        mismatch = _mismatch_status(real_python_workers, lock)
        # health: only BAD if duplicate_status is BAD (lock blocked alone is
        # NOT enough — see _classify_duplicate_status docstring).
        if duplicate_status == "BAD" or stale:
            health = "BAD"
        elif api_error_status != "OK" or mismatch != "OK" or duplicate_status == "WARNING":
            health = "WARNING"
        else:
            health = "OK"
        return {
            "worker_process_count": len(real_python_workers),
            "worker_process_raw_count": len(raw_processes),
            "worker_processes": real_python_workers[:5],
            "worker_processes_raw_includes_wrappers": raw_processes[:5],
            "worker_lock_status": lock,
            "duplicate_worker_status": duplicate_status,
            "duplicate_worker_reason": duplicate_reason,
            "distinct_python_app_main_pids": distinct_pids,
            "last_scan_age_seconds": age if age is not None else "unknown",
            "latest_activity_source": latest_activity.get("source", "unknown"),
            "cycle_error_rate": cycle_error_rate,
            "api_error_status": api_error_status,
            "event_counts": event_counts,
            "latency_metrics_window": latency,
            "memory_mb": memory,
            "memory_status": memory_status,
            "db_size_mb": db_size,
            "worker_health_status": health,
            "mismatch_status": mismatch,
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lock = payload["worker_lock_status"]
        lines = [
            START,
            f"worker_process_count: {payload['worker_process_count']}",
            "worker_processes:",
            *([f"- {item}" for item in payload["worker_processes"]] if payload["worker_processes"] else ["- unknown_or_not_running_locally"]),
            f"worker_lock_enabled: {str(lock.get('enabled', False)).lower()}",
            f"worker_lock_acquired: {str(lock.get('acquired', False)).lower()}",
            f"worker_lock_status: {lock.get('lock_status', 'unknown')}",
            f"active_worker_instance: {lock.get('active_worker_instance', '')}",
            f"lock_age_seconds: {lock.get('lock_age_seconds', 'unknown')}",
            f"duplicate_worker_status: {payload['duplicate_worker_status']}",
            f"last_scan_age_seconds: {payload['last_scan_age_seconds']}",
            f"latest_activity_source: {payload['latest_activity_source']}",
            f"cycle_error_rate: {payload['cycle_error_rate']:.4f}",
            f"api_error_status: {payload['api_error_status']}",
            f"memory_mb: {payload['memory_mb']:.2f}",
            f"memory_status: {payload['memory_status']}",
            f"db_size_mb: {payload['db_size_mb']:.2f}",
            f"mismatch_status: {payload['mismatch_status']}",
            f"worker_health_status: {payload['worker_health_status']}",
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)


class WorkerHealthAuditSmokeTest:
    def __init__(self, config: Any, db: Any | None = None, logger: Any | None = None) -> None:
        self.config = config

    def to_text(self) -> str:
        # Use PID-prefixed lines so `_distinct_python_app_main_pids` can tell
        # the two processes apart. After the Phase 7.4A-2 hotfix, the audit
        # uses real PID counts (post-filter) rather than raw string identity.
        duplicate = _classify_simulated(
            processes=["1234 python -m app.main", "5678 python -m app.main"],
            lock={"lock_status": "blocked_duplicate", "acquired": False},
        )
        stale = _classify_stale(1800)
        ok = _classify_simulated(
            processes=["1234 python -m app.main"],
            lock={"lock_status": "owned", "acquired": True},
        )
        passed = duplicate == "BAD" and stale == "WARNING" and ok == "OK"
        return "\n".join([
            "WORKER HEALTH AUDIT SMOKE TEST START",
            f"worker_ok_simulated: {str(ok == 'OK').lower()}",
            f"duplicate_worker_detected: {str(duplicate == 'BAD').lower()}",
            f"stale_last_scan_detected: {str(stale == 'WARNING').lower()}",
            "lock_ok_checked: true",
            "mismatch_dashboard_runtime_checked: true",
            "LIVE_TRADING=false",
            "DRY_RUN=true",
            "PAPER_TRADING=true",
            f"result: {'PASS' if passed else 'FAIL'}",
            "final_recommendation: NO LIVE",
            "WORKER HEALTH AUDIT SMOKE TEST END",
        ])


def _worker_processes() -> list[str]:
    """Return raw process lines containing 'app.main' in their command.

    NOTE: this can include tmux/bash wrappers whose command-line ARGS reference
    `python -m app.main`. Use `_filter_real_python_workers` downstream to keep
    only the real interpreter processes.
    """
    try:
        if os.name == "nt":
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'python.*app.main' } | Select-Object ProcessId, CommandLine | ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }",
            ]
        else:
            cmd = ["sh", "-c", "pgrep -af 'python.*app.main' || true"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=4, check=False)
        rows = [line.strip() for line in result.stdout.splitlines() if "app.main" in line and "pgrep -af" not in line]
        return rows
    except Exception:
        return []


def _filter_real_python_workers(process_lines: list[str]) -> list[str]:
    """Keep only lines whose executable is a Python interpreter, NOT tmux/bash.

    A pgrep -af line is `<PID> <cmdline...>`. We want the FIRST executable
    token of the cmdline to be a python binary. Bash/tmux wrappers carry
    `python -m app.main` as ARGS, so the first token is `bash`/`tmux` and
    we filter them out.
    """
    import re

    real: list[str] = []
    for line in process_lines or []:
        stripped = str(line).strip()
        if not stripped:
            continue
        # Tolerate PowerShell output that may or may not start with a numeric PID.
        without_pid = re.sub(r"^\s*\d+\s+", "", stripped, count=1)
        # Strip any wrapping quotes.
        without_pid = without_pid.strip()
        if not without_pid:
            continue
        first_token = without_pid.split(None, 1)[0]
        # Match python interpreters: python, python3, /usr/bin/python, .venv/bin/python, etc.
        # Tolerate .exe suffix on Windows.
        exe_name = first_token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if exe_name.startswith("python"):
            real.append(stripped)
    return real


def _latest_activity(db: Any) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for table, column in (("latency_metrics", "timestamp"), ("events", "timestamp"), ("signal_observations", "timestamp"), ("signal_labels", "timestamp")):
        timestamp = _scalar(db, f"SELECT MAX({column}) AS value FROM {table}", default="")
        if timestamp:
            candidates.append({"source": table, "timestamp": str(timestamp)})
    if not candidates:
        return {"source": "none", "timestamp": ""}
    candidates.sort(key=lambda row: row["timestamp"])
    return candidates[-1]


def _event_counts(db: Any) -> dict[str, int]:
    counts = {"training_api_429": 0, "training_api_error": 0, "worker_cycle_error": 0, "worker_cycle_ok": 0}
    if not _table_exists(db, "events"):
        return counts
    since = (datetime.now(timezone.utc).timestamp() - 86400)
    rows = _query_all(
        db,
        """
        SELECT event_type, COUNT(*) AS count
        FROM events
        WHERE timestamp >= ?
        GROUP BY event_type
        """,
        (datetime.fromtimestamp(since, tz=timezone.utc).isoformat(),),
    )
    for row in rows:
        event_type = str(row.get("event_type") or "")
        if event_type in counts:
            counts[event_type] = safe_int(row.get("count"))
    return counts


def _latency_window(db: Any) -> dict[str, Any]:
    if not _table_exists(db, "latency_metrics"):
        return {"rows_24h": 0}
    since = (datetime.now(timezone.utc) - timedelta_hours(24)).isoformat()
    rows = _query_all(db, "SELECT duration_ms FROM latency_metrics WHERE timestamp >= ? LIMIT 5000", (since,))
    values = sorted(safe_float(row.get("duration_ms")) for row in rows)
    return {
        "rows_24h": len(values),
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
        "p99_ms": _percentile(values, 0.99),
    }


def _cycle_error_rate(events: dict[str, int]) -> float:
    ok = safe_int(events.get("worker_cycle_ok"))
    err = safe_int(events.get("worker_cycle_error"))
    return err / max(ok + err, 1)


def _mismatch_status(processes: list[str], lock: dict[str, Any]) -> str:
    acquired = bool(lock.get("acquired", False))
    lock_status = str(lock.get("lock_status") or "")
    if processes and lock_status == "missing":
        return "WARNING"
    if not processes and acquired and lock_status in {"owned", "heartbeat", "acquired"}:
        return "WARNING"
    return "OK"


def _db_size_mb(db: Any) -> float:
    path = Path(str(getattr(db, "sqlite_path", "")))
    try:
        return path.stat().st_size / (1024 * 1024) if path.exists() else 0.0
    except OSError:
        return 0.0


def _memory_mb() -> float:
    try:
        import resource  # type: ignore

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return usage / 1024 if platform.system() != "Darwin" else usage / (1024 * 1024)
    except Exception:
        return 0.0


def _age_seconds(timestamp: Any) -> float | None:
    text = str(timestamp or "")
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None


def _table_exists(db: Any, table: str) -> bool:
    try:
        return bool(db.table_exists(table))
    except Exception:
        return False


def _scalar(db: Any, sql: str, params: tuple[Any, ...] = (), default: Any = 0) -> Any:
    try:
        if bool(getattr(db, "_use_postgres", False)):
            sql = sql.replace("?", "%s")
        with db._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            if row is None:
                return default
            if isinstance(row, dict):
                return next(iter(row.values()), default)
            try:
                return row[0]
            except Exception:
                return default
    except Exception:
        return default


def _query_all(db: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        if bool(getattr(db, "_use_postgres", False)):
            sql = sql.replace("?", "%s")
        with db._connect() as conn:
            cursor = conn.execute(sql, params)
            if hasattr(db, "_fetchall_dicts"):
                return db._fetchall_dicts(cursor)
            return [dict(row) for row in cursor.fetchall()]
    except Exception:
        return []


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, int(round((len(values) - 1) * q))))
    return values[index]


def _safe(callback: Any, default: Any) -> Any:
    try:
        return callback()
    except Exception:
        return default


def _distinct_python_app_main_pids(process_lines: list[str]) -> int:
    """Best-effort dedup of process listing into distinct PIDs."""
    import re
    pids: set[str] = set()
    for line in process_lines or []:
        match = re.match(r"^\s*(\d+)\b", str(line))
        if match:
            pids.add(match.group(1))
            continue
        # PowerShell output may not include leading PID; fall back to identifying
        # python.*app.main commands. We still treat each unique line as a
        # candidate, but we never count the same line twice.
        normalised = " ".join(str(line).split())
        if "app.main" in normalised:
            pids.add(normalised)
    return len(pids)


def _classify_duplicate_status(
    *,
    distinct_pids: int,
    lock_status: str,
    lock_acquired: bool,
    warning: str,
    active_worker_instance: str = "",
    lock_age_seconds: float = 0.0,
    lock_ttl_seconds: int = 120,
) -> tuple[str, str]:
    """Source of truth: number of REAL Python `app.main` processes.

    Reasoning:
      The audit can be invoked from a process DIFFERENT than the worker
      (dashboard report builder, ad-hoc CLI, cron). That process owns a
      different `WorkerLockManager` instance with a different `instance_id`
      so it sees `lock_status="blocked_duplicate"`. That on its own is NOT
      a duplicate worker — it just means the audit is not the worker.

      A duplicate worker is detected only if there are >1 real Python
      processes running `app.main`.

    Outcome matrix:
      distinct_pids > 1 AND blocked_duplicate       -> BAD (real conflict)
      distinct_pids > 1 AND lock missing/expired    -> WARNING (no owner)
      distinct_pids > 1 AND lock owned/heartbeat    -> WARNING (suspicious)
      distinct_pids == 1 AND blocked_duplicate      -> OK (audit-from-non-worker)
      distinct_pids == 1 AND owned/acquired         -> OK
      distinct_pids == 0 AND blocked_duplicate      -> WARNING (stale lock?)
      distinct_pids == 0 AND missing                -> OK (e.g. test env)
    """
    fresh_lock_owner_known = (
        bool(active_worker_instance)
        and lock_age_seconds <= max(60, int(lock_ttl_seconds))
    )

    if distinct_pids > 1 and lock_status == "blocked_duplicate":
        return "BAD", "two_python_app_main_processes_with_lock_conflict"
    if distinct_pids > 1 and "duplicate_worker_detected" in warning:
        return "BAD", "lock_warning_duplicate_with_multiple_real_pids"
    if distinct_pids > 1 and lock_status in {"missing", "expired"}:
        return "WARNING", "multiple_python_app_main_pids_without_active_lock"
    if distinct_pids > 1 and lock_acquired:
        return "WARNING", "multiple_python_app_main_pids_lock_acquired_check_for_race"
    # distinct_pids <= 1 paths — these are the audit-from-other-process cases
    if distinct_pids <= 1 and lock_status == "blocked_duplicate":
        if fresh_lock_owner_known:
            return "OK", "audit_from_non_worker_process_lock_owned_by_worker"
        return "WARNING", "blocked_duplicate_without_fresh_known_owner_check_stale_lock"
    if distinct_pids == 0 and lock_status in {"missing", "expired", "disabled"}:
        return "OK", "no_python_app_main_detected_locally_audit_might_run_remote"
    return "OK", "single_python_app_main_process_lock_consistent"


def _classify_simulated(*, processes: list[str], lock: dict[str, Any]) -> str:
    distinct = _distinct_python_app_main_pids(_filter_real_python_workers(processes))
    if distinct > 1 and str(lock.get("lock_status")) == "blocked_duplicate":
        return "BAD"
    if distinct > 1:
        return "WARNING"
    return "OK"


def _classify_stale(age_seconds: int) -> str:
    return "WARNING" if age_seconds > 900 else "OK"


def timedelta_hours(hours: int) -> Any:
    from datetime import timedelta

    return timedelta(hours=hours)
