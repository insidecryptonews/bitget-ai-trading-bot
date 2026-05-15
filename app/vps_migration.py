from __future__ import annotations

import importlib.util
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import BotConfig, PROJECT_ROOT
from .data_vault import DataVault
from .utils import safe_float
from .worker_lock import WorkerLockManager


GUIDE_START = "VPS MIGRATION GUIDE START"
GUIDE_END = "VPS MIGRATION GUIDE END"
PREFLIGHT_START = "VPS PREFLIGHT START"
PREFLIGHT_END = "VPS PREFLIGHT END"


def build_vps_migration_guide(config: BotConfig) -> str:
    repo_hint = "git clone <TU_REPO_GITHUB> bitget-ai-trading-bot"
    return "\n".join([
        GUIDE_START,
        "target: Ubuntu limpio",
        "mode: PAPER ONLY / NO LIVE",
        "1. Sistema base:",
        "   sudo apt update && sudo apt upgrade -y",
        "   sudo apt install -y git python3 python3-venv python3-pip build-essential",
        "2. Clonar repo:",
        f"   {repo_hint}",
        "   cd bitget-ai-trading-bot",
        "3. Crear entorno Python:",
        "   python3 -m venv .venv",
        "   . .venv/bin/activate",
        "   python -m pip install --upgrade pip",
        "   python -m pip install -r requirements.txt",
        "4. Configurar variables seguras:",
        "   PAPER_TRADING=true",
        "   LIVE_TRADING=false",
        "   DRY_RUN=true",
        "   WORKER_LIGHTWEIGHT_MODE=true",
        "   REQUIRE_SINGLE_WORKER_LOCK=true",
        "   WORKER_LOCK_BACKEND=database",
        "   DATA_VAULT_EXTERNAL_ENABLED=true",
        "   DATA_VAULT_EXTERNAL_PROVIDER=s3_compatible",
        "   DATA_VAULT_EXTERNAL_BUCKET=<bucket>",
        "   DATA_VAULT_EXTERNAL_PREFIX=bitget-ai-trading-bot/training",
        "   DATA_VAULT_S3_ENDPOINT_URL=<cloudflare-r2-endpoint>",
        "   DATA_VAULT_S3_REGION=auto",
        "   DATA_VAULT_S3_ACCESS_KEY_ID=<configurar solo en entorno>",
        "   DATA_VAULT_S3_SECRET_ACCESS_KEY=<configurar solo en entorno>",
        "5. Descargar backup desde R2:",
        "   python -m app.research_lab data-download-latest",
        "6. Validar restore sin escribir:",
        "   python -m app.research_lab data-restore-latest --dry-run",
        "7. Aplicar restore cuando el dry-run pase:",
        "   python -m app.research_lab data-restore-latest --apply",
        "8. Preflight VPS:",
        "   python -m app.research_lab vps-preflight",
        "9. Arrancar worker solo en paper:",
        "   python -m app.main",
        "10. Comprobar dashboard:",
        f"   abrir http://<ip-vps>:{config.port}/dashboard",
        "11. Cuando VPS este estable, parar Railway worker para evitar doble entrenamiento.",
        "12. Confirmacion final:",
        "   LIVE_TRADING=false",
        "   DRY_RUN=true",
        "   PAPER_TRADING=true",
        "   final_recommendation: NO LIVE",
        "secrets: no se imprimen ni se guardan en esta guia",
        GUIDE_END,
    ])


