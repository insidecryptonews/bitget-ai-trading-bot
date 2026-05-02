from __future__ import annotations

import signal
import sys
import time
from typing import Any

from .bitget_client import BitgetClient
from .config import load_config
from .database import Database
from .execution_engine import ExecutionEngine
from .feature_logger import FeatureLogger
from .health_server import HealthState, start_health_server
from .labeler import TripleBarrierLabeler
from .logger import setup_logger
from .market_data import MarketDataProvider, MarketSnapshot
from .meta_model import MetaModel
from .news_intel import NewsIntel
from .order_manager import InstrumentRules, OrderManager
from .paper_trader import PaperTrader
from .portfolio_allocator import PortfolioAllocator
from .position_manager import PositionManager
from .regime_detector import RegimeDetector
from .risk_manager import RiskManager
from .signal_engine import Signal, SignalEngine
from .telegram_alerts import TelegramAlerts
from .utils import iso_utc, safe_float


STOP_REQUESTED = False


def _handle_shutdown(signum: int, frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    config = load_config()
    logger = setup_logger()
    db = Database(config, logger)
    db.initialize()
    telegram = TelegramAlerts(config, logger)
    health = HealthState(mode=config.mode)
    start_health_server(health, config.port, logger)

    if config.live_trading and config.dry_run:
        logger.warning("LIVE_TRADING activo pero DRY_RUN=true: no se enviarán órdenes reales.")
        telegram.send("LIVE_TRADING activo pero DRY_RUN=true: no se enviarán órdenes reales.")

    if config.paper_trading:
        logger.info("Modo PAPER activo. No se enviarán órdenes reales.")
    elif config.can_send_real_orders:
        logger.warning("BOT EN MODO REAL. OPERANDO CON DINERO REAL.")
    else:
        logger.info("Modo DRY_RUN activo. Señales y órdenes calculadas sin ejecución real.")

    client = BitgetClient(config, logger)
    market_data = MarketDataProvider(config, client, logger)
    instruments = _load_instruments(config.symbols, client, logger, config.can_send_real_orders)
    order_manager = OrderManager(instruments)
    signal_engine = SignalEngine(config)
    regime_detector = RegimeDetector(logger)
    allocator = PortfolioAllocator(config)
    risk_manager = RiskManager(config, order_manager, logger)
    paper_trader = PaperTrader(config, db, telegram, logger) if config.paper_trading else None
    execution_engine = ExecutionEngine(config, client, db, telegram, logger, order_manager)
    position_manager = PositionManager(config, client, paper_trader, telegram, logger)
    news_intel = NewsIntel(config, logger)
    feature_logger = FeatureLogger(db, logger) if config.enable_feature_logging else None
    labeler = TripleBarrierLabeler(config, db, logger) if config.enable_signal_labeling else None
    meta_model = MetaModel(config, db, logger) if config.enable_meta_model and config.meta_model_mode != "off" else None
    if meta_model:
        labeled_rows = db.fetch_labeled_signal_rows()
        meta_model.train(labeled_rows)
        logger.info("MetaModel: %s", meta_model.training_reason)

    valid_symbols = [s for s in config.symbols if s in instruments and instruments[s].is_active]
    if not valid_symbols:
        logger.error("No hay símbolos válidos/activos. Abortando para no operar a ciegas.")
        telegram.critical("No hay símbolos válidos/activos. Bot detenido.")
        sys.exit(1)

    balance = config.starting_capital_usdt
    available_balance = balance
    if config.can_send_real_orders:
        if not config.has_bitget_credentials:
            logger.error("LIVE real solicitado, pero faltan credenciales Bitget.")
            sys.exit(1)
        try:
            account = client.get_account("BTCUSDT")
            balance = safe_float(account.get("usdtEquity") or account.get("accountEquity"), config.starting_capital_usdt)
            available_balance = safe_float(
                account.get("available") or account.get("isolatedMaxAvailable") or account.get("crossedMaxAvailable"),
                balance,
            )
            if balance < config.stop_trading_below_balance_usdt:
                logger.error("Balance %.2f menor al mínimo %.2f. No se opera.", balance, config.stop_trading_below_balance_usdt)
                telegram.critical(f"Balance {balance:.2f} USDT menor al mínimo. Bot bloqueado.")
                sys.exit(1)
            startup_msg = (
                f"BOT EN MODO REAL. OPERANDO CON DINERO REAL. BALANCE: {balance:.2f} USDT. "
                f"RIESGO POR TRADE: {config.max_risk_per_trade * 100:.2f}%. "
                f"LEVERAGE MÁXIMO: {config.max_leverage}x. "
                f"MARGIN MODE: {config.margin_mode}. AUTO MARGIN: {str(config.auto_margin).lower()}."
            )
            logger.warning(startup_msg)
            telegram.send(startup_msg)
        except Exception as exc:
            logger.exception("No se pudo validar cuenta live")
            telegram.critical(f"No se pudo validar cuenta live: {exc}")
            sys.exit(1)
    else:
        telegram.startup(config.mode, balance)

    open_positions = position_manager.sync_open_positions()
    health.open_positions = len(open_positions)
    db.record_event("startup", f"Bot iniciado en modo {config.mode}", payload={"symbols": valid_symbols})

    logger.info("Símbolos activos: %s", ", ".join(valid_symbols))
    logger.info("Advertencia: live trading sin backtest validado aumenta el riesgo. Ejecuta backtests antes de activar real.")

    while not STOP_REQUESTED:
        try:
            cycle_start = time.time()
            news = news_intel.check()
            snapshots = market_data.fetch_all(valid_symbols)
            if not snapshots:
                logger.warning("No se pudieron descargar snapshots; pausa de seguridad.")
                time.sleep(config.scan_interval_seconds)
                continue

            regime = regime_detector.detect(snapshots)
            signals = [signal_engine.generate_signal(symbol, snapshot, regime) for symbol, snapshot in snapshots.items()]
            observation_ids: dict[str, int] = {}
            observation_payloads: dict[str, dict[str, Any]] = {}
            if feature_logger:
                for signal_item in signals:
                    observation = feature_logger.build_observation(
                        signal=signal_item,
                        snapshot=snapshots.get(signal_item.symbol),
                        market_regime=regime,
                        all_snapshots=snapshots,
                        operated=False,
                        block_reason=signal_item.reason if signal_item.side == "NO_TRADE" else "",
                        selected_by_allocator=False,
                        risk_manager_approved=False,
                    )
                    observation_payloads[signal_item.symbol] = observation
                    observation_ids[signal_item.symbol] = feature_logger.record_observation(observation)
            if labeler:
                _label_matured_observations(config, db, labeler, snapshots, logger)
            _print_radar(signals, snapshots, regime.regime, logger)

            interesting = [s for s in signals if s.confidence_score >= config.min_score_to_alert]
            for signal_item in interesting:
                if signal_item.side == "NO_TRADE":
                    logger.info("Alerta sin operación: %s %s score=%s %s", signal_item.symbol, signal_item.strategy_type, signal_item.confidence_score, signal_item.reason)
                else:
                    telegram.send(
                        f"Señal {signal_item.side} {signal_item.symbol} score={signal_item.confidence_score} "
                        f"{signal_item.strategy_type}. {signal_item.reason}"
                    )

            open_positions = position_manager.sync_open_positions()
            if news.block_trading:
                logger.warning("News intel bloquea trading: %s", "; ".join(news.warnings))
                selected: list[Signal] = []
            else:
                allocation = allocator.allocate(signals, balance=balance, open_positions=open_positions, regime=regime)
                selected = allocation.selected_signals
                logger.info("Allocator: %s", allocation.reason)
                if feature_logger:
                    for signal_item in selected:
                        feature_logger.update_observation(
                            observation_ids.get(signal_item.symbol),
                            selected_by_allocator=True,
                            block_reason="",
                        )
                    for rejected_signal, reason in allocation.rejected_signals:
                        feature_logger.update_observation(
                            observation_ids.get(rejected_signal.symbol),
                            selected_by_allocator=False,
                            block_reason=reason,
                        )

            if news.reduce_risk:
                logger.warning("News intel reduce riesgo: %s", "; ".join(news.warnings))

            daily_pnl = db.get_daily_realized_pnl()
            weekly_pnl = db.get_weekly_realized_pnl()
            for selected_signal in selected:
                if meta_model:
                    meta_decision = meta_model.evaluate(
                        observation_payloads.get(selected_signal.symbol, {}),
                        risk_manager_approved=True,
                    )
                    if feature_logger:
                        feature_logger.update_observation(
                            observation_ids.get(selected_signal.symbol),
                            meta_probability=meta_decision.meta_probability,
                            meta_decision=meta_decision.meta_decision,
                        )
                    logger.info(
                        "MetaModel %s %s: decision=%s probability=%s reason=%s",
                        selected_signal.symbol,
                        selected_signal.strategy_type,
                        meta_decision.meta_decision,
                        "NA" if meta_decision.meta_probability is None else f"{meta_decision.meta_probability:.3f}",
                        meta_decision.reason,
                    )
                    if meta_decision.blocks_trade:
                        if feature_logger:
                            feature_logger.update_observation(
                                observation_ids.get(selected_signal.symbol),
                                block_reason="meta_model_rejected",
                                risk_manager_approved=False,
                            )
                        continue
                rules = instruments.get(selected_signal.symbol)
                if not rules:
                    logger.warning("%s sin reglas de instrumento. No opera.", selected_signal.symbol)
                    if feature_logger:
                        feature_logger.update_observation(
                            observation_ids.get(selected_signal.symbol),
                            block_reason="missing_instrument_rules",
                        )
                    continue
                if config.can_send_real_orders:
                    balance, available_balance, used_margin, balance_ok = _refresh_live_account_balance(
                        config,
                        client,
                        selected_signal.symbol,
                        logger,
                        telegram,
                        balance,
                        available_balance,
                    )
                    if not balance_ok:
                        logger.error("%s bloqueado: no se pudo refrescar balance live.", selected_signal.symbol)
                        if feature_logger:
                            feature_logger.update_observation(
                                observation_ids.get(selected_signal.symbol),
                                block_reason="live_balance_refresh_failed",
                            )
                        continue
                effective_balance = balance * 0.5 if news.reduce_risk else balance
                risk = risk_manager.validate_signal(
                    selected_signal,
                    balance=effective_balance,
                    available_balance=available_balance,
                    open_positions=open_positions,
                    daily_pnl=daily_pnl,
                    weekly_pnl=weekly_pnl,
                    rules=rules,
                )
                if not risk.approved:
                    logger.info("RiskManager bloquea %s: %s", selected_signal.symbol, risk.reason)
                    if feature_logger:
                        feature_logger.update_observation(
                            observation_ids.get(selected_signal.symbol),
                            risk_manager_approved=False,
                            block_reason=risk.block_reason or risk.reason,
                        )
                    continue
                safe_signal = risk.signal or selected_signal
                if feature_logger:
                    feature_logger.update_observation(
                        observation_ids.get(selected_signal.symbol),
                        risk_manager_approved=True,
                    )
                if config.paper_trading and paper_trader:
                    paper_trader.open_position(safe_signal, risk.risk_amount, risk)
                    if feature_logger:
                        feature_logger.update_observation(
                            observation_ids.get(selected_signal.symbol),
                            operated=True,
                            block_reason="",
                        )
                else:
                    result = execution_engine.execute(safe_signal, risk, rules)
                    logger.info("Execution result %s: %s", safe_signal.symbol, result.reason)
                    if feature_logger:
                        feature_logger.update_observation(
                            observation_ids.get(selected_signal.symbol),
                            operated=result.executed,
                            block_reason="" if result.executed else result.reason,
                        )

            latest_prices = {symbol: snap.current_price for symbol, snap in snapshots.items() if snap.current_price}
            position_manager.monitor(latest_prices)
            open_positions = position_manager.sync_open_positions()
            health.open_positions = len(open_positions)
            health.daily_pnl = daily_pnl
            health.last_scan = iso_utc()
            health.circuit_breaker = bool(risk_manager.cooldown_until)
            db.set_state(
                "last_cycle",
                {
                    "timestamp": health.last_scan,
                    "regime": regime.regime,
                    "open_positions": len(open_positions),
                    "daily_pnl": daily_pnl,
                },
            )
            elapsed = time.time() - cycle_start
            sleep_for = max(1, config.scan_interval_seconds - elapsed)
            time.sleep(sleep_for)
        except Exception as exc:
            logger.exception("Error en ciclo principal")
            db.record_event("main_loop_error", str(exc), level="ERROR")
            telegram.critical(f"Error en ciclo principal: {exc}")
            risk_manager.register_api_failure()
            time.sleep(config.fast_scan_interval_seconds)

    logger.info("Apagado solicitado. Cerrando limpio.")
    telegram.send("Bot detenido limpiamente.")


def _load_instruments(symbols: list[str], client: BitgetClient, logger, require_real_validation: bool) -> dict[str, InstrumentRules]:
    try:
        contracts = client.get_contracts()
        rules = {item.get("symbol", "").upper(): InstrumentRules.from_bitget_contract(item) for item in contracts}
        missing = [s for s in symbols if s not in rules]
        if missing:
            logger.warning("Símbolos no encontrados en Bitget Futures: %s", ", ".join(missing))
        return {symbol: rules[symbol] for symbol in symbols if symbol in rules}
    except Exception as exc:
        logger.error("No se pudieron consultar contratos Bitget: %s", exc)
        if require_real_validation:
            raise
        logger.warning("Modo no-live: se usarán reglas conservadoras de prueba solo para paper/dry-run.")
        return {symbol: _fallback_rules(symbol) for symbol in symbols}


def _fallback_rules(symbol: str) -> InstrumentRules:
    return InstrumentRules(
        symbol=symbol,
        min_trade_num=0.001,
        min_trade_usdt=5.0,
        size_multiplier=0.001,
        volume_place=3,
        price_place=4,
        price_end_step=1.0,
        min_leverage=1,
        max_leverage=5,
        maker_fee_rate=0.0004,
        taker_fee_rate=0.0006,
        symbol_status="normal",
        max_market_order_qty=1_000_000,
        max_order_qty=1_000_000,
    )


def _refresh_live_account_balance(config, client, symbol: str, logger, telegram, fallback_balance: float, fallback_available: float) -> tuple[float, float, float, bool]:
    if not config.can_send_real_orders:
        return fallback_balance, fallback_available, max(0.0, fallback_balance - fallback_available), True
    try:
        account = client.get_account(symbol)
        live_balance = safe_float(
            account.get("usdtEquity") or account.get("accountEquity") or account.get("equity"),
            0.0,
        )
        live_available = safe_float(
            account.get("available") or account.get("isolatedMaxAvailable") or account.get("crossedMaxAvailable"),
            0.0,
        )
        if live_balance <= 0:
            raise RuntimeError(f"Balance live invalido para {symbol}: {account}")
        if live_available < 0:
            raise RuntimeError(f"Available balance live invalido para {symbol}: {account}")
        used_margin = max(0.0, live_balance - live_available)
        logger.info(
            "Live balance refresh %s: live_balance=%.4f live_available_balance=%.4f used_margin=%.4f",
            symbol,
            live_balance,
            live_available,
            used_margin,
        )
        if live_balance < config.stop_trading_below_balance_usdt:
            logger.error("Balance live %.2f menor al minimo %.2f. No se opera.", live_balance, config.stop_trading_below_balance_usdt)
            telegram.critical(f"Balance live {live_balance:.2f} USDT menor al minimo. Operacion bloqueada.")
            return live_balance, live_available, used_margin, False
        return live_balance, live_available, used_margin, True
    except Exception as exc:
        logger.error("No se pudo refrescar balance live para %s: %s", symbol, exc)
        telegram.critical(f"No se pudo refrescar balance live para {symbol}: {exc}. Operacion bloqueada.")
        return fallback_balance, fallback_available, max(0.0, fallback_balance - fallback_available), False


def _label_matured_observations(config, db: Database, labeler: TripleBarrierLabeler, snapshots: dict[str, MarketSnapshot], logger) -> None:
    try:
        import pandas as pd

        pending = db.fetch_unlabeled_signal_observations(limit=200)
        for observation in pending:
            snapshot = snapshots.get(str(observation.get("symbol", "")))
            if not snapshot:
                continue
            frame = snapshot.candles.get(config.main_timeframe.lower())
            if frame is None:
                frame = snapshot.candles.get("5m")
            if frame is None or frame.empty or "timestamp" not in frame.columns:
                continue
            ts = pd.to_datetime(observation.get("timestamp"), utc=True, errors="coerce")
            future = frame[pd.to_datetime(frame["timestamp"], utc=True, errors="coerce") > ts]
            if future.empty:
                continue
            outcome = labeler.label_observation(observation, frame)
            if outcome.first_barrier_hit == "TIME" and len(future) < config.max_holding_bars:
                continue
            labeler.save_label(outcome)
            logger.info(
                "Signal label %s obs=%s label=%s barrier=%s bars=%s",
                observation.get("symbol"),
                observation.get("id"),
                outcome.label,
                outcome.first_barrier_hit,
                outcome.bars_to_outcome,
            )
    except Exception as exc:
        logger.warning("No se pudieron etiquetar observaciones pendientes: %s", exc)


def _print_radar(signals: list[Signal], snapshots: dict[str, MarketSnapshot], regime: str, logger) -> None:
    header = "Símbolo | Precio | Régimen | Sesgo | Score | Señal | Estrategia | Entrada | SL | TP1 | TP2 | Lev | Motivo"
    logger.info(header)
    for signal_item in sorted(signals, key=lambda s: s.confidence_score, reverse=True):
        snap = snapshots.get(signal_item.symbol)
        price = snap.current_price if snap else signal_item.entry_price
        bias = "neutral"
        if signal_item.side == "LONG":
            bias = "alcista"
        elif signal_item.side == "SHORT":
            bias = "bajista"
        reason = signal_item.reason
        if signal_item.side == "NO_TRADE" and signal_item.warnings:
            reason = "No trade: " + "; ".join(signal_item.warnings[:2])
        logger.info(
            "%s | %.8g | %s | %s | %s | %s | %s | %.8g | %.8g | %.8g | %.8g | %sx | %s",
            signal_item.symbol,
            price,
            regime,
            bias,
            signal_item.confidence_score,
            signal_item.side,
            signal_item.strategy_type,
            signal_item.entry_price,
            signal_item.stop_loss,
            signal_item.take_profit_1,
            signal_item.take_profit_2,
            signal_item.leverage_recommendation,
            reason,
        )


if __name__ == "__main__":
    main()
