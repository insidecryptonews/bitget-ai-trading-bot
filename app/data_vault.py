from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import threading
import time
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
MIGRATION_DEEP_START = "MIGRATION READINESS DEEP CHECK START"
MIGRATION_DEEP_END = "MIGRATION READINESS DEEP CHECK END"

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
EXPORT_CHUNK_SIZE = 5000
_BACKUP_LOCK = threading.Lock()


class DataVault:
    def __init__(self, config: BotConfig, db: Database, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger
        self.export_dir = _export_dir(config)

    def status(self) -> dict[str, Any]:
        backups = self.list_backups()
        latest = backups[-1] if backups else None
        latest_size = latest.stat().st_size if latest and latest.exists() else 0
        external = DataVaultExternalStorage(self.config, self.logger)
        remote = external.list_backups() if self.config.data_vault_external_enabled else _empty_remote_status()
        state = self._read_state()
        if self.config.data_vault_external_enabled and remote.get("remote_list_ok"):
            self._update_cache({
                "latest_remote_backup": remote.get("latest_remote_backup", ""),
                "latest_remote_key": remote.get("latest_remote_backup", ""),
                "remote_backup_count": safe_int(remote.get("remote_backup_count")),
                "remote_list_ok": True,
                "last_upload_status": state.get("last_upload_status") or remote.get("last_upload_status") or "none",
                "last_upload_error": remote.get("sanitized_error") or "none",
            })
            state = self._read_state()
        return {
            "export_dir": str(self.export_dir),
            "local_backup_count": len(backups),
            "backup_count": len(backups),
            "latest_local_backup": str(latest) if latest else "",
            "latest_backup": str(latest) if latest else "",
            "latest_local_backup_size_mb": latest_size / (1024 * 1024) if latest_size else 0.0,
            "latest_backup_age_hours": _age_hours(latest) if latest else None,
            "external_enabled": bool(self.config.data_vault_external_enabled),
            "external_provider": self.config.data_vault_external_provider,
            "external_configured": _external_configured(self.config),
            "remote_list_ok": bool(remote.get("remote_list_ok", False)),
            "remote_backup_count": int(remote.get("remote_backup_count", 0) or 0),
            "latest_remote_backup": remote.get("latest_remote_backup", ""),
            "last_upload_status": state.get("last_upload_status") or remote.get("last_upload_status") or "none",
            "last_upload_remote_key": state.get("last_upload_remote_key") or remote.get("latest_remote_backup") or "",
            "last_upload_verified": bool(state.get("last_upload_verified", False)),
            "last_upload_error": state.get("last_upload_error") or remote.get("sanitized_error") or "none",
            "manifest_known_valid": _known_bool(state.get("manifest_valid")),
            "checksum_known_valid": _known_bool(state.get("checksum_valid")),
            "import_dry_run_last_ok": _known_bool(state.get("import_dry_run_ok")),
            "streaming_export": True,
            "memory_safe_export": True,
            "backup_in_progress": _BACKUP_LOCK.locked(),
            "secrets_excluded": True,
            "final_recommendation": "NO LIVE",
        }

    def status_text(self) -> str:
        payload = self.status()
        return "\n".join([
            STATUS_START,
            f"export_dir: {payload['export_dir']}",
            f"local_backup_count: {payload['local_backup_count']}",
            f"latest_local_backup: {payload['latest_local_backup'] or 'none'}",
            f"latest_local_backup_size_mb: {payload['latest_local_backup_size_mb']:.2f}",
            f"external_enabled: {str(payload['external_enabled']).lower()}",
            f"external_provider: {payload['external_provider']}",
            f"external_configured: {str(payload['external_configured']).lower()}",
            f"remote_list_ok: {str(payload['remote_list_ok']).lower()}",
            f"remote_backup_count: {payload['remote_backup_count']}",
            f"latest_remote_backup: {payload['latest_remote_backup'] or 'none'}",
            f"last_upload_status: {payload['last_upload_status']}",
            f"last_upload_verified: {str(payload['last_upload_verified']).lower()}",
            f"last_upload_error: {payload['last_upload_error']}",
            f"manifest_known_valid: {_format_known(payload['manifest_known_valid'])}",
            f"checksum_known_valid: {_format_known(payload['checksum_known_valid'])}",
            f"import_dry_run_last_ok: {_format_known(payload['import_dry_run_last_ok'])}",
            f"streaming_export: {str(payload['streaming_export']).lower()}",
            f"memory_safe_export: {str(payload['memory_safe_export']).lower()}",
            f"backup_in_progress: {str(payload['backup_in_progress']).lower()}",
            "secrets_excluded: true",
            "final_recommendation: NO LIVE",
            STATUS_END,
        ])

    def export(self, *, hours: int = 168, upload: bool = False) -> dict[str, Any]:
        if not _BACKUP_LOCK.acquire(blocking=False):
            return {
                "hours": hours,
                "file": "",
                "manifest_valid": False,
                "tables": [],
                "checksums_created": False,
                "secrets_excluded": True,
                "backup_in_progress": True,
                "streaming_export": True,
                "memory_safe_export": True,
                "external_upload": {"enabled": bool(self.config.data_vault_external_enabled), "attempted": False, "uploaded": False, "sanitized_error": "backup_in_progress"},
                "final_recommendation": "NO LIVE",
            }
        hours = max(1, int(hours or 168))
        started = time.time()
        export_started_at = datetime.now(timezone.utc).isoformat()
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
                path = tables_dir / f"{table}.jsonl.gz"
                row_count = _write_table_jsonl_gz_streaming(self.db, table, path, since_iso=since, timestamp_column=ts_col)
                checksum = _sha256_file(path)
                rel = f"tables/{path.name}"
                files.append({"path": rel, "sha256": checksum, "bytes": path.stat().st_size, "rows": row_count, "table": table})
                tables_summary.append({"table": table, "rows": row_count, "exported": True, "file": rel})
            export_finished_at = datetime.now(timezone.utc).isoformat()
            manifest = {
                "export_id": export_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "export_started_at": export_started_at,
                "export_finished_at": export_finished_at,
                "duration_seconds": round(time.time() - started, 3),
                "git_commit": _git_commit(),
                "schema_version": "training_vault_v1",
                "hours": hours,
                "memory_safe": True,
                "streaming_export": True,
                "tables": tables_summary,
                "files": files,
                "excluded_sensitive_fields": ["api_key", "secret", "token", "password", "passphrase", "private_key", "dashboard_auth_token"],
                "secrets_excluded": True,
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
            manifest["backup_sha256"] = _sha256_file(backup_path)
            # Refresh manifest inside zip with final size.
            _write_json(work_dir / "manifest.json", manifest)
            _zip_dir(work_dir, backup_path)
            manifest["total_compressed_size"] = backup_path.stat().st_size
            manifest["backup_sha256"] = _sha256_file(backup_path)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            _BACKUP_LOCK.release()
        local_size = backup_path.stat().st_size
        max_bytes = max(1, int(self.config.data_vault_max_backup_mb or 1500)) * 1024 * 1024
        warn_bytes = max(1, int(self.config.data_vault_warn_backup_mb or 500)) * 1024 * 1024
        size_warning = "too_large" if local_size > max_bytes else "warn_large" if local_size > warn_bytes else "none"
        external = DataVaultExternalStorage(self.config, self.logger).upload(backup_path, manifest) if upload else _upload_skipped(self.config)
        if external.get("uploaded"):
            self.prune_local_backups(apply=True)
        self._write_state({
            "last_export_file": str(backup_path),
            "latest_local_backup": str(backup_path),
            "latest_backup_size": local_size,
            "latest_sha256": manifest.get("backup_sha256", ""),
            "manifest_valid": True,
            "checksum_valid": True,
            "source": "local",
            "secrets_excluded": True,
            "last_upload_status": "uploaded" if external.get("uploaded") else "failed" if external.get("attempted") else "none",
            "upload_verified": bool(external.get("verified", False)),
            "last_upload_remote_key": external.get("remote_key", ""),
            "latest_remote_backup": external.get("remote_key", ""),
            "latest_remote_key": external.get("remote_key", ""),
            "last_upload_verified": bool(external.get("verified", False)),
            "last_upload_error": external.get("sanitized_error") or external.get("error") or "none",
            "error_sanitized": external.get("sanitized_error") or external.get("error") or "none",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        return {
            "hours": hours,
            "file": str(backup_path),
            "manifest_valid": True,
            "tables": tables_summary,
            "checksums_created": True,
            "checksum_sha256": manifest.get("backup_sha256", ""),
            "local_size_bytes": local_size,
            "size_warning": size_warning,
            "streaming_export": True,
            "memory_safe_export": True,
            "backup_in_progress": False,
            "secrets_excluded": True,
            "external_upload": external,
            "final_recommendation": "NO LIVE",
        }

    def export_text(self, *, hours: int = 168, upload: bool = False) -> str:
        payload = self.export(hours=hours, upload=upload)
        external = payload["external_upload"]
        return "\n".join([
            EXPORT_START,
            f"hours: {payload['hours']}",
            f"file: {payload['file']}",
            f"manifest_valid: {str(payload['manifest_valid']).lower()}",
            "tables:",
            *[f"- {row['table']} rows={row.get('rows', 0)} exported={str(row.get('exported', False)).lower()}" for row in payload["tables"]],
            f"checksums_created: {str(payload['checksums_created']).lower()}",
            f"secrets_excluded: {str(payload['secrets_excluded']).lower()}",
            f"streaming_export: {str(payload.get('streaming_export', True)).lower()}",
            f"memory_safe_export: {str(payload.get('memory_safe_export', True)).lower()}",
            "external_upload:",
            f"- enabled: {str(external.get('enabled', False)).lower()}",
            f"- provider: {external.get('provider', self.config.data_vault_external_provider)}",
            f"- configured: {str(external.get('configured', _external_configured(self.config))).lower()}",
            f"- attempted: {str(external.get('attempted', False)).lower()}",
            f"- uploaded: {str(external.get('uploaded', False)).lower()}",
            f"- remote_key: {external.get('remote_key', '')}",
            f"- remote_size_bytes: {external.get('remote_size_bytes', 0)}",
            f"- local_size_bytes: {external.get('local_size_bytes', payload.get('local_size_bytes', 0))}",
            f"- checksum_sha256: {external.get('checksum_sha256', payload.get('checksum_sha256', ''))}",
            f"- verified: {str(external.get('verified', False)).lower()}",
            f"- sanitized_error: {external.get('sanitized_error', external.get('error', 'none')) or 'none'}",
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
        if not apply:
            self._update_cache({
                "latest_local_backup": str(path),
                "manifest_valid": True,
                "checksum_valid": True,
                "import_dry_run_ok": True,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "source": "local",
                "secrets_excluded": True,
                "error_sanitized": "none",
            })
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

    def upload_latest(self) -> dict[str, Any]:
        latest = self.latest_valid_backup()
        if latest is None:
            return {
                "latest_local_backup": "",
                "manifest_valid": False,
                "checksum_valid": False,
                "external_enabled": bool(self.config.data_vault_external_enabled),
                "external_configured": _external_configured(self.config),
                "uploaded": False,
                "verified": False,
                "remote_key": "",
                "sanitized_error": "no_local_backup",
            }
        try:
            manifest, _ = self.validate_backup(latest)
            manifest_valid = checksum_valid = True
        except Exception as exc:
            return {
                "latest_local_backup": str(latest),
                "manifest_valid": False,
                "checksum_valid": False,
                "external_enabled": bool(self.config.data_vault_external_enabled),
                "external_configured": _external_configured(self.config),
                "uploaded": False,
                "verified": False,
                "remote_key": "",
                "sanitized_error": _sanitize_text(str(exc))[:300],
            }
        result = DataVaultExternalStorage(self.config, self.logger).upload(latest, manifest)
        self._write_state({
            "last_export_file": str(latest),
            "latest_local_backup": str(latest),
            "latest_backup_size": latest.stat().st_size,
            "latest_sha256": _sha256_file(latest),
            "manifest_valid": manifest_valid,
            "checksum_valid": checksum_valid,
            "source": "local",
            "secrets_excluded": True,
            "last_upload_status": "uploaded" if result.get("uploaded") else "failed" if result.get("attempted") else "none",
            "upload_verified": bool(result.get("verified", False)),
            "last_upload_remote_key": result.get("remote_key", ""),
            "latest_remote_backup": result.get("remote_key", ""),
            "latest_remote_key": result.get("remote_key", ""),
            "last_upload_verified": bool(result.get("verified", False)),
            "last_upload_error": result.get("sanitized_error") or result.get("error") or "none",
            "error_sanitized": result.get("sanitized_error") or result.get("error") or "none",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        return {
            "latest_local_backup": str(latest),
            "manifest_valid": manifest_valid,
            "checksum_valid": checksum_valid,
            "external_enabled": bool(result.get("enabled", False)),
            "external_configured": bool(result.get("configured", False)),
            "uploaded": bool(result.get("uploaded", False)),
            "remote_key": result.get("remote_key", ""),
            "verified": bool(result.get("verified", False)),
            "remote_size_bytes": result.get("remote_size_bytes", 0),
            "local_size_bytes": result.get("local_size_bytes", latest.stat().st_size),
            "sanitized_error": result.get("sanitized_error") or result.get("error") or "none",
        }

    def upload_latest_text(self) -> str:
        payload = self.upload_latest()
        return "\n".join([
            "DATA UPLOAD LATEST START",
            f"latest_local_backup: {payload['latest_local_backup'] or 'none'}",
            f"manifest_valid: {str(payload['manifest_valid']).lower()}",
            f"checksum_valid: {str(payload['checksum_valid']).lower()}",
            f"external_enabled: {str(payload['external_enabled']).lower()}",
            f"external_configured: {str(payload['external_configured']).lower()}",
            f"uploaded: {str(payload['uploaded']).lower()}",
            f"remote_key: {payload['remote_key']}",
            f"verified: {str(payload['verified']).lower()}",
            f"sanitized_error: {payload['sanitized_error']}",
            "DATA UPLOAD LATEST END",
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

    def latest_valid_backup(self) -> Path | None:
        for path in reversed(self.list_backups()):
            try:
                self.validate_backup(path)
                return path
            except Exception:
                continue
        return None

    def migration_readiness(self) -> dict[str, Any]:
        backups = self.list_backups()
        latest = backups[-1] if backups else None
        remote = DataVaultExternalStorage(self.config, self.logger).list_backups() if self.config.data_vault_external_enabled else _empty_remote_status()
        state = self._read_state()
        if self.config.data_vault_external_enabled and remote.get("remote_list_ok"):
            self._update_cache({
                "latest_remote_backup": remote.get("latest_remote_backup", ""),
                "latest_remote_key": remote.get("latest_remote_backup", ""),
                "remote_backup_count": safe_int(remote.get("remote_backup_count")),
                "remote_list_ok": True,
                "last_upload_status": state.get("last_upload_status") or remote.get("last_upload_status") or "none",
                "last_upload_error": remote.get("sanitized_error") or "none",
            })
            state = self._read_state()
        remote_verified = bool(
            remote.get("remote_list_ok")
            and safe_int(remote.get("remote_backup_count")) > 0
            and (state.get("last_upload_verified") is True or state.get("upload_verified") is True or bool(remote.get("latest_remote_backup")))
        )
        source = "both" if latest and remote_verified else "local" if latest else "remote" if remote_verified else "none"
        manifest_known = _known_bool(state.get("manifest_valid"))
        checksum_known = _known_bool(state.get("checksum_valid"))
        import_ok = _known_bool(state.get("import_dry_run_ok"))
        ready: bool | str = bool((latest or remote_verified) and manifest_known is True and checksum_known is True and import_ok is True)
        deep_required = not ready
        if latest or remote_verified:
            if ready is False and (manifest_known == "unknown" or checksum_known == "unknown" or import_ok == "unknown"):
                ready = "partial"
        return {
            "mode": "lightweight",
            "backup_exists": bool(latest),
            "readiness_status": "ready" if ready is True else "partial" if ready == "partial" else "not_ready",
            "reason": "heavy_verification_required" if deep_required and (latest or remote_verified) else "no_backup" if not latest and not remote_verified else "ready",
            "next_action": "run migration-readiness-deep-check" if deep_required and (latest or remote_verified) else "none",
            "latest_backup_source": source,
            "latest_backup": str(latest) if latest else "",
            "latest_local_backup": str(latest) if latest else "",
            "latest_remote_backup": remote.get("latest_remote_backup", ""),
            "latest_remote_key": state.get("latest_remote_key") or remote.get("latest_remote_backup", ""),
            "local_backup_count": len(backups),
            "remote_backup_count": safe_int(remote.get("remote_backup_count")),
            "last_upload_status": state.get("last_upload_status") or remote.get("last_upload_status") or "none",
            "last_upload_verified": bool(state.get("last_upload_verified", False) or state.get("upload_verified", False)),
            "manifest_known_valid": manifest_known,
            "checksum_known_valid": checksum_known,
            "import_dry_run_last_ok": import_ok,
            "manifest_valid": manifest_known,
            "checksum_valid": checksum_known,
            "import_dry_run_ok": import_ok,
            "external_backup_configured": _external_configured(self.config),
            "remote_verified": remote_verified,
            "secrets_excluded": True,
            "ready_for_vps_migration": ready,
            "deep_check_required": deep_required,
            "final_recommendation": "NO LIVE",
        }

    def migration_readiness_text(self) -> str:
        payload = self.migration_readiness()
        return "\n".join([
            MIGRATION_START,
            "mode: lightweight",
            f"backup_exists: {str(payload['backup_exists']).lower()}",
            f"readiness_status: {payload['readiness_status']}",
            f"reason: {payload['reason']}",
            f"next_action: {payload['next_action']}",
            f"latest_backup_source: {payload['latest_backup_source']}",
            f"latest_local_backup: {payload['latest_local_backup'] or 'none'}",
            f"latest_remote_backup: {payload['latest_remote_backup'] or 'none'}",
            f"local_backup_count: {payload['local_backup_count']}",
            f"remote_backup_count: {payload['remote_backup_count']}",
            f"last_upload_status: {payload['last_upload_status']}",
            f"last_upload_verified: {str(payload['last_upload_verified']).lower()}",
            f"manifest_known_valid: {_format_known(payload['manifest_known_valid'])}",
            f"checksum_known_valid: {_format_known(payload['checksum_known_valid'])}",
            f"import_dry_run_last_ok: {_format_known(payload['import_dry_run_last_ok'])}",
            f"external_backup_configured: {str(payload['external_backup_configured']).lower()}",
            f"remote_verified: {str(payload['remote_verified']).lower()}",
            "secrets_excluded: true",
            f"ready_for_vps_migration: {_format_known(payload['ready_for_vps_migration'])}",
            f"deep_check_required: {str(payload['deep_check_required']).lower()}",
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

    def migration_readiness_deep_check(self) -> dict[str, Any]:
        backups = self.list_backups()
        latest = backups[-1] if backups else None
        if latest is None:
            payload = {
                "backup_source_checked": "none",
                "latest_backup": "",
                "manifest_valid": False,
                "checksum_valid": False,
                "import_dry_run_ok": False,
                "cache_updated": False,
                "ready_for_vps_migration": False,
                "error_sanitized": "no_local_backup_for_deep_check",
                "final_recommendation": "NO LIVE",
            }
            self._update_cache(payload)
            return payload
        error = "none"
        manifest_valid = checksum_valid = import_ok = False
        try:
            manifest, _ = self.validate_backup(latest)
            manifest_valid = checksum_valid = True
            self.import_backup(file=latest, apply=False)
            import_ok = True
            sha = manifest.get("backup_sha256") or _sha256_file(latest)
        except Exception as exc:
            sha = _sha256_file(latest) if latest.exists() else ""
            error = _sanitize_text(str(exc))[:300]
        cache = {
            "latest_local_backup": str(latest),
            "latest_backup_size": latest.stat().st_size,
            "latest_sha256": sha,
            "manifest_valid": manifest_valid,
            "checksum_valid": checksum_valid,
            "import_dry_run_ok": import_ok,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "source": "local",
            "secrets_excluded": True,
            "error_sanitized": error,
        }
        self._update_cache(cache)
        return {
            "backup_source_checked": "local",
            "latest_backup": str(latest),
            "manifest_valid": manifest_valid,
            "checksum_valid": checksum_valid,
            "import_dry_run_ok": import_ok,
            "cache_updated": True,
            "ready_for_vps_migration": bool(manifest_valid and checksum_valid and import_ok),
            "error_sanitized": error,
            "final_recommendation": "NO LIVE",
        }

    def migration_readiness_deep_check_text(self) -> str:
        payload = self.migration_readiness_deep_check()
        return "\n".join([
            MIGRATION_DEEP_START,
            f"backup_source_checked: {payload['backup_source_checked']}",
            f"latest_backup: {payload['latest_backup'] or 'none'}",
            f"manifest_valid: {str(payload['manifest_valid']).lower()}",
            f"checksum_valid: {str(payload['checksum_valid']).lower()}",
            f"import_dry_run_ok: {str(payload['import_dry_run_ok']).lower()}",
            f"cache_updated: {str(payload['cache_updated']).lower()}",
            f"ready_for_vps_migration: {str(payload['ready_for_vps_migration']).lower()}",
            f"error_sanitized: {payload.get('error_sanitized') or 'none'}",
            "final_recommendation: NO LIVE",
            MIGRATION_DEEP_END,
        ])

    def list_backups(self) -> list[Path]:
        if not self.export_dir.exists():
            return []
        return sorted(self.export_dir.glob("training_vault_*.zip"), key=lambda path: path.stat().st_mtime)

    def prune_local_backups(self, *, apply: bool = True) -> dict[str, Any]:
        backups = self.list_backups()
        keep = max(1, int(self.config.data_vault_max_backups_local or 2))
        valid_latest = self.latest_valid_backup()
        keep_set = set(backups[-keep:])
        if valid_latest:
            keep_set.add(valid_latest)
        delete = [path for path in backups if path not in keep_set]
        deleted: list[str] = []
        if apply and not _BACKUP_LOCK.locked():
            for path in delete:
                try:
                    path.unlink()
                    deleted.append(str(path))
                except OSError:
                    pass
        kept = [str(path) for path in self.list_backups()] if apply else [str(path) for path in backups if path not in delete]
        return {
            "mode": "apply" if apply else "dry-run",
            "local_before": len(backups),
            "local_after": len(kept),
            "deleted": deleted if apply else [str(path) for path in delete],
            "kept": kept,
            "never_deleted_latest_valid": bool(valid_latest is None or str(valid_latest) in kept),
        }

    def prune_text(self, *, apply: bool = False) -> str:
        payload = self.prune_local_backups(apply=apply)
        deleted_lines = [f"- {item}" for item in payload["deleted"][:20]] if payload["deleted"] else ["- none"]
        kept_lines = [f"- {item}" for item in payload["kept"][:20]] if payload["kept"] else ["- none"]
        return "\n".join([
            "DATA VAULT PRUNE START",
            f"mode: {payload['mode']}",
            f"local_before: {payload['local_before']}",
            f"local_after: {payload['local_after']}",
            "deleted:",
            *deleted_lines,
            "kept:",
            *kept_lines,
            f"never_deleted_latest_valid: {str(payload['never_deleted_latest_valid']).lower()}",
            "DATA VAULT PRUNE END",
        ])

    def _state_path(self) -> Path:
        return self.export_dir / "data_vault_state.json"

    def _read_state(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_path().read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_state(self, payload: dict[str, Any]) -> None:
        try:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            self._state_path().write_text(json.dumps(_sanitize_value(payload), indent=2, ensure_ascii=True), encoding="utf-8")
        except OSError:
            pass

    def _update_cache(self, payload: dict[str, Any]) -> None:
        state = self._read_state()
        state.update(_sanitize_value(payload))
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_state(state)


class DataVaultExternalStorage:
    def __init__(self, config: BotConfig, logger: Any | None = None) -> None:
        self.config = config
        self.logger = logger

    def upload(self, path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        local_size = path.stat().st_size if path.exists() else 0
        checksum = _sha256_file(path) if path.exists() else ""
        if not self.config.data_vault_external_enabled:
            return {"enabled": False, "provider": self.config.data_vault_external_provider, "configured": False, "attempted": False, "uploaded": False, "verified": False, "local_size_bytes": local_size, "checksum_sha256": checksum, "sanitized_error": "none"}
        provider = self.config.data_vault_external_provider or "s3_compatible"
        if provider != "s3_compatible":
            return {"enabled": True, "provider": provider, "configured": False, "attempted": False, "uploaded": False, "verified": False, "local_size_bytes": local_size, "checksum_sha256": checksum, "sanitized_error": "provider_not_implemented"}
        if not _external_configured(self.config):
            return {"enabled": True, "provider": provider, "configured": False, "attempted": False, "uploaded": False, "verified": False, "local_size_bytes": local_size, "checksum_sha256": checksum, "sanitized_error": "missing_s3_configuration"}
        try:
            import boto3  # type: ignore
        except Exception:
            return {"enabled": True, "provider": provider, "configured": True, "attempted": False, "uploaded": False, "verified": False, "local_size_bytes": local_size, "checksum_sha256": checksum, "sanitized_error": "boto3_unavailable_external_upload_skipped"}
        remote_key = f"{self.config.data_vault_external_prefix.strip('/')}/{path.name}"
        try:
            client = self._client(boto3)
            client.upload_file(str(path), self.config.data_vault_external_bucket, remote_key)
            remote_size = 0
            verified = False
            try:
                head = client.head_object(Bucket=self.config.data_vault_external_bucket, Key=remote_key)
                remote_size = int(head.get("ContentLength") or 0)
                verified = remote_size == local_size
            except Exception:
                verified = False
            return {
                "enabled": True,
                "provider": provider,
                "configured": True,
                "attempted": True,
                "uploaded": True,
                "remote_key": remote_key,
                "remote_size_bytes": remote_size,
                "local_size_bytes": local_size,
                "checksum_sha256": checksum,
                "verified": verified,
                "sanitized_error": "none" if verified else "head_object_failed_or_size_mismatch",
            }
        except Exception as exc:
            return {
                "enabled": True,
                "provider": provider,
                "configured": True,
                "attempted": True,
                "uploaded": False,
                "remote_key": remote_key,
                "remote_size_bytes": 0,
                "local_size_bytes": local_size,
                "checksum_sha256": checksum,
                "verified": False,
                "sanitized_error": _sanitize_text(str(exc))[:300],
            }

    def list_backups(self) -> dict[str, Any]:
        if not self.config.data_vault_external_enabled:
            return _empty_remote_status()
        if not _external_configured(self.config):
            return {**_empty_remote_status(), "remote_list_ok": False, "sanitized_error": "missing_s3_configuration"}
        try:
            import boto3  # type: ignore
        except Exception:
            return {**_empty_remote_status(), "remote_list_ok": False, "sanitized_error": "boto3_unavailable"}
        try:
            client = self._client(boto3)
            prefix = self.config.data_vault_external_prefix.strip("/") + "/"
            response = client.list_objects_v2(Bucket=self.config.data_vault_external_bucket, Prefix=prefix, MaxKeys=1000)
            objects = [item for item in response.get("Contents", []) if str(item.get("Key", "")).endswith(".zip")]
            objects.sort(key=lambda item: str(item.get("LastModified", "")))
            latest = objects[-1] if objects else {}
            return {
                "remote_list_ok": True,
                "remote_backup_count": len(objects),
                "latest_remote_backup": latest.get("Key", ""),
                "last_upload_status": "uploaded" if objects else "none",
                "sanitized_error": "none",
            }
        except Exception as exc:
            return {**_empty_remote_status(), "remote_list_ok": False, "sanitized_error": _sanitize_text(str(exc))[:300]}

    def _client(self, boto3_module: Any) -> Any:
        return boto3_module.client(
            "s3",
            endpoint_url=self.config.data_vault_s3_endpoint_url or None,
            region_name=self.config.data_vault_s3_region or "auto",
            aws_access_key_id=self.config.data_vault_s3_access_key_id,
            aws_secret_access_key=self.config.data_vault_s3_secret_access_key,
        )


def _write_jsonl_gz(path: Path, rows: list[dict[str, Any]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True, default=str))
            handle.write("\n")


def _write_table_jsonl_gz_streaming(db: Database, table: str, path: Path, *, since_iso: str, timestamp_column: str | None) -> int:
    count = 0
    offset = 0
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        while True:
            rows = db.fetch_table_rows_chunk(
                table,
                since_iso=since_iso,
                timestamp_column=timestamp_column,
                limit=EXPORT_CHUNK_SIZE,
                offset=offset,
            )
            if not rows:
                break
            for row in rows:
                handle.write(json.dumps(_sanitize_row(row), ensure_ascii=True, default=str))
                handle.write("\n")
                count += 1
            if len(rows) < EXPORT_CHUNK_SIZE:
                break
            offset += EXPORT_CHUNK_SIZE
    return count


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


def _upload_skipped(config: BotConfig) -> dict[str, Any]:
    return {
        "enabled": bool(config.data_vault_external_enabled),
        "provider": config.data_vault_external_provider,
        "configured": _external_configured(config),
        "attempted": False,
        "uploaded": False,
        "remote_key": "",
        "remote_size_bytes": 0,
        "local_size_bytes": 0,
        "checksum_sha256": "",
        "verified": False,
        "sanitized_error": "none",
    }


def _empty_remote_status() -> dict[str, Any]:
    return {
        "remote_list_ok": False,
        "remote_backup_count": 0,
        "latest_remote_backup": "",
        "last_upload_status": "none",
        "sanitized_error": "none",
    }


def _known_bool(value: Any) -> bool | str:
    if isinstance(value, bool):
        return value
    if value is None:
        return "unknown"
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return "unknown"


def _format_known(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value or "unknown")


def _age_hours(path: Path | None) -> float | None:
    if path is None:
        return None
    return max(0.0, (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 3600.0)


def data_vault_status_payload(config: BotConfig, db: Database) -> dict[str, Any]:
    return DataVault(config, db).status()


def safe_int_payload(value: Any) -> int:
    return safe_int(value)
