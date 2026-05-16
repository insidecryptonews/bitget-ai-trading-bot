from __future__ import annotations

from typing import Any

from .edge_hardening_utils import (
    FINAL_NO_LIVE,
    apply_net_costs,
    cost_config,
    decision_reason,
    fetch_group_metrics,
    format_num,
    format_pct,
    safe_top,
    since_iso,
)
from .utils import safe_float


START = "NET EDGE LAB START"
END = "NET EDGE LAB END"


class NetEdgeLab:
    """Cost-aware edge analysis. Research-only; it never changes trading state."""

    GROUPS = ("symbol", "side", "market_regime", "score_bucket", "policy_id", "source", "strategy")

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        costs = cost_config(self.config)
        since = since_iso(hours)
        rows_by_group: dict[str, list[dict[str, Any]]] = {}
        all_rows: list[dict[str, Any]] = []
        for group in self.GROUPS:
            rows = [
                apply_net_costs(row, costs)
                for row in fetch_group_metrics(self.db, since=since, group_key=group, limit=30, min_samples=1)
            ]
            for row in rows:
                row["reason"] = decision_reason(row, costs)
            rows.sort(key=lambda item: (safe_float(item.get("net_PF")), safe_float(item.get("net_EV"))), reverse=True)
            rows_by_group[group] = rows
            all_rows.extend(rows)
        all_rows.sort(key=lambda item: (safe_float(item.get("net_PF")), safe_float(item.get("net_EV"))), reverse=True)
        return {
            "hours": max(1, int(hours or 24)),
            "using_conservative_cost_defaults": True,
            "costs": {
                "taker_fee_bps": costs.taker_fee_bps,
                "maker_fee_bps": costs.maker_fee_bps,
                "slippage_bps": costs.slippage_bps,
                "funding_bps_per_8h": costs.funding_bps_per_8h,
                "min_net_pf": costs.min_net_pf,
                "min_samples": costs.min_samples,
            },
            "by_group": rows_by_group,
            "top_candidates": safe_top([row for row in all_rows if row.get("final_decision") == "PAPER_CANDIDATE"], 10),
            "watch_or_shadow": safe_top([row for row in all_rows if row.get("final_decision") in {"WATCH_ONLY", "SHADOW_CANDIDATE"}], 10),
            "rejects": safe_top([row for row in all_rows if row.get("final_decision") == "REJECT"], 10),
            "final_recommendation": FINAL_NO_LIVE,
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            f"using_conservative_cost_defaults={str(payload['using_conservative_cost_defaults']).lower()}",
            "costs:",
            f"- taker_fee_bps={payload['costs']['taker_fee_bps']}",
            f"- maker_fee_bps={payload['costs']['maker_fee_bps']}",
            f"- slippage_bps={payload['costs']['slippage_bps']}",
            f"- funding_bps_per_8h={payload['costs']['funding_bps_per_8h']}",
            "top_candidates:",
            *_row_lines(payload["top_candidates"]),
            "watch_or_shadow:",
            *_row_lines(payload["watch_or_shadow"]),
            "rejects:",
            *_row_lines(payload["rejects"]),
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)


def _row_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    out = []
    for row in rows[:10]:
        out.append(
            "- "
            f"{row.get('group_key')}={row.get('group_value')} "
            f"samples={row.get('samples')} gross_PF={format_num(row.get('gross_PF'))} "
            f"net_PF={format_num(row.get('net_PF'))} net_EV={format_num(row.get('net_EV'), 4)} "
            f"TP={format_pct(row.get('tp_ratio'))} SL={format_pct(row.get('sl_ratio'))} "
            f"TIME={format_pct(row.get('time_ratio'))} confidence={row.get('confidence_class')} "
            f"decision={row.get('final_decision')} reason={row.get('reason')}"
        )
    return out
