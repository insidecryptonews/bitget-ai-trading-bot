"""Exact-allowlist public Bitget market data for the ATI simulator.

No authentication headers, credentials, private clients or mutation endpoints
exist in this module. The paper broker itself has no network dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from .config import InstrumentRule

BASE_URL = "https://api.bitget.com"
ALLOWED_PATHS = {
    "/api/v2/mix/market/ticker",
    "/api/v2/mix/market/candles",
    "/api/v2/mix/market/contracts",
}
FORBIDDEN_PARAM_NAMES = {
    "signature", "apikey", "api_key", "secret", "token", "passphrase",
    "recvwindow", "access_key", "x-mbx-apikey",
}
FORBIDDEN_HEADER_NAMES = {
    "authorization", "access-key", "access-sign", "access-passphrase",
    "access-timestamp", "x-mbx-apikey", "api-key", "apikey", "signature",
}


class AtiPublicMarketError(RuntimeError):
    pass


@dataclass(frozen=True)
class MarketTick:
    symbol: str
    price: float
    source_ts_ms: int
    observed_at: str


@dataclass(frozen=True)
class MarketBar:
    symbol: str
    timestamp_ms: int
    available_at_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def _number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AtiPublicMarketError(f"PUBLIC_MARKET_INVALID_{label}") from exc
    if not math.isfinite(number) or number <= 0:
        raise AtiPublicMarketError(f"PUBLIC_MARKET_INVALID_{label}")
    return number


def _assert_public_get(url: str, params: dict[str, Any], headers: dict[str, str] | None = None) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "api.bitget.com" or parsed.path not in ALLOWED_PATHS:
        raise AtiPublicMarketError("PUBLIC_MARKET_ENDPOINT_BLOCKED")
    sensitive = {str(key).lower() for key in params} & FORBIDDEN_PARAM_NAMES
    if sensitive:
        raise AtiPublicMarketError("PUBLIC_MARKET_SENSITIVE_PARAM_BLOCKED")
    for key in (headers or {}):
        lowered = str(key).lower()
        if (
            lowered in FORBIDDEN_HEADER_NAMES
            or "auth" in lowered or "api" in lowered
            or "sign" in lowered or "token" in lowered
        ):
            raise AtiPublicMarketError("PUBLIC_MARKET_AUTH_HEADER_BLOCKED")


class BitgetPublicMarket:
    def __init__(self, *, timeout_seconds: float = 10.0, session: Any | None = None):
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.session = session or requests.Session()

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = BASE_URL + path
        _assert_public_get(url, params, {})
        response = self.session.get(url, params=params, timeout=self.timeout_seconds, headers={})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or str(payload.get("code")) != "00000":
            raise AtiPublicMarketError(f"PUBLIC_MARKET_RESPONSE_ERROR:{str(payload)[:160]}")
        return payload

    def ticker(self, symbol: str) -> MarketTick:
        symbol = str(symbol).upper()
        payload = self._get("/api/v2/mix/market/ticker", {
            "symbol": symbol, "productType": "USDT-FUTURES",
        })
        data = payload.get("data") or []
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise AtiPublicMarketError("PUBLIC_MARKET_TICKER_EMPTY")
        row = data[0]
        price = _number(row.get("lastPr"), "TICKER_PRICE")
        try:
            source_ts = int(row.get("ts") or payload.get("requestTime"))
        except (TypeError, ValueError) as exc:
            raise AtiPublicMarketError("PUBLIC_MARKET_TICKER_TIMESTAMP") from exc
        return MarketTick(
            symbol=symbol, price=price, source_ts_ms=source_ts,
            observed_at=datetime.now(timezone.utc).isoformat(),
        )

    def closed_bars(self, symbol: str, *, after_ms: int | None = None,
                    now_ms: int | None = None, limit: int = 1000) -> list[MarketBar]:
        symbol = str(symbol).upper()
        params: dict[str, Any] = {
            "symbol": symbol, "productType": "USDT-FUTURES",
            "granularity": "1m", "limit": max(10, min(1000, int(limit))),
        }
        if after_ms is not None:
            params["startTime"] = int(after_ms) + 1
        if now_ms is not None:
            params["endTime"] = int(now_ms)
        payload = self._get("/api/v2/mix/market/candles", params)
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            raise AtiPublicMarketError("PUBLIC_MARKET_CANDLES_INVALID")
        cutoff = int(now_ms or payload.get("requestTime") or datetime.now(timezone.utc).timestamp() * 1000)
        parsed: dict[int, MarketBar] = {}
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                raise AtiPublicMarketError("PUBLIC_MARKET_CANDLE_SHAPE")
            try:
                ts = int(row[0])
            except (TypeError, ValueError) as exc:
                raise AtiPublicMarketError("PUBLIC_MARKET_CANDLE_TIMESTAMP") from exc
            available = ts + 60_000
            if available > cutoff or (after_ms is not None and ts <= int(after_ms)):
                continue
            bar = MarketBar(
                symbol=symbol, timestamp_ms=ts, available_at_ms=available,
                open=_number(row[1], "BAR_OPEN"), high=_number(row[2], "BAR_HIGH"),
                low=_number(row[3], "BAR_LOW"), close=_number(row[4], "BAR_CLOSE"),
                volume=max(0.0, float(row[5] or 0.0)),
            )
            if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close) or bar.low > bar.high:
                raise AtiPublicMarketError("PUBLIC_MARKET_CANDLE_OHLC_INVALID")
            parsed[ts] = bar
        return [parsed[key] for key in sorted(parsed)]

    def instrument_rule(self, symbol: str) -> InstrumentRule:
        symbol = str(symbol).upper()
        payload = self._get("/api/v2/mix/market/contracts", {
            "symbol": symbol, "productType": "USDT-FUTURES",
        })
        rows = payload.get("data") or []
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise AtiPublicMarketError("PUBLIC_MARKET_CONTRACT_EMPTY")
        row = rows[0]
        if str(row.get("symbol") or "").upper() != symbol:
            raise AtiPublicMarketError("PUBLIC_MARKET_CONTRACT_SYMBOL_MISMATCH")
        return InstrumentRule(
            symbol=symbol,
            min_trade_num=_number(row.get("minTradeNum"), "MIN_TRADE_NUM"),
            min_trade_usdt=_number(row.get("minTradeUSDT"), "MIN_TRADE_USDT"),
            quantity_step=_number(row.get("sizeMultiplier") or row.get("minTradeNum"), "QUANTITY_STEP"),
            volume_place=int(row.get("volumePlace")),
            price_place=int(row.get("pricePlace")),
            source="BITGET_PUBLIC_CONTRACT_RUNTIME",
        )
