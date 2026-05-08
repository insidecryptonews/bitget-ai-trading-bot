from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from app.config import BotConfig, PROJECT_ROOT
from app.database import Database
from app.kronos_research import KronosEvaluator, KronosResearch
from app.virtual_portfolio import VirtualPortfolioResearch
from tests.helpers import DummyLogger


class FakeKronosBackend:
    def __init__(self, direction: str = "LONG") -> None:
        self.direction = direction
        self.calls = 0

    def predict(self, frame, x_timestamp, y_timestamp, *, pred_len, temperature, top_p, sample_count):
        self.calls += 1
        current = float(frame["close"].iloc[-1])
        sign = 1 if self.direction == "LONG" else -1
        rows = []
        for index in range(pred_len):
            close = current * (1 + sign * 0.002 * (index + 1))
            rows.append(
                {
                    "open": current,
                    "high": max(current, close) * 1.001,
                    "low": min(current, close) * 0.999,
                    "close": close,
                    "volume": 100,
                    "amount": close * 100,
                }
            )
        return pd.DataFrame(rows)


class BrokenKronosBackend:
    def predict(self, *args, **kwargs):
        raise ImportError("kronos missing")


def make_db(tmp_path, config=None):
    db = Database(config or BotConfig(), DummyLogger())
    db.sqlite_path = tmp_path / "kronos.db"
    db.initialize()
    return db


def insert_observation(db, side="LONG", label=1):
    obs_id = db.record_signal_observation(
        {
            "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
            "symbol": "BTCUSDT",
            "side": side,
            "strategy_type": "BREAKOUT",
            "confidence_score": 88,
            "market_regime": "TREND_UP",
            "entry_price": 100.0,
            "stop_loss": 98.0 if side == "LONG" else 102.0,
            "take_profit_1": 103.0 if side == "LONG" else 97.0,
            "take_profit_2": 105.0 if side == "LONG" else 95.0,
            "risk_reward_ratio": 1.5,
            "rsi_14": 55,
            "volume_relative": 1.8,
            "distance_to_ema_21": 0.01,
            "spread_pct": 0.0005,
            "normalized_atr": 0.01,
            "momentum_5": 0.01,
            "momentum_15": 0.02,
        }
    )
    db.record_signal_label(
        {
            "observation_id": obs_id,
            "label": label,
            "first_barrier_hit": "TP1" if label == 1 else "SL",
            "bars_to_outcome": 4,
            "max_favorable_excursion": 0.03 if label == 1 else 0.0,
            "max_adverse_excursion": -0.02 if label == -1 else 0.0,
            "realized_return_pct": 0.03 if label == 1 else -0.02,
            "simulated_pnl": 3 if label == 1 else -2,
            "would_have_won": int(label == 1),
        }
    )
    return obs_id


def candle_frame(close=100.0, rows=300):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    data = []
    for index in range(rows):
        price = close + index * 0.01
        data.append(
            {
                "timestamp": start + timedelta(minutes=5 * index),
                "open": price,
                "high": price * 1.002,
                "low": price * 0.998,
                "close": price,
                "volume": 100,
            }
        )
    return pd.DataFrame(data)


def test_kronos_research_disabled_by_default():
    config = BotConfig()
    assert config.enable_kronos_research is False
    assert config.live_trading is False
    assert config.dry_run is True


def test_kronos_missing_does_not_break(tmp_path):
    config = BotConfig(enable_kronos_research=True, kronos_timeout_seconds=5)
    db = make_db(tmp_path, config)
    insert_observation(db)
    result = KronosResearch(config, db, DummyLogger(), backend=BrokenKronosBackend()).run_once(
        limit=10,
        candles_by_symbol={"BTCUSDT": candle_frame()},
    )
    assert result.predictions_created == 0
    assert result.errors == 1
    assert "Kronos research unavailable" in result.to_text()
    assert "final recommendation: NO LIVE" in result.to_text()


def test_kronos_once_saves_prediction_and_updates_observation(tmp_path):
    config = BotConfig(enable_kronos_research=True, kronos_pred_len=3)
    db = make_db(tmp_path, config)
    obs_id = insert_observation(db, side="LONG", label=1)
    result = KronosResearch(config, db, DummyLogger(), backend=FakeKronosBackend("LONG")).run_once(
        limit=10,
        candles_by_symbol={"BTCUSDT": candle_frame()},
    )
    assert result.predictions_created == 1
    assert result.disagreement_count == 0
    prediction = db.fetch_kronos_predictions()[0]
    assert prediction["observation_id"] == obs_id
    observation = db.fetch_signal_observations()[0]
    assert observation["kronos_direction"] == "LONG"
    assert observation["kronos_prediction_id"] == prediction["id"]


def test_kronos_once_does_not_use_execution_engine_or_risk_manager():
    text = (PROJECT_ROOT / "app" / "kronos_research.py").read_text(encoding="utf-8")
    assert "ExecutionEngine" not in text
    assert "RiskManager" not in text


def test_kronos_evaluate_works_with_mock_data(tmp_path):
    config = BotConfig(enable_kronos_research=True)
    db = make_db(tmp_path, config)
    obs_id = insert_observation(db, side="LONG", label=1)
    db.record_kronos_prediction(
        {
            "symbol": "BTCUSDT",
            "observation_id": obs_id,
            "model_name": config.kronos_model_name,
            "tokenizer_name": config.kronos_tokenizer_name,
            "lookback": 256,
            "pred_len": 12,
            "current_close": 100.0,
            "predicted_close": 103.0,
            "predicted_return_pct": 0.03,
            "predicted_range_pct": 0.04,
            "direction": "LONG",
            "confidence_score": 0.8,
            "volatility_score": 0.5,
            "forecast_json": "{}",
        }
    )
    report = KronosEvaluator(db).report()
    assert "KRONOS EVALUATION START" in report
    assert "PF Kronos agrees with bot" in report
    assert "final recommendation: NO LIVE" in report


def test_virtual_portfolio_accepts_kronos_variants(tmp_path):
    config = BotConfig(enable_kronos_research=True)
    db = make_db(tmp_path, config)
    obs_id = insert_observation(db, side="LONG", label=1)
    db.update_signal_observation(
        obs_id,
        kronos_direction="LONG",
        kronos_confidence_score=0.9,
        kronos_disagreement=0,
    )
    result = VirtualPortfolioResearch(db, DummyLogger()).simulate(limit=10, max_concurrent=1000)
    assert result.virtual_trades_simulated >= 1
    names = {row["variant_name"] for row in db.fetch_virtual_strategy_summary()}
    assert "KRONOS_AGREE_ONLY" in names
    assert "KRONOS_HIGH_CONFIDENCE_ONLY" in names


def test_kronos_disagree_reverse_variant(tmp_path):
    config = BotConfig(enable_kronos_research=True)
    db = make_db(tmp_path, config)
    obs_id = insert_observation(db, side="LONG", label=-1)
    db.update_signal_observation(
        obs_id,
        kronos_direction="SHORT",
        kronos_confidence_score=0.8,
        kronos_disagreement=1,
    )
    VirtualPortfolioResearch(db, DummyLogger()).simulate(limit=10, max_concurrent=1000)
    names = {row["variant_name"] for row in db.fetch_virtual_strategy_summary()}
    assert "KRONOS_DISAGREE_REVERSE" in names
