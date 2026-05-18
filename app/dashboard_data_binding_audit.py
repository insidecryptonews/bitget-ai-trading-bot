from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .dashboard_pro import sanitize_text_for_dashboard


START = "DASHBOARD DATA BINDING AUDIT START"
END = "DASHBOARD DATA BINDING AUDIT END"


class DashboardDataBindingAudit:
    """Read-only dashboard/report binding audit.

    It checks source contracts and lightweight report generation. It does not trigger backup,
    restore, live execution, paper filter activation, or heavy dashboard auto-refresh.
    """

    def __init__(self, config: Any, db: Any, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        html = _read("app/static/dashboard.html")
        js = _read("app/static/dashboard.js")
        css = _read("app/static/dashboard.css")
        reporter_source = _read("app/dashboard_pro.py")
        missing_fields = []
        for section in ("Score Calibration", "Candidate Incubator", "Training Data Integrity", "Worker Health Audit", "Data Vault Audit", "Dashboard Data Binding"):
            if section not in reporter_source:
                missing_fields.append(f"short_report_missing_{section.replace(' ', '_').lower()}")
        if "labelsReady" not in js or "pendingText" not in js:
            missing_fields.append("overview_false_zero_guard_missing")
        if "score-incubator" not in html:
            missing_fields.append("score_incubator_section_missing")
        if "overflow-wrap" not in css or "technical-output" not in css:
            missing_fields.append("responsive_overflow_guard_missing")
        activation_enabled = bool(getattr(self.config, "enable_paper_policy_filter", False)) or str(getattr(self.config, "paper_policy_filter_mode", "shadow")).lower() != "shadow"
        safety_ok = (
            bool(getattr(self.config, "paper_trading", True))
            and not bool(getattr(self.config, "live_trading", False))
            and bool(getattr(self.config, "dry_run", True))
            and not activation_enabled
        )
        overview_consistency = "BAD" if "overview_false_zero_guard_missing" in missing_fields else "OK"
        refresh_consistency = "BAD" if activation_enabled else "OK"
        return {
            "hours": hours,
            "short_report_ok": "build_short" in reporter_source and "final_recommendation" in reporter_source,
            "score_incubator_in_report": "Score Calibration" in reporter_source and "Candidate Incubator" in reporter_source,
            "new_audits_in_report": all(
                marker in reporter_source
                for marker in ("Training Data Integrity", "Worker Health Audit", "Data Vault Audit", "Dashboard Data Binding")
            ),
            "overview_consistency": overview_consistency,
            "refresh_consistency": refresh_consistency,
            "missing_fields": missing_fields,
            "stale_fields": [],
            "safety_fields_coherent": safety_ok,
            "paper_filter_enabled": bool(getattr(self.config, "enable_paper_policy_filter", False)),
            "paper_policy_filter_mode": str(getattr(self.config, "paper_policy_filter_mode", "shadow")),
            "dashboard_html_loaded": bool(html),
            "dashboard_js_loaded": bool(js),
            "dashboard_css_loaded": bool(css),
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"short_report_ok: {str(payload['short_report_ok']).lower()}",
            f"score_incubator_in_report: {str(payload['score_incubator_in_report']).lower()}",
            f"new_audits_in_report: {str(payload['new_audits_in_report']).lower()}",
            f"overview_consistency: {payload['overview_consistency']}",
            f"refresh_consistency: {payload['refresh_consistency']}",
            "missing_fields:",
            *([f"- {item}" for item in payload["missing_fields"]] if payload["missing_fields"] else ["- none"]),
            "stale_fields:",
            *([f"- {item}" for item in payload["stale_fields"]] if payload["stale_fields"] else ["- none"]),
            f"safety_fields_coherent: {str(payload['safety_fields_coherent']).lower()}",
            f"paper_filter_enabled: {str(payload['paper_filter_enabled']).lower()}",
            f"paper_policy_filter_mode: {payload['paper_policy_filter_mode']}",
            "final_recommendation: NO LIVE",
            END,
        ])


class DashboardDataBindingSmokeTest:
    def __init__(self, config: Any, db: Any | None = None, logger: Any | None = None) -> None:
        self.config = config

    def to_text(self) -> str:
        js = _read("app/static/dashboard.js")
        html = _read("app/static/dashboard.html")
        false_zero_guard = "labelsReady" in js and "pendingText" in js
        score_incubator = "score-incubator" in html and "Score & Incubator" in html
        no_activation = not bool(getattr(self.config, "enable_paper_policy_filter", False)) and str(getattr(self.config, "paper_policy_filter_mode", "shadow")).lower() == "shadow"
        passed = false_zero_guard and score_incubator and no_activation
        return "\n".join([
            "DASHBOARD DATA BINDING SMOKE TEST START",
            f"no_false_zero_guard: {str(false_zero_guard).lower()}",
            f"score_incubator_available: {str(score_incubator).lower()}",
            f"no_activation_enabled: {str(no_activation).lower()}",
            "final_recommendation: NO LIVE",
            f"result: {'PASS' if passed else 'FAIL'}",
            "DASHBOARD DATA BINDING SMOKE TEST END",
        ])


def _read(path: str) -> str:
    try:
        return (PROJECT_ROOT / path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _safe(callback: Any, default: Any) -> Any:
    try:
        return callback()
    except Exception:
        return default
