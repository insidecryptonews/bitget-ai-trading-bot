from __future__ import annotations

from typing import Any

from .edge_hardening_utils import FINAL_NO_LIVE, fetch_group_metrics, format_num, since_iso
from .utils import safe_float, safe_int


START = "SIZING SAFETY LAB START"
END = "SIZING SAFETY LAB END"


class SizingSafetyLab:
    """Simulation-only sizing review. It does not modify live or paper sizing."""

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        rows = fetch_group_metrics(self.db, since=since_iso(hours), group_key="symbol", limit=25, min_samples=1)
        profiles = [
            _profile("fixed_notional", rows, multiplier=1.0),
            _profile("equity_aware_notional", rows, multiplier=0.8),
            _profile("max_notional_cap", rows, multiplier=0.6),
            _profile("symbol_exposure_cap", rows, multiplier=0.5),
            _profile("regime_exposure_cap", rows, multiplier=0.5),
        ]
        unsafe = [row for row in profiles if row["ruin_risk_proxy"] > 0.35 or row["drawdown_proxy"] > 5.0]
        safe = [row for row in profiles if row not in unsafe]
        recommended = safe[0]["name"] if safe else "research_only_no_sizing_change"
        return {
            "hours": max(1, int(hours or 24)),
            "profiles": profiles,
            "unsafe_profiles": unsafe,
            "recommended_sizing_profile": f"{recommended} (research only)",
            "slots_changed": False,
            "live_sizing_touched": False,
            "final_recommendation": FINAL_NO_LIVE,
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            "profiles:",
            *_profile_lines(payload["profiles"]),
            "unsafe_profiles_rejected:",
            *_profile_lines(payload["unsafe_profiles"]),
            f"recommended_sizing_profile: {payload['recommended_sizing_profile']}",
            "slots_changed=false",
            "live_sizing_touched=false",
            "final_recommendation: NO LIVE",
            END,
        ])


def _profile(name: str, rows: list[dict[str, Any]], *, multiplier: float) -> dict[str, Any]:
    samples = sum(safe_int(row.get("samples")) for row in rows)
    weighted_loss = sum(abs(safe_float(row.get("worst_return"))) * safe_int(row.get("samples")) for row in rows)
    drawdown = (weighted_loss / max(samples, 1)) * multiplier
    sl_ratio = sum(safe_float(row.get("sl_ratio")) * safe_int(row.get("samples")) for row in rows) / max(samples, 1)
    ruin = min(1.0, sl_ratio * multiplier * 2.0)
    return {
        "name": name,
        "samples": samples,
        "drawdown_proxy": drawdown,
        "ruin_risk_proxy": ruin,
        "max_exposure_per_symbol": multiplier,
        "decision": "REJECT" if ruin > 0.35 else "WATCH_ONLY",
    }


def _profile_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        f"- {row.get('name')} samples={row.get('samples')} drawdown_proxy={format_num(row.get('drawdown_proxy'))} ruin_risk_proxy={format_num(row.get('ruin_risk_proxy'))} decision={row.get('decision')}"
        for row in rows
    ]
