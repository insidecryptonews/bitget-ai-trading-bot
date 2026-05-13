from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - production installs python-dotenv from requirements.txt
    def load_dotenv(*args, **kwargs):
        return False

from .utils import env_bool, env_decimal, env_float, env_int, parse_csv_symbols


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class BotConfig:
    bitget_api_key: str = ""
    bitget_api_secret: str = ""
    bitget_passphrase: str = ""
    bitget_base_url: str = "https://api.bitget.com"
    enable_training_dashboard: bool = True
    dashboard_auth_token: str = ""
    dashboard_refresh_seconds: int = 10
    product_type: str = "USDT-FUTURES"
    margin_coin: str = "USDT"
    margin_mode: str = "isolated"
    force_isolated_margin: bool = True
    disallow_crossed_margin: bool = True
    auto_margin: bool = False

    paper_trading: bool = True
    live_trading: bool = False
    dry_run: bool = True

    starting_capital_usdt: float = 40.0
    risk_profile: str = "aggressive_small_account"
    default_leverage: int = 3
    max_leverage: int = 5
    max_risk_per_trade: float = 0.025
    max_daily_loss: float = 0.08
    max_weekly_loss: float = 0.18
    max_margin_usage_per_trade: float = 0.45
    max_total_margin_usage: float = 0.75
    margin_safety_buffer_usdt: Decimal = Decimal("1.0")
    min_free_margin_after_trade: Decimal = Decimal("0.20")
    min_stop_distance_pct: float = 0.006
    max_notional_per_trade_small_account: float = 120.0
    max_over_notional_deviation_pct: float = 0.05
    max_under_notional_deviation_pct: float = 0.20
    use_fixed_trade_margin: bool = True
    trade_margin_usdt: Decimal = Decimal("12.00")
    max_trade_margin_usdt: Decimal = Decimal("15.00")
    min_trade_margin_usdt: Decimal = Decimal("5.00")
    max_open_positions: int = 1
    small_account_max_open_positions: int = 1
    allow_second_position_small_account: bool = False
    max_positions_per_symbol: int = 1
    max_correlated_positions: int = 1
    min_score_to_trade: int = 72
    min_score_excellent: int = 85
    min_score_to_alert: int = 62
    min_risk_reward: float = 1.4

    scan_interval_seconds: int = 30
    fast_scan_interval_seconds: int = 10
    position_monitor_interval_seconds: int = 5
    main_timeframe: str = "5m"
    confirmation_timeframe: str = "15m"
    higher_timeframe: str = "1h"

    enable_circuit_breakers: bool = True
    max_consecutive_losses: int = 3
    cooldown_after_losses_minutes: int = 180
    require_stop_loss: bool = True
    require_take_profit: bool = True
    close_if_protection_fails: bool = True
    stop_trading_below_balance_usdt: float = 20.0

    database_url: str = ""
    use_postgres_if_available: bool = True
    port: int = 8080

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    enable_telegram_notifier: bool = False
    telegram_pulse_interval_minutes: int = 10
    telegram_alerts_enabled: bool = True
    telegram_send_training_summary_every_hours: int = 6
    telegram_send_files: bool = False
    telegram_max_message_chars: int = 3500
    telegram_min_alert_interval_seconds: int = 120

    enable_news_intel: bool = False
    news_api_key: str = ""
    sentiment_api_key: str = ""

    enable_feature_logging: bool = True
    enable_signal_labeling: bool = True
    worker_lightweight_mode: bool = True
    enable_training_pulse: bool = True
    training_pulse_interval_minutes: int = 10
    training_pulse_max_lines: int = 80
    training_pulse_top_n: int = 5
    training_pulse_log_on_start: bool = True
    training_pulse_reset_after_emit: bool = True
    label_log_individual: bool = False
    enable_meta_model: bool = False
    meta_model_train_on_start: bool = False
    enable_research_auto_report: bool = True
    research_report_interval_minutes: int = 60
    enable_full_research_auto_report: bool = True
    full_research_report_interval_minutes: int = 60
    full_research_report_mode: str = "compact"
    full_research_startup_mode: str = "compact"
    full_research_startup_enabled: bool = False
    full_research_section_timeout_seconds: int = 10
    full_research_heavy_report_enabled: bool = False
    enable_phase2_persist: bool = False
    phase2_persist_batch_size: int = 250
    phase2_persist_max_labels_per_run: int = 5000
    enable_paper_reconcile_on_start: bool = False
    lightweight_paper_reconcile_on_start: bool = True
    lightweight_paper_reconcile_interval_minutes: int = 30
    bitget_429_backoff_seconds: int = 60
    enable_research_autopilot: bool = False
    research_autopilot_interval_minutes: int = 60
    research_autopilot_phase2_limit_per_run: int = 5000
    research_autopilot_batch_size: int = 250
    enable_virtual_position_research: bool = True
    virtual_max_concurrent_positions: int = 1000
    virtual_portfolio_max_labels_per_run: int = 50000
    enable_daily_research_summary: bool = True
    daily_research_summary_on_start: bool = False
    daily_research_summary_interval_hours: int = 6
    daily_research_summary_window_hours: int = 24
    stale_paper_trade_hours: int = 12
    enable_kronos_research: bool = False
    kronos_model_name: str = "NeoQuasar/Kronos-mini"
    kronos_tokenizer_name: str = "NeoQuasar/Kronos-Tokenizer-base"
    kronos_device: str = "auto"
    kronos_lookback: int = 256
    kronos_pred_len: int = 12
    kronos_sample_count: int = 3
    kronos_top_p: float = 0.9
    kronos_temperature: float = 1.0
    kronos_max_symbols_per_run: int = 5
    kronos_timeout_seconds: int = 30
    meta_model_min_samples: int = 300
    meta_model_min_positives: int = 50
    meta_model_min_negatives: int = 50
    meta_min_probability: float = 0.58
    meta_model_mode: str = "observe_only"
    max_holding_bars: int = 48
    label_use_tp2: bool = False
    radar_log_every_n_cycles: int = 3
    memory_log_interval_minutes: int = 5

    symbols: list[str] = field(default_factory=lambda: [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "XRPUSDT",
        "DOGEUSDT",
        "BNBUSDT",
        "LINKUSDT",
        "AVAXUSDT",
        "ADAUSDT",
        "DOTUSDT",
    ])

    def __post_init__(self) -> None:
        margin_mode = self.margin_mode.lower().strip()
        meta_model_mode = self.meta_model_mode.lower().strip()
        object.__setattr__(self, "margin_mode", margin_mode)
        object.__setattr__(self, "meta_model_mode", meta_model_mode)
        object.__setattr__(self, "full_research_report_mode", self.full_research_report_mode.lower().strip())
        object.__setattr__(self, "full_research_startup_mode", self.full_research_startup_mode.lower().strip())
        object.__setattr__(self, "margin_coin", self.margin_coin.upper().strip())
        for field_name in (
            "trade_margin_usdt",
            "max_trade_margin_usdt",
            "min_trade_margin_usdt",
            "margin_safety_buffer_usdt",
            "min_free_margin_after_trade",
        ):
            object.__setattr__(self, field_name, Decimal(str(getattr(self, field_name))))

        if self.disallow_crossed_margin and margin_mode != "isolated":
            raise ValueError("DISALLOW_CROSSED_MARGIN=true: MARGIN_MODE debe ser isolated.")
        if self.live_trading and not self.force_isolated_margin:
            raise ValueError("LIVE_TRADING=true requiere FORCE_ISOLATED_MARGIN=true.")
        if self.live_trading and margin_mode != "isolated":
            raise ValueError("LIVE_TRADING=true requiere MARGIN_MODE=isolated.")
        if self.trade_margin_usdt > self.max_trade_margin_usdt:
            raise ValueError("TRADE_MARGIN_USDT no puede superar MAX_TRADE_MARGIN_USDT.")
        if self.min_trade_margin_usdt > self.max_trade_margin_usdt:
            raise ValueError("MIN_TRADE_MARGIN_USDT no puede superar MAX_TRADE_MARGIN_USDT.")
        if meta_model_mode not in {"off", "observe_only", "filter"}:
            raise ValueError("META_MODEL_MODE debe ser off, observe_only o filter.")
        if self.full_research_report_mode not in {"compact", "heavy"}:
            raise ValueError("FULL_RESEARCH_REPORT_MODE debe ser compact o heavy.")
        if self.full_research_startup_mode not in {"compact", "heavy"}:
            raise ValueError("FULL_RESEARCH_STARTUP_MODE debe ser compact o heavy.")
        if not 0 <= self.meta_min_probability <= 1:
            raise ValueError("META_MIN_PROBABILITY debe estar entre 0 y 1.")
        if self.kronos_lookback <= 0 or self.kronos_pred_len <= 0:
            raise ValueError("KRONOS_LOOKBACK y KRONOS_PRED_LEN deben ser positivos.")

    @property
    def mode(self) -> str:
        if self.paper_trading:
            return "paper"
        if self.dry_run:
            return "dry_run"
        if self.live_trading:
            return "live"
        return "dry_run"

    @property
    def can_send_real_orders(self) -> bool:
        return self.live_trading and not self.paper_trading and not self.dry_run

    @property
    def has_bitget_credentials(self) -> bool:
        return bool(self.bitget_api_key and self.bitget_api_secret and self.bitget_passphrase)

    @property
    def is_small_account_config(self) -> bool:
        return self.risk_profile == "aggressive_small_account"


