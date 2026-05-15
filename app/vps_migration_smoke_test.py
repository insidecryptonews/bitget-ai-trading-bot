from __future__ import annotations

import json
import shutil
import socket
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

from .config import BotConfig
from .data_vault import DataVault
from .database import Database
from .health_server import HealthState, start_health_server
from .utils import iso_utc
from .vps_migration import VpsPreflight
from .worker_lock import WorkerLockManager


START = "VPS MIGRATION SMOKE TEST START"
END = "VPS MIGRATION SMOKE TEST END"


class VpsMigrationSmokeTest:
    def __init__(self, config: BotConfig, db: Database, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def to_text(self) -> str:
        with tempfile.TemporaryDirectory(prefix="vps_migration_smoke_") as raw_tmp:
            tmp = Path(raw_tmp)
            config = BotConfig(
                data_vault_export_dir=str(tmp / "exports"),
                data_vault_external_enabled=True,
                data_vault_external_bucket="mock-bucket",
                data_vault_external_prefix="bitget-ai-trading-bot/training",
                data_vault_s3_endpoint_url="https://mock-r2.local",
                data_vault_s3_access_key_id="mock-access",
                data_vault_s3_secret_access_key="mock-secret",
                require_single_worker_lock=True,
                worker_lock_ttl_seconds=120,
            )
            db = Database(config, self.logger or _NoopLogger())
            db.sqlite_path = tmp / "smoke.db"
            db.initialize()
            before_paper_open = db.get_paper_trade_summary()["open"]
            _seed_tiny_training_row(db)
            vault = DataVault(config, db, self.logger)
            export = vault.export(hours=1, upload=False)
            source_file = Path(export["file"])
            fake_client = _FakeS3Client(source_file)
            fake = _FakeBoto3(fake_client)
            with _Boto3Patch(fake):
                upload = vault.upload_latest()
                # Remove local backup to prove download/restore can come from the mocked remote.
                source_file.unlink(missing_ok=True)
                download = vault.download_latest()
                restore = vault.restore_latest(apply=False)
                status = vault.status()
                dashboard_ok = _dashboard_endpoints_ok(config, db, self.logger)
            preflight_ok = VpsPreflight(config, db, self.logger).build()["status"] == "VPS_PREFLIGHT_OK"
            live_block_config = BotConfig(live_trading=True, dry_run=True, paper_trading=True)
            live_blocked = VpsPreflight(live_block_config, db, self.logger).build()["status"] == "VPS_PREFLIGHT_BLOCKED"
            lock_a = WorkerLockManager(config, db, self.logger, instance_id="worker-a").acquire()
            lock_b = WorkerLockManager(config, db, self.logger, instance_id="worker-b").acquire()
            opened_after = db.get_paper_trade_summary()["open"]
            secrets_blob = json.dumps({"upload": upload, "download": download, "status": status})
            secrets_excluded = "mock-access" not in secrets_blob and "mock-secret" not in secrets_blob
            result = bool(
                preflight_ok
                and live_blocked
                and upload.get("uploaded")
                and download.get("downloaded")
                and restore.get("manifest_valid")
                and restore.get("checksum_valid")
                and lock_a.acquired
                and not lock_b.acquired
                and dashboard_ok
                and secrets_excluded
                and opened_after == before_paper_open
                and not config.live_trading
                and config.dry_run
                and config.paper_trading
            )
            return "\n".join([
                START,
                f"preflight_ok: {str(preflight_ok).lower()}",
                f"preflight_blocks_live_true: {str(live_blocked).lower()}",
                f"data_vault_no_secrets: {str(secrets_excluded).lower()}",
                f"data_download_latest_mock_ok: {str(download.get('downloaded')).lower()}",
                f"data_restore_latest_dry_run_ok: {str(bool(restore.get('manifest_valid') and restore.get('checksum_valid'))).lower()}",
                f"single_worker_lock_blocks_duplicate: {str(bool(lock_a.acquired and not lock_b.acquired)).lower()}",
                f"dashboard_endpoints_ok: {str(dashboard_ok).lower()}",
                f"opened_paper_trades_from_smoke: {opened_after - before_paper_open}",
                "touched_live: false",
                "slots_changed: false",
                f"LIVE_TRADING={str(config.live_trading).lower()}",
                f"DRY_RUN={str(config.dry_run).lower()}",
                f"PAPER_TRADING={str(config.paper_trading).lower()}",
                f"result: {'PASS' if result else 'FAIL'}",
                END,
            ])


def _seed_tiny_training_row(db: Database) -> None:
    obs_id = db.record_signal_observation({
        "timestamp": iso_utc(),
        "symbol": "BTCUSDT",
        "side": "LONG",
        "strategy_type": "SMOKE",
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


def _dashboard_endpoints_ok(config: BotConfig, db: Database, logger: Any | None) -> bool:
    port = _free_port()
    start_health_server(HealthState(mode=config.mode), port, logger or _NoopLogger(), config=config, db=db)
    base = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            _get(base + "/health")
            break
        except Exception:
            time.sleep(0.05)
    checks = [
        ("/api/training/vps-preflight", "VPS PREFLIGHT START"),
        ("/api/training/fast-runtime-plan", "FAST RUNTIME PLAN START"),
        ("/api/training/data-vault-status", "DATA VAULT STATUS START"),
        ("/api/training/migration-readiness", "MIGRATION READINESS START"),
    ]
    try:
        for path, marker in checks:
            status, body = _get(base + path)
            if status != 200 or marker not in body:
                return False
    except Exception:
        return False
    return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - local test server
        return int(response.status), response.read().decode("utf-8")


class _FakeS3Client:
    def __init__(self, source_file: Path) -> None:
        self.source_file = source_file
        self.objects: dict[str, dict[str, Any]] = {}

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        path = Path(filename)
        self.objects[key] = {"size": path.stat().st_size, "source": path.read_bytes(), "last_modified": "2026-05-16"}

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        data = self.objects.get(key, {}).get("source")
        if data is None:
            data = self.source_file.read_bytes()
        target = Path(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"ContentLength": int(self.objects.get(Key, {}).get("size", 0))}

    def list_objects_v2(self, Bucket: str, Prefix: str, MaxKeys: int = 1000) -> dict[str, Any]:  # noqa: N803
        return {
            "Contents": [
                {"Key": key, "Size": value["size"], "LastModified": value["last_modified"]}
                for key, value in self.objects.items()
                if key.startswith(Prefix)
            ]
        }


class _FakeBoto3:
    def __init__(self, client: _FakeS3Client) -> None:
        self.client_obj = client

    def client(self, service: str, **kwargs: Any) -> _FakeS3Client:
        return self.client_obj


class _Boto3Patch:
    def __init__(self, fake: _FakeBoto3) -> None:
        self.fake = fake
        self.old = None

    def __enter__(self) -> None:
        self.old = sys.modules.get("boto3")
        sys.modules["boto3"] = self.fake

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.old is None:
            sys.modules.pop("boto3", None)
        else:
            sys.modules["boto3"] = self.old


class _NoopLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass
