from __future__ import annotations

from typing import Any

from .edge_hardening_utils import FINAL_NO_LIVE, apply_net_costs, cost_config, fetch_group_metrics, fetch_recent_event_counts, format_num, since_iso
from .utils import safe_int


START = "DECISION LEDGER AUDIT START"
END = "DECISION LEDGER AUDIT END"


class DecisionLedgerAudit:
    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        since = since_iso(hours)
        costs = cost_config(self.config)
        events = fetch_recent_event_counts(self.db, since=since)
        missed = _safe(lambda: self.db.get_missed_high_score_summary_since(since), {"total": 0, "by_reason": []})
        source_rows = [
            apply_net_costs(row, costs)
            for row in fetch_group_metrics(self.db, since=since, group_key="source", limit=25, min_samples=1)
        ]
        block_rows = [
            apply_net_costs(row, costs)
            for row in fetch_group_metrics(self.db, since=since, group_key="policy_id", limit=25, min_samples=1)
            if safe_int(row.get("samples")) >= 1 and row.get("final_decision") == "REJECT"
        ]
        missed_winners = [row for row in source_rows if str(row.get("source") or "").lower() in {"high_score_missed", "edge_guard_block"}]
        return {
            "hours": max(1, int(hours or 24)),
            "allowed_count": sum(count for key, count in events.items() if "ALLOW" in key.upper() or "paper_open" in key),
            "blocked_count": sum(count for key, count in events.items() if "BLOCK" in key.upper() or "reject" in key.lower()),
            "event_counts": events,
            "high_score_missed": missed,
            "edge_net_by_source": source_rows,
            "blocked_groups_net": block_rows[:10],
            "top_missed_opportunities": missed_winners[:10],
            "final_recommendation": FINAL_NO_LIVE,
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            f"allowed_signals={payload['allowed_count']}",
            f"blocked_signals={payload['blocked_count']}",
            f"high_score_missed={payload['high_score_missed'].get('total', 0)}",
            "block_reasons:",
            *_event_lines(payload["event_counts"]),
            "edge_net_by_source:",
            *_source_lines(payload["edge_net_by_source"]),
            "blocked_groups_net:",
            *_source_lines(payload["blocked_groups_net"]),
            "top_missed_opportunities_caution:",
            *_source_lines(payload["top_missed_opportunities"]),
            "final_recommendation: NO LIVE",
            END,
        ])


def _safe(fn, fallback):
    try:
        return fn()
    except Exception:
        return fallback


def _event_lines(events: dict[str, int]) -> list[str]:
    if not events:
        return ["- none"]
    return [f"- {key}={value}" for key, value in sorted(events.items(), key=lambda item: item[1], reverse=True)[:20]]


def _source_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        f"- {row.get('group_key')}={row.get('group_value')} samples={row.get('samples')} net_PF={format_num(row.get('net_PF'))} net_EV={format_num(row.get('net_EV'), 4)} decision={row.get('final_decision')}"
        for row in rows[:10]
    ]
