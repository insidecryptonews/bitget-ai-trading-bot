from app.config import BotConfig
from app.database import Database
from app.feature_logger import FeatureLogger
from app.labeler import TripleBarrierLabeler
from app.main import _emit_research_auto_report_if_due
from app.meta_model import MetaModel
from app.regime_detector import MarketRegime
from app.research_engine import ResearchEngine
from app.walkforward import make_walkforward_splits
from tests.helpers import DummyLogger, candles, signal, snapshot


class CaptureLogger(DummyLogger):
    def __init__(self):
        self.messages = []

    def info(self, message, *args, **kwargs):
        self.messages.append(message % args if args else message)


def make_db(tmp_path):
    db = Database(BotConfig(), DummyLogger())
    db.sqlite_path = tmp_path / "research.db"
    db.initialize()
    return db


def test_feature_logger_saves_complete_signal(tmp_path):
    db = make_db(tmp_path)
    logger = FeatureLogger(db, DummyLogger())
    obs_id = logger.log_signal(
        signal=signal(),
        snapshot=snapshot(),
        market_regime=MarketRegime("TREND_UP", risk_on=True),
        all_snapshots={"BTCUSDT": snapshot()},
        selected_by_allocator=True,
    )
    rows = db.fetch_signal_observations()
    assert obs_id == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["strategy_type"] == "BREAKOUT"
    assert rows[0]["confidence_score"] == 88
    assert rows[0]["rsi_14"] == 55
    assert rows[0]["selected_by_allocator"] == 1


def test_labeler_labels_tp_before_sl_as_positive():
    outcome = TripleBarrierLabeler(BotConfig()).label_observation(
        {"id": 7, **signal().__dict__},
        candles([(100, 101, 99, 100), (100, 103.5, 99.5, 103)]),
    )
    assert outcome.label == 1
    assert outcome.first_barrier_hit == "TP1"
    assert outcome.would_have_won


def test_labeler_labels_sl_before_tp_as_negative():
    outcome = TripleBarrierLabeler(BotConfig()).label_observation(
        {"id": 7, **signal().__dict__},
        candles([(100, 101, 99, 100), (100, 101, 97.5, 98)]),
    )
    assert outcome.label == -1
    assert outcome.first_barrier_hit == "SL"
    assert not outcome.would_have_won


def test_labeler_labels_time_as_flat():
    outcome = TripleBarrierLabeler(BotConfig(max_holding_bars=2)).label_observation(
        {"id": 7, **signal().__dict__},
        candles([(100, 101, 99, 100), (100, 101.5, 99.5, 100.5)]),
    )
    assert outcome.label == 0
    assert outcome.first_barrier_hit == "TIME"


def test_meta_model_does_not_activate_with_less_than_300_samples():
    model = MetaModel(BotConfig(enable_meta_model=True, meta_model_mode="filter"))
    assert not model.train([{"label": 1}] * 100)
    assert not model.validated


def test_meta_model_observe_only_does_not_block():
    model = MetaModel(BotConfig(enable_meta_model=True, meta_model_mode="observe_only"))
    model.static_probability = 0.1
    model.validated = True
    decision = model.evaluate({"confidence_score": 88})
    assert decision.meta_decision == "TRADE"
    assert not decision.blocks_trade


def test_meta_model_filter_blocks_low_probability():
    model = MetaModel(BotConfig(enable_meta_model=True, meta_model_mode="filter", meta_min_probability=0.58))
    model.static_probability = 0.4
    model.validated = True
    decision = model.evaluate({"confidence_score": 88})
    assert decision.meta_decision == "SKIP"
    assert decision.blocks_trade


def test_walkforward_splits_train_validation_test_without_mixing_dates():
    rows = [{"timestamp": f"2026-01-{day:02d}", "label": day % 2} for day in range(1, 16)]
    splits = make_walkforward_splits(rows, train_window=5, validation_window=3, test_window=2)
    assert splits
    first = splits[0]
    assert first.train[-1]["timestamp"] < first.validation[0]["timestamp"]
    assert first.validation[-1]["timestamp"] < first.test[0]["timestamp"]


def test_meta_model_never_approves_signal_blocked_by_risk_manager():
    model = MetaModel(BotConfig(enable_meta_model=True, meta_model_mode="filter"))
    model.static_probability = 0.99
    model.validated = True
    decision = model.evaluate({"confidence_score": 99}, risk_manager_approved=False)
    assert decision.meta_decision == "SKIP"
    assert decision.blocks_trade
    assert "RiskManager" in decision.reason


def test_research_report_includes_paper_open_closed_and_no_labels_message(tmp_path):
    db = make_db(tmp_path)
    trade_open = db.record_trade(mode="paper", signal=signal(), status="PAPER_OPEN")
    trade_closed = db.record_trade(mode="paper", signal=signal(), status="PAPER_OPEN")
    db.update_trade_status(trade_closed, "STOP_LOSS", realized_pnl=-1.0)
    FeatureLogger(db, DummyLogger()).log_signal(
        signal=signal(),
        snapshot=snapshot(),
        market_regime=MarketRegime("TREND_UP"),
        all_snapshots={"BTCUSDT": snapshot()},
        operated=True,
    )

    report = ResearchEngine(db, DummyLogger()).build_report()
    assert "operaciones paper abiertas: 1" in report
    assert "operaciones paper cerradas: 1" in report
    assert "Aun no hay etiquetas triple-barrier suficientes" in report
    assert trade_open > 0


def test_auto_research_report_logs_markers_when_due():
    logger = CaptureLogger()
    config = BotConfig(enable_research_auto_report=True, research_report_interval_minutes=60)

    class FakeResearchEngine:
        def build_report(self):
            return "Research report\nok"

    last = _emit_research_auto_report_if_due(config, FakeResearchEngine(), logger, 0.0, 100.0)
    assert last == 100.0
    assert any("===== RESEARCH REPORT START =====" in msg for msg in logger.messages)
    assert any("===== RESEARCH REPORT END =====" in msg for msg in logger.messages)


def test_auto_research_report_waits_for_interval():
    logger = CaptureLogger()
    config = BotConfig(enable_research_auto_report=True, research_report_interval_minutes=60)

    class FakeResearchEngine:
        def build_report(self):
            raise AssertionError("No debe ejecutarse antes del intervalo")

    last = _emit_research_auto_report_if_due(config, FakeResearchEngine(), logger, 100.0, 120.0)
    assert last == 100.0
    assert logger.messages == []
