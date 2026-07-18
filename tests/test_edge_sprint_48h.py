from __future__ import annotations

import json
import socket
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import health_server, research_lab
from app.config import BotConfig
from app.health_server import HealthState, start_health_server
from app.labs import edge_sprint_48h as sprint
from app.labs import isolated_research_demos as demos
from app.labs import project_memory_contract as memory
from app.labs import research_dashboard_v10_43c as dashboard
from app.labs import storage_remote_restore_guard as storage_guard


ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _account(path: Path, account_id: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE account(account_id TEXT, initial_balance REAL, created_at TEXT)")
    con.execute("INSERT INTO account VALUES(?,?,?)", (account_id, 50.0, "2026-07-18T00:00:00Z"))
    con.commit()
    con.close()
    return path


def _memory_fixture(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    contract = _write(tmp_path / "contract.json", {
        "schema": "bitget_research_project_contract.v1",
        "contract_version": "TEST_V1", "allowed_branch": "backup/ati-wip-cdb0cee",
    })
    sources = {
        "ati_boundary": _write(tmp_path / "ati_boundary.json", {"forward_boundary": "2026-07-18T00:00:00Z"}),
        "ati_state": _write(tmp_path / "ati_state.json", {
            "signals_total": 0, "closed_outcomes": 0,
            "reconciliation": {"status": "PASS"},
        }),
        "ati_account": _account(tmp_path / "ati.sqlite", "ATI_PAPER_50"),
        "ati_executor": _write(tmp_path / "ati_executor.json", {}),
        "p11_status": _write(tmp_path / "p11.json", {
            "boundary": {"forward_start_ms": 1_752_796_800_000,
                         "forward_start_timestamp": "2026-07-18T00:00:00Z"},
            "metrics": {"reconciliation_status": "PASS"},
            "safety": {"holdout_opened": False},
        }),
        "p11_reconciliation": _write(tmp_path / "p11_recon.json", {"status": "PASS"}),
        "cross_boundary": _write(tmp_path / "cross_boundary.json", {"initial_offsets": {"bitget": 10}}),
        "cross_offsets": _write(tmp_path / "cross_offsets.json", {"bitget": 20}),
        "cross_account": _account(tmp_path / "cross.sqlite", "CROSS_VENUE_PAPER_50"),
        "cross_status": _write(tmp_path / "cross_status.json", {"reconciliation": {"status": "PASS"}}),
        "storage_manifest": _write(tmp_path / "storage.json", {"partitions": {}}),
        "feature_manifest": _write(tmp_path / "features.json", {"segments": {}}),
        "challenger_status": _write(tmp_path / "challenger.json", {
            "state": "NEED_MORE_DATA", "auto_promotion": False,
            "holdout_access_count": 0,
        }),
    }
    monkeypatch.setattr(memory, "_safe_config_snapshot", lambda: {
        "status": "OK", "PAPER_TRADING": True, "LIVE_TRADING": False,
        "DRY_RUN": True, "ENABLE_PAPER_POLICY_FILTER": False,
        "ENABLE_CANDIDATE_SHADOW_MONITOR": False, "can_send_real_orders": False,
    })
    monkeypatch.setattr(memory, "_git", lambda *args: (
        "backup/ati-wip-cdb0cee" if args == ("branch", "--show-current")
        else "head" if args == ("rev-parse", "HEAD") else "tree"
    ))
    policy = tmp_path / "policy.py"
    policy.write_text("RESEARCH_ONLY=True\n", encoding="ascii")
    monkeypatch.setattr(memory, "REPO_ROOT", tmp_path)
    return {"contract": contract, "policy": policy, **sources}


def test_project_memory_freezes_and_detects_boundary_account_env_and_policy_changes(
    tmp_path: Path, monkeypatch,
) -> None:
    paths = _memory_fixture(tmp_path, monkeypatch)
    kwargs = {
        "contract_path": paths["contract"], "state_path": tmp_path / "state.json",
        "decision_path": tmp_path / "decisions.jsonl",
        "sources": {key: value for key, value in paths.items() if key not in {"contract", "policy"}},
        "env_path": tmp_path / ".env", "policy_paths": ["policy.py"],
    }
    first = memory.run_contract_audit(apply=True, **kwargs)
    assert first["guardrails_status"] == "PASS"
    assert first["decision_ledger"]["records"] == 1
    assert memory.run_contract_audit(apply=False, **kwargs)["guardrails_status"] == "PASS"

    _write(paths["cross_offsets"], {"bitget": 19})
    assert memory.run_contract_audit(**kwargs)["guardrails_status"] == "PASS"
    _write(paths["cross_offsets"], {"bitget": 21})
    _write(paths["cross_boundary"], {"initial_offsets": {"bitget": 11}})
    assert "BOUNDARY_CHANGED:cross_initial_offsets:bitget" in memory.run_contract_audit(**kwargs)["violations"]
    _write(paths["cross_boundary"], {"initial_offsets": {"bitget": 10}})

    con = sqlite3.connect(paths["ati_account"])
    con.execute("UPDATE account SET initial_balance=51")
    con.commit()
    con.close()
    assert "ACCOUNT_RESET_DETECTED:ati:initial_balance" in memory.run_contract_audit(**kwargs)["violations"]
    con = sqlite3.connect(paths["ati_account"])
    con.execute("UPDATE account SET initial_balance=50")
    con.commit()
    con.close()

    (tmp_path / ".env").write_text("FAKE_ONLY=1\n", encoding="ascii")
    assert "ENV_METADATA_CHANGED" in memory.run_contract_audit(**kwargs)["violations"]
    (tmp_path / ".env").unlink()
    paths["policy"].write_text("RESEARCH_ONLY=False\n", encoding="ascii")
    assert "PROTECTED_ORDER_OR_POLICY_PATH_CHANGED" in memory.run_contract_audit(**kwargs)["violations"]


def test_decision_ledger_is_hash_chained_and_rejects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    memory.append_research_decision({"hypothesis": "FAKE", "result": "REJECT"}, path=path)
    memory.append_research_decision({"hypothesis": "FAKE2", "result": "NEED_DATA"}, path=path)
    assert memory.verify_decision_ledger(path)["status"] == "PASS"
    rows = path.read_text(encoding="utf-8").splitlines()
    rows[0] = rows[0].replace("REJECT", "WATCH_ONLY")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert memory.verify_decision_ledger(path)["status"] == "INVALID"


@pytest.mark.parametrize(
    ("free", "eta", "level", "challenger", "heavy"),
    [
        (12 * storage_guard.GIB, 20, "OK", True, True),
        (12 * storage_guard.GIB, 2, "INFO", True, False),
        (9 * storage_guard.GIB, 20, "WARNING", False, False),
        (6 * storage_guard.GIB, 20, "CRITICAL", False, False),
        (4 * storage_guard.GIB, 20, "ABSOLUTE_PROTECTION", False, False),
    ],
)
def test_disk_guard_levels_are_fail_closed(free, eta, level, challenger, heavy) -> None:
    result = storage_guard.disk_guard_status(
        free_bytes=free, eta_to_guard_hours=eta,
        status_payload={"compression_queue": 2, "analytics_queue": 1},
    )
    assert result["level"] == level
    assert result["allow_challenger"] is challenger
    assert result["allow_heavy_research"] is heavy
    assert result["delete_allowed"] is False
    assert result["final_recommendation"] == "NO LIVE"


def test_remote_restore_never_marks_verified_without_feature_rebuild(
    tmp_path: Path, monkeypatch,
) -> None:
    source = tmp_path / "partition.jsonl.gz"
    source.write_bytes(b"compressed")
    candidate = {
        "source_partition_id": "fake:one", "source": source,
        "source_path": "fake/one.jsonl.gz", "compressed_sha256": "a" * 64,
        "logical_sha256": "b" * 64, "rows": 1,
        "compressed_bytes": len(source.read_bytes()), "parquet_outputs": 1,
        "feature_outputs": 1,
    }
    monkeypatch.setattr(storage_guard, "_select_candidate", lambda **kwargs: candidate)
    monkeypatch.setattr(storage_guard, "disk_guard_status", lambda: {"free_disk_bytes": 20 * storage_guard.GIB})
    monkeypatch.setattr(storage_guard, "_file_sha256", lambda path: "a" * 64)
    monkeypatch.setattr(storage_guard, "_restore_replay", lambda *args: {
        "status": "PASS", "features_replay_ready": False,
    })

    class Backend:
        configured = True
        object_prefix = "safe/test"

        def upload(self, path, key, metadata):
            return {"uploaded": True, "size": path.stat().st_size, "object_key": key}

        def download(self, key, target):
            target.write_bytes(source.read_bytes())
            return {"downloaded": True, "bytes": target.stat().st_size}

    result = storage_guard.verify_remote_restore(
        apply=True, backend=Backend(), temp_root=tmp_path / "restore", write=False,
    )
    assert result["status"] == "REMOTE_RESTORE_FAILED"
    assert result["remote_restore_verified"] is False
    assert result["delete_allowed"] is False
    assert result["blockers"] == ["REMOTE_FEATURE_REBUILD_AND_REPLAY_NOT_VERIFIED"]


def _quotes(side: str) -> list[dict]:
    if side == "LONG":
        return [
            {"timestamp_ms": 101, "bid": 99.9, "ask": 100.0},
            {"timestamp_ms": 102, "bid": 100.0, "ask": 100.1,
             "low_bid": 99.0, "high_bid": 101.0},
        ]
    return [
        {"timestamp_ms": 101, "bid": 100.0, "ask": 100.1},
        {"timestamp_ms": 102, "bid": 100.0, "ask": 100.1,
         "low_ask": 99.0, "high_ask": 101.0},
    ]


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_diagnostic_fill_is_causal_symmetric_and_stop_before_tp(side: str) -> None:
    result = demos.simulate_causal_trade(
        {"side": side, "observed_at_ms": 100}, _quotes(side),
        stop_bps=20, take_profit_bps=20,
    )
    assert result["status"] == "CLOSED"
    assert result["entry_ms"] > 100
    assert result["exit_reason"] == "STOP_BEFORE_TP"
    assert result["net_bps"] < result["gross_bps"]
    assert result["not_edge"] is True


def test_diagnostic_ledger_is_idempotent_isolated_and_cannot_reset(tmp_path: Path) -> None:
    ledger = demos.DiagnosticDemoLedger(tmp_path / "diagnostic.sqlite")
    ledger.initialize(forward_boundary_ms=100, initial_balance=50)
    signal = {"signal_id": "one", "observed_at_ms": 100, "decision_ms": 100,
              "symbol": "BTCUSDT", "side": "LONG"}
    result = demos.simulate_causal_trade(signal, _quotes("LONG"))
    assert ledger.record_simulation(signal, result)["status"] == "RECORDED"
    assert ledger.record_simulation(signal, result)["status"] == "DUPLICATE_IGNORED"
    status = ledger.status()
    assert status["trades"] == 1
    assert status["reconciliation"] == "PASS"
    assert status["diagnostic_only"] is True
    assert status["edge_validated"] is False
    with pytest.raises(ValueError, match="ACCOUNT_RESET_BLOCKED"):
        ledger.initialize(forward_boundary_ms=99, initial_balance=50)


def test_edge_demo_gate_blocks_negative_or_incomplete_candidate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(demos, "EDGE_DEMO_STATUS_PATH", tmp_path / "edge.json")
    challenger = {
        "dataset_hash": "hash", "candidates": [{
            "trial_id": "bad", "family": "x", "state": "WATCH_ONLY",
            "validation": {"cost_scenarios": {
                "15.5": {"net_ev_bps": -1, "net_ev_lower_bound_bps": -2,
                         "n_eff": 200, "trades": 200, "single_symbol_profit_concentration": 0.2},
                "18.0": {"net_ev_bps": -2},
            }, "exposure_matched_baselines": {"15.5": {"no_trade": {"net_ev_bps": 0}}}},
            "walk_forward": {"folds": []},
        }],
    }
    result = demos.edge_demo_status(challenger)
    assert result["account_initialized"] is False
    assert result["activation"] == "disabled"
    assert "VALIDATION_NET_EV_NOT_POSITIVE" in result["gate"]["blockers"]
    assert result["can_send_real_orders"] is False


def test_sprint_snapshots_deduplicate_dataset_and_never_opens_holdout_without_candidate(
    tmp_path: Path, monkeypatch,
) -> None:
    config = json.loads((ROOT / "config" / "research" / "EDGE_SPRINT_48H.json").read_text())
    monkeypatch.setattr(sprint, "load_config", lambda: config)
    monkeypatch.setattr(sprint, "contract_status", lambda: {
        "guardrails_status": "PASS", "violations": [],
    })
    monkeypatch.setattr(sprint, "disk_guard_status", lambda write=False: {
        "level": "OK", "allow_challenger": True, "delete_allowed": False,
    })
    monkeypatch.setattr(sprint, "remote_restore_status", lambda: {
        "status": "BLOCKED_R2_CONFIG_UNAVAILABLE", "remote_restore_verified": False,
    })
    dataset = {"status": "OK", "dataset_hash": "one", "verified_feature_files": 2}
    monkeypatch.setattr(sprint, "_dataset_snapshot", lambda: dict(dataset))
    monkeypatch.setattr(sprint, "_funnel_snapshot", lambda: {
        "ati": {"closed_outcomes": 0},
        "p11": {"closed_outcomes": 0},
        "cross_venue": {"raw_evaluations": 1, "unique_episodes": 1, "paper_trades": 0},
    })
    monkeypatch.setattr(sprint, "CHALLENGER_PATH", _write(tmp_path / "challenger.json", {
        "status": "NEED_MORE_DATA", "state": "NEED_MORE_DATA", "candidates": [],
    }))
    monkeypatch.setattr(sprint, "STORAGE_PATH", tmp_path / "storage.json")
    monkeypatch.setattr(sprint, "SCHEDULER_PATH", tmp_path / "scheduler.json")
    monkeypatch.setattr(sprint, "ensure_diagnostic_demo", lambda **kwargs: {
        "status": "OPERABILITY DIAGNOSTIC ACTIVE - NOT EDGE", "trades": 0,
    })
    monkeypatch.setattr(sprint, "edge_demo_status", lambda challenger, **_kwargs: {
        "status": "NO DEFENSIBLE CANDIDATE - DEMO NOT STARTED",
        "account_initialized": False, "gate": {"status": "NO_DEFENSIBLE_CANDIDATE"},
    })
    start = datetime(2026, 7, 18, tzinfo=timezone.utc)
    kwargs = {
        "state_path": tmp_path / "state.json", "status_path": tmp_path / "status.json",
        "holdout_path": tmp_path / "seal.json", "report_root": tmp_path / "reports",
        "lock_path": tmp_path / "lock",
    }
    first = sprint.run_sprint_cycle(apply=True, now=start, **kwargs)
    assert first["status"] == "ACTIVE"
    assert first["snapshot_count"] == 1
    assert first["analysis_eligible"] is True
    second = sprint.run_sprint_cycle(apply=True, now=start + timedelta(hours=1), **kwargs)
    assert second["snapshot_count"] == 1
    assert second["analysis_eligible"] is False
    third = sprint.run_sprint_cycle(apply=True, now=start + timedelta(hours=6), **kwargs)
    assert third["snapshot_count"] == 2
    assert third["analysis_eligible"] is False
    dataset["dataset_hash"] = "two"
    dataset["verified_feature_files"] = 3
    fourth = sprint.run_sprint_cycle(apply=True, now=start + timedelta(hours=12), **kwargs)
    assert fourth["snapshot_count"] == 3
    assert fourth["analysis_eligible"] is True
    original_final_holdout = sprint._final_holdout
    monkeypatch.setattr(
        sprint, "_final_holdout",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("dry-run opened holdout")),
    )
    preview = sprint.run_sprint_cycle(apply=False, now=start + timedelta(hours=49), **kwargs)
    assert preview["status"] == "FINALIZATION_DUE_APPLY_REQUIRED"
    assert preview["holdout_preview"] == {"status": "NOT_ACCESSED_DRY_RUN", "access_count": 0}
    monkeypatch.setattr(sprint, "_final_holdout", original_final_holdout)
    final = sprint.run_sprint_cycle(apply=True, now=start + timedelta(hours=49), **kwargs)
    assert final["status"] == "COMPLETED"
    assert final["holdout_access_count"] == 0
    assert final["holdout"]["status"] == "NOT_ACCESSED_NO_WATCH_ONLY_CANDIDATE"
    assert final["edge_candidate_demo"]["account_initialized"] is False
    assert Path(final["final_report"]["json"]).is_file()


def test_dataset_snapshot_adapts_real_challenger_contract(monkeypatch) -> None:
    from app.labs import continuous_edge_research_challenger as challenger

    monkeypatch.setattr(
        challenger, "_dataset_contract",
        lambda _path: ([Path("one.parquet"), Path("two.parquet")], "dataset-hash", ["p1"]),
    )
    result = sprint._dataset_snapshot()
    assert result["status"] == "OK"
    assert result["dataset_hash"] == "dataset-hash"
    assert result["verified_feature_files"] == 2
    assert result["source_partition_ids"] == ["p1"]


def test_holdout_candidate_must_match_preregistered_spec() -> None:
    challenger = {
        "dataset_hash": "dataset-1",
        "candidates": [{
            "trial_id": "candidate-1", "family": "family-1", "state": "WATCH_ONLY",
            "spec": {"threshold": 2}, "sealed_holdout": {"access_count": 0},
        }],
    }
    seal = {
        "candidate_specs": [{
            "trial_id": "candidate-1", "family": "family-1",
            "spec_hash": sprint._sha({"threshold": 1}),
        }],
    }
    result = sprint._final_holdout(challenger, "dataset-1", seal)
    assert result == {
        "status": "NOT_ACCESSED_CANDIDATE_NOT_PREREGISTERED", "access_count": 0,
    }


def test_demo_status_query_does_not_write_artifact(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "edge_demo_status.json"
    monkeypatch.setattr(demos, "EDGE_DEMO_STATUS_PATH", target)
    demos.edge_demo_status({})
    assert not target.exists()
    demos.edge_demo_status({}, write_status=True)
    assert target.is_file()


def test_tampered_holdout_seal_blocks_cycle(tmp_path: Path, monkeypatch) -> None:
    config = json.loads((ROOT / "config" / "research" / "EDGE_SPRINT_48H.json").read_text())
    monkeypatch.setattr(sprint, "load_config", lambda: config)
    monkeypatch.setattr(sprint, "contract_status", lambda: {"guardrails_status": "PASS"})
    monkeypatch.setattr(sprint, "disk_guard_status", lambda write=False: {"level": "OK", "allow_challenger": True})
    monkeypatch.setattr(sprint, "remote_restore_status", lambda: {})
    monkeypatch.setattr(sprint, "_dataset_snapshot", lambda: {"dataset_hash": "one", "verified_feature_files": 1})
    monkeypatch.setattr(sprint, "_funnel_snapshot", lambda: {
        "ati": {"closed_outcomes": 0}, "p11": {"closed_outcomes": 0},
        "cross_venue": {"raw_evaluations": 0, "unique_episodes": 0, "paper_trades": 0},
    })
    monkeypatch.setattr(sprint, "CHALLENGER_PATH", _write(tmp_path / "challenger.json", {}))
    monkeypatch.setattr(sprint, "ensure_diagnostic_demo", lambda **kwargs: {"trades": 0})
    monkeypatch.setattr(sprint, "edge_demo_status", lambda challenger, **_kwargs: {"gate": {}})
    kwargs = {
        "state_path": tmp_path / "state.json", "status_path": tmp_path / "status.json",
        "holdout_path": tmp_path / "seal.json", "report_root": tmp_path / "reports",
        "lock_path": tmp_path / "lock",
    }
    sprint.run_sprint_cycle(apply=True, now=datetime(2026, 7, 18, tzinfo=timezone.utc), **kwargs)
    seal = json.loads((tmp_path / "seal.json").read_text())
    seal["max_accesses"] = 2
    (tmp_path / "seal.json").write_text(json.dumps(seal), encoding="utf-8")
    result = sprint.run_sprint_cycle(
        apply=True, now=datetime(2026, 7, 18, 1, tzinfo=timezone.utc), **kwargs,
    )
    assert result["status"] == "BLOCKED_HOLDOUT_SEAL_INVALID"


def test_cli_scheduler_dashboard_and_http_are_research_only() -> None:
    commands = {
        "project-memory-contract-v1", "project-memory-status-v1",
        "storage-disk-guard-v1", "storage-remote-restore-v1",
        "edge-sprint-cycle-v1", "edge-sprint-status-v1", "research-demo-status-v1",
    }
    assert commands <= research_lab.PUBLIC_RESEARCH_ONLY_COMMANDS
    scheduler = (ROOT / "scripts" / "run_storage_edge_scheduler.ps1").read_text(encoding="utf-8")
    for command in ("project-memory-contract-v1", "storage-disk-guard-v1", "edge-sprint-cycle-v1"):
        assert command in scheduler
    assert "BitgetBotStorageEdgeSchedulerV2" in scheduler
    assert "can_send_real_orders" in scheduler
    forbidden = ("place_order", "private_post", "set_leverage", "set_margin_mode")
    assert not any(token in scheduler for token in forbidden)

    source = (ROOT / "app" / "health_server.py").read_text(encoding="utf-8")
    for route in (
        "/api/research/project-memory-contract", "/api/research/edge-sprint-48h",
        "/api/research/operability-diagnostic-demo", "/api/research/edge-candidate-demo",
        "/api/research/storage-remote-restore",
    ):
        assert source.count(f'"{route}"') == 2

    html = dashboard._panel_edge_candidate_demo({"isolated_research_demos": {"candidate": {}}})
    assert "NO DEFENSIBLE CANDIDATE" in html
    assert "No automatic start" in html
    assert "paper filter stays disabled" in html


def test_new_http_endpoints_read_only_artifacts(tmp_path: Path, monkeypatch) -> None:
    artifacts = {
        "_PROJECT_MEMORY_CONTRACT_STATUS": {"guardrails_status": "PASS"},
        "_EDGE_SPRINT_48H_STATUS": {"status": "ACTIVE"},
        "_DIAGNOSTIC_DEMO_STATUS": {"status": "OPERABILITY DIAGNOSTIC ACTIVE - NOT EDGE"},
        "_EDGE_CANDIDATE_DEMO_STATUS": {"status": "NO DEFENSIBLE CANDIDATE - DEMO NOT STARTED"},
        "_STORAGE_REMOTE_RESTORE_STATUS": {"status": "BLOCKED_R2_CONFIG_UNAVAILABLE"},
    }
    for index, (name, payload) in enumerate(artifacts.items()):
        path = _write(tmp_path / f"artifact_{index}.json", payload)
        monkeypatch.setattr(health_server, name, path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])

    class Logger:
        def info(self, *args, **kwargs):
            return None

    start_health_server(
        HealthState(mode="paper"), port, Logger(), config=BotConfig(), host="127.0.0.1",
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            urllib.request.urlopen(base + "/health", timeout=2).close()  # noqa: S310
            break
        except OSError:
            time.sleep(0.05)
    for route in (
        "/api/research/project-memory-contract", "/api/research/edge-sprint-48h",
        "/api/research/operability-diagnostic-demo", "/api/research/edge-candidate-demo",
        "/api/research/storage-remote-restore",
    ):
        with urllib.request.urlopen(base + route, timeout=2) as response:  # noqa: S310
            payload = json.loads(response.read())
        assert payload["research_only"] is True
        assert payload["paper_filter_enabled"] is False
        assert payload["can_send_real_orders"] is False
        assert payload["final_recommendation"] == "NO LIVE"


def test_health_components_keep_sprint_payload_compact(tmp_path: Path, monkeypatch) -> None:
    sprint_artifact = {
        "status": "ACTIVE", "sprint_id": "sprint-1", "snapshot_count": 2,
        "holdout_accesses": 0, "challenger": {"large_nested_payload": "x" * 100_000},
    }
    monkeypatch.setattr(
        health_server, "_EDGE_SPRINT_48H_STATUS",
        _write(tmp_path / "sprint.json", sprint_artifact),
    )
    payload = health_server._research_components_status_payload(HealthState(mode="paper"))
    component = payload["components"]["edge_sprint_48h"]
    assert component["artifact_status"] == "ACTIVE"
    assert component["snapshot_count"] == 2
    assert "challenger" not in component


def test_new_productive_modules_contain_no_order_or_live_path() -> None:
    files = [
        ROOT / "app" / "labs" / "project_memory_contract.py",
        ROOT / "app" / "labs" / "storage_remote_restore_guard.py",
        ROOT / "app" / "labs" / "isolated_research_demos.py",
        ROOT / "app" / "labs" / "edge_sprint_48h.py",
    ]
    forbidden = (
        "private_get(", "private_post(", "place_order(", "set_leverage(",
        "set_margin_mode(", "ExecutionEngine.execute", "PaperTrader.open_position",
        "can_send_real_orders=True", "LIVE_TRADING=True", "ENABLE_PAPER_POLICY_FILTER=True",
    )
    for path in files:
        source = path.read_text(encoding="utf-8")
        assert not any(token in source for token in forbidden), path.name
