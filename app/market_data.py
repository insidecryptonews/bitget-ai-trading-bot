from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .bitget_client import BitgetClient
from .config import BotConfig
from .indicators import add_indicators
from .utils import safe_float


@dataclass
class MarketSnapshot:
    symbol: str
    ticker: dict[str, Any] = field(default_factory=dict)
    candles: dict[str, pd.DataFrame] = field(default_factory=dict)
    current_price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    spread_pct: float = 0.0
    volume_24h_usdt: float = 0.0
    change_24h: float = 0.0
    funding_rate: float = 0.0
    open_interest: float = 0.0
    error: str = ""


class MarketDataProvider:
    def __init__(self, config: BotConfig, client: BitgetClient, logger) -> None:
        self.config = config
        self.client = client
        self.logger = logger
        self.timeframes = ["1m", config.main_timeframe, config.confirmation_timeframe, config.higher_timeframe, "4H"]

    @staticmethod
    def candles_to_frame(raw: list[list[str]]) -> pd.DataFrame:
        rows = []
        for item in raw:
            if len(item) < 7:
                continue
            rows.append(
                {
                    "timestamp": pd.to_datetime(int(item[0]), unit="ms", utc=True),
                    "open": safe_float(item[1]),
                    "high": safe_float(item[2]),
                    "low": safe_float(item[3]),
                    "close": safe_float(item[4]),
                    "volume": safe_float(item[5]),
                    "quote_volume": safe_float(item[6]),
                }
            )
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        return df.sort_values("timestamp").reset_index(drop=True)

    def fetch_symbol(self, symbol: str, limit: int = 220) -> MarketSnapshot:
        snapshot = MarketSnapshot(symbol=symbol)
        try:
            ticker = self.client.get_ticker(symbol)
            snapshot.ticker = ticker
            snapshot.current_price = safe_float(ticker.get("lastPr") or ticker.get("markPrice"))
            snapshot.bid = safe_float(ticker.get("bidPr"))
            snapshot.ask = safe_float(ticker.get("askPr"))
            mid = (snapshot.bid + snapshot.ask) / 2 if snapshot.bid and snapshot.ask else snapshot.current_price
            snapshot.spread_pct = ((snapshot.ask - snapshot.bid) / mid) if mid and snapshot.ask and snapshot.bid else 0.0
            snapshot.volume_24h_usdt = safe_float(ticker.get("usdtVolume") or ticker.get("quoteVolume"))
            snapshot.change_24h = safe_float(ticker.get("change24h"))
            snapshot.funding_rate = safe_float(ticker.get("fundingRate"))
            snapshot.open_interest = safe_float(ticker.get("holdingAmount"))

            for timeframe in dict.fromkeys(self.timeframes):
                api_timeframe = self._api_timeframe(timeframe)
                raw = self.client.get_candles(symbol, api_timeframe, limit=limit)
                df = self.candles_to_frame(raw)
                if not df.empty:
                    snapshot.candles[timeframe.lower()] = add_indicators(df)
        except Exception as exc:
            snapshot.error = str(exc)
            self.logger.warning("%s falló al descargar mercado: %s", symbol, exc)
        return snapshot

    def fetch_all(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        snapshots: dict[str, MarketSnapshot] = {}
        for symbol in symbols:
            snapshot = self.fetch_symbol(symbol)
            if not snapshot.error:
                snapshots[symbol] = snapshot
        return snapshots

    @staticmethod
    def _api_timeframe(timeframe: str) -> str:
        return {"1h": "1H", "4h": "4H"}.get(timeframe.lower(), timeframe)