def load_config(load_dotenv_file: bool = True) -> BotConfig:
    if load_dotenv_file:
        load_dotenv(PROJECT_ROOT / ".env")
        load_dotenv()

    max_leverage = min(env_int(os.getenv("MAX_LEVERAGE"), 5), 5)
    default_leverage = min(env_int(os.getenv("DEFAULT_LEVERAGE"), 3), max_leverage)
    worker_lightweight_mode = env_bool(os.getenv("WORKER_LIGHTWEIGHT_MODE"), True)

    paper_trading = env_bool(os.getenv("PAPER_TRADING"), True)
    live_trading = env_bool(os.getenv("LIVE_TRADING"), False)
    dry_run = env_bool(os.getenv("DRY_RUN"), True)
    enable_feature_logging = env_bool(os.getenv("ENABLE_FEATURE_LOGGING"), True)
    enable_signal_labeling = env_bool(os.getenv("ENABLE_SIGNAL_LABELING"), True)
    enable_training_pulse = env_bool(os.getenv("ENABLE_TRAINING_PULSE"), True)
    enable_meta_model = env_bool(os.getenv("ENABLE_META_MODEL"), False)
    enable_research_auto_report = env_bool(os.getenv("ENABLE_RESEARCH_AUTO_REPORT"), True)
    enable_full_research_auto_report = env_bool(os.getenv("ENABLE_FULL_RESEARCH_AUTO_REPORT"), True)
    enable_daily_research_summary = env_bool(os.getenv("ENABLE_DAILY_RESEARCH_SUMMARY"), True)
    enable_research_autopilot = env_bool(os.getenv("ENABLE_RESEARCH_AUTOPILOT"), False)
    enable_phase2_persist = env_bool(os.getenv("ENABLE_PHASE2_PERSIST"), False)
    enable_kronos_research = env_bool(os.getenv("ENABLE_KRONOS_RESEARCH"), False)
    enable_paper_reconcile_on_start = env_bool(os.getenv("ENABLE_PAPER_RECONCILE_ON_START"), False)
    meta_model_train_on_start = env_bool(os.getenv("META_MODEL_TRAIN_ON_START"), False)
    full_research_startup_enabled = env_bool(os.getenv("FULL_RESEARCH_STARTUP_ENABLED"), False)
    daily_research_summary_on_start = env_bool(os.getenv("DAILY_RESEARCH_SUMMARY_ON_START"), False)

    if worker_lightweight_mode:
        paper_trading = True
        live_trading = False
        dry_run = True
        enable_feature_logging = True
        enable_signal_labeling = True
        enable_training_pulse = True
        enable_meta_model = False
        enable_research_auto_report = False
        enable_full_research_auto_report = False
        enable_daily_research_summary = False
        enable_research_autopilot = False
        enable_phase2_persist = False
        enable_kronos_research = False
        enable_paper_reconcile_on_start = False
        meta_model_train_on_start = False
        full_research_startup_enabled = False
        daily_research_summary_on_start = False

    return BotConfig(
        bitget_api_key=os.getenv("BITGET_API_KEY", ""),
        bitget_api_secret=os.getenv("BITGET_API_SECRET", ""),
        bitget_passphrase=os.getenv("BITGET_PASSPHRASE", ""),
        bitget_base_url=os.getenv("BITGET_BASE_URL", "https://api.bitget.com").rstrip("/"),
        enable_training_dashboard=env_bool(os.getenv("ENABLE_TRAINING_DASHBOARD"), True),
        dashboard_auth_token=os.getenv("DASHBOARD_AUTH_TOKEN", ""),
        dashboard_refresh_seconds=env_int(os.getenv("DASHBOARD_REFRESH_SECONDS"), 10),
        margin_mode=os.getenv("MARGIN_MODE", "isolated"),
        force_isolated_margin=env_bool(os.getenv("FORCE_ISOLATED_MARGIN"), True),
        disallow_crossed_margin=env_bool(os.getenv("DISALLOW_CROSSED_MARGIN"), True),
        auto_margin=env_bool(os.getenv("AUTO_MARGIN"), False),
        paper_trading=paper_trading,
        live_trading=live_trading,
        dry_run=dry_run,
        starting_capital_usdt=env_float(os.getenv("STARTING_CAPITAL_USDT"), 40.0),
        risk_profile=os.getenv("RISK_PROFILE", "aggressive_small_account"),
        default_leverage=default_leverage,
        max_leverage=max_leverage,
        max_risk_per_trade=env_float(os.getenv("MAX_RISK_PER_TRADE"), 0.025),
        max_daily_loss=env_float(os.getenv("MAX_DAILY_LOSS"), 0.08),
        max_weekly_loss=env_float(os.getenv("MAX_WEEKLY_LOSS"), 0.18),
        max_margin_usage_per_trade=env_float(os.getenv("MAX_MARGIN_USAGE_PER_TRADE"), 0.45),
        max_total_margin_usage=env_float(os.getenv("MAX_TOTAL_MARGIN_USAGE"), 0.75),
        margin_safety_buffer_usdt=env_decimal(os.getenv("MARGIN_SAFETY_BUFFER_USDT"), Decimal("1.0")),
        min_free_margin_after_trade=env_decimal(os.getenv("MIN_FREE_MARGIN_AFTER_TRADE"), Decimal("0.20")),
        min_stop_distance_pct=env_float(os.getenv("MIN_STOP_DISTANCE_PCT"), 0.006),
        max_notional_per_trade_small_account=env_float(os.getenv("MAX_NOTIONAL_PER_TRADE_SMALL_ACCOUNT"), 120.0),
        max_over_notional_deviation_pct=env_float(os.getenv("MAX_OVER_NOTIONAL_DEVIATION_PCT"), 0.05),
        max_under_notional_deviation_pct=env_float(os.getenv("MAX_UNDER_NOTIONAL_DEVIATION_PCT"), 0.20),
        use_fixed_trade_margin=env_bool(os.getenv("USE_FIXED_TRADE_MARGIN"), True),
        trade_margin_usdt=env_decimal(os.getenv("TRADE_MARGIN_USDT"), Decimal("12.00")),
        max_trade_margin_usdt=env_decimal(os.getenv("MAX_TRADE_MARGIN_USDT"), Decimal("15.00")),
        min_trade_margin_usdt=env_decimal(os.getenv("MIN_TRADE_MARGIN_USDT"), Decimal("5.00")),
        max_open_positions=env_int(os.getenv("MAX_OPEN_POSITIONS"), 1),
        small_account_max_open_positions=env_int(os.getenv("SMALL_ACCOUNT_MAX_OPEN_POSITIONS"), 1),
        allow_second_position_small_account=env_bool(os.getenv("ALLOW_SECOND_POSITION_SMALL_ACCOUNT"), False),
        max_positions_per_symbol=env_int(os.getenv("MAX_POSITIONS_PER_SYMBOL"), 1),
        max_correlated_positions=env_int(os.getenv("MAX_CORRELATED_POSITIONS"), 1),
        min_score_to_trade=env_int(os.getenv("MIN_SCORE_TO_TRADE"), 72),
        min_score_excellent=env_int(os.getenv("MIN_SCORE_EXCELLENT"), 85),
        min_score_to_alert=env_int(os.getenv("MIN_SCORE_TO_ALERT"), 62),
        min_risk_reward=env_float(os.getenv("MIN_RISK_REWARD"), 1.4),
        scan_interval_seconds=env_int(os.getenv("SCAN_INTERVAL_SECONDS"), 30),
        fast_scan_interval_seconds=env_int(os.getenv("FAST_SCAN_INTERVAL_SECONDS"), 10),
        position_monitor_interval_seconds=env_int(os.getenv("POSITION_MONITOR_INTERVAL_SECONDS"), 5),
        main_timeframe=os.getenv("MAIN_TIMEFRAME", "5m"),
        confirmation_timeframe=os.getenv("CONFIRMATION_TIMEFRAME", "15m"),
        higher_timeframe=os.getenv("HIGHER_TIMEFRAME", "1h"),
        enable_circuit_breakers=env_bool(os.getenv("ENABLE_CIRCUIT_BREAKERS"), True),
        max_consecutive_losses=env_int(os.getenv("MAX_CONSECUTIVE_LOSSES"), 3),
        cooldown_after_losses_minutes=env_int(os.getenv("COOLDOWN_AFTER_LOSSES_MINUTES"), 180),
        require_stop_loss=env_bool(os.getenv("REQUIRE_STOP_LOSS"), True),
        require_take_profit=env_bool(os.getenv("REQUIRE_TAKE_PROFIT"), True),
        close_if_protection_fails=env_bool(os.getenv("CLOSE_IF_PROTECTION_FAILS"), True),
        stop_trading_below_balance_usdt=env_float(os.getenv("STOP_TRADING_BELOW_BALANCE_USDT"), 20.0),
        database_url=os.getenv("DATABASE_URL", ""),
        use_postgres_if_available=env_bool(os.getenv("USE_POSTGRES_IF_AVAILABLE"), True),
        port=env_int(os.getenv("PORT"), 8080),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        enable_telegram_notifier=env_bool(os.getenv("ENABLE_TELEGRAM_NOTIFIER"), False),
        telegram_pulse_interval_minutes=env_int(os.getenv("TELEGRAM_PULSE_INTERVAL_MINUTES"), 10),
        telegram_alerts_enabled=env_bool(os.getenv("TELEGRAM_ALERTS_ENABLED"), True),
        telegram_send_training_summary_every_hours=env_int(os.getenv("TELEGRAM_SEND_TRAINING_SUMMARY_EVERY_HOURS"), 6),
        telegram_send_files=env_bool(os.getenv("TELEGRAM_SEND_FILES"), False),
        telegram_max_message_chars=env_int(os.getenv("TELEGRAM_MAX_MESSAGE_CHARS"), 3500),
        telegram_min_alert_interval_seconds=env_int(os.getenv("TELEGRAM_MIN_ALERT_INTERVAL_SECONDS"), 120),
        enable_news_intel=env_bool(os.getenv("ENABLE_NEWS_INTEL"), False),
        news_api_key=os.getenv("NEWS_API_KEY", ""),
        sentiment_api_key=os.getenv("SENTIMENT_API_KEY", ""),
        enable_feature_logging=enable_feature_logging,
        enable_signal_labeling=enable_signal_labeling,
        worker_lightweight_mode=worker_lightweight_mode,
        enable_training_pulse=enable_training_pulse,
        training_pulse_interval_minutes=env_int(os.getenv("TRAINING_PULSE_INTERVAL_MINUTES"), 10),
        training_pulse_max_lines=env_int(os.getenv("TRAINING_PULSE_MAX_LINES"), 80),
        training_pulse_top_n=env_int(os.getenv("TRAINING_PULSE_TOP_N"), 5),
        training_pulse_log_on_start=env_bool(os.getenv("TRAINING_PULSE_LOG_ON_START"), True),
        training_pulse_reset_after_emit=env_bool(os.getenv("TRAINING_PULSE_RESET_AFTER_EMIT"), True),
        label_log_individual=env_bool(os.getenv("LABEL_LOG_INDIVIDUAL"), False),
        enable_meta_model=enable_meta_model,
        meta_model_train_on_start=meta_model_train_on_start,
        enable_research_auto_report=enable_research_auto_report,
        research_report_interval_minutes=env_int(os.getenv("RESEARCH_REPORT_INTERVAL_MINUTES"), 60),
        enable_full_research_auto_report=enable_full_research_auto_report,
        full_research_report_interval_minutes=env_int(os.getenv("FULL_RESEARCH_REPORT_INTERVAL_MINUTES"), 60),
        full_research_report_mode=os.getenv("FULL_RESEARCH_REPORT_MODE", "compact"),
        full_research_startup_mode=os.getenv("FULL_RESEARCH_STARTUP_MODE", "compact"),
        full_research_startup_enabled=full_research_startup_enabled,
        full_research_section_timeout_seconds=env_int(os.getenv("FULL_RESEARCH_SECTION_TIMEOUT_SECONDS"), 10),
        full_research_heavy_report_enabled=env_bool(os.getenv("FULL_RESEARCH_HEAVY_REPORT_ENABLED"), False),
        enable_phase2_persist=enable_phase2_persist,
        phase2_persist_batch_size=env_int(os.getenv("PHASE2_PERSIST_BATCH_SIZE"), 250),
        phase2_persist_max_labels_per_run=env_int(os.getenv("PHASE2_PERSIST_MAX_LABELS_PER_RUN"), 5000),
        enable_paper_reconcile_on_start=enable_paper_reconcile_on_start,
        lightweight_paper_reconcile_on_start=env_bool(os.getenv("LIGHTWEIGHT_PAPER_RECONCILE_ON_START"), True),
        lightweight_paper_reconcile_interval_minutes=env_int(os.getenv("LIGHTWEIGHT_PAPER_RECONCILE_INTERVAL_MINUTES"), 30),
        bitget_429_backoff_seconds=env_int(os.getenv("BITGET_429_BACKOFF_SECONDS"), 60),
        enable_research_autopilot=enable_research_autopilot,
        research_autopilot_interval_minutes=env_int(os.getenv("RESEARCH_AUTOPILOT_INTERVAL_MINUTES"), 60),
        research_autopilot_phase2_limit_per_run=env_int(os.getenv("RESEARCH_AUTOPILOT_PHASE2_LIMIT_PER_RUN"), 5000),
        research_autopilot_batch_size=env_int(os.getenv("RESEARCH_AUTOPILOT_BATCH_SIZE"), 250),
        enable_virtual_position_research=env_bool(os.getenv("ENABLE_VIRTUAL_POSITION_RESEARCH"), True),
        virtual_max_concurrent_positions=env_int(os.getenv("VIRTUAL_MAX_CONCURRENT_POSITIONS"), 1000),
        virtual_portfolio_max_labels_per_run=env_int(os.getenv("VIRTUAL_PORTFOLIO_MAX_LABELS_PER_RUN"), 50000),
        enable_daily_research_summary=enable_daily_research_summary,
        daily_research_summary_on_start=daily_research_summary_on_start,
        daily_research_summary_interval_hours=env_int(os.getenv("DAILY_RESEARCH_SUMMARY_INTERVAL_HOURS"), 6),
        daily_research_summary_window_hours=env_int(os.getenv("DAILY_RESEARCH_SUMMARY_WINDOW_HOURS"), 24),
        stale_paper_trade_hours=env_int(os.getenv("STALE_PAPER_TRADE_HOURS"), 12),
        enable_kronos_research=enable_kronos_research,
        kronos_model_name=os.getenv("KRONOS_MODEL_NAME", "NeoQuasar/Kronos-mini"),
        kronos_tokenizer_name=os.getenv("KRONOS_TOKENIZER_NAME", "NeoQuasar/Kronos-Tokenizer-base"),
        kronos_device=os.getenv("KRONOS_DEVICE", "auto"),
        kronos_lookback=env_int(os.getenv("KRONOS_LOOKBACK"), 256),
        kronos_pred_len=env_int(os.getenv("KRONOS_PRED_LEN"), 12),
        kronos_sample_count=env_int(os.getenv("KRONOS_SAMPLE_COUNT"), 3),
        kronos_top_p=env_float(os.getenv("KRONOS_TOP_P"), 0.9),
        kronos_temperature=env_float(os.getenv("KRONOS_TEMPERATURE"), 1.0),
        kronos_max_symbols_per_run=env_int(os.getenv("KRONOS_MAX_SYMBOLS_PER_RUN"), 5),
        kronos_timeout_seconds=env_int(os.getenv("KRONOS_TIMEOUT_SECONDS"), 30),
        meta_model_min_samples=env_int(os.getenv("META_MODEL_MIN_SAMPLES"), 300),
        meta_model_min_positives=env_int(os.getenv("META_MODEL_MIN_POSITIVES"), 50),
        meta_model_min_negatives=env_int(os.getenv("META_MODEL_MIN_NEGATIVES"), 50),
        meta_min_probability=env_float(os.getenv("META_MIN_PROBABILITY"), 0.58),
        meta_model_mode=os.getenv("META_MODEL_MODE", "observe_only"),
        max_holding_bars=env_int(os.getenv("MAX_HOLDING_BARS"), 48),
        label_use_tp2=env_bool(os.getenv("LABEL_USE_TP2"), False),
        radar_log_every_n_cycles=env_int(os.getenv("RADAR_LOG_EVERY_N_CYCLES"), 3),
        memory_log_interval_minutes=env_int(os.getenv("MEMORY_LOG_INTERVAL_MINUTES"), 5),
        symbols=parse_csv_symbols(
            os.getenv(
                "SYMBOLS",
                "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,LINKUSDT,AVAXUSDT,ADAUSDT,DOTUSDT",
            )
        ),
    )
