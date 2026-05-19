from __future__ import annotations

import uuid
from dataclasses import dataclass

from .bitget_client import BitgetAPIError, BitgetClient
from .config import BotConfig
from .database import Database
from .execution_safety import emergency_close_with_retry
from .order_manager import InstrumentRules, OrderManager
from .risk_manager import RiskDecision
from .signal_engine import Signal
from .utils import json_dumps, sanitize


@dataclass
class ExecutionResult:
    executed: bool
    mode: str
    reason: str
    order_response: dict | None = None
    trade_id: int = 0


class ExecutionEngine:
    def __init__(
        self,
        config: BotConfig,
        client: BitgetClient,
        db: Database,
        telegram,
        logger,
        order_manager: OrderManager | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.db = db
        self.telegram = telegram
        self.logger = logger
        self.order_manager = order_manager
        self.blocked_symbols: set[str] = set()

    def execute(self, signal: Signal, risk: RiskDecision, rules: InstrumentRules | None = None) -> ExecutionResult:
        if not risk.approved:
            return ExecutionResult(False, self.config.mode, risk.reason)
        signal = risk.signal or signal
        if signal.symbol in self.blocked_symbols:
            return ExecutionResult(False, self.config.mode, f"{signal.symbol} bloqueado hasta revision por fallo de isolated margin")

        if self.config.paper_trading:
            trade_id = self.db.record_trade(mode="paper", signal=signal, status="PAPER_READY", risk_amount=risk.risk_amount)
            return ExecutionResult(False, "paper", "PaperTrader debe ejecutar la simulación", trade_id=trade_id)

        if self.config.dry_run or not self.config.can_send_real_orders:
            payload = self._build_order_preview(signal)
            self.logger.info("DRY RUN orden calculada: %s", json_dumps(payload))
            trade_id = self.db.record_trade(
                mode="dry_run",
                signal=signal,
                status="DRY_RUN",
                risk_amount=risk.risk_amount,
                raw_order_response=payload,
            )
            return ExecutionResult(False, "dry_run", "DRY_RUN: no se envían órdenes reales", payload, trade_id)

        if not self.config.has_bitget_credentials:
            return ExecutionResult(False, "live", "Credenciales Bitget incompletas")
        if self.config.margin_mode != "isolated":
            message = "OPERACION BLOQUEADA: el simbolo no esta en isolated margin."
            self.logger.error("%s margin_mode=%s", message, self.config.margin_mode)
            self.telegram.critical(message)
            return ExecutionResult(False, "live", message)
        if signal.stop_loss <= 0 or signal.take_profit_1 <= 0:
            return ExecutionResult(False, "live", "Protecciones incompletas; no se abre operación real")

        if hasattr(self.db, "fetch_pending_execution_intents") and self.db.fetch_pending_execution_intents():
            return ExecutionResult(False, "live", "PENDING_EXECUTION requiere reconciliacion antes de abrir otra posicion")

        client_oid = ""
        try:
            rules = rules or (self.order_manager.get_rules(signal.symbol) if self.order_manager else None)
            if rules is None:
                return ExecutionResult(False, "live", "Reglas de instrumento no disponibles en ejecución")

            size = OrderManager.round_size(signal.position_size, rules, "down")
            hold_side = "long" if signal.side == "LONG" else "short"
            order_side = "buy" if signal.side == "LONG" else "sell"
            client_oid = self._client_oid("open", signal.symbol)

            margin_status = self.client.ensure_isolated_margin(signal.symbol, signal.side)
            if not margin_status.get("isolatedVerified") or margin_status.get("marginMode") != "isolated":
                message = "OPERACION BLOQUEADA: el simbolo no esta en isolated margin."
                self.logger.error("%s status=%s", message, margin_status)
                self.telegram.critical(message)
                self.blocked_symbols.add(signal.symbol)
                return ExecutionResult(False, "live", message, margin_status)
            self.telegram.send(
                f"Isolated margin verificado para {signal.symbol}. Auto margin off: {margin_status.get('autoMarginOff')}"
            )

            self.client.set_leverage(signal.symbol, signal.leverage_recommendation, hold_side=hold_side)
            order_type = "market" if signal.confidence_score >= self.config.min_score_excellent and signal.risk_reward_ratio >= 1.5 else "limit"
            price = str(OrderManager.round_price(signal.entry_price, rules)) if order_type == "limit" else None
            order_payload = {
                "symbol": signal.symbol,
                "side": order_side,
                "size": str(size),
                "order_type": order_type,
                "trade_side": "open",
                "price": price,
                "client_oid": client_oid,
                "marginMode": self.config.margin_mode,
            }
            if hasattr(self.db, "record_execution_intent"):
                self.db.record_execution_intent(
                    client_oid=client_oid,
                    symbol=signal.symbol,
                    side=signal.side,
                    mode="live",
                    status="PENDING_EXECUTION",
                    order_payload_sanitized=order_payload,
                )
            order_response = self.client.place_order(
                symbol=signal.symbol,
                side=order_side,
                size=str(size),
                order_type=order_type,
                trade_side="open",
                price=price,
                client_oid=client_oid,
                reduce_only=False,
                preset_stop_loss_price=str(OrderManager.round_price(signal.stop_loss, rules)),
                preset_take_profit_price=str(OrderManager.round_price(signal.take_profit_1, rules)),
            )
            if hasattr(self.db, "update_execution_intent"):
                self.db.update_execution_intent(client_oid, status="SENT", order_payload_sanitized={"open": order_response})
            stop_ok = self._place_stop(signal, rules, size, hold_side)
            tp_ok = self._place_take_profits(signal, rules, size, hold_side)
            if not stop_ok:
                close_result = self._emergency_close_with_retry(signal, size)
                close = close_result.get("response")
                if close_result.get("status") == "CRITICAL_UNPROTECTED_POSITION":
                    if hasattr(self.db, "update_execution_intent"):
                        self.db.update_execution_intent(client_oid, status="UNKNOWN", error_sanitized="CRITICAL_UNPROTECTED_POSITION")
                    trade_id = self.db.record_trade(
                        mode="live",
                        signal=signal,
                        status="CRITICAL_UNPROTECTED_POSITION",
                        risk_amount=risk.risk_amount,
                        raw_order_response={"open": order_response, "panic_close": close_result},
                        error_message="stop_loss_failed_and_emergency_close_failed",
                    )
                    return ExecutionResult(False, "live", "Stop fallo y emergency close fallo; posicion sin proteccion critica", close_result, trade_id)
                self.telegram.critical(f"Stop falló en {signal.symbol}. Se intentó cierre inmediato.")
                trade_id = self.db.record_trade(
                    mode="live",
                    signal=signal,
                    status="LIVE_PROTECTION_FAILED_CLOSED",
                    risk_amount=risk.risk_amount,
                    raw_order_response={"open": order_response, "panic_close": close},
                    error_message="stop_loss_failed",
                )
                return ExecutionResult(False, "live", "Stop falló; posición cerrada por seguridad", close, trade_id)
            if not tp_ok and self.config.close_if_protection_fails:
                close_result = self._emergency_close_with_retry(signal, size, prefix="tp-fail")
                close = close_result.get("response")
                self.telegram.critical(f"TP falló en {signal.symbol}. Se intentó cierre por seguridad.")
                trade_id = self.db.record_trade(
                    mode="live",
                    signal=signal,
                    status="LIVE_TP_FAILED_CLOSED",
                    risk_amount=risk.risk_amount,
                    raw_order_response={"open": order_response, "safe_close": close},
                    error_message="take_profit_failed",
                )
                return ExecutionResult(False, "live", "TP falló; cierre de seguridad", close, trade_id)

            trade_id = self.db.record_trade(
                mode="live",
                signal=signal,
                status="LIVE_OPEN",
                risk_amount=risk.risk_amount,
                raw_order_response=order_response,
            )
            if hasattr(self.db, "update_execution_intent"):
                self.db.update_execution_intent(client_oid, status="CONFIRMED", order_payload_sanitized={"open": order_response, "trade_id": trade_id})
            self.telegram.trade_opened(signal)
            self.telegram.send(
                "MODO REAL\n"
                f"Simbolo: {signal.symbol}\n"
                f"Side: {signal.side}\n"
                "Margin mode: isolated\n"
                f"Margin usado: {risk.selected_margin_usdt:.2f} USDT\n"
                f"Leverage: {signal.leverage_recommendation}x\n"
                f"Notional: {risk.notional:.2f} USDT\n"
                f"Entrada: {signal.entry_price:.6g}\n"
                f"SL: {signal.stop_loss:.6g}\n"
                f"TP1: {signal.take_profit_1:.6g}\n"
                f"TP2: {signal.take_profit_2:.6g}"
            )
            return ExecutionResult(True, "live", "Orden real enviada y protecciones colocadas", order_response, trade_id)
        except BitgetAPIError as exc:
            message = f"OPERACION BLOQUEADA: no se pudo verificar/cambiar isolated margin en {signal.symbol}: {exc}"
            self.logger.error(message)
            self.telegram.critical(message)
            self.blocked_symbols.add(signal.symbol)
            return ExecutionResult(False, "live", message)
        except Exception as exc:
            self.logger.exception("Ejecución live falló")
            self.telegram.critical(f"Error crítico ejecutando {signal.symbol}: {exc}")
            if client_oid and hasattr(self.db, "update_execution_intent"):
                self.db.update_execution_intent(client_oid, status="FAILED", error_sanitized=sanitize(str(exc)))
            self.db.record_trade(mode="live", signal=signal, status="LIVE_ERROR", risk_amount=risk.risk_amount, error_message=str(exc))
            return ExecutionResult(False, "live", f"Error ejecución live: {exc}")

    def _place_stop(self, signal: Signal, rules: InstrumentRules, size: float, hold_side: str) -> bool:
        for attempt in range(1, 4):
            try:
                self.client.place_tpsl_order(
                    symbol=signal.symbol,
                    plan_type="loss_plan",
                    trigger_price=str(OrderManager.round_price(signal.stop_loss, rules)),
                    hold_side=hold_side,
                    size=str(size),
                    client_oid=self._client_oid("sl", signal.symbol),
                )
                return True
            except Exception as exc:
                self.logger.error("No se pudo colocar stop en %s intento %s/3: %s", signal.symbol, attempt, exc)
        return False

    def _place_take_profits(self, signal: Signal, rules: InstrumentRules, size: float, hold_side: str) -> bool:
        try:
            half = OrderManager.round_size(size / 2, rules, "down")
            rest = OrderManager.round_size(max(size - half, 0), rules, "down")
            if half > 0:
                self.client.place_tpsl_order(
                    symbol=signal.symbol,
                    plan_type="profit_plan",
                    trigger_price=str(OrderManager.round_price(signal.take_profit_1, rules)),
                    hold_side=hold_side,
                    size=str(half),
                    client_oid=self._client_oid("tp1", signal.symbol),
                )
            if rest > 0:
                self.client.place_tpsl_order(
                    symbol=signal.symbol,
                    plan_type="profit_plan",
                    trigger_price=str(OrderManager.round_price(signal.take_profit_2, rules)),
                    hold_side=hold_side,
                    size=str(rest),
                    client_oid=self._client_oid("tp2", signal.symbol),
                )
            return True
        except Exception as exc:
            self.logger.error("No se pudo colocar TP en %s: %s", signal.symbol, exc)
            return False

    def _build_order_preview(self, signal: Signal) -> dict:
        return {
            "symbol": signal.symbol,
            "side": "buy" if signal.side == "LONG" else "sell",
            "tradeSide": "open",
            "orderType": "market" if signal.confidence_score >= self.config.min_score_excellent else "limit",
            "size": signal.position_size,
            "entry": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit_1": signal.take_profit_1,
            "take_profit_2": signal.take_profit_2,
            "leverage": signal.leverage_recommendation,
            "marginMode": self.config.margin_mode,
            "trade_margin_usdt": float(self.config.trade_margin_usdt),
            "max_trade_margin_usdt": float(self.config.max_trade_margin_usdt),
            "reduceOnly": "NO",
        }

    def _emergency_close_with_retry(self, signal: Signal, size: float, prefix: str = "panic") -> dict:
        return emergency_close_with_retry(
            lambda: self.client.close_position_market(signal.symbol, signal.side, str(size), self._client_oid(prefix, signal.symbol)),
            max_attempts=3,
            alert=self.telegram.critical,
        )

    @staticmethod
    def _client_oid(prefix: str, symbol: str) -> str:
        return f"codex-{prefix}-{symbol.lower()}-{uuid.uuid4().hex[:16]}"
