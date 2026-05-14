from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .catalyst_registry import edge_metrics, match_catalysts
from .config import BotConfig
from .database import Database
from .paper_policy_lab import PaperPolicyLab
from .utils import safe_float, safe_int
from .walk_forward_validation import _filter_policy


START = "POLICY BACKTEST START"
END = "POLICY BACKTEST END"


class PolicyBacktest:
    """Research-only policy backtest over stored labels."""

    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        now = datetime.now(timezone.utc)
        since = (now - timedelta(hours=hours)).isoformat()
        rows = self.db.fetch_labeled_signal_rows_since(since, limit=50000) if hasattr(self.db, "fetch_labeled_signal_rows_since") else []
        policies = PaperPolicyLab(self.config, self.db).build(hours=hours).get("candidate_policies", [])
        baseline = edge_metrics(rows)
        filtered_rows: list[dict[str, Any]] = []
        by_policy = []
        for policy in policies:
            selected = _filter_policy(rows, policy)
            filtered_rows.extend(selected)
            item = edge_metrics(selected)
            item["policy_id"] = policy.get("policy_id")
            by_policy.append(item)
        filtered = edge_metrics(_dedupe(filtered_rows))
        catalysts = self.db.fetch_market_catalysts(since_iso=since, until_iso=now.isoformat(), limit=500)
        with_cat = []
        without_cat = []
        for row in rows:
            bucket = with_cat if match_catalysts(catalysts, str(row.get("symbol") or ""), str(row.get("label_timestamp") or row.get("timestamp") or "")) else without_cat
            bucket.append(row)
        return {
            "hours": hours,
            "baseline": baseline,
            "policy_filtered": filtered,
            "improvement_vs_baseline": safe_float(filtered.get("profit_factor")) - safe_float(baseline.get("profit_factor")),
            "by_policy": by_policy,
            "blocked_impact": _blocked_impact(rows, filtered_rows),
            "catalyst_impact": {
                "with_catalyst": edge_metrics(with_cat),
                "without_catalyst": edge_metrics(without_cat),
            },
            "recommendation": "NO LIVE",
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            "baseline:",
            _metrics_line(payload["baseline"]),
            "policy_filtered:",
            _metrics_line(payload["policy_filtered"]),
            f"improvement_vs_baseline: {safe_float(payload['improvement_vs_baseline']):.2f}",
            "by_policy:",
            *_policy_lines(payload["by_policy"]),
            "blocked_impact:",
            *_blocked_lines(payload["blocked_impact"]),
            "catalyst_impact:",
            f"- with_catalyst PF={safe_float(payload['catalyst_impact']['with_catalyst'].get('profit_factor')):.2f}",
            f"- without_catalyst PF={safe_float(payload['catalyst_impact']['without_catalyst'].get('profit_factor')):.2f}",
            "recommendation:",
            "- NO LIVE",
            END,
        ]
        return "\n".join(lines)


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    unique = []
    for row in rows:
        key = row.get("label_id") or row.get("observation_id") or id(row)
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _blocked_impact(rows: list[dict[str, Any]], selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected_ids = {row.get("label_id") or row.get("observation_id") for row in selected}
    blocked = [row for row in rows if (row.get("label_id") or row.get("observation_id")) not in selected_ids]
    impact = []
    for key in ("side", "market_regime", "symbol"):
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in blocked:
            groups.setdefault(str(row.get(key) or "NA"), []).append(row)
        for value, group in groups.items():
            metrics = edge_metrics(group)
            if safe_int(metrics.get("samples")) >= 5:
                impact.append({"group": value, "type": key, "avoided_loss": -min(0.0, safe_float(metrics.get("expectancy"))), **metrics})
    impact.sort(key=lambda row: safe_float(row.get("avoided_loss")), reverse=True)
    return impact[:8]


def _metrics_line(metrics: dict[str, Any]) -> str:
    return (
        f"- samples={safe_int(metrics.get('samples'))} PF={safe_float(metrics.get('profit_factor')):.2f} "
        f"TP%={safe_float(metrics.get('tp_ratio')) * 100:.1f} SL%={safe_float(metrics.get('sl_ratio')) * 100:.1f} "
        f"TIME%={safe_float(metrics.get('time_ratio')) * 100:.1f}"
    )


def _policy_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [f"- {row.get('policy_id')} samples={safe_int(row.get('samples'))} PF={safe_float(row.get('profit_factor')):.2f}" for row in rows[:8]]


def _blocked_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [f"- {row.get('type')}:{row.get('group')} avoided_loss={safe_float(row.get('avoided_loss')):.4f}" for row in rows[:8]]
