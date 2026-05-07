from datetime import datetime, timedelta, timezone
import time

from app.config import BotConfig
from app.database import Database
from app.full_research_report import END_MARKER, START_MARKER, FullResearchReporter
from app.main import _emit_full_research_auto_report_if_due, _full_research_report_mode, _strip_full_report_markers


class DummyLogger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class CaptureLogger(DummyLogger):
    def __init__(self):
        self.messages = []

    def info(self, message, *args, **kwargs):
        self.messages.append(message % args if args else message)

    def warning(self, message, *args, **kwargs):
        self.messages.append(message % args if args else message)


def make_db(tmp_path):
    db = Database(BotConfig(), DummyLogger())
    db.sqlite_path = tmp_path / "full_report.db"
    db.initialize()
    return db


def insert_observation(db, index=0, label=None, barrier="TIME", ret=0.0, shadow=False, operated=False):
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
    observation_id = db.record_signal_observation(
        {
            "timestamp": timestamp.isoformat(),
            "symbol": "BTCUSDT",
            "side": "LONG",
            "strategy_type": "BREAKOUT",
            "confidence_score": 88,
            "market_regime": "TREND_UP",
            "entry_price": 100,
            "stop_loss": 98,
            "take_profit_1": 103,
            "take_profit_2": 105,
            "risk_reward_ratio": 1.5,
            "leverage_recommendation": 3,
            "spread_pct": 0.0005,
            "volume_24h_usdt": 100_000_000,
            "volume_relative": 1.5,
            "rsi_14": 55,
            "normalized_atr": 0.01,
            "momentum_5": 0.01,
            "momentum_15": 0.02,
            "btc_momentum_5": 0.01,
            "btc_momentum_15": 0.02,
            "eth_momentum_5": 0.01,
            "number_of_symbols_bullish": 7,
            "number_of_symbols_bearish": 3,
            "market_risk_on": 1,
            "market_risk_off": 0,
            "shadow_strategy": int(shadow),
            "variant_params_json": '{"reverse": true}' if shadow else "{}",
            "operated": int(operated),
            "selected_by_allocator": int(operated),
            "risk_manager_approved": int(operated),
        }
    )
    if label is not None:
        db.record_signal_label(
            {
                "observation_id": observation_id,
                "label": label,
                "first_barrier_hit": barrier,
                "bars_to_outcome": 4,
                "max_favorable_excursion": max(ret, 0),
                "max_adverse_excursion": min(ret, 0),
                "realized_return_pct": ret,
                "simulated_pnl": ret * 100,
                "would_have_won": int(label == 1),
            }
        )
    return observation_id


