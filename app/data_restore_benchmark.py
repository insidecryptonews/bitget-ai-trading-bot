from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from .data_vault import DataVault
from .edge_hardening_utils import FINAL_NO_LIVE
from .utils import safe_float


START = "DATA RESTORE BENCHMARK START"
END = "DATA RESTORE BENCHMARK END"


class DataRestoreBenchmark:
    def __init__(self, config: Any, db: Any, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def build(self, *, dry_run: bool = True) -> dict[str, Any]:
        vault = DataVault(self.config, self.db, self.logger)
        status = vault.status()
        latest = status.get("latest_local_backup") or ""
        path = Path(str(latest)) if latest else None
        zip_size = path.stat().st_size if path and path.exists() else 0
        table_sizes = _zip_table_sizes(path) if path and path.exists() else []
        estimated_rows = sum(row.get("estimated_rows", 0) for row in table_sizes)
        estimated_minutes = max(1.0, estimated_rows / 100000.0 * 12.0) if estimated_rows else 0.0
        return {
            "mode": "dry-run" if dry_run else "apply-not-supported-here",
            "latest_backup": latest,
            "zip_size_mb": zip_size / (1024 * 1024),
            "expected_db_size_mb": (zip_size / (1024 * 1024)) * 20.0 if zip_size else 0.0,
            "large_tables": table_sizes[:10],
            "estimated_restore_minutes": estimated_minutes,
            "recommendations": [
                "use transaction batches",
                "use WAL or safe SQLite pragmas during import",
                "defer optional indexes only if importer supports rebuilding them",
                "keep checksum validation before apply",
                "do not load jsonl tables into memory",
            ],
            "db_modified": False,
            "final_recommendation": FINAL_NO_LIVE,
        }

    def to_text(self, *, dry_run: bool = True) -> str:
        payload = self.build(dry_run=dry_run)
        return "\n".join([
            START,
            f"mode: {payload['mode']}",
            f"latest_backup: {payload['latest_backup'] or 'none'}",
            f"zip_size_mb={safe_float(payload['zip_size_mb']):.1f}",
            f"expected_db_size_mb={safe_float(payload['expected_db_size_mb']):.1f}",
            f"estimated_restore_minutes={safe_float(payload['estimated_restore_minutes']):.1f}",
            "large_tables:",
            *_table_lines(payload["large_tables"]),
            "recommendations:",
            *[f"- {item}" for item in payload["recommendations"]],
            "db_modified=false",
            "final_recommendation: NO LIVE",
            END,
        ])


def _zip_table_sizes(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if not info.filename.startswith("tables/") or not info.filename.endswith(".jsonl.gz"):
                    continue
                rows.append({
                    "name": info.filename,
                    "compressed_mb": info.compress_size / (1024 * 1024),
                    "uncompressed_mb": info.file_size / (1024 * 1024),
                    "estimated_rows": max(0, int(info.file_size / 350)),
                })
    except Exception:
        return []
    rows.sort(key=lambda item: item["uncompressed_mb"], reverse=True)
    return rows


def _table_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        f"- {row.get('name')} compressed_mb={safe_float(row.get('compressed_mb')):.1f} uncompressed_mb={safe_float(row.get('uncompressed_mb')):.1f} estimated_rows={row.get('estimated_rows')}"
        for row in rows
    ]
