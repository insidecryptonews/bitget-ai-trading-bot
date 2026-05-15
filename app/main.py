from __future__ import annotations

import signal
import sys
import time
from threading import Thread
from typing import Any

from .bitget_client import BitgetClient
from .config import load_config
from .data_vault import DataVault
from .database import Database
from .daily_summary import DailyResearchSummary
from .edge_guard import EdgeGuard
from .execution_engine import ExecutionEngine
from .feature_logger import FeatureLogger
from .full_research_report import END_MARKER, START_MARKER, FullResearchReporter
from .health_server import HealthState, start_health_server
from .labeler import TripleBarrierLabeler
from .logger import setup_logger
from .market_data import MarketDataProvider, MarketSnapshot
from .meta_model import MetaModel
from .mfe_mae_tracker import MfeMaeTracker
from .news_intel import NewsIntel
from .order_manager import InstrumentRules, OrderManager
from .paper_trader import PaperTrader
from .paper_reconciler import PaperReconciler
from .portfolio_allocator import PortfolioAllocator
from .position_manager import PositionManager
from .regime_detector import RegimeDetector
from .research_autopilot import ResearchAutopilot
from .research_engine import ResearchEngine
from .risk_manager import RiskManager
from .shadow_strategies import ShadowStrategyEngine
from .signal_engine import Signal, SignalEngine
from .telegram_alerts import TelegramAlerts
from .telegram_notifier import TelegramNotifier
from .training_pulse import TrainingPulse
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
    training_pulse = TrainingPulse() if config.enable_training_pulse else None
    telegram_notifier = TelegramNotifier(config, logger)
    telegram = TelegramAlerts(config, logger)
    health = HealthState(mode=config.mode)
    start_health_server(
        health,
        config.port,
        logger,
        config=config,
        db=db,
        training_pulse=training_pulse,
        telegram_notifier=telegram_notifier,
    )
    if config.worker_lightweight_mode:
        logger.info("WORKER_LIGHTWEIGHT_MODE activo: research pesado desactivado en worker 24/7")

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
    shadow_engine = ShadowStrategyEngine(db, feature_logger, logger) if feature_logger else None
    labeler = TripleBarrierLabeler(config, db, logger) if config.enable_signal_labeling else None
    mfe_mae_tracker = MfeMaeTracker(config, db, logger) if config.enable_mfe_mae_capture else None
    research_engine = ResearchEngine(db, logger) if config.enable_research_auto_report else None
    full_research_reporter = FullResearchReporter(db, config, logger) if config.enable_full_research_auto_report else None
    research_autopilot = ResearchAutopilot(config, db, logger) if config.enable_research_autopilot else None
    daily_summary = DailyResearchSummary(config, db, logger) if config.enable_daily_research_summary else None
    last_research_report_at = 0.0
    last_full_research_report_at = 0.0
    last_research_autopilot_at = 0.0
    last_daily_summary_at = 0.0
    last_telegram_pulse_at = 0.0
    meta_model = MetaModel(config, db, logger) if config.enable_meta_model and config.meta_model_mode != "off" else None
    if meta_model and config.meta_model_train_on_start and not config.worker_lightweight_mode:
        labeled_rows = db.fetch_labeled_signal_rows()
        meta_model.train(labeled_rows)
        logger.info("MetaModel: %s", meta_model.training_reason)

    if paper_trader:
        if config.enable_paper_reconcile_on_start or config.lightweight_paper_reconcile_on_start:
            try:
                result = PaperReconciler(config, db, logger).reconcile()
                logger.info("%s", result.to_text())
                _record_paper_reconcile_event(db, result)
                if training_pulse:
                    training_pulse.record_paper_reconcile(result)
                if result.paper_trades_closed_by_label or result.paper_trades_closed_by_time:
                    _send_telegram_alert_if_needed(
                        config,
                        telegram_notifier,
                        "PAPER RECONCILE",
                        (
                            f"closed_label={result.paper_trades_closed_by_label} "
                            f"closed_time={result.paper_trades_closed_by_time} "
                            f"left_open={result.paper_trades_left_open}"
                        ),
                    )
            except Exception as exc:
                logger.warning("Paper reconcile on start fallo sin detener el bot: %s", exc)
        paper_trader.load_open_positions_from_db()

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

    startup_monotonic = time.monotonic()
    if full_research_reporter and config.full_research_startup_enabled and not config.worker_lightweight_mode:
        logger.info("Full research report inicial programado")
        last_full_research_report_at = _emit_full_research_auto_report_if_due(
            config,
            full_research_reporter,
            logger,
            last_full_research_report_at,
            time.monotonic(),
            initial=True,
        )
    elif full_research_reporter:
        last_full_research_report_at = startup_monotonic
    if daily_summary and config.daily_research_summary_on_start and not config.worker_lightweight_mode:
        last_daily_summary_at = _emit_daily_research_summary_if_due(
            config,
            daily_summary,
            logger,
            last_daily_summary_at,
            time.monotonic(),
            initial=True,
        )
    elif daily_summary:
        last_daily_summary_at = startup_monotonic
    if training_pulse and config.training_pulse_log_on_start:
        logger.info("%s", training_pulse.to_text(config))
        db.record_event("training_pulse", "training pulse startup", payload={"startup": True})
        if config.training_pulse_reset_after_emit:
            training_pulse.reset_window()

    cycle_count = 0
    last_memory_log_at = startup_monotonic
    last_lightweight_paper_reconcile_at = startup_monotonic
    last_mfe_mae_debug_at = startup_monotonic
    last_data_vault_backup_at = startup_monotonic
    data_vault_backup_running = {"running": False}
    last_data_vault_backup_at = _data_vault_startup_backup_if_needed(
        config,
        db,
        logger,
        last_data_vault_backup_at,
        startup_monotonic,
        data_vault_backup_running,
    )
    while not STOP_REQUESTED:
        try:
            cycle_count += 1
            if training_pulse:
                training_pulse.record_cycle_start()
            cycle_start = time.time()
            cycle_timer_start = time.monotonic()
            market_fetch_ms = 0.0
            signal_generation_ms = 0.0
            decision_ms = 0.0
            news = news_intel.check()
            market_fetch_start = time.monotonic()
            snapshots = market_data.fetch_all(valid_symbols)
            market_fetch_ms = (time.monotonic() - market_fetch_start) * 1000.0
            if not snapshots:
                _record_latency_metrics(db, {
                    "cycle_total_ms": (time.monotonic() - cycle_timer_start) * 1000.0,
                    "market_fetch_ms": market_fetch_ms,
                })
                logger.warning("No se pudieron descargar snapshots; pausa de seguridad.")
                if training_pulse:
                    training_pulse.record_snapshots(0)
                    training_pulse.record_cycle_ok()
                    _emit_training_pulse_if_due(config, db, training_pulse, logger, time.monotonic())
                time.sleep(config.scan_interval_seconds)
                continue
            if training_pulse:
                training_pulse.record_snapshots(len(snapshots))

            regime = regime_detector.detect(snapshots)
            signal_generation_start = time.monotonic()
            signals = [signal_engine.generate_signal(symbol, snapshot, regime) for symbol, snapshot in snapshots.items()]
            signal_generation_ms = (time.monotonic() - signal_generation_start) * 1000.0
            if training_pulse:
                training_pulse.record_regime(regime.regime)
                training_pulse.record_signals(signals, config.min_score_to_trade)
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
                    if mfe_mae_tracker:
                        mfe_mae_tracker.register_signal(
                            observation_id=observation_ids.get(signal_item.symbol),
                            signal=signal_item,
                            snapshot=snapshots.get(signal_item.symbol),
                            market_regime=regime.regime,
                            source="trade_signal",
                        )
                    if shadow_engine:
                        shadow_engine.log_variants(
                            signal=signal_item,
                            base_observation=observation,
                            market_regime=regime,
                        )
            if mfe_mae_tracker:
                low_score_result = mfe_mae_tracker.register_low_score_samples(
                    signals=signals,
                    snapshots=snapshots,
                    observation_ids=observation_ids,
                    market_regime=regime.regime,
                )
                probe_result = mfe_mae_tracker.register_market_probes(
                    snapshots=snapshots,
                    market_regime=regime.regime,
                    cycle_count=cycle_count,
                )
                if training_pulse:
                    training_pulse.record_mfe_mae(probe_result if probe_result.candidates_tracked or probe_result.market_probes_created else low_score_result)
            if labeler:
                label_counts = _label_matured_observations(config, db, labeler, snapshots, logger)
                if training_pulse:
                    training_pulse.record_labels(label_counts)
            if (
                mfe_mae_tracker
                and config.mfe_mae_update_every_n_cycles > 0
                and cycle_count % max(1, int(config.mfe_mae_update_every_n_cycles or 1)) == 0
            ):
                mfe_result = mfe_mae_tracker.update_active(snapshots)
                if training_pulse:
                    training_pulse.record_mfe_mae(mfe_result)
            if _should_print_radar(config, cycle_count, signals):
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
            decision_start = time.monotonic()
            if news.block_trading:
                logger.warning("News intel bloquea trading: %s", "; ".join(news.warnings))
                selected: list[Signal] = []
            else:
                allocation = allocator.allocate(signals, balance=balance, open_positions=open_positions, regime=regime)
                selected = allocation.selected_signals
                logger.info("Allocator: %s", allocation.reason)
                if training_pulse:
                    training_pulse.record_allocator(allocation.reason, len(selected))
                if _is_slot_block_reason(allocation.reason):
                    db.record_event("training_slot_block", "slot block", payload={"reason": str(allocation.reason)[:300]})
                if feature_logger:
                    for signal_item in selected:
                        feature_logger.update_observation(
                            observation_ids.get(signal_item.symbol),
                            selected_by_allocator=True,
                            block_reason="",
                        )
                    for rejected_signal, reason in allocation.rejected_signals:
                        if rejected_signal.confidence_score >= config.min_score_to_trade and rejected_signal.side in {"LONG", "SHORT"}:
                            _record_high_score_missed_event(
                                db,
                                training_pulse,
                                rejected_signal,
                                regime.regime,
                                reason,
                                min_score_to_trade=config.min_score_to_trade,
                                slot_available=not _is_slot_block_reason(reason),
                                risk_approved=False,
                            )
                        _track_mfe_mae_candidate(
                            mfe_mae_tracker,
                            training_pulse,
                            observation_ids.get(rejected_signal.symbol),
                            rejected_signal,
                            snapshots,
                            regime.regime,
                            _mfe_source_for_reject(reason),
                            reason,
                        )
                        if _is_slot_block_reason(reason):
                            _record_slot_block_event(db, training_pulse, reason)
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
                        _record_high_score_missed_event(
                            db,
                            training_pulse,
                            selected_signal,
                            regime.regime,
                            "meta_model_rejected",
                            min_score_to_trade=config.min_score_to_trade,
                            slot_available=True,
                            risk_approved=True,
                        )
                        _track_mfe_mae_candidate(
                            mfe_mae_tracker,
                            training_pulse,
                            observation_ids.get(selected_signal.symbol),
                            selected_signal,
                            snapshots,
                            regime.regime,
                            "high_score_missed",
                            "meta_model_rejected",
                        )
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
                    _record_high_score_missed_event(
                        db,
                        training_pulse,
                        selected_signal,
                        regime.regime,
                        "missing_instrument_rules",
                        min_score_to_trade=config.min_score_to_trade,
                        slot_available=True,
                        risk_approved=False,
                    )
                    _track_mfe_mae_candidate(
                        mfe_mae_tracker,
                        training_pulse,
                        observation_ids.get(selected_signal.symbol),
                        selected_signal,
                        snapshots,
                        regime.regime,
                        "high_score_missed",
                        "missing_instrument_rules",
                    )
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
                    if training_pulse:
                        training_pulse.record_risk_block(risk.block_reason or risk.reason)
                    _record_high_score_missed_event(
                        db,
                        training_pulse,
                        selected_signal,
                        regime.regime,
                        risk.block_reason or risk.reason,
                        min_score_to_trade=config.min_score_to_trade,
                        slot_available=True,
                        risk_approved=False,
                    )
                    _track_mfe_mae_candidate(
                        mfe_mae_tracker,
                        training_pulse,
                        observation_ids.get(selected_signal.symbol),
                        selected_signal,
                        snapshots,
                        regime.regime,
                        "risk_block",
                        risk.block_reason or risk.reason,
                    )
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
                    if config.enable_edge_guard_paper_filter:
                        edge_decision = EdgeGuard(config, db).evaluate_signal(safe_signal, regime.regime, hours=24)
                        if not edge_decision.allows_paper:
                            reason = f"edge_guard_{edge_decision.decision}_{edge_decision.reason}"
                            logger.info("EdgeGuard bloquea paper %s: %s", safe_signal.symbol, reason)
                            db.record_event(
                                "training_edge_guard_block",
                                "edge guard paper block",
                                payload={
                                    "symbol": safe_signal.symbol,
                                    "side": safe_signal.side,
                                    "score": safe_signal.confidence_score,
                                    "decision": edge_decision.decision,
                                    "reason": edge_decision.reason,
                                    "group": edge_decision.matched_group,
                                    "group_type": edge_decision.group_type,
                                },
                            )
                            _record_high_score_missed_event(
                                db,
                                training_pulse,
                                safe_signal,
                                regime.regime,
                                reason,
                                min_score_to_trade=config.min_score_to_trade,
                                slot_available=True,
                                risk_approved=True,
                            )
                            _track_mfe_mae_candidate(
                                mfe_mae_tracker,
                                training_pulse,
                                observation_ids.get(selected_signal.symbol),
                                safe_signal,
                                snapshots,
                                regime.regime,
                                _mfe_source_for_edge_decision(edge_decision.decision),
                                reason,
                            )
                            if feature_logger:
                                feature_logger.update_observation(
                                    observation_ids.get(selected_signal.symbol),
                                    operated=False,
                                    block_reason=reason,
                                )
                            continue
                    try:
                        paper_trader.open_position(safe_signal, risk.risk_amount, risk)
                        if training_pulse:
                            training_pulse.record_paper_open_attempt(safe_signal.symbol, safe_signal.side, True, "")
                        _send_telegram_alert_if_needed(
                            config,
                            telegram_notifier,
                            "PAPER OPEN",
                            f"{safe_signal.symbol} {safe_signal.side} score={safe_signal.confidence_score}",
                        )
                        _record_paper_open_attempt_event(db, safe_signal, True, "")
                    except Exception as exc:
                        logger.warning("Paper open fallo %s %s: %s", safe_signal.symbol, safe_signal.side, exc)
                        if training_pulse:
                            training_pulse.record_paper_open_attempt(safe_signal.symbol, safe_signal.side, False, str(exc))
                        _send_telegram_alert_if_needed(config, telegram_notifier, "PAPER OPEN FAIL", f"{safe_signal.symbol} {safe_signal.side}: {exc}")
                        _record_paper_open_attempt_event(db, safe_signal, False, str(exc))
                        _record_high_score_missed_event(
                            db,
                            training_pulse,
                            safe_signal,
                            regime.regime,
                            str(exc),
                            min_score_to_trade=config.min_score_to_trade,
                            slot_available=False,
                            risk_approved=True,
                        )
                        _track_mfe_mae_candidate(
                            mfe_mae_tracker,
                            training_pulse,
                            observation_ids.get(selected_signal.symbol),
                            safe_signal,
                            snapshots,
                            regime.regime,
                            "paper_open_fail",
                            str(exc),
                        )
                        continue
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

            decision_ms = (time.monotonic() - decision_start) * 1000.0
            latest_prices = {symbol: snap.current_price for symbol, snap in snapshots.items() if snap.current_price}
            position_manager.monitor(latest_prices)
            open_positions = position_manager.sync_open_positions()
            if training_pulse:
                training_pulse.record_open_paper_positions(len(open_positions) if config.paper_trading else 0)
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
            last_research_report_at = _emit_research_auto_report_if_due(
                config,
                research_engine,
                logger,
                last_research_report_at,
                time.monotonic(),
            )
            last_full_research_report_at = _emit_full_research_auto_report_if_due(
                config,
                full_research_reporter,
                logger,
                last_full_research_report_at,
                time.monotonic(),
            )
            last_research_autopilot_at = _emit_research_autopilot_if_due(
                config,
                research_autopilot,
                logger,
                last_research_autopilot_at,
                time.monotonic(),
            )
            last_daily_summary_at = _emit_daily_research_summary_if_due(
                config,
                daily_summary,
                logger,
                last_daily_summary_at,
                time.monotonic(),
            )
            last_memory_log_at = _log_memory_if_due(config, logger, training_pulse, last_memory_log_at, time.monotonic())
            last_mfe_mae_debug_at = _emit_mfe_mae_debug_if_due(
                config,
                mfe_mae_tracker,
                training_pulse,
                logger,
                last_mfe_mae_debug_at,
                time.monotonic(),
            )
            last_lightweight_paper_reconcile_at = _reconcile_paper_if_due(
                config,
                db,
                paper_trader,
                logger,
                training_pulse,
                telegram_notifier,
                last_lightweight_paper_reconcile_at,
                time.monotonic(),
            )
            last_data_vault_backup_at = _data_vault_backup_if_due(
                config,
                db,
                logger,
                last_data_vault_backup_at,
                time.monotonic(),
                data_vault_backup_running,
            )
            if training_pulse:
                training_pulse.record_cycle_ok()
            _emit_training_pulse_if_due(config, db, training_pulse, logger, time.monotonic())
            last_telegram_pulse_at = _send_telegram_pulse_if_due(
                config,
                telegram_notifier,
                training_pulse,
                last_telegram_pulse_at,
                time.monotonic(),
            )
            _record_latency_metrics(db, {
                "cycle_total_ms": (time.monotonic() - cycle_timer_start) * 1000.0,
                "market_fetch_ms": market_fetch_ms,
                "signal_generation_ms": signal_generation_ms,
                "decision_ms": decision_ms,
            })
            elapsed = time.time() - cycle_start
            sleep_for = max(1, config.scan_interval_seconds - elapsed)
            time.sleep(sleep_for)
        except Exception as exc:
            logger.exception("Error en ciclo principal")
            db.record_event("main_loop_error", str(exc), level="ERROR")
            if training_pulse:
                training_pulse.record_cycle_error(str(exc))
                logger.info("%s", training_pulse.to_text(config))
                db.record_event("training_pulse", "training pulse after cycle error", level="ERROR", payload={"error": str(exc)[:300]})
                _send_telegram_alert_if_needed(config, telegram_notifier, "CYCLE ERROR", str(exc))
                if config.training_pulse_reset_after_emit:
                    training_pulse.reset_window()
            if _is_429_error(str(exc)):
                db.record_event("training_api_429", "Bitget 429/rate limit", level="WARNING", payload={"error": str(exc)[:300]})
                _send_telegram_alert_if_needed(config, telegram_notifier, "CHECK_RATE_LIMIT", str(exc))
                time.sleep(max(1, config.bitget_429_backoff_seconds))
            telegram.critical(f"Error en ciclo principal: {exc}")
            risk_manager.register_api_failure()
            time.sleep(config.fast_scan_interval_seconds)

    logger.info("Apagado solicitado. Cerrando limpio.")
    telegram.send("Bot detenido limpiamente.")


