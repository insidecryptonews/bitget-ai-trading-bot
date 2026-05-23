"""Data Vault Cleanup Audit — READ-ONLY inspection of incomplete_work_dirs.

The Data Vault export occasionally leaves `training_vault_YYYYMMDD_HHMMSS_work`
directories behind when the export aborts mid-flight. This module audits them:
  - age,
  - size,
  - whether a complete + verified vault exists AFTER the incomplete one.

It NEVER deletes anything. It returns a list with a `safe_to_delete` boolean
per entry that the operator can act on manually with confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import iso_utc


FINAL_RECOMMENDATION = "NO LIVE"


@dataclass
class WorkDirEntry:
    name: str
    path: str
    age_hours: float
    size_mb: float
    superseded_by: str = ""
    safe_to_delete: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CleanupReport:
    generated_at: str
    export_dir: str
    incomplete_work_dirs: list[WorkDirEntry] = field(default_factory=list)
    complete_backups_count: int = 0
    latest_complete_backup: str = ""
    total_incomplete_size_mb: float = 0.0
    delete_command_template: str = ""
    notes: list[str] = field(default_factory=list)
    final_recommendation: str = FINAL_RECOMMENDATION
    research_only: bool = True
    can_delete: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class DataVaultCleanupAudit:
    SAFE_AGE_HOURS = 48.0   # work dirs older than 48h with a newer complete backup → safe to delete
    SAFE_MIN_NEWER_BACKUPS = 1

    def __init__(self, config: Any, logger: Any | None = None) -> None:
        self.config = config
        self.logger = logger

    def build(self) -> CleanupReport:
        export_dir = Path(getattr(self.config, "training_vault_dir", "training_exports") or "training_exports")
        if not export_dir.is_absolute():
            export_dir = Path.cwd() / export_dir
        if not export_dir.exists():
            return CleanupReport(
                generated_at=iso_utc(),
                export_dir=str(export_dir),
                notes=["export_dir_not_found"],
            )
        now = datetime.now(timezone.utc)
        work_dirs = sorted(p for p in export_dir.iterdir() if p.is_dir() and p.name.endswith("_work"))
        complete_backups = sorted(p for p in export_dir.iterdir() if p.is_file() and p.suffix == ".zip" and "training_vault" in p.name)
        latest_complete = complete_backups[-1].name if complete_backups else ""

        entries: list[WorkDirEntry] = []
        for path in work_dirs:
            stat = self._safe_stat(path)
            mtime = datetime.fromtimestamp(stat.get("mtime", 0), tz=timezone.utc) if stat else None
            age_hours = ((now - mtime).total_seconds() / 3600.0) if mtime else 0.0
            size_mb = self._dir_size_mb(path)
            superseded_by, newer_count = self._has_newer_complete(path.name, complete_backups)
            safe = bool(age_hours >= self.SAFE_AGE_HOURS and newer_count >= self.SAFE_MIN_NEWER_BACKUPS)
            reason_parts = []
            if age_hours < self.SAFE_AGE_HOURS:
                reason_parts.append(f"too_recent_age_h={age_hours:.1f}")
            if newer_count < self.SAFE_MIN_NEWER_BACKUPS:
                reason_parts.append("no_newer_complete_backup")
            if safe:
                reason_parts.append(f"superseded_by={superseded_by}")
            entries.append(WorkDirEntry(
                name=path.name,
                path=str(path),
                age_hours=age_hours,
                size_mb=size_mb,
                superseded_by=superseded_by,
                safe_to_delete=safe,
                reason=";".join(reason_parts) if reason_parts else "no_action",
            ))

        notes: list[str] = ["READ_ONLY_AUDIT_NO_DELETE_INVOKED"]
        if entries:
            notes.append("review_each_safe_to_delete_entry_manually_before_rm")

        return CleanupReport(
            generated_at=iso_utc(),
            export_dir=str(export_dir),
            incomplete_work_dirs=entries,
            complete_backups_count=len(complete_backups),
            latest_complete_backup=latest_complete,
            total_incomplete_size_mb=sum(e.size_mb for e in entries),
            delete_command_template="rm -rf <path>     # only after manual review; this script never executes it",
            notes=notes,
        )

    @staticmethod
    def _safe_stat(path: Path) -> dict[str, Any]:
        try:
            stat = path.stat()
            return {"mtime": stat.st_mtime}
        except OSError:
            return {}

    @staticmethod
    def _dir_size_mb(path: Path) -> float:
        total = 0
        try:
            for child in path.rglob("*"):
                if child.is_file():
                    total += child.stat().st_size
        except OSError:
            pass
        return total / (1024 * 1024)

    @staticmethod
    def _has_newer_complete(work_name: str, complete_backups: list[Path]) -> tuple[str, int]:
        """Find a complete backup with timestamp AFTER the work dir's timestamp."""
        # work dir: training_vault_YYYYMMDD_HHMMSS_work
        try:
            stem = work_name.rsplit("_work", 1)[0]
            # stem: training_vault_YYYYMMDD_HHMMSS
        except Exception:
            return "", 0
        newer = [p for p in complete_backups if p.stem > stem]
        if not newer:
            return "", 0
        return newer[0].name, len(newer)


def render_report_text(report: CleanupReport) -> str:
    lines = [
        "DATA VAULT CLEANUP AUDIT START",
        f"generated_at: {report.generated_at}",
        f"export_dir: {report.export_dir}",
        f"complete_backups_count: {report.complete_backups_count}",
        f"latest_complete_backup: {report.latest_complete_backup or 'none'}",
        f"incomplete_work_dirs_count: {len(report.incomplete_work_dirs)}",
        f"total_incomplete_size_mb: {report.total_incomplete_size_mb:.2f}",
        "incomplete_work_dirs:",
    ]
    for entry in report.incomplete_work_dirs:
        lines.append(
            f"- name={entry.name} age_h={entry.age_hours:.1f} size_mb={entry.size_mb:.2f} "
            f"safe_to_delete={str(entry.safe_to_delete).lower()} reason={entry.reason}"
        )
    lines.extend([
        f"delete_command_template: {report.delete_command_template}",
        "can_delete: false",
        "research_only: true",
        f"final_recommendation: {report.final_recommendation}",
        "DATA VAULT CLEANUP AUDIT END",
    ])
    return "\n".join(lines)
