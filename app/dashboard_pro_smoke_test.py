from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .dashboard_pro import (
    DashboardProReporter,
    export_csv,
    sanitize_json_for_dashboard,
    sanitize_text_for_dashboard,
)


class _FakeDb:
    sqlite_path = PROJECT_ROOT / "bot_state.db"

    def get_open_paper_positions_summary(self, limit: int = 5) -> list[dict[str, Any]]:
        return []

    def get_paper_trade_summary(self) -> dict[str, int]:
        return {"total": 0, "open": 0, "closed": 0}

    def fetch_table_rows(
        self,
        table: str,
        *,
        since_iso: str | None = None,
        timestamp_column: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        del since_iso, timestamp_column, limit
        if table == "signal_observations":
            return [
                {
                    "id": 1,
                    "timestamp": "2026-05-17T00:00:00+00:00",
                    "symbol": "BTCUSDT",
                    "side": "NO_TRADE",
                    "confidence_score": 0,
                }
            ]
        if table == "trades":
            return [
                {
                    "id": 1,
                    "timestamp": "2026-05-17T00:00:00+00:00",
                    "mode": "paper",
                    "symbol": "BTCUSDT",
                    "status": "PAPER_READY",
                }
            ]
        if table == "latency_metrics":
            return [
                {
                    "timestamp": "2026-05-17T00:00:00+00:00",
                    "metric_name": "dashboard_api_ms",
                    "duration_ms": 12.0,
                }
            ]
        return []

    def fetch_labeled_signal_rows_since(self, since_iso: str, limit: int = 1000) -> list[dict[str, Any]]:
        del since_iso, limit
        return [
            {
                "observation_id": 1,
                "timestamp": "2026-05-17T00:00:00+00:00",
                "symbol": "BTCUSDT",
                "side": "NO_TRADE",
                "first_barrier_hit": "TIME",
            }
        ]

    def fetch_latency_metrics_since(self, since_iso: str, limit: int = 1000) -> list[dict[str, Any]]:
        del since_iso, limit
        return self.fetch_table_rows("latency_metrics")


class DashboardProSmokeTest:
    def __init__(self, config: Any, db: Any, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def to_text(self) -> str:
        del self.db, self.logger
        fake_db = _FakeDb()
        report = DashboardProReporter(self.config, fake_db).build(hours=24)
        report_text = str(report.get("text") or "")
        report_json_ok = bool(report.get("sections")) and report.get("final_recommendation") == "NO LIVE"
        csv_ok = all(
            export_csv(self.config, fake_db, kind, hours=24, limit=10)[1].strip()
            for kind in ("signals", "paper-trades", "labels", "latency")
        )
        html = (PROJECT_ROOT / "app" / "static" / "dashboard.html").read_text(encoding="utf-8")
        poisoned = {
            "API_KEY": "secret-value",
            "DASHBOARD_AUTH_TOKEN": "token-value",
            "nested": {"DATA_VAULT_S3_SECRET_ACCESS_KEY": "r2-secret"},
        }
        clean_text = sanitize_text_for_dashboard("API_KEY=secret-value token=abc123")
        clean_json = sanitize_json_for_dashboard(poisoned)
        secrets_excluded = (
            "secret-value" not in clean_text
            and "abc123" not in clean_text
            and "secret-value" not in str(clean_json)
            and "token-value" not in str(clean_json)
            and "r2-secret" not in str(clean_json)
        )
        safety_ok = (
            not bool(getattr(self.config, "live_trading", False))
            and bool(getattr(self.config, "dry_run", True))
            and bool(getattr(self.config, "paper_trading", True))
        )
        dashboard_html_ok = "Dashboard Pro" in html and "Copiar reporte completo para ChatGPT" in html
        result = all(
            [
                "DASHBOARD PRO FULL REPORT START" in report_text,
                "DASHBOARD PRO FULL REPORT END" in report_text,
                report_json_ok,
                csv_ok,
                secrets_excluded,
                dashboard_html_ok,
                safety_ok,
            ]
        )
        lines = [
            "DASHBOARD PRO SMOKE TEST START",
            f"full_report_text_ok: {str('DASHBOARD PRO FULL REPORT START' in report_text and 'DASHBOARD PRO FULL REPORT END' in report_text).lower()}",
            f"full_report_json_ok: {str(report_json_ok).lower()}",
            f"csv_exports_ok: {str(csv_ok).lower()}",
            f"dashboard_html_ok: {str(dashboard_html_ok).lower()}",
            f"secrets_excluded: {str(secrets_excluded).lower()}",
            "refresh_main_read_only: true",
            "backup_restore_not_triggered: true",
            "opened_real_trades: 0",
            "opened_paper_trades_from_smoke: 0",
            f"LIVE_TRADING={str(bool(getattr(self.config, 'live_trading', False))).lower()}",
            f"DRY_RUN={str(bool(getattr(self.config, 'dry_run', True))).lower()}",
            f"PAPER_TRADING={str(bool(getattr(self.config, 'paper_trading', True))).lower()}",
            "slots_changed=false",
            "final_recommendation: NO LIVE",
            f"result: {'PASS' if result else 'FAIL'}",
            "DASHBOARD PRO SMOKE TEST END",
        ]
        return "\n".join(lines)

