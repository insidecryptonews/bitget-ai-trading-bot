from pathlib import Path
import re

from app.config import BotConfig, PROJECT_ROOT
from app.dashboard_ui_v3_smoke_test import DashboardUiV3SmokeTest


STATIC = PROJECT_ROOT / "app" / "static"


def test_dashboard_ui_v3_structure_is_real_app_shell():
    html = (STATIC / "dashboard.html").read_text(encoding="utf-8")
    css = (STATIC / "dashboard.css").read_text(encoding="utf-8")
    js = (STATIC / "dashboard.js").read_text(encoding="utf-8")

    assert "Bitget AI Research Control" in html
    assert "Training Dashboard Pro" in html
    assert "sidebar" in html
    assert "side-nav" in html
    assert "hero-card" in html
    assert "overviewKpis" in html
    assert "outcomeStackedChart" in html
    assert "signalBarChart" in html
    assert "Reports & Exports" in html
    assert "Score & Incubator" in html
    assert "Data Pipeline & Cost Diagnostics" in html
    assert "Core Corrections / Fase 5 Final" in html
    assert "Pipeline & Costs" in html
    assert "Bitget fees" in html
    assert "scoreMonotonicityChart" in html
    assert "pipelineDuplicateChart" in html
    assert "costSensitivityChart" in html
    assert "candidateIncubatorBtn" in html
    assert "Exit Label Calibration V2" in html
    assert "Time Death" in html
    assert "UTC:" in html
    assert "Madrid:" in html
    assert "--bg" in css
    assert "--panel-hover" in css
    assert "renderKpiCard" in js
    assert "renderReadinessGrid" in js
    assert "renderStackedBar" in js
    assert "renderHorizontalBarChart" in js
    assert "handleScoreCalibration" in js
    assert "handleCandidateIncubator" in js
    assert "labelsReady" in js
    assert "pendingText" in js
    assert "trainingDataIntegrityBtn" in html
    assert "dataPipelineDiagnosisBtn" in html
    assert "labelQualityV2Btn" in html
    assert "bitgetCostModelAuditBtn" in html
    assert "marginModeAuditBtn" in html
    assert "coreCorrectionsBtn" in html
    assert "handleCoreCorrections" in js
    assert "Execution Safety / Pre-Live Hardening" in html
    assert "executionSafetyAuditBtn" in html
    assert "netRrAuditBtn" in html
    assert "handleExecutionSafety" in js
    assert "handleNetRrAudit" in js
    assert "workerHealthAuditBtn" in html
    assert "dashboardDataBindingAuditBtn" in html
    assert "INVALID_METRICS_BLOCKED" in html
    assert "realStrategyBacktesterBtn" in html
    assert "duplicateModuleAuditBtn" in html
    assert "MFE/MAE no cuentan como retorno realizado" in html


def test_dashboard_ui_v3_has_no_dangerous_primary_actions():
    html = (STATIC / "dashboard.html").read_text(encoding="utf-8")
    forbidden = [
        "Activar live",
        "Cambiar slots",
        "Cambiar leverage",
        "Cambiar margin",
        "Borrar DB",
        "Abrir orden",
        "Cerrar orden",
    ]
    for text in forbidden:
        assert text not in html
    assert "ENABLE_PAPER_POLICY_FILTER=true" not in html
    assert "LIVE_TRADING=true" not in html


def test_sidebar_navigation_is_navigation_only():
    html = (STATIC / "dashboard.html").read_text(encoding="utf-8")
    js = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    side_nav = re.search(r'<nav class="side-nav">(.*?)</nav>', html, re.S)
    mobile_nav = re.search(r'<nav class="mobile-nav"[^>]*>(.*?)</nav>', html, re.S)
    assert side_nav
    assert mobile_nav
    nav_markup = side_nav.group(1) + mobile_nav.group(1)

    assert "<button" in nav_markup
    assert "data-target=" in nav_markup
    assert "download" not in nav_markup
    assert "href=" not in nav_markup
    assert "/api/training/export" not in nav_markup
    assert "/api/training/full-report" not in nav_markup
    assert "/api/training/short-report" not in nav_markup
    assert ".csv" not in nav_markup
    assert ".txt" not in nav_markup
    assert ".json" not in nav_markup

    navigation_js = js.split("function bindSafeNavigation", 1)[1].split("function bindActions", 1)[0]
    assert "preventDefault()" in navigation_js
    assert "scrollIntoView" in navigation_js
    assert "fetchText" not in navigation_js
    assert "download(" not in navigation_js
    assert "window.location.href" not in navigation_js
    assert "Blob" not in navigation_js
    assert "localStorage" not in navigation_js


def test_exports_exist_only_in_reports_section_not_sidebar():
    html = (STATIC / "dashboard.html").read_text(encoding="utf-8")
    reports = re.search(r'<section id="reports" class="section">(.*?)</section>', html, re.S)
    side_nav = re.search(r'<nav class="side-nav">(.*?)</nav>', html, re.S)
    assert reports
    assert side_nav

    for token in ("downloadFullTxtBtn", "downloadFullJsonBtn", "downloadSignalsCsvBtn", "downloadCandidatesCsvBtn"):
        assert token in reports.group(1)
        assert token not in side_nav.group(1)
    assert "export-btn" in reports.group(1)
    assert "export-btn" not in side_nav.group(1)


def test_dashboard_main_refresh_does_not_call_backup_restore_or_live():
    js = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    main_steps = js.split("const MAIN_ANALYSIS_STEPS", 1)[1].split("];", 1)[0]

    assert "data-export" not in main_steps
    assert "data-restore" not in main_steps
    assert "post-migration-backup" not in main_steps
    assert "LIVE_TRADING=true" not in main_steps
    assert "training-data-integrity" in main_steps
    assert "data-pipeline-diagnosis" in main_steps
    assert "core-corrections" in main_steps
    assert "label-quality-v2" in main_steps
    assert "bitget-cost-model-audit" in main_steps
    assert "margin-mode-audit" in main_steps
    assert "execution-safety-audit" in main_steps
    assert "worker-health-audit" in main_steps


def test_dashboard_ui_v3_smoke_test_passes():
    text = DashboardUiV3SmokeTest(BotConfig(), None).to_text()

    assert "DASHBOARD UI V3 SMOKE TEST START" in text
    assert "contains_sidebar_nav: true" in text
    assert "contains_chart_containers: true" in text
    assert "refresh_main_no_backup_restore_live: true" in text
    assert "final_recommendation: NO LIVE" in text
    assert "result: PASS" in text
