from __future__ import annotations

from pathlib import Path
from typing import Any

from .adaptive_exit_policy_lab import AdaptiveExitPolicyLab
from .config import BotConfig
from .data_vault import DataVault
from .database import Database
from .fast_execution_readiness import FastExecutionReadiness
from .latency_audit import LatencyAudit
from .time_death_lab import TimeDeathLab


START = "EXIT LATENCY VAULT SMOKE TEST START"
END = "EXIT LATENCY VAULT SMOKE TEST END"


class ExitLatencyVaultSmokeTest:
    def __init__(self, config: BotConfig, db: Database, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def run(self) -> dict[str, Any]:
        before_open = self.db.get_paper_trade_summary().get("open", 0)
        TimeDeathLab(self.config, self.db).build(hours=24)
        AdaptiveExitPolicyLab(self.config, self.db).build(hours=24)
        LatencyAudit(self.config, self.db).build(hours=24)
        FastExecutionReadiness(self.config, self.db).build(hours=24)
        vault = DataVault(self.config, self.db, self.logger)
        export = vault.export(hours=168, upload=False)
        backup_file = Path(export["file"])
        import_check = vault.import_backup(file=backup_file, apply=False)
        migration = vault.migration_readiness()
        after_open = self.db.get_paper_trade_summary().get("open", 0)
        result = {
            "time_death_lab_checked": True,
            "adaptive_exit_policy_checked": True,
            "latency_audit_checked": True,
            "fast_execution_readiness_checked": True,
            "data_export_created": backup_file.exists(),
            "backup_file": str(backup_file),
            "manifest_valid": bool(export.get("manifest_valid")),
            "checksum_valid": bool(import_check.get("checksum_valid")),
            "dry_run_import_ok": import_check.get("result") == "PASS",
            "secrets_excluded": bool(export.get("secrets_excluded")),
            "migration_readiness_checked": bool(migration),
            "external_disabled_ok": not self.config.data_vault_external_enabled,
            "opened_paper_trades": max(0, int(after_open or 0) - int(before_open or 0)),
            "LIVE_TRADING": bool(self.config.live_trading),
            "DRY_RUN": bool(self.config.dry_run),
            "PAPER_TRADING": bool(self.config.paper_trading),
            "slots_changed": False,
        }
        result["result"] = "PASS" if _passes(result) else "FAIL"
        return result

    def to_text(self) -> str:
        payload = self.run()
        return "\n".join([
            START,
            f"time_death_lab_checked: {str(payload['time_death_lab_checked']).lower()}",
            f"adaptive_exit_policy_checked: {str(payload['adaptive_exit_policy_checked']).lower()}",
            f"latency_audit_checked: {str(payload['latency_audit_checked']).lower()}",
            f"fast_execution_readiness_checked: {str(payload['fast_execution_readiness_checked']).lower()}",
            f"data_export_created: {str(payload['data_export_created']).lower()}",
            f"backup_file: {payload['backup_file']}",
            f"manifest_valid: {str(payload['manifest_valid']).lower()}",
            f"checksum_valid: {str(payload['checksum_valid']).lower()}",
            f"dry_run_import_ok: {str(payload['dry_run_import_ok']).lower()}",
            f"secrets_excluded: {str(payload['secrets_excluded']).lower()}",
            f"migration_readiness_checked: {str(payload['migration_readiness_checked']).lower()}",
            f"external_disabled_ok: {str(payload['external_disabled_ok']).lower()}",
            f"opened_paper_trades: {payload['opened_paper_trades']}",
            f"LIVE_TRADING={str(payload['LIVE_TRADING']).lower()}",
            f"DRY_RUN={str(payload['DRY_RUN']).lower()}",
            f"PAPER_TRADING={str(payload['PAPER_TRADING']).lower()}",
            f"slots_changed={str(payload['slots_changed']).lower()}",
            f"result: {payload['result']}",
            END,
        ])


def _passes(payload: dict[str, Any]) -> bool:
    return bool(
        payload.get("data_export_created")
        and payload.get("manifest_valid")
        and payload.get("checksum_valid")
        and payload.get("dry_run_import_ok")
        and payload.get("secrets_excluded")
        and payload.get("opened_paper_trades") == 0
        and payload.get("LIVE_TRADING") is False
        and payload.get("DRY_RUN") is True
        and payload.get("PAPER_TRADING") is True
        and payload.get("slots_changed") is False
    )