@dataclass
class VpsPreflight:
    config: BotConfig
    db: Any
    logger: Any | None = None

    def build(self) -> dict[str, Any]:
        memory = _memory_status()
        disk = shutil.disk_usage(str(PROJECT_ROOT))
        requirements = {name: _module_available(name) for name in ("sqlite3", "boto3", "dotenv")}
        vault = DataVault(self.config, self.db, self.logger)
        try:
            vault_status = vault.status()
        except Exception as exc:
            vault_status = {"error": _safe_error(exc), "remote_backup_count": 0, "latest_remote_backup": ""}
        try:
            readiness = vault.migration_readiness()
        except Exception as exc:
            readiness = {"error": _safe_error(exc), "readiness_status": "error"}
        lock_status = WorkerLockManager(self.config, self.db, self.logger).status().to_dict()
        safety = {
            "LIVE_TRADING": bool(self.config.live_trading),
            "DRY_RUN": bool(self.config.dry_run),
            "PAPER_TRADING": bool(self.config.paper_trading),
            "can_send_real_orders": bool(self.config.can_send_real_orders),
            "margin_mode": self.config.margin_mode,
        }
        dangerous = []
        if self.config.live_trading:
            dangerous.append("LIVE_TRADING=true")
        if not self.config.dry_run:
            dangerous.append("DRY_RUN=false")
        if not self.config.paper_trading:
            dangerous.append("PAPER_TRADING=false")
        if self.config.can_send_real_orders:
            dangerous.append("can_send_real_orders=true")
        db_ok = _db_ok(self.db)
        dashboard_ok = bool(self.config.enable_training_dashboard and "error" not in readiness)
        blocked = bool(dangerous or not db_ok or not dashboard_ok)
        return {
            "python_version": sys.version.split()[0],
            "system": platform.platform(),
            "memory_free_mb": memory["free_mb"],
            "memory_total_mb": memory["total_mb"],
            "disk_free_mb": round(disk.free / (1024 * 1024), 2),
            "disk_total_mb": round(disk.total / (1024 * 1024), 2),
            "requirements": requirements,
            "repo_ok": (PROJECT_ROOT / "app").exists(),
            "db_ok": db_ok,
            "r2_configured": bool(vault_status.get("external_configured")),
            "r2_remote_backup_count": int(vault_status.get("remote_backup_count") or 0),
            "latest_remote_backup": vault_status.get("latest_remote_backup", ""),
            "migration_readiness": readiness.get("readiness_status", "unknown"),
            "dashboard_ok": dashboard_ok,
            "research_commands_available": [
                "data-download-latest",
                "data-restore-latest",
                "migration-readiness",
                "fast-runtime-plan",
                "paper-policy-orchestrator",
            ],
            "worker_lock": lock_status,
            "runtime_profile": self.config.training_runtime_profile,
            "vps_research_profile_enabled": bool(self.config.vps_research_profile_enabled),
            "safety": safety,
            "dangerous": dangerous,
            "status": "VPS_PREFLIGHT_BLOCKED" if blocked else "VPS_PREFLIGHT_OK",
            "final_recommendation": "NO LIVE",
        }

    def to_text(self) -> str:
        payload = self.build()
        return "\n".join([
            PREFLIGHT_START,
            f"python_version: {payload['python_version']}",
            f"system: {payload['system']}",
            f"memory_free_mb: {payload['memory_free_mb']}",
            f"memory_total_mb: {payload['memory_total_mb']}",
            f"disk_free_mb: {payload['disk_free_mb']}",
            f"disk_total_mb: {payload['disk_total_mb']}",
            f"repo_ok: {str(payload['repo_ok']).lower()}",
            f"db_ok: {str(payload['db_ok']).lower()}",
            f"r2_configured: {str(payload['r2_configured']).lower()}",
            f"r2_remote_backup_count: {payload['r2_remote_backup_count']}",
            f"latest_remote_backup: {payload['latest_remote_backup'] or 'none'}",
            f"migration_readiness: {payload['migration_readiness']}",
            f"dashboard_ok: {str(payload['dashboard_ok']).lower()}",
            f"research_commands_available: {','.join(payload['research_commands_available'])}",
            f"current_instance_id: {payload['worker_lock'].get('current_instance_id', '')}",
            f"worker_lock_status: {payload['worker_lock'].get('lock_status', '')}",
            f"active_worker_instance: {payload['worker_lock'].get('active_worker_instance', '')}",
            f"training_runtime_profile: {payload['runtime_profile']}",
            f"vps_research_profile_enabled: {str(payload['vps_research_profile_enabled']).lower()}",
            "safety:",
            f"- LIVE_TRADING={str(payload['safety']['LIVE_TRADING']).lower()}",
            f"- DRY_RUN={str(payload['safety']['DRY_RUN']).lower()}",
            f"- PAPER_TRADING={str(payload['safety']['PAPER_TRADING']).lower()}",
            f"- can_send_real_orders={str(payload['safety']['can_send_real_orders']).lower()}",
            "dangerous:",
            *([f"- {item}" for item in payload["dangerous"]] or ["- none"]),
            f"result: {payload['status']}",
            "final_recommendation: NO LIVE",
            PREFLIGHT_END,
        ])


def _db_ok(db: Any) -> bool:
    try:
        counts = db.get_table_counts()
        return isinstance(counts, dict)
    except Exception:
        return False


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return name in sys.modules


def _memory_status() -> dict[str, float]:
    if platform.system().lower() == "linux":
        try:
            info = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, raw = line.split(":", 1)
                info[key] = safe_float(raw.strip().split()[0]) / 1024.0
            return {"free_mb": round(info.get("MemAvailable", 0.0), 2), "total_mb": round(info.get("MemTotal", 0.0), 2)}
        except Exception:
            pass
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return {"free_mb": round(status.ullAvailPhys / (1024 * 1024), 2), "total_mb": round(status.ullTotalPhys / (1024 * 1024), 2)}
    except Exception:
        return {"free_mb": 0.0, "total_mb": 0.0}


def _safe_error(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:300]
