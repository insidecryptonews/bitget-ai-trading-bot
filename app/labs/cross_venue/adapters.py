"""Public WebSocket adapters for the Tier-1 cross-venue collectors.

Only official public market-data URLs are reachable from this module.  The
adapter contract is intentionally narrow and contains no authentication or
execution method.
"""

from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from . import POLICY_VERSION, safety_envelope
from .models import CanonicalEvent, canonical_symbol, finite, integer, receive_clock
from .providers import PUBLIC_WS_ENDPOINTS, assert_public_ws_url


def _first_level(value: Any) -> tuple[float | None, float | None]:
    if not isinstance(value, list) or not value:
        return None, None
    level = value[0]
    if isinstance(level, dict):
        return finite(level.get("px") or level.get("price")), finite(level.get("sz") or level.get("size"))
    if isinstance(level, (list, tuple)) and level:
        return finite(level[0]), finite(level[1] if len(level) > 1 else None)
    return None, None


def _quote_asset(venue: str, source_symbol: str) -> str:
    if venue == "hyperliquid":
        return "USD"
    symbol = str(source_symbol).upper().replace("-", "")
    return "USDT" if symbol.endswith("USDT") or venue == "okx" else "USD"


def _event(
    venue: str,
    source_symbol: str,
    event_type: str,
    *,
    clock: tuple[str, int, int],
    connection_id: str,
    reconnect_count: int,
    product_type: str = "LINEAR_PERPETUAL",
    **values: Any,
) -> CanonicalEvent:
    wall_iso, wall_ms, mono_ns = clock
    return CanonicalEvent(
        venue=venue,
        symbol=str(source_symbol),
        canonical_symbol=canonical_symbol(source_symbol),
        product_type=product_type,
        quote_asset=_quote_asset(venue, source_symbol),
        event_type=event_type,
        exchange_event_ts=integer(values.pop("exchange_event_ts", None)),
        exchange_publish_ts=integer(values.pop("exchange_publish_ts", None)),
        local_receive_wall_ts=wall_iso,
        local_receive_wall_ms=wall_ms,
        local_receive_monotonic_ns=mono_ns,
        local_wall_minus_monotonic_ms=wall_ms - mono_ns / 1_000_000.0,
        connection_id=connection_id,
        reconnect_count=reconnect_count,
        **values,
    )


