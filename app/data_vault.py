from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import BotConfig, PROJECT_ROOT
from .database import Database
from .utils import safe_int


STATUS_START = "DATA VAULT STATUS START"
STATUS_END = "DATA VAULT STATUS END"
EXPORT_START = "DATA EXPORT START"
EXPORT_END = "DATA EXPORT END"
IMPORT_START = "DATA IMPORT START"
IMPORT_END = "DATA IMPORT END"
MIGRATION_START = "MIGRATION READINESS START"
MIGRATION_END = "MIGRATION READINESS END"

EXPORT_TABLES: dict[str, str | None] = {
    "signal_observations": "timestamp",
    "signal_labels": "timestamp",
    "signal_path_metrics": "created_at",
    "trades": "timestamp",
    "market_catalysts": "created_at",
    "market_context_events": "created_at",
    "events": "timestamp",
    "strategy_variants": "created_at",
    "strategy_variant_results": "last_updated",
    "virtual_research_trades": "created_at",
    "virtual_strategy_summary": "last_updated",
    "research_rules": "created_at",
    "strategy_lab_candidates": "created_at",
    "strategy_lab_walkforward": "created_at",
    "strategy_lab_recommendations": "created_at",
    "kronos_predictions": "created_at",
    "latency_metrics": "timestamp",
    "research_autopilot_runs": "created_at",
}

SENSITIVE_KEY_RE = re.compile(r"(?i)(api[_-]?key|secret|token|password|passphrase|private[_-]?key|dashboard[_-]?auth)")


