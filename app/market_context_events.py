from __future__ import annotations

from typing import Any

from .database import Database
from .utils import iso_utc, json_dumps


class MarketContextEvents:
    """Optional future hook for news, macro, liquidation or sentiment context."""

    def __init__(self, db: Database | None = None, logger=None) -> None:
        self.db = db
        self.logger = logger

    def record_event(
        self,
        *,
        timestamp: str,
        source: str,
        event_type: str,
        symbol: str = "",
        severity: str = "info",
        title: str = "",
        summary: str = "",
        raw: Any | None = None,
    ) -> int:
        if self.db is None:
            return 0
        return self.db.record_market_context_event(
            {
                "timestamp": timestamp,
                "source": source,
                "event_type": event_type,
                "symbol": symbol,
                "severity": severity,
                "title": title,
                "summary": summary,
                "raw_json": json_dumps(raw or {}),
                "created_at": iso_utc(),
            }
        )

    def events(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self.db.fetch_market_context_events(limit) if self.db else []