def _emit_research_auto_report_if_due(config, research_engine: ResearchEngine | None, logger, last_report_at: float, now: float) -> float:
    if not config.enable_research_auto_report or research_engine is None:
        return last_report_at
    interval_seconds = max(1, config.research_report_interval_minutes) * 60
    if last_report_at > 0 and now - last_report_at < interval_seconds:
        return last_report_at
    try:
        report = research_engine.build_report()
        logger.info("===== RESEARCH REPORT START =====\n%s\n===== RESEARCH REPORT END =====", report)
    except Exception as exc:
        logger.warning("No se pudo generar research auto-report: %s", exc)
    return now


def _emit_full_research_auto_report_if_due(
    config,
    reporter: FullResearchReporter | None,
    logger,
    last_report_at: float,
    now: float,
    initial: bool = False,
) -> float:
    if not config.enable_full_research_auto_report or reporter is None:
        return last_report_at
    interval_seconds = max(1, config.full_research_report_interval_minutes) * 60
    if not initial and last_report_at > 0 and now - last_report_at < interval_seconds:
        return last_report_at
    emitted_start = False
    generated = False
    try:
        logger.info(START_MARKER)
        emitted_start = True
        mode = _full_research_report_mode(config, initial)
        try:
            report = reporter.build_report(mode=mode)
        except TypeError:
            report = reporter.build_report()
        logger.info("%s", _strip_full_report_markers(str(report)))
        generated = True
    except Exception as exc:
        logger.warning("No se pudo generar full research auto-report: %s", exc)
    finally:
        if emitted_start:
            logger.info(END_MARKER)
    if generated:
        if initial:
            logger.info("Full research report inicial generado")
        else:
            logger.info("Full research report periódico generado")
    return now


