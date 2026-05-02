from __future__ import annotations

from .bitget_client import BitgetClient
from .config import BotConfig
from .paper_trader import PaperTrader
from .utils import safe_float


class PositionManager:
    def __init__(self, config: BotConfig, client: BitgetClient, paper_trader: PaperTrader | None, telegram, logger) -> None:
        self.config = config
        self.client = client
        self.paper_trader = paper_trader
        self.telegram = telegram
        self.logger = logger

    def sync_open_positions(self) -> list[dict]:
        if self.config.paper_trading and self.paper_trader:
            return self.paper_trader.open_positions()
        if not self.config.has_bitget_credentials or not self.config.live_trading:
            return []
        positions = self.client.get_positions()
        active = [p for p in positions if safe_float(p.get("total")) > 0]
        cross_positions = [
            p for p in active
            if str(p.get("marginMode") or p.get("margin_mode") or "").lower() in {"cross", "crossed"}
        ]
        for position in cross_positions:
            msg = f"Posición no isolated detectada: {position.get('symbol')} {position.get('holdSide')}"
            self.logger.error(msg)
            self.telegram.critical(msg)
        unprotected = [
            p for p in active
            if self.config.require_stop_loss and not p.get("stopLoss")
            or self.config.require_take_profit and not p.get("takeProfit")
        ]
        for position in unprotected:
            msg = f"Posición sin protección detectada: {position.get('symbol')} {position.get('holdSide')}"
            self.logger.error(msg)
            self.telegram.critical(msg)
            if self.config.close_if_protection_fails and self.config.can_send_real_orders:
                try:
                    self.client.close_position_market(
                        position.get("symbol"),
                        "LONG" if position.get("holdSide") == "long" else "SHORT",
                        str(position.get("total")),
                        f"codex-protection-{position.get('symbol')}",
                    )
                except Exception as exc:
                    self.logger.exception("No se pudo cerrar posición sin protección: %s", exc)
        return active

    def monitor(self, latest_prices: dict[str, float]) -> None:
        if self.config.paper_trading and self.paper_trader:
            self.paper_trader.monitor(latest_prices)
            return
        if self.config.can_send_real_orders:
            self.sync_open_positions()
