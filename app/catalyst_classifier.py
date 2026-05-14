from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .utils import sanitize


SYMBOL_ALIASES = {
    "BTC": "BTCUSDT",
    "BITCOIN": "BTCUSDT",
    "ETH": "ETHUSDT",
    "ETHEREUM": "ETHUSDT",
    "XRP": "XRPUSDT",
    "RIPPLE": "XRPUSDT",
    "SOL": "SOLUSDT",
    "SOLANA": "SOLUSDT",
    "DOGE": "DOGEUSDT",
    "DOGECOIN": "DOGEUSDT",
    "ADA": "ADAUSDT",
    "CARDANO": "ADAUSDT",
    "BNB": "BNBUSDT",
    "DOT": "DOTUSDT",
    "POLKADOT": "DOTUSDT",
    "AVAX": "AVAXUSDT",
    "AVALANCHE": "AVAXUSDT",
    "LINK": "LINKUSDT",
    "CHAINLINK": "LINKUSDT",
}


@dataclass(frozen=True)
class CatalystEvent:
    catalyst_id: str
    title: str
    category: str
    symbols: list[str]
    regimes: list[str]
    direction: str
    severity: str
    confidence: float
    source: str
    source_url_hash: str
    published_at: str
    start_at: str
    end_at: str
    summary: str
    raw_ref: str = ""

    def to_record(self) -> dict[str, Any]:
        return {
            "catalyst_id": self.catalyst_id,
            "title": self.title[:240],
            "category": self.category,
            "symbols": ",".join(self.symbols),
            "regimes": ",".join(self.regimes),
            "direction": self.direction,
            "severity": self.severity,
            "confidence": self.confidence,
            "source": self.source,
            "source_url_hash": self.source_url_hash,
            "published_at": self.published_at,
            "start_at": self.start_at,
            "end_at": self.end_at,
            "summary": self.summary[:700],
            "raw_ref": self.raw_ref[:500],
        }


class CatalystClassifier:
    """Deterministic catalyst classifier. No external AI, no private APIs."""

    def classify(
        self,
        *,
        title: str,
        summary: str = "",
        source: str = "manual",
        published_at: datetime | str | None = None,
        symbols_hint: list[str] | None = None,
        source_url: str = "",
    ) -> CatalystEvent:
        title_clean = sanitize(title or "").strip()[:240]
        summary_clean = sanitize(summary or "").strip()[:700]
        text = f"{title_clean} {summary_clean}".lower()
        published = _parse_dt(published_at)
        category, direction, severity, confidence = self._classify_text(text)
        symbols = _detect_symbols(text, symbols_hint)
        regimes = _regimes_for(category, direction)
        window = _window_hours(severity)
        catalyst_id = _stable_id(title_clean, published.isoformat(), ",".join(symbols), source_url)
        url_hash = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:16] if source_url else ""
        return CatalystEvent(
            catalyst_id=catalyst_id,
            title=title_clean or "untitled catalyst",
            category=category,
            symbols=symbols,
            regimes=regimes,
            direction=direction,
            severity=severity,
            confidence=confidence,
            source=source or "unknown",
            source_url_hash=url_hash,
            published_at=published.isoformat(),
            start_at=published.isoformat(),
            end_at=(published + timedelta(hours=window)).isoformat(),
            summary=summary_clean or title_clean,
            raw_ref=source_url,
        )

    def _classify_text(self, text: str) -> tuple[str, str, str, float]:
        if _has(text, "hack", "exploit", "bridge attack", "drain", "stolen", "vulnerability", "pause withdrawals"):
            return "hack", "bearish", "critical", 0.86
        if _has(text, "delisting", "delist", "withdrawal suspension", "outage", "maintenance"):
            return "exchange_delisting" if _has(text, "delist", "delisting") else "exchange_outage", "bearish", "high", 0.78
        if _has(text, "ban", "prohibit", "enforcement", "sec charges", "cftc charges", "crackdown", "sanctions", "investigation", "lawsuit"):
            return "regulation", "bearish", "high", 0.76
        if _has(text, "clarity", "crypto market structure", "bill advances", "etf approval", "lawsuit dismissed", "regulatory clarity", "pro-crypto", "adoption", "reserve", "institutional accumulation"):
            return "regulation", "bullish", "high", 0.80
        if _has(text, "cpi", "inflation", "fed", "rate hike", "yields", "dollar", "jobs", "recession", "risk-off"):
            direction = "bearish" if _has(text, "rate hike", "risk-off", "recession", "hot cpi", "higher yields") else "mixed"
            return "macro", direction, "medium", 0.68
        if _has(text, "rate cut", "risk-on", "liquidity", "soft landing"):
            return "macro", "bullish", "medium", 0.66
        if _has(text, "listing", "airdrop", "partnership", "protocol upgrade", "official statement", "founder statement", "ceo statement"):
            return "official_project_news", "bullish", "medium", 0.64
        if _has(text, "tweet", "elon", "cz", "vitalik", "ripple ceo", "sec chair", "fed chair"):
            return "influencer_social", "mixed", "low", 0.45
        if _has(text, "depeg", "insolvency", "bank run", "reserve concern"):
            return "stablecoin_depeg", "bearish", "critical", 0.82
        return "other", "unknown", "low", 0.35


def _has(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def _detect_symbols(text: str, symbols_hint: list[str] | None = None) -> list[str]:
    found = {str(symbol).upper() for symbol in symbols_hint or [] if symbol}
    upper = text.upper()
    for alias, symbol in SYMBOL_ALIASES.items():
        if re.search(rf"(?<![A-Z0-9]){re.escape(alias)}(USDT)?(?![A-Z0-9])", upper):
            found.add(symbol)
    if not found and _has(text, "crypto", "market", "fed", "risk-on", "risk-off", "cpi", "inflation"):
        found.add("GLOBAL")
    return sorted(found) if found else ["GLOBAL"]


def _regimes_for(category: str, direction: str) -> list[str]:
    if category in {"macro", "regulation"} and direction == "bullish":
        return ["RISK_ON", "TREND_UP"]
    if direction == "bearish":
        return ["RISK_OFF", "TREND_DOWN"]
    return []


def _window_hours(severity: str) -> int:
    if severity in {"critical", "high"}:
        return 72
    if severity == "medium":
        return 24
    return 12


def _parse_dt(value: datetime | str | None) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _stable_id(title: str, published: str, symbols: str, source_url: str) -> str:
    base = f"{title}|{published[:13]}|{symbols}|{source_url}"
    return "cat_" + hashlib.sha256(base.encode("utf-8")).hexdigest()[:20]
