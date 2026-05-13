from __future__ import annotations

from types import SimpleNamespace

from app.config import BotConfig, PROJECT_ROOT
from app.main import _reconcile_paper_if_due
from app.training_pulse import END_MARKER, START_MARKER, TrainingPulse


def test_training_pulse_text_contains_markers():
    pulse = TrainingPulse()
    text = pulse.to_text(BotConfig())
    assert START_MARKER in text
    assert END_MARKER in text
    assert "final_recommendation: NO LIVE" in text


def test_training_pulse_text_respects_max_lines():
    pulse = TrainingPulse()
    for idx in range(20):
        pulse.record_slot_block(f"block {idx}")
    config = BotConfig(training_pulse_max_lines=12)
    text = pulse.to_text(config)
    assert len(text.splitlines()) <= 12
    assert text.splitlines()[-1] == END_MARKER


def test_training_pulse_counts_labels():
    pulse = TrainingPulse()
    pulse.record_labels({"total": 5, "TIME": 2, "SL": 1, "TP1": 1, "TP2": 1})
    text = pulse.to_text(BotConfig())
    assert "labels: total=5 TIME=2 SL=1 TP1=1 TP2=1" in text


def test_training_pulse_counts_signal_sides_and_high_scores():
    pulse = TrainingPulse()
    signals = [
        SimpleNamespace(symbol="BTCUSDT", side="LONG", confidence_score=80, reason="trend"),
        SimpleNamespace(symbol="ETHUSDT", side="SHORT", confidence_score=74, reason="breakdown"),
        SimpleNamespace(symbol="SOLUSDT", side="NO_TRADE", confidence_score=40, reason="choppy"),
    ]
    pulse.record_signals(signals, min_score_to_trade=72)
    assert pulse.signals_long == 1
    assert pulse.signals_short == 1
    assert pulse.signals_no_trade == 1
    assert pulse.high_score_signals_total == 2


def test_training_pulse_detects_missed_high_score():
    pulse = TrainingPulse()
    signals = [SimpleNamespace(symbol="BTCUSDT", side="LONG", confidence_score=88, reason="strong")]
    pulse.record_signals(signals, min_score_to_trade=72)
    pulse.record_high_score_missed("risk_block")
    text = pulse.to_text(BotConfig())
    assert "high_score=1 missed_high_score=1" in text


def test_training_pulse_diagnosis_check_slot():
    pulse = TrainingPulse()
    pulse.record_slot_block("Sin slots de posicion disponibles")
    pulse.record_high_score_missed("Sin slots de posicion disponibles")
    text = pulse.to_text(BotConfig())
    assert "CHECK_SLOT" in text


def test_training_pulse_diagnosis_check_rate_limit():
    pulse = TrainingPulse()
    pulse.record_api_error("HTTP 429 rate limit")
    text = pulse.to_text(BotConfig())
    assert "CHECK_RATE_LIMIT" in text
    assert "api: 429=1 errors=1" in text


def test_reconcile_periodic_does_not_run_when_paper_disabled():
    class DummyDb:
        called = False

    class DummyPaper:
        positions = {}

        def load_open_positions_from_db(self):
            raise AssertionError("should not load paper positions when paper trading is off")

    class DummyLogger:
        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

    config = BotConfig(paper_trading=False, lightweight_paper_reconcile_on_start=True)
    last = 123.0
    assert _reconcile_paper_if_due(config, DummyDb(), DummyPaper(), DummyLogger(), TrainingPulse(), last, 999.0) == last


def test_training_summary_and_acceleration_plan_cli_exist():
    text = (PROJECT_ROOT / "app" / "research_lab.py").read_text(encoding="utf-8")
    assert '"training-summary"' in text
    assert '"acceleration-plan"' in text
