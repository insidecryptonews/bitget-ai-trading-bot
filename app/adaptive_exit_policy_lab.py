from __future__ import annotations

from typing import Any

from .config import BotConfig
from .database import Database
from .time_death_lab import TimeDeathLab
from .utils import safe_float, safe_int


START = "ADAPTIVE EXIT POLICY START"
END = "ADAPTIVE EXIT POLICY END"


class AdaptiveExitPolicyLab:
    """Research-only candidate exit rules. It never changes execution config."""

    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        report = TimeDeathLab(self.config, self.db).build(hours=hours)
        policies = _candidate_policies(report)
        return {
            "hours": hours,
            "candidate_exit_policies": policies,
            "blocked_or_watch": _blocked_or_watch(report),
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            "candidate_exit_policies:",
            *_policy_lines(payload["candidate_exit_policies"]),
            "blocked_or_watch:",
            *[f"- {item}" for item in payload["blocked_or_watch"]],
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)


def _candidate_policies(report: dict[str, Any]) -> list[dict[str, Any]]:
    policies: list[dict[str, Any]] = []
    for row in report.get("worst_time_groups", [])[:8]:
        time_ratio = safe_float(row.get("time_ratio"))
        if time_ratio < 0.60:
            continue
        group = f"{row.get('group_key')}={row.get('group_value')}"
        policies.append({
            "group": group,
            "max_hold_bars": 20 if time_ratio >= 0.80 else 30,
            "early_exit_after_bars": 5 if time_ratio >= 0.80 else 10,
            "min_mfe_required": 0.25,
            "profit_lock": 0.50,
            "reason": "high_time_death_ratio",
        })
    for row in report.get("decay_groups", [])[:5]:
        if safe_float(row.get("hit_ratio")) >= 0.20:
            policies.append({
                "group": f"{row.get('group_key')}={row.get('group_value')}",
                "max_hold_bars": 20,
                "early_exit_after_bars": 10,
                "min_mfe_required": 0.25,
                "profit_lock": 0.50,
                "reason": "mfe_decay_after_initial_move",
            })
    return policies[:12]


def _blocked_or_watch(report: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    overall = report.get("overall", {})
    if safe_float(overall.get("time_ratio")) > 0.60:
        lines.append("watch: muchas senales mueren por TIME; no activar cambios automaticos")
    if safe_float(overall.get("profit_factor")) < 1.0:
        lines.append("block_live: PF insuficiente; solo research/paper")
    if safe_int(overall.get("total")) <= 0:
        lines.append("watch: evidencia insuficiente")
    return lines or ["none"]


def _policy_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- group={row['group']} max_hold_bars={row['max_hold_bars']} "
            f"early_exit_after_bars={row['early_exit_after_bars']} min_mfe_required={row['min_mfe_required']:.2f}% "
            f"profit_lock={row['profit_lock']:.2f}% reason={row['reason']}"
        )
        for row in rows
    ]
