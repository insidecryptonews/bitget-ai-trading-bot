"""Persistent 48-hour edge sprint coordinator.

The coordinator is artifact-only and intentionally does not launch collectors,
execute orders, mutate active policies, or tune on the sealed holdout. The local
storage scheduler is the sole owner of cadence and process mutual exclusion.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .cross_venue import REPO_ROOT
from .isolated_research_demos import (
    DiagnosticDemoLedger,
    edge_demo_status,
    ensure_diagnostic_demo,
)
from .project_memory_contract import (
    DECISION_LEDGER_PATH,
    append_research_decision,
    contract_status,
)
from .storage_remote_restore_guard import disk_guard_status, remote_restore_status


CONFIG_PATH = REPO_ROOT / "config" / "research" / "EDGE_SPRINT_48H.json"
RUNTIME_ROOT = REPO_ROOT / "data" / "runtime" / "edge_sprint_48h"
STATE_PATH = RUNTIME_ROOT / "sprint_state.json"
STATUS_PATH = RUNTIME_ROOT / "sprint_status.json"
HOLDOUT_SEAL_PATH = RUNTIME_ROOT / "holdout_seal.json"
LOCK_PATH = RUNTIME_ROOT / "sprint_cycle.lock"
REPORT_ROOT = REPO_ROOT / "reports" / "research" / "48h_edge_sprint"

ATI_PATH = REPO_ROOT / "reports" / "research" / "ati" / "ati_forward_state.json"
P11_PATH = REPO_ROOT / "reports" / "research" / "p11_short_forward_observer" / "observer_status.json"
CROSS_PATH = REPO_ROOT / "data" / "runtime" / "cross_venue" / "dashboard_snapshot.json"
STORAGE_PATH = REPO_ROOT / "data" / "runtime" / "storage_efficiency_v2" / "storage_status.json"
CHALLENGER_PATH = REPO_ROOT / "data" / "runtime" / "storage_efficiency_v2" / "challenger_status.json"
SCHEDULER_PATH = REPO_ROOT / "data" / "runtime" / "storage_efficiency_v2" / "scheduler_status.json"
ATI_PAPER_PATH = REPO_ROOT / "data" / "runtime" / "ati_paper" / "executor_status.json"
COLLECTOR_ROOT = REPO_ROOT / "external_data" / "staging" / "cross_venue_v1"
RUNTIME_HEARTBEAT_LEDGER_PATH = RUNTIME_ROOT / "runtime_heartbeats.jsonl"
REQUIRED_COLLECTOR_VENUES = ("bitget", "binance", "bybit", "okx", "hyperliquid")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode("ascii")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("EDGE_SPRINT_SYMLINK_BLOCKED")
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


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
            timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"


def safety() -> dict[str, Any]:
    return {
        "research_only": True,
        "simulation_only": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "private_endpoints_used": False,
        "orders_sent": 0,
        "active_policy_modified": False,
        "auto_promotion": False,
        "final_recommendation": "NO LIVE",
    }


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = _read_json(path, {}) or {}
    if config.get("schema") != "edge_sprint_48h.config.v1":
        raise ValueError("EDGE_SPRINT_CONFIG_INVALID")
    locked = {
        "duration_hours": 48,
        "target_active_runtime_seconds": 172800,
        "snapshot_interval_hours": 6,
        "runtime_heartbeat_max_gap_seconds": 900,
        "collector_heartbeat_max_age_seconds": 180,
        "runtime_clock_skew_tolerance_seconds": 30,
        "migration_snapshot_gap_grace_seconds": 900,
        "holdout_max_accesses": 1,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "auto_promotion": False,
        "final_recommendation": "NO LIVE",
    }
    for key, expected in locked.items():
        if config.get(key) != expected:
            raise ValueError(f"EDGE_SPRINT_CONFIG_UNSAFE:{key}")
    if len(config.get("families") or []) != 7:
        raise ValueError("EDGE_SPRINT_FAMILY_REGISTRY_INCOMPLETE")
    return config


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("EDGE_SPRINT_SYMLINK_BLOCKED")
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _collector_progress_marker(
    dataset: dict[str, Any], storage: dict[str, Any], collector_rows: dict[str, Any],
) -> dict[str, Any]:
    return {
        "dataset_hash": dataset.get("dataset_hash"),
        "dataset_rows": _safe_int(dataset.get("total_feature_rows")),
        "verified_feature_files": _safe_int(dataset.get("verified_feature_files")),
        "raw_logical_bytes": _safe_int(storage.get("raw_logical_bytes")),
        "raw_physical_bytes": _safe_int(storage.get("raw_physical_bytes") or storage.get("physical_bytes")),
        "collectors": {
            venue: {
                "normalized_events": _safe_int(row.get("normalized_events")),
                "storage_rows": _safe_int(row.get("storage_rows_this_process")),
                "stream_size_bytes": _safe_int(row.get("stream_size_bytes")),
                "last_hash": row.get("last_hash"),
                "session_started_at": row.get("session_started_at"),
            }
            for venue, row in sorted(collector_rows.items())
        },
    }


def _marker_advanced(previous: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    if not isinstance(previous, dict) or not previous:
        return False
    for key in ("dataset_rows", "verified_feature_files", "raw_logical_bytes"):
        if _safe_int(current.get(key)) > _safe_int(previous.get(key)):
            return True
    if current.get("dataset_hash") and current.get("dataset_hash") != previous.get("dataset_hash"):
        return True
    old_collectors = previous.get("collectors") if isinstance(previous.get("collectors"), dict) else {}
    new_collectors = current.get("collectors") if isinstance(current.get("collectors"), dict) else {}
    for venue, row in new_collectors.items():
        old = old_collectors.get(venue) if isinstance(old_collectors.get(venue), dict) else {}
        for key in ("normalized_events", "storage_rows", "stream_size_bytes"):
            if _safe_int(row.get(key)) > _safe_int(old.get(key)):
                return True
        if row.get("last_hash") and row.get("last_hash") != old.get("last_hash"):
            return True
    return False


def _runtime_evidence(
    *, now: datetime, dataset: dict[str, Any], config: dict[str, Any],
    scheduler_path: Path = SCHEDULER_PATH, storage_path: Path = STORAGE_PATH,
    collector_root: Path = COLLECTOR_ROOT, previous_marker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scheduler = _read_json(scheduler_path, {}) or {}
    storage = _read_json(storage_path, {}) or {}
    max_collector_age = float(config["collector_heartbeat_max_age_seconds"])
    max_scheduler_age = float(config["runtime_heartbeat_max_gap_seconds"])
    clock_skew_tolerance = float(config["runtime_clock_skew_tolerance_seconds"])
    scheduler_started = _parse_time(scheduler.get("started_at"))
    scheduler_age = (now - scheduler_started).total_seconds() if scheduler_started else math.inf
    scheduler_alive = bool(
        scheduler.get("status") == "RUNNING"
        and scheduler.get("collectors_healthy") is True
        and -clock_skew_tolerance <= scheduler_age <= max_scheduler_age
    )
    collectors: dict[str, Any] = {}
    collector_blockers: list[str] = []
    for venue in REQUIRED_COLLECTOR_VENUES:
        path = collector_root / venue / "health.json"
        row = _read_json(path, {}) or {}
        heartbeat = _parse_time(row.get("heartbeat_at"))
        age = (now - heartbeat).total_seconds() if heartbeat else math.inf
        healthy = bool(
            row.get("status") not in {None, "ERROR", "FAILED", "HALTED"}
            and row.get("connected") is True
            and -clock_skew_tolerance <= age <= max_collector_age
            and row.get("uses_private_endpoints") is not True
            and row.get("can_send_real_orders") is not True
        )
        collectors[venue] = {**row, "heartbeat_age_seconds": age, "runtime_healthy": healthy}
        if not healthy:
            collector_blockers.append(f"COLLECTOR_NOT_RUNTIME_HEALTHY:{venue}")
    marker = _collector_progress_marker(dataset, storage, collectors)
    data_growing = _marker_advanced(previous_marker, marker)
    blockers = list(collector_blockers)
    if not scheduler_alive:
        blockers.append("SCHEDULER_HEARTBEAT_NOT_VALID")
    if not data_growing:
        blockers.append("DATA_PROGRESS_NOT_CONFIRMED")
    stack_healthy = scheduler_alive and not collector_blockers
    return {
        "schema": "edge_sprint_runtime_evidence.v2",
        "observed_at": now.isoformat(),
        "scheduler_alive": scheduler_alive,
        "scheduler_status": scheduler.get("status"),
        "scheduler_cycle": scheduler.get("cycle"),
        "scheduler_heartbeat_age_seconds": scheduler_age,
        "collectors_healthy": not collector_blockers,
        "collector_status": {
            venue: {
                "status": row.get("status"),
                "connected": row.get("connected"),
                "heartbeat_age_seconds": row.get("heartbeat_age_seconds"),
                "runtime_healthy": row.get("runtime_healthy"),
            }
            for venue, row in collectors.items()
        },
        "stack_healthy": stack_healthy,
        "data_growing": data_growing,
        "runtime_qualified": stack_healthy and data_growing,
        "progress_marker": marker,
        "progress_marker_hash": _sha(marker),
        "blockers": blockers,
    }


def _snapshot_runtime_qualified(snapshot: dict[str, Any]) -> bool:
    dataset = snapshot.get("dataset") if isinstance(snapshot.get("dataset"), dict) else {}
    scheduler = snapshot.get("scheduler") if isinstance(snapshot.get("scheduler"), dict) else {}
    contract = snapshot.get("contract") if isinstance(snapshot.get("contract"), dict) else {}
    return bool(
        dataset.get("dataset_hash")
        and scheduler.get("status") in {"RUNNING", "COMPLETED"}
        and scheduler.get("collectors_healthy") is True
        and contract.get("guardrails_status") == "PASS"
    )


def _migrate_active_runtime_state(
    state: dict[str, Any], *, now: datetime, config: dict[str, Any], report_root: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if state.get("runtime_accounting_version") == "ACTIVE_RUNTIME_V2":
        return state, None
    before_hash = _sha(state)
    snapshot_dir = report_root / str(state.get("sprint_id")) / "snapshots"
    snapshots: list[dict[str, Any]] = []
    for path in sorted(snapshot_dir.glob("snapshot_*.json")) if snapshot_dir.is_dir() else []:
        row = _read_json(path, {}) or {}
        captured = _parse_time(row.get("captured_at"))
        if captured:
            snapshots.append({"path": path.name, "captured_at": captured, "payload": row})
    credited = 0.0
    intervals: list[dict[str, Any]] = []
    max_gap = float(config["snapshot_interval_hours"]) * 3600.0 + float(
        config["migration_snapshot_gap_grace_seconds"]
    )
    for left, right in zip(snapshots, snapshots[1:]):
        gap = (right["captured_at"] - left["captured_at"]).total_seconds()
        left_data = left["payload"].get("dataset") or {}
        right_data = right["payload"].get("dataset") or {}
        grew = bool(
            right_data.get("dataset_hash") != left_data.get("dataset_hash")
            or _safe_int(right_data.get("total_feature_rows")) > _safe_int(left_data.get("total_feature_rows"))
            or _safe_int(right_data.get("verified_feature_files")) > _safe_int(left_data.get("verified_feature_files"))
        )
        qualified = bool(
            0 < gap <= max_gap
            and _snapshot_runtime_qualified(left["payload"])
            and _snapshot_runtime_qualified(right["payload"])
            and grew
        )
        if qualified:
            credited += gap
        intervals.append({
            "from": left["captured_at"].isoformat(),
            "to": right["captured_at"].isoformat(),
            "seconds": gap,
            "dataset_grew": grew,
            "credited": qualified,
        })
    target = int(config["target_active_runtime_seconds"])
    start = _parse_time(state.get("started_at")) or now
    original_end = state.get("original_planned_wall_clock_end") or state.get("planned_end_at")
    migration = {
        "schema": "edge_sprint_active_runtime_migration.v2",
        "migrated_at": now.isoformat(),
        "method": "CONSERVATIVE_VALID_SNAPSHOT_INTERVALS_ONLY",
        "pre_migration_state_sha256": before_hash,
        "sprint_id_preserved": state.get("sprint_id"),
        "original_started_at": state.get("started_at"),
        "original_planned_wall_clock_end": original_end,
        "original_commit": state.get("commit"),
        "original_tree": state.get("tree"),
        "original_config_hash": state.get("config_hash"),
        "snapshot_count_preserved": _safe_int(state.get("snapshot_count")),
        "holdout_accesses_preserved": _safe_int(state.get("holdout_access_count")),
        "snapshots_evaluated": len(snapshots),
        "credited_active_runtime_seconds": int(credited),
        "intervals": intervals,
        "unproven_wall_time_credited": False,
    }
    state.update({
        "schema": "edge_sprint_48h.state.v2",
        "runtime_accounting_version": "ACTIVE_RUNTIME_V2",
        "target_active_runtime_seconds": target,
        "accumulated_active_runtime_seconds": min(target, int(credited)),
        "active_runtime_remaining_seconds": max(0, target - int(credited)),
        "current_session_started_at": None,
        "current_session_runtime_seconds": 0,
        "last_runtime_observation_at": None,
        "last_valid_heartbeat_at": None,
        "runtime_progress_marker": None,
        "last_runtime_qualified": False,
        "runtime_state": "WAITING_FOR_VALID_HEARTBEAT",
        "explicit_pause": False,
        "paused_at": None,
        "resume_count": 0,
        "shutdown_count": 0,
        "wall_clock_elapsed_seconds": max(0, int((now - start).total_seconds())),
        "original_planned_wall_clock_end": original_end,
        "estimated_completion_at_if_continuous": (now + timedelta(seconds=max(0, target - int(credited)))).isoformat(),
        "actual_completion_at": None,
        "last_snapshot_active_runtime_seconds": min(target, int(credited)),
        "migration": migration,
        "migration_decision_recorded": False,
    })
    return state, migration


def _persist_active_runtime_migration(
    state: dict[str, Any], migration: dict[str, Any] | None, *,
    migration_path: Path, decision_path: Path,
) -> None:
    if not migration:
        return
    _atomic_json(migration_path, migration)
    if state.get("migration_decision_recorded"):
        return
    append_research_decision({
        "dataset_hash": state.get("current_dataset_hash"),
        "commit": _git("rev-parse", "HEAD"),
        "hypothesis": "SPRINT_ACTIVE_RUNTIME_ACCOUNTING",
        "proposed_change": "WALL_CLOCK_TO_ACCUMULATED_VALID_RUNTIME",
        "reason": "PC_OFF_AND_SUSPEND_TIME_MUST_NOT_COUNT",
        "result": "MIGRATED_CONSERVATIVELY_WITHOUT_HOLDOUT_ACCESS",
        "tests": ["PENDING_FINAL_VALIDATION"],
        "final_state": "RESEARCH_ONLY_NO_LIVE",
    }, path=decision_path)
    state["migration_decision_recorded"] = True


def _update_active_runtime(
    state: dict[str, Any], *, now: datetime, evidence: dict[str, Any], config: dict[str, Any],
) -> dict[str, Any]:
    target = int(config["target_active_runtime_seconds"])
    accumulated = min(target, _safe_int(state.get("accumulated_active_runtime_seconds")))
    increment = 0
    last_observation = _parse_time(state.get("last_runtime_observation_at"))
    delta = (now - last_observation).total_seconds() if last_observation else None
    prior_stack_healthy = state.get("last_runtime_stack_healthy") is True
    explicit_pause = state.get("explicit_pause") is True
    max_gap = float(config["runtime_heartbeat_max_gap_seconds"])
    previous_runtime_state = str(state.get("runtime_state") or "")
    if explicit_pause:
        runtime_state = "PAUSED_EXPLICIT"
        state["current_session_started_at"] = None
    elif evidence.get("stack_healthy") is not True:
        runtime_state = "PAUSED_RUNTIME_NOT_QUALIFIED"
        if previous_runtime_state == "RUNNING":
            state["shutdown_count"] = _safe_int(state.get("shutdown_count")) + 1
        state["paused_at"] = state.get("paused_at") or now.isoformat()
        state["current_session_started_at"] = None
        state["current_session_runtime_seconds"] = 0
    else:
        if delta is not None and delta > max_gap:
            if previous_runtime_state == "RUNNING":
                state["shutdown_count"] = _safe_int(state.get("shutdown_count")) + 1
            state["resume_count"] = _safe_int(state.get("resume_count")) + 1
            state["current_session_started_at"] = now.isoformat()
            state["current_session_runtime_seconds"] = 0
            state["paused_at"] = last_observation.isoformat() if last_observation else state.get("paused_at")
            runtime_state = "RESUMED_AFTER_UNCOUNTED_GAP"
        else:
            if not state.get("current_session_started_at"):
                if state.get("last_runtime_observation_at") or state.get("paused_at"):
                    state["resume_count"] = _safe_int(state.get("resume_count")) + 1
                state["current_session_started_at"] = now.isoformat()
                state["current_session_runtime_seconds"] = 0
            if (
                delta is not None and 0 <= delta <= max_gap and prior_stack_healthy
                and evidence.get("data_growing") is True
            ):
                increment = int(delta)
                accumulated = min(target, accumulated + increment)
                state["current_session_runtime_seconds"] = (
                    _safe_int(state.get("current_session_runtime_seconds")) + increment
                )
                state["last_valid_heartbeat_at"] = now.isoformat()
                runtime_state = "RUNNING"
            else:
                runtime_state = "WAITING_FOR_DATA_GROWTH"
    start = _parse_time(state.get("started_at")) or now
    remaining = max(0, target - accumulated)
    snapshot_interval = int(float(config["snapshot_interval_hours"]) * 3600)
    last_snapshot_active = _safe_int(state.get("last_snapshot_active_runtime_seconds"))
    next_snapshot_active = min(target, last_snapshot_active + snapshot_interval)
    state.update({
        "target_active_runtime_seconds": target,
        "accumulated_active_runtime_seconds": accumulated,
        "active_runtime_remaining_seconds": remaining,
        "active_runtime_increment_seconds": increment,
        "wall_clock_elapsed_seconds": max(0, int((now - start).total_seconds())),
        "estimated_completion_at_if_continuous": (now + timedelta(seconds=remaining)).isoformat(),
        "runtime_state": runtime_state,
        "last_runtime_observation_at": now.isoformat(),
        "last_runtime_stack_healthy": evidence.get("stack_healthy") is True,
        "last_runtime_qualified": increment > 0,
        "runtime_progress_marker": evidence.get("progress_marker"),
        "runtime_qualification": evidence,
        "next_runtime_qualified_cycle_active_seconds": next_snapshot_active,
        "next_runtime_qualified_cycle_at_if_continuous": (
            now + timedelta(seconds=max(0, next_snapshot_active - accumulated))
        ).isoformat(),
        "pc_off_time_counts": False,
    })
    return state


@contextmanager
def _cycle_lock(path: Path = LOCK_PATH) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    try:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(f"{os.getpid()}\n")
                handle.flush()
                os.fsync(handle.fileno())
            acquired = True
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                age = 0
            if age > 60 * 60:
                path.unlink(missing_ok=True)
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                    handle.write(f"{os.getpid()}\n")
                acquired = True
        yield acquired
    finally:
        if acquired:
            path.unlink(missing_ok=True)


def _dataset_snapshot() -> dict[str, Any]:
    try:
        from .continuous_edge_research_challenger import FEATURE_MANIFEST_PATH, _dataset_contract

        paths, dataset_hash, source_ids = _dataset_contract(FEATURE_MANIFEST_PATH)
        manifest = _read_json(FEATURE_MANIFEST_PATH, {}) or {}
        total_rows = sum(
            max(0, int(output.get("rows") or 0))
            for record in (manifest.get("segments") or {}).values()
            if isinstance(record, dict) and record.get("status") == "VERIFIED_FEATURES"
            for output in (record.get("outputs") or [])
            if isinstance(output, dict)
        )
        return {
            "status": "OK" if paths else "NEED_MORE_DATA",
            "dataset_hash": dataset_hash,
            "verified_feature_files": len(paths),
            "source_partition_ids": source_ids,
            "total_feature_rows": total_rows,
        }
    except Exception as exc:
        return {
            "status": "NEED_MORE_DATA", "dataset_hash": None,
            "reason": f"DATASET_CONTRACT:{type(exc).__name__}",
            "verified_feature_files": 0, "total_feature_rows": 0,
        }


def _funnel_snapshot() -> dict[str, Any]:
    ati = _read_json(ATI_PATH, {}) or {}
    ati_paper = _read_json(ATI_PAPER_PATH, {}) or {}
    p11 = _read_json(P11_PATH, {}) or {}
    cross = _read_json(CROSS_PATH, {}) or {}
    metrics = p11.get("metrics") if isinstance(p11.get("metrics"), dict) else {}
    leadlag = cross.get("leadlag") if isinstance(cross.get("leadlag"), dict) else {}
    counts = leadlag.get("evaluation_counts") if isinstance(leadlag.get("evaluation_counts"), dict) else {}
    recent = leadlag.get("recent_episodes") if isinstance(leadlag.get("recent_episodes"), list) else []
    true_consensus = sum(
        1 for row in recent
        if isinstance(row, dict) and len(row.get("leader_venues") or []) >= 2
    )
    return {
        "ati_shadow": {
            "observations": int(ati.get("new_forward_candidates_seen") or 0),
            "signals": int(ati.get("signals_total") or 0),
            "blocked_signals": int(ati.get("paper_feed_blocked_signals") or 0),
            "eligible_signals": int(ati.get("paper_feed_eligible_signals") or 0),
            "closed_outcomes": int(ati.get("closed_outcomes") or 0),
            "open_positions": int(ati.get("open_positions") or 0),
            "reconciliation": (ati.get("reconciliation") or {}).get("status") or ati.get("reconciliation_status"),
            "timing_metrics_status": "NEED_MORE_DATA" if not ati.get("timing_metrics") else "AVAILABLE",
        },
        "ati_paper": {
            "trades": int(ati_paper.get("closed_trades") or 0),
            "open_positions": int(ati_paper.get("open_positions") or 0),
            "realized_equity": ati_paper.get("realized_equity"),
            "total_equity": ati_paper.get("total_equity"),
            "reconciliation": (
                (ati_paper.get("reconciliation") or {}).get("status")
                if isinstance(ati_paper.get("reconciliation"), dict)
                else ati_paper.get("reconciliation")
            ),
        },
        "p11": {
            "opportunities": int(metrics.get("forward_opportunities") or 0),
            "rejections": int(metrics.get("forward_rejections") or 0),
            "signals": int(metrics.get("forward_signals") or 0),
            "entries": int(metrics.get("forward_entries") or 0),
            "closed_outcomes": int(metrics.get("forward_closed_outcomes") or 0),
            "time_exits": int(metrics.get("time_exits") or 0),
            "mfe": metrics.get("MFE"), "mae": metrics.get("MAE"),
            "net_pnl": metrics.get("net_pnl"),
            "signal_age_seconds": metrics.get("observer_lag_seconds"),
            "reconciliation": metrics.get("reconciliation_status"),
        },
        "cross_venue": {
            "raw_evaluations": int(counts.get("raw_evaluations") or 0),
            "duplicate_evaluations": int(counts.get("duplicate_evaluations") or 0),
            "unique_episodes": int(counts.get("unique_market_episodes") or 0),
            "true_consensus_recent": true_consensus,
            "insufficient_consensus": int(counts.get("rejected_no_consensus") or 0),
            "cost_rejections": int(counts.get("rejected_costs") or 0),
            "stale_rejections": int(counts.get("rejected_stale") or 0),
            "contract_mismatch": int(counts.get("rejected_contract_mismatch") or 0),
            "accepted_simulated_signals": int(counts.get("accepted_simulated_signals") or 0),
            "paper_trades": len(cross.get("trades") or []),
            "reconciliation": (cross.get("reconciliation") or {}).get("status"),
        },
    }


def _population_counters(
    funnels: dict[str, Any], diagnostic: dict[str, Any], edge_demo: dict[str, Any],
) -> dict[str, int]:
    cross = funnels["cross_venue"]
    p11 = funnels["p11"]
    ati_shadow = funnels["ati_shadow"]
    ati_paper = funnels["ati_paper"]
    return {
        "raw_evaluations": int(cross["raw_evaluations"]),
        "unique_episodes": int(cross["unique_episodes"]),
        "ati_shadow_forward_signals": int(ati_shadow["signals"]),
        "ati_shadow_forward_outcomes": int(ati_shadow["closed_outcomes"]),
        "ati_paper_trades": int(ati_paper["trades"]),
        "ati_paper_open_positions": int(ati_paper["open_positions"]),
        "p11_outcomes": int(p11["closed_outcomes"]),
        "cross_venue_paper_trades": int(cross["paper_trades"]),
        "diagnostic_trades": int(diagnostic.get("trades") or 0),
        "candidate_demo_trades": int(edge_demo.get("trades") or 0),
        "candidate_validation_fills": 0,
        "forward_demo_trades": 0,
    }


def _candidate_seal(challenger: dict[str, Any], dataset_hash: str | None, config_hash: str) -> dict[str, Any]:
    candidates = challenger.get("candidates") if isinstance(challenger.get("candidates"), list) else []
    specs = [
        {"trial_id": row.get("trial_id"), "family": row.get("family"), "spec_hash": _sha(row.get("spec") or {})}
        for row in candidates if isinstance(row, dict)
    ]
    body = {
        "schema": "edge_sprint_holdout_seal.v1",
        "created_at": utc_now(), "dataset_hash": dataset_hash,
        "config_hash": config_hash, "candidate_specs": specs,
        "status": "SEALED_NOT_EVALUATED", "access_count": 0,
        "max_accesses": 1, "retuning_after_access_allowed": False,
    }
    body["seal_hash"] = _sha(body)
    return body


def _final_holdout(
    challenger: dict[str, Any], dataset_hash: str | None, seal: dict[str, Any],
) -> dict[str, Any]:
    candidates = challenger.get("candidates") if isinstance(challenger.get("candidates"), list) else []
    candidate = candidates[0] if candidates else {}
    if candidate.get("state") != "WATCH_ONLY":
        return {"status": "NOT_ACCESSED_NO_WATCH_ONLY_CANDIDATE", "access_count": 0}
    registered = {
        (str(row.get("trial_id") or ""), str(row.get("family") or ""), str(row.get("spec_hash") or ""))
        for row in (seal.get("candidate_specs") or []) if isinstance(row, dict)
    }
    candidate_identity = (
        str(candidate.get("trial_id") or ""),
        str(candidate.get("family") or ""),
        _sha(candidate.get("spec") or {}),
    )
    if candidate_identity not in registered:
        return {"status": "NOT_ACCESSED_CANDIDATE_NOT_PREREGISTERED", "access_count": 0}
    if challenger.get("dataset_hash") != dataset_hash:
        return {"status": "NOT_ACCESSED_DATASET_HASH_MISMATCH", "access_count": 0}
    holdout = candidate.get("sealed_holdout") if isinstance(candidate.get("sealed_holdout"), dict) else {}
    spec = candidate.get("spec") if isinstance(candidate.get("spec"), dict) else {}
    if not spec or int(holdout.get("access_count") or 0) != 0:
        return {"status": "NOT_ACCESSED_SEAL_INVALID", "access_count": 0}
    try:
        from .continuous_edge_research_challenger import (
            FEATURE_MANIFEST_PATH,
            _augment_prefix_features,
            _evaluate,
            load_feature_rows,
            load_storage_config,
        )

        rows, loaded = load_feature_rows(
            FEATURE_MANIFEST_PATH,
            max_rows=int(load_storage_config().get("challenger_max_feature_rows", 500_000)),
        )
        if loaded.get("dataset_hash") != dataset_hash:
            return {"status": "NOT_ACCESSED_DATASET_HASH_MISMATCH", "access_count": 0}
        _augment_prefix_features(rows)
        result = _evaluate(
            rows, spec, start_ms=int(holdout["start_ms"]), end_ms=int(holdout["end_ms"]),
            trials_total=int(challenger.get("trials") or 1), seed=480104,
        )
        base = result["cost_scenarios"]["15.5"]
        stress = result["cost_scenarios"]["18.0"]
        enough = int(base.get("trades") or 0) >= 100 and float(base.get("n_eff") or 0) >= 100
        passed = bool(
            enough and float(base.get("net_ev_bps") or -math.inf) > 0
            and float(base.get("net_ev_lower_bound_bps") or -math.inf) >= 0
            and float(base.get("profit_factor") or 0) > 1
            and float(stress.get("net_ev_bps") or -math.inf) > 0
        )
        return {
            "status": "PASS" if passed else "FAIL" if enough else "NEED_MORE_DATA",
            "access_count": 1, "candidate": candidate.get("trial_id"),
            "cost_scenarios": result["cost_scenarios"],
            "evaluated_once_no_retuning": True,
        }
    except Exception as exc:
        return {
            "status": "ACCESS_FAILED_FAIL_CLOSED", "access_count": 1,
            "error": type(exc).__name__, "evaluated_once_no_retuning": True,
        }


def _write_final_report(state: dict[str, Any], report_root: Path = REPORT_ROOT) -> dict[str, str]:
    output = report_root / str(state["sprint_id"])
    output.mkdir(parents=True, exist_ok=True)
    payload = {**state, "report_generated_at": utc_now()}
    json_path = output / "FINAL_REPORT.json"
    md_path = output / "FINAL_REPORT.md"
    _atomic_json(json_path, payload)
    lines = [
        "# 48H Edge Sprint Final Report", "",
        f"- Sprint: {state['sprint_id']}",
        f"- Status: {state.get('status')}",
        f"- Strategy verdict: {state.get('strategy_verdict')}",
        f"- Active runtime seconds: {state.get('accumulated_active_runtime_seconds')}",
        f"- Active runtime target seconds: {state.get('target_active_runtime_seconds')}",
        f"- Wall-clock elapsed seconds: {state.get('wall_clock_elapsed_seconds')}",
        f"- Original planned wall-clock end: {state.get('original_planned_wall_clock_end')}",
        f"- Actual completion: {state.get('actual_completion_at')}",
        f"- Dataset hash: {state.get('current_dataset_hash')}",
        f"- Holdout: {(state.get('holdout') or {}).get('status')}",
        f"- Diagnostic demo: {(state.get('diagnostic_demo') or {}).get('status')}",
        f"- Edge demo: {(state.get('edge_candidate_demo') or {}).get('status')}",
        "- Populations remain separate: true",
        "- SIMULATION ONLY", "- PAPER_TRADING=True", "- LIVE_TRADING=False",
        "- DRY_RUN=True", "- ENABLE_PAPER_POLICY_FILTER=False",
        "- can_send_real_orders=false", "- FINAL_RECOMMENDATION: NO LIVE", "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return {"json": str(json_path), "markdown": str(md_path)}


def sprint_status(path: Path = STATUS_PATH) -> dict[str, Any]:
    value = _read_json(path, {}) or {}
    if value:
        return value
    return {
        "status": "NOT_STARTED", "strategy_verdict": "NEED_MORE_FORWARD_DATA",
        "sprint_id": None, "snapshots": 0, "holdout_accesses": 0,
        **safety(),
    }


def _finalization_blockers(
    state: dict[str, Any], funnels: dict[str, Any], diagnostic: dict[str, Any],
    contract: dict[str, Any], evidence: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    if contract.get("guardrails_status") != "PASS":
        blockers.append("PROJECT_MEMORY_CONTRACT_NOT_PASS")
    if evidence.get("runtime_qualified") is not True:
        blockers.append("FINAL_RUNTIME_INTERVAL_NOT_QUALIFIED")
    target = _safe_int(state.get("target_active_runtime_seconds"))
    if _safe_int(state.get("last_snapshot_active_runtime_seconds")) < target:
        blockers.append("FINAL_RUNTIME_SNAPSHOT_MISSING")
    reconciliation = {
        "ati_shadow": (funnels.get("ati_shadow") or {}).get("reconciliation"),
        "ati_paper": (funnels.get("ati_paper") or {}).get("reconciliation"),
        "p11": (funnels.get("p11") or {}).get("reconciliation"),
        "cross_venue": (funnels.get("cross_venue") or {}).get("reconciliation"),
        "diagnostic": diagnostic.get("reconciliation"),
    }
    for component, status in reconciliation.items():
        if status != "PASS":
            blockers.append(f"RECONCILIATION_NOT_PASS:{component}:{status or 'UNKNOWN'}")
    return blockers


def pause_sprint_session(
    *, reason: str = "USER_REQUESTED_SHUTDOWN", now: datetime | None = None,
    state_path: Path = STATE_PATH, status_path: Path = STATUS_PATH,
    report_root: Path = REPORT_ROOT,
    migration_path: Path = RUNTIME_ROOT / "active_runtime_migration_v2.json",
    decision_path: Path = DECISION_LEDGER_PATH, lock_path: Path = LOCK_PATH,
) -> dict[str, Any]:
    with _cycle_lock(lock_path) as acquired:
        if not acquired:
            return {"status": "BLOCKED_SPRINT_CYCLE_IN_PROGRESS", **safety()}
        return _pause_sprint_session_unlocked(
            reason=reason, now=now, state_path=state_path, status_path=status_path,
            report_root=report_root, migration_path=migration_path, decision_path=decision_path,
        )


def _pause_sprint_session_unlocked(
    *, reason: str = "USER_REQUESTED_SHUTDOWN", now: datetime | None = None,
    state_path: Path = STATE_PATH, status_path: Path = STATUS_PATH,
    report_root: Path = REPORT_ROOT,
    migration_path: Path = RUNTIME_ROOT / "active_runtime_migration_v2.json",
    decision_path: Path = DECISION_LEDGER_PATH,
) -> dict[str, Any]:
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state = _read_json(state_path, {}) or {}
    if not state:
        return {"status": "NOT_STARTED", **safety()}
    state, migration = _migrate_active_runtime_state(
        state, now=current_time, config=load_config(), report_root=report_root,
    )
    _persist_active_runtime_migration(
        state, migration, migration_path=migration_path, decision_path=decision_path,
    )
    already_paused = state.get("explicit_pause") is True
    state.update({
        "explicit_pause": True,
        "runtime_state": "PAUSED_EXPLICIT",
        "status": "PAUSED",
        "paused_at": current_time.isoformat(),
        "pause_reason": str(reason or "USER_REQUESTED_SHUTDOWN")[:200],
        "current_session_started_at": None,
        "last_runtime_observation_at": None,
        "last_runtime_stack_healthy": False,
        "last_runtime_qualified": False,
        "runtime_progress_marker": None,
        "shutdown_count": _safe_int(state.get("shutdown_count")) + (0 if already_paused else 1),
        "updated_at": current_time.isoformat(),
        **safety(),
    })
    _atomic_json(state_path, state)
    _atomic_json(status_path, state)
    return state


def resume_sprint_session(
    *, now: datetime | None = None, state_path: Path = STATE_PATH,
    status_path: Path = STATUS_PATH, report_root: Path = REPORT_ROOT,
    migration_path: Path = RUNTIME_ROOT / "active_runtime_migration_v2.json",
    decision_path: Path = DECISION_LEDGER_PATH, lock_path: Path = LOCK_PATH,
) -> dict[str, Any]:
    with _cycle_lock(lock_path) as acquired:
        if not acquired:
            return {"status": "BLOCKED_SPRINT_CYCLE_IN_PROGRESS", **safety()}
        return _resume_sprint_session_unlocked(
            now=now, state_path=state_path, status_path=status_path,
            report_root=report_root, migration_path=migration_path, decision_path=decision_path,
        )


def _resume_sprint_session_unlocked(
    *, now: datetime | None = None, state_path: Path = STATE_PATH,
    status_path: Path = STATUS_PATH,
    report_root: Path = REPORT_ROOT,
    migration_path: Path = RUNTIME_ROOT / "active_runtime_migration_v2.json",
    decision_path: Path = DECISION_LEDGER_PATH,
) -> dict[str, Any]:
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state = _read_json(state_path, {}) or {}
    if not state:
        return {"status": "NOT_STARTED", **safety()}
    contract = contract_status()
    if contract.get("guardrails_status") != "PASS":
        return {
            "status": "BLOCKED_PROJECT_MEMORY_CONTRACT",
            "blockers": contract.get("violations") or ["PROJECT_MEMORY_GUARDRAILS_NOT_PASS"],
            **safety(),
        }
    state, migration = _migrate_active_runtime_state(
        state, now=current_time, config=load_config(), report_root=report_root,
    )
    _persist_active_runtime_migration(
        state, migration, migration_path=migration_path, decision_path=decision_path,
    )
    was_paused = state.get("explicit_pause") is True or not state.get("current_session_started_at")
    state.update({
        "explicit_pause": False,
        "runtime_state": "WAITING_FOR_VALID_HEARTBEAT",
        "status": "ACTIVE",
        "last_resumed_at": current_time.isoformat(),
        "current_session_started_at": current_time.isoformat(),
        "current_session_runtime_seconds": 0,
        "last_runtime_observation_at": None,
        "last_runtime_stack_healthy": False,
        "last_runtime_qualified": False,
        "runtime_progress_marker": None,
        "resume_count": _safe_int(state.get("resume_count")) + (1 if was_paused else 0),
        "updated_at": current_time.isoformat(),
        **safety(),
    })
    _atomic_json(state_path, state)
    _atomic_json(status_path, state)
    return state


def run_sprint_cycle(
    *, apply: bool = False, now: datetime | None = None,
    state_path: Path = STATE_PATH, status_path: Path = STATUS_PATH,
    holdout_path: Path = HOLDOUT_SEAL_PATH, report_root: Path = REPORT_ROOT,
    lock_path: Path = LOCK_PATH, heartbeat_path: Path = RUNTIME_HEARTBEAT_LEDGER_PATH,
    migration_path: Path = RUNTIME_ROOT / "active_runtime_migration_v2.json",
    decision_path: Path = DECISION_LEDGER_PATH,
) -> dict[str, Any]:
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    config = load_config()
    contract = contract_status()
    disk = disk_guard_status(write=apply)
    remote = remote_restore_status()
    dataset = _dataset_snapshot()
    challenger = _read_json(CHALLENGER_PATH, {}) or {}
    funnels = _funnel_snapshot()
    with _cycle_lock(lock_path) as acquired:
        if not acquired:
            return {"status": "SKIPPED_DUPLICATE_PROCESS", **safety()}
        previous = _read_json(state_path, {}) or {}
        if contract.get("guardrails_status") != "PASS":
            result = {
                "status": "BLOCKED_PROJECT_MEMORY_CONTRACT",
                "blockers": contract.get("violations") or ["PROJECT_MEMORY_GUARDRAILS_NOT_PASS"],
                "contract": contract, "disk_guard": disk,
                "strategy_verdict": "NEED_MORE_FORWARD_DATA", **safety(),
            }
            if apply:
                _atomic_json(status_path, result)
            return result
        if disk.get("level") == "ABSOLUTE_PROTECTION":
            result = {
                "status": "BLOCKED_ABSOLUTE_DISK_GUARD",
                "blockers": ["COOPERATIVE_COLLECTOR_STOP_REQUIRED"],
                "disk_guard": disk, "strategy_verdict": "NEED_MORE_FORWARD_DATA",
                **safety(),
            }
            if apply:
                _atomic_json(status_path, result)
            return result
        if not previous:
            start = current_time
            sprint_id = "48H_EDGE_SPRINT_" + start.strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid.uuid4().hex[:8]
            config_hash = _sha(config)
            seal = _candidate_seal(challenger, dataset.get("dataset_hash"), config_hash)
            previous = {
                "schema": "edge_sprint_48h.state.v2", "sprint_id": sprint_id,
                "started_at": start.isoformat(),
                "planned_end_at": (start + timedelta(hours=48)).isoformat(),
                "original_planned_wall_clock_end": (start + timedelta(hours=48)).isoformat(),
                "commit": _git("rev-parse", "HEAD"), "tree": _git("rev-parse", "HEAD^{tree}"),
                "config_hash": config_hash, "initial_dataset_hash": dataset.get("dataset_hash"),
                "last_analyzed_dataset_hash": None, "last_snapshot_at": None,
                "last_cycle_at": None, "snapshot_count": 0,
                "holdout_access_count": 0, "holdout": {"status": "SEALED_NOT_EVALUATED", "access_count": 0},
                "finalized": False, "families": config["families"],
                "runtime_accounting_version": "ACTIVE_RUNTIME_V2",
                "target_active_runtime_seconds": int(config["target_active_runtime_seconds"]),
                "accumulated_active_runtime_seconds": 0,
                "active_runtime_remaining_seconds": int(config["target_active_runtime_seconds"]),
                "current_session_started_at": None,
                "current_session_runtime_seconds": 0,
                "last_runtime_observation_at": None,
                "last_valid_heartbeat_at": None,
                "runtime_progress_marker": None,
                "last_runtime_qualified": False,
                "runtime_state": "WAITING_FOR_VALID_HEARTBEAT",
                "explicit_pause": False,
                "paused_at": None,
                "resume_count": 0,
                "shutdown_count": 0,
                "last_snapshot_active_runtime_seconds": 0,
                "actual_completion_at": None,
            }
            if apply:
                _atomic_json(holdout_path, seal)
        elif apply:
            seal = _read_json(holdout_path, {}) or {}
            claimed = str(seal.get("seal_hash") or "")
            seal_body = {key: value for key, value in seal.items() if key != "seal_hash"}
            if (
                not claimed or claimed != _sha(seal_body)
                or seal.get("config_hash") != previous.get("config_hash")
                or int(seal.get("max_accesses") or 0) != 1
                or int(seal.get("access_count") or 0) > 1
            ):
                result = {
                    "status": "BLOCKED_HOLDOUT_SEAL_INVALID",
                    "blockers": ["HOLDOUT_SEAL_MISSING_OR_TAMPERED"],
                    "strategy_verdict": "NEED_MORE_FORWARD_DATA",
                    **safety(),
                }
                _atomic_json(status_path, result)
                return result
        previous, migration = _migrate_active_runtime_state(
            previous, now=current_time, config=config, report_root=report_root,
        )
        if migration and apply:
            _persist_active_runtime_migration(
                previous, migration, migration_path=migration_path, decision_path=decision_path,
            )
        runtime_time = current_time if now is not None else datetime.now(timezone.utc)
        evidence = _runtime_evidence(
            now=runtime_time, dataset=dataset, config=config,
            scheduler_path=SCHEDULER_PATH, storage_path=STORAGE_PATH,
            collector_root=COLLECTOR_ROOT,
            previous_marker=previous.get("runtime_progress_marker"),
        )
        previous = _update_active_runtime(
            previous, now=runtime_time, evidence=evidence, config=config,
        )
        start_at = _parse_time(previous.get("started_at")) or current_time
        target_active = int(config["target_active_runtime_seconds"])
        active_runtime = _safe_int(previous.get("accumulated_active_runtime_seconds"))
        snapshot_interval = int(float(config["snapshot_interval_hours"]) * 3600)
        last_snapshot_active = _safe_int(previous.get("last_snapshot_active_runtime_seconds"))
        snapshot_due = (
            int(previous.get("snapshot_count") or 0) == 0
            or active_runtime >= min(target_active, last_snapshot_active + snapshot_interval)
        )
        dataset_changed = dataset.get("dataset_hash") != previous.get("last_analyzed_dataset_hash")
        analysis_eligible = bool(
            snapshot_due and dataset_changed and disk.get("allow_challenger")
            and dataset.get("dataset_hash")
        )
        diagnostic = ensure_diagnostic_demo(
            now_ms=int(start_at.timestamp() * 1000), write_status=apply,
            ledger=DiagnosticDemoLedger(RUNTIME_ROOT / "operability_diagnostic_demo.sqlite"),
        ) if apply else DiagnosticDemoLedger(RUNTIME_ROOT / "operability_diagnostic_demo.sqlite").status()
        edge_demo = edge_demo_status(challenger, write_status=True) if apply else {
            "status": "NO DEFENSIBLE CANDIDATE - DEMO NOT STARTED",
            "account_initialized": False, **safety(),
        }
        populations = _population_counters(funnels, diagnostic, edge_demo)
        snapshot = {
            "schema": "edge_sprint_48h.snapshot.v1", "captured_at": current_time.isoformat(),
            "active_runtime": {
                "accumulated_seconds": active_runtime,
                "target_seconds": target_active,
                "remaining_seconds": max(0, target_active - active_runtime),
                "runtime_state": previous.get("runtime_state"),
                "qualification": evidence,
            },
            "dataset": dataset, "dataset_changed": dataset_changed,
            "analysis_eligible": analysis_eligible,
            "analysis_executed_by_this_cycle": False,
            "contract": contract, "disk_guard": disk, "remote_restore": remote,
            "storage": _read_json(STORAGE_PATH, {}) or {},
            "scheduler": _read_json(SCHEDULER_PATH, {}) or {},
            "funnels": funnels, "populations": populations,
            "challenger": challenger, "diagnostic_demo": diagnostic,
            "edge_candidate_demo": edge_demo, **safety(),
        }
        if snapshot_due and apply:
            sequence = int(previous.get("snapshot_count") or 0) + 1
            snapshot_path = report_root / str(previous["sprint_id"]) / "snapshots" / f"snapshot_{sequence:03d}.json"
            _atomic_json(snapshot_path, snapshot)
            previous["snapshot_count"] = sequence
            previous["last_snapshot_at"] = current_time.isoformat()
            previous["last_snapshot_active_runtime_seconds"] = active_runtime
        if analysis_eligible:
            previous["last_analyzed_dataset_hash"] = dataset.get("dataset_hash")
        final_due = active_runtime >= target_active
        finalization_blockers = _finalization_blockers(
            previous, funnels, diagnostic, contract, evidence,
        ) if final_due else []
        if final_due and not previous.get("finalized"):
            if not apply:
                previous["holdout_preview"] = {
                    "status": "NOT_ACCESSED_DRY_RUN", "access_count": 0,
                }
            elif not finalization_blockers:
                seal = _read_json(holdout_path, {}) or {}
                holdout = _final_holdout(challenger, dataset.get("dataset_hash"), seal)
                previous["holdout"] = holdout
                previous["holdout_access_count"] = int(holdout.get("access_count") or 0)
                previous["finalized"] = True
                previous["actual_completion_at"] = current_time.isoformat()
                seal.update({
                    "status": holdout.get("status"),
                    "access_count": int(holdout.get("access_count") or 0),
                    "finalized_at": current_time.isoformat(),
                })
                seal.pop("seal_hash", None)
                seal["seal_hash"] = _sha(seal)
                _atomic_json(holdout_path, seal)
        gate = (edge_demo.get("gate") or {}) if isinstance(edge_demo, dict) else {}
        strategy_verdict = (
            "PROMISING WATCH_ONLY" if gate.get("status") == "ELIGIBLE_PENDING_HUMAN_REVIEW"
            else "NEED_MORE_FORWARD_DATA" if challenger.get("state") in {None, "NEED_MORE_DATA"}
            else "NO DEFENSIBLE EDGE FOUND" if challenger.get("status")
            else "NEED_MORE_FORWARD_DATA"
        )
        previous.update({
            "updated_at": current_time.isoformat(), "last_cycle_at": current_time.isoformat(),
            "current_dataset_hash": dataset.get("dataset_hash"),
            "new_verified_partitions": max(
                0, int(dataset.get("verified_feature_files") or 0)
                - int(previous.get("initial_verified_feature_files") or dataset.get("verified_feature_files") or 0),
            ),
            "initial_verified_feature_files": int(
                previous.get("initial_verified_feature_files") or dataset.get("verified_feature_files") or 0
            ),
            "dataset_changed": dataset_changed, "analysis_eligible": analysis_eligible,
            "analysis_executed_by_this_cycle": False,
            "next_cycle_at": previous.get("next_runtime_qualified_cycle_at_if_continuous"),
            "remaining_seconds": previous.get("active_runtime_remaining_seconds"),
            "finalization_blockers": finalization_blockers,
            "status": (
                "COMPLETED" if previous.get("finalized")
                else "FINALIZATION_DUE_APPLY_REQUIRED" if final_due and not apply
                else "FINALIZATION_BLOCKED" if final_due and finalization_blockers
                else "PAUSED" if previous.get("explicit_pause")
                else "ACTIVE"
            ),
            "infrastructure_verdict": (
                "48H SPRINT COMPLETED AND STORAGE SAFE"
                if previous.get("finalized") and remote.get("remote_restore_verified")
                else "48H SPRINT COMPLETED WITH R2 BLOCKED"
                if previous.get("finalized")
                else "48H SPRINT ACTIVO Y STORAGE SEGURO"
                if remote.get("remote_restore_verified")
                else "48H SPRINT ACTIVO CON R2 BLOQUEADO"
            ),
            "strategy_verdict": strategy_verdict,
            "contract": contract, "disk_guard": disk, "remote_restore": remote,
            "funnels": funnels, "populations": populations,
            "challenger": challenger, "diagnostic_demo": diagnostic,
            "edge_candidate_demo": edge_demo, "holdout_accesses": int(previous.get("holdout_access_count") or 0),
            **safety(),
        })
        if previous.get("holdout_access_count", 0) > 1:
            raise ValueError("EDGE_SPRINT_HOLDOUT_ACCESS_LIMIT_EXCEEDED")
        if apply:
            heartbeat_id = _sha({
                "sprint_id": previous.get("sprint_id"),
                "observed_at": evidence.get("observed_at"),
                "progress_marker_hash": evidence.get("progress_marker_hash"),
            })
            if heartbeat_id != previous.get("last_runtime_heartbeat_id"):
                _append_jsonl(heartbeat_path, {
                    "schema": "edge_sprint_runtime_heartbeat.v2",
                    "heartbeat_id": heartbeat_id,
                    "sprint_id": previous.get("sprint_id"),
                    "observed_at": evidence.get("observed_at"),
                    "runtime_state": previous.get("runtime_state"),
                    "runtime_qualified": evidence.get("runtime_qualified"),
                    "active_runtime_increment_seconds": previous.get("active_runtime_increment_seconds"),
                    "accumulated_active_runtime_seconds": previous.get("accumulated_active_runtime_seconds"),
                    "progress_marker_hash": evidence.get("progress_marker_hash"),
                    "blockers": evidence.get("blockers") or [],
                    **safety(),
                })
                previous["last_runtime_heartbeat_id"] = heartbeat_id
            if previous.get("finalized") and not previous.get("final_report"):
                previous["final_report"] = _write_final_report(previous, report_root)
            _atomic_json(state_path, previous)
            _atomic_json(status_path, previous)
        return previous