class DataVault:
    def __init__(self, config: BotConfig, db: Database, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger
        self.export_dir = _export_dir(config)

    def status(self) -> dict[str, Any]:
        backups = self.list_backups()
        latest = backups[-1] if backups else None
        return {
            "export_dir": str(self.export_dir),
            "backup_count": len(backups),
            "latest_backup": str(latest) if latest else "",
            "latest_backup_age_hours": _age_hours(latest) if latest else None,
            "external_enabled": bool(self.config.data_vault_external_enabled),
            "external_provider": self.config.data_vault_external_provider,
            "external_configured": _external_configured(self.config),
            "secrets_excluded": True,
            "final_recommendation": "NO LIVE",
        }

    def status_text(self) -> str:
        payload = self.status()
        return "\n".join([
            STATUS_START,
            f"export_dir: {payload['export_dir']}",
            f"backup_count: {payload['backup_count']}",
            f"latest_backup: {payload['latest_backup'] or 'none'}",
            f"external_enabled: {str(payload['external_enabled']).lower()}",
            f"external_provider: {payload['external_provider']}",
            f"external_configured: {str(payload['external_configured']).lower()}",
            "secrets_excluded: true",
            "final_recommendation: NO LIVE",
            STATUS_END,
        ])

    def export(self, *, hours: int = 168, upload: bool = False) -> dict[str, Any]:
        hours = max(1, int(hours or 168))
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        export_id = datetime.now(timezone.utc).strftime("training_vault_%Y%m%d_%H%M%S")
        self.export_dir.mkdir(parents=True, exist_ok=True)
        work_dir = self.export_dir / f"{export_id}_work"
        tables_dir = work_dir / "tables"
        tables_dir.mkdir(parents=True, exist_ok=True)
        tables_summary: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        try:
            for table, ts_col in EXPORT_TABLES.items():
                if not self.db.table_exists(table):
                    tables_summary.append({"table": table, "rows": 0, "exported": False, "reason": "missing"})
                    continue
                rows = self.db.fetch_table_rows(table, since_iso=since, timestamp_column=ts_col, limit=200000)
                clean_rows = [_sanitize_row(row) for row in rows]
                path = tables_dir / f"{table}.jsonl.gz"
                _write_jsonl_gz(path, clean_rows)
                checksum = _sha256_file(path)
                rel = f"tables/{path.name}"
                files.append({"path": rel, "sha256": checksum, "bytes": path.stat().st_size, "rows": len(clean_rows), "table": table})
                tables_summary.append({"table": table, "rows": len(clean_rows), "exported": True, "file": rel})
            manifest = {
                "export_id": export_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "git_commit": _git_commit(),
                "schema_version": "training_vault_v1",
                "hours": hours,
                "tables": tables_summary,
                "files": files,
                "excluded_sensitive_fields": ["api_key", "secret", "token", "password", "passphrase", "private_key", "dashboard_auth_token"],
                "LIVE_TRADING": bool(self.config.live_trading),
                "DRY_RUN": bool(self.config.dry_run),
                "PAPER_TRADING": bool(self.config.paper_trading),
            }
            _write_json(work_dir / "manifest.json", manifest)
            _write_json(work_dir / "schema_summary.json", {"tables": {item["table"]: self.db.get_table_columns(item["table"]) for item in tables_summary if item.get("exported")}})
            _write_json(work_dir / "export_summary.json", {"hours": hours, "tables": tables_summary, "secrets_excluded": True})
            backup_path = self.export_dir / f"{export_id}.zip"
            _zip_dir(work_dir, backup_path)
            manifest["total_compressed_size"] = backup_path.stat().st_size
            # Refresh manifest inside zip with final size.
            _write_json(work_dir / "manifest.json", manifest)
            _zip_dir(work_dir, backup_path)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        self.prune_local_backups()
        external = DataVaultExternalStorage(self.config, self.logger).upload(backup_path, manifest) if upload else {"enabled": False, "uploaded": False}
        return {
            "hours": hours,
            "file": str(backup_path),
            "manifest_valid": True,
            "tables": tables_summary,
            "checksums_created": True,
            "secrets_excluded": True,
            "external_upload": external,
            "final_recommendation": "NO LIVE",
        }

    def export_text(self, *, hours: int = 168, upload: bool = False) -> str:
        payload = self.export(hours=hours, upload=upload)
        return "\n".join([
            EXPORT_START,
            f"hours: {payload['hours']}",
            f"file: {payload['file']}",
            f"manifest_valid: {str(payload['manifest_valid']).lower()}",
            "tables:",
            *[f"- {row['table']} rows={row.get('rows', 0)} exported={str(row.get('exported', False)).lower()}" for row in payload["tables"]],
            f"checksums_created: {str(payload['checksums_created']).lower()}",
            f"secrets_excluded: {str(payload['secrets_excluded']).lower()}",
            "external_upload:",
            f"- enabled={str(payload['external_upload'].get('enabled', False)).lower()}",
            f"- uploaded={str(payload['external_upload'].get('uploaded', False)).lower()}",
            f"- remote_key={payload['external_upload'].get('remote_key', '')}",
            "final_recommendation: NO LIVE",
            EXPORT_END,
        ])

    def import_backup(self, *, file: str | Path, apply: bool = False) -> dict[str, Any]:
        path = Path(file)
        manifest, files = self.validate_backup(path)
        inserted = updated = duplicates = skipped = 0
        table_rows: list[dict[str, Any]] = []
        for file_info in manifest.get("files", []):
            table = str(file_info.get("table") or "")
            rel = str(file_info.get("path") or "")
            rows = _read_jsonl_gz_from_zip(path, rel)
            if not apply:
                table_rows.append({"table": table, "rows": len(rows), "mode": "dry-run"})
                continue
            for row in rows:
                status = self.db.insert_table_row_if_missing(table, row)
                if status == "inserted":
                    inserted += 1
                elif status == "duplicate":
                    duplicates += 1
                else:
                    skipped += 1
            table_rows.append({"table": table, "rows": len(rows), "mode": "apply"})
        return {
            "file": str(path),
            "mode": "apply" if apply else "dry-run",
            "manifest_valid": True,
            "checksum_valid": True,
            "duplicates_skipped": duplicates,
            "rows_inserted": inserted,
            "rows_updated": updated,
            "rows_skipped": skipped,
            "tables": table_rows,
            "result": "PASS",
        }

    def import_text(self, *, file: str | Path, apply: bool = False) -> str:
        payload = self.import_backup(file=file, apply=apply)
        return "\n".join([
            IMPORT_START,
            f"file: {payload['file']}",
            f"mode: {payload['mode']}",
            f"manifest_valid: {str(payload['manifest_valid']).lower()}",
            f"checksum_valid: {str(payload['checksum_valid']).lower()}",
            f"duplicates_skipped: {payload['duplicates_skipped']}",
            f"rows_inserted: {payload['rows_inserted']}",
            f"rows_updated: {payload['rows_updated']}",
            f"result: {payload['result']}",
            IMPORT_END,
        ])

    def validate_backup(self, path: Path) -> tuple[dict[str, Any], list[str]]:
        if not path.exists():
            raise FileNotFoundError(str(path))
        with zipfile.ZipFile(path, "r") as zf:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            names = zf.namelist()
            for info in manifest.get("files", []):
                rel = str(info.get("path") or "")
                if rel not in names:
                    raise ValueError(f"Missing backup file: {rel}")
                data = zf.read(rel)
                checksum = hashlib.sha256(data).hexdigest()
                if checksum != info.get("sha256"):
                    raise ValueError(f"Checksum mismatch: {rel}")
        return manifest, names

    def migration_readiness(self) -> dict[str, Any]:
        backups = self.list_backups()
        latest = backups[-1] if backups else None
        manifest_valid = checksum_valid = import_dry_run_ok = False
        if latest:
            try:
                self.validate_backup(latest)
                manifest_valid = checksum_valid = True
                self.import_backup(file=latest, apply=False)
                import_dry_run_ok = True
            except Exception as exc:
                if self.logger:
                    self.logger.warning("Data vault readiness fallo: %s", _sanitize_text(str(exc)))
        ready = bool(latest and manifest_valid and checksum_valid and import_dry_run_ok)
        return {
            "backup_exists": bool(latest),
            "latest_backup": str(latest) if latest else "",
            "manifest_valid": manifest_valid,
            "checksum_valid": checksum_valid,
            "import_dry_run_ok": import_dry_run_ok,
            "external_backup_configured": _external_configured(self.config),
            "secrets_excluded": True,
            "ready_for_vps_migration": ready,
            "final_recommendation": "NO LIVE",
        }

    def migration_readiness_text(self) -> str:
        payload = self.migration_readiness()
        return "\n".join([
            MIGRATION_START,
            f"backup_exists: {str(payload['backup_exists']).lower()}",
            f"latest_backup: {payload['latest_backup'] or 'none'}",
            f"manifest_valid: {str(payload['manifest_valid']).lower()}",
            f"checksum_valid: {str(payload['checksum_valid']).lower()}",
            f"import_dry_run_ok: {str(payload['import_dry_run_ok']).lower()}",
            f"external_backup_configured: {str(payload['external_backup_configured']).lower()}",
            "secrets_excluded: true",
            f"ready_for_vps_migration: {str(payload['ready_for_vps_migration']).lower()}",
            "next_steps:",
            "1. download latest backup or fetch from S3 compatible storage",
            "2. clone repo on VPS",
            "3. install requirements",
            "4. set env vars",
            "5. run data-import --dry-run",
            "6. run data-import --apply",
            "7. start worker in paper mode",
            "final_recommendation: NO LIVE",
            MIGRATION_END,
        ])

    def list_backups(self) -> list[Path]:
        if not self.export_dir.exists():
            return []
        return sorted(self.export_dir.glob("training_vault_*.zip"), key=lambda path: path.stat().st_mtime)

    def prune_local_backups(self) -> None:
        backups = self.list_backups()
        keep = max(1, int(self.config.data_vault_max_backups_local or 10))
        for path in backups[:-keep]:
            try:
                path.unlink()
            except OSError:
                pass


class DataVaultExternalStorage:
    def __init__(self, config: BotConfig, logger: Any | None = None) -> None:
        self.config = config
        self.logger = logger

    def upload(self, path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        if not self.config.data_vault_external_enabled:
            return {"enabled": False, "uploaded": False}
        provider = self.config.data_vault_external_provider or "s3_compatible"
        if provider != "s3_compatible":
            return {"enabled": True, "provider": provider, "uploaded": False, "error": "provider_not_implemented"}
        if not _external_configured(self.config):
            return {"enabled": True, "provider": provider, "uploaded": False, "error": "missing_s3_configuration"}
        try:
            import boto3  # type: ignore
        except Exception:
            return {"enabled": True, "provider": provider, "uploaded": False, "error": "boto3_unavailable_external_upload_skipped"}
        remote_key = f"{self.config.data_vault_external_prefix.strip('/')}/{path.name}"
        try:
            client = boto3.client(
                "s3",
                endpoint_url=self.config.data_vault_s3_endpoint_url or None,
                region_name=None if self.config.data_vault_s3_region == "auto" else self.config.data_vault_s3_region,
                aws_access_key_id=self.config.data_vault_s3_access_key_id,
                aws_secret_access_key=self.config.data_vault_s3_secret_access_key,
            )
            client.upload_file(str(path), self.config.data_vault_external_bucket, remote_key)
            return {"enabled": True, "provider": provider, "uploaded": True, "remote_key": remote_key}
        except Exception as exc:
            return {"enabled": True, "provider": provider, "uploaded": False, "remote_key": remote_key, "error": _sanitize_text(str(exc))[:300]}


def _write_jsonl_gz(path: Path, rows: list[dict[str, Any]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True, default=str))
            handle.write("\n")


def _read_jsonl_gz_from_zip(zip_path: Path, rel: str) -> list[dict[str, Any]]:
    with zipfile.ZipFile(zip_path, "r") as zf:
        with zf.open(rel) as raw:
            with gzip.GzipFile(fileobj=raw) as gz:
                return [json.loads(line.decode("utf-8")) for line in gz if line.strip()]


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True, default=str), encoding="utf-8")


