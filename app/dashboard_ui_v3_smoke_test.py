from __future__ import annotations

import re
from typing import Any

from .config import PROJECT_ROOT
from .dashboard_pro import DashboardProReporter, sanitize_text_for_dashboard


FORBIDDEN_BUTTON_TEXT = (
    "Activar live",
    "Cambiar slots",
    "Cambiar leverage",
    "Cambiar margin",
    "Borrar DB",
    "Restore backup",
    "Abrir orden",
    "Cerrar orden",
)


class _FakeDb:
    sqlite_path = PROJECT_ROOT / "bot_state.db"

    def get_open_paper_positions_summary(self, limit: int = 5) -> list[dict[str, Any]]:
        return []

    def get_paper_trade_summary(self) -> dict[str, int]:
        return {"total": 0, "open": 0, "closed": 0}

    def fetch_table_rows(self, *args, **kwargs) -> list[dict[str, Any]]:
        return []

    def fetch_labeled_signal_rows_since(self, *args, **kwargs) -> list[dict[str, Any]]:
        return []

    def fetch_latency_metrics_since(self, *args, **kwargs) -> list[dict[str, Any]]:
        return []


class DashboardUiV3SmokeTest:
    def __init__(self, config: Any, db: Any, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def to_text(self) -> str:
        del self.db, self.logger
        static_dir = PROJECT_ROOT / "app" / "static"
        html = (static_dir / "dashboard.html").read_text(encoding="utf-8")
        css = (static_dir / "dashboard.css").read_text(encoding="utf-8")
        js = (static_dir / "dashboard.js").read_text(encoding="utf-8")
        short_report = DashboardProReporter(self.config, _FakeDb()).build_short(hours=24)
        full_report = DashboardProReporter(self.config, _FakeDb()).build(hours=24)
        combined = f"{html}\n{css}\n{js}"
        side_nav = re.search(r'<nav class="side-nav">(.*?)</nav>', html, re.S)
        mobile_nav = re.search(r'<nav class="mobile-nav"[^>]*>(.*?)</nav>', html, re.S)
        nav_markup = (side_nav.group(1) if side_nav else "") + (mobile_nav.group(1) if mobile_nav else "")
        reports_section = re.search(r'<section id="reports" class="section">(.*?)</section>', html, re.S)
        navigation_js = js.split("function bindSafeNavigation", 1)[-1].split("function bindActions", 1)[0]
        forbidden_buttons_ok = not any(text in html for text in FORBIDDEN_BUTTON_TEXT)
        nav_no_downloads = all(
            token not in nav_markup
            for token in (
                "download",
                "href=",
                "/api/training/export",
                "/api/training/full-report",
                "/api/training/short-report",
                ".csv",
                ".txt",
                ".json",
            )
        )
        nav_handler_safe = (
            "preventDefault()" in navigation_js
            and "scrollIntoView" in navigation_js
            and "fetchText" not in navigation_js
            and "download(" not in navigation_js
            and "window.location.href" not in navigation_js
            and "Blob" not in navigation_js
            and "localStorage" not in navigation_js
        )
        exports_only_in_reports = bool(reports_section and "export-btn" in reports_section.group(1) and "export-btn" not in nav_markup)
        no_refresh_dangerous = all(
            item not in js.split("const MAIN_ANALYSIS_STEPS", 1)[-1].split("];", 1)[0]
            for item in ("data-export", "data-restore", "post-migration-backup", "LIVE_TRADING=true")
        )
        secrets_probe = sanitize_text_for_dashboard("DASHBOARD_AUTH_TOKEN=abc API_KEY=secret DATA_VAULT_S3_SECRET_ACCESS_KEY=r2")
        secrets_ok = "abc" not in secrets_probe and "secret" not in secrets_probe and "r2" not in secrets_probe
        checks = {
            "contains_control_title": "Bitget AI Research Control" in html,
            "contains_training_dashboard_pro": "Training Dashboard Pro" in html,
            "contains_sidebar_nav": "sidebar" in html and "side-nav" in html,
            "contains_overview_hero": "hero-card" in html and "PAPER ONLY. NO LIVE. Keep research." in html,
            "contains_kpi_cards": "overviewKpis" in html and "kpi-card" in css,
            "contains_chart_containers": all(token in html for token in ("outcomeStackedChart", "signalBarChart", "candidateStatusChart", "exitCalibrationChart", "preMoveChart", "latencyChart")),
            "contains_reports_exports_separate": 'id="reports"' in html and "Reports & Exports" in html,
            "contains_score_incubator": 'id="score-incubator"' in html and "Score & Incubator" in html,
            "contains_training_integrity_audit": "trainingDataIntegrityBtn" in html and "training-data-integrity" in js,
            "contains_worker_health_audit": "workerHealthAuditBtn" in html and "worker-health-audit" in js,
            "contains_dashboard_binding_audit": "dashboardDataBindingAuditBtn" in html and "dashboard-data-binding-audit" in js,
            "overview_false_zero_guard": "labelsReady" in js and "pendingText" in js,
            "contains_exit_calibration_v2": "Exit Label Calibration V2" in html,
            "contains_edge_policy": "Edge & Policy" in html,
            "contains_time_death": "Time Death" in html,
            "contains_utc_madrid": "UTC:" in html and "Madrid:" in html,
            "contains_no_live": "NO LIVE" in html,
            "contains_paper_only": "PAPER ONLY" in html,
            "sidebar_navigation_only": nav_no_downloads,
            "sidebar_navigation_handler_safe": nav_handler_safe,
            "downloads_only_in_reports": exports_only_in_reports,
            "no_obvious_secret_literals": all(secret not in combined for secret in ("BITGET_API_KEY=", "DASHBOARD_AUTH_TOKEN=", "DATA_VAULT_S3_SECRET_ACCESS_KEY=")),
            "no_dangerous_buttons": forbidden_buttons_ok,
            "refresh_main_no_backup_restore_live": no_refresh_dangerous,
            "short_report_ok": "DASHBOARD PRO SHORT REPORT START" in str(short_report.get("text") or ""),
            "full_report_ok": "DASHBOARD PRO FULL REPORT START" in str(full_report.get("text") or ""),
            "secrets_sanitized": secrets_ok,
            "LIVE_TRADING_false": not bool(getattr(self.config, "live_trading", False)),
            "DRY_RUN_true": bool(getattr(self.config, "dry_run", True)),
            "PAPER_TRADING_true": bool(getattr(self.config, "paper_trading", True)),
        }
        result = "PASS" if all(checks.values()) else "FAIL"
        lines = ["DASHBOARD UI V3 SMOKE TEST START"]
        lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
        lines.extend(
            [
                "opened_real_trades: 0",
                "opened_paper_trades_from_smoke: 0",
                "slots_changed=false",
                "final_recommendation: NO LIVE",
                f"result: {result}",
                "DASHBOARD UI V3 SMOKE TEST END",
            ]
        )
        return "\n".join(lines)