class PublicVenueAdapter(ABC):
    venue: str
    timeout_seconds = 8
    max_message_bytes = 2_000_000

    def __init__(self, symbols: list[str]):
        self.symbols = [canonical_symbol(item) for item in symbols]
        self.connection_id = f"{self.venue}_{uuid.uuid4().hex[:16]}"
        self.reconnect_count = 0
        self.connected = False
        self.last_event_monotonic_ns: int | None = None
        self.last_error: str | None = None
        self.messages = 0
        self.normalized_events = 0
        self.gaps = 0
        self.duplicates = 0
        self.sequence_regressions = 0
        self._last_sequence: dict[tuple[str, str], int] = {}
        self._socket: Any = None

    @property
    def url(self) -> str:
        return PUBLIC_WS_ENDPOINTS[self.venue]

    def connect(self, connector: Callable[..., Any] | None = None) -> Any:
        url = assert_public_ws_url(self.venue, self.connection_url())
        if connector is None:
            import websocket  # type: ignore

            connector = websocket.create_connection
        self._socket = connector(url, timeout=self.timeout_seconds, header=[])
        self.connected = True
        self.last_error = None
        return self._socket

    def connection_url(self) -> str:
        return self.url

    def subscribe(self) -> list[dict[str, Any]]:
        if self._socket is None:
            raise RuntimeError("CROSS_VENUE_NOT_CONNECTED")
        messages = self.subscription_messages()
        for message in messages:
            self._socket.send(json.dumps(message, separators=(",", ":")))
        return messages

    def receive(self) -> Any:
        if self._socket is None:
            raise RuntimeError("CROSS_VENUE_NOT_CONNECTED")
        raw = self._socket.recv()
        self.messages += 1
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not raw:
            return None
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > self.max_message_bytes:
            raise ValueError("CROSS_VENUE_REMOTE_FRAME_SIZE_BLOCKED")
        if raw in {"pong", "ping"}:
            if raw == "ping":
                self._socket.send("pong")
            return {"control": raw}
        return json.loads(
            raw,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"CROSS_VENUE_REMOTE_NON_FINITE_BLOCKED:{value}")
            ),
        )

    def reconnect(self) -> None:
        self.close()
        self.reconnect_count += 1
        self.connection_id = f"{self.venue}_{uuid.uuid4().hex[:16]}"
        self._last_sequence.clear()

    def close(self) -> None:
        sock, self._socket = self._socket, None
        self.connected = False
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def health(self, *, now_monotonic_ns: int | None = None, stale_after_ms: int = 5000) -> dict[str, Any]:
        now = now_monotonic_ns if now_monotonic_ns is not None else time.monotonic_ns()
        age_ms = None if self.last_event_monotonic_ns is None else max(0.0, (now - self.last_event_monotonic_ns) / 1_000_000)
        if self.last_error:
            status = "ERROR" if self.normalized_events == 0 else "DEGRADED"
        elif not self.connected:
            status = "CONNECTING" if self.normalized_events == 0 else "STALE"
        elif age_ms is None:
            status = "CONNECTING"
        elif age_ms > stale_after_ms:
            status = "STALE"
        else:
            status = "HEALTHY"
        return {
            "component": f"CROSS_VENUE_{self.venue.upper()}", "venue": self.venue,
            "status": status, "connected": self.connected, "messages": self.messages,
            "normalized_events": self.normalized_events, "last_event_age_ms": age_ms,
            "reconnect_count": self.reconnect_count, "gaps": self.gaps,
            "duplicates": self.duplicates, "last_error": self.last_error,
            "sequence_regressions": self.sequence_regressions,
            "sequence_check_status": "MONOTONIC_REGRESSION_ONLY_CHANNEL_CONTRACT_VARIES",
            "connection_id": self.connection_id, **safety_envelope(),
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "venue": self.venue, "symbols": self.symbols,
            "public_websocket": True, "authentication": False,
            "event_types": self.event_types(), "policy_version": POLICY_VERSION,
            **safety_envelope(),
        }

    def provenance(self) -> dict[str, Any]:
        return {
            "venue": self.venue, "official_public_ws": self.url,
            "connection_id": self.connection_id, "policy_version": POLICY_VERSION,
            **safety_envelope(),
        }

    def normalize(self, frame: Any, *, clock: tuple[str, int, int] | None = None) -> list[dict[str, Any]]:
        if not isinstance(frame, dict) or frame.get("control"):
            return []
        clock = clock or receive_clock()
        rows = [event.to_dict() for event in self._normalize(frame, clock=clock)]
        for row in rows:
            sequence = integer(row.get("sequence_id"))
            if sequence is None:
                continue
            key = (str(row.get("canonical_symbol") or ""), str(row.get("event_type") or ""))
            previous = self._last_sequence.get(key)
            if previous is not None and sequence < previous:
                self.sequence_regressions += 1
                self.gaps += 1
                row["source_status"] = "SEQUENCE_REGRESSION_OBSERVED"
            self._last_sequence[key] = max(sequence, previous or sequence)
        if rows:
            self.last_event_monotonic_ns = clock[2]
            self.normalized_events += len(rows)
        return rows

    @abstractmethod
    def subscription_messages(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def event_types(self) -> list[str]: ...

    @abstractmethod
    def _normalize(self, frame: dict[str, Any], *, clock: tuple[str, int, int]) -> list[CanonicalEvent]: ...


class BitgetAdapter(PublicVenueAdapter):
    venue = "bitget"

    def subscription_messages(self) -> list[dict[str, Any]]:
        args = [
            {"instType": "USDT-FUTURES", "channel": channel, "instId": symbol}
            for symbol in self.symbols for channel in ("trade", "books1", "ticker")
        ]
        return [{"op": "subscribe", "args": args}]

    def event_types(self) -> list[str]:
        return ["trade", "book_l1", "ticker"]

    def _normalize(self, frame: dict[str, Any], *, clock: tuple[str, int, int]) -> list[CanonicalEvent]:
        arg = frame.get("arg") if isinstance(frame.get("arg"), dict) else {}
        channel, symbol = str(arg.get("channel") or ""), str(arg.get("instId") or "")
        data = frame.get("data") if isinstance(frame.get("data"), list) else []
        rows: list[CanonicalEvent] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            ts = item.get("ts") or frame.get("ts")
            common = dict(clock=clock, connection_id=self.connection_id, reconnect_count=self.reconnect_count,
                          exchange_event_ts=ts, exchange_publish_ts=frame.get("ts"), raw_schema=f"bitget.{channel}.v2")
            if channel == "trade":
                rows.append(_event(self.venue, symbol, "trade", price=finite(item.get("price")),
                                   size=finite(item.get("size")), taker_side=str(item.get("side") or "").upper() or None,
                                   trade_id=str(item.get("tradeId") or "") or None,
                                   sequence_id=str(item.get("seq") or "") or None, **common))
            elif channel == "books1":
                bid, bid_size = _first_level(item.get("bids")); ask, ask_size = _first_level(item.get("asks"))
                rows.append(_event(self.venue, symbol, "book_l1", best_bid=bid, best_ask=ask,
                                   bid_size=bid_size, ask_size=ask_size,
                                   sequence_id=str(item.get("seq") or item.get("checksum") or "") or None,
                                   snapshot_kind=str(frame.get("action") or "snapshot").upper(), **common))
            elif channel == "ticker":
                rows.append(_event(self.venue, symbol, "ticker", price=finite(item.get("lastPr")),
                                   best_bid=finite(item.get("bidPr")), best_ask=finite(item.get("askPr")),
                                   bid_size=finite(item.get("bidSz")), ask_size=finite(item.get("askSz")),
                                   mark_price=finite(item.get("markPrice")), index_price=finite(item.get("indexPrice")),
                                   funding_rate=finite(item.get("fundingRate")), open_interest=finite(item.get("holdingAmount")),
                                   **common))
        return rows


class BinanceAdapter(PublicVenueAdapter):
    venue = "binance"

    def connection_url(self) -> str:
        streams = [f"{symbol.lower()}@{stream}" for symbol in self.symbols
                   for stream in ("aggTrade", "bookTicker", "markPrice@1s")]
        return f"{self.url}?streams={'/'.join(streams)}"

    def subscription_messages(self) -> list[dict[str, Any]]:
        return []

    def event_types(self) -> list[str]:
        return ["trade", "book_l1", "mark_index_funding"]

    def _normalize(self, frame: dict[str, Any], *, clock: tuple[str, int, int]) -> list[CanonicalEvent]:
        item = frame.get("data") if isinstance(frame.get("data"), dict) else frame
        kind, symbol = str(item.get("e") or ""), str(item.get("s") or "")
        common = dict(clock=clock, connection_id=self.connection_id, reconnect_count=self.reconnect_count,
                      exchange_event_ts=item.get("T") or item.get("E"), exchange_publish_ts=item.get("E"),
                      sequence_id=str(item.get("u") or item.get("a") or "") or None,
                      raw_schema=f"binance.{kind}.usdm")
        if kind == "aggTrade":
            return [_event(self.venue, symbol, "trade", price=finite(item.get("p")), size=finite(item.get("q")),
                           taker_side="SELL" if item.get("m") is True else "BUY",
                           trade_id=str(item.get("a") or "") or None, **common)]
        if kind == "bookTicker":
            return [_event(self.venue, symbol, "book_l1", best_bid=finite(item.get("b")), best_ask=finite(item.get("a")),
                           bid_size=finite(item.get("B")), ask_size=finite(item.get("A")),
                           snapshot_kind="ABSOLUTE_L1", **common)]
        if kind == "markPriceUpdate":
            return [_event(self.venue, symbol, "mark_index_funding", mark_price=finite(item.get("p")),
                           index_price=finite(item.get("i")), funding_rate=finite(item.get("r")), **common)]
        return []


class BybitAdapter(PublicVenueAdapter):
    venue = "bybit"

    def subscription_messages(self) -> list[dict[str, Any]]:
        args = [topic for symbol in self.symbols for topic in
                (f"publicTrade.{symbol}", f"orderbook.1.{symbol}", f"tickers.{symbol}")]
        return [{"op": "subscribe", "args": args}]

    def event_types(self) -> list[str]:
        return ["trade", "book_l1", "ticker"]

    def _normalize(self, frame: dict[str, Any], *, clock: tuple[str, int, int]) -> list[CanonicalEvent]:
        topic = str(frame.get("topic") or "")
        data = frame.get("data")
        rows: list[CanonicalEvent] = []
        if topic.startswith("publicTrade.") and isinstance(data, list):
            for item in data:
                if not isinstance(item, dict): continue
                rows.append(_event(self.venue, item.get("s") or topic.rsplit(".", 1)[-1], "trade",
                                   clock=clock, connection_id=self.connection_id, reconnect_count=self.reconnect_count,
                                   exchange_event_ts=item.get("T"), exchange_publish_ts=frame.get("ts"),
                                   sequence_id=str(frame.get("seq") or "") or None, trade_id=str(item.get("i") or "") or None,
                                   price=finite(item.get("p")), size=finite(item.get("v")),
                                   taker_side=str(item.get("S") or "").upper() or None, raw_schema="bybit.publicTrade.v5"))
        elif topic.startswith("orderbook.1.") and isinstance(data, dict):
            bid, bid_size = _first_level(data.get("b")); ask, ask_size = _first_level(data.get("a"))
            rows.append(_event(self.venue, data.get("s") or topic.rsplit(".", 1)[-1], "book_l1",
                               clock=clock, connection_id=self.connection_id, reconnect_count=self.reconnect_count,
                               exchange_event_ts=data.get("cts") or frame.get("ts"), exchange_publish_ts=frame.get("ts"),
                               sequence_id=str(data.get("seq") or data.get("u") or "") or None,
                               best_bid=bid, best_ask=ask, bid_size=bid_size, ask_size=ask_size,
                               snapshot_kind=str(frame.get("type") or "snapshot").upper(), raw_schema="bybit.orderbook.1.v5"))
        elif topic.startswith("tickers.") and isinstance(data, dict):
            rows.append(_event(self.venue, data.get("symbol") or topic.rsplit(".", 1)[-1], "ticker",
                               clock=clock, connection_id=self.connection_id, reconnect_count=self.reconnect_count,
                               exchange_event_ts=frame.get("cs") or frame.get("ts"), exchange_publish_ts=frame.get("ts"),
                               price=finite(data.get("lastPrice")), best_bid=finite(data.get("bid1Price")),
                               best_ask=finite(data.get("ask1Price")), bid_size=finite(data.get("bid1Size")),
                               ask_size=finite(data.get("ask1Size")), mark_price=finite(data.get("markPrice")),
                               index_price=finite(data.get("indexPrice")), funding_rate=finite(data.get("fundingRate")),
                               open_interest=finite(data.get("openInterest")), raw_schema="bybit.tickers.v5"))
        return rows


class OkxAdapter(PublicVenueAdapter):
    venue = "okx"

    @staticmethod
    def source_symbol(symbol: str) -> str:
        return f"{symbol[:-4]}-USDT-SWAP" if symbol.endswith("USDT") else symbol

    def subscription_messages(self) -> list[dict[str, Any]]:
        args = [{"channel": channel, "instId": self.source_symbol(symbol)}
                for symbol in self.symbols
                for channel in ("trades", "books5", "tickers", "mark-price", "open-interest", "funding-rate")]
        return [{"op": "subscribe", "args": args}]

    def event_types(self) -> list[str]:
        return ["trade", "book_l1", "ticker", "mark_price", "open_interest", "funding"]

    def _normalize(self, frame: dict[str, Any], *, clock: tuple[str, int, int]) -> list[CanonicalEvent]:
        arg = frame.get("arg") if isinstance(frame.get("arg"), dict) else {}
        channel = str(arg.get("channel") or ""); source = str(arg.get("instId") or "")
        symbol = source.replace("-USDT-SWAP", "USDT")
        data = frame.get("data") if isinstance(frame.get("data"), list) else []
        rows: list[CanonicalEvent] = []
        for item in data:
            if not isinstance(item, dict): continue
            common = dict(clock=clock, connection_id=self.connection_id, reconnect_count=self.reconnect_count,
                          exchange_event_ts=item.get("ts"), exchange_publish_ts=frame.get("ts"),
                          sequence_id=str(item.get("seqId") or item.get("tradeId") or "") or None,
                          raw_schema=f"okx.{channel}.v5")
            if channel == "trades":
                rows.append(_event(self.venue, symbol, "trade", price=finite(item.get("px")), size=finite(item.get("sz")),
                                   taker_side=str(item.get("side") or "").upper() or None,
                                   trade_id=str(item.get("tradeId") or "") or None, **common))
            elif channel == "books5":
                bid, bid_size = _first_level(item.get("bids")); ask, ask_size = _first_level(item.get("asks"))
                rows.append(_event(self.venue, symbol, "book_l1", best_bid=bid, best_ask=ask,
                                   bid_size=bid_size, ask_size=ask_size,
                                   snapshot_kind=str(frame.get("action") or "snapshot").upper(), **common))
            elif channel == "tickers":
                rows.append(_event(self.venue, symbol, "ticker", price=finite(item.get("last")),
                                   best_bid=finite(item.get("bidPx")), best_ask=finite(item.get("askPx")),
                                   bid_size=finite(item.get("bidSz")), ask_size=finite(item.get("askSz")), **common))
            elif channel == "mark-price":
                rows.append(_event(self.venue, symbol, "mark_price", mark_price=finite(item.get("markPx")), **common))
            elif channel == "open-interest":
                rows.append(_event(self.venue, symbol, "open_interest", open_interest=finite(item.get("oi")), **common))
            elif channel == "funding-rate":
                rows.append(_event(self.venue, symbol, "funding", funding_rate=finite(item.get("fundingRate")), **common))
        return rows


class HyperliquidAdapter(PublicVenueAdapter):
    venue = "hyperliquid"

    @staticmethod
    def coin(symbol: str) -> str:
        return symbol[:-4] if symbol.endswith("USDT") else symbol

    def subscription_messages(self) -> list[dict[str, Any]]:
        return [{"method": "subscribe", "subscription": {"type": kind, "coin": self.coin(symbol)}}
                for symbol in self.symbols for kind in ("trades", "bbo", "activeAssetCtx")]

    def event_types(self) -> list[str]:
        return ["trade", "book_l1", "asset_context"]

    def _normalize(self, frame: dict[str, Any], *, clock: tuple[str, int, int]) -> list[CanonicalEvent]:
        channel, data = str(frame.get("channel") or ""), frame.get("data")
        rows: list[CanonicalEvent] = []
        if channel == "trades" and isinstance(data, list):
            for item in data:
                if not isinstance(item, dict): continue
                rows.append(_event(self.venue, item.get("coin"), "trade", product_type="OTHER_PERPETUAL",
                                   clock=clock, connection_id=self.connection_id, reconnect_count=self.reconnect_count,
                                   exchange_event_ts=item.get("time"), exchange_publish_ts=item.get("time"),
                                   trade_id=str(item.get("tid") or item.get("hash") or "") or None,
                                   price=finite(item.get("px")), size=finite(item.get("sz")),
                                   taker_side="BUY" if item.get("side") == "B" else "SELL" if item.get("side") == "A" else None,
                                   raw_schema="hyperliquid.trades.v1"))
        elif channel == "bbo" and isinstance(data, dict):
            bid, bid_size = _first_level((data.get("bbo") or [None, None])[:1])
            ask, ask_size = _first_level((data.get("bbo") or [None, None])[1:2])
            rows.append(_event(self.venue, data.get("coin"), "book_l1", product_type="OTHER_PERPETUAL",
                               clock=clock, connection_id=self.connection_id, reconnect_count=self.reconnect_count,
                               exchange_event_ts=data.get("time"), exchange_publish_ts=data.get("time"),
                               best_bid=bid, best_ask=ask, bid_size=bid_size, ask_size=ask_size,
                               snapshot_kind="ABSOLUTE_L1", raw_schema="hyperliquid.bbo.v1"))
        elif channel == "activeAssetCtx" and isinstance(data, dict):
            ctx = data.get("ctx") if isinstance(data.get("ctx"), dict) else data
            rows.append(_event(self.venue, data.get("coin"), "asset_context", product_type="OTHER_PERPETUAL",
                               clock=clock, connection_id=self.connection_id, reconnect_count=self.reconnect_count,
                               exchange_event_ts=data.get("time"), exchange_publish_ts=data.get("time"),
                               mark_price=finite(ctx.get("markPx")), index_price=finite(ctx.get("oraclePx")),
                               funding_rate=finite(ctx.get("funding")), open_interest=finite(ctx.get("openInterest")),
                               raw_schema="hyperliquid.activeAssetCtx.v1"))
        return rows


ADAPTERS = {
    "bitget": BitgetAdapter,
    "binance": BinanceAdapter,
    "bybit": BybitAdapter,
    "okx": OkxAdapter,
    "hyperliquid": HyperliquidAdapter,
}


def make_adapter(venue: str, symbols: list[str]) -> PublicVenueAdapter:
    key = str(venue).lower()
    if key not in ADAPTERS:
        raise ValueError(f"CROSS_VENUE_UNSUPPORTED_VENUE:{key}")
    return ADAPTERS[key](symbols)
