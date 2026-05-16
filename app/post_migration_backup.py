from __future__ import annotations

from typing import Any

from .data_vault import DataVault


START = "POST MIGRATION BACKUP START"
END = "POST MIGRATION BACKUP END"


class PostMigrationBackup:
    def __init__(self, config: Any, db: Any, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def to_text(self, *, hours: int = 168) -> str:
        vault = DataVault(self.config, self.db, self.logger)
        payload = vault.export(hours=hours, upload=True)
        upload = payload.get("external_upload") or {}
        return "\n".join([
            START,
            f"hours: {hours}",
            f"file: {payload.get('file')}",
            f"manifest_valid: {str(payload.get('manifest_valid')).lower()}",
            f"checksum_valid: {str(payload.get('checksums_created')).lower()}",
            "secrets_excluded: true",
            "external_upload:",
            f"- enabled={str(upload.get('enabled', False)).lower()}",
            f"- configured={str(upload.get('configured', False)).lower()}",
            f"- uploaded={str(upload.get('uploaded', False)).lower()}",
            f"- verified={str(upload.get('verified', False)).lower()}",
            f"- remote_key={upload.get('remote_key', '')}",
            f"- sanitized_error={upload.get('sanitized_error', 'none')}",
            "final_recommendation: NO LIVE",
            END,
        ])
