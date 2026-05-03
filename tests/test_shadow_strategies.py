import json

from app.config import BotConfig
from app.database import Database
from app.feature_logger import FeatureLogger
from app.regime_detector import MarketRegime
from app.research_engine import ResearchEngine
from app.shadow_strategies import ShadowStrategyEngine
from tests.helpers import DummyLogger, signal, snapshot


def make_db(tmp_path):
    db = Database(BotConfig(), DummyLogger())
    db.sqlite_path = tmp_path / "shadow.db"
    db.initialize()
    return db


def test_reverse_shadow_long_becomes_short_with_correct_barriers(tmp_path):
    db = make_db(tmp_path)
    feature_logger = FeatureLogger(db, DummyLogger())
    engine = ShadowStrategyEngine(db, feature_logger, DummyLogger())
    base_signal = signal("LONG")
    base_observation = feature_logger.build_observation(
        signal=base_signal,
        snapshot=snapshot(),
        market_regime=MarketRegime("TREND_UP"),
        all_snapshots={"BTCUSDT": snapshot()},
        operated=False,
        block_reason="",
        selected_by_allocator=False,
        risk_manager_approved=False,
    )
    engine.log_variants(signal=base_signal, base_observation=base_observation, market_regime=MarketRegime("TREND_UP"))
    rows = db.fetch_signal_observations()
    reverse_rows = [row for row in rows if json.loads(row["variant_params_json"]).get("reverse")]

    assert reverse_rows
    reverse = reverse_rows[0]
    assert reverse["shadow_strategy"] == 1
    assert reverse["side"] == "SHORT"
    assert reverse["stop_loss"] > reverse["entry_price"]
    assert reverse["take_profit_1"] < reverse["entry_price"]
    assert reverse["take_profit_2"] < reverse["entry_price"]
    assert reverse["original_side"] == "LONG"
    assert reverse["original_strategy_type"] == "BREAKOUT"


def test_reverse_shadow_short_becomes_long_with_correct_barriers(tmp_path):
    db = make_db(tmp_path)
    feature_logger = FeatureLogger(db, DummyLogger())
    engine = ShadowStrategyEngine(db, feature_logger, DummyLogger())
    base_signal = signal("SHORT")
    base_observation = feature_logger.build_observation(
        signal=base_signal,
        snapshot=snapshot(),
        market_regime=MarketRegime("RISK_ON"),
        all_snapshots={"BTCUSDT": snapshot()},
        operated=False,
        block_reason="",
        selected_by_allocator=False,
        risk_manager_approved=False,
    )
    engine.log_variants(signal=base_signal, base_observation=base_observation, market_regime=MarketRegime("RISK_ON"))
    reverse = [row for row in db.fetch_signal_observations() if json.loads(row["variant_params_json"]).get("reverse")][0]

    assert reverse["side"] == "LONG"
    assert reverse["stop_loss"] < reverse["entry_price"]
    assert reverse["take_profit_1"] > reverse["entry_price"]
    assert reverse["take_profit_2"] > reverse["entry_price"]


def test_research_export_writes_expected_files(tmp_path):
    db = make_db(tmp_path)
    feature_logger = FeatureLogger(db, DummyLogger())
    feature_logger.log_signal(
        signal=signal(),
        snapshot=snapshot(),
        market_regime=MarketRegime("TREND_UP"),
        all_snapshots={"BTCUSDT": snapshot()},
    )

    path = ResearchEngine(db, DummyLogger()).export(tmp_path / "exports")
    assert (path / "signal_observations.csv").exists()
    assert (path / "signal_labels.json").exists()
    assert (path / "summaries.json").exists()


def test_variants_report_marks_insufficient_evidence(tmp_path):
    db = make_db(tmp_path)
    variant_id = db.ensure_strategy_variant("reverse_long", {"family": "reverse", "reverse": True}, enabled=True)
    observation_id = db.record_signal_observation(
        {
            "symbol": "BTCUSDT",
            "side": "SHORT",
            "strategy_type": "REVERSE_BREAKOUT",
            "market_regime": "TREND_UP",
            "confidence_score": 88,
            "entry_price": 100,
            "stop_loss": 102,
            "take_profit_1": 97,
            "take_profit_2": 95,
            "risk_reward_ratio": 1.5,
            "shadow_strategy": 1,
            "strategy_variant_id": variant_id,
            "variant_params_json": json.dumps({"family": "reverse", "reverse": True}),
            "original_side": "LONG",
            "original_strategy_type": "BREAKOUT",
            "score_bucket": "85-89",
        }
    )
    db.record_signal_label(
        {
            "observation_id": observation_id,
            "label": 1,
            "first_barrier_hit": "TP1",
            "bars_to_outcome": 2,
            "max_favorable_excursion": 0.03,
            "max_adverse_excursion": -0.005,
            "realized_return_pct": 0.02,
            "simulated_pnl": 1.0,
            "would_have_won": 1,
        }
    )

    report = ResearchEngine(db, DummyLogger()).build_variants_report()
    assert "reverse_long" in report
    assert "insuficiente evidencia" in report
