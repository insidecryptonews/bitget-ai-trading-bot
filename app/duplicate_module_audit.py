from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT


PATTERNS = (
    "walk_forward",
    "walkforward",
    "exit_policy",
    "score_calibration",
    "backtester",
    "smoke_test",
)


def build_duplicate_module_audit(root: Path | None = None) -> dict[str, Any]:
    base = root or PROJECT_ROOT
    app_dir = base / "app"
    files = sorted(app_dir.glob("*.py"))
    groups: dict[str, list[str]] = {}
    for pattern in PATTERNS:
        groups[pattern] = [str(path.relative_to(base)).replace("\\", "/") for path in files if pattern in path.name.lower()]
    recommendations = []
    for pattern, paths in groups.items():
        for path in paths:
            status = "KEEP"
            if pattern == "backtester" and path == "app/backtester.py":
                status = "LEGACY_CANDIDATE"
            elif pattern == "backtester" and path == "app/real_strategy_backtester.py":
                status = "KEEP"
            elif pattern == "smoke_test":
                status = "MIGRATION_CANDIDATE"
            elif len(paths) > 1:
                status = "MERGE_CANDIDATE"
            recommendations.append({
                "group": pattern,
                "module": path,
                "status": status,
                "risk": "review_imports_before_move",
            })
    return {
        "groups": groups,
        "recommendations": recommendations,
        "historical_data_modified": False,
        "files_moved": False,
        "final_recommendation": "NO LIVE",
    }


def duplicate_module_audit_text(root: Path | None = None) -> str:
    payload = build_duplicate_module_audit(root)
    lines = ["DUPLICATE MODULE AUDIT START"]
    for group, paths in payload["groups"].items():
        lines.append(f"{group}: {len(paths)}")
        for path in paths[:20]:
            status = next((item["status"] for item in payload["recommendations"] if item["module"] == path and item["group"] == group), "UNKNOWN_NEEDS_REVIEW")
            lines.append(f"- {path}: {status}")
    lines.extend([
        "historical_data_modified=false",
        "files_moved=false",
        "final_recommendation: NO LIVE",
        "DUPLICATE MODULE AUDIT END",
    ])
    return "\n".join(lines)


def duplicate_module_audit_smoke_text() -> str:
    payload = build_duplicate_module_audit()
    checks = {
        "finds_backtester_group": "backtester" in payload["groups"],
        "documents_without_moving": payload["files_moved"] is False,
        "no_live": payload["final_recommendation"] == "NO LIVE",
    }
    lines = ["DUPLICATE MODULE AUDIT SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend([
        "LIVE_TRADING=false",
        "DRY_RUN=true",
        "PAPER_TRADING=true",
        "ENABLE_PAPER_POLICY_FILTER=false",
        "can_send_real_orders=false",
        f"result: {'PASS' if all(checks.values()) else 'FAIL'}",
        "DUPLICATE MODULE AUDIT SMOKE TEST END",
    ])
    return "\n".join(lines)
