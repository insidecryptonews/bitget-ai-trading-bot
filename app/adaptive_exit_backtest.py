from __future__ import annotations

from typing import Any

from .edge_hardening_utils import FINAL_NO_LIVE, cost_config
from .exit_policy_backtest import ExitPolicyBacktest
from .utils import safe_float, safe_int


START = "ADAPTIVE EXIT BACKTEST START"
END = "ADAPTIVE EXIT BACKTEST END"


class AdaptiveExitBacktest:
    """Research-only exit backtest with conservative cost adjustment."""

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        costs = cost_config(self.config)
        base = ExitPolicyBacktest(self.config, self.db).build(hours=hours)
        variants = []
        for row in base.get("variants", []):
            item = dict(row)
            total_cost_pct = (2 * costs.taker_fee_bps + 2 * costs.slippage_bps + costs.funding_bps_per_8h) / 100.0
            item["estimated_total_cost"] = total_cost_pct
            item["net_expectancy"] = safe_float(item.get("expectancy")) - total_cost_pct
            gross_pf = safe_float(item.get("profit_factor"))
            item["net_pf_proxy"] = max(0.0, gross_pf - total_cost_pct)
            item["decision"] = "RESEARCH_ONLY"
            variants.append(item)
        variants.sort(key=lambda item: (safe_float(item.get("net_pf_proxy")), safe_float(item.get("net_expectancy"))), reverse=True)
        return {
            "hours": max(1, int(hours or 24)),
            "baseline": base.get("baseline", {}),
            "variants": variants,
            "best_by_group": base.get("best_by_group", []),
            "final_recommendation": FINAL_NO_LIVE,
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            "baseline:",
            _metric_line(payload["baseline"]),
            "variants:",
            *_variant_lines(payload["variants"]),
            "group_specific_exit:",
            *_group_lines(payload["best_by_group"]),
            "recommendation:",
            "- research_only",
            "- no live",
            "final_recommendation: NO LIVE",
            END,
        ])


def _metric_line(row: dict[str, Any]) -> str:
    return (
        f"- samples={safe_int(row.get('samples'))} PF={safe_float(row.get('profit_factor')):.2f} "
        f"TP%={safe_float(row.get('tp_ratio')) * 100:.1f} SL%={safe_float(row.get('sl_ratio')) * 100:.1f} "
        f"TIME%={safe_float(row.get('time_ratio')) * 100:.1f}"
    )


def _variant_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- name={row.get('name')} samples={safe_int(row.get('samples'))} gross_PF={safe_float(row.get('profit_factor')):.2f} "
            f"net_pf_proxy={safe_float(row.get('net_pf_proxy')):.2f} net_expectancy={safe_float(row.get('net_expectancy')):.4f} "
            f"cost={safe_float(row.get('estimated_total_cost')):.4f} decision=RESEARCH_ONLY"
        )
        for row in rows[:12]
    ]


def _group_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        f"- {row.get('group_key')}={row.get('group_value')} best={row.get('name')} PF={safe_float(row.get('profit_factor')):.2f}"
        for row in rows[:10]
    ]