def _emit_research_autopilot_if_due(
    config,
    autopilot: ResearchAutopilot | None,
    logger,
    last_run_at: float,
    now: float,
) -> float:
    if not config.enable_research_autopilot or autopilot is None:
        return last_run_at
    interval_seconds = max(1, config.research_autopilot_interval_minutes) * 60
    if last_run_at > 0 and now - last_run_at < interval_seconds:
        return last_run_at
    if getattr(autopilot, "running", False):
        logger.info("Research autopilot ya esta ejecutandose; se omite este ciclo.")
        return last_run_at

    def _run() -> None:
        autopilot.running = True
        try:
            logger.info("%s", autopilot.run_once().to_text())
        except Exception as exc:
            logger.warning("Research autopilot fallo sin detener el bot: %s", exc)
        finally:
            autopilot.running = False

    Thread(target=_run, name="research-autopilot", daemon=True).start()
    logger.info("Research autopilot programado en background.")
    return now


def _emit_daily_research_summary_if_due(
    config,
    summary: DailyResearchSummary | None,
    logger,
    last_summary_at: float,
    now: float,
    initial: bool = False,
) -> float:
    if not config.enable_daily_research_summary or summary is None:
        return last_summary_at
    interval_seconds = max(1, config.daily_research_summary_interval_hours) * 3600
    if not initial and last_summary_at > 0 and now - last_summary_at < interval_seconds:
        return last_summary_at
    try:
        logger.info("%s", summary.build(hours=config.daily_research_summary_window_hours))
        if initial:
            logger.info("Daily research summary inicial generado")
        else:
            logger.info("Daily research summary periodico generado")
    except Exception as exc:
        logger.warning("No se pudo generar daily research summary: %s", exc)
    return now