def _zip_dir(src: Path, dest: Path) -> None:
    if dest.exists():
        dest.unlink()
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(src.rglob("*")):
            if file.is_file():
                zf.write(file, file.relative_to(src).as_posix())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sanitize_row(row: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in row.items():
        if SENSITIVE_KEY_RE.search(str(key)):
            continue
        clean[key] = _sanitize_value(value)
    return clean


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, dict):
        return {key: _sanitize_value(val) for key, val in value.items() if not SENSITIVE_KEY_RE.search(str(key))}
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value[:200]]
    return value


def _sanitize_text(text: str) -> str:
    return re.sub(
        r"(?i)\b(API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSPHRASE|PRIVATE[_-]?KEY|DASHBOARD[_-]?AUTH[_-]?TOKEN)\s*[:=]\s*([^\s,;]+)",
        lambda match: f"{match.group(1)}=***",
        str(text or ""),
    )


def _export_dir(config: BotConfig) -> Path:
    path = Path(config.data_vault_export_dir or "training_exports")
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _git_commit() -> str:
    head = PROJECT_ROOT / ".git" / "HEAD"
    try:
        text = head.read_text(encoding="utf-8").strip()
        if text.startswith("ref:"):
            ref = PROJECT_ROOT / ".git" / text.split(" ", 1)[1]
            return ref.read_text(encoding="utf-8").strip()[:40]
        return text[:40]
    except OSError:
        return ""


def _external_configured(config: BotConfig) -> bool:
    return bool(
        config.data_vault_external_enabled
        and config.data_vault_external_bucket
        and config.data_vault_s3_access_key_id
        and config.data_vault_s3_secret_access_key
    )


def _age_hours(path: Path | None) -> float | None:
    if path is None:
        return None
    return max(0.0, (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 3600.0)


def data_vault_status_payload(config: BotConfig, db: Database) -> dict[str, Any]:
    return DataVault(config, db).status()


def safe_int_payload(value: Any) -> int:
    return safe_int(value)
