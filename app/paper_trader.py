from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count
from typing import Any

from .config import BotConfig
from .database import Database
from .signal_engine import Signal


@dataclass
class PaperPosition:
    trade_id: int
    symbol: str
    side: str
    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    size: float
    leverage: int
    margin_mode: str = "isolated"
    margin_used: float = 0.0
    notional: float = 0.0
    quantity: float = 0.0
    liquidation_estimate: float = 0.0
    tp1_hit: bool = False
    status: str = "OPEN"
    fees: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class PaperTrader:
    def __init__(self, config: BotConfig, db: Database, telegram, logger) -> None:
        self.config = config
        self.db = db
        self.telegram = telegram
        self.logger = logger
        self.positions: dict[str, PaperPosition] = {}
        self._ids = count(1)

    @property
    def reserved_margin(self) -> float:
        return sum(position.margin_used for position in self.positions.values())

    def open_position(self, signal: Signal, risk_amount: float = 0.0, risk=None) -> PaperPosition:
        if signal.symbol in self.positions:
            raise RuntimeError(f"Paper: ya existe posicion en {signal.symbol}")
        max_positions = (
            self.config.small_account_max_open_positions
            if self.config.starting_capital_usdt < 60
            else self.config.max_open_positions
        )
        if len(self.positions) >= max_positions:
            raise RuntimeError("Paper: maximo de posiciones para cuenta pequena alcanzado")

        notional = float(getattr(risk, "notional", 0.0) or (signal.entry_price * signal.position_size))
        margin_used = float(getattr(risk, "selected_margin_usdt", 0.0) or 0.0)
        if margin_used <= 0:
            margin_used = notional / max(signal.leverage_recommendation, 1)
        total_after = self.reserved_margin + margin_used
        max_total = self.config.starting_capital_usdt * self.config.max_total_margin_usage - float(self.config.margin_safety_buffer_usdt)
        if total_after >= max_total:
            raise RuntimeError("Paper: margen aislado total supera el limite configurado")

        fees = signal.entry_price * signal.position_size * 0.0006
        trade_id = self.db.record_trade(mode="paper", signal=signal, status="PAPER_OPEN", risk_amount=risk_amount)
        position = PaperPosition(
            trade_id=trade_id or next(self._ids),
            symbol=signal.symbol,
            side=signal.side,
            entry=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            size=signal.position_size,
            leverage=signal.leverage_recommendation,
            margin_mode="isolated",
            margin_used=margin_used,
            notional=notional,
            quantity=signal.position_size,
            liquidation_estimate=self._liquidation_estimate(signal, margin_used, notional),
            fees=fees,
            metadata={"strategy": signal.strategy_type, "score": signal.confidence_score},
        )
        self.positions[signal.symbol] = position
        self.logger.info(
            "PAPER OPEN %s %s margin_mode=%s margin_used=%.2f notional=%.2f size=%s entry=%s",
            signal.side,
            signal.symbol,
            position.margin_mode,
            position.margin_used,
            position.notional,
            signal.position_size,
            signal.entry_price,
        )
        self.telegram.trade_opened(signal)
        return position

    def monitor(self, latest_prices: dict[str, float]) -> list[PaperPosition]:
        closed: list[PaperPosition] = []
        for symbol, position in list(self.positions.items()):
            price = latest_prices.get(symbol)
            if not price:
                continue
            pnl = self._unrealized_pnl(position, price)
            if self._hit_stop(position, price):
                self._close(position, price, "STOP_LOSS", pnl)
                closed.append(position)
                continue
            if not position.tp1_hit and self._hit_target(position.side, price, position.take_profit_1):
                position.tp1_hit = True
                position.stop_loss = position.entry
                self.logger.info("PAPER TP1 %s: stop movido a break-even", symbol)
                self.telegram.send(f"PAPER TP1 {symbol}. Stop movido a break-even.")
            if self._hit_target(position.side, price, position.take_profit_2):
                self._close(position, price, "TAKE_PROFIT_2", pnl)
                closed.append(position)
            else:
                self.db.update_trade_status(position.trade_id, "PAPER_OPEN", unrealized_pnl=pnl)
        return closed

    def _close(self, position: PaperPosition, price: float, status: str, pnl: float) -> None:
        position.status = status
        realized = pnl - position.fees
        self.db.update_trade_status(position.trade_id, status, realized_pnl=realized, unrealized_pnl=0.0)
        self.positions.pop(position.symbol, None)
        self.logger.info("PAPER CLOSE %s %s pnl=%.4f", position.symbol, status, realized)
        self.telegram.send(f"PAPER cerrado {position.symbol}: {status}. PnL simulado: {realized:.4f} USDT")

    @staticmethod
    def _hit_stop(position: PaperPosition, price: float) -> bool:
        return price <= position.stop_loss if position.side == "LONG" else price >= position.stop_loss

    @staticmethod
    def _hit_target(side: str, price: float, target: float) -> bool:
        return price >= target if side == "LONG" else price <= target

    @staticmethod
    def _unrealized_pnl(position: PaperPosition, price: float) -> float:
        direction = 1 if position.side == "LONG" else -1
        return (price - position.entry) * position.size * direction

    def open_positions(self) -> list[dict[str, Any]]:
        return [
            {
                "symbol": p.symbol,
                "side": p.side,
                "size": p.size,
                "quantity": p.quantity,
                "entry": p.entry,
                "stop_loss": p.stop_loss,
                "take_profit": p.take_profit_2,
                "margin_mode": p.margin_mode,
                "margin_used": p.margin_used,
                "leverage": p.leverage,
                "notional": p.notional,
            }
            for p in self.positions.values()
        ]

    @staticmethod
    def _liquidation_estimate(signal: Signal, margin_used: float, notional: float) -> float:
        if margin_used <= 0 or notional <= 0:
            return 0.0
        cushion_pct = margin_used / notional
        if signal.side == "LONG":
            return signal.entry_price * max(0.0, 1 - cushion_pct)
        return signal.entry_price * (1 + cushion_pct)
