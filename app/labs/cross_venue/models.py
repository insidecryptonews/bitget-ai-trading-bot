"""Canonical public-market event contract and defensive conversions."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def integer(value: Any) -> int | None:
    number = finite(value)
    return int(number) if number is not None else None


def canonical_symbol(value: str) -> str:
    text = str(value or "").upper().replace("-", "").replace("_", "")
    aliases = {
        "BTC": "BTCUSDT", "ETH": "ETHUSDT", "XRP": "XRPUSDT",
        "DOGE": "DOGEUSDT", "XBTUSDT": "BTCUSDT",
    }
    return aliases.get(text, text)


def utc_iso_from_ms(value: int | None) -> str | None:
    if value is None or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(slots=True)
class CanonicalEvent:
    venue: str
    symbol: str
    canonical_symbol: str
    product_type: str
    quote_asset: str
    event_type: str
    exchange_event_ts: int | None
    exchange_publish_ts: int | None
    local_receive_wall_ts: str
    local_receive_wall_ms: int
    local_receive_monotonic_ns: int
    local_wall_minus_monotonic_ms: float | None = None
    sequence_id: str | None = None
    trade_id: str | None = None
    price: float | None = None
    size: float | None = None
    taker_side: str | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    mark_price: float | None = None
    index_price: float | None = None
    funding_rate: float | None = None
    open_interest: float | None = None
    connection_id: str | None = None
    reconnect_count: int = 0
    source_status: str = "OK"
    snapshot_kind: str | None = None
    raw_schema: str | None = None
    event_id: str | None = None

    def validate(self) -> None:
        if not self.venue or not self.canonical_symbol or not self.event_type:
            raise ValueError("CROSS_VENUE_EVENT_IDENTITY_MISSING")
        if self.product_type not in {"LINEAR_PERPETUAL", "SPOT", "OTHER_PERPETUAL"}:
            raise ValueError("CROSS_VENUE_PRODUCT_TYPE_INVALID")
        for label in (
            "price", "size", "best_bid", "best_ask", "bid_size", "ask_size",
            "mark_price", "index_price", "funding_rate", "open_interest",
        ):
            value = getattr(self, label)
            if value is not None and not math.isfinite(float(value)):
                raise ValueError(f"CROSS_VENUE_NON_FINITE:{label}")
        for label in ("price", "size", "best_bid", "best_ask", "bid_size", "ask_size"):
            value = getattr(self, label)
            if value is not None and value < 0:
                raise ValueError(f"CROSS_VENUE_NEGATIVE:{label}")
        if self.best_bid is not None and self.best_ask is not None and self.best_bid > self.best_ask:
            raise ValueError("CROSS_VENUE_CROSSED_BOOK")
        if self.taker_side not in {None, "BUY", "SELL"}:
            raise ValueError("CROSS_VENUE_TAKER_SIDE_INVALID")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        payload = asdict(self)
        if not payload.get("event_id"):
            identity = {
                key: payload.get(key) for key in (
                    "venue", "canonical_symbol", "event_type", "exchange_event_ts",
                    "sequence_id", "trade_id", "price", "size", "best_bid", "best_ask",
                )
            }
            # Exchange identity must survive reconnects.  The local receive clock
            # is only a fallback for feeds that provide no source identity at all.
            if not any(identity.get(key) not in (None, "") for key in
                       ("exchange_event_ts", "sequence_id", "trade_id")):
                identity["local_receive_monotonic_ns"] = payload.get("local_receive_monotonic_ns")
            digest = hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:32]
            payload["event_id"] = f"cve_{digest}"
            self.event_id = payload["event_id"]
        return payload


def receive_clock() -> tuple[str, int, int]:
    wall_ms = time.time_ns() // 1_000_000
    return utc_iso_from_ms(wall_ms) or datetime.now(timezone.utc).isoformat(), wall_ms, time.monotonic_ns()


def midpoint(event: dict[str, Any]) -> float | None:
    bid = finite(event.get("best_bid"))
    ask = finite(event.get("best_ask"))
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        return (bid + ask) / 2.0
    return finite(event.get("price")) or finite(event.get("mark_price"))


def comparable_to_bitget(event: dict[str, Any]) -> bool:
    return (
        event.get("product_type") == "LINEAR_PERPETUAL"
        and event.get("quote_asset") == "USDT"
        and str(event.get("canonical_symbol") or "").endswith("USDT")
    )
