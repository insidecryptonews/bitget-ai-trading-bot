from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.labs import edge_sprint_48h as sprint
from app.labs import research_review_snapshot as review


ROOT = Path(__file__).resolve().parents[1]


def _config() -> dict:
    return {
        "target_active_runtime_seconds": 172800,
        "runtime_heartbeat_max_gap_seconds": 900,
        "collector_heartbeat_max_age_seconds": 180,
        "runtime_clock_skew_tolerance_seconds": 30,
        "snapshot_interval_hours": 6,
        "migration_snapshot_gap_grace_seconds": 900,
    }


def _evidence(*, stack: bool = True, growth: bool = True) -> dict:
    return {
        "stack_healthy": stack,
        "data_growing": growth,
        "runtime_qualified": stack and growth,
        "progress_marker": {"rows": 2 if growth else 1},
        "blockers": [] if stack and growth else ["NOT_QUALIFIED"],
    }


def _state(now: datetime) -> dict:
    return {
        "schema": "edge_sprint_48h.state.v2",
        "runtime_accounting_version": "ACTIVE_RUNTIME_V2",
        "started_at": now.isoformat(),
        "accumulated_active_runtime_seconds": 0,
        "target_active_runtime_seconds": 172800,
        "current_session_started_at": None,
        "current_session_runtime_seconds": 0,
        "last_runtime_observation_at": None,
        "last_runtime_stack_healthy": False,
        "runtime_state": "WAITING_FOR_VALID_HEARTBEAT",
        "explicit_pause": False,
        "resume_count": 0,
        "shutdown_count": 0,
        "last_snapshot_active_runtime_seconds": 0,
    }


def _snapshot(path: Path, captured: datetime, dataset_hash: str, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "captured_at": captured.isoformat(),
        "dataset": {
            "dataset_hash": dataset_hash, "total_feature_rows": rows,
            "verified_feature_files": rows,
        },
        "scheduler": {"status": "COMPLETED", "collectors_healthy": True},
        "contract": {"guardrails_status": "PASS"},
    }), encoding="utf-8")


def test_runtime_evidence_tolerates_small_clock_race_but_blocks_large_future_skew(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    scheduler_path = tmp_path / "scheduler.json"
    storage_path = tmp_path / "storage.json"
    collector_root = tmp_path / "collectors"
    scheduler_path.write_text(json.dumps({
        "status": "RUNNING", "collectors_healthy": True, "started_at": now.isoformat(),
    }), encoding="utf-8")
    storage_path.write_text(json.dumps({"raw_logical_bytes": 2}), encoding="utf-8")
    for venue in sprint.REQUIRED_COLLECTOR_VENUES:
        path = collector_root / venue / "health.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "status": "HEALTHY", "connected": True,
            "heartbeat_at": (now + timedelta(seconds=2)).isoformat(),
            "normalized_events": 2, "can_send_real_orders": False,
            "uses_private_endpoints": False,
        }), encoding="utf-8")
    previous = {"raw_logical_bytes": 1, "collectors": {}}
    evidence = sprint._runtime_evidence(
        now=now, dataset={"dataset_hash": "a"}, config=_config(),
        scheduler_path=scheduler_path, storage_path=storage_path,
        collector_root=collector_root, previous_marker=previous,
    )
    assert evidence["stack_healthy"] is True
    assert evidence["runtime_qualified"] is True

    bad = collector_root / sprint.REQUIRED_COLLECTOR_VENUES[0] / "health.json"
    payload = json.loads(bad.read_text(encoding="utf-8"))
    payload["heartbeat_at"] = (now + timedelta(seconds=120)).isoformat()
    bad.write_text(json.dumps(payload), encoding="utf-8")
    blocked = sprint._runtime_evidence(
        now=now, dataset={"dataset_hash": "a"}, config=_config(),
        scheduler_path=scheduler_path, storage_path=storage_path,
        collector_root=collector_root, previous_marker=previous,
    )
    assert blocked["stack_healthy"] is False
    assert any("COLLECTOR_NOT_RUNTIME_HEALTHY" in row for row in blocked["blockers"])

    payload["heartbeat_at"] = (now - timedelta(seconds=181)).isoformat()
    bad.write_text(json.dumps(payload), encoding="utf-8")
    stale = sprint._runtime_evidence(
        now=now, dataset={"dataset_hash": "a"}, config=_config(),
        scheduler_path=scheduler_path, storage_path=storage_path,
        collector_root=collector_root, previous_marker=previous,
    )
    assert stale["stack_healthy"] is False
    bad.unlink()
    missing = sprint._runtime_evidence(
        now=now, dataset={"dataset_hash": "a"}, config=_config(),
        scheduler_path=scheduler_path, storage_path=storage_path,
        collector_root=collector_root, previous_marker=previous,
    )
    assert missing["stack_healthy"] is False


