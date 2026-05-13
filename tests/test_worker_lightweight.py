from app.config import BotConfig, PROJECT_ROOT, load_config


LIGHTWEIGHT_ENV_KEYS = [
    "WORKER_LIGHTWEIGHT_MODE",
    "PAPER_TRADING",
    "LIVE_TRADING",
    "DRY_RUN",
    "ENABLE_FEATURE_LOGGING",
    "ENABLE_SIGNAL_LABELING",
    "ENABLE_TRAINING_PULSE",
    "LABEL_LOG_INDIVIDUAL",
    "ENABLE_RESEARCH_AUTO_REPORT",
    "ENABLE_FULL_RESEARCH_AUTO_REPORT",
    "ENABLE_DAILY_RESEARCH_SUMMARY",
    "ENABLE_META_MODEL",
    "META_MODEL_TRAIN_ON_START",
    "ENABLE_RESEARCH_AUTOPILOT",
    "ENABLE_PHASE2_PERSIST",
    "ENABLE_KRONOS_RESEARCH",
    "ENABLE_PAPER_RECONCILE_ON_START",
    "LIGHTWEIGHT_PAPER_RECONCILE_ON_START",
    "FULL_RESEARCH_STARTUP_ENABLED",
    "DAILY_RESEARCH_SUMMARY_ON_START",
]


def load_without_env(monkeypatch, **values):
    for key in LIGHTWEIGHT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))
    return load_config(load_dotenv_file=False)


def test_worker_lightweight_default_true():
    assert BotConfig().worker_lightweight_mode is True


def test_worker_lightweight_forces_full_report_off(monkeypatch):
    config = load_without_env(
        monkeypatch,
        WORKER_LIGHTWEIGHT_MODE="true",
        ENABLE_RESEARCH_AUTO_REPORT="true",
        ENABLE_FULL_RESEARCH_AUTO_REPORT="true",
        ENABLE_DAILY_RESEARCH_SUMMARY="true",
        FULL_RESEARCH_STARTUP_ENABLED="true",
        DAILY_RESEARCH_SUMMARY_ON_START="true",
    )
    assert config.enable_research_auto_report is False
    assert config.enable_full_research_auto_report is False
    assert config.enable_daily_research_summary is False
    assert config.full_research_startup_enabled is False
    assert config.daily_research_summary_on_start is False


def test_worker_lightweight_forces_meta_model_off(monkeypatch):
    config = load_without_env(
        monkeypatch,
        WORKER_LIGHTWEIGHT_MODE="true",
        ENABLE_META_MODEL="true",
        META_MODEL_TRAIN_ON_START="true",
    )
    assert config.enable_meta_model is False
    assert config.meta_model_train_on_start is False


def test_worker_lightweight_forces_research_autopilot_off(monkeypatch):
    config = load_without_env(
        monkeypatch,
        WORKER_LIGHTWEIGHT_MODE="true",
        ENABLE_RESEARCH_AUTOPILOT="true",
        ENABLE_PHASE2_PERSIST="true",
    )
    assert config.enable_research_autopilot is False
    assert config.enable_phase2_persist is False


def test_worker_lightweight_forces_kronos_and_reconcile_off(monkeypatch):
    config = load_without_env(
        monkeypatch,
        WORKER_LIGHTWEIGHT_MODE="true",
        ENABLE_KRONOS_RESEARCH="true",
        ENABLE_PAPER_RECONCILE_ON_START="true",
        LIGHTWEIGHT_PAPER_RECONCILE_ON_START="true",
    )
    assert config.enable_kronos_research is False
    assert config.enable_paper_reconcile_on_start is False
    assert config.lightweight_paper_reconcile_on_start is True


def test_worker_lightweight_keeps_training_pulse_enabled(monkeypatch):
    config = load_without_env(
        monkeypatch,
        WORKER_LIGHTWEIGHT_MODE="true",
        ENABLE_TRAINING_PULSE="true",
    )
    assert config.enable_training_pulse is True


def test_lightweight_paper_reconcile_default_true():
    assert BotConfig().lightweight_paper_reconcile_on_start is True


def test_worker_lightweight_does_not_disable_safe_paper_reconcile(monkeypatch):
    config = load_without_env(
        monkeypatch,
        WORKER_LIGHTWEIGHT_MODE="true",
        ENABLE_PAPER_RECONCILE_ON_START="false",
    )
    assert config.worker_lightweight_mode is True
    assert config.enable_paper_reconcile_on_start is False
    assert config.lightweight_paper_reconcile_on_start is True


def test_worker_lightweight_keeps_safe_trading_modes(monkeypatch):
    config = load_without_env(
        monkeypatch,
        WORKER_LIGHTWEIGHT_MODE="true",
        PAPER_TRADING="false",
        LIVE_TRADING="true",
        DRY_RUN="false",
    )
    assert config.paper_trading is True
    assert config.dry_run is True
    assert config.live_trading is False
    assert config.enable_feature_logging is True
    assert config.enable_signal_labeling is True


def test_worker_lightweight_config_defaults_safe():
    config = BotConfig()
    assert config.paper_trading is True
    assert config.live_trading is False
    assert config.dry_run is True
    assert config.worker_lightweight_mode is True
    assert config.enable_training_pulse is True
    assert config.label_log_individual is False


def test_worker_lightweight_keeps_research_cli_commands_available():
    text = (PROJECT_ROOT / "app" / "research_lab.py").read_text(encoding="utf-8")
    for command in ("daily-summary", "training-summary", "acceleration-plan", "strategy-lab", "reconcile-paper", "virtual-portfolio"):
        assert f'"{command}"' in text
    config = BotConfig(
        worker_lightweight_mode=True,
        enable_daily_research_summary=False,
        enable_full_research_auto_report=False,
        enable_research_autopilot=False,
        enable_meta_model=False,
    )
    assert config.worker_lightweight_mode is True
    assert config.enable_daily_research_summary is False
    assert config.enable_full_research_auto_report is False
    assert config.enable_research_autopilot is False
    assert config.enable_meta_model is False
