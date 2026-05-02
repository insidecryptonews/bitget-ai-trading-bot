from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .utils import decimal_quantize, round_to_places, safe_float, safe_int


@dataclass(frozen=True)
class InstrumentRules:
    symbol: str
    min_trade_num: float
    min_trade_usdt: float
    size_multiplier: float
    volume_place: int
    price_place: int
    price_end_step: float
    min_leverage: int
    max_leverage: int
    maker_fee_rate: float
    taker_fee_rate: float
    symbol_status: str
    max_market_order_qty: float
    max_order_qty: float

    @property
    def is_active(self) -> bool:
        return self.symbol_status.lower() == "normal"

    @classmethod
    def from_bitget_contract(cls, contract: dict[str, Any]) -> "InstrumentRules":
        return cls(
            symbol=str(contract.get("symbol", "")).upper(),
            min_trade_num=safe_float(contract.get("minTradeNum"), 0.0),
            min_trade_usdt=safe_float(contract.get("minTradeUSDT"), 0.0),
            size_multiplier=safe_float(contract.get("sizeMultiplier"), safe_float(contract.get("minTradeNum"), 0.001)),
            volume_place=safe_int(contract.get("volumePlace"), 4),
            price_place=safe_int(contract.get("pricePlace"), 2),
            price_end_step=safe_float(contract.get("priceEndStep"), 0.0),
            min_leverage=safe_int(contract.get("minLever"), 1),
            max_leverage=safe_int(contract.get("maxLever"), 1),
            maker_fee_rate=safe_float(contract.get("makerFeeRate"), 0.0004),
            taker_fee_rate=safe_float(contract.get("takerFeeRate"), 0.0006),
            symbol_status=str(contract.get("symbolStatus", "off")),
            max_market_order_qty=safe_float(contract.get("maxMarketOrderQty"), 0.0),
            max_order_qty=safe_float(contract.get("maxOrderQty"), 0.0),
        )


class OrderManager:
    def __init__(self, instruments: dict[str, InstrumentRules]) -> None:
        self.instruments = instruments

    def get_rules(self, symbol: str) -> InstrumentRules | None:
        return self.instruments.get(symbol.upper())

    @staticmethod
    def round_price(price: float, rules: InstrumentRules) -> float:
        return float(round_to_places(price, rules.price_place, "down"))

    @staticmethod
    def round_size(size: float, rules: InstrumentRules, mode: str = "down") -> float:
        by_step = decimal_quantize(size, rules.size_multiplier or 1, mode)
        return float(round_to_places(by_step, rules.volume_place, mode))

    def validate_order(
        self,
        symbol: str,
        size: float,
        entry_price: float,
        leverage: int,
        order_type: str = "market",
    ) -> tuple[bool, list[str]]:
        warnings: list[str] = []
        rules = self.get_rules(symbol)
        if not rules:
            return False, [f"{symbol}: reglas de instrumento no disponibles"]
        if not rules.is_active:
            return False, [f"{symbol}: instrumento no activo ({rules.symbol_status})"]
        if size < rules.min_trade_num:
            warnings.append(f"size {size} < minTradeNum {rules.min_trade_num}")
        if size * entry_price < rules.min_trade_usdt:
            warnings.append(f"notional {size * entry_price:.4f} < minTradeUSDT {rules.min_trade_usdt}")
        if leverage < rules.min_leverage or leverage > rules.max_leverage:
            warnings.append(f"leverage {leverage} fuera de rango {rules.min_leverage}-{rules.max_leverage}")
        limit_qty = rules.max_market_order_qty if order_type == "market" else rules.max_order_qty
        if limit_qty and size > limit_qty:
            warnings.append(f"size {size} supera max qty {limit_qty}")
        return not warnings, warnings

