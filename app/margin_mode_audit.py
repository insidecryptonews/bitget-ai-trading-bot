from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT


START = "MARGIN MODE AUDIT START"
END = "MARGIN MODE AUDIT END"


class MarginModeAudit:
    """Read-only isolated margin audit. It never calls Bitget or mutates settings."""

    def __init__(self, config: Any, db: Any | None = None) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        del hours
        scan = _scan_margin_code()
        margin_mode = str(getattr(self.config, "margin_mode", "") or "").lower()
        force_isolated = bool(getattr(self.config, "force_isolated_margin", False))
        disallow_cross = bool(getattr(self.config, "disallow_crossed_margin", False))
        auto_margin = bool(getattr(self.config, "auto_margin", False))
        cross_detected = margin_mode in {"cross", "crossed"} or scan["dangerous_cross_literals"]
        if margin_mode == "isolated" and force_isolated and disallow_cross and not auto_margin and not scan["dangerous_cross_literals"]:
            status = "ISOLATED_CONFIRMED"
        elif cross_detected:
            status = "CROSS_DETECTED_BAD"
        else:
            status = "UNKNOWN_NEEDS_VERIFICATION"
        return {
            "margin_mode_status": status,
            "configured_margin_mode": margin_mode or "unknown",
            "isolated_required": True,
            "cross_detected": bool(cross_detected),
            "force_isolated_margin": force_isolated,
            "disallow_crossed_margin": disallow_cross,
            "auto_margin": auto_margin,
            "ensure_isolated_margin_present": scan["ensure_isolated_margin_present"],
            "order_params_checked": scan["order_params_checked"],
            "risk_manager_blocks_cross": scan["risk_manager_blocks_cross"],
            "execution_engine_blocks_cross": scan["execution_engine_blocks_cross"],
            "set_leverage_guarded": scan["set_leverage_guarded"],
            "dangerous_fallbacks": scan["dangerous_fallbacks"],
            "recommended_action": _recommended_action(status),
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"margin_mode_status: {payload['margin_mode_status']}",
            f"configured_margin_mode: {payload['configured_margin_mode']}",
            f"isolated_required: {str(payload['isolated_required']).lower()}",
            f"cross_detected: {str(payload['cross_detected']).lower()}",
            f"force_isolated_margin: {str(payload['force_isolated_margin']).lower()}",
            f"disallow_crossed_margin: {str(payload['disallow_crossed_margin']).lower()}",
            f"auto_margin: {str(payload['auto_margin']).lower()}",
            f"ensure_isolated_margin_present: {str(payload['ensure_isolated_margin_present']).lower()}",
            f"order_params_checked: {str(payload['order_params_checked']).lower()}",
            f"risk_manager_blocks_cross: {str(payload['risk_manager_blocks_cross']).lower()}",
            f"execution_engine_blocks_cross: {str(payload['execution_engine_blocks_cross']).lower()}",
            f"set_leverage_guarded: {str(payload['set_leverage_guarded']).lower()}",
            "dangerous_fallbacks:",
            *([f"- {item}" for item in payload["dangerous_fallbacks"]] if payload["dangerous_fallbacks"] else ["- none"]),
            f"recommended_action: {payload['recommended_action']}",
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)


class MarginModeAuditSmokeTest:
    def __init__(self, config: Any, db: Any | None = None, logger: Any | None = None) -> None:
        self.config = config

    def to_text(self) -> str:
        ok_payload = MarginModeAudit(self.config).build()
        bad_config = _UnsafeConfig()
        bad_payload = MarginModeAudit(bad_config).build()
        passed = (
            ok_payload["margin_mode_status"] in {"ISOLATED_CONFIRMED", "UNKNOWN_NEEDS_VERIFICATION"}
            and bad_payload["margin_mode_status"] == "CROSS_DETECTED_BAD"
            and ok_payload["ensure_isolated_margin_present"]
            and ok_payload["order_params_checked"]
            and ok_payload["final_recommendation"] == "NO LIVE"
        )
        return "\n".join([
            "MARGIN MODE AUDIT SMOKE TEST START",
            f"isolated_confirmed_or_unknown: {str(ok_payload['margin_mode_status'] in {'ISOLATED_CONFIRMED', 'UNKNOWN_NEEDS_VERIFICATION'}).lower()}",
            f"cross_detected_bad: {str(bad_payload['margin_mode_status'] == 'CROSS_DETECTED_BAD').lower()}",
            f"ensure_isolated_margin_present: {str(ok_payload['ensure_isolated_margin_present']).lower()}",
            f"order_params_checked: {str(ok_payload['order_params_checked']).lower()}",
            "LIVE_TRADING=false",
            "DRY_RUN=true",
            "PAPER_TRADING=true",
            f"result: {'PASS' if passed else 'FAIL'}",
            "final_recommendation: NO LIVE",
            "MARGIN MODE AUDIT SMOKE TEST END",
        ])


def _scan_margin_code() -> dict[str, Any]:
    files = {
        "bitget_client": PROJECT_ROOT / "app" / "bitget_client.py",
        "execution_engine": PROJECT_ROOT / "app" / "execution_engine.py",
        "risk_manager": PROJECT_ROOT / "app" / "risk_manager.py",
        "config": PROJECT_ROOT / "app" / "config.py",
    }
    text = {name: _read(path) for name, path in files.items()}
    dangerous = []
    for name, body in text.items():
        normalized = body.lower()
        if "marginmode\": \"cross" in normalized or "margin_mode = \"cross" in normalized or "margin_mode='cross" in normalized:
            dangerous.append(f"{name}: explicit cross fallback requires review")
    return {
        "ensure_isolated_margin_present": "def ensure_isolated_margin" in text["bitget_client"],
        "order_params_checked": '"marginMode": self.config.margin_mode' in text["bitget_client"] or "'marginMode': self.config.margin_mode" in text["bitget_client"],
        "risk_manager_blocks_cross": "margin_mode != \"isolated\"" in text["risk_manager"] or "margin_mode != 'isolated'" in text["risk_manager"],
        "execution_engine_blocks_cross": "margin_mode != \"isolated\"" in text["execution_engine"] or "margin_mode != 'isolated'" in text["execution_engine"],
        "set_leverage_guarded": "ensure_isolated_margin" in text["execution_engine"] and "set_leverage" in text["execution_engine"],
        "dangerous_cross_literals": dangerous,
        "dangerous_fallbacks": dangerous,
    }


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _recommended_action(status: str) -> str:
    if status == "ISOLATED_CONFIRMED":
        return "KEEP_ISOLATED_MARGIN_GUARDS"
    if status == "CROSS_DETECTED_BAD":
        return "FIX_MARGIN_MODE_BEFORE_ANY_PAPER_FILTER"
    return "VERIFY_MARGIN_MODE_BEFORE_ANY_PAPER_FILTER"


class _UnsafeConfig:
    margin_mode = "cross"
    force_isolated_margin = False
    disallow_crossed_margin = False
    auto_margin = True