def _emit_training_pulse_if_due(config, db: Database, pulse: TrainingPulse | None, logger, now_monotonic: float) -> None:
    if not config.enable_training_pulse or pulse is None:
        return
    now_dt = _monotonic_to_utc(now_monotonic)
    if not pulse.should_emit(now_dt, config.training_pulse_interval_minutes):
        return
    logger.info("%s", pulse.to_text(config))
    db.record_event("training_pulse", "training pulse periodic", payload={"mode": config.mode})
    if config.training_pulse_reset_after_emit:
        pulse.reset_window()


def _send_telegram_pulse_if_due(
    config,
    notifier: TelegramNotifier,
    pulse: TrainingPulse | None,
    last_sent_at: float,
    now: float,
) -> float:
    if pulse is None or not notifier.enabled() or not notifier.configured():
        return last_sent_at
    interval_seconds = max(1, int(config.telegram_pulse_interval_minutes or 10)) * 60
    if last_sent_at > 0 and now - last_sent_at < interval_seconds:
        return last_sent_at
    text = pulse.to_text(config, update_timestamp=False)
    notifier.send_training_pulse(text)
    data = pulse.to_dict(config)
    diagnosis_text = " ".join(data.get("diagnosis", []))
    if (
        data.get("next_action") == "CHECK_RATE_LIMIT"
        or "CHECK_SLOT" in diagnosis_text
        or int(data.get("health", {}).get("api_429_count", 0) or 0) > 0
        or int(data.get("health", {}).get("cycles_error", 0) or 0) > 0
        or float(data.get("health", {}).get("memory_mb_max", 0.0) or 0.0) > 700
        or bool(data.get("safety", {}).get("live_trading", False))
    ):
        notifier.send_alert("TRAINING ALERT", text)
    return now


