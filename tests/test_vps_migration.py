import json
import socket
import sys
import time
import urllib.request
from pathlib import Path

from app.config import BotConfig
from app.data_vault import DataVault
from app.database import Database
from app.fast_runtime_plan import FastRuntimePlan
from app.health_server import HealthState, start_health_server
from app.research_lab import ResearchLab
from app.utils import iso_utc
from app.vps_migration import VpsPreflight, build_vps_migration_guide
from app.vps_migration_smoke_test import VpsMigrationSmokeTest
from app.worker_lock import WorkerLockManager


class DummyLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.calls = []

    def upload_file(self, filename, bucket, key):
        data = Path(filename).read_bytes()
        self.objects[key] = {"size": len(data), "data": data, "last_modified": "2026-05-16"}
        self.calls.append(("upload", bucket, key))

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.objects[key]["data"])
        self.calls.append(("download", bucket, key))

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
        self.client_obj = client

    def client(self, service, **kwargs):
        return self.client_obj


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
    db.sqlite_path = tmp_path / "vps.db"
    db.initialize()
    return db


def seed(db):
    obs_id = db.record_signal_observation({
        "timestamp": iso_utc(),
        "symbol": "BTCUSDT",
        "side": "LONG",
        "strategy_type": "TEST",
        "confidence_score": 80,
        "market_regime": "RANGE",
        "entry_price": 100.0,
        "score_bucket": "80-89",
    })
    db.record_signal_label({
        "timestamp": iso_utc(),
        "observation_id": obs_id,
        "label": 0,
        "first_barrier_hit": "TIME",
        "bars_to_outcome": 1,
        "realized_return_pct": 0.0,
    })


def test_vps_migration_guide_prints_safe_steps(tmp_path):
    text = build_vps_migration_guide(cfg(tmp_path))
    assert "VPS MIGRATION GUIDE START" in text
    assert "LIVE_TRADING=false" in text
    assert "DRY_RUN=true" in text
    assert "PAPER_TRADING=true" in text
    assert "mock-secret" not in text


def test_vps_preflight_ok_and_blocks_live(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    seed(db)
    text = VpsPreflight(config, db, DummyLogger()).to_text()
    assert "VPS PREFLIGHT START" in text
    assert "result: VPS_PREFLIGHT_OK" in text
    blocked = VpsPreflight(BotConfig(live_trading=True, dry_run=True, paper_trading=True), db, DummyLogger()).to_text()
    assert "result: VPS_PREFLIGHT_BLOCKED" in blocked
    assert "LIVE_TRADING=true" in blocked


def test_data_download_and_restore_latest_with_mock_r2(tmp_path):
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
    seed(db)
    vault = DataVault(config, db, DummyLogger())
    fake_client = FakeS3Client()
    with Boto3Patch(FakeBoto3(fake_client)):
        export = vault.export(hours=1, upload=True)
        Path(export["file"]).unlink()
        download = vault.download_latest()
        restore = vault.restore_latest(apply=False)
    assert download["downloaded"] is True
    assert restore["manifest_valid"] is True
    assert restore["checksum_valid"] is True
    blob = json.dumps({"download": download, "restore": restore})
    assert "access-key" not in blob
    assert "secret-key" not in blob


def test_single_worker_lock_blocks_duplicate(tmp_path):
    config = cfg(tmp_path, require_single_worker_lock=True, worker_lock_ttl_seconds=120)
    db = make_db(tmp_path, config)
    first = WorkerLockManager(config, db, DummyLogger(), instance_id="worker-a").acquire()
    second = WorkerLockManager(config, db, DummyLogger(), instance_id="worker-b").acquire()
    assert first.acquired is True
    assert second.acquired is False
    assert second.lock_status == "blocked_duplicate"


def test_fast_runtime_plan_markers(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    text = FastRuntimePlan(config, db).to_text(hours=24)
    assert "FAST RUNTIME PLAN START" in text
    assert "WebSocket market stream" in text
    assert "final_recommendation: NO LIVE" in text


def test_dashboard_vps_endpoints_do_not_502_or_expose_token(tmp_path):
    config = cfg(tmp_path, dashboard_auth_token="dash-token")
    db = make_db(tmp_path, config)
    seed(db)
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
        ("/api/training/vps-migration-guide", "VPS MIGRATION GUIDE START"),
        ("/api/training/vps-preflight", "VPS PREFLIGHT START"),
        ("/api/training/fast-runtime-plan", "FAST RUNTIME PLAN START"),
        ("/api/training/worker-lock-status", "WORKER LOCK STATUS START"),
        ("/api/training/data-restore-latest", "DATA RESTORE LATEST START"),
    ):
        status, body = _get(base + path + "?token=dash-token")
        assert status == 200
        assert marker in json.loads(body)["text"]
        assert "dash-token" not in body


def test_config_vps_profile_defaults_safe(tmp_path):
    config = cfg(tmp_path)
    assert config.training_runtime_profile == "railway_lightweight"
    assert config.vps_research_profile_enabled is False
    assert config.require_single_worker_lock is True
    assert config.live_trading is False
    assert config.dry_run is True
    assert config.paper_trading is True


def test_research_lab_vps_commands_exist(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    lab = ResearchLab(db, config, DummyLogger(), reports_dir=tmp_path / "reports")
    assert "VPS MIGRATION GUIDE START" in lab.vps_migration_guide()
    assert "VPS PREFLIGHT START" in lab.vps_preflight()
    assert "FAST RUNTIME PLAN START" in lab.fast_runtime_plan()


def test_vps_migration_smoke_test_passes(tmp_path):
    config = cfg(tmp_path)
    db = make_db(tmp_path, config)
    text = VpsMigrationSmokeTest(config, db, DummyLogger()).to_text()
    assert "VPS MIGRATION SMOKE TEST START" in text
    assert "result: PASS" in text
    assert "opened_paper_trades_from_smoke: 0" in text


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - local test server
        return int(response.status), response.read().decode("utf-8")
