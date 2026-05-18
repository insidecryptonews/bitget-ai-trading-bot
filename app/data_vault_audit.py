from __future__ import annotations

from pathlib import Path
from typing import Any

from .data_vault import DataVault


START = "DATA VAULT AUDIT START"
END = "DATA VAULT AUDIT END"


class DataVaultAudit:
    """Read-only Data Vault audit. It never exports, uploads, prunes, restores, or deletes."""

    def __init__(self, config: Any, db: Any, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        vault = DataVault(self.config, self.db, self.logger)
        status = vault.status()
        export_dir = Path(status.get("export_dir") or "")
        incomplete = _incomplete_work_dirs(export_dir)
        latest_age = status.get("latest_backup_age_hours")
        latest_verified = bool(status.get("last_upload_verified") or status.get("manifest_known_valid") is True)
        has_backup = bool(status.get("latest_local_backup") or status.get("latest_remote_backup"))
        backup_ok = has_backup and (latest_verified or status.get("remote_backup_count", 0) > 0)
        too_old = latest_age is not None and float(latest_age) > 48
        if incomplete or not has_backup:
            last_backup_status = "BAD" if not has_backup else "WARNING"
        elif too_old or not backup_ok:
            last_backup_status = "WARNING"
        else:
            last_backup_status = "OK"
        return {
            "hours": hours,
            "export_dir": str(export_dir),
            "latest_local_backup": status.get("latest_local_backup", ""),
            "latest_remote_backup": status.get("latest_remote_backup", ""),
            "last_backup_age_hours": latest_age,
            "last_backup_verified": latest_verified,
            "manifest_known_valid": status.get("manifest_known_valid"),
            "checksum_known_valid": status.get("checksum_known_valid"),
            "secrets_excluded": bool(status.get("secrets_excluded", True)),
            "external_upload_enabled": bool(status.get("external_enabled")),
            "external_upload_configured": bool(status.get("external_configured")),
            "last_upload_status": status.get("last_upload_status", "none"),
            "last_upload_verified": bool(status.get("last_upload_verified")),
            "remote_backup_count": int(status.get("remote_backup_count", 0) or 0),
            "local_backup_count": int(status.get("local_backup_count", 0) or 0),
            "incomplete_work_dirs": incomplete,
            "last_backup_status": last_backup_status,
            "recommendation": "CHECK_BACKUP" if last_backup_status != "OK" else "KEEP_RESEARCH",
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"latest_local_backup: {payload['latest_local_backup'] or 'none'}",
            f"latest_remote_backup: {payload['latest_remote_backup'] or 'none'}",
            f"last_backup_age_hours: {payload['last_backup_age_hours']}",
            f"last_backup_verified: {str(payload['last_backup_verified']).lower()}",
            f"manifest_known_valid: {payload['manifest_known_valid']}",
            f"checksum_known_valid: {payload['checksum_known_valid']}",
            f"secrets_excluded: {str(payload['secrets_excluded']).lower()}",
            f"external_upload_enabled: {str(payload['external_upload_enabled']).lower()}",
            f"external_upload_configured: {str(payload['external_upload_configured']).lower()}",
            f"last_upload_status: {payload['last_upload_status']}",
            f"last_upload_verified: {str(payload['last_upload_verified']).lower()}",
            f"remote_backup_count: {payload['remote_backup_count']}",
            f"local_backup_count: {payload['local_backup_count']}",
            "incomplete_work_dirs:",
            *([f"- {item}" for item in payload["incomplete_work_dirs"]] if payload["incomplete_work_dirs"] else ["- none"]),
            f"last_backup_status: {payload['last_backup_status']}",
            f"recommendation: {payload['recommendation']}",
            "final_recommendation: NO LIVE",
            END,
        ])


def _incomplete_work_dirs(export_dir: Path) -> list[str]:
    if not export_dir.exists():
        return []
    rows: list[str] = []
    for pattern in ("*_work", "*.tmp", "*.download", "*.partial"):
        for path in export_dir.glob(pattern):
            rows.append(path.name)
    return sorted(rows)[:20]