def _send_telegram_alert_if_needed(config, notifier: TelegramNotifier, title: str, text: str) -> None:
    if notifier.enabled() and notifier.configured() and config.telegram_alerts_enabled:
        notifier.send_alert(title, text)


def _reconcile_paper_if_due(
    config,
    db: Database,
    paper_trader: PaperTrader | None,
    logger,
    pulse: TrainingPulse | None,
    notifier: TelegramNotifier | None,
    last_reconcile_at: float,
    now: float,
) -> float:
    if not config.paper_trading or paper_trader is None or not config.lightweight_paper_reconcile_on_start:
        return last_reconcile_at
    interval_seconds = max(1, config.lightweight_paper_reconcile_interval_minutes) * 60
    if last_reconcile_at > 0 and now - last_reconcile_at < interval_seconds:
        return last_reconcile_at
    try:
        result = PaperReconciler(config, db, logger).reconcile()
        logger.info("%s", result.to_text())
        _record_paper_reconcile_event(db, result)
        if pulse:
            pulse.record_paper_reconcile(result)
        if result.paper_trades_closed_by_label or result.paper_trades_closed_by_time:
            paper_trader.positions.clear()
            paper_trader.load_open_positions_from_db()
            if notifier is not None:
                _send_telegram_alert_if_needed(
                    config,
                    notifier,
                    "PAPER RECONCILE",
                    (
                        f"closed_label={result.paper_trades_closed_by_label} "
                        f"closed_time={result.paper_trades_closed_by_time} "
                        f"left_open={result.paper_trades_left_open}"
                    ),
                )
            if pulse:
                logger.info("%s", pulse.to_text(config))
                db.record_event("training_pulse", "training pulse after paper reconcile", payload={"closed": True})
                if config.training_pulse_reset_after_emit:
                    pulse.reset_window()
    except Exception as exc:
        logger.warning("Paper reconcile periodico fallo sin detener el bot: %s", exc)
    return now


