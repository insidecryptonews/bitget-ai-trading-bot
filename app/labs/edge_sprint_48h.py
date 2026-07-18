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
from .project_memory_contract import contract_status
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
        "snapshot_interval_hours": 6,
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
        "ati": {
            "observations": int(ati.get("new_forward_candidates_seen") or 0),
            "signals": int(ati.get("signals_total") or 0),
            "blocked_signals": int(ati.get("paper_feed_blocked_signals") or 0),
            "eligible_signals": int(ati.get("paper_feed_eligible_signals") or 0),
            "closed_outcomes": int(ati.get("closed_outcomes") or 0),
            "open_positions": int(ati.get("open_positions") or 0),
            "reconciliation": (ati.get("reconciliation") or {}).get("status") or ati.get("reconciliation_status"),
            "timing_metrics_status": "NEED_MORE_DATA" if not ati.get("timing_metrics") else "AVAILABLE",
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


def _population_counters(funnels: dict[str, Any], diagnostic: dict[str, Any]) -> dict[str, int]:
    cross = funnels["cross_venue"]
    p11 = funnels["p11"]
    ati = funnels["ati"]
    return {
        "raw_evaluations": int(cross["raw_evaluations"]),
        "unique_episodes": int(cross["unique_episodes"]),
        "diagnostic_trades": int(diagnostic.get("trades") or 0),
        "candidate_validation_fills": 0,
        "forward_demo_trades": 0,
        "ati_paper_trades": int(ati["closed_outcomes"]),
        "p11_outcomes": int(p11["closed_outcomes"]),
        "cross_venue_paper_trades": int(cross["paper_trades"]),
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


def run_sprint_cycle(
    *, apply: bool = False, now: datetime | None = None,
    state_path: Path = STATE_PATH, status_path: Path = STATUS_PATH,
    holdout_path: Path = HOLDOUT_SEAL_PATH, report_root: Path = REPORT_ROOT,
    lock_path: Path = LOCK_PATH,
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
                "schema": "edge_sprint_48h.state.v1", "sprint_id": sprint_id,
                "started_at": start.isoformat(),
                "planned_end_at": (start + timedelta(hours=48)).isoformat(),
                "commit": _git("rev-parse", "HEAD"), "tree": _git("rev-parse", "HEAD^{tree}"),
                "config_hash": config_hash, "initial_dataset_hash": dataset.get("dataset_hash"),
                "last_analyzed_dataset_hash": None, "last_snapshot_at": None,
                "last_cycle_at": None, "snapshot_count": 0,
                "holdout_access_count": 0, "holdout": {"status": "SEALED_NOT_EVALUATED", "access_count": 0},
                "finalized": False, "families": config["families"],
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
        start_at = _parse_time(previous.get("started_at")) or current_time
        end_at = _parse_time(previous.get("planned_end_at")) or (start_at + timedelta(hours=48))
        last_snapshot = _parse_time(previous.get("last_snapshot_at"))
        snapshot_due = last_snapshot is None or (current_time - last_snapshot) >= timedelta(hours=6)
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
        populations = _population_counters(funnels, diagnostic)
        snapshot = {
            "schema": "edge_sprint_48h.snapshot.v1", "captured_at": current_time.isoformat(),
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
        if analysis_eligible:
            previous["last_analyzed_dataset_hash"] = dataset.get("dataset_hash")
        final_due = current_time >= end_at
        if final_due and not previous.get("finalized"):
            if not apply:
                previous["holdout_preview"] = {
                    "status": "NOT_ACCESSED_DRY_RUN", "access_count": 0,
                }
            else:
                seal = _read_json(holdout_path, {}) or {}
                holdout = _final_holdout(challenger, dataset.get("dataset_hash"), seal)
                previous["holdout"] = holdout
                previous["holdout_access_count"] = int(holdout.get("access_count") or 0)
                previous["finalized"] = True
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
            "next_cycle_at": (current_time + timedelta(hours=6)).isoformat(),
            "remaining_seconds": max(0, int((end_at - current_time).total_seconds())),
            "status": (
                "COMPLETED" if previous.get("finalized")
                else "FINALIZATION_DUE_APPLY_REQUIRED" if final_due and not apply
                else "ACTIVE"
            ),
            "infrastructure_verdict": (
                "48H SPRINT ACTIVO Y STORAGE SEGURO" if remote.get("remote_restore_verified")
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
            if previous.get("finalized") and not previous.get("final_report"):
                previous["final_report"] = _write_final_report(previous, report_root)
            _atomic_json(state_path, previous)
            _atomic_json(status_path, previous)
        return previous
