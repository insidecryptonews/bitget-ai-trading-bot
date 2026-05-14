from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BotConfig
from .database import Database


START = "NEWS RISK GATE START"
END = "NEWS RISK GATE END"
NEWS_ALLOW = "NEWS_ALLOW"
NEWS_WATCH = "NEWS_WATCH"
NEWS_BLOCK_LONG = "NEWS_BLOCK_LONG"
NEWS_BLOCK_SHORT = "NEWS_BLOCK_SHORT"
NEWS_BLOCK_SYMBOL = "NEWS_BLOCK_SYMBOL"
NEWS_BLOCK_ALL_PAPER = "NEWS_BLOCK_ALL_PAPER"
NEWS_RISK_OFF = "NEWS_RISK_OFF"
NEWS_CATALYST_BOOST_RESEARCH_ONLY = "NEWS_CATALYST_BOOST_RESEARCH_ONLY"


class NewsRiskGate:
    """Research-only news/catalyst risk recommendations."""

    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        now = datetime.now(timezone.utc)
        since = (now - timedelta(hours=hours)).isoformat()
        catalysts = self.db.fetch_market_catalysts(since_iso=since, until_iso=now.isoformat(), limit=500)
        global_decision = NEWS_ALLOW
        symbol_decisions: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for catalyst in catalysts:
            category = str(catalyst.get("category") or "other")
            direction = str(catalyst.get("direction") or "unknown")
            severity = str(catalyst.get("severity") or "low")
            symbols = _symbols(catalyst)
            decision, reason = self._decision(category, direction, severity)
            if decision in {NEWS_BLOCK_ALL_PAPER, NEWS_RISK_OFF}:
                global_decision = decision
            for symbol in symbols:
                if symbol == "GLOBAL" and decision not in {NEWS_CATALYST_BOOST_RESEARCH_ONLY, NEWS_WATCH}:
                    blocked.append({"symbol": symbol, "decision": decision, "reason": reason, "catalyst_id": catalyst.get("catalyst_id")})
                    continue
                row = {
                    "symbol": symbol,
                    "decision": decision,
                    "reason": reason,
                    "catalyst_id": catalyst.get("catalyst_id"),
                    "category": category,
                    "direction": direction,
                    "severity": severity,
                }
                symbol_decisions.append(row)
                if decision in {NEWS_BLOCK_SYMBOL, NEWS_BLOCK_LONG, NEWS_BLOCK_SHORT, NEWS_BLOCK_ALL_PAPER, NEWS_RISK_OFF}:
                    blocked.append(row)
        return {
            "hours": hours,
            "global_decision": global_decision,
            "symbol_decisions": symbol_decisions,
            "blocked": blocked,
            "active_catalysts": catalysts,
            "recommendation": "NO LIVE",
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            f"global_decision: {payload['global_decision']}",
            "symbol_decisions:",
            *_decision_lines(payload["symbol_decisions"]),
            "blocked:",
            *_decision_lines(payload["blocked"]),
            "recommendation:",
            "- NO LIVE",
            END,
        ]
        return "\n".join(lines)

    @staticmethod
    def _decision(category: str, direction: str, severity: str) -> tuple[str, str]:
        if severity == "critical" and direction == "bearish":
            return NEWS_BLOCK_ALL_PAPER, "critical_bearish_global_or_symbol"
        if category in {"hack", "exploit", "security_incident"}:
            return NEWS_BLOCK_SYMBOL, "security_event"
        if category in {"exchange_delisting", "exchange_outage"}:
            return NEWS_BLOCK_SYMBOL, "exchange_event"
        if category in {"macro", "geopolitical"} and direction == "bearish":
            return NEWS_RISK_OFF, "macro_risk_off"
        if category in {"regulation", "legislation", "sec", "cftc"} and direction == "bullish":
            return NEWS_CATALYST_BOOST_RESEARCH_ONLY, "bullish_catalyst_research_only"
        if category == "influencer_social":
            return NEWS_WATCH, "social_unconfirmed"
        if direction == "bearish":
            return NEWS_BLOCK_LONG, "bearish_catalyst"
        if direction == "bullish":
            return NEWS_CATALYST_BOOST_RESEARCH_ONLY, "bullish_catalyst_research_only"
        return NEWS_WATCH, "unknown_or_mixed"


def _symbols(catalyst: dict[str, Any]) -> list[str]:
    raw = str(catalyst.get("symbols") or "GLOBAL")
    symbols = [item.strip().upper() for item in raw.split(",") if item.strip()]
    return symbols or ["GLOBAL"]


def _decision_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        f"- {row.get('symbol')} {row.get('decision')} reason={row.get('reason')} catalyst_id={row.get('catalyst_id')}"
        for row in rows[:12]
    ]
