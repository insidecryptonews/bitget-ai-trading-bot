from __future__ import annotations

import json
import socket
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

from app.adaptive_exit_policy_lab import AdaptiveExitPolicyLab
from app.config import BotConfig
from app.data_vault import DataVault, DataVaultExternalStorage
from app.database import Database
from app.exit_latency_vault_smoke_test import ExitLatencyVaultSmokeTest
from app.fast_execution_readiness import FastExecutionReadiness
from app.health_server import HealthState, start_health_server
from app.latency_audit import LatencyAudit, percentile
from app.time_death_lab import TimeDeathLab
from app.training_summary import TrainingSummary


class DummyLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.endpoint_url = ""
        self.bucket = ""
        self.prefix = ""

    def upload_file(self, filename, bucket, key):
        self.calls.append(("upload_file", filename, bucket, key))
        self.bucket = bucket
        self.prefix = key.rsplit("/", 1)[0]
        self.objects[key] = {"size": int(__import__("pathlib").Path(filename).stat().st_size), "last_modified": "2026-01-01"}

    def head_object(self, Bucket, Key):  # noqa: N803
        return {"ContentLength": self.objects.get(Key, {}).get("size", 0)}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=1000):  # noqa: N803
        return {
            "Contents": [
                {"Key": key, "Size": value["size"], "LastModified": value["last_modified"]}
                for key, value in self.objects.items()
                if key.startswith(Prefix)
            ]
        }


class FakeBoto3:
    def __init__(self, client):
        self.fake_client = client
        self.kwargs = {}

    def client(self, service, **kwargs):
        self.kwargs = kwargs
        self.fake_client.endpoint_url = kwargs.get("endpoint_url", "")
        return self.fake_client


class Boto3Patch:
    def __init__(self, fake):
        self.fake = fake
        self.old = None

    def __enter__(self):
        self.old = sys.modules.get("boto3")
        sys.modules["boto3"] = self.fake
        return self.fake

    def __exit__(self, exc_type, exc, tb):
        if self.old is None:
            sys.modules.pop("boto3", None)
        else:
            sys.modules["boto3"] = self.old


def cfg(tmp_path, **kwargs):
    base = {
        "data_vault_export_dir": str(tmp_path / "training_exports"),
        "data_vault_external_enabled": False,
    }
    base.update(kwargs)
    return BotConfig(**base)


def make_db(tmp_path, config=None):
    config = config or cfg(tmp_path)
    db = Database(config, DummyLogger())
    db.sqlite_path = tmp_path / "vault.db"
    db.initialize()
    return db


def seed_label(db, *, symbol="BTCUSDT", side="LONG", regime="RANGE", score=82, barrier="TIME", ret=0.0, minutes_ago=10):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    obs_id = db.record_signal_observation({
        "timestamp": ts,
        "symbol": symbol,
        "side": side,
        "strategy_type": "TEST",
        "confidence_score": score,
        "market_regime": regime,
        "entry_price": 100.0,
        "score_bucket": "80-89",
    })
    db.record_signal_label({
        "timestamp": ts,
        "observation_id": obs_id,
        "label": 1 if barrier.startswith("TP") else -1 if barrier == "SL" else 0,
        "first_barrier_hit": barrier,
        "bars_to_outcome": 12,
        "realized_return_pct": ret,
    })
    return obs_id, ts


