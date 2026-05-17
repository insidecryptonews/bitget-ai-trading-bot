from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .catalyst_registry import CatalystRegistry
from .config import BotConfig
from .database import Database
from .edge_guard import ALLOW_PAPER, EdgeGuard
from .news_risk_gate import NEWS_CATALYST_BOOST_RESEARCH_ONLY, NewsRiskGate
from .utils import safe_float, safe_int


START = "PAPER POLICY LAB START"
END = "PAPER POLICY LAB END"


class PaperPolicyLab:
    """Research-only candidate paper policies. It never toggles filters or opens trades."""

    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        edge = EdgeGuard(self.config, self.db).build_edge_guard_report(hours=hours)
        catalyst = CatalystRegistry(self.config, self.db).build_summary(hours=hours)
        news = NewsRiskGate(self.config, self.db).build(hours=hours)
        candidates = self._candidate_policies(edge, catalyst, news)
        blocked = self._blocked(edge, news)
        return {
            "hours": hours,
            "candidate_policies": candidates,
            "blocked": blocked,
            "catalyst_summary": catalyst,
            "news_risk_gate": news,
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            "candidate_policies:",
            *_policy_lines(payload["candidate_policies"]),
            "blocked:",
            *_blocked_lines(payload["blocked"]),
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)

    def _candidate_policies(self, edge: dict[str, Any], catalyst: dict[str, Any], news: dict[str, Any]) -> list[dict[str, Any]]:
        policies: list[dict[str, Any]] = []
        catalyst_dependent = "catalyst_dependent_edge" in catalyst.get("risk_flags", [])
        boost_symbols = {
            str(row.get("symbol") or "").upper()
            for row in news.get("symbol_decisions", [])
            if row.get("decision") == NEWS_CATALYST_BOOST_RESEARCH_ONLY
        }
        edge_rows = (
            edge.get("allow_paper_candidates", [])
            + edge.get("gross_edge_only_candidates", [])
            + edge.get("watch_only_candidates", [])
        )
        for row in edge_rows:
            group_type = str(row.get("group_type") or "")
            group_value = str(row.get("group_value") or "")
            samples = safe_int(row.get("total_labels"))
            pf = safe_float(row.get("profit_factor"))
            if samples < max(5, self.config.edge_guard_min_sample // 2) or pf < 1.1:
                continue
            decision = "PAPER_CANDIDATE" if row.get("decision") == ALLOW_PAPER else "SHADOW_VALIDATE"
            requires_catalyst = catalyst_dependent or (group_type == "symbol" and group_value.upper() in boost_symbols)
            policy = {
                "policy_id": f"policy_{group_type}_{group_value}".replace(" ", "_"),
                "symbol_allowlist": group_value if group_type == "symbol" else "",
                "symbol_blocklist": "",
                "side_allowlist": group_value if group_type == "side" else "",
                "regime_allowlist": group_value if group_type == "regime" else "",
                "regime_blocklist": "",
                "score_bucket_allowlist": group_value if group_type == "score_bucket" else "",
                "source_allowlist": "trade_signal",
                "tp_pct": 1.50,
                "sl_pct": 0.75,
                "max_hold_bars": min(30, self.config.mfe_mae_max_bars),
                "min_pf": self.config.edge_guard_min_pf,
                "min_samples": self.config.edge_guard_min_sample,
                "max_sl_ratio": self.config.edge_guard_max_sl_ratio,
                "min_tp_ratio": self.config.edge_guard_min_tp_ratio,
                "max_time_ratio": self.config.edge_guard_max_time_ratio,
                "requires_catalyst": requires_catalyst,
                "catalyst_id": _first_catalyst_id(catalyst) if requires_catalyst else "",
                "news_gate_decision": NEWS_CATALYST_BOOST_RESEARCH_ONLY if requires_catalyst else "NEWS_ALLOW",
                "decision": decision,
                "reason": _policy_reason(row, requires_catalyst),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "metrics": row,
            }
            policies.append(policy)
        return policies[:12]

    @staticmethod
    def _blocked(edge: dict[str, Any], news: dict[str, Any]) -> list[dict[str, Any]]:
        blocked = []
        for row in edge.get("block_paper_candidates", [])[:12]:
            blocked.append({"group": row.get("group_value"), "type": row.get("group_type"), "reason": row.get("reason"), "source": "edge_guard"})
        for row in news.get("blocked", [])[:12]:
            blocked.append({"group": row.get("symbol"), "type": "news", "reason": row.get("reason"), "source": "news_risk_gate"})
        return blocked


def _first_catalyst_id(summary: dict[str, Any]) -> str:
    rows = summary.get("active_catalysts", [])
    return str(rows[0].get("catalyst_id") or "") if rows else ""


def _policy_reason(row: dict[str, Any], requires_catalyst: bool) -> str:
    suffix = "; validate catalyst dependency" if requires_catalyst else "; validate walk-forward"
    return f"{row.get('group_type')} {row.get('group_value')} PF={safe_float(row.get('profit_factor')):.2f}{suffix}"


def _policy_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- policy_id={row.get('policy_id')} decision={row.get('decision')} "
            f"symbol_allowlist={row.get('symbol_allowlist')} side_allowlist={row.get('side_allowlist')} "
            f"regime_allowlist={row.get('regime_allowlist')} score_bucket_allowlist={row.get('score_bucket_allowlist')} "
            f"requires_catalyst={str(row.get('requires_catalyst')).lower()} reason={row.get('reason')}"
        )
        for row in rows[:8]
    ]


def _blocked_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [f"- {row.get('type')}:{row.get('group')} reason={row.get('reason')} source={row.get('source')}" for row in rows[:12]]
