from __future__ import annotations

try:
    import requests
except ImportError:  # pragma: no cover - production installs requests from requirements.txt
    requests = None  # type: ignore

from .config import BotConfig
from .utils import sanitize


class TelegramAlerts:
    def __init__(self, config: BotConfig, logger) -> None:
        self.config = config
        self.logger = logger
        self.enabled = bool(config.telegram_bot_token and config.telegram_chat_id)

    def send(self, message: str) -> bool:
        if not self.enabled:
            return False
        if requests is None:
            self.logger.warning("Telegram desactivado: paquete requests no instalado.")
            return False
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self.config.telegram_chat_id,
            "text": message[:3900],
            "disable_web_page_preview": True,
        }
        try:
            response = requests.post(url, json=sanitize(payload), timeout=8)
            if response.status_code >= 400:
                self.logger.warning("Telegram rechazó una alerta: %s", response.status_code)
                return False
            return True
        except requests.RequestException as exc:
            self.logger.warning("No se pudo enviar Telegram: %s", exc)
            return False

    def startup(self, mode: str, balance: float | None = None) -> None:
        bal = f"{balance:.2f} USDT" if balance is not None else "no detectado"
        self.send(f"Bot iniciado. Modo: {mode.upper()}. Balance: {bal}.")

    def critical(self, message: str) -> None:
        self.send(f"ALERTA CRITICA\n{message}")

    def trade_opened(self, signal) -> None:
        self.send(
            f"Operacion {signal.side} {signal.symbol}\n"
            f"Estrategia: {signal.strategy_type}\n"
            f"Score: {signal.confidence_score}\n"
            f"Entry: {signal.entry_price:.6g} | SL: {signal.stop_loss:.6g} | "
            f"TP1: {signal.take_profit_1:.6g} | TP2: {signal.take_profit_2:.6g}\n"
            f"Lev: {signal.leverage_recommendation}x"
        )