def seed_path(db, obs_id, *, source="trade_signal", status="matured"):
    db.upsert_signal_path_metric({
        "observation_id": obs_id,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "score": 82,
        "score_bucket": "80-89",
        "market_regime": "RANGE",
        "source": source,
        "entry_price": 100.0,
        "current_price": 100.2,
        "max_favorable_pct": 0.60,
        "max_adverse_pct": 0.20,
        "final_return_pct": -0.05,
        "bars_tracked": 30,
        "bars_to_mfe": 4,
        "bars_to_mae": 10,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def test_time_death_lab_prints_markers_and_detects_time_group(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    for idx in range(12):
        obs_id, _ = seed_label(db, barrier="TIME", ret=0.0, minutes_ago=idx)
        seed_path(db, obs_id)
    text = TimeDeathLab(config, db).to_text(hours=24)
    assert "TIME DEATH LAB START" in text
    assert "worst_time_groups:" in text
    assert "TIME%=" in text
    assert "TIME DEATH LAB END" in text


def test_adaptive_exit_policy_proposes_without_trading(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    before = db.get_paper_trade_summary()["open"]
    for idx in range(12):
        obs_id, _ = seed_label(db, barrier="TIME", ret=0.0, minutes_ago=idx)
        seed_path(db, obs_id)
    text = AdaptiveExitPolicyLab(config, db).to_text(hours=24)
    assert "ADAPTIVE EXIT POLICY START" in text
    assert "early_exit_after_bars" in text
    assert db.get_paper_trade_summary()["open"] == before


def test_latency_audit_calculates_percentiles(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    db.record_latency_metric("cycle_total_ms", 100)
    db.record_latency_metric("cycle_total_ms", 200)
    db.record_latency_metric("cycle_total_ms", 300)
    payload = LatencyAudit(config, db).build(hours=24)
    assert payload["metrics"]["cycle_total_ms"]["p50_ms"] == 200
    assert percentile([1, 2, 3], 0.95) > 2
    assert "LATENCY AUDIT START" in LatencyAudit(config, db).to_text(hours=24)


def test_fast_execution_readiness_marks_railway_research_not_hft(tmp_path):
    text = FastExecutionReadiness(cfg(tmp_path), None).to_text()
    assert "FAST EXECUTION READINESS START" in text
    assert "is_hft_ready: false" in text
    assert "VPS research/paper" in text
    assert "NO LIVE" in text


def test_data_export_creates_manifest_checksums_and_excludes_secrets(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    obs_id, _ = seed_label(db, barrier="TP1", ret=1.0)
    seed_path(db, obs_id)
    db.record_event("secret_test", "API_KEY=should_not_leak", payload={"TOKEN": "should_not_leak"})
    result = DataVault(config, db, DummyLogger()).export(hours=168)
    assert result["manifest_valid"] is True
    assert result["checksums_created"] is True
    with zipfile.ZipFile(result["file"], "r") as zf:
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["files"]
        joined = "\n".join(zf.read(name).decode("latin1", errors="ignore") for name in zf.namelist() if name.endswith(".json") or name.endswith(".gz"))
    assert "should_not_leak" not in joined
    assert any(item["table"] == "signal_path_metrics" for item in manifest["files"])


def test_data_import_dry_run_does_not_write_and_apply_is_idempotent(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    obs_id, _ = seed_label(db, barrier="TP1", ret=1.0)
    seed_path(db, obs_id)
    vault = DataVault(config, db, DummyLogger())
    export = vault.export(hours=168)
    target_config = cfg(tmp_path / "target")
    target_db = make_db(tmp_path / "target", target_config)
    target_vault = DataVault(target_config, target_db, DummyLogger())
    before = target_db.get_table_counts()["signal_labels"]
    dry = target_vault.import_backup(file=export["file"], apply=False)
    assert dry["result"] == "PASS"
    assert target_db.get_table_counts()["signal_labels"] == before
    applied = target_vault.import_backup(file=export["file"], apply=True)
    applied_again = target_vault.import_backup(file=export["file"], apply=True)
    assert applied["rows_inserted"] > 0
    assert applied_again["duplicates_skipped"] > 0


def test_data_vault_status_and_migration_readiness_markers(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    seed_label(db)
    vault = DataVault(config, db, DummyLogger())
    assert "DATA VAULT STATUS START" in vault.status_text()
    vault.export(hours=168)
    text = vault.migration_readiness_text()
    assert "MIGRATION READINESS START" in text
    assert "mode: lightweight" in text
    assert "manifest_known_valid: true" in text


def test_migration_readiness_lightweight_uses_cache_without_decompressing(tmp_path, monkeypatch):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    seed_label(db)
    vault = DataVault(config, db, DummyLogger())
    vault.export(hours=168)

    def boom(path):
        raise AssertionError("validate_backup should not run in lightweight readiness")

    monkeypatch.setattr(vault, "validate_backup", boom)
    payload = vault.migration_readiness()
    assert payload["mode"] == "lightweight"
    assert payload["manifest_known_valid"] is True
    assert payload["checksum_known_valid"] is True


def test_migration_readiness_deep_check_updates_cache(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    seed_label(db)
    vault = DataVault(config, db, DummyLogger())
    vault.export(hours=168)
    payload = vault.migration_readiness_deep_check()
    assert payload["manifest_valid"] is True
    assert payload["checksum_valid"] is True
    assert payload["import_dry_run_ok"] is True
    assert vault._read_state()["import_dry_run_ok"] is True


def test_external_upload_disabled_and_missing_credentials_do_not_fail(tmp_path):
    disabled = DataVaultExternalStorage(cfg(tmp_path), DummyLogger()).upload(tmp_path / "missing.zip", {})
    assert disabled["enabled"] is False
    missing = DataVaultExternalStorage(cfg(tmp_path, data_vault_external_enabled=True), DummyLogger()).upload(tmp_path / "missing.zip", {})
    assert missing["uploaded"] is False
    assert "missing" in missing["sanitized_error"]


def test_external_upload_uses_s3_endpoint_bucket_and_prefix(tmp_path):
    config = cfg(
        tmp_path,
        data_vault_external_enabled=True,
        data_vault_external_bucket="training-bucket",
        data_vault_external_prefix="bitget-ai-trading-bot/training",
        data_vault_s3_endpoint_url="https://r2.example",
        data_vault_s3_access_key_id="access-key",
        data_vault_s3_secret_access_key="secret-key",
    )
    db = make_db(tmp_path, config)
    obs_id, _ = seed_label(db, barrier="TP1", ret=1.0)
    seed_path(db, obs_id)
    fake_client = FakeS3Client()
    fake = FakeBoto3(fake_client)
    with Boto3Patch(fake):
        result = DataVault(config, db, DummyLogger()).export(hours=168, upload=True)
    upload = result["external_upload"]
    assert upload["uploaded"] is True
    assert upload["verified"] is True
    assert upload["remote_key"].startswith("bitget-ai-trading-bot/training/training_vault_")
    assert fake.kwargs["endpoint_url"] == "https://r2.example"
    assert fake.kwargs["region_name"] == "auto"
    assert fake_client.bucket == "training-bucket"
    assert fake_client.prefix == "bitget-ai-trading-bot/training"
    assert "access-key" not in json.dumps(upload)
    assert "secret-key" not in json.dumps(upload)


def test_data_export_upload_text_and_upload_latest_with_mock(tmp_path):
    config = cfg(
        tmp_path,
        data_vault_external_enabled=True,
        data_vault_external_bucket="training-bucket",
        data_vault_external_prefix="bitget-ai-trading-bot/training",
        data_vault_s3_endpoint_url="https://r2.example",
        data_vault_s3_access_key_id="access-key",
        data_vault_s3_secret_access_key="secret-key",
    )
    db = make_db(tmp_path, config)
    seed_label(db, barrier="TP1", ret=1.0)
    fake = FakeBoto3(FakeS3Client())
    vault = DataVault(config, db, DummyLogger())
    with Boto3Patch(fake):
        text = vault.export_text(hours=168, upload=True)
        upload_latest_text = vault.upload_latest_text()
        status = vault.status()
    assert "uploaded: true" in text
    assert "verified: true" in text
    assert "DATA UPLOAD LATEST START" in upload_latest_text
    assert "uploaded: true" in upload_latest_text
    assert status["remote_list_ok"] is True
    assert status["remote_backup_count"] >= 1


def test_dashboard_data_export_endpoint_uploads_when_external_enabled(tmp_path):
    config = cfg(
        tmp_path,
        data_vault_external_enabled=True,
        data_vault_external_bucket="training-bucket",
        data_vault_external_prefix="bitget-ai-trading-bot/training",
        data_vault_s3_endpoint_url="https://r2.example",
        data_vault_s3_access_key_id="access-key",
        data_vault_s3_secret_access_key="secret-key",
    )
    db = make_db(tmp_path, config)
    seed_label(db, barrier="TP1", ret=1.0)
    port = _free_port()
    fake_client = FakeS3Client()
    with Boto3Patch(FakeBoto3(fake_client)):
        start_health_server(HealthState(mode=config.mode), port, DummyLogger(), config=config, db=db)
        base = f"http://127.0.0.1:{port}"
        for _ in range(30):
            try:
                _get(base + "/health")
                break
            except Exception:
                time.sleep(0.05)
        status, body = _get(base + "/api/training/data-export?hours=168")
    payload = json.loads(body)
    assert status == 200
    assert payload["external_upload"]["uploaded"] is True
    assert payload["external_upload"]["verified"] is True
    assert fake_client.calls


def test_automatic_backup_prunes_old_files(tmp_path):
    config = cfg(tmp_path, data_vault_max_backups_local=2)
    db = make_db(tmp_path, config)
    vault = DataVault(config, db, DummyLogger())
    vault.export_dir.mkdir(parents=True, exist_ok=True)
    for idx in range(4):
        path = vault.export_dir / f"training_vault_20260101_00000{idx}.zip"
        path.write_bytes(b"zip")
    vault.prune_local_backups()
    assert len(vault.list_backups()) == 2


def test_acceleration_plan_mentions_backup_when_missing(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    obs_id, _ = seed_label(db, barrier="TP1", ret=1.0)
    seed_path(db, obs_id)
    text = TrainingSummary(config, db).acceleration_plan(hours=24)
    assert "biggest_problem: no_recent_training_backup" in text
    assert "NO LIVE" in text


def test_exit_latency_vault_smoke_test_passes(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    obs_id, _ = seed_label(db, barrier="TIME", ret=0.0)
    seed_path(db, obs_id)
    text = ExitLatencyVaultSmokeTest(config, db, DummyLogger()).to_text()
    assert "EXIT LATENCY VAULT SMOKE TEST START" in text
    assert "result: PASS" in text
    assert "LIVE_TRADING=false" in text
    assert "opened_paper_trades: 0" in text


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - local test server
        return int(response.status), response.read().decode("utf-8")


def test_dashboard_new_endpoints_do_not_expose_secrets(tmp_path):
    config = cfg(tmp_path, dashboard_auth_token="safe-token")
    db = make_db(tmp_path, config)
    seed_label(db)
    port = _free_port()
    start_health_server(HealthState(mode=config.mode), port, DummyLogger(), config=config, db=db)
    base = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            _get(base + "/health")
            break
        except Exception:
            time.sleep(0.05)
    for path, marker in (
        ("/api/training/time-death-lab?hours=24", "TIME DEATH LAB START"),
        ("/api/training/adaptive-exit-policy?hours=24", "ADAPTIVE EXIT POLICY START"),
        ("/api/training/latency-audit?hours=24", "LATENCY AUDIT START"),
        ("/api/training/fast-execution-readiness", "FAST EXECUTION READINESS START"),
        ("/api/training/data-vault-status", "DATA VAULT STATUS START"),
        ("/api/training/data-upload-latest", "DATA UPLOAD LATEST START"),
        ("/api/training/data-vault-prune", "DATA VAULT PRUNE START"),
        ("/api/training/migration-readiness", "MIGRATION READINESS START"),
        ("/api/training/migration-readiness-deep-check", "MIGRATION READINESS DEEP CHECK START"),
    ):
        status, body = _get(base + path + ("&" if "?" in path else "?") + "token=safe-token")
        assert status == 200
        assert marker in json.loads(body)["text"]
        assert "safe-token" not in body


def test_safety_defaults_stay_paper_only(tmp_path):
    config = cfg(tmp_path)
    assert config.live_trading is False
    assert config.dry_run is True
    assert config.paper_trading is True
    assert config.max_open_positions == 1