def test_full_report_generates_without_labels(tmp_path):
    db = make_db(tmp_path)
    insert_observation(db)
    report = FullResearchReporter(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports").build_report()
    assert "total labels: 0" in report
    assert "Recomendacion: NO LIVE" in report


def test_startup_compact_report_generates_markers(tmp_path):
    db = make_db(tmp_path)
    report = FullResearchReporter(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports").build_report(mode="compact")
    assert "FULL RESEARCH LAB REPORT - COMPACT STARTUP" in report
    assert "recomendacion: NO LIVE" in report


def test_startup_compact_report_skips_heavy_sections(tmp_path):
    db = make_db(tmp_path)
    report = FullResearchReporter(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports").build_report(mode="compact")
    assert "Counterfactual Summary" not in report
    assert "Feature Importance" not in report
    assert "informe pesado omitido" in report


def test_startup_compact_uses_signal_summary_not_full_fetch(tmp_path):
    db = make_db(tmp_path)

    def fail_full_fetch(*args, **kwargs):
        raise AssertionError("compact report no debe cargar todas las senales")

    db.fetch_signal_observations = fail_full_fetch
    report = FullResearchReporter(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports").build_report(mode="compact")
    assert "total senales" in report


def test_startup_compact_uses_label_summary_not_full_fetch(tmp_path):
    db = make_db(tmp_path)
    insert_observation(db, index=1, label=1, barrier="TP1", ret=0.03)
    insert_observation(db, index=2, label=-1, barrier="SL", ret=-0.02)
    insert_observation(db, index=3, label=0, barrier="TIME", ret=0.0, shadow=True)

    def fail_full_fetch(*args, **kwargs):
        raise AssertionError("compact report no debe cargar todas las labels")

    db.fetch_labeled_signal_rows = fail_full_fetch
    report = FullResearchReporter(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports").build_report(mode="compact")
    assert "total labels: 3" in report
    assert "labels normales: 2" in report
    assert "labels shadow: 1" in report
    assert "TP1 count: 1" in report
    assert "SL count: 1" in report
    assert "TIME count: 1" in report
    assert "profit factor aproximado: 1.50" in report
    assert "win rate real sobre labels decisivas: 50.0%" in report


def test_startup_compact_labels_no_timeout_with_many_labels(tmp_path):
    db = make_db(tmp_path)
    for index in range(600):
        if index % 10 == 0:
            insert_observation(db, index=index, label=1, barrier="TP1", ret=0.03)
        elif index % 10 in {1, 2}:
            insert_observation(db, index=index, label=-1, barrier="SL", ret=-0.02)
        else:
            insert_observation(db, index=index, label=0, barrier="TIME", ret=0.0)
    logger = CaptureLogger()
    config = BotConfig(full_research_section_timeout_seconds=1)
    report = FullResearchReporter(db, config, logger, reports_dir=tmp_path / "reports").build_report(mode="compact")
    assert "total labels: 600" in report
    assert not any("Full report section timeout: labels" in message for message in logger.messages)


def test_full_report_generates_with_labels(tmp_path):
    db = make_db(tmp_path)
    insert_observation(db, index=1, label=1, barrier="TP1", ret=0.03, operated=True)
    insert_observation(db, index=2, label=-1, barrier="SL", ret=-0.02)
    report = FullResearchReporter(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports").build_report()
    assert "total labels: 2" in report
    assert "TP1 count: 1" in report
    assert "SL count: 1" in report
    assert "senales operadas: 1" in report


def test_full_report_detects_low_profit_factor(tmp_path):
    db = make_db(tmp_path)
    insert_observation(db, index=1, label=1, barrier="TP1", ret=0.01)
    insert_observation(db, index=2, label=-1, barrier="SL", ret=-0.05)
    report = FullResearchReporter(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports").build_report()
    assert "profit factor insuficiente: SI" in report
    assert "recomendacion clara: NO LIVE" in report


def test_full_report_detects_too_many_time_labels(tmp_path):
    db = make_db(tmp_path)
    for index in range(4):
        insert_observation(db, index=index, label=0, barrier="TIME", ret=0.0)
    insert_observation(db, index=5, label=1, barrier="TP1", ret=0.03)
    report = FullResearchReporter(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports").build_report()
    assert "demasiadas TIME: SI" in report


def test_full_report_detects_empty_strategy_variant_results(tmp_path):
    db = make_db(tmp_path)
    db.ensure_strategy_variant("reverse_breakout", {"reverse": True})
    report = FullResearchReporter(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports").build_report()
    assert "strategy_variants: 1" in report
    assert "strategy_variant_results: 0" in report
    assert "strategy_variant_results esta en 0" in report


def test_full_report_recommended_config_never_activates_live(tmp_path):
    db = make_db(tmp_path)
    insert_observation(db, label=1, barrier="TP1", ret=0.03)
    reporter = FullResearchReporter(db, BotConfig(), DummyLogger(), reports_dir=tmp_path / "reports")
    reporter.build_report()
    text = (reporter.research_lab.reports_dir / "recommended_config.env").read_text(encoding="utf-8")
    assert "LIVE_TRADING=true" not in text
    assert "DRY_RUN=false" not in text
    assert "LIVE_TRADING=false" in text


def test_full_report_auto_emit_logs_start_end_markers(tmp_path):
    db = make_db(tmp_path)
    insert_observation(db, label=1, barrier="TP1", ret=0.03)
    logger = CaptureLogger()
    reporter = FullResearchReporter(db, BotConfig(), logger, reports_dir=tmp_path / "reports")
    last = _emit_full_research_auto_report_if_due(BotConfig(), reporter, logger, 0.0, 100.0)
    assert last == 100.0
    joined = "\n".join(logger.messages)
    assert START_MARKER in logger.messages
    assert END_MARKER in logger.messages
    assert "Full research report periódico generado" in joined


def test_full_report_initial_emit_logs_initial_generated(tmp_path):
    db = make_db(tmp_path)
    insert_observation(db, label=1, barrier="TP1", ret=0.03)
    logger = CaptureLogger()
    reporter = FullResearchReporter(db, BotConfig(), logger, reports_dir=tmp_path / "reports")
    last = _emit_full_research_auto_report_if_due(BotConfig(), reporter, logger, 0.0, 100.0, initial=True)
    joined = "\n".join(logger.messages)
    assert last == 100.0
    assert START_MARKER in logger.messages
    assert END_MARKER in logger.messages
    assert "Full research report inicial generado" in joined
    assert "COMPACT STARTUP" in joined


def test_strip_full_report_markers_removes_embedded_markers():
    body = _strip_full_report_markers(f"{START_MARKER}\nbody\n{END_MARKER}")
    assert body == "body"


def test_full_report_failure_is_logged_and_does_not_raise():
    class BrokenReporter:
        def build_report(self):
            raise RuntimeError("boom")

    logger = CaptureLogger()
    last = _emit_full_research_auto_report_if_due(BotConfig(), BrokenReporter(), logger, 0.0, 100.0, initial=True)
    assert last == 100.0
    assert START_MARKER in logger.messages
    assert END_MARKER in logger.messages
    assert any("No se pudo generar full research auto-report" in msg for msg in logger.messages)


def test_full_report_section_failure_continues(tmp_path):
    db = make_db(tmp_path)
    logger = CaptureLogger()
    reporter = FullResearchReporter(db, BotConfig(), logger, reports_dir=tmp_path / "reports")
    result = reporter._timed_section("broken_section", lambda: (_ for _ in ()).throw(RuntimeError("boom")), fallback="fallback ok")
    assert result == "fallback ok"
    assert any("No se pudo generar seccion broken_section" in msg for msg in logger.messages)


def test_full_report_section_timeout_is_omitted(tmp_path):
    db = make_db(tmp_path)
    logger = CaptureLogger()
    config = BotConfig(full_research_section_timeout_seconds=1)
    reporter = FullResearchReporter(db, config, logger, reports_dir=tmp_path / "reports")
    result = reporter._timed_section("slow_section", lambda: (time.sleep(2), "too late")[1], fallback="timeout fallback")
    assert result == "timeout fallback"
    assert any("Full report section timeout: slow_section" in msg for msg in logger.messages)


def test_heavy_report_mode_requires_explicit_enable():
    config = BotConfig(full_research_report_mode="heavy", full_research_heavy_report_enabled=False)
    assert _full_research_report_mode(config, initial=False) == "compact"
    enabled = BotConfig(full_research_report_mode="heavy", full_research_heavy_report_enabled=True)
    assert _full_research_report_mode(enabled, initial=False) == "heavy"