def test_migration_preserves_identity_and_credits_only_proven_snapshot_interval(tmp_path: Path) -> None:
    start = datetime(2026, 7, 18, tzinfo=timezone.utc)
    sprint_id = "48H_EDGE_SPRINT_20260718T125341548203Z_e4ddf6c6"
    snapshots = tmp_path / sprint_id / "snapshots"
    _snapshot(snapshots / "snapshot_001.json", start, "a", 10)
    _snapshot(snapshots / "snapshot_002.json", start + timedelta(hours=6, minutes=2), "b", 20)
    _snapshot(snapshots / "snapshot_003.json", start + timedelta(hours=14, minutes=2), "c", 30)
    legacy = {
        "schema": "edge_sprint_48h.state.v1", "sprint_id": sprint_id,
        "started_at": start.isoformat(), "planned_end_at": (start + timedelta(hours=48)).isoformat(),
        "commit": "head", "tree": "tree", "config_hash": "config",
        "snapshot_count": 3, "holdout_access_count": 0,
    }

    migrated, artifact = sprint._migrate_active_runtime_state(
        legacy, now=start + timedelta(hours=20), config=_config(), report_root=tmp_path,
    )

    assert migrated["sprint_id"] == sprint_id
    assert migrated["snapshot_count"] == 3
    assert migrated["holdout_access_count"] == 0
    assert migrated["accumulated_active_runtime_seconds"] == 6 * 3600 + 120
    assert migrated["original_planned_wall_clock_end"] == (start + timedelta(hours=48)).isoformat()
    assert [row["credited"] for row in artifact["intervals"]] == [True, False]
    assert artifact["unproven_wall_time_credited"] is False


def test_active_runtime_counts_healthy_growth_and_excludes_eight_hour_gap() -> None:
    start = datetime(2026, 7, 18, tzinfo=timezone.utc)
    state = _state(start)
    state = sprint._update_active_runtime(state, now=start, evidence=_evidence(), config=_config())
    state = sprint._update_active_runtime(
        state, now=start + timedelta(minutes=5), evidence=_evidence(), config=_config(),
    )
    assert state["accumulated_active_runtime_seconds"] == 300
    assert state["runtime_state"] == "RUNNING"

    state = sprint._update_active_runtime(
        state, now=start + timedelta(hours=8, minutes=5), evidence=_evidence(), config=_config(),
    )
    assert state["accumulated_active_runtime_seconds"] == 300
    assert state["active_runtime_increment_seconds"] == 0
    assert state["runtime_state"] == "RESUMED_AFTER_UNCOUNTED_GAP"
    assert state["resume_count"] == 1
    assert state["shutdown_count"] == 1

    state = sprint._update_active_runtime(
        state, now=start + timedelta(hours=8, minutes=10), evidence=_evidence(), config=_config(),
    )
    assert state["accumulated_active_runtime_seconds"] == 600
    state = sprint._update_active_runtime(
        state, now=start + timedelta(hours=8, minutes=15),
        evidence=_evidence(stack=False, growth=True), config=_config(),
    )
    assert state["accumulated_active_runtime_seconds"] == 600
    state = sprint._update_active_runtime(
        state, now=start + timedelta(hours=8, minutes=20),
        evidence=_evidence(stack=True, growth=False), config=_config(),
    )
    assert state["accumulated_active_runtime_seconds"] == 600
    assert state["pc_off_time_counts"] is False