def _strip_full_report_markers(report: str) -> str:
    lines = []
    for line in report.splitlines():
        if line.strip() in {
            START_MARKER,
            END_MARKER,
        }:
            continue
        lines.append(line)
    return "\n".join(lines)


def _record_paper_reconcile_event(db: Database, result) -> None:
    db.record_event(
        "training_paper_reconcile",
        "paper reconcile",
        payload={
            "paper_open_before": result.paper_open_before,
            "closed_by_label": result.paper_trades_closed_by_label,
            "closed_by_time": result.paper_trades_closed_by_time,
            "left_open": result.paper_trades_left_open,
            "paper_open_after": result.paper_open_after,
            "errors": result.errors,
        },
    )


def _record_slot_block_event(db: Database, pulse: TrainingPulse | None, reason: str) -> None:
    if pulse:
        pulse.record_slot_block(reason)
    db.record_event("training_slot_block", "slot block", payload={"reason": str(reason)[:300]})


def _record_high_score_missed_event(
    db: Database,
    pulse: TrainingPulse | None,
    signal: Signal,
    market_regime: str,
    reason: str,
    *,
    min_score_to_trade: int,
    slot_available: bool,
    risk_approved: bool,
) -> None:
    if signal.confidence_score < min_score_to_trade or signal.side not in {"LONG", "SHORT"}:
        return
    if pulse:
        pulse.record_high_score_missed(reason)
    db.record_event(
        "training_high_score_missed",
        "high score signal missed",
        payload={
            "symbol": signal.symbol,
            "side": signal.side,
            "score": signal.confidence_score,
            "market_regime": market_regime,
            "reason": str(reason)[:300],
            "slot_available": bool(slot_available),
            "risk_approved": bool(risk_approved),
            "timestamp": iso_utc(),
        },
    )


