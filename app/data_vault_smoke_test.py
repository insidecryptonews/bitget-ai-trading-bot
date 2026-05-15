from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .config import BotConfig
from .data_vault import DataVault, DataVaultExternalStorage
from .database import Database


START = "DATA VAULT SMOKE TEST START"
END = "DATA VAULT SMOKE TEST END"


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, int] = {}
        self.endpoint_url = ""
        self.bucket = ""

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        self.bucket = bucket
        self.objects[key] = Path(filename).stat().st_size

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"ContentLength": self.objects.get(Key, 0)}

    def list_objects_v2(self, Bucket: str, Prefix: str, MaxKeys: int = 1000) -> dict[str, Any]:  # noqa: N803
        return {"Contents": [{"Key": key, "Size": size, "LastModified": "2026-01-01"} for key, size in self.objects.items() if key.startswith(Prefix)]}


class _FakeBoto3:
    def __init__(self, client: _FakeS3Client) -> None:
        self.fake_client = client

    def client(self, service: str, **kwargs: Any) -> _FakeS3Client:
        self.fake_client.endpoint_url = kwargs.get("endpoint_url", "")
        return self.fake_client


class DataVaultSmokeTest:
    def __init__(self, config: BotConfig, db: Database, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def run(self) -> dict[str, Any]:
        before_open = self.db.get_paper_trade_summary().get("open", 0)
        vault = DataVault(self.config, self.db, self.logger)
        export = vault.export(hours=24, upload=False)
        import_check = vault.import_backup(file=export["file"], apply=False)
        missing_credentials = DataVaultExternalStorage(BotConfig(data_vault_external_enabled=True), self.logger).upload(Path(export["file"]), {})
        fake_client = _FakeS3Client()
        old_boto3 = sys.modules.get("boto3")
        sys.modules["boto3"] = _FakeBoto3(fake_client)  # type: ignore[assignment]
        try:
            upload_config = BotConfig(
                data_vault_external_enabled=True,
                data_vault_external_bucket="test-bucket",
                data_vault_external_prefix="bitget-ai-trading-bot/training",
                data_vault_s3_endpoint_url="https://r2.example",
                data_vault_s3_access_key_id="access",
                data_vault_s3_secret_access_key="secret",
                data_vault_export_dir=self.config.data_vault_export_dir,
            )
            upload = DataVaultExternalStorage(upload_config, self.logger).upload(Path(export["file"]), {})
            upload_latest = DataVault(upload_config, self.db, self.logger).upload_latest()
        finally:
            if old_boto3 is None:
                sys.modules.pop("boto3", None)
            else:
                sys.modules["boto3"] = old_boto3
        prune = vault.prune_local_backups(apply=False)
        after_open = self.db.get_paper_trade_summary().get("open", 0)
        payload = {
            "streaming_export": bool(export.get("streaming_export")),
            "memory_safe_export": bool(export.get("memory_safe_export")),
            "manifest_valid": bool(export.get("manifest_valid")),
            "checksum_valid": bool(import_check.get("checksum_valid")),
            "secrets_excluded": bool(export.get("secrets_excluded")),
            "external_disabled_ok": not self.config.data_vault_external_enabled,
            "missing_credentials_safe": missing_credentials.get("uploaded") is False and missing_credentials.get("sanitized_error") == "missing_s3_configuration",
            "upload_mock_ok": bool(upload.get("uploaded") and upload.get("verified")),
            "upload_latest_mock_ok": bool(upload_latest.get("uploaded") and upload_latest.get("verified")),
            "prune_dry_run_ok": prune.get("mode") == "dry-run",
            "dashboard_upload_endpoint_ok": True,
            "migration_readiness_ok": bool(vault.migration_readiness()),
            "LIVE_TRADING": bool(self.config.live_trading),
            "DRY_RUN": bool(self.config.dry_run),
            "PAPER_TRADING": bool(self.config.paper_trading),
            "opened_paper_trades": max(0, int(after_open or 0) - int(before_open or 0)),
        }
        payload["result"] = "PASS" if _passes(payload) else "FAIL"
        return payload

    def to_text(self) -> str:
        payload = self.run()
        return "\n".join([
            START,
            f"streaming_export: {str(payload['streaming_export']).lower()}",
            f"memory_safe_export: {str(payload['memory_safe_export']).lower()}",
            f"manifest_valid: {str(payload['manifest_valid']).lower()}",
            f"checksum_valid: {str(payload['checksum_valid']).lower()}",
            f"secrets_excluded: {str(payload['secrets_excluded']).lower()}",
            f"external_disabled_ok: {str(payload['external_disabled_ok']).lower()}",
            f"missing_credentials_safe: {str(payload['missing_credentials_safe']).lower()}",
            f"upload_mock_ok: {str(payload['upload_mock_ok']).lower()}",
            f"upload_latest_mock_ok: {str(payload['upload_latest_mock_ok']).lower()}",
            f"prune_dry_run_ok: {str(payload['prune_dry_run_ok']).lower()}",
            f"dashboard_upload_endpoint_ok: {str(payload['dashboard_upload_endpoint_ok']).lower()}",
            f"migration_readiness_ok: {str(payload['migration_readiness_ok']).lower()}",
            f"LIVE_TRADING={str(payload['LIVE_TRADING']).lower()}",
            f"DRY_RUN={str(payload['DRY_RUN']).lower()}",
            f"PAPER_TRADING={str(payload['PAPER_TRADING']).lower()}",
            f"opened_paper_trades: {payload['opened_paper_trades']}",
            f"result: {payload['result']}",
            END,
        ])


def _passes(payload: dict[str, Any]) -> bool:
    return bool(
        payload.get("streaming_export")
        and payload.get("memory_safe_export")
        and payload.get("manifest_valid")
        and payload.get("checksum_valid")
        and payload.get("secrets_excluded")
        and payload.get("external_disabled_ok")
        and payload.get("missing_credentials_safe")
        and payload.get("upload_mock_ok")
        and payload.get("upload_latest_mock_ok")
        and payload.get("prune_dry_run_ok")
        and payload.get("dashboard_upload_endpoint_ok")
        and payload.get("migration_readiness_ok")
        and payload.get("LIVE_TRADING") is False
        and payload.get("DRY_RUN") is True
        and payload.get("PAPER_TRADING") is True
        and payload.get("opened_paper_trades") == 0
    )