def test_pause_resume_preserves_counters_and_requires_contract(tmp_path: Path, monkeypatch) -> None:
    start = datetime(2026, 7, 18, tzinfo=timezone.utc)
    state_path = tmp_path / "state.json"
    status_path = tmp_path / "status.json"
    payload = _state(start)
    payload.update({"sprint_id": "same", "accumulated_active_runtime_seconds": 1234,
                    "snapshot_count": 2, "holdout_access_count": 0})
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    paused = sprint.pause_sprint_session(
        now=start + timedelta(hours=1), state_path=state_path, status_path=status_path,
        lock_path=tmp_path / "cycle.lock",
    )
    assert paused["status"] == "PAUSED"
    assert paused["accumulated_active_runtime_seconds"] == 1234
    assert paused["snapshot_count"] == 2
    assert paused["holdout_access_count"] == 0

    monkeypatch.setattr(sprint, "contract_status", lambda: {"guardrails_status": "PASS"})
    resumed = sprint.resume_sprint_session(
        now=start + timedelta(hours=9), state_path=state_path, status_path=status_path,
        lock_path=tmp_path / "cycle.lock",
    )
    assert resumed["status"] == "ACTIVE"
    assert resumed["runtime_state"] == "WAITING_FOR_VALID_HEARTBEAT"
    assert resumed["accumulated_active_runtime_seconds"] == 1234
    assert resumed["resume_count"] == 1
    paused_again = sprint.pause_sprint_session(
        now=start + timedelta(hours=9, minutes=20), state_path=state_path,
        status_path=status_path, lock_path=tmp_path / "cycle.lock",
    )
    assert paused_again["shutdown_count"] == 2
    resumed_again = sprint.resume_sprint_session(
        now=start + timedelta(hours=9, minutes=40), state_path=state_path,
        status_path=status_path, lock_path=tmp_path / "cycle.lock",
    )
    assert resumed_again["resume_count"] == 2
    assert resumed_again["accumulated_active_runtime_seconds"] == 1234


def test_population_counters_separate_ati_shadow_from_ati_paper() -> None:
    funnels = {
        "ati_shadow": {"signals": 29, "closed_outcomes": 11},
        "ati_paper": {"trades": 4, "open_positions": 1},
        "p11": {"closed_outcomes": 3},
        "cross_venue": {"raw_evaluations": 50, "unique_episodes": 7, "paper_trades": 2},
    }
    counters = sprint._population_counters(funnels, {"trades": 5}, {"trades": 0})
    assert counters["ati_shadow_forward_signals"] == 29
    assert counters["ati_shadow_forward_outcomes"] == 11
    assert counters["ati_paper_trades"] == 4
    assert counters["ati_paper_open_positions"] == 1
    assert counters["diagnostic_trades"] == 5


def test_review_snapshot_is_sanitized_and_final_handoff_stays_blocked(
    tmp_path: Path, monkeypatch,
) -> None:
    state_path = tmp_path / "state.json"
    sprint_root = tmp_path / "sprints"
    review_root = tmp_path / "reviews"
    state = {
        "sprint_id": "sprint-1", "status": "ACTIVE",
        "target_active_runtime_seconds": 172800,
        "accumulated_active_runtime_seconds": 1234,
        "active_runtime_remaining_seconds": 171566,
        "holdout_access_count": 0, "holdout_accesses": 0,
        "populations": {"ati_shadow_forward_outcomes": 2, "ati_paper_trades": 1},
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"api_key": "FAKE_SECRET_123456789", "safe": True}), encoding="utf-8")
    monkeypatch.setattr(review, "STATE_PATH", state_path)
    monkeypatch.setattr(review, "STATUS_PATH", state_path)
    monkeypatch.setattr(review, "REPORT_ROOT", sprint_root)
    monkeypatch.setattr(review, "REVIEW_ROOT", review_root)
    monkeypatch.setattr(review, "_candidate_sources", lambda *_args, **_kwargs: [
        ("safe/source.json", source, "json"),
    ])
    monkeypatch.setattr(review, "_loopback_health", lambda: {"status": "OK"})
    monkeypatch.setattr(review, "_process_snapshot", lambda: {"processes": []})
    monkeypatch.setattr(review, "_git_snapshot", lambda: {"branch": "backup/ati-wip-cdb0cee"})

    result = review.export_review_snapshot(apply=True, final=False)
    assert result["status"] == "REVIEW_SNAPSHOT_CREATED"
    assert result["holdout_accesses"] == 0
    with zipfile.ZipFile(result["path"], "r") as archive:
        payload = archive.read("safe/source.json").decode("utf-8")
        assert "FAKE_SECRET_123456789" not in payload
        assert "[REDACTED]" in payload
        assert "FILE_MANIFEST.json" in archive.namelist()
    blocked = review.export_review_snapshot(apply=True, final=True)
    assert blocked["status"] == "BLOCKED_FINAL_HANDOFF_NOT_READY"
    assert blocked["holdout_accesses"] == 0

    state.update({
        "status": "COMPLETED", "accumulated_active_runtime_seconds": 172800,
        "active_runtime_remaining_seconds": 0,
    })
    state_path.write_text(json.dumps(state), encoding="utf-8")
    final_dir = sprint_root / "sprint-1"
    final_dir.mkdir(parents=True)
    (final_dir / "FINAL_REPORT.json").write_text("{}", encoding="utf-8")
    (final_dir / "FINAL_REPORT.md").write_text("# Final\n", encoding="utf-8")
    final = review.export_review_snapshot(apply=True, final=True)
    assert final["status"] == "FINAL_HANDOFF_CREATED"
    assert Path(final["path"]).name == "HANDOFF_REVIEW_PACK.zip"
    assert final["holdout_accesses"] == 0


def test_session_scripts_and_scheduler_remain_research_only() -> None:
    paths = [
        ROOT / "scripts" / "START_RESEARCH_SESSION.ps1",
        ROOT / "scripts" / "STOP_RESEARCH_SESSION.ps1",
        ROOT / "scripts" / "EXPORT_REVIEW_SNAPSHOT.ps1",
        ROOT / "scripts" / "run_storage_edge_scheduler.ps1",
    ]
    forbidden = ("place_order", "private_post", "set_leverage", "set_margin_mode", "LIVE_TRADING=True")
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert not any(token in source for token in forbidden), path.name
    scheduler = paths[-1].read_text(encoding="utf-8")
    assert "edge-sprint-final-handoff-v1" in scheduler
    assert "planned_end_at" not in scheduler
    assert "sprint_active_runtime_seconds" in scheduler
    start_script = paths[0].read_text(encoding="utf-8")
    stop_script = paths[1].read_text(encoding="utf-8")
    assert "QUALIFIED_DATA_GROWTH_CONFIRMED" in start_script
    assert "BLOCKED_SPRINT_CYCLE_IN_PROGRESS" in start_script
    assert "SCHEDULER_DID_NOT_REACH_ATOMIC_CYCLE_BOUNDARY" in stop_script
    assert "safe_to_power_off=true" in stop_script


def test_scheduler_heartbeats_during_long_challenger_runs() -> None:
    scheduler = (ROOT / "scripts" / "run_storage_edge_scheduler.ps1").read_text(encoding="utf-8")
    config = json.loads((ROOT / "config" / "research" / "EDGE_SPRINT_48H.json").read_text(encoding="utf-8"))

    heartbeat_seconds = 300
    assert f"$SprintHeartbeatSeconds = {heartbeat_seconds}" in scheduler
    assert heartbeat_seconds < config["runtime_heartbeat_max_gap_seconds"]
    assert scheduler.index("function Invoke-SprintCycle") < scheduler.index(
        "function Invoke-ChallengerWithSprintHeartbeats"
    )
    challenger = scheduler.split("function Invoke-ChallengerWithSprintHeartbeats", 1)[1]
    challenger = challenger.split("$mutex =", 1)[0]
    assert "WaitForExit($SprintHeartbeatSeconds * 1000)" in challenger
    assert "Invoke-SprintCycle" in challenger
    assert scheduler.index("$earlySprintExit = Invoke-SprintCycle") < scheduler.index(
        "storage-efficiency-cycle-v2 --apply"
    )
