from __future__ import annotations

import csv
import json
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .catalyst_classifier import CatalystClassifier
from .config import BotConfig
from .database import Database
from .utils import sanitize


@dataclass
class IngestResult:
    feeds_checked: int = 0
    items_seen: int = 0
    catalysts_created: int = 0
    duplicates_or_updates: int = 0
    errors: list[str] | None = None

    def to_text(self) -> str:
        errors = self.errors or []
        lines = [
            "CATALYST INGEST START",
            f"feeds_checked: {self.feeds_checked}",
            f"items_seen: {self.items_seen}",
            f"catalysts_created: {self.catalysts_created}",
            f"duplicates_or_updates: {self.duplicates_or_updates}",
            "errors:",
            *([f"- {error[:180]}" for error in errors[:5]] if errors else ["- none"]),
            "final_recommendation: NO LIVE",
            "CATALYST INGEST END",
        ]
        return "\n".join(lines)


class NewsCatalystIngestor:
    """Lightweight RSS/manual catalyst ingestor. It never trades."""

    def __init__(self, config: BotConfig, db: Database, logger=None) -> None:
        self.config = config
        self.db = db
        self.logger = logger
        self.classifier = CatalystClassifier()

    def run(self, *, hours: int | None = None) -> IngestResult:
        result = IngestResult(errors=[])
        if not self.config.enable_news_catalyst_intelligence:
            return result
        max_items = max(1, int(self.config.news_catalyst_max_items_per_run or 50))
        items: list[dict[str, Any]] = []
        for path in (self.config.news_catalyst_manual_events_file, self.config.news_catalyst_social_import_file):
            if path:
                result.feeds_checked += 1
                try:
                    items.extend(self._read_local_file(Path(path)))
                except Exception as exc:
                    result.errors.append(f"{path}: {exc}")
        for feed in self._feeds():
            if len(items) >= max_items:
                break
            result.feeds_checked += 1
            try:
                items.extend(self._read_rss(feed)[: max_items - len(items)])
            except Exception as exc:
                result.errors.append(f"{feed}: {exc}")
                if self.logger:
                    self.logger.warning("Catalyst RSS fallo: %s", exc)
        for item in items[:max_items]:
            result.items_seen += 1
            event = self.classifier.classify(
                title=str(item.get("title") or ""),
                summary=str(item.get("summary") or ""),
                source=str(item.get("source") or "rss"),
                published_at=item.get("published_at"),
                symbols_hint=_symbols_from_item(item),
                source_url=str(item.get("url") or ""),
            )
            before = self.db.fetch_market_catalysts(since_iso=event.start_at, until_iso=event.end_at, limit=1000)
            existed = any(row.get("catalyst_id") == event.catalyst_id for row in before)
            saved = self.db.upsert_market_catalyst(event.to_record())
            if saved:
                if existed:
                    result.duplicates_or_updates += 1
                else:
                    result.catalysts_created += 1
        return result

    def _feeds(self) -> list[str]:
        raw = ",".join([
            self.config.news_catalyst_rss_feeds,
            self.config.news_catalyst_official_feeds,
            self.config.news_catalyst_exchange_feeds,
        ])
        return [item.strip() for item in raw.split(",") if item.strip()]

    def _read_local_file(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            rows = data if isinstance(data, list) else data.get("items", [])
            return [dict(row) for row in rows if isinstance(row, dict)]
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                rows.append(dict(row))
        return rows

    def _read_rss(self, url: str) -> list[dict[str, Any]]:
        request = urllib.request.Request(url, headers={"User-Agent": "bitget-ai-trading-bot-research/1.0"})
        with urllib.request.urlopen(request, timeout=max(1, int(self.config.news_catalyst_timeout_seconds or 10))) as response:  # noqa: S310 - user configured public feeds
            content = response.read(max(512_000))
        root = ET.fromstring(content)
        items: list[dict[str, Any]] = []
        for item in root.findall(".//item")[: max(1, int(self.config.news_catalyst_max_items_per_run or 50))]:
            title = item.findtext("title") or ""
            summary = item.findtext("description") or ""
            link = item.findtext("link") or ""
            published = item.findtext("pubDate") or ""
            items.append({"title": sanitize(title), "summary": sanitize(summary), "url": link, "published_at": published, "source": "rss"})
        return items


def _symbols_from_item(item: dict[str, Any]) -> list[str]:
    raw = item.get("symbols") or item.get("symbol") or ""
    if isinstance(raw, list):
        return [str(symbol).upper() for symbol in raw if symbol]
    return [part.strip().upper() for part in str(raw).split(",") if part.strip()]