def _record_paper_open_attempt_event(db: Database, signal: Signal, success: bool, reason: str) -> None:
    db.record_event(
        "training_paper_open_attempt",
        "paper open attempt",
        payload={
            "symbol": signal.symbol,
            "side": signal.side,
            "score": signal.confidence_score,
            "success": bool(success),
            "reason": str(reason)[:300],
        },
    )


def _track_mfe_mae_candidate(
    tracker: MfeMaeTracker | None,
    pulse: TrainingPulse | None,
    observation_id: int | None,
    signal: Signal,
    snapshots: dict[str, MarketSnapshot],
    market_regime: str,
    source: str,
    reason: str,
) -> None:
    if tracker is None:
        return
    tracker.register_signal(
        observation_id=observation_id,
        signal=signal,
        snapshot=snapshots.get(signal.symbol),
        market_regime=market_regime,
        source=source,
        reject_reason=reason,
    )
    if pulse:
        pulse.record_mfe_mae(tracker.debug_result())


def _is_slot_block_reason(reason: str) -> bool:
    text = str(reason or "").lower()
    return "slot" in text or "sin slots" in text or "posicion" in text or "posición" in text


def _mfe_source_for_reject(reason: str) -> str:
    text = str(reason or "").lower()
    if "notional_deviation" in text or "notional" in text:
        return "notional_deviation"
    if "mÃ¡ximo de posiciones" in text or "maximo de posiciones" in text or "maximum positions" in text:
        return "max_positions"
    if "concentraci" in text or "mejor seÃ±al" in text or "mejor señal" in text:
        return "best_signal_concentration"
    if _is_slot_block_reason(text):
        return "max_positions"
    if "regime" in text or "rÃ©gimen" in text or "regimen" in text or "choppy" in text or "range" in text:
        return "regime_block"
    return "allocator_reject"


def _mfe_source_for_edge_decision(decision: str) -> str:
    text = str(decision or "").upper()
    if text == "SHADOW_ONLY":
        return "edge_guard_shadow"
    if text == "WATCH_ONLY":
        return "edge_guard_watch"
    return "edge_guard_block"


def _is_429_error(text: str) -> bool:
    lowered = str(text or "").lower()
    return "429" in lowered or "rate limit" in lowered


def _monotonic_to_utc(_: float):
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _full_research_report_mode(config, initial: bool) -> str:
    if initial:
        return config.full_research_startup_mode
    if config.full_research_report_mode == "heavy" and config.full_research_heavy_report_enabled:
        return "heavy"
    return "compact"


def _should_print_radar(config, cycle_count: int, signals: list[Signal]) -> bool:
    every = max(1, int(config.radar_log_every_n_cycles or 1))
    if cycle_count % every == 0:
        return True
    return any(
        signal.side != "NO_TRADE" and signal.confidence_score >= config.min_score_to_alert
        for signal in signals
    )


def _read_memory_mb() -> float:
    try:
        import resource  # type: ignore

        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss = float(getattr(usage, "ru_maxrss", 0.0) or 0.0)
        if sys.platform == "darwin":
            return rss / (1024.0 * 1024.0)
        return rss / 1024.0
    except Exception:
        return 0.0


def _log_memory_if_due(config, logger, pulse: TrainingPulse | None, last_log_at: float, now: float) -> float:
    interval_seconds = max(1, int(config.memory_log_interval_minutes or 5)) * 60
    if last_log_at > 0 and now - last_log_at < interval_seconds:
        return last_log_at
    rss_mb = _read_memory_mb()
    if rss_mb > 0:
        logger.info("Worker memory lightweight check: max_rss_mb=%.2f", rss_mb)
        if pulse:
            pulse.record_memory(rss_mb)
    return now


def _record_latency_metrics(db: Database, metrics: dict[str, float]) -> None:
    for name, value in metrics.items():
        try:
            db.record_latency_metric(name, float(value or 0.0), component="main_loop")
        except Exception:
            return


def _data_vault_backup_if_due(
    config,
    db: Database,
    logger,
    last_backup_at: float,
    now: float,
    state: dict[str, bool],
) -> float:
    if not getattr(config, "enable_data_vault_backup", False):
        return last_backup_at
    interval_seconds = max(1, int(getattr(config, "data_vault_backup_interval_hours", 24) or 24)) * 3600
    if last_backup_at > 0 and now - last_backup_at < interval_seconds:
        return last_backup_at
    if state.get("running"):
        return last_backup_at

    def run_backup() -> None:
        state["running"] = True
        try:
            vault = DataVault(config, db, logger)
            logger.info("DATA VAULT BACKUP START")
            result = vault.export(hours=config.data_vault_backup_lookback_hours, upload=True)
            external = result.get("external_upload", {}) or {}
            logger.info(
                "DATA VAULT BACKUP END uploaded=%s verified=%s size_mb=%.2f file=%s",
                str(bool(external.get("uploaded"))).lower(),
                str(bool(external.get("verified"))).lower(),
                float(result.get("local_size_bytes") or 0.0) / (1024.0 * 1024.0),
                result.get("file"),
            )
            if external.get("attempted") and not external.get("uploaded"):
                logger.warning("DATA VAULT BACKUP WARNING sanitized_error=%s", external.get("sanitized_error", "unknown"))
        except Exception as exc:
            logger.warning("Data vault backup fallo sin detener worker: %s", exc)
        finally:
            state["running"] = False

    Thread(target=run_backup, name="data-vault-backup", daemon=True).start()
    return now


def _data_vault_startup_backup_if_needed(
    config,
    db: Database,
    logger,
    last_backup_at: float,
    now: float,
    state: dict[str, bool],
) -> float:
    if not getattr(config, "enable_data_vault_backup", False):
        return last_backup_at
    if not getattr(config, "data_vault_auto_backup_on_start", True):
        return last_backup_at
    try:
        status = DataVault(config, db, logger).status()
        age = status.get("latest_backup_age_hours")
        min_age = max(0, int(getattr(config, "data_vault_auto_backup_on_start_min_age_hours", 12) or 12))
        if age is not None and float(age) < min_age:
            return last_backup_at
        logger.info("DATA VAULT BACKUP START startup=true")
        return _data_vault_backup_if_due(config, db, logger, 0.0, now, state)
    except Exception as exc:
        logger.warning("Data vault startup backup check fallo sin detener worker: %s", exc)
        return last_backup_at


def _emit_mfe_mae_debug_if_due(
    config,
    tracker: MfeMaeTracker | None,
    pulse: TrainingPulse | None,
    logger,
    last_log_at: float,
    now: float,
) -> float:
    if tracker is None or not config.enable_mfe_mae_capture:
        return last_log_at
    interval_seconds = max(1, int(config.mfe_mae_debug_log_every_minutes or 10)) * 60
    if last_log_at > 0 and now - last_log_at < interval_seconds:
        return last_log_at
    result = tracker.debug_result()
    if pulse:
        pulse.record_mfe_mae(result)
    logger.info("%s", tracker.debug_text())
    return now


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


def _label_matured_observations(config, db: Database, labeler: TripleBarrierLabeler, snapshots: dict[str, MarketSnapshot], logger) -> dict[str, int]:
    counts = {"total": 0, "TIME": 0, "SL": 0, "TP1": 0, "TP2": 0}
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
            barrier = str(outcome.first_barrier_hit or "TIME").upper()
            if barrier not in {"TIME", "SL", "TP1", "TP2"}:
                barrier = "TIME"
            counts["total"] += 1
            counts[barrier] += 1
            if config.label_log_individual:
                logger.info(
                    "Signal label %s obs=%s label=%s barrier=%s bars=%s",
                    observation.get("symbol"),
                    observation.get("id"),
                    outcome.label,
                    outcome.first_barrier_hit,
                    outcome.bars_to_outcome,
                )
        if counts["total"] and not config.label_log_individual:
            logger.info(
                "Signal labels summary: total=%s TIME=%s SL=%s TP1=%s TP2=%s",
                counts["total"],
                counts["TIME"],
                counts["SL"],
                counts["TP1"],
                counts["TP2"],
            )
    except Exception as exc:
        logger.warning("No se pudieron etiquetar observaciones pendientes: %s", exc)
    return counts


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
